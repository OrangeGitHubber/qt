"""Caching 1-minute bars.

The fidelity comparison replays on minute bars by design, and an eleven-symbol
day is ~16,000 of them. They were excluded from the cache because the intraday
table's key is (symbol, timestamp) with no record of the bar's SIZE — so a
1-minute and a 15-minute bar stamped on the same quarter-hour collide, the write
is insert-or-ignore, and the survivor is served back inside whichever series asks
next carrying the wrong high and low.

Minute bars have their own tables, which is what makes this safe. The tests that
matter are therefore about SEPARATION and about not caching a bar that is still
moving — not about the round trip.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from qt.services import barcache, barfetch

from test_barfetch import FakeClient


@pytest.fixture()
def cache(monkeypatch):
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    barcache.CacheBase.metadata.create_all(eng)
    Sess = sessionmaker(bind=eng, expire_on_commit=False)
    monkeypatch.setattr(barcache, "_engine", eng)
    monkeypatch.setattr(barcache, "_Session", Sess)
    return Sess


def _bar(ts: datetime, close: float, high: float | None = None) -> dict:
    return {"t": ts.strftime("%Y-%m-%dT%H:%M:%SZ"), "o": close,
            "h": high if high is not None else close, "l": close,
            "c": close, "v": 1000, "vw": close}


def _minutes(start: datetime, n: int, close: float = 100.0) -> list[dict]:
    return [_bar(start + timedelta(minutes=i), close + i) for i in range(n)]


def _count(Sess, model) -> int:
    with Sess() as s:
        return s.query(model).count()


# --- the reason it was unsafe, and why it now is not ------------------------

def test_minute_bars_are_stored_apart_from_fifteen_minute_bars(cache):
    """The whole safety argument. If they shared a table the 15-minute row would
    win or lose an insert-or-ignore race and be served back inside the other
    series with the wrong high and low."""
    base = datetime.now(timezone.utc).replace(second=0, microsecond=0) - timedelta(hours=3)
    quarter = base.replace(minute=0)

    # A 15-minute bar and a 1-minute bar stamped on the SAME instant, with
    # different highs — the collision the old comment described.
    barfetch._save({"AAA": [_bar(quarter, 100.0, high=999.0)]}, "stock", "15Min")
    barfetch._save({"AAA": [_bar(quarter, 100.0, high=101.0)]}, "stock", "1Min")

    assert _count(cache, barcache.IntradayBar) == 1
    assert _count(cache, barcache.MinuteBar) == 1

    with cache() as s:
        fifteen = s.query(barcache.IntradayBar).one()
        minute = s.query(barcache.MinuteBar).one()
    assert fifteen.h == 999.0, "the 15-minute bar kept its own high"
    assert minute.h == 101.0, "the minute bar kept its own high"


def test_crypto_minute_bars_go_to_the_crypto_minute_table(cache):
    base = datetime.now(timezone.utc).replace(second=0, microsecond=0) - timedelta(hours=3)
    barfetch._save({"BTC/USD": [_bar(base, 100.0)]}, "crypto", "1Min")
    assert _count(cache, barcache.CryptoMinuteBar) == 1
    assert _count(cache, barcache.MinuteBar) == 0
    assert _count(cache, barcache.CryptoIntradayBar) == 0


# --- never cache a bar that is still moving --------------------------------

def test_the_minute_in_progress_is_never_cached(cache):
    """A bar is stamped at the START of its slot, so the current minute's bar is
    still being written. Cached once, insert-or-ignore serves that partial row
    as gospel forever."""
    now = datetime(2026, 8, 4, 14, 30, 45, tzinfo=timezone.utc)
    bars = [_bar(now.replace(minute=29, second=0), 100.0),
            _bar(now.replace(minute=30, second=0), 101.0)]
    closed = barfetch._closed_intraday(bars, 1, now)
    assert [b["c"] for b in closed] == [100.0]


def test_a_fifteen_minute_slot_still_uses_its_own_size(cache):
    """Anti-vacuity: the slot size is threaded through, not hardcoded to 1."""
    now = datetime(2026, 8, 4, 14, 46, 10, tzinfo=timezone.utc)
    bars = [_bar(now.replace(minute=30, second=0), 100.0),
            _bar(now.replace(minute=45, second=0), 101.0)]
    assert [b["c"] for b in barfetch._closed_intraday(bars, 15, now)] == [100.0]
    # At minute resolution the 14:45 bar closed long ago and IS keepable.
    assert [b["c"] for b in barfetch._closed_intraday(bars, 1, now)] == [100.0, 101.0]


# --- the point of the exercise: stop re-downloading ------------------------

@pytest.mark.asyncio
async def test_a_warm_minute_cache_does_not_call_alpaca_again(cache):
    start = datetime.now(timezone.utc).replace(second=0, microsecond=0) - timedelta(hours=2)
    payload = {"AAA": _minutes(start, 30)}

    cold = FakeClient([payload])
    first = await barfetch.fetch_bars(
        cold, ["AAA"], "stock", "1Min", start.strftime("%Y-%m-%dT%H:%M:%SZ"))
    assert len(cold.calls) == 1
    assert len(first["AAA"]) == 30

    warm = FakeClient([])
    second = await barfetch.fetch_bars(
        warm, ["AAA"], "stock", "1Min", start.strftime("%Y-%m-%dT%H:%M:%SZ"))
    assert [b["t"] for b in second["AAA"]] == [b["t"] for b in first["AAA"]]
    # The tail may still be fetched (the newest minutes are near the open slot),
    # but the bulk must come from the cache rather than a whole-window re-pull.
    if warm.calls:
        refetch_from = warm.calls[0][3]
        assert refetch_from > start.strftime("%Y-%m-%dT%H:%M:%SZ"), \
            "a warm cache must not re-request the whole window"


@pytest.mark.asyncio
async def test_one_min_is_cacheable_at_all(cache):
    """Guards the CACHEABLE tuple itself: dropping "1Min" from it sends every
    fidelity replay straight back to Alpaca, silently and expensively."""
    assert "1Min" in barfetch.CACHEABLE
    start = datetime.now(timezone.utc).replace(second=0, microsecond=0) - timedelta(hours=2)
    client = FakeClient([{"AAA": _minutes(start, 10)}])
    await barfetch.fetch_bars(client, ["AAA"], "stock", "1Min",
                              start.strftime("%Y-%m-%dT%H:%M:%SZ"))
    assert _count(cache, barcache.MinuteBar) > 0, "nothing was persisted"


def test_the_timeframe_asks_for_the_right_slot_size():
    """`_save` and `_plan` both derive the slot from the timeframe, and both use
    it to decide whether a bar has closed. Getting it wrong at 1Min is the
    corrupting direction: `_plan` would judge the cache fresh while up to
    fourteen minutes were missing, and serve the gap as a complete series."""
    assert barfetch._slot_minutes("1Min") == 1
    assert barfetch._slot_minutes("15Min") == 15
