"""The regime filter is a stock-entry safety rail, so its data fetch must be
robust: Alpaca returns only the current day's daily bar unless an explicit
`start` window is passed, which would leave regime permanently 'unknown' and
block every stock strategy. These tests pin the fix."""

import asyncio

from qt.services import regime


class FakeClient:
    def __init__(self, spy_closes):
        self._closes = spy_closes
        self.calls = []

    async def stock_bars(self, symbols, timeframe="15Min", limit=64, start=None):
        self.calls.append({"symbols": symbols, "timeframe": timeframe, "limit": limit, "start": start})
        return {"SPY": [{"c": c} for c in self._closes]}  # newest-first, like sort=desc


def test_regime_requests_a_history_window_not_just_today():
    """Regression: the fetch must pass a `start`, or Alpaca returns 1 daily bar
    and regime is stuck 'unknown' forever, blocking all stock entries."""
    regime.invalidate_cache()
    client = FakeClient([500.0] * 210)
    asyncio.run(regime.regime_status(client))
    assert client.calls, "regime never fetched SPY bars"
    assert client.calls[0]["start"], "regime must pass a start window, else only today's bar returns"
    assert client.calls[0]["timeframe"] == "1Day"


def test_regime_bull_when_spy_above_200d_ma():
    regime.invalidate_cache()
    # 210 closes: latest 505 sits above the 200-day mean of ~500.
    client = FakeClient([505.0] + [500.0] * 209)
    out = asyncio.run(regime.regime_status(client))
    assert out["ok"] is True and out["insufficient_data"] is False
    assert out["spy_close"] == 505.0 and out["sma200"] < out["spy_close"]


def test_regime_caution_when_spy_below_200d_ma():
    regime.invalidate_cache()
    client = FakeClient([400.0] + [500.0] * 209)  # latest far below the MA
    out = asyncio.run(regime.regime_status(client))
    assert out["ok"] is False and out["insufficient_data"] is False


def test_regime_fails_closed_on_insufficient_history():
    regime.invalidate_cache()
    client = FakeClient([500.0])  # only one bar available
    out = asyncio.run(regime.regime_status(client))
    assert out["ok"] is False and out["insufficient_data"] is True
