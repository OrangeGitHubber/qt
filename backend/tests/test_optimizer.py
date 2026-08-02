"""Parameter-search tests. The whole point of the search is that it fights
overfitting, so the tests pin the anti-overfitting guarantees: the
out-of-sample split is actually applied, the combination count is reported, and
the search runs entirely on an INJECTED fake backtest fn (no network, no
Alpaca)."""

from datetime import datetime, timedelta

from qt.services import optimizer


def _bars(n_days: int, start: str = "2026-01-05T14:00:00Z", base: float = 100.0) -> list[dict]:
    """One daily bar per day, gently rising — enough that every combo can trade."""
    t0 = datetime.fromisoformat(start.replace("Z", "+00:00"))
    out = []
    price = base
    for i in range(n_days):
        ts = t0 + timedelta(days=i)
        price = price * 1.01 if i % 2 else price * 0.995
        out.append({"t": ts.strftime("%Y-%m-%dT%H:%M:%SZ"), "c": round(price, 2), "v": 10000, "vw": round(price, 2)})
    return out


BASE_STRATEGY = {
    "asset_class": "stock",
    "swing_mode": False,
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
            "take_profit_pct": 0.0,
            "max_holding_hours": 0,
            "flatten_before_close": False,
            "exit_below_vwap": False,
        },
    },
}


class RecordingFake:
    """A fake backtest fn that records the time-window it was called with, and
    returns a deterministic score keyed off the combo — so a known winner emerges
    and we can assert exactly which slices the search ran on."""

    def __init__(self):
        self.windows: set[tuple[str, str]] = set()
        self.combos_seen: list[tuple] = []
        self.eligible_seen: list = []
        self.sim_starts: list = []

    def __call__(
        self, strategy, bars_by_symbol, risk, *,
        starting_cash=5000.0, spread_pct=0.1, market="stock", eligible_by_day=None,
        sim_start=None,
    ):
        self.eligible_seen.append(eligible_by_day)
        self.sim_starts.append(sim_start)
        ts = sorted(b["t"] for series in bars_by_symbol.values() for b in series)
        window = (ts[0], ts[-1])
        self.windows.add(window)
        exit_rules = strategy["params"]["exit"]
        entry = strategy["params"]["entry"]
        combo = (
            entry["min_day_gain_pct"],
            exit_rules["trailing_stop_pct"],
            exit_rules["stop_loss_pct"],
            exit_rules["take_profit_pct"],
        )
        self.combos_seen.append(combo)
        # Deterministic "return": peaks when trailing_stop is high — a clear winner.
        score = exit_rules["trailing_stop_pct"] * 2 - entry["min_day_gain_pct"]
        return {
            "net_pnl_pct": round(score, 2),
            "trades": 10,
            "win_rate": 55.0,
            "return_on_deployed_pct": round(score * 1.5, 2),
            "max_drawdown_pct": 3.0,
            "hold_benchmark": [0.0, 2.0, 4.0],
            "hold_benchmark_label": "TEST",
        }


def _run(iterations=20, **kw):
    fake = RecordingFake()
    bars = {"AAA": _bars(100), "BBB": _bars(100)}
    result = optimizer.optimize(
        BASE_STRATEGY, bars, {}, iterations=iterations, seed=7, backtest_fn=fake, **kw
    )
    return result, fake


def test_out_of_sample_split_is_applied():
    # The search must run on TWO disjoint, chronological windows: an in-sample
    # slice (earlier) and an out-of-sample slice (strictly later) it validates on.
    result, fake = _run()
    assert len(fake.windows) == 2, "expected exactly an in-sample and an out-of-sample window"
    windows = sorted(fake.windows)
    in_window, out_window = windows[0], windows[1]
    # No overlap, and out-of-sample is the LATER slice.
    assert in_window[1] < out_window[0]
    # The result reports both windows separately.
    assert result["in_sample_window"]["start"] < result["out_of_sample_window"]["start"]
    assert result["in_sample_window"]["end"] <= result["out_of_sample_window"]["start"]


def test_warmup_gives_each_slice_its_own_indicator_history():
    # The same dead-zone bug as the backtest, but WORSE for the optimizer: without
    # warm-up the out-of-sample slice starts mid-window with no prior bars, so a
    # daily MACD/RSI signal is undefined for its first ~35 bars — and that slice IS
    # the honest verdict. In warm-up mode every slice must carry a history prefix.
    fake = RecordingFake()
    bars = {"AAA": _bars(60)}  # first 20 bars are warm-up; the window is the last 40
    window_start = datetime.fromisoformat(bars["AAA"][20]["t"].replace("Z", "+00:00"))
    optimizer.optimize(
        BASE_STRATEGY, bars, {}, iterations=8, seed=1,
        backtest_fn=fake, sim_start=window_start,
    )
    first_bar = bars["AAA"][0]["t"]
    # Every slice the search ran started its BARS at the very first (warm-up) bar,
    # so both the in-sample and out-of-sample runs had indicator history before
    # their first traded bar — no dead zone in either.
    assert all(w[0] == first_bar for w in fake.windows)
    # Two distinct trading starts: the window start (in-sample) and the split
    # boundary (out-of-sample). Neither is None — warm-up gates trading on both.
    assert None not in fake.sim_starts
    assert window_start in fake.sim_starts
    assert len(set(fake.sim_starts)) == 2


def test_no_warmup_keeps_slices_disjoint_and_ungated():
    # Backward-compat: with no sim_start the split is unchanged — disjoint slices,
    # no warm-up, and the fake is never handed a sim_start (protects the plain
    # injected-fake signature everywhere else).
    _, fake = _run()
    assert len(fake.windows) == 2
    assert set(fake.sim_starts) == {None}


def test_score_counts_held_to_end_positions_as_entries():
    # Since the backtest stopped force-selling at the window end, a config that
    # entered and HELD has trades == 0 — but it deployed real capital N times.
    # The min-trades gate must count entries (closed + still open), or buy-and-
    # hold-flavoured configs silently become unscoreable.
    held = {"net_pnl_pct": 7.5, "trades": 1, "open_positions": [{}, {}]}
    assert optimizer._score(held, min_trades=3) == 7.5  # 1 closed + 2 open = 3
    thin = {"net_pnl_pct": 7.5, "trades": 1, "open_positions": []}
    assert optimizer._score(thin, min_trades=3) is None
    # And _metrics surfaces the same number for the UI / sweep.
    assert optimizer._metrics(held)["entries"] == 3


def test_combination_count_is_reported_and_matches_distinct_combos():
    result, fake = _run(iterations=15)
    # tested_combinations counts DISTINCT configs (random draws de-duped + the
    # local refinement sweep). Out-of-sample validation only re-runs a subset of
    # combos already counted in-sample, so the set of ALL distinct combos the fake
    # ever saw equals the reported count.
    assert result["tested_combinations"] >= 1
    assert result["tested_combinations"] == len(set(fake.combos_seen))


def test_search_uses_injected_fake_no_network():
    # If the injected fake is used, its call count is non-zero and no real
    # backtest / Alpaca client is touched. The fake raising would surface here.
    result, fake = _run(iterations=12)
    assert len(fake.combos_seen) > 0
    assert result["best_draft_params"]["exit"]["stop_loss_pct"] > 0  # a valid, mandatory stop


def test_winner_is_the_top_ranked_and_validated_out_of_sample():
    # The best result must be rank 1, flagged is_best, carry an out-of-sample
    # result (the real number), and have the highest in-sample score of all
    # reported configs.
    result, _ = _run(iterations=25)
    best = result["best"]
    assert best["rank"] == 1
    assert best["is_best"] is True
    assert best["out_of_sample"] is not None
    scores = [r["in_sample_score"] for r in result["results"] if r["in_sample_score"] is not None]
    assert best["in_sample_score"] == max(scores)
    # The fake rewards a high trailing stop, so the winner leans that way.
    assert result["best_draft_params"]["exit"]["trailing_stop_pct"] >= 6.0


def test_neighbourhood_reports_scores_around_the_winner():
    result, _ = _run()
    nb = result["neighbourhood"]
    # Every knob the strategy actually uses is reported. Grids are anchored on
    # the strategy now, so the set is "what's switched on", not a fixed list —
    # this fixture's take-profit is 0 (off), and a percentage step from zero is
    # still zero, so it isn't searched and doesn't appear.
    assert set(nb) == {"min_day_gain_pct", "trailing_stop_pct", "stop_loss_pct"}
    # Each knob reports the winner plus at least one neighbour value.
    for key, points in nb.items():
        assert any(p["is_best"] for p in points)
        assert len(points) >= 2


def test_hold_benchmark_comparison_present():
    result, _ = _run()
    hb = result["hold_benchmark_comparison"]
    assert "strategy_out_of_sample_pct" in hb
    assert hb["hold_out_of_sample_pct"] == 4.0  # last non-None of the fake's series
    assert hb["beat_hold"] in (True, False)


def test_single_symbol_warns_about_generalization():
    fake = RecordingFake()
    result = optimizer.optimize(
        BASE_STRATEGY, {"AAA": _bars(100)}, {}, iterations=10, seed=1, backtest_fn=fake
    )
    assert any("one symbol" in w for w in result["warnings"])


def test_no_trade_reason_surfaced_when_nothing_trades():
    # When every combo makes 0 trades, the optimizer surfaces the backtest's
    # diagnosis so a 0-trade result isn't mistaken for "the strategy is unworkable".
    def zero_trades(strategy, bars_by_symbol, risk, *, starting_cash=5000.0, spread_pct=0.1,
                    market="stock", eligible_by_day=None):
        return {
            "net_pnl_pct": 0.0, "trades": 0, "win_rate": None, "return_on_deployed_pct": 0.0,
            "max_drawdown_pct": 0.0, "hold_benchmark": [0.0], "hold_benchmark_label": "TEST",
            "diagnosis": {"summary": "reached the gain threshold but the 'price above VWAP' "
                          "condition rejected the qualifying bars."},
        }

    bars = {"AAA": _bars(100), "BBB": _bars(100)}
    result = optimizer.optimize(BASE_STRATEGY, bars, {}, iterations=8, seed=3, backtest_fn=zero_trades)
    assert result["no_trade_reason"] and "VWAP" in result["no_trade_reason"]


def test_rsi_knob_searched_only_when_the_strategy_uses_rsi():
    # A strategy with an RSI entry cap on → the optimizer adds rsi_max to the
    # search space (and thus the neighbourhood report).
    base = {
        **BASE_STRATEGY,
        "params": {
            "entry": {**BASE_STRATEGY["params"]["entry"], "rsi_max": 70.0},
            "exit": {**BASE_STRATEGY["params"]["exit"]},
        },
    }
    fake = RecordingFake()
    bars = {"AAA": _bars(100), "BBB": _bars(100)}
    result = optimizer.optimize(base, bars, {}, iterations=20, seed=7, backtest_fn=fake)
    assert "rsi_max" in result["neighbourhood"]
    assert "exit_rsi_above" not in result["neighbourhood"]  # that RSI rule wasn't on

    # A vanilla strategy (no RSI rules) searches exactly the core four — no RSI.
    plain, _ = _run()
    assert "rsi_max" not in plain["neighbourhood"]
    assert "exit_rsi_above" not in plain["neighbourhood"]


def test_macd_speed_searched_only_when_the_strategy_uses_macd():
    # A MACD strategy → the optimizer searches the MACD speed (slow-EMA) knob, and
    # the winning draft carries a valid scaled MACD block (fast < slow).
    base = {
        **BASE_STRATEGY,
        "params": {
            "entry": {**BASE_STRATEGY["params"]["entry"], "require_macd_bullish": True},
            "exit": {**BASE_STRATEGY["params"]["exit"]},
            # A real MACD strategy that never customized its periods serializes
            # "macd": null — this must NOT crash _apply_combo (regression guard).
            "macd": None,
        },
    }
    fake = RecordingFake()
    bars = {"AAA": _bars(100), "BBB": _bars(100)}
    result = optimizer.optimize(base, bars, {}, iterations=20, seed=7, backtest_fn=fake)
    assert "macd_slow" in result["neighbourhood"]
    macd = result["best_draft_params"]["macd"]
    assert macd["fast"] < macd["slow"]
    # The period is a step off the strategy's OWN slow period (26 by default when
    # the block is null), not a value from a fixed list.
    assert macd["slow"] in [
        int(round(v)) for v in optimizer._geometric_grid(26, 0.15, optimizer.KNOB_BOUNDS["macd_slow"])
    ]

    # A plain strategy (no MACD) never searches MACD speed.
    plain, _ = _run()
    assert "macd_slow" not in plain["neighbourhood"]


def test_rsi_min_searched_when_the_strategy_uses_a_floor():
    base = {
        **BASE_STRATEGY,
        "params": {
            "entry": {**BASE_STRATEGY["params"]["entry"], "rsi_min": 40.0},
            "exit": {**BASE_STRATEGY["params"]["exit"]},
        },
    }
    fake = RecordingFake()
    bars = {"AAA": _bars(100), "BBB": _bars(100)}
    result = optimizer.optimize(base, bars, {}, iterations=15, seed=3, backtest_fn=fake)
    assert "rsi_min" in result["neighbourhood"]


def test_eligible_by_day_is_threaded_to_every_backtest():
    # Scanner-replay mode: the same eligible-by-day map (each day's top-N risers)
    # must reach EVERY backtest call — in-sample and out-of-sample alike — so the
    # search optimizes against the strategy's real, day-varying universe.
    eligible = {"2026-01-05": {"AAA"}, "2026-01-06": {"BBB"}}
    result, fake = _run(eligible_by_day=eligible)
    assert fake.eligible_seen, "the fake was never called"
    assert all(e is eligible for e in fake.eligible_seen)
    # And the plain (fixed-universe) path passes None, unchanged.
    _, fake2 = _run()
    assert all(e is None for e in fake2.eligible_seen)


class MixedFake:
    """A fake backtest fn for MIXED-RESOLUTION searches: records the intraday
    replay window it was handed AND the daily indicator series that came with it,
    so the tests can prove the daily series was never sliced up."""

    def __init__(self):
        self.calls: list[dict] = []

    def __call__(
        self, strategy, bars_by_symbol, risk, *,
        starting_cash=5000.0, spread_pct=0.1, market="stock", eligible_by_day=None,
        sim_start=None, daily_bars_by_symbol=None,
    ):
        ts = sorted(b["t"] for series in bars_by_symbol.values() for b in series)
        self.calls.append({
            "window": (ts[0], ts[-1]),
            "daily": daily_bars_by_symbol,
            "sim_start": sim_start,
        })
        return {
            "net_pnl_pct": 5.0, "trades": 10, "win_rate": 55.0,
            "return_on_deployed_pct": 7.5, "max_drawdown_pct": 3.0,
            "hold_benchmark": [0.0, 2.0], "hold_benchmark_label": "TEST",
        }


def test_mixed_resolution_daily_bars_are_never_split():
    # THE REGRESSION THIS PINS: split_in_out_of_sample splits the INTRADAY replay
    # timeline. The daily series is the indicator SOURCE, not a timeline — slice it
    # and the out-of-sample half loses its MACD/RSI history, every indicator comes
    # back None, and the honest verdict number silently collapses to "no trades".
    # So BOTH slices must receive the WHOLE daily series (look-ahead safety is
    # enforced downstream by _daily_frontier's per-day cutoff, not by slicing).
    fake = MixedFake()
    intraday = {"AAA": _bars(60, start="2026-03-01T14:00:00Z")}
    daily = {"AAA": _bars(200, start="2025-09-01T14:00:00Z")}  # reaches back over warm-up
    optimizer.optimize(
        BASE_STRATEGY, intraday, {}, iterations=6, seed=5,
        backtest_fn=fake, daily_bars_by_symbol=daily,
    )
    assert fake.calls, "the fake was never called"
    # Both an in-sample and an out-of-sample slice really ran (two distinct windows).
    assert len({c["window"] for c in fake.calls}) == 2
    for call in fake.calls:
        got = call["daily"]
        assert got is not None, "a slice ran with no daily indicator source"
        assert list(got) == list(daily)
        # Whole series, both ends: a split would trim one or the other.
        assert len(got["AAA"]) == len(daily["AAA"])
        assert got["AAA"][0]["t"] == daily["AAA"][0]["t"]
        assert got["AAA"][-1]["t"] == daily["AAA"][-1]["t"]


def test_mixed_resolution_daily_bars_reach_the_out_of_sample_slice():
    # Same guarantee, aimed squarely at the LATER slice — the one that produces the
    # only number the optimizer treats as real. Identified by its own sim_start
    # (the split boundary), not by position in the call list.
    fake = MixedFake()
    intraday = {"AAA": _bars(60, start="2026-03-01T14:00:00Z")}
    daily = {"AAA": _bars(200, start="2025-09-01T14:00:00Z")}
    window_start = datetime.fromisoformat(intraday["AAA"][0]["t"].replace("Z", "+00:00"))
    optimizer.optimize(
        BASE_STRATEGY, intraday, {}, iterations=6, seed=5, backtest_fn=fake,
        daily_bars_by_symbol=daily, sim_start=window_start,
    )
    oos_calls = [c for c in fake.calls if c["sim_start"] != window_start]
    assert oos_calls, "no out-of-sample slice ran"
    for call in oos_calls:
        assert len(call["daily"]["AAA"]) == len(daily["AAA"])


def test_daily_bars_none_keeps_the_plain_backtest_signature():
    # Backward-compat: with no daily series the kwarg is not passed at all, so a
    # fake (or any caller) with the pre-mixed signature keeps working untouched.
    result, fake = _run()  # RecordingFake has NO daily_bars_by_symbol parameter
    assert fake.combos_seen
    assert result["tested_combinations"] >= 1
