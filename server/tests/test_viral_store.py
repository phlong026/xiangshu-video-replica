"""爆款视频库的去重、统计合并、持久化与刷新状态测试."""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from app.db import connect_database, initialize_database
from app.db_portable import BusinessConnection
from app.viral_store import (
    VIRAL_FETCH_TTL,
    InvalidViralCursorError,
    add_viral_favorite,
    fetch_state_is_fresh,
    get_viral_video,
    is_viral_favorite,
    list_favorite_viral_video_page,
    list_favorite_viral_videos,
    list_viral_video_page,
    list_viral_videos,
    mark_fetch_state,
    remove_viral_favorite,
    update_viral_statistics,
    upsert_viral_videos,
    viral_fetched_at,
)
from app.viral_tikhub import (
    ViralHttpTransport,
    ViralSourceClient,
    ViralVideo,
    WechatVideoDetail,
    _reset_wechat_detail_cache,
)


def _open(path: Path) -> BusinessConnection:
    return BusinessConnection.sqlite(connect_database(path))


def _video(
    *,
    platform: str = "wechat_channels",
    video_id: str = "video-1",
    title: str = "农村庭院设计",
    published_at: int | None = None,
) -> ViralVideo:
    return ViralVideo(
        platform=platform,
        video_id=video_id,
        category="庭院案例",
        title=title,
        author="作者",
        author_avatar="https://cdn.test/avatar.jpg",
        verified=platform == "douyin",
        cover_url="https://cdn.test/cover.jpg",
        duration_ms=12_000,
        likes=100,
        comments=None,
        shares=None,
        collects=None,
        published_at=published_at or int(datetime.now(UTC).timestamp()),
        published_display="1小时前",
        like_display="100",
        tags=["农村庭院", "自建房"],
        play_url=None,
        audio_url=None,
        native={"export_id": "export/test"},
    )


class _DetailTransport(ViralHttpTransport):
    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data

    def request(self, method: str, url: str, *, headers, body=None) -> bytes:
        return json.dumps({"code": 200, "data": self.data}).encode()


def _detail(**counts: int | None) -> WechatVideoDetail:
    _reset_wechat_detail_cache()
    data: dict[str, Any] = {"id": "detail-1", "title": "农村庭院设计"}
    data.update({key: value for key, value in counts.items() if value is not None})
    transport = _DetailTransport(data)
    return ViralSourceClient(
        api_key="test-key",
        transport=transport,
        detail_transport=transport,
    ).wechat_video_detail(export_id="export/test")


def test_upsert_deduplicates_by_platform_and_video_id_and_preserves_cover_key(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "viral.db"
    initialize_database(db_path).close()
    conn = _open(db_path)
    try:
        original = replace(_video(platform="douyin"), cover_key="viral/douyin/video-1/cover")
        refreshed = replace(original, title="刷新后的标题", cover_key=None)
        same_id_other_platform = _video(platform="wechat_channels")

        upsert_viral_videos(conn, [original])
        upsert_viral_videos(conn, [refreshed, same_id_other_platform])

        assert conn.execute("SELECT COUNT(*) AS count FROM viral_videos").fetchone()["count"] == 2
        stored = get_viral_video(conn, platform="douyin", video_id="video-1")
        assert stored is not None
        assert stored.title == "刷新后的标题"
        assert stored.cover_key == "viral/douyin/video-1/cover"
        assert get_viral_video(conn, platform="wechat_channels", video_id="video-1") is not None
    finally:
        conn.close()


def test_wechat_search_null_statistics_do_not_overwrite_enriched_values(tmp_path: Path) -> None:
    db_path = tmp_path / "viral.db"
    initialize_database(db_path).close()
    conn = _open(db_path)
    try:
        enriched = replace(_video(), comments=21, shares=8, collects=13)
        search_refresh = replace(_video(), likes=120, comments=None, shares=None, collects=None)

        upsert_viral_videos(conn, [enriched])
        upsert_viral_videos(conn, [search_refresh])

        stored = get_viral_video(conn, platform="wechat_channels", video_id="video-1")
        assert stored is not None
        assert (stored.likes, stored.comments, stored.shares, stored.collects) == (120, 21, 8, 13)
    finally:
        conn.close()


def test_detail_statistics_preserve_real_zero_and_leave_missing_values_unchanged(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "viral.db"
    initialize_database(db_path).close()
    conn = _open(db_path)
    try:
        upsert_viral_videos(
            conn,
            [replace(_video(), likes=10, comments=20, shares=30, collects=40)],
        )

        update_viral_statistics(
            conn,
            platform="wechat_channels",
            video_id="video-1",
            detail=_detail(like_count=0, forward_count=0),
        )

        stored = get_viral_video(conn, platform="wechat_channels", video_id="video-1")
        assert stored is not None
        assert (stored.likes, stored.comments, stored.shares, stored.collects) == (0, 20, 0, 40)
        assert stored.like_display is None
    finally:
        conn.close()


def test_videos_remain_available_after_connection_reopens(tmp_path: Path) -> None:
    db_path = tmp_path / "viral.db"
    initialize_database(db_path).close()
    first = _open(db_path)
    upsert_viral_videos(first, [_video(video_id="persistent")])
    first.close()

    reopened = _open(db_path)
    try:
        stored = get_viral_video(reopened, platform="wechat_channels", video_id="persistent")
        assert stored is not None
        assert stored.title == "农村庭院设计"
    finally:
        reopened.close()


def test_list_hides_stale_and_minecraft_rows_without_deleting_them(tmp_path: Path) -> None:
    db_path = tmp_path / "viral.db"
    initialize_database(db_path).close()
    conn = _open(db_path)
    try:
        now = datetime.now(UTC)
        recent = _video(video_id="recent", published_at=int(now.timestamp()))
        stale = _video(video_id="stale", published_at=int((now - timedelta(days=8)).timestamp()))
        polluted = _video(video_id="game", title="Minecraft 农村别墅教程")
        upsert_viral_videos(conn, [recent, stale, polluted])

        listed = list_viral_videos(conn, platform="wechat_channels", sort="latest")

        assert [video.video_id for video in listed] == ["recent"]
        assert get_viral_video(conn, platform="wechat_channels", video_id="stale") is not None
        assert get_viral_video(conn, platform="wechat_channels", video_id="game") is not None
    finally:
        conn.close()


def test_list_page_uses_stable_keyset_cursor_and_reports_total(tmp_path: Path) -> None:
    db_path = tmp_path / "viral.db"
    initialize_database(db_path).close()
    conn = _open(db_path)
    try:
        now = int(datetime.now(UTC).timestamp())
        videos = [
            replace(
                _video(video_id=f"video-{index:02d}", published_at=now - index),
                likes=100 - index,
            )
            for index in range(35)
        ]
        upsert_viral_videos(conn, videos)

        first = list_viral_video_page(
            conn,
            platform="wechat_channels",
            sort="hot",
            limit=12,
        )
        second = list_viral_video_page(
            conn,
            platform="wechat_channels",
            sort="hot",
            limit=12,
            cursor=first.next_cursor,
        )
        third = list_viral_video_page(
            conn,
            platform="wechat_channels",
            sort="hot",
            limit=12,
            cursor=second.next_cursor,
        )

        ids = [video.video_id for page in (first, second, third) for video in page.items]
        assert ids == [f"video-{index:02d}" for index in range(35)]
        assert first.total == second.total == third.total == 35
        assert first.has_more is True and second.has_more is True
        assert third.has_more is False and third.next_cursor is None
    finally:
        conn.close()


def test_list_cursor_expires_when_refresh_version_changes(tmp_path: Path) -> None:
    db_path = tmp_path / "viral.db"
    initialize_database(db_path).close()
    conn = _open(db_path)
    try:
        now = int(datetime.now(UTC).timestamp())
        upsert_viral_videos(
            conn,
            [
                replace(
                    _video(video_id=f"versioned-{index:02d}", published_at=now - index),
                    likes=100 - index,
                )
                for index in range(5)
            ],
        )
        first = list_viral_video_page(conn, platform="wechat_channels", sort="hot", limit=2)
        assert first.next_cursor is not None

        mark_fetch_state(conn, platform="wechat_channels", sort="hot")

        with pytest.raises(InvalidViralCursorError):
            list_viral_video_page(
                conn,
                platform="wechat_channels",
                sort="hot",
                limit=2,
                cursor=first.next_cursor,
            )
    finally:
        conn.close()


def test_favorites_are_persistent_idempotent_and_isolated_by_user(tmp_path: Path) -> None:
    db_path = tmp_path / "viral.db"
    initialize_database(db_path).close()
    conn = _open(db_path)
    try:
        conn.execute(
            """
            INSERT INTO users (id, username, display_name, role)
            VALUES ('user-a', 'user-a', 'User A', 'employee')
            """
        )
        conn.commit()
        upsert_viral_videos(conn, [_video(video_id="favorite")])

        assert add_viral_favorite(
            conn,
            user_id="user-a",
            platform="wechat_channels",
            video_id="favorite",
        )
        assert not add_viral_favorite(
            conn,
            user_id="user-a",
            platform="wechat_channels",
            video_id="favorite",
        )
        assert is_viral_favorite(
            conn,
            user_id="user-a",
            platform="wechat_channels",
            video_id="favorite",
        )
        assert not is_viral_favorite(
            conn,
            user_id="user-b",
            platform="wechat_channels",
            video_id="favorite",
        )
        assert [video.video_id for video in list_favorite_viral_videos(conn, user_id="user-a")] == [
            "favorite"
        ]
        assert list_favorite_viral_videos(conn, user_id="user-b") == []

        assert remove_viral_favorite(
            conn,
            user_id="user-a",
            platform="wechat_channels",
            video_id="favorite",
        )
        assert not remove_viral_favorite(
            conn,
            user_id="user-a",
            platform="wechat_channels",
            video_id="favorite",
        )
    finally:
        conn.close()


def test_recent_list_excludes_unknown_and_future_publish_times(tmp_path: Path) -> None:
    db_path = tmp_path / "viral.db"
    initialize_database(db_path).close()
    conn = _open(db_path)
    try:
        now = int(datetime.now(UTC).timestamp())
        upsert_viral_videos(
            conn,
            [
                replace(_video(video_id="recent"), published_at=now - 60),
                replace(_video(video_id="unknown"), published_at=None),
                replace(_video(video_id="future"), published_at=now + 86_400),
            ],
        )

        listed = list_viral_video_page(
            conn,
            platform="wechat_channels",
            sort="latest",
            limit=10,
        )

        assert [video.video_id for video in listed.items] == ["recent"]
        assert listed.total == 1
    finally:
        conn.close()


def test_favorites_support_stable_keyset_pagination(tmp_path: Path) -> None:
    db_path = tmp_path / "viral.db"
    initialize_database(db_path).close()
    conn = _open(db_path)
    try:
        conn.execute(
            """
            INSERT INTO users (id, username, display_name, role)
            VALUES ('user-a', 'user-a', 'User A', 'employee')
            """
        )
        conn.commit()
        videos = [_video(video_id=f"favorite-{index:02d}") for index in range(55)]
        upsert_viral_videos(conn, videos)
        for video in videos:
            add_viral_favorite(
                conn,
                user_id="user-a",
                platform="wechat_channels",
                video_id=video.video_id,
            )

        first = list_favorite_viral_video_page(conn, user_id="user-a", limit=50)
        second = list_favorite_viral_video_page(
            conn,
            user_id="user-a",
            limit=50,
            cursor=first.next_cursor,
        )

        ids = [video.video_id for page in (first, second) for video in page.items]
        assert len(ids) == 55
        assert len(set(ids)) == 55
        assert first.total == second.total == 55
        assert first.has_more is True
        assert second.has_more is False
        assert second.next_cursor is None
    finally:
        conn.close()


@pytest.mark.parametrize("platform", [None, "wechat_channels"])
def test_favorite_page_order_uses_covering_index_without_temp_sort(
    tmp_path: Path, platform: str | None
) -> None:
    db_path = tmp_path / "viral.db"
    initialize_database(db_path).close()
    conn = _open(db_path)
    try:
        platform_sql = "AND f.platform = ?" if platform is not None else ""
        params: tuple[object, ...] = (
            ("user-a", platform, 51) if platform is not None else ("user-a", 51)
        )
        plan = conn.execute(
            f"""
            EXPLAIN QUERY PLAN
            SELECT v.*, f.created_at AS favorite_created_at
            FROM viral_video_favorites f
            JOIN viral_videos v
              ON v.platform = f.platform AND v.video_id = f.video_id
            WHERE f.user_id = ? {platform_sql}
            ORDER BY f.created_at DESC, f.platform ASC, f.video_id ASC
            LIMIT ?
            """,
            params,
        ).fetchall()
        details = [str(row["detail"]) for row in plan]
        assert not any("TEMP B-TREE" in detail for detail in details), details
    finally:
        conn.close()


def test_search_upsert_and_statistics_update_do_not_lose_native_metadata(
    tmp_path: Path, monkeypatch
) -> None:
    from app import viral_store

    db_path = tmp_path / "viral.db"
    initialize_database(db_path).close()
    seed = _open(db_path)
    upsert_viral_videos(seed, [_video()])
    seed.close()

    metadata_read = threading.Event()
    release_upsert = threading.Event()
    original = viral_store._cached_statistics_metadata

    def pause_after_read(conn, videos):
        metadata = original(conn, videos)
        metadata_read.set()
        assert release_upsert.wait(timeout=5)
        return metadata

    monkeypatch.setattr(viral_store, "_cached_statistics_metadata", pause_after_read)

    def refresh_search() -> None:
        conn = _open(db_path)
        try:
            upsert_viral_videos(
                conn,
                [replace(_video(), native={"export_id": "export/fresh", "object_nonce_id": "n2"})],
            )
        finally:
            conn.close()

    statistics_done = threading.Event()

    def refresh_statistics() -> None:
        conn = _open(db_path)
        try:
            update_viral_statistics(
                conn,
                platform="wechat_channels",
                video_id="video-1",
                detail=_detail(comment_count=9),
            )
            statistics_done.set()
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        search_future = pool.submit(refresh_search)
        assert metadata_read.wait(timeout=5)
        statistics_future = pool.submit(refresh_statistics)
        time.sleep(0.05)
        assert not statistics_done.is_set()
        release_upsert.set()
        search_future.result(timeout=5)
        statistics_future.result(timeout=5)

    reopened = _open(db_path)
    try:
        stored = get_viral_video(reopened, platform="wechat_channels", video_id="video-1")
        assert stored is not None
        assert stored.native["export_id"] == "export/fresh"
        assert stored.native["object_nonce_id"] == "n2"
        assert "_statistics_checked_at" in stored.native
        assert stored.comments == 9
    finally:
        reopened.close()


def test_fetch_state_records_time_and_obeys_ttl(tmp_path: Path) -> None:
    db_path = tmp_path / "viral.db"
    initialize_database(db_path).close()
    conn = _open(db_path)
    try:
        assert not fetch_state_is_fresh(
            conn, platform="wechat_channels", sort="hot", max_age=VIRAL_FETCH_TTL
        )
        assert viral_fetched_at(conn, platform="wechat_channels", sort="hot") is None

        before = datetime.now(UTC)
        mark_fetch_state(conn, platform="wechat_channels", sort="hot")
        fetched_at = viral_fetched_at(conn, platform="wechat_channels", sort="hot")
        assert fetched_at is not None
        assert datetime.fromisoformat(fetched_at) >= before
        assert fetch_state_is_fresh(
            conn, platform="wechat_channels", sort="hot", max_age=VIRAL_FETCH_TTL
        )

        stale_time = (datetime.now(UTC) - VIRAL_FETCH_TTL - timedelta(seconds=1)).isoformat()
        conn.execute(
            "UPDATE viral_fetch_state SET fetched_at = %s WHERE platform = %s AND sort = %s",
            (stale_time, "wechat_channels", "hot"),
        )
        conn.commit()
        assert not fetch_state_is_fresh(
            conn, platform="wechat_channels", sort="hot", max_age=VIRAL_FETCH_TTL
        )
    finally:
        conn.close()
