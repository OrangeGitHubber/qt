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


def test_freshest_mover_reports_latest_rank1_and_intraday_flag():
    s = _mem_session()
    assert barcache.freshest_mover(s) is None
    barcache.store_movers(s, "2026-06-10", [("OLD", 5.0, 1.0, 1e6)])
    barcache.store_movers(s, "2026-06-12", [("TOP", 9.0, 2.0, 1e6), ("SECOND", 8.0, 2.0, 1e6)])
    s.commit()
    fm = barcache.freshest_mover(s)
    assert fm == {"symbol": "TOP", "day": "2026-06-12", "change_pct": 9.0, "has_intraday": False}
    barcache.save_intraday_bars(s, "TOP", [{"t": "2026-06-12T14:00:00Z", "o": 2, "h": 2, "l": 2, "c": 2, "v": 1, "vw": 2}])
    s.commit()
    assert barcache.freshest_mover(s)["has_intraday"] is True


def test_cache_stats_reports_persisted_totals():
    s = _mem_session()
    assert barcache.cache_stats(s) == {
        "daily_symbols": 0, "movers_days": 0, "intraday_bars": 0, "latest_day": None, "freshest_mover": None,
    }
    barcache.save_daily_bars(s, "AAA", [
        {"t": "2026-06-01T00:00:00Z", "o": 1, "h": 1, "l": 1, "c": 1, "v": 1, "vw": 1},
        {"t": "2026-06-02T00:00:00Z", "o": 1, "h": 1, "l": 1, "c": 1, "v": 1, "vw": 1},
    ])
    barcache.save_daily_bars(s, "BBB", [{"t": "2026-06-02T00:00:00Z", "o": 1, "h": 1, "l": 1, "c": 1, "v": 1, "vw": 1}])
    barcache.store_movers(s, "2026-06-02", [("AAA", 5.0, 1.0, 1e6)])
    barcache.save_intraday_bars(s, "AAA", [{"t": "2026-06-02T14:00:00Z", "o": 1, "h": 1, "l": 1, "c": 1, "v": 1, "vw": 1}])
    s.commit()
    stats = barcache.cache_stats(s)
    assert stats == {
        "daily_symbols": 2, "movers_days": 1, "intraday_bars": 1, "latest_day": "2026-06-02",
        "freshest_mover": {"symbol": "AAA", "day": "2026-06-02", "change_pct": 5.0, "has_intraday": True},
    }


def test_rank_movers_uses_intraday_peak_when_high_is_given():
    # SPIKE closed almost flat (+5%) but peaked +100% intraday; FADE closed
    # +50% with a +55% peak. An intraday scanner would rank SPIKE first — and
    # ranking on the close alone (the old behaviour) would wrongly drop it.
    quotes = [
        DayQuote(symbol="SPIKE", close=10.5, prev_close=10.0, volume=1_000_000, high=20.0),
        DayQuote(symbol="FADE", close=15.0, prev_close=10.0, volume=1_000_000, high=15.5),
    ]
    ranked = barcache.rank_movers(quotes, top_n=10, min_change_pct=1)
    assert [r[0] for r in ranked] == ["SPIKE", "FADE"]
    assert ranked[0][1] == 100.0  # change_pct is the PEAK gain
    assert ranked[0][2] == 10.5   # price is still the close


def test_intraday_bars_roundtrip_and_filter_by_start_day():
    s = _mem_session()
    barcache.save_intraday_bars(s, "AAA", [
        {"t": "2026-05-30T14:00:00Z", "o": 9, "h": 9.2, "l": 8.9, "c": 9.1, "v": 1e5, "vw": 9.0},  # before start
        {"t": "2026-06-01T14:00:00Z", "o": 10, "h": 10.5, "l": 9.9, "c": 10.4, "v": 2e5, "vw": 10.2},
        {"t": "2026-06-01T14:15:00Z", "o": 10.4, "h": 11, "l": 10.3, "c": 10.9, "v": 3e5, "vw": 10.7},
    ])
    assert not barcache.has_intraday(_mem_session())  # a fresh DB has none
    s.commit()
    assert barcache.has_intraday(s)
    got = barcache.cached_intraday_bars(s, ["AAA", "MISSING"], "2026-06-01")
    assert set(got) == {"AAA"}  # MISSING absent
    assert [b["t"] for b in got["AAA"]] == ["2026-06-01T14:00:00Z", "2026-06-01T14:15:00Z"]  # pre-start dropped, ordered
    barcache.save_intraday_bars(s, "AAA", [
        {"t": "2026-06-01T14:00:00Z", "o": 10, "h": 10.5, "l": 9.9, "c": 10.4, "v": 2e5, "vw": 10.2},
    ])
    s.commit()
    assert s.query(barcache.IntradayBar).count() == 3  # idempotent — no dupes


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


def test_movers_between_groups_by_day_and_honors_start():
    s = _mem_session()
    barcache.store_movers(s, "2026-05-30", [("OLD", 40.0, 5.0, 1e6)])          # before start
    barcache.store_movers(s, "2026-06-01", [("CCC", 50.0, 15.0, 3e7), ("AAA", 20.0, 12.0, 1e7)])
    barcache.store_movers(s, "2026-06-02", [("BBB", 30.0, 8.0, 2e7)])
    s.commit()
    got = barcache.movers_between(s, "2026-06-01")
    assert got == {"2026-06-01": ["CCC", "AAA"], "2026-06-02": ["BBB"]}  # ranked, OLD excluded


def test_movers_between_narrows_to_top_n_at_read_time():
    s = _mem_session()
    barcache.store_movers(s, "2026-06-01", [
        ("CCC", 50.0, 15.0, 3e7), ("AAA", 20.0, 12.0, 1e7), ("BBB", 10.0, 8.0, 2e7),
    ])
    s.commit()
    # Store-wide, narrow-at-read: the same cache serves any riser count with no re-sweep.
    assert barcache.movers_between(s, "2026-06-01", top_n=2) == {"2026-06-01": ["CCC", "AAA"]}
    assert barcache.movers_between(s, "2026-06-01", top_n=1) == {"2026-06-01": ["CCC"]}
    assert barcache.movers_between(s, "2026-06-01") == {"2026-06-01": ["CCC", "AAA", "BBB"]}  # all


def test_the_reads_can_be_bounded_at_both_ends():
    """A replay of a window that ENDED in the past must not be handed the days
    after it. Without a far edge every such read would drag in everything up to
    today — bars the period being replayed could not have seen, and, for the
    movers, entry candidates from days outside it entirely."""
    s = _mem_session()
    for day in ("2026-06-01", "2026-06-02", "2026-06-03"):
        barcache.store_movers(s, day, [("AAA", 20.0, 12.0, 1e7)])
        barcache.save_daily_bars(s, "AAA", [
            {"t": f"{day}T00:00:00Z", "o": 1, "h": 1, "l": 1, "c": 1, "v": 1e6, "vw": 1},
        ])
        # Two stamps a day, the later one well after midnight — the case an
        # inclusive end must still keep, and a naive `ts <= end_day` would drop.
        barcache.save_intraday_bars(s, "AAA", [
            {"t": f"{day}T14:00:00Z", "o": 1, "h": 1, "l": 1, "c": 1, "v": 1, "vw": 1},
            {"t": f"{day}T19:45:00Z", "o": 1, "h": 1, "l": 1, "c": 1, "v": 1, "vw": 1},
        ])
    s.commit()

    assert sorted(barcache.movers_between(s, "2026-06-01", end_day="2026-06-02")) == [
        "2026-06-01", "2026-06-02",
    ]
    assert [b["t"][:10] for b in
            barcache.cached_daily_bars(s, ["AAA"], "2026-06-01", end_day="2026-06-02")["AAA"]] == [
        "2026-06-01", "2026-06-02",
    ]
    assert [b["t"] for b in
            barcache.cached_intraday_bars(s, ["AAA"], "2026-06-01", end_day="2026-06-02")["AAA"]] == [
        "2026-06-01T14:00:00Z", "2026-06-01T19:45:00Z",
        "2026-06-02T14:00:00Z", "2026-06-02T19:45:00Z",
    ]

    # No end = every stored day, exactly as before.
    assert len(barcache.movers_between(s, "2026-06-01")) == 3
    assert len(barcache.cached_intraday_bars(s, ["AAA"], "2026-06-01")["AAA"]) == 6


def test_cached_daily_bars_shapes_like_alpaca_and_filters():
    s = _mem_session()
    barcache.save_daily_bars(s, "AAA", [
        {"t": "2026-05-30T00:00:00Z", "o": 9, "h": 12, "l": 8, "c": 11, "v": 1e6, "vw": 10.5},  # before start
        {"t": "2026-06-01T00:00:00Z", "o": 11, "h": 13, "l": 10, "c": 12, "v": 2e6, "vw": 11.5},
    ])
    barcache.save_daily_bars(s, "BBB", [
        {"t": "2026-06-01T00:00:00Z", "o": 5, "h": 6, "l": 4, "c": 5.5, "v": 3e6, "vw": 5.2},
    ])
    s.commit()
    got = barcache.cached_daily_bars(s, ["AAA", "BBB", "MISSING"], "2026-06-01")
    assert set(got) == {"AAA", "BBB"}  # MISSING absent, no empty key
    assert got["AAA"] == [
        {"t": "2026-06-01T14:00:00Z", "o": 11, "h": 13, "l": 10, "c": 12, "v": 2e6, "vw": 11.5}
    ]  # pre-start bar dropped; stamped 14:00Z so it stays on the same ET day
    assert barcache.cached_daily_bars(s, [], "2026-06-01") == {}


def test_crypto_tables_are_separate_and_stamp_inside_the_utc_day():
    s = _mem_session()
    # Crypto data goes to the crypto tables and never touches the stock tables.
    barcache.save_daily_bars(s, "BTC/USD", [
        {"t": "2026-06-01T00:00:00Z", "o": 100, "h": 130, "l": 95, "c": 120, "v": 1e6, "vw": 115},
    ], model=barcache.CryptoDailyBar)
    barcache.store_movers(s, "2026-06-01", [("BTC/USD", 20.0, 120.0, 1.2e8)], model=barcache.CryptoDailyMover)
    barcache.save_intraday_bars(s, "BTC/USD", [
        {"t": "2026-06-01T12:00:00Z", "o": 100, "h": 130, "l": 95, "c": 120, "v": 1e5, "vw": 115},
    ], model=barcache.CryptoIntradayBar)
    s.commit()
    assert s.query(barcache.DailyBar).count() == 0
    assert s.query(barcache.DailyMover).count() == 0
    assert s.query(barcache.IntradayBar).count() == 0

    # Daily bars are stamped 12:00Z — inside the UTC calendar day the crypto
    # backtest buckets by (midnight UTC would be ambiguous at the boundary).
    got = barcache.cached_daily_bars(s, ["BTC/USD"], "2026-06-01",
                                     model=barcache.CryptoDailyBar, stamp="T12:00:00Z")
    assert got["BTC/USD"][0]["t"] == "2026-06-01T12:00:00Z"

    # movers_between / cached_intraday_bars / has_intraday all target the crypto tables.
    assert barcache.movers_between(s, "2026-06-01", model=barcache.CryptoDailyMover) == {"2026-06-01": ["BTC/USD"]}
    assert barcache.has_intraday(s, model=barcache.CryptoIntradayBar) is True
    assert barcache.has_intraday(s) is False  # stock intraday still empty


def test_crypto_cache_stats_reads_the_crypto_tables():
    s = _mem_session()
    assert barcache.crypto_cache_stats(s) == {
        "daily_symbols": 0, "movers_days": 0, "intraday_bars": 0, "latest_day": None, "freshest_mover": None,
    }
    barcache.save_daily_bars(s, "BTC/USD", [
        {"t": "2026-06-01T00:00:00Z", "o": 1, "h": 1, "l": 1, "c": 1, "v": 1, "vw": 1},
        {"t": "2026-06-02T00:00:00Z", "o": 1, "h": 1, "l": 1, "c": 1, "v": 1, "vw": 1},
    ], model=barcache.CryptoDailyBar)
    barcache.store_movers(s, "2026-06-02", [("BTC/USD", 5.0, 1.0, 1e6)], model=barcache.CryptoDailyMover)
    barcache.save_intraday_bars(s, "BTC/USD", [
        {"t": "2026-06-02T12:00:00Z", "o": 1, "h": 1, "l": 1, "c": 1, "v": 1, "vw": 1},
    ], model=barcache.CryptoIntradayBar)
    s.commit()
    assert barcache.crypto_cache_stats(s) == {
        "daily_symbols": 1, "movers_days": 1, "intraday_bars": 1, "latest_day": "2026-06-02",
        "freshest_mover": {"symbol": "BTC/USD", "day": "2026-06-02", "change_pct": 5.0, "has_intraday": True},
    }
    # And the stock stats remain empty — the two caches are independent.
    assert barcache.cache_stats(s)["daily_symbols"] == 0


def test_pruning_keeps_everything_a_backtest_could_still_ask_for(monkeypatch):
    """Retention is the app's own maximum window, so a prune never causes a
    re-download. A bar one day inside the limit stays; one day past it goes."""
    from datetime import datetime, timedelta, timezone

    s = _mem_session()
    now = datetime.now(timezone.utc)

    def stamp(days_ago):
        return (now - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")

    keep, drop = stamp(barcache.INTRADAY_KEEP_DAYS - 1), stamp(barcache.INTRADAY_KEEP_DAYS + 1)
    for model, sym in ((barcache.IntradayBar, "AAA"), (barcache.CryptoIntradayBar, "BTC/USD")):
        for ts in (keep, drop):
            s.add(model(symbol=sym, ts=ts, o=1, h=1, l=1, c=1, v=1, vw=1))
    s.commit()

    out = barcache.prune_intraday(s)
    assert out["stock"] == 1 and out["crypto"] == 1   # both markets pruned
    assert [b.ts for b in s.query(barcache.IntradayBar).all()] == [keep]
    assert [b.ts for b in s.query(barcache.CryptoIntradayBar).all()] == [keep]


def test_pruning_can_be_turned_off_entirely(monkeypatch):
    """0 means keep everything — for a durable Postgres cache with room to spare."""
    from datetime import datetime, timedelta, timezone

    s = _mem_session()
    ancient = (datetime.now(timezone.utc) - timedelta(days=3000)).strftime("%Y-%m-%dT%H:%M:%SZ")
    s.add(barcache.IntradayBar(symbol="AAA", ts=ancient, o=1, h=1, l=1, c=1, v=1, vw=1))
    s.commit()
    monkeypatch.setenv("QT_BAR_CACHE_KEEP_DAYS", "0")
    assert barcache.prune_intraday(s) == {"pruned": False, "keep_days": 0, "stock": 0, "crypto": 0}
    assert s.query(barcache.IntradayBar).count() == 1


def test_a_typo_in_the_retention_env_var_does_not_disable_pruning(monkeypatch):
    """The one setting whose failure mode is a full disk must not fail open."""
    monkeypatch.setenv("QT_BAR_CACHE_KEEP_DAYS", "forever")
    assert barcache.intraday_keep_days() == barcache.INTRADAY_KEEP_DAYS
    monkeypatch.setenv("QT_BAR_CACHE_KEEP_DAYS", "500")
    assert barcache.intraday_keep_days() == 500
