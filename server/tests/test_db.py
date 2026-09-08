from __future__ import annotations

import json
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
from app.db_portable import BusinessConnection
from app.generation import acquire_generation_task_lease


def _create_minimal_task(
    conn: BusinessConnection,
    *,
    user_id: str,
    project_id: str,
    batch_id: str,
    task_id: str,
) -> str:
    """Test fixture only; production repositories never manufacture owners."""
    with conn:
        conn.execute(
            "INSERT INTO users (id, username, display_name) VALUES (%s, %s, %s)",
            (user_id, user_id, user_id),
        )
        conn.execute(
            "INSERT INTO projects (id, owner_user_id, name) VALUES (%s, %s, %s)",
            (project_id, user_id, project_id),
        )
        conn.execute(
            """
            INSERT INTO generation_batches (
                id, project_id, created_by_user_id, idempotency_key,
                request_hash, request_snapshot_json
            ) VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                batch_id,
                project_id,
                user_id,
                f"{batch_id}:key",
                f"{batch_id}:hash",
                json.dumps(
                    {
                        "prompt_version_id": f"prompt-{batch_id}",
                        "output_duration_seconds": 10,
                        "resolution": "768P",
                    }
                ),
            ),
        )
        conn.execute(
            """
            INSERT INTO generation_tasks (
                id, batch_id, generation_mode, provider, model, status,
                next_poll_at, prompt_snapshot_json
            ) VALUES (%s, %s, 'I2V', 'metaso', 'MiniMax-H3', 'PENDING',
                      CURRENT_TIMESTAMP, %s)
            """,
            (
                task_id,
                batch_id,
                json.dumps({"prompt_text": "test prompt", "first_frame_uri": "fake://first.png"}),
            ),
        )
    return task_id


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
        receipt_columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(viral_link_resolution_receipts)").fetchall()
        }
        receipt_schema = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
            ("viral_link_resolution_receipts",),
        ).fetchone()[0]

    assert db_path.exists()
    assert journal_mode == "wal"
    assert foreign_keys == 1
    assert busy_timeout >= 5000
    assert alembic_versions == ["080_viral_link_resolution_receipts"]
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
        "analysis_tasks",
        "source_frame_tasks",
        "script_rewrite_tasks",
        "oral_billing_reconciliation_operations",
        "viral_video_favorites",
        "viral_video_visibility",
        "viral_runtime_controls",
        "viral_import_tasks",
        "viral_media_preparations",
        "viral_refresh_tasks",
        "viral_link_resolution_receipts",
    }.issubset(tables)
    assert {"lease_owner", "lease_expires_at"}.issubset(receipt_columns)
    assert all(
        status in receipt_schema
        for status in ("PREPARED", "REQUEST_SENT", "FAILED_SAFE", "UNCERTAIN")
    )


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
        analysis_task_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(analysis_tasks)").fetchall()
        }
        analysis_task_indexes = {
            row[1] for row in conn.execute("PRAGMA index_list(analysis_tasks)").fetchall()
        }
        source_frame_task_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(source_frame_tasks)").fetchall()
        }
        source_frame_task_indexes = {
            row[1] for row in conn.execute("PRAGMA index_list(source_frame_tasks)").fetchall()
        }
        script_rewrite_task_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(script_rewrite_tasks)").fetchall()
        }
        script_rewrite_task_indexes = {
            row[1] for row in conn.execute("PRAGMA index_list(script_rewrite_tasks)").fetchall()
        }
        script_rewrite_task_foreign_keys = {
            (row["from"], row["table"], row["to"])
            for row in conn.execute("PRAGMA foreign_key_list(script_rewrite_tasks)").fetchall()
        }
        character_persona_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(character_personas)").fetchall()
        }
        reconcile_operation_columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(generation_task_operations)").fetchall()
        }
        reconcile_operation_indexes = {
            row[1]
            for row in conn.execute("PRAGMA index_list(generation_task_operations)").fetchall()
        }

    assert version == "080_viral_link_resolution_receipts"
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
    assert {
        "project_id",
        "asset_id",
        "duration_seconds",
        "status",
        "locked_by",
        "locked_until",
        "result_version_id",
        "retryable",
    }.issubset(analysis_task_columns)
    assert "uq_analysis_tasks_active_asset" in analysis_task_indexes
    assert {
        "project_id",
        "asset_id",
        "request_hash",
        "request_json",
        "status",
        "locked_by",
        "locked_until",
        "result_version_id",
        "retryable",
    }.issubset(source_frame_task_columns)
    assert "uq_source_frame_tasks_active_asset" in source_frame_task_indexes
    assert {
        "project_id",
        "request_hash",
        "request_json",
        "identity_id",
        "ip_profile_snapshot_json",
        "ip_profile_hash",
        "result_json",
        "status",
        "locked_by",
        "locked_until",
        "provider_started_at",
        "retryable",
    }.issubset(script_rewrite_task_columns)
    assert "uq_script_rewrite_tasks_active_project" in script_rewrite_task_indexes
    assert "idx_script_rewrite_tasks_project_identity_created" in script_rewrite_task_indexes
    assert "ip_profile_revision" in character_persona_columns
    assert not any(key[0] == "identity_id" for key in script_rewrite_task_foreign_keys)
    assert {
        "attempt",
        "locked_by",
        "locked_until",
        "started_at",
        "completed_at",
        "error_code",
        "error_message_redacted",
        "retryable",
    }.issubset(reconcile_operation_columns)
    assert "idx_generation_task_operations_reconcile_claim" in reconcile_operation_indexes


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

    with BusinessConnection.sqlite(connect_database(db_path)) as conn:
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

    with BusinessConnection.sqlite(connect_database(db_path)) as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone()[0] == (
            "080_viral_link_resolution_receipts"
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

    with BusinessConnection.sqlite(connect_database(db_path)) as conn:
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

    with BusinessConnection.sqlite(connect_database(db_path)) as conn:
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

    assert version == "080_viral_link_resolution_receipts"
    assert "oss" not in providers
    assert active_provider == expected_provider

    command.downgrade(alembic_config(db_path), "017_generation_task_retry_lineage")

    with BusinessConnection.sqlite(connect_database(db_path)) as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone()[0] == (
            "017_generation_task_retry_lineage"
        )


def test_remove_oss_migration_refuses_to_orphan_legacy_assets(tmp_path: Path) -> None:
    db_path = tmp_path / "remove-oss-with-assets.db"
    command.upgrade(alembic_config(db_path), "017_generation_task_retry_lineage")

    with BusinessConnection.sqlite(connect_database(db_path)) as conn:
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

    with BusinessConnection.sqlite(connect_database(db_path)) as conn:
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
    with BusinessConnection.sqlite(connect_database(db_path)) as conn:
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
        # The bootstrap logs Chinese messages; the default locale decoding
        # (C/POSIX on CI runners) crashes the reader thread, whose death
        # stalls the pipe and deadlocks the subprocess. Decode UTF-8 with
        # replacement instead.
        encoding="utf-8",
        errors="replace",
    )

    assert result.returncode == 0, result.stderr
    with BusinessConnection.sqlite(connect_database(db_path)) as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone()[0] == (
            "080_viral_link_resolution_receipts"
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

    with BusinessConnection.sqlite(connect_database(db_path)) as conn:
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

    with BusinessConnection.sqlite(connect_database(db_path)) as conn:
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
    """Two workers with independent connections compete for one PENDING task:
    exactly one lease wins. Ported onto the production acquire path when the
    stale GenerationTaskRepository was removed — the invariant it checked
    (single winner under the SQLite lane's BEGIN IMMEDIATE gate) belongs to
    acquire_generation_task_lease, not to a dead twin."""
    db_path = tmp_path / "app.db"
    with initialize_database(db_path) as raw:
        with BusinessConnection.sqlite(raw) as conn:
            task_id = _create_minimal_task(
                conn,
                user_id="user_1",
                project_id="project_1",
                batch_id="batch_1",
                task_id="task_1",
            )

    barrier = threading.Barrier(2)
    leases = []
    leases_lock = threading.Lock()

    def compete(worker_id: str) -> None:
        with BusinessConnection.sqlite(connect_database(db_path)) as conn:
            barrier.wait()
            lease = acquire_generation_task_lease(conn, worker_id=worker_id)
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
    assert str(winners[0]["id"]) == task_id
    assert winners[0]["status"] == "SUBMITTING"
    # The lock columns live on the row, not in the worker payload dict.
    with BusinessConnection.sqlite(connect_database(db_path)) as conn:
        locked = conn.execute(
            "SELECT locked_by, attempt, status FROM generation_tasks WHERE id = %s",
            (task_id,),
        ).fetchone()
    assert locked is not None
    assert locked["locked_by"] in {"worker_a", "worker_b"}
    assert int(locked["attempt"]) == 1


def test_backup_restore_preserves_tasks_versions_and_audit_counts(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    backup_path = tmp_path / "backup.db"
    restored_path = tmp_path / "restored.db"
    with initialize_database(db_path) as raw:
        with BusinessConnection.sqlite(raw) as conn:
            _create_minimal_task(
                conn,
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


def test_backup_leaves_stale_fixed_name_tmp_untouched_and_no_residue(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    backup_path = tmp_path / "backup.db"
    stale_tmp = tmp_path / ".backup.db.tmp"
    with initialize_database(db_path):
        pass
    stale_tmp.write_bytes(b"unrelated file from another writer")

    backup_database(db_path, backup_path)

    # The fixed-name temporary belongs to another writer; a concurrent backup
    # must never unlink or reuse it (M1 review M3), and no random temporary
    # may survive the success path. The published backup is private (0600,
    # POSIX only — Windows reports a synthesized 0o666 for every file).
    assert stale_tmp.read_bytes() == b"unrelated file from another writer"
    assert backup_path.exists()
    residue = [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp") and p != stale_tmp]
    assert residue == []
    if os.name == "posix":
        assert os.stat(backup_path).st_mode & 0o777 == 0o600


def test_restore_leaves_stale_fixed_name_tmp_untouched_and_no_residue(tmp_path: Path) -> None:
    source_path = tmp_path / "app.db"
    backup_path = tmp_path / "backup.db"
    restored_path = tmp_path / "restored.db"
    stale_tmp = tmp_path / ".restored.db.tmp"
    with initialize_database(source_path):
        pass
    backup_database(source_path, backup_path)
    stale_tmp.write_bytes(b"unrelated file from another writer")

    restore_database(backup_path, restored_path)

    assert stale_tmp.read_bytes() == b"unrelated file from another writer"
    assert restored_path.exists()
    residue = [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp") and p != stale_tmp]
    assert residue == []
    if os.name == "posix":
        assert os.stat(restored_path).st_mode & 0o777 == 0o600


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

    with BusinessConnection.sqlite(connect_database(db_path)) as conn:
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


def test_oral_clone_consent_migration_is_reversible(tmp_path: Path) -> None:
    db_path = tmp_path / "oral-consent-migration.db"
    config = alembic_config(db_path)

    command.upgrade(config, "072_oral_clone_consent")
    with connect_database(db_path) as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone()[0] == (
            "072_oral_clone_consent"
        )
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        voice_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(oral_voices)").fetchall()
        }
        avatar_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(oral_avatars)").fetchall()
        }
    assert "oral_consents" in tables
    assert {
        "consent_id",
        "confirmed_by_user_id",
        "confirmed_at",
        "idempotency_key",
        "request_hash",
        "submission_state",
    }.issubset(voice_columns)
    assert {
        "consent_id",
        "idempotency_key",
        "request_hash",
        "submission_state",
    }.issubset(avatar_columns)

    with connect_database(db_path) as conn:
        conn.execute(
            "INSERT INTO users (id, username, display_name) "
            "VALUES ('oral-user', 'oral-user', 'Oral')"
        )
        conn.execute(
            """
            INSERT INTO person_identities (id, owner_user_id, display_name, status)
            VALUES ('oral-identity', 'oral-user', 'Oral', 'ACTIVE')
            """
        )
        conn.execute(
            """
            INSERT INTO assets (
                id, kind, storage_uri, sha256, size_bytes, content_type, created_by_user_id
            ) VALUES ('oral-source', 'oral_audio', 'local://oral/source.mp3',
                      'oral-hash', 1, 'audio/mpeg', 'oral-user')
            """
        )
        conn.execute(
            """
            INSERT INTO oral_consents (
                id, identity_id, owner_user_id, source_asset_id, purpose,
                consent_text_version, source_sha256, consented_at
            ) VALUES ('oral-consent', 'oral-identity', 'oral-user', 'oral-source',
                      'VOICE_CLONE', 'v1', 'oral-hash', CURRENT_TIMESTAMP)
            """
        )
        conn.execute(
            """
            INSERT INTO oral_voices (
                id, identity_id, owner_user_id, title, status, source_asset_id,
                consent_id, confirmed, confirmed_by_user_id, confirmed_at
            ) VALUES ('oral-voice', 'oral-identity', 'oral-user', 'Voice', 'READY',
                      'oral-source', 'oral-consent', 1, 'oral-user', CURRENT_TIMESTAMP)
            """
        )

    command.downgrade(config, "071_studio_material_preferences")
    with connect_database(db_path) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        voice_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(oral_voices)").fetchall()
        }
        avatar_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(oral_avatars)").fetchall()
        }
        voice_count = conn.execute(
            "SELECT COUNT(*) FROM oral_voices WHERE id = 'oral-voice'"
        ).fetchone()[0]
    assert "oral_consents" not in tables
    assert {
        "consent_id",
        "confirmed_by_user_id",
        "confirmed_at",
        "idempotency_key",
        "request_hash",
        "submission_state",
    }.isdisjoint(voice_columns)
    assert {
        "consent_id",
        "idempotency_key",
        "request_hash",
        "submission_state",
    }.isdisjoint(avatar_columns)
    assert voice_count == 1


def test_oral_durable_billing_migration_fails_loud_on_downgrade_with_ledger(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "oral-billing-downgrade.db"
    config = alembic_config(db_path)
    command.upgrade(config, "073_oral_durable_billing")
    with connect_database(db_path) as conn:
        conn.execute(
            "INSERT INTO users (id, username, display_name) VALUES ('u-oral','u-oral','U')"
        )
        conn.execute(
            "INSERT INTO wallets (user_id, available_credits, reserved_credits) "
            "VALUES ('u-oral', 0, 1)"
        )
        conn.execute(
            "INSERT INTO person_identities (id, owner_user_id, display_name, status) "
            "VALUES ('i-oral','u-oral','I','ACTIVE')"
        )
        conn.execute(
            """
            INSERT INTO oral_avatars (
                id, identity_id, owner_user_id, title, status, source_kind, source_asset_id
            ) VALUES ('a-oral','i-oral','u-oral','A','READY','VIDEO','source')
            """
        )
        conn.execute(
            """
            INSERT INTO oral_tasks (
                id, owner_user_id, identity_id, avatar_id, mode, title, status,
                estimated_cost_fen, idempotency_key, billing_round
            ) VALUES ('t-oral','u-oral','i-oral','a-oral','AUDIO','T','QUEUED',
                      1000,'oral-idem',1)
            """
        )
        conn.execute(
            """
            INSERT INTO wallet_transactions (
                id, user_id, type, available_delta, reserved_delta, oral_task_id,
                billing_round, idempotency_key
            ) VALUES ('tx-oral','u-oral','RESERVE',-1,1,'t-oral',1,'oral-reserve')
            """
        )

    with pytest.raises(RuntimeError, match="oral wallet transactions exist"):
        command.downgrade(config, "072_oral_clone_consent")
    with connect_database(db_path) as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone()[0] == (
            "073_oral_durable_billing"
        )


@pytest.mark.parametrize("legacy_status", ["QUEUED", "RUNNING"])
def test_oral_durable_billing_upgrade_rejects_unreserved_active_legacy_tasks(
    tmp_path: Path,
    legacy_status: str,
) -> None:
    db_path = tmp_path / f"oral-active-legacy-{legacy_status.lower()}.db"
    config = alembic_config(db_path)
    command.upgrade(config, "072_oral_clone_consent")
    with connect_database(db_path) as conn:
        conn.execute("INSERT INTO users (id, username, display_name) VALUES ('u-old','u-old','U')")
        conn.execute(
            "INSERT INTO person_identities (id, owner_user_id, display_name, status) "
            "VALUES ('i-old','u-old','I','ACTIVE')"
        )
        conn.execute(
            """
            INSERT INTO oral_avatars (
                id, identity_id, owner_user_id, title, status, source_kind,
                source_asset_id
            ) VALUES ('a-old','i-old','u-old','A','READY','VIDEO','source')
            """
        )
        conn.execute(
            """
            INSERT INTO oral_tasks (
                id, owner_user_id, identity_id, avatar_id, mode, title, status,
                estimated_cost_fen, idempotency_key, submission_state
            ) VALUES ('t-old','u-old','i-old','a-old','AUDIO','T',?,1000,
                      'old-idem','SUBMITTED')
            """,
            (legacy_status,),
        )

    with pytest.raises(RuntimeError, match="active legacy oral tasks"):
        command.upgrade(config, "073_oral_durable_billing")
    with connect_database(db_path) as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone()[0] == (
            "072_oral_clone_consent"
        )
        columns = {row[1] for row in conn.execute("PRAGMA table_info(oral_tasks)")}
        assert "billing_round" not in columns


@pytest.mark.parametrize("legacy_status", ["SUCCEEDED", "FAILED", "CANCELLED"])
def test_oral_durable_billing_upgrade_preserves_terminal_legacy_tasks(
    tmp_path: Path,
    legacy_status: str,
) -> None:
    db_path = tmp_path / f"oral-terminal-legacy-{legacy_status.lower()}.db"
    config = alembic_config(db_path)
    command.upgrade(config, "072_oral_clone_consent")
    with connect_database(db_path) as conn:
        conn.execute("INSERT INTO users (id, username, display_name) VALUES ('u-old','u-old','U')")
        conn.execute(
            "INSERT INTO person_identities (id, owner_user_id, display_name, status) "
            "VALUES ('i-old','u-old','I','ACTIVE')"
        )
        conn.execute(
            "INSERT INTO oral_avatars "
            "(id, identity_id, owner_user_id, title, status, source_kind, source_asset_id) "
            "VALUES ('a-old','i-old','u-old','A','READY','VIDEO','source')"
        )
        conn.execute(
            "INSERT INTO oral_tasks "
            "(id, owner_user_id, identity_id, avatar_id, mode, title, status, "
            "estimated_cost_fen, idempotency_key, submission_state) "
            "VALUES ('t-old','u-old','i-old','a-old','AUDIO','T',?,1000,"
            "'old-idem','SUBMITTED')",
            (legacy_status,),
        )

    command.upgrade(config, "073_oral_durable_billing")
    with connect_database(db_path) as conn:
        row = conn.execute(
            "SELECT status, billing_round FROM oral_tasks WHERE id = 't-old'"
        ).fetchone()
        assert (row["status"], row["billing_round"]) == (legacy_status, None)


def test_script_rewrite_ip_snapshot_migration_downgrade_preserves_request_snapshot(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "script-rewrite-ip-downgrade.db"
    config = alembic_config(db_path)
    command.upgrade(config, "head")
    with connect_database(db_path) as conn:
        conn.execute("INSERT INTO users (id, username, display_name) VALUES ('u-ip','u-ip','U')")
        conn.execute("INSERT INTO projects (id, owner_user_id, name) VALUES ('p-ip','u-ip','P')")
        conn.execute(
            "INSERT INTO person_identities (id, owner_user_id, display_name, status) "
            "VALUES ('i-ip','u-ip','I','ACTIVE')"
        )
        snapshot = '{"identity_id":"i-ip","profile_version":0}'
        request_json = json.dumps(
            {"text": "test", "identity_id": "i-ip", "ip_profile_snapshot": json.loads(snapshot)}
        )
        conn.execute(
            """
            INSERT INTO script_rewrite_tasks (
                id, project_id, created_by_user_id, idempotency_key,
                request_hash, request_json, identity_id,
                ip_profile_snapshot_json, ip_profile_hash, status
            ) VALUES ('t-ip','p-ip','u-ip','idem-ip','request-hash',?,'i-ip',
                      ?, ?, 'FAILED')
            """,
            (request_json, snapshot, "0" * 64),
        )

    command.downgrade(config, "073_oral_durable_billing")
    with connect_database(db_path) as conn:
        row = conn.execute(
            "SELECT request_json FROM script_rewrite_tasks WHERE id = 't-ip'"
        ).fetchone()
        assert json.loads(row["request_json"])["ip_profile_snapshot"] == json.loads(snapshot)
        persona_columns = {
            item[1] for item in conn.execute("PRAGMA table_info(character_personas)").fetchall()
        }
        assert "ip_profile_revision" not in persona_columns
