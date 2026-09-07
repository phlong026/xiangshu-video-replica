"""HTTP routes for durable viral-video project imports."""

from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, status

from app.auth import AuthenticatedUser, Database
from app.customer_fence import BusinessDbDep
from app.viral_import import (
    ViralImportRequest,
    ViralImportTaskResponse,
    enqueue_viral_import_task,
    load_viral_import_task,
    viral_import_task_response,
)

router = APIRouter(prefix="/api/viral", tags=["viral"])


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
