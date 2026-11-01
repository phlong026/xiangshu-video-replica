from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.auth import AuthenticatedUser, Database
from app.character_contracts import ProjectCharacterVersionOption
from app.characters import get_project_main_character
from app.customer_fence import BusinessDbDep
from app.permissions import require_not_auditor, require_project_access
from app.project_character_selection import (
    choose_project_character_version,
    list_available_project_character_versions,
)

router = APIRouter(prefix="/api", tags=["characters"])

# 旧轨 /api/characters 的 CRUD 路由（GET 列表/POST/PATCH/DELETE/单读）已于
# T1 第二步（2026-11-02 准备分支）下线：前端全面走新轨端点（simple-characters
# / person-identities / character-personas / character-versions），仓库内与
# e2e 零调用；生产侧 90 天流量核查见 docs/人物域双轨对账与下线核查清单。
# characters 表与读函数保留（生成流程在用），历史数据不动。


class ProjectMainCharacterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # character_id 旧形态随旧轨一并下线：仓库内前端仅发 version 形态；
    # 旧版桌面客户端若仍发 character_id 将得到 422（G1 流量核查的放行前提
    # 就是该形态已无在途调用）。
    character_version_id: str = Field(min_length=1)


class ProjectMainCharacterResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str
    character_id: str | None
    character_version_id: str | None
    version_id: str
    version_number: int
    character_snapshot: dict[str, object]


@router.get(
    "/projects/{project_id}/main-character",
    response_model=ProjectMainCharacterResponse | None,
)
def read_project_main_character_route(
    project_id: str,
    conn: Database,
    actor: AuthenticatedUser,
) -> ProjectMainCharacterResponse | None:
    require_project_access(
        conn,
        actor=actor,
        project_id=project_id,
        action="project.main_character.read",
    )
    try:
        character = get_project_main_character(conn, project_id=project_id)
    except HTTPException as exc:
        if (
            exc.status_code == 404
            and isinstance(exc.detail, dict)
            and exc.detail.get("code") == "MAIN_CHARACTER_NOT_FOUND"
        ):
            return None
        raise
    return ProjectMainCharacterResponse.model_validate(character)


@router.get(
    "/projects/{project_id}/character-versions/available",
    response_model=list[ProjectCharacterVersionOption],
)
def read_available_project_character_versions(
    project_id: str,
    conn: Database,
    actor: AuthenticatedUser,
) -> list[ProjectCharacterVersionOption]:
    require_project_access(
        conn,
        actor=actor,
        project_id=project_id,
        action="project.main_character.options.read",
    )
    return list_available_project_character_versions(conn, actor=actor, project_id=project_id)


@router.put(
    "/projects/{project_id}/main-character",
    response_model=ProjectMainCharacterResponse,
)
def choose_main_character_route(
    project_id: str,
    payload: ProjectMainCharacterRequest,
    db: BusinessDbDep,
) -> ProjectMainCharacterResponse:
    with db.write() as (conn, actor):
        require_not_auditor(
            conn,
            actor=actor,
            action="project.main_character.choose",
            entity_type="project",
            entity_id=project_id,
        )
        require_project_access(
            conn,
            actor=actor,
            project_id=project_id,
            action="project.main_character.choose",
        )
        result = choose_project_character_version(
            conn,
            actor=actor,
            project_id=project_id,
            character_version_id=payload.character_version_id,
        )
        return ProjectMainCharacterResponse.model_validate(result)
