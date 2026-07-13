"""Encryption for secrets stored in the database.

A random Fernet key is generated on first boot and kept in the data
volume (instance.key). Secrets such as Alpaca API keys are encrypted
with it before they touch SQLite, so a copied qt.db alone leaks nothing.
"""

from pathlib import Path

from cryptography.fernet import Fernet
from sqlalchemy.orm import Session

from qt.models import Secret
from qt.paths import data_dir

_KEY_FILE = "instance.key"
_fernet: Fernet | None = None


def _key_path() -> Path:
    return data_dir() / _KEY_FILE


def get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        path = _key_path()
        if not path.exists():
            path.write_bytes(Fernet.generate_key())
            try:
                path.chmod(0o600)
            except OSError:
                pass  # e.g. Windows dev machine
        _fernet = Fernet(path.read_bytes())
    return _fernet


def set_secret(session: Session, name: str, value: str) -> None:
    ciphertext = get_fernet().encrypt(value.encode()).decode()
    existing = session.get(Secret, name)
    if existing:
        existing.ciphertext = ciphertext
    else:
        session.add(Secret(name=name, ciphertext=ciphertext))


def get_secret(session: Session, name: str) -> str | None:
    row = session.get(Secret, name)
    if row is None:
        return None
    return get_fernet().decrypt(row.ciphertext.encode()).decode()


def delete_secret(session: Session, name: str) -> None:
    row = session.get(Secret, name)
    if row:
        session.delete(row)


# ---- Login session tokens (Fernet = encrypted + timestamped, so TTL works) ----

SESSION_TTL_SECONDS = 7 * 24 * 3600


def create_session_token(email: str) -> str:
    return get_fernet().encrypt(email.encode()).decode()


def verify_session_token(token: str) -> str | None:
    """Return the email if the token is valid and unexpired, else None."""
    from cryptography.fernet import InvalidToken

    try:
        return get_fernet().decrypt(token.encode(), ttl=SESSION_TTL_SECONDS).decode()
    except (InvalidToken, ValueError):
        return None
