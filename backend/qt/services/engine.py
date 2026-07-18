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
from qt.services import notify, regime, scanner
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
    return True, f"up {candidate.change_pct:.2f}% today" + (
        f", above VWAP" if entry.get("require_above_vwap") else ""
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
) -> tuple[bool, str]:
    """Return (should_exit, reason). Stops always apply; in swing mode the
    softer exits (take-profit, VWAP, time) wait until the day after entry."""
    exit_rules = params.get("exit", {})
    change_from_entry = (price / entry_price - 1) * 100
    drop_from_high = (1 - price / high_water) * 100 if high_water else 0.0

    stop_loss = exit_rules.get("stop_loss_pct", 0)
    if stop_loss and change_from_entry <= -stop_loss:
        return True, f"stop-loss: {change_from_entry:.2f}% ≤ -{stop_loss}%"

    trailing = exit_rules.get("trailing_stop_pct", 0)
    if trailing and drop_from_high >= trailing and price > entry_price * (1 - stop_loss / 100):
        return True, f"trailing stop: {drop_from_high:.2f}% off high {high_water:.4f}"

    same_day = entry_at.astimezone(ET).date() == now_utc.astimezone(ET).date()
    if swing_mode and same_day:
        return False, ""  # stops above already had their chance; be patient today

    take_profit = exit_rules.get("take_profit_pct", 0)
    if take_profit and change_from_entry >= take_profit:
        return True, f"take-profit: +{change_from_entry:.2f}% ≥ {take_profit}%"

    if exit_rules.get("exit_below_vwap") and vwap is not None and price < vwap:
        return True, f"price {price:.4f} fell below VWAP {vwap:.4f}"

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


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _daily_loss(session: Session, mode: str, equity: float) -> float:
    """Realized loss today (positive number = loss) from closed trades."""
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    realized = (
        session.query(func.coalesce(func.sum(Trade.pnl), 0.0))
        .filter(Trade.mode == mode, Trade.status == "closed", Trade.exit_at >= today)
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
        should_exit, reason = evaluate_exit(
            params,
            strategy.swing_mode if strategy else True,
            trade.entry_price,
            entry_at,
            trade.high_water or price,
            price,
            vwap,
            now,
            closes_soon,
        )
        if not should_exit:
            continue

        from qt.services import execution

        await execution.close_trade(session, client, trade, price, reason)


async def _consider_entries(
    session: Session,
    client: AlpacaClient,
    mode: str,
    equity: float,
    market_open: bool,
    leverage_unlocked: bool,
) -> None:
    strategies = session.query(Strategy).filter(Strategy.enabled.is_(True)).all()
    if not strategies:
        return

    risk = get_risk(session)
    daily_loss = _daily_loss(session, mode, equity)
    now_et = datetime.now(ET)
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

    regime_state: dict | None = None
    scan_result: dict | None = None

    for strategy in strategies:
        if strategy.asset_class == "stock" and not market_open:
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
                    continue

        candidates = await _candidates_for(session, client, strategy, scan_result)
        if candidates is None:
            continue
        candidates, scan_result = candidates

        params = json.loads(strategy.params)
        for cand in candidates:
            entry_ok, entry_reason = evaluate_entry(params, cand, now_et)
            if not entry_ok:
                continue

            ctx = _build_rail_context(
                session, mode, strategy, cand.symbol, equity, risk,
                leverage_unlocked, daily_loss, today_start,
            )
            rails_ok, rails_reason = check_rails(
                {"max_positions": strategy.max_positions, "sleeve_usd": strategy.sleeve_usd},
                strategy.sizing_usd,
                ctx,
            )
            version_id = _latest_version_id(session, strategy.id)
            if not rails_ok:
                session.add(
                    Trade(
                        strategy_id=strategy.id,
                        config_version_id=version_id,
                        mode=mode, symbol=cand.symbol, asset_class=cand.asset_class,
                        qty=0, notional=0, status="rejected",
                        entry_reason=f"wanted to buy ({entry_reason}) but {rails_reason}",
                    )
                )
                continue

            from qt.services import execution

            await execution.open_trade(
                session, client, strategy, version_id, mode, cand,
                f"{entry_reason}; {rails_reason}",
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
            return await _symbol_candidates(client, strategy.asset_class, syms), scan_result
        except AlpacaError as exc:
            log.warning("custom candidates for '%s' failed: %s", strategy.name, exc)
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


async def _basket_candidates(
    session: Session, client: AlpacaClient, strategy: Strategy
) -> list[Candidate]:
    """Load the strategy's basket, snapshot its members, rank by the chosen
    metric and return the top-N as candidates. The entry rules still filter
    these afterward — top-N is the candidate set, not an auto-buy list."""
    from qt.models import BasketItem
    from qt.services import ranking, stats

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
    symbols = sorted({i.symbol for i in items})
    if not symbols:
        return []

    is_stock = strategy.asset_class == "stock"
    snaps = await (client.stock_snapshots(symbols) if is_stock else client.crypto_snapshots(symbols))

    price_map: dict[str, float] = {}
    vwap_map: dict[str, float | None] = {}
    metrics: dict[str, dict[str, float | None]] = {}
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
        }

    # Bar-based metrics need daily history. Only fetch when the ranking asks for
    # it — momentum_today rides on the snapshot alone.
    if strategy.rank_by in ("return_30d", "relative_strength"):
        lookback_days = 320 if strategy.rank_by == "relative_strength" else 60
        start = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        bars_by_symbol = await client.historical_bars(symbols, strategy.asset_class, "1Day", start)
        for sym in symbols:
            bars = bars_by_symbol.get(sym) or []
            cur = price_map.get(sym)
            metrics[sym]["return_30d"] = stats.pct_change_over(bars, 30, cur)
            metrics[sym]["relative_strength"] = stats.vs_sma_pct(bars, 200, cur)

    ranked = ranking.rank_symbols(metrics, strategy.rank_by, strategy.top_n)
    candidates: list[Candidate] = []
    for sym, _value in ranked:
        price = price_map.get(sym)
        if not price:
            continue
        candidates.append(
            Candidate(
                symbol=sym,
                asset_class=strategy.asset_class,
                price=price,
                change_pct=metrics[sym]["momentum_today"] or 0.0,
                vwap=vwap_map.get(sym),
            )
        )
    return candidates


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
