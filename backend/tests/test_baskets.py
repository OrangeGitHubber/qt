"""Basket model + seeding + CRUD API."""

from qt.db import session_scope
from qt.models import Basket, BasketItem
from qt.services.starter_baskets import STARTER_BASKETS, seed_starter_baskets


def test_starter_baskets_seeded_on_init(client):
    baskets = client.get("/api/baskets").json()
    names = {b["name"] for b in baskets}
    assert set(STARTER_BASKETS) <= names
    defense = next(b for b in baskets if b["name"] == "Defense")
    assert defense["builtin"] is True
    assert defense["count"] == len(STARTER_BASKETS["Defense"])
    assert {s["symbol"] for s in defense["symbols"]} == set(STARTER_BASKETS["Defense"])


def test_seed_is_idempotent():
    with session_scope() as s:
        before = s.query(Basket).count()
        created = seed_starter_baskets(s)  # baskets already exist → no-op
        after = s.query(Basket).count()
    assert created == 0
    assert before == after >= len(STARTER_BASKETS)


def test_seed_from_empty_creates_all():
    # Simulate a fresh DB: wipe baskets, reseed, confirm count, then restore.
    with session_scope() as s:
        s.query(BasketItem).delete()
        s.query(Basket).delete()
    with session_scope() as s:
        created = seed_starter_baskets(s)
    assert created == len(STARTER_BASKETS)
    with session_scope() as s:
        assert s.query(Basket).count() == len(STARTER_BASKETS)


def test_create_rename_delete_and_items(client):
    created = client.post("/api/baskets", json={"name": "My Theme"}).json()
    bid = created["id"]
    assert created["builtin"] is False and created["count"] == 0

    # duplicate name (case-insensitive) rejected
    assert client.post("/api/baskets", json={"name": "my theme"}).status_code == 409

    # add + dedup
    client.post(f"/api/baskets/{bid}/items", json={"symbol": "aapl", "asset_class": "stock"})
    again = client.post(f"/api/baskets/{bid}/items", json={"symbol": "AAPL", "asset_class": "stock"}).json()
    assert again["count"] == 1  # uppercased + deduped
    assert again["symbols"][0]["symbol"] == "AAPL"

    # remove
    removed = client.request(
        "DELETE", f"/api/baskets/{bid}/items/stock/AAPL"
    ).json()
    assert removed["count"] == 0

    # rename
    renamed = client.put(f"/api/baskets/{bid}", json={"name": "Renamed Theme"}).json()
    assert renamed["name"] == "Renamed Theme"

    # delete
    assert client.delete(f"/api/baskets/{bid}").status_code == 200
    assert client.delete(f"/api/baskets/{bid}").status_code == 404


def test_delete_blocked_while_a_strategy_uses_it(client):
    bid = client.post("/api/baskets", json={"name": "In Use"}).json()["id"]
    strat = {
        "name": "Basket strat",
        "asset_class": "stock",
        "universe": "basket",
        "basket_id": bid,
        "rank_by": "momentum_today",
        "top_n": 5,
        "params": {
            "entry": {"min_day_gain_pct": 0, "require_above_vwap": False,
                      "entry_window_start": None, "entry_window_end": None},
            "exit": {"trailing_stop_pct": 5, "stop_loss_pct": 4, "take_profit_pct": 0,
                     "max_holding_hours": 0, "flatten_before_close": False, "exit_below_vwap": False},
        },
        "sizing_usd": 200, "sleeve_usd": 1000, "max_positions": 3,
        "swing_mode": True, "ignore_regime": False,
    }
    sid = client.post("/api/strategies", json=strat).json()["id"]
    assert client.delete(f"/api/baskets/{bid}").status_code == 409
    client.delete(f"/api/strategies/{sid}")
    assert client.delete(f"/api/baskets/{bid}").status_code == 200
