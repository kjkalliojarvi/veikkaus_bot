from collections import namedtuple
from datetime import date, datetime
from enum import Enum
from functools import lru_cache
from typing import Optional
from pydantic import BaseModel
import requests
import json
from .database import Db


headers = {'Content-type':'application/json', 'Accept':'application/json', 'X-ESA-API-Key':'ROBOT'}
URL = 'https://www.veikkaus.fi/api/toto-info/v1'


class Card(BaseModel):
    cancelled: bool
    cardId: int
    country: str
    currentRaceNumber: int
    currentRaceStatus: str
    currentRaceStartTime: int
    firstRaceStart: int
    future: bool
    lastRaceOfficial: Optional[int] = None
    lunchRaces: bool
    meetDate: date
    minutesToPost: Optional[int] = None
    priority: int
    raceType: str
    trackAbbreviation: str
    trackName: str
    trackNumber: int
    mainPerformance: bool
    totoPools: Optional[list] = None
    epgStartTime: int
    epgStopTime: int
    epgChannel: int
    jackpotPools: list[dict]
    bonusPools: list[dict]
    fullHitPotPools: list[dict]
    vowedPayoutPools: list[dict]

    def get_races(self):
        races = []
        all_races = _get_collection(f'/card/{self.cardId}/races')
        for race in all_races:
            races.append(Race(**race))
        return races

    def get_card_pools(self) -> list:
        all_pools = _get_collection(f'/card/{self.cardId}/pools')
        pools = []
        for pool in all_pools:
            pools.append(Pool(**pool))
        return pools


class Race(BaseModel):
    raceId: int
    cardId: int
    number: int
    distance: int
    breed: str
    seriesSpecification: str
    raceStatus: str
    startType: str
    monte: bool
    firstPrize: int
    startTime: int
    intermediateTimesString: Optional[str] = None
    toteResultString: Optional[str] = None
    reserveHorsesOrder: Optional[str] = None
    raceRider: str
    trackProfile: str
    photoFinishUrl: Optional[str] = None

    def get_runners(self):
        runners = []
        all_runners = _get_collection(f'/race/{self.raceId}/runners')
        for runner in all_runners:
            runners.append(Runner(**runner))
        return runners

    def race_record(self, card: Card) -> tuple:
        return (self.raceId,
                self.cardId,
                self.number,
                self.distance,
                self.breed,
                self.seriesSpecification,
                self.startType,
                self.monte,
                self.firstPrize,
                self.startTime,
                self.toteResultString,
                self.trackProfile,
                card.country,
                card.trackAbbreviation,
                card.trackName,
                card.trackNumber)

    def get_race_pools(self) -> list:
        all_pools = _get_collection(f'/race/{self.raceId}/pools')
        pools = []
        for pool in all_pools:
            pools.append(Pool(**pool))
        return pools


class Pool(BaseModel):
    poolId: int
    cardId: int
    firstRaceId: int
    firstRaceStartTime: int
    poolType: str
    poolName: str
    poolStatus: str
    races: list[dict]
    netSales: int
    netPool: int
    betTypes: Optional[list] = None
    allowsCalculator: bool
    allowsBetfile: bool
    allowsFractions: bool
    rules: dict
    operatorMaintenance: bool

    def get_odds(self) -> dict:
        return _get_dict(f'/pool/{self.poolId}/odds')


class Stat(BaseModel):
    year: str
    record1: Optional[str] = None
    record2: Optional[str] = None
    starts: int
    position1: int
    position2: int
    position3: int
    places: int
    winMoney: int
    gallopPercent: Optional[int] = None
    disqualificationPercent: Optional[int] = None
    placementPercent: Optional[int] = None
    winningPercent: Optional[int] = None


class Stats(BaseModel):
    currentYear: Stat
    previousYear: Stat
    total: Stat


class PrevStart(BaseModel):
    priorStartId: int
    distance: int
    driver: Optional[str] = None
    meetDate: str
    raceNumber: int
    shortMeetDate: str
    firstPrize: Optional[int] = None
    startTrack: int
    result: Optional[str] = None
    trackCode: str
    trackName: str
    winOdd: Optional[str] = None
    kmTime: Optional[str] = None
    frontShoes: str
    rearShoes: str
    raceRiderType: Optional[str] = None
    raceStartType: Optional[str] = None
    trackProfileType: Optional[str] = None
    raceSurface: Optional[str] = None
    shoesType: Optional[str] = None
    headGear: Optional[str] = None
    videoLink: Optional[str] = None
    resultsAvailable: bool
    specialCart: str


class Runner(BaseModel):
    runnerId: int
    raceId: int
    horseName: str
    startNumber: int
    startTrack: int
    distance: int
    scratched: bool
    prize: int
    frontShoes: str
    rearShoes: str
    frontShoesChanged: bool
    rearShoesChanged: bool
    sire: str
    dam: str
    damSire: str
    horseAge: int
    birthDate: date
    gender: str
    color: Optional[dict] = None
    mobileStartRecord: Optional[str] = None
    handicapRaceRecord: Optional[str] = None
    driverName: Optional[str] = None
    driverNameInitials: Optional[str] = None
    driverLicenseClass: Optional[str] = None
    driverOutfitColor: Optional[str] = None
    driverRacingColors: Optional[str] = None
    driverHelmetColors: Optional[str] = None
    driverStats: Optional[str] = None
    coachName: str
    coachNameInitials: str
    ownerName: str
    ownerHomeTown: Optional[str] = None
    handicapRating: Optional[int] = None
    specialCart: str
    condition: Optional[int] = None
    expectedValue: Optional[int] = None
    stats: dict
    prevStarts: list[PrevStart]

    def runner_record(self) -> tuple:
        return (self.runnerId,
                self.horseName,
                self.sire,
                self.dam,
                self.damSire,
                self.birthDate.year,
                self.gender,
                self.coachName,
                self.ownerName,
                self.ownerHomeTown)

    def prevstarts_record(self):
        starts = []
        previousdate = '1970-01-01T22:01:00.000+00:00'
        format = '%Y-%m-%dT%H:%M:%S.%f%z'
        for start in self.prevStarts:
            if not (start.raceNumber > 20 or start.raceNumber == 'kl'):
                diff = datetime.strptime(start.meetDate, format) - datetime.strptime(previousdate, format)
                starts.append((
                    self.runnerId,
                    start.priorStartId,
                    start.distance,
                    start.driver,
                    start.meetDate,
                    start.raceNumber,
                    start.shortMeetDate,
                    start.firstPrize,
                    start.startTrack,
                    start.result,
                    start.trackCode,
                    start.winOdd,
                    start.kmTime,
                    start.frontShoes,
                    start.rearShoes,
                    start.raceStartType,
                    start.trackProfileType,
                    start.raceSurface,
                    start.shoesType,
                    start.headGear,
                    start.specialCart,
                    self.coachName,
                    diff.days))
                previousdate = start.meetDate
        return starts


class PoolTypes(Enum):
    VOITTAJA = 'VOI'
    SIJA = 'SIJA'
    KAKSARI = 'KAK'
    TROIKKA = 'TRO'
    DUO = 'DUO'
    T4 = 'T4'
    T5 = 'T5'
    T65 = 'T65'


class VeikkausData:
    def __init__(self, country=None):
        self.country: str = country
        all_cards = []
        all_races = []
        all_runners = []
        cards = get_cards(self.country)
        for card in cards:
            all_cards.append(card)
            races = card.get_races()
            for race in races:
                all_races.append(race)
                runners = race.get_runners()
                for runner in runners:
                    all_runners.append(runner)
        self.cards: list[Card] = all_cards
        self.races: list[Race] = all_races
        self.runners: list[Runner] = all_runners
        print(f'{len(self.cards)} ravit, {len(self.races)} lähtöä, {len(self.runners)} hevosta.')

    def save_to_file(self):
        race_records = []
        runner_records = []
        start_records = []
        for race in self.races:
            for card in self.cards:
                if card.cardId == race.cardId:
                    race_records.append(race.race_record(card))
        for runner in self.runners:
            runner_records.append(runner.runner_record())
            start_records += runner.prevstarts_record()
        data = {
            'races': race_records,
            'runners': runner_records,
            'starts': start_records
        }
        timestamp = datetime.now()
        filename = f'{timestamp.year}-{timestamp.month}-{timestamp.day}-{self.country}.json'
        with open(filename, 'w') as outfile:
            json.dump(data, outfile)


def _get_collection(url: str) -> dict:
    resp = requests.get(f'{URL}{url}', headers=headers)
    return json.loads(resp.text)['collection']


def _get_dict(url: str) -> dict:
    resp = requests.get(f'{URL}{url}', headers=headers)
    return json.loads(resp.text)


@lru_cache
def get_cards(country=None) -> list[Card]:
    cards = _get_collection('/cards/today')
    all_cards = []
    for card in cards:
        if (not country or card['country'] == country):
            all_cards.append(Card(**card))
    return all_cards


def get_previous_starts(runner: Runner) -> list:
    for start in runner.prevStarts:
        print(PrevStart(**start))
    print(Stat(**runner.stats['currentYear']))
    print(Stat(**runner.stats['previousYear']))
    print(Stat(**runner.stats['total']))
