"""The replay may not SELL at a moment the live engine could not, either.

The entry side has been gated since `dcefd01`. The exit side never was, and
`test_audit_backtest_market_hours` said so in its own docstring — "a known
remaining difference rather than an oversight… it deserves its own measurement
rather than riding along with a fix for entries". This is that measurement.

MEASURED 2026-08-07 (`sessioncheck.py`, 275 SPY / 274 AAPL / 274 NVDA days):
the cached stock intraday series spans **08:00–16:45 ET, 36 slots a day**, of
which only 26 (09:30–15:45) are inside regular hours. Ten bars a day — 28% of
every decision point — are pre-market or after-hours. `engine._manage_exits`
returns early for a stock while the market is shut, so on every one of those
bars the replay could stop out, take profit or flatten where live simply cannot.

THE HALF THAT IS NOT THE GATE. `last_of_day` was "the last bar whose day differs
from the next one's", which on an 08:00–16:45 series is the 16:45 bar. Gate the
exits without moving it and `flatten_before_close` fires on a bar nothing may act
on — i.e. never. That is why this fix was deferred once already, and it is what
`_move_last_of_day_into_session` exists for. Both halves are pinned below.
"""

from datetime import datetime, timedelta, timezone

import pytest

from qt.services.backtest import (
    _move_last_of_day_into_session,
    run_backtest,
    run_portfolio_backtest,
)
from qt.services.engine import RISK_DEFAULTS

UTC = timezone.utc
# 2026-08-03 was a Monday. 13:30Z = 09:30 ET, 20:00Z = 16:00 ET.
OPEN_UTC = datetime(2026, 8, 3, 13, 30, tzinfo=UTC)
CLOSE_UTC = datetime(2026, 8, 3, 20, 0, tzinfo=UTC)
RISK = dict(RISK_DEFAULTS, max_total_positions=50, cooldown_hours_after_loss=0)


def _bar(at, close, high=None, low=None):
    return {"t": at.strftime("%Y-%m-%dT%H:%M:%SZ"), "o": close,
            "h": high if high is not None else close,
            "l": low if low is not None else close,
            "c": close, "v": 1e6, "vw": close}


def _strategy(swing=True, **exit_rules):
    base = {"trailing_stop_pct": 0, "stop_loss_pct": 0, "take_profit_pct": 0,
            "max_holding_hours": 0, "flatten_before_close": False,
            "exit_below_vwap": False, "exit_giveback_pct": 0}
    return {
        "asset_class": "stock", "swing_mode": swing, "sizing_usd": 200.0,
        "sleeve_usd": 2000.0, "max_positions": 3,
        "params": {"entry": {"min_day_gain_pct": 1.0, "require_above_vwap": False,
                             "entry_window_start": None, "entry_window_end": None},
                   "exit": {**base, **exit_rules}},
    }


def _run(strategy, bars, *, start, end, symbol="NFLX", market="stock"):
    return run_backtest(strategy, {symbol: bars}, RISK, starting_cash=2000,
                        spread_pct=0, market=market, sim_start=start, sim_end=end)


def _prior_day():
    """A flat prior session so day-gain has a baseline to measure from."""
    return [_bar(OPEN_UTC - timedelta(days=1) + timedelta(minutes=n), 100.0)
            for n in range(0, 390, 15)]


def _bought_then_crashes(crash_at, crash_low):
    """Enter at the open, then put a single deep bar at `crash_at`.

    One variable: WHEN the crash bar sits. The price path is otherwise identical,
    so any difference in the result is the session gate and nothing else."""
    bars = _prior_day()
    at = OPEN_UTC
    end = CLOSE_UTC + timedelta(hours=1)
    while at <= end:
        if at == crash_at:
            bars.append(_bar(at, 102.0, low=crash_low))
        else:
            bars.append(_bar(at, 102.0))
        at += timedelta(minutes=15)
    return bars


def _exits(result):
    return [t for t in (result.get("trade_list") or []) if t.get("exit_reason")]


def test_a_stop_inside_the_session_still_fires():
    """THE CONTROL. Gate too much and the replay stops reproducing the exits it
    exists to grade — which would be a worse bug than the one being fixed."""
    bars = _bought_then_crashes(OPEN_UTC + timedelta(hours=2), crash_low=80.0)
    result = _run(_strategy(stop_loss_pct=5), bars,
                  start=OPEN_UTC, end=CLOSE_UTC + timedelta(hours=2))
    exits = _exits(result)
    assert len(exits) == 1, result.get("diagnosis", {}).get("summary")
    assert "stop" in (exits[0]["exit_reason"] or "").lower(), exits[0]["exit_reason"]


def test_a_stop_after_the_close_does_not_fire():
    """16:30 ET. `engine._manage_exits` is not running — the market is shut — so
    a replay that sells here books a loss live never took."""
    bars = _bought_then_crashes(CLOSE_UTC + timedelta(minutes=30), crash_low=80.0)
    result = _run(_strategy(stop_loss_pct=5), bars,
                  start=OPEN_UTC, end=CLOSE_UTC + timedelta(hours=2))
    assert _exits(result) == [], (
        "the replay stopped out after the close, which live cannot do")


def test_a_stop_before_the_open_does_not_fire():
    """08:15 ET the next morning, the other edge of the same rule. The cache
    really does hold these bars — measured 08:00-16:45 — so this is reachable
    with production data, not a theoretical boundary."""
    bars = _bought_then_crashes(
        OPEN_UTC + timedelta(days=1) - timedelta(hours=1, minutes=15), crash_low=80.0)
    result = _run(_strategy(stop_loss_pct=5), bars,
                  start=OPEN_UTC, end=OPEN_UTC + timedelta(days=1))
    assert _exits(result) == []


def test_crypto_exits_at_any_hour():
    """A 24/7 book has no session to be outside of. Gating it by New York hours
    would delete two thirds of every crypto comparison."""
    bars = _bought_then_crashes(CLOSE_UTC + timedelta(minutes=30), crash_low=80.0)
    result = _run(dict(_strategy(stop_loss_pct=5), asset_class="crypto"), bars,
                  start=OPEN_UTC, end=CLOSE_UTC + timedelta(hours=2),
                  symbol="ADA/USD", market="crypto")
    assert len(_exits(result)) == 1, "a crypto stop must fire at 16:30 ET"


def test_flatten_before_close_still_fires_and_lands_inside_the_session():
    """THE REGRESSION THE MOVE PREVENTS. With `last_of_day` left on the 16:45
    bar, the gate skips it and this exit silently never happens — the position
    is carried overnight instead, which is the opposite of what the rule says."""
    bars = _prior_day()
    at = OPEN_UTC
    while at <= CLOSE_UTC + timedelta(minutes=45):     # runs to 16:45 ET
        bars.append(_bar(at, 102.0))
        at += timedelta(minutes=15)
    # swing=False deliberately: a SWING strategy defers every soft exit until
    # the day after entry (engine.evaluate_exit's `same_day` guard), so
    # flatten-before-close cannot fire on the day you buy and the test would be
    # measuring that rule instead of this one. Stops are unaffected by it, which
    # is why the stop cases above keep the default.
    result = _run(_strategy(swing=False, flatten_before_close=True), bars,
                  start=OPEN_UTC, end=CLOSE_UTC + timedelta(hours=2))
    exits = _exits(result)
    assert len(exits) == 1, "flatten-before-close did not fire at all"
    assert "close" in (exits[0]["exit_reason"] or "").lower()


def test_the_portfolio_loop_is_gated_too():
    """TWO INDEPENDENT BAR LOOPS. `run_portfolio_backtest` has its own copy of
    every rule, and this file's own comments say most of its bugs were a fix
    landing on one loop and not the other. Removing the portfolio exit gate
    passed all six run_backtest cases above, so without this the fix would have
    shipped half-done."""
    strategy = dict(_strategy(stop_loss_pct=5), id=1)
    bars = _bought_then_crashes(CLOSE_UTC + timedelta(minutes=30), crash_low=80.0)
    result = run_portfolio_backtest(
        [strategy], {1: {"NFLX": bars}}, RISK, starting_cash=2000, spread_pct=0,
        sim_start=OPEN_UTC, sim_end=CLOSE_UTC + timedelta(hours=2))
    assert _exits(result) == [], (
        "the portfolio loop stopped out after the close")


def test_the_portfolio_loop_still_exits_inside_the_session():
    """The control for the loop above — gate too much and it stops reproducing
    the exits it exists to grade."""
    strategy = dict(_strategy(stop_loss_pct=5), id=1)
    bars = _bought_then_crashes(OPEN_UTC + timedelta(hours=2), crash_low=80.0)
    result = run_portfolio_backtest(
        [strategy], {1: {"NFLX": bars}}, RISK, starting_cash=2000, spread_pct=0,
        sim_start=OPEN_UTC, sim_end=CLOSE_UTC + timedelta(hours=2))
    assert len(_exits(result)) == 1, result.get("diagnosis", {}).get("summary")


def test_a_daily_bar_backtest_still_exits():
    """A DAILY bar is a whole session, not an instant inside one, and it is
    stamped OUTSIDE 09:30-16:00 by design. Lose that exemption and the gate
    refuses every exit in every daily backtest ever run — which is precisely
    the failure mode that made this fix look expensive."""
    day1 = datetime(2026, 8, 3, 4, 0, tzinfo=UTC)     # midnight ET, daily stamp
    bars = [
        _bar(day1 - timedelta(days=1), 100.0),
        _bar(day1, 102.0),
        _bar(day1 + timedelta(days=1), 102.0, low=80.0),   # the stop bar
        _bar(day1 + timedelta(days=2), 102.0),
    ]
    result = _run(_strategy(swing=False, stop_loss_pct=5), bars,
                  start=day1, end=day1 + timedelta(days=3))
    assert len(_exits(result)) == 1, (
        "a daily backtest took no exit — the daily-bar exemption is gone")


def test_the_portfolio_loop_flattens_inside_the_session_too():
    """`stock_session` is passed at TWO `_prepare` call sites. Hard-coding the
    portfolio one to False passed every other case here, because the stop tests
    never read `last_of_day` — only a flatten case does."""
    strategy = dict(_strategy(swing=False, flatten_before_close=True), id=1)
    bars = _prior_day()
    at = OPEN_UTC
    while at <= CLOSE_UTC + timedelta(minutes=45):      # runs to 16:45 ET
        bars.append(_bar(at, 102.0))
        at += timedelta(minutes=15)
    result = run_portfolio_backtest(
        [strategy], {1: {"NFLX": bars}}, RISK, starting_cash=2000, spread_pct=0,
        sim_start=OPEN_UTC, sim_end=CLOSE_UTC + timedelta(hours=2))
    exits = _exits(result)
    assert len(exits) == 1, "the portfolio loop never flattened"
    assert "close" in (exits[0]["exit_reason"] or "").lower()


# ── the last_of_day move, unit level ─────────────────────────────────────────
def _prepared(*stamps):
    return [{"ts": s, "day": s.astimezone(timezone.utc).strftime("%Y-%m-%d"),
             "last_of_day": i == len(stamps) - 1, "first_of_day": i == 0}
            for i, s in enumerate(stamps)]


def test_last_of_day_moves_to_the_final_in_session_bar():
    rows = _prepared(OPEN_UTC, CLOSE_UTC - timedelta(minutes=15),
                     CLOSE_UTC, CLOSE_UTC + timedelta(minutes=45))
    _move_last_of_day_into_session(rows)
    assert [r["last_of_day"] for r in rows] == [False, True, False, False], (
        "expected the 15:45 bar, not the 16:45 one")


def test_a_single_bar_day_is_left_alone():
    """One bar in a day is a DAILY bar and its flag must survive untouched.

    The stamp is INSIDE the session on purpose. An out-of-session stamp is
    caught by the `not tradable` guard first, which masks the `len < 2` one —
    so the obvious fixture tests the wrong branch and the mutation lives."""
    rows = _prepared(OPEN_UTC + timedelta(hours=2))
    _move_last_of_day_into_session(rows)
    assert rows[0]["last_of_day"] is True


def test_a_day_with_no_tradable_bar_keeps_its_own_last():
    """A window clipped to pre-market only. Flattening on the wrong bar is bad;
    a held position with no flatten bar at all is worse."""
    pre = OPEN_UTC - timedelta(hours=2)
    rows = _prepared(pre, pre + timedelta(minutes=15), pre + timedelta(minutes=30))
    # Flag deliberately on the MIDDLE bar. "Keeps its own last" has to mean the
    # rows are not touched at all; re-pointing at the final bar produces the
    # same answer whenever the flag already sits there, which is what let a
    # `tradable = day_bars` fallback pass unnoticed.
    rows[2]["last_of_day"], rows[1]["last_of_day"] = False, True
    _move_last_of_day_into_session(rows)
    assert [r["last_of_day"] for r in rows] == [False, True, False]


def test_each_day_is_moved_independently():
    """Two sessions in one series must each get their own flag — a single global
    "last bar" would flatten only on the final day of the whole backtest."""
    d2 = OPEN_UTC + timedelta(days=1)
    rows = _prepared(OPEN_UTC, CLOSE_UTC - timedelta(minutes=15),
                     CLOSE_UTC + timedelta(minutes=45),
                     d2, d2 + timedelta(hours=6), d2 + timedelta(hours=7))
    _move_last_of_day_into_session(rows)
    assert [r["last_of_day"] for r in rows] == [False, True, False,
                                                False, True, False]


@pytest.mark.parametrize("hhmm,inside", [
    ((13, 30), True),    # 09:30 ET — the opening bar is IN
    ((19, 45), True),    # 15:45 ET — the last bar of the session
    ((20, 0), False),    # 16:00 ET — the closing print is OUT
    ((13, 15), False),   # 09:15 ET — pre-market
])
def test_the_session_boundaries_are_half_open(hhmm, inside):
    """A bar's stamp is its OPENING minute, so 15:45 is in and 16:00 is out."""
    from qt.services.backtest import _in_session_ts

    assert _in_session_ts(datetime(2026, 8, 3, *hhmm, tzinfo=UTC)) is inside
