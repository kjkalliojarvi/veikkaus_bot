"""The features behind `veikkaus longshots`.

Each test pins one way a T-pool longshot model can quietly go wrong: a
scratched runner whose money still counts into the leg, a part-priced leg where
having a win odd at all means the horse was placed, a driver record that counts
the same afternoon's earlier legs, which a live scorer does not have yet, and a
second pool type leaking into the first's market.
"""
from datetime import date

import duckdb
import pytest

from veikkaus_bot import archive_db, longshots


@pytest.fixture
def conn():
    conn = duckdb.connect(':memory:')
    archive_db.create(conn)
    return conn


def card(conn, card_id, meet_date, track='Ku'):
    conn.execute("""INSERT INTO archive.card (cardId, country, meetDate, trackAbbreviation,
                    trackNumber) VALUES (?, 'FI', ?, ?, 5)""", [card_id, meet_date, track])


def race(conn, race_id, card_id, number=1):
    conn.execute("""INSERT INTO archive.race (raceId, cardId, number, startType, monte)
                    VALUES (?, ?, ?, 'VOLT_START', false)""", [race_id, card_id, number])


def runner(conn, pool_id, leg, race_id, number, share, placement=None, odds: float | None = 10.0,
           driver='D', scratched=None, pool_type='T5', kak=None):
    """One runner in one leg; `share` in percent, `odds` decimal or None."""
    runner_id = race_id * 100 + number
    conn.execute("""INSERT OR IGNORE INTO archive.start (raceId, startNumber, runnerId,
                    horseName, driverName, startTrack, scratched)
                    VALUES (?, ?, ?, ?, ?, ?, false)""",
                 [race_id, number, runner_id, f'h{runner_id}', driver, number])
    conn.execute("""INSERT INTO archive.leg_percentage (poolId, poolType, legNumber, raceId,
                    startNumber, runnerId, percentage, winProbable, scratched, placement)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                 [pool_id, pool_type, leg, race_id, number, runner_id, round(share * 100),
                  None if odds is None else round(odds * 100), scratched, placement])
    if kak is not None:
        conn.execute("INSERT INTO archive.bet_percentage VALUES (?, 'KAK', ?)", [runner_id, kak])


def leg(conn, pool_id, leg_number, race_id, longshot_wins=False, driver='D'):
    """A three-horse leg whose third runner holds 3 % of the pool."""
    runner(conn, pool_id, leg_number, race_id, 1, 80, None if longshot_wins else 1, driver='F')
    runner(conn, pool_id, leg_number, race_id, 2, 17, 2, driver='G')
    runner(conn, pool_id, leg_number, race_id, 3, 3, 1 if longshot_wins else 3, driver=driver)


def rows(conn, pool_type='T5'):
    cur = conn.execute(longshots.LEG_RUNNERS, [pool_type])
    names = [d[0] for d in cur.description]
    return {(r['raceId'], r['startNumber']): r
            for r in (dict(zip(names, row)) for row in cur.fetchall())}


def test_a_scratched_runner_is_out_and_its_money_renormalised(conn):
    """A withdrawn runner keeps what was bet on it before the withdrawal, and
    that still counts into the leg's ~10000 — so the share is taken over the
    runners that ran."""
    card(conn, 1, '2026-01-01')
    race(conn, 10, 1)
    runner(conn, 7, 1, 10, 1, 40, placement=1)
    runner(conn, 7, 1, 10, 2, 10)
    runner(conn, 7, 1, 10, 3, 50, scratched=True)
    seen = rows(conn)
    assert set(seen) == {(10, 1), (10, 2)}
    assert seen[(10, 1)]['tShare'] == pytest.approx(0.8)


def test_a_part_priced_leg_has_no_win_market(conn):
    """On a part-priced race being priced at all is the result. The leg stays
    — its T shares are sound — but the win market is NULL, not a leak."""
    card(conn, 1, '2026-01-01')
    race(conn, 10, 1)
    runner(conn, 7, 1, 10, 1, 90, placement=1, odds=1.5)
    runner(conn, 7, 1, 10, 2, 10, odds=None)
    seen = rows(conn)
    assert [r['pWin'] for r in seen.values()] == [None, None]
    race(conn, 11, 1, number=2)
    leg(conn, 7, 2, 11)
    assert sum(rows(conn)[(11, n)]['pWin'] for n in (1, 2, 3)) == pytest.approx(1)


def test_another_pool_type_is_not_this_market(conn):
    """A T4 and a T5 share races, and their percentages differ; one fit must
    read one pool type."""
    card(conn, 1, '2026-01-01')
    race(conn, 10, 1)
    leg(conn, 7, 1, 10)
    runner(conn, 8, 1, 10, 3, 9, pool_type='T4')
    assert rows(conn)[(10, 3)]['tShare'] == pytest.approx(0.03)
    assert rows(conn, 'T4')[(10, 3)]['tShare'] == pytest.approx(1)


def excess(conn):
    return {(r['raceId'], r['startNumber']): r['driverExcess']
            for r in longshots.training_rows(conn, 'T5') if r['driverName'] == 'D'}


def test_the_driver_record_stops_at_the_day_before(conn):
    """Leg 1's longshot win is on the same afternoon as leg 2, and a live
    scorer of leg 2 does not have it yet; the next day it counts."""
    card(conn, 1, '2026-01-01')
    race(conn, 10, 1, number=1)
    leg(conn, 7, 1, 10, longshot_wins=True)
    race(conn, 11, 1, number=2)
    leg(conn, 7, 2, 11)
    card(conn, 2, '2026-01-02')
    race(conn, 20, 2)
    leg(conn, 8, 1, 20)
    seen = excess(conn)
    assert seen[(10, 3)] == 0
    assert seen[(11, 3)] == 0
    assert seen[(20, 3)] > 0


def test_driver_totals_agree_with_the_window(conn):
    """`score` reads DRIVER_TOTALS and training reads DRIVER_RECORD; for the
    same eve they must be one number."""
    card(conn, 1, '2026-01-01')
    race(conn, 10, 1)
    leg(conn, 7, 1, 10, longshot_wins=True)
    card(conn, 2, '2026-01-02')
    race(conn, 20, 2)
    leg(conn, 8, 1, 20)
    totals = dict(conn.execute(longshots.DRIVER_TOTALS, ['T5', date(2026, 1, 2)]).fetchall())
    assert totals['D'] == pytest.approx(excess(conn)[(20, 3)])


def test_the_other_markets_are_against_the_t_share(conn):
    """A horse the exacta pool backs exactly as much as the T-pool has a log
    ratio of 0, and one with no KAK row is flagged rather than dropped."""
    card(conn, 1, '2026-01-01')
    race(conn, 10, 1)
    runner(conn, 7, 1, 10, 1, 50, placement=1, kak=5000)
    runner(conn, 7, 1, 10, 2, 50, kak=5000)
    runner(conn, 7, 1, 10, 3, 0)
    seen = rows(conn)
    first = dict(zip(longshots.FEATURES, longshots.feature_row(seen[(10, 1)])))
    third = dict(zip(longshots.FEATURES, longshots.feature_row(seen[(10, 3)])))
    assert first['hasKak'] == 1 and first['lnKakRatio'] == pytest.approx(0)
    assert third['hasKak'] == 0 and third['lnKakRatio'] == 0
    assert third['lnMarket'] == pytest.approx(-9.2103, abs=1e-3)   # the share floor


def test_a_live_leg_takes_shares_the_way_the_archive_does():
    """Shares over the runners that ran, and the win market only if every
    one of them is priced — `LEG_RUNNERS`'s rules, applied to a payload."""
    card_ = {'trackAbbreviation': 'Ku'}
    race_ = {'number': 4}
    leg_ = [{'legNumber': 1, 'raceId': 1, 'runnerNumber': 1, 'percentage': 9000,
             'winProbable': 150},
            {'legNumber': 1, 'raceId': 1, 'runnerNumber': 2, 'percentage': 300,
             'winProbable': 0}]
    seen = longshots._leg_rows(card_, race_, leg_, {}, date(2026, 1, 1))
    assert [r['tShare'] for r in seen] == pytest.approx([9000 / 9300, 300 / 9300])
    assert [r['pWin'] for r in seen] == [None, None]
    leg_[1]['winProbable'] = 3000
    seen = longshots._leg_rows(card_, race_, leg_, {}, date(2026, 1, 1))
    assert sum(r['pWin'] for r in seen) == pytest.approx(1)
    assert seen[1]['odds'] == 30.0
