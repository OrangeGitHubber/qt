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


@pytest.fixture()
def client():
    return TestClient(app)


@pytest.fixture()
def db_session():
    with session_scope() as session:
        yield session
