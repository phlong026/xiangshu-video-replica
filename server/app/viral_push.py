"""爆款视频「推送到前端」的共享逻辑（人工策展发布）.

人工采集模式下，内容上架由运营在管理端逐条/批量触发：先经既有
``archive_batch`` 任务完成媒体转存（下载/解密/上传 + 封面 + 统计补采），
素材就绪后由本模块把条目标记为已上架（``collection_published=1`` +
``pushed_at``）。``pushed_at`` 同时是客户可见口径豁免「发布时间 7 天窗口」
的标记——人工上架的内容展示多久由运营控制，不随源站发布时间自然滚出。

推送成功时还会把视频元数据（含统计与已回填文案）写一份 JSON 快照进主存储，
与媒体对象同命名空间，供追溯与排查（数据库仍是唯一真源）。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from app.db_portable import BusinessConnection
from app.storage import StorageAdapter
from app.viral_media import VIRAL_STORAGE_PREFIX, _storage_video_id
from app.viral_tikhub import ViralVideo

# 人工任务类型：运营显式触发的素材准备（单条/批量，可带发布意图）。
# 这些任务不受「定时采集开关」约束——开关只停关键词采集，不停人工动作。
MANUAL_TASK_KINDS = ("single_archive", "archive_batch")


def viral_meta_snapshot_key(platform: str, video_id: str) -> str:
    return f"{VIRAL_STORAGE_PREFIX}/meta/{platform}/{_storage_video_id(video_id)}.json"


def write_viral_metadata_snapshot(
    storage: StorageAdapter,
    video: ViralVideo,
    *,
    pushed_at: str | None = None,
) -> str:
    """把视频元数据快照写进主存储，返回对象 key.

    快照是媒体对象的附属说明，不是数据真源：文案后续回填、统计刷新都只落
    数据库，快照只反映推送（或转存）那一刻的状态。2026-11-11 共享缓存退役，
    快照不再携带共享文案字段。
    """
    snapshot: dict[str, Any] = {
        "snapshotVersion": 1,
        "platform": video.platform,
        "videoId": video.video_id,
        "category": video.category,
        "title": video.title,
        "author": video.author,
        "authorAvatar": video.author_avatar,
        "verified": video.verified,
        "durationMs": video.duration_ms,
        "statistics": {
            "likes": video.likes,
            "comments": video.comments,
            "shares": video.shares,
            "collects": video.collects,
        },
        "publishedAt": video.published_at,
        "publishedDisplay": video.published_display,
        "likeDisplay": video.like_display,
        "tags": list(video.tags),
        "native": dict(video.native),
        "pushedAt": pushed_at,
    }
    payload = json.dumps(snapshot, ensure_ascii=False).encode("utf-8")
    key = viral_meta_snapshot_key(video.platform, video.video_id)
    storage.put_object(key, payload, content_type="application/json")
    return key


def mark_viral_push_published(
    conn: BusinessConnection,
    *,
    platform: str,
    video_id: str,
    actor_user_id: str = "",
    reason: str = "",
    request_id: str = "",
) -> bool:
    """素材就绪前提下把条目标记为已上架；返回是否真的发生了状态变化.

    守卫与客户可见口径（``ready_sql`` + visibility）同源：媒体已归档、封面
    就绪、未删除、未下架才允许置位。推送动作幂等——重复推送不覆盖首推时间。
    """
    now_iso = datetime.now(UTC).isoformat()
    applied = conn.execute(
        """
        UPDATE viral_videos v SET collection_published = 1,
            pushed_at = COALESCE(v.pushed_at, %s)
        WHERE v.platform = %s AND v.video_id = %s AND v.deleted_at IS NULL
            AND EXISTS (
                SELECT 1 FROM viral_media_preparations m
                WHERE m.platform = v.platform AND m.video_id = v.video_id
                    AND m.media_kind = 'video' AND m.status = 'SUCCEEDED'
                    AND m.storage_uri IS NOT NULL
            )
            AND (COALESCE(v.cover_url,'') = '' OR v.cover_key IS NOT NULL)
            AND NOT EXISTS (
                SELECT 1 FROM viral_video_visibility vis
                WHERE vis.platform = v.platform AND vis.video_id = v.video_id
                    AND vis.status != 'AVAILABLE'
            )
        RETURNING v.video_id
        """,
        (now_iso, platform, video_id),
    ).fetchone()
    if applied is None:
        return False
    # actor_user_id 有外键：空串（历史配置缺操作人）必须落 NULL 而不是 ''。
    conn.execute(
        """INSERT INTO audit_logs(
                id,actor_user_id,action,entity_type,entity_id,metadata_json)
            VALUES(%s,%s,'viral_video.push','viral_video',%s,%s)""",
        (
            str(uuid4()),
            actor_user_id or None,
            f"{platform}:{video_id}",
            json.dumps(
                {"reason": reason, "request_id": request_id, "pushed_at": now_iso},
                ensure_ascii=False,
            ),
        ),
    )
    return True


def auto_publish_viral_video(
    conn: BusinessConnection,
    *,
    platform: str,
    video_id: str,
    actor_user_id: str = "",
    reason: str = "",
    request_id: str = "",
) -> bool:
    """自动上架：管理端「爆款视频获取」全自动链路的发布动作.

    与人工 push 共用同一守卫（``mark_viral_push_published``：媒体已归档、
    封面就绪、未删除、未下架），上架成功后把条目置顶首页推荐位——首页
    推荐位跟随后台发布自动变动，不再依赖「首页编排」人工维护。首页容量取
    ``viral_runtime_controls.homepage_capacity``（NULL = 不设上限），超出
    容量的最旧条目自动撤出推荐位（仍在客户列表，不丢内容）。重复触发不
    重复置位：仅首推才动首页，幂等重放不会把老条目重新顶到最前。
    """
    existing = conn.execute(
        "SELECT pushed_at FROM viral_videos WHERE platform=%s AND video_id=%s",
        (platform, video_id),
    ).fetchone()
    first_publish = existing is not None and existing[0] is None
    published = mark_viral_push_published(
        conn,
        platform=platform,
        video_id=video_id,
        actor_user_id=actor_user_id,
        reason=reason,
        request_id=request_id,
    )
    if not published:
        return False
    if first_publish:
        _feature_homepage_top(conn, platform=platform, video_id=video_id)
    return True


def _feature_homepage_top(conn: BusinessConnection, *, platform: str, video_id: str) -> None:
    """置顶首页推荐位（rank 取当前最小值再减一），并按容量撤掉最旧条目.

    下架仍走既有 hide：首页快照查询自带 visibility/删除/就绪守卫，下架
    条目自动从推荐位消失，无需额外清理。
    """
    capacity_row = conn.execute(
        "SELECT homepage_capacity FROM viral_runtime_controls WHERE id=1"
    ).fetchone()
    capacity = int(capacity_row[0]) if capacity_row is not None and capacity_row[0] else None
    conn.execute(
        """
        UPDATE viral_videos v SET homepage_featured = 1,
            homepage_rank = COALESCE((
                SELECT MIN(v2.homepage_rank) FROM viral_videos v2
                WHERE v2.platform = v.platform AND v2.homepage_featured = 1
            ), 0) - 1,
            homepage_featured_at = now(),
            homepage_starts_at = NULL, homepage_ends_at = NULL
        WHERE v.platform = %s AND v.video_id = %s
        """,
        (platform, video_id),
    )
    if capacity is not None and capacity > 0:
        conn.execute(
            """
            UPDATE viral_videos SET homepage_featured = 0, homepage_rank = NULL,
                homepage_featured_at = NULL
            WHERE platform = %s AND homepage_featured = 1
                AND (platform, video_id) NOT IN (
                    SELECT platform, video_id FROM viral_videos
                    WHERE platform = %s AND homepage_featured = 1
                    ORDER BY homepage_rank ASC NULLS LAST, pushed_at DESC NULLS LAST
                    LIMIT %s
                )
            """,
            (platform, platform, capacity),
        )
