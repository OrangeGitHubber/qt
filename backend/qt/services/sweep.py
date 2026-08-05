"""Basket sweep: run the SAME parameter search across every basket and rank the
results by their OUT-OF-SAMPLE margin over SPY.

This exists to answer "which theme, with which settings, would actually have
beaten the market?" honestly — every number comes from the backtester through
qt.services.optimizer.optimize (in-sample search, out-of-sample verdict), never
from a human or an LLM picking winners. The leaderboard is ranked ONLY by the
out-of-sample margin; in-sample numbers are shown as context, not proof.

Anti-overfitting posture is inherited wholesale from the optimizer: the split,
the plateau sweep, the combination count, and its warnings all ride along on
each row. A row whose winner made no out-of-sample trades is ranked LAST no
matter its numbers — an untested hypothesis can't sit above tested ones.

KNOWN LIMITATION — the sweep ranks on DAILY bars. Two of the four searched knobs
(stop_loss_pct, trailing_stop_pct) are price-triggered exits, and a daily replay
checks them once a day at the close: a position that dipped through its stop and
recovered is scored a winner, so a tight stop looks nearly free here. The RANKING
(which theme beat SPY) survives that — every basket is scored the same way — but
the per-row STOP VALUES are indicative only. Re-optimize the basket you like on
the single-strategy optimizer, which replays intraday when the strategy has a
price-triggered exit, before trusting its stops. This is deliberate, not an
oversight: 12 baskets x 25 symbols of 15-minute bars is an enormous download.
"""

from collections.abc import Callable
from datetime import datetime

from qt.services import optimizer

# One template momentum strategy, identical for every basket, so the ONLY
# variable across rows is the basket itself (plus the searched knobs). Plain
# momentum (no MACD/RSI/VWAP): the search space stays the four core knobs and
# daily bars need no indicator warm-up. $1k/trade on a $5k sleeve with 5 slots
# means full deployment is possible — so idle-cash dilution can't make one
# basket's return look artificially small.
TEMPLATE_SIZING = {"sizing_usd": 1000.0, "sleeve_usd": 5000.0, "max_positions": 5}


def _template_strategy() -> dict:
    """The plain-momentum fallback, used only when no strategy is supplied.

    Kept because it is a defensible default for "which themes have momentum at
    all", but it is no longer what the sweep does by default: sweeping a fixed
    template answered a question nobody asked. The point of the feature is
    testing YOUR strategy against every basket without editing its universe
    fifteen times, so `sweep_baskets` now takes the strategy to sweep."""
    return {
        "asset_class": "stock",
        "swing_mode": True,  # daily bars → hold overnight; stops still always apply
        **TEMPLATE_SIZING,
        "params": {
            "entry": {
                # Baselines only — the search overwrites all four knobs per combo.
                "min_day_gain_pct": 2.0,
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


def _parse_ts(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def pct_over_window(bars: list[dict], start_iso: str, end_iso: str) -> float | None:
    """Buy-and-hold % change across (start, end]: baseline = the last close AT or
    BEFORE `start` (the boundary bar the out-of-sample slice begins after), final
    = the last close at or before `end`. None when the bars can't cover it."""
    if not bars:
        return None
    start = _parse_ts(start_iso)
    end = _parse_ts(end_iso)
    base: float | None = None
    final: float | None = None
    for b in bars:  # bars are oldest-first
        ts = _parse_ts(b["t"])
        if ts <= start:
            base = float(b["c"])
        if ts <= end:
            final = float(b["c"])
        else:
            break
    if base is None:  # window starts before the data — no honest baseline
        return None
    if final is None:
        return None
    return round((final / base - 1) * 100, 2)


def sweep_baskets(
    baskets: list[dict],
    bars_by_symbol: dict[str, list[dict]],
    risk: dict,
    *,
    iterations: int = 60,
    starting_cash: float = 5000.0,
    spread_pct: float = 0.1,
    min_symbols: int = 2,
    progress: Callable[[int, int, str], None] | None = None,
    optimize_fn: Callable[..., dict] = optimizer.optimize,
    strategy: dict | None = None,
) -> dict:
    """Run the parameter search over each basket and rank by out-of-sample margin
    over SPY.

    `baskets` is [{"id", "name", "symbols": [...]}] (already resolved to stock
    symbols); `bars_by_symbol` holds daily bars for every basket symbol PLUS SPY.
    Pure sync — the caller fetches bars and runs this in a worker thread.
    `optimize_fn` is injectable for tests (same pattern as optimizer.backtest_fn).

    `strategy` is the config to sweep — YOUR strategy, so the answer is "which
    basket suits what I actually trade" rather than "which basket suits a plain
    momentum template". Its universe/basket_id are irrelevant here (the caller
    supplies the bars for each basket in turn), but its ranking, sizing and every
    entry/exit rule are exactly what gets searched. None falls back to the plain
    template, which is the old behaviour and still a reasonable "do any of these
    themes have momentum at all" question.
    """
    base = strategy or _template_strategy()
    # Whatever the strategy's own universe was, each iteration is scored on ONE
    # basket's bars — so a basket universe pointing at some OTHER basket would be
    # a stale label on every row. Ranking config is untouched: it is a genuine
    # part of the strategy being tested.
    base = {**base, "universe": "custom"}
    sizing = {k: base.get(k) for k in ("sizing_usd", "sleeve_usd", "max_positions")
              if base.get(k) is not None}
    spy_bars = bars_by_symbol.get("SPY") or []
    rows: list[dict] = []
    skipped: list[dict] = []

    for i, basket in enumerate(baskets):
        if progress:
            progress(i, len(baskets), basket["name"])
        bars = {s: bars_by_symbol[s] for s in basket["symbols"] if bars_by_symbol.get(s)}
        if len(bars) < min_symbols:
            skipped.append({
                "basket_name": basket["name"],
                "reason": f"only {len(bars)} of {len(basket['symbols'])} symbols had history",
            })
            continue
        try:
            # seed = basket id → each basket samples its own (stable) corner of the
            # grid, and a re-run of the sweep reproduces the same leaderboard.
            res = optimize_fn(
                base, bars, risk,
                iterations=iterations, starting_cash=starting_cash,
                spread_pct=spread_pct, market="stock", seed=basket["id"],
            )
        except ValueError as exc:  # e.g. no usable bars at all
            skipped.append({"basket_name": basket["name"], "reason": str(exc)})
            continue

        best = res.get("best") or {}
        oos = best.get("out_of_sample") or {}
        oos_pct = oos.get("net_pnl_pct")
        window = res["out_of_sample_window"]
        spy_oos = pct_over_window(spy_bars, window["start"], window["end"])
        margin = (
            round(oos_pct - spy_oos, 2)
            if oos_pct is not None and spy_oos is not None
            else None
        )
        hb = res.get("hold_benchmark_comparison") or {}
        rows.append({
            "basket_id": basket["id"],
            "basket_name": basket["name"],
            "symbols": sorted(bars),
            "tested_combinations": res.get("tested_combinations"),
            "search_space_size": res.get("search_space_size"),
            "best_params": dict(best.get("params") or {}),
            "best_draft_params": res.get("best_draft_params"),
            "in_sample_pct": (best.get("in_sample") or {}).get("net_pnl_pct"),
            "oos_pct": oos_pct,
            "oos_trades": oos.get("trades"),
            # Entries = closed trades + positions held to the OOS end — the honest
            # sample size (a held winner is a data point, not a non-event).
            "oos_entries": oos.get("entries", oos.get("trades")),
            "oos_win_rate": oos.get("win_rate"),
            "oos_max_drawdown_pct": oos.get("max_drawdown_pct"),
            "oos_window": window,
            "hold_oos_pct": hb.get("hold_out_of_sample_pct"),
            "beat_hold": hb.get("beat_hold"),
            "spy_oos_pct": spy_oos,
            "margin_vs_spy": margin,
            "warnings": res.get("warnings") or [],
            "no_trade_reason": res.get("no_trade_reason"),
        })

    # Rank: tested rows (a margin AND at least one out-of-sample ENTRY) by margin,
    # best first; untested rows sink to the bottom regardless of their numbers.
    def untested(r: dict) -> bool:
        return r["margin_vs_spy"] is None or not (r["oos_entries"] or 0)

    rows.sort(key=lambda r: (1 if untested(r) else 0, -(r["margin_vs_spy"] or 0.0)))
    for rank, r in enumerate(rows, start=1):
        r["rank"] = rank
        r["untested"] = untested(r)

    if progress:
        progress(len(baskets), len(baskets), "")
    return {
        "rows": rows,
        "skipped": skipped,
        "iterations": iterations,
        "spy_available": bool(spy_bars),
        # The sizing every row was scored with — the swept strategy's own, or the
        # fallback template's. Named for the UI, which prints it as a caveat.
        "template_sizing": {**TEMPLATE_SIZING, **sizing},
        "swept_strategy": base.get("name"),
    }
