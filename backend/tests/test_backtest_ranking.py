"""The replay makes the LIVE engine's top-N cut.

The engine never evaluates a whole pool: it ranks a ranked strategy's symbols
every cycle and hands the entry rules only the best `top_n`
(engine._ranked_candidates). The backtester ranked nothing, so it evaluated
every symbol it was given — and every backtest and optimizer run of a ranked
strategy therefore drew candidates from a wider pool than live ever would.

These tests pin the four things that makes true: WHICH names are candidates,
IN WHAT ORDER, WHEN the ranking is recomputed, and what happens when the metric
cannot be computed at all.
"""

import copy
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from qt import security
from qt.api import backtest as backtest_api
from qt.broker.alpaca import SECRET_KEY_ID, SECRET_KEY_SECRET, AlpacaClient
from qt.db import session_scope
from qt.models import Basket, BasketItem, Strategy, StrategyConfigVersion, Trade
from qt.services import backtest as bt
from qt.services import stats
from qt.services.backtest import run_backtest, run_portfolio_backtest
from qt.services.engine import RISK_DEFAULTS

RISK = dict(RISK_DEFAULTS, max_total_exposure_usd=1_000_000, max_daily_loss_usd=1_000_000)

BASKET = {
    "asset_class": "stock",
    "universe": "basket",
    "rank_enabled": True,
    "rank_by": "momentum_today",
    "top_n": 1,
    "swing_mode": False,
    "sizing_usd": 1000.0,
    "sleeve_usd": 5000.0,
    "max_positions": 3,
    "params": {
        "entry": {"min_day_gain_pct": 3.0, "require_above_vwap": False},
        "exit": {"trailing_stop_pct": 0, "stop_loss_pct": 0, "take_profit_pct": 0,
                 "max_holding_hours": 0, "flatten_before_close": False},
    },
}


def _strategy(**over) -> dict:
    out = copy.deepcopy(BASKET)
    out.update(over)
    return out


def _hourly(closes: list[float], start: str) -> list[dict]:
    t0 = datetime.fromisoformat(start.replace("Z", "+00:00"))
    return [
        {"t": (t0 + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M:%SZ"),
         "c": c, "v": 1000, "vw": c}
        for i, c in enumerate(closes)
    ]


def _two_days(day1: list[float], day2: list[float]) -> list[dict]:
    """Day one sets the previous-session close; day two is where trading happens."""
    return _hourly(day1, "2026-05-04T14:00:00Z") + _hourly(day2, "2026-05-05T14:00:00Z")


def _daily(closes: list[float], start_day: str = "2026-03-01") -> list[dict]:
    t0 = datetime.fromisoformat(start_day + "T05:00:00+00:00")
    return [
        {"t": (t0 + timedelta(days=i)).strftime("%Y-%m-%dT%H:%M:%SZ"),
         "c": c, "h": c, "l": c, "v": 1000, "vw": c}
        for i, c in enumerate(closes)
    ]


def _bought(result: dict) -> set[str]:
    return {t["symbol"] for t in result["trade_list"]} | {
        p["symbol"] for p in result["open_positions"]
    }


# ── which names are candidates ────────────────────────────────────────────


def test_only_the_top_n_of_the_pool_is_ever_evaluated():
    """The bug, stated as a test. Three names all clear the 3% entry bar and the
    sleeve affords all three, so the ONLY thing that can keep two of them out is
    the ranking — which is exactly the thing live does and the replay didn't."""
    bars = {
        "AAA": _two_days([100, 100], [110, 110]),   # +10%
        "BBB": _two_days([100, 100], [106, 106]),   # +6%
        "CCC": _two_days([100, 100], [104, 104]),   # +4%
    }
    ranked = run_backtest(_strategy(top_n=1), bars, RISK, starting_cash=5000, spread_pct=0)
    assert _bought(ranked) == {"AAA"}

    # The control: the same bars with an UNRANKED universe buy all three. Without
    # this the test above would also pass if entries had simply stopped working.
    unranked = run_backtest(
        _strategy(universe="watchlist", rank_enabled=False), bars, RISK,
        starting_cash=5000, spread_pct=0,
    )
    assert _bought(unranked) == {"AAA", "BBB", "CCC"}
    assert unranked["ranking"] is None


def test_the_top_n_is_recomputed_at_every_evaluation_point():
    """Live ranks on every 60-second tick, so top-N MEMBERSHIP MOVES THROUGH THE
    DAY. A replay that ranked once for the window would be a different wrong
    answer: with top_n=1 only one of these two names could ever be a candidate,
    whichever way that single ranking fell."""
    bars = {
        # bar 1 of day two: AAA +10%, BBB +4% → AAA is the only candidate.
        # bar 2 of day two: AAA +5%,  BBB +9% → BBB is.
        "AAA": _two_days([100, 100], [110, 105]),
        "BBB": _two_days([100, 100], [104, 109]),
    }
    result = run_backtest(_strategy(top_n=1), bars, RISK, starting_cash=5000, spread_pct=0)
    entered = {p["symbol"]: p["entry_at"] for p in result["open_positions"]}
    assert set(entered) == {"AAA", "BBB"}
    # …and each on the bar it actually led, not merely "both eventually".
    assert entered["AAA"].startswith("2026-05-05T14:00")
    assert entered["BBB"].startswith("2026-05-05T15:00")


def test_the_best_ranked_name_gets_first_refusal_on_the_slot():
    """Live walks its candidates strictly best-first, so a max_positions cap bites
    from the BOTTOM of the ranking. ZZZ is the stronger name and AAA the earlier
    one alphabetically — evaluating in symbol order would hand the only slot to
    the weaker name."""
    bars = {
        "AAA": _two_days([100, 100], [105, 105]),   # +5%
        "ZZZ": _two_days([100, 100], [110, 110]),   # +10%
    }
    result = run_backtest(
        _strategy(top_n=2, max_positions=1), bars, RISK, starting_cash=5000, spread_pct=0
    )
    assert _bought(result) == {"ZZZ"}


def test_a_tie_after_live_s_rounding_breaks_on_the_symbol():
    """Live rounds momentum_today to 2dp in _pool_metrics BEFORE ranking, and the
    rounding is what sends near-ties to the deterministic symbol tie-break. On the
    raw numbers BBB (+5.004%) beats AAA (+5.001%); after rounding both read 5.0
    and AAA wins on the symbol."""
    bars = {
        "AAA": _two_days([100, 100], [105.001, 105.001]),
        "BBB": _two_days([100, 100], [105.004, 105.004]),
    }
    result = run_backtest(_strategy(top_n=1), bars, RISK, starting_cash=5000, spread_pct=0)
    assert _bought(result) == {"AAA"}


def test_a_symbol_whose_metric_is_missing_is_dropped_not_scored_as_zero():
    """Live's rule (ranking.rank_symbols): a symbol whose metric is None is
    dropped entirely — you cannot rank on data you don't have — rather than
    treated as a zero. The difference is visible here because AAA's 30-day return
    is NEGATIVE: score the metric-less name as 0 and it takes the only slot, drop
    it and AAA does. NOMET has a perfectly good day-gain, so an unranked replay
    would have evaluated it."""
    bars = {
        "AAA": _daily([100.0] * 30 + [95.0] * 9 + [96.0]),    # 30d return < 0
        "NOMET": _daily([100.0] * 39 + [120.0]),
    }
    strategy = _strategy(rank_by="return_30d", top_n=1)
    strategy["params"]["entry"]["min_day_gain_pct"] = 0.0
    sim_start = datetime.fromisoformat(bars["AAA"][-1]["t"].replace("Z", "+00:00"))
    result = run_backtest(
        strategy, bars, RISK, starting_cash=5000, spread_pct=0, sim_start=sim_start,
        # NOMET is deliberately absent: no daily history, so no metric.
        rank_daily_bars_by_symbol={"AAA": bars["AAA"]},
    )
    assert _bought(result) == {"AAA"}
    assert result["ranking"]["symbol_bars_unrankable"] == 1


# ── what the trade log and the entry reason say ───────────────────────────


def test_the_replay_stamps_the_rank_the_live_journal_stamps():
    bars = {
        "AAA": _two_days([100, 100], [110, 110]),
        "BBB": _two_days([100, 100], [106, 106]),
    }
    result = run_backtest(_strategy(top_n=2), bars, RISK, starting_cash=5000, spread_pct=0)
    by_symbol = {p["symbol"]: p for p in result["open_positions"]}
    assert (by_symbol["AAA"]["rank"], by_symbol["AAA"]["rank_of"]) == (1, 2)
    assert (by_symbol["BBB"]["rank"], by_symbol["BBB"]["rank_of"]) == (2, 2)
    # Reads exactly like the journalled reason (engine._rank_note), so the two
    # sides of a fidelity comparison don't differ over a sentence.
    assert ", ranked #2 of 2 by momentum today" in by_symbol["BBB"]["entry_reason"]


# ── metrics that need daily bars ──────────────────────────────────────────


def test_a_daily_bar_metric_ranks_on_daily_history_not_on_the_day_gain():
    """return_30d is scored from daily closes, and it must be able to pick a
    DIFFERENT name than momentum_today would — otherwise the metric is decorative.
    SLOW has the weaker day but the stronger 30-day run."""
    daily = {
        # 40 daily bars; the last is the replayed day. The 30-day base is the
        # close at index 9, so FAST measures 110/100 and SLOW measures 92/50.
        "FAST": _daily([100.0] * 39 + [110.0]),
        "SLOW": _daily([50.0] * 10 + [90.0] * 29 + [92.0]),
    }
    thirty_back = {
        s: stats.pct_change_over(b, 30, b[-1]["c"]) for s, b in daily.items()
    }
    day_gain = {s: b[-1]["c"] / b[-2]["c"] for s, b in daily.items()}
    assert thirty_back["SLOW"] > thirty_back["FAST"], "the two metrics must disagree"
    assert day_gain["FAST"] > day_gain["SLOW"], "the two metrics must disagree"

    strategy = _strategy(rank_by="return_30d", top_n=1)
    strategy["params"]["entry"]["min_day_gain_pct"] = 0.0
    result = run_backtest(
        strategy, daily, RISK, starting_cash=5000, spread_pct=0,
        sim_start=datetime.fromisoformat(daily["FAST"][-1]["t"].replace("Z", "+00:00")),
    )
    assert _bought(result) == {"SLOW"}
    assert result["ranking"]["applied"] is True
    assert result["ranking"]["metric_source"] == "daily bars"

    # The control: the SAME bars ranked by the day-gain pick the other name.
    by_day_gain = _strategy(rank_by="momentum_today", top_n=1)
    by_day_gain["params"]["entry"]["min_day_gain_pct"] = 0.0
    other = run_backtest(
        by_day_gain, daily, RISK, starting_cash=5000, spread_pct=0,
        sim_start=datetime.fromisoformat(daily["FAST"][-1]["t"].replace("Z", "+00:00")),
    )
    assert _bought(other) == {"FAST"}


def test_a_daily_metric_never_reads_the_bar_s_own_day():
    """The look-ahead rule, probed where it shows: Wilder's RSI reads the whole
    prefix, so leaking day D's own daily close into it changes the number. The
    expected value comes from stats.rsi over the completed bars only — the same
    reference the live engine calls."""
    closes = [100.0, 101.0, 99.0, 103.0, 98.0, 105.0, 97.0, 106.0,
              96.0, 107.0, 95.0, 108.0, 94.0, 109.0, 93.0, 110.0, 92.0, 111.0]
    # Daily bars for 2026-03-01 … 03-18, then day D (03-19) closing wildly out of
    # line. The replay itself runs on an INTRADAY bar of day D, so the current
    # price and day D's own daily close are genuinely different numbers — which is
    # what makes a leak visible at all.
    daily = _daily(closes + [500.0], "2026-03-01")
    intraday = _hourly([111.0, 112.0], "2026-03-19T14:00:00Z")
    prepared = {"AAA": bt._prepare(intraday, bt._et_day)}
    ranker = bt._PoolRanker("rsi", 1, prepared, {"AAA": daily}, bt._et_day)

    bar = prepared["AAA"][0]
    price = bar["close"]
    expected = stats.rsi([{"c": c} for c in closes] + [{"c": price}], 14, price)
    assert ranker._value("AAA", bar, bar["ts"]) == expected
    # And it is NOT the value you get by letting day D's own close in.
    leaked = stats.rsi(
        [{"c": c} for c in closes] + [{"c": 500.0}, {"c": price}], 14, price
    )
    assert expected != leaked


def test_a_daily_metric_with_no_daily_bars_is_reported_not_swallowed():
    """A metric the replay cannot compute must NOT quietly become "no ranking" —
    that is the original bug wearing a hat. The run says so, and says it in terms
    of what it means: the pool it drew from was wider than live's."""
    bars = {
        "AAA": _two_days([100, 100], [110, 110]),
        "BBB": _two_days([100, 100], [106, 106]),
        "CCC": _two_days([100, 100], [104, 104]),
    }
    result = run_backtest(
        _strategy(rank_by="relative_strength", top_n=1), bars, RISK,
        starting_cash=5000, spread_pct=0,
    )
    assert result["ranking"]["applied"] is False
    assert "relative strength" in result["ranking"]["warning"]
    # …and the warning is TRUE: with no cut, all three were evaluated.
    assert _bought(result) == {"AAA", "BBB", "CCC"}


def test_rs_vs_spy_without_the_benchmark_still_makes_live_s_cut():
    """SPY's return is subtracted from every member alike, so it shifts all the
    values by one constant and cannot reorder them. The cut is therefore still
    live's; only the printed values differ, and the run says which."""
    daily = {
        "AAA": _daily([100.0] * 95 + [140.0] * 4 + [141.0]),
        "BBB": _daily([100.0] * 95 + [110.0] * 4 + [111.0]),
    }
    strategy = _strategy(rank_by="rs_vs_spy", top_n=1)
    strategy["params"]["entry"]["min_day_gain_pct"] = 0.0
    sim_start = datetime.fromisoformat(daily["AAA"][-1]["t"].replace("Z", "+00:00"))
    result = run_backtest(
        strategy, daily, RISK, starting_cash=5000, spread_pct=0, sim_start=sim_start
    )
    assert _bought(result) == {"AAA"}
    assert result["ranking"]["applied"] is True
    assert result["ranking"]["benchmark_missing"] is True
    assert "S&P 500" in result["ranking"]["warning"]


def test_the_benchmark_is_used_when_it_is_supplied():
    """With SPY present the metric is out-performance, not the member's own
    return: both names beat their own past, only one beats SPY."""
    daily = {
        "AAA": _daily([100.0] * 95 + [140.0] * 4 + [141.0]),
        "BBB": _daily([100.0] * 95 + [110.0] * 4 + [111.0]),
    }
    spy = _daily([100.0] * 95 + [120.0] * 4 + [121.0])
    prepared = {s: bt._prepare(b, bt._et_day) for s, b in daily.items()}
    ranker = bt._PoolRanker(
        "rs_vs_spy", 1, prepared, {**daily, "SPY": spy}, bt._et_day
    )
    assert ranker._spy_missing is False
    values = {
        s: ranker._value(s, prepared[s][-1], prepared[s][-1]["ts"]) for s in prepared
    }
    assert values["AAA"] > 0 > values["BBB"], values


# ── the universes that must NOT change ────────────────────────────────────


def test_a_scanner_universe_is_left_alone():
    """scanner/both are already ordered by the scan, and the API forces
    rank_enabled off for them. Their replay-side mechanism is eligible_by_day,
    which this change must not touch."""
    assert bt.ranking_config({"universe": "scanner", "rank_enabled": False}) is None
    assert bt.ranking_config({"universe": "both", "rank_enabled": False}) is None
    bars = {
        "AAA": _two_days([100, 100], [110, 110]),
        "BBB": _two_days([100, 100], [106, 106]),
    }
    result = run_backtest(
        _strategy(universe="scanner", rank_enabled=False), bars, RISK,
        starting_cash=5000, spread_pct=0,
        eligible_by_day={"2026-05-05": {"BBB"}},
    )
    assert result["ranking"] is None
    assert _bought(result) == {"BBB"}


def test_a_basket_ranks_even_when_the_row_says_it_should_not():
    """qt.api.strategies forces rank_enabled ON for a basket, so a row (or an old
    snapshot) carrying False is not a request to skip the cut."""
    assert bt.ranking_config({"universe": "basket", "rank_enabled": False}) == (
        "momentum_today", 10,
    )
    assert bt.ranking_config({"universe": "custom", "rank_enabled": True, "top_n": 4}) == (
        "momentum_today", 4,
    )
    assert bt.ranking_config({"universe": "custom", "rank_enabled": False}) is None


# ── the portfolio run ─────────────────────────────────────────────────────


def test_each_strategy_in_a_portfolio_ranks_its_own_universe():
    """One book, two strategies: the ranked one is cut to its top-N while the
    unranked one beside it still evaluates everything it holds."""
    ranked = {
        **_strategy(top_n=1), "id": 1, "name": "Ranked", "max_positions": 3,
    }
    plain = {
        **_strategy(universe="watchlist", rank_enabled=False), "id": 2, "name": "Plain",
        "max_positions": 3,
    }
    bars = {
        1: {"AAA": _two_days([100, 100], [110, 110]),
            "BBB": _two_days([100, 100], [106, 106])},
        2: {"CCC": _two_days([100, 100], [109, 109]),
            "DDD": _two_days([100, 100], [105, 105])},
    }
    result = run_portfolio_backtest(
        [ranked, plain], bars, RISK, starting_cash=20000, spread_pct=0
    )
    held = {p["symbol"]: p["strategy_name"] for p in result["open_positions"]}
    assert set(held) == {"AAA", "CCC", "DDD"}
    assert [r["strategy_id"] for r in result["ranking"]] == [1]
    assert result["ranking"][0]["top_n"] == 1


# ── the optimizer, which is just a lot of backtests ───────────────────────


def test_the_search_scores_every_config_against_the_ranked_pool():
    """The optimizer runs run_backtest dozens of times, so a search of a ranked
    strategy was tuning against a more generous world than the live one. The cut
    has to reach it, and the search has to report which cut it ran under."""
    from qt.services import optimizer

    bars = {
        "AAA": _two_days([100, 100], [110, 110]) + _hourly([110, 110], "2026-05-06T14:00:00Z"),
        "BBB": _two_days([100, 100], [106, 106]) + _hourly([106, 106], "2026-05-06T14:00:00Z"),
    }
    seen: list[dict] = []

    def spy(strategy, bars_by_symbol, risk, **kw):
        result = run_backtest(strategy, bars_by_symbol, risk, **kw)
        seen.append(result)
        return result

    out = optimizer.optimize(
        _strategy(top_n=1), bars, RISK, iterations=2, seed=1, backtest_fn=spy
    )
    assert out["ranking"]["applied"] is True
    assert out["ranking"]["top_n"] == 1
    # Every single run the search scored was cut, not just the reported one.
    assert seen and all(r["ranking"]["applied"] for r in seen)
    assert all("BBB" not in _bought(r) for r in seen)


def test_a_search_that_could_not_rank_says_so_in_its_warnings():
    """A search whose ranking metric could not be computed scored every config
    against a wider pool than live would offer. That belongs beside the results,
    not only inside one of them."""
    from qt.services import optimizer

    bars = {
        "AAA": _two_days([100, 100], [110, 110]) + _hourly([110, 110], "2026-05-06T14:00:00Z"),
        "BBB": _two_days([100, 100], [106, 106]) + _hourly([106, 106], "2026-05-06T14:00:00Z"),
    }
    out = optimizer.optimize(
        _strategy(rank_by="relative_strength", top_n=1), bars, RISK, iterations=2, seed=1
    )
    assert out["ranking"]["applied"] is False
    assert any("relative strength" in w for w in out["warnings"])


# ── the config the replay is handed ───────────────────────────────────────


@pytest.fixture()
def configured(client):
    with session_scope() as s:
        security.set_secret(s, SECRET_KEY_ID, "k")
        security.set_secret(s, SECRET_KEY_SECRET, "s")
    yield
    with session_scope() as s:
        s.query(Trade).delete()
        s.query(StrategyConfigVersion).delete()
        s.query(Strategy).delete()
        security.delete_secret(s, SECRET_KEY_ID)
        security.delete_secret(s, SECRET_KEY_SECRET)


@pytest.fixture()
def basket():
    """A throwaway basket, torn down BY ID. The DB is shared across the suite and
    the starter baskets live in it — clearing the table wholesale here made
    test_baskets re-seed twelve of them and fail two tests away from anything
    this file touches."""
    with session_scope() as s:
        row = Basket(name="ranking test basket")
        s.add(row)
        s.flush()
        bid = row.id
    yield bid
    with session_scope() as s:
        s.query(BasketItem).filter(BasketItem.basket_id == bid).delete()
        s.query(Basket).filter(Basket.id == bid).delete()


def _api_strategy(**over) -> dict:
    body = {
        "name": "ranked basket",
        "asset_class": "stock",
        "universe": "basket",
        "preset": "custom",
        "rank_by": "momentum_today",
        "top_n": 1,
        "rank_enabled": False,   # the API forces this ON for a basket
        "params": {
            "entry": {"min_day_gain_pct": 3, "require_above_vwap": False,
                      "entry_window_start": None, "entry_window_end": None},
            # A hard stop is mandatory on a saved strategy; 50% is far enough
            # away that these flat synthetic prices never reach it.
            "exit": {"trailing_stop_pct": 0, "stop_loss_pct": 50, "take_profit_pct": 0,
                     "max_holding_hours": 0, "flatten_before_close": False,
                     "exit_below_vwap": False},
        },
        "sizing_usd": 1000, "sleeve_usd": 5000, "max_positions": 3,
        "swing_mode": False, "ignore_regime": True,
    }
    body.update(over)
    return body


def test_the_replay_config_carries_the_ranking(client, configured, basket):
    """replay_strategy is the ONE reader both a live row and a frozen config
    version go through. If the ranking doesn't come out of it, nothing downstream
    can make the cut."""
    sid = client.post(
        "/api/strategies", json=_api_strategy(basket_id=basket, top_n=4)
    ).json()["id"]
    with session_scope() as s:
        config = backtest_api.replay_strategy(s.get(Strategy, sid))
    assert (config["rank_by"], config["top_n"], config["rank_enabled"]) == (
        "momentum_today", 4, True,
    )
    assert bt.ranking_config(config) == ("momentum_today", 4)


def test_a_config_version_written_before_ranking_existed_still_ranks():
    """An old snapshot simply lacks the keys. Reading them as None would mean "no
    ranking" — the exact permissive behaviour this change removes — so they fall
    back to the model's own defaults instead."""
    old = {
        "asset_class": "stock", "universe": "basket", "swing_mode": True,
        "sizing_usd": 100, "sleeve_usd": 1000, "max_positions": 2, "params": {},
    }
    config = backtest_api.replay_strategy(old)
    assert (config["rank_by"], config["top_n"]) == ("momentum_today", 10)
    assert bt.ranking_config(config) == ("momentum_today", 10)

    # And the same for a row from a partly-migrated DB, where the columns exist
    # but are NULL — `get(key, default)` hands back the None, not the default.
    nulled = backtest_api.replay_strategy({**old, "rank_by": None, "top_n": None})
    assert (nulled["rank_by"], nulled["top_n"]) == ("momentum_today", 10)


def test_the_endpoint_replays_a_basket_through_its_ranking(client, configured, basket):
    """End to end: the /api/backtest handler must hand the cut all the way down.
    Three names clear the entry bar; the top_n=1 basket may buy only the best."""
    with session_scope() as s:
        for sym in ("AAA", "BBB", "CCC"):
            s.add(BasketItem(basket_id=basket, symbol=sym, asset_class="stock"))
    sid = client.post("/api/strategies", json=_api_strategy(basket_id=basket)).json()["id"]

    start = datetime.now(timezone.utc) - timedelta(days=9)

    def series(gain: float) -> list[dict]:
        out = []
        for i, close in enumerate([100.0, 100.0, 100.0 * (1 + gain), 100.0 * (1 + gain)]):
            ts = start + timedelta(days=i)
            out.append({"t": ts.strftime("%Y-%m-%dT%H:%M:%SZ"), "o": close,
                        "h": close, "l": close, "c": close, "v": 1000, "vw": close})
        return out

    bars = {"AAA": series(0.10), "BBB": series(0.06), "CCC": series(0.04)}
    with patch.object(AlpacaClient, "historical_bars", new=AsyncMock(return_value=bars)):
        body = client.post(
            "/api/backtest",
            json={"strategy_id": sid, "symbols": ["AAA", "BBB", "CCC"], "days": 8,
                  "timeframe": "1Day", "starting_cash": 5000, "spread_pct": 0},
        ).json()
    assert body["ranking"]["applied"] is True
    assert body["ranking"]["top_n"] == 1
    assert _bought(body) == {"AAA"}
