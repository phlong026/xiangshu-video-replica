from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.auth import CurrentUser, get_database
from app.db import connect_database, initialize_database
from app.main import app
from app.media import (
    FFprobeVideoProbe,
    VideoMetadata,
    complete_upload,
)
from app.media import (
    create_upload_intent as create_media_upload_intent,
)
from app.media_routes import get_media_storage, get_video_probe
from app.storage import FakeStorageAdapter


@dataclass(frozen=True)
class FakeVideoProbe:
    duration_seconds: float

    def probe(self, content: bytes, *, filename: str) -> VideoMetadata:
        assert content
        assert filename
        return VideoMetadata(duration_seconds=self.duration_seconds)


@pytest.fixture()
def db_path(tmp_path: Path) -> Iterator[Path]:
    path = tmp_path / "media.db"
    with initialize_database(path) as conn:
        seed_data(conn)
    yield path


@pytest.fixture()
def storage() -> FakeStorageAdapter:
    return FakeStorageAdapter(provider="fake", bucket="private-bucket")


@pytest.fixture()
def client(db_path: Path, storage: FakeStorageAdapter) -> Iterator[TestClient]:
    def database_override() -> Iterator[sqlite3.Connection]:
        conn = connect_database(db_path)
        try:
            yield conn
        finally:
            conn.close()

    app.dependency_overrides[get_database] = database_override
    app.dependency_overrides[get_media_storage] = lambda: storage
    app.dependency_overrides[get_video_probe] = lambda: FakeVideoProbe(duration_seconds=8.0)
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def seed_data(conn: sqlite3.Connection) -> None:
    conn.executemany(
        """
        INSERT INTO users (id, username, display_name, role)
        VALUES (?, ?, ?, ?)
        """,
        [
            ("employee_1", "employee_1", "Employee One", "employee"),
            ("employee_2", "employee_2", "Employee Two", "employee"),
            ("auditor_1", "auditor_1", "Auditor One", "auditor"),
            ("admin_1", "admin_1", "Admin One", "admin"),
        ],
    )
    conn.executemany(
        """
        INSERT INTO projects (id, owner_user_id, name)
        VALUES (?, ?, ?)
        """,
        [
            ("project_owned", "employee_1", "Owned Project"),
            ("project_other", "employee_2", "Other Project"),
        ],
    )
    conn.commit()


def auth_headers(user_id: str) -> dict[str, str]:
    return {"X-Dev-User-Id": user_id}


def create_upload_intent(
    client: TestClient,
    *,
    project_id: str = "project_owned",
    filename: str = "reference.mp4",
    content_type: str = "video/mp4",
    size_bytes: int = 1024,
    user_id: str = "employee_1",
) -> dict[str, object]:
    response = client.post(
        "/api/assets/upload-intent",
        headers=auth_headers(user_id),
        json={
            "project_id": project_id,
            "filename": filename,
            "content_type": content_type,
            "size_bytes": size_bytes,
        },
    )
    assert response.status_code == 200
    return dict(response.json())


def test_owner_can_upload_complete_and_query_video_asset(
    client: TestClient,
    storage: FakeStorageAdapter,
) -> None:
    intent = create_upload_intent(client)
    storage.put_object(str(intent["storage_key"]), b"video-bytes", content_type="video/mp4")

    complete = client.post(
        f"/api/assets/{intent['asset_id']}/complete",
        headers=auth_headers("employee_1"),
    )
    asset = client.get(f"/api/assets/{intent['asset_id']}", headers=auth_headers("employee_1"))

    assert complete.status_code == 200
    assert complete.json()["status"] == "uploaded"
    assert complete.json()["metadata"]["duration_seconds"] == 8.0
    assert asset.status_code == 200
    assert asset.json()["id"] == intent["asset_id"]
    assert asset.json()["project_id"] == "project_owned"
    assert asset.json()["size_bytes"] == len(b"video-bytes")
    assert asset.json()["content_type"] == "video/mp4"


def test_upload_intent_requires_project_owner(client: TestClient) -> None:
    response = client.post(
        "/api/assets/upload-intent",
        headers=auth_headers("employee_1"),
        json={
            "project_id": "project_other",
            "filename": "other.mp4",
            "content_type": "video/mp4",
            "size_bytes": 1024,
        },
    )

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "PROJECT_FORBIDDEN"


def test_upload_intent_rejects_non_video_or_oversized_files(client: TestClient) -> None:
    wrong_type = client.post(
        "/api/assets/upload-intent",
        headers=auth_headers("employee_1"),
        json={
            "project_id": "project_owned",
            "filename": "reference.png",
            "content_type": "image/png",
            "size_bytes": 1024,
        },
    )
    too_large = client.post(
        "/api/assets/upload-intent",
        headers=auth_headers("employee_1"),
        json={
            "project_id": "project_owned",
            "filename": "reference.mp4",
            "content_type": "video/mp4",
            "size_bytes": 50 * 1024 * 1024 + 1,
        },
    )

    assert wrong_type.status_code == 415
    assert wrong_type.json()["detail"]["code"] == "UNSUPPORTED_MEDIA_TYPE"
    assert too_large.status_code == 413
    assert too_large.json()["detail"]["code"] == "MEDIA_TOO_LARGE"


def test_complete_missing_object_can_retry(
    client: TestClient,
    storage: FakeStorageAdapter,
) -> None:
    intent = create_upload_intent(client)

    first = client.post(
        f"/api/assets/{intent['asset_id']}/complete",
        headers=auth_headers("employee_1"),
    )
    storage.put_object(str(intent["storage_key"]), b"video-bytes", content_type="video/mp4")
    retry = client.post(
        f"/api/assets/{intent['asset_id']}/complete",
        headers=auth_headers("employee_1"),
    )

    assert first.status_code == 409
    assert first.json()["detail"]["code"] == "UPLOAD_OBJECT_MISSING"
    assert retry.status_code == 200
    assert retry.json()["status"] == "uploaded"


def test_complete_rejects_duration_outside_precheck_window(
    client: TestClient,
    storage: FakeStorageAdapter,
) -> None:
    app.dependency_overrides[get_video_probe] = lambda: FakeVideoProbe(duration_seconds=3.5)
    intent = create_upload_intent(client)
    storage.put_object(str(intent["storage_key"]), b"video-bytes", content_type="video/mp4")

    response = client.post(
        f"/api/assets/{intent['asset_id']}/complete",
        headers=auth_headers("employee_1"),
    )
    asset = client.get(f"/api/assets/{intent['asset_id']}", headers=auth_headers("employee_1"))

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "VIDEO_DURATION_OUT_OF_RANGE"
    assert asset.status_code == 200
    assert asset.json()["size_bytes"] == 0


def test_default_probe_failure_does_not_mark_upload_complete(
    db_path: Path,
    storage: FakeStorageAdapter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.media.shutil.which", lambda _: None)
    actor = CurrentUser(
        id="employee_1",
        username="employee_1",
        display_name="Employee One",
        role="employee",
    )
    with connect_database(db_path) as conn:
        intent = create_media_upload_intent(
            conn,
            actor=actor,
            storage=storage,
            project_id="project_owned",
            filename="reference.mp4",
            content_type="video/mp4",
            size_bytes=1024,
        )
        storage.put_object(intent.storage_key, b"video-bytes", content_type="video/mp4")

        with pytest.raises(HTTPException) as exc_info:
            complete_upload(
                conn,
                actor=actor,
                storage=storage,
                probe=FFprobeVideoProbe(),
                asset_id=intent.asset_id,
            )
        row = conn.execute(
            "SELECT size_bytes, sha256 FROM assets WHERE id = ?",
            (intent.asset_id,),
        ).fetchone()

    assert exc_info.value.status_code == 503
    assert isinstance(exc_info.value.detail, dict)
    assert exc_info.value.detail["code"] == "VIDEO_PROBE_UNAVAILABLE"
    assert row is not None
    assert int(row["size_bytes"]) == 0
    assert str(row["sha256"]) == ""
