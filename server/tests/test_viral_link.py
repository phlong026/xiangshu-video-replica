from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Mapping
from pathlib import Path
from urllib.error import HTTPError

import pytest
from fastapi.testclient import TestClient

from app.auth import get_database
from app.db import connect_database, initialize_database
from app.db_portable import BusinessConnection
from app.generation_worker import run_worker_once
from app.main import app
from app.storage import FakeStorageAdapter
from app.viral_link import (
    DouyidouHttpTransport,
    DouyidouLinkClient,
    ResolvedViralLink,
    ViralLinkError,
)
from app.viral_media import ViralMediaResult


@pytest.fixture()
def db_path(tmp_path: Path) -> Iterator[Path]:
    path = tmp_path / "viral-link.db"
    with initialize_database(path) as conn:
        conn.execute(
            "INSERT INTO users (id, username, display_name, role) VALUES (?, ?, ?, ?)",
            ("employee_1", "employee_1", "Employee One", "employee"),
        )
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


class StubResolver:
    def __init__(self, result: ResolvedViralLink | Exception) -> None:
        self.result = result
        self.calls: list[tuple[str, str]] = []

    def resolve(self, url: str, *, purpose: str) -> ResolvedViralLink:
        self.calls.append((url, purpose))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class StubTransport(DouyidouHttpTransport):
    def __init__(self, *responses: bytes | Exception) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, Mapping[str, str]]] = []

    def request(self, url: str, *, headers: Mapping[str, str]) -> bytes:
        self.calls.append((url, headers))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _resolved() -> ResolvedViralLink:
    return ResolvedViralLink(
        platform="douyin",
        video_id="7345678901234567890",
        title="三层新中式乡墅",
        author="乡墅营造",
        cover_url="https://cdn.example/cover.jpg",
        video_url="https://cdn.example/video.mp4",
        audio_url="https://cdn.example/audio.mp3",
        duration_ms=32_000,
        source_description="分享文案",
    )


def test_douyidou_signature_and_media_mapping_match_the_gateway_contract() -> None:
    transport = StubTransport(
        b'{"code":0,"data":{"aweme_id":"7345678901234567890",'
        b'"title":"sample","author":{"nickname":"builder"},'
        b'"video":["https://cdn.example/video.mp4"],'
        b'"audio":["https://cdn.example/audio.mp3"],"duration":32}}'
    )
    client = DouyidouLinkClient(
        app_id="app-1",
        app_secret="secret-1",
        transport=transport,
    )

    result = client.resolve(
        "https://www.douyin.com/video/7345678901234567890",
        purpose="replica",
    )

    assert result.video_id == "7345678901234567890"
    assert result.video_url == "https://cdn.example/video.mp4"
    assert result.audio_url == "https://cdn.example/audio.mp3"
    assert result.duration_ms == 32_000
    url, headers = transport.calls[0]
    assert url.endswith(
        "?app_id=app-1&url=https%3A%2F%2Fwww.douyin.com%2Fvideo%2F7345678901234567890"
    )
    assert headers == {"Sign": "d856569640dedb34722b6ce5fdfa9e5e"}


def test_douyidou_retries_only_server_errors() -> None:
    successful = b'{"code":0,"data":{"video":["https://cdn.example/video.mp4"]}}'
    retrying = StubTransport(
        HTTPError("https://gateway.example", 503, "down", {}, None),
        successful,
    )
    client = DouyidouLinkClient(app_id="a", app_secret="s", transport=retrying)
    client.resolve("https://v.douyin.com/share/", purpose="replica")
    assert len(retrying.calls) == 2

    rejected = StubTransport(HTTPError("https://gateway.example", 401, "bad", {}, None))
    client = DouyidouLinkClient(app_id="a", app_secret="s", transport=rejected)
    with pytest.raises(ViralLinkError) as failure:
        client.resolve("https://v.douyin.com/share/", purpose="replica")
    assert failure.value.retryable is False
    assert len(rejected.calls) == 1


def test_douyidou_rejects_expired_and_media_less_responses() -> None:
    expired = DouyidouLinkClient(
        app_id="a",
        app_secret="s",
        transport=StubTransport('{"code":1001,"message":"链接已过期"}'.encode()),
    )
    with pytest.raises(ViralLinkError) as expired_failure:
        expired.resolve("https://v.douyin.com/share/", purpose="replica")
    assert expired_failure.value.code == "VIRAL_LINK_EXPIRED"

    empty = DouyidouLinkClient(
        app_id="a",
        app_secret="s",
        transport=StubTransport(b'{"code":0,"data":{"text":"only text"}}'),
    )
    with pytest.raises(ViralLinkError) as empty_failure:
        empty.resolve("https://v.douyin.com/share/", purpose="replica")
    assert empty_failure.value.code == "VIRAL_LINK_MEDIA_MISSING"


def test_resolved_link_becomes_existing_viral_import_source(
    client: TestClient, db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.viral_import as import_domain
    import app.viral_import_routes as routes

    downloads: list[str] = []

    class LinkPipeline:
        def __init__(self, *, client: object, storage: FakeStorageAdapter) -> None:
            self.storage = storage

        def fetch(self, video: object, *, prefer: str | None = None) -> ViralMediaResult:
            downloads.append(str(prefer))
            stored = self.storage.put_object(
                "viral/douyin/7345678901234567890.mp4",
                b"resolved-link-video",
                content_type="video/mp4",
            )
            return ViralMediaResult(
                kind="video",
                storage_uri=stored.uri,
                url="https://cdn.example/video.mp4",
                size=stored.size,
                content_type=stored.content_type,
                cache_hit=False,
                sha256=stored.sha256,
            )

    resolver = StubResolver(_resolved())
    monkeypatch.setattr(routes, "douyidou_link_client_from_settings", lambda conn: resolver)
    monkeypatch.setattr(import_domain, "ViralMediaPipeline", LinkPipeline)
    monkeypatch.setattr(import_domain, "viral_source_client_from_settings", lambda conn: object())

    response = client.post(
        "/api/viral/link-resolutions",
        headers={"X-Dev-User-Id": "employee_1"},
        json={
            "url": "https://www.douyin.com/video/7345678901234567890",
            "purpose": "replica",
        },
    )

    assert response.status_code == 200, response.text
    item = response.json()["item"]
    assert item["platform"] == "douyin"
    assert item["videoId"] == "7345678901234567890"
    assert item["playUrl"] == "https://cdn.example/video.mp4"
    assert resolver.calls == [("https://www.douyin.com/video/7345678901234567890", "replica")]

    payload = {
        "platform": item["platform"],
        "videoId": item["videoId"],
        "purpose": "replica",
    }
    headers = {"X-Dev-User-Id": "employee_1", "Idempotency-Key": "link-replica-1"}
    first = client.post("/api/viral/videos/import-tasks", headers=headers, json=payload)
    replay = client.post("/api/viral/videos/import-tasks", headers=headers, json=payload)
    assert first.status_code == 202
    assert replay.json()["id"] == first.json()["id"]
    assert replay.json()["projectId"] == first.json()["projectId"]
    storage = FakeStorageAdapter(provider="fake", bucket="private")
    worker_conn = BusinessConnection.sqlite(connect_database(db_path))
    try:
        assert run_worker_once(worker_conn, worker_id="link-worker", storage=storage) == 1
    finally:
        worker_conn.close()
    completed = client.get(
        f"/api/viral/import-tasks/{first.json()['id']}",
        headers={"X-Dev-User-Id": "employee_1"},
    ).json()
    assert completed["status"] == "SUCCEEDED"
    assert downloads == ["video"]
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM viral_import_tasks").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 1
        assert conn.execute("SELECT owner_user_id FROM projects").fetchone()[0] == "employee_1"
        assert (
            conn.execute(
                "SELECT project_id FROM assets WHERE id = ?",
                (completed["sourceAssetId"],),
            ).fetchone()[0]
            == completed["projectId"]
        )


@pytest.mark.parametrize(
    ("url", "code", "message"),
    [
        (
            "https://channels.weixin.qq.com/platform/post/123",
            "WECHAT_CHANNELS_LINK_UNSUPPORTED",
            "视频号链接暂不支持解析，请上传 MP4 或 MOV 文件。",
        ),
        (
            "https://example.com/video/123",
            "VIRAL_LINK_PLATFORM_UNSUPPORTED",
            "当前仅支持抖音视频链接，请上传 MP4 或 MOV 文件。",
        ),
    ],
)
def test_unsupported_links_require_upload_without_creating_projects(
    client: TestClient,
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    url: str,
    code: str,
    message: str,
) -> None:
    import app.viral_import_routes as routes

    resolver = StubResolver(_resolved())
    monkeypatch.setattr(routes, "douyidou_link_client_from_settings", lambda conn: resolver)
    response = client.post(
        "/api/viral/link-resolutions",
        headers={"X-Dev-User-Id": "employee_1"},
        json={"url": url, "purpose": "replica"},
    )

    assert response.status_code == 422
    assert response.json()["detail"] == {"code": code, "message": message}
    assert resolver.calls == []
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM viral_import_tasks").fetchone()[0] == 0


@pytest.mark.parametrize(
    ("error", "expected_code"),
    [
        (
            ViralLinkError(
                410,
                "VIRAL_LINK_EXPIRED",
                "视频链接已过期，请重新复制链接或上传 MP4/MOV 文件。",
                retryable=False,
            ),
            "VIRAL_LINK_EXPIRED",
        ),
        (
            ViralLinkError(
                422,
                "VIRAL_LINK_MEDIA_MISSING",
                "链接中没有可用视频，请上传 MP4 或 MOV 文件。",
                retryable=False,
            ),
            "VIRAL_LINK_MEDIA_MISSING",
        ),
    ],
)
def test_expired_or_media_less_results_do_not_create_fake_projects(
    client: TestClient,
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: ViralLinkError,
    expected_code: str,
) -> None:
    import app.viral_import_routes as routes

    monkeypatch.setattr(
        routes,
        "douyidou_link_client_from_settings",
        lambda conn: StubResolver(error),
    )
    response = client.post(
        "/api/viral/link-resolutions",
        headers={"X-Dev-User-Id": "employee_1"},
        json={"url": "https://v.douyin.com/validShareCode/", "purpose": "replica"},
    )

    assert response.status_code == error.status_code
    assert response.json()["detail"]["code"] == expected_code
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM viral_videos").fetchone()[0] == 0
