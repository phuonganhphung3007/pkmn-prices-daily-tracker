"""
Daily price collection job.

For every card already in pokemon.db's `cards` table (the full catalogue, not
a hand-picked watchlist), calls GET /v1/cards/:id (1 credit each) and stores
one price_history row per TCGplayer price entry returned (a card with 5
condition/variant rows produces 5 rows). It then also calls
GET /v1/cards/:id/listings/ebay (1 credit per listing returned, Pro+ only)
and stores every eBay sold comp — including PSA/BGS/CGC graded sales — into
the same table.

Writes to:
  data/pokemon.db - SQLite cards + price_history tables; see db.py

(This used to read config/watchlist.csv and also append to
data/master_prices.csv / data/snapshots/YYYY-MM-DD.csv. Both dropped: the
watchlist was a small hand-picked subset of cards, but the `cards` table —
filled by sync_all_cards.py — is now the actual source of what's trackable,
so refreshing prices for it directly covers everything the watchlist did and
far more, with one fewer file to keep in sync. The CSVs were pure-append and
git-committed; an unbounded one eventually produced a 100MB+ blob in this
repo's history and broke the daily workflow's push. The DB doesn't have
either problem: it's upserted/deduplicated, not appended, and it's the only
thing committed now.)

Resume support: a single persistent cursor (db.Store.get/set_price_cursor,
stored in pokemon.db itself so it survives a fresh CI checkout) remembers the
last card_id attempted, across days — not just within one day. Each run
continues right after that card, so a run that only gets partway through the
catalogue today picks up tomorrow exactly where it stopped instead of
restarting from the first card_id in `cards` (which would otherwise mean the
back of the catalogue never gets reached at all). Once the cursor reaches the
last card_id, the next card wraps back around to the first one — so once
every card has been priced at least once, this naturally becomes a rolling
daily refresh of the whole catalogue instead of a one-time backfill. eBay
fetches resume separately and incrementally within that: each card's `since`
is the newest ingested_at already stored for it, so a later pass over the
same card only pays for listings added since the last time it was visited.
"""

from datetime import date

import db
from common import ApiClient, CreditBudgetExhausted

EBAY_PAGE_LIMIT = 20
# Safety cap so one card with an unexpectedly long eBay sales history can't
# loop forever / blow the daily credit budget on its own; a card past this
# cap just picks up the rest via `since` on a later run.
EBAY_MAX_PAGES = 50


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
    """The price-history task: TCGplayer + eBay listings for cards in
    `cards`, continuing from the persistent price_cursor (see module
    docstring) rather than restarting from the first card_id every run.
    Stops early (without raising) if `client` runs out of credits — either
    the real daily limit, or a soft per-task allocation set via
    client.set_allocation() by an orchestrator sharing today's budget across
    multiple tasks. Caller owns the Store's lifecycle."""
    today = date.today().isoformat()
    card_ids = store.all_card_ids()
    n = len(card_ids)
    if n == 0:
        print("No cards in `cards` yet — nothing to price.")
        return {"fetched": 0, "failed": 0, "remaining": 0, "stopped_early": False}

    cursor = store.get_price_cursor()
    if cursor is None:
        start = 0
        print(f"Catalogue has {n} card(s); no price_cursor yet — starting from the top.")
    else:
        # First card_id strictly after the cursor; wraps to 0 if the cursor
        # was the last (or past the last) card_id in the catalogue.
        start = next((i for i, cid in enumerate(card_ids) if cid > cursor), 0)
        print(f"Catalogue has {n} card(s); resuming after card_id {cursor}.")

    fetched, failed, stopped_early = 0, 0, False
    for offset in range(n):  # at most one full lap per run
        card_id = card_ids[(start + offset) % n]

        try:
            card = client.get(f"/cards/{card_id}")
        except CreditBudgetExhausted as e:
            print(f"Stopping price history — {e}")
            stopped_early = True
            break
        except RuntimeError as e:
            print(f"Skipping card {card_id}: {e}")
            failed += 1
            store.set_price_cursor(card_id)  # move past it either way — don't retry a dead card all day
            continue

        rows = flatten_card(card, today)
        store.save_flat_rows(rows)

        try:
            since = store.last_ebay_ingested_at(card_id)
            listings = fetch_ebay_listings(client, card_id, since=since)
        except CreditBudgetExhausted as e:
            print(f"Stopping price history — {e}")
            store.set_price_cursor(card_id)
            stopped_early = True
            break
        except RuntimeError as e:
            print(f"Skipping eBay listings for card {card_id}: {e}")
        else:
            if listings:
                ebay_rows = flatten_ebay_listings(card_id, listings)
                store.save_prices([db.to_price_history_row(r) for r in ebay_rows])

        store.set_price_cursor(card_id)
        fetched += 1

        if fetched % 50 == 0:
            print(
                f"  {fetched} cards fetched "
                f"(credits used: {client.credits_charged_today}/{client.credits_limit})"
            )

    return {
        "fetched": fetched,
        "failed": failed,
        "remaining": max(0, n - fetched - failed),
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
        f"{stats['remaining']} left in this lap around the catalogue. "
        f"Credits used today: {client.credits_charged_today}/{client.credits_limit}."
    )


if __name__ == "__main__":
    main()
