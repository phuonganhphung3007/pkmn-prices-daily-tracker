"""
Load a local CSV (data/master_prices.csv by default, or any single day's
snapshot) straight into BigQuery's cards + price_history tables — no GitHub
Actions run required.

Safely re-runnable: cards are upserted by card_id, and price_history rows are
grouped by price_date with each day replacing its own partition, so running
this twice on the same file produces the same tables, never duplicates.
data/snapshots/ files are subsets of the master file, so they aren't loaded
separately when backfilling the master file; the script only warns if a
snapshot has rows the master lacks.

Usage:
  python scripts/backfill_bigquery.py                          # load data/master_prices.csv
  python scripts/backfill_bigquery.py --file data/snapshots/2026-09-15.csv
  python scripts/backfill_bigquery.py --dry-run                # parse + count, no BigQuery
"""

import argparse
import csv
import sys
from collections import defaultdict
from pathlib import Path

from bq import BigQueryStore, to_card_row, to_price_history_row
from common import DATA_DIR

MASTER_CSV = DATA_DIR / "master_prices.csv"
SNAPSHOTS_DIR = DATA_DIR / "snapshots"

# raw_json / flavor_text cells can exceed csv's default 128 KB field limit.
csv.field_size_limit(min(sys.maxsize, 2**31 - 1))


def read_csv(path):
    with open(path, "r", newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--file", type=Path, default=MASTER_CSV, help=f"CSV to load (default: {MASTER_CSV})"
    )
    parser.add_argument("--dry-run", action="store_true", help="parse and report only")
    args = parser.parse_args()

    if not args.file.exists():
        raise SystemExit(f"{args.file} not found.")

    rows = read_csv(args.file)
    by_day = defaultdict(list)
    for row in rows:
        by_day[row["snapshot_date"]].append(row)
    print(f"{args.file.name}: {len(rows)} rows across {len(by_day)} day(s)")

    if args.file == MASTER_CSV:
        for snap in sorted(SNAPSHOTS_DIR.glob("*.csv")):
            n_snap = len(read_csv(snap))
            n_master = len(by_day.get(snap.stem, []))
            if n_snap > n_master:
                print(f"  WARNING: {snap.name} has {n_snap} rows but master has {n_master} for that day")

    if args.dry_run:
        for day in sorted(by_day):
            print(f"  {day}: {len(by_day[day])} rows (dry run)")
        return

    store = BigQueryStore()

    card_rows = [to_card_row(r) for r in rows]
    n_cards = len({r["card_id"] for r in card_rows})
    store.upsert_cards(card_rows)
    print(f"  upserted {n_cards} card(s) into {store.cards_table_id}")

    for day in sorted(by_day):
        price_rows = [to_price_history_row(r) for r in by_day[day]]
        store.load_price_history(price_rows, day, replace=True)
        print(f"  {day}: loaded {len(price_rows)} price row(s) into {store.price_history_table_id}")

    print("Backfill complete.")


if __name__ == "__main__":
    main()
