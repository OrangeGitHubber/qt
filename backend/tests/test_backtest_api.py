"""Backtest endpoint: benchmark selection, incl. not drawing the tested
symbol twice."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from qt import security
from qt.broker.alpaca import SECRET_KEY_ID, SECRET_KEY_SECRET, AlpacaClient
from qt.db import session_scope
from qt.models import Strategy, StrategyConfigVersion, Trade
from qt.services import barcache, barsweep


def hourly(closes: list[float], symbol_days: int = 6) -> list[dict]:
    start = datetime.now(timezone.utc) - timedelta(days=symbol_days)
    return [
        {
            "t": (start + timedelta(hours=i * 6)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "o": c, "h": c * 1.01, "l": c * 0.99, "c": c, "v": 1000, "vw": c,
        }
        for i, c in enumerate(closes)
    ]


BARS = hourly([100, 100, 100, 104, 106, 108, 108, 108])


def _strategy(asset_class: str) -> dict:
    return {
        "name": f"bt {asset_class}",
        "asset_class": asset_class,
        "universe": "scanner",
        "preset": "custom",
        "params": {
            "entry": {"min_day_gain_pct": 3, "require_above_vwap": False,
                      "entry_window_start": None, "entry_window_end": None},
            "exit": {"trailing_stop_pct": 5, "stop_loss_pct": 4, "take_profit_pct": 0,
                     "max_holding_hours": 0, "flatten_before_close": False, "exit_below_vwap": False},
        },
        "sizing_usd": 1000, "sleeve_usd": 5000, "max_positions": 3,
        "swing_mode": False, "ignore_regime": True,
    }


@pytest.fixture()
def configured(client):
    with session_scope() as s:
        security.set_secret(s, SECRET_KEY_ID, "k")
        security.set_secret(s, SECRET_KEY_SECRET, "s")
    yield
    with session_scope() as s:
        s.query(Trade).delete()
        s.query(StrategyConfigVersion).delete()
        s.query(Strategy).delete()
        security.delete_secret(s, SECRET_KEY_ID)
        security.delete_secret(s, SECRET_KEY_SECRET)


def _make(client, asset_class: str) -> int:
    return client.post("/api/strategies", json=_strategy(asset_class)).json()["id"]


def _entries(body: dict) -> int:
    """Closed round-trips + positions still open at the test end. A mover that
    enters and holds through a short window is now an open position, not a
    force-sold 'trade'."""
    return body["trades"] + len(body["open_positions"])


def test_market_benchmark_skipped_when_it_is_the_tested_symbol(client, configured):
    """A BTC/USD strategy tested on BTC/USD must not plot BTC/USD twice."""
    sid = _make(client, "crypto")
    fetch = AsyncMock(return_value={"BTC/USD": BARS})
    with patch.object(AlpacaClient, "historical_bars", new=fetch):
        body = client.post(
            "/api/backtest",
            json={"strategy_id": sid, "symbols": ["BTC/USD"], "days": 30,
                  "timeframe": "1Hour", "starting_cash": 5000, "spread_pct": 0},
        ).json()
    assert body["benchmark"] is None
    assert body["benchmark_symbol"] is None
    assert body["hold_benchmark_label"] == "BTC/USD"   # the one true comparison
    assert fetch.await_count == 1                      # no wasted benchmark fetch


def test_market_benchmark_kept_when_it_differs(client, configured):
    """A stock strategy on NVDA still gets SPY as the market line."""
    sid = _make(client, "stock")
    fetch = AsyncMock(side_effect=[{"NVDA": BARS}, {"SPY": BARS}])
    with patch.object(AlpacaClient, "historical_bars", new=fetch):
        body = client.post(
            "/api/backtest",
            json={"strategy_id": sid, "symbols": ["NVDA"], "days": 30,
                  "timeframe": "1Hour", "starting_cash": 5000, "spread_pct": 0},
        ).json()
    assert body["benchmark_symbol"] == "SPY"
    assert body["hold_benchmark_label"] == "NVDA"
    assert fetch.await_count == 2


@pytest.fixture()
def seeded_cache(monkeypatch):
    """Point the global bar cache at a fresh in-memory DB and seed two recent
    trading days of daily bars + reconstructed movers."""
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    barcache.CacheBase.metadata.create_all(eng)
    Sess = sessionmaker(bind=eng, expire_on_commit=False)
    monkeypatch.setattr(barcache, "_engine", eng)
    monkeypatch.setattr(barcache, "_Session", Sess)

    # Two consecutive recent days: D0 baseline, D1 the +gain day.
    d0 = (datetime.now(timezone.utc) - timedelta(days=6)).strftime("%Y-%m-%d")
    d1 = (datetime.now(timezone.utc) - timedelta(days=5)).strftime("%Y-%m-%d")
    with Sess() as s:
        for sym in ("MOVER", "OTHER"):
            barcache.save_daily_bars(s, sym, [
                {"t": f"{d0}T14:00:00Z", "o": 100, "h": 100, "l": 100, "c": 100, "v": 1e6, "vw": 100},
                {"t": f"{d1}T14:00:00Z", "o": 105, "h": 105, "l": 105, "c": 105, "v": 1e6, "vw": 105},
            ])
        # Only MOVER made the top-N on D1 → OTHER must be gated out.
        barcache.store_movers(s, d1, [("MOVER", 5.0, 105.0, 1e8)])
        s.commit()
    return d1


def test_scanner_replay_gates_to_cached_movers(client, configured, seeded_cache):
    """End-to-end: replay uses the cached day's movers, not a symbol list. Only
    MOVER was a riser on D1, so OTHER (same +5%) must never trade."""
    sid = _make(client, "stock")
    fetch = AsyncMock(side_effect=Exception("no benchmark in test"))  # SPY best-effort
    with patch.object(AlpacaClient, "historical_bars", new=fetch):
        body = client.post(
            "/api/backtest",
            json={"strategy_id": sid, "scanner_replay": True, "days": 30,
                  "starting_cash": 5000, "spread_pct": 0},
        ).json()
    assert body["scanner_replay"] is True
    assert body["universe_size"] == 1        # one unique mover across the window
    assert body["days_replayed"] == 1
    assert body["timeframe"] == "1Day"
    assert _entries(body) == 1               # MOVER entered (held to end); OTHER gated out
    assert body["open_positions"][0]["symbol"] == "MOVER"


def test_scanner_replay_top_n_narrows_the_eligible_movers(client, configured, monkeypatch):
    """replay_top_n narrows each day's eligible risers at read time — no re-sweep.
    Three symbols all qualify on D1; top_n=1 lets only the rank-1 name trade."""
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    barcache.CacheBase.metadata.create_all(eng)
    Sess = sessionmaker(bind=eng, expire_on_commit=False)
    monkeypatch.setattr(barcache, "_engine", eng)
    monkeypatch.setattr(barcache, "_Session", Sess)
    d0 = (datetime.now(timezone.utc) - timedelta(days=6)).strftime("%Y-%m-%d")
    d1 = (datetime.now(timezone.utc) - timedelta(days=5)).strftime("%Y-%m-%d")
    with Sess() as s:
        for sym, c1 in (("TOP", 106), ("MID", 105), ("LOW", 104)):
            barcache.save_daily_bars(s, sym, [
                {"t": f"{d0}T14:00:00Z", "o": 100, "h": 100, "l": 100, "c": 100, "v": 1e6, "vw": 100},
                {"t": f"{d1}T14:00:00Z", "o": c1, "h": c1, "l": c1, "c": c1, "v": 1e6, "vw": c1},
            ])
        barcache.store_movers(s, d1, [  # stored wide; ranked TOP > MID > LOW
            ("TOP", 6.0, 106.0, 1e8), ("MID", 5.0, 105.0, 1e8), ("LOW", 4.0, 104.0, 1e8),
        ])
        s.commit()

    def run(top_n):
        fetch = AsyncMock(side_effect=Exception("no benchmark"))
        with patch.object(AlpacaClient, "historical_bars", new=fetch):
            return client.post("/api/backtest", json={
                "strategy_id": sid, "scanner_replay": True, "replay_top_n": top_n,
                "days": 30, "starting_cash": 50000, "spread_pct": 0}).json()

    sid = _make(client, "stock")
    one = run(1)
    assert one["replay_top_n"] == 1 and one["universe_size"] == 1
    assert [p["symbol"] for p in one["open_positions"]] == ["TOP"]
    three = run(3)
    assert three["universe_size"] == 3
    assert {p["symbol"] for p in three["open_positions"]} == {"TOP", "MID", "LOW"}


def test_scanner_replay_prefers_intraday_bars_when_cached(client, configured, monkeypatch):
    """When intraday bars exist for the movers, replay runs on them (15Min) so
    an intraday flatten-before-close strategy actually flattens same day."""
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    barcache.CacheBase.metadata.create_all(eng)
    Sess = sessionmaker(bind=eng, expire_on_commit=False)
    monkeypatch.setattr(barcache, "_engine", eng)
    monkeypatch.setattr(barcache, "_Session", Sess)
    d0 = (datetime.now(timezone.utc) - timedelta(days=6)).strftime("%Y-%m-%d")
    d1 = (datetime.now(timezone.utc) - timedelta(days=5)).strftime("%Y-%m-%d")

    def ib(ts, c):
        return {"t": ts, "o": c, "h": c, "l": c, "c": c, "v": 1e5, "vw": c}

    with Sess() as s:
        # Prior session flat at 100 (baseline), entry day rises 104→106 intraday.
        barcache.save_intraday_bars(s, "MOVER", [
            ib(f"{d0}T14:00:00Z", 100), ib(f"{d0}T20:00:00Z", 100),
            ib(f"{d1}T14:00:00Z", 104), ib(f"{d1}T15:00:00Z", 105), ib(f"{d1}T20:00:00Z", 106),
        ])
        barcache.store_movers(s, d1, [("MOVER", 6.0, 106.0, 1e8)])
        s.commit()

    sid = _make(client, "stock")
    # Enable flatten-before-close on the strategy so intraday exit is observable.
    strat = _strategy("stock")
    strat["params"]["exit"]["flatten_before_close"] = True
    client.put(f"/api/strategies/{sid}", json=strat)

    fetch = AsyncMock(side_effect=Exception("no benchmark"))
    with patch.object(AlpacaClient, "historical_bars", new=fetch):
        body = client.post("/api/backtest", json={
            "strategy_id": sid, "scanner_replay": True, "days": 30,
            "starting_cash": 5000, "spread_pct": 0}).json()

    assert body["replay_intraday"] is True
    assert body["timeframe"] == "15Min"
    assert body["trades"] == 1
    assert body["trade_list"][0]["exit_reason"].startswith("flatten before market close")


def test_scanner_replay_heals_a_cache_missing_the_intraday_table(client, configured, monkeypatch):
    """Regression: a cache built before the intraday_bars table existed (has
    movers + daily bars only) must not 500 on the now-queried intraday table —
    the endpoint recreates the missing table and falls back to daily bars."""
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    barcache.CacheBase.metadata.create_all(eng)
    Sess = sessionmaker(bind=eng, expire_on_commit=False)
    monkeypatch.setattr(barcache, "_engine", eng)
    monkeypatch.setattr(barcache, "_Session", Sess)
    d0 = (datetime.now(timezone.utc) - timedelta(days=6)).strftime("%Y-%m-%d")
    d1 = (datetime.now(timezone.utc) - timedelta(days=5)).strftime("%Y-%m-%d")
    with Sess() as s:
        barcache.save_daily_bars(s, "MOVER", [
            {"t": f"{d0}T14:00:00Z", "o": 100, "h": 100, "l": 100, "c": 100, "v": 1e6, "vw": 100},
            {"t": f"{d1}T14:00:00Z", "o": 105, "h": 105, "l": 105, "c": 105, "v": 1e6, "vw": 105},
        ])
        barcache.store_movers(s, d1, [("MOVER", 5.0, 105.0, 1e8)])
        s.commit()
    # Simulate the older cache: the intraday table was never created.
    barcache.IntradayBar.__table__.drop(eng)

    sid = _make(client, "stock")
    fetch = AsyncMock(side_effect=Exception("no benchmark"))
    with patch.object(AlpacaClient, "historical_bars", new=fetch):
        r = client.post("/api/backtest", json={
            "strategy_id": sid, "scanner_replay": True, "days": 30,
            "starting_cash": 5000, "spread_pct": 0})
    assert r.status_code == 200, r.text  # not a 500 on the missing table
    body = r.json()
    assert body["replay_intraday"] is False and _entries(body) == 1  # healed → daily fallback


def test_scanner_replay_needs_a_sweep_first(client, configured, monkeypatch):
    """With an empty cache, replay explains itself instead of silently doing nothing."""
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    barcache.CacheBase.metadata.create_all(eng)
    monkeypatch.setattr(barcache, "_engine", eng)
    monkeypatch.setattr(barcache, "_Session", sessionmaker(bind=eng, expire_on_commit=False))
    sid = _make(client, "stock")
    r = client.post("/api/backtest", json={"strategy_id": sid, "scanner_replay": True, "days": 30})
    assert r.status_code == 422
    assert "sweep" in r.json()["detail"].lower()


def test_scanner_replay_crypto_needs_a_crypto_sweep_first(client, configured, monkeypatch):
    """An empty crypto cache explains itself and names the crypto sweep."""
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    barcache.CacheBase.metadata.create_all(eng)
    monkeypatch.setattr(barcache, "_engine", eng)
    monkeypatch.setattr(barcache, "_Session", sessionmaker(bind=eng, expire_on_commit=False))
    sid = _make(client, "crypto")
    r = client.post("/api/backtest", json={"strategy_id": sid, "scanner_replay": True, "days": 30})
    assert r.status_code == 422
    detail = r.json()["detail"].lower()
    assert "crypto" in detail and "sweep" in detail


def test_scanner_replay_crypto_gates_to_cached_movers_by_utc_day(client, configured, monkeypatch):
    """End-to-end crypto replay: reads the CRYPTO tables, buckets by UTC day, and
    gates entries to that day's cached movers — BTC traded, ETH gated out."""
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    barcache.CacheBase.metadata.create_all(eng)
    Sess = sessionmaker(bind=eng, expire_on_commit=False)
    monkeypatch.setattr(barcache, "_engine", eng)
    monkeypatch.setattr(barcache, "_Session", Sess)
    d0 = (datetime.now(timezone.utc) - timedelta(days=6)).strftime("%Y-%m-%d")
    d1 = (datetime.now(timezone.utc) - timedelta(days=5)).strftime("%Y-%m-%d")
    with Sess() as s:
        # Crypto daily bars are UTC-aligned (00:00Z). D1 rises +5% for both pairs.
        for sym in ("BTC/USD", "ETH/USD"):
            barcache.save_daily_bars(s, sym, [
                {"t": f"{d0}T00:00:00Z", "o": 100, "h": 100, "l": 100, "c": 100, "v": 1e6, "vw": 100},
                {"t": f"{d1}T00:00:00Z", "o": 105, "h": 105, "l": 105, "c": 105, "v": 1e6, "vw": 105},
            ], model=barcache.CryptoDailyBar)
        # Only BTC made the top-N on D1 → ETH must be gated out.
        barcache.store_movers(s, d1, [("BTC/USD", 5.0, 105.0, 1e8)], model=barcache.CryptoDailyMover)
        s.commit()

    sid = _make(client, "crypto")
    fetch = AsyncMock(side_effect=Exception("no benchmark in test"))
    with patch.object(AlpacaClient, "historical_bars", new=fetch):
        body = client.post("/api/backtest", json={
            "strategy_id": sid, "scanner_replay": True, "days": 30,
            "starting_cash": 5000, "spread_pct": 0}).json()
    assert body["scanner_replay"] is True
    assert body["universe_size"] == 1        # one unique crypto mover
    assert body["days_replayed"] == 1
    assert body["timeframe"] == "1Day"
    assert _entries(body) == 1               # BTC entered (held to end); ETH gated out
    assert body["open_positions"][0]["symbol"] == "BTC/USD"
    assert body["open_positions"][0]["entry_day"] == d1   # UTC-day bucket, not ET


def test_scanner_replay_crypto_prefers_intraday_bars(client, configured, monkeypatch):
    """Crypto replay uses cached 15-min bars (UTC-day bucketed) when present."""
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    barcache.CacheBase.metadata.create_all(eng)
    Sess = sessionmaker(bind=eng, expire_on_commit=False)
    monkeypatch.setattr(barcache, "_engine", eng)
    monkeypatch.setattr(barcache, "_Session", Sess)
    d0 = (datetime.now(timezone.utc) - timedelta(days=6)).strftime("%Y-%m-%d")
    d1 = (datetime.now(timezone.utc) - timedelta(days=5)).strftime("%Y-%m-%d")

    def ib(ts, c):
        return {"t": ts, "o": c, "h": c, "l": c, "c": c, "v": 1e5, "vw": c}

    with Sess() as s:
        barcache.save_intraday_bars(s, "BTC/USD", [
            ib(f"{d0}T12:00:00Z", 100), ib(f"{d0}T18:00:00Z", 100),
            ib(f"{d1}T12:00:00Z", 104), ib(f"{d1}T18:00:00Z", 106),
        ], model=barcache.CryptoIntradayBar)
        barcache.store_movers(s, d1, [("BTC/USD", 6.0, 106.0, 1e8)], model=barcache.CryptoDailyMover)
        s.commit()

    sid = _make(client, "crypto")
    fetch = AsyncMock(side_effect=Exception("no benchmark"))
    with patch.object(AlpacaClient, "historical_bars", new=fetch):
        body = client.post("/api/backtest", json={
            "strategy_id": sid, "scanner_replay": True, "days": 30,
            "starting_cash": 5000, "spread_pct": 0}).json()
    assert body["replay_intraday"] is True
    assert body["timeframe"] == "15Min"
    assert _entries(body) == 1  # entered on the intraday crypto mover, held to window end


def _no_price_exits(strat: dict) -> dict:
    """Strip every price-triggered exit (stop / trailing / take-profit). Without
    one there is nothing an intraday replay could simulate that a daily one
    can't, so such a strategy stays locked to daily bars. A hard stop is
    mandatory for every strategy EXCEPT a buy-and-hold DCA sleeve, so this is
    the one shape that can legitimately have no price exit at all."""
    strat["params"]["exit"].update(
        {"stop_loss_pct": 0, "trailing_stop_pct": 0, "take_profit_pct": 0}
    )
    strat["params"]["dca"] = {"interval_days": 7}
    return strat


def test_macd_strategy_with_no_stops_rejects_intraday_bars(client, configured):
    """MACD is a DAILY signal live. With no price-triggered exit there's nothing
    to gain from an intraday replay — it would just compute a twitchy intraday
    MACD unlike live — so the endpoint still rejects 15Min/1Hour, and 1Day works."""
    strat = _no_price_exits(_strategy("stock"))
    strat["params"]["entry"]["require_macd_bullish"] = True
    sid = client.post("/api/strategies", json=strat).json()["id"]

    r = client.post("/api/backtest", json={
        "strategy_id": sid, "symbols": ["NVDA"], "days": 30,
        "timeframe": "1Hour", "starting_cash": 5000, "spread_pct": 0})
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert "macd" in detail.lower() and "daily" in detail.lower()

    fetch = AsyncMock(side_effect=[{"NVDA": BARS}, {"SPY": BARS}])
    with patch.object(AlpacaClient, "historical_bars", new=fetch):
        ok = client.post("/api/backtest", json={
            "strategy_id": sid, "symbols": ["NVDA"], "days": 30,
            "timeframe": "1Day", "starting_cash": 5000, "spread_pct": 0})
    assert ok.status_code == 200, ok.text
    assert ok.json()["timeframe"] == "1Day"
    assert ok.json()["mixed_resolution"] is False


def test_rsi_exit_strategy_with_no_stops_rejects_intraday_bars(client, configured):
    """The RSI overbought exit is likewise a daily signal — and with no stop to
    simulate, intraday stays rejected."""
    strat = _no_price_exits(_strategy("stock"))
    strat["params"]["exit"]["exit_rsi_above"] = 70
    sid = client.post("/api/strategies", json=strat).json()["id"]
    r = client.post("/api/backtest", json={
        "strategy_id": sid, "symbols": ["NVDA"], "days": 30,
        "timeframe": "15Min", "starting_cash": 5000, "spread_pct": 0})
    assert r.status_code == 422
    assert "daily" in r.json()["detail"].lower()


def test_macd_strategy_with_stops_runs_mixed_resolution(client, configured):
    """A MACD strategy that ALSO carries a stop is the case one bar stream can't
    serve: daily bars fake the stop, intraday bars fake the signal. It now
    replays 15-minute bars with the MACD taken from a daily series — two fetches,
    the daily one reaching back over the warm-up, the intraday one only over the
    tested window."""
    strat = _strategy("stock")  # keeps stop_loss 4% / trailing 5%
    strat["params"]["entry"]["require_macd_bullish"] = True
    sid = client.post("/api/strategies", json=strat).json()["id"]

    fetch = AsyncMock(side_effect=[{"NVDA": BARS}, {"NVDA": BARS}, {"SPY": BARS}])
    with patch.object(AlpacaClient, "historical_bars", new=fetch):
        r = client.post("/api/backtest", json={
            "strategy_id": sid, "symbols": ["NVDA"], "days": 30,
            "timeframe": "15Min", "starting_cash": 5000, "spread_pct": 0})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mixed_resolution"] is True
    assert body["timeframe"] == "15Min"        # what the exits were checked on
    assert body["signal_timeframe"] == "1Day"  # where MACD/RSI came from

    daily_call, intraday_call = fetch.await_args_list[0], fetch.await_args_list[1]
    assert daily_call.args[2] == "1Day" and intraday_call.args[2] == "15Min"
    # The daily series starts WARMUP_DAYS earlier; the intraday one does not.
    assert daily_call.args[3] < intraday_call.args[3]


def test_market_benchmark_kept_for_a_basket_including_it(client, configured):
    """BTC + ETH basket: 'hold the basket' and 'hold BTC' are different facts."""
    sid = _make(client, "crypto")
    fetch = AsyncMock(side_effect=[{"BTC/USD": BARS, "ETH/USD": BARS}, {"BTC/USD": BARS}])
    with patch.object(AlpacaClient, "historical_bars", new=fetch):
        body = client.post(
            "/api/backtest",
            json={"strategy_id": sid, "symbols": ["BTC/USD", "ETH/USD"], "days": 30,
                  "timeframe": "1Hour", "starting_cash": 5000, "spread_pct": 0},
        ).json()
    assert body["benchmark_symbol"] == "BTC/USD"
    assert body["hold_benchmark_label"] == "2 symbols (equal weight)"


def test_partial_intraday_coverage_falls_back_instead_of_dropping_movers(
    client, configured, monkeypatch
):
    """Regression: the resolution choice used to be `any(intraday_bars)`, so ONE
    symbol with cached 15-min bars flipped the whole replay to intraday — and
    every uncovered mover was silently dropped (run_backtest skips empty series)
    while the header still claimed the full universe. That was safe only while
    the intraday table was filled exclusively by the all-or-nothing sweep; once
    ordinary backtests began caching their own bars, an incidental symbol could
    quietly shrink a 2-symbol replay to 1.

    Full coverage is now required: partial coverage uses DAILY bars, which the
    daily sweep fills for every mover, so nothing disappears."""
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    barcache.CacheBase.metadata.create_all(eng)
    Sess = sessionmaker(bind=eng, expire_on_commit=False)
    monkeypatch.setattr(barcache, "_engine", eng)
    monkeypatch.setattr(barcache, "_Session", Sess)
    d0 = (datetime.now(timezone.utc) - timedelta(days=6)).strftime("%Y-%m-%d")
    d1 = (datetime.now(timezone.utc) - timedelta(days=5)).strftime("%Y-%m-%d")

    with Sess() as s:
        # BOTH names were movers and BOTH have daily bars…
        for sym in ("MOVER", "OTHER"):
            barcache.save_daily_bars(s, sym, [
                {"t": f"{d0}T14:00:00Z", "o": 100, "h": 100, "l": 100, "c": 100, "v": 1e6, "vw": 100},
                {"t": f"{d1}T14:00:00Z", "o": 106, "h": 106, "l": 106, "c": 106, "v": 1e6, "vw": 106},
            ])
        # …but only ONE has intraday bars (e.g. cached by an ordinary backtest).
        barcache.save_intraday_bars(s, "MOVER", [
            {"t": f"{d1}T14:00:00Z", "o": 104, "h": 104, "l": 104, "c": 104, "v": 1e5, "vw": 104},
        ])
        barcache.store_movers(s, d1, [("MOVER", 6.0, 106.0, 1e8), ("OTHER", 6.0, 106.0, 1e8)])
        s.commit()

    sid = _make(client, "stock")
    fetch = AsyncMock(side_effect=Exception("no benchmark"))
    with patch.object(AlpacaClient, "historical_bars", new=fetch):
        body = client.post("/api/backtest", json={
            "strategy_id": sid, "scanner_replay": True, "days": 30,
            "starting_cash": 5000, "spread_pct": 0}).json()

    # Partial coverage (1 of 2) → daily bars, and BOTH movers survive.
    assert body["replay_intraday"] is False
    assert body["timeframe"] == "1Day"
    assert body["intraday_covered"] == 1
    assert body["universe_size"] == 2          # what was actually TESTED
    assert body["universe_dropped"] == []


def test_scanner_replay_runs_mixed_for_an_atr_strategy(client, configured, monkeypatch):
    """The gap this closes: the scanner-replay path had no route to the daily
    bars at all, so an intraday replay computed ATR from 15-minute bars — 14
    periods spanning 3.5 hours instead of 14 days — and every stop derived from
    it came out a fraction of its real width. The replay now takes its signals
    from the cached daily series while the exits run on intraday bars."""
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    barcache.CacheBase.metadata.create_all(eng)
    Sess = sessionmaker(bind=eng, expire_on_commit=False)
    monkeypatch.setattr(barcache, "_engine", eng)
    monkeypatch.setattr(barcache, "_Session", Sess)
    d0 = (datetime.now(timezone.utc) - timedelta(days=6)).strftime("%Y-%m-%d")
    d1 = (datetime.now(timezone.utc) - timedelta(days=5)).strftime("%Y-%m-%d")
    warm = (datetime.now(timezone.utc) - timedelta(days=40)).strftime("%Y-%m-%d")

    def ib(ts, c):
        return {"t": ts, "o": c, "h": c, "l": c, "c": c, "v": 1e5, "vw": c}

    with Sess() as s:
        barcache.save_intraday_bars(s, "BTC/USD", [
            ib(f"{d0}T12:00:00Z", 100), ib(f"{d0}T18:00:00Z", 100),
            ib(f"{d1}T12:00:00Z", 104), ib(f"{d1}T18:00:00Z", 106),
        ], model=barcache.CryptoIntradayBar)
        # Daily history for the ATR — including a bar from before the window.
        barcache.save_daily_bars(s, "BTC/USD", [
            {"t": f"{warm}T00:00:00Z", "o": 90, "h": 95, "l": 88, "c": 92, "v": 1e6, "vw": 92},
            {"t": f"{d0}T00:00:00Z", "o": 100, "h": 102, "l": 98, "c": 100, "v": 1e6, "vw": 100},
            {"t": f"{d1}T00:00:00Z", "o": 104, "h": 107, "l": 103, "c": 106, "v": 1e6, "vw": 106},
        ], model=barcache.CryptoDailyBar)
        barcache.store_movers(s, d1, [("BTC/USD", 6.0, 106.0, 1e8)], model=barcache.CryptoDailyMover)
        s.commit()

    strat = _strategy("crypto")
    strat["params"]["atr"] = {"period": 14, "stop_mult": 1.5, "risk_usd": 0}
    sid = client.post("/api/strategies", json=strat).json()["id"]
    fetch = AsyncMock(side_effect=Exception("no benchmark"))
    with patch.object(AlpacaClient, "historical_bars", new=fetch):
        body = client.post("/api/backtest", json={
            "strategy_id": sid, "scanner_replay": True, "days": 30,
            "starting_cash": 5000, "spread_pct": 0}).json()
    assert body["replay_intraday"] is True
    assert body["mixed_resolution"] is True     # daily signals, intraday exits
    assert body["signal_timeframe"] == "1Day"
    assert body["timeframe"] == "15Min"


def test_scanner_replay_without_daily_signals_is_not_mixed(client, configured, monkeypatch):
    """A strategy with no daily indicator never reads the daily series, so its
    intraday replay is single-resolution — and must not claim otherwise."""
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    barcache.CacheBase.metadata.create_all(eng)
    Sess = sessionmaker(bind=eng, expire_on_commit=False)
    monkeypatch.setattr(barcache, "_engine", eng)
    monkeypatch.setattr(barcache, "_Session", Sess)
    d0 = (datetime.now(timezone.utc) - timedelta(days=6)).strftime("%Y-%m-%d")
    d1 = (datetime.now(timezone.utc) - timedelta(days=5)).strftime("%Y-%m-%d")

    def ib(ts, c):
        return {"t": ts, "o": c, "h": c, "l": c, "c": c, "v": 1e5, "vw": c}

    with Sess() as s:
        barcache.save_intraday_bars(s, "BTC/USD", [
            ib(f"{d0}T12:00:00Z", 100), ib(f"{d1}T12:00:00Z", 106),
        ], model=barcache.CryptoIntradayBar)
        barcache.store_movers(s, d1, [("BTC/USD", 6.0, 106.0, 1e8)], model=barcache.CryptoDailyMover)
        s.commit()

    sid = _make(client, "crypto")  # plain momentum: no MACD/RSI/ATR
    fetch = AsyncMock(side_effect=Exception("no benchmark"))
    with patch.object(AlpacaClient, "historical_bars", new=fetch):
        body = client.post("/api/backtest", json={
            "strategy_id": sid, "scanner_replay": True, "days": 30,
            "starting_cash": 5000, "spread_pct": 0}).json()
    assert body["replay_intraday"] is True
    assert body["mixed_resolution"] is False


def _seed_movers_without_intraday(monkeypatch):
    """A crypto mover with daily bars cached but NO intraday bars — the state
    that used to demote a replay to daily and tell you to go run a sweep."""
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    barcache.CacheBase.metadata.create_all(eng)
    Sess = sessionmaker(bind=eng, expire_on_commit=False)
    monkeypatch.setattr(barcache, "_engine", eng)
    monkeypatch.setattr(barcache, "_Session", Sess)
    d0 = (datetime.now(timezone.utc) - timedelta(days=6)).strftime("%Y-%m-%d")
    d1 = (datetime.now(timezone.utc) - timedelta(days=5)).strftime("%Y-%m-%d")
    with Sess() as s:
        barcache.save_daily_bars(s, "BTC/USD", [
            {"t": f"{d0}T00:00:00Z", "o": 100, "h": 101, "l": 99, "c": 100, "v": 1e6, "vw": 100},
            {"t": f"{d1}T00:00:00Z", "o": 104, "h": 107, "l": 103, "c": 106, "v": 1e6, "vw": 106},
        ], model=barcache.CryptoDailyBar)
        barcache.store_movers(s, d1, [("BTC/USD", 6.0, 106.0, 1e8)], model=barcache.CryptoDailyMover)
        s.commit()
    return d0, d1


def test_replay_downloads_the_intraday_bars_it_is_missing(client, configured, monkeypatch):
    """The whole point: pressing Backtest fetches the missing 15-minute bars and
    replays on them, instead of quietly running on daily bars and telling you to
    visit Settings."""
    d0, d1 = _seed_movers_without_intraday(monkeypatch)

    def ib(ts, c):
        return {"t": ts, "o": c, "h": c, "l": c, "c": c, "v": 1e5, "vw": c}

    fetched = {"BTC/USD": [ib(f"{d0}T12:00:00Z", 100), ib(f"{d0}T18:00:00Z", 100),
                           ib(f"{d1}T12:00:00Z", 104), ib(f"{d1}T18:00:00Z", 106)]}
    # First call = the auto top-up; the later benchmark call is best-effort.
    fetch = AsyncMock(side_effect=[fetched, Exception("no benchmark")])
    sid = _make(client, "crypto")
    with patch.object(AlpacaClient, "historical_bars", new=fetch):
        body = client.post("/api/backtest", json={
            "strategy_id": sid, "scanner_replay": True, "days": 30,
            "starting_cash": 5000, "spread_pct": 0}).json()

    assert body["intraday_topped_up"] is True
    assert body["replay_intraday"] is True   # it really replayed on the new bars
    assert body["timeframe"] == "15Min"
    # And they were CACHED, so the next run is offline.
    with barcache.session() as s:
        assert barcache.cached_intraday_bars(s, ["BTC/USD"], d0, model=barcache.CryptoIntradayBar)["BTC/USD"]


def test_a_failed_top_up_still_returns_a_backtest(client, configured, monkeypatch):
    """A download problem must not turn into a failed backtest — it falls back to
    the daily bars that are already cached and says so."""
    _seed_movers_without_intraday(monkeypatch)
    fetch = AsyncMock(side_effect=Exception("alpaca down"))
    sid = _make(client, "crypto")
    with patch.object(AlpacaClient, "historical_bars", new=fetch):
        r = client.post("/api/backtest", json={
            "strategy_id": sid, "scanner_replay": True, "days": 30,
            "starting_cash": 5000, "spread_pct": 0})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["intraday_topped_up"] is False
    assert body["replay_intraday"] is False
    assert body["timeframe"] == "1Day"


def test_no_download_for_a_strategy_that_gains_nothing_from_intraday(client, configured, monkeypatch):
    """A buy-and-hold sleeve has no stop, no VWAP rule and no entry window, so an
    intraday replay would tell it nothing a daily one doesn't. Don't spend the
    bandwidth."""
    _seed_movers_without_intraday(monkeypatch)
    strat = _no_price_exits(_strategy("crypto"))
    sid = client.post("/api/strategies", json=strat).json()["id"]
    fetch = AsyncMock(side_effect=Exception("no benchmark"))
    with patch.object(AlpacaClient, "historical_bars", new=fetch):
        body = client.post("/api/backtest", json={
            "strategy_id": sid, "scanner_replay": True, "days": 30,
            "starting_cash": 5000, "spread_pct": 0}).json()
    assert body["intraday_topped_up"] is False
    # The only call that may have happened is the benchmark — never a bar sweep.
    assert all("15Min" not in str(c) for c in fetch.await_args_list)


def test_a_broken_top_up_falls_back_instead_of_failing_the_run(client, configured, monkeypatch):
    """The sweep swallows per-DAY fetch errors itself, so the guard around it is
    for the rest: an unreadable cache DB, a bad DSN, a sweep that raises. None of
    those should turn "backtest" into an error page when perfectly good daily
    bars are sitting in the cache."""
    _seed_movers_without_intraday(monkeypatch)

    async def boom(*a, **kw):
        raise RuntimeError("cache DB unreadable")

    monkeypatch.setattr(barsweep, "sweep_crypto_intraday", boom)
    sid = _make(client, "crypto")
    fetch = AsyncMock(side_effect=Exception("no benchmark"))
    with patch.object(AlpacaClient, "historical_bars", new=fetch):
        r = client.post("/api/backtest", json={
            "strategy_id": sid, "scanner_replay": True, "days": 30,
            "starting_cash": 5000, "spread_pct": 0})
    assert r.status_code == 200, r.text
    assert r.json()["intraday_topped_up"] is False
    assert r.json()["timeframe"] == "1Day"
