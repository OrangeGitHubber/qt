from qt import security
from qt.models import Secret


def test_secret_roundtrip(db_session):
    security.set_secret(db_session, "demo", "s3cret-value")
    assert security.get_secret(db_session, "demo") == "s3cret-value"


def test_secret_is_encrypted_at_rest(db_session):
    security.set_secret(db_session, "demo2", "plaintext-should-not-appear")
    row = db_session.get(Secret, "demo2")
    assert "plaintext-should-not-appear" not in row.ciphertext


def test_secret_overwrite(db_session):
    security.set_secret(db_session, "demo3", "first")
    security.set_secret(db_session, "demo3", "second")
    assert security.get_secret(db_session, "demo3") == "second"


def test_missing_secret_returns_none(db_session):
    assert security.get_secret(db_session, "nope") is None
