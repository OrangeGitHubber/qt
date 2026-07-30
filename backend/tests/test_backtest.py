"""Backtester tests on synthetic bars — deterministic price paths whose
correct trades are known by construction."""

import copy
from datetime import datetime, timedelta

import pytest

from qt.services import backtest as bt
from qt.services import stats
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


def _entries(result: dict) -> int:
    """Entries made = closed round-trips + positions still open at the test end.
    Open positions are marked to market, not force-sold, so a strategy that
    entered and simply held to the end has trades == 0 but one open position."""
    return result["trades"] + len(result["open_positions"])


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


def test_eligible_by_day_gates_entries_to_the_days_movers():
    # Both AAA and BBB qualify on day 2 (+4%), but scanner replay says only AAA
    # was a top-N riser that day → BBB must never enter.
    aaa = _spread_day([100, 100, 100], [104, 104, 104])
    bbb = _spread_day([100, 100, 100], [104, 104, 104])
    eligible = {"2026-05-05": {"AAA"}}  # BBB not a mover this day
    result = run_backtest(
        STRATEGY, {"AAA": aaa, "BBB": bbb}, RISK,
        starting_cash=5000, spread_pct=0, eligible_by_day=eligible,
    )
    assert _entries(result) == 1  # held to end → an open position, not a closed trade
    assert result["open_positions"][0]["symbol"] == "AAA"

    # Without the gate, both enter — proving the gate, not the data, blocked BBB.
    ungated = run_backtest(
        STRATEGY, {"AAA": aaa, "BBB": bbb}, RISK, starting_cash=5000, spread_pct=0,
    )
    assert _entries(ungated) == 2


def test_flatten_before_close_fires_on_last_intraday_bar():
    # An intraday strategy that flattens before the close: entered at +4%, no
    # stop/trailing triggers as it rises, so it must exit on the LAST bar of the
    # entry day with the flatten reason — not ride overnight.
    strat = {
        **STRATEGY,
        "params": {
            "entry": STRATEGY["params"]["entry"],
            "exit": {**STRATEGY["params"]["exit"], "flatten_before_close": True},
        },
    }
    series = _spread_day([100, 100, 100], [104, 105, 106])  # day 2 rises, no stop hit
    result = run_backtest(strat, {"TEST": series}, RISK, starting_cash=5000, spread_pct=0)
    assert result["trades"] == 1
    trade = result["trade_list"][0]
    assert trade["exit_reason"] == "flatten before market close"
    assert trade["entry_price"] == 104 and trade["exit_price"] == 106  # exits on the day's last bar


def _daily(closes: list[float], start_day: str = "2026-05-04") -> list[dict]:
    """One bar per ET day (stamped 14:00Z = 10:00 ET, safely inside the day)."""
    from datetime import date

    d0 = date.fromisoformat(start_day)
    out = []
    for i, c in enumerate(closes):
        out.append({"t": f"{(d0 + timedelta(days=i)).isoformat()}T14:00:00Z",
                    "c": c, "v": 1000, "vw": c})
    return out


def test_flatten_before_close_on_daily_bars_still_enters():
    # Regression: with flatten-before-close on and DAILY bars (one bar per day,
    # so each bar is both first AND last of its day), the entry guard must NOT
    # skip every bar — that produced "0 bars evaluated, 0 trades".
    strat = {
        **STRATEGY,
        "params": {
            "entry": STRATEGY["params"]["entry"],
            "exit": {**STRATEGY["params"]["exit"], "flatten_before_close": True},
        },
    }
    series = _daily([100, 104, 104])  # day1 baseline, day2 +4% (entry), day3 flat
    result = run_backtest(strat, {"TEST": series}, RISK, starting_cash=5000, spread_pct=0)
    assert result["diagnosis"]["bars_evaluated"] > 0   # bars WERE evaluated
    assert result["trades"] == 1                        # entered day2…
    assert result["trade_list"][0]["exit_reason"] == "flatten before market close"  # …flattened day3


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


def test_no_trade_reasons_explain_each_flat_day():
    # Two days that never reach the day-gain minimum → every no-trade day gets a
    # plain-English reason naming the day-gain block.
    series = _spread_day([100, 100, 100], [101, 101.5, 102])  # only +2% max, both days
    result = run_backtest(STRATEGY, {"TEST": series}, RISK, starting_cash=5000, spread_pct=0)
    assert result["trades"] == 0
    reasons = result["no_trade_reasons"]
    assert reasons, "a flat run should explain every no-trade day"
    # Each entry is keyed by day and names the dominant blocker.
    for day, text in reasons.items():
        assert text.startswith("No entry")
        assert "day gain below the minimum" in text


def test_no_trade_reasons_omit_days_that_traded():
    # A day that opens a position must NOT appear in no_trade_reasons.
    series = _spread_day([100, 100, 100], [104, 99, 99])  # +4% day → one entry
    result = run_backtest(STRATEGY, {"TEST": series}, RISK, starting_cash=5000, spread_pct=0)
    assert result["trades"] == 1
    entry_day = result["trade_list"][0]["entry_day"]
    assert entry_day not in result["no_trade_reasons"]


def test_no_trade_spans_collapse_consecutive_flat_days():
    # Three days, none reaching the +3% gate → the two EVALUATED no-entry days
    # (day 1 has no prior close to compare) collapse into ONE span.
    series = (
        bars_from([100, 100, 100], "2026-05-04T14:00:00Z")
        + bars_from([101, 101, 101], "2026-05-05T14:00:00Z")  # +1% vs prior day
        + bars_from([101.5, 101.5, 101.5], "2026-05-06T14:00:00Z")  # +0.5%
    )
    result = run_backtest(STRATEGY, {"TEST": series}, RISK, starting_cash=5000, spread_pct=0)
    assert result["trades"] == 0
    spans = result["no_trade_spans"]
    assert len(spans) == 1
    assert spans[0]["days"] == 2
    assert spans[0]["from_day"] == "2026-05-05"
    assert spans[0]["to_day"] == "2026-05-06"
    assert "day gain below the minimum" in spans[0]["reason"]


def test_no_trade_spans_also_count_distinct_symbol_days():
    """The bar-check tally counts every evaluation, so on intraday bars one
    symbol below the gain bar all session scores once per bar — accurate but it
    reads like that many missed chances. The span carries a second reading in
    symbol-days alongside it (both, not instead)."""
    # 2 symbols × 2 evaluated days × 3 bars/day = 12 bar-checks, but only 4
    # symbol-days (2 symbols on each of 2 days).
    flat = (
        bars_from([100, 100, 100], "2026-05-04T14:00:00Z")
        + bars_from([101, 101, 101], "2026-05-05T14:00:00Z")
        + bars_from([101.5, 101.5, 101.5], "2026-05-06T14:00:00Z")
    )
    result = run_backtest(
        STRATEGY, {"AAA": flat, "BBB": list(flat)}, RISK, starting_cash=5000, spread_pct=0
    )
    span = result["no_trade_spans"][0]
    assert "12× day gain below the minimum" in span["reason"]  # unchanged bar-checks
    sd = span["reason_symbol_days"]
    assert "4 symbol-days (2 symbols) — day gain below the minimum" in sd
    assert sd.startswith("Counting each symbol once per day:")


def test_rails_respected_in_backtest():
    # Two symbols both trigger; sleeve budget only fits one $1000 position + cash guard.
    strategy = dict(STRATEGY, sleeve_usd=1500.0)
    series_a = _spread_day([100, 100, 100], [104, 104.5, 105, 105, 105])
    series_b = _spread_day([50, 50, 50], [52, 52.2, 52.5, 52.5, 52.5])
    result = run_backtest(strategy, {"AAA": series_a, "BBB": series_b}, RISK, starting_cash=5000, spread_pct=0)
    # first entry consumes ~$1000 of the $1500 sleeve → second is blocked
    assert _entries(result) == 1
    assert result["open_positions"][0]["symbol"] == "AAA"  # held to end, never sold


def test_open_position_at_end_is_marked_to_market_not_sold():
    series = _spread_day([100, 100, 100], [104, 106, 108])  # never hits an exit rule
    result = run_backtest(STRATEGY, {"TEST": series}, RISK, starting_cash=5000, spread_pct=0)
    assert result["trades"] == 0  # no strategy exit fired — nothing is a closed trade
    assert len(result["open_positions"]) == 1
    pos = result["open_positions"][0]
    assert pos["symbol"] == "TEST"
    assert pos["unrealized_pnl"] > 0  # rode 104 → 108, marked to market
    assert result["net_pnl"] > 0  # the unrealized gain still counts toward equity
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


# ---- optional MACD entry filter, replayed by the backtester ----

def test_macd_entry_filter_off_is_byte_identical():
    # With no MACD flag, the run is unchanged (proves the annotation is a no-op).
    series = _spread_day([100, 100, 100], [104, 107, 110, 102.5, 102.5])
    result = run_backtest(STRATEGY, {"TEST": series}, RISK, starting_cash=5000, spread_pct=0)
    assert result["trades"] == 1


def test_macd_entry_filter_fail_closed_when_history_too_short():
    # require_macd_bullish with the default 12/26/9 periods needs ~35 completed
    # closes; this tiny series can never form the signal, so MACD is None and the
    # fail-closed rule blocks every entry.
    series = _spread_day([100, 100, 100], [104, 107, 110, 102.5, 102.5])
    strat = copy.deepcopy(STRATEGY)
    strat["params"]["entry"]["require_macd_bullish"] = True
    gated = run_backtest(strat, {"TEST": series}, RISK, starting_cash=5000, spread_pct=0)
    assert gated["trades"] == 0


def test_macd_entry_filter_allows_entry_when_bullish():
    # Short periods (2/3/2) + a rising run so the MACD line leads its signal by
    # the entry bar; the +4% day-gain bar then passes the filter and trades once.
    strat = copy.deepcopy(STRATEGY)
    strat["params"]["entry"]["require_macd_bullish"] = True
    strat["params"]["macd"] = {"fast": 2, "slow": 3, "signal": 2}
    series = _spread_day([90, 92, 94, 96, 100], [104, 104])  # day1 rising, day2 +4%
    result = run_backtest(strat, {"TEST": series}, RISK, starting_cash=5000, spread_pct=0)
    assert _entries(result) == 1  # entered on the +4% bar, then held to the end
    assert "MACD bullish" in result["open_positions"][0]["entry_reason"]


# ---- ATR stops & sizing (off by default) ----

def _daily_ohlc(rows: list[tuple[float, float, float]], start: str = "2026-05-01T00:00:00Z") -> list[dict]:
    """One bar PER DAY (close, high, low) — so each bar is its own ET day and ATR
    history accumulates one completed bar at a time, exactly like the live daily
    ATR fetch."""
    t0 = datetime.fromisoformat(start.replace("Z", "+00:00"))
    out = []
    for i, (c, h, l) in enumerate(rows):
        ts = t0 + timedelta(days=i)
        out.append({"t": ts.strftime("%Y-%m-%dT%H:%M:%SZ"), "c": c, "h": h, "l": l, "v": 1000, "vw": c})
    return out


# 16 flat calm days (range 99–101 → ATR ≈ 2), then a +4% riser to 104 → entry.
_ATR_FLAT = [(100.0, 101.0, 99.0)] * 16
_ATR_RISER = _ATR_FLAT + [(104.0, 105.0, 103.0)]


def test_atr_off_is_byte_identical():
    # An all-zero atr block must not change anything vs no atr block at all.
    series = _daily_ohlc(_ATR_RISER + [(104.0, 105.0, 103.0)])
    base = run_backtest(STRATEGY, {"TEST": series}, RISK, starting_cash=5000, spread_pct=0)
    strat = copy.deepcopy(STRATEGY)
    strat["params"]["atr"] = {"period": 14, "stop_mult": 0.0, "risk_usd": 0.0}
    off = run_backtest(strat, {"TEST": series}, RISK, starting_cash=5000, spread_pct=0)
    assert base == off


def test_atr_sizing_enlarges_position_for_a_calm_name():
    # A calm name (ATR ≈ 1.9%) with a 2× stop → 3.8% stop distance. risk_usd 50 →
    # ~$1,300 size, bigger than the fixed $1,000, so more shares are bought.
    series = _daily_ohlc(_ATR_RISER + [(104.0, 105.0, 103.0)])
    fixed = run_backtest(STRATEGY, {"TEST": series}, RISK, starting_cash=5000, spread_pct=0)
    strat = copy.deepcopy(STRATEGY)
    strat["params"]["atr"] = {"period": 14, "stop_mult": 2.0, "risk_usd": 50.0}
    sized = run_backtest(strat, {"TEST": series}, RISK, starting_cash=5000, spread_pct=0)
    # Both enter on the riser and hold to the end (no exit fires) → open positions.
    assert len(fixed["open_positions"]) == 1 and len(sized["open_positions"]) == 1
    assert sized["open_positions"][0]["qty"] > fixed["open_positions"][0]["qty"]


def test_atr_stop_widens_and_holds_a_drop_the_fixed_stop_would_catch():
    # Entry at 104, then a −5% drop to 98.8. Isolate the HARD stop by turning the
    # trailing stop off. The fixed 4% stop exits; a wide 5× ATR stop (≈ 10%) holds.
    series = _daily_ohlc(_ATR_RISER + [(98.8, 99.0, 98.0)])
    base = copy.deepcopy(STRATEGY)
    base["params"]["exit"]["trailing_stop_pct"] = 0

    fixed = run_backtest(base, {"TEST": series}, RISK, starting_cash=5000, spread_pct=0)
    assert "stop-loss" in fixed["trade_list"][0]["exit_reason"]
    assert "ATR" not in fixed["trade_list"][0]["exit_reason"]

    strat = copy.deepcopy(base)
    strat["params"]["atr"] = {"period": 14, "stop_mult": 5.0, "risk_usd": 0.0}  # ATR stop ≈ 10%
    wide = run_backtest(strat, {"TEST": series}, RISK, starting_cash=5000, spread_pct=0)
    # The wide ATR stop is not hit by the 5% drop — no exit fires, so the position
    # is still open at the end (not force-sold).
    assert wide["trades"] == 0
    assert len(wide["open_positions"]) == 1


# 40 warm-up days rising ~1%/day (MACD turns bullish), then 6 window days that
# keep rising — enough history that MACD is defined the instant the window opens.
_MACD_WARMUP_CLOSES = [round(100 * (1.01 ** i), 2) for i in range(46)]
_MACD_WINDOW_START_IDX = 40


def _macd_strategy() -> dict:
    strat = copy.deepcopy(STRATEGY)
    strat["params"]["entry"]["min_day_gain_pct"] = 0.5
    strat["params"]["entry"]["require_macd_bullish"] = True
    strat["params"]["macd"] = None  # default 12/26/9 — also exercises the null-macd path
    return strat


def test_daily_positions_attribute_each_days_move():
    # Per-day holdings attribution: AAA rises (+$36 on day 3) while BBB collapses
    # and stops out (−$126 the same day). Each day lists what was held with its
    # dollar contribution; entered-today costs nothing at spread 0; a position
    # closed that day appears with qty 0; rows sort by |impact|.
    aaa = _daily([100.0, 104.0, 108.0, 99.0])
    bbb = _daily([100.0, 104.0, 90.0, 90.0])
    res = run_backtest(STRATEGY, {"AAA": aaa, "BBB": bbb}, RISK, starting_cash=5000, spread_pct=0)
    dp = res["daily_positions"]
    d1, d2, d3 = "2026-05-04", "2026-05-05", "2026-05-06"

    assert dp[d1] == []  # nothing held on the baseline day
    # Day 2: both entered at their close (9 shares @ 104) — zero cost at spread 0.
    assert {(c["symbol"], c["qty"], c["day_pnl"]) for c in dp[d2]} == {("AAA", 9, 0.0), ("BBB", 9, 0.0)}
    # Day 3: BBB stopped out for 9 × (90 − 104) = −$126 (qty 0 = closed today);
    # AAA held through for 9 × (108 − 104) = +$36. Biggest impact first.
    assert dp[d3][0] == {"symbol": "BBB", "qty": 0, "price": 90.0, "day_pnl": -126.0}
    assert dp[d3][1] == {"symbol": "AAA", "qty": 9, "price": 108.0, "day_pnl": 36.0}
    # The day's contributions sum to the equity move: 5000 → 4910 on day 3.
    assert sum(c["day_pnl"] for c in dp[d3]) == -90.0


def test_entry_on_the_final_bar_stays_open_with_zero_unrealized():
    # Borderline: the entry signal fires on the very LAST bar of the window. The
    # position opens, is immediately marked at its own fill (spread 0), and the
    # run ends — zero unrealized, zero trades, one open position, flat P&L.
    series = _daily([100.0, 104.0])
    res = run_backtest(STRATEGY, {"TEST": series}, RISK, starting_cash=5000, spread_pct=0)
    assert res["trades"] == 0
    assert len(res["open_positions"]) == 1
    assert res["open_positions"][0]["unrealized_pnl"] == 0.0
    assert res["net_pnl"] == 0.0
    assert res["daily_positions"]["2026-05-05"] == [
        {"symbol": "TEST", "qty": 9, "price": 104.0, "day_pnl": 0.0}
    ]


def test_daily_positions_charge_the_spread_on_entry_day():
    # With a real spread the entry-day contribution is the spread cost itself:
    # fill = 104 × 1.005 = 104.52, marked at 104 → 9 × (104 − 104.52) = −$4.68.
    # The flat day after contributes exactly 0 — no phantom drift.
    series = _daily([100.0, 104.0, 104.0])
    res = run_backtest(STRATEGY, {"TEST": series}, RISK, starting_cash=5000, spread_pct=0.5)
    assert res["daily_positions"]["2026-05-05"][0]["day_pnl"] == -4.68
    assert res["daily_positions"]["2026-05-06"][0]["day_pnl"] == 0.0


def test_daily_positions_same_day_round_trip():
    # Intraday: entered at 104 and trailing-stopped out at 102.5 the SAME day —
    # the day's attribution is one qty-0 row carrying the full realized loss.
    series = _spread_day([100, 100, 100], [104, 107, 110, 102.5, 102.5])
    res = run_backtest(STRATEGY, {"TEST": series}, RISK, starting_cash=5000, spread_pct=0)
    assert res["daily_positions"]["2026-05-05"] == [
        {"symbol": "TEST", "qty": 0, "price": 102.5, "day_pnl": -13.5}
    ]


def test_crypto_day_gain_uses_a_rolling_24h_baseline():
    # Crypto has no session day: its day-gain must measure vs ~24h back (the
    # scanner's rolling definition), not vs the previous UTC-day close. Here the
    # prior UTC day CLOSED at 104 after rising from 100; a +8% entry threshold:
    #   vs previous day close (stock rule): 110/104 → +5.8% → skipped
    #   vs 24h back (crypto rule):          110/100 → +10%  → enters
    def ib(ts: str, c: float) -> dict:
        return {"t": ts, "c": c, "v": 1000, "vw": c}

    bars = [
        ib("2026-05-04T12:00:00Z", 100.0), ib("2026-05-04T18:00:00Z", 104.0),
        ib("2026-05-05T12:00:00Z", 110.0), ib("2026-05-05T18:00:00Z", 110.0),
    ]
    strat = copy.deepcopy(STRATEGY)
    strat["asset_class"] = "crypto"
    strat["params"]["entry"]["min_day_gain_pct"] = 8.0

    crypto = run_backtest(strat, {"BTC/USD": bars}, RISK, starting_cash=5000, spread_pct=0, market="crypto")
    assert _entries(crypto) == 1
    entered = ([t["entry_day"] for t in crypto["trade_list"]] + [p["entry_day"] for p in crypto["open_positions"]])
    assert entered == ["2026-05-05"]

    # Control: identical bars under the stock session rule never reach +8%.
    stock_strat = copy.deepcopy(strat)
    stock_strat["asset_class"] = "stock"
    stock = run_backtest(stock_strat, {"TEST": bars}, RISK, starting_cash=5000, spread_pct=0)
    assert _entries(stock) == 0


def test_daily_positions_exclude_warmup_days():
    # With warm-up bars before sim_start, the attribution map must only contain
    # days inside the traded window — warm-up days feed indicators, nothing else.
    bars = _daily(_MACD_WARMUP_CLOSES)
    sim_start = datetime.fromisoformat(
        bars[_MACD_WINDOW_START_IDX]["t"].replace("Z", "+00:00")
    )
    strat = copy.deepcopy(STRATEGY)
    strat["params"]["entry"]["min_day_gain_pct"] = 0.5
    res = run_backtest(
        strat, {"TEST": bars}, RISK, starting_cash=5000, spread_pct=0, sim_start=sim_start,
    )
    window_day = sim_start.date().isoformat()
    assert res["daily_positions"]
    assert all(day >= window_day for day in res["daily_positions"])


def test_no_trade_log_reports_not_enough_funds_after_a_loss():
    # All-in sizing: $ per trade == the sleeve == starting cash. The first trade
    # loses a little, dropping equity below one full position; the no-leverage
    # exposure cap then blocks every later entry. The trade log must name this
    # "not enough funds", not a vague "blocked by a risk rail".
    strat = copy.deepcopy(STRATEGY)
    strat["sizing_usd"] = 1000.0
    strat["sleeve_usd"] = 1000.0
    strat["params"]["entry"]["min_day_gain_pct"] = 3.0
    strat["params"]["exit"]["trailing_stop_pct"] = 0
    strat["params"]["exit"]["stop_loss_pct"] = 3.0
    # day1 +4% (entry: 9 whole shares ≈ $936 in), day2 −13% (stop-loss, equity now
    # ~$874), then +5% days that qualify but a full $1k no longer fits → the
    # no-leverage exposure cap blocks them as "not enough funds".
    bars = _daily([100.0, 104.0, 90.0, 94.5, 99.3, 99.4])
    res = run_backtest(strat, {"AAA": bars}, RISK, starting_cash=1000, spread_pct=0)

    assert res["trades"] == 1  # only the first, losing trade ever opens
    reasons = " ".join(res["no_trade_reasons"].values())
    assert "not enough funds" in reasons
    # And it's attributed to funds, not the generic rail bucket.
    assert "risk rail" not in reasons


def test_macd_strategy_trades_from_window_start_when_warmup_precedes_it():
    # THE dead-zone bug: a MACD strategy couldn't trade until ~35 bars into the
    # test window because the backtest never fetched warm-up history. With warm-up
    # bars before sim_start, MACD is already defined at the window's first bar, so
    # the strategy can enter on day one of the window.
    bars = _daily(_MACD_WARMUP_CLOSES)
    sim_start = datetime.fromisoformat(
        bars[_MACD_WINDOW_START_IDX]["t"].replace("Z", "+00:00")
    )
    strat = _macd_strategy()

    warm = run_backtest(
        strat, {"TEST": bars}, RISK, starting_cash=5000, spread_pct=0, sim_start=sim_start,
    )
    # It entered — and every entry is inside the window, never in the warm-up run.
    # (A rising MACD name never trips a stop, so the position is still open at the
    # end rather than a closed trade.)
    assert _entries(warm) >= 1
    window_day = sim_start.date().isoformat()
    entry_days = [t["entry_day"] for t in warm["trade_list"]] + [p["entry_day"] for p in warm["open_positions"]]
    assert entry_days and all(d >= window_day for d in entry_days)

    # Control: hand it ONLY the window bars (no warm-up) — MACD is undefined for
    # all of them, so require_macd_bullish blocks every entry. This is the old
    # behaviour the fix corrects.
    cold = run_backtest(
        strat, {"TEST": bars[_MACD_WINDOW_START_IDX:]}, RISK, starting_cash=5000, spread_pct=0,
    )
    assert _entries(cold) == 0


# ---- mixed-resolution replay: signals from daily closes, exits on intraday bars ----
#
# The tension this resolves: MACD/RSI must come from COMPLETED DAILY closes (that
# is how the live engine reads them), but a daily replay checks the exit rules
# once a day at the close, so a stop-loss can't be simulated. Mixed resolution
# feeds the indicators from a daily series while replaying intraday bars.

# A fading name — MACD is bearish on every completed daily close — followed by one
# moonshot day. That day's OWN close would flip MACD bullish, which is exactly the
# knowledge a live engine does not have while the day is still in progress.
_FADE = [float(100 - i) for i in range(11)]
_MOONSHOT = 130.0
_SHORT_MACD = {"fast": 2, "slow": 3, "signal": 2}  # short periods: 11 closes suffice


def _mixed_strategy(**exit_overrides) -> dict:
    """A MACD-gated strategy — the shape that needs both resolutions at once."""
    strat = copy.deepcopy(STRATEGY)
    strat["params"]["entry"]["require_macd_bullish"] = True
    strat["params"]["macd"] = _SHORT_MACD
    strat["params"]["exit"].update(exit_overrides)
    return strat


def test_mixed_resolution_never_peeks_at_the_current_days_daily_close():
    # THE look-ahead test. Day D is a +44% moonshot whose close flips MACD from
    # bearish to bullish. Mid-day, live, that close hasn't happened — only closes
    # through D−1 exist, and there MACD is bearish. So every intraday bar on day D
    # must carry the D−1 verdict, never the one day D's own close would give.
    daily_closes = _FADE + [_MOONSHOT]
    daily = _daily(daily_closes, "2026-05-04")
    day_before, day_d = "2026-05-14", "2026-05-15"
    intraday = bars_from([_FADE[-1]] * 3, f"{day_before}T14:00:00Z") + bars_from(
        [120.0, 125.0, _MOONSHOT], f"{day_d}T14:00:00Z"
    )

    through_yesterday = stats.macd_bullish(_FADE, 2, 3, 2)
    including_today = stats.macd_bullish(daily_closes, 2, 3, 2)
    assert through_yesterday is False and including_today is True  # the close really does flip it

    prepared = {"TEST": bt._prepare(intraday, bt._et_day)}
    bt._annotate_macd(prepared, _mixed_strategy()["params"], {"TEST": daily}, bt._et_day)
    on_day_d = [b["macd_bullish"] for b in prepared["TEST"] if b["day"] == day_d]
    assert len(on_day_d) == 3
    assert all(v is through_yesterday for v in on_day_d)   # D−1's verdict…
    assert not any(v is including_today for v in on_day_d)  # …never D's own

    # And the consequence that matters: day D is a +33% riser that sails through the
    # day-gain gate, but MACD (completed closes only) is bearish, so nothing trades.
    blocked = run_backtest(
        _mixed_strategy(), {"TEST": intraday}, RISK, starting_cash=5000, spread_pct=0,
        daily_bars_by_symbol={"TEST": daily},
    )
    assert _entries(blocked) == 0
    # Control: with the MACD gate off the same bars DO trade — it was the signal
    # that blocked the entry, not missing data or a broken timeline.
    ungated = _mixed_strategy()
    ungated["params"]["entry"]["require_macd_bullish"] = False
    assert _entries(run_backtest(
        ungated, {"TEST": intraday}, RISK, starting_cash=5000, spread_pct=0,
        daily_bars_by_symbol={"TEST": daily},
    )) == 1


# A 20-day slide (RSI pinned at 0) then a moonshot day that would lift RSI to ~79.
_RSI_FADE = [float(100 - i) for i in range(20)]


def test_mixed_resolution_rsi_also_stops_at_yesterdays_close():
    # Same rule, the RSI annotator: on day D the RSI must be the one computed from
    # closes through D−1 (0.0 — a relentless slide), not the ~79 that day D's own
    # moonshot close would produce.
    daily_closes = _RSI_FADE + [_MOONSHOT]
    daily = _daily(daily_closes, "2026-05-04")
    day_before, day_d = "2026-05-23", "2026-05-24"
    intraday = bars_from([_RSI_FADE[-1]] * 3, f"{day_before}T14:00:00Z") + bars_from(
        [120.0, 125.0, _MOONSHOT], f"{day_d}T14:00:00Z"
    )
    strat = copy.deepcopy(STRATEGY)
    strat["params"]["entry"]["rsi_min"] = 50.0  # only buy names with real strength

    prepared = {"TEST": bt._prepare(intraday, bt._et_day)}
    bt._annotate_rsi(prepared, strat["params"], {"TEST": daily}, bt._et_day)
    assert stats.rsi_from_closes(daily_closes) > 50  # day D's close WOULD clear the band
    assert [b["rsi"] for b in prepared["TEST"] if b["day"] == day_d] == [0.0, 0.0, 0.0]

    res = run_backtest(
        strat, {"TEST": intraday}, RISK, starting_cash=5000, spread_pct=0,
        daily_bars_by_symbol={"TEST": daily},
    )
    assert _entries(res) == 0  # RSI 0 through yesterday → below the band → no entry


# A calm 1%/day climb (MACD bullish), one +4% day that triggers the entry, then a
# day that CLOSES only 1% down but was 10% down mid-day — right through the 4% stop.
_TREND = [round(100 * (1.01 ** i), 2) for i in range(10)]
_TREND_LAST = _TREND[-1]
_ENTRY_CLOSE = round(_TREND_LAST * 1.04, 2)
_RECOVERED = round(_ENTRY_CLOSE * 0.99, 2)
_INTRADAY_DIP = round(_ENTRY_CLOSE * 0.90, 2)


def test_mixed_resolution_catches_a_stop_the_daily_replay_misses():
    # The whole point of the feature. ONE price path, two replays. On daily bars the
    # exit rules run once, at the close, and the day closed just 1% down — so the
    # position survives and the dip is invisible. On intraday bars, with the very
    # same daily MACD driving the entry, the mid-day plunge through the stop fires
    # the stop-loss — which is what would really have happened.
    strat = _mixed_strategy(trailing_stop_pct=0)  # isolate the hard stop
    daily = _daily(_TREND + [_ENTRY_CLOSE, _RECOVERED], "2026-05-04")
    intraday = (
        bars_from([_TREND_LAST] * 3, "2026-05-13T14:00:00Z")     # the day before
        + bars_from([_ENTRY_CLOSE] * 3, "2026-05-14T14:00:00Z")  # +4% → entry
        + bars_from([_RECOVERED, _INTRADAY_DIP, _RECOVERED], "2026-05-15T14:00:00Z")
    )

    daily_only = run_backtest(strat, {"TEST": daily}, RISK, starting_cash=5000, spread_pct=0)
    assert daily_only["trades"] == 0                 # no stop-out ever fired…
    assert len(daily_only["open_positions"]) == 1    # …the position is still riding
    assert daily_only["open_positions"][0]["entry_price"] == _ENTRY_CLOSE

    mixed = run_backtest(
        strat, {"TEST": intraday}, RISK, starting_cash=5000, spread_pct=0,
        daily_bars_by_symbol={"TEST": daily},
    )
    assert mixed["trades"] == 1
    trade = mixed["trade_list"][0]
    assert "stop-loss" in trade["exit_reason"]
    assert trade["entry_price"] == _ENTRY_CLOSE      # same entry signal, real exit
    assert trade["exit_price"] == _INTRADAY_DIP
    # The honest run is the worse one — that flattering daily result was the bug.
    assert mixed["net_pnl"] < daily_only["net_pnl"] < 0


def test_daily_bars_none_is_the_old_single_resolution_run():
    # No-regression: omitting the new argument, or passing None, must reproduce the
    # single-resolution behaviour exactly — same trade, same numbers as before.
    series = _spread_day([100, 100, 100], [104, 107, 110, 102.5, 102.5])
    old = run_backtest(STRATEGY, {"TEST": series}, RISK, starting_cash=5000, spread_pct=0)
    explicit_none = run_backtest(
        STRATEGY, {"TEST": series}, RISK, starting_cash=5000, spread_pct=0,
        daily_bars_by_symbol=None,
    )
    assert old == explicit_none
    # …and it is still the known-correct result (see test_rise_then_trail_exit).
    assert old["trades"] == 1
    assert "trailing stop" in old["trade_list"][0]["exit_reason"]
    assert old["net_pnl"] == round((102.5 - 104) * 9, 2)


def test_mixed_resolution_leaves_a_symbol_without_daily_bars_undecided():
    # A symbol missing from the daily series must not crash the run — and must not
    # quietly fall back to intraday-derived indicators either. Its MACD stays None,
    # which require_macd_bullish reads as "can't tell" and fails closed. The symbol
    # that does have daily bars trades normally.
    strat = _mixed_strategy()
    daily = _daily(_TREND + [_ENTRY_CLOSE], "2026-05-04")
    intraday = bars_from([_TREND_LAST] * 3, "2026-05-13T14:00:00Z") + bars_from(
        [_ENTRY_CLOSE] * 3, "2026-05-14T14:00:00Z"
    )
    res = run_backtest(
        strat, {"HAS": intraday, "MISSING": intraday}, RISK, starting_cash=5000, spread_pct=0,
        daily_bars_by_symbol={"HAS": daily},  # MISSING has no daily series at all
    )
    entered = [t["symbol"] for t in res["trade_list"]] + [p["symbol"] for p in res["open_positions"]]
    assert entered == ["HAS"]

    # The annotation itself: None on every bar, never an intraday-derived value.
    prepared = {"MISSING": bt._prepare(intraday, bt._et_day)}
    bt._annotate_macd(prepared, strat["params"], {}, bt._et_day)
    assert all(b["macd_bullish"] is None for b in prepared["MISSING"])


def test_warmup_bars_never_touch_equity_or_the_trade_log():
    # A plain momentum strategy (no MACD) would happily buy during the warm-up
    # days if they were simulated. With sim_start set, those bars must feed nothing
    # but indicators: no trades before the window, and the equity curve starts at
    # the window, not the warm-up.
    bars = _daily(_MACD_WARMUP_CLOSES)
    sim_start = datetime.fromisoformat(
        bars[_MACD_WINDOW_START_IDX]["t"].replace("Z", "+00:00")
    )
    strat = copy.deepcopy(STRATEGY)
    strat["params"]["entry"]["min_day_gain_pct"] = 0.5  # every rising day qualifies

    result = run_backtest(
        strat, {"TEST": bars}, RISK, starting_cash=5000, spread_pct=0, sim_start=sim_start,
    )
    window_day = sim_start.date().isoformat()
    assert _entries(result) >= 1
    entry_days = [t["entry_day"] for t in result["trade_list"]] + [p["entry_day"] for p in result["open_positions"]]
    assert entry_days and all(d >= window_day for d in entry_days)
    # No warm-up bar leaked into the equity curve.
    assert all(day >= window_day for day in result["equity_days"])


def _ohlc(rows: list[tuple[float, float, float]], start_day: str = "2026-05-04") -> list[dict]:
    """One DAILY bar per day as (close, high, low) — so a bar can dip through a
    stop and still close higher, which close-only replay can't see."""
    from datetime import date

    d0 = date.fromisoformat(start_day)
    return [
        {"t": f"{(d0 + timedelta(days=i)).isoformat()}T14:00:00Z",
         "c": c, "h": h, "l": lo, "v": 1000, "vw": c}
        for i, (c, h, lo) in enumerate(rows)
    ]


def test_stop_fires_on_the_bar_low_not_just_the_close():
    """THE fidelity fix: entry at 104, then a bar that dips to 96 (through the 4%
    stop at 99.84) but CLOSES back at 105. Judging on the close alone scores this
    a winner still riding; judging on the low — what the price actually did —
    stops you out, which is what live would have done."""
    series = _ohlc([
        (100.0, 100.0, 100.0),   # baseline day
        (104.0, 104.0, 104.0),   # +4% → entry at 104
        (105.0, 106.0, 96.0),    # dips to 96, recovers to close 105
    ])
    res = run_backtest(STRATEGY, {"TEST": series}, RISK, starting_cash=5000, spread_pct=0)
    assert res["trades"] == 1
    t = res["trade_list"][0]
    assert "stop-loss" in t["exit_reason"]
    # Filled at the STOP LEVEL (104 × 0.96 = 99.84), not the 105 close — booking
    # the recovery would be the flattering error.
    assert t["exit_price"] == pytest.approx(99.84, abs=0.01)
    assert res["net_pnl"] < 0


def test_take_profit_fires_on_the_bar_high_and_fills_at_the_target():
    strat = copy.deepcopy(STRATEGY)
    strat["params"]["exit"]["take_profit_pct"] = 10.0
    strat["params"]["exit"]["trailing_stop_pct"] = 0
    series = _ohlc([
        (100.0, 100.0, 100.0),
        (104.0, 104.0, 104.0),          # entry at 104
        (105.0, 116.0, 104.0),          # spikes to 116 (>= +10% = 114.4), closes 105
    ])
    res = run_backtest(strat, {"TEST": series}, RISK, starting_cash=5000, spread_pct=0)
    t = res["trade_list"][0]
    assert "take-profit" in t["exit_reason"]
    assert t["exit_price"] == pytest.approx(114.4, abs=0.01)  # the target, not the close


def test_a_bar_touching_both_stop_and_target_assumes_the_stop_first():
    """Within one bar the order is unknowable, so the conservative reading wins —
    never the flattering one."""
    strat = copy.deepcopy(STRATEGY)
    strat["params"]["exit"]["take_profit_pct"] = 10.0
    strat["params"]["exit"]["trailing_stop_pct"] = 0
    series = _ohlc([
        (100.0, 100.0, 100.0),
        (104.0, 104.0, 104.0),
        (105.0, 120.0, 95.0),   # touches BOTH the +10% target and the -4% stop
    ])
    res = run_backtest(strat, {"TEST": series}, RISK, starting_cash=5000, spread_pct=0)
    assert "stop-loss" in res["trade_list"][0]["exit_reason"]


def test_fill_is_clamped_into_the_bar_when_price_gaps_through_the_stop():
    """A bar entirely below the stop never traded at the stop price — filling
    there would be better than anything available. Clamp to the bar's high."""
    series = _ohlc([
        (100.0, 100.0, 100.0),
        (104.0, 104.0, 104.0),          # entry 104, stop at 99.84
        (90.0, 92.0, 88.0),             # gapped: whole bar below the stop
    ])
    res = run_backtest(STRATEGY, {"TEST": series}, RISK, starting_cash=5000, spread_pct=0)
    t = res["trade_list"][0]
    assert t["exit_price"] == pytest.approx(92.0, abs=0.01)  # the high, not 99.84
