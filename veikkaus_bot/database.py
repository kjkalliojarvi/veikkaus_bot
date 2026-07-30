import sqlite3
from contextlib import contextmanager
import json
from .get_data_json import VeikkausData


CREATE_RUNNER_TABLE = """
    CREATE TABLE IF NOT EXISTS runner(
        runnerId INTEGER,
        horseName TEXT,
        sire TEXT,
        dam TEXT,
        damsire TEXT,
        birthdate TEXT,
        gender TEXT,
        coachName TEXT,
        onwerName TEXT,
        ownerHomeTown TEXT,
        PRIMARY KEY (runnerId) ON CONFLICT REPLACE);
"""

INSERT_RUNNER = 'INSERT INTO runner VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);'

CREATE_START_TABLE = """
    CREATE TABLE IF NOT EXISTS start(
        runnerId INTEGER,
        priorStartId INTEGER,
        distance INTEGER,
        driver TEXT,
        driverFullName TEXT,
        meetDate TEXT,
        raceNumber INTEGER,
        shortMeetDate TEXT,
        firstPrize INTEGER,
        startTrack INTEGER,
        result TEXT,
        trackCode TEXT,
        winOdd INTEGER,
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
        startInterval INTEGER,
        PRIMARY KEY (runnerId, raceNumber, shortMeetDate) ON CONFLICT REPLACE);
"""
INSERT_START = 'INSERT INTO start VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);'

CREATE_RACE_TABLE = """
    CREATE TABLE IF NOT EXISTS race(
        raceId INTEGER,
        cardId INTEGER,
        number INTEGER,
        distance INTEGER,
        breed TEXT,
        seriesSpecification TEXT,
        startType TEXT,
        monte BOOLEAN,
        firstPrize INTEGER,
        startTime INTEGER,
        toteResultString TEXT,
        trackProfile TEXT,
        country TEXT,
        trackAbbreviation TEXT,
        trackName TEXT,
        trackNumber INTEGER,
        PRIMARY KEY (raceId) ON CONFLICT IGNORE);
"""
INSERT_RACE = 'INSERT INTO race VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);'

CREATE_STAT_TABLE = """
    CREATE TABLE IF NOT EXISTS stat(
        runnerId INTEGER,
        period TEXT,
        year TEXT,
        record1 TEXT,
        record2 TEXT,
        starts INTEGER,
        position1 INTEGER,
        position2 INTEGER,
        position3 INTEGER,
        places INTEGER,
        winMoney INTEGER,
        gallopPercent INTEGER,
        disqualificationPercent INTEGER,
        placementPercent INTEGER,
        winningPercent INTEGER,
        PRIMARY KEY (runnerId, period) ON CONFLICT REPLACE);
"""
INSERT_STAT = 'INSERT INTO stat VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);'

CREATE_BETPERCENTAGE_TABLE = """
    CREATE TABLE IF NOT EXISTS bet_percentage(
        runnerId INTEGER,
        poolType TEXT,
        percentage INTEGER,
        PRIMARY KEY (runnerId, poolType) ON CONFLICT REPLACE);
"""
INSERT_BETPERCENTAGE = 'INSERT INTO bet_percentage VALUES (?, ?, ?);'


@contextmanager
def db_ops(db_name):
    conn = sqlite3.connect(db_name)
    cur = conn.cursor()
    yield cur
    conn.commit()
    conn.close()

class Db:
    def __init__(self, db_name):
        self.db_name = db_name

    def create(self):
        with db_ops(self.db_name) as cur:
            cur.execute(CREATE_RUNNER_TABLE)
            cur.execute(CREATE_START_TABLE)
            cur.execute(CREATE_RACE_TABLE)
            cur.execute(CREATE_STAT_TABLE)
            cur.execute(CREATE_BETPERCENTAGE_TABLE)

    def store_races(self, races):
        with db_ops(self.db_name) as cur:
            cur.executemany(INSERT_RACE, races)


    def store_runners(self, runners):
        with db_ops(self.db_name) as cur:
            cur.executemany(INSERT_RUNNER, runners)


    def store_starts(self, starts):
        with db_ops(self.db_name) as cur:
            cur.executemany(INSERT_START, starts)

    def store_stats(self, stats):
        with db_ops(self.db_name) as cur:
            cur.executemany(INSERT_STAT, stats)

    def store_betpercentages(self, betpercentages):
        with db_ops(self.db_name) as cur:
            cur.executemany(INSERT_BETPERCENTAGE, betpercentages)

    def store_data(self, data: VeikkausData):
        records = data.to_json()
        self.store_races(records['races'])
        self.store_runners(records['runners'])
        self.store_starts(records['starts'])
        self.store_stats(records['stats'])
        self.store_betpercentages(records['betpercentages'])

    def store_file(self, jsonfile: str):
        with open(jsonfile, 'r') as openfile:
            json_object = json.load(openfile)
        self.store_races(json_object['races'])
        self.store_runners(json_object['runners'])
        self.store_starts(json_object['starts'])
        self.store_stats(json_object.get('stats', []))
        self.store_betpercentages(json_object.get('betpercentages', []))

    def query_runner(self, name):
        """All previous starts of a runner by horse name, oldest first."""
        with db_ops(self.db_name) as cur:
            res = list(cur.execute(
                """SELECT s.* FROM start s
                   JOIN runner r ON r.runnerId = s.runnerId
                   WHERE r.horseName = ?
                   ORDER BY s.meetDate""", (name,)))
        return res

    def query_starts(self, runner_id: int):
        """All previous starts of a runner by runnerId, oldest first."""
        with db_ops(self.db_name) as cur:
            res = list(cur.execute(
                'SELECT * FROM start WHERE runnerId = ? ORDER BY meetDate',
                (runner_id,)))
        return res

    def query_stats(self, name):
        """A runner's statistics by horse name, one row per period
        (currentYear/previousYear/total)."""
        with db_ops(self.db_name) as cur:
            res = list(cur.execute(
                """SELECT s.* FROM stat s
                   JOIN runner r ON r.runnerId = s.runnerId
                   WHERE r.horseName = ?
                   ORDER BY s.period""", (name,)))
        return res

    def query_betpercentages(self, name):
        """A runner's bet percentages by horse name, one row per pool type.

        Percentages are hundredths of a percent (939 = 9.39 %)."""
        with db_ops(self.db_name) as cur:
            res = list(cur.execute(
                """SELECT b.* FROM bet_percentage b
                   JOIN runner r ON r.runnerId = b.runnerId
                   WHERE r.horseName = ?
                   ORDER BY b.poolType""", (name,)))
        return res


DEFAULT_DB = 'veikkaus_data.db'


def load(args):
    """CLI handler: load one or more saved JSON dumps into a SQLite database.

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
    print(list(cur.execute('select * from temp')))
"""
