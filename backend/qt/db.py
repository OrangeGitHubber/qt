from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from qt.paths import db_url

engine = create_engine(db_url(), connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_connection, _record) -> None:
    # WAL lets the engine loop and the web UI write concurrently without
    # "database is locked" errors; busy_timeout covers the rest.
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def _alembic_config():
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", str(Path(__file__).resolve().parent / "alembic"))
    cfg.set_main_option("sqlalchemy.url", db_url())
    return cfg


def init_db() -> None:
    """Bring the schema to the latest migration.

    Databases created before Alembic existed (Phase 0/1) already have the
    baseline tables but no alembic_version — stamp those at 0001 first so
    the baseline migration is skipped, then upgrade normally.
    """
    from alembic import command

    inspector = inspect(engine)
    has_alembic = inspector.has_table("alembic_version")
    has_legacy = inspector.has_table("settings")
    cfg = _alembic_config()
    if not has_alembic and has_legacy:
        command.stamp(cfg, "0001")
    command.upgrade(cfg, "head")


@contextmanager
def session_scope() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_session() -> Iterator[Session]:
    """FastAPI dependency."""
    with session_scope() as session:
        yield session


def wal_mode() -> str:
    with engine.connect() as conn:
        return conn.execute(text("PRAGMA journal_mode")).scalar_one()
