"""The Heppa crawl graph — Suomen Hippos as a second source.

The Veikkaus API publishes finishing detail only for the runners a pool paid
out on, which is the first three. Everyone else's placing is structurally
absent, and `runner.prevStarts` recovers it only for horses that raced again
while the card was still current — so the backfilled years recover nothing.

Heppa is the official registry and publishes the whole field for every Finnish
meeting, including the local and pony racing the Veikkaus API never carries at
all. This module is the crawl graph for it; the loading half lives in parse.py
and the merge into `archive.start` in archive_db.RECOMPUTE_START_FROM_HEPPA.

Same shape as crawler.py, and deliberately so: the same `Manifest`, the same
raw zone, the same `crawl()` loop, the same resume-by-rerunning. Only the task
constructors and `expand()` differ, because only the API does.

Politeness: `heppa.hippos.fi/robots.txt` disallows exactly `/heppa/racing`,
`/heppa/horse` and `/heppa/person`, and says nothing about `/heppa2_backend`
(the Mobiiliheppa backend this reads) — friendlier than the Veikkaus situation,
but the crawl stays single-threaded at the same delay regardless.
"""
from datetime import date, datetime, timedelta

from . import archive_db
from .archive_db import HEPPA_TRACK_ALIASES, db_ops
from .crawler import Manifest, Task, crawl, refetch_window
from .fetcher import CircuitOpen, Fetcher
from .models import HEPPA_URL


# Stages sit above crawler.py's 0-5 so both sources can share one manifest and
# still order sensibly within a date. Meetings before races, as there.
HEPPA_RESULTS, HEPPA_RACES, HEPPA_START, HEPPA_HORSE = 10, 11, 12, 13
HEPPA_FOREIGN_RACES, HEPPA_FOREIGN_START = 14, 15
HEPPA_HORSE_STAT = 16

# The Heppa crawls are separate opt-ins and drain separately: the meetings crawl
# is driven by a date window, the other two by what the meetings turned up.
# Keeping them apart is what stops `heppa` from silently starting an eight-hour
# horse crawl on the back of a one-day results run.
#
# The starts abroad need their own two types rather than reusing `heppa_races`
# and `heppa_start`, for that same reason: a foreign races payload expands into
# start tasks, and if those carried the Finnish types then a foreign run would
# drain the meetings crawl's pending rows as well as its own.
HEPPA_TYPES = ('heppa_results', 'heppa_races', 'heppa_start')
HEPPA_HORSE_TYPES = ('heppa_horse',)
HEPPA_FOREIGN_TYPES = ('heppa_foreign_races', 'heppa_foreign_start')
HEPPA_STAT_TYPES = ('heppa_horse_stat',)


def months(start: date, end: date) -> list[tuple[date, date]]:
    """Every (first, last) day-pair of the months spanned by [start, end].

    The results endpoint takes a date range and a whole year answers in under
    two seconds, but a month is the unit that makes resumption cheap: a run
    killed mid-window re-fetches at most one listing.
    """
    out = []
    first = start.replace(day=1)
    while first <= end:
        following = (first + timedelta(days=32)).replace(day=1)
        out.append((max(first, start), min(following - timedelta(days=1), end)))
        first = following
    return out


def results_task(first: date, last: date) -> Task:
    """The meetings of one date range.

    The range itself is the entity id, not the month it falls in. A window that
    starts mid-month produces a clipped range, and `INSERT OR IGNORE` would
    otherwise let that clipped listing satisfy a later, wider run — silently
    leaving the first half of the month uncrawled. Distinct ranges are distinct
    tasks; the cost of re-listing is one cheap call per month.

    `meetDate` is the month's first day so that next_pending()'s newest-first
    ordering still holds across both sources.
    """
    span = f'{first.isoformat()}_{last.isoformat()}'
    return Task('heppa_results', span,
                f'/race/results/{first.isoformat()}/{last.isoformat()}/',
                f'heppa/results_{span}.json.gz',
                first.replace(day=1).isoformat(), None, HEPPA_RESULTS)


def _races_task(meet_date: str, track_code: str) -> Task:
    return Task('heppa_races', f'{meet_date}/{track_code}',
                f'/race/{meet_date}/{track_code}/races',
                f'{meet_date}/heppa_{track_code}/races.json.gz',
                meet_date, None, HEPPA_RACES)


def _start_task(meet_date: str, track_code: str, race_number: str) -> Task:
    return Task('heppa_start', f'{meet_date}/{track_code}/{race_number}',
                f'/race/{meet_date}/{track_code}/start/{race_number}',
                f'{meet_date}/heppa_{track_code}/start_{race_number}.json.gz',
                meet_date, None, HEPPA_START)


def horse_task(horse_id: str) -> Task:
    """One horse's registry record.

    Sharded on the last two digits of the id: 14,050 horses in one directory
    is not fatal, but it is unpleasant to work with and the shard costs
    nothing. A horse has no meet date, so the `meetDate` column stays NULL —
    ordering is meaningless here and resumption comes from the manifest status.
    """
    return Task('heppa_horse', horse_id, f'/horse/{horse_id}',
                f'heppa/horse/{horse_id[-2:]}/{horse_id}.json.gz',
                None, None, HEPPA_HORSE)


def _foreign_races_task(meet_date: str, track_code: str) -> Task:
    """One meeting abroad: which of its races a Finnish horse ran in.

    A separate raw directory from the Finnish `heppa_{track}` one. The track
    codes are disjoint by construction — FOREIGN_MEETINGS excludes every code
    the Finnish listing has ever named — but one raw path answerable by two
    endpoint types is the kind of thing that only bites after a track changes
    category, and the prefix costs nothing.
    """
    return Task('heppa_foreign_races', f'{meet_date}/{track_code}',
                f'/race/{meet_date}/{track_code}/races',
                f'{meet_date}/heppa_foreign_{track_code}/races.json.gz',
                meet_date, None, HEPPA_FOREIGN_RACES)


def _foreign_start_task(meet_date: str, track_code: str, race_number: str) -> Task:
    return Task('heppa_foreign_start', f'{meet_date}/{track_code}/{race_number}',
                f'/race/{meet_date}/{track_code}/start/{race_number}',
                f'{meet_date}/heppa_foreign_{track_code}/start_{race_number}.json.gz',
                meet_date, None, HEPPA_FOREIGN_START)


# The meetings abroad, and there is no listing for them: `/race/results` returns
# Finnish events only (0 of the 301 events of 2026 said otherwise), and `/race/`
# takes nothing but a date, so the (date, track) pairs have to come from the
# archive. `prev_start` is the one table that has them — Veikkaus ships a horse's
# own career line, foreign starts included — which is why this crawl, like the
# horse one, has to follow a `heppa` crawl and a `parse`.
#
# **The Finnish track set is read from `heppa_event`, not from `heppa_start`.**
# heppa_event comes from that Finnish-only listing, so it can never name a track
# abroad. heppa_start can, the moment this crawl succeeds once — and then the
# next run would exclude exactly the tracks it had just learnt about and find
# nothing. The bug would look like the crawl finishing early.
#
# The alias fold is Veikkaus's `Hr2` for Harma, which upper-cases to a code Heppa
# does not have; without it the track reads as foreign and this crawl would
# re-fetch 28 Finnish meetings it already has.
_TRACK = ' '.join(f"WHEN '{k}' THEN '{v}'" for k, v in HEPPA_TRACK_ALIASES.items())
FOREIGN_MEETINGS = f"""
    SELECT DISTINCT ps.meetDate,
           CASE upper(ps.trackCode) {_TRACK} ELSE upper(ps.trackCode) END AS trackCode
    FROM archive.prev_start ps
    WHERE ps.trackCode IS NOT NULL
      AND CASE upper(ps.trackCode) {_TRACK} ELSE upper(ps.trackCode) END
          NOT IN (SELECT DISTINCT trackCode FROM archive.heppa_event)
    ORDER BY ps.meetDate DESC
"""


def horse_stat_task(horse_id: str) -> Task:
    """One horse's registry statistics, sharded like its registry record.

    Kept apart from `heppa_horse` rather than folded into it because the two are
    different kinds of fact: `/horse/{id}` is the animal, and nothing in it
    changes, while this is a running total that is only ever true today. Its own
    endpoint type means a re-fetch of one is not a re-fetch of the other.
    """
    return Task('heppa_horse_stat', horse_id, f'/horse/{horse_id}/stats',
                f'heppa/stats/{horse_id[-2:]}/{horse_id}.json.gz',
                None, None, HEPPA_HORSE_STAT)


# Meetings abroad added by hand, for the ones prev_start cannot name.
#
# FOREIGN_MEETINGS finds a meeting only if Veikkaus re-reported one of its
# horses, and its foreign rows begin in 2023-09 because a runners payload
# carries `prevStarts` only while the card is current. Everything outside that
# is invisible to the archive and perfectly visible to a person reading a
# horse's registry page — so this is where that knowledge goes.
#
# A comma-separated `date,trackCode` per line, `#` for comments. The comments
# are the point: a seeded meeting without a note is a fact with no provenance,
# and in a year nobody will remember which horse it was for.
#
# Guessing is cheap. A meeting that does not exist, a track that never held one,
# and a real track on a quiet day all answer `200` with `[]` in two bytes — and
# all three look identical, which is why `backfill_foreign` reports the seeds
# that came back with nothing rather than letting a typo pass for a quiet day.
SEED_FILE = 'foreign_meetings.csv'


def read_seeds(path: str) -> tuple[list[tuple[str, str]], list[str]]:
    """(meetings, complaints) from a seed file. A missing file is no seeds.

    The track code is upper-cased and alias-folded exactly as FOREIGN_MEETINGS
    does its own, so `Sv` and `SV` are one meeting and a seeded `Hr2` becomes
    the `HR` the caller then recognises as Finnish. Recognising it is the
    caller's job, not this function's — it has no archive to compare against.

    A malformed line is reported and skipped rather than fetched: the date goes
    straight into a URL, and `/race/not-a-date/BO/races` is a 400 recorded as a
    failure of the crawl rather than of the file.
    """
    try:
        with open(path, encoding='utf-8') as f:
            lines = f.readlines()
    except FileNotFoundError:
        return [], []
    meetings, complaints = [], []
    for number, line in enumerate(lines, start=1):
        text = line.split('#')[0].strip()
        if not text:
            continue
        parts = [p.strip() for p in text.split(',')]
        if len(parts) != 2 or not all(parts):
            complaints.append(f'{path}:{number}: expected `date,trackCode`, got {text!r}')
            continue
        day, track = parts
        try:
            datetime.strptime(day, '%Y-%m-%d')
        except ValueError:
            complaints.append(f'{path}:{number}: {day!r} is not a yyyy-mm-dd date')
            continue
        track = track.upper()
        meetings.append((day, HEPPA_TRACK_ALIASES.get(track, track)))
    return meetings, complaints


# Seeded meetings whose listing came back empty — a wrong track code and a quiet
# day are the same two bytes, so the one thing that can be said is which seeds
# produced no races at all. Read from the manifest rather than the raw zone: a
# races task with no `heppa_foreign_start` child is exactly an empty listing.
EMPTY_MEETINGS = """
    SELECT m.entityId FROM archive.manifest m
    WHERE m.endpointType = 'heppa_foreign_races' AND m.status = 'done'
      AND NOT EXISTS (SELECT 1 FROM archive.manifest c
                      WHERE c.endpointType = 'heppa_foreign_start'
                        AND c.entityId LIKE m.entityId || '/%')
"""


# The tracks the Finnish listing has named, which is what makes a seeded track
# foreign or a mistake. Same source as FOREIGN_MEETINGS excludes on, for the
# same reason: heppa_event cannot name a track abroad, heppa_start can.
FINNISH_TRACKS = 'SELECT DISTINCT trackCode FROM archive.heppa_event'


# The horse ids the meetings crawl turned up. Read from the archive rather than
# from the raw zone, so this reflects what `parse` has actually loaded — which
# is also why it has to run after `heppa` and `parse`, not alongside them.
HORSE_IDS = """
    SELECT DISTINCT horseId FROM archive.heppa_start
    WHERE horseId IS NOT NULL ORDER BY horseId
"""


def expand(task: Task, payload) -> list[Task]:
    """The children a fetched response implies.

    Heppa sends bare JSON lists, not the `collection` wrapper the Veikkaus API
    uses, so this cannot share crawler.expand().
    """
    if not isinstance(payload, list):
        return []
    if task.endpointType == 'heppa_results':
        children = []
        for day in payload:
            for event in day.get('events', []):
                # A cancelled meeting has no results to publish, and the two
                # flags are perfect complements in practice (15/15 in 2025);
                # test both rather than trusting that to hold.
                if not event.get('finnishTrack'):
                    continue
                if event.get('canceled') or not event.get('hasPublishedResults'):
                    continue
                children.append(_races_task(event['date'], event['trackCode']))
        return children
    if task.endpointType in ('heppa_races', 'heppa_foreign_races'):
        meet_date, track_code = task.entityId.split('/')
        # A meeting abroad lists only the races a Finnish horse ran in, so the
        # numbering arrives with gaps — Solvalla 2026-05-30 comes back as races
        # 1-5, 7, 11 and 31. Read the numbers rather than counting them.
        start_task = (_start_task if task.endpointType == 'heppa_races'
                      else _foreign_start_task)
        return [start_task(meet_date, track_code, entry['race']['startNumber'])
                for entry in payload if entry.get('race', {}).get('startNumber')]
    return []


def backfill(args):
    """CLI handler: enqueue a date window of Heppa meetings and crawl it."""
    start = datetime.strptime(args.start, '%Y-%m-%d').date()
    end = datetime.strptime(args.end, '%Y-%m-%d').date() if args.end else date.today()
    if start > end:
        print('--from must not be after --to.')
        return
    fetcher = Fetcher(args.raw, args.delay, base_url=HEPPA_URL)
    with db_ops(args.db) as conn:
        manifest = Manifest(conn)
        manifest.create()
        manifest.enqueue([results_task(first, last) for first, last in months(start, end)])
        refetch_window(args, manifest, HEPPA_TYPES)
        if args.retry_failed:
            print(f'{manifest.retry_failed(HEPPA_TYPES)} failed rows reset to pending.')
        try:
            fetched = crawl(manifest, fetcher, expand, HEPPA_TYPES, args.limit)
            print(f'{fetched} responses fetched into {args.raw}.')
        except CircuitOpen as e:
            print(f'Crawl paused: {e}\nRerun the same command to resume.')
        for row in manifest.counts():
            print('  {:<14} {:<8} {}'.format(*row))


def backfill_horses(args):
    """CLI handler: crawl the registry record of every horse already seen.

    Driven by the archive rather than by a date window, so it has to follow a
    `heppa` crawl and a `parse`. Re-running it after a later crawl picks up the
    new horses and skips the rest — `INSERT OR IGNORE` makes re-enqueueing a
    no-op.
    """
    fetcher = Fetcher(args.raw, args.delay, base_url=HEPPA_URL)
    with db_ops(args.db) as conn:
        manifest = Manifest(conn)
        manifest.create()
        archive_db.create(conn)
        ids = [row[0] for row in conn.execute(HORSE_IDS).fetchall()]
        if not ids:
            print('No horse ids in the archive yet.\n'
                  'Run `veikkaus heppa --from D` and then `veikkaus parse` first.')
            return
        print(f'{len(ids)} horses known; already-fetched ones are skipped.')
        manifest.enqueue([horse_task(horse_id) for horse_id in ids])
        if args.retry_failed:
            print(f'{manifest.retry_failed(HEPPA_HORSE_TYPES)} failed rows reset to pending.')
        try:
            fetched = crawl(manifest, fetcher, expand, HEPPA_HORSE_TYPES, args.limit)
            print(f'{fetched} responses fetched into {args.raw}.')
        except CircuitOpen as e:
            print(f'Crawl paused: {e}\nRerun the same command to resume.')
        for row in manifest.counts():
            print('  {:<14} {:<8} {}'.format(*row))


def backfill_foreign(args):
    """CLI handler: crawl the meetings abroad that the archive knows about.

    Heppa records a Finnish-registered horse's starts abroad and serves them
    through the same per-meeting endpoints as the home ones — it just never
    lists those meetings, so they have to be discovered from `prev_start`. See
    FOREIGN_MEETINGS.

    Partial by construction, and worth being clear about: a meeting is only
    discoverable if Veikkaus re-reported one of its horses, so this recovers the
    starts abroad the archive can see rather than every start a horse ever had.
    A horse's career before it joined the Finnish register is out of reach
    entirely.
    """
    fetcher = Fetcher(args.raw, args.delay, base_url=HEPPA_URL)
    with db_ops(args.db) as conn:
        manifest = Manifest(conn)
        manifest.create()
        archive_db.create(conn)
        discovered = conn.execute(FOREIGN_MEETINGS).fetchall()
        seeds, complaints = read_seeds(getattr(args, 'seeds', None) or SEED_FILE)
        # A seeded Finnish track is a mistake rather than a discovery: `heppa`
        # already has it, and fetching it again under the foreign endpoint types
        # would store the same rows against a second raw path.
        finnish = {row[0] for row in conn.execute(FINNISH_TRACKS).fetchall()}
        home = [f'{d}/{t}' for d, t in seeds if t in finnish]
        seeds = [(d, t) for d, t in seeds if t not in finnish]
        for complaint in complaints:
            print(complaint)
        if home:
            print(f'{len(home)} seeded meeting(s) are at Finnish tracks and were '
                  'skipped — `heppa` covers those: ' + ', '.join(home[:10]))
        # dict rather than set: the query returns newest first, which is worth
        # keeping even though next_pending sorts by meet date again anyway.
        meetings = list(dict.fromkeys([tuple(row) for row in discovered] + seeds))
        if not meetings:
            print('No meetings abroad in the archive yet.\n'
                  'Run `veikkaus heppa --from D` and then `veikkaus parse` first,\n'
                  f'or list meetings by hand in {SEED_FILE}.')
            return
        tracks = len({track for _, track in meetings})
        extra = len(meetings) - len(discovered)
        seeded = f', {len(seeds)} seeded ({extra} of them new)' if seeds else ''
        print(f'{len(meetings)} meetings abroad at {tracks} tracks{seeded}; '
              'already-fetched ones are skipped.')
        manifest.enqueue([_foreign_races_task(meet_date, track)
                          for meet_date, track in meetings])
        refetch_window(args, manifest, HEPPA_FOREIGN_TYPES)
        if args.retry_failed:
            print(f'{manifest.retry_failed(HEPPA_FOREIGN_TYPES)} failed rows reset to pending.')
        try:
            fetched = crawl(manifest, fetcher, expand, HEPPA_FOREIGN_TYPES, args.limit)
            print(f'{fetched} responses fetched into {args.raw}.')
        except CircuitOpen as e:
            print(f'Crawl paused: {e}\nRerun the same command to resume.')
        empty = {row[0] for row in conn.execute(EMPTY_MEETINGS).fetchall()}
        blank = [f'{d}/{t}' for d, t in seeds if f'{d}/{t}' in empty]
        if blank:
            print(f'{len(blank)} seeded meeting(s) listed no races: '
                  + ', '.join(blank[:10]) + ('' if len(blank) <= 10 else ' ...'))
            print('  A wrong track code and a quiet day look the same here. A whole '
                  'track listed means the\n  code is probably wrong; --refetch-from '
                  'resets a date whose results were not out yet.')
        for row in manifest.counts():
            print('  {:<14} {:<8} {}'.format(*row))


def backfill_horse_stats(args):
    """CLI handler: crawl every known horse's registry statistics.

    **What this is for, and what it must not be used for.** It is the only
    source that says how much of a horse's career the archive is missing: Heppa
    counts the starts abroad that it will not enumerate, so `starts` per season
    is the denominator `heppa-foreign`'s coverage is measured against. It is also
    an as-of-now snapshot, so joining it to a past race leaks that race's result
    — see archive_db.CREATE_HEPPA_HORSE_STAT_TABLE.

    Driven by the archive, like the horse and foreign crawls, so it follows a
    `heppa` crawl and a `parse`.
    """
    fetcher = Fetcher(args.raw, args.delay, base_url=HEPPA_URL)
    with db_ops(args.db) as conn:
        manifest = Manifest(conn)
        manifest.create()
        archive_db.create(conn)
        ids = [row[0] for row in conn.execute(HORSE_IDS).fetchall()]
        if not ids:
            print('No horse ids in the archive yet.\n'
                  'Run `veikkaus heppa --from D` and then `veikkaus parse` first.')
            return
        print(f'{len(ids)} horses known; already-fetched ones are skipped.')
        manifest.enqueue([horse_stat_task(horse_id) for horse_id in ids])
        if args.retry_failed:
            print(f'{manifest.retry_failed(HEPPA_STAT_TYPES)} failed rows reset to pending.')
        try:
            fetched = crawl(manifest, fetcher, expand, HEPPA_STAT_TYPES, args.limit)
            print(f'{fetched} responses fetched into {args.raw}.')
        except CircuitOpen as e:
            print(f'Crawl paused: {e}\nRerun the same command to resume.')
        for row in manifest.counts():
            print('  {:<14} {:<8} {}'.format(*row))
