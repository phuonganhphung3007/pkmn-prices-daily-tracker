"""
BigQuery storage for card prices, split into two tables per data/pokemon.sql:

  cards         - one row per card_id, latest known metadata. Upserted on load
                  (via a staging table + MERGE), so it never grows past one
                  row per card.
  price_history - append-only, one row per (card_id, price_date, condition,
                  variant) — same grain as a row in master_prices.csv —
                  partitioned by price_date and clustered by card_id.

data/pokemon.sql's price_history has one row per (card_id, price_date) with
aggregated price/price_min/price_max. We keep the finer grain instead (one
row per condition/variant, e.g. "Near Mint Holofoil" vs "Lightly Played
Reverse Holofoil") so no information from the API response is collapsed away;
price_min/price_max are dropped since they only make sense once you aggregate.
Its `cards.id` auto-increment surrogate key is dropped too — BigQuery has no
AUTO_INCREMENT, and card_id is already a stable natural key.

Everything goes through load jobs plus one MERGE per upsert_cards() call (no
streaming inserts), so it keeps working on the free BigQuery sandbox: load
jobs and query jobs (which is what a MERGE is) don't require a billing
account, only streaming inserts (tabledata.insertAll) do.

Configuration (env vars / .env):
  GCP_PROJECT_ID          required to enable BigQuery; unset = BigQuery is skipped
  BQ_DATASET              default "pkmnprices"
  BQ_CARDS_TABLE          default "cards"
  BQ_PRICE_HISTORY_TABLE  default "price_history"
  BQ_LOCATION             default "US" (only used when the dataset is first created)
Credentials come from Application Default Credentials: `gcloud auth
application-default login` locally, or GOOGLE_APPLICATION_CREDENTIALS in CI.
"""

import os
from decimal import Decimal, InvalidOperation

import common  # noqa: F401  (loads .env before we read the environment)

# card_id is the join key between the two tables.
CARD_COLUMNS = [
    ("card_id", "INTEGER"),
    ("tcg_player_id", "INTEGER"),
    ("set_id", "INTEGER"),
    ("name", "STRING"),
    ("card_number", "STRING"),
    ("rarity", "STRING"),
    ("card_type", "STRING"),
]
CARDS_SCHEMA = CARD_COLUMNS + [
    ("created_at", "TIMESTAMP"),
    ("updated_at", "TIMESTAMP"),
]

PRICE_HISTORY_SCHEMA = [
    ("card_id", "INTEGER"),
    ("price_date", "DATE"),
    ("price_source", "STRING"),
    ("price_currency", "STRING"),
    ("price_condition", "STRING"),
    ("price_variant", "STRING"),
    ("price", "NUMERIC"),  # market_price in the CSV / API response
    ("price_created_at", "TIMESTAMP"),
]


def is_enabled() -> bool:
    return bool(os.environ.get("GCP_PROJECT_ID"))


def _coerce(row: dict, schema: list[tuple[str, str]]) -> dict:
    """Blank/None -> None, and cast each value to what its column expects, so
    the same code handles API responses (ints, floats) and CSV rows (strings)."""
    out = {}
    for col, bq_type in schema:
        v = row.get(col)
        if v is None or (isinstance(v, str) and v.strip() == ""):
            out[col] = None
        elif bq_type == "INTEGER":
            out[col] = int(float(v))
        elif bq_type == "NUMERIC":
            try:
                out[col] = str(Decimal(str(v)))
            except InvalidOperation:
                out[col] = None
        elif bq_type == "STRING":
            out[col] = str(v)
        else:  # DATE / TIMESTAMP: ISO strings are accepted as-is
            out[col] = str(v)
    return out


def to_card_row(flat_row: dict) -> dict:
    """flat_row is one row as produced by fetch_prices.flatten_card (or a line
    from master_prices.csv, same shape): card metadata + one price entry."""
    return _coerce(
        {
            "card_id": flat_row.get("id"),
            "tcg_player_id": flat_row.get("tcg_player_id"),
            "set_id": flat_row.get("set_id"),
            "name": flat_row.get("name"),
            "card_number": flat_row.get("number"),
            "rarity": flat_row.get("rarity"),
            "card_type": flat_row.get("card_type"),
        },
        CARD_COLUMNS,
    )


def to_price_history_row(flat_row: dict) -> dict:
    return _coerce(
        {
            "card_id": flat_row.get("id"),
            "price_date": flat_row.get("snapshot_date"),
            "price_source": flat_row.get("price_source"),
            "price_currency": flat_row.get("price_currency"),
            "price_condition": flat_row.get("price_condition"),
            "price_variant": flat_row.get("price_variant"),
            "price": flat_row.get("market_price"),
            "price_created_at": flat_row.get("price_created_at"),
        },
        PRICE_HISTORY_SCHEMA,
    )


def dedupe_cards(card_rows: list[dict]) -> list[dict]:
    """Keep the last occurrence of each card_id (a batch has one card row per
    price entry, all identical for a given card)."""
    by_id = {r["card_id"]: r for r in card_rows if r.get("card_id") is not None}
    return list(by_id.values())


class BigQueryStore:
    def __init__(self):
        from google.cloud import bigquery

        self.bq = bigquery
        self.project = os.environ["GCP_PROJECT_ID"]
        self.dataset = os.environ.get("BQ_DATASET", "pkmnprices")
        self.cards_table = os.environ.get("BQ_CARDS_TABLE", "cards")
        self.price_history_table = os.environ.get("BQ_PRICE_HISTORY_TABLE", "price_history")
        self.location = os.environ.get("BQ_LOCATION", "US")
        self.client = bigquery.Client(project=self.project)

        self.cards_table_id = f"{self.project}.{self.dataset}.{self.cards_table}"
        self.staging_table_id = f"{self.project}.{self.dataset}.{self.cards_table}_staging"
        self.price_history_table_id = f"{self.project}.{self.dataset}.{self.price_history_table}"
        self._ensure_tables()

    def _schema(self, columns):
        return [self.bq.SchemaField(name, t) for name, t in columns]

    def _ensure_tables(self):
        dataset = self.bq.Dataset(f"{self.project}.{self.dataset}")
        dataset.location = self.location
        self.client.create_dataset(dataset, exists_ok=True)

        cards = self.bq.Table(self.cards_table_id, schema=self._schema(CARDS_SCHEMA))
        cards.clustering_fields = ["card_id"]
        self.client.create_table(cards, exists_ok=True)

        # Reused as scratch space by upsert_cards(): truncated and reloaded
        # on every call, then merged into `cards`.
        staging = self.bq.Table(self.staging_table_id, schema=self._schema(CARD_COLUMNS))
        self.client.create_table(staging, exists_ok=True)

        price_history = self.bq.Table(
            self.price_history_table_id, schema=self._schema(PRICE_HISTORY_SCHEMA)
        )
        price_history.time_partitioning = self.bq.TimePartitioning(
            type_=self.bq.TimePartitioningType.DAY, field="price_date"
        )
        price_history.clustering_fields = ["card_id"]
        self.client.create_table(price_history, exists_ok=True)

    def done_card_ids(self, snapshot_date: str) -> set[int]:
        """Card IDs already stored in price_history for a day. This is what
        makes a re-run (or a run after hitting the credit limit) resume
        instead of re-spending credits, since the CI runner's local progress
        file doesn't survive between runs."""
        query = (
            f"SELECT DISTINCT card_id FROM `{self.price_history_table_id}` "
            "WHERE price_date = @d"
        )
        cfg = self.bq.QueryJobConfig(
            query_parameters=[self.bq.ScalarQueryParameter("d", "DATE", snapshot_date)]
        )
        return {r.card_id for r in self.client.query(query, job_config=cfg).result()}

    def upsert_cards(self, card_rows: list[dict]):
        """Load rows (already coerced, via to_card_row) into the cards table,
        keyed by card_id: update the row if the card_id exists, insert it if
        not. Safe to call repeatedly with overlapping card_ids."""
        rows = dedupe_cards(card_rows)
        if not rows:
            return

        load_cfg = self.bq.LoadJobConfig(
            schema=self._schema(CARD_COLUMNS),
            source_format=self.bq.SourceFormat.NEWLINE_DELIMITED_JSON,
            write_disposition=self.bq.WriteDisposition.WRITE_TRUNCATE,
        )
        job = self.client.load_table_from_json(rows, self.staging_table_id, job_config=load_cfg)
        job.result()  # raises on failure

        update_cols = [c for c, _ in CARD_COLUMNS if c != "card_id"]
        set_clause = ", ".join(f"{c} = S.{c}" for c in update_cols)
        insert_cols = [c for c, _ in CARD_COLUMNS]
        merge_sql = f"""
            MERGE `{self.cards_table_id}` T
            USING `{self.staging_table_id}` S
            ON T.card_id = S.card_id
            WHEN MATCHED THEN UPDATE SET {set_clause}, updated_at = CURRENT_TIMESTAMP()
            WHEN NOT MATCHED THEN INSERT
                ({", ".join(insert_cols)}, created_at, updated_at)
                VALUES ({", ".join(f"S.{c}" for c in insert_cols)}, CURRENT_TIMESTAMP(), CURRENT_TIMESTAMP())
        """
        self.client.query(merge_sql).result()

    def load_price_history(self, rows: list[dict], snapshot_date: str, replace: bool):
        """Load rows (already coerced, via to_price_history_row) into one
        day's partition. replace=True overwrites that partition (idempotent
        backfill); False appends (daily job)."""
        if not rows:
            return
        cfg = self.bq.LoadJobConfig(
            schema=self._schema(PRICE_HISTORY_SCHEMA),
            source_format=self.bq.SourceFormat.NEWLINE_DELIMITED_JSON,
            write_disposition=(
                self.bq.WriteDisposition.WRITE_TRUNCATE
                if replace
                else self.bq.WriteDisposition.WRITE_APPEND
            ),
        )
        # The partition decorator pins the load to that day and, with
        # WRITE_TRUNCATE, replaces only that day.
        dest = f"{self.price_history_table_id}${snapshot_date.replace('-', '')}"
        job = self.client.load_table_from_json(rows, dest, job_config=cfg)
        job.result()  # raises on failure
