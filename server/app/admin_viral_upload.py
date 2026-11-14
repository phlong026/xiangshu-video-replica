"""管理员上传的视频直接接入既有素材归档与自动发布流程.

2026-11 内容组精简：上传即自动上架（列表 + 首页推荐位置顶），不再依赖
「视频库」人工推送；平台放开到三平台（抖音 / 小红书 / 视频号），卡片展示
所需元数据（作者 / 认证 / 发布时间 / 互动数据 / 标签）由上传人在信息确认
弹窗补填，时长与封面仍由服务端探测/抽帧生成。
"""

import hashlib
import json
import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import uuid4

import psycopg
from fastapi import APIRouter, Form, Request, Response, UploadFile
from pydantic import Field

from app.admin_auth_routes import ContentWriter
from app.admin_write_contract import (
    AdminWriteContract,
    http_error,
    require_write_contract,
    write_with_idempotency,
)
from app.db_portable import BusinessConnection
from app.material_thumbs import extract_thumbnail_jpeg, thumbnail_key_for
from app.media import MAX_UPLOAD_BYTES, FFprobeVideoProbe, probe_video, validate_upload_request
from app.media_tools import MediaValidationFailed, normalize_generated_video
from app.storage import StoredObject
from app.viral_content_observations import record_content_source, record_content_stage
from app.viral_media import viral_cover_key
from app.viral_media_preparation import ViralMediaPreparation
from app.viral_push import auto_publish_viral_video
from app.viral_store import update_viral_cover, upsert_viral_videos
from app.viral_tikhub import MAX_TAGS, ViralVideo

router = APIRouter()

# 卡片标签解析：分隔符 = 空白 / 中英文逗号 / #；去重保序，单标签截 30 字。
_TAG_SPLIT = re.compile(r"[\s,，#]+")


def _parse_upload_tags(raw: str) -> list[str]:
    tags: list[str] = []
    for token in _TAG_SPLIT.split(raw or ""):
        token = token.strip().lstrip("#")[:30]
        if token and token not in tags:
            tags.append(token)
    return tags[:MAX_TAGS]


def _parse_published(raw: str) -> int | None:
    """发布时间（YYYY-MM-DD）→ epoch 秒；解析不了就留空，不阻塞上传."""
    if not raw:
        return None
    try:
        return int(datetime.strptime(raw, "%Y-%m-%d").replace(hour=12, tzinfo=UTC).timestamp())
    except ValueError:
        return None


class ViralUploadRequest(AdminWriteContract):
    platform: Literal["douyin", "wechat_channels", "xiaohongshu"]
    title: str = Field(min_length=1, max_length=200)
    author: str = Field(max_length=120)
    sha256: str
    verified: bool = False
    published_display: str = Field(default="", max_length=40)
    likes: int | None = Field(default=None, ge=0)
    comments: int | None = Field(default=None, ge=0)
    shares: int | None = Field(default=None, ge=0)
    collects: int | None = Field(default=None, ge=0)
    tags: str = Field(default="", max_length=300)


@router.post("/viral/uploads", status_code=201)
def upload_viral_video(
    request: Request,
    response: Response,
    actor: ContentWriter,
    file: UploadFile,
    platform: Annotated[Literal["douyin", "wechat_channels", "xiaohongshu"], Form()],
    title: Annotated[str, Form(min_length=1, max_length=200)],
    author: Annotated[str, Form(max_length=120)] = "",
    confirm: Annotated[bool, Form()] = False,
    reason: Annotated[str, Form()] = "",
    verified: Annotated[bool, Form()] = False,
    published_display: Annotated[str, Form(max_length=40)] = "",
    likes: Annotated[int | None, Form(ge=0)] = None,
    comments: Annotated[int | None, Form(ge=0)] = None,
    shares: Annotated[int | None, Form(ge=0)] = None,
    collects: Annotated[int | None, Form(ge=0)] = None,
    tags: Annotated[str, Form(max_length=300)] = "",
) -> dict[str, object]:
    from app.media_routes import get_media_storage

    if not title.strip():
        raise http_error(422, "VIRAL_UPLOAD_TITLE_REQUIRED", "请输入视频标题。")
    validate_upload_request(
        filename=file.filename or "",
        content_type=file.content_type or "",
        size_bytes=file.size or 0,
        purpose="admin_upload",
    )
    content = file.file.read(MAX_UPLOAD_BYTES + 1)
    if not content or len(content) > MAX_UPLOAD_BYTES:
        raise http_error(413, "VIRAL_UPLOAD_SIZE_INVALID", "请上传不超过 200 MB 的视频文件。")
    body = ViralUploadRequest(
        platform=platform,
        title=title.strip(),
        author=author.strip(),
        sha256=hashlib.sha256(content).hexdigest(),
        confirm=confirm,
        reason=reason,
        verified=verified,
        published_display=published_display.strip(),
        likes=likes,
        comments=comments,
        shares=shares,
        collects=collects,
        tags=tags,
    )
    require_write_contract(request, body)
    metadata = probe_video(FFprobeVideoProbe(), content, filename=file.filename or "video.mp4")
    width, height = metadata.width, metadata.height
    if not width or not height:
        raise http_error(422, "VIRAL_UPLOAD_INVALID", "文件中没有可用的视频画面。")
    try:
        normalized = normalize_generated_video(
            content,
            target_width=width + width % 2,
            target_height=height + height % 2,
            max_bytes=MAX_UPLOAD_BYTES,
            max_duration_seconds=None,
            allow_mov=True,
        )
    except MediaValidationFailed as exc:
        raise http_error(
            422, "VIRAL_UPLOAD_INVALID", "视频无法处理，请更换 MP4/MOV 文件。"
        ) from exc
    thumbnail = extract_thumbnail_jpeg(normalized.content)
    if thumbnail is None:
        raise http_error(503, "VIRAL_UPLOAD_COVER_FAILED", "视频封面未生成，请稍后重试。")
    video_id = f"upload-{body.sha256}"
    upload_tags = _parse_upload_tags(body.tags)
    published_at = _parse_published(body.published_display)

    def business(raw: psycopg.Connection, request_id: str) -> dict[str, object]:
        # 对象上传期间保留幂等锁，局部放宽池连接的 60 秒空闲护栏，事务结束自动还原。
        raw.execute("SET LOCAL idle_in_transaction_session_timeout = '300s'")
        conn = BusinessConnection.postgres(raw)
        storage = get_media_storage(conn)

        def prepare(key: str, check: Callable[[], None]) -> StoredObject:
            check()
            stored = storage.put_object(key, normalized.content, content_type="video/mp4")
            storage.put_object(thumbnail_key_for(key), thumbnail, content_type="image/jpeg")
            return stored

        # 先由既有幂等写占住请求，再产生归档副作用；转码与抽帧已在事务外完成。
        stored, _ = ViralMediaPreparation(storage=storage).fetch(
            platform=platform,
            video_id=video_id,
            kind="video",
            prepare=prepare,
        )
        cover_key = thumbnail_key_for(stored.key)
        # 封面公开副本：/api/viral/covers 与管理端封面路由都按确定性 key
        # （viral_cover_key）取对象，不读 DB cover_key。采集链路写入的就是
        # 这个位置；管理员上传此前只落了 prepared 归档同址缩略图，公开
        # 路径缺对象 → 封面 404（黑屏卡片）。这里同步写公开副本。
        storage.put_object(
            viral_cover_key(platform, video_id),
            thumbnail,
            content_type="image/jpeg",
        )
        video = ViralVideo(
            platform=platform,
            video_id=video_id,
            category="管理员上传",
            title=body.title,
            author=body.author,
            author_avatar=None,
            verified=body.verified,
            cover_url=None,
            cover_key=cover_key,
            duration_ms=round(metadata.duration_seconds * 1000),
            likes=body.likes,
            comments=body.comments,
            shares=body.shares,
            collects=body.collects,
            published_at=published_at,
            published_display=body.published_display or None,
            like_display=None,
            tags=upload_tags,
            native={"admin_upload": True},
        )
        inserted = upsert_viral_videos(conn, [video], commit=False)
        # 通用 upsert 保留旧封面；重新准备会换归档键，必须同步本次封面。
        update_viral_cover(
            conn, platform=platform, video_id=video_id, cover_key=cover_key, commit=False
        )
        record_content_source(
            conn,
            batch_id=f"upload:{request_id}",
            source_kind="admin_upload",
            platform=platform,
            video_id=video_id,
            keyword="",
            is_new=(platform, video_id) in inserted,
        )
        record_content_stage(conn, platform=platform, video_id=video_id, stage="prepared")
        # 上传即自动上架（客户列表 + 首页推荐位置顶）：媒体与封面在同一事务里
        # 已就绪，mark 守卫必然放行；唯一会拦下的是同 sha 重复上传一个已
        # 下架/已删除的视频——给明确的 409，而不是静默入库一个不可见条目。
        auto_published = auto_publish_viral_video(
            conn,
            platform=platform,
            video_id=video_id,
            actor_user_id=actor.user_id,
            reason=body.reason.strip() or "上传自动上架",
            request_id=request_id,
        )
        if not auto_published:
            raise http_error(
                409,
                "VIRAL_VIDEO_UNAVAILABLE",
                "该视频已下架或已删除，无法重新上架；请先恢复或更换文件后重试。",
            )
        raw.execute(
            "INSERT INTO audit_logs(id,actor_user_id,action,entity_type,entity_id,metadata_json) "
            "VALUES(%s,%s,'viral_video.admin_upload','viral_video',%s,%s)",
            (
                str(uuid4()),
                actor.user_id,
                f"{platform}:{video_id}",
                json.dumps({"reason": body.reason, "request_id": request_id}),
            ),
        )
        return {
            "platform": platform,
            "videoId": video_id,
            "autoPublished": auto_published,
            "request_id": request_id,
        }

    return write_with_idempotency(
        request=request,
        response=response,
        actor=actor,
        body=body,
        business=business,
        success_status=201,
    )
