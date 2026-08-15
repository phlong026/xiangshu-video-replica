from __future__ import annotations

import hashlib
import sqlite3
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import character_routes, project_character_selection
from app.auth import CurrentUser, get_database
from app.character_identity import REQUIRED_CHARACTER_VIEW_TYPES, encode_json
from app.db import connect_database, initialize_database
from app.main import app


@dataclass(frozen=True)
class SeededVersion:
    identity_id: str
    persona_id: str
    version_id: str


@pytest.fixture()
def db_path(tmp_path: Path) -> Iterator[Path]:
    path = tmp_path / "project-character-selection.db"
    with initialize_database(path) as conn:
        conn.executemany(
            "INSERT INTO users (id, username, display_name, role) VALUES (?, ?, ?, ?)",
            [
                ("employee_1", "employee_1", "Employee One", "employee"),
                ("employee_2", "employee_2", "Employee Two", "employee"),
                ("admin_1", "admin_1", "Admin One", "admin"),
                ("auditor_1", "auditor_1", "Auditor One", "auditor"),
            ],
        )
        conn.executemany(
            "INSERT INTO projects (id, owner_user_id, name) VALUES (?, ?, ?)",
            [
                ("project-owned", "employee_1", "Owned Project"),
                ("project-other", "employee_2", "Other Project"),
            ],
        )
        conn.commit()
    yield path


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


def headers(user_id: str) -> dict[str, str]:
    return {"X-Dev-User-Id": user_id}


def seed_version(
    db_path: Path,
    *,
    key: str,
    status: str = "PUBLISHED",
    identity_status: str = "ACTIVE",
    authorization_status: str = "AUTHORIZED",
    authorization_expires_at: str = "2035-01-01T00:00:00+00:00",
    source_quality_status: str = "PASSED",
    published_asset_count: int = 7,
) -> SeededVersion:
    identity_id = f"identity-{key}"
    persona_id = f"persona-{key}"
    version_id = f"character-version-{key}"
    persona_snapshot = {
        "name": f"{key} 项目经理",
        "occupation": "乡墅项目经理",
        "costume_description": "深色工装",
    }
    persona_snapshot_json = encode_json(persona_snapshot)
    template_hash = hashlib.sha256(f"template-{key}".encode()).hexdigest()
    assets_by_view: dict[str, object] = {}
    asset_rows: list[tuple[object, ...]] = []
    character_asset_rows: list[tuple[object, ...]] = []
    for index, view_type in enumerate(REQUIRED_CHARACTER_VIEW_TYPES):
        if index >= published_asset_count:
            break
        asset_id = f"asset-{key}-{view_type.lower()}"
        character_asset_id = f"character-asset-{key}-{view_type.lower()}"
        sha256 = hashlib.sha256(asset_id.encode()).hexdigest()
        storage_uri = f"local://characters/{key}/{view_type.lower()}.png"
        asset_rows.append(
            (
                asset_id,
                "character_approved_image",
                storage_uri,
                sha256,
                128,
                "image/png",
                "admin_1",
            )
        )
        character_asset_rows.append(
            (
                character_asset_id,
                version_id,
                asset_id,
                view_type,
            )
        )
        assets_by_view[view_type] = {
            "approved_asset_id": asset_id,
            "character_asset_id": character_asset_id,
            "content_type": "image/png",
            "generated_asset_id": f"generated-{key}-{view_type.lower()}",
            "review_id": f"review-{key}-{view_type.lower()}",
            "sha256": sha256,
            "size_bytes": 128,
            "storage_uri": storage_uri,
        }
    publication_snapshot = {
        "assets_by_view": assets_by_view,
        "character_version_id": version_id,
        "persona_snapshot_hash": hashlib.sha256(persona_snapshot_json.encode()).hexdigest(),
        "published_at": "2030-01-01T00:00:00+00:00",
        "required_view_types": list(REQUIRED_CHARACTER_VIEW_TYPES),
        "schema_version": "character-publication.v1",
        "template_hash": template_hash,
        "template_version": "character-prompt-v1",
    }
    publication_snapshot_json = encode_json(publication_snapshot)
    publication_hash = hashlib.sha256(publication_snapshot_json.encode()).hexdigest()
    authorization_asset_id = f"authorization-{key}"
    source_asset_id = f"source-{key}"
    with connect_database(db_path) as conn:
        conn.executemany(
            """
            INSERT INTO assets (
                id, project_id, kind, storage_uri, sha256, size_bytes,
                content_type, created_by_user_id
            ) VALUES (?, NULL, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    authorization_asset_id,
                    "character_authorization",
                    f"local://characters/{key}/authorization.pdf",
                    hashlib.sha256(authorization_asset_id.encode()).hexdigest(),
                    128,
                    "application/pdf",
                    "admin_1",
                ),
                (
                    source_asset_id,
                    "character_source_image",
                    f"local://characters/{key}/source.png",
                    hashlib.sha256(source_asset_id.encode()).hexdigest(),
                    128,
                    "image/png",
                    "admin_1",
                ),
            ],
        )
        conn.execute(
            """
            INSERT INTO person_identities (
                id, owner_user_id, display_name, authorization_status,
                authorization_asset_id, authorization_scope,
                authorization_expires_at, source_asset_id,
                source_quality_status, status, created_by
            ) VALUES (?, 'employee_1', ?, ?, ?, '["internal-short-video"]',
                      ?, ?, ?, ?, 'admin_1')
            """,
            (
                identity_id,
                f"{key} 荣哥",
                authorization_status,
                authorization_asset_id,
                authorization_expires_at,
                source_asset_id,
                source_quality_status,
                identity_status,
            ),
        )
        conn.execute(
            """
            INSERT INTO character_personas (
                id, identity_id, name, occupation, costume_description,
                usage_scope_json, created_by
            ) VALUES (?, ?, ?, '乡墅项目经理', '深色工装',
                      '["internal-short-video"]', 'admin_1')
            """,
            (persona_id, identity_id, f"{key} 项目经理"),
        )
        conn.execute(
            """
            INSERT INTO character_versions (
                id, persona_id, version_number, status, source_asset_id,
                source_sha256, persona_snapshot_json, provider, model,
                generation_params_json, template_version, template_hash,
                required_view_types_json, published_by, published_at,
                publication_snapshot_json, publication_hash, created_by
            ) VALUES (?, ?, 3, ?, ?, ?, ?, 'fake_character',
                      'fake-character-v1', '{}', 'character-prompt-v1', ?, ?,
                      'admin_1', '2030-01-01T00:00:00+00:00', ?, ?, 'admin_1')
            """,
            (
                version_id,
                persona_id,
                status,
                source_asset_id,
                hashlib.sha256(source_asset_id.encode()).hexdigest(),
                persona_snapshot_json,
                template_hash,
                encode_json(list(REQUIRED_CHARACTER_VIEW_TYPES)),
                publication_snapshot_json,
                publication_hash,
            ),
        )
        conn.executemany(
            """
            INSERT INTO assets (
                id, project_id, kind, storage_uri, sha256, size_bytes,
                content_type, created_by_user_id
            ) VALUES (?, NULL, ?, ?, ?, ?, ?, ?)
            """,
            asset_rows,
        )
        conn.executemany(
            """
            INSERT INTO character_assets (
                id, character_version_id, asset_id, view_type,
                candidate_number, auto_quality_json, review_status,
                is_published_selection
            ) VALUES (?, ?, ?, ?, 1, '{}', 'APPROVED', 1)
            """,
            character_asset_rows,
        )
        conn.commit()
    return SeededVersion(identity_id, persona_id, version_id)


def test_project_lists_only_current_published_versions_with_seven_assets(
    client: TestClient,
    db_path: Path,
) -> None:
    available = seed_version(db_path, key="available")
    seed_version(db_path, key="draft", status="DRAFT")
    seed_version(
        db_path,
        key="expired",
        authorization_expires_at="2020-01-01T00:00:00+00:00",
    )
    seed_version(db_path, key="archived", identity_status="ARCHIVED")
    seed_version(db_path, key="revoked", authorization_status="REVOKED")
    seed_version(db_path, key="source-failed", source_quality_status="FAILED")
    missing_authorization = seed_version(db_path, key="missing-authorization")
    missing_source = seed_version(db_path, key="missing-source")
    seed_version(db_path, key="incomplete", published_asset_count=6)
    with connect_database(db_path) as conn:
        conn.execute(
            "UPDATE person_identities SET authorization_asset_id = NULL WHERE id = ?",
            (missing_authorization.identity_id,),
        )
        conn.execute(
            "UPDATE person_identities SET source_asset_id = NULL WHERE id = ?",
            (missing_source.identity_id,),
        )
        conn.commit()

    for user_id in ("employee_1", "admin_1", "auditor_1"):
        response = client.get(
            "/api/projects/project-owned/character-versions/available",
            headers=headers(user_id),
        )
        assert response.status_code == 200
        assert [item["character_version_id"] for item in response.json()] == [available.version_id]
        option = response.json()[0]
        assert option["identity_name"] == "available 荣哥"
        assert option["persona_snapshot_json"]["name"] == "available 项目经理"
        assert option["version_number"] == 3
        assert len(option["assets"]) == 7
        assert {item["view_type"] for item in option["assets"]} == set(
            REQUIRED_CHARACTER_VIEW_TYPES
        )

    forbidden = client.get(
        "/api/projects/project-other/character-versions/available",
        headers=headers("employee_1"),
    )
    assert forbidden.status_code == 403
    assert forbidden.json()["detail"]["code"] == "PROJECT_FORBIDDEN"


def test_project_selects_character_version_once_and_restores_frozen_snapshot(
    client: TestClient,
    db_path: Path,
) -> None:
    available = seed_version(db_path, key="selected")

    first = client.put(
        "/api/projects/project-owned/main-character",
        headers=headers("employee_1"),
        json={"character_version_id": available.version_id},
    )
    repeated = client.put(
        "/api/projects/project-owned/main-character",
        headers=headers("employee_1"),
        json={"character_version_id": available.version_id},
    )
    restored = client.get(
        "/api/projects/project-owned/main-character",
        headers=headers("employee_1"),
    )

    assert first.status_code == repeated.status_code == restored.status_code == 200
    assert first.json()["character_id"] is None
    assert first.json()["character_version_id"] == available.version_id
    assert repeated.json()["version_id"] == first.json()["version_id"]
    assert restored.json() == first.json()
    snapshot = restored.json()["character_snapshot"]
    assert snapshot["schema_version"] == "project-character-selection.v1"
    assert snapshot["identity"]["display_name"] == "selected 荣哥"
    assert snapshot["persona_snapshot_json"]["name"] == "selected 项目经理"
    assert snapshot["character_version_number"] == 3
    assert len(snapshot["published_assets"]) == 7

    with connect_database(db_path) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM versions WHERE project_id = ? AND kind = 'main_character'",
            ("project-owned",),
        ).fetchone()[0]
        binding = conn.execute(
            """
            SELECT character_id, character_version_id
            FROM project_main_characters WHERE project_id = ?
            """,
            ("project-owned",),
        ).fetchone()
        audit_count = conn.execute(
            """
            SELECT COUNT(*) FROM audit_logs
            WHERE action = 'project.main_character.choose_version'
              AND entity_id = ?
            """,
            (first.json()["version_id"],),
        ).fetchone()[0]
    assert count == 1
    assert binding["character_id"] is None
    assert binding["character_version_id"] == available.version_id
    assert audit_count == 1


def test_concurrent_repeat_selection_reuses_one_project_snapshot(
    client: TestClient,
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    available = seed_version(db_path, key="concurrent")
    barrier = threading.Barrier(2)
    original_choice = character_routes.choose_project_character_version

    def synchronized_choice(
        conn: sqlite3.Connection,
        *,
        actor: CurrentUser,
        project_id: str,
        character_version_id: str,
    ) -> dict[str, object]:
        barrier.wait(timeout=5)
        return original_choice(
            conn,
            actor=actor,
            project_id=project_id,
            character_version_id=character_version_id,
        )

    monkeypatch.setattr(
        character_routes,
        "choose_project_character_version",
        synchronized_choice,
    )
    responses = []
    responses_lock = threading.Lock()

    def select_version() -> None:
        response = client.put(
            "/api/projects/project-owned/main-character",
            headers=headers("employee_1"),
            json={"character_version_id": available.version_id},
        )
        with responses_lock:
            responses.append(response)

    threads = [threading.Thread(target=select_version) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(response.status_code for response in responses) == [200, 200]
    assert len({response.json()["version_id"] for response in responses}) == 1
    with connect_database(db_path) as conn:
        version_count = conn.execute(
            """
            SELECT COUNT(*) FROM versions
            WHERE project_id = ? AND kind = 'main_character'
            """,
            ("project-owned",),
        ).fetchone()[0]
        audit_count = conn.execute(
            """
            SELECT COUNT(*) FROM audit_logs
            WHERE action = 'project.main_character.choose_version'
            """
        ).fetchone()[0]
    assert version_count == 1
    assert audit_count == 1


def test_concurrent_version_switches_return_each_requests_own_snapshot(
    client: TestClient,
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_version = seed_version(db_path, key="switch-first")
    second_version = seed_version(db_path, key="switch-second")
    response_read_barrier = threading.Barrier(2)
    original_get = project_character_selection.get_project_main_character

    def synchronized_get(
        conn: sqlite3.Connection,
        *,
        project_id: str,
    ) -> dict[str, object]:
        if not conn.in_transaction:
            response_read_barrier.wait(timeout=5)
        return original_get(conn, project_id=project_id)

    monkeypatch.setattr(
        project_character_selection,
        "get_project_main_character",
        synchronized_get,
    )
    responses: list[tuple[str, object]] = []
    responses_lock = threading.Lock()

    def select_version(version_id: str) -> None:
        response = client.put(
            "/api/projects/project-owned/main-character",
            headers=headers("employee_1"),
            json={"character_version_id": version_id},
        )
        with responses_lock:
            responses.append((version_id, response))

    threads = [
        threading.Thread(target=select_version, args=(first_version.version_id,)),
        threading.Thread(target=select_version, args=(second_version.version_id,)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(responses) == 2
    for requested_version_id, response in responses:
        assert response.status_code == 200
        assert response.json()["character_version_id"] == requested_version_id
        assert response.json()["character_snapshot"]["character_version_id"] == requested_version_id


def test_project_snapshot_survives_character_archive_and_live_edits(
    client: TestClient,
    db_path: Path,
) -> None:
    available = seed_version(db_path, key="frozen")
    selected = client.put(
        "/api/projects/project-owned/main-character",
        headers=headers("employee_1"),
        json={"character_version_id": available.version_id},
    )
    assert selected.status_code == 200

    with connect_database(db_path) as conn:
        conn.execute(
            """
            UPDATE person_identities
            SET display_name = '已改名', status = 'ARCHIVED'
            WHERE id = ?
            """,
            (available.identity_id,),
        )
        conn.execute(
            "UPDATE character_personas SET name = '已改人设' WHERE id = ?",
            (available.persona_id,),
        )
        conn.execute(
            """
            UPDATE character_versions
            SET status = 'ARCHIVED', persona_snapshot_json = '{"name":"已改快照"}'
            WHERE id = ?
            """,
            (available.version_id,),
        )
        conn.commit()

    restored = client.get(
        "/api/projects/project-owned/main-character",
        headers=headers("employee_1"),
    )
    available_after_archive = client.get(
        "/api/projects/project-owned/character-versions/available",
        headers=headers("employee_1"),
    )

    assert restored.status_code == 200
    snapshot = restored.json()["character_snapshot"]
    assert snapshot["identity"]["display_name"] == "frozen 荣哥"
    assert snapshot["persona_snapshot_json"]["name"] == "frozen 项目经理"
    assert available_after_archive.status_code == 200
    assert available_after_archive.json() == []


def test_project_rejects_unavailable_version_invalid_payload_and_auditor_write(
    client: TestClient,
    db_path: Path,
) -> None:
    available = seed_version(db_path, key="writeable")
    draft = seed_version(db_path, key="draft-write", status="DRAFT")
    expired = seed_version(
        db_path,
        key="expired-write",
        authorization_expires_at="2020-01-01T00:00:00+00:00",
    )
    incomplete = seed_version(db_path, key="incomplete-write", published_asset_count=6)

    for version_id in (draft.version_id, expired.version_id, incomplete.version_id):
        denied = client.put(
            "/api/projects/project-owned/main-character",
            headers=headers("employee_1"),
            json={"character_version_id": version_id},
        )
        assert denied.status_code == 422
        assert denied.json()["detail"]["code"] == "CHARACTER_VERSION_NOT_AVAILABLE"

    auditor = client.put(
        "/api/projects/project-owned/main-character",
        headers=headers("auditor_1"),
        json={"character_version_id": available.version_id},
    )
    assert auditor.status_code == 403
    assert auditor.json()["detail"]["code"] == "ROLE_FORBIDDEN"

    for payload in ({}, {"character_id": "legacy", "character_version_id": available.version_id}):
        invalid = client.put(
            "/api/projects/project-owned/main-character",
            headers=headers("employee_1"),
            json=payload,
        )
        assert invalid.status_code == 422


def test_project_character_selection_is_published_in_openapi(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()

    assert "/api/projects/{project_id}/character-versions/available" in schema["paths"]
    request_schema = schema["components"]["schemas"]["ProjectMainCharacterRequest"]
    assert {"character_id", "character_version_id"} <= set(request_schema["properties"])
    option_schema = schema["components"]["schemas"]["ProjectCharacterVersionOption"]
    assert {
        "character_version_id",
        "identity_name",
        "persona_snapshot_json",
        "publication_hash",
        "assets",
    } <= set(option_schema["required"])
