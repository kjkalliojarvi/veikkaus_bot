import sqlite3
from contextlib import contextmanager


RUNNER_TABLE = """
    CREATE TABLE IF NOT EXISTS runner(
        runnerid INTEGER,
        name TEXT,
        sire TEXT,
        dam TEXT,
        damsire TEXT,
        birthdate TEXT,
        gender TEXT,
        coachName TEXT,
        coachNameInitials TEXT,
        onwerName TEXT,
        ownerHomeTown TEXT,
        PRIMARY KEY (runnerid) ON CONFLICT IGNORE);
"""

START_TABLE = """
    CREATE TABLE IF NOT EXISTS start(
        runnerid INTEGER,
        raceid INTEGER,
        distance INTEGER,
        driver TEXT,
        meetdate TEXT,
        racenumber INTEGER,
        firstprize INTEGER,
        starttrack INTEGER,
        trackcode TEXT,
        winodds INTEGER,
        kmtime TEXT,
        frontshoes TEXT,
        rearshoes TEXT,
        specialcart TEXT,
        coachname TEXT,
        PRIMARY KEY (runnerid, raceid) ON CONFLICT IGNORE);
"""

RACE_TABLE = """
    CREATE TABLE IF NOT EXISTS race(
        raceid INTEGER,
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
        currentRaceNumber INTEGER,
        meetDate TEXT,
        priority INTEGER
        raceType TEXT,
        trackAbbreviation TEXT,
        trackName TEXT,
        trackNumber INTEGER,
        PRIMARY KEY (raceid) ON CONFLICT IGNORE);
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

"""
with db_ops('db_path') as cur:
    cur.execute('create table if not exists temp (id int, name text)')

with db_ops('db_path') as cur:
    rows = [(1, 'a'), (2, 'b'), (3, 'c')]
    cur.executemany('insert into temp values (?, ?)', rows)

with db_ops('db_path') as cur:
    print(list(cur.execute('select * from temp')))
"""
