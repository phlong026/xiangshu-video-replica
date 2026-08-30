"""Routes for the simple character upload flow (方案 A: 极简人物库)."""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from typing import Annotated
from uuid import uuid4

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
from starlette.concurrency import run_in_threadpool

from app.auth import AuthenticatedUser, Database
from app.character_asset_review import cleanup_publication_objects
from app.character_contracts import PersonIdentity, RequiredCharacterViewType
from app.character_identity import character_error, read_identity_row
from app.character_identity_routes import get_character_storage
from app.customer_fence import BusinessDbDep
from app.first_frame_routes import get_image_provider
from app.first_frames import ImageProvider
from app.image_tasks import (
    enqueue_character_sheet_task,
    latest_image_task,
    load_image_task,
    require_character_sheet_task_access,
)
from app.permissions import require_not_auditor, require_project_access
from app.rbac_routes import storage_for_asset
from app.simple_character import (
    SIMPLE_UPLOAD_ALLOWED_TYPES,
    SIMPLE_UPLOAD_MAX_BYTES,
    PreparedSimpleCharacterPublication,
    create_simple_character,
    delete_simple_character_identity,
    list_simple_library,
    prepare_simple_character_generation,
    regenerate_simple_character_contact_sheet,
    rename_simple_character_identity,
    store_simple_character_publication,
)
from app.storage import (
    StorageAdapter,
    StorageBackendUnavailable,
    StoragePermissionError,
    storage_object_ref_from_uri,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/simple-characters", tags=["simple characters"])

InjectedImageProvider = Annotated[ImageProvider, Depends(get_image_provider)]


def _best_effort_delete_task_input(storage: StorageAdapter, storage_uri: str) -> None:
    try:
        storage.delete_object(storage_object_ref_from_uri(storage_uri).key, actor_id=None)
    except (OSError, StorageBackendUnavailable, StoragePermissionError, ValueError):
        logger.warning("unable to clean temporary character task input", exc_info=True)


class SimpleUploadIntentResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    generate_url: str
    method: str
    max_size_bytes: int
    allowed_content_types: list[str]
    required_form_fields: list[str]
    task_status_url_template: str


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
    generation_source: str
    views: list[SimpleCharacterViewResponse]


class SimpleLibraryEntryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    identity_id: str
    display_name: str
    owner_user_id: str | None
    status: str
    contact_sheet_asset_id: str | None
    generation_source: str | None
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
    generation_source: str
    views: list[SimpleCharacterViewResponse]


class IdentityRenameRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str


class CharacterSheetTaskResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    project_id: str | None
    identity_id: str | None
    operation: str
    display_name: str
    status: str
    attempt: int
    result_identity_id: str | None
    result_version_id: str | None
    result: dict[str, object] | None
    error_code: str | None
    error_message: str | None
    retryable: bool
    created_at: str
    updated_at: str
    started_at: str | None
    completed_at: str | None


@router.post("/upload-intent", response_model=SimpleUploadIntentResponse)
def create_simple_upload_intent(
    actor: AuthenticatedUser,
) -> SimpleUploadIntentResponse:
    """Describe how the client should upload a simple character source image.

    The simple flow uploads the image directly with the asynchronous task
    endpoint as multipart form data, so the intent echoes the enqueue contract
    and limits instead of issuing a presigned URL.
    """
    return SimpleUploadIntentResponse(
        generate_url="/api/simple-characters/tasks/generate",
        method="POST (multipart/form-data)",
        max_size_bytes=SIMPLE_UPLOAD_MAX_BYTES,
        allowed_content_types=["image/png", "image/jpeg"],
        required_form_fields=["file", "display_name", "idempotency_key"],
        task_status_url_template="/api/simple-characters/task-status/{task_id}",
    )


@router.post("/generate", response_model=SimpleCharacterResponse, status_code=201)
async def generate_global_simple_character(
    storage: Annotated[StorageAdapter, Depends(get_character_storage)],
    provider: InjectedImageProvider,
    db: BusinessDbDep,
    file: Annotated[UploadFile, File()],
    display_name: Annotated[str, Form()],
    persona_name: Annotated[str, Form()] = "",
) -> SimpleCharacterResponse:
    with db.write() as (conn, actor):
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
    (
        content,
        content_type,
        effective_persona_name,
        prepared,
    ) = await _prepare_simple_character_upload(
        file=file,
        display_name=display_name,
        persona_name=persona_name,
        provider=provider,
        actor=actor,
        storage=storage,
    )
    try:
        with db.write() as (conn, actor):
            return await _run_simple_character_creation(
                conn=conn,
                actor=actor,
                storage=storage,
                content=content,
                content_type=content_type,
                display_name=display_name,
                persona_name=effective_persona_name,
                project_id=None,
                prepared_publication=prepared,
            )
    except Exception:
        cleanup_publication_objects(storage, list(prepared.object_keys))
        raise


@router.post(
    "/tasks/generate",
    response_model=CharacterSheetTaskResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def enqueue_global_simple_character(
    storage: Annotated[StorageAdapter, Depends(get_character_storage)],
    db: BusinessDbDep,
    file: Annotated[UploadFile, File()],
    display_name: Annotated[str, Form()],
    idempotency_key: Annotated[str, Form()],
    persona_name: Annotated[str, Form()] = "",
) -> CharacterSheetTaskResponse:
    return await _enqueue_simple_character_upload(
        storage=storage,
        db=db,
        file=file,
        display_name=display_name,
        idempotency_key=idempotency_key,
        persona_name=persona_name,
        project_id=None,
    )


@router.post(
    "/tasks/{project_id}/generate",
    response_model=CharacterSheetTaskResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def enqueue_project_simple_character(
    project_id: str,
    storage: Annotated[StorageAdapter, Depends(get_character_storage)],
    db: BusinessDbDep,
    file: Annotated[UploadFile, File()],
    display_name: Annotated[str, Form()],
    idempotency_key: Annotated[str, Form()],
    persona_name: Annotated[str, Form()] = "",
) -> CharacterSheetTaskResponse:
    return await _enqueue_simple_character_upload(
        storage=storage,
        db=db,
        file=file,
        display_name=display_name,
        idempotency_key=idempotency_key,
        persona_name=persona_name,
        project_id=project_id,
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
            generation_source=entry.generation_source,
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
    db: BusinessDbDep,
) -> PersonIdentity:
    with db.write() as (conn, actor):
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
    storage: Annotated[StorageAdapter, Depends(get_character_storage)],
    provider: InjectedImageProvider,
    db: BusinessDbDep,
) -> SimpleCharacterRegenerationResponse:
    with db.write() as (conn, actor):
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
            generation_source=result.generation_source,
            views=[
                SimpleCharacterViewResponse(
                    view_type=view.view_type,
                    asset_id=view.asset_id,
                )
                for view in result.views
            ],
        )


@router.post(
    "/identities/{identity_id}/regenerate-contact-sheet-task",
    response_model=CharacterSheetTaskResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def enqueue_regenerate_contact_sheet(
    identity_id: str,
    idempotency_key: Annotated[str, Form()],
    db: BusinessDbDep,
) -> CharacterSheetTaskResponse:
    if len(idempotency_key.strip()) < 8:
        raise character_error(422, "IDEMPOTENCY_KEY_REQUIRED", "请重新提交生成请求。")
    with db.write() as (conn, actor):
        require_not_auditor(
            conn,
            actor=actor,
            action="simple_character.regenerate",
            entity_type="character_version",
            entity_id=identity_id,
        )
        identity = read_identity_row(conn, identity_id)
        if actor.role != "admin" and str(identity["owner_user_id"]) != actor.id:
            raise character_error(
                403,
                "IDENTITY_REGENERATE_FORBIDDEN",
                "只有创建者或管理员可以重新生成多视图。",
            )
        source_asset = conn.execute(
            "SELECT storage_uri, content_type, sha256, size_bytes FROM assets WHERE id = %s",
            (str(identity["source_asset_id"]),),
        ).fetchone()
        if source_asset is None:
            raise character_error(409, "SIMPLE_CHARACTER_SOURCE_MISSING", "人物缺少原始授权照片。")
        row = enqueue_character_sheet_task(
            conn,
            actor=actor,
            operation="REGENERATE",
            project_id=None,
            identity_id=identity_id,
            display_name=str(identity["display_name"]),
            persona_name=str(identity["display_name"]),
            source_storage_uri=str(source_asset["storage_uri"]),
            source_content_type=str(source_asset["content_type"]),
            source_sha256=str(source_asset["sha256"]),
            source_size_bytes=int(source_asset["size_bytes"]),
            idempotency_key=idempotency_key.strip(),
        )
        return character_sheet_task_response(row)


@router.get("/task-status/{task_id}", response_model=CharacterSheetTaskResponse)
def read_character_sheet_task(
    task_id: str,
    conn: Database,
    actor: AuthenticatedUser,
) -> CharacterSheetTaskResponse:
    row = load_image_task(conn, table="character_sheet_tasks", task_id=task_id)
    require_character_sheet_task_access(actor=actor, row=row)
    return character_sheet_task_response(row)


@router.get("/tasks/active-or-latest", response_model=CharacterSheetTaskResponse | None)
def read_latest_character_sheet_task(
    conn: Database,
    actor: AuthenticatedUser,
) -> CharacterSheetTaskResponse | None:
    row = latest_image_task(
        conn,
        table="character_sheet_tasks",
        owner_column="created_by_user_id",
        owner_id=actor.id,
    )
    return None if row is None else character_sheet_task_response(row)


@router.delete("/identities/{identity_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_identity(
    identity_id: str,
    db: BusinessDbDep,
) -> Response:
    with db.write() as (conn, actor):
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
    storage: Annotated[StorageAdapter, Depends(get_character_storage)],
    provider: InjectedImageProvider,
    db: BusinessDbDep,
    file: Annotated[UploadFile, File()],
    display_name: Annotated[str, Form()],
    persona_name: Annotated[str, Form()] = "",
) -> SimpleCharacterResponse:
    with db.write() as (conn, actor):
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
    (
        content,
        content_type,
        effective_persona_name,
        prepared,
    ) = await _prepare_simple_character_upload(
        file=file,
        display_name=display_name,
        persona_name=persona_name,
        provider=provider,
        actor=actor,
        storage=storage,
    )
    try:
        with db.write() as (conn, actor):
            return await _run_simple_character_creation(
                conn=conn,
                actor=actor,
                storage=storage,
                content=content,
                content_type=content_type,
                display_name=display_name,
                persona_name=effective_persona_name,
                project_id=project_id,
                prepared_publication=prepared,
            )
    except Exception:
        cleanup_publication_objects(storage, list(prepared.object_keys))
        raise


async def _prepare_simple_character_upload(
    *,
    file: UploadFile,
    display_name: str,
    persona_name: str,
    provider: ImageProvider,
    actor: AuthenticatedUser,
    storage: StorageAdapter,
) -> tuple[bytes, str, str, PreparedSimpleCharacterPublication]:
    """Render and archive every character object with no session row locked."""

    if file.size is not None and file.size > SIMPLE_UPLOAD_MAX_BYTES:
        raise character_error(
            422,
            "SIMPLE_CHARACTER_IMAGE_TOO_LARGE",
            "人物授权图片超过 10MB 限制。",
        )
    content = await file.read()
    content_type = file.content_type or "application/octet-stream"
    effective_persona_name = persona_name.strip() or display_name.strip()
    generation = await run_in_threadpool(
        prepare_simple_character_generation,
        source_content=content,
        source_content_type=content_type,
        display_name=display_name,
        image_provider=provider,
    )
    prepared = await run_in_threadpool(
        store_simple_character_publication,
        actor=actor,
        storage=storage,
        source_content=content,
        source_content_type=content_type,
        display_name=display_name,
        generation=generation,
    )
    return content, content_type, effective_persona_name, prepared


async def _run_simple_character_creation(
    *,
    conn: Database,
    actor: AuthenticatedUser,
    storage: StorageAdapter,
    content: bytes,
    content_type: str,
    display_name: str,
    persona_name: str,
    project_id: str | None,
    prepared_publication: PreparedSimpleCharacterPublication,
) -> SimpleCharacterResponse:
    """Shared body of the global and project-scoped generate endpoints."""
    try:
        # Provider, image and object-storage work is already complete. Keep
        # the short database publication off the FastAPI event loop as well.
        result = await run_in_threadpool(
            create_simple_character,
            conn,
            actor=actor,
            project_id=project_id,
            storage=storage,
            source_content=content,
            source_content_type=content_type,
            display_name=display_name,
            persona_name=persona_name,
            prepared_publication=prepared_publication,
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
        generation_source=result.generation_source,
        views=[
            SimpleCharacterViewResponse(
                view_type=view.view_type,
                asset_id=view.asset_id,
            )
            for view in result.views
        ],
    )


async def _enqueue_simple_character_upload(
    *,
    storage: StorageAdapter,
    db: BusinessDbDep,
    file: UploadFile,
    display_name: str,
    idempotency_key: str,
    persona_name: str,
    project_id: str | None,
) -> CharacterSheetTaskResponse:
    clean_name = display_name.strip()
    clean_key = idempotency_key.strip()
    if not clean_name:
        raise character_error(422, "SIMPLE_CHARACTER_NAME_REQUIRED", "人物名称不能为空。")
    if len(clean_key) < 8:
        raise character_error(422, "IDEMPOTENCY_KEY_REQUIRED", "请重新提交生成请求。")

    # Reject unauthorized callers before reading or persisting their upload.
    # Authorization is checked again in the enqueue transaction below to
    # protect the interval between this preflight and the database write.
    with db.write() as (conn, actor):
        require_not_auditor(
            conn,
            actor=actor,
            action="simple_character.task.create",
            entity_type="character_sheet_task",
            entity_id=project_id or "collection",
        )
        if project_id is not None:
            require_project_access(
                conn,
                actor=actor,
                project_id=project_id,
                action="simple_character.task.create",
            )

    if file.size is not None and file.size > SIMPLE_UPLOAD_MAX_BYTES:
        raise character_error(
            422, "SIMPLE_CHARACTER_IMAGE_TOO_LARGE", "人物授权图片超过 10MB 限制。"
        )
    content = await file.read()
    if len(content) > SIMPLE_UPLOAD_MAX_BYTES:
        raise character_error(
            422, "SIMPLE_CHARACTER_IMAGE_TOO_LARGE", "人物授权图片超过 10MB 限制。"
        )
    content_type = (file.content_type or "application/octet-stream").split(";", 1)[0].lower()
    if content_type not in SIMPLE_UPLOAD_ALLOWED_TYPES or not content:
        raise character_error(
            422, "SIMPLE_CHARACTER_IMAGE_INVALID", "请上传有效的 PNG 或 JPEG 图片。"
        )
    content_sha256 = hashlib.sha256(content).hexdigest()
    task_input_id = str(uuid4())
    extension = SIMPLE_UPLOAD_ALLOWED_TYPES[content_type]
    input_key = f"users/task-inputs/{task_input_id}.{extension}"
    stored = await run_in_threadpool(
        storage.put_object,
        input_key,
        content,
        content_type=content_type,
    )
    try:
        with db.write() as (conn, actor):
            require_not_auditor(
                conn,
                actor=actor,
                action="simple_character.task.create",
                entity_type="character_sheet_task",
                entity_id=task_input_id,
            )
            if project_id is not None:
                require_project_access(
                    conn,
                    actor=actor,
                    project_id=project_id,
                    action="simple_character.task.create",
                )
            row = enqueue_character_sheet_task(
                conn,
                actor=actor,
                operation="CREATE",
                project_id=project_id,
                identity_id=None,
                display_name=clean_name,
                persona_name=persona_name.strip() or clean_name,
                source_storage_uri=stored.uri,
                source_content_type=content_type,
                source_sha256=content_sha256,
                source_size_bytes=len(content),
                idempotency_key=clean_key,
            )
        if str(row["source_storage_uri"]) != stored.uri:
            _best_effort_delete_task_input(storage, stored.uri)
        return character_sheet_task_response(row)
    except Exception:
        _best_effort_delete_task_input(storage, stored.uri)
        raise


def character_sheet_task_response(row: sqlite3.Row) -> CharacterSheetTaskResponse:
    task = row
    request_payload = json.loads(str(task["request_json"]))
    result_payload = None if task["result_json"] is None else json.loads(str(task["result_json"]))
    return CharacterSheetTaskResponse(
        id=str(task["id"]),
        project_id=(None if task["project_id"] is None else str(task["project_id"])),
        identity_id=(None if task["identity_id"] is None else str(task["identity_id"])),
        operation=str(task["operation"]),
        display_name=str(request_payload.get("display_name") or "人物"),
        status=str(task["status"]),
        attempt=int(task["attempt"]),
        result_identity_id=(
            None if task["result_identity_id"] is None else str(task["result_identity_id"])
        ),
        result_version_id=(
            None if task["result_version_id"] is None else str(task["result_version_id"])
        ),
        result=result_payload,
        error_code=(None if task["error_code"] is None else str(task["error_code"])),
        error_message=(
            None if task["error_message_redacted"] is None else str(task["error_message_redacted"])
        ),
        retryable=bool(task["retryable"]),
        created_at=str(task["created_at"]),
        updated_at=str(task["updated_at"]),
        started_at=(None if task["started_at"] is None else str(task["started_at"])),
        completed_at=(None if task["completed_at"] is None else str(task["completed_at"])),
    )
