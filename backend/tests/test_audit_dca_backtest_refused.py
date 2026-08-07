"""A DCA sleeve's backtest replayed a different strategy, silently.

`dca_sleeve` is a shipped preset — one click — and nothing anywhere refused it.
What the two sides do with the same config is not a detail:

  LIVE  `_consider_entries` sees `dca.interval_days > 0`, hands the strategy to
        `_consider_dca_entries` and `continue`s. `evaluate_entry` is NEVER
        called, so every EntryRules field is dead, the fixed symbol list is
        bought on a calendar, and each buy is an independent LOT — several lots
        of one symbol can be open at once.

  REPLAY  no such branch exists. It evaluates the momentum rules live never
        reads (the preset ships `+3% day · above VWAP`) and never buys on the
        schedule that is the entire point of the sleeve.

So the number was not approximate, it described a different strategy. The
optimizer made it worse: it would happily TUNE entry rules that do nothing.

WHY IT REFUSES RATHER THAN SIMULATES. The lots are the blocker. Both bar loops
store open positions as `dict[str, Trade]` keyed BY SYMBOL, so the replay cannot
hold two lots of one symbol at all — every exposure sum, exit walk and rail
check reads that shape. Simulating DCA is therefore a structural change, not a
missing branch, and a wrong number is worse than no number. Refusing is the
honest half; the cadence is still owed.

The guard lives at the API, not in the page, so the Optimizer cannot walk around
it — which is exactly what a page-level check would have allowed.
"""

import json

import pytest

from qt import security
from qt.api.backtest import DCA_UNSUPPORTED, dca_interval_days, refuse_if_dca
from qt.broker.alpaca import SECRET_KEY_ID, SECRET_KEY_SECRET
from qt.db import session_scope
from qt.models import Strategy, StrategyConfigVersion, Trade
from fastapi import HTTPException


@pytest.fixture()
def configured(client):
    """`require_client` is a DEPENDENCY, so it resolves before the handler body
    and returns 409 "Alpaca is not configured yet" — which would make every
    endpoint assertion below pass for the wrong reason, or fail for one. Stored
    keys are the only way to reach the guard at all."""
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

_MOMENTUM = {
    "entry": {"min_day_gain_pct": 3.0, "require_above_vwap": True,
              "entry_window_start": None, "entry_window_end": None},
    "exit": {"trailing_stop_pct": 5, "stop_loss_pct": 4, "take_profit_pct": 0,
             "max_holding_hours": 0, "flatten_before_close": False,
             "exit_below_vwap": False},
}


def _make(client, name, *, interval_days=None):
    params = json.loads(json.dumps(_MOMENTUM))
    if interval_days is not None:
        params["dca"] = {"interval_days": interval_days}
    r = client.post("/api/strategies", json={
        "name": name, "asset_class": "stock", "universe": "custom",
        "symbols": ["AAA", "BBB"], "preset": "custom", "params": params,
        "sizing_usd": 100, "sleeve_usd": 1000, "max_positions": 3,
        "swing_mode": True, "ignore_regime": True,
    })
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


# ── the predicate ────────────────────────────────────────────────────────────
@pytest.mark.parametrize("params,expected", [
    ({}, 0),
    ({"dca": {}}, 0),
    ({"dca": {"interval_days": 0}}, 0),
    ({"dca": {"interval_days": 7}}, 7),
    ({"dca": {"interval_days": "7"}}, 7),      # a JSON round-trip can stringify it
    ({"dca": None}, 0),
    ({"dca": {"interval_days": None}}, 0),
    ({"dca": {"interval_days": "nonsense"}}, 0),
])
def test_the_dca_test_matches_the_engines(params, expected):
    """`> 0` is the same condition `_consider_entries` branches on. A malformed
    blob must read as "not a DCA sleeve" rather than raise — every other params
    reader in this codebase tolerates junk, and a crash here would take down a
    backtest for an unrelated reason."""
    assert dca_interval_days(params) == expected


# ── the refusal ──────────────────────────────────────────────────────────────
def test_a_dca_sleeve_backtest_is_refused(client, configured):
    sid = _make(client, "dca sleeve", interval_days=7)
    r = client.post("/api/backtest", json={"strategy_id": sid, "days": 30})
    assert r.status_code == 400, r.status_code
    assert "DCA sleeve" in r.json()["detail"]


def test_the_refusal_explains_what_would_have_been_wrong(client, configured):
    """A bare "unsupported" would send you to the docs. The message has to name
    the divergence, because the number it refuses to produce would have LOOKED
    perfectly reasonable."""
    sid = _make(client, "dca explain", interval_days=14)
    detail = client.post("/api/backtest", json={"strategy_id": sid, "days": 30}).json()["detail"]
    assert "evaluate_entry" in detail
    assert "calendar" in detail
    assert "lot" in detail.lower()


def test_an_ordinary_strategy_is_not_refused(client, configured):
    """THE CONTROL. Refuse too much and every momentum backtest dies — a far
    worse outcome than the bug being fixed. 400 is the only status this guard
    may produce; anything else here is a different failure."""
    sid = _make(client, "momentum")
    r = client.post("/api/backtest", json={"strategy_id": sid, "days": 30})
    assert r.status_code != 400, r.text


def test_a_zero_interval_is_not_a_dca_sleeve(client, configured):
    """`interval_days: 0` is how the field looks when the sleeve is switched
    off, and the live engine takes the momentum path for it. The guard has to
    agree or it locks users out of ordinary strategies that once had a DCA
    block."""
    sid = _make(client, "dca off", interval_days=0)
    r = client.post("/api/backtest", json={"strategy_id": sid, "days": 30})
    assert r.status_code != 400, r.text


# ── the optimizer, which would TUNE the dead rules ───────────────────────────
def test_the_optimizer_is_refused_too(client, configured):
    """The worse case. A single wrong backtest is one wrong number; a search
    over rules the engine never reads spends hours tuning nothing, then writes
    the winner back onto the strategy."""
    sid = _make(client, "dca optimise", interval_days=7)
    r = client.post("/api/optimizer", json={"strategy_id": sid, "days": 30,
                                          "iterations": 5})
    assert r.status_code == 400, f"{r.status_code}: {r.text[:200]}"
    assert "DCA sleeve" in r.json()["detail"]


def test_the_guard_sits_below_the_page(client, configured):
    """Enforced at the API rather than in Backtest.tsx, so a second caller —
    the Optimizer, a sweep, a script — cannot route around it. Both endpoints
    give the same answer for the same strategy."""
    sid = _make(client, "dca both", interval_days=7)
    a = client.post("/api/backtest", json={"strategy_id": sid, "days": 30})
    b = client.post("/api/optimizer", json={"strategy_id": sid, "days": 30,
                                          "iterations": 5})
    assert a.status_code == b.status_code == 400
    assert a.json()["detail"] == b.json()["detail"]


def test_the_preset_this_protects_is_still_shipped(client):
    """If `dca_sleeve` ever stops shipping, this guard is dead weight and should
    be reconsidered rather than left behind. Reachable in one click is what made
    the silence serious."""
    presets = client.get("/api/strategies/presets").json()
    flat = json.dumps(presets).lower()
    assert "dca" in flat, presets


def test_the_guard_itself_raises_a_400(client):
    """The predicate and the endpoint are tested above; this pins the middle —
    that `refuse_if_dca` raises 400 with the explaining message, independently
    of any dependency ordering."""
    with pytest.raises(HTTPException) as got:
        refuse_if_dca({"dca": {"interval_days": 7}})
    assert got.value.status_code == 400
    assert got.value.detail == DCA_UNSUPPORTED
    refuse_if_dca({"dca": {"interval_days": 0}})   # must not raise
    refuse_if_dca({})                              # must not raise


def test_the_basket_sweep_is_refused_too(client, configured):
    """The third caller. It used to check "are there baskets to sweep" FIRST, so
    a DCA sleeve got 422 "no baskets" on an account without them and would have
    sailed through on an account with them — a guard that depended on unrelated
    state. The strategy is validated before the environment now."""
    sid = _make(client, "dca sweep", interval_days=7)
    # days >= 90 is SweepBody's own floor, and pydantic validates before the
    # handler runs — a 30 here fails with 422 and never reaches the guard.
    r = client.post("/api/optimizer/sweep", json={"strategy_id": sid, "days": 90,
                                                  "iterations": 5})
    assert r.status_code == 400, f"{r.status_code}: {r.text[:200]}"
    assert "DCA sleeve" in r.json()["detail"]
