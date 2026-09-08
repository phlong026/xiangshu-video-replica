from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.auth import get_database
from app.db import connect_database, initialize_database
from app.db_portable import BusinessConnection
from app.generation_worker import run_worker_once
from app.main import app
from app.storage import FakeStorageAdapter
from app.viral_media import ViralMediaResult
from app.viral_store import upsert_viral_videos
from app.viral_tikhub import ViralVideo


@pytest.fixture()
def db_path(tmp_path: Path) -> Iterator[Path]:
    path = tmp_path / "viral-import.db"
    with initialize_database(path) as raw:
        _seed(raw)
    yield path


@pytest.fixture()
def client(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("VIDEO_REPLICA_DB_PATH", str(db_path))

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


def _video(
    *,
    video_id: str = "viral-1",
    verified_audio: bool = False,
    platform: str = "douyin",
) -> ViralVideo:
    return ViralVideo(
        platform=platform,
        video_id=video_id,
        category="乡墅案例",
        title="三层新中式乡墅",
        author="示例作者",
        author_avatar=None,
        verified=False,
        cover_url=None,
        duration_ms=12_000,
        likes=100,
        comments=2,
        shares=3,
        collects=4,
        published_at=None,
        published_display=None,
        like_display=None,
        play_url="https://cdn.example/video.mp4" if platform == "douyin" else None,
        audio_url="https://cdn.example/audio.mp3" if platform == "douyin" else None,
        native={"source_audio_verified": verified_audio},
    )


def _seed(conn: sqlite3.Connection) -> None:
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
        ("owned-project", "employee_1", "Existing Project"),
    )
    upsert_viral_videos(
        BusinessConnection.sqlite(conn),
        [
            _video(),
            _video(video_id="audio", verified_audio=True),
            _video(video_id="wx-cached", platform="wechat_channels"),
        ],
    )


def _headers(user_id: str, key: str = "import-key") -> dict[str, str]:
    return {"X-Dev-User-Id": user_id, "Idempotency-Key": key}


def test_enqueue_creates_project_and_replays_same_request(client: TestClient) -> None:
    payload = {"platform": "douyin", "videoId": "viral-1", "purpose": "copy"}
    first = client.post("/api/viral/import-tasks", json=payload, headers=_headers("employee_1"))
    replay = client.post("/api/viral/import-tasks", json=payload, headers=_headers("employee_1"))

    assert first.status_code == 202
    assert replay.status_code == 202
    assert replay.json()["id"] == first.json()["id"]
    assert replay.json()["projectId"] == first.json()["projectId"]
    assert first.json()["status"] == "PENDING"


def test_idempotency_conflict_and_permissions(client: TestClient) -> None:
    first = client.post(
        "/api/viral/import-tasks",
        json={"platform": "douyin", "videoId": "viral-1", "purpose": "copy"},
        headers=_headers("employee_1"),
    )
    conflict = client.post(
        "/api/viral/import-tasks",
        json={"platform": "douyin", "videoId": "viral-1", "purpose": "replica"},
        headers=_headers("employee_1"),
    )
    auditor = client.post(
        "/api/viral/import-tasks",
        json={"platform": "douyin", "videoId": "viral-1", "purpose": "copy"},
        headers=_headers("auditor_1", "auditor-key"),
    )
    other = client.get(
        f"/api/viral/import-tasks/{first.json()['id']}",
        headers={"X-Dev-User-Id": "employee_2"},
    )

    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "VIRAL_IMPORT_IDEMPOTENCY_CONFLICT"
    assert auditor.status_code == 403
    assert other.status_code == 404


def test_existing_project_must_be_owned(client: TestClient) -> None:
    response = client.post(
        "/api/viral/import-tasks",
        json={
            "platform": "douyin",
            "videoId": "viral-1",
            "purpose": "copy",
            "projectId": "owned-project",
        },
        headers=_headers("employee_2", "other-key"),
    )
    # Cross-owner project access is deliberately concealed as not found.
    assert response.status_code == 404


class StubPipeline:
    requested_preference: list[str | None] = []
    clients: list[object | None] = []

    def __init__(self, *, client: object, storage: FakeStorageAdapter) -> None:
        self.clients.append(client)
        self.storage = storage

    def fetch(self, video: ViralVideo, *, prefer: str | None = None) -> ViralMediaResult:
        self.requested_preference.append(prefer)
        kind = "audio" if prefer == "audio" else "video"
        suffix = "mp3" if kind == "audio" else "mp4"
        content_type = "audio/mpeg" if kind == "audio" else "video/mp4"
        key = f"viral/{video.platform}/{video.video_id}.{suffix}"
        stored = self.storage.put_object(key, b"source-media", content_type=content_type)
        return ViralMediaResult(
            kind=kind,
            storage_uri=stored.uri,
            url="fake://download",
            size=stored.size,
            content_type=stored.content_type,
            cache_hit=False,
            sha256=stored.sha256,
        )


def test_worker_imports_project_asset_and_exposes_capabilities(
    client: TestClient,
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.viral_import as domain

    StubPipeline.requested_preference.clear()
    StubPipeline.clients.clear()
    monkeypatch.setattr(domain, "ViralMediaPipeline", StubPipeline)
    monkeypatch.setattr(domain, "viral_source_client_from_settings", lambda conn: object())
    storage = FakeStorageAdapter(provider="fake", bucket="private")
    created = client.post(
        "/api/viral/import-tasks",
        json={"platform": "douyin", "videoId": "viral-1", "purpose": "replica"},
        headers=_headers("employee_1", "replica-key"),
    ).json()

    conn = BusinessConnection.sqlite(connect_database(db_path))
    try:
        assert run_worker_once(conn, worker_id="worker-1", storage=storage, max_tasks=1) == 1
    finally:
        conn.close()

    completed = client.get(
        f"/api/viral/import-tasks/{created['id']}", headers={"X-Dev-User-Id": "employee_1"}
    )
    assert completed.status_code == 200
    body = completed.json()
    assert body["status"] == "SUCCEEDED"
    assert body["mediaKind"] == "video"
    assert body["canTranscribe"] is True
    assert body["canAnalyze"] is True
    assert body["sourceAssetId"]
    assert StubPipeline.requested_preference == ["video"]
    assert len(StubPipeline.clients) == 1
    assert StubPipeline.clients[0] is not None

    with connect_database(db_path) as raw:
        asset = raw.execute(
            "SELECT * FROM assets WHERE id = ?", (body["sourceAssetId"],)
        ).fetchone()
        assert asset["kind"] == "reference_video"
        assert asset["storage_uri"].startswith(
            f"fake://private/projects/{created['projectId']}/viral-imports/"
        )
        assert asset["sha256"]
        assert asset["size_bytes"] == len(b"source-media")
        assert '"duration_seconds": 12.0' in asset["metadata_json"]
        assert f'"import_task_id": "{created["id"]}"' in asset["metadata_json"]
        assert '"platform": "douyin"' in asset["metadata_json"]
        assert '"video_id": "viral-1"' in asset["metadata_json"]


def test_completion_rejects_project_owner_change(
    client: TestClient,
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.viral_import as domain

    monkeypatch.setattr(domain, "ViralMediaPipeline", StubPipeline)
    monkeypatch.setattr(domain, "viral_source_client_from_settings", lambda conn: object())
    storage = FakeStorageAdapter(provider="fake", bucket="private")
    created = client.post(
        "/api/viral/import-tasks",
        json={"platform": "douyin", "videoId": "viral-1", "purpose": "replica"},
        headers=_headers("employee_1", "owner-change-key"),
    ).json()

    conn = BusinessConnection.sqlite(connect_database(db_path))
    try:
        lease = domain.acquire_viral_import_task(conn, worker_id="worker-owner-change")
        assert lease is not None
        work = domain.prepare_viral_import_task(conn, lease=lease, storage=storage)
        outcome = domain.perform_viral_import_task(work)
        conn.execute(
            "UPDATE projects SET owner_user_id = %s WHERE id = %s",
            ("employee_2", created["projectId"]),
        )
        conn.commit()

        with pytest.raises(HTTPException) as raised:
            domain.complete_viral_import_task(conn, lease=lease, outcome=outcome)

        assert raised.value.detail["code"] == "VIRAL_IMPORT_PROJECT_CHANGED"
        assert (
            conn.execute(
                "SELECT 1 FROM assets WHERE project_id = %s", (created["projectId"],)
            ).fetchone()
            is None
        )
    finally:
        conn.close()


def test_copy_uses_audio_only_when_source_marks_it_verified(
    client: TestClient,
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.viral_import as domain

    StubPipeline.requested_preference.clear()
    monkeypatch.setattr(domain, "ViralMediaPipeline", StubPipeline)
    monkeypatch.setattr(domain, "viral_source_client_from_settings", lambda conn: object())
    storage = FakeStorageAdapter(provider="fake", bucket="private")

    for index, video_id in enumerate(("viral-1", "audio"), 1):
        client.post(
            "/api/viral/import-tasks",
            json={"platform": "douyin", "videoId": video_id, "purpose": "copy"},
            headers=_headers("employee_1", f"copy-{index}"),
        )
    conn = BusinessConnection.sqlite(connect_database(db_path))
    try:
        assert run_worker_once(conn, worker_id="worker-1", storage=storage, max_tasks=2) == 2
    finally:
        conn.close()

    assert sorted(StubPipeline.requested_preference) == ["audio", "video"]


def test_retryable_failure_reuses_task_and_project(client: TestClient, db_path: Path) -> None:
    payload = {"platform": "douyin", "videoId": "viral-1", "purpose": "copy"}
    created = client.post(
        "/api/viral/import-tasks", json=payload, headers=_headers("employee_1", "retry-key")
    ).json()
    with connect_database(db_path) as conn:
        conn.execute(
            """
            UPDATE viral_import_tasks SET status = 'FAILED', retryable = 1,
                error_code = 'VIRAL_IMPORT_FAILED', completed_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (created["id"],),
        )

    retried = client.post(
        "/api/viral/import-tasks", json=payload, headers=_headers("employee_1", "retry-key")
    )
    assert retried.status_code == 202
    assert retried.json()["id"] == created["id"]
    assert retried.json()["projectId"] == created["projectId"]
    assert retried.json()["status"] == "PENDING"


def test_worker_project_transfer_failure_is_not_replayed_with_same_key(
    client: TestClient,
    db_path: Path,
) -> None:
    payload = {"platform": "douyin", "videoId": "viral-1", "purpose": "replica"}
    created = client.post(
        "/api/viral/import-tasks",
        json=payload,
        headers=_headers("employee_1", "transferred-project-key"),
    ).json()
    with connect_database(db_path) as raw:
        raw.execute(
            "UPDATE projects SET owner_user_id = ? WHERE id = ?",
            ("employee_2", created["projectId"]),
        )

    conn = BusinessConnection.sqlite(connect_database(db_path))
    try:
        assert (
            run_worker_once(
                conn,
                worker_id="worker-transfer",
                storage=FakeStorageAdapter(provider="fake", bucket="private"),
            )
            == 1
        )
    finally:
        conn.close()

    failed = client.get(
        f"/api/viral/import-tasks/{created['id']}",
        headers={"X-Dev-User-Id": "employee_1"},
    )
    assert failed.json()["status"] == "FAILED"
    assert failed.json()["errorCode"] == "VIRAL_IMPORT_PROJECT_CHANGED"
    assert failed.json()["retryable"] is False

    replay = client.post(
        "/api/viral/import-tasks",
        json=payload,
        headers=_headers("employee_1", "transferred-project-key"),
    )
    assert replay.json()["status"] == "FAILED"

    replacement = client.post(
        "/api/viral/import-tasks",
        json=payload,
        headers=_headers("employee_1", "replacement-project-key"),
    )
    assert replacement.status_code == 202
    assert replacement.json()["id"] != created["id"]
    assert replacement.json()["status"] == "PENDING"


def test_worker_missing_source_failure_stays_terminal_and_replay_is_controlled(
    client: TestClient,
    db_path: Path,
) -> None:
    payload = {"platform": "douyin", "videoId": "viral-1", "purpose": "copy"}
    created = client.post(
        "/api/viral/import-tasks",
        json=payload,
        headers=_headers("employee_1", "removed-source-key"),
    ).json()
    with connect_database(db_path) as raw:
        raw.execute(
            "DELETE FROM viral_videos WHERE platform = ? AND video_id = ?",
            ("douyin", "viral-1"),
        )

    conn = BusinessConnection.sqlite(connect_database(db_path))
    try:
        assert (
            run_worker_once(
                conn,
                worker_id="worker-missing-source",
                storage=FakeStorageAdapter(provider="fake", bucket="private"),
            )
            == 1
        )
    finally:
        conn.close()

    failed = client.get(
        f"/api/viral/import-tasks/{created['id']}",
        headers={"X-Dev-User-Id": "employee_1"},
    )
    assert failed.json()["errorCode"] == "VIRAL_VIDEO_NOT_FOUND"
    assert failed.json()["retryable"] is False

    replay = client.post(
        "/api/viral/import-tasks",
        json=payload,
        headers=_headers("employee_1", "removed-source-key"),
    )
    assert replay.status_code == 404
    assert replay.json()["detail"]["code"] == "VIRAL_VIDEO_NOT_FOUND"


def test_unavailable_video_and_missing_idempotency_are_rejected(
    client: TestClient, db_path: Path
) -> None:
    with connect_database(db_path) as conn:
        conn.execute(
            """
            INSERT INTO viral_video_visibility (platform, video_id, status)
            VALUES ('douyin', 'viral-1', 'HIDDEN')
            """
        )
    hidden = client.post(
        "/api/viral/import-tasks",
        json={"platform": "douyin", "videoId": "viral-1", "purpose": "copy"},
        headers=_headers("employee_1", "hidden-key"),
    )
    missing_key = client.post(
        "/api/viral/import-tasks",
        json={"platform": "douyin", "videoId": "audio", "purpose": "copy"},
        headers={"X-Dev-User-Id": "employee_1"},
    )
    assert hidden.status_code == 409
    assert hidden.json()["detail"]["code"] == "VIRAL_VIDEO_UNAVAILABLE"
    assert missing_key.status_code == 422


def test_runtime_switch_blocks_new_imports(client: TestClient, db_path: Path) -> None:
    with connect_database(db_path) as conn:
        conn.execute("UPDATE viral_runtime_controls SET import_enabled = 0 WHERE id = 1")
    response = client.post(
        "/api/viral/import-tasks",
        json={"platform": "douyin", "videoId": "viral-1", "purpose": "copy"},
        headers=_headers("employee_1", "disabled-key"),
    )
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "VIRAL_IMPORT_DISABLED"


def test_cached_wechat_media_imports_without_source_settings(
    client: TestClient, db_path: Path
) -> None:
    storage = FakeStorageAdapter(provider="fake", bucket="private")
    storage.put_object(
        "viral/wechat_channels/wx-cached.mp4",
        b"cached-wechat-video",
        content_type="video/mp4",
    )
    created = client.post(
        "/api/viral/import-tasks",
        json={
            "platform": "wechat_channels",
            "videoId": "wx-cached",
            "purpose": "replica",
        },
        headers=_headers("employee_1", "wx-cache-key"),
    ).json()
    conn = BusinessConnection.sqlite(connect_database(db_path))
    try:
        assert run_worker_once(conn, worker_id="worker-1", storage=storage, max_tasks=1) == 1
    finally:
        conn.close()
    completed = client.get(
        f"/api/viral/import-tasks/{created['id']}",
        headers={"X-Dev-User-Id": "employee_1"},
    )
    assert completed.json()["status"] == "SUCCEEDED"
    assert completed.json()["canAnalyze"] is True


def test_two_imports_share_one_durable_public_media_preparation(
    client: TestClient,
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi import HTTPException

    import app.viral_import as domain
    from app.viral_import import acquire_viral_import_task, prepare_viral_import_task

    monkeypatch.setattr(domain, "viral_source_client_from_settings", lambda conn: object())
    first = client.post(
        "/api/viral/import-tasks",
        json={"platform": "douyin", "videoId": "viral-1", "purpose": "replica"},
        headers=_headers("employee_1", "media-owner-1"),
    ).json()
    second = client.post(
        "/api/viral/import-tasks",
        json={"platform": "douyin", "videoId": "viral-1", "purpose": "replica"},
        headers=_headers("employee_2", "media-owner-2"),
    ).json()
    storage = FakeStorageAdapter(provider="fake", bucket="private")
    conn = BusinessConnection.sqlite(connect_database(db_path))
    try:
        lease_a = acquire_viral_import_task(conn, worker_id="worker-1")
        lease_b = acquire_viral_import_task(conn, worker_id="worker-2")
        assert lease_a is not None and lease_b is not None
        leases = {lease_a.id: lease_a, lease_b.id: lease_b}
        first_lease = leases[first["id"]]
        second_lease = leases[second["id"]]

        first_work = prepare_viral_import_task(conn, lease=first_lease, storage=storage)
        assert first_work.media_preparation is not None
        with pytest.raises(HTTPException) as raised:
            prepare_viral_import_task(conn, lease=second_lease, storage=storage)
        assert raised.value.detail["code"] == "VIRAL_MEDIA_PREPARATION_BUSY"

        row = conn.execute(
            "SELECT status, attempt, owner_task_id FROM viral_media_preparations"
        ).fetchone()
        assert tuple(row) == ("RUNNING", 1, first["id"])
    finally:
        conn.close()


def test_import_rejects_same_size_copy_with_mismatched_hash(
    client: TestClient,
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.viral_import as domain

    class CorruptingStorage(FakeStorageAdapter):
        def copy_object(self, source_key: str, destination_key: str):
            source = self.head_object(source_key)
            assert source is not None
            return self.put_object(
                destination_key,
                b"x" * source.size,
                content_type=source.content_type,
            )

    monkeypatch.setattr(domain, "ViralMediaPipeline", StubPipeline)
    monkeypatch.setattr(domain, "viral_source_client_from_settings", lambda conn: object())
    storage = CorruptingStorage(provider="fake", bucket="private")
    created = client.post(
        "/api/viral/import-tasks",
        json={"platform": "douyin", "videoId": "viral-1", "purpose": "replica"},
        headers=_headers("employee_1", "corrupt-copy-key"),
    ).json()

    conn = BusinessConnection.sqlite(connect_database(db_path))
    try:
        assert run_worker_once(conn, worker_id="worker-1", storage=storage, max_tasks=1) == 1
    finally:
        conn.close()

    completed = client.get(
        f"/api/viral/import-tasks/{created['id']}",
        headers={"X-Dev-User-Id": "employee_1"},
    )
    assert completed.json()["status"] == "FAILED"
    assert not any("/viral-imports/" in key for key in storage._objects)


def test_sqlite_worker_rolls_back_partial_asset_before_marking_failure(
    client: TestClient,
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.generation_worker as worker
    import app.viral_import as domain

    monkeypatch.setattr(domain, "ViralMediaPipeline", StubPipeline)

    def fail_after_asset_insert(conn: BusinessConnection, **kwargs: object) -> None:
        lease = kwargs["lease"]
        conn.execute(
            """
            INSERT INTO assets (
                id, project_id, kind, storage_uri, sha256, size_bytes,
                content_type, created_by_user_id, metadata_json
            ) VALUES ('partial-asset', %s, 'reference_video', 'fake://private/partial',
                      'deadbeef', 1, 'video/mp4', %s, '{}')
            """,
            (lease.project_id, lease.owner_user_id),
        )
        raise RuntimeError("simulated finalize failure")

    monkeypatch.setattr(worker, "complete_viral_import_task", fail_after_asset_insert)
    created = client.post(
        "/api/viral/import-tasks",
        json={"platform": "douyin", "videoId": "viral-1", "purpose": "replica"},
        headers=_headers("employee_1", "rollback-key"),
    ).json()
    storage = FakeStorageAdapter(provider="fake", bucket="private")
    conn = BusinessConnection.sqlite(connect_database(db_path))
    try:
        assert run_worker_once(conn, worker_id="worker-1", storage=storage, max_tasks=1) == 1
    finally:
        conn.close()

    with connect_database(db_path) as raw:
        assert raw.execute("SELECT 1 FROM assets WHERE id = 'partial-asset'").fetchone() is None
        task = raw.execute(
            "SELECT status, retryable FROM viral_import_tasks WHERE id = ?", (created["id"],)
        ).fetchone()
        assert tuple(task) == ("FAILED", 1)

    replay = client.post(
        "/api/viral/import-tasks",
        json={"platform": "douyin", "videoId": "viral-1", "purpose": "replica"},
        headers=_headers("employee_1", "rollback-key"),
    )
    assert replay.status_code == 202
    assert replay.json()["id"] == created["id"]
    assert replay.json()["status"] == "PENDING"
    assert (
        storage.head_object(
            f"projects/{created['projectId']}/viral-imports/{created['id']}/source.mp4"
        )
        is None
    )


def test_stale_attempt_cannot_publish_asset_even_with_same_worker_id(
    client: TestClient,
    db_path: Path,
) -> None:
    from fastapi import HTTPException

    from app.viral_import import (
        ViralImportOutcome,
        acquire_viral_import_task,
        complete_viral_import_task,
    )

    created = client.post(
        "/api/viral/import-tasks",
        json={"platform": "douyin", "videoId": "viral-1", "purpose": "replica"},
        headers=_headers("employee_1", "attempt-fence-key"),
    ).json()
    storage = FakeStorageAdapter(provider="fake", bucket="private")
    stored = storage.put_object("result.mp4", b"video", content_type="video/mp4")
    conn = BusinessConnection.sqlite(connect_database(db_path))
    try:
        stale = acquire_viral_import_task(conn, worker_id="worker-reused")
        assert stale is not None
        conn.execute(
            "UPDATE viral_import_tasks SET attempt = attempt + 1 WHERE id = %s",
            (created["id"],),
        )
        conn.commit()

        with pytest.raises(HTTPException) as raised:
            complete_viral_import_task(
                conn,
                lease=stale,
                outcome=ViralImportOutcome(
                    stored=stored,
                    media_kind="video",
                    duration_seconds=1.0,
                ),
            )
        assert raised.value.detail["code"] == "VIRAL_IMPORT_LEASE_LOST"
        conn.rollback()
        assert (
            conn.execute(
                "SELECT 1 FROM assets WHERE project_id = %s", (created["projectId"],)
            ).fetchone()
            is None
        )
    finally:
        conn.close()
