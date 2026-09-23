"""
Daily price collection job.

For every card in config/watchlist.csv, calls GET /v1/cards/:id (1 credit
each on the Free plan) and appends one CSV row per price entry returned
(a card with 5 TCGplayer condition/variant rows produces 5 output rows).

Writes to:
  data/master_prices.csv        - full history, append-only
  data/snapshots/YYYY-MM-DD.csv - just today's rows, for easy diffing
  BigQuery (when GCP_PROJECT_ID is set) - the system of record; see bq.py

Resume support: if the job is interrupted (credit limit hit, network error,
workflow timeout), data/.progress-YYYY-MM-DD.json remembers which card IDs
were already fetched today, and BigQuery is asked which IDs it already holds
for today (the CI runner's local files don't survive between runs). Re-running
the same day picks up where it left off instead of re-spending credits.
"""

import csv
import json
from datetime import date
from pathlib import Path

import bq
from common import ApiClient, CreditBudgetExhausted, DATA_DIR, load_json, save_json

WATCHLIST_PATH = Path(__file__).resolve().parent.parent / "config" / "watchlist.csv"
MASTER_CSV = DATA_DIR / "master_prices.csv"
SNAPSHOTS_DIR = DATA_DIR / "snapshots"

# Flush to BigQuery every N cards so an interrupted run loses at most one batch.
BQ_BATCH_SIZE = 50

CSV_FIELDS = [
    "snapshot_date",
    "id",
    "tcg_player_id",
    "name",
    "number",
    "total_set_number",
    "rarity",
    "artist",
    "set_id",
    "set_name",
    "stage",
    "card_type",
    "hp",
    "weakness",
    "resistance",
    "retreat_cost",
    "energy_type",
    "ability",
    "flavor_text",
    "attacks",
    "cardmarket_url",
    "cardmarket_product_id",
    "price_source",
    "price_currency",
    "price_condition",
    "price_variant",
    "market_price",
    "price_created_at",
    "image_url",
    "raw_json",
]


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
    row (with price fields blank) so it isn't silently dropped from the CSV."""
    set_info = card.get("set") or {}
    base = {
        "snapshot_date": today,
        "id": card.get("id"),
        "tcg_player_id": card.get("tcg_player_id"),
        "name": card.get("name"),
        "number": card.get("number"),
        "total_set_number": card.get("total_set_number"),
        "rarity": card.get("rarity"),
        "artist": card.get("artist"),
        "set_id": set_info.get("id"),
        "set_name": set_info.get("name"),
        "stage": card.get("stage"),
        "card_type": card.get("card_type"),
        "hp": card.get("hp"),
        "weakness": card.get("weakness"),
        "resistance": card.get("resistance"),
        "retreat_cost": card.get("retreat_cost"),
        "energy_type": json.dumps(card.get("energy_type")) if card.get("energy_type") else None,
        "ability": card.get("ability"),
        "flavor_text": card.get("flavor_text"),
        "attacks": json.dumps(card.get("attacks")) if card.get("attacks") else None,
        "cardmarket_url": card.get("cardmarket_url"),
        "cardmarket_product_id": card.get("cardmarket_product_id"),
        "image_url": card.get("image_url"),
        # Full raw response kept for debugging / recovering fields not yet
        # mapped to a column — cheap insurance against API shape changes.
        "raw_json": json.dumps(card, separators=(",", ":")),
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


def append_rows(path: Path, rows: list[dict]):
    path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not path.exists()
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        if write_header:
            writer.writeheader()
        writer.writerows(rows)


def main():
    today = date.today().isoformat()
    progress_path = DATA_DIR / f".progress-{today}.json"
    done_ids = set(load_json(progress_path, []))

    store = bq.BigQueryStore() if bq.is_enabled() else None
    if store:
        done_ids |= store.done_card_ids(today)
    else:
        print("GCP_PROJECT_ID not set; writing CSV only, skipping BigQuery.")

    watchlist = load_watchlist()
    print(f"Watchlist has {len(watchlist)} card(s); {len(done_ids)} already fetched today.")

    client = ApiClient()
    snapshot_path = SNAPSHOTS_DIR / f"{today}.csv"

    pending: list[dict] = []  # rows fetched but not yet in BigQuery

    def flush():
        if store and pending:
            store.upsert_cards([bq.to_card_row(r) for r in pending])
            store.load_price_history(
                [bq.to_price_history_row(r) for r in pending], today, replace=False
            )
            pending.clear()

    fetched, failed = 0, 0
    try:
        for entry in watchlist:
            card_id = int(entry["id"])
            if card_id in done_ids:
                continue

            try:
                card = client.get(f"/cards/{card_id}")
            except CreditBudgetExhausted as e:
                print(f"Stopping — {e}")
                break
            except RuntimeError as e:
                print(f"Skipping card {card_id}: {e}")
                failed += 1
                done_ids.add(card_id)  # don't retry a permanent error (e.g. 404) all day
                save_json(progress_path, sorted(done_ids))
                continue

            rows = flatten_card(card, today)
            append_rows(MASTER_CSV, rows)
            append_rows(snapshot_path, rows)
            pending.extend(rows)

            done_ids.add(card_id)
            save_json(progress_path, sorted(done_ids))
            fetched += 1

            if fetched % BQ_BATCH_SIZE == 0:
                flush()
                print(
                    f"  {fetched} cards fetched "
                    f"(credits used: {client.credits_charged_today}/{client.credits_limit})"
                )
    finally:
        # Also runs on errors/Ctrl-C so already-paid-for rows still land.
        flush()

    print(
        f"Done. Fetched {fetched} card(s), {failed} failed/skipped, "
        f"{len(watchlist) - len(done_ids)} remaining for a future run. "
        f"Credits used today: {client.credits_charged_today}/{client.credits_limit}."
    )


if __name__ == "__main__":
    main()
