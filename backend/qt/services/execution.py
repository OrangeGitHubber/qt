"""Trade execution: shadow (journal only) and paper (real Alpaca paper
orders, marketable limit only, idempotent client order IDs)."""

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from qt.broker.alpaca import AlpacaClient, AlpacaError
from qt.models import AuditLog, Strategy, Trade
from qt.services import notify
from qt.services.engine import Candidate

log = logging.getLogger("qt.execution")

ENTRY_SLIP_PCT = 0.5  # default: buy limit 0.5% through the price = "marketable"
EXIT_SLIP_PCT = 1.0  # default: sell limit 1% through = exits must fill
FILL_POLL_SECONDS = (1, 2, 3)  # ~6s total

# Broker statuses that mean the order is SETTLED: it will never fill again, and
# it can no longer be cancelled. Whatever filled_qty it carries is final.
DEAD_STATUSES = ("canceled", "expired", "rejected")

# Alpaca will not accept an order worth less than about a dollar. An exit below
# that can NEVER be accepted, so submitting it is not a retry, it is a loop:
# close_trade is called from every 60-second tick for as long as the trade is
# open, and there was no branch that ever stopped. Dust is how a position gets
# there — a part-filled IOC entry, or the crypto fee taken in coin.
MIN_ORDER_NOTIONAL_USD = 1.0

# After this many consecutive failed exits, say so out loud — once. We keep
# retrying (liquidity comes back, and abandoning a real position is worse), but
# a position that cannot be sold is exactly the thing the user must hear about
# rather than find later in a log.
EXIT_ALERT_AFTER_ATTEMPTS = 5

# trade.id -> consecutive failed exit attempts, for the escalating exit chase.
# In-memory: resets on restart (escalation simply restarts from the base buffer).
_exit_attempts: dict[int, int] = {}


def _escalated_exit_pct(base: float, cap: float | None, attempts: int) -> float:
    """The exit marketable buffer for this attempt: start at `base`, widen by one
    base step per prior miss, capped at `cap`. cap<=base means no escalation."""
    base = max(0.0, base)
    cap = max(base, cap if cap is not None else base)
    return min(cap, base + attempts * base)


def _entry_slip_pct(strategy: Strategy) -> float:
    try:
        return float(json.loads(strategy.params).get("entry", {}).get("entry_slippage_pct", ENTRY_SLIP_PCT))
    except Exception:  # noqa: BLE001 — bad/old params: fall back to the default
        return ENTRY_SLIP_PCT


def market_mode(params: dict) -> bool:
    """Whether this strategy opted into MARKET orders + fractional sizing (off by
    default). When on, entries buy a dollar NOTIONAL at market — so a small
    $-per-trade can take a fractional slice of an expensive name — and exits sell
    at market. When off, the default price-protected marketable-limit path runs."""
    try:
        return bool(params.get("execution", {}).get("market_orders", False))
    except Exception:  # noqa: BLE001 — bad/old params: default to the safe path
        return False


def _entry_tif(asset_class: str, is_market: bool) -> str:
    """Time-in-force for an ENTRY order.

    A crypto MARKET entry goes IOC (immediate-or-cancel): take whatever is
    resting on the book right now, and let the venue kill the rest instantly.
    A crypto GTC market order does the opposite — it stays accepted and working
    after we've stopped watching, which is how RENDER/USD sat at "new" for
    minutes while every 60s cycle placed another one. There is nothing to gain
    from a market order that lingers: if it can't be bought at once, we'd rather
    be told so and re-decide on the next candidate.

    Everything else keeps the time-in-force it already had. A crypto marketable
    LIMIT has to be allowed to rest — that's the point of it — so it stays GTC,
    and stocks stay `day`.
    """
    if asset_class != "crypto":
        return "day"
    return "ioc" if is_market else "gtc"


def _round_price(price: float) -> float:
    # US equities disallow sub-penny limits at >=$1; crypto is fine with more.
    return round(price, 2) if price >= 1 else round(price, 6)


def _qty_for(asset_class: str, sizing_usd: float, price: float, fractional: bool = False) -> float:
    if asset_class == "stock" and not fractional:
        return float(int(sizing_usd // price))  # whole shares
    return round(sizing_usd / price, 6)


async def _await_fill(client: AlpacaClient, order_id: str) -> tuple[dict | None, dict | None]:
    """Returns (filled_order, last_seen_order).

    The second element is what the order looked like at the moment we gave up,
    and it has to be captured HERE: the caller's next move is to cancel, which
    overwrites the broker status with "canceled" and destroys the only evidence
    of why the order didn't fill. Resting unfilled, expired, and rejected-after-
    acceptance are three different problems that look identical afterwards."""
    order = None
    for delay in FILL_POLL_SECONDS:
        await asyncio.sleep(delay)
        order = await client.get_order(order_id)
        if order.get("status") == "filled":
            return order, order
        if order.get("status") in DEAD_STATUSES:
            return None, order
    return None, order


async def _broker_held_qty(client: AlpacaClient, symbol: str) -> float | None:
    """How much of `symbol` the broker actually holds right now, or None if we
    could not ask (never a guess — the caller must fall back to the journal).

    Needed because for CRYPTO the journal is knowingly a little above the broker.
    Alpaca charges crypto commission IN THE COIN it delivers, so the position that
    lands is ~0.25% smaller than the order's own filled_qty — and filled_qty is
    what open_trade journals. reconcile.adjust_qty_fee_in_kind squares the two,
    but it runs every 15 MINUTES while exits run every 60 SECONDS, so for up to
    fifteen minutes after any crypto entry the exit asks to sell coins the account
    does not have. Alpaca rejects the order, the trade stays open, and the tick
    tries again a minute later: a stop-loss that cannot fire, on a falling coin,
    for a quarter of an hour.
    """
    from qt.services.reconcile import _norm

    try:
        positions = await client.list_positions()
    except Exception as exc:  # noqa: BLE001 — AlpacaError or network; fall back
        log.warning("could not read broker positions before exiting %s: %s", symbol, exc)
        return None
    key = _norm(symbol)
    for pos in positions:
        if _norm(pos.get("symbol", "")) == key:
            try:
                return abs(float(pos.get("qty") or 0))
            except (TypeError, ValueError):
                return None
    return 0.0  # the broker holds none of it


async def open_trade(
    session: Session,
    client: AlpacaClient,
    strategy: Strategy,
    version_id: int | None,
    mode: str,
    cand: Candidate,
    reason: str,
    sizing_usd: float | None = None,
) -> Trade | None:
    # `sizing_usd` lets the caller override the strategy's fixed $ per trade — the
    # engine passes the ATR-derived size when ATR sizing is on. None = the fixed
    # strategy.sizing_usd, so existing callers are unaffected.
    from qt.settings_service import get_setting

    account_id = get_setting(session, "current_account_id")  # tag the trade's account
    effective_sizing = strategy.sizing_usd if sizing_usd is None else sizing_usd
    is_market = market_mode(json.loads(strategy.params))
    qty = _qty_for(cand.asset_class, effective_sizing, cand.price, fractional=is_market)

    # Whole-share limit mode can't buy an expensive name with a small budget and
    # rejects here; market+fractional mode buys a dollar slice instead (bounded
    # only by Alpaca's $1 notional minimum).
    if is_market and effective_sizing < 1:
        session.add(
            Trade(
                strategy_id=strategy.id, config_version_id=version_id, mode=mode,
                account_id=account_id,
                symbol=cand.symbol, asset_class=cand.asset_class, qty=0, notional=0,
                status="rejected",
                entry_reason=f"wanted to buy ({reason}) but ${effective_sizing:,.2f} is below "
                "Alpaca's $1 minimum for a market order",
            )
        )
        return None
    if qty <= 0 and not is_market:
        session.add(
            Trade(
                strategy_id=strategy.id, config_version_id=version_id, mode=mode,
                account_id=account_id,
                symbol=cand.symbol, asset_class=cand.asset_class, qty=0, notional=0,
                status="rejected",
                entry_reason=f"wanted to buy ({reason}) but position too small: "
                f"${effective_sizing:,.0f} buys 0 whole shares at ${cand.price:,.2f} "
                "— turn on market + fractional trading to buy a slice",
            )
        )
        return None

    now = datetime.now(timezone.utc)
    trade = Trade(
        strategy_id=strategy.id, config_version_id=version_id, mode=mode,
        account_id=account_id,
        symbol=cand.symbol, asset_class=cand.asset_class, qty=qty,
        notional=qty * cand.price, status="open", entry_reason=reason,
        entry_price=cand.price, entry_at=now, high_water=cand.price,
    )

    if mode == "paper":
        client_order_id = f"qt-{uuid.uuid4().hex[:20]}"
        tif = _entry_tif(cand.asset_class, is_market)
        try:
            if is_market:
                # Buy a dollar notional at market — fills fast (so it doesn't
                # orphan) and lets a small budget take a fractional slice.
                order = await client.submit_market_order(
                    cand.symbol, "buy", client_order_id,
                    notional=round(effective_sizing, 2), time_in_force=tif,
                )
            else:
                limit = _round_price(cand.price * (1 + _entry_slip_pct(strategy) / 100))
                order = await client.submit_order(
                    cand.symbol, qty, "buy", limit, client_order_id, time_in_force=tif,
                )
        except AlpacaError as exc:
            trade.status = "rejected"
            trade.entry_reason = f"wanted to buy ({reason}) but order rejected: {exc}"
            session.add(trade)
            return None
        filled, last_seen = await _await_fill(client, order["id"])
        if not filled:
            settled = last_seen or {}
            final = None
            # A DEAD order is finished: an IOC remainder the venue killed, an
            # expiry, a rejection. It cannot fill later and cannot be cancelled,
            # so there is nothing to pull and no race to re-check. Anything else
            # is still working, and that is the case that must be pulled.
            if settled.get("status") not in DEAD_STATUSES:
                try:
                    await client.cancel_order(order["id"])
                except AlpacaError:
                    pass
                # A cancel can race a late fill (a resting order fills
                # asynchronously): re-check once and adopt whatever actually
                # filled, so QT's journal matches Alpaca instead of orphaning a
                # real position that then never shows up in the strategy's
                # holdings.
                try:
                    final = await client.get_order(order["id"])
                except AlpacaError:
                    final = None
                if final and float(final.get("filled_qty") or 0) > 0:
                    settled = final
            if float(settled.get("filled_qty") or 0) > 0:
                # Something did fill. An IOC market entry routinely comes back
                # part-filled — it took what was on the book and the venue
                # cancelled the rest — and that part is a position we really own.
                # Adopt it; the journalling below uses the filled quantity, not
                # the quantity we asked for.
                filled = settled
            else:
                # Name what the broker actually said. "did not fill" on its own
                # covers four different failures — still working, cancelled,
                # expired, rejected after acceptance — and RENDER/USD burned a
                # session precisely because the row couldn't tell them apart.
                # last_seen first: `final` is post-cancel and always says
                # "canceled", which is our own doing, not the reason.
                observed = last_seen or final or {}
                status = observed.get("status") or "unknown"
                partial = float(observed.get("filled_qty") or 0)
                trade.status = "rejected"
                trade.entry_reason = (
                    f"wanted to buy ({reason}) but {'market' if is_market else 'limit'} order "
                    f"did not fill in {sum(FILL_POLL_SECONDS)}s "
                    f"(broker status: {status}, filled {partial:g} of {qty:g})"
                )
                session.add(trade)
                return None
        trade.entry_order_id = order["id"]
        trade.entry_price = float(filled.get("filled_avg_price") or cand.price)
        trade.qty = float(filled.get("filled_qty") or qty)
        trade.notional = trade.entry_price * trade.qty
        trade.high_water = trade.entry_price

    session.add(trade)
    session.add(
        AuditLog(
            category="trade",
            message=f"[{mode}] BUY {trade.qty:g} {cand.symbol} @ ~${trade.entry_price:,.4f}",
            detail=reason,
        )
    )
    await notify.slack_cat(
        session,
        "trade_confirmations",
        f":large_green_circle: *{mode.upper()}* bought {trade.qty:g} × *{cand.symbol}* "
        f"@ ${trade.entry_price:,.4f} · reason: {reason} · strategy: {strategy.name}",
    )
    return trade


async def _alert_stuck_exit(session: Session, trade: Trade, attempts: int) -> None:
    """Say once, out loud, that a position is not getting out. Fires on the
    crossing only, so a chronically stuck trade doesn't become its own flood."""
    if attempts != EXIT_ALERT_AFTER_ATTEMPTS:
        return
    await notify.slack_cat(
        session,
        "reconciliation",
        f":warning: *PAPER* has failed to exit *{trade.symbol}* {attempts} times in a row "
        f"({trade.qty:g} units). Still retrying — check whether the order can fill at all.",
    )


async def close_trade(
    session: Session,
    client: AlpacaClient,
    trade: Trade,
    price: float,
    reason: str,
    *,
    slip_pct: float = EXIT_SLIP_PCT,
    slip_max_pct: float | None = None,
    market: bool = False,
) -> bool:
    exit_price = price

    if trade.mode == "paper":
        client_order_id = f"qt-x-{uuid.uuid4().hex[:18]}"
        # Exits deliberately do NOT use the IOC that crypto market ENTRIES use.
        # An entry that only half-fills is fine — we journal the half and move on.
        # An exit that only half-fills leaves the position part-sold while this
        # journal still says we hold all of it, and the next cycle would then try
        # to sell more than the broker holds.
        #
        # NOTE that GTC does not prevent that: a resting limit fills partially
        # just as readily as an IOC, and we cancel after ~6s either way, so the
        # time-in-force was never the protection this comment claimed. The actual
        # protection is below — a part-filled exit is now adopted and the journal
        # shrunk to the remainder, so no cycle can ever oversell.
        tif = "gtc" if trade.asset_class == "crypto" else "day"
        attempts = _exit_attempts.get(trade.id, 0)

        # Never ask for more than the broker can deliver. Crypto only: the
        # shortfall is the fee-in-kind haircut (see _broker_held_qty), and stock
        # quantities are whole shares that the journal already matches.
        sell_qty = trade.qty
        if trade.asset_class == "crypto":
            from qt.services.reconcile import CRYPTO_FEE_IN_KIND_MAX_PCT

            held = await _broker_held_qty(client, trade.symbol)
            if held is not None and held < trade.qty:
                sell_qty = held
                shortfall_pct = (trade.qty - held) / trade.qty * 100 if trade.qty else 0.0
                if 0 < shortfall_pct <= CRYPTO_FEE_IN_KIND_MAX_PCT:
                    # Inside the fee band this IS the fee, and reconcile would
                    # make exactly this correction on its next pass. Doing it
                    # here keeps the P&L honest about what we actually sold.
                    # A LARGER gap is left alone: reconcile deliberately never
                    # auto-corrects unexplained drift, and neither do we — we
                    # simply refuse to order more than exists.
                    trade.qty = held
                    trade.notional = (trade.entry_price or price) * held

        # An order the broker must reject is not worth placing. Below the
        # minimum there is nothing to retry INTO, so this is the give-up branch
        # that did not exist: stop submitting, say so once, and leave the trade
        # open and truthful so the position can be flattened by hand.
        if sell_qty <= 0 or sell_qty * price < MIN_ORDER_NOTIONAL_USD:
            _exit_attempts[trade.id] = attempts + 1
            if attempts == 0:
                gone = "the broker holds none of it" if sell_qty <= 0 else (
                    f"{sell_qty:g} left is worth ${sell_qty * price:,.2f}, under Alpaca's "
                    f"${MIN_ORDER_NOTIONAL_USD:g} minimum"
                )
                session.add(
                    AuditLog(
                        category="trade",
                        message=f"[paper] SELL {trade.symbol} NOT SUBMITTED — {gone}",
                        detail=f"wanted to exit ({reason}) but no sellable quantity remains",
                    )
                )
                await notify.slack_cat(
                    session,
                    "reconciliation",
                    f":warning: *PAPER* cannot exit *{trade.symbol}* — {gone}. QT has stopped "
                    "retrying this order; the position needs closing by hand.",
                )
            return False
        # Market+fractional strategies sell the whole (possibly fractional)
        # position at market — fills at once, no escalating chase. Otherwise the
        # default escalating marketable-limit sell: widen the buffer with each
        # prior miss (up to slip_max_pct) so a fast drop still gets out — still a
        # limit, never a naked market order.
        pct = _escalated_exit_pct(slip_pct, slip_max_pct, attempts)
        try:
            if market:
                order = await client.submit_market_order(
                    trade.symbol, "sell", client_order_id, qty=sell_qty, time_in_force=tif,
                )
            else:
                limit = _round_price(price * (1 - pct / 100))
                order = await client.submit_order(
                    trade.symbol, sell_qty, "sell", limit, client_order_id, time_in_force=tif,
                )
        except AlpacaError as exc:
            _exit_attempts[trade.id] = attempts + 1
            session.add(
                AuditLog(
                    category="trade",
                    message=f"[paper] SELL {trade.symbol} FAILED — will retry next cycle",
                    detail=str(exc),
                )
            )
            # A sell the broker refuses outright is the loudest version of a
            # stuck position, so it gets the same alert as one that keeps
            # missing — this branch was the silent one.
            await _alert_stuck_exit(session, trade, attempts + 1)
            return False
        filled, last_seen = await _await_fill(client, order["id"])
        if not filled:
            settled = last_seen or {}
            final = None
            # Mirrors open_trade: a DEAD order is finished and cannot be
            # cancelled, anything else is still working and must be pulled — and
            # a cancel can race a fill, so re-read afterwards. open_trade has
            # adopted that late fill since the RENDER incident; the EXIT side
            # never did, and threw away the answer it had just paid for.
            if settled.get("status") not in DEAD_STATUSES:
                try:
                    await client.cancel_order(order["id"])
                except AlpacaError:
                    pass
                try:
                    final = await client.get_order(order["id"])
                except AlpacaError:
                    final = None
                if final and float(final.get("filled_qty") or 0) > 0:
                    settled = final
            sold = float(settled.get("filled_qty") or 0)
            if sold >= sell_qty - 1e-9 and sold > 0:
                filled = settled  # it completed after all — book it as an exit
            elif sold > 0:
                # A PARTIAL exit. The units are gone from the account whatever
                # our journal says, so the journal follows the account: shrink to
                # the remainder. Left whole, the next tick would order the full
                # size again, the broker would refuse it for want of coins, and
                # this would repeat every 60 seconds forever.
                sold_price = float(settled.get("filled_avg_price") or price)
                remaining = trade.qty - sold
                slice_pnl = round((sold_price - (trade.entry_price or sold_price)) * sold, 2)
                trade.qty = remaining
                trade.notional = (trade.entry_price or sold_price) * remaining
                _exit_attempts[trade.id] = attempts + 1
                session.add(
                    AuditLog(
                        category="trade",
                        message=f"[paper] SELL {trade.symbol} PART-FILLED {sold:g} @ "
                        f"${sold_price:,.4f} — {remaining:g} still held, retrying next cycle",
                        detail=f"realized ${slice_pnl:,.2f} on the part sold; the trade's own "
                        f"P&L will cover the remaining {remaining:g} only. Reason: {reason}",
                    )
                )
                await notify.slack_cat(
                    session,
                    "trade_confirmations",
                    f":large_yellow_circle: *PAPER* part-sold {sold:g} × *{trade.symbol}* @ "
                    f"${sold_price:,.4f} (realized ${slice_pnl:,.2f}); {remaining:g} still held.",
                )
                return False
            else:
                _exit_attempts[trade.id] = attempts + 1
                miss = "at market" if market else f"at {pct:.1f}%"
                session.add(
                    AuditLog(
                        category="trade",
                        message=f"[paper] SELL {trade.symbol} did not fill {miss} "
                        f"(attempt {attempts + 1}) — will retry next cycle",
                        detail=f"broker status: {(last_seen or {}).get('status') or 'unknown'}",
                    )
                )
                await _alert_stuck_exit(session, trade, attempts + 1)
                return False
        _exit_attempts.pop(trade.id, None)  # filled — reset the escalation
        trade.exit_order_id = order["id"]
        exit_price = float(filled.get("filled_avg_price") or price)
        # Book the P&L on what the broker actually sold, never on what we asked
        # for. These differ whenever the order was clamped to the held quantity.
        actually_sold = float(filled.get("filled_qty") or 0)
        if actually_sold and actually_sold < trade.qty:
            trade.qty = actually_sold

    trade.exit_price = exit_price
    trade.exit_at = datetime.now(timezone.utc)
    trade.exit_reason = reason
    trade.pnl = round((exit_price - (trade.entry_price or exit_price)) * trade.qty, 2)
    trade.status = "closed"

    pnl_pct = ((exit_price / trade.entry_price - 1) * 100) if trade.entry_price else 0
    emoji = ":chart_with_upwards_trend:" if trade.pnl >= 0 else ":chart_with_downwards_trend:"
    session.add(
        AuditLog(
            category="trade",
            message=f"[{trade.mode}] SELL {trade.qty:g} {trade.symbol} @ ${exit_price:,.4f} "
            f"→ P&L ${trade.pnl:,.2f} ({pnl_pct:+.2f}%)",
            detail=reason,
        )
    )
    await notify.slack_cat(
        session,
        "trade_confirmations",
        f"{emoji} *{trade.mode.upper()}* sold {trade.qty:g} × *{trade.symbol}* @ ${exit_price:,.4f} "
        f"· reason: {reason} · P&L *${trade.pnl:,.2f}* ({pnl_pct:+.2f}%)",
    )
    return True
