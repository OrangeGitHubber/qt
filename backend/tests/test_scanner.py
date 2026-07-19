import copy
from unittest.mock import AsyncMock, patch

import pytest

from qt.broker.alpaca import AlpacaClient
from qt.services import scanner


def _cfg(*, top_n=10, exclude=None, stock=None, crypto=None) -> dict:
    """Build a nested scanner config for tests."""
    cfg = copy.deepcopy(scanner.DEFAULT_CONFIG)
    cfg["top_n"] = top_n
    cfg["exclude_symbols"] = exclude or []
    if stock:
        cfg["stocks"].update(stock)
    if crypto:
        cfg["crypto"].update(crypto)
    return cfg

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


def _hourly_bars(oldest_open: float, newest_close: float, vw: float, n: int = 24, v: float = 10.0) -> list:
    """Newest-first hourly bars (as the client returns them, sort=desc). Only
    the newest close and the oldest open drive the 24h change; every bar's v/vw
    feeds the $ volume sum."""
    bars = [{"t": f"2026-07-18T{23 - i:02d}:00:00Z", "o": 0.0, "c": 0.0, "v": v, "vw": vw} for i in range(n)]
    bars[0]["c"] = newest_close   # most recent
    bars[-1]["o"] = oldest_open   # ~24h ago
    return bars


CRYPTO_BARS = {
    "BTC/USD": _hourly_bars(oldest_open=100_000.0, newest_close=105_000.0, vw=104_000.0),   # +5%
    "DOGE/USD": _hourly_bars(oldest_open=0.21, newest_close=0.20, vw=0.20),                 # down on the day
}


def _client() -> AlpacaClient:
    return AlpacaClient(key_id="k", key_secret="s")


@pytest.fixture(autouse=True)
def _fresh_cache():
    scanner.invalidate_cache()
    yield
    scanner.invalidate_cache()


async def test_scan_stocks_applies_all_filters():
    cfg = _cfg(exclude=["banned"], stock={"min_dollar_volume": 1_000_000})
    with (
        patch.object(AlpacaClient, "stock_movers", new=AsyncMock(return_value=MOVERS)),
        patch.object(AlpacaClient, "stock_snapshots", new=AsyncMock(return_value=STOCK_SNAPSHOTS)),
    ):
        rows, meta = await scanner.scan_stocks(_client(), cfg)
    symbols = [r["symbol"] for r in rows]
    assert "GOOD" in symbols
    assert "PENNY" not in symbols   # under min_price
    assert "THIN" not in symbols    # under dollar-volume floor
    assert "MEH" not in symbols     # under min_change_pct
    assert "BANNED" not in symbols  # excluded (case-insensitive)
    # Diagnostics report the strongest mover seen regardless of filters,
    # with its price and $ volume so the UI can name the exact blocking floor.
    assert meta["scanned"] == 5
    assert meta["best_symbol"] == "PENNY"
    assert meta["best_change_pct"] == 40.0
    assert meta["best_price"] == 0.7
    assert meta["best_dollar_volume"] == round(90_000_000 * 0.65)


async def test_scan_stocks_sorted_and_capped():
    cfg = _cfg(top_n=1, stock={"min_dollar_volume": 0, "min_price": 0, "min_change_pct": 0})
    with (
        patch.object(AlpacaClient, "stock_movers", new=AsyncMock(return_value=MOVERS)),
        patch.object(AlpacaClient, "stock_snapshots", new=AsyncMock(return_value=STOCK_SNAPSHOTS)),
    ):
        rows, _ = await scanner.scan_stocks(_client(), cfg)
    assert len(rows) == 1
    assert rows[0]["symbol"] == "PENNY"  # biggest gainer when filters allow


async def test_scan_crypto_rolling_24h_change_and_filters_losers():
    cfg = _cfg(crypto={"min_dollar_volume": 0})
    with (
        patch.object(AlpacaClient, "crypto_assets", new=AsyncMock(return_value=CRYPTO_ASSETS)),
        patch.object(AlpacaClient, "historical_bars", new=AsyncMock(return_value=CRYPTO_BARS)),
    ):
        rows, meta = await scanner.scan_crypto(_client(), cfg)
    # BTC is +5% over the rolling 24h window; DOGE is down and filtered out.
    assert [r["symbol"] for r in rows] == ["BTC/USD"]
    assert rows[0]["change_pct"] == 5.0
    assert rows[0]["price"] == 105_000.0
    # DOGE still counts as scanned; BTC (+5%) is the strongest.
    assert meta["scanned"] == 2
    assert meta["best_symbol"] == "BTC/USD"
    assert meta["best_price"] == 105_000.0


def test_rolling_24h_handles_empty_and_missing():
    assert scanner._rolling_24h([]) is None
    assert scanner._rolling_24h([{"t": "x", "c": 0, "o": 0}]) is None  # no usable price


async def test_scan_reports_errors_instead_of_crashing(db_session):
    with (
        patch.object(AlpacaClient, "clock", new=AsyncMock(return_value={"is_open": True})),
        patch.object(AlpacaClient, "stock_movers", new=AsyncMock(side_effect=RuntimeError("boom"))),
        patch.object(AlpacaClient, "crypto_assets", new=AsyncMock(return_value=[])),
        patch.object(AlpacaClient, "historical_bars", new=AsyncMock(return_value={})),
    ):
        result = await scanner.scan(db_session, _client())
    assert result["stocks"] == []
    assert any("Stock scan failed" in e for e in result["errors"])
    assert result["market_open"] is True


def test_normalize_migrates_flat_shape():
    """An old flat config copies its single floors onto both asset classes."""
    cfg = scanner._normalize(
        {
            "stocks_enabled": True,
            "crypto_enabled": False,
            "top_n": 7,
            "min_price": 3.0,
            "min_change_pct": 4.0,
            "min_dollar_volume": 2_000_000,
            "exclude_symbols": ["FOO"],
        }
    )
    assert cfg["top_n"] == 7
    assert cfg["exclude_symbols"] == ["FOO"]
    for cls in ("stocks", "crypto"):
        assert cfg[cls]["min_price"] == 3.0
        assert cfg[cls]["min_change_pct"] == 4.0
        assert cfg[cls]["min_dollar_volume"] == 2_000_000
    assert cfg["stocks"]["enabled"] is True
    assert cfg["crypto"]["enabled"] is False


def test_normalize_defaults_split_by_class():
    """A fresh config gives stocks and crypto their own (different) defaults."""
    cfg = scanner._normalize({})
    assert cfg["stocks"]["min_dollar_volume"] == 5_000_000
    assert cfg["crypto"]["min_dollar_volume"] == 1_000_000
    assert cfg["stocks"]["min_price"] == 1.0
    assert cfg["crypto"]["min_price"] == 0.0


def test_normalize_deep_merges_partial_nested():
    """A nested config missing some keys still fills class defaults."""
    cfg = scanner._normalize({"crypto": {"min_dollar_volume": 250_000}})
    assert cfg["crypto"]["min_dollar_volume"] == 250_000
    assert cfg["crypto"]["min_change_pct"] == 1.0   # default preserved
    assert cfg["stocks"]["min_dollar_volume"] == 5_000_000
