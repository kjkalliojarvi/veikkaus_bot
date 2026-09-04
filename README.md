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
uv run veikkaus heppa-foreign               # meetings abroad: starts Finnish horses made there
                                            #   list extra ones in foreign_meetings.csv
uv run veikkaus heppa-stats                 # the registry's career totals, to measure what is missing
uv run veikkaus leg-percentages             # the T-pools' per-leg betting percentages
uv run veikkaus status                      # how far both got
uv run veikkaus parse                       # raw/ -> archive.* tables
uv run veikkaus crosscheck                  # do the two sources agree?
uv run veikkaus stats [name]                # browse a horse's, trainer's or driver's Heppa starts (TUI)
                                            #   click a stats row for the starts behind it
                                            #   `t` cycles horse/trainer/driver; `horse` is an alias
```

**That block is a reference, not a sequence.** `heppa-horses`, `heppa-foreign` and `heppa-stats` are driven by what the archive already holds rather than by a date window, so each needs a `parse` in front of it — run them on a fresh archive and they tell you so and do nothing. A first build therefore parses twice:

```bash
uv run veikkaus backfill --from 2021-01-01 --create-db
uv run veikkaus heppa    --from 2021-01-01
uv run veikkaus parse                       # the three below read the archive
uv run veikkaus heppa-horses
uv run veikkaus heppa-foreign
uv run veikkaus heppa-stats                 # optional: another 14,050 requests
uv run veikkaus leg-percentages             # optional: needs `backfill --odds`, not a parse
uv run veikkaus parse                       # loads what they fetched
```

`leg-percentages` is the one archive-driven command that does **not** need a `parse` in front of it: the pool ids it crawls are read straight out of the archived `pools` payloads in `data/raw/`, so what it needs is a `backfill --odds` run, parsed or not.

The recurring cycle needs only one parse, because those commands read the *previous* run's — see [Keeping the archive up to date](#keeping-the-archive-up-to-date).

Fetching and parsing are deliberately separate. `backfill` only writes gzipped raw responses into `data/raw/` and records every fetch in a manifest table, so it is resumable — kill it and rerun the same command. It crawls newest date first, one request at a time with a ≥1 s delay and exponential backoff. `parse` then reads the raw archive into the `archive.*` tables and can be re-run at any time without re-crawling.

Every command refuses a `--db` path that is not already there, because DuckDB creates a database for whatever path it is handed: a mistyped `--db` would otherwise mint an empty archive and then report zero of everything, which looks exactly like the real one having gone missing. `backfill`, `heppa` and `parse` take `--create-db` for the genuine first run. The other seven do not, for two different reasons: `status`, `crosscheck` and `stats` only ever read one, while `heppa-horses`, `heppa-foreign`, `heppa-stats` and `leg-percentages` are driven by what the archive already holds — so none of them could do anything with a database it had just minted.

`--odds` additionally crawls the pools of every race, the win-pool odds behind them, and the T-pools' per-leg betting percentages (roughly doubles the request count); without it, final win odds are only known for the paid places and there are no betting percentages beyond Kaksari's.

A five-year backfill is on the order of 50,000 requests — about 28 hours at the default 2 s delay. Note that veikkaus.fi's `robots.txt` disallows automated fetching of these paths: keep the crawl slow and single-threaded, and set `VEIKKAUS_CONTACT` to a contact address so the `User-Agent` identifies you. If this becomes more than personal research, ask Veikkaus or Suomen Hippos for sanctioned access.

### T-pool betting percentages

How the Toto4/5/64/65/75 money was spread over the runners of each leg — the crowd's opinion of a race, weighted by real money, at a finer grain than the win odds. The Veikkaus API has them, and not where you would look for them: a runners payload's `betPercentages` carries the **single-race** pools only (KAK, TRO, DUO, EKS — verified over the whole raw zone; there has never been a T-pool row in `archive.bet_percentage`). They come from `/pool/{poolId}/odds` instead, the same endpoint as the win-pool odds, which for a pool with legs returns one row per (leg, runner):

```json
{"legNumber": 1, "raceNumber": 4, "raceId": 3476195931, "runnerNumber": 5,
 "runnerId": 1056861150, "percentage": 3056, "amount": 284265, "winProbable": 289}
```

`percentage` is hundredths of a percent and sums to ~10000 within a leg; `amount` is the money on that runner in that leg. They land in `archive.leg_percentage`, which joins `archive.start` on either `runnerId` or `(raceId, startNumber)`.

One caveat for modelling: **a scratched runner keeps the money bet on it before the withdrawal**, up to 72 % of a leg in one case, and that money still counts into the leg's ~10000. `scratched` marks those rows — the payload sends the flag only when it is set, so the column is TRUE or NULL and never FALSE — and it is the more current of the two scratched columns, since the Veikkaus runners payload behind `archive.start.scratched` is entry data and misses a horse withdrawn at the track.

**This is the one Veikkaus figure that is genuinely retrospective.** `stats` and `prevStarts` ride along in a runners payload only while the card is current, and win odds have to be captured before post time — but a T-pool's odds payload keeps its closing percentages indefinitely. Verified against the first day of the archive: `/pool/7739521/odds` still returns all 81 rows of the Toto75 of 2021-01-02, seven legs, `netSales` 35,240,376, frozen at its final `updated`. So this needs no live cycle:

```bash
uv run veikkaus leg-percentages                   # ~3,180 requests, ~1.8 h
uv run veikkaus leg-percentages --limit 20        # a few pools, to try it
```

It reads the pool ids out of the `pools` payloads already in `data/raw/` and skips what it already has, so re-running it costs only the new ones. The full crawl was **3,182 pools with no failures — 178,175 rows, 2021-01-01 to 2026-08-11**. After the never-run legs are dropped (below) the table holds **176,402 rows over 3,148 pools, 12,906 races and 146,320 runners**: T4 1,327 pools, T5 1,114, T75 362, T65 336, T64 9. None of the 15,482 legs sums outside 9,990–10,010 hundredths, and all but 15 rows join `archive.start` (those 15 are vacated start numbers — scratched, 0 %, no horse). A runner can appear in two or three pools, since one race is often a leg of both the T4 and the T5.

**One caveat beyond the scratched money:** 72 pools sit on the combination-pool meta-cards (`MM` 64, `Sl` 7, `T75` 1). Those pools are real — a Magic Monday T4 is a genuine cross-track pool, verified leg by leg against the Oulu and Teivo races it draws on — but a meta-card re-lists races that also run under their real track, so those 3,975 rows carry the meta-card's copy of `raceId`/`startNumber`. Exclude `trackNumber` 48/88 when grouping by card, exactly as the start-interval recompute does. Going forward `backfill --odds` enqueues them itself and this command is only needed to catch up the cards crawled before it existed.

Two things worth knowing. A pool crawled *while betting was still open* stores percentages that are not final, and `--refetch-from` is what fixes that: `capturedAt` is deliberately not part of the primary key, so the re-fetch replaces the row rather than adding a second version beside it. And `LEG_POOL_TYPES` names the multi-leg types explicitly — every one of them carries a `hasCombinations` field and no single-race pool does, but it arrives `false` on 1,384 of the archived pools, which makes it a live-state flag rather than an identity. A type carrying it that the tuple does not name is reported by the command instead of being silently skipped.

#### What won

`placement` on each row is what that runner did, stamped by `parse` rather than joined at query time (`ArchiveDb.recompute_leg_placements()`). So the question the percentages exist for is a `GROUP BY`:

```sql
SELECT poolType,
       count(*) FILTER (WHERE placement = 1)                    AS wins,
       round(avg(percentage) FILTER (WHERE placement = 1)/100, 2) AS mean_winner_pct
FROM archive.leg_percentage
GROUP BY 1;
```

It holds the order **the pool paid out on** (`archive.start.placement`, Veikkaus-first through the merge), which is the right order for a betting question; the official post-protest order is in `heppa_start.placement` and differs on 16 races. Two thirds of the placements come from Heppa, because 56 % of leg rows finished 4th or worse and Veikkaus publishes only the paid places.

**Every leg in the table has a winner** — that invariant is bought by dropping the legs whose race never ran: 1,773 rows over 33 whole pools, 28 meetings Heppa flags `canceled` plus one card abandoned after race 3 (Lahti 2023-11-02, −2 °C). Not one `prev_start` row names those meetings, against 208 for the days before them, and `raceStatus` is no help — it reads `OFFICIAL` on all 262 of their races. Nothing is lost: the raw payloads stay, `parse --full` re-inserts them, and the rule re-runs every parse.

Count winners with `FILTER (WHERE placement = 1)` and **do not assume one per leg**: 18 legs are dead heats. Oulu 2021-02-06 race 9 has two horses `placingRaw '1'` in both sources and nothing placed second.

The money turns out to know what it is doing. By percentage bucket, the observed win rate:

| bucket | <2% | 2–5% | 5–10% | 10–20% | 20–30% | 30–50% | ≥50% |
|---|---|---|---|---|---|---|---|
| runners | 52,335 | 27,550 | 21,519 | 22,290 | 12,062 | 8,074 | 3,268 |
| win rate | 1.0 % | 3.9 % | 7.9 % | 15.2 % | 26.6 % | 42.5 % | 66.3 % |

The leg favourite wins 41.3 % of the time, and the average winner carried 26.8 % of its leg.

### The Heppa half

`heppa` crawls Suomen Hippos's [Heppa](https://heppa.hippos.fi/mobiili/) registry, which is where the finishing order actually comes from. The Veikkaus API publishes finishing detail only for the runners a pool paid out on — the first three — so roughly **three quarters of the starts in the archive have no placing from that source at all**, and `prev_start` only recovers them for horses that raced again while the card was still current. Heppa publishes the whole field, for every Finnish meeting, back to at least 2000. It also carries three things Veikkaus has no equivalent of: this race's prize money per horse, a disqualification code for every start, and stable registry ids for horses, drivers and trainers.

It runs on the same manifest and the same raw zone as `backfill`, is resumable the same way, and shares `--limit`/`--retry-failed`/`--delay`. The two never drain each other's queue.

```bash
uv run veikkaus heppa --from 2021-01-01           # ~28,000 requests, ~16 h
uv run veikkaus heppa --from 2026-08-08 --to 2026-08-08   # one day, to try it
```

The scope is every Finnish meeting, including the local (`PAIKALLISRAVI`) and pony (`PONI`) racing that the Veikkaus API never reports — real starts in a horse's career that would otherwise be invisible. Heppa also records what Finnish horses do *abroad*, but never lists those meetings, so they need a second pass — see [Starts abroad](#starts-abroad). `heppa.hippos.fi/robots.txt` disallows `/heppa/racing`, `/heppa/horse` and `/heppa/person`, and says nothing about the `/heppa2_backend` endpoints this reads; the crawl is single-threaded at the same delay regardless.

The two sources are complementary, not interchangeable. Heppa has no odds history and no betting percentages, and its horse-level figures are as-of-now rather than as-of-race-day — `horsePriceSum` is career earnings *including* the race being reported, where Veikkaus's `careerWinnings` is the pre-race figure. Keep crawling both.

### The horse registry

`heppa-horses` fetches one `/horse/{id}` record per horse the meetings crawl turned up — **14,050 requests, ~7.8 h**. It is driven by the archive rather than by a date window, so run it after `heppa` and `parse`; re-running it later costs only the horses that are new.

It brings what neither the Veikkaus API nor the results endpoints carry: `registerNo` and **`ueln`** (international, so the join key to any other registry), an *exact* `birthDate` where `archive.horse` has only a year, breeder, colour and breed, and `sireId`/`damId` — a pedigree graph with stable ids instead of the name strings `archive.horse` holds.

**`birthCountry` is origin; `registrationCountry` is where the horse races**, and they differ for every import. That distinction is the one `heppa_start.horseRegistrationCountry` cannot make.

It does **not** improve horse-identity resolution, despite appearances: 5,232 of the 5,235 horses without a registry id race only on the Swedish simulcast and combination-pool cards, and Heppa is the Finnish registry — there is no record of them to fetch.

`heppa-stats` fetches `/horse/{id}/stats` for the same horses — another **14,050 requests** — and it is **reference data only, never a feature**. It is an as-of-now snapshot, so joining it to a past race leaks that race's result and every result after it. It is crawled for one thing nothing else can do: its per-season `starts` is the registry's own count, foreign starts included, so it is the only figure that says how much of a career the archive is *missing*. Combat Fighter reads 81 career starts against the 30 the archive holds; the difference is a Swedish career that predates his Finnish registration.

### Starts abroad

A Finnish horse that races in Sweden or France has those starts in the registry, and `heppa` will never find them: `/race/results/{from}/{to}/` lists Finnish events only — not one of the 301 events of 2026 said otherwise. But the *per-meeting* endpoints serve a foreign meeting perfectly well once you know its date and track, returning every Finnish-registered runner in each race with placing, km time, driver, trainer and prize.

So `heppa-foreign` gets the `(date, trackCode)` pairs from `archive.prev_start` — Veikkaus ships a horse's own career line, foreign starts included — and then crawls the meeting with the same machinery as the home ones.

```bash
uv run veikkaus heppa-foreign                     # ~1,950 requests, ~1.1 h
uv run veikkaus heppa-foreign --limit 40          # a few meetings, to try it
```

**Discovery is at meeting grain, which is why it returns far more than `prev_start` holds.** One row unlocks the whole meeting. The full crawl was 364 meetings at 44 tracks and loaded **3,216 starts over 1,048 horses — `prev_start` had only 660 of them, so four fifths were new**, and 752 horses gained starts. Bollnäs alone is 45 meetings and 1,507 starts.

**Backfilling further back will not help it, and that is the easy mistake to make.** `prevStarts` rides along only while a card is current, so `prev_start` reflects *when* you crawled rather than *what range*: 643 Veikkaus cards were crawled for 2021 and produced **four** `prev_start` rows and no foreign ones, against 12,861 and 408 for 2026. Discovery therefore grows *forward* — every cycle over recent dates makes the next `heppa-foreign` run see more, and re-running costs only the new meetings.

**For history, `foreign_meetings.csv` is the only route.** One `date,trackCode` per line, `#` for comments, unioned with what the query finds. Guessing is cheap — a meeting that never happened, a track that never held one, and a real track on a quiet day all answer in two bytes — but for that same reason they are indistinguishable, so the crawl reports which *seeded* meetings listed no races rather than letting a mistyped code pass for a quiet day. Of the nine foreign meetings the archive holds before 2024, six are seeded and three were found.

Two ways a foreign row is thinner, neither a fault: `startTrack` is `0` where the foreign programme's post is not recorded, and there is no Veikkaus card to bridge to, so `crosscheck` reports these separately instead of counting them as unmatched meetings.

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
uv run veikkaus heppa-foreign
uv run veikkaus parse
```

Already-crawled dates cost nothing, so there is no date arithmetic to get wrong on `--from`. **`--to` is the parameter that matters, and it must never reach a day whose racing is not yet final.**

**Why the lag.** A race that has not run still answers `/race/{raceId}/results` with HTTP 200 — `raceStatus: OPEN` and an empty `results` list. The crawler stores that, marks the task `done`, and because re-enqueueing uses `INSERT OR IGNORE` it is **never fetched again**. Crawl a card too early and its placings are gone from the Veikkaus side for good. Two days is comfortable; it also covers late protests and corrections.

Heppa is more forgiving, and is the safety net. Its month-listing task id contains the actual date range, so the current month is re-listed on every run, and `expand()` only follows meetings whose `hasPublishedResults` is true — a meeting crawled too early is simply not crawled at all until its results exist. So even a premature Veikkaus card recovers its placings later; what you lose is that day's win odds and Kaksari percentages, which have to be captured live regardless. The T-pools' per-leg percentages are the exception — those stay fetchable for years, so `leg-percentages` recovers them whenever it next runs.

**Why `heppa-horses` and `heppa-foreign` come before `parse`.** Both are driven by the archive rather than by a date window — the first reads horse ids out of `archive.heppa_start`, the second reads meetings out of `archive.prev_start` — so both see the *previous* cycle's parse. Putting them there means new horses and newly-discovered meetings lag by one cycle and you pay for one parse instead of two, which is the right trade for a scheduled job. The parse at the end is what loads what they fetched.

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
| `bet_percentage` | (runner, pool type) — the single-race pools only: KAK, TRO, DUO, EKS |
| `odds_snapshot` | (pool, runner, capture time) |
| `leg_percentage` | (pool, leg, runner) — T-pool betting percentages, with what each runner did |
| `heppa_event` | race meeting in the Heppa registry, incl. track condition and temperature |
| `heppa_race` | race in the Heppa registry |
| `heppa_start` | (race, horse) as the registry recorded it — the whole field at every Finnish meeting, plus Finnish horses' starts abroad, told apart by `finnishTrack`; incl. `startInterval` |
| `heppa_horse` | horse in the registry — UELN, exact birth date, origin, breeding |
| `heppa_horse_stat` | (horse, season, monte) — the registry's own career totals. **As-of-now: reference only, never a feature** |
| `manifest` | planned fetch — the crawl ledger, for both sources |

`start` and `prev_start` come from the same payload but answer different questions. The results endpoint publishes finishing detail only for the first three home, so `start.placement` and `start.kmTime` are NULL for the rest of the field. `prev_start` — each horse's own career line, riding along inside the runners payload — has no such cap: it carries a finishing position for the whole field (observed to 16th) plus the Finnish outcome codes for qualifying, retired and disqualified starts. Crawling the calendar therefore reconstructs full finishing orders retrospectively, for every horse that started again. See §2b of the strategy document.

`heppa_start` closes the same gap directly and without that condition. After parsing, its finishing detail is merged into `start` — filling `placement`, `kmTime` and `kmTimeMs` **only where they are NULL**, so a value Veikkaus published is never overwritten — and `start.resultSource` records which source supplied the placing. `start.prizeWon`, `start.disqualifiedCode` and `start.gallop` come from Heppa unconditionally, having no Veikkaus counterpart. Heppa's own final win odd stays in `heppa_start.winOdd`: `start.winOddsFinal` remains purely Veikkaus, where the rest of the odds history lives.

`horse.heppaHorseId` is the registry's id for a horse the archive otherwise knows only by normalised name and birth year. It is resolved positionally, through the races both sources cover, and is the authority when a `horse_key()` collision has to be settled.

**`horseKey` joins, `canonicalKey` identifies.** Veikkaus writes an import's name inconsistently — `Humble Stance`, `Humble Stance* (FR)` and `Humble Stance FR* (FR)` are one horse — so several `horseKey`s can be the same animal. `horse.canonicalKey` is the resolved identity: the registry id where there is one, a marker-free name key otherwise. Every other table still joins on `horseKey`, because that is all a Veikkaus payload can produce, so **group on `canonicalKey` when you want one row per horse.** The registry id deliberately wins over the name, because base names repeat across origin countries even though they never repeat within one.

Both `stat` and `prev_start` ride along only while a card is current, so they stay empty for backfilled years and accumulate from crawling recent dates. That is also why `heppa-foreign` cannot reach old foreign meetings: `prev_start` is where it finds them, so backfilling adds nothing and only the seed file does.

## How it works

- `veikkaus_bot/models.py` — Pydantic models for both APIs (`Card`, `Race`, `Runner`, `Stat`; `HeppaEvent`, `HeppaRace`, `HeppaStart`, `HeppaHorse`, `HeppaHorseStats`).
- `veikkaus_bot/fetcher.py` — polite HTTP client (delay + jitter, backoff, circuit breaker) and the gzipped raw archive.
- `veikkaus_bot/crawler.py` — the resumable fetch ledger (`archive.manifest`), the crawl loop, and the Veikkaus graph.
- `veikkaus_bot/heppa.py` — the Heppa graph: the Finnish meetings, the registry records, the meetings abroad, and the career totals. Four separate opt-ins over the same loop and ledger.
- `veikkaus_bot/archive_db.py` — the `archive.*` schema, its upserts, and the recomputes that resolve identity and the layoff windows.
- `veikkaus_bot/parse.py` — raw archive → `archive.*`, including Finnish km-time and horse-identity parsing.
- `veikkaus_bot/crosscheck.py` — the two sources read against each other, as queries.
- `veikkaus_bot/horse_stats.py` / `horse_tui.py` — the SQL and the Textual app behind `veikkaus stats`.

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
