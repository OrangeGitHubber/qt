"""Minimal backtester (Phase 2.5): replay a strategy config over historical
bars using the SAME pure decision functions the live engine runs
(evaluate_entry / evaluate_exit / check_rails) — the backtest can't drift
from reality because there is only one implementation of the rules.

Honest limitations, surfaced in the UI:
- It replays a FIXED symbol list, not the scanner's historical daily picks
  (Alpaca has no historical movers endpoint). It validates your entry/exit
  rules and risk rails, not the scanner.
- Fills are modeled as bar close ± a configurable spread cost per side.
- Free IEX data; past performance predicts nothing.
"""

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from qt.broker.alpaca import AlpacaClient
from qt.services import stats
from qt.services.engine import (
    Candidate,
    RailContext,
    atr_position_size,
    check_rails,
    evaluate_entry,
    evaluate_exit,
)

ET = ZoneInfo("America/New_York")


def _macd_on(params: dict) -> bool:
    """Whether either MACD toggle (entry filter or exit signal) is set."""
    return bool(
        params.get("entry", {}).get("require_macd_bullish")
        or params.get("exit", {}).get("exit_on_macd_bearish")
    )


def _atr_on(params: dict) -> bool:
    """Whether either ATR feature is on — the ATR stop (stop_mult > 0) or ATR
    sizing (risk_usd > 0). Absent/zero on both = off."""
    a = params.get("atr") or {}
    return float(a.get("stop_mult", 0) or 0) > 0 or float(a.get("risk_usd", 0) or 0) > 0


def _annotate_atr(prepared: dict[str, list[dict]], bars_by_symbol: dict[str, list[dict]], params: dict) -> None:
    """Attach `atr_pct` to each prepared bar, in place, when the strategy opts
    into ATR. The value at bar i is computed from the raw OHLC bars up to and
    INCLUDING the prior completed bar (raw[:i]) — never the current bar, so there
    is NO look-ahead — mirroring _annotate_macd. The prepared series and the raw
    bars are index-aligned (1:1, same order), so raw[:i] is exactly the completed
    history at prepared bar i. No-op when ATR is off, keeping non-ATR backtests
    byte-identical."""
    if not _atr_on(params):
        return
    period = int((params.get("atr") or {}).get("period", 14) or 14)
    for symbol, series in prepared.items():
        raw = bars_by_symbol.get(symbol) or []
        for i, bar in enumerate(series):
            bar["atr_pct"] = stats.atr_pct(raw[:i], period, current_price=bar["close"])


def _annotate_macd(prepared: dict[str, list[dict]], params: dict) -> None:
    """Attach `macd_bullish` to each prepared bar, in place, when the strategy
    opts into MACD. The value at bar i is computed from the replayed closes up to
    and INCLUDING the prior completed bar (closes[:i]) — never the current bar,
    so there is NO look-ahead. No-op when MACD is off, keeping non-MACD backtests
    byte-identical.

    NUANCE: the LIVE engine always computes MACD from DAILY bars, whereas here we
    use the backtest's OWN timeframe bars. For the intended daily/swing use
    (1Day, or 1Hour where a daily MACD and an hourly replay track closely enough)
    this matches the live behaviour; on much finer timeframes the two would
    diverge, which is why MACD is documented as a daily/swing signal."""
    if not _macd_on(params):
        return
    m = params.get("macd") or {}
    fast, slow, signal = int(m.get("fast", 12)), int(m.get("slow", 26)), int(m.get("signal", 9))
    for series in prepared.values():
        closes = [b["close"] for b in series]
        for i, bar in enumerate(series):
            bar["macd_bullish"] = stats.macd_bullish(closes[:i], fast, slow, signal)


def _rsi_on(params: dict) -> bool:
    """Whether any RSI rule is set — the entry band (rsi_min/rsi_max) or the
    overbought exit (exit_rsi_above)."""
    e = params.get("entry", {})
    x = params.get("exit", {})
    return (
        float(e.get("rsi_min", 0) or 0) > 0
        or float(e.get("rsi_max", 0) or 0) > 0
        or float(x.get("exit_rsi_above", 0) or 0) > 0
    )


def _annotate_rsi(prepared: dict[str, list[dict]], params: dict) -> None:
    """Attach `rsi` (Wilder 14) to each prepared bar, in place, when the strategy
    opts into an RSI rule. The value at bar i uses the replayed closes up to and
    INCLUDING the prior completed bar (closes[:i]) — no look-ahead — mirroring
    _annotate_macd. No-op when RSI is off, keeping non-RSI backtests byte-
    identical. Same daily/swing timeframe caveat as MACD."""
    if not _rsi_on(params):
        return
    for series in prepared.values():
        closes = [b["close"] for b in series]
        for i, bar in enumerate(series):
            bar["rsi"] = stats.rsi_from_closes(closes[:i])


@dataclass
class SimTrade:
    symbol: str
    qty: float
    entry_price: float
    entry_at: datetime
    entry_reason: str
    high_water: float
    exit_price: float | None = None
    exit_at: datetime | None = None
    exit_reason: str = ""
    pnl: float | None = None


def _fractional(params: dict) -> bool:
    """Mirror the live engine: a market+fractional strategy sizes stocks by
    dollar slice, not whole shares — so backtesting an expensive-name strategy
    doesn't come back as 0 trades (int(sizing // price) == 0)."""
    return bool(params.get("execution", {}).get("market_orders", False))


def _entry_qty(asset_class: str, sizing: float, fill: float, fractional: bool) -> float:
    if asset_class == "stock" and not fractional:
        return float(int(sizing // fill))  # whole shares
    return round(sizing / fill, 6)


# Human-readable label for each per-day "why no entry" category.
_REJECT_LABELS = {
    "gain": "day gain below the minimum",
    "extended": "too extended (over the max-gain cap)",
    "price": "outside the share-price band",
    "vwap": "not above VWAP",
    "window": "outside the entry-time window",
    "macd": "MACD not bullish",
    "rsi": "RSI outside the entry band",
    "rail": "blocked by a risk rail (max positions / sleeve / cooldown …)",
    "sizing": "position too small / not enough cash",
    "not_eligible": "not a top-N riser that day (scanner replay)",
}


def _reject_category(reason: str) -> str:
    """Bucket an evaluate_entry rejection reason into a short category for the
    per-day 'why no entry' summary shown on the backtest chart."""
    if "< required" in reason:
        return "gain"
    if "too extended" in reason:
        return "extended"
    if "min $" in reason or "max $" in reason:
        return "price"
    if "VWAP" in reason:
        return "vwap"
    if "entry window" in reason:
        return "window"
    if "MACD" in reason:
        return "macd"
    if "RSI" in reason:
        return "rsi"
    return "other"


def _summarize_no_trade_days(
    day_reject: dict[str, "Counter[str]"], entries_by_day: dict[str, int]
) -> dict[str, str]:
    """For each day that evaluated candidates but opened NO position, a plain-
    English reason (the one or two dominant blockers). Days that traded are
    omitted. This is what turns a flat stretch on the equity curve into 'here's
    why it sat out'."""
    out: dict[str, str] = {}
    for day, counter in day_reject.items():
        if entries_by_day.get(day, 0) > 0 or not counter:
            continue
        total = sum(counter.values())
        parts = [
            f"{n}× {_REJECT_LABELS.get(cat, cat)}" for cat, n in counter.most_common(2)
        ]
        out[day] = f"No entry — {total} candidate{'s' if total != 1 else ''} checked, all blocked: " + "; ".join(parts) + "."
    return out


@dataclass
class SimState:
    cash: float
    open_trades: dict[str, SimTrade] = field(default_factory=dict)
    closed: list[SimTrade] = field(default_factory=list)
    entries_by_day: dict[str, int] = field(default_factory=dict)
    realized_by_day: dict[str, float] = field(default_factory=dict)
    last_loss_at: dict[str, datetime] = field(default_factory=dict)


def _parse_ts(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def _et_day(ts: datetime) -> str:
    return ts.astimezone(ET).strftime("%Y-%m-%d")


def _utc_day(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%d")


def _day_fn(market: str):
    """The day-bucketing function for a market. Stocks bucket by the ET SESSION
    day (the default — keeps every existing stock backtest byte-identical);
    crypto is 24/7 and Alpaca's crypto bars are UTC-aligned, so crypto buckets by
    the UTC calendar day — matching how crypto movers are keyed in the cache."""
    return _utc_day if market == "crypto" else _et_day


def _prepare(bars: list[dict], day_of=_et_day) -> list[dict]:
    """Annotate each bar with day-gain vs previous day's close and a running
    intraday VWAP. `day_of` buckets bars into trading days (ET for stocks, UTC
    for crypto)."""
    out = []
    prev_day_close: float | None = None
    cur_day: str | None = None
    last_close: float | None = None
    cum_pv = cum_v = 0.0
    for i, bar in enumerate(bars):
        ts = _parse_ts(bar["t"])
        day = day_of(ts)
        first_of_day = day != cur_day
        if first_of_day:
            prev_day_close = last_close
            cur_day = day
            cum_pv = cum_v = 0.0
        volume = float(bar.get("v") or 0)
        cum_pv += float(bar.get("vw") or bar["c"]) * volume
        cum_v += volume
        change_pct = ((bar["c"] / prev_day_close - 1) * 100) if prev_day_close else None
        # Last bar of its day: the "before the close" moment a flatten-before-
        # close exit needs. `first_of_day` distinguishes a genuine intraday close
        # from a DAILY bar (which is both first and last of its day) — the entry
        # guard must not treat a one-bar day as "the flatten bar".
        next_day = day_of(_parse_ts(bars[i + 1]["t"])) if i + 1 < len(bars) else None
        out.append(
            {
                "ts": ts,
                "day": day,
                "close": float(bar["c"]),
                "change_pct": change_pct,
                "vwap": (cum_pv / cum_v) if cum_v else None,
                "last_of_day": next_day != day,
                "first_of_day": first_of_day,
            }
        )
        last_close = float(bar["c"])
    return out


def _max_drawdown(equity: list[float]) -> float:
    peak = equity[0] if equity else 0.0
    worst = 0.0
    for value in equity:
        peak = max(peak, value)
        if peak > 0:
            worst = max(worst, (peak - value) / peak * 100)
    return round(worst, 2)


def _hold_benchmark(prepared: dict[str, list[dict]], days_index: list[str]) -> list[float | None]:
    """Equal-weight buy-and-hold of the SAME symbols the strategy traded,
    computed from the bars we already downloaded (no extra API calls).

    This is the comparison that actually answers "should I have just held it?"
    — the market benchmark (SPY/BTC) answers a different question.
    """
    per_symbol_days: dict[str, dict[str, float]] = {}
    for symbol, series in prepared.items():
        per_day: dict[str, float] = {}
        for bar in series:
            per_day[bar["day"]] = bar["close"]  # last bar of that day wins
        per_symbol_days[symbol] = per_day

    bases: dict[str, float] = {}
    last_seen: dict[str, float] = {}
    out: list[float | None] = []
    for day in days_index:
        returns = []
        for symbol, per_day in per_symbol_days.items():
            price = per_day.get(day, last_seen.get(symbol))
            if price is None:
                continue  # symbol hadn't started trading yet
            last_seen[symbol] = price
            bases.setdefault(symbol, price)
            returns.append(price / bases[symbol] - 1)
        out.append(round(sum(returns) / len(returns) * 100, 2) if returns else None)
    return out


def run_backtest(
    strategy: dict,
    bars_by_symbol: dict[str, list[dict]],
    risk: dict,
    starting_cash: float = 5000.0,
    spread_pct: float = 0.1,
    eligible_by_day: dict[str, set[str]] | None = None,
    market: str = "stock",
) -> dict:
    """Pure simulation: strategy dict (same shape as the DB row), raw bars per
    symbol, global risk config. Returns metrics + equity curve + trades.

    `eligible_by_day` powers "scanner replay": when given, a symbol may only be
    ENTERED on a day it appears in that day's set (the day's reconstructed
    top-N risers). Exits are never gated — an open position always manages
    itself. When None, every symbol is eligible every day (fixed-list mode).

    `market` selects day bucketing: 'stock' (default) keys days by the ET session
    day; 'crypto' keys by the UTC calendar day, matching the crypto movers cache.
    The default keeps every existing stock backtest byte-identical."""
    params = strategy["params"]
    swing = strategy["swing_mode"]
    sizing = strategy["sizing_usd"]
    slip = spread_pct / 100
    day_of = _day_fn(market)

    prepared = {s: _prepare(b, day_of) for s, b in bars_by_symbol.items() if b}
    _annotate_macd(prepared, params)  # no-op unless the strategy opts into MACD
    _annotate_atr(prepared, bars_by_symbol, params)  # no-op unless the strategy opts into ATR
    _annotate_rsi(prepared, params)  # no-op unless the strategy opts into an RSI rule
    # chronological event stream across all symbols
    events: dict[datetime, dict[str, dict]] = {}
    for symbol, series in prepared.items():
        for bar in series:
            events.setdefault(bar["ts"], {})[symbol] = bar
    if not events:
        return {"error": "No historical bars for those symbols/timeframe."}

    state = SimState(cash=starting_cash)
    equity_curve: list[tuple[str, float]] = []
    last_price: dict[str, float] = {}
    max_deployed = 0.0
    bars_with_position = 0
    total_bar_ticks = 0
    diag = {
        "bars_evaluated": 0,
        "rejected_day_gain": 0,
        "rejected_vwap": 0,
        "rejected_entry_window": 0,
        "entry_ok_but_rail_blocked": 0,
        "too_small_or_no_cash": 0,
        "max_day_gain_pct": None,
        "days_reaching_min_gain": set(),
    }
    # Per-day tally of WHY each candidate was rejected — turned into a plain-English
    # "why no entry" reason for every day that traded nothing (see the chart panel).
    day_reject: dict[str, Counter] = {}

    for ts in sorted(events):
        bars = events[ts]
        for symbol, bar in bars.items():
            last_price[symbol] = bar["close"]
        day = day_of(ts)

        # ---- exits first ----
        for symbol, trade in list(state.open_trades.items()):
            bar = bars.get(symbol)
            if not bar:
                continue
            price = bar["close"]
            trade.high_water = max(trade.high_water, price)
            should_exit, reason = evaluate_exit(
                params, swing, trade.entry_price, trade.entry_at,
                trade.high_water, price, bar["vwap"], ts, bar.get("last_of_day", False),
                macd_bullish=bar.get("macd_bullish"),
                atr_pct=bar.get("atr_pct"),
                rsi=bar.get("rsi"),
            )
            if not should_exit:
                continue
            fill = price * (1 - slip)
            trade.exit_price = fill
            trade.exit_at = ts
            trade.exit_reason = reason
            trade.pnl = round((fill - trade.entry_price) * trade.qty, 2)
            state.cash += fill * trade.qty
            state.realized_by_day[day] = state.realized_by_day.get(day, 0.0) + trade.pnl
            if trade.pnl < 0:
                state.last_loss_at[symbol] = ts
            state.closed.append(trade)
            del state.open_trades[symbol]

        # ---- entries ----
        eligible = eligible_by_day.get(day) if eligible_by_day is not None else None
        for symbol, bar in bars.items():
            if eligible is not None and symbol not in eligible:
                day_reject.setdefault(day, Counter())["not_eligible"] += 1
                continue  # scanner replay: not a top-N riser on this day
            if bar["change_pct"] is None:
                continue
            # Never open a position on the very bar we'd flatten it for the close —
            # a scalp with no time to work. Only applies to a genuine intraday
            # last bar; a DAILY bar is both first and last of its day, so skipping
            # it here would (wrongly) block every entry on the daily fallback.
            if (
                bar.get("last_of_day")
                and not bar.get("first_of_day")
                and params.get("exit", {}).get("flatten_before_close")
            ):
                continue
            diag["bars_evaluated"] += 1
            if diag["max_day_gain_pct"] is None or bar["change_pct"] > diag["max_day_gain_pct"]:
                diag["max_day_gain_pct"] = round(bar["change_pct"], 2)
            if bar["change_pct"] >= params.get("entry", {}).get("min_day_gain_pct", 0):
                diag["days_reaching_min_gain"].add(day)
            # recompute inside the loop: an entry this bar must count against
            # the rails for the next candidate in the same bar
            open_exposure = sum(t.entry_price * t.qty for t in state.open_trades.values())
            equity = state.cash + open_exposure
            cand = Candidate(
                symbol=symbol, asset_class=strategy["asset_class"],
                price=bar["close"], change_pct=bar["change_pct"], vwap=bar["vwap"],
                macd_bullish=bar.get("macd_bullish"),
                rsi=bar.get("rsi"),
            )
            ok, entry_reason = evaluate_entry(params, cand, ts.astimezone(ET))
            if not ok:
                if "< required" in entry_reason:
                    diag["rejected_day_gain"] += 1
                elif "VWAP" in entry_reason:
                    diag["rejected_vwap"] += 1
                elif "entry window" in entry_reason:
                    diag["rejected_entry_window"] += 1
                day_reject.setdefault(day, Counter())[_reject_category(entry_reason)] += 1
                continue
            # ATR sizing (opt-in): size so a stop-out loses ~risk_usd, capped at
            # the sleeve; falls back to the fixed `sizing` when off or atr_pct is
            # unavailable. Byte-identical to `sizing` when ATR sizing is off.
            entry_sizing = atr_position_size(params, sizing, strategy["sleeve_usd"], bar.get("atr_pct"))
            daily_loss = max(0.0, -state.realized_by_day.get(day, 0.0))
            ctx = RailContext(
                equity=equity,
                open_positions_total=len(state.open_trades),
                open_exposure_usd=open_exposure,
                open_positions_strategy=len(state.open_trades),
                open_exposure_strategy_usd=open_exposure,
                entries_today=state.entries_by_day.get(day, 0),
                already_open_symbol=symbol in state.open_trades,
                last_loss_at=state.last_loss_at.get(symbol),
                loss_sale_within_31d=(
                    strategy["asset_class"] == "stock"
                    and symbol in state.last_loss_at
                    and (ts - state.last_loss_at[symbol]) <= timedelta(days=31)
                ),
                risk=risk,
                leverage_unlocked=False,
                daily_loss_usd=daily_loss,
            )
            # cooldown rail uses wall-clock now(); replicate it against sim time instead
            last_loss = ctx.last_loss_at
            ctx.last_loss_at = None
            rails_ok, rails_reason = check_rails(
                {"max_positions": strategy["max_positions"], "sleeve_usd": strategy["sleeve_usd"]},
                entry_sizing, ctx,
            )
            if rails_ok and last_loss is not None:
                cooldown = timedelta(hours=risk.get("cooldown_hours_after_loss", 24))
                if ts - last_loss < cooldown:
                    rails_ok = False
            if not rails_ok:
                diag["entry_ok_but_rail_blocked"] += 1
                day_reject.setdefault(day, Counter())["rail"] += 1
                continue
            fill = bar["close"] * (1 + slip)
            qty = _entry_qty(strategy["asset_class"], entry_sizing, fill, _fractional(params))
            if qty <= 0 or fill * qty > state.cash:
                diag["too_small_or_no_cash"] += 1
                day_reject.setdefault(day, Counter())["sizing"] += 1
                continue
            state.cash -= fill * qty
            state.entries_by_day[day] = state.entries_by_day.get(day, 0) + 1
            state.open_trades[symbol] = SimTrade(
                symbol=symbol, qty=qty, entry_price=fill, entry_at=ts,
                entry_reason=entry_reason, high_water=fill,
            )

        # How much of the account was actually working? (the dilution story)
        deployed = sum(t.entry_price * t.qty for t in state.open_trades.values())
        max_deployed = max(max_deployed, deployed)
        total_bar_ticks += 1
        if state.open_trades:
            bars_with_position += 1

        mark = state.cash + sum(t.qty * last_price.get(t.symbol, t.entry_price) for t in state.open_trades.values())
        if not equity_curve or equity_curve[-1][0] != day:
            equity_curve.append((day, mark))
        else:
            equity_curve[-1] = (day, mark)

    # liquidate leftovers at the last seen price so metrics are complete
    for symbol, trade in list(state.open_trades.items()):
        fill = last_price.get(symbol, trade.entry_price) * (1 - slip)
        trade.exit_price = fill
        trade.exit_at = max(events)
        trade.exit_reason = "end of backtest (forced liquidation)"
        trade.pnl = round((fill - trade.entry_price) * trade.qty, 2)
        state.cash += fill * trade.qty
        state.closed.append(trade)
    state.open_trades.clear()

    min_gain = params.get("entry", {}).get("min_day_gain_pct", 0)
    qualifying_days = len(diag.pop("days_reaching_min_gain"))
    diag["days_reaching_min_gain"] = qualifying_days
    if not state.closed:
        if diag["max_day_gain_pct"] is not None and diag["max_day_gain_pct"] < min_gain:
            diag["summary"] = (
                f"No bar ever reached the {min_gain}% day-gain threshold — the biggest day-gain "
                f"seen at any evaluated bar was {diag['max_day_gain_pct']}%. Lower the minimum gain "
                "or pick more volatile symbols."
            )
        elif qualifying_days and diag["rejected_vwap"] >= max(diag["rejected_entry_window"], 1):
            diag["summary"] = (
                f"The gain threshold was reached on {qualifying_days} day(s), but the 'price above "
                "VWAP' condition rejected the qualifying bars. Try disabling the VWAP rule to see "
                "the difference."
            )
        elif qualifying_days and diag["rejected_entry_window"] > 0:
            diag["summary"] = (
                f"The gain threshold was reached on {qualifying_days} day(s), but only outside the "
                "entry time window. Consider widening the window."
            )
        elif diag["entry_ok_but_rail_blocked"] or diag["too_small_or_no_cash"]:
            diag["summary"] = (
                "Entries qualified but were blocked by risk rails or position sizing "
                "(sleeve/exposure/cash — check $ per trade vs share price)."
            )
        else:
            diag["summary"] = "No bars satisfied all entry conditions simultaneously."
    else:
        diag["summary"] = None

    closed = state.closed
    wins = [t for t in closed if (t.pnl or 0) > 0]
    losses = [t for t in closed if (t.pnl or 0) <= 0]
    gross_win = sum(t.pnl or 0 for t in wins)
    gross_loss = -sum(t.pnl or 0 for t in losses)
    final_equity = state.cash
    equity_values = [v for _, v in equity_curve]
    net_pnl = round(final_equity - starting_cash, 2)
    days_index = [d for d, _ in equity_curve]

    # Capital deployment: a great return on 4% of the account is a small
    # return on the account. Surface both so they can't be confused.
    pct_deployed = round(max_deployed / starting_cash * 100, 2) if starting_cash else 0.0
    return_on_deployed = round(net_pnl / max_deployed * 100, 2) if max_deployed > 0 else None
    time_in_market = round(bars_with_position / total_bar_ticks * 100, 1) if total_bar_ticks else 0.0

    return {
        "starting_cash": starting_cash,
        "final_equity": round(final_equity, 2),
        "net_pnl": net_pnl,
        "net_pnl_pct": round((final_equity / starting_cash - 1) * 100, 2),
        "trades": len(closed),
        "win_rate": round(len(wins) / len(closed) * 100, 1) if closed else None,
        "avg_win": round(gross_win / len(wins), 2) if wins else None,
        "avg_loss": round(-gross_loss / len(losses), 2) if losses else None,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
        "max_drawdown_pct": _max_drawdown(equity_values),
        "spread_cost_pct_per_side": spread_pct,
        "max_deployed_usd": round(max_deployed, 2),
        "pct_capital_deployed": pct_deployed,
        "return_on_deployed_pct": return_on_deployed,
        "time_in_market_pct": time_in_market,
        "diagnosis": diag,
        # {day -> plain-English reason} for every day that evaluated candidates but
        # opened nothing — the chart shows it when you land on a no-trade day.
        "no_trade_reasons": _summarize_no_trade_days(day_reject, state.entries_by_day),
        "equity_days": days_index,
        "equity": [round((v / starting_cash - 1) * 100, 2) for _, v in equity_curve],
        "hold_benchmark": _hold_benchmark(prepared, days_index),
        "hold_benchmark_label": (
            list(prepared)[0] if len(prepared) == 1 else f"{len(prepared)} symbols (equal weight)"
        ),
        "trade_list": [
            {
                "symbol": t.symbol, "qty": t.qty,
                "entry_price": round(t.entry_price, 4), "entry_at": t.entry_at.isoformat(),
                # Day strings so the chart can place markers without the frontend
                # re-deriving timezones and drifting off by a day (ET for stocks,
                # UTC for crypto — matching the equity-curve day index).
                "entry_day": day_of(t.entry_at),
                "entry_reason": t.entry_reason,
                "exit_price": round(t.exit_price or 0, 4),
                "exit_at": t.exit_at.isoformat() if t.exit_at else None,
                "exit_day": day_of(t.exit_at) if t.exit_at else None,
                "exit_reason": t.exit_reason, "pnl": t.pnl,
            }
            for t in closed
        ],
    }


@dataclass
class PortfolioTrade(SimTrade):
    """A SimTrade that also remembers which strategy opened it — the portfolio
    run shares ONE account across N strategies, so every fill must be attributable
    back to its strategy for the contribution breakdown."""

    strategy_id: int = 0
    strategy_name: str = ""


def run_portfolio_backtest(
    strategies: list[dict],
    bars_by_strategy: dict[int, dict[str, list[dict]]],
    risk: dict,
    starting_cash: float = 5000.0,
    spread_pct: float = 0.1,
    market: str = "stock",
) -> dict:
    """Portfolio simulation: replay N strategies over ONE merged timeline sharing
    a SINGLE cash account and the GLOBAL risk rails — exactly like the live engine,
    where every enabled strategy competes for the same account.

    Each `strategies[i]` is a strategy dict (the run_backtest shape) PLUS `id` and
    `name`. `bars_by_strategy` maps strategy id → its own {symbol: raw bars}; a
    strategy trades only its own universe. The rails that bind are cross-strategy:
    max_total_positions, exposure ≤ equity (no leverage), the per-day trade-rate
    limiter, and the daily-loss kill switch — while each strategy keeps its own
    sleeve, sizing and max_positions. `market` selects day bucketing (ET for
    stocks, UTC for crypto), matching run_backtest.

    Returns the standard single-run metrics + equity curve at PORTFOLIO level, PLUS
    a per-strategy `contributions` breakdown whose realized P&L sums to the
    portfolio net P&L. The single-strategy run_backtest is untouched — this is a
    separate arbitration layer over the shared primitives.
    """
    day_of = _day_fn(market)
    slip = spread_pct / 100
    strat_by_id = {s["id"]: s for s in strategies}
    order = {s["id"]: i for i, s in enumerate(strategies)}  # deterministic tie-break

    # Prepare each strategy's own bars, then fold everything into one chronological
    # event stream tagged with the strategy that owns each bar.
    prepared_by_strategy: dict[int, dict[str, list[dict]]] = {
        sid: {s: _prepare(b, day_of) for s, b in (bars_by_strategy.get(sid) or {}).items() if b}
        for sid in strat_by_id
    }
    # Each strategy carries its own MACD/ATR periods/toggles; annotate its own bars.
    for sid, prepared in prepared_by_strategy.items():
        _annotate_macd(prepared, strat_by_id[sid]["params"])
        _annotate_atr(prepared, bars_by_strategy.get(sid) or {}, strat_by_id[sid]["params"])
        _annotate_rsi(prepared, strat_by_id[sid]["params"])
    events: dict[datetime, list[tuple[int, str, dict]]] = {}
    for sid, series_map in prepared_by_strategy.items():
        for symbol, series in series_map.items():
            for bar in series:
                events.setdefault(bar["ts"], []).append((sid, symbol, bar))
    if not events:
        return {"error": "No historical bars for those strategies/timeframe."}

    cash = starting_cash
    open_trades: dict[str, PortfolioTrade] = {}  # keyed by symbol (account-wide, matches the live 'already open' rail)
    closed: list[PortfolioTrade] = []
    entries_by_day: dict[str, int] = {}          # cross-strategy trade-rate limiter
    realized_by_day: dict[str, float] = {}       # daily-loss kill switch
    last_loss_at: dict[str, datetime] = {}
    last_price: dict[str, float] = {}
    equity_curve: list[tuple[str, float]] = []
    max_deployed = 0.0
    bars_with_position = 0
    total_bar_ticks = 0

    for ts in sorted(events):
        tick = events[ts]
        # index this tick's bars for exit lookup + refresh last-seen prices
        bar_at: dict[tuple[int, str], dict] = {}
        for sid, symbol, bar in tick:
            bar_at[(sid, symbol)] = bar
            last_price[symbol] = bar["close"]
        day = day_of(ts)

        # ---- exits first (each trade managed by ITS OWN strategy's rules) ----
        for symbol, trade in list(open_trades.items()):
            bar = bar_at.get((trade.strategy_id, symbol))
            if not bar:
                continue
            strat = strat_by_id[trade.strategy_id]
            price = bar["close"]
            trade.high_water = max(trade.high_water, price)
            should_exit, reason = evaluate_exit(
                strat["params"], strat["swing_mode"], trade.entry_price, trade.entry_at,
                trade.high_water, price, bar["vwap"], ts, bar.get("last_of_day", False),
                macd_bullish=bar.get("macd_bullish"),
                atr_pct=bar.get("atr_pct"),
                rsi=bar.get("rsi"),
            )
            if not should_exit:
                continue
            fill = price * (1 - slip)
            trade.exit_price = fill
            trade.exit_at = ts
            trade.exit_reason = reason
            trade.pnl = round((fill - trade.entry_price) * trade.qty, 2)
            cash += fill * trade.qty
            realized_by_day[day] = realized_by_day.get(day, 0.0) + trade.pnl
            if trade.pnl < 0:
                last_loss_at[symbol] = ts
            closed.append(trade)
            del open_trades[symbol]

        # ---- entries (deterministic order: strategy index, then symbol) ----
        for sid, symbol, bar in sorted(tick, key=lambda e: (order[e[0]], e[1])):
            strat = strat_by_id[sid]
            params = strat["params"]
            if bar["change_pct"] is None:
                continue
            # never open on the very bar we'd flatten for the close (see run_backtest)
            if (
                bar.get("last_of_day")
                and not bar.get("first_of_day")
                and params.get("exit", {}).get("flatten_before_close")
            ):
                continue
            # recompute the shared account state for EACH candidate so an entry
            # earlier in this bar counts against the rails for the next one
            open_exposure = sum(t.entry_price * t.qty for t in open_trades.values())
            equity = cash + open_exposure
            cand = Candidate(
                symbol=symbol, asset_class=strat["asset_class"],
                price=bar["close"], change_pct=bar["change_pct"], vwap=bar["vwap"],
                macd_bullish=bar.get("macd_bullish"),
                rsi=bar.get("rsi"),
            )
            ok, entry_reason = evaluate_entry(params, cand, ts.astimezone(ET))
            if not ok:
                continue
            strat_open = [t for t in open_trades.values() if t.strategy_id == sid]
            strat_exposure = sum(t.entry_price * t.qty for t in strat_open)
            daily_loss = max(0.0, -realized_by_day.get(day, 0.0))
            # ATR sizing (opt-in) per this strategy; falls back to its fixed
            # sizing_usd when off or atr_pct is unavailable. Flows through BOTH the
            # rails and the fill below, matching the live engine.
            sizing = atr_position_size(
                params, strat["sizing_usd"], strat["sleeve_usd"], bar.get("atr_pct")
            )
            ctx = RailContext(
                equity=equity,
                open_positions_total=len(open_trades),
                open_exposure_usd=open_exposure,
                open_positions_strategy=len(strat_open),
                open_exposure_strategy_usd=strat_exposure,
                entries_today=entries_by_day.get(day, 0),
                already_open_symbol=symbol in open_trades,
                last_loss_at=last_loss_at.get(symbol),
                loss_sale_within_31d=(
                    strat["asset_class"] == "stock"
                    and symbol in last_loss_at
                    and (ts - last_loss_at[symbol]) <= timedelta(days=31)
                ),
                risk=risk,
                leverage_unlocked=False,
                daily_loss_usd=daily_loss,
            )
            # cooldown rail uses wall-clock now(); replicate it against sim time
            last_loss = ctx.last_loss_at
            ctx.last_loss_at = None
            rails_ok, _rails_reason = check_rails(
                {"max_positions": strat["max_positions"], "sleeve_usd": strat["sleeve_usd"]},
                sizing, ctx,
            )
            if rails_ok and last_loss is not None:
                cooldown = timedelta(hours=risk.get("cooldown_hours_after_loss", 24))
                if ts - last_loss < cooldown:
                    rails_ok = False
            if not rails_ok:
                continue
            fill = bar["close"] * (1 + slip)
            qty = _entry_qty(strat["asset_class"], sizing, fill, _fractional(params))
            if qty <= 0 or fill * qty > cash:
                continue
            cash -= fill * qty
            entries_by_day[day] = entries_by_day.get(day, 0) + 1
            open_trades[symbol] = PortfolioTrade(
                symbol=symbol, qty=qty, entry_price=fill, entry_at=ts,
                entry_reason=entry_reason, high_water=fill,
                strategy_id=sid, strategy_name=strat.get("name", ""),
            )

        deployed = sum(t.entry_price * t.qty for t in open_trades.values())
        max_deployed = max(max_deployed, deployed)
        total_bar_ticks += 1
        if open_trades:
            bars_with_position += 1

        mark = cash + sum(t.qty * last_price.get(t.symbol, t.entry_price) for t in open_trades.values())
        if not equity_curve or equity_curve[-1][0] != day:
            equity_curve.append((day, mark))
        else:
            equity_curve[-1] = (day, mark)

    # liquidate leftovers at the last seen price so metrics are complete
    for symbol, trade in list(open_trades.items()):
        fill = last_price.get(symbol, trade.entry_price) * (1 - slip)
        trade.exit_price = fill
        trade.exit_at = max(events)
        trade.exit_reason = "end of backtest (forced liquidation)"
        trade.pnl = round((fill - trade.entry_price) * trade.qty, 2)
        cash += fill * trade.qty
        closed.append(trade)
    open_trades.clear()

    wins = [t for t in closed if (t.pnl or 0) > 0]
    losses = [t for t in closed if (t.pnl or 0) <= 0]
    gross_win = sum(t.pnl or 0 for t in wins)
    gross_loss = -sum(t.pnl or 0 for t in losses)
    final_equity = cash
    net_pnl = round(final_equity - starting_cash, 2)
    equity_values = [v for _, v in equity_curve]
    days_index = [d for d, _ in equity_curve]

    pct_deployed = round(max_deployed / starting_cash * 100, 2) if starting_cash else 0.0
    return_on_deployed = round(net_pnl / max_deployed * 100, 2) if max_deployed > 0 else None
    time_in_market = round(bars_with_position / total_bar_ticks * 100, 1) if total_bar_ticks else 0.0

    # Per-strategy contribution: realized P&L, trade count, and share of the
    # portfolio result. These realized totals sum EXACTLY to net_pnl (every fill
    # flows through the one shared cash balance).
    realized_total = round(sum(t.pnl or 0 for t in closed), 2)
    contributions = []
    for strat in strategies:
        sid = strat["id"]
        mine = [t for t in closed if t.strategy_id == sid]
        my_wins = [t for t in mine if (t.pnl or 0) > 0]
        realized = round(sum(t.pnl or 0 for t in mine), 2)
        contributions.append(
            {
                "strategy_id": sid,
                "strategy_name": strat.get("name", ""),
                "realized_pnl": realized,
                "trades": len(mine),
                "wins": len(my_wins),
                "win_rate": round(len(my_wins) / len(mine) * 100, 1) if mine else None,
                # Share of the portfolio's realized total. Sign-preserving, so a
                # losing sleeve inside a winning book shows a negative share.
                "share_pct": round(realized / realized_total * 100, 1) if realized_total else None,
            }
        )

    all_symbols = {s: series for m in prepared_by_strategy.values() for s, series in m.items()}
    return {
        "starting_cash": starting_cash,
        "final_equity": round(final_equity, 2),
        "net_pnl": net_pnl,
        "net_pnl_pct": round((final_equity / starting_cash - 1) * 100, 2),
        "trades": len(closed),
        "win_rate": round(len(wins) / len(closed) * 100, 1) if closed else None,
        "avg_win": round(gross_win / len(wins), 2) if wins else None,
        "avg_loss": round(-gross_loss / len(losses), 2) if losses else None,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
        "max_drawdown_pct": _max_drawdown(equity_values),
        "spread_cost_pct_per_side": spread_pct,
        "max_deployed_usd": round(max_deployed, 2),
        "pct_capital_deployed": pct_deployed,
        "return_on_deployed_pct": return_on_deployed,
        "time_in_market_pct": time_in_market,
        "realized_total": realized_total,
        "contributions": contributions,
        "strategy_count": len(strategies),
        "strategy_names": [s.get("name", "") for s in strategies],
        "equity_days": days_index,
        "equity": [round((v / starting_cash - 1) * 100, 2) for _, v in equity_curve],
        "hold_benchmark": _hold_benchmark(all_symbols, days_index),
        "hold_benchmark_label": (
            list(all_symbols)[0] if len(all_symbols) == 1
            else f"{len(all_symbols)} symbols (equal weight)"
        ),
        "trade_list": [
            {
                "symbol": t.symbol, "qty": t.qty,
                "strategy_id": t.strategy_id, "strategy_name": t.strategy_name,
                "entry_price": round(t.entry_price, 4), "entry_at": t.entry_at.isoformat(),
                "entry_day": day_of(t.entry_at),
                "entry_reason": t.entry_reason,
                "exit_price": round(t.exit_price or 0, 4),
                "exit_at": t.exit_at.isoformat() if t.exit_at else None,
                "exit_day": day_of(t.exit_at) if t.exit_at else None,
                "exit_reason": t.exit_reason, "pnl": t.pnl,
            }
            for t in closed
        ],
    }


async def fetch_benchmark(
    client: AlpacaClient, asset_class: str, start_iso: str, days_index: list[str],
    market: str = "stock",
) -> list[float | None]:
    """Buy-and-hold % series for SPY (stocks) or BTC/USD (crypto), aligned to
    the backtest's day index. `market` buckets the benchmark's daily closes into
    the same day keys the backtest used (ET for a stock replay, UTC for crypto)."""
    symbol = "SPY" if asset_class == "stock" else "BTC/USD"
    day_of = _day_fn(market)
    bars = await client.historical_bars([symbol], asset_class, "1Day", start_iso)
    series = bars.get(symbol) or []
    closes: dict[str, float] = {day_of(_parse_ts(b["t"])): float(b["c"]) for b in series}
    base: float | None = None
    out: list[float | None] = []
    last: float | None = None
    for day in days_index:
        price = closes.get(day, last)
        last = price
        if price is None:
            out.append(None)
            continue
        if base is None:
            base = price
        out.append(round((price / base - 1) * 100, 2))
    return out
