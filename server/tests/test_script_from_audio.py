"""工作台"提取文案"（script-from-audio）管线合同。

锁定的关键行为：
- 幂等提交与项目级并发互斥（409）；
- 审计/权限（auditor 禁写、跨项目禁读）；
- Worker 全链：取原视频 → 抽音轨（测试注入）→ 临时对象上传 → ASR →
  **临时音频即删**（成功与失败终态都无残留）；
- 转写全文进 result_json，绝不写生成门禁管制的脚本版本表；
- ffmpeg/ffprobe 缺失时任务 fail-closed 并给出可读错误。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.asr import AsrProviderError, TranscriptResult
from app.auth import get_database
from app.db import connect_database, initialize_database
from app.db_portable import BusinessConnection
from app.generation_worker import run_worker_once
from app.main import app
from app.storage import FakeStorageAdapter


@pytest.fixture()
def db_path(tmp_path: Path) -> Iterator[Path]:
    db_path = tmp_path / "script-from-audio.db"
    with initialize_database(db_path) as conn:
        seed_data(conn)
    yield db_path


@pytest.fixture()
def client(
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    monkeypatch.setenv("VIDEO_REPLICA_DB_PATH", str(db_path))
    monkeypatch.setenv("VIDEO_REPLICA_ASR_PROVIDER", "fake")

    def database_override() -> Iterator[BusinessConnection]:
        conn = BusinessConnection.sqlite(connect_database(db_path))
        try:
            yield conn
        finally:
            conn.close()

    app.dependency_overrides[get_database] = database_override
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
            ("auditor_1", "auditor_1", "Auditor One", "auditor"),
        ],
    )
    conn.execute(
        "INSERT INTO projects (id, owner_user_id, name) VALUES (?, ?, ?)",
        ("project_owned", "employee_1", "Owned Project"),
    )
    for asset_id in ("asset_video", "asset_video_2"):
        conn.execute(
            """
            INSERT INTO assets (
                id, project_id, kind, storage_uri, sha256, size_bytes,
                content_type, created_by_user_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                asset_id,
                "project_owned",
                "reference_video",
                f"fake://private-bucket/projects/project_owned/uploads/{asset_id}/src.mp4",
                "",
                0,
                "video/mp4",
                "employee_1",
            ),
        )


def auth_headers(user_id: str) -> dict[str, str]:
    return {"X-Dev-User-Id": user_id}


def enqueue(client: TestClient, **overrides: str) -> object:
    body = {"source_asset_id": "asset_video"}
    body.update(overrides)
    return client.post(
        "/api/projects/project_owned/script-from-audio",
        json=body,
        headers=auth_headers("employee_1"),
    )


class StubMediaTools:
    """测试注入：替换 ffmpeg 抽取与 ffprobe 探测，避免依赖真实二进制。"""

    def __init__(self) -> None:
        self.extracted: list[Path] = []
        self.probed: list[Path] = []

    def extract_audio(self, ffmpeg_path: str, video_path: Path, audio_path: Path) -> None:
        self.extracted.append(video_path)
        audio_path.write_bytes(b"fake-audio-bytes")

    def probe_duration_seconds(self, ffprobe_path: str, media_path: Path) -> float | None:
        self.probed.append(media_path)
        return 12.5


@pytest.fixture()
def stub_media(monkeypatch: pytest.MonkeyPatch) -> StubMediaTools:
    stub = StubMediaTools()
    import app.script_from_audio as domain

    monkeypatch.setattr(domain, "extract_audio", stub.extract_audio)
    monkeypatch.setattr(domain, "probe_duration_seconds", stub.probe_duration_seconds)
    monkeypatch.setattr(domain, "resolve_media_binary", lambda tool: f"/stub/{tool}")
    return stub


def test_enqueue_returns_202_and_replays_idempotently(client: TestClient) -> None:
    first = enqueue(client)
    assert first.status_code == 202
    created = first.json()
    assert created["status"] == "PENDING"
    assert created["result"] is None

    # 同内容的新提交按设计返回同一个活跃任务。
    replay = enqueue(client)
    assert replay.status_code == 202
    assert replay.json()["id"] == created["id"]

    # 内容变化的新提交撞上活跃任务 → 409。
    conflict = enqueue(client, source_asset_id="asset_video_2")
    assert conflict.status_code == 409

    with_idem = client.post(
        "/api/projects/project_owned/script-from-audio",
        json={"source_asset_id": "asset_video", "idempotency_key": "key-1"},
        headers=auth_headers("employee_1"),
    )
    assert with_idem.status_code == 202
    replayed = client.post(
        "/api/projects/project_owned/script-from-audio",
        json={"source_asset_id": "asset_video", "idempotency_key": "key-1"},
        headers=auth_headers("employee_1"),
    )
    assert replayed.status_code == 202
    assert replayed.json()["id"] == with_idem.json()["id"]


def test_auditor_cannot_enqueue(client: TestClient) -> None:
    response = client.post(
        "/api/projects/project_owned/script-from-audio",
        json={"source_asset_id": "asset_video"},
        headers=auth_headers("auditor_1"),
    )
    assert response.status_code == 403


def test_other_user_cannot_read_task(client: TestClient) -> None:
    created = enqueue(client)
    task_id = created.json()["id"]
    forbidden = client.get(
        f"/api/script-from-audio-tasks/{task_id}",
        headers=auth_headers("employee_2"),
    )
    # 非本项目成员按"不存在"隐藏（仓库权限语义），而非 403。
    assert forbidden.status_code == 404
    allowed = client.get(
        f"/api/script-from-audio-tasks/{task_id}",
        headers=auth_headers("employee_1"),
    )
    assert allowed.status_code == 200


def test_worker_pipeline_completes_and_deletes_temp_audio(
    client: TestClient, db_path: Path, stub_media: StubMediaTools
) -> None:
    created = enqueue(client)
    task_id = created.json()["id"]

    storage = FakeStorageAdapter(provider="fake", bucket="private-bucket")
    storage.put_object(
        "projects/project_owned/uploads/asset_video/src.mp4",
        b"fake-video-bytes",
        content_type="video/mp4",
    )
    with BusinessConnection.sqlite(connect_database(db_path)) as conn:
        processed = run_worker_once(
            conn,
            worker_id="audio-test-worker",
            storage=storage,
            max_tasks=1,
        )
    assert processed == 1
    assert len(stub_media.extracted) == 1

    task = client.get(
        f"/api/script-from-audio-tasks/{task_id}", headers=auth_headers("employee_1")
    ).json()
    assert task["status"] == "SUCCEEDED"
    assert task["result"]["text"].startswith("（测试转写）")
    assert task["result"]["duration_sec"] == pytest.approx(12.5)

    # 转写用完即删：存储中不存在任何 tmp/asr 残留。
    leftovers = [key for key in storage._objects if key.startswith("tmp/asr/")]
    assert leftovers == []
    # 生成门禁的脚本版本表未被触碰。
    with BusinessConnection.sqlite(connect_database(db_path)) as conn:
        count = conn.execute("SELECT COUNT(*) AS n FROM versions").fetchone()
    assert int(count["n"]) == 0


def test_latest_endpoint_returns_newest_task(client: TestClient) -> None:
    assert (
        client.get(
            "/api/projects/project_owned/script-from-audio-tasks/latest",
            headers=auth_headers("employee_1"),
        ).json()
        is None
    )
    enqueue(client)
    latest = client.get(
        "/api/projects/project_owned/script-from-audio-tasks/latest",
        headers=auth_headers("employee_1"),
    )
    assert latest.status_code == 200
    assert latest.json()["status"] in ("PENDING", "RUNNING", "SUCCEEDED")
    assert latest.json()["source_asset_id"] == "asset_video"


def test_worker_cleans_durable_terminal_audio_after_restart(
    client: TestClient, db_path: Path
) -> None:
    task_id = enqueue(client).json()["id"]
    key = f"tmp/asr/project_owned/{task_id}.m4a"
    storage = FakeStorageAdapter(provider="fake", bucket="private-bucket")
    storage.put_object(key, b"residual", content_type="audio/mp4")
    with BusinessConnection.sqlite(connect_database(db_path)) as conn:
        conn.execute(
            "UPDATE script_from_audio_tasks SET status='FAILED',audio_object_key=%s WHERE id=%s",
            (key, task_id),
        )
        conn.commit()
        run_worker_once(conn, worker_id="cleanup-worker", storage=storage)
        row = conn.execute(
            "SELECT audio_object_key FROM script_from_audio_tasks WHERE id=%s", (task_id,)
        ).fetchone()
        assert row["audio_object_key"] is None
    assert storage.head_object(key) is None


def test_active_project_has_database_mutex(client: TestClient, db_path: Path) -> None:
    enqueue(client)
    with connect_database(db_path) as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO script_from_audio_tasks "
            "(id,project_id,source_asset_id,created_by_user_id,idempotency_key,"
            "request_hash,request_json,status) "
            "SELECT 'racing-task',project_id,source_asset_id,created_by_user_id,"
            "'different-key',request_hash,request_json,'PENDING' FROM script_from_audio_tasks"
        )


@pytest.mark.parametrize("operation", ["mark", "complete", "fail"])
def test_expired_audio_lease_cannot_change_recovered_state(
    client: TestClient, db_path: Path, operation: str
) -> None:
    from fastapi import HTTPException

    from app import script_from_audio as domain

    task_id = enqueue(client).json()["id"]
    with BusinessConnection.sqlite(connect_database(db_path)) as conn:
        lease = domain.acquire_script_from_audio_task(conn, worker_id="same-worker")
        assert lease is not None
        conn.execute(
            "UPDATE script_from_audio_tasks SET locked_until='2000-01-01 00:00:00' WHERE id=%s",
            (task_id,),
        )
        replacement = domain.acquire_script_from_audio_task(conn, worker_id="same-worker")
        assert replacement is not None and replacement.attempt > lease.attempt
        if operation == "fail":
            domain.fail_script_from_audio_task(
                conn, lease=lease, cause=RuntimeError("late"), submission_started=False
            )
        else:
            with pytest.raises(HTTPException):
                if operation == "mark":
                    domain.mark_script_from_audio_submission_started(conn, lease=lease)
                else:
                    domain.complete_script_from_audio_task(
                        conn, lease=lease, result=TranscriptResult("late", 1, "zh")
                    )
        row = domain.load_script_from_audio_task(conn, task_id)
        assert row["status"] == "RUNNING"
        assert row["attempt"] == replacement.attempt
        assert row["provider_started_at"] is None


@pytest.mark.parametrize(
    ("field", "value", "status_code", "code"),
    [
        ("content_type", "image/png", 422, "SCRIPT_FROM_AUDIO_SOURCE_TYPE_INVALID"),
        ("size_bytes", 2_000_000_001, 413, "SCRIPT_FROM_AUDIO_SOURCE_TOO_LARGE"),
    ],
)
def test_invalid_source_rejected_before_enqueue(
    client: TestClient, db_path: Path, field: str, value: object, status_code: int, code: str
) -> None:
    with connect_database(db_path) as conn:
        conn.execute(f"UPDATE assets SET {field}=? WHERE id='asset_video'", (value,))
    response = enqueue(client)
    assert response.status_code == status_code
    assert response.json()["detail"]["code"] == code


def test_missing_asr_configuration_is_a_business_error(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app import script_from_audio as domain

    def unavailable(_conn):
        raise AsrProviderError("语音转写服务未配置")

    monkeypatch.setattr(domain, "get_asr_provider", unavailable)
    response = enqueue(client)
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "SCRIPT_FROM_AUDIO_SERVICE_UNAVAILABLE"


def test_pg_worker_consumes_audio_outside_transaction(
    client: TestClient, db_path: Path, stub_media: StubMediaTools, monkeypatch: pytest.MonkeyPatch
) -> None:
    from contextlib import contextmanager

    from app import generation_worker as worker

    task_id = enqueue(client).json()["id"]
    transaction_open = False

    @contextmanager
    def transaction():
        nonlocal transaction_open
        with connect_database(db_path) as raw:
            transaction_open = True
            try:
                yield raw
            finally:
                transaction_open = False

    monkeypatch.setattr(worker, "pg_transaction", transaction)
    monkeypatch.setattr(BusinessConnection, "postgres", BusinessConnection.sqlite)
    for name in (
        "claim_oral_work",
        "acquire_generation_continuation_lease",
        "acquire_generation_task_lease",
        "acquire_character_generation_task",
        "acquire_analysis_task",
        "acquire_script_rewrite_task",
        "acquire_generation_reconcile_operation",
        "acquire_first_frame_task",
        "acquire_character_sheet_task",
        "_run_pg_source_frame_once",
    ):
        monkeypatch.setattr(worker, name, lambda *args, **kwargs: None)
    storage = FakeStorageAdapter(provider="fake", bucket="private-bucket")
    storage.put_object(
        "projects/project_owned/uploads/asset_video/src.mp4", b"video", content_type="video/mp4"
    )
    original_get = storage.get_object

    def outside_get(key):
        assert not transaction_open
        return original_get(key)

    monkeypatch.setattr(storage, "get_object", outside_get)
    assert worker.run_pg_worker_once(worker_id="pg-audio", storage=storage, max_tasks=1) == 1
    task = client.get(
        f"/api/script-from-audio-tasks/{task_id}", headers=auth_headers("employee_1")
    ).json()
    assert task["status"] == "SUCCEEDED"


def test_audio_receipt_survives_worker_retry_without_reupload_or_resubmit(
    client: TestClient, db_path: Path, stub_media: StubMediaTools, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    from app import script_from_audio as domain
    from app.asr import AsrConfiguration, DashScopeFunAsr

    calls = []
    responses = [
        {"output": {"task_id": "durable-receipt"}},
        {"output": {"task_status": "RUNNING"}},
        {
            "output": {
                "task_status": "SUCCEEDED",
                "results": [
                    {
                        "subtask_status": "SUCCEEDED",
                        "transcription_url": "https://result.invalid/text",
                    }
                ],
            }
        },
        {"transcripts": [{"text": "从原任务恢复的全文"}]},
    ]

    def transport(method, url, **kwargs):
        calls.append(method)
        return 200, json.dumps(responses.pop(0)).encode()

    provider = DashScopeFunAsr(
        AsrConfiguration("https://asr.invalid", "fake", "fun-asr", "fake", 0, 0, 1),
        transport=transport,
        sleep=lambda _: None,
    )
    monkeypatch.setattr(domain, "get_asr_provider", lambda _: provider)
    task_id = enqueue(client).json()["id"]
    storage = FakeStorageAdapter(provider="fake", bucket="private-bucket")
    storage.put_object(
        "projects/project_owned/uploads/asset_video/src.mp4", b"video", content_type="video/mp4"
    )
    with BusinessConnection.sqlite(connect_database(db_path)) as conn:
        assert run_worker_once(conn, worker_id="first-worker", storage=storage, max_tasks=1) == 1
        row = domain.load_script_from_audio_task(conn, task_id)
        assert row["status"] == "PENDING"
        assert row["provider_task_id"] == "durable-receipt"
        assert row["audio_object_key"] is not None
        assert storage.head_object(row["audio_object_key"]) is not None
        assert domain.acquire_script_from_audio_task(conn, worker_id="too-early") is None
        conn.execute(
            "UPDATE script_from_audio_tasks SET next_attempt_at='2000-01-01 00:00:00' WHERE id=%s",
            (task_id,),
        )
        conn.execute("DELETE FROM assets WHERE id='asset_video'")
        conn.commit()

        def forbidden_tool(_tool):
            raise AssertionError("resuming a receipt must not extract audio again")

        monkeypatch.setattr(domain, "resolve_media_binary", forbidden_tool)
        assert run_worker_once(conn, worker_id="replacement", storage=storage, max_tasks=1) == 1
        row = domain.load_script_from_audio_task(conn, task_id)
        assert row["status"] == "SUCCEEDED"
        assert row["audio_object_key"] is None
        assert json.loads(row["result_json"])["text"] == "从原任务恢复的全文"
    assert calls == ["POST", "GET", "GET", "GET"]


def test_local_media_failure_is_not_marked_as_provider_submission(
    client: TestClient, db_path: Path, stub_media: StubMediaTools, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app import script_from_audio as domain

    task_id = enqueue(client).json()["id"]
    storage = FakeStorageAdapter(provider="fake", bucket="private-bucket")
    storage.put_object(
        "projects/project_owned/uploads/asset_video/src.mp4", b"video", content_type="video/mp4"
    )

    def broken_media(*args):
        raise domain.MediaToolFailed("无可用音轨")

    monkeypatch.setattr(domain, "extract_audio", broken_media)
    with BusinessConnection.sqlite(connect_database(db_path)) as conn:
        run_worker_once(conn, worker_id="bad-media", storage=storage, max_tasks=1)
        row = domain.load_script_from_audio_task(conn, task_id)
        assert row["status"] == "FAILED"
        assert row["provider_started_at"] is None
        assert row["retryable"] == 1


def test_existing_audio_receipt_waits_for_configuration_recovery(
    client: TestClient, db_path: Path
) -> None:
    from fastapi import HTTPException

    from app import script_from_audio as domain

    task_id = enqueue(client).json()["id"]
    with BusinessConnection.sqlite(connect_database(db_path)) as conn:
        lease = domain.acquire_script_from_audio_task(conn, worker_id="recovery")
        assert lease is not None
        domain.checkpoint_script_from_audio_task(conn, lease=lease, provider_task_id="existing")
        domain.fail_script_from_audio_task(
            conn,
            lease=lease,
            cause=HTTPException(503, detail={"message": "配置暂不可用"}),
            submission_started=False,
        )
        row = domain.load_script_from_audio_task(conn, task_id)
        assert row["status"] == "PENDING"
        assert row["provider_task_id"] == "existing"
        assert row["retryable"] == 0


def test_worker_failures_delete_temp_audio_and_surface_error(
    client: TestClient, db_path: Path, stub_media: StubMediaTools, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.generation_worker as worker_module
    import app.script_from_audio as domain

    def failing_transcribe(file_url, *, duration_sec=None):
        raise AsrProviderError("语音转写服务返回错误（HTTP 500）")

    created = enqueue(client)
    task_id = created.json()["id"]
    storage = FakeStorageAdapter(provider="fake", bucket="private-bucket")
    storage.put_object(
        "projects/project_owned/uploads/asset_video/src.mp4",
        b"fake-video-bytes",
        content_type="video/mp4",
    )

    original_prepare = domain.prepare_script_from_audio_task

    def prepare_with_failing_asr(conn, *, lease, **kwargs):
        work = original_prepare(conn, lease=lease, storage=storage)
        work.asr = type(
            "FailingAsr",
            (),
            {"transcribe": staticmethod(failing_transcribe)},
        )()
        return work

    monkeypatch.setattr(worker_module, "prepare_script_from_audio_task", prepare_with_failing_asr)

    with BusinessConnection.sqlite(connect_database(db_path)) as conn:
        run_worker_once(conn, worker_id="audio-test-worker", storage=storage, max_tasks=1)

    task = client.get(
        f"/api/script-from-audio-tasks/{task_id}", headers=auth_headers("employee_1")
    ).json()
    assert task["status"] == "FAILED"
    assert "语音转写" in (task["error_message"] or "")
    leftovers = [key for key in storage._objects if key.startswith("tmp/asr/")]
    assert leftovers == []


def test_missing_ffmpeg_fails_closed(
    client: TestClient, db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.generation_worker as worker_module
    import app.script_from_audio as domain
    from app.media_tools import MediaToolUnavailable

    created = enqueue(client)
    task_id = created.json()["id"]
    storage = FakeStorageAdapter(provider="fake", bucket="private-bucket")
    storage.put_object(
        "projects/project_owned/uploads/asset_video/src.mp4",
        b"fake-video-bytes",
        content_type="video/mp4",
    )

    def unavailable(tool: str) -> str:
        raise MediaToolUnavailable("未找到 ffmpeg")

    original_prepare = domain.prepare_script_from_audio_task

    def prepare_without_tools(conn, *, lease, **kwargs):
        saved = domain.resolve_media_binary
        domain.resolve_media_binary = unavailable
        try:
            return original_prepare(conn, lease=lease, storage=storage)
        finally:
            domain.resolve_media_binary = saved

    monkeypatch.setattr(worker_module, "prepare_script_from_audio_task", prepare_without_tools)

    with BusinessConnection.sqlite(connect_database(db_path)) as conn:
        run_worker_once(conn, worker_id="audio-test-worker", storage=storage, max_tasks=1)

    task = client.get(
        f"/api/script-from-audio-tasks/{task_id}", headers=auth_headers("employee_1")
    ).json()
    assert task["status"] == "FAILED"
    assert "未找到 ffmpeg" in (task["error_message"] or "")


def test_transcript_result_dataclass_defaults() -> None:
    result = TranscriptResult(text="abc", duration_sec=None, language=None)
    assert result.text == "abc"
