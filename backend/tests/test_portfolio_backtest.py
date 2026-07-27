"""Portfolio (multi-strategy) backtest: N strategies share ONE account and the
GLOBAL rails on a merged timeline. Synthetic bars whose correct behaviour is
known by construction — same style as test_backtest.py."""

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
from qt.services.backtest import run_portfolio_backtest
from qt.services.engine import RISK_DEFAULTS

RISK = dict(RISK_DEFAULTS, max_total_exposure_usd=1_000_000, max_daily_loss_usd=1_000_000)


def bars_from(closes: list[float], start: str = "2026-05-04T14:00:00Z") -> list[dict]:
    t0 = datetime.fromisoformat(start.replace("Z", "+00:00"))
    return [
        {"t": (t0 + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M:%SZ"), "c": c, "v": 1000, "vw": c}
        for i, c in enumerate(closes)
    ]


def _two_day(day1: list[float], day2: list[float]) -> list[dict]:
    return bars_from(day1, "2026-05-04T14:00:00Z") + bars_from(day2, "2026-05-05T14:00:00Z")


def _strategy(sid: int, name: str, **over) -> dict:
    base = {
        "id": sid,
        "name": name,
        "asset_class": "stock",
        "swing_mode": False,
        "sizing_usd": 1000.0,
        "sleeve_usd": 5000.0,
        "max_positions": 3,
        "params": {
            "entry": {"min_day_gain_pct": 3.0, "require_above_vwap": False,
                      "entry_window_start": None, "entry_window_end": None},
            "exit": {"trailing_stop_pct": 5.0, "stop_loss_pct": 4.0, "take_profit_pct": 0,
                     "max_holding_hours": 0, "flatten_before_close": False, "exit_below_vwap": False},
        },
    }
    base.update(over)
    return base


# Two symbols that each qualify on day 2 (+4%) and hold flat to the end.
RISER = _two_day([100, 100, 100], [104, 104, 104])


def test_two_strategies_share_one_account_and_both_contribute():
    s1 = _strategy(1, "Alpha")
    s2 = _strategy(2, "Beta")
    result = run_portfolio_backtest(
        [s1, s2],
        {1: {"AAA": RISER}, 2: {"BBB": RISER}},
        RISK, starting_cash=5000, spread_pct=0,
    )
    # both strategies traded their own symbol on the shared account
    assert result["trades"] == 2
    assert {t["symbol"] for t in result["trade_list"]} == {"AAA", "BBB"}
    assert result["strategy_count"] == 2

    contribs = {c["strategy_id"]: c for c in result["contributions"]}
    assert contribs[1]["strategy_name"] == "Alpha"
    assert contribs[1]["trades"] == 1 and contribs[2]["trades"] == 1
    assert {t["strategy_id"] for t in result["trade_list"]} == {1, 2}


def test_contributions_sum_to_the_portfolio_realized_total():
    # Mixed outcomes: AAA holds up (win at liquidation), BBB stops out (loss).
    win = _two_day([100, 100, 100], [104, 106, 108])
    lose = _two_day([100, 100, 100], [104, 99, 99])
    result = run_portfolio_backtest(
        [_strategy(1, "Winner"), _strategy(2, "Loser")],
        {1: {"AAA": win}, 2: {"BBB": lose}},
        RISK, starting_cash=5000, spread_pct=0,
    )
    total = round(sum(c["realized_pnl"] for c in result["contributions"]), 2)
    assert total == result["realized_total"] == result["net_pnl"]
    # each side landed on the expected sign
    by_id = {c["strategy_id"]: c for c in result["contributions"]}
    assert by_id[1]["realized_pnl"] > 0
    assert by_id[2]["realized_pnl"] < 0
    # shares are sign-preserving and there are exactly two sleeves
    assert len(result["contributions"]) == 2


def test_max_total_positions_caps_combined_open_positions():
    # Both symbols qualify and hold to the end. With the global cap at 1, the
    # account can hold only ONE across BOTH strategies; raising it to 6 lets both
    # in — proving the CROSS-STRATEGY rail bound, not the per-strategy one.
    strategies = [_strategy(1, "Alpha"), _strategy(2, "Beta")]
    bars = {1: {"AAA": RISER}, 2: {"BBB": RISER}}

    capped = run_portfolio_backtest(
        strategies, bars, dict(RISK, max_total_positions=1), starting_cash=5000, spread_pct=0,
    )
    assert capped["trades"] == 1  # one strategy filled, the other blocked by the shared cap

    opened = run_portfolio_backtest(
        strategies, bars, dict(RISK, max_total_positions=6), starting_cash=5000, spread_pct=0,
    )
    assert opened["trades"] == 2  # cap lifted → both share the account


def test_exposure_never_exceeds_equity_no_leverage():
    # $1000 sizings on a $1500 account: the first position deploys ~$1000, the
    # second would push exposure past equity → the no-leverage rail blocks it.
    strategies = [_strategy(1, "Alpha"), _strategy(2, "Beta")]
    result = run_portfolio_backtest(
        strategies, {1: {"AAA": RISER}, 2: {"BBB": RISER}},
        dict(RISK, max_total_positions=6), starting_cash=1500, spread_pct=0,
    )
    assert result["trades"] == 1  # only one $1000 position fits under equity
    # invariant: the account was never leveraged
    assert result["max_deployed_usd"] <= result["starting_cash"]


def test_no_bars_returns_an_error_not_a_crash():
    result = run_portfolio_backtest(
        [_strategy(1, "Alpha")], {1: {"AAA": []}}, RISK, starting_cash=5000, spread_pct=0,
    )
    assert "error" in result


# --------------------------------------------------------------------------
# Endpoint: POST /api/backtest/portfolio
# --------------------------------------------------------------------------


def hourly(closes: list[float], symbol_days: int = 6) -> list[dict]:
    start = datetime.now(timezone.utc) - timedelta(days=symbol_days)
    return [
        {"t": (start + timedelta(hours=i * 6)).strftime("%Y-%m-%dT%H:%M:%SZ"),
         "o": c, "h": c * 1.01, "l": c * 0.99, "c": c, "v": 1000, "vw": c}
        for i, c in enumerate(closes)
    ]


API_BARS = hourly([100, 100, 100, 104, 106, 108, 108, 108])


def _custom_strategy_payload(name: str, symbol: str) -> dict:
    return {
        "name": name,
        "asset_class": "stock",
        "universe": "custom",
        "symbols": [symbol],
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


def test_portfolio_endpoint_runs_two_strategies_on_one_account(client, configured):
    a = client.post("/api/strategies", json=_custom_strategy_payload("Alpha", "AAA")).json()["id"]
    b = client.post("/api/strategies", json=_custom_strategy_payload("Beta", "BBB")).json()["id"]
    fetch = AsyncMock(return_value={"AAA": API_BARS, "BBB": API_BARS})
    with patch.object(AlpacaClient, "historical_bars", new=fetch):
        body = client.post(
            "/api/backtest/portfolio",
            json={"strategy_ids": [a, b], "days": 30, "timeframe": "1Hour",
                  "starting_cash": 5000, "spread_pct": 0},
        ).json()
    assert body["strategy_count"] == 2
    assert body["trades"] == 2
    assert fetch.await_count == 1  # both stock symbols fetched in one call
    total = round(sum(c["realized_pnl"] for c in body["contributions"]), 2)
    assert total == body["realized_total"] == body["net_pnl"]
    assert {c["strategy_id"] for c in body["contributions"]} == {a, b}


def test_portfolio_endpoint_404_on_unknown_strategy(client, configured):
    a = client.post("/api/strategies", json=_custom_strategy_payload("Alpha", "AAA")).json()["id"]
    r = client.post("/api/backtest/portfolio", json={"strategy_ids": [a, 999999], "days": 30})
    assert r.status_code == 404
