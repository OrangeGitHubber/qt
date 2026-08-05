"""Shipped strategy templates: clone-only, and internally coherent.

Four styles came out of measuring why one strategy underperformed. The finding
was always the same shape — settings belonging to DIFFERENT styles stacked on
one strategy. A dip entry (RSI crossing up out of oversold) carried three trend
confirmations, each measured as near-incompatible with it, and the result took
four trades in three months.

So the templates are the lesson written down, and the tests below assert the two
things that make them useful:

  1. They stay INERT. Enable, edit and delete are all refused, so a template
     cannot drift into a half-configured live strategy. Clone is the only way in.
  2. They stay COHERENT. Each one's settings serve one thesis. The dip buyer must
     not acquire a MACD filter; the trend follower must not acquire a
     take-profit. Those are the specific mistakes that produced the templates,
     so they are asserted rather than trusted to review.
"""

import json

import pytest

from qt.models import Strategy
from qt.services.starter_strategies import (
    STARTER_STRATEGIES,
    seed_starter_strategies,
)


@pytest.fixture(autouse=True)
def _templates_seeded(db_session):
    """Guarantee this file's precondition instead of inheriting it.

    The test database is session-scoped with no per-test rollback, and a dozen
    other test files clear the strategies table wholesale to get a clean slate.
    That legitimately takes the templates seeded at init_db() with it, so by the
    time this file runs there may be none — which failed here as "0 == 4" and
    looked like a seeding bug rather than shared state."""
    seed_starter_strategies(db_session)
    db_session.commit()


def _templates(client) -> dict:
    return {s["name"]: s for s in client.get("/api/strategies").json() if s["template"]}


def _by(name_fragment: str) -> dict:
    return next(s for s in STARTER_STRATEGIES if name_fragment in s["name"])


# ── they exist, and they are marked ────────────────────────────────────────

def test_all_four_are_seeded(client):
    got = _templates(client)
    assert len(got) == 4, sorted(got)
    for spec in STARTER_STRATEGIES:
        assert spec["name"] in got


def test_they_are_seeded_disabled(client):
    assert all(not s["enabled"] for s in _templates(client).values())


def test_seeding_twice_creates_nothing_new(client, db_session):
    """Create-only and idempotent: a reboot must not duplicate them."""
    before = len(_templates(client))
    assert seed_starter_strategies(db_session) == 0
    db_session.commit()
    assert len(_templates(client)) == before


def test_a_users_own_strategy_with_the_same_name_is_not_adopted(client, db_session):
    """Matched on (name, template=True). A user strategy that happens to collide
    must never be mistaken for a shipped one — that would silently make their own
    strategy un-editable and un-enablable.

    The template row is removed first so seeding actually has work to do:
    without that, seed_starter_strategies short-circuits on the existing row and
    the filter under test is never reached. (Found by mutation — dropping the
    `template.is_(True)` clause passed the first version of this test.)
    """
    name = STARTER_STRATEGIES[0]["name"]
    shipped = (
        db_session.query(Strategy)
        .filter(Strategy.name == name, Strategy.template.is_(True))
        .one()
    )
    db_session.delete(shipped)
    mine = Strategy(
        name=name, asset_class="stock", universe="custom", symbols="[]",
        params=json.dumps({"entry": {}, "exit": {"stop_loss_pct": 4}}),
        template=False,
    )
    db_session.add(mine)
    db_session.commit()

    # The user's row must NOT satisfy the "already seeded" check.
    assert seed_starter_strategies(db_session) == 1
    db_session.commit()

    rows = [s for s in client.get("/api/strategies").json() if s["name"] == name]
    assert sorted(r["template"] for r in rows) == [False, True], rows
    # …and theirs is still theirs: editable, enablable, untouched.
    assert client.post(f"/api/strategies/{mine.id}/toggle").status_code == 200


# ── inert: the whole point ─────────────────────────────────────────────────

def _a_template_id(client) -> int:
    return next(iter(_templates(client).values()))["id"]


def test_a_template_cannot_be_enabled(client):
    """The requirement. A template that could be switched on would drift into a
    half-configured live strategy and stop being a reliable starting point."""
    tid = _a_template_id(client)
    resp = client.post(f"/api/strategies/{tid}/toggle")
    assert resp.status_code == 400
    assert "template" in resp.json()["detail"].lower()
    assert client.get("/api/strategies").json()

    still = next(s for s in client.get("/api/strategies").json() if s["id"] == tid)
    assert still["enabled"] is False


def test_a_template_cannot_be_edited(client):
    tid = _a_template_id(client)
    body = {
        "name": "hijacked", "asset_class": "stock", "universe": "custom",
        "symbols": ["AAPL"], "params": {"entry": {}, "exit": {"stop_loss_pct": 4}},
    }
    resp = client.put(f"/api/strategies/{tid}", json=body)
    assert resp.status_code == 400
    still = next(s for s in client.get("/api/strategies").json() if s["id"] == tid)
    assert still["name"] != "hijacked"


def test_a_template_cannot_be_deleted(client):
    tid = _a_template_id(client)
    assert client.delete(f"/api/strategies/{tid}").status_code == 400
    assert any(s["id"] == tid for s in client.get("/api/strategies").json())


def test_the_refusal_says_what_to_do_instead(client):
    """A 400 that only says no is a worse bug than the one it prevents."""
    detail = client.post(f"/api/strategies/{_a_template_id(client)}/toggle").json()["detail"]
    assert "clone" in detail.lower()


# ── cloning is the way in ──────────────────────────────────────────────────

def test_a_clone_is_an_ordinary_strategy(client):
    """A clone must carry the settings and NOT the restriction, or the templates
    are decorative."""
    src = _templates(client)[_by("Dip buyer")["name"]]
    body = {
        "name": "my dip buyer", "asset_class": src["asset_class"],
        "universe": src["universe"], "symbols": src["symbols"],
        "rank_by": src["rank_by"], "rank_enabled": src["rank_enabled"],
        "params": src["params"], "sizing_usd": src["sizing_usd"],
        "sleeve_usd": src["sleeve_usd"], "max_positions": src["max_positions"],
    }
    made = client.post("/api/strategies", json=body)
    assert made.status_code == 200, made.text
    clone = made.json()

    assert clone["template"] is False
    assert clone["params"]["entry"]["rsi_cross_above"] == 35
    # …and it really is unrestricted.
    assert client.post(f"/api/strategies/{clone['id']}/toggle").status_code == 200


# ── each template is coherent for its own style ────────────────────────────

def test_the_dip_buyer_has_no_trend_confirmations():
    """THE measured mistake. require_macd_bullish discarded 41% of this entry's
    signals and the survivors did worse; above-VWAP and a positive min-day-gain
    both mean "already up today", which is the opposite of the thesis."""
    entry = _by("Dip buyer")["params"]["entry"]
    assert entry["rsi_cross_above"] == 35
    assert entry["min_day_gain_pct"] == 0
    assert entry["require_above_vwap"] is False
    assert entry["require_macd_bullish"] is False


def test_the_dip_buyer_banks_profit_before_the_trail_gives_it_back():
    """Measured trades peaked at +17% and +20% and exited at +5% and +4%."""
    assert _by("Dip buyer")["params"]["exit"]["take_profit_pct"] > 0


def test_the_trend_follower_has_no_take_profit():
    """A ceiling defeats the thesis — this is the style that is supposed to own
    the stock that doubles."""
    assert _by("Trend follower")["params"]["exit"]["take_profit_pct"] == 0


def test_the_trend_follower_ranks_by_building_momentum_not_by_level():
    """Ranking by rsi puts the MOST OVERBOUGHT names first, by construction, and
    macd_strength is nearly as bad because the histogram peaks late in a move."""
    assert _by("Trend follower")["rank_by"] == "macd_slope"


def test_the_trend_follower_trails_wider_than_the_dip_buyer():
    """It has to breathe: a trail inside one average day sells every winner."""
    trend = _by("Trend follower")["params"]["atr"]["trail_mult"]
    dip = _by("Dip buyer")["params"]["atr"]["trail_mult"]
    assert trend > dip


def test_the_two_stock_styles_are_genuinely_opposite():
    """The templates only earn their keep if they disagree. If a future edit
    made them converge, this fails and someone has to think about why."""
    dip, trend = _by("Dip buyer")["params"]["entry"], _by("Trend follower")["params"]["entry"]
    assert dip.get("rsi_cross_above") and not trend.get("rsi_cross_above")
    assert trend.get("require_macd_bullish") and not dip.get("require_macd_bullish")


def test_the_intraday_template_turns_swing_mode_off():
    """CRITICAL: swing mode defers the soft exits to the DAY AFTER entry, which
    is fatal for a strategy that closes the same day."""
    spec = _by("Intraday")
    assert spec["swing_mode"] is False
    assert spec["params"]["exit"]["flatten_before_close"] is True
    assert spec["params"]["entry"]["entry_window_start"]


def test_the_dca_baseline_does_no_timing():
    spec = _by("DCA")
    assert spec["params"]["dca"]["interval_days"] > 0
    entry = spec["params"]["entry"]
    assert entry.get("min_day_gain_pct", 0) == 0
    assert not entry.get("require_macd_bullish")
    assert not entry.get("require_above_vwap")


@pytest.mark.parametrize("spec", STARTER_STRATEGIES, ids=lambda s: s["name"])
def test_every_template_explains_itself(spec):
    """The notes ARE the feature — a template without its reasoning is just
    another set of numbers to copy blindly."""
    notes = spec["notes"]
    assert len(notes) > 400, spec["name"]
    assert "WHAT THIS IS" in notes
    assert "EVIDENCE" in notes or "WHAT TO CHANGE" in notes
