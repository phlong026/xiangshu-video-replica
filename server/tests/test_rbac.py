from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.auth import get_database
from app.db import connect_database, initialize_database
from app.main import app


@pytest.fixture()
def db_path(tmp_path: Path) -> Iterator[Path]:
    db_path = tmp_path / "rbac.db"
    with initialize_database(db_path) as connection:
        seed_rbac_data(connection)
    yield db_path


@pytest.fixture()
def client(db_path: Path) -> Iterator[TestClient]:
    def database_override() -> Iterator[sqlite3.Connection]:
        conn = connect_database(db_path)
        try:
            yield conn
        finally:
            conn.close()

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
                "local://owned.mp4",
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


def test_audit_logs_are_readable_only_by_admin_and_auditor(client: TestClient) -> None:
    employee = client.get("/api/audit-logs", headers=auth_headers("employee_1"))
    auditor = client.get("/api/audit-logs", headers=auth_headers("auditor_1"))
    admin = client.get("/api/audit-logs", headers=auth_headers("admin_1"))

    assert employee.status_code == 403
    assert employee.json()["detail"]["code"] == "ROLE_FORBIDDEN"
    assert auditor.status_code == 200
    assert admin.status_code == 200
