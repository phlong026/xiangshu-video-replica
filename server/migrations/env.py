from __future__ import annotations

import os
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool, text

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = None

DATABASE_URL_ENV = "VIDEO_REPLICA_DATABASE_URL"
_PG_URL_PREFIXES = ("postgresql://", "postgres://")


def _to_psycopg_url(url: str) -> str:
    """Alembic runs on the psycopg3 driver; bare ``postgresql://`` URLs
    resolve to the psycopg2 dialect, which is not installed."""
    for prefix in _PG_URL_PREFIXES:
        if url.startswith(prefix):
            return url.replace(prefix, "postgresql+psycopg://", 1)
    return url


def resolve_migration_url() -> str:
    """Resolve the migration target URL (M0 review H3).

    ``VIDEO_REPLICA_DATABASE_URL`` — the same variable ``app.db_pg`` uses to
    pick the runtime database mode — takes precedence over the ``alembic.ini``
    default, so operators can run ``alembic upgrade head`` against PostgreSQL
    without editing the ini file. A URL explicitly set on the Alembic config
    object (tests use ``config.set_main_option``) is still honoured when the
    environment variable is unset.
    """
    env_url = os.environ.get(DATABASE_URL_ENV, "").strip()
    if env_url:
        return _to_psycopg_url(env_url)
    return config.get_main_option("sqlalchemy.url") or ""


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
    """Emit SQL to stdout without a database connection.

    Known limitation (M0 review M7): Alembic hardcodes the offline
    ``alembic_version.version_num`` column as VARCHAR(32), while revision ids
    in this project reach 33+ characters. The offline path therefore cannot
    produce an executable script for this chain — use the online path
    (``alembic upgrade head`` with VIDEO_REPLICA_DATABASE_URL) for all real
    upgrades and DBA-reviewed deployments.
    """
    url = resolve_migration_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    url = resolve_migration_url()
    ensure_sqlite_parent_directory(url)
    connectable = engine_from_config(
        {**config.get_section(config.config_ini_section, {}), "sqlalchemy.url": url},
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
