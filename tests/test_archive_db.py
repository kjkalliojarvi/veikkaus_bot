import duckdb
import pytest

from veikkaus_bot import archive_db
from veikkaus_bot.archive_db import ArchiveDb


PREVSTART_COLUMNS = 30
START_COLUMNS = 22


def prevstart(prior_start_id, horse_key, meet_date, race_number=1):
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
    """Keyed by meet date — priorStartId is not an identity, see below."""
    return dict(db.conn.execute(
        'SELECT meetDate, startInterval FROM archive.prev_start').fetchall())


def test_a_start_is_identified_by_horse_date_and_race_not_by_prior_start_id():
    """A horse's whole prevStarts list is renumbered on every later report, so
    the same start arrives under a fresh priorStartId each time. Keying on that
    id would store the same career once per race the horse subsequently ran."""
    conn = duckdb.connect(':memory:')
    archive_db.create(conn)
    db = ArchiveDb(conn)
    db.store_prevstarts([prevstart(2580766103, 'h|2014', '2026-05-01', race_number=6)])
    db.store_prevstarts([prevstart(2582037176, 'h|2014', '2026-05-01', race_number=6)])
    assert conn.execute('SELECT count(*) FROM archive.prev_start').fetchone()[0] == 1


def test_the_same_start_twice_in_one_batch_also_collapses(db):
    db.store_prevstarts([prevstart(111, 'h|2014', '2026-05-01', race_number=6),
                         prevstart(222, 'h|2014', '2026-05-01', race_number=6)])
    assert db.conn.execute('SELECT count(*) FROM archive.prev_start').fetchone()[0] == 1


def test_two_races_on_one_day_stay_separate(db):
    db.store_prevstarts([prevstart(1, 'h|2019', '2026-06-01', race_number=3),
                         prevstart(2, 'h|2019', '2026-06-01', race_number=9)])
    assert db.conn.execute('SELECT count(*) FROM archive.prev_start').fetchone()[0] == 2


def test_start_interval_counts_days_since_the_previous_start(db):
    db.store_prevstarts([prevstart(1, 'h|2019', '2026-06-01'),
                         prevstart(2, 'h|2019', '2026-06-15'),
                         prevstart(3, 'h|2019', '2026-07-20')])
    db.recompute_start_intervals()
    assert intervals(db)['2026-06-15'] == 14
    assert intervals(db)['2026-07-20'] == 35


def test_a_re_reported_start_does_not_create_a_zero_day_gap(db):
    """The duplicate-row failure mode, seen from the interval side."""
    db.store_prevstarts([prevstart(1, 'h|2019', '2026-06-01'),
                         prevstart(2, 'h|2019', '2026-06-15')])
    db.store_prevstarts([prevstart(101, 'h|2019', '2026-06-01'),   # same starts,
                         prevstart(102, 'h|2019', '2026-06-15')])  # renumbered
    db.recompute_start_intervals()
    assert 0 not in intervals(db).values()
    assert intervals(db)['2026-06-15'] == 14


def test_first_start_of_a_horse_keeps_the_epoch_sentinel(db):
    """No predecessor, so the gap is unknowable — a value to filter, not a real
    interval."""
    db.store_prevstarts([prevstart(1, 'h|2019', '2026-06-01')])
    db.recompute_start_intervals()
    assert intervals(db)['2026-06-01'] > 20000


def test_intervals_do_not_run_across_horses(db):
    db.store_prevstarts([prevstart(1, 'a|2019', '2026-06-01'),
                         prevstart(2, 'b|2020', '2026-06-15')])
    db.recompute_start_intervals()
    assert intervals(db)['2026-06-15'] > 20000   # b's own first start


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
    assert intervals(db)['2026-06-15'] > 20000
    db.store_prevstarts([prevstart(1, 'h|2019', '2026-06-01')])
    db.recompute_start_intervals()
    assert intervals(db)['2026-06-15'] == 14


def coaches(db):
    return dict(db.conn.execute(
        'SELECT meetDate, coachName FROM archive.prev_start').fetchall())


def crawled_race(db, *, race_id, card_id, number, meet_date, horse_key, coach):
    db.store_cards([card(card_id, meet_date)])
    db.store_races([race(race_id, card_id, number)])
    db.store_starts([start(race_id, 1, horse_key, coach)])


def test_prev_start_coach_comes_from_the_crawled_race(db):
    crawled_race(db, race_id=10, card_id=1, number=3, meet_date='2026-06-01',
                 horse_key='h|2019', coach='Old Trainer')
    db.store_prevstarts([prevstart(1, 'h|2019', '2026-06-01', race_number=3)])
    db.recompute_prev_start_coaches()
    assert coaches(db)['2026-06-01'] == 'Old Trainer'


def test_prev_start_coach_is_the_trainer_of_that_day_not_a_later_one(db):
    """The horse changed trainers; each past start must keep its own."""
    crawled_race(db, race_id=10, card_id=1, number=3, meet_date='2026-06-01',
                 horse_key='h|2019', coach='Old Trainer')
    crawled_race(db, race_id=20, card_id=2, number=4, meet_date='2026-08-01',
                 horse_key='h|2019', coach='New Trainer')
    db.store_prevstarts([prevstart(1, 'h|2019', '2026-06-01', race_number=3),
                         prevstart(2, 'h|2019', '2026-08-01', race_number=4)])
    db.recompute_prev_start_coaches()
    assert coaches(db) == {'2026-06-01': 'Old Trainer', '2026-08-01': 'New Trainer'}


def test_prev_start_coach_stays_null_outside_the_crawl_window(db):
    """No archived race for that day, so there is no honest value to use."""
    crawled_race(db, race_id=10, card_id=1, number=3, meet_date='2026-06-01',
                 horse_key='h|2019', coach='Old Trainer')
    db.store_prevstarts([prevstart(9, 'h|2019', '2023-01-05', race_number=7)])
    db.recompute_prev_start_coaches()
    assert coaches(db)['2023-01-05'] is None


def test_prev_start_coach_does_not_leak_between_horses(db):
    crawled_race(db, race_id=10, card_id=1, number=3, meet_date='2026-06-01',
                 horse_key='a|2019', coach='A Trainer')
    db.store_prevstarts([prevstart(1, 'b|2020', '2026-06-01', race_number=3)])
    db.recompute_prev_start_coaches()
    assert coaches(db)['2026-06-01'] is None
