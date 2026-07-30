"""Read-through bar cache for the backtest / optimizer fetch paths.

The three properties that actually matter, pinned here: a warm cache doesn't
call Alpaca, an IN-PROGRESS bar is never persisted (it would poison every later
run with a moving close), and a broken cache degrades to a plain fetch instead
of failing the backtest.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from qt.services import barcache, barfetch


class FakeClient:
    """Records every Alpaca call and returns whatever it was primed with."""

    def __init__(self, payloads=None):
        self.payloads = list(payloads or [])
        self.calls: list[tuple] = []

    async def historical_bars(self, symbols, asset_class, timeframe, start_iso, end_iso=None):
        self.calls.append((tuple(symbols), asset_class, timeframe, start_iso))
        if self.payloads:
            return self.payloads.pop(0)
        return {s: [] for s in symbols}


@pytest.fixture()
def cache(monkeypatch):
    """A fresh in-memory bar cache wired into the module globals."""
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    barcache.CacheBase.metadata.create_all(eng)
    Sess = sessionmaker(bind=eng, expire_on_commit=False)
    monkeypatch.setattr(barcache, "_engine", eng)
    monkeypatch.setattr(barcache, "_Session", Sess)
    return Sess


def _day(offset: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=offset)).strftime("%Y-%m-%d")


def _daily(day: str, close: float, stamp: str = "T05:00:00Z") -> dict:
    return {"t": f"{day}{stamp}", "o": close, "h": close, "l": close, "c": close, "v": 1e6, "vw": close}


def _seed_daily(Sess, symbol: str, days: list[str], model=barcache.DailyBar) -> None:
    with Sess() as s:
        barcache.save_daily_bars(s, symbol, [_daily(d, 100.0) for d in days], model=model)
        s.commit()


async def _fetch(client, symbols, asset_class, timeframe, start_iso):
    return await barfetch.fetch_bars(client, symbols, asset_class, timeframe, start_iso)


@pytest.mark.asyncio
async def test_warm_cache_serves_without_calling_alpaca(cache):
    # Every closed day is cached, so the only bar missing is today's still-open
    # one — which we must not fetch OR cache. No Alpaca call at all.
    days = [_day(-i) for i in range(10, 0, -1)]  # D-10 .. D-1, contiguous
    _seed_daily(cache, "NVDA", days)
    client = FakeClient()

    out = await _fetch(client, ["NVDA"], "stock", "1Day", f"{days[0]}T00:00:00Z")

    assert client.calls == [], "a fully cached window still hit Alpaca"
    assert [b["t"][:10] for b in out["NVDA"]] == days


@pytest.mark.asyncio
async def test_cold_cache_fetches_the_window_and_saves_it(cache):
    days = [_day(-i) for i in range(5, 0, -1)]
    client = FakeClient([{"NVDA": [_daily(d, 100.0) for d in days]}])

    out = await _fetch(client, ["NVDA"], "stock", "1Day", f"{days[0]}T00:00:00Z")

    assert len(client.calls) == 1
    assert client.calls[0][3] == f"{days[0]}T00:00:00Z"  # the whole window
    assert len(out["NVDA"]) == 5
    # …and it landed in the cache, so the next run is free.
    with cache() as s:
        assert {b.day for b in s.query(barcache.DailyBar).all()} == set(days)


@pytest.mark.asyncio
async def test_only_the_missing_edge_is_fetched(cache):
    # Cache holds D-10..D-4; the fetch must start at D-3, not at the window start.
    cached_days = [_day(-i) for i in range(10, 3, -1)]
    edge_days = [_day(-i) for i in range(3, 0, -1)]
    _seed_daily(cache, "NVDA", cached_days)
    client = FakeClient([{"NVDA": [_daily(d, 200.0) for d in edge_days]}])

    out = await _fetch(client, ["NVDA"], "stock", "1Day", f"{cached_days[0]}T00:00:00Z")

    assert len(client.calls) == 1
    assert client.calls[0][3] == f"{_day(-3)}T00:00:00Z"
    # The merged series is complete and chronological — no hole, no duplicates.
    assert [b["t"][:10] for b in out["NVDA"]] == cached_days + edge_days


@pytest.mark.asyncio
async def test_an_in_progress_daily_bar_is_never_cached(cache):
    # Today's daily bar is still being written: its close moves until the session
    # ends. The cache is insert-or-ignore, so persisting it once would serve the
    # wrong close forever. It must still be RETURNED to the caller, just not saved.
    days = [_day(-2), _day(-1), _day(0)]
    client = FakeClient([{"NVDA": [_daily(d, 100.0) for d in days]}])

    out = await _fetch(client, ["NVDA"], "stock", "1Day", f"{days[0]}T00:00:00Z")

    assert [b["t"][:10] for b in out["NVDA"]] == days  # caller still sees today
    with cache() as s:
        assert sorted(b.day for b in s.query(barcache.DailyBar).all()) == [_day(-2), _day(-1)]


@pytest.mark.asyncio
async def test_an_in_progress_intraday_slot_is_never_cached(cache):
    # Same rule one resolution down: a 15-minute bar is stamped at the START of
    # its slot, so the slot currently in progress is unfinished. The previous slot
    # closed exactly as this one opened — that one is safe to keep.
    now = datetime.now(timezone.utc)
    slot = now.replace(minute=(now.minute // 15) * 15, second=0, microsecond=0)
    closed = slot - timedelta(minutes=15)
    bars = [
        {"t": t.strftime("%Y-%m-%dT%H:%M:%SZ"), "o": 1, "h": 1, "l": 1, "c": 1, "v": 10, "vw": 1}
        for t in (closed - timedelta(minutes=15), closed, slot)
    ]
    client = FakeClient([{"NVDA": bars}])

    out = await _fetch(client, ["NVDA"], "stock", "15Min", bars[0]["t"])

    assert len(out["NVDA"]) == 3  # the caller still gets the live slot
    with cache() as s:
        saved = sorted(b.ts for b in s.query(barcache.IntradayBar).all())
    assert saved == [bars[0]["t"], bars[1]["t"]]
    assert slot.strftime("%Y-%m-%dT%H:%M:%SZ") not in saved


@pytest.mark.asyncio
async def test_a_hole_in_the_cached_window_forces_a_full_refetch(cache):
    # D-30..D-25 then D-5..D-1: the middle is missing and we can't tell what else
    # is. Correctness beats thrift — re-fetch that symbol's whole window rather
    # than serve a series with a month quietly absent.
    days = [_day(-i) for i in range(30, 24, -1)] + [_day(-i) for i in range(5, 0, -1)]
    _seed_daily(cache, "NVDA", days)
    client = FakeClient([{"NVDA": [_daily(d, 100.0) for d in days]}])

    await _fetch(client, ["NVDA"], "stock", "1Day", f"{days[0]}T00:00:00Z")

    assert len(client.calls) == 1
    assert client.calls[0][3] == f"{days[0]}T00:00:00Z"  # the whole window, not the edge


@pytest.mark.asyncio
async def test_a_broken_cache_degrades_to_a_plain_fetch(cache, monkeypatch):
    # The cache is an optimization, never a dependency: unreachable, unconfigured
    # or corrupt, the backtest still runs on a plain Alpaca fetch.
    def boom(*_a, **_kw):
        raise RuntimeError("cache DB is on fire")

    monkeypatch.setattr(barcache, "init_cache", boom)
    days = [_day(-3), _day(-2), _day(-1)]
    client = FakeClient([{"NVDA": [_daily(d, 100.0) for d in days]}])

    out = await _fetch(client, ["NVDA"], "stock", "1Day", f"{days[0]}T00:00:00Z")

    assert len(out["NVDA"]) == 3
    assert client.calls[0][3] == f"{days[0]}T00:00:00Z"


@pytest.mark.asyncio
async def test_a_failing_save_still_returns_the_bars(cache, monkeypatch):
    # A write failure must not lose a fetch we already paid for.
    def boom(*_a, **_kw):
        raise RuntimeError("disk full")

    monkeypatch.setattr(barfetch, "_save", boom)
    days = [_day(-3), _day(-2), _day(-1)]
    client = FakeClient([{"NVDA": [_daily(d, 100.0) for d in days]}])

    out = await _fetch(client, ["NVDA"], "stock", "1Day", f"{days[0]}T00:00:00Z")
    assert len(out["NVDA"]) == 3


@pytest.mark.asyncio
async def test_crypto_uses_the_crypto_tables(cache):
    # Stocks and crypto mean different things by "a day", so they live in separate
    # tables — a crypto fetch must never land in the stock cache.
    days = [_day(-3), _day(-2), _day(-1)]
    client = FakeClient([{"BTC/USD": [_daily(d, 100.0, stamp="T00:00:00Z") for d in days]}])

    await _fetch(client, ["BTC/USD"], "crypto", "1Day", f"{days[0]}T00:00:00Z")

    with cache() as s:
        assert s.query(barcache.DailyBar).count() == 0
        assert sorted(b.day for b in s.query(barcache.CryptoDailyBar).all()) == days


@pytest.mark.asyncio
async def test_an_uncached_timeframe_passes_straight_through(cache):
    # 1Hour isn't cached (no table for it) — it must still work, untouched.
    client = FakeClient([{"NVDA": [_daily(_day(-1), 100.0)]}])
    out = await _fetch(client, ["NVDA"], "stock", "1Hour", f"{_day(-5)}T00:00:00Z")
    assert len(client.calls) == 1
    assert len(out["NVDA"]) == 1
    with cache() as s:
        assert s.query(barcache.DailyBar).count() == 0
