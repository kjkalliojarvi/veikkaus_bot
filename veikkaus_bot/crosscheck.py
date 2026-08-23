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
from .archive_db import (DEFAULT_DB, HEPPA_START_BRIDGE, db_ops,
                         heppa_track_code)


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

    # horseKey is deliberately left as the parser computed it, so several keys
    # per registry id is the *input* to identity resolution, not a fault. What
    # must be zero is the second column: any id still spanning two canonical
    # identities after the recompute.
    ('Identity: one registry id spanning several horseKeys (resolved by canonicalKey)', """
        SELECT count(*) AS ids_with_several_horseKeys,
               sum(CASE WHEN canonicals > 1 THEN 1 ELSE 0 END) AS UNRESOLVED_must_be_zero
        FROM (SELECT heppaHorseId, count(DISTINCT horseKey) AS keys,
                     count(DISTINCT canonicalKey) AS canonicals
              FROM archive.horse WHERE heppaHorseId IS NOT NULL
              GROUP BY 1 HAVING count(DISTINCT horseKey) > 1)
    """),

    # `finnishTrack` is filtered, and it has to be: a meeting abroad has no
    # Veikkaus card by design — `heppa.backfill_foreign` fetched it precisely
    # because nothing else knows about it — so 356 of them would sit in here
    # looking like the vocabulary gap this report exists to surface. They get
    # their own report below.
    ('Meetings only Heppa has, and whether their horses resolved', f"""
        SELECT coalesce(e.eventType, '(unknown)') AS eventType,
               count(DISTINCT h.meetDate || h.trackCode) AS meetings,
               count(*) AS starts,
               sum(CASE WHEN h.horseKey IS NOT NULL THEN 1 ELSE 0 END) AS horse_resolved
        FROM archive.heppa_start h
        LEFT JOIN archive.heppa_event e
               ON e.meetDate = h.meetDate AND e.trackCode = h.trackCode
        LEFT JOIN archive.card ca ON ca.meetDate = h.meetDate
                                 AND {heppa_track_code('ca')} = h.trackCode
        WHERE ca.cardId IS NULL AND coalesce(h.finnishTrack, true)
        GROUP BY 1 ORDER BY 3 DESC
    """),

    # `heppa-foreign` is a separate opt-in, so this is empty until it has run.
    # Heppa returns only the Finnish-registered runners of a race abroad, so
    # `starts` is a count of our horses' starts and not of a field.
    #
    # `horse_resolved` is well short of every row, and legitimately so: the
    # mapping from horseId to horseKey is built from the races *both* sources
    # cover, so a horse whose Finnish starts fall outside the Veikkaus crawl has
    # no archive.horse row to resolve to. Measured over the first meetings
    # crawled, 17 of 19 at Bollnas and 8 of 12 at Taby.
    ('Starts abroad: what the foreign crawl reached', """
        SELECT trackCode, count(DISTINCT meetDate) AS meetings, count(*) AS starts,
               count(DISTINCT horseId) AS horses,
               sum(CASE WHEN horseKey IS NOT NULL THEN 1 ELSE 0 END) AS horse_resolved,
               min(meetDate) AS first, max(meetDate) AS last
        FROM archive.heppa_start WHERE finnishTrack = FALSE
        GROUP BY 1 ORDER BY 3 DESC LIMIT 25
    """),

    # Any row here is a meeting whose results the merge cannot reach. A whole
    # track showing up is a vocabulary gap, not a crawl gap — that is how the
    # Hr2/HR alias was found.
    ('Meetings the Veikkaus crawl has but the Heppa crawl has not reached', f"""
        SELECT ca.trackAbbreviation, min(ca.trackName) AS track, count(*) AS cards,
               min(ca.meetDate) AS first, max(ca.meetDate) AS last
        FROM archive.card ca
        LEFT JOIN archive.heppa_event e
               ON e.meetDate = ca.meetDate
              AND e.trackCode = {heppa_track_code('ca')}
        WHERE {REAL_MEETINGS} AND e.meetDate IS NULL
        GROUP BY 1 ORDER BY 3 DESC
    """),

    # horse_key() is name + birth year, and Veikkaus writes an import's name
    # inconsistently, so several keys can be one horse. canonicalKey is the
    # resolved identity: the registry id where there is one, the marker-free
    # name key otherwise.
    ('Horse identity: how much did canonicalKey merge?', """
        SELECT count(*) AS horse_rows,
               count(DISTINCT canonicalKey) AS distinct_horses,
               count(*) - count(DISTINCT canonicalKey) AS rows_merged_away,
               sum(CASE WHEN heppaHorseId IS NOT NULL THEN 1 ELSE 0 END) AS resolved_by_registry_id
        FROM archive.horse
    """),

    ('Horse identity: the largest merges, to eyeball', """
        SELECT canonicalKey, count(*) AS keys,
               string_agg(horseName || ' b' || coalesce(cast(birthYear AS text), '?'), '  /  ')
                   AS variants
        FROM archive.horse GROUP BY 1
        HAVING count(*) > 1 ORDER BY 2 DESC, 1 LIMIT 15
    """),

    # `heppa-horses` is a separate opt-in, so this is empty until it has run.
    ('Registry records: coverage and what they add', """
        SELECT (SELECT count(DISTINCT horseId) FROM archive.heppa_start
                 WHERE horseId IS NOT NULL) AS horses_seen_racing,
               (SELECT count(*) FROM archive.heppa_horse) AS registry_records,
               (SELECT count(*) FROM archive.heppa_horse WHERE ueln IS NOT NULL) AS with_ueln,
               (SELECT count(*) FROM archive.heppa_horse WHERE birthDate IS NOT NULL) AS with_exact_birthdate,
               (SELECT count(*) FROM archive.heppa_horse WHERE sireId IS NOT NULL) AS with_sire_id
    """),

    # The tag in a Veikkaus horse name gestures at origin; birthCountry states
    # it. Rows where they disagree are imports racing in Finland — and the
    # reason registrationCountry is the wrong field for identity work.
    ('Registry records: origin against where the horse races', """
        SELECT coalesce(birthCountry, '(unknown)') AS origin,
               count(*) AS horses,
               sum(CASE WHEN registrationCountry <> birthCountry THEN 1 ELSE 0 END)
                   AS races_elsewhere
        FROM archive.heppa_horse GROUP BY 1 ORDER BY 2 DESC LIMIT 12
    """),

    # The failure mode of the name fallback: two horses of different origin
    # sharing a base name and a foaling year. Rows here are merges to distrust.
    ('Horse identity: merged without a registry id to vouch for it', """
        SELECT canonicalKey, count(*) AS keys,
               string_agg(horseName || ' b' || coalesce(cast(birthYear AS text), '?'), '  /  ')
                   AS variants
        FROM archive.horse WHERE heppaHorseId IS NULL
        GROUP BY 1 HAVING count(*) > 1 ORDER BY 2 DESC LIMIT 15
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
