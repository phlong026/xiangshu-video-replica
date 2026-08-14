from __future__ import annotations

import json
import sqlite3
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.auth import AuthenticatedUser, Database
from app.first_frames import (
    FIRST_FRAME_SELECTION_KIND,
    FakeImageProvider,
    ImageProvider,
    confirm_first_frame,
    current_first_frame_candidates,
    generate_first_frame_candidates,
)
from app.media_routes import get_media_storage
from app.permissions import require_project_access
from app.source_frames import latest_version
from app.storage import StorageAdapter

router = APIRouter(prefix="/api", tags=["first-frames"])


class GenerateFirstFramesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: Literal["gpt-image-2", "nano-banana-pro-2k"] = "nano-banana-pro-2k"
    prompt: str | None = Field(default=None, max_length=4000)
    quantity: int = Field(default=1, ge=1, le=3)


class ConfirmFirstFrameRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    first_frame_asset_id: str = Field(min_length=1)


class VersionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    project_id: str
    asset_id: str | None
    kind: str
    version_number: int
    payload: dict[str, Any]
    created_by_user_id: str | None
    created_at: str


def get_image_provider() -> ImageProvider:
    return FakeImageProvider()


FirstFrameStorage = Annotated[StorageAdapter, Depends(get_media_storage)]
InjectedImageProvider = Annotated[ImageProvider, Depends(get_image_provider)]


@router.post("/projects/{project_id}/first-frames/generate", response_model=VersionResponse)
def generate_project_first_frames(
    project_id: str,
    request: GenerateFirstFramesRequest,
    conn: Database,
    actor: AuthenticatedUser,
    storage: FirstFrameStorage,
    provider: InjectedImageProvider,
) -> VersionResponse:
    row = generate_first_frame_candidates(
        conn,
        project_id=project_id,
        actor=actor,
        storage=storage,
        provider=provider,
        model=request.model,
        prompt=request.prompt,
        quantity=request.quantity,
    )
    return version_response(row)


@router.get("/projects/{project_id}/first-frames/latest", response_model=VersionResponse)
def read_latest_first_frames(
    project_id: str,
    conn: Database,
    actor: AuthenticatedUser,
) -> VersionResponse:
    require_project_access(conn, actor=actor, project_id=project_id, action="first_frame.read")
    try:
        row = current_first_frame_candidates(conn, project_id=project_id)
    except HTTPException as exc:
        if (
            exc.status_code == 409
            and isinstance(exc.detail, dict)
            and exc.detail.get("code") == "FIRST_FRAME_CANDIDATES_NOT_FOUND"
        ):
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "FIRST_FRAME_CANDIDATES_NOT_FOUND",
                    "message": "No first frames found.",
                },
            ) from exc
        raise
    return version_response(row)


@router.post("/projects/{project_id}/first-frames/confirm", response_model=VersionResponse)
def confirm_project_first_frame(
    project_id: str,
    request: ConfirmFirstFrameRequest,
    conn: Database,
    actor: AuthenticatedUser,
) -> VersionResponse:
    return version_response(
        confirm_first_frame(
            conn,
            project_id=project_id,
            first_frame_asset_id=request.first_frame_asset_id,
            actor=actor,
        )
    )


@router.get("/projects/{project_id}/first-frames/selection/latest", response_model=VersionResponse)
def read_latest_first_frame_selection(
    project_id: str,
    conn: Database,
    actor: AuthenticatedUser,
) -> VersionResponse:
    require_project_access(conn, actor=actor, project_id=project_id, action="first_frame.read")
    row = latest_version(conn, project_id, FIRST_FRAME_SELECTION_KIND)
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "FIRST_FRAME_SELECTION_NOT_FOUND",
                "message": "No first frame confirmed.",
            },
        )
    payload = json.loads(str(row["payload_json"]))
    try:
        candidates = current_first_frame_candidates(conn, project_id=project_id)
    except HTTPException as exc:
        if (
            exc.status_code == 409
            and isinstance(exc.detail, dict)
            and exc.detail.get("code") == "FIRST_FRAME_CANDIDATES_NOT_FOUND"
        ):
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "FIRST_FRAME_SELECTION_NOT_FOUND",
                    "message": "No first frame confirmed.",
                },
            ) from exc
        raise
    if payload.get("first_frame_candidates_version_id") != str(candidates["id"]):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "FIRST_FRAME_SELECTION_STALE",
                "message": "Select a first frame from the latest candidate set.",
            },
        )
    return version_response(row)


def version_response(row: sqlite3.Row) -> VersionResponse:
    return VersionResponse(
        id=str(row["id"]),
        project_id=str(row["project_id"]),
        asset_id=None if row["asset_id"] is None else str(row["asset_id"]),
        kind=str(row["kind"]),
        version_number=int(row["version_number"]),
        payload=json.loads(str(row["payload_json"])),
        created_by_user_id=None
        if row["created_by_user_id"] is None
        else str(row["created_by_user_id"]),
        created_at=str(row["created_at"]),
    )
