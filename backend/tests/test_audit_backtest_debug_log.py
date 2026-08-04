"""The per-bar debug log the replay can return on request.

Added because the aggregate counters in `diagnosis` structurally cannot answer
the question they were being asked. Strategy 25 bought AMZN live at 14:01:20 and
the replay waited until 14:26 with every named entry condition apparently
satisfied at both instants; `entry_ok_but_rail_blocked: 438` says a rail fired
438 times across the session and names none of them, on none of the bars.
"""

from datetime import datetime, timedelta, timezone

from qt.services.backtest import DEBUG_LOG_MAX_LINES, _btlog, run_backtest
from qt.services.engine import RISK_DEFAULTS

RISK = dict(
    RISK_DEFAULTS,
    max_total_positions=50,
    max_total_exposure_usd=1_000_000,
    max_daily_loss_usd=1_000_000,
    max_trades_per_day=1000,
    wash_sale_guard="off",
)


def _strategy(**kw) -> dict:
    base = {
        "asset_class": "stock",
        "swing_mode": False,
        "sizing_usd": 1000.0,
        "sleeve_usd": 5000.0,
        "max_positions": 5,
        "params": {
            "entry": {
                "min_day_gain_pct": 1.0,
                "require_above_vwap": False,
                "entry_window_start": None,
                "entry_window_end": None,
            },
            "exit": {
                "trailing_stop_pct": 0,
                "stop_loss_pct": 0,
                "take_profit_pct": 0,
                "max_holding_hours": 0,
                "flatten_before_close": False,
                "exit_below_vwap": False,
            },
        },
    }
    base.update(kw)
    return base


def _bars(n: int, *, start_close: float = 100.0, step_pct: float = 6.0) -> list[dict]:
    """Raw Alpaca daily bars, each one up `step_pct` on the last, so every bar
    after the first clears the 1% gain threshold and is actually judged."""
    out = []
    day0 = datetime(2026, 5, 1, 14, 0, tzinfo=timezone.utc)
    close = start_close
    for i in range(n):
        ts = day0 + timedelta(days=i)
        out.append({
            "t": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "o": close, "h": close, "l": close, "c": close, "v": 1000, "vw": close,
        })
        close *= 1 + step_pct / 100
    return out


def test_the_log_is_absent_unless_it_was_asked_for():
    """A diagnostic must not ride along on every ordinary backtest."""
    result = run_backtest(_strategy(), {"AAA": _bars(5)}, RISK)
    assert "debug_log" not in result


def test_asking_for_it_returns_lines():
    result = run_backtest(_strategy(), {"AAA": _bars(5)}, RISK, debug_log=True)
    assert result["debug_log"]
    assert any("BTDBG" in line for line in result["debug_log"])


def test_every_line_carries_the_bar_instant_not_just_the_day():
    """The whole point on a 1-minute replay: WHICH minute the verdict changed."""
    result = run_backtest(_strategy(), {"AAA": _bars(5)}, RISK, debug_log=True)
    per_bar = [ln for ln in result["debug_log"] if " AAA " in ln]
    assert per_bar, result["debug_log"]
    assert any("2026-05-" in ln and "14:00" in ln for ln in per_bar), per_bar[:5]


def test_a_rail_rejection_names_the_rail():
    """`entry_ok_but_rail_blocked: 438` naming no rail is what prompted this."""
    tight = dict(RISK, max_total_positions=0)
    result = run_backtest(
        _strategy(), {"AAA": _bars(5), "BBB": _bars(5)}, tight, debug_log=True
    )
    rails = [ln for ln in result["debug_log"] if "reject-rail:" in ln]
    assert rails, result["debug_log"]
    assert any("rail:" in ln for ln in rails)


def test_the_pre_truncation_count_is_reported():
    """A log that silently stops reads as though the last line were the last
    decision. The total has to survive truncation."""
    result = run_backtest(_strategy(), {"AAA": _bars(5)}, RISK, debug_log=True)
    assert result["debug_log_total"] == len(result["debug_log"])
    assert result["debug_log_truncated"] is False


def test_a_long_run_is_truncated_and_says_so():
    symbols = {f"S{i:02d}": _bars(200, step_pct=0.5) for i in range(40)}
    result = run_backtest(_strategy(), symbols, RISK, debug_log=True)
    assert result["debug_log_truncated"] is True
    assert len(result["debug_log"]) == DEBUG_LOG_MAX_LINES
    assert result["debug_log_total"] > DEBUG_LOG_MAX_LINES


def test_the_sink_collects_instead_of_printing(capsys):
    """Collected lines must not also go to the container log, or a diagnostic
    request floods the operator's stream with someone else's question."""
    sink: list[str] = []
    bar = {"close": 10.0, "change_pct": 1.0, "ts": datetime(2026, 8, 3, 14, 1, tzinfo=timezone.utc)}
    _btlog("2026-08-03", "AAA", bar, {"entry": {}}, "ENTER", sink)
    assert sink and "AAA" in sink[0]
    assert capsys.readouterr().out == ""


def test_without_a_sink_it_still_prints(capsys):
    """The container-log path is what QT_BACKTEST_DEBUG uses; don't break it."""
    bar = {"close": 10.0, "change_pct": 1.0, "ts": datetime(2026, 8, 3, 14, 1, tzinfo=timezone.utc)}
    _btlog("2026-08-03", "AAA", bar, {"entry": {}}, "ENTER")
    assert "BTDBG" in capsys.readouterr().out
