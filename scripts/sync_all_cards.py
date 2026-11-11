"""
Fill the database with every card the API knows about, then keep it priced.

Phase 1 (get all): page through GET /v1/cards and upsert every card into the
  `cards` table. If a list item already carries a `prices` array, its rows go
  straight into `price_history` for today. If it has no prices, the card id
  is queued for phase 2.
Phase 2 (one by one): for each queued card, GET /v1/cards/:id and load its
  prices, same as fetch_prices.py does.
Phase 3 (once the catalogue's fully discovered): once phase 1 has paged every
  list page and phase 2's queue is empty — i.e. every card the API knows
  about is in `cards` — there's no more discovery work left for this script
  to do, but there's often still credit budget for the run. Rather than stop,
  it hands off to fetch_prices.run(), which refreshes prices for every card
  in `cards` (itself resumable/skip-if-already-done-today), so a run that
  finishes catalogue discovery early still spends the rest of its budget
  usefully instead of going idle.

Both list/detail endpoints bill 1 credit per item returned, and the full
catalogue is far bigger than one day's credit budget, so phases 1-2 are built
to run over many days: data/.progress-sync-all.json remembers the next list
page and the detail queue, and cards already priced today in data/pokemon.db
are skipped. Just re-run it (e.g. once a day) until phase 1-2 reports it's
finished — after that, every run is phase 3 only.

Usage:
  python scripts/sync_all_cards.py                  # continue where it left off
  python scripts/sync_all_cards.py --max-pages 5    # cap list pages this run
  python scripts/sync_all_cards.py --list-only      # phase 1 only, no detail/price calls
  python scripts/sync_all_cards.py --restart        # forget progress, start at page 1
"""

import argparse
from datetime import date

import db
import fetch_prices
from common import ApiClient, CreditBudgetExhausted, DATA_DIR, load_json, save_json

PROGRESS_PATH = DATA_DIR / ".progress-sync-all.json"
PER_PAGE = 100


def load_progress() -> dict:
    p = load_json(PROGRESS_PATH, {})
    return {
        "next_page": p.get("next_page", 1),
        "list_done": p.get("list_done", False),
        "detail_queue": p.get("detail_queue", []),
    }


class Writer:
    """Writes card + price rows to pokemon.db."""

    def __init__(self, store: db.Store, today: str):
        self.store = store
        self.today = today
        self.card_rows: list[dict] = []

    def add_card_only(self, flat_row: dict):
        self.card_rows.append(db.to_card_row(flat_row))

    def add_priced(self, flat_rows: list[dict]):
        self.store.save_flat_rows(flat_rows)

    def flush(self):
        self.store.upsert_cards(self.card_rows)
        self.card_rows.clear()


def list_phase(client, writer, progress, done_ids, max_pages):
    """Returns False if the run should stop (credits gone)."""
    pages_this_run = 0
    while not progress["list_done"]:
        if max_pages is not None and pages_this_run >= max_pages:
            print(f"Reached --max-pages {max_pages}.")
            return False
        page = progress["next_page"]
        try:
            resp = client.get("/cards", params={"page": page, "per_page": PER_PAGE})
        except CreditBudgetExhausted as e:
            print(f"Stopping list phase at page {page} — {e}")
            return False
        except RuntimeError as e:
            # Transient (rate limiting that outlasted the backoff, a 5xx
            # streak, a network blip) — stop without advancing next_page, so
            # the next run retries this same page instead of losing it.
            print(f"Stopping list phase at page {page} — transient error: {e}")
            return False

        priced, queued = 0, 0
        queue = set(progress["detail_queue"])
        for item in resp["data"]:
            card_id = int(item["id"])
            if item.get("prices"):
                if card_id not in done_ids:
                    writer.add_priced(fetch_prices.flatten_card(item, writer.today))
                    done_ids.add(card_id)
                    priced += 1
                else:
                    writer.add_card_only(fetch_prices.flatten_card({**item, "prices": None}, writer.today)[0])
            else:
                writer.add_card_only(fetch_prices.flatten_card(item, writer.today)[0])
                if card_id not in done_ids and card_id not in queue:
                    queue.add(card_id)
                    progress["detail_queue"].append(card_id)
                    queued += 1
        writer.flush()

        total_pages = resp["pagination"]["total_pages"]
        progress["next_page"] = page + 1
        progress["list_done"] = page >= total_pages or not resp["data"]
        save_json(PROGRESS_PATH, progress)
        pages_this_run += 1
        print(
            f"  page {page}/{total_pages}: {len(resp['data'])} card(s), "
            f"{priced} priced from list, {queued} queued for detail"
        )
    return True


def detail_phase(client, writer, progress, done_ids):
    fetched, failed = 0, 0
    try:
        while progress["detail_queue"]:
            card_id = progress["detail_queue"][0]
            if card_id not in done_ids:
                try:
                    card = client.get(f"/cards/{card_id}")
                except CreditBudgetExhausted as e:
                    print(f"Stopping detail phase — {e}")
                    break
                except RuntimeError as e:
                    print(f"Skipping card {card_id}: {e}")
                    failed += 1
                else:
                    writer.add_priced(fetch_prices.flatten_card(card, writer.today))
                    done_ids.add(card_id)
                    fetched += 1
            progress["detail_queue"].pop(0)

            if fetched and fetched % 50 == 0:
                save_json(PROGRESS_PATH, progress)
                print(f"  {fetched} card(s) priced one by one, {len(progress['detail_queue'])} left")
    finally:
        save_json(PROGRESS_PATH, progress)
    print(f"Detail phase: {fetched} fetched, {failed} failed, {len(progress['detail_queue'])} left in queue.")


def run(
    client: ApiClient,
    store: db.Store,
    max_pages: int | None = None,
    list_only: bool = False,
    restart: bool = False,
) -> dict:
    """The catalog-growth task: page the full card list into `cards`, then
    (unless list_only) fetch + store prices one by one for cards the list
    didn't already include prices for. Once that's fully caught up (unless
    list_only), hands any leftover budget to fetch_prices.run() for a price
    refresh pass over the whole `cards` table — see the module docstring's
    phase 3. Stops early (without raising) if `client` runs out of credits —
    either the real daily limit, or a soft per-task allocation set via
    client.set_allocation() by an orchestrator sharing today's budget across
    multiple tasks. Caller owns the Store's lifecycle."""
    if restart and PROGRESS_PATH.exists():
        PROGRESS_PATH.unlink()
    progress = load_progress()
    today = date.today().isoformat()
    done_ids = store.done_card_ids(today)

    print(
        f"List phase {'done' if progress['list_done'] else 'resuming at page ' + str(progress['next_page'])}; "
        f"{len(progress['detail_queue'])} card(s) queued for detail; "
        f"{len(done_ids)} already priced today."
    )

    writer = Writer(store, today)
    keep_going = list_phase(client, writer, progress, done_ids, max_pages)
    if keep_going and not list_only:
        detail_phase(client, writer, progress, done_ids)

    finished = progress["list_done"] and not progress["detail_queue"]
    if finished:
        print("All cards synced.")
        PROGRESS_PATH.unlink(missing_ok=True)

    price_stats = None
    if finished and not list_only:
        print("Catalogue fully discovered — continuing with a price refresh over all cards.")
        price_stats = fetch_prices.run(client, store)

    return {
        "list_done": progress["list_done"],
        "detail_queue_len": len(progress["detail_queue"]),
        "finished": finished,
        "price_stats": price_stats,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--max-pages", type=int, default=None, help="cap list pages fetched this run")
    ap.add_argument(
        "--list-only", action="store_true", help="skip the detail phase and the price refresh pass"
    )
    ap.add_argument("--restart", action="store_true", help="discard saved progress first")
    args = ap.parse_args()

    store = db.Store()
    client = ApiClient()
    try:
        stats = run(client, store, max_pages=args.max_pages, list_only=args.list_only, restart=args.restart)
    finally:
        store.close()

    if stats["price_stats"]:
        p = stats["price_stats"]
        print(
            f"Price refresh: fetched {p['fetched']} card(s), {p['failed']} failed/skipped, "
            f"{p['remaining']} remaining."
        )

    print(f"Credits used today: {client.credits_charged_today}/{client.credits_limit}.")


if __name__ == "__main__":
    main()
