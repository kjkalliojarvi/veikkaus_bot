# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Builds a past-performance dataset of Finnish harness racing (ravit) by crawling the Veikkaus open REST API (`https://www.veikkaus.fi/api/toto-info/v1`) into a raw JSON archive and parsing that into DuckDB. Requires Python >= 3.14, managed with `uv`.

`totodatacollectionstrategy.md` is the spec. Read §2b (the Phase 0 findings) before changing anything that touches the API — it records where the live API contradicts the rest of that plan.

## Commands

```bash
uv sync                                    # install deps into .venv (make install)
uv run veikkaus backfill --from 2021-01-01 # crawl the race calendar into data/raw/ (resumable)
uv run veikkaus parse                      # raw archive -> archive.* tables
uv run veikkaus status                     # crawl manifest progress
uv run pytest tests                        # run tests (make run-tests)
uv build                                   # build sdist + wheel (make dist)
```

The CLI entry point is `veikkaus` (defined in `[project.scripts]` → `veikkaus_bot.__main__:veikkaus`).

## Architecture

One pipeline, in two halves that never run together: **fetching and parsing are deliberately separate**, so a parsing bug never costs a re-crawl and the raw responses survive even if the API is retired in the 2027 licensing transition (§4, §10 of the strategy).

- `models.py` — Pydantic models for the API resources: `Card` → `Race` → `Runner`, plus `Stat` for one period of a runner's `stats` block. Validation only; nothing here fetches.
- `fetcher.py` — `Fetcher` does one request at a time: base delay 1.5 s ±30 % jitter, retries 429/5xx/timeouts through 30 s → 2 min → 10 min, and raises `CircuitOpen` after 5 consecutive failures. 400/404 mean "nothing there", not an error. `store_raw()`/`read_raw()` are the gzipped raw zone, laid out as `data/raw/{meetDate}/card_{cardId}/…`. `VEIKKAUS_CONTACT` sets the contact address in the `User-Agent`.
- `crawler.py` — `Manifest` is the fetch ledger (`archive.manifest`, PK `(endpointType, entityId)`); `enqueue` uses `INSERT OR IGNORE` so re-running a window never re-fetches finished work. `next_pending()` orders by `meetDate DESC, stage ASC` — newest season first (§6), cards before races before per-race payloads. `expand()` turns a fetched response into its children and is the only place the crawl graph is defined. Kill and rerun to resume.
- `archive_db.py` — the `archive` schema, its upserts, and the two shared DuckDB helpers (`db_ops`, `_insert_many`). Tables: `card`, `race`, `horse`, `start`, `prev_start`, `stat`, `bet_percentage`, `odds_snapshot`, `manifest`.
- `parse.py` — walks the manifest's `done` rows, reads the raw files, upserts. Idempotent. Holds the Finnish km-time parser and `horse_key()`.

**`start` vs `prev_start` — the distinction that matters most here.** `archive.start` is one row per (race, horse) built from the runners payload: it describes *the race*, and its `placement`/`kmTime` stop at the paid places because that is all the results endpoint publishes. `archive.prev_start` is built from `runner.prevStarts` — the horse's own career line riding along inside the same payload — and it describes *the careers*: a finishing position for the whole field (observed to 16th), km time, win odd, driver, track, distance and post, per earlier start. It carries `horseKey` rather than the reporting `runnerId`, and is keyed on **(horseKey, meetDate, raceNumber)** — the natural identity of a start.

**Do not key it on `priorStartId`.** That id is assigned per reporting payload, not per start: when a horse races again its whole prevStarts list comes back renumbered as a fresh contiguous block (observed as a constant +1,271,073 offset between two reports of one career). Keying on it stores the same career once per race the horse subsequently ran, and feeds `startInterval` a zero-day gap between each copy. This was shipped and then fixed; the regression tests in `tests/test_archive_db.py` pin it. `raceNumber` belongs in the key because heats and finals put a horse in two races on one card — those are real rows with a genuine zero-day gap.

`prev_start.trackCode` shares a vocabulary with `card.trackAbbreviation`, so the two join on (meetDate, track, raceNumber) — verified against directly-crawled results, which agreed on every overlapping placing.

**`startInterval` is filled in after loading, not per record** (`ArchiveDb.recompute_start_intervals()`, called at the end of `parse_all()`). A reported prevStarts list is a truncated ~8-entry window, so the gap in front of its oldest entry is unknowable from that list alone; and because the window slides forward, whichever report landed last would otherwise decide the stored value. Recomputing over the accumulated table with a window function per `horseKey` makes it deterministic, and re-derives correctly as more dates are crawled in. A horse's earliest start in the archive has no predecessor and keeps the old pipeline's epoch sentinel — **days since 1970-01-01, so it varies with the meet date (~19,600–20,700 observed), not a fixed number.** Filter it in analysis with a threshold well above any plausible gap (`startInterval > 10000`), never by equality. Exactly one row per horse carries it.

**DuckDB rules.** Every id and epoch-millisecond value is `BIGINT` (`INTEGER` is 32-bit and `raceId`/`startTime` overflow it). Every table upserts with `INSERT OR REPLACE` — the SQLite `ON CONFLICT` clause inside a `PRIMARY KEY` definition is not supported. `_insert_many()` skips empty batches, which DuckDB's `executemany` rejects, and collapses duplicates *within* a batch against the matching `*_KEY` column positions, so the winning row is chosen in Python rather than by DuckDB's per-statement conflict handling — last row for a key wins, matching the SQL. Keep each `*_KEY` in sync with its table's `PRIMARY KEY` when columns move. Column order in the `*_record()` builders in `parse.py` must stay in sync with the matching `INSERT_*` in `archive_db.py`.

**API facts the schema is shaped around**, all verified in Phase 0 and all easy to get wrong:

- **A runners payload never carries result fields for its own race**, in any era. Placings come from `race.toteResultString` (which names the placed start numbers even when a pool paid fewer than three) with `/race/{raceId}/results` adding km time and the final win odd for the paid places only — so `archive.start.placement` is NULL past third. The full order is recovered from `prev_start` instead, retrospectively.
- **Prev-start `meetDate` is midnight Finnish time expressed in UTC**, so its UTC date is a day early on every row. Parse the meet date from `shortMeetDate` (`dd.mm.yy`) — that is what `parse_meet_date()` does, and it is what lines up with `card.meetDate`. Prev-start `winOdd` is a digit string in hundredths (`'1002'` = 10.02).
- **`prev_start.result` is a raw code**, kept verbatim: numeric placings alongside Finnish outcome codes (`kl` koelähtö, `k` keskeytti, `hpl`/`hll`/`hlo4` hylätty). `parse_result()` fills `placement` only for the numeric ones. Qualifying starts (`kl`, usually raceNumber > 20) are kept rather than filtered — `result` identifies them.
- **A prev-start entry carries no trainer.** It has `driver`/`driverFullName` and nothing else about the connections. `prev_start.coachName` is therefore *sourced from the archive*, not from the payload: `RECOMPUTE_PREV_START_COACH` fills it from the `archive.start` row for that same race, matched on (horseKey, meetDate, raceNumber), which recorded the trainer as of that day. Rows outside the crawl window stay NULL — there is no honest source for them. Never stamp the reporting runner's `coachName` onto these rows: that is the trainer at a *later* race, and with a start re-reported by every subsequent race it would be whichever report happened to land last. The old `main.start` table did exactly that. Track is deliberately not in the join — `prev_start.trackCode` and `card.trackAbbreviation` are not verified to share a vocabulary at every track, and a horse cannot be in two places on one day.
- **`runner.prize` is career earnings, not the race purse** (it equals `stats.total.winMoney`), hence the column name `careerWinnings`.
- **`placing` is a reserved word** in DuckDB's Postgres-derived parser, so the column is `placement`.
- **Km times drop the leading minute** — `'24,9a'` is 1.24,9 from an auto start, 84900 ms. Monté times take a leading `m` (`'m19,6'`); slower times spell the minute out (`'2.05,0'`).
- **`autoStart` comes from `race.startType`, not from the km-time suffix.** Start type is a property of the race, so it is known for every runner — including scratched horses and everyone outside the paid places, none of whom have a km time to read an `a` off. `recompute_auto_starts()` sets it from the race after loading; the suffix survives as a per-horse cross-check (the two agree exactly wherever both exist). Only `CAR_START`/`VOLT_START` have ever been observed and anything else is left NULL. `prev_start` has a `raceStartType` field but the API sends `UNKNOWN` in every entry, so there the suffix is the only per-row signal and the crawled race fills what it can.
- **Historical payloads are thinner than live ones.** `Card.currentRaceStatus`/`currentRaceStartTime`/`firstRaceStart`/`epg*`, `Race.startTime`, `Runner.stats`, and the breeding/`birthDate` fields are Optional because they are *absent* on old cards, not merely sometimes-missing. Don't tighten them back up. `stats` and `prevStarts` ride along only while a card is current, so `archive.stat` and `archive.prev_start` are empty for backfilled years — the career history accumulates from crawling *recent* dates, not old ones.

**Horse identity.** `horse_key()` = `normalize(name)|birthYear`, where normalize folds case and spacing and drops the `*` marker but *keeps* a country tag such as `(SE)` — that tag is what separates an import from a same-named domestic horse. The API exposes no registration number; Hippos's Heppa registry is the authority when a collision has to be resolved.

**Pipeline:** `__main__.veikkaus()` (argparse) exposes three subcommands. `backfill --from D [--to D] [--odds] [--limit N] [--retry-failed]` → `crawler.backfill()` crawls into the raw zone; `parse` → `parse.parse_all()` loads the raw zone into `archive.*`; `status` prints manifest counts.

**Not part of the pipeline:** `odds_json.py` and `odds_xml.py` are standalone scratch scripts, imported by nothing.

## Automation

None. The repository has no CI workflow — the previous daily job collected the `fi_se` dump, which has been removed. Phase 3 of the strategy (daily entries + results re-fetch, optional odds snapshots near post time) is the intended replacement and is not built.
