"""Replaying a config that is NOT the strategy's current one.

Every trade records the `StrategyConfigVersion` that produced it, precisely so a
later question — "would the backtester have made this trade?" — can be asked of
the settings that were live at the time. Until now nothing could use that: the
replay read the strategy row and got today's settings, so editing a stop after a
trade quietly changed the question from "does the replay reproduce reality" to
"does today's strategy reproduce yesterday's trades".

`replay()` takes a config and a symbol list instead of a strategy id, so a
historical snapshot can be handed in. `run()` — the endpoint — is a thin wrapper
that looks the strategy up and passes today's; there is still exactly one
implementation of a backtest underneath both.
"""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from qt import security
from qt.api import backtest as backtest_api
from qt.api.backtest import BacktestBody
from qt.broker.alpaca import SECRET_KEY_ID, SECRET_KEY_SECRET, AlpacaClient
from qt.db import session_scope
from qt.models import Strategy, StrategyConfigVersion, Trade


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


def _strategy_body(min_gain: float) -> dict:
    return {
        "name": "config replay", "asset_class": "stock", "universe": "custom",
        "symbols": ["CFGA"], "preset": "custom",
        "params": {
            "entry": {"min_day_gain_pct": min_gain, "require_above_vwap": False,
                      "entry_window_start": None, "entry_window_end": None},
            "exit": {"trailing_stop_pct": 5, "stop_loss_pct": 4, "take_profit_pct": 0,
                     "max_holding_hours": 0, "flatten_before_close": False,
                     "exit_below_vwap": False},
        },
        "sizing_usd": 1000, "sleeve_usd": 5000, "max_positions": 3,
        "swing_mode": True, "ignore_regime": True,
    }


def _bars() -> list[dict]:
    """Twenty daily bars, flat at 100 until a single +4% day. That rise clears a
    3% entry bar and fails a 20% one, so which config was replayed is visible in
    the trade count alone."""
    now = datetime.now(timezone.utc)
    out = []
    for n in range(20, 0, -1):
        c = 100.0 if n > 10 else 104.0
        ts = (now - timedelta(days=n)).replace(hour=14, minute=0, second=0, microsecond=0)
        out.append({"t": ts.strftime("%Y-%m-%dT%H:%M:%SZ"), "o": c, "h": c, "l": c,
                    "c": c, "v": 1000, "vw": c})
    return out


def _traded(result: dict) -> int:
    return len(result["trade_list"]) + len(result["open_positions"])


def _body(sid: int) -> BacktestBody:
    return BacktestBody(strategy_id=sid, symbols=["CFGA"], days=30, timeframe="1Day",
                        starting_cash=5000, spread_pct=0)


async def test_the_config_handed_in_wins_over_the_strategy_row(client, configured):
    """The whole point. The strategy is edited to a threshold nothing clears; a
    replay of the config that was live BEFORE that edit still finds the trade."""
    sid = client.post("/api/strategies", json=_strategy_body(3)).json()["id"]
    with session_scope() as s:
        then = backtest_api.replay_strategy(
            json.loads(s.query(StrategyConfigVersion).filter_by(strategy_id=sid).one().snapshot)
        )
    client.put(f"/api/strategies/{sid}", json=_strategy_body(20))  # nothing clears 20%

    with patch.object(AlpacaClient, "historical_bars", new=AsyncMock(return_value={"CFGA": _bars()})):
        # Today's settings, through the endpoint the browser uses.
        today = client.post(
            "/api/backtest",
            json={"strategy_id": sid, "symbols": ["CFGA"], "days": 30, "timeframe": "1Day",
                  "starting_cash": 5000, "spread_pct": 0},
        ).json()
        # The same window, the same bars, the config that was live back then.
        with session_scope() as session:
            before = await backtest_api.replay(
                _body(sid), then, ["CFGA"], strategy_name="config replay",
                session=session, client=AlpacaClient("k", "s"),
            )

    assert _traded(today) == 0, "a 20% entry bar should reject a 4% day"
    assert _traded(before) == 1, "the snapshot's 3% bar should accept it"


async def test_the_endpoint_still_replays_todays_settings(client, configured):
    """The control for the test above, and the guarantee for every existing
    caller: run() must keep meaning "replay this strategy as it stands"."""
    sid = client.post("/api/strategies", json=_strategy_body(3)).json()["id"]
    with patch.object(AlpacaClient, "historical_bars", new=AsyncMock(return_value={"CFGA": _bars()})):
        body = client.post(
            "/api/backtest",
            json={"strategy_id": sid, "symbols": ["CFGA"], "days": 30, "timeframe": "1Day",
                  "starting_cash": 5000, "spread_pct": 0},
        ).json()
    assert _traded(body) == 1


async def test_the_symbols_handed_in_are_the_ones_replayed(client, configured):
    """A universe is half of a config. Replaying the basket as it is TODAY
    against trades made from an older membership is the same mistake as
    replaying today's stop — so `replay()` takes the symbol list rather than
    re-deriving it from the strategy row."""
    sid = client.post("/api/strategies", json=_strategy_body(3)).json()["id"]
    with session_scope() as s:
        config = backtest_api.replay_strategy(s.get(Strategy, sid))

    bars = {"CFGA": _bars(), "CFGB": _bars()}
    with patch.object(AlpacaClient, "historical_bars", new=AsyncMock(return_value=bars)):
        with session_scope() as session:
            narrow = await backtest_api.replay(
                _body(sid), config, ["CFGA"], strategy_name="config replay",
                session=session, client=AlpacaClient("k", "s"),
            )
            wide = await backtest_api.replay(
                _body(sid), config, ["CFGA", "CFGB"], strategy_name="config replay",
                session=session, client=AlpacaClient("k", "s"),
            )
    assert narrow["symbols"] == ["CFGA"] and _traded(narrow) == 1
    assert wide["symbols"] == ["CFGA", "CFGB"] and _traded(wide) == 2


def test_a_snapshot_and_a_row_produce_the_same_replay_config(client, configured):
    """The two readers must agree, or a comparison between "then" and "now" would
    be measuring the reader as much as the config. `params` arrives as JSON text
    from the row and as a dict from the snapshot — the one place they differ."""
    import json

    sid = client.post("/api/strategies", json=_strategy_body(3)).json()["id"]
    with session_scope() as s:
        from_row = backtest_api.replay_strategy(s.get(Strategy, sid))
        snapshot = json.loads(
            s.query(StrategyConfigVersion).filter_by(strategy_id=sid).one().snapshot
        )
    from_snapshot = backtest_api.replay_strategy(snapshot)
    assert from_row == from_snapshot
    assert from_row["params"]["entry"]["min_day_gain_pct"] == 3
