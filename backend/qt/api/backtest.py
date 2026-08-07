"""Backtest endpoint: replay a saved strategy over history.

Two ways in, same work:

* ``POST /api/backtest`` (and ``/portfolio``) run the replay INSIDE the request
  and return the result. Simple, and what the tests drive.
* ``POST /api/backtest/start`` runs the very same handler as a background task
  and returns a job id to poll at ``/api/backtest/job/{id}``.

The second exists because a long replay outlives an HTTP request. A 350-day,
30-symbol backtest takes minutes, and anything in front of this container gives
up long before that: nginx defaults to a 60-second read timeout and Cloudflare
enforces a fixed 100 seconds (HTTP 524) that no plan setting can raise. The
work was fine — the connection died. Polling a job keeps every request short,
so the proxy has nothing to time out.
"""

import asyncio
import json
import logging
import uuid
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.orm import Session

from qt.api.market import require_client
from qt.broker.alpaca import AlpacaClient, AlpacaError
from qt.db import get_session, session_scope
from qt.models import BasketItem, Strategy, WatchlistItem
from qt.services import backtest, barfetch
from qt.services.engine import get_risk

log = logging.getLogger("qt.api.backtest")

router = APIRouter(prefix="/api/backtest", tags=["backtest"])

# Where a running backtest says what it's up to. A ContextVar rather than an
# argument because the handlers below are FastAPI routes: an extra parameter
# would be read as a request field. Background jobs install a sink here; a direct
# POST leaves it unset and _report() costs nothing. asyncio.to_thread copies the
# context, so reports from inside the (threaded) replay arrive too.
_reporter: ContextVar[Callable[[str, int | None], None] | None] = ContextVar("bt_reporter", default=None)


def _report(phase: str, pct: int | None = None) -> None:
    sink = _reporter.get()
    if sink is not None:
        sink(phase, pct)


def _replay_progress(done: int, total: int) -> None:
    """run_backtest's observer hook — bars replayed out of bars total."""
    _report("Replaying history…", int(done * 100 / total) if total else 0)


TIMEFRAMES = ("1Min", "15Min", "1Hour", "1Day")

# The bar sizes that mean "replay the day as it unfolded" rather than "one
# decision per day". Named because several guards below ask the same question and
# would otherwise drift apart the next time a size is added.
INTRADAY_TIMEFRAMES = ("1Min", "15Min")


def intraday_model_for(asset_class: str, timeframe: str = "15Min"):
    """The bar-cache table a given intraday resolution lives in.

    Four tables, not two: stock and crypto keep their own (their "day" means
    different things), and 1-minute bars keep their own again because a minute
    bar and a 15-minute bar collide on the (symbol, timestamp) key four times an
    hour — see the note above MinuteBar in qt.services.barcache. One place to ask,
    so no caller can read one resolution and write another."""
    from qt.services import barcache

    crypto = asset_class == "crypto"
    if timeframe == "1Min":
        return barcache.CryptoMinuteBar if crypto else barcache.MinuteBar
    return barcache.CryptoIntradayBar if crypto else barcache.IntradayBar


def _bar_label(timeframe: str) -> str:
    """How to say a bar size to a human watching a progress bar."""
    return {"1Min": "1-minute bars", "15Min": "15-minute bars"}.get(
        timeframe, f"{timeframe} bars"
    )

# Commission per side, as a % of notional. US equities on Alpaca are
# commission-free (a sell carries tiny SEC/TAF regulatory fees — cents, and not
# worth modelling). CRYPTO is not free: Alpaca charges a volume-tiered
# 0.15%-0.25% per side at the entry tier (under $100k/month), so a round trip
# costs roughly half a percent. A strategy taking 1-3% moves several times a day
# hands over a serious share of its edge, and a backtest that ignores this
# reports a profit the real account would never have seen.
#
# https://docs.alpaca.markets/docs/crypto-fees
DEFAULT_FEE_PCT = {"stock": 0.0, "crypto": 0.25}


@dataclass
class ScannerReplayDataset:
    """The offline scanner-replay dataset read from the bar cache: for each past
    day, only that day's cached top-N movers are eligible to enter. Shared by the
    single-strategy scanner-replay backtest and the scanner-replay optimizer so
    both build the universe the exact same way."""
    bars: dict[str, list[dict]]
    eligible_by_day: dict[str, set[str]]
    timeframe: str
    used_intraday: bool
    union: list[str]
    market: str
    benchmark_class: str
    benchmark_symbol: str
    start_day: str
    days_replayed: int
    # Symbols actually replayed vs. those that made a mover list but had no bars
    # at the chosen resolution. `universe_size` must report what was TESTED, not
    # what was hoped for — a silently shrunken universe is a wrong result that
    # looks right.
    replayed: list[str]
    dropped: list[str]
    intraday_covered: int
    # The DAILY series for the same names, reaching back over the warm-up before
    # the window — the indicator SOURCE, never the replay timeline. MACD, RSI and
    # ATR are daily signals live, so on an intraday replay they must come from
    # here rather than from 15-minute bars (a "14-period ATR" on 15-minute bars
    # measures 3.5 hours). Callers pass it only when the strategy has such a
    # signal; run_backtest keeps it look-ahead-safe via _daily_frontier, which
    # only ever reads daily bars completed BEFORE each replay bar's own day.
    daily: dict[str, list[dict]]
    # Symbol-days an intraday replay had to cover with the DAILY bar because no
    # 15-minute bars were cached for them. Reported rather than hidden: those days
    # had their stops checked at daily resolution, not intraday.
    daily_filled_days: int = 0
    # The last day of the window, and the day the BASELINE prefix opens on.
    # `start_day` is where TRADING may begin; `baseline_day` sits a few days
    # earlier and exists only so the first tradable bar has a day-gain reference
    # (crypto measures against the close 24h back — see BASELINE_WARMUP_DAYS).
    # Bars between the two are loaded and never traded; `sim_start` enforces it.
    end_day: str = ""
    baseline_day: str = ""
    # Names made eligible because the LIVE side is known to have traded them on
    # that day, not because the cached movers list produced them. Reported so a
    # comparison built on a seeded universe can never be read as a reconstruction
    # of the universe the scanner really had — see `seed_by_day` below.
    seeded: list[str] = field(default_factory=list)


def _fill_intraday_gaps(
    intraday: dict[str, list[dict]], daily: dict[str, list[dict]], start_day: str
) -> tuple[dict[str, list[dict]], int]:
    """Give every symbol a bar on every day of the window, using its DAILY bar on
    days it has no intraday ones.

    The intraday cache is filled per MOVER-DAY: when a symbol makes a day's top-N
    list, that day (plus a short baseline before it) gets 15-minute bars. Nothing
    else does. So a name that rose once on day 30 and was never a riser again has
    intraday bars around day 30 and nothing afterwards — while a position opened
    on day 30 may still be open on day 120.

    For that position the replay was then flying blind: no bar means no mark, so
    it kept its last seen price, and no bar means no exit check either, so its
    stop-loss and trailing stop could not fire. The position ran unmanaged until
    the symbol happened to be a riser again, at which point weeks of price move
    landed in one step. The equity chart's flat-then-cliff shape was exactly this.

    The daily series is already loaded here, already covers the whole window for
    every symbol (the daily sweep is universe-wide), and costs nothing extra. On a
    filled day the position is marked at that day's close and its stops are
    checked against that day's high and low — daily resolution rather than
    15-minute, which is precisely what a daily-bar backtest gives and is not
    blind. Days that DO have intraday bars are untouched, so nothing is
    double-counted and the resolution is never downgraded.

    Returns the merged bars and how many symbol-days were filled, so the run can
    report how much of it was checked at which resolution instead of implying the
    whole thing was intraday.
    """
    merged: dict[str, list[dict]] = {}
    filled = 0
    for symbol, series in intraday.items():
        covered = {b["t"][:10] for b in series}
        # start_day: `daily` deliberately reaches back over the indicator warm-up,
        # and those earlier bars must not leak into the replay timeline — that
        # would silently extend the tested window before the period asked for.
        gap_bars = [
            b
            for b in (daily.get(symbol) or [])
            if b["t"][:10] >= start_day and b["t"][:10] not in covered
        ]
        if gap_bars:
            filled += len(gap_bars)
            # Tagged, so a later pass can tell a stand-in from a real bar. The
            # timestamp cannot: a daily stock bar is stamped 14:00Z and 14:00Z is
            # an ordinary 15-minute bar time during the session, so matching on
            # the stamp would read genuine intraday bars as fills and re-download
            # days that were already covered, every single run.
            gap_bars = [{**b, "daily_fill": True} for b in gap_bars]
            merged[symbol] = sorted(series + gap_bars, key=lambda b: b["t"])
        else:
            merged[symbol] = series
    return merged, filled


def held_spans(result: dict) -> dict[str, tuple[str, str]]:
    """For each symbol the replay actually held, the first entry day and the last
    day it was still open — merged across every position in that symbol.

    This is the coverage a replay genuinely needs at full resolution. The cache is
    filled per mover-day, which is the right rule for deciding ENTRIES (a symbol
    can only be bought on a day it was a riser) and the wrong one for HOLDING: a
    position outlives its symbol's time on the top-N list, and every day it is
    open needs bars for the exits to be evaluated when they actually happened."""
    spans: dict[str, tuple[str, str]] = {}

    def widen(symbol: str, first: str, last: str) -> None:
        low, high = spans.get(symbol, (first, last))
        spans[symbol] = (min(low, first), max(high, last))

    for trade in result.get("trade_list") or []:
        entry, exit_day = trade.get("entry_day"), trade.get("exit_day")
        if entry:
            widen(trade["symbol"], entry, exit_day or entry)
    for position in result.get("open_positions") or []:
        # Still open at the end, so it needed bars right up to the last day tested.
        last = (result.get("equity_days") or [position.get("entry_day")])[-1]
        if position.get("entry_day"):
            widen(position["symbol"], position["entry_day"], last)
    return spans


def daily_filled_held_days(result: dict, ds) -> int:
    """How many daily-filled symbol-days fell inside a span where a position was
    actually OPEN.

    `daily_filled_days` counts the whole universe — every symbol-day anywhere in
    the scanner pool that fell back to a daily bar (see _fill_intraday_gaps). The
    caveat printed beside it, though, is about HELD positions: "no chance for a
    stop to fire". Those are different populations, and the gap between them is
    large: a 720-day crypto replay reported 1,737 daily-resolution symbol-days
    while holding a position on none of them, which reads as 1,737 days of
    unwatched positions and is not what happened.

    A day with nothing open costs an entry's timing at worst. A day with a
    position open costs a stop the chance to fire at the moment it was hit. Only
    the second is worth alarming anyone about, so it gets its own number."""
    spans = held_spans(result)
    if not spans:
        return 0
    held = 0
    for symbol, (first, last) in spans.items():
        for bar in ds.bars.get(symbol) or []:
            # `daily_fill` is the explicit tag _fill_intraday_gaps leaves; the
            # timestamp cannot be trusted to identify a stand-in (see its note).
            if bar.get("daily_fill") and first <= bar["t"][:10] <= last:
                held += 1
    return held


async def fetch_held_position_bars(
    client: AlpacaClient,
    result: dict,
    ds: ScannerReplayDataset,
    *,
    asset_class: str,
    report=None,
    timeframe: str = "15Min",
) -> int:
    """Download the intraday bars for days a position was HELD but the cache only
    had a daily bar for, and store them. Returns how many symbol-days were filled.

    `timeframe` must be the resolution the DATASET was read at, or this writes
    bars into a table the reload never looks in and the second pass silently
    achieves nothing.

    Why a second pass rather than fetching up front: which symbols get held, and
    for how long, is a property of the strategy — it cannot be known before the
    replay runs. Pre-fetching intraday bars for every mover across the whole
    window would download tens of times more data than any run uses, most of it
    for symbols the strategy never buys.

    So: replay once to learn the holdings, fetch exactly those symbol-days, replay
    again. The cost is bounded by what was actually held, and it is paid once —
    the bars land in the cache, so later runs over the same period read them
    offline. It also warms the cache for the optimizer, which searches the same
    universe over the same window.
    """
    from qt.services import barcache, barsweep

    intraday_model = intraday_model_for(asset_class, timeframe)

    spans = held_spans(result)
    if not spans:
        return 0

    # Which days inside each held span have no intraday bars. Days the cache
    # already covers are skipped — this only fills genuine holes.
    # "Covered" must mean 15-MINUTE coverage: the daily stand-ins are in ds.bars
    # too, and counting them would leave the very days this exists to fetch
    # looking already done. They carry an explicit tag rather than being spotted
    # by their timestamp — see _fill_intraday_gaps for why the stamp lies.
    covered_by_symbol: dict[str, set[str]] = {
        symbol: {b["t"][:10] for b in series if not b.get("daily_fill")}
        for symbol, series in ds.bars.items()
    }

    wanted: dict[str, tuple[str, str]] = {}
    for symbol, (first, last) in spans.items():
        have = covered_by_symbol.get(symbol, set())
        missing = [
            d
            for d in _days_between(first, last)
            if d not in have
        ]
        if missing:
            wanted[symbol] = (min(missing), max(missing))

    if not wanted:
        return 0

    filled = 0
    sess = barcache.session()
    try:
        for index, (symbol, (first, last)) in enumerate(sorted(wanted.items()), start=1):
            if report:
                report(
                    f"Downloading {_bar_label(timeframe)} for held positions — {symbol} "
                    f"({index} of {len(wanted)})",
                    int(index * 100 / len(wanted)),
                )
            end = (date.fromisoformat(last) + timedelta(days=1)).isoformat()
            try:
                data = await barsweep._bars_with_retry(
                    client, [symbol], timeframe, first, end,
                    asset_class=asset_class, attempts=2, retry_delay=1.0,
                )
            except Exception as exc:  # noqa: BLE001 — one symbol failing is not a failed run
                log.warning("held-position bar fetch failed for %s: %s", symbol, exc)
                continue
            for name, bars in data.items():
                if bars:
                    filled += barcache.save_intraday_bars(
                        sess, name, bars, model=intraday_model
                    )
            sess.commit()
    finally:
        sess.close()
    return filled


def _days_between(first: str, last: str) -> list[str]:
    start, end = date.fromisoformat(first), date.fromisoformat(last)
    return [
        (start + timedelta(days=n)).isoformat() for n in range((end - start).days + 1)
    ]


def _has_bars_from(series: list[dict] | None, day: str) -> bool:
    """Whether a series holds a bar on or after `day`. ISO timestamps compare
    lexically, so the day prefix is the whole test."""
    return any(b["t"][:10] >= day for b in series or [])


def _needs_intraday_sweep_detail(window_hours: float, symbols: int) -> str:
    """The 422 a short window gets when no intraday bars can be found for it.

    One wording, two places: the dataset raises it for callers that read the
    cache directly, and the scanner replay raises it AFTER trying to fetch the
    missing bars itself. A silent empty replay presented as a verdict is the
    worst outcome available, so this must stay reachable in both."""
    return (
        f"This window is only {window_hours:.1f}h long, which needs intraday bars, "
        f"and none of its {symbols} symbol(s) have any cached. Run an intraday "
        "sweep (Settings → Historical bar cache) and try again."
    )


def load_scanner_replay_dataset(
    asset_class: str, days: int, replay_top_n: int, scanner_cfg: dict | None = None,
    *, end: datetime | None = None, always_eligible: list[str] | None = None,
    window_hours: float | None = None, seed_by_day: dict[str, list[str]] | None = None,
    allow_empty_intraday: bool = False, intraday_timeframe: str = "15Min",
) -> ScannerReplayDataset:
    """Read the cached historical top-N risers + their bars for `days` back.
    Prefers cached INTRADAY bars (so intraday exits behave); falls back to daily
    when no intraday sweep has been run. Fully offline. Raises HTTPException 422
    when the cache is empty and 502 on a bad cache DSN — both API-shaped because
    both callers are FastAPI routes.

    `end` moves the window's far edge off "now": `days` are counted back from it,
    and the movers, the intraday bars and the daily bars all stop there. None
    keeps the original meaning — the last `days` up to this moment — and reads
    exactly the same rows as before, so an ordinary backtest is untouched. It
    exists so a period that ended in the past can be replayed with the
    configuration that was live during it.

    `seed_by_day` ({'YYYY-MM-DD': [symbol, …]}) makes named symbols eligible on
    named days on the CALLER'S authority, alongside whatever the cached movers
    produce. It exists for the fidelity comparison, and it is not a
    reconstruction of anything: the cached movers rank a whole day's close-to-
    close move and are re-filtered by TODAY'S scanner settings, while the live
    crypto universe is a rolling-24h top-N recomputed every cycle under the
    settings of the time. Those two sets are simply different, so a name the
    engine demonstrably traded can be absent from the replay's universe and every
    such trade reads as one the backtest missed. Seeding the days the engine
    actually acted removes that difference and leaves the SIGNAL difference,
    which is the question being asked. Only days the movers cache already knows
    about are widened — a day with no mover row is unrestricted already, and
    adding a key for it would narrow the universe rather than widen it. The
    seeded names come back on `.seeded` so no report can present this as
    fidelity it does not have.

    `allow_empty_intraday` suppresses the short-window 422 below, for a caller
    that intends to fetch the missing bars and load again. It does not make the
    empty case acceptable — see `_needs_intraday_sweep_detail`.

    `intraday_timeframe` picks WHICH intraday cache is read: the 15-minute tables
    (the default, and what every ordinary backtest and the optimizer use) or the
    1-minute ones. Only the fidelity comparison asks for the finer set, and only
    over a window of hours — the live engine decides every 60 seconds, so a
    15-minute replay cannot see a signal that came and went between two of its
    bars, and every such trade is reported as one the replay missed. The two
    resolutions live in different tables, so asking for one never reads or writes
    the other; see intraday_model_for."""
    from qt.services import barcache

    crypto = asset_class == "crypto"
    daily_model = barcache.CryptoDailyBar if crypto else barcache.DailyBar
    mover_model = barcache.CryptoDailyMover if crypto else barcache.DailyMover
    intraday_model = intraday_model_for(asset_class, intraday_timeframe)
    daily_stamp = "T12:00:00Z" if crypto else "T14:00:00Z"
    market = "crypto" if crypto else "stock"
    market_key = "crypto" if crypto else "stocks"  # the scanner config's own key
    benchmark_symbol = "BTC/USD" if crypto else "SPY"

    try:
        barcache.init_cache()
    except Exception as exc:  # noqa: BLE001 — surface a bad cache DSN clearly
        raise HTTPException(status_code=502, detail=f"Could not open the bar cache DB: {exc}")

    # `end_day` stays None for an open-ended run so every cache query keeps the
    # exact filter it had — a windowed run is the new case, not the default.
    finish = end or datetime.now(timezone.utc)
    start_day = (finish - timedelta(days=days)).strftime("%Y-%m-%d")
    end_day = finish.strftime("%Y-%m-%d") if end is not None else None
    # The window's last day, always known — `end_day` above is deliberately None
    # for an open-ended run so those queries keep the exact filter they had, but
    # a caller filling gaps still needs to know where the window stops.
    last_day = finish.strftime("%Y-%m-%d")
    # Where the INTRADAY series opens, a few days before trading may begin. Every
    # bar needs a day-gain baseline before it can be judged at all — crypto
    # against the close 24h back, stocks against the previous session — and
    # `_simulate` skips a bar whose change_pct is None without a word. Reading
    # intraday bars from `start_day` therefore made the window's first 24 hours
    # unusable, which on a window only a few hours long is the whole thing: the
    # replay evaluated ZERO bars and the report blamed the strategy's rules. The
    # equivalent fix on the live-fetch path (see `replay`'s baseline_start) never
    # reached here, because this path reads the cache instead of fetching.
    baseline_day = (
        datetime.strptime(start_day, "%Y-%m-%d")
        - timedelta(days=BASELINE_WARMUP_DAYS.get(market, 5))
    ).strftime("%Y-%m-%d")
    cache = barcache.session()
    try:
        # The replay's universe must obey the SAME scanner settings the live
        # engine obeys. Read them now rather than trusting whatever was in force
        # when the sweep ran — an exclusion added since, or a price floor raised,
        # would otherwise keep feeding the backtest names it must never trade.
        f = scanner_cfg[market_key] if scanner_cfg else {}
        movers = barcache.movers_between(
            cache, start_day, top_n=replay_top_n, model=mover_model,
            exclude=set(scanner_cfg.get("exclude_symbols") or []) if scanner_cfg else set(),
            min_price=float(f.get("min_price") or 0),
            max_price=float(f.get("max_price") or 0),
            min_dollar_volume=float(f.get("min_dollar_volume") or 0),
            end_day=end_day,
            # The live scanner refuses stablecoins by construction
            # (scanner._reject_reason), and until now that refusal was live-only:
            # the movers cache still offered USDC/USDT to every replay and every
            # optimizer run, which is a live-vs-replay divergence inside the very
            # feature whose job is detecting divergence.
            crypto=crypto,
        )
        if not movers:
            asset = "crypto" if crypto else "stock"
            raise HTTPException(
                status_code=422,
                detail=f"No cached {asset} movers yet — run a {'crypto ' if crypto else ''}sweep first "
                       "(Settings → Historical bar cache).",
            )
        # `always_eligible` are names the strategy may buy on ANY day regardless
        # of whether they rose — a watchlist. A "scanner AND watchlist" universe
        # is otherwise replayed as one or the other: treat it as scanner-only and
        # every watchlist trade reads as one the replay missed; treat it as
        # watchlist-only and every scanner trade does. Both are silent, and both
        # blame the backtester for being pointed at half the universe.
        pinned = {s.strip().upper() for s in (always_eligible or []) if s.strip()}
        # Seeded names, per day (see the docstring). Only days the movers cache
        # already gates are widened: `run_backtest` treats a day ABSENT from
        # eligible_by_day as unrestricted, so adding a key for such a day would
        # cut its universe down to the seeds — the opposite of the intent.
        seeds_by_day = {
            day: {s.strip().upper() for s in syms if s and s.strip()}
            for day, syms in (seed_by_day or {}).items()
        }
        # Seeds must be able to OPEN a day, not merely join one. The movers cache
        # only gains a row for a day once that day's DAILY bar exists, and the
        # sweep deliberately refuses today's still-forming bar — so "today" never
        # has a row, and a comparison of a strategy switched on this morning is
        # entirely about today. Iterating movers alone therefore dropped every
        # seed on the one day that mattered, leaving `seeded` empty.
        #
        # It also mattered in the other direction: run_backtest treats a day
        # ABSENT from eligible_by_day as UNRESTRICTED, so that day let the replay
        # buy anything it had bars for — which is where "the replay invented
        # BTC/USD and DOGE/USD" came from. Naming the day, even with only the
        # seeds, gates it.
        all_days = set(movers) | set(seeds_by_day)
        eligible_by_day = {
            day: set(movers.get(day, ())) | pinned | seeds_by_day.get(day, set())
            for day in all_days
        }
        seeded = sorted(
            {s for day in all_days for s in seeds_by_day.get(day, set())} - pinned
            - {s for syms in movers.values() for s in syms}
        )
        union = sorted(
            {s for syms in movers.values() for s in syms} | pinned | set(seeded)
        )
        # Intraday needs to cover the WHOLE mover set, not just some of it. This
        # used to test `any(...)`, which was safe only by accident: the intraday
        # table was filled exclusively by the "Sweep intraday" job, which fetches
        # every mover — all-or-nothing. Once ordinary backtests began caching
        # their bars too, a single incidental symbol (a name you happened to
        # backtest that later showed up as a mover) could flip the whole replay
        # to intraday, and every uncovered symbol — silently dropped downstream,
        # because run_backtest skips empty series — would vanish while the header
        # still claimed the full universe. Demand full coverage or use daily,
        # which the daily sweep fills completely.
        # From `baseline_day`, not `start_day` — the extra days are the day-gain
        # reference and are excluded from trading by `sim_start`, never by being
        # absent. "Covered", though, still means covered INSIDE the window: a
        # symbol whose only intraday bars sit in the baseline prefix has nothing
        # to replay, and counting it would flip a short window to intraday on the
        # strength of bars that can never trade.
        intraday_bars = barcache.cached_intraday_bars(
            cache, union, baseline_day, model=intraday_model, end_day=end_day
        )
        intraday_covered = sorted(
            s for s in union if _has_bars_from(intraday_bars.get(s), start_day)
        )
        # Full coverage is the rule (see above). The exception is a window too
        # short for daily bars to represent AT ALL: a daily bar is stamped at the
        # start of its day, so a 4-hour window contains none of them and the
        # "safe" fallback yields an empty replay — which is exactly how a
        # fidelity report came to say "the replay passed on ADA/XRP/SOL" when the
        # replay had no bars to pass on. There, partial intraday beats nothing:
        # the uncovered names are named as dropped, like any other gap.
        short_window = (
            window_hours is not None and window_hours < MIN_HOURS_FOR_DAILY_REPLAY
        )
        used_intraday = bool(union) and (
            len(intraday_covered) == len(union) or (short_window and bool(intraday_covered))
        )
        if short_window and not used_intraday and not allow_empty_intraday:
            raise HTTPException(
                status_code=422,
                detail=_needs_intraday_sweep_detail(window_hours, len(union)),
            )
        # The daily series is loaded either way, and always reaches back over the
        # warm-up: it is the replay timeline when there is no intraday coverage,
        # and the indicator source when there is. Costs almost nothing — daily
        # bars are one row per symbol per day, already in the cache — and without
        # the warm-up prefix every daily indicator is dead for the window's first
        # ~35 days, which on a 180-day replay is a fifth of the test.
        warm_start = (
            datetime.strptime(start_day, "%Y-%m-%d") - timedelta(days=WARMUP_DAYS)
        ).strftime("%Y-%m-%d")
        daily = barcache.cached_daily_bars(
            cache, union, warm_start, model=daily_model, stamp=daily_stamp, end_day=end_day
        )
        if used_intraday:
            if short_window:
                # No daily gap-filling on a short window: a filled daily bar sits
                # at the day's start, i.e. outside the window, so it adds a row
                # that can never trade and makes coverage look better than it is.
                bars = {s: series for s, series in intraday_bars.items() if series}
                filled = 0
            else:
                bars, filled = _fill_intraday_gaps(intraday_bars, daily, start_day)
            timeframe = intraday_timeframe
        else:
            # From `baseline_day`, for the same reason the intraday branch does:
            # the FIRST day of the window needs a prior bar to measure a day-gain
            # against — the previous session for stocks, ~24h back for crypto —
            # and `_simulate` drops a bar whose change_pct is None in silence. A
            # daily replay was therefore blind on day one of every window it was
            # ever asked about, and a one-day window saw nothing at all.
            #
            # Costs no extra reading: `daily` is already loaded from `warm_start`
            # (the indicator warm-up, far deeper than this). The trim exists only
            # to stop the equity curve spanning the whole warm-up, so moving it a
            # couple of days earlier is free. `sim_start` keeps the prefix
            # untradeable, and `replayed` below still measures from `start_day`,
            # so coverage numbers are unchanged.
            bars = {
                s: [b for b in series if b["t"][:10] >= baseline_day]
                for s, series in daily.items()
            }
            timeframe = "1Day"
            filled = 0  # a daily replay is daily everywhere; nothing to fill
        # Whatever the resolution, a symbol with no bars can't be replayed. Name
        # them rather than quietly shrinking the universe under the user.
        # Bars in the BASELINE prefix don't count: they exist to be a reference
        # price and can never trade, so a symbol holding only those was not
        # replayed and saying otherwise inflates `universe_size`.
        replayed = sorted(s for s in union if _has_bars_from(bars.get(s), start_day))
        dropped = sorted(set(union) - set(replayed))
    finally:
        cache.close()

    return ScannerReplayDataset(
        bars=bars, eligible_by_day=eligible_by_day, timeframe=timeframe,
        used_intraday=used_intraday, union=union, market=market,
        benchmark_class=market, benchmark_symbol=benchmark_symbol,
        start_day=start_day, days_replayed=len(movers), daily=daily,
        daily_filled_days=filled,
        replayed=replayed, dropped=dropped, intraday_covered=len(intraday_covered),
        end_day=last_day, baseline_day=baseline_day, seeded=seeded,
    )


class BacktestBody(BaseModel):
    strategy_id: int
    symbols: list[str] = []  # empty = use the watchlist for the strategy's asset class
    scanner_replay: bool = False  # replay the cached historical daily top-N risers instead
    replay_top_n: int = Field(default=10, ge=1, le=100)  # how many of each day's risers are eligible
    days: int = Field(default=90, ge=7, le=730)
    # An EXPLICIT window, as an alternative to counting `days` back from now.
    # Either end may be given on its own: `window_end` alone replays the `days`
    # before it, `window_start` alone runs from there to now. Both together pin
    # the window exactly, which is what a caller replaying one segment of a past
    # period with the config that was live during it needs — `days` cannot
    # express "12 May to 3 June" at all, because its far edge is always today.
    window_start: datetime | None = None
    window_end: datetime | None = None
    # {'YYYY-MM-DD': [symbol, …]} — names the CALLER knows were in the live
    # universe on that day, made eligible in a scanner replay alongside the
    # cached movers. The fidelity comparison fills this from its own journal.
    # It is not a reconstruction and never silently improves a result: see
    # `seed_by_day` on load_scanner_replay_dataset, and `universe_seeded` on the
    # response, which names every symbol that got in this way.
    seed_by_day: dict[str, list[str]] = Field(default_factory=dict)
    # {symbol: when it last closed at a loss BEFORE this window} — the after-loss
    # cooldown (and, for stocks, the wash-sale guard) the account already carried
    # in. The simulation only ever learns about losses it made itself, so without
    # this a replay of a mid-history window starts with a clean slate the live
    # engine did not have. Empty for every ordinary backtest, which has no
    # "before the window" worth inventing; the fidelity comparison fills it from
    # the journal. See run_backtest's `prior_loss_at`.
    prior_loss_at: dict[str, datetime] = Field(default_factory=dict)
    # 1Min is accepted but is NOT a general backtest option: `_check_window`
    # refuses it past MAX_HOURS_FOR_MINUTE_REPLAY. It exists for the fidelity
    # comparison, which replays a window of hours and has to resolve decisions
    # the live engine made on a 60-second cycle. Deliberately reachable through
    # the ordinary endpoint rather than behind a second door — a caller who
    # genuinely wants a one-day replay at full resolution should not need a
    # separate implementation of a backtest to get it — and the length guard is
    # what stops it becoming an accidental way to download ten million bars.
    timeframe: str = Field(default="1Hour", pattern="^(1Min|15Min|1Hour|1Day)$")
    # Return the replay's PER-BAR verdict for every candidate it judged, on the
    # response. Off by default and never set by the UI: it is a diagnostic for
    # answering "the engine bought this at 14:01 and the replay waited until
    # 14:26 — what did the replay see in between", which the aggregate counters
    # in `diagnosis` structurally cannot answer, because they total a session
    # into single integers. Capped at DEBUG_LOG_MAX_LINES with the pre-truncation
    # count reported alongside.
    debug: bool = False
    # Positions the ACCOUNT held that this replay cannot know about: other
    # strategies' positions, and this strategy's own positions that were already
    # open when the window started. Each {symbol, from, to|null, notional}.
    # Empty for every ordinary backtest — only the fidelity comparison, which has
    # the journal to reconstruct them from, ever fills this. See
    # qt.services.backtest._AccountBackdrop for why it is not general account
    # state and must never carry a decision.
    account_positions: list[dict] = Field(default_factory=list)
    # The other two halves of the same account backdrop, and for the same reason:
    # `check_rails` counts BOTH daily rails across the whole account with no
    # strategy filter, so a one-strategy replay was freer than live on both.
    #   * `account_entries` — when the REST of the account filled an entry, each
    #     {at}. Counts against the cross-strategy max_trades_per_day limiter.
    #   * `account_realized` — the rest of the account's closed P&L, each
    #     {at, pnl}, SIGNED. Feeds the whole-account daily-loss kill switch.
    # Same population rule as account_positions: other strategies' rows, plus
    # this strategy's rows from before the window (which the replay cannot
    # reproduce because it starts flat).
    account_entries: list[dict] = Field(default_factory=list)
    account_realized: list[dict] = Field(default_factory=list)
    # The non-fill circuit breaker's evidence, each {symbol, at, filled}: live
    # benches a symbol after three consecutive misses, and the replay fills every
    # order by construction so it can never generate that evidence itself. See
    # qt.services.backtest._NonfillLedger. Empty for every ordinary backtest.
    nonfill_events: list[dict] = Field(default_factory=list)
    # {symbol: [moments the live engine really opened a position]} — and ONLY a
    # fidelity comparison ever fills it. On those bars, and no others, a replay
    # that found no entry at the close may re-ask the same rules using the bar's
    # HIGH: the journal is evidence the trade happened, so a rule satisfied
    # somewhere inside that bar is a sampling difference rather than a
    # disagreement. Empty for every ordinary backtest, which has no ground truth
    # to justify it and would simply become more permissive. See
    # backtest._may_look_inside_bar, and note it is inert at 1-minute resolution
    # by construction — _apply_poller_view has already flattened the extremes
    # there, deliberately, because live gets one look per bar too.
    intrabar_entry_at: dict[str, list[datetime]] = Field(default_factory=dict)
    starting_cash: float = Field(default=5000, ge=100, le=10_000_000)
    spread_pct: float = Field(default=0.1, ge=0, le=2)
    # None = use the asset class's real-world rate (see DEFAULT_FEE_PCT). An
    # explicit 0 is honoured, for asking "what would this look like fee-free?".
    fee_pct: float | None = Field(default=None, ge=0, le=2)

    @model_validator(mode="after")
    def _check_window(self) -> "BacktestBody":
        # Everything QT stores and sends is UTC, so stamping a naive datetime is
        # a restatement rather than an assumption — and without it the comparison
        # against an aware now() below raises instead of answering.
        if self.window_start is not None and self.window_start.tzinfo is None:
            self.window_start = self.window_start.replace(tzinfo=timezone.utc)
        if self.window_end is not None and self.window_end.tzinfo is None:
            self.window_end = self.window_end.replace(tzinfo=timezone.utc)
        start, end = self.window()
        if end <= start:
            raise ValueError("The window's end must be after its start.")
        if (end - start) > timedelta(days=730):
            raise ValueError("A backtest window can span at most 730 days.")
        # ONE-MINUTE BARS ARE FOR SHORT WINDOWS AND NOTHING ELSE. 1,440 bars per
        # crypto pair per day means forty names over 180 days is ten million
        # bars — infeasible to download, pointless to store, and answering a
        # question 15-minute bars already answer well at that horizon. The cap is
        # the same number the fidelity comparison uses to CHOOSE the resolution,
        # so that path can never build a request this refuses.
        if self.timeframe == "1Min" and (end - start) > timedelta(
            hours=MAX_HOURS_FOR_MINUTE_REPLAY
        ):
            raise ValueError(
                f"A 1-minute replay can span at most {MAX_HOURS_FOR_MINUTE_REPLAY} hours — "
                "past that it is millions of bars to answer a question 15-minute bars "
                "already answer. Use 15Min."
            )
        return self

    def window(self) -> tuple[datetime, datetime]:
        """(start, end) of the period to TRADE — what sim_start and sim_end become.

        Note there is no 7-day floor here, unlike `days`. That floor guards the UI
        control against a window too short to mean anything; an explicit window is
        used by callers that CUT a period at the moments a config changed, and two
        edits on the same afternoon legitimately leave a segment of hours. Refusing
        it would force those trades to be scored against the wrong config, which is
        the whole thing this exists to stop."""
        end = self.window_end or datetime.now(timezone.utc)
        start = self.window_start or (end - timedelta(days=self.days))
        return start, end

    def span_days(self) -> int:
        """The window's length in whole days, rounded UP — what the day-keyed
        scanner-replay cache reads back. Rounding down would drop the window's
        first day whenever it starts mid-session."""
        start, end = self.window()
        return max(1, -(-int((end - start).total_seconds()) // 86400))


class PortfolioBacktestBody(BaseModel):
    strategy_ids: list[int] = Field(..., min_length=1, max_length=12)
    days: int = Field(default=90, ge=7, le=730)
    timeframe: str = Field(default="1Hour", pattern="^(15Min|1Hour|1Day)$")
    starting_cash: float = Field(default=5000, ge=100, le=10_000_000)
    spread_pct: float = Field(default=0.1, ge=0, le=2)


def replay_inputs(ds: ScannerReplayDataset, params: dict, replay_top_n: int) -> dict:
    """Everything a scanner-replay run takes from a dataset, derived in ONE place
    so the optimizer's request handler and its background task cannot disagree
    about what was replayed — they each hold a different dataset once the task
    tops up the intraday cache."""
    daily = ds.daily if _needs_warmup(params) else None
    return {
        "bars": ds.bars,
        "daily": daily,
        "eligible_by_day": ds.eligible_by_day,
        "timeframe": ds.timeframe,
        # Mixed only when the replay is intraday AND something reads the daily
        # series; either alone is a single-resolution run.
        "mixed": bool(ds.used_intraday and daily),
        "extra": {
            "scanner_replay": True,
            "replay_intraday": ds.used_intraday,
            "replay_top_n": replay_top_n,
            "universe_size": len(ds.replayed),  # what was TESTED, not what made a list
            "universe_dropped": ds.dropped,
            "intraday_covered": ds.intraday_covered,
            "daily_filled_days": ds.daily_filled_days,
            "days_replayed": ds.days_replayed,
            # Names in the universe on the caller's authority rather than from
            # the cached movers — empty for every ordinary run.
            "universe_seeded": ds.seeded,
        },
    }


def _wants_intraday_replay(params: dict) -> bool:
    """Whether replaying this strategy on 15-minute bars would tell you anything
    a daily replay can't: any price-triggered exit (a stop checked once a day at
    the close is barely a stop), the VWAP rule, or an entry-time window. A
    buy-and-hold sleeve with none of these gains nothing, so it isn't worth a
    download."""
    entry = params.get("entry") or {}
    return bool(
        _has_price_triggered_exit(params)
        or entry.get("require_above_vwap")
        or (entry.get("entry_window_start") and entry.get("entry_window_end"))
    )


# How much of the replay window one symbol may be missing before the direct
# fill below leaves it to the sweep. This pass exists for the RECENT EDGE — the
# days the periodic sweep has not reached yet — and for a handful of names that
# were never swept because they never made a mover list. A symbol missing more
# than this is not an edge, it is an unbuilt cache, and fetching it name by name
# would turn a 180-day replay into thousands of requests.
REPLAY_FILL_MAX_DAYS_PER_SYMBOL = 8
# And a hard ceiling on how many requests the whole pass may make, for the same
# reason: a stock sweep's mover union runs to thousands, and one symbol whose
# gaps are scattered costs a request per run of them.
REPLAY_FILL_MAX_REQUESTS = 60

# The 1-minute pass needs a ceiling the request count cannot express. A request
# is a contiguous RUN of days, so the two caps above bound how many CALLS are
# made and say nothing about how much comes back — and at one minute a call
# brings back fifteen times the bars a 15-minute call does (1,440 a day for a
# crypto pair, ~390 for a stock session). Forty symbols each missing three days
# is forty requests either way; it is ~170,000 minute bars against ~11,000
# 15-minute ones, and the second number is the one that lands in the owner's
# Postgres.
#
# So the minute pass is budgeted in SYMBOL-DAYS, which is what actually costs:
# 120 of them is roughly 170k crypto bars or 47k stock bars, downloaded once for
# a window and read from the cache thereafter. Over budget, the pass stands down
# entirely and the replay falls back to 15-minute bars — a coarser answer, never
# a wrong one, and the response says which resolution graded the trades.
MINUTE_FILL_MAX_SYMBOL_DAYS = 120


def _contiguous(days: list[str]) -> list[tuple[str, str]]:
    """Sorted days → the (first, last) runs they form.

    One span from min to max would be simpler and wrong: a symbol missing only
    the baseline day and only today would have every covered day in between
    re-downloaded, every run, for as long as that shape persisted."""
    runs: list[tuple[str, str]] = []
    for day in sorted(days):
        if runs and date.fromisoformat(day) - date.fromisoformat(runs[-1][1]) == timedelta(days=1):
            runs[-1] = (runs[-1][0], day)
        else:
            runs.append((day, day))
    return runs


async def fetch_replay_window_intraday(
    client: AlpacaClient,
    ds: ScannerReplayDataset,
    *,
    asset_class: str,
    report=None,
    timeframe: str = "15Min",
) -> int:
    """Download the intraday bars the replay WINDOW is missing, straight from the
    broker, and cache them. Returns how many bars were saved.

    `timeframe` is the resolution being replayed, and it selects both what is
    asked of the broker and which cache table the answer lands in (they are
    separate per resolution — see intraday_model_for). At 1Min the pass is also
    budgeted in symbol-days rather than only in requests, because a request
    returns fifteen times as much: see MINUTE_FILL_MAX_SYMBOL_DAYS.

    The intraday sweep cannot do this, and that is the point. It walks
    `daily_movers` rows and fetches each mover-day, so a day with no mover row
    is a day it never asks about — and a day only has a mover row once its DAILY
    bar exists. Crypto's daily bar for the current UTC day deliberately does not
    exist (it is still forming; caching the partial would freeze a wrong
    change_pct forever), so the whole mover→intraday pipeline is structurally
    incapable of covering today. A fidelity comparison of "since I switched it
    on last night" is almost entirely today, and got nothing: the crypto cache
    reached 2026-08-02 while the window ran to 2026-08-03, so every in-universe
    symbol evaluated zero bars and the report read as six missed trades.

    Also covers a name that is in the universe but was never a mover — a seeded
    symbol (see `seed_by_day`) or a pinned watchlist one. The sweep has no reason
    to have fetched those on any day.

    Which days count as missing is decided against the DAILY cache rather than
    the calendar: a day the symbol has a daily bar for is a day it traded, and
    anything after the newest daily bar in the cache is too new to have been
    swept. Weekends and holidays therefore never register as gaps, so this does
    not re-request empty ranges on every run.
    """
    from qt.services import barcache, barsweep

    crypto = asset_class == "crypto"
    intraday_model = intraday_model_for(asset_class, timeframe)
    daily_model = barcache.CryptoDailyBar if crypto else barcache.DailyBar
    if not ds.union or not ds.baseline_day or not ds.end_day:
        return 0

    window_days = _days_between(ds.baseline_day, ds.end_day)
    # TODAY is never "already covered", however many of its bars are cached. A
    # window that ends now is asking about the last few hours, and the bars for
    # those hours did not exist when the earlier run of this fetched the day.
    # Treating a partially-fetched today as done is how a comparison run twice in
    # an afternoon would keep answering with the morning's data.
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    sess = barcache.session()
    try:
        have = barcache.cached_intraday_days(
            sess, ds.union, ds.baseline_day, model=intraday_model, end_day=ds.end_day
        )
        swept_to = barcache.latest_daily_day(sess, model=daily_model) or ""
        wanted: list[tuple[str, str, str]] = []
        symbol_days = 0
        for symbol in sorted(ds.union):
            covered = have.get(symbol, set())
            traded = {b["t"][:10] for b in ds.daily.get(symbol) or []}
            missing = [
                d
                for d in window_days
                if (d not in covered or d == today) and (d in traded or d > swept_to)
            ]
            if missing and len(missing) <= REPLAY_FILL_MAX_DAYS_PER_SYMBOL:
                symbol_days += len(missing)
                wanted += [(symbol, first, last) for first, last in _contiguous(missing)]
        # The VOLUME budget, which only bites at one minute — see
        # MINUTE_FILL_MAX_SYMBOL_DAYS. None at 15 minutes, so that pass behaves
        # exactly as it always has.
        budget = MINUTE_FILL_MAX_SYMBOL_DAYS if timeframe == "1Min" else None
        over_budget = budget is not None and symbol_days > budget
        if not wanted or len(wanted) > REPLAY_FILL_MAX_REQUESTS or over_budget:
            if wanted:
                log.info(
                    "replay window fill skipped (%s): %s requests needed (cap %s), "
                    "%s symbol-days (budget %s)",
                    timeframe, len(wanted), REPLAY_FILL_MAX_REQUESTS, symbol_days, budget,
                )
            return 0

        saved = 0
        for index, (symbol, first, last) in enumerate(wanted, start=1):
            if report:
                report(
                    f"Downloading the {_bar_label(timeframe)} this window is missing — {symbol} "
                    f"({index} of {len(wanted)})",
                    int(index * 100 / len(wanted)),
                )
            end_exclusive = (date.fromisoformat(last) + timedelta(days=1)).isoformat()
            try:
                # Two attempts, like every other interactive fetch here: somebody
                # is watching a progress bar, and one symbol failing is not a
                # failed run.
                data = await barsweep._bars_with_retry(
                    client, [symbol], timeframe, first, end_exclusive,
                    asset_class=asset_class, attempts=2, retry_delay=1.0,
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("replay window bar fetch failed for %s: %s", symbol, exc)
                continue
            for name, bars in data.items():
                if bars:
                    saved += barcache.save_intraday_bars(
                        sess, name, bars, model=intraday_model
                    )
            sess.commit()
        return saved
    finally:
        sess.close()


async def ensure_replay_intraday(
    client: AlpacaClient,
    ds: ScannerReplayDataset,
    params: dict,
    *,
    asset_class: str,
    days: int,
    replay_top_n: int,
    scanner_cfg: dict | None,
    report=None,
    end: datetime | None = None,
    always_eligible: list[str] | None = None,
    window_hours: float | None = None,
    seed_by_day: dict[str, list[str]] | None = None,
    allow_empty_intraday: bool = False,
    timeframe: str = "15Min",
) -> tuple[ScannerReplayDataset, bool]:
    """Fetch the intraday bars this replay is missing, then re-read the dataset.

    Replay only uses intraday bars when they cover the WHOLE mover set, so a
    single uncached name silently demoted the run to daily bars — where a stop
    can only trigger at the close. The fix used to be a manual trip to Settings
    to run a sweep, which is a strange thing to ask of someone who just pressed
    "backtest": the app knows exactly which bars are missing, so it fetches them.

    Bounded by design: `since_day` limits the pull to the replay window, and the
    sweep is resumable per (day, symbol), so the download happens once and every
    later run over the same period is a cache read. Returns the dataset to use —
    never a worse one than it was handed, because a partial fill that lost bars
    would be a downgrade dressed as a top-up.

    TWO fetches, because they close different holes. The mover-day SWEEP fills a
    universe the cache never had; `fetch_replay_window_intraday` fills the days
    the sweep cannot reach — the current, still-forming day, and names that were
    never movers. Only the second runs when the dataset already says
    `used_intraday`: since a short window accepts PARTIAL coverage (see the
    dataset's note), "already intraday" can mean one symbol with one bar, and
    returning early there is precisely how a four-hour window with the cache a
    day behind replayed nothing at all.

    At `timeframe="1Min"` only the FIRST of the two fetches runs. The mover-day
    sweep produces 15-minute bars and nothing else, so running it here would
    spend a long download filling a table this dataset is not reading and then
    reload to find exactly as little as before. The window fill can fetch any
    resolution, and on a short window it is the pass that matters anyway."""
    if not _wants_intraday_replay(params):
        return ds, False

    from qt.services import barcache, barsweep

    crypto = asset_class == "crypto"
    sweep_fn = barsweep.sweep_crypto_intraday if crypto else barsweep.sweep_intraday_movers

    def reload() -> ScannerReplayDataset:
        return load_scanner_replay_dataset(
            asset_class, days, replay_top_n, scanner_cfg, end=end,
            always_eligible=always_eligible, window_hours=window_hours,
            seed_by_day=seed_by_day, allow_empty_intraday=allow_empty_intraday,
            intraday_timeframe=timeframe,
        )

    def better(fresh: ScannerReplayDataset) -> bool:
        """Never hand back less than we were given. A reload that lost intraday
        coverage, or bars, is not an improvement — and quietly adopting it would
        turn a failed top-up into a shrunken replay nobody asked for."""
        if ds.used_intraday and not fresh.used_intraday:
            return False
        if fresh.used_intraday and not ds.used_intraday:
            return True
        return sum(map(len, fresh.bars.values())) > sum(map(len, ds.bars.values()))

    # Always first: cheap when there is nothing to do (one DISTINCT over the
    # cache), and it is the only pass that can reach the window's newest days.
    try:
        filled = await fetch_replay_window_intraday(
            client, ds, asset_class=asset_class, report=report, timeframe=timeframe,
        )
    except Exception as exc:  # noqa: BLE001 — a failed top-up is not a failed backtest
        log.warning("replay window intraday fill failed (%s); continuing", exc)
        filled = 0
    topped_up = False
    if filled:
        fresh = await asyncio.to_thread(reload)
        if better(fresh):
            ds, topped_up = fresh, True

    if ds.used_intraday:
        return ds, topped_up

    if timeframe != "15Min":
        # The sweep below only ever fetches 15-minute bars. At any other
        # resolution it would download a great deal to fill a table this dataset
        # does not read, so it is skipped and the caller decides what to do with
        # a dataset that still has no coverage — which for the fidelity replay is
        # to drop back to 15-minute bars and say so.
        return ds, topped_up

    if report:
        report("Downloading the 15-minute bars this replay is missing…", 0)

    def on_progress(done: int, total: int, saved: int, bars: int) -> None:
        if report and total:
            report(
                f"Downloading 15-minute bars — day {done} of {total} ({bars:,} bars cached)",
                int(done * 100 / total),
            )

    sess = barcache.session()
    try:
        # since_day: only the window being replayed. Without it this would sweep
        # every mover-day the cache has ever known, which on a 30-day backtest
        # means downloading a year of bars nobody asked for. It has no far edge,
        # so a window that closed in the past still sweeps forward to today —
        # more than that run needs, but the sweep is idempotent and per-day
        # resumable, so it is paid once and every later window inside it is then
        # a cache read.
        # Less patient than the manual sweep on purpose. That one runs unattended
        # and can afford three attempts with exponential backoff; this one runs
        # while somebody watches a progress bar, and the sweep is resumable per
        # (day, symbol), so a day lost to a blip is picked up by the next run
        # rather than lost. Grinding 3 x backoff through every day of a dead API
        # key would stall an interactive backtest for many minutes.
        await sweep_fn(
            client, sess, since_day=ds.start_day, progress=on_progress,
            attempts=2, retry_delay=1.0,
        )
    except Exception as exc:  # noqa: BLE001 — a failed top-up is not a failed backtest
        log.warning("auto intraday top-up failed (%s); continuing on daily bars", exc)
        return ds, topped_up
    finally:
        sess.close()

    fresh = await asyncio.to_thread(reload)
    return (fresh, True) if better(fresh) else (ds, topped_up)


def _strategy_symbols(session: Session, strategy: Strategy) -> list[str]:
    """Resolve the symbols a strategy trades in a portfolio backtest. A merged
    timeline can't reconstruct the scanner's historical daily picks, so a
    scanner strategy falls back to the watchlist for its asset class — the same
    honest limitation the single-strategy fixed-list mode carries."""
    if strategy.universe == "custom":
        return [s.strip().upper() for s in (json.loads(strategy.symbols) if strategy.symbols else []) if s.strip()]
    if strategy.universe == "basket" and strategy.basket_id is not None:
        return sorted(
            {
                i.symbol
                for i in session.query(BasketItem).filter(
                    BasketItem.basket_id == strategy.basket_id,
                    BasketItem.asset_class == strategy.asset_class,
                )
            }
        )
    # scanner | watchlist | both → the watchlist for this asset class
    return sorted(
        {
            i.symbol
            for i in session.query(WatchlistItem).filter(
                WatchlistItem.asset_class == strategy.asset_class
            )
        }
    )


def _uses_rsi(params: dict) -> bool:
    """Every RSI rule, in ONE place. The entry band, the entry CROSSING, and all
    three RSI exits.

    This existed as three separate copies in this module and the new direction
    rules were added to none of them. A strategy whose only RSI rule was the
    crossing therefore did not count as using a daily signal, so no daily bars
    were fetched, so _annotate_rsi fell back to computing RSI off the REPLAY's
    own bars — hourly ones. "RSI three bars ago" then meant three HOURS ago
    rather than three sessions, which is not the signal live evaluates, and the
    strategy sat out the entire window. Measured on "Favorites - optimized 4 aug
    v2 no macd", which took four trades and returned 0%."""
    entry = params.get("entry") or {}
    exit_rules = params.get("exit") or {}
    return (
        float(entry.get("rsi_min", 0) or 0) > 0
        or float(entry.get("rsi_max", 0) or 0) > 0
        or float(entry.get("rsi_cross_above", 0) or 0) > 0
        or float(exit_rules.get("exit_rsi_above", 0) or 0) > 0
        or float(exit_rules.get("exit_rsi_below", 0) or 0) > 0
        or bool(exit_rules.get("exit_rsi_falling"))
    )


def _uses_daily_only_signals(params: dict) -> bool:
    """MACD, RSI or ATR on, and NOT the (intraday-only) VWAP rule. The live engine
    computes all three from COMPLETED DAILY bars, so an intraday backtest computes
    them on intraday closes — twitchy and unlike live. Such a strategy must be
    backtested on 1Day. (If VWAP is also on, the strategy is misconfigured; the
    VWAP guard — which needs intraday — takes precedence and this one stands down
    so the two don't deadlock.)

    ATR belongs here for the same reason as MACD/RSI, and more sharply: a
    "14-period ATR" over 15-minute bars measures three and a half HOURS of range,
    not fourteen days, so it comes out a fraction of the real figure and every
    stop derived from it lands absurdly tight. _needs_warmup has always counted
    ATR as a daily indicator; this function disagreeing with it was the bug —
    ATR strategies were fetched warm-up history and then classified as if they
    had no daily signal at all."""
    entry = params.get("entry", {})
    exit_rules = params.get("exit", {})
    atr = params.get("atr") or {}
    if entry.get("require_above_vwap"):
        return False
    macd = bool(entry.get("require_macd_bullish") or exit_rules.get("exit_on_macd_bearish"))
    rsi = _uses_rsi(params)
    # ANY ATR feature: the volatility stop, the volatility TRAIL, or ATR-based
    # position sizing. All three read the same daily-bar ATR series.
    atr_on = (
        float(atr.get("stop_mult", 0) or 0) > 0
        or float(atr.get("trail_mult", 0) or 0) > 0
        or float(atr.get("risk_usd", 0) or 0) > 0
    )
    return macd or rsi or atr_on


def _has_price_triggered_exit(params: dict) -> bool:
    """Whether any exit fires off the PRICE itself — stop-loss, trailing stop or
    take-profit. These are the rules a once-a-day daily replay cannot simulate:
    it checks them at the close, so a position that dipped through its stop and
    recovered is scored a winner.

    The ATR stop counts too — it IS a stop, just one whose distance is set by
    volatility instead of a fixed percentage. It has to be named explicitly
    because it REPLACES stop_loss_pct, so a strategy could carry a zero fixed
    stop and still exit on price."""
    x = params.get("exit") or {}
    atr = params.get("atr") or {}
    return any(
        float(x.get(k, 0) or 0) > 0
        for k in ("stop_loss_pct", "trailing_stop_pct", "take_profit_pct",
                  "exit_giveback_pct")
    ) or float(atr.get("stop_mult", 0) or 0) > 0 or float(
        atr.get("trail_mult", 0) or 0
    ) > 0


def daily_signal_names(params: dict) -> str:
    """Which daily-only signals a strategy actually uses, for error messages.

    The guards below used to say "MACD/RSI" because those were the only two.
    Adding ATR made that wording wrong in the one place it matters most: a user
    with an ATR scalper and no MACD anywhere gets told their strategy uses MACD,
    goes looking for it, and finds nothing. Name what is really there."""
    entry = params.get("entry") or {}
    exit_rules = params.get("exit") or {}
    atr = params.get("atr") or {}
    names: list[str] = []
    if entry.get("require_macd_bullish") or exit_rules.get("exit_on_macd_bearish"):
        names.append("MACD")
    if _uses_rsi(params):
        names.append("RSI")
    if float(atr.get("stop_mult", 0) or 0) > 0:
        names.append("the ATR stop")
    if float(atr.get("trail_mult", 0) or 0) > 0:
        names.append("the ATR trailing stop")
    if float(atr.get("risk_usd", 0) or 0) > 0:
        names.append("ATR position sizing")
    return " and ".join(names) if names else "daily signals"


def _mixed_resolution(params: dict) -> bool:
    """The strategy that needs BOTH resolutions at once: its signals are daily
    (MACD/RSI — the live engine reads them off completed daily closes) but its
    exits are price-triggered (a stop only means something intraday). On one bar
    stream it can have correct signals with fake stops (1Day) or correct stops
    with twitchy signals (15Min) — never both. Mixed-resolution replay gives it
    both: indicators from the daily series, entries/exits over 15-minute bars."""
    return _uses_daily_only_signals(params) and _has_price_triggered_exit(params)


# Calendar days of history fetched BEFORE the tested window so the daily
# indicators (MACD/RSI/ATR) are defined from day one of the window — the backtest
# equivalent of the live engine's 120-day MACD lookback. ~150 days ≈ 100 trading
# bars, comfortably above MACD's slow+signal warm-up.
WARMUP_DAYS = 150

# Below this, a daily replay cannot represent the window at all: a daily bar is
# stamped at the START of its day, so a window of a few hours contains no daily
# bar even when the day itself has one. A replay that falls back to daily here
# does not produce a rough answer, it produces an empty one — and the fidelity
# report then reads as "the backtest missed all your trades".
MIN_HOURS_FOR_DAILY_REPLAY = 48

# Above this, a replay does not get 1-minute bars — a ceiling rather than a
# floor, because the finer resolution is bought with download volume rather than
# demanded by anything in the strategy.
#
# The live engine evaluates every 60 seconds. A 15-minute replay is therefore 15x
# coarser than the thing it is grading, and a signal that appeared and vanished
# inside a quarter hour is invisible to it — measured: a coin bought live at
# 13:18:18, between the replay's 13:15 and 13:30 bars, came back as a trade the
# replay missed, which then left the replay a position slot spare, so it bought
# something else and THAT came back as a trade it invented. One blind spot,
# two false verdicts pointing opposite ways.
#
# A day is where paying for that stops being worth it. A comparison longer than
# 24 hours is asking whether the strategy behaves the same in general, which
# 15-minute resolution answers; a comparison shorter than a day is asking about
# particular fills, which nothing coarser can settle. 24h also bounds the
# download: with the baseline prefix in front of it, ~3 days of minute bars per
# crypto symbol and ~6 per stock.
MAX_HOURS_FOR_MINUTE_REPLAY = 24

# How many replayed symbols are worth listing in a result. Above this the list
# is dropped and only `universe_size` is reported — a 180-day stock sweep can
# touch thousands of names, and nobody reads that. Below it, naming them lets
# the fidelity comparison tell "outside the universe" apart from "watched and
# passed", which is the difference between a coverage gap and a real bug.
UNIVERSE_LIST_CAP = 1000

# Calendar days of history a replay needs BEFORE its window even when no daily
# indicator is involved — purely so the first bar has a day-gain baseline to
# measure against. Crypto measures against the close ~24h back (one prior day,
# plus one so a partial first day can't be the only candidate); stocks measure
# against the previous SESSION close, which can be four days back over a long
# weekend. Without this the first day of every window is silently unusable:
# `_prepare` leaves change_pct None and `_simulate` skips those bars, so a short
# window evaluates NOTHING and the report blames the strategy's rules.
BASELINE_WARMUP_DAYS = {"crypto": 2, "stock": 5}


def warmup_days_for(params: dict, asset_class: str) -> int:
    """How much history to fetch before the window, from the strategy's OWN
    indicator settings rather than one constant for everybody.

    Deliberately delegates to the LIVE engine's `_daily_lookback_days`: the
    replay is only faithful if its MACD/RSI/ATR see the same span of history the
    live engine gave them. A 26/9 MACD needs 35 completed bars, a 50-period ATR
    needs 110 — one flat number is either wasteful for the first or wrong for
    the second, and 'wrong' means the indicator is undefined for the opening
    stretch of the window and every entry there is silently dropped.
    """
    from qt.services.engine import _daily_lookback_days

    baseline = BASELINE_WARMUP_DAYS.get(asset_class, 5)
    if not _needs_warmup(params):
        return baseline
    atr = params.get("atr") or {}
    want_atr = (
        float(atr.get("stop_mult", 0) or 0) > 0
        or float(atr.get("trail_mult", 0) or 0) > 0
        or float(atr.get("risk_usd", 0) or 0) > 0
    )
    return max(baseline, _daily_lookback_days(params, want_atr))


def _rank_needs_daily(strategy) -> tuple[bool, int]:
    """(does this strategy's LIVE ranking metric come from daily bars, how much
    history it needs). A ranked strategy scored on relative_strength / rs_vs_spy /
    return_30d / rsi needs a daily series even when it uses no daily INDICATOR at
    all — without one the replay cannot reproduce live's ordering and says so
    instead of quietly evaluating the whole pool."""
    cfg = backtest.ranking_config(
        {
            "universe": strategy.universe,
            "rank_enabled": strategy.rank_enabled,
            "rank_by": strategy.rank_by,
            "top_n": strategy.top_n,
        }
    )
    if cfg is None or cfg[0] not in backtest.DAILY_RANK_METRICS:
        return False, 0
    return True, backtest.RANK_LOOKBACK_DAYS[cfg[0]]


def _needs_warmup(params: dict) -> bool:
    """Whether the strategy uses a daily indicator (MACD / RSI / ATR) that needs
    prior history to be defined — if so, the backtest fetches WARMUP_DAYS before
    the window so those signals aren't dead for the window's first ~35 bars."""
    entry = params.get("entry") or {}
    exit_rules = params.get("exit") or {}
    atr = params.get("atr") or {}
    return bool(
        entry.get("require_macd_bullish")
        or exit_rules.get("exit_on_macd_bearish")
        or _uses_rsi(params)
        or float(atr.get("stop_mult", 0) or 0) > 0
        or float(atr.get("trail_mult", 0) or 0) > 0
        or float(atr.get("risk_usd", 0) or 0) > 0
    )


@router.post("/portfolio")
async def run_portfolio(
    body: PortfolioBacktestBody,
    session: Session = Depends(get_session),
    client: AlpacaClient = Depends(require_client),
) -> dict:
    """Portfolio (multi-strategy) backtest: replay N strategies over ONE shared
    account and the global rails, exactly like the live engine. Returns the
    portfolio equity curve + metrics + a per-strategy contribution breakdown."""
    # De-dupe while preserving the order the caller selected them in.
    seen: set[int] = set()
    ids = [sid for sid in body.strategy_ids if not (sid in seen or seen.add(sid))]
    strategies = [session.get(Strategy, sid) for sid in ids]
    missing = [sid for sid, s in zip(ids, strategies) if s is None]
    if missing:
        raise HTTPException(status_code=404, detail=f"Strategy not found: {missing}.")

    if body.timeframe == "1Day" and any(
        json.loads(s.params).get("entry", {}).get("require_above_vwap") for s in strategies
    ):
        raise HTTPException(
            status_code=422,
            detail="A selected strategy uses the VWAP rule, which needs intraday bars — pick 1Hour or 15Min.",
        )
    daily_signalled = [
        s for s in strategies if _uses_daily_only_signals(json.loads(s.params))
    ]
    # MIXED RESOLUTION rescues a daily-signalled strategy on an intraday timeframe:
    # the replay stays intraday for everyone while that strategy's MACD/RSI/ATR
    # come from its own daily series. It only helps a strategy with something
    # price-triggered to gain from intraday bars, though — one with no stop at all
    # learns nothing from the finer stream and should stay on daily.
    mixed_portfolio = bool(daily_signalled) and body.timeframe in ("15Min", "1Hour")
    unrescuable = [
        s for s in daily_signalled if not _has_price_triggered_exit(json.loads(s.params))
    ]
    if body.timeframe in ("15Min", "1Hour") and unrescuable:
        culprit = unrescuable[0]
        raise HTTPException(
            status_code=422,
            detail=f"\"{culprit.name}\" uses {daily_signal_names(json.loads(culprit.params))} "
            "and has no stop, trailing stop or take-profit — so an intraday replay would measure "
            "that signal over hours instead of days and gain nothing in return. Use 1 Day.",
        )

    # Resolve each strategy's own universe, then fetch bars ONCE per asset class.
    symbols_by_strategy = {s.id: _strategy_symbols(session, s) for s in strategies}
    if not any(symbols_by_strategy.values()):
        raise HTTPException(
            status_code=422,
            detail="None of those strategies resolved to any symbols — add symbols to a watchlist/basket, "
            "or pick custom-universe strategies.",
        )
    total_symbols = len({sym for syms in symbols_by_strategy.values() for sym in syms})
    if total_symbols > 40:
        raise HTTPException(status_code=422, detail="Too many symbols across those strategies (max 40; rate limits).")

    # Warm-up the daily indicators before the window (if any strategy uses them),
    # so MACD/RSI/ATR are live from day one of the tested window.
    window_start = datetime.now(timezone.utc) - timedelta(days=body.days)
    # Only DAILY bars need warm-up: on daily bars body.days calendar days yield
    # ~35 fewer usable bars once MACD warms up, hence the dead zone. Intraday
    # windows already hold plenty of bars for any indicator (and MACD/RSI are
    # 422-blocked there anyway), so warm-up is a daily-only concern.
    # The deepest requirement across the picked strategies — each read off its
    # own indicator settings — because one fetch serves all of them. The
    # per-asset-class baseline is folded in by warmup_days_for, so even an
    # indicator-free portfolio still gets a day-gain reference.
    warmup = max(
        warmup_days_for(json.loads(s.params), s.asset_class) for s in strategies
    ) if strategies else 0
    # The deep warm-up belongs to the DAILY series only. `replay()` got this
    # guard and this path did not, so a plain intraday portfolio run started its
    # 15-minute fetch up to 120 days early: for 40 crypto symbols that is roughly
    # 460,000 bars downloaded and cached to warm indicators the run never reads.
    # The trigger is a strategy carrying BOTH the VWAP rule and MACD — VWAP takes
    # precedence in _uses_daily_only_signals, so mixed_portfolio stays False and
    # the single fetch below used `start`. Results were never wrong (sim_start
    # still gates trading); the cost was download volume and rate limits.
    baseline_warmup = max(
        (BASELINE_WARMUP_DAYS.get(s.asset_class, 5) for s in strategies), default=0
    )
    start = (window_start - timedelta(days=warmup)).strftime("%Y-%m-%dT%H:%M:%SZ")
    intraday_start = (window_start - timedelta(days=baseline_warmup)).strftime("%Y-%m-%dT%H:%M:%SZ")
    by_class: dict[str, list[str]] = {}
    for s in strategies:
        by_class.setdefault(s.asset_class, [])
        for sym in symbols_by_strategy[s.id]:
            if sym not in by_class[s.asset_class]:
                by_class[s.asset_class].append(sym)
    bars_cache: dict[str, dict[str, list]] = {}
    daily_cache: dict[str, dict[str, list]] = {}
    # Strategies whose RANKING metric is derived from daily bars, and the deepest
    # lookback among them (relative_strength wants 320 days — far more than any
    # indicator warm-up). See _rank_needs_daily.
    rank_daily_cache: dict[str, dict[str, list]] = {}
    rank_daily_needed = {s.id: _rank_needs_daily(s) for s in strategies}
    rank_lookback = max((d for _, d in rank_daily_needed.values()), default=0)
    rank_start = (window_start - timedelta(days=rank_lookback + 5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rank_classes = {s.asset_class for s in strategies if rank_daily_needed[s.id][0]}
    # SPY is the benchmark rs_vs_spy is measured against and nothing else in a
    # portfolio run fetches it. Its absence would not change WHICH names make the
    # cut (its return is subtracted from every member alike), only the values —
    # but a reported value that isn't the one live computed is still a lie.
    rank_benchmark = any(
        rank_daily_needed[s.id][0] and s.rank_by == "rs_vs_spy" for s in strategies
    )
    try:
        for asset_class, syms in by_class.items():
            if syms:
                # Fetched separately from the indicator series because it reaches
                # further back, and skipped entirely on a 1Day run — there the
                # replay's own bars ARE the daily series (see _rank_daily_source).
                if asset_class in rank_classes and body.timeframe != "1Day":
                    rank_daily_cache[asset_class] = await barfetch.fetch_bars(
                        client,
                        syms + ["SPY"] if (rank_benchmark and asset_class == "stock") else syms,
                        asset_class, "1Day", rank_start,
                    )
                # Read-through the bar cache, like the single-strategy run. This
                # is the HEAVIEST fetch in the app — every symbol of every picked
                # strategy — and it was the one path still re-downloading the same
                # history on every run.
                if mixed_portfolio:
                    # Two fetches with deliberately different windows, exactly like
                    # the single-strategy mixed run: the DAILY series reaches back
                    # over the warm-up so indicators are alive from day one, while
                    # the intraday series covers only the tested window (fetching
                    # warm-up intraday too would multiply the download for bars
                    # that could never trade).
                    daily_cache[asset_class] = await barfetch.fetch_bars(
                        client, syms, asset_class, "1Day", start
                    )
                    bars_cache[asset_class] = await barfetch.fetch_bars(
                        client, syms, asset_class, body.timeframe,
                        window_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    )
                else:
                    bars_cache[asset_class] = await barfetch.fetch_bars(
                        client, syms, asset_class, body.timeframe,
                        start if body.timeframe == "1Day" else intraday_start,
                    )
    except AlpacaError as exc:
        raise HTTPException(status_code=502, detail=f"Bar download failed ({exc.status_code}): {exc}")

    bars_by_strategy = {
        s.id: {sym: bars_cache.get(s.asset_class, {}).get(sym, []) for sym in symbols_by_strategy[s.id]}
        for s in strategies
    }
    # Only strategies that actually read a daily indicator get a daily series;
    # the rest are untouched, so a plain momentum book replays exactly as before.
    daily_by_strategy = {
        s.id: {
            sym: daily_cache.get(s.asset_class, {}).get(sym, [])
            for sym in symbols_by_strategy[s.id]
        }
        for s in strategies
        if mixed_portfolio and _needs_warmup(json.loads(s.params))
    } or None
    # A daily-bar RANKING metric needs daily history even when nothing in the book
    # is mixed-resolution — see the same guard in replay(). One fetch per asset
    # class, reused by every strategy in it.
    rank_daily_by_strategy = {
        s.id: {
            sym: rank_daily_cache.get(s.asset_class, {}).get(sym, [])
            for sym in [*symbols_by_strategy[s.id], *(["SPY"] if s.rank_by == "rs_vs_spy" else [])]
        }
        for s in strategies
        if rank_daily_needed[s.id][0] and s.asset_class in rank_daily_cache
    } or None
    strategy_dicts = [
        {
            "id": s.id,
            "name": s.name,
            "asset_class": s.asset_class,
            # The universe cut the live engine makes every cycle — without these
            # a ranked strategy in a portfolio run drew from its whole pool while
            # live drew from its top-N (see backtest.ranking_config).
            "universe": s.universe,
            "rank_enabled": s.rank_enabled,
            "rank_by": s.rank_by,
            "top_n": s.top_n,
            "swing_mode": s.swing_mode,
            "allow_concurrent_symbol": s.allow_concurrent_symbol,
            "sizing_usd": s.sizing_usd,
            "sleeve_usd": s.sleeve_usd,
            "max_positions": s.max_positions,
            "params": json.loads(s.params),
        }
        for s in strategies
    ]
    # Day bucketing: all-crypto portfolios key by the UTC day; any stock present
    # keys by the ET session day (mixed books use the stock convention).
    market = "crypto" if all(s.asset_class == "crypto" for s in strategies) else "stock"

    # OFF the event loop — see the note on run()'s call. get_risk() returns a
    # plain dict, so nothing DB-bound crosses the thread boundary.
    _report("Replaying the portfolio…")
    result = await asyncio.to_thread(
        backtest.run_portfolio_backtest,
        strategy_dicts, bars_by_strategy, get_risk(session),
        starting_cash=body.starting_cash, spread_pct=body.spread_pct, market=market,
        sim_start=window_start if warmup else None,
        daily_bars_by_strategy=daily_by_strategy,
        rank_daily_by_strategy=rank_daily_by_strategy,
        # THE SAME RATES BOTH SINGLE-STRATEGY PATHS CHARGE. Without this an
        # all-crypto portfolio was scored commission-free while the same
        # strategies run one at a time paid 0.25% a side — half a percent a round
        # trip — so the two screens disagreed by construction and the portfolio
        # one was always the flattering answer. Keyed by asset class so a mixed
        # book charges its crypto sleeve without inventing a stock commission.
        fee_pct_by_class=DEFAULT_FEE_PCT,
    )
    if "error" in result:
        raise HTTPException(status_code=422, detail=result["error"])

    result["timeframe"] = body.timeframe
    result["mixed_resolution"] = bool(daily_by_strategy)
    if daily_by_strategy:
        result["signal_timeframe"] = "1Day"
    result["days"] = body.days
    return result


def _stamp_window(result: dict, body: BacktestBody, start: datetime, end: datetime) -> None:
    """Record the exact period replayed, but ONLY when the caller pinned one.

    An open-ended run's window ends "now", and stamping that would put a moving
    timestamp on a result whose shape is otherwise stable — and invite a UI to
    print a precise end for a period whose end is just when you pressed the
    button. A caller that asked for a window gets it back, so it can prove which
    slice this result covers."""
    if body.window_start is None and body.window_end is None:
        return
    result["window_start"] = start.isoformat()
    result["window_end"] = end.isoformat()


def replay_strategy(config) -> dict:
    """The fields a replay actually reads, lifted out of a strategy config.

    Two things produce such a config and they are the same shape by construction:
    the live Strategy row, and a StrategyConfigVersion snapshot — which is that
    row's serialization, frozen at the moment it was saved. One reader for both
    is the point: replaying the config that PRODUCED a set of trades must go down
    the identical path as replaying today's, or the two results differ for
    reasons that have nothing to do with the config.

    Accepts the ORM row or the decoded snapshot dict. `params` is JSON text on the
    row and already a dict in a snapshot, so it is decoded only when it needs to
    be. Everything else a strategy carries — its name, whether it is enabled, its
    provenance — is irrelevant here, and passing the whole object through would
    invite the replay to quietly start depending on it."""
    get = config.get if isinstance(config, dict) else lambda k: getattr(config, k)
    # Config versions written before a column existed simply lack the key, and an
    # ORM row from a partially-migrated DB can lack the attribute. Both must land
    # on the model's own default rather than on None, because for the ranking
    # fields below None means "don't rank" — which is precisely the permissive
    # behaviour this replay path was fixed to stop.
    def get_or(key, default):
        # Absent and NULL collapse to the same answer on purpose: `.get`/`getattr`
        # yield None for a key that was never written, and a partly-migrated row
        # yields None for a column that exists but is empty. One coalesce covers
        # both — passing `default` to `.get` as well would be dead code.
        value = config.get(key) if isinstance(config, dict) else getattr(config, key, None)
        return default if value is None else value

    params = get("params")
    return {
        "asset_class": get("asset_class"),
        # The replay reads this to decide whether the day's risers are eligible,
        # so it has to come from the SNAPSHOT like everything else here — a
        # strategy that has since been switched from "scanner" to "watchlist"
        # must still replay as the scanner strategy it was.
        "universe": get("universe"),
        # THE TOP-N CUT. The live engine ranks a ranked strategy's pool every
        # cycle and evaluates only the best `top_n` of it; without these three the
        # replay evaluated the whole pool and every result came back more
        # permissive than reality. Carried on the SNAPSHOT like everything else
        # here, so a strategy whose ranking metric has since been changed still
        # replays as the strategy it was.
        "rank_enabled": get_or("rank_enabled", False),
        "rank_by": get_or("rank_by", "momentum_today"),
        "top_n": get_or("top_n", 10),
        "swing_mode": get("swing_mode"),
        # Whether this strategy may hold a symbol ANOTHER strategy already holds.
        # Only matters once a replay is told what the rest of the account held
        # (BacktestBody.account_positions): live relaxes exactly this one rail
        # when it is on — the caps and the cooldown stay account-wide.
        "allow_concurrent_symbol": get_or("allow_concurrent_symbol", False),
        "sizing_usd": get("sizing_usd"),
        "sleeve_usd": get("sleeve_usd"),
        "max_positions": get("max_positions"),
        "params": json.loads(params) if isinstance(params, str) else params,
    }


DCA_UNSUPPORTED = (
    "This is a DCA sleeve, and the backtester cannot replay one yet — so rather "
    "than hand you a number that describes a different strategy, it refuses."
    "\n\n"
    "Live, a DCA sleeve buys its fixed symbol list on a calendar: "
    "`_consider_entries` hands it to `_consider_dca_entries` and skips "
    "`evaluate_entry` entirely, so the momentum rules on the card are dead, "
    "and each scheduled buy is an independent LOT — several lots of one "
    "symbol can be open at once. The replay has no such branch: it would "
    "evaluate the momentum rules live never reads, and never buy on the "
    "schedule that is the sleeve's whole point."
    "\n\n"
    "Simulating it properly needs the replay to hold more than one open lot "
    "per symbol, and both bar loops key their open positions BY symbol — so "
    "this is a structural change, not a missing branch."
)


def dca_interval_days(params: dict) -> int:
    """`> 0` means the live engine takes the DCA path and never calls
    `evaluate_entry`. Tolerates a malformed params blob the way every other
    reader here does: unreadable means "not a DCA sleeve", never a crash."""
    try:
        return int((params.get("dca") or {}).get("interval_days", 0) or 0)
    except (TypeError, ValueError):
        return 0


def refuse_if_dca(params: dict) -> None:
    """Refuse rather than silently replay the wrong strategy.

    The queue's phrasing, and it is the right one: simulate the cadence or
    refuse loudly — silence is the one wrong answer. Until the lot model exists
    this is the honest half, and it is enforced at the API rather than in the
    page so the Optimizer cannot walk around it."""
    if dca_interval_days(params) > 0:
        raise HTTPException(status_code=400, detail=DCA_UNSUPPORTED)


@router.post("")
async def run(
    body: BacktestBody,
    session: Session = Depends(get_session),
    client: AlpacaClient = Depends(require_client),
) -> dict:
    """The endpoint: look the strategy up, then replay it AS IT STANDS TODAY.

    Everything after the lookup lives in `replay()`, which takes a config rather
    than a strategy id — so a caller holding a historical snapshot can run the
    same replay against that instead, without a second implementation of a
    backtest to drift from this one."""
    strategy = session.get(Strategy, body.strategy_id)
    if not strategy:
        raise HTTPException(status_code=404, detail="Strategy not found.")
    refuse_if_dca(replay_strategy(strategy).get("params") or {})

    return await replay(
        body,
        replay_strategy(strategy),
        body.symbols,
        strategy_name=strategy.name,
        session=session,
        client=client,
    )


async def replay(
    body: BacktestBody,
    strategy_dict: dict,
    symbols: list[str],
    *,
    strategy_name: str,
    session: Session,
    client: AlpacaClient,
) -> dict:
    """Replay a GIVEN config over a window — the whole backtest, minus the "which
    strategy?" lookup.

    `strategy_dict` is what run_backtest reads (see replay_strategy) and
    `symbols` the universe it was resolved to; neither is re-derived from the
    strategy row, so passing a historical snapshot and the symbols that were in
    the basket at the time replays THAT, not today's. `body.strategy_id` is still
    carried for the caller's own bookkeeping and is deliberately not consulted
    here — the config in hand wins."""
    # An explicit symbol list still means "test exactly these". Absent one, a
    # scanner-ish universe replays the risers rather than quietly falling through
    # to the watchlist — which is what "both" (scanner AND watchlist) used to do,
    # dropping every scanner-driven trade from the replay while the result still
    # looked complete.
    if body.scanner_replay or (
        strategy_dict.get("universe") in ("scanner", "both") and not [s for s in symbols if s.strip()]
    ):
        return await _scanner_replay(body, strategy_dict, strategy_name, session, client, symbols)

    symbols = [s.strip().upper() for s in symbols if s.strip()]
    if not symbols:
        symbols = [
            i.symbol
            for i in session.query(WatchlistItem)
            .filter(WatchlistItem.asset_class == strategy_dict["asset_class"])
            .all()
        ]
    if not symbols:
        raise HTTPException(
            status_code=422,
            detail="No symbols: pass some, or add symbols to the watchlist for this asset class.",
        )
    if len(symbols) > 50:
        raise HTTPException(status_code=422, detail="Max 50 symbols per backtest (rate limits).")

    params = strategy_dict["params"]
    # Daily signals + a price-triggered exit → replay INTRADAY with the indicators
    # taken from the daily series (see _mixed_resolution). This is a property of
    # the strategy, not of the requested bar size, so it's decided here and the
    # replay timeframe follows from it.
    mixed = _mixed_resolution(params)

    if body.timeframe == "1Day" and params.get("entry", {}).get("require_above_vwap"):
        raise HTTPException(
            status_code=422,
            detail="This strategy uses the VWAP rule, which needs intraday bars — pick 1Hour or 15Min.",
        )
    if (
        body.timeframe in INTRADAY_TIMEFRAMES + ("1Hour",)
        and _uses_daily_only_signals(params)
        and not mixed
    ):
        # Still wrong for a MACD/RSI strategy with no price-triggered exit: there
        # is nothing an intraday replay would buy us, and the indicators would be
        # computed off intraday closes. Mixed-resolution runs are exempt — they
        # replay intraday precisely BECAUSE the signals stay daily.
        raise HTTPException(
            status_code=422,
            detail=f"This strategy uses {daily_signal_names(params)}, which the live engine reads "
            "off completed DAILY bars — on intraday bars it would be measured over hours instead of "
            "days and wouldn't match live. Use 1 Day.",
        )

    # Fetch WARM-UP history before the window when the strategy uses daily
    # indicators, so MACD/RSI/ATR are defined from day one of the tested window
    # (the sim ignores warm-up bars for trading — see run_backtest's sim_start).
    window_start, window_end = body.window()
    # Bars are fetched from a start and always run to NOW — there is no end on the
    # fetch — so a window that closed in the past is enforced by the SIM, not by
    # trimming the download. Costs a few extra bars; the alternative is a second
    # fetch path and a second thing to get wrong.
    sim_end = body.window_end
    # Two different warm-ups, because they answer two different questions. The
    # DAILY series needs enough history for this strategy's own indicators to be
    # defined (read off its real MACD/ATR settings, not a flat constant). The
    # replayed series needs only enough to give the first bar a day-gain baseline
    # — but it needs that ALWAYS, indicators or not, and its absence is what made
    # a short crypto window evaluate nothing at all.
    asset_class = strategy_dict["asset_class"]
    daily_warmup = warmup_days_for(params, asset_class)
    # A DAILY-BAR RANKING METRIC is a third claim on the warm-up, and a bigger one
    # than any indicator: relative_strength is a 200-day average, and live fetches
    # 320 days to compute it. Fetch short and the metric is None for the opening
    # stretch of the window, every member drops out of the ranking, and the replay
    # takes NO trades at all where live took plenty — a failure that looks exactly
    # like a strategy that stopped working. See backtest.RANK_LOOKBACK_DAYS.
    rank_cfg = backtest.ranking_config(strategy_dict)
    rank_needs_daily = rank_cfg is not None and rank_cfg[0] in backtest.DAILY_RANK_METRICS
    if rank_needs_daily:
        daily_warmup = max(daily_warmup, backtest.RANK_LOOKBACK_DAYS[rank_cfg[0]] + 5)
    baseline_warmup = BASELINE_WARMUP_DAYS.get(asset_class, 5)
    warmup = daily_warmup
    fetch_start = (window_start - timedelta(days=daily_warmup)).strftime("%Y-%m-%dT%H:%M:%SZ")
    baseline_start = (window_start - timedelta(days=baseline_warmup)).strftime("%Y-%m-%dT%H:%M:%SZ")
    window_start_str = window_start.strftime("%Y-%m-%dT%H:%M:%SZ")
    # The bar size actually REPLAYED. A mixed-resolution run is intraday by
    # definition (that is the point of it), so a caller asking for 1Day or 1Hour
    # gets 15-minute bars. But a caller who asked for a FINER intraday size is
    # asking for something mixed resolution can perfectly well give, and pinning
    # it back to 15Min would silently discard the resolution a fidelity window
    # requested.
    replay_timeframe = (
        (body.timeframe if body.timeframe in INTRADAY_TIMEFRAMES else "15Min")
        if mixed
        else body.timeframe
    )
    daily_bars: dict[str, list[dict]] | None = None
    # Read-through the bar cache (qt.services.barfetch): the same year of history
    # was being re-downloaded on every run. Only the missing recent edge is
    # fetched, and any cache trouble degrades to a plain Alpaca fetch.
    _report(f"Downloading {len(symbols)} symbol{'' if len(symbols) == 1 else 's'} of history…")
    # A daily indicator on an INTRADAY replay needs the daily series whether or
    # not `mixed` fired. `mixed` rests on _uses_daily_only_signals, which returns
    # False the moment the VWAP rule is on — it calls that combination
    # "misconfigured" and stands down. But VWAP + MACD is an ordinary strategy and
    # live runs it without difficulty: VWAP intraday, MACD off completed daily
    # closes. Standing down meant no daily fetch, so _annotate_macd fell back to
    # the replay's OWN closes and computed a ONE-MINUTE MACD against live's daily
    # one — two different indicators sharing a name. Measured on strategy 25:
    # live bought AMZN at 14:01 on a bullish daily MACD; the replay sat through
    # 74 bars of "MACD not bullish" waiting for a 1-minute crossover at 14:26.
    # _needs_warmup is the same question without the VWAP veto, and is already
    # what decides whether warm-up history is worth fetching at all.
    daily_signals_intraday = _needs_warmup(params) and replay_timeframe != "1Day"
    try:
        if mixed or daily_signals_intraday:
            # Two fetches, deliberately different windows: the DAILY series reaches
            # back over the warm-up so the indicators are defined from day one,
            # while the intraday series covers only the tested window — 15-minute
            # crypto bars are 96/symbol/day, so fetching warm-up intraday too would
            # multiply the download for bars that could never trade.
            daily_bars = await barfetch.fetch_bars(
                client, symbols, strategy_dict["asset_class"], "1Day", fetch_start
            )
            bars = await barfetch.fetch_bars(
                client, symbols, strategy_dict["asset_class"], replay_timeframe, baseline_start
            )
        else:
            # 1Day replays reach back over the indicator warm-up; intraday ones
            # only need the baseline days — 15-minute crypto bars run 96 per
            # symbol per day, so pulling 120 days of them would be an enormous
            # download for bars that exist only to be a reference price.
            bars = await barfetch.fetch_bars(
                client, symbols, strategy_dict["asset_class"], replay_timeframe,
                fetch_start if replay_timeframe == "1Day" else baseline_start,
            )
        # The ranking's own daily source. A 1Day replay IS a daily series and a
        # mixed run already has one, so this only fires for the remaining case —
        # an intraday replay of a strategy with no daily INDICATOR but a daily
        # RANKING metric — plus SPY, which nothing else ever fetches and which
        # rs_vs_spy is measured against.
        rank_daily: dict[str, list[dict]] | None = None
        if rank_needs_daily:
            if daily_bars is None and replay_timeframe != "1Day":
                rank_daily = await barfetch.fetch_bars(
                    client, symbols, strategy_dict["asset_class"], "1Day", fetch_start
                )
            if rank_cfg[0] == "rs_vs_spy":
                spy = await barfetch.fetch_bars(client, ["SPY"], "stock", "1Day", fetch_start)
                rank_daily = {**(rank_daily or daily_bars or {}), "SPY": spy.get("SPY") or []}
    except AlpacaError as exc:
        raise HTTPException(status_code=502, detail=f"Bar download failed ({exc.status_code}): {exc}")

    # Day bucketing must be the SAME on both sides of a mixed run, or the daily
    # series and the intraday bars disagree about which day a bar belongs to and
    # the look-ahead frontier leaks. Crypto daily bars are stamped 00:00Z, so
    # crypto buckets by the UTC day — as the live engine, the optimizer and the
    # scanner replay all do.
    #
    # This used to apply only to MIXED crypto runs, "keeping the historical
    # 'stock' default untouched" for the rest. That default was wrong: bucketing
    # a 00:00Z daily bar by ET files it under the PREVIOUS day, and it left the
    # plain crypto backtest as the only place in the app not using the live
    # convention — so the optimizer tuned against one definition of a day and the
    # backtest graded against another.
    market = "crypto" if strategy_dict["asset_class"] == "crypto" else "stock"
    # Run the replay OFF the event loop. It is pure CPU over every bar — a
    # 350-day, 30-symbol run is hundreds of thousands of them — and a coroutine
    # that never awaits owns the loop for its whole duration. That would freeze
    # the engine tick, every other request, AND the very /job polls that exist to
    # keep each request short, so the proxy would time out anyway. A thread lets
    # the loop breathe (Python drops the GIL periodically), which is all the
    # poller needs; get_risk() is a plain dict, so nothing DB-bound crosses over.
    _report("Replaying history…", 0)
    result = await asyncio.to_thread(
        backtest.run_backtest,
        strategy_dict, bars, get_risk(session),
        starting_cash=body.starting_cash, spread_pct=body.spread_pct,
        fee_pct=(
            body.fee_pct if body.fee_pct is not None else DEFAULT_FEE_PCT.get(strategy_dict["asset_class"], 0.0)
        ),
        market=market,
        # Mixed runs fetch intraday bars for the window only, so sim_start is a
        # belt-and-braces guard: nothing before the window can ever trade. An
        # explicit window always sets it, because there the guard is the only
        # thing standing between the replay and bars outside the period asked for.
        sim_start=window_start if (warmup or mixed or body.window_start) else None,
        sim_end=sim_end,
        daily_bars_by_symbol=daily_bars,
        rank_daily_bars_by_symbol=rank_daily,
        progress=_replay_progress,
        # Empty for every ordinary backtest — see BacktestBody.prior_loss_at.
        prior_loss_at=body.prior_loss_at or None,
        debug_log=body.debug,
        account_positions=body.account_positions or None,
        account_entries=body.account_entries or None,
        account_realized=body.account_realized or None,
        nonfill_events=body.nonfill_events or None,
        intrabar_entry_at=body.intrabar_entry_at or None,
    )
    if "error" in result:
        raise HTTPException(status_code=422, detail=result["error"])

    # The market benchmark is only informative when it's a DIFFERENT asset from
    # the one being traded. Testing BTC/USD against a "market" of BTC/USD drew
    # the same asset twice (and disagreed with itself, being sampled from daily
    # bars rather than the strategy's own). Skip it — and save the API call.
    market_symbol = "SPY" if strategy_dict["asset_class"] == "stock" else "BTC/USD"
    result["benchmark"] = None
    result["benchmark_symbol"] = None
    if [market_symbol] != symbols:
        _report(f"Fetching the {market_symbol} benchmark…")
        try:
            result["benchmark"] = await backtest.fetch_benchmark(
                # Same day bucketing as the run, or the benchmark line lands a day
                # off the equity curve (only differs for a crypto mixed run).
                client, strategy_dict["asset_class"], window_start_str, result["equity_days"],
                market=market,
            )
            result["benchmark_symbol"] = market_symbol
        except Exception:
            result["benchmark"] = None
            result["benchmark_symbol"] = None

    result["strategy_name"] = strategy_name
    result["symbols"] = symbols
    # `rails_seeded` is NOT set here, deliberately. It used to be
    # `sorted(body.prior_loss_at)` — the request body echoed straight back into the
    # result, which confirms nothing: a replay that accepted the seed and then
    # ignored it reported it as applied just the same, and the fidelity report
    # reads this field to claim the rails WERE seeded. run_backtest now derives it
    # from the rail state it actually loaded (see its `rails_seeded`), so unwiring
    # the seed empties the list instead of leaving the claim standing.
    # `timeframe` is what was REPLAYED (what the stops were checked on); on a
    # mixed run the signals came from a coarser series, named separately.
    result["timeframe"] = replay_timeframe
    result["mixed_resolution"] = mixed
    if mixed:
        result["signal_timeframe"] = "1Day"
    # The window's LENGTH, which for a plain `days` request is `days` itself.
    result["days"] = body.span_days()
    _stamp_window(result, body, window_start, window_end)
    return result


async def _scanner_replay(
    body: BacktestBody, strategy_dict: dict, strategy_name: str,
    session: Session, client: AlpacaClient, pinned_symbols: list[str] | None = None,
) -> dict:
    """Replay the historical 'today's risers' the scanner would have surfaced:
    for each past day, only that day's cached top-N movers are eligible to
    enter. Prefers cached INTRADAY bars (so intraday exits — flatten-before-
    close, VWAP, the entry window — behave for real); falls back to daily bars
    when no intraday sweep has been run. Fully offline. Works for stocks (ET
    session days) and crypto (UTC calendar days) off their SEPARATE caches."""
    # Both off the event loop: the dataset read sweeps the whole bar cache and the
    # replay is pure CPU. See the note on run()'s call.
    _report("Reading cached movers…")
    from qt.services import scanner as scanner_svc

    cfg = scanner_svc.get_config(session)
    # The dataset is read for the SPAN of the window ending where the window ends,
    # so a segment of a past period reads that segment's movers and bars — not the
    # last N days up to today. `span`/`window_end` collapse to `days`/None for an
    # ordinary request, which is why every reload below passes the same pair.
    span, window_end = body.span_days(), body.window_end
    # Hours, not days: `span_days()` rounds a 3.5-hour segment to 1, which cannot
    # distinguish "one day" from "one afternoon" — and only the second is fatal
    # to a daily replay.
    _ws, _we = body.window()
    window_hours = (_we - _ws).total_seconds() / 3600
    # A "scanner + watchlist" strategy may buy a watchlist name on ANY day, not
    # only the days it rose, so those symbols stay eligible throughout instead of
    # waiting for the movers list to supply them. Before this, "both" fell all
    # the way through to the watchlist-only path and every scanner-driven trade
    # silently vanished from the replay — a result that described half the
    # strategy while looking complete.
    pinned: list[str] = []
    if strategy_dict.get("universe") == "both":
        pinned = [s.strip().upper() for s in (pinned_symbols or []) if s.strip()] or [
            i.symbol
            for i in session.query(WatchlistItem)
            .filter(WatchlistItem.asset_class == strategy_dict["asset_class"])
            .all()
        ]
    seed_by_day = body.seed_by_day or None
    # `allow_empty_intraday`: the "no intraday at all on a short window" refusal
    # is held back until AFTER the fetch below, not dropped. Raising it here
    # would refuse a window the app is perfectly able to download bars for, and
    # tell the user to go and run a sweep that structurally cannot cover the day
    # they are asking about (see fetch_replay_window_intraday). The same refusal,
    # word for word, is re-raised below when the fetch did not help — a silent
    # empty replay presented as a verdict remains the worst outcome available.
    async def _acquire(tf: str) -> tuple[ScannerReplayDataset, bool]:
        """Read the dataset at one intraday resolution, fetching what the window
        is missing. Written once because it is now run at more than one size."""
        loaded = await asyncio.to_thread(
            load_scanner_replay_dataset, strategy_dict["asset_class"], span, body.replay_top_n, cfg,
            end=window_end, always_eligible=pinned, window_hours=window_hours,
            seed_by_day=seed_by_day, allow_empty_intraday=True, intraday_timeframe=tf,
        )
        # Missing bars are fetched here rather than sent back as an instruction
        # to go and run a sweep. Runs once per window; afterwards the cache
        # serves it.
        return await ensure_replay_intraday(
            client, loaded, strategy_dict["params"],
            asset_class=strategy_dict["asset_class"], days=span,
            replay_top_n=body.replay_top_n, scanner_cfg=cfg, report=_report,
            end=window_end, always_eligible=pinned, window_hours=window_hours,
            seed_by_day=seed_by_day, allow_empty_intraday=True, timeframe=tf,
        )

    # WHICH INTRADAY RESOLUTION. 15 minutes unless the caller explicitly asked
    # for minute bars — which only the fidelity comparison does, and only on a
    # window short enough to afford them (BacktestBody refuses a longer one).
    preferred = body.timeframe if body.timeframe == "1Min" else "15Min"
    ds, topped_up = await _acquire(preferred)

    # A FINER RESOLUTION MUST NEVER MEAN LESS COVERAGE. The minute tables start
    # empty and the minute fill has a volume budget, so the fetch can quite
    # legitimately come back covering fewer names than the 15-minute cache
    # already covers — and a short window accepts PARTIAL coverage, so
    # `used_intraday` alone is not the test: one symbol with one minute bar
    # passes it while twenty-nine others are dropped. That is the shape of the
    # STOCK risk in particular, where the free IEX feed is thin and a mover
    # union is large; it is also what happens on any cache that has never
    # fetched a minute bar before.
    #
    # So: unless the minute pass covered the whole universe intraday, read the
    # 15-minute dataset too and keep whichever covers more, minute bars winning a
    # tie. Full minute coverage cannot be beaten, so the case this feature exists
    # for pays nothing extra. The resolution actually used travels back on
    # `timeframe`, so a report always says which bars graded the trades.
    def _coverage(d: ScannerReplayDataset) -> tuple[bool, int]:
        """How much of the universe this dataset can replay intraday. A daily
        fallback scores (False, 0) however many DAILY bars it has — those are not
        what either resolution is being compared on."""
        return (d.used_intraday, d.intraday_covered if d.used_intraday else 0)

    if preferred != "15Min" and _coverage(ds) < (True, len(ds.union)):
        coarse, coarse_topped = await _acquire("15Min")
        if _coverage(coarse) > _coverage(ds):
            log.info(
                "minute replay covered %s of %s symbols; falling back to 15Min (%s)",
                _coverage(ds)[1], len(ds.union), _coverage(coarse)[1],
            )
            ds, topped_up = coarse, topped_up or coarse_topped
    if window_hours < MIN_HOURS_FOR_DAILY_REPLAY and not ds.used_intraday:
        raise HTTPException(
            status_code=422,
            detail=_needs_intraday_sweep_detail(window_hours, len(ds.union)),
        )

    # Where trading may begin. The dataset now carries a BASELINE prefix before
    # the window — bars that exist only to give the first tradable bar something
    # to measure its day-gain against — so the floor has to be stated rather than
    # implied by the earliest bar. For a pinned window it is the window's start;
    # for an ordinary `days` run it is midnight of `start_day`, which is exactly
    # where the bars used to begin, so such a run replays as it always did.
    trade_from = body.window_start or datetime.strptime(
        ds.start_day, "%Y-%m-%d"
    ).replace(tzinfo=timezone.utc)

    _report("Replaying history…", 0)

    async def _replay_dataset(ds: ScannerReplayDataset) -> dict:
        """One replay of one dataset. Written once because it is run TWICE — the
        second pass below replays the same window on better bars — and the two
        drifting apart is a bug with no symptom: the second result is the one
        returned, so anything the first pass alone was given (a seed, a flag)
        would evaporate exactly when the cache was topped up.

        MACD/RSI/ATR come from the DAILY series, never from the replay stream. On
        an intraday replay that is the difference between a real indicator and one
        measured over 15-minute bars; on a daily replay it adds the warm-up so the
        signal isn't dead for the window's first weeks. Omitted entirely for a
        strategy with no daily signal, so its replay is untouched."""
        return await asyncio.to_thread(
            backtest.run_backtest,
            strategy_dict, ds.bars, get_risk(session),
            starting_cash=body.starting_cash, spread_pct=body.spread_pct,
            fee_pct=(
                body.fee_pct if body.fee_pct is not None
                else DEFAULT_FEE_PCT.get(strategy_dict["asset_class"], 0.0)
            ),
            eligible_by_day=ds.eligible_by_day, market=ds.market,
            daily_bars_by_symbol=ds.daily if _needs_warmup(strategy_dict["params"]) else None,
            # The cache is keyed by DAY, so a window that opens or closes mid-session
            # would otherwise pick up the whole of its first and last day — and the
            # dataset's baseline prefix reaches further back still. `trade_from` is
            # midnight of `start_day` for an ordinary run, i.e. where the bars used to
            # start, so such a run replays byte-identically to before.
            sim_start=trade_from,
            sim_end=window_end,
            progress=_replay_progress,
            # Empty for every ordinary backtest — see BacktestBody.prior_loss_at.
            prior_loss_at=body.prior_loss_at or None,
            debug_log=body.debug,
            account_positions=body.account_positions or None,
            account_entries=body.account_entries or None,
            account_realized=body.account_realized or None,
            nonfill_events=body.nonfill_events or None,
            intrabar_entry_at=body.intrabar_entry_at or None,
        )

    replay_daily = ds.daily if _needs_warmup(strategy_dict["params"]) else None
    result = await _replay_dataset(ds)

    # SECOND PASS. The first replay tells us which symbols were actually held and
    # for how long — something no amount of pre-fetching could know, because it
    # depends on the strategy. Those held days are the ones that need real
    # 15-minute bars: a daily fill keeps a position visible, but it can only
    # resolve the exit once per day, so the capital (and the position slot) stays
    # tied up until that bar instead of freeing at the moment the stop was hit.
    # Fetch exactly those days, then replay again on the better data.
    if ds.used_intraday and "error" not in result and ds.daily_filled_days:
        got = await fetch_held_position_bars(
            client, result, ds, asset_class=strategy_dict["asset_class"], report=_report,
            # The resolution the DATASET ended up at, not the one requested: a
            # minute run that fell back to 15 minutes must fetch and reload the
            # same table it is reading, or this pass writes bars nobody looks at.
            timeframe=ds.timeframe,
        )
        if got:
            ds = await asyncio.to_thread(
                load_scanner_replay_dataset,
                strategy_dict["asset_class"], span, body.replay_top_n, cfg, end=window_end,
                window_hours=window_hours,
                always_eligible=pinned,
                seed_by_day=seed_by_day, allow_empty_intraday=True,
                intraday_timeframe=ds.timeframe,
            )
            _report("Replaying history…", 0)
            replay_daily = ds.daily if _needs_warmup(strategy_dict["params"]) else None
            result = await _replay_dataset(ds)
            topped_up = True

    if "error" in result:
        raise HTTPException(status_code=422, detail=result["error"])

    # Broad-market benchmark (SPY for stocks, BTC/USD for crypto), best-effort —
    # the tested-symbols hold benchmark is meaningless here (many names), so drop it.
    result["hold_benchmark"] = None
    result["hold_benchmark_label"] = None
    result["benchmark"] = None
    result["benchmark_symbol"] = None
    try:
        result["benchmark"] = await backtest.fetch_benchmark(
            client, ds.benchmark_class, ds.start_day, result["equity_days"], market=ds.market
        )
        result["benchmark_symbol"] = ds.benchmark_symbol
    except Exception:
        pass

    result["strategy_name"] = strategy_name
    # `rails_seeded` comes from run_backtest, not from the request — see the note
    # in replay(). This path had the same echo and the same hole.
    result["scanner_replay"] = True
    result["replay_intraday"] = ds.used_intraday
    result["intraday_topped_up"] = topped_up  # bars were downloaded for this run
    # Mixed = the replay ran on intraday bars while the signals came from daily
    # ones. Only true when BOTH are the case; a daily replay has one resolution,
    # and a strategy with no daily signal never reads the daily series.
    result["mixed_resolution"] = bool(ds.used_intraday and replay_daily)
    if result["mixed_resolution"]:
        result["signal_timeframe"] = "1Day"
    result["replay_top_n"] = body.replay_top_n
    result["universe_size"] = len(ds.replayed)  # what was TESTED
    result["universe_dropped"] = ds.dropped
    # Names in the universe on the CALLER'S say-so rather than because the cached
    # movers produced them (see BacktestBody.seed_by_day). Empty for every
    # ordinary backtest. Reported because a comparison run against a seeded
    # universe has not reconstructed the universe the scanner really had, and
    # nothing downstream may present it as though it had.
    result["universe_seeded"] = ds.seeded
    # THE UNIVERSE IS NOT A RECONSTRUCTION OF THE LIVE ONE, and the report has to
    # be able to say so. Two reasons, both structural rather than bugs:
    #
    #   * the cached movers rank a whole day's close-to-close move, while the
    #     live crypto scanner ranks a ROLLING 24h figure recomputed every cycle
    #     (scanner.rolling_24h — THE definition of a crypto day gain in QT). The
    #     top-N at 20:57 is simply not the top-N of the day that contains it.
    #   * the cached rows are re-filtered with TODAY'S scanner settings, on
    #     purpose (see barcache.movers_between — it is what stops the replay
    #     trading names now on the exclude list). A floor raised since the trades
    #     happened therefore judges them by a rule that did not exist yet.
    result["universe_from_daily_movers"] = True
    result["scanner_config_is_current"] = True
    result["intraday_covered"] = ds.intraday_covered
    result["daily_filled_days"] = ds.daily_filled_days
    # …and how many of those actually mattered. See the function.
    result["daily_filled_held_days"] = daily_filled_held_days(result, ds)
    result["days_replayed"] = ds.days_replayed
    timeframe = ds.timeframe
    # The names actually replayed. This used to be [] with "too many to list" —
    # true for a long stock sweep, but the fidelity comparison reads this list as
    # "the universe the replay covered", and an empty one means UNKNOWN. Every
    # missed trade on a scanner strategy was then reported as "the replay was
    # watching this symbol and passed", which is a claim the report had no basis
    # for. Listed when the set is small enough to be worth carrying; a genuinely
    # huge sweep still says nothing rather than shipping thousands of strings.
    result["symbols"] = ds.replayed if len(ds.replayed) <= UNIVERSE_LIST_CAP else []
    result["timeframe"] = timeframe
    result["days"] = span
    _stamp_window(result, body, *body.window())
    return result


# ---------------------------------------------------------------------------
# Background jobs
#
# A replay that takes minutes cannot live inside an HTTP request: nginx's default
# read timeout is 60s and Cloudflare's is a FIXED 100s (it answers HTTP 524, and
# no plan setting raises it). Start the same handler as a task, hand back an id,
# and let the browser poll — every request then finishes in milliseconds.
#
# In-process and deliberately not persisted: a backtest is a read-only
# experiment, so losing jobs on restart just means running it again.
# ---------------------------------------------------------------------------

JOB_TTL_SECONDS = 900  # keep a finished job around this long for the poller
MAX_JOBS = 12


@dataclass
class BacktestJob:
    id: str
    kind: str  # "single" | "portfolio"
    running: bool = True
    # What it is doing right now, so a multi-minute run shows a pulse rather
    # than a frozen button. pct covers the replay only — the phase that has a
    # knowable length.
    phase: str = "Starting…"
    pct: int | None = None
    started_at: str = ""
    finished_at: str | None = None
    error: str | None = None
    # Carried so the browser can tell "your strategy is misconfigured" (422) from
    # "Alpaca is down" (502) — the same distinction the direct endpoint gives.
    status_code: int | None = None
    result: dict | None = field(default=None)


_jobs: dict[str, BacktestJob] = {}
_tasks: set[asyncio.Task] = set()


def _prune_jobs() -> None:
    """Drop finished jobs the browser is done with. Results carry full equity
    curves and trade lists, so they are not something to accumulate forever."""
    now = datetime.now(timezone.utc)
    for job_id, job in list(_jobs.items()):
        if job.running or not job.finished_at:
            continue
        age = (now - datetime.fromisoformat(job.finished_at)).total_seconds()
        if age > JOB_TTL_SECONDS:
            del _jobs[job_id]
    # Backstop: a browser that never polls would otherwise pile results up.
    if len(_jobs) > MAX_JOBS:
        finished = sorted(
            (j for j in _jobs.values() if not j.running),
            key=lambda j: j.finished_at or "",
        )
        for job in finished[: len(_jobs) - MAX_JOBS]:
            _jobs.pop(job.id, None)


def _new_job(kind: str) -> BacktestJob:
    _prune_jobs()
    job = BacktestJob(
        id=uuid.uuid4().hex, kind=kind, started_at=datetime.now(timezone.utc).isoformat()
    )
    _jobs[job.id] = job
    return job


async def _run_job(job: BacktestJob, handler, body, client: AlpacaClient) -> None:
    """Run one of the request handlers with a session of the JOB's own.

    The request's session is closed the moment the start endpoint returns, so the
    task opens its own. Reusing the handlers verbatim is the point: there is one
    implementation of a backtest, and polling must not become a second one that
    can drift from it.
    """
    def sink(phase: str, pct: int | None) -> None:
        job.phase = phase
        job.pct = pct

    _reporter.set(sink)
    try:
        with session_scope() as session:
            job.result = await handler(body, session, client)
    except HTTPException as exc:
        job.error = str(exc.detail)
        job.status_code = exc.status_code
    except AlpacaError as exc:
        log.exception("backtest job: bar download failed")
        job.error = f"Bar download failed ({exc.status_code}): {exc}"
        job.status_code = 502
    except asyncio.CancelledError:
        # Never leave a cancelled job looking like a finished one with no result —
        # that is the silent failure this whole change exists to remove.
        job.error = "The backtest was cancelled (the server shut down or reloaded). Run it again."
        job.status_code = 503
        raise
    except Exception as exc:  # noqa: BLE001 — surface any failure to the poller
        log.exception("backtest job failed")
        job.error = str(exc) or exc.__class__.__name__
        job.status_code = 500
    finally:
        job.running = False
        job.finished_at = datetime.now(timezone.utc).isoformat()


def _spawn(job: BacktestJob, handler, body, client: AlpacaClient) -> None:
    task = asyncio.create_task(_run_job(job, handler, body, client))
    # Without a strong reference the loop may garbage-collect a running task.
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


@router.post("/start")
async def start_backtest(
    body: BacktestBody,
    session: Session = Depends(get_session),
    client: AlpacaClient = Depends(require_client),
) -> dict:
    """Start a single-strategy backtest in the background; poll /job/{id}."""
    # Cheap existence check up front so a bad id still fails immediately, the way
    # it did before. Everything else is validated by the handler itself.
    if not session.get(Strategy, body.strategy_id):
        raise HTTPException(status_code=404, detail="Strategy not found.")
    job = _new_job("single")
    _spawn(job, run, body, client)
    return {"job_id": job.id}


@router.post("/portfolio/start")
async def start_portfolio_backtest(
    body: PortfolioBacktestBody,
    session: Session = Depends(get_session),
    client: AlpacaClient = Depends(require_client),
) -> dict:
    """Start a portfolio backtest in the background; poll /job/{id}."""
    if not body.strategy_ids:
        raise HTTPException(status_code=422, detail="Pick at least one strategy.")
    job = _new_job("portfolio")
    _spawn(job, run_portfolio, body, client)
    return {"job_id": job.id}


@router.get("/job/{job_id}")
def backtest_job(job_id: str) -> dict:
    """Poll a background backtest. While it runs the result is null; when it
    finishes, exactly one of `result` / `error` is set."""
    job = _jobs.get(job_id)
    if job is None:
        # Expired, or lost to a restart. Say which — "not found" alone reads like
        # a bug, and the honest answer is that the run has to be repeated.
        raise HTTPException(
            status_code=404,
            detail="That backtest is no longer available (it expired, or the server restarted). Run it again.",
        )
    return {
        "job_id": job.id,
        "kind": job.kind,
        "running": job.running,
        "phase": job.phase,
        "pct": job.pct,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
        "error": job.error,
        "status_code": job.status_code,
        "result": job.result,
    }
