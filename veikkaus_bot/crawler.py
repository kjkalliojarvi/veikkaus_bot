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
        parsedAt TEXT,           -- when this payload was last loaded; NULL = never
        PRIMARY KEY (endpointType, entityId));
"""

# Added after the table first shipped; CREATE TABLE IF NOT EXISTS will not
# widen an existing manifest, and IF NOT EXISTS makes this a no-op on one that
# already has it.
ADD_MANIFEST_COLUMNS = (
    'ALTER TABLE archive.manifest ADD COLUMN IF NOT EXISTS parsedAt TEXT;',
)

# Never clobbers a row that is already done — re-enqueueing is a no-op.
INSERT_TASK = """
    INSERT OR IGNORE INTO archive.manifest
    VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', NULL, NULL, 0, NULL, NULL);
"""


class Manifest:
    """The fetch ledger. Wraps an open DuckDB connection."""

    def __init__(self, conn):
        self.conn = conn

    def create(self):
        self.conn.execute(CREATE_SCHEMA)
        self.conn.execute(CREATE_MANIFEST_TABLE)
        for statement in ADD_MANIFEST_COLUMNS:
            self.conn.execute(statement)

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
        """Record the outcome of a fetch, and forget that it was ever parsed.

        Clearing `parsedAt` is what makes a re-fetch authoritative: the payload
        on disk has just been replaced, so whatever was loaded from it is stale
        by definition. Leaving it to the `parsedAt < fetchedAt` comparison
        alone would lose a re-fetch that landed in the same second as the parse
        before it, both stamps being second-resolution.
        """
        self.conn.execute(
            """UPDATE archive.manifest
               SET status = ?, httpCode = ?, fetchedAt = ?, attempts = attempts + 1,
                   error = ?, parsedAt = NULL
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

    def done(self, endpoint_type: str, unparsed_only: bool = False) -> list[Task]:
        """Every successfully fetched row of one endpoint type, oldest date first.

        `unparsed_only` narrows it to what a parse still owes work on. A
        re-fetch clears `parsedAt` outright (see `mark`), so the NULL branch is
        what normally catches a row put back through the crawl by
        `reset_window()`. The timestamp comparison is the backstop for a
        `fetchedAt` that moved some other way.
        """
        unparsed = 'AND (parsedAt IS NULL OR parsedAt < fetchedAt)' if unparsed_only else ''
        rows = self.conn.execute(
            f"""SELECT endpointType, entityId, url, rawPath, meetDate, cardId, stage
                FROM archive.manifest
                WHERE endpointType = ? AND status = 'done' {unparsed}
                ORDER BY meetDate ASC""", (endpoint_type,)).fetchall()
        return [Task(*row) for row in rows]

    def mark_parsed(self, tasks: list[Task]):
        """Record that these payloads have been loaded.

        Called once per phase, *after* its final flush: a crash between the two
        re-does the phase, which is safe because every upsert is idempotent,
        whereas stamping first would lose the rows silently.
        """
        if tasks:
            now = datetime.now().isoformat(timespec='seconds')
            self.conn.executemany(
                """UPDATE archive.manifest SET parsedAt = ?
                   WHERE endpointType = ? AND entityId = ?""",
                [(now, t.endpointType, t.entityId) for t in tasks])

    def reset_window(self, first: str, last: str, types: tuple) -> int:
        """Make a date range fetchable again. Returns the number of rows reset.

        The recovery for a card crawled before its racing was final: a `done`
        task is otherwise never fetched again, and a race that has not run
        still answers with HTTP 200 and an empty result list.

        Restricted to one source's endpoint types, so refetching a Veikkaus
        window cannot disturb the Heppa rows sharing this manifest. Rows with
        no `meetDate` — the per-horse registry records — are never in a date
        range and are correctly left alone.

        `missing` and `failed` are reset alongside `done`: recovering a
        mis-timed crawl is the whole point, and 'nothing there' is exactly what
        an early fetch looks like.
        """
        placeholders = ', '.join('?' * len(types))
        rows = self.conn.execute(
            f"""UPDATE archive.manifest SET status = 'pending'
                WHERE meetDate BETWEEN ? AND ?
                  AND endpointType IN ({placeholders})
                  AND status <> 'pending'
                RETURNING 1""", (first, last, *types)).fetchall()
        return len(rows)


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
                # Most tasks are placed by their meet date; a horse has none,
                # so fall back to the entity it names.
                where = task.meetDate or task.entityId
                print(f'{fetched} fetched, at {where} ({task.endpointType})', flush=True)


def refetch_window(args, manifest: Manifest, types: tuple):
    """Apply --refetch-from/--refetch-to, if given. Shared by both sources.

    Deliberately a separate window from --from/--to: the recommended update
    cycle pins --from at the start of the archive, so a flag reusing that
    window would quietly re-crawl years.
    """
    if not getattr(args, 'refetch_start', None):
        return
    first = args.refetch_start
    last = args.refetch_end or first
    if first > last:
        print('--refetch-from must not be after --refetch-to.')
        return
    count = manifest.reset_window(first, last, types)
    print(f'{count} manifest rows in {first}..{last} reset to pending.')


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
        refetch_window(args, manifest, VEIKKAUS_TYPES)
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
