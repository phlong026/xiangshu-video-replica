"""爆款视频参考库持久化（C4 重启）.

搜索结果按 ``(platform, video_id)`` 唯一 upsert 到 ``viral_videos``：
跨分类/跨排序去重，互动数据随每次回源刷新，未再出现的条目保留在库中
作为长期参考。列表读取走库，回源判据由 ``viral_fetch_state`` 提供
（TTL 内只读库不请求上游，作为计费护栏）。

SQL 一律走 ``BusinessConnection.execute``（PostgreSQL ``%s`` 占位符，
SQLite 由 ``translate_to_sqlite`` 翻译）。
"""

from __future__ import annotations

import binascii
import json
import threading
from base64 import urlsafe_b64decode, urlsafe_b64encode
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast

from app.db_portable import BusinessConnection
from app.viral_tikhub import MAX_TAGS, ViralVideo, WechatVideoDetail, is_irrelevant_viral_video

VIRAL_FETCH_TTL = timedelta(hours=1)
STATISTICS_CHECKED_AT_KEY = "_statistics_checked_at"
STATISTICS_RETRY_AT_KEY = "_statistics_retry_at"
_STATISTICS_METADATA_KEYS = (STATISTICS_CHECKED_AT_KEY, STATISTICS_RETRY_AT_KEY)
_METADATA_LOOKUP_CHUNK_SIZE = 400
# 桌面服务是单进程；统一串行化 native_json 的读改写，避免不同请求互相覆盖。
_NATIVE_JSON_RMW_LOCK = threading.RLock()
ViralAvailability = Literal["available", "hidden", "unavailable"]

_UPSERT_SQL = """
INSERT INTO viral_videos (
    platform, video_id, category, title, author, author_avatar, verified,
    cover_url, cover_key, duration_ms, likes, comments, shares, collects,
    published_at, published_display, like_display, tags_json,
    play_url, audio_url, native_json
) VALUES (
    %s, %s, %s, %s, %s, %s, %s,
    %s, %s, %s, %s, %s, %s, %s,
    %s, %s, %s, %s,
    %s, %s, %s
)
ON CONFLICT (platform, video_id) DO UPDATE SET
    category = excluded.category,
    title = excluded.title,
    author = excluded.author,
    author_avatar = COALESCE(excluded.author_avatar, viral_videos.author_avatar),
    verified = excluded.verified,
    cover_url = COALESCE(excluded.cover_url, viral_videos.cover_url),
    duration_ms = COALESCE(NULLIF(excluded.duration_ms, 0), viral_videos.duration_ms),
    likes = excluded.likes,
    comments = COALESCE(excluded.comments, viral_videos.comments),
    shares = COALESCE(excluded.shares, viral_videos.shares),
    collects = COALESCE(excluded.collects, viral_videos.collects),
    published_at = COALESCE(excluded.published_at, viral_videos.published_at),
    published_display = COALESCE(excluded.published_display, viral_videos.published_display),
    like_display = excluded.like_display,
    tags_json = CASE WHEN excluded.tags_json = '[]' THEN viral_videos.tags_json
                     ELSE excluded.tags_json END,
    play_url = COALESCE(excluded.play_url, viral_videos.play_url),
    audio_url = COALESCE(excluded.audio_url, viral_videos.audio_url),
    native_json = excluded.native_json,
    updated_at = CURRENT_TIMESTAMP
"""

_ORDER_BY = {
    "hot": "likes DESC, COALESCE(published_at, -1) DESC, video_id",
    "latest": "COALESCE(published_at, -1) DESC, likes DESC, video_id",
}

_AVAILABILITY_VALUES = {"available", "hidden", "unavailable"}


class InvalidViralCursorError(ValueError):
    """Raised when a list cursor is malformed or belongs to another query."""


@dataclass(frozen=True)
class ViralVideoPage:
    items: list[ViralVideo]
    total: int
    has_more: bool
    next_cursor: str | None


def _row_to_video(row: Any) -> ViralVideo:
    mapping = dict(row)
    tags = json.loads(mapping.get("tags_json") or "[]")
    native = json.loads(mapping.get("native_json") or "{}")
    return ViralVideo(
        platform=str(mapping["platform"]),
        video_id=str(mapping["video_id"]),
        category=str(mapping.get("category") or ""),
        title=str(mapping.get("title") or ""),
        author=str(mapping.get("author") or ""),
        author_avatar=mapping.get("author_avatar"),
        verified=bool(mapping.get("verified")),
        cover_url=mapping.get("cover_url"),
        cover_key=mapping.get("cover_key"),
        duration_ms=int(mapping.get("duration_ms") or 0),
        likes=int(mapping.get("likes") or 0),
        comments=mapping.get("comments"),
        shares=mapping.get("shares"),
        collects=mapping.get("collects"),
        published_at=mapping.get("published_at"),
        published_display=mapping.get("published_display"),
        like_display=mapping.get("like_display"),
        tags=[str(tag) for tag in tags][:MAX_TAGS],
        play_url=mapping.get("play_url"),
        audio_url=mapping.get("audio_url"),
        native=native if isinstance(native, dict) else {},
    )


def _video_row(video: ViralVideo) -> tuple[object, ...]:
    return (
        video.platform,
        video.video_id,
        video.category,
        video.title,
        video.author,
        video.author_avatar,
        1 if video.verified else 0,
        video.cover_url,
        video.cover_key,
        video.duration_ms,
        video.likes,
        video.comments,
        video.shares,
        video.collects,
        video.published_at,
        video.published_display,
        video.like_display,
        json.dumps(video.tags[:MAX_TAGS], ensure_ascii=False),
        video.play_url,
        video.audio_url,
        json.dumps(video.native, ensure_ascii=False),
    )


def _native_from_json(value: object) -> dict[str, Any]:
    try:
        native = json.loads(str(value or "{}"))
    except json.JSONDecodeError:
        return {}
    return native if isinstance(native, dict) else {}


def _cached_statistics_metadata(
    conn: BusinessConnection, videos: list[ViralVideo]
) -> dict[tuple[str, str], dict[str, Any]]:
    by_platform: dict[str, list[str]] = {}
    for video in videos:
        ids = by_platform.setdefault(video.platform, [])
        if video.video_id not in ids:
            ids.append(video.video_id)

    cached: dict[tuple[str, str], dict[str, Any]] = {}
    for platform, video_ids in by_platform.items():
        for start in range(0, len(video_ids), _METADATA_LOOKUP_CHUNK_SIZE):
            chunk = video_ids[start : start + _METADATA_LOOKUP_CHUNK_SIZE]
            placeholders = ", ".join("%s" for _ in chunk)
            rows = conn.execute(
                f"""
                SELECT platform, video_id, native_json FROM viral_videos
                WHERE platform = %s AND video_id IN ({placeholders})
                """,
                (platform, *chunk),
            ).fetchall()
            for row in rows:
                native = _native_from_json(row["native_json"])
                metadata = {key: native[key] for key in _STATISTICS_METADATA_KEYS if key in native}
                if metadata:
                    cached[(str(row["platform"]), str(row["video_id"]))] = metadata
    return cached


def _merge_cached_statistics_metadata(
    conn: BusinessConnection, videos: list[ViralVideo]
) -> list[ViralVideo]:
    cached = _cached_statistics_metadata(conn, videos)
    merged: list[ViralVideo] = []
    for video in videos:
        metadata = cached.get((video.platform, video.video_id))
        if not metadata:
            merged.append(video)
            continue
        native = dict(video.native)
        native.update(metadata)
        merged.append(replace(video, native=native))
    return merged


def upsert_viral_videos(
    conn: BusinessConnection, videos: list[ViralVideo], *, commit: bool = True
) -> None:
    """按 (platform, video_id) 去重写入/刷新条目."""
    with _NATIVE_JSON_RMW_LOCK:
        for video in _merge_cached_statistics_metadata(conn, videos):
            conn.execute(_UPSERT_SQL, _video_row(video))
        if commit:
            conn.commit()


def list_viral_videos(conn: BusinessConnection, *, platform: str, sort: str) -> list[ViralVideo]:
    order = _ORDER_BY.get(sort, _ORDER_BY["hot"])
    now = int(datetime.now(UTC).timestamp())
    rows = conn.execute(
        f"""
        SELECT * FROM viral_videos
        WHERE platform = %s AND published_at BETWEEN %s AND %s
        ORDER BY {order}
        """,
        (platform, now - int(timedelta(days=7).total_seconds()), now),
    ).fetchall()
    return [_row_to_video(row) for row in rows if not is_irrelevant_viral_video(str(row["title"]))]


def _cursor_values(video: ViralVideo, sort: str) -> tuple[int, int, str]:
    published_at = video.published_at if video.published_at is not None else -1
    if sort == "latest":
        return published_at, video.likes, video.video_id
    return video.likes, published_at, video.video_id


def _encode_cursor(video: ViralVideo, *, platform: str, sort: str, data_version: str | None) -> str:
    payload = {
        "v": 2,
        "p": platform,
        "s": sort,
        "d": data_version,
        "k": list(_cursor_values(video, sort)),
    }
    encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return urlsafe_b64encode(encoded).decode("ascii").rstrip("=")


def _decode_cursor(
    cursor: str, *, platform: str, sort: str
) -> tuple[tuple[int, int, str], str | None]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(urlsafe_b64decode(padded.encode("ascii")))
        values = payload["k"]
        if (
            payload.get("v") != 2
            or payload.get("p") != platform
            or payload.get("s") != sort
            or not isinstance(values, list)
            or len(values) != 3
            or not isinstance(values[0], int)
            or not isinstance(values[1], int)
            or not isinstance(values[2], str)
        ):
            raise ValueError
        data_version = payload.get("d")
        if data_version is not None and not isinstance(data_version, str):
            raise ValueError
        return (values[0], values[1], values[2]), data_version
    except (
        AttributeError,
        binascii.Error,
        KeyError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
        UnicodeDecodeError,
    ) as exc:
        raise InvalidViralCursorError("invalid viral video cursor") from exc


def validate_viral_cursor(cursor: str, *, platform: str, sort: str) -> None:
    _decode_cursor(cursor, platform=platform, sort=sort)


def _page_boundary_sql(sort: str, values: tuple[int, int, str]) -> tuple[str, tuple[object, ...]]:
    first, second, video_id = values
    if sort == "latest":
        return (
            """
            AND (
                COALESCE(published_at, -1) < %s
                OR (COALESCE(published_at, -1) = %s AND likes < %s)
                OR (COALESCE(published_at, -1) = %s AND likes = %s AND video_id > %s)
            )
            """,
            (first, first, second, first, second, video_id),
        )
    return (
        """
        AND (
            likes < %s
            OR (likes = %s AND COALESCE(published_at, -1) < %s)
            OR (likes = %s AND COALESCE(published_at, -1) = %s AND video_id > %s)
        )
        """,
        (first, first, second, first, second, video_id),
    )


def _recent_relevant_total(
    conn: BusinessConnection, *, platform: str, cutoff: int, now: int
) -> int:
    rows = conn.execute(
        """
        SELECT title FROM viral_videos
        WHERE platform = %s AND published_at BETWEEN %s AND %s
          AND NOT EXISTS (
              SELECT 1 FROM viral_video_visibility visibility
              WHERE visibility.platform = viral_videos.platform
                AND visibility.video_id = viral_videos.video_id
                AND visibility.status != 'AVAILABLE'
          )
        """,
        (platform, cutoff, now),
    ).fetchall()
    return sum(not is_irrelevant_viral_video(str(row["title"])) for row in rows)


def list_viral_video_page(
    conn: BusinessConnection,
    *,
    platform: str,
    sort: str,
    limit: int,
    cursor: str | None = None,
) -> ViralVideoPage:
    """Read one stable keyset page without loading all video rows into memory."""
    order = _ORDER_BY.get(sort, _ORDER_BY["hot"])
    cutoff = int((datetime.now(UTC) - timedelta(days=7)).timestamp())
    now = int(datetime.now(UTC).timestamp())
    data_version = viral_fetched_at(conn, platform=platform, sort=sort)
    boundary = None
    if cursor:
        boundary, cursor_version = _decode_cursor(cursor, platform=platform, sort=sort)
        if cursor_version != data_version:
            raise InvalidViralCursorError("viral video cursor data version changed")
    collected: list[ViralVideo] = []
    exhausted = False
    chunk_size = max(32, min(100, limit * 2))

    while len(collected) <= limit and not exhausted:
        boundary_sql, boundary_params = (
            _page_boundary_sql(sort, boundary) if boundary is not None else ("", ())
        )
        rows = conn.execute(
            f"""
            SELECT * FROM viral_videos
            WHERE platform = %s AND published_at BETWEEN %s AND %s
              AND NOT EXISTS (
                  SELECT 1 FROM viral_video_visibility visibility
                  WHERE visibility.platform = viral_videos.platform
                    AND visibility.video_id = viral_videos.video_id
                    AND visibility.status != 'AVAILABLE'
              )
            {boundary_sql}
            ORDER BY {order}
            LIMIT %s
            """,
            (platform, cutoff, now, *boundary_params, chunk_size),
        ).fetchall()
        exhausted = len(rows) < chunk_size
        if not rows:
            break
        for row in rows:
            video = _row_to_video(row)
            if not is_irrelevant_viral_video(video.title):
                collected.append(video)
                if len(collected) > limit:
                    break
        boundary = _cursor_values(_row_to_video(rows[-1]), sort)

    items = collected[:limit]
    has_more = len(collected) > limit
    return ViralVideoPage(
        items=items,
        total=_recent_relevant_total(conn, platform=platform, cutoff=cutoff, now=now),
        has_more=has_more,
        next_cursor=(
            _encode_cursor(
                items[-1],
                platform=platform,
                sort=sort,
                data_version=data_version,
            )
            if has_more and items
            else None
        ),
    )


def get_viral_video(conn: BusinessConnection, *, platform: str, video_id: str) -> ViralVideo | None:
    row = conn.execute(
        """
        SELECT * FROM viral_videos
        WHERE platform = %s AND video_id = %s
        """,
        (platform, video_id),
    ).fetchone()
    return _row_to_video(row) if row is not None else None


def viral_video_availabilities(
    conn: BusinessConnection, *, platform: str, video_ids: list[str]
) -> dict[str, ViralAvailability]:
    if not video_ids:
        return {}
    placeholders = ", ".join("%s" for _ in video_ids)
    rows = conn.execute(
        f"""
        SELECT video_id, status FROM viral_video_visibility
        WHERE platform = %s AND video_id IN ({placeholders})
        """,
        (platform, *video_ids),
    ).fetchall()
    result: dict[str, ViralAvailability] = {}
    for row in rows:
        value = str(row["status"]).lower()
        if value in _AVAILABILITY_VALUES:
            result[str(row["video_id"])] = cast(ViralAvailability, value)
    return result


def viral_video_availability(
    conn: BusinessConnection, *, platform: str, video_id: str
) -> ViralAvailability:
    return viral_video_availabilities(conn, platform=platform, video_ids=[video_id]).get(
        video_id, "available"
    )


def viral_runtime_controls(conn: BusinessConnection) -> tuple[bool, bool]:
    row = conn.execute(
        "SELECT collection_enabled, import_enabled FROM viral_runtime_controls WHERE id = 1"
    ).fetchone()
    if row is None:
        return False, False
    return bool(row["collection_enabled"]), bool(row["import_enabled"])


def is_viral_favorite(
    conn: BusinessConnection, *, user_id: str, platform: str, video_id: str
) -> bool:
    return (
        conn.execute(
            """
            SELECT 1 FROM viral_video_favorites
            WHERE user_id = %s AND platform = %s AND video_id = %s
            """,
            (user_id, platform, video_id),
        ).fetchone()
        is not None
    )


def favorite_viral_video_ids(
    conn: BusinessConnection, *, user_id: str, platform: str, video_ids: list[str]
) -> set[str]:
    if not video_ids:
        return set()
    placeholders = ", ".join("%s" for _ in video_ids)
    rows = conn.execute(
        f"""
        SELECT video_id FROM viral_video_favorites
        WHERE user_id = %s AND platform = %s AND video_id IN ({placeholders})
        """,
        (user_id, platform, *video_ids),
    ).fetchall()
    return {str(row["video_id"]) for row in rows}


def add_viral_favorite(
    conn: BusinessConnection, *, user_id: str, platform: str, video_id: str
) -> bool:
    cursor = conn.execute(
        """
        INSERT INTO viral_video_favorites (user_id, platform, video_id)
        VALUES (%s, %s, %s)
        ON CONFLICT (user_id, platform, video_id) DO NOTHING
        """,
        (user_id, platform, video_id),
    )
    conn.commit()
    return cursor.rowcount > 0


def remove_viral_favorite(
    conn: BusinessConnection, *, user_id: str, platform: str, video_id: str
) -> bool:
    cursor = conn.execute(
        """
        DELETE FROM viral_video_favorites
        WHERE user_id = %s AND platform = %s AND video_id = %s
        """,
        (user_id, platform, video_id),
    )
    conn.commit()
    return cursor.rowcount > 0


def list_favorite_viral_videos(
    conn: BusinessConnection, *, user_id: str, platform: str | None = None
) -> list[ViralVideo]:
    platform_sql = "AND f.platform = %s" if platform is not None else ""
    params: tuple[object, ...] = (user_id, platform) if platform is not None else (user_id,)
    rows = conn.execute(
        f"""
        SELECT v.* FROM viral_video_favorites f
        JOIN viral_videos v ON v.platform = f.platform AND v.video_id = f.video_id
        WHERE f.user_id = %s {platform_sql}
        ORDER BY f.created_at DESC, f.platform, f.video_id
        """,
        params,
    ).fetchall()
    return [_row_to_video(row) for row in rows]


def _encode_favorite_cursor(
    *, created_at: str, platform: str, video_id: str, platform_filter: str | None
) -> str:
    payload = {
        "v": 1,
        "t": "favorite",
        "p": platform_filter,
        "k": [created_at, platform, video_id],
    }
    encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return urlsafe_b64encode(encoded).decode("ascii").rstrip("=")


def _decode_favorite_cursor(cursor: str, *, platform_filter: str | None) -> tuple[str, str, str]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(urlsafe_b64decode(padded.encode("ascii")))
        values = payload["k"]
        if (
            payload.get("v") != 1
            or payload.get("t") != "favorite"
            or payload.get("p") != platform_filter
            or not isinstance(values, list)
            or len(values) != 3
            or any(not isinstance(value, str) for value in values)
        ):
            raise ValueError
        return values[0], values[1], values[2]
    except (
        AttributeError,
        binascii.Error,
        KeyError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
        UnicodeDecodeError,
    ) as exc:
        raise InvalidViralCursorError("invalid favorite cursor") from exc


def list_favorite_viral_video_page(
    conn: BusinessConnection,
    *,
    user_id: str,
    limit: int,
    platform: str | None = None,
    cursor: str | None = None,
) -> ViralVideoPage:
    """Read a user's favorites without loading the full collection."""
    platform_sql = "AND f.platform = %s" if platform is not None else ""
    platform_params: tuple[object, ...] = (platform,) if platform is not None else ()
    boundary_sql = ""
    boundary_params: tuple[object, ...] = ()
    if cursor is not None:
        created_at, boundary_platform, video_id = _decode_favorite_cursor(
            cursor, platform_filter=platform
        )
        boundary_sql = """
            AND (
                f.created_at < %s
                OR (f.created_at = %s AND f.platform > %s)
                OR (f.created_at = %s AND f.platform = %s AND f.video_id > %s)
            )
        """
        boundary_params = (
            created_at,
            created_at,
            boundary_platform,
            created_at,
            boundary_platform,
            video_id,
        )
    rows = conn.execute(
        f"""
        SELECT v.*, f.created_at AS favorite_created_at
        FROM viral_video_favorites f
        JOIN viral_videos v ON v.platform = f.platform AND v.video_id = f.video_id
        WHERE f.user_id = %s {platform_sql} {boundary_sql}
        ORDER BY f.created_at DESC, f.platform, f.video_id
        LIMIT %s
        """,
        (user_id, *platform_params, *boundary_params, limit + 1),
    ).fetchall()
    total_row = conn.execute(
        f"""
        SELECT COUNT(*) AS count
        FROM viral_video_favorites f
        JOIN viral_videos v ON v.platform = f.platform AND v.video_id = f.video_id
        WHERE f.user_id = %s {platform_sql}
        """,
        (user_id, *platform_params),
    ).fetchone()
    has_more = len(rows) > limit
    page_rows = rows[:limit]
    return ViralVideoPage(
        items=[_row_to_video(row) for row in page_rows],
        total=int(total_row["count"]) if total_row is not None else 0,
        has_more=has_more,
        next_cursor=(
            _encode_favorite_cursor(
                created_at=str(page_rows[-1]["favorite_created_at"]),
                platform=str(page_rows[-1]["platform"]),
                video_id=str(page_rows[-1]["video_id"]),
                platform_filter=platform,
            )
            if has_more and page_rows
            else None
        ),
    )


def update_viral_cover(
    conn: BusinessConnection,
    *,
    platform: str,
    video_id: str,
    cover_key: str,
) -> None:
    """封面落存储成功后回写 key（路由据此下发自有稳定地址）."""
    conn.execute(
        """
        UPDATE viral_videos
        SET cover_key = %s, updated_at = CURRENT_TIMESTAMP
        WHERE platform = %s AND video_id = %s
        """,
        (cover_key, platform, video_id),
    )
    conn.commit()


def fetch_state_is_fresh(
    conn: BusinessConnection, *, platform: str, sort: str, max_age: timedelta
) -> bool:
    row = conn.execute(
        """
        SELECT fetched_at FROM viral_fetch_state
        WHERE platform = %s AND sort = %s
        """,
        (platform, sort),
    ).fetchone()
    if row is None:
        return False
    try:
        fetched_at = datetime.fromisoformat(str(row["fetched_at"]))
    except ValueError:
        return False
    if fetched_at.tzinfo is None:
        fetched_at = fetched_at.replace(tzinfo=UTC)
    return datetime.now(UTC) - fetched_at <= max_age


def mark_fetch_state(conn: BusinessConnection, *, platform: str, sort: str) -> None:
    now = datetime.now(UTC).isoformat()
    conn.execute(
        """
        INSERT INTO viral_fetch_state (platform, sort, fetched_at)
        VALUES (%s, %s, %s)
        ON CONFLICT (platform, sort) DO UPDATE SET
            fetched_at = excluded.fetched_at
        """,
        (platform, sort, now),
    )
    conn.commit()


def viral_fetched_at(conn: BusinessConnection, *, platform: str, sort: str) -> str | None:
    row = conn.execute(
        "SELECT fetched_at FROM viral_fetch_state WHERE platform = %s AND sort = %s",
        (platform, sort),
    ).fetchone()
    return str(row["fetched_at"]) if row else None


def update_viral_statistics(
    conn: BusinessConnection, *, platform: str, video_id: str, detail: WechatVideoDetail
) -> None:
    """复用媒体详情响应，仅回填实际返回的统计；搜索缺失字段不冲掉补采结果。"""
    with _NATIVE_JSON_RMW_LOCK:
        row = conn.execute(
            "SELECT native_json FROM viral_videos WHERE platform = %s AND video_id = %s",
            (platform, video_id),
        ).fetchone()
        if row is None:
            return
        native = _native_from_json(row["native_json"])
        native[STATISTICS_CHECKED_AT_KEY] = datetime.now(UTC).isoformat()
        native.pop(STATISTICS_RETRY_AT_KEY, None)
        description = getattr(detail, "description", None)
        if description:
            native["source_description"] = description
        conn.execute(
            """
            UPDATE viral_videos SET
                likes = COALESCE(%s, likes), comments = COALESCE(%s, comments),
                shares = COALESCE(%s, shares), collects = COALESCE(%s, collects),
                like_display = CASE WHEN %s IS NOT NULL THEN NULL ELSE like_display END,
                native_json = %s,
                updated_at = CURRENT_TIMESTAMP
            WHERE platform = %s AND video_id = %s
            """,
            (
                detail.like_count,
                detail.comment_count,
                detail.forward_count,
                detail.fav_count,
                detail.like_count,
                json.dumps(native, ensure_ascii=False),
                platform,
                video_id,
            ),
        )
        conn.commit()


def mark_viral_statistics_failure(
    conn: BusinessConnection, *, platform: str, video_id: str
) -> None:
    """记录详情补采失败时间，供短冷却复用；不改成功时间与已有统计。"""
    with _NATIVE_JSON_RMW_LOCK:
        row = conn.execute(
            "SELECT native_json FROM viral_videos WHERE platform = %s AND video_id = %s",
            (platform, video_id),
        ).fetchone()
        if row is None:
            return
        native = _native_from_json(row["native_json"])
        native[STATISTICS_RETRY_AT_KEY] = datetime.now(UTC).isoformat()
        conn.execute(
            """
            UPDATE viral_videos SET native_json = %s, updated_at = CURRENT_TIMESTAMP
            WHERE platform = %s AND video_id = %s
            """,
            (json.dumps(native, ensure_ascii=False), platform, video_id),
        )
        conn.commit()
