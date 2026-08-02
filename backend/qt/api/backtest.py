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


TIMEFRAMES = ("15Min", "1Hour", "1Day")

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


async def fetch_held_position_bars(
    client: AlpacaClient,
    result: dict,
    ds: ScannerReplayDataset,
    *,
    asset_class: str,
    report=None,
) -> int:
    """Download the 15-minute bars for days a position was HELD but the cache only
    had a daily bar for, and store them. Returns how many symbol-days were filled.

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

    crypto = asset_class == "crypto"
    intraday_model = barcache.CryptoIntradayBar if crypto else barcache.IntradayBar

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
                    f"Downloading 15-minute bars for held positions — {symbol} "
                    f"({index} of {len(wanted)})",
                    int(index * 100 / len(wanted)),
                )
            end = (date.fromisoformat(last) + timedelta(days=1)).isoformat()
            try:
                data = await barsweep._bars_with_retry(
                    client, [symbol], "15Min", first, end,
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


def load_scanner_replay_dataset(
    asset_class: str, days: int, replay_top_n: int, scanner_cfg: dict | None = None,
    *, end: datetime | None = None, always_eligible: list[str] | None = None,
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
    configuration that was live during it."""
    from qt.services import barcache

    crypto = asset_class == "crypto"
    daily_model = barcache.CryptoDailyBar if crypto else barcache.DailyBar
    mover_model = barcache.CryptoDailyMover if crypto else barcache.DailyMover
    intraday_model = barcache.CryptoIntradayBar if crypto else barcache.IntradayBar
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
        eligible_by_day = {day: set(syms) | pinned for day, syms in movers.items()}
        union = sorted({s for syms in movers.values() for s in syms} | pinned)
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
        intraday_bars = barcache.cached_intraday_bars(
            cache, union, start_day, model=intraday_model, end_day=end_day
        )
        intraday_covered = sorted(s for s in union if intraday_bars.get(s))
        used_intraday = len(intraday_covered) == len(union) and bool(union)
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
            bars, filled = _fill_intraday_gaps(intraday_bars, daily, start_day)
            timeframe = "15Min"
        else:
            bars = {
                s: [b for b in series if b["t"][:10] >= start_day] for s, series in daily.items()
            }
            timeframe = "1Day"
            filled = 0  # a daily replay is daily everywhere; nothing to fill
        # Whatever the resolution, a symbol with no bars can't be replayed. Name
        # them rather than quietly shrinking the universe under the user.
        replayed = sorted(s for s in union if bars.get(s))
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
    timeframe: str = Field(default="1Hour", pattern="^(15Min|1Hour|1Day)$")
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
) -> tuple[ScannerReplayDataset, bool]:
    """Fetch the 15-minute bars this replay is missing, then re-read the dataset.

    Replay only uses intraday bars when they cover the WHOLE mover set, so a
    single uncached name silently demoted the run to daily bars — where a stop
    can only trigger at the close. The fix used to be a manual trip to Settings
    to run a sweep, which is a strange thing to ask of someone who just pressed
    "backtest": the app knows exactly which bars are missing, so it fetches them.

    Bounded by design: `since_day` limits the pull to the replay window, and the
    sweep is resumable per (day, symbol), so the download happens once and every
    later run over the same period is a cache read. Returns the dataset to use —
    the reloaded one when the fetch achieved full coverage, otherwise the
    original, because a partial fill still can't be replayed intraday and the run
    should continue on daily bars rather than fail."""
    if ds.used_intraday or not _wants_intraday_replay(params):
        return ds, False

    from qt.services import barcache, barsweep

    crypto = asset_class == "crypto"
    sweep_fn = barsweep.sweep_crypto_intraday if crypto else barsweep.sweep_intraday_movers
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
        return ds, False
    finally:
        sess.close()

    fresh = await asyncio.to_thread(
        load_scanner_replay_dataset, asset_class, days, replay_top_n, scanner_cfg, end=end,
        always_eligible=always_eligible,
    )
    return (fresh, True) if fresh.used_intraday else (ds, False)


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
    rsi = (
        float(entry.get("rsi_min", 0) or 0) > 0
        or float(entry.get("rsi_max", 0) or 0) > 0
        or float(exit_rules.get("exit_rsi_above", 0) or 0) > 0
    )
    # Either ATR feature: the volatility stop, or ATR-based position sizing.
    atr_on = float(atr.get("stop_mult", 0) or 0) > 0 or float(atr.get("risk_usd", 0) or 0) > 0
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
        for k in ("stop_loss_pct", "trailing_stop_pct", "take_profit_pct")
    ) or float(atr.get("stop_mult", 0) or 0) > 0


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
    if (
        float(entry.get("rsi_min", 0) or 0) > 0
        or float(entry.get("rsi_max", 0) or 0) > 0
        or float(exit_rules.get("exit_rsi_above", 0) or 0) > 0
    ):
        names.append("RSI")
    if float(atr.get("stop_mult", 0) or 0) > 0:
        names.append("the ATR stop")
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
        or float(entry.get("rsi_min", 0) or 0) > 0
        or float(entry.get("rsi_max", 0) or 0) > 0
        or float(exit_rules.get("exit_rsi_above", 0) or 0) > 0
        or float(atr.get("stop_mult", 0) or 0) > 0
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
    warmup = (
        WARMUP_DAYS
        if (body.timeframe == "1Day" or mixed_portfolio)
        and any(_needs_warmup(json.loads(s.params)) for s in strategies)
        else 0
    )
    start = (window_start - timedelta(days=warmup)).strftime("%Y-%m-%dT%H:%M:%SZ")
    by_class: dict[str, list[str]] = {}
    for s in strategies:
        by_class.setdefault(s.asset_class, [])
        for sym in symbols_by_strategy[s.id]:
            if sym not in by_class[s.asset_class]:
                by_class[s.asset_class].append(sym)
    bars_cache: dict[str, dict[str, list]] = {}
    daily_cache: dict[str, dict[str, list]] = {}
    try:
        for asset_class, syms in by_class.items():
            if syms:
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
                        client, syms, asset_class, body.timeframe, start
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
    strategy_dicts = [
        {
            "id": s.id,
            "name": s.name,
            "asset_class": s.asset_class,
            "swing_mode": s.swing_mode,
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
    params = get("params")
    return {
        "asset_class": get("asset_class"),
        # The replay reads this to decide whether the day's risers are eligible,
        # so it has to come from the SNAPSHOT like everything else here — a
        # strategy that has since been switched from "scanner" to "watchlist"
        # must still replay as the scanner strategy it was.
        "universe": get("universe"),
        "swing_mode": get("swing_mode"),
        "sizing_usd": get("sizing_usd"),
        "sleeve_usd": get("sleeve_usd"),
        "max_positions": get("max_positions"),
        "params": json.loads(params) if isinstance(params, str) else params,
    }


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
    if body.timeframe in ("15Min", "1Hour") and _uses_daily_only_signals(params) and not mixed:
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
    needs_warmup = _needs_warmup(params)
    warmup = WARMUP_DAYS if (body.timeframe == "1Day" or mixed) and needs_warmup else 0
    fetch_start = (window_start - timedelta(days=warmup)).strftime("%Y-%m-%dT%H:%M:%SZ")
    window_start_str = window_start.strftime("%Y-%m-%dT%H:%M:%SZ")
    # The bar size actually REPLAYED. Mixed resolution always replays 15-minute
    # bars — the finest the free feed gives — whatever the caller asked for.
    replay_timeframe = "15Min" if mixed else body.timeframe
    daily_bars: dict[str, list[dict]] | None = None
    # Read-through the bar cache (qt.services.barfetch): the same year of history
    # was being re-downloaded on every run. Only the missing recent edge is
    # fetched, and any cache trouble degrades to a plain Alpaca fetch.
    _report(f"Downloading {len(symbols)} symbol{'' if len(symbols) == 1 else 's'} of history…")
    try:
        if mixed:
            # Two fetches, deliberately different windows: the DAILY series reaches
            # back over the warm-up so the indicators are defined from day one,
            # while the intraday series covers only the tested window — 15-minute
            # crypto bars are 96/symbol/day, so fetching warm-up intraday too would
            # multiply the download for bars that could never trade.
            daily_bars = await barfetch.fetch_bars(
                client, symbols, strategy_dict["asset_class"], "1Day", fetch_start
            )
            bars = await barfetch.fetch_bars(
                client, symbols, strategy_dict["asset_class"], replay_timeframe, window_start_str
            )
        else:
            bars = await barfetch.fetch_bars(
                client, symbols, strategy_dict["asset_class"], replay_timeframe, fetch_start
            )
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
        progress=_replay_progress,
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
    ds = await asyncio.to_thread(
        load_scanner_replay_dataset, strategy_dict["asset_class"], span, body.replay_top_n, cfg,
        end=window_end, always_eligible=pinned,
    )
    # Missing 15-minute bars are fetched here rather than sent back as an
    # instruction to go and run a sweep. Runs once per window; afterwards the
    # cache serves it.
    ds, topped_up = await ensure_replay_intraday(
        client, ds, strategy_dict["params"],
        asset_class=strategy_dict["asset_class"], days=span,
        replay_top_n=body.replay_top_n, scanner_cfg=cfg, report=_report,
        end=window_end, always_eligible=pinned,
    )

    _report("Replaying history…", 0)
    # MACD/RSI/ATR come from the DAILY series, never from the replay stream. On an
    # intraday replay that is the difference between a real indicator and one
    # measured over 15-minute bars; on a daily replay it adds the warm-up so the
    # signal isn't dead for the window's first weeks. Omitted entirely for a
    # strategy with no daily signal, so its replay is untouched.
    replay_daily = ds.daily if _needs_warmup(strategy_dict["params"]) else None
    result = await asyncio.to_thread(
        backtest.run_backtest,
        strategy_dict, ds.bars, get_risk(session),
        starting_cash=body.starting_cash, spread_pct=body.spread_pct,
        fee_pct=(
            body.fee_pct if body.fee_pct is not None else DEFAULT_FEE_PCT.get(strategy_dict["asset_class"], 0.0)
        ),
        eligible_by_day=ds.eligible_by_day, market=ds.market,
        daily_bars_by_symbol=replay_daily,
        # The dataset is already trimmed to the window, so these are exactness
        # rather than safety: the cache is keyed by DAY, so a window that opens or
        # closes mid-session would otherwise pick up the whole of its first and
        # last day. Both are None for an ordinary run, which therefore replays
        # byte-identically to before.
        sim_start=body.window_start,
        sim_end=window_end,
        progress=_replay_progress,
    )

    # SECOND PASS. The first replay tells us which symbols were actually held and
    # for how long — something no amount of pre-fetching could know, because it
    # depends on the strategy. Those held days are the ones that need real
    # 15-minute bars: a daily fill keeps a position visible, but it can only
    # resolve the exit once per day, so the capital (and the position slot) stays
    # tied up until that bar instead of freeing at the moment the stop was hit.
    # Fetch exactly those days, then replay again on the better data.
    if ds.used_intraday and "error" not in result and ds.daily_filled_days:
        got = await fetch_held_position_bars(
            client, result, ds, asset_class=strategy_dict["asset_class"], report=_report
        )
        if got:
            ds = await asyncio.to_thread(
                load_scanner_replay_dataset,
                strategy_dict["asset_class"], span, body.replay_top_n, cfg, end=window_end,
                always_eligible=pinned,
            )
            _report("Replaying history…", 0)
            replay_daily = ds.daily if _needs_warmup(strategy_dict["params"]) else None
            result = await asyncio.to_thread(
                backtest.run_backtest,
                strategy_dict, ds.bars, get_risk(session),
                starting_cash=body.starting_cash, spread_pct=body.spread_pct,
                fee_pct=(
                    body.fee_pct if body.fee_pct is not None
                    else DEFAULT_FEE_PCT.get(strategy_dict["asset_class"], 0.0)
                ),
                eligible_by_day=ds.eligible_by_day, market=ds.market,
                daily_bars_by_symbol=replay_daily,
                sim_start=body.window_start,
                sim_end=window_end,
                progress=_replay_progress,
            )
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
    result["intraday_covered"] = ds.intraday_covered
    result["daily_filled_days"] = ds.daily_filled_days
    result["days_replayed"] = ds.days_replayed
    timeframe = ds.timeframe
    result["symbols"] = []  # too many to list; summarized by universe_size
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
