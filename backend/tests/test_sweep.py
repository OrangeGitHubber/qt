"""Basket sweep: every basket through the SAME parameter search, ranked by the
out-of-sample margin over SPY. Tests pin the ranking rules (tested rows by
margin, untested rows sink), the skip reasons, and the SPY window math — the
search itself is injected (same pattern as the optimizer's backtest_fn)."""

from datetime import date, datetime, timedelta

from qt.services import optimizer
from qt.services.sweep import pct_over_window, sweep_baskets

# A fixed out-of-sample window every fake result reports; SPY closes are built
# around it so the expected margin is known by construction.
OOS_START = "2026-05-01T00:00:00+00:00"
OOS_END = "2026-06-30T00:00:00+00:00"


def _daily(closes: list[float], start_day: str = "2026-01-05") -> list[dict]:
    d0 = date.fromisoformat(start_day)
    return [
        {"t": f"{(d0 + timedelta(days=i)).isoformat()}T14:00:00Z", "c": c, "v": 1000, "vw": c}
        for i, c in enumerate(closes)
    ]


# SPY: 100 flat until the OOS boundary, then climbs to 105 → OOS return +5%.
SPY_BARS = _daily([100.0] * 100, "2026-01-25") + _daily([101.0, 103.0, 105.0], "2026-06-01")


def _fake_result(oos_pct: float | None, oos_trades: int) -> dict:
    return {
        "best": {
            "params": {"min_day_gain_pct": 2.0, "trailing_stop_pct": 4.0,
                       "stop_loss_pct": 3.0, "take_profit_pct": 8.0},
            "in_sample": {"net_pnl_pct": 9.9, "trades": 12},
            "out_of_sample": {"net_pnl_pct": oos_pct, "trades": oos_trades,
                              "entries": oos_trades,  # mirrors optimizer._metrics
                              "win_rate": 50.0, "max_drawdown_pct": 2.0},
        },
        "best_draft_params": {"entry": {}, "exit": {}},
        "tested_combinations": 41,
        "search_space_size": 2352,
        "out_of_sample_window": {"start": OOS_START, "end": OOS_END, "days": 60.0},
        "hold_benchmark_comparison": {"hold_out_of_sample_pct": 4.0, "beat_hold": True},
        "warnings": [],
        "no_trade_reason": None,
    }


def _fake_optimize(results_by_first_symbol: dict[str, dict]):
    """Route each basket to its canned result via its first (sorted) symbol."""

    def fn(strategy, bars_by_symbol, risk, **kw):
        return results_by_first_symbol[sorted(bars_by_symbol)[0]]

    return fn


BARS = {  # every basket symbol needs *some* bars; content is irrelevant to fakes
    sym: _daily([100.0, 101.0, 102.0]) for sym in ("AAA", "BBB", "CCC", "DDD")
}


def test_rows_rank_by_out_of_sample_margin_over_spy():
    baskets = [
        {"id": 1, "name": "Alpha", "symbols": ["AAA"]},
        {"id": 2, "name": "Beta", "symbols": ["BBB"]},
    ]
    fake = _fake_optimize({
        "AAA": _fake_result(oos_pct=3.0, oos_trades=5),   # margin 3 − 5 = −2
        "BBB": _fake_result(oos_pct=12.0, oos_trades=5),  # margin 12 − 5 = +7 → wins
    })
    out = sweep_baskets(baskets, {**BARS, "SPY": SPY_BARS}, {}, min_symbols=1, optimize_fn=fake)
    assert [r["basket_name"] for r in out["rows"]] == ["Beta", "Alpha"]
    assert out["rows"][0]["spy_oos_pct"] == 5.0
    assert out["rows"][0]["margin_vs_spy"] == 7.0
    assert out["rows"][1]["margin_vs_spy"] == -2.0
    assert [r["rank"] for r in out["rows"]] == [1, 2]


def test_untested_rows_sink_below_tested_ones():
    # A huge margin with ZERO out-of-sample trades is an untested hypothesis — it
    # must rank BELOW a modest but tested result.
    baskets = [
        {"id": 1, "name": "Loud", "symbols": ["AAA"]},
        {"id": 2, "name": "Quiet", "symbols": ["BBB"]},
    ]
    fake = _fake_optimize({
        "AAA": _fake_result(oos_pct=50.0, oos_trades=0),  # untested (0 trades)
        "BBB": _fake_result(oos_pct=6.0, oos_trades=4),   # tested, margin +1
    })
    out = sweep_baskets(baskets, {**BARS, "SPY": SPY_BARS}, {}, min_symbols=1, optimize_fn=fake)
    assert [r["basket_name"] for r in out["rows"]] == ["Quiet", "Loud"]
    assert out["rows"][1]["untested"] is True


def test_baskets_without_history_are_skipped_with_a_reason():
    baskets = [
        {"id": 1, "name": "Ghost", "symbols": ["ZZZ", "YYY"]},  # no bars at all
        {"id": 2, "name": "Real", "symbols": ["AAA", "BBB"]},
    ]
    fake = _fake_optimize({"AAA": _fake_result(oos_pct=6.0, oos_trades=4)})
    out = sweep_baskets(baskets, {**BARS, "SPY": SPY_BARS}, {}, optimize_fn=fake)
    assert [r["basket_name"] for r in out["rows"]] == ["Real"]
    assert out["skipped"][0]["basket_name"] == "Ghost"
    assert "0 of 2" in out["skipped"][0]["reason"]


def test_missing_spy_marks_rows_untested_but_still_ranks_them():
    # No SPY data → no margin exists, so even a strong tested result must carry
    # the untested flag (there is nothing honest to rank it against) — but the
    # sweep still completes and assigns ranks rather than erroring.
    baskets = [{"id": 1, "name": "Alpha", "symbols": ["AAA"]}]
    fake = _fake_optimize({"AAA": _fake_result(oos_pct=12.0, oos_trades=5)})
    out = sweep_baskets(baskets, dict(BARS), {}, min_symbols=1, optimize_fn=fake)  # no SPY key
    assert out["spy_available"] is False
    row = out["rows"][0]
    assert row["spy_oos_pct"] is None and row["margin_vs_spy"] is None
    assert row["untested"] is True and row["rank"] == 1


def test_oos_entries_count_held_positions_as_tested():
    # A config that entered out-of-sample and HELD to the end has 0 closed trades
    # but real entries — that's tested, not untested (the post-forced-liquidation
    # rule: a held winner is a data point).
    result = _fake_result(oos_pct=9.0, oos_trades=0)
    result["best"]["out_of_sample"]["entries"] = 3  # 0 closed + 3 still open
    baskets = [{"id": 1, "name": "Holder", "symbols": ["AAA"]}]
    out = sweep_baskets(
        baskets, {**BARS, "SPY": SPY_BARS}, {}, min_symbols=1,
        optimize_fn=_fake_optimize({"AAA": result}),
    )
    assert out["rows"][0]["untested"] is False
    assert out["rows"][0]["margin_vs_spy"] == 4.0  # 9 − 5


def test_pct_over_window_baseline_and_missing_data():
    bars = _daily([100.0, 102.0, 104.0], "2026-05-01")
    # baseline = last close ≤ start (100 on 5/1), final = last close ≤ end (104)
    assert pct_over_window(bars, "2026-05-01T20:00:00+00:00", "2026-05-03T20:00:00+00:00") == 4.0
    # window starts before the data → no honest baseline
    assert pct_over_window(bars, "2026-04-01T00:00:00+00:00", "2026-05-03T00:00:00+00:00") is None
    assert pct_over_window([], OOS_START, OOS_END) is None


def test_sweep_runs_the_real_optimizer_end_to_end():
    # Integration: the genuine optimize() on synthetic bars — proves the wiring
    # (template strategy, kwargs, result-shape) without asserting on returns.
    closes = []
    p = 100.0
    for i in range(120):
        p = p * 1.012 if i % 2 else p * 0.997
        closes.append(round(p, 2))
    bars = {"AAA": _daily(closes), "BBB": _daily(closes), "SPY": _daily(closes)}
    baskets = [{"id": 7, "name": "Pair", "symbols": ["AAA", "BBB"]}]
    seen: list[tuple[int, int, str]] = []
    out = sweep_baskets(
        baskets, bars, {"max_trades_per_day": 10, "max_total_positions": 6,
                        "max_total_exposure_usd": 1e6, "max_daily_loss_usd": 1e6,
                        "max_daily_loss_pct": 100, "cooldown_hours_after_loss": 0,
                        "wash_sale_guard": "off"},
        iterations=5, optimize_fn=optimizer.optimize,
        progress=lambda d, t, n: seen.append((d, t, n)),
    )
    assert len(out["rows"]) == 1
    row = out["rows"][0]
    assert row["tested_combinations"] >= 1
    assert row["best_draft_params"] is not None
    assert row["spy_oos_pct"] is not None  # SPY series covered the OOS window
    assert seen[0] == (0, 1, "Pair") and seen[-1] == (1, 1, "")
