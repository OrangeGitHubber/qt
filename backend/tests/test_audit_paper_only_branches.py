""""Not paper" used to mean "do not touch the broker". With live, it means the opposite.

Step three of live trading, and the one my own notes flagged as the sharp edge:

    "Audit every `mode == "paper"` branch. Today 'not paper' silently means 'do
     not touch the broker' — a safe default now, exactly the wrong one once live
     exists. Grep for it; there are several."

Four sites, and two of them would have broken live outright:

  execution.open_trade     `if mode == "paper"` around the order submission.
                           A live strategy would have journalled its entry and
                           placed NOTHING.

  execution.close_trade    the same test around the SELL. Worse: a live position
                           would have fallen straight past every order-placing
                           branch to the journal update, so QT would mark the
                           trade closed, book a P&L, and leave the position open
                           at the broker with no stop and nobody watching it. A
                           live position with no way to close it is the worst
                           single outcome in this codebase.

  jobs.reconcile_open_trades  read the one global mode and reconciled that book
                           with one client, so LIVE POSITIONS WOULD NEVER HAVE
                           BEEN RECONCILED — the only book where an unnoticed
                           orphan costs real money would have been the only one
                           nobody checked.

  api.broker.liquidate     selected every non-shadow trade and closed them all
                           through the paper client. The panic button would have
                           errored on each live position and then marked it
                           closed anyway, reporting a flat book while the live
                           positions stayed open.

THE FIX IS A NAMED PREDICATE, `places_orders`, written as an inclusion list
rather than `!= "shadow"`: a fifth mode added later must default to NOT trading
and have to be added deliberately.
"""

from unittest.mock import AsyncMock, patch

import pytest

from qt import security
from qt.broker.alpaca import (
    LIVE_SECRET_KEY_ID,
    LIVE_SECRET_KEY_SECRET,
    SECRET_KEY_ID,
    SECRET_KEY_SECRET,
    AlpacaClient,
)
from qt.broker.factory import places_orders
from qt.db import session_scope
from qt.models import Strategy, Trade
from qt.settings_service import set_setting


# ── the predicate ────────────────────────────────────────────────────────────
def test_paper_and_live_place_orders_shadow_does_not():
    assert places_orders("paper") is True
    assert places_orders("live") is True
    assert places_orders("shadow") is False


def test_an_unknown_mode_does_not_place_orders():
    """An inclusion list, not `!= "shadow"`. A mode added later must default to
    not trading and have to be added here on purpose."""
    for bad in ("", None, "off", "prod", "real", "LIVE!", "papers"):
        assert places_orders(bad) is False, bad


def test_the_predicate_normalises():
    assert places_orders(" LIVE ") is True
    assert places_orders("Paper") is True


# ── the two execution branches ───────────────────────────────────────────────
def _sources() -> str:
    import inspect

    from qt.services import execution

    return inspect.getsource(execution)


def test_neither_execution_branch_still_tests_for_paper():
    """Pinned as source text because the alternative is a live integration test
    against a broker. Both sites are the difference between a live strategy
    trading and a live strategy silently doing nothing."""
    src = _sources()
    assert 'if mode == "paper":' not in src, "entry submission is still paper-only"
    assert 'if trade.mode == "paper":' not in src, "exit submission is still paper-only"
    assert src.count("places_orders(") >= 2


def test_shadow_still_places_no_order():
    """THE CONTROL. Widen this too far and shadow — the mode whose entire purpose
    is to touch nothing — starts sending orders to a broker."""
    assert places_orders("shadow") is False


# ── reconciliation covers every order-placing book ───────────────────────────
@pytest.fixture()
def both_books():
    with session_scope() as s:
        security.set_secret(s, SECRET_KEY_ID, "paper-id")
        security.set_secret(s, SECRET_KEY_SECRET, "paper-secret")
        security.set_secret(s, LIVE_SECRET_KEY_ID, "live-id")
        security.set_secret(s, LIVE_SECRET_KEY_SECRET, "live-secret")
        set_setting(s, "engine_mode", "live")
    yield
    with session_scope() as s:
        set_setting(s, "engine_mode", "off")
        for k in (SECRET_KEY_ID, SECRET_KEY_SECRET,
                  LIVE_SECRET_KEY_ID, LIVE_SECRET_KEY_SECRET):
            security.delete_secret(s, k)


async def _reconcile_modes() -> list[str]:
    from qt.services import jobs

    seen: list[str] = []

    async def _apply(session, client, mode):
        seen.append(mode)
        return []

    with patch("qt.services.reconcile.apply_reconciliation", _apply):
        await jobs.reconcile_open_trades()
    return seen


async def test_reconciliation_covers_the_live_book(both_books):
    """The book where an unnoticed orphan costs real money must not be the one
    nobody checks."""
    assert sorted(await _reconcile_modes()) == ["live", "paper"]


async def test_reconciliation_skips_a_book_with_no_credentials():
    with session_scope() as s:
        security.set_secret(s, SECRET_KEY_ID, "paper-id")
        security.set_secret(s, SECRET_KEY_SECRET, "paper-secret")
        security.delete_secret(s, LIVE_SECRET_KEY_ID)
        security.delete_secret(s, LIVE_SECRET_KEY_SECRET)
        set_setting(s, "engine_mode", "live")
    try:
        assert await _reconcile_modes() == ["paper"]
    finally:
        with session_scope() as s:
            set_setting(s, "engine_mode", "off")
            security.delete_secret(s, SECRET_KEY_ID)
            security.delete_secret(s, SECRET_KEY_SECRET)


async def test_reconciliation_never_touches_shadow(both_books):
    """Shadow places no order, so there is no broker state to compare against."""
    assert "shadow" not in await _reconcile_modes()


async def test_the_master_switch_stops_reconciliation_reaching_a_broker(both_books):
    """The ceiling applies here too: "stop touching the broker" must not still
    be calling list_positions on two accounts."""
    with session_scope() as s:
        set_setting(s, "engine_mode", "shadow")
    assert await _reconcile_modes() == []


async def test_master_off_reconciles_nothing(both_books):
    with session_scope() as s:
        set_setting(s, "engine_mode", "off")
    assert await _reconcile_modes() == []


# ── the master switch is the last gate ───────────────────────────────────────
def test_switching_the_master_to_live_needs_confirmation(client, both_books):
    r = client.post("/api/engine/mode", json={"mode": "live"})
    assert r.status_code == 428, r.text
    assert "real money" in r.json()["detail"], r.json()["detail"]


def test_the_master_cannot_go_live_without_credentials(client):
    with session_scope() as s:
        security.delete_secret(s, LIVE_SECRET_KEY_ID)
        security.delete_secret(s, LIVE_SECRET_KEY_SECRET)
    r = client.post("/api/engine/mode", json={"mode": "live", "confirm": True})
    assert r.status_code == 409, r.text
    assert "credentials" in r.json()["detail"]


def test_paper_still_needs_confirmation(client):
    """THE CONTROL for the rewrite: generalising the check must not lose the
    confirmation paper already had."""
    r = client.post("/api/engine/mode", json={"mode": "paper"})
    assert r.status_code == 428, r.text


def test_switching_off_never_needs_confirmation(client):
    """Off and shadow are the brakes. They must stay one click."""
    for mode in ("off", "shadow"):
        assert client.post("/api/engine/mode", json={"mode": mode}).status_code == 200, mode


# ── the panic button flattens every book ─────────────────────────────────────
def test_liquidation_closes_each_book_with_its_own_client(client, both_books):
    """The panic button must mean everything. A version that silently skipped the
    live account would be worse than one that did nothing, because it reports
    success — and the loop marks trades closed whether or not the close worked,
    so QT would show a flat book with live positions still open."""
    from qt.broker.alpaca import LIVE_BASE_URL, PAPER_BASE_URL

    with session_scope() as s:
        strat = s.query(Strategy).first()
        assert strat is not None, "need a strategy to hang trades on"
        for mode in ("paper", "live", "shadow"):
            s.add(Trade(
                strategy_id=strat.id, mode=mode, symbol={"paper":"LQP","live":"LQL","shadow":"LQS"}[mode],
                asset_class="stock", qty=1, notional=100, status="open",
                entry_price=100.0, entry_order_id=f"o-{mode}",
            ))

    closed_through: list[tuple[str, str]] = []

    async def _list_positions(self):
        """Each ACCOUNT holds only its own position. This is the whole point: the
        paper account does not hold LQL, so a paper-client close of it would
        error — and the endpoint marks trades closed regardless of errors."""
        sym = "LQL" if self.base_url == LIVE_BASE_URL else "LQP"
        return [{"symbol": sym, "qty": "1", "current_price": "101"}]

    async def _close_position(self, symbol, qty=None):
        closed_through.append((symbol, self.base_url))
        return {}

    try:
        with patch.object(AlpacaClient, "list_positions", _list_positions), \
                patch.object(AlpacaClient, "close_position", _close_position):
            r = client.post("/api/broker/liquidate", json={"include_orphans": False})
        assert r.status_code == 200, r.text
        assert r.json()["books"] == ["paper", "live"], r.json()

        # THE ASSERTION THAT MATTERS, and the one this test was missing: each
        # position was closed through ITS OWN account. Without it the test passed
        # with every non-shadow trade closed through the paper client, because
        # the end state — both rows closed — looks identical either way.
        assert dict(closed_through) == {"LQP": PAPER_BASE_URL, "LQL": LIVE_BASE_URL}, (
            closed_through)

        with session_scope() as s:
            rows = {t.mode: t.status for t in s.query(Trade)
                    .filter(Trade.symbol.like("LQ%")).all()}
        assert rows["paper"] == "closed" and rows["live"] == "closed"
        assert rows["shadow"] == "open", "shadow is hypothetical — leave it alone"
    finally:
        with session_scope() as s:
            s.query(Trade).filter(Trade.symbol.like("LQ%")).delete(
                synchronize_session=False)


def test_liquidation_skips_a_book_with_no_credentials(client):
    with session_scope() as s:
        security.set_secret(s, SECRET_KEY_ID, "paper-id")
        security.set_secret(s, SECRET_KEY_SECRET, "paper-secret")
        security.delete_secret(s, LIVE_SECRET_KEY_ID)
        security.delete_secret(s, LIVE_SECRET_KEY_SECRET)
    try:
        with patch.object(AlpacaClient, "list_positions", AsyncMock(return_value=[])):
            r = client.post("/api/broker/liquidate", json={"include_orphans": False})
        assert r.status_code == 200, r.text
        assert r.json()["books"] == ["paper"]
    finally:
        with session_scope() as s:
            security.delete_secret(s, SECRET_KEY_ID)
            security.delete_secret(s, SECRET_KEY_SECRET)
