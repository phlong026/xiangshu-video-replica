from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.auth import get_database
from app.db import connect_database, initialize_database
from app.db_portable import BusinessConnection
from app.first_frame_routes import get_image_provider
from app.first_frames import (
    FIRST_FRAME_NO_TEXT_CONSTRAINT,
    ApilioFirstFrameQualityInspector,
    FirstFrameCandidateInspection,
    FirstFrameCharacterInputs,
    FirstFrameQualityInspectorFailed,
    FirstFrameSourceInspection,
    GeneratedImage,
    ImageInput,
    RetryableImageProviderFailed,
    apply_selected_scene_look,
    bounded_source_frame_quality_inspector,
    derive_project_appearance_spec,
    normalize_prompt,
)
from app.generation_worker import run_worker_once
from app.image_tasks import (
    acquire_first_frame_task,
    complete_first_frame_task,
    prepare_first_frame_task,
    record_image_task_provider,
    renew_image_task_lease,
    run_first_frame_task_outside_transaction,
)
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


def test_source_frame_quality_wrapper_uses_one_bounded_attempt() -> None:
    inspector = ApilioFirstFrameQualityInspector(api_key="test-key")

    bounded = bounded_source_frame_quality_inspector(inspector)

    assert isinstance(bounded, ApilioFirstFrameQualityInspector)
    assert bounded.max_attempts == 1
    assert getattr(bounded.transport, "timeout_seconds") == 8.0


@dataclass
class SequenceFirstFrameQualityInspector:
    candidate_inspections: list[FirstFrameCandidateInspection]
    source_person_count: int = 1
    source_calls: int = 0
    candidate_calls: int = 0

    def inspect_source(self, source_image: ImageInput) -> FirstFrameSourceInspection:
        assert source_image.content
        self.source_calls += 1
        return FirstFrameSourceInspection(
            person_count=self.source_person_count,
            notes=[],
            provider="fake-first-frame-quality",
            model="fake-first-frame-quality-v1",
        )

    def inspect_candidate(
        self,
        *,
        source_image: ImageInput,
        character_reference_images: list[ImageInput],
        candidate: GeneratedImage,
        expected_outfit: str,
    ) -> FirstFrameCandidateInspection:
        assert source_image.content
        assert character_reference_images
        assert candidate.content
        assert expected_outfit
        self.candidate_calls += 1
        return self.candidate_inspections.pop(0)


@dataclass
class UnavailableCandidateQualityInspector:
    candidate_calls: int = 0

    def inspect_source(self, source_image: ImageInput) -> FirstFrameSourceInspection:
        assert source_image.content
        return FirstFrameSourceInspection(
            person_count=1,
            notes=[],
            provider="fake-first-frame-quality",
            model="fake-first-frame-quality-v1",
        )

    def inspect_candidate(
        self,
        *,
        source_image: ImageInput,
        character_reference_images: list[ImageInput],
        candidate: GeneratedImage,
        expected_outfit: str,
    ) -> FirstFrameCandidateInspection:
        assert source_image.content
        assert character_reference_images
        assert candidate.content
        assert expected_outfit
        self.candidate_calls += 1
        raise FirstFrameQualityInspectorFailed("quality service unavailable")


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


def test_contact_sheet_prompt_keeps_server_template_before_user_addition() -> None:
    result = normalize_prompt(
        "让人物手里拿一把红色雨伞",
        character_name="林夏",
        reference_roles=["contact_sheet", "source"],
    )

    assert "第 1 张输入图是原视频源帧" in result
    assert "第 2 张输入图是该角色的五视图参考板" in result
    assert "用户补充要求：\n让人物手里拿一把红色雨伞" in result
    assert result.endswith(FIRST_FRAME_NO_TEXT_CONSTRAINT)


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
            ("employee_2", "employee_2", "Employee Two", "employee"),
            ("admin_1", "admin_1", "Admin One", "admin"),
            ("auditor_1", "auditor_1", "Auditor One", "auditor"),
        ],
    )
    conn.executemany(
        "INSERT INTO projects (id, owner_user_id, name) VALUES (?, ?, ?)",
        [
            ("project_owned", "employee_1", "Owned Project"),
            ("project_other", "employee_2", "Other Project"),
        ],
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


def extract_source_frames(
    client: TestClient,
    *,
    timestamps_seconds: list[float] | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {"asset_id": "reference_owned"}
    if timestamps_seconds is not None:
        payload["timestamps_seconds"] = timestamps_seconds
    enqueued = client.post(
        "/api/projects/project_owned/source-frames/extract",
        json=payload,
        headers=headers("employee_1"),
    )
    assert enqueued.status_code == 202

    storage_override = client.app.dependency_overrides[get_media_storage]
    task_storage = storage_override()
    with BusinessConnection.sqlite(
        connect_database(Path(os.environ["VIDEO_REPLICA_DB_PATH"]))
    ) as conn:
        assert (
            run_worker_once(
                conn,
                worker_id="source-frame-setup-worker",
                storage=task_storage,
                source_frame_extractor=FakeSourceFrameExtractor(),
                max_tasks=1,
            )
            == 1
        )

    latest = client.get(
        "/api/projects/project_owned/source-frames/latest",
        headers=headers("employee_1"),
    )
    assert latest.status_code == 200
    return latest.json()


def prepare_inputs(client: TestClient) -> str:
    extracted = extract_source_frames(client)
    source_frame_asset_id = extracted["payload"]["candidates"][0]["asset_id"]
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


def test_first_frame_task_is_idempotent_and_worker_publishes_result(
    client: TestClient,
    db_path: Path,
    provider: RecordingImageProvider,
    storage: FakeStorageAdapter,
) -> None:
    prepare_inputs(client)
    request = {
        "model": "nano-banana-pro-2k",
        "quantity": 1,
        "idempotency_key": "first-frame-task-1",
    }
    first = client.post(
        "/api/projects/project_owned/first-frame-tasks",
        json=request,
        headers=headers("employee_1"),
    )
    replay = client.post(
        "/api/projects/project_owned/first-frame-tasks",
        json=request,
        headers=headers("employee_1"),
    )

    assert first.status_code == 202
    assert first.json()["stage"] == "QUEUED"
    assert replay.status_code == 202
    assert replay.json()["id"] == first.json()["id"]
    with BusinessConnection.sqlite(connect_database(db_path)) as conn:
        assert (
            run_worker_once(
                conn,
                worker_id="image-worker-test",
                storage=storage,
                first_frame_storage=storage,
                image_provider=provider,
                max_tasks=1,
            )
            == 1
        )

    task = client.get(
        f"/api/first-frame-tasks/{first.json()['id']}",
        headers=headers("employee_1"),
    )
    assert task.status_code == 200
    assert task.json()["status"] == "SUCCEEDED"
    assert task.json()["stage"] == "SUCCEEDED"
    assert task.json()["result_version_id"]
    latest = client.get(
        "/api/projects/project_owned/first-frames/latest",
        headers=headers("employee_1"),
    )
    assert latest.status_code == 200
    assert latest.json()["id"] == task.json()["result_version_id"]


def test_first_frame_write_role_gate_and_customer_access(
    client: TestClient,
    db_path: Path,
) -> None:
    prepare_inputs(client)
    request = {
        "model": "nano-banana-pro-2k",
        "quantity": 1,
        "idempotency_key": "first-frame-role-gate",
    }

    auditor_generate = client.post(
        "/api/projects/project_owned/first-frame-tasks",
        json=request,
        headers=headers("auditor_1"),
    )
    auditor_confirm = client.post(
        "/api/projects/project_owned/first-frames/confirm",
        json={"first_frame_asset_id": "missing-candidate"},
        headers=headers("auditor_1"),
    )

    assert auditor_generate.status_code == 403
    assert auditor_generate.json()["detail"]["code"] == "ROLE_FORBIDDEN"
    assert auditor_confirm.status_code == 403
    assert auditor_confirm.json()["detail"]["code"] == "ROLE_FORBIDDEN"
    with BusinessConnection.sqlite(connect_database(db_path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM first_frame_tasks").fetchone()[0] == 0
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM versions WHERE kind = 'first_frame_selection'"
            ).fetchone()[0]
            == 0
        )
        conn.execute("UPDATE users SET role = 'customer' WHERE id = 'employee_1'")
        conn.commit()

    customer_generate = client.post(
        "/api/projects/project_owned/first-frame-tasks",
        json=request,
        headers=headers("employee_1"),
    )
    assert customer_generate.status_code == 202, customer_generate.text


def test_first_frame_task_exposes_real_worker_stage(
    client: TestClient,
    db_path: Path,
) -> None:
    prepare_inputs(client)
    created = client.post(
        "/api/projects/project_owned/first-frame-tasks",
        json={
            "model": "gpt-image-2",
            "quantity": 1,
            "idempotency_key": "first-frame-stage-1",
        },
        headers=headers("employee_1"),
    )
    assert created.status_code == 202
    task_id = created.json()["id"]

    with BusinessConnection.sqlite(connect_database(db_path)) as conn:
        lease = acquire_first_frame_task(conn, worker_id="image-worker-stage-test")
        assert lease is not None

    preparing = client.get(
        f"/api/first-frame-tasks/{task_id}",
        headers=headers("employee_1"),
    )
    assert preparing.json()["stage"] == "PREPARING"

    with BusinessConnection.sqlite(connect_database(db_path)) as conn:
        record_image_task_provider(
            conn,
            table="first_frame_tasks",
            lease=lease,
            provider="fake",
            model="gpt-image-2",
        )

    generating = client.get(
        f"/api/first-frame-tasks/{task_id}",
        headers=headers("employee_1"),
    )
    assert generating.json()["stage"] == "GENERATING"

    with BusinessConnection.sqlite(connect_database(db_path)) as conn:
        conn.execute(
            "UPDATE first_frame_tasks SET result_json = %s WHERE id = %s",
            (
                json.dumps(
                    {
                        "execution": {"provider": "fake", "model": "gpt-image-2"},
                        "checkpoint": {"schema_version": 1, "candidates": []},
                    }
                ),
                task_id,
            ),
        )
        conn.commit()

    verifying = client.get(
        f"/api/first-frame-tasks/{task_id}",
        headers=headers("employee_1"),
    )
    assert verifying.json()["stage"] == "VERIFYING"


def test_first_frame_idempotent_replay_requires_project_access(client: TestClient) -> None:
    prepare_inputs(client)
    request = {
        "model": "nano-banana-pro-2k",
        "quantity": 1,
        "idempotency_key": "first-frame-foreign-replay-1",
    }
    created = client.post(
        "/api/projects/project_owned/first-frame-tasks",
        json=request,
        headers=headers("employee_1"),
    )
    replay = client.post(
        "/api/projects/project_owned/first-frame-tasks",
        json=request,
        headers=headers("employee_2"),
    )
    missing = client.post(
        "/api/projects/project_missing/first-frame-tasks",
        json=request,
        headers=headers("employee_2"),
    )

    assert created.status_code == 202
    assert replay.status_code == 404
    assert replay.content == missing.content


def test_first_frame_task_replay_survives_input_change_but_worker_fails_closed(
    client: TestClient,
    db_path: Path,
    provider: RecordingImageProvider,
    storage: FakeStorageAdapter,
) -> None:
    prepare_inputs(client)
    request = {
        "model": "nano-banana-pro-2k",
        "quantity": 1,
        "idempotency_key": "first-frame-task-stale-1",
    }
    first = client.post(
        "/api/projects/project_owned/first-frame-tasks",
        json=request,
        headers=headers("employee_1"),
    )
    assert first.status_code == 202

    extract_source_frames(client, timestamps_seconds=[0.6])

    replay = client.post(
        "/api/projects/project_owned/first-frame-tasks",
        json=request,
        headers=headers("employee_1"),
    )
    assert replay.status_code == 202
    assert replay.json()["id"] == first.json()["id"]

    with BusinessConnection.sqlite(connect_database(db_path)) as conn:
        assert (
            run_worker_once(
                conn,
                worker_id="image-worker-stale-test",
                storage=storage,
                first_frame_storage=storage,
                image_provider=provider,
                max_tasks=1,
            )
            == 1
        )

    task = client.get(
        f"/api/first-frame-tasks/{first.json()['id']}",
        headers=headers("employee_1"),
    )
    assert task.status_code == 200
    assert task.json()["status"] == "FAILED"
    assert task.json()["error_code"] == "FIRST_FRAME_TASK_INPUTS_CHANGED"
    assert provider.calls == []


def test_first_frame_task_storage_read_failure_is_retryable_before_provider_submit(
    client: TestClient,
    db_path: Path,
    provider: RecordingImageProvider,
    storage: FakeStorageAdapter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepare_inputs(client)
    created = client.post(
        "/api/projects/project_owned/first-frame-tasks",
        json={
            "model": "nano-banana-pro-2k",
            "quantity": 1,
            "idempotency_key": "first-frame-storage-down-1",
        },
        headers=headers("employee_1"),
    )
    assert created.status_code == 202

    def fail_storage_read(_key: str) -> bytes:
        raise OSError("storage unavailable")

    monkeypatch.setattr(storage, "get_object", fail_storage_read)
    with BusinessConnection.sqlite(connect_database(db_path)) as conn:
        assert (
            run_worker_once(
                conn,
                worker_id="image-worker-storage-test",
                storage=storage,
                first_frame_storage=storage,
                image_provider=provider,
                max_tasks=1,
            )
            == 1
        )

    task = client.get(
        f"/api/first-frame-tasks/{created.json()['id']}",
        headers=headers("employee_1"),
    )
    assert task.status_code == 200
    assert task.json()["status"] == "FAILED"
    assert task.json()["error_code"] == "FIRST_FRAME_INPUT_STORAGE_UNAVAILABLE"
    assert task.json()["retryable"] is True
    assert provider.calls == []


def test_first_frame_task_lease_loss_rolls_back_published_rows(
    client: TestClient,
    db_path: Path,
    provider: RecordingImageProvider,
    storage: FakeStorageAdapter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepare_inputs(client)
    created = client.post(
        "/api/projects/project_owned/first-frame-tasks",
        json={
            "model": "nano-banana-pro-2k",
            "quantity": 1,
            "idempotency_key": "first-frame-lease-race-1",
        },
        headers=headers("employee_1"),
    )
    assert created.status_code == 202

    with BusinessConnection.sqlite(connect_database(db_path)) as conn:
        lease = acquire_first_frame_task(conn, worker_id="image-worker-lease-test")
        assert lease is not None
        prepared = prepare_first_frame_task(conn, lease=lease, provider=provider)
        work, stored = run_first_frame_task_outside_transaction(
            prepared,
            storage=storage,
        )
        original_execute = conn.execute
        raced = False

        def race_lease(sql: str, params: tuple[object, ...] = ()):
            nonlocal raced
            if not raced and "INSERT INTO assets" in sql:
                raced = True
                original_execute(
                    "UPDATE first_frame_tasks SET locked_by = %s WHERE id = %s",
                    ("stolen-worker", lease.id),
                )
            return original_execute(sql, params)

        monkeypatch.setattr(conn, "execute", race_lease)
        with pytest.raises(RuntimeError, match="lease was lost"):
            complete_first_frame_task(
                conn,
                prepared=prepared,
                work=work,
                stored=stored,
            )

        task = original_execute(
            "SELECT locked_by, status FROM first_frame_tasks WHERE id = %s",
            (lease.id,),
        ).fetchone()
        assert task is not None
        assert task["locked_by"] == lease.worker_id
        assert task["status"] == "RUNNING"
        assert (
            original_execute(
                "SELECT COUNT(*) FROM versions WHERE kind = 'first_frame_candidates'"
            ).fetchone()[0]
            == 0
        )
        assert (
            original_execute("SELECT COUNT(*) FROM assets WHERE kind = 'first_frame'").fetchone()[0]
            == 0
        )


def test_first_frame_task_lease_can_be_renewed_during_long_quality_work(
    client: TestClient,
    db_path: Path,
) -> None:
    prepare_inputs(client)
    created = client.post(
        "/api/projects/project_owned/first-frame-tasks",
        json={
            "model": "nano-banana-pro-2k",
            "quantity": 1,
            "idempotency_key": "first-frame-renew-lease-1",
        },
        headers=headers("employee_1"),
    )
    assert created.status_code == 202

    with BusinessConnection.sqlite(connect_database(db_path)) as conn:
        lease = acquire_first_frame_task(conn, worker_id="image-worker-renew-test")
        assert lease is not None
        conn.execute(
            "UPDATE first_frame_tasks SET locked_until = datetime('now', '+1 second') "
            "WHERE id = %s",
            (lease.id,),
        )
        conn.commit()
        before = conn.execute(
            "SELECT locked_until FROM first_frame_tasks WHERE id = %s",
            (lease.id,),
        ).fetchone()
        assert before is not None

        renew_image_task_lease(
            conn,
            table="first_frame_tasks",
            lease=lease,
        )
        after = conn.execute(
            "SELECT locked_until FROM first_frame_tasks WHERE id = %s",
            (lease.id,),
        ).fetchone()

    assert after is not None
    assert str(after["locked_until"]) > str(before["locked_until"])


def test_final_retryable_provider_failure_stops_automatic_paid_retry(
    client: TestClient,
    db_path: Path,
    storage: FakeStorageAdapter,
) -> None:
    prepare_inputs(client)
    created = client.post(
        "/api/projects/project_owned/first-frame-tasks",
        json={
            "model": "nano-banana-pro-2k",
            "quantity": 1,
            "idempotency_key": "first-frame-provider-uncertain-1",
        },
        headers=headers("employee_1"),
    )
    assert created.status_code == 202

    with BusinessConnection.sqlite(connect_database(db_path)) as conn:
        assert (
            run_worker_once(
                conn,
                worker_id="image-worker-provider-uncertain",
                storage=storage,
                image_provider=FlakyImageProvider(failures_remaining=2),
                first_frame_quality_inspector=SequenceFirstFrameQualityInspector(
                    candidate_inspections=[]
                ),
                max_tasks=1,
            )
            == 1
        )

    task = client.get(
        f"/api/first-frame-tasks/{created.json()['id']}",
        headers=headers("employee_1"),
    )
    assert task.status_code == 200
    assert task.json()["status"] == "SUBMISSION_UNCERTAIN"
    assert task.json()["error_code"] == "IMAGE_TASK_SUBMISSION_UNCERTAIN"
    assert task.json()["retryable"] is False


def test_first_frame_task_publishes_rejected_candidates_with_quality_labels(
    client: TestClient,
    db_path: Path,
    provider: RecordingImageProvider,
    storage: FakeStorageAdapter,
) -> None:
    prepare_inputs(client)
    created = client.post(
        "/api/projects/project_owned/first-frame-tasks",
        json={
            "model": "gpt-image-2",
            "quantity": 1,
            "idempotency_key": "first-frame-quality-labels-1",
        },
        headers=headers("employee_1"),
    )
    assert created.status_code == 202
    rejected = passing_candidate_inspection().model_copy(
        update={
            "full_person_reconstruction_score": 0.2,
            "head_only_replacement_detected": True,
            "original_body_retained": True,
        }
    )

    with BusinessConnection.sqlite(connect_database(db_path)) as conn:
        assert (
            run_worker_once(
                conn,
                worker_id="image-worker-quality-labels",
                storage=storage,
                first_frame_storage=storage,
                image_provider=provider,
                first_frame_quality_inspector=SequenceFirstFrameQualityInspector(
                    candidate_inspections=[rejected, rejected]
                ),
                max_tasks=1,
            )
            == 1
        )
        task = conn.execute(
            "SELECT status, error_code, result_version_id FROM first_frame_tasks WHERE id = %s",
            (created.json()["id"],),
        ).fetchone()
        assert task is not None
        assert task["status"] == "SUCCEEDED"
        assert task["result_version_id"] is not None
        version = conn.execute(
            "SELECT payload_json FROM versions WHERE id = %s",
            (str(task["result_version_id"]),),
        ).fetchone()

    assert version is not None
    candidates = json.loads(str(version["payload_json"]))["candidates"]
    assert len(candidates) == 2
    assert all(candidate["quality"]["passed"] is False for candidate in candidates)
    assert all(
        "HEAD_ONLY_REPLACEMENT" in candidate["quality"]["issue_codes"] for candidate in candidates
    )
    assert len(provider.calls) == 2


def test_first_frame_task_reuses_checkpoint_after_quality_service_recovers(
    client: TestClient,
    db_path: Path,
    provider: RecordingImageProvider,
    storage: FakeStorageAdapter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import generation_worker as worker

    generated_counts: list[int] = []
    original_generate = worker.run_first_frame_task_outside_transaction

    def record_actual_images(*args, **kwargs):
        return original_generate(*args, **kwargs, on_generated_images=generated_counts.append)

    monkeypatch.setattr(worker, "run_first_frame_task_outside_transaction", record_actual_images)
    prepare_inputs(client)
    created = client.post(
        "/api/projects/project_owned/first-frame-tasks",
        json={
            "model": "gpt-image-2",
            "quantity": 1,
            "idempotency_key": "first-frame-checkpoint-resume-1",
        },
        headers=headers("employee_1"),
    )
    assert created.status_code == 202
    unavailable = UnavailableCandidateQualityInspector()

    with BusinessConnection.sqlite(connect_database(db_path)) as conn:
        assert (
            run_worker_once(
                conn,
                worker_id="image-worker-checkpoint-first",
                storage=storage,
                first_frame_storage=storage,
                image_provider=provider,
                first_frame_quality_inspector=unavailable,
                max_tasks=1,
            )
            == 1
        )
        interrupted = conn.execute(
            "SELECT status, result_json FROM first_frame_tasks WHERE id = %s",
            (created.json()["id"],),
        ).fetchone()
        assert interrupted is not None
        assert interrupted["status"] == "PENDING"
        interrupted_payload = json.loads(str(interrupted["result_json"]))
        assert interrupted_payload["checkpoint"]["candidates"]
        assert interrupted_payload["execution"] == {
            "provider": "fake",
            "model": "gpt-image-2",
        }
        conn.execute(
            """
            UPDATE first_frame_tasks
            SET status = 'RUNNING', locked_by = 'stopped-worker',
                locked_until = datetime('now', '-1 minute')
            WHERE id = %s
            """,
            (created.json()["id"],),
        )
        conn.commit()

        assert (
            run_worker_once(
                conn,
                worker_id="image-worker-checkpoint-second",
                storage=storage,
                first_frame_storage=storage,
                image_provider=provider,
                first_frame_quality_inspector=SequenceFirstFrameQualityInspector(
                    candidate_inspections=[passing_candidate_inspection()]
                ),
                max_tasks=1,
            )
            == 1
        )
        completed = conn.execute(
            "SELECT status, result_version_id FROM first_frame_tasks WHERE id = %s",
            (created.json()["id"],),
        ).fetchone()

    assert unavailable.candidate_calls == 1
    assert len(provider.calls) == 1
    assert generated_counts == [1], "checkpoint recovery must not report another paid image"
    assert completed is not None
    assert completed["status"] == "SUCCEEDED"
    assert completed["result_version_id"] is not None


def test_first_frame_checkpoint_rejects_a_different_provider(
    client: TestClient,
    db_path: Path,
    storage: FakeStorageAdapter,
) -> None:
    prepare_inputs(client)
    created = client.post(
        "/api/projects/project_owned/first-frame-tasks",
        json={
            "model": "gpt-image-2",
            "quantity": 1,
            "idempotency_key": "first-frame-checkpoint-provider-change-1",
        },
        headers=headers("employee_1"),
    )
    assert created.status_code == 202
    provider_a = RecordingImageProvider(provider_name="provider-a")
    provider_b = RecordingImageProvider(provider_name="provider-b")

    with BusinessConnection.sqlite(connect_database(db_path)) as conn:
        assert (
            run_worker_once(
                conn,
                worker_id="image-worker-provider-a",
                storage=storage,
                first_frame_storage=storage,
                image_provider=provider_a,
                first_frame_quality_inspector=UnavailableCandidateQualityInspector(),
                max_tasks=1,
            )
            == 1
        )
        assert (
            run_worker_once(
                conn,
                worker_id="image-worker-provider-b",
                storage=storage,
                first_frame_storage=storage,
                image_provider=provider_b,
                first_frame_quality_inspector=SequenceFirstFrameQualityInspector(
                    candidate_inspections=[passing_candidate_inspection()]
                ),
                max_tasks=1,
            )
            == 1
        )
        task = conn.execute(
            "SELECT status, error_code, result_json FROM first_frame_tasks WHERE id = %s",
            (created.json()["id"],),
        ).fetchone()

    assert task is not None
    assert task["status"] == "FAILED"
    assert task["error_code"] == "IMAGE_TASK_PROVIDER_CHANGED"
    assert json.loads(str(task["result_json"]))["execution"]["provider"] == "provider-a"
    assert len(provider_a.calls) == 1
    assert provider_b.calls == []

    retried = client.post(
        "/api/projects/project_owned/first-frame-tasks",
        json={
            "model": "gpt-image-2",
            "quantity": 1,
            "idempotency_key": "first-frame-checkpoint-provider-change-2",
        },
        headers=headers("employee_1"),
    )
    assert retried.status_code == 202
    with BusinessConnection.sqlite(connect_database(db_path)) as conn:
        pending = conn.execute(
            "SELECT result_json FROM first_frame_tasks WHERE id = %s",
            (retried.json()["id"],),
        ).fetchone()
        assert pending is not None
        assert pending["result_json"] is None
        assert (
            run_worker_once(
                conn,
                worker_id="image-worker-provider-b-retry",
                storage=storage,
                first_frame_storage=storage,
                image_provider=provider_b,
                first_frame_quality_inspector=SequenceFirstFrameQualityInspector(
                    candidate_inspections=[passing_candidate_inspection()]
                ),
                max_tasks=1,
            )
            == 1
        )
        retried_task = conn.execute(
            "SELECT status FROM first_frame_tasks WHERE id = %s",
            (retried.json()["id"],),
        ).fetchone()

    assert len(provider_b.calls) == 1
    assert retried_task is not None
    assert retried_task["status"] == "SUCCEEDED"


def test_new_first_frame_task_does_not_copy_legacy_checkpoint_without_execution(
    client: TestClient,
    db_path: Path,
) -> None:
    prepare_inputs(client)
    request = {
        "model": "gpt-image-2",
        "quantity": 1,
        "idempotency_key": "legacy-checkpoint-copy-1",
    }
    created = client.post(
        "/api/projects/project_owned/first-frame-tasks",
        json=request,
        headers=headers("employee_1"),
    )
    assert created.status_code == 202
    legacy_checkpoint = {"checkpoint": {"schema_version": 1, "candidates": [{"quality": None}]}}
    with BusinessConnection.sqlite(connect_database(db_path)) as conn:
        conn.execute(
            """
            UPDATE first_frame_tasks
            SET status = 'FAILED', result_json = %s
            WHERE id = %s
            """,
            (json.dumps(legacy_checkpoint), created.json()["id"]),
        )
        conn.commit()

    replacement = client.post(
        "/api/projects/project_owned/first-frame-tasks",
        json={**request, "idempotency_key": "legacy-checkpoint-copy-2"},
        headers=headers("employee_1"),
    )

    assert replacement.status_code == 202
    assert replacement.json()["id"] != created.json()["id"]
    with BusinessConnection.sqlite(connect_database(db_path)) as conn:
        row = conn.execute(
            "SELECT result_json FROM first_frame_tasks WHERE id = %s",
            (replacement.json()["id"],),
        ).fetchone()
    assert row is not None
    assert row["result_json"] is None


def test_expired_first_frame_task_does_not_resume_legacy_checkpoint_without_execution(
    client: TestClient,
    db_path: Path,
) -> None:
    prepare_inputs(client)
    created = client.post(
        "/api/projects/project_owned/first-frame-tasks",
        json={
            "model": "gpt-image-2",
            "quantity": 1,
            "idempotency_key": "legacy-checkpoint-expired-1",
        },
        headers=headers("employee_1"),
    )
    assert created.status_code == 202
    legacy_checkpoint = {"checkpoint": {"schema_version": 1, "candidates": [{"quality": None}]}}
    with BusinessConnection.sqlite(connect_database(db_path)) as conn:
        conn.execute(
            """
            UPDATE first_frame_tasks
            SET status = 'RUNNING', attempt = 1, locked_by = 'legacy-worker',
                locked_until = datetime('now', '-1 minute'), result_json = %s
            WHERE id = %s
            """,
            (json.dumps(legacy_checkpoint), created.json()["id"]),
        )
        conn.commit()

        lease = acquire_first_frame_task(conn, worker_id="replacement-worker")
        row = conn.execute(
            "SELECT status, error_code, result_json FROM first_frame_tasks WHERE id = %s",
            (created.json()["id"],),
        ).fetchone()

    assert lease is None
    assert row is not None
    assert row["status"] == "SUBMISSION_UNCERTAIN"
    assert row["error_code"] == "IMAGE_TASK_LEASE_EXPIRED"
    assert json.loads(str(row["result_json"])) == legacy_checkpoint


def test_acquired_first_frame_task_rejects_legacy_checkpoint_without_execution(
    client: TestClient,
    db_path: Path,
    storage: FakeStorageAdapter,
) -> None:
    prepare_inputs(client)
    created = client.post(
        "/api/projects/project_owned/first-frame-tasks",
        json={
            "model": "gpt-image-2",
            "quantity": 1,
            "idempotency_key": "legacy-checkpoint-acquired-1",
        },
        headers=headers("employee_1"),
    )
    assert created.status_code == 202
    legacy_checkpoint = {"checkpoint": {"schema_version": 1, "candidates": [{"quality": None}]}}
    provider = RecordingImageProvider(provider_name="current-provider")
    with BusinessConnection.sqlite(connect_database(db_path)) as conn:
        conn.execute(
            "UPDATE first_frame_tasks SET result_json = %s WHERE id = %s",
            (json.dumps(legacy_checkpoint), created.json()["id"]),
        )
        conn.commit()

        assert (
            run_worker_once(
                conn,
                worker_id="image-worker-legacy-checkpoint",
                storage=storage,
                first_frame_storage=storage,
                image_provider=provider,
                first_frame_quality_inspector=SequenceFirstFrameQualityInspector(
                    candidate_inspections=[passing_candidate_inspection()]
                ),
                max_tasks=1,
            )
            == 1
        )
        row = conn.execute(
            "SELECT status, error_code, result_json FROM first_frame_tasks WHERE id = %s",
            (created.json()["id"],),
        ).fetchone()

    assert provider.calls == []
    assert row is not None
    assert row["status"] == "FAILED"
    assert row["error_code"] == "IMAGE_TASK_EXECUTION_UNKNOWN"
    assert json.loads(str(row["result_json"])) == legacy_checkpoint


def test_new_first_frame_task_reuses_checkpoint_from_uncertain_task(
    client: TestClient,
    db_path: Path,
    provider: RecordingImageProvider,
    storage: FakeStorageAdapter,
) -> None:
    prepare_inputs(client)
    request = {
        "model": "gpt-image-2",
        "quantity": 1,
        "idempotency_key": "first-frame-checkpoint-uncertain-1",
    }
    created = client.post(
        "/api/projects/project_owned/first-frame-tasks",
        json=request,
        headers=headers("employee_1"),
    )
    assert created.status_code == 202

    with BusinessConnection.sqlite(connect_database(db_path)) as conn:
        conn.execute(
            "UPDATE first_frame_tasks SET attempt = 1 WHERE id = %s",
            (created.json()["id"],),
        )
        conn.commit()
        assert (
            run_worker_once(
                conn,
                worker_id="image-worker-checkpoint-uncertain",
                storage=storage,
                first_frame_storage=storage,
                image_provider=provider,
                first_frame_quality_inspector=UnavailableCandidateQualityInspector(),
                max_tasks=1,
            )
            == 1
        )
        uncertain = conn.execute(
            "SELECT status, result_json FROM first_frame_tasks WHERE id = %s",
            (created.json()["id"],),
        ).fetchone()

    assert uncertain is not None
    assert uncertain["status"] == "SUBMISSION_UNCERTAIN"
    assert json.loads(str(uncertain["result_json"]))["checkpoint"]["candidates"]

    resumed = client.post(
        "/api/projects/project_owned/first-frame-tasks",
        json={**request, "idempotency_key": "first-frame-checkpoint-uncertain-2"},
        headers=headers("employee_1"),
    )
    assert resumed.status_code == 202
    assert resumed.json()["id"] != created.json()["id"]

    with BusinessConnection.sqlite(connect_database(db_path)) as conn:
        assert (
            run_worker_once(
                conn,
                worker_id="image-worker-checkpoint-resubmitted",
                storage=storage,
                first_frame_storage=storage,
                image_provider=provider,
                first_frame_quality_inspector=SequenceFirstFrameQualityInspector(
                    candidate_inspections=[passing_candidate_inspection()]
                ),
                max_tasks=1,
            )
            == 1
        )
        completed = conn.execute(
            "SELECT status FROM first_frame_tasks WHERE id = %s",
            (resumed.json()["id"],),
        ).fetchone()

    assert len(provider.calls) == 1
    assert completed is not None
    assert completed["status"] == "SUCCEEDED"


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
    assert body["payload"]["reconstruction_mode"] == "full_person_replace.v1"
    assert body["payload"]["character_contract"] == {
        "body_reconstruction": True,
        "clothing_policy": "project_appearance_first",
        "identity_source": "legacy_views_only",
        "preserve_framing": True,
        "preserve_pose": True,
        "preserve_scene": True,
    }
    assert body["payload"]["project_appearance"]["category"] == "GENERAL"
    assert body["payload"]["project_character_appearance_version_id"]
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


def test_project_appearance_uses_the_selected_source_frame_scene() -> None:
    spec = derive_project_appearance_spec(
        analysis_payload={
            "theme": "工程项目业务介绍",
            "visual_style": "真实纪实",
            "shots": [
                {
                    "start_time": 0,
                    "end_time": 4,
                    "subject": "企业负责人",
                    "action": "在办公室介绍合作模式",
                    "scene": "商务办公室",
                },
                {
                    "start_time": 4,
                    "end_time": 12,
                    "subject": "项目负责人",
                    "action": "在施工现场边走边介绍工程进度",
                    "scene": "建筑施工现场",
                },
            ],
        },
        source_analysis_version_id="analysis-1",
        source_timestamp_seconds=8,
    )

    assert spec.category == "CONSTRUCTION"
    assert spec.scene == "建筑施工现场"
    assert "工装" in spec.outfit_description
    assert "施工现场" in spec.selection_reason


def test_project_appearance_uses_half_open_segment_boundaries() -> None:
    spec = derive_project_appearance_spec(
        analysis_payload={
            "shots": [
                {
                    "start_time": 0,
                    "end_time": 5,
                    "subject": "主讲人",
                    "action": "办公室口播",
                    "scene": "商务办公室",
                },
                {
                    "start_time": 5,
                    "end_time": 10,
                    "subject": "项目负责人",
                    "action": "查看施工进度",
                    "scene": "建筑施工现场",
                },
            ]
        },
        source_analysis_version_id="analysis-boundary",
        source_timestamp_seconds=5,
    )

    assert spec.scene == "建筑施工现场"
    assert spec.category == "CONSTRUCTION"


def test_selected_scene_look_is_the_authoritative_outfit_for_first_frame() -> None:
    automatic = derive_project_appearance_spec(
        analysis_payload={
            "shots": [
                {
                    "start_time": 0,
                    "end_time": 10,
                    "subject": "主讲人",
                    "action": "在会议室介绍业务",
                    "scene": "商务会议室",
                }
            ]
        },
        source_analysis_version_id="analysis-scene-look",
        source_timestamp_seconds=3,
    )
    character_inputs = FirstFrameCharacterInputs(
        main_character_version_id="main-character-v1",
        character_snapshot={
            "persona_snapshot_json": {
                "name": "工地造型",
                "scene_description": "乡村别墅施工现场，白天自然光",
                "costume_description": "黄色安全帽、深蓝色工装和反光背心",
                "appearance_constraints_json": {"appearance_type": "scene"},
            }
        },
        reference_asset_ids=["contact-sheet", "source-photo"],
        character_name="林夏",
        authorized_project_ids=[],
        character_version_id="scene-look-v1",
        reference_asset_roles=["contact_sheet", "source_photo"],
    )

    selected = apply_selected_scene_look(automatic, character_inputs=character_inputs)
    prompt = normalize_prompt(
        None,
        character_name="林夏",
        reference_roles=character_inputs.reference_asset_roles,
        project_appearance=selected,
    )

    assert selected.appearance_source == "SCENE_LOOK"
    assert selected.scene == "商务会议室"
    assert selected.scene_look_description == "乡村别墅施工现场，白天自然光"
    assert selected.outfit_description == "黄色安全帽、深蓝色工装和反光背心"
    assert "用户已选择的场景造型" in prompt
    assert "黄色安全帽、深蓝色工装和反光背心" in prompt
    assert "服装、鞋履与配饰必须以该参考板为准" in prompt
    assert "实际背景仍以原视频源帧为准" in prompt
    assert "不得直接照搬" not in prompt


def test_full_person_prompt_uses_project_appearance_instead_of_copying_reference_clothes() -> None:
    spec = derive_project_appearance_spec(
        analysis_payload={
            "theme": "企业客户合作",
            "visual_style": "写实",
            "shots": [
                {
                    "start_time": 0,
                    "end_time": 12,
                    "subject": "企业负责人",
                    "action": "向镜头介绍业务",
                    "scene": "商务会议室",
                }
            ],
        },
        source_analysis_version_id="analysis-1",
        source_timestamp_seconds=3,
    )

    prompt = normalize_prompt(
        None,
        character_name="林夏",
        reference_roles=["contact_sheet", "source_photo"],
        project_appearance=spec,
    )

    assert "完整重构原人物的头脸、发型、颈部、肤色、身形比例" in prompt
    assert "严禁只替换脸部" in prompt
    assert spec.outfit_description in prompt
    assert "参考图中的服装只用于理解人物体型，不得直接照搬" in prompt
    assert "如果画面中有多人，只重构这一名主要人物" in prompt
    assert "不得把目标人物外观扩散到旁人" in prompt


def passing_candidate_inspection() -> FirstFrameCandidateInspection:
    return FirstFrameCandidateInspection(
        person_count=1,
        identity_match_score=0.94,
        full_person_reconstruction_score=0.91,
        outfit_match_score=0.88,
        pose_preserved=True,
        framing_preserved=True,
        scene_preserved=True,
        anatomy_valid=True,
        head_only_replacement_detected=False,
        original_body_retained=False,
        text_detected=False,
        notes=[],
        provider="fake-first-frame-quality",
        model="fake-first-frame-quality-v1",
    )


def test_quality_gate_retries_a_head_only_result_before_publishing(
    client: TestClient,
    provider: RecordingImageProvider,
) -> None:
    from app.first_frame_routes import get_first_frame_quality_inspector

    rejected = passing_candidate_inspection().model_copy(
        update={
            "full_person_reconstruction_score": 0.2,
            "head_only_replacement_detected": True,
            "original_body_retained": True,
            "notes": ["仅头部发生变化，身体与服装仍属于原人物"],
        }
    )
    inspector = SequenceFirstFrameQualityInspector(
        candidate_inspections=[rejected, passing_candidate_inspection()]
    )
    app.dependency_overrides[get_first_frame_quality_inspector] = lambda: inspector
    prepare_inputs(client)

    response = client.post(
        "/api/projects/project_owned/first-frames/generate",
        json={"model": "gpt-image-2", "quantity": 1},
        headers=headers("employee_1"),
    )

    assert response.status_code == 200
    candidates = response.json()["payload"]["candidates"]
    assert len(candidates) == 2
    passed = [item for item in candidates if item["quality"]["passed"]]
    assert len(passed) == 1
    assert passed[0]["quality"]["attempt"] == 2
    assert len(provider.calls) == 2
    assert "自动质检未通过" in str(provider.calls[1]["prompt"])
    assert inspector.source_calls == 1
    assert inspector.candidate_calls == 2


def test_quality_gate_blocks_generation_when_selected_source_frame_has_multiple_people(
    client: TestClient,
    provider: RecordingImageProvider,
) -> None:
    from app.first_frame_routes import get_first_frame_quality_inspector

    inspector = SequenceFirstFrameQualityInspector(
        candidate_inspections=[],
        source_person_count=2,
    )
    app.dependency_overrides[get_first_frame_quality_inspector] = lambda: inspector
    prepare_inputs(client)

    response = client.post(
        "/api/projects/project_owned/first-frames/generate",
        json={"model": "gpt-image-2", "quantity": 1},
        headers=headers("employee_1"),
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "SINGLE_PERSON_SOURCE_REQUIRED"
    assert provider.calls == []


def test_customer_production_rejects_fake_first_frame_quality_override(
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.first_frame_routes import get_first_frame_quality_inspector

    monkeypatch.setenv("VIDEO_REPLICA_CUSTOMER_PRODUCTION", "true")
    monkeypatch.setenv("VIDEO_REPLICA_FAKE_FIRST_FRAME_QUALITY_INSPECTOR", "1")
    with BusinessConnection.sqlite(connect_database(db_path)) as conn:
        with pytest.raises(HTTPException) as error:
            get_first_frame_quality_inspector(conn)

    assert error.value.status_code == 503
    assert error.value.detail["code"] == "FAKE_FIRST_FRAME_QUALITY_FORBIDDEN"


def test_analysis_person_count_blocks_multi_person_video_before_paid_generation(
    client: TestClient,
    db_path: Path,
    provider: RecordingImageProvider,
) -> None:
    prepare_inputs(client)
    with BusinessConnection.sqlite(connect_database(db_path)) as conn:
        conn.execute(
            """
            INSERT INTO versions (
                id, project_id, asset_id, kind, version_number,
                payload_json, created_by_user_id
            ) VALUES (%s, %s, NULL, 'analysis', 1, %s, %s)
            """,
            (
                "analysis-multi-person",
                "project_owned",
                json.dumps(
                    {
                        "analysis": {
                            "theme": "双人访谈",
                            "visual_style": "写实",
                            "shots": [
                                {
                                    "start_time": 0,
                                    "end_time": 12,
                                    "subject": "两名访谈人物",
                                    "action": "面对面交谈",
                                    "scene": "办公室",
                                    "person_count": 2,
                                }
                            ],
                        }
                    }
                ),
                "employee_1",
            ),
        )
        conn.commit()

    response = client.post(
        "/api/projects/project_owned/first-frames/generate",
        json={"model": "gpt-image-2", "quantity": 1},
        headers=headers("employee_1"),
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "MULTI_PERSON_VIDEO_UNSUPPORTED"
    assert provider.calls == []


def test_legacy_analysis_without_person_count_requires_reanalysis_before_paid_generation(
    client: TestClient,
    db_path: Path,
    provider: RecordingImageProvider,
) -> None:
    prepare_inputs(client)
    with BusinessConnection.sqlite(connect_database(db_path)) as conn:
        conn.execute(
            """
            INSERT INTO versions (
                id, project_id, asset_id, kind, version_number,
                payload_json, created_by_user_id
            ) VALUES (%s, %s, NULL, 'analysis', 1, %s, %s)
            """,
            (
                "analysis-legacy-no-person-count",
                "project_owned",
                json.dumps(
                    {
                        "analysis": {
                            "shots": [
                                {
                                    "start_time": 0,
                                    "end_time": 12,
                                    "subject": "主讲人物",
                                    "action": "面对镜头讲话",
                                    "scene": "办公室",
                                }
                            ]
                        }
                    }
                ),
                "employee_1",
            ),
        )
        conn.commit()

    response = client.post(
        "/api/projects/project_owned/first-frames/generate",
        json={"model": "gpt-image-2", "quantity": 1},
        headers=headers("employee_1"),
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "VIDEO_ANALYSIS_UPGRADE_REQUIRED"
    assert provider.calls == []


def test_custom_prompt_cannot_bypass_the_no_text_constraint(
    client: TestClient,
    provider: RecordingImageProvider,
) -> None:
    prepare_inputs(client)

    response = client.post(
        "/api/projects/project_owned/first-frames/generate",
        json={
            "model": "gpt-image-2",
            "prompt": "保留源图中的标题和招牌文字。",
            "quantity": 1,
        },
        headers=headers("employee_1"),
    )

    assert response.status_code == 200
    effective_prompt = str(provider.calls[0]["prompt"])
    assert "保留源图中的标题和招牌文字。" in effective_prompt
    assert "硬性输出约束" in effective_prompt
    assert "最终首帧不得出现任何文字" in effective_prompt
    assert "标题、字幕、话题词、标签、招牌、门联、水印与 Logo" in effective_prompt
    assert "封面文字由后期添加" in effective_prompt
    assert effective_prompt.count("硬性输出约束") == 1
    assert effective_prompt.endswith("若其他指令与本约束冲突，一律以本约束为准。")


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


def test_uninspected_legacy_first_frame_candidate_cannot_be_confirmed(
    client: TestClient,
    db_path: Path,
) -> None:
    prepare_inputs(client)
    with BusinessConnection.sqlite(connect_database(db_path)) as conn:
        source_selection = conn.execute(
            "SELECT id FROM versions WHERE project_id = %s AND kind = 'source_frame_selection' "
            "ORDER BY version_number DESC LIMIT 1",
            ("project_owned",),
        ).fetchone()
        main_character = conn.execute(
            "SELECT id FROM versions WHERE project_id = %s AND kind = 'main_character' "
            "ORDER BY version_number DESC LIMIT 1",
            ("project_owned",),
        ).fetchone()
        assert source_selection is not None
        assert main_character is not None
        conn.execute(
            """
            INSERT INTO versions (
                id, project_id, asset_id, kind, version_number,
                payload_json, created_by_user_id
            ) VALUES (%s, %s, NULL, 'first_frame_candidates', 1, %s, %s)
            """,
            (
                "legacy-uninspected-candidates",
                "project_owned",
                json.dumps(
                    {
                        "source_frame_selection_version_id": str(source_selection["id"]),
                        "main_character_version_id": str(main_character["id"]),
                        "candidates": [{"asset_id": "character_front"}],
                    }
                ),
                "employee_1",
            ),
        )
        conn.commit()

    response = client.post(
        "/api/projects/project_owned/first-frames/confirm",
        json={"first_frame_asset_id": "character_front"},
        headers=headers("employee_1"),
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "FIRST_FRAME_QUALITY_NOT_VERIFIED"


def test_unverified_first_frame_confirmation_requires_explicit_override(
    client: TestClient,
    db_path: Path,
    provider: RecordingImageProvider,
    storage: FakeStorageAdapter,
) -> None:
    prepare_inputs(client)
    created = client.post(
        "/api/projects/project_owned/first-frame-tasks",
        json={
            "model": "gpt-image-2",
            "quantity": 1,
            "idempotency_key": "first-frame-override-confirm-1",
        },
        headers=headers("employee_1"),
    )
    assert created.status_code == 202
    rejected = passing_candidate_inspection().model_copy(update={"outfit_match_score": 0.1})

    with BusinessConnection.sqlite(connect_database(db_path)) as conn:
        assert (
            run_worker_once(
                conn,
                worker_id="image-worker-override-confirm",
                storage=storage,
                first_frame_storage=storage,
                image_provider=provider,
                first_frame_quality_inspector=SequenceFirstFrameQualityInspector(
                    candidate_inspections=[rejected, rejected]
                ),
                max_tasks=1,
            )
            == 1
        )
        task = conn.execute(
            "SELECT result_version_id FROM first_frame_tasks WHERE id = %s",
            (created.json()["id"],),
        ).fetchone()
        assert task is not None
        assert task["result_version_id"] is not None
        version = conn.execute(
            "SELECT payload_json FROM versions WHERE id = %s",
            (str(task["result_version_id"]),),
        ).fetchone()
    assert version is not None
    candidates = json.loads(str(version["payload_json"]))["candidates"]
    asset_id = str(candidates[0]["asset_id"])

    blocked = client.post(
        "/api/projects/project_owned/first-frames/confirm",
        json={"first_frame_asset_id": asset_id},
        headers=headers("employee_1"),
    )
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "FIRST_FRAME_QUALITY_NOT_VERIFIED"

    overridden = client.post(
        "/api/projects/project_owned/first-frames/confirm",
        json={"first_frame_asset_id": asset_id, "allow_unverified": True},
        headers=headers("employee_1"),
    )
    assert overridden.status_code == 200, overridden.text
    assert overridden.json()["payload"]["quality_override"] is True


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
    extracted_again = extract_source_frames(client)
    new_source_frame_asset_id = extracted_again["payload"]["candidates"][0]["asset_id"]
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


def test_new_analysis_invalidates_project_appearance_and_first_frame_candidates(
    client: TestClient,
    db_path: Path,
) -> None:
    prepare_inputs(client)
    generated = client.post(
        "/api/projects/project_owned/first-frames/generate",
        json={"model": "gpt-image-2", "quantity": 1},
        headers=headers("employee_1"),
    )
    assert generated.status_code == 200

    with BusinessConnection.sqlite(connect_database(db_path)) as conn:
        conn.execute(
            """
            INSERT INTO versions (
                id, project_id, asset_id, kind, version_number,
                payload_json, created_by_user_id
            ) VALUES (%s, %s, NULL, 'analysis', 1, %s, %s)
            """,
            (
                "analysis-after-first-frame",
                "project_owned",
                json.dumps(
                    {
                        "analysis": {
                            "theme": "商务合作",
                            "visual_style": "写实",
                            "shots": [
                                {
                                    "start_time": 0,
                                    "end_time": 12,
                                    "subject": "企业负责人",
                                    "action": "介绍合作方案",
                                    "scene": "商务会议室",
                                }
                            ],
                        }
                    }
                ),
                "employee_1",
            ),
        )
        conn.commit()

    latest = client.get(
        "/api/projects/project_owned/first-frames/latest",
        headers=headers("employee_1"),
    )

    assert latest.status_code == 409
    assert latest.json()["detail"]["code"] == "FIRST_FRAME_CANDIDATES_STALE"


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
