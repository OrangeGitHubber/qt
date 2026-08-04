"""The movers sweep must store what YOUR scanner would accept, not the defaults.

MEASURED. "Crypto - many movements" could not be compared at all: every stretch
after 2026-08-02 came back "the replay of the stretch this falls in did not run
— No cached crypto movers yet". The cache was not empty (1,125 rows over 315
days) and the read-side filters were innocent — filtered and unfiltered lookups
returned the identical 315 days. What was missing was the days themselves:
one or two movers on recent days, and 2026-08-03 absent entirely.

The sweep writes with `scanner.CRYPTO_DEFAULTS` — min_change_pct 1.0,
min_dollar_volume 25,000 — while the owner's configured scanner runs at 0.0 and
3,000. So the cache was built for a scanner nobody was running: on a quiet day
no coin cleared 1%, nothing was written, and a fidelity comparison of a scanner
strategy over that day has no universe to reconstruct and refuses the stretch.

WHY WRITE TIME IS THE PLACE THIS MATTERS. `load_scanner_replay_dataset` re-applies
the live config on the way OUT, deliberately — "Read them now rather than trusting
whatever was in force when the sweep ran". That is only sound if the sweep stored
a superset of what the scanner accepts. Filtering harder on the way IN cannot be
undone by any amount of re-reading: the rows were never written.

Both twins had it, so both are pinned here.
"""

from unittest.mock import AsyncMock, patch

import pytest

from qt import security
from qt.broker.alpaca import SECRET_KEY_ID, SECRET_KEY_SECRET
from qt.db import session_scope
from qt.services import barcache, barsweep, jobs, scanner
from qt.settings_service import set_setting

# Deliberately looser than CRYPTO_DEFAULTS on every axis that can silently drop a
# day: a coin that moved 0.0% on $3,000 of volume is one this owner's scanner
# really does accept, and really did trade.
LOOSE_CRYPTO = {
    "enabled": True, "min_price": 0.05, "max_price": 0.0,
    "min_change_pct": 0.0, "min_dollar_volume": 3000.0,
}
LOOSE_STOCKS = {
    "enabled": True, "min_price": 1.0, "max_price": 0.0,
    "min_change_pct": 0.5, "min_dollar_volume": 50_000.0,
}


@pytest.fixture()
def configured(client):
    with session_scope() as s:
        security.set_secret(s, SECRET_KEY_ID, "k")
        security.set_secret(s, SECRET_KEY_SECRET, "s")
        set_setting(s, scanner.CONFIG_KEY, {
            "top_n": 10, "exclude_symbols": ["TRUMP/USD"],
            "crypto": dict(LOOSE_CRYPTO), "stocks": dict(LOOSE_STOCKS),
        })
    yield
    with session_scope() as s:
        set_setting(s, scanner.CONFIG_KEY, {})
        security.delete_secret(s, SECRET_KEY_ID)
        security.delete_secret(s, SECRET_KEY_SECRET)


def _thresholds(update_name: str, sweep, has_rows: bool) -> dict:
    """Run one sweep job against a stubbed updater and return the floors it was
    asked to store with."""
    seen: dict = {}

    async def spy(client, sess, **kwargs):
        seen.update(kwargs)
        return {}

    class _Row:
        pass

    class _Query:
        def first(self):
            return _Row() if has_rows else None

    class _Sess:
        def query(self, *a, **k):
            return _Query()

        def close(self):
            pass

    with patch.object(barsweep, update_name, new=spy), \
            patch.object(barcache, "init_cache", new=lambda *a, **k: None), \
            patch.object(barcache, "session", new=lambda *a, **k: _Sess()), \
            patch.object(barcache, "has_intraday", new=lambda *a, **k: False):
        import asyncio

        asyncio.run(sweep())
    return seen


def test_the_crypto_sweep_stores_with_the_configured_floors(client, configured):
    """The measured case. With the defaults in force the owner's quiet days were
    never written, and no re-read could recover them."""
    got = _thresholds("crypto_daily_movers_update", jobs.crypto_movers_sweep, has_rows=True)

    assert got, "the sweep never reached the updater"
    assert got["min_change_pct"] == LOOSE_CRYPTO["min_change_pct"], got
    assert got["min_dollar_volume"] == LOOSE_CRYPTO["min_dollar_volume"], got
    assert got["min_price"] == LOOSE_CRYPTO["min_price"], got
    # …and specifically NOT the defaults, or this passes on a config that happens
    # to equal them.
    assert got["min_change_pct"] != scanner.CRYPTO_DEFAULTS["min_change_pct"]
    assert got["min_dollar_volume"] != scanner.CRYPTO_DEFAULTS["min_dollar_volume"]


def test_the_stock_sweep_stores_with_the_configured_floors(client, configured):
    """Same fault, same file, four lines apart. Fixing one of a pair in this
    codebase has been the shape of the last several bugs."""
    with patch.object(jobs, "_should_sweep_today", new=lambda *a, **k: True, create=True):
        got = _thresholds("daily_movers_update", jobs.daily_movers_sweep, has_rows=True)

    assert got, "the sweep never reached the updater"
    assert got["min_change_pct"] == LOOSE_STOCKS["min_change_pct"], got
    assert got["min_dollar_volume"] == LOOSE_STOCKS["min_dollar_volume"], got
    assert got["min_change_pct"] != scanner.STOCK_DEFAULTS["min_change_pct"]
