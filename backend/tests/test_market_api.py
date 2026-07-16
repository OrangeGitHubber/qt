from unittest.mock import AsyncMock, patch

import pytest

from qt import security
from qt.broker.alpaca import SECRET_KEY_ID, SECRET_KEY_SECRET, AlpacaClient
from qt.db import session_scope


@pytest.fixture()
def configured(client):
    """Store fake keys so require_client passes."""
    with session_scope() as session:
        security.set_secret(session, SECRET_KEY_ID, "k")
        security.set_secret(session, SECRET_KEY_SECRET, "s")
    yield
    with session_scope() as session:
        security.delete_secret(session, SECRET_KEY_ID)
        security.delete_secret(session, SECRET_KEY_SECRET)


def test_scanner_requires_setup(client):
    assert client.get("/api/scanner").status_code == 409


def test_scanner_config_roundtrip(client):
    cfg = client.get("/api/scanner/config").json()
    assert cfg["top_n"] == 10

    cfg["top_n"] = 5
    cfg["exclude_symbols"] = [" tsla ", "gme", "GME"]
    updated = client.put("/api/scanner/config", json=cfg).json()
    assert updated["top_n"] == 5
    assert updated["exclude_symbols"] == ["GME", "TSLA"]

    assert client.get("/api/scanner/config").json()["top_n"] == 5


def test_scanner_config_rejects_bad_values(client):
    cfg = client.get("/api/scanner/config").json()
    cfg["top_n"] = 0
    assert client.put("/api/scanner/config", json=cfg).status_code == 422


def test_watchlist_crud(client, configured):
    snapshot = {"BTC/USD": {"dailyBar": {"c": 105000.0}, "prevDailyBar": {"c": 100000.0}}}
    with patch.object(AlpacaClient, "crypto_snapshots", new=AsyncMock(return_value=snapshot)):
        resp = client.post("/api/watchlist", json={"symbol": "btc/usd", "asset_class": "crypto"})
        assert resp.status_code == 200
        assert resp.json()["symbol"] == "BTC/USD"

        # duplicate rejected
        assert client.post("/api/watchlist", json={"symbol": "BTC/USD", "asset_class": "crypto"}).status_code == 409

        with patch.object(AlpacaClient, "stock_snapshots", new=AsyncMock(return_value={})):
            body = client.get("/api/watchlist").json()
    assert body["items"][0]["symbol"] == "BTC/USD"
    assert body["items"][0]["change_pct"] == 5.0

    assert client.delete("/api/watchlist/crypto/BTC/USD").status_code == 200
    assert client.delete("/api/watchlist/crypto/BTC/USD").status_code == 404


def test_watchlist_rejects_unknown_symbol(client, configured):
    with patch.object(AlpacaClient, "stock_snapshots", new=AsyncMock(return_value={})):
        resp = client.post("/api/watchlist", json={"symbol": "NOTREAL", "asset_class": "stock"})
    assert resp.status_code == 404


def test_watchlist_add_trusts_local_directory_without_api_call(client, configured):
    """A symbol in the directory is added without any quote round-trip — so it
    works even when market data is down."""
    from qt.models import Asset

    with session_scope() as s:
        s.add(Asset(symbol="MSFT", asset_class="stock", name="Microsoft Corp", exchange="NASDAQ", fractionable=True))

    boom = AsyncMock(side_effect=AssertionError("must not call Alpaca for a known symbol"))
    with patch.object(AlpacaClient, "stock_snapshots", new=boom):
        resp = client.post("/api/watchlist", json={"symbol": "msft", "asset_class": "stock"})
    assert resp.status_code == 200
    assert resp.json()["symbol"] == "MSFT"

    client.delete("/api/watchlist/stock/MSFT")
    with session_scope() as s:
        s.query(Asset).delete()


def test_bars_endpoint(client, configured):
    bars = {"AAPL": [{"t": "2026-07-11T15:00:00Z", "c": 202.0, "v": 10}, {"t": "2026-07-11T14:45:00Z", "c": 201.0, "v": 12}]}
    with patch.object(AlpacaClient, "stock_bars", new=AsyncMock(return_value=bars)):
        body = client.get("/api/market/bars", params={"symbol": "aapl"}).json()
    assert body["symbol"] == "AAPL"
    # flipped to oldest-first for charting
    assert [b["c"] for b in body["bars"]] == [201.0, 202.0]
