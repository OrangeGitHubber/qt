"""Stats tests on constructed bars whose correct answers are known by hand."""

from datetime import datetime, timedelta, timezone

import pytest

from qt.services import stats


def bars(closes: list[float], highs=None, lows=None, start_days_ago: int | None = None) -> list[dict]:
    """Daily bars, oldest-first, one per day ending today."""
    n = len(closes)
    first = datetime.now(timezone.utc) - timedelta(days=(start_days_ago if start_days_ago is not None else n - 1))
    out = []
    for i, close in enumerate(closes):
        out.append(
            {
                "t": (first + timedelta(days=i)).strftime("%Y-%m-%dT00:00:00Z"),
                "o": close,
                "h": (highs[i] if highs else close),
                "l": (lows[i] if lows else close),
                "c": close,
                "v": 1000,
            }
        )
    return out


def test_pct_change_over_uses_the_bar_from_that_many_days_ago():
    # 40 daily bars: price 100 throughout, then last bar 110
    series = bars([100.0] * 39 + [110.0])
    assert stats.pct_change_over(series, 30) == 10.0


def test_pct_change_over_returns_none_without_enough_history():
    series = bars([100.0] * 5 + [110.0])  # only ~6 days
    assert stats.pct_change_over(series, 30) is None


def test_pct_change_over_prefers_live_price_when_given():
    series = bars([100.0] * 40)
    assert stats.pct_change_over(series, 30, current_price=125.0) == 25.0


def test_atr_pct_on_a_steady_2_percent_range():
    # every day: high 102, low 100, close 101 → true range 2 on a 101 close
    closes = [101.0] * 20
    series = bars(closes, highs=[102.0] * 20, lows=[100.0] * 20)
    assert stats.atr_pct(series, 14) == pytest.approx(1.98, abs=0.02)


def test_atr_includes_overnight_gaps_not_just_intraday_range():
    # tiny intraday ranges, but each day gaps 10 from the prior close
    closes = [100.0, 110.0, 120.0, 130.0, 140.0, 150.0, 160.0, 170.0,
              180.0, 190.0, 200.0, 210.0, 220.0, 230.0, 240.0, 250.0]
    series = bars(closes, highs=[c + 0.5 for c in closes], lows=[c - 0.5 for c in closes])
    atr = stats.atr_pct(series, 14)
    # gap of 10 dwarfs the 1.0 intraday range → ATR ≈ 10.5, not ~1
    assert atr is not None and atr > 3


def test_atr_none_without_enough_bars():
    assert stats.atr_pct(bars([100.0] * 10), 14) is None


def test_sma_and_vs_sma():
    series = bars([100.0] * 200)
    assert stats.sma(series, 200) == 100.0
    assert stats.vs_sma_pct(series, 200, current_price=110.0) == 10.0
    assert stats.vs_sma_pct(series, 200, current_price=90.0) == -10.0


def test_vs_sma_none_when_history_short():
    assert stats.vs_sma_pct(bars([100.0] * 50), 200) is None


def test_compute_reports_none_rather_than_a_shorter_window():
    result = stats.compute(bars([100.0] * 20), current_price=105.0)
    assert result["change_30d_pct"] is None       # <30 days of history
    assert result["vs_sma200_pct"] is None        # <200 bars
    assert result["atr_pct"] is not None          # 14-day ATR is satisfiable
    assert result["bars_available"] == 20


def test_compute_full_history():
    series = bars([100.0] * 250)
    result = stats.compute(series, current_price=120.0)
    assert result["change_30d_pct"] == 20.0
    assert result["vs_sma200_pct"] == 20.0
    assert result["bars_available"] == 250
