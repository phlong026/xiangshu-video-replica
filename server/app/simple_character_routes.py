"""Routes for the simple character upload flow (方案 A: 极简人物库)."""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, ConfigDict

from app.auth import AuthenticatedUser, Database
from app.character_identity import character_error
from app.character_identity_routes import get_character_storage
from app.permissions import require_not_auditor, require_project_access
from app.simple_character import (
    SIMPLE_UPLOAD_MAX_BYTES,
    create_simple_character,
)
from app.storage import StorageAdapter

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/simple-characters", tags=["simple characters"])


class SimpleUploadIntentResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    generate_url: str
    method: str
    max_size_bytes: int
    allowed_content_types: list[str]


class SimpleCharacterResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    identity_id: str
    persona_id: str
    character_version_id: str
    publication_hash: str


@router.post("/upload-intent", response_model=SimpleUploadIntentResponse)
def create_simple_upload_intent(
    actor: AuthenticatedUser,
) -> SimpleUploadIntentResponse:
    """Describe how the client should upload a simple character source image.

    The simple flow uploads the image directly with the generate endpoint as
    multipart form data, so the intent only echoes the target endpoint and
    limits instead of issuing a presigned URL.
    """
    return SimpleUploadIntentResponse(
        generate_url="/api/simple-characters/generate",
        method="POST (multipart/form-data)",
        max_size_bytes=SIMPLE_UPLOAD_MAX_BYTES,
        allowed_content_types=["image/png", "image/jpeg"],
    )


@router.post(
    "/{project_id}/generate",
    response_model=SimpleCharacterResponse,
    status_code=201,
)
async def generate_simple_character(
    project_id: str,
    conn: Database,
    actor: AuthenticatedUser,
    storage: Annotated[StorageAdapter, Depends(get_character_storage)],
    file: Annotated[UploadFile, File()],
    display_name: Annotated[str, Form()],
    persona_name: Annotated[str, Form()] = "",
) -> SimpleCharacterResponse:
    """Upload one authorization image and publish a seven-view character.

    The image is stored as both the authorization proof and the source asset,
    the seven standard views are generated locally, auto-approved, and the
    resulting character version is published so it immediately appears in the
    project's available character version list.
    """
    require_not_auditor(
        conn,
        actor=actor,
        action="simple_character.create",
        entity_type="character_version",
        entity_id="collection",
    )
    require_project_access(
        conn,
        actor=actor,
        project_id=project_id,
        action="simple_character.create",
    )
    # Reject oversized uploads before reading the body into memory.
    if file.size is not None and file.size > SIMPLE_UPLOAD_MAX_BYTES:
        raise character_error(
            422,
            "SIMPLE_CHARACTER_IMAGE_TOO_LARGE",
            "人物授权图片超过 10MB 限制。",
        )
    content = await file.read()
    effective_persona_name = persona_name.strip() or display_name.strip()
    try:
        result = create_simple_character(
            conn,
            actor=actor,
            project_id=project_id,
            storage=storage,
            source_content=content,
            source_content_type=file.content_type or "application/octet-stream",
            display_name=display_name,
            persona_name=effective_persona_name,
        )
    except HTTPException:
        raise
    except Exception as exc:  # pragma: no cover - defensive guard
        logger.exception("Simple character generation failed unexpectedly")
        raise character_error(
            500,
            "SIMPLE_CHARACTER_GENERATION_FAILED",
            "一键生成人物失败，请稍后重试。",
        ) from exc
    return SimpleCharacterResponse(
        identity_id=result.identity_id,
        persona_id=result.persona_id,
        character_version_id=result.character_version_id,
        publication_hash=result.publication_hash,
    )
