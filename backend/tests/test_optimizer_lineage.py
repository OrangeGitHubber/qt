"""Repeated optimization spends the out-of-sample guarantee, silently.

The split is honest exactly once per slice of history. Search a strategy, save
the draft, search THAT draft over the same period, and the held-out portion is no
longer held out — the person at the keyboard is now selecting settings knowing
how they scored on it. After a few rounds that slice has effectively seen
thousands of configurations while the app keeps printing a confident number
beside them.

Nothing could see this happening, because each draft was just another strategy.
These tests cover the ancestry that makes it visible.
"""

from sqlalchemy import inspect

from qt.api.optimizer import optimizer_lineage
from qt.db import engine, session_scope
from qt.models import Strategy


def _strategy_body(name: str, **over) -> dict:
    body = {
        "name": name,
        "asset_class": "stock",
        "universe": "custom",
        "symbols": ["AAPL"],
        "preset": "custom",
        "params": {
            "entry": {"min_day_gain_pct": 3, "require_above_vwap": False},
            "exit": {"trailing_stop_pct": 5, "stop_loss_pct": 4, "take_profit_pct": 0},
        },
        "sizing_usd": 1000,
        "sleeve_usd": 1000,
        "max_positions": 1,
        "swing_mode": True,
        "ignore_regime": True,
    }
    body.update(over)
    return body


def test_the_provenance_columns_survived_the_migration(_db):
    cols = {c["name"] for c in inspect(engine).get_columns("strategies")}
    assert {"optimized_from_id", "optimized_days", "optimized_at"} <= cols


def test_a_hand_built_strategy_is_generation_one(client):
    """No ancestry, nothing spent — the out-of-sample number means what it says."""
    sid = client.post("/api/strategies", json=_strategy_body("hand built")).json()["id"]
    with session_scope() as s:
        assert optimizer_lineage(s, s.get(Strategy, sid), 180)["generation"] == 1


def test_saving_a_draft_records_where_it_came_from(client):
    parent = client.post("/api/strategies", json=_strategy_body("parent")).json()["id"]
    draft = client.post(
        "/api/strategies",
        json=_strategy_body("parent (search draft)", optimized_from_id=parent, optimized_days=180),
    ).json()["id"]
    with session_scope() as s:
        row = s.get(Strategy, draft)
        assert row.optimized_from_id == parent
        assert row.optimized_days == 180
        assert row.optimized_at is not None  # stamped server-side, not by the client


def test_optimizing_a_draft_reports_the_next_generation(client):
    parent = client.post("/api/strategies", json=_strategy_body("gen0")).json()["id"]
    draft = client.post(
        "/api/strategies",
        json=_strategy_body("gen1", optimized_from_id=parent, optimized_days=180),
    ).json()["id"]
    with session_scope() as s:
        lineage = optimizer_lineage(s, s.get(Strategy, draft), 180)
    assert lineage["generation"] == 2
    assert lineage["same_window"] == 1
    assert lineage["root"] == "gen0"


def test_a_long_chain_counts_every_ancestor(client):
    """Three saves deep is where the reported figure has drifted furthest from
    anything independent."""
    ids = [client.post("/api/strategies", json=_strategy_body("root")).json()["id"]]
    for gen in range(1, 4):
        ids.append(
            client.post(
                "/api/strategies",
                json=_strategy_body(f"gen{gen}", optimized_from_id=ids[-1], optimized_days=180),
            ).json()["id"]
        )
    with session_scope() as s:
        lineage = optimizer_lineage(s, s.get(Strategy, ids[-1]), 180)
    assert lineage["generation"] == 4
    assert lineage["same_window"] == 3
    assert lineage["root"] == "root"


def test_a_different_day_count_is_still_counted_as_a_previous_search(client):
    """Changing 180 days to 200 is NOT fresh data — every window ends today, so
    the shorter one is contained in the longer one. The generation still counts;
    only `same_window` distinguishes them, so the UI can say which it was."""
    parent = client.post("/api/strategies", json=_strategy_body("p")).json()["id"]
    draft = client.post(
        "/api/strategies",
        json=_strategy_body("d", optimized_from_id=parent, optimized_days=200),
    ).json()["id"]
    with session_scope() as s:
        lineage = optimizer_lineage(s, s.get(Strategy, draft), 180)
    assert lineage["generation"] == 2      # still a repeat search
    assert lineage["same_window"] == 0     # ...but of a different span
    assert lineage["windows"] == [200]


def test_a_deleted_ancestor_ends_the_chain_instead_of_breaking_it(client):
    """Strategies get deleted. A dangling parent id must stop the walk, not raise
    — the count is then a floor, which is the honest direction to be wrong in."""
    parent = client.post("/api/strategies", json=_strategy_body("doomed")).json()["id"]
    draft = client.post(
        "/api/strategies",
        json=_strategy_body("orphan", optimized_from_id=parent, optimized_days=180),
    ).json()["id"]
    client.delete(f"/api/strategies/{parent}")
    with session_scope() as s:
        lineage = optimizer_lineage(s, s.get(Strategy, draft), 180)
    assert lineage["generation"] == 2
    assert lineage["root"] is None


def test_a_cycle_cannot_hang_the_walk(client):
    """Nothing creates a cycle today, but the walk follows user-editable rows and
    an infinite loop here would hang the request that starts every search."""
    a = client.post("/api/strategies", json=_strategy_body("a")).json()["id"]
    b = client.post(
        "/api/strategies", json=_strategy_body("b", optimized_from_id=a, optimized_days=180)
    ).json()["id"]
    with session_scope() as s:
        s.get(Strategy, a).optimized_from_id = b  # a -> b -> a
        s.get(Strategy, a).optimized_days = 180
    with session_scope() as s:
        lineage = optimizer_lineage(s, s.get(Strategy, b), 180)
    assert lineage["generation"] >= 2  # terminated at all is the point
