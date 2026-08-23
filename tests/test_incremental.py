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
from veikkaus_bot.crawler import (Manifest, VEIKKAUS_TYPES, cards_task, dates,
                                  expand)
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
