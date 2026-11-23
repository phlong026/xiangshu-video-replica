"""单条转存在共享媒体租约内恢复抖音过期播放地址。

下载使用真实媒体管线和内存存储，HTTP 连接与链接解析器均为测试替身；
数据库用例沿用 T34 迁移夹具，验证发布成功后才 CAS 回写及人工元数据。
"""

# Imported pytest fixtures are injected by parameter name.
# ruff: noqa: F811, F401

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import replace
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlsplit

import psycopg
import pytest
from psycopg.rows import dict_row
from test_admin_session_routes import route_state, sessions_dsn  # noqa: F401

from app import viral_collection, viral_link, viral_media, viral_media_preparation
from app.admin_viral_metrics import video_business_metrics
from app.billing_meter import billing_context
from app.db_pg import DATABASE_URL_ENV, close_pg_pool
from app.db_portable import BusinessConnection
from app.storage import FakeStorageAdapter, StorageBackendUnavailable
from app.usage_billing import accept_operation, reconcile_operations
from app.viral_collection_budget import collection_budget
from app.viral_content_overview import content_overview
from app.viral_link import ResolvedViralLink, ResolvedViralMetadata, ViralLinkError
from app.viral_media import ViralMediaDNSUnavailable, ViralMediaError, ViralMediaPipeline
from app.viral_media_preparation import ViralMediaBusy, ViralMediaLeaseLost, ViralMediaPreparation
from app.viral_refresh import ViralRefreshLease, acquire_viral_refresh_task, fail_viral_refresh_task
from app.viral_tikhub import ViralVideo, WechatVideoDetail

PLATFORM = "douyin"
VIDEO_ID = "7294812345678901234"
EXPIRED_URL = "https://cdn.example.test/expired.mp4"
FRESH_URL = "https://cdn.example.test/fresh.mp4"
RENEWED_URL = "https://cdn.example.test/renewed.mp4"
CONCURRENT_URL = "https://cdn.example.test/concurrent.mp4"
AUDIO_URL = "https://cdn.example.test/manual.mp3"
MP4 = b"\x00\x00\x00\x18ftypisom" + b"video content"
AUDIO = b"ID3" + b"audio content"


def _video(
    *,
    video_id: str = VIDEO_ID,
    platform: str = PLATFORM,
    play_url: str | None = EXPIRED_URL,
    audio_url: str | None = AUDIO_URL,
) -> ViralVideo:
    return ViralVideo(
        platform=platform,
        video_id=video_id,
        category="人工分类",
        title="人工标题",
        author="人工作者",
        author_avatar="https://avatar.example.test/manual.jpg",
        verified=True,
        cover_url=None,
        duration_ms=11_000,
        likes=42,
        comments=3,
        shares=4,
        collects=5,
        published_at=1788700000,
        published_display="人工发布时间",
        like_display="人工点赞数",
        tags=["人工标签"],
        play_url=play_url,
        audio_url=audio_url,
        native={"source_description": "人工描述", "manual": "保留"},
    )


def _resolved(
    *, platform: str = PLATFORM, video_id: str = VIDEO_ID, video_url: str | None = FRESH_URL
) -> ResolvedViralLink:
    return ResolvedViralLink(
        platform=platform,  # type: ignore[arg-type]
        video_id=video_id,
        title="解析器标题，不能覆盖人工标题",
        author="解析器作者",
        cover_url="https://cdn.example.test/resolver-cover.jpg",
        video_url=video_url,
        audio_url="https://cdn.example.test/resolver-audio.mp3",
        duration_ms=99_000,
        source_description="解析器描述",
        metadata=ResolvedViralMetadata(likes=999, tags=("解析器标签",)),
        native={"manual": "不应覆盖"},
    )


class _Resolver:
    def __init__(
        self,
        result: ResolvedViralLink | None = None,
        *,
        error: ViralLinkError | None = None,
        on_resolve: Callable[[], None] | None = None,
    ) -> None:
        self.result = result if result is not None else _resolved()
        self.error = error
        self.on_resolve = on_resolve
        self.calls: list[tuple[str, str]] = []

    def resolve(self, url: str, *, purpose: str) -> ResolvedViralLink:
        self.calls.append((url, purpose))
        if self.on_resolve is not None:
            self.on_resolve()
        if self.error is not None:
            raise self.error
        return self.result


class _Response:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self.headers = {"Content-Length": str(len(body)), "Content-Type": "video/mp4"}
        self._body = BytesIO(body)

    def read(self, size: int) -> bytes:
        return self._body.read(size)


class _Http:
    """保留 UrlFetcher 的状态码处理，阻断 DNS 和 HTTP 实际外呼。"""

    def __init__(self, responses: dict[str, tuple[int, bytes]]) -> None:
        self.responses = responses
        self.errors: dict[str, Exception] = {}
        self.calls: list[str] = []
        self.headers: list[dict[str, str]] = []
        self.on_fetch: Callable[[str], None] | None = None

    def resolve(self, url: str) -> tuple[str, str, int, str]:
        self.calls.append(url)
        if self.on_fetch is not None:
            self.on_fetch(url)
        if url in self.errors:
            raise self.errors[url]
        if url not in self.responses:
            pytest.fail(f"意外的媒体请求：{url}")
        parsed = urlsplit(url)
        assert parsed.hostname is not None
        return parsed.scheme, parsed.hostname, 443, "8.8.8.8"

    def connect(self, scheme: str, hostname: str, port: int, _ip: str, _timeout: float) -> Any:
        http = self

        class Connection:
            def request(self, method: str, target: str, *, headers: Mapping[str, str]) -> None:
                assert method == "GET" and port == 443
                self.url = f"{scheme}://{hostname}{target}"
                http.headers.append(dict(headers))

            def getresponse(self) -> _Response:
                status, body = http.responses[self.url]
                return _Response(status, body)

            def close(self) -> None:
                pass

        return Connection()


def _install_http(monkeypatch: pytest.MonkeyPatch, http: _Http) -> None:
    monkeypatch.setattr(viral_media, "_resolve_public_http_url", http.resolve)
    monkeypatch.setattr(viral_media, "_pinned_connection", http.connect)


def _install_archive_fakes(
    monkeypatch: pytest.MonkeyPatch,
    *,
    http: _Http,
    resolver: _Resolver,
    source_client: Any = None,
) -> None:
    _install_http(monkeypatch, http)

    def source_factory(_conn: Any) -> Any:
        if source_client is None:
            pytest.fail("直链转存不应创建 TikHub 客户端")
        return source_client

    def resolver_factory(_conn: Any) -> _Resolver:
        return resolver

    monkeypatch.setattr(viral_collection, "viral_source_client_from_settings", source_factory)
    monkeypatch.setattr(viral_collection, "douyidou_link_client_from_settings", resolver_factory)
    monkeypatch.setattr(viral_link, "douyidou_link_client_from_settings", resolver_factory)
    monkeypatch.setattr(
        viral_collection, "wechat_link_client_from_settings_or_none", lambda _c: None
    )


def _storage() -> FakeStorageAdapter:
    return FakeStorageAdapter(provider="fake", bucket="archive-refresh")


def _seed_video(
    raw: psycopg.Connection,
    *,
    platform: str = PLATFORM,
    video_id: str = VIDEO_ID,
    play_url: str | None = EXPIRED_URL,
    native: dict[str, Any] | None = None,
) -> None:
    video = _video(platform=platform, video_id=video_id, play_url=play_url)
    raw.execute(
        "INSERT INTO viral_videos (platform, video_id, category, title, author, author_avatar, "
        "verified, cover_url, duration_ms, likes, comments, shares, collects, published_at, "
        "published_display, like_display, tags_json, play_url, audio_url, native_json) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
        "%s, %s, %s, %s, %s, %s)",
        (
            video.platform,
            video.video_id,
            video.category,
            video.title,
            video.author,
            video.author_avatar,
            int(video.verified),
            video.cover_url,
            video.duration_ms,
            video.likes,
            video.comments,
            video.shares,
            video.collects,
            video.published_at,
            video.published_display,
            video.like_display,
            json.dumps(video.tags, ensure_ascii=False),
            video.play_url,
            video.audio_url,
            json.dumps(native if native is not None else video.native, ensure_ascii=False),
        ),
    )
    raw.execute(
        "INSERT INTO viral_content_sources(batch_id,source_kind,platform,video_id,keyword) "
        "VALUES('archive-operator','admin_link_import',%s,%s,'') ON CONFLICT DO NOTHING",
        (platform, video_id),
    )


def _seed_running_task(raw: psycopg.Connection, *, platform: str = PLATFORM) -> None:
    raw.execute(
        "INSERT INTO viral_refresh_tasks (id, platform, sort, status, attempt, "
        "locked_by, locked_until, collection_config_json) "
        "VALUES ('task-archive-refresh', %s, 'latest', 'RUNNING', 1, "
        "'archive-refresh-worker', '2099-01-01 00:00:00', %s)",
        (platform, json.dumps({"kind": "single_archive", "video_id": VIDEO_ID})),
    )


def _lease(platform: str = PLATFORM) -> ViralRefreshLease:
    return ViralRefreshLease(
        id="task-archive-refresh",
        worker_id="archive-refresh-worker",
        platform=platform,
        sort="latest",
        attempt=1,
    )


def _seed_archive(
    dsn: str,
    *,
    platform: str = PLATFORM,
    play_url: str | None = EXPIRED_URL,
    native: dict[str, Any] | None = None,
) -> None:
    with psycopg.connect(dsn, autocommit=True) as raw:
        _seed_video(raw, platform=platform, play_url=play_url, native=native)
        _seed_running_task(raw, platform=platform)


def _stored_video(dsn: str, platform: str = PLATFORM, video_id: str = VIDEO_ID) -> tuple[Any, ...]:
    with psycopg.connect(dsn) as raw:
        row = raw.execute(
            "SELECT play_url, category, title, author, author_avatar, verified, cover_url, "
            "cover_key, duration_ms, likes, comments, shares, collects, published_at, "
            "published_display, like_display, tags_json, audio_url, native_json "
            "FROM viral_videos WHERE platform=%s AND video_id=%s",
            (platform, video_id),
        ).fetchone()
    assert row is not None
    return row


def _preparation(dsn: str, platform: str = PLATFORM, video_id: str = VIDEO_ID) -> tuple[Any, ...]:
    with psycopg.connect(dsn) as raw:
        row = raw.execute(
            "SELECT status, attempt, locked_by, storage_uri FROM viral_media_preparations "
            "WHERE platform=%s AND video_id=%s AND media_kind='video'",
            (platform, video_id),
        ).fetchone()
    assert row is not None
    return row


def _checkpoint(dsn: str) -> dict[str, Any]:
    with psycopg.connect(dsn) as raw:
        row = raw.execute(
            "SELECT checkpoint_json FROM viral_refresh_tasks WHERE id=%s", (_lease().id,)
        ).fetchone()
    assert row is not None
    return json.loads(row[0])


def _refresh_billing(dsn: str) -> dict[str, dict[str, Any]]:
    with psycopg.connect(dsn, row_factory=dict_row) as raw:
        rows = raw.execute(
            "SELECT o.id AS operation_id,a.id AS attempt_id,o.state AS operation_state,"
            "a.state AS attempt_state,a.usage,a.cost_fen,o.actual_units,o.charged_credits,"
            "o.user_id,o.module,o.api_metadata,"
            "o.completed_at IS NOT NULL AS operation_completed,"
            "a.completed_at IS NOT NULL AS attempt_completed "
            "FROM billing_operations o JOIN billing_attempts a ON a.operation_id=o.id "
            "WHERE o.service='link_resolution' "
            "AND o.api_metadata->>'api_type'='viral_media_refresh' "
            "AND o.api_metadata->>'refresh_task_id'=%s ORDER BY o.id,a.id",
            (_lease().id,),
        ).fetchall()
    billing = {row["operation_id"]: row for row in rows}
    assert len(billing) == len(rows)
    return billing


def _reclaim_archive_task(dsn: str) -> ViralRefreshLease:
    with psycopg.connect(dsn, autocommit=True) as raw:
        raw.execute(
            "UPDATE viral_refresh_tasks SET locked_until='2020-01-01 00:00:00' WHERE id=%s",
            (_lease().id,),
        )
        # 用数据库时钟结束冷却，重试回归不等待真实的五秒窗口。
        raw.execute(
            "UPDATE viral_media_preparations SET updated_at='2020-01-01 00:00:00' "
            "WHERE platform=%s AND video_id=%s AND media_kind='video'",
            (PLATFORM, VIDEO_ID),
        )
    with psycopg.connect(dsn) as raw:
        reclaimed = acquire_viral_refresh_task(
            BusinessConnection.postgres(raw), worker_id="reclaimed-worker"
        )
    assert reclaimed is not None and reclaimed.id == _lease().id and reclaimed.attempt == 2
    return reclaimed


def _assert_resolved_once(resolver: _Resolver) -> None:
    assert len(resolver.calls) == 1
    url, purpose = resolver.calls[0]
    assert VIDEO_ID in url
    assert purpose == "replica"


@pytest.fixture()
def archive_state(route_state: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    close_pg_pool()
    monkeypatch.setenv(DATABASE_URL_ENV, route_state)
    yield route_state
    close_pg_pool()


def test_old_archive_task_cannot_prepare_customer_only_material(
    archive_state: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_archive(archive_state)
    with psycopg.connect(archive_state) as raw:
        raw.execute(
            "DELETE FROM viral_content_sources WHERE platform=%s AND video_id=%s",
            (PLATFORM, VIDEO_ID),
        )
        raw.execute(
            "INSERT INTO viral_content_sources(batch_id,source_kind,platform,video_id,keyword) "
            "VALUES('private-old-task','customer_import',%s,%s,'')",
            (PLATFORM, VIDEO_ID),
        )
    before = _stored_video(archive_state)
    http = _Http({})
    _install_http(monkeypatch, http)
    storage = _storage()
    with pytest.raises(ViralLinkError) as caught:
        viral_collection._run_single_archive(_lease(), storage, VIDEO_ID)
    assert caught.value.code == "VIRAL_VIDEO_NOT_FOUND"
    assert caught.value.retryable is False
    assert http.calls == []
    assert _stored_video(archive_state) == before
    assert storage.head_object(viral_media.viral_media_key(PLATFORM, VIDEO_ID, "video")) is None
    with psycopg.connect(archive_state) as raw:
        assert (
            raw.execute(
                "SELECT count(*) FROM viral_media_preparations WHERE platform=%s AND video_id=%s",
                (PLATFORM, VIDEO_ID),
            ).fetchone()[0]
            == 0
        )
        assert (
            raw.execute(
                "SELECT count(*) FROM billing_operations WHERE api_metadata->>'refresh_task_id'=%s",
                (_lease().id,),
            ).fetchone()[0]
            == 0
        )


# 真实管线的边界不依赖数据库，便于在 PG 尚未配置时运行。
def test_pipeline_recovers_a_403_once() -> None:
    http = _Http({EXPIRED_URL: (403, b""), FRESH_URL: (200, MP4)})
    calls: list[ViralVideo] = []
    storage = _storage()
    with pytest.MonkeyPatch.context() as monkeypatch:
        _install_http(monkeypatch, http)

        def recover(video: ViralVideo) -> ViralVideo:
            calls.append(video)
            return replace(video, play_url=FRESH_URL)

        result = ViralMediaPipeline(client=None, storage=storage, recover_video=recover).fetch(
            _video(), prefer="video"
        )
    assert http.calls == [EXPIRED_URL, FRESH_URL]
    assert calls == [_video()]
    assert storage.get_object(viral_media.viral_media_key(PLATFORM, VIDEO_ID, "video")) == MP4
    assert result.kind == "video"


@pytest.mark.parametrize("error_type", [ViralMediaDNSUnavailable, ViralMediaBusy])
def test_pipeline_does_not_recover_dns_or_busy(
    monkeypatch: pytest.MonkeyPatch, error_type: type[Exception]
) -> None:
    http = _Http({})
    original_error = error_type("原始下载失败")
    http.errors[EXPIRED_URL] = original_error
    _install_http(monkeypatch, http)
    recovered: list[ViralVideo] = []
    pipeline = ViralMediaPipeline(
        client=None,
        storage=_storage(),
        recover_video=lambda video: recovered.append(video) or video,
    )
    with pytest.raises(error_type) as caught:
        pipeline.fetch(_video(), prefer="video")
    assert caught.value is original_error
    assert http.calls == [EXPIRED_URL]
    assert recovered == []


def test_xiaohongshu_pipeline_downloads_video_and_audio_without_tikhub(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """现有任务表不允许小红书平台，故只验证无客户端的直链下载管线。"""
    http = _Http({EXPIRED_URL: (200, MP4), AUDIO_URL: (200, AUDIO)})
    _install_http(monkeypatch, http)
    storage = _storage()
    pipeline = ViralMediaPipeline(client=None, storage=storage)
    video = _video(platform="xiaohongshu")
    assert pipeline.fetch(video, prefer="video").kind == "video"
    assert pipeline.fetch(video, prefer="audio").kind == "audio"
    assert http.calls == [EXPIRED_URL, AUDIO_URL]
    assert storage.get_object(viral_media.viral_media_key(video.platform, VIDEO_ID, "video")) == MP4
    assert (
        storage.get_object(viral_media.viral_media_key(video.platform, VIDEO_ID, "audio")) == AUDIO
    )


def test_xiaohongshu_pipeline_preserves_direct_download_failure_without_tikhub(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """小红书暂无任务表支持，异常路径同样在独立媒体管线中验证。"""
    http = _Http({EXPIRED_URL: (403, b"")})
    _install_http(monkeypatch, http)
    storage = _storage()
    video = _video(platform="xiaohongshu")
    with pytest.raises(ViralMediaError, match="媒体地址返回异常状态"):
        ViralMediaPipeline(client=None, storage=storage).fetch(video, prefer="video")
    assert http.calls == [EXPIRED_URL]
    assert (
        storage.head_object(viral_media.viral_media_key(video.platform, VIDEO_ID, "video")) is None
    )


def test_pipeline_does_not_recover_failed_audio(monkeypatch: pytest.MonkeyPatch) -> None:
    http = _Http({AUDIO_URL: (403, b"")})
    _install_http(monkeypatch, http)
    recovered: list[ViralVideo] = []
    pipeline = ViralMediaPipeline(
        client=None,
        storage=_storage(),
        recover_video=lambda video: recovered.append(video) or video,
    )
    with pytest.raises(ViralMediaError, match="媒体地址返回异常状态"):
        pipeline.fetch(_video(), prefer="audio")
    assert http.calls == [AUDIO_URL]
    assert recovered == []


def test_pipeline_recovers_missing_play_url(monkeypatch: pytest.MonkeyPatch) -> None:
    http = _Http({FRESH_URL: (200, MP4)})
    _install_http(monkeypatch, http)
    recovered: list[ViralVideo] = []

    def recover(video: ViralVideo) -> ViralVideo:
        recovered.append(video)
        return replace(video, play_url=FRESH_URL)

    ViralMediaPipeline(client=None, storage=_storage(), recover_video=recover).fetch(
        _video(play_url=None), prefer="video"
    )
    assert recovered == [_video(play_url=None)]
    assert http.calls == [FRESH_URL]


def test_pipeline_never_recovers_twice(monkeypatch: pytest.MonkeyPatch) -> None:
    http = _Http({EXPIRED_URL: (403, b""), FRESH_URL: (403, b"")})
    _install_http(monkeypatch, http)
    recovered: list[ViralVideo] = []

    def recover(video: ViralVideo) -> ViralVideo:
        recovered.append(video)
        return replace(video, play_url=FRESH_URL)

    with pytest.raises(ViralMediaError, match="媒体地址返回异常状态"):
        ViralMediaPipeline(client=None, storage=_storage(), recover_video=recover).fetch(
            _video(), prefer="video"
        )
    assert http.calls == [EXPIRED_URL, FRESH_URL]
    assert recovered == [_video()]


def test_refresh_only_returns_a_replaced_video_without_database_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(DATABASE_URL_ENV, raising=False)
    monkeypatch.setattr(viral_collection, "record_external_call", lambda **_kwargs: None)
    monkeypatch.setattr(
        viral_collection, "_connection", lambda: pytest.fail("解析期间不应访问数据库")
    )
    original = _video()
    resolver = _Resolver()
    refreshed = viral_collection._refresh_douyin_play_url(resolver, original)  # type: ignore[arg-type]
    assert refreshed == replace(original, play_url=FRESH_URL)
    assert original.play_url == EXPIRED_URL
    _assert_resolved_once(resolver)


@pytest.mark.parametrize(
    ("retryable", "uncertain"), [(True, False), (True, True), (False, False), (False, True)]
)
def test_refresh_propagates_all_link_errors(
    retryable: bool, uncertain: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(DATABASE_URL_ENV, raising=False)
    monkeypatch.setattr(viral_collection, "record_external_call", lambda **_kwargs: None)
    error = ViralLinkError(
        504, "TEST_LINK_ERROR", "解析异常", retryable=retryable, uncertain=uncertain
    )
    resolver = _Resolver(error=error)
    with pytest.raises(ViralLinkError) as caught:
        viral_collection._refresh_douyin_play_url(resolver, _video())  # type: ignore[arg-type]
    assert caught.value is error
    _assert_resolved_once(resolver)


@pytest.mark.parametrize(
    "video", [_video(platform="wechat_channels"), _video(video_id="invalid-video-id")]
)
def test_refresh_rejects_unsupported_identity_without_resolving(video: ViralVideo) -> None:
    resolver = _Resolver()
    assert viral_collection._refresh_douyin_play_url(resolver, video) is None  # type: ignore[arg-type]
    assert resolver.calls == []


@pytest.mark.parametrize(
    ("platform", "native"),
    [
        ("douyin", None),
        ("wechat_channels", {"share_url": "https://weixin.qq.com/sph/example"}),
    ],
)
def test_direct_archive_does_not_construct_tikhub(
    archive_state: str,
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    native: dict[str, Any] | None,
) -> None:
    _seed_archive(archive_state, platform=platform, native=native)
    http = _Http({EXPIRED_URL: (200, MP4), AUDIO_URL: (200, AUDIO)})
    resolver = _Resolver()
    _install_archive_fakes(monkeypatch, http=http, resolver=resolver)
    storage = _storage()
    viral_collection._run_single_archive(_lease(platform), storage, VIDEO_ID)
    assert http.calls == (
        [EXPIRED_URL] if platform == "wechat_channels" else [EXPIRED_URL, AUDIO_URL]
    )
    if platform == "wechat_channels":
        assert http.headers[0]["Referer"] == "https://channels.weixin.qq.com/"
    assert resolver.calls == []
    assert _preparation(archive_state, platform)[:2] == ("SUCCEEDED", 1)
    cached = ViralMediaPreparation(storage=storage).cached(
        platform=platform, video_id=VIDEO_ID, kind="video"
    )
    assert cached is not None and storage.get_object(cached.key) == MP4


def test_single_archive_recovers_403_within_one_shared_lease(
    archive_state: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_archive(archive_state)
    before = _stored_video(archive_state)
    events: list[tuple[str, tuple[Any, ...], str | None]] = []

    def record(event: str) -> None:
        events.append((event, _preparation(archive_state), _stored_video(archive_state)[0]))

    http = _Http({EXPIRED_URL: (403, b""), FRESH_URL: (200, MP4), AUDIO_URL: (200, AUDIO)})
    http.on_fetch = record
    resolver = _Resolver(on_resolve=lambda: record("resolve"))
    _install_archive_fakes(monkeypatch, http=http, resolver=resolver)
    storage = _storage()
    original_publish = ViralMediaPreparation._publish
    publication: list[tuple[tuple[Any, ...], str | None, tuple[Any, ...], str | None]] = []

    def observe_publish(self: ViralMediaPreparation, media_lease: Any, stored: Any) -> None:
        state_before = _preparation(archive_state)
        url_before = _stored_video(archive_state)[0]
        original_publish(self, media_lease, stored)
        if state_before[0] == "RUNNING":
            publication.append(
                (
                    state_before,
                    url_before,
                    _preparation(archive_state),
                    _stored_video(archive_state)[0],
                )
            )

    monkeypatch.setattr(ViralMediaPreparation, "_publish", observe_publish)
    viral_collection._run_single_archive(_lease(), storage, VIDEO_ID)

    assert http.calls == [EXPIRED_URL, FRESH_URL, AUDIO_URL]
    _assert_resolved_once(resolver)
    assert [event for event, _, _ in events] == [EXPIRED_URL, "resolve", FRESH_URL, AUDIO_URL]
    during_video = [state for _, state, _ in events[:3]]
    assert all(state[:2] == ("RUNNING", 1) for state in during_video)
    assert len({state[2] for state in during_video}) == 1 and during_video[0][2]
    assert all(state[3] is None for state in during_video)
    assert all(url == EXPIRED_URL for _, _, url in events[:3])
    assert len(publication) == 1
    state_before, url_before, state_after, url_after = publication[0]
    assert state_before[:2] == ("RUNNING", 1)
    assert state_after[:2] == ("SUCCEEDED", 1) and state_after[3]
    assert url_before == url_after == EXPIRED_URL
    assert _preparation(archive_state)[:2] == ("SUCCEEDED", 1)
    progress = _checkpoint(archive_state)
    assert progress["media_refresh_requests"][VIDEO_ID] == "DONE"
    assert progress["media_refresh_results"][VIDEO_ID] == {
        "original_url": EXPIRED_URL,
        "play_url": FRESH_URL,
    }
    after = _stored_video(archive_state)
    assert after[0] == FRESH_URL
    assert after[1:] == before[1:]
    cached = ViralMediaPreparation(storage=storage).cached(
        platform=PLATFORM, video_id=VIDEO_ID, kind="video"
    )
    assert cached is not None and storage.get_object(cached.key) == MP4


def test_successful_refresh_records_platform_cost_without_customer_charge(
    archive_state: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_archive(archive_state)
    with psycopg.connect(archive_state) as raw:
        original_tariff = raw.execute(
            "SELECT enabled, unit_credits, unit_cost_fen FROM billing_tariffs "
            "WHERE service='link_resolution'"
        ).fetchone()
    try:
        with psycopg.connect(archive_state) as raw:
            raw.execute(
                "INSERT INTO billing_tariffs(service,enabled,unit_credits,unit_cost_fen) "
                "VALUES ('link_resolution',true,5,2.5) ON CONFLICT(service) DO UPDATE SET "
                "enabled=excluded.enabled,unit_credits=excluded.unit_credits,"
                "unit_cost_fen=excluded.unit_cost_fen"
            )
            raw.execute(
                "INSERT INTO wallets(user_id,available_credits,reserved_credits) "
                "VALUES ('customer_u',100,0) ON CONFLICT(user_id) DO UPDATE SET "
                "available_credits=100,reserved_credits=0"
            )
            customer_operation = accept_operation(
                BusinessConnection.postgres(raw),
                user_id="customer_u",
                service="link_resolution",
                source_id="customer-archive-context",
                units=1,
            )
        with psycopg.connect(archive_state) as raw:
            wallet_before = raw.execute(
                "SELECT available_credits,reserved_credits FROM wallets WHERE user_id='customer_u'"
            ).fetchone()
            ledger_before = raw.execute("SELECT count(*) FROM wallet_transactions").fetchone()[0]
        http = _Http({EXPIRED_URL: (403, b""), FRESH_URL: (200, MP4), AUDIO_URL: (200, AUDIO)})
        resolver = _Resolver()
        _install_archive_fakes(monkeypatch, http=http, resolver=resolver)
        with billing_context("customer-archive-context"):
            viral_collection._run_single_archive(_lease(), _storage(), VIDEO_ID)
        _assert_resolved_once(resolver)
        with psycopg.connect(archive_state) as raw:
            rows = raw.execute(
                "SELECT o.user_id,o.module,o.state,o.actual_units,o.reserved_credits,"
                "o.charged_credits,a.state,a.usage,a.unit_cost_fen,a.cost_fen,"
                "o.api_metadata->>'video_platform',o.api_metadata->>'video_id' "
                "FROM billing_operations o JOIN billing_attempts a ON a.operation_id=o.id "
                "WHERE o.service='link_resolution' AND o.user_id IS NULL"
            ).fetchall()
            assert rows == [
                (
                    None,
                    "platform",
                    "SUCCEEDED",
                    1,
                    0,
                    0,
                    "ACTUAL",
                    1,
                    Decimal("2.5"),
                    Decimal("2.5"),
                    PLATFORM,
                    VIDEO_ID,
                )
            ]
            assert (
                raw.execute(
                    "SELECT count(*) FROM billing_attempts WHERE operation_id=%s",
                    (customer_operation,),
                ).fetchone()[0]
                == 0
            )
            assert (
                raw.execute(
                    "SELECT available_credits,reserved_credits FROM wallets "
                    "WHERE user_id='customer_u'"
                ).fetchone()
                == wallet_before
            )
            assert (
                raw.execute("SELECT count(*) FROM wallet_transactions").fetchone()[0]
                == ledger_before
            )
            conn = BusinessConnection.postgres(raw)
            budget = collection_budget(conn)
            assert budget["month_spend_fen"] == budget["month_total_cost_fen"] == 2.5
            assert budget["month_unknown_cost_count"] == budget["month_pending_cost_count"] == 0
            metrics = video_business_metrics(conn, platform=PLATFORM, video_id=VIDEO_ID)
            assert metrics["attributedDataCalls"] == 1 and metrics["knownDataCostFen"] == 2.5
            assert metrics["unknownDataCostCalls"] == metrics["pendingDataCostCalls"] == 0
            assert metrics["chargedCredits"] == metrics["customerTotal"] == 0
            today = raw.execute(
                "SELECT (clock_timestamp() AT TIME ZONE 'Asia/Shanghai')::date"
            ).fetchone()[0]
            finance = content_overview(conn, start=today, end=today)["finance"]
            assert finance["knownInterfaceCostFen"] == finance["recordedCostFen"] == 2.5
            assert finance["knownCostFen"] == 2.5 + finance["knownResourceCostFen"]
            assert finance["knownRevenueFen"] == 0
    finally:
        with psycopg.connect(archive_state, autocommit=True) as raw:
            if original_tariff is None:
                raw.execute("DELETE FROM billing_tariffs WHERE service='link_resolution'")
            else:
                raw.execute(
                    "UPDATE billing_tariffs SET enabled=%s,unit_credits=%s,unit_cost_fen=%s "
                    "WHERE service='link_resolution'",
                    original_tariff,
                )


@pytest.mark.parametrize("changed_field", ["price", "version"])
def test_archive_estimate_includes_link_cost_and_rejects_changed_snapshot(
    archive_state: str, changed_field: str
) -> None:
    from fastapi import HTTPException

    from app.admin_viral_costs import (
        OperationEstimateRequest,
        cost_snapshot,
        operation_estimate,
        require_cost_snapshot,
    )

    _seed_archive(archive_state)
    with psycopg.connect(archive_state) as raw:
        try:
            raw.execute(
                "INSERT INTO billing_tariffs(service,enabled,unit_credits,unit_cost_fen,version) "
                "VALUES ('viral_data',true,0,3,1),('link_resolution',true,0,2.5,1) "
                "ON CONFLICT(service) DO UPDATE SET enabled=excluded.enabled,"
                "unit_credits=excluded.unit_credits,unit_cost_fen=excluded.unit_cost_fen,"
                "version=excluded.version"
            )
            _seed_video(raw, platform="wechat_channels")
            payload = OperationEstimateRequest.model_validate(
                {
                    "action": "archive",
                    "items": [
                        {"platform": PLATFORM, "video_id": VIDEO_ID},
                        {"platform": PLATFORM, "video_id": VIDEO_ID},
                        {"platform": PLATFORM, "video_id": "admin-video/opaque=id"},
                        {"platform": "wechat_channels", "video_id": VIDEO_ID},
                    ],
                }
            )
            conn = BusinessConnection.postgres(raw)
            operations_before = raw.execute("SELECT count(*) FROM billing_operations").fetchone()[0]
            snapshot, data_price = cost_snapshot(conn)
            estimate = operation_estimate(conn, payload)
            assert estimate["snapshot"] == snapshot and data_price == 3
            assert estimate["logicalCallsMax"] == 1 and estimate["normalRetryCallsMax"] == 3
            assert estimate["linkResolutionCallsMax"] == 3
            assert estimate["linkResolutionUnitCostFen"] == 2.5
            assert estimate["dataCostMaxFen"] == 16.5
            assert estimate["mediaDownloadsMax"] == 3 and estimate["reusedVideos"] == 0
            assert estimate["customerCredits"] == 0 and estimate["totalCostFen"] is None
            require_cost_snapshot(raw, snapshot)

            if changed_field == "price":
                raw.execute(
                    "UPDATE billing_tariffs SET unit_cost_fen=7.5 WHERE service='link_resolution'"
                )
            else:
                raw.execute("UPDATE billing_tariffs SET version=2 WHERE service='link_resolution'")
            updated_snapshot, updated_data_price = cost_snapshot(conn)
            assert updated_snapshot != snapshot and updated_data_price == data_price
            updated_estimate = operation_estimate(conn, payload)
            assert updated_estimate["snapshot"] == updated_snapshot
            assert updated_estimate["linkResolutionCallsMax"] == 3
            assert updated_estimate["linkResolutionUnitCostFen"] == (
                7.5 if changed_field == "price" else 2.5
            )
            assert updated_estimate["dataCostMaxFen"] == (
                31.5 if changed_field == "price" else 16.5
            )
            with pytest.raises(HTTPException) as caught:
                require_cost_snapshot(raw, snapshot)
            assert caught.value.status_code == 409
            assert caught.value.detail["code"] == "VIRAL_COST_CHANGED"
            require_cost_snapshot(raw, updated_snapshot)
            assert (
                raw.execute("SELECT count(*) FROM billing_operations").fetchone()[0]
                == operations_before
            )
        finally:
            # 价格夹具全部回滚，避免影响复用同一迁移库的其他测试。
            raw.rollback()


def test_single_archive_keeps_valid_resolved_url_when_concurrent_url_is_invalid(
    archive_state: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_archive(archive_state)
    before = _stored_video(archive_state)

    def write_concurrent_invalid_url() -> None:
        assert _preparation(archive_state)[:2] == ("RUNNING", 1)
        with psycopg.connect(archive_state) as raw:
            raw.execute("SET LOCAL statement_timeout = '2s'")
            raw.execute(
                "UPDATE viral_videos SET play_url=%s WHERE platform=%s AND video_id=%s",
                (CONCURRENT_URL, PLATFORM, VIDEO_ID),
            )

    http = _Http(
        {
            EXPIRED_URL: (403, b""),
            FRESH_URL: (200, MP4),
            CONCURRENT_URL: (403, b""),
            AUDIO_URL: (200, AUDIO),
        }
    )
    resolver = _Resolver(on_resolve=write_concurrent_invalid_url)
    _install_archive_fakes(monkeypatch, http=http, resolver=resolver)
    storage = _storage()
    viral_collection._run_single_archive(_lease(), storage, VIDEO_ID)
    _assert_resolved_once(resolver)
    assert http.calls == [EXPIRED_URL, FRESH_URL, AUDIO_URL]
    assert _preparation(archive_state)[:2] == ("SUCCEEDED", 1)
    after = _stored_video(archive_state)
    assert after[0] == CONCURRENT_URL and after[1:] == before[1:]
    progress = _checkpoint(archive_state)
    assert progress["media_refresh_requests"][VIDEO_ID] == "DONE"
    assert progress["media_refresh_results"][VIDEO_ID] == {
        "original_url": EXPIRED_URL,
        "play_url": FRESH_URL,
    }
    cached = ViralMediaPreparation(storage=storage).cached(
        platform=PLATFORM, video_id=VIDEO_ID, kind="video"
    )
    assert cached is not None and storage.get_object(cached.key) == MP4


def test_single_archive_recovers_missing_play_url_in_same_lease(
    archive_state: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_archive(archive_state, play_url=None)
    before = _stored_video(archive_state)
    observed: list[tuple[Any, ...]] = []
    http = _Http({FRESH_URL: (200, MP4), AUDIO_URL: (200, AUDIO)})
    resolver = _Resolver(on_resolve=lambda: observed.append(_preparation(archive_state)))
    _install_archive_fakes(monkeypatch, http=http, resolver=resolver)
    storage = _storage()
    viral_collection._run_single_archive(_lease(), storage, VIDEO_ID)
    _assert_resolved_once(resolver)
    assert len(observed) == 1 and observed[0][:2] == ("RUNNING", 1) and observed[0][2]
    assert _preparation(archive_state)[:2] == ("SUCCEEDED", 1)
    after = _stored_video(archive_state)
    assert after[0] == FRESH_URL and after[1:] == before[1:]
    assert http.calls == [FRESH_URL, AUDIO_URL]


def test_single_archive_second_download_failure_does_not_store_fresh_url(
    archive_state: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_archive(archive_state)
    before = _stored_video(archive_state)
    http = _Http({EXPIRED_URL: (403, b""), FRESH_URL: (403, b"")})
    resolver = _Resolver()
    _install_archive_fakes(monkeypatch, http=http, resolver=resolver)
    with pytest.raises(ViralMediaError, match="媒体地址返回异常状态"):
        viral_collection._run_single_archive(_lease(), _storage(), VIDEO_ID)
    _assert_resolved_once(resolver)
    assert http.calls == [EXPIRED_URL, FRESH_URL]
    assert _stored_video(archive_state) == before
    assert _preparation(archive_state)[:2] == ("FAILED", 1)


def test_single_archive_publish_failure_does_not_store_fresh_url(
    archive_state: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_archive(archive_state)
    before = _stored_video(archive_state)
    http = _Http({EXPIRED_URL: (403, b""), FRESH_URL: (200, MP4)})
    resolver = _Resolver()
    _install_archive_fakes(monkeypatch, http=http, resolver=resolver)

    def fail_publish(_self: Any, _lease: Any, _stored: Any) -> None:
        raise ViralMediaLeaseLost("发布租约已失效")

    monkeypatch.setattr(ViralMediaPreparation, "_publish", fail_publish)
    with pytest.raises(ViralMediaLeaseLost):
        viral_collection._run_single_archive(_lease(), _storage(), VIDEO_ID)
    _assert_resolved_once(resolver)
    assert http.calls == [EXPIRED_URL, FRESH_URL]
    assert _stored_video(archive_state) == before
    assert _preparation(archive_state)[:2] == ("FAILED", 1)


@pytest.mark.parametrize("saved_url_expired", [False, True])
def test_single_archive_retry_reuses_resolved_url_before_resolving_again(
    archive_state: str, monkeypatch: pytest.MonkeyPatch, saved_url_expired: bool
) -> None:
    _seed_archive(archive_state)
    before = _stored_video(archive_state)
    http = _Http(
        {
            EXPIRED_URL: (403, b""),
            FRESH_URL: (200, MP4),
            RENEWED_URL: (200, MP4),
            AUDIO_URL: (200, AUDIO),
        }
    )
    resolver = _Resolver()
    _install_archive_fakes(monkeypatch, http=http, resolver=resolver)
    storage = _storage()
    original_put_file = storage.put_file
    uploads: list[str] = []

    def fail_first_upload(key: str, path: Path, *, content_type: str) -> Any:
        uploads.append(key)
        if len(uploads) == 1:
            raise StorageBackendUnavailable("存储暂时不可用")
        return original_put_file(key, path, content_type=content_type)

    monkeypatch.setattr(storage, "put_file", fail_first_upload)
    with pytest.raises(StorageBackendUnavailable, match="存储暂时不可用"):
        viral_collection._run_single_archive(_lease(), storage, VIDEO_ID)
    _assert_resolved_once(resolver)
    assert http.calls == [EXPIRED_URL, FRESH_URL]
    assert _stored_video(archive_state) == before
    assert _preparation(archive_state)[:2] == ("FAILED", 1)
    progress = _checkpoint(archive_state)
    assert progress["media_refresh_requests"][VIDEO_ID] == "DONE"
    assert progress["media_refresh_results"][VIDEO_ID] == {
        "original_url": EXPIRED_URL,
        "play_url": FRESH_URL,
    }
    original_accounting = progress["media_refresh_accounting"][VIDEO_ID]
    original_billing = _refresh_billing(archive_state)
    assert len(original_billing) == 1
    original_operation = original_billing[original_accounting["operation_id"]]
    assert original_operation["attempt_id"] == original_accounting["attempt_id"]
    assert original_operation["operation_state"] == "SUCCEEDED"
    assert original_operation["usage"] == original_operation["actual_units"] == 1
    assert original_operation["attempt_completed"] and original_operation["operation_completed"]

    reclaimed = _reclaim_archive_task(archive_state)
    if saved_url_expired:
        http.responses[FRESH_URL] = (403, b"")
        resolver.result = _resolved(video_url=RENEWED_URL)

        def verify_saved_url_was_tried() -> None:
            assert http.calls == [EXPIRED_URL, FRESH_URL, FRESH_URL]

        resolver.on_resolve = verify_saved_url_was_tried
    viral_collection._run_single_archive(reclaimed, storage, VIDEO_ID)

    expected_url = RENEWED_URL if saved_url_expired else FRESH_URL
    assert http.calls == (
        [EXPIRED_URL, FRESH_URL, FRESH_URL, RENEWED_URL, AUDIO_URL]
        if saved_url_expired
        else [EXPIRED_URL, FRESH_URL, FRESH_URL, AUDIO_URL]
    )
    assert len(resolver.calls) == (2 if saved_url_expired else 1)
    assert all(VIDEO_ID in url and purpose == "replica" for url, purpose in resolver.calls)
    assert _preparation(archive_state)[:2] == ("SUCCEEDED", 2)
    after = _stored_video(archive_state)
    assert after[0] == expected_url and after[1:] == before[1:]
    progress = _checkpoint(archive_state)
    assert progress["media_refresh_requests"][VIDEO_ID] == "DONE"
    assert progress["media_refresh_results"][VIDEO_ID] == {
        "original_url": EXPIRED_URL,
        "play_url": expected_url,
    }
    billing = _refresh_billing(archive_state)
    assert billing[original_accounting["operation_id"]] == original_operation
    assert len(billing) == (2 if saved_url_expired else 1)
    if saved_url_expired:
        accounting = progress["media_refresh_accounting"][VIDEO_ID]
        assert accounting["task_attempt"] == reclaimed.attempt
        assert accounting["operation_id"] != original_accounting["operation_id"]
        assert accounting["attempt_id"] != original_accounting["attempt_id"]
        operation = billing[accounting["operation_id"]]
        assert operation["attempt_id"] == accounting["attempt_id"]
        assert operation["operation_state"] == "SUCCEEDED"
        assert operation["usage"] == operation["actual_units"] == 1
        assert operation["charged_credits"] == 0
        assert operation["attempt_completed"] and operation["operation_completed"]
    else:
        assert progress["media_refresh_accounting"][VIDEO_ID] == original_accounting
        assert billing == original_billing
    cached = ViralMediaPreparation(storage=storage).cached(
        platform=PLATFORM, video_id=VIDEO_ID, kind="video"
    )
    assert cached is not None and storage.get_object(cached.key) == MP4


def test_single_archive_cached_retry_repairs_cas_after_publication_interruption(
    archive_state: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_archive(archive_state)
    before = _stored_video(archive_state)
    http = _Http({EXPIRED_URL: (403, b""), FRESH_URL: (200, MP4), AUDIO_URL: (200, AUDIO)})
    resolver = _Resolver()
    _install_archive_fakes(monkeypatch, http=http, resolver=resolver)
    storage = _storage()
    original_store = viral_collection._store_douyin_play_url
    cas_attempts: list[tuple[int, str]] = []

    class Interrupted(BaseException):
        pass

    def interrupt_before_cas(
        original: ViralVideo,
        refreshed: ViralVideo,
        *,
        lease: ViralRefreshLease,
        storage_uri: str,
    ) -> ViralVideo | None:
        assert _preparation(archive_state)[:2] == ("SUCCEEDED", 1)
        assert _preparation(archive_state)[3] == storage_uri
        assert original.play_url == EXPIRED_URL and refreshed.play_url == FRESH_URL
        cas_attempts.append((lease.attempt, storage_uri))
        if len(cas_attempts) == 1:
            raise Interrupted()
        return original_store(original, refreshed, lease=lease, storage_uri=storage_uri)

    monkeypatch.setattr(viral_collection, "_store_douyin_play_url", interrupt_before_cas)
    with pytest.raises(Interrupted):
        viral_collection._run_single_archive(_lease(), storage, VIDEO_ID)
    _assert_resolved_once(resolver)
    assert http.calls == [EXPIRED_URL, FRESH_URL]
    assert _stored_video(archive_state) == before
    assert _preparation(archive_state)[:2] == ("SUCCEEDED", 1)
    progress = _checkpoint(archive_state)
    assert progress["media_refresh_requests"][VIDEO_ID] == "DONE"
    assert progress["media_refresh_results"][VIDEO_ID] == {
        "original_url": EXPIRED_URL,
        "play_url": FRESH_URL,
    }
    billing_before = _refresh_billing(archive_state)
    assert len(billing_before) == 1
    assert all(row["operation_state"] == "SUCCEEDED" for row in billing_before.values())
    original_fetch = ViralMediaPipeline.fetch
    cache_hits: list[tuple[str | None, bool]] = []

    def observe_fetch(
        self: ViralMediaPipeline, video: ViralVideo, *, prefer: str | None = None
    ) -> Any:
        result = original_fetch(self, video, prefer=prefer)
        cache_hits.append((prefer, result.cache_hit))
        return result

    monkeypatch.setattr(ViralMediaPipeline, "fetch", observe_fetch)
    reclaimed = _reclaim_archive_task(archive_state)
    viral_collection._run_single_archive(reclaimed, storage, VIDEO_ID)
    assert cache_hits == [("video", True), ("audio", False)]
    assert len(cas_attempts) == 2
    assert [attempt for attempt, _uri in cas_attempts] == [1, 2]
    assert cas_attempts[0][1] == cas_attempts[1][1]
    assert _preparation(archive_state)[:2] == ("SUCCEEDED", 1)
    assert http.calls == [EXPIRED_URL, FRESH_URL, AUDIO_URL]
    _assert_resolved_once(resolver)
    assert _refresh_billing(archive_state) == billing_before
    after = _stored_video(archive_state)
    assert after[0] == FRESH_URL and after[1:] == before[1:]


def test_single_archive_media_lease_lost_during_resolution_does_not_store_url(
    archive_state: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_archive(archive_state)
    before = _stored_video(archive_state)
    http = _Http({EXPIRED_URL: (403, b""), FRESH_URL: (200, MP4)})

    def replace_owner() -> None:
        with psycopg.connect(archive_state, autocommit=True) as raw:
            raw.execute(
                "UPDATE viral_media_preparations SET locked_by='other-worker', "
                "locked_until='2099-01-01 00:00:00' WHERE platform=%s AND video_id=%s "
                "AND media_kind='video' AND status='RUNNING'",
                (PLATFORM, VIDEO_ID),
            )

    resolver = _Resolver(on_resolve=replace_owner)
    _install_archive_fakes(monkeypatch, http=http, resolver=resolver)
    with pytest.raises(ViralMediaLeaseLost):
        viral_collection._run_single_archive(_lease(), _storage(), VIDEO_ID)
    _assert_resolved_once(resolver)
    assert _stored_video(archive_state) == before
    assert _preparation(archive_state)[:3] == ("RUNNING", 1, "other-worker")


@pytest.mark.parametrize("mismatch", ["video_id", "platform", "missing_url"])
def test_refresh_rejects_unrelated_result_without_updating_db(
    archive_state: str, mismatch: str
) -> None:
    _seed_archive(archive_state)
    before = _stored_video(archive_state)
    result = {
        "video_id": _resolved(video_id="7294812345678901235"),
        "platform": _resolved(platform="xiaohongshu"),
        "missing_url": _resolved(video_url=None),
    }[mismatch]
    other_before = None
    if mismatch != "missing_url":
        with psycopg.connect(archive_state, autocommit=True) as raw:
            _seed_video(
                raw, platform=result.platform, video_id=result.video_id, play_url=CONCURRENT_URL
            )
        other_before = _stored_video(archive_state, result.platform, result.video_id)
    resolver = _Resolver(result)
    assert viral_collection._refresh_douyin_play_url(resolver, _video()) is None  # type: ignore[arg-type]
    _assert_resolved_once(resolver)
    assert _stored_video(archive_state) == before
    if other_before is not None:
        assert _stored_video(archive_state, result.platform, result.video_id) == other_before


def test_refresh_resolves_outside_transaction_and_store_uses_cas(
    archive_state: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_archive(archive_state)
    before = _stored_video(archive_state)
    original_connection = viral_collection._connection
    open_connections = 0

    @contextmanager
    def monitored_connection() -> Iterator[Any]:
        nonlocal open_connections
        open_connections += 1
        try:
            with original_connection() as conn:
                yield conn
        finally:
            open_connections -= 1

    monkeypatch.setattr(viral_collection, "_connection", monitored_connection)

    def concurrent_update() -> None:
        assert open_connections == 0
        with psycopg.connect(archive_state, autocommit=True) as raw:
            raw.execute("SET statement_timeout = '2s'")
            raw.execute(
                "UPDATE viral_videos SET play_url=%s WHERE platform=%s AND video_id=%s",
                (CONCURRENT_URL, PLATFORM, VIDEO_ID),
            )

    original = _video()
    resolver = _Resolver(on_resolve=concurrent_update)
    refreshed = viral_collection._refresh_douyin_play_url(resolver, original)  # type: ignore[arg-type]
    _assert_resolved_once(resolver)
    assert refreshed == replace(original, play_url=FRESH_URL)
    assert _stored_video(archive_state)[0] == CONCURRENT_URL
    assert open_connections == 0
    storage = _storage()
    stored, cache_hit = ViralMediaPreparation(storage=storage).fetch(
        platform=PLATFORM,
        video_id=VIDEO_ID,
        kind="video",
        prepare=lambda key, _check: storage.put_object(key, MP4, content_type="video/mp4"),
    )
    assert not cache_hit
    with pytest.raises(ViralMediaLeaseLost):
        viral_collection._store_douyin_play_url(
            original, refreshed, lease=_lease(), storage_uri="fake://wrong-object"
        )
    assert _stored_video(archive_state)[0] == CONCURRENT_URL
    latest = viral_collection._store_douyin_play_url(
        original, refreshed, lease=_lease(), storage_uri=stored.uri
    )
    assert latest is not None and latest.play_url == CONCURRENT_URL
    after = _stored_video(archive_state)
    assert after[0] == CONCURRENT_URL
    assert after[1:] == before[1:]
    assert open_connections == 0


def test_single_archive_rejects_other_video_and_preserves_403(
    archive_state: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_archive(archive_state)
    before = _stored_video(archive_state)
    http = _Http({EXPIRED_URL: (403, b"")})
    resolver = _Resolver(_resolved(video_id="7294812345678901235"))
    _install_archive_fakes(monkeypatch, http=http, resolver=resolver)
    with pytest.raises(ViralMediaError, match="媒体地址返回异常状态"):
        viral_collection._run_single_archive(_lease(), _storage(), VIDEO_ID)
    _assert_resolved_once(resolver)
    assert http.calls == [EXPIRED_URL]
    assert _stored_video(archive_state) == before
    assert _preparation(archive_state)[:2] == ("FAILED", 1)


def test_single_archive_keeps_original_error_when_link_resolution_fails(
    archive_state: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_archive(archive_state)
    original_error = ViralMediaError("原始媒体下载错误")
    http = _Http({})
    http.errors[EXPIRED_URL] = original_error
    resolver = _Resolver(error=ViralLinkError(503, "TEST_LINK_ERROR", "解析失败", retryable=True))
    _install_archive_fakes(monkeypatch, http=http, resolver=resolver)
    with pytest.raises(ViralMediaError) as caught:
        viral_collection._run_single_archive(_lease(), _storage(), VIDEO_ID)
    assert caught.value is original_error
    _assert_resolved_once(resolver)
    assert http.calls == [EXPIRED_URL]
    assert _stored_video(archive_state)[0] == EXPIRED_URL
    assert _preparation(archive_state)[:2] == ("FAILED", 1)


@pytest.mark.parametrize("cache_ready", [False, True])
def test_reclaimed_task_settles_pending_cost_without_resubmitting_resolution(
    archive_state: str, monkeypatch: pytest.MonkeyPatch, cache_ready: bool
) -> None:
    _seed_archive(archive_state)
    with psycopg.connect(archive_state, autocommit=True) as raw:
        raw.execute(
            "UPDATE viral_refresh_tasks SET checkpoint_json=%s WHERE id=%s",
            (json.dumps({"other_progress": "保留"}, ensure_ascii=False), _lease().id),
        )
    before = _stored_video(archive_state)

    class Interrupted(BaseException):
        pass

    def interrupt_during_resolution() -> None:
        progress = _checkpoint(archive_state)
        assert progress["media_refresh_requests"][VIDEO_ID] == "SENDING"
        accounting = progress["media_refresh_accounting"][VIDEO_ID]
        billing = _refresh_billing(archive_state)
        # 另一个连接在真实外呼前就能读到发送标记和两个计费记录。
        assert len(billing) == 1
        operation = billing[accounting["operation_id"]]
        assert operation["attempt_id"] == accounting["attempt_id"]
        assert accounting["task_attempt"] == _lease().attempt
        assert operation["operation_state"] == operation["attempt_state"] == "PENDING"
        assert operation["usage"] is operation["cost_fen"] is None
        assert operation["user_id"] is None and operation["module"] == "platform"
        assert operation["api_metadata"]["refresh_task_attempt"] == str(_lease().attempt)
        assert not operation["operation_completed"] and not operation["attempt_completed"]
        raise Interrupted()

    http = _Http({EXPIRED_URL: (403, b""), AUDIO_URL: (200, AUDIO)})
    resolver = _Resolver(on_resolve=interrupt_during_resolution)
    _install_archive_fakes(monkeypatch, http=http, resolver=resolver)
    storage = _storage()
    with pytest.raises(Interrupted):
        viral_collection._run_single_archive(_lease(), storage, VIDEO_ID)
    progress = _checkpoint(archive_state)
    assert progress["other_progress"] == "保留"
    assert progress["media_refresh_requests"][VIDEO_ID] == "SENDING"
    accounting = progress["media_refresh_accounting"][VIDEO_ID]
    _assert_resolved_once(resolver)

    reclaimed = _reclaim_archive_task(archive_state)
    if cache_ready:
        _stored, cache_hit = ViralMediaPreparation(storage=storage).fetch(
            platform=PLATFORM,
            video_id=VIDEO_ID,
            kind="video",
            prepare=lambda key, _check: storage.put_object(key, MP4, content_type="video/mp4"),
        )
        assert not cache_hit

    def verify_startup_compensation(_url: str) -> None:
        operation = _refresh_billing(archive_state)[accounting["operation_id"]]
        assert operation["operation_state"] == "FAILED"
        assert operation["attempt_state"] == "UNKNOWN"
        assert _checkpoint(archive_state)["media_refresh_requests"][VIDEO_ID] == "BLOCKED"

    http.on_fetch = verify_startup_compensation
    if cache_ready:
        viral_collection._run_single_archive(reclaimed, storage, VIDEO_ID)
        assert _preparation(archive_state)[:2] == ("SUCCEEDED", 2)
    else:
        with pytest.raises(ViralLinkError) as caught:
            viral_collection._run_single_archive(reclaimed, storage, VIDEO_ID)
        assert caught.value.uncertain and not caught.value.retryable
        with psycopg.connect(archive_state) as raw:
            fail_viral_refresh_task(
                BusinessConnection.postgres(raw), lease=reclaimed, cause=caught.value
            )
        with psycopg.connect(archive_state) as raw:
            row = raw.execute(
                "SELECT status,retryable,error_message_redacted FROM viral_refresh_tasks "
                "WHERE id=%s",
                (_lease().id,),
            ).fetchone()
        assert row is not None and row[:2] == ("FAILED", 0) and "待核对" in row[2]
    _assert_resolved_once(resolver)
    assert http.calls == ([EXPIRED_URL, AUDIO_URL] if cache_ready else [EXPIRED_URL, EXPIRED_URL])
    assert _stored_video(archive_state) == before
    progress = _checkpoint(archive_state)
    assert progress["other_progress"] == "保留"
    assert progress["media_refresh_requests"][VIDEO_ID] == "BLOCKED"
    assert progress["media_refresh_accounting"][VIDEO_ID] == accounting
    billing = _refresh_billing(archive_state)
    assert len(billing) == 1
    operation = billing[accounting["operation_id"]]
    assert operation["attempt_id"] == accounting["attempt_id"]
    assert operation["operation_state"] == "FAILED" and operation["attempt_state"] == "UNKNOWN"
    assert operation["usage"] is operation["cost_fen"] is None
    assert operation["actual_units"] == operation["charged_credits"] == 0
    assert operation["operation_completed"] and operation["attempt_completed"]


def test_reconcile_interrupted_resolution_preserves_live_lease_and_settles_orphaned_cost(
    archive_state: str,
) -> None:
    _seed_archive(archive_state)
    viral_collection._mark_media_refresh(_lease(), VIDEO_ID, "SENDING")
    accounting = _checkpoint(archive_state)["media_refresh_accounting"][VIDEO_ID]
    with psycopg.connect(archive_state) as raw:
        # 仅让夹具时间跨过补偿窗口，正常路径的不可变账单触发器仍保留。
        raw.execute("SET LOCAL session_replication_role = replica")
        raw.execute(
            "UPDATE billing_operations SET created_at=now()-interval '1 hour' WHERE id=%s",
            (accounting["operation_id"],),
        )
        raw.execute("SET LOCAL session_replication_role = DEFAULT")
    pending = _refresh_billing(archive_state)
    assert len(pending) == 1
    operation = pending[accounting["operation_id"]]
    assert operation["attempt_id"] == accounting["attempt_id"]
    assert operation["operation_state"] == operation["attempt_state"] == "PENDING"
    with psycopg.connect(archive_state) as raw:
        assert reconcile_operations(BusinessConnection.postgres(raw)) == 0
    assert _refresh_billing(archive_state) == pending

    with psycopg.connect(archive_state) as raw:
        raw.execute(
            "UPDATE viral_refresh_tasks SET status='PENDING',locked_by=NULL,locked_until=NULL,"
            "checkpoint_json='{}' WHERE id=%s",
            (_lease().id,),
        )
    assert _checkpoint(archive_state) == {}
    with psycopg.connect(archive_state) as raw:
        assert reconcile_operations(BusinessConnection.postgres(raw)) == 1
    billing = _refresh_billing(archive_state)
    assert len(billing) == 1
    operation = billing[accounting["operation_id"]]
    assert operation["attempt_id"] == accounting["attempt_id"]
    assert operation["operation_state"] == "FAILED" and operation["attempt_state"] == "UNKNOWN"
    assert operation["usage"] is operation["cost_fen"] is None
    assert operation["actual_units"] == operation["charged_credits"] == 0
    assert operation["operation_completed"] and operation["attempt_completed"]
    with psycopg.connect(archive_state) as raw:
        assert reconcile_operations(BusinessConnection.postgres(raw)) == 0
    assert _refresh_billing(archive_state) == billing


def test_known_retryable_resolution_failure_allows_later_retry(
    archive_state: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_archive(archive_state)
    with psycopg.connect(archive_state, autocommit=True) as raw:
        raw.execute(
            "UPDATE viral_refresh_tasks SET collection_config_json=%s WHERE id=%s",
            (json.dumps({"kind": "single_archive", "video_id": VIDEO_ID}), _lease().id),
        )
    http = _Http({EXPIRED_URL: (403, b""), FRESH_URL: (200, MP4), AUDIO_URL: (200, AUDIO)})
    resolver = _Resolver(error=ViralLinkError(503, "TEST_LINK_ERROR", "解析失败", retryable=True))
    _install_archive_fakes(monkeypatch, http=http, resolver=resolver)
    storage = _storage()
    with pytest.raises(ViralMediaError, match="媒体地址返回异常状态"):
        viral_collection._run_single_archive(_lease(), storage, VIDEO_ID)
    _assert_resolved_once(resolver)
    assert _checkpoint(archive_state)["media_refresh_requests"][VIDEO_ID] == "DONE"
    with psycopg.connect(archive_state, autocommit=True) as raw:
        raw.execute(
            "UPDATE viral_refresh_tasks SET locked_until='2020-01-01 00:00:00' WHERE id=%s",
            (_lease().id,),
        )
        raw.execute(
            "UPDATE viral_media_preparations SET updated_at='2020-01-01 00:00:00' "
            "WHERE platform=%s AND video_id=%s AND media_kind='video'",
            (PLATFORM, VIDEO_ID),
        )
    with psycopg.connect(archive_state) as raw:
        reclaimed = acquire_viral_refresh_task(
            BusinessConnection.postgres(raw), worker_id="reclaimed-worker"
        )
    assert reclaimed is not None and reclaimed.attempt == 2
    resolver.error = None
    viral_collection._run_single_archive(reclaimed, storage, VIDEO_ID)
    assert len(resolver.calls) == 2
    assert _checkpoint(archive_state)["media_refresh_requests"][VIDEO_ID] == "DONE"
    assert _stored_video(archive_state)[0] == FRESH_URL


def test_manual_archive_enqueue_clears_old_resolution_checkpoint(archive_state: str) -> None:
    from app.admin_runtime_routes import _enqueue_archive_batch

    _seed_archive(archive_state)
    with psycopg.connect(archive_state) as raw:
        raw.execute(
            "UPDATE viral_refresh_tasks SET status='FAILED',locked_by=NULL,locked_until=NULL,"
            "checkpoint_json=%s WHERE id=%s",
            (json.dumps({"media_refresh_requests": {VIDEO_ID: "BLOCKED"}}), _lease().id),
        )
        task_id = _enqueue_archive_batch(
            raw,
            PLATFORM,
            [VIDEO_ID],
            feature_after=None,
            actor_user_id="admin_u",
            reason="人工重新转存",
            request_id="archive-manual-retry-test",
            audit_action="viral_video.archive",
        )
    assert task_id == _lease().id
    with psycopg.connect(archive_state) as raw:
        row = raw.execute(
            "SELECT status,checkpoint_json FROM viral_refresh_tasks WHERE id=%s", (task_id,)
        ).fetchone()
    assert row is not None and row[0] == "PENDING" and json.loads(row[1]) == {}


@pytest.mark.parametrize("kind", ["single_archive", "archive_batch"])
@pytest.mark.parametrize(("retryable", "uncertain"), [(True, True), (False, False)])
def test_uncertain_or_nonretryable_link_failure_does_not_retry_task(
    archive_state: str,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    retryable: bool,
    uncertain: bool,
) -> None:
    _seed_archive(archive_state)
    config: dict[str, Any] = {"kind": kind}
    second_video_id = "7294812345678901235"
    if kind == "archive_batch":
        config["video_ids"] = [VIDEO_ID, second_video_id]
        with psycopg.connect(archive_state, autocommit=True) as raw:
            _seed_video(raw, video_id=second_video_id, play_url=FRESH_URL)
    else:
        config["video_id"] = VIDEO_ID
    with psycopg.connect(archive_state, autocommit=True) as raw:
        raw.execute(
            "UPDATE viral_refresh_tasks SET collection_config_json=%s WHERE id=%s",
            (json.dumps(config), _lease().id),
        )
    before = _stored_video(archive_state)
    error = ViralLinkError(
        504 if uncertain else 422,
        "TEST_LINK_ERROR",
        "解析结果未知" if uncertain else "解析不可重试",
        retryable=retryable,
        uncertain=uncertain,
    )
    http = _Http({EXPIRED_URL: (403, b""), FRESH_URL: (200, MP4), AUDIO_URL: (200, AUDIO)})
    resolver = _Resolver(error=error)
    _install_archive_fakes(monkeypatch, http=http, resolver=resolver)
    storage = _storage()

    with pytest.raises(ViralLinkError) as caught:
        viral_collection.run_viral_collection(_lease(), storage)
    if kind == "single_archive":
        assert caught.value is error
    assert caught.value.uncertain or not caught.value.retryable
    _assert_resolved_once(resolver)
    expected_calls = (
        [EXPIRED_URL, FRESH_URL, AUDIO_URL] if kind == "archive_batch" else [EXPIRED_URL]
    )
    assert http.calls == expected_calls
    assert _stored_video(archive_state) == before
    assert _preparation(archive_state)[:2] == ("FAILED", 1)
    progress = _checkpoint(archive_state)
    assert progress["media_refresh_requests"][VIDEO_ID] in ("SENDING", "BLOCKED")
    if kind == "archive_batch":
        assert _preparation(archive_state, video_id=second_video_id)[:2] == ("SUCCEEDED", 1)
        cached = ViralMediaPreparation(storage=storage).cached(
            platform=PLATFORM, video_id=second_video_id, kind="video"
        )
        assert cached is not None and storage.get_object(cached.key) == MP4
        assert progress["failed_video_ids"] == [VIDEO_ID]

    with psycopg.connect(archive_state) as raw:
        fail_viral_refresh_task(
            BusinessConnection.postgres(raw), lease=_lease(), cause=caught.value
        )
    with psycopg.connect(archive_state, autocommit=True) as raw:
        row = raw.execute(
            "SELECT status, retryable, retry_count FROM viral_refresh_tasks WHERE id=%s",
            (_lease().id,),
        ).fetchone()
        assert row == ("FAILED", 0, 0)
        raw.execute(
            "UPDATE viral_refresh_tasks SET updated_at='2020-01-01 00:00:00' WHERE id=%s",
            (_lease().id,),
        )
        raw.execute(
            "UPDATE viral_runtime_controls SET collection_enabled=1, monthly_budget_fen=NULL, "
            "collection_time=NULL, next_collection_at='2099-01-01 00:00:00', keywords_json=%s "
            "WHERE id=1",
            (json.dumps([{"platform": "douyin", "category": "测试", "keyword": "测试"}]),),
        )
    with psycopg.connect(archive_state) as raw:
        assert not viral_collection.enqueue_due_viral_collections(BusinessConnection.postgres(raw))
    with psycopg.connect(archive_state) as raw:
        row = raw.execute(
            "SELECT status, retryable, retry_count FROM viral_refresh_tasks WHERE id=%s",
            (_lease().id,),
        ).fetchone()
    assert row == ("FAILED", 0, 0)
    _assert_resolved_once(resolver)


def test_mixed_batch_preserves_uncertain_message_and_exact_failed_video_ids(
    archive_state: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_archive(archive_state)
    uncertain_id = "7294812345678901235"
    succeeded_id = "7294812345678901236"
    nonretryable_id = "7294812345678901237"
    video_ids = [VIDEO_ID, uncertain_id, succeeded_id, nonretryable_id]
    failed_ids = [VIDEO_ID, uncertain_id, nonretryable_id]
    errors = {
        VIDEO_ID: ViralLinkError(422, "TEST_INVALID_LINK", "解析不可重试", retryable=False),
        uncertain_id: ViralLinkError(
            504, "TEST_LINK_UNCERTAIN", "解析结果未知", retryable=True, uncertain=True
        ),
        nonretryable_id: ViralLinkError(422, "TEST_INVALID_LINK", "解析不可重试", retryable=False),
    }
    with psycopg.connect(archive_state) as raw:
        for video_id in video_ids[1:]:
            _seed_video(
                raw,
                video_id=video_id,
                play_url=FRESH_URL if video_id == succeeded_id else EXPIRED_URL,
            )
        raw.execute(
            "UPDATE viral_refresh_tasks SET collection_config_json=%s WHERE id=%s",
            (json.dumps({"kind": "archive_batch", "video_ids": video_ids}), _lease().id),
        )
    before = {video_id: _stored_video(archive_state, video_id=video_id) for video_id in video_ids}
    http = _Http({EXPIRED_URL: (403, b""), FRESH_URL: (200, MP4), AUDIO_URL: (200, AUDIO)})
    resolver = _Resolver()

    def fail_resolution_for_current_video() -> None:
        url, purpose = resolver.calls[-1]
        assert purpose == "replica"
        matching = [video_id for video_id in failed_ids if video_id in url]
        assert len(matching) == 1
        resolver.error = errors[matching[0]]

    resolver.on_resolve = fail_resolution_for_current_video
    _install_archive_fakes(monkeypatch, http=http, resolver=resolver)
    storage = _storage()
    with pytest.raises(ViralLinkError) as caught:
        viral_collection.run_viral_collection(_lease(), storage)
    assert caught.value is errors[uncertain_id] and caught.value.uncertain
    assert len(resolver.calls) == 3
    assert all(
        video_id in call[0] for video_id, call in zip(failed_ids, resolver.calls, strict=True)
    )
    assert http.calls == [EXPIRED_URL, EXPIRED_URL, FRESH_URL, AUDIO_URL, EXPIRED_URL]
    progress = _checkpoint(archive_state)
    assert progress["failed_video_ids"] == failed_ids
    assert progress["media_refresh_requests"] == dict.fromkeys(failed_ids, "BLOCKED")
    assert set(progress["media_refresh_accounting"]) == set(failed_ids)
    for video_id in video_ids:
        assert _stored_video(archive_state, video_id=video_id) == before[video_id]
        assert _preparation(archive_state, video_id=video_id)[:2] == (
            "SUCCEEDED" if video_id == succeeded_id else "FAILED",
            1,
        )
    cached = ViralMediaPreparation(storage=storage).cached(
        platform=PLATFORM, video_id=succeeded_id, kind="video"
    )
    assert cached is not None and storage.get_object(cached.key) == MP4
    billing = _refresh_billing(archive_state)
    assert len(billing) == 3
    for video_id in failed_ids:
        accounting = progress["media_refresh_accounting"][video_id]
        operation = billing[accounting["operation_id"]]
        assert operation["attempt_id"] == accounting["attempt_id"]
        assert operation["api_metadata"]["video_id"] == video_id
        assert operation["operation_state"] == "FAILED" and operation["attempt_state"] == "UNKNOWN"
        assert operation["usage"] is operation["cost_fen"] is None
        assert operation["actual_units"] == operation["charged_credits"] == 0
        assert operation["operation_completed"] and operation["attempt_completed"]
    with psycopg.connect(archive_state) as raw:
        fail_viral_refresh_task(
            BusinessConnection.postgres(raw), lease=_lease(), cause=caught.value
        )
    with psycopg.connect(archive_state) as raw:
        row = raw.execute(
            "SELECT status,retryable,retry_count,error_message_redacted "
            "FROM viral_refresh_tasks WHERE id=%s",
            (_lease().id,),
        ).fetchone()
    assert row is not None and row[:3] == ("FAILED", 0, 0) and "待核对" in row[3]
    assert _checkpoint(archive_state)["failed_video_ids"] == failed_ids
    assert _refresh_billing(archive_state) == billing


def test_single_archive_dns_failure_does_not_resolve(
    archive_state: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_archive(archive_state)
    original_error = ViralMediaDNSUnavailable("媒体域名无法解析")
    http = _Http({})
    http.errors[EXPIRED_URL] = original_error
    resolver = _Resolver()
    _install_archive_fakes(monkeypatch, http=http, resolver=resolver)
    with pytest.raises(ViralMediaDNSUnavailable) as caught:
        viral_collection._run_single_archive(_lease(), _storage(), VIDEO_ID)
    assert caught.value is original_error
    assert resolver.calls == []
    assert http.calls == [EXPIRED_URL]
    assert _stored_video(archive_state)[0] == EXPIRED_URL
    assert _preparation(archive_state)[:2] == ("FAILED", 1)


def test_single_archive_busy_lease_does_not_resolve(
    archive_state: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_archive(archive_state)
    with psycopg.connect(archive_state, autocommit=True) as raw:
        raw.execute(
            "INSERT INTO viral_media_preparations "
            "(id, platform, video_id, media_kind, status, attempt, locked_by, locked_until) "
            "VALUES ('other-media-lease', %s, %s, 'video', 'RUNNING', 1, 'other-worker', "
            "'2099-01-01 00:00:00')",
            (PLATFORM, VIDEO_ID),
        )
    http = _Http({EXPIRED_URL: (200, MP4)})
    resolver = _Resolver()
    _install_archive_fakes(monkeypatch, http=http, resolver=resolver)
    original_init = ViralMediaPreparation.__init__

    def no_wait(self: ViralMediaPreparation, *, storage: Any, **_kwargs: Any) -> None:
        original_init(self, storage=storage, wait_seconds=0, poll_seconds=0.01)

    monkeypatch.setattr(ViralMediaPreparation, "__init__", no_wait)
    with pytest.raises(ViralMediaBusy):
        viral_collection._run_single_archive(_lease(), _storage(), VIDEO_ID)
    assert resolver.calls == []
    assert http.calls == []
    assert _preparation(archive_state)[:3] == ("RUNNING", 1, "other-worker")
    assert _stored_video(archive_state)[0] == EXPIRED_URL


def test_wechat_share_archive_failure_does_not_use_douyin_resolver(
    archive_state: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    platform = "wechat_channels"
    native = {"share_url": "https://weixin.qq.com/sph/example"}
    _seed_archive(archive_state, platform=platform, native=native)
    http = _Http({EXPIRED_URL: (403, b"")})
    resolver = _Resolver()
    _install_archive_fakes(monkeypatch, http=http, resolver=resolver)
    with pytest.raises(ViralMediaError, match="媒体地址返回异常状态"):
        viral_collection._run_single_archive(_lease(platform), _storage(), VIDEO_ID)
    assert resolver.calls == []
    assert http.calls == [EXPIRED_URL]
    assert _stored_video(archive_state, platform)[0] == EXPIRED_URL


def test_wechat_non_share_still_uses_detail_client(
    archive_state: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    platform = "wechat_channels"
    _seed_archive(archive_state, platform=platform, native={"export_id": "export-123"})
    detail = WechatVideoDetail(
        object_id=VIDEO_ID,
        object_nonce_id=None,
        title="视频号标题",
        description=None,
        nickname="视频号作者",
        username=None,
        create_time=None,
        like_count=1,
        fav_count=None,
        forward_count=None,
        comment_count=None,
        city=None,
        full_url=FRESH_URL,
        decode_key="1",
        cover_url=None,
        duration_ms=None,
        width=None,
        height=None,
    )
    calls: list[tuple[str, str | None]] = []

    class Source:
        def wechat_video_detail(
            self, *, export_id: str, object_nonce_id: str | None
        ) -> WechatVideoDetail:
            calls.append((export_id, object_nonce_id))
            return detail

    http = _Http({FRESH_URL: (200, MP4)})
    resolver = _Resolver()
    _install_archive_fakes(monkeypatch, http=http, resolver=resolver, source_client=Source())
    storage = _storage()
    viral_collection._run_single_archive(_lease(platform), storage, VIDEO_ID)
    assert calls == [("export-123", None)]
    assert http.calls == [FRESH_URL]
    assert http.headers[0]["Referer"] == "https://channels.weixin.qq.com/"
    assert resolver.calls == []
    assert _preparation(archive_state, platform)[:2] == ("SUCCEEDED", 1)
    cached = ViralMediaPreparation(storage=storage).cached(
        platform=platform, video_id=VIDEO_ID, kind="video"
    )
    assert cached is not None and storage.get_object(cached.key) == MP4


def test_single_archive_waits_for_failed_cooldown_and_passes_seven_seconds(
    archive_state: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_archive(archive_state)
    with psycopg.connect(archive_state, autocommit=True) as raw:
        raw.execute(
            "INSERT INTO viral_media_preparations "
            "(id, platform, video_id, media_kind, status, attempt, updated_at) "
            "VALUES ('failed-media-lease', %s, %s, 'video', 'FAILED', 1, "
            "to_char((clock_timestamp() AT TIME ZONE 'UTC') + interval '5 minutes', "
            "'YYYY-MM-DD HH24:MI:SS'))",
            (PLATFORM, VIDEO_ID),
        )
    http = _Http({EXPIRED_URL: (200, MP4), AUDIO_URL: (200, AUDIO)})
    resolver = _Resolver()
    _install_archive_fakes(monkeypatch, http=http, resolver=resolver)
    waits: list[float] = []
    original_fetch = ViralMediaPreparation.fetch
    failed_waits: list[float] = []

    def spy_fetch(self: ViralMediaPreparation, **kwargs: Any) -> Any:
        failed_waits.append(kwargs.get("failed_wait_seconds", 0))
        return original_fetch(self, **kwargs)

    def finish_cooldown(seconds: float) -> None:
        waits.append(seconds)
        with psycopg.connect(archive_state, autocommit=True) as raw:
            raw.execute(
                "UPDATE viral_media_preparations SET updated_at=to_char("
                "(clock_timestamp() AT TIME ZONE 'UTC') - interval '10 seconds', "
                "'YYYY-MM-DD HH24:MI:SS') WHERE id='failed-media-lease'"
            )

    monkeypatch.setattr(ViralMediaPreparation, "fetch", spy_fetch)
    monkeypatch.setattr(
        viral_media_preparation,
        "time",
        SimpleNamespace(monotonic=time.monotonic, sleep=finish_cooldown),
    )
    viral_collection._run_single_archive(_lease(), _storage(), VIDEO_ID)
    assert failed_waits and all(value == 7 for value in failed_waits)
    assert len(waits) == 1 and 0 < waits[0] <= 0.25
    assert _preparation(archive_state)[:2] == ("SUCCEEDED", 2)
    assert http.calls == [EXPIRED_URL, AUDIO_URL]
    assert resolver.calls == []
