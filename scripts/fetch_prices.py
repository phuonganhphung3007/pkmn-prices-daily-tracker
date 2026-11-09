"""
Daily price collection job.

For every card in config/watchlist.csv, calls GET /v1/cards/:id (1 credit
each) and stores one price_history row per TCGplayer price entry returned (a
card with 5 condition/variant rows produces 5 rows). It then also calls
GET /v1/cards/:id/listings/ebay (1 credit per listing returned, Pro+ only)
and stores every eBay sold comp — including PSA/BGS/CGC graded sales — into
the same table.

Writes to:
  data/pokemon.db - SQLite cards + price_history tables; see db.py

(This used to also append to data/master_prices.csv and
data/snapshots/YYYY-MM-DD.csv. Dropped — an unbounded, forever-growing,
git-committed CSV with the full raw API response repeated on every single
price-entry row eventually produced a 100MB+ blob in this repo's history and
broke the daily workflow's push. The DB doesn't have this problem: it's
upserted/deduplicated rather than pure-append.)

Resume support: if the job is interrupted (credit limit hit, network error,
workflow timeout), data/.progress-YYYY-MM-DD.json remembers which card IDs
were already fetched today, and pokemon.db is asked which IDs it already holds
for today (the progress file doesn't survive between CI runs, but the committed
database does). Re-running the same day picks up where it left off instead of
re-spending credits. eBay fetches resume separately and incrementally: each
card's `since` is the newest ingested_at already stored for it, so a later run
only pays for listings added after the last one it saw.
"""

import csv
from datetime import date
from pathlib import Path

import db
from common import ApiClient, CreditBudgetExhausted, DATA_DIR, load_json, save_json

WATCHLIST_PATH = Path(__file__).resolve().parent.parent / "config" / "watchlist.csv"

EBAY_PAGE_LIMIT = 20
# Safety cap so one card with an unexpectedly long eBay sales history can't
# loop forever / blow the daily credit budget on its own; a card past this
# cap just picks up the rest via `since` on a later run.
EBAY_MAX_PAGES = 50


def load_watchlist() -> list[dict]:
    if not WATCHLIST_PATH.exists():
        sys_exit_no_watchlist()
    with open(WATCHLIST_PATH, "r", newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def sys_exit_no_watchlist():
    raise SystemExit(
        f"No watchlist found at {WATCHLIST_PATH}. "
        "Run scripts/build_watchlist.py first to populate it."
    )


def flatten_card(card: dict, today: str) -> list[dict]:
    """One row per price entry. A card with no prices yet still gets one
    row (with price fields blank) so db.upsert_cards still records it."""
    set_info = card.get("set") or {}
    base = {
        "snapshot_date": today,
        "id": card.get("id"),
        "tcg_player_id": card.get("tcg_player_id"),
        "name": card.get("name"),
        "number": card.get("number"),
        "rarity": card.get("rarity"),
        "set_id": set_info.get("id"),
        "card_type": card.get("card_type"),
    }

    prices = card.get("prices") or []
    if not prices:
        row = dict(base)
        row.update(
            price_source=None,
            price_currency=None,
            price_condition=None,
            price_variant=None,
            market_price=None,
            price_created_at=None,
        )
        return [row]

    rows = []
    for p in prices:
        row = dict(base)
        row.update(
            price_source=p.get("source"),
            price_currency=p.get("currency"),
            price_condition=p.get("condition"),
            price_variant=p.get("variant"),
            market_price=p.get("market_price"),
            price_created_at=p.get("created_at"),
        )
        rows.append(row)
    return rows


def fetch_ebay_listings(client: ApiClient, card_id: int, since: str | None = None) -> list[dict]:
    """Pages GET /cards/:id/listings/ebay (1 credit per listing returned).
    `since` filters on the API's own ingested_at, so passing the newest
    ingested_at we already have in the DB fetches only what's new."""
    params = {"sort": "date_desc", "limit": EBAY_PAGE_LIMIT}
    if since:
        params["since"] = since

    listings: list[dict] = []
    cursor = None
    for _ in range(EBAY_MAX_PAGES):
        page_params = dict(params)
        if cursor:
            page_params["cursor"] = cursor
        resp = client.get(f"/cards/{card_id}/listings/ebay", params=page_params)
        listings.extend(resp.get("data") or [])
        pagination = resp.get("pagination") or {}
        if not pagination.get("has_more"):
            break
        cursor = pagination.get("next_cursor")
        if not cursor:
            break
    return listings


def flatten_ebay_listings(card_id: int, listings: list[dict]) -> list[dict]:
    """One row per eBay sold listing, in the same flat-row shape fetch_card
    rows use, ready for db.to_price_history_row. price_date is the listing's
    own sold_at (not today) since these are historical individual sales, not
    a daily aggregate like tcgplayer's market_price."""
    rows = []
    for item in listings:
        rows.append(
            {
                "id": card_id,
                "price_date": item.get("sold_at"),
                "price_source": "ebay",
                "price_currency": item.get("currency"),
                "price_condition": None,
                "price_variant": item.get("variant"),
                "market_price": item.get("price"),
                "price_created_at": item.get("ingested_at"),
                "sale_type": item.get("sale_type"),
                "grader": item.get("grader"),
                "grade": item.get("grade"),
                "grade_qualifier": item.get("grade_qualifier"),
                "listing_url": item.get("listing_url"),
                "external_id": item.get("id"),
            }
        )
    return rows


def run(client: ApiClient, store: db.Store) -> dict:
    """The watchlist price-history task: TCGplayer + eBay listings for every
    not-yet-done card today. Stops early (without raising) if `client` runs
    out of credits — either the real daily limit, or a soft per-task
    allocation set via client.set_allocation() by an orchestrator sharing
    today's budget across multiple tasks. Caller owns the Store's lifecycle."""
    today = date.today().isoformat()
    progress_path = DATA_DIR / f".progress-{today}.json"
    done_ids = set(load_json(progress_path, []))
    done_ids |= store.done_card_ids(today)

    watchlist = load_watchlist()
    print(f"Watchlist has {len(watchlist)} card(s); {len(done_ids)} already fetched today.")

    fetched, failed, stopped_early = 0, 0, False
    for entry in watchlist:
        card_id = int(entry["id"])
        if card_id in done_ids:
            continue

        try:
            card = client.get(f"/cards/{card_id}")
        except CreditBudgetExhausted as e:
            print(f"Stopping price history — {e}")
            stopped_early = True
            break
        except RuntimeError as e:
            print(f"Skipping card {card_id}: {e}")
            failed += 1
            done_ids.add(card_id)  # don't retry a permanent error (e.g. 404) all day
            save_json(progress_path, sorted(done_ids))
            continue

        rows = flatten_card(card, today)
        store.save_flat_rows(rows)

        try:
            since = store.last_ebay_ingested_at(card_id)
            listings = fetch_ebay_listings(client, card_id, since=since)
        except CreditBudgetExhausted as e:
            print(f"Stopping price history — {e}")
            done_ids.add(card_id)
            save_json(progress_path, sorted(done_ids))
            stopped_early = True
            break
        except RuntimeError as e:
            print(f"Skipping eBay listings for card {card_id}: {e}")
        else:
            if listings:
                ebay_rows = flatten_ebay_listings(card_id, listings)
                store.save_prices([db.to_price_history_row(r) for r in ebay_rows])

        done_ids.add(card_id)
        save_json(progress_path, sorted(done_ids))
        fetched += 1

        if fetched % 50 == 0:
            print(
                f"  {fetched} cards fetched "
                f"(credits used: {client.credits_charged_today}/{client.credits_limit})"
            )

    return {
        "fetched": fetched,
        "failed": failed,
        "remaining": max(0, len(watchlist) - len(done_ids)),
        "stopped_early": stopped_early,
    }


def main():
    store = db.Store()
    client = ApiClient()
    try:
        stats = run(client, store)
    finally:
        store.close()

    print(
        f"Done. Fetched {stats['fetched']} card(s), {stats['failed']} failed/skipped, "
        f"{stats['remaining']} remaining for a future run. "
        f"Credits used today: {client.credits_charged_today}/{client.credits_limit}."
    )


if __name__ == "__main__":
    main()
