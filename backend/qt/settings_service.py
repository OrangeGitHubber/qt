import json
from typing import Any

from sqlalchemy.orm import Session

from qt.models import Setting

DEFAULTS: dict[str, Any] = {
    "trading_mode": "paper",  # paper | live_approval | live_auto (ladder enforced later)
    "app_name": "QT Auto-Trader",
    # Display only — every stored timestamp is and stays UTC. This is the wall
    # clock the UI renders them on, and it defaults to the market's own zone
    # because the times worth comparing (an entry, a close, an edit) are market
    # times. Server-side rather than per-browser so it survives a restart and
    # reads the same from every device.
    "display_timezone": "America/New_York",
}


def get_setting(session: Session, key: str) -> Any:
    row = session.get(Setting, key)
    if row is None:
        return DEFAULTS.get(key)
    return json.loads(row.value)


def set_setting(session: Session, key: str, value: Any) -> None:
    encoded = json.dumps(value)
    row = session.get(Setting, key)
    if row:
        row.value = encoded
    else:
        session.add(Setting(key=key, value=encoded))


def all_settings(session: Session) -> dict[str, Any]:
    merged = dict(DEFAULTS)
    for row in session.query(Setting).all():
        merged[row.key] = json.loads(row.value)
    return merged
