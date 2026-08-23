from datetime import date

import duckdb
import pytest

from veikkaus_bot.crawler import (CARDS_DATE, Manifest, RACES, RUNNERS, Task,
                                  cards_task, dates, expand)


@pytest.fixture
def manifest():
    m = Manifest(duckdb.connect(':memory:'))
    m.create()
    return m


def test_dates_runs_newest_first():
    assert dates(date(2026, 8, 6), date(2026, 8, 9)) == [
        date(2026, 8, 9), date(2026, 8, 8), date(2026, 8, 7), date(2026, 8, 6)]


def test_cards_task_paths_follow_the_raw_zone_layout():
    task = cards_task(date(2026, 8, 9))
    assert task.url == '/cards/date/2026-08-09'
    assert task.rawPath == '2026-08-09/cards.json.gz'
    assert task.stage == CARDS_DATE


def test_expand_cards_keeps_only_the_wanted_country():
    task = cards_task(date(2026, 8, 9))
    payload = {'collection': [{'cardId': 1, 'country': 'FI'},
                              {'cardId': 2, 'country': 'SE'}]}
    children = expand(task, payload, 'FI', with_odds=False)
    assert [c.entityId for c in children] == ['1']
    assert children[0].rawPath == '2026-08-09/card_1/races.json.gz'


def test_expand_races_asks_for_runners_and_results():
    task = Task('races', '1', '/card/1/races', 'p', '2026-08-09', 1, RACES)
    payload = {'collection': [{'raceId': 7}]}
    assert [c.endpointType for c in expand(task, payload, 'FI', with_odds=False)] == [
        'runners', 'results']
    assert [c.endpointType for c in expand(task, payload, 'FI', with_odds=True)] == [
        'runners', 'results', 'pools']


def test_expand_pools_only_follows_the_win_pool():
    task = Task('pools', '7', '/race/7/pools', 'p', '2026-08-09', 1, 4)
    payload = {'collection': [{'poolId': 10, 'poolType': 'VOI'},
                              {'poolId': 11, 'poolType': 'KAK'}]}
    children = expand(task, payload, 'FI', with_odds=True)
    assert [c.entityId for c in children] == ['10']


def test_leaf_endpoints_expand_to_nothing():
    task = Task('runners', '7', '/race/7/runners', 'p', '2026-08-09', 1, RUNNERS)
    assert expand(task, {'collection': [{'runnerId': 1}]}, 'FI', with_odds=True) == []


def test_pending_work_comes_out_newest_date_and_lowest_stage_first(manifest):
    manifest.enqueue([cards_task(date(2026, 8, 7)), cards_task(date(2026, 8, 9))])
    manifest.enqueue([Task('races', '1', '/card/1/races', 'p', '2026-08-09', 1, RACES)])
    assert [(t.endpointType, t.meetDate) for t in manifest.next_pending(10)] == [
        ('cards_date', '2026-08-09'), ('races', '2026-08-09'), ('cards_date', '2026-08-07')]


def test_marking_a_task_takes_it_out_of_the_queue(manifest):
    task = cards_task(date(2026, 8, 9))
    manifest.enqueue([task])
    manifest.mark(task, 'done', 200, None)
    assert manifest.next_pending(10) == []
    assert [t.entityId for t in manifest.done('cards_date')] == ['2026-08-09']


def test_re_enqueueing_never_resets_finished_work(manifest):
    """Restarting a crawl over the same window must not re-fetch what is done."""
    task = cards_task(date(2026, 8, 9))
    manifest.enqueue([task])
    manifest.mark(task, 'done', 200, None)
    manifest.enqueue([task])
    assert manifest.next_pending(10) == []


def test_retry_failed_requeues_only_failures(manifest):
    good, bad = cards_task(date(2026, 8, 9)), cards_task(date(2026, 8, 8))
    manifest.enqueue([good, bad])
    manifest.mark(good, 'done', 200, None)
    manifest.mark(bad, 'failed', 503, 'boom')
    assert manifest.retry_failed() == 1
    assert [t.entityId for t in manifest.next_pending(10)] == ['2026-08-08']


def test_a_sigterm_leaves_quietly_rather_than_raising_in_the_handler():
    """`signal.signal` calls a handler with (signum, frame). With a one-argument
    handler a real SIGTERM raised TypeError inside the handler itself, so a long
    crawl blew up in `time.sleep` and took its `conn.close()` with it instead of
    stopping — observed on a heppa-foreign run killed at 1,500 fetches."""
    import signal as signal_module

    from veikkaus_bot.__main__ import sigterm_exit

    with pytest.raises(SystemExit):
        sigterm_exit(signal_module.SIGTERM, None)   # as the signal module calls it
    with pytest.raises(SystemExit):
        sigterm_exit(None)                          # as the dispatch tail calls it
    with pytest.raises(SystemExit):
        sigterm_exit()
