from __future__ import annotations

import json
import os
import sqlite3
import stat
from pathlib import Path

import pytest

from app.backup import create_readonly_snapshot
from scripts.reconcile_customer_billing import (
    ColumnSpec,
    canonical_value,
    compute_table_digest,
    connect_sqlite_readonly,
    reconcile_connections,
    redact_postgres_dsn,
    safe_error_message,
    validate_asset_references_sqlite,
    validate_wallet_invariants,
)
from scripts.sqlite_to_postgres import (
    MigrationSafetyError,
    migrate_snapshot,
    require_maintenance_window,
    require_validated_postgres_foreign_keys,
    topological_order,
    validate_revision_pair,
)


def _basic_sqlite(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            PRAGMA foreign_keys = ON;
            CREATE TABLE users (id TEXT PRIMARY KEY);
            CREATE TABLE assets (
                id TEXT PRIMARY KEY,
                storage_uri TEXT NOT NULL,
                sha256 TEXT NOT NULL
            );
            CREATE TABLE wallets (
                user_id TEXT PRIMARY KEY REFERENCES users(id),
                available_credits INTEGER NOT NULL,
                reserved_credits INTEGER NOT NULL
            );
            CREATE TABLE recharge_orders (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(id),
                status TEXT NOT NULL,
                credits INTEGER NOT NULL
            );
            CREATE TABLE wallet_transactions (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(id),
                type TEXT NOT NULL,
                available_delta INTEGER NOT NULL,
                reserved_delta INTEGER NOT NULL,
                recharge_order_id TEXT REFERENCES recharge_orders(id),
                task_id TEXT,
                billing_round INTEGER
            );
            CREATE TABLE generation_tasks (
                id TEXT PRIMARY KEY,
                result_asset_id TEXT REFERENCES assets(id),
                provider_response_asset_id TEXT REFERENCES assets(id)
            );
            INSERT INTO users VALUES ('u1');
            INSERT INTO assets VALUES ('a1', 'cos://private/a1.mp4', 'abc');
            INSERT INTO wallets VALUES ('u1', 10, 0);
            INSERT INTO recharge_orders VALUES ('o1', 'u1', 'PAID', 10);
            INSERT INTO wallet_transactions VALUES ('tx1', 'u1', 'CHARGE', 10, 0, 'o1', NULL, NULL);
            INSERT INTO generation_tasks VALUES ('t1', 'a1', NULL);
            """
        )


def test_readonly_snapshot_preserves_source_and_is_private(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    snapshot = tmp_path / "snapshot.db"
    _basic_sqlite(source)
    before = source.read_bytes()
    before_mtime = source.stat().st_mtime_ns

    metadata = create_readonly_snapshot(source, snapshot)

    assert source.read_bytes() == before
    assert source.stat().st_mtime_ns == before_mtime
    assert metadata.snapshot_path == snapshot.resolve()
    assert len(metadata.sha256) == 64
    assert stat.S_IMODE(snapshot.stat().st_mode) == 0o600
    with sqlite3.connect(snapshot) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_snapshot_refuses_existing_evidence(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    snapshot = tmp_path / "snapshot.db"
    _basic_sqlite(source)
    snapshot.write_text("do-not-overwrite", encoding="utf-8")
    with pytest.raises(FileExistsError):
        create_readonly_snapshot(source, snapshot)
    assert snapshot.read_text(encoding="utf-8") == "do-not-overwrite"


def test_redaction_and_error_messages_never_expose_passwords() -> None:
    dsn = "postgresql://migration:super%40secret@db.example/customer"
    redacted = redact_postgres_dsn(dsn)
    message = safe_error_message(RuntimeError("failed for password super%40secret"), dsn)
    assert redacted == "postgresql://migration@db.example/customer"
    assert "super%40secret" not in message
    assert "super@secret" not in message


def test_table_digest_is_order_independent_and_separates_pk_from_rows(tmp_path: Path) -> None:
    first = tmp_path / "first.db"
    second = tmp_path / "second.db"
    fixtures = (
        (first, (("a", "one"), ("b", "two"))),
        (second, (("b", "two"), ("a", "one"))),
    )
    for path, rows in fixtures:
        with sqlite3.connect(path) as conn:
            conn.execute("CREATE TABLE sample (id TEXT PRIMARY KEY, value TEXT NOT NULL)")
            conn.executemany("INSERT INTO sample VALUES (?, ?)", rows)
    specs = (ColumnSpec("id", "text", False), ColumnSpec("value", "text", False))
    with sqlite3.connect(first) as left, sqlite3.connect(second) as right:
        left_digest = compute_table_digest(left, "sample", specs, ("id",))
        right_digest = compute_table_digest(right, "sample", specs, ("id",))
        assert left_digest == right_digest
        right.execute("UPDATE sample SET value = 'changed' WHERE id = 'b'")
        changed = compute_table_digest(right, "sample", specs, ("id",))
    assert changed.primary_key_sha256 == left_digest.primary_key_sha256
    assert changed.row_sha256 != left_digest.row_sha256


def test_wallet_and_asset_invariants_detect_drift(tmp_path: Path) -> None:
    database = tmp_path / "source.db"
    _basic_sqlite(database)
    with sqlite3.connect(database) as conn:
        conn.execute("UPDATE wallets SET available_credits = 9 WHERE user_id = 'u1'")
        conn.execute("DELETE FROM wallet_transactions WHERE id = 'tx1'")
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("UPDATE generation_tasks SET result_asset_id = 'missing' WHERE id = 't1'")
        wallet = validate_wallet_invariants(conn)
        assets = validate_asset_references_sqlite(conn)
    assert not wallet.ok
    assert wallet.wallet_balance_mismatches == 1
    assert wallet.paid_without_charge == 1
    assert not assets.ok
    assert assets.orphan_reference_count == 1
    assert assets.object_storage_checked is False


def test_canonical_values_are_stable() -> None:
    assert canonical_value(True, "boolean") is True
    assert canonical_value(1, "boolean") is True
    assert canonical_value(memoryview(b"abc"), "bytea") == {"base64": "YWJj"}
    assert canonical_value('{"b":2,"a":1}', "jsonb") == {"a": 1, "b": 2}


def test_revision_and_dependency_guards() -> None:
    validate_revision_pair(
        "025_postgres_runtime_compatibility",
        "025_postgres_runtime_compatibility",
    )
    with pytest.raises(MigrationSafetyError, match="expected T07 Alembic head"):
        validate_revision_pair("024_wallet_backfill", "024_wallet_backfill")
    order = topological_order(
        {"users", "projects", "assets"},
        {"projects": {"users"}, "assets": {"projects"}},
    )
    assert order.index("users") < order.index("projects") < order.index("assets")
    with pytest.raises(MigrationSafetyError, match="cycle"):
        topological_order({"a", "b"}, {"a": {"b"}, "b": {"a"}})


class _ScalarCursor:
    def __init__(self, value: int) -> None:
        self.value = value

    def fetchone(self) -> tuple[int]:
        return (self.value,)


class _ScalarConnection:
    def __init__(self, value: int) -> None:
        self.value = value

    def execute(self, query: str, params: object = None) -> _ScalarCursor:
        assert "convalidated" in query
        assert params is None
        return _ScalarCursor(self.value)


def test_unvalidated_postgres_foreign_keys_block_import() -> None:
    with pytest.raises(MigrationSafetyError, match="unvalidated foreign-key"):
        require_validated_postgres_foreign_keys(_ScalarConnection(2))
    require_validated_postgres_foreign_keys(_ScalarConnection(0))
    with pytest.raises(MigrationSafetyError, match="maintenance window"):
        require_maintenance_window(False)
    require_maintenance_window(True)


DEFAULT_PG_DSN = "postgresql://testuser:testpass@localhost:5433/customer_v3_test"
PG_SKIP = "PostgreSQL fixture not reachable; run scripts/pg-fixture.sh start"


def _pg_dsn() -> str:
    return os.environ.get("TEST_POSTGRESQL_URL", DEFAULT_PG_DSN)


def _pg_available() -> bool:
    try:
        import psycopg

        with psycopg.connect(_pg_dsn(), connect_timeout=3) as conn:
            return conn.execute("SELECT 1").fetchone()[0] == 1
    except Exception:
        return False


pg_only = pytest.mark.skipif(not _pg_available(), reason=PG_SKIP)


def _admin_dsn() -> str:
    return _pg_dsn().rsplit("/", 1)[0] + "/postgres"


def _database_dsn(name: str) -> str:
    return _pg_dsn().rsplit("/", 1)[0] + f"/{name}"


def _drop_database(name: str) -> None:
    import psycopg
    from psycopg import sql

    with psycopg.connect(_admin_dsn(), autocommit=True) as conn:
        statement = sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
            sql.Identifier(name)
        )
        conn.execute(statement)


def _create_database(name: str) -> str:
    import psycopg
    from psycopg import sql

    _drop_database(name)
    with psycopg.connect(_admin_dsn(), autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    return _database_dsn(name)


def _alembic_config(dsn: str):  # type: ignore[no-untyped-def]
    from alembic.config import Config

    server_dir = Path(__file__).resolve().parent.parent
    config = Config(str(server_dir / "alembic.ini"))
    config.set_main_option("script_location", str(server_dir / "migrations"))
    sqlalchemy_dsn = dsn.replace("postgresql://", "postgresql+psycopg://", 1)
    config.set_main_option("sqlalchemy.url", sqlalchemy_dsn)
    return config


def _upgrade_pg(dsn: str) -> None:
    from alembic import command

    command.upgrade(_alembic_config(dsn), "head")


def _create_head_source(path: Path) -> None:
    from app.db import connect_database, initialize_database

    conn = initialize_database(path)
    conn.close()
    with connect_database(path) as conn:
        conn.execute(
            "INSERT INTO users (id, username, display_name) VALUES (?, ?, ?)",
            ("u-t07", "t07-user", "T07 User"),
        )
        conn.execute(
            "INSERT INTO projects (id, owner_user_id, name) VALUES (?, ?, ?)",
            ("p-t07", "u-t07", "T07 Project"),
        )
        conn.execute(
            """
            INSERT INTO assets (
                id, project_id, kind, storage_uri, sha256, size_bytes,
                content_type, created_by_user_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "a-t07",
                "p-t07",
                "GENERATED_VIDEO",
                "cos://private/t07.mp4",
                "a" * 64,
                1024,
                "video/mp4",
                "u-t07",
            ),
        )
        conn.execute(
            """
            INSERT INTO generation_batches (
                id, project_id, created_by_user_id, idempotency_key,
                request_hash, request_snapshot_json, display_name
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ("b-t07", "p-t07", "u-t07", "t07-batch-idem", "hash", "{}", "T07 Batch"),
        )
        conn.execute(
            """
            INSERT INTO generation_tasks (
                id, batch_id, provider, model, status, archive_status,
                quality_status, result_asset_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "t-t07",
                "b-t07",
                "apilio",
                "test-model",
                "SUCCEEDED",
                "ARCHIVED",
                "PASSED",
                "a-t07",
            ),
        )
        conn.execute(
            "INSERT INTO wallets VALUES (?, ?, ?, CURRENT_TIMESTAMP)",
            ("u-t07", 10, 0),
        )
        conn.execute(
            """
            INSERT INTO recharge_orders (
                id, user_id, merchant_order_no, provider, provider_trade_no,
                channel, status, pricing_scope, base_unit_price_fen_snapshot,
                charged_unit_price_fen_snapshot, min_recharge_fen_snapshot,
                recharge_step_fen_snapshot, amount_fen, credits, paid_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "o-t07",
                "u-t07",
                "T07-ORDER",
                "zpay",
                "T07-TRADE",
                "alipay",
                "PAID",
                "INTERNAL",
                1000,
                1000,
                10000,
                1000,
                10000,
                10,
                "2026-08-21T00:00:00+00:00",
            ),
        )
        conn.execute(
            """
            INSERT INTO wallet_transactions (
                id, user_id, type, available_delta, reserved_delta,
                recharge_order_id, idempotency_key
            ) VALUES (?, ?, 'CHARGE', ?, 0, ?, ?)
            """,
            ("tx-t07", "u-t07", 10, "o-t07", "t07-charge"),
        )
        conn.commit()


@pg_only
def test_real_pg_import_reconcile_repeat_and_rollback(tmp_path: Path) -> None:
    import psycopg

    source = tmp_path / "source.db"
    snapshot_path = tmp_path / "snapshot.db"
    _create_head_source(source)
    snapshot = create_readonly_snapshot(source, snapshot_path)
    name = "t07_compact_import"
    dsn = _create_database(name)
    try:
        _upgrade_pg(dsn)
        result = migrate_snapshot(snapshot, dsn, batch_size=2)
        assert result.status == "imported"
        assert result.reconciliation.ok
        repeated = migrate_snapshot(snapshot, dsn, batch_size=2)
        assert repeated.status == "already_reconciled"
        with psycopg.connect(dsn) as conn:
            assert conn.execute("SELECT count(*) FROM users").fetchone()[0] == 1
            assert conn.execute("SELECT available_credits FROM wallets").fetchone()[0] == 10
    finally:
        _drop_database(name)

    rollback_name = "t07_compact_rollback"
    rollback_dsn = _create_database(rollback_name)
    try:
        _upgrade_pg(rollback_dsn)
        with pytest.raises(RuntimeError, match="injected failure"):
            migrate_snapshot(snapshot, rollback_dsn, fail_after_table="users")
        with psycopg.connect(rollback_dsn) as conn:
            assert conn.execute("SELECT count(*) FROM users").fetchone()[0] == 0
            assert conn.execute("SELECT count(*) FROM runtime_settings").fetchone()[0] == 1
    finally:
        _drop_database(rollback_name)


@pg_only
def test_real_pg_divergent_target_and_revision_mismatch_fail_closed(tmp_path: Path) -> None:
    import psycopg

    source = tmp_path / "source.db"
    _create_head_source(source)
    snapshot = create_readonly_snapshot(source, tmp_path / "snapshot.db")
    name = "t07_compact_guards"
    dsn = _create_database(name)
    try:
        _upgrade_pg(dsn)
        with psycopg.connect(dsn) as conn:
            conn.execute(
                "INSERT INTO users (id, username, display_name) VALUES (%s, %s, %s)",
                ("other", "other", "Other"),
            )
            conn.commit()
        with pytest.raises(MigrationSafetyError, match="non-empty"):
            migrate_snapshot(snapshot, dsn)
        with psycopg.connect(dsn) as conn:
            assert conn.execute("SELECT id FROM users").fetchall() == [("other",)]
            conn.execute("DELETE FROM users")
            conn.execute(
                "UPDATE alembic_version SET version_num = %s",
                ("024_wallet_backfill",),
            )
            conn.commit()
        with pytest.raises(MigrationSafetyError, match="Alembic revision mismatch"):
            migrate_snapshot(snapshot, dsn)
        with psycopg.connect(dsn) as conn:
            assert conn.execute("SELECT count(*) FROM users").fetchone()[0] == 0
    finally:
        _drop_database(name)


@pg_only
def test_real_pg_reconciliation_detects_pk_row_wallet_and_asset_drift(
    tmp_path: Path,
) -> None:
    import psycopg

    source = tmp_path / "source.db"
    _create_head_source(source)
    snapshot = create_readonly_snapshot(source, tmp_path / "snapshot.db")
    name = "t07_compact_drift"
    dsn = _create_database(name)
    try:
        _upgrade_pg(dsn)
        assert migrate_snapshot(snapshot, dsn).reconciliation.ok
        with psycopg.connect(dsn) as target:
            target.execute(
                "INSERT INTO users (id, username, display_name) VALUES (%s, %s, %s)",
                ("target-only", "target-only", "Target Only"),
            )
            target.execute(
                "UPDATE assets SET storage_uri = %s WHERE id = %s",
                ("cos://private/changed.mp4", "a-t07"),
            )
            target.execute(
                "DELETE FROM wallet_transactions WHERE id = %s",
                ("tx-t07",),
            )
            target.commit()
            with connect_sqlite_readonly(snapshot.snapshot_path) as source_conn:
                report = reconcile_connections(
                    source_conn,
                    target,
                    source_snapshot_sha256=snapshot.sha256,
                    target_dsn=dsn,
                )
        assert not report.ok
        assert "TABLE_DIGEST_MISMATCH:users" in report.issues
        assert "TABLE_DIGEST_MISMATCH:assets" in report.issues
        assert "TARGET_WALLET_INVARIANT_FAILED" in report.issues
    finally:
        _drop_database(name)
