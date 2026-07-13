import os
from unittest.mock import AsyncMock, patch

import pytest

from qt import security
from qt.api.deps import SESSION_COOKIE
from qt.db import session_scope
from qt.settings_service import set_setting


@pytest.fixture()
def real_auth(client):
    """Disable the test-suite auth bypass and configure Google auth."""
    os.environ["QT_AUTH_DISABLED"] = "false"
    with session_scope() as s:
        set_setting(s, "google_client_id", "test-client-id.apps.googleusercontent.com")
        security.set_secret(s, "google_client_secret", "test-secret-value")
        set_setting(s, "owner_email", "werner@example.com")
        set_setting(s, "allowed_emails", ["werner@example.com"])
    yield
    os.environ["QT_AUTH_DISABLED"] = "true"
    with session_scope() as s:
        set_setting(s, "google_client_id", None)
        set_setting(s, "allowed_emails", [])
        set_setting(s, "owner_email", None)


def _cookie_for(email: str) -> str:
    return security.create_session_token(email)


def test_unconfigured_instance_exposes_nothing(client):
    os.environ["QT_AUTH_DISABLED"] = "false"
    try:
        assert client.get("/api/status").status_code == 503
        assert client.get("/api/scanner/config").status_code == 503
        assert client.get("/api/health").status_code == 200  # health stays public
        state = client.get("/api/auth/state").json()
        assert state["configured"] is False and state["email"] is None
    finally:
        os.environ["QT_AUTH_DISABLED"] = "true"


def test_no_cookie_is_401(client, real_auth):
    assert client.get("/api/status").status_code == 401
    assert client.get("/api/watchlist").status_code == 401


def test_valid_session_cookie_passes(client, real_auth):
    client.cookies.set(SESSION_COOKIE, _cookie_for("werner@example.com"))
    resp = client.get("/api/scanner/config")
    assert resp.status_code == 200
    client.cookies.clear()


def test_forged_cookie_rejected(client, real_auth):
    client.cookies.set(SESSION_COOKIE, "gAAAAABforged-nonsense")
    assert client.get("/api/status").status_code == 401
    client.cookies.clear()


def test_email_not_on_allowlist_rejected(client, real_auth):
    client.cookies.set(SESSION_COOKIE, _cookie_for("stranger@example.com"))
    assert client.get("/api/status").status_code == 403
    client.cookies.clear()


def test_bootstrap_only_once(client, real_auth):
    resp = client.post(
        "/api/auth/bootstrap",
        json={"client_id": "x" * 20, "client_secret": "y" * 20, "owner_email": "a@b.com"},
    )
    assert resp.status_code == 409


def test_owner_manages_allowlist(client, real_auth):
    client.cookies.set(SESSION_COOKIE, _cookie_for("werner@example.com"))
    resp = client.post("/api/auth/allowlist", json={"email": "brother@example.com"})
    assert resp.status_code == 200
    assert "brother@example.com" in resp.json()["emails"]

    # non-owner cannot modify
    client.cookies.set(SESSION_COOKIE, _cookie_for("brother@example.com"))
    resp = client.post("/api/auth/allowlist", json={"email": "eve@example.com"})
    assert resp.status_code == 403

    # owner cannot be removed
    client.cookies.set(SESSION_COOKIE, _cookie_for("werner@example.com"))
    assert client.delete("/api/auth/allowlist/werner@example.com").status_code == 400
    assert client.delete("/api/auth/allowlist/brother@example.com").status_code == 200
    client.cookies.clear()


def test_callback_sets_cookie_for_allowed_email(client, real_auth):
    # login to obtain the state cookie
    resp = client.get("/api/auth/login", follow_redirects=False)
    assert resp.status_code == 307
    assert "accounts.google.com" in resp.headers["location"]
    state = resp.headers["location"].split("state=")[1].split("&")[0]

    with patch(
        "qt.api.auth._exchange_code_for_email",
        new=AsyncMock(return_value=("werner@example.com", True)),
    ):
        resp = client.get(
            f"/api/auth/callback?code=fake&state={state}", follow_redirects=False
        )
    assert resp.status_code == 307
    assert SESSION_COOKIE in resp.cookies

    with patch(
        "qt.api.auth._exchange_code_for_email",
        new=AsyncMock(return_value=("stranger@example.com", True)),
    ):
        client.cookies.clear()
        resp2 = client.get("/api/auth/login", follow_redirects=False)
        state2 = resp2.headers["location"].split("state=")[1].split("&")[0]
        resp3 = client.get(
            f"/api/auth/callback?code=fake&state={state2}", follow_redirects=False
        )
    assert "denied" in resp3.headers["location"]
    assert SESSION_COOKIE not in resp3.cookies
    client.cookies.clear()
