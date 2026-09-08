from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.materials as materials
from app.auth import get_database
from app.db import connect_database, initialize_database
from app.db_portable import BusinessConnection
from app.main import app
from app.media_routes import get_media_storage
from app.storage import FakeStorageAdapter, LocalStorageAdapter


@pytest.fixture()
def db_path(tmp_path: Path) -> Iterator[Path]:
    path = tmp_path / "materials.db"
    with initialize_database(path) as conn:
        seed_materials(conn)
    yield path


@pytest.fixture()
def storage() -> FakeStorageAdapter:
    return FakeStorageAdapter(provider="fake", bucket="private-bucket")


@pytest.fixture()
def client(
    db_path: Path,
    storage: FakeStorageAdapter,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    monkeypatch.setenv("VIDEO_REPLICA_DB_PATH", str(db_path))

    def database_override() -> Iterator[BusinessConnection]:
        conn = BusinessConnection.sqlite(connect_database(db_path))
        try:
            yield conn
        finally:
            conn.close()

    app.dependency_overrides[get_database] = database_override
    app.dependency_overrides[get_media_storage] = lambda: storage
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def auth_headers(user_id: str = "employee_1") -> dict[str, str]:
    return {"X-Dev-User-Id": user_id}


def seed_materials(conn: sqlite3.Connection) -> None:
    conn.executemany(
        "INSERT INTO users (id, username, display_name, role) VALUES (?, ?, ?, ?)",
        [
            ("employee_1", "employee_1", "Employee One", "employee"),
            ("employee_2", "employee_2", "Employee Two", "employee"),
            ("auditor_1", "auditor_1", "Auditor One", "auditor"),
        ],
    )
    conn.executemany(
        "INSERT INTO projects (id, owner_user_id, name) VALUES (?, ?, ?)",
        [
            ("project-owned", "employee_1", "自有项目"),
            ("project-other", "employee_2", "他人项目"),
        ],
    )
    conn.executemany(
        """
        INSERT INTO assets (
            id, project_id, kind, storage_uri, sha256, size_bytes,
            content_type, metadata_json, created_by_user_id
        ) VALUES (?, ?, 'reference_video', ?, ?, 8, 'video/mp4', '{}', ?)
        """,
        [
            (
                "asset-owned",
                "project-owned",
                "fake://private-bucket/projects/project-owned/source.mp4",
                "a" * 64,
                "employee_1",
            ),
            (
                "asset-other",
                "project-other",
                "fake://private-bucket/projects/project-other/source.mp4",
                "b" * 64,
                "employee_2",
            ),
        ],
    )
    conn.execute(
        """
        INSERT INTO generation_batches (
            id, project_id, created_by_user_id, idempotency_key,
            request_hash, request_snapshot_json, status, display_name
        ) VALUES ('batch-direct', 'project-owned', 'employee_1', 'idem-direct',
                  'hash-direct', '{}', 'SUCCEEDED', '直出成片')
        """
    )
    conn.execute(
        """
        INSERT INTO generation_tasks (
            id, batch_id, provider, model, status, archive_status,
            provider_result_url, completed_at
        ) VALUES ('task-direct', 'batch-direct', 'metaso', 'h3', 'SUCCEEDED',
                  'DIRECT', 'https://provider.example/result.mp4', CURRENT_TIMESTAMP)
        """
    )
    conn.commit()


def test_lists_owned_stored_and_direct_materials_with_server_pagination(
    client: TestClient,
) -> None:
    first = client.get(
        "/api/studio/materials?page=1&page_size=1",
        headers=auth_headers(),
    )
    assert first.status_code == 200
    assert first.json()["total"] == 2
    assert len(first.json()["items"]) == 1

    second = client.get(
        "/api/studio/materials?page=2&page_size=1",
        headers=auth_headers(),
    )
    ids = {first.json()["items"][0]["id"], second.json()["items"][0]["id"]}
    assert ids == {"asset:asset-owned", "generation:task-direct"}
    assert all(item["owner_user_id"] == "employee_1" for item in first.json()["items"])


def test_direct_result_is_visible_but_not_presented_as_cloud_asset(client: TestClient) -> None:
    response = client.get(
        "/api/studio/materials?source=generation",
        headers=auth_headers(),
    )
    assert response.status_code == 200
    material = response.json()["items"][0]
    assert material == {
        **material,
        "id": "generation:task-direct",
        "asset_id": None,
        "generation_task_id": "task-direct",
        "delivery": "direct",
        "status": "ready",
        "saved": False,
    }
    assert material["allowed_uses"] == []
    assert "preview" in material["allowed_actions"]
    assert "rename" not in material["allowed_actions"]


def test_upload_audio_to_storage_then_complete_and_list_it(
    client: TestClient,
    storage: FakeStorageAdapter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(materials, "probe_audio_duration", lambda _content: 42.0)
    intent = client.post(
        "/api/studio/materials/upload-intent",
        headers=auth_headers(),
        json={
            "filename": "讲解.mp3",
            "content_type": "audio/mpeg",
            "size_bytes": 11,
            "title": "完整口播音频",
            "group": "口播素材",
            "audio_purpose": "oral_audio",
            "duration_seconds": 42,
        },
    )
    assert intent.status_code == 200
    body = intent.json()
    assert body["material_id"] == f"asset:{body['asset_id']}"
    assert body["storage_key"].startswith("materials/employee_1/")

    storage.put_object(body["storage_key"], b"ID3abcdefgh", content_type="audio/mpeg")
    completed = client.post(
        f"/api/studio/materials/uploads/{body['asset_id']}/complete",
        headers=auth_headers(),
    )
    assert completed.status_code == 200, completed.text
    assert completed.json()["status"] == "ready"
    assert completed.json()["size_bytes"] == 11

    listed = client.get(
        "/api/studio/materials?media_type=audio",
        headers=auth_headers(),
    )
    assert listed.status_code == 200
    material = listed.json()["items"][0]
    assert material["title"] == "完整口播音频"
    assert material["allowed_uses"] == ["oral_audio"]
    assert material["duration_seconds"] == 42
    assert material["delivery"] == "stored"
    assert material["saved"] is True


@pytest.mark.parametrize(
    ("purpose", "duration", "expected_code"),
    [
        (None, None, "MATERIAL_AUDIO_PURPOSE_REQUIRED"),
        ("voice_clone", 4.9, "MATERIAL_AUDIO_DURATION_INVALID"),
        ("voice_clone", 180.1, "MATERIAL_AUDIO_DURATION_INVALID"),
        ("oral_audio", None, "MATERIAL_AUDIO_DURATION_REQUIRED"),
    ],
)
def test_audio_upload_intent_enforces_purpose_duration_contract(
    client: TestClient,
    purpose: str,
    duration: float | None,
    expected_code: str,
) -> None:
    response = client.post(
        "/api/studio/materials/upload-intent",
        headers=auth_headers(),
        json={
            "filename": "voice.mp3",
            "content_type": "audio/mpeg",
            "size_bytes": 10,
            "audio_purpose": purpose,
            "duration_seconds": duration,
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == expected_code


def test_audio_complete_rejects_duration_that_differs_from_browser_claim(
    client: TestClient,
    storage: FakeStorageAdapter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(materials, "probe_audio_duration", lambda _content: 181.0)
    intent = client.post(
        "/api/studio/materials/upload-intent",
        headers=auth_headers(),
        json={
            "filename": "voice.mp3",
            "content_type": "audio/mpeg",
            "size_bytes": 3,
            "audio_purpose": "voice_clone",
            "duration_seconds": 30,
        },
    ).json()
    storage.put_object(intent["storage_key"], b"ID3", content_type="audio/mpeg")

    response = client.post(
        f"/api/studio/materials/uploads/{intent['asset_id']}/complete",
        headers=auth_headers(),
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "MATERIAL_AUDIO_DURATION_MISMATCH"


def test_historical_audio_without_verified_purpose_is_read_only(
    client: TestClient,
    db_path: Path,
) -> None:
    with connect_database(db_path) as conn:
        conn.execute(
            """
            INSERT INTO assets (
                id, project_id, kind, storage_uri, sha256, size_bytes,
                content_type, metadata_json, created_by_user_id
            ) VALUES ('legacy-audio', NULL, 'material_audio', 'local://materials/legacy.mp3',
                      'legacy-hash', 9, 'audio/mpeg',
                      '{"audio_purpose":"oral_audio","duration_seconds":42}',
                      'employee_1')
            """
        )

    response = client.get(
        "/api/studio/materials?media_type=audio",
        headers=auth_headers(),
    )

    assert response.status_code == 200
    legacy = next(item for item in response.json()["items"] if item["asset_id"] == "legacy-audio")
    assert legacy["status"] == "ready"
    assert legacy["allowed_uses"] == []


def test_non_audio_upload_rejects_audio_purpose(client: TestClient) -> None:
    response = client.post(
        "/api/studio/materials/upload-intent",
        headers=auth_headers(),
        json={
            "filename": "photo.png",
            "content_type": "image/png",
            "size_bytes": 10,
            "audio_purpose": "oral_audio",
            "duration_seconds": 12,
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "MATERIAL_AUDIO_PURPOSE_INVALID"


@pytest.mark.parametrize(
    ("filename", "content_type"),
    [
        ("notes.txt", "text/plain"),
        ("fake.mp3", "video/mp4"),
        ("fake.png", "audio/mpeg"),
    ],
)
def test_upload_intent_rejects_unsupported_or_mismatched_media(
    client: TestClient,
    filename: str,
    content_type: str,
) -> None:
    response = client.post(
        "/api/studio/materials/upload-intent",
        headers=auth_headers(),
        json={
            "filename": filename,
            "content_type": content_type,
            "size_bytes": 10,
        },
    )
    assert response.status_code == 415
    assert response.json()["detail"]["code"] == "MATERIAL_TYPE_UNSUPPORTED"


def test_rename_hide_and_resolve_hidden_material(
    client: TestClient,
    db_path: Path,
) -> None:
    renamed = client.patch(
        "/api/studio/materials/asset:asset-owned",
        headers=auth_headers(),
        json={"title": "新的素材名称", "group": "重点参考"},
    )
    assert renamed.status_code == 200
    assert renamed.json()["title"] == "新的素材名称"
    assert renamed.json()["group"] == "重点参考"
    with connect_database(db_path) as conn:
        action = conn.execute(
            "SELECT action FROM audit_logs WHERE action = 'studio.material.update'"
        ).fetchone()
    assert action is not None

    hidden = client.delete(
        "/api/studio/materials/asset:asset-owned",
        headers=auth_headers(),
    )
    assert hidden.status_code == 204
    listed = client.get("/api/studio/materials", headers=auth_headers())
    assert "asset:asset-owned" not in {item["id"] for item in listed.json()["items"]}

    resolved = client.post(
        "/api/studio/materials/resolve",
        headers=auth_headers(),
        json={"material_ids": ["asset:asset-owned", "asset:asset-other", "asset:missing"]},
    )
    assert resolved.status_code == 200
    assert resolved.json()["items"][0]["id"] == "asset:asset-owned"
    assert resolved.json()["items"][0]["hidden"] is True
    assert resolved.json()["unavailable_ids"] == ["asset:asset-other", "asset:missing"]


@pytest.mark.parametrize("method", ["patch", "delete"])
def test_auditor_cannot_change_material_library(
    client: TestClient,
    method: str,
) -> None:
    response = getattr(client, method)(
        "/api/studio/materials/asset:asset-owned",
        headers=auth_headers("auditor_1"),
        **({"json": {"title": "审计员不得修改"}} if method == "patch" else {}),
    )
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "ROLE_FORBIDDEN"


def test_complete_rejects_bytes_that_do_not_match_declared_media(
    client: TestClient,
    storage: FakeStorageAdapter,
) -> None:
    intent = client.post(
        "/api/studio/materials/upload-intent",
        headers=auth_headers(),
        json={
            "filename": "voice.mp3",
            "content_type": "audio/mpeg",
            "size_bytes": 12,
            "audio_purpose": "voice_clone",
            "duration_seconds": 30,
        },
    ).json()
    storage.put_object(intent["storage_key"], b"not an audio", content_type="audio/mpeg")

    response = client.post(
        f"/api/studio/materials/uploads/{intent['asset_id']}/complete",
        headers=auth_headers(),
    )
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "MATERIAL_CONTENT_INVALID"


def test_local_material_upload_uses_authenticated_api_endpoint(
    client: TestClient,
    tmp_path: Path,
) -> None:
    local = LocalStorageAdapter(root=tmp_path / "objects", bucket="local-private")
    app.dependency_overrides[get_media_storage] = lambda: local
    content = b"\x89PNG\r\n\x1a\nbody"
    intent = client.post(
        "/api/studio/materials/upload-intent",
        headers=auth_headers(),
        json={
            "filename": "庭院.png",
            "content_type": "image/png",
            "size_bytes": len(content),
        },
    )
    assert intent.status_code == 200
    body = intent.json()
    assert body["url"].endswith(f"/api/studio/materials/uploads/{body['asset_id']}/content")

    uploaded = client.put(
        f"/api/studio/materials/uploads/{body['asset_id']}/content",
        headers={**auth_headers(), "Content-Type": "image/png"},
        content=content,
    )
    assert uploaded.status_code == 204
    completed = client.post(
        f"/api/studio/materials/uploads/{body['asset_id']}/complete",
        headers=auth_headers(),
    )
    assert completed.status_code == 200
    assert completed.json()["saved"] is True


def test_other_user_cannot_complete_material_upload(
    client: TestClient,
    storage: FakeStorageAdapter,
) -> None:
    intent = client.post(
        "/api/studio/materials/upload-intent",
        headers=auth_headers("employee_1"),
        json={
            "filename": "voice.mp3",
            "content_type": "audio/mpeg",
            "size_bytes": 3,
            "audio_purpose": "voice_clone",
            "duration_seconds": 30,
        },
    ).json()
    storage.put_object(intent["storage_key"], b"ID3", content_type="audio/mpeg")

    response = client.post(
        f"/api/studio/materials/uploads/{intent['asset_id']}/complete",
        headers=auth_headers("employee_2"),
    )
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "ASSET_NOT_FOUND"
