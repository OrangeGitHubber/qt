"""Crypto must mean the same thing in the optimizer, the backtest and live.

Crypto has no session: it trades 24/7, its daily bars are stamped 00:00Z, and
"up X% today" means the last 24 HOURS rolling, not since an ET calendar
midnight. The live engine, the scanner, the optimizer and the portfolio
backtester all worked that way. The single-strategy backtest did not — it used
the stock convention unless the run happened to be mixed-resolution, so the
optimizer tuned min_day_gain_pct against one definition and the backtest then
graded it against another.
"""

from datetime import datetime, timedelta, timezone

import pytest

from qt.services.backtest import _day_fn, _prepare, run_backtest


def _crypto_bars(n: int = 300, start: datetime | None = None) -> list[dict]:
    """15-minute bars rising steadily across several ET midnights."""
    start = start or datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)
    out = []
    for i in range(n):
        ts = start + timedelta(minutes=15 * i)
        c = 100 * (1 + 0.0004 * i)
        out.append({"t": ts.strftime("%Y-%m-%dT%H:%M:%SZ"), "o": c, "h": c, "l": c, "c": c, "v": 10, "vw": c})
    return out


def _strategy(asset_class: str) -> dict:
    return {
        "asset_class": asset_class,
        "swing_mode": False,
        "sizing_usd": 1000,
        "sleeve_usd": 5000,
        "max_positions": 3,
        "params": {
            "entry": {"min_day_gain_pct": 3.0, "require_above_vwap": False,
                      "entry_window_start": None, "entry_window_end": None},
            "exit": {"trailing_stop_pct": 5, "stop_loss_pct": 4, "take_profit_pct": 0,
                     "max_holding_hours": 0, "flatten_before_close": False, "exit_below_vwap": False},
        },
    }


RISK = {"max_daily_loss_usd": 1e9, "max_daily_loss_pct": 100, "max_total_positions": 50,
        "max_total_exposure_usd": 1e9, "max_trades_per_day": 200,
        "cooldown_hours_after_loss": 0, "wash_sale_guard": "off", "leverage_enabled": False}


def test_the_two_day_gain_baselines_really_do_differ():
    """Guards the premise. If these ever agree, the tests below prove nothing."""
    bars = _crypto_bars()
    et = _prepare(bars, _day_fn("stock"), rolling_24h=False)[200]["change_pct"]
    utc = _prepare(bars, _day_fn("crypto"), rolling_24h=True)[200]["change_pct"]
    assert et is not None and utc is not None
    assert abs(utc - et) > 1.0, f"baselines too close to distinguish: {et=} {utc=}"


def test_a_crypto_backtest_uses_the_rolling_24h_baseline():
    """The bug: run_backtest tied rolling_24h to the day-bucketing flag, so a
    crypto run bucketed as 'stock' measured day-gain against an ET calendar day.
    It must follow the STRATEGY'S asset class, like run_portfolio_backtest does."""
    bars = {"BTC/USD": _crypto_bars()}
    crypto = run_backtest(_strategy("crypto"), bars, RISK, market="crypto", starting_cash=5000, spread_pct=0)
    # Forced onto the stock bucketing: the baseline must STILL be rolling, because
    # it is a crypto strategy. Same trades either way.
    forced = run_backtest(_strategy("crypto"), bars, RISK, market="stock", starting_cash=5000, spread_pct=0)
    assert crypto["trades"] == forced["trades"], "day-gain baseline changed with the bucketing flag"
    assert crypto["diagnosis"]["max_day_gain_pct"] == forced["diagnosis"]["max_day_gain_pct"]


def test_a_stock_backtest_is_untouched():
    """The counterpart: a stock strategy must keep the session-close baseline."""
    bars = {"NVDA": _crypto_bars()}
    a = run_backtest(_strategy("stock"), bars, RISK, market="stock", starting_cash=5000, spread_pct=0)
    b = _prepare(_crypto_bars(), _day_fn("stock"), rolling_24h=False)[200]["change_pct"]
    assert a["diagnosis"]["max_day_gain_pct"] is not None
    assert b is not None  # the stock path still measures vs the previous session close


@pytest.mark.parametrize("stamp,expected", [("2026-08-01T00:00:00Z", "2026-08-01")])
def test_a_crypto_daily_bar_is_filed_on_its_own_utc_day(stamp, expected):
    """Alpaca stamps crypto daily bars 00:00Z. Bucketed by ET that is 20:00 the
    PREVIOUS day, so every daily crypto bar landed one day early."""
    rows = _prepare([{"t": stamp, "o": 1, "h": 1, "l": 1, "c": 1, "v": 1, "vw": 1}],
                    _day_fn("crypto"), rolling_24h=True)
    assert rows[0]["day"] == expected


# ---- the LIVE watchlist branch must use the same rolling definition ----

async def test_a_crypto_watchlist_candidate_uses_the_rolling_24h_change():
    """The last consumer still on the calendar day.

    `_candidates_for`'s watchlist branch computed change from dailyBar vs
    prevDailyBar with no asset-class check — the 00:00-UTC calendar change, not
    the rolling 24h every other part of QT uses. It is reached by every
    "watchlist" strategy and ALWAYS by "both" (the API forces rank_enabled off
    there), so those strategies gated entries on a number the scanner, the
    watchlist page, the backtest and the optimizer all disagreed with: just
    after 00:00 UTC everything read ~0% and nothing could qualify.

    The two sources are made to disagree loudly here — rolling says +5%, the
    calendar day says +50% — so the assertion cannot pass by coincidence.
    """
    from unittest.mock import AsyncMock, patch

    from qt.broker.alpaca import AlpacaClient
    from qt.db import session_scope
    from qt.models import Strategy, WatchlistItem
    from qt.services import engine

    snap = {
        "latestTrade": {"p": 150.0, "t": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")},
        "dailyBar": {"c": 150.0, "vw": 149.0},
        "prevDailyBar": {"c": 100.0},  # calendar-day reading: +50%
    }
    with session_scope() as s:
        strat = Strategy(
            name="rolling watchlist", enabled=True, asset_class="crypto", universe="watchlist",
            preset="custom", params='{"entry":{},"exit":{"stop_loss_pct":4}}',
            sizing_usd=100, sleeve_usd=1000, max_positions=3, swing_mode=False, ignore_regime=False,
        )
        s.add(strat)
        s.add(WatchlistItem(symbol="ROLL/USD", asset_class="crypto"))
        s.flush()
        sid = strat.id

    try:
        with (
            patch.object(AlpacaClient, "crypto_snapshots", new=AsyncMock(return_value={"ROLL/USD": snap})),
            patch.object(engine.scanner, "crypto_rolling_stats",
                         new=AsyncMock(return_value={"ROLL/USD": (150.0, 5.0, 9_000.0)})),
        ):
            with session_scope() as s:
                strategy = s.get(Strategy, sid)
                cands, _ = await engine._candidates_for(
                    s, AlpacaClient(key_id="k", key_secret="s"), strategy, None
                )
        got = next(c for c in cands if c.symbol == "ROLL/USD")
        assert got.change_pct == 5.0, (
            "the watchlist branch used the 00:00-UTC calendar change instead of the "
            "rolling 24h figure every other consumer in QT uses"
        )
    finally:
        with session_scope() as s:
            s.query(WatchlistItem).filter(WatchlistItem.symbol == "ROLL/USD").delete()
            s.query(Strategy).filter(Strategy.id == sid).delete()
