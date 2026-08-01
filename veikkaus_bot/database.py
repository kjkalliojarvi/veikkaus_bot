import duckdb
from contextlib import contextmanager
import json
from .get_data_json import VeikkausData


CREATE_CARD_TABLE = """
    CREATE TABLE IF NOT EXISTS card(
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

INSERT_CARD = 'INSERT OR REPLACE INTO card VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);'
CARD_KEY = (0,)  # cardId

CREATE_RUNNER_TABLE = """
    CREATE TABLE IF NOT EXISTS runner(
        runnerId BIGINT,
        horseName TEXT,
        sire TEXT,
        dam TEXT,
        damsire TEXT,
        birthdate TEXT,
        gender TEXT,
        coachName TEXT,
        onwerName TEXT,
        ownerHomeTown TEXT,
        PRIMARY KEY (runnerId));
"""

INSERT_RUNNER = 'INSERT OR REPLACE INTO runner VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);'
RUNNER_KEY = (0,)  # runnerId

CREATE_START_TABLE = """
    CREATE TABLE IF NOT EXISTS start(
        runnerId BIGINT,
        priorStartId BIGINT,
        distance BIGINT,
        driver TEXT,
        driverFullName TEXT,
        meetDate TEXT,
        raceNumber BIGINT,
        shortMeetDate TEXT,
        firstPrize BIGINT,
        startTrack BIGINT,
        result TEXT,
        trackCode TEXT,
        winOdd TEXT,
        kmTime TEXT,
        frontShoes TEXT,
        rearShoes TEXT,
        raceStartType TEXT,
        trackProfileType TEXT,
        raceSurface TEXT,
        shoesType TEXT,
        headGear TEXT,
        specialCart TEXT,
        coachName TEXT,
        startInterval BIGINT,
        PRIMARY KEY (runnerId, raceNumber, shortMeetDate));
"""
INSERT_START = 'INSERT OR REPLACE INTO start VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);'
START_KEY = (0, 6, 7)  # runnerId, raceNumber, shortMeetDate

CREATE_RACE_TABLE = """
    CREATE TABLE IF NOT EXISTS race(
        raceId BIGINT,
        cardId BIGINT,
        number BIGINT,
        distance BIGINT,
        breed TEXT,
        seriesSpecification TEXT,
        startType TEXT,
        monte BOOLEAN,
        firstPrize BIGINT,
        startTime BIGINT,
        toteResultString TEXT,
        trackProfile TEXT,
        trackAbbreviation TEXT,
        PRIMARY KEY (raceId));
"""
INSERT_RACE = 'INSERT OR REPLACE INTO race VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);'
RACE_KEY = (0,)  # raceId

CREATE_STAT_TABLE = """
    CREATE TABLE IF NOT EXISTS stat(
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
INSERT_STAT = 'INSERT OR REPLACE INTO stat VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);'
STAT_KEY = (0, 1)  # runnerId, period

CREATE_BETPERCENTAGE_TABLE = """
    CREATE TABLE IF NOT EXISTS bet_percentage(
        runnerId BIGINT,
        poolType TEXT,
        percentage BIGINT,
        PRIMARY KEY (runnerId, poolType));
"""
INSERT_BETPERCENTAGE = 'INSERT OR REPLACE INTO bet_percentage VALUES (?, ?, ?);'
BETPERCENTAGE_KEY = (0, 1)  # runnerId, poolType


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
    row for the same key across calls. This collapses duplicates *within* one
    batch as well, so the winning row is decided here rather than by DuckDB's
    per-statement conflict handling. Every table upserts with
    `INSERT OR REPLACE`, so the last row for a key wins — matching the SQL.
    """
    unique = {}
    for row in rows:
        unique[tuple(row[i] for i in key)] = row
    # DuckDB's executemany rejects an empty parameter list.
    if unique:
        cur.executemany(statement, list(unique.values()))


class Db:
    def __init__(self, db_name):
        self.db_name = db_name

    def create(self):
        with db_ops(self.db_name) as cur:
            cur.execute(CREATE_CARD_TABLE)
            cur.execute(CREATE_RUNNER_TABLE)
            cur.execute(CREATE_START_TABLE)
            cur.execute(CREATE_RACE_TABLE)
            cur.execute(CREATE_STAT_TABLE)
            cur.execute(CREATE_BETPERCENTAGE_TABLE)

    def store_cards(self, cards):
        with db_ops(self.db_name) as cur:
            _insert_many(cur, INSERT_CARD, cards, CARD_KEY)

    def store_races(self, races):
        with db_ops(self.db_name) as cur:
            _insert_many(cur, INSERT_RACE, races, RACE_KEY)


    def store_runners(self, runners):
        with db_ops(self.db_name) as cur:
            _insert_many(cur, INSERT_RUNNER, runners, RUNNER_KEY)


    def store_starts(self, starts):
        with db_ops(self.db_name) as cur:
            _insert_many(cur, INSERT_START, starts, START_KEY)

    def store_stats(self, stats):
        with db_ops(self.db_name) as cur:
            _insert_many(cur, INSERT_STAT, stats, STAT_KEY)

    def store_betpercentages(self, betpercentages):
        with db_ops(self.db_name) as cur:
            _insert_many(cur, INSERT_BETPERCENTAGE, betpercentages, BETPERCENTAGE_KEY)

    def store_data(self, data: VeikkausData):
        records = data.to_json()
        self.store_cards(records['cards'])
        self.store_races(records['races'])
        self.store_runners(records['runners'])
        self.store_starts(records['starts'])
        self.store_stats(records['stats'])
        self.store_betpercentages(records['betpercentages'])

    def store_file(self, jsonfile: str):
        with open(jsonfile, 'r') as openfile:
            json_object = json.load(openfile)
        self.store_cards(json_object.get('cards', []))
        self.store_races(json_object['races'])
        self.store_runners(json_object['runners'])
        self.store_starts(json_object['starts'])
        self.store_stats(json_object.get('stats', []))
        self.store_betpercentages(json_object.get('betpercentages', []))

    def query_runner(self, name):
        """All previous starts of a runner by horse name, oldest first."""
        with db_ops(self.db_name) as cur:
            res = cur.execute(
                """SELECT s.* FROM start s
                   JOIN runner r ON r.runnerId = s.runnerId
                   WHERE r.horseName = ?
                   ORDER BY s.meetDate""", (name,)).fetchall()
        return res

    def query_starts(self, runner_id: int):
        """All previous starts of a runner by runnerId, oldest first."""
        with db_ops(self.db_name) as cur:
            res = cur.execute(
                'SELECT * FROM start WHERE runnerId = ? ORDER BY meetDate',
                (runner_id,)).fetchall()
        return res

    def query_stats(self, name):
        """A runner's statistics by horse name, one row per period
        (currentYear/previousYear/total)."""
        with db_ops(self.db_name) as cur:
            res = cur.execute(
                """SELECT s.* FROM stat s
                   JOIN runner r ON r.runnerId = s.runnerId
                   WHERE r.horseName = ?
                   ORDER BY s.period""", (name,)).fetchall()
        return res

    def query_betpercentages(self, name):
        """A runner's bet percentages by horse name, one row per pool type.

        Percentages are hundredths of a percent (939 = 9.39 %)."""
        with db_ops(self.db_name) as cur:
            res = cur.execute(
                """SELECT b.* FROM bet_percentage b
                   JOIN runner r ON r.runnerId = b.runnerId
                   WHERE r.horseName = ?
                   ORDER BY b.poolType""", (name,)).fetchall()
        return res


DEFAULT_DB = 'veikkaus_data.duckdb'


def load(args):
    """CLI handler: load one or more saved JSON dumps into a DuckDB database.

    Creates the tables if they don't exist, then loads each file. Each file
    is wrapped so a bad dump doesn't abort the rest of the batch.
    """
    db = Db(getattr(args, 'db', None) or DEFAULT_DB)
    db.create()
    for jsonfile in args.jsonfile:
        try:
            db.store_file(jsonfile)
            print(f'{jsonfile} loaded into {db.db_name}.')
        except Exception as e:
            print(f'{jsonfile}: {e}')


"""
with db_ops('db_path') as cur:
    cur.execute('create table if not exists temp (id int, name text)')

with db_ops('db_path') as cur:
    rows = [(1, 'a'), (2, 'b'), (3, 'c')]
    cur.executemany('insert into temp values (?, ?)', rows)

with db_ops('db_path') as cur:
    print(cur.execute('select * from temp').fetchall())
"""
