"""Routes for the simple character upload flow (方案 A: 极简人物库)."""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Response,
    UploadFile,
    status,
)
from pydantic import BaseModel, ConfigDict

from app.auth import AuthenticatedUser, Database
from app.character_contracts import PersonIdentity, RequiredCharacterViewType
from app.character_identity import character_error
from app.character_identity_routes import get_character_storage
from app.first_frame_routes import get_image_provider
from app.first_frames import ImageProvider
from app.permissions import require_not_auditor, require_project_access
from app.rbac_routes import storage_for_asset
from app.simple_character import (
    SIMPLE_UPLOAD_MAX_BYTES,
    create_simple_character,
    delete_simple_character_identity,
    list_simple_library,
    regenerate_simple_character_contact_sheet,
    rename_simple_character_identity,
)
from app.storage import StorageAdapter

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/simple-characters", tags=["simple characters"])

InjectedImageProvider = Annotated[ImageProvider, Depends(get_image_provider)]


class SimpleUploadIntentResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    generate_url: str
    method: str
    max_size_bytes: int
    allowed_content_types: list[str]


class SimpleCharacterViewResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    view_type: RequiredCharacterViewType
    asset_id: str


class SimpleCharacterResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    identity_id: str
    persona_id: str
    character_version_id: str
    publication_hash: str
    contact_sheet_asset_id: str
    views: list[SimpleCharacterViewResponse]


class SimpleLibraryEntryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    identity_id: str
    display_name: str
    owner_user_id: str | None
    status: str
    contact_sheet_asset_id: str | None
    views: list[SimpleCharacterViewResponse]


class SimpleCharacterRegenerationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    identity_id: str
    persona_id: str
    character_version_id: str
    previous_version_id: str
    version_number: int
    publication_hash: str
    contact_sheet_asset_id: str
    views: list[SimpleCharacterViewResponse]


class IdentityRenameRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str


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


@router.post("/generate", response_model=SimpleCharacterResponse, status_code=201)
async def generate_global_simple_character(
    conn: Database,
    actor: AuthenticatedUser,
    storage: Annotated[StorageAdapter, Depends(get_character_storage)],
    provider: InjectedImageProvider,
    file: Annotated[UploadFile, File()],
    display_name: Annotated[str, Form()],
    persona_name: Annotated[str, Form()] = "",
) -> SimpleCharacterResponse:
    """Global one-click character creation (人物库精简流程，无项目上下文).

    Mirrors the project-scoped endpoint but skips ``require_project_access``:
    the character library page has no project context, and the creator's
    identity ownership is recorded for later renames.
    """
    require_not_auditor(
        conn,
        actor=actor,
        action="simple_character.create",
        entity_type="character_version",
        entity_id="collection",
    )
    return await _run_simple_character_creation(
        conn=conn,
        actor=actor,
        storage=storage,
        provider=provider,
        file=file,
        display_name=display_name,
        persona_name=persona_name,
        project_id=None,
    )


@router.get("/library", response_model=list[SimpleLibraryEntryResponse])
def read_simple_library(
    conn: Database,
    actor: AuthenticatedUser,
) -> list[SimpleLibraryEntryResponse]:
    """List characters with their contact sheet and seven-view asset ids."""
    return [
        SimpleLibraryEntryResponse(
            identity_id=entry.identity_id,
            display_name=entry.display_name,
            owner_user_id=entry.owner_user_id,
            status=entry.status,
            contact_sheet_asset_id=entry.contact_sheet_asset_id,
            views=[
                SimpleCharacterViewResponse(
                    view_type=view.view_type,
                    asset_id=view.asset_id,
                )
                for view in entry.views
            ],
        )
        for entry in list_simple_library(conn, actor=actor)
    ]


@router.patch("/identities/{identity_id}/name")
def rename_identity(
    identity_id: str,
    request: IdentityRenameRequest,
    conn: Database,
    actor: AuthenticatedUser,
) -> PersonIdentity:
    """Rename a character identity (owner or admin only)."""
    return rename_simple_character_identity(
        conn,
        actor=actor,
        identity_id=identity_id,
        display_name=request.display_name,
    )


@router.post(
    "/identities/{identity_id}/regenerate-contact-sheet",
    response_model=SimpleCharacterRegenerationResponse,
    status_code=201,
)
def regenerate_contact_sheet(
    identity_id: str,
    conn: Database,
    actor: AuthenticatedUser,
    storage: Annotated[StorageAdapter, Depends(get_character_storage)],
    provider: InjectedImageProvider,
) -> SimpleCharacterRegenerationResponse:
    """Re-run the five-view contact sheet from the original source photo.

    Reuses the identity's stored authorization photo with the same
    identity-preserve prompt, publishes the result as the next character
    version, and keeps the previous published version untouched so projects
    already bound to it continue to work.
    """
    require_not_auditor(
        conn,
        actor=actor,
        action="simple_character.regenerate",
        entity_type="character_version",
        entity_id=identity_id,
    )
    try:
        result = regenerate_simple_character_contact_sheet(
            conn,
            actor=actor,
            identity_id=identity_id,
            storage=storage,
            image_provider=provider,
        )
    except HTTPException:
        raise
    except Exception as exc:  # pragma: no cover - defensive guard
        logger.exception("Simple character regeneration failed unexpectedly")
        raise character_error(
            500,
            "SIMPLE_CHARACTER_REGENERATION_FAILED",
            "重新生成多视图失败，请稍后重试。",
        ) from exc
    return SimpleCharacterRegenerationResponse(
        identity_id=result.identity_id,
        persona_id=result.persona_id,
        character_version_id=result.character_version_id,
        previous_version_id=result.previous_version_id,
        version_number=result.version_number,
        publication_hash=result.publication_hash,
        contact_sheet_asset_id=result.contact_sheet_asset_id,
        views=[
            SimpleCharacterViewResponse(
                view_type=view.view_type,
                asset_id=view.asset_id,
            )
            for view in result.views
        ],
    )


@router.delete("/identities/{identity_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_identity(
    identity_id: str,
    conn: Database,
    actor: AuthenticatedUser,
) -> Response:
    """Delete a character identity with all derived assets (owner or admin)."""
    delete_simple_character_identity(
        conn,
        actor=actor,
        identity_id=identity_id,
        storage_for_uri=storage_for_asset,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


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
    provider: InjectedImageProvider,
    file: Annotated[UploadFile, File()],
    display_name: Annotated[str, Form()],
    persona_name: Annotated[str, Form()] = "",
) -> SimpleCharacterResponse:
    """Upload one authorization image and publish a seven-view character.

    The image is stored as both the authorization proof and the source asset,
    a single seven-view contact sheet plus the seven standard views are
    generated, auto-approved, and the resulting character version is
    published so it immediately appears in the project's available character
    version list.
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
    return await _run_simple_character_creation(
        conn=conn,
        actor=actor,
        storage=storage,
        provider=provider,
        file=file,
        display_name=display_name,
        persona_name=persona_name,
        project_id=project_id,
    )


async def _run_simple_character_creation(
    *,
    conn: Database,
    actor: AuthenticatedUser,
    storage: StorageAdapter,
    provider: ImageProvider,
    file: UploadFile,
    display_name: str,
    persona_name: str,
    project_id: str | None,
) -> SimpleCharacterResponse:
    """Shared body of the global and project-scoped generate endpoints."""
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
            image_provider=provider,
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
        contact_sheet_asset_id=result.contact_sheet_asset_id,
        views=[
            SimpleCharacterViewResponse(
                view_type=view.view_type,
                asset_id=view.asset_id,
            )
            for view in result.views
        ],
    )
