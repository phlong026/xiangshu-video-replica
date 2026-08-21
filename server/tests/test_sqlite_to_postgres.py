"""T07 / DB-05 / DB-06: SQLite-to-PostgreSQL import and reconciliation."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from alembic import command
from alembic.config import Config

from app.backup import create_readonly_snapshot, sha256_file
from app.db import connect_database, initialize_database
from scripts import sqlite_to_postgres
from scripts.reconcile_customer_billing import reconcile_databases
from scripts.sqlite_to_postgres import (
    MigrationPreconditionError,
    MigrationReconciliationError,
    import_sqlite_to_postgres,
)

DEFAULT_DSN = "postgresql://testuser:testpass@localhost:5433/customer_v3_test"
SKIP_REASON = "PostgreSQL fixture not reachable; start it via scripts/pg-fixture.sh start"


def _pg_dsn() -> str:
    return os.environ.get("TEST_POSTGRESQL_URL", DEFAULT_DSN)


def _pg_available(dsn: str) -> bool:
    try:
        with psycopg.connect(dsn, connect_timeout=3) as conn:
            conn.execute("SELECT 1")
    except Exception:
        return False
    return True


pytestmark = pytest.mark.skipif(not _pg_available(_pg_dsn()), reason=SKIP_REASON)


def _admin_dsn() -> str:
    return _pg_dsn().rsplit("/", 1)[0] + "/postgres"


def _database_dsn(name: str) -> str:
    return _pg_dsn().rsplit("/", 1)[0] + f"/{name}"


def _sqlalchemy_dsn(dsn: str) -> str:
    return dsn.replace("postgresql://", "postgresql+psycopg://", 1)


def _alembic_config(dsn: str) -> Config:
    server_dir = Path(__file__).resolve().parent.parent
    config = Config(str(server_dir / "alembic.ini"))
    config.set_main_option("script_location", str(server_dir / "migrations"))
    config.set_main_option("sqlalchemy.url", _sqlalchemy_dsn(dsn))
    return config


@pytest.fixture
def postgres_database() -> Iterator[str]:
    name = f"t07_{uuid4().hex[:12]}"
    with psycopg.connect(_admin_dsn(), autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    dsn = _database_dsn(name)
    command.upgrade(_alembic_config(dsn), "head")
    try:
        yield dsn
    finally:
        with psycopg.connect(_admin_dsn(), autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def _seed_source(path: Path) -> Path:
    with initialize_database(path) as conn:
        conn.execute(
            "INSERT INTO users (id, username, display_name) VALUES (?, ?, ?)",
            ("user-1", "customer-one", "Customer One"),
        )
        conn.execute(
            "INSERT INTO projects (id, owner_user_id, name) VALUES (?, ?, ?)",
            ("project-1", "user-1", "Migration Project"),
        )
        conn.execute(
            """
            INSERT INTO assets (
                id, project_id, kind, storage_uri, sha256, size_bytes,
                content_type, created_by_user_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "asset-1",
                "project-1",
                "generation_result",
                "cos://private-bucket/results/asset-1.mp4",
                "a" * 64,
                1234,
                "video/mp4",
                "user-1",
            ),
        )
        conn.execute(
            """
            INSERT INTO generation_batches (
                id, project_id, created_by_user_id, idempotency_key,
                request_hash, request_snapshot_json, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "batch-1",
                "project-1",
                "user-1",
                "batch-idem-1",
                "batch-hash-1",
                "{}",
                "SUCCEEDED",
            ),
        )
        conn.execute(
            """
            INSERT INTO generation_tasks (
                id, batch_id, provider, model, status, archive_status,
                quality_status, result_asset_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "task-1",
                "batch-1",
                "apilio",
                "test-model",
                "SUCCEEDED",
                "ARCHIVED",
                "PASSED",
                "asset-1",
            ),
        )
        conn.execute(
            "INSERT INTO wallets (user_id, available_credits, reserved_credits) VALUES (?, ?, ?)",
            ("user-1", 3, 0),
        )
        conn.execute(
            """
            INSERT INTO recharge_orders (
                id, user_id, merchant_order_no, provider, provider_trade_no,
                channel, status, pricing_scope, base_unit_price_fen_snapshot,
                charged_unit_price_fen_snapshot, min_recharge_fen_snapshot,
                recharge_step_fen_snapshot, amount_fen, credits, notify_digest,
                paid_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (
                "order-1",
                "user-1",
                "merchant-1",
                "zpay",
                "trade-1",
                "alipay",
                "PAID",
                "INTERNAL",
                1000,
                1000,
                1000,
                1000,
                3000,
                3,
                "notify-digest-1",
            ),
        )
        conn.execute(
            """
            INSERT INTO wallet_transactions (
                id, user_id, type, available_delta, reserved_delta,
                recharge_order_id, idempotency_key
            ) VALUES (?, ?, 'CHARGE', ?, 0, ?, ?)
            """,
            ("tx-charge-1", "user-1", 3, "order-1", "zpay:charge:order-1"),
        )
        conn.commit()
    return path


def test_readonly_snapshot_does_not_mutate_source(tmp_path: Path) -> None:
    source = _seed_source(tmp_path / "source.db")
    before = sha256_file(source)

    snapshot = create_readonly_snapshot(source, tmp_path / "snapshot.db")

    assert snapshot.source_sha256 == before
    assert len(snapshot.snapshot_sha256) == 64
    assert sha256_file(source) == before
    with sqlite3.connect(f"{snapshot.path.as_uri()}?mode=ro", uri=True) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM users")


def test_cli_requires_maintenance_window_confirmation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _seed_source(tmp_path / "source.db")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "sqlite_to_postgres.py",
            "--sqlite",
            str(source),
            "--postgres-url",
            "postgresql://redacted.invalid/customer",
        ],
    )

    with pytest.raises(SystemExit) as exc_info:
        sqlite_to_postgres.main()

    assert exc_info.value.code == 2


def test_import_and_reconcile_happy_path(tmp_path: Path, postgres_database: str) -> None:
    source = _seed_source(tmp_path / "source.db")

    result = import_sqlite_to_postgres(
        source,
        postgres_database,
        snapshot_path=tmp_path / "snapshot.db",
    )

    assert result.status == "imported"
    assert result.reconciliation.ok
    assert result.source_sha256 == sha256_file(source)
    payload = json.dumps(result.to_dict(), sort_keys=True)
    assert "testpass" not in payload
    assert "customer-one" not in payload
    assert "cos://private-bucket/results/asset-1.mp4" not in payload
    assert result.reconciliation.tables
    for table in result.reconciliation.tables:
        assert table.source_count == table.target_count
        assert table.source_pk_sha256 == table.target_pk_sha256
        assert table.source_rows_sha256 == table.target_rows_sha256
        assert len(table.source_rows_sha256) == 64
    with psycopg.connect(postgres_database) as conn:
        assert conn.execute("SELECT count(*) FROM users").fetchone()[0] == 1
        assert conn.execute("SELECT available_credits FROM wallets").fetchone()[0] == 3


def test_repeated_import_is_idempotent(tmp_path: Path, postgres_database: str) -> None:
    source = _seed_source(tmp_path / "source.db")
    import_sqlite_to_postgres(source, postgres_database, snapshot_path=tmp_path / "first.db")

    repeated = import_sqlite_to_postgres(
        source,
        postgres_database,
        snapshot_path=tmp_path / "second.db",
    )

    assert repeated.status == "already_reconciled"
    assert repeated.reconciliation.ok
    with psycopg.connect(postgres_database) as conn:
        assert conn.execute("SELECT count(*) FROM users").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM wallet_transactions").fetchone()[0] == 1


def test_import_rolls_back_on_injected_failure(
    tmp_path: Path,
    postgres_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _seed_source(tmp_path / "source.db")
    source_sha256 = sha256_file(source)
    snapshot_path = tmp_path / "snapshot.db"
    original = sqlite_to_postgres._insert_table_rows
    calls = 0

    def fail_after_first_table(*args: object, **kwargs: object) -> int:
        nonlocal calls
        calls += 1
        inserted = original(*args, **kwargs)
        if calls == 1:
            raise RuntimeError("injected T07 failure")
        return inserted

    monkeypatch.setattr(sqlite_to_postgres, "_insert_table_rows", fail_after_first_table)

    with pytest.raises(RuntimeError, match="injected T07 failure"):
        import_sqlite_to_postgres(
            source,
            postgres_database,
            snapshot_path=snapshot_path,
        )

    assert sha256_file(source) == source_sha256
    assert snapshot_path.is_file()
    with sqlite3.connect(f"{source.as_uri()}?mode=ro", uri=True) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("SELECT count(*) FROM users").fetchone()[0] == 1
    with sqlite3.connect(f"{snapshot_path.as_uri()}?mode=ro", uri=True) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("SELECT count(*) FROM wallet_transactions").fetchone()[0] == 1
    with psycopg.connect(postgres_database) as conn:
        assert conn.execute("SELECT count(*) FROM users").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM wallet_transactions").fetchone()[0] == 0


def test_unknown_source_revision_is_rejected(tmp_path: Path, postgres_database: str) -> None:
    source = _seed_source(tmp_path / "source.db")
    with connect_database(source) as conn:
        conn.execute("UPDATE alembic_version SET version_num = 'unknown_t07_revision'")
        conn.commit()

    with pytest.raises(MigrationPreconditionError, match="source Alembic revision"):
        import_sqlite_to_postgres(
            source,
            postgres_database,
            snapshot_path=tmp_path / "snapshot.db",
        )


def test_divergent_non_seed_target_is_rejected(tmp_path: Path, postgres_database: str) -> None:
    source = _seed_source(tmp_path / "source.db")
    with psycopg.connect(postgres_database) as conn:
        conn.execute(
            "INSERT INTO users (id, username, display_name) VALUES (%s, %s, %s)",
            ("target-user", "target-only", "Target Only"),
        )
        conn.commit()

    with pytest.raises(MigrationPreconditionError, match="non-seed data"):
        import_sqlite_to_postgres(
            source,
            postgres_database,
            snapshot_path=tmp_path / "snapshot.db",
        )

    with psycopg.connect(postgres_database) as conn:
        assert conn.execute("SELECT count(*) FROM users").fetchone()[0] == 1
        assert conn.execute("SELECT username FROM users").fetchone()[0] == "target-only"


def test_wallet_recalculation_detects_drift(tmp_path: Path, postgres_database: str) -> None:
    source = _seed_source(tmp_path / "source.db")
    import_sqlite_to_postgres(source, postgres_database, snapshot_path=tmp_path / "snapshot.db")
    with psycopg.connect(postgres_database) as conn:
        conn.execute("UPDATE wallets SET available_credits = 99 WHERE user_id = 'user-1'")
        conn.commit()

    report = reconcile_databases(source, postgres_database)

    assert not report.ok
    assert any(issue.code == "wallet_balance_mismatch" for issue in report.issues)


def test_paid_order_without_charge_is_detected(tmp_path: Path, postgres_database: str) -> None:
    source = _seed_source(tmp_path / "source.db")
    import_sqlite_to_postgres(source, postgres_database, snapshot_path=tmp_path / "snapshot.db")
    with psycopg.connect(postgres_database) as conn:
        conn.execute("DELETE FROM wallet_transactions WHERE id = 'tx-charge-1'")
        conn.commit()

    report = reconcile_databases(source, postgres_database)

    assert not report.ok
    assert any(issue.code == "paid_order_charge_mismatch" for issue in report.issues)


def test_non_paid_order_with_charge_blocks_import(tmp_path: Path, postgres_database: str) -> None:
    source = _seed_source(tmp_path / "source.db")
    with connect_database(source) as conn:
        conn.execute("UPDATE recharge_orders SET status = 'PENDING', paid_at = NULL")
        conn.commit()

    with pytest.raises(MigrationReconciliationError, match="unpaid_order_has_charge"):
        import_sqlite_to_postgres(
            source,
            postgres_database,
            snapshot_path=tmp_path / "snapshot.db",
        )


def test_charge_without_order_blocks_import(tmp_path: Path, postgres_database: str) -> None:
    source = _seed_source(tmp_path / "source.db")
    with connect_database(source) as conn:
        conn.execute("UPDATE recharge_orders SET status = 'PENDING', paid_at = NULL")
        conn.execute(
            "UPDATE wallet_transactions SET recharge_order_id = NULL WHERE id = 'tx-charge-1'"
        )
        conn.commit()

    with pytest.raises(MigrationReconciliationError, match="charge_without_order"):
        import_sqlite_to_postgres(
            source,
            postgres_database,
            snapshot_path=tmp_path / "snapshot.db",
        )


def test_asset_reference_orphan_blocks_import(tmp_path: Path, postgres_database: str) -> None:
    source = _seed_source(tmp_path / "source.db")
    with connect_database(source) as conn:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute(
            "UPDATE generation_tasks SET result_asset_id = 'missing-asset' WHERE id = 'task-1'"
        )
        conn.commit()

    with pytest.raises(MigrationReconciliationError, match="asset reference"):
        import_sqlite_to_postgres(
            source,
            postgres_database,
            snapshot_path=tmp_path / "snapshot.db",
        )

    with psycopg.connect(postgres_database) as conn:
        assert conn.execute("SELECT count(*) FROM generation_tasks").fetchone()[0] == 0
