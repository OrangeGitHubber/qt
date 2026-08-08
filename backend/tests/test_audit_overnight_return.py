"""Settings that force a same-day exit give up where the return actually is.

MEASURED, 2026-08-08, five large-cap US equities over 318 sessions (May 2025 -
Aug 2026, hourly bars from this app's own cache). Each session split into the
GAP (previous close -> next open, held while the market is shut) and the SESSION
(open -> close):

              $100 gap only    $100 session only    $100 buy & hold
    MSFT           118.96              98.82             115.90
    AAPL           114.15             128.83             149.91
    AMD            328.14             152.44             486.58
    AAL            165.51              96.09             158.87
    NVDA           198.23             101.27             197.09

On four of five, every point of gain arrived overnight and the session
contributed nothing or lost money. The overnight period was also the LESS
volatile of the two on four of five. So a rule that guarantees a flat book at
the close is not trading risk for return — it gives up more return than risk.

WHAT THIS FILE ASSERTS, and why each one is a real hazard rather than a style
opinion:

  1. Nothing SHIPS with a forced same-day exit except the one template whose
     entire thesis is intraday. A preset or template that quietly flattens is
     the dangerous case, because the user picked it from a dropdown for its
     entry rules and inherited an exit that deletes the overnight return.
  2. `max_holding_hours` under 18 is the same setting wearing different clothes.
     Entering at 10:00 with a 6-hour cap is a forced exit at 16:00 whether or
     not `flatten_before_close` is set, so both are checked together — a guard
     on one alone is walked around by the other.
  3. The one template that DOES flatten discloses the measured cost in its
     notes. It used to say the setting "guarantees no overnight gap risk", which
     is true and one-sided; the notes now carry the other half.
  4. The optimizer cannot switch either knob ON. A search that discovered
     "flatten before close" and wrote it back onto a swing strategy would be
     this bug arriving by machine.

CRYPTO IS EXCLUDED, deliberately. There is no session and therefore no gap, and
`evaluate_exit` already gates the swing deferral on `not is_crypto`. A holding
cap on a 24/7 instrument is an ordinary risk choice, not this mistake.
"""

import pytest

from qt.services.optimizer import _active_param_space
from qt.services.presets import PRESETS
from qt.services.starter_strategies import STARTER_STRATEGIES

# Below this, a cap set on entry forces an exit inside the same US session.
# 18h clears a 09:30 entry to the next morning's open with room to spare.
SAME_SESSION_HOURS = 18

INTRADAY_TEMPLATE = "Template · Intraday scanner rider"


def _exit_rules(cfg: dict) -> dict:
    return (cfg.get("params") or {}).get("exit") or {}


def _forces_same_day_exit(cfg: dict) -> bool:
    ex = _exit_rules(cfg)
    if ex.get("flatten_before_close"):
        return True
    cap = ex.get("max_holding_hours") or 0
    return 0 < float(cap) < SAME_SESSION_HOURS


# ── 1. what ships ────────────────────────────────────────────────────────────
@pytest.mark.parametrize("key", sorted(PRESETS))
def test_no_preset_forces_a_same_day_exit(key):
    """Presets are the dropdown a novice picks from. None of them is an intraday
    style, so none may carry an intraday exit."""
    cfg = PRESETS[key]
    assert not _forces_same_day_exit(cfg), (
        f"preset {key!r} forces a same-day exit: {_exit_rules(cfg)}")


def test_only_the_intraday_template_flattens():
    flatteners = [t["name"] for t in STARTER_STRATEGIES if _forces_same_day_exit(t)]
    assert flatteners == [INTRADAY_TEMPLATE], flatteners


def test_every_stock_template_that_holds_overnight_really_can():
    """The complement of the above, stated from the other side: a template that
    is not the intraday one must have no cap short enough to bite, INCLUDING the
    caps that are set. 120h and 240h are fine; 6h is not."""
    for t in STARTER_STRATEGIES:
        if t["name"] == INTRADAY_TEMPLATE or t.get("asset_class") == "crypto":
            continue
        cap = _exit_rules(t).get("max_holding_hours") or 0
        assert cap == 0 or float(cap) >= SAME_SESSION_HOURS, (
            f"{t['name']!r} caps holding at {cap}h — inside one session")


def test_the_crypto_carve_out_is_real_and_not_an_excuse():
    """This file exempts crypto, so the exemption has to mean something: the
    engine must genuinely treat crypto as sessionless. If that ever changes, the
    carve-out is hiding cases rather than describing one."""
    import inspect

    from qt.services import engine

    src = inspect.getsource(engine.evaluate_exit)
    assert "not is_crypto" in src, (
        "evaluate_exit no longer exempts crypto from the session logic — the "
        "crypto carve-out in this file is no longer justified")


# ── 2. the template that does flatten, discloses the cost ────────────────────
def _intraday_notes() -> str:
    got = [t for t in STARTER_STRATEGIES if t["name"] == INTRADAY_TEMPLATE]
    assert got, f"{INTRADAY_TEMPLATE!r} is no longer shipped"
    return got[0]["notes"]


def test_the_intraday_template_discloses_the_measured_cost():
    notes = _intraday_notes()
    assert "gap" in notes.lower() and "return" in notes.lower()
    # The actual numbers, not a vague warning. A reader has to be able to see
    # the size of what they are giving up without leaving the page.
    for figure in ("118.96", "98.82", "198.23", "101.27"):
        assert figure in notes, f"{figure} missing from the notes"


def test_the_notes_say_the_overnight_period_was_LESS_volatile():
    """The half that kills the natural defence of this setting.

    "I give up return to reduce risk" is a reasonable trade and would make the
    flatten defensible. It is not what was measured: overnight was the calmer of
    the two periods on four of five names, so the setting gives up more return
    than risk. Without these figures a reader can still tell themselves the
    comfortable story, which is why they are asserted separately from the return
    table above — that table alone is consistent with a fair risk trade.

    This replaced a weaker check that merely looked for the word "cost" anywhere
    in the notes. It survived its mutation: the notes discuss cost in three
    unrelated places, so the assertion was true no matter what the flatten
    section said."""
    notes = _intraday_notes()
    assert "volatil" in notes.lower()
    for figure in ("119bp", "135bp", "195", "250"):
        assert figure in notes, f"volatility figure {figure} missing"


def test_the_notes_say_it_does_not_apply_to_crypto():
    """The most likely misreading: carrying "never hold overnight" onto a crypto
    strategy, where there is no overnight."""
    notes = _intraday_notes().lower()
    assert "crypto" in notes


def test_the_notes_do_not_recommend_trading_the_gap_instead():
    """The measurement's second half. Gap capture is 318 round trips a year and
    loses to buy-and-hold on all five names after 3bp — a reader who takes only
    the first table away would build exactly the wrong thing."""
    notes = _intraday_notes().lower()
    assert "buy-and-hold" in notes or "buy and hold" in notes


# ── 3. the optimizer cannot switch either knob on ────────────────────────────
def _space_for(exit_rules: dict) -> dict:
    return _active_param_space({"params": {
        "entry": {"min_day_gain_pct": 3.0},
        "exit": {"trailing_stop_pct": 5.0, "stop_loss_pct": 4.0, **exit_rules},
    }})


def test_the_optimizer_never_searches_the_session_knobs():
    """A search that turned flattening ON would introduce this bug by machine,
    and would write the winner back onto the strategy."""
    space = _space_for({})
    assert "flatten_before_close" not in space
    assert "max_holding_hours" not in space


def test_the_optimizer_leaves_them_alone_even_when_already_set():
    """The `> 0` rule elsewhere in `_active_param_space` means "switched on, so
    tune it". These two must be exempt from that: tuning a 6h cap to 4h is
    still the wrong axis, and it is set by intent rather than by tuning."""
    space = _space_for({"max_holding_hours": 6, "flatten_before_close": True})
    assert "max_holding_hours" not in space
    assert "flatten_before_close" not in space


def test_the_optimizer_still_searches_something():
    """THE CONTROL. If `_active_param_space` returned nothing at all, every
    assertion above would pass while the optimizer was broken."""
    assert set(_space_for({})) >= {"trailing_stop_pct", "stop_loss_pct"}
