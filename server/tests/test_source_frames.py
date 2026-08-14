from __future__ import annotations

import shutil
import sqlite3
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.auth import get_database
from app.db import connect_database, initialize_database
from app.main import app
from app.media_routes import get_media_storage
from app.source_frame_routes import ExtractSourceFramesRequest, get_source_frame_extractor
from app.source_frames import (
    ExtractedSourceFrame,
    FFmpegSourceFrameExtractor,
    score_grayscale_frame,
)
from app.storage import FakeStorageAdapter


@dataclass(frozen=True)
class FakeSourceFrameExtractor:
    def extract(
        self,
        content: bytes,
        *,
        filename: str,
        timestamps_seconds: tuple[float, ...],
    ) -> list[ExtractedSourceFrame]:
        assert content == b"reference-video"
        assert filename == "reference.mp4"
        return [
            ExtractedSourceFrame(
                timestamp_seconds=timestamp,
                image=f"frame-{timestamp}".encode(),
                technical_score=round(timestamp / 3, 3),
            )
            for timestamp in timestamps_seconds
        ]


@dataclass(frozen=True)
class EmptySourceFrameExtractor:
    def extract(
        self,
        content: bytes,
        *,
        filename: str,
        timestamps_seconds: tuple[float, ...],
    ) -> list[ExtractedSourceFrame]:
        return [ExtractedSourceFrame(timestamp_seconds=0.5, image=b"")]


@pytest.fixture()
def db_path(tmp_path: Path) -> Iterator[Path]:
    path = tmp_path / "source-frames.db"
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
    app.dependency_overrides[get_source_frame_extractor] = FakeSourceFrameExtractor
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
    conn.execute(
        """
        INSERT INTO assets (
            id, project_id, kind, storage_uri, sha256, size_bytes, content_type,
            created_by_user_id
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "reference_owned",
            "project_owned",
            "reference_video",
            "fake://private-bucket/projects/project_owned/uploads/reference_owned/reference.mp4",
            "reference-hash",
            len(b"reference-video"),
            "video/mp4",
            "employee_1",
        ),
    )
    conn.commit()


def auth_headers(user_id: str) -> dict[str, str]:
    return {"X-Dev-User-Id": user_id}


def test_owner_can_extract_candidates_and_confirm_one(
    client: TestClient,
    db_path: Path,
    storage: FakeStorageAdapter,
) -> None:
    storage.put_object(
        "projects/project_owned/uploads/reference_owned/reference.mp4",
        b"reference-video",
        content_type="video/mp4",
    )

    extracted = client.post(
        "/api/projects/project_owned/source-frames/extract",
        json={"asset_id": "reference_owned"},
        headers=auth_headers("employee_1"),
    )

    assert extracted.status_code == 200
    body = extracted.json()
    assert body["kind"] == "source_frame_candidates"
    assert body["version_number"] == 1
    candidates = body["payload"]["candidates"]
    assert [candidate["timestamp_seconds"] for candidate in candidates] == [2.5, 1.5, 0.5]
    assert [candidate["score"] for candidate in candidates] == [0.833, 0.5, 0.167]
    assert all(candidate["asset_id"] for candidate in candidates)
    first_asset = candidates[0]["asset_id"]
    stored = storage.head_object(f"projects/project_owned/source-frames/{first_asset}.jpg")
    assert stored is not None
    assert stored.content_type == "image/jpeg"

    confirmed = client.post(
        "/api/projects/project_owned/source-frames/confirm",
        json={"source_frame_asset_id": candidates[1]["asset_id"]},
        headers=auth_headers("employee_1"),
    )
    latest = client.get(
        "/api/projects/project_owned/source-frames/selection/latest",
        headers=auth_headers("employee_1"),
    )

    assert confirmed.status_code == 200
    assert confirmed.json()["payload"]["source_frame_asset_id"] == candidates[1]["asset_id"]
    assert latest.status_code == 200
    assert latest.json()["id"] == confirmed.json()["id"]
    with connect_database(db_path) as conn:
        asset = conn.execute(
            "SELECT kind, content_type FROM assets WHERE id = ?",
            (candidates[1]["asset_id"],),
        ).fetchone()
    assert asset is not None
    assert asset["kind"] == "source_frame"
    assert asset["content_type"] == "image/jpeg"


def test_source_frame_extraction_requires_owner_and_ready_reference(
    client: TestClient,
    storage: FakeStorageAdapter,
) -> None:
    storage.put_object(
        "projects/project_owned/uploads/reference_owned/reference.mp4",
        b"reference-video",
        content_type="video/mp4",
    )

    forbidden = client.post(
        "/api/projects/project_owned/source-frames/extract",
        json={"asset_id": "reference_owned"},
        headers=auth_headers("employee_2"),
    )

    assert forbidden.status_code == 403
    assert forbidden.json()["detail"]["code"] == "PROJECT_FORBIDDEN"


def test_confirmation_rejects_assets_outside_the_latest_candidate_set(
    client: TestClient,
    storage: FakeStorageAdapter,
) -> None:
    storage.put_object(
        "projects/project_owned/uploads/reference_owned/reference.mp4",
        b"reference-video",
        content_type="video/mp4",
    )
    extracted = client.post(
        "/api/projects/project_owned/source-frames/extract",
        json={"asset_id": "reference_owned"},
        headers=auth_headers("employee_1"),
    )
    assert extracted.status_code == 200

    response = client.post(
        "/api/projects/project_owned/source-frames/confirm",
        json={"source_frame_asset_id": "not-a-candidate"},
        headers=auth_headers("employee_1"),
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "SOURCE_FRAME_CANDIDATE_NOT_FOUND"


def test_reextracting_source_frames_makes_the_previous_selection_stale(
    client: TestClient,
    storage: FakeStorageAdapter,
) -> None:
    storage.put_object(
        "projects/project_owned/uploads/reference_owned/reference.mp4",
        b"reference-video",
        content_type="video/mp4",
    )
    first = client.post(
        "/api/projects/project_owned/source-frames/extract",
        json={"asset_id": "reference_owned"},
        headers=auth_headers("employee_1"),
    )
    assert first.status_code == 200
    confirmed = client.post(
        "/api/projects/project_owned/source-frames/confirm",
        json={"source_frame_asset_id": first.json()["payload"]["candidates"][0]["asset_id"]},
        headers=auth_headers("employee_1"),
    )
    assert confirmed.status_code == 200

    second = client.post(
        "/api/projects/project_owned/source-frames/extract",
        json={"asset_id": "reference_owned"},
        headers=auth_headers("employee_1"),
    )
    selection = client.get(
        "/api/projects/project_owned/source-frames/selection/latest",
        headers=auth_headers("employee_1"),
    )

    assert second.status_code == 200
    assert second.json()["id"] != first.json()["id"]
    assert selection.status_code == 409
    assert selection.json()["detail"]["code"] == "SOURCE_FRAME_SELECTION_STALE"


def test_owner_can_reextract_manually_selected_timestamps_within_first_three_seconds(
    client: TestClient,
    storage: FakeStorageAdapter,
) -> None:
    storage.put_object(
        "projects/project_owned/uploads/reference_owned/reference.mp4",
        b"reference-video",
        content_type="video/mp4",
    )

    response = client.post(
        "/api/projects/project_owned/source-frames/extract",
        json={"asset_id": "reference_owned", "timestamps_seconds": [0.2, 2.8]},
        headers=auth_headers("employee_1"),
    )

    assert response.status_code == 200
    payload = response.json()["payload"]
    assert payload["requested_timestamps_seconds"] == [0.2, 2.8]
    assert [candidate["timestamp_seconds"] for candidate in payload["candidates"]] == [2.8, 0.2]


def test_manual_source_frame_timestamp_is_limited_to_the_first_three_seconds(
    client: TestClient,
) -> None:
    response = client.post(
        "/api/projects/project_owned/source-frames/extract",
        json={"asset_id": "reference_owned", "timestamps_seconds": [3.1]},
        headers=auth_headers("employee_1"),
    )

    assert response.status_code == 422


@pytest.mark.parametrize("invalid_value", [float("nan"), float("inf"), float("-inf")])
def test_manual_source_frame_timestamp_rejects_non_finite_values(
    invalid_value: float,
) -> None:
    with pytest.raises(ValidationError):
        ExtractSourceFramesRequest.model_validate(
            {"asset_id": "reference_owned", "timestamps_seconds": [invalid_value]}
        )


@pytest.mark.parametrize("timestamps", [[-0.1], [1.0, 1.0]])
def test_manual_source_frame_timestamp_rejects_invalid_ranges_or_duplicates(
    client: TestClient,
    timestamps: list[float],
) -> None:
    response = client.post(
        "/api/projects/project_owned/source-frames/extract",
        json={"asset_id": "reference_owned", "timestamps_seconds": timestamps},
        headers=auth_headers("employee_1"),
    )

    assert response.status_code == 422


def test_technical_frame_score_prefers_high_detail_over_a_flat_frame() -> None:
    flat_score = score_grayscale_frame(bytes([128]) * 64)
    detailed_score = score_grayscale_frame(bytes([0, 255]) * 32)

    assert 0 <= flat_score <= 1
    assert 0 <= detailed_score <= 1
    assert detailed_score > flat_score


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is required")
def test_ffmpeg_extractor_returns_scored_jpegs_for_requested_timestamps(tmp_path: Path) -> None:
    video_path = tmp_path / "reference.mp4"
    created = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=160x90:rate=10",
            "-t",
            "4",
            "-c:v",
            "mpeg4",
            str(video_path),
        ],
        capture_output=True,
        check=False,
        timeout=15,
    )
    assert created.returncode == 0

    frames = FFmpegSourceFrameExtractor().extract(
        video_path.read_bytes(),
        filename=video_path.name,
        timestamps_seconds=(0.2, 1.5, 2.8),
    )

    assert [frame.timestamp_seconds for frame in frames] == [0.2, 1.5, 2.8]
    assert all(frame.image.startswith(b"\xff\xd8") for frame in frames)
    assert all(frame.technical_score is not None for frame in frames)
    assert all(
        frame.technical_score is not None and 0 <= frame.technical_score <= 1 for frame in frames
    )


def test_empty_extracted_frame_is_reported_as_an_extraction_failure(
    client: TestClient,
    storage: FakeStorageAdapter,
) -> None:
    storage.put_object(
        "projects/project_owned/uploads/reference_owned/reference.mp4",
        b"reference-video",
        content_type="video/mp4",
    )
    app.dependency_overrides[get_source_frame_extractor] = EmptySourceFrameExtractor

    response = client.post(
        "/api/projects/project_owned/source-frames/extract",
        json={"asset_id": "reference_owned"},
        headers=auth_headers("employee_1"),
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "SOURCE_FRAME_EXTRACTION_FAILED"


def test_database_failure_removes_uploaded_candidate_frames(
    client: TestClient,
    storage: FakeStorageAdapter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage.put_object(
        "projects/project_owned/uploads/reference_owned/reference.mp4",
        b"reference-video",
        content_type="video/mp4",
    )

    def fail_insert_version(*args: object, **kwargs: object) -> sqlite3.Row:
        raise sqlite3.IntegrityError("simulated persistence failure")

    monkeypatch.setattr("app.source_frames.insert_version", fail_insert_version)
    response = client.post(
        "/api/projects/project_owned/source-frames/extract",
        json={"asset_id": "reference_owned"},
        headers=auth_headers("employee_1"),
    )

    assert response.status_code == 500
    assert response.json()["detail"]["code"] == "SOURCE_FRAME_PERSIST_FAILED"
    assert not [key for key in storage._objects if "/source-frames/" in key]
