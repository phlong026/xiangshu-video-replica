from __future__ import annotations

from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from app.auth import AuthenticatedUser, Database
from app.media import (
    MAX_UPLOAD_BYTES,
    FFprobeVideoProbe,
    VideoMetadata,
    VideoProbe,
    complete_upload,
    create_upload_intent,
)
from app.permissions import require_not_auditor, require_project_access
from app.settings import SettingsDecryptError, SettingsKeyMissing, SettingsRepository
from app.storage import (
    StorageAdapter,
    StorageBackendUnavailable,
    cloud_storage_config_from_settings,
    create_local_storage_from_environment,
    create_storage_adapter,
)

router = APIRouter(prefix="/api/assets", tags=["media"])
LOCAL_API_BASE_URL = "http://127.0.0.1:8000"


class UploadIntentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(min_length=1)
    filename: str = Field(min_length=1)
    content_type: str = Field(min_length=1)
    size_bytes: int = Field(ge=0)


class UploadIntentResponse(BaseModel):
    asset_id: str
    project_id: str
    storage_key: str
    method: str
    url: str
    headers: dict[str, str]
    expires_at: str


class CompleteUploadResponse(BaseModel):
    asset_id: str
    project_id: str
    status: str
    storage_uri: str
    sha256: str
    size_bytes: int
    content_type: str
    metadata: VideoMetadata


def get_media_storage(conn: Database) -> StorageAdapter:
    try:
        repo = SettingsRepository(conn)
        runtime = repo.read_runtime_settings()
        provider = str(runtime["active_storage_provider"])
        if provider == "local":
            return create_local_storage_from_environment()
        config = repo.load_provider_config(provider)
        return create_storage_adapter(cloud_storage_config_from_settings(provider, config))
    except (SettingsDecryptError, SettingsKeyMissing, StorageBackendUnavailable, ValueError) as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "STORAGE_SETTINGS_UNAVAILABLE"},
        ) from exc


def get_video_probe() -> VideoProbe:
    return FFprobeVideoProbe()


MediaStorage = Annotated[StorageAdapter, Depends(get_media_storage)]
InjectedVideoProbe = Annotated[VideoProbe, Depends(get_video_probe)]


@router.post("/upload-intent", response_model=UploadIntentResponse)
def create_asset_upload_intent(
    payload: UploadIntentRequest,
    conn: Database,
    actor: AuthenticatedUser,
    storage: MediaStorage,
) -> UploadIntentResponse:
    intent = create_upload_intent(
        conn,
        actor=actor,
        storage=storage,
        project_id=payload.project_id,
        filename=payload.filename,
        content_type=payload.content_type,
        size_bytes=payload.size_bytes,
    )
    upload_url = intent.url
    if storage.provider == "local":
        # The client cannot PUT to a `local://` scheme URL; route uploads through
        # the local server endpoint instead so the desktop app can upload files.
        upload_url = (
            f"{LOCAL_API_BASE_URL}/api/assets/local-objects/{quote(intent.storage_key, safe='/')}"
        )
    return UploadIntentResponse(
        asset_id=intent.asset_id,
        project_id=intent.project_id,
        storage_key=intent.storage_key,
        method=intent.method,
        url=upload_url,
        headers=intent.headers,
        expires_at=intent.expires_at,
    )


@router.post("/{asset_id}/complete", response_model=CompleteUploadResponse)
def complete_asset_upload(
    asset_id: str,
    conn: Database,
    actor: AuthenticatedUser,
    storage: MediaStorage,
    probe: InjectedVideoProbe,
) -> CompleteUploadResponse:
    completed = complete_upload(conn, actor=actor, storage=storage, probe=probe, asset_id=asset_id)
    return CompleteUploadResponse(
        asset_id=completed.asset_id,
        project_id=completed.project_id,
        status=completed.status,
        storage_uri=completed.storage_uri,
        sha256=completed.sha256,
        size_bytes=completed.size_bytes,
        content_type=completed.content_type,
        metadata=completed.metadata,
    )


@router.put("/local-objects/{object_key:path}")
async def put_local_object(
    object_key: str,
    request: Request,
    conn: Database,
    actor: AuthenticatedUser,
    storage: MediaStorage,
) -> Response:
    """Receive a raw PUT body and write it to the local storage adapter.

    Only reachable when the runtime storage provider is `local`; the desktop
    client uploads the reference video here instead of a `local://` scheme URL.
    Keeps the same role/project gates as the cloud intent flow: auditors are
    read-only and only the project owner/admin may write objects.
    """
    if storage.provider != "local":
        raise HTTPException(status_code=404, detail={"code": "LOCAL_UPLOAD_UNAVAILABLE"})
    require_not_auditor(
        conn,
        actor=actor,
        action="asset.object.put",
        entity_type="asset",
        entity_id=object_key,
    )
    prefix = "projects/"
    if not object_key.startswith(prefix) or "/" not in object_key[len(prefix) :]:
        raise HTTPException(status_code=400, detail={"code": "INVALID_OBJECT_KEY"})
    project_id = object_key[len(prefix) :].split("/", 1)[0]
    require_project_access(
        conn,
        actor=actor,
        project_id=project_id,
        action="asset.object.put",
    )
    content = await request.body()
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail={"code": "PAYLOAD_TOO_LARGE"})
    content_type = request.headers.get("content-type", "application/octet-stream")
    storage.put_object(object_key, content, content_type=content_type)
    return Response(status_code=204)
