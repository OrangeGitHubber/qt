"""Audit (2026-08-03) of the OPTIMIZER's two silent disagreements with the
backtester it is supposed to be searching with.

  1. IT MODELLED ZERO FEES. `optimize` never passed `fee_pct`, so run_backtest's
     0.0 default applied — while both single-strategy backtest paths charge the
     asset class's real rate (qt.api.backtest.DEFAULT_FEE_PCT: crypto 0.25% a
     side, ~0.5% a round trip). A fee-free search prefers higher-frequency
     settings, because extra entries cost nothing; the Backtest page then scored
     the very same window worse. The two tools disagreed by construction and the
     optimizer was always the flattering one.

  2. IT HAD NO BASELINE WARM-UP. The intraday fetch opened exactly at
     `window_start`, so the window's first bars had no day-gain reference,
     `_prepare` left change_pct None, and run_backtest skipped them without a
     word — the search was blind on day one of every window. And with no
     `sim_start` the in/out boundary was measured across window+warm-up rather
     than across the window, so the "70/30" split was neither.
"""

from datetime import datetime, timedelta

from qt.services import optimizer

BASE = {
    "asset_class": "crypto",
    "swing_mode": True,
    "sizing_usd": 1000.0,
    "sleeve_usd": 5000.0,
    "max_positions": 2,
    "params": {
        "entry": {"min_day_gain_pct": 3.0, "require_above_vwap": False,
                  "entry_window_start": None, "entry_window_end": None},
        "exit": {"trailing_stop_pct": 5.0, "stop_loss_pct": 4.0, "take_profit_pct": 0.0,
                 "max_holding_hours": 0, "flatten_before_close": False,
                 "exit_below_vwap": False},
    },
}


def _bars(n: int, start: str = "2026-03-01T00:00:00Z") -> list[dict]:
    t0 = datetime.fromisoformat(start.replace("Z", "+00:00"))
    return [
        {"t": (t0 + timedelta(days=i)).strftime("%Y-%m-%dT%H:%M:%SZ"),
         "c": 100.0 + i, "h": 100.0 + i, "l": 100.0 + i, "v": 1000, "vw": 100.0 + i}
        for i in range(n)
    ]


class FeeFake:
    """Records the fee each slice was run with. Its signature accepts `fee_pct`
    only as a keyword with a default, so it also proves the kwarg is ABSENT when
    no rate was named (a fake with the plain signature must stay callable)."""

    def __init__(self):
        self.fees: list[float | None] = []
        self.sim_starts: list = []

    def __call__(
        self, strategy, bars_by_symbol, risk, *,
        starting_cash=5000.0, spread_pct=0.1, market="stock", eligible_by_day=None,
        sim_start=None, fee_pct="ABSENT",
    ):
        self.fees.append(fee_pct)
        self.sim_starts.append(sim_start)
        return {
            "net_pnl_pct": 5.0, "trades": 10, "win_rate": 55.0,
            "return_on_deployed_pct": 7.5, "max_drawdown_pct": 3.0,
            "open_positions": [], "hold_benchmark": [0.0, 2.0],
            "hold_benchmark_label": "TEST",
        }


def _run(**kw):
    fake = FeeFake()
    result = optimizer.optimize(
        BASE, {"AAA/USD": _bars(60)}, {}, iterations=6, seed=5, backtest_fn=fake, **kw
    )
    return result, fake


def test_every_slice_of_the_search_pays_the_fee_it_was_given():
    """Both slices, not just one: an in-sample score chosen fee-free and an
    out-of-sample verdict validated with fees would not be comparable, and the
    verdict is the only number the optimizer treats as real."""
    _result, fake = _run(fee_pct=0.25)
    assert fake.fees, "the fake was never called"
    assert set(fake.fees) == {0.25}


def test_the_search_reports_the_rate_it_charged():
    """Read off the result, so "the optimizer models no fees" can never be true
    again without the result saying so."""
    result, _fake = _run(fee_pct=0.25)
    assert result["fee_pct_per_side"] == 0.25


def test_naming_no_rate_keeps_the_kwarg_off_the_call():
    """Backward compatibility, deliberately: a caller (or an injected fake) with
    the pre-fee signature must keep working, and the result must say plainly that
    this search was fee-free rather than implying a rate it never charged."""
    result, fake = _run()
    assert set(fake.fees) == {"ABSENT"}
    assert result["fee_pct_per_side"] is None


def test_a_zero_rate_is_still_a_stated_rate():
    """Stocks really are commission-free, and that is a different claim from
    "nobody asked" — the stock search must be able to say it charged 0."""
    result, fake = _run(fee_pct=0.0)
    assert set(fake.fees) == {0.0}
    assert result["fee_pct_per_side"] == 0.0


def test_the_two_slices_are_still_the_two_slices():
    """Anti-vacuity guard for the tests above: they would pass just as well if the
    search had collapsed to a single run. It has not — an in-sample slice and an
    out-of-sample slice really both ran."""
    window_start = datetime.fromisoformat("2026-03-01T00:00:00+00:00")
    _result, fake = _run(fee_pct=0.25, sim_start=window_start)
    assert len({s for s in fake.sim_starts}) == 2
