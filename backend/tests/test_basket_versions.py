"""A basket's members are versioned, because a strategy's config version isn't
enough to reconstruct what it traded.

The strategy snapshot records WHICH basket it points at, never WHO is in it. Edit
the basket — add a symbol, drop one — and the strategy's own config version stays
byte-identical while the universe it trades has genuinely changed.

That gap is worse than having no record. The backtest-fidelity check compares the
config that produced a trade against the config being replayed; reading today's
membership it would find nothing different and report "no configuration drift",
which is a confident statement of something false.
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import inspect

from qt.api.baskets import members_at
from qt.db import engine, session_scope
from qt.models import BasketVersion


import pytest


@pytest.fixture(autouse=True)
def _clean_baskets():
    """Remove the baskets these tests create.

    They share one database with every other test file, and test_baskets.py
    asserts that seeding starts from an EMPTY basket table — so leaving rows
    behind fails a test that has nothing to do with this feature, in a way that
    only shows up when the whole suite runs."""
    yield
    from qt.models import Basket, BasketItem, BasketVersion

    with session_scope() as s:
        ids = [b.id for b in s.query(Basket).filter(Basket.name.like("Banking %"))]
        if ids:
            s.query(BasketVersion).filter(BasketVersion.basket_id.in_(ids)).delete(
                synchronize_session=False
            )
            s.query(BasketItem).filter(BasketItem.basket_id.in_(ids)).delete(
                synchronize_session=False
            )
            s.query(Basket).filter(Basket.id.in_(ids)).delete(synchronize_session=False)


_counter = iter(range(1, 9999))


def _basket(client) -> int:
    """A fresh, uniquely-named basket. These tests share one database, and the
    create endpoint rejects duplicate names — reusing "Banking" made every test
    after the first receive a 409 and fail on a missing id rather than on
    anything it was actually asserting."""
    name = f"Banking {next(_counter)}"
    r = client.post("/api/baskets", json={"name": name})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def test_the_versions_table_survived_the_migration(_db):
    assert "basket_versions" in set(inspect(engine).get_table_names())


def test_adding_a_symbol_snapshots_the_new_membership(client):
    bid = _basket(client)
    client.post(f"/api/baskets/{bid}/items", json={"symbol": "JPM", "asset_class": "stock"})
    with session_scope() as s:
        versions = s.query(BasketVersion).filter_by(basket_id=bid).all()
        assert len(versions) == 1
        assert "JPM" in versions[0].snapshot


def test_the_snapshot_is_taken_after_the_change_not_before(client):
    """Snapshotting before the flush would immortalise the state the edit was
    replacing — every version would be one step behind reality."""
    bid = _basket(client)
    client.post(f"/api/baskets/{bid}/items", json={"symbol": "JPM", "asset_class": "stock"})
    client.post(f"/api/baskets/{bid}/items", json={"symbol": "BAC", "asset_class": "stock"})
    with session_scope() as s:
        latest = (
            s.query(BasketVersion)
            .filter_by(basket_id=bid)
            .order_by(BasketVersion.version_no.desc())
            .first()
        )
    assert "JPM" in latest.snapshot and "BAC" in latest.snapshot


def test_removing_a_symbol_is_recorded_too(client):
    bid = _basket(client)
    client.post(f"/api/baskets/{bid}/items", json={"symbol": "JPM", "asset_class": "stock"})
    client.delete(f"/api/baskets/{bid}/items/stock/JPM")
    with session_scope() as s:
        latest = (
            s.query(BasketVersion)
            .filter_by(basket_id=bid)
            .order_by(BasketVersion.version_no.desc())
            .first()
        )
    assert "JPM" not in latest.snapshot


def test_membership_is_recoverable_as_of_a_moment(client):
    """THE point: what did this basket hold when that trade was made?"""
    bid = _basket(client)
    client.post(f"/api/baskets/{bid}/items", json={"symbol": "JPM", "asset_class": "stock"})
    with session_scope() as s:
        # Backdate the first snapshot so the two are unambiguously ordered.
        first = s.query(BasketVersion).filter_by(basket_id=bid).one()
        first.created_at = datetime.now(timezone.utc) - timedelta(days=10)
    client.post(f"/api/baskets/{bid}/items", json={"symbol": "BAC", "asset_class": "stock"})

    with session_scope() as s:
        back_then = members_at(s, bid, datetime.now(timezone.utc) - timedelta(days=5))
        today = members_at(s, bid, datetime.now(timezone.utc))
    assert [m["symbol"] for m in back_then] == ["JPM"]
    assert sorted(m["symbol"] for m in today) == ["BAC", "JPM"]


def test_a_basket_with_no_history_answers_unknown_not_todays_members(client):
    """A basket that predates versioning, or that nobody has edited since, has no
    snapshot. Returning today's members would be a confident answer to a question
    we cannot answer — and the fidelity report would then declare "no drift" on
    the strength of it."""
    bid = _basket(client)
    with session_scope() as s:
        assert members_at(s, bid, datetime.now(timezone.utc)) is None


def test_asking_before_the_first_snapshot_is_also_unknown(client):
    bid = _basket(client)
    client.post(f"/api/baskets/{bid}/items", json={"symbol": "JPM", "asset_class": "stock"})
    with session_scope() as s:
        assert members_at(s, bid, datetime.now(timezone.utc) - timedelta(days=30)) is None


def test_deleting_a_basket_takes_its_history_with_it(client):
    """Deletion is already blocked while a strategy points here, so once it's
    gone there is nothing left for the history to explain — and the foreign key
    would refuse the delete otherwise."""
    bid = _basket(client)
    client.post(f"/api/baskets/{bid}/items", json={"symbol": "JPM", "asset_class": "stock"})
    assert client.delete(f"/api/baskets/{bid}").status_code == 200
    with session_scope() as s:
        assert s.query(BasketVersion).filter_by(basket_id=bid).count() == 0
