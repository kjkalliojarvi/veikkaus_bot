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
        PRIMARY KEY (horseKey));
"""
INSERT_HORSE = 'INSERT OR REPLACE INTO archive.horse VALUES (?, ?, ?, ?, ?, ?, ?, ?);'
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
        PRIMARY KEY (raceId, startNumber));
"""
INSERT_START = ('INSERT OR REPLACE INTO archive.start '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '
                '?, ?, ?, ?);')
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
        PRIMARY KEY (meetDate, trackCode, raceNumber, programNumber));
"""
INSERT_HEPPA_START = ('INSERT OR REPLACE INTO archive.heppa_start VALUES '
                      '(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '
                      '?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);')
HEPPA_START_KEY = (0, 1, 2, 3)  # meetDate, trackCode, raceNumber, programNumber

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
# The bridge itself, kept separate so that `crosscheck.py` validates the very
# join the merge relies on rather than a hand-copied lookalike.
HEPPA_START_BRIDGE = """
    FROM archive.heppa_start h
    JOIN archive.card ca ON ca.meetDate = h.meetDate
                        AND upper(ca.trackAbbreviation) = h.trackCode
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
                      CREATE_HEPPA_START_TABLE,
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

    def recompute_start_from_heppa(self):
        """Merge the registry's finishing detail into archive.start.

        Three statements, and all three are needed for the result to be a pure
        function of the two tables rather than of how often this has run.
        """
        self.conn.execute(RESET_HEPPA_CONTRIBUTION)
        self.conn.execute(RECOMPUTE_START_FROM_HEPPA)
        self.conn.execute(LABEL_VEIKKAUS_RESULTS)


def query_horse(db_name: str, name: str, before: str | None = None):
    """Past performances of a horse, oldest first — the §5 join.

    `before` (yyyy-mm-dd) keeps the query time-aware: only starts a model could
    have known about before that meet date.
    """
    sql = """SELECT c.meetDate, c.trackAbbreviation, r.number, r.distance, r.startType,
                    s.startNumber, s.startTrack, s.driverName, s.placement, s.kmTime,
                    s.kmTimeMs, s.winOddsFinal, s.careerWinnings
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
