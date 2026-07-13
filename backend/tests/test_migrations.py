from sqlalchemy import inspect

from qt.db import engine, wal_mode


def test_all_tables_exist_after_init(_db):
    tables = set(inspect(engine).get_table_names())
    expected = {
        "alembic_version",
        "settings",
        "secrets",
        "audit_log",
        "watchlist",
        "strategies",
        "strategy_config_versions",
        "trades",
        "benchmark_snapshots",
    }
    assert expected <= tables


def test_wal_mode_enabled(_db):
    assert wal_mode().lower() == "wal"
