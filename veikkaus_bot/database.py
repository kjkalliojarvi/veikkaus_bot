import sqlite3
from contextlib import contextmanager


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
        coachNameInitials TEXT,
        onwerName TEXT,
        ownerHomeTown TEXT,
        PRIMARY KEY (runnerId) ON CONFLICT REPLACE);
"""

INSERT_RUNNER = 'INSERT INTO runner VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);'

CREATE_START_TABLE = """
    CREATE TABLE IF NOT EXISTS start(
        runnerId INTEGER,
        priorStartId INTEGER,
        distance INTEGER,
        driver TEXT,
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
        PRIMARY KEY (runnerId, priorStartId) ON CONFLICT REPLACE);
"""
INSERT_START = 'INSERT INTO start VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);'

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
        trackSurface TEXT,
        country TEXT,
        trackAbbreviation TEXT,
        trackName TEXT,
        trackNumber INTEGER,
        PRIMARY KEY (raceId) ON CONFLICT REPLACE);
"""
INSERT_RACE = 'INSERT INTO race VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);'


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

    def store_races(self, races):
        with db_ops(self.db_name) as cur:
            cur.executemany(INSERT_RACE, races)


    def store_runners(self, runners):
        with db_ops(self.db_name) as cur:
            cur.executemany(INSERT_RUNNER, runners)


    def store_starts(self, starts):
        with db_ops(self.db_name) as cur:
            cur.executemany(INSERT_START, starts)

    def query_runner(self, name):
        with db_ops(self.db_name) as cur:
            res = list(cur.execute("SELECT runnerId FROM runner WHERE horseName='%s'" %  name))
            res2 = list(cur.execute("SELECT * FROM start WHERE runnerId='%d'" % res[0]))
        return res2


"""
with db_ops('db_path') as cur:
    cur.execute('create table if not exists temp (id int, name text)')

with db_ops('db_path') as cur:
    rows = [(1, 'a'), (2, 'b'), (3, 'c')]
    cur.executemany('insert into temp values (?, ?)', rows)

with db_ops('db_path') as cur:
    print(list(cur.execute('select * from temp')))
"""
