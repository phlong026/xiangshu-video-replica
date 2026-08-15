from __future__ import annotations

import json
import sqlite3
import struct
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.auth import get_database
from app.character_identity import (
    REQUIRED_CHARACTER_VIEW_TYPES,
    SourceImageInspection,
    SourceImageInspector,
    approved_character_asset_key,
    generated_character_asset_key,
)
from app.character_identity_routes import (
    get_character_storage,
    get_source_image_inspector,
)
from app.db import connect_database, initialize_database
from app.main import app
from app.media_routes import get_media_storage
from app.storage import FakeStorageAdapter


@dataclass
class FakeSourceImageInspector(SourceImageInspector):
    result: SourceImageInspection

    def inspect(
        self,
        content: bytes,
        *,
        content_type: str,
    ) -> SourceImageInspection:
        assert content
        assert content_type in {"image/jpeg", "image/png"}
        return self.result


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "character-identity.db"
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
    return FakeStorageAdapter(provider="local", bucket="private-bucket")


@pytest.fixture()
def inspector() -> FakeSourceImageInspector:
    return FakeSourceImageInspector(
        SourceImageInspection(
            person_count=1,
            face_count=1,
            face_visible=True,
            sharpness_score=0.92,
            occlusion_detected=False,
            watermark_detected=False,
            notes=[],
            provider="fake-source-inspector",
            model="fake-source-v1",
        )
    )


@pytest.fixture()
def client(
    db_path: Path,
    storage: FakeStorageAdapter,
    inspector: FakeSourceImageInspector,
) -> Iterator[TestClient]:
    def database_override() -> Iterator[sqlite3.Connection]:
        conn = connect_database(db_path)
        try:
            yield conn
        finally:
            conn.close()

    app.dependency_overrides[get_database] = database_override
    app.dependency_overrides[get_character_storage] = lambda: storage
    app.dependency_overrides[get_media_storage] = lambda: storage
    app.dependency_overrides[get_source_image_inspector] = lambda: inspector
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


def create_identity(
    client: TestClient,
    *,
    expires_at: str = "2035-01-01T00:00:00Z",
) -> dict[str, object]:
    response = client.post(
        "/api/person-identities",
        headers=headers("admin_1"),
        json={
            "display_name": "荣哥",
            "owner_user_id": "employee_1",
            "authorization_scope": ["internal-short-video"],
            "authorization_expires_at": expires_at,
        },
    )
    assert response.status_code == 201
    return cast(dict[str, object], response.json())


def upload_authorization(
    client: TestClient,
    storage: FakeStorageAdapter,
    identity_id: str,
) -> dict[str, object]:
    content = b"%PDF-1.7\nportrait authorization"
    intent_response = client.post(
        f"/api/person-identities/{identity_id}/authorization-upload-intent",
        headers=headers("admin_1"),
        json={
            "filename": "authorization.pdf",
            "content_type": "application/pdf",
            "size_bytes": len(content),
        },
    )
    assert intent_response.status_code == 200
    intent = cast(dict[str, object], intent_response.json())
    storage.put_object(str(intent["storage_key"]), content, content_type="application/pdf")
    complete = client.post(
        f"/api/person-identities/{identity_id}/authorization-upload-complete",
        headers=headers("admin_1"),
        json={"asset_id": intent["asset_id"]},
    )
    assert complete.status_code == 200
    return cast(dict[str, object], complete.json())


def upload_source(
    client: TestClient,
    storage: FakeStorageAdapter,
    identity_id: str,
    *,
    content: bytes | None = None,
) -> dict[str, object]:
    image = content or png_header()
    intent_response = client.post(
        f"/api/person-identities/{identity_id}/source-upload-intent",
        headers=headers("admin_1"),
        json={
            "filename": "source.png",
            "content_type": "image/png",
            "size_bytes": len(image),
        },
    )
    assert intent_response.status_code == 200
    intent = cast(dict[str, object], intent_response.json())
    storage.put_object(str(intent["storage_key"]), image, content_type="image/png")
    complete = client.post(
        f"/api/person-identities/{identity_id}/source-upload-complete",
        headers=headers("admin_1"),
        json={"asset_id": intent["asset_id"]},
    )
    assert complete.status_code == 200
    return cast(dict[str, object], complete.json())


def activate_identity(
    client: TestClient,
    storage: FakeStorageAdapter,
) -> dict[str, object]:
    identity = create_identity(client)
    identity_id = str(identity["id"])
    upload_authorization(client, storage, identity_id)
    completed = upload_source(client, storage, identity_id)
    return cast(dict[str, object], completed["identity"])


def create_persona(
    client: TestClient,
    identity_id: str,
    *,
    name: str = "乡墅项目管理专家",
) -> dict[str, object]:
    response = client.post(
        f"/api/person-identities/{identity_id}/personas",
        headers=headers("admin_1"),
        json={
            "name": name,
            "occupation": "项目管理专家",
            "scene_description": "乡村自建别墅工地",
            "appearance_constraints_json": {"hair": "short"},
            "costume_description": "工程马甲和安全帽",
            "default_background": "施工现场",
            "positive_prompt": "专业、真实",
            "negative_prompt": "不要卡通化",
            "usage_scope_json": ["internal-short-video"],
        },
    )
    assert response.status_code == 201
    return cast(dict[str, object], response.json())


def create_version(
    client: TestClient,
    persona_id: str,
    *,
    model: str = "fake-character-v1",
) -> dict[str, object]:
    response = client.post(
        f"/api/character-personas/{persona_id}/versions",
        headers=headers("admin_1"),
        json={
            "provider": "fake",
            "model": model,
            "generation_params_json": {"seed": 42},
        },
    )
    assert response.status_code == 201
    return cast(dict[str, object], response.json())


def test_admin_completes_authorized_source_upload_without_persisting_signed_url(
    client: TestClient,
    db_path: Path,
    storage: FakeStorageAdapter,
) -> None:
    identity = create_identity(client)
    identity_id = str(identity["id"])
    assert identity["authorization_status"] == "PENDING"
    assert identity["status"] == "DRAFT"

    authorized = upload_authorization(client, storage, identity_id)
    assert authorized["authorization_status"] == "AUTHORIZED"
    assert authorized["status"] == "DRAFT"

    completed = upload_source(client, storage, identity_id)
    active = cast(dict[str, object], completed["identity"])
    quality = cast(dict[str, object], completed["quality"])
    assert active["status"] == "ACTIVE"
    assert active["source_quality_status"] == "PASSED"
    assert quality["width"] == 1024
    assert quality["height"] == 1536
    assert quality["issue_codes"] == []

    with connect_database(db_path) as conn:
        assets = conn.execute(
            """
            SELECT id, project_id, storage_uri, sha256, content_type, metadata_json
            FROM assets
            WHERE id IN (?, ?)
            ORDER BY kind
            """,
            (active["authorization_asset_id"], active["source_asset_id"]),
        ).fetchall()
        audit_payloads = [
            str(row[0])
            for row in conn.execute(
                """
                SELECT metadata_json FROM audit_logs
                WHERE entity_id IN (?, ?)
                """,
                (active["authorization_asset_id"], active["source_asset_id"]),
            ).fetchall()
        ]

    assert len(assets) == 2
    assert {row["project_id"] for row in assets} == {None}
    assert all(str(row["sha256"]) for row in assets)
    assert all("?" not in str(row["storage_uri"]) for row in assets)
    assert any(
        f"users/employee_1/identities/{identity_id}/authorization/" in str(row["storage_uri"])
        for row in assets
    )
    assert any(
        f"users/employee_1/identities/{identity_id}/source/" in str(row["storage_uri"])
        for row in assets
    )
    assert all("http" not in payload and "sig=" not in payload for payload in audit_payloads)


def test_source_quality_failure_is_actionable_and_does_not_activate_identity(
    client: TestClient,
    db_path: Path,
    storage: FakeStorageAdapter,
    inspector: FakeSourceImageInspector,
) -> None:
    inspector.result = SourceImageInspection(
        person_count=2,
        face_count=2,
        face_visible=False,
        sharpness_score=0.2,
        occlusion_detected=True,
        watermark_detected=True,
        notes=["请使用单人、无遮挡、无水印的清晰照片"],
        provider="fake-source-inspector",
        model="fake-source-v1",
    )
    identity = create_identity(client)
    identity_id = str(identity["id"])
    upload_authorization(client, storage, identity_id)

    image = png_header()
    intent = client.post(
        f"/api/person-identities/{identity_id}/source-upload-intent",
        headers=headers("admin_1"),
        json={
            "filename": "source.png",
            "content_type": "image/png",
            "size_bytes": len(image),
        },
    ).json()
    storage.put_object(str(intent["storage_key"]), image, content_type="image/png")
    response = client.post(
        f"/api/person-identities/{identity_id}/source-upload-complete",
        headers=headers("admin_1"),
        json={"asset_id": intent["asset_id"]},
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "SOURCE_IMAGE_QUALITY_FAILED"
    assert set(response.json()["detail"]["issue_codes"]) >= {
        "MULTIPLE_PEOPLE",
        "FACE_NOT_VISIBLE",
        "IMAGE_NOT_SHARP",
        "FACE_OCCLUDED",
        "WATERMARK_DETECTED",
    }
    identity_response = client.get(
        f"/api/person-identities/{identity_id}",
        headers=headers("admin_1"),
    )
    assert identity_response.json()["status"] == "DRAFT"
    assert identity_response.json()["source_quality_status"] == "FAILED"
    with connect_database(db_path) as conn:
        metadata = conn.execute(
            "SELECT metadata_json FROM assets WHERE id = ?",
            (intent["asset_id"],),
        ).fetchone()[0]
    assert "WATERMARK_DETECTED" in json.loads(str(metadata))["quality"]["issue_codes"]


def test_source_completion_does_not_reactivate_identity_archived_during_inspection(
    client: TestClient,
    db_path: Path,
    storage: FakeStorageAdapter,
    inspector: FakeSourceImageInspector,
) -> None:
    identity = activate_identity(client, storage)
    identity_id = str(identity["id"])
    image = png_header()
    intent = client.post(
        f"/api/person-identities/{identity_id}/source-upload-intent",
        headers=headers("admin_1"),
        json={
            "filename": "replacement.png",
            "content_type": "image/png",
            "size_bytes": len(image),
        },
    ).json()
    storage.put_object(str(intent["storage_key"]), image, content_type="image/png")

    class ArchivingInspector:
        def inspect(self, content: bytes, *, content_type: str) -> SourceImageInspection:
            with connect_database(db_path) as conn:
                conn.execute(
                    "UPDATE person_identities SET status = 'ARCHIVED' WHERE id = ?",
                    (identity_id,),
                )
                conn.commit()
            return inspector.result

    app.dependency_overrides[get_source_image_inspector] = lambda: ArchivingInspector()
    response = client.post(
        f"/api/person-identities/{identity_id}/source-upload-complete",
        headers=headers("admin_1"),
        json={"asset_id": intent["asset_id"]},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "IDENTITY_AUTHORIZATION_REQUIRED"
    with connect_database(db_path) as conn:
        archived = conn.execute(
            "SELECT status, source_asset_id FROM person_identities WHERE id = ?",
            (identity_id,),
        ).fetchone()
    assert archived["status"] == "ARCHIVED"
    assert archived["source_asset_id"] == identity["source_asset_id"]


def test_persona_versions_freeze_snapshots_and_history_is_not_rewritten(
    client: TestClient,
    storage: FakeStorageAdapter,
) -> None:
    identity = activate_identity(client, storage)
    persona = create_persona(client, str(identity["id"]))
    version_one = create_version(client, str(persona["id"]), model="fake-character-v1")
    second_persona = create_persona(
        client,
        str(identity["id"]),
        name="乡墅品牌主理人",
    )
    second_persona_version = create_version(client, str(second_persona["id"]))

    update = client.patch(
        f"/api/character-personas/{persona['id']}",
        headers=headers("admin_1"),
        json={
            "name": "企业 AI 落地顾问",
            "costume_description": "深色商务休闲服",
        },
    )
    assert update.status_code == 200
    version_two = create_version(client, str(persona["id"]), model="fake-character-v2")

    restored_one = client.get(
        f"/api/character-versions/{version_one['id']}",
        headers=headers("admin_1"),
    ).json()
    versions = client.get(
        f"/api/character-personas/{persona['id']}/versions",
        headers=headers("admin_1"),
    ).json()

    assert [version["version_number"] for version in versions] == [1, 2]
    assert version_one["source_sha256"] == version_two["source_sha256"]
    assert restored_one["persona_snapshot_json"]["name"] == "乡墅项目管理专家"
    assert restored_one["persona_snapshot_json"]["costume_description"] == "工程马甲和安全帽"
    assert version_two["persona_snapshot_json"]["name"] == "企业 AI 落地顾问"
    assert version_two["template_version"]
    assert len(str(version_two["template_hash"])) == 64
    assert version_two["required_view_types_json"] == list(REQUIRED_CHARACTER_VIEW_TYPES)
    assert second_persona["identity_id"] == identity["id"]
    assert second_persona_version["persona_id"] == second_persona["id"]

    archive = client.post(
        f"/api/character-versions/{version_one['id']}/archive",
        headers=headers("admin_1"),
    )
    assert archive.status_code == 200
    assert archive.json()["status"] == "ARCHIVED"
    assert (
        client.delete(
            f"/api/character-personas/{persona['id']}",
            headers=headers("admin_1"),
        ).status_code
        == 409
    )


def test_employee_reads_only_current_authorized_published_versions(
    client: TestClient,
    db_path: Path,
    storage: FakeStorageAdapter,
) -> None:
    identity = activate_identity(client, storage)
    persona = create_persona(client, str(identity["id"]))
    draft = create_version(client, str(persona["id"]))

    assert (
        client.get(
            f"/api/character-versions/{draft['id']}",
            headers=headers("employee_1"),
        ).status_code
        == 404
    )
    with connect_database(db_path) as conn:
        conn.execute(
            """
            UPDATE character_versions
            SET status = 'PUBLISHED', published_by = 'admin_1', published_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (draft["id"],),
        )
        conn.commit()

    employee_versions = client.get(
        f"/api/character-personas/{persona['id']}/versions",
        headers=headers("employee_1"),
    )
    assert employee_versions.status_code == 200
    assert [item["id"] for item in employee_versions.json()] == [draft["id"]]
    assert employee_versions.json()[0]["source_asset_id"] is None
    assert employee_versions.json()[0]["source_sha256"] is None

    revoke = client.patch(
        f"/api/person-identities/{identity['id']}",
        headers=headers("admin_1"),
        json={"authorization_status": "REVOKED"},
    )
    assert revoke.status_code == 200
    assert revoke.json()["status"] == "REVOKED"
    blocked_version = client.post(
        f"/api/character-personas/{persona['id']}/versions",
        headers=headers("admin_1"),
        json={
            "provider": "fake",
            "model": "fake-character-v2",
            "generation_params_json": {},
        },
    )
    assert blocked_version.status_code == 409
    assert blocked_version.json()["detail"]["code"] == "IDENTITY_NOT_ACTIVE"
    assert (
        client.get(
            f"/api/character-personas/{persona['id']}/versions",
            headers=headers("employee_1"),
        ).json()
        == []
    )
    auditor = client.get(
        f"/api/character-versions/{draft['id']}",
        headers=headers("auditor_1"),
    )
    assert auditor.status_code == 200
    assert auditor.json()["source_asset_id"] == draft["source_asset_id"]


def test_only_admin_can_manage_identity_and_local_identity_object_uploads(
    client: TestClient,
    db_path: Path,
    storage: FakeStorageAdapter,
) -> None:
    denied = client.post(
        "/api/person-identities",
        headers=headers("employee_1"),
        json={
            "display_name": "Nope",
            "authorization_scope": ["internal-short-video"],
        },
    )
    assert denied.status_code == 403
    assert denied.json()["detail"]["code"] == "ROLE_FORBIDDEN"

    identity = create_identity(client)
    identity_id = str(identity["id"])
    content = b"%PDF-1.7\nauthorization"
    intent = client.post(
        f"/api/person-identities/{identity_id}/authorization-upload-intent",
        headers=headers("admin_1"),
        json={
            "filename": "authorization.pdf",
            "content_type": "application/pdf",
            "size_bytes": len(content),
        },
    ).json()
    employee_put = client.put(
        f"/api/assets/local-objects/{intent['storage_key']}",
        headers={**headers("employee_1"), "Content-Type": "application/pdf"},
        content=content,
    )
    forged_put = client.put(
        "/api/assets/local-objects/users/employee_1/identities/forged/document.pdf",
        headers={**headers("admin_1"), "Content-Type": "application/pdf"},
        content=content,
    )
    wrong_type_put = client.put(
        f"/api/assets/local-objects/{intent['storage_key']}",
        headers={**headers("admin_1"), "Content-Type": "image/png"},
        content=content,
    )
    admin_put = client.put(
        f"/api/assets/local-objects/{intent['storage_key']}",
        headers={**headers("admin_1"), "Content-Type": "application/pdf"},
        content=content,
    )

    assert employee_put.status_code == 403
    assert forged_put.status_code == 400
    assert wrong_type_put.status_code == 415
    assert admin_put.status_code == 204
    assert storage.get_object(str(intent["storage_key"])) == content
    with connect_database(db_path) as conn:
        denied_audit = conn.execute(
            """
            SELECT metadata_json FROM audit_logs
            WHERE action = 'security.role_denied' AND actor_user_id = 'employee_1'
            ORDER BY created_at DESC LIMIT 1
            """
        ).fetchone()
    assert denied_audit is not None


def test_role_gate_runs_before_source_inspector_configuration(
    client: TestClient,
    storage: FakeStorageAdapter,
    inspector: FakeSourceImageInspector,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = activate_identity(client, storage)
    monkeypatch.delenv("VIDEO_REPLICA_SETTINGS_KEY", raising=False)
    app.dependency_overrides.pop(get_source_image_inspector)
    try:
        response = client.post(
            f"/api/person-identities/{identity['id']}/source-upload-complete",
            headers=headers("employee_1"),
            json={"asset_id": "not-an-asset"},
        )
    finally:
        app.dependency_overrides[get_source_image_inspector] = lambda: inspector

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "ROLE_FORBIDDEN"


def test_identity_assets_are_evidence_only_for_auditor_and_private_from_employee(
    client: TestClient,
    storage: FakeStorageAdapter,
) -> None:
    identity = activate_identity(client, storage)
    source_asset_id = str(identity["source_asset_id"])

    auditor_metadata = client.get(
        f"/api/assets/{source_asset_id}",
        headers=headers("auditor_1"),
    )
    auditor_download = client.post(
        f"/api/assets/{source_asset_id}/download-url",
        headers=headers("auditor_1"),
    )
    employee_metadata = client.get(
        f"/api/assets/{source_asset_id}",
        headers=headers("employee_1"),
    )

    assert auditor_metadata.status_code == 200
    assert auditor_metadata.json()["project_id"] is None
    assert auditor_download.status_code == 403
    assert employee_metadata.status_code == 403
    assert employee_metadata.json()["detail"]["code"] == "ASSET_FORBIDDEN"


def test_invalid_source_format_and_expired_authorization_fail_closed(
    client: TestClient,
    storage: FakeStorageAdapter,
) -> None:
    expired = create_identity(client, expires_at="2020-01-01T00:00:00Z")
    expired_id = str(expired["id"])
    authorized = upload_authorization(client, storage, expired_id)
    assert authorized["authorization_status"] == "EXPIRED"
    assert authorized["status"] == "EXPIRED"
    blocked = client.post(
        f"/api/person-identities/{expired_id}/source-upload-intent",
        headers=headers("admin_1"),
        json={
            "filename": "source.png",
            "content_type": "image/png",
            "size_bytes": len(png_header()),
        },
    )
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "IDENTITY_AUTHORIZATION_REQUIRED"

    identity = create_identity(client)
    identity_id = str(identity["id"])
    upload_authorization(client, storage, identity_id)
    invalid = client.post(
        f"/api/person-identities/{identity_id}/source-upload-intent",
        headers=headers("admin_1"),
        json={
            "filename": "source.gif",
            "content_type": "image/gif",
            "size_bytes": 100,
        },
    )
    assert invalid.status_code == 415
    assert invalid.json()["detail"]["code"] == "SOURCE_IMAGE_TYPE_UNSUPPORTED"


def test_character_asset_object_keys_separate_generated_and_approved_purposes() -> None:
    generated = generated_character_asset_key(
        owner_user_id="employee_1",
        persona_id="persona-1",
        version_id="version-1",
        view_type="FRONT_FACE",
        asset_id="asset-1",
    )
    approved = approved_character_asset_key(
        owner_user_id="employee_1",
        persona_id="persona-1",
        version_id="version-1",
        view_type="FRONT_FACE",
        asset_id="asset-1",
    )

    assert generated == (
        "users/employee_1/personas/persona-1/versions/version-1/generated/FRONT_FACE/asset-1.png"
    )
    assert approved == (
        "users/employee_1/personas/persona-1/versions/version-1/approved/FRONT_FACE/asset-1.png"
    )
    assert generated != approved


def test_unconfigured_source_inspector_fails_closed(
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("VIDEO_REPLICA_SETTINGS_KEY", raising=False)
    with connect_database(db_path) as conn:
        with pytest.raises(HTTPException) as error:
            get_source_image_inspector(conn)

    assert error.value.status_code == 503
    assert error.value.detail["code"] == "SOURCE_IMAGE_INSPECTOR_NOT_CONFIGURED"


def test_null_required_identity_and_persona_updates_return_validation_errors(
    client: TestClient,
    storage: FakeStorageAdapter,
) -> None:
    identity = activate_identity(client, storage)
    persona = create_persona(client, str(identity["id"]))

    identity_response = client.patch(
        f"/api/person-identities/{identity['id']}",
        headers=headers("admin_1"),
        json={"display_name": None},
    )
    scope_response = client.patch(
        f"/api/person-identities/{identity['id']}",
        headers=headers("admin_1"),
        json={"authorization_scope": None},
    )
    persona_response = client.patch(
        f"/api/character-personas/{persona['id']}",
        headers=headers("admin_1"),
        json={"name": None},
    )

    assert identity_response.status_code == 422
    assert identity_response.json()["detail"]["code"] == "IDENTITY_NAME_REQUIRED"
    assert scope_response.status_code == 422
    assert scope_response.json()["detail"]["code"] == "IDENTITY_AUTHORIZATION_SCOPE_REQUIRED"
    assert persona_response.status_code == 422
    assert persona_response.json()["detail"]["code"] == "CHARACTER_PERSONA_NAME_REQUIRED"
