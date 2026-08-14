from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest
from fastapi.testclient import TestClient

from app.auth import get_database
from app.db import connect_database, initialize_database
from app.main import app


@pytest.fixture()
def db_path(tmp_path: Path) -> Iterator[Path]:
    db_path = tmp_path / "characters.db"
    with initialize_database(db_path) as connection:
        seed_data(connection)
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


def seed_data(conn: sqlite3.Connection) -> None:
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
            content_type,
            created_by_user_id
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                "asset_ref_2",
                "project_owned",
                "image",
                "local://ref-2.png",
                "sha-ref-2",
                12,
                "image/png",
                "employee_1",
            ),
            (
                "asset_ref_1",
                "project_owned",
                "image",
                "local://ref-1.png",
                "sha-ref-1",
                12,
                "image/png",
                "employee_1",
            ),
        ],
    )
    conn.commit()


def headers(user_id: str) -> dict[str, str]:
    return {"X-Dev-User-Id": user_id}


def create_character(
    client: TestClient,
    *,
    name: str,
    is_active: bool = True,
    authorization_project_ids: list[str] | None = None,
    authorization_expires_at: str | None = None,
) -> dict[str, object]:
    response = client.post(
        "/api/characters",
        headers=headers("admin_1"),
        json={
            "name": name,
            "reference_asset_ids": ["asset_ref_2", "asset_ref_1"],
            "authorization_project_ids": authorization_project_ids or [],
            "authorization_expires_at": authorization_expires_at,
            "is_active": is_active,
        },
    )
    assert response.status_code == 201
    return cast(dict[str, object], response.json())


def test_characters_migration_creates_library_tables(db_path: Path) -> None:
    with connect_database(db_path) as conn:
        version = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            ).fetchall()
        }

    assert version == "006_active_storage_provider"
    assert {"characters", "project_main_characters"}.issubset(tables)


def test_admin_crud_preserves_reference_order_and_employee_reads_only_available(
    client: TestClient,
) -> None:
    available = create_character(
        client,
        name="Visible Character",
        authorization_project_ids=["project_owned"],
    )
    disabled = create_character(client, name="Disabled Character", is_active=False)
    expired = create_character(
        client,
        name="Expired Character",
        authorization_expires_at="2020-01-01T00:00:00Z",
    )
    out_of_scope = create_character(
        client,
        name="Other Project Character",
        authorization_project_ids=["project_other"],
    )

    employee_list = client.get(
        "/api/characters?project_id=project_owned",
        headers=headers("employee_1"),
    )
    admin_list = client.get("/api/characters", headers=headers("admin_1"))

    assert employee_list.status_code == 200
    assert [item["id"] for item in employee_list.json()] == [available["id"]]
    assert admin_list.status_code == 200
    assert {item["id"] for item in admin_list.json()} == {
        available["id"],
        disabled["id"],
        expired["id"],
        out_of_scope["id"],
    }
    assert available["reference_asset_ids"] == ["asset_ref_2", "asset_ref_1"]

    patch = client.patch(
        f"/api/characters/{available['id']}",
        headers=headers("admin_1"),
        json={"name": "Renamed Character", "reference_asset_ids": ["asset_ref_1"]},
    )
    assert patch.status_code == 200
    assert patch.json()["name"] == "Renamed Character"
    assert patch.json()["reference_asset_ids"] == ["asset_ref_1"]

    delete = client.delete(f"/api/characters/{disabled['id']}", headers=headers("admin_1"))
    assert delete.status_code == 204


def test_employee_cannot_choose_disabled_expired_or_out_of_scope_character(
    client: TestClient,
) -> None:
    disabled = create_character(client, name="Disabled Character", is_active=False)
    expired = create_character(
        client,
        name="Expired Character",
        authorization_expires_at="2020-01-01T00:00:00Z",
    )
    out_of_scope = create_character(
        client,
        name="Other Project Character",
        authorization_project_ids=["project_other"],
    )

    for character in (disabled, expired, out_of_scope):
        response = client.put(
            "/api/projects/project_owned/main-character",
            headers=headers("employee_1"),
            json={"character_id": character["id"]},
        )
        assert response.status_code == 422
        assert response.json()["detail"]["code"] == "CHARACTER_NOT_AVAILABLE"


def test_project_main_character_selection_records_immutable_version_snapshot(
    client: TestClient,
    db_path: Path,
) -> None:
    character = create_character(
        client,
        name="Original Hero",
        authorization_project_ids=["project_owned"],
    )

    selection = client.put(
        "/api/projects/project_owned/main-character",
        headers=headers("employee_1"),
        json={"character_id": character["id"]},
    )
    assert selection.status_code == 200
    assert selection.json()["project_id"] == "project_owned"
    assert selection.json()["character_snapshot"]["name"] == "Original Hero"
    assert selection.json()["version_number"] == 1

    client.patch(
        f"/api/characters/{character['id']}",
        headers=headers("admin_1"),
        json={"name": "Mutated Hero"},
    )

    with connect_database(db_path) as conn:
        row = conn.execute(
            """
            SELECT kind, version_number, payload_json
            FROM versions
            WHERE project_id = ?
            """,
            ("project_owned",),
        ).fetchone()

    payload = json.loads(str(row["payload_json"]))
    assert row["kind"] == "main_character"
    assert row["version_number"] == 1
    assert payload["character_snapshot"]["name"] == "Original Hero"
    assert payload["character_snapshot"]["reference_asset_ids"] == ["asset_ref_2", "asset_ref_1"]


def test_employee_cannot_manage_character_library(client: TestClient) -> None:
    response = client.post(
        "/api/characters",
        headers=headers("employee_1"),
        json={"name": "Nope", "reference_asset_ids": []},
    )

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "ROLE_FORBIDDEN"
