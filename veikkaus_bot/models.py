"""Pydantic models for the two APIs the pipeline reads.

The Veikkaus toto-info models mirror that API's nested hierarchy — `Card` →
`Race` → `Runner`, with `Stat` riding along inside a runner. The `Heppa*`
models at the bottom mirror Hippos's Heppa backend, the official registry that
supplies the finishing detail Veikkaus never publishes (see heppa.py). They
validate what the crawler archived; nothing here fetches.

Almost every field is Optional because the live API omits fields depending on
race state *and* on age: historical payloads are markedly thinner than today's
(see §2b of totodatacollectionstrategy.md). Adding a required field risks
validation failures on real responses.
"""
from datetime import date
from typing import Optional

from pydantic import BaseModel


URL = 'https://www.veikkaus.fi/api/toto-info/v1'
HEPPA_URL = 'https://heppa.hippos.fi/heppa2_backend'


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


# --- Heppa (Suomen Hippos) --------------------------------------------------
#
# The official registry, and the only source that publishes a finishing
# position for the whole field. Two conventions run through every model below
# and neither is optional to remember:
#
# 1. **Every scalar arrives as a string** — `'1'`, `'2080'`, `'4.44'`. They are
#    typed `str` here and converted in parse.py, so that `'0'` meaning "no
#    placing" and `'4.44'` meaning 444 hundredths stay explicit decisions
#    rather than silent coercions.
# 2. **`startNumber` is the race number; `programNumber` is the horse's start
#    number.** Heppa's naming inverts the Veikkaus vocabulary exactly.
class HeppaEvent(BaseModel):
    """One race meeting, from `/race/results/{from}/{to}/`.

    `(date, trackCode)` identifies it — verified unique across all 472 events
    of 2025 — and is what `archive.card` joins to via
    `upper(card.trackAbbreviation)`.
    """
    date: str
    trackCode: str
    name: Optional[str] = None
    startTime: Optional[str] = None
    eventType: Optional[str] = None       # TOTO*, PAIKALLISRAVI, PONI
    trackShortname: Optional[str] = None
    trackName: Optional[str] = None
    trackCity: Optional[str] = None
    trackNumber: Optional[str] = None
    trackType: Optional[str] = None       # KESARATA / TALVIRATA
    trackCondition: Optional[str] = None
    temperature: Optional[str] = None
    meetNumber: Optional[str] = None
    specialRaceEventName: Optional[str] = None
    canceled: Optional[bool] = None
    hasPublishedResults: Optional[bool] = None
    isFreeToPublish: Optional[bool] = None
    majorRace: Optional[bool] = None
    finnishTrack: Optional[bool] = None
    hoofLeagueRace: Optional[bool] = None
    finnHorseChampionshipRace: Optional[bool] = None
    ponyChampionshipRace: Optional[bool] = None
    divisionFinal: Optional[bool] = None
    formattedTrackName: Optional[str] = None
    tototvLink: Optional[str] = None


class HeppaRace(BaseModel):
    """The `race` object inside a `/race/{date}/{trackCode}/races` entry.

    `startForm` (TASOITUSAJO / RYHMALAHTO) is handicap-versus-group and is
    *not* the CAR/VOLT axis that `archive.race.startType` records — the
    auto-start signal is the per-horse `distanceCode` on HeppaStart.
    """
    date: str
    trackCode: str
    startNumber: str                      # the race number
    raceName: Optional[str] = None
    categoryNumber: Optional[str] = None
    plannedTime: Optional[str] = None
    actualTime: Optional[str] = None
    startType: Optional[str] = None       # TOTO / ...
    startForm: Optional[str] = None
    monte: Optional[bool] = None
    eventType: Optional[str] = None       # LAMMINVERISET / SUOMENHEVOSET / ...
    baseDistance: Optional[str] = None
    levellingHeader: Optional[str] = None
    firstPrice: Optional[str] = None
    priceSum: Optional[str] = None
    trackNumber: Optional[str] = None
    status: Optional[str] = None
    specialRace: Optional[bool] = None
    totoResultsReady: Optional[bool] = None
    publishFree: Optional[bool] = None
    finnishTrack: Optional[bool] = None
    totoTypes: Optional[list] = None
    tototvLink: Optional[str] = None
    photo: Optional[dict] = None


class HeppaRaceEntry(BaseModel):
    """One element of the races listing: the race plus its result summary."""
    race: HeppaRace
    gameTypes: Optional[list] = None
    totoResults: Optional[list] = None
    intermediateTime: Optional[str] = None


class HeppaStart(BaseModel):
    """One horse in one race, from `/race/{date}/{trackCode}/start/{raceNo}`.

    This is the row that fills the holes in `archive.start`. Four fields are
    required because they are what places it — the rest follows the standing
    rule that a validation error costs the whole row.

    Two traps live in here. `placing` is a string with three regimes ('0' means
    no placing, '1'-'13' a real one, and >= 100 is 100 + the finishing position
    of a disqualified horse) — parse.parse_placing() owns that. And
    `horsePriceSum` is career earnings *including* this race, unlike Veikkaus's
    pre-race `careerWinnings`, so it leaks the result and must never become an
    as-of-race-day feature. The same caution applies to `record`.
    """
    date: str
    trackCode: str
    startNumber: str                      # the race number
    programNumber: str                    # the horse's start number
    horseId: Optional[str] = None         # 19-digit; stays a string, not BIGINT
    horseName: Optional[str] = None
    horseBreed: Optional[str] = None
    horseRegistrationCountry: Optional[str] = None
    lane: Optional[str] = None
    distance: Optional[str] = None
    distanceCode: Optional[str] = None    # 'ke'/'ake'/'ly'/'aly'/'akp' — 'a' is an auto start
    placing: Optional[str] = None
    disqualifiedCode: Optional[str] = None  # hpl, hll, hlo, hrp, k
    gallop: Optional[bool] = None
    absent: Optional[bool] = None
    kilometerTime: Optional[str] = None       # '1.18.8'
    shortKilometerTime: Optional[str] = None  # '18,8' — the archive.start format
    totalTime: Optional[str] = None
    price: Optional[str] = None           # this race's prize money for this horse
    winOdds: Optional[str] = None         # '4.44'
    winOddsStr: Optional[str] = None
    winOdds2: Optional[str] = None
    horsePriceSum: Optional[str] = None   # career earnings, post-race
    record: Optional[str] = None
    recordType: Optional[str] = None
    recordMonte: Optional[bool] = None
    shortRecord: Optional[str] = None
    carRecord: Optional[str] = None
    carRecordType: Optional[str] = None
    carRecordMonte: Optional[bool] = None
    shortCarRecord: Optional[str] = None
    driverId: Optional[str] = None
    driverName: Optional[str] = None
    driverFirstName: Optional[str] = None
    driverLastName: Optional[str] = None
    driverShortFirstName: Optional[str] = None
    originalDriverId: Optional[str] = None
    originalDriverFirstName: Optional[str] = None
    originalDriverLastName: Optional[str] = None
    originalDriverShortFirstName: Optional[str] = None
    trainerId: Optional[str] = None
    trainerName: Optional[str] = None
    ownerName: Optional[str] = None
    ownerCity: Optional[str] = None
    shoesFront: Optional[str] = None      # K / E / X, not Veikkaus's HAS_SHOES
    shoesBack: Optional[str] = None
    americanSulkyKEX: Optional[str] = None
    startForm: Optional[str] = None
    startType: Optional[str] = None
    monte: Optional[bool] = None
    status: Optional[str] = None
    condition: Optional[str] = None
    expectedValue: Optional[str] = None
    commentText: Optional[str] = None
    testStartAccepted: Optional[str] = None
    startAmount: Optional[str] = None
    trainerCommentsUpdated: Optional[bool] = None
    trackNumber: Optional[str] = None
    finnishTrack: Optional[bool] = None
