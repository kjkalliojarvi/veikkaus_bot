"""The crawl-derived past-performance dataset (strategy §5).

The tables live in a DuckDB schema named `archive`, keeping the crawl-derived
dataset distinct from anything else the database file might hold.

Column names mirror the API's camelCase. Two DuckDB rules hold throughout:
every id and epoch-millisecond value is `BIGINT` (`INTEGER` is 32-bit and
`raceId`/`startTime` overflow it), and every table upserts with
`INSERT OR REPLACE` — the SQLite `ON CONFLICT` clause inside a `PRIMARY KEY`
definition is not supported.
"""
import os
from contextlib import contextmanager

import duckdb


# Alongside the raw zone (`data/raw`), so that both halves of the pipeline
# default into `data/` and a bare `parse` finds the manifest a bare `backfill`
# wrote.
DEFAULT_DB = 'data/veikkaus_data.duckdb'

CREATE_SCHEMA = 'CREATE SCHEMA IF NOT EXISTS archive;'

CREATE_CARD_TABLE = """
    CREATE TABLE IF NOT EXISTS archive.card(
        cardId BIGINT,
        country TEXT,
        meetDate TEXT,
        trackAbbreviation TEXT,
        trackName TEXT,
        trackNumber BIGINT,
        raceType TEXT,
        firstRaceStart BIGINT,
        lunchRaces BOOLEAN,
        mainPerformance BOOLEAN,
        cancelled BOOLEAN,
        PRIMARY KEY (cardId));
"""
INSERT_CARD = 'INSERT OR REPLACE INTO archive.card VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);'
CARD_KEY = (0,)  # cardId

CREATE_RACE_TABLE = """
    CREATE TABLE IF NOT EXISTS archive.race(
        raceId BIGINT,
        cardId BIGINT,
        number BIGINT,
        startTime BIGINT,
        distance BIGINT,
        startType TEXT,
        monte BOOLEAN,
        firstPrize BIGINT,
        breed TEXT,
        seriesSpecification TEXT,
        raceStatus TEXT,
        raceRider TEXT,
        trackProfile TEXT,
        toteResultString TEXT,
        intermediateTimesString TEXT,
        PRIMARY KEY (raceId));
"""
INSERT_RACE = ('INSERT OR REPLACE INTO archive.race '
               'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);')
RACE_KEY = (0,)  # raceId

# `horseKey` stays the key every other table joins on — it is what the parser
# can compute from a Veikkaus payload alone. `canonicalKey` is the identity to
# *group* by: the registry knows that several horseKeys are one horse, and
# RECOMPUTE_HORSE_IDENTITY writes that down. Analysis wanting one row per horse
# should group on canonicalKey, not horseKey.
CREATE_HORSE_TABLE = """
    CREATE TABLE IF NOT EXISTS archive.horse(
        horseKey TEXT,
        horseName TEXT,
        birthYear BIGINT,
        gender TEXT,
        sire TEXT,
        dam TEXT,
        damSire TEXT,
        heppaHorseId TEXT,       -- Hippos's registry id; recomputed, see below
        baseKey TEXT,            -- name key with the import markers removed
        canonicalKey TEXT,       -- the horseKey that stands for this horse
        PRIMARY KEY (horseKey));
"""
INSERT_HORSE = 'INSERT OR REPLACE INTO archive.horse VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);'
HORSE_KEY = (0,)  # horseKey

# One row per (race, horse) — one past-performance line.
# placement/kmTime/winOddsFinal stay NULL outside the paid places: the API only
# publishes finishing detail for the runners a pool paid out on (see §2b).
# The plan calls the column `placing`, which DuckDB's Postgres-derived parser
# reserves (OVERLAY ... PLACING ...), so it is `placement` here.
CREATE_START_TABLE = """
    CREATE TABLE IF NOT EXISTS archive.start(
        raceId BIGINT,
        startNumber BIGINT,
        runnerId BIGINT,
        horseKey TEXT,
        horseName TEXT,
        driverName TEXT,
        coachName TEXT,
        ownerName TEXT,
        ownerHomeTown TEXT,
        startTrack BIGINT,
        distance BIGINT,
        frontShoes TEXT,
        rearShoes TEXT,
        specialCart TEXT,
        handicapRating BIGINT,
        scratched BOOLEAN,
        careerWinnings BIGINT,   -- runner.prize: career earnings, not this race's purse
        placement BIGINT,
        kmTime TEXT,
        kmTimeMs BIGINT,
        autoStart BOOLEAN,
        winOddsFinal BIGINT,
        prizeWon BIGINT,         -- this race's purse; Heppa only, no Veikkaus equivalent
        disqualifiedCode TEXT,   -- hpl, hll, hlo, hrp, k — Heppa only
        gallop BOOLEAN,          -- Heppa only
        resultSource TEXT,       -- which source supplied `placement`
        startInterval BIGINT,    -- days since this horse's previous known start
        PRIMARY KEY (raceId, startNumber));
"""
INSERT_START = ('INSERT OR REPLACE INTO archive.start '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '
                '?, ?, ?, ?, ?);')
START_KEY = (0, 1)  # raceId, startNumber

CREATE_ODDS_TABLE = """
    CREATE TABLE IF NOT EXISTS archive.odds_snapshot(
        poolId BIGINT,
        raceId BIGINT,
        startNumber BIGINT,
        capturedAt BIGINT,
        poolType TEXT,
        probable BIGINT,
        amount BIGINT,
        scratched BOOLEAN,
        PRIMARY KEY (poolId, startNumber, capturedAt));
"""
INSERT_ODDS = 'INSERT OR REPLACE INTO archive.odds_snapshot VALUES (?, ?, ?, ?, ?, ?, ?, ?);'
ODDS_KEY = (0, 2, 3)  # poolId, startNumber, capturedAt

# Career/season form and betting support ride along inside a runners payload,
# but only while the card is current — both are empty for historical cards.
CREATE_STAT_TABLE = """
    CREATE TABLE IF NOT EXISTS archive.stat(
        runnerId BIGINT,
        period TEXT,
        year TEXT,
        record1 TEXT,
        record2 TEXT,
        starts BIGINT,
        position1 BIGINT,
        position2 BIGINT,
        position3 BIGINT,
        places BIGINT,
        winMoney BIGINT,
        gallopPercent BIGINT,
        disqualificationPercent BIGINT,
        placementPercent BIGINT,
        winningPercent BIGINT,
        PRIMARY KEY (runnerId, period));
"""
INSERT_STAT = ('INSERT OR REPLACE INTO archive.stat '
               'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);')
STAT_KEY = (0, 1)  # runnerId, period

CREATE_BETPERCENTAGE_TABLE = """
    CREATE TABLE IF NOT EXISTS archive.bet_percentage(
        runnerId BIGINT,
        poolType TEXT,
        percentage BIGINT,
        PRIMARY KEY (runnerId, poolType));
"""
INSERT_BETPERCENTAGE = 'INSERT OR REPLACE INTO archive.bet_percentage VALUES (?, ?, ?);'
BETPERCENTAGE_KEY = (0, 1)  # runnerId, poolType

# A horse's earlier starts, as reported inside a runners payload. This is the
# only source in the API that carries the *whole* field's finishing detail
# (`archive.start` stops at the paid places), so it is the backbone of the
# past-performance history.
#
# Keyed on (horseKey, meetDate, raceNumber) — the natural identity of a start —
# and emphatically *not* on priorStartId. That id is assigned per reporting
# payload, not per start: when a horse races again, its whole prevStarts list
# comes back renumbered, one contiguous block of new ids (observed as a
# constant offset between two reports of the same career). Keying on it would
# store one row per (start x every later race of that horse), which over a
# multi-year crawl is the same history duplicated many times over, and would
# feed `startInterval` a stream of zero-day gaps between the copies.
# priorStartId is kept as informational — whatever the latest report called it.
CREATE_PREVSTART_TABLE = """
    CREATE TABLE IF NOT EXISTS archive.prev_start(
        priorStartId BIGINT,
        horseKey TEXT,
        meetDate TEXT,           -- local meet date, from shortMeetDate
        meetDateTime TEXT,       -- raw timestamp: midnight Finnish time in UTC
        trackCode TEXT,
        trackName TEXT,
        raceNumber BIGINT,
        distance BIGINT,
        startTrack BIGINT,
        driver TEXT,
        driverFullName TEXT,
        firstPrize BIGINT,
        result TEXT,             -- raw code: '1'..'14', kl, k, hpl, hll, hlo4...
        placement BIGINT,        -- the numeric codes only; NULL for the rest
        kmTime TEXT,
        kmTimeMs BIGINT,
        autoStart BOOLEAN,
        winOdd BIGINT,           -- hundredths, as sent (a digit string)
        frontShoes TEXT,
        rearShoes TEXT,
        shoesType TEXT,
        headGear TEXT,
        specialCart TEXT,
        raceStartType TEXT,
        raceRiderType TEXT,
        trackProfileType TEXT,
        raceSurface TEXT,
        resultsAvailable BOOLEAN,
        startInterval BIGINT,    -- days since the horse's previous known start
        coachName TEXT,          -- trainer at *this* start, from archive.start
        PRIMARY KEY (horseKey, meetDate, raceNumber));
"""
INSERT_PREVSTART = ('INSERT OR REPLACE INTO archive.prev_start VALUES '
                    '(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '
                    '?, ?, ?, ?, ?, ?, ?);')
PREVSTART_KEY = (1, 2, 6)  # horseKey, meetDate, raceNumber

# --- Heppa (Suomen Hippos) --------------------------------------------------
#
# The official registry, crawled by heppa.py. Everything below is keyed on
# (meetDate, trackCode[, raceNumber[, programNumber]]) — Heppa exposes no
# equivalent of cardId or raceId, and that tuple is what joins back to
# `archive.card` via `upper(card.trackAbbreviation)`. (date, trackCode) was
# verified unique across all 472 events of 2025.
#
# These tables also cover the local (PAIKALLISRAVI) and pony (PONI) meetings
# that the Veikkaus API never reports at all, so a row here need not have any
# `archive.card` counterpart.
CREATE_HEPPA_EVENT_TABLE = """
    CREATE TABLE IF NOT EXISTS archive.heppa_event(
        meetDate TEXT,
        trackCode TEXT,
        trackNumber BIGINT,
        trackShortname TEXT,
        trackName TEXT,
        trackCity TEXT,
        eventType TEXT,          -- TOTO*, PAIKALLISRAVI, PONI
        name TEXT,
        startTime TEXT,
        meetNumber BIGINT,
        trackType TEXT,          -- KESARATA / TALVIRATA
        trackCondition TEXT,
        temperature BIGINT,
        specialRaceEventName TEXT,
        majorRace BOOLEAN,
        canceled BOOLEAN,
        PRIMARY KEY (meetDate, trackCode));
"""
INSERT_HEPPA_EVENT = ('INSERT OR REPLACE INTO archive.heppa_event '
                      'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);')
HEPPA_EVENT_KEY = (0, 1)  # meetDate, trackCode

# `raceNumber` is the payload's `race.startNumber` — Heppa's naming inverts the
# Veikkaus vocabulary, where startNumber is the horse's number.
CREATE_HEPPA_RACE_TABLE = """
    CREATE TABLE IF NOT EXISTS archive.heppa_race(
        meetDate TEXT,
        trackCode TEXT,
        raceNumber BIGINT,
        raceName TEXT,
        categoryNumber BIGINT,
        plannedTime TEXT,
        actualTime TEXT,
        startForm TEXT,          -- TASOITUSAJO / RYHMALAHTO: handicap vs group,
                                 -- NOT the CAR/VOLT axis of race.startType
        monte BOOLEAN,
        eventType TEXT,          -- LAMMINVERISET / SUOMENHEVOSET / ...
        baseDistance BIGINT,
        levellingHeader TEXT,
        firstPrize BIGINT,
        priceSum BIGINT,
        status TEXT,
        intermediateTime TEXT,
        PRIMARY KEY (meetDate, trackCode, raceNumber));
"""
INSERT_HEPPA_RACE = ('INSERT OR REPLACE INTO archive.heppa_race '
                     'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);')
HEPPA_RACE_KEY = (0, 1, 2)  # meetDate, trackCode, raceNumber

# One row per (race, horse), from the official registry — the table that fills
# the holes in `archive.start`. Unlike the Veikkaus results endpoint this
# carries a finishing position for the *whole* field, plus three things the
# Veikkaus API has no equivalent of at all: this race's prize money, a
# disqualification code for every start, and stable registry ids.
#
# `placingRaw` keeps the code verbatim; `placement` holds only the numeric
# placings, per parse.parse_placing(). The column cannot be called `placing` —
# DuckDB's Postgres-derived parser reserves it, exactly as for archive.start.
#
# `horsePriceSum` is career earnings *including* this race, unlike Veikkaus's
# pre-race `careerWinnings`. It is kept for cross-checking, never as a feature.
#
# `horseKey` is NULL at insert and recomputed: a Heppa start carries a birth
# year nowhere, so identity has to come from the archive via `horseId`.
CREATE_HEPPA_START_TABLE = """
    CREATE TABLE IF NOT EXISTS archive.heppa_start(
        meetDate TEXT,
        trackCode TEXT,
        raceNumber BIGINT,
        programNumber BIGINT,    -- the horse's start number
        horseKey TEXT,           -- recomputed from horseId, see below
        horseId TEXT,            -- 19-digit registry id; TEXT, not BIGINT
        horseName TEXT,
        horseBreed TEXT,
        horseRegistrationCountry TEXT,
        startTrack BIGINT,       -- `lane`, named to match archive.start
        distance BIGINT,
        distanceCode TEXT,       -- 'ke'/'ake'/... — an 'a' prefix is an auto start
        placingRaw TEXT,
        placement BIGINT,        -- the numeric placings only
        disqualifiedCode TEXT,   -- hpl, hll, hlo, hrp, k
        gallop BOOLEAN,
        absent BOOLEAN,          -- scratched
        kmTime TEXT,             -- shortKilometerTime: the archive.start format
        kmTimeMs BIGINT,
        autoStart BOOLEAN,       -- from distanceCode, not from startForm
        totalTime TEXT,
        prizeWon BIGINT,         -- `price`: this race's purse for this horse
        winOdd BIGINT,           -- hundredths, as archive.prev_start stores it
        horsePriceSum BIGINT,    -- career earnings, POST-race: never a feature
        driverId TEXT,
        driverName TEXT,
        driverFirstName TEXT,
        driverLastName TEXT,
        originalDriverId TEXT,
        trainerId TEXT,
        trainerName TEXT,
        ownerName TEXT,
        ownerCity TEXT,
        frontShoes TEXT,         -- K / E / X, not Veikkaus's HAS_SHOES
        rearShoes TEXT,
        specialCart TEXT,        -- americanSulkyKEX
        record TEXT,
        recordType TEXT,
        monte BOOLEAN,
        status TEXT,
        commentText TEXT,
        startInterval BIGINT,    -- days since this horse's previous known start
        PRIMARY KEY (meetDate, trackCode, raceNumber, programNumber));
"""
INSERT_HEPPA_START = ('INSERT OR REPLACE INTO archive.heppa_start VALUES '
                      '(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '
                      '?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);')
HEPPA_START_KEY = (0, 1, 2, 3)  # meetDate, trackCode, raceNumber, programNumber

# The registry's record of the animal rather than of a race — one row per
# horse, from `/horse/{horseId}`. Nothing in it is time-varying, so unlike the
# `/horse/{id}/stats` endpoint (deliberately not crawled) it cannot leak a
# result into an as-of-race-day feature.
#
# `registerNo`/`ueln` is what the Veikkaus API has no equivalent of at all, and
# unlike `horseId` it means something outside Heppa: UELN is international, so
# it is the join to any other registry.
#
# `birthCountry` is the origin the country tag in a Veikkaus horse name is
# gesturing at. Note it is not `registrationCountry`, which says where a horse
# races and reads 'FI' for any import — the distinction that makes
# `heppa_start.horseRegistrationCountry` the wrong field for identity work.
CREATE_HEPPA_HORSE_TABLE = """
    CREATE TABLE IF NOT EXISTS archive.heppa_horse(
        horseId TEXT,
        horseName TEXT,
        birthDate TEXT,          -- exact, where archive.horse has only a year
        birthDateAccurate BOOLEAN,
        registerNo TEXT,
        ueln TEXT,               -- international; the cross-registry join key
        chipNo TEXT,
        dead BOOLEAN,
        registrationSuspended BOOLEAN,
        species TEXT,
        breedCode TEXT,
        breedFinName TEXT,
        gender TEXT,
        color TEXT,
        birthCountry TEXT,       -- origin
        birthCountryName TEXT,
        birthPlace TEXT,
        origin TEXT,
        registrationCountry TEXT,  -- where it races; not the origin
        breedingUnion TEXT,
        breederName TEXT,
        ownerName TEXT,
        trainerId TEXT,
        trainerName TEXT,
        homeTrackName TEXT,
        homeTrackCity TEXT,
        bestRecord TEXT,
        sireId TEXT,
        sireName TEXT,
        sireRegisterNo TEXT,
        damId TEXT,
        damName TEXT,
        damRegisterNo TEXT,
        PRIMARY KEY (horseId));
"""
INSERT_HEPPA_HORSE = ('INSERT OR REPLACE INTO archive.heppa_horse VALUES '
                      '(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '
                      '?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);')
HEPPA_HORSE_KEY = (0,)  # horseId

# The prev-start block names a driver but never a trainer, so the trainer at
# the time of a past start has to come from the archive itself: the crawled
# `archive.start` row for that same race, which recorded the trainer as of that
# day. Rows outside the crawl window stay NULL — there is no source for them,
# and the reporting runner's `coachName` is emphatically not one. That is the
# trainer at a *later* race, and under priorStartId dedup it would be whichever
# report happened to land last.
#
# Matched on horse + meet date + race number. Track is deliberately not part of
# the join: `prev_start.trackCode` and `card.trackAbbreviation` are not verified
# to share a vocabulary at every track, and a horse cannot be in two places on
# one day anyway.
# Start type is a property of the race, not of a horse's performance, so it is
# known for every runner — including the scratched ones and everyone outside
# the paid places. Deriving it only from the `a` suffix on a km time (which is
# all the runners payload offers per horse) left it NULL wherever no time was
# recorded, which was most of the table. Where both sources exist they agree
# exactly, so the race wins and the suffix becomes a cross-check.
# Only CAR_START and VOLT_START have ever been observed; anything else is left
# NULL rather than silently called a volt start.
RECOMPUTE_START_AUTOSTART = """
    UPDATE archive.start AS s
    SET autoStart = (r.startType = 'CAR_START')
    FROM archive.race AS r
    WHERE r.raceId = s.raceId
      AND r.startType IN ('CAR_START', 'VOLT_START');
"""

# The prev-start block has a raceStartType field, but the API sends UNKNOWN in
# every entry ever observed, so the km-time suffix is the only per-row signal
# there. Where the crawl has covered the race itself, fill the gaps the suffix
# cannot reach — a start with no recorded time still had a start type. Existing
# values are left alone; the two sources agree, and the suffix is per-horse.
RECOMPUTE_PREV_START_AUTOSTART = """
    UPDATE archive.prev_start AS p
    SET autoStart = t.autoStart
    FROM (SELECT s.horseKey, ca.meetDate, r.number AS raceNumber,
                 min(CAST(r.startType = 'CAR_START' AS TINYINT)) = 1 AS autoStart
          FROM archive.start s
          JOIN archive.race r ON r.raceId = s.raceId
          JOIN archive.card ca ON ca.cardId = r.cardId
          WHERE r.startType IN ('CAR_START', 'VOLT_START')
          GROUP BY 1, 2, 3) AS t
    WHERE t.horseKey = p.horseKey
      AND t.meetDate = p.meetDate
      AND t.raceNumber = p.raceNumber
      AND p.autoStart IS NULL;
"""

RECOMPUTE_PREV_START_COACH = """
    UPDATE archive.prev_start AS p
    SET coachName = c.coachName
    FROM (SELECT s.horseKey, ca.meetDate, r.number AS raceNumber,
                 min(s.coachName) AS coachName
          FROM archive.start s
          JOIN archive.race r ON r.raceId = s.raceId
          JOIN archive.card ca ON ca.cardId = r.cardId
          WHERE s.coachName IS NOT NULL
          GROUP BY 1, 2, 3) AS c
    WHERE c.horseKey = p.horseKey
      AND c.meetDate = p.meetDate
      AND c.raceNumber = p.raceNumber;
"""

# Days since the horse's previous start, filled in after loading rather than
# per record. A reported prevStarts list is a truncated window (~8 entries), so
# the gap in front of its oldest entry is unknowable from that list alone — and
# as the window slides, whichever report lands last would decide the value.
# Computing it over the whole accumulated table instead makes it deterministic
# and re-derives it correctly as new dates are crawled in.
#
# A horse's first start in the archive has no predecessor. It keeps the old
# pipeline's sentinel — days since the 1970 epoch, so ~20000 — rather than
# NULL, which is a value to filter out in analysis, not a real interval.
RECOMPUTE_START_INTERVAL = """
    UPDATE archive.prev_start AS p
    SET startInterval = g.gap
    FROM (SELECT horseKey, meetDate, raceNumber,
                 coalesce(
                     date_diff('day',
                               lag(CAST(meetDate AS DATE)) OVER (PARTITION BY horseKey
                                                                 ORDER BY meetDate, raceNumber),
                               CAST(meetDate AS DATE)),
                     date_diff('day', DATE '1970-01-01', CAST(meetDate AS DATE))) AS gap
          FROM archive.prev_start) AS g
    WHERE g.horseKey = p.horseKey
      AND g.meetDate = p.meetDate
      AND g.raceNumber = p.raceNumber;
"""

# --- Heppa recomputes -------------------------------------------------------
#
# The bridge between the two sources is positional, never name-based:
# (card.meetDate, upper(card.trackAbbreviation), race.number, start.startNumber)
# against (meetDate, trackCode, raceNumber, programNumber). Matching on names
# would break on the country tag that Veikkaus appends and Heppa keeps in a
# separate `horseRegistrationCountry` field.
#
# `upper()` is what makes the track join work: Veikkaus writes 'Ku', 'Tk',
# 'Jo'; Heppa writes 'KU', 'TK', 'JO'. Verified to match for every real Finnish
# track in the archive. The cards that do not match are the Swedish simulcasts
# (trackAbbreviation ending '-V') and the Veikkaus combination-pool meta-cards
# (MM, KUN, CIT, T75, Sl, JAA) — neither is a real meeting and neither has a
# Heppa counterpart by design.
# Veikkaus writes Härmä two ways — 'Hr' (Härmä Powerpark) and 'Hr2' (Härmä),
# both trackNumber 37 — while Heppa has only 'HR'. Upper-casing alone therefore
# leaves 'HR2', which matches nothing, and it silently cost 28 meetings: 28 Hr2
# cards and 28 otherwise-unmatched HR meetings, on exactly the same 28 dates,
# with nothing left over on either side. 'Hr' and 'Hr2' never fall on the same
# date, so folding both onto 'HR' cannot make a meeting ambiguous.
#
# Kept as an explicit alias rather than a rule like "strip trailing digits":
# this is one quirk of one operator's vocabulary, not a pattern.
HEPPA_TRACK_ALIASES = {'HR2': 'HR'}


def heppa_track_code(alias: str = 'ca') -> str:
    """SQL mapping a card's trackAbbreviation onto Heppa's trackCode."""
    whens = ' '.join(f"WHEN '{k}' THEN '{v}'" for k, v in HEPPA_TRACK_ALIASES.items())
    return f'CASE upper({alias}.trackAbbreviation) {whens} ELSE upper({alias}.trackAbbreviation) END'


# The bridge itself, kept separate so that `crosscheck.py` validates the very
# join the merge relies on rather than a hand-copied lookalike.
HEPPA_START_BRIDGE = f"""
    FROM archive.heppa_start h
    JOIN archive.card ca ON ca.meetDate = h.meetDate
                        AND {heppa_track_code('ca')} = h.trackCode
    JOIN archive.race r ON r.cardId = ca.cardId AND r.number = h.raceNumber
    JOIN archive.start s ON s.raceId = r.raceId AND s.startNumber = h.programNumber
"""

HEPPA_START_JOIN = f"""
    SELECT s.raceId, s.startNumber, s.horseKey,
           h.horseId, h.placement, h.kmTime, h.kmTimeMs, h.winOdd,
           h.prizeWon, h.disqualifiedCode, h.gallop
    {HEPPA_START_BRIDGE}
"""

# Hippos's registry id for a horse the archive already knows by name+birth year.
# This is the identity resolution strategy §5 asks for: `horse_key()` is a
# name-and-year guess, `horseId` is authoritative. A horseKey that resolves to
# more than one horseId is a genuine collision — min() picks one so the column
# stays deterministic, and the collision report in the docs finds the rest.
RECOMPUTE_HEPPA_HORSE_ID = f"""
    UPDATE archive.horse AS h
    SET heppaHorseId = m.horseId
    FROM (SELECT horseKey, min(horseId) AS horseId
          FROM ({HEPPA_START_JOIN}) WHERE horseId IS NOT NULL
          GROUP BY horseKey) AS m
    WHERE m.horseKey = h.horseKey;
"""

# The reverse direction, and the reason the horse-level mapping exists at all:
# a Heppa start carries no birth year, so `horse_key()` cannot be computed from
# one. Going through `horseId` reaches the local and pony meetings too, where
# there is no `archive.start` row to join to but the horse appears elsewhere in
# the archive.
RECOMPUTE_HEPPA_START_HORSEKEY = """
    UPDATE archive.heppa_start AS h
    SET horseKey = k.horseKey
    FROM (SELECT heppaHorseId, min(horseKey) AS horseKey
          FROM archive.horse WHERE heppaHorseId IS NOT NULL
          GROUP BY heppaHorseId) AS k
    WHERE k.heppaHorseId = h.horseId;
"""

# Which horseKeys are actually one horse.
#
# `horse_key()` is name + birth year, and Veikkaus writes an import's name
# inconsistently — 'Humble Stance', 'Humble Stance* (FR)' and
# 'Humble Stance FR* (FR)' are one horse under one registry id. That split 182
# horses across 365 keys.
#
# The registry id decides wherever we have one, and `baseKey` only has to carry
# the horses it does not reach. That order matters: base names collide across
# origin countries — 'Elliot' and 'Elliot (DK)', both foaled 2016, are two real
# horses — and grouping by the id first means the name fallback never gets the
# chance to merge them. Names never repeat *within* an origin country, so for a
# horse with no registry id the base name plus birth year is the best available
# identity, and `base_horse_key()` is deliberately conservative about what it
# strips.
#
# min() picks the representative so the column is deterministic.
RECOMPUTE_HORSE_IDENTITY = """
    UPDATE archive.horse AS h
    SET canonicalKey = m.canonicalKey
    FROM (SELECT coalesce(heppaHorseId, baseKey, horseKey) AS identity,
                 min(horseKey) AS canonicalKey
          FROM archive.horse
          GROUP BY 1) AS m
    WHERE coalesce(h.heppaHorseId, h.baseKey, h.horseKey) = m.identity;
"""

# The payoff: the whole field's finishing detail, merged into archive.start —
# in three statements, because the merge has to be idempotent on its own.
#
# The obvious single UPDATE is not. It reads `s.placement` to decide
# `resultSource` and writes `s.placement` in the same breath, so a second run
# sees Heppa's own fill sitting there and relabels it 'veikkaus'. Every other
# recompute in this file is a pure function of the tables it reads, and this
# one has to be as well: `parse_all()` happens to rebuild `archive.start` from
# the raw zone first, which would mask the problem, but a recompute whose
# answer depends on how many times it has run is a trap either way.
#
# So: clear what Heppa previously contributed, merge, then label the rest.
# `resultSource` is the provenance marker that makes step 1 possible, which is
# also why the merge stays away from `winOddsFinal` — one marker cannot honestly
# describe two columns that can come from different sources on the same row.
# Heppa's final win odd is in `heppa_start.winOdd` for cross-checking; the
# archive's own odds stay purely Veikkaus, where the whole odds history lives.
RESET_HEPPA_CONTRIBUTION = """
    UPDATE archive.start
    SET placement = CASE WHEN resultSource = 'heppa' THEN NULL ELSE placement END,
        kmTime = CASE WHEN resultSource = 'heppa' THEN NULL ELSE kmTime END,
        kmTimeMs = CASE WHEN resultSource = 'heppa' THEN NULL ELSE kmTimeMs END,
        resultSource = NULL,
        prizeWon = NULL,
        disqualifiedCode = NULL,
        gallop = NULL;
"""

# Veikkaus wins where it has an answer — coalesce, never overwrite — so the
# paid places keep the value the betting operator published and the other
# ~195,000 starts get theirs from the registry.
#
# prizeWon/disqualifiedCode/gallop are taken unconditionally: Veikkaus has no
# equivalent of any of them, so there is nothing to coalesce against. Note a
# disqualified horse gets a code but no placement, and so no resultSource.
RECOMPUTE_START_FROM_HEPPA = f"""
    UPDATE archive.start AS s
    SET placement = coalesce(s.placement, j.placement),
        kmTime = coalesce(s.kmTime, j.kmTime),
        kmTimeMs = coalesce(s.kmTimeMs, j.kmTimeMs),
        prizeWon = j.prizeWon,
        disqualifiedCode = j.disqualifiedCode,
        gallop = j.gallop,
        resultSource = CASE WHEN s.placement IS NULL AND j.placement IS NOT NULL
                            THEN 'heppa' END
    FROM ({HEPPA_START_JOIN}) AS j
    WHERE j.raceId = s.raceId
      AND j.startNumber = s.startNumber;
"""

# Everything still placed after the merge came from the Veikkaus payload — the
# rows Heppa never reached as well as the ones where it agreed but lost the
# coalesce. Labelling them here rather than in the merge keeps the statement
# above from having to reason about rows it does not join to.
LABEL_VEIKKAUS_RESULTS = """
    UPDATE archive.start SET resultSource = 'veikkaus'
    WHERE placement IS NOT NULL AND resultSource IS NULL;
"""

# --- cross-source start intervals -------------------------------------------
#
# Days since the horse's previous start, on the tables a model actually reads.
# `archive.prev_start.startInterval` above answers a narrower question over one
# table; this one is the layoff feature, and the two disagree on purpose (see
# CLAUDE.md).
#
# Every start the archive knows about, from all three start-bearing tables.
# `archive.prev_start` reaches back before the crawl window; `heppa_start`
# reaches the local (PAIKALLISRAVI) and pony (PONI) meetings that have no
# Veikkaus card at all. Neither is visible from `archive.start`, which is why
# this is a union rather than a window over one table — and a missed
# intervening start does not merely go unrecorded, it inflates the next gap.
#
# canonicalKey, not horseKey: Veikkaus writes an import's name inconsistently
# and that splits 182 horses across 365 keys. Partitioning on horseKey would
# give each fragment a career of its own and inflate every gap in it.
#
# The combination-pool meta-cards are excluded, and they have to be. They are
# not meetings: a meta-card re-lists races that also appear under their real
# track, so counting them puts the horse at two tracks on one day and hands the
# next real start a zero-day gap. Measured before this filter: 2,514 horse-days
# spread over more than one track, 2,489 of them a meta-card, and 2,475 of the
# 2,806 zero-day gaps on archive.start were spurious. Dropping them costs
# almost nothing — of 2,712 meta-card start rows, 8 are the only appearance
# their horse has that day.
#
# The Swedish simulcasts (trackNumber 57/87, abbreviation ending '-V') are
# deliberately *kept*. They are the other kind of card that has no Heppa
# counterpart, but unlike a meta-card they are a real race the horse really
# ran, duplicating nothing: they account for 0 of the multi-track days.
# Dropping them would inflate the gaps around them.
#
# Meta-card start rows therefore end up with startInterval NULL, since the
# join-back has no key to find. That is honest — they are pool bookkeeping, not
# starts — but it is a third reason for a NULL on archive.start.
META_CARD_TRACK_NUMBERS = (48, 88)   # MM, Sl, T75 / KUN, JAA, CIT

# `ran` is the runner-ness vote, because a scratched horse did not start.
# `archive.start.scratched` is entry-time data and is wrong for the 175 starts
# where Heppa says `absent`; `heppa_start.absent` is right. `prev_start`
# abstains — it only supplies a date, and it is verified to list withdrawn
# entries: the 4 keys where it contradicts Heppa's `absent` all have `result`
# NULL and `placingRaw` '0' on the same track. A career line with no result
# code is not evidence of a start, so it is not offered as one.
KNOWN_STARTS = f"""
    SELECT h.canonicalKey                   AS canonicalKey,
           ca.meetDate                      AS meetDate,
           r.number                         AS raceNumber,
           NOT coalesce(s.scratched, FALSE) AS ran
    FROM archive.start s
    JOIN archive.race r ON r.raceId = s.raceId
    JOIN archive.card ca ON ca.cardId = r.cardId
    JOIN archive.horse h ON h.horseKey = s.horseKey
    WHERE coalesce(ca.trackNumber, 0) NOT IN {META_CARD_TRACK_NUMBERS}
    UNION ALL
    SELECT h.canonicalKey, hs.meetDate, hs.raceNumber,
           NOT coalesce(hs.absent, FALSE)
    FROM archive.heppa_start hs
    JOIN archive.horse h ON h.horseKey = hs.horseKey
    UNION ALL
    SELECT h.canonicalKey, p.meetDate, p.raceNumber, CAST(NULL AS BOOLEAN)
    FROM archive.prev_start p
    JOIN archive.horse h ON h.horseKey = p.horseKey
    WHERE p.result IS NOT NULL
"""

# One row per real appearance, with the days since the one before it.
#
# Deduped on (canonicalKey, meetDate, raceNumber) — the natural identity of a
# start, and the same one archive.prev_start is keyed on, for the same reason:
# heats and finals put a horse in two races on one card and that is a genuine
# zero-day gap, while a start two sources both cover is still one start. Track
# is not in the key, because a horse cannot be in two places on one day.
#
# `coalesce(bool_and(ran), TRUE)` is the vote: every source with an opinion has
# to agree the horse ran, and a key nobody has an opinion on — a prev-start
# outside the crawl window — counts. bool_and ignores NULLs, so abstention
# needs no FILTER clause.
#
# A horse's earliest known start gets NULL, not the epoch sentinel that
# archive.prev_start.startInterval carries. That sentinel exists because the old
# pipeline had nowhere to put "unknowable"; a nullable column does.
KNOWN_START_GAPS = f"""
    SELECT canonicalKey, meetDate, raceNumber,
           date_diff('day',
                     lag(CAST(meetDate AS DATE)) OVER (PARTITION BY canonicalKey
                                                       ORDER BY meetDate, raceNumber),
                     CAST(meetDate AS DATE)) AS gap
    FROM (SELECT canonicalKey, meetDate, raceNumber
          FROM ({KNOWN_STARTS})
          WHERE canonicalKey IS NOT NULL
          GROUP BY 1, 2, 3
          HAVING coalesce(bool_and(ran), TRUE))
"""

# Cleared first, for the reason RESET_HEPPA_CONTRIBUTION is: `UPDATE ... FROM`
# only touches the rows its subquery joins to, so a start that stops qualifying
# — a re-crawl finds Heppa calling it absent — would otherwise keep the gap it
# had. Two whole-table updates buy an answer that is a function of the tables
# rather than of how often this has run.
RESET_START_INTERVALS = (
    'UPDATE archive.start SET startInterval = NULL;',
    'UPDATE archive.heppa_start SET startInterval = NULL;',
)

# archive.start has no meetDate, raceNumber or trackCode of its own, so the
# join-back re-walks start -> race -> card exactly as RECOMPUTE_PREV_START_COACH
# does, and re-derives the key rather than carrying raceId through the window.
# That is deliberate: the dedup grain is coarser than (raceId, startNumber), and
# the archive has a case where one (canonicalKey, meetDate, raceNumber) covers
# two cards — 'all chance|2018' on 2024-03-24, race 8, at both Y and Kt.
# Carrying an aggregated raceId would leave one of those two rows NULL forever,
# and a key that exists only in prev_start has no raceId to carry at all.
RECOMPUTE_START_STARTINTERVAL = f"""
    UPDATE archive.start AS s
    SET startInterval = g.gap
    FROM (SELECT s2.raceId, s2.startNumber, k.gap
          FROM archive.start s2
          JOIN archive.race r ON r.raceId = s2.raceId
          JOIN archive.card ca ON ca.cardId = r.cardId
          JOIN archive.horse h ON h.horseKey = s2.horseKey
          JOIN ({KNOWN_START_GAPS}) AS k
            ON k.canonicalKey = h.canonicalKey
           AND k.meetDate = ca.meetDate
           AND k.raceNumber = r.number) AS g
    WHERE g.raceId = s.raceId
      AND g.startNumber = s.startNumber;
"""

# heppa_start carries meetDate and raceNumber itself, so only the identity has
# to be resolved. `horseKey` is NULL wherever RECOMPUTE_HEPPA_START_HORSEKEY
# never reached, and those rows cannot be placed in any career timeline: the
# inner join drops them and startInterval stays NULL. So NULL means two things
# on this table — no predecessor, or no identity — and the query that separates
# them is `WHERE horseKey IS NOT NULL AND startInterval IS NULL`.
RECOMPUTE_HEPPA_START_STARTINTERVAL = f"""
    UPDATE archive.heppa_start AS hs
    SET startInterval = g.gap
    FROM (SELECT hs2.meetDate, hs2.trackCode, hs2.raceNumber, hs2.programNumber, k.gap
          FROM archive.heppa_start hs2
          JOIN archive.horse h ON h.horseKey = hs2.horseKey
          JOIN ({KNOWN_START_GAPS}) AS k
            ON k.canonicalKey = h.canonicalKey
           AND k.meetDate = hs2.meetDate
           AND k.raceNumber = hs2.raceNumber) AS g
    WHERE g.meetDate = hs.meetDate
      AND g.trackCode = hs.trackCode
      AND g.raceNumber = hs.raceNumber
      AND g.programNumber = hs.programNumber;
"""

CREATE_INDEXES = (
    'CREATE INDEX IF NOT EXISTS idx_start_horse ON archive.start(horseKey);',
    'CREATE INDEX IF NOT EXISTS idx_race_card ON archive.race(cardId);',
    'CREATE INDEX IF NOT EXISTS idx_heppa_start_horse ON archive.heppa_start(horseId);',
)

# Columns added after the tables first shipped. `CREATE TABLE IF NOT EXISTS`
# leaves an existing archive untouched, so widening it takes an explicit ALTER;
# `IF NOT EXISTS` makes each one a no-op on a database that already has it.
ADD_COLUMNS = (
    'ALTER TABLE archive.start ADD COLUMN IF NOT EXISTS prizeWon BIGINT;',
    'ALTER TABLE archive.start ADD COLUMN IF NOT EXISTS disqualifiedCode TEXT;',
    'ALTER TABLE archive.start ADD COLUMN IF NOT EXISTS gallop BOOLEAN;',
    'ALTER TABLE archive.start ADD COLUMN IF NOT EXISTS resultSource TEXT;',
    'ALTER TABLE archive.horse ADD COLUMN IF NOT EXISTS heppaHorseId TEXT;',
    'ALTER TABLE archive.horse ADD COLUMN IF NOT EXISTS baseKey TEXT;',
    'ALTER TABLE archive.horse ADD COLUMN IF NOT EXISTS canonicalKey TEXT;',
    'ALTER TABLE archive.start ADD COLUMN IF NOT EXISTS startInterval BIGINT;',
    'ALTER TABLE archive.heppa_start ADD COLUMN IF NOT EXISTS startInterval BIGINT;',
)


@contextmanager
def db_ops(db_name):
    # DuckDB will not create a missing parent directory, and the default path
    # is under data/, which a fresh clone does not have.
    os.makedirs(os.path.dirname(db_name) or '.', exist_ok=True)
    conn = duckdb.connect(db_name)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


@contextmanager
def db_read(db_name):
    """A read-only connection, for the commands that only ever query.

    Two things `db_ops` does that a reader must not. It `makedirs` the parent
    and hands DuckDB a path, which mints a database for whatever it is given —
    the failure `require_db()` exists to stop. And a read-write connection
    holds the archive against a concurrent `parse` for as long as it is open.

    Read-only still takes a lock — DuckDB refuses a read-write open while any
    reader is attached — so this stays a per-query context manager rather than
    a connection held for the length of a UI session. Opening the 317 MB
    archive read-only measures ~6 ms, so the lock window is milliseconds and a
    `parse` can run alongside a browsing session.

    It also cannot run ADD_COLUMNS, so an archive predating a column surfaces
    it as a duckdb.BinderException at the query rather than being migrated
    underneath the reader. Callers report it; see `horse_tui`.
    """
    conn = duckdb.connect(db_name, read_only=True)
    try:
        yield conn
    finally:
        conn.close()


def _insert_many(cur, statement, rows, key):
    """Insert rows, at most one per primary key.

    Every table has a primary key, so the database already refuses a second
    row for the same key across calls, files and runs. This collapses
    duplicates *within* one batch as well, so the winning row is decided here
    rather than by DuckDB's per-statement conflict handling — last row for a
    key wins, matching `INSERT OR REPLACE`. Keep each `*_KEY` in sync with its
    table's `PRIMARY KEY` when columns move.
    """
    unique = {}
    for row in rows:
        unique[tuple(row[i] for i in key)] = row
    # DuckDB's executemany rejects an empty parameter list.
    if unique:
        cur.executemany(statement, list(unique.values()))


def create(conn):
    conn.execute(CREATE_SCHEMA)
    for statement in (CREATE_CARD_TABLE, CREATE_RACE_TABLE, CREATE_HORSE_TABLE,
                      CREATE_START_TABLE, CREATE_ODDS_TABLE, CREATE_STAT_TABLE,
                      CREATE_BETPERCENTAGE_TABLE, CREATE_PREVSTART_TABLE,
                      CREATE_HEPPA_EVENT_TABLE, CREATE_HEPPA_RACE_TABLE,
                      CREATE_HEPPA_START_TABLE, CREATE_HEPPA_HORSE_TABLE,
                      *ADD_COLUMNS, *CREATE_INDEXES):
        conn.execute(statement)


class ArchiveDb:
    """Batched upserts into the archive schema."""

    def __init__(self, conn):
        self.conn = conn

    def store_cards(self, rows):
        _insert_many(self.conn, INSERT_CARD, rows, CARD_KEY)

    def store_races(self, rows):
        _insert_many(self.conn, INSERT_RACE, rows, RACE_KEY)

    def store_horses(self, rows):
        _insert_many(self.conn, INSERT_HORSE, rows, HORSE_KEY)

    def store_starts(self, rows):
        _insert_many(self.conn, INSERT_START, rows, START_KEY)

    def store_odds(self, rows):
        _insert_many(self.conn, INSERT_ODDS, rows, ODDS_KEY)

    def store_stats(self, rows):
        _insert_many(self.conn, INSERT_STAT, rows, STAT_KEY)

    def store_betpercentages(self, rows):
        _insert_many(self.conn, INSERT_BETPERCENTAGE, rows, BETPERCENTAGE_KEY)

    def store_prevstarts(self, rows):
        _insert_many(self.conn, INSERT_PREVSTART, rows, PREVSTART_KEY)

    def store_heppa_events(self, rows):
        _insert_many(self.conn, INSERT_HEPPA_EVENT, rows, HEPPA_EVENT_KEY)

    def store_heppa_races(self, rows):
        _insert_many(self.conn, INSERT_HEPPA_RACE, rows, HEPPA_RACE_KEY)

    def store_heppa_starts(self, rows):
        _insert_many(self.conn, INSERT_HEPPA_START, rows, HEPPA_START_KEY)

    def store_heppa_horses(self, rows):
        _insert_many(self.conn, INSERT_HEPPA_HORSE, rows, HEPPA_HORSE_KEY)

    def recompute_start_intervals(self):
        self.conn.execute(RECOMPUTE_START_INTERVAL)

    def recompute_prev_start_coaches(self):
        self.conn.execute(RECOMPUTE_PREV_START_COACH)

    def recompute_auto_starts(self):
        self.conn.execute(RECOMPUTE_START_AUTOSTART)
        self.conn.execute(RECOMPUTE_PREV_START_AUTOSTART)

    def recompute_heppa_links(self):
        """Resolve horse identity between the two sources, both directions.

        Order matters: the horse-level mapping is built from the races both
        sources cover, then read back to reach the meetings only Heppa has.
        """
        self.conn.execute(RECOMPUTE_HEPPA_HORSE_ID)
        self.conn.execute(RECOMPUTE_HEPPA_START_HORSEKEY)
        self.conn.execute(RECOMPUTE_HORSE_IDENTITY)

    def recompute_start_from_heppa(self):
        """Merge the registry's finishing detail into archive.start.

        Three statements, and all three are needed for the result to be a pure
        function of the two tables rather than of how often this has run.
        """
        self.conn.execute(RESET_HEPPA_CONTRIBUTION)
        self.conn.execute(RECOMPUTE_START_FROM_HEPPA)
        self.conn.execute(LABEL_VEIKKAUS_RESULTS)

    def recompute_cross_source_intervals(self):
        """Days since each horse's previous known start, over all three sources.

        Distinct from recompute_start_intervals(), which is prev_start's own
        same-table column: this unions archive.start, archive.heppa_start and
        archive.prev_start, partitions on canonicalKey rather than horseKey, and
        leaves a horse's earliest known start NULL rather than stamping the
        epoch sentinel on it. The two can disagree for the same underlying
        start, and each is right about its own question.

        Must run after recompute_heppa_links(): canonicalKey and
        heppa_start.horseKey are both written there, and this reads both. Run it
        first instead and the column comes out almost entirely NULL, with no
        error anywhere.
        """
        for statement in RESET_START_INTERVALS:
            self.conn.execute(statement)
        self.conn.execute(RECOMPUTE_START_STARTINTERVAL)
        self.conn.execute(RECOMPUTE_HEPPA_START_STARTINTERVAL)


def query_horse(db_name: str, name: str, before: str | None = None):
    """Past performances of a horse, oldest first — the §5 join.

    `before` (yyyy-mm-dd) keeps the query time-aware: only starts a model could
    have known about before that meet date.
    """
    sql = """SELECT c.meetDate, c.trackAbbreviation, r.number, r.distance, r.startType,
                    s.startNumber, s.startTrack, s.driverName, s.placement, s.kmTime,
                    s.kmTimeMs, s.winOddsFinal, s.careerWinnings, s.startInterval
             FROM archive.start s
             JOIN archive.race r ON r.raceId = s.raceId
             JOIN archive.card c ON c.cardId = r.cardId
             JOIN archive.horse h ON h.horseKey = s.horseKey
             WHERE h.horseName = ?"""
    params = [name]
    if before:
        sql += ' AND c.meetDate < ?'
        params.append(before)
    with db_ops(db_name) as conn:
        return conn.execute(sql + ' ORDER BY c.meetDate', params).fetchall()


def query_prev_starts(db_name: str, name: str, before: str | None = None):
    """A horse's reported career line, oldest first.

    Wider than `query_horse`: `prev_start` carries a finishing position for the
    whole field, not just the paid places, so this is the past-performance
    history to build features from. `before` (yyyy-mm-dd) keeps it time-aware.
    """
    sql = """SELECT p.meetDate, p.trackCode, p.raceNumber, p.distance, p.startTrack,
                    p.driver, p.result, p.placement, p.kmTime, p.kmTimeMs,
                    p.autoStart, p.winOdd, p.firstPrize, p.startInterval, p.coachName
             FROM archive.prev_start p
             JOIN archive.horse h ON h.horseKey = p.horseKey
             WHERE h.horseName = ?"""
    params = [name]
    if before:
        sql += ' AND p.meetDate < ?'
        params.append(before)
    with db_ops(db_name) as conn:
        return conn.execute(sql + ' ORDER BY p.meetDate', params).fetchall()
