from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.auth import get_database
from app.character_identity import REQUIRED_CHARACTER_VIEW_TYPES
from app.character_identity_routes import get_character_storage
from app.character_image_generation import deterministic_png
from app.db import connect_database, initialize_database
from app.main import app
from app.media_routes import get_media_storage
from app.storage import FakeStorageAdapter


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "simple-character-test.db"
    with initialize_database(path) as conn:
        conn.executemany(
            """
            INSERT INTO users (id, username, display_name, role)
            VALUES (?, ?, ?, ?)
            """,
            [
                ("admin_1", "admin_1", "Admin One", "admin"),
                ("employee_1", "employee_1", "Employee One", "employee"),
                ("employee_2", "employee_2", "Employee Two", "employee"),
                ("auditor_1", "auditor_1", "Auditor One", "auditor"),
            ],
        )
        conn.execute(
            """
            INSERT INTO projects (id, owner_user_id, name)
            VALUES ('project-owned', 'employee_1', 'Owned Project')
            """,
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
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def headers(user_id: str) -> dict[str, str]:
    return {"X-Dev-User-Id": user_id}


def upload_files() -> dict[str, object]:
    return {
        "file": ("character.png", deterministic_png(b"simple-character-source"), "image/png"),
    }


def generate(client: TestClient, *, user_id: str = "employee_1", project_id: str = "project-owned"):
    return client.post(
        f"/api/simple-characters/{project_id}/generate",
        headers=headers(user_id),
        files=upload_files(),
        data={"display_name": "荣哥", "persona_name": "乡墅项目管理专家"},
    )


def test_simple_character_generation_requires_auth(client: TestClient) -> None:
    response = client.post("/api/simple-characters/upload-intent")
    assert response.status_code == 401
    response = client.post(
        "/api/simple-characters/project-owned/generate",
        files=upload_files(),
        data={"display_name": "荣哥"},
    )
    assert response.status_code == 401


def test_upload_intent_returns_direct_upload_contract(client: TestClient) -> None:
    response = client.post("/api/simple-characters/upload-intent", headers=headers("employee_1"))
    assert response.status_code == 200
    payload = response.json()
    assert payload["method"].startswith("POST")
    assert "generate" in payload["generate_url"]
    assert payload["max_size_bytes"] == 10 * 1024 * 1024
    assert "image/png" in payload["allowed_content_types"]


def test_simple_character_rejects_missing_project(client: TestClient) -> None:
    response = generate(client, project_id="project-missing")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "PROJECT_NOT_FOUND"


def test_simple_character_rejects_unsupported_content_type(client: TestClient) -> None:
    response = client.post(
        "/api/simple-characters/project-owned/generate",
        headers=headers("employee_1"),
        files={"file": ("notes.txt", b"plain text", "text/plain")},
        data={"display_name": "荣哥"},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "SIMPLE_CHARACTER_IMAGE_TYPE_UNSUPPORTED"


def test_simple_character_rejects_empty_name(client: TestClient) -> None:
    response = client.post(
        "/api/simple-characters/project-owned/generate",
        headers=headers("employee_1"),
        files=upload_files(),
        data={"display_name": "   "},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "SIMPLE_CHARACTER_NAME_REQUIRED"


def test_auditor_cannot_generate(client: TestClient) -> None:
    response = generate(client, user_id="auditor_1")
    assert response.status_code == 403


def test_employee_cannot_generate_for_foreign_project(client: TestClient) -> None:
    response = generate(client, user_id="employee_2")
    assert response.status_code == 403


def test_generated_character_appears_in_available_versions(
    client: TestClient, db_path: Path
) -> None:
    response = generate(client)
    assert response.status_code == 201, response.text
    payload = response.json()
    version_id = payload["character_version_id"]
    assert payload["publication_hash"]

    # Version row uses the simple_upload generation mode and is published.
    with connect_database(db_path) as conn:
        row = conn.execute(
            """
            SELECT status, generation_mode, published_at, persona_snapshot_json
            FROM character_versions
            WHERE id = ?
            """,
            (version_id,),
        ).fetchone()
        assert row is not None
        assert row["status"] == "PUBLISHED"
        assert row["generation_mode"] == "simple_upload"
        assert row["published_at"] is not None

        # The frozen persona snapshot matches the traditional flow's contract.
        snapshot = json.loads(str(row["persona_snapshot_json"]))
        assert snapshot["id"] == payload["persona_id"]
        assert snapshot["identity_id"] == payload["identity_id"]
        assert snapshot["name"] == "乡墅项目管理专家"
        assert snapshot["usage_scope_json"] == ["internal-short-video"]
        assert set(snapshot) == {
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
        }

        # Identity is self-authorized and active.
        identity = conn.execute(
            """
            SELECT authorization_status, authorization_asset_id, source_asset_id,
                   source_quality_status, status
            FROM person_identities
            WHERE id = ?
            """,
            (payload["identity_id"],),
        ).fetchone()
        assert identity is not None
        assert identity["authorization_status"] == "AUTHORIZED"
        assert identity["authorization_asset_id"] == identity["source_asset_id"]
        assert identity["source_asset_id"] is not None
        assert identity["source_quality_status"] == "PASSED"
        assert identity["status"] == "ACTIVE"

        # Seven approved published selections exist.
        assets = conn.execute(
            """
            SELECT view_type, review_status, is_published_selection
            FROM character_assets
            WHERE character_version_id = ?
            """,
            (version_id,),
        ).fetchall()
        assert {row["view_type"] for row in assets} == set(REQUIRED_CHARACTER_VIEW_TYPES)
        assert all(row["review_status"] == "APPROVED" for row in assets)
        assert all(row["is_published_selection"] == 1 for row in assets)

    # Downstream: the version is selectable for the owning project.
    versions_response = client.get(
        "/api/projects/project-owned/character-versions/available",
        headers=headers("employee_1"),
    )
    assert versions_response.status_code == 200, versions_response.text
    options = versions_response.json()
    matching = [option for option in options if option["character_version_id"] == version_id]
    assert len(matching) == 1
    option = matching[0]
    assert len(option["assets"]) == len(REQUIRED_CHARACTER_VIEW_TYPES)
    assert {asset["view_type"] for asset in option["assets"]} == set(REQUIRED_CHARACTER_VIEW_TYPES)
