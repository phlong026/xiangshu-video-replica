from __future__ import annotations

import base64
import logging
import sqlite3
from typing import cast
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel

from app.auth import AuthenticatedUser, Database
from app.customer_fence import BusinessDbDep
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
    enqueue_generation_reconcile_operation,
    generation_runtime_limits,
    get_generation_batch,
    h3_provider_for_task,
    latest_generation_reconcile_operation,
    list_generation_batches,
    load_generation_reconcile_operation,
    lock_prompt_version,
    preview_prompt_text,
    regenerate_generation_batch,
    regenerate_generation_task,
    rename_generation_batch,
    retry_generation_task,
    revise_prompt_version,
    version_result,
    version_state,
)
from app.permissions import (
    require_not_auditor,
    require_project_access,
    require_role,
    write_audit,
)
from app.script_rewrite import (
    ScriptRewriteRequest,
    ScriptRewriteResult,
    enqueue_script_rewrite_task,
    latest_script_rewrite_task,
    load_script_rewrite_task,
    script_rewrite_task_result,
)

router = APIRouter(prefix="/api", tags=["generation"])
logger = logging.getLogger(__name__)


class ScriptRewriteTaskResponse(BaseModel):
    id: str
    project_id: str
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
) -> ScriptRewriteTaskResponse | None:
    require_project_access(
        conn,
        actor=actor,
        project_id=project_id,
        action="project.script_rewrite_read",
    )
    row = latest_script_rewrite_task(conn, project_id=project_id)
    return None if row is None else script_rewrite_task_response(row)


def script_rewrite_task_response(row: sqlite3.Row) -> ScriptRewriteTaskResponse:
    return ScriptRewriteTaskResponse(
        id=str(row["id"]),
        project_id=str(row["project_id"]),
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
        require_project_access(
            conn, actor=actor, project_id=str(batch["project_id"]), action="generation_batch.hide"
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
            "SELECT project_id FROM generation_batches WHERE id = %s",
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
        WHERE generation_tasks.id = %s
        """,
        (task_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail={"code": "TASK_NOT_FOUND"})
    return cast(sqlite3.Row, row)


@router.get(
    "/generation-tasks/{task_id}/preview-url",
    response_model=GenerationTaskPreviewUrlResponse,
)
def read_generation_task_preview_url(
    task_id: str,
    conn: Database,
    actor: AuthenticatedUser,
) -> GenerationTaskPreviewUrlResponse:
    task = conn.execute(
        """
        SELECT batch.project_id, task.provider, task.provider_result_url
        FROM generation_tasks AS task
        JOIN generation_batches AS batch ON batch.id = task.batch_id
        WHERE task.id = %s
          AND task.archive_status IN ('ARCHIVING', 'DIRECT', 'ARCHIVE_FAILED')
        """,
        (task_id,),
    ).fetchone()
    if task is None:
        raise HTTPException(status_code=404, detail={"code": "TASK_NOT_FOUND"})
    require_project_access(
        conn,
        actor=actor,
        project_id=str(task["project_id"]),
        action="generation_task.preview",
    )
    result_url = task["provider_result_url"]
    if not isinstance(result_url, str) or not result_url.strip():
        raise HTTPException(status_code=409, detail={"code": "RESULT_URL_NOT_READY"})
    if task["provider"] == "fake_h3" and result_url.startswith("fake://"):
        # Browsers cannot fetch fake://; only the internal fixture is embedded.
        # The factory retains the production ban, and ownership was checked above.
        try:
            provider = h3_provider_for_task(conn, "fake_h3")
            content = provider.download_result(result_url)
        except H3ProviderSettingsUnavailable as exc:
            logger.warning("fake result unavailable for task %s: %s", task_id, exc)
            raise HTTPException(
                status_code=503, detail={"code": "FAKE_RESULT_UNAVAILABLE"}
            ) from exc
        result_url = "data:video/mp4;base64," + base64.b64encode(content).decode("ascii")
    return GenerationTaskPreviewUrlResponse(url=result_url)


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
        require_project_access(
            conn,
            actor=actor,
            project_id=str(row["project_id"]),
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
    require_project_access(
        conn,
        actor=actor,
        project_id=str(row["project_id"]),
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
    require_project_access(
        conn,
        actor=actor,
        project_id=str(task["project_id"]),
        action="generation_task.reconcile_read",
    )
    row = latest_generation_reconcile_operation(conn, task_id=task_id)
    return None if row is None else generation_reconcile_operation_response(row)


def generation_reconcile_operation_response(
    row: sqlite3.Row,
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
