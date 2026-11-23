"""爆款视频持久化：客户目录只读取管理员上传或手工推送的归档内容。

历史采集与个人导入仍保存在内容池；公共发布权限统一由 customer_visible_sql 决定。
列表、计数和游标共享数据库快照，浏览不请求上游。
"""

from __future__ import annotations

import binascii
import json
import threading
from base64 import urlsafe_b64decode, urlsafe_b64encode
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast
from uuid import uuid4

from app.db_portable import BusinessConnection
from app.viral_content_state import customer_visible_sql, managed_source_sql
from app.viral_tikhub import MAX_TAGS, ViralVideo, WechatVideoDetail

VIRAL_FETCH_TTL = timedelta(hours=1)
# 客户解析新素材时的默认分类；运营资格由独立来源记录决定，不能再由分类推断。
LINK_IMPORT_CATEGORY = "链接导入"
STATISTICS_CHECKED_AT_KEY = "_statistics_checked_at"
STATISTICS_RETRY_AT_KEY = "_statistics_retry_at"
STATISTICS_OBJECT_ID_KEY = "_statistics_object_id"
_STATISTICS_METADATA_KEYS = (
    STATISTICS_CHECKED_AT_KEY,
    STATISTICS_RETRY_AT_KEY,
    STATISTICS_OBJECT_ID_KEY,
    "_statistics_updated_at",
    "_statistics_field_times",
    "_statistics_source",
)
_METADATA_LOOKUP_CHUNK_SIZE = 400
# 桌面服务是单进程；统一串行化 native_json 的读改写，避免不同请求互相覆盖。
_NATIVE_JSON_RMW_LOCK = threading.RLock()
ViralAvailability = Literal["available", "hidden", "unavailable"]

_INSERT_SQL = """
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
"""

_UPSERT_SQL = (
    _INSERT_SQL
    + """
ON CONFLICT (platform, video_id) DO UPDATE SET
    category = CASE WHEN excluded.category = '' THEN viral_videos.category
                    ELSE excluded.category END,
    title = excluded.title,
    author = excluded.author,
    author_avatar = COALESCE(excluded.author_avatar, viral_videos.author_avatar),
    verified = excluded.verified,
    cover_url = COALESCE(excluded.cover_url, viral_videos.cover_url),
    duration_ms = COALESCE(NULLIF(excluded.duration_ms, 0), viral_videos.duration_ms),
    likes = COALESCE(excluded.likes, viral_videos.likes),
    comments = COALESCE(excluded.comments, viral_videos.comments),
    shares = COALESCE(excluded.shares, viral_videos.shares),
    collects = COALESCE(excluded.collects, viral_videos.collects),
    published_at = COALESCE(excluded.published_at, viral_videos.published_at),
    published_display = COALESCE(excluded.published_display, viral_videos.published_display),
    like_display = CASE WHEN excluded.likes IS NOT NULL THEN excluded.like_display
                        ELSE viral_videos.like_display END,
    tags_json = CASE WHEN excluded.tags_json = '[]' THEN viral_videos.tags_json
                     ELSE excluded.tags_json END,
    play_url = COALESCE(excluded.play_url, viral_videos.play_url),
    audio_url = COALESCE(excluded.audio_url, viral_videos.audio_url),
    native_json = excluded.native_json,
    updated_at = CURRENT_TIMESTAMP
"""
)

_ORDER_BY = {
    "hot": "COALESCE(likes, -1) DESC, COALESCE(published_at, -1) DESC, video_id",
    "latest": "COALESCE(published_at, -1) DESC, COALESCE(likes, -1) DESC, video_id",
}

_AVAILABILITY_VALUES = {"available", "hidden", "unavailable"}

# featured 排序中「未置顶」的哨兵：int4 上限，等价于 NULLS LAST。
_VIRAL_RANK_SENTINEL = 2147483647

# 公共列表、收藏和缓存回收必须共用发布判据。
_PUBLISHED_SQL = "AND " + customer_visible_sql("viral_videos")


class InvalidViralCursorError(ValueError):
    """Raised when a list cursor is malformed or belongs to another query."""


@dataclass(frozen=True)
class ViralVideoPage:
    items: list[ViralVideo]
    total: int
    has_more: bool
    next_cursor: str | None
    data_version: str | None = None


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
        homepage_featured=bool(mapping.get("homepage_featured")),
        homepage_rank=mapping.get("homepage_rank"),
        homepage_starts_at=(
            str(mapping["homepage_starts_at"]) if mapping.get("homepage_starts_at") else None
        ),
        homepage_ends_at=(
            str(mapping["homepage_ends_at"]) if mapping.get("homepage_ends_at") else None
        ),
        pushed_at=(str(mapping["pushed_at"]) if mapping.get("pushed_at") else None),
        duration_ms=int(mapping.get("duration_ms") or 0),
        likes=mapping.get("likes"),
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
        native = dict(video.native)
        native.update(metadata or {})
        _record_statistics(
            native,
            {name: getattr(video, name) for name in ("likes", "comments", "shares", "collects")},
            source="platform_search",
        )
        merged.append(replace(video, native=native))
    return merged


def _record_statistics(
    native: dict[str, Any], values: dict[str, int | None], *, source: str
) -> None:
    now = datetime.now(UTC).isoformat()
    present = [name for name, value in values.items() if value is not None]
    native["_statistics_checked_at"] = now
    native["_statistics_status"] = (
        "updated"
        if len(present) == len(values)
        else "partial"
        if present or native.get("_statistics_updated_at")
        else "unknown"
    )
    if present:
        native["_statistics_updated_at"] = now
        native["_statistics_source"] = source
        times = dict(native.get("_statistics_field_times") or {})
        times.update({name: now for name in present})
        native["_statistics_field_times"] = times


def upsert_viral_videos(
    conn: BusinessConnection,
    videos: list[ViralVideo],
    *,
    commit: bool = True,
    update_existing: bool = True,
) -> set[tuple[str, str]]:
    """按 (platform, video_id) 去重；客户解析仅补新行，不改写已存在的运营信息。"""
    inserted: set[tuple[str, str]] = set()
    # 冲突判断由数据库串行化；不能先查来源再写，否则并发管理员导入尚未提交的
    # 来源记录不可见，客户请求仍可能覆盖管理员刚保存的标题、分类和封面。
    sql = (
        _UPSERT_SQL
        if update_existing
        else _INSERT_SQL + " ON CONFLICT (platform, video_id) DO NOTHING"
    )
    with _NATIVE_JSON_RMW_LOCK:
        for video in _merge_cached_statistics_metadata(conn, videos):
            row = conn.execute(
                sql + " RETURNING platform,video_id,(xmax=0) AS inserted",
                _video_row(video),
            ).fetchone()
            if row is not None and row["inserted"]:
                inserted.add((str(row["platform"]), str(row["video_id"])))
        if commit:
            conn.commit()
    return inserted


def list_viral_videos(conn: BusinessConnection, *, platform: str, sort: str) -> list[ViralVideo]:
    order = _ORDER_BY.get(sort, _ORDER_BY["hot"])
    rows = conn.execute(
        f"SELECT * FROM viral_videos WHERE platform=%s {_PUBLISHED_SQL} ORDER BY {order}",
        (platform,),
    ).fetchall()
    return [_row_to_video(row) for row in rows]


def reclaimable_viral_video_ids(
    conn: BusinessConnection, *, user_id: str, platform: str, video_ids: list[str]
) -> list[str]:
    """按公共发布权限回收目录缓存；收藏不能保留已撤回或从未推送的内容。"""
    del user_id
    if not video_ids:
        return []
    rows = conn.execute(
        f"SELECT video_id FROM viral_videos WHERE platform=%s AND video_id=ANY(%s) "
        f"{_PUBLISHED_SQL}",
        (platform, video_ids),
    ).fetchall()
    still_served = {str(row["video_id"]) for row in rows}
    return [video_id for video_id in video_ids if video_id not in still_served]


def _cursor_values(video: ViralVideo, sort: str, *, featured: bool = False) -> tuple[object, ...]:
    """游标键：非 featured 为 3 元键；featured 前置置顶序（哨兵=未置顶）."""
    published_at = video.published_at if video.published_at is not None else -1
    if featured:
        # featured 排序以置顶序为先：NULLS LAST 用 int4 上限哨兵编码进游标。
        rank = video.homepage_rank if video.homepage_rank is not None else _VIRAL_RANK_SENTINEL
        if sort == "latest":
            return (
                rank,
                published_at,
                (video.likes if video.likes is not None else -1),
                video.video_id,
            )
        return (
            rank,
            (video.likes if video.likes is not None else -1),
            published_at,
            video.video_id,
        )
    if sort == "latest":
        return (published_at, (video.likes if video.likes is not None else -1), video.video_id)
    return ((video.likes if video.likes is not None else -1), published_at, video.video_id)


def _encode_cursor(
    video: ViralVideo,
    *,
    platform: str,
    sort: str,
    data_version: str | None,
    featured: bool = False,
) -> str:
    payload = {
        # v3：featured 游标的 k 首位携带置顶序（哨兵=未置顶）；v2 为无置顶序
        # 的旧格式，仅非 featured 查询可继续消费。
        "v": 3 if featured else 2,
        "p": platform,
        "s": sort,
        "d": data_version,
        "k": list(_cursor_values(video, sort, featured=featured)),
    }
    encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return urlsafe_b64encode(encoded).decode("ascii").rstrip("=")


def _decode_cursor(
    cursor: str, *, platform: str, sort: str
) -> tuple[tuple[object, ...], str | None]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(urlsafe_b64decode(padded.encode("ascii")))
        values = payload["k"]
        version = payload.get("v")
        if (
            version not in (2, 3)
            or payload.get("p") != platform
            or payload.get("s") != sort
            or not isinstance(values, list)
            or (version == 2 and len(values) != 3)
            or (version == 3 and len(values) != 4)
            or not all(isinstance(value, int) for value in values[:-1])
            or not isinstance(values[-1], str)
        ):
            raise ValueError
        data_version = payload.get("d")
        if data_version is not None and not isinstance(data_version, str):
            raise ValueError
        return tuple(values), data_version
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


def _page_boundary_sql(
    sort: str, values: tuple[object, ...], *, featured: bool = False
) -> tuple[str, tuple[object, ...]]:
    if featured:
        rank, *rest = values
        base_sql, base_params = _page_boundary_sql(sort, tuple(rest))
        flat = " ".join(base_sql.split())
        return (
            f"AND (COALESCE(homepage_rank, %s) > %s OR (COALESCE(homepage_rank, %s) = %s {flat}))",
            (_VIRAL_RANK_SENTINEL, rank, _VIRAL_RANK_SENTINEL, rank, *base_params),
        )
    first, second, video_id = values
    if sort == "latest":
        return (
            """
            AND (
                COALESCE(published_at, -1) < %s
                OR (COALESCE(published_at, -1) = %s AND COALESCE(likes, -1) < %s)
                OR (COALESCE(published_at, -1) = %s AND COALESCE(likes, -1) = %s AND video_id > %s)
            )
            """,
            (first, first, second, first, second, video_id),
        )
    return (
        """
        AND (
            COALESCE(likes, -1) < %s
            OR (COALESCE(likes, -1) = %s AND COALESCE(published_at, -1) < %s)
            OR (COALESCE(likes, -1) = %s AND COALESCE(published_at, -1) = %s AND video_id > %s)
        )
        """,
        (first, first, second, first, second, video_id),
    )


def _list_homepage_snapshot(
    conn: BusinessConnection,
    *,
    platform: str,
    sort: str,
    limit: int,
    cursor: str | None,
    featured_only: bool = True,
) -> ViralVideoPage:
    # AuthReader已查询过数据库，不能中途切换隔离级别；版本、页和总数由同一SQL快照读取。
    ranked = featured_only and sort == "hot"
    order = _ORDER_BY.get(sort, _ORDER_BY["hot"])
    if ranked:
        order = f"homepage_rank ASC NULLS LAST, {order}"
    boundary, expected = (
        _decode_cursor(cursor, platform=platform, sort=sort) if cursor else (None, None)
    )
    if boundary is not None and len(boundary) != (4 if ranked else 3):
        raise InvalidViralCursorError("viral video cursor belongs to another list")
    boundary_sql, params = (
        _page_boundary_sql(sort, boundary, featured=ranked) if boundary else ("", ())
    )
    result = conn.execute(
        f"""WITH homepage_snapshot AS MATERIALIZED (
            SELECT * FROM viral_videos WHERE platform=%s AND pushed_at IS NOT NULL
                AND (NOT %s OR homepage_featured=1) AND deleted_at IS NULL
        ), eligible AS MATERIALIZED (
            SELECT * FROM homepage_snapshot AS viral_videos WHERE TRUE {_PUBLISHED_SQL}
        ), revision AS (
            SELECT md5(COALESCE(jsonb_agg(jsonb_build_array(video_id,homepage_rank,
                homepage_starts_at,homepage_ends_at,likes,published_at,title,pushed_at,
                homepage_featured,updated_at,
                EXISTS(SELECT 1 FROM eligible e WHERE e.video_id=homepage_snapshot.video_id))
                ORDER BY video_id)::text,'[]') || COALESCE((SELECT fetched_at::text
                    FROM viral_fetch_state WHERE platform=%s AND sort=%s),'')) AS value
            FROM homepage_snapshot
        ), page AS (
            SELECT * FROM eligible WHERE TRUE {boundary_sql} ORDER BY {order} LIMIT %s
        ) SELECT revision.value AS data_version,(SELECT count(*) FROM eligible) AS total,
            COALESCE((SELECT jsonb_agg(to_jsonb(page) ORDER BY {order})
                FROM page),'[]'::jsonb) AS rows
        FROM revision""",
        (platform, featured_only, platform, sort, *params, limit + 1),
    ).fetchone()
    version = f"{'home' if featured_only else 'public'}-snapshot:{result['data_version']}"
    if cursor is not None and expected != version:
        raise InvalidViralCursorError("viral video cursor data version changed")
    collected = [_row_to_video(row) for row in result["rows"]]
    items = collected[:limit]
    has_more = len(collected) > limit
    return ViralVideoPage(
        items=items,
        total=int(result["total"]),
        has_more=has_more,
        data_version=version,
        next_cursor=_encode_cursor(
            items[-1], platform=platform, sort=sort, data_version=version, featured=ranked
        )
        if has_more and items
        else None,
    )


def list_viral_video_page(
    conn: BusinessConnection,
    *,
    platform: str,
    sort: str,
    limit: int,
    cursor: str | None = None,
    featured_only: bool = False,
) -> ViralVideoPage:
    """公共列表与首页用同一快照计算页、总数和版本，上传推送后旧游标失效。"""
    return _list_homepage_snapshot(
        conn,
        platform=platform,
        sort=sort,
        limit=limit,
        cursor=cursor,
        featured_only=featured_only,
    )


def get_viral_video(conn: BusinessConnection, *, platform: str, video_id: str) -> ViralVideo | None:
    row = conn.execute(
        """
        SELECT * FROM viral_videos
        WHERE platform = %s AND video_id = %s AND deleted_at IS NULL
        """,
        (platform, video_id),
    ).fetchone()
    return _row_to_video(row) if row is not None else None


def viral_publication_clock(
    conn: BusinessConnection, *, platform: str, featured_only: bool
) -> tuple[str, str | None]:
    # 只报告人工发布资产的排期边界，私有导入与源站发布时间不会改变公共可见性。
    visible_before = customer_visible_sql("v", at="(change.boundary - interval '1 microsecond')")
    visible_after = customer_visible_sql("v", at="change.boundary")
    row = conn.execute(
        "SELECT now(), (SELECT min(change.boundary) FROM viral_videos v "
        "CROSS JOIN LATERAL (VALUES (v.homepage_starts_at), (v.homepage_ends_at)) "
        "change(boundary) WHERE v.platform=%s AND (NOT %s OR v.homepage_featured=1) "
        f"AND change.boundary>now() AND (({visible_before}) != ({visible_after})))",
        (platform, featured_only),
    ).fetchone()
    return row[0].isoformat(), row[1].isoformat() if row[1] else None


def is_customer_visible_viral_video(
    conn: BusinessConnection,
    *,
    platform: str,
    video_id: str,
    user_id: str,
    public_only: bool = False,
) -> bool:
    predicate = customer_visible_sql("v") if public_only else customer_accessible_viral_sql("v")
    params = (platform, video_id) if public_only else (platform, video_id, user_id)
    return (
        conn.execute(
            "SELECT 1 FROM viral_videos v WHERE v.platform=%s AND v.video_id=%s AND " + predicate,
            params,
        ).fetchone()
        is not None
    )


def customer_accessible_viral_sql(alias: str = "v") -> str:
    # 私有素材凭本人回执继续创作；历史误推送位不能撤销本人的使用权，也不能
    # 授予其他客户访问。确有运营来源的已推送条目仍严格受公共发布状态约束。
    return (
        f"({customer_visible_sql(alias)} OR (({alias}.pushed_at IS NULL "
        f"OR NOT {managed_source_sql(alias)}) "
        f"AND {alias}.deleted_at IS NULL AND NOT EXISTS "
        "(SELECT 1 FROM viral_video_visibility access_vis "
        f"WHERE access_vis.platform={alias}.platform "
        f"AND access_vis.video_id={alias}.video_id AND access_vis.status!='AVAILABLE') "
        f"AND ({alias}.homepage_starts_at IS NULL OR {alias}.homepage_starts_at<=now()) "
        f"AND ({alias}.homepage_ends_at IS NULL OR {alias}.homepage_ends_at>now()) "
        "AND EXISTS (SELECT 1 FROM ("
        "SELECT own_import.owner_user_id FROM viral_import_tasks own_import "
        f"WHERE own_import.platform={alias}.platform AND own_import.video_id={alias}.video_id "
        "AND own_import.status='SUCCEEDED' UNION ALL "
        "SELECT own_receipt.owner_user_id FROM viral_link_resolution_receipts own_receipt "
        "WHERE own_receipt.status='SUCCEEDED' "
        f"AND own_receipt.response_json::jsonb->'item'->>'platform'={alias}.platform "
        f"AND own_receipt.response_json::jsonb->'item'->>'videoId'={alias}.video_id"
        ") private_owners WHERE private_owners.owner_user_id=%s)))"
    )


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
        WHERE f.user_id = %s {platform_sql} AND {customer_visible_sql("v")}
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
                f.favorite_created_at < %s
                OR (f.favorite_created_at = %s AND f.platform > %s)
                OR (f.favorite_created_at = %s AND f.platform = %s AND f.video_id > %s)
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
    # 页和总数读取同一快照，避免下架恰好发生在两次查询之间造成口径错位。
    result = conn.execute(
        f"""WITH eligible AS MATERIALIZED (
            SELECT v.*, f.created_at AS favorite_created_at
            FROM viral_video_favorites f
            JOIN viral_videos v ON v.platform=f.platform AND v.video_id=f.video_id
            WHERE f.user_id=%s AND {customer_visible_sql("v")} {platform_sql}
        ), page AS (
            SELECT * FROM eligible f WHERE TRUE
            {boundary_sql}
            ORDER BY f.favorite_created_at DESC,f.platform,f.video_id LIMIT %s
        ) SELECT (SELECT count(*) FROM eligible) AS total,
            COALESCE((SELECT jsonb_agg(to_jsonb(page)
                ORDER BY favorite_created_at DESC,platform,video_id) FROM page),'[]'::jsonb) AS rows
        """,
        (user_id, *platform_params, *boundary_params, limit + 1),
    ).fetchone()
    rows = result["rows"]
    has_more = len(rows) > limit
    page_rows = rows[:limit]
    return ViralVideoPage(
        items=[_row_to_video(row) for row in page_rows],
        total=int(result["total"]),
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
    commit: bool = True,
) -> None:
    """封面落存储成功后回写 key（路由据此下发自有稳定地址）.

    ``commit=False`` 供已在事务内的调用方（搜索落库）使用，避免中途提交。
    """
    conn.execute(
        """
        UPDATE viral_videos
        SET cover_key = %s, updated_at = CURRENT_TIMESTAMP
        WHERE platform = %s AND video_id = %s
        """,
        (cover_key, platform, video_id),
    )
    if commit:
        conn.commit()


@dataclass(frozen=True)
class ViralDiscovery:
    """一条发现记录 + 其内容池条目（条目缺失/已删时为 None）."""

    platform: str
    video_id: str
    keyword: str
    search_date: str
    searched_at: str
    video: ViralVideo | None


def mark_viral_discoveries(
    conn: BusinessConnection,
    *,
    user_id: str,
    keyword: str,
    platform: str,
    video_ids: Sequence[str],
    search_date: str,
    searched_at: str,
) -> int:
    """记录"某客户某天用某词搜到了哪些视频"；同键重复命中只刷新 searched_at.

    返回尝试写入的去重条数（幂等重放不产生重复行）。
    """
    marked = 0
    for video_id in dict.fromkeys(video_ids):
        conn.execute(
            """
            INSERT INTO viral_search_discoveries
                (id, user_id, keyword, platform, video_id, search_date, searched_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (user_id, keyword, platform, video_id, search_date)
            DO UPDATE SET searched_at = excluded.searched_at,
                          updated_at = CURRENT_TIMESTAMP
            """,
            (uuid4().hex, user_id, keyword, platform, video_id, search_date, searched_at),
        )
        marked += 1
    return marked


def list_viral_discoveries(
    conn: BusinessConnection, *, user_id: str, search_date: str
) -> list[ViralDiscovery]:
    """客户"我的发现"（按天）：发现记录左联内容池，带回视频当前数据.

    ``d.platform`` / ``d.video_id`` 起别名避开与 ``v.*`` 的同名列冲突：
    LEFT JOIN 未命中时 ``v.platform`` 为 NULL，若与 ``d.platform`` 同名，
    ``dict(row)`` 会折叠成首个（NULL），发现记录自身字段会丢。
    """
    rows = conn.execute(
        """
        SELECT v.*, d.platform AS d_platform, d.video_id AS d_video_id,
               d.keyword, d.search_date, d.searched_at
        FROM viral_search_discoveries d
        LEFT JOIN viral_videos v
            ON v.platform = d.platform AND v.video_id = d.video_id
            AND v.deleted_at IS NULL
        WHERE d.user_id = %s AND d.search_date = %s
        ORDER BY d.searched_at DESC, d.keyword, d.video_id
        """,
        (user_id, search_date),
    ).fetchall()
    discoveries: list[ViralDiscovery] = []
    for row in rows:
        mapping = dict(row)
        video = _row_to_video(mapping) if mapping.get("title") is not None else None
        discoveries.append(
            ViralDiscovery(
                platform=str(mapping["d_platform"]),
                video_id=str(mapping["d_video_id"]),
                keyword=str(mapping["keyword"]),
                search_date=str(mapping["search_date"]),
                searched_at=str(mapping["searched_at"]),
                video=video,
            )
        )
    return discoveries


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
        _record_statistics(
            native,
            {
                "likes": detail.like_count,
                "comments": detail.comment_count,
                "shares": detail.forward_count,
                "collects": detail.fav_count,
            },
            source="platform_detail",
        )
        native.pop(STATISTICS_RETRY_AT_KEY, None)
        object_id = str(getattr(detail, "object_id", ""))
        if object_id.isascii() and object_id.isdigit() and len(object_id) <= 20:
            native[STATISTICS_OBJECT_ID_KEY] = object_id
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
        native["_statistics_status"] = "failed"
        native[STATISTICS_RETRY_AT_KEY] = datetime.now(UTC).isoformat()
        conn.execute(
            """
            UPDATE viral_videos SET native_json = %s, updated_at = CURRENT_TIMESTAMP
            WHERE platform = %s AND video_id = %s
            """,
            (json.dumps(native, ensure_ascii=False), platform, video_id),
        )
        conn.commit()
