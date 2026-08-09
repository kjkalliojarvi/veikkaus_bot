"""Pydantic models for the Veikkaus toto-info API resources.

These mirror the API's nested hierarchy — `Card` → `Race` → `Runner`, with
`Stat` riding along inside a runner. They validate what the crawler archived;
nothing here fetches. The API sends Finnish-labeled harness data.

Almost every field is Optional because the live API omits fields depending on
race state *and* on age: historical payloads are markedly thinner than today's
(see §2b of totodatacollectionstrategy.md). Adding a required field risks
validation failures on real responses.
"""
from datetime import date
from typing import Optional

from pydantic import BaseModel


URL = 'https://www.veikkaus.fi/api/toto-info/v1'


# Cards fetched by date omit the live-progress and EPG blocks that today's
# cards always carry, and before roughly 2010 they omit the start times too.
class Card(BaseModel):
    cancelled: bool
    cardId: int
    country: str
    currentRaceNumber: int
    currentRaceStatus: Optional[str] = None
    currentRaceStartTime: Optional[int] = None
    firstRaceStart: Optional[int] = None
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
    epgStartTime: Optional[int] = None
    epgStopTime: Optional[int] = None
    epgChannel: Optional[int] = None
    jackpotPools: list[dict]
    bonusPools: list[dict]
    fullHitPotPools: list[dict]
    vowedPayoutPools: list[dict]


class Race(BaseModel):
    raceId: int
    cardId: int
    number: int
    distance: int
    breed: Optional[str] = None
    seriesSpecification: str
    raceStatus: str
    startType: str
    monte: bool
    firstPrize: int
    startTime: Optional[int] = None  # absent on cards older than roughly 2010
    intermediateTimesString: Optional[str] = None
    toteResultString: Optional[str] = None
    reserveHorsesOrder: Optional[str] = None
    raceRider: str
    trackProfile: str
    photoFinishUrl: Optional[str] = None


class Stat(BaseModel):
    """One statistics period from a runner's `stats` block."""
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


class PrevStart(BaseModel):
    """One earlier start of a horse, as reported inside a runners payload.

    Only `priorStartId` is required: this block is the richest per-horse result
    source in the API and it is not worth losing a whole career line to one
    unexpected gap. Note `meetDate` is midnight *Finnish* time expressed in
    UTC, so its UTC date is a day early — `shortMeetDate` (dd.mm.yy) is the
    meet date that matches `card.meetDate`.
    """
    priorStartId: int
    distance: Optional[int] = None
    driver: Optional[str] = None
    driverFullName: Optional[str] = None
    meetDate: Optional[str] = None
    raceNumber: Optional[int] = None
    shortMeetDate: Optional[str] = None
    firstPrize: Optional[int] = None
    startTrack: Optional[int] = None
    result: Optional[str] = None
    trackCode: Optional[str] = None
    trackName: Optional[str] = None
    winOdd: Optional[str] = None
    kmTime: Optional[str] = None
    frontShoes: Optional[str] = None
    rearShoes: Optional[str] = None
    raceRiderType: Optional[str] = None
    raceStartType: Optional[str] = None
    trackProfileType: Optional[str] = None
    raceSurface: Optional[str] = None
    shoesType: Optional[str] = None
    headGear: Optional[str] = None
    videoLink: Optional[str] = None
    resultsAvailable: Optional[bool] = None
    specialCart: Optional[str] = None


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
    # Breeding and birth date go missing on the oldest cards (one runner in 349
    # sampled from 2005), so none of them can be required.
    sire: Optional[str] = None
    dam: Optional[str] = None
    damSire: Optional[str] = None
    horseAge: int
    birthDate: Optional[date] = None
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
    # These three ride along only on cards that are still current; historical
    # runners payloads carry none of them. `stats` is keyed currentYear /
    # previousYear / total, `betPercentages` by pool type.
    stats: dict = {}
    betPercentages: Optional[dict] = None
    prevStarts: list[PrevStart] = []
