from datetime import date

import duckdb
import pytest

from veikkaus_bot.crawler import CARDS_DATE, Manifest, Task, cards_task
from veikkaus_bot.heppa import (HEPPA_RACES, HEPPA_RESULTS, HEPPA_START, HEPPA_TYPES,
                                expand, months, results_task)


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
