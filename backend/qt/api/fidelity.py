"""Is the backtester telling the truth? Point it at a period you already traded.

A DEVELOPMENT AND VALIDATION TOOL, not part of the daily trading loop. Its job is
to earn (or withdraw) trust in the replay: run the same strategy over a window
you have real trades for, and diff the two. Once the replay is trusted this page
goes quiet and stays quiet — which is the point. It is the instrument, not the
machine.

Two endpoints:

  POST /api/fidelity/compare — replay a window this instance has traded, and diff.
  GET  /api/fidelity/export  — the journal for a window as plain JSON, so a
                               separate (production) instance can hand its real
                               trades to a development one for the same diff.

The export deliberately carries trades and nothing else: no keys, no account
numbers, no settings. It leaves one machine and lands on another, so it should be
boring to lose.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from qt.api.backtest import (
    MAX_HOURS_FOR_MINUTE_REPLAY,
    MIN_HOURS_FOR_DAILY_REPLAY,
    BacktestBody,
    _mixed_resolution,
    _strategy_symbols,
    _uses_daily_only_signals,
    replay,
    replay_strategy,
    run,
)
from qt.api.market import require_client
from qt.broker.alpaca import AlpacaClient
from qt.db import get_session
from qt.api import baskets as baskets_api
from qt.models import AuditLog, BasketItem, Strategy, StrategyConfigVersion, Trade
from qt.services import backtest, fidelity
from qt.services.backtest import _day_fn
from qt.services.engine import get_risk
from qt.timeutil import utc_aware

log = logging.getLogger("qt.api.fidelity")

router = APIRouter(prefix="/api/fidelity", tags=["fidelity"])


class CompareBody(BaseModel):
    strategy_id: int
    # Optional, and normally left alone. The window that makes sense is "since
    # this strategy started trading" — asking the user for a number invites one
    # that is wrong in the only direction that matters (too long), and the answer
    # is already in the journal. Kept so an older frontend still works, and as a
    # cap for anyone who deliberately wants less.
    days: int | None = Field(default=None, ge=1, le=730)
    # An explicit window, which `days` cannot express: the useful comparison on a
    # heavily edited strategy is a stretch BETWEEN edits, and that stretch ended
    # in the past. Given both, `days` is ignored.
    window_start: datetime | None = None
    window_end: datetime | None = None
    # Which real history to judge against. Paper is the honest default: it has
    # the volume, and DECISION fidelity is exactly as testable there as live.
    # Execution fidelity is not — Alpaca's paper fills are simulated, so
    # calibrating costs against them would bake in a fiction. See the note the
    # response carries.
    mode: str = Field(default="paper", pattern="^(paper|live|shadow)$")
    # Trades exported from another instance. When present these are compared
    # instead of this instance's own journal — the prod → dev path.
    imported_trades: list[dict] | None = None


def _aware(ts: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes even for timezone-aware columns, and
    comparing one against an aware `since` raises. Everything QT stores is UTC,
    so saying so is a restatement rather than an assumption.

    The rule lives in qt.timeutil now — one definition for the whole API, which
    is also what stamps the offset on every timestamp the API emits."""
    return utc_aware(ts)


def _day_of(asset_class: str):
    """The SAME day boundary the replay buckets by — ET for stocks, UTC for
    crypto — reused from the backtester rather than re-derived here. Matching is
    by day, so both sides must agree what a day IS; roll one of them and a trade
    near midnight lands in a different bucket and reads as a mismatch that never
    happened."""
    return _day_fn("crypto" if asset_class == "crypto" else "stock")


def _basket_drift(session: Session, strategy: Strategy, live_rows: list[dict]) -> tuple[list[dict], int]:
    """Whether the basket's MEMBERS changed, and how often, across these trades.

    Snapshots are timestamp-exact — an edit at 14:32 is recorded at 14:32, so a
    trade at 14:00 resolves to the old membership and one at 15:00 to the new.
    Nothing is rounded to a day.

    The anchor is the EARLIEST trade, not the latest. Comparing today against the
    most recent trade answers a narrower question than it appears to: a basket
    edited early in the window and left alone since would show no difference at
    all, while every trade before that edit ran on a different list.

    The count is the other half. Membership that changed and changed back reads
    as identical from any two points, so the number of snapshots taken during the
    window is the only thing that can say the universe moved underneath these
    trades. Returned separately because it is a different claim from "these two
    lists differ", not a stronger version of it.
    """
    if strategy.universe != "basket" or not strategy.basket_id:
        return [], 0
    from qt.models import BasketVersion

    earliest = (
        session.query(func.min(Trade.entry_at))
        .filter(Trade.strategy_id == strategy.id, Trade.entry_at.isnot(None))
        .scalar()
    )
    if earliest is None:
        return [], 0  # imported trades carry no timestamps — nothing to key on
    since = _aware(earliest)
    changes_during = (
        session.query(func.count(BasketVersion.id))
        .filter(
            BasketVersion.basket_id == strategy.basket_id,
            BasketVersion.created_at > since,
        )
        .scalar()
        or 0
    )
    then = baskets_api.members_at(session, strategy.basket_id, since)
    if then is None:
        return [], changes_during
    then_set = {(m["asset_class"], m["symbol"]) for m in then}
    now_set = {
        (i.asset_class, i.symbol)
        for i in session.query(BasketItem).filter(BasketItem.basket_id == strategy.basket_id)
    }
    if then_set == now_set:
        return [], changes_during
    added = sorted(s for _, s in now_set - then_set)
    removed = sorted(s for _, s in then_set - now_set)
    return (
        [
            {
                "field": "Basket members",
                "then": f"{len(then_set)} symbols"
                + (f" (since removed: {', '.join(removed)})" if removed else ""),
                "now": f"{len(now_set)} symbols"
                + (f" (since added: {', '.join(added)})" if added else ""),
            }
        ],
        changes_during,
    )


def _serialize_current(strategy: Strategy) -> dict:
    """Today's config in the same shape a version snapshot holds, so the two can
    be diffed field by field rather than eyeballed — and so the same reader
    (qt.api.backtest.replay_strategy) can turn either into a replay."""
    return {
        "asset_class": strategy.asset_class,
        "universe": strategy.universe,
        "symbols": json.loads(strategy.symbols) if strategy.symbols else [],
        "basket_id": strategy.basket_id,
        "rank_by": strategy.rank_by,
        "top_n": strategy.top_n,
        "rank_enabled": bool(strategy.rank_enabled),
        "sizing_usd": strategy.sizing_usd,
        "sleeve_usd": strategy.sleeve_usd,
        "max_positions": strategy.max_positions,
        "swing_mode": strategy.swing_mode,
        "ignore_regime": strategy.ignore_regime,
        "params": json.loads(strategy.params),
    }


# ---------------------------------------------------------------------------
# SEGMENTATION
#
# A single replay uses ONE configuration. Edit a strategy — or the basket it
# points at — partway through the period being compared and no single
# configuration is faithful to all of those trades: the ones before the edit ran
# on settings the replay isn't using, and the report's mismatches are that rather
# than anything wrong with the backtester.
#
# So the window is CUT at the moments the configuration changed, and each stretch
# is replayed with the configuration that was actually live during it.
#
# WHAT SEGMENTING CANNOT DO, and why it is said out loud rather than papered over:
#
#   Each segment's replay starts with FRESH CASH AND NO POSITIONS. Real trading
#   did not restart at those moments. So a position carried across a boundary is
#   invisible to the segment that inherited it, and a segment that begins with a
#   full wallet has room the live account did not. Both push the same way — the
#   replay looks freer than reality — and neither is a fault in the backtester.
#
#   A trade opened in one segment and closed in another cannot be reproduced by
#   any of them. Those are counted and set aside, exactly like a hand-closed
#   exit: the ENTRY was a real decision and still counts, but scoring the exit
#   against a replay that stopped watching would report the exit logic as broken
#   when it was never given the chance.
#
# Which is why the number of cuts is capped. Two or three long stretches are
# worth the boundary cost; ten short ones are mostly boundary, and the report
# says it declined to segment rather than hand back a worse answer confidently.
# ---------------------------------------------------------------------------

MAX_SEGMENTS = 8

# And how many DAY-SIZED pieces a window may be cut into so that the stretches
# holding trades keep minute bars (see _chunk_for_minute_replay). A separate
# number from MAX_SEGMENTS because it answers a separate question: that one is
# about how many fresh-account boundaries a comparison can absorb before the
# boundaries are the finding, this one is about how many replays — each with its
# own dataset, its own fetch, its own day of 1,440 bars per symbol — a single
# click may set off. A fortnight of daily trading is generous for the question
# minute bars answer ("did the replay see the fill I actually got"); past that
# the window is asking about behaviour in general, and 15-minute bars answer
# that. Older stretches are coalesced and the response says how many.
MAX_MINUTE_CHUNKS = 14


@dataclass
class _Segment:
    """One stretch of the compared window, and the configuration live during it."""

    start: datetime
    end: datetime
    config: dict            # a StrategyConfigVersion snapshot (or today's config)
    version_no: int | None  # None when nothing was recorded that early
    symbols: list[str]
    scanner_replay: bool
    # False when the universe of that moment could not be reconstructed — a
    # basket with no snapshot that old, or a watchlist, which is not versioned at
    # all. The segment is still replayed, with today's list, and says so.
    universe_known: bool = True
    # Where the REPLAY is allowed to start trading, when that differs from where
    # this stretch begins for the purpose of filing trades. They differ exactly
    # once: on the day the strategy went live. Trades are matched by DAY, so the
    # stretch has to open at midnight or that day's real trades fall outside it —
    # but a replay that also opens at midnight gets the morning to itself and
    # buys things the engine, not yet running, never saw. Those land as "the
    # backtest invented a trade", which is the exact false verdict this window
    # exists to prevent.
    replay_start: datetime | None = None
    # The after-loss cooldowns in force when THIS stretch opened (see
    # _prior_losses). Per segment rather than per comparison: a loss taken in
    # stretch one is inside the window but strictly before stretch two, and
    # stretch two's replay starts flat and would otherwise buy through it.
    rails_seed: dict[str, datetime] = field(default_factory=dict)
    live: list[dict] = field(default_factory=list)
    # True when this stretch exists only because a long window was cut down to
    # something a minute replay can cover (see _chunk_for_minute_replay), not
    # because anything about the strategy changed. `segmented` in the report
    # means "your configuration changed mid-window" and must not start firing
    # for every comparison that happens to be older than a day.
    resolution_chunk: bool = False
    result: dict | None = None
    error: str | None = None


def _replay_shape(snapshot: dict) -> str:
    """Everything about a config that can change what a replay DOES, as one
    comparable value.

    A config version is written on EVERY save, including one that only renamed
    the strategy or edited its notes. Cutting the window at a rename would split
    the comparison — and pay the fresh-cash cost of a boundary — for a change
    that cannot move a single trade. Only a difference here is a real boundary."""
    return json.dumps(
        {
            "replay": replay_strategy(snapshot),
            "universe": snapshot.get("universe"),
            "basket_id": snapshot.get("basket_id"),
            "symbols": sorted(snapshot.get("symbols") or []),
        },
        sort_keys=True,
        default=str,
    )


def _config_timeline(
    session: Session, strategy: Strategy, since: datetime, until: datetime
) -> list[tuple[datetime, dict, int | None]]:
    """(moment, config, version_no) for every configuration in force during the
    window, oldest first. The first entry always starts at `since`, so a window
    is always covered end to end even when nothing was recorded before it."""
    rows = (
        session.query(StrategyConfigVersion)
        .filter(StrategyConfigVersion.strategy_id == strategy.id)
        .order_by(StrategyConfigVersion.created_at, StrategyConfigVersion.version_no)
        .all()
    )
    at_start = [r for r in rows if (_aware(r.created_at) or since) <= since]
    if at_start:
        opening = (json.loads(at_start[-1].snapshot), at_start[-1].version_no)
    elif rows:
        # Nothing recorded before the window opened, usually because the strategy
        # was CREATED inside it. The earliest snapshot — the strategy as it first
        # existed — is the closest thing there is to what was live at the start.
        # Today's config would be the FURTHEST thing from it, and using it would
        # manufacture a boundary at the strategy's own creation and then replay
        # today's settings over the stretch before it existed.
        opening = (json.loads(rows[0].snapshot), rows[0].version_no)
    else:
        # No history at all: these trades predate versioning. Today's config is
        # the only thing there is, and the drift report already says as much.
        opening = (_serialize_current(strategy), None)
    timeline: list[tuple[datetime, dict, int | None]] = [(since, *opening)]
    for row in rows:
        created = _aware(row.created_at)
        if created is None or not (since < created < until):
            continue
        snapshot = json.loads(row.snapshot)
        if _replay_shape(snapshot) != _replay_shape(timeline[-1][1]):
            timeline.append((created, snapshot, row.version_no))
    return timeline


def _basket_cuts(
    session: Session, strategy: Strategy, since: datetime, until: datetime
) -> list[datetime]:
    """Moments inside the window when the basket's MEMBERSHIP actually changed.

    Separate from the config timeline because a basket edit leaves the strategy's
    own config version byte-identical — that is the whole reason baskets are
    versioned. Membership is compared as a set, filtered to the strategy's asset
    class, so adding a crypto name to a basket a stock strategy uses is correctly
    not a boundary."""
    if strategy.universe != "basket" or not strategy.basket_id:
        return []
    from qt.models import BasketVersion

    def members(snapshot: str) -> frozenset[str]:
        return frozenset(
            m["symbol"]
            for m in json.loads(snapshot)
            if m.get("asset_class") == strategy.asset_class
        )

    at_start = baskets_api.members_at(session, strategy.basket_id, since)
    previous = (
        frozenset(
            m["symbol"] for m in at_start if m.get("asset_class") == strategy.asset_class
        )
        if at_start is not None
        else None
    )
    cuts: list[datetime] = []
    for row in (
        session.query(BasketVersion)
        .filter(BasketVersion.basket_id == strategy.basket_id)
        .order_by(BasketVersion.created_at, BasketVersion.version_no)
        .all()
    ):
        created = _aware(row.created_at)
        if created is None or not (since < created < until):
            continue
        now_members = members(row.snapshot)
        # `previous is None` = the membership before the window is unknown, so we
        # cannot tell whether this edit changed anything. Treat it as a boundary:
        # a cut we didn't need costs a reset, a cut we missed silently replays
        # the wrong universe over half the trades.
        if previous is None or now_members != previous:
            cuts.append(created)
        previous = now_members
    return cuts


def _symbols_at(
    session: Session, strategy: Strategy, config: dict, when: datetime
) -> tuple[list[str], bool, bool]:
    """(symbols, scanner_replay, universe_known) for a config at a moment.

    `universe_known` is False when the list had to be substituted with today's:
    a basket with no snapshot that far back, or a watchlist, which nothing
    versions. The segment still runs — a replay of the wrong universe is at least
    a replay — but the report must not present it as a reconstruction."""
    universe = config.get("universe")
    asset_class = config.get("asset_class") or strategy.asset_class
    if universe == "custom":
        return [s.strip().upper() for s in (config.get("symbols") or []) if s.strip()], False, True
    if universe == "basket" and config.get("basket_id"):
        members = baskets_api.members_at(session, config["basket_id"], when)
        if members is None:
            return _strategy_symbols(session, strategy), False, False
        return (
            sorted({m["symbol"] for m in members if m.get("asset_class") == asset_class}),
            False,
            True,
        )
    if universe == "scanner":
        # The scanner replays each day's real cached risers, so its universe is
        # reconstructed from history already — nothing to substitute.
        return [], True, True
    if universe == "both":
        # Scanner AND watchlist. Scanner-replayed, with the watchlist alongside —
        # but the watchlist itself is not versioned, so what it held back then is
        # unknown and this is not a faithful reconstruction.
        return _strategy_symbols(session, strategy), True, False
    return _strategy_symbols(session, strategy), False, False  # watchlist


def _build_segments(
    session: Session, strategy: Strategy, since: datetime, until: datetime
) -> list[_Segment]:
    """Cut the window at every moment the configuration or the basket changed."""
    timeline = _config_timeline(session, strategy, since, until)
    cuts = sorted({moment for moment, _, _ in timeline} | set(_basket_cuts(session, strategy, since, until)))
    segments: list[_Segment] = []
    for start, end in zip(cuts, cuts[1:] + [until]):
        if end <= start:
            continue  # two edits in the same second leave nothing between them
        config, version_no = next(
            (cfg, no) for moment, cfg, no in reversed(timeline) if moment <= start
        )
        symbols, scanner, known = _symbols_at(session, strategy, config, start)
        segments.append(
            _Segment(
                start=start, end=end, config=config, version_no=version_no,
                symbols=symbols, scanner_replay=scanner, universe_known=known,
            )
        )
    return segments


def _merge_chunk_results(first: dict | None, second: dict | None) -> dict | None:
    """Two day-sized chunks of ONE configuration, as one result for reporting.

    Only the fields `_segment_rows` reads need to be right here: the trades (so a
    stretch is not reported empty because its trades landed in a later chunk) and
    the no-trade reason, which may only be claimed when NEITHER chunk traded."""
    if first is None:
        return second
    if second is None:
        return first
    trades = (first.get("trade_list") or []) + (second.get("trade_list") or [])
    positions = (first.get("open_positions") or []) + (second.get("open_positions") or [])
    diagnosis = first.get("diagnosis") if (first.get("diagnosis") or {}).get("summary")         else second.get("diagnosis")
    return {
        **first, "trade_list": trades, "open_positions": positions,
        # A reason explains a silent run. If either chunk traded, the stretch did.
        "diagnosis": (diagnosis if not (trades or positions) else None),
    }


def _config_stretches(segments: list[_Segment]) -> list[_Segment]:
    """The segments as the READER understands them: one per configuration, with
    any day-sized resolution chunks folded back into the stretch they came from.

    The replay needs the chunks (see _chunk_for_minute_replay); the report must
    not show them. "Your strategy changed here" and "we cut the window so we
    could keep minute bars" are different statements, and a change log that
    listed the second as the first would invent edits the user never made."""
    out: list[_Segment] = []
    for segment in segments:
        if segment.resolution_chunk and out:
            # The chunks' RESULTS have to be merged, not just their spans. Keeping
            # the first chunk's result reported a stretch as having traded nothing
            # whenever its trades happened to fall in a later chunk — the same
            # per-piece-value-presented-as-the-whole fault this file has now been
            # bitten by three times.
            merged = _merge_chunk_results(out[-1].result, segment.result)
            out[-1] = replace(
                out[-1], end=segment.end,
                live=out[-1].live + segment.live,
                result=merged,
                error=out[-1].error or segment.error,
            )
        else:
            out.append(segment)
    return out


def _cutting_buys_minute_bars(segment: _Segment) -> bool:
    """Would cutting THIS stretch to day-sized pieces actually get minute bars?

    Only a stretch already being replayed on 15-minute bars has anything to gain:
    cut it down and the pieces qualify for 1Min, which is the same bar family
    fifteen times finer. Everything else is cut for nothing, or worse.

    A DAILY replay is the "worse". `_timeframe_for` puts a window shorter than
    MIN_HOURS_FOR_DAILY_REPLAY on intraday bars because a daily bar is stamped at
    the start of its day and a window of hours contains none — so cutting a
    month-long daily comparison into 24-hour pieces does not refine it, it
    converts it, and every piece then asks for intraday bars a daily strategy has
    no data for and no business trading on. Measured the moment chunking first
    reached the replay: four segment tests whose strategies replay daily went
    from matching their trades to matching none of them."""
    params = replay_strategy(segment.config).get("params") or {}
    hours = (segment.end - segment.start).total_seconds() / 3600
    return (
        _timeframe_for(params, hours) == "15Min"
        and _timeframe_for(params, MAX_HOURS_FOR_MINUTE_REPLAY) == "1Min"
    )


def _floor_boundaries_to_bars(segments: list[_Segment]) -> list[_Segment]:
    """Open each stretch at the START OF A BAR, not at the instant the
    configuration changed.

    MEASURED ON STRATEGY 25, and it cost three real trades. Config version 2 was
    written at 13:58:05.173; MSFT filled at 13:58:58.131, fifty-three seconds
    later on the same minute bar. The window is cut at every config change, so
    the second stretch replayed from 13:58:05 — five seconds INTO that bar. A
    replay grades a bar when it CLOSES and can only act on bars beginning at or
    after its start, so the 13:58 bar was unreachable. Both MSFT and AMZN enter
    on it (measured directly at 1Min: both at 13:59:00, which is that bar
    closing). No resolution could have saved those trades; they were outside the
    replay's reach before the bar size was ever chosen.

    `_replay_floor` has always said this about go-live — "gating at 14:30:07
    skips the bar that CAUSED that first trade" — and was only ever applied
    there. A configuration changes at a wall-clock instant; bars do not.

    The bar size is the one the stretch will REALLY be replayed at, which for a
    stretch about to be chunked is the chunk's, not the whole stretch's. Getting
    that wrong the generous way is worse than not flooring at all: a 28-hour
    stretch floored as if it were 15-minute bars opens thirteen minutes before
    the edit, and anything the replay buys there is bought under a configuration
    that was not yet in force.

    The previous stretch keeps its END, so the two overlap by seconds. That is
    deliberate and it is where trades in those seconds belong: `_place_trades`
    files a trade under the FIRST stretch that contains it, so the seconds
    before the edit still count against the config that was actually live. And
    the earlier stretch cannot double-count the shared bar — it ends before that
    bar closes.

    NOT for a stretch replayed on DAILY bars, and that exclusion is the whole
    reason this is bounded rather than general. A daily bar is stamped at the
    start of its day, so flooring an afternoon edit to that bar hands the replay
    the entire morning — up to 24 hours under a configuration that was not yet in
    force, to spare it a fraction of one decision. Caught by the rail-seed test:
    a boundary floored back over a loss that had happened three hours before it
    stopped being a loss the stretch had to carry. The correction is worth making
    only while it is smaller than the thing being corrected — at most one
    intraday bar, never a day."""
    out = [segments[0]] if segments else []
    for previous, segment in zip(segments, segments[1:]):
        params = replay_strategy(segment.config).get("params") or {}
        span = (segment.end - segment.start).total_seconds() / 3600
        hours = (
            min(span, MAX_HOURS_FOR_MINUTE_REPLAY)
            if _cutting_buys_minute_bars(segment)
            else span
        )
        timeframe = _timeframe_for(params, hours)
        if timeframe == "1Day":
            out.append(segment)
            continue
        # Never back past the previous stretch's own start: two edits inside one
        # bar would otherwise reorder the boundaries they came from.
        floored = max(previous.start, _bar_floor(segment.start, timeframe))
        out.append(replace(segment, start=floored))
    return out


def _chunk_for_minute_replay(
    segments: list[_Segment], live_rows: list[dict]
) -> list[_Segment]:
    """Cut long stretches down to something a MINUTE replay can cover.

    The live engine decides every 60 seconds. `_timeframe_for` only asks for
    minute bars while a stretch fits inside MAX_HOURS_FOR_MINUTE_REPLAY, and a
    comparison's window grows for as long as the strategy stays switched on — so
    a strategy live for a day and a bit silently drops to 15-minute bars and
    starts sampling fifteen times less often than the thing it is grading.

    Measured, and this is the whole reason: strategy 25 compared clean at 1Min
    with a 12-hour window; overnight the same stretch reached 26 hours, fell to
    15Min, and three real trades came back as "the replay missed it — this is
    the kind that points at a real bug". Nothing about the strategy had changed.
    The bar size had.

    Cutting EVERY long stretch into day-sized pieces would be the obvious fix and
    the wrong one: minute bars run 1,440 per symbol per day, so a strategy live
    for three months would ask for millions of them to compare a handful of
    trades. Instead only the pieces that CONTAIN a real trade are kept at day
    size — those are the only places a comparison has anything to say — and
    adjacent trade-free pieces are coalesced back into one long stretch, which
    keeps its coarse bars because there is nothing in it to compare.

    And only stretches that would GAIN minute bars are cut at all. A daily replay
    cut into 24-hour pieces is not refined but converted, onto intraday bars its
    strategy has no signals for. See _cutting_buys_minute_bars.

    So the cost tracks how often you traded, not how long the strategy has been
    enabled — bounded by MAX_MINUTE_CHUNKS, because "how often you traded" is
    unbounded too. Config stretches are never merged ACROSS: `replace` keeps each
    piece's own config, universe and version, so a stretch that was replayed
    under version 2 stays under version 2."""
    cap = timedelta(hours=MAX_HOURS_FOR_MINUTE_REPLAY)
    entries = sorted(
        e for e in (_parse(r.get("entry_at")) for r in live_rows if r.get("entry_at"))
        if e is not None
    )
    # Every stretch that has something to gain, cut into day-sized pieces.
    cut: list[tuple[_Segment, list[tuple[datetime, datetime, bool]]]] = []
    for segment in segments:
        if not _cutting_buys_minute_bars(segment):
            cut.append((segment, [(segment.start, segment.end, False)]))
            continue
        # No early return for a SHORT stretch: the loop below yields exactly one
        # piece for it anyway. A guard there was tried and could not be made to
        # fail — it was a shortcut, not a rule, and a branch that cannot be wrong
        # is a branch that hides the one that can.
        pieces: list[tuple[datetime, datetime, bool]] = []
        start = segment.start
        while start < segment.end:
            end = min(start + cap, segment.end)
            pieces.append((start, end, any(start <= e < end for e in entries)))
            start = end
        cut.append((segment, pieces))

    # HOW MANY OF THOSE MAY STAY SMALL. Each one is a separate replay — its own
    # dataset, its own fetch, its own day of minute bars per symbol — so a
    # strategy that has traded daily since May would ask for ninety of them and
    # spend a quarter of an hour doing it. The NEWEST are kept: a comparison is
    # nearly always being read about a strategy running now. Older trades are
    # still compared, on whatever bars their coalesced stretch gets, and the
    # response says how many were left coarse rather than letting the report
    # imply the whole window was graded at 60 seconds.
    traded_pieces = sorted(
        (piece for _, pieces in cut for piece in pieces if piece[2]), key=lambda p: p[0]
    )
    keep = {p[0] for p in traded_pieces[-MAX_MINUTE_CHUNKS:]}

    out: list[_Segment] = []
    for segment, pieces in cut:
        merged: list[tuple[datetime, datetime]] = []
        for start, end, _ in pieces:
            # A run of pieces that are not being kept small becomes ONE piece:
            # there is nothing in it to compare at minute resolution, so paying
            # for minute bars over it would buy nothing.
            if start not in keep and merged and merged[-1][0] not in keep:
                merged[-1] = (merged[-1][0], end)
            else:
                merged.append((start, end))
        out.extend(
            # `live` and `rails_seed` are handed out FRESH, and this is not
            # defensive tidying: dataclasses.replace copies field VALUES, so
            # every piece cut from one stretch was given the same empty list and
            # the same empty dict. _place_trades then appended each trade to what
            # looked like one chunk's journal and was in fact all of them, so
            # every chunk was seeded with every trade, `live_trades` per stretch
            # counted the window's total, and a failure recorded on one chunk
            # reached the trades of its neighbours. Invisible to a unit test
            # holding the pieces still and reading only their spans.
            replace(segment, start=a, end=b, resolution_chunk=i > 0, live=[], rails_seed={})
            for i, (a, b) in enumerate(merged)
        )
    return out


def _resolution_report(stretches: list[tuple[str | None, int]]) -> dict:
    """Which bars actually graded this comparison, and how much of it they missed.

    `report["timeframe"]` is one string for a window that can now hold stretches
    replayed at different sizes — the pieces holding trades keep minute bars, the
    trade-free remainder and anything past MAX_MINUTE_CHUNKS does not. Quoting
    the busiest stretch's size over the whole run would say "1Min" about a report
    whose older half was graded fifteen times more coarsely, and the reader would
    take a mismatch there for a signal difference.

    Read off what each stretch REPORTED rather than what was asked for: the
    replay drops back to 15-minute bars when minute ones would cover less of the
    universe, and a section claiming a resolution nothing achieved is worse than
    no section at all.

    `stretches` is (timeframe or None, live trades in it) per replayed stretch."""
    coarse = [(tf, n) for tf, n in stretches if tf and tf != "1Min"]
    return {
        "minute_stretches": sum(1 for tf, _ in stretches if tf == "1Min"),
        "coarser_stretches": len(coarse),
        # The number that matters: trades graded by bars fifteen times coarser
        # than the 60-second engine that made them. A "the replay missed it" on
        # one of these is a resolution artefact until proven otherwise.
        "live_trades_graded_coarse": sum(n for _, n in coarse),
    }


def _change_log(segments: list[_Segment], live_rows: list[dict]) -> list[dict]:
    """Each moment the configuration or basket changed, with what moved and how
    many real trades were made under the stretch that followed.

    Werner asked for the edits to appear alongside the trades, and the reason is
    sound: "48 trades the backtest invented" is unreadable as a flat list, while
    "the universe widened on the 24th, and 30 of them are after that" is a
    finding. The count per stretch is what turns a date into an explanation."""
    log: list[dict] = []
    segments = _config_stretches(segments)
    for previous, segment in zip(segments, segments[1:]):
        traded = sum(
            1
            for r in live_rows
            if (entry := _parse(r.get("entry_day"))) is not None
            and segment.start <= entry < segment.end
        )
        log.append(
            {
                "at": _iso(segment.start),
                "changed": fidelity.config_drift(previous.config, segment.config)[:4],
                "live_trades_after": traded,
                "version_no": segment.version_no,
            }
        )
    return log


def _stable_window(segments: list[_Segment], live_rows: list[dict]) -> dict | None:
    """The longest stretch with no edit at all, and how many real trades it holds.

    With 21 edits in 90 days there is no stable strategy to validate — every
    comparison is measuring the edits. Rather than leave the user to work out
    which dates to type, find the longest unedited stretch that actually traded
    and hand it over. Needs at least two trades: a window with one is a smaller
    anecdote, not a better comparison.

    None when nothing qualifies, because proposing a window that would report
    the same emptiness is worse than proposing nothing.

    Reads the COALESCED view: a day-sized piece cut so a long window could keep
    minute bars is not a stretch anybody edited around, and offering one as "the
    longest stretch with no edit" would hand the reader a 24-hour window and call
    it a finding about their configuration."""
    best: dict | None = None
    for segment in _config_stretches(segments):
        traded = [
            r
            for r in live_rows
            if (entry := _parse(r.get("entry_day"))) is not None
            and segment.start <= entry < segment.end
            and r.get("status") in ("open", "closed")
        ]
        if len(traded) < 2:
            continue
        days = max(1, round((segment.end - segment.start).total_seconds() / 86400))
        # Ranked by TRADES, not by length: a long quiet stretch proves less than
        # a short busy one, and the sample size is what the verdict rests on.
        if best is None or len(traded) > best["live_trades"]:
            best = {
                "start": _iso(segment.start),
                "end": _iso(segment.end),
                "days": days,
                "live_trades": len(traded),
            }
    return best


def _place_trades(
    segments: list[_Segment], live_rows: list[dict]
) -> tuple[int, list[dict]]:
    """File each live trade under the segment its ENTRY fell in, and mark the ones
    whose exit landed in a later one. Returns (how many crossed, the rows that
    could not be filed).

    The entry is what decides the segment, because the entry is the decision the
    configuration produced. A trade that then closed after the boundary is
    flagged rather than dropped: its entry is real evidence, and only its exit is
    beyond any single segment's reach.

    A REJECTED row never filled, so it has no moment to file under — and it must
    still reach the comparison, because it is the only thing that tells a trade
    the backtest invented from one the engine wanted and a rail refused. Those
    come back as leftovers rather than being dropped."""
    crossed = 0
    unplaced: list[dict] = []
    for row in live_rows:
        entered = _parse(row.get("entry_at"))
        segment = (
            next((s for s in segments if s.start <= entered < s.end), None)
            if entered is not None
            else None
        )
        if segment is None:
            unplaced.append(row)
            continue
        exited = _parse(row.get("exit_at"))
        spans = exited is not None and exited > segment.end
        if spans:
            crossed += 1
        segment.live.append({**row, "spans_segment_boundary": spans})
    return crossed, unplaced


def _parse(stamp: str | None) -> datetime | None:
    if not stamp:
        return None
    try:
        return _aware(datetime.fromisoformat(stamp))
    except ValueError:
        return None


def _bar_floor(moment: datetime, timeframe: str) -> datetime:
    """The start of the bar containing `moment`.

    This is where a replay may begin trading when the engine went live partway
    through a day, and both edges matter:

    Start at MIDNIGHT and the replay gets the whole morning to itself. If the
    symbol looked better at 10:00 than it did at 14:30, the backtest buys the
    10:00 opportunity the engine — not yet running — never saw, and the report
    calls it a trade the backtest invented. That is the same false verdict the
    window exists to prevent, merely compressed into day one.

    Start at the exact fill time and it is worse in the other direction: the bar
    that CAUSED that first trade began before the fill, so gating at 14:30:07
    skips it and the replay misses the very trade it is being judged against.

    The bar containing the moment is the only bound that admits the triggering
    bar and nothing earlier. On a daily replay the bar IS the day, so this
    reduces to midnight — and no head start is possible there, because a daily
    replay only gets one decision point for the whole day anyway."""
    if timeframe == "1Day":
        return moment.replace(hour=0, minute=0, second=0, microsecond=0)
    minutes = {"1Hour": 60, "1Min": 1}.get(timeframe, 15)
    floored = moment.replace(second=0, microsecond=0)
    return floored - timedelta(minutes=floored.minute % minutes)


def first_trade_at(session: Session, strategy: Strategy, mode: str) -> datetime | None:
    """When this strategy first did anything in this mode.

    The window must not begin before it. A 90-day comparison of a strategy that
    has been running for five days replays 85 days in which it did not exist,
    and every trade the replay takes in that stretch is counted as one it
    "invented" — dozens of them, all of which say nothing about the backtester
    and drown the handful of days that do."""
    # When the strategy ACTED, not when the row was written. They differ for a
    # trade reconciled after the fact, and `created_at` alone would then place
    # the strategy's first activity later than it really was and clip the window
    # past the trades it is supposed to include.
    filters = (Trade.strategy_id == strategy.id, Trade.mode == mode)
    stamps = [
        _aware(session.query(func.min(Trade.entry_at)).filter(*filters).scalar()),
        # Rejected rows never filled, so entry_at is null on exactly the rows
        # that prove the engine was running and being refused.
        _aware(session.query(func.min(Trade.created_at)).filter(*filters).scalar()),
    ]
    known = [s for s in stamps if s is not None]
    return min(known) if known else None


def activated_at(session: Session, strategy: Strategy) -> datetime | None:
    """The moment this strategy was switched ON.

    This is the honest start of a comparison for a strategy that has not traded
    yet. "It hasn't traded" is not a reason to refuse to answer: whether the
    REPLAY would have traded in the same stretch is exactly the question, and
    "neither of us did anything" is a real agreement rather than an error.

    Two sources, in order of trust. `enabled_at` is the record (migration 0014)
    and belongs to the row, so nothing done to the strategy afterwards — renaming
    included — can affect it.

    It is null only for strategies switched on before that column existed, and
    for those the audit line the toggle wrote is the sole surviving evidence.
    That line embeds the name the strategy had AT THE TIME, so matching today's
    name alone would lose the moment for anything renamed since. Every name the
    strategy has ever carried is recoverable from its config snapshots, so all
    of them are tried.

    Returns None when nothing was ever enabled, or when a legacy activation
    genuinely cannot be recovered (no audit line survives). Unknown is the right
    answer there: a confident wrong go-live time would shift every verdict in a
    report whose entire job is measuring accuracy."""
    if strategy.enabled_at is not None:
        return _aware(strategy.enabled_at)
    row = (
        session.query(AuditLog)
        .filter(
            AuditLog.category == "strategy",
            AuditLog.message.in_(
                [f"Strategy '{n}' ENABLED" for n in _historical_names(session, strategy)]
            ),
        )
        .order_by(AuditLog.at)
        .first()
    )
    return _aware(row.at) if row else None


def _historical_names(session: Session, strategy: Strategy) -> list[str]:
    """Every name this strategy has been known by, today's included.

    A config version is written on every save, and each snapshot carries the name
    as it stood — so the rename history is already recorded, just never read
    before now."""
    names = {strategy.name}
    for (snapshot,) in session.query(StrategyConfigVersion.snapshot).filter(
        StrategyConfigVersion.strategy_id == strategy.id
    ):
        try:
            name = json.loads(snapshot).get("name")
        except Exception:  # noqa: BLE001 — an unreadable snapshot is just one fewer name
            continue
        if name:
            names.add(name)
    return sorted(names)


def _journal_rows(
    session: Session, strategy: Strategy, since: datetime, until: datetime, mode: str
) -> list[dict]:
    """This instance's own trades for the window, shaped for the comparison.

    REJECTED rows are included on purpose. They are the only way to tell a trade
    the backtest invented from one the engine wanted and a rail refused, and
    those two mean opposite things about the replay."""
    day_of = _day_of(strategy.asset_class)
    rows = (
        session.query(Trade)
        .filter(Trade.strategy_id == strategy.id, Trade.mode == mode)
        .all()
    )
    out: list[dict] = []
    for t in rows:
        # A rejected row never filled, so it has no entry_at — fall back to when
        # it was written, or the window would silently drop exactly the rows that
        # tell a blocked trade from an invented one.
        stamp = _aware(t.entry_at) or _aware(t.created_at)
        # Bounded at BOTH ends now: comparing a past stretch against trades that
        # ran after it would count every later trade as one the backtest missed.
        if stamp is None or not (since <= stamp < until):
            continue
        out.append(
            {
                "symbol": t.symbol,
                "status": t.status,
                "entry_day": day_of(t.entry_at) if t.entry_at else None,
                "exit_day": day_of(t.exit_at) if t.exit_at else None,
                # Timestamps as well as days: matching is by day (a fill at
                # 14:03 and a 14:00 bar are the same decision), but the report
                # has to SHOW the times or "the backtest bought it a day late"
                # is indistinguishable from "an hour late".
                "entry_at": _iso(_aware(t.entry_at)),
                "exit_at": _iso(_aware(t.exit_at)),
                # The exact moments, not just the days. Matching stays day-based
                # (see the module docstring in qt.services.fidelity), but SPLITTING
                # the window at a config edit needs to know which side of 14:32 a
                # trade fell on. The comparison ignores these keys.
                "entry_at": _iso(t.entry_at),
                "exit_at": _iso(t.exit_at),
                "entry_price": t.entry_price,
                "exit_price": t.exit_price,
                "pnl": t.pnl,
                "entry_reason": t.entry_reason,
                "exit_reason": t.exit_reason,
                "config_version_id": t.config_version_id,
            }
        )
    return out


def _iso(ts: datetime | None) -> str | None:
    aware = _aware(ts)
    return aware.isoformat() if aware else None


def _intrabar_entry_at(live_rows: list[dict]) -> dict[str, list[datetime]]:
    """{symbol: the moments the engine really opened a position} — the evidence
    that lets a replay reconsider ONE bar.

    A fidelity comparison knows something no backtest does: this trade happened.
    So when the replay finds no entry at a bar's close on the very bar the
    journal says live filled, it may re-ask the same rules using that bar's HIGH
    — a rule satisfied inside the bar is a sampling difference, not a
    disagreement. Same reasoning as `_seed_by_day`, which removes the universe
    artefact for the same purpose: strip the differences you can prove are
    artefacts, so what is left is about the rules.

    Rejected rows are excluded. They never filled, so they are not evidence that
    anything was tradable — they are evidence a rail said no, which the replay
    applies for itself."""
    out: dict[str, list[datetime]] = {}
    for row in live_rows:
        if row.get("status") not in ("open", "closed"):
            continue
        moment = _parse(row.get("entry_at"))
        symbol = (row.get("symbol") or "").upper()
        if moment is not None and symbol:
            out.setdefault(symbol, []).append(moment)
    return out


def _seed_by_day(live_rows: list[dict]) -> dict[str, list[str]]:
    """{entry day: [symbols the live engine acted on that day]}.

    Handed to a scanner replay as `seed_by_day`, and the reason is narrow: the
    replay reconstructs its universe from the cached DAILY movers, ranked on a
    whole day's close-to-close move and re-filtered with TODAY'S scanner
    settings. The live crypto scanner ranks a ROLLING 24-hour figure recomputed
    every cycle, under the settings of the time. Those are different sets, so a
    coin the engine demonstrably bought at 20:57 can be absent from the replay's
    universe — and then the report says "the backtest missed six trades" when
    the backtest was never pointed at them.

    A symbol the engine ACTED on was in the engine's universe on that day. That
    is not an assumption, it is what the journal records. Seeding exactly those
    (symbol, day) pairs removes the universe difference and leaves the SIGNAL
    difference, which is the question this page exists to answer.

    Rejected rows count. A rail refusing a trade still proves the engine wanted
    it, which means the scanner had surfaced it. Per DAY rather than for the
    whole window on purpose: pinning a name for every day of a long comparison
    would hand the replay a universe far wider than the scanner ever had, and
    the invented trades that followed would be an artefact of this function."""
    out: dict[str, set[str]] = {}
    for row in live_rows:
        day, symbol = row.get("entry_day"), (row.get("symbol") or "").strip().upper()
        if day and symbol:
            out.setdefault(day, set()).add(symbol)
    return {day: sorted(symbols) for day, symbols in sorted(out.items())}


# The wash-sale window, restated from qt.services.engine._build_rail_context.
# It is the IRS's number, not a setting, which is why both places can hold it.
WASH_SALE_DAYS = 31


def _account_positions(
    session: Session, strategy: Strategy, since: datetime, until: datetime, mode: str
) -> list[dict]:
    """Positions the ACCOUNT held across this window that the replay cannot know
    about — the second half of the same job `_prior_losses` does for cooldowns.

    `check_rails` reads `open_positions_total`, `open_exposure_usd` and
    `already_open_symbol` as ACCOUNT-wide facts ("position already open for this
    symbol (any strategy)"), but a replay of ONE strategy fed its own numbers
    into all three. That makes it strictly freer than live, and freer means it
    buys names live refused — which the report then files against the backtester
    as a trade it invented.

    Two populations, deliberately disjoint from anything the replay opens itself,
    so nothing is counted twice:
      * every OTHER strategy's positions overlapping the window;
      * THIS strategy's positions that were already open when the window began —
        the replay starts flat, so live's already-open rail could not fire.

    Rejected rows are excluded BY STATUS, not by inference from `entry_at`: a
    refused order can carry one. Getting that wrong turns a journal of refusals
    into a full account and stops the replay trading at all — see the filter."""
    rows = (
        session.query(Trade)
        .filter(
            Trade.mode == mode,
            # A REFUSED ORDER IS A DECISION, NOT A HOLDING — and it can carry an
            # entry_at, so `entry_at is not null` does not imply it. This filter
            # sat here once, was deleted as "genuinely dead: no row can fail that
            # and pass this", and that claim was wrong: `open_trade`'s
            # did-not-fill path stamps an entry_at on a rejected row, which
            # `_account_entries` states in its own docstring twenty lines below.
            #
            # Measured on the owner's instance after it was removed: 4,612 such
            # rows, 4,492 of them overlapping at once against a position cap of
            # 50. Every candidate the replay looked at died at "rail: max open
            # positions reached" before the entry rules were read, on every bar
            # of every stretch, and the comparison reported three real trades as
            # "the replay missed it — this is the kind that points at a real
            # bug". The replay never got to have an opinion about them.
            #
            # It was deleted because mutating it could not make a test fail. The
            # test that covers this builds its rejected rows with a NULL
            # entry_at, so no fixture could tell the two clauses apart — a
            # surviving mutation means no TEST distinguishes the branch, never
            # that no DATA can.
            Trade.status != "rejected",
            # KEPT even though `entry_at < until` below already drops NULLs on
            # its own (SQL comparisons against NULL are never true). Relying on
            # NULL comparison semantics to enforce a rule this load-bearing is
            # too subtle to leave implicit.
            Trade.entry_at.isnot(None),
            Trade.entry_at < until,
            or_(Trade.exit_at.is_(None), Trade.exit_at > since),
            or_(Trade.strategy_id != strategy.id, Trade.entry_at < since),
        )
        .all()
    )
    out: list[dict] = []
    for t in rows:
        notional = float((t.qty or 0) * (t.entry_price or 0)) or float(t.notional or 0)
        out.append({
            "symbol": t.symbol,
            "from": t.entry_at.isoformat(),
            "to": t.exit_at.isoformat() if t.exit_at else None,
            "notional": round(notional, 2),
        })
    return out


def _account_entries(
    session: Session, strategy: Strategy, since: datetime, until: datetime, mode: str
) -> list[dict]:
    """When the REST of the account filled an entry inside this window — the
    seeded half of `entries_today`.

    THE DEFINITION IS COPIED FROM THE ENGINE. `_build_rail_context` counts
    `Trade.entry_at >= today_start` with `status != 'rejected'`, in this mode,
    WITH NO STRATEGY FILTER: the trade-rate limiter is deliberately
    cross-strategy. A one-strategy replay counted only its own entries, so on an
    account running several strategies it had most of a fresh daily budget that
    live did not, and it entered where live had already hit the cap.

    The population rule is `_account_positions`', unchanged: other strategies'
    rows, plus this strategy's rows from BEFORE the window. This strategy's
    in-window entries are excluded because the replay makes its own and adding
    them would double-count — it would spend the day's budget twice.

    `status != 'rejected'` is restated here rather than inherited from the
    entry_at NULL check, because unlike `_account_positions` these two are NOT
    equivalent: a rejected row from `open_trade`'s did-not-fill path can carry an
    entry_at, and live does not count it."""
    rows = (
        session.query(Trade.entry_at)
        .filter(
            Trade.mode == mode,
            Trade.status != "rejected",
            Trade.entry_at.isnot(None),
            Trade.entry_at >= since,
            Trade.entry_at < until,
            or_(Trade.strategy_id != strategy.id, Trade.entry_at < since),
        )
        .all()
    )
    return [{"at": when.isoformat()} for (when,) in rows if when is not None]


def _account_realized(
    session: Session, strategy: Strategy, since: datetime, until: datetime, mode: str
) -> list[dict]:
    """The rest of the account's realized P&L inside this window — the seeded
    half of the daily-loss kill switch.

    THE DEFINITION IS COPIED FROM THE ENGINE. `_daily_loss` sums `Trade.pnl` over
    CLOSED trades with `exit_at >= day_start` in this mode, again with no
    strategy filter, and takes max(0, -total) once at the end. So the amounts are
    sent SIGNED — a winner elsewhere in the account really does buy this
    strategy's losses some headroom, and clamping each row would refuse trades
    live allowed.

    Same population rule as `_account_positions` and `_account_entries`. A trade
    this strategy opened INSIDE the window is excluded even when it closes inside
    it, because the replay opens and closes that one itself; a trade it opened
    BEFORE the window is included, because the replay starts flat and never sees
    that exit at all."""
    rows = (
        session.query(Trade.exit_at, Trade.pnl)
        .filter(
            Trade.mode == mode,
            Trade.status == "closed",
            Trade.exit_at >= since,
            Trade.exit_at < until,
            or_(Trade.strategy_id != strategy.id, Trade.entry_at < since),
        )
        .all()
    )
    return [
        {"at": when.isoformat(), "pnl": float(pnl or 0.0)}
        for when, pnl in rows
        if when is not None
    ]


def _nonfill_events(
    session: Session, since: datetime, until: datetime, mode: str
) -> list[dict]:
    """The non-fill circuit breaker's evidence across this window (and the week
    before it), so the replay can tell which symbols live had benched.

    The replay fills every order at the bar close by construction, so it can
    never produce a "did not fill" of its own — it was therefore optimistic about
    exactly the symbols live had put away after three misses, and would buy them
    where live refused.

    THE CLASSIFICATION IS COPIED FROM THE ENGINE (`_build_rail_context`'s
    lookback query): a row that is open or closed is a FILL, which ends the
    streak; a rejected row whose reason says "did not fill" is a STRIKE; every
    other rejection is neither, because never having placed an order is not the
    same as having placed one and missed — those rows are simply not returned.
    Account-wide per symbol, no strategy filter, because the live query has none.

    Reaches `_NonfillLedger.LOOKBACK_DAYS` back BEFORE the window: live's own
    query looks 7 days back, and a streak that began on the Friday is what
    benches a symbol on the Monday the comparison starts."""
    floor = since - timedelta(days=backtest.NONFILL_LOOKBACK_DAYS)
    rows = (
        session.query(Trade.symbol, Trade.created_at, Trade.status)
        .filter(
            Trade.mode == mode,
            Trade.created_at >= floor,
            Trade.created_at < until,
            or_(
                Trade.status.in_(("open", "closed")),
                Trade.entry_reason.like("%did not fill%"),
            ),
        )
        .all()
    )
    # A rail rejection is dropped BY THE QUERY above — its status is "rejected"
    # and its reason does not say "did not fill" — which is the same place live
    # drops it. There is deliberately no second check here: a Python guard
    # restating the SQL predicate cannot disagree with it, so it can never fire,
    # and unfireable code reads like a rule when it is only an echo.
    return [
        {
            "symbol": symbol,
            "at": created.isoformat(),
            "filled": status in ("open", "closed"),
        }
        for symbol, created, status in rows
        if symbol and created is not None
    ]


def _prior_losses(
    session: Session, strategy: Strategy, before: datetime, mode: str, risk: dict
) -> dict[str, datetime]:
    """{symbol: when it last closed at a loss} for the losses the account had
    ALREADY taken when this window opened — the rail state the live engine
    started the window holding.

    The replay simulates the after-loss cooldown correctly, but only ever learns
    about losses it made itself, so it opens every window with a clean slate the
    engine did not have. Measured: a comparison reported that the replay "invented"
    an ADA/USD buy, when the truth was the reverse — another strategy had lost on
    ADA twelve hours earlier and the account-wide 24h cooldown was still in force.
    The replay had no way to know, and the report blamed the backtester for it.

    THE DEFINITION IS COPIED FROM THE ENGINE, not re-invented: the most recent
    losing CLOSED exit for that symbol, account-wide (any strategy), within this
    mode. Seed it with anything else and the discrepancy has merely been moved.

    Only losses that can still BLOCK something are returned:
      * within `cooldown_hours_after_loss` — the cooldown rail;
      * and for a STOCK strategy whose wash-sale guard is on, within 31 days,
        because that guard reads the same history over a longer window.
    Anything older cannot refuse a single entry, and sending it would inflate
    what the report claims to have seeded.

    Strictly BEFORE the window: a loss inside it is the simulation's own business
    and it will record that itself (and overwrite this seed for that symbol).
    """
    horizon = timedelta(hours=float(risk.get("cooldown_hours_after_loss", 24) or 0))
    if strategy.asset_class == "stock" and risk.get("wash_sale_guard", "block") != "off":
        horizon = max(horizon, timedelta(days=WASH_SALE_DAYS))
    if horizon <= timedelta(0):
        return {}
    rows = (
        session.query(Trade.symbol, func.max(Trade.exit_at))
        .filter(
            Trade.mode == mode,
            Trade.status == "closed",
            Trade.pnl < 0,
            # No "exit_at is not null" clause: `< before` already excludes it,
            # since NULL compares to nothing.
            Trade.exit_at < before,
            Trade.exit_at >= before - horizon,
        )
        .group_by(Trade.symbol)
        .all()
    )
    out: dict[str, datetime] = {}
    for symbol, when in rows:
        stamp = _aware(when)
        if symbol and stamp is not None:
            out[symbol.upper()] = stamp
    return out


def _intraday_size(window_hours: float | None) -> str:
    """WHICH intraday bar, once it is settled that the replay wants one.

    The live engine evaluates every 60 seconds. 15-minute bars are therefore 15x
    coarser than the decisions being graded, and that gap is not academic: a coin
    was bought live at 13:18:18, between the replay's 13:15 and 13:30 bars, and
    came back as a trade the replay missed — which then left it a position slot
    spare, so it bought something else and that came back as a trade it invented.
    A single blind spot produces two false verdicts pointing opposite ways.

    Minute bars close it, and are affordable only because the window is short:
    1,440 bars per crypto pair per day means this can never be the resolution for
    an ordinary backtest, and MAX_HOURS_FOR_MINUTE_REPLAY is where it stops.
    Above that a comparison is asking about behaviour in general rather than
    about particular fills, and 15-minute bars answer that.

    An UNKNOWN length gets the coarser one. There is no window to be short."""
    if window_hours is not None and window_hours <= MAX_HOURS_FOR_MINUTE_REPLAY:
        return "1Min"
    return "15Min"


def _replay_floor(
    params: dict,
    began: datetime | None,
    since: datetime,
    until: datetime,
    *,
    stretch: tuple[datetime, datetime] | None = None,
) -> datetime:
    """Where the replay may START trading: the beginning of the bar containing
    go-live, never earlier than the window itself.

    Getting this wrong is not cosmetic. Floor with 15-minute bars a run that then
    replays 1-minute ones and the replay opens up to fourteen minutes before the
    engine did — free minutes in which it can buy something the engine never saw,
    which the report files as a trade the backtest invented. That is the exact
    verdict the hold-back exists to prevent.

    `stretch` is the piece the replay will really cover for that moment. It has
    to be measured instead of the whole window, because a chunked comparison
    replays its pieces at a resolution the window's own length would never have
    chosen — floor at the window's size and the finer replay inherits a bar it
    was never going to use. Absent one (an unsegmented comparison, or imported
    trades, which are never split) the piece IS the window.

    Resolved TWICE either way, and the reason only appeared once minute bars did.
    The floor is the start of a bar, so it needs the bar size; the bar size comes
    from the replayed span's LENGTH; and that length depends on the floor,
    because the floor is where the replay opens. Circular — and it closes in one
    step, because raising the floor can only SHORTEN the span, and a shorter span
    can only choose a size that is finer or the same. So: pick a size from the
    widest possible span, floor with it, then re-pick from the span that
    produces. Anything further is a fixed point already."""
    if not began:
        return since
    start, end = stretch if stretch is not None else (since, until)
    hours = (end - max(since, start)).total_seconds() / 3600
    floor = max(since, _bar_floor(began, _timeframe_for(params, hours)))
    hours = (end - max(floor, start)).total_seconds() / 3600
    return max(since, _bar_floor(began, _timeframe_for(params, hours)))


def _timeframe_for(params: dict, window_hours: float | None = None) -> str:
    """The bar size the STRATEGY demands, not a hardcoded one. Asking for daily
    bars on a strategy that needs intraday ones gets every entry rejected, and the
    report would then read "the backtest missed all 20 trades" when the truth is
    that it was handed the wrong resolution to make any.

    Decided per SEGMENT on a segmented comparison: a strategy that gained a VWAP
    rule partway through the window needs daily bars for the stretch before that
    edit and intraday ones after it.

    This is a REQUEST, not a promise. The replay drops back to 15-minute bars
    when minute ones would cover less of the universe, and the response reports
    what it actually used — see `_scanner_replay`."""
    entry = params.get("entry") or {}
    wants_intraday = bool(
        entry.get("require_above_vwap")
        or (entry.get("entry_window_start") and entry.get("entry_window_end"))
    )
    if _mixed_resolution(params) or (wants_intraday and not _uses_daily_only_signals(params)):
        return _intraday_size(window_hours)
    # Length matters as much as the rules do. A window shorter than a couple of
    # days holds one daily bar or none, so a daily replay judges nothing and the
    # report reads as "the backtest missed all your trades" — which is how a
    # 3.5-hour crypto segment came back with six live buys and zero replayed
    # ones. Strategies with daily-only signals are left alone: replaying those
    # intraday would compute MACD/RSI off intraday closes, which is a worse lie
    # than the one being fixed.
    if (
        window_hours is not None
        and window_hours < MIN_HOURS_FOR_DAILY_REPLAY
        and not _uses_daily_only_signals(params)
    ):
        return _intraday_size(window_hours)
    return "1Day"


async def _replay_segments(
    segments: list[_Segment],
    strategy: Strategy,
    *,
    spread_pct: float,
    session: Session,
    client: AlpacaClient,
    mode: str,
    risk: dict,
) -> None:
    """Replay each segment over its OWN window with its OWN configuration.

    A failure is recorded on the segment rather than raised. A short stretch can
    legitimately hold no bars at all — a boundary either side of a weekend — and
    losing the whole comparison to that would be absurd when every other segment
    replayed fine."""
    for segment in segments:
        config = replay_strategy(segment.config)
        start = segment.replay_start or segment.start
        if start >= segment.end:
            # Nothing to replay, and the backtest refuses such a window outright
            # — as a pydantic error, which surfaces as a 500 rather than as
            # anything a user could act on. Recorded like any other segment
            # failure so the rest of the comparison still runs.
            segment.error = "This stretch ends before the replay could start, so it was skipped."
            continue
        # The rail state the account carried into THIS stretch. Both replay paths
        # need it and only one of them is this one, which is the exact shape of
        # bug that has bitten this file before.
        #
        # No "unless these are imported trades" guard, unlike the unsegmented
        # path: imported trades are never segmented (this machine's config
        # history is not the history that produced them), so a stretch is by
        # construction always this instance's own.
        segment.rails_seed = _prior_losses(session, strategy, start, mode, risk)
        try:
            segment.result = await replay(
                BacktestBody(
                    strategy_id=strategy.id,
                    symbols=segment.symbols,
                    window_start=start,
                    window_end=segment.end,
                    scanner_replay=segment.scanner_replay,
                    # This stretch's OWN trades, not the whole window's — a
                    # segment must not be handed names that only traded under a
                    # configuration it is not replaying.
                    seed_by_day=_seed_by_day(segment.live) if segment.scanner_replay else {},
                    prior_loss_at=segment.rails_seed,
                    # What the rest of the account held, traded and lost across
                    # THIS stretch — all three halves of the same backdrop.
                    account_positions=_account_positions(
                        session, strategy, start, segment.end, mode
                    ),
                    account_entries=_account_entries(
                        session, strategy, start, segment.end, mode
                    ),
                    account_realized=_account_realized(
                        session, strategy, start, segment.end, mode
                    ),
                    nonfill_events=_nonfill_events(session, start, segment.end, mode),
                    # THIS stretch's own fills, like everything else here: a
                    # stretch must not be handed moments from a configuration it
                    # is not replaying.
                    intrabar_entry_at=_intrabar_entry_at(segment.live),
                    timeframe=_timeframe_for(
                        config["params"], (segment.end - start).total_seconds() / 3600
                    ),
                    # The sleeve THAT config had. Starting every segment from
                    # today's would answer a question about today's budget.
                    starting_cash=max(config.get("sleeve_usd") or 0, 100),
                    spread_pct=spread_pct,
                    fee_pct=None,  # the asset class's real rate
                ),
                config,
                segment.symbols,
                strategy_name=strategy.name,
                session=session,
                client=client,
            )
        except HTTPException as exc:
            segment.error = str(exc.detail)
            log.info("fidelity segment %s → %s not replayed: %s",
                     segment.start, segment.end, segment.error)

    # WHOSE TRADES WERE NEVER LOOKED AT. Carried on the live rows themselves
    # because that is where the comparison reads them, and the comparison is
    # what writes the verdict. Until this, a stretch that errored contributed no
    # trades and no universe — and its live trades were then reported as "the
    # replay was watching this symbol and passed, this is the kind that points at
    # a real bug", on the strength of a universe some OTHER stretch had reported.
    # The strongest sentence in the report, about a window nothing ever ran over.
    for segment in segments:
        if not segment.error:
            continue
        for row in segment.live:
            row["replay_error"] = segment.error


def _merge_segment_results(segments: list[_Segment]) -> tuple[dict, list[str], list[dict]]:
    """One backtest-shaped result out of many, so the comparison arithmetic stays
    in exactly one place (qt.services.fidelity.compare) rather than being
    re-derived per segment and summed.

    Sound because the segments cover DISJOINT windows: no two can produce a trade
    on the same (symbol, day) unless a boundary fell mid-session, and there the
    earlier segment's trade wins — which is the one whose config produced the
    live decision being matched."""
    replayed = [s for s in segments if s.result]
    combined = {
        "trade_list": [t for s in replayed for t in (s.result.get("trade_list") or [])],
        "open_positions": [p for s in replayed for p in (s.result.get("open_positions") or [])],
    }
    # The universe is only known if EVERY replayed segment reported one. An
    # empty list means "this segment could not say what it covered", and a union
    # would let a segment that DID report lend its universe to one that didn't —
    # reinstating the exact false verdict this reporting was fixed to remove
    # ("the replay was watching this symbol and passed"). Unknown has to be
    # contagious: one silent segment makes the merged answer unknown too.
    #
    # Inert until recently, which is why it survived: scanner replays always
    # returned [] here, so the union was always empty and everything correctly
    # read as unknown. Now that the replay names its symbols, a split comparison
    # across a universe-changing edit — or one segment over UNIVERSE_LIST_CAP —
    # would produce a confident answer from partial evidence.
    per_segment = [list(s.result.get("symbols") or []) for s in replayed]
    symbols = (
        sorted({sym for names in per_segment for sym in names})
        if per_segment and all(per_segment)
        else []
    )
    gaps = [g for s in replayed for g in (s.result.get("bar_gaps") or [])]
    return combined, symbols, gaps


def _exit_model(results: list[dict]) -> dict:
    """How the replay judged EXITS, carried into the fidelity report.

    /api/backtest has reported `exit_model` and `bar_seconds` since the poller
    model landed, and this endpoint did not — which put them in the one place
    they are least needed and left them out of the one place they are most. The
    fidelity report IS the thing whose exit numbers moved when the model changed;
    a reader looking at `median_exit_delta_pct` had no way to tell which model
    produced it, so a number that halved looked like luck.

    "mixed" is a real answer, not a fudge: a segmented comparison picks a bar size
    per stretch (see _timeframe_for), so one half of a report can be poller-modelled
    and the other half not. `bar_seconds` then reports the COARSEST stretch,
    matching _apply_poller_view's rule that an unknown or coarse resolution must
    never be the answer that flatters a stop.

    `poll_phase_floor_pct` is the residual nobody can remove. See the note.
    """
    models = {r.get("exit_model") for r in results if r.get("exit_model")}
    seconds = [r.get("bar_seconds") for r in results if r.get("bar_seconds") is not None]
    moves = [
        r.get("median_bar_move_pct") for r in results
        if r.get("median_bar_move_pct") is not None
    ]
    model = models.pop() if len(models) == 1 else ("mixed" if models else None)
    # The LARGEST across stretches, for the same reason bar_seconds is: a floor
    # under a residual must never be quoted lower than the evidence supports, or
    # it turns into an invitation to chase what it was written to stop.
    floor_pct = max(moves) if moves else None
    return {
        "model": model,
        "bar_seconds": max(seconds) if seconds else None,
        "poll_seconds": backtest.LIVE_POLL_SECONDS,
        # The measured size of one bar's move — the scale of the gap that the
        # replay's clock being out of phase with the engine's opens up. See
        # qt.services.backtest._median_bar_move_pct.
        "poll_phase_floor_pct": floor_pct,
        "note": (
            "The replay looks at the price when each bar CLOSES, on the minute. The live"
            " engine looks every 60 seconds counted from whenever it last started, so its"
            " looks land at some arbitrary second past the minute — :17, :18 and :44 have"
            " all been seen. Nothing records that offset, so the two are sampling instants"
            " up to one bar apart, and the exits they pick can differ by about the size of"
            " one bar's move"
            + (f" (measured here: {floor_pct:g}%)" if floor_pct is not None else "")
            + ". That is the FLOOR under the exit difference below, not a bug to chase:"
            " finer bars cannot fix it, because one minute is the finest bar there is and"
            " the missing information is the phase of a clock, not the resolution of the"
            " tape. Only tick data would close it."
        )
        if model == "poller"
        else (
            "Exits were judged against each bar's high and low, because the bars are"
            " coarser than the live engine's 60-second look — over a bar that long the"
            " engine would have sampled a breach many times. A comparison of individual"
            " FILLS wants a short enough window to be replayed on 1-minute bars; this one"
            " is measuring behaviour in general."
        ),
    }


def _merge_ranking(rankings: list[dict]) -> dict | None:
    """Every replayed stretch's ranking counters, added up.

    This used to be `next(r["ranking"] for r in results ...)` — the FIRST stretch's
    counters, presented as the whole comparison's. On a split comparison that is
    not a rounding error, it is a different stretch's arithmetic: strategy 25 on
    2026-08-03 reported 261 ranked / 116 cut / 29 unrankable, all of it from the
    silent overnight stretch, while the stretch that actually took the five
    trades contributed its trades and had its counters discarded. Replaying that
    second stretch directly through /api/backtest evaluates 5,106 symbol-bars —
    twelve times what the report showed, for the window the reader is looking at.

    Exactly the fault already fixed for `backtest_no_trade_reason` (see the
    comment at the report level): a PER-SEGMENT value handed out as report-wide.
    The counters were the same bug wearing a `next()` instead of an index, which
    is why the earlier pass walked past them.

    The counts are counts of symbol-bars, so they add. The DESCRIPTORS may not:
    a config edit is what splits a comparison in the first place, so `top_n`,
    `rank_by` and `pool_size` can genuinely differ between stretches. Where they
    do, the merged answer is None rather than one stretch's value — the same
    choice `_exit_model` makes with "mixed", and for the same reason.

    `applied` is contagious in the false direction, matching the universe rule in
    _merge_segment_results: one stretch that could not rank makes the merged
    claim untrue, because some of the trades below were chosen without ranking."""
    if not rankings:
        return None
    if len(rankings) == 1:
        return dict(rankings[0])

    def agreed(key: str):
        values = {json.dumps(r.get(key), sort_keys=True, default=str) for r in rankings}
        return rankings[0].get(key) if len(values) == 1 else None

    merged = {
        **rankings[0],
        "applied": all(r.get("applied") for r in rankings),
        "benchmark_missing": any(r.get("benchmark_missing") for r in rankings),
        "rank_by": agreed("rank_by"),
        "top_n": agreed("top_n"),
        "pool_size": agreed("pool_size"),
        "metric_source": agreed("metric_source"),
        "symbols_never_rankable": sorted(
            {s for r in rankings for s in (r.get("symbols_never_rankable") or [])}
        ),
        "stretches_counted": len(rankings),
    }
    for key in ("symbol_bars_ranked", "symbol_bars_cut", "symbol_bars_unrankable"):
        merged[key] = sum(r.get(key) or 0 for r in rankings)
    warnings = [r.get("warning") for r in rankings if r.get("warning")]
    if len(rankings) > 1:
        # Said out loud because a merged "never rankable" is a weaker claim than a
        # single stretch's: a name unrankable in one stretch may have ranked fine
        # in another, and the union cannot tell the reader which.
        warnings.append(
            f"Counted across {len(rankings)} stretches of a split comparison."
            " Symbols listed as never rankable were never rankable in at least one"
            " stretch, not necessarily in all of them."
        )
    merged["warning"] = " ".join(dict.fromkeys(warnings)) or None
    return merged


def _ranking_report(results: list[dict]) -> dict | None:
    """The replay's own ranking counters, plus what an UNRANKABLE symbol-bar
    actually cost — which the counters alone do not say.

    The three numbers arrive as bare integers (`symbol_bars_ranked: 261`,
    `symbol_bars_cut: 116`, `symbol_bars_unrankable: 29`) and a reader has no way
    to tell which of the two possible meanings the third one has. They are
    opposite in seriousness:

      * passed through as though ranked — a hole in the top-N cut, and exactly how
        a replay invents a trade live would never have looked at; or
      * dropped — the pool quietly shrinks and the next name down takes the place.

    It is the second. `_Ranker.rank` computes each bar's metric, and
    `ranking.rank_symbols` drops every symbol whose value is None before taking
    the top N, so an unrankable bar is never eligible and cannot produce a trade.
    That also means it is already inside `symbol_bars_cut` — `excluded` is
    `len(bars) - len(ranked)` — so the two must not be added together, which is
    the other reading a bare pair of integers invites.

    Traced through qt.services.backtest._Ranker.rank and qt.services.ranking
    .rank_symbols rather than assumed; both are read-only from here.

    Said in the report because 29 of 261 is 11% of the sample, and a tenth of the
    pool going missing on the days it went missing is a coverage caveat on every
    "the replay missed this trade" verdict below."""
    ranking = _merge_ranking([r["ranking"] for r in results if r.get("ranking")])
    if not ranking:
        return None
    offered = ranking.get("symbol_bars_ranked") or 0
    unrankable = ranking.get("symbol_bars_unrankable") or 0
    if not unrankable:
        return {**ranking, "unrankable_effect": None}
    share = round(unrankable / offered * 100, 1) if offered else None
    return {
        **ranking,
        "unrankable_effect": (
            f"{unrankable} of the {offered} symbol-bars offered to the ranking"
            + (f" ({share}%)" if share is not None else "")
            + f" had no {(ranking.get('rank_by') or 'ranking').replace('_', ' ')} value, so they"
            " were DROPPED before the top-N cut — the replay could not enter those names on"
            " those bars, and the next name down took the place. They cannot invent a trade;"
            " they shrink the pool. They are already counted inside the"
            f" {ranking.get('symbol_bars_cut')} cut, not on top of it. Where a real trade of"
            " yours is reported as one the replay missed, this is one of the ways it could"
            " have gone unseen."
        ),
    }


def _segment_rows(segments: list[_Segment]) -> list[dict]:
    """What the UI needs to say a comparison was split, and how honest each piece
    of it is."""
    segments = _config_stretches(segments)
    return [
        {
            "from": s.start.isoformat(),
            "to": s.end.isoformat(),
            "version_no": s.version_no,
            "symbols": len(s.symbols),
            # False = the universe of that moment could not be reconstructed and
            # today's was substituted. Named per segment so one unrecoverable
            # stretch does not discredit the rest.
            "universe_known": s.universe_known,
            "live_trades": len(s.live),
            "backtest_trades": (
                len((s.result.get("trade_list") or [])) + len((s.result.get("open_positions") or []))
                if s.result
                else 0
            ),
            # WHY THIS STRETCH was silent. The report-level
            # `backtest_no_trade_reason` cannot say this: it is one string for a
            # comparison that may hold a silent stretch and a busy one, and
            # quoting the silent stretch's reason over the whole run is how a VWAP
            # rejection from a config edited away weeks ago ended up presented as
            # the explanation for a run that traded. Scoped to the stretch it is
            # actually about.
            #
            # Passed straight through, with no "…and only if this stretch traded
            # nothing" guard: the backtester sets `diagnosis.summary` to None on
            # any run that opened a position, so a stretch that traded has nothing
            # here already. A second guard here looked prudent and was unreachable
            # — it could not be made to fail — so it is the backtester's invariant
            # that holds this up, and a test pins it as such rather than pretending
            # this line is doing the work.
            "no_trade_reason": (s.result.get("diagnosis") or {}).get("summary") if s.result else None,
            "error": s.error,
        }
        for s in segments
    ]


@router.post("/compare")
async def compare(
    body: CompareBody,
    session: Session = Depends(get_session),
    client: AlpacaClient = Depends(require_client),
) -> dict:
    """Replay the window and diff it against what really happened."""
    strategy = session.get(Strategy, body.strategy_id)
    if not strategy:
        raise HTTPException(status_code=404, detail="Strategy not found.")

    # The window, resolved once and used by everything below: an explicit
    # start/end when given (the only way to name a stretch that ENDED in the
    # past), otherwise the last `days` days. Computed before the journal read
    # because both ends bound which trades are in scope.
    began = None if body.imported_trades is not None else first_trade_at(session, strategy, body.mode)
    # A strategy that has been switched on but has not traded still has a
    # meaningful window: switch-on until now. Without this the report refused to
    # run at all, which reads as breakage at exactly the moment a user is
    # checking whether their new strategy works.
    switched_on = None if body.imported_trades is not None else activated_at(session, strategy)
    if began is None:
        began = switched_on
    until = _aware(body.window_end) or datetime.now(timezone.utc)
    # Precedence: an explicit stretch (the "use that window" button), then a
    # deliberate day count, then the strategy's own lifetime — which is the case
    # that needs no input and is right nearly always.
    if body.window_start is not None:
        since = _aware(body.window_start)
    elif body.days is not None:
        since = until - timedelta(days=body.days)
    elif began is not None:
        # The DAY the strategy went live, so that day's own trades — matched by
        # day, and therefore parsed to midnight — fall inside the window. The
        # replay is held back to the exact moment separately, below: opening
        # both at midnight would hand the backtest the morning before go-live and
        # score whatever it bought there as a trade it invented.
        since = began.replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        # Never traded and never enabled — there is no moment to start from. The
        # 422 below explains that better than an arbitrary number would.
        since = until - timedelta(days=90)
    if until <= since:
        raise HTTPException(status_code=422, detail="The window ends before it starts.")

    # Clamped even when asked for more: replaying before the strategy existed
    # scores every trade the backtest takes there as one it invented — dozens of
    # them, none about the backtester, burying the days that are.
    clamped = bool(began and began.replace(hour=0, minute=0, second=0, microsecond=0) > since)
    if clamped:
        since = began.replace(hour=0, minute=0, second=0, microsecond=0)
    # The hold-back is resolved further down, once the window has been cut into
    # the stretches that will really be replayed: the floor is the start of a
    # BAR, so it cannot be chosen before the bar size is, and chunking is what
    # decides the bar size. See _replay_floor.

    # THE ENGINE COULD NOT ACT BEFORE IT WAS SWITCHED ON. Until now go-live was
    # only ever displayed, never enforced: the window opened at the first trade's
    # midnight and the replay was held back only to that trade's bar, so a
    # strategy enabled at 23:18 that first traded after midnight handed the
    # backtest the whole preceding evening. It bought there — correctly, by its
    # own rules — and every one of those buys was reported as a trade it
    # invented, which is a verdict about a period the engine did not exist in.
    #
    # Applied whenever go-live is known, including when activity predates it.
    # enabled_at records the LATEST switch-on, so earlier activity belongs to an
    # earlier live period that was paused — a different run of the strategy, and
    # not what "how is it doing since I turned it on" is asking about. Judging
    # this run against trades from that one is how a paused-and-resumed strategy
    # ends up compared over hours its current configuration never traded.
    if switched_on and switched_on < until:
        since = max(since, switched_on)
        # The hold-back needs no clamp of its own: it is resolved below, after
        # this, and is a max() against `since` there. One here would look like
        # belt-and-braces and be unreachable.
    if until <= since:
        raise HTTPException(
            status_code=422,
            detail=f"\"{strategy.name}\" has no {body.mode} history before that window ends — "
            "there is nothing in it to compare.",
        )

    live_rows = (
        body.imported_trades
        if body.imported_trades is not None
        else _journal_rows(session, strategy, since, until, body.mode)
    )
    if not live_rows and began is None:
        # Nothing traded AND never switched on: there is no window and no event.
        raise HTTPException(
            status_code=422,
            detail=f"\"{strategy.name}\" has never been enabled and has no {body.mode} trades, "
            "so there is no period to compare. Enable it, or import an export from another instance.",
        )
    # An empty journal is NOT a refusal from here on. The replay still runs over
    # the same window, and what it did — nothing, or something the engine never
    # did — is the finding.

    # The replay is the ordinary backtest, unchanged and with the strategy's own
    # universe — anything else would be comparing against a different experiment.
    # Scanner strategies replay their real day-varying universe automatically.
    fee_pct = None  # let the backtest use the asset class's real rate
    spread_pct = 0.1
    # "both" is scanner AND watchlist: scanner-replayed, with the watchlist
    # pinned as eligible every day. Treating it as watchlist-only reports every
    # scanner trade as one the replay missed.
    scanner_replay = strategy.universe in ("scanner", "both")
    # The strategy's OWN universe, never the watchlist. run() falls back to the
    # watchlist when handed no symbols, which for a custom-universe strategy
    # would replay a different set of names than the one that produced these
    # trades — and then every mismatch would be an artefact of the substitution.
    # "both" is scanner-replayed AND carries its watchlist, which travels as the
    # always-eligible set rather than as the whole universe.
    symbols = [] if strategy.universe == "scanner" else _strategy_symbols(session, strategy)

    # SHOULD THIS COMPARISON BE SPLIT? Only when a single replay is provably
    # answering the wrong question — the strategy, or the basket it points at,
    # changed while these trades were being made.
    #
    # Imported trades are never split. They come from another instance, so the
    # config history here is not the history that produced them, and cutting the
    # window at THIS machine's edits would be splitting on unrelated events. The
    # export also carries days rather than moments, so there is nothing to cut on.
    #
    # Cut a SECOND time, for resolution rather than for configuration: a stretch
    # longer than a day cannot have minute bars, and the live engine decides
    # every 60 seconds. See _chunk_for_minute_replay.
    segments = _chunk_for_minute_replay(
        _floor_boundaries_to_bars(
            _build_segments(session, strategy, since, until)
            if body.imported_trades is None
            else []
        ),
        live_rows,
    )

    # THE HOLD-BACK, resolved here rather than with the rest of the window,
    # because it is the start of a BAR and only now is the bar size known. The
    # stretch go-live falls in is the one that will be replayed over it, and a
    # chunked window replays that stretch at a finer size than the whole window
    # would have chosen — so floor it with the whole window's size and the
    # replay opens up to fourteen minutes before the engine did, in bars fine
    # enough to trade in. Whatever it buys there is reported as a trade it
    # invented, which is the exact false verdict the hold-back exists to prevent.
    #
    # ANCHORED TO GO-LIVE, not to the first fill. The rule the hold-back exists
    # for is "the engine could not act before it was switched on" — a strategy
    # enabled at 3pm must not be replayed from that morning. Anchoring to the
    # first TRADE is the same idea taken far past its warrant: a strategy switched
    # on at 23:06 that first traded at 09:58 the next morning had its replay
    # opened eleven hours late, on the very bar that produced the trade it was
    # being judged against — and the run-up the engine really had (the session's
    # VWAP, the day's range, what it had already passed on) went with it.
    #
    # Falls back to the first trade only when go-live is genuinely unknown:
    # strategies switched on before `enabled_at` existed and whose audit line has
    # not survived. A guess there would shift every verdict in a report whose
    # whole job is measuring accuracy, so the conservative anchor is right.
    #
    # No clamp needed for a strategy enabled long BEFORE the window: _replay_floor
    # never returns earlier than `since`, and the engine was live across the whole
    # window anyway, so there is nothing to hold back from.
    anchor = switched_on or began
    replay_from = _replay_floor(
        json.loads(strategy.params), anchor, since, until,
        stretch=next(
            ((s.start, s.end) for s in segments if s.start <= anchor < s.end), None
        ) if anchor else None,
    )
    if segments and replay_from > segments[0].start:
        # The hold-back lands wherever go-live falls, which is NOT necessarily
        # the first stretch: create a strategy in the morning, tweak it twice,
        # and it first trades that afternoon — three stretches, all but the last
        # ending before the engine ever acted. Pinning the hold-back to stretch
        # one then asked for a window ending hours before it began, which
        # pydantic refused and the user saw as a 500.
        #
        # Stretches that end at or before the hold-back are dropped rather than
        # replayed. They cannot contain a trade — the hold-back is floored to the
        # bar containing the FIRST one — so replaying them could only invent
        # trades in a period the engine was not yet running, which is the exact
        # false verdict the hold-back exists to prevent.
        segments[:] = [s for s in segments if s.end > replay_from] or segments[-1:]
        if segments[0].start < replay_from < segments[0].end:
            segments[0].replay_start = replay_from
    # WHICH CAP, AND WHAT IT COUNTS. `MAX_SEGMENTS` is about EDITS: past about
    # eight of them the fresh-account resets at each boundary say more than the
    # comparison does, and the honest answer is to narrow the window. Counting
    # resolution chunks against it would refuse to split a strategy that was
    # never edited at all, purely for having been switched on for a fortnight —
    # and then say "the configuration changed 11 times", which is a lie about
    # something the reader can check.
    config_stretches = sum(1 for s in segments if not s.resolution_chunk)
    too_many = config_stretches > MAX_SEGMENTS
    # "Segmented" is a claim about the STRATEGY — the UI prints "the
    # configuration changed while these trades were being made" beside it — so it
    # counts config stretches and nothing else. Cutting a window so it can keep
    # minute bars is not an edit, and a report that said it was would invent
    # edits the owner never made on every comparison older than a day.
    segmented = 1 < config_stretches <= MAX_SEGMENTS
    # …but the REPLAY follows the pieces, whatever made them. Splitting was gated
    # on `segmented` and that is the whole reason 70d6c4c did not fix what it was
    # written for: strategy 25 had never been edited, so its chunks were computed,
    # counted, and thrown away, and the window replayed whole at 15-minute bars.
    # One flag for what the reader is told, one for what the code does — they are
    # different questions and sharing a name is what hid this.
    split = len(segments) > 1 and not too_many
    not_segmented_reason = (
        f"The configuration changed {config_stretches - 1} times during this window. "
        f"Splitting it into {config_stretches} stretches would mean {config_stretches} "
        "replays each starting with a fresh account and no open positions, and past about "
        f"{MAX_SEGMENTS} those resets say more about the split than about the backtester. "
        "Narrow the window to a stretch you edited less."
        if too_many
        else None
    )

    # THE RAILS THE ACCOUNT ALREADY CARRIED IN. Seeded for a comparison and for a
    # comparison only: this is a window the account really lived through, so
    # "what was still cooling off at the start" has a truthful answer. An
    # ordinary backtest has no such answer and is left exactly as it was.
    #
    # Never for IMPORTED trades. Those came from another instance, so THIS
    # machine's journal is not the history that produced them and seeding from it
    # would manufacture cooldowns the exporting account never had.
    risk = get_risk(session)
    seed_rails = body.imported_trades is None
    rails_seed: dict[str, datetime] = {}

    if split:
        crossed, unplaced = _place_trades(segments, live_rows)
        await _replay_segments(
            segments, strategy, spread_pct=spread_pct, session=session, client=client,
            mode=body.mode, risk=risk,
        )
        if all(s.error for s in segments):
            # Nothing replayed at all — that is a real failure, not a caveat.
            raise HTTPException(status_code=422, detail=segments[0].error)
        result, replayed_symbols, bar_gaps = _merge_segment_results(segments)
        live_rows = [row for s in segments for row in s.live] + unplaced
        # The first segment that can explain a silent replay. On a split
        # comparison there is no single diagnosis, so this is a hint rather than
        # the whole story — the segment table below carries the rest.
        # Prefer the segment that actually has trades to explain. Taking the
        # first one reported a VWAP rejection from a stretch with zero live
        # trades, under a config that had since been edited away — a true
        # sentence about the wrong stretch, which is worse than none, because it
        # names a rule the reader then goes and changes.
        ranked = sorted(
            (s for s in segments if s.result), key=lambda s: len(s.live), reverse=True
        )
        no_trade_reason = next(
            (
                (s.result.get("diagnosis") or {}).get("summary")
                for s in ranked
                if (s.result.get("diagnosis") or {}).get("summary")
            ),
            None,
        )
        timeframe = next((s.result.get("timeframe") for s in ranked), None)
        fee_assumed = next(
            (s.result.get("fee_pct_per_side") for s in segments if s.result), 0.0
        )
        results = [s.result for s in segments if s.result]
        # Newest per symbol across the stretches: each was seeded at its own
        # start, so the same symbol can appear more than once with an older
        # timestamp, and the report should quote the one nearest the trades.
        for segment in segments:
            for symbol, when in segment.rails_seed.items():
                rails_seed[symbol] = max(when, rails_seed.get(symbol, when))
    else:
        crossed = 0
        rails_seed = (
            _prior_losses(session, strategy, replay_from, body.mode, risk)
            if seed_rails
            else {}
        )
        result = await run(
            BacktestBody(
                strategy_id=strategy.id,
                # The window is passed explicitly, so `days` — which only ever
                # means "back from now" — would be redundant here, and wrong once
                # the window ends in the past. The REPLAY starts at the moment the
                # engine went live, not at that day's midnight: the difference is
                # a morning of trades it could not have taken.
                window_start=replay_from,
                window_end=until,
                symbols=symbols,
                scanner_replay=scanner_replay,
                # See _seed_by_day: the names the engine acted on were in the
                # engine's universe, and the cached-movers reconstruction cannot
                # know that. Scanner replays only — a custom or basket universe
                # is already exactly the list that produced these trades.
                seed_by_day=_seed_by_day(live_rows) if scanner_replay else {},
                prior_loss_at=rails_seed,
                account_positions=_account_positions(
                    session, strategy, replay_from, until, body.mode
                ),
                # The other two daily rails, and the non-fill breaker. Gated on
                # `seed_rails` for the same reason `prior_loss_at` is: imported
                # trades came from another instance, so THIS machine's journal is
                # not the account that produced them and seeding from it would
                # manufacture a trade budget and a loss the exporter never had.
                account_entries=(
                    _account_entries(session, strategy, replay_from, until, body.mode)
                    if seed_rails else []
                ),
                account_realized=(
                    _account_realized(session, strategy, replay_from, until, body.mode)
                    if seed_rails else []
                ),
                nonfill_events=(
                    _nonfill_events(session, replay_from, until, body.mode)
                    if seed_rails else []
                ),
                # See _intrabar_entry_at. Gated on `seed_rails` for the reason
                # every other journal-derived input is: imported trades came from
                # another instance, so this machine's journal is not evidence
                # about what that engine could see.
                intrabar_entry_at=_intrabar_entry_at(live_rows) if seed_rails else {},
                timeframe=_timeframe_for(
                    json.loads(strategy.params), (until - replay_from).total_seconds() / 3600
                ),
                starting_cash=max(strategy.sleeve_usd, 100),
                spread_pct=spread_pct,
                fee_pct=fee_pct,
            ),
            session=session,
            client=client,
        )
        replayed_symbols = result.get("symbols") or []
        bar_gaps = result.get("bar_gaps") or []
        no_trade_reason = (result.get("diagnosis") or {}).get("summary")
        timeframe = result.get("timeframe")
        fee_assumed = result.get("fee_pct_per_side") or 0.0
        results = [result]

    # Names that got into the replay's universe because the JOURNAL says the
    # engine acted on them, not because the cached movers produced them. They
    # have to travel all the way to the trade log: "the replay was watching this
    # and passed" means something quite different when the only reason it was
    # watching is that we told it to.
    seeded_symbols = sorted({s for r in results for s in (r.get("universe_seeded") or [])})
    # HOW FAR APART TWO SIDES MAY OPEN THE SAME TRADE before it stops being the
    # same moment. Derived, not chosen: the replay acts when a bar CLOSES and the
    # live engine looks every LIVE_POLL_SECONDS from an offset nothing records,
    # so one bar plus one poll is the gap that exists for reasons which are not
    # disagreements — the same residual `exit_model.poll_phase_floor_pct`
    # reports. Past it, something genuinely differed.
    #
    # The COARSEST stretch sets it, matching _exit_model's rule: a mixed-
    # resolution comparison must not have its finest piece licensing complaints
    # about its coarsest. And an unknown resolution disables the check entirely —
    # "unknown" must not become the condemning answer any more than the
    # flattering one.
    bar_seconds = [r.get("bar_seconds") for r in results if r.get("bar_seconds") is not None]
    report = fidelity.compare(
        live_rows,
        result,
        assumed_spread_pct=spread_pct,
        assumed_fee_pct=fee_assumed or 0.0,
        replayed_symbols=replayed_symbols,
        seeded_symbols=seeded_symbols,
        timing_tolerance_seconds=(
            max(bar_seconds) + backtest.LIVE_POLL_SECONDS if bar_seconds else None
        ),
        # The same coarsest figure, for a different sentence: entries are judged
        # on a bar's CLOSE while exits see its high and low, so a missed entry
        # can be the resolution where a missed exit cannot. See
        # fidelity._CLOSE_ONLY_CAVEAT.
        bar_seconds=max(bar_seconds) if bar_seconds else None,
    )
    # When the replay traded NOTHING, the backtester already knows why — it
    # counts every rejection reason as it goes. Passing that through turns a
    # screen of "the backtest missed this" into the one sentence that explains
    # all of them at once.
    #
    # ONLY when it traded nothing. The backtester writes a diagnosis whenever it
    # has rejections to explain, which a run that also took trades certainly has,
    # and on a SEGMENTED comparison the reason is picked from whichever stretch
    # holds the most live trades — not from a stretch that was silent. Strategy 25
    # came back carrying "the 'price above VWAP' condition rejected the qualifying
    # bars… try disabling the VWAP rule" beside `backtest_trades: 5`: a no-trade
    # explanation for a run that took five trades, naming a rule the reader would
    # then go and switch off. Gated here rather than in either branch above,
    # because there are two branches and the last four bugs in this file were a
    # fix that landed on one of them. Per-stretch reasons survive on the segment
    # rows, where they are correctly scoped to the stretch they describe.
    report["backtest_no_trade_reason"] = (
        no_trade_reason if report["decision"]["backtest_trades"] == 0 else None
    )

    # HOW THE REPLAY'S UNIVERSE WAS ARRIVED AT. Said out loud because it is the
    # single largest reason a scanner comparison disagrees, and nothing on screen
    # would otherwise hint at it:
    #
    #   * `from_daily_movers` — the replay reconstructs each day's eligible set
    #     from the cached DAILY movers: top-N by that day's whole close-to-close
    #     move. The live crypto scanner ranks a ROLLING 24-hour figure recomputed
    #     every cycle (scanner.rolling_24h), so the set it had at 20:57 is not the
    #     set the day produces. This is a structural difference, not a bug, and
    #     it cannot be closed without recording the scanner's real universe at
    #     decision time.
    #   * `scanner_config_is_current` — those cached rows are re-filtered with
    #     TODAY'S price/volume/exclusion settings, deliberately (it is what stops
    #     a replay trading names now on the exclude list). A floor raised since
    #     these trades happened therefore judges them by a rule that did not
    #     exist yet.
    #   * `seeded` — names let in because the journal proves the engine acted on
    #     them that day. Whatever they contribute is NOT evidence that the replay
    #     reconstructed the universe; it is evidence about the SIGNAL, with the
    #     universe difference deliberately removed.
    report["universe"] = {
        "from_daily_movers": any(r.get("universe_from_daily_movers") for r in results),
        "scanner_config_is_current": any(r.get("scanner_config_is_current") for r in results),
        "seeded": seeded_symbols,
        "dropped": sorted({s for r in results for s in (r.get("universe_dropped") or [])}),
        # THE TOP-N CUT, read back off the replay's own results rather than off
        # the strategy row. A ranked strategy's engine only ever evaluates the
        # best `top_n` of its pool; the replay now does the same, and this says
        # whether it managed it. `ranked: false` with a warning means it could
        # NOT — the replay considered names live would never have looked at, and
        # any "the replay invented this trade" verdict below has to be read with
        # that in mind, because that is exactly how such a trade gets invented.
        "ranking": _ranking_report(results),
        # Which bars the replay's MACD/RSI/ATR came from. Belongs in THIS report
        # above all others: a comparison whose replay ran a 1-minute MACD against
        # live's daily one is not measuring the backtester's fidelity at all, and
        # every timing difference it lists is an artefact. Any stretch that fell
        # back makes the whole comparison suspect, so the worst case wins.
        "daily_signals": next(
            (r["daily_signals"] for r in results
             if r.get("daily_signals") and r["daily_signals"].get("warning")),
            next((r["daily_signals"] for r in results if r.get("daily_signals")), None),
        ),
    }

    # THE RAIL STATE THE REPLAY WAS HANDED, said out loud for the same reason
    # `universe.seeded` is: the replay did not work this out, we told it, and a
    # report that quietly flatters itself is worth less than no report. Read back
    # off the RESULTS (`rails_seeded`, echoed by the replay) rather than off what
    # this function meant to send, so unwiring the seed empties this list instead
    # of leaving it making a claim nothing supports.
    confirmed = {s for r in results for s in (r.get("rails_seeded") or [])}
    cooldown_h = float(risk.get("cooldown_hours_after_loss", 24) or 0)
    claimed = sorted(confirmed & set(rails_seed))
    # WHICH OF THOSE COULD ACTUALLY BLOCK THIS REPLAY. The seed is deliberately
    # account-wide and cross-strategy, because the live rail is: engine
    # ._build_rail_context takes max(exit_at) over every closed losing trade for
    # the symbol in this mode with no strategy filter at all, and the note above
    # check_rails says the cooldown and wash-sale guard stay portfolio-wide
    # whatever a strategy is set to. So the SEMANTICS are right and are left
    # alone — the replay is not being over-blocked.
    #
    # What was wrong was the reporting. Strategy 25 is a stock strategy over an
    # 11-symbol custom universe, and its report listed 29 seeded symbols: eleven
    # crypto pairs and eight unrelated stocks among them, none of which the replay
    # would ever evaluate, none of which could refuse a single entry, and all of
    # which buried the handful that could. The 31-day wash-sale horizon is what
    # makes the list that long — it reaches far past the 24h cooldown and sweeps
    # up everything the account lost on in a month.
    #
    # Scoped only when the replay's universe is KNOWN. A scanner strategy can
    # surface any name on any day, so there is nothing to scope against and the
    # whole list is the honest answer.
    in_universe = (
        {s.upper() for s in symbols}
        | {s.upper() for seg in segments for s in seg.symbols}
        | {s.upper() for s in replayed_symbols}
    )
    relevant = [s for s in claimed if s in in_universe] if in_universe else claimed
    report["rails"] = {
        # Symbols the account had already lost on when the window opened, so the
        # replay began holding them off exactly as the engine was. `cooldown_until`
        # is when the after-loss rail lapses; a stock entry that is already past
        # it can still be refused by the wash-sale guard, which reads the same
        # loss over 31 days.
        "seeded_losses": [
            {
                "symbol": symbol,
                "last_loss_at": _iso(rails_seed[symbol]),
                "cooldown_until": _iso(rails_seed[symbol] + timedelta(hours=cooldown_h)),
            }
            for symbol in relevant
        ],
        # Seeded, account-wide and correctly so, but on names this replay was
        # never going to evaluate. Counted rather than listed: the number is the
        # only part that carries information, and the rows were the noise.
        "seeded_losses_outside_universe": len(claimed) - len(relevant),
        "cooldown_hours_after_loss": cooldown_h,
        # WHAT THE REPLAY WAS TOLD, AND WHAT IT STILL IS NOT. Read off
        # engine.check_rails against qt.services.backtest's RailContext, field by
        # field. Kept scrupulously in step with what the seeding functions above
        # actually send: a stale entry here is worse than none, because it tells
        # the reader to discount a difference that has already been removed, and
        # the next person to investigate spends their evening on it.
        "seeded": [
            "positions the engine already held when the window opened, and positions OTHER "
            "strategies held: the 'already open for this symbol' rail is account-wide, and the "
            "replay is now told about both",
            f"the account-wide open-position cap ({risk.get('max_total_positions')}) and the "
            "exposure cap: other strategies' holdings count toward both, as they do live",
            f"the account-wide daily trade limit ({risk.get('max_trades_per_day')}/day): the rest "
            "of the account's entries are counted against the replay's budget, as they are live",
            "the daily-loss kill switch: the rest of the account's realised P&L for the day counts "
            "toward it, signed — so another strategy's winner buys headroom exactly as it does live",
            "the non-fill cooldown: the symbols live had benched after repeated 'did not fill' "
            "attempts are benched for the replay too, on the same escalating schedule",
            "both daily rails now reset at MIDNIGHT NEW YORK, which is when live resets them for "
            "every asset class — the replay used to roll a crypto book over at midnight UTC, "
            "4–5 hours out of step",
        ],
        # What is left. Each is a way the replay can still look freer (or
        # stricter) than the engine was, and naming them is cheaper than being
        # asked later why a mismatch survived.
        "not_seeded": [
            "the replay fills every order at the bar's close, so it never MISSES a fill of its own "
            "— it inherits live's non-fill history but cannot add to it inside the window",
            "slippage and fees are modelled, not replayed: price differences are reported "
            "separately as execution cost and are not signal disagreements",
        ],
    }

    # CONFIG DRIFT. Every trade records the config version that produced it. An
    # UNSEGMENTED replay uses the strategy as it stands TODAY, so if it was edited
    # since — a different universe, a bigger sleeve, more positions — the
    # comparison is answering "does today's strategy reproduce yesterday's
    # trades", which is a different and much less interesting question. It looks
    # identical on screen, which is why it has to be stated.
    #
    # A SEGMENTED comparison has already answered that objection: each stretch was
    # replayed with its own config. The drift is still reported — it is how you
    # see what moved — but it no longer invalidates the comparison, and the UI
    # reads `segmented` to know which of the two it is looking at.
    version_ids = {r.get("config_version_id") for r in live_rows if r.get("config_version_id")}
    versions = (
        session.query(StrategyConfigVersion)
        .filter(StrategyConfigVersion.id.in_(version_ids))
        .order_by(StrategyConfigVersion.version_no)
        .all()
        if version_ids
        else []
    )
    produced_by = json.loads(versions[-1].snapshot) if versions else None
    # BASKET MEMBERSHIP. A strategy's config version records which basket it
    # points at, not who is in it — so a basket edit changes the universe while
    # leaving the strategy's own version identical. Without this the drift check
    # above would report "no change" and actively reassure you.
    basket_drift, basket_changes = _basket_drift(session, strategy, live_rows)
    report["config"] = {
        # More than one means the strategy was edited DURING the window, so no
        # single replay can be faithful to all of these trades — not even one
        # using an old config.
        "versions_used": len(versions),
        "produced_by_version": versions[-1].version_no if versions else None,
        "drift": fidelity.config_drift(produced_by, _serialize_current(strategy)) + basket_drift,
        # Basket edits made WHILE these trades were happening. Distinct from the
        # drift above: membership that changed and changed back looks identical
        # from any two points, so only a count can reveal it.
        "basket_changes_during_window": basket_changes,
        # Every edit inside the window, with what moved and how many real trades
        # followed it. Reported whether or not the window was split — when it was
        # NOT split (too many edits) this is the only thing that can explain the
        # mismatches, and it is exactly then that it matters most.
        "changes": _change_log(segments, live_rows) if segments else [],
        # The longest unedited stretch that actually traded. With a heavily edited
        # window every comparison measures the edits; this is the window that
        # would measure the backtester instead.
        "stable_window": _stable_window(segments, live_rows) if config_stretches > 1 else None,
        # Was the window CUT at the moments the configuration changed, so each
        # stretch ran on the settings that were live during it?
        "segmented": segmented,
        "segments": _segment_rows(segments) if segmented else [],
        # Live trades that opened in one stretch and closed in another. No
        # segment's replay can reproduce those — each starts with no positions
        # and stops at its own end — so their entries still count and their exits
        # are set aside. Reported here because it is the honest cost of splitting.
        "boundary_spanning_trades": crossed,
        # Set when the window COULD have been split and deliberately wasn't.
        "not_segmented_reason": not_segmented_reason,
        # WHICH BARS GRADED WHAT. A window is now cut so the pieces holding
        # trades keep minute bars while the rest stays coarse, which means one
        # report can hold both — and `timeframe` above is a single string. See
        # _resolution_report.
        "resolution": _resolution_report(
            [((s.result or {}).get("timeframe"), len(s.live)) for s in segments]
            if split
            else [(timeframe, len(live_rows))]
        ),
        # Segments whose universe had to be filled in with today's list (a basket
        # with no snapshot that old, or a watchlist, which nothing versions).
        "segments_with_unknown_universe": (
            sum(1 for s in segments if not s.universe_known) if segmented else 0
        ),
    }
    # ONE stream: buys, sells, strategy edits and basket edits, ordered by when
    # each actually happened. Separate lists made the reader do the merge in
    # their head — and the merge is the whole point, because "the replay invented
    # four trades" means something entirely different once you can see the
    # universe widened eleven minutes earlier.
    report["timeline"] = sorted(
        [{**r, "kind": "trade"} for r in report.get("log") or []]
        + [
            {
                "kind": "edit",
                "at": c["at"],
                "day": (c["at"] or "")[:10],
                "symbol": "",
                "action": "edited",
                "verdict": "strategy changed",
                "detail": (
                    "Strategy edited: "
                    + ("; ".join(
                        f"{d['field']} {json.dumps(d['then'])} → {json.dumps(d['now'])}"
                        for d in c["changed"]
                    ) or "settings changed")
                    + f". {c['live_trades_after']} real trade"
                    + ("" if c["live_trades_after"] == 1 else "s")
                    + " followed before the next change."
                ),
            }
            for c in (report.get("config") or {}).get("changes") or []
        ]
        # The moment the engine was allowed to start. Every trade below is
        # judged from here, and its absence made a report on a freshly enabled
        # strategy look like it had simply failed to find anything.
        + (
            [{
                "kind": "activated",
                "at": _iso(switched_on),
                "day": _iso(switched_on)[:10],
                "symbol": "",
                "action": "activated",
                "verdict": "went live",
                "detail": f"\"{strategy.name}\" was switched on. The replay starts here too — "
                          "nothing before this moment counts against either side.",
            }]
            if switched_on and since <= switched_on <= until
            else []
        ),
        key=lambda r: (str(r["at"] or r["day"]), r.get("symbol") or ""),
    )
    report["window"] = {
        "start": _iso(since),
        "end": _iso(until),
        "days": max(1, round((until - since).total_seconds() / 86400)),
        # True when the request asked for more history than the strategy has.
        "clamped_to_first_trade": clamped,
        "first_trade_at": _iso(began),
    }
    # Said plainly rather than left to be inferred from an empty table: a
    # comparison where NEITHER side traded is an agreement, and the most likely
    # reading of an empty report — "it's broken" — is the wrong one.
    report["quiet"] = not live_rows and not (report.get("backtest_only") or [])
    report["activated_at"] = _iso(switched_on)
    report["strategy_name"] = strategy.name
    report["mode"] = body.mode
    report["days"] = body.days
    report["imported"] = body.imported_trades is not None
    report["timeframe"] = timeframe
    # STRETCHES THAT NEVER RAN. A split comparison records a failed replay on the
    # segment and carries on, which is right — one stretch with no bars either
    # side of a weekend must not cost the whole report — but it leaves the report
    # covering less than it says. The per-trade verdict names it for trades made
    # inside such a stretch; this names the stretch itself, so one that happened
    # to hold no live trades cannot pass for agreement.
    #
    # Reported whether or not the window was `segmented`: a comparison cut only
    # for resolution has no segment table on screen to hide behind, and that is
    # exactly the case a week-old window now takes.
    report["replay_failures"] = [
        {"from": _iso(s.start), "to": _iso(s.end), "error": s.error}
        for s in segments
        if s.error
    ]
    # WHICH EXIT MODEL produced the exit numbers in this report, and where their
    # floor is. Both are properties of the replay that the reader cannot infer
    # from anything else on screen. See _exit_model.
    report["exit_model"] = _exit_model(results)
    # Gaps in the replay's data invalidate a mismatch before it means anything:
    # a "trade the backtest missed" on a day with no bars is a cache problem, not
    # a replay bug. Carried through so the UI can say which it is.
    report["bar_gaps"] = bar_gaps
    # Paper fills are SIMULATED by the broker, so the execution half of this
    # report describes Alpaca's simulator rather than a real market. Decision
    # fidelity is unaffected — that is why paper is the sensible place to start.
    report["execution_is_measurable"] = body.mode == "live"
    return report


@router.get("/export")
def export(
    days: int = 90,
    mode: str = "live",
    session: Session = Depends(get_session),
) -> dict:
    """The journal for a window, as plain JSON, so another instance can diff
    against it. Trades only — no keys, no account numbers, no settings."""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = session.query(Trade).filter(Trade.mode == mode).all()
    strategies = {s.id: s for s in session.query(Strategy).all()}
    out = []
    for t in rows:
        stamp = _aware(t.entry_at) or _aware(t.created_at)
        if stamp is None or stamp < since:
            continue
        strategy = strategies.get(t.strategy_id)
        day_of = _day_of(strategy.asset_class if strategy else "stock")
        out.append(
            {
                "strategy_name": strategy.name if strategy else None,
                "symbol": t.symbol,
                "status": t.status,
                "entry_day": day_of(t.entry_at) if t.entry_at else None,
                "exit_day": day_of(t.exit_at) if t.exit_at else None,
                "entry_price": t.entry_price,
                "exit_price": t.exit_price,
                "pnl": t.pnl,
                "entry_reason": t.entry_reason,
                "exit_reason": t.exit_reason,
                "config_version_id": t.config_version_id,
            }
        )
    return {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "mode": mode,
        "days": days,
        "trades": out,
    }
