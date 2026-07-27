import asyncio

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from qt.broker.alpaca import AlpacaError
from qt.services import barcache, barsweep


def _mem_session():
    # In-memory SQLite with one shared connection so the schema persists.
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    barcache.CacheBase.metadata.create_all(eng)
    return sessionmaker(bind=eng, expire_on_commit=False)()


def _bar(day, c, v=1_000_000, vw=None):
    return {"t": f"{day}T00:00:00Z", "o": c, "h": c, "l": c, "c": c, "v": v, "vw": vw}


class FakeClient:
    """Stands in for AlpacaClient: canned assets + per-symbol daily bars."""

    def __init__(self, assets, bars_by_symbol, fail_batches=None):
        self._assets = assets
        self._bars = bars_by_symbol
        self._fail_batches = fail_batches or set()
        self.batch_calls = 0

    async def list_assets(self, alpaca_asset_class):
        assert alpaca_asset_class == "us_equity"
        return self._assets

    async def historical_bars(self, symbols, asset_class, timeframe, start_iso, end_iso=None):
        assert asset_class == "stock" and timeframe == "1Day"
        self.batch_calls += 1
        if self.batch_calls in self._fail_batches:
            raise AlpacaError(429, "rate limited")
        return {s: self._bars.get(s, []) for s in symbols}


class FakeIntradayClient:
    """Serves intraday bars and records each request's window, so tests can
    assert the sweep batches by day and includes a prior-session baseline."""

    def __init__(self, intraday_by_symbol):
        self._bars = intraday_by_symbol
        self.calls = []  # (symbols, timeframe, start, end)

    async def historical_bars(self, symbols, asset_class, timeframe, start_iso, end_iso=None):
        assert asset_class == "stock"
        self.calls.append((tuple(symbols), timeframe, start_iso, end_iso))
        # Return only bars that fall within [start, end) — like the real API.
        out = {}
        for s in symbols:
            out[s] = [b for b in self._bars.get(s, []) if start_iso <= b["t"] < (end_iso or "9999")]
        return out


ASSETS = [
    {"symbol": "AAA", "exchange": "NASDAQ", "tradable": True},
    {"symbol": "BBB", "exchange": "NYSE", "tradable": True},
    {"symbol": "CCC", "exchange": "ARCA", "tradable": True},
    {"symbol": "JUNK", "exchange": "OTC", "tradable": True},      # OTC — must be filtered out
    {"symbol": "BLANK", "exchange": "", "tradable": True},        # blank exchange — filtered out
]

BARS = {
    "AAA": [_bar("2026-06-01", 10.0), _bar("2026-06-02", 11.0), _bar("2026-06-03", 12.0)],
    "BBB": [_bar("2026-06-01", 20.0), _bar("2026-06-02", 20.5), _bar("2026-06-03", 21.0)],
    "CCC": [_bar("2026-06-01", 5.0), _bar("2026-06-02", 5.1), _bar("2026-06-03", 5.2)],
}


def test_universe_excludes_otc_and_blank():
    assert barsweep.tradable_universe(ASSETS) == ["AAA", "BBB", "CCC"]


def test_sweep_saves_bars_excludes_otc_and_is_idempotent():
    s = _mem_session()
    client = FakeClient(ASSETS, BARS)

    summary = asyncio.run(barsweep.sweep_daily_bars(client, s, days=30, batch_size=2))
    assert summary["symbols_total"] == 3          # JUNK + BLANK excluded
    assert summary["symbols_saved"] == 3
    assert summary["batches"] == 2                 # 3 symbols / batch_size 2
    assert summary["errors"] == 0

    # 3 symbols * 3 days = 9 rows, and JUNK never fetched.
    assert s.query(barcache.DailyBar).count() == 9
    assert s.query(barcache.DailyBar).filter(barcache.DailyBar.symbol == "JUNK").count() == 0

    # Re-running the sweep must not duplicate immutable bars.
    asyncio.run(barsweep.sweep_daily_bars(client, s, days=30, batch_size=2))
    assert s.query(barcache.DailyBar).count() == 9


def test_sweep_skips_failing_batch_without_aborting():
    s = _mem_session()
    # batch_size 1 => 3 batches; fail the 2nd (BBB).
    client = FakeClient(ASSETS, BARS, fail_batches={2})
    summary = asyncio.run(barsweep.sweep_daily_bars(client, s, days=30, batch_size=1))

    assert summary["errors"] == 1
    assert summary["symbols_saved"] == 2           # AAA + CCC saved, BBB skipped
    assert s.query(barcache.DailyBar).filter(barcache.DailyBar.symbol == "BBB").count() == 0
    assert s.query(barcache.DailyBar).filter(barcache.DailyBar.symbol == "AAA").count() == 3


def test_progress_callback_receives_batch_updates():
    s = _mem_session()
    client = FakeClient(ASSETS, BARS)
    seen = []
    asyncio.run(
        barsweep.sweep_daily_bars(
            client, s, days=30, batch_size=2, progress=lambda d, t, saved: seen.append((d, t, saved))
        )
    )
    assert seen[-1] == (2, 2, 3)                    # last update: batch 2 of 2, 3 symbols saved


def test_reconstruct_movers_ranks_top_gainer_and_skips_earliest_day():
    s = _mem_session()
    # 2026-06-02: AAA +10%, BBB +2.5%, CCC +2% → AAA is the clear rank-1 riser.
    # 2026-06-01 is the earliest day (no prior close) and must be skipped.
    barcache.save_daily_bars(s, "AAA", BARS["AAA"])
    barcache.save_daily_bars(s, "BBB", BARS["BBB"])
    barcache.save_daily_bars(s, "CCC", BARS["CCC"])
    s.commit()

    days = barsweep.reconstruct_movers(
        s, top_n=10, min_change_pct=0.0, min_price=0.0, max_price=0.0, min_dollar_volume=0.0
    )
    assert days == 2                                # 06-02 and 06-03; 06-01 skipped

    assert barcache.top_movers(s, "2026-06-01") == []       # earliest day never stored
    movers_02 = barcache.top_movers(s, "2026-06-02")
    assert movers_02[0].symbol == "AAA" and movers_02[0].rank == 1


def test_reconstruct_movers_applies_price_and_volume_filters():
    s = _mem_session()
    # PENNY: sub-$1 (excluded by min_price). THIN: tiny volume (excluded by
    # min_dollar_volume). GOOD: clears both. All gain +50% on 2026-06-02.
    barcache.save_daily_bars(s, "PENNY", [_bar("2026-06-01", 0.40), _bar("2026-06-02", 0.60)])
    barcache.save_daily_bars(s, "THIN", [_bar("2026-06-01", 10.0, v=100), _bar("2026-06-02", 15.0, v=100)])
    barcache.save_daily_bars(s, "GOOD", [_bar("2026-06-01", 10.0), _bar("2026-06-02", 15.0)])
    s.commit()

    barsweep.reconstruct_movers(
        s, top_n=10, min_change_pct=2.0, min_price=1.0, max_price=0.0, min_dollar_volume=5_000_000
    )
    movers = barcache.top_movers(s, "2026-06-02")
    assert [m.symbol for m in movers] == ["GOOD"]   # PENNY (price) and THIN (volume) excluded


def test_reconstruct_since_day_reranks_only_recent_days_with_a_real_prior_close():
    s = _mem_session()
    # AAA has bars on 06-01..06-04. Reconstructing only since 06-03 must still
    # measure 06-03's gain against 06-02's close (from the lookback window), and
    # must NOT (re)store movers for 06-02.
    barcache.save_daily_bars(s, "AAA", [
        _bar("2026-06-01", 10.0), _bar("2026-06-02", 10.0),
        _bar("2026-06-03", 12.0), _bar("2026-06-04", 12.0),
    ])
    s.commit()
    days = barsweep.reconstruct_movers(
        s, top_n=10, min_change_pct=0.0, min_price=0.0, max_price=0.0, min_dollar_volume=0.0,
        since_day="2026-06-03", lookback_days=15,
    )
    assert days == 2                                        # 06-03 and 06-04 only
    assert barcache.top_movers(s, "2026-06-02") == []       # pre-window day untouched
    m03 = barcache.top_movers(s, "2026-06-03")
    assert m03[0].symbol == "AAA" and round(m03[0].change_pct) == 20  # 10.0 → 12.0 vs prior close


def _ibar(ts, c):
    return {"t": ts, "o": c, "h": c, "l": c, "c": c, "v": 10000, "vw": c}


def test_sweep_intraday_movers_batches_by_day_with_prior_session_baseline():
    s = _mem_session()
    # AAA is a mover on 2026-06-02; BBB on 2026-06-03.
    barcache.store_movers(s, "2026-06-02", [("AAA", 40.0, 5.0, 1e7)])
    barcache.store_movers(s, "2026-06-03", [("BBB", 30.0, 8.0, 1e7)])
    s.commit()
    intraday = {
        "AAA": [_ibar("2026-06-01T14:00:00Z", 4.9), _ibar("2026-06-02T14:00:00Z", 6.9)],
        "BBB": [_ibar("2026-06-02T14:00:00Z", 7.9), _ibar("2026-06-03T14:00:00Z", 9.9)],
    }
    client = FakeIntradayClient(intraday)
    summary = asyncio.run(barsweep.sweep_intraday_movers(client, s, timeframe="15Min", baseline_days=4))

    assert summary["days"] == 2 and summary["errors"] == 0
    # One request per mover-day, each starting baseline_days before it (so the
    # prior session's bar is fetched → the day-gain baseline exists).
    assert len(client.calls) == 2
    (syms0, tf0, start0, end0) = client.calls[0]
    assert syms0 == ("AAA",) and tf0 == "15Min"
    assert start0 == "2026-05-29" and end0 == "2026-06-03"  # 06-02 minus 4 days .. 06-02 plus 1
    # Bars persisted and readable, prior-session bar included.
    got = barcache.cached_intraday_bars(s, ["AAA"], "2026-06-01")
    assert [b["t"] for b in got["AAA"]] == ["2026-06-01T14:00:00Z", "2026-06-02T14:00:00Z"]


def test_intraday_progress_reports_running_bar_count():
    # The status counter must climb live, not sit at 0 until the end.
    s = _mem_session()
    barcache.store_movers(s, "2026-06-02", [("AAA", 40.0, 5.0, 1e7)])
    barcache.store_movers(s, "2026-06-03", [("BBB", 30.0, 8.0, 1e7)])
    s.commit()
    intraday = {
        "AAA": [_ibar("2026-06-01T14:00:00Z", 4.9), _ibar("2026-06-02T14:00:00Z", 6.9)],
        "BBB": [_ibar("2026-06-02T14:00:00Z", 7.9), _ibar("2026-06-03T14:00:00Z", 9.9)],
    }
    seen = []  # (day_done, day_total, symbols_saved, bars_saved)
    asyncio.run(barsweep.sweep_intraday_movers(
        FakeIntradayClient(intraday), s, baseline_days=4,
        progress=lambda d, t, syms, bars: seen.append((d, t, syms, bars)),
    ))
    assert [c[3] for c in seen] == sorted(c[3] for c in seen)  # bar count only climbs
    assert seen[-1][3] > 0                                     # ends with a real count, not 0


def test_daily_movers_update_pulls_recent_and_reranks():
    s = _mem_session()
    from datetime import datetime, timedelta, timezone

    d_prev = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%d")
    d_today = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    bars = {"AAA": [_bar(d_prev, 10.0), _bar(d_today, 13.0)],   # +30%
            "BBB": [_bar(d_prev, 20.0), _bar(d_today, 20.2)]}   # +1%
    client = FakeClient([{"symbol": s_, "exchange": "NASDAQ", "tradable": True} for s_ in bars], bars)
    summary = asyncio.run(barsweep.daily_movers_update(
        client, s, min_change_pct=0.0, min_price=0.0, max_price=0.0, min_dollar_volume=0.0,
        overlap_days=5,
    ))
    assert summary["days_reconstructed"] >= 1
    movers = barcache.top_movers(s, d_today)
    assert movers[0].symbol == "AAA" and movers[0].rank == 1   # biggest riser ranked first
