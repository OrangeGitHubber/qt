"""`optimized_at` must mean an optimizer search produced this row.

It was stamped whenever `optimized_from_id` was set — and a CLONE sets that too,
because the parent link is what hangs a copy in its family tree on the
Strategies page. So every copy carried an optimisation date for a search that
never ran, and `optimizer_lineage` reads that field straight into the ancestry
chain it shows you.

The clone already knew better about the other half of the pair. It nulls
`optimized_days` deliberately, with the comment: "it records the length of the
parameter search that produced a strategy, and no search produced this one.
Copying it would have the row claim a backtest it never had." The timestamp now
obeys the same rule, so the two fields can no longer disagree about whether a
search happened.
"""

from qt.db import session_scope
from qt.models import Strategy

_PARAMS = {
    "entry": {"min_day_gain_pct": 3.0, "require_above_vwap": True,
              "entry_window_start": None, "entry_window_end": None},
    "exit": {"trailing_stop_pct": 5, "stop_loss_pct": 4, "take_profit_pct": 0,
             "max_holding_hours": 0, "flatten_before_close": False,
             "exit_below_vwap": False},
}


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


def _stamp(sid):
    with session_scope() as s:
        return s.get(Strategy, sid).optimized_at


def test_a_hand_built_strategy_has_no_optimisation_date(client):
    assert _stamp(_create(client, "hand built")) is None


def test_a_clone_has_no_optimisation_date(client):
    """THE BUG. A clone sets `optimized_from_id` to join the family tree and
    leaves `optimized_days` null because no search produced it. The timestamp
    used to be stamped anyway."""
    parent = _create(client, "parent")
    child = _create(client, "parent (copy)", optimized_from_id=parent,
                    optimized_days=None)
    assert _stamp(child) is None, (
        "a copy was given an optimisation date for a search that never ran")


def test_an_optimizer_result_does_have_one(client):
    """THE CONTROL. Suppress too much and the lineage panel loses the dates it
    exists to show — a worse outcome than the bug being fixed."""
    parent = _create(client, "searched parent")
    child = _create(client, "searched child", optimized_from_id=parent,
                    optimized_days=90)
    assert _stamp(child) is not None


def test_the_two_optimizer_fields_agree(client):
    """The invariant, stated directly: `optimized_at` is set exactly when
    `optimized_days` is. Either both describe a search or neither does."""
    parent = _create(client, "agreement parent")
    for days in (None, 30, 180):
        sid = _create(client, f"agreement {days}", optimized_from_id=parent,
                      optimized_days=days)
        with session_scope() as s:
            row = s.get(Strategy, sid)
            assert (row.optimized_at is not None) == (row.optimized_days is not None), (
                f"optimized_days={row.optimized_days} but "
                f"optimized_at={row.optimized_at}")


def test_days_without_a_parent_still_stamps_nothing(client):
    """A search always has a parent to have searched FROM, so days-without-parent
    is a malformed row rather than a second way of being optimised. It must not
    acquire a date by the back door."""
    assert _stamp(_create(client, "orphan days", optimized_days=60)) is None


def test_the_lineage_chain_reports_the_missing_date_as_missing(client):
    """What the field feeds. `optimizer_lineage` walks the parent links and
    reports each ancestor's date; a clone in that chain must show no date rather
    than the moment it was copied."""
    from qt.api.optimizer import optimizer_lineage

    parent = _create(client, "lineage parent")
    child = _create(client, "lineage copy", optimized_from_id=parent,
                    optimized_days=None)
    with session_scope() as s:
        got = optimizer_lineage(s, s.get(Strategy, child), days=90)
    assert got["generation"] == 2, got
    assert [c for c in got.get("windows", [])] == [], got
