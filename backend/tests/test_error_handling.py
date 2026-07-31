"""What the browser is told when the server breaks.

FastAPI's default 500 body is the string "Internal Server Error". A user
reporting one hands you nothing to search the log with, and a transient
"database is locked" — which is fixed by trying again — looked identical to a
permanent bug. Both are now answered specifically.
"""

import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from qt.main import app


@pytest.fixture()
def raw_client():
    """A client that does NOT re-raise server exceptions, so we see the response
    the browser would actually get. The default TestClient propagates them to the
    test instead, which is useful for debugging and useless for testing the
    handler that turns them into a 500."""
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture()
def boom_routes():
    """Two routes that fail the two ways that matter, mounted for one test."""
    router = APIRouter(prefix="/api/_test")

    @router.get("/boom")
    def boom() -> dict:
        raise RuntimeError("something specific went wrong internally")

    @router.get("/locked")
    def locked() -> dict:
        raise OperationalError("UPDATE strategies ...", {}, Exception("database is locked"))

    @router.get("/other-db-error")
    def other_db_error() -> dict:
        raise OperationalError("SELECT ...", {}, Exception("no such column: bogus"))

    # The SPA catch-all is registered at import time, so an appended route would
    # never be reached — put these in FRONT of it for the duration of the test.
    before = len(app.router.routes)
    app.include_router(router)
    added = app.router.routes[before:]
    app.router.routes = added + app.router.routes[:before]
    yield
    app.router.routes = [r for r in app.router.routes if "/api/_test" not in getattr(r, "path", "")]


def test_a_500_carries_a_reference_you_can_grep_for(raw_client, boom_routes, caplog):
    with caplog.at_level("ERROR"):
        r = raw_client.get("/api/_test/boom", follow_redirects=False)
    assert r.status_code == 500
    detail = r.json()["detail"]
    # The user-facing message must contain a reference...
    assert "ref " in detail
    ref = detail.split("ref ")[1].split(")")[0].strip()
    assert len(ref) == 6
    # ...and the SAME reference must appear in the log, next to the real error.
    logged = caplog.text
    assert ref in logged, "the reference the user was given isn't in the log"
    assert "something specific went wrong internally" in logged


def test_the_internal_message_is_not_leaked_to_the_browser(raw_client, boom_routes):
    """The reference is for correlating, not for shipping stack traces to the UI."""
    r = raw_client.get("/api/_test/boom", follow_redirects=False)
    assert "something specific went wrong internally" not in r.json()["detail"]


def test_a_locked_database_says_so_and_says_to_retry(client, boom_routes):
    """SQLite serialises writers, so a save colliding with the engine tick is
    transient. Answering 500 made a retryable hiccup look like a broken app."""
    r = client.get("/api/_test/locked", follow_redirects=False)
    assert r.status_code == 503
    detail = r.json()["detail"].lower()
    assert "busy" in detail and "try again" in detail
    assert "nothing was saved" in detail  # it must not leave you guessing about state


def test_a_non_lock_database_error_is_still_a_500(client, boom_routes):
    """Only the transient case gets the friendly retry. A genuine schema/SQL bug
    must keep its reference and its 500, not be dressed up as 'try again'."""
    r = client.get("/api/_test/other-db-error", follow_redirects=False)
    assert r.status_code == 500
    assert "ref " in r.json()["detail"]
