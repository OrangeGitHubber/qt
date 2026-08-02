"""Held days get REAL 15-minute bars, not just a daily stand-in.

Filling a held position's missing days with its daily bar stops the replay going
blind, but it can only resolve an exit once per day. If the stop was really hit at
11:00, a daily bar settles it at the bar's own stamp — so the capital and the
position slot stay tied up for hours they wouldn't have been, and entries the
strategy would have taken with that freed slot never happen. The fill keeps the
run honest about P&L; it cannot keep it honest about timing.

Which symbols get held, and for how long, is a property of the STRATEGY — it
cannot be known before the replay runs, and pre-fetching intraday bars for every
mover across the window would download tens of times more than any run uses. So
the replay runs once to learn the holdings, fetches exactly those symbol-days,
and replays again.
"""

import pytest

from qt.api.backtest import _days_between, held_spans
from tests.test_backtest_api import _make, configured  # noqa: F401 — shared fixtures


def test_a_closed_trade_covers_entry_through_exit():
    result = {
        "trade_list": [
            {"symbol": "DOGE/USD", "entry_day": "2026-05-05", "exit_day": "2026-05-09"}
        ],
        "open_positions": [],
    }
    assert held_spans(result) == {"DOGE/USD": ("2026-05-05", "2026-05-09")}


def test_a_position_still_open_covers_through_the_last_tested_day():
    """An open position needed bars right up to the end of the window — that is
    exactly the case that produced a flat line running to the chart's edge."""
    result = {
        "trade_list": [],
        "open_positions": [{"symbol": "ADA/USD", "entry_day": "2026-05-05"}],
        "equity_days": ["2026-05-04", "2026-05-05", "2026-06-30"],
    }
    assert held_spans(result) == {"ADA/USD": ("2026-05-05", "2026-06-30")}


def test_several_positions_in_one_symbol_merge_into_one_span():
    """Two round trips weeks apart become a single fetch range. Requesting each
    separately would cost more calls than fetching straight through."""
    result = {
        "trade_list": [
            {"symbol": "BTC/USD", "entry_day": "2026-05-05", "exit_day": "2026-05-07"},
            {"symbol": "BTC/USD", "entry_day": "2026-06-01", "exit_day": "2026-06-03"},
        ],
        "open_positions": [],
    }
    assert held_spans(result) == {"BTC/USD": ("2026-05-05", "2026-06-03")}


def test_a_same_day_round_trip_still_needs_its_day():
    """Entered and exited on one day: the span is that day, not an empty range."""
    result = {
        "trade_list": [
            {"symbol": "ETH/USD", "entry_day": "2026-05-05", "exit_day": "2026-05-05"}
        ],
        "open_positions": [],
    }
    assert held_spans(result) == {"ETH/USD": ("2026-05-05", "2026-05-05")}


def test_a_trade_with_no_exit_day_is_treated_as_open_on_its_entry_day():
    """exit_day is None for a position the log hasn't closed. Reading that as an
    empty span would skip the one day we know it was held."""
    result = {
        "trade_list": [{"symbol": "SOL/USD", "entry_day": "2026-05-05", "exit_day": None}],
        "open_positions": [],
    }
    assert held_spans(result) == {"SOL/USD": ("2026-05-05", "2026-05-05")}


def test_a_run_that_never_traded_asks_for_nothing():
    """No holdings, no download. The whole point is that the fetch is bounded by
    what was actually held."""
    assert held_spans({"trade_list": [], "open_positions": []}) == {}
    assert held_spans({}) == {}


def test_the_day_range_is_inclusive_at_both_ends():
    assert _days_between("2026-05-05", "2026-05-08") == [
        "2026-05-05", "2026-05-06", "2026-05-07", "2026-05-08"
    ]
    assert _days_between("2026-05-05", "2026-05-05") == ["2026-05-05"]


def test_the_replay_fetches_the_held_days_it_only_had_daily_bars_for(client, configured, monkeypatch):
    """End to end through the endpoint: a position is held past the day its
    symbol was a riser, so those days arrive as daily fills on the first pass —
    and the run then downloads the real 15-minute bars for exactly that span."""
    from datetime import datetime, timedelta, timezone
    from unittest.mock import AsyncMock, patch

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from qt.broker.alpaca import AlpacaClient
    from qt.services import barcache

    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    barcache.CacheBase.metadata.create_all(eng)
    Sess = sessionmaker(bind=eng, expire_on_commit=False)
    monkeypatch.setattr(barcache, "_engine", eng)
    monkeypatch.setattr(barcache, "_Session", Sess)

    now = datetime.now(timezone.utc)
    days = [(now - timedelta(days=n)).strftime("%Y-%m-%d") for n in (7, 6, 5, 4)]
    riser = days[1]

    def ib(ts, c):
        return {"t": ts, "o": c, "h": c, "l": c, "c": c, "v": 1e5, "vw": c}

    with Sess() as s:
        # Intraday only around the riser day — the cache's real shape.
        barcache.save_intraday_bars(s, "BTC/USD", [
            ib(f"{days[0]}T12:00:00Z", 100.0), ib(f"{riser}T12:00:00Z", 106.0),
        ], model=barcache.CryptoIntradayBar)
        barcache.save_daily_bars(s, "BTC/USD", [
            {"t": f"{d}T00:00:00Z", "o": c, "h": c, "l": c, "c": c, "v": 1e6, "vw": c}
            for d, c in zip(days, (100.0, 106.0, 105.0, 104.0))
        ], model=barcache.CryptoDailyBar)
        barcache.store_movers(s, riser, [("BTC/USD", 6.0, 106.0, 1e8)],
                              model=barcache.CryptoDailyMover)
        s.commit()

    # The bars the second pass goes and gets for the held days.
    fetched = {"BTC/USD": [ib(f"{days[2]}T06:00:00Z", 105.5), ib(f"{days[2]}T18:00:00Z", 105.0),
                           ib(f"{days[3]}T06:00:00Z", 104.5), ib(f"{days[3]}T18:00:00Z", 104.0)]}
    calls: list[tuple] = []

    async def fake_bars(self, symbols, asset_class, timeframe, start, end=None):
        calls.append((tuple(symbols), timeframe, start, end))
        return fetched

    sid = _make(client, "crypto")
    with patch.object(AlpacaClient, "historical_bars", new=fake_bars):
        body = client.post("/api/backtest", json={
            "strategy_id": sid, "scanner_replay": True, "days": 30,
            "starting_cash": 5000, "spread_pct": 0}).json()

    assert body["replay_intraday"] is True
    # It asked for 15-minute bars for the held symbol, over the held days only.
    held_calls = [c for c in calls if c[1] == "15Min"]
    assert held_calls, f"no intraday fetch for held days; calls were {calls}"
    assert held_calls[0][0] == ("BTC/USD",)
    # ...and the downloaded bars were cached, so the next run reads them offline.
    with barcache.session() as s:
        cached = barcache.cached_intraday_bars(
            s, ["BTC/USD"], days[0], model=barcache.CryptoIntradayBar
        )["BTC/USD"]
    assert any(b["t"].startswith(days[2]) for b in cached)
