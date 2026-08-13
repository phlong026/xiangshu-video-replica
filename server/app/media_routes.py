from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from app.auth import AuthenticatedUser, Database
from app.media import (
    FFprobeVideoProbe,
    VideoMetadata,
    VideoProbe,
    complete_upload,
    create_upload_intent,
)
from app.storage import LocalStorageAdapter, StorageAdapter

STORAGE_ROOT_ENV = "VIDEO_REPLICA_STORAGE_ROOT"

router = APIRouter(prefix="/api/assets", tags=["media"])


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


def get_media_storage() -> StorageAdapter:
    root = Path(os.environ.get(STORAGE_ROOT_ENV, "/tmp/video-replica-storage"))
    return LocalStorageAdapter(root=root)


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
    return UploadIntentResponse(
        asset_id=intent.asset_id,
        project_id=intent.project_id,
        storage_key=intent.storage_key,
        method=intent.method,
        url=intent.url,
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
