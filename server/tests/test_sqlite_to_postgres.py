from __future__ import annotations

import os
import sqlite3
import stat
import sys
from pathlib import Path

import pytest

from app import backup as backup_module
from app.backup import create_readonly_snapshot
from scripts.reconcile_customer_billing import (
    ColumnSpec,
    canonical_value,
    compute_table_digest,
    connect_sqlite_readonly,
    reconcile_connection_pair,
    redact_postgres_dsn,
    safe_error_message,
    validate_database_invariants,
)
from scripts.sqlite_to_postgres import (
    MIGRATION_ADVISORY_LOCK_KEYS,
    MigrationReconciliationError,
    MigrationSafetyError,
    migrate_snapshot,
    require_maintenance_window,
    require_migration_lock,
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
            CREATE TABLE characters (
                id TEXT PRIMARY KEY,
                reference_asset_ids_json TEXT NOT NULL
            );
            CREATE TABLE character_reference_selections (
                id TEXT PRIMARY KEY,
                recommended_asset_ids_json TEXT NOT NULL,
                selected_asset_ids_json TEXT NOT NULL
            );
            INSERT INTO users VALUES ('u1');
            INSERT INTO assets VALUES ('a1', 'cos://private/a1.mp4', 'abc');
            INSERT INTO wallets VALUES ('u1', 10, 0);
            INSERT INTO recharge_orders VALUES ('o1', 'u1', 'PAID', 10);
            INSERT INTO wallet_transactions VALUES ('tx1', 'u1', 'CHARGE', 10, 0, 'o1', NULL, NULL);
            INSERT INTO generation_tasks VALUES ('t1', 'a1', NULL);
            INSERT INTO characters VALUES ('c1', '["a1"]');
            INSERT INTO character_reference_selections VALUES ('s1', '["a1"]', '["a1"]');
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
    assert metadata.path == snapshot.resolve()
    assert metadata.snapshot_path == snapshot.resolve()
    assert metadata.sha256 == metadata.snapshot_sha256
    assert len(metadata.source_sha256) == 64
    assert len(metadata.snapshot_sha256) == 64
    # Windows reports a synthesized 0o666 mode for every file; the 0600
    # privacy assertion is POSIX-only (mirrors the guard in app/backup.py).
    if sys.platform != "win32":
        assert stat.S_IMODE(snapshot.stat().st_mode) == 0o600
    with sqlite3.connect(snapshot) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


class _TrackingConnection:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self.closed = False

    def __getattr__(self, name: str):  # type: ignore[no-untyped-def]
        return getattr(self.connection, name)

    def __enter__(self) -> _TrackingConnection:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if exc_type is None:
            self.connection.commit()
        else:
            self.connection.rollback()

    def close(self) -> None:
        self.closed = True
        self.connection.close()


def test_snapshot_closes_readonly_connection_deterministically(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.db"
    snapshot = tmp_path / "snapshot.db"
    _basic_sqlite(source)
    tracking = _TrackingConnection(backup_module._readonly_connection(source))

    def open_tracking(_: Path) -> _TrackingConnection:
        return tracking

    monkeypatch.setattr(backup_module, "_readonly_connection", open_tracking)
    create_readonly_snapshot(source, snapshot)

    assert tracking.closed


def test_snapshot_publish_rolls_back_when_temp_cleanup_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    temporary = tmp_path / ".snapshot.tmp"
    destination = tmp_path / "snapshot.db"
    temporary.write_bytes(b"immutable-evidence")
    original_unlink = Path.unlink

    def fail_temporary_unlink(self: Path, missing_ok: bool = False) -> None:
        if self == temporary:
            raise OSError("injected temporary cleanup failure")
        original_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", fail_temporary_unlink)
    with pytest.raises(OSError, match="injected temporary cleanup failure"):
        backup_module._publish_without_overwrite(temporary, destination)

    assert temporary.exists()
    assert not destination.exists()


def test_snapshot_writer_fence_blocks_resumed_writer_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A resumed writer cannot commit while the fence is held (PR review P1).

    The old TOCTOU window (writer landing between the final hash/stat checks
    and the metadata computation) is closed by holding BEGIN IMMEDIATE for the
    whole snapshot creation: the resumed writer now fails with SQLITE_BUSY
    instead of silently changing the source after the checks passed.
    """

    source = tmp_path / "source.db"
    snapshot = tmp_path / "snapshot.db"
    _basic_sqlite(source)
    before = source.read_bytes()
    original_publish = backup_module._publish_without_overwrite

    def attempt_write_during_publish(temporary: Path, destination: Path) -> None:
        writer = sqlite3.connect(source, timeout=0)
        try:
            with pytest.raises(sqlite3.OperationalError):
                writer.execute("UPDATE wallets SET available_credits = 11 WHERE user_id = 'u1'")
                writer.commit()
        finally:
            writer.close()
        original_publish(temporary, destination)

    monkeypatch.setattr(backup_module, "_publish_without_overwrite", attempt_write_during_publish)
    metadata = create_readonly_snapshot(source, snapshot)

    assert source.read_bytes() == before
    assert metadata.path == snapshot.resolve()


def test_snapshot_writer_fence_blocks_concurrent_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.db"
    _basic_sqlite(source)
    original_publish = backup_module._publish_without_overwrite

    def concurrent_snapshot_during_publish(temporary: Path, destination: Path) -> None:
        with pytest.raises(RuntimeError, match="migration writer fence"):
            create_readonly_snapshot(source, tmp_path / "second.db")
        original_publish(temporary, destination)

    monkeypatch.setattr(
        backup_module, "_publish_without_overwrite", concurrent_snapshot_during_publish
    )
    create_readonly_snapshot(source, tmp_path / "first.db")
    # The fence is released once the snapshot completes: a later snapshot succeeds.
    create_readonly_snapshot(source, tmp_path / "third.db")


def test_snapshot_final_recheck_detects_direct_file_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Byte-level mutation that bypasses SQLite locking is still caught.

    POSIX advisory locks only constrain cooperating processes; a direct file
    write after publication is the last TOCTOU variant and must still fail
    the final hash/stat re-check (hash first, then stat) and roll the
    published snapshot back.
    """

    source = tmp_path / "source.db"
    snapshot = tmp_path / "snapshot.db"
    _basic_sqlite(source)
    original_publish = backup_module._publish_without_overwrite

    def mutate_file_after_publish(temporary: Path, destination: Path) -> None:
        original_publish(temporary, destination)
        with open(source, "r+b") as stream:
            stream.write(b"X")

    monkeypatch.setattr(backup_module, "_publish_without_overwrite", mutate_file_after_publish)
    with pytest.raises(RuntimeError, match="source changed"):
        create_readonly_snapshot(source, snapshot)

    assert not snapshot.exists()


def test_snapshot_refuses_existing_evidence(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    snapshot = tmp_path / "snapshot.db"
    _basic_sqlite(source)
    snapshot.write_text("do-not-overwrite", encoding="utf-8")

    with pytest.raises(FileExistsError):
        create_readonly_snapshot(source, snapshot)

    assert snapshot.read_text(encoding="utf-8") == "do-not-overwrite"


def test_snapshot_rejects_active_wal_sidecars(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    _basic_sqlite(source)
    writer = sqlite3.connect(source)
    try:
        assert writer.execute("PRAGMA journal_mode = WAL").fetchone()[0].lower() == "wal"
        writer.execute("UPDATE wallets SET available_credits = 11 WHERE user_id = 'u1'")
        writer.execute(
            "INSERT INTO wallet_transactions VALUES ('tx2', 'u1', 'CHARGE', 1, 0, NULL, NULL, NULL)"
        )
        writer.commit()
        assert Path(f"{source}-wal").exists()
        with pytest.raises(RuntimeError, match="WAL/journal sidecars"):
            create_readonly_snapshot(source, tmp_path / "blocked.db")
    finally:
        writer.close()

    # The WAL file is gone after the last close, but the persistent WAL
    # journal-mode flag in the header remains: the next ordinary connection
    # (including the snapshot writer fence) would resurrect -wal/-shm. The
    # maintenance-window contract requires the source to end in DELETE mode,
    # so the snapshot fails closed with an explicit switch instruction.
    assert not Path(f"{source}-wal").exists()
    with pytest.raises(RuntimeError, match="WAL journal mode"):
        create_readonly_snapshot(source, tmp_path / "after-close.db")

    with sqlite3.connect(source) as switcher:
        assert switcher.execute("PRAGMA journal_mode = DELETE").fetchone()[0].lower() == "delete"
    metadata = create_readonly_snapshot(source, tmp_path / "after-delete.db")
    assert metadata.path.exists()


def test_redaction_and_error_messages_never_expose_passwords() -> None:
    dsn = (
        "postgresql://migration:super%40secret@db.example/customer"
        "?sslpassword=query%40sensitive&application_name=t07#fragment%40sensitive"
    )
    redacted = redact_postgres_dsn(dsn)
    message = safe_error_message(
        RuntimeError(
            f"failed for {dsn}; credentials: super@secret, query@sensitive, "
            "query%40sensitive, fragment@sensitive, sslpassword"
        ),
        dsn,
    )
    assert redacted == "postgresql://migration@db.example/customer"
    assert (
        redact_postgres_dsn("postgresql://migration:hidden@db.example:notaport/customer")
        == "<redacted-postgres-dsn>"
    )
    for sensitive_value in (
        "super%40secret",
        "super@secret",
        "query@sensitive",
        "query%40sensitive",
        "fragment@sensitive",
        "sslpassword",
    ):
        assert sensitive_value not in message


def test_safe_error_message_bounds_untrusted_exception_text() -> None:
    """Driver/conversion errors degrade to class + stage, never raw text (PR review P1)."""

    dsn = "postgresql://migration:secret@db.example/customer"
    leaky_value_error = ValueError(
        "invalid literal for int() with base 10: 'cos://private/customer-row.mp4'"
    )
    bounded = safe_error_message(leaky_value_error, dsn, stage="import")
    assert "cos://private/customer-row.mp4" not in bounded
    assert "ValueError" in bounded
    assert "import" in bounded

    leaky_driver_error = sqlite3.DatabaseError(
        "DETAIL:  Failing row contains (cos://private/secret.mp4, token-digest-123)"
    )
    bounded_driver = safe_error_message(leaky_driver_error, dsn, stage="reconciliation")
    assert "secret.mp4" not in bounded_driver
    assert "token-digest-123" not in bounded_driver
    assert "DatabaseError" in bounded_driver
    assert "reconciliation" in bounded_driver

    oversized = safe_error_message(RuntimeError("x" * 10_000 + " trailing-secret"), dsn)
    assert len(oversized) < 10_000
    assert oversized.endswith("…<truncated>")


def test_wallet_mismatch_detail_reports_counts_only(tmp_path: Path) -> None:
    """Wallet drift reports carry counts, never exact balances (PR review P2)."""

    database = tmp_path / "source.db"
    _basic_sqlite(database)
    with sqlite3.connect(database) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("UPDATE wallets SET available_credits = 987654321 WHERE user_id = 'u1'")
        conn.commit()
        issues = validate_database_invariants(conn, "sqlite", "source")

    wallet_issues = [issue for issue in issues if issue.code == "wallet_balance_mismatch"]
    assert wallet_issues
    for issue in wallet_issues:
        assert "987654321" not in issue.detail
        assert "available=" not in issue.detail
        assert "reserved=" not in issue.detail


def _billing_sqlite(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            PRAGMA foreign_keys = ON;
            CREATE TABLE generation_batches (
                id TEXT PRIMARY KEY,
                created_by_user_id TEXT NOT NULL
            );
            CREATE TABLE generation_tasks (
                id TEXT PRIMARY KEY,
                batch_id TEXT NOT NULL REFERENCES generation_batches(id)
            );
            CREATE TABLE wallet_transactions (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                type TEXT NOT NULL,
                available_delta INTEGER NOT NULL,
                reserved_delta INTEGER NOT NULL,
                recharge_order_id TEXT,
                task_id TEXT,
                billing_round INTEGER
            );
            INSERT INTO generation_batches VALUES ('b-owner', 'u-owner');
            INSERT INTO generation_batches VALUES ('b-other', 'u-other');
            INSERT INTO generation_tasks VALUES ('t-double', 'b-owner');
            INSERT INTO generation_tasks VALUES ('t-wrong', 'b-other');
            INSERT INTO generation_tasks VALUES ('t-gap', 'b-owner');
            -- t-double: one round with two RESERVE rows -> round mismatch
            INSERT INTO wallet_transactions VALUES
                ('w1', 'u-owner', 'RESERVE', 0, 5, NULL, 't-double', 1);
            INSERT INTO wallet_transactions VALUES
                ('w2', 'u-owner', 'RESERVE', 0, 5, NULL, 't-double', 1);
            -- t-wrong: billing rows not owned by the batch owner
            INSERT INTO wallet_transactions VALUES
                ('w3', 'u-owner', 'RESERVE', 0, 5, NULL, 't-wrong', 1);
            INSERT INTO wallet_transactions VALUES
                ('w4', 'u-owner', 'SETTLE', 3, -5, NULL, 't-wrong', 1);
            -- t-gap: billing starts at round 2 -> non-contiguous rounds
            INSERT INTO wallet_transactions VALUES
                ('w5', 'u-owner', 'RESERVE', 0, 5, NULL, 't-gap', 2);
            INSERT INTO wallet_transactions VALUES
                ('w6', 'u-owner', 'RELEASE', 0, -5, NULL, 't-gap', 2);
            """
        )


def test_generation_billing_invariants_detect_drift(tmp_path: Path) -> None:
    database = tmp_path / "billing.db"
    _billing_sqlite(database)
    with sqlite3.connect(database) as conn:
        conn.row_factory = sqlite3.Row
        issues = validate_database_invariants(conn, "sqlite", "source")

    codes = {issue.code for issue in issues}
    assert "generation_billing_round_mismatch" in codes
    assert "generation_billing_owner_mismatch" in codes
    assert "generation_billing_round_gap" in codes
    for issue in issues:
        assert "u-owner" not in issue.detail
        assert "u-other" not in issue.detail


def test_table_digest_is_order_independent_streaming_and_separates_pk(tmp_path: Path) -> None:
    first = tmp_path / "first.db"
    second = tmp_path / "second.db"
    fixtures = (
        (first, (("a", "one"), ("b", "two"), ("c", "three"))),
        (second, (("c", "three"), ("b", "two"), ("a", "one"))),
    )
    for path, rows in fixtures:
        with sqlite3.connect(path) as conn:
            conn.execute("CREATE TABLE sample (id TEXT PRIMARY KEY, value TEXT NOT NULL)")
            conn.executemany("INSERT INTO sample VALUES (?, ?)", rows)
    specs = (ColumnSpec("id", "text", False), ColumnSpec("value", "text", False))
    with connect_sqlite_readonly(first) as left, connect_sqlite_readonly(second) as right:
        left_digest = compute_table_digest(
            left,
            "sample",
            specs,
            ("id",),
            dialect="sqlite",
            batch_size=1,
        )
        right_digest = compute_table_digest(
            right,
            "sample",
            specs,
            ("id",),
            dialect="sqlite",
            batch_size=2,
        )
    assert left_digest == right_digest

    with sqlite3.connect(second) as conn:
        conn.execute("UPDATE sample SET value = 'changed' WHERE id = 'b'")
    with connect_sqlite_readonly(second) as changed_conn:
        changed = compute_table_digest(
            changed_conn,
            "sample",
            specs,
            ("id",),
            dialect="sqlite",
            batch_size=1,
        )
    assert changed.primary_key_sha256 == left_digest.primary_key_sha256
    assert changed.row_sha256 != left_digest.row_sha256


def test_wallet_scalar_and_json_asset_invariants_detect_drift(tmp_path: Path) -> None:
    database = tmp_path / "source.db"
    _basic_sqlite(database)
    with sqlite3.connect(database) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("UPDATE wallets SET available_credits = 9 WHERE user_id = 'u1'")
        conn.execute("DELETE FROM wallet_transactions WHERE id = 'tx1'")
        conn.execute("UPDATE generation_tasks SET result_asset_id = 'missing' WHERE id = 't1'")
        conn.execute("UPDATE characters SET reference_asset_ids_json = '[\"missing-json\"]'")
        conn.execute(
            "UPDATE character_reference_selections "
            "SET recommended_asset_ids_json = '[\"missing-recommended\"]', "
            "selected_asset_ids_json = '[\"missing-selected\"]'"
        )
        conn.commit()
        issues = validate_database_invariants(conn, "sqlite", "source")

    codes = [issue.code for issue in issues]
    scopes = [issue.scope for issue in issues]
    assert "wallet_balance_mismatch" in codes
    assert "paid_order_charge_mismatch" in codes
    assert "asset_reference_orphan" in codes
    assert "source:generation_tasks.result_asset_id" in scopes
    assert "source:characters.reference_asset_ids_json" in scopes
    assert "source:character_reference_selections.recommended_asset_ids_json" in scopes
    assert "source:character_reference_selections.selected_asset_ids_json" in scopes


def test_invalid_asset_reference_json_fails_closed(tmp_path: Path) -> None:
    database = tmp_path / "source.db"
    _basic_sqlite(database)
    with sqlite3.connect(database) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("UPDATE characters SET reference_asset_ids_json = '{bad-json'")
        conn.commit()
        issues = validate_database_invariants(conn, "sqlite", "source")
    assert any(issue.code == "asset_reference_json_invalid" for issue in issues)


def test_canonical_values_are_stable() -> None:
    assert canonical_value(True, "boolean") is True
    assert canonical_value(1, "boolean") is True
    assert canonical_value(memoryview(b"abc"), "bytea") == {
        "bytes_sha256": "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
        "size": 3,
    }
    assert canonical_value('{"b":2,"a":1}', "jsonb") == {"a": 1, "b": 2}


def test_revision_dependency_and_maintenance_guards() -> None:
    head = "026_customer_security_and_billing"
    validate_revision_pair(head, head, expected_head=head)
    with pytest.raises(MigrationSafetyError, match="Alembic revision mismatch"):
        validate_revision_pair("024_wallet_backfill", head, expected_head=head)

    order = topological_order(
        {"users", "projects", "assets"},
        {"projects": {"users"}, "assets": {"projects"}},
    )
    assert order.index("users") < order.index("projects") < order.index("assets")
    with pytest.raises(MigrationSafetyError, match="cycle"):
        topological_order({"a", "b"}, {"a": {"b"}, "b": {"a"}})

    with pytest.raises(MigrationSafetyError, match="maintenance window"):
        require_maintenance_window(False)
    require_maintenance_window(True)


class _ScalarCursor:
    def __init__(self, value: int) -> None:
        self.value = value

    def fetchone(self) -> tuple[int]:
        return (self.value,)


class _ScalarConnection:
    def __init__(self, value: int) -> None:
        self.value = value

    def execute(self, query: str) -> _ScalarCursor:
        assert "convalidated" in query
        return _ScalarCursor(self.value)


def test_unvalidated_postgres_foreign_keys_block_import() -> None:
    with pytest.raises(MigrationSafetyError, match="unvalidated foreign-key"):
        require_validated_postgres_foreign_keys(_ScalarConnection(2))
    require_validated_postgres_foreign_keys(_ScalarConnection(0))


class _BooleanCursor:
    def __init__(self, value: bool) -> None:
        self.value = value

    def fetchone(self) -> tuple[bool]:
        return (self.value,)


class _AdvisoryLockConnection:
    def __init__(self, acquired: bool) -> None:
        self.acquired = acquired

    def execute(self, query: str, params: tuple[int, int]) -> _BooleanCursor:
        assert "pg_try_advisory_xact_lock" in query
        assert params == MIGRATION_ADVISORY_LOCK_KEYS
        return _BooleanCursor(self.acquired)


def test_migration_advisory_lock_fails_closed() -> None:
    require_migration_lock(_AdvisoryLockConnection(True))
    with pytest.raises(MigrationSafetyError, match="another T07 migration"):
        require_migration_lock(_AdvisoryLockConnection(False))


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
        statement = sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
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
            "INSERT INTO characters (id, name, reference_asset_ids_json) VALUES (?, ?, ?)",
            ("c-t07", "T07 Character", '["a-t07"]'),
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
    conn.close()

    with sqlite3.connect(path) as checkpoint:
        checkpoint.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        checkpoint.execute("PRAGMA journal_mode = DELETE")


@pg_only
def test_real_pg_migration_advisory_lock_blocks_concurrent_cutover(
    tmp_path: Path,
) -> None:
    import psycopg

    source = tmp_path / "source.db"
    _create_head_source(source)
    snapshot = create_readonly_snapshot(source, tmp_path / "snapshot.db")
    name = "t07_compact_lock"
    dsn = _create_database(name)
    try:
        _upgrade_pg(dsn)
        with psycopg.connect(dsn) as blocker:
            with blocker.transaction():
                blocker.execute(
                    "SELECT pg_advisory_xact_lock(%s, %s)",
                    MIGRATION_ADVISORY_LOCK_KEYS,
                )
                with pytest.raises(
                    MigrationSafetyError,
                    match="another T07 migration",
                ):
                    migrate_snapshot(snapshot, dsn)
    finally:
        _drop_database(name)


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
    from psycopg.rows import dict_row

    source = tmp_path / "source.db"
    _create_head_source(source)
    snapshot = create_readonly_snapshot(source, tmp_path / "snapshot.db")
    name = "t07_compact_drift"
    dsn = _create_database(name)
    try:
        _upgrade_pg(dsn)
        assert migrate_snapshot(snapshot, dsn).reconciliation.ok
        with psycopg.connect(dsn, row_factory=dict_row) as target:
            target.execute(
                "INSERT INTO users (id, username, display_name) VALUES (%s, %s, %s)",
                ("target-only", "target-only", "Target Only"),
            )
            target.execute(
                "UPDATE assets SET storage_uri = %s WHERE id = %s",
                ("cos://private/changed.mp4", "a-t07"),
            )
            target.execute(
                "UPDATE characters SET reference_asset_ids_json = %s::jsonb WHERE id = %s",
                ('["missing-target-json"]', "c-t07"),
            )
            target.execute("DELETE FROM wallet_transactions WHERE id = %s", ("tx-t07",))
            target.commit()
            with connect_sqlite_readonly(snapshot.path) as source_conn:
                report = reconcile_connection_pair(source_conn, target)
        assert not report.ok
        issue_pairs = {(issue.code, issue.scope) for issue in report.issues}
        assert ("table_primary_key_mismatch", "users") in issue_pairs
        assert ("table_hash_mismatch", "assets") in issue_pairs
        assert (
            "asset_reference_orphan",
            "target:characters.reference_asset_ids_json",
        ) in issue_pairs
        assert any(issue.code == "wallet_balance_mismatch" for issue in report.issues)
    finally:
        _drop_database(name)


@pg_only
def test_real_pg_import_rejects_non_empty_target_only_table(tmp_path: Path) -> None:
    """A non-empty PG-only table (admin_sessions, revision 026) is divergent
    state: the T07 cutover happens before the customer production line opens,
    so the import must fail closed on the table contract."""

    import psycopg

    source = tmp_path / "source.db"
    _create_head_source(source)
    snapshot = create_readonly_snapshot(source, tmp_path / "snapshot.db")
    name = "t08_pg_only_guard"
    dsn = _create_database(name)
    try:
        _upgrade_pg(dsn)
        with psycopg.connect(dsn) as conn:
            conn.execute(
                "INSERT INTO users (id, username, display_name, role) "
                "VALUES ('u-admin', 'u-admin', 'Admin', 'admin')"
            )
            conn.execute(
                "INSERT INTO admin_sessions "
                "(id, actor_user_id, session_digest, csrf_digest, "
                " last_activity_at, expires_at, created_ip_digest, created_ua_digest) "
                "VALUES ('as1', 'u-admin', 'digest-1', 'csrf-1', "
                " '2026-08-22T00:00:00+00:00', '2099-01-01T00:00:00+00:00', "
                " 'ip-digest', 'ua-digest')"
            )
            conn.commit()
        with pytest.raises(MigrationSafetyError, match="table contract differs"):
            migrate_snapshot(snapshot, dsn)
    finally:
        _drop_database(name)


@pg_only
def test_real_pg_import_rejects_json_asset_orphan_before_writing_target(
    tmp_path: Path,
) -> None:
    import psycopg

    source = tmp_path / "source.db"
    _create_head_source(source)
    with sqlite3.connect(source) as conn:
        conn.execute(
            "UPDATE characters SET reference_asset_ids_json = '[\"missing-asset\"]' "
            "WHERE id = 'c-t07'"
        )
        conn.commit()
    snapshot = create_readonly_snapshot(source, tmp_path / "snapshot.db")
    name = "t07_json_asset_guard"
    dsn = _create_database(name)
    try:
        _upgrade_pg(dsn)
        with pytest.raises(MigrationReconciliationError, match="asset_reference_orphan"):
            migrate_snapshot(snapshot, dsn)
        with psycopg.connect(dsn) as conn:
            assert conn.execute("SELECT count(*) FROM users").fetchone()[0] == 0
    finally:
        _drop_database(name)
