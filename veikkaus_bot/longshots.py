"""Which T-pool longshots win: a model for horses holding <= 4 % of a T-pool leg.

The query layer and the model behind `veikkaus longshots`. **The market here
is one T-pool's own leg percentage**, not the win odds, and the pool type is
asked for rather than mixed: a T75 bettor spreads over seven legs and a T4
bettor over four, so the same horse holds a different share of each, and one
model across types would be averaging different markets. Each type gets its
own fit on its own history.

The win pool and the exacta (KAK) pool come in only as *disagreements* with the
T-pool — `lnWinRatio` and `lnKakRatio` are 0 where they agree with it. Measured
before this was written, the KAK pool out-informs the win pool and the T-pool
is the weakest of the three as a win signal; a T bettor builds a chain and
spreads, so a leg percentage is monotone in the win chance but is not one.
That is exactly the gap a longshot pick lives in.

**A pick is a horse whose P(win) is above its T share** (`edge` > 1). The
payout of a T bet is split among the winning tickets, so a horse that wins
more often than the pool backs it is the leg where a ticket gains on the
field. That is the T-pool counterpart of expected return > 1; there is no
per-horse price to multiply by.

Three things are handled once here:

- **A scratched runner is out of the leg and its share renormalised away.** It
  keeps the money bet on it before the withdrawal (up to 72 % of a leg), which
  still counts into the leg's ~10000 — so shares are taken over the runners
  that ran, in training and live alike.
- **The win market comes from the leg payload's own `winProbable`, and only
  where the whole leg is priced.** On a part-priced race being priced is the
  result. Taking it from the same payload in training and live means the two
  read one figure.
- **The driver's record stops at the day before**, never the day itself: a
  live scorer of leg 4 does not have leg 1's result in the archive yet.

All three markets are *closing* figures in the archive. Scored live they are
whatever the pools hold at the moment of the fetch, so a score means most just
before the first leg.

Run the backtest directly:

    uv run python -m veikkaus_bot.longshots T5
"""
from datetime import date, datetime
import orjson
import math
import sys

import numpy as np

from .archive_db import DEFAULT_DB, db_read
from .crawler import LEG_POOL_TYPES
from .fetcher import Fetcher


# The population: the share the question names.
MAX_T_SHARE = 0.04

# A leg share of 0 has no logarithm; a hundredth of a percent is the payload's
# resolution, so it is the floor.
MIN_T_SHARE = 0.0001

# A start this many days after the last counts as fresh.
FRESH_DAYS = 14

# Off an auto start the outside posts are worse; off a volt the back row is
# not, so this is auto-only.
AUTO_OUTSIDE_POST = 7

# Shrinks a driver's longshot record towards zero: a driver with 100 prior
# longshot rides is trusted half as much as the raw excess says.
DRIVER_PRIOR = 100

# Not meetings for the live scorer: the Swedish simulcasts. The meta-cards
# (48/88) are kept, unlike elsewhere in the repo — a Magic Monday T4 is a real
# pool whose legs are real races, and it is the pool being bet.
SIMULCAST_TRACK_NUMBERS = (57, 87)

# One row per runner per leg of one pool type. `tShare` is renormalised over
# the runners that ran; `pWin` is the win market's view from the same payload,
# NULL unless the whole leg is priced.
LEG_RUNNERS = """
    WITH leg AS (
        SELECT lp.poolId, lp.legNumber, lp.raceId, lp.startNumber, lp.runnerId,
               lp.percentage / sum(lp.percentage) OVER l AS tShare,
               CASE WHEN count(*) OVER l = count(*) FILTER (WHERE lp.winProbable > 0) OVER l
                    THEN (100.0 / nullif(lp.winProbable, 0))
                         / sum(100.0 / nullif(lp.winProbable, 0)) OVER l END AS pWin,
               lp.winProbable / 100.0 AS odds,
               coalesce(lp.placement = 1, false) AS won
        FROM archive.leg_percentage lp
        WHERE lp.poolType = ? AND lp.scratched IS NULL
        WINDOW l AS (PARTITION BY lp.poolId, lp.legNumber)),
    kak AS (
        SELECT runnerId, percentage FROM archive.bet_percentage WHERE poolType = 'KAK')
    SELECT leg.*,
           kak.percentage / sum(kak.percentage) OVER (PARTITION BY leg.poolId, leg.legNumber)
               AS kakShare,
           s.horseName, s.driverName, s.startTrack, s.frontShoes, s.rearShoes,
           s.startInterval, r.startType, coalesce(r.monte, false) AS monte,
           r.number AS raceNumber, ca.meetDate::DATE AS meetDate,
           ca.trackAbbreviation AS track
    FROM leg
    JOIN archive.start s USING (raceId, startNumber)
    JOIN archive.race r USING (raceId)
    JOIN archive.card ca USING (cardId)
    LEFT JOIN kak ON kak.runnerId = leg.runnerId
"""

IS_LONGSHOT = f'tShare <= {MAX_T_SHARE}'

# The driver's longshot record in this pool type, as an excess over the pool:
# the sum of (won - tShare) over every earlier longshot ride, shrunk by
# DRIVER_PRIOR. A driver whose longshots win exactly what the pool says
# scores 0 however many rides.
#
# RANGE ... 1 DAY PRECEDING, not ROWS ... 1 PRECEDING: the window has to stop
# at the day before, because a live scorer does not have the same afternoon's
# earlier legs in the archive.
DRIVER_RECORD = f"""
    SELECT *,
        coalesce(sum(won::INT - tShare) OVER w, 0)
            / (coalesce(count(*) OVER w, 0) + {DRIVER_PRIOR}) AS driverExcess
    FROM ({LEG_RUNNERS}) WHERE {IS_LONGSHOT}
    WINDOW w AS (PARTITION BY driverName ORDER BY meetDate
                 RANGE BETWEEN UNBOUNDED PRECEDING AND INTERVAL 1 DAY PRECEDING)
"""

# The same record, totalled up to the eve of a card being scored.
DRIVER_TOTALS = f"""
    SELECT driverName,
           sum(won::INT - tShare) / (count(*) + {DRIVER_PRIOR}) AS driverExcess
    FROM ({LEG_RUNNERS}) WHERE {IS_LONGSHOT} AND meetDate < ?
    GROUP BY driverName
"""

FEATURES = ('lnMarket', 'lnWinRatio', 'hasWin', 'lnKakRatio', 'hasKak',
            'bare', 'fresh', 'monte', 'autoOutside', 'driverExcess')


def feature_row(r: dict) -> list[float]:
    """One start's features, in FEATURES order, from a dict of its raw facts.

    Shared by the backtest and the live scorer, so the two cannot drift. The
    other two markets are logged against the T share, which makes 0 the
    'agrees with the T-pool' point, and a missing one is a 0 plus a flag rather
    than a dropped row: early in the day the win pool is often not yet priced
    on every runner.
    """
    t = max(r['tShare'], MIN_T_SHARE)
    win, kak = r.get('pWin'), r.get('kakShare')
    has_win = win is not None and win > 0
    has_kak = kak is not None and kak > 0
    bare = 'NO_SHOES' in (r.get('frontShoes'), r.get('rearShoes'))
    interval = r.get('startInterval')
    post = r.get('startTrack') or 0
    return [math.log(t),
            math.log(win / t) if has_win else 0.0, float(has_win),
            math.log(kak / t) if has_kak else 0.0, float(has_kak),
            float(bare),
            float(interval is not None and interval <= FRESH_DAYS),
            float(bool(r.get('monte'))),
            float(r.get('startType') == 'CAR_START' and post >= AUTO_OUTSIDE_POST),
            r.get('driverExcess') or 0.0]


def training_rows(conn, pool_type: str) -> list[dict]:
    cur = conn.execute(DRIVER_RECORD + ' ORDER BY meetDate, poolId, legNumber, startNumber',
                       [pool_type])
    names = [d[0] for d in cur.description]
    return [dict(zip(names, row)) for row in cur.fetchall()]


def design(rows: list[dict]) -> tuple[np.ndarray, np.ndarray]:
    x = np.array([[1.0, *feature_row(r)] for r in rows])
    y = np.array([float(r['won']) for r in rows])
    return x, y


def fit(x: np.ndarray, y: np.ndarray, ridge: float = 1e-3) -> np.ndarray:
    """Logistic regression by Newton's method. A small ridge keeps the
    Hessian invertible when a flag column is all zeros in a slice."""
    beta = np.zeros(x.shape[1])
    for _ in range(50):
        p = 1 / (1 + np.exp(-x @ beta))
        grad = x.T @ (y - p) - ridge * beta
        hess = (x * (p * (1 - p))[:, None]).T @ x + ridge * np.eye(x.shape[1])
        step = np.linalg.solve(hess, grad)
        beta += step
        if np.abs(step).max() < 1e-8:
            break
    return beta


def predict(beta: np.ndarray, x: np.ndarray) -> np.ndarray:
    return 1 / (1 + np.exp(-x @ beta))


def log_loss(p: np.ndarray, y: np.ndarray) -> float:
    p = np.clip(p, 1e-9, 1 - 1e-9)
    return float(-(y * np.log(p) + (1 - y) * np.log(1 - p)).mean())


def backtest(conn, pool_type: str, train_to: int = 2024):
    """Fit one pool type on the seasons up to `train_to`, report every later one.

    The baseline is the same regression on `lnMarket` alone, so it already
    corrects however the pool misprices its longshots on average: what is left
    is what the other features add. A pick's yardstick is wins against the
    wins the pool expected (the sum of their shares) — above 1.00 means the
    pool underbacked them.
    """
    rows = training_rows(conn, pool_type)
    train = [r for r in rows if r['meetDate'].year <= train_to]
    if not train:
        print(f'No {pool_type} longshots in the archive up to {train_to}.')
        return
    x, y = design(train)
    beta, base = fit(x, y), fit(x[:, :2], y)
    print(f'{pool_type}: trained on {len(train):,} longshots, seasons to {train_to}.')
    for name, b in zip(('intercept', *FEATURES), beta):
        print(f'  {name:<13} {b:+.3f}')
    for year in sorted({r['meetDate'].year for r in rows if r['meetDate'].year > train_to}):
        test = [r for r in rows if r['meetDate'].year == year]
        xt, yt = design(test)
        p, pb = predict(beta, xt), predict(base, xt[:, :2])
        print(f'\n{year}: {len(test):,} longshots, {int(yt.sum())} won, '
              f'pool expected {sum(r["tShare"] for r in test):.1f}')
        print(f'  log-loss  model {log_loss(p, yt):.5f}   pool only {log_loss(pb, yt):.5f}')
        picks = [r for r, q in zip(test, p) if q > max(r['tShare'], MIN_T_SHARE)]
        if picks:
            won = sum(r['won'] for r in picks)
            expected = sum(r['tShare'] for r in picks)
            print(f'  edge > 1: {len(picks):,} picks, {won} won, pool expected '
                  f'{expected:.1f} ({won / expected:.2f}x)')
    return beta


# --- live -----------------------------------------------------------------


def _collection(fetcher: Fetcher, path: str) -> list:
    result = fetcher.fetch(path)
    if result.body is None:
        return []
    payload = orjson.loads(result.body)
    return payload.get('collection', []) if isinstance(payload, dict) else []


def _last_start(prev_starts: list) -> date | None:
    """The most recent earlier start's date, from `shortMeetDate` (dd.mm.yy):
    `meetDate` there is a day early, being Finnish midnight in UTC."""
    days = [datetime.strptime(s['shortMeetDate'], '%d.%m.%y').date()
            for s in prev_starts if s.get('shortMeetDate')]
    return max(days, default=None)


def live_cards(fetcher: Fetcher, day: date) -> list[dict]:
    """The day's Finnish cards that carry at least one T-pool."""
    return [c for c in _collection(fetcher, f'/cards/date/{day.isoformat()}')
            if c.get('country') == 'FI'
            and c.get('trackNumber') not in SIMULCAST_TRACK_NUMBERS
            and set(c.get('totoPools') or []) & set(LEG_POOL_TYPES)]


def ask_pool(cards: list[dict]) -> str | None:
    """Ask which T-pool to score, offering the ones the day's cards carry."""
    offered = {}
    for c in cards:
        for t in c.get('totoPools') or []:
            if t in LEG_POOL_TYPES:
                offered.setdefault(t, []).append(c.get('trackAbbreviation'))
    if not offered:
        return None
    for t in sorted(offered):
        print(f'  {t:<4} {", ".join(offered[t])}')
    choice = input('Which T-bet? ').strip().upper()
    return choice if choice in offered else None


def live_legs(fetcher: Fetcher, cards: list[dict], pool_type: str, day: date) -> list[dict]:
    """Every runner of every leg of the pool type, with the features' raw facts.

    Fetched directly and kept in memory: **never through the manifest**. A
    card crawled before it runs is marked `done` with no results, and stays so.
    Nothing is written to the raw zone either.
    """
    rows = []
    for card in cards:
        if pool_type not in (card.get('totoPools') or []):
            continue
        races = {r['raceId']: r for r in _collection(fetcher, f'/card/{card["cardId"]}/races')}
        pool_id = None
        for race_id in races:
            pool_id = next((p['poolId'] for p in _collection(fetcher, f'/race/{race_id}/pools')
                            if p.get('poolType') == pool_type), None)
            if pool_id:
                break
        if not pool_id:
            continue
        result = fetcher.fetch(f'/pool/{pool_id}/odds')
        legs = orjson.loads(result.body).get('odds', []) if result.body else []
        by_race = {}
        for o in legs:
            if not o.get('scratched'):
                by_race.setdefault(o['raceId'], []).append(o)
        for race_id, leg in by_race.items():
            runners = {r['startNumber']: r
                       for r in _collection(fetcher, f'/race/{race_id}/runners')}
            rows.extend(_leg_rows(card, races.get(race_id, {}), leg, runners, day))
    return rows


def _leg_rows(card: dict, race: dict, leg: list, runners: dict, day: date) -> list[dict]:
    """One leg's runners, with the shares taken the way `LEG_RUNNERS` takes
    them: over the runners that ran, and the win market only if all priced."""
    total = sum(o.get('percentage') or 0 for o in leg)
    if not total:
        return []
    priced = all((o.get('winProbable') or 0) > 0 for o in leg)
    inv = {o['runnerNumber']: 100 / o['winProbable'] for o in leg} if priced else {}
    kak = {o['runnerNumber']: (((runners.get(o['runnerNumber']) or {}).get('betPercentages')
                                or {}).get('KAK') or {}).get('percentage') for o in leg}
    kak_total = sum(v for v in kak.values() if v)
    rows = []
    for o in leg:
        n = o['runnerNumber']
        r = runners.get(n) or {}
        last = _last_start(r.get('prevStarts') or [])
        rows.append({
            'legNumber': o.get('legNumber'), 'raceId': o['raceId'],
            'raceNumber': o.get('raceNumber') or race.get('number'),
            'track': card.get('trackAbbreviation'), 'startNumber': n,
            'horseName': r.get('horseName') or '', 'driverName': r.get('driverName'),
            'tShare': (o.get('percentage') or 0) / total,
            'pWin': inv[n] / sum(inv.values()) if priced else None,
            'odds': o['winProbable'] / 100 if priced else None,
            'kakShare': kak[n] / kak_total if kak[n] else None,
            'frontShoes': r.get('frontShoes'), 'rearShoes': r.get('rearShoes'),
            'startInterval': (day - last).days if last else None,
            'monte': race.get('monte'), 'startType': race.get('startType'),
            'startTrack': r.get('startTrack')})
    return rows


def score(conn, rows: list[dict], pool_type: str, day: date) -> list[tuple[dict, float]]:
    """Fit the pool type on every longshot before `day`, then score `rows`'
    longshots. Returned in leg order, best edge first within a leg."""
    train = [r for r in training_rows(conn, pool_type) if r['meetDate'] < day]
    if not train:
        return []
    beta = fit(*design(train))
    excess = dict(conn.execute(DRIVER_TOTALS, [pool_type, day]).fetchall())
    shots = [r for r in rows if r['tShare'] <= MAX_T_SHARE]
    for r in shots:
        r['driverExcess'] = excess.get(r['driverName'], 0.0)
    p = predict(beta, np.array([[1.0, *feature_row(r)] for r in shots])) if shots else []
    return sorted(zip(shots, p), key=lambda pair: (
        pair[0]['track'], pair[0]['legNumber'], -pair[1] / max(pair[0]['tShare'], MIN_T_SHARE)))


def longshots(args):
    """CLI handler: backtest a T-pool's model, or rank a card's longshots in it."""
    pool_type = args.pool.upper() if args.pool else None
    if args.backtest:
        if pool_type not in LEG_POOL_TYPES:
            sys.exit(f'--backtest needs --pool, one of {", ".join(LEG_POOL_TYPES)}.')
        with db_read(args.db) as conn:
            backtest(conn, pool_type, args.train_to)
        return
    day = datetime.strptime(args.date, '%Y-%m-%d').date() if args.date else date.today()
    fetcher = Fetcher(args.raw, args.delay)
    cards = live_cards(fetcher, day)
    if not cards:
        print(f'No Finnish T-pools on {day}.')
        return
    if pool_type is None:
        pool_type = ask_pool(cards)
    if not pool_type or pool_type not in {t for c in cards for t in c.get('totoPools') or []}:
        print(f'No {pool_type or "such"} pool on {day}.')
        return
    rows = live_legs(fetcher, cards, pool_type, day)
    with db_read(args.db) as conn:
        ranked = score(conn, rows, pool_type, day)
    # Only the picks: the horses the model rates above their share of the pool.
    picks = [(r, p) for r, p in ranked if p > max(r['tShare'], MIN_T_SHARE)]
    print(f'{len(picks)} of {len(ranked)} {pool_type} longshots (<= {MAX_T_SHARE:.0%} of '
          f'their leg) on {day} have an edge above 1.00 (P(win) / T share):')
    if not picks:
        return
    print(f'{"leg":<8} {"race":>4} {"#":>2}  {"horse":<24} {"driver":<20} {"T %":>5} '
          f'{"P(win)":>7} {"odds":>6} {"edge":>5}')
    for r, p in picks:
        odds = f'{r["odds"]:>6.1f}' if r.get('odds') else f'{"-":>6}'
        print(f'{r["track"]:<5}{r["legNumber"]:>3} {r["raceNumber"] or 0:>4} '
              f'{r["startNumber"]:>2}  {r["horseName"][:24]:<24} '
              f'{(r["driverName"] or "")[:20]:<20} {100 * r["tShare"]:>5.2f} '
              f'{100 * p:>6.2f}% {odds} {p / max(r["tShare"], MIN_T_SHARE):>5.2f}')


if __name__ == '__main__':
    with db_read(DEFAULT_DB) as conn:
        backtest(conn, sys.argv[1] if len(sys.argv) > 1 else 'T5')
