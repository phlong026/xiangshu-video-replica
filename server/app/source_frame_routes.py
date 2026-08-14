from __future__ import annotations

import json
import sqlite3
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.auth import AuthenticatedUser, Database
from app.media_routes import get_media_storage
from app.permissions import require_project_access
from app.source_frames import (
    SOURCE_FRAME_CANDIDATES_KIND,
    SOURCE_FRAME_SELECTION_KIND,
    FFmpegSourceFrameExtractor,
    SourceFrameExtractor,
    confirm_source_frame,
    extract_source_frame_candidates,
    latest_version,
)
from app.storage import StorageAdapter

router = APIRouter(prefix="/api", tags=["source-frames"])


class ExtractSourceFramesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset_id: str = Field(min_length=1)


class ConfirmSourceFrameRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_frame_asset_id: str = Field(min_length=1)


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


def get_source_frame_extractor() -> SourceFrameExtractor:
    return FFmpegSourceFrameExtractor()


SourceFrameStorage = Annotated[StorageAdapter, Depends(get_media_storage)]
InjectedSourceFrameExtractor = Annotated[SourceFrameExtractor, Depends(get_source_frame_extractor)]


@router.post("/projects/{project_id}/source-frames/extract", response_model=VersionResponse)
def extract_project_source_frames(
    project_id: str,
    request: ExtractSourceFramesRequest,
    conn: Database,
    actor: AuthenticatedUser,
    storage: SourceFrameStorage,
    extractor: InjectedSourceFrameExtractor,
) -> VersionResponse:
    row = extract_source_frame_candidates(
        conn,
        project_id=project_id,
        asset_id=request.asset_id,
        actor=actor,
        storage=storage,
        extractor=extractor,
    )
    return version_response(row)


@router.get("/projects/{project_id}/source-frames/latest", response_model=VersionResponse)
def read_latest_source_frames(
    project_id: str,
    conn: Database,
    actor: AuthenticatedUser,
) -> VersionResponse:
    require_project_access(conn, actor=actor, project_id=project_id, action="source_frame.read")
    row = latest_version(conn, project_id, SOURCE_FRAME_CANDIDATES_KIND)
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "SOURCE_FRAME_CANDIDATES_NOT_FOUND",
                "message": "No source frames found.",
            },
        )
    return version_response(row)


@router.post("/projects/{project_id}/source-frames/confirm", response_model=VersionResponse)
def confirm_project_source_frame(
    project_id: str,
    request: ConfirmSourceFrameRequest,
    conn: Database,
    actor: AuthenticatedUser,
) -> VersionResponse:
    row = confirm_source_frame(
        conn,
        project_id=project_id,
        source_frame_asset_id=request.source_frame_asset_id,
        actor=actor,
    )
    return version_response(row)


@router.get(
    "/projects/{project_id}/source-frames/selection/latest",
    response_model=VersionResponse,
)
def read_latest_source_frame_selection(
    project_id: str,
    conn: Database,
    actor: AuthenticatedUser,
) -> VersionResponse:
    require_project_access(conn, actor=actor, project_id=project_id, action="source_frame.read")
    row = latest_version(conn, project_id, SOURCE_FRAME_SELECTION_KIND)
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "SOURCE_FRAME_SELECTION_NOT_FOUND",
                "message": "No source frame confirmed.",
            },
        )
    payload = json.loads(str(row["payload_json"]))
    candidate_version_id = payload.get("source_frame_candidates_version_id")
    current_candidates = latest_version(conn, project_id, SOURCE_FRAME_CANDIDATES_KIND)
    if current_candidates is None or candidate_version_id != str(current_candidates["id"]):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "SOURCE_FRAME_SELECTION_STALE",
                "message": "Select a source frame from the latest candidate set.",
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
