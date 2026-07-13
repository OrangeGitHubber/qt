from alembic import context
from sqlalchemy import create_engine, pool


def run() -> None:
    url = context.config.get_main_option("sqlalchemy.url")
    if context.is_offline_mode():
        context.configure(url=url, target_metadata=None)
        with context.begin_transaction():
            context.run_migrations()
        return

    engine = create_engine(url, poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=None)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


run()
