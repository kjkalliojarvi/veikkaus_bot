from datetime import date

import duckdb
import pytest

from veikkaus_bot.crawler import CARDS_DATE, Manifest, Task, cards_task
from veikkaus_bot import archive_db
from veikkaus_bot.heppa import (FINNISH_TRACKS, FOREIGN_MEETINGS, HEPPA_FOREIGN_RACES,
                                HEPPA_FOREIGN_START, HEPPA_HORSE, HEPPA_HORSE_STAT,
                                HEPPA_HORSE_TYPES, HEPPA_RACES, HEPPA_RESULTS,
                                HEPPA_START, HEPPA_TYPES, _foreign_races_task, expand,
                                horse_stat_task, horse_task, months, read_seeds,
                                results_task)


@pytest.fixture
def manifest():
    m = Manifest(duckdb.connect(':memory:'))
    m.create()
    return m


def event(track_code='SN', day='2026-08-08', **overrides):
    """A results-listing event with the flags expand() actually tests."""
    return {'date': day, 'trackCode': track_code, 'finnishTrack': True,
            'canceled': False, 'hasPublishedResults': True} | overrides


def results_payload(*events, day='2026-08-08'):
    return [{'raceDate': day, 'events': list(events)}]


def test_months_chunks_the_window_and_clips_both_ends():
    assert months(date(2026, 6, 15), date(2026, 8, 3)) == [
        (date(2026, 6, 15), date(2026, 6, 30)),
        (date(2026, 7, 1), date(2026, 7, 31)),
        (date(2026, 8, 1), date(2026, 8, 3))]


def test_months_handles_a_window_inside_one_month():
    assert months(date(2026, 8, 8), date(2026, 8, 8)) == [
        (date(2026, 8, 8), date(2026, 8, 8))]


def test_months_crosses_a_year_boundary():
    assert months(date(2025, 12, 30), date(2026, 1, 2)) == [
        (date(2025, 12, 30), date(2025, 12, 31)),
        (date(2026, 1, 1), date(2026, 1, 2))]


def test_a_clipped_range_is_a_different_task_from_the_full_month():
    """`INSERT OR IGNORE` means a task id is a promise about what was fetched.

    If a mid-month start reused the month's id, a later run over the full month
    would find that id already done and silently never crawl the first half.
    """
    clipped = results_task(date(2026, 8, 8), date(2026, 8, 31))
    full = results_task(date(2026, 8, 1), date(2026, 8, 31))
    assert clipped.entityId != full.entityId
    assert clipped.rawPath != full.rawPath


def test_results_task_paths_follow_the_raw_zone_layout():
    task = results_task(date(2026, 8, 1), date(2026, 8, 31))
    assert task.url == '/race/results/2026-08-01/2026-08-31/'
    assert task.rawPath == 'heppa/results_2026-08-01_2026-08-31.json.gz'
    assert task.meetDate == '2026-08-01'
    assert task.stage == HEPPA_RESULTS


def test_expand_results_asks_for_the_races_of_each_meeting():
    task = results_task(date(2026, 8, 1), date(2026, 8, 31))
    children = expand(task, results_payload(event('SN'), event('Y')))
    assert [c.endpointType for c in children] == ['heppa_races', 'heppa_races']
    assert [c.entityId for c in children] == ['2026-08-08/SN', '2026-08-08/Y']
    assert children[0].url == '/race/2026-08-08/SN/races'
    assert children[0].rawPath == '2026-08-08/heppa_SN/races.json.gz'
    assert children[0].stage == HEPPA_RACES


def test_expand_results_skips_foreign_cancelled_and_unpublished_meetings():
    task = results_task(date(2026, 8, 1), date(2026, 8, 31))
    children = expand(task, results_payload(
        event('SN'),
        event('SE', finnishTrack=False),
        event('KU', canceled=True),
        event('T', hasPublishedResults=False)))
    assert [c.entityId for c in children] == ['2026-08-08/SN']


def test_expand_races_asks_for_the_field_of_each_race():
    task = Task('heppa_races', '2026-08-08/SN', '/race/2026-08-08/SN/races',
                'p', '2026-08-08', None, HEPPA_RACES)
    payload = [{'race': {'startNumber': '1'}}, {'race': {'startNumber': '2'}}]
    children = expand(task, payload)
    assert [c.entityId for c in children] == ['2026-08-08/SN/1', '2026-08-08/SN/2']
    assert children[0].url == '/race/2026-08-08/SN/start/1'
    assert children[0].rawPath == '2026-08-08/heppa_SN/start_1.json.gz'
    assert children[0].stage == HEPPA_START


def test_expand_races_skips_an_entry_with_no_race_number():
    task = Task('heppa_races', '2026-08-08/SN', '/race/2026-08-08/SN/races',
                'p', '2026-08-08', None, HEPPA_RACES)
    assert expand(task, [{'race': {}}, {}]) == []


def test_the_field_is_a_leaf():
    task = Task('heppa_start', '2026-08-08/SN/1', '/race/2026-08-08/SN/start/1',
                'p', '2026-08-08', None, HEPPA_START)
    assert expand(task, [{'programNumber': '9', 'placing': '1'}]) == []


def test_a_wrapped_payload_expands_to_nothing():
    """Heppa sends bare lists; a `collection` dict would be the Veikkaus API."""
    task = results_task(date(2026, 8, 1), date(2026, 8, 31))
    assert expand(task, {'collection': [{'date': '2026-08-08'}]}) == []


def test_the_two_sources_share_a_manifest_without_draining_each_other(manifest):
    """One run, one host. A Heppa crawl must not fetch Veikkaus rows or vice
    versa — different rate limit, different circuit breaker."""
    manifest.enqueue([cards_task(date(2026, 8, 8)),
                      results_task(date(2026, 8, 1), date(2026, 8, 31))])
    assert [t.endpointType for t in manifest.next_pending(10)] == ['cards_date']
    assert [t.endpointType for t in manifest.next_pending(10, HEPPA_TYPES)] == [
        'heppa_results']


def test_retry_failed_stays_inside_its_own_source(manifest):
    veikkaus = cards_task(date(2026, 8, 8))
    heppa = results_task(date(2026, 8, 1), date(2026, 8, 31))
    manifest.enqueue([veikkaus, heppa])
    manifest.mark(veikkaus, 'failed', 503, 'boom')
    manifest.mark(heppa, 'failed', 503, 'boom')
    assert manifest.retry_failed(HEPPA_TYPES) == 1
    assert [t.endpointType for t in manifest.next_pending(10, HEPPA_TYPES)] == [
        'heppa_results']
    assert manifest.next_pending(10) == []


def test_heppa_stages_sort_after_the_veikkaus_ones_within_a_date():
    """Both sources share next_pending()'s (meetDate DESC, stage ASC) ordering,
    so the stage ranges must not overlap."""
    assert CARDS_DATE < HEPPA_RESULTS < HEPPA_RACES < HEPPA_START


def test_horse_task_shards_the_raw_zone():
    """14,050 horses in one directory is not fatal, only unpleasant."""
    task = horse_task('7913507947789197818')
    assert task.endpointType == 'heppa_horse'
    assert task.entityId == '7913507947789197818'
    assert task.url == '/horse/7913507947789197818'
    assert task.rawPath == 'heppa/horse/18/7913507947789197818.json.gz'
    assert task.meetDate is None          # a horse has no meet date
    assert task.stage == HEPPA_HORSE


def test_a_horse_record_is_a_leaf():
    task = horse_task('7913507947789197818')
    assert expand(task, {'id': '7913507947789197818', 'name': "Boomer's Revenge"}) == []


def test_the_horse_crawl_does_not_drain_the_meetings_crawl(manifest):
    """Two separate opt-ins: a one-day results run must not kick off an
    eight-hour horse crawl, and vice versa."""
    manifest.enqueue([results_task(date(2026, 8, 1), date(2026, 8, 31)),
                      horse_task('7913507947789197818')])
    assert [t.endpointType for t in manifest.next_pending(10, HEPPA_TYPES)] == ['heppa_results']
    assert [t.endpointType for t in manifest.next_pending(10, HEPPA_HORSE_TYPES)] == ['heppa_horse']


def test_re_enqueueing_a_known_horse_never_refetches_it(manifest):
    """Re-running after a later meetings crawl should cost only the new horses."""
    task = horse_task('7913507947789197818')
    manifest.enqueue([task])
    manifest.mark(task, 'done', 200, None)
    manifest.enqueue([task, horse_task('4605744788003903677')])
    assert [t.entityId for t in manifest.next_pending(10, HEPPA_HORSE_TYPES)] == [
        '4605744788003903677']


# --- the meetings abroad ---------------------------------------------------
#
# Heppa serves a Finnish horse's foreign starts through the same per-meeting
# endpoints as the home ones, but never lists those meetings, so they are
# discovered from the archive instead. These pin the two halves of that: the
# expander, and the query that decides what counts as abroad.


def test_a_foreign_races_payload_asks_for_the_field_of_each_race():
    task = _foreign_races_task('2026-05-30', 'SV')
    assert task.endpointType == 'heppa_foreign_races'
    assert task.url == '/race/2026-05-30/SV/races'
    assert task.stage == HEPPA_FOREIGN_RACES
    children = expand(task, [{'race': {'startNumber': '3'}},
                             {'race': {'startNumber': '11'}}])
    assert [c.endpointType for c in children] == ['heppa_foreign_start'] * 2
    assert [c.entityId for c in children] == ['2026-05-30/SV/3', '2026-05-30/SV/11']
    assert children[0].url == '/race/2026-05-30/SV/start/3'
    assert children[0].stage == HEPPA_FOREIGN_START


def test_a_foreign_meeting_keeps_the_gaps_in_its_race_numbering():
    """Only the races a Finnish horse ran in come back, so Solvalla 2026-05-30
    is races 1-5, 7, 11 and 31. Counting them instead of reading them would ask
    for races 6, 8, 9 and 10 and miss 11 and 31 entirely."""
    task = _foreign_races_task('2026-05-30', 'SV')
    payload = [{'race': {'startNumber': n}} for n in ('1', '5', '7', '11', '31')]
    assert [c.entityId.rsplit('/', 1)[1] for c in expand(task, payload)] == [
        '1', '5', '7', '11', '31']


def test_the_foreign_raw_zone_is_a_directory_of_its_own():
    """The track codes are disjoint from the Finnish ones by construction, but
    one raw path answerable by two endpoint types only bites after a track
    changes category, and the prefix costs nothing."""
    home = expand(Task('heppa_races', '2026-05-30/SV', 'u', 'p', '2026-05-30', None,
                       HEPPA_RACES), [{'race': {'startNumber': '3'}}])[0]
    abroad = expand(_foreign_races_task('2026-05-30', 'SV'),
                    [{'race': {'startNumber': '3'}}])[0]
    assert home.rawPath == '2026-05-30/heppa_SV/start_3.json.gz'
    assert abroad.rawPath == '2026-05-30/heppa_foreign_SV/start_3.json.gz'


def test_a_foreign_field_is_a_leaf():
    task = Task('heppa_foreign_start', '2026-05-30/SV/3', 'u', 'p', '2026-05-30',
                None, HEPPA_FOREIGN_START)
    assert expand(task, [{'programNumber': '0', 'placing': '4'}]) == []


@pytest.fixture
def archive():
    conn = duckdb.connect(':memory:')
    archive_db.create(conn)
    return conn


def prev_start(track_code, meet_date='2026-05-30'):
    row = [None] * archive_db.INSERT_PREVSTART.count('?')
    row[1], row[2], row[4], row[6] = 'boomer|2015', meet_date, track_code, 1
    return tuple(row)


def heppa_event(track_code, meet_date='2026-08-08'):
    row = [None] * archive_db.INSERT_HEPPA_EVENT.count('?')
    row[0], row[1] = meet_date, track_code
    return tuple(row)


def discovered(conn):
    return conn.execute(FOREIGN_MEETINGS).fetchall()


def test_a_track_the_finnish_listing_never_named_is_a_meeting_abroad(archive):
    archive_db.ArchiveDb(archive).store_heppa_events([heppa_event('SN')])
    archive_db.ArchiveDb(archive).store_prevstarts([prev_start('Sv')])
    assert discovered(archive) == [('2026-05-30', 'SV')]


def test_a_finnish_track_is_not_a_meeting_abroad(archive):
    db = archive_db.ArchiveDb(archive)
    db.store_heppa_events([heppa_event('SN')])
    db.store_prevstarts([prev_start('Sn')])
    assert discovered(archive) == []


def test_the_harma_alias_is_folded_before_the_comparison(archive):
    """Veikkaus writes Harma as both `Hr` and `Hr2`; Heppa has only `HR`. Without
    the fold `HR2` reads as a track abroad and this crawl would re-fetch 28
    Finnish meetings it already has."""
    db = archive_db.ArchiveDb(archive)
    db.store_heppa_events([heppa_event('HR')])
    db.store_prevstarts([prev_start('Hr2')])
    assert discovered(archive) == []


def test_a_meeting_already_crawled_is_still_discovered(archive):
    """The self-poisoning case, and the reason the exclusion reads heppa_event
    rather than heppa_start. heppa_event comes from the Finnish-only listing so
    it can never name a track abroad; heppa_start can, the moment this crawl
    succeeds once — and then the next run would exclude exactly the tracks it had
    just learnt about and find nothing, looking like a crawl that finished."""
    db = archive_db.ArchiveDb(archive)
    db.store_heppa_events([heppa_event('SN')])
    db.store_prevstarts([prev_start('Sv')])
    row = [None] * archive_db.INSERT_HEPPA_START.count('?')
    row[0], row[1], row[2], row[3] = '2026-05-30', 'SV', 3, 0
    row[5], row[42] = 'H1', False
    db.store_heppa_starts([tuple(row)])
    assert discovered(archive) == [('2026-05-30', 'SV')]


# --- the registry's own career totals --------------------------------------


def test_a_horse_stat_task_is_sharded_and_separate_from_the_registry_record():
    """Its own endpoint type, because the two are different kinds of fact: the
    registry record never changes, this is only ever true today."""
    stat, record = horse_stat_task('8751664442635804005'), horse_task('8751664442635804005')
    assert stat.url == '/horse/8751664442635804005/stats'
    assert stat.rawPath == 'heppa/stats/05/8751664442635804005.json.gz'
    assert stat.stage == HEPPA_HORSE_STAT
    assert stat.endpointType != record.endpointType
    assert stat.rawPath != record.rawPath


def test_a_horse_stat_payload_is_a_leaf():
    assert expand(horse_stat_task('H1'), {'id': 'H1', 'stats': []}) == []


# --- seeding a meeting by hand ---------------------------------------------
#
# prev_start can only name a meeting if Veikkaus re-reported one of its horses,
# and its foreign rows begin in 2023-09, so the seed file is where knowledge
# from outside the archive goes. Guessing is cheap — a meeting that never
# happened, a track that never held one and a real track on a quiet day all
# answer 200 with `[]` in two bytes — so the parsing is what has to be careful.


def seeds(tmp_path, text):
    path = tmp_path / 'seeds.csv'
    path.write_text(text, encoding='utf-8')
    return read_seeds(str(path))


def test_a_missing_seed_file_is_no_seeds_rather_than_an_error():
    """The default path is one nobody has to create."""
    assert read_seeds('/nowhere/foreign_meetings.csv') == ([], [])


def test_comments_and_blank_lines_are_skipped(tmp_path):
    meetings, complaints = seeds(tmp_path, """
        # Combat Fighter, Elitloppet weekend

        2026-05-30,SV     # trailing comment
        """.replace(' ' * 8, ''))
    assert meetings == [('2026-05-30', 'SV')]
    assert complaints == []


def test_a_track_code_is_upper_cased_and_alias_folded(tmp_path):
    """`Sv` and `SV` are one meeting, and a seeded `Hr2` becomes the `HR` the
    caller then recognises as Finnish and skips."""
    meetings, _ = seeds(tmp_path, '2026-05-30, sv\n2024-03-01,Hr2\n')
    assert meetings == [('2026-05-30', 'SV'), ('2024-03-01', 'HR')]


def test_a_malformed_line_is_reported_and_not_fetched(tmp_path):
    """The date goes straight into a URL, so `/race/not-a-date/BO/races` would be
    recorded as a failure of the crawl rather than of the file."""
    meetings, complaints = seeds(tmp_path, '2026-99-99,BO\njust-one-field\n2026-05-30,\n')
    assert meetings == []
    assert len(complaints) == 3
    assert "'2026-99-99' is not a yyyy-mm-dd date" in complaints[0]
    assert 'expected `date,trackCode`' in complaints[1]


def test_a_duplicate_seed_survives_parsing_and_is_deduped_later(tmp_path):
    """read_seeds does not dedupe: whether a seed is new depends on the archive,
    which it has no access to. backfill_foreign folds them together, and the
    manifest's INSERT OR IGNORE would anyway."""
    meetings, _ = seeds(tmp_path, '2026-05-30,SV\n2026-05-30,sv\n')
    assert meetings == [('2026-05-30', 'SV')] * 2
    assert list(dict.fromkeys(meetings)) == [('2026-05-30', 'SV')]


def test_the_finnish_track_set_comes_from_the_listing_not_from_the_starts(archive):
    """What makes a seeded track foreign or a mistake. Same source as
    FOREIGN_MEETINGS excludes on, and for the same reason: heppa_event cannot
    name a track abroad, heppa_start can once the foreign crawl has run."""
    db = archive_db.ArchiveDb(archive)
    db.store_heppa_events([heppa_event('SN')])
    row = [None] * archive_db.INSERT_HEPPA_START.count('?')
    row[0], row[1], row[2], row[3] = '2026-05-30', 'SV', 3, 0
    row[5], row[42] = 'H1', False
    db.store_heppa_starts([tuple(row)])
    assert [r[0] for r in archive.execute(FINNISH_TRACKS).fetchall()] == ['SN']
