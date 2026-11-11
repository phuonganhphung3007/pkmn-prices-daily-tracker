"""
Daily automation: one shared credit pool, split across two priorities.

  1. Catalog growth  (sync_all_cards.py) - discover every card in the API and
     queue the ones without inline prices for a detail/price lookup. This can
     take many days to finish the full ~93,000-card catalog. Once fully
     caught up, it has no more discovery work, so it falls through to the
     same price refresh priority 2 does with whatever budget it has left.
  2. Price history    (fetch_prices.py)   - refresh today's prices (TCGplayer
     market price + eBay sold listings, including graded sales) for every
     card already in `cards`. Needs to run every day to be useful.

Both tasks are already resumable on their own (sync_all_cards.py's progress
file, fetch_prices.py's per-day progress file + what's already in
price_history) — what they don't have on their own is a way to share one
day's credit budget fairly. Running sync_all_cards.py until it's out of
credits (the naive approach) can burn the *entire* daily pool on catalog
growth alone and leave nothing for that day's price refresh.

This script fixes that by giving catalog growth first claim on a share of
today's remaining credits (default 50%, since it's priority 1), then handing
whatever it didn't spend — plus price history's own share — to price
history. Catalog growth still can't starve price history out entirely: its
spending is capped at its share even if it would otherwise keep going. (The
two tasks won't double-spend credits on the same card even when catalog
growth's own leftover-budget price refresh and this script's explicit price
history step both run in one invocation — fetch_prices.run() skips whatever
is already marked done for today.)

Usage:
  python scripts/daily_sync.py                      # the daily cron entry point
  python scripts/daily_sync.py --catalog-share 0.3   # give catalog 30% instead of 50%
  python scripts/daily_sync.py --max-pages 20         # also cap catalog list pages this run
"""

import argparse
import os

import db
import fetch_prices
import sync_all_cards
from common import ApiClient, CreditBudgetExhausted

DEFAULT_CATALOG_SHARE = float(os.environ.get("PKMNPRICES_CATALOG_SHARE", "0.5"))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument(
        "--catalog-share",
        type=float,
        default=DEFAULT_CATALOG_SHARE,
        help=f"fraction of today's remaining credits catalog growth claims first (default {DEFAULT_CATALOG_SHARE})",
    )
    ap.add_argument("--max-pages", type=int, default=None, help="cap catalog list pages this run")
    args = ap.parse_args()
    if not 0 <= args.catalog_share <= 1:
        raise SystemExit("--catalog-share must be between 0 and 1")

    store = db.Store()
    client = ApiClient()
    try:
        try:
            client.get("/cards", params={"page": 1, "per_page": 1})
        except CreditBudgetExhausted as e:
            print(f"No credits left to even start today — {e}")
            return
        except RuntimeError as e:
            print(f"Couldn't reach the API to start today's run — {e}")
            return
        pool = client.remaining_credits()
        catalog_budget = int(pool * args.catalog_share)
        price_budget = pool - catalog_budget
        print(
            f"Today's remaining pool: {pool} credits. Priority 1 (catalog growth) gets "
            f"{catalog_budget}; priority 2 (price history) gets {price_budget} plus "
            f"whatever catalog growth doesn't spend.\n"
        )

        print("--- 1. Catalog growth ---")
        client.set_allocation(catalog_budget)
        catalog_stats = sync_all_cards.run(client, store, max_pages=args.max_pages)
        catalog_spent = client.allocation_spent()
        leftover = max(0, catalog_budget - catalog_spent)
        print(
            f"Catalog growth spent {catalog_spent}/{catalog_budget} credits "
            f"({'fully caught up' if catalog_stats['finished'] else 'not finished — resumes next run'}).\n"
        )
        if catalog_stats.get("price_stats"):
            cp = catalog_stats["price_stats"]
            print(
                f"  (catalogue was already fully discovered, so that budget also went toward a "
                f"price refresh: fetched {cp['fetched']}, {cp['failed']} failed/skipped, "
                f"{cp['remaining']} remaining)\n"
            )

        print("--- 2. Price history ---")
        client.set_allocation(price_budget + leftover)
        price_stats = fetch_prices.run(client, store)
        print(
            f"Price history: fetched {price_stats['fetched']} card(s), "
            f"{price_stats['failed']} failed/skipped, {price_stats['remaining']} remaining.\n"
        )
    finally:
        store.close()

    print(f"Credits used today: {client.credits_charged_today}/{client.credits_limit}.")


if __name__ == "__main__":
    main()
