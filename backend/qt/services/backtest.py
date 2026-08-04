"""Minimal backtester (Phase 2.5): replay a strategy config over historical
bars using the SAME pure decision functions the live engine runs
(evaluate_entry / evaluate_exit / check_rails) — the backtest can't drift
from reality because there is only one implementation of the rules.

Honest limitations, surfaced in the UI:
- It replays a FIXED symbol list, not the scanner's historical daily picks
  (Alpaca has no historical movers endpoint). It validates your entry/exit
  rules and risk rails, not the scanner.
- Fills are modeled as bar close ± a configurable spread cost per side.
- Exits are judged against what the LIVE engine could have seen. It polls every
  60 seconds and sells at market — it places no resting stop or limit orders — so
  on bars that size or finer the replay judges and fills on the CLOSE (one look
  per bar, same as live). On coarser bars, where a 60-second poller would have
  sampled a breach many times over, stops stay judged on the bar's high/low and
  fill at the trigger level. See _apply_poller_view.
- Free IEX data; past performance predicts nothing.
"""

import os
from bisect import bisect_left
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from math import floor, log10
from zoneinfo import ZoneInfo

from qt.broker.alpaca import AlpacaClient
from qt.services import ranking, stats
from qt.services.engine import (
    RSI_PERIOD,
    Candidate,
    RailContext,
    _rank_note,
    atr_position_size,
    check_rails,
    evaluate_entry,
    evaluate_exit,
)

ET = ZoneInfo("America/New_York")


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


def _btlog(
    day: str,
    symbol: str,
    bar: dict,
    params: dict,
    decision: str,
    sink: list[str] | None = None,
) -> None:
    """One compact per-candidate diagnostic line: the day-gain, the MACD state
    (bull/bear/none + raw line vs signal), RSI, and the exact accept/reject/rail
    decision — so a 'why didn't this bar trade' question is answerable.

    Two destinations, because they answer different questions. QT_BACKTEST_DEBUG
    prints to the container log, which is where you look when a scheduled run
    misbehaves and nobody was watching. `sink` collects the same lines onto the
    response, which is where you look when you are holding a specific
    disagreement and need the per-bar verdict for one symbol between two
    timestamps — the case this was added for: AMZN qualified live at 14:01 and
    the replay did not enter until 14:26, with every named entry condition
    apparently satisfied at both instants."""
    chg = bar.get("change_pct")
    chg_txt = "  n/a" if chg is None else f"{chg:+.2f}%"
    macd = bar.get("macd_raw")
    if not _macd_on(params):
        macd_txt = "off"
    elif macd is None:
        macd_txt = "None(warmup)"
    else:
        macd_txt = f"{'BULL' if bar.get('macd_bullish') else 'BEAR'}(l={macd[0]:+.4f} s={macd[1]:+.4f})"
    rsi = bar.get("rsi")
    rsi_txt = "" if rsi is None else f" rsi={rsi:.1f}"
    # `ts` is what _prepare puts on a prepared bar (a real datetime); `t` is the
    # raw Alpaca key, accepted so this can be called on either shape.
    at = bar.get("ts") or bar.get("t")
    # The bar's own instant, not just the day. A per-bar log keyed only by day is
    # useless on a 1-minute replay, where one day is 390 lines per symbol and the
    # whole question is WHICH minute the verdict changed on.
    stamp = at.isoformat() if hasattr(at, "isoformat") else (str(at) if at else day)
    line = (
        f"BTDBG {stamp} {symbol:<6} close={bar['close']:.2f} chg={chg_txt} "
        f"macd={macd_txt}{rsi_txt} -> {decision}"
    )
    if sink is not None:
        sink.append(line)
    else:
        print(line, flush=True)


# How often the LIVE engine looks at a price. The scheduler runs the engine tick
# every 60 seconds (qt/main.py: IntervalTrigger(seconds=60)), and each tick reads
# one snapshot per open symbol and sells AT MARKET if a rule fires — QT places no
# resting stop or limit orders. So the live engine's view of the tape is a
# sequence of 60-second samples, not the continuous tape.
class _AccountBackdrop:
    """Positions the ACCOUNT held that this replay cannot know about.

    `check_rails` splits its inputs in two: `open_positions_strategy` and
    `open_exposure_strategy_usd` are this strategy's, while
    `open_positions_total`, `open_exposure_usd` and `already_open_symbol` are the
    whole ACCOUNT's — "position already open for this symbol (any strategy)" is
    the rail's own wording. The replay fed its own numbers into both halves,
    because a simulation of one strategy has no idea what the other sixteen were
    holding. That makes it strictly FREER than live, and a replay that is freer
    than live buys things live refused and the report files them as trades the
    backtest invented.

    Two populations belong here, and neither can overlap what the replay holds
    itself, so there is no double-counting:
      * positions OTHER strategies held, and
      * positions THIS strategy already held when the window opened — the replay
        starts flat, so live's already-open rail could not block it.

    Deliberately NOT a general "account state" object. It seeds facts the replay
    could not derive, which is the same licence `prior_loss_at` already takes;
    it must never seed a DECISION, or the comparison starts agreeing with live
    by construction and stops measuring anything."""

    __slots__ = ("_spans",)

    def __init__(self, positions: list[dict] | None):
        self._spans: list[tuple[str, datetime, datetime | None, float]] = []
        for p in positions or []:
            start = p.get("from")
            if start is None:
                continue
            self._spans.append((
                str(p.get("symbol") or ""),
                _as_utc(start),
                _as_utc(p["to"]) if p.get("to") else None,
                float(p.get("notional") or 0.0),
            ))

    def at(self, ts: datetime) -> tuple[int, float, set[str]]:
        """(count, exposure_usd, symbols) held by the rest of the account at `ts`.
        Half-open [from, to): a position closed at 14:26 no longer blocks a
        candidate evaluated on the 14:26 bar, matching how live would see it once
        the sell has settled."""
        count, exposure, symbols = 0, 0.0, set()
        for symbol, start, end, notional in self._spans:
            if start <= ts and (end is None or ts < end):
                count += 1
                exposure += notional
                symbols.add(symbol)
        return count, exposure, symbols


def _as_utc(value) -> datetime:
    """Accept an ISO string or a datetime; naive input is treated as UTC, which
    is what every stored instant in this project already is."""
    dt = value if isinstance(value, datetime) else datetime.fromisoformat(
        str(value).replace("Z", "+00:00")
    )
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _daily_signal_report(
    params: dict, daily_bars_by_symbol: dict | None, bar_seconds: float | None
) -> dict | None:
    """Which bars the daily-style indicators were actually computed from.

    The live engine reads MACD, RSI and ATR off COMPLETED DAILY closes, always.
    A replay can only match that if it was handed a daily series; without one it
    computes them from its own bars, and on a fine timeframe that is a different
    indicator wearing the same name. A 1-minute MACD crosses when a minute of
    tape turns, a daily MACD when a day does.

    This was silent, and it cost a whole session's investigation: strategy 25's
    replay entered AMZN 25 minutes after the engine did, and every named entry
    condition looked satisfied at both instants. The per-bar log showed the MACD
    line moving every minute — a daily MACD is one value per day. Nothing in the
    response said which one had been used, so the numbers looked comparable and
    were not.

    None when the strategy uses no daily-style indicator: there is nothing to
    caveat, and a caveat on every result is a caveat nobody reads."""
    names = [n for n, on in (
        ("MACD", _macd_on(params)), ("RSI", _rsi_on(params)), ("ATR", _atr_on(params)),
    ) if on]
    if not names:
        return None
    from_daily = bool(daily_bars_by_symbol)
    intraday = bar_seconds is not None and bar_seconds < 86_400
    listed = " and ".join(names) if len(names) < 3 else "MACD, RSI and ATR"
    report = {
        "indicators": names,
        "computed_from": "daily bars" if from_daily else "the replay's own bars",
        "matches_live": bool(from_daily or not intraday),
        "warning": None,
    }
    if not from_daily and intraday:
        every = (
            f"{int(bar_seconds // 60)}-minute" if bar_seconds < 3600
            else f"{int(bar_seconds // 3600)}-hour"
        )
        report["warning"] = (
            f"{listed} came from this replay's own {every} bars, NOT from daily bars."
            f" The live engine always reads {listed} off completed DAILY closes, so"
            f" this run tested a {every} {names[0]} — a different signal that turns"
            " at different times. Entry and exit timings here are not comparable to"
            " live, and neither is any conclusion about whether the rule works."
        )
    return report


LIVE_POLL_SECONDS = 60

# Ceiling on the per-bar debug log returned to a caller. A 1-minute replay of a
# ten-name universe over one session is ~20,000 lines; this keeps a diagnostic
# response from becoming a download while staying far above the few hundred
# lines any single question actually needs.
DEBUG_LOG_MAX_LINES = 5000


def _bar_seconds(timeline: list[datetime]) -> float | None:
    """The replay's bar size, read off the bars themselves rather than a label.

    The MEDIAN gap between consecutive timestamps, not the minimum or the mean:
    the minimum would be dragged down by one stray duplicate-ish timestamp (which
    would wrongly declare a coarse series fine, the one error that neuters a stop),
    and the mean is pulled up by overnight and weekend gaps. On any real series the
    in-session gaps outnumber the day boundaries by an order of magnitude, so the
    median is the bar size exactly: 60 for 1-minute bars, 86400 for daily ones.

    Measured rather than taken from the request's timeframe label on purpose, and
    it is not just defensiveness: the question being asked is "how many polls fit
    inside one bar", and a sparse series answers it honestly. A thin name whose
    1-minute bars actually arrive every two or three minutes really did span two
    or three polls, so it keeps the intra-bar model — which the label "1Min" would
    have talked us out of. The timeline is the union across every symbol in the
    run, so one thin name cannot drag a busy comparison off its true resolution.

    None when there is nothing to measure (fewer than two distinct timestamps) —
    callers must then assume the COARSE behaviour, because "I don't know" must
    never be the answer that makes a stop lenient."""
    deltas = sorted(
        (b - a).total_seconds() for a, b in zip(timeline, timeline[1:]) if b > a
    )
    if not deltas:
        return None
    return deltas[len(deltas) // 2]


def _median_bar_move_pct(prepared: dict[str, list[dict]]) -> float | None:
    """The typical size of one bar's move, as a % — the median of |close-to-close|
    across every symbol in the run.

    This is the SCALE OF THE PHASE ERROR, and it exists so a fidelity report can
    say where its own floor is. The replay samples at bar closes (:00). The live
    engine ticks on a 60-second interval that started whenever the process did, so
    it samples at :17, :18, :44 — an arbitrary, unknowable, unrecoverable offset.
    Even with a perfect exit model and perfect data, the two are looking at
    instants up to one bar apart, and one bar apart is worth about this much.

    It is not a bound on any single trade: a threshold race decided differently by
    the two sampling grids (live takes the stop, the replay takes the take-profit
    a moment later) separates by the distance between two RULES, not by one bar's
    move. It is the right order of magnitude for the residual, and its purpose is
    to stop the chase — no finer bars fix this, because Alpaca's finest bar IS one
    minute and the missing information is the phase of a clock nobody recorded.
    Only tick data would, and QT does not have it.

    None when there is nothing to measure."""
    moves = [
        abs(b["close"] / a["close"] - 1) * 100
        for series in prepared.values()
        for a, b in zip(series, series[1:])
        if a["close"]
    ]
    if not moves:
        return None
    moves.sort()
    return round(moves[len(moves) // 2], 4)


def _apply_poller_view(prepared: dict[str, list[dict]], bar_seconds: float | None) -> bool:
    """Collapse each bar to what a 60-second POLLER could have seen, when the bars
    are no coarser than the poll itself. Returns whether it did.

    WHY. The replay judges stops against a bar's intra-bar high/low and fills at
    the trigger level — the right model for a resting stop/limit order, and the
    right model for any bar long enough that a 60-second poller would have sampled
    the breach. It is the WRONG model at 1-minute resolution, where the poller gets
    exactly ONE look per bar: a price that was touched at 10:07:20 and gone by
    10:07:40 was never available to the live engine, so scoring an exit on it books
    a fill nobody could have got. Measured on a real 2-hour minute-bar comparison
    this ran the replay's exits 0.73% better than reality, three of the four
    residual mismatches being a take-profit just above the threshold that live
    never got.

    HOW. Flatten high and low onto the close. Everything downstream then follows
    without a single conditional: `high_water` advances close-to-close (exactly
    what the live engine does with the polled price), `evaluate_exit` sees
    high == low == price (exactly the arguments live passes), and `_fill_price`
    clamps the trigger level into a zero-width range, i.e. fills at the close —
    which is what selling at market on the poll that noticed actually gets you.

    WHERE IT STOPS. Only when bar_seconds <= LIVE_POLL_SECONDS. On 15-minute bars
    the poller took fifteen looks inside each bar and a stop genuinely breached
    would have been seen; on daily bars, hundreds. Ignoring those would be a worse
    lie than the one being fixed, so coarse bars keep the intra-bar model. An
    unknown bar size (None) is treated as coarse for the same reason."""
    if bar_seconds is None or bar_seconds > LIVE_POLL_SECONDS:
        return False
    for series in prepared.values():
        for bar in series:
            bar["high"] = bar["low"] = bar["close"]
    return True


def _price(v: float | None) -> float | None:
    """A price rounded for OUTPUT, with the precision it actually has.

    Four decimals everywhere was fine for a stock and destroyed a coin. SHIB/USD
    trades near $0.00001, and `round(0.0000123, 4)` is `0.0` — so a serialized
    trade carried a zero entry price, and qt.services.fidelity._pct_delta then
    computed (0 - live)/live = -100% and fed that into `median_entry_delta_pct`,
    `measured_cost_per_side_pct` and `suggested_spread_pct`. Those are the numbers
    the fidelity report exists to produce, and the last of them is copied straight
    into the backtest's spread setting: a measuring instrument was corrupting its
    own measurement, and only the median's robustness kept it from being obvious.

    Rounding is NOT the enemy and is deliberately kept — it is what stops the UI
    printing fourteen digits of float noise (see fidelity.compare). So: four
    decimals from a dollar up, exactly as before, and SIX SIGNIFICANT FIGURES
    below a dollar, which is the same discipline engine._money applies with its
    2/4/6 ladder, carried far enough down to survive a sub-cent asset.

    Only ever applied on the way OUT. Every P&L, fill, fee and equity figure in
    this module is computed from the unrounded value and is unaffected."""
    if v is None:
        return None
    if v == 0 or abs(v) >= 1:
        return round(v, 4)
    return round(v, 5 - floor(log10(abs(v))))


def _fill_price(trigger: dict, bar: dict) -> float:
    """Where a price-triggered exit actually filled.

    A stop breached mid-bar fills at the STOP LEVEL, not the bar's close —
    filling at the close after the price dipped through and recovered books a
    recovery you never got, which is the flattering direction. Clamped into the
    bar's own range so a gap straight through the level can't fill better than
    anything that traded: if the whole bar is below the stop, you get the high,
    not the untouched stop price. Non-price exits (MACD, RSI, time, flatten)
    supply no level and fill at the close as before."""
    level = trigger.get("exit_price")
    if level is None:
        return bar["close"]
    return min(max(level, bar["low"]), bar["high"])


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


def _daily_frontier(
    series: list[dict], daily_bars: list[dict], day_of
) -> tuple[list[dict], dict[str, int]]:
    """Mixed-resolution plumbing, and the ONE place the look-ahead rule is
    enforced. Returns (a) the DAILY series collapsed to one bar per calendar day,
    oldest-first, and (b) for every day the INTRADAY series touches, how many
    daily bars precede it.

    THE RULE: `bisect_left(days, d)` counts the daily bars STRICTLY BEFORE day d
    (ISO day strings sort lexicographically == chronologically). So an indicator
    read on any intraday bar of day D is derived from daily closes through D−1
    only — day D's own daily close, which live hasn't happened yet while the day
    is still in progress, can never enter the calculation. That is exactly the
    `closes[:i]` semantic the daily-only annotators use, carried onto an intraday
    replay: one frontier per day, shared by every bar of that day.

    A symbol with no daily bars yields an empty series and a 0 cutoff for every
    day → the indicator is None everywhere (never crash, never fall back to
    intraday-derived values)."""
    by_day: dict[str, dict] = {}
    for b in daily_bars:
        by_day[day_of(_parse_ts(b["t"]))] = b  # defensive: last bar of a day wins
    days = sorted(by_day)
    ordered = [by_day[d] for d in days]
    cutoff = {d: bisect_left(days, d) for d in {b["day"] for b in series}}
    return ordered, cutoff


def _annotate_atr(
    prepared: dict[str, list[dict]],
    bars_by_symbol: dict[str, list[dict]],
    params: dict,
    daily_bars_by_symbol: dict[str, list[dict]] | None = None,
    day_of=_et_day,
) -> None:
    """Attach `atr_pct` to each prepared bar, in place, when the strategy opts
    into ATR. The value at bar i is computed from the raw OHLC bars up to and
    INCLUDING the prior completed bar (raw[:i]) — never the current bar, so there
    is NO look-ahead — mirroring _annotate_macd. The prepared series and the raw
    bars are index-aligned (1:1, same order), so raw[:i] is exactly the completed
    history at prepared bar i. No-op when ATR is off, keeping non-ATR backtests
    byte-identical.

    MIXED RESOLUTION: with `daily_bars_by_symbol`, the true range comes from the
    DAILY bars completed before the bar's own day (see _daily_frontier) while the
    price denominator stays the intraday close — exactly what the live engine
    does (completed daily bars, current price)."""
    if not _atr_on(params):
        return
    period = int((params.get("atr") or {}).get("period", 14) or 14)
    if daily_bars_by_symbol is not None:
        for symbol, series in prepared.items():
            ordered, cutoff = _daily_frontier(series, daily_bars_by_symbol.get(symbol) or [], day_of)
            history = {d: ordered[:k] for d, k in cutoff.items()}  # sliced once per day
            for bar in series:
                bar["atr_pct"] = stats.atr_pct(
                    history[bar["day"]], period, current_price=bar["close"]
                )
        return
    for symbol, series in prepared.items():
        raw = bars_by_symbol.get(symbol) or []
        for i, bar in enumerate(series):
            bar["atr_pct"] = stats.atr_pct(raw[:i], period, current_price=bar["close"])


def _annotate_macd(
    prepared: dict[str, list[dict]],
    params: dict,
    daily_bars_by_symbol: dict[str, list[dict]] | None = None,
    day_of=_et_day,
) -> None:
    """Attach `macd_bullish` to each prepared bar, in place, when the strategy
    opts into MACD. The value at bar i is computed from the replayed closes up to
    and INCLUDING the prior completed bar (closes[:i]) — never the current bar,
    so there is NO look-ahead. No-op when MACD is off, keeping non-MACD backtests
    byte-identical.

    NUANCE: the LIVE engine always computes MACD from DAILY bars, whereas here we
    use the backtest's OWN timeframe bars. For the intended daily/swing use
    (1Day, or 1Hour where a daily MACD and an hourly replay track closely enough)
    this matches the live behaviour; on much finer timeframes the two would
    diverge, which is why MACD is documented as a daily/swing signal.

    MIXED RESOLUTION: with `daily_bars_by_symbol` the nuance above disappears —
    the MACD comes from the DAILY closes completed before each intraday bar's own
    day (see _daily_frontier), exactly like live, while the replay itself runs on
    the intraday bars so price-triggered exits are real."""
    if not _macd_on(params):
        return
    m = params.get("macd") or {}
    fast, slow, signal = int(m.get("fast", 12)), int(m.get("slow", 26)), int(m.get("signal", 9))
    if daily_bars_by_symbol is not None:
        for symbol, series in prepared.items():
            ordered, cutoff = _daily_frontier(series, daily_bars_by_symbol.get(symbol) or [], day_of)
            closes = [float(b["c"]) for b in ordered]
            # One macd() per DAY (the frontier is shared by that day's bars).
            by_day = {d: stats.macd(closes[:k], fast, slow, signal) for d, k in cutoff.items()}
            for bar in series:
                m = by_day[bar["day"]]
                bar["macd_raw"] = m
                bar["macd_bullish"] = None if m is None else (m[0] > m[1])
        return
    for series in prepared.values():
        closes = [b["close"] for b in series]
        for i, bar in enumerate(series):
            # One macd() call (bullish is just line > signal), keeping the raw
            # (line, signal, hist) tuple around for QT_BACKTEST_DEBUG logging.
            m = stats.macd(closes[:i], fast, slow, signal)
            bar["macd_raw"] = m
            bar["macd_bullish"] = None if m is None else (m[0] > m[1])


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


def _annotate_rsi(
    prepared: dict[str, list[dict]],
    params: dict,
    daily_bars_by_symbol: dict[str, list[dict]] | None = None,
    day_of=_et_day,
) -> None:
    """Attach `rsi` (Wilder 14) to each prepared bar, in place, when the strategy
    opts into an RSI rule. The value at bar i uses the replayed closes up to and
    INCLUDING the prior completed bar (closes[:i]) — no look-ahead — mirroring
    _annotate_macd. No-op when RSI is off, keeping non-RSI backtests byte-
    identical. Same daily/swing timeframe caveat as MACD — and the same
    MIXED-RESOLUTION escape from it (daily closes completed before the bar's own
    day; see _daily_frontier)."""
    if not _rsi_on(params):
        return
    if daily_bars_by_symbol is not None:
        for symbol, series in prepared.items():
            ordered, cutoff = _daily_frontier(series, daily_bars_by_symbol.get(symbol) or [], day_of)
            closes = [float(b["c"]) for b in ordered]
            by_day = {d: stats.rsi_from_closes(closes[:k]) for d, k in cutoff.items()}
            for bar in series:
                bar["rsi"] = by_day[bar["day"]]
        return
    for series in prepared.values():
        closes = [b["close"] for b in series]
        for i, bar in enumerate(series):
            bar["rsi"] = stats.rsi_from_closes(closes[:i])


# ───────────────────────────── TOP-N RANKING ──────────────────────────────
#
# WHAT WAS WRONG. The live engine never evaluates a whole pool. It ranks the
# strategy's symbols by `rank_by` and hands the entry rules only the top `top_n`
# (engine._ranked_candidates → engine._pool_metrics → ranking.rank_symbols). The
# replay ranked nothing, so it evaluated every symbol it was given — including
# names live would not have looked at. Measured on a 24-pair crypto basket with
# top_n=10, the replay bought three coins ranked #13, #17 and #19 live. That is
# not only an instrument fault: EVERY backtest and EVERY optimizer run of a
# ranked strategy was drawing from a wider pool than live, so results were
# systematically too permissive — more candidates, more entries, more chances to
# stumble into a winner — and nothing on screen said so.
#
# WHEN IT RANKS. Every evaluation point, exactly like live's 60-second tick, so
# the top-N MEMBERSHIP MOVES THROUGH THE DAY. Ranking once for the window would
# be a different wrong answer, not a fix.
#
# WHICH UNIVERSES. The same rule the API enforces (qt.api.strategies.StrategyBody
# ._sanity): a basket is ALWAYS ranked, watchlist/custom opt in via rank_enabled,
# and scanner/both never rank because the scanner already ordered them — their
# replay-side equivalent is `eligible_by_day`, which is a different mechanism for
# a different universe and is left completely alone. Both gates apply if a caller
# ever passes both; each only narrows.

# The metrics live derives from DAILY bars. momentum_today is the only one that
# rides on the snapshot alone — and the replay already holds it: `change_pct` on
# a prepared bar IS the number the snapshot carries (previous session close for
# stocks, the hour-quantised rolling 24h reference for crypto).
DAILY_RANK_METRICS = ("return_30d", "relative_strength", "rs_vs_spy", "rsi")

# How much daily history each metric is computed from — copied from live's own
# fetch window (engine._pool_metrics: 320 days for the two 200-day-average
# metrics, 60 for the rest) rather than "however much history the replay happens
# to hold". That is a fidelity requirement, not an optimisation: Wilder's RSI
# smoothing has unbounded memory, so the same closes over 300 days and over 60
# give different numbers, and live only ever sees 60. It happens to also be what
# keeps the cost bounded — see the note on _PoolRanker.
RANK_LOOKBACK_DAYS = {
    "return_30d": 60,
    "rsi": 60,
    "relative_strength": 320,
    "rs_vs_spy": 320,
}
_RETURN_30D_DAYS = 30       # engine._pool_metrics: stats.pct_change_over(bars, 30, …)
_SMA_PERIOD = 200           # engine._pool_metrics: stats.vs_sma_pct(bars, 200, …)
_RS_VS_SPY_DAYS = 90        # engine._pool_metrics.RS_VS_SPY_WINDOW

# A replay whose own bars ARE the daily series can be its own ranking source.
# Measured off the timeline (see _bar_seconds), tolerant of the odd short day.
_DAILY_BAR_SECONDS = 82800  # 23h


def ranking_config(strategy: dict) -> tuple[str, int] | None:
    """(rank_by, top_n) when this config's universe is one the LIVE engine ranks,
    else None. Mirrors qt.api.strategies.StrategyBody._sanity, which is what
    forces rank_enabled ON for a basket and OFF for scanner/both — so a strategy
    row saved with rank_enabled=False and universe="basket" still ranks, here as
    it does live.

    Defaults rather than None for a config snapshot saved before these columns
    existed: falling through to "don't rank" would be the old bug wearing the hat
    of backwards compatibility."""
    if not (strategy.get("rank_enabled") or strategy.get("universe") == "basket"):
        return None
    rank_by = strategy.get("rank_by") or "momentum_today"
    top_n = int(strategy.get("top_n") or 10)
    if rank_by not in ranking.RANK_METRICS or top_n <= 0:
        return None
    return rank_by, top_n


class _PoolRanker:
    """Live's candidate cut, reproduced per evaluation point.

    COST, measured rather than assumed. Ranking is O(pool) per bar plus one sort.
    The daily-bar metrics call the same qt.services.stats functions the engine
    calls, over a per-(symbol, day) slice of daily history built ONCE in __init__
    and trimmed to live's own lookback — the trim is worth a factor of five on
    its own (81µs for rsi over 320 daily bars, 15µs over 60).

    On the heaviest run the API can produce — a 50-symbol 15-minute replay over
    180 days, 864k bars, 17,280 evaluation points — against a 9.3s unranked
    baseline: momentum_today 7.6s, relative_strength 17.3s, rsi 21.8s,
    return_30d 23.9s. On a real 24-pair basket (414k bars, 4.3s unranked):
    3.9s / 8.6s / 10.6s / 11.7s.

    momentum_today — the default, and the metric a basket almost always uses —
    comes out FASTER than not ranking at all, because cutting 24 candidates to 10
    removes more evaluate_entry calls than the ranking costs. The daily metrics
    add seconds, not minutes, to runs that are already tens of seconds. No fast
    path was hand-written for them: a second implementation of Wilder's smoothing
    that drifted from stats.rsi_from_closes would cost far more than it saved.

    LOOK-AHEAD. The daily prefix comes from _daily_frontier, so a bar on day D
    reads daily closes through D−1 only. The in-progress day is appended as a
    synthetic bar carrying the CURRENT price, which is precisely the shape live
    sees: its daily fetch returns today's partial bar and it passes the live
    quote as `current_price` on top.

    MISSING DATA AND TIES are not improvised — ranking.rank_symbols is the one
    implementation, shared with the engine: a symbol whose metric is None is
    dropped (you cannot rank on data you don't have) and ties break on symbol
    ascending. momentum_today is rounded to 2dp before ranking because live
    rounds it in _pool_metrics, and rounding is what decides which near-ties fall
    through to the symbol tie-break."""

    def __init__(
        self,
        rank_by: str,
        top_n: int,
        prepared: dict[str, list[dict]],
        daily_bars_by_symbol: dict[str, list[dict]] | None,
        day_of,
        benchmark: str = "SPY",
    ) -> None:
        self.rank_by = rank_by
        self.top_n = top_n
        self.applied = True
        self.warning: str | None = None
        self.pool_size = len(prepared)
        self.evaluated = 0      # symbol-bars offered to the ranking
        self.excluded = 0       # …cut by the top-N or unrankable
        self.unrankable = 0     # …whose metric could not be computed at all
        # WHICH symbols those were. The count alone cannot answer the question a
        # 0-trade result raises — "did the ranking quietly make some name (or
        # every name) permanently un-buyable?" — because a symbol whose metric is
        # never computable is dropped by ranking.rank_symbols on every bar and so
        # can never be entered, silently, for the whole run. See report().
        self._rankable: set[str] = set()
        self._unrankable: set[str] = set()
        self._prefix: dict[str, dict[str, list[dict]]] = {}
        self._spy: dict[str, float | None] = {}
        self._spy_missing = False
        if rank_by not in DAILY_RANK_METRICS:
            return

        daily = daily_bars_by_symbol or {}
        if not any(daily.get(symbol) for symbol in prepared):
            # NOT a silent fall-back to no ranking — that is the bug this class
            # exists to close. The replay says so in its result instead.
            self.applied = False
            self.warning = (
                f"This strategy ranks its pool by {rank_by.replace('_', ' ')}, which the live "
                "engine computes from daily bars — and none were available to this replay. It "
                "therefore evaluated the WHOLE pool, where live would have evaluated only the "
                f"top {top_n}, so it had more candidates to choose from than live ever did."
            )
            return

        lookback = timedelta(days=RANK_LOOKBACK_DAYS[rank_by])
        # One reference instant per day (its first replayed bar) for the lookback
        # trim. Day granularity is enough: the trim edge sits 60 or 320 days back,
        # far outside the 30/90/200-bar windows the metrics actually read from.
        day_start: dict[str, datetime] = {}
        for series in prepared.values():
            for bar in series:
                d = bar["day"]
                if d not in day_start or bar["ts"] < day_start[d]:
                    day_start[d] = bar["ts"]
        for symbol, series in prepared.items():
            ordered, cutoff = _daily_frontier(series, daily.get(symbol) or [], day_of)
            times = [_parse_ts(b["t"]) for b in ordered]
            self._prefix[symbol] = {
                d: ordered[bisect_left(times, day_start[d] - lookback, 0, k):k]
                for d, k in cutoff.items()
            }

        if rank_by == "rs_vs_spy":
            spy_bars = daily.get(benchmark) or []
            # SPY's own return over the window is SUBTRACTED FROM EVERY MEMBER, so
            # it shifts all the values by one constant and cannot change the ORDER
            # — which is the only thing eligibility depends on. Without the
            # benchmark the cut is therefore still exactly live's; only the printed
            # values differ, and the result says so rather than pretending.
            self._spy_missing = not spy_bars
            if spy_bars:
                ordered, cutoff = _daily_frontier(
                    [{"day": d} for d in day_start], spy_bars, day_of
                )
                for d, k in cutoff.items():
                    window = ordered[:k]
                    self._spy[d] = (
                        stats.pct_change_over(
                            window, _RS_VS_SPY_DAYS, float(window[-1]["c"])
                        )
                        if window
                        else None
                    )
            else:
                self.warning = (
                    "Ranking by relative strength vs S&P 500 without SPY's daily bars: the "
                    "top-N cut is still live's (SPY's return is common to every member, so it "
                    "cannot reorder them), but the values shown are the member's own 90-day "
                    "return rather than its out-performance."
                )

    def _value(self, symbol: str, bar: dict, ts: datetime) -> float | None:
        if self.rank_by == "momentum_today":
            change = bar["change_pct"]
            # 2dp because live rounds here (engine._pool_metrics), and the rounding
            # is what sends near-ties to the symbol tie-break instead of ordering
            # them on noise.
            return None if change is None else round(change, 2)
        prefix = self._prefix.get(symbol, {}).get(bar["day"]) or []
        if not prefix:
            return None
        price = bar["close"]
        if self.rank_by == "relative_strength":
            return stats.vs_sma_pct(prefix + [{"c": price}], _SMA_PERIOD, price)
        if self.rank_by == "rsi":
            return stats.rsi(prefix + [{"c": price}], RSI_PERIOD, price)
        # The two window-return metrics read the synthetic bar's TIMESTAMP (it is
        # what pct_change_over measures its window back from), so it carries one.
        window = prefix + [{"t": ts.strftime("%Y-%m-%dT%H:%M:%SZ"), "c": price}]
        if self.rank_by == "return_30d":
            return stats.pct_change_over(window, _RETURN_30D_DAYS, price)
        mine = stats.pct_change_over(window, _RS_VS_SPY_DAYS, price)
        spy = 0.0 if self._spy_missing else self._spy.get(bar["day"])
        if mine is None or spy is None:
            return None
        return round(mine - spy, 2)

    def rank(self, bars: dict[str, dict], ts: datetime) -> list[tuple[str, int, int]]:
        """This instant's top-N as (symbol, rank, ranked_of), BEST FIRST.

        The order is load-bearing, not cosmetic: live walks its candidates
        strictly best-first, so the strongest name gets first refusal on the
        sleeve and a max_positions cap bites from the bottom of the list. A
        replay that evaluated the same names in symbol order would fill the last
        slot with a different one.

        The pool is the symbols with a bar AT THIS INSTANT. A symbol whose feed
        skipped this bar is treated exactly as live treats a symbol with no
        metric — dropped, and its place taken by the next one down. Every other
        decision in this replay is made off the bars present at `ts`; ranking a
        name the replay cannot then enter would be worse."""
        metrics: dict[str, dict[str, float | None]] = {}
        for symbol, bar in bars.items():
            value = self._value(symbol, bar, ts)
            metrics[symbol] = {self.rank_by: value}
            if value is None:
                self.unrankable += 1
                self._unrankable.add(symbol)
            else:
                self._rankable.add(symbol)
        ranked = ranking.rank_symbols(metrics, self.rank_by, self.top_n)
        self.evaluated += len(bars)
        self.excluded += len(bars) - len(ranked)
        ranked_of = len(ranked)
        return [(symbol, i, ranked_of) for i, (symbol, _v) in enumerate(ranked, start=1)]

    def never_rankable(self) -> list[str]:
        """Symbols whose metric could NEVER be computed — so `rank_symbols`
        dropped them on every bar and they could not be entered once, all run."""
        return sorted(self._unrankable - self._rankable)

    def _unrankable_warning(self) -> str | None:
        """The other half of "say so rather than quietly do less".

        `applied` only goes False when NO symbol has any daily history at all.
        Between that and a clean run sits the case that actually bites: the
        history is present but too SHORT for the metric — relative_strength is a
        200-day average, so a series of 150 daily bars makes every member
        unrankable, `rank_symbols` returns nothing on every bar, and the replay
        takes ZERO trades while reporting `applied: true`. On screen that is
        indistinguishable from "your strategy never triggered", which is the
        wrong conclusion to hand somebody about to change their rules.

        Two shapes, both reported: nothing was EVER rankable (no entry was
        possible at all), and some names were never rankable (those names were
        silently un-buyable while the rest of the pool traded)."""
        if not self.applied or not self.evaluated:
            return None
        metric = self.rank_by.replace("_", " ")
        if not self._rankable:
            return (
                f"NO trade was possible: this strategy ranks its pool by {metric}, and that "
                f"value could not be computed for a single one of its {self.pool_size} symbols "
                "on any bar — so the top-N cut was empty every time and no symbol was ever "
                "offered to the entry rules. This says nothing about your entry rules. The "
                "usual cause is a daily series shorter than the metric needs."
            )
        missing = self.never_rankable()
        if missing:
            shown = ", ".join(missing[:6]) + (f" +{len(missing) - 6} more" if len(missing) > 6 else "")
            return (
                f"{len(missing)} of {self.pool_size} symbols could never be ranked by {metric} "
                f"({shown}), so they were dropped from the top-N cut on every bar and could not "
                "be bought at all in this run. Live would have ranked them if it had their daily "
                "history."
            )
        return None

    def report(self) -> dict:
        """What the run did about ranking, for the result. Reported whether or not
        it worked: "the replay could not reproduce live's ordering" is exactly the
        thing a permissive backtest must not keep to itself."""
        warning = self.warning or self._unrankable_warning()
        return {
            "applied": self.applied,
            "symbols_never_rankable": self.never_rankable(),
            "rank_by": self.rank_by,
            "top_n": self.top_n,
            "pool_size": self.pool_size,
            "metric_source": (
                "the replay's own day-gain"
                if self.rank_by not in DAILY_RANK_METRICS
                else "daily bars"
            ),
            "symbol_bars_ranked": self.evaluated,
            "symbol_bars_cut": self.excluded,
            "symbol_bars_unrankable": self.unrankable,
            "benchmark_missing": self._spy_missing,
            "warning": warning,
        }


def _rank_daily_source(
    explicit: dict[str, list[dict]] | None,
    indicator_daily: dict[str, list[dict]] | None,
    replay_bars: dict[str, list[dict]],
    bar_seconds: float | None,
) -> dict[str, list[dict]] | None:
    """Where a daily-bar ranking metric reads its history from, in preference
    order: a series fetched FOR the ranking (the only one guaranteed to reach
    back over live's full lookback, and the only one that can carry SPY), then
    the mixed-resolution indicator series, and finally the replay's own bars when
    those ARE daily. None when the replay has no daily history at all — reported,
    never quietly ignored."""
    if explicit:
        return explicit
    if indicator_daily:
        return indicator_daily
    if bar_seconds is not None and bar_seconds >= _DAILY_BAR_SECONDS:
        return replay_bars
    return None


@dataclass
class SimTrade:
    symbol: str
    qty: float
    entry_price: float
    entry_at: datetime
    entry_reason: str
    high_water: float
    # Where this symbol placed in its strategy's ranking on the bar it was
    # bought, and how many were ranked — the same pair the live journal stamps
    # ("ranked #10 of 10"), so a fidelity comparison can match on it instead of
    # inferring it. None on an unranked universe.
    rank: int | None = None
    rank_of: int | None = None
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
    "funds": "not enough funds for a full position (no-leverage cap)",
    "sleeve_full": "sleeve budget already fully deployed",
    "cooldown": "in cooldown after a loss",
    "washsale": "wash-sale guard (recent loss on this symbol)",
    "already_open": "already holding this symbol",
    "maxpos": "max open positions reached",
    "traderate": "daily trade-rate limit reached",
    "dailyloss": "daily-loss kill switch tripped",
    "rail": "blocked by a risk rail",
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


def _rail_category(reason: str) -> str:
    """Bucket a check_rails rejection into a specific category so the 'why no
    entry' log names the ACTUAL blocker instead of a generic 'a risk rail'. The
    exposure-cap case is the common one novices hit: per-trade size ≥ equity, so
    after any loss no full position fits and the no-leverage rail blocks every
    trade — surfaced plainly as 'not enough funds'."""
    if "exposure cap" in reason:
        return "funds"
    if "sleeve budget" in reason:
        return "sleeve_full"
    if "cooldown" in reason:
        return "cooldown"
    if "wash-sale" in reason:
        return "washsale"
    if "already open" in reason:
        return "already_open"
    if "max positions" in reason or "max open positions" in reason:
        return "maxpos"
    if "trade-rate" in reason:
        return "traderate"
    if "daily loss" in reason:
        return "dailyloss"
    return "rail"


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


def _no_trade_spans(
    days_index: list[str],
    day_reject: dict[str, "Counter[str]"],
    entries_by_day: dict[str, int],
    action_days: set[str],
    day_reject_syms: dict[str, dict[str, set[str]]] | None = None,
) -> list[dict]:
    """Collapse consecutive no-ENTRY days into spans for the trade log — each with
    its dominant blocker across the whole run. A run is broken by any day that
    traded (bought OR sold), so a weeks-long flat stretch becomes ONE readable row
    ('May 12–Jun 5 · 24 days · MACD not bullish') instead of dozens.

    Each span carries TWO readings of the same rejections. `reason` counts every
    bar-check, which on 15-minute bars means one symbol below the gain bar all
    session scores 26 — accurate, but it reads like 26 missed chances.
    `reason_symbol_days` re-counts them as distinct symbol-days ("3 symbols on 2
    days"), which is what a person actually wants to know. Both are shown."""
    spans: list[dict] = []
    run: list[str] = []

    def flush() -> None:
        nonlocal run
        if run:
            total: Counter = Counter()
            for d in run:
                total.update(day_reject.get(d, Counter()))
            parts = [f"{n}× {_REJECT_LABELS.get(cat, cat)}" for cat, n in total.most_common(2)]
            # Same categories, re-counted as distinct (symbol, day) pairs.
            sym_days: Counter = Counter()
            syms_seen: dict[str, set[str]] = {}
            for d in run:
                for cat, syms in (day_reject_syms or {}).get(d, {}).items():
                    sym_days[cat] += len(syms)
                    syms_seen.setdefault(cat, set()).update(syms)
            sd_parts = [
                f"{sym_days[cat]} symbol-day{'s' if sym_days[cat] != 1 else ''} "
                f"({len(syms_seen.get(cat, ()))} symbol{'s' if len(syms_seen.get(cat, ())) != 1 else ''}) "
                f"— {_REJECT_LABELS.get(cat, cat)}"
                for cat, _ in total.most_common(2)
                if sym_days.get(cat)
            ]
            spans.append(
                {
                    "from_day": run[0],
                    "to_day": run[-1],
                    "days": len(run),
                    "reason": "; ".join(parts) if parts else "no candidates evaluated",
                    "reason_symbol_days": (
                        f"Counting each symbol once per day: {'; '.join(sd_parts)}."
                        if sd_parts
                        else ""
                    ),
                }
            )
        run = []

    for day in days_index:
        if day in action_days:
            flush()
        elif entries_by_day.get(day, 0) == 0 and day in day_reject:
            run.append(day)
        else:
            flush()
    flush()
    return spans


@dataclass
class SimState:
    cash: float
    open_trades: dict[str, SimTrade] = field(default_factory=dict)
    closed: list[SimTrade] = field(default_factory=list)
    entries_by_day: dict[str, int] = field(default_factory=dict)
    realized_by_day: dict[str, float] = field(default_factory=dict)
    last_loss_at: dict[str, datetime] = field(default_factory=dict)


def _seed_losses(prior: dict[str, datetime] | None) -> dict[str, datetime]:
    """The caller's prior-loss seed, copied and made UTC-aware.

    Copied because the simulation WRITES to this dict as it runs, and a caller
    that replays several segments off one seed would otherwise find its own
    input mutated by the first of them. Made aware because every bar timestamp
    in the sim is aware, and `ts - last_loss` on a naive one raises — a seed
    that arrived over JSON, or straight out of SQLite, is routinely naive.
    Everything QT stores is UTC, so saying so is a restatement."""
    return {
        symbol.upper(): (when if when.tzinfo else when.replace(tzinfo=timezone.utc))
        for symbol, when in (prior or {}).items()
        if when is not None
    }


def _rolling_ref_at(ts: datetime) -> datetime:
    """The instant the LIVE engine's "24h change" is actually measured from.

    Not `ts - 24h`, and the difference is the whole point. Live reads the figure
    off HOURLY bars: scanner.crypto_rolling_stats asks for ~25h of them and
    scanner.rolling_24h keeps the newest 24, then takes the OPEN of the oldest one
    in that window. Bars start on the hour, so that open is the price at
    `floor_to_hour(now) - 23h` — anywhere from 1 to 60 minutes NEWER than 24h ago,
    depending only on how far into the hour the engine happened to tick.

    The replay was measuring from `ts - 24h` exactly. On daily bars the two pick
    the same bar and nothing changes, which is why this went unnoticed for so
    long; on 1-minute bars they are up to an hour apart, and an hour of crypto is
    easily a few tenths of a percent. Against a 0% minimum-gain gate a few tenths
    of a percent is the entire decision, so the replay bought names the engine had
    never seen a positive number for — and the fidelity report filed those as
    trades the backtest INVENTED, i.e. as a fault in the strategy logic, when the
    two sides were simply reading the tape from different instants.

    Modelling live's quantisation rather than "fixing" it is the same choice
    _apply_poller_view makes about the 60-second tick: the replay's job is to
    reproduce what the engine could see, not to be more right than it. Changing
    the live definition instead would move real trading behaviour to make a
    measuring instrument agree with itself, which is backwards."""
    return ts.replace(minute=0, second=0, microsecond=0) - timedelta(hours=23)


def _prepare(bars: list[dict], day_of=_et_day, rolling_24h: bool = False) -> list[dict]:
    """Annotate each bar with day-gain and a running intraday VWAP. `day_of`
    buckets bars into trading days (ET for stocks, UTC for crypto).

    Day-gain baseline: stocks measure vs the previous SESSION day's close;
    crypto (`rolling_24h=True`) measures vs the price at the same instant the
    live engine's rolling 24h figure measures from — see _rolling_ref_at, which
    is where the "~24h" is pinned down exactly. On DAILY crypto bars that lands on
    the previous daily bar either way, so daily backtests are unchanged; on
    intraday bars it is what makes the replay's day-gain the engine's day-gain."""
    out = []
    prev_day_close: float | None = None
    cur_day: str | None = None
    last_close: float | None = None
    cum_pv = cum_v = 0.0
    parsed = [_parse_ts(b["t"]) for b in bars]
    ref_idx = -1  # rolling mode: newest bar strictly before live's reference instant
    for i, bar in enumerate(bars):
        ts = parsed[i]
        day = day_of(ts)
        first_of_day = day != cur_day
        if first_of_day:
            prev_day_close = last_close
            cur_day = day
            cum_pv = cum_v = 0.0
        volume = float(bar.get("v") or 0)
        cum_pv += float(bar.get("vw") or bar["c"]) * volume
        cum_v += volume
        if rolling_24h:
            # STRICTLY before the reference instant, so the bar taken is the one
            # that ENDS on it: the close of the 13:59 minute bar is the price at
            # 14:00, which is exactly the open of the 14:00 hourly bar live reads.
            # `<=` would take the bar that starts there instead and land a minute
            # late. Monotonic in ts, so the incremental walk is still valid.
            cutoff = _rolling_ref_at(ts)
            while ref_idx + 1 < i and parsed[ref_idx + 1] < cutoff:
                ref_idx += 1
            ref = float(bars[ref_idx]["c"]) if ref_idx >= 0 and parsed[ref_idx] < cutoff else None
            change_pct = ((bar["c"] / ref - 1) * 100) if ref else None
        else:
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
                # Intra-bar extremes so stops can be judged on what the price
                # actually DID inside the bar, not just where it closed. Falls
                # back to the close when a feed (or a synthetic series) omits
                # them — then behaviour is exactly the old close-only one.
                # _apply_poller_view flattens these onto the close when the bars
                # are no coarser than the live engine's 60-second look, because
                # then the extremes are information live never had.
                "high": float(bar.get("h") or bar["c"]),
                "low": float(bar.get("l") or bar["c"]),
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


def _bar_gaps(days_index: list[str], market: str) -> list[dict]:
    """Stretches of the tested window with NO bars at all.

    The day index is built from days that actually had bars, so a hole doesn't
    render as a flat line — it renders as nothing, and the equity chart draws a
    straight segment from the last day before it to the first day after. That
    looks like "the strategy sat still", which is the one thing it definitely
    wasn't doing:

      - Open positions keep their last seen price for the whole gap (see the
        `last_price` carry-forward), so the equity line is frozen at a stale
        mark and then lurches when real prices reappear. The cliff is the whole
        gap's move arriving in one step, not a crash.
      - Worse, stops CANNOT FIRE without bars. A trailing stop or stop-loss that
        would have closed the position mid-gap never gets the chance, so the
        result reports losses the live engine would have cut.

    Crypto trades every calendar day, so any skipped day is a real hole. Stocks
    skip weekends and holidays, so only a run of more than four calendar days is
    suspicious — that tolerates a long weekend without crying wolf.
    """
    tolerance = 1 if market == "crypto" else 4
    gaps: list[dict] = []
    for previous, following in zip(days_index, days_index[1:]):
        a = datetime.fromisoformat(previous).date()
        b = datetime.fromisoformat(following).date()
        missing = (b - a).days - 1
        if (b - a).days > tolerance:
            gaps.append({"after": previous, "before": following, "days": missing})
    return gaps


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
    fee_pct: float = 0.0,
    eligible_by_day: dict[str, set[str]] | None = None,
    market: str = "stock",
    sim_start: datetime | None = None,
    *,
    sim_end: datetime | None = None,
    daily_bars_by_symbol: dict[str, list[dict]] | None = None,
    rank_daily_bars_by_symbol: dict[str, list[dict]] | None = None,
    progress: Callable[[int, int], None] | None = None,
    prior_loss_at: dict[str, datetime] | None = None,
    debug_log: bool = False,
    account_positions: list[dict] | None = None,
) -> dict:
    """Pure simulation: strategy dict (same shape as the DB row), raw bars per
    symbol, global risk config. Returns metrics + equity curve + trades.

    `sim_start` marks where TRADING begins: bars before it are WARM-UP only — they
    feed the daily indicators (MACD/RSI/ATR need ~35+ prior bars to even be
    defined, exactly like the live engine's 120-day lookback) but never trade,
    never touch equity, and never appear in the trade log or diagnostics. Without
    it a MACD strategy backtested over a short window couldn't trade for its first
    ~35 days because MACD was undefined — unfaithful to live. None = trade every
    bar (the old behaviour, for callers that pre-trim).

    `sim_end` marks where the window CLOSES, inclusive: the replay stops dead at
    the last bar on or before it. None = run to the end of the bars (the old
    behaviour). It exists because bars are fetched from a start and always run to
    now, so "replay 12 May → 3 June" cannot be expressed by trimming the fetch.

    Bars after `sim_end` are dropped ENTIRELY — they do not even re-mark open
    positions. That is the deliberate choice: marking a held position at a price
    from after the window would put a number on the screen the window could not
    have known, and every equity, unrealized-P&L and drawdown figure would then
    describe a period longer than the one asked for. A position still open at
    `sim_end` is marked at the last price INSIDE the window, exactly as a position
    open at the end of the bars always has been. The window's end is the end.

    `eligible_by_day` powers "scanner replay": when given, a symbol may only be
    ENTERED on a day it appears in that day's set (the day's reconstructed
    top-N risers). Exits are never gated — an open position always manages
    itself. When None, every symbol is eligible every day (fixed-list mode).

    `market` selects day bucketing: 'stock' (default) keys days by the ET session
    day; 'crypto' keys by the UTC calendar day, matching the crypto movers cache.
    The default keeps every existing stock backtest byte-identical.

    `daily_bars_by_symbol` turns on MIXED-RESOLUTION replay, which resolves the
    one tension a single bar stream can't: MACD/RSI/ATR must come from COMPLETED
    DAILY closes (that's how the live engine reads them), but a daily replay calls
    the exit logic once a day at the close, so a stop-loss or trailing stop can't
    be simulated — an intraday dip through the stop that recovers by the close is
    scored a WINNER. When given, `bars_by_symbol` is the INTRADAY replay timeline
    (entries, exits, equity, day-gain, VWAP) and `daily_bars_by_symbol` is a daily
    series used ONLY to derive the indicators, per symbol, day by day. The daily
    series is expected to reach BACK BEFORE the intraday window — that's the
    indicator warm-up. Look-ahead safety is enforced in _daily_frontier: an
    intraday bar on day D reads the value derived from daily closes through D−1,
    never D's own close. A symbol with no daily bars gets None indicators for
    every bar (the entry/exit rules already treat None as "can't tell").
    None (the default) = single-resolution replay, byte-identical to before.

    `rank_daily_bars_by_symbol` is the daily history a DAILY-BAR ranking metric
    (relative_strength, rs_vs_spy, return_30d, rsi) is scored from, and must reach
    back over live's own lookback for that metric — see RANK_LOOKBACK_DAYS. Only
    needed when the replay has no daily series of its own: a 1Day replay is its
    own source, and a mixed-resolution run's indicator series is used when it
    reaches far enough. Include "SPY" in it for rs_vs_spy. When a ranked strategy
    needs daily bars and none arrive, the replay does NOT quietly fall back to
    evaluating the whole pool — it says so in `result["ranking"]`.

    `progress` is called as (bars_done, bars_total) every so often so a caller
    running this in the background can say how far along it is. Purely an
    observer — the simulation never reads it, and None (the default) keeps this
    function exactly as pure as it was.

    `prior_loss_at` ({symbol: when it last closed at a loss}) is the rail state
    the account already carried INTO the window. The after-loss cooldown — and,
    for stocks, the wash-sale guard — are both driven by the most recent losing
    exit per symbol, and the simulation only ever learns about losses it made
    itself. So a replay starting mid-history begins with a clean slate the live
    engine did not have, refuses nothing, buys a symbol live was still cooling
    off on, and the fidelity report blames the backtester for it.

    None (the default) is a clean slate, so an ORDINARY backtest is byte-
    identical to before — and deliberately so. A 180-day backtest has no "before
    the window" to speak of, and inventing one would be arbitrary. Only the
    fidelity comparison, which is replaying a window the account really lived
    through, has a truthful answer to seed with.

    A loss INSIDE the window overwrites the seed for that symbol: this is the
    starting point, not a floor."""
    params = strategy["params"]
    swing = strategy["swing_mode"]
    sizing = strategy["sizing_usd"]
    slip = spread_pct / 100
    # A commission is NOT a price adjustment — it's cash off the top of each
    # side, charged on the notional. Modelling it as extra slippage would flatter
    # a big position and punish a small one, which is backwards.
    fee_rate = max(0.0, fee_pct) / 100
    fees_paid = 0.0
    day_of = _day_fn(market)

    prepared = {
        # Rolling 24h day-gain follows the STRATEGY'S asset class, not the day
        # bucketing. Tying it to `market` meant a crypto run bucketed as 'stock'
        # silently measured day-gain against an ET calendar day — the thing
        # min_day_gain_pct filters on — while live, the optimizer and the
        # portfolio backtester all used the rolling 24h baseline. Same bars, a
        # 2.4x different number. run_portfolio_backtest already keyed off the
        # asset class for exactly this reason; this path never caught up.
        s: _prepare(b, day_of, rolling_24h=strategy["asset_class"] == "crypto")
        for s, b in bars_by_symbol.items()
        if b
    }
    # Each is a no-op unless the strategy opts in. `daily_bars_by_symbol` (None
    # by default) switches them to the daily series for their values while the
    # replay below still runs bar by intraday bar — same `day_of` bucketing on
    # both sides, so the two resolutions agree on what "a day" is.
    _annotate_macd(prepared, params, daily_bars_by_symbol, day_of)
    _annotate_atr(prepared, bars_by_symbol, params, daily_bars_by_symbol, day_of)
    _annotate_rsi(prepared, params, daily_bars_by_symbol, day_of)
    # At or below the live engine's 60-second poll cadence, a bar's high/low carry
    # information the live engine could not have had — see _apply_poller_view.
    # Coarser bars are untouched, so every 15-minute/hourly/daily backtest keeps
    # the intra-bar model.
    bar_seconds = _bar_seconds(sorted({b["ts"] for s in prepared.values() for b in s}))
    # Measured BEFORE nothing in particular — the poller view rewrites high/low,
    # never close — but read here so it sits next to the model it qualifies.
    bar_move_pct = _median_bar_move_pct(prepared)
    # The pool cut live makes every cycle. Built here, before the poller view, so
    # it reads the untouched bars — it scores on close and change_pct, neither of
    # which _apply_poller_view rewrites, but the ordering makes that a fact rather
    # than a coincidence.
    rank_cfg = ranking_config(strategy)
    ranker = (
        _PoolRanker(
            rank_cfg[0], rank_cfg[1], prepared,
            _rank_daily_source(
                rank_daily_bars_by_symbol, daily_bars_by_symbol, bars_by_symbol, bar_seconds
            ),
            day_of,
        )
        if rank_cfg is not None
        else None
    )
    poller_view = _apply_poller_view(prepared, bar_seconds)
    # chronological event stream across all symbols
    events: dict[datetime, dict[str, dict]] = {}
    for symbol, series in prepared.items():
        for bar in series:
            events.setdefault(bar["ts"], {})[symbol] = bar
    if not events:
        return {"error": "No historical bars for those symbols/timeframe."}

    state = SimState(cash=starting_cash, last_loss_at=_seed_losses(prior_loss_at))
    # WHAT THE SIMULATION ACTUALLY TOOK IN, read off the seeded rail state rather
    # than off the caller's request. The API used to answer this by echoing its own
    # request body back — a confirmation that confirms nothing, because a path that
    # accepted the seed and then ignored it reported it as applied just the same,
    # and the fidelity report would have said the rails were seeded when they were
    # not. Snapshotted HERE, before the loop: `state.last_loss_at` gains the
    # window's own losing exits as the replay runs, and reading it at the end would
    # report symbols this window produced as though they had been carried in.
    rails_seeded = sorted(state.last_loss_at)
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
    # The same rejections counted as distinct SYMBOL-DAYS. The tally above counts
    # every bar-check, so on 15-minute bars one symbol sitting below the gain bar
    # all session scores 26 — true, but it reads like 26 missed chances. This
    # answers "how many symbols, on how many days" instead.
    day_reject_syms: dict[str, dict[str, set[str]]] = {}

    def reject(day: str, symbol: str, category: str) -> None:
        day_reject.setdefault(day, Counter())[category] += 1
        day_reject_syms.setdefault(day, {}).setdefault(category, set()).add(symbol)

    # `debug_log` is the caller asking for the lines BACK; the env var is the
    # operator asking for them in the container log. Either turns the logging on.
    backdrop = _AccountBackdrop(account_positions)
    debug_lines: list[str] | None = [] if debug_log else None
    debug = bool(os.getenv("QT_BACKTEST_DEBUG")) or bool(debug_log)
    def emit(line: str) -> None:
        if debug_lines is not None:
            debug_lines.append(line)
        else:
            print(line, flush=True)

    if debug:
        emit(
            f"BTDBG === run_backtest symbols={sorted(prepared)} "
            f"min_gain={params.get('entry', {}).get('min_day_gain_pct')} macd_on={_macd_on(params)} "
            f"require_macd={params.get('entry', {}).get('require_macd_bullish')} sim_start={sim_start} ==="
        )
        for sym, ser in prepared.items():
            defined = [b["day"] for b in ser if b.get("macd_bullish") is not None]
            emit(
                f"BTDBG   {sym}: {len(ser)} bars [{ser[0]['day']}..{ser[-1]['day']}] "
                f"macd_defined_from={defined[0] if defined else 'never'}"
            )

    # Per-day P&L attribution: for every simulated day, which positions were held
    # and how much each moved the equity. A holding's day-P&L = Δ(position value)
    # + that symbol's cash flows today (exit proceeds / entry cost) — the three
    # cases (held through, entered today, exited today) all fall out of the one
    # formula, and the per-day sum equals the equity curve's day change.
    attrib_day: str | None = None
    prev_day_value: dict[str, float] = {}  # position values at the PRIOR day's close
    day_flows: dict[str, float] = {}  # today's cash flows per symbol (+sell, −buy)
    day_contrib: dict[str, list[dict]] = {}

    timeline = sorted(events)
    # Report roughly a hundred times over the run, whatever its length — often
    # enough that the number visibly moves, rare enough to cost nothing.
    tick_every = max(1, len(timeline) // 100)
    for step, ts in enumerate(timeline):
        # Ahead of everything else in the loop on purpose: a bar past the window's
        # end must not trade, mark a holding, or extend the day index. See the
        # sim_end note above for why that is not the mirror image of warm-up.
        # A break rather than a continue because the timeline is sorted — there is
        # nothing after this bar that could ever be inside the window.
        if sim_end is not None and ts > sim_end:
            break
        if progress is not None and step % tick_every == 0:
            progress(step, len(timeline))
        bars = events[ts]
        day = day_of(ts)
        if sim_start is not None and ts < sim_start:
            for symbol, bar in bars.items():
                last_price[symbol] = bar["close"]
            continue  # warm-up bar: fed the indicators above; no trading / equity / diagnostics
        if day != attrib_day:
            # Day rollover: freeze YESTERDAY's closing position values before
            # today's prices overwrite last_price — that's the baseline each
            # holding's day-P&L is measured against.
            prev_day_value = {
                s: t.qty * last_price.get(s, t.entry_price) for s, t in state.open_trades.items()
            }
            day_flows = {}
            attrib_day = day
        for symbol, bar in bars.items():
            last_price[symbol] = bar["close"]

        # ---- exits first ----
        for symbol, trade in list(state.open_trades.items()):
            bar = bars.get(symbol)
            if not bar:
                continue
            price = bar["close"]
            # The peak is the bar's HIGH, not its close — a trailing stop trails
            # the actual high-water mark, not the close-to-close one.
            trade.high_water = max(trade.high_water, bar["high"])
            trigger: dict = {}
            should_exit, reason = evaluate_exit(
                params, swing, trade.entry_price, trade.entry_at,
                trade.high_water, price, bar["vwap"], ts, bar.get("last_of_day", False),
                macd_bullish=bar.get("macd_bullish"),
                atr_pct=bar.get("atr_pct"),
                rsi=bar.get("rsi"),
                is_crypto=strategy["asset_class"] == "crypto",
                bar_high=bar["high"], bar_low=bar["low"], out=trigger,
            )
            if not should_exit:
                continue
            fill = _fill_price(trigger, bar) * (1 - slip)
            trade.exit_price = fill
            trade.exit_at = ts
            trade.exit_reason = reason
            # Both sides' fees land on the trade that closes: the entry fee was
            # already paid in cash, but the P&L has to carry it or every trade
            # reads better than it was.
            exit_fee = fill * trade.qty * fee_rate
            entry_fee = trade.entry_price * trade.qty * fee_rate
            # Only the EXIT side is added here: the entry side was counted when it
            # was actually paid (see the entry leg). Counting it again on the
            # close would double it, and counting it ONLY here left every position
            # still open at the end reporting a commission of zero on money that
            # had already left the account.
            fees_paid += exit_fee
            trade.pnl = round((fill - trade.entry_price) * trade.qty - exit_fee - entry_fee, 2)
            state.cash += fill * trade.qty - exit_fee
            day_flows[symbol] = day_flows.get(symbol, 0.0) + fill * trade.qty - exit_fee
            state.realized_by_day[day] = state.realized_by_day.get(day, 0.0) + trade.pnl
            if trade.pnl < 0:
                state.last_loss_at[symbol] = ts
            state.closed.append(trade)
            del state.open_trades[symbol]
            if debug:
                _btlog(day, symbol, bar, params, f"EXIT {reason} @ {fill:.2f} pnl={trade.pnl}", debug_lines)

        # ---- entries ----
        eligible = eligible_by_day.get(day) if eligible_by_day is not None else None
        # Ranked universe: only the top-N are candidates, and they are offered
        # BEST FIRST — the sleeve, the cash and max_positions all bite from the
        # bottom of that list, exactly as they do live. Symbols cut by the rank
        # are dropped silently rather than tallied as rejections: live never
        # looked at them, and counting fourteen of a twenty-four-name basket on
        # every bar would bury the real blocker under "outside the top 10" in
        # every no-trade-day summary.
        if ranker is not None and ranker.applied:
            order = ranker.rank(bars, ts)
        else:
            order = [(symbol, None, None) for symbol in bars]
        for symbol, rank_pos, rank_of in order:
            bar = bars[symbol]
            if eligible is not None and symbol not in eligible:
                reject(day, symbol, "not_eligible")
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
                rank=rank_pos, rank_of=rank_of,
            )
            ok, entry_reason = evaluate_entry(params, cand, ts.astimezone(ET))
            # The same sentence the live journal writes (engine._rank_note), so a
            # replayed entry and the real one it is being compared against read
            # alike instead of differing for a reason that isn't a difference.
            entry_reason += _rank_note(rank_cfg[0] if rank_cfg else None, cand)
            if not ok:
                if "< required" in entry_reason:
                    diag["rejected_day_gain"] += 1
                elif "VWAP" in entry_reason:
                    diag["rejected_vwap"] += 1
                elif "entry window" in entry_reason:
                    diag["rejected_entry_window"] += 1
                reject(day, symbol, _reject_category(entry_reason))
                if debug:
                    _btlog(day, symbol, bar, params, f"reject-entry: {entry_reason}", debug_lines)
                continue
            # ATR sizing (opt-in): size so a stop-out loses ~risk_usd, capped at
            # the sleeve; falls back to the fixed `sizing` when off or atr_pct is
            # unavailable. Byte-identical to `sizing` when ATR sizing is off.
            entry_sizing = atr_position_size(params, sizing, strategy["sleeve_usd"], bar.get("atr_pct"))
            daily_loss = max(0.0, -state.realized_by_day.get(day, 0.0))
            # What the REST of the account held at this instant. Zero unless the
            # caller seeded it, so an ordinary backtest replays byte-identically.
            others_n, others_usd, others_symbols = backdrop.at(ts)
            ctx = RailContext(
                # Equity rises with the account's other holdings too: the no-leverage
                # cap is min(max_total_exposure_usd, equity), so counting their
                # exposure without their equity would invent a rail live never hit.
                equity=equity + others_usd,
                open_positions_total=len(state.open_trades) + others_n,
                open_exposure_usd=open_exposure + others_usd,
                open_positions_strategy=len(state.open_trades),
                open_exposure_strategy_usd=open_exposure,
                entries_today=state.entries_by_day.get(day, 0),
                already_open_symbol=symbol in state.open_trades or symbol in others_symbols,
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
                    rails_reason = (
                        f"rail: cooldown after loss ({(ts - last_loss).days}d since {last_loss.date()} "
                        f"< {risk.get('cooldown_hours_after_loss', 24)}h)"
                    )
            if not rails_ok:
                diag["entry_ok_but_rail_blocked"] += 1
                reject(day, symbol, _rail_category(rails_reason))
                if debug:
                    _btlog(day, symbol, bar, params, f"reject-rail: {rails_reason}", debug_lines)
                continue
            fill = bar["close"] * (1 + slip)
            qty = _entry_qty(strategy["asset_class"], entry_sizing, fill, _fractional(params))
            # The fee is part of what the buy costs you, so it has to clear the
            # cash check too — otherwise the sim can spend money it doesn't have.
            if qty <= 0 or fill * qty * (1 + fee_rate) > state.cash:
                diag["too_small_or_no_cash"] += 1
                reject(day, symbol, "sizing")
                if debug:
                    _btlog(day, symbol, bar, params, f"reject-sizing: qty={qty} cost={fill * qty:.2f} cash={state.cash:.2f}", debug_lines)
                continue
            state.cash -= fill * qty * (1 + fee_rate)
            fees_paid += fill * qty * fee_rate  # counted when it is paid, not when it closes
            day_flows[symbol] = day_flows.get(symbol, 0.0) - fill * qty * (1 + fee_rate)
            state.entries_by_day[day] = state.entries_by_day.get(day, 0) + 1
            if debug:
                _btlog(day, symbol, bar, params, f"ENTER @ {fill:.2f} x{qty} ({entry_reason})", debug_lines)
            state.open_trades[symbol] = SimTrade(
                symbol=symbol, qty=qty, entry_price=fill, entry_at=ts,
                entry_reason=entry_reason, high_water=fill,
                rank=rank_pos, rank_of=rank_of,
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

        # Per-holding contribution to today's move (recomputed each bar; the last
        # write is the day's close). qty 0 = a position closed earlier today.
        touched = set(prev_day_value) | set(day_flows) | set(state.open_trades)
        contrib = []
        for s in touched:
            t = state.open_trades.get(s)
            value_now = t.qty * last_price.get(s, t.entry_price) if t else 0.0
            pnl = value_now - prev_day_value.get(s, 0.0) + day_flows.get(s, 0.0)
            contrib.append({
                "symbol": s,
                "qty": t.qty if t else 0,
                "price": _price(last_price[s]) if s in last_price else None,
                "day_pnl": round(pnl, 2),
            })
        contrib.sort(key=lambda c: -abs(c["day_pnl"]))
        day_contrib[day] = contrib

    # Positions still open at the end are NOT force-sold: the strategy never
    # signalled an exit, so a synthetic end-of-test sale would invent a trade and
    # skew win-rate / profit-factor. Mark them to market instead — their value
    # counts toward equity / net P&L (unrealized), while the closed-trade stats
    # below cover only REAL strategy exits.
    open_positions = []
    open_value = 0.0
    for symbol, trade in state.open_trades.items():
        mark_price = last_price.get(symbol, trade.entry_price)
        open_value += mark_price * trade.qty
        open_positions.append({
            "symbol": symbol, "qty": trade.qty,
            "entry_price": _price(trade.entry_price), "entry_at": trade.entry_at.isoformat(),
            "entry_day": day_of(trade.entry_at), "entry_reason": trade.entry_reason,
            "mark_price": _price(mark_price),
            "unrealized_pnl": round((mark_price - trade.entry_price) * trade.qty, 2),
            "rank": trade.rank, "rank_of": trade.rank_of,
        })

    min_gain = params.get("entry", {}).get("min_day_gain_pct", 0)
    qualifying_days = len(diag.pop("days_reaching_min_gain"))
    diag["days_reaching_min_gain"] = qualifying_days
    if not state.closed and not open_positions:
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
        elif not diag["bars_evaluated"]:
            # Nothing was ever judged, so nothing can be said about the rules.
            # Reporting a rule verdict here is how a 3.5-hour crypto window
            # replayed on daily bars came back as "the rules rejected your
            # trades" — the replay had not looked at a single bar. A day-gain
            # baseline needs a prior bar to measure from (the previous session
            # for stocks, 24h back for crypto); without one every bar is
            # unusable and silently skipped.
            diag["summary"] = (
                "The replay could not evaluate a single bar in this window — so this says "
                "nothing about your rules. Either no bars were returned for the window, or "
                "none had a day-gain baseline to measure against (that needs history from "
                "before the window starts). A window shorter than a day replayed on daily "
                "bars is the usual cause."
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
    final_equity = state.cash + open_value  # cash + open positions marked to market
    equity_values = [v for _, v in equity_curve]
    net_pnl = round(final_equity - starting_cash, 2)
    days_index = [d for d, _ in equity_curve]
    # Days that saw a buy OR a sell — these break the "no action" runs below.
    action_days: set[str] = set()
    for t in closed:
        action_days.add(day_of(t.entry_at))
        if t.exit_at:
            action_days.add(day_of(t.exit_at))
    for p in open_positions:
        action_days.add(p["entry_day"])  # an entry that never exited is still an action

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
        "fee_pct_per_side": fee_pct,
        # Which exit model this run used, so a number that moved can be explained
        # rather than guessed at. "poller" = bars at or below the live engine's
        # 60-second look; exits judged and filled on the close, like live.
        # "intrabar" = coarser bars; stops judged on the bar's high/low and filled
        # at the trigger level, because a 60-second poller would have sampled it.
        "exit_model": "poller" if poller_view else "intrabar",
        "bar_seconds": bar_seconds,
        # How far apart two sampling grids one bar out of phase can land — the
        # floor under any exit comparison. See _median_bar_move_pct.
        "median_bar_move_pct": bar_move_pct,
        # THE TOP-N CUT. Present (and honest) on every run of a ranked strategy:
        # `applied` false means the replay could not reproduce live's ordering and
        # therefore drew from a wider pool than live would have — the single
        # reason a ranked backtest reads more permissive than reality. None on an
        # unranked universe, which is not the same thing as a failed ranking.
        "ranking": ranker.report() if ranker is not None else None,
        # What the commissions actually took, in dollars. A rate in the header is
        # abstract; "$412 of fees on 63 round trips" is the number that decides
        # whether a strategy this busy can carry its own costs.
        "fees_paid": round(fees_paid, 2),
        # Symbols whose after-loss cooldown (and, for stocks, wash-sale guard) this
        # run STARTED with — see the note where it is snapshotted. Empty for every
        # ordinary backtest, which has no "before the window" to carry in.
        "rails_seeded": rails_seeded,
        "max_deployed_usd": round(max_deployed, 2),
        "pct_capital_deployed": pct_deployed,
        "return_on_deployed_pct": return_on_deployed,
        "time_in_market_pct": time_in_market,
        # Which bars MACD/RSI/ATR actually came from. None when none are on.
        "daily_signals": _daily_signal_report(params, daily_bars_by_symbol, bar_seconds),
        # Only when the caller asked. Truncated rather than streamed: a 1-minute
        # replay of eleven symbols is ~20,000 lines, and the point of asking is
        # always a narrow window, so the caller filters. `debug_log_total` is the
        # count BEFORE truncation, because a log that silently stops is worse
        # than no log — you would read the last line as the last decision.
        **(
            {
                "debug_log": debug_lines[:DEBUG_LOG_MAX_LINES],
                "debug_log_total": len(debug_lines),
                "debug_log_truncated": len(debug_lines) > DEBUG_LOG_MAX_LINES,
            }
            if debug_lines is not None
            else {}
        ),
        "diagnosis": diag,
        # {day -> plain-English reason} for every day that evaluated candidates but
        # opened nothing — the chart shows it when you land on a no-trade day.
        "no_trade_reasons": _summarize_no_trade_days(day_reject, state.entries_by_day),
        # Consecutive no-entry days collapsed into spans, interleaved into the
        # trade log so a flat stretch explains itself ({from_day,to_day,days,reason}).
        "no_trade_spans": _no_trade_spans(
            days_index, day_reject, state.entries_by_day, action_days, day_reject_syms
        ),
        "equity_days": days_index,
        "equity": [round((v / starting_cash - 1) * 100, 2) for _, v in equity_curve],
        # Stretches with no bars at all. The chart draws a straight line across
        # them, which reads as "nothing happened" when in fact positions were
        # marked at a stale price and no stop could fire — so the run has to say
        # so rather than let the flat segment speak for itself.
        "bar_gaps": _bar_gaps(days_index, market),
        "hold_benchmark": _hold_benchmark(prepared, days_index),
        "hold_benchmark_label": (
            list(prepared)[0] if len(prepared) == 1 else f"{len(prepared)} symbols (equal weight)"
        ),
        # Positions still held when the test window ended (marked to market, not
        # sold). Their unrealized P&L is already in net_pnl / the equity curve.
        "open_positions": open_positions,
        # {day -> [{symbol, qty, price, day_pnl}]}: what was held each day and how
        # much each holding moved the equity (qty 0 = closed that day). The chart
        # hover uses it to explain a day's rise/fall position by position.
        "daily_positions": day_contrib,
        "trade_list": [
            {
                "symbol": t.symbol, "qty": t.qty,
                "entry_price": _price(t.entry_price), "entry_at": t.entry_at.isoformat(),
                # Day strings so the chart can place markers without the frontend
                # re-deriving timezones and drifting off by a day (ET for stocks,
                # UTC for crypto — matching the equity-curve day index).
                "entry_day": day_of(t.entry_at),
                "entry_reason": t.entry_reason,
                # Where this name stood in its own strategy's ranking when it was
                # bought — the pair the live journal stamps, so the two sides of a
                # fidelity comparison can be matched on it.
                "rank": t.rank, "rank_of": t.rank_of,
                "exit_price": _price(t.exit_price or 0),
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
    sim_start: datetime | None = None,
    daily_bars_by_strategy: dict[int, dict[str, list[dict]]] | None = None,
    sim_end: datetime | None = None,
    prior_loss_at: dict[str, datetime] | None = None,
    rank_daily_by_strategy: dict[int, dict[str, list[dict]]] | None = None,
    fee_pct_by_class: dict[str, float] | None = None,
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

    `sim_end` closes the window (inclusive), mirroring run_backtest's: the replay
    stops at the last bar on or before it and bars after it never mark a position.

    `prior_loss_at` seeds the after-loss cooldown (and the wash-sale guard) with
    the losses the account already carried into the window, exactly as in
    run_backtest — the rail is account-wide there and here, so one dict keyed by
    symbol covers the whole book. None (the default) starts clean, leaving every
    existing portfolio backtest untouched.

    RANKING is per strategy and per evaluation point, exactly as in run_backtest:
    a strategy whose universe live ranks (a basket, or watchlist/custom with
    rank_enabled) offers only its own top-N, best first, while the strategies
    beside it are untouched. `rank_daily_by_strategy` is the daily history a
    daily-bar ranking metric is scored from, keyed by strategy id — see
    run_backtest's `rank_daily_bars_by_symbol`.

    `fee_pct_by_class` ({asset_class: commission % per side}) is the round-trip
    cost, charged PER STRATEGY off its own asset class. Keyed that way rather
    than as one number because a portfolio is the one replay that can hold both:
    US equities on Alpaca are commission-free while crypto is charged 0.15–0.25%
    a side, so a mixed book with one flat rate would either invent a stock fee or
    excuse the crypto one. None (the default) = free, which keeps every existing
    portfolio backtest byte-identical — and is why an all-crypto book reported a
    profit the real account would never have seen until its caller started
    passing this.
    """
    day_of = _day_fn(market)
    slip = spread_pct / 100
    # Per-strategy commission rate, resolved once. Same modelling as
    # run_backtest: cash off the top of each side on the notional, never an
    # adjustment to the price (see the note there).
    fee_rate_by_sid = {
        s["id"]: max(0.0, float((fee_pct_by_class or {}).get(s["asset_class"], 0.0) or 0.0)) / 100
        for s in strategies
    }
    fees_paid = 0.0
    strat_by_id = {s["id"]: s for s in strategies}
    order = {s["id"]: i for i, s in enumerate(strategies)}  # deterministic tie-break

    # Prepare each strategy's own bars, then fold everything into one chronological
    # event stream tagged with the strategy that owns each bar.
    prepared_by_strategy: dict[int, dict[str, list[dict]]] = {
        sid: {
            # Rolling 24h day-gain for a CRYPTO strategy's bars (its own asset
            # class, not the portfolio's day-bucketing market — a mixed book
            # keeps each side's own baseline).
            s: _prepare(b, day_of, rolling_24h=strat_by_id[sid]["asset_class"] == "crypto")
            for s, b in (bars_by_strategy.get(sid) or {}).items()
            if b
        }
        for sid in strat_by_id
    }
    # Each strategy carries its own MACD/ATR periods/toggles; annotate its own bars.
    #
    # MIXED RESOLUTION. `daily_bars_by_strategy` makes this a mixed run: the
    # merged timeline stays intraday for everyone, while MACD, RSI and ATR are
    # read off each strategy's DAILY series — the same arrangement run_backtest
    # gives a single strategy, and the same reason. The live engine computes all
    # three from completed daily bars; a "14-period ATR" over 15-minute bars
    # spans three and a half hours, not fourteen days.
    #
    # This was thought to need a per-strategy timeline, which it does not. Every
    # strategy replays the SAME stream; only the indicator SOURCE differs, and
    # that is per-strategy already because each carries its own periods. The
    # annotators have taken a daily source all along — the portfolio simply never
    # passed one. Look-ahead safety is unchanged: _daily_frontier only ever reads
    # daily bars completed BEFORE each replay bar's own day.
    #
    # `day_of` IS AN ARGUMENT, and leaving it out cost a full day of look-ahead.
    # The annotators default to `_et_day`, while `prepared` above was bucketed by
    # this run's own `day_of` — `_utc_day` for an all-crypto book. A crypto daily
    # bar opens at 00:00Z, which ET files under the PREVIOUS day, so
    # _daily_frontier's "daily bars strictly before day D" quietly included day
    # D's own close: an intraday bar at 02:00Z read a daily MACD/RSI/ATR derived
    # from a close 22 hours in its future. Measured on a probe with a 20-day
    # losing streak and a huge up day D, the replay entered on an RSI of 52 where
    # live could only ever have seen 0. run_backtest passes it; this path never did.
    for sid, prepared in prepared_by_strategy.items():
        daily = (daily_bars_by_strategy or {}).get(sid)
        _annotate_macd(prepared, strat_by_id[sid]["params"], daily, day_of)
        _annotate_atr(
            prepared, bars_by_strategy.get(sid) or {}, strat_by_id[sid]["params"],
            daily, day_of,
        )
        _annotate_rsi(prepared, strat_by_id[sid]["params"], daily, day_of)
    # The poller view is a property of the SHARED timeline, not of one strategy:
    # every strategy in a portfolio run replays the same stream, so the bar size is
    # measured once across all of them and applied to all of them. See
    # _apply_poller_view for why, and for why coarse bars are left alone.
    bar_seconds = _bar_seconds(
        sorted({
            b["ts"]
            for by_symbol in prepared_by_strategy.values()
            for series in by_symbol.values()
            for b in series
        })
    )
    bar_move_pct = _median_bar_move_pct(
        {
            f"{sid}:{symbol}": series
            for sid, by_symbol in prepared_by_strategy.items()
            for symbol, series in by_symbol.items()
        }
    )
    # One ranker per RANKED strategy (see the _PoolRanker note): each ranks its
    # own universe by its own metric, and the strategies beside it are unaffected.
    rankers: dict[int, _PoolRanker] = {}
    rank_cfg_by_sid: dict[int, tuple[str, int]] = {}
    for sid, prepared in prepared_by_strategy.items():
        cfg = ranking_config(strat_by_id[sid])
        if cfg is None or not prepared:
            continue
        rank_cfg_by_sid[sid] = cfg
        rankers[sid] = _PoolRanker(
            cfg[0], cfg[1], prepared,
            _rank_daily_source(
                (rank_daily_by_strategy or {}).get(sid),
                (daily_bars_by_strategy or {}).get(sid),
                bars_by_strategy.get(sid) or {},
                bar_seconds,
            ),
            day_of,
        )
    poller_view = False
    for by_symbol in prepared_by_strategy.values():
        poller_view = _apply_poller_view(by_symbol, bar_seconds) or poller_view
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
    last_loss_at: dict[str, datetime] = _seed_losses(prior_loss_at)
    # Snapshotted before the loop, for the reason run_backtest gives at the same
    # spot: this dict grows the window's own losing exits as the replay runs.
    rails_seeded = sorted(last_loss_at)
    last_price: dict[str, float] = {}
    equity_curve: list[tuple[str, float]] = []
    max_deployed = 0.0
    bars_with_position = 0
    total_bar_ticks = 0

    for ts in sorted(events):
        # Ahead of the last_price refresh below on purpose: a bar past the
        # window's end must not re-mark a holding either (see run_backtest).
        if sim_end is not None and ts > sim_end:
            break
        tick = events[ts]
        # index this tick's bars for exit lookup + refresh last-seen prices
        bar_at: dict[tuple[int, str], dict] = {}
        for sid, symbol, bar in tick:
            bar_at[(sid, symbol)] = bar
            last_price[symbol] = bar["close"]
        day = day_of(ts)
        if sim_start is not None and ts < sim_start:
            continue  # warm-up bar: feeds indicators only, no trading / equity

        # ---- exits first (each trade managed by ITS OWN strategy's rules) ----
        for symbol, trade in list(open_trades.items()):
            bar = bar_at.get((trade.strategy_id, symbol))
            if not bar:
                continue
            strat = strat_by_id[trade.strategy_id]
            price = bar["close"]
            trade.high_water = max(trade.high_water, bar["high"])
            trigger = {}
            should_exit, reason = evaluate_exit(
                strat["params"], strat["swing_mode"], trade.entry_price, trade.entry_at,
                trade.high_water, price, bar["vwap"], ts, bar.get("last_of_day", False),
                macd_bullish=bar.get("macd_bullish"),
                atr_pct=bar.get("atr_pct"),
                rsi=bar.get("rsi"),
                is_crypto=strat["asset_class"] == "crypto",
                bar_high=bar["high"], bar_low=bar["low"], out=trigger,
            )
            if not should_exit:
                continue
            fill = _fill_price(trigger, bar) * (1 - slip)
            trade.exit_price = fill
            trade.exit_at = ts
            trade.exit_reason = reason
            # Both sides' fees land on the trade that closes, exactly as in
            # run_backtest: the entry fee already left the cash account, but the
            # P&L has to carry it or every trade reads better than it was.
            fee_rate = fee_rate_by_sid.get(trade.strategy_id, 0.0)
            exit_fee = fill * trade.qty * fee_rate
            entry_fee = trade.entry_price * trade.qty * fee_rate
            fees_paid += exit_fee  # the entry side was counted when it was paid
            trade.pnl = round((fill - trade.entry_price) * trade.qty - exit_fee - entry_fee, 2)
            cash += fill * trade.qty - exit_fee
            realized_by_day[day] = realized_by_day.get(day, 0.0) + trade.pnl
            if trade.pnl < 0:
                last_loss_at[symbol] = ts
            closed.append(trade)
            del open_trades[symbol]

        # ---- entries (deterministic order: strategy index, then symbol) ----
        # …except inside a RANKED strategy, where the order is its ranking and
        # only its top-N are offered at all — the account's cash and the global
        # rails are shared, so which name a strategy puts forward first decides
        # what the strategy after it can still afford.
        tick_by_sid: dict[int, dict[str, dict]] = {}
        for sid, symbol, bar in tick:
            tick_by_sid.setdefault(sid, {})[symbol] = bar
        candidates: list[tuple[int, str, dict, int | None, int | None]] = []
        for sid in sorted(tick_by_sid, key=lambda s: order[s]):
            ranker = rankers.get(sid)
            if ranker is not None and ranker.applied:
                candidates += [
                    (sid, symbol, tick_by_sid[sid][symbol], pos, of)
                    for symbol, pos, of in ranker.rank(tick_by_sid[sid], ts)
                ]
            else:
                candidates += [
                    (sid, symbol, tick_by_sid[sid][symbol], None, None)
                    for symbol in sorted(tick_by_sid[sid])
                ]
        for sid, symbol, bar, rank_pos, rank_of in candidates:
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
                rank=rank_pos, rank_of=rank_of,
            )
            ok, entry_reason = evaluate_entry(params, cand, ts.astimezone(ET))
            entry_reason += _rank_note(
                (rank_cfg_by_sid.get(sid) or (None,))[0], cand
            )
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
            # The fee is part of what the buy costs, so it clears the cash check
            # too — otherwise the sim spends money it doesn't have (run_backtest).
            fee_rate = fee_rate_by_sid.get(sid, 0.0)
            if qty <= 0 or fill * qty * (1 + fee_rate) > cash:
                continue
            cash -= fill * qty * (1 + fee_rate)
            fees_paid += fill * qty * fee_rate  # counted when it is paid, not when it closes
            entries_by_day[day] = entries_by_day.get(day, 0) + 1
            open_trades[symbol] = PortfolioTrade(
                symbol=symbol, qty=qty, entry_price=fill, entry_at=ts,
                entry_reason=entry_reason, high_water=fill,
                rank=rank_pos, rank_of=rank_of,
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

    # Open positions at the end aren't force-sold (see run_backtest) — marked to
    # market so their value is in equity / net P&L, but never counted as trades.
    open_positions = []
    open_value = 0.0
    unrealized_by_strat: dict[int, float] = {}
    for symbol, trade in open_trades.items():
        mark_price = last_price.get(symbol, trade.entry_price)
        open_value += mark_price * trade.qty
        u = round((mark_price - trade.entry_price) * trade.qty, 2)
        unrealized_by_strat[trade.strategy_id] = unrealized_by_strat.get(trade.strategy_id, 0.0) + u
        open_positions.append({
            "symbol": symbol, "qty": trade.qty,
            "entry_price": _price(trade.entry_price), "entry_at": trade.entry_at.isoformat(),
            "entry_day": day_of(trade.entry_at), "entry_reason": trade.entry_reason,
            "mark_price": _price(mark_price), "unrealized_pnl": u,
            "rank": trade.rank, "rank_of": trade.rank_of,
            "strategy_id": trade.strategy_id, "strategy_name": trade.strategy_name,
        })

    wins = [t for t in closed if (t.pnl or 0) > 0]
    losses = [t for t in closed if (t.pnl or 0) <= 0]
    gross_win = sum(t.pnl or 0 for t in wins)
    gross_loss = -sum(t.pnl or 0 for t in losses)
    final_equity = cash + open_value  # cash + open positions marked to market
    net_pnl = round(final_equity - starting_cash, 2)
    equity_values = [v for _, v in equity_curve]
    days_index = [d for d, _ in equity_curve]

    pct_deployed = round(max_deployed / starting_cash * 100, 2) if starting_cash else 0.0
    return_on_deployed = round(net_pnl / max_deployed * 100, 2) if max_deployed > 0 else None
    time_in_market = round(bars_with_position / total_bar_ticks * 100, 1) if total_bar_ticks else 0.0

    # Per-strategy contribution: realized P&L (closed trades) + unrealized (still
    # open, marked to market), trade count, and share of the portfolio's TOTAL
    # P&L. realized + unrealized across all strategies sums EXACTLY to net_pnl.
    contributions = []
    for strat in strategies:
        sid = strat["id"]
        mine = [t for t in closed if t.strategy_id == sid]
        my_wins = [t for t in mine if (t.pnl or 0) > 0]
        realized = round(sum(t.pnl or 0 for t in mine), 2)
        unrealized = round(unrealized_by_strat.get(sid, 0.0), 2)
        total = round(realized + unrealized, 2)
        contributions.append(
            {
                "strategy_id": sid,
                "strategy_name": strat.get("name", ""),
                "realized_pnl": realized,
                "unrealized_pnl": unrealized,
                "trades": len(mine),
                "wins": len(my_wins),
                "win_rate": round(len(my_wins) / len(mine) * 100, 1) if mine else None,
                # Share of the portfolio's total P&L. Sign-preserving, so a losing
                # sleeve inside a winning book shows a negative share.
                "share_pct": round(total / net_pnl * 100, 1) if net_pnl else None,
            }
        )

    all_symbols = {s: series for m in prepared_by_strategy.values() for s, series in m.items()}
    # The rates that actually applied, by asset class present in the book. The
    # scalar `fee_pct_per_side` mirrors the single-strategy result and is only a
    # number when the whole book pays one rate — a mixed book has no single rate,
    # and printing one of the two would be a smaller lie but still a lie.
    rates_by_class = {
        s["asset_class"]: round(fee_rate_by_sid[s["id"]] * 100, 6) for s in strategies
    }
    distinct_rates = set(rates_by_class.values())
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
        # What the commissions took, in dollars — the number that decides whether
        # a book this busy can carry its own costs. Present on every run (0.0 on a
        # stock book) so a caller can tell "no fees were charged" from "this
        # backtest doesn't model fees", which is exactly what was indistinguishable
        # before an all-crypto portfolio could be charged at all.
        "fees_paid": round(fees_paid, 2),
        "fee_pct_per_side": next(iter(distinct_rates)) if len(distinct_rates) == 1 else None,
        "fee_pct_per_side_by_class": rates_by_class,
        "rails_seeded": rails_seeded,  # see run_backtest
        "exit_model": "poller" if poller_view else "intrabar",  # see run_backtest
        "bar_seconds": bar_seconds,
        "median_bar_move_pct": bar_move_pct,  # see _median_bar_move_pct
        # One block per RANKED strategy in the book — the top-N cut it made, and
        # whether it could be made at all. Empty when nothing in the book ranks.
        "ranking": [
            {"strategy_id": sid, "strategy_name": strat_by_id[sid].get("name", ""), **r.report()}
            for sid, r in rankers.items()
        ],
        "max_deployed_usd": round(max_deployed, 2),
        "pct_capital_deployed": pct_deployed,
        "return_on_deployed_pct": return_on_deployed,
        "time_in_market_pct": time_in_market,
        "realized_total": round(sum(t.pnl or 0 for t in closed), 2),
        "open_positions": open_positions,
        "contributions": contributions,
        "strategy_count": len(strategies),
        "strategy_names": [s.get("name", "") for s in strategies],
        "equity_days": days_index,
        "equity": [round((v / starting_cash - 1) * 100, 2) for _, v in equity_curve],
        "bar_gaps": _bar_gaps(days_index, market),  # see the single-strategy note
        "hold_benchmark": _hold_benchmark(all_symbols, days_index),
        "hold_benchmark_label": (
            list(all_symbols)[0] if len(all_symbols) == 1
            else f"{len(all_symbols)} symbols (equal weight)"
        ),
        "trade_list": [
            {
                "symbol": t.symbol, "qty": t.qty,
                "strategy_id": t.strategy_id, "strategy_name": t.strategy_name,
                "entry_price": _price(t.entry_price), "entry_at": t.entry_at.isoformat(),
                "entry_day": day_of(t.entry_at),
                "entry_reason": t.entry_reason,
                "rank": t.rank, "rank_of": t.rank_of,  # see run_backtest's trade_list
                "exit_price": _price(t.exit_price or 0),
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
    from qt.services import barfetch  # local: barfetch pulls in the cache layer

    symbol = "SPY" if asset_class == "stock" else "BTC/USD"
    day_of = _day_fn(market)
    # Cached too. One symbol is cheap, but it's a year of SPY re-downloaded on
    # every single backtest, and it's the same daily series the strategies use.
    # Safe because a cached daily bar is re-stamped to a PRE-MARKET time (see
    # barfetch.DAILY_STAMP), so it lands in the same day bucket as a fresh one —
    # a benchmark line landing a day off the equity curve is exactly the kind of
    # quiet wrongness this codebase keeps having to dig out.
    bars = await barfetch.fetch_bars(client, [symbol], asset_class, "1Day", start_iso)
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
