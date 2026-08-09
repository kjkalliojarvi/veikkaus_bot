"""The crawl-derived past-performance dataset (strategy §5).

The tables live in a DuckDB schema named `archive`, keeping the crawl-derived
dataset distinct from anything else the database file might hold.

Column names mirror the API's camelCase. Two DuckDB rules hold throughout:
every id and epoch-millisecond value is `BIGINT` (`INTEGER` is 32-bit and
`raceId`/`startTime` overflow it), and every table upserts with
`INSERT OR REPLACE` — the SQLite `ON CONFLICT` clause inside a `PRIMARY KEY`
definition is not supported.
"""
from contextlib import contextmanager

import duckdb


DEFAULT_DB = 'veikkaus_data.duckdb'

CREATE_SCHEMA = 'CREATE SCHEMA IF NOT EXISTS archive;'

CREATE_CARD_TABLE = """
    CREATE TABLE IF NOT EXISTS archive.card(
        cardId BIGINT,
        country TEXT,
        meetDate TEXT,
        trackAbbreviation TEXT,
        trackName TEXT,
        trackNumber BIGINT,
        raceType TEXT,
        firstRaceStart BIGINT,
        lunchRaces BOOLEAN,
        mainPerformance BOOLEAN,
        cancelled BOOLEAN,
        PRIMARY KEY (cardId));
"""
INSERT_CARD = 'INSERT OR REPLACE INTO archive.card VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);'
CARD_KEY = (0,)  # cardId

CREATE_RACE_TABLE = """
    CREATE TABLE IF NOT EXISTS archive.race(
        raceId BIGINT,
        cardId BIGINT,
        number BIGINT,
        startTime BIGINT,
        distance BIGINT,
        startType TEXT,
        monte BOOLEAN,
        firstPrize BIGINT,
        breed TEXT,
        seriesSpecification TEXT,
        raceStatus TEXT,
        raceRider TEXT,
        trackProfile TEXT,
        toteResultString TEXT,
        intermediateTimesString TEXT,
        PRIMARY KEY (raceId));
"""
INSERT_RACE = ('INSERT OR REPLACE INTO archive.race '
               'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);')
RACE_KEY = (0,)  # raceId

CREATE_HORSE_TABLE = """
    CREATE TABLE IF NOT EXISTS archive.horse(
        horseKey TEXT,
        horseName TEXT,
        birthYear BIGINT,
        gender TEXT,
        sire TEXT,
        dam TEXT,
        damSire TEXT,
        PRIMARY KEY (horseKey));
"""
INSERT_HORSE = 'INSERT OR REPLACE INTO archive.horse VALUES (?, ?, ?, ?, ?, ?, ?);'
HORSE_KEY = (0,)  # horseKey

# One row per (race, horse) — one past-performance line.
# placement/kmTime/winOddsFinal stay NULL outside the paid places: the API only
# publishes finishing detail for the runners a pool paid out on (see §2b).
# The plan calls the column `placing`, which DuckDB's Postgres-derived parser
# reserves (OVERLAY ... PLACING ...), so it is `placement` here.
CREATE_START_TABLE = """
    CREATE TABLE IF NOT EXISTS archive.start(
        raceId BIGINT,
        startNumber BIGINT,
        runnerId BIGINT,
        horseKey TEXT,
        horseName TEXT,
        driverName TEXT,
        coachName TEXT,
        ownerName TEXT,
        ownerHomeTown TEXT,
        startTrack BIGINT,
        distance BIGINT,
        frontShoes TEXT,
        rearShoes TEXT,
        specialCart TEXT,
        handicapRating BIGINT,
        scratched BOOLEAN,
        careerWinnings BIGINT,   -- runner.prize: career earnings, not this race's purse
        placement BIGINT,
        kmTime TEXT,
        kmTimeMs BIGINT,
        autoStart BOOLEAN,
        winOddsFinal BIGINT,
        PRIMARY KEY (raceId, startNumber));
"""
INSERT_START = ('INSERT OR REPLACE INTO archive.start '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);')
START_KEY = (0, 1)  # raceId, startNumber

CREATE_ODDS_TABLE = """
    CREATE TABLE IF NOT EXISTS archive.odds_snapshot(
        poolId BIGINT,
        raceId BIGINT,
        startNumber BIGINT,
        capturedAt BIGINT,
        poolType TEXT,
        probable BIGINT,
        amount BIGINT,
        scratched BOOLEAN,
        PRIMARY KEY (poolId, startNumber, capturedAt));
"""
INSERT_ODDS = 'INSERT OR REPLACE INTO archive.odds_snapshot VALUES (?, ?, ?, ?, ?, ?, ?, ?);'
ODDS_KEY = (0, 2, 3)  # poolId, startNumber, capturedAt

# Career/season form and betting support ride along inside a runners payload,
# but only while the card is current — both are empty for historical cards.
CREATE_STAT_TABLE = """
    CREATE TABLE IF NOT EXISTS archive.stat(
        runnerId BIGINT,
        period TEXT,
        year TEXT,
        record1 TEXT,
        record2 TEXT,
        starts BIGINT,
        position1 BIGINT,
        position2 BIGINT,
        position3 BIGINT,
        places BIGINT,
        winMoney BIGINT,
        gallopPercent BIGINT,
        disqualificationPercent BIGINT,
        placementPercent BIGINT,
        winningPercent BIGINT,
        PRIMARY KEY (runnerId, period));
"""
INSERT_STAT = ('INSERT OR REPLACE INTO archive.stat '
               'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);')
STAT_KEY = (0, 1)  # runnerId, period

CREATE_BETPERCENTAGE_TABLE = """
    CREATE TABLE IF NOT EXISTS archive.bet_percentage(
        runnerId BIGINT,
        poolType TEXT,
        percentage BIGINT,
        PRIMARY KEY (runnerId, poolType));
"""
INSERT_BETPERCENTAGE = 'INSERT OR REPLACE INTO archive.bet_percentage VALUES (?, ?, ?);'
BETPERCENTAGE_KEY = (0, 1)  # runnerId, poolType

# A horse's earlier starts, as reported inside a runners payload. This is the
# only source in the API that carries the *whole* field's finishing detail
# (`archive.start` stops at the paid places), so it is the backbone of the
# past-performance history. Keyed on priorStartId, which is globally unique, so
# the same start re-reported on every later race of that horse collapses to one
# row instead of one per reporting runner.
CREATE_PREVSTART_TABLE = """
    CREATE TABLE IF NOT EXISTS archive.prev_start(
        priorStartId BIGINT,
        horseKey TEXT,
        meetDate TEXT,           -- local meet date, from shortMeetDate
        meetDateTime TEXT,       -- raw timestamp: midnight Finnish time in UTC
        trackCode TEXT,
        trackName TEXT,
        raceNumber BIGINT,
        distance BIGINT,
        startTrack BIGINT,
        driver TEXT,
        driverFullName TEXT,
        firstPrize BIGINT,
        result TEXT,             -- raw code: '1'..'14', kl, k, hpl, hll, hlo4...
        placement BIGINT,        -- the numeric codes only; NULL for the rest
        kmTime TEXT,
        kmTimeMs BIGINT,
        autoStart BOOLEAN,
        winOdd BIGINT,           -- hundredths, as sent (a digit string)
        frontShoes TEXT,
        rearShoes TEXT,
        shoesType TEXT,
        headGear TEXT,
        specialCart TEXT,
        raceStartType TEXT,
        raceRiderType TEXT,
        trackProfileType TEXT,
        raceSurface TEXT,
        resultsAvailable BOOLEAN,
        startInterval BIGINT,    -- days since the horse's previous known start
        coachName TEXT,          -- trainer at *this* start, from archive.start
        PRIMARY KEY (priorStartId));
"""
INSERT_PREVSTART = ('INSERT OR REPLACE INTO archive.prev_start VALUES '
                    '(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '
                    '?, ?, ?, ?, ?, ?, ?);')
PREVSTART_KEY = (0,)  # priorStartId

# The prev-start block names a driver but never a trainer, so the trainer at
# the time of a past start has to come from the archive itself: the crawled
# `archive.start` row for that same race, which recorded the trainer as of that
# day. Rows outside the crawl window stay NULL — there is no source for them,
# and the reporting runner's `coachName` is emphatically not one. That is the
# trainer at a *later* race, and under priorStartId dedup it would be whichever
# report happened to land last.
#
# Matched on horse + meet date + race number. Track is deliberately not part of
# the join: `prev_start.trackCode` and `card.trackAbbreviation` are not verified
# to share a vocabulary at every track, and a horse cannot be in two places on
# one day anyway.
RECOMPUTE_PREV_START_COACH = """
    UPDATE archive.prev_start AS p
    SET coachName = c.coachName
    FROM (SELECT s.horseKey, ca.meetDate, r.number AS raceNumber,
                 min(s.coachName) AS coachName
          FROM archive.start s
          JOIN archive.race r ON r.raceId = s.raceId
          JOIN archive.card ca ON ca.cardId = r.cardId
          WHERE s.coachName IS NOT NULL
          GROUP BY 1, 2, 3) AS c
    WHERE c.horseKey = p.horseKey
      AND c.meetDate = p.meetDate
      AND c.raceNumber = p.raceNumber;
"""

# Days since the horse's previous start, filled in after loading rather than
# per record. A reported prevStarts list is a truncated window (~8 entries), so
# the gap in front of its oldest entry is unknowable from that list alone — and
# as the window slides, whichever report lands last would decide the value.
# Computing it over the whole accumulated table instead makes it deterministic
# and re-derives it correctly as new dates are crawled in.
#
# A horse's first start in the archive has no predecessor. It keeps the old
# pipeline's sentinel — days since the 1970 epoch, so ~20000 — rather than
# NULL, which is a value to filter out in analysis, not a real interval.
RECOMPUTE_START_INTERVAL = """
    UPDATE archive.prev_start AS p
    SET startInterval = g.gap
    FROM (SELECT priorStartId,
                 coalesce(
                     date_diff('day',
                               lag(CAST(meetDate AS DATE)) OVER (PARTITION BY horseKey
                                                                 ORDER BY meetDate, priorStartId),
                               CAST(meetDate AS DATE)),
                     date_diff('day', DATE '1970-01-01', CAST(meetDate AS DATE))) AS gap
          FROM archive.prev_start
          WHERE meetDate IS NOT NULL) AS g
    WHERE g.priorStartId = p.priorStartId;
"""

CREATE_INDEXES = (
    'CREATE INDEX IF NOT EXISTS idx_start_horse ON archive.start(horseKey);',
    'CREATE INDEX IF NOT EXISTS idx_race_card ON archive.race(cardId);',
    'CREATE INDEX IF NOT EXISTS idx_prevstart_horse ON archive.prev_start(horseKey);',
)


@contextmanager
def db_ops(db_name):
    conn = duckdb.connect(db_name)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _insert_many(cur, statement, rows, key):
    """Insert rows, at most one per primary key.

    Every table has a primary key, so the database already refuses a second
    row for the same key across calls, files and runs. This collapses
    duplicates *within* one batch as well, so the winning row is decided here
    rather than by DuckDB's per-statement conflict handling — last row for a
    key wins, matching `INSERT OR REPLACE`. Keep each `*_KEY` in sync with its
    table's `PRIMARY KEY` when columns move.
    """
    unique = {}
    for row in rows:
        unique[tuple(row[i] for i in key)] = row
    # DuckDB's executemany rejects an empty parameter list.
    if unique:
        cur.executemany(statement, list(unique.values()))


def create(conn):
    conn.execute(CREATE_SCHEMA)
    for statement in (CREATE_CARD_TABLE, CREATE_RACE_TABLE, CREATE_HORSE_TABLE,
                      CREATE_START_TABLE, CREATE_ODDS_TABLE, CREATE_STAT_TABLE,
                      CREATE_BETPERCENTAGE_TABLE, CREATE_PREVSTART_TABLE,
                      *CREATE_INDEXES):
        conn.execute(statement)


class ArchiveDb:
    """Batched upserts into the archive schema."""

    def __init__(self, conn):
        self.conn = conn

    def store_cards(self, rows):
        _insert_many(self.conn, INSERT_CARD, rows, CARD_KEY)

    def store_races(self, rows):
        _insert_many(self.conn, INSERT_RACE, rows, RACE_KEY)

    def store_horses(self, rows):
        _insert_many(self.conn, INSERT_HORSE, rows, HORSE_KEY)

    def store_starts(self, rows):
        _insert_many(self.conn, INSERT_START, rows, START_KEY)

    def store_odds(self, rows):
        _insert_many(self.conn, INSERT_ODDS, rows, ODDS_KEY)

    def store_stats(self, rows):
        _insert_many(self.conn, INSERT_STAT, rows, STAT_KEY)

    def store_betpercentages(self, rows):
        _insert_many(self.conn, INSERT_BETPERCENTAGE, rows, BETPERCENTAGE_KEY)

    def store_prevstarts(self, rows):
        _insert_many(self.conn, INSERT_PREVSTART, rows, PREVSTART_KEY)

    def recompute_start_intervals(self):
        self.conn.execute(RECOMPUTE_START_INTERVAL)

    def recompute_prev_start_coaches(self):
        self.conn.execute(RECOMPUTE_PREV_START_COACH)


def query_horse(db_name: str, name: str, before: str | None = None):
    """Past performances of a horse, oldest first — the §5 join.

    `before` (yyyy-mm-dd) keeps the query time-aware: only starts a model could
    have known about before that meet date.
    """
    sql = """SELECT c.meetDate, c.trackAbbreviation, r.number, r.distance, r.startType,
                    s.startNumber, s.startTrack, s.driverName, s.placement, s.kmTime,
                    s.kmTimeMs, s.winOddsFinal, s.careerWinnings
             FROM archive.start s
             JOIN archive.race r ON r.raceId = s.raceId
             JOIN archive.card c ON c.cardId = r.cardId
             JOIN archive.horse h ON h.horseKey = s.horseKey
             WHERE h.horseName = ?"""
    params = [name]
    if before:
        sql += ' AND c.meetDate < ?'
        params.append(before)
    with db_ops(db_name) as conn:
        return conn.execute(sql + ' ORDER BY c.meetDate', params).fetchall()


def query_prev_starts(db_name: str, name: str, before: str | None = None):
    """A horse's reported career line, oldest first.

    Wider than `query_horse`: `prev_start` carries a finishing position for the
    whole field, not just the paid places, so this is the past-performance
    history to build features from. `before` (yyyy-mm-dd) keeps it time-aware.
    """
    sql = """SELECT p.meetDate, p.trackCode, p.raceNumber, p.distance, p.startTrack,
                    p.driver, p.result, p.placement, p.kmTime, p.kmTimeMs,
                    p.autoStart, p.winOdd, p.firstPrize, p.startInterval, p.coachName
             FROM archive.prev_start p
             JOIN archive.horse h ON h.horseKey = p.horseKey
             WHERE h.horseName = ?"""
    params = [name]
    if before:
        sql += ' AND p.meetDate < ?'
        params.append(before)
    with db_ops(db_name) as conn:
        return conn.execute(sql + ' ORDER BY p.meetDate', params).fetchall()
