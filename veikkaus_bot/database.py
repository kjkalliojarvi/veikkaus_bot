import sqlite3
from contextlib import contextmanager


RUNNER_TABLE = """
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

START_TABLE = """
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

RACE_TABLE = """
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

@contextmanager
def db_ops(db_name='testi.db'):
    conn = sqlite3.connect(db_name)
    cur = conn.cursor()
    yield cur
    conn.commit()
    conn.close()


def createdb():
    with db_ops() as cur:
        cur.execute(RUNNER_TABLE)
        cur.execute(START_TABLE)
        cur.execute(RACE_TABLE)


def store_races(races):
    with db_ops() as cur:
        cur.executemany('INSERT INTO race VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)', races)


def store_runners(runners):
    with db_ops() as cur:
        cur.executemany('INSERT INTO runner VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)', runners)


def store_starts(starts):
    with db_ops() as cur:
        cur.executemany('INSERT INTO start VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)', starts)


"""
with db_ops('db_path') as cur:
    cur.execute('create table if not exists temp (id int, name text)')

with db_ops('db_path') as cur:
    rows = [(1, 'a'), (2, 'b'), (3, 'c')]
    cur.executemany('insert into temp values (?, ?)', rows)

with db_ops('db_path') as cur:
    print(list(cur.execute('select * from temp')))
"""
