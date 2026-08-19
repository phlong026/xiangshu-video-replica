from __future__ import annotations

import hmac
import json
import time
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
from app.permissions import require_not_auditor, require_project_access, require_role
from app.settings import SettingsRepository, SettingsUnavailableError, settings_encryption_key
from app.storage import (
    StorageAdapter,
    StorageBackendUnavailable,
    cloud_storage_config_from_settings,
    create_local_storage_from_environment,
    create_storage_adapter,
    local_download_signature,
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
    """业务主存储：源参考视频（拆解需要 HTTPS URL）、人物图片、多视角
    图与首帧。配置了 COS 就上云；未配置退回本地盘（桌面单机场景）。"""
    try:
        repo = SettingsRepository(conn)
        config = repo.load_provider_config("cos")
        if config:
            return create_storage_adapter(cloud_storage_config_from_settings("cos", config))
        return create_local_storage_from_environment()
    except (SettingsUnavailableError, StorageBackendUnavailable, ValueError) as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "STORAGE_SETTINGS_UNAVAILABLE"},
        ) from exc


def get_local_result_storage(conn: Database) -> StorageAdapter:
    """生成结果归档存储：成片视频固定落本地盘不上云（按需节省云存储
    成本；读取按存储 URI 的 provider 路由，本地对象走 API 签名下载）。"""
    try:
        return create_local_storage_from_environment()
    except StorageBackendUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "STORAGE_SETTINGS_UNAVAILABLE"},
        ) from exc


def get_video_probe() -> VideoProbe:
    return FFprobeVideoProbe()


MediaStorage = Annotated[StorageAdapter, Depends(get_media_storage)]
LocalResultStorage = Annotated[StorageAdapter, Depends(get_local_result_storage)]
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
    prefix = "projects/"
    expected_content_type: str | None = None
    expected_size: int | None = None
    if object_key.startswith(prefix) and "/" in object_key[len(prefix) :]:
        require_not_auditor(
            conn,
            actor=actor,
            action="asset.object.put",
            entity_type="asset",
            entity_id=object_key,
        )
        project_id = object_key[len(prefix) :].split("/", 1)[0]
        require_project_access(
            conn,
            actor=actor,
            project_id=project_id,
            action="asset.object.put",
        )
    else:
        require_role(
            conn,
            actor=actor,
            allowed_roles={"admin"},
            action="asset.object.put",
            entity_type="asset",
            entity_id=object_key,
        )
        storage_uri = f"{storage.provider}://{storage.bucket}/{object_key}"
        pending = conn.execute(
            """
            SELECT id, kind, content_type, metadata_json
            FROM assets
            WHERE project_id IS NULL AND storage_uri = ? AND sha256 = '' AND size_bytes = 0
            """,
            (storage_uri,),
        ).fetchone()
        if pending is None:
            raise HTTPException(status_code=400, detail={"code": "INVALID_OBJECT_KEY"})
        try:
            metadata = json.loads(str(pending["metadata_json"]))
        except json.JSONDecodeError as exc:
            raise HTTPException(
                status_code=409,
                detail={"code": "IDENTITY_UPLOAD_INTENT_INVALID"},
            ) from exc
        if (
            str(pending["kind"]) not in {"character_authorization", "character_source_image"}
            or not isinstance(metadata, dict)
            or metadata.get("upload_status") != "PENDING"
            or metadata.get("object_key") != object_key
        ):
            raise HTTPException(
                status_code=409,
                detail={"code": "IDENTITY_UPLOAD_INTENT_INVALID"},
            )
        expected_content_type = str(pending["content_type"])
        requested_size = metadata.get("requested_size_bytes")
        if isinstance(requested_size, int):
            expected_size = requested_size
    content_length = request.headers.get("content-length")
    if content_length and content_length.isdigit() and int(content_length) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail={"code": "PAYLOAD_TOO_LARGE"})
    content = await request.body()
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail={"code": "PAYLOAD_TOO_LARGE"})
    content_type = request.headers.get("content-type", "application/octet-stream")
    if expected_content_type is not None and content_type != expected_content_type:
        raise HTTPException(status_code=415, detail={"code": "CONTENT_TYPE_MISMATCH"})
    if expected_size is not None and len(content) != expected_size:
        raise HTTPException(status_code=409, detail={"code": "UPLOAD_SIZE_MISMATCH"})
    storage.put_object(object_key, content, content_type=content_type)
    return Response(status_code=204)


@router.get("/local-objects/{object_key:path}")
def get_local_object(
    object_key: str,
    request: Request,
    storage: MediaStorage,
) -> Response:
    """Serve a local-storage object through the API.

    The download URL carries a short-lived HMAC signature (issued by
    create_download_url) because an <img> tag cannot attach the dev identity
    header. The signature is bound to the object key and expiry, so a leaked URL
    cannot be replayed after it expires.
    """
    if storage.provider != "local":
        raise HTTPException(status_code=404, detail={"code": "LOCAL_DOWNLOAD_UNAVAILABLE"})
    expires_at = request.query_params.get("expires")
    signature = request.query_params.get("sig")
    secret = settings_encryption_key()
    if (
        not expires_at
        or not signature
        or not secret
        or not expires_at.isdigit()
        or int(expires_at) < int(time.time())
        or not hmac.compare_digest(
            signature,
            local_download_signature(object_key, expires_at, secret=secret),
        )
    ):
        raise HTTPException(status_code=403, detail={"code": "LOCAL_DOWNLOAD_FORBIDDEN"})
    stored = storage.head_object(object_key)
    if stored is None:
        raise HTTPException(status_code=404, detail={"code": "OBJECT_NOT_FOUND"})
    content = storage.get_object(object_key)
    return Response(content=content, media_type=stored.content_type)
