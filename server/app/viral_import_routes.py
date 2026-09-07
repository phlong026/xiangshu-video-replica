"""HTTP routes for durable viral-video project imports."""

from typing import Annotated, Literal

from fastapi import APIRouter, Header, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from app.auth import AuthenticatedUser, Database
from app.customer_fence import BusinessDbDep
from app.permissions import require_not_auditor
from app.viral_import import (
    ViralImportRequest,
    ViralImportTaskResponse,
    enqueue_viral_import_task,
    load_viral_import_task,
    viral_import_task_response,
)
from app.viral_link import (
    ViralLinkError,
    douyidou_link_client_from_settings,
    normalize_supported_link,
)
from app.viral_routes import ViralVideoItem
from app.viral_store import upsert_viral_videos
from app.viral_tikhub import ViralVideo

router = APIRouter(prefix="/api/viral", tags=["viral"])


class ViralLinkResolutionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = Field(min_length=1, max_length=2000)
    purpose: Literal["copy", "replica"]


class ViralLinkResolutionResponse(BaseModel):
    item: ViralVideoItem


@router.post("/link-resolutions", response_model=ViralLinkResolutionResponse)
def resolve_viral_link(
    request: ViralLinkResolutionRequest,
    db: BusinessDbDep,
) -> ViralLinkResolutionResponse:
    try:
        normalized_url = normalize_supported_link(request.url)
        with db.write() as (conn, actor):
            require_not_auditor(
                conn,
                actor=actor,
                action="viral.link.resolve",
                entity_type="viral_link",
                entity_id="douyin",
            )
            resolver = douyidou_link_client_from_settings(conn)
        resolved = resolver.resolve(normalized_url, purpose=request.purpose)
    except ViralLinkError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code, "message": exc.message},
        ) from exc

    video = ViralVideo(
        platform=resolved.platform,
        video_id=resolved.video_id,
        category="链接导入",
        title=resolved.title,
        author=resolved.author,
        author_avatar=None,
        verified=False,
        cover_url=resolved.cover_url,
        duration_ms=resolved.duration_ms,
        likes=0,
        comments=None,
        shares=None,
        collects=None,
        published_at=None,
        published_display=None,
        like_display=None,
        tags=[],
        play_url=resolved.video_url,
        audio_url=resolved.audio_url,
        native={
            "source_audio_verified": bool(resolved.audio_url),
            "source_description": resolved.source_description,
            "link_resolved": True,
        },
    )
    with db.write() as (conn, _actor):
        upsert_viral_videos(conn, [video])
    return ViralLinkResolutionResponse(item=ViralVideoItem(**video.to_client_dict()))


@router.post(
    "/import-tasks",
    response_model=ViralImportTaskResponse,
    status_code=status.HTTP_202_ACCEPTED,
    include_in_schema=False,
)
@router.post(
    "/videos/import-tasks",
    response_model=ViralImportTaskResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def create_viral_import_task(
    request: ViralImportRequest,
    db: BusinessDbDep,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> ViralImportTaskResponse:
    key = (idempotency_key or "").strip()
    if not key or len(key) > 128:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "VIRAL_IMPORT_IDEMPOTENCY_KEY_REQUIRED",
                "message": "Idempotency-Key 请求头无效。",
            },
        )
    with db.write() as (conn, actor):
        with conn:
            row = enqueue_viral_import_task(conn, actor=actor, request=request, idempotency_key=key)
        return viral_import_task_response(row)


@router.get("/import-tasks/{task_id}", response_model=ViralImportTaskResponse)
def read_viral_import_task(
    task_id: str, conn: Database, actor: AuthenticatedUser
) -> ViralImportTaskResponse:
    row = load_viral_import_task(conn, task_id)
    if str(row["owner_user_id"]) != actor.id:
        raise HTTPException(
            status_code=404,
            detail={"code": "VIRAL_IMPORT_TASK_NOT_FOUND", "message": "导入任务不存在。"},
        )
    return viral_import_task_response(row)
