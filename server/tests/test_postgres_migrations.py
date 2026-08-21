"""
T03 / DB-01 - PostgreSQL 16 fixture tests (canonical path per V3 frozen file mapping).

Verifies: the local/CI PG fixture is reachable, is PostgreSQL 16, and supports
concurrent independent connections. Tests are skipped automatically when no
PostgreSQL is available (e.g. the Linux quality gate before the PG service is
wired into CI), so SQLite-only environments stay green.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

import asyncpg  # type: ignore[import-untyped]
import psycopg
import pytest

DEFAULT_DSN = "postgresql://testuser:testpass@localhost:5433/customer_v3_test"
SKIP_REASON = "PostgreSQL fixture not reachable; start it via scripts/pg-fixture.sh start"


def _pg_dsn() -> str:
    return os.environ.get("TEST_POSTGRESQL_URL", DEFAULT_DSN)


def _pg_available(dsn: str) -> bool:
    try:

        async def probe() -> None:
            conn = await asyncpg.connect(dsn)
            await conn.close()

        asyncio.run(asyncio.wait_for(probe(), timeout=3))
    except Exception:
        return False
    return True


pytestmark = pytest.mark.skipif(
    not _pg_available(_pg_dsn()),
    reason=SKIP_REASON,
)


def _run(coro_fn: Callable[[], Coroutine[Any, Any, None]]) -> None:
    return asyncio.run(coro_fn())


def test_database_creation() -> None:
    async def case() -> None:
        conn = await asyncpg.connect(_pg_dsn())
        try:
            result = await conn.fetchval("SELECT current_database();")
            assert result == "customer_v3_test", f"unexpected database {result}"
        finally:
            await conn.close()

    _run(case)


def test_user_identity() -> None:
    async def case() -> None:
        conn = await asyncpg.connect(_pg_dsn())
        try:
            result = await conn.fetchval("SELECT current_user;")
            assert result == "testuser", f"unexpected user {result}"
        finally:
            await conn.close()

    _run(case)


def test_postgres_version() -> None:
    async def case() -> None:
        conn = await asyncpg.connect(_pg_dsn())
        try:
            result = await conn.fetchval("SHOW server_version;")
            assert str(result).startswith("16."), f"expected PostgreSQL 16.x, got {result}"
        finally:
            await conn.close()

    _run(case)


def test_independent_connections() -> None:
    async def case() -> None:
        conn1 = await asyncpg.connect(_pg_dsn())
        conn2 = await asyncpg.connect(_pg_dsn())
        try:
            assert await conn1.fetchval("SELECT 1;") == 1
            assert await conn2.fetchval("SELECT 2;") == 2
        finally:
            await conn1.close()
            await conn2.close()

    _run(case)


# ---------------------------------------------------------------------------
# T06 / DB-04 — full upgrade/downgrade/re-upgrade rehearsal on PostgreSQL
# ---------------------------------------------------------------------------

_skip_no_pg = pytest.mark.skipif(
    not _pg_available(_pg_dsn()),
    reason=f"PostgreSQL fixture not reachable at {_pg_dsn()}",
)


def _admin_dsn() -> str:
    return _pg_dsn().rsplit("/", 1)[0] + "/postgres"


def _rehearsal_dsn() -> str:
    """Dedicated database for the rehearsal (downgrade drops every table)."""
    return _pg_dsn().rsplit("/", 1)[0] + "/t06_migrate_test"


def _drop_database(db_name: str) -> None:
    with psycopg.connect(_admin_dsn(), autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)')


def _alembic_config(dsn: str):  # type: ignore[no-untyped-def]
    from alembic.config import Config

    server_dir = Path(__file__).resolve().parent.parent
    config = Config(str(server_dir / "alembic.ini"))
    config.set_main_option("script_location", str(server_dir / "migrations"))
    config.set_main_option("sqlalchemy.url", dsn)
    return config


@_skip_no_pg
def test_pg_full_upgrade_downgrade_reupgrade_and_indexes() -> None:
    """DB-04 rehearsal: empty PG database, upgrade to head, verify key tables/
    constraints, downgrade to base, then re-upgrade to head. Historical
    revisions keep their SQLite behaviour; PG-only branches are dialect-guarded
    inside the revisions and env.py."""
    from alembic import command

    dsn = _rehearsal_dsn()
    sqlalchemy_dsn = dsn.replace("postgresql://", "postgresql+psycopg://")
    _drop_database("t06_migrate_test")
    with psycopg.connect(_admin_dsn(), autocommit=True) as conn:
        conn.execute('CREATE DATABASE "t06_migrate_test"')

    try:
        # Stage 1: upgrade to head on the empty database.
        command.upgrade(_alembic_config(sqlalchemy_dsn), "head")

        with psycopg.connect(dsn) as conn:
            version = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
            assert version == "024_wallet_backfill", f"unexpected head revision: {version}"

            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
                ).fetchall()
            }
            for required in (
                "users",
                "projects",
                "characters",
                "generation_batches",
                "generation_tasks",
                "wallets",
                "wallet_transactions",
                "recharge_orders",
                "character_generation_tasks",
                "external_call_logs",
            ):
                assert required in tables, f"missing table {required} after upgrade head"

            constraints = {
                row[0]
                for row in conn.execute(
                    "SELECT conname FROM pg_constraint WHERE connamespace = 'public'::regnamespace"
                ).fetchall()
            }
            assert "uq_generation_batches_user_project_key" in constraints
            assert "generation_tasks_batch_id_fkey" in constraints, (
                "009 must re-attach the FK on PG"
            )

            # Partial unique indexes must exist on PG with their WHERE clauses
            # (sqlite_where is silently ignored by PG — DB-04/P1 review).
            index_defs = {
                row[0]: row[1]
                for row in conn.execute(
                    "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = 'public'"
                ).fetchall()
            }
            assert any(
                name.startswith("idx_generation_tasks_prompt_version") for name in index_defs
            )
            expected_partial = {
                "uq_character_assets_published_view": "is_published_selection = 1",
                "uq_generation_task_operations_pending": "result_status = 'PENDING'",
                "uq_recharge_orders_provider_trade_no": "provider_trade_no IS NOT NULL",
                "uq_wallet_transactions_charge_order": "type = 'CHARGE'",
                "uq_wallet_transactions_reserve_round": "type = 'RESERVE'",
            }
            for name, predicate in expected_partial.items():
                assert name in index_defs, f"partial unique index {name} missing on PG"
                assert predicate in index_defs[name], (
                    f"{name} must be a partial index with WHERE {predicate}, "
                    f"got: {index_defs[name]}"
                )

        # Stage 2: downgrade all the way to base.
        command.downgrade(_alembic_config(sqlalchemy_dsn), "base")
        with psycopg.connect(dsn) as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
                ).fetchall()
            }
            assert "users" not in tables, "downgrade to base must drop business tables"

        # Stage 3: re-upgrade to head (rehearsal of a rolled-back deployment).
        command.upgrade(_alembic_config(sqlalchemy_dsn), "head")
        with psycopg.connect(dsn) as conn:
            version = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
            assert version == "024_wallet_backfill"
    finally:
        _drop_database("t06_migrate_test")
