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

    def __call__(
        self, strategy, bars_by_symbol, risk, *,
        starting_cash=5000.0, spread_pct=0.1, market="stock", eligible_by_day=None,
    ):
        self.eligible_seen.append(eligible_by_day)
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
    assert set(nb) == set(optimizer.PARAM_SPACE)
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
    assert macd["slow"] in optimizer.MACD_PARAM_SPACE["macd_slow"]

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
