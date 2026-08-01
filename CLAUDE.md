# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Fetches Finnish/Swedish harness racing (ravit) data from the Veikkaus open REST API (`https://www.veikkaus.fi/api/toto-info/v1`) and persists it. Requires Python >= 3.14, managed with `uv`.

## Commands

```bash
uv sync                                    # install deps into .venv (make install)
uv run veikkaus fi_se                      # run the CLI: fetch FI + SE data, write data/*.json
uv run veikkaus load data/*.json --db veikkaus_data.duckdb  # load saved dump(s) into DuckDB
uv run python veikkaus_bot/get_data_json.py  # same as fi_se, invoked directly (used by CI)
uv run pytest tests                        # run tests (make run-tests) — see caveat below
uv build                                   # build sdist + wheel (make dist)
```

The CLI entry point is `veikkaus` (defined in `[project.scripts]` → `veikkaus_bot.__main__:veikkaus`). Output JSON is written to a `data/` directory that must exist first (`mkdir data`).

**Caveat:** the Makefile/CI reference a `tests` directory that does not exist in the repo, so `pytest` currently collects nothing.

## Architecture

**`get_data_json.py` is the core.** Everything else consumes it.

- **API models** (Pydantic `BaseModel`): `Card` → `Race` → `Runner`, mirroring the API's nested resource hierarchy. Each model has `get_*` methods that lazily fetch its children via the module-level `_get_collection` / `_get_dict` helpers (e.g. `Card.get_races()`, `Race.get_runners()`). `Pool`/`Stat`/`Runner.prevStarts` model auxiliary data.
- **`VeikkausData(country)`** eagerly walks the full Card→Race→Runner tree on construction, flattening into `self.cards/races/runners`. `to_json()` reshapes these into positional tuple records (`cards`, `races`, `runners`, `starts`, `stats`, `betpercentages`) via the `*_record()`/`*_records()` methods on the models — column order in those tuples must stay in sync with the DB `INSERT` statements. `save_to_file()` dumps to `data/{Y}-{M}-{D}-{country}.json`.
- **Runner statistics** ride along inside each `/race/{raceId}/runners` response as `runner.stats` (a dict keyed `currentYear`/`previousYear`/`total`, each a `Stat`). `Runner.stats_records()` parses these into one positional record per period (keyed by `runnerId` + `period`), collected under the `stats` key of `to_json()` and persisted to the `stat` table.
- **Bet percentages** also ride along as `runner.betPercentages` (a dict keyed by pool type, e.g. `{'KAK': {'percentage': 939}}`). `Runner.betpercentages_record()` flattens these into one record per pool type (keyed by `runnerId` + `poolType`), collected under the `betpercentages` key of `to_json()` and persisted to the `bet_percentage` table.
- **`get_cards()` is `@lru_cache`d** — it hits `/cards/today` once per process. All API fields not guaranteed present are `Optional` with defaults; the API sends Finnish-labeled harness data.
- The many `Optional` fields exist because the live API omits fields depending on race state; adding a required field risks validation failures on real responses.

**Persistence — two parallel, non-interchangeable implementations:**
- `database.py` — the working one. Raw `duckdb` with a `db_ops` context manager and `Db` class. Tables: `card`, `race`, `runner`, `start`, `stat`, `bet_percentage`. `store_file()` loads a saved JSON dump; `store_data()` consumes a `VeikkausData` directly. Table column order matches the `to_json()` tuples.
  - The `card` table holds one row per meeting (`Card.card_record()`), deliberately excluding the live progress fields (`currentRaceNumber`, `minutesToPost`, the `*Pools` blobs). `race` keeps only `trackAbbreviation`; `country`, `trackName`, and `trackNumber` now live solely on `card` and are reached via `race.cardId → card.cardId`. Dumps written before the table existed have no `cards` key (so `store_file()` reads it with `.get('cards', [])`) and carry 16-column race records that no longer match the 13-column `INSERT` — reload those from a fresh fetch.
  - DuckDB is stricter than SQLite, so three things must hold: numeric columns are `BIGINT` (ids like `raceId`/`priorStartId` and `startTime` exceed `INT32`, which DuckDB's `INTEGER` is); `winOdd` is `TEXT` because the API sends odds as strings; and every table upserts with `INSERT OR REPLACE` (the SQLite `ON CONFLICT` clause in a `PRIMARY KEY` definition is not supported). `_insert_many()` skips empty batches, which DuckDB's `executemany` rejects.
  - **No duplicates, two layers.** Every table declares a primary key, so the database itself rejects a second row for a key across calls, files, and runs. `_insert_many()` additionally collapses duplicates *within* one batch against the matching `*_KEY` column positions, so the winning row is chosen in Python instead of by DuckDB's per-statement conflict handling — last row for a key wins, matching `INSERT OR REPLACE`. Keep each `*_KEY` in sync with its table's `PRIMARY KEY` when columns move. `race` replaces rather than ignores so a re-load later in the day lands the finishing order (`toteResultString`), which the original SQLite `ON CONFLICT IGNORE` would have discarded.
- `database2.py` — an incomplete SQLAlchemy 2.0 ORM rewrite (declarative `RunnerTable`/`StartTable`/`RaceTable`/`StatTable`/`BetPercentageTable`). `create_db()` references undefined `CREATE_*` names and `DB_FILE` is hardcoded to an absolute path — not functional; treat as WIP unless actively completing it.

**Pipeline:** `__main__.veikkaus()` (argparse) exposes two subcommands. `fi_se` → `get_data_json.fi_se()` fetches FI then SE, each wrapped in its own try/except so one country failing doesn't block the other → JSON files in `data/`. `load <jsonfile>... [--db PATH]` → `database.load()` creates the tables and loads each dump via `Db.store_file()` (per-file try/except; default DB `veikkaus_data.duckdb`) — this is the only path that writes to DuckDB.

## Automation

`.github/workflows/fi_se_load.yml` runs daily at 21:55 UTC (and on manual dispatch): `uv sync --frozen`, `mkdir data`, run `get_data_json.py`, load the JSON dumps into `data/veikkaus_data.duckdb` via `veikkaus load data/*.json`, then upload `data/` (JSON snapshots + the DuckDB DB) as a build artifact.
