"""Watchlist stats + history endpoint, with Alpaca mocked."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from qt import security
from qt.api import market
from qt.broker.alpaca import SECRET_KEY_ID, SECRET_KEY_SECRET, AlpacaClient, AlpacaError
from qt.db import session_scope
from qt.models import Asset, WatchlistItem


def daily(closes: list[float]) -> list[dict]:
    first = datetime.now(timezone.utc) - timedelta(days=len(closes) - 1)
    return [
        {
            "t": (first + timedelta(days=i)).strftime("%Y-%m-%dT00:00:00Z"),
            "o": c, "h": c * 1.01, "l": c * 0.99, "c": c, "v": 1000,
        }
        for i, c in enumerate(closes)
    ]


SNAPSHOT = {"NVDA": {"dailyBar": {"c": 120.0}, "prevDailyBar": {"c": 100.0}}}


@pytest.fixture()
def watched(client):
    with session_scope() as s:
        security.set_secret(s, SECRET_KEY_ID, "k")
        security.set_secret(s, SECRET_KEY_SECRET, "s")
        s.add(Asset(symbol="NVDA", asset_class="stock", name="NVIDIA", exchange="NASDAQ", fractionable=True))
        s.add(WatchlistItem(symbol="NVDA", asset_class="stock"))
    market._daily_cache.update(day=None, bars={})
    yield
    with session_scope() as s:
        s.query(WatchlistItem).delete()
        s.query(Asset).delete()
        security.delete_secret(s, SECRET_KEY_ID)
        security.delete_secret(s, SECRET_KEY_SECRET)
    market._daily_cache.update(day=None, bars={})


def test_watchlist_includes_stats(client, watched):
    bars = {"NVDA": daily([100.0] * 250)}
    with (
        patch.object(AlpacaClient, "stock_snapshots", new=AsyncMock(return_value=SNAPSHOT)),
        patch.object(AlpacaClient, "crypto_snapshots", new=AsyncMock(return_value={})),
        patch.object(AlpacaClient, "historical_bars", new=AsyncMock(return_value=bars)),
    ):
        body = client.get("/api/watchlist").json()
    row = body["items"][0]
    assert row["change_pct"] == 20.0             # today, from the snapshot
    assert row["change_30d_pct"] == 20.0         # live 120 vs 100 a month ago
    assert row["vs_sma200_pct"] == 20.0          # 120 vs a flat 100 average
    assert row["atr_pct"] is not None
    assert row["bars_available"] == 250
    assert body["errors"] == []


def test_daily_bars_fetched_once_per_day(client, watched):
    bars = {"NVDA": daily([100.0] * 250)}
    fetch = AsyncMock(return_value=bars)
    with (
        patch.object(AlpacaClient, "stock_snapshots", new=AsyncMock(return_value=SNAPSHOT)),
        patch.object(AlpacaClient, "crypto_snapshots", new=AsyncMock(return_value={})),
        patch.object(AlpacaClient, "historical_bars", new=fetch),
    ):
        client.get("/api/watchlist")
        client.get("/api/watchlist")
        client.get("/api/watchlist")
    assert fetch.await_count == 1  # cached for the UTC day, not per poll


def test_history_failure_degrades_to_prices_only(client, watched):
    with (
        patch.object(AlpacaClient, "stock_snapshots", new=AsyncMock(return_value=SNAPSHOT)),
        patch.object(AlpacaClient, "crypto_snapshots", new=AsyncMock(return_value={})),
        patch.object(AlpacaClient, "historical_bars", new=AsyncMock(side_effect=AlpacaError(500, "boom"))),
    ):
        body = client.get("/api/watchlist").json()
    row = body["items"][0]
    assert row["price"] == 120.0                 # prices still shown
    assert row["change_30d_pct"] is None         # stats degrade quietly
    assert any("History fetch failed" in e for e in body["errors"])


def test_history_endpoint(client, watched):
    bars = {"NVDA": daily([100.0, 105.0, 110.0])}
    with patch.object(AlpacaClient, "historical_bars", new=AsyncMock(return_value=bars)):
        body = client.get("/api/market/history", params={"symbol": "nvda", "asset_class": "stock"}).json()
    assert body["symbol"] == "NVDA"
    assert [b["c"] for b in body["bars"]] == [100.0, 105.0, 110.0]
    assert "stats" in body


def test_history_404_when_no_data(client, watched):
    with patch.object(AlpacaClient, "historical_bars", new=AsyncMock(return_value={})):
        resp = client.get("/api/market/history", params={"symbol": "NOPE"})
    assert resp.status_code == 404
