from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from alembic import command

from app.auth import CurrentUser
from app.characters import (
    choose_project_main_character,
    create_character,
    get_project_main_character,
    update_character,
)
from app.db import alembic_config, connect_database, initialize_database
from app.main import app

CHARACTER_DOMAIN_TABLES = {
    "person_identities",
    "character_personas",
    "character_versions",
    "character_assets",
    "character_asset_reviews",
    "character_generation_tasks",
    "character_reference_selections",
}

CHARACTER_DOMAIN_SCHEMAS = {
    "PersonIdentity": {
        "id",
        "owner_user_id",
        "display_name",
        "authorization_status",
        "authorization_asset_id",
        "authorization_scope",
        "authorization_expires_at",
        "source_asset_id",
        "source_quality_status",
        "status",
        "created_by",
        "created_at",
        "updated_at",
    },
    "CharacterPersona": {
        "id",
        "identity_id",
        "name",
        "occupation",
        "scene_description",
        "appearance_constraints_json",
        "costume_description",
        "default_background",
        "positive_prompt",
        "negative_prompt",
        "usage_scope_json",
        "created_by",
        "created_at",
        "updated_at",
    },
    "CharacterVersion": {
        "id",
        "persona_id",
        "version_number",
        "status",
        "source_asset_id",
        "source_sha256",
        "persona_snapshot_json",
        "provider",
        "model",
        "generation_params_json",
        "template_version",
        "template_hash",
        "required_view_types_json",
        "published_by",
        "published_at",
        "created_by",
        "created_at",
    },
    "CharacterAsset": {
        "id",
        "character_version_id",
        "asset_id",
        "view_type",
        "candidate_number",
        "generation_task_id",
        "auto_quality_json",
        "review_status",
        "is_published_selection",
        "created_at",
    },
    "CharacterAssetReview": {
        "id",
        "character_asset_id",
        "reviewer_user_id",
        "decision",
        "issue_codes_json",
        "comment",
        "created_at",
    },
    "CharacterGenerationTask": {
        "id",
        "character_version_id",
        "view_type",
        "provider",
        "model",
        "idempotency_key",
        "request_hash",
        "candidate_number",
        "request_snapshot_json",
        "status",
        "provider_task_id",
        "attempt",
        "max_attempts",
        "error_code",
        "error_message_redacted",
        "cost_amount",
        "next_poll_at",
        "created_by",
        "created_at",
        "updated_at",
        "started_at",
        "completed_at",
    },
    "CharacterReferenceSelection": {
        "id",
        "project_id",
        "source_frame_version_id",
        "character_version_id",
        "recommended_asset_ids_json",
        "selected_asset_ids_json",
        "recommendation_reason_json",
        "selected_by",
        "selected_at",
    },
}


def test_character_domain_contracts_are_published_in_openapi() -> None:
    schemas = app.openapi()["components"]["schemas"]

    for name, expected_properties in CHARACTER_DOMAIN_SCHEMAS.items():
        assert name in schemas
        assert set(schemas[name]["properties"]) == expected_properties
        assert set(schemas[name]["required"]) == expected_properties

    assert schemas["PersonIdentity"]["properties"]["status"]["enum"] == [
        "DRAFT",
        "ACTIVE",
        "EXPIRED",
        "REVOKED",
        "ARCHIVED",
    ]
    assert schemas["CharacterVersion"]["properties"]["status"]["enum"] == [
        "DRAFT",
        "GENERATING",
        "REVIEWING",
        "PUBLISHED",
        "FAILED",
        "ARCHIVED",
    ]
    assert schemas["CharacterGenerationTask"]["properties"]["status"]["enum"] == [
        "PENDING",
        "RUNNING",
        "SUCCEEDED",
        "FAILED",
    ]
    required_views = [
        "FRONT_FACE",
        "FRONT_HALF",
        "FRONT_FULL",
        "LEFT_45",
        "RIGHT_45",
        "LEFT_SIDE",
        "RIGHT_SIDE",
    ]
    assert schemas["CharacterGenerationTask"]["properties"]["view_type"]["enum"] == (required_views)
    assert (
        schemas["CharacterVersion"]["properties"]["required_view_types_json"]["items"]["enum"]
        == required_views
    )
    assert schemas["CharacterAsset"]["properties"]["view_type"]["enum"] == [
        *required_views,
        "IMPORTED_REFERENCE",
    ]


def test_generated_client_contains_character_domain_contracts() -> None:
    generated_types = (
        Path(__file__).resolve().parents[2] / "client" / "src" / "generated" / "api.ts"
    ).read_text(encoding="utf-8")

    for name in CHARACTER_DOMAIN_SCHEMAS:
        assert f"{name}: {{" in generated_types


def test_empty_database_upgrade_creates_character_domain_constraints(tmp_path: Path) -> None:
    db_path = tmp_path / "empty-character-domain.db"

    with initialize_database(db_path) as conn:
        version = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            ).fetchall()
        }
        main_character_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(project_main_characters)")
        }
        version_indexes = {row[1] for row in conn.execute("PRAGMA index_list(character_versions)")}
        asset_indexes = {row[1] for row in conn.execute("PRAGMA index_list(character_assets)")}
        task_columns = {
            row[1]: row for row in conn.execute("PRAGMA table_info(character_generation_tasks)")
        }
        task_indexes = {
            row[1] for row in conn.execute("PRAGMA index_list(character_generation_tasks)")
        }
        call_log_columns = {
            row[1]: row for row in conn.execute("PRAGMA table_info(external_call_logs)")
        }
        call_log_foreign_keys = {
            (row["from"], row["table"], row["to"])
            for row in conn.execute("PRAGMA foreign_key_list(external_call_logs)")
        }
        asset_columns = {
            row[1]: row for row in conn.execute("PRAGMA table_info(assets)").fetchall()
        }

    assert version == "014_character_image_generation"
    assert CHARACTER_DOMAIN_TABLES.issubset(tables)
    assert "character_version_id" in main_character_columns
    assert "uq_character_versions_persona_version" in version_indexes
    assert "uq_character_assets_published_view" in asset_indexes
    assert {
        "idempotency_key",
        "request_hash",
        "candidate_number",
        "attempt",
        "max_attempts",
        "locked_by",
        "locked_until",
        "next_poll_at",
        "error_message_redacted",
        "created_by",
        "created_at",
        "updated_at",
    }.issubset(task_columns)
    assert task_columns["idempotency_key"][3] == 1
    assert task_columns["request_hash"][3] == 1
    assert task_columns["candidate_number"][3] == 1
    assert task_columns["attempt"][3] == 1
    assert task_columns["max_attempts"][3] == 1
    assert "idx_character_generation_tasks_lease" in task_indexes
    assert "uq_character_generation_tasks_idempotency" in task_indexes
    assert "uq_character_generation_tasks_candidate" in task_indexes
    assert "character_generation_task_id" in call_log_columns
    assert (
        "character_generation_task_id",
        "character_generation_tasks",
        "id",
    ) in call_log_foreign_keys
    assert asset_columns["project_id"][3] == 0


def test_legacy_character_upgrade_backfills_domain_and_preserves_project_snapshot(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "legacy-character.db"
    command.upgrade(alembic_config(db_path), "011_add_archive_retry_count")
    legacy_snapshot = _seed_legacy_character(db_path)

    command.upgrade(alembic_config(db_path), "head")

    with connect_database(db_path) as conn:
        identity = conn.execute("SELECT * FROM person_identities").fetchone()
        persona = conn.execute("SELECT * FROM character_personas").fetchone()
        version = conn.execute("SELECT * FROM character_versions").fetchone()
        assets = conn.execute("SELECT * FROM character_assets ORDER BY candidate_number").fetchall()
        project_link = conn.execute(
            "SELECT * FROM project_main_characters WHERE project_id = 'project_legacy'"
        ).fetchone()
        legacy_payload = conn.execute(
            "SELECT payload_json FROM versions WHERE id = 'snapshot_v1'"
        ).fetchone()[0]
        restored = get_project_main_character(conn, project_id="project_legacy")

    assert identity["id"] == "legacy-identity:character_legacy"
    assert identity["display_name"] == "Legacy Hero"
    assert identity["owner_user_id"] == "admin_1"
    assert identity["status"] == "ACTIVE"
    assert json.loads(identity["authorization_scope"]) == ["project_legacy"]
    assert identity["source_asset_id"] == "asset_ref_2"

    assert persona["id"] == "legacy-persona:character_legacy"
    assert persona["identity_id"] == identity["id"]
    assert persona["name"] == "Legacy Hero"
    assert json.loads(persona["usage_scope_json"]) == ["project_legacy"]

    assert version["id"] == "legacy-version:character_legacy"
    assert version["persona_id"] == persona["id"]
    assert version["version_number"] == 1
    assert version["status"] == "PUBLISHED"
    assert version["provider"] == "legacy-import"
    assert version["template_version"] == "legacy-character-v1"
    assert len(str(version["template_hash"])) == 64
    assert json.loads(version["required_view_types_json"]) == []
    assert json.loads(version["persona_snapshot_json"]) == legacy_snapshot

    assert [(row["asset_id"], row["candidate_number"]) for row in assets] == [
        ("asset_ref_2", 1),
        ("asset_ref_1", 2),
    ]
    assert [row["is_published_selection"] for row in assets] == [1, 0]
    assert {row["review_status"] for row in assets} == {"APPROVED"}

    assert project_link["version_id"] == "snapshot_v1"
    assert project_link["character_version_id"] == version["id"]
    assert legacy_payload == json.dumps(
        {
            "project_id": "project_legacy",
            "character_id": "character_legacy",
            "character_snapshot": legacy_snapshot,
            "selected_by_user_id": "employee_1",
        },
        ensure_ascii=True,
        sort_keys=True,
    )
    assert restored["version_id"] == "snapshot_v1"
    assert restored["character_snapshot"] == legacy_snapshot


def test_legacy_template_hash_backfill_is_reversible(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy-template-hash-roundtrip.db"
    command.upgrade(alembic_config(db_path), "011_add_archive_retry_count")
    _seed_legacy_character(db_path)
    command.upgrade(alembic_config(db_path), "012_character_domain")
    with connect_database(db_path) as conn:
        before = conn.execute(
            "SELECT template_version, template_hash FROM character_versions"
        ).fetchone()
    assert before["template_version"] is None
    assert before["template_hash"] is None

    command.upgrade(alembic_config(db_path), "013_character_identity_assets")
    with connect_database(db_path) as conn:
        upgraded = conn.execute(
            "SELECT template_version, template_hash FROM character_versions"
        ).fetchone()
    assert upgraded["template_version"] == "legacy-character-v1"
    assert len(str(upgraded["template_hash"])) == 64

    command.downgrade(alembic_config(db_path), "012_character_domain")
    with connect_database(db_path) as conn:
        downgraded = conn.execute(
            "SELECT template_version, template_hash FROM character_versions"
        ).fetchone()
    assert downgraded["template_version"] is None
    assert downgraded["template_hash"] is None


def test_character_domain_downgrade_and_reupgrade_preserve_legacy_data(tmp_path: Path) -> None:
    db_path = tmp_path / "character-domain-roundtrip.db"
    command.upgrade(alembic_config(db_path), "011_add_archive_retry_count")
    legacy_snapshot = _seed_legacy_character(db_path)
    command.upgrade(alembic_config(db_path), "head")

    command.downgrade(alembic_config(db_path), "011_add_archive_retry_count")

    with connect_database(db_path) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            ).fetchall()
        }
        main_character_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(project_main_characters)")
        }
        character = conn.execute(
            "SELECT * FROM characters WHERE id = 'character_legacy'"
        ).fetchone()
        restored = get_project_main_character(conn, project_id="project_legacy")

    assert CHARACTER_DOMAIN_TABLES.isdisjoint(tables)
    assert "character_version_id" not in main_character_columns
    assert character["name"] == "Legacy Hero"
    assert restored["character_snapshot"] == legacy_snapshot

    command.upgrade(alembic_config(db_path), "head")

    with connect_database(db_path) as conn:
        imported_version = conn.execute(
            "SELECT id, persona_snapshot_json FROM character_versions"
        ).fetchone()
        project_link = conn.execute(
            "SELECT character_version_id, version_id FROM project_main_characters"
        ).fetchone()

    assert imported_version["id"] == "legacy-version:character_legacy"
    assert json.loads(imported_version["persona_snapshot_json"]) == legacy_snapshot
    assert project_link["character_version_id"] == imported_version["id"]
    assert project_link["version_id"] == "snapshot_v1"


def test_character_identity_asset_downgrade_roundtrip_preserves_object_and_reference(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "character-identity-asset-roundtrip.db"
    with initialize_database(db_path) as conn:
        conn.execute(
            """
            INSERT INTO users (id, username, display_name, role)
            VALUES ('admin_1', 'admin', 'Admin', 'admin')
            """
        )
        conn.execute(
            """
            INSERT INTO assets (
                id, project_id, kind, storage_uri, sha256, size_bytes,
                content_type, created_by_user_id
            )
            VALUES (
                'identity-source-1', NULL, 'character_source_image',
                'local://private/users/admin_1/identities/identity-1/source/identity-source-1.png',
                'source-sha', 128, 'image/png', 'admin_1'
            )
            """
        )
        conn.execute(
            """
            INSERT INTO person_identities (
                id, owner_user_id, display_name, authorization_status,
                authorization_scope, source_asset_id, source_quality_status,
                status, created_by
            )
            VALUES (
                'identity-1', 'admin_1', 'Hero', 'AUTHORIZED', '["internal"]',
                'identity-source-1', 'PASSED', 'ACTIVE', 'admin_1'
            )
            """
        )
        conn.commit()

    command.downgrade(alembic_config(db_path), "012_character_domain")
    with connect_database(db_path) as conn:
        downgraded = conn.execute(
            "SELECT project_id, storage_uri FROM assets WHERE id = 'identity-source-1'"
        ).fetchone()
        identity_source = conn.execute(
            "SELECT source_asset_id FROM person_identities WHERE id = 'identity-1'"
        ).fetchone()[0]

    assert downgraded["project_id"] == "migration-character-assets-project"
    assert str(downgraded["storage_uri"]).endswith(
        "/users/admin_1/identities/identity-1/source/identity-source-1.png"
    )
    assert identity_source == "identity-source-1"

    command.upgrade(alembic_config(db_path), "head")
    with connect_database(db_path) as conn:
        upgraded = conn.execute(
            "SELECT project_id FROM assets WHERE id = 'identity-source-1'"
        ).fetchone()
        placeholder_project = conn.execute(
            "SELECT 1 FROM projects WHERE id = 'migration-character-assets-project'"
        ).fetchone()
        placeholder_user = conn.execute(
            "SELECT 1 FROM users WHERE id = 'migration-character-assets'"
        ).fetchone()

    assert upgraded["project_id"] is None
    assert placeholder_project is None
    assert placeholder_user is None


def test_character_image_generation_migration_downgrade_roundtrip(tmp_path: Path) -> None:
    db_path = tmp_path / "character-image-generation-roundtrip.db"
    command.upgrade(alembic_config(db_path), "head")

    command.downgrade(alembic_config(db_path), "013_character_identity_assets")

    with connect_database(db_path) as conn:
        downgraded_version = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        downgraded_task_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(character_generation_tasks)")
        }
        downgraded_log_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(external_call_logs)")
        }

    assert downgraded_version == "013_character_identity_assets"
    assert "idempotency_key" not in downgraded_task_columns
    assert "character_generation_task_id" not in downgraded_log_columns

    command.upgrade(alembic_config(db_path), "head")

    with connect_database(db_path) as conn:
        upgraded_version = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        upgraded_task_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(character_generation_tasks)")
        }
        upgraded_log_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(external_call_logs)")
        }

    assert upgraded_version == "014_character_image_generation"
    assert "idempotency_key" in upgraded_task_columns
    assert "character_generation_task_id" in upgraded_log_columns


def test_character_image_generation_migration_backfills_existing_tasks(tmp_path: Path) -> None:
    db_path = tmp_path / "character-image-generation-backfill.db"
    config = alembic_config(db_path)
    command.upgrade(config, "013_character_identity_assets")

    with connect_database(db_path) as conn:
        conn.execute(
            """
            INSERT INTO users (id, username, display_name, role)
            VALUES ('user-1', 'admin', '管理员', 'admin')
            """
        )
        conn.execute(
            """
            INSERT INTO person_identities (
                id, display_name, authorization_status, authorization_scope,
                source_quality_status, status, created_by
            ) VALUES (
                'identity-1', '角色', 'AUTHORIZED', '["character_generation"]',
                'PASSED', 'ACTIVE', 'user-1'
            )
            """
        )
        conn.execute(
            """
            INSERT INTO character_personas (id, identity_id, name, created_by)
            VALUES ('persona-1', 'identity-1', '角色人设', 'user-1')
            """
        )
        conn.execute(
            """
            INSERT INTO character_versions (
                id, persona_id, version_number, provider, model, created_by
            ) VALUES ('version-1', 'persona-1', 1, 'fake', 'fake-character-v1', 'user-1')
            """
        )
        conn.executemany(
            """
            INSERT INTO character_generation_tasks (
                id, character_version_id, view_type, provider, model
            ) VALUES (?, 'version-1', 'FRONT_FACE', 'fake', 'fake-character-v1')
            """,
            [("legacy-task-1",), ("legacy-task-2",)],
        )
        conn.commit()

    command.upgrade(config, "head")

    with connect_database(db_path) as conn:
        tasks = conn.execute(
            """
            SELECT id, idempotency_key, request_hash, candidate_number,
                   attempt, max_attempts, created_at, updated_at
            FROM character_generation_tasks
            ORDER BY candidate_number
            """
        ).fetchall()

    assert [row["candidate_number"] for row in tasks] == [1, 2]
    assert [row["idempotency_key"] for row in tasks] == [
        "legacy:legacy-task-1",
        "legacy:legacy-task-2",
    ]
    assert [row["request_hash"] for row in tasks] == [
        "legacy:legacy-task-1",
        "legacy:legacy-task-2",
    ]
    assert all(row["attempt"] == 0 for row in tasks)
    assert all(row["max_attempts"] == 3 for row in tasks)
    assert all(row["created_at"] is not None for row in tasks)
    assert all(row["updated_at"] is not None for row in tasks)


def test_legacy_character_upgrade_preserves_revoked_and_expired_authorization(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "legacy-character-statuses.db"
    command.upgrade(alembic_config(db_path), "011_add_archive_retry_count")
    _seed_legacy_character(db_path)
    with connect_database(db_path) as conn:
        conn.executemany(
            """
            INSERT INTO characters (
                id, name, reference_asset_ids_json, authorization_project_ids_json,
                authorization_expires_at, is_active, created_by_user_id
            )
            VALUES (?, ?, '[]', ?, ?, ?, 'admin_1')
            """,
            [
                ("character_expired", "Expired Hero", "[]", "2020-01-01T00:00:00Z", 1),
                ("character_revoked", "Revoked Hero", "[]", None, 0),
                ("character_bad_scope", "Bad Scope Hero", "not-json", None, 1),
                ("character_bad_expiry", "Bad Expiry Hero", "[]", "not-a-date", 1),
            ],
        )
        conn.commit()

    command.upgrade(alembic_config(db_path), "head")

    with connect_database(db_path) as conn:
        states = {
            row["id"]: (row["status"], row["authorization_status"])
            for row in conn.execute(
                """
                SELECT id, status, authorization_status
                FROM person_identities
                WHERE id IN (
                    'legacy-identity:character_expired',
                    'legacy-identity:character_revoked',
                    'legacy-identity:character_bad_scope',
                    'legacy-identity:character_bad_expiry'
                )
                """
            )
        }

    assert states == {
        "legacy-identity:character_expired": ("EXPIRED", "EXPIRED"),
        "legacy-identity:character_revoked": ("REVOKED", "REVOKED"),
        "legacy-identity:character_bad_scope": ("REVOKED", "REVOKED"),
        "legacy-identity:character_bad_expiry": ("EXPIRED", "EXPIRED"),
    }


def test_legacy_selection_write_path_keeps_character_version_link_synchronized(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "legacy-character-selection.db"
    command.upgrade(alembic_config(db_path), "011_add_archive_retry_count")
    _seed_legacy_character(db_path)
    with connect_database(db_path) as conn:
        conn.execute(
            """
            INSERT INTO characters (
                id, name, reference_asset_ids_json, authorization_project_ids_json,
                is_active, created_by_user_id
            )
            VALUES (
                'character_second', 'Second Hero', '["asset_ref_1"]',
                '["project_legacy"]', 1, 'admin_1'
            )
            """
        )
        conn.commit()
    command.upgrade(alembic_config(db_path), "head")

    employee = CurrentUser(
        id="employee_1",
        username="employee",
        display_name="Employee",
        role="employee",
    )
    admin = CurrentUser(
        id="admin_1",
        username="admin",
        display_name="Admin",
        role="admin",
    )
    with connect_database(db_path) as conn:
        choose_project_main_character(
            conn,
            actor=employee,
            project_id="project_legacy",
            character_id="character_second",
        )
        linked_version = conn.execute(
            """
            SELECT character_version_id
            FROM project_main_characters
            WHERE project_id = 'project_legacy'
            """
        ).fetchone()[0]

        update_character(
            conn,
            actor=admin,
            character_id="character_second",
            name="Changed Second Hero",
            reference_asset_ids=None,
            authorization_project_ids=None,
            authorization_expires_at=None,
            clear_authorization_expires_at=False,
            is_active=None,
        )
        choose_project_main_character(
            conn,
            actor=employee,
            project_id="project_legacy",
            character_id="character_second",
        )
        stale_version = conn.execute(
            """
            SELECT character_version_id
            FROM project_main_characters
            WHERE project_id = 'project_legacy'
            """
        ).fetchone()[0]

        created_after_migration = create_character(
            conn,
            actor=admin,
            name="Post Migration Hero",
            reference_asset_ids=["asset_ref_1"],
            authorization_project_ids=["project_legacy"],
            authorization_expires_at=None,
            is_active=True,
        )
        choose_project_main_character(
            conn,
            actor=employee,
            project_id="project_legacy",
            character_id=created_after_migration.id,
        )
        cleared_version = conn.execute(
            """
            SELECT character_version_id
            FROM project_main_characters
            WHERE project_id = 'project_legacy'
            """
        ).fetchone()[0]

    assert linked_version == "legacy-version:character_second"
    assert stale_version == "legacy-version:character_second:2"
    assert cleared_version == f"legacy-version:{created_after_migration.id}"


def test_character_domain_constraints_reject_invalid_and_ambiguous_records(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "character-domain-constraints.db"

    with initialize_database(db_path) as conn:
        conn.execute(
            """
            INSERT INTO users (id, username, display_name)
            VALUES ('u1', 'u1', 'User One')
            """
        )
        conn.execute(
            """
            INSERT INTO projects (id, owner_user_id, name)
            VALUES ('project_1', 'u1', 'Project One')
            """
        )
        conn.executemany(
            """
            INSERT INTO assets (
                id, project_id, kind, storage_uri, sha256, size_bytes
            )
            VALUES (?, 'project_1', 'image', ?, ?, 1)
            """,
            [
                ("asset_1", "local://asset-1.png", "sha-1"),
                ("asset_2", "local://asset-2.png", "sha-2"),
            ],
        )
        conn.execute(
            """
            INSERT INTO person_identities (id, display_name, status)
            VALUES ('i1', 'Hero', 'ACTIVE')
            """
        )
        conn.execute(
            "INSERT INTO character_personas (id, identity_id, name) VALUES ('p1', 'i1', 'Hero')"
        )
        conn.execute(
            """
            INSERT INTO character_versions (id, persona_id, version_number, status)
            VALUES ('v1', 'p1', 1, 'DRAFT')
            """
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO character_versions (id, persona_id, version_number, status)
                VALUES ('v2', 'p1', 1, 'PUBLISHED')
                """
            )
        conn.execute(
            """
            INSERT INTO character_assets (
                id, character_version_id, asset_id, view_type,
                candidate_number, review_status, is_published_selection
            )
            VALUES ('ca1', 'v1', 'asset_1', 'FRONT_FACE', 1, 'APPROVED', 1)
            """
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO character_assets (
                    id, character_version_id, asset_id, view_type,
                    candidate_number, review_status, is_published_selection
                )
                VALUES ('ca2', 'v1', 'asset_2', 'FRONT_FACE', 2, 'APPROVED', 1)
                """
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO person_identities (id, display_name, status)
                VALUES ('i2', 'Bad', 'UNKNOWN')
                """
            )


def _seed_legacy_character(db_path: Path) -> dict[str, object]:
    snapshot: dict[str, object] = {
        "id": "character_legacy",
        "name": "Legacy Hero",
        "reference_asset_ids": ["asset_ref_2", "asset_missing", "asset_ref_1"],
        "authorization_project_ids": ["project_legacy"],
        "authorization_expires_at": None,
        "is_active": True,
    }
    payload = {
        "project_id": "project_legacy",
        "character_id": "character_legacy",
        "character_snapshot": snapshot,
        "selected_by_user_id": "employee_1",
    }
    with connect_database(db_path) as conn:
        conn.executemany(
            """
            INSERT INTO users (id, username, display_name, role)
            VALUES (?, ?, ?, ?)
            """,
            [
                ("admin_1", "admin", "Admin", "admin"),
                ("employee_1", "employee", "Employee", "employee"),
            ],
        )
        conn.execute(
            """
            INSERT INTO projects (id, owner_user_id, name)
            VALUES ('project_legacy', 'employee_1', 'Legacy Project')
            """
        )
        conn.executemany(
            """
            INSERT INTO assets (
                id, project_id, kind, storage_uri, sha256, size_bytes,
                content_type, created_by_user_id
            )
            VALUES (?, 'project_legacy', 'image', ?, ?, 12, 'image/png', 'admin_1')
            """,
            [
                ("asset_ref_2", "local://ref-2.png", "sha-ref-2"),
                ("asset_ref_1", "local://ref-1.png", "sha-ref-1"),
            ],
        )
        conn.execute(
            """
            INSERT INTO characters (
                id, name, reference_asset_ids_json, authorization_project_ids_json,
                authorization_expires_at, is_active, created_by_user_id,
                created_at, updated_at
            )
            VALUES (?, ?, ?, ?, NULL, 1, 'admin_1', ?, ?)
            """,
            (
                "character_legacy",
                "Legacy Hero",
                json.dumps(snapshot["reference_asset_ids"]),
                json.dumps(snapshot["authorization_project_ids"]),
                "2026-01-02T03:04:05Z",
                "2026-01-02T03:04:05Z",
            ),
        )
        conn.execute(
            """
            INSERT INTO versions (
                id, project_id, kind, version_number, payload_json, created_by_user_id
            )
            VALUES ('snapshot_v1', 'project_legacy', 'main_character', 1, ?, 'employee_1')
            """,
            (json.dumps(payload, ensure_ascii=True, sort_keys=True),),
        )
        conn.execute(
            """
            INSERT INTO project_main_characters (
                project_id, character_id, version_id, selected_by_user_id,
                selected_at
            )
            VALUES (
                'project_legacy', 'character_legacy', 'snapshot_v1',
                'employee_1', '2026-01-02T03:05:00Z'
            )
            """
        )
        conn.commit()
    return snapshot
