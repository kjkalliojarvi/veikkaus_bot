"""The queries behind `veikkaus horse`.

Each test pins one of the traps `horse_stats` exists to handle once: the
withdrawn entries that carry equipment but never raced, a horse whose name
Veikkaus spells three ways, a start with no placement that is still a start,
and the unknown start interval that must not pass for a quick turnaround.
"""
import duckdb
import pytest

from veikkaus_bot import archive_db, horse_stats
from veikkaus_bot.archive_db import ArchiveDb

HORSE_COLUMNS = archive_db.INSERT_HORSE.count('?')
HEPPA_START_COLUMNS = archive_db.INSERT_HEPPA_START.count('?')


def horse(horse_key, name, canonical_key=None, birth_year=2015):
    """A horse row; canonicalKey defaults to the horse's own key."""
    row = [None] * HORSE_COLUMNS
    row[0], row[1], row[2] = horse_key, name, birth_year
    row[9] = canonical_key or horse_key
    return tuple(row)


DERIVE = object()   # 'not given', so that auto_start=None can mean NULL


def hstart(meet_date, horse_key, *, race_number=1, program_number=1, placement=None,
           absent=None, front='K', rear='K', cart='E', interval=None,
           distance_code='ke', auto_start=DERIVE, gallop=None):
    """A heppa_start row with only the columns these tests care about set.

    `auto_start` defaults to what parse_heppa_auto_start() would make of
    `distance_code`, because that is the only way the column is ever filled.
    """
    row = [None] * HEPPA_START_COLUMNS
    row[0], row[1], row[2], row[3] = meet_date, 'TK', race_number, program_number
    row[4] = horse_key
    row[11] = distance_code
    row[13], row[15], row[16] = placement, gallop, absent
    row[19] = _auto(distance_code) if auto_start is DERIVE else auto_start
    row[33], row[34], row[35] = front, rear, cart
    row[41] = interval
    return tuple(row)


def _auto(distance_code):
    return distance_code.startswith('a') if distance_code else None


@pytest.fixture
def db():
    conn = duckdb.connect(':memory:')
    archive_db.create(conn)
    return ArchiveDb(conn)


def overall(db, key='boomer|2015'):
    return horse_stats.fetch(db.conn, horse_stats.SQL_OVERALL, [key])[1][0]


def rows_of(db, sql, key='boomer|2015'):
    return horse_stats.fetch(db.conn, sql, [key])[1]


# --- absent, and what a start is ------------------------------------------


def test_absent_starts_are_excluded_from_every_count(db):
    """A withdrawn entry carries shoes and a cart, because entry data is what
    the registry holds at that point — but the horse never went to the gate.
    Counting one invents a shoe combination it never raced in, 21,138 times
    over the archive."""
    db.store_horses([horse('boomer|2015', 'Boomer')])
    db.store_heppa_starts([
        hstart('2026-01-01', 'boomer|2015', placement=1),
        hstart('2026-01-08', 'boomer|2015', placement=4),
        hstart('2026-01-15', 'boomer|2015', absent=True, front='E', rear='E'),
    ])
    assert overall(db) == (2, 1, 0, 0, 0)
    assert rows_of(db, horse_stats.SQL_SHOES) == [('K / K', 2, 1, 0, 0, 0)]


def test_a_start_with_no_placement_still_counts_as_a_start(db):
    """36,054 non-absent rows — one start in eight — have a NULL placement: a
    gallop or a disqualification. They are real starts and belong in the
    denominator, or every win rate comes out inflated."""
    db.store_horses([horse('boomer|2015', 'Boomer')])
    db.store_heppa_starts([hstart('2026-01-01', 'boomer|2015', placement=None)])
    assert overall(db) == (1, 0, 0, 0, 0)


def test_every_breakdown_sums_back_to_the_overall_start_count(db):
    """The one check that catches a NULL falling out of a grouping."""
    db.store_horses([horse('boomer|2015', 'Boomer')])
    db.store_heppa_starts([
        hstart('2026-01-01', 'boomer|2015', placement=1, interval=None),
        hstart('2026-01-20', 'boomer|2015', placement=2, front='E', cart='K', interval=19),
        hstart('2026-04-01', 'boomer|2015', rear='X', cart='X', interval=71),
    ])
    starts = overall(db)[0]
    assert starts == 3
    for _, sql in horse_stats.BREAKDOWNS[1:]:
        assert sum(row[1] for row in rows_of(db, sql)) == starts


# --- identity --------------------------------------------------------------


def test_a_search_hit_on_a_name_variant_reports_the_canonical_horse_s_starts(db):
    """Veikkaus spells one import's name three ways, and the starts hang off
    the canonical key. Filtering and grouping in one pass restricts the join to
    the matched rows, so searching a variant reports the horse as having no
    starts — verified against the archive, where 'humble stance fr' returns 0
    one-stage and 2 two-stage."""
    db.store_horses([
        horse('humble stance (fr)|2017', 'Humble Stance* (FR)', birth_year=2017),
        horse('humble stance fr (fr)|2017', 'Humble Stance FR* (FR)',
              canonical_key='humble stance (fr)|2017', birth_year=2017),
    ])
    db.store_heppa_starts([hstart('2026-01-01', 'humble stance (fr)|2017', placement=1)])
    names, rows = horse_stats.search_horses(db.conn, 'humble stance fr')
    assert names == ['horse', 'born', 'starts', 'canonicalKey']
    assert rows == [('Humble Stance* (FR)', 2017, 1, 'humble stance (fr)|2017')]


def test_starts_are_counted_across_every_horsekey_of_one_canonical_horse(db):
    """This guards the query rather than reproducing today's data: the
    recomputes happen to leave `heppa_start.horseKey` equal to `canonicalKey`,
    so the split state is written here directly. Grouping on `horseKey` would
    answer for a fraction of the career the moment that stops holding."""
    db.store_horses([
        horse('humble stance (fr)|2017', 'Humble Stance* (FR)', birth_year=2017),
        horse('humble stance|2017', 'Humble Stance',
              canonical_key='humble stance (fr)|2017', birth_year=2017),
    ])
    db.store_heppa_starts([
        hstart('2026-01-01', 'humble stance (fr)|2017', placement=1),
        hstart('2026-01-08', 'humble stance|2017', placement=2),
    ])
    assert overall(db, 'humble stance (fr)|2017') == (2, 1, 1, 0, 0)


def test_a_horse_with_no_starts_is_still_a_search_hit(db):
    """5,419 of the archive's 17,099 horses have no heppa_start row — mostly
    the Swedish simulcast, which the Finnish registry has no record of. The
    search must answer 'found it, nothing here' rather than 'no such horse'."""
    db.store_horses([horse('boomer|2015', 'Boomer')])
    assert horse_stats.search_horses(db.conn, 'boomer')[1] == [
        ('Boomer', 2015, 0, 'boomer|2015')]


# --- start intervals -------------------------------------------------------


def test_a_null_start_interval_is_its_own_bucket_and_comes_last(db):
    """On heppa_start the unknown gap is NULL, not prev_start's epoch sentinel,
    so a bare `<= 14` predicate would file every horse's earliest start as a
    quick turnaround."""
    db.store_horses([horse('boomer|2015', 'Boomer')])
    db.store_heppa_starts([
        hstart('2026-01-01', 'boomer|2015', interval=None),
        hstart('2026-01-04', 'boomer|2015', race_number=2, interval=3),
    ])
    assert rows_of(db, horse_stats.SQL_INTERVAL) == [
        ('<= 14 days', 1, 0, 0, 0, 0),
        ('unknown (no earlier start known)', 1, 0, 0, 0, 0),
    ]


def test_interval_buckets_are_ordered_by_length_not_alphabetically(db):
    """Sorted as text these come out '15-30', '31-60', '<= 14', '> 60'. The
    order below is the one a reader expects, and it comes from a hidden numeric
    sort key that never reaches the table."""
    db.store_horses([horse('boomer|2015', 'Boomer')])
    db.store_heppa_starts([
        hstart('2026-01-01', 'boomer|2015', race_number=n, interval=days)
        for n, days in enumerate((3, 20, 45, 100), start=1)])
    assert [row[0] for row in rows_of(db, horse_stats.SQL_INTERVAL)] == [
        '<= 14 days', '15-30 days', '31-60 days', '> 60 days']


def test_the_bucket_boundaries_are_where_they_look(db):
    """A zero-day gap is real — heats and a final on one card, 342 rows of it —
    and belongs in the shortest bucket, not in 'unknown'."""
    db.store_horses([horse('boomer|2015', 'Boomer')])
    db.store_heppa_starts([
        hstart('2026-01-01', 'boomer|2015', race_number=n, interval=days)
        for n, days in enumerate((0, 14, 15, 30, 31, 60, 61), start=1)])
    assert rows_of(db, horse_stats.SQL_INTERVAL) == [
        ('<= 14 days', 2, 0, 0, 0, 0),
        ('15-30 days', 2, 0, 0, 0, 0),
        ('31-60 days', 2, 0, 0, 0, 0),
        ('> 60 days', 1, 0, 0, 0, 0),
    ]


# --- the other two breakdowns ---------------------------------------------


def test_shoe_combinations_keep_the_registry_s_codes_verbatim(db):
    """X is 'not reported', not a third kind of shoeing, so it is its own row
    rather than folded into the majority."""
    db.store_horses([horse('boomer|2015', 'Boomer')])
    db.store_heppa_starts([
        hstart('2026-01-01', 'boomer|2015', placement=1),
        hstart('2026-01-08', 'boomer|2015', front='E', rear='K', placement=2),
        hstart('2026-01-15', 'boomer|2015', front='X', rear='X'),
    ])
    assert rows_of(db, horse_stats.SQL_SHOES) == [
        ('E / K', 1, 0, 1, 0, 0), ('K / K', 1, 1, 0, 0, 0), ('X / X', 1, 0, 0, 0, 0)]


def test_carts_are_counted_by_the_registry_code(db):
    """specialCart is Heppa's americanSulkyKEX — K yes, E no, X not reported."""
    db.store_horses([horse('boomer|2015', 'Boomer')])
    db.store_heppa_starts([
        hstart('2026-01-01', 'boomer|2015', cart='K', placement=3),
        hstart('2026-01-08', 'boomer|2015', cart='K', placement=1),
        hstart('2026-01-15', 'boomer|2015', cart='E'),
    ])
    assert rows_of(db, horse_stats.SQL_CART) == [
        ('K', 2, 1, 0, 1, 0), ('E', 1, 0, 0, 0, 0)]


# --- distance class and start type ----------------------------------------


def test_the_distance_class_drops_the_auto_start_prefix(db):
    """Heppa's eight codes are four classes and an `a` for the start type, and
    the start type is the next breakdown's axis. Keeping the prefix here would
    split every class in two and ask the same question twice."""
    db.store_horses([horse('boomer|2015', 'Boomer')])
    db.store_heppa_starts([
        hstart('2026-01-01', 'boomer|2015', distance_code='ke', placement=1),
        hstart('2026-01-08', 'boomer|2015', distance_code='ake', placement=2),
    ])
    assert rows_of(db, horse_stats.SQL_DISTANCE) == [('ke', 2, 1, 1, 0, 0)]


def test_distance_classes_are_ordered_short_to_long_not_alphabetically(db):
    """Alphabetically these come out ke, kp, ly, pi. Observed spans put them
    ly 600-1,980 m, ke 2,000-2,480, kp 2,500-2,860, pi 3,020-4,240."""
    db.store_horses([horse('boomer|2015', 'Boomer')])
    db.store_heppa_starts([
        hstart('2026-01-01', 'boomer|2015', race_number=n, distance_code=code)
        for n, code in enumerate(('api', 'kp', 'aly', 'ke'), start=1)])
    assert [row[0] for row in rows_of(db, horse_stats.SQL_DISTANCE)] == ['ly', 'ke', 'kp', 'pi']


def test_an_unrecognised_distance_code_is_kept_and_sorted_last(db):
    """Every breakdown has to sum back to the overall start count, so a code
    outside the four is its own row rather than dropped — as is a missing one."""
    db.store_horses([horse('boomer|2015', 'Boomer')])
    db.store_heppa_starts([
        hstart('2026-01-01', 'boomer|2015', distance_code='ke'),
        hstart('2026-01-08', 'boomer|2015', distance_code='zz'),
        hstart('2026-01-15', 'boomer|2015', distance_code=None),
    ])
    rows = rows_of(db, horse_stats.SQL_DISTANCE)
    assert [row[0] for row in rows] == ['ke', 'unknown', 'zz']
    assert sum(row[1] for row in rows) == overall(db)[0]


def test_start_type_reads_the_parsed_column_not_the_code(db):
    """`autoStart` is the column analysis joins on, so this counts that. On a
    Heppa row it is the same `a` prefix read twice — this test writes the two
    apart to prove which one the table is reporting."""
    db.store_horses([horse('boomer|2015', 'Boomer')])
    db.store_heppa_starts([
        hstart('2026-01-01', 'boomer|2015', distance_code='ke', auto_start=True),
        hstart('2026-01-08', 'boomer|2015', distance_code='ake', auto_start=False),
        hstart('2026-01-15', 'boomer|2015', distance_code='zz', auto_start=None),
    ])
    assert rows_of(db, horse_stats.SQL_AUTOSTART) == [
        ('auto', 1, 0, 0, 0, 0), ('unknown', 1, 0, 0, 0, 0), ('volt', 1, 0, 0, 0, 0)]


# --- gallops ---------------------------------------------------------------


def test_a_gallop_that_still_won_counts_in_both_columns(db):
    """`gallop` is orthogonal to the placings and does not subtract from them —
    2,832 of the archive's galloping starts won anyway. The column answers 'how
    often did it break', not 'what else happened instead of placing'."""
    db.store_horses([horse('boomer|2015', 'Boomer')])
    db.store_heppa_starts([
        hstart('2026-01-01', 'boomer|2015', placement=1, gallop=True),
        hstart('2026-01-08', 'boomer|2015', placement=None, gallop=True),
        hstart('2026-01-15', 'boomer|2015', placement=2, gallop=False),
    ])
    assert overall(db) == (3, 1, 1, 0, 2)


def test_gallops_are_counted_in_every_breakdown(db):
    """One column per breakdown, not one table of its own: the question is
    always 'how often, under these conditions'."""
    db.store_horses([horse('boomer|2015', 'Boomer')])
    db.store_heppa_starts([
        hstart('2026-01-01', 'boomer|2015', front='E', cart='K', distance_code='aly',
               interval=None, gallop=True),
        hstart('2026-01-08', 'boomer|2015', interval=7, gallop=False),
    ])
    for _, sql in horse_stats.BREAKDOWNS:
        assert sum(row[-1] for row in rows_of(db, sql)) == 1


def test_gallop_is_the_last_column_of_every_breakdown(db):
    """The TUI puts its blank spacer column in front of the last cell, so the
    gallop count has to stay there. Appending a column to PLACINGS without
    moving the spacer would separate the wrong pair."""
    db.store_horses([horse('boomer|2015', 'Boomer')])
    db.store_heppa_starts([hstart('2026-01-01', 'boomer|2015')])
    for _, sql in horse_stats.BREAKDOWNS:
        assert horse_stats.fetch(db.conn, sql, ['boomer|2015'])[0][-1] == 'gallop'
