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
from .fetcher import CircuitOpen, Fetcher, read_raw


# Stages order the work within one meet date: cards before races before the
# per-race payloads. next_pending() sorts by (meetDate DESC, stage ASC).
CARDS_DATE, RACES, RUNNERS, RESULTS, POOLS, ODDS, LEG_ODDS = range(7)

# The manifest is shared with the Heppa crawl (heppa.py), which uses its own
# endpoint types and its own stage range. next_pending() filters on these so a
# run of one source never fetches the other's rows — different host, different
# rate limit, different circuit breaker.
VEIKKAUS_TYPES = ('cards_date', 'races', 'runners', 'results', 'pools', 'odds',
                  'leg_odds')

# The multi-leg pools' own opt-in, for the same reason the Heppa crawls have
# theirs: `leg-percentages` is driven by what the raw zone already holds rather
# than by a date window, so it must not drain a half-finished `backfill`.
LEG_TYPES = ('leg_odds',)

# The pool types whose odds payload is per (leg, runner) rather than per
# runner — the T-pools, Toto4 through Toto86. Named explicitly rather than
# derived: every pool of these types carries a `hasCombinations` field and no
# single-race pool has one (verified over all 26,348 archived pools payloads:
# present on 14,261 T-pool rows, absent on every VOI/SIJ/KAK/DUO/TRO/EKS one),
# but it arrives *false* on 1,384 of them, so it is a live-state flag and not
# an identity. `leg-percentages` reports a type carrying it that this tuple
# does not name, which turns a new Veikkaus product into a printed line rather
# than data silently missing. T86 has not been observed in the archive.
LEG_POOL_TYPES = ('T4', 'T5', 'T64', 'T65', 'T75', 'T86')

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


def _pool_odds_task(kind: str, stage: int, meet_date: str, card_id: int,
                    pool_id: int) -> Task:
    """One pool's odds payload.

    The same endpoint serves the win pool's per-runner odds and a multi-leg
    pool's per-leg percentages; the endpoint type is what separates them, so
    each can be crawled, limited and re-fetched on its own. Pool ids are unique
    across types, so the two never collide on the manifest or in the raw zone.
    """
    return Task(kind, str(pool_id), f'/pool/{pool_id}/odds',
                f'{meet_date}/card_{card_id}/pool_{pool_id}_odds.json.gz',
                meet_date, card_id, stage)


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
        # Only the win pool carries a per-runner odd for every starter; the
        # multi-leg pools carry the per-leg betting percentages, which exist
        # nowhere else in the API (see LEG_POOL_TYPES).
        children = []
        for pool in collection:
            pool_type = pool.get('poolType')
            if pool_type == 'VOI':
                children.append(_pool_odds_task('odds', ODDS, task.meetDate,
                                                task.cardId, pool['poolId']))
            elif pool_type in LEG_POOL_TYPES:
                # A multi-leg pool is listed by every one of its legs, so this
                # enqueues it once per leg race. All the legs are on one card,
                # so the duplicates agree on date and card, and INSERT OR
                # IGNORE keeps the first.
                children.append(_pool_odds_task('leg_odds', LEG_ODDS, task.meetDate,
                                                task.cardId, pool['poolId']))
        return children
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


def _leg_pool_tasks(manifest: Manifest, raw_root: str) -> tuple[list[Task], dict]:
    """Every multi-leg pool the archived `pools` payloads name.

    Driven by the raw zone rather than by a date window, because the pool ids
    only exist inside those payloads and a `pools` task that is already `done`
    is never fetched again — so the T-pool children `expand()` now emits were
    never enqueued for a card crawled before this existed. Reading the archive
    back is also the cheap way round: 26k local files against one request per
    card, and the answer is the same.

    Returns the tasks, plus a count per pool type that looks multi-leg but is
    not in LEG_POOL_TYPES — see that tuple for why it is reported and not
    guessed at.
    """
    tasks, seen, unknown = [], set(), {}
    for task in manifest.done('pools'):
        payload = read_raw(raw_root, task.rawPath)
        if not isinstance(payload, dict):
            continue
        for pool in payload.get('collection', []):
            pool_type, pool_id = pool.get('poolType'), pool.get('poolId')
            # `seen` covers the reported types too, so both counts are per
            # pool: a multi-leg pool is listed once per leg either way.
            if pool_id is None or pool_id in seen:
                continue
            if pool_type in LEG_POOL_TYPES:
                seen.add(pool_id)
                tasks.append(_pool_odds_task('leg_odds', LEG_ODDS, task.meetDate,
                                             task.cardId, pool_id))
            elif pool.get('hasCombinations') is not None:
                seen.add(pool_id)
                unknown[pool_type] = unknown.get(pool_type, 0) + 1
    return tasks, unknown


def backfill_leg_percentages(args):
    """CLI handler: crawl the multi-leg pools' per-leg betting percentages.

    The one Veikkaus figure that is genuinely retrospective. Odds and betting
    support otherwise have to be captured live — `stats` and `prevStarts` ride
    along in a runners payload only while the card is current — but a T-pool's
    odds payload keeps its closing percentages indefinitely, verified back to
    the first day of the archive. So this needs no live cycle: it fetches
    whatever the crawled `pools` payloads name and skips what it already has.

    Driven by the archive, like the three Heppa crawls, so it follows a
    `backfill --odds` run. Going forward `expand()` enqueues these itself, and
    re-running this is a no-op over the pools already fetched.
    """
    fetcher = Fetcher(args.raw, args.delay)
    with db_ops(args.db) as conn:
        manifest = Manifest(conn)
        manifest.create()
        tasks, unknown = _leg_pool_tasks(manifest, args.raw)
        if not tasks:
            print('No multi-leg pools in the raw zone yet.\n'
                  'They are named by the pools payloads, which only a '
                  '`backfill --odds` run fetches.')
            return
        print(f'{len(tasks)} multi-leg pools known; already-fetched ones are skipped.')
        for pool_type, count in sorted(unknown.items()):
            print(f'  {count} {pool_type} pools look multi-leg (they carry '
                  'hasCombinations) but are not in\n  crawler.LEG_POOL_TYPES, so '
                  'nothing was fetched for them.')
        manifest.enqueue(tasks)
        refetch_window(args, manifest, LEG_TYPES)
        if args.retry_failed:
            print(f'{manifest.retry_failed(LEG_TYPES)} failed rows reset to pending.')
        try:
            # A pool's odds payload is a leaf: it has no children.
            fetched = crawl(manifest, fetcher, lambda t, p: [], LEG_TYPES, args.limit)
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
