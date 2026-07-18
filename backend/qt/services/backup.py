"""Periodic SQLite backups of the precious DB (qt.db).

Uses Python's online backup API (``sqlite3.Connection.backup``), which is safe
on a live WAL database — it takes a consistent snapshot without blocking
writers. Backups land in ``<data>/backups/qt-YYYYMMDD-HHMMSS.db`` and the last
``retain`` are kept.

The bar cache (``bars.db``, when it exists) is deliberately NOT backed up: it's
bulk, disposable, rebuildable reference data.

Restore is a manual, documented file swap (see docs/data-persistence.md):
stop the container, replace ``qt.db`` with a chosen backup, keep the matching
``instance.key``, start again. :func:`restore_db` performs that copy and is used
by the roundtrip test.
"""

from __future__ import annotations

import logging
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("qt.backup")

DEFAULT_RETAIN = 7
_PREFIX = "qt-"
_SUFFIX = ".db"


def _list_backups(backups_dir: Path) -> list[Path]:
    return sorted(backups_dir.glob(f"{_PREFIX}*{_SUFFIX}"))


def prune_backups(backups_dir: Path, retain: int) -> list[Path]:
    """Keep the newest ``retain`` backups (lexical == chronological given the
    timestamp name). Returns the paths removed."""
    if retain <= 0:
        return []
    files = _list_backups(backups_dir)
    to_remove = files[:-retain] if len(files) > retain else []
    for path in to_remove:
        try:
            path.unlink()
        except OSError:
            log.warning("could not remove old backup %s", path)
    return to_remove


def backup_db(data_dir: Path, retain: int = DEFAULT_RETAIN, name: str | None = None) -> Path:
    """Snapshot ``<data>/qt.db`` into ``<data>/backups/`` and prune old ones.

    ``name`` overrides the generated timestamp filename (used by tests to avoid
    same-second collisions). Returns the backup path.
    """
    src = data_dir / "qt.db"
    if not src.exists():
        raise FileNotFoundError(f"no database to back up at {src}")

    backups_dir = data_dir / "backups"
    backups_dir.mkdir(parents=True, exist_ok=True)

    filename = name or f"{_PREFIX}{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}{_SUFFIX}"
    dest = backups_dir / filename

    src_conn = sqlite3.connect(str(src))
    dst_conn = sqlite3.connect(str(dest))
    try:
        src_conn.backup(dst_conn)  # online, WAL-safe consistent snapshot
    finally:
        dst_conn.close()
        src_conn.close()

    prune_backups(backups_dir, retain)
    log.info("DB backup written: %s", dest)
    return dest


def restore_db(backup_path: Path, data_dir: Path) -> Path:
    """Copy a backup into place as ``<data>/qt.db`` (overwriting). The caller is
    responsible for stopping the app first and keeping the matching
    instance.key. Returns the restored DB path."""
    dest = data_dir / "qt.db"
    # Remove stale WAL sidecars so the restored file isn't shadowed by them.
    for sidecar in (data_dir / "qt.db-wal", data_dir / "qt.db-shm"):
        try:
            sidecar.unlink()
        except OSError:
            pass
    shutil.copyfile(backup_path, dest)
    return dest
