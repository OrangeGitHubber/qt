"""Audit (2026-08-03): the optimizer's bar fetch never got the BASELINE warm-up
the backtest has had for a while.

Every bar needs a day-gain reference before it can be judged at all — crypto
against the close ~24h back, stocks against the previous session — and
`backtest._prepare` leaves change_pct None without one, after which run_backtest
skips the bar in silence. `qt.api.backtest.replay()` fixes that by opening the
REPLAYED series `BASELINE_WARMUP_DAYS` before the window while the DAILY series
reaches back over the indicator lookback. The optimizer had neither split: it
used one flat `WARMUP_DAYS` gated on `_needs_warmup`, so a plain intraday search
got no prefix at all, and its mixed branch started the 15-minute series exactly
at `window_start`.

Driven through `_run_search` with the bar fetch and the search itself stubbed, so
what is asserted is the REQUEST the optimizer makes — which is the thing that was
wrong.
"""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from qt.api import optimizer as optimizer_api
from qt.api.backtest import BASELINE_WARMUP_DAYS
from qt.services import barfetch, optimizer as optimizer_svc

CRYPTO_PARAMS = {
    "entry": {"min_day_gain_pct": 1.0, "require_above_vwap": False,
              "entry_window_start": None, "entry_window_end": None},
    "exit": {"trailing_stop_pct": 5.0, "stop_loss_pct": 4.0, "take_profit_pct": 0.0,
             "max_holding_hours": 0, "flatten_before_close": False,
             "exit_below_vwap": False},
}


MACD_PARAMS = {
    "entry": dict(CRYPTO_PARAMS["entry"], require_macd_bullish=True),
    "exit": CRYPTO_PARAMS["exit"],
}


def _strategy(asset_class="crypto", params=None) -> dict:
    return {
        "asset_class": asset_class,
        "swing_mode": True,
        "sizing_usd": 1000.0,
        "sleeve_usd": 5000.0,
        "max_positions": 2,
        "params": params or CRYPTO_PARAMS,
    }


class _Recorder:
    """Captures every (timeframe, start) the search asked the bar layer for."""

    def __init__(self):
        self.calls: list[tuple[str, datetime]] = []

    async def __call__(self, client, symbols, asset_class, timeframe, start_iso, *a, **kw):
        self.calls.append((timeframe, datetime.fromisoformat(start_iso.replace("Z", "+00:00"))))
        return {s: [{"t": start_iso, "c": 100.0, "h": 100.0, "l": 100.0, "v": 1, "vw": 100.0}]
                for s in symbols}

    def start_of(self, timeframe: str) -> datetime:
        for tf, start in self.calls:
            if tf == timeframe:
                return start
        raise AssertionError(f"no {timeframe} fetch was made; got {[c[0] for c in self.calls]}")


def _drive(monkeypatch, *, timeframe: str, mixed: bool, days: int = 60,
           strategy: dict | None = None) -> tuple[_Recorder, dict]:
    rec = _Recorder()
    monkeypatch.setattr(barfetch, "fetch_bars", rec)
    seen: dict = {}

    def fake_optimize(*a, **kw):
        seen.update(kw)
        return {"tested_combinations": 1}

    monkeypatch.setattr(optimizer_svc, "optimize", fake_optimize)
    optimizer_api._progress.error = None
    optimizer_api._progress.result = None
    asyncio.run(optimizer_api._run_search(
        object(), strategy or _strategy(), {}, ["AAA/USD"], (strategy or _strategy())["asset_class"],
        timeframe, days, 5, 5000, 0.1, mixed=mixed,
    ))
    assert optimizer_api._progress.error is None, optimizer_api._progress.error
    return rec, seen


def _window_start(days: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days)


@pytest.mark.parametrize("timeframe,mixed", [("15Min", False), ("15Min", True)])
def test_the_replayed_series_opens_before_the_window(monkeypatch, timeframe, mixed):
    """THE REGRESSION. The intraday series must start at least the asset class's
    baseline days before the window, or its first day has no day-gain reference
    and every bar of it is silently unusable."""
    days = 60
    rec, _ = _drive(monkeypatch, timeframe=timeframe, mixed=mixed, days=days)
    intraday_start = rec.start_of("15Min")
    lead = (_window_start(days) - intraday_start).total_seconds() / 86400
    assert lead >= BASELINE_WARMUP_DAYS["crypto"] - 0.01, (
        f"the 15-minute series opened only {lead:.2f} days before the window"
    )


def test_the_daily_series_still_reaches_back_over_the_indicator_lookback(monkeypatch):
    """The other half of the split: the baseline prefix must not have SHORTENED
    the daily series, which is what the indicators are computed from."""
    days = 60
    rec, _ = _drive(
        monkeypatch, timeframe="15Min", mixed=True, days=days,
        strategy=_strategy(params=MACD_PARAMS),  # a strategy that HAS a daily indicator
    )
    daily_start = rec.start_of("1Day")
    lead = (_window_start(days) - daily_start).total_seconds() / 86400
    assert lead > BASELINE_WARMUP_DAYS["crypto"] + 30, (
        f"the daily series only reached back {lead:.1f} days — too short for a daily indicator"
    )


def test_the_search_is_told_where_the_window_actually_starts(monkeypatch):
    """`sim_start` is what keeps the baseline days untradeable AND what makes
    split_in_out_of_sample measure the 70/30 boundary over the window rather than
    over window+prefix. Without it the prefix is both traded and counted."""
    days = 60
    _rec, seen = _drive(monkeypatch, timeframe="15Min", mixed=False, days=days)
    assert seen.get("sim_start") is not None
    drift = abs((seen["sim_start"] - _window_start(days)).total_seconds())
    assert drift < 120, "sim_start is not the window start"


def test_a_daily_search_keeps_its_deep_prefix(monkeypatch):
    """Anti-regression on the branch that was already right: a 1Day search reads
    its OWN bars as the indicator source, so it must keep fetching from the deep
    warm-up rather than being cut back to the baseline days."""
    days = 60
    rec, _ = _drive(
        monkeypatch, timeframe="1Day", mixed=False, days=days,
        strategy=_strategy(params=MACD_PARAMS),
    )
    lead = (_window_start(days) - rec.start_of("1Day")).total_seconds() / 86400
    assert lead > BASELINE_WARMUP_DAYS["crypto"] + 30
