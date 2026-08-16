from __future__ import annotations

import json
import sqlite3
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.auth import AuthenticatedUser, Database
from app.first_frames import (
    APILIO_DEFAULT_BASE_URL,
    FIRST_FRAME_SELECTION_KIND,
    ApilioImageProvider,
    FakeImageProvider,
    ImageProvider,
    confirm_first_frame,
    current_first_frame_candidates,
    generate_first_frame_candidates,
)
from app.media_routes import get_media_storage
from app.permissions import require_project_access
from app.settings import SettingsDecryptError, SettingsKeyMissing, SettingsRepository
from app.source_frames import latest_version
from app.storage import StorageAdapter

router = APIRouter(prefix="/api", tags=["first-frames"])


class GenerateFirstFramesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: Literal["gpt-image-2", "nano-banana-pro-2k"] = "nano-banana-pro-2k"
    prompt: str | None = Field(default=None, max_length=4000)
    quantity: int = Field(default=1, ge=1, le=3)
    character_version_id: str | None = Field(default=None, min_length=1)
    character_reference_selection_id: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def validate_character_binding(self) -> GenerateFirstFramesRequest:
        if (self.character_version_id is None) != (self.character_reference_selection_id is None):
            raise ValueError("character version and reference selection must be supplied together")
        return self


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


def get_image_provider(conn: Database) -> ImageProvider:
    has_saved_apilio_config = (
        conn.execute(
            "SELECT 1 FROM provider_settings WHERE provider = ?",
            ("apilio",),
        ).fetchone()
        is not None
    )
    try:
        config = SettingsRepository(conn).load_provider_config("apilio")
    except (SettingsDecryptError, SettingsKeyMissing) as exc:
        if has_saved_apilio_config:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "APILIO_SETTINGS_UNAVAILABLE",
                    "message": (
                        "Apilio settings cannot be decrypted. Ask an administrator to repair them."
                    ),
                },
            ) from exc
        return FakeImageProvider()
    if not config:
        return FakeImageProvider()
    api_key = config.get("api_key")
    if not api_key:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "APILIO_SETTINGS_UNAVAILABLE",
                "message": (
                    "Apilio API Key is unavailable. Ask an administrator to repair settings."
                ),
            },
        )
    return ApilioImageProvider(
        api_key=api_key,
        # The desktop settings page does not expose a custom Apilio endpoint.
        # Keeping the origin fixed prevents an imported legacy base_url from
        # receiving the configured bearer token (same policy as the analysis path).
        base_url=APILIO_DEFAULT_BASE_URL,
    )


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
        character_version_id=request.character_version_id,
        character_reference_selection_id=request.character_reference_selection_id,
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


@router.get("/projects/{project_id}/first-frames/history", response_model=list[VersionResponse])
def read_first_frame_history(
    project_id: str,
    conn: Database,
    actor: AuthenticatedUser,
) -> list[VersionResponse]:
    require_project_access(conn, actor=actor, project_id=project_id, action="first_frame.read")
    rows = conn.execute(
        """
        SELECT id, project_id, asset_id, kind, version_number, payload_json,
               created_by_user_id, created_at
        FROM versions
        WHERE project_id = ? AND kind = ?
        ORDER BY version_number DESC
        LIMIT 20
        """,
        (project_id, "first_frame_candidates"),
    ).fetchall()
    return [version_response(row) for row in rows]


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
