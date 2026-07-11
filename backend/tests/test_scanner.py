from unittest.mock import AsyncMock, patch

import pytest

from qt.broker.alpaca import AlpacaClient
from qt.services import scanner

MOVERS = {
    "gainers": [
        {"symbol": "GOOD", "percent_change": 8.5, "change": 2.0, "price": 25.5},
        {"symbol": "PENNY", "percent_change": 40.0, "change": 0.2, "price": 0.7},
        {"symbol": "THIN", "percent_change": 6.0, "change": 1.0, "price": 18.0},
        {"symbol": "MEH", "percent_change": 0.5, "change": 0.05, "price": 10.0},
        {"symbol": "BANNED", "percent_change": 9.0, "change": 1.0, "price": 12.0},
    ],
    "losers": [],
}

STOCK_SNAPSHOTS = {
    "GOOD": {"dailyBar": {"c": 25.5, "v": 2_000_000, "vw": 25.0}},   # $50M volume
    "PENNY": {"dailyBar": {"c": 0.7, "v": 90_000_000, "vw": 0.65}},
    "THIN": {"dailyBar": {"c": 18.0, "v": 10_000, "vw": 18.0}},      # $180k volume
    "MEH": {"dailyBar": {"c": 10.0, "v": 5_000_000, "vw": 10.0}},
    "BANNED": {"dailyBar": {"c": 12.0, "v": 9_000_000, "vw": 12.0}},
}

CRYPTO_ASSETS = [
    {"symbol": "BTC/USD", "tradable": True},
    {"symbol": "DOGE/USD", "tradable": True},
]

CRYPTO_SNAPSHOTS = {
    "BTC/USD": {
        "dailyBar": {"c": 105_000.0, "v": 500.0, "vw": 104_000.0},
        "prevDailyBar": {"c": 100_000.0},
    },
    "DOGE/USD": {
        "dailyBar": {"c": 0.20, "v": 1_000_000.0, "vw": 0.20},
        "prevDailyBar": {"c": 0.21},  # down on the day
    },
}


def _client() -> AlpacaClient:
    return AlpacaClient(key_id="k", key_secret="s")


@pytest.fixture(autouse=True)
def _fresh_cache():
    scanner.invalidate_cache()
    yield
    scanner.invalidate_cache()


async def test_scan_stocks_applies_all_filters():
    cfg = dict(scanner.DEFAULT_CONFIG, exclude_symbols=["banned"], min_dollar_volume=1_000_000)
    with (
        patch.object(AlpacaClient, "stock_movers", new=AsyncMock(return_value=MOVERS)),
        patch.object(AlpacaClient, "stock_snapshots", new=AsyncMock(return_value=STOCK_SNAPSHOTS)),
    ):
        rows = await scanner.scan_stocks(_client(), cfg)
    symbols = [r["symbol"] for r in rows]
    assert "GOOD" in symbols
    assert "PENNY" not in symbols   # under min_price
    assert "THIN" not in symbols    # under dollar-volume floor
    assert "MEH" not in symbols     # under min_change_pct
    assert "BANNED" not in symbols  # excluded (case-insensitive)


async def test_scan_stocks_sorted_and_capped():
    cfg = dict(scanner.DEFAULT_CONFIG, top_n=1, min_dollar_volume=0, min_price=0, min_change_pct=0)
    with (
        patch.object(AlpacaClient, "stock_movers", new=AsyncMock(return_value=MOVERS)),
        patch.object(AlpacaClient, "stock_snapshots", new=AsyncMock(return_value=STOCK_SNAPSHOTS)),
    ):
        rows = await scanner.scan_stocks(_client(), cfg)
    assert len(rows) == 1
    assert rows[0]["symbol"] == "PENNY"  # biggest gainer when filters allow


async def test_scan_crypto_computes_change_and_filters_losers():
    cfg = dict(scanner.DEFAULT_CONFIG, min_dollar_volume=0)
    with (
        patch.object(AlpacaClient, "crypto_assets", new=AsyncMock(return_value=CRYPTO_ASSETS)),
        patch.object(AlpacaClient, "crypto_snapshots", new=AsyncMock(return_value=CRYPTO_SNAPSHOTS)),
    ):
        rows = await scanner.scan_crypto(_client(), cfg)
    assert [r["symbol"] for r in rows] == ["BTC/USD"]
    assert rows[0]["change_pct"] == 5.0


async def test_scan_reports_errors_instead_of_crashing(db_session):
    with (
        patch.object(AlpacaClient, "stock_movers", new=AsyncMock(side_effect=RuntimeError("boom"))),
        patch.object(AlpacaClient, "crypto_assets", new=AsyncMock(return_value=[])),
        patch.object(AlpacaClient, "crypto_snapshots", new=AsyncMock(return_value={})),
    ):
        result = await scanner.scan(db_session, _client())
    assert result["stocks"] == []
    assert any("Stock scan failed" in e for e in result["errors"])
