"""The trading engine.

Design rules:
- Decision logic is in PURE functions (evaluate_entry / evaluate_exit /
  check_rails) that take plain data and return (verdict, reason) — so the
  risk rails get exhaustive unit tests with no mocking gymnastics.
  "Pure" includes THE CLOCK. Two of check_rails' rails are elapsed-time rules
  (the after-loss cooldown and the non-fill cooldown), and it used to read
  datetime.now() for both — which made it impure in the one way that matters
  to a replay, because a backtest asking "would this have been blocked at
  14:02 last Tuesday" got an answer measured from today. It now takes `now`
  and every caller says which instant it is judging: the engine passes the
  wall clock, the backtester passes its bar's timestamp.
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

from sqlalchemy import func, or_
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
    """The MASTER switch. Not what any given strategy trades — see
    `effective_mode`, which is what the engine actually acts on."""
    return get_setting(session, "engine_mode") or "off"


# How hot each mode is. `off` touches nothing; `shadow` journals a decision and
# places no order; `paper` places real orders against Alpaca's paper account;
# `live` spends real money.
MODE_RANK = {"off": 0, "shadow": 1, "paper": 2, "live": 3}

# What a strategy may be SET to. `off` is deliberately absent: a strategy that
# should not run is disabled, and having two independent ways to express "not
# running" is how one of them gets forgotten.
STRATEGY_MODES = ("shadow", "paper", "live")

# Live is BUILT BUT NOT REACHABLE until live credentials exist and the broker
# layer routes to them. Until then the API refuses to set it, so the column, the
# grouping and the ceiling can all be exercised and tested without a single code
# path that could reach real money. Flipping this on is a deliberate, separate
# act — see docs and the live-credentials work.
LIVE_ENABLED = False


def effective_mode(master: str | None, strategy_mode: str | None) -> str:
    """What a strategy ACTUALLY runs as, given the master switch.

    The master is a CEILING, never a floor. A strategy can only ever run at or
    below it, so:

      * master 'off'    -> nothing runs, whatever the strategies say
      * master 'shadow' -> everything degrades to shadow: one setting stops the
                           whole instance touching the broker without editing 18
                           strategies one at a time
      * master 'live'   -> each strategy runs at its own mode, and a paper
                           strategy stays paper

    Taking the COOLER of the two is the property that matters. A max() here — or
    treating the master as "the mode everything runs in", which is what the code
    did before per-strategy mode existed — would let the master PROMOTE a shadow
    strategy into spending money, and the one-click global kill switch would
    become a one-click global go-live.

    Unknown values on either side collapse to 'off'. A mode nobody recognises is
    not a reason to guess, and every wrong guess here is a wrong guess about
    whether real money moves."""
    m = MODE_RANK.get((master or "off").strip().lower())
    s = MODE_RANK.get((strategy_mode or "").strip().lower())
    if m is None or s is None:
        return "off"
    rank = min(m, s)
    for name, value in MODE_RANK.items():
        if value == rank:
            return name
    return "off"


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
    # RSI as it stood RSI_SLOPE_SPAN completed bars ago. Two numbers are all
    # the direction rules need: rising = rsi > rsi_prev, crossed up through T =
    # rsi_prev <= T < rsi. Only populated when a direction rule is switched on.
    rsi_prev: float | None = None
    # Where this symbol placed in its strategy's ranking THIS cycle (1 = best),
    # and how many were ranked. None on an unranked universe (the scanner is
    # already ordered by the scan itself, and a plain watchlist has no order).
    #
    # Recorded because "bought AVGO, up 2.17%, MACD bullish" doesn't tell you
    # whether it was the strongest name in the basket or the last one standing
    # after everything above it was already held or failed the entry rules. Those
    # are very different trades and the journal read identically for both.
    rank: int | None = None
    rank_of: int | None = None
    # When this symbol last actually TRADED, per the snapshot's latestTrade.
    # None = the snapshot didn't carry one. Crypto only acts on this (see
    # CRYPTO_QUIET_TRADE_MINUTES): reported in the entry reason so a later
    # non-fill has its explanation beside it. It decides nothing.
    last_trade_at: datetime | None = None
    # WHEN THE ENGINE LOOKED: the instant the snapshot that produced `price` came
    # back from the broker. Decides nothing — it is recorded onto whatever journal
    # row this candidate produces (see _stamp_entry_eval) so a later comparison
    # knows which second live was sampling.
    #
    # Per SNAPSHOT CALL, not per tick. A tick evaluates every enabled strategy in
    # sequence and each one issues its own snapshot fetch, so the first symbol of
    # the tick and the last can be many seconds apart; stamping one per-tick
    # instant on all of them would be precise-looking and wrong, which is exactly
    # the ambiguity this is meant to remove. Per (symbol, decision) would be
    # finer still but would be a lie in the other direction: symbols in one batch
    # genuinely were read by ONE call, and inventing distinct instants for them
    # would imply a resolution the fetch does not have.
    #
    # None = we do not know. The scanner path can fall back to a price carried
    # over from the (up to 30s cached) scan when a snapshot returns none, and the
    # instant that price was read is not recorded anywhere — so it stays None
    # rather than borrowing the fresher call's timestamp.
    observed_at: datetime | None = None


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
    # WHO took that losing exit — "this strategy" or another strategy's name.
    # Purely for the rejection message, and only because the message without it
    # was actively misleading: the cooldown is account-wide BY DESIGN (see
    # _build_rail_context), so a strategy that has never once filled this symbol
    # can be blocked by it, and "cooldown after loss" read as though it had lost.
    # 353 such rows in 54 minutes were debugged against that wrong reading.
    # None (the backtester never sets it) simply drops the clause.
    last_loss_by: str | None = None
    leverage_unlocked: bool = False
    daily_loss_usd: float = 0.0
    # Consecutive "did not fill" attempts on this symbol (any strategy), and when
    # the most recent one was. A fill resets the count; rail rejections don't
    # touch it, because never having tried is not the same as having tried and
    # missed. See NONFILL_* below.
    nonfill_strikes: int = 0
    last_nonfill_at: datetime | None = None


# Non-fill circuit breaker. RENDER/USD submitted and cancelled a market order
# every 60 seconds for over an hour: 40 orders, 40 identical journal rows, no
# possibility of success — Alpaca accepted each one ("new") and nothing on the
# pair ever traded. Three strikes buys an hour off, and each further miss
# doubles it up to half a day, so a genuine blip (a fast tape, a momentary gap)
# costs a symbol one hour while a pair that structurally cannot fill puts itself
# away. The wait is measured from the LAST miss, so when it lapses exactly one
# attempt goes through — if that fills, the streak is over; if it misses, the
# window doubles. No new state to persist: the streak is read back off the
# journal, so it survives a restart, which an in-memory counter would not.
# How long a crypto pair can go without a print before the trace says so. This
# is INFORMATION, not a veto — it used to block the entry, and that was wrong.
#
# The reasoning behind the block was that a pair with no recent trades has
# nothing to fill against, generalised from RENDER/USD sitting frozen at exactly
# $1.3749 for over 90 minutes while every order against it went unfilled. Two
# things were wrong with it. Measured on a Monday midday, five of eight MAJOR
# pairs exceeded a 15-minute threshold and DOGE/USD — one of the most heavily
# traded coins there is — exceeded 120 minutes; Alpaca's crypto venue simply
# prints sparsely. And more fundamentally, Alpaca fills crypto against
# market-maker QUOTES, not against recent trades, so print age was never
# measuring the thing that decides whether an order fills. RENDER was a pair
# where both happened to be dead, and one incident is not a rule.
#
# What replaced it is evidence rather than prophecy: the non-fill cooldown backs
# a symbol off after three OBSERVED failures, and with IOC each of those costs
# nothing but the attempt. It also removes a divergence the replay could never
# model — bars carry no print age — which was quietly making every fidelity
# comparison of a crypto strategy unfaithful by construction.
CRYPTO_QUIET_TRADE_MINUTES = 60

NONFILL_STRIKES_BEFORE_COOLDOWN = 3
NONFILL_COOLDOWN_BASE_HOURS = 1.0
NONFILL_COOLDOWN_MAX_HOURS = 12.0


def nonfill_cooldown_hours(strikes: int) -> float:
    """0 = no cooling off yet. Doubles per strike past the threshold, capped."""
    if strikes < NONFILL_STRIKES_BEFORE_COOLDOWN:
        return 0.0
    over = strikes - NONFILL_STRIKES_BEFORE_COOLDOWN
    return min(NONFILL_COOLDOWN_MAX_HOURS, NONFILL_COOLDOWN_BASE_HOURS * (2**over))


def _money(v: float) -> str:
    """A price with enough decimals to mean something: cents on an ordinary
    stock, more on a sub-dollar mover or a cheap coin. Raw floats printed to four
    places ("818.6700") read as noise and hid the dollar sign entirely."""
    dp = 2 if abs(v) >= 1 else 4 if abs(v) >= 0.01 else 6
    return f"${v:,.{dp}f}"


def evaluate_entry(params: dict, candidate: Candidate, now_et: datetime) -> tuple[bool, str]:
    entry = params.get("entry", {})
    # 0 = OFF, like every other optional rule here. It used to be an unguarded
    # comparison, so 0 quietly meant "must not be down today" — invisible on a
    # momentum strategy, fatal on a buy-the-dip one, and impossible to switch
    # off at all. A NEGATIVE value is still a live threshold ("down, but no
    # worse than this"), which is why the test is truthiness and not `> 0`.
    min_gain = entry.get("min_day_gain_pct", 0) or 0
    if min_gain and candidate.change_pct < min_gain:
        return False, f"day gain {candidate.change_pct:.2f}% < required {min_gain:g}%"
    max_gain = entry.get("max_day_gain_pct", 0)
    if max_gain and candidate.change_pct > max_gain:
        return False, f"day gain {candidate.change_pct:.2f}% > max {max_gain:g}% (too extended to chase)"
    min_price = entry.get("min_price", 0)
    if min_price and candidate.price < min_price:
        return False, f"price {_money(candidate.price)} < min {_money(min_price)}"
    max_price = entry.get("max_price", 0)
    if max_price and candidate.price > max_price:
        return False, f"price {_money(candidate.price)} > max {_money(max_price)}"
    if entry.get("require_above_vwap"):
        if candidate.vwap is None:
            return False, "VWAP unavailable — rule requires price above VWAP"
        if candidate.price <= candidate.vwap:
            return False, f"price {_money(candidate.price)} not above VWAP {_money(candidate.vwap)}"
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
    # RSI CROSSING UP, which is a different question from the band above: not
    # "is RSI in a good place" but "has it just turned". Requires BOTH sides —
    # at or below the level `span` bars ago, above it now — so a name that has
    # sat above the threshold for weeks never qualifies. Fail-closed on a
    # missing reading, exactly like MACD and the band.
    cross_above = entry.get("rsi_cross_above", 0) or 0
    if cross_above:
        if candidate.rsi is None or candidate.rsi_prev is None:
            return False, "RSI unavailable — rule requires an RSI crossing"
        if candidate.rsi <= cross_above:
            return False, (
                f"RSI {candidate.rsi:.0f} still at/below {cross_above:g} — no cross up yet"
            )
        if candidate.rsi_prev > cross_above:
            return False, (
                f"RSI {candidate.rsi:.0f} above {cross_above:g}, but it already was "
                f"({candidate.rsi_prev:.0f}) — the cross is not recent"
            )
    # Every clause names the actual reading AND the bar it cleared, so a buy that
    # scraped in reads differently from one that sailed past — the rejection
    # reasons have always done this; the acceptance reason didn't.
    parts = [f"up {candidate.change_pct:.2f}% today" + (f" (min {min_gain:g}%)" if min_gain else "")]
    # A quiet tape is worth SAYING when the entry is taken, so a later non-fill
    # has its explanation sitting right next to it in the journal — but it must
    # not decide anything. See CRYPTO_QUIET_TRADE_MINUTES.
    if candidate.asset_class == "crypto" and candidate.last_trade_at is not None:
        quiet_min = (now_et - candidate.last_trade_at).total_seconds() / 60
        if quiet_min > CRYPTO_QUIET_TRADE_MINUTES:
            parts.append(f"last print {quiet_min:.0f}m ago")
    if entry.get("require_above_vwap") and candidate.vwap is not None:
        parts.append(f"above VWAP {_money(candidate.vwap)}")
    elif entry.get("require_above_vwap"):
        parts.append("above VWAP")
    if entry.get("require_macd_bullish"):
        parts.append("MACD bullish")
    if (rsi_min or rsi_max) and candidate.rsi is not None:
        band = f" (band {rsi_min:g}-{rsi_max:g})" if rsi_min and rsi_max else ""
        parts.append(f"RSI {candidate.rsi:.0f}{band}")
    if cross_above and candidate.rsi is not None and candidate.rsi_prev is not None:
        parts.append(
            f"RSI crossed up through {cross_above:g} "
            f"({candidate.rsi_prev:.0f} → {candidate.rsi:.0f})"
        )
    return True, ", ".join(parts)


def check_rails(
    strategy_cfg: dict,
    sizing_usd: float,
    ctx: RailContext,
    now: datetime | None = None,
) -> tuple[bool, str]:
    """Verdict on one candidate against the risk rails, at the instant `now`.

    `now` is the clock the two ELAPSED-TIME rails are measured against — the
    after-loss cooldown and the non-fill cooldown. None means "the wall clock",
    which is what the live engine wants and is the behaviour this function had
    before the parameter existed; a REPLAY passes the timestamp of the bar it is
    judging, so the answer describes that moment rather than this one. Nothing
    else in here reads a clock, so with `now` supplied the function is pure.
    """
    now = now or datetime.now(timezone.utc)
    risk = ctx.risk
    if ctx.already_open_symbol:
        return False, (
            "rail: this strategy already holds this symbol"
            if strategy_cfg.get("allow_concurrent_symbol")
            else "rail: position already open for this symbol (any strategy)"
        )
    cooldown_h = nonfill_cooldown_hours(ctx.nonfill_strikes)
    if cooldown_h and ctx.last_nonfill_at is not None:
        since = now - ctx.last_nonfill_at
        if since < timedelta(hours=cooldown_h):
            return False, (
                f"rail: cooling off after {ctx.nonfill_strikes} non-fills "
                f"({since.total_seconds()/3600:.1f}h of {cooldown_h:g}h)"
            )
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
        since = now - ctx.last_loss_at
        if since < cooldown:
            # This cooldown is ACCOUNT-WIDE by design — any strategy's losing exit
            # on this symbol cools the symbol for every strategy (see
            # _build_rail_context, and the same statement in the Strategies UI).
            # The message has to SAY so: read as "cooldown after loss" alone it
            # claims the blocked strategy lost, and a strategy that has never
            # filled the symbol at all reads as broken rather than as cooled off
            # by somebody else's trade.
            whose = f"the losing exit was {ctx.last_loss_by}'s" if ctx.last_loss_by else "any strategy's loss counts"
            return False, (
                f"rail: cooldown after loss ({since.total_seconds()/3600:.1f}h of "
                f"{risk['cooldown_hours_after_loss']}h) — account-wide on this symbol, {whose}"
            )
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
    rsi_prev: float | None = None,
    regime_bearish: bool = False,
    is_crypto: bool = False,
    bar_high: float | None = None,
    bar_low: float | None = None,
    out: dict | None = None,
) -> tuple[bool, str]:
    """Return (should_exit, reason). Stops and the max-hold ceiling ALWAYS apply;
    in swing mode the softer exits (take-profit, VWAP, MACD, RSI, regime) wait
    until the day after entry.

    `is_crypto` disables the swing deferral entirely: it defers until the entry's
    ET calendar date rolls over, which is meaningless for a 24/7 asset (New York
    midnight is nothing to Bitcoin). Crypto exits are live from the fill; use
    max_holding_hours for a time limit. Mirrors the flatten-before-close guard,
    which is already stock-only for the same reason.

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
    # LIVE passes a tick price only, so low/high default to it and everything
    # below behaves exactly as before — one 60-second look, sell at market. A
    # BACKTEST passes the bar's extremes: a stop breached at 10:07 and recovered
    # by 10:15 really did take you out, and judging it on the close alone quietly
    # turns a loss into a winner.
    #
    # That is only true when the bar is coarser than live's look, though. On
    # 1-minute bars the poller gets one sample per bar, so an extreme that came
    # and went inside the bar was never on offer to it, and the caller must pass
    # the close as both extremes or the replay books fills nobody could have got.
    # The backtester does exactly that — see backtest._apply_poller_view; this
    # function stays a pure rule evaluator and takes the caller's word for what
    # the price did.
    low = bar_low if bar_low is not None else price
    high = bar_high if bar_high is not None else price
    change_from_entry = (price / entry_price - 1) * 100
    worst_from_entry = (low / entry_price - 1) * 100
    drop_from_high = (1 - low / high_water) * 100 if high_water else 0.0

    def _trigger(level: float, reason: str) -> tuple[bool, str]:
        """Exit AT the trigger level, not the bar's close. Filling at the close
        after an intra-bar breach is the flattering error: price dipped through
        your stop and recovered, and you'd book the recovery you never got."""
        if out is not None:
            out["exit_price"] = level
        return True, reason

    stop_loss = exit_rules.get("stop_loss_pct", 0)
    # ATR stop (opt-in): the effective hard stop = stop_mult × current ATR%. Falls
    # back to the fixed stop when off or atr_pct is unavailable — never stopless.
    atr_stop_mult = float((params.get("atr") or {}).get("stop_mult", 0) or 0)
    effective_stop = stop_loss
    if atr_stop_mult > 0 and atr_pct is not None and atr_pct > 0:
        effective_stop = atr_stop_mult * atr_pct
    # Checked FIRST, which also settles the within-bar ambiguity: if a bar touched
    # both the stop and the take-profit we can't know the order, so we assume the
    # stop came first — the conservative reading, never the flattering one.
    if effective_stop and worst_from_entry <= -effective_stop:
        level = entry_price * (1 - effective_stop / 100)
        if effective_stop != stop_loss:
            return _trigger(level, (
                f"ATR stop-loss: {worst_from_entry:.2f}% ≤ -{effective_stop:.2f}% "
                f"({atr_stop_mult:g}× ATR {atr_pct:.2f}%)"
            ))
        return _trigger(level, f"stop-loss: {worst_from_entry:.2f}% ≤ -{stop_loss:g}%")

    trailing = exit_rules.get("trailing_stop_pct", 0)
    # ATR trailing stop (opt-in), the same shape as the hard stop above: the
    # effective trail = trail_mult × current ATR%. A FIXED percentage cannot
    # serve a pool that spans SPY and a small cap — measured on Werner's
    # "Favorites" strategy, whose 19 names ranged from a 0.5% to a 3.8% median
    # daily move, so a 3% trail sat inside the noise for the names that trended
    # and never triggered at all for the ones that didn't. Falls back to the
    # fixed percentage when off or atr_pct is unavailable — never stopless.
    atr_trail_mult = float((params.get("atr") or {}).get("trail_mult", 0) or 0)
    effective_trail = trailing
    if atr_trail_mult > 0 and atr_pct is not None and atr_pct > 0:
        effective_trail = atr_trail_mult * atr_pct
    if effective_trail and drop_from_high >= effective_trail and low > entry_price * (1 - effective_stop / 100):
        stop_level = high_water * (1 - effective_trail / 100)
        if effective_trail != trailing:
            return _trigger(stop_level, (
                f"ATR trailing stop: {drop_from_high:.2f}% ≥ {effective_trail:.2f}% off high "
                f"{_money(high_water)} ({atr_trail_mult:g}× ATR {atr_pct:.2f}%) "
                f"(stop {_money(stop_level)})"
            ))
        # Was: "trailing stop: 8.54% off high 818.6700" — the drop, and nothing
        # to measure it against. Every other exit names actual vs threshold; this
        # one left you to remember your own setting and do the arithmetic.
        return _trigger(
            stop_level,
            f"trailing stop: {drop_from_high:.2f}% ≥ {trailing:g}% off high "
            f"{_money(high_water)} (stop {_money(stop_level)})",
        )

    # Max holding time is a HARD ceiling, so it sits with the stops ABOVE the
    # swing deferral. It used to be below it, which meant a hold cap shorter than
    # the rest of the entry day silently could not fire in swing mode ("max hold
    # 2h" quietly became "some time tomorrow"). A time limit the user set must
    # always be honoured.
    max_hold = exit_rules.get("max_holding_hours", 0)
    if max_hold:
        held_hours = (now_utc - entry_at).total_seconds() / 3600
        if held_hours >= max_hold:
            return True, f"max holding period: {held_hours:.1f}h ≥ {max_hold:g}h"

    # Swing = don't take the soft exits on the entry day. Stocks only: crypto has
    # no session, so there is no "entry day" to be patient through.
    same_day = entry_at.astimezone(ET).date() == now_utc.astimezone(ET).date()
    if swing_mode and same_day and not is_crypto:
        return False, ""  # stops above already had their chance; be patient today

    take_profit = exit_rules.get("take_profit_pct", 0)
    best_from_entry = (high / entry_price - 1) * 100
    if take_profit and best_from_entry >= take_profit:
        return _trigger(
            entry_price * (1 + take_profit / 100),
            f"take-profit: +{best_from_entry:.2f}% ≥ {take_profit:g}%",
        )

    # GIVE-BACK: keep a fixed share of the peak GAIN, rather than a fixed share
    # of the price. Measured on strategy 31, every losing trade exited on a trail
    # WIDER than the whole gain it was protecting — PLTR peaked at +7.4% under a
    # 13.6% trail, GOOGL at +4.2% under 8.24%, AMZN at +4.7% under 6.99%. A trail
    # wider than the move cannot lock anything in; it can only fire below entry,
    # so it is a second stop-loss wearing a trailing stop's name.
    #
    # This scales with the move instead. At 25%: a position that peaked +170%
    # exits at +127%, one that peaked +5% exits at +3.75%. Two consequences worth
    # stating — it NEVER converts a winner into a loser (the level is always above
    # entry while giveback < 100), and it places no ceiling on the upside, which
    # is what a take-profit does and why the two are alternatives rather than
    # partners.
    giveback = exit_rules.get("exit_giveback_pct", 0) or 0
    peak_gain = (high_water / entry_price - 1) * 100 if high_water else 0.0
    if giveback and peak_gain > 0:
        floor_gain = peak_gain * (1 - giveback / 100)
        level = entry_price * (1 + floor_gain / 100)
        # `low`, not `price`: an intra-bar dip through the level is an exit, the
        # same reading every other price-triggered rule here uses.
        if low <= level:
            return _trigger(
                level,
                f"gave back {giveback:g}% of the peak gain: +{peak_gain:.2f}% "
                f"high → +{floor_gain:.2f}% floor ({_money(level)})",
            )

    if exit_rules.get("exit_below_vwap") and vwap is not None and price < vwap:
        return True, f"price {_money(price)} fell below VWAP {_money(vwap)}"

    # Optional daily-MACD exit: only act on a CONFIRMED bearish cross (False).
    # None means we can't tell right now — never force an exit on that.
    if exit_rules.get("exit_on_macd_bearish") and macd_bullish is False:
        return True, "MACD turned bearish — daily line crossed below its signal"

    # Optional RSI overbought exit: take profit on froth. 0 = off; None RSI (not
    # enough history / fetch blip) never forces an exit.
    exit_rsi_above = exit_rules.get("exit_rsi_above", 0) or 0
    if exit_rsi_above and rsi is not None and rsi >= exit_rsi_above:
        return True, f"RSI {rsi:.0f} ≥ {exit_rsi_above:g} (overbought)"

    # The two DIRECTIONAL RSI exits, mirroring rsi_cross_above on the way in.
    # Both need the earlier reading, and both stay silent without it — a
    # missing signal never forces a sale.
    exit_rsi_below = exit_rules.get("exit_rsi_below", 0) or 0
    if exit_rsi_below and rsi is not None and rsi <= exit_rsi_below:
        return True, f"RSI {rsi:.0f} ≤ {exit_rsi_below:g} (momentum gone)"
    if exit_rules.get("exit_rsi_falling") and rsi is not None and rsi_prev is not None:
        if rsi < rsi_prev:
            return True, (
                f"RSI falling: {rsi_prev:.0f} → {rsi:.0f} over "
                f"{stats.RSI_SLOPE_SPAN} bars"
            )

    # Optional market-regime exit (opt-in, stocks): sell into cash when the broad
    # market is in a downtrend (SPY below its 200-day MA). The caller supplies the
    # bearish flag ONLY for a stock strategy that opted in, and only when the
    # regime is genuinely known (an unknown regime is passed as False), so a data
    # blip never triggers a sell.
    if exit_rules.get("exit_on_regime_bear") and regime_bearish:
        return True, "market regime bearish — SPY below its 200-day average"

    if exit_rules.get("flatten_before_close") and market_closes_soon:
        return True, "flatten before market close — no overnight exposure"

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


async def _quotes_for(
    client: AlpacaClient, trades: list[Trade]
) -> tuple[dict[str, dict], dict[str, datetime]]:
    """Returns (snapshot per symbol, WHEN that snapshot came back per symbol).

    The second map is the exit side of "which second was the engine looking at".
    It is keyed per symbol because the two legs are two separate fetches issued
    one after the other — one instant for both would be wrong for whichever leg
    it wasn't measured on, and the whole point of recording this is that seconds
    matter. Within a leg the symbols really were read by one call, so they
    honestly share an instant."""
    stocks = sorted({t.symbol for t in trades if t.asset_class == "stock"})
    cryptos = sorted({t.symbol for t in trades if t.asset_class == "crypto"})
    quotes: dict[str, dict] = {}
    observed_at: dict[str, datetime] = {}
    if stocks:
        quotes.update(await client.stock_snapshots(stocks))
        # Taken AFTER the await: this is the moment the engine held that view of
        # the tape and could act on it. The broker sampled it somewhere inside
        # the call, so this is the tight upper bound, not a guess.
        at = datetime.now(timezone.utc)
        observed_at.update(dict.fromkeys(stocks, at))
    if cryptos:
        quotes.update(await client.crypto_snapshots(cryptos))
        at = datetime.now(timezone.utc)
        observed_at.update(dict.fromkeys(cryptos, at))
    return quotes, observed_at


def _price_from_snapshot(snap: dict) -> tuple[float | None, float | None]:
    price = (snap.get("latestTrade") or {}).get("p") or (snap.get("dailyBar") or {}).get("c")
    vwap = (snap.get("dailyBar") or {}).get("vw")
    return (float(price) if price else None, float(vwap) if vwap else None)


def last_trade_at_from_snapshot(snap: dict) -> datetime | None:
    """When the symbol last traded, from latestTrade.t. None if absent or
    unparseable — callers must treat that as "unknown", never as "stale", or a
    change in Alpaca's payload shape would halt trading everywhere at once."""
    raw = (snap.get("latestTrade") or {}).get("t")
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


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


def _strategy_macd_periods(strategy) -> tuple[int, int, int]:
    """The row's MACD periods, for the ranking metrics. Tolerates unparseable
    params the same way every other reader here does — a bad params blob must
    not take down the tick, it just means the 12/26/9 defaults."""
    try:
        return _macd_periods(json.loads(strategy.params) or {})
    except (TypeError, ValueError):
        return (12, 26, 9)


def _macd_periods(params: dict) -> tuple[int, int, int]:
    m = params.get("macd") or {}
    return int(m.get("fast", 12)), int(m.get("slow", 26)), int(m.get("signal", 9))


def _atr_period(params: dict) -> int:
    return int((params.get("atr") or {}).get("period", 14) or 14)


def _atr_stop_enabled(params: dict) -> bool:
    """ATR stop is on when stop_mult > 0 (it replaces the fixed hard stop)."""
    return float((params.get("atr") or {}).get("stop_mult", 0) or 0) > 0


def _atr_trail_enabled(params: dict) -> bool:
    """ATR trailing stop is on when trail_mult > 0 (it replaces the fixed
    trailing_stop_pct). Independent of the hard stop's multiplier."""
    return float((params.get("atr") or {}).get("trail_mult", 0) or 0) > 0


def _atr_value_needed(params: dict) -> bool:
    """Whether evaluate_exit needs an atr_pct at all. Every caller that FETCHES
    the daily bars must ask this and not `_atr_stop_enabled`, or a strategy with
    only the ATR TRAIL configured gets atr_pct=None and falls silently back to
    the fixed percentage — the feature switched on in the UI and doing nothing."""
    return _atr_stop_enabled(params) or _atr_trail_enabled(params)


def _atr_sizing_enabled(params: dict) -> bool:
    """ATR sizing needs BOTH a risk budget AND a stop multiple — the size is
    derived from the ATR stop distance, so it's meaningless without the stop."""
    a = params.get("atr") or {}
    return float(a.get("risk_usd", 0) or 0) > 0 and float(a.get("stop_mult", 0) or 0) > 0


RSI_PERIOD = 14  # standard Wilder period; fixed (not a user knob) to keep the config lean


def _entry_rsi_enabled(params: dict) -> bool:
    """The RSI entry band is on when either bound is set (> 0)."""
    e = params.get("entry", {})
    return (
        float(e.get("rsi_min", 0) or 0) > 0
        or float(e.get("rsi_max", 0) or 0) > 0
        or float(e.get("rsi_cross_above", 0) or 0) > 0
    )


def _exit_rsi_enabled(params: dict) -> bool:
    """Any RSI-based exit: the overbought ceiling, the momentum-gone floor, or
    the "RSI turned down" direction rule. This is the gate every daily-bars FETCH
    asks, so a rule missing from here is a rule that silently never fires."""
    x = params.get("exit") or {}
    return (
        float(x.get("exit_rsi_above", 0) or 0) > 0
        or float(x.get("exit_rsi_below", 0) or 0) > 0
        or bool(x.get("exit_rsi_falling"))
    )


def _wants_regime_exit(strategy: Strategy | None) -> bool:
    """A stock strategy that opted into the market-regime exit (sell to cash when
    SPY is below its 200-day). Stocks only — crypto has no SPY regime."""
    if strategy is None or strategy.asset_class != "stock":
        return False
    try:
        return bool(json.loads(strategy.params).get("exit", {}).get("exit_on_regime_bear"))
    except (TypeError, ValueError):
        return False


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
    """Calendar-day window for the daily-bars fetch, from the strategy's OWN
    indicator periods.

    `MACD_LOOKBACK_DAYS` is the floor, not the answer. It was the answer for
    MACD until 2026-08-07, which made `warmup_days_for`'s promise — "from the
    strategy's own indicator settings rather than one constant for everybody" —
    true of ATR and false of MACD. The schema allows `slow` up to 200 and
    `signal` up to 100, i.e. ~300 completed bars, against a flat 120 CALENDAR
    days (~83 trading bars for a stock). The MACD was then undefined across the
    opening stretch of the window, and an undefined MACD silently drops every
    entry that asked for it — the failure looks like a strategy that does not
    fire, not like a fetch that was too short.

    Both terms use the same `n * 2 + 10` shape ATR already used: a calendar
    window roughly double the bars needed, plus a margin for holidays. MACD
    needs `slow + signal` bars for its signal line to exist at all.

    Defaults are deliberately unchanged: 12/26/9 asks for (26+9)*2+10 = 80,
    below the 120 floor, so every existing MACD-only fetch is byte-identical."""
    _fast, slow, signal = _macd_periods(params)
    need = max(MACD_LOOKBACK_DAYS, (slow + signal) * 2 + 10)
    if want_atr:
        need = max(need, _atr_period(params) * 2 + 10)
    return need


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
            cand.rsi_prev = stats.rsi_back(daily, RSI_PERIOD)


async def _daily_exit_signals(
    session: Session, client: AlpacaClient, open_trades: list[Trade]
) -> tuple[
    dict[int, bool | None], dict[int, float | None],
    dict[int, float | None], dict[int, float | None],
]:
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
        want_atr = _atr_value_needed(params)
        want_rsi = _exit_rsi_enabled(params)
        if not (want_macd or want_atr or want_rsi):
            continue
        grouped[t.strategy_id] = (s, params, {t.symbol})

    macd_flags: dict[int, bool | None] = {}
    atr_pcts: dict[int, float | None] = {}
    rsi_flags: dict[int, float | None] = {}
    # The reading RSI_SLOPE_SPAN bars back, for exit_rsi_falling. Computed in
    # the same pass off the same bars — no extra fetch.
    rsi_prevs: dict[int, float | None] = {}
    for sid, (s, params, symbols) in grouped.items():
        fast, slow, signal = _macd_periods(params)
        period = _atr_period(params)
        want_macd = bool(params.get("exit", {}).get("exit_on_macd_bearish"))
        want_atr = _atr_value_needed(params)
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
                rsi_prevs[t.id] = stats.rsi_back(daily, RSI_PERIOD)
    return macd_flags, atr_pcts, rsi_flags, rsi_prevs


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
        # ONE PASS PER MODE. Each mode is its own book: `Trade.mode` partitions
        # every rail query (daily loss, exposure, trade-rate, cooldown), so a
        # paper strategy's losses must never eat a live strategy's budget or the
        # other way round. Running the modes as separate passes is what keeps
        # that partition true rather than merely likely.
        groups: dict[str, list[Strategy]] = {}
        for strategy in session.query(Strategy).filter(Strategy.enabled.is_(True)).all():
            run_as = effective_mode(mode, strategy.mode)
            if run_as == "off":
                continue
            groups.setdefault(run_as, []).append(strategy)

        # Exits are keyed on the trade's OWN recorded mode, so they must run for
        # every mode that has open trades — including one whose strategies were
        # all disabled or demoted since. Otherwise demoting a strategy to shadow
        # would abandon its open paper positions with their stops unwatched.
        for run_as in sorted(
            {t.mode for t in session.query(Trade.mode)
             .filter(Trade.status == "open").distinct()}
            | set(groups),
            key=lambda m: MODE_RANK.get(m, 0),
        ):
            if effective_mode(mode, run_as) != run_as:
                continue  # the master switch has this book cooled off
            await _manage_exits(session, client, run_as, market_open, closes_soon)

        if lifecycle.is_shutting_down():
            log.info("shutdown requested — skipping new entries this tick")
        else:
            for run_as, members in groups.items():
                await _consider_entries(
                    session, client, run_as, equity, market_open, leverage_unlocked,
                    strategies=members,
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
        quotes, observed_at = await _quotes_for(client, open_trades)
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
    macd_exit_flags, atr_exit_pcts, rsi_exit_flags, rsi_exit_prevs = await _daily_exit_signals(
        session, client, open_trades
    )

    # Market-regime exit (opt-in, stocks): compute the regime ONCE, and only if
    # some open stock position's strategy wants it (otherwise zero extra work — and
    # regime_status is cached anyway). Fail-safe: an unknown regime (insufficient
    # data / fetch blip) is treated as NOT bearish, so a data hiccup never sells
    # everything to cash.
    regime_bearish = False
    if any(_wants_regime_exit(session.get(Strategy, t.strategy_id)) for t in open_trades):
        try:
            rs = await regime.regime_status(client)
            regime_bearish = rs.get("ok") is False and not rs.get("insufficient_data", False)
        except Exception as exc:  # noqa: BLE001 — never force exits on a regime fetch failure
            log.warning("regime exit check failed — holding positions: %s", exc)

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
                rsi_prev=rsi_exit_prevs.get(trade.id),
                regime_bearish=regime_bearish and trade.asset_class == "stock",
                # No swing deferral for a 24/7 asset — see evaluate_exit.
                is_crypto=trade.asset_class == "crypto",
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
        # WHICH SECOND this exit was decided on, and the price it was decided on.
        # The residual the fidelity report calls a "poll phase floor" is an EXIT
        # residual — the replay checks the stop at bar close, the engine checked
        # it at :17 — so this is the column that closes it. exit_price is the
        # FILL, which has slippage and a limit in it; this is what was seen.
        #
        # Stamped only when an exit was actually decided, not on every look at
        # every open position: that would dirty every open row every minute for
        # no new information. Stamped even if close_trade could not complete —
        # the LOOK happened, and the next attempt overwrites it, so the value
        # always describes the look that produced the exit finally recorded.
        # After the await and attribute-only, so it cannot affect the order.
        trade.exit_eval_at = observed_at.get(trade.symbol)
        trade.exit_eval_price = price


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


def _stamp_entry_eval(
    session: Session, strategy_id: int, cand: Candidate, trade: Trade | None
) -> None:
    """Record WHICH SECOND the engine was looking at, and the price it saw, onto
    whatever journal row this entry decision produced.

    Two shapes of row need it and only one of them comes back to the caller. A
    fill is returned by execution.open_trade; a decline is NOT — open_trade
    journals a "rejected" Trade into this session and returns None, on all five
    of its failure paths. So when there is no trade we stamp the rejected rows it
    left behind, matched on this strategy and symbol.

    Rejections matter here at least as much as fills: "live passed on this, the
    replay bought it" is the argument the fidelity comparison exists to settle,
    and it cannot be settled without knowing which second live was looking.

    Wrapped so it can never abort a trade. The order is already placed and the
    row is already correct without these two fields — a bookkeeping detail must
    not be able to raise past a real position.
    """
    try:
        rows = (
            [trade]
            if trade is not None
            else [
                o
                for o in session.new
                if isinstance(o, Trade)
                and o.strategy_id == strategy_id
                and o.symbol == cand.symbol
                and o.entry_eval_at is None
            ]
        )
        for row in rows:
            # May be None — a candidate whose price came from somewhere with no
            # recorded observation instant. Left as NULL rather than filled in
            # with "now", because "we do not know when the engine looked" has to
            # stay distinguishable from a recorded time.
            row.entry_eval_at = cand.observed_at
            row.entry_eval_price = cand.price
    except Exception:  # noqa: BLE001 — never let bookkeeping break a trade
        log.warning(
            "could not record the evaluation instant for %s", cand.symbol, exc_info=True
        )


async def _consider_entries(
    session: Session,
    client: AlpacaClient,
    mode: str,
    equity: float,
    market_open: bool,
    leverage_unlocked: bool,
    strategies: list[Strategy] | None = None,
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

    # `tick` passes the group it resolved for THIS mode. The query is the
    # fallback for direct callers (tests, and any future caller that has no
    # grouping to do) and deliberately still returns every enabled strategy —
    # narrowing it by `Strategy.mode == mode` would be wrong, because the master
    # switch can cool a paper strategy into the shadow pass and a column filter
    # cannot see that.
    if strategies is None:
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
                    "rank": cand.rank,
                    "rank_of": cand.rank_of,
                    "decision": "",
                    "reason": "",
                }
                rank_note = _rank_note(strategy.rank_by, cand)
                if not entry_ok:
                    row["decision"], row["reason"] = "skipped", f"{entry_reason}{rank_note}"
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
                    {
                        "max_positions": strategy.max_positions,
                        "sleeve_usd": strategy.sleeve_usd,
                        "allow_concurrent_symbol": strategy.allow_concurrent_symbol,
                    },
                    entry_sizing,
                    ctx,
                    # The wall clock, read HERE per candidate — the same instant
                    # and the same frequency check_rails read it for itself
                    # before the parameter existed. Live behaviour is unchanged;
                    # it is now merely stated rather than assumed.
                    datetime.now(timezone.utc),
                )
                version_id = _latest_version_id(session, strategy.id)
                if not rails_ok:
                    row["decision"], row["reason"] = "blocked", f"{entry_reason}, but {rails_reason}"
                    trace["candidates"].append(row)
                    # The trace above always shows the block; the JOURNAL only
                    # records it when it's news. See RAIL_REJECTION_REPEAT_MINUTES.
                    if _rail_rejection_is_new(session, mode, strategy.id, cand.symbol, rails_reason):
                        session.add(
                            Trade(
                                strategy_id=strategy.id,
                                config_version_id=version_id,
                                mode=mode, symbol=cand.symbol, asset_class=cand.asset_class,
                                account_id=get_setting(session, "current_account_id"),
                                qty=0, notional=0, status="rejected",
                                entry_reason=f"wanted to buy ({entry_reason}{rank_note}) but {rails_reason}",
                                # Which second the engine was looking at, and at
                                # what price, when it declined. Nothing else on a
                                # rejected row records a price at all.
                                entry_eval_at=cand.observed_at,
                                entry_eval_price=cand.price,
                            )
                        )
                    continue

                bought_reason = f"{entry_reason}{rank_note}; {rails_reason}"
                trace["candidates"].append(row)

                from qt.services import execution

                # The trace used to say "bought" HERE, before the order existed.
                # open_trade declines in five ways — under Alpaca's $1 minimum,
                # zero whole shares, a broker rejection, an order that never
                # filled, a fill poll that timed out — and each returns None
                # after journalling why. The trace never heard about any of them,
                # so a name whose order kept failing showed "bought · all rails
                # passed" on every single run: it never became a position, so it
                # was never blocked by the already-held rail, so it was retried
                # and mis-reported a minute later, forever.
                trade = await execution.open_trade(
                    session, client, strategy, version_id, mode, cand,
                    bought_reason,
                    sizing_usd=entry_sizing,
                )
                # Whichever row that produced — the fill, or the "rejected" row
                # open_trade journals when the order never happened — carries the
                # instant the engine looked and the price it saw.
                _stamp_entry_eval(session, strategy.id, cand, trade)
                if trade is None:
                    # open_trade wrote the real reason onto a rejected Trade it
                    # added to this session; prefer that over a generic line, so
                    # the panel names the actual obstacle.
                    why = next(
                        (
                            o.entry_reason
                            for o in session.new
                            if isinstance(o, Trade)
                            and o.symbol == cand.symbol
                            and o.status == "rejected"
                            and o.entry_reason
                        ),
                        None,
                    )
                    row["decision"] = "not filled"
                    row["reason"] = why or f"{bought_reason}; but the order did not complete"
                else:
                    row["decision"], row["reason"] = "bought", bought_reason

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
            {
                        "max_positions": strategy.max_positions,
                        "sleeve_usd": strategy.sleeve_usd,
                        "allow_concurrent_symbol": strategy.allow_concurrent_symbol,
                    },
            strategy.sizing_usd,
            ctx,
            datetime.now(timezone.utc),  # the wall clock, as before — see _consider_entries
        )
        reason = f"DCA scheduled lot (every {interval_days}d)"
        if not rails_ok:
            if _rail_rejection_is_new(session, mode, strategy.id, sym, rails_reason):
                session.add(
                    Trade(
                        strategy_id=strategy.id, config_version_id=version_id,
                        mode=mode, symbol=sym, asset_class=cand.asset_class,
                        account_id=get_setting(session, "current_account_id"),
                        qty=0, notional=0, status="rejected",
                        entry_reason=f"wanted to buy ({reason}) but {rails_reason}",
                        entry_eval_at=cand.observed_at,
                        entry_eval_price=cand.price,
                    )
                )
            continue

        from qt.services import execution

        trade = await execution.open_trade(
            session, client, strategy, version_id, mode, cand,
            f"{reason}; {rails_reason}",
        )
        _stamp_entry_eval(session, strategy.id, cand, trade)


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
                    client, strategy.asset_class, syms, strategy.rank_by, strategy.top_n,
                    _strategy_macd_periods(strategy),
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
                client, strategy.asset_class, syms, strategy.rank_by, strategy.top_n,
                _strategy_macd_periods(strategy),
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
            snapped_at = datetime.now(timezone.utc)  # when this view of the tape landed
            for row in rows:
                snap = snaps.get(row["symbol"]) or {}
                price, vwap = _price_from_snapshot(snap)
                candidates.append(
                    Candidate(
                        symbol=row["symbol"], asset_class=row["asset_class"],
                        price=price or row["price"], change_pct=row["change_pct"], vwap=vwap,
                        last_trade_at=last_trade_at_from_snapshot(snap),
                        # Only when the price came from THIS call. The fallback is
                        # the scan's own price, read at an instant nothing records
                        # (the scan is cached for up to 30s), and stamping it with
                        # this timestamp would claim a freshness it does not have.
                        observed_at=snapped_at if price else None,
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
                # Measured HERE, not after the rolling-stats fetch below: the
                # price these candidates carry comes from `snaps`, and a second
                # round-trip's worth of latency does not belong in it.
                snapped_at = datetime.now(timezone.utc)
                # Crypto day-gain is the ROLLING 24h change, not the 00:00-UTC
                # calendar-day one. This branch was the last consumer still using
                # the calendar day, and it is reached by every "watchlist" and
                # every "both" strategy (the API forces rank_enabled off for
                # "both"), so those strategies were gating entries on a number
                # that no other part of QT agrees with: just after 00:00 UTC every
                # crypto candidate read ~0% and nothing could qualify, and by
                # 23:00 UTC the same coin read at its daily maximum. The scanner,
                # the watchlist page, the backtest and the optimizer all use
                # rolling_24h — and the fidelity report was attributing the
                # resulting live-vs-replay gap to the signal.
                crypto_stats = (
                    {}
                    if strategy.asset_class == "stock"
                    else await scanner.crypto_rolling_stats(client, symbols)
                )
                for sym in symbols:
                    snap = snaps.get(sym) or {}
                    price, vwap = _price_from_snapshot(snap)
                    if strategy.asset_class == "stock":
                        daily = (snap.get("dailyBar") or {}).get("c")
                        prev = (snap.get("prevDailyBar") or {}).get("c")
                        change = ((daily / prev - 1) * 100) if daily and prev else 0.0
                    else:
                        roll = crypto_stats.get(sym)  # not `stats` — see _pool_metrics
                        change = roll[1] if roll else 0.0
                    if price:
                        candidates.append(
                            Candidate(
                                symbol=sym, asset_class=strategy.asset_class,
                                price=price, change_pct=round(change, 2), vwap=vwap,
                                last_trade_at=last_trade_at_from_snapshot(snap),
                                observed_at=snapped_at,
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
    snapped_at = datetime.now(timezone.utc)  # when this view of the tape landed
    # Crypto "day gain" is the ROLLING 24h change (the scanner's definition —
    # crypto has no session day), NOT the 00:00-UTC calendar bar. The snapshot
    # still supplies price/vwap (freshest for sizing); only the change baseline
    # differs. Stocks keep the session-day change from the snapshot.
    crypto_stats = {} if is_stock else await scanner.crypto_rolling_stats(client, symbols)
    out: list[Candidate] = []
    for sym in symbols:
        snap = snaps.get(sym) or {}
        price, vwap = _price_from_snapshot(snap)
        if is_stock:
            daily = (snap.get("dailyBar") or {}).get("c")
            prev = (snap.get("prevDailyBar") or {}).get("c")
            change = ((daily / prev - 1) * 100) if daily and prev else 0.0
        else:
            roll = crypto_stats.get(sym)  # not `stats` — see _pool_metrics
            change = roll[1] if roll else 0.0
        if price:
            out.append(
                Candidate(
                    symbol=sym, asset_class=asset_class,
                    price=price, change_pct=round(change, 2), vwap=vwap,
                    last_trade_at=last_trade_at_from_snapshot(snap),
                    observed_at=snapped_at,
                )
            )
    return out


async def _pool_metrics(
    client: AlpacaClient, asset_class: str, symbols: list[str], rank_by: str,
    macd_periods: tuple[int, int, int] = (12, 26, 9),
) -> tuple[dict, dict, dict, dict, datetime | None]:
    """Snapshot a pool and compute the ranking metric for each symbol. Returns
    (metrics, price_map, vwap_map, trade_at_map, snapped_at). Shared by live
    candidate selection and the 'current ranking' view so both rank identically.
    Only fetches the daily bars a bar-based metric needs; momentum_today rides on
    the snapshot alone.

    `snapped_at` is when the PRICE snapshot landed — the instant the engine saw
    this pool. It is measured before the daily-bars fetch below, which on a
    bar-based ranking is a second round trip over 320 days of history: timing the
    look after that would add the latency of a call the prices did not come from.
    None when there was nothing to snapshot."""
    from qt.services import stats

    price_map: dict[str, float] = {}
    vwap_map: dict[str, float | None] = {}
    trade_at_map: dict[str, datetime | None] = {}
    metrics: dict[str, dict[str, float | None]] = {}
    if not symbols:
        return metrics, price_map, vwap_map, trade_at_map, None

    is_stock = asset_class == "stock"
    snaps = await (client.stock_snapshots(symbols) if is_stock else client.crypto_snapshots(symbols))
    snapped_at = datetime.now(timezone.utc)
    # Crypto momentum_today = rolling 24h change (one definition everywhere —
    # see scanner.rolling_24h); stocks use the snapshot's session-day change.
    crypto_stats = {} if is_stock else await scanner.crypto_rolling_stats(client, symbols)
    for sym in symbols:
        snap = snaps.get(sym) or {}
        price, vwap = _price_from_snapshot(snap)
        if is_stock:
            daily = (snap.get("dailyBar") or {}).get("c")
            prev = (snap.get("prevDailyBar") or {}).get("c")
            change = ((daily / prev - 1) * 100) if daily and prev else None
        else:
            # NOT named `stats`: this function also uses the qt.services.stats
            # MODULE below, and binding that name here made it a local for the
            # whole function — so a CRYPTO pool ranked by return_30d /
            # relative_strength / rsi reached `stats.pct_change_over` holding a
            # rolling-stats tuple and raised AttributeError. That propagates out
            # of _candidates_for (which only catches AlpacaError) and kills the
            # entire tick, for every strategy, every 60 seconds.
            roll = crypto_stats.get(sym)
            change = roll[1] if roll else None
        if price:
            price_map[sym] = price
        vwap_map[sym] = vwap
        trade_at_map[sym] = last_trade_at_from_snapshot(snap)
        metrics[sym] = {
            "momentum_today": round(change, 2) if change is not None else None,
            "return_30d": None,
            "relative_strength": None,
            "rs_vs_spy": None,
            "rsi": None,
            "macd_strength": None,
            "macd_slope": None,
        }

    # rs_vs_spy is a STOCK-only metric (SPY is the benchmark); never fetch SPY for crypto.
    RS_VS_SPY_WINDOW = 90  # calendar days of relative return
    want_rs_vs_spy = rank_by == "rs_vs_spy" and asset_class == "stock"

    macd_rank = rank_by in ("macd_strength", "macd_slope")
    if rank_by in ("return_30d", "relative_strength", "rsi") or want_rs_vs_spy or macd_rank:
        # MACD ranking gets the SAME 120-day window the MACD entry signal uses
        # (MACD_LOOKBACK_DAYS), so a strategy that ranks and gates on MACD is
        # reading one indicator rather than two that disagree.
        lookback_days = (
            320 if rank_by in ("relative_strength", "rs_vs_spy")
            else MACD_LOOKBACK_DAYS if macd_rank
            else 60
        )
        start = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        fetch_symbols = symbols + ["SPY"] if want_rs_vs_spy else symbols
        bars_by_symbol = await client.historical_bars(fetch_symbols, asset_class, "1Day", start)
        for sym in symbols:
            bars = bars_by_symbol.get(sym) or []
            cur = price_map.get(sym)
            metrics[sym]["return_30d"] = stats.pct_change_over(bars, 30, cur)
            metrics[sym]["relative_strength"] = stats.vs_sma_pct(bars, 200, cur)
            metrics[sym]["rsi"] = stats.rsi(bars, RSI_PERIOD, cur)
            if macd_rank:
                # COMPLETED bars only and no live price — matching the MACD
                # entry signal exactly (see _completed_daily_bars). Passing
                # `cur` here would make the ranking twitch every 60s on an
                # indicator deliberately built to hold still.
                done = _completed_daily_bars(bars, asset_class)
                fast, slow, signal = macd_periods
                metrics[sym]["macd_strength"] = stats.macd_strength_pct(done, fast, slow, signal)
                metrics[sym]["macd_slope"] = stats.macd_slope_pct(done, fast, slow, signal)
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

    return metrics, price_map, vwap_map, trade_at_map, snapped_at


def _rank_note(rank_by: str | None, cand: Candidate) -> str:
    """", ranked #4 of 25 by momentum today" — or "" on an unranked universe.

    Without this the journal said "up 2.17% today, MACD bullish" whether the buy
    was the strongest name in the basket or the twenty-fourth. The engine takes
    candidates strictly best-first, so a low rank means everything above it was
    already held or failed the rules — worth knowing when a strategy that allows
    4 positions keeps ending up in the tail of its own list.

    Takes `rank_by` rather than the Strategy row so the BACKTESTER can produce the
    identical sentence from a plain config dict — the replay now makes the same
    top-N cut live does, and a replayed entry reason that reads differently from
    the journalled one is a difference the fidelity report would have to explain.
    """
    if cand.rank is None:
        return ""
    metric = (rank_by or "").replace("_", " ") or "rank"
    of = f" of {cand.rank_of}" if cand.rank_of else ""
    return f", ranked #{cand.rank}{of} by {metric}"


async def _ranked_candidates(
    client: AlpacaClient, asset_class: str, symbols: list[str], rank_by: str, top_n: int,
    macd_periods: tuple[int, int, int] = (12, 26, 9),
) -> list[Candidate]:
    """Snapshot a POOL of symbols, score each by `rank_by`, and return the top-N
    as candidates. Universe-agnostic: the pool can be a basket's members, a
    watchlist, or a hand-picked custom list. The entry rules still filter the
    result; top-N is the candidate set, not an auto-buy list."""
    from qt.services import ranking

    symbols = sorted(set(symbols))
    if not symbols:
        return []
    metrics, price_map, vwap_map, trade_at_map, snapped_at = await _pool_metrics(
        client, asset_class, symbols, rank_by, macd_periods
    )
    ranked = ranking.rank_symbols(metrics, rank_by, top_n)
    candidates: list[Candidate] = []
    # rank_symbols returns best-first, and the entry loop walks this list in
    # order — so the strongest name gets first refusal on the sleeve. Stamping
    # the position here is what lets the journal say WHERE in the list a buy came
    # from; a symbol with no price is skipped but must not renumber the rest.
    for position, (sym, _value) in enumerate(ranked, start=1):
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
                last_trade_at=trade_at_map.get(sym),
                observed_at=snapped_at,
                rank=position,
                rank_of=len(ranked),
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
        strategy.rank_by, strategy.top_n, _strategy_macd_periods(strategy),
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
    cands = await _ranked_candidates(
        client, s.asset_class, pool, s.rank_by, s.top_n, _strategy_macd_periods(s)
    )
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
    metrics, price_map, _vwap, _trade_at, _snapped_at = await _pool_metrics(
        client, strategy.asset_class, pool, strategy.rank_by,
        _strategy_macd_periods(strategy),
    )

    # Daily-MACD (bullish/bearish) per symbol for the ranking view — a separate,
    # best-effort daily-bars fetch (the view is on-demand, so the extra call is
    # fine). Uses the strategy's own MACD periods, or the 12/26/9 defaults. A blip
    # leaves it None, shown as "—". Purely informational: it does NOT affect the
    # ranking, only the extra column so you can see momentum direction at a glance.
    macd_by_sym: dict[str, bool | None] = {}
    try:
        fast, slow, signal = _macd_periods(json.loads(strategy.params))
        start = (datetime.now(timezone.utc) - timedelta(days=MACD_LOOKBACK_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
        macd_bars = await client.historical_bars(pool, strategy.asset_class, "1Day", start)
        for sym in pool:
            daily = _completed_daily_bars(macd_bars.get(sym) or [], strategy.asset_class)
            macd_by_sym[sym] = stats.macd_bullish([float(b["c"]) for b in daily], fast, slow, signal)
    except Exception:  # noqa: BLE001 — never fail the ranking view on the MACD add-on
        macd_by_sym = {}

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
            "macd_bullish": macd_by_sym.get(sym),
        })
    for sym in pool:  # couldn't rank (metric is None) — surface as unranked, last
        if sym not in ranked_syms:
            rows.append({
                "symbol": sym, "rank": None, "value": None, "in_top_n": False,
                "price": price_map.get(sym), "change_pct": metrics.get(sym, {}).get("momentum_today"),
                "macd_bullish": macd_by_sym.get(sym),
            })
    return rows


# A rail that blocks a candidate blocks it on EVERY 60-second tick until the
# condition lifts, and each block used to write its own journal row. A 24-hour
# post-loss cooldown therefore wrote ~1,440 rows per symbol per strategy; one
# crypto probe produced 500 rows in 54 minutes, 497 of them rejections, and the
# journal the owner reads to answer "why didn't it trade?" was buried under the
# answer repeated a thousand times.
#
# It is the same shape as the non-fill breaker's own rows evicting its evidence
# (see the lookback query below): a guard whose output floods the table it is
# read from. So the journal records the rail's STATE CHANGES, not its ticks —
# one row when a rail starts blocking a (strategy, symbol), and a refresh no
# more than hourly so a long block still shows up in a recent-hours view.
#
# Nothing downstream counts these rows: the fidelity comparison uses them as a
# SET of (symbol, day) keys with one representative reason, entries_today and
# the scoreboard exclude rejections, and the non-fill streak excludes rail rows
# in its query. Fewer duplicates costs no information anywhere.
RAIL_REJECTION_REPEAT_MINUTES = 60


def _rail_signature(entry_reason: str | None) -> str:
    """The IDENTITY of the rail in a rejection reason, without the numbers that
    change every tick — "cooldown after loss (2.9h of 24.0h)" and the same rail
    3.0h later share a signature, but a different rail never does.

    Returns "" when the text names no rail at all (an execution-side rejection
    such as "did not fill", or a hand-written reason). An empty signature never
    matches anything, so those rows are never suppressed and never suppress.
    """
    text = entry_reason or ""
    marker = "rail: "
    idx = text.find(marker)
    if idx == -1:
        return ""
    return text[idx + len(marker) :].split("(")[0].strip()


def _rail_rejection_is_new(
    session: Session, mode: str, strategy_id: int, symbol: str, rails_reason: str
) -> bool:
    """Should this rail rejection be journalled, or is it the same block we
    already recorded a moment ago? True = write the row."""
    signature = _rail_signature(rails_reason)
    if not signature:
        return True  # not a rail we recognise — never suppress
    latest = (
        session.query(Trade)
        .filter(
            Trade.mode == mode,
            Trade.strategy_id == strategy_id,
            Trade.symbol == symbol,
            Trade.status == "rejected",
        )
        .order_by(Trade.created_at.desc())
        .first()
    )
    if latest is None or _rail_signature(latest.entry_reason) != signature:
        return True  # a different rail (or none yet) — this is a state change
    created = latest.created_at
    if created is None:
        return True
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - created) >= timedelta(
        minutes=RAIL_REJECTION_REPEAT_MINUTES
    )


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
    # "Already open for this symbol" — account-wide by default, so whoever gets
    # there first owns the name. A strategy can opt out of that and take its own
    # position in a symbol ANOTHER strategy holds; it is still blocked from
    # stacking a second position on its OWN (that's scaling-in, not this).
    #
    # Note this only relaxes the position rail. The wash-sale guard and the
    # loss cooldown below stay portfolio-wide whatever this is set to: they
    # protect the ACCOUNT, and the IRS counts the account, not your strategies.
    symbol_q = open_q.filter(Trade.symbol == symbol)
    if strategy.allow_concurrent_symbol:
        symbol_q = symbol_q.filter(Trade.strategy_id == strategy.id)
    already_open = symbol_q.count() > 0
    # The most recent LOSING exit on this symbol, and which strategy took it.
    # Deliberately NOT filtered by strategy — see the account-wide note above.
    # The owner is fetched with it purely so the rejection can name them.
    loss_row = (
        session.query(Trade.exit_at, Trade.strategy_id)
        .filter(
            Trade.mode == mode, Trade.symbol == symbol, Trade.status == "closed",
            Trade.pnl < 0, Trade.exit_at.isnot(None),
        )
        .order_by(Trade.exit_at.desc())
        .first()
    )
    last_loss, last_loss_by = (loss_row[0], None) if loss_row else (None, None)
    if loss_row is not None:
        if loss_row[1] == strategy.id:
            last_loss_by = "this strategy"
        else:
            owner = session.get(Strategy, loss_row[1]) if loss_row[1] else None
            last_loss_by = f"“{owner.name}”" if owner else "a strategy since deleted"
    if last_loss is not None and last_loss.tzinfo is None:
        last_loss = last_loss.replace(tzinfo=timezone.utc)
    wash = False
    if strategy.asset_class == "stock":
        cutoff = datetime.now(timezone.utc) - timedelta(days=31)
        # Filtered by MODE, like every other query here. Shadow mode places no
        # real orders, so a shadow "loss" is a simulated sale that the IRS has
        # never heard of — it must not block a real paper entry. This was the one
        # query in this function without the filter, so weeks of shadow-mode
        # journal could silently wash-sale-block live entries for 31 days.
        wash = (
            session.query(func.count(Trade.id))
            .filter(
                Trade.mode == mode,
                Trade.symbol == symbol, Trade.asset_class == "stock",
                Trade.status == "closed", Trade.pnl < 0, Trade.exit_at >= cutoff,
            )
            .scalar()
            > 0
        )
    # The non-fill streak, newest first: count "did not fill" rejections until a
    # real trade breaks the run. Other rejections (rails) are skipped rather than
    # counted or treated as a reset — they mean we never placed an order at all.
    strikes, last_nonfill = 0, None
    # Rail rejections are excluded IN THE QUERY, not skipped in the loop. The
    # cooling-off rail writes a rejected row every single cycle it blocks, so at
    # a 60s tick its own rows filled the 50-row window in under an hour and
    # evicted the "did not fill" rows that justified the cooldown — the breaker
    # quietly released itself after ~50 minutes and the 12h cap was unreachable.
    # A symbol re-evaluated round the clock (i.e. any crypto pair, i.e. exactly
    # the RENDER/USD case this was built for) defeated it fastest.
    recent = (
        session.query(Trade)
        .filter(
            Trade.mode == mode, Trade.symbol == symbol,
            Trade.created_at >= datetime.now(timezone.utc) - timedelta(days=7),
            or_(
                Trade.status.in_(("open", "closed")),
                Trade.entry_reason.like("%did not fill%"),
            ),
        )
        .order_by(Trade.created_at.desc())
        .limit(50)
        .all()
    )
    for t in recent:
        if t.status in ("open", "closed"):
            break  # it filled — whatever came before is history
        if t.status == "rejected" and "did not fill" in (t.entry_reason or ""):
            strikes += 1
            if last_nonfill is None:
                last_nonfill = t.created_at
    if last_nonfill is not None and last_nonfill.tzinfo is None:
        last_nonfill = last_nonfill.replace(tzinfo=timezone.utc)
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
        nonfill_strikes=strikes,
        last_nonfill_at=last_nonfill,
        last_loss_by=last_loss_by,
    )


def _latest_version_id(session: Session, strategy_id: int) -> int | None:
    row = (
        session.query(StrategyConfigVersion.id)
        .filter(StrategyConfigVersion.strategy_id == strategy_id)
        .order_by(StrategyConfigVersion.version_no.desc())
        .first()
    )
    return row[0] if row else None
