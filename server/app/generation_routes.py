from __future__ import annotations

import sqlite3
from typing import cast

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from app.auth import AuthenticatedUser, Database
from app.generation import (
    BatchResult,
    BatchStatusFilter,
    ConfirmNotChargedRequest,
    FakeH3Provider,
    GenerationBatchListPage,
    GenerationBatchRenameRequest,
    GenerationBatchRequest,
    GenerationRuntimeLimits,
    GenerationTaskRetryRequest,
    H3Provider,
    H3ProviderSettingsUnavailable,
    PaidRegenerationRequest,
    PromptCompileRequest,
    PromptPreviewRequest,
    PromptPreviewResult,
    PromptRevisionRequest,
    ReconcileGenerationTaskRequest,
    ScriptRequest,
    TaskResult,
    VersionResult,
    VersionState,
    compile_prompt_version,
    confirm_generation_task_not_charged,
    create_generation_batch,
    create_script_version,
    generation_runtime_limits,
    get_generation_batch,
    h3_provider_for_task,
    list_generation_batches,
    lock_prompt_version,
    preview_prompt_text,
    reconcile_generation_task,
    regenerate_generation_batch,
    regenerate_generation_task,
    rename_generation_batch,
    retry_generation_task,
    revise_prompt_version,
    version_result,
    version_state,
)
from app.media import storage_key_from_uri
from app.media_routes import get_local_result_storage
from app.permissions import (
    require_not_auditor,
    require_project_access,
    require_role,
    write_audit,
)
from app.rbac_routes import storage_for_asset
from app.script_rewrite import (
    ScriptRewriteRequest,
    ScriptRewriteResult,
    rewrite_script_with_deepseek,
)
from app.storage import StorageBackendUnavailable

router = APIRouter(prefix="/api", tags=["generation"])


def get_h3_provider() -> H3Provider:
    return FakeH3Provider()


@router.post("/projects/{project_id}/scripts", response_model=VersionResult)
def create_project_script(
    project_id: str,
    request: ScriptRequest,
    conn: Database,
    actor: AuthenticatedUser,
) -> VersionResult:
    row = create_script_version(conn, project_id=project_id, actor=actor, request=request)
    return version_result(row)


@router.post(
    "/projects/{project_id}/script-rewrite",
    response_model=ScriptRewriteResult,
)
def rewrite_project_script(
    project_id: str,
    request: ScriptRewriteRequest,
    conn: Database,
    actor: AuthenticatedUser,
) -> ScriptRewriteResult:
    require_not_auditor(
        conn,
        actor=actor,
        action="project.script_rewrite",
        entity_type="project",
        entity_id=project_id,
    )
    require_project_access(
        conn,
        actor=actor,
        project_id=project_id,
        action="project.script_rewrite",
    )
    return rewrite_script_with_deepseek(
        conn,
        actor=actor,
        source_text=request.text,
    )


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
    conn: Database,
    actor: AuthenticatedUser,
) -> VersionResult:
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
    conn: Database,
    actor: AuthenticatedUser,
) -> VersionResult:
    row = revise_prompt_version(
        conn,
        project_id=project_id,
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
    conn: Database,
    actor: AuthenticatedUser,
) -> VersionResult:
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
    conn: Database,
    actor: AuthenticatedUser,
    request: GenerationBatchRequest | None = None,
    provider: H3Provider = Depends(get_h3_provider),
) -> BatchResult:
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


@router.patch("/generation-batches/{batch_id}/name", response_model=BatchResult)
def rename_generation_batch_record(
    batch_id: str,
    request: GenerationBatchRenameRequest,
    conn: Database,
    actor: AuthenticatedUser,
) -> BatchResult:
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
    conn: Database,
    actor: AuthenticatedUser,
) -> Response:
    batch = conn.execute(
        "SELECT id, project_id, created_by_user_id FROM generation_batches WHERE id = ?",
        (batch_id,),
    ).fetchone()
    if batch is None:
        raise HTTPException(status_code=404, detail={"code": "BATCH_NOT_FOUND"})
    if actor.role != "admin" and str(batch["created_by_user_id"]) != actor.id:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "GENERATION_BATCH_FORBIDDEN",
                "message": "只有批次创建者或管理员可以删除批次。",
            },
        )

    # 付费 provider 调用仍在途时禁止删除（与项目删除同一约束），否则
    # 任务的云端回写会丢失，费用对账失去依据。
    has_active_tasks = conn.execute(
        """
        SELECT 1
        FROM generation_tasks
        WHERE batch_id = ?
          AND status IN ('PENDING', 'SUBMITTING', 'QUEUED', 'RUNNING', 'ARCHIVING')
        LIMIT 1
        """,
        (batch_id,),
    ).fetchone()
    if has_active_tasks:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "BATCH_DELETE_HAS_ACTIVE_TASKS",
                "message": "批次存在进行中的生成任务，请等待任务结束或失败后再删除。",
            },
        )

    result_assets = conn.execute(
        """
        SELECT assets.id, assets.storage_uri
        FROM assets
        JOIN generation_tasks ON generation_tasks.result_asset_id = assets.id
        WHERE generation_tasks.batch_id = ?
        """,
        (batch_id,),
    ).fetchall()
    task_count = int(
        conn.execute(
            "SELECT COUNT(*) FROM generation_tasks WHERE batch_id = ?",
            (batch_id,),
        ).fetchone()[0]
    )

    # 尽力清理云端产物：后端不可用（如云凭证已移除）不阻塞删除，失败
    # 数进审计日志；审计日志本身始终保留，付费记录仍可对账。
    storage_cleanup_failed_count = 0
    for asset in result_assets:
        try:
            storage = storage_for_asset(conn, str(asset["storage_uri"]))
            storage.delete_object(
                storage_key_from_uri(str(asset["storage_uri"])), actor_id=actor.id
            )
        except (HTTPException, StorageBackendUnavailable, OSError, ValueError):
            storage_cleanup_failed_count += 1

    with conn:
        # 结果资产行必须先于批次删除（任务级联删除后子查询会失效）。
        conn.execute(
            """
            DELETE FROM assets
            WHERE id IN (
                SELECT result_asset_id FROM generation_tasks
                WHERE batch_id = ? AND result_asset_id IS NOT NULL
            )
            """,
            (batch_id,),
        )
        conn.execute("DELETE FROM generation_batches WHERE id = ?", (batch_id,))
    write_audit(
        conn,
        actor=actor,
        action="generation_batch.delete",
        entity_type="generation_batch",
        entity_id=batch_id,
        metadata={
            "project_id": str(batch["project_id"]),
            "deleted_task_count": task_count,
            "deleted_asset_count": len(result_assets),
            "storage_cleanup_failed_count": storage_cleanup_failed_count,
        },
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/generation-batches/{batch_id}/regenerate",
    response_model=BatchResult,
)
def regenerate_batch(
    batch_id: str,
    conn: Database,
    actor: AuthenticatedUser,
    request: PaidRegenerationRequest | None = None,
) -> BatchResult:
    row = conn.execute(
        "SELECT project_id FROM generation_batches WHERE id = ?",
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
    require_project_access(
        conn,
        actor=actor,
        project_id=str(row["project_id"]),
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


def _generation_task_context(conn: Database, task_id: str) -> sqlite3.Row:
    row = conn.execute(
        """
        SELECT
            generation_batches.id AS batch_id,
            generation_batches.project_id,
            generation_batches.created_by_user_id,
            generation_tasks.provider
        FROM generation_tasks
        JOIN generation_batches ON generation_batches.id = generation_tasks.batch_id
        WHERE generation_tasks.id = ?
        """,
        (task_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail={"code": "TASK_NOT_FOUND"})
    return cast(sqlite3.Row, row)


@router.post("/generation-tasks/{task_id}/retry", response_model=TaskResult)
def retry_task(
    task_id: str,
    conn: Database,
    actor: AuthenticatedUser,
    request: GenerationTaskRetryRequest | None = None,
) -> TaskResult:
    row = _generation_task_context(conn, task_id)
    require_not_auditor(
        conn,
        actor=actor,
        action="generation_task.retry",
        entity_type="generation_task",
        entity_id=task_id,
    )
    require_project_access(
        conn,
        actor=actor,
        project_id=str(row["project_id"]),
        action="generation_task.retry",
    )
    if request is None:
        raise HTTPException(status_code=422, detail={"code": "RETRY_REQUEST_REQUIRED"})
    return retry_generation_task(conn, task_id=task_id, actor=actor, request=request)


@router.post(
    "/generation-tasks/{task_id}/regenerate",
    response_model=BatchResult,
)
def regenerate_task(
    task_id: str,
    conn: Database,
    actor: AuthenticatedUser,
    request: PaidRegenerationRequest | None = None,
) -> BatchResult:
    row = _generation_task_context(conn, task_id)
    require_not_auditor(
        conn,
        actor=actor,
        action="generation_task.regenerate",
        entity_type="generation_task",
        entity_id=task_id,
    )
    require_project_access(
        conn,
        actor=actor,
        project_id=str(row["project_id"]),
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
    conn: Database,
    actor: AuthenticatedUser,
    request: ConfirmNotChargedRequest | None = None,
) -> TaskResult:
    row = _generation_task_context(conn, task_id)
    require_role(
        conn,
        actor=actor,
        allowed_roles={"admin"},
        action="generation_task.confirm_not_charged",
        entity_type="generation_task",
        entity_id=task_id,
    )
    require_project_access(
        conn,
        actor=actor,
        project_id=str(row["project_id"]),
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


@router.post("/generation-tasks/{task_id}/reconcile", response_model=TaskResult)
def reconcile_uncertain_task(
    task_id: str,
    conn: Database,
    actor: AuthenticatedUser,
    request: ReconcileGenerationTaskRequest | None = None,
) -> TaskResult:
    row = _generation_task_context(conn, task_id)
    require_not_auditor(
        conn,
        actor=actor,
        action="generation_task.reconcile",
        entity_type="generation_task",
        entity_id=task_id,
    )
    require_project_access(
        conn,
        actor=actor,
        project_id=str(row["project_id"]),
        action="generation_task.reconcile",
    )
    if request is None:
        raise HTTPException(status_code=422, detail={"code": "RECONCILE_REQUEST_REQUIRED"})

    def provider_factory() -> H3Provider:
        try:
            return h3_provider_for_task(conn, str(row["provider"]))
        except H3ProviderSettingsUnavailable as exc:
            raise HTTPException(
                status_code=503,
                detail={"code": "METASO_SETTINGS_UNAVAILABLE"},
            ) from exc

    return reconcile_generation_task(
        conn,
        task_id=task_id,
        batch_id=str(row["batch_id"]),
        project_id=str(row["project_id"]),
        created_by_user_id=str(row["created_by_user_id"]),
        actor=actor,
        request=request,
        # 归档重试只写生成成片，固定落本地盘（与 worker 归档一致）。
        storage_factory=lambda: get_local_result_storage(conn),
        provider_factory=provider_factory,
    )
