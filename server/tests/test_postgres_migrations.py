"""
T03 / DB-01 - PostgreSQL 16 fixture tests (canonical path per V3 frozen file mapping).

Verifies: the local/CI PG fixture is reachable, is PostgreSQL 16, and supports
concurrent independent connections. Tests are skipped automatically when no
PostgreSQL is available, so SQLite-only environments stay green; the Linux
quality gate wires a postgres:16 service and sets TEST_POSTGRESQL_URL so these
tests always run in CI (M0 review H2).
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
            assert version == "028_admin_write_idempotency", f"unexpected head revision: {version}"

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
                "admin_sessions",
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
                # C1 regression lock: 022 shipped sqlite_where-only; the
                # predicate is restored append-only by 025 on PG.
                "uq_wallet_transactions_terminal_round": (
                    "ANY (ARRAY['SETTLE'::text, 'RELEASE'::text])"
                ),
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
            assert version == "028_admin_write_idempotency"
    finally:
        _drop_database("t06_migrate_test")


def test_pg_wallet_terminal_round_row_level() -> None:
    """C1 row-level regression: the billing lifecycle must work on PG.

    internal_billing writes RESERVE first and the terminal SETTLE (or
    RELEASE) afterwards with the *same* (task_id, billing_round). With the
    degraded table-wide unique index from 022 the terminal insert was
    rejected; with the 025 partial index it must succeed, while a second
    terminal row on the same key must still be rejected.
    """
    from alembic import command

    db_name = "m0_c1_row_test"
    dsn = _pg_dsn().rsplit("/", 1)[0] + f"/{db_name}"
    sqlalchemy_dsn = dsn.replace("postgresql://", "postgresql+psycopg://")
    _drop_database(db_name)
    with psycopg.connect(_admin_dsn(), autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{db_name}"')

    def insert_tx(
        conn: psycopg.Connection, tx_id: str, tx_type: str, avail: int, reserved: int
    ) -> None:
        conn.execute(
            "INSERT INTO wallet_transactions "
            "(id, user_id, type, available_delta, reserved_delta, "
            " task_id, billing_round, idempotency_key) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (tx_id, "u1", tx_type, avail, reserved, "t1", 1, f"{tx_type.lower()}:t1:1"),
        )

    try:
        command.upgrade(_alembic_config(sqlalchemy_dsn), "head")
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(
                "INSERT INTO users (id, username, display_name) VALUES ('u1', 'u1', 'User One')"
            )
            conn.execute(
                "INSERT INTO projects (id, owner_user_id, name) VALUES ('p1', 'u1', 'C1 Repro')"
            )
            conn.execute(
                "INSERT INTO generation_batches "
                "(id, project_id, created_by_user_id, idempotency_key, "
                " request_hash, request_snapshot_json) "
                "VALUES ('b1', 'p1', 'u1', 'b1-idem', 'b1-hash', '{}')"
            )
            conn.execute(
                "INSERT INTO generation_tasks (id, batch_id, provider, model) "
                "VALUES ('t1', 'b1', 'apilio', 'test-model')"
            )
            conn.execute("INSERT INTO wallets (user_id) VALUES ('u1')")

            # RESERVE then SETTLE on the same (task_id, billing_round):
            # the exact sequence that failed with 022's degraded index.
            insert_tx(conn, "tx1", "RESERVE", -1, 1)
            insert_tx(conn, "tx2", "SETTLE", 0, -1)

            # A second terminal row on the same key must still be rejected
            # by the partial unique index (idempotency of the terminal side).
            with pytest.raises(psycopg.errors.UniqueViolation):
                insert_tx(conn, "tx3", "RELEASE", 1, -1)
    finally:
        _drop_database(db_name)


def test_pg_wallet_downgrade_blocked_when_ledger_has_settled_rounds() -> None:
    """Review P1 regression: 025 downgrade must refuse (loudly, with the
    recovery path) once the ledger legitimately holds a RESERVE row plus a
    terminal row sharing (task_id, billing_round) — recreating the 022
    table-wide unique index would raise a uniqueness violation and brick the
    rollback. An empty ledger still downgrades symmetrically (Stage 2 of the
    rehearsal above)."""
    from alembic import command

    db_name = "m0_p1_downgrade_test"
    dsn = _pg_dsn().rsplit("/", 1)[0] + f"/{db_name}"
    sqlalchemy_dsn = dsn.replace("postgresql://", "postgresql+psycopg://")
    _drop_database(db_name)
    with psycopg.connect(_admin_dsn(), autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{db_name}"')

    try:
        command.upgrade(_alembic_config(sqlalchemy_dsn), "head")
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(
                "INSERT INTO users (id, username, display_name) VALUES ('u1', 'u1', 'User One')"
            )
            conn.execute(
                "INSERT INTO projects (id, owner_user_id, name) VALUES ('p1', 'u1', 'P1 Repro')"
            )
            conn.execute(
                "INSERT INTO generation_batches "
                "(id, project_id, created_by_user_id, idempotency_key, "
                " request_hash, request_snapshot_json) "
                "VALUES ('b1', 'p1', 'u1', 'b1-idem', 'b1-hash', '{}')"
            )
            conn.execute(
                "INSERT INTO generation_tasks (id, batch_id, provider, model) "
                "VALUES ('t1', 'b1', 'apilio', 'test-model')"
            )
            conn.execute("INSERT INTO wallets (user_id) VALUES ('u1')")
            for tx_id, tx_type, avail, reserved in (
                ("tx1", "RESERVE", -1, 1),
                ("tx2", "SETTLE", 0, -1),
            ):
                conn.execute(
                    "INSERT INTO wallet_transactions "
                    "(id, user_id, type, available_delta, reserved_delta, "
                    " task_id, billing_round, idempotency_key) "
                    "VALUES (%s, 'u1', %s, %s, %s, 't1', 1, %s)",
                    (tx_id, tx_type, avail, reserved, f"{tx_type.lower()}:t1:1"),
                )

        with pytest.raises(RuntimeError, match="cannot downgrade 025"):
            command.downgrade(_alembic_config(sqlalchemy_dsn), "024_wallet_backfill")

        # The database must be left exactly at head (no partial rollback).
        with psycopg.connect(dsn) as conn:
            version = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        assert version == "028_admin_write_idempotency"
    finally:
        _drop_database(db_name)


# ---------------------------------------------------------------------------
# T08 / DB-07 — billing provider / pricing_scope conditional constraints
# ---------------------------------------------------------------------------

_T08_BASELINE_ORDER = {
    "id": "o-t08",
    "user_id": "u-t08",
    "merchant_order_no": "T08-ORDER",
    "provider": "zpay",
    "provider_trade_no": None,
    "channel": "alipay",
    "status": "PENDING",
    "pricing_scope": "INTERNAL",
    "base_unit_price_fen_snapshot": 1000,
    "charged_unit_price_fen_snapshot": 1000,
    "min_recharge_fen_snapshot": 10000,
    "recharge_step_fen_snapshot": 1000,
    "amount_fen": 10000,
    "credits": 10,
    "paid_at": None,
}


def _t08_database(db_name: str) -> str:
    from alembic import command

    _drop_database(db_name)
    with psycopg.connect(_admin_dsn(), autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{db_name}"')
    dsn = _pg_dsn().rsplit("/", 1)[0] + f"/{db_name}"
    command.upgrade(_alembic_config(dsn.replace("postgresql://", "postgresql+psycopg://")), "head")
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO users (id, username, display_name) VALUES ('u-t08', 'u-t08', 'T08')"
        )
    return dsn


def _insert_t08_order(conn: psycopg.Connection, seq: int, **overrides: object) -> None:
    row = {
        **_T08_BASELINE_ORDER,
        "id": f"o-t08-{seq}",
        "merchant_order_no": f"T08-ORDER-{seq}",
        **overrides,
    }
    columns = ", ".join(row)
    placeholders = ", ".join("%s" for _ in row)
    conn.execute(
        f"INSERT INTO recharge_orders ({columns}) VALUES ({placeholders})",
        tuple(row.values()),
    )


def test_pg_billing_provider_shapes_accepted_and_rejected() -> None:
    """DB-07 exit gate: zpay / activation_code / admin_adjustment legal shapes
    pass, illegal shapes are rejected by PostgreSQL check constraints — not by
    application code (No-Go rule)."""

    db_name = "t08_billing_shapes"
    dsn = _t08_database(db_name)
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            # --- legal shapes -------------------------------------------------
            # zpay + INTERNAL + PENDING: the existing internal recharge flow.
            _insert_t08_order(conn, 1)
            # zpay + CUSTOMER_STANDARD + PAID with trade number: customer
            # top-up through ZPay (T22) at a customer price >= base price.
            _insert_t08_order(
                conn,
                2,
                pricing_scope="CUSTOMER_STANDARD",
                status="PAID",
                charged_unit_price_fen_snapshot=1500,
                amount_fen=15000,
                credits=10,
                provider_trade_no="ZPAY-TRADE-2",
                paid_at="2026-08-22T00:00:00+00:00",
            )
            # activation_code + CUSTOMER_STANDARD + PAID, no third-party trade
            # number, face value below the internal minimum recharge (the
            # batch face value decides, min/step ladders do not apply).
            _insert_t08_order(
                conn,
                3,
                provider="activation_code",
                pricing_scope="CUSTOMER_STANDARD",
                status="PAID",
                charged_unit_price_fen_snapshot=1500,
                amount_fen=1500,
                credits=1,
                paid_at="2026-08-22T00:00:00+00:00",
            )
            # admin_adjustment + INTERNAL + PAID, no trade number, amount below
            # the minimum recharge and off the step ladder (adjustments are
            # defined by their audited source document).
            _insert_t08_order(
                conn,
                4,
                provider="admin_adjustment",
                status="PAID",
                amount_fen=5000,
                credits=5,
                paid_at="2026-08-22T00:00:00+00:00",
            )

            # A CHARGE transaction referencing the activation_code order keeps
            # the existing append-only ledger shape (provider-agnostic).
            conn.execute(
                "INSERT INTO wallet_transactions "
                "(id, user_id, type, available_delta, reserved_delta, "
                " recharge_order_id, idempotency_key) "
                "VALUES ('tx-t08-3', 'u-t08', 'CHARGE', 1, 0, 'o-t08-3', 'charge:o-t08-3')"
            )

            # --- illegal shapes ----------------------------------------------
            def rejected(seq: int, **overrides: object) -> None:
                with pytest.raises(psycopg.errors.CheckViolation):
                    _insert_t08_order(conn, seq, **overrides)

            rejected(10, provider="wechat")  # unknown provider
            rejected(11, pricing_scope="CHANNEL_A")  # scope frozen until PRICE-01
            rejected(
                12,
                provider="activation_code",
                status="PAID",
                paid_at="2026-08-22T00:00:00+00:00",
            )  # scope pairing: activation_code is customer-only
            rejected(
                13,
                provider="activation_code",
                pricing_scope="CUSTOMER_STANDARD",
                status="PENDING",
                charged_unit_price_fen_snapshot=1500,
                amount_fen=1500,
                credits=1,
            )  # activation must land PAID atomically
            rejected(
                14,
                provider="activation_code",
                pricing_scope="CUSTOMER_STANDARD",
                status="PAID",
                provider_trade_no="X",
                charged_unit_price_fen_snapshot=1500,
                amount_fen=1500,
                credits=1,
                paid_at="2026-08-22T00:00:00+00:00",
            )  # no third-party trade number for activation codes
            rejected(
                15,
                provider="admin_adjustment",
                amount_fen=5000,
                credits=5,
            )  # adjustments must land PAID atomically (default PENDING here)
            rejected(
                16,
                status="PAID",
                provider_trade_no=None,
                paid_at="2026-08-22T00:00:00+00:00",
            )  # a paid ZPay order must carry its provider trade number
            rejected(22, provider_trade_no="ZPAY-SQUAT")  # a non-PAID ZPay order must not
            #   reserve a globally unique third-party trade number (review P2)
            rejected(
                17,
                pricing_scope="CUSTOMER_STANDARD",
                charged_unit_price_fen_snapshot=500,
                amount_fen=5000,
                credits=10,
            )  # customer price must never undercut the internal base price
            rejected(18, amount_fen=9000, credits=9)  # zpay: below min recharge
            rejected(19, amount_fen=10500)  # zpay: off the recharge step ladder
            rejected(20, credits=9)  # credits * price != amount (all providers)
            rejected(21, status="REFUNDED")  # unknown status (022 regression)
    finally:
        _drop_database(db_name)


def test_pg_admin_sessions_schema_and_invariants() -> None:
    """026 must carry the full frozen topic (review P1): the admin_sessions
    data layer for T09/DB-08 — digests only, unique session digest, expiry
    ordering, actor FK — lands in the same revision as the billing
    constraints so T09 is never left without a compliant schema home."""

    db_name = "t08_admin_sessions"
    dsn = _t08_database(db_name)
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(
                "INSERT INTO users (id, username, display_name, role) "
                "VALUES ('u-admin', 'u-admin', 'Admin', 'admin')"
            )
            conn.execute(
                "INSERT INTO admin_sessions "
                "(id, actor_user_id, session_digest, csrf_digest, "
                " last_activity_at, expires_at, created_ip_digest, created_ua_digest) "
                "VALUES ('as1', 'u-admin', 'digest-1', 'csrf-1', "
                " '2026-08-22T00:00:01+00:00', '2099-01-01T00:00:00+00:00', "
                " 'ip-digest', 'ua-digest')"
            )
            # A second session for the same actor is fine (session rotation),
            # but the session digest is globally unique.
            conn.execute(
                "INSERT INTO admin_sessions "
                "(id, actor_user_id, session_digest, csrf_digest, "
                " last_activity_at, expires_at, created_ip_digest, created_ua_digest) "
                "VALUES ('as2', 'u-admin', 'digest-2', 'csrf-2', "
                " '2026-08-22T00:00:01+00:00', '2099-01-01T00:00:00+00:00', "
                " 'ip-digest', 'ua-digest')"
            )
            with pytest.raises(psycopg.errors.UniqueViolation):
                conn.execute(
                    "INSERT INTO admin_sessions "
                    "(id, actor_user_id, session_digest, csrf_digest, "
                    " last_activity_at, expires_at, created_ip_digest, created_ua_digest) "
                    "VALUES ('as3', 'u-admin', 'digest-1', 'csrf-3', "
                    " '2026-08-22T00:00:01+00:00', '2099-01-01T00:00:00+00:00', "
                    " 'ip-digest', 'ua-digest')"
                )
            with pytest.raises(psycopg.errors.CheckViolation):
                conn.execute(
                    "INSERT INTO admin_sessions "
                    "(id, actor_user_id, session_digest, csrf_digest, "
                    " last_activity_at, expires_at, created_ip_digest, created_ua_digest) "
                    "VALUES ('as4', 'u-admin', 'digest-4', 'csrf-4', "
                    " '2026-08-22T00:00:01+00:00', '2026-08-21T00:00:00+00:00', "
                    " 'ip-digest', 'ua-digest')"
                )  # expires_at must be after created_at
            with pytest.raises(psycopg.errors.ForeignKeyViolation):
                conn.execute(
                    "INSERT INTO admin_sessions "
                    "(id, actor_user_id, session_digest, csrf_digest, "
                    " last_activity_at, expires_at, created_ip_digest, created_ua_digest) "
                    "VALUES ('as5', 'u-missing', 'digest-5', 'csrf-5', "
                    " '2026-08-22T00:00:01+00:00', '2099-01-01T00:00:00+00:00', "
                    " 'ip-digest', 'ua-digest')"
                )  # every session must trace back to a real actor

            indexes = {
                row[0]
                for row in conn.execute(
                    "SELECT indexname FROM pg_indexes WHERE tablename = 'admin_sessions'"
                ).fetchall()
            }
            assert any(name.startswith("idx_admin_sessions_actor_status") for name in indexes)
    finally:
        _drop_database(db_name)


def test_pg_billing_constraints_downgrade_guard() -> None:
    """026 downgrade refuses (loudly) once non-zpay / non-INTERNAL orders exist;
    an empty ledger downgrades symmetrically back to the 022 constraint set."""

    from alembic import command

    db_name = "t08_downgrade_guard"
    dsn = _t08_database(db_name)
    sqlalchemy_dsn = dsn.replace("postgresql://", "postgresql+psycopg://")
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            _insert_t08_order(
                conn,
                1,
                provider="activation_code",
                pricing_scope="CUSTOMER_STANDARD",
                status="PAID",
                charged_unit_price_fen_snapshot=1500,
                amount_fen=1500,
                credits=1,
                paid_at="2026-08-22T00:00:00+00:00",
            )

        with pytest.raises(RuntimeError, match="cannot downgrade 026"):
            # Three steps: 028->027 (empty idempotency ledger, symmetric)
            # then 027->026 (empty catalog, symmetric) then 026->025,
            # which the guard refuses. Alembic runs multi-step downgrades in
            # one transactional-DDL transaction, so the whole attempt rolls
            # back and the database stays atomically at head (025-guard
            # precedent in this file).
            command.downgrade(_alembic_config(sqlalchemy_dsn), "-3")
        with psycopg.connect(dsn) as conn:
            version = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        assert version == "028_admin_write_idempotency"

        # Remove the customer order (test data only — confirmed production rows
        # are never deleted, which is exactly why the guard exists) and the
        # downgrade becomes possible again. Three steps (028->027->026->025)
        # restore the 022 constraint set the final assertion exercises.
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute("DELETE FROM recharge_orders WHERE provider != 'zpay'")
        command.downgrade(_alembic_config(sqlalchemy_dsn), "-3")
        with psycopg.connect(dsn) as conn:
            version = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        assert version == "025_postgres_runtime_compatibility"
        # Back on 022 constraints, a customer-scope order is rejected again.
        with psycopg.connect(dsn, autocommit=True) as conn:
            with pytest.raises(psycopg.errors.CheckViolation):
                _insert_t08_order(conn, 2, pricing_scope="CUSTOMER_STANDARD")
    finally:
        _drop_database(db_name)
