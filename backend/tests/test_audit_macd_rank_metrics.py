"""Ranking by MACD — as a level, and as a rate of change.

WHY, measured on Werner's "Favorites" strategy (2026-08-05). It ranked by RSI
descending, which by construction puts the MOST OVERBOUGHT names at the top of
the pool, and then required a 1% up-day + above VWAP + MACD already bullish to
enter. Every filter selected "has already run". 17 trades, 11.8% win rate,
profit factor 0.48 — and 6 of the 17 exited on the HARD stop, meaning price fell
below entry without ever setting a high water mark. A third of entries went
straight down. ranking.py's own docstring had warned that RSI ranking wants an
overbought exit paired with it; there wasn't one.

So: rank on MACD instead. Two metrics, because "biggest convergence" and "fastest
rising" are different questions and the difference is the whole point.

  macd_strength — histogram as a % of price. A LEVEL.
  macd_slope    — how fast that histogram is rising, as a % of price. LEADING.

The normalisation is not cosmetic and is tested as its own claim: raw MACD scales
with the share price. On 2026-05-22 DELL's histogram read +13.44 and AAL's +0.029
— a 460x gap between a $400 stock and a $14 one that says nothing about momentum.
Ranking the raw number sorts a mixed list by price.
"""

import pytest

from qt.services import stats
from qt.services.ranking import RANK_METRICS, rank_symbols


def _bars(closes: list[float]) -> list[dict]:
    return [{"c": c} for c in closes]


def _ramp(n: int, start: float, step: float) -> list[float]:
    return [start + step * i for i in range(n)]


# Long enough for 12/26/9 plus the slope span to be defined.
FLAT = [100.0] * 60


def test_both_metrics_are_registered():
    assert "macd_strength" in RANK_METRICS
    assert "macd_slope" in RANK_METRICS


def test_strength_is_positive_when_momentum_is_bullish():
    rising = _bars(FLAT + _ramp(20, 100.0, 2.0))
    assert stats.macd_strength_pct(rising) > 0


def test_strength_is_negative_when_momentum_is_bearish():
    falling = _bars(FLAT + _ramp(20, 100.0, -2.0))
    assert stats.macd_strength_pct(falling) < 0


def test_strength_is_a_percentage_of_price_not_raw_macd():
    """THE claim that makes this rankable across symbols. Two series with the
    SAME shape at different price levels must score the same — a $400 stock and
    a $14 stock moving identically are equally strong.

    Raw MACD would differ by the price ratio, which is how ranking an
    unnormalised histogram silently becomes ranking by share price."""
    cheap = _bars([c for c in FLAT] + _ramp(20, 100.0, 2.0))
    dear = _bars([c * 40 for c in FLAT] + _ramp(20, 4000.0, 80.0))

    assert stats.macd_strength_pct(cheap) == pytest.approx(
        stats.macd_strength_pct(dear), rel=1e-6
    )
    # ...and the raw histograms really are 40x apart, so the test isn't vacuous.
    raw_cheap = stats.macd([b["c"] for b in cheap])[2]
    raw_dear = stats.macd([b["c"] for b in dear])[2]
    assert raw_dear == pytest.approx(raw_cheap * 40, rel=1e-6)


def test_slope_is_positive_while_momentum_is_still_building():
    accelerating = _bars(FLAT + _ramp(12, 100.0, 3.0))
    assert stats.macd_slope_pct(accelerating) > 0


def test_slope_turns_negative_before_strength_does():
    """The reason both exist, and the answer to 'is this just RSI again?'.

    After a rally stalls, the histogram is still HIGH (strength stays positive —
    the trade looks great) while it has already started shrinking (slope negative
    — momentum is leaving). Rank on the level and you buy the top; rank on the
    slope and you don't."""
    stalling = _bars(FLAT + _ramp(22, 100.0, 3.0) + [166.0] * 2)
    assert stats.macd_strength_pct(stalling) > 0, "still bullish by the level"
    assert stats.macd_slope_pct(stalling) < 0, "but already fading"


def test_the_two_metrics_can_rank_a_pool_differently():
    """End to end through rank_symbols: the two metrics must be able to DISAGREE
    about the same pool, or macd_slope is just macd_strength with extra steps.

    The fixture is the claim and it had to be found rather than assumed: my first
    attempt stalled the leader for six bars, which decayed its histogram so far
    that the builder won on BOTH metrics and proved nothing. Two flat bars is the
    window where STALL is still the stronger name by the level while already the
    weaker one by direction."""
    stalled = _bars(FLAT + _ramp(22, 100.0, 3.0) + [166.0] * 2)
    building = _bars(FLAT + _ramp(6, 100.0, 2.5))
    metrics = {
        "STALL": {
            "macd_strength": stats.macd_strength_pct(stalled),
            "macd_slope": stats.macd_slope_pct(stalled),
        },
        "BUILD": {
            "macd_strength": stats.macd_strength_pct(building),
            "macd_slope": stats.macd_slope_pct(building),
        },
    }
    assert rank_symbols(metrics, "macd_strength", 1)[0][0] == "STALL"
    assert rank_symbols(metrics, "macd_slope", 1)[0][0] == "BUILD"


@pytest.mark.parametrize("fn", [stats.macd_strength_pct, stats.macd_slope_pct])
def test_too_little_history_is_none_not_a_guess(fn):
    """Same contract as every other metric here: a value computed over a shorter
    window than advertised is worse than no value, because rank_symbols DROPS a
    None and would happily rank a fabricated one."""
    assert fn(_bars([100.0] * 10)) is None
    assert fn([]) is None


def test_a_zero_price_does_not_divide_by_zero():
    assert stats.macd_strength_pct(_bars(FLAT + _ramp(20, 100.0, 2.0) + [0.0])) is None


def test_the_slope_span_must_be_positive():
    rising = _bars(FLAT + _ramp(20, 100.0, 2.0))
    assert stats.macd_slope_pct(rising, span=0) is None


def test_custom_periods_change_the_answer():
    """The strategy's own MACD periods reach the metric — otherwise a strategy
    with custom periods would rank on one MACD and gate entries on another."""
    rising = _bars(FLAT + _ramp(20, 100.0, 2.0))
    assert stats.macd_strength_pct(rising, 12, 26, 9) != stats.macd_strength_pct(
        rising, 5, 35, 5
    )


def test_the_shared_core_still_gives_the_old_macd_answer():
    """`macd` was refactored onto _macd_lines so there is ONE EMA chain. Its
    published triple must be unchanged: line, signal, and line-minus-signal."""
    closes = FLAT + _ramp(20, 100.0, 2.0)
    line, sig, hist = stats.macd(closes)
    assert hist == pytest.approx(line - sig, abs=1e-6)
    assert stats.macd_bullish(closes) is (line > sig)


def test_the_macd_rank_value_is_computed_once_per_symbol_day(monkeypatch):
    """PERFORMANCE, measured. The ranker runs on every BAR, and these metrics
    read only the daily prefix — no live price is injected, deliberately — so on
    hourly bars every day recomputed three full EMA passes over ~120 daily closes
    seven times per symbol for an identical answer.

    At 30 symbols it made macd_slope 4.1x the cost of momentum_today and turned
    optimizer runs on the Trend Follower template painfully slow. Memoised per
    (symbol, day): 2.9x faster, byte-identical trade list and equity curve.

    This asserts the CALL COUNT rather than the wall clock, because a timing
    assertion on a shared CI box is a flaky test pretending to be a guarantee."""
    from datetime import datetime, timedelta, timezone

    from qt.services import backtest as bt
    from qt.services.engine import RISK_DEFAULTS

    calls = {"n": 0}
    real = stats.macd_slope_pct

    def counted(*a, **k):
        calls["n"] += 1
        return real(*a, **k)

    monkeypatch.setattr(bt.stats, "macd_slope_pct", counted)

    utc = timezone.utc
    start = datetime(2025, 1, 2, 14, 30, tzinfo=utc)
    n_days, per_day, n_syms = 120, 7, 4

    def series(seed):
        out, price = [], 100.0 + seed
        for d in range(n_days):
            for h in range(per_day):
                t = start + timedelta(days=d, hours=h)
                price *= 1 + (((d * 7 + h * 3 + seed) % 11) - 5) / 400
                out.append({"t": t.strftime("%Y-%m-%dT%H:%M:%SZ"), "o": price,
                            "h": price * 1.004, "l": price * 0.996, "c": price,
                            "v": 1e6, "vw": price})
        return out

    syms = {f"S{i}": series(i) for i in range(n_syms)}
    daily = {s: [b for i, b in enumerate(v) if i % per_day == per_day - 1]
             for s, v in syms.items()}
    strategy = {
        "asset_class": "stock", "swing_mode": True, "sizing_usd": 500.0,
        "sleeve_usd": 5000.0, "max_positions": 4, "universe": "custom",
        "rank_enabled": True, "rank_by": "macd_slope", "top_n": 4,
        "params": {"entry": {"min_day_gain_pct": 1.0},
                   "exit": {"stop_loss_pct": 6.0, "trailing_stop_pct": 12.0}},
    }
    bt.run_backtest(
        strategy, syms, dict(RISK_DEFAULTS, max_total_positions=50),
        starting_cash=5000, spread_pct=0.1,
        sim_start=start + timedelta(days=40), daily_bars_by_symbol=daily,
    )

    # One per (symbol, traded day) at most. Without the memo it is one per BAR,
    # which is `per_day` times larger — so the ceiling below is comfortably
    # under the uncached count and comfortably above the cached one.
    ceiling = n_syms * n_days
    assert calls["n"] <= ceiling, f"{calls['n']} calls — the per-day memo is not working"
    assert calls["n"] > 0, "vacuous: the metric was never computed at all"
