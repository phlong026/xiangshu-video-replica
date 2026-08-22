from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from alembic import command
from cryptography.fernet import Fernet

from app.backup import backup_database, check_database, restore_database, run_daily_backup
from app.db import alembic_config, connect_database, initialize_database
from app.repositories import GenerationTaskRepository


def test_initialize_database_applies_sqlite_pragmas_and_migrations(tmp_path: Path) -> None:
    db_path = tmp_path / "data" / "app.db"

    with initialize_database(db_path) as conn:
        journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        foreign_keys = conn.execute("PRAGMA foreign_keys").fetchone()[0]
        busy_timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            ).fetchall()
        }
        alembic_versions = [
            row[0] for row in conn.execute("SELECT version_num FROM alembic_version").fetchall()
        ]

    assert db_path.exists()
    assert journal_mode == "wal"
    assert foreign_keys == 1
    assert busy_timeout >= 5000
    assert alembic_versions == ["028_admin_write_idempotency"]
    assert "schema_migrations" not in tables
    assert {
        "users",
        "projects",
        "assets",
        "versions",
        "generation_batches",
        "generation_tasks",
        "external_call_logs",
        "audit_logs",
        "characters",
        "project_main_characters",
        "person_identities",
        "character_personas",
        "character_versions",
        "character_assets",
        "character_asset_reviews",
        "character_generation_tasks",
        "character_reference_selections",
        "generation_task_operations",
    }.issubset(tables)


def test_alembic_upgrades_empty_database_to_head(tmp_path: Path) -> None:
    db_path = tmp_path / "empty.db"

    with initialize_database(db_path) as conn:
        version = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        task_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(generation_tasks)").fetchall()
        }
        task_indexes = {
            row[1] for row in conn.execute("PRAGMA index_list(generation_tasks)").fetchall()
        }
        task_foreign_keys = {
            (row["from"], row["table"], row["to"])
            for row in conn.execute("PRAGMA foreign_key_list(generation_tasks)").fetchall()
        }

    assert version == "028_admin_write_idempotency"
    assert {
        "locked_by",
        "locked_until",
        "provider_task_id",
        "result_asset_id",
        "prompt_snapshot_json",
        "provider_request_json",
    }.issubset(task_columns)
    assert "idx_generation_tasks_prompt_version" in task_indexes
    assert ("prompt_version_id", "versions", "id") in task_foreign_keys


def test_retry_lineage_revision_is_reversible(tmp_path: Path) -> None:
    db_path = tmp_path / "retry-lineage.db"

    with initialize_database(db_path) as conn:
        batch_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(generation_batches)").fetchall()
        }
        task_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(generation_tasks)").fetchall()
        }
        task_indexes = {
            row[1] for row in conn.execute("PRAGMA index_list(generation_tasks)").fetchall()
        }

    assert {"source_batch_id", "source_task_id", "generation_reason"}.issubset(batch_columns)
    assert {
        "retry_of_task_id",
        "superseded_by_task_id",
        "superseded_at",
        "retry_reason",
        "retry_requested_by_user_id",
        "retry_requested_at",
        "billing_confirmation_status",
        "billing_confirmed_by_user_id",
        "billing_confirmed_at",
        "billing_confirmation_reason",
    }.issubset(task_columns)
    assert "idx_generation_tasks_active_attention" in task_indexes

    command.downgrade(alembic_config(db_path), "016_character_reference_snapshot")

    with connect_database(db_path) as conn:
        version = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        batch_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(generation_batches)").fetchall()
        }
        task_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(generation_tasks)").fetchall()
        }
        operation_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            ("generation_task_operations",),
        ).fetchone()

    assert version == "016_character_reference_snapshot"
    assert "source_batch_id" not in batch_columns
    assert "retry_of_task_id" not in task_columns
    assert operation_table is None

    command.upgrade(alembic_config(db_path), "head")

    with connect_database(db_path) as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone()[0] == (
            "028_admin_write_idempotency"
        )


@pytest.mark.parametrize(
    ("has_cos_config", "expected_provider"),
    [(True, "cos"), (False, "local")],
)
def test_remove_oss_migration_purges_settings_and_selects_safe_fallback(
    tmp_path: Path,
    has_cos_config: bool,
    expected_provider: str,
) -> None:
    db_path = tmp_path / f"remove-oss-{expected_provider}.db"
    command.upgrade(alembic_config(db_path), "017_generation_task_retry_lineage")

    with connect_database(db_path) as conn:
        conn.execute(
            "INSERT INTO users (id, username, display_name, role) "
            "VALUES ('admin', 'admin', 'Admin', 'admin')"
        )
        conn.execute(
            "INSERT INTO provider_settings (provider, encrypted_config, updated_by_user_id) "
            "VALUES ('oss', 'encrypted-oss', 'admin')"
        )
        if has_cos_config:
            conn.execute(
                "INSERT INTO provider_settings (provider, encrypted_config, updated_by_user_id) "
                "VALUES ('cos', 'encrypted-cos', 'admin')"
            )
        conn.execute(
            "UPDATE runtime_settings SET active_storage_provider = 'oss', "
            "updated_by_user_id = 'admin' WHERE id = 1"
        )
        conn.commit()

    command.upgrade(alembic_config(db_path), "head")

    with connect_database(db_path) as conn:
        version = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        providers = {
            row[0] for row in conn.execute("SELECT provider FROM provider_settings").fetchall()
        }
        active_provider = conn.execute(
            "SELECT active_storage_provider FROM runtime_settings WHERE id = 1"
        ).fetchone()[0]

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO provider_settings (provider, encrypted_config) "
                "VALUES ('oss', 'removed')"
            )

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE runtime_settings SET active_storage_provider = 'oss' WHERE id = 1")

    assert version == "028_admin_write_idempotency"
    assert "oss" not in providers
    assert active_provider == expected_provider

    command.downgrade(alembic_config(db_path), "017_generation_task_retry_lineage")

    with connect_database(db_path) as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone()[0] == (
            "017_generation_task_retry_lineage"
        )


def test_remove_oss_migration_refuses_to_orphan_legacy_assets(tmp_path: Path) -> None:
    db_path = tmp_path / "remove-oss-with-assets.db"
    command.upgrade(alembic_config(db_path), "017_generation_task_retry_lineage")

    with connect_database(db_path) as conn:
        conn.execute(
            "INSERT INTO users (id, username, display_name, role) "
            "VALUES ('admin', 'admin', 'Admin', 'admin')"
        )
        conn.execute(
            "INSERT INTO projects (id, owner_user_id, name) "
            "VALUES ('project-1', 'admin', 'Legacy OSS project')"
        )
        conn.execute(
            "INSERT INTO assets ("
            "id, project_id, kind, storage_uri, sha256, size_bytes, content_type, "
            "created_by_user_id"
            ") VALUES ("
            "'asset-1', 'project-1', 'video', 'oss://legacy-bucket/video.mp4', "
            "'legacy-sha256', 1, 'video/mp4', 'admin'"
            ")"
        )
        conn.execute(
            "INSERT INTO provider_settings (provider, encrypted_config, updated_by_user_id) "
            "VALUES ('oss', 'encrypted-oss', 'admin')"
        )
        conn.execute(
            "UPDATE runtime_settings SET active_storage_provider = 'oss', "
            "updated_by_user_id = 'admin' WHERE id = 1"
        )
        conn.commit()

    with pytest.raises(RuntimeError, match="OSS-backed assets must be migrated"):
        command.upgrade(alembic_config(db_path), "head")

    with connect_database(db_path) as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone()[0] == (
            "017_generation_task_retry_lineage"
        )
        assert (
            conn.execute(
                "SELECT encrypted_config FROM provider_settings WHERE provider = 'oss'"
            ).fetchone()[0]
            == "encrypted-oss"
        )
        assert (
            conn.execute(
                "SELECT active_storage_provider FROM runtime_settings WHERE id = 1"
            ).fetchone()[0]
            == "oss"
        )
        assert (
            conn.execute("SELECT storage_uri FROM assets WHERE id = 'asset-1'").fetchone()[0]
            == "oss://legacy-bucket/video.mp4"
        )


def test_runtime_bootstrap_upgrades_an_existing_database_before_startup(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "bootstrap-upgrade.db"
    command.upgrade(alembic_config(db_path), "017_generation_task_retry_lineage")
    with connect_database(db_path) as conn:
        conn.execute(
            "INSERT INTO provider_settings (provider, encrypted_config) "
            "VALUES ('oss', 'legacy-encrypted-value')"
        )
        conn.execute("UPDATE runtime_settings SET active_storage_provider = 'oss' WHERE id = 1")
        conn.commit()

    result = subprocess.run(
        [sys.executable, "-m", "app.bootstrap"],
        cwd=Path(__file__).resolve().parents[1],
        env={
            **os.environ,
            "VIDEO_REPLICA_DB_PATH": str(db_path),
            "VIDEO_REPLICA_SETTINGS_KEY": Fernet.generate_key().decode("ascii"),
        },
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    with connect_database(db_path) as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone()[0] == (
            "028_admin_write_idempotency"
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM provider_settings WHERE provider = 'oss'"
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT active_storage_provider FROM runtime_settings WHERE id = 1"
            ).fetchone()[0]
            == "local"
        )


def test_alembic_revision_can_downgrade_to_base(tmp_path: Path) -> None:
    db_path = tmp_path / "downgrade.db"
    with initialize_database(db_path):
        pass

    command.downgrade(alembic_config(db_path), "base")

    with connect_database(db_path) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            ).fetchall()
        }

    assert "generation_tasks" not in tables
    assert "users" not in tables


def test_generation_revision_can_downgrade_to_characters(tmp_path: Path) -> None:
    db_path = tmp_path / "generation-downgrade.db"
    with initialize_database(db_path):
        pass

    command.downgrade(alembic_config(db_path), "003_characters")

    with connect_database(db_path) as conn:
        version = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        task_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(generation_tasks)").fetchall()
        }
        task_indexes = {
            row[1] for row in conn.execute("PRAGMA index_list(generation_tasks)").fetchall()
        }

    assert version == "003_characters"
    assert "prompt_version_id" not in task_columns
    assert "prompt_snapshot_json" not in task_columns
    assert "provider_request_json" not in task_columns
    assert "idx_generation_tasks_prompt_version" not in task_indexes


def test_foreign_keys_are_enforced(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"

    with initialize_database(db_path) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO projects (id, owner_user_id, name)
                VALUES (?, ?, ?)
                """,
                ("project_1", "missing_user", "Project"),
            )


def test_wal_reader_is_not_blocked_by_uncommitted_writer(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    with initialize_database(db_path) as conn:
        conn.execute(
            "INSERT INTO users (id, username, display_name) VALUES (?, ?, ?)",
            ("user_1", "alice", "Alice"),
        )
        conn.commit()

    writer = connect_database(db_path)
    reader = connect_database(db_path)
    try:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute(
            "INSERT INTO users (id, username, display_name) VALUES (?, ?, ?)",
            ("user_2", "bob", "Bob"),
        )

        count = reader.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    finally:
        writer.rollback()
        writer.close()
        reader.close()

    assert count == 1


def test_atomic_task_lease_allows_only_one_worker_with_independent_connections(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "app.db"
    with initialize_database(db_path) as conn:
        repo = GenerationTaskRepository(conn)
        task_id = repo.create_minimal_task(
            user_id="user_1",
            project_id="project_1",
            batch_id="batch_1",
            task_id="task_1",
        )

    barrier = threading.Barrier(2)
    leases = []
    leases_lock = threading.Lock()

    def compete(worker_id: str) -> None:
        with connect_database(db_path) as conn:
            barrier.wait()
            lease = GenerationTaskRepository(conn).acquire_next_lease(
                worker_id=worker_id,
                lease_seconds=30,
            )
        with leases_lock:
            leases.append(lease)

    threads = [
        threading.Thread(target=compete, args=("worker_a",)),
        threading.Thread(target=compete, args=("worker_b",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    winners = [lease for lease in leases if lease is not None]
    assert len(winners) == 1
    assert winners[0].id == task_id
    assert winners[0].locked_by in {"worker_a", "worker_b"}
    assert winners[0].attempt == 1


def test_expired_lease_can_be_recovered_by_another_worker(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    with initialize_database(db_path) as conn:
        repo = GenerationTaskRepository(conn)
        repo.create_minimal_task(
            user_id="user_1",
            project_id="project_1",
            batch_id="batch_1",
            task_id="task_1",
        )
        first = repo.acquire_next_lease(worker_id="worker_a", lease_seconds=-1)
        recovered = repo.acquire_next_lease(worker_id="worker_b", lease_seconds=30)

    assert first is not None
    assert recovered is not None
    assert recovered.locked_by == "worker_b"
    assert recovered.attempt == 2


def test_backup_restore_preserves_tasks_versions_and_audit_counts(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    backup_path = tmp_path / "backup.db"
    restored_path = tmp_path / "restored.db"
    with initialize_database(db_path) as conn:
        repo = GenerationTaskRepository(conn)
        repo.create_minimal_task(
            user_id="user_1",
            project_id="project_1",
            batch_id="batch_1",
            task_id="task_1",
        )
        conn.execute(
            """
            INSERT INTO versions (id, project_id, asset_id, kind, version_number, payload_json)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("version_1", "project_1", None, "script", 1, "{}"),
        )
        conn.execute(
            """
            INSERT INTO audit_logs (id, actor_user_id, action, entity_type, entity_id)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("audit_1", "user_1", "task.created", "generation_task", "task_1"),
        )
        conn.commit()

    backup_database(db_path, backup_path)
    restore_database(backup_path, restored_path)

    with connect_database(restored_path) as conn:
        counts = {
            name: conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
            for name in ("generation_tasks", "versions", "audit_logs")
        }

    assert counts == {"generation_tasks": 1, "versions": 1, "audit_logs": 1}


def test_database_integrity_check_and_cli_reject_corruption(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    broken_path = tmp_path / "broken.db"
    with initialize_database(db_path):
        pass
    broken_path.write_text("not a sqlite database", encoding="utf-8")

    assert check_database(db_path) == db_path.resolve()
    subprocess.run(
        [sys.executable, "-m", "app.backup", "check", str(db_path)],
        check=True,
        cwd=Path(__file__).resolve().parents[1],
    )
    with pytest.raises(sqlite3.DatabaseError):
        check_database(broken_path)


def test_backup_refuses_missing_source_without_creating_empty_database(tmp_path: Path) -> None:
    source_path = tmp_path / "missing.db"
    backup_path = tmp_path / "backup.db"

    with pytest.raises(FileNotFoundError):
        backup_database(source_path, backup_path)

    assert not source_path.exists()
    assert not backup_path.exists()


def test_restore_keeps_existing_target_when_backup_integrity_fails(tmp_path: Path) -> None:
    valid_path = tmp_path / "valid.db"
    broken_backup_path = tmp_path / "broken.db"
    target_path = tmp_path / "target.db"

    with initialize_database(valid_path) as conn:
        conn.execute(
            "INSERT INTO users (id, username, display_name) VALUES (?, ?, ?)",
            ("user_1", "alice", "Alice"),
        )
        conn.commit()
    backup_database(valid_path, target_path)
    broken_backup_path.write_text("not a sqlite database", encoding="utf-8")

    with pytest.raises(sqlite3.DatabaseError):
        restore_database(broken_backup_path, target_path)

    with connect_database(target_path) as conn:
        count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    assert count == 1


def test_daily_backup_creates_dated_backup_file(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    backup_dir = tmp_path / "daily"
    with initialize_database(db_path):
        pass

    backup_path = run_daily_backup(db_path, backup_dir)

    assert backup_path.parent == backup_dir
    assert backup_path.name.startswith("app-")
    assert backup_path.name.endswith(".db")
    with connect_database(backup_path) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_backup_cli_can_backup_and_restore_database(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    backup_path = tmp_path / "backup.db"
    restored_path = tmp_path / "restored.db"
    with initialize_database(db_path) as conn:
        conn.execute(
            "INSERT INTO users (id, username, display_name) VALUES (?, ?, ?)",
            ("user_1", "alice", "Alice"),
        )
        conn.commit()

    subprocess.run(
        [
            sys.executable,
            "-m",
            "app.backup",
            "backup",
            str(db_path),
            str(backup_path),
        ],
        check=True,
        cwd=Path(__file__).resolve().parents[1],
    )
    subprocess.run(
        [
            sys.executable,
            "-m",
            "app.backup",
            "restore",
            str(backup_path),
            str(restored_path),
        ],
        check=True,
        cwd=Path(__file__).resolve().parents[1],
    )

    with connect_database(restored_path) as conn:
        count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    assert count == 1


def test_migration_007_downgrade_resets_local_to_cos(tmp_path: Path) -> None:
    db_path = tmp_path / "downgrade-007.db"
    with initialize_database(db_path) as conn:
        conn.execute("UPDATE runtime_settings SET active_storage_provider='local' WHERE id=1")
        conn.commit()

    command.downgrade(alembic_config(db_path), "006_active_storage_provider")

    with connect_database(db_path) as conn:
        provider = conn.execute(
            "SELECT active_storage_provider FROM runtime_settings WHERE id = 1"
        ).fetchone()[0]
    assert provider == "cos"


def test_migration_009_downgrade_fails_on_cross_project_duplicate_key(tmp_path: Path) -> None:
    db_path = tmp_path / "downgrade-009.db"
    with initialize_database(db_path) as conn:
        conn.execute(
            "INSERT INTO users (id, username, display_name, role) VALUES ('u1','u1','U','employee')"
        )
        conn.executemany(
            "INSERT INTO projects (id, owner_user_id, name) VALUES (?, 'u1', ?)",
            [("p1", "P1"), ("p2", "P2")],
        )
        conn.executemany(
            """
            INSERT INTO generation_batches (
                id, project_id, created_by_user_id, idempotency_key, request_hash,
                request_snapshot_json, status
            ) VALUES (?, ?, 'u1', 'same-key', ?, '{}', 'QUEUED')
            """,
            [("b1", "p1", "h1"), ("b2", "p2", "h2")],
        )
        conn.commit()

    # The pre-009 constraint (created_by_user_id, idempotency_key) is a superset;
    # downgrading with the same key across two projects must fail.
    with pytest.raises(Exception):
        command.downgrade(alembic_config(db_path), "008_provider_result_url")
