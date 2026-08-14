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
uv run veikkaus backfill --from 2021-01-01  # crawl Veikkaus into data/raw/
uv run veikkaus heppa --from 2021-01-01     # crawl Heppa into data/raw/
uv run veikkaus status                      # how far both got
uv run veikkaus parse                       # raw/ -> archive.* tables
uv run veikkaus crosscheck                  # do the two sources agree?
```

Fetching and parsing are deliberately separate. `backfill` only writes gzipped raw responses into `data/raw/` and records every fetch in a manifest table, so it is resumable — kill it and rerun the same command. It crawls newest date first, one request at a time with a ≥1 s delay and exponential backoff. `parse` then reads the raw archive into the `archive.*` tables and can be re-run at any time without re-crawling.

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

### Cross-checking

`crosscheck` is the validation of §8 of the strategy, as a query rather than 20 manual lookups: it reports whether the two sources agree wherever both have an answer, how much of the field is still unplaced and why, which horses fail identity resolution, and how much of the archive the Heppa crawl has yet to reach. It runs over the same join the merge uses, so a bridge that drifts breaks the check rather than quietly passing it.

Two disagreements are expected and are not faults. **Horse names differ on imports** — Veikkaus appends the country tag (`Kapplans Orlando (SE)`), Heppa keeps it in `horseRegistrationCountry` — which is exactly why the bridge is positional and never name-based. **Km-time strings differ** wherever a start-type or equipment marker exists: Veikkaus writes `31,5x`, `m18,5a`; Heppa's short form is bare. The parsed `kmTimeMs` is what has to agree, so `start.kmTime` is not uniform after the merge — analyse on `kmTimeMs` and `autoStart`.

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
| `start` | (race, horse) — the race as entered and as it paid out |
| `prev_start` | earlier start of a horse — the past-performance history, incl. `startInterval` (days since the horse's previous start) and `coachName` (back-filled from the archived race) |
| `stat` | (runner, period) — career/season form, current cards only |
| `bet_percentage` | (runner, pool type) |
| `odds_snapshot` | (pool, runner, capture time) |
| `heppa_event` | race meeting in the Heppa registry, incl. track condition and temperature |
| `heppa_race` | race in the Heppa registry |
| `heppa_start` | (race, horse) as the registry recorded it — the whole field |
| `manifest` | planned fetch — the crawl ledger, for both sources |

`start` and `prev_start` come from the same payload but answer different questions. The results endpoint publishes finishing detail only for the first three home, so `start.placement` and `start.kmTime` are NULL for the rest of the field. `prev_start` — each horse's own career line, riding along inside the runners payload — has no such cap: it carries a finishing position for the whole field (observed to 16th) plus the Finnish outcome codes for qualifying, retired and disqualified starts. Crawling the calendar therefore reconstructs full finishing orders retrospectively, for every horse that started again. See §2b of the strategy document.

`heppa_start` closes the same gap directly and without that condition. After parsing, its finishing detail is merged into `start` — filling `placement`, `kmTime` and `kmTimeMs` **only where they are NULL**, so a value Veikkaus published is never overwritten — and `start.resultSource` records which source supplied the placing. `start.prizeWon`, `start.disqualifiedCode` and `start.gallop` come from Heppa unconditionally, having no Veikkaus counterpart. Heppa's own final win odd stays in `heppa_start.winOdd`: `start.winOddsFinal` remains purely Veikkaus, where the rest of the odds history lives.

`horse.heppaHorseId` is the registry's id for a horse the archive otherwise knows only by normalised name and birth year. It is resolved positionally, through the races both sources cover, and is the authority when a `horse_key()` collision has to be settled.

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

## License

MIT — see [LICENSE](LICENSE).
