"""首页和提取后继续拆解共用的服务端派发；读取动作不会创建付费任务。"""

from __future__ import annotations

import hashlib
import json
import math
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.analysis import enqueue_analysis_task, find_latest_analysis_task
from app.analysis_routes import (
    CreateAnalysisRequest,
    get_video_analysis_provider,
    validate_analysis_enqueue,
)
from app.auth import CurrentUser, ReadDatabase, ShortAuthUser
from app.customer_fence import BusinessDbDep
from app.db_portable import BusinessConnection, BusinessRow
from app.media import validate_duration, validate_upload_request
from app.permissions import require_asset_access, require_not_auditor, require_project_access
from app.replica_prompt import (
    assert_replica_prompt_enqueue_allowed,
    enqueue_replica_prompt_task,
    find_latest_replica_prompt_task,
)

router = APIRouter(prefix="/api/projects", tags=["source-analysis"])


class SourceAnalysisRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_id: str = Field(min_length=1, max_length=128)
    source_revision: int = Field(ge=1)
    idempotency_key: str = Field(min_length=8, max_length=128)
    reanalyze: bool = False


class SourceAnalysisResponse(BaseModel):
    id: str
    source_id: str
    status: str
    route: str | None
    task_id: str | None


def _response(row: BusinessRow) -> SourceAnalysisResponse:
    return SourceAnalysisResponse(**{key: row[key] for key in SourceAnalysisResponse.model_fields})


def dispatch_source_analysis(
    conn: BusinessConnection, *, actor: CurrentUser, project_id: str, request: SourceAnalysisRequest
) -> SourceAnalysisResponse:
    require_not_auditor(
        conn, actor=actor, action="source.analyze", entity_type="project", entity_id=project_id
    )
    require_project_access(conn, actor=actor, project_id=project_id, action="source.analyze")
    project = conn.execute(
        "SELECT current_source_id FROM projects WHERE id=%s FOR UPDATE", (project_id,)
    ).fetchone()
    fingerprint = hashlib.sha256(request.model_dump_json().encode()).hexdigest()
    action = conn.execute(
        "SELECT * FROM source_analysis_actions WHERE project_id=%s AND owner_user_id=%s AND idempotency_key=%s",
        (project_id, actor.id, request.idempotency_key),
    ).fetchone()
    if action is not None:
        if action["request_hash"] != fingerprint:
            raise HTTPException(
                409,
                detail={
                    "code": "SOURCE_ACTION_CONFLICT",
                    "message": "来源或分析要求已变化，请重新提交。",
                },
            )
        if action["status"] == "DISPATCHED":
            return _response(action)
    source = conn.execute(
        "SELECT * FROM project_sources WHERE id=%s AND project_id=%s FOR UPDATE",
        (request.source_id, project_id),
    ).fetchone()
    if source is None or project is None or project["current_source_id"] != request.source_id:
        raise HTTPException(
            409, detail={"code": "SOURCE_REPLACED", "message": "当前来源已变化，请刷新后重试。"}
        )
    revision = int(source["revision"])
    continuation = (
        action is not None
        and action["status"] == "NEEDS_ORIGINAL_UPLOAD"
        and revision == request.source_revision + 1
        and source["original_video_asset_id"] is not None
    )
    if revision != request.source_revision and not continuation:
        raise HTTPException(
            409,
            detail={
                "code": "SOURCE_REVISION_CONFLICT",
                "message": "来源版本已变化，请刷新后重试。",
            },
        )
    route, task_id = None, None
    status = "NEEDS_ORIGINAL_UPLOAD"
    asset_id = source["original_video_asset_id"]
    if asset_id is not None:
        asset = require_asset_access(
            conn, actor=actor, asset_id=str(asset_id), action="source.analyze"
        )
        metadata = json.loads(str(asset["metadata_json"] or "{}"))
        duration = float(metadata.get("duration_seconds") or 0)
        if not math.isfinite(duration) or duration < 3.9:
            raise HTTPException(
                422,
                detail={
                    "code": "SOURCE_DURATION_INVALID",
                    "message": "来源时长不可用，请重新上传。",
                },
            )
        if duration > 180.1:
            status = "NEEDS_RANGE_SELECTION"
        else:
            route = "replica" if duration <= 30.1 else "replica_prompt"
            validate_duration(duration, purpose=route)
            validate_upload_request(
                filename=str(source["filename"]),
                content_type=str(asset["content_type"]),
                size_bytes=int(asset["size_bytes"]),
                purpose=route,
            )
            if route == "replica":
                validate_analysis_enqueue(
                    conn,
                    actor=actor,
                    project_id=project_id,
                    request=CreateAnalysisRequest(asset_id=str(asset_id)),
                )
            else:
                assert_replica_prompt_enqueue_allowed(asset, measured_duration=duration)
            existing = (
                find_latest_analysis_task if route == "replica" else find_latest_replica_prompt_task
            )(conn, project_id=project_id, asset_id=str(asset_id))
            if existing is not None and not request.reanalyze:
                task_id = str(existing["id"])
            else:
                provider = get_video_analysis_provider(
                    conn, lane="analysis" if route == "replica" else "replica_prompt"
                )
                if provider.requires_https_video_url and str(asset["storage_uri"]).startswith(
                    "local://"
                ):
                    raise HTTPException(
                        503,
                        detail={
                            "code": "SOURCE_VIDEO_URL_UNAVAILABLE",
                            "message": "请配置云端素材存储后进行拆解。",
                        },
                    )
                enqueue = (
                    enqueue_analysis_task if route == "replica" else enqueue_replica_prompt_task
                )
                task, _ = enqueue(
                    conn,
                    project_id=project_id,
                    asset_id=str(asset_id),
                    created_by_user_id=actor.id,
                    duration_seconds=duration,
                )
                task_id = str(task["id"])
            status = "DISPATCHED"
    if action is None:
        action = conn.execute(
            "INSERT INTO source_analysis_actions(id,project_id,source_id,source_revision,owner_user_id,idempotency_key,request_hash,status,route,task_id) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
            (
                str(uuid4()),
                project_id,
                request.source_id,
                request.source_revision,
                actor.id,
                request.idempotency_key,
                fingerprint,
                status,
                route,
                task_id,
            ),
        ).fetchone()
    else:
        action = conn.execute(
            "UPDATE source_analysis_actions SET status=%s,route=%s,task_id=%s,updated_at=now() WHERE id=%s RETURNING *",
            (status, route, task_id, action["id"]),
        ).fetchone()
    return _response(action)


@router.post("/{project_id}/source-analysis", response_model=SourceAnalysisResponse)
def analyze_source(
    project_id: str, request: SourceAnalysisRequest, db: BusinessDbDep
) -> SourceAnalysisResponse:
    with db.write() as (conn, actor):
        return dispatch_source_analysis(conn, actor=actor, project_id=project_id, request=request)


@router.get("/{project_id}/source-analysis/{action_id}", response_model=SourceAnalysisResponse)
def read_source_action(
    project_id: str, action_id: str, conn: ReadDatabase, actor: ShortAuthUser
) -> SourceAnalysisResponse:
    require_project_access(conn, actor=actor, project_id=project_id, action="source.analyze_read")
    row = conn.execute(
        "SELECT * FROM source_analysis_actions WHERE id=%s AND project_id=%s AND owner_user_id=%s",
        (action_id, project_id, actor.id),
    ).fetchone()
    if row is None:
        raise HTTPException(
            404, detail={"code": "SOURCE_ACTION_MISSING", "message": "来源分析动作不存在。"}
        )
    return _response(row)
