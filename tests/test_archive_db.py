import duckdb
import pytest

from veikkaus_bot import archive_db
from veikkaus_bot.archive_db import ArchiveDb


# Derived from the INSERT statements rather than written down, so that adding a
# column to a table cannot leave these silently short.
PREVSTART_COLUMNS = archive_db.INSERT_PREVSTART.count('?')
START_COLUMNS = archive_db.INSERT_START.count('?')
HEPPA_START_COLUMNS = archive_db.INSERT_HEPPA_START.count('?')
HORSE_COLUMNS = archive_db.INSERT_HORSE.count('?')


def prevstart(prior_start_id, horse_key, meet_date, race_number=1):
    """A prev_start row with only the columns this test cares about set."""
    row = [None] * PREVSTART_COLUMNS
    row[0], row[1], row[2], row[6] = prior_start_id, horse_key, meet_date, race_number
    return tuple(row)


def start(race_id, start_number, horse_key, coach_name, auto_start=None,
          placement=None, km_time=None, win_odds=None):
    row = [None] * START_COLUMNS
    row[0], row[1], row[3], row[6] = race_id, start_number, horse_key, coach_name
    row[17], row[18] = placement, km_time
    row[20], row[21] = auto_start, win_odds
    return tuple(row)


def race(race_id, card_id, number, start_type=None):
    row = [None] * 15
    row[0], row[1], row[2], row[5] = race_id, card_id, number, start_type
    return tuple(row)


def card(card_id, meet_date, track_abbreviation=None):
    row = [None] * 11
    row[0], row[2], row[3] = card_id, meet_date, track_abbreviation
    return tuple(row)


def horse(horse_key, base_key=None, name=None):
    row = [None] * HORSE_COLUMNS
    row[0], row[1] = horse_key, name
    row[8] = base_key if base_key is not None else horse_key
    return tuple(row)


def heppa_start(meet_date, track_code, race_number, program_number, horse_id=None,
                placement=None, km_time=None, win_odd=None, prize_won=None,
                disqualified_code=None, gallop=None):
    """A heppa_start row with only the columns this test cares about set."""
    row = [None] * HEPPA_START_COLUMNS
    row[0], row[1], row[2], row[3] = meet_date, track_code, race_number, program_number
    row[5] = horse_id
    row[13], row[14], row[15] = placement, disqualified_code, gallop
    row[17], row[21], row[22] = km_time, prize_won, win_odd
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


def test_auto_start_is_set_for_every_runner_not_just_those_with_a_km_time(db):
    """Start type belongs to the race, so a scratched horse or one outside the
    paid places has one too — even with no km time to read a suffix from."""
    db.store_races([race(10, 1, 3, start_type='CAR_START')])
    db.store_starts([start(10, 1, 'a|2019', None, auto_start=True),   # had a km time
                     start(10, 2, 'b|2019', None, auto_start=None)])  # did not
    db.recompute_auto_starts()
    assert db.conn.execute(
        'SELECT count(*) FROM archive.start WHERE autoStart IS NULL').fetchone()[0] == 0
    assert db.conn.execute(
        'SELECT DISTINCT autoStart FROM archive.start').fetchall() == [(True,)]


def test_volt_start_races_are_not_auto(db):
    db.store_races([race(20, 1, 4, start_type='VOLT_START')])
    db.store_starts([start(20, 1, 'a|2019', None)])
    db.recompute_auto_starts()
    assert db.conn.execute('SELECT autoStart FROM archive.start').fetchone()[0] is False


def test_an_unrecognised_start_type_stays_null_rather_than_guessing(db):
    db.store_races([race(30, 1, 5, start_type='UNKNOWN')])
    db.store_starts([start(30, 1, 'a|2019', None)])
    db.recompute_auto_starts()
    assert db.conn.execute('SELECT autoStart FROM archive.start').fetchone()[0] is None


def test_prev_start_auto_start_is_filled_from_a_crawled_race(db):
    """The prev-start block always sends raceStartType=UNKNOWN, so a start with
    no recorded time can only get its type from the crawled race."""
    db.store_cards([card(1, '2026-06-01')])
    db.store_races([race(10, 1, 3, start_type='CAR_START')])
    db.store_starts([start(10, 1, 'h|2019', None)])
    db.store_prevstarts([prevstart(1, 'h|2019', '2026-06-01', race_number=3)])
    db.recompute_auto_starts()
    assert db.conn.execute('SELECT autoStart FROM archive.prev_start').fetchone()[0] is True


def test_prev_start_auto_start_from_the_km_suffix_is_not_overwritten(db):
    db.store_cards([card(1, '2026-06-01')])
    db.store_races([race(10, 1, 3, start_type='CAR_START')])
    db.store_starts([start(10, 1, 'h|2019', None)])
    row = list(prevstart(1, 'h|2019', '2026-06-01', race_number=3))
    row[16] = False                       # autoStart already read off the km time
    db.store_prevstarts([tuple(row)])
    db.recompute_auto_starts()
    assert db.conn.execute('SELECT autoStart FROM archive.prev_start').fetchone()[0] is False


# --- Heppa merge ------------------------------------------------------------
#
# The bridge is positional: (card.meetDate, upper(card.trackAbbreviation),
# race.number, start.startNumber) against (meetDate, trackCode, raceNumber,
# programNumber). Every fixture below sets up that join and then asserts on
# what the recomputes derive across it.

def savonlinna(db, **start_overrides):
    """The hand-verified Savonlinna 2026-08-08 race 1 shape: one Veikkaus card
    and race, one start, one Heppa row for the same horse."""
    db.store_cards([card(1, '2026-08-08', track_abbreviation='SN')])
    db.store_races([race(10, 1, 1)])
    db.store_starts([start(10, 9, 'boomer|2021', None, **start_overrides)])


def started(db, column):
    return db.conn.execute(f'SELECT {column} FROM archive.start').fetchone()[0]


def test_heppa_fills_a_placement_the_paid_places_never_reached(db):
    """The whole point: 195,690 starts have no placement because Veikkaus
    publishes finishing detail only for the runners a pool paid out on."""
    savonlinna(db)
    db.store_heppa_starts([heppa_start('2026-08-08', 'SN', 1, 9, placement=7,
                                       km_time='21,4')])
    db.recompute_start_from_heppa()
    assert started(db, 'placement') == 7
    assert started(db, 'kmTime') == '21,4'
    assert started(db, 'resultSource') == 'heppa'


def test_veikkaus_wins_where_it_has_an_answer(db):
    """Coalesce, never overwrite — the first three keep what the betting
    operator published, and a disagreement stays queryable rather than lost."""
    savonlinna(db, placement=1, km_time='18,8', win_odds=444)
    db.store_heppa_starts([heppa_start('2026-08-08', 'SN', 1, 9, placement=2,
                                       km_time='19,9', win_odd=999)])
    db.recompute_start_from_heppa()
    assert started(db, 'placement') == 1
    assert started(db, 'kmTime') == '18,8'
    assert started(db, 'winOddsFinal') == 444
    assert started(db, 'resultSource') == 'veikkaus'


def test_the_heppa_only_columns_are_taken_unconditionally(db):
    """Veikkaus has no equivalent of any of these — `runner.prize` is career
    earnings, and there is no disqualification code anywhere in that API."""
    savonlinna(db, placement=1)
    db.store_heppa_starts([heppa_start('2026-08-08', 'SN', 1, 9, placement=1,
                                       prize_won=700, gallop=False)])
    db.recompute_start_from_heppa()
    assert started(db, 'prizeWon') == 700
    assert started(db, 'gallop') is False


def test_a_disqualified_horse_keeps_its_code_but_no_placement(db):
    savonlinna(db)
    db.store_heppa_starts([heppa_start('2026-08-08', 'SN', 1, 9, placement=None,
                                       disqualified_code='hlo', gallop=True)])
    db.recompute_start_from_heppa()
    assert started(db, 'placement') is None
    assert started(db, 'disqualifiedCode') == 'hlo'
    assert started(db, 'resultSource') is None


def test_the_track_join_is_case_insensitive(db):
    """Veikkaus writes 'Ku', 'Tk', 'Jo'; Heppa writes 'KU', 'TK', 'JO'."""
    db.store_cards([card(1, '2026-06-01', track_abbreviation='Ku')])
    db.store_races([race(10, 1, 3)])
    db.store_starts([start(10, 5, 'h|2019', None)])
    db.store_heppa_starts([heppa_start('2026-06-01', 'KU', 3, 5, placement=4)])
    db.recompute_start_from_heppa()
    assert started(db, 'placement') == 4


def test_a_meeting_with_no_veikkaus_card_touches_nothing(db):
    """Local and pony meetings have no card at all — they must not fall through
    onto some other track's start rows."""
    savonlinna(db)
    db.store_heppa_starts([heppa_start('2026-08-08', 'PX', 1, 9, placement=3)])
    db.recompute_start_from_heppa()
    assert started(db, 'placement') is None


def test_the_race_number_is_part_of_the_join(db):
    """Heats and finals put a horse in two races on one card."""
    savonlinna(db)
    db.store_heppa_starts([heppa_start('2026-08-08', 'SN', 2, 9, placement=3)])
    db.recompute_start_from_heppa()
    assert started(db, 'placement') is None


def test_the_registry_id_reaches_the_archive_horse(db):
    """`horse_key()` is a name-and-year guess; horseId is authoritative."""
    savonlinna(db)
    db.store_horses([horse('boomer|2021')])
    db.store_heppa_starts([heppa_start('2026-08-08', 'SN', 1, 9,
                                       horse_id='7913507947789197818', placement=1)])
    db.recompute_heppa_links()
    assert db.conn.execute(
        'SELECT heppaHorseId FROM archive.horse').fetchone()[0] == '7913507947789197818'


def test_horse_key_reaches_a_meeting_only_heppa_covers(db):
    """A Heppa start carries no birth year, so identity has to travel through
    the registry id — which is what reaches the local meetings, where there is
    no archive.start row to join to."""
    savonlinna(db)
    db.store_horses([horse('boomer|2021')])
    db.store_heppa_starts([
        heppa_start('2026-08-08', 'SN', 1, 9, horse_id='791350', placement=1),
        heppa_start('2026-07-01', 'PX', 4, 2, horse_id='791350', placement=5)])
    db.recompute_heppa_links()
    keys = dict(db.conn.execute(
        'SELECT meetDate, horseKey FROM archive.heppa_start').fetchall())
    assert keys == {'2026-08-08': 'boomer|2021', '2026-07-01': 'boomer|2021'}


def test_horse_key_does_not_leak_between_horses(db):
    db.store_cards([card(1, '2026-08-08', track_abbreviation='SN')])
    db.store_races([race(10, 1, 1)])
    db.store_starts([start(10, 9, 'boomer|2021', None),
                     start(10, 3, 'darina|2018', None)])
    db.store_horses([horse('boomer|2021'), horse('darina|2018')])
    db.store_heppa_starts([heppa_start('2026-08-08', 'SN', 1, 9, horse_id='791350'),
                           heppa_start('2026-08-08', 'SN', 1, 3, horse_id='460574')])
    db.recompute_heppa_links()
    assert dict(db.conn.execute(
        'SELECT horseId, horseKey FROM archive.heppa_start').fetchall()) == {
            '791350': 'boomer|2021', '460574': 'darina|2018'}


def test_the_merge_is_deterministic_however_the_sources_arrive(db):
    """Both halves are re-derived on every parse, so a re-crawl of either
    source cannot leave a stale value behind."""
    savonlinna(db)
    db.store_heppa_starts([heppa_start('2026-08-08', 'SN', 1, 9, placement=7)])
    db.recompute_start_from_heppa()
    db.recompute_start_from_heppa()
    assert started(db, 'placement') == 7
    assert started(db, 'resultSource') == 'heppa'


def test_running_the_merge_twice_does_not_relabel_heppas_own_fill(db):
    """The merge reads `placement` to decide `resultSource` and writes it in
    the same statement, so without the reset a second run sees Heppa's fill
    sitting there and calls it Veikkaus's."""
    savonlinna(db)
    db.store_heppa_starts([heppa_start('2026-08-08', 'SN', 1, 9, placement=7)])
    db.recompute_start_from_heppa()
    db.recompute_start_from_heppa()
    assert started(db, 'placement') == 7
    assert started(db, 'resultSource') == 'heppa'


def test_withdrawn_heppa_data_is_not_left_behind(db):
    """The reset is what makes the merge a function of the two tables rather
    than of everything that has ever been merged into it."""
    savonlinna(db)
    db.store_heppa_starts([heppa_start('2026-08-08', 'SN', 1, 9, placement=7,
                                       prize_won=100)])
    db.recompute_start_from_heppa()
    db.conn.execute('DELETE FROM archive.heppa_start')
    db.recompute_start_from_heppa()
    assert started(db, 'placement') is None
    assert started(db, 'prizeWon') is None
    assert started(db, 'resultSource') is None


def test_a_veikkaus_placement_is_labelled_even_with_no_heppa_row(db):
    savonlinna(db, placement=2)
    db.recompute_start_from_heppa()
    assert started(db, 'resultSource') == 'veikkaus'


def test_heppa_does_not_touch_the_win_odds(db):
    """`resultSource` describes where `placement` came from, and one marker
    cannot honestly describe a column that can come from the odds crawl on the
    same row. archive.start.winOddsFinal stays purely Veikkaus; Heppa's final
    odd is in heppa_start.winOdd for cross-checking."""
    savonlinna(db)
    db.store_heppa_starts([heppa_start('2026-08-08', 'SN', 1, 9, placement=7,
                                       win_odd=999)])
    db.recompute_start_from_heppa()
    assert started(db, 'winOddsFinal') is None


# --- track vocabulary --------------------------------------------------------

def test_harma_is_matched_under_both_veikkaus_spellings(db):
    """Veikkaus writes Härmä as 'Hr' and 'Hr2'; Heppa has only 'HR'. Upper-casing
    alone leaves 'HR2', which matched nothing and silently cost 28 meetings."""
    db.store_cards([card(1, '2023-06-23', track_abbreviation='Hr2'),
                    card(2, '2023-07-01', track_abbreviation='Hr')])
    db.store_races([race(10, 1, 4), race(20, 2, 4)])
    db.store_starts([start(10, 3, 'a|2019', None), start(20, 3, 'b|2019', None)])
    db.store_heppa_starts([heppa_start('2023-06-23', 'HR', 4, 3, placement=6),
                           heppa_start('2023-07-01', 'HR', 4, 3, placement=7)])
    db.recompute_start_from_heppa()
    assert dict(db.conn.execute(
        'SELECT raceId, placement FROM archive.start').fetchall()) == {10: 6, 20: 7}


# --- horse identity ----------------------------------------------------------

def identities(db):
    return dict(db.conn.execute(
        'SELECT horseKey, canonicalKey FROM archive.horse').fetchall())


def test_keys_sharing_a_registry_id_resolve_to_one_horse(db):
    """Veikkaus writes an import's name three ways; the registry says it is one
    horse. That split 182 horses across 365 keys before this."""
    db.store_horses([horse('humble stance|2017', base_key='humble stance|2017'),
                     horse('humble stance (fr)|2017', base_key='humble stance|2017'),
                     horse('humble stance fr (fr)|2017', base_key='humble stance|2017')])
    db.conn.execute("UPDATE archive.horse SET heppaHorseId = '754090'")
    db.recompute_heppa_links()
    assert len(set(identities(db).values())) == 1


def test_the_registry_id_wins_over_a_matching_base_name(db):
    """'Elliot' and 'Elliot (DK)', both foaled 2016, share a base name and are
    two real horses. Grouping by the id first means the name fallback never gets
    the chance to merge them."""
    db.store_horses([horse('elliot|2016', base_key='elliot|2016'),
                     horse('elliot (dk)|2016', base_key='elliot|2016')])
    db.conn.execute("UPDATE archive.horse SET heppaHorseId = '400255' WHERE horseKey = 'elliot|2016'")
    db.conn.execute("UPDATE archive.horse SET heppaHorseId = '486517' WHERE horseKey = 'elliot (dk)|2016'")
    db.recompute_heppa_links()
    assert len(set(identities(db).values())) == 2


def test_the_base_name_carries_horses_the_registry_never_reached(db):
    db.store_horses([horse('lerin|2015', base_key='lerin|2015'),
                     horse('lerin (se)|2015', base_key='lerin|2015')])
    db.recompute_heppa_links()
    assert len(set(identities(db).values())) == 1


def test_identity_does_not_merge_different_horses(db):
    db.store_horses([horse('consta|2019', base_key='consta|2019'),
                     horse('consta|2021', base_key='consta|2021')])
    db.recompute_heppa_links()
    assert len(set(identities(db).values())) == 2


def test_identity_is_deterministic_whatever_the_insert_order(db):
    rows = [horse('b (se)|2017', base_key='b|2017'), horse('b|2017', base_key='b|2017')]
    db.store_horses(rows)
    db.recompute_heppa_links()
    first = identities(db)
    db.store_horses(list(reversed(rows)))
    db.recompute_heppa_links()
    assert identities(db) == first


def test_every_horse_gets_an_identity_even_with_no_base_key(db):
    db.conn.execute("INSERT INTO archive.horse (horseKey) VALUES ('orphan|2020')")
    db.recompute_heppa_links()
    assert identities(db) == {'orphan|2020': 'orphan|2020'}
