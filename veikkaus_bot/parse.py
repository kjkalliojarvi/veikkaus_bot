"""Raw zone -> archive tables (strategy §4: fetching and parsing stay separate).

Nothing here touches the network. The manifest says which responses were
fetched and where they were stored; this walks those files and upserts them.
Re-running it over the same raw zone produces the same database, so a parsing
bug is always fixable without re-crawling.
"""
from datetime import datetime
import re

from . import archive_db
from .archive_db import ArchiveDb, db_ops
from .crawler import Manifest
from .fetcher import read_raw
from .models import Card, Race, Runner, Stat


FLUSH = 5000

# Finnish km-time notation. The leading minute is conventionally dropped
# ('24,9a' is 1.24,9 driven from an auto start); slower times spell it out
# ('2.05,0'). A trailing letter group carries start-type and equipment markers,
# and monté times take a leading 'm' ('m19,6') — race.monte says the same thing.
KM_TIME_RE = re.compile(r'^([a-zäöå]*)(?:(\d+)[.:])?(\d{1,2}),(\d)\s*([a-zäöå]*)$',
                        re.IGNORECASE)


def normalize_name(name: str) -> str:
    """Fold a horse name to its comparison form.

    The `*` marker (registered abroad) is dropped, but a country tag such as
    `(SE)` is kept: it is what tells an import apart from a same-named
    domestic horse.
    """
    return ' '.join(name.replace('*', '').split()).casefold()


def horse_key(name: str, birth_year: int | None) -> str:
    """Stable-enough horse identity (strategy §5).

    The API exposes no registration number, so identity is name + birth year.
    Collisions are possible and Hippos's Heppa registry is the authority when
    one has to be resolved.
    """
    return f'{normalize_name(name)}|{birth_year if birth_year else ""}'


def parse_km_time(text: str | None) -> tuple[int | None, bool]:
    """'24,9a' -> (84900, True). Returns (None, False) if it does not parse."""
    if not text:
        return None, False
    match = KM_TIME_RE.match(text.strip())
    if not match:
        return None, False
    _prefix, minutes, seconds, tenths, suffix = match.groups()
    total = (int(minutes) if minutes else 1) * 60000 + int(seconds) * 1000 + int(tenths) * 100
    return total, suffix.lower().startswith('a')


def parse_result(text: str | None) -> int | None:
    """A prev-start `result` code to a finishing position, when it is one.

    Placings come through as plain numbers, well past the third place that
    `archive.start` stops at. Everything else is a Finnish outcome code —
    `kl` (koelähtö, a qualifying start), `k` (keskeytti, did not finish),
    `hpl`/`hll`/`hlo4` (hylätty, disqualified) — which has no position and is
    kept verbatim in the `result` column instead.
    """
    if text is None:
        return None
    text = text.strip()
    return int(text) if text.isdigit() else None


def parse_meet_date(short_meet_date: str | None) -> str | None:
    """'22.12.24' -> '2024-12-22'.

    Deliberately not derived from the sibling `meetDate` field: that is
    midnight Finnish time expressed in UTC, so its date component is a day
    early on every single row. This one matches `archive.card.meetDate`.
    """
    if not short_meet_date:
        return None
    try:
        return datetime.strptime(short_meet_date.strip(), '%d.%m.%y').date().isoformat()
    except ValueError:
        return None


def parse_win_odd(text: str | None) -> int | None:
    """The API sends a prev-start's win odd as a digit string of hundredths."""
    if not text or not text.strip().isdigit():
        return None
    return int(text.strip())


def parse_tote_result(text: str | None) -> list[int]:
    """'6-3-8' -> [6, 3, 8], the start numbers of the paid places in order."""
    if not text:
        return []
    return [int(part) for part in text.split('-') if part.strip().isdigit()]


def card_record(card: Card) -> tuple:
    """Column order must stay in sync with archive_db.INSERT_CARD."""
    return (card.cardId,
            card.country,
            card.meetDate.isoformat(),
            card.trackAbbreviation,
            card.trackName,
            card.trackNumber,
            card.raceType,
            card.firstRaceStart,
            card.lunchRaces,
            card.mainPerformance,
            card.cancelled)


def race_record(race: Race) -> tuple:
    """Column order must stay in sync with archive_db.INSERT_RACE."""
    return (race.raceId,
            race.cardId,
            race.number,
            race.startTime,
            race.distance,
            race.startType,
            race.monte,
            race.firstPrize,
            race.breed,
            race.seriesSpecification,
            race.raceStatus,
            race.raceRider,
            race.trackProfile,
            race.toteResultString,
            race.intermediateTimesString)


def birth_year(runner: Runner) -> int | None:
    """None on the oldest cards, which sometimes carry no birth date at all."""
    return runner.birthDate.year if runner.birthDate else None


def is_placeholder(runner: Runner) -> bool:
    """True for the `Poissa` ("absent") runner the API sends for a vacated start
    number. It is not a horse: it is always scratched, carries no `coachName` at
    all, and its `horseAge` is arbitrary (0–24 observed across 19 of them), so
    it would otherwise land one junk `archive.horse` row per distinct age —
    colliding across tracks and dates — plus a start with nothing in it.

    `driverName` is not part of the test: it comes back as 'Poissa', '- -',
    'Poissa Poissa' or empty.
    """
    return normalize_name(runner.horseName) == 'poissa' and runner.coachName is None


def horse_record(runner: Runner) -> tuple:
    """Column order must stay in sync with archive_db.INSERT_HORSE."""
    return (horse_key(runner.horseName, birth_year(runner)),
            runner.horseName,
            birth_year(runner),
            runner.gender,
            runner.sire,
            runner.dam,
            runner.damSire)


def start_record(runner: Runner, result: dict | None, win_odds: int | None) -> tuple:
    """Column order must stay in sync with archive_db.INSERT_START."""
    result = result or {}
    km_time = result.get('kmTime')
    km_time_ms, auto_start = parse_km_time(km_time)
    return (runner.raceId,
            runner.startNumber,
            runner.runnerId,
            horse_key(runner.horseName, birth_year(runner)),
            runner.horseName,
            runner.driverName,
            runner.coachName,
            runner.ownerName,
            runner.ownerHomeTown,
            runner.startTrack,
            runner.distance,
            runner.frontShoes,
            runner.rearShoes,
            runner.specialCart,
            runner.handicapRating,
            runner.scratched,
            runner.prize,  # career earnings; equals stats.total.winMoney where both exist
            result.get('placement'),
            km_time,
            km_time_ms,
            # Provisional: the km-time suffix only speaks for horses that
            # recorded a time. recompute_auto_starts() then sets this from
            # race.startType, which covers every runner.
            auto_start if km_time_ms else None,
            win_odds if win_odds is not None else result.get('winOdds'))


def stat_records(runner: Runner) -> list[tuple]:
    """One record per statistics period. Empty for historical runners.

    Column order must stay in sync with archive_db.INSERT_STAT.
    """
    records = []
    for period in ('currentYear', 'previousYear', 'total'):
        raw = runner.stats.get(period)
        if raw is None:
            continue
        stat = Stat(**raw)
        records.append((runner.runnerId,
                        period,
                        stat.year,
                        stat.record1,
                        stat.record2,
                        stat.starts,
                        stat.position1,
                        stat.position2,
                        stat.position3,
                        stat.places,
                        stat.winMoney,
                        stat.gallopPercent,
                        stat.disqualificationPercent,
                        stat.placementPercent,
                        stat.winningPercent))
    return records


def betpercentage_records(runner: Runner) -> list[tuple]:
    """One record per pool type (e.g. KAK, T5). Percentages are hundredths of
    a percent — 939 is 9.39 %.

    Column order must stay in sync with archive_db.INSERT_BETPERCENTAGE.
    """
    return [(runner.runnerId, pool_type, values.get('percentage'))
            for pool_type, values in (runner.betPercentages or {}).items()]


def prevstart_records(runner: Runner) -> list[tuple]:
    """A horse's earlier starts, one record each. Empty for historical runners.

    Attached to the horse rather than to the runner reporting them, and
    identified by (horse, meet date, race number) so that the same start
    collapses onto one row however many later races re-report it. Entries
    missing any of those three cannot be placed in a career and are skipped —
    the caller reports the count.

    `startInterval` and `coachName` are left NULL here and filled in over the
    whole table once loading finishes — see archive_db.RECOMPUTE_START_INTERVAL
    and RECOMPUTE_PREV_START_COACH. Note in particular that `coachName` is
    *not* taken from `runner`: that is the trainer at a later race.

    Column order must stay in sync with archive_db.INSERT_PREVSTART.
    """
    key = horse_key(runner.horseName, birth_year(runner))
    records = []
    for start in runner.prevStarts:
        meet_date = parse_meet_date(start.shortMeetDate)
        if meet_date is None or start.raceNumber is None:
            continue
        km_time_ms, auto_start = parse_km_time(start.kmTime)
        records.append((start.priorStartId,
                        key,
                        meet_date,
                        start.meetDate,
                        start.trackCode,
                        start.trackName,
                        start.raceNumber,
                        start.distance,
                        start.startTrack,
                        start.driver,
                        start.driverFullName,
                        start.firstPrize,
                        start.result,
                        parse_result(start.result),
                        start.kmTime,
                        km_time_ms,
                        auto_start if km_time_ms else None,
                        parse_win_odd(start.winOdd),
                        start.frontShoes,
                        start.rearShoes,
                        start.shoesType,
                        start.headGear,
                        start.specialCart,
                        start.raceStartType,
                        start.raceRiderType,
                        start.trackProfileType,
                        start.raceSurface,
                        start.resultsAvailable,
                        None,    # startInterval — recomputed after loading
                        None))   # coachName — sourced from archive.start, ditto
    return records


def _flush(store, rows, force=False):
    if rows and (force or len(rows) >= FLUSH):
        store(rows)
        rows.clear()


def _each_payload(manifest: Manifest, raw_root: str, endpoint_type: str):
    """Yield (task, payload) for every archived response of one endpoint type."""
    for task in manifest.done(endpoint_type):
        payload = read_raw(raw_root, task.rawPath)
        if payload is None:
            print(f'missing raw file: {task.rawPath}')
            continue
        yield task, payload


def _results_map(manifest: Manifest, raw_root: str) -> dict:
    """(raceId, startNumber) -> placement / km time / final win odds.

    Only the first three finishers appear here at all; the API publishes no
    full finishing order (see §2b of the strategy doc). Two layers, because
    neither is complete on its own:

    1. `race.toteResultString` names the placed start numbers even when a pool
       paid fewer than three places (a Sija pool that paid two, say).
    2. The results payload adds km time and the final win odd, but only for
       the runners a pool actually paid out on.
    """
    out: dict[tuple[int, int], dict] = {}
    for _, payload in _each_payload(manifest, raw_root, 'races'):
        for race in payload.get('collection', []):
            placed = parse_tote_result(race.get('toteResultString'))
            for placement, start_number in enumerate(placed, start=1):
                out.setdefault((race['raceId'], start_number), {})['placement'] = placement

    for _, payload in _each_payload(manifest, raw_root, 'results'):
        race_id = payload.get('raceId')
        if race_id is None:
            continue
        for row in payload.get('results', []):
            if row.get('poolType') not in ('VOI', 'SIJ'):
                continue
            start_number = row.get('startNumber')
            if start_number is None:
                continue
            entry = out.setdefault((race_id, start_number), {})
            if row.get('position') is not None:
                entry['placement'] = row['position']
            if row.get('kmTime'):
                entry['kmTime'] = row['kmTime']
            # Only the win pool's `probable` is a win odd; Sija pays place odds.
            if row['poolType'] == 'VOI' and row.get('probable') is not None:
                entry['winOdds'] = row['probable']
    return out


UNKNOWN_CAPTURE_TIME = 0


def _captured_at(payload: dict, pool_start_time: int | None) -> int:
    """When an odds payload was published.

    `capturedAt` is what separates two snapshots of the same pool and runner,
    so it is part of the primary key and cannot be NULL. Old payloads drop
    `updated` (and `updatedString` with it) — seen on 2005 cards — so fall back
    to the pool's own race start time, which dates the snapshot closely enough
    for a backfill, where there is exactly one fetch per pool anyway. Failing
    that, an explicit epoch sentinel: unknown, but still addressable, and not a
    silently plausible timestamp.
    """
    updated = payload.get('updated')
    if updated is not None:
        return updated
    if pool_start_time is not None:
        return pool_start_time
    return UNKNOWN_CAPTURE_TIME


def _odds_map(manifest: Manifest, raw_root: str, db: ArchiveDb) -> dict:
    """Store odds snapshots and return (raceId, startNumber) -> final win odds.

    Only present when the crawl ran with --odds; otherwise the win odds in
    `archive.start` come from the paid places in the results payload.
    """
    pool_race, pool_time = {}, {}
    for task, payload in _each_payload(manifest, raw_root, 'pools'):
        for pool in payload.get('collection', []):
            pool_race[pool['poolId']] = int(task.entityId)
            pool_time[pool['poolId']] = pool.get('firstRaceStartTime')

    out: dict[tuple[int, int], int] = {}
    rows = []
    for _, payload in _each_payload(manifest, raw_root, 'odds'):
        pool_id = payload.get('poolId')
        race_id = pool_race.get(pool_id)
        if race_id is None:
            continue
        captured_at = _captured_at(payload, pool_time.get(pool_id))
        for odd in payload.get('odds', []):
            start_number = odd.get('runnerNumber')
            if start_number is None:
                continue
            rows.append((pool_id,
                         race_id,
                         start_number,
                         captured_at,
                         payload.get('poolType'),
                         odd.get('probable'),
                         odd.get('amount'),
                         bool(odd.get('scratched', False))))
            if odd.get('probable') is not None:
                out[(race_id, start_number)] = odd['probable']
        _flush(db.store_odds, rows)
    _flush(db.store_odds, rows, force=True)
    return out


def _parse_cards(manifest: Manifest, raw_root: str, db: ArchiveDb, country: str) -> int:
    rows = []
    count = 0
    for task, payload in _each_payload(manifest, raw_root, 'cards_date'):
        for raw in payload.get('collection', []):
            if raw.get('country') != country:
                continue
            try:
                rows.append(card_record(Card(**raw)))
                count += 1
            except Exception as e:
                print(f'{task.rawPath}: card {raw.get("cardId")}: {e}')
        _flush(db.store_cards, rows)
    _flush(db.store_cards, rows, force=True)
    return count


def _parse_races(manifest: Manifest, raw_root: str, db: ArchiveDb) -> int:
    rows = []
    count = 0
    for task, payload in _each_payload(manifest, raw_root, 'races'):
        for raw in payload.get('collection', []):
            try:
                rows.append(race_record(Race(**raw)))
                count += 1
            except Exception as e:
                print(f'{task.rawPath}: race {raw.get("raceId")}: {e}')
        _flush(db.store_races, rows)
    _flush(db.store_races, rows, force=True)
    return count


def _parse_runners(manifest: Manifest, raw_root: str, db: ArchiveDb,
                   results: dict, odds: dict) -> tuple[int, int]:
    """Returns (starts, prev-start records seen — before deduplication).

    The prev-start count is reports, not rows: the same start comes back on
    every later race of that horse, and they collapse onto one row.
    """
    horses, starts, stats, betpercentages, prevstarts = [], [], [], [], []
    count = 0
    prevstart_count = 0
    unplaceable = 0
    placeholders = 0
    for task, payload in _each_payload(manifest, raw_root, 'runners'):
        for raw in payload.get('collection', []):
            try:
                runner = Runner(**raw)
            except Exception as e:
                print(f'{task.rawPath}: runner {raw.get("runnerId")}: {e}')
                continue
            if is_placeholder(runner):
                placeholders += 1
                continue
            key = (runner.raceId, runner.startNumber)
            horses.append(horse_record(runner))
            starts.append(start_record(runner, results.get(key), odds.get(key)))
            stats += stat_records(runner)
            betpercentages += betpercentage_records(runner)
            runner_prevstarts = prevstart_records(runner)
            prevstarts += runner_prevstarts
            prevstart_count += len(runner_prevstarts)
            unplaceable += len(runner.prevStarts) - len(runner_prevstarts)
            count += 1
        _flush(db.store_horses, horses)
        _flush(db.store_starts, starts)
        _flush(db.store_stats, stats)
        _flush(db.store_betpercentages, betpercentages)
        _flush(db.store_prevstarts, prevstarts)
    _flush(db.store_horses, horses, force=True)
    _flush(db.store_starts, starts, force=True)
    _flush(db.store_stats, stats, force=True)
    _flush(db.store_betpercentages, betpercentages, force=True)
    _flush(db.store_prevstarts, prevstarts, force=True)
    if placeholders:
        print(f'{placeholders} `Poissa` placeholder runners skipped: vacated start numbers.')
    if unplaceable:
        print(f'{unplaceable} prev-start entries skipped: no meet date or race number.')
    return count, prevstart_count


def parse_all(db_name: str, raw_root: str, country: str) -> dict:
    """Walk the raw zone into the archive tables. Idempotent."""
    with db_ops(db_name) as conn:
        manifest = Manifest(conn)
        manifest.create()
        archive_db.create(conn)
        db = ArchiveDb(conn)
        results = _results_map(manifest, raw_root)
        odds = _odds_map(manifest, raw_root, db)
        cards = _parse_cards(manifest, raw_root, db, country)
        races = _parse_races(manifest, raw_root, db)
        starts, prevstarts = _parse_runners(manifest, raw_root, db, results, odds)
        db.recompute_start_intervals()
        db.recompute_prev_start_coaches()
        db.recompute_auto_starts()
        counts = {'cards': cards, 'races': races, 'starts': starts,
                  'prev-starts': prevstarts}
    return counts


def parse(args):
    """CLI handler: parse the raw zone into the archive tables."""
    counts = parse_all(args.db, args.raw, args.country)
    for name, count in counts.items():
        print(f'{count} {name} parsed into {args.db}.')
