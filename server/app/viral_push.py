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
    copy_text: str | None = None,
    copy_updated_at: str | None = None,
    pushed_at: str | None = None,
) -> str:
    """把视频元数据快照写进主存储，返回对象 key.

    快照是媒体对象的附属说明，不是数据真源：文案后续回填、统计刷新都只落
    数据库，快照只反映推送（或转存）那一刻的状态。
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
        "copy": (
            {"text": copy_text, "updatedAt": copy_updated_at} if copy_text is not None else None
        ),
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
