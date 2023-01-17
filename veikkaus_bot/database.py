import sqlite3
from contextlib import contextmanager


HORSE = """
    CREATE TABLE IF NOT EXISTS horse(horseid, name, sire, dam, damsire, birthdate, gender, coachname)
"""

START = """
    CREATE TABLE start(horseid, startid, distance, driver, meetdate, racenumber, firstprize, starttrack,
                trackcode, winodds, kmtime, frontshoes, rearshoes, specialcart, coachname)
"""

@contextmanager
def db_ops(db_name='testi.db'):
    conn = sqlite3.connect(db_name)
    cur = conn.cursor()
    yield cur
    conn.commit()
    conn.close()


def createdb():
    con = sqlite3.connect("heppa.db")
    cur = con.cursor()
    cur.execute(HORSE)
    cur.execute(START)

with db_ops('db_path') as cur:
    cur.execute('create table if not exists temp (id int, name text)')

with db_ops('db_path') as cur:
    rows = [(1, 'a'), (2, 'b'), (3, 'c')]
    cur.executemany('insert into temp values (?, ?)', rows)

with db_ops('db_path') as cur:
    print(list(cur.execute('select * from temp')))
