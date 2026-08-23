"""One horse's — or one trainer's — registry starts, counted every way the TUI
shows them.

The query layer behind `veikkaus stats`. Read-only, and deliberately free of
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

**Whose starts are being counted is a `Subject`, and nothing else varies.** A
horse and a trainer are the same seven questions asked of the same table, so a
second module would be a second definition of what '<= 14 days' means, free to
drift — the trap `crosscheck.py` avoids by building every report on
`archive_db.HEPPA_START_BRIDGE` rather than a hand-copied lookalike. See
`Subject`.

Run it directly to iterate on the SQL without starting Textual:

    uv run python -m veikkaus_bot.horse_stats 'com milton'
    uv run python -m veikkaus_bot.horse_stats 'koivunen' trainer
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
_HORSE_FROM = f"""
    FROM archive.heppa_start hs
    JOIN archive.horse h ON h.horseKey = hs.horseKey
    WHERE h.canonicalKey = ? AND {NOT_ABSENT}
"""

# A trainer is identified by `trainerId`, never by the name. Both are non-NULL
# on all 314,981 rows and every id maps to exactly one name, but 4,665 ids carry
# only 4,630 distinct names: 35 names belong to two different people, and
# grouping on the name would report one career for two.
#
# **The absence of a join is the load-bearing part.** Joining archive.horse the
# way the horse subject must would cost the 27,430 non-absent rows
# RECOMPUTE_HEPPA_START_HORSEKEY never reached — 9 % of them, starts the trainer
# really had — and nothing here needs that table: `hs.horseName` is on the row.
_TRAINER_FROM = f"""
    FROM archive.heppa_start hs
    WHERE hs.trainerId = ? AND {NOT_ABSENT}
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
SQL_SEARCH_HORSES = """
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

# The trainer search, and every way it differs from the horse search above is
# deliberate.
#
# One stage, not two: there is no canonicalisation to resolve, because one
# trainerId is one trainer — the horse's 365 keys for 182 animals have no
# counterpart here. No replace(name, '*', ''), which is a horse-name quirk. And
# no LEFT JOIN arm: a horse exists in archive.horse whether or not it ever
# started, so 'found it, nothing here' is a state that search has to render,
# while a trainer exists only as a start row and cannot be in it.
#
# `horses` and `years` are not decoration — they are the disambiguator the 35
# shared names force, because the identity is 19 digits and is not something to
# read. count(DISTINCT horseId) rather than horseName: the registry id is stable
# where a name is a string, and it needs no join.
#
# **The parentheses around the OR are load-bearing.** Without them the absent
# filter binds to the id arm alone, and a pasted id lists withdrawn entries.
#
# `trainerId = ?` rather than a LIKE on a 19-digit number: pasting an id means
# that exact id, and it keeps the parameters (term, term, limit) for both
# subjects, so `search()` needs to know nothing about which one it is running.
SQL_SEARCH_TRAINERS = f"""
    SELECT any_value(hs.trainerName)                        AS trainer,
           count(*)                                         AS starts,
           count(DISTINCT hs.horseId)                       AS horses,
           substr(min(hs.meetDate), 1, 4) || '-'
             || substr(max(hs.meetDate), 1, 4)              AS years,
           hs.trainerId                                     AS trainerId
    FROM archive.heppa_start hs
    WHERE (lower(hs.trainerName) LIKE '%' || lower(?) || '%' OR hs.trainerId = ?)
      AND {NOT_ABSENT}
    GROUP BY hs.trainerId
    ORDER BY starts DESC, trainer
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
# NULL is its own bucket, and *for a horse* it means exactly one thing: `absent`
# is filtered above, and the 27,430 rows RECOMPUTE_HEPPA_START_HORSEKEY never
# reached have no horseKey to join on at all. What is left is a start with no
# known predecessor across the three start-bearing tables — at most one row per
# horse. Never `startInterval > 10000` here: that sentinel is prev_start's, and
# heppa_start says unknown with NULL.
#
# **That collapse is conditional on the subject, and the label over-claims for a
# trainer.** startInterval is the *horse's* gap whoever trained it, which is a
# fair trainer question, but _TRAINER_FROM does not join archive.horse, so a
# trainer's NULL bucket mixes each of its horses' earliest known starts with the
# rows that have no identity at all, and it is no longer at most one row —
# 38,351 non-absent rows are NULL archive-wide. The string stays as it is
# because it is right for a horse and two tests pin it; the LEGEND carries the
# caveat that is true for both.
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

# The full name, and not either of the two columns that look more like an
# identity.
#
# `driverName` is the short form ('S Raitala') and it is not reliable: 113 ids
# carry a second, *different* person's short name on a handful of rows each, so
# Santtu Raitala's 9,212 starts include one reading 'V Stenman'. Grouping on it
# would split a driver's row and could cost them a place in the top three below.
# `driverId` is clean — zero ids carry more than one first+last name — but it is
# 19 digits, and a bucket label is read.
#
# So the label is the full name: non-NULL and never partial on any of the
# 293,843 non-absent rows, which is data rather than construction, so no
# coalesce is needed. 3,138 distinct values against 3,143 ids — 5 names belong
# to two people (557 starts, 0.19 %) and merge into one bucket. The drill-down
# merges identically, so the count a bucket shows is still the count it opens.
_DRIVER = "hs.driverFirstName || ' ' || hs.driverLastName"

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


class Subject(NamedTuple):
    """Whose starts a breakdown counts — a horse, or a trainer.

    The `BREAKDOWNS` below say how to group a set of `heppa_start` rows; a
    Subject says *which* rows, and what a drill-down lists behind one bucket.
    Nothing else differs: the label expressions, PLACINGS, the orderings and the
    absent filter are the same questions asked of the same table.

    `frm` is the contract, and it is three things at once. The table is aliased
    `hs`, because every shared expression above says `hs.`. It takes exactly one
    `?`, the identity, so `bucket_starts` can build the parameters without
    knowing the subject. And it *ends* in a WHERE, so `Axis.starts` can hang
    `AND <label> = ?` off it.
    """

    name: str        # the noun the UI puts in its prompt and its messages
    frm: str         # FROM … WHERE <identity> = ? AND NOT absent
    columns: str     # the drill-down's SELECT list
    search: str      # (display…, identity last), given (term, term, limit)


HORSE = Subject('horse', _HORSE_FROM, START_COLUMNS, SQL_SEARCH_HORSES)

# The horse column the horse view has no use for: a trainer's start list is
# 'which of mine ran', so it goes first and the ten shared columns follow
# unchanged. hs.horseName rather than a join, for the reason on _TRAINER_FROM.
TRAINER = Subject('trainer', _TRAINER_FROM,
                  f'hs.horseName AS horse, {START_COLUMNS}', SQL_SEARCH_TRAINERS)


class Axis(NamedTuple):
    """One way of grouping a subject's starts, and both queries about it.

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
    it lists every start the subject has.

    `limit` caps the **breakdown** and nothing else. `starts` stays uncapped, so
    a bucket that is on screen still opens exactly the count it shows; capping
    both would break the one invariant this class exists to hold. A capped axis
    therefore does not sum back to the overall start count, which is why the
    only one that has a limit says so in its title.
    """

    title: str
    column: str | None = None
    label: str | None = None
    order: str = 'starts DESC'
    sort: str | None = None      # a numeric sort key, where the label sorts wrong
    limit: int | None = None     # keep only the busiest N buckets

    def breakdown(self, subject: Subject) -> str:
        """Starts, placings and gallops per bucket."""
        if self.label is None:
            return f'SELECT {PLACINGS} {subject.frm}'
        group = f'GROUP BY 1, {self.sort}' if self.sort else 'GROUP BY 1'
        cap = f' LIMIT {self.limit}' if self.limit else ''
        return (f'SELECT {self.label} AS "{self.column}", {PLACINGS} '
                f'{subject.frm} {group} ORDER BY {self.order}{cap}')

    def starts(self, subject: Subject) -> str:
        """The individual starts behind one bucket, newest first.

        programNumber breaks the ties, because a trainer can have two horses in
        one race and a two-column order over (date, race) is no longer total. A
        no-op for a horse, which cannot be in one race twice.
        """
        bucket = '' if self.label is None else f'AND {self.label} = ?'
        return (f'SELECT {subject.columns} {subject.frm} {bucket} '
                f'ORDER BY hs.meetDate DESC, hs.raceNumber DESC, hs.programNumber')


# crosscheck.REPORTS's shape, one entry per axis: the UI builds a labelled table
# per entry in a loop, so an eighth breakdown is one line here and no widget
# code. The shoe combinations involving X are kept as they are rather than
# folded into the majority or into one 'unknown' — the K/E/X codes stay verbatim
# through the whole pipeline, and X is 'not reported' rather than a third kind
# of shoeing, so folding it either invents shoeings or hides starts. specialCart
# is Heppa's americanSulkyKEX (K = yes, E = no, X = not reported) and gets the
# same treatment, glossed once in the UI legend.
#
# Driver is the one capped axis, and the cap is **named in the title** because
# it is the one breakdown that does not sum back to Overall: the top three cover
# 79.6 % of a horse's starts and 75.2 % of a trainer's (median 4 drivers per
# horse, mean 7.9 per trainer, max 131). A cap that did not say so would read as
# a complete table. The `, driver` tiebreak is what makes the cut deterministic
# rather than whichever equal row DuckDB happened to return — 970 trainers have
# a starts tie straddling the third and fourth driver.
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
    Axis('Driver (top 3 by starts)', 'driver', _DRIVER, 'starts DESC, driver',
         limit=3),
)

SEARCH_LIMIT = 50


def fetch(conn, sql: str, params=()):
    """(column names, rows) — headers from the query, as crosscheck._show does.

    Keeping the names on the result rather than in the caller means the SQL is
    the only place a column is named.
    """
    rows = conn.execute(sql, list(params)).fetchall()
    return [d[0] for d in conn.description], rows


def search(conn, subject: Subject, term: str, limit: int = SEARCH_LIMIT):
    """Subjects whose name or identity contains `term`, most starts first.

    One row per identity — per `canonicalKey` for a horse, so the three ways
    Veikkaus spells `Humble Stance` are one hit; per `trainerId` for a trainer.
    The last column is that identity, for the caller to pass back to
    `Axis.breakdown`; the first is what to display for it.
    """
    return fetch(conn, subject.search, [term, term, limit])


def bucket_starts(conn, subject: Subject, axis: Axis, key: str, label: str | None = None):
    """The individual starts behind one row of `axis.breakdown(subject)`.

    `label` is that row's bucket, and it is ignored for `Overall`, which has no
    label expression to compare it against.
    """
    params = [key] if axis.label is None else [key, label]
    return fetch(conn, axis.starts(subject), params)


if __name__ == '__main__':
    import sys

    term = sys.argv[1] if len(sys.argv) > 1 else ''
    who = TRAINER if len(sys.argv) > 2 and sys.argv[2].startswith('t') else HORSE
    with db_read(DEFAULT_DB) as connection:
        names, hits = search(connection, who, term)
        count = names.index('starts')
        print(f'{len(hits)} {who.name}(s) for {term!r}: '
              + ', '.join(f'{r[0]} ({r[count]})' for r in hits[:5]))
        if hits:
            key = hits[0][-1]
            for axis in BREAKDOWNS:
                columns, rows = fetch(connection, axis.breakdown(who), [key])
                print(f'\n=== {key} — {axis.title} ===')
                print('  ' + '  '.join(str(c) for c in columns))
                for row in rows:
                    print('  ' + '  '.join('' if v is None else str(v) for v in row))
