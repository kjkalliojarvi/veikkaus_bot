"""Raw zone -> archive tables (strategy §4: fetching and parsing stay separate).

Nothing here touches the network. The manifest says which responses were
fetched and where they were stored; this walks those files and upserts them.
Re-running it over the same raw zone produces the same database, so a parsing
bug is always fixable without re-crawling.
"""
from datetime import datetime
import re

from . import archive_db
from .archive_db import ArchiveDb, db_ops
from .crawler import Manifest
from .fetcher import read_raw
from .models import (Card, HeppaEvent, HeppaHorse, HeppaHorseStats, HeppaRaceEntry,
                     HeppaStart, Race, Runner, Stat)


FLUSH = 5000

# Finnish km-time notation. The leading minute is conventionally dropped
# ('24,9a' is 1.24,9 driven from an auto start); slower times spell it out
# ('2.05,0'). A trailing letter group carries start-type and equipment markers,
# and monté times take a leading 'm' ('m19,6') — race.monte says the same thing.
KM_TIME_RE = re.compile(r'^([a-zäöå]*)(?:(\d+)[.:])?(\d{1,2}),(\d)\s*([a-zäöå]*)$',
                        re.IGNORECASE)


def normalize_name(name: str) -> str:
    """Fold a horse name to its comparison form.

    The `*` marker (registered abroad) is dropped, but a country tag such as
    `(SE)` is kept: it is what tells an import apart from a same-named
    domestic horse.
    """
    return ' '.join(name.replace('*', '').split()).casefold()


def horse_key(name: str, birth_year: int | None) -> str:
    """Stable-enough horse identity (strategy §5).

    The API exposes no registration number, so identity is name + birth year.
    Collisions are possible and Hippos's Heppa registry is the authority when
    one has to be resolved — `archive.horse.heppaHorseId` now carries it, and
    `canonicalKey` says which of these keys are one horse.
    """
    return f'{normalize_name(name)}|{birth_year if birth_year else ""}'


COUNTRY_TAG_RE = re.compile(r'\s*\(([a-z]{2,3})\)\s*$', re.IGNORECASE)


def strip_import_markers(name: str) -> str:
    """Drop the import markers Veikkaus writes inconsistently.

    The same horse arrives as 'Humble Stance', 'Humble Stance* (FR)' and
    'Humble Stance FR* (FR)'. Two markers are in play: a parenthesised country
    tag, and the country letter that the Nordic convention appends to an
    import's registered name ('Morell S (SE)', 'Black Swan N (NO)').

    The trailing token is only removed when it *agrees* with the tag — equal to
    it, or its first letter. That is what tells a country marker apart from a
    stable suffix, and those are common and are genuinely part of the name:
    'Birbone OK (IT)', 'Vulcano OP (IT)', 'Remington XO', "Aurelia's Pearl KS".
    Stripping a trailing letter unconditionally would merge horses that only
    share a stem.

    A token with no tag to agree with is therefore kept — 'Pompom S*' stays
    distinct from 'Pompom* (SE)' here. That pair is one horse, but it is the
    registry id that says so, not this function.
    """
    match = COUNTRY_TAG_RE.search(name)
    if not match:
        return name
    tag = match.group(1).upper()
    stem = name[:match.start()].replace('*', '').strip()
    parts = stem.split()
    if len(parts) > 1:
        last = parts[-1].upper()
        if last == tag or (len(last) == 1 and tag.startswith(last)):
            stem = ' '.join(parts[:-1])
    return stem


def base_horse_key(name: str, birth_year: int | None) -> str:
    """Horse identity with the import markers removed (strategy §5).

    Only used where no registry id reached the horse — see
    archive_db.RECOMPUTE_HORSE_IDENTITY, which prefers the id precisely because
    base names *can* repeat across origin countries even though they never
    repeat within one.
    """
    return horse_key(strip_import_markers(name), birth_year)


def parse_km_time(text: str | None) -> tuple[int | None, bool]:
    """'24,9a' -> (84900, True). Returns (None, False) if it does not parse."""
    if not text:
        return None, False
    match = KM_TIME_RE.match(text.strip())
    if not match:
        return None, False
    _prefix, minutes, seconds, tenths, suffix = match.groups()
    total = (int(minutes) if minutes else 1) * 60000 + int(seconds) * 1000 + int(tenths) * 100
    return total, suffix.lower().startswith('a')


def parse_result(text: str | None) -> int | None:
    """A prev-start `result` code to a finishing position, when it is one.

    Placings come through as plain numbers, well past the third place that
    `archive.start` stops at. Everything else is a Finnish outcome code —
    `kl` (koelähtö, a qualifying start), `k` (keskeytti, did not finish),
    `hpl`/`hll`/`hlo4` (hylätty, disqualified) — which has no position and is
    kept verbatim in the `result` column instead.
    """
    if text is None:
        return None
    text = text.strip()
    return int(text) if text.isdigit() else None


def parse_meet_date(short_meet_date: str | None) -> str | None:
    """'22.12.24' -> '2024-12-22'.

    Deliberately not derived from the sibling `meetDate` field: that is
    midnight Finnish time expressed in UTC, so its date component is a day
    early on every single row. This one matches `archive.card.meetDate`.
    """
    if not short_meet_date:
        return None
    try:
        return datetime.strptime(short_meet_date.strip(), '%d.%m.%y').date().isoformat()
    except ValueError:
        return None


def parse_win_odd(text: str | None) -> int | None:
    """The API sends a prev-start's win odd as a digit string of hundredths."""
    if not text or not text.strip().isdigit():
        return None
    return int(text.strip())


def parse_tote_result(text: str | None) -> list[int]:
    """'6-3-8' -> [6, 3, 8], the start numbers of the paid places in order."""
    if not text:
        return []
    return [int(part) for part in text.split('-') if part.strip().isdigit()]


# --- Heppa ------------------------------------------------------------------
#
# Heppa sends every scalar as a string, and uses '-' for "no value" in several
# places, so each of these has to decide what an absent field looks like rather
# than leaning on a type.
HEPPA_INT_RE = re.compile(r'^-?\d+$')


def parse_heppa_int(text: str | None) -> int | None:
    """A Heppa numeric string to an int. '-' and '' mean absent.

    The sign matters: `temperature` goes negative on a winter card.
    """
    if text is None:
        return None
    text = text.strip()
    return int(text) if HEPPA_INT_RE.match(text) else None


def parse_placing(text: str | None) -> int | None:
    """A Heppa `placing` to a finishing position, when it is one.

    Three regimes, all observed:

    - `'0'` — no placing at all: the horse was absent, or was disqualified
      under `hpl`/`hll`/`hrp`/`k`.
    - `'1'`-`'13'` — a real finishing position, for the whole field rather than
      just the paid places. This is the value that fills `archive.start`.
    - `'105'`, `'108'`, `'110'` — 100 + the position the horse crossed the line
      in, always alongside `disqualifiedCode='hlo'`. A disqualified horse holds
      no position, so this returns None and the code survives in `placingRaw`.
      This is the same information `archive.prev_start` already carries as
      `hlo4`, and the same treatment: `parse_result()` leaves those NULL too.
      Filling `placement` from it would put two horses in the same position in
      one race and break the placings-are-unique check of strategy §8.
    """
    value = parse_heppa_int(text)
    if value is None or value <= 0 or value >= 100:
        return None
    return value


def parse_heppa_km_time(text: str | None) -> int | None:
    """'1.18.8' -> 78800 ms, the long form of a km time.

    Only needed where `shortKilometerTime` ('18,8', which `parse_km_time`
    already reads) is absent. Unlike the short form this one is unambiguous —
    the minute is always spelled out — so there is no start-type suffix to
    read, and `distanceCode` carries that instead.
    """
    if not text:
        return None
    parts = text.strip().split('.')
    if len(parts) != 3 or not all(HEPPA_INT_RE.match(p) for p in parts):
        return None
    minutes, seconds, tenths = (int(p) for p in parts)
    return minutes * 60000 + seconds * 1000 + tenths * 100


def parse_heppa_odds(text: str | None) -> int | None:
    """'4.44' -> 444. Heppa sends a decimal where the rest of the archive
    stores hundredths, matching `probable` and `prev_start.winOdd`.
    """
    if not text:
        return None
    try:
        return round(float(text.strip()) * 100)
    except ValueError:
        return None


def blank_to_none(text: str | None) -> str | None:
    """Heppa's placeholder for an absent value is '-', not an empty field.

    Kept out of the tables: a horse with no known breeder is not a horse bred
    by '-', and letting the placeholder through would make every count of
    "how many do we know" wrong.
    """
    if text is None:
        return None
    text = text.strip()
    return text or None if text != '-' else None


def parse_heppa_auto_start(distance_code: str | None) -> bool | None:
    """Auto start from a Heppa start's `distanceCode` ('ke'/'ake'/'ly'/'aly').

    An 'a' prefix is the auto start, the same convention the km-time suffix
    uses elsewhere in the archive. Note this is *not* readable from the race's
    `startForm`: TASOITUSAJO/RYHMALAHTO is handicap-versus-group, a different
    axis from CAR/VOLT. Unknown stays NULL rather than defaulting to volt.
    """
    if not distance_code:
        return None
    return distance_code.strip().lower().startswith('a')


def card_record(card: Card) -> tuple:
    """Column order must stay in sync with archive_db.INSERT_CARD."""
    return (card.cardId,
            card.country,
            card.meetDate.isoformat(),
            card.trackAbbreviation,
            card.trackName,
            card.trackNumber,
            card.raceType,
            card.firstRaceStart,
            card.lunchRaces,
            card.mainPerformance,
            card.cancelled)


def race_record(race: Race) -> tuple:
    """Column order must stay in sync with archive_db.INSERT_RACE."""
    return (race.raceId,
            race.cardId,
            race.number,
            race.startTime,
            race.distance,
            race.startType,
            race.monte,
            race.firstPrize,
            race.breed,
            race.seriesSpecification,
            race.raceStatus,
            race.raceRider,
            race.trackProfile,
            race.toteResultString,
            race.intermediateTimesString)


def birth_year(runner: Runner) -> int | None:
    """None on the oldest cards, which sometimes carry no birth date at all."""
    return runner.birthDate.year if runner.birthDate else None


def is_placeholder(runner: Runner) -> bool:
    """True for the `Poissa` ("absent") runner the API sends for a vacated start
    number. It is not a horse: it is always scratched, carries no `coachName` at
    all, and its `horseAge` is arbitrary (0–24 observed across 19 of them), so
    it would otherwise land one junk `archive.horse` row per distinct age —
    colliding across tracks and dates — plus a start with nothing in it.

    `driverName` is not part of the test: it comes back as 'Poissa', '- -',
    'Poissa Poissa' or empty.
    """
    return normalize_name(runner.horseName) == 'poissa' and runner.coachName is None


def horse_record(runner: Runner) -> tuple:
    """Column order must stay in sync with archive_db.INSERT_HORSE.

    `heppaHorseId` is NULL here and recomputed from the races both sources
    cover — the Veikkaus API exposes no registration number at all.
    """
    return (horse_key(runner.horseName, birth_year(runner)),
            runner.horseName,
            birth_year(runner),
            runner.gender,
            runner.sire,
            runner.dam,
            runner.damSire,
            None,    # heppaHorseId — see archive_db.RECOMPUTE_HEPPA_HORSE_ID
            base_horse_key(runner.horseName, birth_year(runner)),
            None)    # canonicalKey — see archive_db.RECOMPUTE_HORSE_IDENTITY


def start_record(runner: Runner, result: dict | None, win_odds: int | None) -> tuple:
    """One row of `archive.start`.

    The last five columns are left NULL here and recomputed after loading —
    four from `heppa_start` (archive_db.RECOMPUTE_START_FROM_HEPPA) and
    `startInterval` from all three start-bearing tables at once
    (archive_db.RECOMPUTE_START_STARTINTERVAL). Writing them NULL on every
    parse is deliberate: it means a re-crawl of the Veikkaus half cannot leave
    a stale derived value behind, since the recomputes re-derive every one of
    them from scratch at the end of every run.

    Column order must stay in sync with archive_db.INSERT_START.
    """
    result = result or {}
    km_time = result.get('kmTime')
    km_time_ms, auto_start = parse_km_time(km_time)
    return (runner.raceId,
            runner.startNumber,
            runner.runnerId,
            horse_key(runner.horseName, birth_year(runner)),
            runner.horseName,
            runner.driverName,
            runner.coachName,
            runner.ownerName,
            runner.ownerHomeTown,
            runner.startTrack,
            runner.distance,
            runner.frontShoes,
            runner.rearShoes,
            runner.specialCart,
            runner.handicapRating,
            runner.scratched,
            runner.prize,  # career earnings; equals stats.total.winMoney where both exist
            result.get('placement'),
            km_time,
            km_time_ms,
            # Provisional: the km-time suffix only speaks for horses that
            # recorded a time. recompute_auto_starts() then sets this from
            # race.startType, which covers every runner.
            auto_start if km_time_ms else None,
            win_odds if win_odds is not None else result.get('winOdds'),
            None,    # prizeWon         \
            None,    # disqualifiedCode  } from archive.heppa_start, after loading
            None,    # gallop           /
            None,    # resultSource — which source ended up supplying placement
            None)    # startInterval — see archive_db.KNOWN_START_GAPS


def stat_records(runner: Runner) -> list[tuple]:
    """One record per statistics period. Empty for historical runners.

    Column order must stay in sync with archive_db.INSERT_STAT.
    """
    records = []
    for period in ('currentYear', 'previousYear', 'total'):
        raw = runner.stats.get(period)
        if raw is None:
            continue
        stat = Stat(**raw)
        records.append((runner.runnerId,
                        period,
                        stat.year,
                        stat.record1,
                        stat.record2,
                        stat.starts,
                        stat.position1,
                        stat.position2,
                        stat.position3,
                        stat.places,
                        stat.winMoney,
                        stat.gallopPercent,
                        stat.disqualificationPercent,
                        stat.placementPercent,
                        stat.winningPercent))
    return records


def betpercentage_records(runner: Runner) -> list[tuple]:
    """One record per pool type (e.g. KAK, T5). Percentages are hundredths of
    a percent — 939 is 9.39 %.

    Column order must stay in sync with archive_db.INSERT_BETPERCENTAGE.
    """
    return [(runner.runnerId, pool_type, values.get('percentage'))
            for pool_type, values in (runner.betPercentages or {}).items()]


def prevstart_records(runner: Runner) -> list[tuple]:
    """A horse's earlier starts, one record each. Empty for historical runners.

    Attached to the horse rather than to the runner reporting them, and
    identified by (horse, meet date, race number) so that the same start
    collapses onto one row however many later races re-report it. Entries
    missing any of those three cannot be placed in a career and are skipped —
    the caller reports the count.

    `startInterval` and `coachName` are left NULL here and filled in over the
    whole table once loading finishes — see archive_db.RECOMPUTE_START_INTERVAL
    and RECOMPUTE_PREV_START_COACH. Note in particular that `coachName` is
    *not* taken from `runner`: that is the trainer at a later race.

    Column order must stay in sync with archive_db.INSERT_PREVSTART.
    """
    key = horse_key(runner.horseName, birth_year(runner))
    records = []
    for start in runner.prevStarts:
        meet_date = parse_meet_date(start.shortMeetDate)
        if meet_date is None or start.raceNumber is None:
            continue
        km_time_ms, auto_start = parse_km_time(start.kmTime)
        records.append((start.priorStartId,
                        key,
                        meet_date,
                        start.meetDate,
                        start.trackCode,
                        start.trackName,
                        start.raceNumber,
                        start.distance,
                        start.startTrack,
                        start.driver,
                        start.driverFullName,
                        start.firstPrize,
                        start.result,
                        parse_result(start.result),
                        start.kmTime,
                        km_time_ms,
                        auto_start if km_time_ms else None,
                        parse_win_odd(start.winOdd),
                        start.frontShoes,
                        start.rearShoes,
                        start.shoesType,
                        start.headGear,
                        start.specialCart,
                        start.raceStartType,
                        start.raceRiderType,
                        start.trackProfileType,
                        start.raceSurface,
                        start.resultsAvailable,
                        None,    # startInterval — recomputed after loading
                        None))   # coachName — sourced from archive.start, ditto
    return records


def heppa_event_record(event: HeppaEvent) -> tuple:
    """One race meeting from the Heppa results listing.

    Cancelled meetings are kept — a meeting that did not happen is a fact worth
    recording, and `canceled` says so. They simply have no races under them.

    Column order must stay in sync with archive_db.INSERT_HEPPA_EVENT.
    """
    return (event.date,
            event.trackCode,
            parse_heppa_int(event.trackNumber),
            event.trackShortname,
            event.trackName,
            event.trackCity,
            event.eventType,
            event.name,
            event.startTime,
            parse_heppa_int(event.meetNumber),
            event.trackType,
            event.trackCondition,
            parse_heppa_int(event.temperature),
            event.specialRaceEventName,
            event.majorRace,
            event.canceled)


def heppa_race_record(entry: HeppaRaceEntry) -> tuple:
    """One race. `intermediateTime` sits on the entry, not on the race.

    Column order must stay in sync with archive_db.INSERT_HEPPA_RACE.
    """
    race = entry.race
    return (race.date,
            race.trackCode,
            parse_heppa_int(race.startNumber),  # Heppa's startNumber is the race number
            race.raceName,
            parse_heppa_int(race.categoryNumber),
            race.plannedTime,
            race.actualTime,
            race.startForm,
            race.monte,
            race.eventType,
            parse_heppa_int(race.baseDistance),
            race.levellingHeader,
            parse_heppa_int(race.firstPrice),
            parse_heppa_int(race.priceSum),
            race.status,
            entry.intermediateTime)


def heppa_start_record(start: HeppaStart) -> tuple:
    """One horse in one race, from the official registry.

    The km time is read from `shortKilometerTime` where it exists, because that
    is byte-identical to the notation `archive.start.kmTime` already uses and
    `parse_km_time` already handles; `kilometerTime` is the long-form fallback.
    The auto-start flag deliberately does *not* come from that time's suffix —
    `distanceCode` carries it for every horse, including those with no time.

    `finnishTrack` is the payload's own flag and the only thing distinguishing a
    start abroad, which `heppa.backfill_foreign` fetches from the same
    endpoints. It is what `archive.heppa_start.horseId` joined the primary key
    for: Heppa numbers every foreign start `programNumber` '0'.

    `horseKey` is NULL here: a Heppa start carries no birth year, so identity
    cannot be computed from it. archive_db.RECOMPUTE_HEPPA_START_HORSEKEY
    fills it through the registry id instead. `startInterval` is NULL for the
    same sort of reason — it is a fact about a career, not about a payload —
    and archive_db.RECOMPUTE_HEPPA_START_STARTINTERVAL fills it.

    Column order must stay in sync with archive_db.INSERT_HEPPA_START.
    """
    km_time = start.shortKilometerTime
    km_time_ms = parse_km_time(km_time)[0]
    if km_time_ms is None:
        km_time_ms = parse_heppa_km_time(start.kilometerTime)
    return (start.date,
            start.trackCode,
            parse_heppa_int(start.startNumber),    # the race number
            parse_heppa_int(start.programNumber),  # the horse's start number
            None,                                  # horseKey — recomputed
            start.horseId,
            start.horseName,
            start.horseBreed,
            start.horseRegistrationCountry,
            parse_heppa_int(start.lane),
            parse_heppa_int(start.distance),
            start.distanceCode,
            start.placing,
            parse_placing(start.placing),
            start.disqualifiedCode,
            start.gallop,
            start.absent,
            km_time,
            km_time_ms,
            parse_heppa_auto_start(start.distanceCode),
            start.totalTime,
            parse_heppa_int(start.price),
            parse_heppa_odds(start.winOdds),
            parse_heppa_int(start.horsePriceSum),
            start.driverId,
            start.driverName,
            start.driverFirstName,
            start.driverLastName,
            start.originalDriverId,
            start.trainerId,
            start.trainerName,
            start.ownerName,
            start.ownerCity,
            start.shoesFront,
            start.shoesBack,
            start.americanSulkyKEX,
            start.record,
            start.recordType,
            start.monte,
            start.status,
            start.commentText,
            None,    # startInterval — see archive_db.KNOWN_START_GAPS
            start.finnishTrack)


def heppa_horse_record(horse: HeppaHorse) -> tuple:
    """One horse's registry record.

    `sire` and `dam` arrive as nested objects and are flattened to id, name and
    registration number — one generation, which is all this endpoint carries.

    Heppa writes '-' for an absent value in most of these fields, and a '-'
    breeder is not a breeder called '-'.

    Column order must stay in sync with archive_db.INSERT_HEPPA_HORSE.
    """
    sire = horse.sire or {}
    dam = horse.dam or {}
    return (horse.id,
            blank_to_none(horse.name),
            blank_to_none(horse.birthDate),
            horse.birthDateAccurate,
            blank_to_none(horse.registerNo),
            blank_to_none(horse.ueln),
            blank_to_none(horse.chipNo),
            horse.dead,
            horse.registrationSuspended,
            blank_to_none(horse.species),
            blank_to_none(horse.breedCode),
            blank_to_none(horse.breedFinName),
            blank_to_none(horse.gender),
            blank_to_none(horse.color),
            blank_to_none(horse.birthCountry),
            blank_to_none(horse.birthCountryName),
            blank_to_none(horse.birthPlace),
            blank_to_none(horse.origin),
            blank_to_none(horse.registrationCountry),
            blank_to_none(horse.breedingUnion),
            blank_to_none(horse.breederName),
            blank_to_none(horse.ownerName),
            blank_to_none(horse.trainerId),
            blank_to_none(horse.trainerName),
            blank_to_none(horse.homeTrackName),
            blank_to_none(horse.homeTrackCity),
            blank_to_none(horse.bestRecord),
            blank_to_none(sire.get('id')),
            blank_to_none(sire.get('name')),
            blank_to_none(sire.get('registerNo')),
            blank_to_none(dam.get('id')),
            blank_to_none(dam.get('name')),
            blank_to_none(dam.get('registerNo')))


def heppa_horse_stat_records(stats: HeppaHorseStats) -> list[tuple]:
    """Every row of one horse's registry statistics — seasons and totals, both
    gaits.

    Four buckets become one table: `stats`/`total` under the sulky and
    `monteStats`/`monteTotal` in monte, told apart by the `monte` column rather
    than by which list they came from, because that is what a query wants. The
    totals keep the payload's own `year` of '0'.

    A line with no year is dropped: `year` is a third of the primary key, and a
    row that cannot say which season it counts is not a fact about anything.

    Column order must stay in sync with archive_db.INSERT_HEPPA_HORSE_STAT.
    """
    rows = []
    for monte, lines in ((False, [stats.total, *stats.stats]),
                         (True, [stats.monteTotal, *stats.monteStats])):
        for line in lines:
            if line is None or line.year is None:
                continue
            rows.append((stats.id,
                         line.year,
                         monte,
                         parse_heppa_int(line.starts),
                         parse_heppa_int(line.firstPlaces),
                         parse_heppa_int(line.secondPlaces),
                         parse_heppa_int(line.thirdPlaces),
                         parse_heppa_int(line.gallops),
                         parse_heppa_int(line.disqualifications),
                         parse_heppa_int(line.priceMoney),
                         parse_heppa_int(line.priceMoneyPerStart),
                         parse_heppa_int(line.winningPercent),
                         parse_heppa_int(line.placementPercent),
                         parse_heppa_int(line.gallopPercentage),
                         parse_heppa_int(line.disqualificationPercentage),
                         blank_to_none(line.record),
                         blank_to_none(line.recordType),
                         line.recordMonte,
                         blank_to_none(line.carRecord),
                         blank_to_none(line.carRecordType),
                         line.carRecordMonte,
                         blank_to_none(line.bestRecordOfYear),
                         blank_to_none(line.bestRecordOfAllTime)))
    return rows


def _flush(store, rows, force=False):
    if rows and (force or len(rows) >= FLUSH):
        store(rows)
        rows.clear()


def _read(raw_root: str, task):
    payload = read_raw(raw_root, task.rawPath)
    if payload is None:
        print(f'missing raw file: {task.rawPath}')
    return payload


def _each_payload(manifest: Manifest, raw_root: str, endpoint_type: str,
                  consumed: list | None = None, unparsed_only: bool = True):
    """Yield (task, payload) for the archived responses of one endpoint type.

    By default only those a parse still owes work on. Tasks are appended to
    `consumed` *after* their body has run, so the caller can stamp them once
    the phase has flushed — see Manifest.mark_parsed.
    """
    for task in manifest.done(endpoint_type, unparsed_only):
        payload = _read(raw_root, task)
        if payload is None:
            continue
        yield task, payload
        if consumed is not None:
            consumed.append(task)


def _payloads_for(manifest: Manifest, raw_root: str, endpoint_type: str, keep):
    """Archived responses whose *task* `keep` accepts, parsed or not.

    For the two lookups that have to reach payloads outside the current unit of
    work — a runners payload needs its race's results however long ago those
    were loaded. Filtering on the task avoids reading the file to reject it.
    """
    for task in manifest.done(endpoint_type):
        if not keep(task):
            continue
        payload = _read(raw_root, task)
        if payload is not None:
            yield task, payload


def _results_map(manifest: Manifest, raw_root: str, runner_tasks: list) -> dict:
    """(raceId, startNumber) -> placement / km time / final win odds.

    Only the first three finishers appear here at all; the API publishes no
    full finishing order (see §2b of the strategy doc). Two layers, because
    neither is complete on its own:

    1. `race.toteResultString` names the placed start numbers even when a pool
       paid fewer than three places (a Sija pool that paid two, say).
    2. The results payload adds km time and the final win odd, but only for
       the runners a pool actually paid out on.

    Scoped to the races whose runners are actually being parsed, which on an
    incremental run is a dozen cards rather than the whole archive. It has to
    reach *parsed* payloads too — a newly loaded runners payload still needs
    its race's results, which were loaded long ago — so this reads by task
    rather than by what is outstanding. Both ids are already on the manifest
    row: a runners task is keyed by raceId and carries its cardId.
    """
    race_ids = {int(task.entityId) for task in runner_tasks}
    card_ids = {task.cardId for task in runner_tasks if task.cardId is not None}
    out: dict[tuple[int, int], dict] = {}
    if not race_ids:
        return out

    for _, payload in _payloads_for(manifest, raw_root, 'races',
                                    lambda t: t.cardId in card_ids):
        for race in payload.get('collection', []):
            if race['raceId'] not in race_ids:
                continue
            placed = parse_tote_result(race.get('toteResultString'))
            for placement, start_number in enumerate(placed, start=1):
                out.setdefault((race['raceId'], start_number), {})['placement'] = placement

    for _, payload in _payloads_for(manifest, raw_root, 'results',
                                    lambda t: int(t.entityId) in race_ids):
        race_id = payload.get('raceId')
        if race_id is None:
            continue
        for row in payload.get('results', []):
            if row.get('poolType') not in ('VOI', 'SIJ'):
                continue
            start_number = row.get('startNumber')
            if start_number is None:
                continue
            entry = out.setdefault((race_id, start_number), {})
            if row.get('position') is not None:
                entry['placement'] = row['position']
            if row.get('kmTime'):
                entry['kmTime'] = row['kmTime']
            # Only the win pool's `probable` is a win odd; Sija pays place odds.
            if row['poolType'] == 'VOI' and row.get('probable') is not None:
                entry['winOdds'] = row['probable']
    return out


UNKNOWN_CAPTURE_TIME = 0


def _captured_at(payload: dict, pool_start_time: int | None) -> int:
    """When an odds payload was published.

    `capturedAt` is what separates two snapshots of the same pool and runner,
    so it is part of the primary key and cannot be NULL. Old payloads drop
    `updated` (and `updatedString` with it) — seen on 2005 cards — so fall back
    to the pool's own race start time, which dates the snapshot closely enough
    for a backfill, where there is exactly one fetch per pool anyway. Failing
    that, an explicit epoch sentinel: unknown, but still addressable, and not a
    silently plausible timestamp.
    """
    updated = payload.get('updated')
    if updated is not None:
        return updated
    if pool_start_time is not None:
        return pool_start_time
    return UNKNOWN_CAPTURE_TIME


def _store_odds(manifest: Manifest, raw_root: str, db: ArchiveDb,
                unparsed_only: bool = True) -> int:
    """Load odds snapshots. Only present when the crawl ran with --odds.

    The pool -> race mapping lives in the sibling pools payload, which will
    usually already be parsed, so it is read by card rather than by what is
    outstanding — both task types carry `cardId`.
    """
    odds_tasks = manifest.done('odds', unparsed_only)
    if not odds_tasks:
        return 0
    cards = {task.cardId for task in odds_tasks if task.cardId is not None}

    pool_race, pool_time = {}, {}
    for task, payload in _payloads_for(manifest, raw_root, 'pools',
                                       lambda t: t.cardId in cards):
        for pool in payload.get('collection', []):
            pool_race[pool['poolId']] = int(task.entityId)
            pool_time[pool['poolId']] = pool.get('firstRaceStartTime')

    consumed, rows = [], []
    count = 0
    for task, payload in _each_payload(manifest, raw_root, 'odds', consumed, unparsed_only):
        pool_id = payload.get('poolId')
        race_id = pool_race.get(pool_id)
        if race_id is None:
            continue
        captured_at = _captured_at(payload, pool_time.get(pool_id))
        for odd in payload.get('odds', []):
            start_number = odd.get('runnerNumber')
            if start_number is None:
                continue
            rows.append((pool_id,
                         race_id,
                         start_number,
                         captured_at,
                         payload.get('poolType'),
                         odd.get('probable'),
                         odd.get('amount'),
                         bool(odd.get('scratched', False))))
        count += 1
        _flush(db.store_odds, rows)
    _flush(db.store_odds, rows, force=True)
    manifest.mark_parsed(consumed)
    return count


# The final win odd is whatever the win pool last published for a runner.
# Reading it back from the stored snapshots rather than rebuilding it while
# loading them decouples `archive.start` from which odds payloads happen to be
# outstanding — and is the more defensible answer besides: latest `capturedAt`
# wins, deterministically, where an in-memory map kept whichever payload was
# iterated last.
FINAL_WIN_ODDS = """
    SELECT raceId, startNumber, probable FROM (
        SELECT raceId, startNumber, probable,
               row_number() OVER (PARTITION BY raceId, startNumber
                                  ORDER BY capturedAt DESC) AS rn
        FROM archive.odds_snapshot
        WHERE poolType = 'VOI' AND probable IS NOT NULL)
    WHERE rn = 1
"""


def _final_win_odds(db: ArchiveDb) -> dict:
    """(raceId, startNumber) -> final win odd, from archive.odds_snapshot."""
    return {(race_id, start_number): probable
            for race_id, start_number, probable in db.conn.execute(FINAL_WIN_ODDS).fetchall()}


def _parse_cards(manifest: Manifest, raw_root: str, db: ArchiveDb, country: str,
                 unparsed_only: bool = True) -> int:
    rows, consumed = [], []
    count = 0
    for task, payload in _each_payload(manifest, raw_root, 'cards_date', consumed,
                                       unparsed_only):
        for raw in payload.get('collection', []):
            if raw.get('country') != country:
                continue
            try:
                rows.append(card_record(Card(**raw)))
                count += 1
            except Exception as e:
                print(f'{task.rawPath}: card {raw.get("cardId")}: {e}')
        _flush(db.store_cards, rows)
    _flush(db.store_cards, rows, force=True)
    manifest.mark_parsed(consumed)
    return count


def _parse_races(manifest: Manifest, raw_root: str, db: ArchiveDb,
                 unparsed_only: bool = True) -> int:
    rows, consumed = [], []
    count = 0
    for task, payload in _each_payload(manifest, raw_root, 'races', consumed, unparsed_only):
        for raw in payload.get('collection', []):
            try:
                rows.append(race_record(Race(**raw)))
                count += 1
            except Exception as e:
                print(f'{task.rawPath}: race {raw.get("raceId")}: {e}')
        _flush(db.store_races, rows)
    _flush(db.store_races, rows, force=True)
    manifest.mark_parsed(consumed)
    return count


def _parse_runners(manifest: Manifest, raw_root: str, db: ArchiveDb,
                   results: dict, odds: dict,
                   unparsed_only: bool = True) -> tuple[int, int]:
    """Returns (starts, prev-start records seen — before deduplication).

    The prev-start count is reports, not rows: the same start comes back on
    every later race of that horse, and they collapse onto one row.
    """
    horses, starts, stats, betpercentages, prevstarts = [], [], [], [], []
    consumed = []
    count = 0
    prevstart_count = 0
    unplaceable = 0
    placeholders = 0
    for task, payload in _each_payload(manifest, raw_root, 'runners', consumed,
                                       unparsed_only):
        for raw in payload.get('collection', []):
            try:
                runner = Runner(**raw)
            except Exception as e:
                print(f'{task.rawPath}: runner {raw.get("runnerId")}: {e}')
                continue
            if is_placeholder(runner):
                placeholders += 1
                continue
            key = (runner.raceId, runner.startNumber)
            horses.append(horse_record(runner))
            starts.append(start_record(runner, results.get(key), odds.get(key)))
            stats += stat_records(runner)
            betpercentages += betpercentage_records(runner)
            runner_prevstarts = prevstart_records(runner)
            prevstarts += runner_prevstarts
            prevstart_count += len(runner_prevstarts)
            unplaceable += len(runner.prevStarts) - len(runner_prevstarts)
            count += 1
        _flush(db.store_horses, horses)
        _flush(db.store_starts, starts)
        _flush(db.store_stats, stats)
        _flush(db.store_betpercentages, betpercentages)
        _flush(db.store_prevstarts, prevstarts)
    _flush(db.store_horses, horses, force=True)
    _flush(db.store_starts, starts, force=True)
    _flush(db.store_stats, stats, force=True)
    _flush(db.store_betpercentages, betpercentages, force=True)
    _flush(db.store_prevstarts, prevstarts, force=True)
    manifest.mark_parsed(consumed)
    if placeholders:
        print(f'{placeholders} `Poissa` placeholder runners skipped: vacated start numbers.')
    if unplaceable:
        print(f'{unplaceable} prev-start entries skipped: no meet date or race number.')
    return count, prevstart_count


def _heppa_entries(task, payload) -> list:
    """The elements of a Heppa payload.

    Heppa sends bare lists where the Veikkaus API sends a `collection` wrapper.
    Anything else is drift worth naming rather than iterating into: a dict
    would otherwise loop over its keys and fail one confusing row at a time.
    """
    if isinstance(payload, list):
        return payload
    print(f'{task.rawPath}: expected a list, got {type(payload).__name__}')
    return []


def _parse_heppa_events(manifest: Manifest, raw_root: str, db: ArchiveDb,
                        unparsed_only: bool = True) -> int:
    """The meetings listing, including the cancelled and the non-toto ones."""
    rows, consumed = [], []
    count = 0
    for task, payload in _each_payload(manifest, raw_root, 'heppa_results', consumed,
                                       unparsed_only):
        for day in _heppa_entries(task, payload):
            for raw in day.get('events', []):
                if not raw.get('finnishTrack'):
                    continue
                try:
                    rows.append(heppa_event_record(HeppaEvent(**raw)))
                    count += 1
                except Exception as e:
                    print(f'{task.rawPath}: event {raw.get("date")} '
                          f'{raw.get("trackCode")}: {e}')
        _flush(db.store_heppa_events, rows)
    _flush(db.store_heppa_events, rows, force=True)
    manifest.mark_parsed(consumed)
    return count


def _parse_heppa_races(manifest: Manifest, raw_root: str, db: ArchiveDb,
                       unparsed_only: bool = True) -> int:
    rows, consumed = [], []
    count = 0
    for task, payload in _each_payload(manifest, raw_root, 'heppa_races', consumed,
                                       unparsed_only):
        for raw in _heppa_entries(task, payload):
            try:
                rows.append(heppa_race_record(HeppaRaceEntry(**raw)))
                count += 1
            except Exception as e:
                print(f'{task.rawPath}: race {raw.get("race", {}).get("startNumber")}: {e}')
        _flush(db.store_heppa_races, rows)
    _flush(db.store_heppa_races, rows, force=True)
    manifest.mark_parsed(consumed)
    return count


def _parse_heppa_starts(manifest: Manifest, raw_root: str, db: ArchiveDb,
                        unparsed_only: bool = True) -> int:
    """Both kinds of Heppa field: the Finnish meetings and the ones abroad.

    One loop over two endpoint types, because they are the same payload from the
    same endpoint and land in the same table — only their discovery differed.

    A start with no `horseId` is dropped rather than stored. `horseId` joined the
    primary key when the starts abroad arrived, DuckDB will not have a NULL
    there, and one such row would fail the whole batch it rides in rather than
    itself. It happens on none of the archive's 314,981 rows; this is for the
    day the payload changes.
    """
    rows, consumed = [], []
    count = 0
    for endpoint_type in ('heppa_start', 'heppa_foreign_start'):
        for task, payload in _each_payload(manifest, raw_root, endpoint_type, consumed,
                                           unparsed_only):
            for raw in _heppa_entries(task, payload):
                try:
                    start = HeppaStart(**raw)
                    if start.horseId is None:
                        print(f'{task.rawPath}: start {raw.get("programNumber")}: '
                              'no horseId, dropped')
                        continue
                    rows.append(heppa_start_record(start))
                    count += 1
                except Exception as e:
                    print(f'{task.rawPath}: start {raw.get("programNumber")}: {e}')
            _flush(db.store_heppa_starts, rows)
    _flush(db.store_heppa_starts, rows, force=True)
    manifest.mark_parsed(consumed)
    return count


def _parse_heppa_horses(manifest: Manifest, raw_root: str, db: ArchiveDb,
                        unparsed_only: bool = True) -> int:
    """One object per response here, not a list — this endpoint returns a horse."""
    rows, consumed = [], []
    count = 0
    for task, payload in _each_payload(manifest, raw_root, 'heppa_horse', consumed,
                                       unparsed_only):
        try:
            rows.append(heppa_horse_record(HeppaHorse(**payload)))
            count += 1
        except Exception as e:
            print(f'{task.rawPath}: horse {task.entityId}: {e}')
        _flush(db.store_heppa_horses, rows)
    _flush(db.store_heppa_horses, rows, force=True)
    manifest.mark_parsed(consumed)
    return count


def _parse_heppa_horse_stats(manifest: Manifest, raw_root: str, db: ArchiveDb,
                             unparsed_only: bool = True) -> int:
    rows, consumed = [], []
    count = 0
    for task, payload in _each_payload(manifest, raw_root, 'heppa_horse_stat', consumed,
                                       unparsed_only):
        try:
            new = heppa_horse_stat_records(HeppaHorseStats(**payload))
            rows.extend(new)
            count += len(new)
        except Exception as e:
            print(f'{task.rawPath}: {e}')
        _flush(db.store_heppa_horse_stats, rows)
    _flush(db.store_heppa_horse_stats, rows, force=True)
    manifest.mark_parsed(consumed)
    return count


def parse_all(db_name: str, raw_root: str, country: str, full: bool = False) -> dict:
    """Walk the raw zone into the archive tables. Idempotent.

    By default only payloads fetched since they were last loaded — a nightly
    cycle adds a dozen race days and should not pay for re-validating the whole
    archive. `full=True` reloads everything, which is what a change to any
    `*_record()` builder or scalar parser requires: this tracks what has been
    parsed, not what the parser would produce.

    The recompute_* pass always runs over the whole tables. It costs a fraction
    of a second, and being whole-table is exactly what makes it deterministic.
    """
    unparsed = not full
    with db_ops(db_name) as conn:
        manifest = Manifest(conn)
        manifest.create()
        archive_db.create(conn)
        db = ArchiveDb(conn)
        # The runners still outstanding decide how much of the results lookup
        # has to be built, so they are resolved before anything is loaded.
        runner_tasks = manifest.done('runners', unparsed)
        results = _results_map(manifest, raw_root, runner_tasks)
        _store_odds(manifest, raw_root, db, unparsed)
        odds = _final_win_odds(db)
        cards = _parse_cards(manifest, raw_root, db, country, unparsed)
        races = _parse_races(manifest, raw_root, db, unparsed)
        starts, prevstarts = _parse_runners(manifest, raw_root, db, results, odds, unparsed)
        heppa_events = _parse_heppa_events(manifest, raw_root, db, unparsed)
        heppa_races = _parse_heppa_races(manifest, raw_root, db, unparsed)
        heppa_starts = _parse_heppa_starts(manifest, raw_root, db, unparsed)
        heppa_horses = _parse_heppa_horses(manifest, raw_root, db, unparsed)
        heppa_stats = _parse_heppa_horse_stats(manifest, raw_root, db, unparsed)
        db.recompute_start_intervals()
        db.recompute_prev_start_coaches()
        db.recompute_auto_starts()
        # After the Veikkaus-only recomputes: identity first, then the merge
        # that reads it.
        db.recompute_heppa_links()
        db.recompute_start_from_heppa()
        # Last, because it reads canonicalKey and heppa_start.horseKey, which
        # recompute_heppa_links() above is what fills in.
        db.recompute_cross_source_intervals()
        counts = {'cards': cards, 'races': races, 'starts': starts,
                  'prev-starts': prevstarts, 'heppa events': heppa_events,
                  'heppa races': heppa_races, 'heppa starts': heppa_starts,
                  'heppa horses': heppa_horses, 'heppa horse stats': heppa_stats}
    return counts


def parse(args):
    """CLI handler: parse the raw zone into the archive tables."""
    counts = parse_all(args.db, args.raw, args.country, getattr(args, 'full', False))
    for name, count in counts.items():
        print(f'{count} {name} parsed into {args.db}.')
    if not any(counts.values()):
        print('Nothing new to parse. Use --full to reload the whole raw zone.')
