from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass, replace
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
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
from app.media_routes import get_generation_result_storage, get_media_storage, get_video_probe
from app.settings import SettingsRepository
from app.storage import FakeStorageAdapter, LocalStorageAdapter


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
    db_path: Path,
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
    with connect_database(db_path) as conn:
        project = conn.execute(
            "SELECT status FROM projects WHERE id = ?", ("project_owned",)
        ).fetchone()
        uploaded_asset = conn.execute(
            "SELECT kind FROM assets WHERE id = ?", (str(intent["asset_id"]),)
        ).fetchone()
    assert project is not None
    assert project["status"] == "REFERENCE_READY"
    assert uploaded_asset is not None
    assert uploaded_asset["kind"] == "reference_video"


def test_complete_upload_calculates_a_content_hash_when_storage_head_has_none(
    db_path: Path,
) -> None:
    class HeadWithoutHashStorage(FakeStorageAdapter):
        def head_object(self, key: str):  # type: ignore[no-untyped-def]
            stored = super().head_object(key)
            return None if stored is None else replace(stored, sha256="")

    actor = CurrentUser(
        id="employee_1",
        username="employee_1",
        display_name="Employee One",
        role="employee",
    )
    storage = HeadWithoutHashStorage(provider="fake", bucket="private-bucket")
    content = b"video-bytes"
    with connect_database(db_path) as conn:
        intent = create_media_upload_intent(
            conn,
            actor=actor,
            storage=storage,
            project_id="project_owned",
            filename="reference.mp4",
            content_type="video/mp4",
            size_bytes=len(content),
        )
        storage.put_object(intent.storage_key, content, content_type="video/mp4")

        completed = complete_upload(
            conn,
            actor=actor,
            storage=storage,
            probe=FakeVideoProbe(duration_seconds=8.0),
            asset_id=intent.asset_id,
        )

    assert completed.sha256 == hashlib.sha256(content).hexdigest()


def test_media_storage_prefers_cos_when_configured(
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """业务主存储（源视频/人物图片/首帧）：配置了 COS 即上云，不再依赖
    runtime 的存储开关——拆解与付费生成都需要 HTTPS URL。"""
    monkeypatch.setenv("VIDEO_REPLICA_SETTINGS_KEY", Fernet.generate_key().decode("ascii"))
    selected_storage = FakeStorageAdapter(provider="cos", bucket="private-bucket")
    monkeypatch.setattr("app.media_routes.create_storage_adapter", lambda _: selected_storage)

    with connect_database(db_path) as conn:
        repo = SettingsRepository(conn)
        repo.save_provider_config(
            "cos",
            {
                "access_key_id": "cos-id",
                "secret_access_key": "cos-secret",
                "bucket": "private-bucket",
                "region": "ap-shanghai",
            },
            actor_user_id="admin_1",
        )
        repo.save_runtime_settings(
            max_generation_count_per_batch=4,
            max_concurrent_h3_tasks=2,
            active_storage_provider="local",
            actor_user_id="admin_1",
        )

        assert get_media_storage(conn) is selected_storage


def test_media_storage_falls_back_to_local_without_cos(
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """未配置 COS 时主存储退回本地盘，桌面单机场景仍可用。"""
    monkeypatch.setenv("VIDEO_REPLICA_SETTINGS_KEY", Fernet.generate_key().decode("ascii"))
    root = tmp_path / "local-storage"
    monkeypatch.setenv("VIDEO_REPLICA_STORAGE_ROOT", str(root))

    with connect_database(db_path) as conn:
        repo = SettingsRepository(conn)
        repo.save_runtime_settings(
            max_generation_count_per_batch=4,
            max_concurrent_h3_tasks=2,
            active_storage_provider="cos",
            actor_user_id="admin_1",
        )

        storage = get_media_storage(conn)
        assert isinstance(storage, LocalStorageAdapter)
        assert storage.root == root.resolve()


def test_generation_result_storage_uses_cos_when_configured(
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """内部计费只有在成片进入已配置的 COS 后才允许结算。"""
    monkeypatch.setenv("VIDEO_REPLICA_SETTINGS_KEY", Fernet.generate_key().decode("ascii"))
    selected_storage = FakeStorageAdapter(provider="cos", bucket="private-bucket")
    monkeypatch.setattr("app.media_routes.create_storage_adapter", lambda _: selected_storage)

    with connect_database(db_path) as conn:
        repo = SettingsRepository(conn)
        repo.save_provider_config(
            "cos",
            {
                "access_key_id": "cos-id",
                "secret_access_key": "cos-secret",
                "bucket": "private-bucket",
                "region": "ap-shanghai",
            },
            actor_user_id="admin_1",
        )

        assert get_generation_result_storage(conn) is selected_storage


def test_get_media_storage_uses_local_adapter_when_provider_is_local(
    db_path: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("VIDEO_REPLICA_SETTINGS_KEY", Fernet.generate_key().decode("ascii"))
    root = tmp_path / "local-storage"
    monkeypatch.setenv("VIDEO_REPLICA_STORAGE_ROOT", str(root))

    with connect_database(db_path) as conn:
        repo = SettingsRepository(conn)
        repo.save_runtime_settings(
            max_generation_count_per_batch=4,
            max_concurrent_h3_tasks=2,
            active_storage_provider="local",
            actor_user_id="admin_1",
        )

        storage = get_media_storage(conn)
        assert isinstance(storage, LocalStorageAdapter)
        assert storage.root == root.resolve()


def test_get_media_storage_local_without_root_raises(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VIDEO_REPLICA_SETTINGS_KEY", Fernet.generate_key().decode("ascii"))
    monkeypatch.delenv("VIDEO_REPLICA_STORAGE_ROOT", raising=False)

    with connect_database(db_path) as conn:
        repo = SettingsRepository(conn)
        repo.save_runtime_settings(
            max_generation_count_per_batch=4,
            max_concurrent_h3_tasks=2,
            active_storage_provider="local",
            actor_user_id="admin_1",
        )

        with pytest.raises(HTTPException) as excinfo:
            get_media_storage(conn)
        assert excinfo.value.status_code == 503
        assert isinstance(excinfo.value.detail, dict)
        assert excinfo.value.detail["code"] == "STORAGE_SETTINGS_UNAVAILABLE"


def test_local_storage_intent_url_and_upload_endpoint(
    db_path: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("VIDEO_REPLICA_SETTINGS_KEY", Fernet.generate_key().decode("ascii"))
    root = tmp_path / "local-storage"
    monkeypatch.setenv("VIDEO_REPLICA_STORAGE_ROOT", str(root))
    storage = LocalStorageAdapter(root=root)

    def database_override() -> Iterator[sqlite3.Connection]:
        conn = connect_database(db_path)
        try:
            yield conn
        finally:
            conn.close()

    app.dependency_overrides[get_database] = database_override
    app.dependency_overrides[get_media_storage] = lambda: storage
    try:
        client = TestClient(app)

        intent_resp = client.post(
            "/api/assets/upload-intent",
            headers=auth_headers("employee_1"),
            json={
                "project_id": "project_owned",
                "filename": "clip.mp4",
                "content_type": "video/mp4",
                "size_bytes": 123,
            },
        )
        assert intent_resp.status_code == 200
        intent = intent_resp.json()
        assert intent["url"].startswith("http://127.0.0.1:8000/api/assets/local-objects/")

        put_resp = client.put(
            f"/api/assets/local-objects/{intent['storage_key']}",
            content=b"mp4-bytes",
            headers=auth_headers("employee_1"),
        )
        assert put_resp.status_code == 204
        assert storage.get_object(intent["storage_key"]) == b"mp4-bytes"
    finally:
        app.dependency_overrides.clear()


def test_local_upload_endpoint_rejects_non_local_storage(client: TestClient) -> None:
    response = client.put(
        "/api/assets/local-objects/projects/x/uploads/a/v.mp4",
        content=b"bytes",
        headers=auth_headers("employee_1"),
    )
    assert response.status_code == 404


def test_local_upload_endpoint_enforces_role_and_project_gates(
    db_path: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("VIDEO_REPLICA_SETTINGS_KEY", Fernet.generate_key().decode("ascii"))
    monkeypatch.setenv("VIDEO_REPLICA_STORAGE_ROOT", str(tmp_path / "local-storage"))
    monkeypatch.setattr("app.media_routes.MAX_UPLOAD_BYTES", 8)
    storage = LocalStorageAdapter(root=tmp_path / "local-storage")

    def database_override() -> Iterator[sqlite3.Connection]:
        conn = connect_database(db_path)
        try:
            yield conn
        finally:
            conn.close()

    app.dependency_overrides[get_database] = database_override
    app.dependency_overrides[get_media_storage] = lambda: storage
    try:
        client = TestClient(app)
        key = "projects/project_owned/uploads/asset-1/demo.mp4"

        auditor_resp = client.put(
            f"/api/assets/local-objects/{key}",
            content=b"x",
            headers=auth_headers("auditor_1"),
        )
        assert auditor_resp.status_code == 403

        denied_resp = client.put(
            f"/api/assets/local-objects/{key}",
            content=b"x",
            headers=auth_headers("employee_2"),
        )
        assert denied_resp.status_code == 403

        ok_resp = client.put(
            f"/api/assets/local-objects/{key}",
            content=b"mp4",
            headers=auth_headers("employee_1"),
        )
        assert ok_resp.status_code == 204
        assert storage.get_object(key) == b"mp4"

        big_resp = client.put(
            f"/api/assets/local-objects/{key}",
            content=b"x" * 9,
            headers=auth_headers("employee_1"),
        )
        assert big_resp.status_code == 413
    finally:
        app.dependency_overrides.clear()


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


def test_complete_rejects_a_non_reference_video_asset(
    client: TestClient,
    db_path: Path,
) -> None:
    with connect_database(db_path) as conn:
        conn.execute(
            """
            INSERT INTO assets (
                id, project_id, kind, storage_uri,
                sha256, size_bytes, content_type, created_by_user_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "generated_video",
                "project_owned",
                "video",
                "fake://private-bucket/outputs/generated.mp4",
                "hash",
                10,
                "video/mp4",
                "employee_1",
            ),
        )
        conn.commit()

    response = client.post(
        "/api/assets/generated_video/complete",
        headers=auth_headers("employee_1"),
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "ASSET_NOT_REFERENCE_VIDEO"
    with connect_database(db_path) as conn:
        project = conn.execute(
            "SELECT status FROM projects WHERE id = ?", ("project_owned",)
        ).fetchone()
    assert project is not None
    assert project["status"] == "ACTIVE"


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


def test_complete_accepts_small_container_duration_rounding_overrun(
    client: TestClient,
    storage: FakeStorageAdapter,
) -> None:
    app.dependency_overrides[get_video_probe] = lambda: FakeVideoProbe(duration_seconds=15.033333)
    intent = create_upload_intent(client)
    storage.put_object(str(intent["storage_key"]), b"video-bytes", content_type="video/mp4")

    response = client.post(
        f"/api/assets/{intent['asset_id']}/complete",
        headers=auth_headers("employee_1"),
    )

    assert response.status_code == 200
    assert response.json()["metadata"]["duration_seconds"] == 15.033333


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


def test_local_download_url_and_proxy(
    db_path: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    key = Fernet.generate_key().decode("ascii")
    monkeypatch.delenv("VIDEO_REPLICA_SETTINGS_KEY", raising=False)
    monkeypatch.delenv("VIDEO_REPLICA_DISABLE_LOCAL_KEYSTORE", raising=False)
    monkeypatch.setattr("app.settings.load_or_create_local_settings_key", lambda: key)
    monkeypatch.setenv("VIDEO_REPLICA_STORAGE_ROOT", str(tmp_path / "local-storage"))
    storage = LocalStorageAdapter(root=tmp_path / "local-storage")
    storage.put_object(
        "projects/project_owned/uploads/asset-1/demo.mp4",
        b"mp4-content",
        content_type="video/mp4",
    )

    with connect_database(db_path) as conn:
        conn.execute(
            """
            INSERT INTO assets (
                id,
                project_id,
                kind,
                storage_uri,
                sha256,
                size_bytes,
                content_type,
                created_by_user_id
            )
            VALUES (?, ?, 'video', ?, 'sha', 10, 'video/mp4', 'employee_1')
            """,
            (
                "asset-1",
                "project_owned",
                "local://local-private/projects/project_owned/uploads/asset-1/demo.mp4",
            ),
        )

    def database_override() -> Iterator[sqlite3.Connection]:
        conn = connect_database(db_path)
        try:
            yield conn
        finally:
            conn.close()

    app.dependency_overrides[get_database] = database_override
    app.dependency_overrides[get_media_storage] = lambda: storage
    try:
        client = TestClient(app)
        url_resp = client.post(
            "/api/assets/asset-1/download-url", headers=auth_headers("employee_1")
        )
        assert url_resp.status_code == 200
        url = url_resp.json()["url"]
        assert url.startswith("http://127.0.0.1:8000/api/assets/local-objects/")

        from urllib.parse import urlsplit

        parsed = urlsplit(url)
        path = f"{parsed.path}?{parsed.query}"
        get_resp = client.get(path)
        assert get_resp.status_code == 200
        assert get_resp.content == b"mp4-content"

        bad_resp = client.get(f"{path}x")
        assert bad_resp.status_code == 403
    finally:
        app.dependency_overrides.clear()
