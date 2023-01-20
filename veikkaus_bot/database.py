import sqlite3


HORSE = """
    CREATE TABLE IF NOT EXISTS horse(horseid, name, sire, dam, damsire, birthdate, gender, coachname)
"""

START = """
    CREATE TABLE start(horseid, startid, distance, driver, meetdate, racenumber, firstprize, starttrack,
                trackcode, winodds, kmtime, frontshoes, rearshoes, specialcart, coachname)
"""


def get_cursor():
    con =

def createdb():
    con = sqlite3.connect("heppa.db")
    cur = con.cursor()
    cur.execute(HORSE)
    cur.execute(START)

