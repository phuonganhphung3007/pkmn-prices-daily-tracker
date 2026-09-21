"""
BigQuery storage for the price history.

One table (default `<project>.pkmnprices.prices`), same columns as the CSVs,
partitioned by snapshot_date and clustered by card id. `master_prices.csv` is
just every snapshot concatenated, so a single table covers both: a "snapshot"
is `WHERE snapshot_date = '2026-09-15'`.

Everything goes through load jobs (no streaming inserts, no DML), which keeps
it working on the free BigQuery sandbox and makes writes to a day's partition
cheap and atomic.

Configuration (env vars / .env):
  GCP_PROJECT_ID  required to enable BigQuery; unset = BigQuery is skipped
  BQ_DATASET      default "pkmnprices"
  BQ_TABLE        default "prices"
  BQ_LOCATION     default "US" (only used when the dataset is first created)
Credentials come from Application Default Credentials: `gcloud auth
application-default login` locally, or GOOGLE_APPLICATION_CREDENTIALS in CI.
"""

import os
from decimal import Decimal, InvalidOperation

import common  # noqa: F401  (loads .env before we read the environment)

# Column order matches CSV_FIELDS in fetch_prices.py.
SCHEMA = [
    ("snapshot_date", "DATE"),
    ("id", "INTEGER"),
    ("tcg_player_id", "INTEGER"),
    ("name", "STRING"),
    ("number", "STRING"),  # not always numeric, e.g. "SV107"
    ("total_set_number", "STRING"),
    ("rarity", "STRING"),
    ("artist", "STRING"),
    ("set_id", "INTEGER"),
    ("set_name", "STRING"),
    ("stage", "STRING"),
    ("card_type", "STRING"),
    ("hp", "INTEGER"),
    ("weakness", "STRING"),
    ("resistance", "STRING"),
    ("retreat_cost", "INTEGER"),
    ("energy_type", "STRING"),
    ("ability", "STRING"),
    ("flavor_text", "STRING"),
    ("attacks", "STRING"),
    ("cardmarket_url", "STRING"),
    ("cardmarket_product_id", "INTEGER"),
    ("price_source", "STRING"),
    ("price_currency", "STRING"),
    ("price_condition", "STRING"),
    ("price_variant", "STRING"),
    ("market_price", "NUMERIC"),
    ("price_created_at", "TIMESTAMP"),
    ("image_url", "STRING"),
    ("raw_json", "STRING"),
]
_TYPES = dict(SCHEMA)


def is_enabled() -> bool:
    return bool(os.environ.get("GCP_PROJECT_ID"))


def coerce_row(row: dict) -> dict:
    """Blank/None -> None, and cast each value to what its column expects, so
    the same code handles API responses (ints, floats) and CSV rows (strings)."""
    out = {}
    for col, bq_type in SCHEMA:
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


class PriceTable:
    def __init__(self):
        from google.cloud import bigquery

        self.bq = bigquery
        self.project = os.environ["GCP_PROJECT_ID"]
        self.dataset = os.environ.get("BQ_DATASET", "pkmnprices")
        self.table = os.environ.get("BQ_TABLE", "prices")
        self.location = os.environ.get("BQ_LOCATION", "US")
        self.client = bigquery.Client(project=self.project)
        self.table_id = f"{self.project}.{self.dataset}.{self.table}"
        self._ensure_table()

    def _schema(self):
        return [self.bq.SchemaField(name, t) for name, t in SCHEMA]

    def _ensure_table(self):
        dataset = self.bq.Dataset(f"{self.project}.{self.dataset}")
        dataset.location = self.location
        self.client.create_dataset(dataset, exists_ok=True)

        table = self.bq.Table(self.table_id, schema=self._schema())
        table.time_partitioning = self.bq.TimePartitioning(
            type_=self.bq.TimePartitioningType.DAY, field="snapshot_date"
        )
        table.clustering_fields = ["id"]
        self.client.create_table(table, exists_ok=True)

    def done_ids(self, snapshot_date: str) -> set[int]:
        """Card IDs already stored for a day. This is what makes a re-run (or a
        run after hitting the credit limit) resume instead of re-spending
        credits, since the CI runner's local progress file doesn't survive."""
        query = (
            f"SELECT DISTINCT id FROM `{self.table_id}` "
            "WHERE snapshot_date = @d"
        )
        cfg = self.bq.QueryJobConfig(
            query_parameters=[self.bq.ScalarQueryParameter("d", "DATE", snapshot_date)]
        )
        return {r.id for r in self.client.query(query, job_config=cfg).result()}

    def load_rows(self, rows: list[dict], snapshot_date: str, replace: bool):
        """Load rows into one day's partition. replace=True overwrites that
        partition (idempotent backfill); False appends (daily job)."""
        if not rows:
            return
        cfg = self.bq.LoadJobConfig(
            schema=self._schema(),
            source_format=self.bq.SourceFormat.NEWLINE_DELIMITED_JSON,
            write_disposition=(
                self.bq.WriteDisposition.WRITE_TRUNCATE
                if replace
                else self.bq.WriteDisposition.WRITE_APPEND
            ),
        )
        # The partition decorator pins the load to that day and, with
        # WRITE_TRUNCATE, replaces only that day.
        dest = f"{self.table_id}${snapshot_date.replace('-', '')}"
        job = self.client.load_table_from_json(
            [coerce_row(r) for r in rows], dest, job_config=cfg
        )
        job.result()  # raises on failure
