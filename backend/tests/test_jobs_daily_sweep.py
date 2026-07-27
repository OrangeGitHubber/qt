"""The forward daily movers job MAINTAINS an existing cache but never
bootstraps one, so users who don't use scanner replay pay no daily Alpaca
cost. These tests pin that gate without touching the network."""

import asyncio

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from qt.services import barcache, barsweep, calendar
from qt.services import jobs


def _mem_cache(monkeypatch):
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    barcache.CacheBase.metadata.create_all(eng)
    Sess = sessionmaker(bind=eng, expire_on_commit=False)
    monkeypatch.setattr(barcache, "_engine", eng)
    monkeypatch.setattr(barcache, "_Session", Sess)
    return Sess


def _wire(monkeypatch, *, trading=True):
    # A non-None "client" so the job proceeds; calendar says it's a trading day.
    monkeypatch.setattr(jobs, "get_client", lambda session: object())

    async def _is_trading(client, today=None):
        return trading

    monkeypatch.setattr(calendar, "is_trading_today", _is_trading)


def test_daily_sweep_is_a_noop_on_an_empty_cache(monkeypatch):
    _mem_cache(monkeypatch)
    _wire(monkeypatch)

    async def _boom(*a, **k):  # must NOT be called when the cache is empty
        raise AssertionError("daily_movers_update ran against an un-bootstrapped cache")

    monkeypatch.setattr(barsweep, "daily_movers_update", _boom)
    asyncio.run(jobs.daily_movers_sweep())  # returns cleanly, no update attempted


def test_daily_sweep_updates_a_populated_cache(monkeypatch):
    Sess = _mem_cache(monkeypatch)
    _wire(monkeypatch)
    with Sess() as s:  # seed one bar so the cache counts as "already built"
        barcache.save_daily_bars(s, "AAA", [
            {"t": "2026-06-01T14:00:00Z", "o": 10, "h": 10, "l": 10, "c": 10, "v": 1e6, "vw": 10},
        ])
        s.commit()

    called = {}

    async def _spy(client, sess, **kwargs):
        called.update(kwargs)
        called["ran"] = True
        return {"symbols_saved": 0, "days_reconstructed": 0}

    monkeypatch.setattr(barsweep, "daily_movers_update", _spy)
    asyncio.run(jobs.daily_movers_sweep())
    assert called.get("ran") is True
    # Uses the live stock scanner's floors, and stores the generous wide set.
    assert called["min_change_pct"] == 2.0 and called["min_price"] == 1.0


def test_daily_sweep_does_not_touch_crypto_without_a_crypto_cache(monkeypatch):
    """A stock-only cache maintains stocks but never bootstraps a crypto cache."""
    Sess = _mem_cache(monkeypatch)
    _wire(monkeypatch)
    with Sess() as s:
        barcache.save_daily_bars(s, "AAA", [
            {"t": "2026-06-01T14:00:00Z", "o": 10, "h": 10, "l": 10, "c": 10, "v": 1e6, "vw": 10},
        ])
        s.commit()

    async def _stock_noop(client, sess, **kwargs):
        return {"symbols_saved": 0, "days_reconstructed": 0}

    async def _crypto_boom(*a, **k):
        raise AssertionError("crypto update ran against an un-bootstrapped crypto cache")

    monkeypatch.setattr(barsweep, "daily_movers_update", _stock_noop)
    monkeypatch.setattr(barsweep, "crypto_daily_movers_update", _crypto_boom)
    asyncio.run(jobs.daily_movers_sweep())  # returns cleanly, no crypto update


def test_crypto_sweep_is_a_noop_on_an_empty_crypto_cache(monkeypatch):
    """The crypto job maintains but never bootstraps — nothing runs on an empty
    crypto cache (even if a STOCK cache exists)."""
    Sess = _mem_cache(monkeypatch)
    _wire(monkeypatch)
    with Sess() as s:  # a stock bar, but no crypto bar
        barcache.save_daily_bars(s, "AAA", [
            {"t": "2026-06-01T14:00:00Z", "o": 10, "h": 10, "l": 10, "c": 10, "v": 1e6, "vw": 10},
        ])
        s.commit()

    async def _crypto_boom(*a, **k):
        raise AssertionError("crypto update ran against an un-bootstrapped crypto cache")

    monkeypatch.setattr(barsweep, "crypto_daily_movers_update", _crypto_boom)
    asyncio.run(jobs.crypto_movers_sweep())  # returns cleanly, no update attempted


def test_crypto_sweep_maintains_a_populated_crypto_cache_every_calendar_day(monkeypatch):
    """When a crypto cache exists, the dedicated crypto job maintains it with the
    crypto scanner floors — and runs even when the US market is CLOSED (crypto is
    24/7, so it's not gated to trading days)."""
    Sess = _mem_cache(monkeypatch)
    _wire(monkeypatch, trading=False)  # US market closed today — crypto must still run
    with Sess() as s:  # only a crypto bar — the stock side is irrelevant here
        barcache.save_daily_bars(s, "BTC/USD", [
            {"t": "2026-06-01T00:00:00Z", "o": 100, "h": 100, "l": 100, "c": 100, "v": 1e6, "vw": 100},
        ], model=barcache.CryptoDailyBar)
        s.commit()

    called = {}

    async def _crypto_spy(client, sess, **kwargs):
        called.update(kwargs)
        called["ran"] = True
        return {"symbols_saved": 0, "days_reconstructed": 0}

    monkeypatch.setattr(barsweep, "crypto_daily_movers_update", _crypto_spy)
    asyncio.run(jobs.crypto_movers_sweep())
    assert called.get("ran") is True
    # Uses the crypto scanner's floors (1% change, $1M volume, no $1 price floor).
    assert called["min_change_pct"] == 1.0 and called["min_price"] == 0.0
    assert called["min_dollar_volume"] == 25_000
