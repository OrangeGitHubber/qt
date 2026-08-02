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


# --- telling a mismatched comparison from a broken backtester ---------------


def test_a_symbol_outside_the_replayed_universe_is_flagged_as_such():
    """The failure that produced a screen of 20 identical "the backtest missed
    this" rows: the replay was pointed at a different set of names entirely, so
    it was never going to find them. That is a mismatched comparison, not a
    faulty backtester, and the two need completely different responses."""
    out = compare(
        [_live("AMC", "2026-07-20"), _live("MS", "2026-07-27")],
        _result(),
        replayed_symbols=["MS", "AIG", "ALL"],
    )
    by_symbol = {r["symbol"]: r for r in out["live_only"]}
    assert by_symbol["AMC"]["in_replayed_universe"] is False
    assert by_symbol["MS"]["in_replayed_universe"] is True
    assert out["decision"]["missed_outside_universe"] == 1


def test_without_a_known_universe_it_says_unknown_rather_than_guessing():
    """No universe supplied (an imported export, say) must not silently report
    every symbol as in-universe — that would hide the exact problem above."""
    out = compare([_live("AMC", "2026-07-20")], _result())
    assert out["live_only"][0]["in_replayed_universe"] is None
    assert out["decision"]["missed_outside_universe"] == 0


def test_the_replayed_universe_is_reported_back():
    """So the screen can name what it was actually pointed at instead of leaving
    you to infer it."""
    out = compare([], _result(), replayed_symbols=["msft", "MS"])
    assert out["replayed_symbols"] == ["MS", "MSFT"]


# --- exits nobody's strategy chose ------------------------------------------


def test_a_force_exit_still_counts_as_a_matched_entry():
    """You pressed sell. The ENTRY was still a genuine strategy decision, and
    dropping the whole trade would throw away a real data point."""
    out = compare(
        [_live("NVDA", "2026-05-05", exit_reason="force-closed by hand (market order)")],
        _result([_sim("NVDA", "2026-05-05", exit_reason="take-profit: +10%")]),
    )
    assert out["decision"]["matched"] == 1
    assert out["matched"][0]["exit_comparable"] is False


def test_a_force_exit_is_not_scored_as_an_exit_disagreement():
    """The replay had no way to know you clicked sell on a Tuesday. Counting it
    would report the exit logic as broken when it was never consulted."""
    out = compare(
        [_live("NVDA", "2026-05-05", exit_reason="force-closed by hand (market order)")],
        _result([_sim("NVDA", "2026-05-05", exit_reason="take-profit: +10%")]),
    )
    assert out["decision"]["same_exit_rule_pct"] is None   # nothing comparable to score
    assert out["decision"]["same_exit_day_pct"] is None
    assert out["decision"]["manual_exits"] == 1


def test_a_force_exit_never_pollutes_the_measured_trading_cost():
    """THE one that matters. The cost median is meant to be typed into the
    backtest's spread setting. A hand-closed trade's exit price differs from the
    rule-based one because it was a DIFFERENT DECISION, not because of slippage —
    letting it in would push every future backtest wrong on the strength of a
    button press."""
    forced = _live("AAA", "2026-05-05", entry=100.0, exit_price=200.0,
                   exit_reason="force-closed by hand (market order)")
    normal = _live("BBB", "2026-05-05", entry=100.0, exit_price=110.0)
    out = compare(
        [forced, normal],
        _result([
            _sim("AAA", "2026-05-05", entry=100.1, exit_price=110.0),   # 45% "delta"
            _sim("BBB", "2026-05-05", entry=100.1, exit_price=110.11),
        ]),
        assumed_spread_pct=0.1,
    )
    # Two entries and ONE exit compared — the forced exit is set aside.
    assert out["execution"]["fills_compared"] == 3
    # …so the suggestion stays in slippage territory instead of being dragged to 45%.
    assert out["execution"]["suggested_spread_pct"] < 0.5


def test_a_force_exit_is_kept_out_of_the_pnl_gap_too():
    """Its profit was decided by when you pressed the button, so it says nothing
    about which way the backtest leans."""
    forced = _live("AAA", "2026-05-05", pnl=1000.0,
                   exit_reason="force-closed by hand (market order)")
    normal = _live("BBB", "2026-05-05", pnl=10.0)
    out = compare(
        [forced, normal],
        _result([_sim("AAA", "2026-05-05", pnl=5.0), _sim("BBB", "2026-05-05", pnl=14.0)]),
    )
    assert out["execution"]["backtest_pnl_optimism_usd"] == 4.0  # 14 - 10, BBB only


def test_the_other_exits_no_strategy_chose_are_treated_the_same_way():
    """An account reset and a reconciliation are equally outside the replay's
    reach — the position was ended by something that isn't a rule."""
    for reason in (
        "manual liquidation (account reset)",
        "reconciled: position no longer held at broker",
        "force-closed by hand (shadow — no order was ever placed)",
    ):
        out = compare(
            [_live("NVDA", "2026-05-05", exit_reason=reason)],
            _result([_sim("NVDA", "2026-05-05", exit_reason="take-profit: +10%")]),
        )
        assert out["matched"][0]["exit_comparable"] is False, reason
        assert out["decision"]["manual_exits"] == 1, reason


def test_an_ordinary_rule_exit_is_still_compared():
    """The guard must not swallow the normal case it is protecting."""
    out = compare(
        [_live("NVDA", "2026-05-05", exit_reason="trailing stop: -5% from high")],
        _result([_sim("NVDA", "2026-05-05", exit_reason="trailing stop: -5% from high")]),
    )
    assert out["matched"][0]["exit_comparable"] is True
    assert out["decision"]["manual_exits"] == 0
    assert out["decision"]["same_exit_rule_pct"] == 100.0


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


def test_the_replay_uses_the_bar_size_the_strategy_needs(client, configured, monkeypatch):
    """It used to ask for daily bars regardless. A strategy needing intraday ones
    then rejected every entry, took no trades, and the report blamed the
    backtester for "missing" every real trade — when it had simply been handed a
    resolution it could not trade on."""
    from unittest.mock import AsyncMock, patch

    from qt.api import backtest as backtest_api
    from qt.broker.alpaca import AlpacaClient

    strat = _strategy_body()
    strat["params"]["entry"]["require_above_vwap"] = True   # intraday-only rule
    sid = client.post("/api/strategies", json=strat).json()["id"]

    from datetime import datetime, timedelta, timezone
    with session_scope() as s:
        s.add(Trade(
            strategy_id=sid, mode="paper", symbol="NVDA", asset_class="stock",
            status="closed", qty=10, notional=1000, entry_price=100.0, exit_price=110.0,
            pnl=100.0, entry_reason="gain 5%", exit_reason="take-profit: +10%",
            entry_at=datetime.now(timezone.utc) - timedelta(days=3),
            exit_at=datetime.now(timezone.utc) - timedelta(days=2),
        ))

    seen: dict = {}
    real_run = backtest_api.run

    async def spy(body, **kw):
        seen["timeframe"] = body.timeframe
        return await real_run(body, **kw)

    bars = [{"t": (datetime.now(timezone.utc) - timedelta(days=n)).strftime("%Y-%m-%dT%H:%M:%SZ"),
             "o": 100, "h": 100, "l": 100, "c": 100, "v": 1e6, "vw": 100} for n in (10, 9, 8)]
    monkeypatch.setattr("qt.api.fidelity.run", spy)
    with patch.object(AlpacaClient, "historical_bars", new=AsyncMock(return_value={"NVDA": bars})):
        r = client.post("/api/fidelity/compare", json={"strategy_id": sid, "days": 30})
    assert r.status_code == 200, r.text
    assert seen["timeframe"] == "15Min", "a VWAP strategy must not be replayed on daily bars"


# --- the config that produced the trades ------------------------------------


def test_no_config_change_reports_no_drift():
    """The good case, and it must stay quiet: an empty list is what tells you
    the comparison is apples to apples."""
    from qt.services.fidelity import config_drift

    cfg = {"universe": "scanner", "sleeve_usd": 500, "params": {"exit": {"stop_loss_pct": 4}}}
    assert config_drift(cfg, dict(cfg)) == []
    assert config_drift(None, cfg) == []   # nothing recorded — say nothing


def test_a_changed_universe_is_named():
    """Werner's case: trades made under one universe, replayed against another.
    The version snapshot is exactly where that shows up."""
    from qt.services.fidelity import config_drift

    drift = config_drift(
        {"universe": "scanner", "symbols": []},
        {"universe": "basket", "symbols": []},
    )
    assert {"field": "Universe", "then": "scanner", "now": "basket"} in drift


def test_sleeve_and_position_count_changes_are_named_too():
    """They don't change WHICH symbols qualify, but they change how many trades
    fit and how big each is — so the replay takes a different set."""
    from qt.services.fidelity import config_drift

    drift = config_drift(
        {"sleeve_usd": 500, "max_positions": 3, "sizing_usd": 100},
        {"sleeve_usd": 2000, "max_positions": 8, "sizing_usd": 250},
    )
    fields = {c["field"] for c in drift}
    assert {"Sleeve budget", "Max positions", "$ per trade"} <= fields


def test_a_changed_rule_inside_params_is_flattened_to_one_row():
    """A nested blob diff would be unreadable; one row per changed setting is
    what you can act on."""
    from qt.services.fidelity import config_drift

    drift = config_drift(
        {"params": {"exit": {"stop_loss_pct": 4, "trailing_stop_pct": 5}}},
        {"params": {"exit": {"stop_loss_pct": 6, "trailing_stop_pct": 5}}},
    )
    assert drift == [{"field": "Exit stop_loss_pct", "then": 4, "now": 6}]


def test_the_report_names_the_config_that_produced_the_trades(client, configured):
    """End to end: edit the strategy after it traded, and the comparison says so
    instead of quietly measuring today's settings against yesterday's history."""
    from datetime import datetime, timedelta, timezone
    from unittest.mock import AsyncMock, patch

    from qt.broker.alpaca import AlpacaClient
    from qt.models import StrategyConfigVersion

    sid = client.post("/api/strategies", json=_strategy_body()).json()["id"]
    with session_scope() as s:
        version = s.query(StrategyConfigVersion).filter_by(strategy_id=sid).one()
        s.add(Trade(
            strategy_id=sid, mode="paper", symbol="NVDA", asset_class="stock",
            status="closed", qty=10, notional=1000, entry_price=100.0, exit_price=110.0,
            pnl=100.0, entry_reason="gain 5%", exit_reason="take-profit: +10%",
            config_version_id=version.id,
            entry_at=datetime.now(timezone.utc) - timedelta(days=3),
            exit_at=datetime.now(timezone.utc) - timedelta(days=2),
        ))

    # Now widen the sleeve — exactly the kind of edit that changes what a replay
    # would do without changing which symbols qualify.
    edited = _strategy_body()
    edited["sleeve_usd"] = 50000
    client.put(f"/api/strategies/{sid}", json=edited)

    bars = [{"t": (datetime.now(timezone.utc) - timedelta(days=n)).strftime("%Y-%m-%dT%H:%M:%SZ"),
             "o": 100, "h": 100, "l": 100, "c": 100, "v": 1e6, "vw": 100} for n in (10, 9, 8)]
    with patch.object(AlpacaClient, "historical_bars", new=AsyncMock(return_value={"NVDA": bars})):
        body = client.post("/api/fidelity/compare", json={"strategy_id": sid, "days": 30}).json()

    assert body["config"]["versions_used"] == 1
    fields = {c["field"] for c in body["config"]["drift"]}
    assert "Sleeve budget" in fields, body["config"]["drift"]


def test_a_changed_basket_shows_up_even_though_the_strategy_never_changed(client, configured):
    """The gap Werner spotted: a strategy's config version records WHICH basket
    it uses, not who is in it. Edit the basket and the strategy's own version is
    byte-identical — so the drift check would find nothing and report "no
    configuration drift", which is a confident statement of something false."""
    from datetime import datetime, timedelta, timezone
    from unittest.mock import AsyncMock, patch

    from qt.broker.alpaca import AlpacaClient
    from qt.models import BasketVersion

    from qt.models import Basket, BasketItem, BasketVersion

    bid = client.post("/api/baskets", json={"name": "Fidelity Banking"}).json()["id"]
    client.post(f"/api/baskets/{bid}/items", json={"symbol": "NVDA", "asset_class": "stock"})

    strat = _strategy_body()
    strat.update({"universe": "basket", "basket_id": bid, "symbols": []})
    sid = client.post("/api/strategies", json=strat).json()["id"]

    with session_scope() as s:
        # Backdate the snapshot so the trade below lands after it.
        s.query(BasketVersion).filter_by(basket_id=bid).one().created_at = (
            datetime.now(timezone.utc) - timedelta(days=10)
        )
        s.add(Trade(
            strategy_id=sid, mode="paper", symbol="NVDA", asset_class="stock",
            status="closed", qty=10, notional=1000, entry_price=100.0, exit_price=110.0,
            pnl=100.0, entry_reason="gain 5%", exit_reason="take-profit: +10%",
            entry_at=datetime.now(timezone.utc) - timedelta(days=5),
            exit_at=datetime.now(timezone.utc) - timedelta(days=4),
        ))

    # The basket changes AFTER the trade — the strategy row is untouched.
    client.post(f"/api/baskets/{bid}/items", json={"symbol": "MSFT", "asset_class": "stock"})

    bars = [{"t": (datetime.now(timezone.utc) - timedelta(days=n)).strftime("%Y-%m-%dT%H:%M:%SZ"),
             "o": 100, "h": 100, "l": 100, "c": 100, "v": 1e6, "vw": 100} for n in (10, 9, 8)]
    with patch.object(AlpacaClient, "historical_bars", new=AsyncMock(return_value={"NVDA": bars, "MSFT": bars})):
        body = client.post("/api/fidelity/compare", json={"strategy_id": sid, "days": 30}).json()

    drift = {c["field"]: c for c in body["config"]["drift"]}
    # Leave the shared database as we found it — test_baskets.py asserts seeding
    # starts from an empty table.
    with session_scope() as s:
        s.query(BasketVersion).filter_by(basket_id=bid).delete()
        s.query(BasketItem).filter_by(basket_id=bid).delete()
        s.query(Basket).filter_by(id=bid).delete()

    assert "Basket members" in drift, body["config"]["drift"]
    assert "MSFT" in drift["Basket members"]["now"]   # named, not just counted
