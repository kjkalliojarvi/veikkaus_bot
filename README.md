# veikkaus_bot

Builds a past-performance dataset of Finnish harness racing (ravit) from two sources — the [Veikkaus](https://www.veikkaus.fi) open Toto REST API for entries and the betting market, and Suomen Hippos's [Heppa](https://heppa.hippos.fi/mobiili/) registry for the official results — by crawling both into a raw JSON archive and parsing that into DuckDB. The approach and its constraints are set out in [totodatacollectionstrategy.md](totodatacollectionstrategy.md).

## Requirements

- Python >= 3.14
- [uv](https://docs.astral.sh/uv/)

## Install

```bash
uv sync
```

## Usage

```bash
uv run veikkaus backfill --from 2021-01-01 --create-db   # first run only
uv run veikkaus backfill --from 2021-01-01  # crawl Veikkaus into data/raw/
uv run veikkaus heppa --from 2021-01-01     # crawl Heppa meetings into data/raw/
uv run veikkaus heppa-horses                # one registry record per horse
uv run veikkaus status                      # how far both got
uv run veikkaus parse                       # raw/ -> archive.* tables
uv run veikkaus crosscheck                  # do the two sources agree?
uv run veikkaus horse [name]                # browse one horse's Heppa starts (TUI)
                                            #   click a stats row for the starts behind it
```

Fetching and parsing are deliberately separate. `backfill` only writes gzipped raw responses into `data/raw/` and records every fetch in a manifest table, so it is resumable — kill it and rerun the same command. It crawls newest date first, one request at a time with a ≥1 s delay and exponential backoff. `parse` then reads the raw archive into the `archive.*` tables and can be re-run at any time without re-crawling.

Every command refuses a `--db` path that is not already there, because DuckDB creates a database for whatever path it is handed: a mistyped `--db` would otherwise mint an empty archive and then report zero of everything, which looks exactly like the real one having gone missing. `backfill`, `heppa` and `parse` take `--create-db` for the genuine first run; `status`, `crosscheck` and `heppa-horses` only ever read an archive, so they have no such flag.

`--odds` additionally crawls win-pool odds for every race (roughly doubles the request count); without it, final win odds are only known for the paid places.

A five-year backfill is on the order of 50,000 requests — about 28 hours at the default 2 s delay. Note that veikkaus.fi's `robots.txt` disallows automated fetching of these paths: keep the crawl slow and single-threaded, and set `VEIKKAUS_CONTACT` to a contact address so the `User-Agent` identifies you. If this becomes more than personal research, ask Veikkaus or Suomen Hippos for sanctioned access.

### The Heppa half

`heppa` crawls Suomen Hippos's [Heppa](https://heppa.hippos.fi/mobiili/) registry, which is where the finishing order actually comes from. The Veikkaus API publishes finishing detail only for the runners a pool paid out on — the first three — so roughly **three quarters of the starts in the archive have no placing from that source at all**, and `prev_start` only recovers them for horses that raced again while the card was still current. Heppa publishes the whole field, for every Finnish meeting, back to at least 2000. It also carries three things Veikkaus has no equivalent of: this race's prize money per horse, a disqualification code for every start, and stable registry ids for horses, drivers and trainers.

It runs on the same manifest and the same raw zone as `backfill`, is resumable the same way, and shares `--limit`/`--retry-failed`/`--delay`. The two never drain each other's queue.

```bash
uv run veikkaus heppa --from 2021-01-01           # ~28,000 requests, ~16 h
uv run veikkaus heppa --from 2026-08-08 --to 2026-08-08   # one day, to try it
```

The scope is every Finnish meeting, including the local (`PAIKALLISRAVI`) and pony (`PONI`) racing that the Veikkaus API never reports — real starts in a horse's career that would otherwise be invisible. `heppa.hippos.fi/robots.txt` disallows `/heppa/racing`, `/heppa/horse` and `/heppa/person`, and says nothing about the `/heppa2_backend` endpoints this reads; the crawl is single-threaded at the same delay regardless.

The two sources are complementary, not interchangeable. Heppa has no odds history and no betting percentages, and its horse-level figures are as-of-now rather than as-of-race-day — `horsePriceSum` is career earnings *including* the race being reported, where Veikkaus's `careerWinnings` is the pre-race figure. Keep crawling both.

### The horse registry

`heppa-horses` fetches one `/horse/{id}` record per horse the meetings crawl turned up — **14,050 requests, ~7.8 h**. It is driven by the archive rather than by a date window, so run it after `heppa` and `parse`; re-running it later costs only the horses that are new.

It brings what neither the Veikkaus API nor the results endpoints carry: `registerNo` and **`ueln`** (international, so the join key to any other registry), an *exact* `birthDate` where `archive.horse` has only a year, breeder, colour and breed, and `sireId`/`damId` — a pedigree graph with stable ids instead of the name strings `archive.horse` holds.

**`birthCountry` is origin; `registrationCountry` is where the horse races**, and they differ for every import. That distinction is the one `heppa_start.horseRegistrationCountry` cannot make.

It does **not** improve horse-identity resolution, despite appearances: 5,232 of the 5,235 horses without a registry id race only on the Swedish simulcast and combination-pool cards, and Heppa is the Finnish registry — there is no record of them to fetch. `/horse/{id}/stats` is deliberately not crawled either; it is as-of-now and would leak results into any as-of-race-day feature.

### Cross-checking

`crosscheck` is the validation of §8 of the strategy, as a query rather than 20 manual lookups: it reports whether the two sources agree wherever both have an answer, how much of the field is still unplaced and why, which horses fail identity resolution, and how much of the archive the Heppa crawl has yet to reach. It runs over the same join the merge uses, so a bridge that drifts breaks the check rather than quietly passing it.

Two disagreements are expected and are not faults. **Horse names differ on imports** — Veikkaus appends the country tag (`Kapplans Orlando (SE)`), Heppa keeps it in `horseRegistrationCountry` — which is exactly why the bridge is positional and never name-based. **Km-time strings differ** wherever a start-type or equipment marker exists: Veikkaus writes `31,5x`, `m18,5a`; Heppa's short form is bare. The parsed `kmTimeMs` is what has to agree, so `start.kmTime` is not uniform after the merge — analyse on `kmTimeMs` and `autoStart`.

### Keeping the archive up to date

Both crawls are resumable and skip what they already have, so the recommended cycle is to pin `--from` at the start of the archive and move only `--to`:

```bash
LAST=$(date -v-2d +%F)                                  # BSD/macOS date; GNU: date -d '2 days ago' +%F
uv run veikkaus backfill --from 2021-01-01 --to "$LAST"
uv run veikkaus heppa    --from 2021-01-01 --to "$LAST"
uv run veikkaus heppa-horses
uv run veikkaus parse
```

Already-crawled dates cost nothing, so there is no date arithmetic to get wrong on `--from`. **`--to` is the parameter that matters, and it must never reach a day whose racing is not yet final.**

**Why the lag.** A race that has not run still answers `/race/{raceId}/results` with HTTP 200 — `raceStatus: OPEN` and an empty `results` list. The crawler stores that, marks the task `done`, and because re-enqueueing uses `INSERT OR IGNORE` it is **never fetched again**. Crawl a card too early and its placings are gone from the Veikkaus side for good. Two days is comfortable; it also covers late protests and corrections.

Heppa is more forgiving, and is the safety net. Its month-listing task id contains the actual date range, so the current month is re-listed on every run, and `expand()` only follows meetings whose `hasPublishedResults` is true — a meeting crawled too early is simply not crawled at all until its results exist. So even a premature Veikkaus card recovers its placings later; what you lose is that day's odds and betting percentages, which have to be captured live regardless.

**Why `heppa-horses` comes before `parse`.** It reads horse ids out of `archive.heppa_start`, so it sees the previous cycle's parse. Running it there means new horses lag by one cycle and you pay for one parse instead of two — the right trade for a scheduled job.

**Cost.** The whole cycle is a few hundred requests and a `parse` measured in seconds: **`parse` only loads payloads fetched since it last ran.** A settled archive parses in under a second. Use `parse --full` after changing any parser — the manifest records what has been loaded, not what the parser would now produce.

**If a date does get crawled too early**, re-fetch it:

```bash
uv run veikkaus backfill --from 2021-01-01 --to "$LAST" --refetch-from 2026-08-16
uv run veikkaus heppa    --from 2021-01-01 --to "$LAST" --refetch-from 2026-08-16
```

`--refetch-from` (with an optional `--refetch-to`, defaulting to the same day) puts that window back in the queue, prints how many rows it reset, and the crawl picks them up. It is a **separate window from `--from`/`--to` on purpose** — the cycle above pins `--from` at 2021-01-01, so a flag reusing that window would re-crawl five years. It touches only the calling source's rows, and only dated ones, so the per-horse registry records are never affected.

The next `parse` then reloads exactly those payloads, because a re-fetch clears their parsed marker. Note `--retry-failed` is a different thing: it resets only `failed` rows, whereas `--refetch-from` recovers `done` and `missing` ones too — and `done` is what an early crawl looks like.

### Crawl off-peak

**When you crawl matters more than how fast.** Finnish racing runs roughly 12:00–22:00 local time, and during those hours the API is serving live betting traffic and odds updates. Crawling in the small hours puts the load where their capacity is idle:

```bash
# 02:00–06:00 Finnish time, resuming wherever the last run stopped
uv run veikkaus backfill --from 2021-01-01 --limit 7000
```

The crawl is resumable, so `--limit` splits a multi-day backfill across successive nights — rerun the same command and it picks up from the manifest. Two other levers reduce the footprint more than a slower delay does: omitting `--odds` roughly halves the request count, and narrowing the date window cuts it proportionally.

The endpoints sit behind a CDN with a 10-second cache, so a single-pass crawl misses the edge on every request and reaches origin — the delay is the only thing limiting what they carry.

## Data

Everything lands in the `archive` schema of a DuckDB file:

| Table | One row per |
|---|---|
| `card` | race day at a track |
| `race` | race |
| `horse` | horse, keyed by normalised name + birth year |
| `start` | (race, horse) — the race as entered and as it paid out, incl. `startInterval` (days since the horse's previous known start, across all three sources) |
| `prev_start` | earlier start of a horse — the past-performance history, incl. its own `startInterval` and `coachName` (back-filled from the archived race) |
| `stat` | (runner, period) — career/season form, current cards only |
| `bet_percentage` | (runner, pool type) |
| `odds_snapshot` | (pool, runner, capture time) |
| `heppa_event` | race meeting in the Heppa registry, incl. track condition and temperature |
| `heppa_race` | race in the Heppa registry |
| `heppa_start` | (race, horse) as the registry recorded it — the whole field, incl. `startInterval` |
| `heppa_horse` | horse in the registry — UELN, exact birth date, origin, breeding |
| `manifest` | planned fetch — the crawl ledger, for both sources |

`start` and `prev_start` come from the same payload but answer different questions. The results endpoint publishes finishing detail only for the first three home, so `start.placement` and `start.kmTime` are NULL for the rest of the field. `prev_start` — each horse's own career line, riding along inside the runners payload — has no such cap: it carries a finishing position for the whole field (observed to 16th) plus the Finnish outcome codes for qualifying, retired and disqualified starts. Crawling the calendar therefore reconstructs full finishing orders retrospectively, for every horse that started again. See §2b of the strategy document.

`heppa_start` closes the same gap directly and without that condition. After parsing, its finishing detail is merged into `start` — filling `placement`, `kmTime` and `kmTimeMs` **only where they are NULL**, so a value Veikkaus published is never overwritten — and `start.resultSource` records which source supplied the placing. `start.prizeWon`, `start.disqualifiedCode` and `start.gallop` come from Heppa unconditionally, having no Veikkaus counterpart. Heppa's own final win odd stays in `heppa_start.winOdd`: `start.winOddsFinal` remains purely Veikkaus, where the rest of the odds history lives.

`horse.heppaHorseId` is the registry's id for a horse the archive otherwise knows only by normalised name and birth year. It is resolved positionally, through the races both sources cover, and is the authority when a `horse_key()` collision has to be settled.

**`horseKey` joins, `canonicalKey` identifies.** Veikkaus writes an import's name inconsistently — `Humble Stance`, `Humble Stance* (FR)` and `Humble Stance FR* (FR)` are one horse — so several `horseKey`s can be the same animal. `horse.canonicalKey` is the resolved identity: the registry id where there is one, a marker-free name key otherwise. Every other table still joins on `horseKey`, because that is all a Veikkaus payload can produce, so **group on `canonicalKey` when you want one row per horse.** The registry id deliberately wins over the name, because base names repeat across origin countries even though they never repeat within one.

Both `stat` and `prev_start` ride along only while a card is current, so they stay empty for backfilled years and accumulate from crawling recent dates.

## How it works

- `veikkaus_bot/models.py` — Pydantic models for the API resources (`Card`, `Race`, `Runner`, `Stat`).
- `veikkaus_bot/fetcher.py` — polite HTTP client (delay + jitter, backoff, circuit breaker) and the gzipped raw archive.
- `veikkaus_bot/crawler.py` — the resumable fetch ledger (`archive.manifest`) and the backfill driver.
- `veikkaus_bot/archive_db.py` — the `archive.*` schema and its upserts.
- `veikkaus_bot/parse.py` — raw archive → `archive.*`, including Finnish km-time and horse-identity parsing.

## Development

```bash
make install       # uv sync
make run-tests     # uv run pytest tests
make dist          # build sdist + wheel
```

### Where a full parse spends its hour

`parse` is incremental, so this is the cost of `--full` — or of the very first run. Measured over the full archive, so that the next person optimising it starts from data rather than intuition:

| phase | payloads | cost |
|---|---|---|
| `_parse_heppa_starts` | 31,822 | **~35 min** |
| `_parse_runners` | 26,369 | **~31 min** |
| `_odds_map` | 51,045 | ~10 min |
| `_results_map`, cards, races, heppa races/horses | ~21,600 | ~7 min |
| all five `recompute_*` | whole tables | **0.3 s** |

The cost is **per payload, and it is Pydantic validation rather than I/O** — reading a gzipped file is about 3 ms of the ~70 ms a runners payload takes. The recomputes are free, so keeping them whole-table (which is what makes them deterministic) costs nothing.

That shape is why the incremental parse works the way it does. A `parsedAt` column on the manifest skips payloads already loaded, so a nightly cycle costs seconds; the `recompute_*` pass still runs over the whole tables every time, because at 0.3 s it is free and being whole-table is exactly what makes it deterministic.

Two dependencies had to be broken for that to be correct. `_results_map` is scoped to the races whose runners are outstanding but still reads *parsed* results payloads, because a newly loaded runners payload needs a results payload loaded long ago. And the final win odds are read back from `archive.odds_snapshot` rather than rebuilt while loading it — which is also the more defensible answer, since latest `capturedAt` wins deterministically where the old in-memory map kept whichever payload was iterated last. On the current archive the two agree exactly: all 250,976 (race, start) pairs have a single win-pool snapshot, so the difference only starts to matter once odds are polled repeatedly before post time.

## License

MIT — see [LICENSE](LICENSE).
