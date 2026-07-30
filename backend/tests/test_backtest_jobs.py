"""Background backtest jobs.

A long replay outlives an HTTP request — Cloudflare cuts the connection at a
fixed 100 seconds (HTTP 524) and nginx at 60 by default — so the browser starts
a job and polls it. These tests pin the contract the poller depends on: the job
returns the SAME result the direct endpoint does, and a failure inside the task
surfaces as an error rather than a hang.
"""

import asyncio
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from qt import security
from qt.broker.alpaca import SECRET_KEY_ID, SECRET_KEY_SECRET, AlpacaClient
from qt.db import session_scope
from qt.models import Strategy, StrategyConfigVersion, Trade


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


def _strategy(asset_class: str = "stock") -> dict:
    return {
        "name": f"job {asset_class}",
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


def _poll(client, job_id: str, tries: int = 100) -> dict:
    """Poll like the browser does, but without the 1.5s wait."""
    for _ in range(tries):
        body = client.get(f"/api/backtest/job/{job_id}").json()
        if not body["running"]:
            return body
        time.sleep(0.02)
    raise AssertionError("job never finished")


def test_job_returns_the_same_result_as_the_direct_endpoint(client, configured):
    """The job path must not become a second implementation that can drift."""
    sid = client.post("/api/strategies", json=_strategy()).json()["id"]
    req = {"strategy_id": sid, "symbols": ["NVDA"], "days": 30,
           "timeframe": "1Hour", "starting_cash": 5000, "spread_pct": 0}

    with patch.object(AlpacaClient, "historical_bars",
                      new=AsyncMock(side_effect=[{"NVDA": BARS}, {"SPY": BARS}])):
        direct = client.post("/api/backtest", json=req).json()

    with patch.object(AlpacaClient, "historical_bars",
                      new=AsyncMock(side_effect=[{"NVDA": BARS}, {"SPY": BARS}])):
        start = client.post("/api/backtest/start", json=req)
        assert start.status_code == 200
        job = _poll(client, start.json()["job_id"])

    assert job["error"] is None
    assert job["result"]["trades"] == direct["trades"]
    assert job["result"]["equity"] == direct["equity"]
    assert job["result"]["strategy_name"] == direct["strategy_name"]


def test_start_rejects_an_unknown_strategy_immediately(client, configured):
    """A typo shouldn't cost a poll cycle to discover."""
    r = client.post("/api/backtest/start",
                    json={"strategy_id": 999999, "symbols": ["NVDA"], "days": 30,
                          "timeframe": "1Hour", "starting_cash": 5000, "spread_pct": 0})
    assert r.status_code == 404


def test_a_failure_inside_the_job_surfaces_as_an_error(client, configured):
    """The whole point of the rewrite is that failures stop being invisible: a
    422 raised by the handler has to reach the poller, with its status."""
    sid = client.post("/api/strategies", json=_strategy()).json()["id"]
    # No symbols passed and an empty watchlist → the handler raises 422.
    start = client.post("/api/backtest/start",
                        json={"strategy_id": sid, "symbols": [], "days": 30,
                              "timeframe": "1Hour", "starting_cash": 5000, "spread_pct": 0})
    job = _poll(client, start.json()["job_id"])
    assert job["result"] is None
    assert job["status_code"] == 422
    assert "symbols" in job["error"].lower()


def test_a_broker_failure_surfaces_too(client, configured):
    """Alpaca falling over must not leave the poller waiting forever."""
    sid = client.post("/api/strategies", json=_strategy()).json()["id"]
    with patch.object(AlpacaClient, "historical_bars",
                      new=AsyncMock(side_effect=RuntimeError("alpaca exploded"))):
        start = client.post("/api/backtest/start",
                            json={"strategy_id": sid, "symbols": ["NVDA"], "days": 30,
                                  "timeframe": "1Hour", "starting_cash": 5000, "spread_pct": 0})
        job = _poll(client, start.json()["job_id"])
    assert job["result"] is None
    assert "alpaca exploded" in job["error"]


def test_unknown_job_id_explains_itself(client, configured):
    """After a restart the job is gone; say so instead of a bare 404."""
    r = client.get("/api/backtest/job/does-not-exist")
    assert r.status_code == 404
    assert "run it again" in r.json()["detail"].lower()


def test_portfolio_job_runs_too(client, configured):
    """The portfolio replay is the slower of the two — it needs the job path most."""
    sid = client.post("/api/strategies", json=_strategy()).json()["id"]
    req = {"strategy_ids": [sid], "days": 30, "timeframe": "1Hour",
           "starting_cash": 5000, "spread_pct": 0}
    with patch.object(AlpacaClient, "historical_bars",
                      new=AsyncMock(return_value={"NVDA": BARS})):
        start = client.post("/api/backtest/portfolio/start", json=req)
        assert start.status_code == 200
        job = _poll(client, start.json()["job_id"])
    assert job["kind"] == "portfolio"
    assert (job["result"] is not None) or (job["error"] is not None)


def test_the_replay_does_not_block_the_event_loop(client, configured):
    """The job scheme collapses if the replay hogs the loop.

    run_backtest is pure CPU with no awaits. Called inline, it owns the event
    loop for its whole duration — freezing the engine tick, every other request,
    and the /job polls that exist to keep each request SHORT. The proxy would
    then time out on the poll instead of the backtest: same HTTP 524, more code.
    It runs in a thread for exactly that reason.

    Tested against the handler directly rather than through the API, because
    TestClient cancels tasks spawned during a request when that request's portal
    closes (uvicorn keeps one long-lived loop, so background jobs survive there —
    the same pattern the optimizer has always used). Here a heartbeat coroutine
    races the replay: if the replay blocks, the heartbeat cannot tick.
    """
    import qt.api.backtest as api_bt
    import qt.services.backtest as bt_service
    from qt.db import session_scope as scope

    sid = client.post("/api/strategies", json=_strategy()).json()["id"]
    real = bt_service.run_backtest

    def slow(*a, **kw):
        time.sleep(1.0)  # stands in for the replay's arithmetic
        return real(*a, **kw)

    body = api_bt.BacktestBody(strategy_id=sid, symbols=["NVDA"], days=30,
                               timeframe="1Hour", starting_cash=5000, spread_pct=0)
    beats = 0

    async def heartbeat():
        nonlocal beats
        while True:
            await asyncio.sleep(0.02)
            beats += 1

    async def drive():
        pulse = asyncio.create_task(heartbeat())
        try:
            with scope() as session:
                return await api_bt.run(body, session, AlpacaClient("k", "s"))
        finally:
            pulse.cancel()

    with patch.object(AlpacaClient, "historical_bars",
                      new=AsyncMock(side_effect=[{"NVDA": BARS}, {"SPY": BARS}])), \
         patch.object(bt_service, "run_backtest", new=slow):
        result = asyncio.run(drive())

    assert result["strategy_name"]
    # A blocked loop would leave this at roughly zero; a free one ticks ~50 times
    # during the 1s replay. Assert well under that to stay clear of CI jitter.
    assert beats >= 20, f"heartbeat ticked only {beats}x — the replay is blocking the event loop"
