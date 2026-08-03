"""The forward daily movers job maintains an existing cache, and BUILDS one
when an enabled strategy replays the scanner and there is none. The gate is the
strategies, not the cache: a user with no scanner strategy still pays no daily
Alpaca cost, and one who has a scanner strategy never has to know a cache is a
thing. These tests pin that gate without touching the network."""

import asyncio

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from qt.db import session_scope
from qt.models import Strategy
from qt.services import barcache, barsweep, calendar
from qt.services import jobs


def _scanner_strategy(asset_class="stock", universe="scanner", enabled=True):
    """A strategy that (usually) wants a scanner-replay cache, cleaned up after."""
    import json

    with session_scope() as s:
        row = Strategy(
            name=f"sweep-gate {asset_class} {universe} {enabled}",
            asset_class=asset_class, universe=universe, enabled=enabled,
            preset="custom", params=json.dumps({"entry": {}, "exit": {}}),
            sizing_usd=100, sleeve_usd=1000, max_positions=1,
        )
        s.add(row)
        s.flush()
        return row.id


def _drop(sid):
    with session_scope() as s:
        s.query(Strategy).filter(Strategy.id == sid).delete()


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


def test_daily_sweep_builds_nothing_when_no_strategy_wants_it(monkeypatch):
    """No scanner strategy, no cache, no Alpaca cost — the whole reason the build
    is gated rather than unconditional."""
    _mem_cache(monkeypatch)
    _wire(monkeypatch)

    async def _boom(*a, **k):
        raise AssertionError("daily_movers_update ran against an un-bootstrapped cache")

    monkeypatch.setattr(barsweep, "daily_movers_update", _boom)
    built = _spy_bootstrap(monkeypatch)
    asyncio.run(jobs.daily_movers_sweep())
    assert built == []


def _spy_bootstrap(monkeypatch) -> list:
    """Record bootstrap calls instead of downloading a year of bars."""
    from qt.api import barcache as barcache_api

    built: list = []

    async def _fake(client, market, days=365):
        built.append((market, days))
        return True

    monkeypatch.setattr(barcache_api, "bootstrap", _fake)
    return built


def test_an_enabled_scanner_strategy_builds_the_cache_itself(monkeypatch):
    """Werner's ask: don't make me press a button once before a scanner strategy
    can be backtested. An empty cache plus a strategy that needs one is enough."""
    _mem_cache(monkeypatch)
    _wire(monkeypatch)
    built = _spy_bootstrap(monkeypatch)
    sid = _scanner_strategy()
    try:
        asyncio.run(jobs.daily_movers_sweep())
    finally:
        _drop(sid)
    assert built == [("stock", 365)]


def test_a_scanner_plus_watchlist_strategy_counts_too(monkeypatch):
    """"both" replays the scanner, so it needs the same cache "scanner" does."""
    _mem_cache(monkeypatch)
    _wire(monkeypatch)
    built = _spy_bootstrap(monkeypatch)
    sid = _scanner_strategy(universe="both")
    try:
        asyncio.run(jobs.daily_movers_sweep())
    finally:
        _drop(sid)
    assert built == [("stock", 365)]


def test_bars_left_behind_by_an_ordinary_backtest_do_not_count_as_a_cache(monkeypatch):
    """The state a real user is actually in, and the one that broke.

    An ordinary backtest caches the daily bars of the few symbols it tested
    (barfetch writes the same table the sweep does), so "are there daily bars?"
    answers yes long before a scanner replay can work — that needs the day-by-day
    MOVERS, which only a reconstruction produces. Gating the build on bars meant
    the cache was declared already-built, the maintenance pass ran instead, and
    the replay kept refusing with "no cached movers yet". Gate on the thing the
    replay actually reads."""
    Sess = _mem_cache(monkeypatch)
    _wire(monkeypatch)
    with Sess() as s:
        # Two symbols, as a backtest of them would leave behind. No movers.
        for sym in ("AAA", "BBB"):
            barcache.save_daily_bars(s, sym, [
                {"t": "2026-06-01T14:00:00Z", "o": 10, "h": 10, "l": 10, "c": 10, "v": 1e6, "vw": 10},
            ])
        s.commit()

    built = _spy_bootstrap(monkeypatch)
    sid = _scanner_strategy()
    try:
        asyncio.run(jobs.daily_movers_sweep())
    finally:
        _drop(sid)
    assert built == [("stock", 365)]


def test_the_crypto_job_builds_its_own_cache_too(monkeypatch):
    """The stock job's tests could all pass while crypto never built anything —
    they are separate functions over separate tables, and crypto is the side
    Werner is using."""
    Sess = _mem_cache(monkeypatch)
    _wire(monkeypatch)
    with Sess() as s:
        barcache.save_daily_bars(s, "BTC/USD", [
            {"t": "2026-06-01T00:00:00Z", "o": 10, "h": 10, "l": 10, "c": 10, "v": 1e6, "vw": 10},
        ], model=barcache.CryptoDailyBar)
        s.commit()

    built = _spy_bootstrap(monkeypatch)
    sid = _scanner_strategy(asset_class="crypto", universe="both")
    try:
        asyncio.run(jobs.crypto_movers_sweep())
    finally:
        _drop(sid)
    assert built == [("crypto", 365)]


def test_a_disabled_or_unrelated_strategy_does_not_build_it(monkeypatch):
    """Three ways to look like a reason to build without being one: paused, the
    wrong asset class, and a universe that never replays the scanner."""
    _mem_cache(monkeypatch)
    _wire(monkeypatch)
    built = _spy_bootstrap(monkeypatch)
    ids = [
        _scanner_strategy(enabled=False),
        _scanner_strategy(asset_class="crypto"),
        _scanner_strategy(universe="watchlist"),
    ]
    try:
        asyncio.run(jobs.daily_movers_sweep())
    finally:
        for sid in ids:
            _drop(sid)
    assert built == []


def test_the_build_is_not_blocked_by_a_closed_market(monkeypatch):
    """A year of history downloads just as well on a Sunday. Making the first
    build wait for Monday would be the same "come back later" as the button."""
    _mem_cache(monkeypatch)
    _wire(monkeypatch, trading=False)
    built = _spy_bootstrap(monkeypatch)
    sid = _scanner_strategy()
    try:
        asyncio.run(jobs.daily_movers_sweep())
    finally:
        _drop(sid)
    assert built == [("stock", 365)]


def test_daily_sweep_updates_a_populated_cache(monkeypatch):
    Sess = _mem_cache(monkeypatch)
    _wire(monkeypatch)
    # A cache counts as built when it has MOVERS — a bar on its own is what an
    # ordinary backtest leaves behind, not evidence of a reconstruction.
    with Sess() as s:
        barcache.save_daily_bars(s, "AAA", [
            {"t": "2026-06-01T14:00:00Z", "o": 10, "h": 10, "l": 10, "c": 10, "v": 1e6, "vw": 10},
        ])
        barcache.store_movers(s, "2026-06-01", [("AAA", 5.0, 10.0, 1e7)])
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
    with Sess() as s:  # only the crypto side — the stock tables are irrelevant here
        barcache.save_daily_bars(s, "BTC/USD", [
            {"t": "2026-06-01T00:00:00Z", "o": 100, "h": 100, "l": 100, "c": 100, "v": 1e6, "vw": 100},
        ], model=barcache.CryptoDailyBar)
        barcache.store_movers(s, "2026-06-01", [("BTC/USD", 5.0, 100.0, 1e7)],
                              model=barcache.CryptoDailyMover)
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
