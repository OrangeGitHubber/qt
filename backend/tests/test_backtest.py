"""Backtester tests on synthetic bars — deterministic price paths whose
correct trades are known by construction."""

from datetime import datetime, timedelta

import pytest

from qt.services.backtest import run_backtest
from qt.services.engine import RISK_DEFAULTS

STRATEGY = {
    "asset_class": "stock",
    "swing_mode": False,  # intraday so take-profit/stops act same-day
    "sizing_usd": 1000.0,
    "sleeve_usd": 5000.0,
    "max_positions": 3,
    "params": {
        "entry": {
            "min_day_gain_pct": 3.0,
            "require_above_vwap": False,
            "entry_window_start": None,
            "entry_window_end": None,
        },
        "exit": {
            "trailing_stop_pct": 5.0,
            "stop_loss_pct": 4.0,
            "take_profit_pct": 0,
            "max_holding_hours": 0,
            "flatten_before_close": False,
            "exit_below_vwap": False,
        },
    },
}

RISK = dict(RISK_DEFAULTS, max_total_exposure_usd=1_000_000, max_daily_loss_usd=1_000_000)


def bars_from(closes: list[float], start: str = "2026-05-04T14:00:00Z") -> list[dict]:
    """One bar per hour; day 1 establishes the previous close baseline."""
    t0 = datetime.fromisoformat(start.replace("Z", "+00:00"))
    out = []
    for i, close in enumerate(closes):
        ts = t0 + timedelta(hours=i)
        out.append({"t": ts.strftime("%Y-%m-%dT%H:%M:%SZ"), "c": close, "v": 1000, "vw": close})
    return out


def _spread_day(closes_day1: list[float], closes_day2: list[float]) -> list[dict]:
    """Two consecutive days of hourly bars (7 bars/day max keeps same ET day)."""
    day1 = bars_from(closes_day1, "2026-05-04T14:00:00Z")
    day2 = bars_from(closes_day2, "2026-05-05T14:00:00Z")
    return day1 + day2


def test_rise_then_trail_exit():
    # Day 1 closes at 100 (baseline). Day 2: +4% (entry), runs to 110, drops to
    # 102.5 → trailing stop fires, and at +2.5% day-gain the exit bar does NOT
    # re-qualify for entry.
    series = _spread_day([100, 100, 100], [104, 107, 110, 102.5, 102.5])
    result = run_backtest(STRATEGY, {"TEST": series}, RISK, starting_cash=5000, spread_pct=0)
    assert result["trades"] == 1
    trade = result["trade_list"][0]
    assert "trailing stop" in trade["exit_reason"]
    assert trade["entry_price"] == 104
    assert trade["exit_price"] == 102.5
    assert result["net_pnl"] == round((102.5 - 104) * 9, 2)
    assert result["win_rate"] == 0.0


def test_stop_loss_and_costs():
    # Entry at 104, collapse to 99 → stop-loss. Spread cost should worsen P&L.
    series = _spread_day([100, 100, 100], [104, 99, 99])
    no_cost = run_backtest(STRATEGY, {"TEST": series}, RISK, starting_cash=5000, spread_pct=0)
    with_cost = run_backtest(STRATEGY, {"TEST": series}, RISK, starting_cash=5000, spread_pct=0.2)
    assert no_cost["trades"] == 1
    assert "stop-loss" in no_cost["trade_list"][0]["exit_reason"]
    assert with_cost["net_pnl"] < no_cost["net_pnl"] < 0
    assert with_cost["max_drawdown_pct"] > 0


def test_no_entry_when_gain_too_small():
    series = _spread_day([100, 100, 100], [101, 101.5, 102])  # only +2% max
    result = run_backtest(STRATEGY, {"TEST": series}, RISK, starting_cash=5000, spread_pct=0)
    assert result["trades"] == 0
    assert result["net_pnl"] == 0
    # zero-trade runs must explain themselves
    diag = result["diagnosis"]
    assert diag["max_day_gain_pct"] == 2.0
    assert diag["days_reaching_min_gain"] == 0
    assert "day-gain threshold" in diag["summary"]
    assert "2.0%" in diag["summary"]


def test_diagnosis_identifies_vwap_as_blocker():
    # +4% gain but every close sits below the running VWAP → VWAP is the blocker
    strategy = {**STRATEGY, "params": {
        "entry": {**STRATEGY["params"]["entry"], "require_above_vwap": True},
        "exit": STRATEGY["params"]["exit"],
    }}
    day1 = bars_from([100, 100, 100], "2026-05-04T14:00:00Z")
    t0 = "2026-05-05T14:00:00Z"
    day2 = bars_from([104, 104, 104], t0)
    for b in day2:
        b["vw"] = 105.0  # VWAP above close all day
    result = run_backtest(strategy, {"TEST": day1 + day2}, RISK, starting_cash=5000, spread_pct=0)
    assert result["trades"] == 0
    diag = result["diagnosis"]
    assert diag["days_reaching_min_gain"] == 1
    assert diag["rejected_vwap"] > 0
    assert "VWAP" in diag["summary"]


def test_diagnosis_none_when_trades_exist():
    series = _spread_day([100, 100, 100], [104, 99, 99])
    result = run_backtest(STRATEGY, {"TEST": series}, RISK, starting_cash=5000, spread_pct=0)
    assert result["trades"] == 1
    assert result["diagnosis"]["summary"] is None


def test_rails_respected_in_backtest():
    # Two symbols both trigger; sleeve budget only fits one $1000 position + cash guard.
    strategy = dict(STRATEGY, sleeve_usd=1500.0)
    series_a = _spread_day([100, 100, 100], [104, 104.5, 105, 105, 105])
    series_b = _spread_day([50, 50, 50], [52, 52.2, 52.5, 52.5, 52.5])
    result = run_backtest(strategy, {"AAA": series_a, "BBB": series_b}, RISK, starting_cash=5000, spread_pct=0)
    # first entry consumes ~$1000 of the $1500 sleeve → second is blocked
    assert result["trades"] == 1
    assert result["trade_list"][0]["exit_reason"].startswith("end of backtest")


def test_forced_liquidation_marks_open_positions():
    series = _spread_day([100, 100, 100], [104, 106, 108])  # never hits an exit rule
    result = run_backtest(STRATEGY, {"TEST": series}, RISK, starting_cash=5000, spread_pct=0)
    assert result["trades"] == 1
    assert result["trade_list"][0]["exit_reason"] == "end of backtest (forced liquidation)"
    assert result["net_pnl"] > 0  # rode 104 → 108
    assert result["equity"][-1] > 0


def test_capital_deployment_metrics_expose_dilution():
    # $1000 per trade on a $50,000 account: a good trade return is a tiny
    # account return. Both numbers must be visible.
    strategy = dict(STRATEGY, sizing_usd=1000.0)
    series = _spread_day([100, 100, 100], [104, 99, 99])  # entry then stop-loss
    result = run_backtest(strategy, {"TEST": series}, RISK, starting_cash=50_000, spread_pct=0)

    assert result["trades"] == 1
    assert result["max_deployed_usd"] == pytest.approx(936.0, abs=1)  # 9 shares @ $104
    assert result["pct_capital_deployed"] == pytest.approx(1.87, abs=0.1)  # ~2% of the account
    # account return is tiny, but return on the money actually used is ~5x bigger
    assert abs(result["net_pnl_pct"]) < 0.1
    assert abs(result["return_on_deployed_pct"]) > 4
    assert 0 < result["time_in_market_pct"] <= 100


def test_no_trades_reports_zero_deployment_not_a_crash():
    series = _spread_day([100, 100, 100], [101, 101.5, 102])  # never qualifies
    result = run_backtest(STRATEGY, {"TEST": series}, RISK, starting_cash=5000, spread_pct=0)
    assert result["trades"] == 0
    assert result["max_deployed_usd"] == 0
    assert result["pct_capital_deployed"] == 0
    assert result["return_on_deployed_pct"] is None  # no division by zero
    assert result["time_in_market_pct"] == 0


def test_hold_benchmark_tracks_the_tested_symbol():
    # TEST goes 100 → 108 across the window; buy-and-hold should show +8%
    series = _spread_day([100, 100, 100], [104, 106, 108])
    result = run_backtest(STRATEGY, {"TEST": series}, RISK, starting_cash=5000, spread_pct=0)
    assert result["hold_benchmark_label"] == "TEST"
    assert result["hold_benchmark"][0] == 0.0  # day 1 baseline
    assert result["hold_benchmark"][-1] == pytest.approx(8.0, abs=0.01)


def test_hold_benchmark_equal_weights_multiple_symbols():
    up = _spread_day([100, 100, 100], [104, 106, 110])      # +10%
    flat = _spread_day([50, 50, 50], [50, 50, 50])          # 0%
    result = run_backtest(STRATEGY, {"UP": up, "FLAT": flat}, RISK, starting_cash=5000, spread_pct=0)
    assert result["hold_benchmark_label"] == "2 symbols (equal weight)"
    assert result["hold_benchmark"][-1] == pytest.approx(5.0, abs=0.01)  # (10 + 0) / 2


def test_trades_carry_et_day_strings_for_chart_markers():
    series = _spread_day([100, 100, 100], [104, 99, 99])
    result = run_backtest(STRATEGY, {"TEST": series}, RISK, starting_cash=5000, spread_pct=0)
    trade = result["trade_list"][0]
    assert trade["entry_day"] in result["equity_days"]
    assert trade["exit_day"] in result["equity_days"]


def test_equity_curve_daily_and_metrics_shape():
    series = _spread_day([100, 100, 100], [104, 99, 99])
    result = run_backtest(STRATEGY, {"TEST": series}, RISK, starting_cash=5000, spread_pct=0.1)
    assert len(result["equity_days"]) == len(result["equity"]) == 2
    for key in ("win_rate", "profit_factor", "avg_win", "avg_loss", "max_drawdown_pct"):
        assert key in result
