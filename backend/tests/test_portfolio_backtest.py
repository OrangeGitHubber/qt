"""Portfolio (multi-strategy) backtest: N strategies share ONE account and the
GLOBAL rails on a merged timeline. Synthetic bars whose correct behaviour is
known by construction — same style as test_backtest.py."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
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


def _entries(r: dict) -> int:
    """Closed round-trips + positions still open at the end (marked to market,
    not force-sold). A riser that holds flat to the end is now an open position."""
    return r["trades"] + len(r["open_positions"])


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
    # both strategies entered their own symbol on the shared account (each holds
    # flat to the end → an open position, not a closed trade)
    assert _entries(result) == 2
    assert {p["symbol"] for p in result["open_positions"]} == {"AAA", "BBB"}
    assert result["strategy_count"] == 2

    contribs = {c["strategy_id"]: c for c in result["contributions"]}
    assert contribs[1]["strategy_name"] == "Alpha"
    assert {p["strategy_id"] for p in result["open_positions"]} == {1, 2}


def test_contributions_sum_to_the_portfolio_realized_total():
    # Mixed outcomes: AAA holds up (win at liquidation), BBB stops out (loss).
    win = _two_day([100, 100, 100], [104, 106, 108])
    lose = _two_day([100, 100, 100], [104, 99, 99])
    result = run_portfolio_backtest(
        [_strategy(1, "Winner"), _strategy(2, "Loser")],
        {1: {"AAA": win}, 2: {"BBB": lose}},
        RISK, starting_cash=5000, spread_pct=0,
    )
    # realized (closed) + unrealized (still open) across strategies reconciles to
    # net_pnl. AAA holds up (unrealized win), BBB stops out (realized loss).
    total = round(sum(c["realized_pnl"] + c["unrealized_pnl"] for c in result["contributions"]), 2)
    assert total == result["net_pnl"]
    by_id = {c["strategy_id"]: c for c in result["contributions"]}
    assert by_id[1]["unrealized_pnl"] > 0  # winner held to the end
    assert by_id[2]["realized_pnl"] < 0    # loser exited on the stop
    # there are exactly two sleeves
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
    assert _entries(capped) == 1  # one strategy filled, the other blocked by the shared cap

    opened = run_portfolio_backtest(
        strategies, bars, dict(RISK, max_total_positions=6), starting_cash=5000, spread_pct=0,
    )
    assert _entries(opened) == 2  # cap lifted → both share the account


def test_exposure_never_exceeds_equity_no_leverage():
    # $1000 sizings on a $1500 account: the first position deploys ~$1000, the
    # second would push exposure past equity → the no-leverage rail blocks it.
    strategies = [_strategy(1, "Alpha"), _strategy(2, "Beta")]
    result = run_portfolio_backtest(
        strategies, {1: {"AAA": RISER}, 2: {"BBB": RISER}},
        dict(RISK, max_total_positions=6), starting_cash=1500, spread_pct=0,
    )
    assert _entries(result) == 1  # only one $1000 position fits under equity
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


ET = ZoneInfo("America/New_York")


def hourly(closes: list[float], symbol_days: int | None = None) -> list[dict]:
    """One bar per day, stamped 11:05 New York — MID-SESSION, deliberately.

    Anchored on the clock rather than on `datetime.now()`'s hour because the
    replay now refuses stock entries outside 09:30-16:00 ET
    (backtest._in_trading_session). With six-hour steps from "now", whether the
    qualifying bar fell inside the session depended on what time of day the
    suite ran — green in the afternoon, red at 2am. See the twin in
    test_backtest_api.py."""
    days = symbol_days if symbol_days is not None else len(closes)
    start = datetime.now(timezone.utc) - timedelta(days=days)
    return [
        {"t": (start + timedelta(days=i))
              .astimezone(ET)
              .replace(hour=11, minute=5, second=0, microsecond=0)
              .astimezone(timezone.utc)
              .strftime("%Y-%m-%dT%H:%M:%SZ"),
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
    assert _entries(body) == 2
    assert fetch.await_count == 1  # both stock symbols fetched in one call
    total = round(sum(c["realized_pnl"] + c["unrealized_pnl"] for c in body["contributions"]), 2)
    assert total == body["net_pnl"]
    assert {c["strategy_id"] for c in body["contributions"]} == {a, b}


def test_portfolio_endpoint_404_on_unknown_strategy(client, configured):
    a = client.post("/api/strategies", json=_custom_strategy_payload("Alpha", "AAA")).json()["id"]
    r = client.post("/api/backtest/portfolio", json={"strategy_ids": [a, 999999], "days": 30})
    assert r.status_code == 404
