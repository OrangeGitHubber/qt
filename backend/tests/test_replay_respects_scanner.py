"""Scanner-replay must obey the scanner's own settings.

A mover list is reconstructed once and cached. The filters were applied only at
that moment, so a symbol added to "never trade these" afterwards — or a price
floor raised — kept being replayed: the backtest traded names the live engine is
configured never to touch, and reported a result for a strategy you could not
actually run.

Werner's case exactly: TRUMP/USD on the exclude list and SHIB/USD at $0.000009
under a $0.05 floor, both still showing up as open positions in a compare run.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from qt.services import barcache


@pytest.fixture()
def cache(monkeypatch):
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    barcache.CacheBase.metadata.create_all(eng)
    Sess = sessionmaker(bind=eng, expire_on_commit=False)
    monkeypatch.setattr(barcache, "_engine", eng)
    monkeypatch.setattr(barcache, "_Session", Sess)
    with Sess() as s:
        # Stored as Alpaca's bars endpoint returns crypto: slash-less.
        barcache.store_movers(s, "2026-07-30", [
            ("TRUMPUSD", 12.0, 1.44, 500_000.0),   # banned by name
            ("SHIBUSD", 9.0, 0.000009, 900_000.0),  # under a $0.05 floor
            ("DOTUSD", 6.0, 0.78, 400_000.0),       # fine
            ("THINUSD", 5.0, 2.00, 50.0),           # under a volume floor
        ], model=barcache.CryptoDailyMover)
        s.commit()
    return Sess


def _read(Sess, **kw):
    with Sess() as s:
        return barcache.movers_between(s, "2026-07-01", model=barcache.CryptoDailyMover, **kw)


def test_without_filters_everything_stored_comes_back(cache):
    """The premise: all four are in the cache, so the assertions below are about
    filtering and not about an empty fixture."""
    assert len(_read(cache)["2026-07-30"]) == 4


def test_an_excluded_symbol_is_not_replayed(cache):
    """THE reported bug."""
    got = _read(cache, exclude={"TRUMP/USD"})["2026-07-30"]
    assert "TRUMPUSD" not in got
    assert "DOTUSD" in got


def test_the_exclusion_matches_across_the_two_spellings(cache):
    """The scanner config holds 'TRUMP/USD'; the cache holds 'TRUMPUSD'. A
    literal comparison silently excludes nothing, which is how this hid."""
    assert "TRUMPUSD" not in _read(cache, exclude={"TRUMP/USD"})["2026-07-30"]
    assert "TRUMPUSD" not in _read(cache, exclude={"trumpusd"})["2026-07-30"]


def test_a_price_floor_raised_after_the_sweep_still_applies(cache):
    """Werner's SHIB row: reconstructed when the floor was 0, replayed after he
    set it to $0.05. Re-checked at read time, so no re-sweep is needed."""
    got = _read(cache, min_price=0.05)["2026-07-30"]
    assert "SHIBUSD" not in got and "DOTUSD" in got


def test_a_volume_floor_raised_after_the_sweep_still_applies(cache):
    assert "THINUSD" not in _read(cache, min_dollar_volume=1000)["2026-07-30"]


def test_filters_apply_before_the_top_n_cut(cache):
    """Order matters: filtering after the cut would let a banned name consume one
    of the N slots and silently shrink the day's universe."""
    got = _read(cache, exclude={"TRUMP/USD"}, top_n=2)["2026-07-30"]
    assert got == ["SHIBUSD", "DOTUSD"], got


def test_a_sweep_stops_storing_banned_names(cache):
    """Belt and braces — read-time filtering is what fixes an existing cache,
    this stops a new one filling up with names you've banned."""
    quotes = [
        barcache.DayQuote(symbol="TRUMPUSD", close=1.44, prev_close=1.2, high=1.5, volume=1e6, vwap=1.44),
        barcache.DayQuote(symbol="DOTUSD", close=0.78, prev_close=0.7, high=0.8, volume=1e6, vwap=0.78),
    ]
    ranked = barcache.rank_movers(quotes, top_n=10, exclude={"TRUMP/USD"})
    assert [r[0] for r in ranked] == ["DOTUSD"]
