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
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from qt.api.backtest import (
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
from qt.models import BasketItem, Strategy, StrategyConfigVersion, Trade
from qt.services import fidelity
from qt.services.backtest import _day_fn

log = logging.getLogger("qt.api.fidelity")

router = APIRouter(prefix="/api/fidelity", tags=["fidelity"])


class CompareBody(BaseModel):
    strategy_id: int
    days: int = Field(default=90, ge=7, le=730)
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
    so saying so is a restatement rather than an assumption."""
    if ts is None:
        return None
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=timezone.utc)


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
    live: list[dict] = field(default_factory=list)
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
    return _strategy_symbols(session, strategy), False, False  # watchlist | both


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


def _change_log(segments: list[_Segment], live_rows: list[dict]) -> list[dict]:
    """Each moment the configuration or basket changed, with what moved and how
    many real trades were made under the stretch that followed.

    Werner asked for the edits to appear alongside the trades, and the reason is
    sound: "48 trades the backtest invented" is unreadable as a flat list, while
    "the universe widened on the 24th, and 30 of them are after that" is a
    finding. The count per stretch is what turns a date into an explanation."""
    log: list[dict] = []
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
    the same emptiness is worse than proposing nothing."""
    best: dict | None = None
    for segment in segments:
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


def _timeframe_for(params: dict) -> str:
    """The bar size the STRATEGY demands, not a hardcoded one. Asking for daily
    bars on a strategy that needs intraday ones gets every entry rejected, and the
    report would then read "the backtest missed all 20 trades" when the truth is
    that it was handed the wrong resolution to make any.

    Decided per SEGMENT on a segmented comparison: a strategy that gained a VWAP
    rule partway through the window needs daily bars for the stretch before that
    edit and intraday ones after it."""
    entry = params.get("entry") or {}
    wants_intraday = bool(
        entry.get("require_above_vwap")
        or (entry.get("entry_window_start") and entry.get("entry_window_end"))
    )
    if _mixed_resolution(params) or (wants_intraday and not _uses_daily_only_signals(params)):
        return "15Min"
    return "1Day"


async def _replay_segments(
    segments: list[_Segment],
    strategy: Strategy,
    *,
    spread_pct: float,
    session: Session,
    client: AlpacaClient,
) -> None:
    """Replay each segment over its OWN window with its OWN configuration.

    A failure is recorded on the segment rather than raised. A short stretch can
    legitimately hold no bars at all — a boundary either side of a weekend — and
    losing the whole comparison to that would be absurd when every other segment
    replayed fine."""
    for segment in segments:
        config = replay_strategy(segment.config)
        try:
            segment.result = await replay(
                BacktestBody(
                    strategy_id=strategy.id,
                    symbols=segment.symbols,
                    window_start=segment.start,
                    window_end=segment.end,
                    scanner_replay=segment.scanner_replay,
                    timeframe=_timeframe_for(config["params"]),
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
    symbols = sorted({sym for s in replayed for sym in (s.result.get("symbols") or [])})
    gaps = [g for s in replayed for g in (s.result.get("bar_gaps") or [])]
    return combined, symbols, gaps


def _segment_rows(segments: list[_Segment]) -> list[dict]:
    """What the UI needs to say a comparison was split, and how honest each piece
    of it is."""
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
    since = _aware(body.window_start) or (datetime.now(timezone.utc) - timedelta(days=body.days))
    until = _aware(body.window_end) or datetime.now(timezone.utc)
    if until <= since:
        raise HTTPException(status_code=422, detail="The window ends before it starts.")

    live_rows = (
        body.imported_trades
        if body.imported_trades is not None
        else _journal_rows(session, strategy, since, until, body.mode)
    )
    if not live_rows:
        raise HTTPException(
            status_code=422,
            detail=f"No {body.mode} trades for \"{strategy.name}\" in the last {body.days} days — "
            "there is nothing to compare the backtest against. Let it trade for a while, widen the "
            "window, or import an export from another instance.",
        )

    # The replay is the ordinary backtest, unchanged and with the strategy's own
    # universe — anything else would be comparing against a different experiment.
    # Scanner strategies replay their real day-varying universe automatically.
    fee_pct = None  # let the backtest use the asset class's real rate
    spread_pct = 0.1
    scanner_replay = strategy.universe == "scanner"
    # The strategy's OWN universe, never the watchlist. run() falls back to the
    # watchlist when handed no symbols, which for a custom-universe strategy
    # would replay a different set of names than the one that produced these
    # trades — and then every mismatch would be an artefact of the substitution.
    symbols = [] if scanner_replay else _strategy_symbols(session, strategy)

    # SHOULD THIS COMPARISON BE SPLIT? Only when a single replay is provably
    # answering the wrong question — the strategy, or the basket it points at,
    # changed while these trades were being made.
    #
    # Imported trades are never split. They come from another instance, so the
    # config history here is not the history that produced them, and cutting the
    # window at THIS machine's edits would be splitting on unrelated events. The
    # export also carries days rather than moments, so there is nothing to cut on.
    segments = (
        _build_segments(session, strategy, since, until)
        if body.imported_trades is None
        else []
    )
    too_many = len(segments) > MAX_SEGMENTS
    segmented = 1 < len(segments) <= MAX_SEGMENTS
    not_segmented_reason = (
        f"The configuration changed {len(segments) - 1} times during this window. "
        f"Splitting it into {len(segments)} stretches would mean {len(segments)} replays "
        "each starting with a fresh account and no open positions, and past about "
        f"{MAX_SEGMENTS} those resets say more about the split than about the backtester. "
        "Narrow the window to a stretch you edited less."
        if too_many
        else None
    )

    if segmented:
        crossed, unplaced = _place_trades(segments, live_rows)
        await _replay_segments(
            segments, strategy, spread_pct=spread_pct, session=session, client=client
        )
        if all(s.error for s in segments):
            # Nothing replayed at all — that is a real failure, not a caveat.
            raise HTTPException(status_code=422, detail=segments[0].error)
        result, replayed_symbols, bar_gaps = _merge_segment_results(segments)
        live_rows = [row for s in segments for row in s.live] + unplaced
        # The first segment that can explain a silent replay. On a split
        # comparison there is no single diagnosis, so this is a hint rather than
        # the whole story — the segment table below carries the rest.
        no_trade_reason = next(
            (
                (s.result.get("diagnosis") or {}).get("summary")
                for s in segments
                if s.result and (s.result.get("diagnosis") or {}).get("summary")
            ),
            None,
        )
        timeframe = next(
            (s.result.get("timeframe") for s in segments if s.result), None
        )
        fee_assumed = next(
            (s.result.get("fee_pct_per_side") for s in segments if s.result), 0.0
        )
    else:
        crossed = 0
        result = await run(
            BacktestBody(
                strategy_id=strategy.id,
                window_start=since,
                window_end=until,
                symbols=symbols,
                days=body.days,
                scanner_replay=scanner_replay,
                timeframe=_timeframe_for(json.loads(strategy.params)),
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

    report = fidelity.compare(
        live_rows,
        result,
        assumed_spread_pct=spread_pct,
        assumed_fee_pct=fee_assumed or 0.0,
        replayed_symbols=replayed_symbols,
    )
    # When the replay traded NOTHING, the backtester already knows why — it
    # counts every rejection reason as it goes. Passing that through turns a
    # screen of "the backtest missed this" into the one sentence that explains
    # all of them at once.
    report["backtest_no_trade_reason"] = no_trade_reason

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
        "stable_window": _stable_window(segments, live_rows) if len(segments) > 1 else None,
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
        # Segments whose universe had to be filled in with today's list (a basket
        # with no snapshot that old, or a watchlist, which nothing versions).
        "segments_with_unknown_universe": (
            sum(1 for s in segments if not s.universe_known) if segmented else 0
        ),
    }
    report["strategy_name"] = strategy.name
    report["mode"] = body.mode
    report["days"] = body.days
    report["imported"] = body.imported_trades is not None
    report["timeframe"] = timeframe
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
