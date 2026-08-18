from __future__ import annotations

import hashlib
import json
import socket
import sqlite3
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app.auth import get_database
from app.db import connect_database, initialize_database
from app.generation import (
    MAX_ARCHIVE_RETRIES,
    FakeH3Provider,
    H3CreateResult,
    H3ProviderFailed,
    H3ProviderSettingsUnavailable,
    MetasoH3Provider,
    SubmissionUncertain,
    build_h3_request,
    compile_prompt_text,
    generation_task_operation_hash,
    h3_provider_for_task,
    mark_expired_active_leases_needing_attention,
    mark_task_submission_uncertain,
    reconcile_submission_uncertain_task,
    run_next_generation_task,
)
from app.generation_routes import get_h3_provider
from app.generation_worker import run_worker_once
from app.main import app
from app.settings import SETTINGS_KEY_ENV, SettingsRepository
from app.storage import FakeStorageAdapter, StorageBackendUnavailable


def _fake_public_dns(hostname: str, port: int, type: int) -> list[tuple[object, ...]]:
    del hostname, type
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]


class RecordedMetasoTransport:
    def __init__(self, responses: list[bytes]) -> None:
        self.responses = responses
        self.requests: list[tuple[str, str, dict[str, str], bytes | None]] = []

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        body: bytes | None = None,
    ) -> bytes:
        self.requests.append((method, url, headers, body))
        return self.responses.pop(0)


@pytest.fixture()
def db_path(tmp_path: Path) -> Iterator[Path]:
    db_path = tmp_path / "generation.db"
    with initialize_database(db_path) as conn:
        seed_data(conn)
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
    app.dependency_overrides[get_h3_provider] = lambda: FakeH3Provider()
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
                "first_frame_owned",
                "project_owned",
                "image",
                "fake://generation-results/first-frame.png",
                "sha-first",
                12,
                "image/png",
                "employee_1",
            ),
            (
                "first_frame_other",
                "project_other",
                "image",
                "local://other-frame.png",
                "sha-other",
                12,
                "image/png",
                "employee_2",
            ),
        ],
    )
    conn.execute(
        """
        INSERT INTO versions (
            id,
            project_id,
            asset_id,
            kind,
            version_number,
            payload_json,
            created_by_user_id
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "shot_card_v1",
            "project_owned",
            None,
            "shot_card",
            1,
            json.dumps(
                {
                    "duration_seconds": 10,
                    "shots": [
                        {
                            "shot_id": "S01",
                            "start_time": 0,
                            "end_time": 5,
                            "shot_type": "近景",
                            "composition": "人物居中",
                            "camera_motion": "固定",
                            "subject": "主讲人",
                            "action": "看镜头口播",
                            "scene": "室内",
                            "spoken_text": "原始第一句",
                            "transition": "硬切",
                            "motion": {
                                "subject_motion_state": "GESTURING_ONLY",
                                "subject_direction": "in_place",
                                "subject_displacement": "无位移",
                                "hand_action": "双手做自然讲解手势",
                                "camera_motion": "STATIC",
                                "relative_motion": "固定机位，人物站位不变",
                            },
                        },
                        {
                            "shot_id": "S02",
                            "start_time": 5,
                            "end_time": 10,
                            "shot_type": "中景",
                            "composition": "三分法",
                            "camera_motion": "轻微推进",
                            "subject": "主讲人",
                            "action": "继续讲解",
                            "scene": "室内",
                            "spoken_text": "原始第二句",
                            "transition": "硬切",
                            "motion": {
                                "subject_motion_state": "GESTURING_ONLY",
                                "subject_direction": "in_place",
                                "subject_displacement": "无位移",
                                "hand_action": "双手做自然讲解手势",
                                "camera_motion": "PUSH_IN",
                                "relative_motion": "镜头缓慢推近，人物站位不变",
                            },
                        },
                    ],
                },
                ensure_ascii=True,
                sort_keys=True,
            ),
            "employee_1",
        ),
    )

    conn.executemany(
        """
        INSERT INTO versions (
            id, project_id, asset_id, kind, version_number, payload_json, created_by_user_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                "first_frame_candidates_v1",
                "project_owned",
                "first_frame_owned",
                "first_frame_candidates",
                1,
                json.dumps(
                    {"candidates": [{"asset_id": "first_frame_owned"}]},
                    ensure_ascii=True,
                    sort_keys=True,
                ),
                "employee_1",
            ),
            (
                "first_frame_selection_v1",
                "project_owned",
                "first_frame_owned",
                "first_frame_selection",
                1,
                json.dumps(
                    {
                        "first_frame_candidates_version_id": "first_frame_candidates_v1",
                        "first_frame_asset_id": "first_frame_owned",
                    },
                    ensure_ascii=True,
                    sort_keys=True,
                ),
                "employee_1",
            ),
        ],
    )
    conn.commit()


def test_metaso_h3_provider_creates_polls_filters_and_downloads_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.generation.socket.getaddrinfo", _fake_public_dns)
    provider_task_id = "task-real-1"
    result_url = "https://files.example.test/video.mp4?signature=test"
    transport = RecordedMetasoTransport(
        [
            json.dumps({"task_id": provider_task_id}).encode(),
            json.dumps(
                {
                    "items": [
                        {"id": "another-task", "status": "succeeded"},
                        {"id": provider_task_id, "status": "queued", "content": {}},
                    ],
                    "total": 2,
                }
            ).encode(),
            json.dumps(
                {
                    "items": [
                        {
                            "id": provider_task_id,
                            "status": "succeeded",
                            "content": {"url": result_url},
                        }
                    ],
                    "total": 1,
                }
            ).encode(),
            b"generated-video-bytes",
        ]
    )
    waits: list[float] = []
    provider = MetasoH3Provider(
        api_key="metaso-test-key",
        transport=transport,
        poll_interval_seconds=0.25,
        max_poll_attempts=2,
        sleeper=waits.append,
        audio_quality_checker=lambda _: ("AUDIO_QUALITY_FAILED", ["AUDIO_QUALITY_FAILED"]),
    )

    result = provider.create_image_to_video(
        build_h3_request(
            prompt_text="生成自然运动的视频",
            first_frame_url="https://storage.example.test/first-frame.png",
            duration_seconds=5,
            resolution="768P",
        )
    )

    assert result.provider_task_id == provider_task_id
    assert result.result_url == result_url
    assert result.result_content == b"generated-video-bytes"
    assert result.audio_quality_status == "AUDIO_QUALITY_FAILED"
    assert result.quality_issue_codes == ["AUDIO_QUALITY_FAILED"]
    assert waits == [0.25]
    create_method, create_url, create_headers, create_body = transport.requests[0]
    assert create_method == "POST"
    assert create_url == "https://metaso.cn/api/minimax/v2/video_generation"
    assert create_headers["Authorization"] == "Bearer metaso-test-key"
    assert json.loads(create_body or b"{}") == {
        "model": "MiniMax-H3",
        "content": [
            {"type": "text", "text": "生成自然运动的视频"},
            {
                "type": "image_url",
                "image_url": {"url": "https://storage.example.test/first-frame.png"},
                "role": "first_frame",
            },
        ],
        "resolution": "768P",
        "duration": 5,
        "ratio": "adaptive",
    }
    query_method, query_url, query_headers, query_body = transport.requests[1]
    assert query_method == "GET"
    assert query_url.endswith("/api/minimax/v2/query/video_generation?task_id=task-real-1")
    assert query_headers["Authorization"] == "Bearer metaso-test-key"
    assert query_body is None
    download_method, download_url, download_headers, download_body = transport.requests[-1]
    assert download_method == "GET"
    assert download_url == result_url
    assert "Authorization" not in download_headers
    assert download_body is None


def test_metaso_h3_provider_refuses_to_treat_an_unrelated_list_item_as_its_result() -> None:
    transport = RecordedMetasoTransport(
        [
            b'{"task_id":"task-real-1"}',
            b'{"items":[{"id":"another-task","status":"succeeded"}],"total":1}',
        ]
    )
    provider = MetasoH3Provider(
        api_key="metaso-test-key",
        transport=transport,
        poll_interval_seconds=0,
        max_poll_attempts=1,
    )

    with pytest.raises(H3ProviderFailed, match="did not return the created task"):
        provider.create_image_to_video(
            build_h3_request(
                prompt_text="生成自然运动的视频",
                first_frame_url="https://storage.example.test/first-frame.png",
                duration_seconds=5,
                resolution="768P",
            )
        )


def test_metaso_h3_provider_rejects_a_non_https_first_frame_url() -> None:
    transport = RecordedMetasoTransport([])
    provider = MetasoH3Provider(api_key="metaso-test-key", transport=transport)

    with pytest.raises(H3ProviderFailed, match="HTTPS first-frame URL"):
        provider.create_image_to_video(
            build_h3_request(
                prompt_text="生成自然运动的视频",
                first_frame_url="fake://private/first-frame.png",
                duration_seconds=5,
                resolution="768P",
            )
        )

    assert transport.requests == []


def auth_headers(user_id: str) -> dict[str, str]:
    return {"X-Dev-User-Id": user_id}


def create_locked_prompt(
    client: TestClient,
    *,
    script_text: str = "第一句完整意思。第二句也完整。",
) -> str:
    script = client.post(
        "/api/projects/project_owned/scripts",
        headers=auth_headers("employee_1"),
        json={
            "source": "custom",
            "text": script_text,
            "shot_card_version_id": "shot_card_v1",
        },
    )
    assert script.status_code == 200

    prompt = client.post(
        "/api/projects/project_owned/prompts/compile",
        headers=auth_headers("employee_1"),
        json={
            "script_version_id": script.json()["id"],
            "shot_card_version_id": "shot_card_v1",
            "first_frame_asset_id": "first_frame_owned",
            "output_duration_seconds": 10,
            "resolution": "768P",
        },
    )
    assert prompt.status_code == 200

    locked = client.post(
        f"/api/projects/project_owned/prompts/{prompt.json()['id']}/lock",
        headers=auth_headers("employee_1"),
    )
    assert locked.status_code == 200
    assert locked.json()["payload"]["status"] == "LOCKED"
    return str(locked.json()["id"])


def insert_next_shot_card_version(
    db_path: Path,
    *,
    version_id: str = "shot_card_v2",
) -> None:
    with connect_database(db_path) as conn:
        previous = conn.execute(
            "SELECT payload_json FROM versions WHERE id = 'shot_card_v1'"
        ).fetchone()
        assert previous is not None
        payload = json.loads(str(previous["payload_json"]))
        payload["source_analysis_version_id"] = "analysis_v2"
        conn.execute(
            """
            INSERT INTO versions (
                id, project_id, asset_id, kind, version_number, payload_json, created_by_user_id
            ) VALUES (?, 'project_owned', NULL, 'shot_card', 2, ?, 'employee_1')
            """,
            (version_id, json.dumps(payload, ensure_ascii=True, sort_keys=True)),
        )
        conn.commit()


def insert_generation_history(
    conn: sqlite3.Connection,
    *,
    batch_id: str,
    project_id: str = "project_owned",
    created_by_user_id: str = "employee_1",
    batch_status: str = "QUEUED",
    task_status: str = "PENDING",
    archive_status: str = "PENDING",
    quality_status: str = "PENDING",
    quality_issue_codes: list[str] | None = None,
    provider_task_id: str | None = None,
    provider_result_url: str | None = None,
    result_asset_id: str | None = None,
    error_code: str | None = "SAFE_ERROR",
    next_poll_at: str | None = None,
    created_at: str = "2026-08-16 10:00:00",
    submitted_at: str | None = None,
    started_at: str | None = None,
    completed_at: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO generation_batches (
            id, project_id, created_by_user_id, idempotency_key,
            request_hash, request_snapshot_json, status, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            batch_id,
            project_id,
            created_by_user_id,
            f"key-{batch_id}",
            f"hash-{batch_id}",
            json.dumps(
                {
                    "prompt_version_id": f"prompt-{batch_id}",
                    "output_duration_seconds": 10,
                    "resolution": "768P",
                },
                sort_keys=True,
            ),
            batch_status,
            created_at,
            created_at,
        ),
    )
    conn.execute(
        """
        INSERT INTO generation_tasks (
            id, batch_id, generation_mode, provider, model, provider_task_id,
            status, attempt, archive_status, archive_retry_count,
            quality_status, quality_issue_codes, error_code,
            error_message_redacted, result_asset_id, estimated_cost, actual_cost,
            submitted_at, started_at, completed_at, created_at, updated_at,
            prompt_snapshot_json, provider_result_url, next_poll_at
        )
        VALUES (?, ?, 'I2V', 'fake_h3', 'MiniMax-H3', ?, ?, 2, ?, 1, ?, ?,
                ?, 'A redacted failure summary.', ?, 1.25, 1.5,
                ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            f"{batch_id}-task",
            batch_id,
            provider_task_id,
            task_status,
            archive_status,
            quality_status,
            json.dumps(quality_issue_codes or []),
            error_code,
            result_asset_id,
            submitted_at,
            started_at,
            completed_at,
            created_at,
            created_at,
            json.dumps(
                {
                    "prompt_text": "test prompt",
                    "first_frame_uri": "fake://generation-results/first-frame.png",
                },
                sort_keys=True,
            ),
            provider_result_url,
            next_poll_at,
        ),
    )


class ArchiveOnlyRetryProvider(FakeH3Provider):
    def __init__(self) -> None:
        self.create_calls = 0
        self.download_calls = 0

    def create_image_to_video(self, request: dict[str, Any]) -> H3CreateResult:
        del request
        self.create_calls += 1
        raise AssertionError("archive retry must not create a new provider task")

    def download_result(self, url: str) -> bytes:
        assert url == "https://provider.example/result.mp4"
        self.download_calls += 1
        return b"archived-result"


class CountingRetryProvider(FakeH3Provider):
    def __init__(self) -> None:
        self.create_calls = 0

    def create_image_to_video(self, request: dict[str, Any]) -> H3CreateResult:
        self.create_calls += 1
        return super().create_image_to_video(request)


def paid_regeneration_payload(
    key: str,
    *,
    reason: str = "人工确认需要重新生成",
    estimated_cost: float | None = 2.5,
) -> dict[str, object]:
    return {
        "idempotency_key": key,
        "payment_confirmed": True,
        "payment_confirmation_version": "V1",
        "estimated_cost_snapshot": estimated_cost,
        "generation_reason": reason,
    }


def test_batch_paid_regeneration_replays_frozen_snapshots_without_superseding_source(
    db_path: Path,
    client: TestClient,
) -> None:
    prompt_id = create_locked_prompt(client)
    source = client.post(
        "/api/projects/project_owned/generation-batches",
        headers=auth_headers("employee_1"),
        json={
            "quantity": 2,
            "prompt_version_id": prompt_id,
            "first_frame_asset_id": "first_frame_owned",
            "output_duration_seconds": 10,
            "resolution": "768P",
            "idempotency_key": "source-batch-for-regeneration",
        },
    )
    assert source.status_code == 200
    source_batch_id = str(source.json()["id"])

    with connect_database(db_path) as conn:
        source_batch = conn.execute(
            "SELECT request_snapshot_json FROM generation_batches WHERE id = ?",
            (source_batch_id,),
        ).fetchone()
        source_tasks = conn.execute(
            """
            SELECT id, prompt_snapshot_json
            FROM generation_tasks
            WHERE batch_id = ?
            ORDER BY created_at, id
            """,
            (source_batch_id,),
        ).fetchall()
        prompt = conn.execute(
            "SELECT payload_json FROM versions WHERE id = ?",
            (prompt_id,),
        ).fetchone()
        mutated_prompt = json.loads(str(prompt["payload_json"]))
        mutated_prompt["prompt_text"] = "这是上游后来的内容，不得进入冻结重生成"
        conn.execute(
            "UPDATE versions SET payload_json = ? WHERE id = ?",
            (json.dumps(mutated_prompt, sort_keys=True), prompt_id),
        )
        conn.commit()

    request = paid_regeneration_payload("batch-regenerate-key")
    first = client.post(
        f"/api/generation-batches/{source_batch_id}/regenerate",
        headers=auth_headers("employee_1"),
        json=request,
    )
    replay = client.post(
        f"/api/generation-batches/{source_batch_id}/regenerate",
        headers=auth_headers("employee_1"),
        json=request,
    )
    changed_reason = client.post(
        f"/api/generation-batches/{source_batch_id}/regenerate",
        headers=auth_headers("employee_1"),
        json=paid_regeneration_payload(
            "batch-regenerate-key",
            reason="同一键不得改变原因",
        ),
    )
    second_purchase = client.post(
        f"/api/generation-batches/{source_batch_id}/regenerate",
        headers=auth_headers("employee_1"),
        json=paid_regeneration_payload("batch-regenerate-key-2"),
    )

    assert first.status_code == 200
    assert replay.status_code == 200
    assert changed_reason.status_code == 409
    assert changed_reason.json()["detail"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert second_purchase.status_code == 200
    assert replay.json()["id"] == first.json()["id"]
    assert second_purchase.json()["id"] != first.json()["id"]
    assert first.json()["source_batch_id"] == source_batch_id
    assert first.json()["source_task_id"] is None
    assert first.json()["generation_reason"] == request["generation_reason"]
    assert first.json()["quantity"] == 2

    with connect_database(db_path) as conn:
        regenerated_batch = conn.execute(
            """
            SELECT request_hash, request_snapshot_json, source_batch_id,
                   source_task_id, generation_reason
            FROM generation_batches
            WHERE id = ?
            """,
            (first.json()["id"],),
        ).fetchone()
        second_hash = conn.execute(
            "SELECT request_hash FROM generation_batches WHERE id = ?",
            (second_purchase.json()["id"],),
        ).fetchone()[0]
        regenerated_tasks = conn.execute(
            """
            SELECT retry_of_task_id, prompt_snapshot_json, estimated_cost
            FROM generation_tasks
            WHERE batch_id = ?
            """,
            (first.json()["id"],),
        ).fetchall()
        source_superseded = conn.execute(
            """
            SELECT superseded_by_task_id
            FROM generation_tasks
            WHERE batch_id = ?
            """,
            (source_batch_id,),
        ).fetchall()
        audit_count = conn.execute(
            """
            SELECT COUNT(*) FROM audit_logs
            WHERE action = 'generation_batch.regenerate' AND entity_id = ?
            """,
            (first.json()["id"],),
        ).fetchone()[0]

    assert regenerated_batch["request_snapshot_json"] == source_batch["request_snapshot_json"]
    assert regenerated_batch["source_batch_id"] == source_batch_id
    assert regenerated_batch["source_task_id"] is None
    assert regenerated_batch["generation_reason"] == request["generation_reason"]
    assert regenerated_batch["request_hash"] == second_hash
    source_prompts = {str(row["id"]): str(row["prompt_snapshot_json"]) for row in source_tasks}
    assert {
        str(row["retry_of_task_id"]): str(row["prompt_snapshot_json"]) for row in regenerated_tasks
    } == source_prompts
    assert {float(row["estimated_cost"]) for row in regenerated_tasks} == {1.25}
    assert {row["superseded_by_task_id"] for row in source_superseded} == {None}
    assert audit_count == 1


def test_batch_paid_regeneration_hash_rejects_same_key_for_another_source(
    client: TestClient,
) -> None:
    source_ids: list[str] = []
    for index in range(2):
        prompt_id = create_locked_prompt(client, script_text=f"来源 {index} 的冻结脚本。")
        source = client.post(
            "/api/projects/project_owned/generation-batches",
            headers=auth_headers("employee_1"),
            json={
                "quantity": 1,
                "prompt_version_id": prompt_id,
                "first_frame_asset_id": "first_frame_owned",
                "output_duration_seconds": 10,
                "resolution": "768P",
                "idempotency_key": f"source-batch-{index}",
            },
        )
        assert source.status_code == 200
        source_ids.append(str(source.json()["id"]))

    request = paid_regeneration_payload("shared-regeneration-key")
    first = client.post(
        f"/api/generation-batches/{source_ids[0]}/regenerate",
        headers=auth_headers("employee_1"),
        json=request,
    )
    conflict = client.post(
        f"/api/generation-batches/{source_ids[1]}/regenerate",
        headers=auth_headers("employee_1"),
        json=request,
    )

    assert first.status_code == 200
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "IDEMPOTENCY_CONFLICT"


def test_task_paid_regeneration_supersedes_audio_failure_once_and_keeps_history(
    db_path: Path,
    client: TestClient,
) -> None:
    prompt_id = create_locked_prompt(client)
    source = client.post(
        "/api/projects/project_owned/generation-batches",
        headers=auth_headers("employee_1"),
        json={
            "quantity": 1,
            "prompt_version_id": prompt_id,
            "first_frame_asset_id": "first_frame_owned",
            "output_duration_seconds": 10,
            "resolution": "768P",
            "idempotency_key": "audio-failure-source",
            "fake_audio_quality": "missing",
        },
    )
    assert source.status_code == 200
    source_batch_id = str(source.json()["id"])
    source_task_id = str(source.json()["tasks"][0]["id"])
    with connect_database(db_path) as conn:
        result = run_next_generation_task(
            conn,
            worker_id="audio-failure-worker",
            provider=FakeH3Provider(audio_quality="missing"),
            storage=FakeStorageAdapter(provider="fake", bucket="generation-results"),
        )
    assert result is not None
    assert result.quality_status == "AUDIO_QUALITY_FAILED"

    request = paid_regeneration_payload(
        "task-regenerate-key",
        reason="音频质检失败，确认重新生成视频",
        estimated_cost=1.25,
    )
    first = client.post(
        f"/api/generation-tasks/{source_task_id}/regenerate",
        headers=auth_headers("employee_1"),
        json=request,
    )
    replay = client.post(
        f"/api/generation-tasks/{source_task_id}/regenerate",
        headers=auth_headers("employee_1"),
        json=request,
    )
    second_key = client.post(
        f"/api/generation-tasks/{source_task_id}/regenerate",
        headers=auth_headers("employee_1"),
        json=paid_regeneration_payload("task-regenerate-key-2"),
    )

    assert first.status_code == 200
    assert replay.status_code == 200
    assert replay.json()["id"] == first.json()["id"]
    assert first.json()["source_batch_id"] == source_batch_id
    assert first.json()["source_task_id"] == source_task_id
    assert first.json()["quantity"] == 1
    assert second_key.status_code == 409
    assert second_key.json()["detail"]["code"] == "SOURCE_TASK_ALREADY_SUPERSEDED"

    replacement_task_id = str(first.json()["tasks"][0]["id"])
    with connect_database(db_path) as conn:
        source_row = conn.execute(
            """
            SELECT status, quality_status, result_asset_id, superseded_by_task_id,
                   superseded_at
            FROM generation_tasks WHERE id = ?
            """,
            (source_task_id,),
        ).fetchone()
        replacement_row = conn.execute(
            """
            SELECT retry_of_task_id, retry_reason, retry_requested_by_user_id,
                   estimated_cost, status
            FROM generation_tasks WHERE id = ?
            """,
            (replacement_task_id,),
        ).fetchone()
        audit_count = conn.execute(
            """
            SELECT COUNT(*) FROM audit_logs
            WHERE action = 'generation_task.regenerate' AND entity_id = ?
            """,
            (replacement_task_id,),
        ).fetchone()[0]

    assert source_row["status"] == "SUCCEEDED"
    assert source_row["quality_status"] == "AUDIO_QUALITY_FAILED"
    assert source_row["result_asset_id"] is not None
    assert source_row["superseded_by_task_id"] == replacement_task_id
    assert source_row["superseded_at"] is not None
    assert replacement_row["retry_of_task_id"] == source_task_id
    assert replacement_row["retry_reason"] == request["generation_reason"]
    assert replacement_row["retry_requested_by_user_id"] == "employee_1"
    assert replacement_row["estimated_cost"] == 1.25
    assert replacement_row["status"] == "PENDING"
    assert audit_count == 1

    historical = client.get(
        f"/api/generation-batches/{source_batch_id}",
        headers=auth_headers("employee_1"),
    )
    assert historical.status_code == 200
    assert historical.json()["status"] == "COMPLETED_WITH_FAILURES"
    assert historical.json()["progress"]["counts"]["needs_attention"] == 0
    assert historical.json()["progress"]["historical_counts"] == {
        "archive_failed": 0,
        "audio_quality_failed": 1,
        "failed": 0,
        "superseded": 1,
    }

    provider = CountingRetryProvider()
    with connect_database(db_path) as conn:
        storage = FakeStorageAdapter(provider="fake", bucket="generation-results")
        completed = run_next_generation_task(
            conn,
            worker_id="replacement-worker",
            provider=provider,
            storage=storage,
        )
        no_duplicate = run_next_generation_task(
            conn,
            worker_id="replacement-worker",
            provider=provider,
            storage=storage,
        )
    assert completed is not None
    assert completed.id == replacement_task_id
    assert completed.quality_status == "AUDIO_OK"
    assert no_duplicate is None
    assert provider.create_calls == 1


def test_task_paid_regeneration_accepts_a_failed_submitted_provider_call(
    db_path: Path,
    client: TestClient,
) -> None:
    with connect_database(db_path) as conn:
        insert_generation_history(
            conn,
            batch_id="submitted-failure",
            batch_status="COMPLETED_WITH_FAILURES",
            task_status="FAILED",
            provider_task_id="provider-paid-failure",
            submitted_at="2026-08-16 10:00:01",
            completed_at="2026-08-16 10:00:03",
            error_code="H3_PROVIDER_FAILED",
        )
        conn.commit()

    source = client.get(
        "/api/generation-batches/submitted-failure",
        headers=auth_headers("employee_1"),
    )
    response = client.post(
        "/api/generation-tasks/submitted-failure-task/regenerate",
        headers=auth_headers("employee_1"),
        json=paid_regeneration_payload("submitted-failure-regeneration"),
    )

    assert source.status_code == 200
    assert source.json()["tasks"][0]["available_actions"] == ["REGENERATE"]
    assert response.status_code == 200
    assert response.json()["source_task_id"] == "submitted-failure-task"
    assert response.json()["tasks"][0]["retry_of_task_id"] == "submitted-failure-task"


def test_task_paid_regeneration_concurrency_creates_only_one_replacement(
    db_path: Path,
    client: TestClient,
) -> None:
    with connect_database(db_path) as conn:
        insert_generation_history(
            conn,
            batch_id="paid-regeneration-race",
            batch_status="NEEDS_ATTENTION",
            task_status="SUCCEEDED",
            archive_status="ARCHIVED",
            quality_status="AUDIO_QUALITY_FAILED",
            quality_issue_codes=["AUDIO_QUALITY_FAILED"],
            provider_task_id="provider-paid-race",
            submitted_at="2026-08-16 10:00:01",
            completed_at="2026-08-16 10:00:03",
            error_code=None,
        )
        conn.commit()

    barrier = threading.Barrier(2)
    statuses: list[int] = []
    result_lock = threading.Lock()

    def regenerate(key: str) -> None:
        barrier.wait()
        response = client.post(
            "/api/generation-tasks/paid-regeneration-race-task/regenerate",
            headers=auth_headers("employee_1"),
            json=paid_regeneration_payload(key),
        )
        with result_lock:
            statuses.append(response.status_code)

    threads = [
        threading.Thread(target=regenerate, args=("race-regenerate-a",)),
        threading.Thread(target=regenerate, args=("race-regenerate-b",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(statuses) == [200, 409]
    with connect_database(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM generation_batches").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM generation_tasks").fetchone()[0] == 2
        assert (
            conn.execute(
                """
                SELECT COUNT(*) FROM audit_logs
                WHERE action = 'generation_task.regenerate'
                """
            ).fetchone()[0]
            == 1
        )


@pytest.mark.parametrize(
    (
        "batch_id",
        "task_status",
        "archive_status",
        "quality_status",
        "provider_task_id",
        "submitted_at",
        "expected_code",
    ),
    [
        (
            "pre-provider",
            "FAILED",
            "PENDING",
            "PENDING",
            None,
            None,
            "PAID_REGENERATION_NOT_ALLOWED",
        ),
        (
            "uncertain-paid",
            "SUBMISSION_UNCERTAIN",
            "PENDING",
            "PENDING",
            "provider-known",
            "2026-08-16 10:00:01",
            "MUST_RECONCILE_SUBMISSION",
        ),
        (
            "archive-paid",
            "SUCCEEDED",
            "ARCHIVE_FAILED",
            "AUDIO_OK",
            "provider-known",
            "2026-08-16 10:00:01",
            "ARCHIVE_RETRY_ONLY",
        ),
    ],
)
def test_task_paid_regeneration_rejects_non_payable_states(
    db_path: Path,
    client: TestClient,
    batch_id: str,
    task_status: str,
    archive_status: str,
    quality_status: str,
    provider_task_id: str | None,
    submitted_at: str | None,
    expected_code: str,
) -> None:
    with connect_database(db_path) as conn:
        insert_generation_history(
            conn,
            batch_id=batch_id,
            batch_status="NEEDS_ATTENTION",
            task_status=task_status,
            archive_status=archive_status,
            quality_status=quality_status,
            provider_task_id=provider_task_id,
            provider_result_url=(
                "https://provider.example/result.mp4"
                if archive_status == "ARCHIVE_FAILED"
                else None
            ),
            submitted_at=submitted_at,
            error_code="FIRST_FRAME_URL_SIGN_FAILED",
        )
        conn.commit()

    response = client.post(
        f"/api/generation-tasks/{batch_id}-task/regenerate",
        headers=auth_headers("employee_1"),
        json=paid_regeneration_payload(f"regenerate-{batch_id}"),
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == expected_code


def test_paid_regeneration_requires_write_access_and_explicit_payment_confirmation(
    client: TestClient,
) -> None:
    prompt_id = create_locked_prompt(client)
    source = client.post(
        "/api/projects/project_owned/generation-batches",
        headers=auth_headers("employee_1"),
        json={
            "quantity": 1,
            "prompt_version_id": prompt_id,
            "first_frame_asset_id": "first_frame_owned",
            "output_duration_seconds": 10,
            "resolution": "768P",
            "idempotency_key": "payment-gate-source",
        },
    ).json()

    missing_payment = client.post(
        f"/api/generation-batches/{source['id']}/regenerate",
        headers=auth_headers("employee_1"),
        json={
            "idempotency_key": "missing-payment",
            "generation_reason": "人工要求重新生成",
            "payment_confirmation_version": "V1",
            "estimated_cost_snapshot": None,
        },
    )
    auditor = client.post(
        f"/api/generation-batches/{source['id']}/regenerate",
        headers=auth_headers("auditor_1"),
        json=paid_regeneration_payload("auditor-payment-attempt"),
    )
    task_auditor = client.post(
        f"/api/generation-tasks/{source['tasks'][0]['id']}/regenerate",
        headers=auth_headers("auditor_1"),
        json=paid_regeneration_payload("auditor-task-payment-attempt"),
    )

    assert missing_payment.status_code == 422
    assert auditor.status_code == 403
    assert auditor.json()["detail"]["code"] == "ROLE_FORBIDDEN"
    assert task_auditor.status_code == 403
    assert task_auditor.json()["detail"]["code"] == "ROLE_FORBIDDEN"


def test_retry_archive_failed_is_idempotent_and_never_creates_provider_task(
    db_path: Path, client: TestClient
) -> None:
    with connect_database(db_path) as conn:
        insert_generation_history(
            conn,
            batch_id="archive-retry",
            batch_status="PARTIAL_FAILED",
            task_status="SUCCEEDED",
            archive_status="ARCHIVE_FAILED",
            provider_task_id="provider-paid-1",
            provider_result_url="https://provider.example/result.mp4",
        )
        conn.commit()

    request = {"idempotency_key": "retry-archive-1", "retry_reason": "重新归档成片"}
    first = client.post(
        "/api/generation-tasks/archive-retry-task/retry",
        headers=auth_headers("employee_1"),
        json=request,
    )
    replay = client.post(
        "/api/generation-tasks/archive-retry-task/retry",
        headers=auth_headers("employee_1"),
        json=request,
    )

    assert first.status_code == 200
    assert replay.status_code == 200
    assert first.json()["id"] == "archive-retry-task"
    assert first.json()["status"] == "SUCCEEDED"

    provider = ArchiveOnlyRetryProvider()
    with connect_database(db_path) as conn:
        queued = conn.execute(
            "SELECT next_poll_at FROM generation_tasks WHERE id = ?",
            ("archive-retry-task",),
        ).fetchone()
        operation_count = conn.execute(
            "SELECT COUNT(*) FROM generation_task_operations WHERE task_id = ?",
            ("archive-retry-task",),
        ).fetchone()[0]
        audit_count = conn.execute(
            "SELECT COUNT(*) FROM audit_logs WHERE action = ? AND entity_id = ?",
            ("generation_task.archive_retry_queued", "archive-retry-task"),
        ).fetchone()[0]
        assert queued["next_poll_at"] is not None

        result = run_next_generation_task(
            conn,
            worker_id="archive-worker",
            provider=provider,
            storage=FakeStorageAdapter(provider="cos", bucket="generation-results"),
        )

    assert operation_count == 1
    assert audit_count == 1
    assert provider.create_calls == 0
    assert provider.download_calls == 1
    assert result is not None
    assert result.archive_status == "ARCHIVED"


def test_retry_pre_provider_failure_requeues_once_and_records_lineage(
    db_path: Path, client: TestClient
) -> None:
    with connect_database(db_path) as conn:
        insert_generation_history(
            conn,
            batch_id="safe-retry",
            batch_status="FAILED",
            task_status="FAILED",
            error_code="FIRST_FRAME_URL_SIGN_FAILED",
        )
        conn.commit()

    request = {"idempotency_key": "retry-safe-1", "retry_reason": "修复首帧签名后重试"}
    first = client.post(
        "/api/generation-tasks/safe-retry-task/retry",
        headers=auth_headers("employee_1"),
        json=request,
    )
    replay = client.post(
        "/api/generation-tasks/safe-retry-task/retry",
        headers=auth_headers("employee_1"),
        json=request,
    )

    assert first.status_code == 200
    assert replay.status_code == 200
    assert first.json()["status"] == "PENDING"
    assert first.json()["retry_reason"] == "修复首帧签名后重试"

    with connect_database(db_path) as conn:
        row = conn.execute(
            """
            SELECT status, error_code, error_message_redacted, next_poll_at,
                   retry_reason, retry_requested_by_user_id, retry_requested_at
            FROM generation_tasks WHERE id = ?
            """,
            ("safe-retry-task",),
        ).fetchone()
        operation_count = conn.execute(
            "SELECT COUNT(*) FROM generation_task_operations WHERE task_id = ?",
            ("safe-retry-task",),
        ).fetchone()[0]

    assert row["status"] == "PENDING"
    assert row["error_code"] is None
    assert row["error_message_redacted"] is None
    assert row["next_poll_at"] is not None
    assert row["retry_reason"] == "修复首帧签名后重试"
    assert row["retry_requested_by_user_id"] == "employee_1"
    assert row["retry_requested_at"] is not None
    assert operation_count == 1

    provider = CountingRetryProvider()
    with connect_database(db_path) as conn:
        storage = FakeStorageAdapter(provider="fake", bucket="generation-results")
        result = run_next_generation_task(
            conn,
            worker_id="safe-retry-worker",
            provider=provider,
            storage=storage,
        )
        no_duplicate = run_next_generation_task(
            conn,
            worker_id="safe-retry-worker",
            provider=provider,
            storage=storage,
        )

    assert result is not None
    assert result.status == "SUCCEEDED"
    assert result.attempt == 3
    assert no_duplicate is None
    assert provider.create_calls == 1


@pytest.mark.parametrize(
    (
        "batch_id",
        "task_status",
        "archive_status",
        "quality_status",
        "provider_task_id",
        "error_code",
        "expected_code",
    ),
    [
        (
            "archive-missing",
            "FAILED",
            "ARCHIVE_FAILED",
            "PENDING",
            "paid-provider-id",
            "ARCHIVE_RETRY_EXHAUSTED",
            "ARCHIVE_RESULT_UNAVAILABLE",
        ),
        (
            "provider-failed",
            "FAILED",
            "PENDING",
            "PENDING",
            "paid-provider-id",
            "H3_PROVIDER_FAILED",
            "REQUIRES_PAID_REGENERATION",
        ),
        (
            "uncertain-known",
            "SUBMISSION_UNCERTAIN",
            "PENDING",
            "PENDING",
            "provider-known",
            "SUBMISSION_UNCERTAIN",
            "MUST_RECONCILE_SUBMISSION",
        ),
        (
            "audio-failed",
            "SUCCEEDED",
            "ARCHIVED",
            "AUDIO_QUALITY_FAILED",
            "provider-paid-id",
            None,
            "REQUIRES_PAID_REGENERATION",
        ),
    ],
)
def test_retry_rejects_unsafe_state_transitions(
    db_path: Path,
    client: TestClient,
    batch_id: str,
    task_status: str,
    archive_status: str,
    quality_status: str,
    provider_task_id: str | None,
    error_code: str | None,
    expected_code: str,
) -> None:
    with connect_database(db_path) as conn:
        insert_generation_history(
            conn,
            batch_id=batch_id,
            batch_status="FAILED",
            task_status=task_status,
            archive_status=archive_status,
            quality_status=quality_status,
            provider_task_id=provider_task_id,
            error_code=error_code,
        )
        conn.commit()

    response = client.post(
        f"/api/generation-tasks/{batch_id}-task/retry",
        headers=auth_headers("employee_1"),
        json={"idempotency_key": f"retry-{batch_id}", "retry_reason": "请求安全重试"},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == expected_code


def test_retry_idempotency_key_conflicts_when_payload_changes(
    db_path: Path, client: TestClient
) -> None:
    with connect_database(db_path) as conn:
        insert_generation_history(
            conn,
            batch_id="retry-conflict",
            batch_status="FAILED",
            task_status="FAILED",
            error_code="FIRST_FRAME_URL_SIGN_FAILED",
        )
        conn.commit()

    first = client.post(
        "/api/generation-tasks/retry-conflict-task/retry",
        headers=auth_headers("employee_1"),
        json={"idempotency_key": "same-retry-key", "retry_reason": "第一次原因"},
    )
    conflict = client.post(
        "/api/generation-tasks/retry-conflict-task/retry",
        headers=auth_headers("employee_1"),
        json={"idempotency_key": "same-retry-key", "retry_reason": "不同原因"},
    )

    assert first.status_code == 200
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "IDEMPOTENCY_CONFLICT"


def test_only_admin_can_confirm_an_unbilled_uncertain_submission(
    db_path: Path, client: TestClient
) -> None:
    with connect_database(db_path) as conn:
        insert_generation_history(
            conn,
            batch_id="confirm-unbilled",
            batch_status="NEEDS_ATTENTION",
            task_status="SUBMISSION_UNCERTAIN",
            error_code="SUBMISSION_UNCERTAIN",
        )
        conn.commit()

    payload = {"idempotency_key": "confirm-unbilled-1", "reason": "已核对供应商账单，未产生扣费"}
    employee = client.post(
        "/api/generation-tasks/confirm-unbilled-task/confirm-not-charged",
        headers=auth_headers("employee_1"),
        json=payload,
    )
    admin = client.post(
        "/api/generation-tasks/confirm-unbilled-task/confirm-not-charged",
        headers=auth_headers("admin_1"),
        json=payload,
    )
    replay = client.post(
        "/api/generation-tasks/confirm-unbilled-task/confirm-not-charged",
        headers=auth_headers("admin_1"),
        json=payload,
    )

    assert employee.status_code == 403
    assert admin.status_code == 200
    assert replay.status_code == 200
    assert admin.json()["status"] == "PENDING"

    with connect_database(db_path) as conn:
        row = conn.execute(
            """
            SELECT status, billing_confirmation_status, billing_confirmed_by_user_id,
                   billing_confirmed_at, billing_confirmation_reason, submitted_at
            FROM generation_tasks WHERE id = ?
            """,
            ("confirm-unbilled-task",),
        ).fetchone()
        operation_count = conn.execute(
            "SELECT COUNT(*) FROM generation_task_operations WHERE task_id = ? AND action = ?",
            ("confirm-unbilled-task", "CONFIRM_NOT_CHARGED"),
        ).fetchone()[0]

    assert row["status"] == "PENDING"
    assert row["billing_confirmation_status"] == "CONFIRMED_NOT_CHARGED"
    assert row["billing_confirmed_by_user_id"] == "admin_1"
    assert row["billing_confirmed_at"] is not None
    assert row["billing_confirmation_reason"] == payload["reason"]
    assert row["submitted_at"] is None
    assert operation_count == 1


def test_script_maps_spoken_text_to_shots_without_deleting_user_text(client: TestClient) -> None:
    blank = client.post(
        "/api/projects/project_owned/scripts",
        headers=auth_headers("employee_1"),
        json={
            "source": "custom",
            "text": "   ",
            "shot_card_version_id": "shot_card_v1",
        },
    )
    assert blank.status_code == 422
    assert blank.json()["detail"]["code"] == "SCRIPT_TEXT_REQUIRED"

    response = client.post(
        "/api/projects/project_owned/scripts",
        headers=auth_headers("employee_1"),
        json={
            "source": "custom",
            "text": "第一句不要切断。第二句保留原文。",
            "shot_card_version_id": "shot_card_v1",
        },
    )

    assert response.status_code == 200
    payload = response.json()["payload"]
    assert payload["source"] == "custom"
    assert payload["char_count"] == len("第一句不要切断。第二句保留原文。")
    assert payload["full_text"] == "第一句不要切断。第二句保留原文。"
    assert [segment["shot_id"] for segment in payload["shot_mappings"]] == ["S01", "S02"]
    assert payload["shot_mappings"][0]["text"] == "第一句不要切断。"
    assert payload["shot_mappings"][1]["text"] == "第二句保留原文。"
    assert payload["creates_audio_task"] is False


def test_generation_workflow_exposes_runtime_limits_and_script_staleness(
    client: TestClient,
    db_path: Path,
) -> None:
    runtime = client.get(
        "/api/generation/runtime-limits",
        headers=auth_headers("employee_1"),
    )
    assert runtime.status_code == 200
    assert runtime.json() == {
        "min_quantity": 1,
        "max_quantity": 4,
        "estimated_cost_per_task": None,
    }

    missing = client.get(
        "/api/projects/project_owned/scripts/latest",
        headers=auth_headers("employee_1"),
    )
    assert missing.status_code == 200
    assert missing.json() == {"version": None, "stale": False, "stale_reasons": []}

    script = client.post(
        "/api/projects/project_owned/scripts",
        headers=auth_headers("employee_1"),
        json={
            "source": "original",
            "text": "原始第一句。原始第二句。",
            "shot_card_version_id": "shot_card_v1",
        },
    )
    assert script.status_code == 200

    current = client.get(
        "/api/projects/project_owned/scripts/latest",
        headers=auth_headers("employee_1"),
    )
    assert current.status_code == 200
    assert current.json()["version"]["id"] == script.json()["id"]
    assert current.json()["stale"] is False

    insert_next_shot_card_version(db_path)

    stale = client.get(
        "/api/projects/project_owned/scripts/latest",
        headers=auth_headers("employee_1"),
    )
    assert stale.status_code == 200
    assert stale.json()["stale"] is True
    assert stale.json()["stale_reasons"] == ["SHOT_CARD_SUPERSEDED"]

    rejected = client.post(
        "/api/projects/project_owned/scripts",
        headers=auth_headers("employee_1"),
        json={
            "source": "custom",
            "text": "不能绑定旧镜头卡。",
            "shot_card_version_id": "shot_card_v1",
        },
    )
    assert rejected.status_code == 409
    assert rejected.json()["detail"]["code"] == "SHOT_CARD_STALE"


def test_new_analysis_makes_the_existing_shot_card_and_script_stale(
    client: TestClient,
    db_path: Path,
) -> None:
    with connect_database(db_path) as conn:
        shot_card = conn.execute(
            "SELECT payload_json FROM versions WHERE id = 'shot_card_v1'"
        ).fetchone()
        assert shot_card is not None
        shot_payload = json.loads(str(shot_card["payload_json"]))
        shot_payload["source_analysis_version_id"] = "analysis_v1"
        conn.execute(
            "UPDATE versions SET payload_json = ? WHERE id = 'shot_card_v1'",
            (json.dumps(shot_payload, ensure_ascii=True, sort_keys=True),),
        )
        conn.execute(
            """
            INSERT INTO versions (
                id, project_id, asset_id, kind, version_number, payload_json, created_by_user_id
            ) VALUES ('analysis_v1', 'project_owned', NULL, 'analysis', 1, '{}', 'employee_1')
            """
        )
        conn.commit()

    script = client.post(
        "/api/projects/project_owned/scripts",
        headers=auth_headers("employee_1"),
        json={
            "source": "original",
            "text": "跟随第一版分析。",
            "shot_card_version_id": "shot_card_v1",
        },
    )
    assert script.status_code == 200

    with connect_database(db_path) as conn:
        conn.execute(
            """
            INSERT INTO versions (
                id, project_id, asset_id, kind, version_number, payload_json, created_by_user_id
            ) VALUES ('analysis_v2', 'project_owned', NULL, 'analysis', 2, '{}', 'employee_1')
            """
        )
        conn.commit()

    state = client.get(
        "/api/projects/project_owned/scripts/latest",
        headers=auth_headers("employee_1"),
    )
    assert state.status_code == 200
    assert state.json()["stale"] is True
    assert state.json()["stale_reasons"] == ["ANALYSIS_SUPERSEDED"]

    rejected = client.post(
        "/api/projects/project_owned/scripts",
        headers=auth_headers("employee_1"),
        json={
            "source": "original",
            "text": "不能继续绑定旧分析。",
            "shot_card_version_id": "shot_card_v1",
        },
    )
    assert rejected.status_code == 409
    assert rejected.json()["detail"]["code"] == "SHOT_CARD_STALE"


def test_prompt_revision_freezes_sources_and_batch_reports_staleness(
    client: TestClient,
) -> None:
    script = client.post(
        "/api/projects/project_owned/scripts",
        headers=auth_headers("employee_1"),
        json={
            "source": "custom",
            "text": "第一句。第二句。",
            "shot_card_version_id": "shot_card_v1",
        },
    ).json()
    compiled = client.post(
        "/api/projects/project_owned/prompts/compile",
        headers=auth_headers("employee_1"),
        json={
            "script_version_id": script["id"],
            "shot_card_version_id": "shot_card_v1",
            "first_frame_asset_id": "first_frame_owned",
            "output_duration_seconds": 10,
            "resolution": "768P",
        },
    )
    assert compiled.status_code == 200
    compiled_payload = compiled.json()["payload"]
    assert compiled_payload["status"] == "SAVED"
    assert compiled_payload["template_version"] == "h3.prompt.v3"
    assert len(compiled_payload["template_hash"]) == 64
    assert compiled_payload["source_analysis_version_id"] is None
    assert compiled_payload["script_version_id"] == script["id"]
    assert compiled_payload["shot_card_version_id"] == "shot_card_v1"
    assert compiled_payload["first_frame_candidates_version_id"] == "first_frame_candidates_v1"
    assert compiled_payload["first_frame_selection_version_id"] == "first_frame_selection_v1"
    assert compiled_payload["character_version_id"] is None
    assert compiled_payload["character_reference_selection_id"] is None

    blank_revision = client.post(
        "/api/projects/project_owned/prompts/revise",
        headers=auth_headers("employee_1"),
        json={
            "base_prompt_version_id": compiled.json()["id"],
            "prompt_text": "   ",
        },
    )
    assert blank_revision.status_code == 422
    assert blank_revision.json()["detail"]["code"] == "PROMPT_TEXT_REQUIRED"

    revised = client.post(
        "/api/projects/project_owned/prompts/revise",
        headers=auth_headers("employee_1"),
        json={
            "base_prompt_version_id": compiled.json()["id"],
            "prompt_text": "人工修订后的 H3 Prompt",
        },
    )
    assert revised.status_code == 200
    revised_payload = revised.json()["payload"]
    assert revised.json()["version_number"] == 2
    assert revised_payload["status"] == "SAVED"
    assert revised_payload["prompt_text"] == "人工修订后的 H3 Prompt"
    assert revised_payload["base_prompt_version_id"] == compiled.json()["id"]
    assert revised_payload["script_version_id"] == script["id"]
    assert revised_payload["content_hash"] != compiled_payload["content_hash"]

    stale_old_lock = client.post(
        f"/api/projects/project_owned/prompts/{compiled.json()['id']}/lock",
        headers=auth_headers("employee_1"),
    )
    assert stale_old_lock.status_code == 409
    assert stale_old_lock.json()["detail"]["code"] == "PROMPT_STALE"

    prompt_state = client.get(
        "/api/projects/project_owned/prompts/latest",
        headers=auth_headers("employee_1"),
    )
    assert prompt_state.status_code == 200
    assert prompt_state.json()["version"]["id"] == revised.json()["id"]
    assert prompt_state.json()["stale"] is False

    locked = client.post(
        f"/api/projects/project_owned/prompts/{revised.json()['id']}/lock",
        headers=auth_headers("employee_1"),
    )
    assert locked.status_code == 200

    created = client.post(
        "/api/projects/project_owned/generation-batches",
        headers=auth_headers("employee_1"),
        json={
            "quantity": 2,
            "prompt_version_id": revised.json()["id"],
            "first_frame_asset_id": "first_frame_owned",
            "output_duration_seconds": 10,
            "resolution": "768P",
            "idempotency_key": "workflow-key",
        },
    )
    assert created.status_code == 200
    assert created.json()["project_id"] == "project_owned"
    assert created.json()["prompt_version_id"] == revised.json()["id"]
    assert created.json()["stale"] is False
    assert created.json()["tasks"][0]["prompt_snapshot"]["prompt_text"] == (
        "人工修订后的 H3 Prompt"
    )

    newer_script = client.post(
        "/api/projects/project_owned/scripts",
        headers=auth_headers("employee_1"),
        json={
            "source": "custom",
            "text": "这是更新后的口播稿。",
            "shot_card_version_id": "shot_card_v1",
        },
    )
    assert newer_script.status_code == 200

    stale_prompt = client.get(
        "/api/projects/project_owned/prompts/latest",
        headers=auth_headers("employee_1"),
    )
    assert stale_prompt.status_code == 200
    assert stale_prompt.json()["stale"] is True
    assert stale_prompt.json()["stale_reasons"] == ["SCRIPT_SUPERSEDED"]

    stale_batch = client.get(
        f"/api/generation-batches/{created.json()['id']}",
        headers=auth_headers("employee_1"),
    )
    assert stale_batch.status_code == 200
    assert stale_batch.json()["stale"] is True

    replay = client.post(
        "/api/projects/project_owned/generation-batches",
        headers=auth_headers("employee_1"),
        json={
            "quantity": 2,
            "prompt_version_id": revised.json()["id"],
            "first_frame_asset_id": "first_frame_owned",
            "output_duration_seconds": 10,
            "resolution": "768P",
            "idempotency_key": "workflow-key",
        },
    )
    assert replay.status_code == 200
    assert replay.json()["id"] == created.json()["id"]
    assert replay.json()["stale"] is True


def test_prompt_compiler_rescales_shot_timeline_to_output_duration(
    client: TestClient,
) -> None:
    script = client.post(
        "/api/projects/project_owned/scripts",
        headers=auth_headers("employee_1"),
        json={
            "source": "custom",
            "text": "第一句。第二句。",
            "shot_card_version_id": "shot_card_v1",
        },
    ).json()

    compiled = client.post(
        "/api/projects/project_owned/prompts/compile",
        headers=auth_headers("employee_1"),
        json={
            "script_version_id": script["id"],
            "shot_card_version_id": "shot_card_v1",
            "first_frame_asset_id": "first_frame_owned",
            "output_duration_seconds": 4,
            "resolution": "768P",
        },
    )

    assert compiled.status_code == 200
    payload = compiled.json()["payload"]
    assert payload["template_version"] == "h3.prompt.v3"
    assert payload["source_duration_seconds"] == 10
    assert payload["timeline_scale_factor"] == 0.4
    assert "生成一条 4 秒" in payload["prompt_text"]
    assert "[0.0-2.0s]" in payload["prompt_text"]
    assert "[2.0-4.0s]" in payload["prompt_text"]
    assert "[5.0-10.0s]" not in payload["prompt_text"]


def test_compile_prompt_text_renders_structured_motion_as_movement_instructions() -> None:
    """WALKING 镜头必须编译出正向运动指令，不能被“保持连续”稀释掉。"""
    prompt_text = compile_prompt_text(
        script_payload={"full_text": "边走边说。", "shot_mappings": []},
        shot_payload={
            "shots": [
                {
                    "shot_id": "S01",
                    "start_time": 0,
                    "end_time": 5,
                    "shot_type": "中景",
                    "composition": "人物居中",
                    "camera_motion": "手持跟拍",
                    "subject": "主讲人",
                    "action": "边向镜头走近边口播",
                    "scene": "工地",
                    "spoken_text": "边走边说。",
                    "transition": "硬切",
                    "motion": {
                        "subject_motion_state": "WALKING",
                        "subject_direction": "toward_camera",
                        "subject_displacement": "向镜头走近两三步",
                        "hand_action": "双臂随步态交替自然摆动",
                        "camera_motion": "HANDHELD_TRACKING",
                        "relative_motion": "人物逐渐靠近镜头，画面占比增大",
                    },
                }
            ]
        },
        source_duration_seconds=5,
        duration_seconds=5,
        resolution="768P",
    )

    assert "主体：主讲人" in prompt_text
    assert "人物动作：边向镜头走近边口播" in prompt_text
    assert "人物持续行走" in prompt_text
    assert "不得僵立原地" in prompt_text
    assert "向镜头方向" in prompt_text
    assert "位移：向镜头走近两三步" in prompt_text
    assert "手部：双臂随步态交替自然摆动" in prompt_text
    assert "相对运动：人物逐渐靠近镜头，画面占比增大" in prompt_text
    # motion.camera_motion 枚举优先于自由文本“手持跟拍”。
    assert "手持平稳跟拍" in prompt_text
    assert "人物严格按各镜头的动作与运镜描述真实运动" in prompt_text


def test_compile_prompt_text_falls_back_to_action_text_for_legacy_shots() -> None:
    """旧版拆解结果没有 motion：回退到 action 文本，运镜用自由文本。"""
    prompt_text = compile_prompt_text(
        script_payload={"full_text": "你好。", "shot_mappings": []},
        shot_payload={
            "shots": [
                {
                    "shot_id": "S01",
                    "start_time": 0,
                    "end_time": 5,
                    "shot_type": "近景",
                    "composition": "人物居中",
                    "camera_motion": "固定",
                    "subject": "主讲人",
                    "action": "看镜头口播",
                    "scene": "室内",
                    "spoken_text": "你好。",
                    "transition": "硬切",
                }
            ]
        },
        source_duration_seconds=5,
        duration_seconds=5,
        resolution="768P",
    )

    assert "主体：主讲人" in prompt_text
    assert "人物动作：看镜头口播" in prompt_text
    # 自由文本运镜原样保留，不误用枚举标签。
    assert "固定；主体：主讲人" in prompt_text


@pytest.mark.parametrize(
    ("template_attribute", "next_value"),
    [
        ("H3_PROMPT_TEMPLATE_VERSION", "h3.prompt.v4"),
        ("H3_PROMPT_TEMPLATE_HASH", "new-template-hash"),
    ],
)
def test_prompt_becomes_stale_when_the_compiler_template_changes(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    template_attribute: str,
    next_value: str,
) -> None:
    script = client.post(
        "/api/projects/project_owned/scripts",
        headers=auth_headers("employee_1"),
        json={
            "source": "custom",
            "text": "模板变化前的口播稿。",
            "shot_card_version_id": "shot_card_v1",
        },
    ).json()
    prompt = client.post(
        "/api/projects/project_owned/prompts/compile",
        headers=auth_headers("employee_1"),
        json={
            "script_version_id": script["id"],
            "shot_card_version_id": "shot_card_v1",
            "first_frame_asset_id": "first_frame_owned",
            "output_duration_seconds": 10,
            "resolution": "768P",
        },
    ).json()
    monkeypatch.setattr(f"app.generation.{template_attribute}", next_value)

    state = client.get(
        "/api/projects/project_owned/prompts/latest",
        headers=auth_headers("employee_1"),
    )
    rejected_lock = client.post(
        f"/api/projects/project_owned/prompts/{prompt['id']}/lock",
        headers=auth_headers("employee_1"),
    )

    assert state.status_code == 200
    assert state.json()["stale"] is True
    assert state.json()["stale_reasons"] == ["TEMPLATE_SUPERSEDED"]
    assert rejected_lock.status_code == 409
    assert rejected_lock.json()["detail"]["code"] == "PROMPT_STALE"


def test_prompt_compile_rejects_a_superseded_script(client: TestClient) -> None:
    first_script = client.post(
        "/api/projects/project_owned/scripts",
        headers=auth_headers("employee_1"),
        json={
            "source": "custom",
            "text": "旧口播稿。",
            "shot_card_version_id": "shot_card_v1",
        },
    ).json()
    latest_script = client.post(
        "/api/projects/project_owned/scripts",
        headers=auth_headers("employee_1"),
        json={
            "source": "custom",
            "text": "新口播稿。",
            "shot_card_version_id": "shot_card_v1",
        },
    )
    assert latest_script.status_code == 200

    response = client.post(
        "/api/projects/project_owned/prompts/compile",
        headers=auth_headers("employee_1"),
        json={
            "script_version_id": first_script["id"],
            "shot_card_version_id": "shot_card_v1",
            "first_frame_asset_id": "first_frame_owned",
            "output_duration_seconds": 10,
            "resolution": "768P",
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "SCRIPT_STALE"


def test_prompt_preview_compiles_the_latest_script_and_shot_card_without_persisting(
    client: TestClient,
) -> None:
    script = client.post(
        "/api/projects/project_owned/scripts",
        headers=auth_headers("employee_1"),
        json={
            "source": "custom",
            "text": "预览用的口播稿。",
            "shot_card_version_id": "shot_card_v1",
        },
    )
    assert script.status_code == 200

    response = client.post(
        "/api/projects/project_owned/prompts/preview",
        headers=auth_headers("employee_1"),
        json={},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["script_source"] == "script_version"
    assert body["shot_card_version_id"] == "shot_card_v1"
    assert body["output_duration_seconds"] == 10
    assert body["resolution"] == "768P"
    assert "生成一条 10 秒" in body["prompt_text"]
    assert "预览用的口播稿。" in body["prompt_text"]

    prompt_state = client.get(
        "/api/projects/project_owned/prompts/latest",
        headers=auth_headers("employee_1"),
    )
    assert prompt_state.status_code == 200
    assert prompt_state.json()["version"] is None


def test_prompt_preview_falls_back_to_the_original_script_from_the_analysis(
    client: TestClient,
) -> None:
    response = client.post(
        "/api/projects/project_owned/prompts/preview",
        headers=auth_headers("employee_1"),
        json={"output_duration_seconds": 4, "resolution": "2K"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["script_source"] == "analysis_original"
    assert body["shot_card_version_id"] == "shot_card_v1"
    assert body["output_duration_seconds"] == 4
    assert body["resolution"] == "2K"
    assert "生成一条 4 秒" in body["prompt_text"]
    assert "原始第一句" in body["prompt_text"]
    assert "原始第二句" in body["prompt_text"]


def test_prompt_preview_compiles_from_the_wrapped_analysis_payload(
    client: TestClient,
    db_path: Path,
) -> None:
    # 项目页新上传自动拆解的项目只有 analysis 版本（拆解落库为
    # {"analysis": {...}} 包装结构、无 shot_card），行内提示词预览必须能
    # 直接解包编译，而不是误报 SHOT_CARD_TIMELINE_INVALID。
    wrapped_analysis = {
        "schema_version": 1,
        "analysis": {
            "duration_seconds": 12,
            "original_script": "包装原稿第一句。包装原稿第二句。",
            "shots": [
                {
                    "shot_id": "S01",
                    "start_time": 0,
                    "end_time": 6,
                    "shot_type": "近景",
                    "composition": "人物居中",
                    "camera_motion": "固定",
                    "subject": "主讲人",
                    "action": "看镜头口播",
                    "scene": "室内",
                    "spoken_text": "包装原稿第一句。",
                    "transition": "硬切",
                },
                {
                    "shot_id": "S02",
                    "start_time": 6,
                    "end_time": 12,
                    "shot_type": "中景",
                    "composition": "三分法",
                    "camera_motion": "固定",
                    "subject": "主讲人",
                    "action": "继续讲解",
                    "scene": "室内",
                    "spoken_text": "包装原稿第二句。",
                    "transition": "硬切",
                },
            ],
        },
        "source_asset": {"id": "reference_owned", "storage_uri": "fake://reference.mp4"},
        "provider_response_ref": "resp_wrapped",
    }
    with connect_database(db_path) as conn:
        conn.execute(
            "DELETE FROM versions WHERE project_id = ? AND kind IN (?, ?)",
            ("project_owned", "shot_card", "script"),
        )
        conn.execute(
            """
            INSERT INTO versions (
                id, project_id, asset_id, kind, version_number, payload_json, created_by_user_id
            ) VALUES ('analysis_wrapped', 'project_owned', NULL, 'analysis', 3, ?, 'employee_1')
            """,
            (json.dumps(wrapped_analysis, ensure_ascii=True, sort_keys=True),),
        )
        conn.commit()

    response = client.post(
        "/api/projects/project_owned/prompts/preview",
        headers=auth_headers("employee_1"),
        json={},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["script_source"] == "analysis_original"
    assert body["shot_card_version_id"] is None
    assert body["output_duration_seconds"] == 12
    assert "包装原稿第一句" in body["prompt_text"]
    assert "包装原稿第二句" in body["prompt_text"]


def test_prompt_preview_requires_project_shots(client: TestClient) -> None:
    response = client.post(
        "/api/projects/project_other/prompts/preview",
        headers=auth_headers("employee_2"),
        json={},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "ANALYSIS_NOT_READY"


def test_prompt_compile_requires_the_currently_confirmed_first_frame(
    client: TestClient,
    db_path: Path,
) -> None:
    with connect_database(db_path) as conn:
        conn.execute(
            "DELETE FROM versions WHERE project_id = ? AND kind = ?",
            ("project_owned", "first_frame_selection"),
        )
        conn.commit()

    script = client.post(
        "/api/projects/project_owned/scripts",
        headers=auth_headers("employee_1"),
        json={
            "source": "custom",
            "text": "第一句。第二句。",
            "shot_card_version_id": "shot_card_v1",
        },
    ).json()
    response = client.post(
        "/api/projects/project_owned/prompts/compile",
        headers=auth_headers("employee_1"),
        json={
            "script_version_id": script["id"],
            "shot_card_version_id": "shot_card_v1",
            "first_frame_asset_id": "first_frame_owned",
            "output_duration_seconds": 10,
            "resolution": "768P",
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "FIRST_FRAME_CONFIRMATION_REQUIRED"


def test_prompt_must_be_locked_and_batch_keeps_locked_snapshot_without_provider_call(
    client: TestClient,
    db_path: Path,
) -> None:
    script = client.post(
        "/api/projects/project_owned/scripts",
        headers=auth_headers("employee_1"),
        json={
            "source": "custom",
            "text": "第一句。第二句。",
            "shot_card_version_id": "shot_card_v1",
        },
    ).json()
    prompt = client.post(
        "/api/projects/project_owned/prompts/compile",
        headers=auth_headers("employee_1"),
        json={
            "script_version_id": script["id"],
            "shot_card_version_id": "shot_card_v1",
            "first_frame_asset_id": "first_frame_owned",
            "output_duration_seconds": 10,
            "resolution": "768P",
        },
    ).json()

    rejected = client.post(
        "/api/projects/project_owned/generation-batches",
        headers=auth_headers("employee_1"),
        json={
            "quantity": 1,
            "prompt_version_id": prompt["id"],
            "first_frame_asset_id": "first_frame_owned",
            "output_duration_seconds": 10,
            "resolution": "768P",
            "idempotency_key": "draft-key",
        },
    )
    assert rejected.status_code == 409
    assert rejected.json()["detail"]["code"] == "PROMPT_NOT_LOCKED"

    prompt_id = create_locked_prompt(client)
    batch = client.post(
        "/api/projects/project_owned/generation-batches",
        headers=auth_headers("employee_1"),
        json={
            "quantity": 1,
            "prompt_version_id": prompt_id,
            "first_frame_asset_id": "first_frame_owned",
            "output_duration_seconds": 10,
            "resolution": "768P",
            "idempotency_key": "locked-key",
        },
    )

    assert batch.status_code == 200
    task = batch.json()["tasks"][0]
    assert batch.json()["status"] == "QUEUED"
    assert task["status"] == "PENDING"
    assert task["archive_status"] == "PENDING"
    assert task["result_asset_id"] is None
    assert task["prompt_snapshot"]["status"] == "LOCKED"
    with connect_database(db_path) as conn:
        stored = conn.execute(
            """
            SELECT prompt_snapshot_json, provider_task_id, provider_request_json
            FROM generation_tasks
            WHERE id = ?
            """,
            (task["id"],),
        ).fetchone()
    assert stored is not None
    assert json.loads(str(stored["prompt_snapshot_json"]))["status"] == "LOCKED"
    assert stored["provider_task_id"] is None
    assert stored["provider_request_json"] is None


@pytest.mark.parametrize(
    ("duration_seconds", "resolution"),
    [(15, "768P"), (10, "2K")],
)
def test_batch_rejects_parameters_that_differ_from_the_locked_prompt(
    client: TestClient,
    duration_seconds: int,
    resolution: str,
) -> None:
    prompt_id = create_locked_prompt(client)

    response = client.post(
        "/api/projects/project_owned/generation-batches",
        headers=auth_headers("employee_1"),
        json={
            "quantity": 1,
            "prompt_version_id": prompt_id,
            "first_frame_asset_id": "first_frame_owned",
            "output_duration_seconds": duration_seconds,
            "resolution": resolution,
            "idempotency_key": f"parameter-mismatch-{duration_seconds}-{resolution}",
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "PROMPT_PARAMETERS_MISMATCH"


def test_h3_template_hash_is_derived_from_the_rendering_spec() -> None:
    import app.generation as generation

    template_spec = getattr(generation, "H3_PROMPT_TEMPLATE_SPEC", None)
    assert template_spec is not None
    canonical_spec = json.dumps(
        template_spec,
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode()

    assert generation.H3_PROMPT_TEMPLATE_HASH == hashlib.sha256(canonical_spec).hexdigest()


def test_h3_request_contract_is_i2v_text_first_frame_adaptive_and_duration_guard() -> None:
    request = build_h3_request(
        prompt_text="生成一条短视频",
        first_frame_url="fake://generation-results/first-frame.png",
        duration_seconds=10,
        resolution="768P",
    )

    assert request == {
        "model": "MiniMax-H3",
        "content": [
            {"type": "text", "text": "生成一条短视频"},
            {
                "type": "image_url",
                "image_url": {"url": "fake://generation-results/first-frame.png"},
                "role": "first_frame",
            },
        ],
        "resolution": "768P",
        "duration": 10,
        "ratio": "adaptive",
    }
    with pytest.raises(ValueError, match="duration"):
        build_h3_request(
            prompt_text="生成一条短视频",
            first_frame_url="fake://generation-results/first-frame.png",
            duration_seconds=16,
            resolution="768P",
        )


def test_generation_batch_quantity_limits_idempotency_and_fake_archive(
    client: TestClient,
    db_path: Path,
) -> None:
    prompt_id = create_locked_prompt(client)
    first = client.post(
        "/api/projects/project_owned/generation-batches",
        headers=auth_headers("employee_1"),
        json={
            "quantity": 3,
            "prompt_version_id": prompt_id,
            "first_frame_asset_id": "first_frame_owned",
            "output_duration_seconds": 10,
            "resolution": "768P",
            "idempotency_key": "same-key",
        },
    )
    same = client.post(
        "/api/projects/project_owned/generation-batches",
        headers=auth_headers("employee_1"),
        json={
            "quantity": 3,
            "prompt_version_id": prompt_id,
            "first_frame_asset_id": "first_frame_owned",
            "output_duration_seconds": 10,
            "resolution": "768P",
            "idempotency_key": "same-key",
        },
    )
    conflict = client.post(
        "/api/projects/project_owned/generation-batches",
        headers=auth_headers("employee_1"),
        json={
            "quantity": 2,
            "prompt_version_id": prompt_id,
            "first_frame_asset_id": "first_frame_owned",
            "output_duration_seconds": 10,
            "resolution": "768P",
            "idempotency_key": "same-key",
        },
    )
    too_many = client.post(
        "/api/projects/project_owned/generation-batches",
        headers=auth_headers("employee_1"),
        json={
            "quantity": 5,
            "prompt_version_id": prompt_id,
            "first_frame_asset_id": "first_frame_owned",
            "output_duration_seconds": 10,
            "resolution": "768P",
            "idempotency_key": "too-many",
        },
    )

    assert first.status_code == 200
    assert len(first.json()["tasks"]) == 3
    assert same.status_code == 200
    assert same.json()["id"] == first.json()["id"]
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert too_many.status_code == 422
    assert too_many.json()["detail"]["code"] == "QUANTITY_EXCEEDS_LIMIT"

    with connect_database(db_path) as conn:
        task_rows = conn.execute(
            """
            SELECT status, archive_status, quality_status, result_asset_id, provider_request_json
            FROM generation_tasks
            WHERE batch_id = ?
            """,
            (first.json()["id"],),
        ).fetchall()
    assert len(task_rows) == 3
    assert {str(row["status"]) for row in task_rows} == {"PENDING"}
    assert {str(row["archive_status"]) for row in task_rows} == {"PENDING"}
    assert {str(row["quality_status"]) for row in task_rows} == {"PENDING"}
    assert {row["result_asset_id"] for row in task_rows} == {None}
    assert {row["provider_request_json"] for row in task_rows} == {None}

    storage = FakeStorageAdapter(provider="fake", bucket="generation-results")
    with connect_database(db_path) as conn:
        for _ in range(3):
            assert (
                run_next_generation_task(
                    conn,
                    worker_id="worker_a",
                    provider=FakeH3Provider(),
                    storage=storage,
                )
                is not None
            )
        request_row = conn.execute(
            """
            SELECT provider_request_json
            FROM generation_tasks
            WHERE batch_id = ?
            ORDER BY id
            LIMIT 1
            """,
            (first.json()["id"],),
        ).fetchone()
    request = json.loads(str(request_row["provider_request_json"]))
    assert request["content"][0]["type"] == "text"
    assert request["content"][1]["role"] == "first_frame"
    after_worker = client.get(
        f"/api/generation-batches/{first.json()['id']}",
        headers=auth_headers("employee_1"),
    ).json()
    assert after_worker["progress"]["terminal_count"] == 3
    assert after_worker["progress"]["progress_percent"] == 100
    assert {task["archive_status"] for task in after_worker["tasks"]} == {"ARCHIVED"}


def test_generation_batch_replay_ignores_a_later_lower_quantity_limit(
    client: TestClient,
    db_path: Path,
) -> None:
    prompt_id = create_locked_prompt(client)
    request = {
        "quantity": 3,
        "prompt_version_id": prompt_id,
        "first_frame_asset_id": "first_frame_owned",
        "output_duration_seconds": 10,
        "resolution": "768P",
        "idempotency_key": "limit-change-replay",
    }
    first = client.post(
        "/api/projects/project_owned/generation-batches",
        headers=auth_headers("employee_1"),
        json=request,
    )
    assert first.status_code == 200

    with connect_database(db_path) as conn:
        conn.execute("UPDATE runtime_settings SET max_generation_count_per_batch = 1 WHERE id = 1")
        conn.commit()

    replay = client.post(
        "/api/projects/project_owned/generation-batches",
        headers=auth_headers("employee_1"),
        json=request,
    )

    assert replay.status_code == 200
    assert replay.json()["id"] == first.json()["id"]


def test_generation_batch_create_route_is_unique_and_returns_batch_result(
    client: TestClient,
) -> None:
    matching_routes = [
        route for route in expanded_api_routes() if is_generation_batch_create_route(route)
    ]
    assert len(matching_routes) == 1

    prompt_id = create_locked_prompt(client)
    response = client.post(
        "/api/projects/project_owned/generation-batches",
        headers=auth_headers("employee_1"),
        json={
            "quantity": 1,
            "prompt_version_id": prompt_id,
            "first_frame_asset_id": "first_frame_owned",
            "output_duration_seconds": 10,
            "resolution": "768P",
            "idempotency_key": "unique-route",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "QUEUED"
    assert payload["progress"]["total_count"] == 1
    assert len(payload["tasks"]) == 1


def test_generation_batch_list_paginates_and_returns_safe_task_summaries(
    client: TestClient,
    db_path: Path,
) -> None:
    with connect_database(db_path) as conn:
        insert_generation_history(conn, batch_id="batch-history-01")
        insert_generation_history(
            conn,
            batch_id="batch-history-02",
            provider_task_id="pt-123",
        )
        insert_generation_history(
            conn,
            batch_id="batch-history-03",
            batch_status="NEEDS_ATTENTION",
            task_status="SUCCEEDED",
            archive_status="ARCHIVE_FAILED",
            quality_status="AUDIO_QUALITY_FAILED",
            quality_issue_codes=["AUDIO_QUALITY_FAILED"],
            provider_task_id="provider-sensitive-1234567890",
            result_asset_id="first_frame_owned",
            submitted_at="2026-08-16 09:59:55",
            started_at="2026-08-16 10:00:00",
            completed_at="2026-08-16 10:00:05",
        )
        insert_generation_history(
            conn,
            batch_id="batch-other-01",
            project_id="project_other",
            created_by_user_id="employee_2",
        )
        conn.commit()

    first_page = client.get(
        "/api/generation-batches?limit=2",
        headers=auth_headers("employee_1"),
    )

    assert first_page.status_code == 200
    payload = first_page.json()
    assert [item["id"] for item in payload["items"]] == [
        "batch-history-03",
        "batch-history-02",
    ]
    assert payload["next_cursor"]
    rich_batch = payload["items"][0]
    assert rich_batch["project_name"] == "Owned Project"
    assert rich_batch["created_by_display_name"] == "Employee One"
    assert rich_batch["prompt_version_id"] == "prompt-batch-history-03"
    assert rich_batch["needs_attention_count"] == 1
    assert rich_batch["has_results"] is True
    assert rich_batch["total_estimated_cost"] == 1.25
    assert rich_batch["total_actual_cost"] == 1.5
    task = rich_batch["tasks"][0]
    assert task["stage"] == "ARCHIVE_FAILED"
    assert task["provider_task_id_tail"] == "34567890"
    assert task["attempt"] == 2
    assert task["archive_retry_count"] == 1
    assert task["duration_seconds"] == 5.0
    assert task["quality_status"] == "AUDIO_QUALITY_FAILED"
    assert task["quality_issue_codes"] == ["AUDIO_QUALITY_FAILED"]
    assert task["error_message_redacted"] == "A redacted failure summary."
    assert "provider_task_id" not in task
    assert "provider_result_url" not in task
    assert "prompt_snapshot" not in task
    assert "download_url" not in task
    assert payload["items"][1]["tasks"][0]["provider_task_id_tail"] is None

    second_page = client.get(
        "/api/generation-batches",
        params={"limit": 2, "cursor": payload["next_cursor"]},
        headers=auth_headers("employee_1"),
    )

    assert second_page.status_code == 200
    assert [item["id"] for item in second_page.json()["items"]] == ["batch-history-01"]
    assert second_page.json()["next_cursor"] is None

    mismatched_cursor = client.get(
        "/api/generation-batches",
        params={
            "limit": 2,
            "cursor": payload["next_cursor"],
            "needs_attention": "true",
        },
        headers=auth_headers("employee_1"),
    )
    invalid_cursor = client.get(
        "/api/generation-batches?cursor=not-a-cursor",
        headers=auth_headers("employee_1"),
    )

    assert mismatched_cursor.status_code == 400
    assert mismatched_cursor.json()["detail"]["code"] == "CURSOR_FILTER_MISMATCH"
    assert invalid_cursor.status_code == 400
    assert invalid_cursor.json()["detail"]["code"] == "INVALID_CURSOR"


def test_generation_batch_list_filters_and_enforces_project_scope(
    client: TestClient,
    db_path: Path,
) -> None:
    with connect_database(db_path) as conn:
        insert_generation_history(
            conn,
            batch_id="batch-owned-normal",
            created_by_user_id="admin_1",
        )
        insert_generation_history(
            conn,
            batch_id="batch-owned-uncertain",
            batch_status="QUEUED",
            task_status="SUBMITTING",
        )
        mark_task_submission_uncertain(
            conn,
            task_id="batch-owned-uncertain-task",
            message="Submission result is unknown.",
        )
        insert_generation_history(
            conn,
            batch_id="batch-owned-quality-status-only",
            batch_status="NEEDS_ATTENTION",
            task_status="SUCCEEDED",
            archive_status="ARCHIVED",
            quality_status="AUDIO_QUALITY_FAILED",
        )
        insert_generation_history(
            conn,
            batch_id="batch-owned-superseded-quality",
            batch_status="COMPLETED_WITH_FAILURES",
            task_status="SUCCEEDED",
            archive_status="ARCHIVED",
            quality_status="AUDIO_QUALITY_FAILED",
        )
        conn.execute(
            "UPDATE generation_tasks SET superseded_by_task_id = ? WHERE batch_id = ?",
            ("replacement-task", "batch-owned-superseded-quality"),
        )
        insert_generation_history(
            conn,
            batch_id="batch-other-normal",
            project_id="project_other",
            created_by_user_id="employee_2",
        )
        conn.commit()

    employee = client.get(
        "/api/generation-batches",
        headers=auth_headers("employee_1"),
    )
    forbidden_project = client.get(
        "/api/generation-batches?project_id=project_other",
        headers=auth_headers("employee_1"),
    )
    attention = client.get(
        "/api/generation-batches?needs_attention=true",
        headers=auth_headers("employee_1"),
    )
    normal = client.get(
        "/api/generation-batches?needs_attention=false",
        headers=auth_headers("employee_1"),
    )
    by_creator = client.get(
        "/api/generation-batches?created_by_user_id=admin_1",
        headers=auth_headers("employee_1"),
    )
    by_status = client.get(
        "/api/generation-batches?status=NEEDS_ATTENTION",
        headers=auth_headers("employee_1"),
    )
    admin = client.get("/api/generation-batches", headers=auth_headers("admin_1"))
    auditor = client.get("/api/generation-batches", headers=auth_headers("auditor_1"))

    assert employee.status_code == 200
    assert {item["project_id"] for item in employee.json()["items"]} == {"project_owned"}
    assert {item["id"] for item in employee.json()["items"]} == {
        "batch-owned-normal",
        "batch-owned-quality-status-only",
        "batch-owned-superseded-quality",
        "batch-owned-uncertain",
    }
    assert forbidden_project.status_code == 403
    assert forbidden_project.json()["detail"]["code"] == "PROJECT_FORBIDDEN"
    assert [item["id"] for item in attention.json()["items"]] == [
        "batch-owned-uncertain",
        "batch-owned-quality-status-only",
    ]
    quality_status_only = next(
        item
        for item in attention.json()["items"]
        if item["id"] == "batch-owned-quality-status-only"
    )
    assert quality_status_only["needs_attention_count"] == 1
    assert quality_status_only["tasks"][0]["stage"] == "QUALITY_FAILED"
    assert [item["id"] for item in normal.json()["items"]] == [
        "batch-owned-superseded-quality",
        "batch-owned-normal",
    ]
    superseded = normal.json()["items"][0]
    assert superseded["needs_attention_count"] == 0
    assert superseded["tasks"][0]["available_actions"] == []
    assert [item["id"] for item in by_creator.json()["items"]] == ["batch-owned-normal"]
    assert [item["id"] for item in by_status.json()["items"]] == [
        "batch-owned-uncertain",
        "batch-owned-quality-status-only",
    ]
    assert {item["project_id"] for item in admin.json()["items"]} == {
        "project_owned",
        "project_other",
    }
    assert {item["project_id"] for item in auditor.json()["items"]} == {
        "project_owned",
        "project_other",
    }


def test_generation_batch_list_fetches_all_page_tasks_in_one_query(
    client: TestClient,
    db_path: Path,
) -> None:
    with connect_database(db_path) as conn:
        for index in range(25):
            insert_generation_history(
                conn,
                batch_id=f"batch-query-{index:02d}",
                created_at=f"2026-08-16 10:{index:02d}:00",
            )
        conn.commit()

    statements: list[str] = []
    original_override = app.dependency_overrides[get_database]

    def traced_database_override() -> Iterator[sqlite3.Connection]:
        conn = connect_database(db_path)
        conn.set_trace_callback(statements.append)
        try:
            yield conn
        finally:
            conn.close()

    app.dependency_overrides[get_database] = traced_database_override
    try:
        response = client.get(
            "/api/generation-batches?limit=20",
            headers=auth_headers("employee_1"),
        )
    finally:
        app.dependency_overrides[get_database] = original_override

    assert response.status_code == 200
    assert len(response.json()["items"]) == 20
    normalized = [" ".join(statement.upper().split()) for statement in statements]
    batch_queries = [
        statement for statement in normalized if "FROM GENERATION_BATCHES AS BATCH" in statement
    ]
    page_task_queries = [
        statement
        for statement in normalized
        if "FROM GENERATION_TASKS AS TASK" in statement and "TASK.BATCH_ID IN (" in statement
    ]
    assert len(batch_queries) == 1
    assert len(page_task_queries) == 1


def expanded_api_routes() -> list[APIRoute]:
    routes: list[APIRoute] = []
    for route in app.routes:
        if isinstance(route, APIRoute):
            routes.append(route)
            continue
        original_router = getattr(route, "original_router", None)
        if original_router is None:
            continue
        routes.extend(child for child in original_router.routes if isinstance(child, APIRoute))
    return routes


def is_generation_batch_create_route(route: APIRoute) -> bool:
    return route.path == "/api/projects/{project_id}/generation-batches" and "POST" in route.methods


def test_batch_progress_counts_archive_failed_and_audio_quality(
    client: TestClient,
    db_path: Path,
) -> None:
    prompt_id = create_locked_prompt(client)
    batch = client.post(
        "/api/projects/project_owned/generation-batches",
        headers=auth_headers("employee_1"),
        json={
            "quantity": 2,
            "prompt_version_id": prompt_id,
            "first_frame_asset_id": "first_frame_owned",
            "output_duration_seconds": 10,
            "resolution": "768P",
            "idempotency_key": "progress-key",
            "fake_audio_quality": "missing",
        },
    ).json()

    response = client.get(
        f"/api/generation-batches/{batch['id']}",
        headers=auth_headers("employee_1"),
    )

    assert response.status_code == 200
    progress = response.json()["progress"]
    assert progress["total_count"] == 2
    assert progress["terminal_count"] == 0
    assert progress["progress_percent"] == 0
    assert progress["counts"]["pending"] == 2

    with connect_database(db_path) as conn:
        assert (
            run_next_generation_task(
                conn,
                worker_id="worker_a",
                provider=FakeH3Provider(audio_quality="missing"),
                storage=FakeStorageAdapter(provider="fake", bucket="generation-results"),
            )
            is not None
        )
        assert (
            run_next_generation_task(
                conn,
                worker_id="worker_a",
                provider=FakeH3Provider(audio_quality="missing"),
                storage=FakeStorageAdapter(provider="fake", bucket="generation-results"),
            )
            is not None
        )

    after_worker = client.get(
        f"/api/generation-batches/{batch['id']}",
        headers=auth_headers("employee_1"),
    )
    progress = after_worker.json()["progress"]
    assert progress["terminal_count"] == 2
    assert progress["progress_percent"] == 100
    assert progress["counts"]["succeeded"] == 2
    assert progress["counts"]["needs_attention"] == 2
    assert {task["quality_status"] for task in after_worker.json()["tasks"]} == {
        "AUDIO_QUALITY_FAILED"
    }


def test_generation_requires_owner_and_configured_real_provider(client: TestClient) -> None:
    prompt_id = create_locked_prompt(client)
    other_owner = client.post(
        "/api/projects/project_owned/generation-batches",
        headers=auth_headers("employee_2"),
        json={
            "quantity": 1,
            "prompt_version_id": prompt_id,
            "first_frame_asset_id": "first_frame_owned",
            "output_duration_seconds": 10,
            "resolution": "768P",
            "idempotency_key": "other-owner",
        },
    )
    real_provider = client.post(
        "/api/projects/project_owned/generation-batches",
        headers=auth_headers("employee_1"),
        json={
            "quantity": 1,
            "prompt_version_id": prompt_id,
            "first_frame_asset_id": "first_frame_owned",
            "output_duration_seconds": 10,
            "resolution": "768P",
            "idempotency_key": "real-provider",
            "provider": "metaso",
        },
    )

    assert other_owner.status_code == 403
    assert other_owner.json()["detail"]["code"] == "PROJECT_FORBIDDEN"
    assert real_provider.status_code == 503
    assert real_provider.json()["detail"]["code"] == "METASO_SETTINGS_UNAVAILABLE"


def test_generation_can_queue_metaso_after_its_key_is_saved(
    client: TestClient,
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = Fernet.generate_key().decode("ascii")
    monkeypatch.setenv(SETTINGS_KEY_ENV, key)
    with connect_database(db_path) as conn:
        SettingsRepository(conn, fernet=Fernet(key.encode("ascii"))).save_provider_config(
            "metaso",
            {"api_key": "metaso-test-key"},
            actor_user_id="admin_1",
        )

    prompt_id = create_locked_prompt(client)
    response = client.post(
        "/api/projects/project_owned/generation-batches",
        headers=auth_headers("employee_1"),
        json={
            "quantity": 1,
            "prompt_version_id": prompt_id,
            "first_frame_asset_id": "first_frame_owned",
            "output_duration_seconds": 10,
            "resolution": "768P",
            "idempotency_key": "metaso-enabled",
            "provider": "metaso",
        },
    )

    assert response.status_code == 200
    assert response.json()["status"] == "QUEUED"
    assert response.json()["tasks"][0]["status"] == "PENDING"


def test_locked_prompt_is_consumed_by_only_one_distinct_idempotency_key(
    db_path: Path,
    client: TestClient,
) -> None:
    prompt_id = create_locked_prompt(client)
    barrier = threading.Barrier(2)
    results: list[int] = []
    results_lock = threading.Lock()

    def create_batch(key: str) -> None:
        barrier.wait()
        with connect_database(db_path) as conn:
            response = client.post(
                "/api/projects/project_owned/generation-batches",
                headers=auth_headers("employee_1"),
                json={
                    "quantity": 1,
                    "prompt_version_id": prompt_id,
                    "first_frame_asset_id": "first_frame_owned",
                    "output_duration_seconds": 10,
                    "resolution": "768P",
                    "idempotency_key": key,
                },
            )
            _ = conn
        with results_lock:
            results.append(response.status_code)

    threads = [
        threading.Thread(target=create_batch, args=("race-a",)),
        threading.Thread(target=create_batch, args=("race-b",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(results) == [200, 409]
    with connect_database(db_path) as conn:
        batch_count = conn.execute("SELECT COUNT(*) FROM generation_batches").fetchone()[0]
        task_count = conn.execute("SELECT COUNT(*) FROM generation_tasks").fetchone()[0]
    assert batch_count == 1
    assert task_count == 1


def test_worker_respects_runtime_concurrency_limit(db_path: Path, client: TestClient) -> None:
    prompt_id = create_locked_prompt(client)
    batch = client.post(
        "/api/projects/project_owned/generation-batches",
        headers=auth_headers("employee_1"),
        json={
            "quantity": 2,
            "prompt_version_id": prompt_id,
            "first_frame_asset_id": "first_frame_owned",
            "output_duration_seconds": 10,
            "resolution": "768P",
            "idempotency_key": "concurrency",
        },
    ).json()
    with connect_database(db_path) as conn:
        conn.execute(
            """
            UPDATE runtime_settings
            SET max_concurrent_h3_tasks = 1
            WHERE id = 1
            """
        )
        conn.execute(
            """
            UPDATE generation_tasks
            SET status = 'SUBMITTING', locked_by = 'busy-worker'
            WHERE id = (
                SELECT id
                FROM generation_tasks
                WHERE batch_id = ?
                ORDER BY id
                LIMIT 1
            )
            """,
            (batch["id"],),
        )
        conn.commit()

        assert (
            run_next_generation_task(
                conn,
                worker_id="worker_b",
                provider=FakeH3Provider(),
                storage=FakeStorageAdapter(provider="fake", bucket="generation-results"),
            )
            is None
        )


def test_concurrent_workers_cannot_exceed_runtime_concurrency_limit(
    db_path: Path,
    client: TestClient,
) -> None:
    prompt_id = create_locked_prompt(client)
    batch = client.post(
        "/api/projects/project_owned/generation-batches",
        headers=auth_headers("employee_1"),
        json={
            "quantity": 2,
            "prompt_version_id": prompt_id,
            "first_frame_asset_id": "first_frame_owned",
            "output_duration_seconds": 10,
            "resolution": "768P",
            "idempotency_key": "concurrent-workers",
        },
    ).json()
    with connect_database(db_path) as conn:
        conn.execute(
            """
            UPDATE runtime_settings
            SET max_concurrent_h3_tasks = 1
            WHERE id = 1
            """
        )
        conn.commit()

    provider_started = threading.Event()
    release_provider = threading.Event()

    class BlockingProvider(FakeH3Provider):
        def create_image_to_video(self, request: dict[str, Any]) -> H3CreateResult:
            provider_started.set()
            assert release_provider.wait(timeout=5)
            return super().create_image_to_video(request)

    results: list[str | None] = []
    results_lock = threading.Lock()

    def run_worker(worker_id: str, provider: FakeH3Provider) -> None:
        with connect_database(db_path) as conn:
            result = run_next_generation_task(
                conn,
                worker_id=worker_id,
                provider=provider,
                storage=FakeStorageAdapter(provider="fake", bucket="generation-results"),
            )
        with results_lock:
            results.append(None if result is None else result.status)

    first = threading.Thread(target=run_worker, args=("worker_a", BlockingProvider()))
    first.start()
    assert provider_started.wait(timeout=5)

    second = threading.Thread(target=run_worker, args=("worker_b", FakeH3Provider()))
    second.start()
    second.join(timeout=5)
    release_provider.set()
    first.join(timeout=5)

    assert sorted(results, key=lambda value: "" if value is None else value) == [None, "SUCCEEDED"]
    with connect_database(db_path) as conn:
        rows = conn.execute(
            """
            SELECT status
            FROM generation_tasks
            WHERE batch_id = ?
            ORDER BY status
            """,
            (batch["id"],),
        ).fetchall()

    assert [str(row["status"]) for row in rows] == ["PENDING", "SUCCEEDED"]


@pytest.mark.parametrize("stale_status", ["SUBMITTING", "RUNNING", "ARCHIVING"])
def test_worker_marks_expired_active_lease_for_manual_attention_without_resubmitting(
    db_path: Path,
    client: TestClient,
    stale_status: str,
) -> None:
    prompt_id = create_locked_prompt(client)
    batch = client.post(
        "/api/projects/project_owned/generation-batches",
        headers=auth_headers("employee_1"),
        json={
            "quantity": 1,
            "prompt_version_id": prompt_id,
            "first_frame_asset_id": "first_frame_owned",
            "output_duration_seconds": 10,
            "resolution": "768P",
            "idempotency_key": f"expired-{stale_status.lower()}",
        },
    ).json()

    expired_at = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
    with connect_database(db_path) as conn:
        conn.execute(
            """
            UPDATE generation_tasks
            SET
                status = ?,
                locked_by = 'dead-worker',
                locked_until = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE batch_id = ?
            """,
            (stale_status, expired_at, batch["id"]),
        )
        conn.commit()

        class FailIfCalledProvider(FakeH3Provider):
            def create_image_to_video(self, request: dict[str, Any]) -> H3CreateResult:
                raise AssertionError("expired active leases must not be submitted again")

        run_next_generation_task(
            conn,
            worker_id="recovery-worker",
            provider=FailIfCalledProvider(),
            storage=FakeStorageAdapter(provider="fake", bucket="generation-results"),
        )
        row = conn.execute(
            """
            SELECT status, error_code, locked_by, locked_until
            FROM generation_tasks
            WHERE batch_id = ?
            """,
            (batch["id"],),
        ).fetchone()

    assert row["status"] == "SUBMISSION_UNCERTAIN"
    assert row["error_code"] == "LEASE_EXPIRED_NEEDS_ATTENTION"
    assert row["locked_by"] is None
    assert row["locked_until"] is None


def test_worker_marks_submission_uncertain_without_auto_retry(
    db_path: Path,
    client: TestClient,
) -> None:
    prompt_id = create_locked_prompt(client)
    client.post(
        "/api/projects/project_owned/generation-batches",
        headers=auth_headers("employee_1"),
        json={
            "quantity": 1,
            "prompt_version_id": prompt_id,
            "first_frame_asset_id": "first_frame_owned",
            "output_duration_seconds": 10,
            "resolution": "768P",
            "idempotency_key": "uncertain",
        },
    )

    class UncertainProvider(FakeH3Provider):
        def create_image_to_video(self, request: dict[str, Any]) -> H3CreateResult:
            raise SubmissionUncertain("provider response was lost after submit")

    with connect_database(db_path) as conn:
        result = run_next_generation_task(
            conn,
            worker_id="worker_a",
            provider=UncertainProvider(),
            storage=FakeStorageAdapter(provider="fake", bucket="generation-results"),
        )
        assert result is not None
        second = run_next_generation_task(
            conn,
            worker_id="worker_b",
            provider=FakeH3Provider(),
            storage=FakeStorageAdapter(provider="fake", bucket="generation-results"),
        )
        row = conn.execute(
            """
            SELECT status, error_code, provider_task_id
            FROM generation_tasks
            """
        ).fetchone()

    assert second is None
    assert row["status"] == "SUBMISSION_UNCERTAIN"
    assert row["error_code"] == "SUBMISSION_UNCERTAIN"
    assert row["provider_task_id"] is None


def test_worker_fails_immediately_when_first_frame_url_cannot_be_signed(
    db_path: Path,
    client: TestClient,
) -> None:
    prompt_id = create_locked_prompt(client)
    batch = client.post(
        "/api/projects/project_owned/generation-batches",
        headers=auth_headers("employee_1"),
        json={
            "quantity": 1,
            "prompt_version_id": prompt_id,
            "first_frame_asset_id": "first_frame_owned",
            "output_duration_seconds": 10,
            "resolution": "768P",
            "idempotency_key": "first-frame-signing-failure",
        },
    ).json()

    class SigningFailureStorage(FakeStorageAdapter):
        def create_download_intent(self, *args: Any, **kwargs: Any) -> Any:
            raise StorageBackendUnavailable("temporary URL signer unavailable")

    class FailIfCalledProvider(FakeH3Provider):
        def create_image_to_video(self, request: dict[str, Any]) -> H3CreateResult:
            raise AssertionError("provider must not be called before the first-frame URL is signed")

    with connect_database(db_path) as conn:
        result = run_next_generation_task(
            conn,
            worker_id="worker_a",
            provider=FailIfCalledProvider(),
            storage=SigningFailureStorage(provider="fake", bucket="generation-results"),
        )
        row = conn.execute(
            """
            SELECT status, error_code, locked_by, locked_until, submitted_at, provider_task_id
            FROM generation_tasks
            """
        ).fetchone()

    assert result is not None
    assert result.status == "FAILED"
    assert row["status"] == "FAILED"
    assert row["error_code"] == "FIRST_FRAME_URL_SIGN_FAILED"
    assert row["locked_by"] is None
    assert row["locked_until"] is None
    assert row["submitted_at"] is None
    assert row["provider_task_id"] is None

    batch_status = client.get(
        f"/api/generation-batches/{batch['id']}",
        headers=auth_headers("employee_1"),
    ).json()
    assert batch_status["status"] == "COMPLETED_WITH_FAILURES"
    assert batch_status["progress"]["terminal_count"] == 1


def test_worker_records_a_known_h3_provider_failure(
    db_path: Path,
    client: TestClient,
) -> None:
    prompt_id = create_locked_prompt(client)
    client.post(
        "/api/projects/project_owned/generation-batches",
        headers=auth_headers("employee_1"),
        json={
            "quantity": 1,
            "prompt_version_id": prompt_id,
            "first_frame_asset_id": "first_frame_owned",
            "output_duration_seconds": 10,
            "resolution": "768P",
            "idempotency_key": "provider-known-failure",
        },
    )

    class FailedProvider(FakeH3Provider):
        def create_image_to_video(self, request: dict[str, Any]) -> H3CreateResult:
            raise H3ProviderFailed(
                "METASO returned failed", provider_task_id="metaso-task-1", terminal=True
            )

    with connect_database(db_path) as conn:
        result = run_next_generation_task(
            conn,
            worker_id="worker_a",
            provider=FailedProvider(),
            storage=FakeStorageAdapter(provider="fake", bucket="generation-results"),
        )
        row = conn.execute(
            "SELECT status, error_code, provider_task_id FROM generation_tasks"
        ).fetchone()

    assert result is not None
    assert result.status == "FAILED"
    assert row["status"] == "FAILED"
    assert row["error_code"] == "H3_PROVIDER_FAILED"
    assert row["provider_task_id"] == "metaso-task-1"


def test_worker_preserves_a_known_nonterminal_h3_task_for_manual_recovery(
    db_path: Path,
    client: TestClient,
) -> None:
    prompt_id = create_locked_prompt(client)
    client.post(
        "/api/projects/project_owned/generation-batches",
        headers=auth_headers("employee_1"),
        json={
            "quantity": 1,
            "prompt_version_id": prompt_id,
            "first_frame_asset_id": "first_frame_owned",
            "output_duration_seconds": 10,
            "resolution": "768P",
            "idempotency_key": "provider-recovery-needed",
        },
    )

    class RecoveryProvider(FakeH3Provider):
        def create_image_to_video(self, request: dict[str, Any]) -> H3CreateResult:
            raise H3ProviderFailed("METASO query timed out", provider_task_id="metaso-task-2")

    with connect_database(db_path) as conn:
        result = run_next_generation_task(
            conn,
            worker_id="worker_a",
            provider=RecoveryProvider(),
            storage=FakeStorageAdapter(provider="fake", bucket="generation-results"),
        )
        row = conn.execute(
            "SELECT status, error_code, provider_task_id FROM generation_tasks"
        ).fetchone()

    assert result is not None
    assert result.status == "SUBMISSION_UNCERTAIN"
    assert row["error_code"] == "SUBMISSION_UNCERTAIN"
    assert row["provider_task_id"] == "metaso-task-2"


def test_worker_loop_processes_all_queued_fake_tasks(
    db_path: Path,
    client: TestClient,
) -> None:
    prompt_id = create_locked_prompt(client)
    client.post(
        "/api/projects/project_owned/generation-batches",
        headers=auth_headers("employee_1"),
        json={
            "quantity": 2,
            "prompt_version_id": prompt_id,
            "first_frame_asset_id": "first_frame_owned",
            "output_duration_seconds": 10,
            "resolution": "768P",
            "idempotency_key": "worker-loop",
        },
    )

    with connect_database(db_path) as conn:
        processed = run_worker_once(
            conn,
            worker_id="worker-loop",
            storage=FakeStorageAdapter(provider="fake", bucket="generation-results"),
        )

    assert processed == 2


def test_worker_loop_can_stop_after_one_task_for_observable_progress(
    db_path: Path,
    client: TestClient,
) -> None:
    prompt_id = create_locked_prompt(client)
    created = client.post(
        "/api/projects/project_owned/generation-batches",
        headers=auth_headers("employee_1"),
        json={
            "quantity": 2,
            "prompt_version_id": prompt_id,
            "first_frame_asset_id": "first_frame_owned",
            "output_duration_seconds": 10,
            "resolution": "768P",
            "idempotency_key": "worker-loop-one-task",
        },
    ).json()
    storage = FakeStorageAdapter(provider="fake", bucket="generation-results")

    with connect_database(db_path) as conn:
        processed = run_worker_once(
            conn,
            worker_id="worker-loop-limited",
            storage=storage,
            max_tasks=1,
        )

    first_progress = client.get(
        f"/api/generation-batches/{created['id']}",
        headers=auth_headers("employee_1"),
    ).json()["progress"]
    with connect_database(db_path) as conn:
        remaining = run_worker_once(
            conn,
            worker_id="worker-loop-remainder",
            storage=storage,
            max_tasks=1,
        )

    assert processed == 1
    assert first_progress["terminal_count"] == 1
    assert first_progress["progress_percent"] == 50
    assert remaining == 1


@pytest.mark.parametrize(
    ("outcome", "expected_exception"),
    [
        ("provider_failed", H3ProviderFailed),
        ("submission_uncertain", SubmissionUncertain),
    ],
)
def test_fake_h3_provider_supports_deterministic_gate1_failures(
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
    expected_exception: type[Exception],
) -> None:
    monkeypatch.setenv("VIDEO_REPLICA_FAKE_H3_OUTCOME", outcome)
    request = build_h3_request(
        prompt_text="Gate 1 deterministic failure",
        first_frame_url="https://storage.example.test/first-frame.png",
        duration_seconds=10,
        resolution="768P",
    )

    with connect_database(db_path) as conn:
        provider = h3_provider_for_task(conn, "fake_h3")

    with pytest.raises(expected_exception):
        provider.create_image_to_video(request)


def test_fake_h3_provider_uses_explicit_gate1_result_fixture(
    db_path: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture_content = b"\x00\x00\x00\x18ftypisomvalid-gate1-video"
    fixture_path = tmp_path / "gate1-result.mp4"
    fixture_path.write_bytes(fixture_content)
    monkeypatch.setenv("VIDEO_REPLICA_FAKE_H3_RESULT_PATH", str(fixture_path))
    request = build_h3_request(
        prompt_text="Gate 1 playable result",
        first_frame_url="https://storage.example.test/first-frame.png",
        duration_seconds=10,
        resolution="768P",
    )

    with connect_database(db_path) as conn:
        provider = h3_provider_for_task(conn, "fake_h3")

    result = provider.create_image_to_video(request)
    assert result.result_content == fixture_content
    assert provider.download_result(result.result_url) == fixture_content


def test_worker_archives_result_with_cloud_like_storage(
    db_path: Path,
    client: TestClient,
) -> None:
    prompt_id = create_locked_prompt(client)
    client.post(
        "/api/projects/project_owned/generation-batches",
        headers=auth_headers("employee_1"),
        json={
            "quantity": 1,
            "prompt_version_id": prompt_id,
            "first_frame_asset_id": "first_frame_owned",
            "output_duration_seconds": 10,
            "resolution": "768P",
            "idempotency_key": "archive-closed",
        },
    )

    with connect_database(db_path) as conn:
        result = run_next_generation_task(
            conn,
            worker_id="worker_a",
            provider=FakeH3Provider(),
            storage=FakeStorageAdapter(provider="cos", bucket="generation-results"),
            first_frame_storage=FakeStorageAdapter(provider="fake", bucket="generation-results"),
        )
        row = conn.execute(
            """
            SELECT status, archive_status, result_asset_id, error_code
            FROM generation_tasks
            """
        ).fetchone()

    assert result is not None
    assert row["status"] == "SUCCEEDED"
    assert row["archive_status"] == "ARCHIVED"
    assert row["result_asset_id"] is not None
    assert row["error_code"] is None


def test_worker_archive_retry_recovers_after_initial_failure(
    db_path: Path, client: TestClient
) -> None:
    prompt_id = create_locked_prompt(client)
    client.post(
        "/api/projects/project_owned/generation-batches",
        headers=auth_headers("employee_1"),
        json={
            "quantity": 1,
            "prompt_version_id": prompt_id,
            "first_frame_asset_id": "first_frame_owned",
            "output_duration_seconds": 10,
            "resolution": "768P",
            "idempotency_key": "archive-retry",
        },
    )

    class FailingArchiveStorage(FakeStorageAdapter):
        def put_object(self, key: str, content: bytes, *, content_type: str):  # type: ignore[override]
            raise StorageBackendUnavailable("simulated archive outage")

    with connect_database(db_path) as conn:
        run_next_generation_task(
            conn,
            worker_id="worker_a",
            provider=FakeH3Provider(),
            storage=FailingArchiveStorage(provider="cos", bucket="generation-results"),
            first_frame_storage=FakeStorageAdapter(provider="fake", bucket="generation-results"),
        )
        row = conn.execute(
            """
            SELECT status, archive_status, provider_result_url, result_asset_id
            FROM generation_tasks
            """
        ).fetchone()
        assert row["status"] == "SUCCEEDED"
        assert row["archive_status"] == "ARCHIVE_FAILED"
        assert row["provider_result_url"] is not None
        assert row["result_asset_id"] is None

        result = run_next_generation_task(
            conn,
            worker_id="worker_a",
            provider=FakeH3Provider(),
            storage=FakeStorageAdapter(provider="cos", bucket="generation-results"),
            first_frame_storage=FakeStorageAdapter(provider="fake", bucket="generation-results"),
        )
        row2 = conn.execute(
            """
            SELECT status, archive_status, provider_result_url, result_asset_id
            FROM generation_tasks
            """
        ).fetchone()

    assert result is not None
    assert row2["status"] == "SUCCEEDED"
    assert row2["archive_status"] == "ARCHIVED"
    assert row2["provider_result_url"] is None
    assert row2["result_asset_id"] is not None


def test_archive_retry_download_failure_keeps_task_retryable(
    db_path: Path, client: TestClient
) -> None:
    prompt_id = create_locked_prompt(client)
    client.post(
        "/api/projects/project_owned/generation-batches",
        headers=auth_headers("employee_1"),
        json={
            "quantity": 1,
            "prompt_version_id": prompt_id,
            "first_frame_asset_id": "first_frame_owned",
            "output_duration_seconds": 10,
            "resolution": "768P",
            "idempotency_key": "archive-retry-fail",
        },
    )

    class FailingArchiveStorage(FakeStorageAdapter):
        def put_object(self, key: str, content: bytes, *, content_type: str):  # type: ignore[override]
            raise StorageBackendUnavailable("simulated archive outage")

    class FailingDownloadProvider(FakeH3Provider):
        def download_result(self, url: str) -> bytes:
            raise H3ProviderFailed("download failed")

    with connect_database(db_path) as conn:
        run_next_generation_task(
            conn,
            worker_id="worker_a",
            provider=FakeH3Provider(),
            storage=FailingArchiveStorage(provider="cos", bucket="generation-results"),
            first_frame_storage=FakeStorageAdapter(provider="fake", bucket="generation-results"),
        )
        run_next_generation_task(
            conn,
            worker_id="worker_a",
            provider=FailingDownloadProvider(),
            storage=FakeStorageAdapter(provider="cos", bucket="generation-results"),
            first_frame_storage=FakeStorageAdapter(provider="fake", bucket="generation-results"),
        )
        row = conn.execute(
            """
            SELECT status, archive_status, provider_result_url, next_poll_at
            FROM generation_tasks
            """
        ).fetchone()

    assert row["status"] == "SUCCEEDED"
    assert row["archive_status"] == "ARCHIVE_FAILED"
    assert row["provider_result_url"] is not None
    assert row["next_poll_at"] is not None


def test_archive_retry_with_missing_provider_settings_backs_off_not_fails(
    db_path: Path, client: TestClient
) -> None:
    prompt_id = create_locked_prompt(client)
    client.post(
        "/api/projects/project_owned/generation-batches",
        headers=auth_headers("employee_1"),
        json={
            "quantity": 1,
            "prompt_version_id": prompt_id,
            "first_frame_asset_id": "first_frame_owned",
            "output_duration_seconds": 10,
            "resolution": "768P",
            "idempotency_key": "archive-retry-settings",
        },
    )

    class FailingArchiveStorage(FakeStorageAdapter):
        def put_object(self, key: str, content: bytes, *, content_type: str):  # type: ignore[override]
            raise StorageBackendUnavailable("simulated archive outage")

    with connect_database(db_path) as conn:
        run_next_generation_task(
            conn,
            worker_id="worker_a",
            provider=FakeH3Provider(),
            storage=FailingArchiveStorage(provider="cos", bucket="generation-results"),
            first_frame_storage=FakeStorageAdapter(provider="fake", bucket="generation-results"),
        )
        # Simulate a paid METASO task whose provider settings vanished.
        conn.execute("UPDATE generation_tasks SET provider = 'metaso'")
        run_next_generation_task(
            conn,
            worker_id="worker_a",
            provider=None,
            storage=FakeStorageAdapter(provider="cos", bucket="generation-results"),
            first_frame_storage=FakeStorageAdapter(provider="fake", bucket="generation-results"),
        )
        row = conn.execute(
            """
            SELECT status, archive_status, provider_result_url, next_poll_at
            FROM generation_tasks
            """
        ).fetchone()

    assert row["status"] == "SUCCEEDED"
    assert row["archive_status"] == "ARCHIVE_FAILED"
    assert row["provider_result_url"] is not None
    assert row["next_poll_at"] is not None


def test_expired_archive_retry_lease_resets_to_retryable_not_uncertain(
    db_path: Path, client: TestClient
) -> None:
    prompt_id = create_locked_prompt(client)
    client.post(
        "/api/projects/project_owned/generation-batches",
        headers=auth_headers("employee_1"),
        json={
            "quantity": 1,
            "prompt_version_id": prompt_id,
            "first_frame_asset_id": "first_frame_owned",
            "output_duration_seconds": 10,
            "resolution": "768P",
            "idempotency_key": "archive-retry-lease",
        },
    )

    class FailingArchiveStorage(FakeStorageAdapter):
        def put_object(self, key: str, content: bytes, *, content_type: str):  # type: ignore[override]
            raise StorageBackendUnavailable("simulated archive outage")

    with connect_database(db_path) as conn:
        run_next_generation_task(
            conn,
            worker_id="worker_a",
            provider=FakeH3Provider(),
            storage=FailingArchiveStorage(provider="cos", bucket="generation-results"),
            first_frame_storage=FakeStorageAdapter(provider="fake", bucket="generation-results"),
        )
        # Simulate a worker crash mid-retry with an expired lease.
        conn.execute(
            """
            UPDATE generation_tasks
            SET status = 'SUBMITTING', locked_by = 'worker_a',
                locked_until = datetime('now', '-1 second')
            """
        )
        mark_expired_active_leases_needing_attention(conn)
        row = conn.execute("SELECT status, archive_status FROM generation_tasks").fetchone()

    assert row["status"] == "SUCCEEDED"
    assert row["archive_status"] == "ARCHIVE_FAILED"


class ReconcileSucceededProvider(MetasoH3Provider):
    def _query_task(self, provider_task_id: str) -> dict[str, Any]:
        return {
            "id": provider_task_id,
            "status": "succeeded",
            "content": {"url": "https://example.com/results/ok.mp4"},
        }

    def download_result(self, url: str) -> bytes:
        return b"reconciled-mp4-bytes"


class ReconcileRunningProvider(MetasoH3Provider):
    def _query_task(self, provider_task_id: str) -> dict[str, Any]:
        return {"id": provider_task_id, "status": "running"}


class ReconcileFailedProvider(MetasoH3Provider):
    def _query_task(self, provider_task_id: str) -> dict[str, Any]:
        return {"id": provider_task_id, "status": "failed"}


def test_reconcile_submission_uncertain_recovers_succeeded_result(
    db_path: Path, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.generation.socket.getaddrinfo", _fake_public_dns)
    prompt_id = create_locked_prompt(client)
    client.post(
        "/api/projects/project_owned/generation-batches",
        headers=auth_headers("employee_1"),
        json={
            "quantity": 1,
            "prompt_version_id": prompt_id,
            "first_frame_asset_id": "first_frame_owned",
            "output_duration_seconds": 10,
            "resolution": "768P",
            "idempotency_key": "reconcile-ok",
        },
    )

    with connect_database(db_path) as conn:
        task = conn.execute("SELECT id, batch_id FROM generation_tasks").fetchone()
        conn.execute(
            """
            UPDATE generation_tasks
            SET status = 'SUBMISSION_UNCERTAIN', error_code = 'SUBMISSION_UNCERTAIN',
                provider_task_id = 'pt-123'
            WHERE id = ?
            """,
            (str(task["id"]),),
        )
        result = reconcile_submission_uncertain_task(
            conn,
            task_id=str(task["id"]),
            batch_id=str(task["batch_id"]),
            project_id="project_owned",
            created_by_user_id="employee_1",
            storage_factory=lambda: FakeStorageAdapter(provider="cos", bucket="generation-results"),
            provider=ReconcileSucceededProvider(api_key="test-key"),
        )
        row = conn.execute(
            "SELECT status, archive_status, result_asset_id FROM generation_tasks"
        ).fetchone()

    assert result is not None
    assert row["status"] == "SUCCEEDED"
    assert row["archive_status"] == "ARCHIVED"
    assert row["result_asset_id"] is not None


def test_reconcile_route_is_idempotent_and_audited(
    db_path: Path,
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.generation_routes as generation_routes_module

    monkeypatch.setattr("app.generation.socket.getaddrinfo", _fake_public_dns)
    monkeypatch.setattr(
        "app.generation_routes.h3_provider_for_task",
        lambda _conn, _provider: ReconcileSucceededProvider(api_key="test-key"),
    )
    monkeypatch.setattr(
        generation_routes_module,
        "get_media_storage",
        lambda _conn: FakeStorageAdapter(provider="cos", bucket="generation-results"),
    )
    prompt_id = create_locked_prompt(client)
    created = client.post(
        "/api/projects/project_owned/generation-batches",
        headers=auth_headers("employee_1"),
        json={
            "quantity": 1,
            "prompt_version_id": prompt_id,
            "first_frame_asset_id": "first_frame_owned",
            "output_duration_seconds": 10,
            "resolution": "768P",
            "idempotency_key": "reconcile-route-idempotent",
        },
    )
    task_id = created.json()["tasks"][0]["id"]
    with connect_database(db_path) as conn:
        conn.execute(
            """
            UPDATE generation_tasks
            SET status = 'SUBMISSION_UNCERTAIN', error_code = 'SUBMISSION_UNCERTAIN',
                provider_task_id = 'provider-reconcile-route'
            WHERE id = ?
            """,
            (task_id,),
        )
        conn.commit()

    payload = {"idempotency_key": "reconcile-operation-1"}
    first = client.post(
        f"/api/generation-tasks/{task_id}/reconcile",
        headers=auth_headers("employee_1"),
        json=payload,
    )

    def unavailable_storage(*_args: object, **_kwargs: object) -> FakeStorageAdapter:
        raise HTTPException(
            status_code=503,
            detail={"code": "STORAGE_SETTINGS_UNAVAILABLE"},
        )

    def unavailable_provider(*_args: object, **_kwargs: object) -> FakeH3Provider:
        raise H3ProviderSettingsUnavailable("provider settings removed")

    monkeypatch.setattr(
        generation_routes_module,
        "get_media_storage",
        unavailable_storage,
        raising=False,
    )
    monkeypatch.setattr(
        generation_routes_module,
        "h3_provider_for_task",
        unavailable_provider,
    )
    replay = client.post(
        f"/api/generation-tasks/{task_id}/reconcile",
        headers=auth_headers("employee_1"),
        json=payload,
    )

    assert first.status_code == 200
    assert replay.status_code == 200
    assert first.json()["result_asset_id"] == replay.json()["result_asset_id"]
    with connect_database(db_path) as conn:
        result_asset_count = conn.execute(
            "SELECT COUNT(*) FROM assets WHERE kind = 'video' AND id = ?",
            (first.json()["result_asset_id"],),
        ).fetchone()[0]
        operation_count = conn.execute(
            "SELECT COUNT(*) FROM generation_task_operations WHERE task_id = ? AND action = ?",
            (task_id, "RECONCILE"),
        ).fetchone()[0]
        requested_audits = conn.execute(
            "SELECT COUNT(*) FROM audit_logs WHERE action = ? AND entity_id = ?",
            ("generation_task.reconcile_requested", task_id),
        ).fetchone()[0]
        completed_audits = conn.execute(
            "SELECT COUNT(*) FROM audit_logs WHERE action = ? AND entity_id = ?",
            ("generation_task.reconcile_archived", task_id),
        ).fetchone()[0]

    assert result_asset_count == 1
    assert operation_count == 1
    assert requested_audits == 1
    assert completed_audits == 1


@pytest.mark.parametrize("reuse_idempotency_key", [True, False])
def test_reconcile_route_recovers_an_abandoned_pending_reservation(
    db_path: Path,
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    reuse_idempotency_key: bool,
) -> None:
    import app.generation_routes as generation_routes_module

    monkeypatch.setattr(
        generation_routes_module,
        "h3_provider_for_task",
        lambda _conn, _provider: ReconcileFailedProvider(api_key="test-key"),
    )
    monkeypatch.setattr(
        generation_routes_module,
        "get_media_storage",
        lambda _conn: FakeStorageAdapter(provider="cos", bucket="generation-results"),
    )
    prompt_id = create_locked_prompt(client)
    created = client.post(
        "/api/projects/project_owned/generation-batches",
        headers=auth_headers("employee_1"),
        json={
            "quantity": 1,
            "prompt_version_id": prompt_id,
            "first_frame_asset_id": "first_frame_owned",
            "output_duration_seconds": 10,
            "resolution": "768P",
            "idempotency_key": "reconcile-stale-reservation-batch",
        },
    )
    task_id = created.json()["tasks"][0]["id"]
    operation_key = "reconcile-stale-reservation-operation"
    request_hash = generation_task_operation_hash(
        action="RECONCILE",
        task_id=task_id,
        payload={},
    )
    with connect_database(db_path) as conn:
        conn.execute(
            """
            UPDATE generation_tasks
            SET status = 'SUBMISSION_UNCERTAIN', error_code = 'SUBMISSION_UNCERTAIN',
                provider_task_id = 'provider-stale-reservation'
            WHERE id = ?
            """,
            (task_id,),
        )
        conn.execute(
            """
            INSERT INTO generation_task_operations (
                id, task_id, actor_user_id, action, idempotency_key,
                request_hash, result_task_id, result_status, updated_at
            )
            VALUES (
                'stale-reconcile-operation', ?, 'employee_1', 'RECONCILE', ?,
                ?, ?, 'PENDING', datetime('now', '-1 day')
            )
            """,
            (task_id, operation_key, request_hash, task_id),
        )
        conn.commit()

    response = client.post(
        f"/api/generation-tasks/{task_id}/reconcile",
        headers=auth_headers("employee_1"),
        json={
            "idempotency_key": (
                operation_key if reuse_idempotency_key else "replacement-reconcile-operation"
            )
        },
    )

    assert response.status_code == 200
    assert response.json()["status"] == "FAILED"
    with connect_database(db_path) as conn:
        operations = conn.execute(
            """
            SELECT id, result_status
            FROM generation_task_operations
            WHERE task_id = ? AND action = 'RECONCILE'
            """,
            (task_id,),
        ).fetchall()
        recovery_audits = conn.execute(
            """
            SELECT COUNT(*)
            FROM audit_logs
            WHERE action = 'generation_task.reconcile_stale_reservation_released'
              AND entity_id = ?
            """,
            (task_id,),
        ).fetchone()[0]

    assert len(operations) == 1
    assert operations[0]["result_status"] == "COMPLETED"
    assert operations[0]["id"] != "stale-reconcile-operation"
    assert recovery_audits == 1


def test_reconcile_provider_failure_does_not_require_storage_settings(
    db_path: Path,
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.generation_routes as generation_routes_module

    storage_calls = 0

    def unavailable_storage(*_args: object, **_kwargs: object) -> FakeStorageAdapter:
        nonlocal storage_calls
        storage_calls += 1
        raise HTTPException(
            status_code=503,
            detail={"code": "STORAGE_SETTINGS_UNAVAILABLE"},
        )

    monkeypatch.setattr(
        generation_routes_module,
        "h3_provider_for_task",
        lambda _conn, _provider: ReconcileFailedProvider(api_key="test-key"),
    )
    monkeypatch.setattr(
        generation_routes_module,
        "get_media_storage",
        unavailable_storage,
    )
    prompt_id = create_locked_prompt(client)
    created = client.post(
        "/api/projects/project_owned/generation-batches",
        headers=auth_headers("employee_1"),
        json={
            "quantity": 1,
            "prompt_version_id": prompt_id,
            "first_frame_asset_id": "first_frame_owned",
            "output_duration_seconds": 10,
            "resolution": "768P",
            "idempotency_key": "reconcile-without-storage-batch",
        },
    )
    task_id = created.json()["tasks"][0]["id"]
    with connect_database(db_path) as conn:
        conn.execute(
            """
            UPDATE generation_tasks
            SET status = 'SUBMISSION_UNCERTAIN', error_code = 'SUBMISSION_UNCERTAIN',
                provider_task_id = 'provider-failed-without-storage'
            WHERE id = ?
            """,
            (task_id,),
        )
        conn.commit()

    response = client.post(
        f"/api/generation-tasks/{task_id}/reconcile",
        headers=auth_headers("employee_1"),
        json={"idempotency_key": "reconcile-without-storage-operation"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "FAILED"
    assert storage_calls == 0


def test_reconcile_lost_reservation_cannot_finalize_an_archived_result(
    db_path: Path,
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.generation_routes as generation_routes_module

    uploaded_keys: list[str] = []
    replaced_reservation_ids: list[str] = []

    class TakeoverDuringArchiveStorage(FakeStorageAdapter):
        def put_object(self, key: str, content: bytes, *, content_type: str):  # type: ignore[override]
            stored = super().put_object(key, content, content_type=content_type)
            uploaded_keys.append(key)
            with connect_database(db_path) as takeover_conn:
                replaced = takeover_conn.execute(
                    """
                    SELECT id FROM generation_task_operations
                    WHERE task_id = ? AND action = 'RECONCILE' AND result_status = 'PENDING'
                    """,
                    (task_id,),
                ).fetchone()
                assert replaced is not None
                replaced_reservation_ids.append(str(replaced["id"]))
                takeover_conn.execute(
                    """
                    DELETE FROM generation_task_operations
                    WHERE task_id = ? AND action = 'RECONCILE' AND result_status = 'PENDING'
                    """,
                    (task_id,),
                )
                takeover_conn.execute(
                    """
                    INSERT INTO generation_task_operations (
                        id, task_id, actor_user_id, action, idempotency_key,
                        request_hash, result_task_id, result_status
                    )
                    VALUES (
                        'replacement-reconcile-reservation', ?, 'employee_1',
                        'RECONCILE', 'replacement-after-takeover', ?, ?, 'PENDING'
                    )
                    """,
                    (
                        task_id,
                        generation_task_operation_hash(
                            action="RECONCILE",
                            task_id=task_id,
                            payload={},
                        ),
                        task_id,
                    ),
                )
                takeover_conn.commit()
            return stored

    storage = TakeoverDuringArchiveStorage(
        provider="cos",
        bucket="generation-results",
    )
    monkeypatch.setattr("app.generation.socket.getaddrinfo", _fake_public_dns)
    monkeypatch.setattr(
        generation_routes_module,
        "h3_provider_for_task",
        lambda _conn, _provider: ReconcileSucceededProvider(api_key="test-key"),
    )
    monkeypatch.setattr(
        generation_routes_module,
        "get_media_storage",
        lambda _conn: storage,
    )
    prompt_id = create_locked_prompt(client)
    created = client.post(
        "/api/projects/project_owned/generation-batches",
        headers=auth_headers("employee_1"),
        json={
            "quantity": 1,
            "prompt_version_id": prompt_id,
            "first_frame_asset_id": "first_frame_owned",
            "output_duration_seconds": 10,
            "resolution": "768P",
            "idempotency_key": "reconcile-takeover-batch",
        },
    )
    task_id = created.json()["tasks"][0]["id"]
    with connect_database(db_path) as conn:
        conn.execute(
            """
            UPDATE generation_tasks
            SET status = 'SUBMISSION_UNCERTAIN', error_code = 'SUBMISSION_UNCERTAIN',
                provider_task_id = 'provider-reconcile-takeover'
            WHERE id = ?
            """,
            (task_id,),
        )
        conn.commit()

    response = client.post(
        f"/api/generation-tasks/{task_id}/reconcile",
        headers=auth_headers("employee_1"),
        json={"idempotency_key": "old-reconcile-reservation"},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "RECONCILE_RESERVATION_LOST"
    assert len(uploaded_keys) == 1
    assert uploaded_keys[0] == (f"generation-results/{task_id}/{replaced_reservation_ids[0]}.mp4")
    assert storage.head_object(uploaded_keys[0]) is None
    with connect_database(db_path) as conn:
        task_row = conn.execute(
            "SELECT status, archive_status, result_asset_id FROM generation_tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        asset_count = conn.execute(
            "SELECT COUNT(*) FROM assets WHERE project_id = 'project_owned' AND kind = 'video'"
        ).fetchone()[0]
        completed_audits = conn.execute(
            """
            SELECT COUNT(*) FROM audit_logs
            WHERE action = 'generation_task.reconcile_archived' AND entity_id = ?
            """,
            (task_id,),
        ).fetchone()[0]
        operations = conn.execute(
            """
            SELECT id, result_status FROM generation_task_operations
            WHERE task_id = ? AND action = 'RECONCILE'
            """,
            (task_id,),
        ).fetchall()

    assert task_row["status"] == "SUBMISSION_UNCERTAIN"
    assert task_row["archive_status"] == "PENDING"
    assert task_row["result_asset_id"] is None
    assert asset_count == 0
    assert completed_audits == 0
    assert [(row["id"], row["result_status"]) for row in operations] == [
        ("replacement-reconcile-reservation", "PENDING")
    ]


def test_reconcile_without_provider_task_id_requires_manual_confirmation(
    db_path: Path, client: TestClient
) -> None:
    prompt_id = create_locked_prompt(client)
    client.post(
        "/api/projects/project_owned/generation-batches",
        headers=auth_headers("employee_1"),
        json={
            "quantity": 1,
            "prompt_version_id": prompt_id,
            "first_frame_asset_id": "first_frame_owned",
            "output_duration_seconds": 10,
            "resolution": "768P",
            "idempotency_key": "reconcile-manual",
        },
    )

    with connect_database(db_path) as conn:
        task = conn.execute("SELECT id, batch_id FROM generation_tasks").fetchone()
        conn.execute(
            """
            UPDATE generation_tasks
            SET status = 'SUBMISSION_UNCERTAIN', error_code = 'SUBMISSION_UNCERTAIN',
                provider_task_id = NULL
            WHERE id = ?
            """,
            (str(task["id"]),),
        )
        with pytest.raises(HTTPException) as excinfo:
            reconcile_submission_uncertain_task(
                conn,
                task_id=str(task["id"]),
                batch_id=str(task["batch_id"]),
                project_id="project_owned",
                created_by_user_id="employee_1",
                storage_factory=lambda: FakeStorageAdapter(
                    provider="cos", bucket="generation-results"
                ),
                provider=ReconcileSucceededProvider(api_key="test-key"),
            )

    assert excinfo.value.status_code == 409
    assert excinfo.value.detail["code"] == "SUBMISSION_REQUIRES_MANUAL_CONFIRMATION"


def test_reconcile_running_task_keeps_uncertain(db_path: Path, client: TestClient) -> None:
    prompt_id = create_locked_prompt(client)
    client.post(
        "/api/projects/project_owned/generation-batches",
        headers=auth_headers("employee_1"),
        json={
            "quantity": 1,
            "prompt_version_id": prompt_id,
            "first_frame_asset_id": "first_frame_owned",
            "output_duration_seconds": 10,
            "resolution": "768P",
            "idempotency_key": "reconcile-running",
        },
    )

    with connect_database(db_path) as conn:
        task = conn.execute("SELECT id, batch_id FROM generation_tasks").fetchone()
        conn.execute(
            """
            UPDATE generation_tasks
            SET status = 'SUBMISSION_UNCERTAIN', error_code = 'SUBMISSION_UNCERTAIN',
                provider_task_id = 'pt-running'
            WHERE id = ?
            """,
            (str(task["id"]),),
        )
        with pytest.raises(HTTPException) as excinfo:
            reconcile_submission_uncertain_task(
                conn,
                task_id=str(task["id"]),
                batch_id=str(task["batch_id"]),
                project_id="project_owned",
                created_by_user_id="employee_1",
                storage_factory=lambda: FakeStorageAdapter(
                    provider="cos", bucket="generation-results"
                ),
                provider=ReconcileRunningProvider(api_key="test-key"),
            )

    assert excinfo.value.status_code == 409
    assert excinfo.value.detail["code"] == "PROVIDER_STILL_PROCESSING"


def _insert_locked_prompt_for_project(
    conn: sqlite3.Connection, *, prompt_id: str, project_id: str, first_frame_asset_id: str
) -> None:
    conn.execute(
        """
        INSERT INTO versions (
    id, project_id, asset_id, kind, version_number, payload_json, created_by_user_id
)
        VALUES (?, ?, ?, 'h3_prompt', 1, ?, 'admin_1')
        """,
        (
            prompt_id,
            project_id,
            first_frame_asset_id,
            json.dumps(
                {
                    "status": "LOCKED",
                    "first_frame_uri": f"fake://generation-results/{first_frame_asset_id}.png",
                    "first_frame_asset_id": first_frame_asset_id,
                    "prompt_text": "test prompt",
                    "content_hash": "test-hash",
                    "duration_seconds": 10,
                    "resolution": "768P",
                },
                ensure_ascii=True,
                sort_keys=True,
            ),
        ),
    )


def test_idempotency_key_is_scoped_per_project(db_path: Path, client: TestClient) -> None:
    with connect_database(db_path) as conn:
        _insert_locked_prompt_for_project(
            conn,
            prompt_id="prompt_a",
            project_id="project_owned",
            first_frame_asset_id="first_frame_owned",
        )
        _insert_locked_prompt_for_project(
            conn,
            prompt_id="prompt_b",
            project_id="project_other",
            first_frame_asset_id="first_frame_other",
        )
        # project_other needs its own candidates + confirmed first-frame
        # selection (project_owned already has both in seed_data).
        conn.execute(
            """
            INSERT INTO versions (
    id, project_id, asset_id, kind, version_number, payload_json, created_by_user_id
)
            VALUES (
                'ff_cand_other', 'project_other', 'first_frame_other', 'first_frame_candidates',
                1, ?, 'admin_1'
            )
            """,
            (
                json.dumps(
                    {"candidates": [{"asset_id": "first_frame_other"}]},
                    ensure_ascii=True,
                    sort_keys=True,
                ),
            ),
        )
        conn.execute(
            """
            INSERT INTO versions (
    id, project_id, asset_id, kind, version_number, payload_json, created_by_user_id
)
            VALUES (
                'ff_sel_other', 'project_other', 'first_frame_other', 'first_frame_selection',
                1, ?, 'admin_1'
            )
            """,
            (
                json.dumps(
                    {
                        "first_frame_candidates_version_id": "ff_cand_other",
                        "first_frame_asset_id": "first_frame_other",
                    },
                    ensure_ascii=True,
                    sort_keys=True,
                ),
            ),
        )

    batch_a = client.post(
        "/api/projects/project_owned/generation-batches",
        headers=auth_headers("admin_1"),
        json={
            "quantity": 1,
            "prompt_version_id": "prompt_a",
            "first_frame_asset_id": "first_frame_owned",
            "output_duration_seconds": 10,
            "resolution": "768P",
            "idempotency_key": "shared-key",
        },
    )
    batch_b = client.post(
        "/api/projects/project_other/generation-batches",
        headers=auth_headers("admin_1"),
        json={
            "quantity": 1,
            "prompt_version_id": "prompt_b",
            "first_frame_asset_id": "first_frame_other",
            "output_duration_seconds": 10,
            "resolution": "768P",
            "idempotency_key": "shared-key",
        },
    )

    assert batch_a.status_code == 200, batch_a.text
    assert batch_b.status_code == 200, batch_b.text
    assert batch_a.json()["id"] != batch_b.json()["id"]


def test_metaso_batch_requires_cloud_storage(
    db_path: Path, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VIDEO_REPLICA_SETTINGS_KEY", Fernet.generate_key().decode("ascii"))
    with connect_database(db_path) as conn:
        conn.execute("UPDATE runtime_settings SET active_storage_provider='local' WHERE id=1")
        SettingsRepository(conn).save_provider_config(
            "metaso", {"api_key": "metaso-key"}, actor_user_id="admin_1"
        )
    prompt_id = create_locked_prompt(client)
    response = client.post(
        "/api/projects/project_owned/generation-batches",
        headers=auth_headers("employee_1"),
        json={
            "quantity": 1,
            "prompt_version_id": prompt_id,
            "first_frame_asset_id": "first_frame_owned",
            "output_duration_seconds": 10,
            "resolution": "768P",
            "idempotency_key": "metaso-local",
            "provider": "metaso",
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "METASO_REQUIRES_CLOUD_STORAGE"


def test_archive_retry_exhausts_to_terminal_failure(db_path: Path, client: TestClient) -> None:
    prompt_id = create_locked_prompt(client)
    client.post(
        "/api/projects/project_owned/generation-batches",
        headers=auth_headers("employee_1"),
        json={
            "quantity": 1,
            "prompt_version_id": prompt_id,
            "first_frame_asset_id": "first_frame_owned",
            "output_duration_seconds": 10,
            "resolution": "768P",
            "idempotency_key": "archive-retry-exhaust",
        },
    )

    class FailingArchiveStorage(FakeStorageAdapter):
        def put_object(self, key: str, content: bytes, *, content_type: str):  # type: ignore[override]
            raise StorageBackendUnavailable("simulated archive outage")

    class FailingDownloadProvider(FakeH3Provider):
        def download_result(self, url: str) -> bytes:
            raise H3ProviderFailed("download failed")

    with connect_database(db_path) as conn:
        run_next_generation_task(
            conn,
            worker_id="worker_a",
            provider=FakeH3Provider(),
            storage=FailingArchiveStorage(provider="cos", bucket="generation-results"),
            first_frame_storage=FakeStorageAdapter(provider="fake", bucket="generation-results"),
        )
        for _ in range(MAX_ARCHIVE_RETRIES):
            # Each failed attempt backs off 60s via next_poll_at; fast-forward so
            # the next acquire picks the task up again.
            conn.execute("UPDATE generation_tasks SET next_poll_at = datetime('now', '-1 second')")
            run_next_generation_task(
                conn,
                worker_id="worker_a",
                provider=FailingDownloadProvider(),
                storage=FakeStorageAdapter(provider="cos", bucket="generation-results"),
                first_frame_storage=FakeStorageAdapter(
                    provider="fake", bucket="generation-results"
                ),
            )
        row = conn.execute(
            "SELECT status, archive_status, provider_result_url, error_code FROM generation_tasks"
        ).fetchone()

    assert row["status"] == "FAILED"
    assert row["error_code"] == "ARCHIVE_RETRY_EXHAUSTED"
    assert row["provider_result_url"] is None


def test_reconcile_route_guards_and_rejects_non_uncertain_task(
    db_path: Path, client: TestClient
) -> None:
    from app.media_routes import get_media_storage

    app.dependency_overrides[get_media_storage] = lambda: FakeStorageAdapter(
        provider="fake", bucket="g"
    )
    prompt_id = create_locked_prompt(client)
    client.post(
        "/api/projects/project_owned/generation-batches",
        headers=auth_headers("employee_1"),
        json={
            "quantity": 1,
            "prompt_version_id": prompt_id,
            "first_frame_asset_id": "first_frame_owned",
            "output_duration_seconds": 10,
            "resolution": "768P",
            "idempotency_key": "reconcile-route",
        },
    )
    with connect_database(db_path) as conn:
        task_id = str(conn.execute("SELECT id FROM generation_tasks").fetchone()["id"])

    # Missing task -> 404
    missing = client.post(
        "/api/generation-tasks/not-exist/reconcile", headers=auth_headers("employee_1")
    )
    assert missing.status_code == 404

    # Auditor is read-only -> 403
    auditor = client.post(
        f"/api/generation-tasks/{task_id}/reconcile", headers=auth_headers("auditor_1")
    )
    assert auditor.status_code == 403

    # A non-UNCERTAIN task is rejected before any provider call -> 409
    normal = client.post(
        f"/api/generation-tasks/{task_id}/reconcile",
        headers=auth_headers("employee_1"),
        json={"idempotency_key": "reconcile-normal-task"},
    )
    assert normal.status_code == 409
    assert normal.json()["detail"]["code"] == "TASK_NOT_UNCERTAIN"


def _insert_result_asset(
    conn: sqlite3.Connection,
    asset_id: str,
    project_id: str = "project_owned",
) -> None:
    conn.execute(
        """
        INSERT INTO assets (
            id, project_id, kind, storage_uri, sha256, size_bytes,
            content_type, created_by_user_id
        )
        VALUES (?, ?, 'video', ?, ?, 12, 'video/mp4', 'employee_1')
        """,
        (asset_id, project_id, f"fake://generation-results/{asset_id}.mp4", f"sha-{asset_id}"),
    )


def test_generation_batch_rename_by_creator_or_admin_only(
    client: TestClient,
    db_path: Path,
) -> None:
    with connect_database(db_path) as conn:
        insert_generation_history(conn, batch_id="batch-rename")
        conn.commit()

    renamed = client.patch(
        "/api/generation-batches/batch-rename/name",
        headers=auth_headers("employee_1"),
        json={"display_name": "乡墅爆款第 2 期"},
    )
    other_creator = client.patch(
        "/api/generation-batches/batch-rename/name",
        headers=auth_headers("employee_2"),
        json={"display_name": "不应生效"},
    )
    auditor = client.patch(
        "/api/generation-batches/batch-rename/name",
        headers=auth_headers("auditor_1"),
        json={"display_name": "不应生效"},
    )
    admin = client.patch(
        "/api/generation-batches/batch-rename/name",
        headers=auth_headers("admin_1"),
        json={"display_name": "管理员改名"},
    )
    blank = client.patch(
        "/api/generation-batches/batch-rename/name",
        headers=auth_headers("employee_1"),
        json={"display_name": "   "},
    )
    overlong = client.patch(
        "/api/generation-batches/batch-rename/name",
        headers=auth_headers("employee_1"),
        json={"display_name": "超" * 121},
    )

    assert renamed.status_code == 200
    assert renamed.json()["display_name"] == "乡墅爆款第 2 期"
    assert other_creator.status_code == 403
    assert other_creator.json()["detail"]["code"] == "GENERATION_BATCH_FORBIDDEN"
    assert auditor.status_code == 403
    assert admin.status_code == 200
    assert admin.json()["display_name"] == "管理员改名"
    assert blank.status_code == 422
    assert blank.json()["detail"]["code"] == "GENERATION_BATCH_NAME_REQUIRED"
    assert overlong.status_code == 422

    detail = client.get("/api/generation-batches/batch-rename", headers=auth_headers("employee_1"))
    listed = client.get("/api/generation-batches", headers=auth_headers("employee_1"))
    with connect_database(db_path) as conn:
        audits = conn.execute(
            """
            SELECT metadata_json FROM audit_logs
            WHERE action = 'generation_batch.rename'
            ORDER BY created_at, id
            """
        ).fetchall()

    assert detail.status_code == 200
    assert detail.json()["display_name"] == "管理员改名"
    assert listed.status_code == 200
    assert listed.json()["items"][0]["display_name"] == "管理员改名"
    # 同秒写入的两条审计按 uuid 主键排序顺序不稳定，断言用集合比较。
    recorded = [json.loads(str(row["metadata_json"])) for row in audits]
    assert sorted(recorded, key=lambda item: str(item["from_name"])) == [
        {"from_name": None, "to_name": "乡墅爆款第 2 期"},
        {"from_name": "乡墅爆款第 2 期", "to_name": "管理员改名"},
    ]


def test_generation_batch_delete_removes_tasks_and_result_assets(
    client: TestClient,
    db_path: Path,
) -> None:
    with connect_database(db_path) as conn:
        _insert_result_asset(conn, "asset-del")
        insert_generation_history(
            conn,
            batch_id="batch-del",
            task_status="FAILED",
            result_asset_id="asset-del",
        )
        conn.commit()

    response = client.delete(
        "/api/generation-batches/batch-del", headers=auth_headers("employee_1")
    )

    assert response.status_code == 204
    with connect_database(db_path) as conn:
        batch = conn.execute(
            "SELECT id FROM generation_batches WHERE id = ?", ("batch-del",)
        ).fetchone()
        task = conn.execute(
            "SELECT id FROM generation_tasks WHERE batch_id = ?", ("batch-del",)
        ).fetchone()
        asset = conn.execute("SELECT id FROM assets WHERE id = ?", ("asset-del",)).fetchone()
        audit = conn.execute(
            "SELECT metadata_json FROM audit_logs WHERE action = 'generation_batch.delete'"
        ).fetchone()
    assert batch is None
    assert task is None
    assert asset is None
    assert audit is not None
    metadata = json.loads(str(audit["metadata_json"]))
    assert metadata == {
        "project_id": "project_owned",
        "deleted_task_count": 1,
        "deleted_asset_count": 1,
        # fake:// 存储后端不可用，尽力清理失败但删除不被阻塞。
        "storage_cleanup_failed_count": 1,
    }


def test_generation_batch_delete_blocked_while_tasks_active(
    client: TestClient,
    db_path: Path,
) -> None:
    with connect_database(db_path) as conn:
        insert_generation_history(conn, batch_id="batch-active")
        conn.commit()

    response = client.delete(
        "/api/generation-batches/batch-active", headers=auth_headers("employee_1")
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "BATCH_DELETE_HAS_ACTIVE_TASKS"


def test_generation_batch_delete_forbidden_for_non_creator_and_auditor(
    client: TestClient,
    db_path: Path,
) -> None:
    with connect_database(db_path) as conn:
        insert_generation_history(
            conn,
            batch_id="batch-guard",
            task_status="FAILED",
        )
        conn.commit()

    other = client.delete("/api/generation-batches/batch-guard", headers=auth_headers("employee_2"))
    auditor = client.delete(
        "/api/generation-batches/batch-guard", headers=auth_headers("auditor_1")
    )
    missing = client.delete("/api/generation-batches/not-exist", headers=auth_headers("employee_1"))
    admin = client.delete("/api/generation-batches/batch-guard", headers=auth_headers("admin_1"))

    assert other.status_code == 403
    assert other.json()["detail"]["code"] == "GENERATION_BATCH_FORBIDDEN"
    assert auditor.status_code == 403
    assert missing.status_code == 404
    assert admin.status_code == 204
