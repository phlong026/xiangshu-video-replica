from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.auth import get_database
from app.db import connect_database, initialize_database
from app.first_frame_routes import get_image_provider
from app.first_frames import GeneratedImage, ImageInput, RetryableImageProviderFailed
from app.main import app
from app.media_routes import get_media_storage
from app.source_frame_routes import get_source_frame_extractor
from app.source_frames import ExtractedSourceFrame
from app.storage import FakeStorageAdapter


@dataclass
class RecordingImageProvider:
    calls: list[dict[str, object]] = field(default_factory=list)
    provider_name: str = "fake"

    def edit(
        self,
        *,
        model: str,
        prompt: str,
        source_image: ImageInput,
        character_reference_images: list[ImageInput],
        output_count: int,
    ) -> list[GeneratedImage]:
        self.calls.append(
            {
                "model": model,
                "prompt": prompt,
                "source_image": source_image.content,
                "character_reference_images": [
                    image.content for image in character_reference_images
                ],
                "output_count": output_count,
            }
        )
        return [
            GeneratedImage(content=f"first-frame-{index}".encode(), content_type="image/png")
            for index in range(output_count)
        ]


@dataclass
class FlakyImageProvider(RecordingImageProvider):
    failures_remaining: int = 1

    def edit(
        self,
        *,
        model: str,
        prompt: str,
        source_image: ImageInput,
        character_reference_images: list[ImageInput],
        output_count: int,
    ) -> list[GeneratedImage]:
        if self.failures_remaining:
            self.failures_remaining -= 1
            raise RetryableImageProviderFailed("temporary provider failure")
        return super().edit(
            model=model,
            prompt=prompt,
            source_image=source_image,
            character_reference_images=character_reference_images,
            output_count=output_count,
        )


@dataclass(frozen=True)
class FakeSourceFrameExtractor:
    def extract(
        self,
        content: bytes,
        *,
        filename: str,
        timestamps_seconds: tuple[float, ...],
    ) -> list[ExtractedSourceFrame]:
        return [
            ExtractedSourceFrame(timestamp_seconds=timestamp, image=f"source-{timestamp}".encode())
            for timestamp in timestamps_seconds
        ]


@pytest.fixture()
def db_path(tmp_path: Path) -> Iterator[Path]:
    path = tmp_path / "first-frames.db"
    with initialize_database(path) as conn:
        seed_data(conn)
    yield path


@pytest.fixture()
def storage() -> FakeStorageAdapter:
    storage = FakeStorageAdapter(provider="fake", bucket="private-bucket")
    storage.put_object(
        "projects/project_owned/uploads/reference_owned/reference.mp4",
        b"reference-video",
        content_type="video/mp4",
    )
    storage.put_object("character/front.png", b"character-front", content_type="image/png")
    storage.put_object("character/side.png", b"character-side", content_type="image/png")
    return storage


@pytest.fixture()
def provider() -> RecordingImageProvider:
    return RecordingImageProvider()


@pytest.fixture()
def client(
    db_path: Path,
    storage: FakeStorageAdapter,
    provider: RecordingImageProvider,
) -> Iterator[TestClient]:
    def database_override() -> Iterator[sqlite3.Connection]:
        conn = connect_database(db_path)
        try:
            yield conn
        finally:
            conn.close()

    app.dependency_overrides[get_database] = database_override
    app.dependency_overrides[get_media_storage] = lambda: storage
    app.dependency_overrides[get_source_frame_extractor] = FakeSourceFrameExtractor
    app.dependency_overrides[get_image_provider] = lambda: provider
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def seed_data(conn: sqlite3.Connection) -> None:
    conn.executemany(
        "INSERT INTO users (id, username, display_name, role) VALUES (?, ?, ?, ?)",
        [
            ("employee_1", "employee_1", "Employee One", "employee"),
            ("admin_1", "admin_1", "Admin One", "admin"),
        ],
    )
    conn.execute(
        "INSERT INTO projects (id, owner_user_id, name) VALUES (?, ?, ?)",
        ("project_owned", "employee_1", "Owned Project"),
    )
    conn.executemany(
        """
        INSERT INTO assets (
            id, project_id, kind, storage_uri, sha256, size_bytes, content_type,
            created_by_user_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                "reference_owned",
                "project_owned",
                "reference_video",
                "fake://private-bucket/projects/project_owned/uploads/reference_owned/reference.mp4",
                "reference-hash",
                15,
                "video/mp4",
                "employee_1",
            ),
            (
                "character_front",
                "project_owned",
                "image",
                "fake://private-bucket/character/front.png",
                "front-hash",
                15,
                "image/png",
                "admin_1",
            ),
            (
                "character_side",
                "project_owned",
                "image",
                "fake://private-bucket/character/side.png",
                "side-hash",
                14,
                "image/png",
                "admin_1",
            ),
        ],
    )
    conn.commit()


def headers(user_id: str) -> dict[str, str]:
    return {"X-Dev-User-Id": user_id}


def prepare_inputs(client: TestClient) -> str:
    extracted = client.post(
        "/api/projects/project_owned/source-frames/extract",
        json={"asset_id": "reference_owned"},
        headers=headers("employee_1"),
    )
    assert extracted.status_code == 200
    source_frame_asset_id = extracted.json()["payload"]["candidates"][0]["asset_id"]
    confirmed = client.post(
        "/api/projects/project_owned/source-frames/confirm",
        json={"source_frame_asset_id": source_frame_asset_id},
        headers=headers("employee_1"),
    )
    assert confirmed.status_code == 200

    character = client.post(
        "/api/characters",
        headers=headers("admin_1"),
        json={
            "name": "林夏",
            "reference_asset_ids": ["character_front", "character_side"],
            "authorization_project_ids": ["project_owned"],
            "is_active": True,
        },
    )
    assert character.status_code == 201
    selected = client.put(
        "/api/projects/project_owned/main-character",
        json={"character_id": character.json()["id"]},
        headers=headers("employee_1"),
    )
    assert selected.status_code == 200
    return source_frame_asset_id


def test_generate_candidates_archives_them_and_preserves_image_input_order(
    client: TestClient,
    provider: RecordingImageProvider,
    storage: FakeStorageAdapter,
) -> None:
    source_frame_asset_id = prepare_inputs(client)

    response = client.post(
        "/api/projects/project_owned/first-frames/generate",
        json={"model": "nano-banana-pro-2k", "quantity": 2},
        headers=headers("employee_1"),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["kind"] == "first_frame_candidates"
    assert body["payload"]["source_frame_asset_id"] == source_frame_asset_id
    assert body["payload"]["model"] == "nano-banana-pro-2k"
    assert body["payload"]["provider"] == "fake"
    assert len(body["payload"]["candidates"]) == 2
    assert provider.calls == [
        {
            "model": "nano-banana-pro-2k",
            "prompt": body["payload"]["prompt"],
            "source_image": b"source-0.5",
            "character_reference_images": [b"character-front", b"character-side"],
            "output_count": 2,
        }
    ]
    for candidate in body["payload"]["candidates"]:
        assert storage.head_object(candidate["storage_key"]) is not None


def test_confirmed_first_frame_is_versioned_and_latest_candidates_invalidate_old_confirmation(
    client: TestClient,
) -> None:
    prepare_inputs(client)
    first = client.post(
        "/api/projects/project_owned/first-frames/generate",
        json={"model": "gpt-image-2", "quantity": 1},
        headers=headers("employee_1"),
    )
    first_asset_id = first.json()["payload"]["candidates"][0]["asset_id"]

    confirmed = client.post(
        "/api/projects/project_owned/first-frames/confirm",
        json={"first_frame_asset_id": first_asset_id},
        headers=headers("employee_1"),
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["payload"]["first_frame_asset_id"] == first_asset_id

    newer = client.post(
        "/api/projects/project_owned/first-frames/generate",
        json={"model": "gpt-image-2", "quantity": 1},
        headers=headers("employee_1"),
    )
    assert newer.status_code == 200
    latest = client.get(
        "/api/projects/project_owned/first-frames/selection/latest",
        headers=headers("employee_1"),
    )
    assert latest.status_code == 409
    assert latest.json()["detail"]["code"] == "FIRST_FRAME_SELECTION_STALE"


def test_employee_can_view_newest_first_frame_candidate_versions(client: TestClient) -> None:
    prepare_inputs(client)
    first = client.post(
        "/api/projects/project_owned/first-frames/generate",
        json={"model": "gpt-image-2", "quantity": 1},
        headers=headers("employee_1"),
    )
    second = client.post(
        "/api/projects/project_owned/first-frames/generate",
        json={"model": "nano-banana-pro-2k", "quantity": 1},
        headers=headers("employee_1"),
    )

    response = client.get(
        "/api/projects/project_owned/first-frames/history",
        headers=headers("employee_1"),
    )

    assert response.status_code == 200
    assert [version["id"] for version in response.json()] == [
        second.json()["id"],
        first.json()["id"],
    ]


def test_generation_retries_one_transient_image_provider_failure(
    client: TestClient,
    provider: RecordingImageProvider,
) -> None:
    flaky = FlakyImageProvider()
    client.app.dependency_overrides[get_image_provider] = lambda: flaky
    prepare_inputs(client)

    response = client.post(
        "/api/projects/project_owned/first-frames/generate",
        json={"model": "gpt-image-2", "quantity": 1},
        headers=headers("employee_1"),
    )

    assert response.status_code == 200
    assert flaky.failures_remaining == 0
    assert len(flaky.calls) == 1
    assert provider.calls == []


def test_source_frame_reconfirmation_makes_existing_first_frame_candidates_stale(
    client: TestClient,
) -> None:
    prepare_inputs(client)
    generated = client.post(
        "/api/projects/project_owned/first-frames/generate",
        json={"quantity": 1},
        headers=headers("employee_1"),
    )
    first_frame_asset_id = generated.json()["payload"]["candidates"][0]["asset_id"]
    extracted_again = client.post(
        "/api/projects/project_owned/source-frames/extract",
        json={"asset_id": "reference_owned"},
        headers=headers("employee_1"),
    )
    new_source_frame_asset_id = extracted_again.json()["payload"]["candidates"][0]["asset_id"]
    confirmed_source = client.post(
        "/api/projects/project_owned/source-frames/confirm",
        json={"source_frame_asset_id": new_source_frame_asset_id},
        headers=headers("employee_1"),
    )
    assert confirmed_source.status_code == 200

    response = client.post(
        "/api/projects/project_owned/first-frames/confirm",
        json={"first_frame_asset_id": first_frame_asset_id},
        headers=headers("employee_1"),
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "FIRST_FRAME_CANDIDATES_STALE"


def test_main_character_reselection_makes_confirmed_first_frame_stale(client: TestClient) -> None:
    prepare_inputs(client)
    generated = client.post(
        "/api/projects/project_owned/first-frames/generate",
        json={"quantity": 1},
        headers=headers("employee_1"),
    )
    first_frame_asset_id = generated.json()["payload"]["candidates"][0]["asset_id"]
    confirmed = client.post(
        "/api/projects/project_owned/first-frames/confirm",
        json={"first_frame_asset_id": first_frame_asset_id},
        headers=headers("employee_1"),
    )
    assert confirmed.status_code == 200
    character = client.get(
        "/api/projects/project_owned/main-character",
        headers=headers("employee_1"),
    )
    reselection = client.put(
        "/api/projects/project_owned/main-character",
        json={"character_id": character.json()["character_id"]},
        headers=headers("employee_1"),
    )
    assert reselection.status_code == 200

    latest = client.get(
        "/api/projects/project_owned/first-frames/selection/latest",
        headers=headers("employee_1"),
    )

    assert latest.status_code == 409
    assert latest.json()["detail"]["code"] == "FIRST_FRAME_CANDIDATES_STALE"


def test_generation_uses_the_selected_character_snapshot_for_reference_order(
    client: TestClient,
    provider: RecordingImageProvider,
) -> None:
    prepare_inputs(client)
    character = client.get(
        "/api/projects/project_owned/main-character",
        headers=headers("employee_1"),
    )
    changed = client.patch(
        f"/api/characters/{character.json()['character_id']}",
        json={"reference_asset_ids": ["character_side"]},
        headers=headers("admin_1"),
    )
    assert changed.status_code == 200

    response = client.post(
        "/api/projects/project_owned/first-frames/generate",
        json={"quantity": 1},
        headers=headers("employee_1"),
    )

    assert response.status_code == 200
    assert provider.calls[0]["character_reference_images"] == [
        b"character-front",
        b"character-side",
    ]
