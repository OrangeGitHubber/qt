"""A "match" thirteen hours apart is not a match.

MEASURED on "Crypto - many movements" once the comparison could finally run.
Trades are paired by (symbol, DAY) — deliberately, because a live fill at 14:03
and a 14:00 bar ARE the same decision and demanding equal timestamps would report
every trade as a mismatch. For crypto a day is 24 hours of UTC, so that pairing
rule let these all read as the same green "match":

    XRP  live 08-04 06:46Z   replay 06:48Z    2 minutes
    SOL  live 08-03 00:57Z   replay 13:55Z    13 hours
    AAVE live 08-03 16:44Z   replay 01:07Z    15.6 hours (replay first)

The report carried both instants the whole time and the verdict ignored them, so
a strategy trading a dozen times a day scored a match rate built partly on trades
that happened most of a day apart.

THE TOLERANCE IS DERIVED, NOT PICKED. The replay acts when a bar CLOSES; the live
engine looks every 60 seconds from an unrecorded offset. So the two sample
instants up to one bar plus one poll interval apart for reasons that are not
disagreements — exactly the residual `exit_model.poll_phase_floor_pct` already
reports. Past that, something differed. That formula also degrades correctly: on
a DAILY replay the tolerance is a whole day, so a daily entry — which has no
meaningful clock, only a bar stamp — is never flagged.

Unknown resolution flags nothing. A tolerance nobody can compute is not a licence
to guess, and the same "unknown must not be the flattering answer" rule that
governs bar_seconds elsewhere applies in reverse here: it must not be the
CONDEMNING answer either.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from qt import security
from qt.api import fidelity as fidelity_api
from qt.broker.alpaca import SECRET_KEY_ID, SECRET_KEY_SECRET, AlpacaClient
from qt.db import session_scope
from qt.models import Strategy, StrategyConfigVersion, Trade
from qt.services import backtest as backtest_service
from qt.services import barcache
from qt.services.fidelity import compare

DAY = "2026-08-03"
MINUTE = 60.0
DAILY = 86_400.0


def _live(at: str, symbol: str = "SOL/USD") -> dict:
    return {"symbol": symbol, "entry_day": DAY, "entry_at": at, "exit_day": None,
            "entry_price": 100.0, "pnl": None, "status": "open",
            "entry_reason": "gain", "exit_reason": None}


def _sim(at: str, symbol: str = "SOL/USD") -> dict:
    return {"symbol": symbol, "entry_day": DAY, "entry_at": at, "exit_day": None,
            "entry_price": 100.0, "pnl": None, "exit_reason": None}


def _bought(live_at: str, sim_at: str, tolerance: float | None) -> dict:
    report = compare(
        [_live(live_at)],
        {"trade_list": [], "open_positions": [_sim(sim_at)]},
        timing_tolerance_seconds=tolerance,
    )
    return next(r for r in report["log"] if r["action"] == "bought")


def test_a_fill_within_one_bar_and_one_poll_is_still_a_match():
    """The measured XRP row: two minutes on a 1-minute replay. The replay grades
    a bar at its close and the engine looks every 60 seconds from an offset
    nothing records, so this gap is the sampling residual, not a decision."""
    row = _bought("2026-08-04T06:46:00+00:00", "2026-08-04T06:48:00+00:00",
                  MINUTE + 60.0)
    assert row["verdict"] == "match", row


def test_a_fill_thirteen_hours_later_is_not():
    """The measured SOL row. Same UTC day, same symbol, same green verdict as the
    two-minute one — which is what made the match rate mean less than it read."""
    row = _bought("2026-08-03T00:57:00+00:00", "2026-08-03T13:55:00+00:00",
                  MINUTE + 60.0)
    assert row["verdict"] == "timing differs", row
    assert "13 hours" in row["detail"], row["detail"]


def test_the_replay_being_first_is_reported_the_same_way():
    """AAVE, where the replay bought 15 hours BEFORE live. A rule that only
    caught a late replay would miss half of them, and the early ones matter more
    — those are the trades it had no business taking yet."""
    row = _bought("2026-08-03T16:44:00+00:00", "2026-08-03T01:07:00+00:00",
                  MINUTE + 60.0)
    assert row["verdict"] == "timing differs", row


def test_a_daily_replay_is_not_flagged_for_a_gap_smaller_than_its_bar():
    """A 1Day entry carries a bar stamp, not a fill time, so the clock difference
    is an artefact of the resolution. The derived tolerance handles this without a
    special case — which is why it is derived."""
    row = _bought("2026-08-03T00:57:00+00:00", "2026-08-03T13:55:00+00:00",
                  DAILY + 60.0)
    assert row["verdict"] == "match", row


def test_an_unknown_resolution_flags_nothing():
    row = _bought("2026-08-03T00:57:00+00:00", "2026-08-03T13:55:00+00:00", None)
    assert row["verdict"] == "match", row


def test_the_count_is_on_the_decision_stats_too():
    """One line in a log of forty is easy to miss, and the match rate above it is
    the number people actually read."""
    report = compare(
        [_live("2026-08-03T00:57:00+00:00"), _live("2026-08-04T06:46:00+00:00", "XRP/USD")],
        {"trade_list": [], "open_positions": [
            _sim("2026-08-03T13:55:00+00:00"), _sim("2026-08-04T06:48:00+00:00", "XRP/USD")]},
        timing_tolerance_seconds=MINUTE + 60.0,
    )
    assert report["decision"]["matched"] == 2, "both are still the same trade"
    assert report["decision"]["entries_timing_differs"] == 1, report["decision"]


# ---------------------------------------------------------------------------
# …and the tolerance has to REACH the comparison. Every fault found on
# 2026-08-04 lived in the wiring between two correct halves, not inside either
# of them: chunking that never reached the replay, a seed that never reached the
# rails, a filter deleted because no fixture could distinguish it. A rule that
# is only tested by calling it directly is a rule that has not been tested.
# ---------------------------------------------------------------------------


@pytest.fixture()
def cache(monkeypatch):
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False},
                        poolclass=StaticPool)
    barcache.CacheBase.metadata.create_all(eng)
    monkeypatch.setattr(barcache, "_engine", eng)
    monkeypatch.setattr(barcache, "_Session", sessionmaker(bind=eng, expire_on_commit=False))


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


async def _bars(self, symbols, asset_class, timeframe, start, end=None):
    first = datetime.fromisoformat(start.replace("Z", "+00:00"))
    step = timedelta(days=1) if timeframe == "1Day" else timedelta(minutes=15)
    out, cursor, last = [], first, datetime.now(timezone.utc) + timedelta(days=1)
    while cursor < last:
        out.append({"t": cursor.strftime("%Y-%m-%dT%H:%M:%SZ"), "o": 100.0, "h": 100.0,
                    "l": 100.0, "c": 100.0, "v": 1e5, "vw": 100.0})
        cursor += step
    return {s: list(out) for s in symbols}


def test_the_endpoint_hands_the_comparison_a_real_tolerance(client, configured, cache):
    """Not None, and derived from the resolution the replay actually used. With
    None the check is inert and every gap reads "match" again — the failure mode
    is silent, which is exactly why it is asserted at the boundary."""
    end = (datetime.now(timezone.utc) - timedelta(days=1)).replace(
        hour=23, minute=0, second=0, microsecond=0)
    start = end - timedelta(hours=6)
    sid = client.post("/api/strategies", json={
        "name": "timing wiring", "asset_class": "crypto", "universe": "custom",
        "symbols": ["TMG/USD"], "preset": "custom",
        "params": {
            "entry": {"min_day_gain_pct": 3.0, "require_above_vwap": True,
                      "entry_window_start": None, "entry_window_end": None},
            "exit": {"trailing_stop_pct": 5, "stop_loss_pct": 4, "take_profit_pct": 0,
                     "max_holding_hours": 0, "flatten_before_close": False,
                     "exit_below_vwap": False},
        },
        "sizing_usd": 100, "sleeve_usd": 1000, "max_positions": 3,
        "swing_mode": True, "ignore_regime": True,
    }).json()["id"]
    with session_scope() as s:
        s.add(Trade(
            strategy_id=sid, mode="paper", symbol="TMG/USD", asset_class="crypto",
            status="open", qty=10, notional=100, entry_price=100.0,
            entry_reason="gain", entry_at=start + timedelta(hours=1),
        ))

    seen: dict = {}
    real = fidelity_api.fidelity.compare

    def spy(*args, **kwargs):
        seen.update(kwargs)
        return real(*args, **kwargs)

    with patch.object(AlpacaClient, "historical_bars", new=_bars), \
            patch.object(fidelity_api.fidelity, "compare", new=spy):
        response = client.post("/api/fidelity/compare", json={
            "strategy_id": sid, "mode": "paper",
            "window_start": start.isoformat(), "window_end": end.isoformat(),
        })
    assert response.status_code == 200, response.text

    tolerance = seen.get("timing_tolerance_seconds")
    assert tolerance is not None, (
        "the comparison was handed no tolerance, so every gap reads as a match "
        f"whatever its size — kwargs: {sorted(seen)}"
    )
    # One bar plus one poll, and nothing else. Asserted against the FORMULA over
    # every bar size the replay can choose, so widening the tolerance by any
    # other route — a constant nudged, a fudge factor added — fails here.
    poll = backtest_service.LIVE_POLL_SECONDS
    assert tolerance in {60 + poll, 900 + poll, 3600 + poll, 86_400 + poll}, tolerance
