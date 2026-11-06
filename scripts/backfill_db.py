"""
Load a local CSV (data/master_prices.csv by default, or any single day's
snapshot) into data/pokemon.db's cards + price_history tables.

Safe to re-run: cards are upserted by card_id, and a price row with the same
(card, date, source, condition, variant) replaces the existing one, so
loading the same file twice never creates duplicates.

Usage:
  python scripts/backfill_db.py                                  # load data/master_prices.csv
  python scripts/backfill_db.py --file data/snapshots/2026-09-15.csv
  python scripts/backfill_db.py --dry-run                        # parse + count only
"""

import argparse
import csv
import sys
from collections import Counter
from pathlib import Path

import db
from common import DATA_DIR

MASTER_CSV = DATA_DIR / "master_prices.csv"

# raw_json / flavor_text cells can exceed csv's default 128 KB field limit.
csv.field_size_limit(min(sys.maxsize, 2**31 - 1))


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--file", type=Path, default=MASTER_CSV, help=f"CSV to load (default: {MASTER_CSV})"
    )
    parser.add_argument("--dry-run", action="store_true", help="parse and report only")
    args = parser.parse_args()

    if not args.file.exists():
        raise SystemExit(f"{args.file} not found.")

    with open(args.file, "r", newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    per_day = Counter(r["snapshot_date"] for r in rows)
    print(f"{args.file.name}: {len(rows)} rows across {len(per_day)} day(s)")
    for day in sorted(per_day):
        print(f"  {day}: {per_day[day]} rows")
    if args.dry_run:
        return

    store = db.Store()
    try:
        store.save_flat_rows(rows)
        n_cards = store.conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0]
        n_prices = store.conn.execute("SELECT COUNT(*) FROM price_history").fetchone()[0]
    finally:
        store.close()
    print(f"Loaded into {store.path}: {n_cards} card(s), {n_prices} price row(s) in total.")


if __name__ == "__main__":
    main()
