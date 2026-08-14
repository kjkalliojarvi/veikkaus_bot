"""Cross-validate the two sources against each other (strategy §8/§8b).

§8 asked for ~20 races to be spot-checked by hand against Heppa. Now that the
registry is crawled rather than browsed, the same check is a query over every
start where both sources have an answer — and it runs over the same bridge the
merge itself uses (`archive_db.HEPPA_START_BRIDGE`), so a join that drifts
breaks the validation rather than quietly passing it.

Read-only. Run it after `parse`:

    uv run veikkaus crosscheck

Two disagreements are expected and are *not* faults:

- **Horse names differ** wherever a horse is an import. Veikkaus appends the
  country tag (`Kapplans Orlando (SE)`); Heppa keeps it in
  `horseRegistrationCountry` and leaves the name clean. This is precisely why
  the bridge is positional and never name-based, so the name column is a
  diagnostic here, not a key.
- **Km-time strings differ** wherever the start type or equipment left a marker.
  Veikkaus writes `31,5x` and `m18,5a`; Heppa's `shortKilometerTime` is bare.
  The parsed `kmTimeMs` is what has to agree, and that is what report 1 checks.

Anything else that differs is worth reading before trusting the merged column.
"""
from .archive_db import DEFAULT_DB, HEPPA_START_BRIDGE, db_ops


# Real Finnish meetings only. The Swedish simulcast cards (trackAbbreviation
# ending '-V', trackNumber 57/87) and the Veikkaus combination-pool meta-cards
# (MM, KUN, CIT, T75, Sl, JAA, trackNumber 48/88) have no Heppa counterpart by
# design, so counting them as uncovered would be misleading.
REAL_MEETINGS = "ca.country = 'FI' AND ca.trackNumber NOT IN (48, 57, 87, 88)"

REPORTS = (
    ('Overlap: do the two sources agree where both have an answer?', f"""
        SELECT count(*) AS compared,
               sum(CASE WHEN s.placement = h.placement THEN 1 ELSE 0 END) AS placement_agree,
               sum(CASE WHEN s.placement <> h.placement THEN 1 ELSE 0 END) AS placement_differ,
               sum(CASE WHEN s.kmTimeMs IS NOT NULL AND h.kmTimeMs IS NOT NULL
                         AND s.kmTimeMs <> h.kmTimeMs THEN 1 ELSE 0 END) AS kmtime_differ,
               sum(CASE WHEN s.autoStart IS NOT NULL AND h.autoStart IS NOT NULL
                         AND s.autoStart <> h.autoStart THEN 1 ELSE 0 END) AS autostart_differ,
               sum(CASE WHEN s.winOddsFinal IS NOT NULL AND h.winOdd IS NOT NULL
                         AND s.winOddsFinal <> h.winOdd THEN 1 ELSE 0 END) AS winodds_differ
        {HEPPA_START_BRIDGE}
        WHERE s.resultSource = 'veikkaus' AND h.placement IS NOT NULL
    """),

    ('Placings that actually disagree', f"""
        SELECT h.meetDate, h.trackCode, h.raceNumber, h.programNumber,
               s.horseName, s.placement AS veikkaus, h.placement AS heppa
        {HEPPA_START_BRIDGE}
        WHERE s.resultSource = 'veikkaus' AND h.placement IS NOT NULL
          AND s.placement <> h.placement
        LIMIT 20
    """),

    ('Scratchings: Veikkaus `scratched` against Heppa `absent`', f"""
        SELECT count(*) AS compared,
               sum(CASE WHEN coalesce(s.scratched, false) <> coalesce(h.absent, false)
                        THEN 1 ELSE 0 END) AS differ
        {HEPPA_START_BRIDGE}
    """),

    # 142000 cents (pre-race) + 700 EUR won = 2120 EUR (post-race). A
    # corroboration of what the two earnings figures mean, not an invariant:
    # it only holds where careerWinnings was current as of that day.
    ('Earnings: careerWinnings (cents, pre-race) + price = horsePriceSum', f"""
        SELECT count(*) AS compared,
               sum(CASE WHEN s.careerWinnings + h.prizeWon * 100 = h.horsePriceSum * 100
                        THEN 1 ELSE 0 END) AS reconciles
        {HEPPA_START_BRIDGE}
        WHERE s.careerWinnings IS NOT NULL AND h.prizeWon IS NOT NULL
          AND h.horsePriceSum IS NOT NULL
    """),

    ('Is the positional bridge landing on the same horse? (imports differ, see above)', f"""
        SELECT count(*) AS joined,
               sum(CASE WHEN lower(replace(s.horseName, '*', ''))
                           <> lower(replace(h.horseName, '*', '')) THEN 1 ELSE 0 END)
                   AS name_differ,
               sum(CASE WHEN lower(replace(s.horseName, '*', ''))
                           <> lower(replace(h.horseName, '*', ''))
                        AND position('(' IN s.horseName) = 0 THEN 1 ELSE 0 END)
                   AS differ_without_a_country_tag
        {HEPPA_START_BRIDGE}
        WHERE s.horseName IS NOT NULL AND h.horseName IS NOT NULL
    """),

    ('Name mismatches that a country tag does not explain', f"""
        SELECT h.meetDate, h.trackCode, h.raceNumber, h.programNumber,
               s.horseName AS veikkaus, h.horseName AS heppa
        {HEPPA_START_BRIDGE}
        WHERE lower(replace(s.horseName, '*', '')) <> lower(replace(h.horseName, '*', ''))
          AND position('(' IN s.horseName) = 0
        LIMIT 20
    """),

    ('Coverage: where each placing came from, by year', f"""
        SELECT substr(ca.meetDate, 1, 4) AS year,
               count(*) AS starts,
               sum(CASE WHEN s.resultSource = 'veikkaus' THEN 1 ELSE 0 END) AS from_veikkaus,
               sum(CASE WHEN s.resultSource = 'heppa' THEN 1 ELSE 0 END) AS from_heppa,
               sum(CASE WHEN s.placement IS NULL AND NOT coalesce(s.scratched, false)
                        THEN 1 ELSE 0 END) AS still_unplaced
        FROM archive.start s
        JOIN archive.race r ON r.raceId = s.raceId
        JOIN archive.card ca ON ca.cardId = r.cardId
        WHERE {REAL_MEETINGS}
        GROUP BY 1 ORDER BY 1
    """),

    ('What is left unplaced, by outcome code', """
        SELECT coalesce(disqualifiedCode, '(none — not yet covered by a Heppa crawl)') AS code,
               count(*) AS starts
        FROM archive.start
        WHERE placement IS NULL AND NOT coalesce(scratched, false)
        GROUP BY 1 ORDER BY 2 DESC LIMIT 15
    """),

    # `horse_key()` is a name-and-birth-year guess and horseId is authoritative,
    # so either direction disagreeing is a real identity bug worth reading.
    ('Identity: one horseKey resolving to several registry ids', f"""
        SELECT s.horseKey, count(DISTINCT h.horseId) AS ids,
               string_agg(DISTINCT h.horseId, ' / ') AS which
        {HEPPA_START_BRIDGE}
        WHERE h.horseId IS NOT NULL
        GROUP BY 1 HAVING count(DISTINCT h.horseId) > 1
        ORDER BY 2 DESC LIMIT 25
    """),

    ('Identity: one registry id resolving to several horseKeys', f"""
        SELECT h.horseId, count(DISTINCT s.horseKey) AS keys,
               string_agg(DISTINCT s.horseKey, ' / ') AS which
        {HEPPA_START_BRIDGE}
        WHERE h.horseId IS NOT NULL
        GROUP BY 1 HAVING count(DISTINCT s.horseKey) > 1
        ORDER BY 2 DESC LIMIT 25
    """),

    ('Meetings only Heppa has, and whether their horses resolved', """
        SELECT coalesce(e.eventType, '(unknown)') AS eventType,
               count(DISTINCT h.meetDate || h.trackCode) AS meetings,
               count(*) AS starts,
               sum(CASE WHEN h.horseKey IS NOT NULL THEN 1 ELSE 0 END) AS horse_resolved
        FROM archive.heppa_start h
        LEFT JOIN archive.heppa_event e
               ON e.meetDate = h.meetDate AND e.trackCode = h.trackCode
        LEFT JOIN archive.card ca ON ca.meetDate = h.meetDate
                                 AND upper(ca.trackAbbreviation) = h.trackCode
        WHERE ca.cardId IS NULL
        GROUP BY 1 ORDER BY 3 DESC
    """),

    ('Meetings the Veikkaus crawl has but the Heppa crawl has not reached', f"""
        SELECT substr(ca.meetDate, 1, 4) AS year, count(*) AS cards
        FROM archive.card ca
        LEFT JOIN archive.heppa_event e
               ON e.meetDate = ca.meetDate
              AND e.trackCode = upper(ca.trackAbbreviation)
        WHERE {REAL_MEETINGS} AND e.meetDate IS NULL
        GROUP BY 1 ORDER BY 1
    """),
)


def _show(conn, title: str, sql: str):
    print(f'\n=== {title} ===')
    rows = conn.execute(sql).fetchall()
    if not rows:
        print('  (none)')
        return
    names = [d[0] for d in conn.description]
    widths = [max(len(n), *(len(_cell(r[i])) for r in rows)) for i, n in enumerate(names)]
    print('  ' + '  '.join(n.ljust(w) for n, w in zip(names, widths)))
    for row in rows:
        print('  ' + '  '.join(_cell(v).ljust(w) for v, w in zip(row, widths)))


def _cell(value) -> str:
    return '' if value is None else str(value)


def crosscheck(args):
    """CLI handler: print every cross-source report."""
    with db_ops(args.db) as conn:
        for title, sql in REPORTS:
            _show(conn, title, sql)


if __name__ == '__main__':
    from argparse import Namespace
    crosscheck(Namespace(db=DEFAULT_DB))
