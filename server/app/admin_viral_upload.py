"""管理员上传的视频直接接入既有素材归档与人工发布流程。"""

import hashlib
import json
from collections.abc import Callable
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
from app.viral_media_preparation import ViralMediaPreparation
from app.viral_store import update_viral_cover, upsert_viral_videos
from app.viral_tikhub import ViralVideo

router = APIRouter()


class ViralUploadRequest(AdminWriteContract):
    platform: Literal["douyin", "wechat_channels"]
    title: str = Field(min_length=1, max_length=200)
    author: str = Field(max_length=120)
    sha256: str


@router.post("/viral/uploads", status_code=201)
def upload_viral_video(
    request: Request,
    response: Response,
    actor: ContentWriter,
    file: UploadFile,
    platform: Annotated[Literal["douyin", "wechat_channels"], Form()],
    title: Annotated[str, Form(min_length=1, max_length=200)],
    author: Annotated[str, Form(max_length=120)] = "",
    confirm: Annotated[bool, Form()] = False,
    reason: Annotated[str, Form()] = "",
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
        video = ViralVideo(
            platform=platform,
            video_id=video_id,
            category="管理员上传",
            title=body.title,
            author=body.author,
            author_avatar=None,
            verified=False,
            cover_url=None,
            cover_key=cover_key,
            duration_ms=round(metadata.duration_seconds * 1000),
            likes=None,
            comments=None,
            shares=None,
            collects=None,
            published_at=None,
            published_display=None,
            like_display=None,
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
        return {"platform": platform, "videoId": video_id, "request_id": request_id}

    return write_with_idempotency(
        request=request,
        response=response,
        actor=actor,
        body=body,
        business=business,
        success_status=201,
    )
