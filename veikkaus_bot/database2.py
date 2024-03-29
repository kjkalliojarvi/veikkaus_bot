from decouple import config
from sqlalchemy import create_engine, text, MetaData, Table, Column, Integer, String, Boolean
from sqlalchemy.schema import PrimaryKeyConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
import sqlite3
from typing import List, Optional


# DB_FILE = config('DB_FILE')
DB_FILE = '/home/kari/python/veikkaus_bot/veikkaus_data.db'
sqlite_string = f'sqlite+pysqlite:///{DB_FILE}'
engine = create_engine(sqlite_string, echo=True)
metadata_obj = MetaData()


class Base(DeclarativeBase):
    pass


class RunnerTable(Base):
    __tablename__ = 'runner',

    runnerId: Mapped[int] = mapped_column(primary_key=True)
    horseName: Mapped[str]
    sire: Mapped[str]
    dam: Mapped[str]
    damsire: Mapped[str]
    birthdate: Mapped[str]
    gender: Mapped[str]
    coachName: Mapped[str]
    coachNameInitials: Mapped[str]
    ownerName: Mapped[str]
    ownerHomeTown: Mapped[str]


class StartTable(Base):
    __tablename__ = 'start'

    runnerId: Mapped[int] = mapped_column(primary_key=True)
    priorStartId: Mapped[int] = mapped_column(primary_key=True)
    distance: Mapped[int]
    driver: Mapped[str]
    meetDate: Mapped[str]
    raceNumber: Mapped[int]
    shortMeetDate:Mapped[str]
    firstPrize: Mapped[int]
    startTrack: Mapped[int]
    result: Mapped[str]
    trackCode: Mapped[str]
    winOdd: Mapped[int]
    kmTime: Mapped[str]
    frontShoes: Mapped[str]
    rearShoes: Mapped[str]
    raceStartType: Mapped[str]
    trackProfileType: Mapped[str]
    raceSurface: Mapped[str]
    shoesType: Mapped[str]
    headGear: Mapped[str]
    specialCart: Mapped[str]
    coachName: Mapped[str]
    startInterval: Mapped[int]


class RaceTable(Base):
    __tablename__ = 'race'

    raceId: Mapped[int] = mapped_column(primary_key=True)
    cardId: Mapped[int]
    number: Mapped[int]
    distance: Mapped[int]
    breed: Mapped[str]
    seriesSpecification: Mapped[str]
    startType: Mapped[str]
    monte: Mapped[bool]
    firstPrize: Mapped[int]
    startTime: Mapped[int]
    toteResultString: Mapped[str]
    trackProfile: Mapped[str]
    trackSurface: Mapped[str]
    country: Mapped[str]
    trackAbbreviation: Mapped[str]
    trackName: Mapped[str]
    trackNumber: Mapped[int]


def create_db():
    with engine.connect() as conn:
        conn.execute(text(CREATE_RUNNER_TABLE))
        conn.execute(text(CREATE_RACE_TABLE))
        conn.execute(text(CREATE_START_TABLE))
        conn.commit()
