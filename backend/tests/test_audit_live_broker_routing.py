"""Live orders go to the live host with the live keys, and nothing else does.

Step two of live trading. Step one made mode a per-strategy attribute; this is
the part that makes a live strategy actually reach a different broker account.

THE ONE INVARIANT. The live credentials appear with the live host and nowhere
else, and the live host appears with the live credentials and nowhere else.
Cross-wiring is not symmetric in consequence:

  * paper keys against the LIVE host        -> 401, loud, harmless
  * a PAPER-mode client against the live host -> real orders, placed silently,
                                                 while every label says paper

So the tests below check the pairing in both directions rather than just
asserting that live works.

SHADOW USES THE PAPER CREDENTIALS ON PURPOSE. Shadow places no order, but it does
read quotes, bars and the clock, and those calls need a key. Handing it the live
pair would leave the live account one bug away from the mode whose entire purpose
is to touch nothing.

EQUITY IS PER ACCOUNT. Position sizing and the exposure rail are both fractions
of account equity, so running the live pass on the paper account's equity would
size real money against imaginary money. The tick reads each mode's own account.

WHO ENTERS THE KEYS. Werner does, through the setup endpoint, which verifies them
against Alpaca's LIVE host before storing — pasting the paper pair in is rejected
by Alpaca rather than by a guess of ours about key formats. Assistants never
handle them.
"""

from unittest.mock import AsyncMock, patch

import pytest

from qt import security
from qt.broker.alpaca import (
    LIVE_BASE_URL,
    LIVE_SECRET_KEY_ID,
    LIVE_SECRET_KEY_SECRET,
    PAPER_BASE_URL,
    SECRET_KEY_ID,
    SECRET_KEY_SECRET,
    AlpacaClient,
    AlpacaError,
)
from qt.broker.factory import broker_target, get_client, live_credentials_stored
from qt.db import session_scope
from qt.services.engine import ENGINE_MODES, live_available


# ── the routing table, pure ──────────────────────────────────────────────────
def test_live_is_the_only_mode_that_reaches_the_live_host():
    """Read as: no mode other than `live` may name the live URL."""
    for mode in ("shadow", "paper"):
        assert broker_target(mode)[2] == PAPER_BASE_URL, mode
    assert broker_target("live")[2] == LIVE_BASE_URL


def test_the_live_credentials_are_used_by_nothing_else():
    """The other direction of the same invariant, and the one that costs money
    if it slips: a paper-mode client built from live keys."""
    for mode in ("shadow", "paper"):
        key_name, secret_name, _ = broker_target(mode)
        assert key_name == SECRET_KEY_ID, mode
        assert secret_name == SECRET_KEY_SECRET, mode


def test_live_uses_the_live_credentials():
    key_name, secret_name, url = broker_target("live")
    assert (key_name, secret_name, url) == (
        LIVE_SECRET_KEY_ID, LIVE_SECRET_KEY_SECRET, LIVE_BASE_URL)


def test_the_two_hosts_are_actually_different():
    """A guard against the whole file being vacuous. If someone pointed
    LIVE_BASE_URL at the paper host "for testing", every assertion above would
    still pass and live trading would silently be paper trading."""
    assert PAPER_BASE_URL != LIVE_BASE_URL
    assert "paper" in PAPER_BASE_URL and "paper" not in LIVE_BASE_URL


def test_an_unknown_mode_gets_no_broker_at_all():
    """Not a fallback to paper. A mode nobody recognises must not silently
    acquire the ability to trade, and 'defaults to paper' hides a typo until it
    is a live one."""
    for bad in ("", None, "off", "LIVE!", "prod", "real"):
        assert broker_target(bad) is None, bad


def test_mode_names_are_normalised():
    assert broker_target(" LIVE ") == broker_target("live")


# ── building the client ──────────────────────────────────────────────────────
@pytest.fixture()
def paper_only():
    """Paper keys stored, live keys deliberately absent — the normal state."""
    with session_scope() as s:
        security.set_secret(s, SECRET_KEY_ID, "paper-id")
        security.set_secret(s, SECRET_KEY_SECRET, "paper-secret")
        security.delete_secret(s, LIVE_SECRET_KEY_ID)
        security.delete_secret(s, LIVE_SECRET_KEY_SECRET)
    yield
    with session_scope() as s:
        security.delete_secret(s, SECRET_KEY_ID)
        security.delete_secret(s, SECRET_KEY_SECRET)
        security.delete_secret(s, LIVE_SECRET_KEY_ID)
        security.delete_secret(s, LIVE_SECRET_KEY_SECRET)


@pytest.fixture()
def both_keys(paper_only):
    with session_scope() as s:
        security.set_secret(s, LIVE_SECRET_KEY_ID, "live-id")
        security.set_secret(s, LIVE_SECRET_KEY_SECRET, "live-secret")
    yield


def test_no_live_keys_means_no_live_client(paper_only):
    """THE GATE. With no live credentials the live book cannot trade, whatever
    any strategy's mode says — `get_client` returns None and every caller
    already treats None as "not configured, do nothing"."""
    with session_scope() as s:
        assert get_client(s, "live") is None
        assert get_client(s, "paper") is not None
        assert live_credentials_stored(s) is False
        assert live_available(s) is False


def test_a_half_configured_live_account_is_not_available(paper_only):
    """One key without the other is not a usable account, and reporting it as
    available would let a strategy be promoted and then quietly fail to trade."""
    with session_scope() as s:
        security.set_secret(s, LIVE_SECRET_KEY_ID, "live-id")
    with session_scope() as s:
        assert live_credentials_stored(s) is False
        assert get_client(s, "live") is None


def test_the_live_client_carries_live_keys_and_the_live_host(both_keys):
    with session_scope() as s:
        c = get_client(s, "live")
    assert c.key_id == "live-id" and c.key_secret == "live-secret"
    assert c.base_url == LIVE_BASE_URL


def test_the_paper_client_is_unaffected_by_live_keys_existing(both_keys):
    """Storing live keys must not change what paper does. If it did, adding
    credentials would silently repoint a running paper account."""
    with session_scope() as s:
        c = get_client(s, "paper")
    assert c.key_id == "paper-id"
    assert c.base_url == PAPER_BASE_URL


def test_shadow_never_gets_the_live_keys(both_keys):
    with session_scope() as s:
        c = get_client(s, "shadow")
    assert c.key_id == "paper-id"
    assert c.base_url == PAPER_BASE_URL


def test_the_default_client_is_still_paper(both_keys):
    """Dozens of callers ask for "the client" with no mode. That must keep
    meaning paper even once live keys exist."""
    with session_scope() as s:
        assert get_client(s).base_url == PAPER_BASE_URL


def test_deleting_the_live_keys_makes_live_unreachable_again(both_keys):
    """The fastest route back, and it must never be harder than adding them."""
    with session_scope() as s:
        security.delete_secret(s, LIVE_SECRET_KEY_ID)
        security.delete_secret(s, LIVE_SECRET_KEY_SECRET)
    with session_scope() as s:
        assert get_client(s, "live") is None
        assert live_available(s) is False


# ── storing the keys ─────────────────────────────────────────────────────────
def test_live_keys_are_verified_against_the_LIVE_host(client, paper_only):
    """The check that makes storing them meaningful. Paper keys pasted here must
    be rejected by Alpaca, not accepted and then found broken at order time —
    the worst outcome, because the UI would say live and nothing would trade."""
    seen = {}

    async def _account(self):
        seen["url"] = self.base_url
        return {"account_number": "LIVE123", "status": "ACTIVE"}

    with patch.object(AlpacaClient, "account", _account):
        r = client.post("/api/setup/alpaca/live",
                        json={"key_id": "k", "key_secret": "s"})
    assert r.status_code == 200, r.text
    assert seen["url"] == LIVE_BASE_URL, "verified against the wrong host"
    with session_scope() as s:
        assert live_credentials_stored(s) is True


def test_rejected_keys_are_not_stored(client, paper_only):
    """Nothing may be written on a failed verification, or a typo leaves live
    'configured' with credentials that cannot trade."""
    with patch.object(AlpacaClient, "account",
                      AsyncMock(side_effect=AlpacaError(401, "unauthorized"))):
        r = client.post("/api/setup/alpaca/live",
                        json={"key_id": "paper", "key_secret": "keys"})
    assert r.status_code == 400, r.text
    assert "paper keys will not work here" in r.json()["detail"]
    with session_scope() as s:
        assert live_credentials_stored(s) is False


def test_storing_live_keys_does_not_repoint_the_account_tag(client, paper_only):
    """`current_account_id` stamps NEW trades and the paper engine is running.
    Writing the live account number here would tag paper trades with it."""
    from qt.settings_service import get_setting, set_setting

    with session_scope() as s:
        set_setting(s, "current_account_id", "PA_ORIGINAL")
    with patch.object(AlpacaClient, "account",
                      AsyncMock(return_value={"account_number": "LIVE123"})):
        client.post("/api/setup/alpaca/live", json={"key_id": "k", "key_secret": "s"})
    with session_scope() as s:
        assert get_setting(s, "current_account_id") == "PA_ORIGINAL"


def test_setup_state_reports_the_two_separately(client, both_keys):
    """Paper being configured says nothing about whether real money can move."""
    state = client.get("/api/setup/state").json()
    assert state["alpaca_configured"] is True
    assert state["alpaca_live_configured"] is True


def test_the_live_keys_can_be_forgotten(client, both_keys):
    r = client.delete("/api/setup/alpaca/live")
    assert r.status_code == 200
    assert r.json()["alpaca_live_configured"] is False
    with session_scope() as s:
        assert live_credentials_stored(s) is False
        # The paper account must survive it.
        assert get_client(s, "paper") is not None


# ── the master switch ────────────────────────────────────────────────────────
def test_live_is_a_valid_master_mode():
    """Without this the ceiling caps everything at paper and no strategy could
    ever run live however it was configured."""
    assert "live" in ENGINE_MODES
