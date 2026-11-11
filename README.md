# pkmnprices-tracker

Daily Pokémon card price tracker built on the [pkmnprices.com](https://pkmnprices.com) API (Pro plan). Grows a full catalogue of cards and collects TCGplayer (USD) prices and eBay sold comps (including PSA/BGS/CGC graded sales) for every one of them, once a day via GitHub Actions, saving it all to a SQLite database.

## How it works

```
scripts/daily_sync.py       <- daily entry point: splits today's credits across the two tasks below
scripts/sync_all_cards.py   <- task: discover every card in the API into `cards` (multi-day), then price it
scripts/fetch_prices.py     <- task: GET /v1/cards/:id + /listings/ebay for every card already in `cards`
data/pokemon.db             <- SQLite: cards + price_history tables
scripts/db.py               <- SQLite table setup + save helpers
scripts/backfill_db.py      <- one-off: import an externally-supplied CSV export into pokemon.db
.github/workflows/daily-price-update.yml <- cron job: daily_sync.py -> pokemon.db, committed back
```

**No CSVs are written or read by the daily pipeline** — `pokemon.db` is the only input and output. There used to be a hand-picked `config/watchlist.csv` that `fetch_prices.py` priced instead of the full catalogue, plus `data/master_prices.csv` / `data/snapshots/YYYY-MM-DD.csv` that every run appended to. All three are gone: the watchlist was a small subset of what `sync_all_cards.py` discovers anyway, so `fetch_prices.py` now prices every card already in `cards` directly; and the append-only CSVs (with the full raw API response duplicated on *every single price-entry row*) grew a historical blob past 100MB in this repo's git history, which GitHub rejects on push. The DB has neither problem — rows are upserted/deduplicated, not appended forever, and it's the only thing committed now. (`backfill_db.py` still reads a CSV, but only as a one-off import path for an externally-supplied file — nothing in this repo produces one for it to read anymore.)

### `scripts/sync_all_cards.py` — catalogue growth, then price refresh

Three phases, the first two gated behind the previous one finishing (data/.progress-sync-all.json tracks where each run left off):

1. **List** — pages `GET /v1/cards` (1 credit/item) and upserts every card into `cards`. A list item with inline prices is stored immediately; one without is queued for phase 2.
2. **Detail** — once every list page has been walked, fetches `GET /v1/cards/:id` (1 credit/card) one by one for everything phase 1 queued.
3. **Price refresh** — once phases 1-2 are *both* fully caught up (the whole catalogue discovered and priced at least once), there's no more discovery work left, but a run often still has budget to spend. Rather than go idle, it hands off to `fetch_prices.py`'s own logic for the rest of its budget, so nothing is wasted once the catalogue stops growing.

```
python scripts/sync_all_cards.py --max-pages 1   # try one page
python scripts/sync_all_cards.py                 # continue where it left off
python scripts/sync_all_cards.py --list-only      # phase 1 only — skip phase 2 and phase 3
```

### `scripts/fetch_prices.py` — price refresh for the whole catalogue

For every card_id already in `cards` (not a watchlist — the full table, whatever `sync_all_cards.py` has discovered so far), calls `GET /v1/cards/:id` (1 credit) for TCGplayer prices and `GET /v1/cards/:id/listings/ebay` (1 credit/listing) for eBay sold comps, skipping anything already done today. This is also exactly what `sync_all_cards.py`'s phase 3 calls into — the two never double-spend credits on the same card in one run, since whichever runs first marks it done for today and the other skips it.

### `scripts/daily_sync.py` — the daily entry point

Two priorities compete for the same daily credit pool: catalog growth can spill over for days without hurting anything, but price refresh needs to run *every* day to be worth having. Just running `sync_all_cards.py` until it's out of credits — the naive approach — can burn the entire day's budget on discovery alone (or, once discovery's done, that's no longer even possible — see phase 3 above — but a fixed split still keeps each priority's share predictable).

`daily_sync.py` queries today's real remaining credit pool once, then splits it:

1. **Catalog growth** (priority 1, `sync_all_cards.run()`) gets first claim on a share of the pool — `--catalog-share`, default 50% (`PKMNPRICES_CATALOG_SHARE` env var) — and is hard-capped there even if it would otherwise keep going.
2. **Price history** (priority 2, `fetch_prices.run()`) gets the rest of the pool, plus whatever catalog growth didn't spend (e.g. because it finished a page run early, or had nothing left to discover and its own phase-3 price refresh already used part of its share).

Both tasks remain independently resumable (their own progress files / DB state), so this is just how *today's* budget gets divided between them — run it daily (the GitHub Actions workflow already does) and each day picks up exactly where the previous one left off.

```
python scripts/daily_sync.py                      # the daily cron entry point
python scripts/daily_sync.py --catalog-share 0.3    # give catalog growth 30% instead of 50%
python scripts/daily_sync.py --max-pages 20         # also cap catalog list pages this run
```

`fetch_prices.py` and `sync_all_cards.py` are still runnable directly (e.g. for a one-off manual fetch or to let one task use the *entire* day's budget) — each only self-limits to the real account-wide daily cap when run on its own.

## Setup

1. `pip install -r requirements.txt`
2. Copy `.env.example` to `.env` and add your key from the [pkmnprices dashboard](https://pkmnprices.com/dashboard).
3. Run a manual sync to test: `python scripts/daily_sync.py` (nothing to configure first — there's no watchlist to build; `cards` starts empty and grows from here)
4. For GitHub Actions: add `PKMNPRICES_API_KEY` as a repo secret (Settings → Secrets and variables → Actions), push this repo, and the workflow runs daily on its own (or trigger it manually from the Actions tab via "Run workflow").

## SQLite database (`data/pokemon.db`)

All data is saved in one SQLite file, `data/pokemon.db` (override with `PKMNPRICES_DB_PATH`). It's created on first run, needs no server or account, and is committed back to the repo by the daily workflow. Two tables, based on `data/pokemon.sql`:

- `cards`: one row per `card_id`, the latest known metadata (`name`, `card_number`, `rarity`, `card_type`, `set_id`, `tcg_player_id`). Upserted on every load, so a card's row is updated in place, never duplicated. A missing value in new data never overwrites a known one. It never holds grade or price fields — a card can have zero, one, or many `price_history` rows, graded or not.
- `price_history`: holds two different grains, told apart by `price_source`:
  - `tcgplayer`: one row per `(card_id, price_date, condition, variant)` — a daily aggregate `market_price`. Loading the same day again replaces the row instead of duplicating it.
  - `ebay`: one row per individual sold listing from `GET /cards/:id/listings/ebay` (Pro+). `external_id` (the listing's own id) is the real dedup key here, not the date — a card routinely has several eBay rows for the same day, and even several at the same grade, because each is a distinct sale (e.g. three separate PSA 10 sales at different prices). `grader`, `grade`, `grade_qualifier`, `sale_type`, and `listing_url` are only ever populated on these rows; they're always `NULL` on `tcgplayer` rows, which have no notion of grading.

  `price` is the API's `market_price` for `tcgplayer` rows or a sale price for `ebay` rows; `price_date` is `YYYY-MM-DD` — the snapshot date for `tcgplayer`, the listing's own `sold_at` for `ebay`.

An existing `pokemon.db` created before these grading columns existed is migrated in place the first time `db.Store()` runs against it (`ALTER TABLE ... ADD COLUMN`, then the unique index is rebuilt) — no manual steps needed.

If a run stops early (credit limit, error), the next run asks `price_history` which cards it already has for today and continues from there. eBay fetching resumes separately and incrementally: each card's `since` cursor is the newest `ingested_at` already stored for it, so a later run only pays credits for listings added after the last one it saw.

**How many grades does a card have?** It varies, and that's by design — the schema never assumes one grade per card:
- A `tcgplayer` row is always ungraded (raw market price), one row per condition/variant/day.
- An `ebay` row can be anything from zero (no graded sales yet) to dozens — different graders, different grades, or several sales at the identical grade. All of them key off the same `card_id`, so querying "every grade ever seen for this card" is just `SELECT DISTINCT grader, grade, grade_qualifier FROM price_history WHERE card_id = ? AND price_source = 'ebay'`.

**Importing an old CSV export** (e.g. from before CSVs were dropped, or a `master_prices.csv` someone hands you) — safe to re-run:
```
python scripts/backfill_db.py --dry-run   # parse + count only
python scripts/backfill_db.py --file path/to/master_prices.csv
python scripts/backfill_db.py --file path/to/a-snapshot.csv   # just one day
```

Example: latest price per card and variant

```sql
SELECT c.name, c.card_number, c.rarity, p.*
FROM price_history p
JOIN cards c USING (card_id)
WHERE p.price_date = (SELECT MAX(price_date) FROM price_history);
```

## Plan: coverage and limits

This repo now runs on **Pro** (20,000 credits/day). `common.py`'s `DAILY_CREDIT_LIMIT` still defaults to 500 (the Free-plan value) but self-corrects on the first request of the day from the live `x-credits-limit` response header, so no `.env` change is required — set `PKMNPRICES_DAILY_LIMIT=20000` only if you want the pre-flight check to know the real ceiling before that first call.

**Daily coverage:** `GET /v1/cards/:id` costs 1 credit/card; `GET /v1/cards/:id/listings/ebay` costs 1 credit per listing returned (up to 20/page). The catalogue (~93,000 cards) is far bigger than the 20,000/day budget, so `daily_sync.py` makes steady, resumable progress rather than finishing in one run — see `scripts/sync_all_cards.py` above.

**Discovery cost is separate and comes out of the same pool.** `GET /v1/cards` (catalogue discovery) also bills 1 credit per item returned, at up to 100 per page.

**What's now available on Pro** (previously `403` on Free):
- EUR / Cardmarket prices, and Japanese/German cards.
- Price history (`/cards/:id/prices/history`) — not wired up here, since `price_history` already builds this repo's own history day by day.
- `GET /cards/:id/listings/ebay` — eBay sold comps, including PSA/BGS/CGC graded sales. `fetch_prices.py` calls this for every card in `cards` alongside the TCGplayer fetch; see the SQLite section above for how graded rows are stored.
- Sealed products (`/sealed`, `/sealed/:id`, `/sealed/:id/listings/ebay`) — not wired up here; this tracker is card-only for now.

## Notes

- Rate limiting (60/min on Free) is handled with exponential backoff in `common.py`; at 1 request per card this is very unlikely to be hit before the credit limit is.
- Every run writes `data/pokemon.db`; the workflow commits it back to the repo. `data/master_prices.csv` and `data/snapshots/` are gitignored now — any copies on disk are leftovers from before CSVs were dropped and are safe to delete.
- `data/.progress-YYYY-MM-DD.json` is gitignored — it's a same-day resume aid, not meant to persist across days.
