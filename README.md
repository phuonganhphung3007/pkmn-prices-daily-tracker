# pkmnprices-tracker

Daily Pokémon card price tracker built on the [pkmnprices.com](https://pkmnprices.com) API (Free plan). Collects TCGplayer (USD) prices for a watchlist of cards once a day via GitHub Actions and saves them to a SQLite database (plus CSV).

## How it works

```
config/watchlist.csv        <- cards you're tracking (id, tcg_player_id, name, set_name)
scripts/build_watchlist.py  <- one-time/occasional: discover card IDs, add to watchlist
scripts/daily_sync.py       <- daily entry point: splits today's credits across the two tasks below
scripts/fetch_prices.py     <- task: GET /v1/cards/:id + /listings/ebay for each watchlisted card
scripts/sync_all_cards.py   <- task: fill pokemon.db with every card in the API (multi-day)
data/master_prices.csv      <- full append-only TCGplayer history, one row per price entry per day
data/snapshots/YYYY-MM-DD.csv <- just that day's TCGplayer rows
data/pokemon.db             <- SQLite: cards + price_history tables
scripts/db.py               <- SQLite table setup + save helpers
scripts/backfill_db.py      <- load a local CSV (master or one snapshot) into pokemon.db
.github/workflows/daily-price-update.yml <- cron job: daily_sync.py -> pokemon.db, committed back
```

Discovery (searching for cards) and daily collection (pricing cards you've already chosen) are **deliberately separate scripts**, because both `GET /v1/cards` (search) and `GET /v1/cards/:id` (detail) bill 1 credit per item returned. If discovery and collection were combined, you'd re-pay the search cost every single day. Splitting them means:

- You pay the search/discovery cost once per card, ever.
- The watchlist task's cost is `(1 + eBay listings returned) × watchlist size` — predictable day to day once each card's eBay history has been backfilled once.

### `scripts/daily_sync.py` — the daily entry point

Two tasks compete for the same daily credit pool, with different urgency: catalog growth (`sync_all_cards.py`) can spill over for days without hurting anything, but price history (`fetch_prices.py`) needs to run *every* day to be worth having. Just running catalog growth until it runs out of credits — the naive approach — can burn the entire day's budget on catalog growth alone and leave nothing for that day's price refresh.

`daily_sync.py` fixes that by querying today's real remaining credit pool once, then splitting it:

1. **Catalog growth** (priority 1) gets first claim on a share of the pool — `--catalog-share`, default 50% (`PKMNPRICES_CATALOG_SHARE` env var) — and is hard-capped there even if it would otherwise keep going.
2. **Price history** (priority 2) gets the rest of the pool, plus whatever catalog growth didn't spend (e.g. because it finished a page run early, or is already fully caught up).

Both tasks remain independently resumable (their own progress files / DB state), so this is just how *today's* budget gets divided between them — run it daily (the GitHub Actions workflow already does) and each day picks up exactly where the previous one left off for both tasks.

```
python scripts/daily_sync.py                      # the daily cron entry point
python scripts/daily_sync.py --catalog-share 0.3    # give catalog growth 30% instead of 50%
python scripts/daily_sync.py --max-pages 20         # also cap catalog list pages this run
```

`fetch_prices.py` and `sync_all_cards.py` are still runnable directly (e.g. for a one-off manual fetch or to let one task use the *entire* day's budget) — each only self-limits to the real account-wide daily cap when run on its own.

## Setup

1. `pip install -r requirements.txt`
2. Copy `.env.example` to `.env` and add your key from the [pkmnprices dashboard](https://pkmnprices.com/dashboard).
3. Build your watchlist, e.g.:
   ```
   python scripts/build_watchlist.py --set-id 123
   python scripts/build_watchlist.py --name "Charizard"
   python scripts/build_watchlist.py --tcg-player-id 519184
   ```
4. Run a manual fetch to test: `python scripts/daily_sync.py` (or `python scripts/fetch_prices.py` for just the watchlist task, skipping catalog growth)
5. For GitHub Actions: add `PKMNPRICES_API_KEY` as a repo secret (Settings → Secrets and variables → Actions), push this repo, and the workflow runs daily on its own (or trigger it manually from the Actions tab via "Run workflow").

## SQLite database (`data/pokemon.db`)

All data is saved in one SQLite file, `data/pokemon.db` (override with `PKMNPRICES_DB_PATH`). It's created on first run, needs no server or account, and is committed back to the repo by the daily workflow. Two tables, based on `data/pokemon.sql`:

- `cards`: one row per `card_id`, the latest known metadata (`name`, `card_number`, `rarity`, `card_type`, `set_id`, `tcg_player_id`). Upserted on every load, so a card's row is updated in place, never duplicated. A missing value in new data never overwrites a known one. It never holds grade or price fields — a card can have zero, one, or many `price_history` rows, graded or not.
- `price_history`: holds two different grains, told apart by `price_source`:
  - `tcgplayer`: one row per `(card_id, price_date, condition, variant)`, the same grain as a row in `master_prices.csv` — a daily aggregate `market_price`. Loading the same day again replaces the row instead of duplicating it.
  - `ebay`: one row per individual sold listing from `GET /cards/:id/listings/ebay` (Pro+). `external_id` (the listing's own id) is the real dedup key here, not the date — a card routinely has several eBay rows for the same day, and even several at the same grade, because each is a distinct sale (e.g. three separate PSA 10 sales at different prices). `grader`, `grade`, `grade_qualifier`, `sale_type`, and `listing_url` are only ever populated on these rows; they're always `NULL` on `tcgplayer` rows, which have no notion of grading.

  `price` is the API's `market_price` for `tcgplayer` rows or a sale price for `ebay` rows; `price_date` is `YYYY-MM-DD` — the snapshot date for `tcgplayer`, the listing's own `sold_at` for `ebay`.

An existing `pokemon.db` created before these grading columns existed is migrated in place the first time `db.Store()` runs against it (`ALTER TABLE ... ADD COLUMN`, then the unique index is rebuilt) — no manual steps needed.

If a run stops early (credit limit, error), the next run asks `price_history` which cards it already has for today and continues from there. eBay fetching resumes separately and incrementally: each card's `since` cursor is the newest `ingested_at` already stored for it, so a later run only pays credits for listings added after the last one it saw.

**How many grades does a card have?** It varies, and that's by design — the schema never assumes one grade per card:
- A `tcgplayer` row is always ungraded (raw market price), one row per condition/variant/day.
- An `ebay` row can be anything from zero (no graded sales yet) to dozens — different graders, different grades, or several sales at the identical grade. All of them key off the same `card_id`, so querying "every grade ever seen for this card" is just `SELECT DISTINCT grader, grade, grade_qualifier FROM price_history WHERE card_id = ? AND price_source = 'ebay'`.

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

## Plan: coverage and limits

This repo now runs on **Pro** (20,000 credits/day). `common.py`'s `DAILY_CREDIT_LIMIT` still defaults to 500 (the Free-plan value) but self-corrects on the first request of the day from the live `x-credits-limit` response header, so no `.env` change is required — set `PKMNPRICES_DAILY_LIMIT=20000` only if you want the pre-flight check to know the real ceiling before that first call.

**Daily coverage:** `GET /v1/cards/:id` costs 1 credit/card; `GET /v1/cards/:id/listings/ebay` costs 1 credit per listing returned (up to 20/page). A watchlist of a few hundred cards, each with a handful of new eBay comps a day, comfortably fits in the 20,000/day budget — the daily job stops early and resumes on the next run if it ever doesn't.

**Discovery cost is separate and comes out of the same pool.** Building a watchlist via `GET /v1/cards` also bills 1 credit per item returned, at up to 100 per page.

**What's now available on Pro** (previously `403` on Free):
- EUR / Cardmarket prices, and Japanese/German cards.
- Price history (`/cards/:id/prices/history`) — not wired up here, since `master_prices.csv` + `price_history` already build this repo's own history day by day.
- `GET /cards/:id/listings/ebay` — eBay sold comps, including PSA/BGS/CGC graded sales. `fetch_prices.py` now calls this for every watchlisted card alongside the TCGplayer fetch; see the SQLite section above for how graded rows are stored.
- Sealed products (`/sealed`, `/sealed/:id`, `/sealed/:id/listings/ebay`) — not wired up here; the watchlist/tracker is card-only for now.

## Notes

- Rate limiting (60/min on Free) is handled with exponential backoff in `common.py`; at 1 request per card this is very unlikely to be hit before the credit limit is.
- Every run writes both `data/pokemon.db` and the CSVs; the workflow commits all of `data/` back to the repo.
- `data/.progress-YYYY-MM-DD.json` is gitignored — it's a same-day resume aid, not meant to persist across days.
