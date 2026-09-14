"""
Build (or extend) config/watchlist.csv — the list of card IDs that
fetch_prices.py tracks every day.

Why this is a separate script:
GET /v1/cards (the search/list endpoint) bills 1 credit per item RETURNED,
just like GET /v1/cards/:id does. Searching a whole set costs as much as
fetching that many cards' prices. Keeping discovery separate from the daily
fetch means:
  1. You only pay the discovery cost once per card (not every day).
  2. You can spread discovery across multiple days if a set is large.
  3. The daily job's credit spend is 100% predictable: 1 credit x watchlist size.

Usage examples:
  python scripts/build_watchlist.py --set-id 123
  python scripts/build_watchlist.py --name "Charizard"
  python scripts/build_watchlist.py --tcg-player-id 519184
"""

import argparse
import csv
from pathlib import Path

from common import ApiClient, CreditBudgetExhausted, DATA_DIR

WATCHLIST_PATH = Path(__file__).resolve().parent.parent / "config" / "watchlist.csv"
WATCHLIST_FIELDS = ["id", "tcg_player_id", "name", "set_name"]


def load_existing_ids() -> set[int]:
    if not WATCHLIST_PATH.exists():
        return set()
    with open(WATCHLIST_PATH, "r", newline="", encoding="utf-8") as f:
        return {int(row["id"]) for row in csv.DictReader(f)}


def append_watchlist(rows: list[dict]):
    WATCHLIST_PATH.parent.mkdir(exist_ok=True)
    write_header = not WATCHLIST_PATH.exists()
    with open(WATCHLIST_PATH, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=WATCHLIST_FIELDS)
        if write_header:
            writer.writeheader()
        writer.writerows(rows)


def search_cards(client: ApiClient, params: dict, per_page: int = 100, max_pages: int | None = None):
    page = 1
    while True:
        try:
            resp = client.get(
                "/cards", params={**params, "page": page, "per_page": per_page}
            )
        except CreditBudgetExhausted as e:
            print(f"Stopping discovery — {e}")
            return
        for item in resp["data"]:
            yield item
        total_pages = resp["pagination"]["total_pages"]
        print(
            f"  page {page}/{total_pages} "
            f"(credits used so far today: {client.credits_charged_today}/{client.credits_limit})"
        )
        if page >= total_pages or (max_pages and page >= max_pages):
            return
        page += 1


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--set-id", type=int, help="Restrict to a set by ID")
    ap.add_argument("--name", help="Substring match on card name")
    ap.add_argument("--rarity", help="Filter by rarity")
    ap.add_argument("--tcg-player-id", type=int, help="Add a single known card by TCGplayer ID")
    ap.add_argument("--max-pages", type=int, default=None, help="Cap pages fetched this run")
    args = ap.parse_args()

    client = ApiClient()
    existing = load_existing_ids()
    new_rows = []

    if args.tcg_player_id:
        candidates = search_cards(client, {"tcg_player_id": args.tcg_player_id})
    else:
        params = {}
        if args.set_id:
            params["set_id"] = args.set_id
        if args.name:
            params["name"] = args.name
        if args.rarity:
            params["rarity"] = args.rarity
        if not params:
            ap.error("Provide at least one of --set-id / --name / --rarity / --tcg-player-id")
        candidates = search_cards(client, params, max_pages=args.max_pages)

    for card in candidates:
        if card["id"] in existing:
            continue
        new_rows.append(
            {
                "id": card["id"],
                "tcg_player_id": card.get("tcg_player_id"),
                "name": card.get("name"),
                "set_name": (card.get("set") or {}).get("name"),
            }
        )
        existing.add(card["id"])

    if new_rows:
        append_watchlist(new_rows)
        print(f"Added {len(new_rows)} new card(s) to {WATCHLIST_PATH}")
    else:
        print("No new cards found to add.")

    print(f"Watchlist size is now {len(existing)} card(s).")
    if len(existing) > 490:
        print(
            "WARNING: watchlist exceeds ~490 cards — the daily fetch job "
            "(1 credit/card) will not be able to cover it all within the "
            "Free plan's 500 credits/day. Consider trimming the watchlist "
            "or upgrading plans."
        )


if __name__ == "__main__":
    main()
