"""Incremental parse and the --refetch-from window reset.

These drive `parse_all` over a real (tiny) raw zone rather than mocking it,
because the thing worth testing is precisely the interaction between the
manifest's bookkeeping and what ends up in the tables.
"""
from datetime import date
import gzip
import json
import pathlib

import duckdb
import pytest

from veikkaus_bot import parse as P
from veikkaus_bot.archive_db import ArchiveDb
from veikkaus_bot.crawler import (LEG_ODDS, Manifest, VEIKKAUS_TYPES,
                                  _pool_odds_task, cards_task, dates, expand)
from veikkaus_bot.heppa import (HEPPA_TYPES, _foreign_races_task, horse_task,
                                results_task)
from veikkaus_bot.heppa import expand as heppa_expand


MEET = '2026-08-08'
CARD_ID, RACE_ID = 1, 7

CARDS = {'collection': [{'cardId': CARD_ID, 'country': 'FI', 'meetDate': MEET,
                         'trackAbbreviation': 'SN', 'trackName': 'Savonlinna',
                         'trackNumber': 24}]}
RACES = {'collection': [{'raceId': RACE_ID, 'cardId': CARD_ID, 'number': 1,
                         'startType': 'VOLT_START', 'toteResultString': '9-7-8'}]}
RUNNERS = {'collection': [
    {'runnerId': 100 + n, 'raceId': RACE_ID, 'startNumber': n,
     'horseName': f'Horse {n}', 'birthDate': '2019-05-01', 'coachName': 'T. Korpela'}
    for n in (7, 8, 9, 10)]}
RESULTS = {'raceId': RACE_ID, 'results': [
    {'poolType': 'VOI', 'startNumber': 9, 'position': 1, 'kmTime': '18,8', 'probable': 444},
    {'poolType': 'VOI', 'startNumber': 7, 'position': 2, 'kmTime': '19,8'}]}


def store(raw_root, raw_path, payload):
    full = raw_root / raw_path
    full.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(full, 'wt', encoding='utf-8') as f:
        json.dump(payload, f)


@pytest.fixture
def archive(tmp_path):
    """A one-card raw zone with a manifest that says it was all fetched."""
    raw = tmp_path / 'raw'
    db = str(tmp_path / 'a.duckdb')
    conn = duckdb.connect(db)
    manifest = Manifest(conn)
    manifest.create()

    cards = cards_task(date.fromisoformat(MEET))
    manifest.enqueue([cards])
    store(raw, cards.rawPath, CARDS)
    children = expand(cards, CARDS, 'FI', with_odds=False)
    manifest.enqueue(children)
    store(raw, children[0].rawPath, RACES)
    grandchildren = expand(children[0], RACES, 'FI', with_odds=False)
    manifest.enqueue(grandchildren)
    for task in grandchildren:
        store(raw, task.rawPath, RUNNERS if task.endpointType == 'runners' else RESULTS)
    for task in (cards, *children, *grandchildren):
        manifest.mark(task, 'done', 200, None)
    conn.close()
    return str(raw), db


def run(archive, **kw):
    raw, db = archive
    return P.parse_all(db, raw, 'FI', **kw)


def refetch(db, meet_date, types=VEIKKAUS_TYPES):
    """Reset a window and re-crawl it, exactly as `crawl()` would.

    Going through `mark()` rather than writing the status column directly is
    the point: that is where a re-fetch forgets it was ever parsed.
    """
    conn = duckdb.connect(db)
    m = Manifest(conn)
    m.reset_window(meet_date, meet_date, types)
    for task in m.next_pending(1000, types):
        m.mark(task, 'done', 200, None)
    conn.close()


def refetch_one(db, endpoint_type):
    """Re-fetch a single endpoint type, leaving its siblings settled."""
    conn = duckdb.connect(db)
    m = Manifest(conn)
    for task in m.done(endpoint_type):
        m.mark(task, 'done', 200, None)
    conn.close()


def placements(db):
    conn = duckdb.connect(db, read_only=True)
    rows = dict(conn.execute(
        'SELECT startNumber, placement FROM archive.start ORDER BY startNumber').fetchall())
    conn.close()
    return rows


# --- incremental parse -------------------------------------------------------

def test_the_first_parse_loads_everything(archive):
    counts = run(archive)
    assert (counts['cards'], counts['races'], counts['starts']) == (1, 1, 4)


def test_the_second_parse_is_a_no_op(archive):
    run(archive)
    assert sum(run(archive).values()) == 0


def test_a_no_op_parse_does_not_disturb_the_tables(archive):
    run(archive)
    before = placements(archive[1])
    run(archive)
    assert placements(archive[1]) == before
    assert before == {7: 2, 8: 3, 9: 1, 10: None}


def test_a_refetched_day_is_parsed_again(archive):
    run(archive)
    refetch(archive[1], MEET)
    counts = run(archive)
    assert (counts['cards'], counts['races'], counts['starts']) == (1, 1, 4)


def test_a_refetched_runner_still_finds_its_result(archive):
    """The regression the scoped `_results_map` risks: a runners payload loaded
    on its own still needs the results payload for its race, which was loaded
    in some earlier run and is not itself outstanding."""
    run(archive)
    refetch_one(archive[1], 'runners')
    conn = duckdb.connect(archive[1])
    conn.execute('DELETE FROM archive.start')
    conn.close()
    assert run(archive)['starts'] == 4
    assert placements(archive[1]) == {7: 2, 8: 3, 9: 1, 10: None}


def test_a_stale_parsedAt_is_also_caught(archive):
    """The backstop, for a fetchedAt that moved without going through mark()."""
    run(archive)
    conn = duckdb.connect(archive[1])
    conn.execute("UPDATE archive.manifest SET fetchedAt = '2099-01-01T00:00:00' "
                 "WHERE endpointType = 'runners'")
    conn.close()
    assert run(archive)['starts'] == 4


def test_full_reloads_a_settled_archive(archive):
    run(archive)
    assert sum(run(archive).values()) == 0
    counts = run(archive, full=True)
    assert (counts['cards'], counts['races'], counts['starts']) == (1, 1, 4)


def test_full_does_not_leave_the_archive_looking_unparsed(archive):
    run(archive, full=True)
    assert sum(run(archive).values()) == 0


# --- reset_window ------------------------------------------------------------

def manifest_of(db):
    conn = duckdb.connect(db)
    m = Manifest(conn)
    m.create()
    return conn, m


def test_reset_window_only_touches_its_own_dates(archive):
    conn, m = manifest_of(archive[1])
    assert m.reset_window('2026-08-01', '2026-08-07', VEIKKAUS_TYPES) == 0
    assert m.reset_window(MEET, MEET, VEIKKAUS_TYPES) == 4
    conn.close()


def test_reset_window_leaves_the_other_source_alone(archive):
    conn, m = manifest_of(archive[1])
    heppa = results_task(date(2026, 8, 1), date(2026, 8, 31))
    m.enqueue([heppa])
    m.mark(heppa, 'done', 200, None)
    m.reset_window('2026-08-01', '2026-08-31', VEIKKAUS_TYPES)
    assert m.next_pending(10, HEPPA_TYPES) == []
    assert m.reset_window('2026-08-01', '2026-08-31', HEPPA_TYPES) == 1
    conn.close()


def test_reset_window_never_touches_an_undated_task(archive):
    """A registry record is not dated, so it can never be in a date range."""
    conn, m = manifest_of(archive[1])
    horse = horse_task('7913507947789197818')
    m.enqueue([horse])
    m.mark(horse, 'done', 200, None)
    m.reset_window('1900-01-01', '2100-01-01', HEPPA_TYPES)
    assert conn.execute("SELECT status FROM archive.manifest "
                        "WHERE endpointType = 'heppa_horse'").fetchone() == ('done',)
    conn.close()


@pytest.mark.parametrize('status', ['done', 'missing', 'failed'])
def test_reset_window_recovers_every_settled_status(status):
    """An early crawl looks like `done` with empty results, or like `missing` —
    both have to become fetchable again."""
    conn = duckdb.connect(':memory:')
    m = Manifest(conn)
    m.create()
    task = cards_task(date.fromisoformat(MEET))
    m.enqueue([task])
    m.mark(task, status, 200, None)
    assert m.reset_window(MEET, MEET, VEIKKAUS_TYPES) == 1
    assert [t.entityId for t in m.next_pending(10)] == [MEET]


def test_reset_window_counts_only_what_it_changed():
    conn = duckdb.connect(':memory:')
    m = Manifest(conn)
    m.create()
    tasks = [cards_task(d) for d in dates(date(2026, 8, 6), date(2026, 8, 8))]
    m.enqueue(tasks)
    m.mark(tasks[0], 'done', 200, None)          # only one is settled
    assert m.reset_window('2026-08-06', '2026-08-08', VEIKKAUS_TYPES) == 1
    assert m.reset_window('2026-08-06', '2026-08-08', VEIKKAUS_TYPES) == 0


# --- the starts abroad, through parse_all -----------------------------------
#
# The crawl half is pinned in test_heppa.py and the row shape in test_parse.py.
# What is left is the interaction: a foreign meeting's payloads sitting in the
# raw zone under their own endpoint types have to reach archive.heppa_start,
# which is a different code path from the home ones only in how it is discovered.


# Bollnas race 4 as Heppa sends it — every runner programNumber '0'.
ABROAD = [{'date': '2026-08-16', 'trackCode': 'BO', 'startNumber': '4',
           'programNumber': '0', 'horseId': f'H{n}', 'horseName': f'Abroad {n}',
           'placing': str(n), 'shortKilometerTime': '15,5', 'distance': '2140',
           'distanceCode': 'ake', 'lane': str(n), 'finnishTrack': False}
          for n in (1, 2, 3, 4, 5)]


def with_foreign_meeting(archive, field=None):
    """Add one crawled meeting abroad to the fixture's raw zone."""
    raw, db = archive
    conn = duckdb.connect(db)
    manifest = Manifest(conn)
    races = _foreign_races_task('2026-08-16', 'BO')
    listing = [{'race': {'startNumber': '4'}}]
    manifest.enqueue([races])
    store(pathlib.Path(raw), races.rawPath, listing)
    children = heppa_expand(races, listing)
    manifest.enqueue(children)
    store(pathlib.Path(raw), children[0].rawPath,
          ABROAD if field is None else field)
    for task in (races, *children):
        manifest.mark(task, 'done', 200, None)
    conn.close()
    return raw, db


def test_a_crawled_meeting_abroad_reaches_the_start_table(archive):
    """Five horses, one race, every one of them programNumber 0 — the case the
    primary key gained horseId for."""
    counts = run(with_foreign_meeting(archive))
    assert counts['heppa starts'] == 5
    raw, db = archive
    conn = duckdb.connect(db)
    assert conn.execute("""SELECT count(*), count(DISTINCT horseId)
        FROM archive.heppa_start WHERE finnishTrack = FALSE""").fetchone() == (5, 5)
    assert conn.execute("""SELECT count(*) FROM archive.heppa_start
        WHERE finnishTrack = FALSE AND placement IS NOT NULL""").fetchone()[0] == 5
    conn.close()


def test_a_start_abroad_with_no_registry_id_is_dropped_not_fatal(archive):
    """horseId is in the primary key, so DuckDB will not hold a NULL there and
    one such row would fail the whole batch it rides in. It is dropped instead,
    and its neighbours still land."""
    field = ABROAD[:2] + [{k: v for k, v in ABROAD[2].items() if k != 'horseId'}]
    counts = run(with_foreign_meeting(archive, field))
    assert counts['heppa starts'] == 2
    raw, db = archive
    conn = duckdb.connect(db)
    assert conn.execute(
        'SELECT count(*) FROM archive.heppa_start').fetchone()[0] == 2
    conn.close()


def test_a_second_parse_does_not_re_add_the_meeting_abroad(archive):
    """The same idempotence the rest of the pipeline has: the manifest's parsed
    bookkeeping covers the new endpoint types too."""
    prepared = with_foreign_meeting(archive)
    assert run(prepared)['heppa starts'] == 5
    assert run(prepared)['heppa starts'] == 0


# One T5 pool, two legs' worth of runners, shaped exactly as the live payload:
# `ticks` twins `amount`, `scratched` is present on some rows and not others,
# and the last row has no legNumber at all.
LEG_POOL_ID = 55
LEG_ODDS_PAYLOAD = {
    'poolId': LEG_POOL_ID, 'poolType': 'T5', 'netSales': 930125, 'netPool': 604580,
    'updated': 1788429164129,
    'odds': [
        {'legNumber': 1, 'raceNumber': 1, 'raceId': RACE_ID, 'runnerNumber': 9,
         'runnerId': 109, 'percentage': 3056, 'amount': 284265, 'ticks': 284265,
         'winProbable': 289},
        {'legNumber': 1, 'raceNumber': 1, 'raceId': RACE_ID, 'runnerNumber': 7,
         'runnerId': 107, 'percentage': 426, 'amount': 39620, 'ticks': 39620,
         'winProbable': 1762, 'scratched': True},
        {'legNumber': 2, 'raceNumber': 2, 'raceId': RACE_ID + 1, 'runnerNumber': 9,
         'runnerId': 209, 'percentage': 1676, 'amount': 155922, 'ticks': 155922,
         'winProbable': 1385},
        {'raceNumber': 2, 'raceId': RACE_ID + 1, 'runnerNumber': 8, 'percentage': 79}]}


def with_leg_pool(archive, payload=None):
    """Add one crawled multi-leg pool to the fixture's raw zone."""
    raw, db = archive
    conn = duckdb.connect(db)
    manifest = Manifest(conn)
    task = _pool_odds_task('leg_odds', LEG_ODDS, MEET, CARD_ID, LEG_POOL_ID)
    manifest.enqueue([task])
    store(pathlib.Path(raw), task.rawPath,
          LEG_ODDS_PAYLOAD if payload is None else payload)
    manifest.mark(task, 'done', 200, None)
    conn.close()
    return raw, db


def leg_rows(db):
    conn = duckdb.connect(db)
    rows = conn.execute("""SELECT legNumber, startNumber, percentage, amount, scratched,
                                  capturedAt, netSales, poolType, placement
                           FROM archive.leg_percentage ORDER BY legNumber, startNumber"""
                        ).fetchall()
    conn.close()
    return rows


def test_a_crawled_multi_leg_pool_reaches_the_leg_percentage_table(archive):
    """Two of the payload's four rows survive, for two different reasons.

    The fourth has no legNumber, which is in the primary key, so the parse
    drops it at the door. Leg 2 names a race the fixture does not have, so it
    has no winner and the recompute drops it — see the straddle test below.
    What is left is leg 1, stamped with what its runners did: the card's
    toteResultString is '9-7-8', so 9 won and 7 was second.
    """
    counts = run(with_leg_pool(archive))
    assert counts['multi-leg pools'] == 1
    assert leg_rows(archive[1]) == [
        (1, 7, 426, 39620, True, 1788429164129, 930125, 'T5', 2),
        (1, 9, 3056, 284265, None, 1788429164129, 930125, 'T5', 1)]


def test_a_second_parse_does_not_re_add_the_multi_leg_pool(archive):
    prepared = with_leg_pool(archive)
    run(prepared)
    before = leg_rows(prepared[1])
    assert run(prepared)['multi-leg pools'] == 0
    assert leg_rows(prepared[1]) == before


def test_a_refetched_pool_replaces_its_figures_rather_than_adding_a_version(archive):
    """Why capturedAt is not in the primary key, unlike odds_snapshot's.

    A pool crawled while betting was still open holds percentages that are not
    final; --refetch-from is what fixes that, and it has to *replace* them —
    one row per (pool, leg, runner), holding the closing figures.
    """
    prepared = with_leg_pool(archive)
    run(prepared)
    closing = {**LEG_ODDS_PAYLOAD, 'updated': LEG_ODDS_PAYLOAD['updated'] + 60_000,
               'odds': [{**LEG_ODDS_PAYLOAD['odds'][0], 'percentage': 2900}]}
    with_leg_pool(prepared, closing)
    assert run(prepared)['multi-leg pools'] == 1
    assert leg_rows(prepared[1]) == [
        (1, 7, 426, 39620, True, 1788429164129, 930125, 'T5', 2),
        (1, 9, 2900, 284265, None, 1788429164129 + 60_000, 930125, 'T5', 1)]


def test_a_leg_whose_race_never_ran_is_dropped_and_its_sibling_survives(archive):
    """The canceled meeting, at leg grain.

    A leg no runner of which was placed first is a race that never ran — 139 of
    the archive's 144 such races are on meetings Heppa flags `canceled`, the
    other five are one card abandoned partway. Dropping them leaves the
    invariant every statistic wants: every leg in the table has a winner.

    Leg grain matters even though no pool in the archive straddles yet: here
    leg 1 ran and leg 2 did not, and the pool keeps the leg that ran.
    """
    run(with_leg_pool(archive))
    conn = duckdb.connect(archive[1])
    assert conn.execute(
        'SELECT DISTINCT legNumber FROM archive.leg_percentage').fetchall() == [(1,)]
    # The invariant, stated the way a statistics query would rely on it.
    assert conn.execute("""SELECT count(*) FROM (
        SELECT poolId, legNumber FROM archive.leg_percentage
        GROUP BY 1, 2 HAVING count(*) FILTER (WHERE placement = 1) = 0)""").fetchone()[0] == 0
    conn.close()


def test_the_drop_does_not_run_away_on_a_second_parse(archive):
    """The failure mode a delete introduces: parse twice, lose more each time."""
    prepared = with_leg_pool(archive)
    run(prepared)
    before = leg_rows(prepared[1])
    run(prepared)
    assert leg_rows(prepared[1]) == before


def test_a_dropped_leg_comes_back_when_its_result_does(archive):
    """What makes the delete safe rather than a one-way door.

    The raw payloads are untouched, so `parse --full` re-inserts every dropped
    row and the rule is re-evaluated against whatever results exist by then —
    the recovery for a card crawled before its results were published.
    """
    raw, db = with_leg_pool(archive)
    run((raw, db))
    assert [r[0] for r in leg_rows(db)] == [1, 1]

    # The missing race arrives: leg 2's race, with its own field and result.
    conn = duckdb.connect(db)
    conn.execute("INSERT INTO archive.race (raceId, cardId, number, toteResultString) "
                 "VALUES (?, ?, 2, '9-8-7')", (RACE_ID + 1, CARD_ID))
    conn.execute('INSERT INTO archive.start (raceId, startNumber, placement) VALUES (?, 9, 1)',
                 (RACE_ID + 1,))
    conn.close()

    run((raw, db), full=True)
    assert [(r[0], r[1], r[8]) for r in leg_rows(db)] == [(1, 7, 2), (1, 9, 1), (2, 9, 1)]


def test_a_dead_heat_puts_two_winners_in_one_leg(archive):
    """18 legs of the archive hold two `placement = 1` rows.

    Oulu 2021-02-06 race 9 is the shape of it, and both sources agree: Jamir
    and Evartti are `placingRaw '1'`, and nothing is placed second. So a
    statistics query counts winners with a FILTER and must accept more than one
    per leg rather than joining on the assumption of exactly one.
    """
    raw, db = with_leg_pool(archive)
    run((raw, db))
    conn = duckdb.connect(db)
    # Straight to the recompute rather than through a parse: a full re-parse
    # would rebuild archive.start from the runners payload and undo this.
    conn.execute('UPDATE archive.start SET placement = 1 WHERE raceId = ? AND startNumber = 7',
                 (RACE_ID,))
    assert ArchiveDb(conn).recompute_leg_placements() == 0
    rows = conn.execute('SELECT startNumber, placement FROM archive.leg_percentage '
                        'ORDER BY startNumber').fetchall()
    conn.close()
    assert rows == [(7, 1), (9, 1)]
