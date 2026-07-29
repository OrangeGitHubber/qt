"""The trading engine.

Design rules:
- Decision logic is in PURE functions (evaluate_entry / evaluate_exit /
  check_rails) that take plain data and return (verdict, reason) — so the
  risk rails get exhaustive unit tests with no mocking gymnastics.
- The engine never trusts itself over the journal: every action and every
  rail-rejection is persisted with its reason.
- Modes: off → shadow (journal only, no orders) → paper (simulated orders).
  Live modes arrive in Phase 5.
"""

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import func
from sqlalchemy.orm import Session

from qt.broker.alpaca import AlpacaClient, AlpacaError
from qt.broker.factory import get_client
from qt.db import session_scope
from qt.models import AuditLog, Strategy, StrategyConfigVersion, Trade
from qt.services import notify, persistence, regime, scanner, stats
from qt.settings_service import get_setting, set_setting

log = logging.getLogger("qt.engine")
ET = ZoneInfo("America/New_York")

ENGINE_MODES = ("off", "shadow", "paper")

RISK_DEFAULTS: dict = {
    "max_daily_loss_usd": 200.0,
    "max_daily_loss_pct": 5.0,
    "max_total_positions": 6,
    "max_total_exposure_usd": 3000.0,
    "max_trades_per_day": 10,
    "cooldown_hours_after_loss": 24,
    "wash_sale_guard": "block",  # block | warn | off
    "leverage_enabled": False,   # double-locked; see api/engine.py
}


def get_risk(session: Session) -> dict:
    stored = get_setting(session, "risk_config") or {}
    return {**RISK_DEFAULTS, **stored}


def get_mode(session: Session) -> str:
    return get_setting(session, "engine_mode") or "off"


# --------------------------------------------------------------------------
# Pure decision functions
# --------------------------------------------------------------------------


@dataclass
class Candidate:
    symbol: str
    asset_class: str
    price: float
    change_pct: float
    vwap: float | None = None
    # Daily-MACD momentum, computed from COMPLETED daily bars only (the adapter
    # excludes today's in-progress bar). True = bullish (line > signal), False =
    # bearish, None = not enough history to decide. Only set when a strategy opts
    # into the MACD entry filter; otherwise stays None and is ignored.
    macd_bullish: bool | None = None
    # Daily ATR as a % of price (the symbol's typical daily move), computed from
    # COMPLETED daily bars only — same look-ahead-safe fetch as macd_bullish. Only
    # set when a strategy opts into ATR sizing; otherwise stays None. Drives the
    # ATR position size at entry (see atr_position_size).
    atr_pct: float | None = None
    # Wilder's 14-day RSI from COMPLETED daily closes. Only set when a strategy
    # opts into an RSI entry band; otherwise None and ignored.
    rsi: float | None = None


@dataclass
class RailContext:
    """Everything check_rails needs, precomputed as plain numbers."""

    equity: float
    open_positions_total: int
    open_exposure_usd: float
    open_positions_strategy: int
    open_exposure_strategy_usd: float
    entries_today: int
    already_open_symbol: bool
    last_loss_at: datetime | None  # last losing exit for this symbol (any strategy)
    loss_sale_within_31d: bool  # stocks: sold this symbol at a loss in last 31 days
    risk: dict = field(default_factory=dict)
    leverage_unlocked: bool = False
    daily_loss_usd: float = 0.0


def evaluate_entry(params: dict, candidate: Candidate, now_et: datetime) -> tuple[bool, str]:
    entry = params.get("entry", {})
    min_gain = entry.get("min_day_gain_pct", 0)
    if candidate.change_pct < min_gain:
        return False, f"day gain {candidate.change_pct:.2f}% < required {min_gain}%"
    max_gain = entry.get("max_day_gain_pct", 0)
    if max_gain and candidate.change_pct > max_gain:
        return False, f"day gain {candidate.change_pct:.2f}% > max {max_gain}% (too extended to chase)"
    min_price = entry.get("min_price", 0)
    if min_price and candidate.price < min_price:
        return False, f"price ${candidate.price:.2f} < min ${min_price:g}"
    max_price = entry.get("max_price", 0)
    if max_price and candidate.price > max_price:
        return False, f"price ${candidate.price:.2f} > max ${max_price:g}"
    if entry.get("require_above_vwap"):
        if candidate.vwap is None:
            return False, "VWAP unavailable — rule requires price above VWAP"
        if candidate.price <= candidate.vwap:
            return False, f"price {candidate.price:.4f} not above VWAP {candidate.vwap:.4f}"
    start, end = entry.get("entry_window_start"), entry.get("entry_window_end")
    if start and end:
        hhmm = now_et.strftime("%H:%M")
        if not (start <= hhmm <= end):
            return False, f"outside entry window {start}–{end} ET (now {hhmm})"
    # Optional daily-MACD entry filter (off by default). Fail-closed: block on
    # both bearish (False) AND insufficient history (None) — an unproven momentum
    # signal is not a green light.
    if entry.get("require_macd_bullish") and candidate.macd_bullish is not True:
        detail = "not enough daily history" if candidate.macd_bullish is None else "line ≤ signal (bearish)"
        return False, f"MACD not bullish — {detail}"
    # Optional RSI entry band (rsi_min / rsi_max, 0 = that bound is off). Fail-
    # closed on missing RSI, like MACD — an unknown reading is not a green light.
    rsi_min = entry.get("rsi_min", 0) or 0
    rsi_max = entry.get("rsi_max", 0) or 0
    if rsi_min or rsi_max:
        if candidate.rsi is None:
            return False, "RSI unavailable — rule requires an RSI band"
        if rsi_min and candidate.rsi < rsi_min:
            return False, f"RSI {candidate.rsi:.0f} < min {rsi_min:g}"
        if rsi_max and candidate.rsi > rsi_max:
            return False, f"RSI {candidate.rsi:.0f} > max {rsi_max:g} (overbought)"
    return True, f"up {candidate.change_pct:.2f}% today" + (
        f", above VWAP" if entry.get("require_above_vwap") else ""
    ) + (", MACD bullish" if entry.get("require_macd_bullish") else "") + (
        f", RSI {candidate.rsi:.0f}" if (rsi_min or rsi_max) and candidate.rsi is not None else ""
    )


def check_rails(strategy_cfg: dict, sizing_usd: float, ctx: RailContext) -> tuple[bool, str]:
    risk = ctx.risk
    if ctx.already_open_symbol:
        return False, "rail: position already open for this symbol"
    if ctx.entries_today >= risk["max_trades_per_day"]:
        return False, f"rail: trade-rate limit reached ({risk['max_trades_per_day']}/day)"
    if ctx.open_positions_total >= risk["max_total_positions"]:
        return False, f"rail: max open positions reached ({risk['max_total_positions']})"
    if ctx.open_positions_strategy >= strategy_cfg["max_positions"]:
        return False, f"rail: strategy max positions reached ({strategy_cfg['max_positions']})"
    if ctx.open_exposure_strategy_usd + sizing_usd > strategy_cfg["sleeve_usd"]:
        return False, (
            f"rail: sleeve budget exceeded (${ctx.open_exposure_strategy_usd:,.0f} held "
            f"+ ${sizing_usd:,.0f} > ${strategy_cfg['sleeve_usd']:,.0f})"
        )
    exposure_cap = min(risk["max_total_exposure_usd"], ctx.equity) if not (
        risk.get("leverage_enabled") and ctx.leverage_unlocked
    ) else risk["max_total_exposure_usd"]
    if ctx.open_exposure_usd + sizing_usd > exposure_cap:
        return False, (
            f"rail: exposure cap (${ctx.open_exposure_usd:,.0f} + ${sizing_usd:,.0f} "
            f"> ${exposure_cap:,.0f}{' — no-leverage rail' if exposure_cap == ctx.equity else ''})"
        )
    max_loss = min(risk["max_daily_loss_usd"], ctx.equity * risk["max_daily_loss_pct"] / 100)
    if ctx.daily_loss_usd >= max_loss:
        return False, f"rail: daily loss kill switch (${ctx.daily_loss_usd:,.0f} ≥ ${max_loss:,.0f})"
    if ctx.last_loss_at is not None:
        cooldown = timedelta(hours=risk["cooldown_hours_after_loss"])
        since = datetime.now(timezone.utc) - ctx.last_loss_at
        if since < cooldown:
            return False, f"rail: cooldown after loss ({since.total_seconds()/3600:.1f}h of {risk['cooldown_hours_after_loss']}h)"
    guard = risk.get("wash_sale_guard", "block")
    if ctx.loss_sale_within_31d and guard == "block":
        return False, "rail: wash-sale guard — sold this stock at a loss within 31 days"
    return True, "all rails passed" + (
        " (wash-sale warning: loss sale within 31 days)" if ctx.loss_sale_within_31d and guard == "warn" else ""
    )


def evaluate_exit(
    params: dict,
    swing_mode: bool,
    entry_price: float,
    entry_at: datetime,
    high_water: float,
    price: float,
    vwap: float | None,
    now_utc: datetime,
    market_closes_soon: bool,
    macd_bullish: bool | None = None,
    atr_pct: float | None = None,
    rsi: float | None = None,
) -> tuple[bool, str]:
    """Return (should_exit, reason). Stops always apply; in swing mode the
    softer exits (take-profit, VWAP, time, MACD) wait until the day after entry.

    `macd_bullish` is the daily-MACD momentum for this symbol (True/False/None),
    supplied by the caller only when the strategy opts into the MACD exit signal;
    default None so existing callers are unaffected.

    `atr_pct` is the symbol's daily ATR as a % of price. When the strategy enables
    the ATR stop (params["atr"]["stop_mult"] > 0) AND atr_pct is available, the
    effective hard-stop % becomes stop_mult × atr_pct — wider for volatile names,
    tighter for calm ones — used IN PLACE OF the fixed stop_loss_pct while keeping
    the hard stop's top priority. It's recomputed from the CURRENT bar's ATR each
    tick, so the stop is volatility-adaptive: it breathes with the symbol. When
    atr_pct is unavailable (fetch blip / too little history) it FALLS BACK to the
    fixed stop_loss_pct — a position is never left stopless. Default None so
    existing callers are unaffected."""
    exit_rules = params.get("exit", {})
    change_from_entry = (price / entry_price - 1) * 100
    drop_from_high = (1 - price / high_water) * 100 if high_water else 0.0

    stop_loss = exit_rules.get("stop_loss_pct", 0)
    # ATR stop (opt-in): the effective hard stop = stop_mult × current ATR%. Falls
    # back to the fixed stop when off or atr_pct is unavailable — never stopless.
    atr_stop_mult = float((params.get("atr") or {}).get("stop_mult", 0) or 0)
    effective_stop = stop_loss
    if atr_stop_mult > 0 and atr_pct is not None and atr_pct > 0:
        effective_stop = atr_stop_mult * atr_pct
    if effective_stop and change_from_entry <= -effective_stop:
        if effective_stop != stop_loss:
            return True, (
                f"ATR stop-loss: {change_from_entry:.2f}% ≤ -{effective_stop:.2f}% "
                f"({atr_stop_mult:g}× ATR {atr_pct:.2f}%)"
            )
        return True, f"stop-loss: {change_from_entry:.2f}% ≤ -{stop_loss}%"

    trailing = exit_rules.get("trailing_stop_pct", 0)
    if trailing and drop_from_high >= trailing and price > entry_price * (1 - effective_stop / 100):
        return True, f"trailing stop: {drop_from_high:.2f}% off high {high_water:.4f}"

    same_day = entry_at.astimezone(ET).date() == now_utc.astimezone(ET).date()
    if swing_mode and same_day:
        return False, ""  # stops above already had their chance; be patient today

    take_profit = exit_rules.get("take_profit_pct", 0)
    if take_profit and change_from_entry >= take_profit:
        return True, f"take-profit: +{change_from_entry:.2f}% ≥ {take_profit}%"

    if exit_rules.get("exit_below_vwap") and vwap is not None and price < vwap:
        return True, f"price {price:.4f} fell below VWAP {vwap:.4f}"

    # Optional daily-MACD exit: only act on a CONFIRMED bearish cross (False).
    # None means we can't tell right now — never force an exit on that.
    if exit_rules.get("exit_on_macd_bearish") and macd_bullish is False:
        return True, "MACD turned bearish"

    # Optional RSI overbought exit: take profit on froth. 0 = off; None RSI (not
    # enough history / fetch blip) never forces an exit.
    exit_rsi_above = exit_rules.get("exit_rsi_above", 0) or 0
    if exit_rsi_above and rsi is not None and rsi >= exit_rsi_above:
        return True, f"RSI {rsi:.0f} ≥ {exit_rsi_above:g} (overbought)"

    max_hold = exit_rules.get("max_holding_hours", 0)
    if max_hold:
        held_hours = (now_utc - entry_at).total_seconds() / 3600
        if held_hours >= max_hold:
            return True, f"max holding period: {held_hours:.1f}h ≥ {max_hold}h"

    if exit_rules.get("flatten_before_close") and market_closes_soon:
        return True, "flatten before market close"

    return False, ""


# --------------------------------------------------------------------------
# Engine tick (impure shell around the pure core)
# --------------------------------------------------------------------------


def _trading_day_start(now: datetime | None = None) -> datetime:
    """The UTC instant at which today's US trading day began — 00:00 in
    America/New_York, the same ET calendar boundary `_et_day` uses everywhere
    else. The daily risk counters (trade-rate limiter and daily-loss kill
    switch) reset here.

    NOT midnight UTC: that lands at 7-8pm ET the *previous* evening, so a
    UTC-day reset would zero the counters mid-session — harmless for stocks
    (market shut by then) but wrong for 24/7 crypto, where it would hand back
    a fresh trade budget and loss headroom in the middle of a trading day.

    Returned tz-aware in UTC; SQLite ignores the tzinfo and compares the (now
    UTC) wall-clock components against the journal's naive-UTC timestamps.
    `now` is injectable so the boundary can be tested across DST and the
    ET-evening rollover without patching the clock."""
    now = now or datetime.now(timezone.utc)
    et_midnight = now.astimezone(ET).replace(hour=0, minute=0, second=0, microsecond=0)
    return et_midnight.astimezone(timezone.utc)


def _daily_loss(session: Session, mode: str, day_start: datetime) -> float:
    """Realized loss so far this trading day (positive number = loss) from
    closed trades, measured from the ET day boundary."""
    realized = (
        session.query(func.coalesce(func.sum(Trade.pnl), 0.0))
        .filter(Trade.mode == mode, Trade.status == "closed", Trade.exit_at >= day_start)
        .scalar()
    )
    return max(0.0, -float(realized))


async def _quotes_for(client: AlpacaClient, trades: list[Trade]) -> dict[str, dict]:
    stocks = sorted({t.symbol for t in trades if t.asset_class == "stock"})
    cryptos = sorted({t.symbol for t in trades if t.asset_class == "crypto"})
    quotes: dict[str, dict] = {}
    if stocks:
        quotes.update(await client.stock_snapshots(stocks))
    if cryptos:
        quotes.update(await client.crypto_snapshots(cryptos))
    return quotes


def _price_from_snapshot(snap: dict) -> tuple[float | None, float | None]:
    price = (snap.get("latestTrade") or {}).get("p") or (snap.get("dailyBar") or {}).get("c")
    vwap = (snap.get("dailyBar") or {}).get("vw")
    return (float(price) if price else None, float(vwap) if vwap else None)


# --------------------------------------------------------------------------
# MACD adapter (optional, off-by-default per-strategy signal)
#
# The pure MACD math lives in qt.services.stats and is look-ahead-safe as long
# as the caller passes only COMPLETED closes. In the LIVE engine that means
# daily bars with TODAY's in-progress bar excluded — that's the whole job of
# these helpers. Only strategies that opt into require_macd_bullish (entry) or
# exit_on_macd_bearish (exit) trigger any of this; everyone else does zero extra
# work.
# --------------------------------------------------------------------------

MACD_LOOKBACK_DAYS = 120  # ~120 daily bars: comfortably above the 12/26/9 warm-up


def _macd_periods(params: dict) -> tuple[int, int, int]:
    m = params.get("macd") or {}
    return int(m.get("fast", 12)), int(m.get("slow", 26)), int(m.get("signal", 9))


def _atr_period(params: dict) -> int:
    return int((params.get("atr") or {}).get("period", 14) or 14)


def _atr_stop_enabled(params: dict) -> bool:
    """ATR stop is on when stop_mult > 0 (it replaces the fixed hard stop)."""
    return float((params.get("atr") or {}).get("stop_mult", 0) or 0) > 0


def _atr_sizing_enabled(params: dict) -> bool:
    """ATR sizing needs BOTH a risk budget AND a stop multiple — the size is
    derived from the ATR stop distance, so it's meaningless without the stop."""
    a = params.get("atr") or {}
    return float(a.get("risk_usd", 0) or 0) > 0 and float(a.get("stop_mult", 0) or 0) > 0


RSI_PERIOD = 14  # standard Wilder period; fixed (not a user knob) to keep the config lean


def _entry_rsi_enabled(params: dict) -> bool:
    """The RSI entry band is on when either bound is set (> 0)."""
    e = params.get("entry", {})
    return float(e.get("rsi_min", 0) or 0) > 0 or float(e.get("rsi_max", 0) or 0) > 0


def _exit_rsi_enabled(params: dict) -> bool:
    """The RSI overbought exit is on when its threshold is set (> 0)."""
    return float((params.get("exit") or {}).get("exit_rsi_above", 0) or 0) > 0


def atr_position_size(
    params: dict, sizing_usd: float, sleeve_usd: float, atr_pct: float | None
) -> float:
    """The $ to commit to a new position. When ATR sizing is on (risk_usd > 0 AND
    stop_mult > 0) and atr_pct is available, size so a stop-out — a move of
    stop_mult × atr_pct against entry — loses about risk_usd:

        size = risk_usd / (stop_mult × atr_pct / 100)

    A volatile name (big atr_pct) therefore gets a SMALLER position, a calm one a
    LARGER position, for the same dollar risk. The result is CAPPED at the
    strategy's sleeve_usd so a very calm name can't compute a size that blows the
    sleeve budget, and floored at 0 (a below-minimum size then falls through to
    execution's existing 'position too small' rejection). Falls back to the fixed
    sizing_usd when ATR sizing is off or atr_pct is unavailable — sizing is never
    left undefined."""
    a = params.get("atr") or {}
    stop_mult = float(a.get("stop_mult", 0) or 0)
    risk_usd = float(a.get("risk_usd", 0) or 0)
    if risk_usd > 0 and stop_mult > 0 and atr_pct and atr_pct > 0:
        size = risk_usd / (stop_mult * atr_pct / 100)
        return max(0.0, min(size, sleeve_usd))
    return sizing_usd


def _daily_lookback_days(params: dict, want_atr: bool) -> int:
    """Calendar-day window for the daily-bars fetch. MACD-only keeps the original
    120-day window (so MACD-only fetches are unchanged); ATR widens it when its
    period is large enough to need more than that."""
    if not want_atr:
        return MACD_LOOKBACK_DAYS
    return max(MACD_LOOKBACK_DAYS, _atr_period(params) * 2 + 10)


def _completed_daily_bars(bars: list[dict], asset_class: str) -> list[dict]:
    """The COMPLETED daily bars (oldest-first) — today's in-progress bar is
    dropped so neither MACD nor ATR peeks at an unfinished day. Stocks bucket by
    the ET session day, crypto by the UTC calendar day (matching the backtester's
    day bucketing and the crypto movers cache)."""
    if not bars:
        return []
    tz = timezone.utc if asset_class == "crypto" else ET
    today = datetime.now(tz).date()
    out: list[dict] = []
    for b in bars:
        ts = datetime.fromisoformat(b["t"].replace("Z", "+00:00"))
        if ts.astimezone(tz).date() >= today:
            continue  # today's (or a future) in-progress bar — exclude
        out.append(b)
    return out


def _completed_daily_closes(bars: list[dict], asset_class: str) -> list[float]:
    """Closes of only the COMPLETED daily bars (see _completed_daily_bars)."""
    return [float(b["c"]) for b in _completed_daily_bars(bars, asset_class)]


async def _apply_daily_signals(
    client: AlpacaClient, strategy: Strategy, params: dict, candidates: list[Candidate]
) -> None:
    """Set candidate.macd_bullish and/or candidate.atr_pct for a strategy that
    opted into the MACD entry filter and/or ATR sizing, via ONE batched daily-bars
    call for the candidate symbols — daily bars are fetched ONCE and both signals
    computed from them, never twice. No-op when neither is enabled or there are no
    candidates.

    (Basket ranking may itself fetch daily bars, but over different windows
    — 60/320 days keyed to the metric. A dedicated fetch keeps these inputs
    correct and independent of whatever the ranking happened to pull.)"""
    want_macd = bool(params.get("entry", {}).get("require_macd_bullish"))
    want_atr = _atr_sizing_enabled(params)
    want_rsi = _entry_rsi_enabled(params)
    if not candidates or not (want_macd or want_atr or want_rsi):
        return
    fast, slow, signal = _macd_periods(params)
    period = _atr_period(params)
    symbols = sorted({c.symbol for c in candidates})
    start = (
        datetime.now(timezone.utc) - timedelta(days=_daily_lookback_days(params, want_atr))
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        bars_by_symbol = await client.historical_bars(symbols, strategy.asset_class, "1Day", start)
    except AlpacaError as exc:
        # Fail-safe: leave macd_bullish=None (evaluate_entry blocks rather than
        # trading blind) and atr_pct=None (atr_position_size falls back to the
        # fixed size) rather than trading on a missing signal.
        log.warning("daily signals for '%s' — daily bars fetch failed: %s", strategy.name, exc)
        return
    for cand in candidates:
        daily = _completed_daily_bars(bars_by_symbol.get(cand.symbol) or [], strategy.asset_class)
        if want_macd:
            cand.macd_bullish = stats.macd_bullish(
                [float(b["c"]) for b in daily], fast, slow, signal
            )
        if want_atr:
            cand.atr_pct = stats.atr_pct(daily, period, current_price=cand.price)
        if want_rsi:
            cand.rsi = stats.rsi(daily, RSI_PERIOD)


async def _daily_exit_signals(
    session: Session, client: AlpacaClient, open_trades: list[Trade]
) -> tuple[dict[int, bool | None], dict[int, float | None], dict[int, float | None]]:
    """For every open trade whose strategy opted into exit_on_macd_bearish and/or
    the ATR stop, return ({trade_id: macd_bullish}, {trade_id: atr_pct}). Computed
    once up front — mirroring _rotation_dropout_reasons — with one batched
    daily-bars call per opted-in strategy (its held symbols share an asset class
    and periods); the two signals share that single fetch. Strategies wanting
    neither are skipped entirely, so they cost nothing.

    The ATR% here uses the last completed daily close as the price denominator (no
    live quote at this point in the tick) — look-ahead-safe and recomputed every
    tick, so the resulting stop still breathes with the symbol's volatility."""
    grouped: dict[int, tuple[Strategy, dict, set[str]]] = {}
    for t in open_trades:
        if t.strategy_id in grouped:
            grouped[t.strategy_id][2].add(t.symbol)
            continue
        s = session.get(Strategy, t.strategy_id)
        if s is None:
            continue
        try:
            params = json.loads(s.params)
        except (TypeError, ValueError):
            params = {}
        want_macd = bool(params.get("exit", {}).get("exit_on_macd_bearish"))
        want_atr = _atr_stop_enabled(params)
        want_rsi = _exit_rsi_enabled(params)
        if not (want_macd or want_atr or want_rsi):
            continue
        grouped[t.strategy_id] = (s, params, {t.symbol})

    macd_flags: dict[int, bool | None] = {}
    atr_pcts: dict[int, float | None] = {}
    rsi_flags: dict[int, float | None] = {}
    for sid, (s, params, symbols) in grouped.items():
        fast, slow, signal = _macd_periods(params)
        period = _atr_period(params)
        want_macd = bool(params.get("exit", {}).get("exit_on_macd_bearish"))
        want_atr = _atr_stop_enabled(params)
        want_rsi = _exit_rsi_enabled(params)
        start = (
            datetime.now(timezone.utc) - timedelta(days=_daily_lookback_days(params, want_atr))
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            bars_by_symbol = await client.historical_bars(sorted(symbols), s.asset_class, "1Day", start)
        except AlpacaError as exc:
            # Can't tell → leave these trades at None; evaluate_exit won't force a
            # MACD exit on None, and the ATR stop falls back to the fixed stop, so
            # a fetch blip never sells (or un-stops) a position blindly.
            log.warning("daily exit signals for '%s' — daily bars fetch failed: %s", s.name, exc)
            continue
        for t in open_trades:
            if t.strategy_id != sid:
                continue
            daily = _completed_daily_bars(bars_by_symbol.get(t.symbol) or [], s.asset_class)
            if want_macd:
                macd_flags[t.id] = stats.macd_bullish(
                    [float(b["c"]) for b in daily], fast, slow, signal
                )
            if want_atr:
                atr_pcts[t.id] = stats.atr_pct(daily, period)
            if want_rsi:
                rsi_flags[t.id] = stats.rsi(daily, RSI_PERIOD)
    return macd_flags, atr_pcts, rsi_flags


async def tick(leverage_unlocked: bool = False) -> None:
    """One engine cycle: manage exits, then consider entries."""
    with session_scope() as session:
        mode = get_mode(session)
        if mode == "off":
            return
        client = get_client(session)
        if client is None:
            return

        try:
            account = await client.account()
            clock = await client.clock()
        except Exception as exc:
            log.warning("tick: broker unreachable: %s", exc)
            return

        equity = float(account.get("equity") or 0)
        # Remember the live account so new trades get tagged with it — even if the
        # keys were saved before per-account tagging existed. Write only on change.
        acct_num = account.get("account_number")
        if acct_num and get_setting(session, "current_account_id") != acct_num:
            set_setting(session, "current_account_id", acct_num)
        market_open = bool(clock.get("is_open"))
        closes_soon = False
        if market_open and clock.get("next_close"):
            next_close = datetime.fromisoformat(clock["next_close"].replace("Z", "+00:00"))
            closes_soon = (next_close - datetime.now(timezone.utc)) < timedelta(minutes=10)

        from qt.services import lifecycle, watchdog

        # Exits always run — closing positions during shutdown is desirable and
        # a mid-close order is never abandoned. New entries are skipped once a
        # shutdown has been requested so we don't open a position we can't mind.
        await _manage_exits(session, client, mode, market_open, closes_soon)
        if lifecycle.is_shutting_down():
            log.info("shutdown requested — skipping new entries this tick")
        else:
            await _consider_entries(
                session, client, mode, equity, market_open, leverage_unlocked
            )

        # Heartbeat: this tick completed the decision loop successfully.
        watchdog.record_heartbeat(session)


async def _rotation_dropout_reasons(
    session: Session, client: AlpacaClient, open_trades: list[Trade]
) -> dict[int, str]:
    """For rotation strategies (basket universe + exit.rotate_on_rank_dropout),
    return {trade_id: reason} for each open position whose symbol has fallen out
    of its strategy's current top-N ranking — the signal to rotate it out. A
    strategy whose ranking can't be computed right now is skipped so we never
    force a blind exit."""
    rotate: dict[int, Strategy] = {}
    seen: set[int] = set()
    for t in open_trades:
        if t.strategy_id in seen:
            continue
        seen.add(t.strategy_id)
        s = session.get(Strategy, t.strategy_id)
        # Rotation only makes sense for a RANKED universe (basket / watchlist /
        # custom with ranking on) — that's what defines a "top N" to fall out of.
        if s is None or not s.rank_enabled or s.universe not in ("basket", "watchlist", "custom"):
            continue
        try:
            exit_cfg = json.loads(s.params).get("exit", {})
        except (TypeError, ValueError):
            exit_cfg = {}
        if exit_cfg.get("rotate_on_rank_dropout"):
            rotate[s.id] = s

    reasons: dict[int, str] = {}
    for sid, s in rotate.items():
        try:
            top = await _ranked_symbols_now(session, client, s)
        except AlpacaError as exc:
            log.warning("rotation rank check for '%s' failed — holding positions: %s", s.name, exc)
            continue
        if not top:
            continue  # couldn't rank right now — don't rotate blindly
        for t in open_trades:
            if t.strategy_id == sid and t.symbol not in top:
                reasons[t.id] = f"rotated out of the top {s.top_n} by {s.rank_by.replace('_', ' ')}"
    return reasons


async def _manage_exits(
    session: Session, client: AlpacaClient, mode: str, market_open: bool, closes_soon: bool
) -> None:
    open_trades = (
        session.query(Trade).filter(Trade.mode == mode, Trade.status == "open").all()
    )
    if not open_trades:
        return
    try:
        quotes = await _quotes_for(client, open_trades)
    except AlpacaError as exc:
        log.warning("exit check: quote fetch failed: %s", exc)
        return

    now = datetime.now(timezone.utc)
    # Rotation strategies (a basket ranked by relative strength etc.) sell a
    # holding the moment it drops OUT of the current top-N — that IS the rotation.
    # Compute each such strategy's live top-N once, up front, and mark the
    # dropouts; the per-trade loop below turns them into exits (still respecting
    # market hours). A ranking we can't compute leaves holdings untouched.
    rotation_reason = await _rotation_dropout_reasons(session, client, open_trades)
    # Daily-MACD exit signal AND the ATR stop's current ATR%, both computed once up
    # front for the strategies that opted in (one shared batched daily-bars call
    # each). Empty dicts when nobody opted in.
    macd_exit_flags, atr_exit_pcts, rsi_exit_flags = await _daily_exit_signals(session, client, open_trades)

    for trade in open_trades:
        if trade.asset_class == "stock" and not market_open:
            continue  # can't exit a stock while the market is closed
        snap = quotes.get(trade.symbol) or {}
        price, vwap = _price_from_snapshot(snap)
        if price is None or trade.entry_price is None:
            continue

        if trade.high_water is None or price > trade.high_water:
            trade.high_water = price

        strategy = session.get(Strategy, trade.strategy_id)
        params = json.loads(strategy.params) if strategy else {}
        entry_at = trade.entry_at if trade.entry_at.tzinfo else trade.entry_at.replace(tzinfo=timezone.utc)
        if trade.id in rotation_reason:
            # Rank-dropout wins over the normal exit rules — rotate out now.
            should_exit, reason = True, rotation_reason[trade.id]
        else:
            should_exit, reason = evaluate_exit(
                params,
                strategy.swing_mode if strategy else True,
                trade.entry_price,
                entry_at,
                trade.high_water or price,
                price,
                vwap,
                now,
                # "flatten before close" is a stock concept — crypto has no close,
                # and closes_soon is derived from the stock clock. Never let it
                # flatten a 24/7 crypto position.
                closes_soon and trade.asset_class == "stock",
                macd_bullish=macd_exit_flags.get(trade.id),
                atr_pct=atr_exit_pcts.get(trade.id),
                rsi=rsi_exit_flags.get(trade.id),
            )
        if not should_exit:
            continue

        from qt.services import execution

        exit_cfg = params.get("exit", {})
        await execution.close_trade(
            session, client, trade, price, reason,
            slip_pct=exit_cfg.get("exit_slippage_pct", execution.EXIT_SLIP_PCT),
            slip_max_pct=exit_cfg.get("exit_slippage_max_pct"),
            market=execution.market_mode(params),
        )


# One-time log guard so the persistence-freeze warning doesn't repeat every tick.
_entries_frozen_logged = False

# Per-strategy decision trace from the most recent entry cycle — a debugging aid
# ("last run"): what each strategy looked at and why it did (or didn't) buy.
# In-memory (engine + API share one process); resets on restart. Keyed by
# strategy id. Never let building it affect trading (try/finally, no swallowing).
_last_run: dict[int, dict] = {}

_RANK_LABELS = {
    "momentum_today": "today's % move",
    "return_30d": "30-day return",
    "relative_strength": "relative strength (vs 200-day avg)",
    "rs_vs_spy": "relative strength vs the S&P 500",
    "rsi": "RSI (14-day)",
}


def get_last_run(strategy_id: int) -> dict | None:
    """The most recent entry-cycle decision trace for a strategy, or None if it
    hasn't been evaluated since startup."""
    return _last_run.get(strategy_id)


def _universe_summary(session: Session, strategy: Strategy) -> str:
    """Plain-English description of WHERE this strategy's candidates come from —
    the key to explaining why a name you expected wasn't even considered."""
    ranked = strategy.rank_enabled or strategy.universe == "basket"
    rank_txt = _RANK_LABELS.get(strategy.rank_by, strategy.rank_by)
    if strategy.universe == "basket":
        from qt.models import Basket

        b = session.get(Basket, strategy.basket_id) if strategy.basket_id else None
        name = f"“{b.name}”" if b else "basket"
        return (
            f"Top {strategy.top_n} of the {name} basket, ranked by {rank_txt} — "
            f"only these are evaluated; the other members aren't."
        )
    if strategy.universe == "custom":
        return (
            f"Top {strategy.top_n} of your symbol list, ranked by {rank_txt} (only these are evaluated)."
            if ranked else "Your symbol list (all of it is evaluated)."
        )
    if strategy.universe == "watchlist":
        return (
            f"Top {strategy.top_n} of your watchlist, ranked by {rank_txt} (only these are evaluated)."
            if ranked else "Your watchlist (all of it is evaluated)."
        )
    if strategy.universe == "both":
        return "Today's scanner movers plus your watchlist."
    return "Today's scanner movers (the day's top risers)."


async def _consider_entries(
    session: Session,
    client: AlpacaClient,
    mode: str,
    equity: float,
    market_open: bool,
    leverage_unlocked: bool,
) -> None:
    # Non-negotiable persistence guard: if we've CONFIDENTLY detected that /data
    # is ephemeral (the trade journal won't survive a restart), do NOT open new
    # positions. A lost journal blinds the "already open" rail, so the bot
    # re-buys positions the broker already holds — duplicate orders and orphans,
    # exactly the incident this guards against. Exits and reconciliation still
    # run; only new entries freeze. Boot already logged + Slack-alerted this, and
    # the dashboard shows a persistent red banner. Only `False` (confident)
    # freezes — an ambiguous `None` (local dev, no /proc) trades normally.
    global _entries_frozen_logged
    if persistence.boot_state().get("data_persistent") is False:
        if not _entries_frozen_logged:
            log.error(
                "NEW ENTRIES FROZEN — /data is not persistent, so the trade journal is "
                "ephemeral and re-buying already-held positions is likely. Fix the volume "
                "mapping (see the dashboard banner / docs/data-persistence.md). Exits still run."
            )
            _entries_frozen_logged = True
        return

    strategies = session.query(Strategy).filter(Strategy.enabled.is_(True)).all()
    if not strategies:
        return

    risk = get_risk(session)
    now_et = datetime.now(ET)
    today_start = _trading_day_start()  # 00:00 ET — shared by both daily counters
    daily_loss = _daily_loss(session, mode, today_start)

    regime_state: dict | None = None
    scan_result: dict | None = None

    for strategy in strategies:
        # A per-strategy decision trace built as we go (the "last run" debug view).
        # try/finally stores it no matter how the iteration ends; we never swallow
        # a trading error — it still propagates exactly as before.
        trace: dict = {
            "strategy_id": strategy.id,
            "strategy_name": strategy.name,
            "ran_at": datetime.now(timezone.utc).isoformat(),
            "mode": mode,
            "universe": "",
            "outcome": "",
            "candidates": [],
        }
        try:
            if strategy.asset_class == "stock" and not market_open:
                trace["outcome"] = "Market closed — this stock strategy doesn't trade now."
                continue

            params = json.loads(strategy.params)

            # DCA sleeve branch: a strategy whose params carry dca.interval_days > 0
            # is an always-on dollar-cost-averaging baseline. It buys a FIXED symbol
            # list on a fixed cadence regardless of momentum, so it takes its own
            # entry path (independent lots) and skips BOTH the momentum entry rules
            # and the regime gate — DCA buys through all market conditions by design.
            dca = params.get("dca") or {}
            if int(dca.get("interval_days", 0) or 0) > 0:
                trace["universe"] = "DCA sleeve"
                trace["outcome"] = "Dollar-cost-averaging sleeve — buys on its own schedule, not on these rules."
                await _consider_dca_entries(
                    session, client, mode, strategy, params, equity, risk,
                    leverage_unlocked, daily_loss, today_start,
                )
                continue

            # Regime gate (stocks only, exits unaffected)
            if strategy.asset_class == "stock" and not strategy.ignore_regime:
                if get_setting(session, "regime_filter_enabled") is not False:
                    if regime_state is None:
                        try:
                            regime_state = await regime.regime_status(client)
                        except Exception as exc:
                            regime_state = {"ok": False, "detail": f"regime check failed: {exc}"}
                    if not regime_state["ok"]:
                        trace["outcome"] = (
                            "Regime filter blocked stock entries: "
                            f"{regime_state.get('detail', 'S&P 500 below its 200-day average')}."
                        )
                        continue

            trace["universe"] = _universe_summary(session, strategy)
            candidates = await _candidates_for(session, client, strategy, scan_result)
            if candidates is None:
                trace["outcome"] = "Couldn't read candidates from the broker this cycle (will retry)."
                continue
            candidates, scan_result = candidates

            # Optional daily signals (MACD entry filter and/or ATR sizing): ONE batched
            # daily-bars call, then set each candidate's macd_bullish / atr_pct before
            # evaluate_entry and sizing read them.
            await _apply_daily_signals(client, strategy, params, candidates)

            if not candidates:
                trace["outcome"] = "No candidates in the universe this cycle."

            for cand in candidates:
                entry_ok, entry_reason = evaluate_entry(params, cand, now_et)
                row = {
                    "symbol": cand.symbol,
                    "price": cand.price,
                    "change_pct": cand.change_pct,
                    "macd_bullish": cand.macd_bullish,
                    "decision": "",
                    "reason": "",
                }
                if not entry_ok:
                    row["decision"], row["reason"] = "skipped", entry_reason
                    trace["candidates"].append(row)
                    continue

                # ATR sizing (opt-in): size so a stop-out loses ~risk_usd, capped at the
                # sleeve; falls back to the fixed sizing_usd when off or atr_pct is
                # unavailable. This size flows through the rails AND into open_trade.
                entry_sizing = atr_position_size(
                    params, strategy.sizing_usd, strategy.sleeve_usd, cand.atr_pct
                )

                ctx = _build_rail_context(
                    session, mode, strategy, cand.symbol, equity, risk,
                    leverage_unlocked, daily_loss, today_start,
                )
                rails_ok, rails_reason = check_rails(
                    {"max_positions": strategy.max_positions, "sleeve_usd": strategy.sleeve_usd},
                    entry_sizing,
                    ctx,
                )
                version_id = _latest_version_id(session, strategy.id)
                if not rails_ok:
                    row["decision"], row["reason"] = "blocked", f"{entry_reason}, but {rails_reason}"
                    trace["candidates"].append(row)
                    session.add(
                        Trade(
                            strategy_id=strategy.id,
                            config_version_id=version_id,
                            mode=mode, symbol=cand.symbol, asset_class=cand.asset_class,
                            account_id=get_setting(session, "current_account_id"),
                            qty=0, notional=0, status="rejected",
                            entry_reason=f"wanted to buy ({entry_reason}) but {rails_reason}",
                        )
                    )
                    continue

                row["decision"], row["reason"] = "bought", f"{entry_reason}; {rails_reason}"
                trace["candidates"].append(row)

                from qt.services import execution

                await execution.open_trade(
                    session, client, strategy, version_id, mode, cand,
                    f"{entry_reason}; {rails_reason}",
                    sizing_usd=entry_sizing,
                )

            if candidates and not trace["outcome"]:
                bought = sum(1 for c in trace["candidates"] if c["decision"] == "bought")
                trace["outcome"] = f"Evaluated {len(candidates)} candidate(s); bought {bought}."
        finally:
            _last_run[strategy.id] = trace


async def _consider_dca_entries(
    session: Session,
    client: AlpacaClient,
    mode: str,
    strategy: Strategy,
    params: dict,
    equity: float,
    risk: dict,
    leverage_unlocked: bool,
    daily_loss: float,
    today_start: datetime,
) -> None:
    """DCA (dollar-cost-averaging) sleeve: buy a FIXED symbol list on a fixed
    cadence, regardless of momentum — the dumb, steady baseline the momentum
    strategies must beat.

    Each scheduled buy is its own clean single-entry Trade — an independent LOT.
    There is NO averaged basis and NO multi-buy→one-sell: the engine's one-open-
    position-per-symbol model is preserved, we simply allow SEVERAL lots of the
    same symbol to coexist. To do that we bypass EXACTLY ONE rail, the
    "already open for this symbol" check; every other rail (sleeve budget,
    exposure ≤ equity, cross-strategy trade-rate limiter, daily-loss kill switch,
    cooldown, wash-sale) still runs through check_rails unchanged.

    Lots are buy-and-hold: they carry the strategy's exit rules like any other
    trade, so if the user leaves all exits off they simply accumulate.
    """
    interval_days = int(params["dca"]["interval_days"])
    symbols = json.loads(strategy.symbols) if strategy.symbols else []
    if not symbols:
        return

    # Snapshot the fixed list for prices. DCA does NOT run the momentum entry
    # rules — the price is only needed to size the lot.
    try:
        candidates = await _symbol_candidates(client, strategy.asset_class, symbols)
    except AlpacaError as exc:
        log.warning("DCA candidates for '%s' failed: %s", strategy.name, exc)
        return
    cand_by_symbol = {c.symbol: c for c in candidates}

    now_utc = datetime.now(timezone.utc)
    interval = timedelta(days=interval_days)
    version_id = _latest_version_id(session, strategy.id)

    for sym in symbols:
        cand = cand_by_symbol.get(sym)
        if cand is None:
            continue  # no price this cycle — can't size a lot; try again next tick

        # Cadence check: the most recent NON-rejected lot for THIS strategy+symbol.
        # entry_at (the buy instant) drives the schedule; a lot that has since
        # closed still counts as the last buy. None = never bought → due now.
        last_buy = (
            session.query(func.max(Trade.entry_at))
            .filter(
                Trade.mode == mode,
                Trade.strategy_id == strategy.id,
                Trade.symbol == sym,
                Trade.status != "rejected",
            )
            .scalar()
        )
        if last_buy is not None and last_buy.tzinfo is None:
            last_buy = last_buy.replace(tzinfo=timezone.utc)
        if last_buy is not None and (now_utc - last_buy) < interval:
            continue  # still inside the interval — not due yet

        ctx = _build_rail_context(
            session, mode, strategy, sym, equity, risk,
            leverage_unlocked, daily_loss, today_start,
        )
        # THE DCA bypass — the only rail DCA skips. Independent lots mean a fresh
        # buy is allowed even while a prior lot for this symbol is still open.
        # Neutralise ONLY this flag; check_rails then enforces everything else.
        ctx.already_open_symbol = False
        rails_ok, rails_reason = check_rails(
            {"max_positions": strategy.max_positions, "sleeve_usd": strategy.sleeve_usd},
            strategy.sizing_usd,
            ctx,
        )
        reason = f"DCA scheduled lot (every {interval_days}d)"
        if not rails_ok:
            session.add(
                Trade(
                    strategy_id=strategy.id, config_version_id=version_id,
                    mode=mode, symbol=sym, asset_class=cand.asset_class,
                    account_id=get_setting(session, "current_account_id"),
                    qty=0, notional=0, status="rejected",
                    entry_reason=f"wanted to buy ({reason}) but {rails_reason}",
                )
            )
            continue

        from qt.services import execution

        await execution.open_trade(
            session, client, strategy, version_id, mode, cand,
            f"{reason}; {rails_reason}",
        )


async def _candidates_for(
    session: Session, client: AlpacaClient, strategy: Strategy, scan_result: dict | None
):
    """Collect candidates from the strategy's universe. Returns (list, scan_result)."""
    if strategy.universe == "basket":
        try:
            return await _basket_candidates(session, client, strategy), scan_result
        except AlpacaError as exc:
            log.warning("basket candidates for '%s' failed: %s", strategy.name, exc)
            return None

    if strategy.universe == "custom":
        syms = json.loads(strategy.symbols) if strategy.symbols else []
        if not syms:
            return [], scan_result
        try:
            # Opt-in ranking: trade only the top-N of the custom list by the chosen
            # metric; otherwise consider them all (entry rules decide).
            if strategy.rank_enabled:
                return await _ranked_candidates(
                    client, strategy.asset_class, syms, strategy.rank_by, strategy.top_n
                ), scan_result
            return await _symbol_candidates(client, strategy.asset_class, syms), scan_result
        except AlpacaError as exc:
            log.warning("custom candidates for '%s' failed: %s", strategy.name, exc)
            return None

    # A watchlist universe with ranking on: rank the whole watchlist and keep the
    # top-N. ("both" never ranks — rank_enabled is forced off in the API — because
    # its scanner half is already ranked.)
    if strategy.universe == "watchlist" and strategy.rank_enabled:
        from qt.models import WatchlistItem

        syms = [
            i.symbol
            for i in session.query(WatchlistItem).filter(WatchlistItem.asset_class == strategy.asset_class).all()
        ]
        if not syms:
            return [], scan_result
        try:
            return await _ranked_candidates(
                client, strategy.asset_class, syms, strategy.rank_by, strategy.top_n
            ), scan_result
        except AlpacaError as exc:
            log.warning("watchlist ranking for '%s' failed: %s", strategy.name, exc)
            return None

    candidates: list[Candidate] = []
    try:
        if strategy.universe in ("scanner", "both"):
            if scan_result is None:
                scan_result = await scanner.scan(session, client)
            rows = scan_result["stocks"] if strategy.asset_class == "stock" else scan_result["crypto"]
            symbols = [r["symbol"] for r in rows]
            snaps = (
                await client.stock_snapshots(symbols)
                if strategy.asset_class == "stock"
                else await client.crypto_snapshots(symbols)
            )
            for row in rows:
                price, vwap = _price_from_snapshot(snaps.get(row["symbol"]) or {})
                candidates.append(
                    Candidate(
                        symbol=row["symbol"], asset_class=row["asset_class"],
                        price=price or row["price"], change_pct=row["change_pct"], vwap=vwap,
                    )
                )
        if strategy.universe in ("watchlist", "both"):
            from qt.models import WatchlistItem

            items = (
                session.query(WatchlistItem)
                .filter(WatchlistItem.asset_class == strategy.asset_class)
                .all()
            )
            symbols = [i.symbol for i in items if i.symbol not in {c.symbol for c in candidates}]
            if symbols:
                snaps = (
                    await client.stock_snapshots(symbols)
                    if strategy.asset_class == "stock"
                    else await client.crypto_snapshots(symbols)
                )
                for sym in symbols:
                    snap = snaps.get(sym) or {}
                    price, vwap = _price_from_snapshot(snap)
                    daily = (snap.get("dailyBar") or {}).get("c")
                    prev = (snap.get("prevDailyBar") or {}).get("c")
                    change = ((daily / prev - 1) * 100) if daily and prev else 0.0
                    if price:
                        candidates.append(
                            Candidate(
                                symbol=sym, asset_class=strategy.asset_class,
                                price=price, change_pct=round(change, 2), vwap=vwap,
                            )
                        )
    except AlpacaError as exc:
        log.warning("candidates for '%s' failed: %s", strategy.name, exc)
        return None
    return candidates, scan_result


async def _symbol_candidates(
    client: AlpacaClient, asset_class: str, symbols: list[str]
) -> list[Candidate]:
    """Build candidates from a hand-picked symbol list (universe="custom").
    Snapshots each symbol; the strategy's entry rules still filter them — this
    is the candidate set, not an auto-buy list."""
    is_stock = asset_class == "stock"
    snaps = await (client.stock_snapshots(symbols) if is_stock else client.crypto_snapshots(symbols))
    out: list[Candidate] = []
    for sym in symbols:
        snap = snaps.get(sym) or {}
        price, vwap = _price_from_snapshot(snap)
        daily = (snap.get("dailyBar") or {}).get("c")
        prev = (snap.get("prevDailyBar") or {}).get("c")
        change = ((daily / prev - 1) * 100) if daily and prev else 0.0
        if price:
            out.append(
                Candidate(
                    symbol=sym, asset_class=asset_class,
                    price=price, change_pct=round(change, 2), vwap=vwap,
                )
            )
    return out


async def _pool_metrics(
    client: AlpacaClient, asset_class: str, symbols: list[str], rank_by: str
) -> tuple[dict, dict, dict]:
    """Snapshot a pool and compute the ranking metric for each symbol. Returns
    (metrics, price_map, vwap_map). Shared by live candidate selection and the
    'current ranking' view so both rank identically. Only fetches the daily bars
    a bar-based metric needs; momentum_today rides on the snapshot alone."""
    from qt.services import stats

    price_map: dict[str, float] = {}
    vwap_map: dict[str, float | None] = {}
    metrics: dict[str, dict[str, float | None]] = {}
    if not symbols:
        return metrics, price_map, vwap_map

    is_stock = asset_class == "stock"
    snaps = await (client.stock_snapshots(symbols) if is_stock else client.crypto_snapshots(symbols))
    for sym in symbols:
        snap = snaps.get(sym) or {}
        price, vwap = _price_from_snapshot(snap)
        daily = (snap.get("dailyBar") or {}).get("c")
        prev = (snap.get("prevDailyBar") or {}).get("c")
        change = ((daily / prev - 1) * 100) if daily and prev else None
        if price:
            price_map[sym] = price
        vwap_map[sym] = vwap
        metrics[sym] = {
            "momentum_today": round(change, 2) if change is not None else None,
            "return_30d": None,
            "relative_strength": None,
            "rs_vs_spy": None,
            "rsi": None,
        }

    # rs_vs_spy is a STOCK-only metric (SPY is the benchmark); never fetch SPY for crypto.
    RS_VS_SPY_WINDOW = 90  # calendar days of relative return
    want_rs_vs_spy = rank_by == "rs_vs_spy" and asset_class == "stock"

    if rank_by in ("return_30d", "relative_strength", "rsi") or want_rs_vs_spy:
        lookback_days = 320 if rank_by in ("relative_strength", "rs_vs_spy") else 60
        start = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        fetch_symbols = symbols + ["SPY"] if want_rs_vs_spy else symbols
        bars_by_symbol = await client.historical_bars(fetch_symbols, asset_class, "1Day", start)
        for sym in symbols:
            bars = bars_by_symbol.get(sym) or []
            cur = price_map.get(sym)
            metrics[sym]["return_30d"] = stats.pct_change_over(bars, 30, cur)
            metrics[sym]["relative_strength"] = stats.vs_sma_pct(bars, 200, cur)
            metrics[sym]["rsi"] = stats.rsi(bars, RSI_PERIOD, cur)
        if want_rs_vs_spy:
            spy_bars = bars_by_symbol.get("SPY") or []
            spy_last = float(spy_bars[-1]["c"]) if spy_bars else None
            spy_return = stats.pct_change_over(spy_bars, RS_VS_SPY_WINDOW, spy_last)
            if spy_return is not None:
                for sym in symbols:
                    member_return = stats.pct_change_over(
                        bars_by_symbol.get(sym) or [], RS_VS_SPY_WINDOW, price_map.get(sym)
                    )
                    if member_return is not None:
                        metrics[sym]["rs_vs_spy"] = round(member_return - spy_return, 2)

    return metrics, price_map, vwap_map


async def _ranked_candidates(
    client: AlpacaClient, asset_class: str, symbols: list[str], rank_by: str, top_n: int
) -> list[Candidate]:
    """Snapshot a POOL of symbols, score each by `rank_by`, and return the top-N
    as candidates. Universe-agnostic: the pool can be a basket's members, a
    watchlist, or a hand-picked custom list. The entry rules still filter the
    result; top-N is the candidate set, not an auto-buy list."""
    from qt.services import ranking

    symbols = sorted(set(symbols))
    if not symbols:
        return []
    metrics, price_map, vwap_map = await _pool_metrics(client, asset_class, symbols, rank_by)
    candidates: list[Candidate] = []
    for sym, _value in ranking.rank_symbols(metrics, rank_by, top_n):
        price = price_map.get(sym)
        if not price:
            continue
        candidates.append(
            Candidate(
                symbol=sym,
                asset_class=asset_class,
                price=price,
                change_pct=metrics[sym]["momentum_today"] or 0.0,
                vwap=vwap_map.get(sym),
            )
        )
    return candidates


def _basket_symbols(session: Session, strategy: Strategy) -> list[str]:
    """The strategy's basket members for its asset class (deduped, sorted)."""
    from qt.models import BasketItem

    if strategy.basket_id is None:
        return []
    items = (
        session.query(BasketItem)
        .filter(
            BasketItem.basket_id == strategy.basket_id,
            BasketItem.asset_class == strategy.asset_class,
        )
        .all()
    )
    return sorted({i.symbol for i in items})


async def _basket_candidates(
    session: Session, client: AlpacaClient, strategy: Strategy
) -> list[Candidate]:
    """A basket universe: rank its members by the chosen metric and take the
    top-N (a basket is always ranked)."""
    return await _ranked_candidates(
        client, strategy.asset_class, _basket_symbols(session, strategy),
        strategy.rank_by, strategy.top_n,
    )


def _strategy_pool(session: Session, strategy: Strategy) -> list[str]:
    """The full symbol pool a ranked strategy ranks over: basket members, the
    watchlist for its asset class, or its custom list. Empty for scanner/both
    (those aren't fixed pools — the scanner ranks by movement itself)."""
    if strategy.universe == "basket":
        return _basket_symbols(session, strategy)
    if strategy.universe == "custom":
        return sorted(
            {s.strip().upper() for s in (json.loads(strategy.symbols) if strategy.symbols else []) if s.strip()}
        )
    if strategy.universe == "watchlist":
        from qt.models import WatchlistItem

        return [
            i.symbol
            for i in session.query(WatchlistItem).filter(WatchlistItem.asset_class == strategy.asset_class).all()
        ]
    return []


async def _ranked_symbols_now(session: Session, client: AlpacaClient, s: Strategy) -> set[str]:
    """The set of symbols currently in a ranked strategy's top-N — its live pool
    ranked and cut to top_n. Used by the rotation check to see which open
    positions have fallen out. Empty set for a non-ranked universe."""
    pool = _strategy_pool(session, s)
    if not pool:
        return set()
    cands = await _ranked_candidates(client, s.asset_class, pool, s.rank_by, s.top_n)
    return {c.symbol for c in cands}


async def rank_pool(session: Session, client: AlpacaClient, strategy: Strategy) -> list[dict]:
    """The FULL live ranking of a ranked strategy's pool — every symbol with its
    metric value, rank, and whether it makes the top-N cut. Symbols the metric
    can't be computed for (no data) are listed last as unranked. This is what the
    'current ranking' view shows so you can see why a name is (or isn't) eligible."""
    from qt.services import ranking

    pool = sorted(set(_strategy_pool(session, strategy)))
    if not pool:
        return []
    metrics, price_map, _vwap = await _pool_metrics(client, strategy.asset_class, pool, strategy.rank_by)
    rows: list[dict] = []
    ranked_syms: set[str] = set()
    for i, (sym, value) in enumerate(ranking.rank_symbols(metrics, strategy.rank_by, len(pool)), start=1):
        ranked_syms.add(sym)
        rows.append({
            "symbol": sym,
            "rank": i,
            "value": round(value, 2),
            "in_top_n": i <= strategy.top_n,
            "price": price_map.get(sym),
            "change_pct": metrics[sym]["momentum_today"],
        })
    for sym in pool:  # couldn't rank (metric is None) — surface as unranked, last
        if sym not in ranked_syms:
            rows.append({
                "symbol": sym, "rank": None, "value": None, "in_top_n": False,
                "price": price_map.get(sym), "change_pct": metrics.get(sym, {}).get("momentum_today"),
            })
    return rows


def _build_rail_context(
    session: Session, mode: str, strategy: Strategy, symbol: str, equity: float,
    risk: dict, leverage_unlocked: bool, daily_loss: float, today_start: datetime,
) -> RailContext:
    open_q = session.query(Trade).filter(Trade.mode == mode, Trade.status == "open")
    open_total = open_q.count()
    open_exposure = sum(t.notional for t in open_q.all())
    strat_open = open_q.filter(Trade.strategy_id == strategy.id)
    open_strategy = strat_open.count()
    exposure_strategy = sum(t.notional for t in strat_open.all())
    entries_today = (
        session.query(func.count(Trade.id))
        .filter(Trade.mode == mode, Trade.entry_at >= today_start, Trade.status != "rejected")
        .scalar()
    )
    already_open = (
        open_q.filter(Trade.symbol == symbol).count() > 0
    )
    last_loss = (
        session.query(func.max(Trade.exit_at))
        .filter(Trade.mode == mode, Trade.symbol == symbol, Trade.status == "closed", Trade.pnl < 0)
        .scalar()
    )
    if last_loss is not None and last_loss.tzinfo is None:
        last_loss = last_loss.replace(tzinfo=timezone.utc)
    wash = False
    if strategy.asset_class == "stock":
        cutoff = datetime.now(timezone.utc) - timedelta(days=31)
        wash = (
            session.query(func.count(Trade.id))
            .filter(
                Trade.symbol == symbol, Trade.asset_class == "stock",
                Trade.status == "closed", Trade.pnl < 0, Trade.exit_at >= cutoff,
            )
            .scalar()
            > 0
        )
    return RailContext(
        equity=equity,
        open_positions_total=open_total,
        open_exposure_usd=open_exposure,
        open_positions_strategy=open_strategy,
        open_exposure_strategy_usd=exposure_strategy,
        entries_today=int(entries_today or 0),
        already_open_symbol=already_open,
        last_loss_at=last_loss,
        loss_sale_within_31d=wash,
        risk=risk,
        leverage_unlocked=leverage_unlocked,
        daily_loss_usd=daily_loss,
    )


def _latest_version_id(session: Session, strategy_id: int) -> int | None:
    row = (
        session.query(StrategyConfigVersion.id)
        .filter(StrategyConfigVersion.strategy_id == strategy_id)
        .order_by(StrategyConfigVersion.version_no.desc())
        .first()
    )
    return row[0] if row else None
