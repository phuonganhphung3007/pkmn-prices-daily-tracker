"""
SQLite storage for card prices: one file, data/pokemon.db (override with
PKMNPRICES_DB_PATH), with two tables based on data/pokemon.sql:

  cards         - one row per card_id, latest known metadata. Upserted, so it
                  never grows past one row per card.
  price_history - one row per (card_id, price_date, source, condition,
                  variant), the same grain as a row in master_prices.csv.
                  Re-loading the same day replaces rows instead of
                  duplicating them.

data/pokemon.sql's price_history has one row per (card_id, price_date) with
aggregated price/price_min/price_max. We keep the finer grain instead (one row
per condition/variant, e.g. "Near Mint Holofoil" vs "Lightly Played Reverse
Holofoil") so nothing from the API response is collapsed away.
"""

import os
import sqlite3
from decimal import Decimal, InvalidOperation
from pathlib import Path

from common import DATA_DIR

DB_PATH = Path(os.environ.get("PKMNPRICES_DB_PATH", DATA_DIR / "pokemon.db"))

CARD_COLUMNS = ["card_id", "tcg_player_id", "set_id", "name", "card_number", "rarity", "card_type"]
PRICE_COLUMNS = [
    "card_id",
    "price_date",
    "price_source",
    "price_currency",
    "price_condition",
    "price_variant",
    "price",
    "price_created_at",
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS cards (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    card_id       INTEGER NOT NULL UNIQUE,
    tcg_player_id INTEGER,
    set_id        INTEGER,
    name          TEXT,
    card_number   TEXT,
    rarity        TEXT,
    card_type     TEXT,
    created_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS price_history (
    card_id          INTEGER NOT NULL REFERENCES cards(card_id),
    price_date       TEXT NOT NULL,      -- YYYY-MM-DD
    price_source     TEXT,
    price_currency   TEXT,
    price_condition  TEXT,
    price_variant    TEXT,
    price            REAL,               -- the API's market_price
    price_created_at TEXT,
    created_at       TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- IFNULL because SQLite treats NULLs as distinct in a unique index, and a
-- card with no prices still gets one all-NULL row per day.
CREATE UNIQUE INDEX IF NOT EXISTS price_history_grain ON price_history (
    card_id, price_date,
    IFNULL(price_source, ''), IFNULL(price_condition, ''), IFNULL(price_variant, '')
);
CREATE INDEX IF NOT EXISTS price_history_date ON price_history (price_date);
"""


def _blank(v):
    return v is None or (isinstance(v, str) and v.strip() == "")


def _int(v):
    return None if _blank(v) else int(float(v))


def _str(v):
    return None if _blank(v) else str(v)


def _num(v):
    if _blank(v):
        return None
    try:
        return float(Decimal(str(v)))
    except InvalidOperation:
        return None


def to_card_row(flat_row: dict) -> dict:
    """flat_row is one row as produced by fetch_prices.flatten_card (or a line
    from master_prices.csv, same shape): card metadata + one price entry."""
    return {
        "card_id": _int(flat_row.get("id")),
        "tcg_player_id": _int(flat_row.get("tcg_player_id")),
        "set_id": _int(flat_row.get("set_id")),
        "name": _str(flat_row.get("name")),
        "card_number": _str(flat_row.get("number")),
        "rarity": _str(flat_row.get("rarity")),
        "card_type": _str(flat_row.get("card_type")),
    }


def to_price_history_row(flat_row: dict) -> dict:
    return {
        "card_id": _int(flat_row.get("id")),
        "price_date": _str(flat_row.get("snapshot_date")),
        "price_source": _str(flat_row.get("price_source")),
        "price_currency": _str(flat_row.get("price_currency")),
        "price_condition": _str(flat_row.get("price_condition")),
        "price_variant": _str(flat_row.get("price_variant")),
        "price": _num(flat_row.get("market_price")),
        "price_created_at": _str(flat_row.get("price_created_at")),
    }


class Store:
    def __init__(self, path: Path = DB_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.executescript(SCHEMA)

    def close(self):
        self.conn.close()

    def done_card_ids(self, price_date: str) -> set[int]:
        """Card IDs that already have price rows for a day, so a re-run
        resumes instead of re-spending credits."""
        cur = self.conn.execute(
            "SELECT DISTINCT card_id FROM price_history WHERE price_date = ?", (price_date,)
        )
        return {r[0] for r in cur}

    def upsert_cards(self, card_rows: list[dict]):
        """Insert new cards, update existing ones by card_id. A NULL in the
        incoming row never overwrites a known value (e.g. a GET /v1/cards list
        item has no card_type, but an earlier detail fetch may have stored it)."""
        rows = {r["card_id"]: r for r in card_rows if r.get("card_id") is not None}
        if not rows:
            return
        cols = ", ".join(CARD_COLUMNS)
        params = ", ".join(f":{c}" for c in CARD_COLUMNS)
        updates = ", ".join(
            f"{c} = COALESCE(excluded.{c}, cards.{c})" for c in CARD_COLUMNS if c != "card_id"
        )
        with self.conn:
            self.conn.executemany(
                f"""INSERT INTO cards ({cols}) VALUES ({params})
                    ON CONFLICT(card_id) DO UPDATE SET {updates},
                        updated_at = CURRENT_TIMESTAMP""",
                list(rows.values()),
            )

    def save_prices(self, price_rows: list[dict]):
        """Insert price rows; a row with the same (card, date, source,
        condition, variant) replaces the old one, so re-loads are idempotent."""
        rows = [r for r in price_rows if r.get("card_id") is not None and r.get("price_date")]
        if not rows:
            return
        cols = ", ".join(PRICE_COLUMNS)
        params = ", ".join(f":{c}" for c in PRICE_COLUMNS)
        with self.conn:
            self.conn.executemany(
                f"INSERT OR REPLACE INTO price_history ({cols}) VALUES ({params})", rows
            )

    def save_flat_rows(self, flat_rows: list[dict]):
        """Convenience: card upsert + price rows from flatten_card() output."""
        self.upsert_cards([to_card_row(r) for r in flat_rows])
        self.save_prices([to_price_history_row(r) for r in flat_rows])
