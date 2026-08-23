"""The queries behind `veikkaus stats`.

Each test pins one of the traps `horse_stats` exists to handle once: the
withdrawn entries that carry equipment but never raced, a horse whose name
Veikkaus spells three ways, a start with no placement that is still a start,
and the unknown start interval that must not pass for a quick turnaround.

Every `hstart` row belongs to one horse **and** to one trainer, so the generic
tests read the same archive twice — once per `horse_stats.Subject` — with no
second fixture and no duplicated expectations. A subject whose FROM clause
counts the wrong rows fails against the fixture the other subject passes.
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
           distance_code='ke', auto_start=DERIVE, gallop=None,
           placing_raw=None, dq=None, start_track=None, win_odd=None,
           prize_won=None, horse_name=None, horse_id=None, trainer='T1',
           trainer_name='Pasi Vaittinen', driver=None, driver_first='Santtu',
           driver_last='Raitala'):
    """A heppa_start row with only the columns these tests care about set.

    `auto_start` defaults to what parse_heppa_auto_start() would make of
    `distance_code`, because that is the only way the column is ever filled.

    The trainer and the driver default to one person each, so every row a
    fixture writes is one trainer's row as well as one horse's. `horse_name`
    defaults to the key, because a trainer's start list names the horse and a
    NULL there would be a blank column in every test.
    """
    row = [None] * HEPPA_START_COLUMNS
    row[0], row[1], row[2], row[3] = meet_date, 'TK', race_number, program_number
    # horseId is in the primary key now, so it cannot be NULL; unset, it is
    # derived from the name so two different horses stay two.
    row[4] = horse_key
    row[5] = horse_id or f'id-{horse_name or horse_key}'
    row[6] = horse_name or horse_key
    row[9], row[11] = start_track, distance_code
    row[12], row[13], row[14] = placing_raw, placement, dq
    row[15], row[16] = gallop, absent
    row[19] = _auto(distance_code) if auto_start is DERIVE else auto_start
    row[21], row[22] = prize_won, win_odd
    row[25], row[26], row[27] = driver, driver_first, driver_last
    row[29], row[30] = trainer, trainer_name
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


HORSE_KEY = 'boomer|2015'
TRAINER_ID = 'T1'


@pytest.fixture(params=[(horse_stats.HORSE, HORSE_KEY), (horse_stats.TRAINER, TRAINER_ID)],
                ids=['horse', 'trainer'])
def who(request):
    """The same rows, read as one horse and as one trainer.

    Every `hstart` puts its row under both, so the two subjects see the same
    counts and the tests below keep one set of literals.
    """
    return request.param


def axis(title):
    """One breakdown by name, since the SQL is generated per axis now."""
    return next(a for a in horse_stats.BREAKDOWNS if a.title.startswith(title))


def overall(db, key=HORSE_KEY, subject=horse_stats.HORSE):
    return horse_stats.fetch(db.conn, axis('Overall').breakdown(subject), [key])[1][0]


def rows_of(db, one, key=HORSE_KEY, subject=horse_stats.HORSE):
    return horse_stats.fetch(db.conn, one.breakdown(subject), [key])[1]


def starts_of(db, one, bucket=None, key=HORSE_KEY, subject=horse_stats.HORSE):
    """The drill-down behind one bucket row: (column names, rows)."""
    return horse_stats.bucket_starts(db.conn, subject, one, key, bucket)


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
    assert rows_of(db, axis('Shoes')) == [('K / K', 2, 1, 0, 0, 0)]


def test_a_start_with_no_placement_still_counts_as_a_start(db):
    """36,054 non-absent rows — one start in eight — have a NULL placement: a
    gallop or a disqualification. They are real starts and belong in the
    denominator, or every win rate comes out inflated."""
    db.store_horses([horse('boomer|2015', 'Boomer')])
    db.store_heppa_starts([hstart('2026-01-01', 'boomer|2015', placement=None)])
    assert overall(db) == (1, 0, 0, 0, 0)


def test_every_breakdown_sums_back_to_the_overall_start_count(db, who):
    """The one check that catches a NULL falling out of a grouping.

    The capped axis is exempt, and the exemption is asserted rather than
    tolerated: a top-3 breakdown may not sum back, but it may never show a
    fourth row or claim more starts than the subject has. Its title is what
    tells a reader the same thing.
    """
    subject, key = who
    db.store_horses([horse('boomer|2015', 'Boomer')])
    db.store_heppa_starts([
        hstart('2026-01-01', 'boomer|2015', placement=1, interval=None),
        hstart('2026-01-20', 'boomer|2015', placement=2, front='E', cart='K', interval=19),
        hstart('2026-04-01', 'boomer|2015', rear='X', cart='X', interval=71),
    ])
    starts = overall(db, key, subject)[0]
    assert starts == 3
    for one in horse_stats.BREAKDOWNS[1:]:
        counted = sum(row[1] for row in rows_of(db, one, key, subject))
        if one.limit:
            assert len(rows_of(db, one, key, subject)) <= one.limit
            assert counted <= starts
        else:
            assert counted == starts


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
    names, rows = horse_stats.search(db.conn, horse_stats.HORSE, 'humble stance fr')
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
    assert horse_stats.search(db.conn, horse_stats.HORSE, 'boomer')[1] == [
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
    assert rows_of(db, axis('Days since')) == [
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
    assert [row[0] for row in rows_of(db, axis('Days since'))] == [
        '<= 14 days', '15-30 days', '31-60 days', '> 60 days']


def test_the_bucket_boundaries_are_where_they_look(db):
    """A zero-day gap is real — heats and a final on one card, 342 rows of it —
    and belongs in the shortest bucket, not in 'unknown'."""
    db.store_horses([horse('boomer|2015', 'Boomer')])
    db.store_heppa_starts([
        hstart('2026-01-01', 'boomer|2015', race_number=n, interval=days)
        for n, days in enumerate((0, 14, 15, 30, 31, 60, 61), start=1)])
    assert rows_of(db, axis('Days since')) == [
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
    assert rows_of(db, axis('Shoes')) == [
        ('E / K', 1, 0, 1, 0, 0), ('K / K', 1, 1, 0, 0, 0), ('X / X', 1, 0, 0, 0, 0)]


def test_carts_are_counted_by_the_registry_code(db):
    """specialCart is Heppa's americanSulkyKEX — K yes, E no, X not reported."""
    db.store_horses([horse('boomer|2015', 'Boomer')])
    db.store_heppa_starts([
        hstart('2026-01-01', 'boomer|2015', cart='K', placement=3),
        hstart('2026-01-08', 'boomer|2015', cart='K', placement=1),
        hstart('2026-01-15', 'boomer|2015', cart='E'),
    ])
    assert rows_of(db, axis('Cart')) == [
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
    assert rows_of(db, axis('Distance')) == [('ke', 2, 1, 1, 0, 0)]


def test_distance_classes_are_ordered_short_to_long_not_alphabetically(db):
    """Alphabetically these come out ke, kp, ly, pi. Observed spans put them
    ly 600-1,980 m, ke 2,000-2,480, kp 2,500-2,860, pi 3,020-4,240."""
    db.store_horses([horse('boomer|2015', 'Boomer')])
    db.store_heppa_starts([
        hstart('2026-01-01', 'boomer|2015', race_number=n, distance_code=code)
        for n, code in enumerate(('api', 'kp', 'aly', 'ke'), start=1)])
    assert [row[0] for row in rows_of(db, axis('Distance'))] == ['ly', 'ke', 'kp', 'pi']


def test_an_unrecognised_distance_code_is_kept_and_sorted_last(db):
    """Every breakdown has to sum back to the overall start count, so a code
    outside the four is its own row rather than dropped — as is a missing one."""
    db.store_horses([horse('boomer|2015', 'Boomer')])
    db.store_heppa_starts([
        hstart('2026-01-01', 'boomer|2015', distance_code='ke'),
        hstart('2026-01-08', 'boomer|2015', distance_code='zz'),
        hstart('2026-01-15', 'boomer|2015', distance_code=None),
    ])
    rows = rows_of(db, axis('Distance'))
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
    assert rows_of(db, axis('Start type')) == [
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


def test_gallops_are_counted_in_every_breakdown(db, who):
    """One column per breakdown, not one table of its own: the question is
    always 'how often, under these conditions'."""
    subject, key = who
    db.store_horses([horse('boomer|2015', 'Boomer')])
    db.store_heppa_starts([
        hstart('2026-01-01', 'boomer|2015', front='E', cart='K', distance_code='aly',
               interval=None, gallop=True),
        hstart('2026-01-08', 'boomer|2015', interval=7, gallop=False),
    ])
    for one in horse_stats.BREAKDOWNS:
        assert sum(row[-1] for row in rows_of(db, one, key, subject)) == 1


def test_gallop_is_the_last_column_of_every_breakdown(db, who):
    """The TUI puts its blank spacer column in front of the last cell, so the
    gallop count has to stay there. Appending a column to PLACINGS without
    moving the spacer would separate the wrong pair."""
    subject, key = who
    db.store_horses([horse('boomer|2015', 'Boomer')])
    db.store_heppa_starts([hstart('2026-01-01', 'boomer|2015')])
    for one in horse_stats.BREAKDOWNS:
        assert horse_stats.fetch(db.conn, one.breakdown(subject), [key])[0][-1] == 'gallop'


# --- drilling down ---------------------------------------------------------
#
# Clicking a bucket row lists the starts behind it, and the filter is generated
# from the same expression that labelled the bucket. These pin that the two
# cannot disagree.


def spread(db):
    """One horse with starts spread across every axis, including the NULLs."""
    db.store_horses([horse('boomer|2015', 'Boomer')])
    db.store_heppa_starts([
        hstart('2026-01-01', 'boomer|2015', placement=1, interval=None),
        hstart('2026-01-08', 'boomer|2015', placement=2, front='E', cart='K',
               distance_code='aly', interval=7, gallop=True),
        hstart('2026-01-08', 'boomer|2015', race_number=9, front='X', rear='X',
               distance_code='api', interval=0),
        hstart('2026-02-20', 'boomer|2015', distance_code=None, cart='X', interval=43),
        hstart('2026-06-01', 'boomer|2015', placement=3, distance_code='kp', interval=101),
        hstart('2026-06-08', 'boomer|2015', absent=True, front='E', rear='E', cart='K'),
    ])


def test_every_bucket_drills_down_to_exactly_its_starts(db, who):
    """The invariant the generated SQL exists for: one label expression means
    the count a bucket shows and the rows it opens cannot drift apart. A
    hand-written second copy of the CASE logic is what this would catch.

    True of the capped axis too: the cap trims which buckets are on screen, and
    a bucket that is on screen still opens exactly what it says.
    """
    subject, key = who
    spread(db)
    for one in horse_stats.BREAKDOWNS:
        for row in rows_of(db, one, key, subject):
            bucket = row[0] if one.label else None
            starts = row[1] if one.label else row[0]
            assert len(starts_of(db, one, bucket, key, subject)[1]) == starts, (
                one.title, bucket)


def test_the_overall_row_drills_down_to_every_start(db, who):
    """Overall has no label, so it groups nothing and filters nothing."""
    subject, key = who
    spread(db)
    assert (len(starts_of(db, axis('Overall'), None, key, subject)[1])
            == overall(db, key, subject)[0] == 5)


def test_absent_starts_never_appear_in_a_drill_down(db):
    """The withdrawn entry in `spread` carries E / E shoes and a K cart, so a
    drill-down that forgot the filter would show it under both."""
    spread(db)
    dates = [row[0] for row in starts_of(db, axis('Overall'))[1]]
    assert '2026-06-08' not in dates
    assert [row[0] for row in rows_of(db, axis('Shoes'))] == ['K / K', 'E / K', 'X / X']


def test_the_unknown_interval_bucket_drills_down_to_the_start_with_no_predecessor(db):
    """The longest and most breakable of the labels, and a NULL behind it."""
    spread(db)
    names, rows = starts_of(db, axis('Days since'), 'unknown (no earlier start known)')
    assert [row[0] for row in rows] == ['2026-01-01']


def test_the_unknown_distance_bucket_drills_down_to_the_start_with_no_code(db):
    spread(db)
    assert [row[0] for row in starts_of(db, axis('Distance'), 'unknown')[1]] == ['2026-02-20']


def test_a_null_equipment_code_is_a_bucket_that_can_still_be_opened(db):
    """`||` propagates a NULL, so an uncoalesced shoe label would be NULL — a
    blank row that opens nothing, because `NULL = ?` is never true. Zero
    archive rows have it today, which is data rather than construction."""
    db.store_horses([horse('boomer|2015', 'Boomer')])
    db.store_heppa_starts([
        hstart('2026-01-01', 'boomer|2015', front=None),
        hstart('2026-01-08', 'boomer|2015', cart=None),
    ])
    for one, bucket in ((axis('Shoes'), 'unknown'), (axis('Cart'), 'unknown')):
        assert bucket in [row[0] for row in rows_of(db, one)]
        assert len(starts_of(db, one, bucket)[1]) == 1


def test_a_start_with_no_placement_reads_as_its_outcome_code(db):
    """`placement` is NULL on 12.3 % of non-absent starts, and in a start list a
    blank cell reads as data we do not have rather than as what happened."""
    db.store_horses([horse('boomer|2015', 'Boomer')])
    db.store_heppa_starts([
        hstart('2026-01-01', 'boomer|2015', placement=4, placing_raw='4'),
        hstart('2026-01-08', 'boomer|2015', placing_raw='0', dq='hpl'),
        hstart('2026-01-15', 'boomer|2015', placing_raw='108', dq='hlo'),
        hstart('2026-01-22', 'boomer|2015', placing_raw='0'),
    ])
    names, rows = starts_of(db, axis('Overall'))
    assert names == ['date', 'trk', 'race', 'dist', 'lane', 'plc', 'km time', 'odds',
                     'prize', 'driver']
    assert [row[5] for row in rows] == ['-', 'hlo', 'hpl', '4']


def test_the_start_list_is_newest_first(db):
    """Newest is the start you want, and within a card the later race is the
    later start — heats and a final sit on one day."""
    spread(db)
    rows = starts_of(db, axis('Overall'))[1]
    assert [(row[0], row[2]) for row in rows] == [
        ('2026-06-01', 1), ('2026-02-20', 1), ('2026-01-08', 9), ('2026-01-08', 1),
        ('2026-01-01', 1)]


def test_every_axis_drills_down_to_the_same_columns(db, who):
    """One column set for all seven is what lets the panel be one widget.

    Within a subject, not across: a trainer's list names the horse and a
    horse's does not, and what matters is that one panel renders every axis of
    whichever subject is on screen.
    """
    subject, key = who
    spread(db)
    assert len({tuple(starts_of(
        db, one, (rows_of(db, one, key, subject)[0][0] if one.label else None),
        key, subject)[0]) for one in horse_stats.BREAKDOWNS}) == 1


def test_the_start_list_carries_the_post_the_odds_and_the_prize(db):
    """Three columns with three different NULL stories. `startTrack` is never
    NULL; `prizeWon` is never NULL either and its 0 is what an unplaced start
    won, not a gap; `winOdd` is NULL on the 44,995 local and pony starts with no
    betting, and its 64 stored zeros are 'not reported' rather than a price of
    nothing, since 1.00 is the floor of a win odd. The odds are text because
    round() would print a 2.60 price as 2.6."""
    db.store_horses([horse('boomer|2015', 'Boomer')])
    db.store_heppa_starts([
        hstart('2026-01-01', 'boomer|2015', placement=1, start_track=5,
               win_odd=1002, prize_won=2200),
        hstart('2026-01-08', 'boomer|2015', start_track=12, win_odd=None, prize_won=0),
        hstart('2026-01-15', 'boomer|2015', start_track=1, win_odd=0, prize_won=0),
    ])
    names, rows = starts_of(db, axis('Overall'))
    assert [(row[names.index('lane')], row[names.index('odds')], row[names.index('prize')])
            for row in rows] == [(1, None, 0), (12, None, 0), (5, '10.02', 2200)]


# --- the trainer subject ---------------------------------------------------
#
# A trainer asks the same seven questions of the same table; only the FROM
# clause and the drill-down's horse column differ. These pin the parts that are
# not shared, and the one that would be quietly wrong if the horse subject's
# FROM clause were reused.


def test_a_trainer_s_starts_include_a_horse_with_no_resolved_identity(db):
    """The reason _TRAINER_FROM joins nothing. 27,430 of the archive's 293,843
    non-absent rows — 9 % — have a NULL horseKey, because
    RECOMPUTE_HEPPA_START_HORSEKEY never reached them; mostly local and pony
    meetings. They are starts the trainer really had, and joining archive.horse
    the way the horse subject must would drop every one of them without an
    error anywhere. The horse's name is on the row, so nothing is lost."""
    db.store_horses([horse('boomer|2015', 'Boomer')])
    db.store_heppa_starts([
        hstart('2026-01-01', 'boomer|2015', horse_name='Boomer', placement=1),
        hstart('2026-01-01', None, program_number=2, horse_name='Nameless', placement=2),
    ])
    assert overall(db, TRAINER_ID, horse_stats.TRAINER) == (2, 1, 1, 0, 0)
    assert overall(db) == (1, 1, 0, 0, 0)
    names, rows = starts_of(db, axis('Overall'), None, TRAINER_ID, horse_stats.TRAINER)
    assert [row[names.index('horse')] for row in rows] == ['Boomer', 'Nameless']


def test_two_trainers_with_the_same_name_are_counted_apart(db):
    """35 of the archive's 4,630 trainer names belong to two different people,
    which is why the identity is the 19-digit id and never the name."""
    db.store_horses([horse('boomer|2015', 'Boomer')])
    db.store_heppa_starts([
        hstart('2026-01-01', 'boomer|2015', placement=1),
        hstart('2026-01-01', 'boomer|2015', program_number=2, trainer='T2', placement=2),
        hstart('2026-01-08', 'boomer|2015', trainer='T2'),
    ])
    assert overall(db, 'T1', horse_stats.TRAINER) == (1, 1, 0, 0, 0)
    assert overall(db, 'T2', horse_stats.TRAINER) == (2, 0, 1, 0, 0)
    rows = horse_stats.search(db.conn, horse_stats.TRAINER, 'pasi')[1]
    assert [(row[0], row[1], row[-1]) for row in rows] == [
        ('Pasi Vaittinen', 2, 'T2'), ('Pasi Vaittinen', 1, 'T1')]


def test_a_trainer_search_is_grouped_on_the_id_and_returns_it_last(db):
    """The hit list's contract: the last column is the identity the caller
    passes back, and `horses`/`years` are the disambiguator the shared names
    force, since a 19-digit id is not something to read."""
    db.store_horses([horse('boomer|2015', 'Boomer')])
    db.store_heppa_starts([
        hstart('2026-01-01', 'boomer|2015', horse_id='H1'),
        hstart('2027-02-03', 'boomer|2015', horse_id='H1', race_number=2),
    ])
    names, rows = horse_stats.search(db.conn, horse_stats.TRAINER, 'pasi')
    assert names == ['trainer', 'starts', 'horses', 'years', 'trainerId']
    assert rows == [('Pasi Vaittinen', 2, 1, '2026-2027', 'T1')]


def test_a_pasted_trainer_id_finds_that_trainer_and_not_its_withdrawals(db):
    """Pasting an id from a start list is a real thing to want, so the id arm is
    `= ?` rather than a LIKE. The parentheses around the OR are what keep the
    absent filter on both arms: without them a pasted id lists withdrawals."""
    db.store_horses([horse('boomer|2015', 'Boomer')])
    db.store_heppa_starts([
        hstart('2026-01-01', 'boomer|2015', horse_id='H1'),
        hstart('2026-01-01', 'boomer|2015', program_number=2, horse_id='H2', absent=True),
    ])
    assert horse_stats.search(db.conn, horse_stats.TRAINER, 'T1')[1] == [
        ('Pasi Vaittinen', 1, 1, '2026-2026', 'T1')]


def test_a_trainer_search_counts_horses_by_registry_id_not_by_name(db):
    """Names repeat across origin countries — Elliot and Elliot (DK), both
    foaled 2016, are two real horses — so counting the string would report one
    horse where the registry knows two."""
    db.store_horses([horse('boomer|2015', 'Boomer')])
    db.store_heppa_starts([
        hstart('2026-01-01', 'boomer|2015', horse_id='H1', horse_name='Elliot'),
        hstart('2026-01-01', 'boomer|2015', program_number=2, horse_id='H2',
               horse_name='Elliot'),
    ])
    assert horse_stats.search(db.conn, horse_stats.TRAINER, 'pasi')[1][0][2] == 2


def test_the_trainer_start_list_names_the_horse_first(db):
    """A trainer's drill-down answers 'which of mine ran', so the horse leads
    and the ten shared columns follow unchanged."""
    db.store_horses([horse('boomer|2015', 'Boomer')])
    db.store_heppa_starts([hstart('2026-01-01', 'boomer|2015')])
    names, _ = starts_of(db, axis('Overall'), None, TRAINER_ID, horse_stats.TRAINER)
    assert names == ['horse', 'date', 'trk', 'race', 'dist', 'lane', 'plc', 'km time',
                     'odds', 'prize', 'driver']


# --- the driver axis -------------------------------------------------------


def four_drivers(db):
    """One subject, four drivers, one more start each than the next."""
    db.store_horses([horse('boomer|2015', 'Boomer')])
    db.store_heppa_starts([
        hstart(f'2026-01-{day:02d}', 'boomer|2015', driver_first=first,
               driver_last='Ahonen')
        for first, days in (('Aki', (1, 2, 3, 4)), ('Bea', (5, 6, 7)),
                            ('Cid', (8, 9)), ('Dan', (10,)))
        for day in days])


def test_the_driver_breakdown_keeps_only_the_three_busiest(db):
    """The one capped axis. A horse has a median of 4 drivers and a trainer a
    mean of 7.9 (max 131), so an uncapped panel would be a wall; the cap is
    named in the axis title because the table then does not sum back."""
    four_drivers(db)
    assert [(row[0], row[1]) for row in rows_of(db, axis('Driver'))] == [
        ('Aki Ahonen', 4), ('Bea Ahonen', 3), ('Cid Ahonen', 2)]


def test_a_driver_cut_by_the_cap_is_still_a_bucket_that_opens(db):
    """The cap is on the breakdown and not on `starts`: a bucket on screen opens
    exactly its count, and the query behind a bucket knows nothing about how
    many buckets were displayed."""
    four_drivers(db)
    assert len(starts_of(db, axis('Driver'), 'Aki Ahonen')[1]) == 4
    assert len(starts_of(db, axis('Driver'), 'Dan Ahonen')[1]) == 1


def test_the_driver_label_is_the_full_name_not_the_short_form(db):
    """`driverName` is the short form and it is not reliable: 113 driver ids
    carry a second, different person's short name on a few rows each, so Santtu
    Raitala's 9,212 archive starts include one reading 'V Stenman'. Grouping on
    it would split a driver's row and could cost them a place in the top
    three."""
    db.store_horses([horse('boomer|2015', 'Boomer')])
    db.store_heppa_starts([
        hstart('2026-01-01', 'boomer|2015', driver='S Raitala'),
        hstart('2026-01-08', 'boomer|2015', driver='V Stenman'),
    ])
    assert rows_of(db, axis('Driver')) == [('Santtu Raitala', 2, 0, 0, 0, 0)]
