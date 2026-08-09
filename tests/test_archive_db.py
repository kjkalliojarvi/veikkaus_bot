import duckdb
import pytest

from veikkaus_bot import archive_db
from veikkaus_bot.archive_db import ArchiveDb


PREVSTART_COLUMNS = 30
START_COLUMNS = 22


def prevstart(prior_start_id, horse_key, meet_date, race_number=None):
    """A prev_start row with only the columns this test cares about set."""
    row = [None] * PREVSTART_COLUMNS
    row[0], row[1], row[2], row[6] = prior_start_id, horse_key, meet_date, race_number
    return tuple(row)


def start(race_id, start_number, horse_key, coach_name):
    row = [None] * START_COLUMNS
    row[0], row[1], row[3], row[6] = race_id, start_number, horse_key, coach_name
    return tuple(row)


def race(race_id, card_id, number):
    row = [None] * 15
    row[0], row[1], row[2] = race_id, card_id, number
    return tuple(row)


def card(card_id, meet_date):
    row = [None] * 11
    row[0], row[2] = card_id, meet_date
    return tuple(row)


@pytest.fixture
def db():
    conn = duckdb.connect(':memory:')
    archive_db.create(conn)
    return ArchiveDb(conn)


def intervals(db):
    return dict(db.conn.execute(
        'SELECT priorStartId, startInterval FROM archive.prev_start').fetchall())


def test_start_interval_counts_days_since_the_previous_start(db):
    db.store_prevstarts([prevstart(1, 'h|2019', '2026-06-01'),
                         prevstart(2, 'h|2019', '2026-06-15'),
                         prevstart(3, 'h|2019', '2026-07-20')])
    db.recompute_start_intervals()
    assert intervals(db)[2] == 14
    assert intervals(db)[3] == 35


def test_first_start_of_a_horse_keeps_the_epoch_sentinel(db):
    """No predecessor, so the gap is unknowable — a value to filter, not a real
    interval."""
    db.store_prevstarts([prevstart(1, 'h|2019', '2026-06-01')])
    db.recompute_start_intervals()
    assert intervals(db)[1] > 20000


def test_intervals_do_not_run_across_horses(db):
    db.store_prevstarts([prevstart(1, 'a|2019', '2026-06-01'),
                         prevstart(2, 'b|2020', '2026-06-15')])
    db.recompute_start_intervals()
    assert intervals(db)[2] > 20000   # b's own first start, not 14 days after a's


def test_recompute_is_deterministic_regardless_of_insert_order(db):
    """A start is re-reported by every later race of the horse, so the row that
    lands last is arbitrary — the interval must not depend on it."""
    rows = [prevstart(1, 'h|2019', '2026-06-01'),
            prevstart(2, 'h|2019', '2026-06-15'),
            prevstart(3, 'h|2019', '2026-07-20')]
    db.store_prevstarts(list(reversed(rows)))
    db.recompute_start_intervals()
    first = intervals(db)
    db.store_prevstarts(rows)          # same starts reported again, other order
    db.recompute_start_intervals()
    assert intervals(db) == first


def test_a_newly_crawled_earlier_start_corrects_the_interval(db):
    """Backfilling older dates fills a gap that was previously unknowable."""
    db.store_prevstarts([prevstart(2, 'h|2019', '2026-06-15')])
    db.recompute_start_intervals()
    assert intervals(db)[2] > 20000
    db.store_prevstarts([prevstart(1, 'h|2019', '2026-06-01')])
    db.recompute_start_intervals()
    assert intervals(db)[2] == 14


def test_rows_without_a_meet_date_are_left_alone(db):
    db.store_prevstarts([prevstart(1, 'h|2019', None)])
    db.recompute_start_intervals()
    assert intervals(db)[1] is None


def coaches(db):
    return dict(db.conn.execute(
        'SELECT priorStartId, coachName FROM archive.prev_start').fetchall())


def crawled_race(db, *, race_id, card_id, number, meet_date, horse_key, coach):
    db.store_cards([card(card_id, meet_date)])
    db.store_races([race(race_id, card_id, number)])
    db.store_starts([start(race_id, 1, horse_key, coach)])


def test_prev_start_coach_comes_from_the_crawled_race(db):
    crawled_race(db, race_id=10, card_id=1, number=3, meet_date='2026-06-01',
                 horse_key='h|2019', coach='Old Trainer')
    db.store_prevstarts([prevstart(1, 'h|2019', '2026-06-01', race_number=3)])
    db.recompute_prev_start_coaches()
    assert coaches(db)[1] == 'Old Trainer'


def test_prev_start_coach_is_the_trainer_of_that_day_not_a_later_one(db):
    """The horse changed trainers; each past start must keep its own."""
    crawled_race(db, race_id=10, card_id=1, number=3, meet_date='2026-06-01',
                 horse_key='h|2019', coach='Old Trainer')
    crawled_race(db, race_id=20, card_id=2, number=4, meet_date='2026-08-01',
                 horse_key='h|2019', coach='New Trainer')
    db.store_prevstarts([prevstart(1, 'h|2019', '2026-06-01', race_number=3),
                         prevstart(2, 'h|2019', '2026-08-01', race_number=4)])
    db.recompute_prev_start_coaches()
    assert coaches(db) == {1: 'Old Trainer', 2: 'New Trainer'}


def test_prev_start_coach_stays_null_outside_the_crawl_window(db):
    """No archived race for that day, so there is no honest value to use."""
    crawled_race(db, race_id=10, card_id=1, number=3, meet_date='2026-06-01',
                 horse_key='h|2019', coach='Old Trainer')
    db.store_prevstarts([prevstart(9, 'h|2019', '2023-01-05', race_number=7)])
    db.recompute_prev_start_coaches()
    assert coaches(db)[9] is None


def test_prev_start_coach_does_not_leak_between_horses(db):
    crawled_race(db, race_id=10, card_id=1, number=3, meet_date='2026-06-01',
                 horse_key='a|2019', coach='A Trainer')
    db.store_prevstarts([prevstart(1, 'b|2020', '2026-06-01', race_number=3)])
    db.recompute_prev_start_coaches()
    assert coaches(db)[1] is None
