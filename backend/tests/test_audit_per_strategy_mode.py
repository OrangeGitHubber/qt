"""Mode becomes PER-STRATEGY, so shadow, paper and live can run side by side.

Until now the engine ran in exactly one mode — a single `engine_mode` setting
applied to everything enabled — which makes "go live" an all-or-nothing switch
over every strategy at once. This account has 18 enabled strategies, several
named "tester".

    "no no, not everyone will be running two containers. I will because I'm
     doing testing and dev. by default you should be able to run paper next to
     live next to shadow"        — Werner, 2026-08-04

THE CENTRAL SAFETY PROPERTY is that the master switch is a CEILING, never a
floor: a strategy runs at the COOLER of (master, its own mode). That gives one
setting that stops the whole instance touching the broker, without editing 18
strategies one at a time. Get the direction wrong — a max() instead of a min() —
and the global kill switch silently becomes a global go-live. Most of this file
exists to pin that one direction down.

LIVE IS GATED ON CREDENTIALS EXISTING, not on a constant in the source. The API
refuses to set live while no live key pair is stored, because a strategy marked
live with no live credentials could not place a live order anyway — it would sit
there looking armed and trading nothing. A source-level flag was the first design
and was wrong in both directions: too hard to turn off (needs a deploy) and too
easy to turn on (one flip, for everyone). See test_audit_live_broker_routing.py
for the routing that gate protects.

MODE IS NOT PART OF `StrategyBody`, deliberately. It is the only field that
decides whether real money moves, and putting it on the ordinary save would let
any form write it — including the optimizer's "save as draft", which builds a
whole StrategyBody out of a parameter search.
"""

import pytest

from qt.db import session_scope
from qt.models import Strategy
from qt.services.engine import (
    MODE_RANK,
    STRATEGY_MODES,
    effective_mode,
)

_PARAMS = {
    "entry": {"min_day_gain_pct": 3.0, "require_above_vwap": True},
    "exit": {"trailing_stop_pct": 5, "stop_loss_pct": 4},
}


@pytest.fixture(autouse=True)
def _templates_seeded(db_session):
    """Guarantee this file's precondition rather than inheriting it. The test
    database is session-scoped with no per-test rollback and several other files
    clear the strategies table wholesale, so by the time this runs there may be
    no templates — which failed here as "no templates seeded" and looked like a
    seeding bug rather than shared state. The template suite carries the same
    fixture for the same reason."""
    from qt.services.starter_strategies import seed_starter_strategies

    seed_starter_strategies(db_session)
    db_session.commit()


def _create(client, name, **over):
    body = {
        "name": name, "asset_class": "stock", "universe": "custom",
        "symbols": ["AAA"], "preset": "custom", "params": _PARAMS,
        "sizing_usd": 100, "sleeve_usd": 1000, "max_positions": 3,
        "swing_mode": True, "ignore_regime": True,
    }
    body.update(over)
    r = client.post("/api/strategies", json=body)
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


# ── the ceiling ──────────────────────────────────────────────────────────────
def test_the_master_switch_cools_but_never_heats():
    """THE PROPERTY THE WHOLE DESIGN RESTS ON. Read the table in both
    directions: down the rows the master tightens, across it the strategy asks
    for more, and the answer is never hotter than either."""
    assert effective_mode("shadow", "live") == "shadow"    # master wins when cooler
    assert effective_mode("shadow", "paper") == "shadow"
    assert effective_mode("live", "paper") == "paper"      # strategy wins when cooler
    assert effective_mode("live", "shadow") == "shadow"
    assert effective_mode("paper", "live") == "paper"
    assert effective_mode("paper", "paper") == "paper"


def test_master_off_stops_everything():
    """The kill switch. No strategy setting may survive it."""
    for want in STRATEGY_MODES:
        assert effective_mode("off", want) == "off", want


def test_the_master_can_never_promote_a_shadow_strategy():
    """Stated on its own because it is the failure that spends money. If this
    ever returns anything but 'shadow', a strategy the user put in the safest
    mode is trading because of a GLOBAL setting they changed for other
    reasons."""
    for master in ("shadow", "paper", "live"):
        assert effective_mode(master, "shadow") == "shadow", master


def test_an_unknown_mode_on_either_side_is_off():
    """A vocabulary that drifts — a typo, a hand-edited row, a value from a
    later version — must not be guessed at. Every wrong guess here is a guess
    about whether real money moves."""
    for bad in ("", None, "LIVE!", "papers", "on", "true"):
        assert effective_mode(bad, "paper") == "off", bad
        assert effective_mode("paper", bad) == "off", bad


def test_mode_names_are_case_and_space_insensitive():
    """The value survives a JSON round trip and hand editing; ' Paper ' must
    not read as an unknown mode and silently disable a working strategy."""
    assert effective_mode(" PAPER ", "Paper") == "paper"


def test_the_rank_order_is_the_one_the_ceiling_relies_on():
    assert MODE_RANK["off"] < MODE_RANK["shadow"] < MODE_RANK["paper"] < MODE_RANK["live"]


def test_off_is_not_a_strategy_mode():
    """A strategy that should not run is DISABLED. Two independent ways to say
    "not running" is how one of them gets forgotten."""
    assert "off" not in STRATEGY_MODES
    assert set(STRATEGY_MODES) == {"shadow", "paper", "live"}


# ── what a new strategy gets ─────────────────────────────────────────────────
def test_a_new_strategy_starts_in_shadow(client):
    """The safe default. A strategy must be deliberately promoted and can never
    arrive at a hotter mode by omission."""
    sid = _create(client, "fresh")
    assert client.get("/api/strategies").json()
    with session_scope() as s:
        assert s.get(Strategy, sid).mode == "shadow"


def test_mode_cannot_be_set_through_the_ordinary_save(client):
    """The optimizer's "save as draft" builds a whole StrategyBody from a search
    result. If mode rode along on that body, a parameter search could promote a
    strategy into spending real money as a side effect of tuning a stop."""
    sid = _create(client, "sneaky", mode="live")
    with session_scope() as s:
        assert s.get(Strategy, sid).mode == "shadow"


def test_the_mode_is_reported(client):
    sid = _create(client, "reported")
    row = [x for x in client.get("/api/strategies").json() if x["id"] == sid][0]
    assert row["mode"] == "shadow"


# ── the promotion path ───────────────────────────────────────────────────────
def _set_mode(client, sid, mode, confirm=False):
    return client.post(f"/api/strategies/{sid}/mode",
                       json={"mode": mode, "confirm": confirm})


def test_heating_up_needs_confirmation(client):
    sid = _create(client, "promote me")
    r = _set_mode(client, sid, "paper")
    assert r.status_code == 428, r.text
    assert "simulated broker" in r.json()["detail"]
    with session_scope() as s:
        assert s.get(Strategy, sid).mode == "shadow", "must not have moved"


def test_confirmed_promotion_works(client):
    sid = _create(client, "promote confirmed")
    assert _set_mode(client, sid, "paper", confirm=True).status_code == 200
    with session_scope() as s:
        assert s.get(Strategy, sid).mode == "paper"


def test_cooling_down_never_asks(client):
    """Reaching for the brake must not be the thing that asks you an extra
    question. If this ever needs `confirm`, someone in a hurry leaves a
    strategy trading."""
    sid = _create(client, "cool me")
    _set_mode(client, sid, "paper", confirm=True)
    r = _set_mode(client, sid, "shadow")           # no confirm
    assert r.status_code == 200, r.text
    with session_scope() as s:
        assert s.get(Strategy, sid).mode == "shadow"


def test_live_is_refused_with_no_live_credentials(client):
    """Live is gated on CREDENTIALS EXISTING, not on a constant in the source.

    A hardcoded flag would be both too hard to turn off (needs a deploy) and too
    easy to turn on (one flip, for everyone). Werner's live keys are his to add
    and his to delete, and deleting them is the fastest route back to
    unreachable. The message has to say what is missing and where to fix it,
    because "not available" alone sends you to the source."""
    from qt.broker.factory import live_credentials_stored

    with session_scope() as s:
        assert live_credentials_stored(s) is False, "precondition: no live keys"
    sid = _create(client, "too eager")
    r = _set_mode(client, sid, "live", confirm=True)
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert "credentials" in detail and "Setup" in detail
    assert "does not start live trading" in detail, (
        "storing keys must not read as consent to trade live")
    with session_scope() as s:
        assert s.get(Strategy, sid).mode == "shadow"


def test_an_unknown_mode_is_refused(client):
    sid = _create(client, "nonsense mode")
    assert _set_mode(client, sid, "yolo", confirm=True).status_code == 422


def test_a_template_cannot_be_switched_between_modes(client):
    """Templates are inert by design. One carrying a hot mode would be cloned
    into a hot strategy by a single click."""
    templates = [s for s in client.get("/api/strategies").json() if s["template"]]
    assert templates, "no templates seeded — this guard would be vacuous"
    r = _set_mode(client, templates[0]["id"], "paper", confirm=True)
    # 400 is `_refuse_if_template`'s existing status for every other verb; this
    # endpoint reuses it rather than inventing a second one.
    assert r.status_code == 400, r.text
    assert "template" in r.json()["detail"]


def test_every_shipped_template_is_in_shadow(client):
    """Whatever the instance's global mode was at migration time. A template in
    paper or live is one click from being a live strategy."""
    for t in [s for s in client.get("/api/strategies").json() if s["template"]]:
        assert t["mode"] == "shadow", t["name"]


def test_setting_the_mode_it_already_has_is_a_no_op(client):
    """Idempotent, and specifically must not demand confirmation for a change
    that isn't one — a UI that re-sends state would otherwise 428 forever."""
    sid = _create(client, "same mode")
    assert _set_mode(client, sid, "shadow").status_code == 200


@pytest.mark.parametrize("mode", ["shadow", "paper"])
def test_the_move_is_recorded_in_the_audit_log(client, mode):
    """Every transition between modes is a consequential action and the audit
    log is the only place the sequence survives."""
    from qt.models import AuditLog

    sid = _create(client, f"audited {mode}")
    _set_mode(client, sid, mode, confirm=True)
    with session_scope() as s:
        hits = (
            s.query(AuditLog)
            .filter(AuditLog.category == "strategy",
                    AuditLog.message.like(f"%audited {mode}%mode%"))
            .all()
        )
        if mode != "shadow":  # shadow -> shadow is a no-op and logs nothing
            assert hits, "no audit row for the mode change"


# ── the promotion path demands a track record (stage 4) ──────────────────────
def _paper_history(sid: int, n: int, days_ago: int) -> None:
    """n closed PAPER trades, the earliest `days_ago` days back."""
    from datetime import datetime, timedelta, timezone

    from qt.models import Trade

    start = datetime.now(timezone.utc) - timedelta(days=days_ago)
    with session_scope() as s:
        for i in range(n):
            s.add(Trade(
                strategy_id=sid, mode="paper", symbol="AAA", asset_class="stock",
                qty=1, notional=100, status="closed", entry_price=100.0,
                exit_price=101.0, pnl=1.0, entry_at=start + timedelta(hours=i),
                exit_at=start + timedelta(hours=i + 1),
            ))


def _blocker(sid: int) -> str | None:
    from qt.api.strategies import live_promotion_blocker
    from qt.models import Strategy as S

    with session_scope() as s:
        return live_promotion_blocker(s, s.get(S, sid))


def test_a_brand_new_strategy_cannot_go_live(client):
    """The gate that matters most. Every serious bug found on 2026-08-10 —
    an exit that liquidated another strategy's lot, a P&L path that could not
    book a loss — surfaced only because trades were actually running. A config
    that has never placed an order has not tested the code it is about to trust
    with real money."""
    sid = _create(client, "untested")
    reason = _blocker(sid)
    assert reason and "closed paper trade" in reason


def test_enough_trades_over_enough_days_clears_it(client):
    from qt.api.strategies import LIVE_MIN_CLOSED_TRADES, LIVE_MIN_DAYS_RUNNING

    sid = _create(client, "seasoned")
    _paper_history(sid, LIVE_MIN_CLOSED_TRADES, LIVE_MIN_DAYS_RUNNING + 1)
    assert _blocker(sid) is None


def test_trades_alone_are_not_enough(client):
    """Twenty trades in an afternoon is one market condition, not a track
    record. The clock is a separate gate from the count."""
    from qt.api.strategies import LIVE_MIN_CLOSED_TRADES

    sid = _create(client, "busy afternoon")
    _paper_history(sid, LIVE_MIN_CLOSED_TRADES + 5, 0)
    reason = _blocker(sid)
    assert reason and "day(s)" in reason


def test_days_alone_are_not_enough(client):
    """A strategy enabled a month ago that never filled anything has proved
    nothing about the order path."""
    sid = _create(client, "idle month")
    _paper_history(sid, 2, 60)
    reason = _blocker(sid)
    assert reason and "closed paper trade" in reason


def test_shadow_history_does_not_count(client):
    """Shadow fills are ASSUMPTIONS — the execution layer never ran — so shadow
    history says nothing about whether this strategy's orders fill."""
    from datetime import datetime, timedelta, timezone

    from qt.models import Trade

    sid = _create(client, "shadow only")
    start = datetime.now(timezone.utc) - timedelta(days=60)
    with session_scope() as s:
        for i in range(50):
            s.add(Trade(
                strategy_id=sid, mode="shadow", symbol="AAA", asset_class="stock",
                qty=1, notional=100, status="closed", entry_price=100.0,
                exit_price=101.0, pnl=1.0, entry_at=start, exit_at=start,
            ))
    reason = _blocker(sid)
    assert reason and "closed paper trade" in reason


def test_the_gate_does_not_require_the_record_to_be_PROFITABLE(client):
    """Deliberate. Seven mechanisms measured this session beat nothing, so a
    profit filter would mostly select for luck — and "20 winning paper trades"
    is a number a bad strategy reaches by accident. This checks the config has
    been through the machinery, not that it is any good."""
    from datetime import datetime, timedelta, timezone

    from qt.api.strategies import LIVE_MIN_CLOSED_TRADES, LIVE_MIN_DAYS_RUNNING
    from qt.models import Trade

    sid = _create(client, "loser")
    start = datetime.now(timezone.utc) - timedelta(days=LIVE_MIN_DAYS_RUNNING + 1)
    with session_scope() as s:
        for i in range(LIVE_MIN_CLOSED_TRADES):
            s.add(Trade(
                strategy_id=sid, mode="paper", symbol="AAA", asset_class="stock",
                qty=1, notional=100, status="closed", entry_price=100.0,
                exit_price=50.0, pnl=-50.0, entry_at=start, exit_at=start,
            ))
    assert _blocker(sid) is None, "a losing record must still be allowed through"


def test_the_gate_runs_before_the_credential_check_is_reached(client):
    """Order of operations. With no live keys stored the endpoint 409s either
    way, so this pins that an untested strategy is told about its RECORD rather
    than being sent off to add credentials it will still not be able to use."""
    sid = _create(client, "no record no keys")
    detail = _set_mode(client, sid, "live", confirm=True).json()["detail"]
    assert "closed paper trade" in detail, detail
    assert "credentials are stored" in detail, "both blockers must be reported at once"
