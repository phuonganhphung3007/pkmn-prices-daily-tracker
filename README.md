# pkmnprices-tracker

Daily Pokémon card price tracker built on the [pkmnprices.com](https://pkmnprices.com) API (Free plan). Collects TCGplayer (USD) prices for a watchlist of cards once a day via GitHub Actions and saves them to a SQLite database (plus CSV).

## How it works

```
config/watchlist.csv        <- cards you're tracking (id, tcg_player_id, name, set_name)
scripts/build_watchlist.py  <- one-time/occasional: discover card IDs, add to watchlist
scripts/fetch_prices.py     <- daily: GET /v1/cards/:id for each watchlisted card
data/master_prices.csv      <- full append-only history, one row per price entry per day
data/snapshots/YYYY-MM-DD.csv <- just that day's rows
data/pokemon.db             <- SQLite: cards + price_history tables
scripts/db.py               <- SQLite table setup + save helpers
scripts/backfill_db.py      <- load a local CSV (master or one snapshot) into pokemon.db
scripts/sync_all_cards.py   <- fill pokemon.db with every card in the API (multi-day)
.github/workflows/daily-price-update.yml <- cron job: fetch_prices.py -> pokemon.db, committed back
```

Discovery (searching for cards) and daily collection (pricing cards you've already chosen) are **deliberately separate scripts**, because both `GET /v1/cards` (search) and `GET /v1/cards/:id` (detail) bill 1 credit per item returned. If discovery and collection were combined, you'd re-pay the search cost every single day. Splitting them means:

- You pay the search/discovery cost once per card, ever.
- The daily job's cost is exactly `1 credit × watchlist size` — fully predictable.

## Setup

1. `pip install -r requirements.txt`
2. Copy `.env.example` to `.env` and add your key from the [pkmnprices dashboard](https://pkmnprices.com/dashboard).
3. Build your watchlist, e.g.:
   ```
   python scripts/build_watchlist.py --set-id 123
   python scripts/build_watchlist.py --name "Charizard"
   python scripts/build_watchlist.py --tcg-player-id 519184
   ```
4. Run a manual fetch to test: `python scripts/fetch_prices.py`
5. For GitHub Actions: add `PKMNPRICES_API_KEY` as a repo secret (Settings → Secrets and variables → Actions), push this repo, and the workflow runs daily on its own (or trigger it manually from the Actions tab via "Run workflow").

## SQLite database (`data/pokemon.db`)

All data is saved in one SQLite file, `data/pokemon.db` (override with `PKMNPRICES_DB_PATH`). It's created on first run, needs no server or account, and is committed back to the repo by the daily workflow. Two tables, based on `data/pokemon.sql`:

- `cards`: one row per `card_id`, the latest known metadata (`name`, `card_number`, `rarity`, `card_type`, `set_id`, `tcg_player_id`). Upserted on every load, so a card's row is updated in place, never duplicated. A missing value in new data never overwrites a known one.
- `price_history`: one row per `(card_id, price_date, source, condition, variant)`, the same grain as a row in `master_prices.csv`. `price` is the API's `market_price`, `price_date` is `YYYY-MM-DD`. Loading the same day again replaces rows instead of duplicating them.

If a run stops early (credit limit, error), the next run asks `price_history` which cards it already has for today and continues from there.

**Load existing CSV history** (safe to re-run):
```
python scripts/backfill_db.py --dry-run   # parse + count only
python scripts/backfill_db.py             # loads data/master_prices.csv
python scripts/backfill_db.py --file data/snapshots/2026-09-15.csv   # just one day
```

**Sync every card** the API knows about with `scripts/sync_all_cards.py`. It pages through `GET /v1/cards` and upserts every card into `cards`. Prices included in a list item go straight into `price_history`. Cards without prices are queued, and each one is fetched with `GET /v1/cards/:id`. On the Free plan the list never includes prices and the catalogue has ~28,700 cards, so a full sync costs about 2 credits per card and takes many days. Progress is saved in `data/.progress-sync-all.json`, so just re-run it until it prints "All cards synced."
```
python scripts/sync_all_cards.py --max-pages 1   # try one page
python scripts/sync_all_cards.py                 # continue where it left off
```

Example: latest price per card and variant

```sql
SELECT c.name, c.card_number, c.rarity, p.*
FROM price_history p
JOIN cards c USING (card_id)
WHERE p.price_date = (SELECT MAX(price_date) FROM price_history);
```

## CSV schema (`data/master_prices.csv`)

One row per `(card, price entry, day)`. A card with 5 TCGplayer condition/variant combinations produces 5 rows per day it's fetched.

| Column | Source |
|---|---|
| `snapshot_date` | date the row was collected |
| `id`, `tcg_player_id`, `name`, `number`, `total_set_number`, `rarity`, `artist`, `image_url` | card identity fields |
| `set_id`, `set_name` | from the nested `set` object |
| `stage`, `card_type`, `hp`, `weakness`, `resistance`, `retreat_cost`, `energy_type`, `ability`, `flavor_text`, `attacks` | TCGplayer-sourced attributes (often null for Trainers/Energy/older cards) |
| `cardmarket_url`, `cardmarket_product_id` | Cardmarket mapping identifiers (may be null; unrelated to plan tier) |
| `price_source`, `price_currency`, `price_condition`, `price_variant`, `market_price`, `price_created_at` | one row's worth of the `prices[]` array |
| `raw_json` | the full API response for that card, for debugging/recovering fields not yet mapped to a column |

## Free plan: coverage and limits

**Credits:** 500/day, reset daily, 60 requests/min, 1 key. Every `GET /v1/cards/:id` call costs 1 credit (single item returned).

**Daily coverage:** with a 10-credit safety buffer built into `common.py`, the daily job can fetch **up to ~490 cards per day**, one credit each. That's a hard ceiling on the Free plan — a bigger watchlist just means the job stops partway through and picks up the rest on the next run (resume is automatic via the per-day progress file).

**Discovery cost is separate and comes out of the same 500/day pool.** Building a watchlist via `GET /v1/cards` also bills 1 credit per item returned, at up to 100 per page. Searching a 250-card set to seed the watchlist costs ~250 credits *that day*, leaving only ~250 for the daily fetch run on the same day. For large watchlists, either spread discovery across a few days or run it a day before you need full daily coverage.

**What the Free plan cannot fetch at all** (all return `403`):
- Cardmarket / EUR prices — the `prices[]` array only ever contains `tcgplayer`/USD rows; German-language cards have *no* USD pricing, so they're effectively untrackable on Free.
- Japanese or German cards — `language=Japanese`/`German` is rejected outright; only English cards are servable.
- Price history (`/cards/:id/prices/history`) — this repo's own `master_prices.csv` becomes your history instead, built up one day at a time.
- Any listings endpoint (`/listings/ebay`, `/listings/cardmarket`, `/listings/tcgplayer`) — no live marketplace offers or eBay sold comps.
- Sealed products (`/sealed`, `/sealed/:id`, and their sub-endpoints) — booster boxes, ETBs, etc. are Pro+ only.

Everything else on `GET /v1/cards/:id` — all the requested fields, plus `stage`, `card_type`, `ability`, `attacks`, `cardmarket_url`/`cardmarket_product_id` mapping IDs, etc. — is returned in full on Free; it's specifically EUR pricing, non-English cards, history, listings, and sealed products that are gated.

### If you outgrow Free

Pro (20,000 credits/day) removes the coverage ceiling almost entirely — e.g. tracking 5,000 cards daily costs 5,000 credits, well inside budget — and unlocks EUR prices, Japanese/German cards, price history (so you could stop rolling your own), and listings/sealed data.

## Notes

- Rate limiting (60/min on Free) is handled with exponential backoff in `common.py`; at 1 request per card this is very unlikely to be hit before the credit limit is.
- Every run writes both `data/pokemon.db` and the CSVs; the workflow commits all of `data/` back to the repo.
- `data/.progress-YYYY-MM-DD.json` is gitignored — it's a same-day resume aid, not meant to persist across days.
