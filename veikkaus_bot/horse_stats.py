"""One horse's registry starts, counted every way the TUI shows them.

The query layer behind `veikkaus horse`. Read-only, and deliberately free of
any UI import: every function takes an open connection, so the SQL is testable
against an in-memory archive the way the rest of the suite tests SQL.

Three traps are handled here once rather than in each caller, because every
hand-written version of these questions in this repo's history got at least one
of them wrong:

- **`absent` rows are excluded everywhere.** A withdrawn entry carries shoes
  and a cart, because entry data is what the registry has at that point, but
  never a placement or a startInterval. Counting one invents a shoe combination
  the horse never raced in and dilutes every rate — 21,138 rows of it.
- **Identity is `canonicalKey`, not `horseKey`.** `horse_key()` is name plus
  birth year, and Veikkaus writes an import's name inconsistently, which splits
  182 horses across 365 keys.
- **A NULL `startInterval` is its own bucket.** On `heppa_start` the unknown
  gap is NULL, not `prev_start`'s epoch sentinel, so bucketing it with a
  `<= 14` predicate silently files every horse's earliest start as a quick
  turnaround.

Each breakdown is an `Axis`, and the label expression that names its buckets is
written once: the aggregate selects it, and the drill-down behind a clicked
bucket compares against it. See `Axis`.

Run it directly to iterate on the SQL without starting Textual:

    uv run python -m veikkaus_bot.horse_stats 'com milton'
"""
from typing import NamedTuple

from .archive_db import DEFAULT_DB, db_read


# Absent rows are filtered in every query. coalesce(), not a bare NOT: `absent`
# is non-NULL on all 314,981 archive rows, but the test row builders leave
# unset columns NULL and `NOT NULL` is NULL — which would filter out every row
# the tests insert.
NOT_ABSENT = 'NOT coalesce(hs.absent, false)'

# Quoted identifiers so '1st'/'2nd'/'3rd' survive as table headers.
#
# `gallop` is orthogonal to the placings and does not subtract from them: a
# horse can gallop and still win, and 48,078 of the archive's 51,730 galloping
# starts were placed at all, 2,832 of them first. So this column overlaps the
# three before it by design — it is 'how often did it break', not a fourth
# outcome. NULL only ever occurs on absent rows, which are filtered out anyway,
# and FILTER reads a NULL as false regardless.
PLACINGS = """count(*)                                 AS starts,
           count(*) FILTER (WHERE hs.placement = 1) AS "1st",
           count(*) FILTER (WHERE hs.placement = 2) AS "2nd",
           count(*) FILTER (WHERE hs.placement = 3) AS "3rd",
           count(*) FILTER (WHERE hs.gallop)        AS gallop"""

# canonicalKey identifies, horseKey joins. The two agree on heppa_start today —
# RECOMPUTE_HEPPA_START_HORSEKEY and RECOMPUTE_HORSE_IDENTITY both write
# min(horseKey) over the same registry group — but grouping on horseKey would be
# one recompute away from answering for a fraction of a career.
_FROM = f"""
    FROM archive.heppa_start hs
    JOIN archive.horse h ON h.horseKey = hs.horseKey
    WHERE h.canonicalKey = ? AND {NOT_ABSENT}
"""

# Two stages, and that is the whole point of this query.
#
# Filtering and grouping in one pass restricts the join to the *matched* horse
# rows, so a term matching a non-canonical name variant reports a horse that
# has starts as having none: 'humble stance fr' returns 0 starts one-stage and
# 2 two-stage, for the same horse. The `matched` CTE resolves the term to
# identities; the outer join then covers every horseKey of each identity.
#
# The rest, each measured against the archive: replace(name, '*', '') on both
# sides because 6,791 names carry a mid-name '*' (`Humble Stance* (FR)`) that
# defeats typing a displayed name back in; the canonicalKey arm because those
# keys are all over crosscheck output and pasting one in is useful; any_value
# FILTER on the canonical row rather than min() over the variants; LEFT JOIN
# because 5,419 of 17,099 horses have no heppa_start row and must still be
# findable; and starts DESC because the horse you meant is the one that raced
# most, not the one whose name sorts first.
SQL_SEARCH = """
    WITH matched AS (
        SELECT DISTINCT canonicalKey
        FROM archive.horse
        WHERE lower(replace(horseName, '*', '')) LIKE '%' || lower(replace(?, '*', '')) || '%'
           OR lower(canonicalKey) LIKE '%' || lower(?) || '%'
    )
    SELECT any_value(h.horseName) FILTER (WHERE h.horseKey = h.canonicalKey) AS horse,
           any_value(h.birthYear) FILTER (WHERE h.horseKey = h.canonicalKey) AS born,
           count(*) FILTER (WHERE hs.meetDate IS NOT NULL
                              AND NOT coalesce(hs.absent, false))            AS starts,
           h.canonicalKey                                                    AS canonicalKey
    FROM matched m
    JOIN archive.horse h ON h.canonicalKey = m.canonicalKey
    LEFT JOIN archive.heppa_start hs ON hs.horseKey = h.horseKey
    GROUP BY h.canonicalKey
    ORDER BY starts DESC, horse
    LIMIT ?
"""

# The distance class, with the auto-start prefix taken off: the eight codes
# Heppa writes are four classes and an `a` for the start type — 'ke'/'ake',
# 'ly'/'aly', 'kp'/'akp', 'pi'/'api' — and the start type is an axis of its own
# below, so keeping the prefix here would split every class in two and ask the
# same question twice. substr() rather than ltrim(code, 'a'), which would eat
# every leading 'a' of a code that ever starts with two.
#
# NULL becomes its own 'unknown' row rather than disappearing: every breakdown
# has to sum back to the overall start count, and a bucket that cannot be
# clicked through to its starts is worse than an ugly label.
_CLASS = """CASE WHEN hs.distanceCode LIKE 'a%' THEN substr(hs.distanceCode, 2)
                     ELSE hs.distanceCode END"""

# Ordered short to long, because the label sorts wrong: alphabetically these
# come out ke, kp, ly, pi. Observed spans, non-absent: ly 600-1,980 m,
# ke 2,000-2,480 m, kp 2,500-2,860 m, pi 3,020-4,240 m. A code outside the four
# sorts last rather than being dropped.
_CLASS_ORDER = f"""CASE {_CLASS} WHEN 'ly' THEN 0 WHEN 'ke' THEN 1
                                     WHEN 'kp' THEN 2 WHEN 'pi' THEN 3
                                     ELSE 4 END"""

# Auto start against volt, off the parsed column rather than re-derived from the
# code above — that column is what analysis joins on, so this counts the thing
# it would count. On a Heppa row the two are the same fact read twice
# (`parse_heppa_auto_start` reads the same `a` prefix), which is why this table
# always mirrors the a/bare split of the distance table exactly. It earns its
# place anyway: start type is the axis, and it is the one that survives if a
# later source fills `autoStart` some other way.
#
# NULL is 'unknown' and stays visible: `parse_heppa_auto_start` leaves it NULL
# for a code it does not recognise rather than defaulting to volt.
_AUTO = """CASE hs.autoStart WHEN true THEN 'auto' WHEN false THEN 'volt'
                                 ELSE 'unknown' END"""

# The layoff buckets sort by length, so the label cannot be the sort key: as
# text they come out '15-30', '31-60', '<= 14', '> 60'. A second CASE gives the
# numeric key, and DuckDB takes it in GROUP BY and ORDER BY without it reaching
# the SELECT list, so the sort key never shows up in the table.
#
# NULL is its own bucket, and per horse it means exactly one thing: `absent` is
# filtered above, and the 27,430 rows RECOMPUTE_HEPPA_START_HORSEKEY never
# reached have no horseKey to join on at all. What is left is a start with no
# known predecessor across the three start-bearing tables — at most one row per
# horse. Never `startInterval > 10000` here: that sentinel is prev_start's, and
# heppa_start says unknown with NULL.
_LAYOFF = """CASE WHEN hs.startInterval IS NULL THEN 'unknown (no earlier start known)'
                      WHEN hs.startInterval <= 14   THEN '<= 14 days'
                      WHEN hs.startInterval <= 30   THEN '15-30 days'
                      WHEN hs.startInterval <= 60   THEN '31-60 days'
                                                    ELSE '> 60 days' END"""

_LAYOFF_ORDER = """CASE WHEN hs.startInterval IS NULL THEN 4
                            WHEN hs.startInterval <= 14   THEN 0
                            WHEN hs.startInterval <= 30   THEN 1
                            WHEN hs.startInterval <= 60   THEN 2
                                                          ELSE 3 END"""

# The individual starts behind a bucket. Compact on purpose — the identity of
# the start, the conditions it ran under, how it went, and who drove.
#
# The placing column coalesces, because `placement` is NULL on 12.3 % of
# non-absent starts and an empty cell would read as data we do not have. A
# disqualification shows its code, and a start that finished outside the
# placings shows '-'. Neither is a missing value.
#
# `lane` is `startTrack`, which archive_db names to match archive.start; it is
# the post, 1-15, and never NULL. `odds` is `winOdd` scaled out of hundredths,
# and NULL on the 44,995 non-absent rows of local and pony meetings where there
# was no betting — blank there is the truth. nullif(0) because 1.00 is the floor
# of a win odd, so the 64 rows storing 0 are 'not reported' rather than a price
# of nothing. `prize` is `prizeWon`, this race's money for this horse, in euros
# and never NULL: 0 is what an unplaced start won, which is a fact rather than a
# gap, and 128,262 rows say it.
#
# printf rather than round(), which returns a double and so prints 2.6 for a
# price of 2.60. printf keeps a NULL NULL, so the no-betting rows stay blank.
START_COLUMNS = """hs.meetDate AS date, hs.trackCode AS trk, hs.raceNumber AS race,
           hs.distance AS dist, hs.startTrack AS lane,
           coalesce(cast(hs.placement AS varchar), hs.disqualifiedCode, '-') AS plc,
           hs.kmTime AS "km time",
           printf('%.2f', nullif(hs.winOdd, 0) / 100.0) AS odds,
           hs.prizeWon AS prize, hs.driverName AS driver"""


class Axis(NamedTuple):
    """One way of grouping a horse's starts, and both queries about it.

    `label` is the SQL expression that produces the bucket label, and it is
    written **once**: `breakdown` selects it as the group column and `starts`
    compares it against a clicked label. Two copies would be two definitions of
    what '<= 14 days' means, free to drift apart — the trap `crosscheck.py`
    avoids by building every report on `archive_db.HEPPA_START_BRIDGE` instead
    of a hand-copied lookalike. Verified over the 400 busiest horses and all
    6,110 of their buckets: the filter returns exactly the aggregate's count,
    every time, and no label is ever NULL, so `= ?` needs no NULL-safe form.

    Every label is non-NULL by construction, and that is a requirement rather
    than a nicety: `NULL = ?` is never true, so a NULL label would be a blank
    bucket row that opens an empty list. Hence the `coalesce` around the shoe
    concatenation, where `||` propagates a NULL, and around `specialCart`. Both
    are NULL on zero archive rows today, which is data and not construction.

    `Overall` has no label. It groups nothing and filters nothing, so clicking
    it lists every start the horse has.
    """

    title: str
    column: str | None = None
    label: str | None = None
    order: str = 'starts DESC'
    sort: str | None = None      # a numeric sort key, where the label sorts wrong

    @property
    def breakdown(self) -> str:
        """Starts, placings and gallops per bucket."""
        if self.label is None:
            return f'SELECT {PLACINGS} {_FROM}'
        group = f'GROUP BY 1, {self.sort}' if self.sort else 'GROUP BY 1'
        return (f'SELECT {self.label} AS "{self.column}", {PLACINGS} '
                f'{_FROM} {group} ORDER BY {self.order}')

    @property
    def starts(self) -> str:
        """The individual starts behind one bucket, newest first."""
        bucket = '' if self.label is None else f'AND {self.label} = ?'
        return (f'SELECT {START_COLUMNS} {_FROM} {bucket} '
                f'ORDER BY hs.meetDate DESC, hs.raceNumber DESC')


# crosscheck.REPORTS's shape, one entry per axis: the UI builds a labelled table
# per entry in a loop, so a seventh breakdown is one line here and no widget
# code. The shoe combinations involving X are kept as they are rather than
# folded into the majority or into one 'unknown' — the K/E/X codes stay verbatim
# through the whole pipeline, and X is 'not reported' rather than a third kind
# of shoeing, so folding it either invents shoeings or hides starts. specialCart
# is Heppa's americanSulkyKEX (K = yes, E = no, X = not reported) and gets the
# same treatment, glossed once in the UI legend.
BREAKDOWNS = (
    Axis('Overall'),
    Axis('Shoes (front / rear)', 'shoes',
         "coalesce(hs.frontShoes || ' / ' || hs.rearShoes, 'unknown')",
         'starts DESC, shoes'),
    Axis('Cart', 'cart', "coalesce(hs.specialCart, 'unknown')", 'starts DESC, cart'),
    Axis('Distance class', 'distance', f"coalesce({_CLASS}, 'unknown')",
         f'{_CLASS_ORDER}, distance', _CLASS_ORDER),
    Axis('Start type', 'start type', _AUTO, 'starts DESC, "start type"'),
    Axis('Days since previous start', 'days since previous', _LAYOFF,
         _LAYOFF_ORDER, _LAYOFF_ORDER),
)

SEARCH_LIMIT = 50


def fetch(conn, sql: str, params=()):
    """(column names, rows) — headers from the query, as crosscheck._show does.

    Keeping the names on the result rather than in the caller means the SQL is
    the only place a column is named.
    """
    rows = conn.execute(sql, list(params)).fetchall()
    return [d[0] for d in conn.description], rows


def search_horses(conn, term: str, limit: int = SEARCH_LIMIT):
    """Horses whose name or key contains `term`, most starts first.

    One row per horse — per `canonicalKey`, so the three ways Veikkaus spells
    `Humble Stance` are one hit. The last column is the key, for the caller to
    pass back to `breakdowns()`.
    """
    return fetch(conn, SQL_SEARCH, [term, term, limit])


def bucket_starts(conn, axis: Axis, canonical_key: str, label: str | None = None):
    """The individual starts behind one row of `axis.breakdown`.

    `label` is that row's bucket, and it is ignored for `Overall`, which has no
    label expression to compare it against.
    """
    params = [canonical_key] if axis.label is None else [canonical_key, label]
    return fetch(conn, axis.starts, params)


if __name__ == '__main__':
    import sys

    term = sys.argv[1] if len(sys.argv) > 1 else ''
    with db_read(DEFAULT_DB) as connection:
        names, hits = search_horses(connection, term)
        print(f'{len(hits)} hit(s) for {term!r}: ' + ', '.join(f'{r[0]} ({r[2]})' for r in hits[:5]))
        if hits:
            key = hits[0][-1]
            for axis in BREAKDOWNS:
                columns, rows = fetch(connection, axis.breakdown, [key])
                print(f'\n=== {key} — {axis.title} ===')
                print('  ' + '  '.join(str(c) for c in columns))
                for row in rows:
                    print('  ' + '  '.join('' if v is None else str(v) for v in row))
