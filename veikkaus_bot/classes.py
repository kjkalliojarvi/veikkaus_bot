from datetime import date, datetime
from typing import List, Dict, Optional
from pydantic import BaseModel


class Card(BaseModel):
    cancelled: bool
    cardId: int
    country: str
    currentRaceNumber: int
    currentRaceStatus: str
    currentRaceStartTime: int
    firstRaceStart: int
    future: bool
    lastRaceOfficial: Optional[int]
    lunchRaces: bool
    meetDate: date
    minutesToPost: int
    priority: int
    raceType: str
    trackAbbreviation: str
    trackName: str
    trackNumber: int
    mainPerformance: bool
    totoPools: Optional[list]
    epgStartTime: int
    epgStopTime: int
    epgChannel: int
    jackpotPools: list[dict]
    bonusPools: list[dict]


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
    toteResultString: str
    reserveHorsesOrder: str
    raceRider: str
    trackProfile: str
    trackSurface: str

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
    allowsCalculator: bool
    allowsBetfile: bool
    allowsFractions: bool
    operatorMaintenance: bool


class Stat(BaseModel):
    year: str
    record1: str
    record2: str
    starts: int
    position1: int
    position2: int
    position3: int
    places: int
    winMoney: int


class Stats(BaseModel):
    currentYear: Stat
    previousYear: Stat
    total: Stat


class PrevStart(BaseModel):
    priorStartId: int
    distance: int
    driver: str
    meetDate: datetime
    raceNumber: int
    shortMeetDate: date
    firstPrize: int
    startTrack: int
    result: str
    trackCode: str
    winOdd: str
    kmTime: str
    frontShoes: str
    rearShoes: str
    raceRiderType: str
    raceStartType: str
    trackProfileType: str
    raceSurface: str
    shoesType: str
    headGear: str
    videoLink: str
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
    color: dict
    mobileStartRecord: str
    handicapRaceRecord: str
    driverName: str
    driverNameInitials: str
    driverLicenseClass: str
    driverOutfitColor: str
    driverRacingColors: str
    driverHelmetColors: str
    driverStats: str
    coachName: str
    coachNameInitials: str
    ownerName: str
    ownerHomeTown: str
    specialCart: str
    condition: int
    expectedValue: int
    stats: dict
    prevStarts: list

