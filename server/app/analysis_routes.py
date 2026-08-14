from __future__ import annotations

import json
import sqlite3
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.analysis import (
    ANALYSIS_KIND,
    SHOT_CARD_KIND,
    FakeGemini,
    analyze_video,
    create_analysis_version,
    create_shot_card_version,
    get_version,
    validate_shot_cards,
)
from app.auth import AuthenticatedUser, Database
from app.media import is_reference_video_asset
from app.permissions import (
    require_asset_access,
    require_not_auditor,
    require_project_access,
    write_audit,
)

router = APIRouter(prefix="/api", tags=["analysis"])


class CreateAnalysisRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset_id: str = Field(min_length=1)
    duration_seconds: float = Field(gt=0)


class UpdateShotCardsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    shots: list[dict[str, Any]] = Field(min_length=1)


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


@router.post("/projects/{project_id}/analysis", response_model=VersionResponse)
def create_project_analysis(
    project_id: str,
    request: CreateAnalysisRequest,
    conn: Database,
    actor: AuthenticatedUser,
) -> VersionResponse:
    require_not_auditor(
        conn,
        actor=actor,
        action="analysis.create",
        entity_type="project",
        entity_id=project_id,
    )
    require_project_access(conn, actor=actor, project_id=project_id, action="analysis.create")
    asset = require_asset_access(
        conn,
        actor=actor,
        asset_id=request.asset_id,
        action="analysis.create",
    )
    if str(asset["project_id"]) != project_id:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "ASSET_PROJECT_MISMATCH",
                "message": "Asset does not belong to the requested project.",
            },
        )
    if not is_reference_video_asset(asset):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "ANALYSIS_ASSET_NOT_REFERENCE_VIDEO",
                "message": "Analysis requires a reference video asset.",
            },
        )
    if not str(asset["sha256"]) or int(asset["size_bytes"]) <= 0:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "REFERENCE_VIDEO_NOT_READY",
                "message": "Reference video upload is not ready for analysis.",
            },
        )

    result = analyze_video(
        video_uri=str(asset["storage_uri"]),
        video_duration_seconds=request.duration_seconds,
        provider=FakeGemini(),
    )
    row = create_analysis_version(
        conn,
        project_id=project_id,
        asset_id=request.asset_id,
        asset_uri=str(asset["storage_uri"]),
        created_by_user_id=actor.id,
        result=result,
    )
    write_audit(
        conn,
        actor=actor,
        action="analysis.create",
        entity_type="version",
        entity_id=str(row["id"]),
        metadata={"project_id": project_id, "asset_id": request.asset_id},
    )
    return version_response(row)


@router.get("/analysis/{analysis_id}", response_model=VersionResponse)
def read_analysis(
    analysis_id: str,
    conn: Database,
    actor: AuthenticatedUser,
) -> VersionResponse:
    row = load_analysis_version(conn, analysis_id)
    require_project_access(
        conn,
        actor=actor,
        project_id=str(row["project_id"]),
        action="analysis.read",
    )
    return version_response(row)


@router.get("/projects/{project_id}/analysis/latest", response_model=VersionResponse)
def read_latest_project_analysis(
    project_id: str,
    conn: Database,
    actor: AuthenticatedUser,
) -> VersionResponse:
    require_project_access(conn, actor=actor, project_id=project_id, action="analysis.read")
    row = conn.execute(
        """
        SELECT id, project_id, asset_id, kind, version_number, payload_json, created_by_user_id,
               created_at
        FROM versions
        WHERE project_id = ? AND kind = ?
        ORDER BY version_number DESC
        LIMIT 1
        """,
        (project_id, ANALYSIS_KIND),
    ).fetchone()
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "ANALYSIS_NOT_FOUND", "message": "Project has no analysis version."},
        )
    return version_response(row)


@router.get("/projects/{project_id}/shot-cards/latest", response_model=VersionResponse)
def read_latest_project_shot_card(
    project_id: str,
    conn: Database,
    actor: AuthenticatedUser,
) -> VersionResponse:
    require_project_access(conn, actor=actor, project_id=project_id, action="shot_card.read")
    row = conn.execute(
        """
        SELECT id, project_id, asset_id, kind, version_number, payload_json, created_by_user_id,
               created_at
        FROM versions
        WHERE project_id = ? AND kind = ?
        ORDER BY version_number DESC
        LIMIT 1
        """,
        (project_id, SHOT_CARD_KIND),
    ).fetchone()
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "SHOT_CARD_NOT_FOUND", "message": "Project has no shot card version."},
        )
    return version_response(row)


@router.put("/analysis/{analysis_id}/shots", response_model=VersionResponse)
def update_analysis_shots(
    analysis_id: str,
    request: UpdateShotCardsRequest,
    conn: Database,
    actor: AuthenticatedUser,
) -> VersionResponse:
    require_not_auditor(
        conn,
        actor=actor,
        action="shot_card.update",
        entity_type="analysis",
        entity_id=analysis_id,
    )
    row = load_analysis_version(conn, analysis_id)
    require_project_access(
        conn,
        actor=actor,
        project_id=str(row["project_id"]),
        action="shot_card.update",
    )
    payload = json.loads(str(row["payload_json"]))
    try:
        shots = validate_shot_cards(
            request.shots,
            duration_seconds=float(payload["analysis"]["duration_seconds"]),
        )
    except ValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "SHOT_CARD_INVALID", "message": str(exc)},
        ) from exc

    shot_card = create_shot_card_version(
        conn,
        analysis_version=row,
        created_by_user_id=actor.id,
        shots=shots,
    )
    write_audit(
        conn,
        actor=actor,
        action="shot_card.update",
        entity_type="version",
        entity_id=str(shot_card["id"]),
        metadata={"source_analysis_version_id": analysis_id},
    )
    return version_response(shot_card)


def load_analysis_version(conn: sqlite3.Connection, analysis_id: str) -> sqlite3.Row:
    try:
        row = get_version(conn, analysis_id)
    except LookupError as exc:
        raise HTTPException(
            status_code=404,
            detail={"code": "ANALYSIS_NOT_FOUND", "message": "Analysis version does not exist."},
        ) from exc

    if str(row["kind"]) != ANALYSIS_KIND:
        raise HTTPException(
            status_code=404,
            detail={"code": "ANALYSIS_NOT_FOUND", "message": "Analysis version does not exist."},
        )
    return row


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
