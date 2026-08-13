from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from app.auth import AuthenticatedUser, Database
from app.generation import (
    BatchResult,
    FakeH3Provider,
    GenerationBatchRequest,
    H3Provider,
    PromptCompileRequest,
    ScriptRequest,
    VersionResult,
    compile_prompt_version,
    create_generation_batch,
    create_script_version,
    get_generation_batch,
    lock_prompt_version,
    version_result,
)

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


@router.post("/projects/{project_id}/prompts/compile", response_model=VersionResult)
def compile_project_prompt(
    project_id: str,
    request: PromptCompileRequest,
    conn: Database,
    actor: AuthenticatedUser,
) -> VersionResult:
    row = compile_prompt_version(conn, project_id=project_id, actor=actor, request=request)
    return version_result(row)


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


@router.get("/generation-batches/{batch_id}", response_model=BatchResult)
def read_generation_batch(
    batch_id: str,
    conn: Database,
    actor: AuthenticatedUser,
) -> BatchResult:
    return get_generation_batch(conn, batch_id=batch_id, actor=actor)
