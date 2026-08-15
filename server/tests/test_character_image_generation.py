from __future__ import annotations

import json
import sqlite3
import struct
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest
from alembic import command
from fastapi.testclient import TestClient

from app.auth import get_database
from app.character_identity import SourceImageInspection, SourceImageInspector
from app.character_identity_routes import (
    get_character_storage,
    get_source_image_inspector,
)
from app.character_image_generation import (
    CharacterImageProviderFailed,
    CharacterImageRequest,
    CharacterImageResult,
    FakeCharacterImageProvider,
    run_next_character_generation_task,
)
from app.db import alembic_config, connect_database, initialize_database
from app.generation_worker import run_worker_once
from app.main import app
from app.media_routes import get_media_storage
from app.storage import FakeStorageAdapter, StoredObject, storage_object_ref_from_uri

REQUIRED_VIEWS = {
    "FRONT_FACE",
    "FRONT_HALF",
    "FRONT_FULL",
    "LEFT_45",
    "RIGHT_45",
    "LEFT_SIDE",
    "RIGHT_SIDE",
}


@dataclass
class PassingSourceInspector(SourceImageInspector):
    def inspect(self, content: bytes, *, content_type: str) -> SourceImageInspection:
        assert content
        assert content_type == "image/png"
        return SourceImageInspection(
            person_count=1,
            face_count=1,
            face_visible=True,
            sharpness_score=0.95,
            occlusion_detected=False,
            watermark_detected=False,
            notes=[],
            provider="fake-source-inspector",
            model="fake-source-v1",
        )


@dataclass
class MutatingCharacterImageProvider(FakeCharacterImageProvider):
    db_path: Path
    mutation: str

    def generate_view(self, request: CharacterImageRequest) -> CharacterImageResult:
        result = super().generate_view(request)
        with connect_database(self.db_path) as conn:
            if self.mutation == "revoke_identity":
                conn.execute(
                    """
                    UPDATE person_identities
                    SET authorization_status = 'REVOKED', status = 'REVOKED'
                    WHERE id = (
                        SELECT persona.identity_id
                        FROM character_versions AS version
                        JOIN character_personas AS persona ON persona.id = version.persona_id
                        WHERE version.id = ?
                    )
                    """,
                    (request.character_version_id,),
                )
            elif self.mutation == "archive_version":
                conn.execute(
                    "UPDATE character_versions SET status = 'ARCHIVED' WHERE id = ?",
                    (request.character_version_id,),
                )
            else:
                raise AssertionError(f"unsupported mutation: {self.mutation}")
            conn.commit()
        return result


@dataclass
class LeaseReplacingCharacterImageProvider(FakeCharacterImageProvider):
    db_path: Path
    fail_after_replacement: bool = False

    def generate_view(self, request: CharacterImageRequest) -> CharacterImageResult:
        result = super().generate_view(request)
        with connect_database(self.db_path) as conn:
            updated = conn.execute(
                """
                UPDATE character_generation_tasks
                SET locked_by = 'replacement-worker',
                    locked_until = datetime('now', '+5 minutes'),
                    attempt = attempt + 1,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND status = 'RUNNING'
                """,
                (request.task_id,),
            )
            assert updated.rowcount == 1
            conn.commit()
        if self.fail_after_replacement:
            raise CharacterImageProviderFailed(
                "CHARACTER_PROVIDER_TIMEOUT",
                "character image provider timed out",
                retriable=True,
            )
        return result


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "character-generation.db"
    with initialize_database(path) as conn:
        conn.executemany(
            """
            INSERT INTO users (id, username, display_name, role)
            VALUES (?, ?, ?, ?)
            """,
            [
                ("admin_1", "admin_1", "Admin One", "admin"),
                ("employee_1", "employee_1", "Employee One", "employee"),
                ("auditor_1", "auditor_1", "Auditor One", "auditor"),
            ],
        )
        conn.commit()
    return path


@pytest.fixture()
def storage() -> FakeStorageAdapter:
    return FakeStorageAdapter(provider="fake", bucket="character-private")


@pytest.fixture()
def client(db_path: Path, storage: FakeStorageAdapter) -> Iterator[TestClient]:
    def database_override() -> Iterator[sqlite3.Connection]:
        conn = connect_database(db_path)
        try:
            yield conn
        finally:
            conn.close()

    app.dependency_overrides[get_database] = database_override
    app.dependency_overrides[get_character_storage] = lambda: storage
    app.dependency_overrides[get_media_storage] = lambda: storage
    app.dependency_overrides[get_source_image_inspector] = PassingSourceInspector
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def headers(user_id: str) -> dict[str, str]:
    return {"X-Dev-User-Id": user_id}


def png_header(width: int = 1024, height: int = 1536) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", 13)
        + b"IHDR"
        + struct.pack(">II", width, height)
        + b"\x08\x02\x00\x00\x00"
    )


def create_character_version(
    client: TestClient,
    storage: FakeStorageAdapter,
    *,
    generation_params: dict[str, object] | None = None,
) -> dict[str, object]:
    identity_response = client.post(
        "/api/person-identities",
        headers=headers("admin_1"),
        json={
            "display_name": "荣哥",
            "owner_user_id": "employee_1",
            "authorization_scope": ["internal-short-video"],
            "authorization_expires_at": "2035-01-01T00:00:00Z",
        },
    )
    assert identity_response.status_code == 201
    identity_id = str(identity_response.json()["id"])

    authorization = b"%PDF-1.7\nportrait authorization"
    authorization_intent = client.post(
        f"/api/person-identities/{identity_id}/authorization-upload-intent",
        headers=headers("admin_1"),
        json={
            "filename": "authorization.pdf",
            "content_type": "application/pdf",
            "size_bytes": len(authorization),
        },
    ).json()
    storage.put_object(
        str(authorization_intent["storage_key"]),
        authorization,
        content_type="application/pdf",
    )
    assert (
        client.post(
            f"/api/person-identities/{identity_id}/authorization-upload-complete",
            headers=headers("admin_1"),
            json={"asset_id": authorization_intent["asset_id"]},
        ).status_code
        == 200
    )

    source = png_header()
    source_intent = client.post(
        f"/api/person-identities/{identity_id}/source-upload-intent",
        headers=headers("admin_1"),
        json={
            "filename": "source.png",
            "content_type": "image/png",
            "size_bytes": len(source),
        },
    ).json()
    storage.put_object(str(source_intent["storage_key"]), source, content_type="image/png")
    assert (
        client.post(
            f"/api/person-identities/{identity_id}/source-upload-complete",
            headers=headers("admin_1"),
            json={"asset_id": source_intent["asset_id"]},
        ).status_code
        == 200
    )

    persona_response = client.post(
        f"/api/person-identities/{identity_id}/personas",
        headers=headers("admin_1"),
        json={
            "name": "乡墅项目管理专家",
            "occupation": "乡墅项目管理",
            "costume_description": "工程马甲和安全帽",
            "positive_prompt": "自然、稳定、真实",
        },
    )
    assert persona_response.status_code == 201
    version_response = client.post(
        f"/api/character-personas/{persona_response.json()['id']}/versions",
        headers=headers("admin_1"),
        json={
            "provider": "fake_character",
            "model": "fake-character-v1",
            "generation_params_json": generation_params or {},
        },
    )
    assert version_response.status_code == 201
    return cast(dict[str, object], version_response.json())


def enqueue(
    client: TestClient,
    version_id: str,
    *,
    key: str,
    views: list[str] | None = None,
    candidates_per_view: int = 1,
    user_id: str = "admin_1",
) -> object:
    payload: dict[str, object] = {
        "idempotency_key": key,
        "candidates_per_view": candidates_per_view,
    }
    if views is not None:
        payload["view_types"] = views
    return client.post(
        f"/api/character-versions/{version_id}/generate-assets",
        headers=headers(user_id),
        json=payload,
    )


def test_generation_migration_adds_reversible_lease_and_call_log_contract(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "migration.db"
    command.upgrade(alembic_config(db_path), "014_character_image_generation")
    with connect_database(db_path) as conn:
        task_columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(character_generation_tasks)")
        }
        task_indexes = {
            str(row[1]) for row in conn.execute("PRAGMA index_list(character_generation_tasks)")
        }
        call_columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(external_call_logs)")
        }
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
    } <= task_columns
    assert "idx_character_generation_tasks_lease" in task_indexes
    assert "uq_character_generation_tasks_candidate" in task_indexes
    assert "character_generation_task_id" in call_columns

    command.downgrade(alembic_config(db_path), "013_character_identity_assets")
    with connect_database(db_path) as conn:
        downgraded_task_columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(character_generation_tasks)")
        }
        downgraded_call_columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(external_call_logs)")
        }
    assert "attempt" not in downgraded_task_columns
    assert "character_generation_task_id" not in downgraded_call_columns


def test_admin_queues_seven_views_idempotently_and_roles_fail_closed(
    client: TestClient,
    db_path: Path,
    storage: FakeStorageAdapter,
) -> None:
    version = create_character_version(client, storage)
    version_id = str(version["id"])

    employee = enqueue(client, version_id, key="all-views", user_id="employee_1")
    auditor = enqueue(client, version_id, key="all-views", user_id="auditor_1")
    first = enqueue(client, version_id, key="all-views")
    replay = enqueue(client, version_id, key="all-views")
    conflict = enqueue(client, version_id, key="all-views", views=["FRONT_FACE"])

    assert employee.status_code == 403
    assert auditor.status_code == 403
    assert first.status_code == 202
    assert replay.status_code == 202
    assert {task["view_type"] for task in first.json()} == REQUIRED_VIEWS
    assert [task["id"] for task in replay.json()] == [task["id"] for task in first.json()]
    assert all(task["status"] == "PENDING" for task in first.json())
    assert all(task["candidate_number"] == 1 for task in first.json())
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "IDEMPOTENCY_CONFLICT"
    employee_tasks = client.get(
        f"/api/character-versions/{version_id}/generation-tasks",
        headers=headers("employee_1"),
    )
    auditor_tasks = client.get(
        f"/api/character-versions/{version_id}/generation-tasks",
        headers=headers("auditor_1"),
    )
    assert employee_tasks.status_code == 403
    assert auditor_tasks.status_code == 200
    assert len(auditor_tasks.json()) == 7
    with connect_database(db_path) as conn:
        task_count = conn.execute(
            "SELECT COUNT(*) FROM character_generation_tasks WHERE character_version_id = ?",
            (version_id,),
        ).fetchone()[0]
        status = conn.execute(
            "SELECT status FROM character_versions WHERE id = ?",
            (version_id,),
        ).fetchone()[0]
    assert task_count == 7
    assert status == "GENERATING"


def test_generation_rejects_identity_revoked_before_or_after_enqueue(
    client: TestClient,
    db_path: Path,
    storage: FakeStorageAdapter,
) -> None:
    version = create_character_version(client, storage)
    version_id = str(version["id"])
    with connect_database(db_path) as conn:
        identity_id = conn.execute(
            """
            SELECT persona.identity_id
            FROM character_versions AS version
            JOIN character_personas AS persona ON persona.id = version.persona_id
            WHERE version.id = ?
            """,
            (version_id,),
        ).fetchone()[0]
        conn.execute(
            """
            UPDATE person_identities
            SET authorization_status = 'REVOKED', status = 'REVOKED'
            WHERE id = ?
            """,
            (identity_id,),
        )
        conn.commit()

    rejected = enqueue(client, version_id, key="revoked-before", views=["FRONT_FACE"])
    assert rejected.status_code == 409
    assert rejected.json()["detail"]["code"] == "IDENTITY_NOT_ACTIVE"

    with connect_database(db_path) as conn:
        conn.execute(
            """
            UPDATE person_identities
            SET authorization_status = 'AUTHORIZED', status = 'ACTIVE'
            WHERE id = ?
            """,
            (identity_id,),
        )
        conn.commit()
    assert enqueue(client, version_id, key="revoked-after", views=["FRONT_FACE"]).status_code == 202

    with connect_database(db_path) as conn:
        conn.execute(
            """
            UPDATE person_identities
            SET authorization_status = 'REVOKED', status = 'REVOKED'
            WHERE id = ?
            """,
            (identity_id,),
        )
        conn.commit()
        result = run_next_character_generation_task(
            conn,
            worker_id="revoked-worker",
            storage=storage,
            provider=FakeCharacterImageProvider(),
        )
        asset_count = conn.execute("SELECT COUNT(*) FROM character_assets").fetchone()[0]
        call_error = conn.execute(
            """
            SELECT error_code
            FROM external_call_logs
            WHERE character_generation_task_id = ?
            """,
            (result.id,),
        ).fetchone()[0]

    assert result.status == "FAILED"
    assert result.error_code == "IDENTITY_NOT_ACTIVE"
    assert asset_count == 0
    assert call_error == "IDENTITY_NOT_ACTIVE"


def test_worker_fails_cleanly_when_version_is_archived_after_enqueue(
    client: TestClient,
    db_path: Path,
    storage: FakeStorageAdapter,
) -> None:
    version = create_character_version(client, storage)
    version_id = str(version["id"])
    assert (
        enqueue(client, version_id, key="archived-after", views=["FRONT_FACE"]).status_code == 202
    )
    assert (
        client.post(
            f"/api/character-versions/{version_id}/archive",
            headers=headers("admin_1"),
        ).status_code
        == 200
    )

    with connect_database(db_path) as conn:
        result = run_next_character_generation_task(
            conn,
            worker_id="archived-worker",
            storage=storage,
            provider=FakeCharacterImageProvider(),
        )

    assert result is not None
    assert result.status == "FAILED"
    assert result.error_code == "CHARACTER_VERSION_NOT_GENERATABLE"


def test_worker_rejects_source_content_changed_after_version_freeze(
    client: TestClient,
    db_path: Path,
    storage: FakeStorageAdapter,
) -> None:
    version = create_character_version(client, storage)
    version_id = str(version["id"])
    assert (
        enqueue(client, version_id, key="source-changed", views=["FRONT_FACE"]).status_code == 202
    )
    with connect_database(db_path) as conn:
        source_uri = conn.execute(
            """
            SELECT asset.storage_uri
            FROM character_versions AS version
            JOIN assets AS asset ON asset.id = version.source_asset_id
            WHERE version.id = ?
            """,
            (version_id,),
        ).fetchone()[0]
    source_key = storage_object_ref_from_uri(str(source_uri)).key
    storage.put_object(source_key, png_header() + b"changed", content_type="image/png")

    with connect_database(db_path) as conn:
        result = run_next_character_generation_task(
            conn,
            worker_id="source-check-worker",
            storage=storage,
            provider=FakeCharacterImageProvider(),
        )
        asset_count = conn.execute("SELECT COUNT(*) FROM character_assets").fetchone()[0]

    assert result is not None
    assert result.status == "FAILED"
    assert result.error_code == "CHARACTER_VERSION_SOURCE_CHANGED"
    assert asset_count == 0


@pytest.mark.parametrize(
    ("mutation", "error_code"),
    [
        ("revoke_identity", "IDENTITY_NOT_ACTIVE"),
        ("archive_version", "CHARACTER_VERSION_NOT_GENERATABLE"),
    ],
)
def test_worker_revalidates_state_after_provider_returns(
    client: TestClient,
    db_path: Path,
    storage: FakeStorageAdapter,
    mutation: str,
    error_code: str,
) -> None:
    version = create_character_version(client, storage)
    version_id = str(version["id"])
    assert enqueue(client, version_id, key=mutation, views=["FRONT_FACE"]).status_code == 202

    with connect_database(db_path) as conn:
        result = run_next_character_generation_task(
            conn,
            worker_id="state-race-worker",
            storage=storage,
            provider=MutatingCharacterImageProvider(db_path, mutation),
        )
        asset_count = conn.execute("SELECT COUNT(*) FROM character_assets").fetchone()[0]

    assert result is not None
    assert result.status == "FAILED"
    assert result.error_code == error_code
    assert asset_count == 0


def test_worker_revalidates_state_in_success_transaction_and_cleans_object(
    client: TestClient,
    db_path: Path,
    storage: FakeStorageAdapter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    version = create_character_version(client, storage)
    version_id = str(version["id"])
    assert (
        enqueue(client, version_id, key="archive-during-store", views=["FRONT_FACE"]).status_code
        == 202
    )
    original_put = storage.put_object
    generated_key: list[str] = []

    def archive_during_generated_put(
        key: str,
        content: bytes,
        *,
        content_type: str,
    ) -> StoredObject:
        stored = original_put(key, content, content_type=content_type)
        if "/generated/" in stored.key:
            generated_key.append(stored.key)
            with connect_database(db_path) as mutation_conn:
                mutation_conn.execute(
                    "UPDATE character_versions SET status = 'ARCHIVED' WHERE id = ?",
                    (version_id,),
                )
                mutation_conn.commit()
        return stored

    monkeypatch.setattr(storage, "put_object", archive_during_generated_put)
    with connect_database(db_path) as conn:
        result = run_next_character_generation_task(
            conn,
            worker_id="storage-race-worker",
            storage=storage,
            provider=FakeCharacterImageProvider(),
        )
        asset_count = conn.execute("SELECT COUNT(*) FROM character_assets").fetchone()[0]

    assert result is not None
    assert result.status == "FAILED"
    assert result.error_code == "CHARACTER_VERSION_NOT_GENERATABLE"
    assert asset_count == 0
    assert len(generated_key) == 1
    assert storage.head_object(generated_key[0]) is None


@pytest.mark.parametrize("provider_fails", [False, True])
def test_stale_worker_cannot_finalize_after_lease_is_reassigned(
    client: TestClient,
    db_path: Path,
    storage: FakeStorageAdapter,
    provider_fails: bool,
) -> None:
    version = create_character_version(client, storage)
    version_id = str(version["id"])
    assert (
        enqueue(client, version_id, key="lease-reassigned", views=["FRONT_FACE"]).status_code == 202
    )

    with connect_database(db_path) as conn:
        stale_result = run_next_character_generation_task(
            conn,
            worker_id="stale-worker",
            storage=storage,
            provider=LeaseReplacingCharacterImageProvider(
                db_path,
                fail_after_replacement=provider_fails,
            ),
        )
        task_after_stale = conn.execute(
            """
            SELECT status, locked_by, attempt
            FROM character_generation_tasks
            WHERE character_version_id = ?
            """,
            (version_id,),
        ).fetchone()
        asset_count_after_stale = conn.execute(
            "SELECT COUNT(*) FROM character_assets WHERE character_version_id = ?",
            (version_id,),
        ).fetchone()[0]
        stale_audit_count = conn.execute(
            """
            SELECT COUNT(*)
            FROM audit_logs
            WHERE action = 'character_generation.stale_worker_ignored'
            """
        ).fetchone()[0]

    assert stale_result is not None
    assert stale_result.status == "RUNNING"
    assert dict(task_after_stale) == {
        "status": "RUNNING",
        "locked_by": "replacement-worker",
        "attempt": 2,
    }
    assert asset_count_after_stale == 0
    assert stale_audit_count == 1

    with connect_database(db_path) as conn:
        conn.execute(
            """
            UPDATE character_generation_tasks
            SET locked_until = datetime('now', '-1 minute')
            WHERE character_version_id = ?
            """,
            (version_id,),
        )
        conn.commit()
        final_result = run_next_character_generation_task(
            conn,
            worker_id="recovery-worker",
            storage=storage,
            provider=FakeCharacterImageProvider(),
        )
        final_asset_count = conn.execute(
            "SELECT COUNT(*) FROM character_assets WHERE character_version_id = ?",
            (version_id,),
        ).fetchone()[0]

    assert final_result is not None
    assert final_result.status == "SUCCEEDED"
    assert final_result.attempt == 3
    assert final_asset_count == 1


def test_stale_worker_cleans_object_when_lease_changes_during_storage(
    client: TestClient,
    db_path: Path,
    storage: FakeStorageAdapter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    version = create_character_version(client, storage)
    version_id = str(version["id"])
    assert (
        enqueue(client, version_id, key="lease-during-store", views=["FRONT_FACE"]).status_code
        == 202
    )
    original_put = storage.put_object
    generated_key: list[str] = []

    def replace_lease_during_generated_put(
        key: str,
        content: bytes,
        *,
        content_type: str,
    ) -> StoredObject:
        stored = original_put(key, content, content_type=content_type)
        if "/generated/" in stored.key:
            generated_key.append(stored.key)
            with connect_database(db_path) as replacement_conn:
                updated = replacement_conn.execute(
                    """
                    UPDATE character_generation_tasks
                    SET locked_by = 'replacement-worker',
                        locked_until = datetime('now', '+5 minutes'),
                        attempt = attempt + 1,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE character_version_id = ? AND status = 'RUNNING'
                    """,
                    (version_id,),
                )
                assert updated.rowcount == 1
                replacement_conn.commit()
        return stored

    monkeypatch.setattr(storage, "put_object", replace_lease_during_generated_put)
    with connect_database(db_path) as conn:
        result = run_next_character_generation_task(
            conn,
            worker_id="storage-stale-worker",
            storage=storage,
            provider=FakeCharacterImageProvider(),
        )
        asset_count = conn.execute(
            "SELECT COUNT(*) FROM character_assets WHERE character_version_id = ?",
            (version_id,),
        ).fetchone()[0]
        stale_call = conn.execute(
            """
            SELECT error_code, provider_request_id
            FROM external_call_logs
            WHERE character_generation_task_id = ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (result.id,),
        ).fetchone()

    assert result is not None
    assert result.status == "RUNNING"
    assert result.attempt == 2
    assert asset_count == 0
    assert len(generated_key) == 1
    assert storage.head_object(generated_key[0]) is None
    assert stale_call["error_code"] == "CHARACTER_LEASE_LOST"
    assert stale_call["provider_request_id"] is not None


def test_desktop_worker_generates_all_views_and_writes_redacted_evidence(
    client: TestClient,
    db_path: Path,
    storage: FakeStorageAdapter,
) -> None:
    version = create_character_version(client, storage)
    version_id = str(version["id"])
    response = enqueue(client, version_id, key="worker-success")
    assert response.status_code == 202

    with connect_database(db_path) as conn:
        processed = run_worker_once(
            conn,
            worker_id="character-worker",
            storage=storage,
            character_provider=FakeCharacterImageProvider(),
        )
        tasks = conn.execute(
            "SELECT * FROM character_generation_tasks ORDER BY view_type"
        ).fetchall()
        assets = conn.execute(
            """
            SELECT character_assets.*, assets.storage_uri, assets.sha256
            FROM character_assets
            JOIN assets ON assets.id = character_assets.asset_id
            WHERE character_assets.character_version_id = ?
            ORDER BY character_assets.view_type
            """,
            (version_id,),
        ).fetchall()
        calls = conn.execute(
            """
            SELECT * FROM external_call_logs
            WHERE character_generation_task_id IS NOT NULL
            ORDER BY created_at
            """
        ).fetchall()
        version_status = conn.execute(
            "SELECT status FROM character_versions WHERE id = ?",
            (version_id,),
        ).fetchone()[0]

    assert processed == 7
    assert {str(task["status"]) for task in tasks} == {"SUCCEEDED"}
    assert len(assets) == 7
    assert {str(asset["view_type"]) for asset in assets} == REQUIRED_VIEWS
    assert {int(asset["candidate_number"]) for asset in assets} == {1}
    assert {str(asset["review_status"]) for asset in assets} == {"NOT_REVIEWED"}
    quality_results = [json.loads(str(asset["auto_quality_json"])) for asset in assets]
    assert {quality["schema_version"] for quality in quality_results} == {"character-quality.v1"}
    assert all(quality["simulated"] is True for quality in quality_results)
    assert all(quality["blocking_issue_codes"] == [] for quality in quality_results)
    assert all("identity_consistency" in quality["scores"] for quality in quality_results)
    assert version_status == "REVIEWING"
    assert len(calls) == 7
    assert all(call["request_hash"] and call["response_asset_id"] for call in calls)
    assert all(call["error_message_redacted"] is None for call in calls)
    call_payload = json.dumps([dict(row) for row in calls], ensure_ascii=False)
    assert "荣哥" not in call_payload
    assert "乡墅项目管理专家" not in call_payload
    assert "positive_prompt" not in call_payload
    for asset in assets:
        reference = storage_object_ref_from_uri(str(asset["storage_uri"]))
        assert reference.key.startswith("users/employee_1/personas/")
        assert storage.get_object(reference.key).startswith(b"\x89PNG\r\n\x1a\n")


def test_one_invalid_view_does_not_block_the_other_six(
    client: TestClient,
    db_path: Path,
    storage: FakeStorageAdapter,
) -> None:
    version = create_character_version(
        client,
        storage,
        generation_params={
            "fake_behavior_by_view": {"LEFT_SIDE": {"type": "invalid_response", "fail_attempts": 1}}
        },
    )
    version_id = str(version["id"])
    assert enqueue(client, version_id, key="isolated-failure").status_code == 202

    with connect_database(db_path) as conn:
        processed = run_worker_once(
            conn,
            worker_id="character-worker",
            storage=storage,
            character_provider=FakeCharacterImageProvider(),
        )
        rows = conn.execute(
            "SELECT view_type, status, error_code FROM character_generation_tasks"
        ).fetchall()
        asset_count = conn.execute("SELECT COUNT(*) FROM character_assets").fetchone()[0]
        version_status = conn.execute(
            "SELECT status FROM character_versions WHERE id = ?", (version_id,)
        ).fetchone()[0]

    assert processed == 7
    assert {
        str(row["view_type"]) for row in rows if str(row["status"]) == "SUCCEEDED"
    } == REQUIRED_VIEWS - {"LEFT_SIDE"}
    failed = next(row for row in rows if str(row["view_type"]) == "LEFT_SIDE")
    assert failed["status"] == "FAILED"
    assert failed["error_code"] == "CHARACTER_PROVIDER_INVALID_RESPONSE"
    assert asset_count == 6
    assert version_status == "REVIEWING"


@pytest.mark.parametrize(
    ("failure_type", "error_code"),
    [
        ("timeout", "CHARACTER_PROVIDER_TIMEOUT"),
        ("rate_limit", "CHARACTER_PROVIDER_RATE_LIMITED"),
        ("server_error", "CHARACTER_PROVIDER_UNAVAILABLE"),
    ],
)
def test_transient_provider_failures_retry_then_succeed(
    client: TestClient,
    db_path: Path,
    storage: FakeStorageAdapter,
    failure_type: str,
    error_code: str,
) -> None:
    version = create_character_version(
        client,
        storage,
        generation_params={
            "fake_behavior_by_view": {"FRONT_FACE": {"type": failure_type, "fail_attempts": 1}}
        },
    )
    version_id = str(version["id"])
    assert (
        enqueue(client, version_id, key=f"retry-{failure_type}", views=["FRONT_FACE"]).status_code
        == 202
    )

    with connect_database(db_path) as conn:
        first = run_next_character_generation_task(
            conn,
            worker_id="character-worker",
            storage=storage,
            provider=FakeCharacterImageProvider(),
        )
        pending = conn.execute(
            "SELECT status, attempt, error_code, next_poll_at FROM character_generation_tasks"
        ).fetchone()
        conn.execute("UPDATE character_generation_tasks SET next_poll_at = CURRENT_TIMESTAMP")
        conn.commit()
        second = run_next_character_generation_task(
            conn,
            worker_id="character-worker-restarted",
            storage=storage,
            provider=FakeCharacterImageProvider(),
        )
        succeeded = conn.execute(
            "SELECT status, attempt, error_code FROM character_generation_tasks"
        ).fetchone()
        call_count = conn.execute(
            "SELECT COUNT(*) FROM external_call_logs WHERE character_generation_task_id IS NOT NULL"
        ).fetchone()[0]

    assert first is not None
    assert pending["status"] == "PENDING"
    assert pending["attempt"] == 1
    assert pending["error_code"] == error_code
    assert pending["next_poll_at"] is not None
    assert second is not None
    assert succeeded["status"] == "SUCCEEDED"
    assert succeeded["attempt"] == 2
    assert succeeded["error_code"] is None
    assert call_count == 2


def test_retry_limit_and_expired_running_lease_are_recoverable(
    client: TestClient,
    db_path: Path,
    storage: FakeStorageAdapter,
) -> None:
    failing_version = create_character_version(
        client,
        storage,
        generation_params={
            "fake_behavior_by_view": {"RIGHT_SIDE": {"type": "timeout", "fail_attempts": 99}}
        },
    )
    assert (
        enqueue(
            client,
            str(failing_version["id"]),
            key="retry-limit",
            views=["RIGHT_SIDE"],
        ).status_code
        == 202
    )
    with connect_database(db_path) as conn:
        for _ in range(3):
            assert (
                run_next_character_generation_task(
                    conn,
                    worker_id="retry-worker",
                    storage=storage,
                    provider=FakeCharacterImageProvider(),
                )
                is not None
            )
            conn.execute(
                """
                UPDATE character_generation_tasks
                SET next_poll_at = CURRENT_TIMESTAMP
                WHERE status = 'PENDING'
                """
            )
            conn.commit()
        failed = conn.execute(
            "SELECT status, attempt, next_poll_at FROM character_generation_tasks"
        ).fetchone()
    assert failed["status"] == "FAILED"
    assert failed["attempt"] == 3
    assert failed["next_poll_at"] is None

    recovered_version = create_character_version(client, storage)
    recovered_version_id = str(recovered_version["id"])
    assert (
        enqueue(
            client,
            recovered_version_id,
            key="expired-lease",
            views=["LEFT_45"],
        ).status_code
        == 202
    )
    with connect_database(db_path) as conn:
        conn.execute(
            """
            UPDATE character_generation_tasks
            SET status = 'RUNNING', locked_by = 'dead-worker',
                locked_until = datetime('now', '-1 minute')
            WHERE character_version_id = ?
            """,
            (recovered_version_id,),
        )
        conn.commit()
        recovered = run_next_character_generation_task(
            conn,
            worker_id="replacement-worker",
            storage=storage,
            provider=FakeCharacterImageProvider(),
        )
        row = conn.execute(
            """
            SELECT status, attempt, locked_by, locked_until
            FROM character_generation_tasks WHERE character_version_id = ?
            """,
            (recovered_version_id,),
        ).fetchone()
    assert recovered is not None
    assert row["status"] == "SUCCEEDED"
    assert row["attempt"] == 1
    assert row["locked_by"] is None
    assert row["locked_until"] is None


def test_expired_final_attempt_is_failed_instead_of_stuck_running(
    client: TestClient,
    db_path: Path,
    storage: FakeStorageAdapter,
) -> None:
    version = create_character_version(client, storage)
    version_id = str(version["id"])
    assert enqueue(client, version_id, key="expired-final", views=["RIGHT_45"]).status_code == 202

    with connect_database(db_path) as conn:
        conn.execute(
            """
            UPDATE character_generation_tasks
            SET status = 'RUNNING', attempt = max_attempts,
                locked_by = 'dead-final-worker',
                locked_until = datetime('now', '-1 minute')
            WHERE character_version_id = ?
            """,
            (version_id,),
        )
        conn.commit()
        assert (
            run_next_character_generation_task(
                conn,
                worker_id="replacement-worker",
                storage=storage,
                provider=FakeCharacterImageProvider(),
            )
            is None
        )
        task = conn.execute(
            """
            SELECT id, status, attempt, error_code, error_message_redacted,
                   locked_by, locked_until, next_poll_at, completed_at
            FROM character_generation_tasks
            WHERE character_version_id = ?
            """,
            (version_id,),
        ).fetchone()
        version_status = conn.execute(
            "SELECT status FROM character_versions WHERE id = ?",
            (version_id,),
        ).fetchone()[0]
        call_error = conn.execute(
            """
            SELECT error_code
            FROM external_call_logs
            WHERE character_generation_task_id = ?
            """,
            (task["id"],),
        ).fetchone()[0]
        audit_count = conn.execute(
            """
            SELECT COUNT(*) FROM audit_logs
            WHERE entity_id = ? AND action = 'character_generation.lease_expired'
            """,
            (task["id"],),
        ).fetchone()[0]

    assert task["status"] == "FAILED"
    assert task["attempt"] == 3
    assert task["error_code"] == "CHARACTER_LEASE_EXPIRED"
    assert task["error_message_redacted"] == "character generation lease expired"
    assert task["locked_by"] is None
    assert task["locked_until"] is None
    assert task["next_poll_at"] is None
    assert task["completed_at"] is not None
    assert version_status == "FAILED"
    assert call_error == "CHARACTER_LEASE_EXPIRED"
    assert audit_count == 1


def test_regeneration_creates_a_new_candidate_without_overwriting_review(
    client: TestClient,
    db_path: Path,
    storage: FakeStorageAdapter,
) -> None:
    version = create_character_version(client, storage)
    version_id = str(version["id"])
    assert (
        enqueue(client, version_id, key="first-candidate", views=["FRONT_FACE"]).status_code == 202
    )
    with connect_database(db_path) as conn:
        assert (
            run_next_character_generation_task(
                conn,
                worker_id="character-worker",
                storage=storage,
                provider=FakeCharacterImageProvider(),
            )
            is not None
        )
        original = conn.execute("SELECT * FROM character_assets").fetchone()
        conn.execute(
            "UPDATE character_assets SET review_status = 'REJECTED' WHERE id = ?",
            (original["id"],),
        )
        conn.commit()

    regenerated = client.post(
        f"/api/character-assets/{original['id']}/regenerate",
        headers=headers("admin_1"),
        json={"idempotency_key": "regenerate-front-face"},
    )
    assert regenerated.status_code == 202
    assert len(regenerated.json()) == 1
    assert regenerated.json()[0]["candidate_number"] == 2
    with connect_database(db_path) as conn:
        assert (
            run_next_character_generation_task(
                conn,
                worker_id="character-worker",
                storage=storage,
                provider=FakeCharacterImageProvider(),
            )
            is not None
        )
        assets = conn.execute(
            """
            SELECT candidate_number, review_status, asset_id
            FROM character_assets
            WHERE character_version_id = ? AND view_type = 'FRONT_FACE'
            ORDER BY candidate_number
            """,
            (version_id,),
        ).fetchall()
    assert [int(asset["candidate_number"]) for asset in assets] == [1, 2]
    assert assets[0]["review_status"] == "REJECTED"
    assert assets[1]["review_status"] == "NOT_REVIEWED"
    assert assets[0]["asset_id"] != assets[1]["asset_id"]
