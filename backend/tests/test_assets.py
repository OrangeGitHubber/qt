from unittest.mock import patch

from qt.broker.alpaca import AlpacaClient
from qt.db import session_scope
from qt.models import Asset
from qt.services import assets

EQUITIES = [
    {"symbol": "NVDA", "name": "NVIDIA Corporation", "exchange": "NASDAQ", "tradable": True, "fractionable": True},
    {"symbol": "NVDL", "name": "GraniteShares 2x Long NVDA Daily ETF", "exchange": "NASDAQ", "tradable": True, "fractionable": False},
    {"symbol": "AMD", "name": "Advanced Micro Devices, Inc.", "exchange": "NASDAQ", "tradable": True, "fractionable": True},
    {"symbol": "GONE", "name": "Delisted Corp", "exchange": "NYSE", "tradable": False, "fractionable": False},
]
CRYPTOS = [
    {"symbol": "BTC/USD", "name": "Bitcoin / US Dollar", "exchange": "CRYPTO", "tradable": True, "fractionable": True},
]


async def _sync():
    async def fake_list(self, alpaca_class):
        rows = EQUITIES if alpaca_class == "us_equity" else CRYPTOS
        return [r for r in rows if r.get("tradable")]

    with patch.object(AlpacaClient, "list_assets", fake_list):
        with session_scope() as s:
            return await assets.sync(s, AlpacaClient(key_id="k", key_secret="s"))


async def test_sync_stores_tradable_only():
    counts = await _sync()
    assert counts == {"stock": 3, "crypto": 1}  # GONE excluded (not tradable)
    with session_scope() as s:
        assert s.get(Asset, ("NVDA", "stock")).name == "NVIDIA Corporation"
        assert s.get(Asset, ("GONE", "stock")) is None
        assert s.get(Asset, ("BTC/USD", "crypto")).name == "Bitcoin / US Dollar"
        s.query(Asset).delete()


async def test_sync_removes_delisted_and_is_idempotent():
    await _sync()
    with session_scope() as s:
        s.add(Asset(symbol="OLD", asset_class="stock", name="Gone Inc", exchange="NYSE", fractionable=False))
    await _sync()  # second sync should prune OLD and not duplicate
    with session_scope() as s:
        assert s.get(Asset, ("OLD", "stock")) is None
        assert s.query(Asset).filter(Asset.asset_class == "stock").count() == 3
        s.query(Asset).delete()


async def test_search_by_ticker_and_name():
    await _sync()
    with session_scope() as s:
        # exact ticker ranks first, ahead of an ETF whose NAME contains NVDA
        rows = assets.search(s, "nvda", "stock")
        assert rows[0]["symbol"] == "NVDA"
        assert "NVDL" in [r["symbol"] for r in rows]

        # company-name search works
        rows = assets.search(s, "nvidia", "stock")
        assert rows[0]["symbol"] == "NVDA"

        rows = assets.search(s, "micro", "stock")
        assert rows[0]["symbol"] == "AMD"

        # crypto by name
        rows = assets.search(s, "bitcoin", "crypto")
        assert rows[0]["symbol"] == "BTC/USD"

        # asset-class filter keeps stocks out of crypto results
        assert assets.search(s, "nvidia", "crypto") == []

        # empty query returns nothing rather than the whole exchange
        assert assets.search(s, "  ", "stock") == []
        s.query(Asset).delete()


async def test_search_endpoint_and_status(client):
    await _sync()
    body = client.get("/api/assets/search", params={"q": "nvid", "asset_class": "stock"}).json()
    assert body[0]["symbol"] == "NVDA"
    assert body[0]["name"] == "NVIDIA Corporation"
    assert body[0]["fractionable"] is True

    st = client.get("/api/assets/status").json()
    assert st["stocks"] == 3 and st["crypto"] == 1
    assert st["stale"] is False
    with session_scope() as s:
        s.query(Asset).delete()


def test_status_empty_directory_is_stale(client):
    st = client.get("/api/assets/status").json()
    assert st["count"] == 0
    assert st["stale"] is True
