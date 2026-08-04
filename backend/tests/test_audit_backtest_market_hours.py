"""The replay may not buy at a moment the live engine could not.

MEASURED. The comparison on strategy 25 reported "the replay bought NFLX. You
never did — it believes something was tradable that wasn't", at 16:00 ET on
2026-08-03. The replay's own per-bar log showed why, and it was not the price:
NFLX was over the entry threshold from the 09:30 opening bar and never dropped
below it. It was evaluated on exactly ONE bar of the 391 that day, because the
strategy ranks its pool and only the top 5 of 10 are candidates on any bar —
and NFLX only reached the top 5 on the closing print.

That print is the one bar the live engine structurally cannot act on.
`_consider_entries` skips stock entries outright when the broker's clock says
the market is shut ("if strategy.asset_class == 'stock' and not market_open"),
and at 16:00:00 ET it is. The simulator had no equivalent gate: it buckets days
by the ET session but never asked whether a bar sat inside trading hours, so it
got one decision point per day that the engine never has — and it spent it.

EXITS ARE NOT GATED HERE, and that is a known remaining difference rather than
an oversight: live skips stock EXITS when the market is shut too (see
engine._manage_exits), so a stop hit on the closing print does not fire live
until the next open. Changing that moves every stop, target and trailing exit in
the suite, and it deserves its own measurement rather than riding along with a
fix for entries. Named here so the gap is on the record.
"""

from datetime import datetime, timedelta, timezone

from qt.services.backtest import run_backtest
from qt.services.engine import RISK_DEFAULTS

UTC = timezone.utc
# 2026-08-03 was a Monday. 13:30Z is 09:30 ET and 20:00Z is 16:00 ET.
OPEN_UTC = datetime(2026, 8, 3, 13, 30, tzinfo=UTC)
CLOSE_UTC = datetime(2026, 8, 3, 20, 0, tzinfo=UTC)

STRATEGY = {
    "asset_class": "stock",
    "swing_mode": True,
    "sizing_usd": 200.0,
    "sleeve_usd": 2000.0,
    "max_positions": 3,
    "params": {
        "entry": {"min_day_gain_pct": 1.3, "require_above_vwap": False,
                  "entry_window_start": None, "entry_window_end": None},
        "exit": {"trailing_stop_pct": 0, "stop_loss_pct": 50, "take_profit_pct": 0,
                 "max_holding_hours": 0, "flatten_before_close": False,
                 "exit_below_vwap": False},
    },
}


def _bar(at: datetime, close: float) -> dict:
    return {"t": at.strftime("%Y-%m-%dT%H:%M:%SZ"), "o": close, "h": close,
            "l": close, "c": close, "v": 1e6, "vw": close}


def _series(qualifying_from: datetime) -> list[dict]:
    """A flat prior day to set the baseline, then 2026-08-03 minute by minute:
    below the entry threshold until `qualifying_from`, over it after.

    NFLX's real shape was the opposite — qualifying all day and only becoming a
    CANDIDATE at the close — but the ranking is not what is being pinned here.
    Moving the qualifying moment is the same experiment with one variable."""
    bars = [_bar(OPEN_UTC - timedelta(days=1) + timedelta(minutes=n), 100.0) for n in range(0, 390, 15)]
    at = OPEN_UTC
    while at <= CLOSE_UTC:
        bars.append(_bar(at, 102.0 if at >= qualifying_from else 100.2))
        at += timedelta(minutes=1)
    return bars


def _entries(result: dict) -> list[dict]:
    return (result.get("trade_list") or []) + (result.get("open_positions") or [])


def _run(bars: list[dict]) -> dict:
    return run_backtest(
        STRATEGY, {"NFLX": bars}, dict(RISK_DEFAULTS, max_total_positions=50),
        starting_cash=2000, spread_pct=0,
        sim_start=OPEN_UTC, sim_end=CLOSE_UTC + timedelta(minutes=1),
    )


def test_the_closing_print_is_not_a_decision_point():
    """16:00 ET is the closing auction. The engine's clock says shut, so it never
    sees this bar — and a replay that does gets a free trade every single day."""
    result = _run(_series(qualifying_from=CLOSE_UTC))
    assert _entries(result) == [], (
        "the replay bought on the 16:00 bar, which the live engine cannot act on"
    )


def test_a_bar_inside_the_session_still_trades():
    """The control, and the whole point of the gate being about HOURS rather
    than about the last bar: gate too much and the replay stops reproducing the
    trades it is supposed to grade."""
    result = _run(_series(qualifying_from=OPEN_UTC + timedelta(hours=2)))
    entries = _entries(result)
    assert len(entries) == 1, result.get("diagnosis", {}).get("summary")
    assert entries[0]["symbol"] == "NFLX"


def test_pre_market_is_refused_too():
    """Same rule, other edge. The cache holds 09:30–16:00 today, so this is not
    yet reachable with real data — it is pinned because "outside the session" is
    the rule, and "the last bar of the day" is not."""
    early = [_bar(OPEN_UTC - timedelta(days=1) + timedelta(minutes=n), 100.0) for n in range(0, 390, 15)]
    at = OPEN_UTC - timedelta(hours=2)          # 07:30 ET, pre-market
    while at < OPEN_UTC:
        early.append(_bar(at, 102.0))
        at += timedelta(minutes=1)
    result = run_backtest(
        STRATEGY, {"NFLX": early}, dict(RISK_DEFAULTS, max_total_positions=50),
        starting_cash=2000, spread_pct=0,
        sim_start=OPEN_UTC - timedelta(hours=3), sim_end=OPEN_UTC,
    )
    assert _entries(result) == []


def test_crypto_is_not_gated_because_it_never_closes():
    """A 24/7 book has no session to be outside of, and gating it by New York
    hours would delete two thirds of every crypto comparison."""
    crypto = dict(STRATEGY, asset_class="crypto")
    bars = [_bar(OPEN_UTC - timedelta(days=1) + timedelta(minutes=n), 100.0) for n in range(0, 390, 15)]
    at = CLOSE_UTC + timedelta(hours=2)         # 18:00 ET — no US session at all
    while at <= CLOSE_UTC + timedelta(hours=3):
        bars.append(_bar(at, 102.0))
        at += timedelta(minutes=1)
    result = run_backtest(
        crypto, {"ADA/USD": bars}, dict(RISK_DEFAULTS, max_total_positions=50),
        starting_cash=2000, spread_pct=0, market="crypto",
        sim_start=CLOSE_UTC + timedelta(hours=2), sim_end=CLOSE_UTC + timedelta(hours=4),
    )
    assert len(_entries(result)) == 1
