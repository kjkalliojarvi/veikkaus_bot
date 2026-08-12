# veikkaus_bot

Builds a past-performance dataset of Finnish harness racing (ravit) from the [Veikkaus](https://www.veikkaus.fi) open Toto REST API, by crawling the race calendar into a raw JSON archive and parsing that into DuckDB. The approach and its constraints are set out in [totodatacollectionstrategy.md](totodatacollectionstrategy.md).

## Requirements

- Python >= 3.14
- [uv](https://docs.astral.sh/uv/)

## Install

```bash
uv sync
```

## Usage

```bash
uv run veikkaus backfill --from 2021-01-01  # crawl into data/raw/
uv run veikkaus status                      # how far it got
uv run veikkaus parse                       # raw/ -> archive.* tables
```

Fetching and parsing are deliberately separate. `backfill` only writes gzipped raw responses into `data/raw/` and records every fetch in a manifest table, so it is resumable — kill it and rerun the same command. It crawls newest date first, one request at a time with a ≥1 s delay and exponential backoff. `parse` then reads the raw archive into the `archive.*` tables and can be re-run at any time without re-crawling.

`--odds` additionally crawls win-pool odds for every race (roughly doubles the request count); without it, final win odds are only known for the paid places.

A five-year backfill is on the order of 50,000 requests — about 28 hours at the default 2 s delay. Note that veikkaus.fi's `robots.txt` disallows automated fetching of these paths: keep the crawl slow and single-threaded, and set `VEIKKAUS_CONTACT` to a contact address so the `User-Agent` identifies you. If this becomes more than personal research, ask Veikkaus or Suomen Hippos for sanctioned access.

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
| `manifest` | planned fetch — the crawl ledger |

`start` and `prev_start` come from the same payload but answer different questions. The results endpoint publishes finishing detail only for the first three home, so `start.placement` and `start.kmTime` are NULL for the rest of the field. `prev_start` — each horse's own career line, riding along inside the runners payload — has no such cap: it carries a finishing position for the whole field (observed to 16th) plus the Finnish outcome codes for qualifying, retired and disqualified starts. Crawling the calendar therefore reconstructs full finishing orders retrospectively, for every horse that started again. See §2b of the strategy document.

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
