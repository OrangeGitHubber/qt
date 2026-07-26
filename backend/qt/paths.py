"""Resolve the data directory that holds the SQLite DB and instance key.

In the Docker image this is /data (an unraid appdata volume). For local
development it falls back to ./data next to the repo so nothing touches
the system.
"""

import os
from pathlib import Path


def data_dir() -> Path:
    raw = os.environ.get("QT_DATA_DIR")
    if raw:
        path = Path(raw)
    elif Path("/data").is_dir():
        path = Path("/data")
    else:
        path = Path(__file__).resolve().parent.parent / "data"
    path.mkdir(parents=True, exist_ok=True)
    return path


def db_url() -> str:
    return f"sqlite:///{data_dir() / 'qt.db'}"


def bar_cache_url() -> str:
    """Where the bulk, rebuildable historical bar / movers cache lives.

    Separate from qt.db (which holds precious config/keys/journal and is
    backed up). Defaults to a local bars.db SQLite file; set QT_BAR_CACHE_URL
    to a Postgres DSN for a durable, shared cache. Accepts the 'postgres://'
    scheme too (SQLAlchemy only understands 'postgresql://')."""
    raw = os.environ.get("QT_BAR_CACHE_URL")
    if raw:
        if raw.startswith("postgres://"):
            raw = "postgresql://" + raw[len("postgres://") :]
        return raw
    return f"sqlite:///{data_dir() / 'bars.db'}"
