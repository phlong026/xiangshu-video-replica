from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from cryptography.fernet import Fernet
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app.auth import get_database
from app.db import connect_database, initialize_database
from app.generation import (
    FakeH3Provider,
    H3CreateResult,
    H3ProviderFailed,
    MetasoH3Provider,
    SubmissionUncertain,
    build_h3_request,
    mark_expired_active_leases_needing_attention,
    run_next_generation_task,
)
from app.generation_routes import get_h3_provider
from app.generation_worker import run_worker_once
from app.main import app
from app.settings import SETTINGS_KEY_ENV, SettingsRepository
from app.storage import FakeStorageAdapter, StorageBackendUnavailable


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


def test_metaso_h3_provider_creates_polls_filters_and_downloads_result() -> None:
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
        "resolution": "768",
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


def test_script_maps_spoken_text_to_shots_without_deleting_user_text(client: TestClient) -> None:
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
            "output_duration_seconds": 5,
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
