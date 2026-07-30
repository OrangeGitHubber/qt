"""Strategy parameter search (Phase 4): sweep a momentum strategy's parameter
space with the EXISTING backtester to find configs that actually held up — a
disciplined alternative to hand-guessing numbers.

This is a PARAMETER SEARCH, not "AI". It is deliberately built to fight
overfitting, because the easiest thing in the world is to find a knob setting
that looks brilliant on one slice of history and falls apart the moment the
market does something new:

- OUT-OF-SAMPLE ALWAYS. The search only ever sees the first ~70% of the window
  (in-sample). Every reported winner is then re-run on the final ~30% the
  search never touched (out-of-sample). Only the out-of-sample number is real.
- IT COUNTS THE COINS. `tested_combinations` — how many distinct configs were
  actually run — is always returned, so a "winner" out of 12 tries reads very
  differently from a winner out of 2,000.
- PLATEAUS, NOT PEAKS. Around the winner we sweep each parameter one step either
  way and report those neighbouring scores. A good setting sits on a plateau
  (its neighbours score similarly); a lone spike surrounded by bad neighbours is
  noise dressed up as signal.
- vs BUY-AND-HOLD. The winner's out-of-sample return is put next to simply
  holding the same symbols — if it can't beat that, the trading destroyed value.

The result is a HYPOTHESIS, returned as draft parameters the user can edit; it
never enables anything. It still has to earn its way up the shadow -> paper
ladder like any other strategy.

Reuses qt.services.backtest.run_backtest UNCHANGED. The search function accepts
an injected backtest fn so it is unit-testable with no network / no Alpaca.
"""

from __future__ import annotations

import copy
import random
from datetime import datetime
from typing import Callable

from qt.services.backtest import run_backtest

# The coarse grids the random search draws from. Discrete (not continuous) so a
# "neighbour" is well-defined — the plateau sweep steps to the adjacent value on
# each grid. Every stop_loss value is > 0: a hard stop is mandatory, so the best
# draft is always a valid strategy. Exhaustive would be 8*7*6*7 = 2,352 combos;
# we random-sample a small K of them, then refine locally (coarse-to-fine).
PARAM_SPACE: dict[str, list[float]] = {
    "min_day_gain_pct": [1.0, 1.5, 2.0, 3.0, 4.0, 5.0, 7.0, 10.0],
    "trailing_stop_pct": [2.0, 3.0, 4.0, 5.0, 6.0, 8.0, 10.0],
    "stop_loss_pct": [2.0, 3.0, 4.0, 5.0, 6.0, 8.0],
    "take_profit_pct": [0.0, 5.0, 8.0, 10.0, 12.0, 15.0, 20.0],
}

# RSI/MACD knobs are searched ONLY when the base strategy already uses that
# signal, so the optimizer tunes the factors you're using rather than inventing
# new ones. For RSI, 0.0 stays in range so the search can also decide the
# threshold isn't helping and turn it back off.
RSI_PARAM_SPACE: dict[str, list[float]] = {
    "rsi_max": [0.0, 60.0, 65.0, 70.0, 75.0, 80.0],       # entry: skip overbought
    "rsi_min": [0.0, 30.0, 40.0, 50.0, 55.0, 60.0],       # entry: require some strength
    "exit_rsi_above": [0.0, 65.0, 70.0, 75.0, 80.0, 85.0],  # exit: sell on froth
}

# MACD tuning = the LAG knob. We search the slow-EMA period (lower = a faster,
# less-laggy MACD) and derive the fast line from the strategy's own fast/slow
# ratio, so the whole MACD scales faster/slower while keeping its shape — always
# valid (fast < slow), one clean numeric knob that fits the plateau report.
MACD_PARAM_SPACE: dict[str, list[float]] = {
    "macd_slow": [13.0, 17.0, 21.0, 26.0, 34.0],
}

# Which strategy-params section each searchable knob belongs to (macd_slow is
# routed to the `macd` block specially in _apply_combo).
_ENTRY_KNOBS = {"min_day_gain_pct", "rsi_max", "rsi_min"}


def _active_param_space(base_strategy: dict) -> dict[str, list[float]]:
    """The knobs to search for THIS strategy: always the core four, plus an RSI
    knob for each RSI rule it uses and the MACD speed knob if it uses MACD. Keeps
    a plain (no RSI/MACD) strategy's search — and its tests — byte-identical."""
    space = dict(PARAM_SPACE)
    params = base_strategy.get("params") or {}
    entry = params.get("entry") or {}
    exit_rules = params.get("exit") or {}
    if float(entry.get("rsi_max", 0) or 0) > 0:
        space["rsi_max"] = RSI_PARAM_SPACE["rsi_max"]
    if float(entry.get("rsi_min", 0) or 0) > 0:
        space["rsi_min"] = RSI_PARAM_SPACE["rsi_min"]
    if float(exit_rules.get("exit_rsi_above", 0) or 0) > 0:
        space["exit_rsi_above"] = RSI_PARAM_SPACE["exit_rsi_above"]
    if entry.get("require_macd_bullish") or exit_rules.get("exit_on_macd_bearish"):
        space["macd_slow"] = MACD_PARAM_SPACE["macd_slow"]
    return space

ProgressFn = Callable[[int, int], None]
BacktestFn = Callable[..., dict]


def _parse_ts(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def _window(start: datetime, end: datetime) -> dict:
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "days": round((end - start).total_seconds() / 86400, 1),
    }


def split_in_out_of_sample(
    bars_by_symbol: dict[str, list[dict]],
    in_sample_frac: float = 0.7,
    window_start: datetime | None = None,
) -> tuple[dict[str, list[dict]], dict[str, list[dict]], datetime, datetime, datetime]:
    """Split every symbol's bars at a single global time boundary so all symbols
    share the SAME in/out split. In-sample = bars at/ before the boundary (the
    first `in_sample_frac` of the calendar window); out-of-sample = bars strictly
    after it. Splitting by time (not by count) keeps the two slices genuinely
    chronological and non-overlapping — the out-of-sample slice is always the
    later, unseen market.

    `window_start` enables WARM-UP mode (for daily MACD/RSI/ATR strategies): the
    leading bars before `window_start` are warm-up history, not part of the
    optimization window, so (a) the in/out boundary is measured over the window
    ONLY (warm-up doesn't shift it), and (b) BOTH slices carry a warm-up prefix so
    their indicators are live from the first traded bar — the in-sample slice keeps
    the bars up to the boundary (warm-up included), and the out-of-sample slice
    gets the FULL series (everything before the boundary is its warm-up). The
    caller gates trading with each slice's sim_start. Without `window_start` the
    split is unchanged: the two slices are disjoint and neither has warm-up."""
    all_ts = sorted(
        _parse_ts(b["t"]) for series in bars_by_symbol.values() for b in series
    )
    if not all_ts:
        raise ValueError("No historical bars to optimize over.")
    t0, t1 = all_ts[0], all_ts[-1]
    origin = window_start if window_start is not None else t0
    boundary = origin + (t1 - origin) * in_sample_frac
    in_sample: dict[str, list[dict]] = {}
    out_sample: dict[str, list[dict]] = {}
    for symbol, series in bars_by_symbol.items():
        in_sample[symbol] = [b for b in series if _parse_ts(b["t"]) <= boundary]
        if window_start is None:
            out_sample[symbol] = [b for b in series if _parse_ts(b["t"]) > boundary]
        else:
            out_sample[symbol] = list(series)  # full series; sim_start=boundary gates trading
    return in_sample, out_sample, origin, boundary, t1


def _apply_combo(base_strategy: dict, combo: dict) -> dict:
    """A copy of the base strategy dict with the searched knobs overwritten, each
    routed to its params section (entry vs exit). Everything else (asset class,
    sizing, sleeve, entry window, VWAP rule, MACD toggles…) is left exactly as the
    user configured it — the search tunes momentum/exit aggressiveness (and, when
    the strategy uses RSI, its thresholds), not the whole strategy."""
    params = copy.deepcopy(base_strategy["params"])
    entry = params.setdefault("entry", {})
    exit_rules = params.setdefault("exit", {})
    for key, value in combo.items():
        if key == "macd_slow":
            # Scale MACD along the strategy's own fast/slow ratio: a smaller slow
            # period = a faster, less-laggy MACD. Keeps fast < slow by construction.
            # NOTE: a strategy that uses MACD but never customized its periods
            # serializes "macd": null, so setdefault would hand back None — take
            # `get(...) or {}` and reassign to cover both null and absent.
            m = params.get("macd") or {}
            params["macd"] = m
            base_slow = float(m.get("slow", 26) or 26)
            base_fast = float(m.get("fast", 12) or 12)
            ratio = (base_fast / base_slow) if base_slow else (12 / 26)
            slow = int(value)
            fast = max(2, min(slow - 1, round(slow * ratio)))
            m["fast"], m["slow"], m["signal"] = fast, slow, int(m.get("signal", 9) or 9)
        elif key in _ENTRY_KNOBS:
            entry[key] = value
        else:
            exit_rules[key] = value
    return {**base_strategy, "params": params}


def _entries(res: dict | None) -> int:
    """Entries made = closed round-trips + positions still open at the end. The
    backtest no longer force-sells held-to-end positions into fake 'trades', so
    THIS is the honest sample size — a config that entered five times and held
    its winners is five data points, not zero."""
    if not res:
        return 0
    return (res.get("trades") or 0) + len(res.get("open_positions") or [])


def _metrics(res: dict | None) -> dict | None:
    """The handful of numbers the UI shows per config — pulled from a full
    run_backtest result (or None if the config produced no runnable result)."""
    if not res or "error" in res:
        return None
    return {
        "net_pnl_pct": res.get("net_pnl_pct"),
        "trades": res.get("trades"),
        "entries": _entries(res),
        "win_rate": res.get("win_rate"),
        "return_on_deployed_pct": res.get("return_on_deployed_pct"),
        "max_drawdown_pct": res.get("max_drawdown_pct"),
    }


def _score(res: dict | None, min_trades: int) -> float | None:
    """A config's in-sample score: its account return %, but only if it made at
    least `min_trades` ENTRIES (closed trades + positions held to the end — a
    held winner is a data point, not a non-event). Rewarding a config that fired
    once and got lucky is exactly the overfitting trap this search exists to
    avoid, so thin configs score None (ranked below every real one)."""
    if not res or "error" in res:
        return None
    if _entries(res) < min_trades:
        return None
    return res.get("net_pnl_pct")


def _sort_key(entry: dict):
    score = entry["score"]
    # None (too-thin) configs sink to the bottom; ties break on trade count so a
    # config that proved itself over more trades wins.
    return (score is not None, score if score is not None else float("-inf"), entry["in_sample"]["trades"] if entry["in_sample"] else 0)


def optimize(
    base_strategy: dict,
    bars_by_symbol: dict[str, list[dict]],
    risk: dict,
    *,
    iterations: int = 40,
    starting_cash: float = 5000.0,
    spread_pct: float = 0.1,
    market: str = "stock",
    in_sample_frac: float = 0.7,
    min_trades: int = 3,
    top_n_validate: int = 6,
    seed: int | None = None,
    eligible_by_day: dict[str, set[str]] | None = None,
    backtest_fn: BacktestFn = run_backtest,
    progress: ProgressFn | None = None,
    sim_start: datetime | None = None,
    daily_bars_by_symbol: dict[str, list[dict]] | None = None,
) -> dict:
    """Search the momentum param space and return the out-of-sample-validated
    findings.

    Flow (coarse-to-fine):
      1. Split the bars into in-sample (first ~70%) and out-of-sample (last ~30%).
      2. Random-sample `iterations` distinct combos; run each on the IN-SAMPLE
         slice only. Score = in-sample account return (needs >= min_trades).
      3. Refine locally around the leader: sweep each knob one grid step either
         way (one knob at a time), running those on the in-sample slice too. This
         both finds a better local optimum AND produces the plateau/neighbourhood
         picture.
      4. Validate the top configs on the OUT-OF-SAMPLE slice the search never saw.
         Only these numbers are treated as real.

    `backtest_fn` defaults to the real backtester but can be injected (tests pass
    a fake — no network). Returns the dict documented at the bottom.

    `daily_bars_by_symbol` turns on MIXED-RESOLUTION search: `bars_by_symbol` is
    then the INTRADAY replay timeline (so the searched stop-loss / trailing stop /
    take-profit are checked against real intraday prices instead of once a day at
    the close) while the indicators come from the daily series. Three of the four
    core knobs are price-triggered exits, so without this a daily replay makes a
    tight stop look almost free — it only ever triggers on a close — and the
    search drifts toward stops that would whipsaw in real trading."""
    rng = random.Random(seed)

    in_sample, out_sample, t0, boundary, t1 = split_in_out_of_sample(
        bars_by_symbol, in_sample_frac, window_start=sim_start
    )

    # Warm-up mode (daily MACD/RSI/ATR): each slice trades only its own window but
    # keeps the earlier bars as indicator history, so neither slice has a dead zone
    # where the signal isn't defined yet. The in-sample slice trades from the window
    # start; the out-of-sample slice trades from the boundary (everything before it,
    # including the whole in-sample window, is warm-up). Passed only when warm-up is
    # active so injected test fakes with the plain signature stay callable.
    in_kw = {} if sim_start is None else {"sim_start": sim_start}
    out_kw = {} if sim_start is None else {"sim_start": boundary}

    # MIXED RESOLUTION — the daily series is NEVER SPLIT. split_in_out_of_sample
    # divides the INTRADAY replay timeline by time; the daily bars are not a replay
    # timeline at all, they are the indicator SOURCE, so both slices get the WHOLE
    # series. That stays look-ahead-safe because _daily_frontier derives a per-day
    # cutoff from whichever intraday series it is handed and only ever uses daily
    # bars strictly BEFORE each bar's own day — later daily bars in the series are
    # unreachable. Split them and the out-of-sample slice would lose its MACD/RSI
    # history, every indicator would come back None, and the honest verdict number
    # would silently collapse to "no trades". Passed only when given, so injected
    # test fakes with the plain signature stay callable (same as in_kw/out_kw).
    daily_kw = (
        {} if daily_bars_by_symbol is None else {"daily_bars_by_symbol": daily_bars_by_symbol}
    )

    # In scanner-replay mode a symbol may only ENTER on the days it was a top-N
    # riser. The same eligible-by-day map is passed to both slices: run_backtest
    # only ever looks up the days present in the bars it's given, so the early
    # (in-sample) and late (out-of-sample) slices each see just their own days —
    # no need to split the map. None = the normal fixed-universe search.
    def run_in_sample(combo: dict) -> dict:
        strat = _apply_combo(base_strategy, combo)
        return backtest_fn(
            strat, in_sample, risk,
            starting_cash=starting_cash, spread_pct=spread_pct, market=market,
            eligible_by_day=eligible_by_day, **in_kw, **daily_kw,
        )

    def run_out_of_sample(combo: dict) -> dict:
        strat = _apply_combo(base_strategy, combo)
        return backtest_fn(
            strat, out_sample, risk,
            starting_cash=starting_cash, spread_pct=spread_pct, market=market,
            # NOT split: the same WHOLE daily series the in-sample slice got.
            eligible_by_day=eligible_by_day, **out_kw, **daily_kw,
        )

    # ---- 1 & 2: random search over the coarse grid (in-sample only) ----
    # The active space is the core four knobs plus an RSI knob per RSI rule the
    # strategy already uses (non-RSI strategies search exactly as before).
    space = _active_param_space(base_strategy)
    keys = list(space)
    evaluated: dict[tuple, dict] = {}  # combo-tuple -> {combo, score, in_sample metrics}

    # De-dupe: with a small grid, random draws collide; keep sampling (bounded)
    # until we have `iterations` DISTINCT combos or exhaust the space.
    space_size = 1
    for values in space.values():
        space_size *= len(values)
    target = min(iterations, space_size)

    def evaluate(combo: dict) -> dict:
        key = tuple(combo[k] for k in keys)
        if key in evaluated:
            return evaluated[key]
        res = run_in_sample(combo)
        entry = {
            "combo": combo,
            "score": _score(res, min_trades),
            "in_sample": _metrics(res),
            "out_of_sample": None,
            # Why this combo made no trades — the backtest's plain-English reason
            # (VWAP rejected / outside entry window / symbols too calm / rails).
            "no_trade_reason": (
                (res.get("diagnosis") or {}).get("summary")
                if not res.get("error") and (res.get("trades") or 0) == 0
                else None
            ),
        }
        evaluated[key] = entry
        if progress:
            progress(len(evaluated), max(target, len(evaluated)))
        return entry

    attempts = 0
    while len([e for e in evaluated]) < target and attempts < target * 20:
        attempts += 1
        combo = {k: rng.choice(space[k]) for k in keys}
        evaluate(combo)

    if not evaluated:  # pathological (empty grid) — cannot happen with the constants
        raise ValueError("Parameter space is empty.")

    # ---- 3: local refinement / plateau sweep around the current leader ----
    # Bounded hill-climb: sweep each knob one grid step either way around the
    # leader; if a neighbour wins, adopt it and sweep again. This is the "fine" of
    # coarse-to-fine, AND it guarantees the FINAL leader's immediate neighbours
    # were all evaluated — which is exactly the plateau picture reported below.
    def current_best() -> dict:
        return sorted(evaluated.values(), key=_sort_key, reverse=True)[0]

    leader = current_best()
    for _ in range(len(keys) * 2):  # bound: can't climb forever on a finite grid
        pivot = leader
        for key in keys:
            values = space[key]
            idx = values.index(pivot["combo"][key])
            for j in (idx - 1, idx + 1):
                if 0 <= j < len(values):
                    neighbour = dict(pivot["combo"])
                    neighbour[key] = values[j]
                    evaluate(neighbour)
        leader = current_best()
        if leader is pivot:  # no neighbour improved on the pivot — settled
            break

    ranked = sorted(evaluated.values(), key=_sort_key, reverse=True)

    # ---- 4: out-of-sample validation of the top configs ----
    for entry in ranked[:top_n_validate]:
        res = run_out_of_sample(entry["combo"])
        entry["out_of_sample"] = _metrics(res)
        entry["_oos_full"] = res  # kept transiently for the hold-benchmark read

    results = []
    for rank, entry in enumerate(ranked[:top_n_validate], start=1):
        results.append(
            {
                "rank": rank,
                "params": dict(entry["combo"]),
                "in_sample_score": entry["score"],
                "in_sample": entry["in_sample"],
                "out_of_sample": entry["out_of_sample"],
                "is_best": entry is leader,
            }
        )

    # Neighbourhood: per knob, the in-sample scores of the leader plus its one-step
    # neighbours (same value on the other three knobs). All-similar = a plateau
    # (trustworthy); leader spiking alone = probably noise.
    neighbourhood: dict[str, list[dict]] = {}
    other_keys = {k: {o: leader["combo"][o] for o in keys if o != k} for k in keys}
    for key in keys:
        pts = []
        for entry in evaluated.values():
            combo = entry["combo"]
            if all(combo[o] == other_keys[key][o] for o in other_keys[key]):
                pts.append(
                    {
                        "value": combo[key],
                        "score": entry["score"],
                        "is_best": entry is leader,
                    }
                )
        pts.sort(key=lambda p: p["value"])
        neighbourhood[key] = pts

    # Buy-and-hold comparison, on the OUT-OF-SAMPLE slice, for the winner.
    oos_full = leader.get("_oos_full") or {}
    hold_series = oos_full.get("hold_benchmark") or []
    hold_last = next((v for v in reversed(hold_series) if v is not None), None)
    strat_oos = (leader["out_of_sample"] or {}).get("net_pnl_pct")
    beat_hold = None
    if strat_oos is not None and hold_last is not None:
        beat_hold = strat_oos > hold_last

    warnings: list[str] = []
    if len(bars_by_symbol) < 2:
        warnings.append(
            "Only one symbol was tested — a config that fits one ticker's history "
            "rarely generalizes. Validate across several symbols or the basket."
        )
    if ((leader["out_of_sample"] or {}).get("entries") or 0) == 0:
        warnings.append(
            "The winning config never entered a position in the out-of-sample "
            "period — its in-sample result is unconfirmed. Treat it as untested."
        )

    best_draft = _apply_combo(base_strategy, leader["combo"])["params"]

    # If NOTHING traded across the whole search, the result is meaningless — say
    # WHY, using the diagnosis of the most permissive combo (lowest min-gain), so
    # a 0-trade run isn't mistaken for "the strategy is unworkable". Usually it's a
    # config mismatch (e.g. the VWAP rule or an entry-time window on daily bars).
    total_in_sample_trades = sum(
        (e["in_sample"] or {}).get("trades", 0) or 0 for e in evaluated.values()
    )
    no_trade_reason = None
    if total_in_sample_trades == 0 and evaluated:
        most_permissive = min(evaluated.values(), key=lambda e: e["combo"]["min_day_gain_pct"])
        no_trade_reason = most_permissive.get("no_trade_reason")

    return {
        "tested_combinations": len(evaluated),
        # Total size of the grid the random search samples from — so "tested N"
        # reads as a deliberate coarse-to-fine SAMPLE, not the whole space. It's
        # the product of every active knob's value count (grows fast with knobs).
        "search_space_size": space_size,
        "no_trade_reason": no_trade_reason,
        "iterations": iterations,
        "in_sample_window": _window(t0, boundary),
        "out_of_sample_window": _window(boundary, t1),
        "results": results,
        "best": results[0] if results else None,
        "best_draft_params": best_draft,
        "neighbourhood": neighbourhood,
        "hold_benchmark_comparison": {
            "strategy_out_of_sample_pct": strat_oos,
            "hold_out_of_sample_pct": hold_last,
            "hold_label": oos_full.get("hold_benchmark_label"),
            "beat_hold": beat_hold,
        },
        "symbols": sorted(bars_by_symbol),
        "warnings": warnings,
    }
