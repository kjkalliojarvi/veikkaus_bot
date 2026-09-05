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

# The multi-leg pools' betting percentages: how the T-pool money was spread
# over the runners of each leg. This is the only source for them — a runners
# payload's `betPercentages` carries the single-race pools and nothing else
# (KAK, TRO, DUO, EKS, over the whole raw zone), so archive.bet_percentage has
# never held a T-pool row. They come from /pool/{poolId}/odds, the same
# endpoint as archive.odds_snapshot above, which returns one row per (leg,
# runner) for a pool with legs instead of one per runner.
#
# `capturedAt` is deliberately *not* in the primary key, where odds_snapshot
# has it. The win pool is crawled for its history — many snapshots of one pool
# — while these are wanted final, one row per (pool, leg, runner): a re-fetch
# of a pool crawled while betting was still open replaces its figures with the
# closing ones rather than accumulating a second version beside them. The
# figures freeze once a pool has run, and stay fetchable for years (verified
# back to 2021-01-01), which is what makes this backfillable at all.
#
# netSales/netPool are pool-level and repeat on every row of the pool, as
# poolType does on odds_snapshot. Aggregate them per pool, never over rows.
CREATE_LEG_PERCENTAGE_TABLE = """
    CREATE TABLE IF NOT EXISTS archive.leg_percentage(
        poolId BIGINT,
        poolType TEXT,           -- T4, T5, T64, T65, T75 (crawler.LEG_POOL_TYPES)
        legNumber BIGINT,        -- 1..n within this pool
        raceId BIGINT,
        raceNumber BIGINT,
        startNumber BIGINT,      -- the runner's start number (payload: runnerNumber)
        runnerId BIGINT,         -- joins archive.start / archive.bet_percentage
        percentage BIGINT,       -- hundredths of a percent; sums to ~10000 per leg
        amount BIGINT,           -- money on this runner in this leg (payload `ticks` is its twin)
        winProbable BIGINT,      -- the win-pool probable, hundredths
        scratched BOOLEAN,       -- TRUE or NULL: the payload only ever sends the flag
        capturedAt BIGINT,       -- payload `updated`
        netSales BIGINT,         -- pool-level, repeated on every row of the pool
        netPool BIGINT,          -- pool-level, likewise
        placement BIGINT,        -- what this runner did: recomputed after loading
        PRIMARY KEY (poolId, legNumber, startNumber));
"""
INSERT_LEG_PERCENTAGE = ('INSERT OR REPLACE INTO archive.leg_percentage '
                         'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);')
LEG_PERCENTAGE_KEY = (0, 2, 5)  # poolId, legNumber, startNumber

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
#
# **`horseId` is in the primary key because of the starts abroad.** Heppa serves
# a Finnish horse's foreign meetings through the same per-meeting endpoints
# (`heppa.backfill_foreign`), but it gives every one of those starts
# `programNumber` '0' — Bollnas 2026-08-16 race 4 returns two Finnish horses,
# both '0'. Without `horseId` the four-column key collides, and the collision is
# silent: `_insert_many` dedupes within a batch on HEPPA_START_KEY and
# INSERT OR REPLACE dedupes across them, so one of the two starts simply is not
# there. `horseId` is non-NULL on every row the archive has ever held, and
# `programNumber = 0` occurs on no Finnish row, so both halves are safe.
#
# `finnishTrack` comes straight off the payload and is the only thing marking a
# row as a start abroad. It matters downstream: crosscheck's unmatched-meeting
# reports have to exclude these, because a foreign meeting has no Veikkaus card
# by design and 356 of them would drown the signal the report exists for.
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
        finnishTrack BOOLEAN,    -- FALSE on a start abroad; see the PK note above
        PRIMARY KEY (meetDate, trackCode, raceNumber, programNumber, horseId));
"""
INSERT_HEPPA_START = ('INSERT OR REPLACE INTO archive.heppa_start VALUES '
                      '(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '
                      '?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);')
# meetDate, trackCode, raceNumber, programNumber, horseId — horseId is index 5,
# not 4, and this tuple must stay in step with the PRIMARY KEY above.
HEPPA_START_KEY = (0, 1, 2, 3, 5)  # meetDate, trackCode, raceNumber, programNumber

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

# What each T-pool leg runner actually did, stamped onto archive.leg_percentage
# after loading. The percentages say where the money went; without this the
# question they exist to answer — how did the money do — needs a join every
# time, and that join has a trap: a window function placed after the winner
# filter sees a one-row partition and calls every winner the favourite.
#
# `archive.start.placement` is the order the pool *paid out on*, which is the
# right order for a betting question. Veikkaus wins the merge's coalesce, so
# on the 16 races where a post-race disqualification moved the official result
# this holds the payout order; heppa_start.placement holds the corrected one.
#
# It joins on (raceId, startNumber) rather than through the Heppa bridge, and
# that is what keeps the 72 meta-card pools working: a meta-card has no Heppa
# event by design, yet all 370 of its legs have a placement here.
#
# The reset is not optional. Without it a re-crawl that removes a result would
# leave the old placement behind, the same reason start_record() writes its
# derived columns NULL on every parse.
RESET_LEG_PLACEMENT = 'UPDATE archive.leg_percentage SET placement = NULL;'

RECOMPUTE_LEG_PLACEMENT = """
    UPDATE archive.leg_percentage AS lp
    SET placement = s.placement
    FROM archive.start AS s
    WHERE s.raceId = lp.raceId
      AND s.startNumber = lp.startNumber;
"""

# A leg no runner of which was placed first is a race that never ran: 139 of
# the 144 such races sit on 28 meetings Heppa flags `canceled`, and the other
# five are one card abandoned after race 3 (Lahti 2023-11-02, -2 C). Verified
# independently — not one prev_start row names those (date, track) pairs,
# against 208 for the days before them — and note archive.race.raceStatus is
# no help, reading OFFICIAL on all 262 races of the canceled cards.
#
# Dropping them leaves the invariant every statistic wants: every leg in this
# table has a winner. It removed 1,773 rows over 33 pools, every one of those
# pools whole.
#
# Leg grain, not pool grain, even though nothing in the archive straddles yet:
# a meeting abandoned mid-card could leave a pool with some legs run and some
# not, and dropping only the unrun ones is right there — percentages are per
# leg, so each surviving leg still sums to ~10000. Such a pool would then show
# fewer legs than its product name implies.
#
# Nothing is lost, and that is what makes a delete acceptable here: the raw
# payloads stay in the raw zone, `parse --full` re-inserts every dropped row,
# and this rule re-runs on every parse. So a card crawled before its results
# were published — what the two-day --to lag exists to prevent — is recovered
# rather than lost.
DROP_UNRUN_LEGS = """
    DELETE FROM archive.leg_percentage
    WHERE (poolId, legNumber) IN (
        SELECT poolId, legNumber FROM archive.leg_percentage
        GROUP BY 1, 2 HAVING count(*) FILTER (WHERE placement = 1) = 0);
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
#
# **`horseId` is in the join-back because of the starts abroad, and leaving it
# out was a real bug rather than a tidiness point.** Every foreign start in one
# race carries `programNumber` 0, so (meetDate, trackCode, raceNumber,
# programNumber) matches the whole race there, and `UPDATE ... FROM` with a
# non-unique match takes an arbitrary row of the group: horses were handed each
# other's layoffs. Observed before the fix — Combat Fighter's Solvalla start read
# 20 days where the union said 14. The join-back must use the primary key, all
# five columns of it.
RECOMPUTE_HEPPA_START_STARTINTERVAL = f"""
    UPDATE archive.heppa_start AS hs
    SET startInterval = g.gap
    FROM (SELECT hs2.meetDate, hs2.trackCode, hs2.raceNumber, hs2.programNumber,
                 hs2.horseId, k.gap
          FROM archive.heppa_start hs2
          JOIN archive.horse h ON h.horseKey = hs2.horseKey
          JOIN ({KNOWN_START_GAPS}) AS k
            ON k.canonicalKey = h.canonicalKey
           AND k.meetDate = hs2.meetDate
           AND k.raceNumber = hs2.raceNumber) AS g
    WHERE g.meetDate = hs.meetDate
      AND g.trackCode = hs.trackCode
      AND g.raceNumber = hs.raceNumber
      AND g.programNumber = hs.programNumber
      AND g.horseId = hs.horseId;
"""

# The registry's own count of a horse's career, from `/horse/{horseId}/stats`.
# One row per (horse, season, monte), plus a `year = '0'` row for the career
# total the payload calls `total`.
#
# **As-of-now, and reference-only.** This is what the registry holds today, so
# joining it to a past race leaks that race's result and every result after it —
# the rule `heppa_start.horsePriceSum` and `record` already carry, and the reason
# this endpoint went uncrawled for so long. It is here for one job: `starts` is
# the only figure that says how much of a horse's career the archive is missing,
# because Heppa counts the starts abroad that it will not enumerate. Combat
# Fighter reads 81 career starts against the 27 the archive holds, 16 of them in
# 2026 against 10 — and the rest predate its Finnish registration, so nothing
# will ever reach them.
#
# `monte` is in the key rather than a filter: monte and sulky racing keep
# separate records, and the payload ships them as separate buckets.
CREATE_HEPPA_HORSE_STAT_TABLE = """
    CREATE TABLE IF NOT EXISTS archive.heppa_horse_stat(
        horseId TEXT,
        year TEXT,               -- '0' is the career total
        monte BOOLEAN,
        starts BIGINT,
        firstPlaces BIGINT,
        secondPlaces BIGINT,
        thirdPlaces BIGINT,
        gallops BIGINT,
        disqualifications BIGINT,
        priceMoney BIGINT,       -- euros, as heppa_start.prizeWon is
        priceMoneyPerStart BIGINT,
        winningPercent BIGINT,
        placementPercent BIGINT,
        gallopPercentage BIGINT,
        disqualificationPercentage BIGINT,
        record TEXT,
        recordType TEXT,
        recordMonte BOOLEAN,
        carRecord TEXT,
        carRecordType TEXT,
        carRecordMonte BOOLEAN,
        bestRecordOfYear TEXT,
        bestRecordOfAllTime TEXT,
        PRIMARY KEY (horseId, year, monte));
"""
INSERT_HEPPA_HORSE_STAT = ('INSERT OR REPLACE INTO archive.heppa_horse_stat VALUES '
                           '(' + ', '.join('?' * 23) + ');')
HEPPA_HORSE_STAT_KEY = (0, 1, 2)  # horseId, year, monte


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
    'ALTER TABLE archive.heppa_start ADD COLUMN IF NOT EXISTS finnishTrack BOOLEAN;',
    'ALTER TABLE archive.leg_percentage ADD COLUMN IF NOT EXISTS placement BIGINT;',
)


# ADD_COLUMNS cannot change a primary key, and archive.heppa_start's gained a
# column when the crawl learned to fetch the meetings abroad. An archive built
# before that keeps the four-column key, under which two foreign starts in one
# race collide, so it is rebuilt once rather than left to lose rows quietly.
#
# The rows survive: every column already exists, only the constraint changes.
# And `finnishTrack` is stamped TRUE on what was already there, which is a fact
# rather than a default — every pre-existing row descends from a `heppa_results`
# event, and that listing is Finnish-only (0 of the 301 events of 2026 said
# otherwise). So no re-parse is needed, though one is harmless.
#
# A row with no `horseId` cannot come along, because the new key will not have a
# NULL in it, so the rebuild drops those and says how many. None of the 314,981
# rows this was written against had one; a re-parse would drop them too, for the
# same reason, so this loses nothing that could be restored.
HEPPA_START_PK = """
    SELECT constraint_column_names FROM duckdb_constraints()
    WHERE table_name = 'heppa_start' AND constraint_type = 'PRIMARY KEY'
"""

MIGRATE_HEPPA_START_PK = (
    # The index on horseId has to go first: DuckDB refuses to rename a table
    # anything depends on. create() runs CREATE_INDEXES after this, which puts
    # it straight back.
    'DROP INDEX IF EXISTS archive.idx_heppa_start_horse;',
    'ALTER TABLE archive.heppa_start RENAME TO heppa_start_old;',
    CREATE_HEPPA_START_TABLE,
    'INSERT INTO archive.heppa_start SELECT * FROM archive.heppa_start_old '
    'WHERE horseId IS NOT NULL;',
    'DROP TABLE archive.heppa_start_old;',
    'UPDATE archive.heppa_start SET finnishTrack = TRUE WHERE finnishTrack IS NULL;',
)


def migrate_heppa_start_pk(conn) -> bool:
    """Rebuild archive.heppa_start if its key predates the starts abroad.

    Returns whether it did anything, so a caller can say so. A fresh archive is
    created with the right key and skips this entirely.
    """
    key = conn.execute(HEPPA_START_PK).fetchone()
    if not key or 'horseId' in key[0]:
        return False
    dropped = conn.execute('SELECT count(*) FROM archive.heppa_start '
                           'WHERE horseId IS NULL').fetchone()[0]
    for statement in MIGRATE_HEPPA_START_PK:
        conn.execute(statement)
    if dropped:
        print(f'archive.heppa_start: rebuilt on the new primary key; {dropped} '
              'row(s) with no horseId dropped, which the new key cannot hold.')
    return True


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
                      CREATE_START_TABLE, CREATE_ODDS_TABLE,
                      CREATE_LEG_PERCENTAGE_TABLE, CREATE_STAT_TABLE,
                      CREATE_BETPERCENTAGE_TABLE, CREATE_PREVSTART_TABLE,
                      CREATE_HEPPA_EVENT_TABLE, CREATE_HEPPA_RACE_TABLE,
                      CREATE_HEPPA_START_TABLE, CREATE_HEPPA_HORSE_TABLE,
                      CREATE_HEPPA_HORSE_STAT_TABLE, *ADD_COLUMNS):
        conn.execute(statement)
    # Between the columns and the indexes, and both halves of that matter: the
    # rebuild copies every column, so it needs ADD_COLUMNS to have run, and it
    # renames the table, which DuckDB refuses while an index depends on it.
    migrate_heppa_start_pk(conn)
    for statement in CREATE_INDEXES:
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

    def store_leg_percentages(self, rows):
        _insert_many(self.conn, INSERT_LEG_PERCENTAGE, rows, LEG_PERCENTAGE_KEY)

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

    def store_heppa_horse_stats(self, rows):
        _insert_many(self.conn, INSERT_HEPPA_HORSE_STAT, rows, HEPPA_HORSE_STAT_KEY)

    def recompute_start_intervals(self):
        self.conn.execute(RECOMPUTE_START_INTERVAL)

    def recompute_prev_start_coaches(self):
        self.conn.execute(RECOMPUTE_PREV_START_COACH)

    def recompute_leg_placements(self) -> int:
        """Stamp each T-pool leg runner's finish, and drop the legs that never ran.

        Must run after recompute_start_from_heppa(): that merge is what puts
        the whole field into archive.start.placement, and two thirds of the
        placements here come from Heppa — 56% of leg rows finished 4th or
        worse, which Veikkaus never publishes. Run it before the merge and the
        column is 1st-to-3rd only.

        Returns the number of rows dropped, which the caller prints: a silent
        delete is exactly what would read as data having gone missing.
        """
        self.conn.execute(RESET_LEG_PLACEMENT)
        self.conn.execute(RECOMPUTE_LEG_PLACEMENT)
        before = self.conn.execute('SELECT count(*) FROM archive.leg_percentage').fetchone()[0]
        self.conn.execute(DROP_UNRUN_LEGS)
        after = self.conn.execute('SELECT count(*) FROM archive.leg_percentage').fetchone()[0]
        return before - after

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
