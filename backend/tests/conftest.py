import os
import tempfile

import pytest

# Isolate every test run in a throwaway data dir BEFORE qt modules import.
_tmp = tempfile.mkdtemp(prefix="qt-test-")
os.environ["QT_DATA_DIR"] = _tmp
# Most tests exercise business logic, not the auth gate; auth tests flip this off.
os.environ["QT_AUTH_DISABLED"] = "true"
# Never run the background scheduler inside tests.
os.environ["QT_NO_SCHEDULER"] = "true"

from fastapi.testclient import TestClient  # noqa: E402

from qt.db import init_db, session_scope  # noqa: E402
from qt.main import app  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _db():
    init_db()


@pytest.fixture(autouse=True)
def _reset_account_setting():
    """Trades are tagged with the current broker account, and the journal / P&L
    views default to filtering by it. The setting is global, so a test that saves
    keys (which stamps it) would otherwise make later tests' untagged trades
    vanish. Clear it before each test so the default filter is inert unless the
    test sets it deliberately."""
    from qt.settings_service import set_setting

    with session_scope() as s:
        set_setting(s, "current_account_id", "")
    yield


@pytest.fixture()
def client():
    return TestClient(app)


@pytest.fixture()
def db_session():
    with session_scope() as session:
        yield session
