"""Stablecoins are never momentum candidates.

USDC/USD came back from the live scanner as a PASSING candidate on 2026-08-02:
+0.03% "day gain", $4,930 of 24h volume. A dollar-pegged token cannot trend, so
any strategy with a low enough gain gate would buy a dollar with a dollar and
burn a position slot doing it. These tests pin the skip, its named reason, and —
the easy thing to get wrong — that gold-pegged PAXG is NOT caught by it.
"""

import copy
from unittest.mock import AsyncMock, patch

import pytest

from qt.broker.alpaca import AlpacaClient
from qt.services import scanner


def _cfg(*, exclude=None) -> dict:
    """Crypto floors wide open, so the ONLY thing that can reject a pair here is
    the stablecoin rule (or the user's exclude list)."""
    cfg = copy.deepcopy(scanner.DEFAULT_CONFIG)
    cfg["exclude_symbols"] = exclude or []
    cfg["crypto"].update(min_price=0.0, min_change_pct=0.0, min_dollar_volume=0)
    return cfg


def _hourly_bars(oldest_open: float, newest_close: float, vw: float, n: int = 24, v: float = 1000.0) -> list:
    bars = [{"t": f"2026-08-01T{23 - i:02d}:00:00Z", "o": 0.0, "c": 0.0, "v": v, "vw": vw} for i in range(n)]
    bars[0]["c"] = newest_close
    bars[-1]["o"] = oldest_open
    return bars


# Every pair is given a REAL, positive move so nothing can be rejected by the
# gain floor by accident — a stablecoin that vanishes here vanished on purpose.
# (Peg noise would be ~+0.03%; +2% is deliberately generous.)
BARS = {
    "USDC/USD": _hourly_bars(oldest_open=1.00, newest_close=1.02, vw=1.0),
    "USDT/USD": _hourly_bars(oldest_open=1.00, newest_close=1.02, vw=1.0),
    "DAI/USD": _hourly_bars(oldest_open=1.00, newest_close=1.02, vw=1.0),
    "PAXG/USD": _hourly_bars(oldest_open=3_000.0, newest_close=3_060.0, vw=3_000.0),
    "ETH/USD": _hourly_bars(oldest_open=3_000.0, newest_close=3_060.0, vw=3_000.0),
}
ASSETS = [{"symbol": s, "tradable": True} for s in BARS]


def _client() -> AlpacaClient:
    return AlpacaClient(key_id="k", key_secret="s")


@pytest.fixture(autouse=True)
def _fresh_cache():
    scanner.invalidate_cache()
    yield
    scanner.invalidate_cache()


async def _scan(cfg) -> tuple[list[dict], dict]:
    with (
        patch.object(AlpacaClient, "crypto_assets", new=AsyncMock(return_value=ASSETS)),
        patch.object(AlpacaClient, "historical_bars", new=AsyncMock(return_value=BARS)),
    ):
        return await scanner.scan_crypto(_client(), cfg)


async def test_stablecoins_are_skipped_with_their_own_reason():
    rows, meta = await _scan(_cfg())
    symbols = [r["symbol"] for r in rows]
    for pegged in ("USDC/USD", "USDT/USD", "DAI/USD"):
        assert pegged not in symbols
    # A DISTINCT tally line, not folded into "on your exclude list" or (since a
    # real stablecoin barely moves) hidden under the min-gain floor.
    assert meta["rejected"].get(scanner.STABLECOIN_REASON) == 3
    assert "stablecoin" in scanner.STABLECOIN_REASON
    assert "on your exclude list" not in meta["rejected"]


async def test_gold_pegged_paxg_still_passes():
    """PAXG tracks the GOLD price, not a dollar — it genuinely moves, so it is a
    real momentum candidate. Catching it in the stablecoin net (it is a Paxos
    token one letter from USDP) would silently delete a tradable name."""
    rows, _ = await _scan(_cfg())
    assert "PAXG/USD" in [r["symbol"] for r in rows]
    assert scanner.is_stablecoin("PAXG/USD") is False


async def test_an_ordinary_pair_is_untouched():
    rows, meta = await _scan(_cfg())
    eth = next(r for r in rows if r["symbol"] == "ETH/USD")
    assert eth["change_pct"] == 2.0
    assert meta["passed"] == 2   # ETH and PAXG; the three pegged names are gone


async def test_the_users_exclude_list_still_works():
    """The manual list is unchanged: it still rejects, still case-insensitively,
    and still under its own reason."""
    rows, meta = await _scan(_cfg(exclude=["eth/usd"]))
    assert "ETH/USD" not in [r["symbol"] for r in rows]
    assert meta["rejected"].get("on your exclude list") == 1
    assert [r["symbol"] for r in rows] == ["PAXG/USD"]


def test_peg_noise_is_blamed_on_the_peg_not_on_your_gain_floor():
    """The live 2026-08-02 numbers, at the DEFAULT 1% min gain. USDC's +0.03%
    also fails that floor, so the order of the checks decides what the panel
    says. "below your 1% min gain" would invite Werner to lower the floor —
    which is exactly the setting that let a stablecoin through in the first
    place. The permanent reason has to win."""
    f = dict(scanner.CRYPTO_DEFAULTS)
    assert f["min_change_pct"] == 1.0, "this test is about the default gain floor"
    reason = scanner._reject_reason(f, [], price=0.9998, change_pct=0.03, dollar_volume=4_930.0, symbol="USDC/USD")
    assert reason == scanner.STABLECOIN_REASON
    # Same numbers on a non-pegged coin: the gain floor is still doing its job.
    assert "min gain" in scanner._reject_reason(f, [], 0.9998, 0.03, 4_930.0, "ADA/USD")


def test_the_base_asset_is_matched_not_the_quote():
    """Every crypto pair here is quoted in USD; matching the quote would wipe out
    the whole universe. Stock tickers are never pair-shaped, so they can't be
    caught by the list either."""
    assert scanner.is_stablecoin("USDC/USD") is True
    assert scanner.is_stablecoin("BTC/USD") is False
    assert scanner.is_stablecoin("usdt/usd") is True   # case-insensitive
    assert scanner.is_stablecoin("USDC") is False      # a bare ticker is not a pair
