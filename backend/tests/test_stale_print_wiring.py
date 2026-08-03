"""The stale-print guard is only real if the candidate builders actually carry
`last_trade_at` from the snapshot into the Candidate.

A coverage audit found that replacing every `last_trade_at=...` at every
construction site with `None` left the whole suite green: the parser had tests,
the rule had tests, and the WIRE BETWEEN THEM had none, so the feature could be
reduced to a no-op in silence. These are that wire.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from qt.broker.alpaca import AlpacaClient
from qt.services import engine
from qt.services.engine import CRYPTO_STALE_TRADE_MINUTES, evaluate_entry

SYMBOL = "STALE/USD"
NOW = datetime.now(timezone.utc)
STALE_AT = NOW - timedelta(minutes=CRYPTO_STALE_TRADE_MINUTES * 2)


def _snap(traded_at: datetime) -> dict:
    return {
        "latestTrade": {"p": 1.3749, "t": traded_at.isoformat().replace("+00:00", "Z")},
        "dailyBar": {"c": 1.3749, "vw": 1.37, "o": 1.36},
        "prevDailyBar": {"c": 1.36},
    }


def _client() -> AlpacaClient:
    return AlpacaClient(key_id="k", key_secret="s")


def _assert_carried(cand) -> None:
    assert cand is not None, "no candidate was built at all — test setup is wrong"
    assert cand.last_trade_at is not None, (
        "the snapshot's latestTrade.t never reached the Candidate, so the stale-print "
        "guard can never fire from this builder"
    )
    # Not just "something": the actual instant, or a builder could satisfy the
    # check above by stamping now() and the guard would never see a stale pair.
    assert abs((cand.last_trade_at - STALE_AT).total_seconds()) < 2


async def test_symbol_candidates_carry_the_print_time():
    with (
        patch.object(AlpacaClient, "crypto_snapshots", new=AsyncMock(return_value={SYMBOL: _snap(STALE_AT)})),
        patch.object(engine.scanner, "crypto_rolling_stats", new=AsyncMock(return_value={SYMBOL: (1.3749, 5.0, 9_000.0)})),
    ):
        cands = await engine._symbol_candidates(_client(), "crypto", [SYMBOL])
    _assert_carried(next((c for c in cands if c.symbol == SYMBOL), None))


async def test_ranked_candidates_carry_the_print_time():
    """The ranked path builds through _pool_metrics and a separate parallel map,
    so it can lose the timestamp independently of the others."""
    with patch.object(AlpacaClient, "crypto_snapshots", new=AsyncMock(return_value={SYMBOL: _snap(STALE_AT)})):
        with patch.object(engine.scanner, "crypto_rolling_stats", new=AsyncMock(return_value={SYMBOL: (1.3749, 5.0, 9_000.0)})):
            cands = await engine._ranked_candidates(_client(), "crypto", [SYMBOL], "momentum_today", 10)
    _assert_carried(next((c for c in cands if c.symbol == SYMBOL), None))


async def test_pool_metrics_returns_the_print_time_map():
    with patch.object(AlpacaClient, "crypto_snapshots", new=AsyncMock(return_value={SYMBOL: _snap(STALE_AT)})):
        with patch.object(engine.scanner, "crypto_rolling_stats", new=AsyncMock(return_value={SYMBOL: (1.3749, 5.0, 9_000.0)})):
            _metrics, _prices, _vwaps, trade_at = await engine._pool_metrics(
                _client(), "crypto", [SYMBOL], "momentum_today"
            )
    assert trade_at.get(SYMBOL) is not None
    assert abs((trade_at[SYMBOL] - STALE_AT).total_seconds()) < 2


async def test_a_stale_pair_built_this_way_is_actually_rejected():
    """End to end: snapshot -> builder -> entry rules. This is the claim the
    feature makes, and the only test that fails if ANY link in the chain breaks."""
    with (
        patch.object(AlpacaClient, "crypto_snapshots", new=AsyncMock(return_value={SYMBOL: _snap(STALE_AT)})),
        patch.object(engine.scanner, "crypto_rolling_stats", new=AsyncMock(return_value={SYMBOL: (1.3749, 5.0, 9_000.0)})),
    ):
        cands = await engine._symbol_candidates(_client(), "crypto", [SYMBOL])
    params = {"entry": {"min_day_gain_pct": 0, "require_above_vwap": False}, "exit": {}}
    ok, reason = evaluate_entry(params, cands[0], datetime.now(timezone.utc))
    assert not ok
    assert "no trades on this pair" in reason
