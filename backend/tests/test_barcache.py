from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from qt.services import barcache
from qt.services.barcache import DayQuote


def _mem_session():
    # In-memory SQLite with one shared connection so the schema persists.
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    barcache.CacheBase.metadata.create_all(eng)
    return sessionmaker(bind=eng, expire_on_commit=False)()


def _q(symbol, close, prev, vol, vw=None):
    return DayQuote(symbol=symbol, close=close, prev_close=prev, volume=vol, vwap=vw)


def test_rank_movers_ranks_by_gain_and_caps_top_n():
    quotes = [
        _q("AAA", 12.0, 10.0, 1_000_000),   # +20%
        _q("BBB", 11.0, 10.0, 1_000_000),   # +10%
        _q("CCC", 15.0, 10.0, 1_000_000),   # +50%
        _q("DDD", 9.0, 10.0, 1_000_000),    # -10% (below default min 0)
    ]
    ranked = barcache.rank_movers(quotes, top_n=2, min_change_pct=1)
    assert [r[0] for r in ranked] == ["CCC", "AAA"]  # highest gain first, DDD & BBB dropped
    assert ranked[0][1] == 50.0


def test_rank_movers_applies_price_and_volume_and_max_gain():
    quotes = [
        _q("CHEAP", 6.0, 5.0, 2_000_000),     # +20%, $12M vol, price $6
        _q("PRICEY", 60.0, 50.0, 2_000_000),  # +20%, price $60
        _q("THIN", 6.0, 5.0, 1_000),          # +20%, tiny $ volume
        _q("BLOWOFF", 30.0, 5.0, 2_000_000),  # +500%, too extended
    ]
    ranked = barcache.rank_movers(
        quotes, top_n=10, min_change_pct=1, max_change_pct=100,
        min_price=1, max_price=10, min_dollar_volume=1_000_000,
    )
    assert [r[0] for r in ranked] == ["CHEAP"]  # only the sub-$10, liquid, not-too-extended mover


def test_save_daily_bars_is_idempotent_and_readable():
    s = _mem_session()
    bars = [
        {"t": "2026-06-01T00:00:00Z", "o": 9, "h": 12, "l": 8, "c": 11, "v": 1_000_000, "vw": 10.5},
        {"t": "2026-06-02T00:00:00Z", "o": 11, "h": 13, "l": 10, "c": 12, "v": 2_000_000, "vw": 11.5},
    ]
    assert barcache.save_daily_bars(s, "AAA", bars) == 2
    barcache.save_daily_bars(s, "AAA", bars)  # again — must not duplicate
    s.commit()
    assert s.query(barcache.DailyBar).count() == 2


def test_store_and_read_movers_roundtrip():
    s = _mem_session()
    ranked = [("CCC", 50.0, 15.0, 30_000_000.0), ("AAA", 20.0, 12.0, 12_000_000.0)]
    barcache.store_movers(s, "2026-06-01", ranked)
    s.commit()
    got = barcache.top_movers(s, "2026-06-01")
    assert [m.symbol for m in got] == ["CCC", "AAA"]  # ordered by rank
    assert got[0].rank == 1 and got[0].change_pct == 50.0
    # replace is clean, not append
    barcache.store_movers(s, "2026-06-01", [("ZZZ", 5.0, 3.0, 1_000_000.0)])
    s.commit()
    assert [m.symbol for m in barcache.top_movers(s, "2026-06-01")] == ["ZZZ"]
