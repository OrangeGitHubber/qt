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

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from qt.broker.alpaca import AlpacaClient
from qt.services.engine import Candidate, RailContext, check_rails, evaluate_entry, evaluate_exit

ET = ZoneInfo("America/New_York")


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


def _prepare(bars: list[dict]) -> list[dict]:
    """Annotate each bar with day-gain vs previous ET day's close and a
    running intraday VWAP."""
    out = []
    prev_day_close: float | None = None
    cur_day: str | None = None
    last_close: float | None = None
    cum_pv = cum_v = 0.0
    for bar in bars:
        ts = _parse_ts(bar["t"])
        day = _et_day(ts)
        if day != cur_day:
            prev_day_close = last_close
            cur_day = day
            cum_pv = cum_v = 0.0
        volume = float(bar.get("v") or 0)
        cum_pv += float(bar.get("vw") or bar["c"]) * volume
        cum_v += volume
        change_pct = ((bar["c"] / prev_day_close - 1) * 100) if prev_day_close else None
        out.append(
            {
                "ts": ts,
                "day": day,
                "close": float(bar["c"]),
                "change_pct": change_pct,
                "vwap": (cum_pv / cum_v) if cum_v else None,
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
) -> dict:
    """Pure simulation: strategy dict (same shape as the DB row), raw bars per
    symbol, global risk config. Returns metrics + equity curve + trades."""
    params = strategy["params"]
    swing = strategy["swing_mode"]
    sizing = strategy["sizing_usd"]
    slip = spread_pct / 100

    prepared = {s: _prepare(b) for s, b in bars_by_symbol.items() if b}
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

    for ts in sorted(events):
        bars = events[ts]
        for symbol, bar in bars.items():
            last_price[symbol] = bar["close"]
        day = _et_day(ts)

        # ---- exits first ----
        for symbol, trade in list(state.open_trades.items()):
            bar = bars.get(symbol)
            if not bar:
                continue
            price = bar["close"]
            trade.high_water = max(trade.high_water, price)
            should_exit, reason = evaluate_exit(
                params, swing, trade.entry_price, trade.entry_at,
                trade.high_water, price, bar["vwap"], ts, False,
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
        for symbol, bar in bars.items():
            if bar["change_pct"] is None:
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
            )
            ok, entry_reason = evaluate_entry(params, cand, ts.astimezone(ET))
            if not ok:
                if "< required" in entry_reason:
                    diag["rejected_day_gain"] += 1
                elif "VWAP" in entry_reason:
                    diag["rejected_vwap"] += 1
                elif "entry window" in entry_reason:
                    diag["rejected_entry_window"] += 1
                continue
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
                sizing, ctx,
            )
            if rails_ok and last_loss is not None:
                cooldown = timedelta(hours=risk.get("cooldown_hours_after_loss", 24))
                if ts - last_loss < cooldown:
                    rails_ok = False
            if not rails_ok:
                diag["entry_ok_but_rail_blocked"] += 1
                continue
            fill = bar["close"] * (1 + slip)
            qty = float(int(sizing // fill)) if strategy["asset_class"] == "stock" else round(sizing / fill, 6)
            if qty <= 0 or fill * qty > state.cash:
                diag["too_small_or_no_cash"] += 1
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
                # ET day strings so the chart can place markers without the
                # frontend re-deriving timezones and drifting off by a day
                "entry_day": _et_day(t.entry_at),
                "entry_reason": t.entry_reason,
                "exit_price": round(t.exit_price or 0, 4),
                "exit_at": t.exit_at.isoformat() if t.exit_at else None,
                "exit_day": _et_day(t.exit_at) if t.exit_at else None,
                "exit_reason": t.exit_reason, "pnl": t.pnl,
            }
            for t in closed
        ],
    }


async def fetch_benchmark(
    client: AlpacaClient, asset_class: str, start_iso: str, days_index: list[str]
) -> list[float | None]:
    """Buy-and-hold % series for SPY (stocks) or BTC/USD (crypto), aligned to
    the backtest's day index."""
    symbol = "SPY" if asset_class == "stock" else "BTC/USD"
    bars = await client.historical_bars([symbol], asset_class, "1Day", start_iso)
    series = bars.get(symbol) or []
    closes: dict[str, float] = {_et_day(_parse_ts(b["t"])): float(b["c"]) for b in series}
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
