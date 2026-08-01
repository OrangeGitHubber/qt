"""Per-strategy freeform notes.

Somewhere to keep your own reasoning next to the strategy it's about. The engine
never reads it — which is exactly why editing one must NOT create a config
version. Versions exist so every trade points at the settings that produced it;
minting one for a note would put a "v124" in the journal for a change that
altered nothing.
"""

from qt.db import session_scope
from qt.models import Strategy, StrategyConfigVersion, Trade


def _strategy(**over) -> dict:
    body = {
        "name": "notes test", "asset_class": "stock", "universe": "custom",
        "symbols": ["AAPL"], "preset": "custom",
        "params": {
            "entry": {"min_day_gain_pct": 2, "require_above_vwap": False,
                      "entry_window_start": None, "entry_window_end": None},
            "exit": {"trailing_stop_pct": 5, "stop_loss_pct": 4, "take_profit_pct": 0,
                     "max_holding_hours": 0, "flatten_before_close": False, "exit_below_vwap": False},
        },
        "sizing_usd": 1000, "sleeve_usd": 5000, "max_positions": 3,
        "swing_mode": True, "ignore_regime": True,
    }
    body.update(over)
    return body


def _get(client, sid: int) -> dict:
    """Fetch BY ID. Indexing [0] assumed this was the only strategy in the DB —
    true alone, false in the suite, where another test's row can sort first."""
    rows = [r for r in client.get("/api/strategies").json() if r["id"] == sid]
    assert rows, f"strategy {sid} not found"
    return rows[0]


def _versions(sid: int) -> int:
    with session_scope() as s:
        return s.query(StrategyConfigVersion).filter(StrategyConfigVersion.strategy_id == sid).count()


def _cleanup():
    with session_scope() as s:
        s.query(Trade).delete()
        s.query(StrategyConfigVersion).delete()
        s.query(Strategy).delete()


def test_notes_round_trip(client):
    note = "Tested 1.5x ATR over 500d — beat SPY but lost to buy&hold.\nTry a wider trailing stop next."
    sid = client.post("/api/strategies", json=_strategy(notes=note)).json()["id"]
    assert _get(client, sid)["notes"] == note
    _cleanup()


def test_editing_only_the_notes_does_not_create_a_config_version(client):
    """THE point. A note changes no behaviour, so the trade history must not
    gain a version that claims otherwise."""
    body = _strategy(notes="first thoughts")
    sid = client.post("/api/strategies", json=body).json()["id"]
    baseline = _versions(sid)

    r = client.put(f"/api/strategies/{sid}", json={**body, "notes": "second thoughts"})
    assert r.status_code == 200
    assert r.json()["notes"] == "second thoughts"
    assert _versions(sid) == baseline, "a note edit minted a config version"
    _cleanup()


def test_changing_a_real_setting_still_creates_one(client):
    """The counterpart — the versioning that keeps the journal honest must not
    have been weakened to achieve the above."""
    body = _strategy(notes="unchanged")
    sid = client.post("/api/strategies", json=body).json()["id"]
    baseline = _versions(sid)

    changed = {**body}
    changed["params"] = {**body["params"], "exit": {**body["params"]["exit"], "stop_loss_pct": 9}}
    assert client.put(f"/api/strategies/{sid}", json=changed).status_code == 200
    assert _versions(sid) == baseline + 1, "a real config change did NOT create a version"
    _cleanup()


def test_a_setting_and_a_note_changed_together_creates_exactly_one(client):
    body = _strategy(notes="before")
    sid = client.post("/api/strategies", json=body).json()["id"]
    baseline = _versions(sid)

    changed = {**body, "notes": "after"}
    changed["params"] = {**body["params"], "exit": {**body["params"]["exit"], "stop_loss_pct": 7}}
    client.put(f"/api/strategies/{sid}", json=changed)
    assert _versions(sid) == baseline + 1
    _cleanup()


def test_notes_are_optional_and_default_empty(client):
    """Existing strategies predate the column; they must not 422 on save."""
    body = _strategy()
    body.pop("notes", None)
    sid = client.post("/api/strategies", json=body).json()["id"]
    assert _get(client, sid)["notes"] == ""
    assert client.put(f"/api/strategies/{sid}", json=body).status_code == 200
    _cleanup()
