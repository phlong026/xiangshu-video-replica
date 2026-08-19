"""Minimal routes for the WIP simple character upload contract."""

from __future__ import annotations

from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict

from app.auth import AuthenticatedUser

router = APIRouter(prefix="/api/simple-characters", tags=["simple characters"])


class SimpleCharacterUploadIntentResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    upload_url: str
    asset_id: str
    method: str
    expires_in: int


class SimpleCharacterUploadResultResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version_id: str
    persona_id: str
    identity_id: str
    combined_asset_url: str
    view_assets: list[tuple[str, str]]


@router.post("/upload-intent", response_model=SimpleCharacterUploadIntentResponse)
def create_simple_character_upload_intent(
    actor: AuthenticatedUser,
) -> SimpleCharacterUploadIntentResponse:
    asset_id = f"simple-character-source-{uuid4().hex}"
    return SimpleCharacterUploadIntentResponse(
        upload_url=f"/api/simple-characters/uploads/{asset_id}",
        asset_id=asset_id,
        method="PUT",
        expires_in=900,
    )


@router.put("/uploads/{asset_id}", status_code=status.HTTP_204_NO_CONTENT)
async def receive_simple_character_upload(
    asset_id: str,
    request: Request,
    actor: AuthenticatedUser,
) -> Response:
    # The WIP branch keeps the upload contract available while generation is
    # connected to the durable character pipeline in a later increment.
    await request.body()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{project_id}/generate",
    response_model=SimpleCharacterUploadResultResponse,
    status_code=status.HTTP_501_NOT_IMPLEMENTED,
)
async def generate_simple_character(
    project_id: str,
    request: Request,
    actor: AuthenticatedUser,
) -> SimpleCharacterUploadResultResponse:
    await request.body()
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail={
            "code": "SIMPLE_CHARACTER_GENERATION_NOT_IMPLEMENTED",
            "message": "极简人物生成流水线尚未接入。",
        },
    )
