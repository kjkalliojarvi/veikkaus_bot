# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Fetches Finnish/Swedish harness racing (ravit) data from the Veikkaus open REST API (`https://www.veikkaus.fi/api/toto-info/v1`) and persists it. Requires Python >= 3.14, managed with `uv`.

## Commands

```bash
uv sync                                    # install deps into .venv (make install)
uv run veikkaus fi_se                      # run the CLI: fetch FI + SE data, write data/*.json
uv run python veikkaus_bot/get_data_json.py  # same as fi_se, invoked directly (used by CI)
uv run pytest tests                        # run tests (make run-tests) — see caveat below
uv build                                   # build sdist + wheel (make dist)
```

The CLI entry point is `veikkaus` (defined in `[project.scripts]` → `veikkaus_bot.__main__:veikkaus`). Output JSON is written to a `data/` directory that must exist first (`mkdir data`).

**Caveat:** the Makefile/CI reference a `tests` directory that does not exist in the repo, so `pytest` currently collects nothing.

## Architecture

**`get_data_json.py` is the core.** Everything else consumes it.

- **API models** (Pydantic `BaseModel`): `Card` → `Race` → `Runner`, mirroring the API's nested resource hierarchy. Each model has `get_*` methods that lazily fetch its children via the module-level `_get_collection` / `_get_dict` helpers (e.g. `Card.get_races()`, `Race.get_runners()`). `Pool`/`Stat`/`Runner.prevStarts` model auxiliary data.
- **`VeikkausData(country)`** eagerly walks the full Card→Race→Runner tree on construction, flattening into `self.cards/races/runners`. `to_json()` reshapes these into positional tuple records (`races`, `runners`, `starts`, `stats`) via the `*_record()`/`*_records()` methods on the models — column order in those tuples must stay in sync with the DB `INSERT` statements. `save_to_file()` dumps to `data/{Y}-{M}-{D}-{country}.json`.
- **Runner statistics** ride along inside each `/race/{raceId}/runners` response as `runner.stats` (a dict keyed `currentYear`/`previousYear`/`total`, each a `Stat`). `Runner.stats_records()` parses these into one positional record per period (keyed by `runnerId` + `period`), collected under the `stats` key of `to_json()` and persisted to the `stat` table.
- **`get_cards()` is `@lru_cache`d** — it hits `/cards/today` once per process. All API fields not guaranteed present are `Optional` with defaults; the API sends Finnish-labeled harness data.
- The many `Optional` fields exist because the live API omits fields depending on race state; adding a required field risks validation failures on real responses.

**Persistence — two parallel, non-interchangeable implementations:**
- `database.py` — the working one. Raw `sqlite3` with a `db_ops` context manager and `Db` class. Tables: `race`, `runner`, `start`, `stat`. `store_file()` loads a saved JSON dump; `store_data()` consumes a `VeikkausData` directly. Table column order matches the `to_json()` tuples.
- `database2.py` — an incomplete SQLAlchemy 2.0 ORM rewrite (declarative `RunnerTable`/`StartTable`/`RaceTable`/`StatTable`). `create_db()` references undefined `CREATE_*` names and `DB_FILE` is hardcoded to an absolute path — not functional; treat as WIP unless actively completing it.

**Pipeline:** `__main__.veikkaus()` (argparse, single `fi_se` subcommand) → `get_data_json.fi_se()` fetches FI then SE, each wrapped in its own try/except so one country failing doesn't block the other → JSON files in `data/`.

## Automation

`.github/workflows/fi_se_load.yml` runs daily at 21:55 UTC (and on manual dispatch): `uv sync --frozen`, `mkdir data`, run `get_data_json.py`, upload `data/` as a build artifact. No database step in CI — it only captures the JSON snapshots.
