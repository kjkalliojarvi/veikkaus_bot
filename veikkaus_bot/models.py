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
    cardId: int
    meetDate: date
    cancelled: Optional[bool] = None
    country: Optional[str] = None
    currentRaceNumber: Optional[int] = None
    currentRaceStatus: Optional[str] = None
    currentRaceStartTime: Optional[int] = None
    firstRaceStart: Optional[int] = None
    future: Optional[bool] = None
    lastRaceOfficial: Optional[int] = None
    lunchRaces: Optional[bool] = None
    minutesToPost: Optional[int] = None
    priority: Optional[int] = None
    raceType: Optional[str] = None
    trackAbbreviation: Optional[str] = None
    trackName: Optional[str] = None
    trackNumber: Optional[int] = None
    mainPerformance: Optional[bool] = None
    totoPools: Optional[list] = None
    epgStartTime: Optional[int] = None
    epgStopTime: Optional[int] = None
    epgChannel: Optional[int] = None
    # The live-progress pool blobs are never read; they exist to document the
    # payload and must not be able to reject a card.
    jackpotPools: Optional[list[dict]] = None
    bonusPools: Optional[list[dict]] = None
    fullHitPotPools: Optional[list[dict]] = None
    vowedPayoutPools: Optional[list[dict]] = None


class Race(BaseModel):
    raceId: int
    cardId: int
    number: Optional[int] = None
    distance: Optional[int] = None
    breed: Optional[str] = None
    seriesSpecification: Optional[str] = None
    raceStatus: Optional[str] = None
    startType: Optional[str] = None
    monte: Optional[bool] = None
    firstPrize: Optional[int] = None
    startTime: Optional[int] = None  # absent on cards older than roughly 2010
    intermediateTimesString: Optional[str] = None
    toteResultString: Optional[str] = None
    reserveHorsesOrder: Optional[str] = None
    raceRider: Optional[str] = None
    trackProfile: Optional[str] = None
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


# Only the four fields that identify a start are required: raceId + startNumber
# key `archive.start`, runnerId keys the stat and bet-percentage rows, and
# horseName builds the horse key. A runner missing any of those cannot be
# placed and should fail loudly.
#
# Everything else is Optional on purpose. A validation error costs the *whole*
# runner — its start, horse, stats and career line — so a required field is a
# standing offer to trade a full row for a cosmetic one. The API has taken that
# offer repeatedly: thin pre-2010 cards, absent breeding on a 2005 runner, a
# 2021 runner with no `coachNameInitials`, and the `Poissa` ("absent")
# placeholder the API sends for a vacated start number, which carries no
# `coachName` at all. Genuine schema drift still shows up as NULLs in the
# tables rather than as silence.
class Runner(BaseModel):
    runnerId: int
    raceId: int
    horseName: str
    startNumber: int
    startTrack: Optional[int] = None
    distance: Optional[int] = None
    scratched: Optional[bool] = None
    prize: Optional[int] = None
    frontShoes: Optional[str] = None
    rearShoes: Optional[str] = None
    frontShoesChanged: Optional[bool] = None
    rearShoesChanged: Optional[bool] = None
    sire: Optional[str] = None
    dam: Optional[str] = None
    damSire: Optional[str] = None
    horseAge: Optional[int] = None
    birthDate: Optional[date] = None
    gender: Optional[str] = None
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
    coachName: Optional[str] = None
    coachNameInitials: Optional[str] = None
    ownerName: Optional[str] = None
    ownerHomeTown: Optional[str] = None
    handicapRating: Optional[int] = None
    specialCart: Optional[str] = None
    condition: Optional[int] = None
    expectedValue: Optional[int] = None
    # These three ride along only on cards that are still current; historical
    # runners payloads carry none of them. `stats` is keyed currentYear /
    # previousYear / total, `betPercentages` by pool type.
    stats: dict = {}
    betPercentages: Optional[dict] = None
    prevStarts: list[PrevStart] = []
