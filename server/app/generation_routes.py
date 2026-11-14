from __future__ import annotations

import base64
import hashlib
import json
import logging
import math
from typing import Any, Literal, cast
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel

from app.auth import AuthenticatedUser, CurrentUser, Database
from app.customer_fence import BusinessDbDep
from app.db_pg import pg_transaction
from app.db_portable import BusinessConnection, BusinessRow
from app.generation import (
    RESULT_URL_EXPIRED_STATUSES,
    STRICT_DURATION_POLICY,
    ApplySavedPromptRequest,
    BatchResult,
    BatchStatusFilter,
    ConfirmNotChargedRequest,
    FakeH3Provider,
    GenerationBatchListPage,
    GenerationBatchRenameRequest,
    GenerationBatchRequest,
    GenerationPriceQuote,
    GenerationRuntimeLimits,
    GenerationTaskRetryRequest,
    H3Provider,
    H3ProviderFailed,
    H3ProviderSettingsUnavailable,
    PaidRegenerationRequest,
    PromptCompileRequest,
    PromptPreviewRequest,
    PromptPreviewResult,
    PromptRevisionRequest,
    ReconcileGenerationTaskRequest,
    SavedPromptRequest,
    ScriptRequest,
    TaskResult,
    VersionResult,
    VersionState,
    apply_saved_prompt,
    cancel_generation_batch,
    claim_generation_result_archive,
    compile_prompt_version,
    confirm_generation_task_not_charged,
    create_generation_batch,
    create_script_version,
    enqueue_generation_reconcile_operation,
    generation_price_quote,
    generation_regeneration_price_quote,
    generation_result_archive_ready,
    generation_runtime_limits,
    get_generation_batch,
    get_task_result,
    h3_provider_for_task,
    latest_generation_reconcile_operation,
    list_generation_batches,
    list_saved_prompts,
    load_generation_reconcile_operation,
    lock_prompt_version,
    persist_generation_result_archive,
    preview_prompt_text,
    provider_result_url_expired,
    provider_result_url_needs_refresh,
    regenerate_generation_batch,
    regenerate_generation_task,
    release_generation_result_archive_claim,
    rename_generation_batch,
    renew_generation_result_archive_claim,
    require_batch_access,
    requires_strict_generation_duration,
    result_url_expired_error,
    retry_generation_task,
    revise_prompt_version,
    save_prompt_to_library,
    version_result,
    version_state,
)
from app.material_thumbs import store_video_thumbnail
from app.media import (
    MAX_UPLOAD_BYTES,
    FFprobeVideoProbe,
    VideoProbeFailed,
    VideoProbeUnavailable,
    storage_key_from_uri,
)
from app.media_routes import MediaStorage, storage_for_asset
from app.media_tools import (
    MediaToolFailed,
    MediaToolUnavailable,
    MediaValidationFailed,
    normalize_generated_video,
)
from app.permissions import (
    require_asset_access,
    require_not_auditor,
    require_project_access,
    require_role,
    write_audit,
)
from app.script_rewrite import (
    ScriptRewriteRequest,
    ScriptRewriteResult,
    _canonical_json,
    _validated_ip_profile_snapshot,
    enqueue_script_rewrite_task,
    latest_script_rewrite_task,
    load_script_rewrite_task,
    require_current_script_rewrite_source,
    require_owned_script_rewrite_identity,
    script_rewrite_task_result,
    validated_script_rewrite_request,
)
from app.storage import StorageAdapter, StorageBackendUnavailable

router = APIRouter(prefix="/api", tags=["generation"])
GENERATION_RESULT_MAX_DURATION_SECONDS = 60
logger = logging.getLogger(__name__)


class ScriptRewriteIpProfileSummary(BaseModel):
    display_name: str
    role: str
    service_scope: str
    target_audience: str
    expression_style: str
    audience_needs: str = ""
    factual_background: str = ""
    sample_script: str = ""
    forbidden_claims: str = ""
    profile_version: int


class ScriptRewriteTaskResponse(BaseModel):
    id: str
    project_id: str
    identity_id: str | None
    ip_profile_hash: str | None
    ip_profile_snapshot: ScriptRewriteIpProfileSummary | None
    source_asset_id: str | None
    source_text: str
    instructions: str = ""
    status: str
    attempt: int
    result: ScriptRewriteResult | None
    error_code: str | None
    error_message: str | None
    retryable: bool
    created_at: str
    updated_at: str
    started_at: str | None
    completed_at: str | None


class GenerationReconcileOperationResponse(BaseModel):
    id: str
    task_id: str
    status: str
    attempt: int
    error_code: str | None
    error_message: str | None
    retryable: bool
    created_at: str
    updated_at: str
    started_at: str | None
    completed_at: str | None


class GenerationTaskPreviewUrlResponse(BaseModel):
    url: str


def get_h3_provider() -> H3Provider:
    return FakeH3Provider()


@router.post("/projects/{project_id}/scripts", response_model=VersionResult)
def create_project_script(
    project_id: str,
    request: ScriptRequest,
    db: BusinessDbDep,
) -> VersionResult:
    with db.write() as (conn, actor):
        row = create_script_version(conn, project_id=project_id, actor=actor, request=request)
    return version_result(row)


@router.post(
    "/projects/{project_id}/script-rewrite",
    response_model=ScriptRewriteTaskResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def rewrite_project_script(
    project_id: str,
    request: ScriptRewriteRequest,
    db: BusinessDbDep,
) -> ScriptRewriteTaskResponse:
    with db.write() as (conn, actor):
        row = enqueue_script_rewrite_task(
            conn,
            actor=actor,
            project_id=project_id,
            source_text=request.text,
            idempotency_key=request.idempotency_key or str(uuid4()),
            identity_id=request.identity_id,
            source_asset_id=request.source_asset_id,
            instructions=request.instructions,
        )
        return script_rewrite_task_response(row)


@router.get(
    "/script-rewrite-tasks/{task_id}",
    response_model=ScriptRewriteTaskResponse,
)
def read_script_rewrite_task(
    task_id: str,
    conn: Database,
    actor: AuthenticatedUser,
) -> ScriptRewriteTaskResponse:
    row = load_script_rewrite_task(conn, task_id)
    require_project_access(
        conn,
        actor=actor,
        project_id=str(row["project_id"]),
        action="project.script_rewrite_read",
    )
    return script_rewrite_task_response(row)


@router.get(
    "/projects/{project_id}/script-rewrite-tasks/latest",
    response_model=ScriptRewriteTaskResponse | None,
)
def read_latest_script_rewrite_task(
    project_id: str,
    conn: Database,
    actor: AuthenticatedUser,
    identity_scope: Literal["all", "identity", "none"] = Query(default="all"),
    identity_id: str | None = Query(default=None, min_length=1, max_length=128),
    source_asset_id: str | None = Query(default=None, min_length=1, max_length=128),
) -> ScriptRewriteTaskResponse | None:
    require_project_access(
        conn,
        actor=actor,
        project_id=project_id,
        action="project.script_rewrite_read",
    )
    if identity_scope == "identity" and identity_id is None:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "SCRIPT_REWRITE_IDENTITY_REQUIRED",
                "message": "按人物查询时必须提供 identity_id。",
            },
        )
    if identity_scope != "identity" and identity_id is not None:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "SCRIPT_REWRITE_IDENTITY_SCOPE_INVALID",
                "message": "identity_id 仅可用于 identity 查询范围。",
            },
        )
    if identity_scope == "identity":
        assert identity_id is not None
        require_owned_script_rewrite_identity(
            conn,
            actor=actor,
            identity_id=identity_id,
        )
    if source_asset_id is not None:
        require_current_script_rewrite_source(
            conn,
            actor=actor,
            project_id=project_id,
            source_asset_id=source_asset_id,
        )
    row = latest_script_rewrite_task(
        conn,
        project_id=project_id,
        identity_id=identity_id,
        identity_scope=identity_scope,
    )
    if row is not None and source_asset_id is not None:
        try:
            request = validated_script_rewrite_request(row)
        except ValueError as exc:
            raise _script_rewrite_request_integrity_error(row["id"], exc) from exc
        if request.source_asset_id != source_asset_id:
            return None
    return None if row is None else script_rewrite_task_response(row)


def script_rewrite_task_response(row: BusinessRow) -> ScriptRewriteTaskResponse:
    snapshot = _script_rewrite_profile_summary(
        row["ip_profile_snapshot_json"],
        task_id=str(row["id"]),
        identity_id=None if row["identity_id"] is None else str(row["identity_id"]),
        expected_hash=None if row["ip_profile_hash"] is None else str(row["ip_profile_hash"]),
    )
    try:
        request = validated_script_rewrite_request(row)
    except ValueError as exc:
        raise _script_rewrite_request_integrity_error(row["id"], exc) from exc
    return ScriptRewriteTaskResponse(
        id=str(row["id"]),
        project_id=str(row["project_id"]),
        identity_id=None if row["identity_id"] is None else str(row["identity_id"]),
        ip_profile_hash=(None if row["ip_profile_hash"] is None else str(row["ip_profile_hash"])),
        ip_profile_snapshot=snapshot,
        source_asset_id=request.source_asset_id,
        source_text=request.source_text,
        instructions=request.instructions,
        status=str(row["status"]),
        attempt=int(row["attempt"]),
        result=script_rewrite_task_result(row),
        error_code=None if row["error_code"] is None else str(row["error_code"]),
        error_message=(
            None if row["error_message_redacted"] is None else str(row["error_message_redacted"])
        ),
        retryable=bool(row["retryable"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        started_at=None if row["started_at"] is None else str(row["started_at"]),
        completed_at=(None if row["completed_at"] is None else str(row["completed_at"])),
    )


def _script_rewrite_request_integrity_error(task_id: object, cause: Exception) -> HTTPException:
    logger.error("Script rewrite task %s has invalid request payload: %s", task_id, cause)
    return HTTPException(
        status_code=500,
        detail={
            "code": "SCRIPT_REWRITE_REQUEST_INTEGRITY_ERROR",
            "message": "改写任务原文校验失败，请重新提交。",
        },
    )


def _script_rewrite_profile_summary(
    value: object,
    *,
    task_id: str,
    identity_id: str | None,
    expected_hash: str | None,
) -> ScriptRewriteIpProfileSummary | None:
    if value is None and identity_id is None and expected_hash is None:
        return None
    try:
        snapshot = json.loads(str(value))
        validated = _validated_ip_profile_snapshot(snapshot)
        actual_hash = hashlib.sha256(_canonical_json(validated).encode("utf-8")).hexdigest()
        if validated["identity_id"] != identity_id or actual_hash != expected_hash:
            raise ValueError("snapshot identity or hash mismatch")
        return ScriptRewriteIpProfileSummary.model_validate(validated)
    except (json.JSONDecodeError, TypeError, ValueError):
        logger.error("Script rewrite task %s has an invalid IP profile snapshot", task_id)
        raise HTTPException(
            status_code=500,
            detail={
                "code": "SCRIPT_REWRITE_SNAPSHOT_INTEGRITY_ERROR",
                "message": "改写任务的人物档案快照完整性校验失败。",
            },
        ) from None


@router.get("/projects/{project_id}/scripts/latest", response_model=VersionState)
def read_latest_project_script(
    project_id: str,
    conn: Database,
    actor: AuthenticatedUser,
) -> VersionState:
    return version_state(
        conn,
        project_id=project_id,
        actor=actor,
        kind="script",
    )


@router.post("/projects/{project_id}/prompts/compile", response_model=VersionResult)
def compile_project_prompt(
    project_id: str,
    request: PromptCompileRequest,
    db: BusinessDbDep,
) -> VersionResult:
    with db.write() as (conn, actor):
        row = compile_prompt_version(conn, project_id=project_id, actor=actor, request=request)
    return version_result(row)


@router.post("/projects/{project_id}/prompts/preview", response_model=PromptPreviewResult)
def preview_project_prompt(
    project_id: str,
    request: PromptPreviewRequest,
    conn: Database,
    actor: AuthenticatedUser,
) -> PromptPreviewResult:
    return preview_prompt_text(
        conn,
        project_id=project_id,
        actor=actor,
        request=request,
    )


@router.post("/projects/{project_id}/prompts/revise", response_model=VersionResult)
def revise_project_prompt(
    project_id: str,
    request: PromptRevisionRequest,
    db: BusinessDbDep,
) -> VersionResult:
    with db.write() as (conn, actor):
        row = revise_prompt_version(
            conn,
            project_id=project_id,
            actor=actor,
            request=request,
        )
    return version_result(row)


@router.post("/projects/{project_id}/saved-prompts", response_model=VersionResult)
def create_saved_prompt(
    project_id: str,
    request: SavedPromptRequest,
    db: BusinessDbDep,
) -> VersionResult:
    with db.write() as (conn, actor):
        row = save_prompt_to_library(
            conn,
            project_id=project_id,
            actor=actor,
            request=request,
        )
    return version_result(row)


@router.get("/projects/{project_id}/saved-prompts", response_model=list[VersionResult])
def read_saved_prompts(
    project_id: str,
    conn: Database,
    actor: AuthenticatedUser,
) -> list[VersionResult]:
    return [
        version_result(row) for row in list_saved_prompts(conn, project_id=project_id, actor=actor)
    ]


# ========== 新增：H3 提示词预览编译 API（前端实时使用） ==========


class CompileH3PreviewRequest(BaseModel):
    """前端用于实时预览 H3 提示词编译结果"""

    shots: list[dict[str, Any]]  # ShotCard JSON
    original_script: str
    analysis_version_id: str


class CompileH3PreviewResult(BaseModel):
    h3_prompt_text: str


@router.post(
    "/projects/{project_id}/prompts/compile-h3-preview",
    response_model=CompileH3PreviewResult,
)
def compile_h3_preview(
    project_id: str,
    request: CompileH3PreviewRequest,
    conn: Database,
    actor: AuthenticatedUser,
) -> CompileH3PreviewResult:
    """
    轻量级 H3 提示词编译，用于前端实时预览。
    不写入数据库，只返回编译后的文本。

    参数:
    - project_id: 项目 ID
    - request.shots: 分镜卡数据
    - request.original_script: 原始脚本
    - request.analysis_version_id: 分析版本 ID

    返回:
    - h3_prompt_text: 编译后的完整 H3 提示词
    """
    try:
        from app.generation import compile_prompt_text

        # 解析 shots 数据
        shot_payload = {
            "shots": request.shots,
            "duration_seconds": None,  # 将由 compile_prompt_text 自动计算
        }

        script_payload = {
            "full_text": request.original_script,
            "shot_mappings": [],
        }

        # 手动计算时间线长度
        source_duration_seconds = 0.0
        for shot in request.shots:
            end_time = float(shot.get("end_time", 0))
            if end_time > source_duration_seconds:
                source_duration_seconds = end_time

        # 调用核心编译函数
        prompt_text = compile_prompt_text(
            script_payload=dict(script_payload),
            shot_payload=dict(shot_payload),
            source_duration_seconds=source_duration_seconds,
            duration_seconds=15,  # 默认 15 秒
            resolution="768P",  # 默认分辨率
        )

        return CompileH3PreviewResult(h3_prompt_text=prompt_text)

    except Exception as e:
        logging.error(f"Failed to compile H3 preview: {e}")
        raise HTTPException(status_code=500, detail=f"编译 H3 提示词失败：{str(e)}")


@router.post(
    "/projects/{project_id}/saved-prompts/{saved_prompt_id}/apply",
    response_model=VersionResult,
)
def apply_project_saved_prompt(
    project_id: str,
    saved_prompt_id: str,
    request: ApplySavedPromptRequest,
    db: BusinessDbDep,
) -> VersionResult:
    with db.write() as (conn, actor):
        row = apply_saved_prompt(
            conn,
            project_id=project_id,
            saved_prompt_id=saved_prompt_id,
            actor=actor,
            request=request,
        )
    return version_result(row)


@router.get("/projects/{project_id}/prompts/latest", response_model=VersionState)
def read_latest_project_prompt(
    project_id: str,
    conn: Database,
    actor: AuthenticatedUser,
) -> VersionState:
    return version_state(
        conn,
        project_id=project_id,
        actor=actor,
        kind="h3_prompt",
    )


@router.post(
    "/projects/{project_id}/prompts/{prompt_version_id}/lock",
    response_model=VersionResult,
)
def lock_project_prompt(
    project_id: str,
    prompt_version_id: str,
    db: BusinessDbDep,
) -> VersionResult:
    with db.write() as (conn, actor):
        row = lock_prompt_version(
            conn,
            project_id=project_id,
            prompt_version_id=prompt_version_id,
            actor=actor,
        )
    return version_result(row)


@router.post("/projects/{project_id}/generation-batches", response_model=BatchResult)
def create_project_generation_batch(
    project_id: str,
    db: BusinessDbDep,
    request: GenerationBatchRequest | None = None,
    provider: H3Provider = Depends(get_h3_provider),
) -> BatchResult:
    with db.write() as (conn, actor):
        if request is None:
            if actor.role == "auditor":
                raise HTTPException(status_code=403, detail={"code": "ROLE_FORBIDDEN"})
            raise HTTPException(
                status_code=422,
                detail={"code": "GENERATION_REQUEST_REQUIRED"},
            )
        return create_generation_batch(
            conn,
            project_id=project_id,
            actor=actor,
            request=request,
            provider_client=provider,
        )


@router.get("/generation-batches", response_model=GenerationBatchListPage)
def list_generation_batch_records(
    conn: Database,
    actor: AuthenticatedUser,
    project_id: str | None = Query(default=None, min_length=1, max_length=128),
    created_by_user_id: str | None = Query(default=None, min_length=1, max_length=128),
    status: BatchStatusFilter | None = Query(default=None),
    needs_attention: bool | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    cursor: str | None = Query(default=None, min_length=1, max_length=512),
) -> GenerationBatchListPage:
    return list_generation_batches(
        conn,
        actor=actor,
        project_id=project_id,
        created_by_user_id=created_by_user_id,
        status=status,
        needs_attention=needs_attention,
        limit=limit,
        cursor=cursor,
    )


@router.get("/generation-batches/{batch_id}", response_model=BatchResult)
def read_generation_batch(
    batch_id: str,
    conn: Database,
    actor: AuthenticatedUser,
) -> BatchResult:
    return get_generation_batch(conn, batch_id=batch_id, actor=actor)


@router.post("/generation-batches/{batch_id}/cancel", response_model=BatchResult)
def cancel_generation_batch_record(
    batch_id: str,
    db: BusinessDbDep,
) -> BatchResult:
    with db.write() as (conn, actor):
        return cancel_generation_batch(conn, actor=actor, batch_id=batch_id)


@router.patch("/generation-batches/{batch_id}/name", response_model=BatchResult)
def rename_generation_batch_record(
    batch_id: str,
    request: GenerationBatchRenameRequest,
    db: BusinessDbDep,
) -> BatchResult:
    with db.write() as (conn, actor):
        return rename_generation_batch(
            conn,
            actor=actor,
            batch_id=batch_id,
            display_name=request.display_name,
        )


@router.delete(
    "/generation-batches/{batch_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_generation_batch_record(
    batch_id: str,
    db: BusinessDbDep,
) -> Response:
    with db.write() as (conn, actor):
        batch = conn.execute(
            "SELECT id, project_id, created_by_user_id FROM generation_batches WHERE id = %s",
            (batch_id,),
        ).fetchone()
        if batch is None:
            raise HTTPException(status_code=404, detail={"code": "BATCH_NOT_FOUND"})
        if actor.role != "admin" and str(batch["created_by_user_id"]) != actor.id:
            raise HTTPException(
                status_code=404,
                detail={"code": "BATCH_NOT_FOUND"},
            )

        require_not_auditor(
            conn,
            actor=actor,
            action="generation_batch.hide",
            entity_type="generation_batch",
            entity_id=batch_id,
        )
        require_batch_access(
            conn,
            actor=actor,
            project_id=None if batch["project_id"] is None else str(batch["project_id"]),
            created_by_user_id=str(batch["created_by_user_id"]),
            action="generation_batch.hide",
        )
        # List removal is an account preference, never cancellation or data erasure.
        with conn:
            inserted = conn.execute(
                """
                INSERT INTO customer_batch_visibility (user_id, batch_id)
                VALUES (%s, %s)
                ON CONFLICT (user_id, batch_id) DO NOTHING
                """,
                (actor.id, batch_id),
            )
            if inserted.rowcount == 1:
                write_audit(
                    conn,
                    actor=actor,
                    action="generation_batch.hide",
                    entity_type="generation_batch",
                    entity_id=batch_id,
                    metadata={
                        "project_id": str(batch["project_id"]),
                        "hidden_for_user_id": actor.id,
                    },
                    commit=False,
                )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/generation-batches/{batch_id}/regeneration-quote",
    response_model=GenerationPriceQuote,
)
def read_batch_regeneration_quote(
    batch_id: str, conn: Database, actor: AuthenticatedUser
) -> GenerationPriceQuote:
    return generation_regeneration_price_quote(
        conn, actor=actor, source_kind="batch", source_id=batch_id
    )


@router.post(
    "/generation-batches/{batch_id}/regenerate",
    response_model=BatchResult,
)
def regenerate_batch(
    batch_id: str,
    db: BusinessDbDep,
    request: PaidRegenerationRequest | None = None,
) -> BatchResult:
    with db.write() as (conn, actor):
        row = conn.execute(
            "SELECT project_id, created_by_user_id FROM generation_batches WHERE id = %s",
            (batch_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail={"code": "BATCH_NOT_FOUND"})
        require_not_auditor(
            conn,
            actor=actor,
            action="generation_batch.regenerate",
            entity_type="generation_batch",
            entity_id=batch_id,
        )
        require_batch_access(
            conn,
            actor=actor,
            project_id=None if row["project_id"] is None else str(row["project_id"]),
            created_by_user_id=str(row["created_by_user_id"]),
            action="generation_batch.regenerate",
        )
        if request is None:
            raise HTTPException(
                status_code=422,
                detail={"code": "PAID_REGENERATION_REQUEST_REQUIRED"},
            )
        return regenerate_generation_batch(
            conn,
            batch_id=batch_id,
            actor=actor,
            request=request,
        )


@router.get("/generation/runtime-limits", response_model=GenerationRuntimeLimits)
def read_generation_runtime_limits(
    conn: Database,
    _actor: AuthenticatedUser,
) -> GenerationRuntimeLimits:
    return generation_runtime_limits(conn)


@router.get("/generation/price-quote", response_model=GenerationPriceQuote)
def read_generation_price_quote(
    conn: Database,
    actor: AuthenticatedUser,
    resolution: Literal["768P", "2K"] = Query(default="768P"),
    # 16-30 秒为长档计价（generation_price_quote 内拒绝 2K 超档组合）。
    duration_seconds: int = Query(default=8, ge=4, le=30),
    quantity: int = Query(default=1, ge=1),
) -> GenerationPriceQuote:
    # 子账号按母账号钱包计价：套餐权益也随钱包主人（T2.10）。
    return generation_price_quote(
        conn,
        resolution=resolution,
        duration_seconds=duration_seconds,
        quantity=quantity,
        user_id=actor.id,
    )


def _generation_task_context(conn: Database, task_id: str) -> BusinessRow:
    row = conn.execute(
        """
        SELECT
            generation_batches.id AS batch_id,
            generation_batches.project_id,
            generation_batches.created_by_user_id,
            generation_tasks.provider
        FROM generation_tasks
        JOIN generation_batches ON generation_batches.id = generation_tasks.batch_id
        WHERE generation_tasks.id = %s
        """,
        (task_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail={"code": "TASK_NOT_FOUND"})
    return cast(BusinessRow, row)


def _generation_preview_task(
    conn: BusinessConnection, *, actor: CurrentUser, task_id: str, lock: bool = False
) -> BusinessRow:
    task = conn.execute(
        """
        SELECT batch.project_id, batch.created_by_user_id,
               task.provider, task.provider_task_id, task.provider_result_url, task.status,
               task.billed_seconds, task.prompt_snapshot_json,
               COALESCE(task.locked_by IS NOT NULL
                        AND task.locked_until::timestamptz > clock_timestamp(), FALSE)
                   AS result_lease_active
        FROM generation_tasks AS task
        JOIN generation_batches AS batch ON batch.id = task.batch_id
        WHERE task.id = %s
          AND (task.archive_status IN ('ARCHIVING', 'DIRECT', 'ARCHIVE_FAILED')
               OR (task.archive_status='ARCHIVED' AND task.billed_seconds=30))
        """
        + (" FOR UPDATE OF task" if lock else ""),
        (task_id,),
    ).fetchone()
    if task is None:
        raise HTTPException(status_code=404, detail={"code": "TASK_NOT_FOUND"})
    require_batch_access(
        conn,
        actor=actor,
        project_id=None if task["project_id"] is None else str(task["project_id"]),
        created_by_user_id=str(task["created_by_user_id"]),
        action="generation_task.preview",
    )
    if requires_strict_generation_duration(dict(task)):
        # 只读入口不转码；客户端先走幂等归档，再使用已校验资产的授权链接。
        raise HTTPException(
            409,
            detail={
                "code": "RESULT_DURATION_PREPARATION_REQUIRED",
                "message": "30 秒成片需先完成交付校验；不会重新生成或扣费。",
            },
        )
    result_url = task["provider_result_url"]
    if not isinstance(result_url, str) or not result_url.strip():
        raise HTTPException(status_code=409, detail={"code": "RESULT_URL_NOT_READY"})
    return cast(BusinessRow, task)


@router.get(
    "/generation-tasks/{task_id}/preview-url",
    response_model=GenerationTaskPreviewUrlResponse,
)
def read_generation_task_preview_url(
    task_id: str,
    db: BusinessDbDep,
    verify: bool = Query(
        default=False,
        description="播放/下载失败后由客户端带上：服务端探测直链是否已失效。",
    ),
) -> GenerationTaskPreviewUrlResponse:
    provider = None
    with db.write() as (conn, actor):
        task = _generation_preview_task(conn, actor=actor, task_id=task_id)
        original_url = result_url = str(task["provider_result_url"])
        refresh = (
            task["provider"] == "metaso"
            and task["status"] == "SUCCEEDED"
            and bool(task["provider_task_id"])
            and provider_result_url_needs_refresh(result_url)
        )
        fake_result = task["provider"] == "fake_h3" and result_url.startswith("fake://")
        if refresh or fake_result:
            try:
                provider = h3_provider_for_task(conn, str(task["provider"]), task_id=task_id)
            except H3ProviderSettingsUnavailable as exc:
                raise HTTPException(
                    503,
                    detail={
                        "code": "RESULT_URL_REFRESH_UNAVAILABLE",
                        "message": "成片播放地址暂时无法刷新，请稍后重试。",
                    },
                ) from exc

    # 外部查询必须在会话栅栏事务结束后执行，不能让刷新链接再次挡住首页媒体授权。
    if refresh:
        assert provider is not None
        try:
            current = provider.query_image_to_video(str(task["provider_task_id"]))
        except H3ProviderFailed as exc:
            raise HTTPException(
                503,
                detail={
                    "code": "RESULT_URL_REFRESH_FAILED",
                    "message": "成片播放地址刷新失败，请稍后重试。",
                },
            ) from exc
        if (
            current.status != "SUCCEEDED"
            or not current.result_url
            or provider_result_url_needs_refresh(current.result_url)
        ):
            raise HTTPException(503, detail={"code": "RESULT_URL_REFRESH_NOT_READY"})
        result_url = current.result_url
    expired = None
    if fake_result:
        # Browsers cannot fetch fake://; only the internal fixture is embedded.
        # The factory retains the production ban, and ownership was checked above.
        try:
            assert provider is not None
            content = provider.download_result(result_url)
        except H3ProviderSettingsUnavailable as exc:
            logger.warning("fake result unavailable for task %s: %s", task_id, exc)
            raise HTTPException(
                status_code=503, detail={"code": "FAKE_RESULT_UNAVAILABLE"}
            ) from exc
        result_url = "data:video/mp4;base64," + base64.b64encode(content).decode("ascii")
    elif verify:
        expired = provider_result_url_expired(result_url)
    if refresh or verify or fake_result:
        # HEAD 探测和续签都可能阻塞；全部外部 I/O 结束后再核对会话、归属及源记录。
        with db.write() as (conn, actor):
            latest = _generation_preview_task(conn, actor=actor, task_id=task_id, lock=refresh)
            if (
                latest["provider_result_url"] != original_url
                or latest["provider_task_id"] != task["provider_task_id"]
                or latest["provider"] != task["provider"]
                or latest["status"] != task["status"]
            ):
                # 其他请求已换源，旧地址的探测结论不能否定当前成片。
                result_url = str(latest["provider_result_url"])
            else:
                if expired:
                    raise result_url_expired_error()
                if refresh and not latest["result_lease_active"]:
                    # 归档持有租约时仅交付新签名，不改源记录，避免打断在途保存。
                    conn.execute(
                        "UPDATE generation_tasks SET provider_result_url=%s "
                        "WHERE id=%s AND provider_result_url=%s",
                        (result_url, task_id, original_url),
                    )
                    write_audit(
                        conn,
                        actor=actor,
                        action="generation_task.preview_url.refresh",
                        entity_type="generation_task",
                        entity_id=task_id,
                        metadata={"reason": "expired_signature"},
                    )
    return GenerationTaskPreviewUrlResponse(url=result_url)


@router.post("/generation-tasks/{task_id}/archive", response_model=TaskResult)
def archive_generation_result(task_id: str, db: BusinessDbDep, storage: MediaStorage) -> TaskResult:
    prepared = None
    source_storage: StorageAdapter | None = None
    source_key = ""
    try:
        with db.write() as (conn, actor):
            prepared = claim_generation_result_archive(conn, actor=actor, task_id=task_id)
            if generation_result_archive_ready(conn, prepared):
                return get_task_result(conn, task_id)
            if prepared["result_asset_id"]:
                # 已归档原片从自己的云资产修复，不依赖可能过期的原始直链。
                source_asset = require_asset_access(
                    conn,
                    actor=actor,
                    asset_id=str(prepared["result_asset_id"]),
                    action="generation_task.archive",
                )
                source_uri = str(source_asset["storage_uri"])
                source_storage = storage_for_asset(conn, source_uri)
                source_key = storage_key_from_uri(source_uri)
            else:
                provider = h3_provider_for_task(conn, str(prepared["provider"]), task_id=task_id)
        download_url = str(prepared["provider_result_url"] or "")
        if (
            source_storage is None
            and requires_strict_generation_duration(prepared)
            and prepared["provider"] == "metaso"
            and prepared.get("provider_task_id")
            and provider_result_url_needs_refresh(download_url)
        ):
            # 严格交付入口不返回原片直链；这里只查询同一已完成任务，保留续签能力。
            refreshed = None
            refresh_error = None
            try:
                refreshed = provider.query_image_to_video(str(prepared["provider_task_id"]))
            except H3ProviderFailed as exc:
                refresh_error = exc
            # 续签外呼期间可能退出账号、换源或失去租约；复核后才允许下载新签名。
            with db.write() as (conn, actor):
                prepared = renew_generation_result_archive_claim(
                    conn, actor=actor, prepared=prepared
                )
            if refresh_error is not None:
                raise HTTPException(
                    503,
                    detail={
                        "code": "RESULT_URL_REFRESH_FAILED",
                        "message": "成片播放地址刷新失败，请稍后重试。",
                    },
                ) from refresh_error
            if (
                refreshed is None
                or refreshed.status != "SUCCEEDED"
                or not refreshed.result_url
                or provider_result_url_needs_refresh(refreshed.result_url)
            ):
                raise HTTPException(503, detail={"code": "RESULT_URL_REFRESH_NOT_READY"})
            # 不改任务源记录，避免续签打断保存租约；新地址仅用于本次原片读取。
            download_url = refreshed.result_url
        # Download only the existing result; no generation submission or billing operation.
        try:
            if source_storage is not None:
                chunks = source_storage.iter_object(source_key, end=MAX_UPLOAD_BYTES)
                buffer = bytearray()
                try:
                    for chunk in chunks:
                        if len(buffer) + len(chunk) > MAX_UPLOAD_BYTES:
                            raise VideoProbeFailed("成片超过归档大小上限")
                        buffer.extend(chunk)
                finally:
                    close = getattr(chunks, "close", None)
                    if callable(close):
                        close()
                content = bytes(buffer)
            else:
                content = provider.download_result(download_url)
        except H3ProviderFailed as exc:
            # 直链已失效时重试必然失败：如实告知「文件已失效」，不再提示「请重试」。
            if exc.http_status in RESULT_URL_EXPIRED_STATUSES:
                raise result_url_expired_error() from exc
            raise
        if not content or len(content) > MAX_UPLOAD_BYTES or content[4:8] != b"ftyp":
            raise VideoProbeFailed("invalid MP4 result")
        metadata = FFprobeVideoProbe().probe(content, filename="result.mp4")
        if not math.isfinite(metadata.duration_seconds) or metadata.duration_seconds <= 0:
            raise VideoProbeFailed("invalid video duration")
        # Check ownership/session after the download and before CPU-heavy work.
        with db.write() as (conn, actor):
            renewed = renew_generation_result_archive_claim(conn, actor=actor, prepared=prepared)
        prepared = renewed
        duration_seconds = metadata.duration_seconds
        normalization_metadata = None
        snapshot = json.loads(str(prepared["prompt_snapshot_json"] or "{}"))
        if requires_strict_generation_duration(prepared):
            source_digest = hashlib.sha256(content).hexdigest()
            normalized = normalize_generated_video(
                content,
                target_width=metadata.width or 0,
                target_height=metadata.height or 0,
                target_duration_seconds=30,
                max_duration_seconds=GENERATION_RESULT_MAX_DURATION_SECONDS,
            )
            content = normalized.content
            duration_seconds = normalized.duration_seconds
            normalization_metadata = {
                "policy": STRICT_DURATION_POLICY,
                "target_duration_seconds": 30,
                "source_sha256": source_digest,
                "source_duration_seconds": metadata.duration_seconds,
                "width": normalized.width,
                "height": normalized.height,
                "transformed": normalized.transformed,
            }
        # Only this explicitly requested profile has a confirmed pixel mapping.
        # Adaptive and other profiles retain the supplier's original result.
        elif (
            isinstance(snapshot, dict)
            and snapshot.get("resolution") == "2K"
            and snapshot.get("ratio") == "9:16"
        ):
            source_digest = hashlib.sha256(content).hexdigest()
            normalized = normalize_generated_video(
                content,
                target_width=1440,
                target_height=2560,
                max_duration_seconds=GENERATION_RESULT_MAX_DURATION_SECONDS,
            )
            content = normalized.content
            duration_seconds = normalized.duration_seconds
            if not content or len(content) > MAX_UPLOAD_BYTES:
                raise VideoProbeFailed("invalid normalized video size")
            normalization_metadata = {
                "policy": "explicit_2k_portrait_v1",
                "requested_resolution": "2K",
                "requested_ratio": "9:16",
                "source_sha256": source_digest,
                "width": normalized.width,
                "height": normalized.height,
                "source_sample_aspect_ratio": normalized.source_sample_aspect_ratio,
                "source_display_aspect_ratio": normalized.source_display_aspect_ratio,
                "sample_aspect_ratio": normalized.sample_aspect_ratio,
                "display_aspect_ratio": normalized.display_aspect_ratio,
                "source_rotation_degrees": normalized.source_rotation_degrees,
                "transformed": normalized.transformed,
            }
        digest = hashlib.sha256(content).hexdigest()
        # No DB transaction stays open during normalization or object upload.
        with db.write() as (conn, actor):
            renewed = renew_generation_result_archive_claim(conn, actor=actor, prepared=prepared)
        prepared = renewed
        stored = storage.put_object(
            f"generation-results/{task_id}/{digest}.mp4",
            content,
            content_type="video/mp4",
        )
        # MATERIAL-THUMBS-B：成片字节在手时抽首帧（写事务之外）；失败只损失缩略图。
        thumbnail_key = store_video_thumbnail(storage, stored.key, content)
        with db.write() as (conn, actor):
            return persist_generation_result_archive(
                conn,
                actor=actor,
                prepared=prepared,
                stored=stored,
                duration_seconds=duration_seconds,
                normalization_metadata=normalization_metadata,
                thumbnail_key=thumbnail_key,
            )
    except (
        H3ProviderFailed,
        H3ProviderSettingsUnavailable,
        VideoProbeFailed,
        VideoProbeUnavailable,
        MediaToolFailed,
        MediaToolUnavailable,
        MediaValidationFailed,
        StorageBackendUnavailable,
    ) as exc:
        raise HTTPException(
            503,
            detail={
                "code": "RESULT_ARCHIVE_UNAVAILABLE",
                "message": "成片保存暂时失败，请重试；不会重新生成或扣费。",
            },
        ) from exc
    finally:
        if prepared is not None and prepared.get("_archive_claim_acquired"):
            try:
                # A revoked session must not strand its claim. This internal
                # transaction can only release the exact server-created lease.
                with pg_transaction() as raw:
                    release_generation_result_archive_claim(
                        BusinessConnection.postgres(raw), prepared=prepared
                    )
            except Exception as exc:
                # Preserve the original 401/409/503; expiry still permits recovery.
                logger.warning("manual archive claim cleanup failed: %s", type(exc).__name__)


@router.post("/generation-tasks/{task_id}/retry", response_model=TaskResult)
def retry_task(
    task_id: str,
    db: BusinessDbDep,
    request: GenerationTaskRetryRequest | None = None,
) -> TaskResult:
    with db.write() as (conn, actor):
        row = _generation_task_context(conn, task_id)
        require_not_auditor(
            conn,
            actor=actor,
            action="generation_task.retry",
            entity_type="generation_task",
            entity_id=task_id,
        )
        require_batch_access(
            conn,
            actor=actor,
            project_id=None if row["project_id"] is None else str(row["project_id"]),
            created_by_user_id=str(row["created_by_user_id"]),
            action="generation_task.retry",
        )
        if request is None:
            raise HTTPException(status_code=422, detail={"code": "RETRY_REQUEST_REQUIRED"})
        return retry_generation_task(conn, task_id=task_id, actor=actor, request=request)


@router.get(
    "/generation-tasks/{task_id}/regeneration-quote",
    response_model=GenerationPriceQuote,
)
def read_task_regeneration_quote(
    task_id: str, conn: Database, actor: AuthenticatedUser
) -> GenerationPriceQuote:
    return generation_regeneration_price_quote(
        conn, actor=actor, source_kind="task", source_id=task_id
    )


@router.post(
    "/generation-tasks/{task_id}/regenerate",
    response_model=BatchResult,
)
def regenerate_task(
    task_id: str,
    db: BusinessDbDep,
    request: PaidRegenerationRequest | None = None,
) -> BatchResult:
    with db.write() as (conn, actor):
        row = _generation_task_context(conn, task_id)
        require_not_auditor(
            conn,
            actor=actor,
            action="generation_task.regenerate",
            entity_type="generation_task",
            entity_id=task_id,
        )
        require_batch_access(
            conn,
            actor=actor,
            project_id=None if row["project_id"] is None else str(row["project_id"]),
            created_by_user_id=str(row["created_by_user_id"]),
            action="generation_task.regenerate",
        )
        if request is None:
            raise HTTPException(
                status_code=422,
                detail={"code": "PAID_REGENERATION_REQUEST_REQUIRED"},
            )
        return regenerate_generation_task(
            conn,
            task_id=task_id,
            actor=actor,
            request=request,
        )


@router.post(
    "/generation-tasks/{task_id}/confirm-not-charged",
    response_model=TaskResult,
)
def confirm_task_not_charged(
    task_id: str,
    db: BusinessDbDep,
    request: ConfirmNotChargedRequest | None = None,
) -> TaskResult:
    with db.write() as (conn, actor):
        row = _generation_task_context(conn, task_id)
        require_role(
            conn,
            actor=actor,
            allowed_roles={"admin"},
            action="generation_task.confirm_not_charged",
            entity_type="generation_task",
            entity_id=task_id,
        )
        require_batch_access(
            conn,
            actor=actor,
            project_id=None if row["project_id"] is None else str(row["project_id"]),
            created_by_user_id=str(row["created_by_user_id"]),
            action="generation_task.confirm_not_charged",
        )
        if request is None:
            raise HTTPException(
                status_code=422,
                detail={"code": "CONFIRM_NOT_CHARGED_REQUEST_REQUIRED"},
            )
        return confirm_generation_task_not_charged(
            conn,
            task_id=task_id,
            actor=actor,
            request=request,
        )


@router.post(
    "/generation-tasks/{task_id}/reconcile",
    response_model=GenerationReconcileOperationResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def reconcile_uncertain_task(
    task_id: str,
    db: BusinessDbDep,
    request: ReconcileGenerationTaskRequest | None = None,
) -> GenerationReconcileOperationResponse:
    with db.write() as (conn, actor):
        row = _generation_task_context(conn, task_id)
        require_not_auditor(
            conn,
            actor=actor,
            action="generation_task.reconcile",
            entity_type="generation_task",
            entity_id=task_id,
        )
        require_batch_access(
            conn,
            actor=actor,
            project_id=None if row["project_id"] is None else str(row["project_id"]),
            created_by_user_id=str(row["created_by_user_id"]),
            action="generation_task.reconcile",
        )
        if request is None:
            raise HTTPException(status_code=422, detail={"code": "RECONCILE_REQUEST_REQUIRED"})

        operation = enqueue_generation_reconcile_operation(
            conn,
            task_id=task_id,
            batch_id=str(row["batch_id"]),
            project_id=str(row["project_id"]),
            actor=actor,
            request=request,
        )
        return generation_reconcile_operation_response(operation)


@router.get(
    "/generation-reconcile-operations/{operation_id}",
    response_model=GenerationReconcileOperationResponse,
)
def read_generation_reconcile_operation(
    operation_id: str,
    conn: Database,
    actor: AuthenticatedUser,
) -> GenerationReconcileOperationResponse:
    row = load_generation_reconcile_operation(conn, operation_id)
    require_batch_access(
        conn,
        actor=actor,
        project_id=None if row["project_id"] is None else str(row["project_id"]),
        created_by_user_id=str(row["actor_user_id"]),
        action="generation_task.reconcile_read",
    )
    return generation_reconcile_operation_response(row)


@router.get(
    "/generation-tasks/{task_id}/reconcile/latest",
    response_model=GenerationReconcileOperationResponse | None,
)
def read_latest_generation_reconcile_operation(
    task_id: str,
    conn: Database,
    actor: AuthenticatedUser,
) -> GenerationReconcileOperationResponse | None:
    task = _generation_task_context(conn, task_id)
    require_batch_access(
        conn,
        actor=actor,
        project_id=(None if task["project_id"] is None else str(task["project_id"])),
        created_by_user_id=str(task["created_by_user_id"]),
        action="generation_task.reconcile_read",
    )
    row = latest_generation_reconcile_operation(conn, task_id=task_id)
    return None if row is None else generation_reconcile_operation_response(row)


def generation_reconcile_operation_response(
    row: BusinessRow,
) -> GenerationReconcileOperationResponse:
    result_status = str(row["result_status"])
    operation_status = (
        "RUNNING"
        if result_status == "PENDING" and row["locked_by"] is not None
        else "PENDING"
        if result_status == "PENDING"
        else "SUCCEEDED"
        if result_status == "COMPLETED"
        else "FAILED"
    )
    return GenerationReconcileOperationResponse(
        id=str(row["id"]),
        task_id=str(row["task_id"]),
        status=operation_status,
        attempt=int(row["attempt"]),
        error_code=None if row["error_code"] is None else str(row["error_code"]),
        error_message=(
            None if row["error_message_redacted"] is None else str(row["error_message_redacted"])
        ),
        retryable=bool(row["retryable"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        started_at=None if row["started_at"] is None else str(row["started_at"]),
        completed_at=(None if row["completed_at"] is None else str(row["completed_at"])),
    )
