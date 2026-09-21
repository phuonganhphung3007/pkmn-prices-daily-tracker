"""
One-off (and safely re-runnable) backfill of data/master_prices.csv into BigQuery.

Rows are grouped by snapshot_date and each day replaces its own partition, so
running this twice produces the same table, never duplicates. Snapshot files
in data/snapshots/ are subsets of the master file, so they aren't loaded
separately; the script only warns if a snapshot has rows the master lacks.

Usage:
  python scripts/backfill_bigquery.py            # load everything
  python scripts/backfill_bigquery.py --dry-run  # parse + count, no BigQuery
"""

import argparse
import csv
import sys
from collections import defaultdict

from bq import PriceTable, coerce_row
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
    parser.add_argument("--dry-run", action="store_true", help="parse and report only")
    args = parser.parse_args()

    if not MASTER_CSV.exists():
        raise SystemExit(f"{MASTER_CSV} not found.")

    by_day = defaultdict(list)
    for row in read_csv(MASTER_CSV):
        by_day[row["snapshot_date"]].append(coerce_row(row))
    print(f"{MASTER_CSV.name}: {sum(map(len, by_day.values()))} rows across {len(by_day)} day(s)")

    for snap in sorted(SNAPSHOTS_DIR.glob("*.csv")):
        n_snap = len(read_csv(snap))
        n_master = len(by_day.get(snap.stem, []))
        if n_snap > n_master:
            print(f"  WARNING: {snap.name} has {n_snap} rows but master has {n_master} for that day")

    if args.dry_run:
        for day in sorted(by_day):
            print(f"  {day}: {len(by_day[day])} rows (dry run)")
        return

    table = PriceTable()
    for day in sorted(by_day):
        table.load_rows(by_day[day], day, replace=True)
        print(f"  {day}: loaded {len(by_day[day])} rows into {table.table_id}")
    print("Backfill complete.")


if __name__ == "__main__":
    main()
