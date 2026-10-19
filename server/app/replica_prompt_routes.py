"""长视频复刻提示词（replica_prompt）HTTP 路由.

与 /analysis-tasks 同构：入队请求只做校验与持久化，禁止任何 provider /
storage 网络调用（付费调用在 worker 事务外）；轮询走只读任务端点。
"""

from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from app.analysis_routes import get_video_analysis_provider
from app.auth import AuthenticatedUser, Database
from app.customer_fence import BusinessDbDep
from app.ops_metrics import get_or_create_request_id
from app.permissions import (
    remap_security_denial,
    require_asset_access,
    require_project_access,
    write_audit,
)
from app.replica_prompt import (
    assert_replica_prompt_enqueue_allowed,
    cancel_replica_prompt_task,
    enqueue_replica_prompt_task,
    find_latest_replica_prompt_task,
    find_latest_replica_prompt_version,
    load_replica_prompt_task,
)

router = APIRouter(prefix="/api", tags=["replica_prompt"])


class CreateReplicaPromptTaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset_id: str = Field(min_length=1)
    reuse_existing: bool = True


class ReplicaPromptTaskResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    project_id: str
    asset_id: str
    status: str
    attempt: int
    result_version_id: str | None
    error_code: str | None
    error_message: str | None
    failure_phase: str | None
    retryable: bool
    request_id: str | None
    created_at: str
    updated_at: str
    started_at: str | None
    completed_at: str | None


class ReplicaPromptVersionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    project_id: str
    asset_id: str | None
    kind: str
    version_number: int
    payload: dict[str, Any]
    created_by_user_id: str | None
    created_at: str


def replica_prompt_task_response(row: sqlite3.Row) -> ReplicaPromptTaskResponse:
    return ReplicaPromptTaskResponse(
        id=str(row["id"]),
        project_id=str(row["project_id"]),
        asset_id=str(row["asset_id"]),
        status=str(row["status"]),
        attempt=int(row["attempt"]),
        result_version_id=(
            None if row["result_version_id"] is None else str(row["result_version_id"])
        ),
        error_code=None if row["error_code"] is None else str(row["error_code"]),
        error_message=(
            None if row["error_message_redacted"] is None else str(row["error_message_redacted"])
        ),
        failure_phase=None if row["failure_phase"] is None else str(row["failure_phase"]),
        retryable=bool(row["retryable"]),
        request_id=None if row["request_id"] is None else str(row["request_id"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        started_at=None if row["started_at"] is None else str(row["started_at"]),
        completed_at=None if row["completed_at"] is None else str(row["completed_at"]),
    )


def _measured_asset_duration(asset: sqlite3.Row) -> float:
    import json

    try:
        metadata = json.loads(str(asset["metadata_json"] or "{}"))
    except (json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "REPLICA_PROMPT_DURATION_UNAVAILABLE",
                "message": "参考视频时长不可用，请重新上传。",
            },
        ) from exc
    stored = metadata.get("duration_seconds")
    if not isinstance(stored, int | float) or isinstance(stored, bool):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "REPLICA_PROMPT_DURATION_UNAVAILABLE",
                "message": "参考视频时长不可用，请重新上传。",
            },
        )
    return float(stored)


@router.post(
    "/projects/{project_id}/replica-prompt-tasks",
    response_model=ReplicaPromptTaskResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def create_project_replica_prompt_task(
    project_id: str,
    request: CreateReplicaPromptTaskRequest,
    db: BusinessDbDep,
    http_request: Request,
) -> ReplicaPromptTaskResponse:
    """校验并持久化长视频拆解任务；本请求内禁止 provider/storage 网络调用."""
    request_id = get_or_create_request_id(http_request)
    with db.write() as (conn, actor):
        require_project_access(
            conn,
            actor=actor,
            project_id=project_id,
            action="replica_prompt.task.create",
        )
        asset = require_asset_access(
            conn,
            actor=actor,
            asset_id=request.asset_id,
            action="replica_prompt.task.create",
        )
        if str(asset["project_id"]) != project_id:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "ASSET_PROJECT_MISMATCH",
                    "message": "Asset does not belong to the requested project.",
                },
            )
        assert_replica_prompt_enqueue_allowed(
            asset, measured_duration=_measured_asset_duration(asset)
        )
        if request.reuse_existing:
            existing = find_latest_replica_prompt_task(
                conn, project_id=project_id, asset_id=request.asset_id
            )
            if existing is not None:
                return replica_prompt_task_response(existing)
        provider = get_video_analysis_provider(conn)
        if provider.requires_https_video_url and str(asset["storage_uri"]).startswith("local://"):
            raise HTTPException(
                503,
                detail={
                    "code": "REPLICA_PROMPT_VIDEO_URL_UNAVAILABLE",
                    "message": "当前视频位于本地，云端分析无法读取。请配置云端素材存储后重新上传。",
                },
            )
        row, created = enqueue_replica_prompt_task(
            conn,
            project_id=project_id,
            asset_id=request.asset_id,
            created_by_user_id=actor.id,
            duration_seconds=_measured_asset_duration(asset),
            request_id=request_id,
        )
        if not created:
            return replica_prompt_task_response(row)
        write_audit(
            conn,
            actor=actor,
            action="replica_prompt.task_enqueued",
            entity_type="replica_prompt_task",
            entity_id=str(row["id"]),
            metadata={
                "project_id": project_id,
                "asset_id": request.asset_id,
                "asset_sha256": str(asset["sha256"]),
                "request_id": request_id,
            },
        )
        return replica_prompt_task_response(row)


@router.get("/replica-prompt-tasks/{task_id}", response_model=ReplicaPromptTaskResponse)
def read_replica_prompt_task(
    task_id: str,
    conn: Database,
    actor: AuthenticatedUser,
) -> ReplicaPromptTaskResponse:
    row = load_replica_prompt_task(conn, task_id)
    try:
        require_project_access(
            conn,
            actor=actor,
            project_id=str(row["project_id"]),
            action="replica_prompt.task.read",
        )
    except HTTPException as exc:
        if exc.status_code == 404:
            raise remap_security_denial(
                exc,
                status_code=404,
                detail={"code": "REPLICA_PROMPT_TASK_NOT_FOUND"},
            ) from exc
        raise
    return replica_prompt_task_response(row)


@router.get("/projects/{project_id}/replica-prompt-tasks/latest")
def read_latest_replica_prompt_task(
    project_id: str,
    conn: Database,
    actor: AuthenticatedUser,
) -> ReplicaPromptTaskResponse | None:
    """项目内最新一次长视频拆解任务；没有历史时返回 null（前端据此判空）."""
    require_project_access(
        conn,
        actor=actor,
        project_id=project_id,
        action="replica_prompt.task.read",
    )
    row = conn.execute(
        """
        SELECT * FROM replica_prompt_tasks
        WHERE project_id = %s
        ORDER BY created_at DESC, id DESC
        LIMIT 1
        """,
        (project_id,),
    ).fetchone()
    return None if row is None else replica_prompt_task_response(row)


@router.post("/replica-prompt-tasks/{task_id}/cancel", response_model=ReplicaPromptTaskResponse)
def cancel_project_replica_prompt_task(
    task_id: str,
    db: BusinessDbDep,
) -> ReplicaPromptTaskResponse:
    with db.write() as (conn, actor):
        row = cancel_replica_prompt_task(conn, task_id=task_id, actor=actor)
        return replica_prompt_task_response(row)


@router.get("/projects/{project_id}/replica-prompt/latest")
def read_latest_replica_prompt_result(
    project_id: str,
    conn: Database,
    actor: AuthenticatedUser,
) -> ReplicaPromptVersionResponse | None:
    """项目内最新一份长视频拆解结果；没有时返回 null."""
    require_project_access(
        conn,
        actor=actor,
        project_id=project_id,
        action="replica_prompt.read",
    )
    row = find_latest_replica_prompt_version(conn, project_id=project_id)
    if row is None:
        return None
    return ReplicaPromptVersionResponse(
        id=str(row["id"]),
        project_id=str(row["project_id"]),
        asset_id=None if row["asset_id"] is None else str(row["asset_id"]),
        kind=str(row["kind"]),
        version_number=int(row["version_number"]),
        payload=_version_payload(row),
        created_by_user_id=(
            None if row["created_by_user_id"] is None else str(row["created_by_user_id"])
        ),
        created_at=str(row["created_at"]),
    )


def _version_payload(row: sqlite3.Row) -> dict[str, Any]:
    import json

    try:
        payload = json.loads(str(row["payload_json"]))
    except (json.JSONDecodeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}
