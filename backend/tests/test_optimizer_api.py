"""Optimizer endpoint guard: MACD/RSI strategies are daily signals, so an
intraday search is rejected (mirrors the backtest guard). The 422 happens
synchronously, before any background search starts."""

import pytest

from qt import security
from qt.broker.alpaca import SECRET_KEY_ID, SECRET_KEY_SECRET
from qt.db import session_scope
from qt.models import Strategy, StrategyConfigVersion, Trade


def _strategy(*, macd=False, rsi_exit=False) -> dict:
    entry = {
        "min_day_gain_pct": 1,
        "require_above_vwap": False,
        "require_macd_bullish": macd,
        "entry_window_start": None,
        "entry_window_end": None,
    }
    exit_rules = {
        "trailing_stop_pct": 5, "stop_loss_pct": 4, "take_profit_pct": 0,
        "max_holding_hours": 0, "flatten_before_close": False, "exit_below_vwap": False,
    }
    if rsi_exit:
        exit_rules["exit_rsi_above"] = 70
    return {
        "name": "opt guard", "asset_class": "stock", "universe": "custom",
        "symbols": ["AAPL"], "preset": "custom",
        "params": {"entry": entry, "exit": exit_rules},
        "sizing_usd": 1000, "sleeve_usd": 1000, "max_positions": 1,
        "swing_mode": True, "ignore_regime": True,
    }


@pytest.fixture()
def configured():
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


def _optimize(client, sid, timeframe):
    return client.post(
        "/api/optimizer",
        json={"strategy_id": sid, "symbols": ["AAPL"], "timeframe": timeframe,
              "days": 180, "iterations": 5},
    )


def test_optimizer_rejects_intraday_for_a_macd_strategy(client, configured):
    sid = client.post("/api/strategies", json=_strategy(macd=True)).json()["id"]
    r = _optimize(client, sid, "1Hour")
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert "MACD" in detail and "daily" in detail


def test_optimizer_rejects_intraday_for_an_rsi_strategy(client, configured):
    sid = client.post("/api/strategies", json=_strategy(rsi_exit=True)).json()["id"]
    r = _optimize(client, sid, "15Min")
    assert r.status_code == 422
    assert "daily" in r.json()["detail"]
