from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import NoReturn

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.auth import get_database
from app.db import connect_database, initialize_database
from app.main import app
from app.storage import LocalStorageAdapter, StorageBackendUnavailable


@pytest.fixture()
def db_path(tmp_path: Path) -> Iterator[Path]:
    db_path = tmp_path / "rbac.db"
    with initialize_database(db_path) as connection:
        seed_rbac_data(connection)
    yield db_path


@pytest.fixture()
def client(db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    def database_override() -> Iterator[sqlite3.Connection]:
        conn = connect_database(db_path)
        try:
            yield conn
        finally:
            conn.close()

    storage_root = tmp_path / "private-storage"
    monkeypatch.setenv("VIDEO_REPLICA_SETTINGS_KEY", Fernet.generate_key().decode("ascii"))
    monkeypatch.setenv("VIDEO_REPLICA_STORAGE_ROOT", str(storage_root))
    storage = LocalStorageAdapter(root=storage_root, bucket="private-bucket")
    storage.put_object("outputs/asset_owned.mp4", b"video", content_type="video/mp4")
    app.dependency_overrides[get_database] = database_override
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def seed_rbac_data(conn: sqlite3.Connection) -> None:
    conn.executemany(
        """
        INSERT INTO users (id, username, display_name, role)
        VALUES (?, ?, ?, ?)
        """,
        [
            ("employee_1", "employee_1", "Employee One", "employee"),
            ("employee_2", "employee_2", "Employee Two", "employee"),
            ("admin_1", "admin_1", "Admin One", "admin"),
            ("auditor_1", "auditor_1", "Auditor One", "auditor"),
            ("inactive_1", "inactive_1", "Inactive One", "employee"),
            ("owner_1", "owner_1", "Owner One", "owner"),
        ],
    )
    conn.execute("UPDATE users SET is_active = 0 WHERE id = ?", ("inactive_1",))
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
    conn.executemany(
        """
        INSERT INTO assets (
            id,
            project_id,
            kind,
            storage_uri,
            sha256,
            size_bytes,
            created_by_user_id
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                "asset_owned",
                "project_owned",
                "video",
                "local://private-bucket/outputs/asset_owned.mp4",
                "sha-owned",
                12,
                "employee_1",
            ),
            (
                "asset_other",
                "project_other",
                "video",
                "local://other.mp4",
                "sha-other",
                12,
                "employee_2",
            ),
        ],
    )
    conn.execute(
        """
        INSERT INTO generation_batches (
            id,
            project_id,
            created_by_user_id,
            idempotency_key,
            request_hash,
            request_snapshot_json
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        ("batch_owned", "project_owned", "employee_1", "key", "hash", "{}"),
    )
    conn.execute(
        """
        INSERT INTO generation_tasks (
            id,
            batch_id,
            generation_mode,
            provider,
            model,
            status
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        ("task_owned", "batch_owned", "I2V", "metaso", "MiniMax-H3", "FAILED"),
    )
    conn.commit()


def auth_headers(user_id: str) -> dict[str, str]:
    return {"X-Dev-User-Id": user_id}


def audit_actions(db_path: Path) -> list[str]:
    with connect_database(db_path) as conn:
        return [str(row["action"]) for row in conn.execute("SELECT action FROM audit_logs")]


def test_dev_header_login_returns_active_user_role(client: TestClient) -> None:
    response = client.get("/api/auth/me", headers=auth_headers("employee_1"))

    assert response.status_code == 200
    assert response.json() == {
        "id": "employee_1",
        "username": "employee_1",
        "display_name": "Employee One",
        "role": "employee",
    }


def test_desktop_identity_login_works_without_dev_header(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VIDEO_REPLICA_DESKTOP_USER_ID", "employee_1")
    monkeypatch.delenv("VIDEO_REPLICA_ALLOW_DEV_IDENTITY_HEADER", raising=False)

    response = client.get("/api/auth/me")

    assert response.status_code == 200
    assert response.json()["id"] == "employee_1"


def test_dev_header_is_disabled_by_default(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("VIDEO_REPLICA_DESKTOP_USER_ID", raising=False)
    monkeypatch.delenv("VIDEO_REPLICA_ALLOW_DEV_IDENTITY_HEADER", raising=False)

    response = client.get("/api/auth/me", headers=auth_headers("employee_1"))

    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "AUTH_DESKTOP_IDENTITY_REQUIRED"


def test_desktop_identity_ignores_spoofed_dev_header(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VIDEO_REPLICA_DESKTOP_USER_ID", "employee_1")
    monkeypatch.setenv("VIDEO_REPLICA_ALLOW_DEV_IDENTITY_HEADER", "1")

    response = client.get("/api/auth/me", headers=auth_headers("admin_1"))

    assert response.status_code == 200
    assert response.json()["id"] == "employee_1"
    assert response.json()["role"] == "employee"


@pytest.mark.parametrize(
    ("user_id", "expected_status", "expected_code"),
    [
        ("inactive_1", 401, "AUTH_INVALID"),
        ("missing_1", 401, "AUTH_INVALID"),
        ("owner_1", 403, "ROLE_INVALID"),
    ],
)
def test_desktop_identity_rejects_inactive_missing_or_invalid_role(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    user_id: str,
    expected_status: int,
    expected_code: str,
) -> None:
    monkeypatch.setenv("VIDEO_REPLICA_DESKTOP_USER_ID", user_id)

    response = client.get("/api/auth/me")

    assert response.status_code == expected_status
    assert response.json()["detail"]["code"] == expected_code


def test_auth_me_records_login_success(client: TestClient, db_path: Path) -> None:
    response = client.get("/api/auth/me", headers=auth_headers("employee_1"))

    assert response.status_code == 200
    with connect_database(db_path) as conn:
        row = conn.execute(
            """
            SELECT actor_user_id, action, entity_type, entity_id, metadata_json
            FROM audit_logs
            WHERE action = 'auth.login_success'
            """
        ).fetchone()
    assert row is not None
    assert dict(row) == {
        "actor_user_id": "employee_1",
        "action": "auth.login_success",
        "entity_type": "user",
        "entity_id": "employee_1",
        "metadata_json": "{}",
    }


def test_auth_me_records_login_failure_without_storing_identity_header(
    client: TestClient,
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("VIDEO_REPLICA_DESKTOP_USER_ID", raising=False)
    monkeypatch.delenv("VIDEO_REPLICA_ALLOW_DEV_IDENTITY_HEADER", raising=False)

    response = client.get("/api/auth/me", headers=auth_headers("employee_1"))

    assert response.status_code == 401
    with connect_database(db_path) as conn:
        row = conn.execute(
            """
            SELECT actor_user_id, action, entity_type, entity_id, metadata_json
            FROM audit_logs
            WHERE action = 'auth.login_failure'
            """
        ).fetchone()
    assert row is not None
    assert row["actor_user_id"] is None
    assert row["entity_type"] == "auth"
    assert row["entity_id"] == "current_user"
    assert row["metadata_json"] == (
        '{"code":"AUTH_DESKTOP_IDENTITY_REQUIRED","identity_source":"none"}'
    )
    assert "employee_1" not in row["metadata_json"]


def test_desktop_origin_can_preflight_project_deletion(client: TestClient) -> None:
    response = client.options(
        "/api/projects/project_owned",
        headers={
            "Origin": "http://127.0.0.1:5173",
            "Access-Control-Request-Method": "DELETE",
        },
    )

    assert response.status_code == 200
    assert "DELETE" in response.headers["access-control-allow-methods"]


def test_employee_can_create_and_list_only_their_projects(
    client: TestClient,
    db_path: Path,
) -> None:
    created = client.post(
        "/api/projects",
        headers=auth_headers("employee_1"),
        json={"name": "  新的复刻项目  "},
    )
    listed = client.get("/api/projects", headers=auth_headers("employee_1"))

    assert created.status_code == 201
    assert created.json()["name"] == "新的复刻项目"
    assert created.json()["owner_user_id"] == "employee_1"
    assert created.json()["status"] == "ACTIVE"
    assert [project["name"] for project in listed.json()] == [
        "新的复刻项目",
        "Owned Project",
    ]

    with connect_database(db_path) as conn:
        row = conn.execute(
            "SELECT metadata_json FROM audit_logs WHERE action = 'project.create'"
        ).fetchone()
    assert row is not None
    assert '"name": "\\u65b0\\u7684\\u590d\\u523b\\u9879\\u76ee"' in str(row["metadata_json"])


def test_owner_can_delete_an_unfinished_project_and_its_pending_upload(
    client: TestClient,
    db_path: Path,
    tmp_path: Path,
) -> None:
    storage = LocalStorageAdapter(root=tmp_path / "private-storage", bucket="private-bucket")
    storage_key = "projects/project_delete/uploads/asset-pending/reference.mp4"
    storage.put_object(storage_key, b"pending-video", content_type="video/mp4")
    with connect_database(db_path) as conn:
        conn.execute(
            "INSERT INTO projects (id, owner_user_id, name) VALUES (?, ?, ?)",
            ("project_delete", "employee_1", "Delete Me"),
        )
        conn.execute(
            """
            INSERT INTO assets (
                id, project_id, kind, storage_uri, sha256, size_bytes, content_type,
                created_by_user_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "asset_pending",
                "project_delete",
                "reference_video",
                f"local://private-bucket/{storage_key}",
                "",
                0,
                "video/mp4",
                "employee_1",
            ),
        )
        conn.commit()

    response = client.delete("/api/projects/project_delete", headers=auth_headers("employee_1"))

    assert response.status_code == 204
    assert storage.head_object(storage_key) is None
    with connect_database(db_path) as conn:
        project = conn.execute(
            "SELECT id FROM projects WHERE id = ?", ("project_delete",)
        ).fetchone()
    assert project is None
    assert "project.delete" in audit_actions(db_path)


def test_project_delete_removes_a_project_with_completed_work(
    client: TestClient,
    db_path: Path,
    tmp_path: Path,
) -> None:
    response = client.delete("/api/projects/project_owned", headers=auth_headers("employee_1"))

    assert response.status_code == 204
    storage = LocalStorageAdapter(root=tmp_path / "private-storage", bucket="private-bucket")
    assert storage.head_object("outputs/asset_owned.mp4") is None
    with connect_database(db_path) as conn:
        remaining = {
            table: conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE project_id = ?",  # noqa: S608
                ("project_owned",),
            ).fetchone()[0]
            for table in ("assets", "versions", "generation_batches")
        }
        remaining["projects"] = conn.execute(
            "SELECT COUNT(*) FROM projects WHERE id = ?", ("project_owned",)
        ).fetchone()[0]
        remaining_tasks = conn.execute(
            """
            SELECT COUNT(*) FROM generation_tasks
            WHERE batch_id IN (
                SELECT id FROM generation_batches WHERE project_id = ?
            )
            """,
            ("project_owned",),
        ).fetchone()[0]
        audit = conn.execute(
            "SELECT metadata_json FROM audit_logs WHERE action = 'project.delete'"
        ).fetchone()
    assert remaining == {"projects": 0, "assets": 0, "versions": 0, "generation_batches": 0}
    assert remaining_tasks == 0
    assert audit is not None
    assert json.loads(str(audit["metadata_json"])) == {
        "deleted_asset_count": 1,
        "deleted_versions_count": 0,
        "storage_cleanup_failed_count": 0,
    }


def test_project_delete_blocked_while_generation_tasks_are_active(
    client: TestClient,
    db_path: Path,
) -> None:
    with connect_database(db_path) as conn:
        conn.execute(
            "UPDATE generation_tasks SET status = 'RUNNING' WHERE id = ?",
            ("task_owned",),
        )
        conn.commit()

    response = client.delete("/api/projects/project_owned", headers=auth_headers("employee_1"))

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "PROJECT_DELETE_HAS_ACTIVE_TASKS"
    with connect_database(db_path) as conn:
        project = conn.execute(
            "SELECT id FROM projects WHERE id = ?", ("project_owned",)
        ).fetchone()
    assert project is not None


def test_project_delete_tolerates_storage_cleanup_failure(
    client: TestClient,
    db_path: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable(_conn: sqlite3.Connection, _storage_uri: str) -> NoReturn:
        raise StorageBackendUnavailable("cloud credentials removed")

    monkeypatch.setattr("app.rbac_routes.storage_for_asset", unavailable)

    response = client.delete("/api/projects/project_owned", headers=auth_headers("employee_1"))

    assert response.status_code == 204
    storage = LocalStorageAdapter(root=tmp_path / "private-storage", bucket="private-bucket")
    assert storage.head_object("outputs/asset_owned.mp4") is not None
    with connect_database(db_path) as conn:
        project = conn.execute(
            "SELECT id FROM projects WHERE id = ?", ("project_owned",)
        ).fetchone()
        audit = conn.execute(
            "SELECT metadata_json FROM audit_logs WHERE action = 'project.delete'"
        ).fetchone()
    assert project is None
    assert audit is not None
    assert json.loads(str(audit["metadata_json"]))["storage_cleanup_failed_count"] == 1


def test_project_owner_can_rename_their_project(
    client: TestClient,
    db_path: Path,
) -> None:
    response = client.patch(
        "/api/projects/project_owned/name",
        headers=auth_headers("employee_1"),
        json={"name": "乡墅爆款第一期"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == "project_owned"
    assert body["name"] == "乡墅爆款第一期"
    with connect_database(db_path) as conn:
        stored = conn.execute(
            "SELECT name FROM projects WHERE id = ?", ("project_owned",)
        ).fetchone()
        audit = conn.execute(
            "SELECT metadata_json FROM audit_logs WHERE action = 'project.rename'"
        ).fetchone()
    assert stored is not None
    assert str(stored["name"]) == "乡墅爆款第一期"
    assert audit is not None
    metadata = json.loads(str(audit["metadata_json"]))
    assert metadata == {"from_name": "Owned Project", "to_name": "乡墅爆款第一期"}


def test_admin_can_rename_any_project(client: TestClient) -> None:
    response = client.patch(
        "/api/projects/project_owned/name",
        headers=auth_headers("admin_1"),
        json={"name": "Admin Renamed"},
    )

    assert response.status_code == 200
    assert response.json()["name"] == "Admin Renamed"


def test_project_rename_rejects_blank_and_overlong_names(client: TestClient) -> None:
    blank = client.patch(
        "/api/projects/project_owned/name",
        headers=auth_headers("employee_1"),
        json={"name": "   "},
    )
    overlong = client.patch(
        "/api/projects/project_owned/name",
        headers=auth_headers("employee_1"),
        json={"name": "超" * 121},
    )

    assert blank.status_code == 422
    assert blank.json()["detail"]["code"] == "PROJECT_NAME_REQUIRED"
    assert overlong.status_code == 422


def test_project_rename_forbidden_for_other_owner_and_auditor(
    client: TestClient,
) -> None:
    other_owner = client.patch(
        "/api/projects/project_other/name",
        headers=auth_headers("employee_1"),
        json={"name": "不应改名"},
    )
    auditor = client.patch(
        "/api/projects/project_owned/name",
        headers=auth_headers("auditor_1"),
        json={"name": "不应改名"},
    )

    assert other_owner.status_code == 403
    assert other_owner.json()["detail"]["code"] == "PROJECT_FORBIDDEN"
    assert auditor.status_code == 403
    assert auditor.json()["detail"]["code"] == "ROLE_FORBIDDEN"


def test_auditor_cannot_create_projects_and_admin_can_list_all_projects(
    client: TestClient,
) -> None:
    forbidden = client.post(
        "/api/projects",
        headers=auth_headers("auditor_1"),
        json={"name": "不应创建"},
    )
    listed = client.get("/api/projects", headers=auth_headers("admin_1"))

    assert forbidden.status_code == 403
    assert forbidden.json()["detail"]["code"] == "ROLE_FORBIDDEN"
    assert [project["name"] for project in listed.json()] == [
        "Other Project",
        "Owned Project",
    ]


def test_auditor_can_read_all_project_asset_and_task_evidence(client: TestClient) -> None:
    headers = auth_headers("auditor_1")

    projects = client.get("/api/projects", headers=headers)
    project = client.get("/api/projects/project_owned", headers=headers)
    asset = client.get("/api/assets/asset_owned", headers=headers)
    batch = client.get("/api/generation-batches/batch_owned", headers=headers)

    assert projects.status_code == 200
    assert [item["name"] for item in projects.json()] == [
        "Other Project",
        "Owned Project",
    ]
    assert project.status_code == 200
    assert asset.status_code == 200
    assert batch.status_code == 200


def test_project_list_exposes_reference_video_state_for_upload_recovery(
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
                "reference_pending",
                "project_owned",
                "reference_video",
                "cos://private-bucket/projects/project_owned/reference.mp4",
                "",
                0,
                "video/mp4",
                "employee_1",
            ),
        )
        conn.commit()

    response = client.get("/api/projects", headers=auth_headers("employee_1"))
    detail = client.get("/api/projects/project_owned", headers=auth_headers("employee_1"))

    assert response.status_code == 200
    assert response.json()[0] == {
        "id": "project_owned",
        "owner_user_id": "employee_1",
        "name": "Owned Project",
        "status": "ACTIVE",
        "reference_asset_id": "reference_pending",
        "reference_upload_status": "UPLOAD_PENDING",
        "analysis_status": "NOT_READY",
    }
    assert detail.json()["reference_asset_id"] == "reference_pending"
    assert detail.json()["reference_upload_status"] == "UPLOAD_PENDING"
    assert detail.json()["analysis_status"] == "NOT_READY"


def test_project_list_recognizes_legacy_reference_video_uploads(
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
                "legacy_reference",
                "project_owned",
                "video",
                "cos://private-bucket/projects/project_owned/uploads/legacy/reference.mp4",
                "legacy-hash",
                1024,
                "video/mp4",
                "employee_1",
            ),
        )
        conn.commit()

    response = client.get("/api/projects/project_owned", headers=auth_headers("employee_1"))

    assert response.status_code == 200
    assert response.json()["reference_asset_id"] == "legacy_reference"
    assert response.json()["reference_upload_status"] == "READY"
    assert response.json()["analysis_status"] == "PENDING"


def test_project_list_marks_existing_analysis_as_ready(
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
                "reference_ready",
                "project_owned",
                "reference_video",
                "cos://private-bucket/projects/project_owned/reference.mp4",
                "ready-hash",
                1024,
                "video/mp4",
                "employee_1",
            ),
        )
        conn.execute(
            """
            INSERT INTO versions (
                id, project_id, asset_id, kind, version_number,
                payload_json, created_by_user_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "analysis_ready",
                "project_owned",
                "reference_ready",
                "analysis",
                1,
                "{}",
                "employee_1",
            ),
        )
        conn.commit()

    response = client.get("/api/projects/project_owned", headers=auth_headers("employee_1"))

    assert response.status_code == 200
    assert response.json()["analysis_status"] == "READY"

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
                "reference_newer",
                "project_owned",
                "reference_video",
                "cos://private-bucket/projects/project_owned/reference-newer.mp4",
                "newer-hash",
                2048,
                "video/mp4",
                "employee_1",
            ),
        )
        conn.commit()

    stale = client.get("/api/projects/project_owned", headers=auth_headers("employee_1"))

    assert stale.status_code == 200
    assert stale.json()["reference_asset_id"] == "reference_newer"
    assert stale.json()["analysis_status"] == "PENDING"


def test_auditor_cannot_generate_retry_or_download(client: TestClient) -> None:
    headers = auth_headers("auditor_1")

    generate = client.post("/api/projects/project_owned/generation-batches", headers=headers)
    retry = client.post("/api/generation-tasks/task_owned/retry", headers=headers)
    download = client.post("/api/assets/asset_owned/download-url", headers=headers)

    assert generate.status_code == 403
    assert retry.status_code == 403
    assert download.status_code == 403
    assert generate.json()["detail"]["code"] == "ROLE_FORBIDDEN"
    assert retry.json()["detail"]["code"] == "ROLE_FORBIDDEN"
    assert download.json()["detail"]["code"] == "ROLE_FORBIDDEN"


def test_user_without_project_permission_cannot_read_asset(client: TestClient) -> None:
    response = client.get("/api/assets/asset_other", headers=auth_headers("employee_1"))

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "PROJECT_FORBIDDEN"


def test_project_owner_can_read_asset(client: TestClient) -> None:
    response = client.get("/api/assets/asset_owned", headers=auth_headers("employee_1"))

    assert response.status_code == 200
    assert response.json()["id"] == "asset_owned"
    assert response.json()["project_id"] == "project_owned"


def test_project_owner_receives_short_lived_storage_download_url(client: TestClient) -> None:
    response = client.post(
        "/api/assets/asset_owned/download-url",
        headers=auth_headers("employee_1"),
    )

    assert response.status_code == 200
    assert response.json()["url"].startswith(
        "http://127.0.0.1:8000/api/assets/local-objects/outputs/asset_owned.mp4?"
    )
    assert "expires=" in response.json()["url"]
    assert "sig=" in response.json()["url"]


def test_non_character_asset_cannot_use_character_cache(client: TestClient) -> None:
    response = client.post(
        "/api/assets/asset_owned/cached-url",
        headers=auth_headers("employee_1"),
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "CHARACTER_CACHE_UNSUPPORTED"


def test_download_audit_does_not_store_temporary_url(client: TestClient, db_path: Path) -> None:
    response = client.post(
        "/api/assets/asset_owned/download-url",
        headers=auth_headers("employee_1"),
    )

    assert response.status_code == 200
    with connect_database(db_path) as conn:
        row = conn.execute(
            """
            SELECT metadata_json
            FROM audit_logs
            WHERE action = 'asset.download_url.create'
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """
        ).fetchone()

    assert row is not None
    assert "x-expires" not in str(row["metadata_json"])
    assert "local://" not in str(row["metadata_json"])


def test_download_rejects_cloud_asset_when_bucket_does_not_match_configuration(
    client: TestClient,
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with connect_database(db_path) as conn:
        conn.execute(
            "UPDATE assets SET storage_uri = ? WHERE id = ?",
            ("cos://other-bucket/outputs/asset_owned.mp4", "asset_owned"),
        )
        conn.commit()

    class ConfiguredStorage:
        def load_provider_config(self, provider: str) -> dict[str, str]:
            assert provider == "cos"
            return {"bucket": "private-bucket"}

    monkeypatch.setattr("app.rbac_routes.SettingsRepository", lambda _conn: ConfiguredStorage())
    response = client.post(
        "/api/assets/asset_owned/download-url",
        headers=auth_headers("employee_1"),
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "STORAGE_BUCKET_MISMATCH"


def test_audit_logs_are_readable_only_by_admin_and_auditor(client: TestClient) -> None:
    employee = client.get("/api/audit-logs", headers=auth_headers("employee_1"))
    auditor = client.get("/api/audit-logs", headers=auth_headers("auditor_1"))
    admin = client.get("/api/audit-logs", headers=auth_headers("admin_1"))

    assert employee.status_code == 403
    assert employee.json()["detail"]["code"] == "ROLE_FORBIDDEN"
    assert auditor.status_code == 200
    assert admin.status_code == 200
