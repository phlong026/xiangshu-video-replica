"""项目来源恢复接口，写入复用现有会话事务守卫。"""

from fastapi import APIRouter

from app.auth import ReadDatabase, ShortAuthUser
from app.customer_fence import BusinessDbDep
from app.project_sources import (
    BindProjectSource,
    CreateProjectSource,
    ProjectSourceResponse,
    bind_project_source,
    create_project_source,
    current_project_source,
    source_response,
)

router = APIRouter(prefix="/api/projects", tags=["project-sources"])


@router.post("/{project_id}/sources", response_model=ProjectSourceResponse)
def create_source(
    project_id: str, request: CreateProjectSource, db: BusinessDbDep
) -> ProjectSourceResponse:
    with db.write() as (conn, actor):
        return source_response(
            create_project_source(conn, actor=actor, project_id=project_id, request=request)
        )


@router.get("/{project_id}/sources/current", response_model=ProjectSourceResponse | None)
def read_source(
    project_id: str, conn: ReadDatabase, actor: ShortAuthUser
) -> ProjectSourceResponse | None:
    row = current_project_source(conn, actor=actor, project_id=project_id)
    return source_response(row) if row is not None else None


@router.post("/{project_id}/sources/{source_id}/bind", response_model=ProjectSourceResponse)
def bind_source(
    project_id: str, source_id: str, request: BindProjectSource, db: BusinessDbDep
) -> ProjectSourceResponse:
    with db.write() as (conn, actor):
        return source_response(
            bind_project_source(
                conn, actor=actor, project_id=project_id, source_id=source_id, request=request
            )
        )
