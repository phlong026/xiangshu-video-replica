from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.rbac_routes as rbac_routes
import app.simple_character_routes as simple_character_routes
from app.auth import get_database
from app.character_identity import REQUIRED_CHARACTER_VIEW_TYPES
from app.character_identity_routes import get_character_storage
from app.character_image_generation import deterministic_png
from app.db import connect_database, initialize_database
from app.first_frame_routes import get_image_provider
from app.first_frames import GeneratedImage, ImageProviderFailed
from app.main import app
from app.media import storage_key_from_uri
from app.media_routes import get_media_storage
from app.simple_character import SIMPLE_CONTACT_SHEET_MODEL, contact_sheet_placeholder_png
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


@dataclass
class StubContactSheetProvider:
    """Image provider stub that renders the contact sheet deterministically."""

    provider_name: str = "stub"
    calls: list[dict[str, object]] = field(default_factory=list)

    def edit(
        self,
        *,
        model: str,
        prompt: str,
        source_image: object,
        character_reference_images: list[object],
        output_count: int,
    ) -> list[GeneratedImage]:
        self.calls.append(
            {
                "model": model,
                "prompt": prompt,
                "source_image": source_image,
                "character_reference_images": character_reference_images,
                "output_count": output_count,
            }
        )
        return [GeneratedImage(content=b"contact-sheet-image", content_type="image/png")]


class FailingContactSheetProvider(StubContactSheetProvider):
    def edit(self, **kwargs: object) -> list[GeneratedImage]:
        raise ImageProviderFailed("provider down")


@pytest.fixture()
def contact_sheet_provider() -> StubContactSheetProvider:
    return StubContactSheetProvider()


@pytest.fixture()
def client(
    db_path: Path,
    storage: FakeStorageAdapter,
    contact_sheet_provider: StubContactSheetProvider,
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
    app.dependency_overrides[get_image_provider] = lambda: contact_sheet_provider
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


def test_generate_response_returns_published_view_assets(client: TestClient, db_path: Path) -> None:
    response = generate_global(client)
    assert response.status_code == 201, response.text
    payload = response.json()

    # The response carries one approved asset per required view so the UI can
    # preview and download the seven views immediately after upload.
    assert [view["view_type"] for view in payload["views"]] == list(REQUIRED_CHARACTER_VIEW_TYPES)
    assert all(view["asset_id"] for view in payload["views"])

    with connect_database(db_path) as conn:
        published_ids = {
            str(row["asset_id"])
            for row in conn.execute(
                """
                SELECT asset_id FROM character_assets
                WHERE character_version_id = ?
                  AND review_status = 'APPROVED'
                  AND is_published_selection = 1
                """,
                (payload["character_version_id"],),
            ).fetchall()
        }
    assert {view["asset_id"] for view in payload["views"]} == published_ids


def test_generate_creates_contact_sheet_asset(
    client: TestClient,
    db_path: Path,
    storage: FakeStorageAdapter,
    contact_sheet_provider: StubContactSheetProvider,
) -> None:
    source_bytes = deterministic_png(b"simple-character-source")
    response = generate_global(client)
    assert response.status_code == 201, response.text
    payload = response.json()
    contact_asset_id = payload["contact_sheet_asset_id"]
    assert contact_asset_id

    # The provider was asked to render the single five-view sheet from the
    # uploaded photo with the identity-preserve prompt.
    assert len(contact_sheet_provider.calls) == 1
    call = contact_sheet_provider.calls[0]
    assert call["model"] == SIMPLE_CONTACT_SHEET_MODEL
    assert "five-panel" in str(call["prompt"])
    assert getattr(call["source_image"], "content") == source_bytes
    assert call["output_count"] == 1

    with connect_database(db_path) as conn:
        row = conn.execute(
            "SELECT kind, sha256, metadata_json FROM assets WHERE id = ?",
            (contact_asset_id,),
        ).fetchone()
        assert row is not None
        assert row["kind"] == "character_contact_sheet"
        assert row["sha256"] == hashlib.sha256(b"contact-sheet-image").hexdigest()
        metadata = json.loads(str(row["metadata_json"]))
        assert metadata["character_version_id"] == payload["character_version_id"]
        assert metadata["generation_source"] == "image_provider"
        assert storage.get_object(str(metadata["object_key"])) == b"contact-sheet-image"

        snapshot = json.loads(
            str(
                conn.execute(
                    "SELECT publication_snapshot_json FROM character_versions WHERE id = ?",
                    (payload["character_version_id"],),
                ).fetchone()["publication_snapshot_json"],
            )
        )
        assert snapshot["contact_sheet_asset_id"] == contact_asset_id


def test_contact_sheet_download_url_allowed_for_employees(
    client: TestClient,
    storage: FakeStorageAdapter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = generate_global(client).json()
    # The fake adapter's storage_uri cannot be re-signed by storage_for_asset;
    # reroute resolution to the same in-memory adapter so the endpoint covers
    # the full permission branch instead of a 503.
    monkeypatch.setattr(
        rbac_routes,
        "storage_for_asset",
        lambda conn, storage_uri: storage,
    )

    for user_id in ("employee_1", "employee_2"):
        response = client.post(
            f"/api/assets/{created['contact_sheet_asset_id']}/download-url",
            headers=headers(user_id),
        )
        assert response.status_code == 200, response.text
        assert response.json()["url"]


def test_contact_sheet_provider_failure_falls_back_to_placeholder(
    client: TestClient,
    db_path: Path,
) -> None:
    app.dependency_overrides[get_image_provider] = lambda: FailingContactSheetProvider()
    response = generate_global(client)
    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["contact_sheet_asset_id"]

    with connect_database(db_path) as conn:
        row = conn.execute(
            "SELECT kind, size_bytes, metadata_json FROM assets WHERE id = ?",
            (payload["contact_sheet_asset_id"],),
        ).fetchone()
        assert row is not None
        assert row["kind"] == "character_contact_sheet"
        metadata = json.loads(str(row["metadata_json"]))
        assert metadata["generation_source"] == "local_placeholder"
        expected = contact_sheet_placeholder_png(
            f"contact-sheet:{payload['character_version_id']}".encode()
        )
        assert row["size_bytes"] == len(expected)


def test_contact_sheet_download_url_rejects_auditors(client: TestClient) -> None:
    created = generate_global(client).json()

    response = client.post(
        f"/api/assets/{created['contact_sheet_asset_id']}/download-url",
        headers=headers("auditor_1"),
    )
    assert response.status_code == 403


def test_library_requires_auth(client: TestClient) -> None:
    response = client.get("/api/simple-characters/library")
    assert response.status_code == 401


def test_library_lists_characters_with_published_views(
    client: TestClient,
) -> None:
    created = generate_global(client).json()

    response = client.get(
        "/api/simple-characters/library",
        headers=headers("employee_1"),
    )

    assert response.status_code == 200, response.text
    entries = response.json()
    matching = [entry for entry in entries if entry["identity_id"] == created["identity_id"]]
    assert len(matching) == 1
    entry = matching[0]
    assert entry["display_name"] == "荣哥"
    assert entry["status"] == "ACTIVE"
    assert entry["contact_sheet_asset_id"] == created["contact_sheet_asset_id"]
    assert {view["view_type"] for view in entry["views"]} == set(REQUIRED_CHARACTER_VIEW_TYPES)
    assert {view["asset_id"] for view in entry["views"]} == {
        view["asset_id"] for view in created["views"]
    }


def test_library_falls_back_to_views_when_snapshot_has_no_contact_sheet(
    client: TestClient, db_path: Path
) -> None:
    """Versions published before contact sheets keep serving the seven-grid UI."""
    created = generate_global(client).json()
    version_id = created["character_version_id"]
    with connect_database(db_path) as conn:
        row = conn.execute(
            "SELECT publication_snapshot_json FROM character_versions WHERE id = ?",
            (version_id,),
        ).fetchone()
        snapshot = json.loads(str(row["publication_snapshot_json"]))
        del snapshot["contact_sheet_asset_id"]
        conn.execute(
            "UPDATE character_versions SET publication_snapshot_json = ? WHERE id = ?",
            (json.dumps(snapshot), version_id),
        )
        conn.commit()

    response = client.get("/api/simple-characters/library", headers=headers("employee_1"))
    matching = [
        entry for entry in response.json() if entry["identity_id"] == created["identity_id"]
    ]
    assert len(matching) == 1
    entry = matching[0]
    assert entry["contact_sheet_asset_id"] is None
    assert len(entry["views"]) == len(REQUIRED_CHARACTER_VIEW_TYPES)


def test_library_is_visible_to_all_roles(client: TestClient) -> None:
    generate_global(client)

    for user_id in ("employee_1", "admin_1", "auditor_1"):
        response = client.get("/api/simple-characters/library", headers=headers(user_id))
        assert response.status_code == 200, response.text
        assert len(response.json()) == 1


def generate_global(client: TestClient, *, user_id: str = "employee_1"):
    return client.post(
        "/api/simple-characters/generate",
        headers=headers(user_id),
        files=upload_files(),
        data={"display_name": "荣哥", "persona_name": "乡墅项目管理专家"},
    )


def test_global_generate_creates_identity_without_project_context(
    client: TestClient, db_path: Path
) -> None:
    response = generate_global(client)
    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["publication_hash"]

    # The identity is owned by the creator so it can be renamed later.
    with connect_database(db_path) as conn:
        row = conn.execute(
            "SELECT owner_user_id, display_name, status FROM person_identities WHERE id = ?",
            (payload["identity_id"],),
        ).fetchone()
        assert row is not None
        assert row["owner_user_id"] == "employee_1"
        assert row["display_name"] == "荣哥"
        assert row["status"] == "ACTIVE"


def test_global_generate_still_rejects_auditors(client: TestClient) -> None:
    response = generate_global(client, user_id="auditor_1")
    assert response.status_code == 403


def test_owner_can_rename_identity(client: TestClient) -> None:
    created = generate_global(client).json()

    response = client.patch(
        f"/api/simple-characters/identities/{created['identity_id']}/name",
        headers=headers("employee_1"),
        json={"display_name": "新名字"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["display_name"] == "新名字"


def test_admin_can_rename_any_identity(client: TestClient) -> None:
    created = generate_global(client).json()

    response = client.patch(
        f"/api/simple-characters/identities/{created['identity_id']}/name",
        headers=headers("admin_1"),
        json={"display_name": "管理员改名"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["display_name"] == "管理员改名"


def test_other_employee_cannot_rename_identity(client: TestClient) -> None:
    created = generate_global(client).json()

    response = client.patch(
        f"/api/simple-characters/identities/{created['identity_id']}/name",
        headers=headers("employee_2"),
        json={"display_name": "越权改名"},
    )

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "IDENTITY_RENAME_FORBIDDEN"


def test_rename_rejects_empty_name(client: TestClient) -> None:
    created = generate_global(client).json()

    response = client.patch(
        f"/api/simple-characters/identities/{created['identity_id']}/name",
        headers=headers("employee_1"),
        json={"display_name": "   "},
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "IDENTITY_NAME_REQUIRED"


def test_owner_delete_removes_identity_records_and_objects(
    client: TestClient,
    db_path: Path,
    storage: FakeStorageAdapter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = generate_global(client).json()
    identity_id = created["identity_id"]
    # Route resolution through the real storage_for_asset would 503 on the
    # fake:// uri; reroute to the in-memory adapter so object deletion is real.
    monkeypatch.setattr(
        simple_character_routes,
        "storage_for_asset",
        lambda conn, uri: storage,
    )

    with connect_database(db_path) as conn:
        keys = [
            storage_key_from_uri(str(row["storage_uri"]))
            for row in conn.execute(
                """
                SELECT assets.storage_uri
                FROM assets
                WHERE json_extract(assets.metadata_json, '$.identity_id') = ?
                   OR assets.id IN (
                        SELECT view.asset_id
                        FROM character_assets AS view
                        JOIN character_versions AS version
                          ON version.id = view.character_version_id
                        JOIN character_personas AS persona
                          ON persona.id = version.persona_id
                        WHERE persona.identity_id = ?
                          AND view.asset_id IS NOT NULL
                   )
                """,
                (identity_id, identity_id),
            ).fetchall()
        ]
    assert len(keys) >= 9  # source + contact sheet + seven view assets

    response = client.delete(
        f"/api/simple-characters/identities/{identity_id}",
        headers=headers("employee_1"),
    )
    assert response.status_code == 204, response.text

    library = client.get("/api/simple-characters/library", headers=headers("employee_1")).json()
    assert all(entry["identity_id"] != identity_id for entry in library)

    with connect_database(db_path) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM person_identities WHERE id = ?", (identity_id,)
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                """
            SELECT COUNT(*) FROM character_personas WHERE identity_id = ?
            """,
                (identity_id,),
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM character_versions WHERE id = ?",
                (created["character_version_id"],),
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM character_assets WHERE character_version_id = ?",
                (created["character_version_id"],),
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                """
            SELECT COUNT(*) FROM assets
            WHERE json_extract(metadata_json, '$.identity_id') = ?
            """,
                (identity_id,),
            ).fetchone()[0]
            == 0
        )

    for key in keys:
        with pytest.raises(KeyError):
            storage.get_object(key)


def test_admin_can_delete_any_identity(client: TestClient) -> None:
    created = generate_global(client).json()

    response = client.delete(
        f"/api/simple-characters/identities/{created['identity_id']}",
        headers=headers("admin_1"),
    )

    assert response.status_code == 204, response.text


def test_other_employee_cannot_delete_identity(client: TestClient) -> None:
    created = generate_global(client).json()

    response = client.delete(
        f"/api/simple-characters/identities/{created['identity_id']}",
        headers=headers("employee_2"),
    )

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "IDENTITY_DELETE_FORBIDDEN"


def test_delete_rejects_identity_selected_by_project(
    client: TestClient,
    db_path: Path,
) -> None:
    created = generate_global(client).json()
    version_id = created["character_version_id"]
    with connect_database(db_path) as conn:
        conn.execute(
            """
            INSERT INTO versions (id, project_id, kind, version_number, payload_json)
            VALUES ('version-frame', 'project-owned', 'script', 1, '{}')
            """
        )
        conn.execute(
            """
            INSERT INTO character_reference_selections (
                id, project_id, source_frame_version_id, character_version_id
            ) VALUES ('selection-1', 'project-owned', 'version-frame', ?)
            """,
            (version_id,),
        )
        conn.commit()

    response = client.delete(
        f"/api/simple-characters/identities/{created['identity_id']}",
        headers=headers("employee_1"),
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "IDENTITY_IN_USE"
    # Nothing was removed.
    library = client.get("/api/simple-characters/library", headers=headers("employee_1")).json()
    assert any(entry["identity_id"] == created["identity_id"] for entry in library)


def test_delete_missing_identity_returns_404(client: TestClient) -> None:
    response = client.delete(
        "/api/simple-characters/identities/identity-missing",
        headers=headers("admin_1"),
    )
    assert response.status_code == 404


def test_owner_regenerates_contact_sheet_as_next_version(
    client: TestClient,
    db_path: Path,
    contact_sheet_provider: StubContactSheetProvider,
) -> None:
    created = generate_global(client).json()
    identity_id = created["identity_id"]

    response = client.post(
        f"/api/simple-characters/identities/{identity_id}/regenerate-contact-sheet",
        headers=headers("employee_1"),
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["identity_id"] == identity_id
    assert body["persona_id"] == created["persona_id"]
    assert body["previous_version_id"] == created["character_version_id"]
    assert body["version_number"] == 2
    assert body["character_version_id"] != created["character_version_id"]
    assert body["contact_sheet_asset_id"] != created["contact_sheet_asset_id"]
    assert len(body["views"]) == len(REQUIRED_CHARACTER_VIEW_TYPES)

    # The regeneration call reuses the stored source photo as provider input.
    assert contact_sheet_provider.calls[-1]["model"] == SIMPLE_CONTACT_SHEET_MODEL
    source_image = contact_sheet_provider.calls[-1]["source_image"]
    assert source_image.content == deterministic_png(b"simple-character-source")

    # The previously published version stays untouched for bound projects.
    with connect_database(db_path) as conn:
        rows = conn.execute(
            """
            SELECT version_number, status FROM character_versions
            WHERE persona_id = ? ORDER BY version_number
            """,
            (created["persona_id"],),
        ).fetchall()
    assert [(row["version_number"], row["status"]) for row in rows] == [
        (1, "PUBLISHED"),
        (2, "PUBLISHED"),
    ]

    # The library preview switches to the regenerated contact sheet.
    library = client.get("/api/simple-characters/library", headers=headers("employee_1")).json()
    entry = next(item for item in library if item["identity_id"] == identity_id)
    assert entry["contact_sheet_asset_id"] == body["contact_sheet_asset_id"]


def test_regenerate_contact_sheet_requires_owner_or_admin(client: TestClient) -> None:
    created = generate_global(client).json()
    url = (
        f"/api/simple-characters/identities/{created['identity_id']}"
        "/regenerate-contact-sheet"
    )

    forbidden = client.post(url, headers=headers("employee_2"))
    assert forbidden.status_code == 403
    assert forbidden.json()["detail"]["code"] == "IDENTITY_REGENERATE_FORBIDDEN"

    auditor = client.post(url, headers=headers("auditor_1"))
    assert auditor.status_code == 403

    missing = client.post(
        "/api/simple-characters/identities/identity-missing/regenerate-contact-sheet",
        headers=headers("admin_1"),
    )
    assert missing.status_code == 404

    # The admin can regenerate any identity.
    admin_ok = client.post(url, headers=headers("admin_1"))
    assert admin_ok.status_code == 201, admin_ok.text
    assert admin_ok.json()["version_number"] == 2
