"""A stretch of the window with no bars must announce itself.

The equity day index is built from days that HAD bars, so a hole isn't a flat
line in the data — it's absent, and the chart draws one straight segment from the
last day before it to the first day after. That reads as "the strategy sat
still", which is the one thing it was definitely not doing:

  - Open positions keep their last seen price across the whole gap, so the mark
    is frozen and then lurches when real prices return. The cliff at the end of a
    flat stretch is the gap's entire move arriving at once, not a crash.
  - Stops cannot fire without bars. A trailing stop that would have closed the
    position mid-gap never gets the chance, so the run reports a loss the live
    engine would have cut.

Silence there is the worst outcome: a straight line looks like data.
"""

from qt.services.backtest import _bar_gaps


def _days(*days: str) -> list[str]:
    return list(days)


def test_a_continuous_crypto_window_reports_nothing():
    assert _bar_gaps(_days("2026-05-01", "2026-05-02", "2026-05-03"), "crypto") == []


def test_a_missing_crypto_day_is_a_gap():
    """Crypto trades every calendar day, so any skipped day is a real hole."""
    gaps = _bar_gaps(_days("2026-05-01", "2026-05-03"), "crypto")
    assert gaps == [{"after": "2026-05-01", "before": "2026-05-03", "days": 1}]


def test_the_six_week_hole_is_measured_not_just_flagged():
    """The shape that prompted this: weeks with nothing, then a vertical drop."""
    gaps = _bar_gaps(_days("2026-05-05", "2026-06-17"), "crypto")
    assert gaps == [{"after": "2026-05-05", "before": "2026-06-17", "days": 42}]


def test_a_stock_weekend_is_not_a_gap():
    """Friday to Monday is normal. Crying wolf on every weekend would train the
    warning to be ignored, which costs more than not having it."""
    assert _bar_gaps(_days("2026-05-01", "2026-05-04"), "stock") == []


def test_a_stock_long_weekend_is_not_a_gap():
    """Friday to Tuesday — a Monday holiday."""
    assert _bar_gaps(_days("2026-05-01", "2026-05-05"), "stock") == []


def test_a_stock_week_off_is_a_gap():
    """Beyond a long weekend there is no benign explanation."""
    gaps = _bar_gaps(_days("2026-05-01", "2026-05-11"), "stock")
    assert gaps == [{"after": "2026-05-01", "before": "2026-05-11", "days": 9}]


def test_every_hole_is_listed_not_just_the_first():
    """One gap is a data problem; three is a pattern, and the difference tells
    you whether the cache is short at the edges or full of holes."""
    gaps = _bar_gaps(
        _days("2026-05-01", "2026-05-03", "2026-05-04", "2026-05-20", "2026-05-21"), "crypto"
    )
    assert [g["days"] for g in gaps] == [1, 15]


def test_a_single_day_window_cannot_have_a_gap():
    assert _bar_gaps(_days("2026-05-01"), "crypto") == []
    assert _bar_gaps([], "crypto") == []


def test_a_real_replay_with_a_hole_reports_it_and_shows_the_stale_mark():
    """End to end, because the unit above proves the arithmetic and nothing else.

    This reproduces the reported shape exactly: a position is opened, the bars
    stop for six weeks, and resume far lower. The equity curve has no points
    inside the hole (so the chart draws one straight segment across it), and the
    first day back carries the entire move as a single step. Both are artefacts
    of missing data, and the run has to say so.
    """
    from qt.services.backtest import run_backtest
    from qt.services.engine import RISK_DEFAULTS

    strategy = {
        "asset_class": "crypto",
        "swing_mode": False,
        "sizing_usd": 1000.0,
        "sleeve_usd": 5000.0,
        "max_positions": 3,
        "params": {
            "entry": {"min_day_gain_pct": 3.0, "require_above_vwap": False,
                      "entry_window_start": None, "entry_window_end": None},
            "exit": {"trailing_stop_pct": 90.0, "stop_loss_pct": 95.0, "take_profit_pct": 0,
                     "max_holding_hours": 0, "flatten_before_close": False,
                     "exit_below_vwap": False},
        },
    }

    def bar(day: str, close: float) -> dict:
        return {"t": f"{day}T12:00:00Z", "o": close, "h": close, "l": close,
                "c": close, "v": 10000, "vw": close}

    series = [
        bar("2026-05-01", 100.0),
        bar("2026-05-02", 105.0),   # +5% — entry
        bar("2026-05-03", 106.0),
        bar("2026-05-04", 107.0),
        bar("2026-05-05", 108.0),
        # ---- six weeks with nothing at all ----
        bar("2026-06-17", 70.0),    # back, far lower
        bar("2026-06-18", 69.0),
    ]
    risk = dict(RISK_DEFAULTS, max_total_exposure_usd=1_000_000, max_daily_loss_usd=1_000_000)
    result = run_backtest(strategy, {"AAA": series}, risk, starting_cash=5000,
                          spread_pct=0, market="crypto")

    assert result["bar_gaps"] == [
        {"after": "2026-05-05", "before": "2026-06-17", "days": 42}
    ]
    # The hole really is absent from the curve — nothing to plot, hence the
    # straight line — and the drop lands entirely on the first day back.
    assert "2026-05-20" not in result["equity_days"]
    back = result["equity_days"].index("2026-06-17")
    step = result["equity"][back] - result["equity"][back - 1]
    assert step < -5, "the whole gap's move should arrive in one step"
