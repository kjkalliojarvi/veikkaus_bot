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

from .archive_db import db_ops
from .crawler import Manifest, Task, crawl
from .fetcher import CircuitOpen, Fetcher
from .models import HEPPA_URL


# Stages sit above crawler.py's 0-5 so both sources can share one manifest and
# still order sensibly within a date. Meetings before races, as there.
HEPPA_RESULTS, HEPPA_RACES, HEPPA_START = 10, 11, 12

HEPPA_TYPES = ('heppa_results', 'heppa_races', 'heppa_start')


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
    if task.endpointType == 'heppa_races':
        meet_date, track_code = task.entityId.split('/')
        return [_start_task(meet_date, track_code, entry['race']['startNumber'])
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
        if args.retry_failed:
            print(f'{manifest.retry_failed(HEPPA_TYPES)} failed rows reset to pending.')
        try:
            fetched = crawl(manifest, fetcher, expand, HEPPA_TYPES, args.limit)
            print(f'{fetched} responses fetched into {args.raw}.')
        except CircuitOpen as e:
            print(f'Crawl paused: {e}\nRerun the same command to resume.')
        for row in manifest.counts():
            print('  {:<14} {:<8} {}'.format(*row))
