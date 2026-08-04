"""The wire: every place the engine reads the tape must carry WHEN it read it.

test_eval_instant_recording covers what lands in the journal. This covers the
half in front of it — the candidate builders and the exit quote fetch. Without
these, replacing every `observed_at=` at every construction site with `None`
would leave the recording tests green and the whole feature a no-op in silence
(the same coverage hole that let the stale-print wiring rot; see
test_stale_print_wiring).

Two design decisions are pinned here rather than just commented:

* the instant is measured when the PRICE snapshot lands, not after whatever else
  the same function goes on to fetch, and
* it is per snapshot CALL, so the two legs of the exit fetch do not share one.
"""

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy import inspect

from qt.broker.alpaca import AlpacaClient
from qt.db import engine as db_engine
from qt.db import session_scope
from qt.models import Strategy, Trade
from qt.services import engine

SYMBOL = "EVALWIRE/USD"
STOCK = "EVALWIRESTK"


def _snap(price: float = 1.37) -> dict:
    return {
        "latestTrade": {"p": price, "t": "2026-08-04T14:01:17Z"},
        "dailyBar": {"c": price, "vw": price, "o": price},
        "prevDailyBar": {"c": price},
    }


def _client() -> AlpacaClient:
    return AlpacaClient(key_id="k", key_secret="s")


def _assert_measured_during(observed: datetime | None, before: datetime, after: datetime) -> None:
    assert observed is not None, (
        "the builder never recorded when it looked, so every row it produces will "
        "read as 'we do not know' and the replay cannot align to it"
    )
    assert observed.tzinfo is not None, "a naive instant cannot be compared to a bar's clock"
    # Bracketed by the call itself: a hard-coded or stale constant fails here just
    # as loudly as a missing one.
    assert before <= observed <= after


async def test_symbol_candidates_record_when_the_snapshot_landed():
    with (
        patch.object(AlpacaClient, "crypto_snapshots", new=AsyncMock(return_value={SYMBOL: _snap()})),
        patch.object(engine.scanner, "crypto_rolling_stats",
                     new=AsyncMock(return_value={SYMBOL: (1.37, 5.0, 9_000.0)})),
    ):
        before = datetime.now(timezone.utc)
        cands = await engine._symbol_candidates(_client(), "crypto", [SYMBOL])
        after = datetime.now(timezone.utc)
    _assert_measured_during(cands[0].observed_at, before, after)


async def test_ranked_candidates_record_when_the_snapshot_landed():
    """The ranked path builds through _pool_metrics, so it can lose the instant
    independently of the others."""
    with (
        patch.object(AlpacaClient, "crypto_snapshots", new=AsyncMock(return_value={SYMBOL: _snap()})),
        patch.object(engine.scanner, "crypto_rolling_stats",
                     new=AsyncMock(return_value={SYMBOL: (1.37, 5.0, 9_000.0)})),
    ):
        before = datetime.now(timezone.utc)
        cands = await engine._ranked_candidates(_client(), "crypto", [SYMBOL], "momentum_today", 10)
        after = datetime.now(timezone.utc)
    _assert_measured_during(cands[0].observed_at, before, after)


async def test_the_instant_is_measured_before_the_daily_bars_fetch():
    """A bar-based ranking makes a SECOND round trip, over 320 days of history,
    after the prices are already in hand. Timing the look after that would charge
    the price snapshot for latency it did not incur — and the whole reason this
    column exists is that seconds matter."""
    bars_started: list[datetime] = []

    async def slow_bars(self, symbols, asset_class, timeframe, start):
        bars_started.append(datetime.now(timezone.utc))
        await asyncio.sleep(0.2)
        return {}

    with (
        patch.object(AlpacaClient, "crypto_snapshots", new=AsyncMock(return_value={SYMBOL: _snap()})),
        patch.object(engine.scanner, "crypto_rolling_stats",
                     new=AsyncMock(return_value={SYMBOL: (1.37, 5.0, 9_000.0)})),
        patch.object(AlpacaClient, "historical_bars", slow_bars),
    ):
        *_rest, snapped_at = await engine._pool_metrics(_client(), "crypto", [SYMBOL], "return_30d")

    assert bars_started, "test setup: the bar-based ranking should have fetched bars"
    assert snapped_at <= bars_started[0], (
        "the observation instant was taken after the daily-bars fetch, so it reports "
        "the price as having been read up to a round trip later than it was"
    )


async def test_the_scanner_branch_records_the_snapshot_it_actually_used():
    """Two claims in one run, because they are the same decision seen from both
    sides: a price that came from THIS snapshot is stamped, and a price that fell
    back to the scan's own (cached for up to 30s, read at an instant nothing
    records) is left NULL rather than borrowing the fresher timestamp."""
    strategy = Strategy(
        name="wire scanner", enabled=True, asset_class="stock", universe="scanner",
        preset="custom", params='{"entry":{},"exit":{}}', rank_enabled=False,
    )
    scan = {
        "stocks": [
            {"symbol": STOCK, "asset_class": "stock", "price": 10.0, "change_pct": 4.0},
            {"symbol": "NOSNAP", "asset_class": "stock", "price": 20.0, "change_pct": 3.0},
        ],
        "crypto": [],
    }

    with (
        patch.object(engine.scanner, "scan", new=AsyncMock(return_value=scan)),
        # Only the first symbol comes back; NOSNAP must fall back to the scan price.
        patch.object(AlpacaClient, "stock_snapshots", new=AsyncMock(return_value={STOCK: _snap(10.5)})),
    ):
        before = datetime.now(timezone.utc)
        cands, _ = await engine._candidates_for(MagicMock(), _client(), strategy, None)
        after = datetime.now(timezone.utc)

    by_symbol = {c.symbol: c for c in cands}
    _assert_measured_during(by_symbol[STOCK].observed_at, before, after)
    assert by_symbol["NOSNAP"].price == 20.0, "test setup: this one should be the fallback price"
    assert by_symbol["NOSNAP"].observed_at is None, (
        "a price carried over from the cached scan was stamped with the fresh call's "
        "instant, claiming a freshness it does not have"
    )


async def test_the_watchlist_branch_records_when_the_snapshot_landed():
    from qt.models import WatchlistItem

    strategy = Strategy(
        name="wire watchlist", enabled=True, asset_class="crypto", universe="watchlist",
        preset="custom", params='{"entry":{},"exit":{}}', rank_enabled=False,
    )
    with session_scope() as session:
        session.add(WatchlistItem(symbol=SYMBOL, asset_class="crypto"))
        session.flush()
        with (
            patch.object(AlpacaClient, "crypto_snapshots", new=AsyncMock(return_value={SYMBOL: _snap()})),
            patch.object(engine.scanner, "crypto_rolling_stats",
                         new=AsyncMock(return_value={SYMBOL: (1.37, 5.0, 9_000.0)})),
        ):
            before = datetime.now(timezone.utc)
            cands, _ = await engine._candidates_for(session, _client(), strategy, None)
            after = datetime.now(timezone.utc)
        cand = next(c for c in cands if c.symbol == SYMBOL)
        _assert_measured_during(cand.observed_at, before, after)
        session.query(WatchlistItem).filter(WatchlistItem.symbol == SYMBOL).delete()


async def test_each_exit_quote_leg_gets_its_own_instant():
    """_quotes_for issues one fetch per asset class, one after the other. Sharing
    a single instant between them would be wrong for whichever leg it was not
    measured on — and 'seconds are wrong' is the entire defect being fixed."""
    trades = [
        Trade(strategy_id=1, mode="paper", symbol=STOCK, asset_class="stock", status="open"),
        Trade(strategy_id=1, mode="paper", symbol=SYMBOL, asset_class="crypto", status="open"),
    ]

    async def slow_crypto(self, symbols):
        await asyncio.sleep(0.2)
        return {SYMBOL: _snap()}

    with (
        patch.object(AlpacaClient, "stock_snapshots", new=AsyncMock(return_value={STOCK: _snap(10.0)})),
        patch.object(AlpacaClient, "crypto_snapshots", slow_crypto),
    ):
        _quotes, observed = await engine._quotes_for(_client(), trades)

    assert observed[STOCK] is not None and observed[SYMBOL] is not None
    assert observed[SYMBOL] > observed[STOCK], (
        "both legs of the exit quote fetch recorded the same instant, so one of them "
        "is reporting a look it never took at that time"
    )


# --------------------------------------------------------------------------
# The schema itself — a column that only exists on fresh databases is a bug.
# --------------------------------------------------------------------------


def test_the_eval_columns_exist_and_are_nullable():
    """The owner's database has months of history and is brought forward by the
    migration chain, not by create_all. Nullable with no default is what lets an
    old row say "we do not know" instead of inventing a time."""
    cols = {c["name"]: c for c in inspect(db_engine).get_columns("trades")}
    for name in ("entry_eval_at", "entry_eval_price", "exit_eval_at", "exit_eval_price"):
        assert name in cols, f"{name} never reached the trades table"
        assert cols[name]["nullable"], f"{name} cannot hold 'unknown', so old rows must lie"


def test_a_row_written_without_them_reads_as_unknown():
    """No default sneaks in behind the migration: a row nobody stamped must come
    back NULL, not 0.0 and not created_at."""
    with session_scope() as session:
        strategy = Strategy(
            name="wire legacy row", enabled=False, asset_class="stock", universe="scanner",
            preset="custom", params='{"entry":{},"exit":{}}',
        )
        session.add(strategy)
        session.flush()
        trade = Trade(
            strategy_id=strategy.id, mode="evalwire", symbol=STOCK, asset_class="stock",
            qty=0, notional=0, status="rejected", entry_reason="legacy row",
        )
        session.add(trade)
        session.flush()
        tid, sid = trade.id, strategy.id
    with session_scope() as session:
        row = session.get(Trade, tid)
        assert row.entry_eval_at is None
        assert row.entry_eval_price is None
        assert row.exit_eval_at is None
        assert row.exit_eval_price is None
        session.delete(row)
        session.query(Strategy).filter(Strategy.id == sid).delete()
