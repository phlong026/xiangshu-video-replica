from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from test_generation import auth_headers, create_locked_prompt, seed_data

from app.auth import get_database
from app.db import connect_database, initialize_database
from app.generation import FakeH3Provider, H3CreateResult, run_next_generation_task
from app.generation_routes import get_h3_provider
from app.main import app
from app.storage import DownloadIntent, FakeStorageAdapter


class RecordingFakeProvider(FakeH3Provider):
    def __init__(self) -> None:
        super().__init__()
        self.requests: list[dict[str, Any]] = []

    def create_image_to_video(self, request: dict[str, Any]) -> H3CreateResult:
        self.requests.append(request)
        return super().create_image_to_video(request)


class RecordingDownloadStorage(FakeStorageAdapter):
    def __init__(self) -> None:
        super().__init__(provider="fake", bucket="generation-results")
        self.download_keys: list[str] = []
        self.signed_first_frame_url = "https://storage.example.test/signed/first-frame.png"

    def create_download_intent(
        self,
        key: str,
        *,
        expires_in: timedelta,
        can_read: bool,
    ) -> DownloadIntent:
        self.download_keys.append(key)
        assert can_read is True
        assert expires_in <= timedelta(minutes=15)
        return DownloadIntent(
            method="GET",
            url=self.signed_first_frame_url,
            key=key,
            expires_at=datetime.now(UTC) + expires_in,
        )


@pytest.fixture()
def db_path(tmp_path: Path) -> Iterator[Path]:
    db_path = tmp_path / "generation-e2e.db"
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


def test_fake_provider_e2e_from_locked_prompt_to_worker_progress(
    client: TestClient,
    db_path: Path,
) -> None:
    prompt_id = create_locked_prompt(
        client,
        script_text="开场说明产品价值。结尾给出行动建议。",
    )
    first_batch = create_batch(client, prompt_id=prompt_id, idempotency_key="fake-e2e")
    same_batch = create_batch(client, prompt_id=prompt_id, idempotency_key="fake-e2e")

    assert same_batch["id"] == first_batch["id"]
    assert first_batch["status"] == "QUEUED"
    assert first_batch["progress"] == {
        "total_count": 2,
        "terminal_count": 0,
        "progress_percent": 0,
        "counts": {
            "pending": 2,
            "submitting": 0,
            "queued": 0,
            "running": 0,
            "archiving": 0,
            "succeeded": 0,
            "failed": 0,
            "cancelled": 0,
            "needs_attention": 0,
        },
        "historical_counts": {
            "archive_failed": 0,
            "audio_quality_failed": 0,
            "failed": 0,
            "superseded": 0,
        },
    }
    assert [task["status"] for task in first_batch["tasks"]] == ["PENDING", "PENDING"]
    assert {task["prompt_snapshot"]["status"] for task in first_batch["tasks"]} == {"LOCKED"}

    provider = RecordingFakeProvider()
    storage = FakeStorageAdapter(provider="fake", bucket="generation-results")
    with connect_database(db_path) as conn:
        assert run_next_generation_task(
            conn,
            worker_id="worker-a",
            provider=provider,
            storage=storage,
        )
        assert run_next_generation_task(
            conn,
            worker_id="worker-a",
            provider=provider,
            storage=storage,
        )
        assert (
            run_next_generation_task(
                conn,
                worker_id="worker-a",
                provider=provider,
                storage=storage,
            )
            is None
        )
        stored_tasks = load_batch_tasks(conn, batch_id=first_batch["id"])

    assert len(provider.requests) == 2
    assert provider.requests[0] == provider.requests[1]
    assert provider.requests[0]["model"] == "MiniMax-H3"
    assert provider.requests[0]["ratio"] == "adaptive"
    assert provider.requests[0]["content"][0]["type"] == "text"
    assert provider.requests[0]["content"][1]["role"] == "first_frame"
    assert all(row["provider_task_id"].startswith("fake-h3-") for row in stored_tasks)
    assert all(
        json.loads(row["provider_request_json"]) == provider.requests[0] for row in stored_tasks
    )

    after_worker = client.get(
        f"/api/generation-batches/{first_batch['id']}",
        headers=auth_headers("employee_1"),
    )

    assert after_worker.status_code == 200
    payload = after_worker.json()
    assert payload["status"] == "SUCCEEDED"
    assert payload["progress"]["terminal_count"] == 2
    assert payload["progress"]["progress_percent"] == 100
    assert {task["status"] for task in payload["tasks"]} == {"SUCCEEDED"}
    assert {task["archive_status"] for task in payload["tasks"]} == {"ARCHIVED"}
    assert all(task["result_asset_id"] for task in payload["tasks"])
    assert all("result_url" not in task for task in payload["tasks"])


def test_worker_uses_temporary_storage_download_url_for_h3_first_frame(
    client: TestClient,
    db_path: Path,
) -> None:
    prompt_id = create_locked_prompt(client)
    batch = create_batch(client, prompt_id=prompt_id, idempotency_key="signed-first-frame")

    provider = RecordingFakeProvider()
    storage = RecordingDownloadStorage()
    with connect_database(db_path) as conn:
        result = run_next_generation_task(
            conn,
            worker_id="worker-signed-url",
            provider=provider,
            storage=storage,
        )

    assert result is not None
    assert (
        batch["tasks"][0]["prompt_snapshot"]["first_frame_uri"]
        == "fake://generation-results/first-frame.png"
    )
    assert storage.download_keys, "worker must ask storage for a short-lived first-frame URL"
    assert provider.requests[0]["content"][1]["image_url"]["url"] == storage.signed_first_frame_url


def test_generation_flow_never_creates_independent_audio_tasks(
    client: TestClient,
    db_path: Path,
) -> None:
    prompt_id = create_locked_prompt(client)
    batch = create_batch(client, prompt_id=prompt_id, idempotency_key="no-audio-task")

    with connect_database(db_path) as conn:
        rows = conn.execute(
            """
            SELECT generation_mode, provider_request_json, prompt_snapshot_json
            FROM generation_tasks
            WHERE batch_id = ?
            """,
            (batch["id"],),
        ).fetchall()
        script_payload = conn.execute(
            """
            SELECT payload_json
            FROM versions
            WHERE kind = 'script'
            ORDER BY created_at DESC, version_number DESC
            LIMIT 1
            """
        ).fetchone()

    assert script_payload is not None
    assert json.loads(str(script_payload["payload_json"]))["creates_audio_task"] is False
    assert len(rows) == 2
    assert {row["generation_mode"] for row in rows} == {"I2V"}
    assert {row["provider_request_json"] for row in rows} == {None}
    assert all("audio" not in str(row["prompt_snapshot_json"]).lower() for row in rows)


def create_batch(client: TestClient, *, prompt_id: str, idempotency_key: str) -> dict[str, Any]:
    response = client.post(
        "/api/projects/project_owned/generation-batches",
        headers=auth_headers("employee_1"),
        json={
            "quantity": 2,
            "prompt_version_id": prompt_id,
            "first_frame_asset_id": "first_frame_owned",
            "output_duration_seconds": 10,
            "resolution": "768P",
            "idempotency_key": idempotency_key,
        },
    )
    assert response.status_code == 200
    return dict(response.json())


def load_batch_tasks(conn: sqlite3.Connection, *, batch_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT provider_task_id, provider_request_json
        FROM generation_tasks
        WHERE batch_id = ?
        ORDER BY id
        """,
        (batch_id,),
    ).fetchall()
