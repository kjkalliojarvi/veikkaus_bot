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


def prevstart(prior_start_id, horse_key, meet_date, race_number=1, result=None):
    """A prev_start row with only the columns this test cares about set."""
    row = [None] * PREVSTART_COLUMNS
    row[0], row[1], row[2], row[6] = prior_start_id, horse_key, meet_date, race_number
    row[12] = result
    return tuple(row)


def start(race_id, start_number, horse_key, coach_name, auto_start=None,
          placement=None, km_time=None, win_odds=None, scratched=None):
    row = [None] * START_COLUMNS
    row[0], row[1], row[3], row[6] = race_id, start_number, horse_key, coach_name
    row[15] = scratched
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
                disqualified_code=None, gallop=None, absent=None):
    """A heppa_start row with only the columns this test cares about set."""
    row = [None] * HEPPA_START_COLUMNS
    row[0], row[1], row[2], row[3] = meet_date, track_code, race_number, program_number
    # horseId joined the primary key when the starts abroad arrived, so it can no
    # longer be NULL. Unset, it is a per-row synthetic that matches no
    # archive.horse — the recomputes see it exactly as they saw a NULL.
    row[5] = horse_id or f'x{meet_date}{track_code}{race_number}{program_number}'
    row[13], row[14], row[15], row[16] = placement, disqualified_code, gallop, absent
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


# --- cross-source start intervals --------------------------------------------
#
# `startInterval` on archive.start and archive.heppa_start is the layoff
# feature: days since the horse's previous known start, over the union of all
# three start-bearing tables. Distinct from prev_start's own same-table column
# above, which these must leave alone.
#
# Every fixture here stores an archive.horse row and calls
# recompute_heppa_links() first, via recompute(). canonicalKey is what the
# window partitions on and heppa_start.horseKey is how a Heppa row enters the
# union at all, and both are written there.

def start_intervals(db):
    return {(r, n): v for r, n, v in db.conn.execute(
        'SELECT raceId, startNumber, startInterval FROM archive.start').fetchall()}


def heppa_intervals(db):
    """Keyed by meet date — one Heppa row per date in these fixtures."""
    return dict(db.conn.execute(
        'SELECT meetDate, startInterval FROM archive.heppa_start').fetchall())


def veikkaus_start(db, race_id, meet_date, horse_key, number=1, track='SN',
                   card_id=None, track_number=2, **start_overrides):
    """One card, one race, one start — the smallest crawled appearance."""
    card_id = race_id if card_id is None else card_id
    row = list(card(card_id, meet_date, track_abbreviation=track))
    row[5] = track_number
    db.store_cards([tuple(row)])
    db.store_races([race(race_id, card_id, number)])
    db.store_starts([start(race_id, 1, horse_key, None, **start_overrides)])


def recompute(db):
    """The production order: identity, then the window that reads it."""
    db.recompute_heppa_links()
    db.recompute_cross_source_intervals()


def test_a_start_interval_counts_days_since_the_previous_start(db):
    db.store_horses([horse('h|2019')])
    veikkaus_start(db, 10, '2026-06-01', 'h|2019')
    veikkaus_start(db, 20, '2026-06-15', 'h|2019')
    recompute(db)
    assert start_intervals(db) == {(10, 1): None, (20, 1): 14}


def test_a_heppa_only_meeting_shortens_a_veikkaus_gap(db):
    """The reason this is a union and not a window over archive.start: a local
    meeting has no Veikkaus card at all, and missing it does not merely go
    unrecorded — it inflates the next gap, here from 9 days to 30."""
    db.store_horses([horse('h|2019')])
    veikkaus_start(db, 10, '2026-06-01', 'h|2019')
    veikkaus_start(db, 20, '2026-07-01', 'h|2019')
    db.store_heppa_starts([
        heppa_start('2026-06-01', 'SN', 1, 1, horse_id='791350'),    # bridges the id
        heppa_start('2026-06-22', 'PX', 4, 2, horse_id='791350')])   # local, no card
    recompute(db)
    assert start_intervals(db)[(20, 1)] == 9
    assert heppa_intervals(db)['2026-06-22'] == 21


def test_a_prev_start_before_the_crawl_window_shortens_the_first_gap(db):
    """prev_start is the only source that reaches back before the crawl."""
    db.store_horses([horse('h|2019')])
    veikkaus_start(db, 10, '2026-06-01', 'h|2019')
    db.store_prevstarts([prevstart(1, 'h|2019', '2026-05-18', race_number=3, result='4')])
    recompute(db)
    assert start_intervals(db)[(10, 1)] == 14


def test_the_earliest_known_start_is_null_not_the_epoch_sentinel(db):
    """A nullable column has somewhere to put "unknowable", so it does not need
    prev_start's days-since-1970 sentinel. Filter this one with IS NULL."""
    db.store_horses([horse('h|2019')])
    veikkaus_start(db, 10, '2026-06-01', 'h|2019')
    recompute(db)
    assert start_intervals(db)[(10, 1)] is None


def test_a_scratched_start_gets_no_interval_and_is_not_a_predecessor(db):
    """A scratched horse did not start, so the next real gap counts past it."""
    db.store_horses([horse('h|2019')])
    veikkaus_start(db, 10, '2026-06-01', 'h|2019')
    veikkaus_start(db, 20, '2026-06-15', 'h|2019', scratched=True)
    veikkaus_start(db, 30, '2026-06-29', 'h|2019')
    recompute(db)
    assert start_intervals(db) == {(10, 1): None, (20, 1): None, (30, 1): 28}


def test_heppa_absent_overrules_veikkaus_scratched_being_false(db):
    """The 175-row case: the runners payload is entry data, so a horse withdrawn
    at the track is not marked there. Heppa is right, and every source with an
    opinion has to agree before a key counts as a start."""
    db.store_horses([horse('h|2019')])
    veikkaus_start(db, 10, '2026-06-01', 'h|2019')
    veikkaus_start(db, 20, '2026-06-15', 'h|2019', scratched=False)
    veikkaus_start(db, 30, '2026-06-29', 'h|2019')
    db.store_heppa_starts([heppa_start('2026-06-15', 'SN', 1, 1,
                                       horse_id='791350', absent=True)])
    recompute(db)
    assert start_intervals(db)[(20, 1)] is None
    assert start_intervals(db)[(30, 1)] == 28


def test_a_prev_start_row_does_not_resurrect_a_start_heppa_calls_absent(db):
    """prev_start abstains from the vote rather than casting one: it only
    supplies a date, and it is observed to list entries that were withdrawn."""
    db.store_horses([horse('h|2019')])
    veikkaus_start(db, 10, '2026-06-01', 'h|2019')
    veikkaus_start(db, 20, '2026-06-15', 'h|2019')
    veikkaus_start(db, 30, '2026-06-29', 'h|2019')
    db.store_heppa_starts([heppa_start('2026-06-15', 'SN', 1, 1,
                                       horse_id='791350', absent=True)])
    db.store_prevstarts([prevstart(1, 'h|2019', '2026-06-15', race_number=1, result='4')])
    recompute(db)
    assert start_intervals(db)[(30, 1)] == 28


def test_a_career_line_with_no_result_code_is_not_offered_as_a_start(db):
    db.store_horses([horse('h|2019')])
    veikkaus_start(db, 10, '2026-06-01', 'h|2019')
    db.store_prevstarts([prevstart(1, 'h|2019', '2026-05-18', race_number=3)])
    recompute(db)
    assert start_intervals(db)[(10, 1)] is None


def test_two_races_on_one_day_give_a_zero_day_gap(db):
    """Heats and finals put a horse in two races on one card — a real start
    twice, and a real zero-day gap. Ordered by race number within the date."""
    db.store_horses([horse('h|2019')])
    veikkaus_start(db, 10, '2026-06-01', 'h|2019', number=1, card_id=1)
    veikkaus_start(db, 20, '2026-06-01', 'h|2019', number=5, card_id=1)
    recompute(db)
    assert start_intervals(db) == {(10, 1): None, (20, 1): 0}


def test_one_start_seen_by_both_sources_is_still_one_start(db):
    """Dedup on (canonicalKey, meetDate, raceNumber), or the overlap between the
    two sources would manufacture a zero-day gap on every bridged race."""
    db.store_horses([horse('h|2019')])
    veikkaus_start(db, 10, '2026-06-01', 'h|2019')
    veikkaus_start(db, 20, '2026-06-15', 'h|2019')
    db.store_heppa_starts([heppa_start('2026-06-01', 'SN', 1, 1, horse_id='791350'),
                           heppa_start('2026-06-15', 'SN', 1, 1, horse_id='791350')])
    recompute(db)
    assert start_intervals(db)[(20, 1)] == 14
    assert heppa_intervals(db) == {'2026-06-01': None, '2026-06-15': 14}


def test_intervals_partition_on_canonical_key_not_horse_key(db):
    """Veikkaus writes an import's name inconsistently, which split 182 horses
    across 365 keys. On horseKey each fragment would get a career of its own and
    the second start here would look like a debut."""
    db.store_horses([horse('humble stance|2018'), horse('humble stance* (fr)|2018')])
    veikkaus_start(db, 10, '2026-06-01', 'humble stance|2018')
    veikkaus_start(db, 20, '2026-06-15', 'humble stance* (fr)|2018')
    db.store_heppa_starts([heppa_start('2026-06-01', 'SN', 1, 1, horse_id='791350'),
                           heppa_start('2026-06-15', 'SN', 1, 1, horse_id='791350')])
    recompute(db)
    assert start_intervals(db)[(20, 1)] == 14


def test_cross_source_intervals_do_not_run_across_horses(db):
    db.store_horses([horse('a|2019'), horse('b|2019')])
    veikkaus_start(db, 10, '2026-06-01', 'a|2019')
    veikkaus_start(db, 20, '2026-06-15', 'b|2019')
    recompute(db)
    assert start_intervals(db) == {(10, 1): None, (20, 1): None}


def test_an_unresolved_heppa_horse_key_stays_null(db):
    """No identity, no career timeline — and no predecessor offered to anyone
    else's either. On heppa_start, NULL therefore means either "no predecessor"
    or "no identity"; horseKey IS NOT NULL separates them."""
    db.store_horses([horse('h|2019')])
    veikkaus_start(db, 10, '2026-06-01', 'h|2019')
    db.store_heppa_starts([heppa_start('2026-05-18', 'PX', 4, 2, horse_id='999')])
    recompute(db)
    assert heppa_intervals(db)['2026-05-18'] is None
    assert start_intervals(db)[(10, 1)] is None


def test_the_cross_source_recompute_is_idempotent(db):
    db.store_horses([horse('h|2019')])
    veikkaus_start(db, 10, '2026-06-01', 'h|2019')
    veikkaus_start(db, 20, '2026-06-15', 'h|2019')
    recompute(db)
    once = start_intervals(db)
    recompute(db)
    assert start_intervals(db) == once == {(10, 1): None, (20, 1): 14}


def test_the_cross_source_recompute_is_deterministic_whatever_the_insert_order(db):
    db.store_horses([horse('h|2019')])
    veikkaus_start(db, 20, '2026-06-15', 'h|2019')
    veikkaus_start(db, 10, '2026-06-01', 'h|2019')
    recompute(db)
    assert start_intervals(db) == {(10, 1): None, (20, 1): 14}


def test_a_newly_crawled_earlier_start_corrects_a_previously_computed_gap(db):
    """Crawling backwards is the normal case — newest season first — so a gap
    has to shorten when its predecessor finally arrives."""
    db.store_horses([horse('h|2019')])
    veikkaus_start(db, 20, '2026-06-15', 'h|2019')
    recompute(db)
    assert start_intervals(db)[(20, 1)] is None
    veikkaus_start(db, 10, '2026-06-01', 'h|2019')
    recompute(db)
    assert start_intervals(db)[(20, 1)] == 14


def test_a_start_that_stops_qualifying_loses_its_interval(db):
    """What the reset is for. `UPDATE ... FROM` only touches rows its subquery
    joins to, so a start that drops out of the timeline — a re-crawl finds Heppa
    calling it absent — would otherwise keep the gap it had."""
    db.store_horses([horse('h|2019')])
    veikkaus_start(db, 10, '2026-06-01', 'h|2019')
    veikkaus_start(db, 20, '2026-06-15', 'h|2019')
    recompute(db)
    assert start_intervals(db)[(20, 1)] == 14
    db.store_heppa_starts([heppa_start('2026-06-15', 'SN', 1, 1,
                                       horse_id='791350', absent=True)])
    recompute(db)
    assert start_intervals(db)[(20, 1)] is None


def test_the_prev_start_interval_column_is_untouched(db):
    """The two columns answer different questions and disagree on purpose."""
    db.store_horses([horse('h|2019')])
    db.store_prevstarts([prevstart(1, 'h|2019', '2026-06-01', race_number=3, result='4')])
    db.recompute_start_intervals()
    sentinel = intervals(db)['2026-06-01']
    assert sentinel > 10000
    recompute(db)
    assert intervals(db)['2026-06-01'] == sentinel


def test_a_combination_pool_meta_card_is_not_a_start(db):
    """MM, Sl, T75, KUN, JAA and CIT are not meetings — a meta-card re-lists
    races that also run under their real track. Counting one put the horse at
    two tracks on one day and handed the next real start a zero-day gap, which
    is where 2,475 of 2,806 zero gaps came from before this filter."""
    db.store_horses([horse('h|2019')])
    veikkaus_start(db, 10, '2026-06-01', 'h|2019', track='O', track_number=17)
    veikkaus_start(db, 15, '2026-06-01', 'h|2019', track='MM', number=4, track_number=48)
    veikkaus_start(db, 20, '2026-06-15', 'h|2019', track='O', track_number=17)
    recompute(db)
    assert start_intervals(db)[(20, 1)] == 14        # not 0
    assert start_intervals(db)[(15, 1)] is None      # the meta-card row itself


def test_a_swedish_simulcast_is_a_real_start(db):
    """The other card with no Heppa counterpart, and the opposite treatment: a
    simulcast duplicates nothing, so dropping it would inflate the gap."""
    db.store_horses([horse('h|2019')])
    veikkaus_start(db, 10, '2026-06-01', 'h|2019', track='O', track_number=17)
    veikkaus_start(db, 15, '2026-06-08', 'h|2019', track='Bo-V', track_number=57)
    veikkaus_start(db, 20, '2026-06-15', 'h|2019', track='O', track_number=17)
    recompute(db)
    assert start_intervals(db)[(15, 1)] == 7
    assert start_intervals(db)[(20, 1)] == 7


# --- the starts abroad -----------------------------------------------------
#
# Heppa numbers every start at a meeting abroad `programNumber` 0, so the key
# that identifies a Finnish start no longer identifies these. The failure is
# silent — INSERT OR REPLACE keeps the last row for a key and `_insert_many`
# collapses a batch the same way — which is why it is pinned here.


def foreign_start(horse_id, meet_date='2026-08-16', track_code='BO', race_number=4,
                  placement=None):
    """A start abroad: programNumber 0, as Heppa sends it, and finnishTrack FALSE."""
    row = [None] * HEPPA_START_COLUMNS
    row[0], row[1], row[2], row[3] = meet_date, track_code, race_number, 0
    row[5], row[13], row[42] = horse_id, placement, False
    return tuple(row)


def test_several_horses_in_one_race_abroad_all_survive_one_batch(db):
    """Bollnas 2026-08-16 race 8 really does return five Finnish horses, every
    one of them programNumber 0. Without horseId in the key, four of the five are
    dropped with no error anywhere."""
    db.store_heppa_starts([foreign_start(f'H{n}', placement=n) for n in range(1, 6)])
    assert db.conn.execute("""
        SELECT count(*), count(DISTINCT horseId) FROM archive.heppa_start
        WHERE finnishTrack = FALSE""").fetchone() == (5, 5)


def test_several_horses_in_one_race_abroad_all_survive_separate_batches(db):
    """The same across batches, where INSERT OR REPLACE rather than the in-Python
    dedupe is what would have lost them."""
    for n in range(1, 6):
        db.store_heppa_starts([foreign_start(f'H{n}', placement=n)])
    assert db.conn.execute(
        'SELECT count(*) FROM archive.heppa_start').fetchone()[0] == 5


def test_one_horse_re_reported_in_one_race_abroad_is_still_one_row(db):
    """The other half of the key: same horse, same race, twice. The placement of
    the later row wins, as everywhere else in the archive."""
    db.store_heppa_starts([foreign_start('H1', placement=7),
                           foreign_start('H1', placement=4)])
    assert db.conn.execute(
        'SELECT count(*), max(placement) FROM archive.heppa_start').fetchone() == (1, 4)


def test_a_home_start_and_a_start_abroad_can_share_a_date_and_a_race_number(db):
    """Different tracks, so nothing clever is needed — but the row that says
    which is which is `finnishTrack`, and it has to survive the round trip."""
    db.store_heppa_starts([heppa_start('2026-08-16', 'Y', 4, 6, horse_id='H1'),
                           foreign_start('H1')])
    assert db.conn.execute("""
        SELECT coalesce(finnishTrack, true) AS home, count(*)
        FROM archive.heppa_start GROUP BY 1 ORDER BY 1""").fetchall() == [
            (False, 1), (True, 1)]


def test_the_primary_key_gains_horseid_on_an_older_archive(db):
    """archive_db.create() rebuilds the table when its key predates the starts
    abroad, and the rebuild has to keep every row and stamp finnishTrack TRUE —
    every pre-existing row descends from the Finnish-only results listing.
    """
    db.conn.execute('DROP TABLE archive.heppa_start')
    db.conn.execute(archive_db.CREATE_HEPPA_START_TABLE.replace(
        ', horseId));', '));').replace('finnishTrack BOOLEAN,', ''))
    db.conn.execute("INSERT INTO archive.heppa_start VALUES "
                    "('2026-08-08', 'Y', 4, 6, NULL, 'H1'" + ', NULL' * 36 + ')')
    archive_db.create(db.conn)
    assert db.conn.execute(archive_db.HEPPA_START_PK).fetchone()[0] == [
        'meetDate', 'trackCode', 'raceNumber', 'programNumber', 'horseId']
    assert db.conn.execute(
        'SELECT count(*), min(finnishTrack) FROM archive.heppa_start').fetchone() == (1, True)
    assert archive_db.migrate_heppa_start_pk(db.conn) is False


def test_two_horses_in_one_race_abroad_keep_their_own_layoffs(db):
    """The join-back in RECOMPUTE_HEPPA_START_STARTINTERVAL has to use the whole
    primary key. Every foreign start in a race is programNumber 0, so the old
    four-column match covered the entire race, and `UPDATE ... FROM` with a
    non-unique match takes an arbitrary row of the group — the two horses below
    were handed each other's layoff. Observed on the real archive before the fix:
    Combat Fighter's Solvalla start read 20 days where the union said 14.
    """
    db.store_horses([horse('early|2019', name='Early'), horse('late|2019', name='Late')])
    db.store_heppa_starts([
        # one horse raced a week before the meeting abroad, the other a month
        heppa_start('2026-08-09', 'Y', 1, 5, horse_id='EARLY'),
        heppa_start('2026-07-16', 'Y', 1, 6, horse_id='LATE'),
        foreign_start('EARLY', meet_date='2026-08-16'),
        foreign_start('LATE', meet_date='2026-08-16'),
    ])
    db.conn.execute("UPDATE archive.horse SET heppaHorseId = 'EARLY' "
                    "WHERE horseKey = 'early|2019'")
    db.conn.execute("UPDATE archive.horse SET heppaHorseId = 'LATE' "
                    "WHERE horseKey = 'late|2019'")
    recompute(db)
    assert db.conn.execute("""
        SELECT horseId, startInterval FROM archive.heppa_start
        WHERE finnishTrack = FALSE ORDER BY horseId""").fetchall() == [
            ('EARLY', 7), ('LATE', 31)]


def test_the_rebuild_drops_a_row_that_never_had_a_registry_id(db):
    """horseId is in the new key, so DuckDB will not hold a NULL there and the
    rebuild would fail on the whole table rather than on the row. None of the
    314,981 rows this was written against had one, and a re-parse would drop it
    too, so nothing recoverable is lost — but it has to be the row that goes,
    not the migration."""
    db.conn.execute('DROP TABLE archive.heppa_start')
    db.conn.execute(archive_db.CREATE_HEPPA_START_TABLE.replace(
        ', horseId));', '));').replace('finnishTrack BOOLEAN,', ''))
    for prognum, horse_id in ((6, "'H1'"), (7, 'NULL')):
        db.conn.execute("INSERT INTO archive.heppa_start VALUES "
                        f"('2026-08-08', 'Y', 4, {prognum}, NULL, {horse_id}"
                        + ', NULL' * 36 + ')')
    archive_db.create(db.conn)
    assert db.conn.execute(
        'SELECT count(*), min(horseId) FROM archive.heppa_start').fetchone() == (1, 'H1')
