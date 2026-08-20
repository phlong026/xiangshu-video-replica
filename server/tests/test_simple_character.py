from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import struct
import threading
import time
import zlib
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit

import pytest
from cryptography.fernet import Fernet
from fastapi import UploadFile
from fastapi.testclient import TestClient

import app.rbac_routes as rbac_routes
import app.simple_character_routes as simple_character_routes
from app.auth import CurrentUser, get_database
from app.character_identity import REQUIRED_CHARACTER_VIEW_TYPES
from app.character_identity_routes import get_character_storage
from app.character_image_generation import deterministic_png, png_chunk
from app.db import connect_database, initialize_database
from app.first_frame_routes import get_image_provider
from app.first_frames import GeneratedImage, ImageProviderFailed
from app.main import app
from app.media import storage_key_from_uri
from app.media_routes import get_media_storage
from app.simple_character import (
    SIMPLE_CONTACT_SHEET_MODEL,
    SimpleCharacterCreationResult,
    SimpleCharacterView,
    _decode_png_rgb,
    contact_sheet_placeholder_png,
    crop_contact_sheet_views,
)
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
    sheet_content: bytes = b"contact-sheet-image"

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
        return [GeneratedImage(content=self.sheet_content, content_type="image/png")]


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


def test_simple_character_generation_runs_provider_work_off_the_event_loop(
    db_path: Path,
    storage: FakeStorageAdapter,
    contact_sheet_provider: StubContactSheetProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event_loop_thread_id = threading.get_ident()
    provider_thread_id: int | None = None

    def observed_create_simple_character(
        *args: object, **kwargs: object
    ) -> SimpleCharacterCreationResult:
        nonlocal provider_thread_id
        provider_thread_id = threading.get_ident()
        return SimpleCharacterCreationResult(
            identity_id="identity-1",
            persona_id="persona-1",
            character_version_id="version-1",
            publication_hash="hash-1",
            contact_sheet_asset_id="asset-1",
            views=(SimpleCharacterView(view_type="FRONT_FACE", asset_id="asset-front"),),
        )

    monkeypatch.setattr(
        simple_character_routes,
        "create_simple_character",
        observed_create_simple_character,
    )

    class InMemoryUpload:
        size = 5
        content_type = "image/png"

        async def read(self) -> bytes:
            return b"image"

    async def generate_character() -> simple_character_routes.SimpleCharacterResponse:
        with connect_database(db_path) as conn:
            return await simple_character_routes._run_simple_character_creation(
                conn=conn,
                actor=CurrentUser(
                    id="employee_1",
                    username="employee_1",
                    display_name="Employee One",
                    role="employee",
                ),
                storage=storage,
                provider=contact_sheet_provider,
                file=cast(UploadFile, InMemoryUpload()),
                display_name="荣哥",
                persona_name="",
                project_id="project-owned",
            )

    response = asyncio.run(generate_character())

    assert response.character_version_id == "version-1"
    assert provider_thread_id is not None
    assert provider_thread_id != event_loop_thread_id


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


def test_character_cache_downloads_once_and_serves_local_copy(
    client: TestClient,
    storage: FakeStorageAdapter,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    created = generate_global(client).json()
    asset_id = created["contact_sheet_asset_id"]
    monkeypatch.setenv("VIDEO_REPLICA_HOME", str(tmp_path / "video-replica-home"))
    monkeypatch.setenv("VIDEO_REPLICA_SETTINGS_KEY", Fernet.generate_key().decode("ascii"))
    monkeypatch.setattr(rbac_routes, "storage_for_asset", lambda conn, storage_uri: storage)

    get_object_calls: list[str] = []
    original_get_object = storage.get_object

    def counted_get_object(key: str) -> bytes:
        get_object_calls.append(key)
        time.sleep(0.05)
        return original_get_object(key)

    monkeypatch.setattr(storage, "get_object", counted_get_object)

    with ThreadPoolExecutor(max_workers=2) as executor:
        requests = [
            executor.submit(
                client.post,
                f"/api/assets/{asset_id}/cached-url",
                headers=headers("employee_1"),
            )
            for _ in range(2)
        ]
        first, second = (request.result() for request in requests)

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert len(get_object_calls) == 1

    # Drop process-only state and make any cloud read fail: the next request
    # must still succeed from the file cache, as it would after an app restart.
    with rbac_routes.CHARACTER_CACHE_LOCKS_GUARD:
        rbac_routes.CHARACTER_CACHE_LOCKS.clear()

    def unexpected_storage(*_args: object) -> FakeStorageAdapter:
        raise AssertionError("cached character image must not be downloaded again")

    monkeypatch.setattr(rbac_routes, "storage_for_asset", unexpected_storage)

    auditor = client.post(
        f"/api/assets/{asset_id}/cached-url",
        headers=headers("auditor_1"),
    )
    assert auditor.status_code == 200, auditor.text
    assert len(get_object_calls) == 1

    parsed = urlsplit(second.json()["url"])
    cached = client.get(f"{parsed.path}?{parsed.query}")
    assert cached.status_code == 200
    assert cached.content == b"contact-sheet-image"
    assert cached.headers["content-type"].startswith("image/png")

    invalid_signature = client.get(f"{parsed.path}?{parsed.query}x")
    assert invalid_signature.status_code == 403


def test_approved_character_view_can_use_local_cache(
    client: TestClient,
    storage: FakeStorageAdapter,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    created = generate_global(client).json()
    asset_id = created["views"][0]["asset_id"]
    monkeypatch.setenv("VIDEO_REPLICA_HOME", str(tmp_path / "video-replica-home"))
    monkeypatch.setenv("VIDEO_REPLICA_SETTINGS_KEY", Fernet.generate_key().decode("ascii"))
    monkeypatch.setattr(rbac_routes, "storage_for_asset", lambda conn, storage_uri: storage)

    response = client.post(
        f"/api/assets/{asset_id}/cached-url",
        headers=headers("employee_1"),
    )

    assert response.status_code == 200, response.text
    parsed = urlsplit(response.json()["url"])
    cached = client.get(f"{parsed.path}?{parsed.query}")
    assert cached.status_code == 200
    assert cached.headers["content-type"].startswith("image/png")


def test_character_cache_rejects_source_with_wrong_hash(
    client: TestClient,
    storage: FakeStorageAdapter,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    created = generate_global(client).json()
    asset_id = created["contact_sheet_asset_id"]
    cache_home = tmp_path / "video-replica-home"
    monkeypatch.setenv("VIDEO_REPLICA_HOME", str(cache_home))
    monkeypatch.setenv("VIDEO_REPLICA_SETTINGS_KEY", Fernet.generate_key().decode("ascii"))
    monkeypatch.setattr(rbac_routes, "storage_for_asset", lambda conn, storage_uri: storage)
    monkeypatch.setattr(storage, "get_object", lambda key: b"corrupted-image")

    response = client.post(
        f"/api/assets/{asset_id}/cached-url",
        headers=headers("employee_1"),
    )

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "CHARACTER_CACHE_UNAVAILABLE"
    cache_root = cache_home / "storage-cache" / "character-images"
    assert not cache_root.exists() or not any(cache_root.iterdir())


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
    url = f"/api/simple-characters/identities/{created['identity_id']}/regenerate-contact-sheet"

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


# ---------------------------------------------------------------------------
# Contact-sheet view cropping


def build_five_panel_sheet_png(width: int = 300, height: int = 200) -> bytes:
    """Build a divider-accurate five-panel sheet (three columns + stacked pair).

    The layout mirrors the provider prompt: white outer border, thin white
    dividers, three equal tall columns on ~70% of the width, and two stacked
    close-ups on the right. Distinct panel colours make crop assertions easy.
    """
    border, gap = 6, 4
    left_zone = round((width - 2 * border - gap) * 0.705)
    column = (left_zone - 2 * gap) / 3
    c1 = round(border + column)
    c2 = round(border + column + gap)
    c3 = round(border + 2 * column + gap)
    c4 = round(border + 2 * column + 2 * gap)
    c5 = border + left_zone
    right_x0 = border + left_zone + gap
    middle = height // 2
    colors = {
        "front_full": (200, 10, 10),
        "left_45": (10, 200, 10),
        "left_45_dark": (10, 100, 10),
        "left_side": (10, 10, 200),
        "left_side_dark": (10, 10, 100),
        "front_face": (200, 200, 10),
        "lower_right": (200, 10, 200),
    }
    white = (255, 255, 255)

    def pixel(x: int, y: int) -> tuple[int, int, int]:
        if x < border or x >= width - border or y < border or y >= height - border:
            return white
        if x < c1:
            return colors["front_full"]
        if x < c2:
            return white
        if x < c3:
            # Two hues inside the LEFT_45 column so mirror assertions can
            # tell the mirrored ordering apart from the original.
            mid = (c2 + c3) // 2
            return colors["left_45"] if x < mid else colors["left_45_dark"]
        if x < c4:
            return white
        if x < c5:
            mid = (c4 + c5) // 2
            return colors["left_side"] if x < mid else colors["left_side_dark"]
        if x < right_x0:
            return white
        if y < middle - 2:
            return colors["front_face"]
        if y < middle + 2:
            return white
        return colors["lower_right"]

    scanlines = bytearray()
    for y in range(height):
        scanlines += b"\x00"
        for x in range(width):
            scanlines += bytes(pixel(x, y))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + png_chunk(b"IHDR", header)
        + png_chunk(b"IDAT", zlib.compress(bytes(scanlines)))
        + png_chunk(b"IEND", b"")
    )


def build_solid_sheet_png(width: int = 300, height: int = 200) -> bytes:
    """A divider-free solid sheet that forces the nominal-layout fallback."""
    color = (60, 120, 180)
    scanlines = bytearray()
    for _ in range(height):
        scanlines += b"\x00" + bytes(color) * width
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + png_chunk(b"IHDR", header)
        + png_chunk(b"IDAT", zlib.compress(bytes(scanlines)))
        + png_chunk(b"IEND", b"")
    )


def _mirror_pixels(row: bytes) -> bytes:
    """Reverse the pixel order of an RGB row, keeping channel order."""
    return b"".join(row[i : i + 3] for i in range(len(row) - 3, -1, -3))


def test_crop_contact_sheet_views_extracts_panels_from_dividers() -> None:
    views = crop_contact_sheet_views(build_five_panel_sheet_png(), "image/png")
    assert views is not None
    assert set(views) == set(REQUIRED_CHARACTER_VIEW_TYPES)
    for content in views.values():
        assert content.startswith(b"\x89PNG\r\n\x1a\n")

    decoded = {name: _decode_png_rgb(content) for name, content in views.items()}
    # FRONT_FULL is the first tall column: 64px wide, full inner height.
    width, height, rows = decoded["FRONT_FULL"]
    assert (width, height) == (64, 188)
    assert rows[0][:3] == bytes((200, 10, 10))
    # FRONT_FACE is the upper-right close-up: 84px wide, upper inner half.
    width, height, rows = decoded["FRONT_FACE"]
    assert (width, height) == (84, 92)
    assert rows[height // 2][:3] == bytes((200, 200, 10))
    # LEFT_45 keeps the middle column's bright-half colour on its left edge.
    _, _, rows = decoded["LEFT_45"]
    assert rows[0][:3] == bytes((10, 200, 10))
    assert rows[0][-3:] == bytes((10, 100, 10))
    # RIGHT_* views are pixel-exact horizontal mirrors of the LEFT_* panels.
    assert decoded["RIGHT_45"][2][0] == _mirror_pixels(decoded["LEFT_45"][2][0])
    assert decoded["RIGHT_SIDE"][2][-1] == _mirror_pixels(decoded["LEFT_SIDE"][2][-1])
    assert decoded["RIGHT_45"][2][0][:3] == bytes((10, 100, 10))
    assert decoded["RIGHT_45"][2][0][-3:] == bytes((10, 200, 10))
    # FRONT_HALF is the upper portion of the front-full column.
    width, height, rows = decoded["FRONT_HALF"]
    assert width == 64
    assert height == round(188 * 0.62)
    assert rows[0][:3] == bytes((200, 10, 10))


def test_crop_contact_sheet_views_falls_back_to_nominal_layout() -> None:
    views = crop_contact_sheet_views(build_solid_sheet_png(), "image/png")
    assert views is not None
    assert set(views) == set(REQUIRED_CHARACTER_VIEW_TYPES)
    decoded = {name: _decode_png_rgb(content) for name, content in views.items()}
    # Nominal geometry: border 4, gap 4, left zone ~70.5% split into thirds.
    width, height, rows = decoded["FRONT_FULL"]
    assert (width, height) == (66, 192)
    assert rows[0][:3] == bytes((60, 120, 180))
    width, height, _ = decoded["FRONT_FACE"]
    assert (width, height) == (82, 94)


def test_crop_contact_sheet_views_returns_none_for_undecodable_sheets() -> None:
    # The default provider stub payload is not a PNG at all.
    assert crop_contact_sheet_views(b"contact-sheet-image", "image/png") is None
    # Non-PNG provider output (JPEG/WebP) cannot be cropped without Pillow.
    assert crop_contact_sheet_views(build_five_panel_sheet_png(), "image/jpeg") is None
    # Truncated PNG data fails decoding instead of producing garbage views.
    assert crop_contact_sheet_views(build_five_panel_sheet_png()[:200], "image/png") is None


def test_generate_crops_views_from_provider_sheet(
    client: TestClient,
    db_path: Path,
    storage: FakeStorageAdapter,
    contact_sheet_provider: StubContactSheetProvider,
) -> None:
    """With a real PNG sheet from the provider, published views must be crops."""
    sheet = build_five_panel_sheet_png()
    contact_sheet_provider.sheet_content = sheet
    response = generate_global(client)
    assert response.status_code == 201, response.text
    payload = response.json()

    expected = crop_contact_sheet_views(sheet, "image/png")
    assert expected is not None

    with connect_database(db_path) as conn:
        approved = conn.execute(
            """
            SELECT a.storage_uri, a.metadata_json
            FROM character_assets AS view
            JOIN assets AS a ON a.id = view.asset_id
            WHERE view.character_version_id = ?
              AND view.is_published_selection = 1
            """,
            (payload["character_version_id"],),
        ).fetchall()
        assert len(approved) == len(REQUIRED_CHARACTER_VIEW_TYPES)
        for row in approved:
            metadata = json.loads(str(row["metadata_json"]))
            view_type = str(metadata["view_type"])
            content = storage.get_object(storage_key_from_uri(str(row["storage_uri"])))
            assert content == expected[view_type]

        generated = conn.execute(
            """
            SELECT a.metadata_json FROM assets AS a
            WHERE a.kind = 'character_generated_image'
              AND json_extract(a.metadata_json, '$.character_version_id') = ?
            """,
            (payload["character_version_id"],),
        ).fetchall()
        assert len(generated) == len(REQUIRED_CHARACTER_VIEW_TYPES)
        assert all(
            json.loads(str(row[0]))["view_content_source"] == "contact_sheet_crop"
            for row in generated
        )


def test_generate_falls_back_to_placeholder_views_for_stub_payload(
    client: TestClient,
    db_path: Path,
) -> None:
    """Undecodable provider output keeps the deterministic placeholder views."""
    response = generate_global(client)
    assert response.status_code == 201, response.text
    payload = response.json()

    with connect_database(db_path) as conn:
        generated = conn.execute(
            """
            SELECT a.metadata_json FROM assets AS a
            WHERE a.kind = 'character_generated_image'
              AND json_extract(a.metadata_json, '$.character_version_id') = ?
            """,
            (payload["character_version_id"],),
        ).fetchall()
        assert len(generated) == len(REQUIRED_CHARACTER_VIEW_TYPES)
        assert all(
            json.loads(str(row[0]))["view_content_source"] == "local_placeholder"
            for row in generated
        )
