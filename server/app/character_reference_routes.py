from __future__ import annotations

from fastapi import APIRouter, status
from pydantic import BaseModel, ConfigDict, Field

from app.auth import AuthenticatedUser, Database
from app.character_contracts import CharacterReferenceSelection
from app.character_reference_matching import (
    create_character_reference_selection,
    get_latest_character_reference_selection,
)

router = APIRouter(prefix="/api", tags=["character-references"])


class SelectCharacterReferencesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    selected_asset_ids: list[str] | None = Field(default=None, min_length=1, max_length=4)


@router.post(
    "/projects/{project_id}/character-reference-selection",
    response_model=CharacterReferenceSelection,
    status_code=status.HTTP_201_CREATED,
)
def select_character_references(
    project_id: str,
    request: SelectCharacterReferencesRequest,
    conn: Database,
    actor: AuthenticatedUser,
) -> CharacterReferenceSelection:
    return create_character_reference_selection(
        conn,
        actor=actor,
        project_id=project_id,
        selected_asset_ids=request.selected_asset_ids,
    )


@router.get(
    "/projects/{project_id}/character-reference-selection/latest",
    response_model=CharacterReferenceSelection,
)
def read_latest_character_reference_selection(
    project_id: str,
    conn: Database,
    actor: AuthenticatedUser,
) -> CharacterReferenceSelection:
    return get_latest_character_reference_selection(
        conn,
        actor=actor,
        project_id=project_id,
    )
