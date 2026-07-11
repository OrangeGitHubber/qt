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
