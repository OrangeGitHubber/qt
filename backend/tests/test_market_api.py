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
    # Crypto "today" is the ROLLING 24h change from hourly bars (the scanner's
    # definition), not the snapshot's UTC-day bar: 100k open 24h ago → 105k now.
    hourly = {
        "BTC/USD": [
            {"t": "2026-07-29T00:00:00Z", "o": 100000.0, "c": 100500.0, "v": 1, "vw": 100500.0},
            {"t": "2026-07-29T23:00:00Z", "o": 104000.0, "c": 105000.0, "v": 1, "vw": 105000.0},
        ]
    }
    with (
        patch.object(AlpacaClient, "crypto_snapshots", new=AsyncMock(return_value=snapshot)),
        patch.object(AlpacaClient, "historical_bars", new=AsyncMock(return_value=hourly)),
    ):
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
    # The sparkline now uses daily bars (historical_bars, oldest-first) rather
    # than the thin intraday feed, so most stocks actually get a trend line.
    bars = {"AAPL": [{"t": "2026-07-10T00:00:00Z", "c": 201.0}, {"t": "2026-07-11T00:00:00Z", "c": 202.0}]}
    with patch.object(AlpacaClient, "historical_bars", new=AsyncMock(return_value=bars)):
        body = client.get("/api/market/bars", params={"symbol": "aapl"}).json()
    assert body["symbol"] == "AAPL"
    # oldest-first, as returned
    assert [b["c"] for b in body["bars"]] == [201.0, 202.0]


def test_the_scanner_reports_how_many_it_cut(monkeypatch):
    """A full list hides everything below top_n, so "why isn't my mover here?"
    had no answer on screen. `passed` is the count BEFORE the cut."""
    import asyncio
    from unittest.mock import AsyncMock, patch

    from qt.broker.alpaca import AlpacaClient
    from qt.services import scanner

    # 15 pairs all up 2-16%, comfortably past the crypto floors.
    pairs = [{"symbol": f"C{i}/USD"} for i in range(15)]
    stats = {f"C{i}/USD": (10.0, 2.0 + i, 1_000_000.0) for i in range(15)}
    cfg = scanner.DEFAULT_CONFIG | {"top_n": 10}

    with patch.object(AlpacaClient, "crypto_assets", new=AsyncMock(return_value=pairs)), \
         patch.object(scanner, "crypto_rolling_stats", new=AsyncMock(return_value=stats)):
        rows, meta = asyncio.run(scanner.scan_crypto(AlpacaClient("k", "s"), cfg))

    assert len(rows) == 10, "top_n should still cut the list"
    assert meta["passed"] == 15, "the count before the cut must be reported"
    assert meta["scanned"] == 15
    # The cut is by size of move, so the weakest movers are the ones missing.
    assert rows[0]["symbol"] == "C14/USD" and rows[-1]["symbol"] == "C5/USD"


def test_the_scanner_names_which_filter_rejected_what():
    """Werner's case, reproduced. ADA at $0.1734 vanished from a 3-row crypto
    list with a $0.50 min price set. Nothing was broken — but the panel gave no
    way to reach that conclusion, and a short list reads as a broken scanner."""
    import asyncio
    from unittest.mock import AsyncMock, patch

    from qt.broker.alpaca import AlpacaClient
    from qt.services import scanner

    # His actual numbers: three coins over $0.50, ADA and DOGE well under it.
    stats = {
        "DOT/USD": (0.7789, 2.58, 1_000.0),
        "USDT/USD": (0.9989, 0.04, 1_000.0),
        "USDC/USD": (0.9998, 0.03, 18_000.0),
        "ADA/USD": (0.1734, 3.23, 50_000.0),
        "DOGE/USD": (0.07, 4.10, 90_000.0),
    }
    pairs = [{"symbol": s} for s in stats]
    cfg = scanner.DEFAULT_CONFIG | {"top_n": 10}
    cfg["crypto"] = dict(cfg["crypto"]) | {"min_price": 0.5, "min_change_pct": 0.0, "min_dollar_volume": 1000}

    with patch.object(AlpacaClient, "crypto_assets", new=AsyncMock(return_value=pairs)), \
         patch.object(scanner, "crypto_rolling_stats", new=AsyncMock(return_value=stats)):
        rows, meta = asyncio.run(scanner.scan_crypto(AlpacaClient("k", "s"), cfg))

    assert {r["symbol"] for r in rows} == {"DOT/USD", "USDT/USD", "USDC/USD"}
    # The two biggest movers are missing, and the panel can now say why.
    assert meta["rejected"] == {"below your $0.5 min price": 2}


def test_a_rejection_reason_names_the_setting_that_caused_it():
    """Each reason has to be traceable to a field in Filters — 'excluded' alone
    would send you hunting through four numbers to find which one."""
    from qt.services import scanner

    f = {"min_price": 1.0, "max_price": 100.0, "min_change_pct": 2.0, "min_dollar_volume": 5000}
    assert "min price" in scanner._reject_reason(f, [], 0.5, 9.0, 9e6, "X")
    assert "max price" in scanner._reject_reason(f, [], 500.0, 9.0, 9e6, "X")
    assert "min gain" in scanner._reject_reason(f, [], 50.0, 0.5, 9e6, "X")
    assert "min $ volume" in scanner._reject_reason(f, [], 50.0, 9.0, 10.0, "X")
    assert "exclude list" in scanner._reject_reason(f, ["X"], 50.0, 9.0, 9e6, "X")
    assert scanner._reject_reason(f, [], 50.0, 9.0, 9e6, "X") is None
