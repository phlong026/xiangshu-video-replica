from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from threading import Event
from urllib.error import HTTPError
from urllib.request import Request

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.auth import CurrentUser, get_database
from app.db import connect_database, initialize_database
from app.db_portable import BusinessConnection
from app.generation_worker import run_worker_once
from app.main import app
from app.media import VideoMetadata, VideoProbeFailed
from app.storage import FakeStorageAdapter
from app.viral_link import (
    DouyidouHttpTransport,
    DouyidouLinkClient,
    NoRedirectHandler,
    ResolvedViralLink,
    UrllibDouyidouHttpTransport,
    ViralLinkError,
)
from app.viral_media import UrlFetcher, ViralMediaError, ViralMediaResult


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


class StubProbe:
    def __init__(self, *, duration: float = 12.0, failure: Exception | None = None) -> None:
        self.duration = duration
        self.failure = failure

    def probe(self, content: bytes, *, filename: str) -> VideoMetadata:
        if self.failure is not None:
            raise self.failure
        return VideoMetadata(duration_seconds=self.duration)


@pytest.fixture()
def forbid_explicit_transaction_control(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("explicit commit/rollback is forbidden inside a fenced transaction")

    monkeypatch.setattr(BusinessConnection, "commit", forbidden)
    monkeypatch.setattr(BusinessConnection, "rollback", forbidden)


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


def test_douyidou_does_not_retry_a_paid_request_without_provider_idempotency() -> None:
    transport = StubTransport(HTTPError("https://gateway.example", 503, "down", {}, None))
    client = DouyidouLinkClient(app_id="a", app_secret="s", transport=transport)
    with pytest.raises(ViralLinkError) as failure:
        client.resolve("https://v.douyin.com/share/", purpose="replica")
    assert failure.value.uncertain is True
    assert len(transport.calls) == 1

    rejected = StubTransport(HTTPError("https://gateway.example", 401, "bad", {}, None))
    client = DouyidouLinkClient(app_id="a", app_secret="s", transport=rejected)
    with pytest.raises(ViralLinkError) as failure:
        client.resolve("https://v.douyin.com/share/", purpose="replica")
    assert failure.value.retryable is False
    assert len(rejected.calls) == 1


def test_douyidou_requires_a_canonical_native_video_id() -> None:
    client = DouyidouLinkClient(
        app_id="a",
        app_secret="s",
        transport=StubTransport(b'{"code":0,"data":{"video":["https://cdn.example/video.mp4"]}}'),
    )
    with pytest.raises(ViralLinkError) as failure:
        client.resolve("https://v.douyin.com/share/", purpose="replica")
    assert failure.value.code == "VIRAL_LINK_NATIVE_ID_INVALID"


@pytest.mark.parametrize(
    "url",
    [
        "http://gateway.diadi.cn/api/parse",
        "https://evil.example/api/parse",
        "https://127.0.0.1/api/parse",
    ],
)
def test_douyidou_transport_refuses_noncanonical_gateway_targets(url: str) -> None:
    with pytest.raises(ViralLinkError) as failure:
        UrllibDouyidouHttpTransport().request(url, headers={"Sign": "local-contract"})
    assert failure.value.code == "VIRAL_LINK_GATEWAY_INVALID"


def test_douyidou_transport_does_not_forward_signature_on_redirect() -> None:
    request = Request("https://gateway.diadi.cn/api/parse", headers={"Sign": "local-contract"})
    redirected = NoRedirectHandler().redirect_request(
        request,
        None,
        302,
        "Found",
        {"Location": "http://127.0.0.1/internal"},
        "http://127.0.0.1/internal",
    )
    assert redirected is None


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


@pytest.mark.parametrize(
    ("content", "kind", "content_type"),
    [
        (b"<html>expired</html>", "video", "text/html"),
        (b'{"error":"expired"}', "video", "application/json"),
        (b"\x00\x00\x00\x18ftypisom-broken", "video", "application/json"),
        (b"ID3broken", "audio", "video/mp4"),
    ],
)
def test_link_media_preflight_rejects_wrong_content_or_mime(
    content: bytes, kind: str, content_type: str
) -> None:
    from app.viral_import_routes import validate_resolved_media_content

    with pytest.raises(ViralLinkError) as failure:
        validate_resolved_media_content(
            content,
            kind=kind,
            content_type=content_type,
            probe=StubProbe(),
        )
    assert failure.value.code == "VIRAL_LINK_MEDIA_INVALID"


def test_link_media_preflight_rejects_corrupt_mp4_after_magic_check() -> None:
    from app.viral_import_routes import validate_resolved_media_content

    with pytest.raises(ViralLinkError) as failure:
        validate_resolved_media_content(
            b"\x00\x00\x00\x18ftypisom-broken",
            kind="video",
            content_type="video/mp4",
            probe=StubProbe(failure=VideoProbeFailed("broken")),
        )
    assert failure.value.code == "VIRAL_LINK_MEDIA_INVALID"


def test_link_media_fetcher_enforces_the_smaller_preflight_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.viral_media as media

    class Response:
        status = 200
        headers = {"Content-Length": "9", "Content-Type": "video/mp4"}

        def read(self, size: int = -1) -> bytes:
            return b""

    class Connection:
        def request(self, method: str, target: str, *, headers: Mapping[str, str]) -> None:
            pass

        def getresponse(self) -> Response:
            return Response()

        def close(self) -> None:
            pass

    monkeypatch.setattr(
        media,
        "_resolve_public_http_url",
        lambda url: ("https", "cdn.example", 443, "93.184.216.34"),
    )
    with pytest.raises(ViralMediaError, match="上限"):
        UrlFetcher(max_bytes=8, connection_factory=lambda *args: Connection()).fetch(
            "https://cdn.example/video.mp4"
        )


def test_link_resolution_requires_idempotency_key(client: TestClient) -> None:
    response = client.post(
        "/api/viral/link-resolutions",
        headers={"X-Dev-User-Id": "employee_1"},
        json={"url": "https://v.douyin.com/share/", "purpose": "replica"},
    )
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["header", "Idempotency-Key"]


def test_link_resolution_openapi_locks_idempotency_purpose_and_errors() -> None:
    schema = app.openapi()
    operation = schema["paths"]["/api/viral/link-resolutions"]["post"]

    idempotency_header = next(
        parameter for parameter in operation["parameters"] if parameter["name"] == "Idempotency-Key"
    )
    assert idempotency_header["required"] is True
    assert set(operation["responses"]) >= {"200", "409", "410", "422", "502", "503", "504"}

    request_schema = schema["components"]["schemas"]["ViralLinkResolutionRequest"]
    assert request_schema["properties"]["purpose"]["enum"] == ["copy", "replica"]
    response_schema = schema["components"]["schemas"]["ViralLinkResolutionResponse"]
    assert "importIdempotencyKey" in response_schema["required"]


def test_resolved_link_becomes_existing_viral_import_source(
    client: TestClient,
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
    monkeypatch.setattr(
        routes,
        "get_media_storage",
        lambda conn: FakeStorageAdapter(provider="fake", bucket="private"),
    )
    monkeypatch.setattr(routes, "preflight_resolved_media", lambda *args, **kwargs: None)
    monkeypatch.setattr(import_domain, "ViralMediaPipeline", LinkPipeline)
    monkeypatch.setattr(import_domain, "viral_source_client_from_settings", lambda conn: object())

    response = client.post(
        "/api/viral/link-resolutions",
        headers={"X-Dev-User-Id": "employee_1", "Idempotency-Key": "resolve-link-1"},
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
    with sqlite3.connect(db_path) as receipt_conn:
        receipt = receipt_conn.execute(
            "SELECT status, response_json FROM viral_link_resolution_receipts"
        ).fetchone()
        assert receipt is not None
        assert receipt[0] == "SUCCEEDED"
        assert receipt[1]

    replay = client.post(
        "/api/viral/link-resolutions",
        headers={"X-Dev-User-Id": "employee_1", "Idempotency-Key": "resolve-link-1"},
        json={
            "url": "https://www.douyin.com/video/7345678901234567890",
            "purpose": "replica",
        },
    )
    assert replay.json() == response.json()
    assert len(resolver.calls) == 1
    conflict = client.post(
        "/api/viral/link-resolutions",
        headers={"X-Dev-User-Id": "employee_1", "Idempotency-Key": "resolve-link-1"},
        json={"url": "https://v.douyin.com/another/", "purpose": "replica"},
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "VIRAL_LINK_IDEMPOTENCY_CONFLICT"
    assert len(resolver.calls) == 1

    same_video = client.post(
        "/api/viral/link-resolutions",
        headers={"X-Dev-User-Id": "employee_1", "Idempotency-Key": "resolve-link-2"},
        json={"url": "https://v.douyin.com/other-short-link/", "purpose": "replica"},
    )
    assert same_video.status_code == 200
    assert same_video.json()["importIdempotencyKey"] == response.json()["importIdempotencyKey"]

    payload = {
        "platform": item["platform"],
        "videoId": item["videoId"],
        "purpose": "replica",
    }
    headers = {
        "X-Dev-User-Id": "employee_1",
        "Idempotency-Key": response.json()["importIdempotencyKey"],
    }
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


def test_link_resolution_relies_on_fenced_transaction_boundaries(
    client: TestClient,
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    forbid_explicit_transaction_control: None,
) -> None:
    import app.viral_import_routes as routes

    resolver = StubResolver(_resolved())
    monkeypatch.setattr(routes, "douyidou_link_client_from_settings", lambda conn: resolver)
    monkeypatch.setattr(
        routes,
        "get_media_storage",
        lambda conn: FakeStorageAdapter(provider="fake", bucket="private"),
    )
    monkeypatch.setattr(routes, "preflight_resolved_media", lambda *args, **kwargs: None)

    response = client.post(
        "/api/viral/link-resolutions",
        headers={"X-Dev-User-Id": "employee_1", "Idempotency-Key": "fenced-success"},
        json={"url": "https://v.douyin.com/fenced/", "purpose": "replica"},
    )

    assert response.status_code == 200
    assert len(resolver.calls) == 1
    with sqlite3.connect(db_path) as conn:
        assert (
            conn.execute(
                "SELECT status FROM viral_link_resolution_receipts WHERE idempotency_key = ?",
                ("fenced-success",),
            ).fetchone()[0]
            == "SUCCEEDED"
        )


def test_uncertain_resolution_receipt_blocks_another_paid_call(
    client: TestClient, db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.viral_import_routes as routes

    resolver = StubResolver(
        ViralLinkError(
            504,
            "VIRAL_LINK_SUBMISSION_UNCERTAIN",
            "视频链接解析结果未知。",
            retryable=False,
            uncertain=True,
        )
    )
    monkeypatch.setattr(routes, "douyidou_link_client_from_settings", lambda conn: resolver)
    headers = {"X-Dev-User-Id": "employee_1", "Idempotency-Key": "uncertain-link"}
    payload = {"url": "https://v.douyin.com/uncertain/", "purpose": "replica"}

    first = client.post("/api/viral/link-resolutions", headers=headers, json=payload)
    replay = client.post("/api/viral/link-resolutions", headers=headers, json=payload)

    assert first.status_code == 504
    assert replay.status_code == 503
    assert replay.json()["detail"]["code"] == "VIRAL_LINK_SUBMISSION_UNCERTAIN"
    assert len(resolver.calls) == 1
    with sqlite3.connect(db_path) as conn:
        assert (
            conn.execute("SELECT status FROM viral_link_resolution_receipts").fetchone()[0]
            == "UNCERTAIN"
        )


def test_concurrent_same_key_calls_provider_once(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    forbid_explicit_transaction_control: None,
) -> None:
    import app.viral_import_routes as routes

    started = Event()
    release = Event()
    calls = 0

    class BlockingResolver:
        def resolve(self, url: str, *, purpose: str) -> ResolvedViralLink:
            nonlocal calls
            calls += 1
            started.set()
            assert release.wait(timeout=5)
            return _resolved()

    monkeypatch.setattr(
        routes, "douyidou_link_client_from_settings", lambda conn: BlockingResolver()
    )
    monkeypatch.setattr(
        routes,
        "get_media_storage",
        lambda conn: FakeStorageAdapter(provider="fake", bucket="private"),
    )
    monkeypatch.setattr(routes, "preflight_resolved_media", lambda *args, **kwargs: None)
    headers = {"X-Dev-User-Id": "employee_1", "Idempotency-Key": "concurrent-link"}
    payload = {"url": "https://v.douyin.com/concurrent/", "purpose": "replica"}

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(
            client.post, "/api/viral/link-resolutions", headers=headers, json=payload
        )
        assert started.wait(timeout=5)
        concurrent = client.post("/api/viral/link-resolutions", headers=headers, json=payload)
        release.set()
        completed = first.result(timeout=5)

    assert completed.status_code == 200
    assert concurrent.status_code == 409
    assert concurrent.json()["detail"]["code"] == "VIRAL_LINK_IN_PROGRESS"
    assert calls == 1


def _insert_expired_link_receipt(
    db_path: Path, *, status: str, key: str, url: str = "https://v.douyin.com/restart/"
) -> None:
    from app.viral_import_routes import _link_request_hash

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO viral_link_resolution_receipts (
                id, owner_user_id, idempotency_key, normalized_url, purpose,
                request_hash, status, lease_owner, lease_expires_at
            ) VALUES (?, 'employee_1', ?, ?, 'replica', ?, ?, 'dead-process', ?)
            """,
            (
                f"receipt-{key}",
                key,
                url,
                _link_request_hash(url, "replica"),
                status,
                "2000-01-01 00:00:00+00:00",
            ),
        )


def test_expired_prepared_receipt_is_safely_claimed_once_after_restart(
    client: TestClient,
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    forbid_explicit_transaction_control: None,
) -> None:
    import app.viral_import_routes as routes

    _insert_expired_link_receipt(db_path, status="PREPARED", key="prepared-restart")
    resolver = StubResolver(_resolved())
    monkeypatch.setattr(routes, "douyidou_link_client_from_settings", lambda conn: resolver)
    monkeypatch.setattr(
        routes,
        "get_media_storage",
        lambda conn: FakeStorageAdapter(provider="fake", bucket="private"),
    )
    monkeypatch.setattr(routes, "preflight_resolved_media", lambda *args, **kwargs: None)

    response = client.post(
        "/api/viral/link-resolutions",
        headers={"X-Dev-User-Id": "employee_1", "Idempotency-Key": "prepared-restart"},
        json={"url": "https://v.douyin.com/restart/", "purpose": "replica"},
    )

    assert response.status_code == 200
    assert len(resolver.calls) == 1


def test_expired_request_sent_receipt_becomes_uncertain_without_paid_retry(
    client: TestClient,
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    forbid_explicit_transaction_control: None,
) -> None:
    import app.viral_import_routes as routes

    _insert_expired_link_receipt(db_path, status="REQUEST_SENT", key="sent-restart")
    resolver = StubResolver(_resolved())
    monkeypatch.setattr(routes, "douyidou_link_client_from_settings", lambda conn: resolver)

    response = client.post(
        "/api/viral/link-resolutions",
        headers={"X-Dev-User-Id": "employee_1", "Idempotency-Key": "sent-restart"},
        json={"url": "https://v.douyin.com/restart/", "purpose": "replica"},
    )

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "VIRAL_LINK_SUBMISSION_UNCERTAIN"
    assert resolver.calls == []
    with sqlite3.connect(db_path) as conn:
        assert (
            conn.execute(
                "SELECT status FROM viral_link_resolution_receipts WHERE idempotency_key = ?",
                ("sent-restart",),
            ).fetchone()[0]
            == "UNCERTAIN"
        )


def test_expired_request_sent_commits_uncertain_before_pg_style_http_error(
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.viral_import_routes as routes

    _insert_expired_link_receipt(db_path, status="REQUEST_SENT", key="pg-sent-restart")
    resolver = StubResolver(_resolved())
    monkeypatch.setattr(routes, "douyidou_link_client_from_settings", lambda conn: resolver)
    monkeypatch.setattr(BusinessConnection, "__exit__", lambda self, *args: None)

    class PgStyleDb:
        @contextmanager
        def write(self):
            raw = connect_database(db_path)
            raw.execute("BEGIN IMMEDIATE")
            connection = BusinessConnection.sqlite(raw)
            actor = CurrentUser(
                id="employee_1",
                username="employee_1",
                display_name="Employee One",
                role="employee",
            )
            try:
                yield connection, actor
            except BaseException:
                raw.rollback()
                raise
            else:
                raw.commit()
            finally:
                raw.close()

    with pytest.raises(HTTPException) as raised:
        routes.resolve_viral_link(
            routes.ViralLinkResolutionRequest(
                url="https://v.douyin.com/restart/", purpose="replica"
            ),
            PgStyleDb(),
            "pg-sent-restart",
        )

    assert raised.value.status_code == 503
    assert resolver.calls == []
    with sqlite3.connect(db_path) as conn:
        assert (
            conn.execute(
                "SELECT status FROM viral_link_resolution_receipts WHERE idempotency_key = ?",
                ("pg-sent-restart",),
            ).fetchone()[0]
            == "UNCERTAIN"
        )


def test_provider_success_then_process_crash_never_repeats_paid_request(
    client: TestClient,
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    forbid_explicit_transaction_control: None,
) -> None:
    import app.viral_import_routes as routes

    resolver = StubResolver(_resolved())
    monkeypatch.setattr(routes, "douyidou_link_client_from_settings", lambda conn: resolver)
    monkeypatch.setattr(
        routes,
        "get_media_storage",
        lambda conn: FakeStorageAdapter(provider="fake", bucket="private"),
    )
    monkeypatch.setattr(
        routes,
        "preflight_resolved_media",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("process crashed")),
    )
    headers = {"X-Dev-User-Id": "employee_1", "Idempotency-Key": "success-crash"}
    payload = {"url": "https://v.douyin.com/restart/", "purpose": "replica"}

    with pytest.raises(RuntimeError, match="process crashed"):
        client.post("/api/viral/link-resolutions", headers=headers, json=payload)
    assert len(resolver.calls) == 1
    with sqlite3.connect(db_path) as conn:
        assert (
            conn.execute(
                "SELECT status FROM viral_link_resolution_receipts WHERE idempotency_key = ?",
                ("success-crash",),
            ).fetchone()[0]
            == "REQUEST_SENT"
        )
        conn.execute(
            """UPDATE viral_link_resolution_receipts
            SET lease_expires_at = ? WHERE idempotency_key = ?""",
            ("2000-01-01 00:00:00+00:00", "success-crash"),
        )

    replay = client.post("/api/viral/link-resolutions", headers=headers, json=payload)
    assert replay.status_code == 503
    assert replay.json()["detail"]["code"] == "VIRAL_LINK_SUBMISSION_UNCERTAIN"
    assert len(resolver.calls) == 1


def test_invalid_downloaded_media_never_creates_source_project_or_asset(
    client: TestClient, db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.viral_import_routes as routes

    monkeypatch.setattr(
        routes, "douyidou_link_client_from_settings", lambda conn: StubResolver(_resolved())
    )
    monkeypatch.setattr(
        routes,
        "get_media_storage",
        lambda conn: FakeStorageAdapter(provider="fake", bucket="private"),
    )
    monkeypatch.setattr(
        routes,
        "preflight_resolved_media",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            ViralLinkError(
                422,
                "VIRAL_LINK_MEDIA_INVALID",
                "链接媒体格式无效，请上传 MP4 或 MOV 文件。",
                retryable=False,
            )
        ),
    )
    response = client.post(
        "/api/viral/link-resolutions",
        headers={"X-Dev-User-Id": "employee_1", "Idempotency-Key": "bad-media"},
        json={"url": "https://v.douyin.com/bad-media/", "purpose": "replica"},
    )
    assert response.status_code == 422
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM viral_videos").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0] == 0


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
        headers={"X-Dev-User-Id": "employee_1", "Idempotency-Key": "unsupported-link"},
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
        headers={"X-Dev-User-Id": "employee_1", "Idempotency-Key": expected_code},
        json={"url": "https://v.douyin.com/validShareCode/", "purpose": "replica"},
    )

    assert response.status_code == error.status_code
    assert response.json()["detail"]["code"] == expected_code
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM viral_videos").fetchone()[0] == 0
