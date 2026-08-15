from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.analysis import (
    ANALYSIS_KIND,
    APILIO_DEFAULT_BASE_URL,
    SHOT_CARD_KIND,
    AnalysisProviderFailed,
    ApilioGemini,
    FakeGemini,
    VideoAnalysisProvider,
    analyze_video,
    create_analysis_version,
    create_or_recover_analysis_version,
    create_shot_card_version,
    find_analysis_version_for_asset,
    get_version,
    validate_shot_cards,
)
from app.auth import AuthenticatedUser, Database
from app.media import is_reference_video_asset
from app.media_routes import get_media_storage
from app.permissions import (
    require_asset_access,
    require_not_auditor,
    require_project_access,
    write_audit,
)
from app.settings import SettingsDecryptError, SettingsKeyMissing, SettingsRepository
from app.storage import (
    StorageAdapter,
    StorageBackendUnavailable,
    require_storage_match,
    storage_object_ref_from_uri,
)

router = APIRouter(prefix="/api", tags=["analysis"])


def get_video_analysis_provider(conn: Database) -> VideoAnalysisProvider:
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
                detail={"code": "APILIO_SETTINGS_UNAVAILABLE"},
            ) from exc
        return FakeGemini()
    api_key = config.get("analysis_api_key") or config.get("api_key")
    if not api_key:
        return FakeGemini()
    return ApilioGemini(
        api_key=api_key,
        # The desktop settings page does not expose a custom Apilio endpoint.
        # Keeping the origin fixed prevents an imported legacy base_url from
        # receiving the configured bearer token.
        base_url=APILIO_DEFAULT_BASE_URL,
    )


def signed_video_url_for_provider(storage: StorageAdapter, *, asset_uri: str) -> str:
    try:
        reference = storage_object_ref_from_uri(asset_uri)
        require_storage_match(storage, reference)
        intent = storage.create_download_intent(
            reference.key,
            expires_in=timedelta(minutes=15),
            can_read=True,
        )
    except (StorageBackendUnavailable, ValueError) as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "ANALYSIS_VIDEO_URL_UNAVAILABLE"},
        ) from exc
    if not intent.url.startswith("https://"):
        raise HTTPException(
            status_code=503,
            detail={
                "code": "ANALYSIS_VIDEO_URL_UNAVAILABLE",
                "message": (
                    "当前视频分析模型只能读取 HTTPS 视频；本地存储无法用于真实视频拆解。"
                    "请在设置中切换至腾讯云 COS 或阿里云 OSS 后重新上传。"
                ),
            },
        )
    return intent.url


class CreateAnalysisRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset_id: str = Field(min_length=1)
    # Reference videos are capped at 15s; keep the analysis time axis bounded.
    duration_seconds: float | None = Field(default=None, gt=0, le=15)
    reuse_existing: bool = False


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

    should_reuse = request.duration_seconds is None or request.reuse_existing
    if should_reuse:
        existing = find_analysis_version_for_asset(
            conn,
            project_id=project_id,
            asset_id=request.asset_id,
        )
        if existing is not None:
            write_audit(
                conn,
                actor=actor,
                action="analysis.recover_existing",
                entity_type="version",
                entity_id=str(existing["id"]),
                metadata={"project_id": project_id, "asset_id": request.asset_id},
            )
            return version_response(existing)

    provider = get_video_analysis_provider(conn)
    video_uri = str(asset["storage_uri"])
    if provider.requires_https_video_url:
        video_uri = signed_video_url_for_provider(
            get_media_storage(conn),
            asset_uri=video_uri,
        )
    metadata_row = conn.execute(
        "SELECT metadata_json FROM assets WHERE id = ?", (request.asset_id,)
    ).fetchone()
    measured_duration: float | None = None
    if metadata_row is not None:
        metadata = json.loads(str(metadata_row["metadata_json"]))
        stored_duration = metadata.get("duration_seconds")
        if isinstance(stored_duration, int | float):
            measured_duration = float(stored_duration)
    if measured_duration is None:
        measured_duration = request.duration_seconds
    if measured_duration is None:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "ANALYSIS_DURATION_UNAVAILABLE",
                "message": "Reference video duration is unavailable; upload it again.",
            },
        )
    if (
        request.duration_seconds is not None
        and abs(measured_duration - request.duration_seconds) > 1.0
    ):
        raise HTTPException(
            status_code=400,
            detail={
                "code": "ANALYSIS_DURATION_MISMATCH",
                "message": "Requested duration does not match the reference video.",
            },
        )
    try:
        result = analyze_video(
            video_uri=video_uri,
            video_duration_seconds=measured_duration,
            provider=provider,
        )
    except AnalysisProviderFailed as exc:
        code = (
            "ANALYSIS_PROVIDER_RATE_LIMITED"
            if exc.http_status == 429
            else "ANALYSIS_PROVIDER_FAILED"
        )
        raise HTTPException(
            status_code=429 if exc.http_status == 429 else 502,
            detail={"code": code},
        ) from exc
    if should_reuse:
        row, created = create_or_recover_analysis_version(
            conn,
            project_id=project_id,
            asset_id=request.asset_id,
            asset_uri=str(asset["storage_uri"]),
            created_by_user_id=actor.id,
            result=result,
        )
    else:
        row = create_analysis_version(
            conn,
            project_id=project_id,
            asset_id=request.asset_id,
            asset_uri=str(asset["storage_uri"]),
            created_by_user_id=actor.id,
            result=result,
        )
        created = True
    write_audit(
        conn,
        actor=actor,
        action="analysis.create" if created else "analysis.recover_existing",
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
