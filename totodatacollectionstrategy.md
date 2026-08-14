# Data Collection Strategy: Horse Past Performances from the Veikkaus Toto-Info API

**Target:** `https://www.veikkaus.fi/api/toto-info/v1`
**Purpose:** Build a past-performance dataset of Finnish trotting races for ML / predictive modeling
**Backfill depth:** 3–5 years · **Storage:** raw JSON archive + normalized SQLite
**Date:** 2026-08-09

---

## 1. Context, constraints and risks

Three things shape this strategy before any code is written.

**The API is race-centric, not horse-centric.** There is no known endpoint that returns "all past starts of horse X". The API serves race *cards* (a track's race day), the races on a card, and the runners in each race. A horse's past-performance history therefore has to be *constructed* by collecting every race over the backfill window and then grouping the runner rows by horse. This inverts the problem: instead of querying horses, you crawl the calendar exhaustively and build the horse timeline yourself. Everything below follows from this.

**The regulatory clock is ticking.** Finland's gambling reform moves horse betting (Toto) from the Veikkaus monopoly to a licensed, competitive market — the licensed market opens in 2027, and Suomen Hippos has been actively positioning for it. There is a real possibility that this API is restructured, migrated, or shut down when the transition happens. The practical consequence: **run the historical backfill early and archive raw responses**, so that even if the API disappears, the data survives and can be re-parsed at will.

**Access and terms.** The toto-info API is the unauthenticated public JSON backend of veikkaus.fi's own Toto pages — no API key or login is needed (unlike the wagering API documented in Veikkaus's [sport-games-robot](https://github.com/veikkaus/sport-games-robot) repo). However, veikkaus.fi's `robots.txt` disallows automated fetching of these paths, which is also why the endpoints could not be probed live while preparing this document. Treat this as a signal to (a) crawl politely and slowly, (b) keep the use personal/research-scoped, and (c) consider contacting Veikkaus or Suomen Hippos for sanctioned access if the project becomes serious. The authoritative long-term registry of Finnish race results is Hippos's [Heppa system](https://heppa.hippos.fi/heppa/app) — a useful cross-validation source and fallback (see §8).

## 2. Endpoint inventory (to be verified in Phase 0)

The endpoint set below is assembled from community usage of this API; because live probing was blocked from this environment, **Phase 0 of the plan is to verify each endpoint, its exact field names, and its historical reach before building anything on top of it.** Veikkaus also publishes an XML schema for the toto data model at [`/api/toto-info/v1/xml/toto.xsd`](https://www.veikkaus.fi/api/toto-info/v1/xml/toto.xsd), which is worth downloading in Phase 0 as a field-name reference.

| # | Endpoint (relative to `/api/toto-info/v1`) | Returns | Role in this project |
|---|---|---|---|
| 1 | `/cards/today` | Race cards for today | Incremental mode entry point |
| 2 | `/cards/date/{yyyy-MM-dd}` | Race cards for an arbitrary date | **Backfill entry point** — the crawl driver |
| 3 | `/card/{cardId}/races` | Races on a card (start time, distance, start type, prize money, race status) | One call per card |
| 4 | `/race/{raceId}/runners` | Runners with horse, driver/trainer, program number, post/track number, shoes, career stats — and after the race, result fields (placing, km time, gallop/disqualification codes) | **The core past-performance payload** |
| 5 | `/card/{cardId}/pools` and/or `/race/{raceId}/pools` | Betting pools attached to a card/race (Voittaja, Sija, Kaksari, Troikka, Toto64/65/75/86…) | Needed to reach odds |
| 6 | `/pool/{poolId}/odds` | Current/final odds for a pool | Win odds are a strong ML feature (market consensus) |
| 7 | `/pool/{poolId}/results` | Official pool results / payouts | Payout data for betting-side analysis |

Phase 0 checklist (a morning's work with `curl`/Python from your own machine):

1. Confirm each endpoint above responds and capture one full sample response per endpoint to fix the real field names.
2. **Probe historical reach:** binary-search `/cards/date/{d}` backwards (e.g. 2025 → 2023 → 2021…) to find the earliest date that still returns cards, and check whether old races still serve `runners` with result fields and whether old pools still serve `odds`/`results`. This determines whether "5 years" is actually available; if odds history is shallower than results history, the odds feature simply starts later.
3. Check what a *pre-race* runners payload contains vs. a *post-race* one (result fields appear after the race; race/card objects carry a status field that tells you which state you're looking at).
4. Note the card-level `country` field: Veikkaus also sells Toto on Swedish and other foreign cards. Decide the filter now — for *Finnish* races keep `country == "FI"` (verify exact value in Phase 0).
5. Record rate-limit behavior: send ~1 req/s for a few minutes and watch for 429s or throttling headers.

### 2b. Phase 0 findings (probed 2026-08-09)

Phase 0 was run against the live API from the project machine. It confirmed most of §2 and
contradicted three assumptions that the implementation had to be built around. The XSD is
archived at [`docs/toto.xsd`](docs/toto.xsd).

| # | Endpoint | Verdict |
|---|---|---|
| 1 | `/cards/today` | ✅ as described |
| 2 | `/cards/date/{yyyy-MM-dd}` | ✅ **reaches back to at least 2005** — 3–5 years is comfortably available. 2000 and earlier time out (504) |
| 3 | `/card/{cardId}/races` | ✅ as described |
| 4 | `/race/{raceId}/runners` | ⚠️ **carries no result fields for *this* race, ever** — but `runner.prevStarts` carries the horse's full past-performance line, uncapped. See below |
| 5 | `/card/{cardId}/pools`, `/race/{raceId}/pools` | ✅ as described |
| 6 | `/pool/{poolId}/odds` | ✅ per-runner `probable` (hundredths) + `amount`, with an `updated` timestamp |
| 7 | `/pool/{poolId}/results` | ✅ but superseded by the endpoint below |
| — | `/race/{raceId}/results` | ✅ **undocumented in §2 and better** — card block, `raceStatus`, `toteResult` and every pool's results in one call. Halves the result-side request count |

**1. Results do not live on the runner.** A runners payload contains entry data only —
horse, driver, trainer, shoes, post — before *and* after the race, in 2005 as in 2026. The
plan's §5 `start.placing` / `km_time_ms` / `result_code` / `prize_won` therefore cannot be
filled from endpoint 4. What is actually available per race:

- `/race/{raceId}/results` → `position` and `kmTime` for the runners a pool paid out on, plus
  the win pool's final `probable`. In practice **the first three only**.
- `race.toteResultString` (`"6-3-8"`) → the same top three, as start numbers.

`runner.prize` is *not* this race's purse — it is the horse's **career earnings** (it equals
`stats.total.winMoney` on every runner where both are present), so it carries no finishing
information at all. It is stored as `careerWinnings` to stop that misreading.

So *going forwards* from a race, the API gives only the paid places, and `archive.start` keeps
`placement`/`kmTime` NULL for the rest of the field rather than pretending otherwise.

**But the full finishing order is recoverable backwards, from `runner.prevStarts`.** Each
runners payload carries the horse's own career line — one entry per earlier start, with a
`result` code, km time, win odd, driver, track, distance and post. Those results are *not*
capped at third: observed placings run to 16th, alongside Finnish outcome codes (`kl`
koelähtö, `k` keskeytti, `hpl`/`hll`/`hlo4` hylätty) that no other endpoint exposes. This is
where §5's result-code parsing trap actually lives.

The consequence for the crawl is large: every horse that starts again reports its own placing
in every race it ran, so **crawling the calendar reconstructs full finishing orders
retrospectively** — indexed by horse rather than by race, and complete for any horse that
raced again. Cross-checked on 2026-08-06 Kuopio against the directly-crawled pool results: the
two sources agreed on every overlapping placing and the prev-start rows added placings (6th,
8th) that the results endpoint structurally cannot report. `prev_start.trackCode` uses the same
vocabulary as `card.trackAbbreviation`, so the two join on (meetDate, track, raceNumber).

Heppa (§8) therefore drops from "required for placings 4+" to a cross-validation source and a
fallback for horses that never started again.

Two traps inside the prev-start block, both verified rather than assumed:

- **`meetDate` is midnight Finnish time expressed in UTC**, so its UTC date is a day early on
  *every* row (1732/1732 sampled). `shortMeetDate` (`dd.mm.yy`) is the meet date that lines up
  with `card.meetDate`, and is what `prev_start.meetDate` is parsed from.
- **`winOdd` is a digit string in hundredths** (`'1002'` = 10.02), matching `probable`
  elsewhere, not a decimal.

**2. Historical payloads are thinner than live ones.** Cards fetched by date omit the
live-progress and EPG blocks (`currentRaceStatus`, `epgStartTime`/`epgStopTime`/`epgChannel`,
and before ~2010 also `currentRaceStartTime` and `firstRaceStart`); races before ~2010 omit
`startTime` (the card's `meetDate` still dates them); and historical runners carry neither
`stats` nor `prevStarts`. The Pydantic models in `get_data_json.py` were relaxed accordingly —
this is exactly the §10 "silent schema drift" risk, in the time direction.

**3. Volume is lower than estimated.** Sampled weeks show ~9 FI cards/week ≈ **470/year**, not
600 — so a 5-year backfill is ~2,400 cards and ~24,000 races, roughly 50,000 requests without
odds. At the 2 s base delay used by the crawler that is ~28 hours. Crawl it off-peak:
Finnish racing runs roughly 12:00-22:00 local, so the small hours put the load where the
API is not already serving live betting traffic. The endpoints sit behind a CDN with a
10-second cache, which a single-pass crawl misses on every request, so the pace is the only
thing limiting what the origin carries.

Also confirmed: `country == "FI"` is the right filter (a single day's cards span FI/SE/NO/FR/CA/AU),
and km times use the notation `24,9a` — leading minute dropped, trailing letters carrying
start-type and equipment markers.

## 3. Volume estimate and crawl budget

Finland runs roughly 550–650 race days per year across ~20 tracks, typically 9–11 races per card and ~10–14 runners per race. For a 5-year backfill that is on the order of:

| Object | Per year | 5 years |
|---|---|---|
| Cards (FI) | ~600 | ~3,000 |
| Races | ~5,500–6,500 | ~30,000 |
| Runner rows (starts) | ~70,000 | ~350,000 |
| HTTP requests (cards + races + runners) | ~7,500 | ~35,000 |
| + pools & odds requests | roughly doubles it | ~70,000–90,000 total |

At a polite 1 request/second, the whole 5-year backfill is **roughly a day of wall-clock crawling** — small enough that there is no need for parallelism, which keeps you well inside polite-crawler territory. Raw JSON for the full backfill will be a few GB uncompressed, well under 1 GB gzipped; the SQLite file will be a few hundred MB.

## 4. Architecture: raw zone first, database second

The single most important design decision: **separate fetching from parsing.** Every successful HTTP response is written verbatim (gzipped) to a raw archive before anything is parsed. Parsing bugs are then always recoverable without re-crawling, and if Veikkaus retires the API in 2027 you still hold the source data.

```
raw/
  2024-03-15/
    cards.json.gz                      ← /cards/date/2024-03-15
    card_123456/
      races.json.gz                    ← /card/123456/races
      race_7890123_runners.json.gz     ← /race/7890123/runners
      pools.json.gz
      pool_555_odds.json.gz
      pool_555_results.json.gz
manifest.sqlite                        ← fetch ledger (see §6)
toto.sqlite                            ← normalized DB (see §5)
```

A separate parse step reads the raw zone and upserts into SQLite. The parser is idempotent: re-running it over the same raw files produces the same database.

## 5. SQLite schema (normalized core + ML view)

```sql
CREATE TABLE card (
  card_id      INTEGER PRIMARY KEY,      -- API cardId
  date         TEXT NOT NULL,            -- yyyy-mm-dd
  track_name   TEXT NOT NULL,
  country      TEXT NOT NULL,            -- keep FI filter decision explicit
  first_start  TEXT,
  raw_json     TEXT                      -- optional: card object as-is
);

CREATE TABLE race (
  race_id      INTEGER PRIMARY KEY,
  card_id      INTEGER NOT NULL REFERENCES card(card_id),
  number       INTEGER NOT NULL,         -- race # on the card
  start_time   TEXT,
  distance     INTEGER,                  -- metres
  start_type   TEXT,                     -- volt/auto (tasoitusajo vs autolähtö)
  monte        INTEGER DEFAULT 0,
  prize_money  INTEGER,
  breed        TEXT,                     -- lämminverinen / suomenhevonen etc.
  status       TEXT                      -- upcoming / official / cancelled
);

-- One row per (horse, race) = one START = one past-performance line.
CREATE TABLE start (
  race_id        INTEGER NOT NULL REFERENCES race(race_id),
  program_number INTEGER NOT NULL,
  horse_key      TEXT NOT NULL REFERENCES horse(horse_key),
  driver_name    TEXT,
  trainer_name   TEXT,
  post_position  INTEGER,                -- track/lane number
  handicap_dist  INTEGER,                -- actual start distance (volt handicaps)
  front_shoes    INTEGER,                -- shoeing info if present
  rear_shoes     INTEGER,
  scratched      INTEGER DEFAULT 0,      -- poisjäänyt
  -- result fields (NULL until race is official):
  placing        INTEGER,                -- NULL for dq/dnf
  result_code    TEXT,                   -- raw code: hylätty/keskeytti/hl/kl...
  km_time_ms     INTEGER,                -- parsed km time in ms
  gallop         INTEGER DEFAULT 0,      -- broke stride ('a'/gallop markers)
  win_odds_final REAL,                   -- final win odds if captured
  prize_won      INTEGER,
  PRIMARY KEY (race_id, program_number)
);

CREATE TABLE horse (
  horse_key   TEXT PRIMARY KEY,          -- see identity note below
  name        TEXT NOT NULL,
  register_no TEXT,                      -- if API exposes it
  birth_year  INTEGER,
  sex         TEXT,
  breed       TEXT,
  sire        TEXT, dam TEXT             -- if available
);

CREATE TABLE odds_snapshot (             -- incremental mode only
  pool_id     INTEGER, race_id INTEGER, program_number INTEGER,
  captured_at TEXT, odds REAL, pool_type TEXT,
  PRIMARY KEY (pool_id, program_number, captured_at)
);

CREATE INDEX idx_start_horse ON start(horse_key);
CREATE INDEX idx_race_card   ON race(card_id);
```

With this shape, "past performances of horse X before date D" is one indexed query over `start ⋈ race ⋈ card`, which is exactly the join your ML feature pipeline needs.

**Horse identity is the hard problem.** Verify in Phase 0 whether the runners payload carries a stable horse identifier (registration number) or only the name. Names alone are *nearly* unique in Finnish trotting but not guaranteed across breeds and imports. If no stable ID is exposed, use `horse_key = normalize(name) + birth_year` (if birth year is present) and keep a manual-review table for collisions. Hippos's Heppa registry is the authority if you ever need to resolve a conflict. The same applies in milder form to drivers and trainers (initials, name variants).

### 5b. As implemented

The schema above is DDL-sketch, not the literal build. Four deliberate deviations:

- **DuckDB, not SQLite.** The repository's storage backend is DuckDB, so the archive lives in
  the same `veikkaus_data.duckdb` file under its own `archive` schema. That keeps the plan's
  table names (`card`, `race`, `start`, `horse`, `odds_snapshot`) intact while leaving the
  `main` tables that the daily `fi_se` dump writes untouched — `main.start` and
  `archive.start` are different things and now say so. Ids and epoch-millisecond values are
  `BIGINT` (DuckDB's `INTEGER` is 32-bit), and every table upserts with `INSERT OR REPLACE`.
- **camelCase column names**, mirroring the API and the existing `main` tables.
- **`start.horseKey` is `normalize(name)|birthYear`**, where normalize folds case and spacing
  and drops the `*` marker but *keeps* a country tag such as `(SE)` — that tag is precisely
  what separates an import from a same-named domestic horse.
- **A second source has its own tables.** `heppa_event`, `heppa_race` and `heppa_start` hold
  the Heppa registry verbatim (§8b), keyed on (meetDate, trackCode[, raceNumber[,
  programNumber]]) because Heppa exposes no `cardId` or `raceId`. `start` gained `prizeWon`,
  `disqualifiedCode`, `gallop` and `resultSource`, and `horse` gained `heppaHorseId`; all five
  are written NULL by the parser and derived afterwards, like `startInterval` before them.

**Parsing traps to handle deliberately** (unit-test these): Finnish km-time notation (`14,5a` = 1:14.5 with auto start; `a` suffix and gallop marks), result codes for disqualified/broke/did-not-finish starts (hylätty, keskeytti, poisjäänyt), volt-start distance handicaps (a 2140 m runner in a "2100 m" race), and cancelled races/cards.

## 6. Crawler design

**Manifest-driven, resumable.** A `manifest` table records every planned fetch: `(endpoint_type, entity_id, url, status, http_code, fetched_at, raw_path)`. The crawler's loop is simply "take the next `pending` row, fetch, store raw, mark `done`". Kill it anytime; restart resumes exactly where it stopped. Backfill enumeration is: insert one `cards/date` row per date in the window → parsing each cards response inserts `races` rows per FI card → parsing races inserts `runners` (+ pools → odds/results) rows.

**Politeness.** Single-threaded, fixed delay ≥ 1 s between requests (add ±30 % jitter), a descriptive `User-Agent` including a contact address, `Accept-Encoding: gzip`, and exponential backoff (30 s → 2 min → 10 min) on 429/5xx/timeouts with a circuit breaker that pauses the run after ~5 consecutive failures. Never hammer: this is an unofficial use of a public backend, and the whole 5-year crawl fits in a day even at this pace.

**Ordering.** Backfill from **most recent backwards**. Recent seasons matter most for model training, so if historical reach turns out shallower than hoped, or access breaks mid-crawl, you have already banked the most valuable data.

## 7. Ongoing incremental collection (after backfill)

A small daily job keeps the dataset current and — critically for ML — captures what the backfill cannot: **pre-race market data and pre-race entry state.**

The daily cycle: in the morning, fetch `/cards/today` (and tomorrow's date) and store the entry lists — this is the "as-known-before-the-race" snapshot, including scratches as they appear. If you want odds as a feature, poll each race's win pool a few times in the final ~30 minutes before its start (e.g. T-30, T-10, T-2) into `odds_snapshot`; historical endpoints will most likely give you only final odds, so forward collection is the only way to get odds trajectories. Then, 1–2 hours after each card finishes, re-fetch the runners for every race to pick up official results, and re-fetch once more the next morning to catch late corrections/protests. Schedule it with cron/systemd on any always-on machine; the job is a few hundred requests per day at most.

## 8. Validation and cross-checking

Trust but verify, per phase. After backfill, run structural checks: every card has races, every non-cancelled official race has runners with results, placings within a race are consistent (unique, gap-free apart from dq/dnf), km times fall in plausible ranges (1:08–1:50), and per-year race counts match the ~5,500–6,500 expectation. Finally, sanity-check the ML view: pick a well-known horse, pull its reconstructed career line from `start`, and compare with its Heppa career page — this specifically validates the horse-identity resolution of §5.

### 8b. Heppa, as implemented

Heppa was planned here as a manual spot-check and a fallback. It is now a **crawled source in its
own right** — `veikkaus heppa` — because the gap it fills turned out to be far larger than §2b's
optimism about `prev_start` allowed for. Of 268,864 starts at Finnish meetings in the 2021→ archive,
**195,690 had no placing at all**: `prev_start` only recovers a start for a horse that raced again
*while the card was still current*, so the backfilled years recover essentially nothing (13,271 rows
against 26,348 races, 84 % of them 2026).

The registry publishes the whole field for every Finnish meeting back to at least 2000, through the
Mobiiliheppa backend at `https://heppa.hippos.fi/heppa2_backend`: `/race/results/{from}/{to}/` lists
the meetings in a date range, `/race/{date}/{trackCode}/races` their races, and
`/race/{date}/{trackCode}/start/{raceNo}` the field. Unauthenticated, and — unlike the Veikkaus
paths — not disallowed by `robots.txt`, which names only `/heppa/racing`, `/heppa/horse` and
`/heppa/person`. Roughly 28,000 requests for the 2021→ window, ~16 h at the 2 s delay.

Three things arrive that the Veikkaus API has no equivalent of anywhere: **this race's prize money
per horse** (`runner.prize` is career earnings, as §2b established), a **disqualification code for
every start** rather than only for horses that were re-reported, and **stable registry ids** for
horses, drivers and trainers — which settles the §5 horse-identity problem outright, since
`horse_key()` is a name-and-birth-year guess and `horseId` is authoritative. Track conditions and
temperature come along too.

The cross-check §8 asked for is now a query rather than 20 manual lookups — `veikkaus crosscheck`.
Every start where both sources have a placing is a comparison, and `archive.start.resultSource`
records which source supplied each one. The merge coalesces — Veikkaus never gets overwritten — so
a disagreement is visible rather than resolved silently.

Run on the first crawled day (2026-08-08, four meetings, 369 registry starts, 224 of them joining
to a Veikkaus start), the two sources agreed on **every** overlapping value: 60/60 placings,
187/187 km times in milliseconds, 224/224 auto-start flags, 209/209 final win odds, 224/224
scratchings against `absent`. No horse-identity collision in either direction. Savonlinna race 1
went from 3 placings to 11. Two things the comparison exposed that are worth knowing:

- **Horse names differ on 50 of 224 rows, and that is correct.** Veikkaus appends the country tag
  (`Kapplans Orlando (SE)`); Heppa keeps it in `horseRegistrationCountry` and leaves the name clean.
  This is exactly why the bridge between the sources is positional and never name-based.
- **Km-time *strings* differ on 30 of 60**, because Veikkaus carries the start-type and equipment
  markers (`31,5x`, `m18,5a`) and Heppa's short form is bare. The parsed `kmTimeMs` agrees
  everywhere. After the merge `archive.start.kmTime` is therefore not uniform; analyse on
  `kmTimeMs` and `autoStart`.

That 224/224 auto-start agreement is itself a result: it independently confirms that the `a` prefix
on Heppa's per-horse `distanceCode` means the same thing as `race.startType = 'CAR_START'`.

**Heppa does not replace the Veikkaus crawl.** It has no odds history (269,010 `odds_snapshot`
rows) and no betting percentages (414,457 rows), and its horse-level figures are as-of-now rather
than as-of-race-day: `horsePriceSum` includes the race being reported, where `careerWinnings` is
the pre-race number. Both crawls stay, and the 2027 fallback argument is now stronger, not weaker —
the results half of the dataset no longer depends on Veikkaus at all.

## 9. Phased plan

| Phase | Work | Effort | Status |
|---|---|---|---|
| **0 — Probe** | Verify endpoints & field names (§2 checklist), download `toto.xsd`, determine historical reach and rate-limit behavior, fix the FI-card filter | ½–1 day | **done** — §2b |
| **1 — Build** | Manifest ledger, fetcher with politeness/backoff, raw-zone writer, parsers + schema, unit tests for km-time/result-code parsing | 2–3 days | **done** — `veikkaus backfill` / `parse` / `status` |
| **2 — Backfill** | Run newest→oldest over 3–5 years; monitor; then run §8 structural checks | ~1–2 days wall-clock | **done** — 2021-01-01 → 2026-08, 2,701 cards / 26,348 races |
| **2b — Heppa** | Crawl the registry for the finishing order the Veikkaus API structurally cannot publish (§8b); merge into `start`, resolve horse identity | ~16 h wall-clock | **done** — `veikkaus heppa` |
| **3 — Incremental** | Daily cron: entries + results re-fetch; optional odds snapshots near post time | ½ day to set up | not started |
| **4 — Features** | Build the ML feature layer on top of `start` (last-5 form, km-time trends, driver/trainer stats, class moves, distance/start-type splits) — strictly time-aware (only data available before each race) | ongoing | not started |

One §5 addition made during Phase 1: `archive.prev_start` holds the per-horse career lines
lifted out of `runner.prevStarts`, keyed on (horse, meet date, race number) so that a start
re-reported on every later race of that horse collapses onto one row. Note that the API's
`priorStartId` is *not* an identity for a start — a horse's whole career line is renumbered on
every report — so it cannot serve as the key. Race number is part of it because heats and
finals put a horse in two races on one card. It is the only
table carrying a finishing position for the whole field, which makes it the backbone of the
past-performance history — `archive.start` describes the race, `archive.prev_start` describes
the careers.

`prev_start.startInterval` carries the days since the horse's previous known start — layoff
length is a strong form signal. It is computed over the whole accumulated table after loading
rather than per record, because a reported prevStarts list is a truncated window and the gap in
front of its oldest entry cannot be read from that list alone. A horse's earliest start in the
archive keeps an epoch sentinel (days since 1970-01-01, so ~19,600–20,700 depending on the meet
date — filter with `> 10000`, not by equality).

The block carries **no trainer**, only `driver`/`driverFullName`. `prev_start.coachName` is
therefore back-filled from the archive — the `archive.start` row for that same race, which
recorded the trainer as of that day — and left NULL where the crawl has not covered the race.
It is never taken from the runner reporting the line: that is the trainer at a *later* race,
which silently rewrites a horse's history whenever it changes yards. Trainer coverage on the
history is thus a function of backfill depth, and grows as the crawl does.

## 10. Key risks summary

| Risk | Mitigation |
|---|---|
| API restructured/retired in 2027 licensing transition | Backfill early; archive raw JSON; **Heppa now crawled as a second source (§8b)**, so the results half no longer depends on Veikkaus |
| Historical odds/pools not available far back | Accept results-only history for old years; collect odds forward from now |
| No stable horse ID in payloads | name+birth-year key, **resolved against Heppa's `horseId` (§8b)**, which makes the collision review a query |
| robots.txt / ToS friction | Slow single-threaded crawl, identifying User-Agent, personal-research scope, consider asking for sanctioned access |
| Silent schema drift in the API | Raw zone + manifest lets you re-parse; log unknown fields loudly |

---

*Endpoint paths and field names in §2/§5 are compiled from community usage of this API and must be confirmed in Phase 0 — live verification was not possible while writing this document because veikkaus.fi disallows automated fetching from this environment.*
