"""DB backup: online-backup roundtrip and retention pruning."""

import sqlite3

import pytest

from qt.services import backup


def _seed_db(path, rows):
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    conn.executemany("INSERT INTO t (id, v) VALUES (?, ?)", rows)
    conn.commit()
    conn.close()


def _read_rows(path):
    conn = sqlite3.connect(str(path))
    try:
        return conn.execute("SELECT id, v FROM t ORDER BY id").fetchall()
    finally:
        conn.close()


def test_backup_restore_roundtrip(tmp_path):
    rows = [(1, "alpha"), (2, "beta"), (3, "gamma")]
    _seed_db(tmp_path / "qt.db", rows)

    dest = backup.backup_db(tmp_path, retain=7)
    assert dest.exists()
    assert _read_rows(dest) == rows  # backup is a faithful copy

    # Restore into a fresh data dir and confirm the rows survive.
    restore_dir = tmp_path / "restored"
    restore_dir.mkdir()
    restored = backup.restore_db(dest, restore_dir)
    assert restored == restore_dir / "qt.db"
    assert _read_rows(restored) == rows


def test_backup_survives_open_wal_connection(tmp_path):
    # Simulate a live DB: keep a WAL connection open with an uncommitted-then-
    # committed write, then back up. The snapshot must reflect committed state.
    _seed_db(tmp_path / "qt.db", [(1, "a")])
    live = sqlite3.connect(str(tmp_path / "qt.db"))
    live.execute("INSERT INTO t (id, v) VALUES (2, 'b')")
    live.commit()
    try:
        dest = backup.backup_db(tmp_path)
    finally:
        live.close()
    assert _read_rows(dest) == [(1, "a"), (2, "b")]


def test_missing_db_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        backup.backup_db(tmp_path)


def test_prune_keeps_newest_n(tmp_path):
    backups_dir = tmp_path / "backups"
    backups_dir.mkdir()
    # Names sort chronologically by construction.
    names = [f"qt-2026070{i}-000000.db" for i in range(1, 10)]
    for n in names:
        (backups_dir / n).write_text("x")

    removed = backup.prune_backups(backups_dir, retain=3)
    remaining = sorted(p.name for p in backups_dir.glob("qt-*.db"))
    assert remaining == names[-3:]
    assert len(removed) == 6


def test_prune_retain_zero_is_noop(tmp_path):
    backups_dir = tmp_path / "backups"
    backups_dir.mkdir()
    (backups_dir / "qt-20260701-000000.db").write_text("x")
    assert backup.prune_backups(backups_dir, retain=0) == []
    assert len(list(backups_dir.glob("qt-*.db"))) == 1


def test_backup_prunes_within_data_dir(tmp_path):
    _seed_db(tmp_path / "qt.db", [(1, "a")])
    backups_dir = tmp_path / "backups"
    backups_dir.mkdir()
    for i in range(1, 6):
        (backups_dir / f"qt-2026070{i}-000000.db").write_text("old")
    # A new backup with retain=3 should leave exactly 3 files total.
    backup.backup_db(tmp_path, retain=3, name="qt-20260709-000000.db")
    assert len(list(backups_dir.glob("qt-*.db"))) == 3
