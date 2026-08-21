from __future__ import annotations

from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool, text

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = None


def ensure_sqlite_parent_directory(url: str) -> None:
    """Make Alembic's standalone SQLite default work on a fresh checkout."""
    prefix = "sqlite:///"
    if not url.startswith(prefix) or url.endswith(":memory:"):
        return
    Path(url.removeprefix(prefix)).parent.mkdir(parents=True, exist_ok=True)


def widen_postgres_version_table(connection) -> None:
    """Pre-create/width the alembic_version table on PostgreSQL.

    Alembic creates ``alembic_version.version_num`` as VARCHAR(32), but this
    project's revision ids (e.g. ``017_generation_task_retry_lineage``)
    exceed 32 characters. SQLite does not enforce the declared width, so the
    issue only surfaces on PostgreSQL. Creating the table first with a wider
    column is enough: Alembic's own ``_ensure_version_table`` skips tables
    that already exist. Existing narrower tables (interrupted upgrades) are
    widened in place. SQLite databases are untouched.
    """
    if connection.dialect.name != "postgresql":
        return
    connection.execute(
        text(
            "CREATE TABLE IF NOT EXISTS alembic_version ("
            "version_num VARCHAR(64) NOT NULL, "
            "CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num))"
        )
    )
    connection.execute(
        text("ALTER TABLE alembic_version ALTER COLUMN version_num TYPE VARCHAR(64)")
    )


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    ensure_sqlite_parent_directory(config.get_main_option("sqlalchemy.url"))
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        widen_postgres_version_table(connection)
        # Commit the DDL explicitly: otherwise the connection stays inside an
        # implicit autobegin transaction, Alembic then reuses that "external"
        # transaction, and the whole upgrade chain is rolled back when the
        # connection closes.
        connection.commit()
        context.configure(connection=connection, target_metadata=target_metadata)

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
