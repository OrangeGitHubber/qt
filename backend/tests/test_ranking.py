"""The pure top-N ranking function — no broker, no DB."""

import pytest

from qt.services.ranking import RANK_METRICS, rank_symbols


def _metrics(**by_symbol):
    """Helper: {'AAA': (mom, ret, rs)} -> full metrics dict."""
    return {
        sym: {"momentum_today": t[0], "return_30d": t[1], "relative_strength": t[2]}
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
    m = _metrics(A=(1.0, 2.0, 3.0), B=(4.0, 5.0, 6.0))
    for metric in RANK_METRICS:
        ranked = rank_symbols(m, metric, 1)
        assert ranked == [("B", pytest.approx(m["B"][metric]))]
