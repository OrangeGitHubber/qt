"""Comparing what really happened against what the replay says would have.

The point of the feature is to stop a backtest being taken on trust, so its own
arithmetic had better be trustworthy. The cases that matter are the ones where a
naive diff would mislead:

  - a position still open at the window's end is a real decision, not a miss;
  - a trade a RAIL refused is not a backtest error;
  - two stop-losses at slightly different prices are the same rule, not a
    disagreement;
  - and a small sample must say so rather than print a confident percentage.
"""

from qt.services.fidelity import compare


def _live(symbol, day, entry=100.0, exit_price=110.0, exit_day="2026-05-06",
          pnl=10.0, status="closed", exit_reason="take-profit: +10%", entry_reason="gain 5%"):
    return {"symbol": symbol, "entry_day": day, "exit_day": exit_day, "entry_price": entry,
            "exit_price": exit_price, "pnl": pnl, "status": status,
            "entry_reason": entry_reason, "exit_reason": exit_reason}


def _sim(symbol, day, entry=100.0, exit_price=110.0, exit_day="2026-05-06",
         pnl=10.0, exit_reason="take-profit: +10%"):
    return {"symbol": symbol, "entry_day": day, "exit_day": exit_day, "entry_price": entry,
            "exit_price": exit_price, "pnl": pnl, "exit_reason": exit_reason}


def _result(trades=None, open_positions=None):
    return {"trade_list": trades or [], "open_positions": open_positions or []}


def test_the_same_trade_on_both_sides_is_matched():
    out = compare([_live("NVDA", "2026-05-05")], _result([_sim("NVDA", "2026-05-05")]))
    assert out["decision"]["matched"] == 1
    assert not out["live_only"] and not out["backtest_only"]


def test_a_trade_only_the_engine_took_is_a_miss_by_the_backtest():
    """The replay's view of that day was wrong — usually missing bars."""
    out = compare([_live("NVDA", "2026-05-05")], _result())
    assert [r["symbol"] for r in out["live_only"]] == ["NVDA"]
    assert out["decision"]["missed_by_backtest"] == 1


def test_a_trade_only_the_backtest_took_is_flagged_as_invented():
    out = compare([], _result([_sim("NVDA", "2026-05-05")]))
    assert [r["symbol"] for r in out["backtest_only"]] == ["NVDA"]
    assert out["decision"]["invented_by_backtest"] == 1


def test_a_trade_a_rail_refused_is_not_counted_against_the_backtest():
    """THE distinction that makes the report usable. The engine wanted this
    trade and a rail said no; the replay starts from a different state, so it
    can legitimately have room where live had none. Filing that as 'the backtest
    invented a trade' would bury the real mismatches in noise."""
    rejected = _live("NVDA", "2026-05-05", status="rejected",
                     entry_reason="wanted to buy (gain 5%) but daily loss cap hit")
    out = compare([rejected], _result([_sim("NVDA", "2026-05-05")]))
    assert not out["backtest_only"]
    assert len(out["rails_blocked"]) == 1
    assert "daily loss cap" in out["rails_blocked"][0]["blocked_by"]
    # …and it must not drag the match rate down either.
    assert out["decision"]["invented_by_backtest"] == 0


def test_a_position_still_open_at_the_end_counts_as_a_decision():
    """It entered — that IS the decision under test. Ignoring open positions
    would report every held winner as a trade the backtest missed."""
    out = compare(
        [_live("NVDA", "2026-05-05", exit_price=None, exit_day=None, pnl=None, status="open")],
        _result(open_positions=[{"symbol": "NVDA", "entry_day": "2026-05-05", "entry_price": 100.0}]),
    )
    assert out["decision"]["matched"] == 1
    assert out["decision"]["missed_by_backtest"] == 0


def test_the_fill_difference_is_measured_and_signed():
    """Positive = the backtest got the better price, i.e. it flatters itself.
    The sign is the whole point: it says which way every result is biased."""
    out = compare(
        [_live("NVDA", "2026-05-05", entry=100.0, exit_price=110.0)],
        _result([_sim("NVDA", "2026-05-05", entry=99.0, exit_price=111.0)]),
    )
    m = out["matched"][0]
    assert m["entry_delta_pct"] == -1.0   # bought 1% cheaper in the replay
    assert m["exit_delta_pct"] == pytest_approx(0.9091)


def pytest_approx(v):  # tiny local helper so the intent reads inline
    class _A:
        def __eq__(self, other):
            return abs(other - v) < 0.001
    return _A()


def test_the_same_rule_at_a_different_price_is_not_a_mismatch():
    """Reasons are prose with numbers in them. Comparing whole sentences would
    call two identical stop-outs a disagreement."""
    out = compare(
        [_live("NVDA", "2026-05-05", exit_reason="stop-loss: -4.10% <= -4%")],
        _result([_sim("NVDA", "2026-05-05", exit_reason="stop-loss: -4.32% <= -4%")]),
    )
    assert out["matched"][0]["exit_reason_matches"] is True
    assert out["decision"]["same_exit_rule_pct"] == 100.0


def test_a_genuinely_different_exit_rule_is_reported():
    out = compare(
        [_live("NVDA", "2026-05-05", exit_reason="trailing stop: -5% from high")],
        _result([_sim("NVDA", "2026-05-05", exit_reason="take-profit: +10%")]),
    )
    assert out["matched"][0]["exit_reason_matches"] is False


def test_leaving_on_a_different_day_is_a_decision_difference():
    out = compare(
        [_live("NVDA", "2026-05-05", exit_day="2026-05-06")],
        _result([_sim("NVDA", "2026-05-05", exit_day="2026-05-09")]),
    )
    assert out["matched"][0]["exit_day_matches"] is False
    assert out["decision"]["same_exit_day_pct"] == 0.0


def test_the_match_rate_counts_the_backtests_own_inventions():
    """Two of three decisions agree. Scoring only against live trades would let
    a replay that invents freely still claim 100%."""
    out = compare(
        [_live("AAA", "2026-05-05"), _live("BBB", "2026-05-05")],
        _result([_sim("AAA", "2026-05-05"), _sim("CCC", "2026-05-05")]),
    )
    assert out["decision"]["matched"] == 1
    assert out["decision"]["match_rate_pct"] == round(1 / 3 * 100, 1)


def test_a_thin_sample_says_so_instead_of_printing_a_confident_number():
    out = compare([_live("NVDA", "2026-05-05")], _result([_sim("NVDA", "2026-05-05")]))
    assert out["decision"]["enough_to_judge"] is False
    assert out["execution"]["enough_to_judge"] is False


def test_a_full_sample_clears_the_bar():
    live = [_live(f"S{n}", "2026-05-05") for n in range(30)]
    sim = _result([_sim(f"S{n}", "2026-05-05") for n in range(30)])
    out = compare(live, sim)
    assert out["decision"]["enough_to_judge"] is True


def test_the_suggested_cost_comes_from_the_measured_fills():
    """The payoff: a number to type into the backtest form, derived from real
    fills rather than guessed."""
    live = [_live(f"S{n}", "2026-05-05", entry=100.0, exit_price=110.0) for n in range(3)]
    sim = _result([_sim(f"S{n}", "2026-05-05", entry=99.8, exit_price=110.22) for n in range(3)])
    out = compare(live, sim, assumed_spread_pct=0.1)
    assert out["execution"]["assumed_spread_pct"] == 0.1
    assert out["execution"]["suggested_spread_pct"] == 0.2   # median |delta| per side
    assert out["execution"]["fills_compared"] == 6


def test_no_matches_suggests_nothing_rather_than_zero():
    """A suggestion built on no data is worse than no suggestion: 0% spread
    would silently make every future backtest more optimistic."""
    out = compare([], _result())
    assert out["execution"]["suggested_spread_pct"] is None
    assert out["execution"]["backtest_pnl_optimism_usd"] is None
    assert out["decision"]["match_rate_pct"] is None


def test_the_pnl_gap_shows_which_way_the_backtest_leans():
    live = [_live("AAA", "2026-05-05", pnl=10.0), _live("BBB", "2026-05-05", pnl=-5.0)]
    sim = _result([_sim("AAA", "2026-05-05", pnl=14.0), _sim("BBB", "2026-05-05", pnl=-3.0)])
    out = compare(live, sim)
    assert out["execution"]["backtest_pnl_optimism_usd"] == 6.0  # (14-3) - (10-5)


# --- the endpoint ----------------------------------------------------------

import pytest

from qt import security
from qt.broker.alpaca import SECRET_KEY_ID, SECRET_KEY_SECRET
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


def _strategy_body(asset_class="stock"):
    return {
        "name": f"fid {asset_class}", "asset_class": asset_class, "universe": "custom",
        "symbols": ["NVDA"], "preset": "custom",
        "params": {
            "entry": {"min_day_gain_pct": 3, "require_above_vwap": False},
            "exit": {"trailing_stop_pct": 5, "stop_loss_pct": 4, "take_profit_pct": 0},
        },
        "sizing_usd": 1000, "sleeve_usd": 5000, "max_positions": 3,
        "swing_mode": True, "ignore_regime": True,
    }


def test_comparing_with_no_real_trades_explains_itself(client, configured):
    """The commonest first experience: a strategy that hasn't traded yet. A bare
    'no data' would leave you guessing which of the three fixes applies."""
    sid = client.post("/api/strategies", json=_strategy_body()).json()["id"]
    r = client.post("/api/fidelity/compare", json={"strategy_id": sid, "days": 30})
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert "nothing to compare" in detail
    assert "import" in detail and "widen" in detail


def test_a_paper_comparison_says_its_costs_are_not_measurable(client, configured):
    """Paper fills are simulated by the broker. Decision fidelity is fully
    testable there; execution fidelity is not, and quietly presenting simulated
    slippage as measured cost would be the worst kind of wrong — a confident
    number nobody can act on."""
    from datetime import datetime, timedelta, timezone
    from unittest.mock import AsyncMock, patch

    from qt.broker.alpaca import AlpacaClient

    sid = client.post("/api/strategies", json=_strategy_body()).json()["id"]
    with session_scope() as s:
        s.add(Trade(
            strategy_id=sid, mode="paper", symbol="NVDA", asset_class="stock",
            status="closed", qty=10, notional=1000, entry_price=100.0, exit_price=110.0,
            pnl=100.0, entry_reason="gain 5%", exit_reason="take-profit: +10%",
            entry_at=datetime.now(timezone.utc) - timedelta(days=3),
            exit_at=datetime.now(timezone.utc) - timedelta(days=2),
        ))

    bars = [{"t": (datetime.now(timezone.utc) - timedelta(days=n)).strftime("%Y-%m-%dT%H:%M:%SZ"),
             "o": 100, "h": 100, "l": 100, "c": 100, "v": 1e6, "vw": 100} for n in (10, 9, 8)]
    with patch.object(AlpacaClient, "historical_bars", new=AsyncMock(return_value={"NVDA": bars})):
        r = client.post("/api/fidelity/compare", json={"strategy_id": sid, "days": 30, "mode": "paper"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["execution_is_measurable"] is False
    assert body["mode"] == "paper"
    # The live trade is there to be accounted for one way or another.
    assert body["decision"]["live_trades"] == 1


def test_the_export_carries_trades_and_no_secrets(client, configured):
    """It leaves one machine and lands on another, so it must be boring to lose."""
    from datetime import datetime, timedelta, timezone

    sid = client.post("/api/strategies", json=_strategy_body()).json()["id"]
    with session_scope() as s:
        s.add(Trade(
            strategy_id=sid, mode="live", symbol="NVDA", asset_class="stock",
            status="closed", qty=10, notional=1000, entry_price=100.0, exit_price=110.0,
            pnl=100.0, entry_reason="gain 5%", exit_reason="take-profit: +10%",
            entry_at=datetime.now(timezone.utc) - timedelta(days=3),
            exit_at=datetime.now(timezone.utc) - timedelta(days=2),
        ))
    body = client.get("/api/fidelity/export?days=30&mode=live").json()
    assert len(body["trades"]) == 1
    row = body["trades"][0]
    assert row["symbol"] == "NVDA" and row["entry_price"] == 100.0
    blob = str(body).lower()
    for leaked in ("secret", "api_key", "account_number", "password", "token"):
        assert leaked not in blob


def test_an_imported_export_is_compared_instead_of_local_history(client, configured):
    """The prod -> dev path: this instance has never traded, and still produces a
    report from the other machine's trades."""
    from datetime import datetime, timedelta, timezone
    from unittest.mock import AsyncMock, patch

    from qt.broker.alpaca import AlpacaClient

    sid = client.post("/api/strategies", json=_strategy_body()).json()["id"]
    day = (datetime.now(timezone.utc) - timedelta(days=3)).strftime("%Y-%m-%d")
    imported = [{
        "symbol": "NVDA", "status": "closed", "entry_day": day, "exit_day": day,
        "entry_price": 100.0, "exit_price": 110.0, "pnl": 100.0,
        "entry_reason": "gain 5%", "exit_reason": "take-profit: +10%",
    }]
    bars = [{"t": (datetime.now(timezone.utc) - timedelta(days=n)).strftime("%Y-%m-%dT%H:%M:%SZ"),
             "o": 100, "h": 100, "l": 100, "c": 100, "v": 1e6, "vw": 100} for n in (10, 9, 8)]
    with patch.object(AlpacaClient, "historical_bars", new=AsyncMock(return_value={"NVDA": bars})):
        r = client.post("/api/fidelity/compare", json={
            "strategy_id": sid, "days": 30, "imported_trades": imported})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["imported"] is True
    assert body["decision"]["live_trades"] == 1
