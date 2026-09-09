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
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit

import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException, UploadFile
from fastapi.testclient import TestClient

import app.rbac_routes as rbac_routes
import app.simple_character as simple_character
import app.simple_character_routes as simple_character_routes
from app.auth import CurrentUser, get_database
from app.character_identity import REQUIRED_CHARACTER_VIEW_TYPES
from app.character_identity_routes import get_character_storage
from app.character_image_generation import deterministic_png, png_chunk
from app.customer_fence import BusinessDb
from app.db import connect_database, initialize_database
from app.db_portable import BusinessConnection
from app.first_frame_routes import get_image_provider
from app.first_frames import (
    FirstFrameCandidateInspection,
    FirstFrameSourceInspection,
    GeneratedImage,
    ImageInput,
    ImageProviderFailed,
    SceneContactSheetInspection,
    effective_reference_asset_ids,
)
from app.generation_worker import run_worker_once
from app.image_tasks import (
    acquire_character_sheet_task,
    complete_character_sheet_task,
    perform_character_sheet_task,
    prepare_character_sheet_task,
)
from app.main import app
from app.media import storage_key_from_uri
from app.media_routes import get_media_storage
from app.simple_character import (
    SIMPLE_CONTACT_SHEET_MODEL,
    PreparedSimpleCharacterGeneration,
    _decode_png_rgb,
    contact_sheet_placeholder_png,
    crop_contact_sheet_views,
    delete_simple_character_identity,
    store_simple_character_publication,
)
from app.storage import FakeStorageAdapter

STUB_CONTACT_SHEET = contact_sheet_placeholder_png(b"stub-contact-sheet")


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
    sheet_content: bytes = STUB_CONTACT_SHEET

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


@dataclass
class SequenceSceneLookQualityInspector:
    inspections: list[SceneContactSheetInspection]
    calls: int = 0

    def inspect_source(self, source_image: ImageInput) -> FirstFrameSourceInspection:
        raise AssertionError("source-frame inspection is not expected")

    def inspect_candidate(
        self,
        *,
        source_image: ImageInput,
        character_reference_images: list[ImageInput],
        candidate: GeneratedImage,
        expected_outfit: str,
    ) -> FirstFrameCandidateInspection:
        raise AssertionError("first-frame inspection is not expected")

    def inspect_scene_contact_sheet(
        self,
        *,
        source_image: ImageInput,
        contact_sheet: GeneratedImage,
        scene_description: str,
        costume_description: str,
    ) -> SceneContactSheetInspection:
        assert source_image.content
        assert contact_sheet.content
        assert scene_description
        assert costume_description
        self.calls += 1
        return self.inspections.pop(0)


def scene_sheet_inspection(*, identity_score: float = 0.95) -> SceneContactSheetInspection:
    return SceneContactSheetInspection(
        view_count=5,
        identity_consistency_score=identity_score,
        outfit_match_score=0.95,
        scene_match_score=0.95,
        anatomy_valid=True,
        text_detected=False,
        extra_people_detected=False,
        notes=[],
        provider="test-scene-quality",
        model="test-scene-quality-v1",
    )


@pytest.fixture()
def contact_sheet_provider() -> StubContactSheetProvider:
    return StubContactSheetProvider(sheet_content=build_five_panel_sheet_png())


@pytest.fixture()
def client(
    db_path: Path,
    storage: FakeStorageAdapter,
    contact_sheet_provider: StubContactSheetProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    # Migrated routes (BusinessDb.write) open their own SQLite connection from
    # the env path; it must point at the same database the override yields.
    monkeypatch.setenv("VIDEO_REPLICA_DB_PATH", str(db_path))

    def database_override() -> Iterator[BusinessConnection]:
        conn = BusinessConnection.sqlite(connect_database(db_path))
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
    assert payload["generate_url"] == "/api/simple-characters/tasks/generate"
    assert payload["max_size_bytes"] == 10 * 1024 * 1024
    assert "image/png" in payload["allowed_content_types"]
    assert "image/webp" in payload["allowed_content_types"]
    assert payload["required_form_fields"] == ["file", "display_name", "idempotency_key"]
    assert payload["task_status_url_template"] == "/api/simple-characters/task-status/{task_id}"


def test_character_sheet_task_is_idempotent_and_recovers_from_server_state(
    client: TestClient,
    db_path: Path,
    storage: FakeStorageAdapter,
    contact_sheet_provider: StubContactSheetProvider,
) -> None:
    data = {
        "display_name": "荣哥",
        "persona_name": "乡墅项目管理专家",
        "idempotency_key": "character-sheet-task-1",
    }
    first = client.post(
        "/api/simple-characters/tasks/generate",
        headers=headers("employee_1"),
        files=upload_files(),
        data=data,
    )
    replay = client.post(
        "/api/simple-characters/tasks/generate",
        headers=headers("employee_1"),
        files=upload_files(),
        data=data,
    )

    assert first.status_code == 202
    assert replay.status_code == 202
    assert replay.json()["id"] == first.json()["id"]
    with BusinessConnection.sqlite(connect_database(db_path)) as conn:
        assert (
            run_worker_once(
                conn,
                worker_id="image-worker-test",
                storage=storage,
                image_provider=contact_sheet_provider,
                max_tasks=1,
            )
            == 1
        )

    task = client.get(
        f"/api/simple-characters/task-status/{first.json()['id']}",
        headers=headers("employee_1"),
    )
    assert task.status_code == 200
    assert task.json()["status"] == "SUCCEEDED"
    assert task.json()["result_identity_id"]
    assert task.json()["result"]["generation_source"] == "image_provider"
    with BusinessConnection.sqlite(connect_database(db_path)) as conn:
        stored_result = json.loads(
            str(
                conn.execute(
                    "SELECT result_json FROM character_sheet_tasks WHERE id = %s",
                    (first.json()["id"],),
                ).fetchone()[0]
            )
        )
    assert stored_result["provider"] == "stub"
    assert stored_result["model"] == "gpt-image-2"


def test_character_sheet_task_status_hides_foreign_task(client: TestClient) -> None:
    created = client.post(
        "/api/simple-characters/tasks/generate",
        headers=headers("employee_1"),
        files=upload_files(),
        data={"display_name": "荣哥", "idempotency_key": "character-sheet-task-private"},
    )
    assert created.status_code == 202, created.text

    foreign = client.get(
        f"/api/simple-characters/task-status/{created.json()['id']}",
        headers=headers("employee_2"),
    )
    missing = client.get(
        "/api/simple-characters/task-status/task-missing",
        headers=headers("employee_2"),
    )

    assert foreign.status_code == 404
    assert foreign.content == missing.content
    assert foreign.json()["detail"]["code"] == "IMAGE_TASK_NOT_FOUND"


def test_character_sheet_task_requires_auth_and_enforces_actual_upload_size(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unauthenticated = client.post(
        "/api/simple-characters/tasks/generate",
        files=upload_files(),
        data={"display_name": "荣哥", "idempotency_key": "character-sheet-task-2"},
    )
    assert unauthenticated.status_code == 401

    monkeypatch.setattr(simple_character_routes, "SIMPLE_UPLOAD_MAX_BYTES", 4)
    oversized = client.post(
        "/api/simple-characters/tasks/generate",
        headers=headers("employee_1"),
        files={"file": ("portrait.png", b"12345", "image/png")},
        data={"display_name": "荣哥", "idempotency_key": "character-sheet-task-3"},
    )
    assert oversized.status_code == 422
    assert oversized.json()["detail"]["code"] == "SIMPLE_CHARACTER_IMAGE_TOO_LARGE"


def test_character_sheet_task_authorizes_before_persisting_upload(
    client: TestClient,
    storage: FakeStorageAdapter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writes: list[str] = []
    original_put_object = storage.put_object

    def observed_put_object(key: str, content: bytes, *, content_type: str):
        writes.append(key)
        return original_put_object(key, content, content_type=content_type)

    monkeypatch.setattr(storage, "put_object", observed_put_object)
    response = client.post(
        "/api/simple-characters/tasks/project-owned/generate",
        headers=headers("employee_2"),
        files=upload_files(),
        data={"display_name": "荣哥", "idempotency_key": "character-sheet-task-4"},
    )
    missing = client.post(
        "/api/simple-characters/tasks/project-missing/generate",
        headers=headers("employee_2"),
        files=upload_files(),
        data={"display_name": "荣哥", "idempotency_key": "character-sheet-task-5"},
    )

    assert response.status_code == 404
    assert response.content == missing.content
    assert writes == []


def test_character_sheet_task_lease_loss_rolls_back_character_rows(
    client: TestClient,
    db_path: Path,
    storage: FakeStorageAdapter,
    contact_sheet_provider: StubContactSheetProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = client.post(
        "/api/simple-characters/tasks/generate",
        headers=headers("employee_1"),
        files=upload_files(),
        data={
            "display_name": "荣哥",
            "idempotency_key": "character-sheet-lease-race-1",
        },
    )
    assert created.status_code == 202

    with BusinessConnection.sqlite(connect_database(db_path)) as conn:
        lease = acquire_character_sheet_task(conn, worker_id="image-worker-lease-test")
        assert lease is not None
        prepared = prepare_character_sheet_task(
            conn,
            lease=lease,
            storage=storage,
            provider=contact_sheet_provider,
        )
        generation = perform_character_sheet_task(prepared)
        original_execute = conn.execute
        raced = False

        def race_lease(sql: str, params: tuple[object, ...] = ()):
            nonlocal raced
            if not raced and "INSERT INTO person_identities" in sql:
                raced = True
                original_execute(
                    "UPDATE character_sheet_tasks SET locked_by = %s WHERE id = %s",
                    ("stolen-worker", lease.id),
                )
            return original_execute(sql, params)

        monkeypatch.setattr(conn, "execute", race_lease)
        with pytest.raises(HTTPException) as exc_info:
            complete_character_sheet_task(
                conn,
                prepared=prepared,
                generation=generation,
                storage=storage,
            )
        assert exc_info.value.status_code == 500

        task = original_execute(
            "SELECT locked_by, status FROM character_sheet_tasks WHERE id = %s",
            (lease.id,),
        ).fetchone()
        assert task is not None
        assert task["locked_by"] == lease.worker_id
        assert task["status"] == "RUNNING"
        assert original_execute("SELECT COUNT(*) FROM person_identities").fetchone()[0] == 0
        assert original_execute("SELECT COUNT(*) FROM character_versions").fetchone()[0] == 0


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


@pytest.mark.parametrize(
    ("filename", "content", "content_type"),
    [
        pytest.param("portrait.png", b"\x89PNG\r\n\x1a\n", "image/png", id="png-header"),
        pytest.param("portrait.jpg", b"\xff\xd8\xff\xd9", "image/jpeg", id="jpeg-header"),
        pytest.param(
            "portrait.webp",
            b"RIFF\x04\x00\x00\x00WEBP",
            "image/webp",
            id="webp-header",
        ),
        pytest.param(
            "portrait.jpg",
            deterministic_png(b"mime-mismatch"),
            "image/jpeg",
            id="mime-mismatch",
        ),
        pytest.param(
            "portrait.png",
            bytes.fromhex(
                "89504e470d0a1a0a0000000d494844520000012c000000c80802000000"
                "ddbd4b020000000949444154789c630000000100015eff7df900000000"
                "49454e44ae426082"
            ),
            "image/png",
            id="png-invalid-pixels",
        ),
        pytest.param(
            "portrait.jpg",
            bytes.fromhex("ffd8ffc000070800010001ffda000278ffd9"),
            "image/jpeg",
            id="jpeg-invalid-pixels",
        ),
        pytest.param(
            "portrait.webp",
            bytes.fromhex("524946461800000057454250565038200c0000000000009d012a010001000000"),
            "image/webp",
            id="webp-invalid-pixels",
        ),
    ],
)
def test_simple_character_rejects_invalid_image_bytes(
    client: TestClient,
    db_path: Path,
    contact_sheet_provider: StubContactSheetProvider,
    filename: str,
    content: bytes,
    content_type: str,
) -> None:
    response = client.post(
        "/api/simple-characters/project-owned/generate",
        headers=headers("employee_1"),
        files={"file": (filename, content, content_type)},
        data={"display_name": "荣哥"},
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "SIMPLE_CHARACTER_IMAGE_INVALID"
    assert contact_sheet_provider.calls == []
    with BusinessConnection.sqlite(connect_database(db_path)) as conn:
        assert conn.execute("SELECT 1 FROM person_identities").fetchone() is None


def test_simple_character_accepts_webp_source(
    client: TestClient,
    contact_sheet_provider: StubContactSheetProvider,
) -> None:
    source = bytes.fromhex(
        "524946461e000000574542505650384c110000002f0000000007d0fffef7bfff8188e87f0000"
    )
    response = client.post(
        "/api/simple-characters/project-owned/generate",
        headers=headers("employee_1"),
        files={"file": ("portrait.webp", source, "image/webp")},
        data={"display_name": "荣哥"},
    )

    assert response.status_code == 201, response.text
    provider_source = contact_sheet_provider.calls[-1]["source_image"]
    assert getattr(provider_source, "content") == source
    assert getattr(provider_source, "content_type") == "image/webp"


def test_simple_character_accepts_jpeg_source(
    client: TestClient,
    contact_sheet_provider: StubContactSheetProvider,
) -> None:
    source = bytes.fromhex(
        "ffd8ffe000104a46494600010200000100010000fffe00104c61766336322e32382e31303200"
        "ffdb004300083e3e493e49555555555555645d64686868646464646868687070708383837070"
        "70686870707c7c83838f938f8787838793939b9b9bbabab2b2d9d9e0ffffffffc4004b000101"
        "00000000000000000000000000000008010100000000000000000000000000000000100100"
        "000000000000000000000000000000110100000000000000000000000000000000ffc00011"
        "080010001003012200021100031100ffda000c03010002110311003f009fc007ffd9"
    )
    response = client.post(
        "/api/simple-characters/project-owned/generate",
        headers=headers("employee_1"),
        files={"file": ("portrait.jpg", source, "image/jpeg")},
        data={"display_name": "荣哥"},
    )

    assert response.status_code == 201, response.text
    provider_source = contact_sheet_provider.calls[-1]["source_image"]
    assert getattr(provider_source, "content") == source
    assert getattr(provider_source, "content_type") == "image/jpeg"


def test_character_sheet_task_rejects_invalid_image_before_storage(
    client: TestClient,
    db_path: Path,
    storage: FakeStorageAdapter,
) -> None:
    response = client.post(
        "/api/simple-characters/tasks/generate",
        headers=headers("employee_1"),
        files={"file": ("portrait.png", b"\x89PNG\r\n\x1a\n", "image/png")},
        data={"display_name": "荣哥", "idempotency_key": "invalid-source"},
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "SIMPLE_CHARACTER_IMAGE_INVALID"
    assert storage._objects == {}
    with BusinessConnection.sqlite(connect_database(db_path)) as conn:
        assert conn.execute("SELECT 1 FROM character_sheet_tasks").fetchone() is None


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
    storage: FakeStorageAdapter,
    contact_sheet_provider: StubContactSheetProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event_loop_thread_id = threading.get_ident()
    provider_thread_id: int | None = None
    storage_thread_ids: list[int] = []
    original_edit = contact_sheet_provider.edit
    original_put_object = storage.put_object

    def observed_edit(
        *,
        model: str,
        prompt: str,
        source_image: object,
        character_reference_images: list[object],
        output_count: int,
    ) -> list[GeneratedImage]:
        nonlocal provider_thread_id
        provider_thread_id = threading.get_ident()
        return original_edit(
            model=model,
            prompt=prompt,
            source_image=source_image,
            character_reference_images=character_reference_images,
            output_count=output_count,
        )

    monkeypatch.setattr(contact_sheet_provider, "edit", observed_edit)

    def observed_put_object(
        key: str,
        content: bytes,
        *,
        content_type: str,
    ):
        storage_thread_ids.append(threading.get_ident())
        return original_put_object(key, content, content_type=content_type)

    monkeypatch.setattr(storage, "put_object", observed_put_object)

    source_content = deterministic_png(b"async-character-source")

    class InMemoryUpload:
        size = len(source_content)
        content_type = "image/png"

        async def read(self) -> bytes:
            return source_content

    async def prepare_character():
        return await simple_character_routes._prepare_simple_character_upload(
            file=cast(UploadFile, InMemoryUpload()),
            display_name="荣哥",
            persona_name="",
            provider=contact_sheet_provider,
            actor=CurrentUser(
                id="employee_1",
                username="employee_1",
                display_name="Employee One",
                role="employee",
            ),
            storage=storage,
        )

    content, content_type, persona_name, prepared = asyncio.run(prepare_character())

    assert content == source_content
    assert content_type == "image/png"
    assert persona_name == "荣哥"
    assert prepared.generation.contact_content == contact_sheet_provider.sheet_content
    assert len(prepared.object_keys) == 16
    assert provider_thread_id is not None
    assert provider_thread_id != event_loop_thread_id
    assert storage_thread_ids
    assert all(thread_id != event_loop_thread_id for thread_id in storage_thread_ids)


def test_auditor_cannot_generate(client: TestClient) -> None:
    response = generate(client, user_id="auditor_1")
    assert response.status_code == 403


def test_employee_cannot_generate_for_foreign_project(client: TestClient) -> None:
    response = generate(client, user_id="employee_2")
    missing = client.post(
        "/api/simple-characters/project-missing/generate",
        headers=headers("employee_2"),
        files=upload_files(),
        data={"display_name": "荣哥", "persona_name": "乡墅项目管理专家"},
    )
    assert response.status_code == 404
    assert response.content == missing.content


def test_generated_character_appears_in_available_versions(
    client: TestClient, db_path: Path
) -> None:
    response = generate(client)
    assert response.status_code == 201, response.text
    payload = response.json()
    version_id = payload["character_version_id"]
    assert payload["publication_hash"]
    # Version row uses the simple_upload generation mode and is published.
    with BusinessConnection.sqlite(connect_database(db_path)) as conn:
        row = conn.execute(
            """
            SELECT status, generation_mode, published_at, persona_snapshot_json,
                   publication_snapshot_json
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
        reviews = conn.execute(
            """
            SELECT review.reviewer_user_id, review.decision, review.comment
            FROM character_asset_reviews AS review
            JOIN character_assets AS asset ON asset.id = review.character_asset_id
            WHERE asset.character_version_id = ?
            """,
            (version_id,),
        ).fetchall()
        assert len(reviews) == len(REQUIRED_CHARACTER_VIEW_TYPES)
        assert all(review["reviewer_user_id"] is None for review in reviews)
        assert all(review["decision"] == "APPROVED" for review in reviews)
        assert all(
            review["comment"] == "System auto-approved by direct-publish policy."
            for review in reviews
        )
        publication = json.loads(str(row["publication_snapshot_json"]))
        assert publication["review_policy"] == "SYSTEM_AUTO_PUBLISH"

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

    with BusinessConnection.sqlite(connect_database(db_path)) as conn:
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
    assert payload["generation_source"] == "image_provider"

    # The provider was asked to render the single five-view sheet from the
    # uploaded photo with the identity-preserve prompt.
    assert len(contact_sheet_provider.calls) == 1
    call = contact_sheet_provider.calls[0]
    assert call["model"] == SIMPLE_CONTACT_SHEET_MODEL
    assert "five-panel" in str(call["prompt"])
    assert getattr(call["source_image"], "content") == source_bytes
    assert call["output_count"] == 1

    with BusinessConnection.sqlite(connect_database(db_path)) as conn:
        row = conn.execute(
            "SELECT kind, sha256, metadata_json FROM assets WHERE id = ?",
            (contact_asset_id,),
        ).fetchone()
        assert row is not None
        assert row["kind"] == "character_contact_sheet"
        assert row["sha256"] == hashlib.sha256(contact_sheet_provider.sheet_content).hexdigest()
        metadata = json.loads(str(row["metadata_json"]))
        assert metadata["character_version_id"] == payload["character_version_id"]
        assert metadata["generation_source"] == "image_provider"
        assert (
            storage.get_object(str(metadata["object_key"])) == contact_sheet_provider.sheet_content
        )

        snapshot = json.loads(
            str(
                conn.execute(
                    "SELECT publication_snapshot_json FROM character_versions WHERE id = ?",
                    (payload["character_version_id"],),
                ).fetchone()["publication_snapshot_json"],
            )
        )
        assert snapshot["contact_sheet_asset_id"] == contact_asset_id


def test_contact_sheet_download_url_isolated_to_owner_and_privileged_roles(
    client: TestClient,
    storage: FakeStorageAdapter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VIDEO_REPLICA_SETTINGS_KEY", Fernet.generate_key().decode("ascii"))
    created = generate_global(client).json()
    # The fake adapter's storage_uri cannot be re-signed by storage_for_asset;
    # reroute resolution to the same in-memory adapter so the endpoint covers
    # the full permission branch instead of a 503.
    monkeypatch.setattr(
        rbac_routes,
        "storage_for_asset",
        lambda conn, storage_uri: storage,
    )

    for user_id in ("employee_1", "admin_1"):
        response = client.post(
            f"/api/assets/{created['contact_sheet_asset_id']}/download-url",
            headers=headers(user_id),
        )
        assert response.status_code == 200, response.text
        assert response.json()["url"]

    foreign = client.post(
        f"/api/assets/{created['contact_sheet_asset_id']}/download-url",
        headers=headers("employee_2"),
    )
    absent = client.post(
        "/api/assets/asset-missing/download-url",
        headers=headers("employee_2"),
    )
    auditor = client.post(
        f"/api/assets/{created['contact_sheet_asset_id']}/download-url",
        headers=headers("auditor_1"),
    )
    assert foreign.status_code == 404
    assert foreign.content == absent.content
    assert auditor.status_code == 403


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
    assert auditor.status_code == 403, auditor.text
    assert auditor.json()["detail"]["code"] == "ROLE_FORBIDDEN"
    assert len(get_object_calls) == 1

    parsed = urlsplit(second.json()["url"])
    cached = client.get(f"{parsed.path}?{parsed.query}")
    assert cached.status_code == 200
    assert cached.content == build_five_panel_sheet_png()
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


def test_customer_character_cache_is_shared_across_api_replicas(
    client: TestClient,
    storage: FakeStorageAdapter,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    created = generate_global(client).json()
    asset_id = created["contact_sheet_asset_id"]
    shared_cache = FakeStorageAdapter(provider="cos", bucket="private-customer-cache")
    first_replica_home = tmp_path / "api-1"
    second_replica_home = tmp_path / "api-2"
    monkeypatch.setenv("VIDEO_REPLICA_HOME", str(first_replica_home))
    monkeypatch.setenv("VIDEO_REPLICA_SETTINGS_KEY", Fernet.generate_key().decode("ascii"))
    monkeypatch.setattr(rbac_routes, "is_customer_production", lambda: True)
    monkeypatch.setattr(rbac_routes, "storage_for_asset", lambda conn, storage_uri: storage)
    monkeypatch.setattr(
        rbac_routes,
        "_customer_character_cache_storage",
        lambda conn: shared_cache,
    )
    monkeypatch.setattr(
        rbac_routes,
        "_load_customer_character_cache_storage",
        lambda: shared_cache,
    )
    events: list[str] = []
    original_write = BusinessDb.write

    @contextmanager
    def traced_write(
        business_db: BusinessDb,
        **kwargs: object,
    ) -> Iterator[tuple[BusinessConnection, CurrentUser]]:
        events.append("pg-enter")
        with original_write(business_db, **kwargs) as value:
            yield value
        events.append("pg-exit")

    def assert_pg_released(event: str) -> None:
        events.append(event)
        assert "pg-exit" in events
        assert events.index("pg-exit") < events.index(event)

    original_head_object = shared_cache.head_object
    original_put_object = shared_cache.put_object
    original_get_object = storage.get_object

    def traced_head_object(key: str):
        assert_pg_released("cos-head")
        return original_head_object(key)

    def traced_put_object(key: str, content: bytes, *, content_type: str) -> None:
        assert_pg_released("cos-put")
        original_put_object(key, content, content_type=content_type)

    def traced_get_object(key: str) -> bytes:
        assert_pg_released("cos-source-get")
        return original_get_object(key)

    monkeypatch.setattr(BusinessDb, "write", traced_write)
    monkeypatch.setattr(shared_cache, "head_object", traced_head_object)
    monkeypatch.setattr(shared_cache, "put_object", traced_put_object)
    monkeypatch.setattr(storage, "get_object", traced_get_object)

    response = client.post(
        f"/api/assets/{asset_id}/cached-url",
        headers=headers("employee_1"),
    )

    assert response.status_code == 200, response.text
    parsed = urlsplit(response.json()["url"])
    # Production COS credentials are intentionally scoped to the existing
    # projects/users namespaces. Keep the shared preview cache inside that
    # allowlisted boundary instead of requiring a new bucket-root permission.
    cache_key = f"projects/character-cache/{Path(parsed.path).name}"
    assert shared_cache.head_object(cache_key) is not None
    assert events[:5] == [
        "pg-enter",
        "pg-exit",
        "cos-head",
        "cos-source-get",
        "cos-put",
    ]
    assert not first_replica_home.exists()

    monkeypatch.setenv("VIDEO_REPLICA_HOME", str(second_replica_home))
    with rbac_routes.CHARACTER_CACHE_LOCKS_GUARD:
        rbac_routes.CHARACTER_CACHE_LOCKS.clear()

    cached = client.get(f"{parsed.path}?{parsed.query}")

    assert cached.status_code == 200, cached.text
    assert cached.content == build_five_panel_sheet_png()
    assert cached.headers["content-type"].startswith("image/png")
    assert not second_replica_home.exists()


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


def test_contact_sheet_provider_failure_is_visible_and_does_not_publish(
    client: TestClient,
    db_path: Path,
) -> None:
    app.dependency_overrides[get_image_provider] = lambda: FailingContactSheetProvider()
    response = generate_global(client)
    assert response.status_code == 502, response.text
    assert response.json()["detail"]["code"] == "CONTACT_SHEET_PROVIDER_FAILED"

    with BusinessConnection.sqlite(connect_database(db_path)) as conn:
        assert conn.execute("SELECT 1 FROM person_identities").fetchone() is None


def test_undecodable_provider_sheet_is_visible_and_does_not_publish(
    client: TestClient,
    db_path: Path,
    storage: FakeStorageAdapter,
    contact_sheet_provider: StubContactSheetProvider,
) -> None:
    contact_sheet_provider.sheet_content = b"contact-sheet-image"

    response = generate_global(client)

    assert response.status_code == 502, response.text
    assert response.json()["detail"]["code"] == "CONTACT_SHEET_PROVIDER_INVALID_OUTPUT"
    assert storage._objects == {}
    with BusinessConnection.sqlite(connect_database(db_path)) as conn:
        assert conn.execute("SELECT 1 FROM person_identities").fetchone() is None
        assert conn.execute("SELECT 1 FROM character_assets").fetchone() is None


def test_prepared_publication_rejects_uncroppable_sheet_before_storage(
    storage: FakeStorageAdapter,
) -> None:
    actor = CurrentUser(
        id="employee_1",
        username="employee_1",
        display_name="Employee One",
        role="employee",
    )
    generation = PreparedSimpleCharacterGeneration(
        version_id="version-invalid-sheet",
        contact_content=corrupt_png(build_five_panel_sheet_png(), "ihdr-compression"),
        contact_content_type="image/png",
        contact_source="image_provider",
    )

    with pytest.raises(HTTPException) as exc_info:
        store_simple_character_publication(
            actor=actor,
            storage=storage,
            source_content=deterministic_png(b"prepared-publication-source"),
            source_content_type="image/png",
            display_name="荣哥",
            generation=generation,
        )

    assert exc_info.value.status_code == 502
    assert exc_info.value.detail["code"] == "CONTACT_SHEET_PROVIDER_INVALID_OUTPUT"
    assert storage._objects == {}


def test_unconfigured_local_provider_keeps_explicit_placeholder_path(
    client: TestClient,
    db_path: Path,
) -> None:
    app.dependency_overrides[get_image_provider] = lambda: None

    response = generate_global(client)

    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["generation_source"] == "local_placeholder"
    with BusinessConnection.sqlite(connect_database(db_path)) as conn:
        row = conn.execute(
            "SELECT metadata_json FROM assets WHERE id = ?",
            (payload["contact_sheet_asset_id"],),
        ).fetchone()
        assert row is not None
        assert json.loads(str(row["metadata_json"]))["generation_source"] == "local_placeholder"


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


def test_library_uses_keyset_pages_without_duplicate_identities(client: TestClient) -> None:
    created_ids = {generate_global(client).json()["identity_id"] for _ in range(3)}

    first = client.get(
        "/api/simple-characters/library?limit=2",
        headers=headers("employee_1"),
    )

    assert first.status_code == 200, first.text
    first_page = first.json()
    assert len(first_page["items"]) == 2
    assert first_page["next_cursor"]

    second = client.get(
        "/api/simple-characters/library",
        params={"limit": 2, "cursor": first_page["next_cursor"]},
        headers=headers("employee_1"),
    )
    assert second.status_code == 200, second.text
    second_page = second.json()
    assert second_page["next_cursor"] is None
    listed_ids = [
        *(item["identity_id"] for item in first_page["items"]),
        *(item["identity_id"] for item in second_page["items"]),
    ]
    assert len(listed_ids) == len(set(listed_ids))
    assert set(listed_ids) == created_ids


def test_library_cursor_is_bound_to_actor_scope_and_query(client: TestClient) -> None:
    generate_global(client, user_id="employee_1")
    generate_global(client, user_id="employee_1")
    generate_global(client, user_id="employee_2")
    first = client.get(
        "/api/simple-characters/library?limit=1",
        headers=headers("employee_1"),
    ).json()
    cursor = first["next_cursor"]
    assert cursor

    wrong_query = client.get(
        "/api/simple-characters/library",
        params={"limit": 1, "cursor": cursor, "query": "荣哥"},
        headers=headers("employee_1"),
    )
    wrong_actor = client.get(
        "/api/simple-characters/library",
        params={"limit": 1, "cursor": cursor},
        headers=headers("employee_2"),
    )
    invalid = client.get(
        "/api/simple-characters/library?cursor=not-a-cursor",
        headers=headers("employee_1"),
    )

    assert wrong_query.status_code == 400
    assert wrong_query.json()["detail"]["code"] == "CURSOR_SCOPE_MISMATCH"
    assert wrong_actor.status_code == 400
    assert wrong_actor.json()["detail"]["code"] == "CURSOR_SCOPE_MISMATCH"
    assert invalid.status_code == 400
    assert invalid.json()["detail"]["code"] == "INVALID_CURSOR"

    no_matches = client.get(
        "/api/simple-characters/library",
        params={"query": "不存在的人物"},
        headers=headers("employee_1"),
    )
    assert no_matches.status_code == 200
    assert no_matches.json() == {"items": [], "next_cursor": None, "total": 0}

    oversized = client.get(
        "/api/simple-characters/library",
        params={"cursor": "x" * 513},
        headers=headers("employee_1"),
    )
    assert oversized.status_code == 422


def test_library_lists_characters_with_published_views(
    client: TestClient,
) -> None:
    created = generate_global(client).json()

    response = client.get(
        "/api/simple-characters/library",
        headers=headers("employee_1"),
    )

    assert response.status_code == 200, response.text
    entries = response.json()["items"]
    matching = [entry for entry in entries if entry["identity_id"] == created["identity_id"]]
    assert len(matching) == 1
    entry = matching[0]
    assert entry["display_name"] == "荣哥"
    assert entry["persona_id"] == created["persona_id"]
    assert entry["version_number"] == 1
    assert entry["role"] == ""
    assert entry["service_scope"] == ""
    assert entry["target_audience"] == ""
    assert entry["expression_style"] == ""
    assert entry["status"] == "ACTIVE"
    assert entry["contact_sheet_asset_id"] == created["contact_sheet_asset_id"]
    assert entry["generation_source"] == "image_provider"
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
    with BusinessConnection.sqlite(connect_database(db_path)) as conn:
        row = conn.execute(
            "SELECT publication_snapshot_json FROM character_versions WHERE id = ?",
            (version_id,),
        ).fetchone()
        snapshot = json.loads(str(row["publication_snapshot_json"]))
        del snapshot["contact_sheet_asset_id"]
        snapshot.pop("generation_source", None)
        conn.execute(
            "UPDATE character_versions SET publication_snapshot_json = ? WHERE id = ?",
            (json.dumps(snapshot), version_id),
        )
        conn.commit()

    response = client.get("/api/simple-characters/library", headers=headers("employee_1"))
    matching = [
        entry
        for entry in response.json()["items"]
        if entry["identity_id"] == created["identity_id"]
    ]
    assert len(matching) == 1
    entry = matching[0]
    assert entry["contact_sheet_asset_id"] is None
    assert entry["generation_source"] is None
    assert len(entry["views"]) == len(REQUIRED_CHARACTER_VIEW_TYPES)


def test_library_is_isolated_by_owner_outside_control_roles(client: TestClient) -> None:
    first = generate_global(client, user_id="employee_1").json()
    second = generate_global(client, user_id="employee_2").json()

    employee_one = client.get("/api/simple-characters/library", headers=headers("employee_1"))
    employee_two = client.get("/api/simple-characters/library", headers=headers("employee_2"))
    assert [item["identity_id"] for item in employee_one.json()["items"]] == [first["identity_id"]]
    assert [item["identity_id"] for item in employee_two.json()["items"]] == [second["identity_id"]]

    for user_id in ("admin_1", "auditor_1"):
        response = client.get("/api/simple-characters/library", headers=headers(user_id))
        assert response.status_code == 200, response.text
        assert {item["identity_id"] for item in response.json()["items"]} == {
            first["identity_id"],
            second["identity_id"],
        }


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
    with BusinessConnection.sqlite(connect_database(db_path)) as conn:
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
    missing = client.patch(
        "/api/simple-characters/identities/identity-missing/name",
        headers=headers("employee_2"),
        json={"display_name": "越权改名"},
    )

    assert response.status_code == 404
    assert response.content == missing.content


def test_rename_rejects_empty_name(client: TestClient) -> None:
    created = generate_global(client).json()

    response = client.patch(
        f"/api/simple-characters/identities/{created['identity_id']}/name",
        headers=headers("employee_1"),
        json={"display_name": "   "},
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "IDENTITY_NAME_REQUIRED"


def test_owner_updates_ip_profile_and_preserves_other_constraints(
    client: TestClient,
    db_path: Path,
) -> None:
    created = generate_global(client).json()
    with BusinessConnection.sqlite(connect_database(db_path)) as conn:
        conn.execute(
            "UPDATE character_personas SET appearance_constraints_json = ? WHERE id = ?",
            (json.dumps({"keep_me": "unchanged"}), created["persona_id"]),
        )
        conn.commit()

    response = client.patch(
        f"/api/simple-characters/identities/{created['identity_id']}/profile",
        headers=headers("employee_1"),
        json={
            "display_name": "荣老师",
            "role": "乡墅项目管理顾问",
            "service_scope": "自建房全流程管理",
            "target_audience": "首次建房的返乡业主",
            "expression_style": "专业、直白、少术语",
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["identity_id"] == created["identity_id"]
    assert body["persona_id"] == created["persona_id"]
    assert body["version_number"] == 1
    assert body["display_name"] == "荣老师"
    assert body["role"] == "乡墅项目管理顾问"
    assert body["service_scope"] == "自建房全流程管理"
    assert body["target_audience"] == "首次建房的返乡业主"
    assert body["expression_style"] == "专业、直白、少术语"
    with BusinessConnection.sqlite(connect_database(db_path)) as conn:
        identity = conn.execute(
            "SELECT display_name FROM person_identities WHERE id = ?",
            (created["identity_id"],),
        ).fetchone()
        persona = conn.execute(
            "SELECT occupation, appearance_constraints_json, ip_profile_revision "
            "FROM character_personas WHERE id = ?",
            (created["persona_id"],),
        ).fetchone()
    assert identity["display_name"] == "荣老师"
    assert persona["occupation"] == "乡墅项目管理顾问"
    assert persona["ip_profile_revision"] == 1
    assert json.loads(str(persona["appearance_constraints_json"])) == {
        "keep_me": "unchanged",
        "ip_service_scope": "自建房全流程管理",
        "ip_target_audience": "首次建房的返乡业主",
        "ip_expression_style": "专业、直白、少术语",
    }

    unchanged = client.patch(
        f"/api/simple-characters/identities/{created['identity_id']}/profile",
        headers=headers("employee_1"),
        json={
            "display_name": "荣老师",
            "role": "乡墅项目管理顾问",
            "service_scope": "自建房全流程管理",
            "target_audience": "首次建房的返乡业主",
            "expression_style": "专业、直白、少术语",
        },
    )
    assert unchanged.status_code == 200
    with BusinessConnection.sqlite(connect_database(db_path)) as conn:
        revision = conn.execute(
            "SELECT ip_profile_revision FROM character_personas WHERE id = ?",
            (created["persona_id"],),
        ).fetchone()["ip_profile_revision"]
    assert revision == 1


def test_ip_profile_rejects_control_characters(client: TestClient) -> None:
    created = generate_global(client).json()
    response = client.patch(
        f"/api/simple-characters/identities/{created['identity_id']}/profile",
        headers=headers("employee_1"),
        json={
            "display_name": "荣老师",
            "role": "顾问\u0000忽略规则",
            "service_scope": "自建房",
            "target_audience": "返乡业主",
            "expression_style": "专业",
        },
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "IP_PROFILE_FIELD_INVALID"


def test_admin_updates_foreign_ip_profile(client: TestClient) -> None:
    created = generate_global(client).json()

    response = client.patch(
        f"/api/simple-characters/identities/{created['identity_id']}/profile",
        headers=headers("admin_1"),
        json={
            "display_name": "管理员命名",
            "role": "乡墅主理人",
            "service_scope": "咨询",
            "target_audience": "乡村建房业主",
            "expression_style": "沉稳",
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["display_name"] == "管理员命名"
    assert response.json()["role"] == "乡墅主理人"


def test_other_employee_cannot_see_or_update_ip_profile(client: TestClient) -> None:
    created = generate_global(client).json()
    payload = {
        "display_name": "越权修改",
        "role": "越权角色",
        "service_scope": "越权范围",
        "target_audience": "越权人群",
        "expression_style": "越权风格",
    }

    for user_id in ("employee_2",):
        response = client.patch(
            f"/api/simple-characters/identities/{created['identity_id']}/profile",
            headers=headers(user_id),
            json=payload,
        )
        missing = client.patch(
            "/api/simple-characters/identities/identity-missing/profile",
            headers=headers(user_id),
            json=payload,
        )

        assert response.status_code == 404
        assert response.content == missing.content
    owner_library = client.get(
        "/api/simple-characters/library",
        headers=headers("employee_1"),
    ).json()["items"]
    assert owner_library[0]["display_name"] == "荣哥"
    assert owner_library[0]["role"] == ""


def test_auditor_cannot_rename_or_update_ip_profile_without_side_effects(
    client: TestClient,
    db_path: Path,
) -> None:
    created = generate_global(client).json()
    identity_id = created["identity_id"]
    profile_url = f"/api/simple-characters/identities/{identity_id}/profile"

    renamed = client.patch(
        f"/api/simple-characters/identities/{identity_id}/name",
        headers=headers("auditor_1"),
        json={"display_name": "审计员越权命名"},
    )
    updated = client.patch(
        profile_url,
        headers=headers("auditor_1"),
        json={
            "display_name": "审计员越权档案",
            "role": "越权角色",
            "service_scope": "越权范围",
            "target_audience": "越权人群",
            "expression_style": "越权风格",
        },
    )

    assert renamed.status_code == 403
    assert renamed.json()["detail"]["code"] == "ROLE_FORBIDDEN"
    assert updated.status_code == 403
    assert updated.json()["detail"]["code"] == "ROLE_FORBIDDEN"
    with BusinessConnection.sqlite(connect_database(db_path)) as conn:
        identity = conn.execute(
            "SELECT display_name FROM person_identities WHERE id = %s", (identity_id,)
        ).fetchone()
        persona = conn.execute(
            "SELECT occupation, ip_profile_revision FROM character_personas WHERE id = %s",
            (created["persona_id"],),
        ).fetchone()
    assert identity["display_name"] == "荣哥"
    assert persona["occupation"] is None
    assert persona["ip_profile_revision"] == 0


def test_customer_can_update_own_ip_profile(client: TestClient, db_path: Path) -> None:
    created = generate_global(client).json()
    with BusinessConnection.sqlite(connect_database(db_path)) as conn:
        conn.execute("UPDATE users SET role = 'customer' WHERE id = 'employee_1'")
        conn.commit()

    response = client.patch(
        f"/api/simple-characters/identities/{created['identity_id']}/profile",
        headers=headers("employee_1"),
        json={
            "display_name": "客户自有IP",
            "role": "乡墅顾问",
            "service_scope": "建房咨询",
            "target_audience": "返乡业主",
            "expression_style": "专业直接",
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["display_name"] == "客户自有IP"


def test_ip_profile_update_does_not_modify_scene_persona(
    client: TestClient,
    db_path: Path,
) -> None:
    created = generate_global(client).json()
    scene_persona_id = "scene-persona-profile-guard"
    scene_constraints = {
        "appearance_type": "scene",
        "ip_service_scope": "场景原值",
        "scene_only": True,
    }
    with BusinessConnection.sqlite(connect_database(db_path)) as conn:
        conn.execute(
            """
            INSERT INTO character_personas (
                id, identity_id, name, occupation, appearance_constraints_json,
                usage_scope_json, created_by, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (
                scene_persona_id,
                created["identity_id"],
                "工地场景",
                "场景角色",
                json.dumps(scene_constraints),
                "[]",
                "employee_1",
            ),
        )
        conn.commit()

    response = client.patch(
        f"/api/simple-characters/identities/{created['identity_id']}/profile",
        headers=headers("employee_1"),
        json={
            "display_name": "荣老师",
            "role": "基础角色",
            "service_scope": "基础服务",
            "target_audience": "基础人群",
            "expression_style": "基础风格",
        },
    )

    assert response.status_code == 200, response.text
    with BusinessConnection.sqlite(connect_database(db_path)) as conn:
        scene = conn.execute(
            "SELECT occupation, appearance_constraints_json FROM character_personas WHERE id = ?",
            (scene_persona_id,),
        ).fetchone()
    assert scene["occupation"] == "场景角色"
    assert json.loads(str(scene["appearance_constraints_json"])) == scene_constraints


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

    with BusinessConnection.sqlite(connect_database(db_path)) as conn:
        version_ids = {
            str(row[0])
            for row in conn.execute(
                """
                SELECT version.id
                FROM character_versions AS version
                JOIN character_personas AS persona ON persona.id = version.persona_id
                WHERE persona.identity_id = ?
                """,
                (identity_id,),
            ).fetchall()
        }
        asset_rows = []
        for row in conn.execute("SELECT id, storage_uri, metadata_json FROM assets").fetchall():
            metadata = json.loads(str(row["metadata_json"]))
            if (
                metadata.get("identity_id") == identity_id
                or metadata.get("character_version_id") in version_ids
            ):
                asset_rows.append(row)
        asset_ids = [str(row["id"]) for row in asset_rows]
        keys = [storage_key_from_uri(str(row["storage_uri"])) for row in asset_rows]
    # source + contact sheet + seven generated candidates + seven published views
    assert len(keys) >= 16

    response = client.delete(
        f"/api/simple-characters/identities/{identity_id}",
        headers=headers("employee_1"),
    )
    assert response.status_code == 204, response.text

    library = client.get("/api/simple-characters/library", headers=headers("employee_1")).json()[
        "items"
    ]
    assert all(entry["identity_id"] != identity_id for entry in library)

    with BusinessConnection.sqlite(connect_database(db_path)) as conn:
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
                f"SELECT COUNT(*) FROM assets WHERE id IN ({','.join('?' for _ in asset_ids)})",
                tuple(asset_ids),
            ).fetchone()[0]
            == 0
        )

    for key in keys:
        with pytest.raises(KeyError):
            storage.get_object(key)


def test_auditor_cannot_delete_an_identity_even_when_recorded_as_owner(
    client: TestClient,
    db_path: Path,
) -> None:
    created = generate_global(client).json()
    identity_id = created["identity_id"]
    with BusinessConnection.sqlite(connect_database(db_path)) as conn:
        conn.execute(
            "UPDATE person_identities SET owner_user_id = %s WHERE id = %s",
            ("auditor_1", identity_id),
        )
        conn.commit()

    response = client.delete(
        f"/api/simple-characters/identities/{identity_id}",
        headers=headers("auditor_1"),
    )

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "ROLE_FORBIDDEN"
    with BusinessConnection.sqlite(connect_database(db_path)) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM person_identities WHERE id = %s",
                (identity_id,),
            ).fetchone()[0]
            == 1
        )


def test_database_delete_failure_does_not_remove_storage_objects(
    client: TestClient,
    db_path: Path,
    storage: FakeStorageAdapter,
) -> None:
    created = generate_global(client).json()
    identity_id = created["identity_id"]
    with BusinessConnection.sqlite(connect_database(db_path)) as conn:
        asset = conn.execute(
            """
            SELECT asset.storage_uri
            FROM assets AS asset
            JOIN person_identities AS identity ON identity.source_asset_id = asset.id
            WHERE identity.id = %s
            """,
            (identity_id,),
        ).fetchone()
        assert asset is not None
        source_key = storage_key_from_uri(str(asset["storage_uri"]))
        conn.execute(
            """
            CREATE TRIGGER fail_identity_delete
            BEFORE DELETE ON person_identities
            BEGIN
                SELECT RAISE(FAIL, 'forced identity delete failure');
            END
            """
        )
        conn.commit()

        with pytest.raises(sqlite3.IntegrityError, match="forced identity delete failure"):
            delete_simple_character_identity(
                conn,
                actor=CurrentUser(
                    id="employee_1",
                    username="employee_1",
                    display_name="Employee One",
                    role="employee",
                ),
                identity_id=identity_id,
                storage_for_uri=lambda _conn, _uri: storage,
            )

    assert storage.get_object(source_key)


def test_storage_cleanup_failure_is_audited_after_database_delete(
    client: TestClient,
    db_path: Path,
    storage: FakeStorageAdapter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = generate_global(client).json()
    identity_id = created["identity_id"]
    monkeypatch.setattr(
        simple_character_routes,
        "storage_for_asset",
        lambda conn, uri: storage,
    )

    def fail_delete_object(key: str, *, actor_id: str | None = None) -> None:
        raise OSError(f"storage unavailable for {key} ({actor_id})")

    monkeypatch.setattr(storage, "delete_object", fail_delete_object)

    response = client.delete(
        f"/api/simple-characters/identities/{identity_id}",
        headers=headers("employee_1"),
    )

    assert response.status_code == 204, response.text
    with BusinessConnection.sqlite(connect_database(db_path)) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM person_identities WHERE id = %s",
                (identity_id,),
            ).fetchone()[0]
            == 0
        )
        audit = conn.execute(
            """
            SELECT metadata_json FROM audit_logs
            WHERE action = 'simple_character.delete.storage_cleanup'
              AND entity_id = %s
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (identity_id,),
        ).fetchone()
    assert audit is not None
    metadata = json.loads(str(audit["metadata_json"]))
    assert metadata["failed_count"] > 0
    assert metadata["deleted_count"] == 0


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
    missing = client.delete(
        "/api/simple-characters/identities/identity-missing",
        headers=headers("employee_2"),
    )

    assert response.status_code == 404
    assert response.content == missing.content


def test_delete_rejects_identity_selected_by_project(
    client: TestClient,
    db_path: Path,
) -> None:
    created = generate_global(client).json()
    version_id = created["character_version_id"]
    with BusinessConnection.sqlite(connect_database(db_path)) as conn:
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
    library = client.get("/api/simple-characters/library", headers=headers("employee_1")).json()[
        "items"
    ]
    assert any(entry["identity_id"] == created["identity_id"] for entry in library)


def test_delete_rejects_identity_with_active_scene_task(client: TestClient) -> None:
    created = generate_global(client).json()
    identity_id = created["identity_id"]
    queued = client.post(
        f"/api/simple-characters/identities/{identity_id}/scene-looks/tasks/generate",
        headers=headers("employee_1"),
        json={
            "scene_name": "商务讲解",
            "scene_description": "现代会议室",
            "costume_description": "深色西装",
            "idempotency_key": "scene-look-delete-guard",
        },
    )
    assert queued.status_code == 202

    response = client.delete(
        f"/api/simple-characters/identities/{identity_id}",
        headers=headers("employee_1"),
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "IDENTITY_DELETE_HAS_ACTIVE_TASKS"


def test_delete_rejects_identity_with_active_oral_clone(
    client: TestClient,
    db_path: Path,
) -> None:
    created = generate_global(client).json()
    identity_id = created["identity_id"]
    with BusinessConnection.sqlite(connect_database(db_path)) as conn:
        source_asset_id = conn.execute(
            "SELECT source_asset_id FROM person_identities WHERE id = %s",
            (identity_id,),
        ).fetchone()[0]
        conn.execute(
            """
            INSERT INTO oral_avatars (
                id, identity_id, owner_user_id, title, vendor_task_id,
                status, source_kind, source_asset_id
            ) VALUES (%s, %s, 'employee_1', '制作中分身', 'vendor-task',
                      'RUNNING', 'IMAGE', %s)
            """,
            ("active-avatar", identity_id, source_asset_id),
        )
        conn.commit()

    response = client.delete(
        f"/api/simple-characters/identities/{identity_id}",
        headers=headers("employee_1"),
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "IDENTITY_DELETE_HAS_ACTIVE_TASKS"


def test_delete_rejects_identity_with_oral_billing_history(
    client: TestClient,
    db_path: Path,
) -> None:
    created = generate_global(client).json()
    identity_id = created["identity_id"]
    with BusinessConnection.sqlite(connect_database(db_path)) as conn:
        source_asset_id = conn.execute(
            "SELECT source_asset_id FROM person_identities WHERE id = %s",
            (identity_id,),
        ).fetchone()[0]
        conn.execute(
            """
            INSERT INTO oral_avatars (
                id, identity_id, owner_user_id, title, vendor_avatar_id,
                status, source_kind, source_asset_id
            ) VALUES ('billed-avatar', %s, 'employee_1', '已就绪分身', 'avatar',
                      'READY', 'IMAGE', %s)
            """,
            (identity_id, source_asset_id),
        )
        conn.execute(
            """
            INSERT INTO oral_tasks (
                id, owner_user_id, identity_id, avatar_id, mode, title, status,
                estimated_cost_fen, idempotency_key, billing_round
            ) VALUES ('billed-oral-task', 'employee_1', %s, 'billed-avatar', 'AUDIO',
                      '历史口播', 'CANCELLED', 1000, 'billed-oral-idem', 1)
            """,
            (identity_id,),
        )
        conn.execute(
            """
            INSERT INTO wallet_transactions (
                id, user_id, type, available_delta, reserved_delta,
                oral_task_id, billing_round, idempotency_key
            ) VALUES ('billed-oral-reserve', 'employee_1', 'RESERVE', -1, 1,
                      'billed-oral-task', 1, 'billed-oral-reserve-key')
            """
        )
        conn.commit()

    response = client.delete(
        f"/api/simple-characters/identities/{identity_id}",
        headers=headers("employee_1"),
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "IDENTITY_DELETE_HAS_BILLING_HISTORY"


def test_delete_removes_completed_oral_result_asset(
    client: TestClient,
    db_path: Path,
    storage: FakeStorageAdapter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = generate_global(client).json()
    identity_id = created["identity_id"]
    stored = storage.put_object(
        "oral/results/completed.mp4",
        b"completed-oral-video",
        content_type="video/mp4",
    )
    monkeypatch.setattr(
        simple_character_routes,
        "storage_for_asset",
        lambda conn, uri: storage,
    )
    with BusinessConnection.sqlite(connect_database(db_path)) as conn:
        source_asset_id = conn.execute(
            "SELECT source_asset_id FROM person_identities WHERE id = %s",
            (identity_id,),
        ).fetchone()[0]
        conn.execute(
            """
            INSERT INTO assets (
                id, project_id, kind, storage_uri, sha256, size_bytes,
                content_type, created_by_user_id
            ) VALUES ('oral-result', NULL, 'oral_video', %s, %s, %s,
                      'video/mp4', 'employee_1')
            """,
            (stored.uri, stored.sha256, stored.size),
        )
        conn.execute(
            """
            INSERT INTO oral_avatars (
                id, identity_id, owner_user_id, title, vendor_avatar_id,
                status, source_kind, source_asset_id
            ) VALUES ('ready-avatar', %s, 'employee_1', '已就绪分身', 'vendor-avatar',
                      'READY', 'IMAGE', %s)
            """,
            (identity_id, source_asset_id),
        )
        conn.execute(
            """
            INSERT INTO oral_tasks (
                id, owner_user_id, identity_id, avatar_id, mode, title,
                status, result_asset_id, estimated_cost_fen, idempotency_key
            ) VALUES ('completed-oral', 'employee_1', %s, 'ready-avatar', 'AUDIO',
                      '已完成口播', 'SUCCEEDED', 'oral-result', 1000,
                      'completed-oral-delete')
            """,
            (identity_id,),
        )
        conn.commit()

    response = client.delete(
        f"/api/simple-characters/identities/{identity_id}",
        headers=headers("employee_1"),
    )

    assert response.status_code == 204, response.text
    with BusinessConnection.sqlite(connect_database(db_path)) as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM assets WHERE id = 'oral-result'").fetchone()[0] == 0
        )
    with pytest.raises(KeyError):
        storage.get_object("oral/results/completed.mp4")


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
    with BusinessConnection.sqlite(connect_database(db_path)) as conn:
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
    library = client.get("/api/simple-characters/library", headers=headers("employee_1")).json()[
        "items"
    ]
    entry = next(item for item in library if item["identity_id"] == identity_id)
    assert entry["contact_sheet_asset_id"] == body["contact_sheet_asset_id"]


def test_regenerate_contact_sheet_targets_base_persona_after_scene_look(
    client: TestClient,
    db_path: Path,
    storage: FakeStorageAdapter,
    contact_sheet_provider: StubContactSheetProvider,
) -> None:
    created = generate_global(client).json()
    identity_id = created["identity_id"]
    queued = client.post(
        f"/api/simple-characters/identities/{identity_id}/scene-looks/tasks/generate",
        headers=headers("employee_1"),
        json={
            "scene_name": "工地巡检",
            "scene_description": "乡村别墅施工现场，白天自然光",
            "costume_description": "黄色安全帽、深蓝色工装和反光背心",
            "idempotency_key": "scene-before-base-regeneration",
        },
    )
    assert queued.status_code == 202, queued.text
    with BusinessConnection.sqlite(connect_database(db_path)) as conn:
        assert (
            run_worker_once(
                conn,
                worker_id="scene-before-regeneration-worker",
                storage=storage,
                image_provider=contact_sheet_provider,
                max_tasks=1,
            )
            == 1
        )

    scene_task = client.get(
        f"/api/simple-characters/task-status/{queued.json()['id']}",
        headers=headers("employee_1"),
    ).json()
    scene_persona_id = scene_task["result"]["persona_id"]
    response = client.post(
        f"/api/simple-characters/identities/{identity_id}/regenerate-contact-sheet",
        headers=headers("employee_1"),
    )

    assert response.status_code == 201, response.text
    regenerated = response.json()
    assert regenerated["persona_id"] == created["persona_id"]
    assert regenerated["previous_version_id"] == created["character_version_id"]
    assert regenerated["version_number"] == 2
    with BusinessConnection.sqlite(connect_database(db_path)) as conn:
        scene_versions = conn.execute(
            "SELECT version_number FROM character_versions WHERE persona_id = %s",
            (scene_persona_id,),
        ).fetchall()
    assert [row[0] for row in scene_versions] == [1]


def test_regenerate_contact_sheet_requires_owner_or_admin(client: TestClient) -> None:
    created = generate_global(client).json()
    url = f"/api/simple-characters/identities/{created['identity_id']}/regenerate-contact-sheet"

    forbidden = client.post(url, headers=headers("employee_2"))
    foreign_missing = client.post(
        "/api/simple-characters/identities/identity-missing/regenerate-contact-sheet",
        headers=headers("employee_2"),
    )
    assert forbidden.status_code == 404
    assert forbidden.content == foreign_missing.content

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


def test_async_regenerate_task_hides_foreign_identity(client: TestClient) -> None:
    created = generate_global(client).json()
    task_url = (
        f"/api/simple-characters/identities/{created['identity_id']}/regenerate-contact-sheet-task"
    )
    form = {"idempotency_key": "idem-regenerate-foreign"}

    foreign = client.post(task_url, data=form, headers=headers("employee_2"))
    missing = client.post(
        "/api/simple-characters/identities/identity-missing/regenerate-contact-sheet-task",
        data=form,
        headers=headers("employee_2"),
    )

    assert foreign.status_code == 404
    assert foreign.content == missing.content
    assert foreign.json()["detail"]["code"] == "PERSON_IDENTITY_NOT_FOUND"


def test_owner_generates_and_lists_a_direct_publish_scene_look(
    client: TestClient,
    db_path: Path,
    storage: FakeStorageAdapter,
    contact_sheet_provider: StubContactSheetProvider,
) -> None:
    created = generate_global(client).json()
    identity_id = created["identity_id"]
    scene_quality = SequenceSceneLookQualityInspector(
        inspections=[
            scene_sheet_inspection(identity_score=0.2),
            scene_sheet_inspection(identity_score=0.4),
            scene_sheet_inspection(),
        ]
    )
    provider_calls_before_scene = len(contact_sheet_provider.calls)

    queued = client.post(
        f"/api/simple-characters/identities/{identity_id}/scene-looks/tasks/generate",
        headers=headers("employee_1"),
        json={
            "scene_name": "工地巡检",
            "scene_description": "乡村别墅施工现场，白天自然光",
            "costume_description": "黄色安全帽、深蓝色工装和反光背心",
            "idempotency_key": "scene-look-task-1",
        },
    )
    assert queued.status_code == 202, queued.text

    active = client.get(
        f"/api/simple-characters/identities/{identity_id}/scene-looks/tasks/active-or-latest",
        headers=headers("employee_1"),
    )
    assert active.status_code == 200, active.text
    assert active.json()["id"] == queued.json()["id"]
    assert active.json()["status"] == "PENDING"

    foreign_active = client.get(
        f"/api/simple-characters/identities/{identity_id}/scene-looks/tasks/active-or-latest",
        headers=headers("employee_2"),
    )
    assert foreign_active.status_code == 404

    latest_base_task = client.get(
        "/api/simple-characters/tasks/active-or-latest",
        headers=headers("employee_1"),
    )
    assert latest_base_task.status_code == 200, latest_base_task.text
    assert latest_base_task.json() is None

    with BusinessConnection.sqlite(connect_database(db_path)) as conn:
        assert (
            run_worker_once(
                conn,
                worker_id="scene-look-worker",
                storage=storage,
                image_provider=contact_sheet_provider,
                first_frame_quality_inspector=scene_quality,
                max_tasks=1,
            )
            == 1
        )

    task = client.get(
        f"/api/simple-characters/task-status/{queued.json()['id']}",
        headers=headers("employee_1"),
    )
    assert task.status_code == 200
    assert task.json()["status"] == "SUCCEEDED"
    result = task.json()["result"]
    assert result["identity_id"] == identity_id
    assert result["scene_name"] == "工地巡检"
    assert len(result["views"]) == len(REQUIRED_CHARACTER_VIEW_TYPES)
    assert scene_quality.calls == 0
    assert len(contact_sheet_provider.calls) - provider_calls_before_scene == 1

    with BusinessConnection.sqlite(connect_database(db_path)) as conn:
        source_asset_id = str(
            conn.execute(
                "SELECT source_asset_id FROM person_identities WHERE id = %s",
                (identity_id,),
            ).fetchone()[0]
        )
        reference_asset_ids, reference_asset_roles = effective_reference_asset_ids(
            conn,
            character_version_id=result["character_version_id"],
            legacy_selected=[view["asset_id"] for view in result["views"]],
        )
        publication = json.loads(
            str(
                conn.execute(
                    "SELECT publication_snapshot_json FROM character_versions WHERE id = %s",
                    (result["character_version_id"],),
                ).fetchone()[0]
            )
        )
    assert reference_asset_ids == [result["contact_sheet_asset_id"], source_asset_id]
    assert reference_asset_roles == ["contact_sheet", "source_photo"]
    assert "scene_quality" not in publication

    looks = client.get(
        f"/api/simple-characters/identities/{identity_id}/scene-looks",
        headers=headers("employee_1"),
    )
    assert looks.status_code == 200, looks.text
    look_page = looks.json()
    assert look_page["total"] == 1
    assert look_page["limit"] == 12
    assert look_page["offset"] == 0
    assert look_page["items"] == [
        {
            **result,
            "published_at": look_page["items"][0]["published_at"],
        }
    ]

    # Creating a scene look must not replace the identity's base appearance.
    library = client.get(
        "/api/simple-characters/library",
        headers=headers("employee_1"),
    ).json()["items"]
    base = next(item for item in library if item["identity_id"] == identity_id)
    assert base["contact_sheet_asset_id"] == created["contact_sheet_asset_id"]
    assert base["scene_look_count"] == 1

    # The first-frame flow can select this exact scene look while the base
    # appearance remains the safe automatic default in the client.
    options = client.get(
        "/api/projects/project-owned/character-versions/available",
        headers=headers("employee_1"),
    )
    assert options.status_code == 200, options.text
    scene_option = next(
        option
        for option in options.json()
        if option["character_version_id"] == result["character_version_id"]
    )
    assert scene_option["persona_snapshot_json"]["name"] == "工地巡检"
    assert scene_option["persona_snapshot_json"]["scene_description"] == (
        "乡村别墅施工现场，白天自然光"
    )
    assert scene_option["persona_snapshot_json"]["costume_description"] == (
        "黄色安全帽、深蓝色工装和反光背心"
    )
    assert scene_option["persona_snapshot_json"]["appearance_constraints_json"] == {
        "appearance_type": "scene"
    }

    prompt = str(contact_sheet_provider.calls[-1]["prompt"])
    assert "乡村别墅施工现场" in prompt
    assert "黄色安全帽" in prompt
    assert "Do not change the person's identity or gender" in prompt


def test_scene_looks_hide_foreign_identities_and_reject_auditors(
    client: TestClient,
    db_path: Path,
) -> None:
    created = generate_global(client).json()
    identity_id = created["identity_id"]
    url = f"/api/simple-characters/identities/{identity_id}/scene-looks"

    foreign = client.get(url, headers=headers("employee_2"))
    missing = client.get(
        "/api/simple-characters/identities/identity-missing/scene-looks",
        headers=headers("employee_2"),
    )
    assert foreign.status_code == 404
    assert foreign.content == missing.content

    auditor = client.post(
        f"{url}/tasks/generate",
        headers=headers("auditor_1"),
        json={
            "scene_name": "商务讲解",
            "scene_description": "现代会议室",
            "costume_description": "深色西装",
            "idempotency_key": "scene-look-auditor",
        },
    )
    assert auditor.status_code == 403
    assert auditor.json()["detail"]["code"] == "ROLE_FORBIDDEN"
    with BusinessConnection.sqlite(connect_database(db_path)) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM character_sheet_tasks "
                "WHERE idempotency_key = 'scene-look-auditor'"
            ).fetchone()[0]
            == 0
        )


def test_customer_can_enqueue_own_scene_look(client: TestClient, db_path: Path) -> None:
    created = generate_global(client).json()
    with BusinessConnection.sqlite(connect_database(db_path)) as conn:
        conn.execute("UPDATE users SET role = 'customer' WHERE id = 'employee_1'")
        conn.commit()

    response = client.post(
        f"/api/simple-characters/identities/{created['identity_id']}/scene-looks/tasks/generate",
        headers=headers("employee_1"),
        json={
            "scene_name": "客户讲解",
            "scene_description": "现代客厅",
            "costume_description": "商务休闲装",
            "idempotency_key": "customer-scene-look",
        },
    )

    assert response.status_code == 202, response.text


def test_scene_look_page_limits_database_rows_and_returns_scoped_total(
    client: TestClient,
    db_path: Path,
) -> None:
    created = generate_global(client).json()
    identity_id = created["identity_id"]
    with BusinessConnection.sqlite(connect_database(db_path)) as conn:
        for index in range(13):
            persona_id = f"scene-page-persona-{index:02d}"
            conn.execute(
                """
                INSERT INTO character_personas (
                    id, identity_id, name, appearance_constraints_json,
                    usage_scope_json, created_by, created_at, updated_at
                ) VALUES (%s, %s, %s, %s, '[]', 'employee_1', %s, %s)
                """,
                (
                    persona_id,
                    identity_id,
                    f"场景 {index:02d}",
                    json.dumps({"appearance_type": "scene"}),
                    f"2026-09-07T00:{index:02d}:00Z",
                    f"2026-09-07T00:{index:02d}:00Z",
                ),
            )
            conn.execute(
                """
                INSERT INTO character_versions (
                    id, persona_id, version_number, status,
                    publication_snapshot_json, publication_hash,
                    published_at, published_by, created_by
                ) VALUES (%s, %s, 1, 'PUBLISHED', %s, %s, %s,
                          'employee_1', 'employee_1')
                """,
                (
                    f"scene-page-version-{index:02d}",
                    persona_id,
                    json.dumps(
                        {
                            "contact_sheet_asset_id": f"sheet-{index:02d}",
                            "generation_source": "test",
                        }
                    ),
                    "0" * 64,
                    f"2026-09-07T00:{index:02d}:00Z",
                ),
            )
        for version_number in range(2, 202):
            conn.execute(
                """
                INSERT INTO character_versions (
                    id, persona_id, version_number, status,
                    publication_snapshot_json, publication_hash,
                    published_at, published_by, created_by
                ) VALUES (%s, %s, %s, 'PUBLISHED', %s, %s, %s,
                          'employee_1', 'employee_1')
                """,
                (
                    f"unrelated-base-version-{version_number:03d}",
                    created["persona_id"],
                    version_number,
                    json.dumps(
                        {
                            "contact_sheet_asset_id": f"base-sheet-{version_number:03d}",
                            "generation_source": "test",
                        }
                    ),
                    "1" * 64,
                    f"2026-09-06T{version_number % 24:02d}:00:00Z",
                ),
            )
        conn.commit()
        statements: list[str] = []
        conn.set_trace_callback(statements.append)
        page = simple_character.list_simple_scene_looks_page(
            conn,
            actor=CurrentUser(
                id="employee_1",
                username="employee_1",
                display_name="Employee One",
                role="employee",
            ),
            identity_id=identity_id,
            limit=12,
            offset=12,
        )

    assert page.total == 13
    assert [item.scene_name for item in page.items] == ["场景 00"]
    page_queries = [
        " ".join(statement.upper().split())
        for statement in statements
        if "FROM CHARACTER_PERSONAS AS PERSONA" in statement.upper()
    ]
    assert len(page_queries) == 2
    page_query = next(statement for statement in page_queries if "LIMIT 12 OFFSET 12" in statement)
    assert "ROW_NUMBER" not in page_query
    assert page_query.index("WITH PAGED_PERSONAS AS") < page_query.index("LATEST_VERSIONS AS")
    assert "JOIN PAGED_PERSONAS AS PERSONA" in page_query


def test_scene_look_rejects_archived_identity_before_enqueue(
    client: TestClient,
    db_path: Path,
) -> None:
    created = generate_global(client).json()
    identity_id = created["identity_id"]
    with BusinessConnection.sqlite(connect_database(db_path)) as conn:
        conn.execute(
            "UPDATE person_identities SET status = 'ARCHIVED' WHERE id = %s",
            (identity_id,),
        )
        conn.commit()

    response = client.post(
        f"/api/simple-characters/identities/{identity_id}/scene-looks/tasks/generate",
        headers=headers("employee_1"),
        json={
            "scene_name": "商务讲解",
            "scene_description": "现代会议室",
            "costume_description": "深色西装",
            "idempotency_key": "scene-look-archived",
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "IDENTITY_ARCHIVED"
    with BusinessConnection.sqlite(connect_database(db_path)) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM character_sheet_tasks WHERE identity_id = %s",
            (identity_id,),
        ).fetchone()[0]
    assert count == 0


def test_scene_look_rejects_blank_inputs_before_provider_work(
    client: TestClient,
    db_path: Path,
    contact_sheet_provider: StubContactSheetProvider,
) -> None:
    created = generate_global(client).json()
    provider_calls = len(contact_sheet_provider.calls)

    response = client.post(
        f"/api/simple-characters/identities/{created['identity_id']}/scene-looks/tasks/generate",
        headers=headers("employee_1"),
        json={
            "scene_name": "   ",
            "scene_description": "现代会议室",
            "costume_description": "深色西装",
            "idempotency_key": "scene-look-blank",
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "SCENE_LOOK_NAME_REQUIRED"
    assert len(contact_sheet_provider.calls) == provider_calls
    with BusinessConnection.sqlite(connect_database(db_path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM character_sheet_tasks").fetchone()[0] == 0


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


def corrupt_png(sheet: bytes, corruption: str) -> bytes:
    if corruption == "missing-iend":
        return sheet[:-12]
    mutable = bytearray(sheet)
    position = 8
    while position < len(mutable):
        length = struct.unpack(">I", mutable[position : position + 4])[0]
        chunk_type = bytes(mutable[position + 4 : position + 8])
        crc_position = position + 8 + length
        if corruption == "ihdr-crc" and chunk_type == b"IHDR":
            mutable[crc_position] ^= 0x01
            return bytes(mutable)
        if corruption == "ihdr-compression" and chunk_type == b"IHDR":
            mutable[position + 18] = 1
            new_crc = zlib.crc32(bytes(mutable[position + 4 : crc_position])) & 0xFFFFFFFF
            mutable[crc_position : crc_position + 4] = struct.pack(">I", new_crc)
            return bytes(mutable)
        if corruption == "idat-crc" and chunk_type == b"IDAT":
            mutable[crc_position] ^= 0x01
            return bytes(mutable)
        if corruption == "idat-data" and chunk_type == b"IDAT":
            mutable[position + 8] ^= 0x01
            new_crc = zlib.crc32(bytes(mutable[position + 4 : crc_position])) & 0xFFFFFFFF
            mutable[crc_position : crc_position + 4] = struct.pack(">I", new_crc)
            return bytes(mutable)
        if corruption == "idat-crc-removed" and chunk_type == b"IDAT":
            return bytes(mutable[:crc_position] + mutable[crc_position + 4 :])
        position = crc_position + 4
    raise AssertionError(f"missing PNG chunk for corruption: {corruption}")


def test_simple_character_rejects_png_with_invalid_compressed_stream(
    client: TestClient,
    db_path: Path,
    contact_sheet_provider: StubContactSheetProvider,
) -> None:
    response = client.post(
        "/api/simple-characters/generate",
        headers=headers("employee_1"),
        files={
            "file": (
                "portrait.png",
                corrupt_png(deterministic_png(b"corrupt-source"), "idat-data"),
                "image/png",
            )
        },
        data={"display_name": "荣哥"},
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "SIMPLE_CHARACTER_IMAGE_INVALID"
    assert contact_sheet_provider.calls == []
    with BusinessConnection.sqlite(connect_database(db_path)) as conn:
        assert conn.execute("SELECT 1 FROM person_identities").fetchone() is None


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
    for corruption in (
        "missing-iend",
        "ihdr-crc",
        "ihdr-compression",
        "idat-crc",
        "idat-crc-removed",
        "idat-data",
    ):
        assert (
            crop_contact_sheet_views(
                corrupt_png(build_five_panel_sheet_png(), corruption),
                "image/png",
            )
            is None
        )


def test_crop_contact_sheet_views_honors_decompressed_size_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(simple_character, "SIMPLE_PNG_MAX_DECOMPRESSED_BYTES", 64)
    assert crop_contact_sheet_views(build_five_panel_sheet_png(), "image/png") is None


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

    with BusinessConnection.sqlite(connect_database(db_path)) as conn:
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


@pytest.mark.parametrize(
    "corruption",
    [
        "truncated",
        "missing-iend",
        "ihdr-crc",
        "ihdr-compression",
        "idat-crc",
        "idat-crc-removed",
        "idat-data",
    ],
)
def test_generate_rejects_invalid_provider_sheet_without_placeholder_views(
    client: TestClient,
    db_path: Path,
    storage: FakeStorageAdapter,
    contact_sheet_provider: StubContactSheetProvider,
    corruption: str,
) -> None:
    """Invalid provider output must not create approved placeholder views."""
    sheet = build_five_panel_sheet_png()
    contact_sheet_provider.sheet_content = (
        sheet[:200] if corruption == "truncated" else corrupt_png(sheet, corruption)
    )
    response = generate_global(client)
    assert response.status_code == 502, response.text
    assert response.json()["detail"]["code"] == "CONTACT_SHEET_PROVIDER_INVALID_OUTPUT"
    assert storage._objects == {}

    with BusinessConnection.sqlite(connect_database(db_path)) as conn:
        assert conn.execute("SELECT 1 FROM person_identities").fetchone() is None
        assert conn.execute("SELECT 1 FROM character_assets").fetchone() is None
