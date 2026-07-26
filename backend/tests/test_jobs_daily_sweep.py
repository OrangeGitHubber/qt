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
