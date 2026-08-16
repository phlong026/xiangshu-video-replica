from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from app.auth import AuthenticatedUser, Database
from app.generation import (
    BatchResult,
    BatchStatusFilter,
    FakeH3Provider,
    GenerationBatchListPage,
    GenerationBatchRequest,
    GenerationRuntimeLimits,
    H3Provider,
    H3ProviderSettingsUnavailable,
    PromptCompileRequest,
    PromptRevisionRequest,
    ScriptRequest,
    TaskResult,
    VersionResult,
    VersionState,
    compile_prompt_version,
    create_generation_batch,
    create_script_version,
    generation_runtime_limits,
    get_generation_batch,
    h3_provider_for_task,
    list_generation_batches,
    lock_prompt_version,
    reconcile_submission_uncertain_task,
    revise_prompt_version,
    version_result,
    version_state,
)
from app.media_routes import MediaStorage
from app.permissions import require_not_auditor, require_project_access

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


@router.get("/generation/runtime-limits", response_model=GenerationRuntimeLimits)
def read_generation_runtime_limits(
    conn: Database,
    _actor: AuthenticatedUser,
) -> GenerationRuntimeLimits:
    return generation_runtime_limits(conn)


@router.post("/generation-tasks/{task_id}/reconcile", response_model=TaskResult)
def reconcile_uncertain_task(
    task_id: str,
    conn: Database,
    actor: AuthenticatedUser,
    storage: MediaStorage,
) -> TaskResult:
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
    try:
        provider = h3_provider_for_task(conn, str(row["provider"]))
    except H3ProviderSettingsUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "METASO_SETTINGS_UNAVAILABLE"},
        ) from exc
    return reconcile_submission_uncertain_task(
        conn,
        task_id=task_id,
        batch_id=str(row["batch_id"]),
        project_id=str(row["project_id"]),
        created_by_user_id=str(row["created_by_user_id"]),
        storage=storage,
        provider=provider,
    )
