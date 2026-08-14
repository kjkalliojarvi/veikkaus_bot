"""Manifest-driven, resumable backfill crawler (strategy §6).

The manifest is a ledger of every planned fetch. The crawl loop is just "take
the next pending row, fetch it, store the raw response, mark it done" — and
parsing a fetched response enqueues its children. Kill the process at any
point; restarting resumes exactly where it stopped.

Ordering is newest date first (§6): if access breaks mid-crawl, the most
valuable seasons are already banked.
"""
from collections import namedtuple
from datetime import date, datetime, timedelta
import json

from .archive_db import CREATE_SCHEMA, db_ops
from .fetcher import CircuitOpen, Fetcher


# Stages order the work within one meet date: cards before races before the
# per-race payloads. next_pending() sorts by (meetDate DESC, stage ASC).
CARDS_DATE, RACES, RUNNERS, RESULTS, POOLS, ODDS = range(6)

# The manifest is shared with the Heppa crawl (heppa.py), which uses its own
# endpoint types and its own stage range. next_pending() filters on these so a
# run of one source never fetches the other's rows — different host, different
# rate limit, different circuit breaker.
VEIKKAUS_TYPES = ('cards_date', 'races', 'runners', 'results', 'pools', 'odds')

Task = namedtuple('Task', 'endpointType entityId url rawPath meetDate cardId stage')

CREATE_MANIFEST_TABLE = """
    CREATE TABLE IF NOT EXISTS archive.manifest(
        endpointType TEXT,
        entityId TEXT,
        url TEXT,
        rawPath TEXT,
        meetDate TEXT,
        cardId BIGINT,
        stage BIGINT,
        status TEXT,
        httpCode BIGINT,
        fetchedAt TEXT,
        attempts BIGINT,
        error TEXT,
        PRIMARY KEY (endpointType, entityId));
"""

# Never clobbers a row that is already done — re-enqueueing is a no-op.
INSERT_TASK = """
    INSERT OR IGNORE INTO archive.manifest
    VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', NULL, NULL, 0, NULL);
"""


class Manifest:
    """The fetch ledger. Wraps an open DuckDB connection."""

    def __init__(self, conn):
        self.conn = conn

    def create(self):
        self.conn.execute(CREATE_SCHEMA)
        self.conn.execute(CREATE_MANIFEST_TABLE)

    def enqueue(self, tasks: list[Task]):
        if tasks:
            self.conn.executemany(INSERT_TASK, [tuple(t) for t in tasks])

    def next_pending(self, limit: int, types: tuple = VEIKKAUS_TYPES) -> list[Task]:
        placeholders = ', '.join('?' * len(types))
        rows = self.conn.execute(
            f"""SELECT endpointType, entityId, url, rawPath, meetDate, cardId, stage
                FROM archive.manifest
                WHERE status = 'pending' AND endpointType IN ({placeholders})
                ORDER BY meetDate DESC, stage ASC LIMIT ?""",
            (*types, limit)).fetchall()
        return [Task(*row) for row in rows]

    def mark(self, task: Task, status: str, http_code, error):
        self.conn.execute(
            """UPDATE archive.manifest
               SET status = ?, httpCode = ?, fetchedAt = ?, attempts = attempts + 1,
                   error = ?
               WHERE endpointType = ? AND entityId = ?""",
            (status, http_code, datetime.now().isoformat(timespec='seconds'), error,
             task.endpointType, task.entityId))

    def retry_failed(self, types: tuple = VEIKKAUS_TYPES) -> int:
        placeholders = ', '.join('?' * len(types))
        where = f"status = 'failed' AND endpointType IN ({placeholders})"
        before = self.conn.execute(
            f'SELECT count(*) FROM archive.manifest WHERE {where}', types).fetchone()[0]
        self.conn.execute(
            f"UPDATE archive.manifest SET status = 'pending' WHERE {where}", types)
        return before

    def counts(self) -> list[tuple]:
        return self.conn.execute(
            """SELECT endpointType, status, count(*) FROM archive.manifest
               GROUP BY endpointType, status ORDER BY endpointType, status""").fetchall()

    def done(self, endpoint_type: str) -> list[Task]:
        """Every successfully fetched row of one endpoint type, oldest date first."""
        rows = self.conn.execute(
            """SELECT endpointType, entityId, url, rawPath, meetDate, cardId, stage
               FROM archive.manifest
               WHERE endpointType = ? AND status = 'done'
               ORDER BY meetDate ASC""", (endpoint_type,)).fetchall()
        return [Task(*row) for row in rows]


def dates(start: date, end: date) -> list[date]:
    """Every date in [start, end], newest first."""
    span = (end - start).days
    return [end - timedelta(days=n) for n in range(span + 1)]


def cards_task(day: date) -> Task:
    d = day.isoformat()
    return Task('cards_date', d, f'/cards/date/{d}', f'{d}/cards.json.gz', d, None, CARDS_DATE)


def _races_task(meet_date: str, card_id: int) -> Task:
    return Task('races', str(card_id), f'/card/{card_id}/races',
                f'{meet_date}/card_{card_id}/races.json.gz', meet_date, card_id, RACES)


def _race_task(kind: str, stage: int, meet_date: str, card_id: int, race_id: int) -> Task:
    return Task(kind, str(race_id), f'/race/{race_id}/{kind}',
                f'{meet_date}/card_{card_id}/race_{race_id}_{kind}.json.gz',
                meet_date, card_id, stage)


def _odds_task(meet_date: str, card_id: int, pool_id: int) -> Task:
    return Task('odds', str(pool_id), f'/pool/{pool_id}/odds',
                f'{meet_date}/card_{card_id}/pool_{pool_id}_odds.json.gz',
                meet_date, card_id, ODDS)


def expand(task: Task, payload, country: str, with_odds: bool) -> list[Task]:
    """The children a fetched response implies."""
    collection = payload.get('collection', []) if isinstance(payload, dict) else []
    if task.endpointType == 'cards_date':
        return [_races_task(task.meetDate, c['cardId'])
                for c in collection if c.get('country') == country]
    if task.endpointType == 'races':
        children = []
        for race in collection:
            children.append(_race_task('runners', RUNNERS, task.meetDate, task.cardId, race['raceId']))
            children.append(_race_task('results', RESULTS, task.meetDate, task.cardId, race['raceId']))
            if with_odds:
                children.append(_race_task('pools', POOLS, task.meetDate, task.cardId, race['raceId']))
        return children
    if task.endpointType == 'pools':
        # Only the win pool carries a per-runner odd for every starter.
        return [_odds_task(task.meetDate, task.cardId, p['poolId'])
                for p in collection if p.get('poolType') == 'VOI']
    return []


def crawl(manifest: Manifest, fetcher: Fetcher, expander, types: tuple = VEIKKAUS_TYPES,
          limit: int | None = None) -> int:
    """Drain the manifest. Returns the number of rows fetched.

    `expander` is the crawl graph — `(task, payload) -> list[Task]`. The loop
    itself knows nothing about either API, so the Heppa source reuses it whole.
    """
    fetched = 0
    while True:
        batch = manifest.next_pending(50, types)
        if not batch:
            return fetched
        for task in batch:
            if limit is not None and fetched >= limit:
                return fetched
            result = fetcher.fetch(task.url)
            if result.body is None:
                status = 'missing' if result.error is None else 'failed'
                manifest.mark(task, status, result.httpCode, result.error)
                continue
            fetcher.store_raw(task.rawPath, result.body)
            try:
                manifest.enqueue(expander(task, json.loads(result.body)))
            except (ValueError, KeyError) as e:
                manifest.mark(task, 'failed', result.httpCode, f'expand: {e}')
                continue
            manifest.mark(task, 'done', result.httpCode, None)
            fetched += 1
            if fetched % 100 == 0:
                print(f'{fetched} fetched, at {task.meetDate} ({task.endpointType})', flush=True)


def backfill(args):
    """CLI handler: enqueue a date window and crawl it, newest date first."""
    start = datetime.strptime(args.start, '%Y-%m-%d').date()
    end = datetime.strptime(args.end, '%Y-%m-%d').date() if args.end else date.today()
    if start > end:
        print('--from must not be after --to.')
        return
    fetcher = Fetcher(args.raw, args.delay)
    with db_ops(args.db) as conn:
        manifest = Manifest(conn)
        manifest.create()
        manifest.enqueue([cards_task(d) for d in dates(start, end)])
        if args.retry_failed:
            print(f'{manifest.retry_failed()} failed rows reset to pending.')
        try:
            fetched = crawl(manifest, fetcher,
                            lambda t, p: expand(t, p, args.country, args.odds),
                            VEIKKAUS_TYPES, args.limit)
            print(f'{fetched} responses fetched into {args.raw}.')
        except CircuitOpen as e:
            print(f'Crawl paused: {e}\nRerun the same command to resume.')
        for row in manifest.counts():
            print('  {:<12} {:<8} {}'.format(*row))


def status(args):
    """CLI handler: what the manifest knows."""
    with db_ops(args.db) as conn:
        manifest = Manifest(conn)
        manifest.create()
        rows = manifest.counts()
    if not rows:
        print('Manifest is empty.')
        return
    for row in rows:
        print('{:<12} {:<8} {}'.format(*row))
