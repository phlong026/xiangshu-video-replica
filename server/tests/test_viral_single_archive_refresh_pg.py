"""爆款单条转存对过期播放地址的自动重取（VIRAL-TRANSFER-REFRESH）.

生产 2026-11-15 实锤：库里 ``play_url`` 是采集/导入当时的限时 CDN 签名直链，
延后补转存必然 403（同条目 audio_url 仍 200），任务反复失败（attempt=8）。
不变量：
- 抖音视频下载遇普通 ``ViralMediaError`` 时重外呼取新直链、回库并只重试一次；
- 重取无果（无精确命中/无新地址/数据源异常）保持原失败，不入库无关视频；
- Busy（他人在准备）与 DNS 不可达不消耗这次外呼；非抖音平台不走重取；
- 重试成功后库里播放地址已更新，后续步骤消费新条目。

单元部分直接测 ``_refresh_douyin_play_url`` 分支；矩阵部分驱动
``_run_single_archive``（复用 T34 夹具链：模块级建库 → alembic head →
每用例 TRUNCATE 隔离），管线与数据源全部桩化，不发真实外呼。
"""

# Imported pytest fixtures are injected by parameter name.
# ruff: noqa: F811, F401

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import psycopg
import pytest
from pg_test_kit import require_pg_or_explicit_skip  # noqa: F401
from test_admin_session_routes import route_state, sessions_dsn  # noqa: F401

from app import viral_collection
from app.db_pg import DATABASE_URL_ENV, close_pg_pool
from app.storage import FakeStorageAdapter
from app.viral_media import ViralMediaDNSUnavailable, ViralMediaError
from app.viral_media_preparation import ViralMediaBusy
from app.viral_refresh import ViralRefreshLease
from app.viral_tikhub import ViralSourceError, ViralVideo

PLATFORM = "douyin"
VIDEO_ID = "archive-refresh-video"
EXPIRED_URL = "https://v9.douyinvod.com/expired-signature/video/tos/cn/old/?a=1"
FRESH_URL = "https://v9.douyinvod.com/fresh-signature/video/tos/cn/new/?a=2"


def _video(
    video_id: str = VIDEO_ID,
    platform: str = PLATFORM,
    play_url: str | None = EXPIRED_URL,
) -> ViralVideo:
    return ViralVideo(
        platform=platform,
        video_id=video_id,
        category="",
        title="标题",
        author="作者",
        author_avatar=None,
        verified=False,
        cover_url=None,
        duration_ms=11_000,
        likes=1,
        comments=None,
        shares=None,
        collects=None,
        published_at=None,
        published_display=None,
        like_display=None,
        tags=[],
        play_url=play_url,
        audio_url=None,
        native={},
    )


class _FakeClient:
    """douyin_refresh 桩：按预设返回候选或抛错，记录调用."""

    def __init__(
        self,
        candidates: list[ViralVideo] | None = None,
        *,
        error: ViralSourceError | None = None,
    ) -> None:
        self.candidates = candidates or []
        self.error = error
        self.calls: list[str] = []

    def douyin_refresh(self, *, platform: str, video_id: str) -> list[ViralVideo]:
        self.calls.append(f"{platform}:{video_id}")
        if self.error is not None:
            raise self.error
        return self.candidates


class _FakePipeline:
    """fetch 桩：video 形态遇 ``fail_on_play_url`` 抛 ``ViralMediaError``.

    ``fetch_error`` 优先：设置后 video 形态一律抛它（用于 Busy/DNS/非抖音），
    此时 ``fail_on_play_url`` 不参与判断。
    """

    def __init__(
        self,
        *,
        client: Any = None,
        storage: Any = None,
        shared: bool = False,
        cancellation_check: Any = None,
        wechat_link_client: Any = None,
        fail_on_play_url: str | None = EXPIRED_URL,
        fetch_error: ViralSourceError | None = None,
    ) -> None:
        del client, storage, shared, cancellation_check, wechat_link_client
        self.calls: list[tuple[str, str, str | None]] = []
        self.fail_on_play_url = fail_on_play_url
        self.fetch_error = fetch_error
        self.detail: Any = None

    def fetch(self, video: ViralVideo, *, prefer: str | None = None) -> Any:
        kind = prefer or "audio"
        self.calls.append((video.video_id, kind, video.play_url))
        if kind != "video":
            return None
        if self.fetch_error is not None:
            raise self.fetch_error
        if video.play_url == self.fail_on_play_url:
            raise ViralMediaError("媒体地址返回异常状态")
        return None


def _seed_video(raw: psycopg.Connection, *, platform: str = PLATFORM) -> None:
    raw.execute(
        "INSERT INTO viral_videos (platform, video_id, title, cover_url, play_url, "
        "published_at) VALUES (%s, %s, '转存重取测试', '', %s, 1788700000)",
        (platform, VIDEO_ID, EXPIRED_URL),
    )


def _seed_running_task(raw: psycopg.Connection, *, platform: str = PLATFORM) -> None:
    raw.execute(
        "INSERT INTO viral_refresh_tasks (id, platform, sort, status, attempt, "
        "locked_by, locked_until) VALUES ('task-archive-refresh', %s, 'latest', "
        "'RUNNING', 1, 'archive-refresh-worker', '2099-01-01 00:00:00')",
        (platform,),
    )


def _lease(platform: str = PLATFORM) -> ViralRefreshLease:
    return ViralRefreshLease(
        id="task-archive-refresh",
        worker_id="archive-refresh-worker",
        platform=platform,
        sort="latest",
        attempt=1,
    )


def _install_fakes(
    monkeypatch: pytest.MonkeyPatch,
    *,
    pipeline: _FakePipeline,
    client: _FakeClient,
) -> None:
    monkeypatch.setattr(viral_collection, "ViralMediaPipeline", lambda **_kwargs: pipeline)
    monkeypatch.setattr(viral_collection, "viral_source_client_from_settings", lambda _conn: client)


def _video_calls(pipeline: _FakePipeline) -> list[str | None]:
    return [call[2] for call in pipeline.calls if call[1] == "video"]


def _stored_play_url(dsn: str) -> str | None:
    with psycopg.connect(dsn) as raw:
        row = raw.execute(
            "SELECT play_url FROM viral_videos WHERE platform=%s AND video_id=%s",
            (PLATFORM, VIDEO_ID),
        ).fetchone()
    return None if row is None else row[0]


# -- 单元：_refresh_douyin_play_url 分支 --------------------------------------


def test_refresh_picks_exact_video_with_new_play_url_and_upserts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    upserted: list[ViralVideo] = []
    monkeypatch.setattr(
        viral_collection,
        "upsert_viral_videos",
        lambda _conn, videos: upserted.extend(videos) or set(),
    )
    client = _FakeClient([_video(video_id="other"), _video(play_url=FRESH_URL)])
    refreshed = viral_collection._refresh_douyin_play_url(
        object(),
        client,
        _video(),  # type: ignore[arg-type]
    )
    assert refreshed is not None and refreshed.play_url == FRESH_URL
    assert client.calls == [f"{PLATFORM}:{VIDEO_ID}"]
    assert [item.play_url for item in upserted] == [FRESH_URL]


def test_refresh_returns_none_when_no_exact_match(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(viral_collection, "upsert_viral_videos", lambda _conn, videos: set())
    client = _FakeClient([_video(video_id="unrelated", play_url=FRESH_URL)])
    assert (
        viral_collection._refresh_douyin_play_url(
            object(),
            client,
            _video(),  # type: ignore[arg-type]
        )
        is None
    )


def test_refresh_returns_none_when_data_source_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(viral_collection, "upsert_viral_videos", lambda _conn, videos: set())
    client = _FakeClient(error=ViralSourceError("上游故障"))
    assert (
        viral_collection._refresh_douyin_play_url(
            object(),
            client,
            _video(),  # type: ignore[arg-type]
        )
        is None
    )


def test_refresh_skipped_for_non_douyin_platform() -> None:
    client = _FakeClient(candidates=[_video(play_url=FRESH_URL)])
    refreshed = viral_collection._refresh_douyin_play_url(
        object(),
        client,
        _video(platform="wechat_channels"),  # type: ignore[arg-type]
    )
    assert refreshed is None
    assert client.calls == []


# -- 矩阵：_run_single_archive 全链路（PG） ------------------------------------


@pytest.fixture()
def archive_state(route_state: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """沿用 T34 库：环境变量指向夹具库并清池，供 ``_connection()`` 直连."""
    close_pg_pool()
    monkeypatch.setenv(DATABASE_URL_ENV, route_state)
    yield route_state
    close_pg_pool()


def test_single_archive_refreshes_expired_play_url_and_retries_once(
    archive_state: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    require_pg_or_explicit_skip(archive_state)
    pipeline = _FakePipeline()
    client = _FakeClient([_video(play_url=FRESH_URL)])
    _install_fakes(monkeypatch, pipeline=pipeline, client=client)
    with psycopg.connect(archive_state, autocommit=True) as raw:
        _seed_video(raw)
        _seed_running_task(raw)
    viral_collection._run_single_archive(
        _lease(), FakeStorageAdapter(provider="fake", bucket="archive-refresh"), VIDEO_ID
    )
    assert _video_calls(pipeline) == [EXPIRED_URL, FRESH_URL]
    assert client.calls == [f"{PLATFORM}:{VIDEO_ID}"]
    assert _stored_play_url(archive_state) == FRESH_URL


def test_single_archive_keeps_failure_when_refresh_has_no_match(
    archive_state: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    require_pg_or_explicit_skip(archive_state)
    pipeline = _FakePipeline()
    client = _FakeClient([_video(video_id="unrelated", play_url=FRESH_URL)])
    _install_fakes(monkeypatch, pipeline=pipeline, client=client)
    with psycopg.connect(archive_state, autocommit=True) as raw:
        _seed_video(raw)
        _seed_running_task(raw)
    with pytest.raises(ViralMediaError):
        viral_collection._run_single_archive(
            _lease(), FakeStorageAdapter(provider="fake", bucket="archive-refresh"), VIDEO_ID
        )
    assert _video_calls(pipeline) == [EXPIRED_URL]
    assert _stored_play_url(archive_state) == EXPIRED_URL


def test_single_archive_busy_does_not_consume_refresh(
    archive_state: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    require_pg_or_explicit_skip(archive_state)
    pipeline = _FakePipeline(fetch_error=ViralMediaBusy("该视频素材正在准备中"))
    client = _FakeClient([_video(play_url=FRESH_URL)])
    _install_fakes(monkeypatch, pipeline=pipeline, client=client)
    with psycopg.connect(archive_state, autocommit=True) as raw:
        _seed_video(raw)
        _seed_running_task(raw)
    with pytest.raises(ViralMediaBusy):
        viral_collection._run_single_archive(
            _lease(), FakeStorageAdapter(provider="fake", bucket="archive-refresh"), VIDEO_ID
        )
    assert client.calls == []


def test_single_archive_dns_unavailable_does_not_consume_refresh(
    archive_state: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    require_pg_or_explicit_skip(archive_state)
    pipeline = _FakePipeline(fetch_error=ViralMediaDNSUnavailable("媒体域名无法解析"))
    client = _FakeClient([_video(play_url=FRESH_URL)])
    _install_fakes(monkeypatch, pipeline=pipeline, client=client)
    with psycopg.connect(archive_state, autocommit=True) as raw:
        _seed_video(raw)
        _seed_running_task(raw)
    with pytest.raises(ViralMediaDNSUnavailable):
        viral_collection._run_single_archive(
            _lease(), FakeStorageAdapter(provider="fake", bucket="archive-refresh"), VIDEO_ID
        )
    assert client.calls == []


def test_single_archive_non_douyin_keeps_failure_without_refresh(
    archive_state: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    require_pg_or_explicit_skip(archive_state)
    pipeline = _FakePipeline(fail_on_play_url=None, fetch_error=ViralMediaError("素材不可得"))
    client = _FakeClient([_video(play_url=FRESH_URL)])
    _install_fakes(monkeypatch, pipeline=pipeline, client=client)
    with psycopg.connect(archive_state, autocommit=True) as raw:
        _seed_video(raw, platform="wechat_channels")
        _seed_running_task(raw, platform="wechat_channels")
    with pytest.raises(ViralMediaError):
        viral_collection._run_single_archive(
            _lease(platform="wechat_channels"),
            FakeStorageAdapter(provider="fake", bucket="archive-refresh"),
            VIDEO_ID,
        )
    assert client.calls == []
