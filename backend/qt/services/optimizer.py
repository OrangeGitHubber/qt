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

# Every knob is searched on a GEOMETRIC grid anchored on the strategy's own
# current value: v, v*(1+p), v*(1+p)^2 … and v/(1+p), v/(1+p)^2 … out to
# STEPS_EACH_WAY in both directions.
#
# Fixed absolute grids (2.0, 3.0, 4.0 …) had three problems this fixes:
#   - They were scale-blind. One step meant +50% at the bottom of the grid and
#     +12% near the top, so resolution was coarsest exactly where a knob is most
#     sensitive. A relative step is the same size everywhere by construction.
#   - Your own setting was usually NOT on the grid, so the search never actually
#     evaluated the config you were running, and "the winner beat your setting"
#     was a comparison nobody had run. The anchor is always on the grid now.
#   - Every knob needed its own hand-chosen list, which had to be re-argued each
#     time one was added. One rule now covers all of them, including any added
#     later.
#
# The trade-off, stated plainly: this is a LOCAL search. It refines the strategy
# you have; it cannot discover that a wholly different setting is better. At the
# default 15% x 4 steps it reaches x0.57 to x1.75 of each value. To travel
# further, run the search again on the resulting draft — each run re-anchors on
# the new values, so repeated runs walk.
RELATIVE_STEP_DEFAULT = 0.15
STEPS_EACH_WAY = 4

# (floor, ceiling, decimals) per knob, so every value the search can propose is
# one the draft can actually be SAVED with. decimals=0 means the knob is an
# integer (MACD periods).
#
# "Saved with" means BOTH gates, not just the pydantic one. The strategy schema
# is the looser of the two — it allows any float in range — while the strategy
# editor's number inputs carry their own `min`, and a value under that min makes
# the whole edit form unsavable in the browser. The floors below are therefore
# the tighter of (schema, editor): trailing stop 0.5 and stop-loss 0.1 come from
# frontend/src/pages/Strategies.tsx, not from strategies.py, because a 0.06%
# trailing stop is both un-editable and nonsense to trade. Backed by
# test_optimizer_precision.py, which reads those mins out of the editor.
#
# decimals is the ONLY quantization applied, and it is deliberately finer than
# one decimal place: the grid is geometric, so on a small anchor (0.6 -> 0.69 ->
# 0.79) rounding to tenths would collapse neighbouring steps onto the same value
# and shrink the search. Hundredths are meaningful downstream — evaluate_exit
# compares entry_price * (1 - pct/100) against raw bar lows with no rounding, so
# 1.23% and 1.20% are genuinely different stops.
KNOB_BOUNDS: dict[str, tuple[float, float, int]] = {
    "min_day_gain_pct": (0.05, 100.0, 2),
    "trailing_stop_pct": (0.5, 50.0, 2),
    "stop_loss_pct": (0.1, 50.0, 2),
    "take_profit_pct": (0.05, 500.0, 2),
    "rsi_max": (1.0, 99.0, 1),
    "rsi_min": (1.0, 99.0, 1),
    "exit_rsi_above": (1.0, 99.0, 1),
    "atr_stop_mult": (0.1, 20.0, 2),
    "macd_slow": (3, 200, 0),
}


def _geometric_grid(anchor: float, step: float, bounds: tuple[float, float, int]) -> list[float]:
    """The values to try for one knob: the anchor, multiplied and divided by
    (1+step) repeatedly, clamped to the knob's legal range.

    The anchor is kept EXACT (never rounded) so the strategy's current setting is
    always genuinely among the values tested — that is what makes the before/after
    comparison real. Rounded neighbours are de-duplicated, which matters for small
    integers: 15% steps either side of a MACD slow period of 5 land on the same
    number more than once.

    The anchor also survives the CLAMP, which the neighbours don't. The floors
    here match the strategy editor's, and an old strategy imported through the API
    could sit below one (a 0.2% trailing stop, say) — dropping it would mean the
    search never evaluated the config the user is actually running and the whole
    before/after panel became fiction. The anchor is not a proposal, it is the
    status quo: a draft built from it is exactly as savable as the strategy it was
    read out of. Everything the search PROPOSES still respects the bounds."""
    lo, hi, decimals = bounds
    values: set[float] = set()
    for k in range(-STEPS_EACH_WAY, STEPS_EACH_WAY + 1):
        v = anchor * (1.0 + step) ** k
        v = float(round(v)) if decimals == 0 else round(v, decimals)
        if k == 0:
            v = float(anchor)  # exact, so the current config is always evaluated
            values.add(v)
        elif lo <= v <= hi:
            values.add(v)
    return sorted(values)

# Where each searchable knob lives in the strategy's params, so one loop can read
# every anchor. macd_slow is special-cased (it lives in the `macd` block and is
# only meaningful when a MACD toggle is on).
_ENTRY_KNOBS = {"min_day_gain_pct", "rsi_max", "rsi_min"}
_EXIT_KNOBS = {"trailing_stop_pct", "stop_loss_pct", "take_profit_pct", "exit_rsi_above"}
# Explicitly ORDERED, never a set union: the key order fixes the order random
# combos are drawn in, so a set's arbitrary iteration order would make a seeded
# run unreproducible across processes.
_CORE_KNOBS = (
    "min_day_gain_pct",
    "trailing_stop_pct",
    "stop_loss_pct",
    "take_profit_pct",
    "rsi_min",
    "rsi_max",
    "exit_rsi_above",
)


def _anchor(params: dict, key: str) -> float:
    entry = params.get("entry") or {}
    exit_rules = params.get("exit") or {}
    if key == "macd_slow":
        return float((params.get("macd") or {}).get("slow", 26) or 26)
    if key == "atr_stop_mult":
        return float((params.get("atr") or {}).get("stop_mult", 0) or 0)
    src = entry if key in _ENTRY_KNOBS else exit_rules
    return float(src.get(key, 0) or 0)


def _active_param_space(
    base_strategy: dict, relative_step: float = RELATIVE_STEP_DEFAULT
) -> dict[str, list[float]]:
    """The knobs to search for THIS strategy, each on a geometric grid around its
    current value.

    A knob is searched when it is SWITCHED ON — i.e. its value is above zero.
    That one rule replaces the per-knob conditions that used to be written out
    separately for RSI, MACD and ATR, and it follows the principle those already
    encoded: tune the factors you are actually using rather than inventing new
    ones. It does mean the search will not turn a rule ON for you — a take-profit
    of 0 stays 0, because zero has no meaningful percentage step and guessing an
    anchor would be inventing a rule you didn't ask for. Switch it on with any
    value and the next search will tune it.

    MACD is the exception to "> 0": its period is always non-zero, so it stays
    gated on the strategy actually using a MACD signal."""
    params = base_strategy.get("params") or {}
    entry = params.get("entry") or {}
    exit_rules = params.get("exit") or {}

    keys = list(_CORE_KNOBS)
    if entry.get("require_macd_bullish") or exit_rules.get("exit_on_macd_bearish"):
        keys.append("macd_slow")
    if _anchor(params, "atr_stop_mult") > 0:
        keys.append("atr_stop_mult")

    space: dict[str, list[float]] = {}
    for key in keys:
        anchor = _anchor(params, key)
        if anchor <= 0:
            continue
        grid = _geometric_grid(anchor, relative_step, KNOB_BOUNDS[key])
        # A grid of one value is not a search — it happens when the bounds clamp
        # everything to a single point. Listing it would put a knob in the report
        # that no iteration could ever vary.
        if len(grid) > 1:
            space[key] = grid

    # ATR stop on: the fixed stop is INERT (evaluate_exit replaces it with
    # stop_mult x ATR%), so searching it would spend iterations proving a knob
    # does nothing and print a meaningless "best" beside the values that count.
    if "atr_stop_mult" in space:
        space.pop("stop_loss_pct", None)
    return space


def _combo_is_valid(combo: dict) -> bool:
    """Whether a combination is one the strategy schema would actually accept.

    The RSI band is the case that bites: rsi_min and rsi_max are searched
    independently, so with a wide step rsi_min can be stepped UP past a rsi_max
    that stepped DOWN — an entry band with no width, which the strategy model
    rejects ("RSI min must be below RSI max"). A search that evaluated those
    would eventually report a winner whose draft cannot be saved, which is a
    dead end presented as a recommendation. Skipped before evaluation rather
    than clamped, so the reported combo is always exactly what was run.

    The anchor combination is always valid — the base strategy passed the same
    validation — so the search can never be left with nothing to try."""
    lo, hi = combo.get("rsi_min"), combo.get("rsi_max")
    return not (lo is not None and hi is not None and lo >= hi)


def _baseline_values(base_strategy: dict, space: dict[str, list[float]]) -> dict[str, dict]:
    """What the strategy is set to TODAY for each searched knob, so the result can
    show before -> after.

    Read from the base strategy the search actually ran against — NOT from
    whatever the UI has loaded — so editing the strategy after a run can't
    silently rewrite the "before" and make the search look like it proposed
    something it didn't.

    `in_grid` says whether that value was itself among the values tried. With
    grids anchored on the strategy it is always true, and the field is kept
    deliberately: it is the UI's guarantee that "your 3.5 vs the winner's 4.0" is
    a comparison that was actually run, and it should keep telling the truth if
    the grids ever change again."""
    params = base_strategy.get("params") or {}
    out: dict[str, dict] = {}
    for key, values in space.items():
        current = _anchor(params, key)
        out[key] = {
            "value": current,
            "in_grid": any(abs(current - v) < 1e-9 for v in values),
        }
    return out


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
        elif key == "atr_stop_mult":
            # Lives in its own params block, alongside the period and risk sizing
            # the user set — those are left exactly as configured.
            a = params.get("atr") or {}
            params["atr"] = a
            a["stop_mult"] = value
            a.setdefault("period", 14)
            a.setdefault("risk_usd", 0)
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
    fee_pct: float | None = None,
    market: str = "stock",
    in_sample_frac: float = 0.7,
    min_trades: int = 3,
    relative_step: float = RELATIVE_STEP_DEFAULT,
    top_n_validate: int = 6,
    seed: int | None = None,
    eligible_by_day: dict[str, set[str]] | None = None,
    backtest_fn: BacktestFn = run_backtest,
    progress: ProgressFn | None = None,
    sim_start: datetime | None = None,
    daily_bars_by_symbol: dict[str, list[dict]] | None = None,
    rank_daily_bars_by_symbol: dict[str, list[dict]] | None = None,
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
    search drifts toward stops that would whipsaw in real trading.

    `fee_pct` is the commission per side, as a % of notional — the SAME number the
    Backtest page charges (qt.api.backtest.DEFAULT_FEE_PCT: 0 for stocks, 0.25 for
    crypto). It was missing entirely, and on crypto that is not a rounding matter:
    a round trip costs ~0.5%, so a fee-free search systematically preferred
    higher-frequency settings — more entries look strictly better when the entries
    are free — and the Backtest page then scored the very same window worse. The
    two tools disagreed by construction, and the optimizer was the flattering one.
    None (the default) keeps a caller that says nothing about fees exactly as it
    was, and keeps the kwarg off injected test fakes with the plain signature."""
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
    # The RANKING's daily source, and NEVER SPLIT either, for the same reason: it
    # is a metric source, not a replay timeline, and run_backtest only ever reads
    # the daily bars completed before each replayed bar's own day. Splitting it
    # would leave the out-of-sample slice with no 200-day average, every member
    # unrankable, and a verdict of "no trades" that says nothing about the config.
    if rank_daily_bars_by_symbol is not None:
        daily_kw = {**daily_kw, "rank_daily_bars_by_symbol": rank_daily_bars_by_symbol}
    # BOTH SLICES, one dict — an in-sample slice searched fee-free and an
    # out-of-sample slice validated with fees would make the verdict number
    # incomparable with the score that chose the winner. Passed only when the
    # caller named a rate, so a fake with the plain signature stays callable
    # (same reason as in_kw/out_kw/daily_kw).
    fee_kw = {} if fee_pct is None else {"fee_pct": fee_pct}

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
            eligible_by_day=eligible_by_day, **in_kw, **daily_kw, **fee_kw,
        )

    def run_out_of_sample(combo: dict) -> dict:
        strat = _apply_combo(base_strategy, combo)
        return backtest_fn(
            strat, out_sample, risk,
            starting_cash=starting_cash, spread_pct=spread_pct, market=market,
            # NOT split: the same WHOLE daily series the in-sample slice got.
            eligible_by_day=eligible_by_day, **out_kw, **daily_kw, **fee_kw,
        )

    # ---- 1 & 2: random search over the coarse grid (in-sample only) ----
    # The active space is the core four knobs plus an RSI knob per RSI rule the
    # strategy already uses (non-RSI strategies search exactly as before).
    space = _active_param_space(base_strategy, relative_step)
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
        if not _combo_is_valid(combo):
            continue  # counted as an attempt, so the loop always terminates
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
                    if _combo_is_valid(neighbour):
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
    # THE TOP-N CUT the whole search ran under, read back off the winner's own
    # out-of-sample run rather than re-derived from the config — unwire the
    # ranking and this empties instead of going on making a claim. A search that
    # could NOT reproduce live's ordering scored every config it tried against a
    # wider pool than live would have offered it, which is a reason to distrust
    # the ranking of the results and not just their absolute numbers.
    ranking = oos_full.get("ranking")
    if ranking and not ranking.get("applied") and ranking.get("warning"):
        warnings.append(ranking["warning"])

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
        # .get, not [...]: min_day_gain_pct is only in the combo when it was
        # searched, and with anchored grids a strategy whose min-gain is 0 (no
        # minimum) doesn't have that knob at all.
        most_permissive = min(
            evaluated.values(), key=lambda e: e["combo"].get("min_day_gain_pct", 0)
        )
        no_trade_reason = most_permissive.get("no_trade_reason")

    return {
        "tested_combinations": len(evaluated),
        # Total size of the grid the random search samples from — so "tested N"
        # reads as a deliberate coarse-to-fine SAMPLE, not the whole space. It's
        # the product of every active knob's value count (grows fast with knobs).
        "search_space_size": space_size,
        # The step size every grid was built from, so the UI can say what "one
        # step either way" on the plateau chart actually means.
        "relative_step": relative_step,
        # The commission every config in this search paid, per side. Reported so
        # a search and a backtest of the same window can be shown to have been
        # scored on the same terms — and so "the optimizer models no fees" can
        # never again be true without the result saying so. None = the caller
        # named no rate, i.e. this search was fee-free.
        "fee_pct_per_side": fee_pct,
        "no_trade_reason": no_trade_reason,
        "iterations": iterations,
        "in_sample_window": _window(t0, boundary),
        "out_of_sample_window": _window(boundary, t1),
        "results": results,
        "best": results[0] if results else None,
        # The strategy's CURRENT value for each searched knob — the "before" of
        # the before/after, captured from the strategy this search ran against.
        "baseline_params": _baseline_values(base_strategy, space),
        "best_draft_params": best_draft,
        "neighbourhood": neighbourhood,
        "hold_benchmark_comparison": {
            "strategy_out_of_sample_pct": strat_oos,
            "hold_out_of_sample_pct": hold_last,
            "hold_label": oos_full.get("hold_benchmark_label"),
            "beat_hold": beat_hold,
        },
        "symbols": sorted(bars_by_symbol),
        # None on an unranked universe; see backtest.run_backtest's `ranking`.
        "ranking": ranking,
        "warnings": warnings,
    }
