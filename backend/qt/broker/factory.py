from sqlalchemy.orm import Session

from qt import security
from qt.broker.alpaca import SECRET_KEY_ID, SECRET_KEY_SECRET, AlpacaClient


def get_client(session: Session) -> AlpacaClient | None:
    """Build a client from the stored (encrypted) paper keys, or None if not set up."""
    key_id = security.get_secret(session, SECRET_KEY_ID)
    key_secret = security.get_secret(session, SECRET_KEY_SECRET)
    if not key_id or not key_secret:
        return None
    return AlpacaClient(key_id=key_id, key_secret=key_secret)
