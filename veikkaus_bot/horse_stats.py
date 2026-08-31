"""One horse's, one trainer's or one driver's registry starts, counted every way
the TUI shows them.

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
- **Race numbers 20 and above are not the ordinary programme**, and are counted
  nowhere here — 36,309 non-absent rows of qualifiers and pony racing, which
  would otherwise sit in every rate beside the races they are not.

Each breakdown is an `Axis`, and the label expression that names its buckets is
written once: the aggregate selects it, and the drill-down behind a clicked
bucket compares against it. See `Axis`.

**Whose starts are being counted is a `Subject`, and nothing else varies.** A
horse, a trainer and a driver are the same nine questions asked of the same
table, so a second module per subject would be a second definition of what
'<= 14 days' means, free to drift — the trap `crosscheck.py` avoids by building
every report on `archive_db.HEPPA_START_BRIDGE` rather than a hand-copied
lookalike. See `Subject`.

Run it directly to iterate on the SQL without starting Textual:

    uv run python -m veikkaus_bot.horse_stats 'com milton'
    uv run python -m veikkaus_bot.horse_stats 'koivunen' trainer
    uv run python -m veikkaus_bot.horse_stats 'raitala' driver
"""
from typing import NamedTuple

from .archive_db import DEFAULT_DB, db_read


# Absent rows are filtered in every query. coalesce(), not a bare NOT: `absent`
# is non-NULL on all 314,981 archive rows, but the test row builders leave
# unset columns NULL and `NOT NULL` is NULL — which would filter out every row
# the tests insert.
NOT_ABSENT = 'NOT coalesce(hs.absent, false)'

# Race numbers 20 and above are not the ordinary programme, and nothing in this
# module counts them. The archive holds two such bands and neither is a race the
# rest of a subject's record should be averaged with:
#
# - **21-25 are the qualifiers** (koelähtö) — 10,864 non-absent starts over
#   2,157 races, every one `eventType` YHDISTETTY, with **no prize money at all**
#   and no betting.
# - **31-42 are pony racing** — 25,445 starts over 3,836 A_PONIT/B_PONIT races,
#   with a purse (avg 114 € first prize) but again no betting.
#
# Against 262,183 non-absent starts at race numbers 1-14, avg 2,293 € first
# prize. **There are no race numbers between 15 and 20**, so this threshold sits
# in a gap in the data rather than on a boundary.
#
# The cost is real and was accepted rather than overlooked: 1,944 horses, 692
# trainers and 721 drivers have no starts left once these go. A trainer or a
# driver in that set drops out of search entirely, because neither person search
# has a zero-start arm; a horse still appears with 0 starts, because
# SQL_SEARCH_HORSES LEFT JOINs.
#
# No coalesce, unlike NOT_ABSENT: `raceNumber` is part of heppa_start's PRIMARY
# KEY, so a NULL is impossible by construction rather than merely absent today —
# the same argument _TRACK makes for `trackCode`.
REAL_RACE = 'hs.raceNumber < 20'

# What every query in this module counts: a horse that went to the gate, in a
# real race. Written once so the breakdown, the drill-down behind a clicked
# bucket and the search hit count cannot drift apart — the same reason `Axis`
# writes its label expression once.
COUNTED = f'{NOT_ABSENT} AND {REAL_RACE}'

# Quoted identifiers so '1st'/'2nd'/'3rd' survive as table headers.
#
# **`dq` sits with the placings and `gallop` sits alone behind them, and that
# ordering is the whole point of the layout.** A disqualification is a fourth
# outcome: `disqualifiedCode` is non-NULL on 36,513 of the 298,492 non-absent
# rows (hpl 16,696, hll 11,174, k 4,007, hlo 3,543, and six rarer codes), and
# **not one of them carries a placement** — the two are disjoint by data as well
# as by meaning. `gallop` is not: a horse can gallop and still win, 48,078 of
# the 51,730 galloping starts were placed at all and 2,832 of them first, and
# 3,765 rows are both a gallop and a disqualification. The TUI's blank spacer
# column means 'this one overlaps the placings', so it goes in front of `gallop`
# and nothing else — see `horse_tui._spaced`. sjoden appends its `dq` after
# `gallop` and spaces the last two; copying that here would say the opposite of
# what is true.
#
# `IS NOT NULL` rather than a nullif or a length test: zero non-absent rows hold
# an empty code, which is data rather than construction, but a code is a code.
# NULL on `gallop` only ever occurs on absent rows, which are filtered out
# anyway, and FILTER reads a NULL as false regardless.
PLACINGS = """count(*)                                                 AS starts,
           count(*) FILTER (WHERE hs.placement = 1)                AS "1st",
           count(*) FILTER (WHERE hs.placement = 2)                AS "2nd",
           count(*) FILTER (WHERE hs.placement = 3)                AS "3rd",
           count(*) FILTER (WHERE hs.disqualifiedCode IS NOT NULL) AS dq,
           count(*) FILTER (WHERE hs.gallop)                       AS gallop"""

# canonicalKey identifies, horseKey joins. The two agree on heppa_start today —
# RECOMPUTE_HEPPA_START_HORSEKEY and RECOMPUTE_HORSE_IDENTITY both write
# min(horseKey) over the same registry group — but grouping on horseKey would be
# one recompute away from answering for a fraction of a career.
_HORSE_FROM = f"""
    FROM archive.heppa_start hs
    JOIN archive.horse h ON h.horseKey = hs.horseKey
    WHERE h.canonicalKey = ? AND {COUNTED}
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
    WHERE hs.trainerId = ? AND {COUNTED}
"""

# A driver is identified by `driverId`, never by either name column. It is
# non-NULL on all 298,492 non-absent rows and carries 3,196 distinct ids, and
# **zero of them carry two different first+last names** — while 6 full names are
# shared by two ids, so grouping on the name would report one career for two.
# `driverName` is worse still: it is the short form ('S Raitala') and 129 ids
# carry more than one.
#
# No join, for the reason on _TRAINER_FROM: joining archive.horse would cost the
# 27,430 non-absent rows RECOMPUTE_HEPPA_START_HORSEKEY never reached — starts
# the driver really had — and `hs.horseName` is on the row.
_DRIVER_FROM = f"""
    FROM archive.heppa_start hs
    WHERE hs.driverId = ? AND {COUNTED}
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

# The post, prefixed by the start type, because the bare number is two different
# questions averaged together.
#
# Off an auto the field lines up abreast and the inside is a shorter trip; off a
# volt they turn into the race from a standing tier, where the inside is
# traffic. This archive says so plainly — win rate by post, non-absent starts:
# auto peaks at post 4-5 (13.0 %, 12.9 %), while volt peaks at post 1 (15.4 %)
# and falls monotonically to 8.4 % by post 4. A merged bucket 4 therefore
# reports a figure describing neither. Merging also hides the ranges: post 12
# has 6,479 auto starts against 386 volt ones.
#
# **startTrack = 0 is 'not reported', not a post**, and it collapses into one
# 'unknown' bucket rather than becoming 'auto 0'/'volt 0' — which would be two
# rows saying the same thing, and would sort ahead of post 1 as if it were
# further inside. It is 87 non-absent rows, every one of them finnishTrack =
# false: a programme abroad that does not name the post. The column is NULL on
# **zero** rows of the whole table, so the coalesce is construction rather than
# data — but `||` propagates a NULL, and a NULL label is a bucket that cannot be
# opened, so the guard fails toward a visible row. See `Axis`.
#
# cast to text so the label is a string like every other axis: `Axis.starts`
# compares it against a `?` bound from a table cell, which is text by the time
# it comes back out of the UI.
_POST = f"""CASE WHEN coalesce(hs.startTrack, 0) = 0 THEN 'unknown'
                     ELSE {_AUTO} || ' ' || cast(hs.startTrack AS varchar) END"""

# Grouped by start type, then numerically within it, or the labels sort 'auto
# 1', 'auto 10', 'auto 11', …, 'auto 2'. One key does both: the type's rank
# times a hundred plus the post, which is safe because the posts run 1-16 and no
# field reaches 100. The unknowns sort last rather than first, which a NULL
# would do in DuckDB's default ordering. Injective over the labels, so the
# ordering is total without a tiebreak.
_POST_ORDER = """CASE WHEN coalesce(hs.startTrack, 0) = 0 THEN 9999
                          ELSE (CASE hs.autoStart WHEN true THEN 0
                                                  WHEN false THEN 1
                                                  ELSE 2 END) * 100
                               + hs.startTrack END"""

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
# trainer or a driver.** startInterval is the *horse's* gap whoever trained or
# drove it, which is a fair question of both, but neither _TRAINER_FROM nor
# _DRIVER_FROM joins archive.horse, so their NULL bucket mixes each horse's
# earliest known start with the rows that have no identity at all, and it is no
# longer at most one row — 38,351 non-absent rows are NULL archive-wide. The
# string stays as it is because it is right for a horse and two tests pin it;
# the LEGEND carries the caveat that is true for all three.
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

# The registry's track code, not a name, and that is a measured decision rather
# than a shortcut. archive.heppa_start has no name column; archive.heppa_event
# has one, but a LEFT JOIN on (meetDate, trackCode) leaves 3,648 non-absent
# starts with a NULL name and resolves only 52 of the 99 codes — the local, pony
# and foreign meetings have no event row — and an inner join would drop those
# starts outright, which is the trap _TRAINER_FROM exists to avoid. That column
# is the organisation's name in any case ('Vermon Ravirata Oy'), not a track's.
# The code is also what the drill-down's `trk` column shows and what every other
# tool here speaks, so the bucket and the start list share one vocabulary.
#
# No coalesce: trackCode is part of heppa_start's PRIMARY KEY, so a NULL is
# impossible by construction rather than merely absent today.
_TRACK = 'hs.trackCode'

# The busiest tracks are not the ones a subject is *good* at, so this axis ranks
# by wins and falls back to starts. Ordering on the `"1st"` output alias rather
# than repeating the FILTER keeps one definition of what a win is, and DuckDB
# takes a quoted alias in ORDER BY.
#
# **The fallback is the normal case, not an edge case**, which is why it is in
# the title rather than in a comment: a subject wins at a median of 1 track (a
# horse or a trainer) or **0** (a driver), and 89.6 % of horses, 80.5 % of
# trainers and 84.0 % of drivers win at fewer than 5 tracks. So for most
# subjects most of this table is the starts fallback, and `starts DESC` is what
# fills it — every track with no win ties at 0 and is then ranked by how often
# the subject went there.
#
# `, track` breaks the remaining ties, so the cut is deterministic rather than
# whichever equal row DuckDB happened to return.
_TRACK_ORDER = '"1st" DESC, starts DESC, track'

# The full name, and not either of the two columns that look more like an
# identity.
#
# `driverName` is the short form ('S Raitala') and it is not reliable: 129 ids
# carry a second, *different* person's short name on a handful of rows each, so
# Santtu Raitala's 9,256 starts include one reading 'V Stenman'. Grouping on it
# would split a driver's row and could cost them a place in the top three below.
# `driverId` is clean — zero ids carry more than one first+last name — but it is
# 19 digits, and a bucket label is read.
#
# So the label is the full name: non-NULL and never partial on any of the
# 298,492 non-absent rows, which is data rather than construction, so no
# coalesce is needed. 3,190 distinct values against 3,196 ids — 6 names belong
# to two people and merge into one bucket. The drill-down merges identically, so
# the count a bucket shows is still the count it opens.
_DRIVER = "hs.driverFirstName || ' ' || hs.driverLastName"

# The trainer's name, for the counterpart axis a driver gets. `trainerName` is
# already a full name, unlike `driverName`, and is non-NULL on every row; 4,656
# ids carry 4,620 distinct names on the non-absent rows, so 36 names belong to
# two people and merge into one bucket — which the drill-down merges
# identically, so the count a bucket shows is still the count it opens. The
# 19-digit id is the identity a *subject* is keyed on; a bucket label is read.
_TRAINER = 'hs.trainerName'

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
#
# An f-string, alone among the three searches' literals, so that `starts`
# counts what the panels count: writing the filter out inline here would be a
# second definition of which rows are a start, free to drift from COUNTED. The
# query holds no braces of its own, so nothing needs escaping.
SQL_SEARCH_HORSES = f"""
    WITH matched AS (
        SELECT DISTINCT canonicalKey
        FROM archive.horse
        WHERE lower(replace(horseName, '*', '')) LIKE '%' || lower(replace(?, '*', '')) || '%'
           OR lower(canonicalKey) LIKE '%' || lower(?) || '%'
    )
    SELECT any_value(h.horseName) FILTER (WHERE h.horseKey = h.canonicalKey) AS horse,
           any_value(h.birthYear) FILTER (WHERE h.horseKey = h.canonicalKey) AS born,
           count(*) FILTER (WHERE hs.meetDate IS NOT NULL AND {COUNTED})    AS starts,
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
# that exact id, and it keeps the parameters (term, term, limit) for every
# subject, so `search()` needs to know nothing about which one it is running.
SQL_SEARCH_TRAINERS = f"""
    SELECT any_value(hs.trainerName)                        AS trainer,
           count(*)                                         AS starts,
           count(DISTINCT hs.horseId)                       AS horses,
           substr(min(hs.meetDate), 1, 4) || '-'
             || substr(max(hs.meetDate), 1, 4)              AS years,
           hs.trainerId                                     AS trainerId
    FROM archive.heppa_start hs
    WHERE (lower(hs.trainerName) LIKE '%' || lower(?) || '%' OR hs.trainerId = ?)
      AND {COUNTED}
    GROUP BY hs.trainerId
    ORDER BY starts DESC, trainer
    LIMIT ?
"""

# The driver search, and it is SQL_SEARCH_TRAINERS's shape line for line: one
# stage (one driverId is one driver, so there is nothing to canonicalise), no
# LEFT JOIN arm (a driver exists only as a start row and cannot be 'found it,
# nothing here'), the same `horses`/`years` disambiguators, the same `= ?` on a
# 19-digit id, and the same (term, term, limit) parameters, so `search()` still
# needs to know nothing about which subject it is running.
#
# **The parentheses around the OR are load-bearing**, as they are there: without
# them the absent filter binds to the id arm alone and a pasted id lists
# withdrawn entries.
#
# The one difference is which name it matches and displays. `hs.driverName` is
# the short form and 129 ids carry more than one of them, so a term typed as a
# surname would miss and a displayed hit would be unstable; the search runs on
# the concatenated full name, which is what the driver axis already labels its
# buckets with. Matching the short form *as well* would need a third `?` and
# would break the parameter contract above.
SQL_SEARCH_DRIVERS = f"""
    SELECT any_value({_DRIVER})                             AS driver,
           count(*)                                         AS starts,
           count(DISTINCT hs.horseId)                       AS horses,
           substr(min(hs.meetDate), 1, 4) || '-'
             || substr(max(hs.meetDate), 1, 4)              AS years,
           hs.driverId                                      AS driverId
    FROM archive.heppa_start hs
    WHERE (lower({_DRIVER}) LIKE '%' || lower(?) || '%' OR hs.driverId = ?)
      AND {COUNTED}
    GROUP BY hs.driverId
    ORDER BY starts DESC, driver
    LIMIT ?
"""

# The individual starts behind a bucket. Compact on purpose — the identity of
# the start, the conditions it ran under, how it went, and who drove.
#
# The placing column coalesces, because `placement` is NULL on 12.3 % of
# non-absent starts and an empty cell would read as data we do not have. A
# disqualification shows its code, and a start that finished outside the
# placings shows '-'. Neither is a missing value.
#
# `lane` is `startTrack`, which archive_db names to match archive.start; it is
# the post, 1-16, and never NULL — but 0 where a programme abroad did not report
# it, which is what the post-position axis buckets as 'unknown'. `odds` is
# `winOdd` scaled out of hundredths, and NULL on the 44,995 non-absent rows of
# local and pony meetings where there was no betting — blank there is the truth.
# nullif(0) because 1.00 is the floor of a win odd, so the 64 rows storing 0 are
# 'not reported' rather than a price of nothing. `prize` is `prizeWon`, this
# race's money for this horse, in euros and never NULL: 0 is what an unplaced
# start won, which is a fact rather than a gap, and 128,262 rows say it.
#
# printf rather than round(), which returns a double and so prints 2.6 for a
# price of 2.60. printf keeps a NULL NULL, so the no-betting rows stay blank.
START_COLUMNS = """hs.meetDate AS date, hs.trackCode AS trk, hs.raceNumber AS race,
           hs.distance AS dist, hs.startTrack AS lane,
           coalesce(cast(hs.placement AS varchar), hs.disqualifiedCode, '-') AS plc,
           hs.kmTime AS "km time",
           printf('%.2f', nullif(hs.winOdd, 0) / 100.0) AS odds,
           hs.prizeWon AS prize, hs.driverName AS driver"""

# The horse column the horse view has no use for: a trainer's or a driver's
# start list is 'which horse ran', so it goes first and the ten shared columns
# follow unchanged. hs.horseName rather than a join, for the reason on
# _TRAINER_FROM.
_WITH_HORSE = f'hs.horseName AS horse, {START_COLUMNS}'


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
    concatenation, where `||` propagates a NULL, around `specialCart`, and
    around `startTrack` in the post label. All three are NULL on zero archive
    rows today, which is data and not construction.

    `Overall` has no label. It groups nothing and filters nothing, so clicking
    it lists every start the subject has.

    `limit` caps the **breakdown** and nothing else. `starts` stays uncapped, so
    a bucket that is on screen still opens exactly the count it shows; capping
    both would break the one invariant this class exists to hold. A capped axis
    therefore does not sum back to the overall start count, which is why the two
    that have a limit say so in their titles.

    It is defined above `Subject` because `Subject.partner` is annotated `Axis`
    and a NamedTuple evaluates its annotations when the class body runs. The
    methods here take the forward reference `'Subject'` for the mirror image of
    the same reason.
    """

    title: str
    column: str | None = None
    label: str | None = None
    order: str = 'starts DESC'
    sort: str | None = None      # a numeric sort key, where the label sorts wrong
    limit: int | None = None     # keep only the busiest N buckets

    def breakdown(self, subject: 'Subject') -> str:
        """Starts, placings, disqualifications and gallops per bucket."""
        if self.label is None:
            return f'SELECT {PLACINGS} {subject.frm}'
        group = f'GROUP BY 1, {self.sort}' if self.sort else 'GROUP BY 1'
        cap = f' LIMIT {self.limit}' if self.limit else ''
        return (f'SELECT {self.label} AS "{self.column}", {PLACINGS} '
                f'{subject.frm} {group} ORDER BY {self.order}{cap}')

    def starts(self, subject: 'Subject') -> str:
        """The individual starts behind one bucket, newest first.

        programNumber breaks the ties, because a trainer or a driver can have
        two horses in one race and a two-column order over (date, race) is no
        longer total. A no-op for a horse, which cannot be in one race twice.
        """
        bucket = '' if self.label is None else f'AND {self.label} = ?'
        return (f'SELECT {subject.columns} {subject.frm} {bucket} '
                f'ORDER BY hs.meetDate DESC, hs.raceNumber DESC, hs.programNumber')


# The counterpart-role axis, the one breakdown that depends on who is being
# counted: a horse and a trainer want to know which drivers, and a driver wants
# to know which trainers — asking a driver which drivers drove it returns one
# row equal to Overall.
#
# **There is one per subject rather than one per role**, because the axis is a
# per-`Subject` field and nothing forces the three to agree. They happen to all
# be 5 today, which is a decision rather than a coincidence: a top 3 cut a
# majority of every subject. 56.1 % of horses have more than 3 drivers and
# 38.6 % more than 5 (median 4, mean 5.5, max 35), and the coverage says the
# same — for a horse 3 drivers cover 79.5 % of the starts and 5 cover 88.8 %;
# for a trainer 75.1 % and 83.9 % (mean 7.9 drivers per trainer, max 131); for a
# driver 3 trainers cover only 52.5 % and 5 cover 57.9 %, because a driver has a
# median of 2 trainers but a maximum of 922 and the busy drivers carry the rows.
#
# Those horse figures are grouped on `canonicalKey`, which is what `_HORSE_FROM`
# joins for — grouping on `horseKey` reads 72.5 %/81.2 % instead, because the
# 182 horses split across 365 keys are then counted as separate, shorter
# careers. Measure this the way the axis runs, not the way the table stores.
#
# The cap is **named in the title** because these are the breakdowns that do not
# sum back to Overall, and a cap that did not say so would read as a complete
# table. The `, name` tiebreak makes the cut deterministic rather than whichever
# equal row DuckDB happened to return — 970 trainers have a starts tie
# straddling the third and fourth driver.
def _partner(role: str, label: str, limit: int) -> Axis:
    """One subject's counterpart-role axis, capped and titled with its cap."""
    return Axis(f'{role.capitalize()} (top {limit} by starts)', role, label,
                f'starts DESC, {role}', limit=limit)


HORSE_DRIVERS = _partner('driver', _DRIVER, 5)
TRAINER_DRIVERS = _partner('driver', _DRIVER, 5)
DRIVER_TRAINERS = _partner('trainer', _TRAINER, 5)

# crosscheck.REPORTS's shape, one entry per axis, in the order the UI stacks
# them: the equipment, then the race's shape, then the timeline, then where, and
# the people last. The UI builds a labelled table per entry in a loop, so a
# tenth breakdown is one line here and no widget code.
#
# The shoe combinations involving X are kept as they are rather than folded into
# the majority or into one 'unknown' — the K/E/X codes stay verbatim through the
# whole pipeline, and X is 'not reported' rather than a third kind of shoeing,
# so folding it either invents shoeings or hides starts. specialCart is Heppa's
# americanSulkyKEX (K = yes, E = no, X = not reported) and gets the same
# treatment, glossed once in the UI legend.
#
# Post position sits directly after the start type it is namespaced by. Track is
# the second capped axis, and the cap is in its title for the same reason the
# counterpart role's is: 99 codes archive-wide, a median of 7 tracks per horse
# (max 30), 8 per trainer (max 47) and 5 per driver (max 46), with the top 5
# covering 78.7 % / 75.8 % / 68.9 % of the starts respectively. The two tables
# that do not sum back therefore sit together at the foot of the pane, where a
# reader has already seen the complete ones.
COMMON = (
    Axis('Overall'),
    Axis('Shoes (front / rear)', 'shoes',
         "coalesce(hs.frontShoes || ' / ' || hs.rearShoes, 'unknown')",
         'starts DESC, shoes'),
    Axis('Cart', 'cart', "coalesce(hs.specialCart, 'unknown')", 'starts DESC, cart'),
    Axis('Distance class', 'distance', f"coalesce({_CLASS}, 'unknown')",
         f'{_CLASS_ORDER}, distance', _CLASS_ORDER),
    Axis('Start type', 'start type', _AUTO, 'starts DESC, "start type"'),
    Axis('Post position (per start type)', 'post', _POST, _POST_ORDER, _POST_ORDER),
    Axis('Days since previous start', 'days since previous', _LAYOFF,
         _LAYOFF_ORDER, _LAYOFF_ORDER),
    Axis('Track (top 5 by wins, then starts)', 'track', _TRACK, _TRACK_ORDER, limit=5),
)

# Constant across subjects, which is what lets the UI build its panels once and
# never rebuild them when the subject changes. See `breakdowns`.
AXIS_COUNT = len(COMMON) + 1


class Subject(NamedTuple):
    """Whose starts a breakdown counts — a horse, a trainer or a driver.

    `COMMON` says how to group a set of `heppa_start` rows; a Subject says
    *which* rows, what a drill-down lists behind one bucket, and which
    counterpart role its last axis asks about. Nothing else differs: the label
    expressions, PLACINGS, the orderings and the absent filter are the same
    questions asked of the same table, which is the whole reason there is no
    `trainer_stats.py` and no `driver_stats.py`.

    `frm` is the contract, and it is three things at once. The table is aliased
    `hs`, because every shared expression above says `hs.`. It takes exactly one
    `?`, the identity, so `bucket_starts` can build the parameters without
    knowing the subject. And it *ends* in a WHERE, so `Axis.starts` can hang
    `AND <label> = ?` off it.

    `partner` is the counterpart-role axis, and it is what makes the third
    subject cost one line instead of a widget rewrite: every subject answers
    `AXIS_COUNT` axes, so the UI retitles a panel rather than rebuilding a tree.
    """

    name: str        # the noun the UI puts in its prompt and its messages
    frm: str         # FROM … WHERE <identity> = ? AND NOT absent
    columns: str     # the drill-down's SELECT list
    search: str      # (display…, identity last), given (term, term, limit)
    partner: Axis    # the counterpart role its last axis asks about


HORSE = Subject('horse', _HORSE_FROM, START_COLUMNS, SQL_SEARCH_HORSES,
                HORSE_DRIVERS)
TRAINER = Subject('trainer', _TRAINER_FROM, _WITH_HORSE, SQL_SEARCH_TRAINERS,
                  TRAINER_DRIVERS)
DRIVER = Subject('driver', _DRIVER_FROM, _WITH_HORSE, SQL_SEARCH_DRIVERS,
                 DRIVER_TRAINERS)

# The order `t` cycles through in the UI.
SUBJECTS = (HORSE, TRAINER, DRIVER)

SEARCH_LIMIT = 50


def breakdowns(subject: Subject) -> tuple:
    """The axes for one subject: the shared eight, then its counterpart role.

    Always `AXIS_COUNT` long, whichever subject it is, so the UI's panels are
    built once and only ever retitled.
    """
    return COMMON + (subject.partner,)


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
    Veikkaus spells `Humble Stance` are one hit; per `trainerId` or `driverId`
    for a person. The last column is that identity, for the caller to pass back
    to `Axis.breakdown`; the first is what to display for it.
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
    role = sys.argv[2][0].lower() if len(sys.argv) > 2 else 'h'
    who = {'t': TRAINER, 'd': DRIVER}.get(role, HORSE)
    with db_read(DEFAULT_DB) as connection:
        names, hits = search(connection, who, term)
        count = names.index('starts')
        print(f'{len(hits)} {who.name}(s) for {term!r}: '
              + ', '.join(f'{r[0]} ({r[count]})' for r in hits[:5]))
        if hits:
            key = hits[0][-1]
            for axis in breakdowns(who):
                columns, rows = fetch(connection, axis.breakdown(who), [key])
                print(f'\n=== {key} — {axis.title} ===')
                print('  ' + '  '.join(str(c) for c in columns))
                for row in rows:
                    print('  ' + '  '.join('' if v is None else str(v) for v in row))
