"""The pure top-N ranking function — no broker, no DB."""

import pytest

from qt.services.ranking import RANK_METRICS, rank_symbols


def _metrics(**by_symbol):
    """Helper: {'AAA': (mom, ret, rs[, rs_vs_spy[, rsi]])} -> full metrics dict.

    The 4th (rs_vs_spy) and 5th (rsi) tuple elements are optional so the existing
    3-tuple callers keep working; they default to None."""
    return {
        sym: {
            "momentum_today": t[0],
            "return_30d": t[1],
            "relative_strength": t[2],
            "rs_vs_spy": t[3] if len(t) > 3 else None,
            "rsi": t[4] if len(t) > 4 else None,
        }
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
    m = _metrics(A=(1.0, 2.0, 3.0, 4.0, 5.0), B=(6.0, 7.0, 8.0, 9.0, 10.0))
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
    with patch.object(engine, "_pool_metrics", new=AsyncMock(return_value=(metrics, prices, {}, {}))):
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
    with patch.object(engine, "_pool_metrics", new=AsyncMock(return_value=(metrics, prices, {}, {}))):
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
