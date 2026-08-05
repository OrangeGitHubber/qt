"""The pure top-N ranking function — no broker, no DB."""

import pytest

from qt.services.ranking import RANK_METRICS, rank_symbols


def _metrics(**by_symbol):
    """Helper: {'AAA': (mom, ret, rs[, rs_vs_spy[, rsi[, macd_str[, macd_slope]]]])}
    -> full metrics dict.

    Elements past the 3rd are optional so the existing 3-tuple callers keep
    working; they default to None. Positions are appended in RANK_METRICS order —
    test_all_metrics_are_rankable walks that tuple, so a metric registered
    without a slot here fails loudly rather than going untested."""
    keys = (
        "momentum_today", "return_30d", "relative_strength", "rs_vs_spy", "rsi",
        "macd_strength", "macd_slope",
    )
    return {
        sym: {k: (t[i] if len(t) > i else None) for i, k in enumerate(keys)}
        for sym, t in by_symbol.items()
    }


def test_ranks_descending_by_chosen_metric():
    m = _metrics(A=(1.0, None, None), B=(5.0, None, None), C=(3.0, None, None))
    assert rank_symbols(m, "momentum_today", 2) == [("B", 5.0), ("C", 3.0)]


def test_top_n_caps_result():
    m = _metrics(A=(1.0, 1, 1), B=(2.0, 2, 2), C=(3.0, 3, 3))
    assert [s for s, _ in rank_symbols(m, "momentum_today", 1)] == ["C"]
    assert len(rank_symbols(m, "momentum_today", 10)) == 3  # never more than exist


def test_missing_metric_drops_symbol():
    # C has no return_30d → excluded when ranking by it, even though it leads on momentum.
    m = _metrics(A=(1.0, 4.0, None), B=(2.0, 9.0, None), C=(99.0, None, None))
    ranked = rank_symbols(m, "return_30d", 5)
    assert [s for s, _ in ranked] == ["B", "A"]


def test_ties_break_on_symbol_ascending():
    m = _metrics(BBB=(5.0, None, None), AAA=(5.0, None, None), CCC=(5.0, None, None))
    assert [s for s, _ in rank_symbols(m, "momentum_today", 3)] == ["AAA", "BBB", "CCC"]


def test_relative_strength_can_be_negative():
    m = _metrics(A=(0, 0, -3.0), B=(0, 0, -1.0), C=(0, 0, -10.0))
    assert [s for s, _ in rank_symbols(m, "relative_strength", 2)] == ["B", "A"]


def test_top_n_zero_or_negative_returns_empty():
    m = _metrics(A=(1.0, None, None))
    assert rank_symbols(m, "momentum_today", 0) == []
    assert rank_symbols(m, "momentum_today", -3) == []


def test_unknown_metric_raises():
    with pytest.raises(ValueError):
        rank_symbols(_metrics(A=(1.0, None, None)), "dividend_yield", 3)


def test_all_metrics_are_rankable():
    m = _metrics(A=(1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0), B=(8.0, 9.0, 10.0, 11.0, 12.0, 13.0, 14.0))
    for metric in RANK_METRICS:
        ranked = rank_symbols(m, metric, 1)
        assert ranked == [("B", pytest.approx(m["B"][metric]))]


def test_rs_vs_spy_ranks_higher_outperformance_first():
    # rs_vs_spy = member return minus SPY return; the biggest OUT-performer wins.
    m = _metrics(A=(0, 0, 0, 2.0), B=(0, 0, 0, 9.5), C=(0, 0, 0, -4.0))
    assert [s for s, _ in rank_symbols(m, "rs_vs_spy", 3)] == ["B", "A", "C"]


def test_rs_vs_spy_can_be_negative_and_drops_missing():
    # A trails SPY (negative but present → ranked); C has no value → dropped.
    m = _metrics(A=(0, 0, 0, -1.5), B=(0, 0, 0, -0.5), C=(0, 0, 0, None))
    assert [s for s, _ in rank_symbols(m, "rs_vs_spy", 5)] == ["B", "A"]


# ---------------------------------------------------------------------------
# Rank provenance on the trade.
#
# "up 2.17% today, MACD bullish" read identically whether the buy was the
# strongest name in the basket or the twenty-fourth one — the last thing left
# after everything above it was already held or failed the rules. Those are
# different trades. The rank now travels with the candidate into the journal.
# ---------------------------------------------------------------------------

import asyncio  # noqa: E402
from unittest.mock import AsyncMock, patch  # noqa: E402

from qt.models import Strategy  # noqa: E402
from qt.services import engine  # noqa: E402


def _ranked(symbols_to_change: dict[str, float]):
    """Run _ranked_candidates over a pool with known momentum, mocking the
    snapshot layer so only the ranking + stamping is under test."""
    metrics = {s: {"momentum_today": v} for s, v in symbols_to_change.items()}
    prices = {s: 100.0 for s in symbols_to_change}
    with patch.object(engine, "_pool_metrics", new=AsyncMock(return_value=(metrics, prices, {}, {}, None))):
        return asyncio.run(
            engine._ranked_candidates(
                None, "stock", list(symbols_to_change), "momentum_today", len(symbols_to_change)
            )
        )


def test_candidates_are_stamped_best_first():
    cands = _ranked({"AAA": 1.0, "BBB": 9.0, "CCC": 5.0})
    assert [(c.symbol, c.rank) for c in cands] == [("BBB", 1), ("CCC", 2), ("AAA", 3)]
    assert {c.rank_of for c in cands} == {3}


def test_an_unpriced_symbol_does_not_renumber_the_rest():
    """A symbol dropped for want of a price must not promote everything below it
    — the rank has to mean its place in the RANKING, not its index in whatever
    survived."""
    metrics = {s: {"momentum_today": v} for s, v in {"AAA": 9.0, "BBB": 5.0, "CCC": 1.0}.items()}
    prices = {"AAA": 100.0, "CCC": 100.0}  # BBB (rank 2) has no price
    with patch.object(engine, "_pool_metrics", new=AsyncMock(return_value=(metrics, prices, {}, {}, None))):
        cands = asyncio.run(
            engine._ranked_candidates(None, "stock", ["AAA", "BBB", "CCC"], "momentum_today", 3)
        )
    assert [(c.symbol, c.rank) for c in cands] == [("AAA", 1), ("CCC", 3)]


def test_the_rank_note_names_the_position_and_the_metric():
    # Takes the metric NAME, not the Strategy row — the backtester writes the same
    # sentence from a plain config dict (see backtest._PoolRanker).
    cand = engine.Candidate(symbol="AVGO", asset_class="stock", price=397.8, change_pct=2.17,
                            rank=24, rank_of=25)
    assert engine._rank_note("momentum_today", cand) == ", ranked #24 of 25 by momentum today"


def test_an_unranked_universe_adds_nothing():
    """A scanner strategy has no top-N to place a symbol in — the reason string
    must be left exactly as it was."""
    cand = engine.Candidate(symbol="AVGO", asset_class="stock", price=397.8, change_pct=2.17)
    assert engine._rank_note("momentum_today", cand) == ""


async def test_a_crypto_pool_can_be_ranked_by_a_bar_based_metric():
    """_pool_metrics binds the qt.services.stats MODULE, and its crypto branch
    used to rebind that same name per symbol to a rolling-stats tuple. Python
    makes it a local for the whole function, so a CRYPTO pool ranked by
    return_30d / relative_strength / rsi reached `stats.pct_change_over` holding
    a tuple and raised AttributeError.

    That is not a bad ranking, it is a dead tick: the exception escapes
    _candidates_for (which only catches AlpacaError) and aborts the entire entry
    cycle for EVERY strategy, once a minute, for as long as one crypto basket is
    ranked that way. Stocks never hit it, which is why it survived.
    """
    from datetime import datetime, timedelta, timezone
    from unittest.mock import AsyncMock, patch

    from qt.broker.alpaca import AlpacaClient
    from qt.services import engine

    snap = {"latestTrade": {"p": 1.5}, "dailyBar": {"c": 1.5, "vw": 1.5}, "prevDailyBar": {"c": 1.4}}
    # 40 daily bars ending today, so the 30-day lookback actually reaches back.
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    bars = [
        {"c": 1.0, "h": 1.0, "l": 1.0, "o": 1.0,
         "t": (start + timedelta(days=i)).isoformat().replace("+00:00", "Z")}
        for i in range(40)
    ]
    with (
        patch.object(AlpacaClient, "crypto_snapshots", new=AsyncMock(return_value={"ZZZ/USD": snap})),
        patch.object(engine.scanner, "crypto_rolling_stats",
                     new=AsyncMock(return_value={"ZZZ/USD": (1.5, 7.0, 1_000.0)})),
        patch.object(AlpacaClient, "historical_bars", new=AsyncMock(return_value={"ZZZ/USD": bars})),
    ):
        metrics, prices, *_ = await engine._pool_metrics(
            AlpacaClient(key_id="k", key_secret="s"), "crypto", ["ZZZ/USD"], "return_30d"
        )

    assert prices["ZZZ/USD"] == 1.5
    assert metrics["ZZZ/USD"]["return_30d"] is not None, "the bar-based metric never got computed"
    # The rolling-24h change still reached the metric it belongs to.
    assert metrics["ZZZ/USD"]["momentum_today"] == 7.0
