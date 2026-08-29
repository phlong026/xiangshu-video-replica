from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import uuid4

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.analysis import (
    ANALYSIS_KIND,
    APILIO_DEFAULT_BASE_URL,
    NETWORK_FAILURE_PHASE,
    SHOT_CARD_KIND,
    AnalysisProviderFailed,
    AnalysisResult,
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
from app.auth import AuthenticatedUser, CurrentUser, Database, Role
from app.customer_fence import BusinessDbDep
from app.db_portable import BusinessConnection
from app.media import (
    DURATION_ROUNDING_TOLERANCE_SECONDS,
    MAX_DURATION_SECONDS,
    is_reference_video_asset,
)
from app.media_routes import get_media_storage
from app.permissions import (
    require_asset_access,
    require_not_auditor,
    require_project_access,
    write_audit,
)
from app.settings import SettingsRepository, SettingsUnavailableError
from app.storage import (
    StorageAdapter,
    StorageBackendUnavailable,
    require_storage_match,
    storage_object_ref_from_uri,
)

router = APIRouter(prefix="/api", tags=["analysis"])

# Must track the upload precheck: a video that passed upload at 15.05s has to stay
# analysable instead of being rejected as an invalid request.
MAX_ANALYSIS_DURATION_SECONDS = MAX_DURATION_SECONDS + DURATION_ROUNDING_TOLERANCE_SECONDS
ANALYSIS_TASK_LEASE_MINUTES = 10


def get_video_analysis_provider(conn: Database) -> VideoAnalysisProvider:
    has_saved_apilio_config = (
        conn.execute(
            "SELECT 1 FROM provider_settings WHERE provider = %s",
            ("apilio",),
        ).fetchone()
        is not None
    )
    try:
        config = SettingsRepository(conn).load_provider_config("apilio")
    except SettingsUnavailableError as exc:
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
                    "请在设置中切换至腾讯云 COS 后重新上传。"
                ),
            },
        )
    return intent.url


class CreateAnalysisRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset_id: str = Field(min_length=1)
    # Reference videos are capped at 15s plus the upload rounding tolerance;
    # keep the analysis time axis bounded by the same contract.
    duration_seconds: float | None = Field(default=None, gt=0, le=MAX_ANALYSIS_DURATION_SECONDS)
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


class AnalysisTaskResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    project_id: str
    asset_id: str
    status: str
    attempt: int
    result_version_id: str | None
    error_code: str | None
    error_message: str | None
    failure_phase: str | None
    retryable: bool
    created_at: str
    updated_at: str
    started_at: str | None
    completed_at: str | None


@dataclass(frozen=True)
class AnalysisTaskLease:
    id: str
    project_id: str
    asset_id: str
    created_by_user_id: str
    duration_seconds: float
    worker_id: str


@dataclass(frozen=True)
class AnalysisTaskWork:
    lease: AnalysisTaskLease
    provider: VideoAnalysisProvider
    video_uri: str
    asset_uri: str


@router.post("/projects/{project_id}/analysis", response_model=VersionResponse)
def create_project_analysis(
    project_id: str,
    request: CreateAnalysisRequest,
    db: BusinessDbDep,
) -> VersionResponse:
    with db.write() as (conn, actor):
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
            "SELECT metadata_json FROM assets WHERE id = %s", (request.asset_id,)
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
            raise analysis_provider_error(exc) from exc
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


@router.post(
    "/projects/{project_id}/analysis-tasks",
    response_model=AnalysisTaskResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def create_project_analysis_task(
    project_id: str,
    request: CreateAnalysisRequest,
    db: BusinessDbDep,
) -> AnalysisTaskResponse:
    """Validate quickly and persist work for the generation worker.

    No provider or storage-network call is allowed in this request.  Customer
    session fencing therefore protects only the enqueue commit and can never
    block the heartbeat for the lifetime of a model request.
    """
    with db.write() as (conn, actor):
        asset, measured_duration = validate_analysis_enqueue(
            conn,
            actor=actor,
            project_id=project_id,
            request=request,
        )
        existing_task = conn.execute(
            """
            SELECT * FROM analysis_tasks
            WHERE project_id = %s AND asset_id = %s
              AND status IN ('PENDING', 'RUNNING')
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (project_id, request.asset_id),
        ).fetchone()
        if existing_task is not None:
            return analysis_task_response(existing_task)

        task_id = str(uuid4())
        conn.execute(
            """
            INSERT INTO analysis_tasks (
                id, project_id, asset_id, created_by_user_id,
                duration_seconds, status
            ) VALUES (%s, %s, %s, %s, %s, 'PENDING')
            ON CONFLICT DO NOTHING
            """,
            (task_id, project_id, request.asset_id, actor.id, measured_duration),
        )
        inserted_task = conn.execute(
            "SELECT * FROM analysis_tasks WHERE id = %s",
            (task_id,),
        ).fetchone()
        if inserted_task is None:
            # A concurrent request won the partial unique index. Return that
            # durable task instead of surfacing a transient 500 to the client.
            concurrent_task = conn.execute(
                """
                SELECT * FROM analysis_tasks
                WHERE project_id = %s AND asset_id = %s
                  AND status IN ('PENDING', 'RUNNING')
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """,
                (project_id, request.asset_id),
            ).fetchone()
            if concurrent_task is None:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "ANALYSIS_TASK_ENQUEUE_CONFLICT",
                        "message": "拆解任务状态已变化，请重新提交。",
                        "retryable": True,
                    },
                )
            return analysis_task_response(concurrent_task)
        write_audit(
            conn,
            actor=actor,
            action="analysis.task_enqueued",
            entity_type="analysis_task",
            entity_id=task_id,
            metadata={
                "project_id": project_id,
                "asset_id": request.asset_id,
                "asset_sha256": str(asset["sha256"]),
            },
        )
        row = load_analysis_task(conn, task_id)
        return analysis_task_response(row)


@router.get("/analysis-tasks/{task_id}", response_model=AnalysisTaskResponse)
def read_analysis_task(
    task_id: str,
    conn: Database,
    actor: AuthenticatedUser,
) -> AnalysisTaskResponse:
    row = load_analysis_task(conn, task_id)
    require_project_access(
        conn,
        actor=actor,
        project_id=str(row["project_id"]),
        action="analysis.task.read",
    )
    return analysis_task_response(row)


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
        WHERE project_id = %s AND kind = %s
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


@router.get("/projects/{project_id}/shot-cards/latest", response_model=VersionResponse | None)
def read_latest_project_shot_card(
    project_id: str,
    conn: Database,
    actor: AuthenticatedUser,
) -> VersionResponse | None:
    require_project_access(conn, actor=actor, project_id=project_id, action="shot_card.read")
    row = conn.execute(
        """
        SELECT id, project_id, asset_id, kind, version_number, payload_json, created_by_user_id,
               created_at
        FROM versions
        WHERE project_id = %s AND kind = %s
        ORDER BY version_number DESC
        LIMIT 1
        """,
        (project_id, SHOT_CARD_KIND),
    ).fetchone()
    if row is None:
        return None
    return version_response(row)


@router.put("/analysis/{analysis_id}/shots", response_model=VersionResponse)
def update_analysis_shots(
    analysis_id: str,
    request: UpdateShotCardsRequest,
    db: BusinessDbDep,
) -> VersionResponse:
    with db.write() as (conn, actor):
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


def validate_analysis_enqueue(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    project_id: str,
    request: CreateAnalysisRequest,
) -> tuple[sqlite3.Row, float]:
    require_not_auditor(
        conn,
        actor=actor,
        action="analysis.task.create",
        entity_type="project",
        entity_id=project_id,
    )
    require_project_access(
        conn,
        actor=actor,
        project_id=project_id,
        action="analysis.task.create",
    )
    asset = require_asset_access(
        conn,
        actor=actor,
        asset_id=request.asset_id,
        action="analysis.task.create",
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
    return asset, analysis_duration_for_asset(
        conn,
        asset_id=request.asset_id,
        requested_duration=request.duration_seconds,
    )


def analysis_duration_for_asset(
    conn: BusinessConnection,
    *,
    asset_id: str,
    requested_duration: float | None,
) -> float:
    metadata_row = conn.execute(
        "SELECT metadata_json FROM assets WHERE id = %s", (asset_id,)
    ).fetchone()
    measured_duration: float | None = None
    if metadata_row is not None:
        metadata = json.loads(str(metadata_row["metadata_json"]))
        stored_duration = metadata.get("duration_seconds")
        if isinstance(stored_duration, int | float):
            measured_duration = float(stored_duration)
    if measured_duration is None:
        measured_duration = requested_duration
    if measured_duration is None:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "ANALYSIS_DURATION_UNAVAILABLE",
                "message": "Reference video duration is unavailable; upload it again.",
            },
        )
    if requested_duration is not None and abs(measured_duration - requested_duration) > 1.0:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "ANALYSIS_DURATION_MISMATCH",
                "message": "Requested duration does not match the reference video.",
            },
        )
    return measured_duration


def acquire_analysis_task(
    conn: BusinessConnection,
    *,
    worker_id: str,
) -> AnalysisTaskLease | None:
    now = datetime.now(UTC)
    now_text = now.strftime("%Y-%m-%d %H:%M:%S")
    # One analysis can use the primary 240-second request plus one 240-second
    # structural repair request. Keep the lease longer than both attempts so a
    # second worker cannot mark a still-running paid request as interrupted.
    locked_until = (now + timedelta(minutes=ANALYSIS_TASK_LEASE_MINUTES)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    try:
        conn.execute(
            """
            UPDATE analysis_tasks
            SET status = 'FAILED',
                error_code = 'ANALYSIS_WORKER_INTERRUPTED',
                error_message_redacted = '拆解任务执行中断，请重新拆解。',
                retryable = 1,
                locked_by = NULL,
                locked_until = NULL,
                completed_at = %s,
                updated_at = %s
            WHERE status = 'RUNNING' AND locked_until IS NOT NULL AND locked_until <= %s
            """,
            (now_text, now_text, now_text),
        )
        row = conn.execute(
            """
            UPDATE analysis_tasks
            SET status = 'RUNNING',
                attempt = attempt + 1,
                locked_by = %s,
                locked_until = %s,
                started_at = COALESCE(started_at, %s),
                updated_at = %s,
                error_code = NULL,
                error_message_redacted = NULL,
                failure_phase = NULL,
                retryable = 0
            WHERE id = (
                SELECT id FROM analysis_tasks
                WHERE status = 'PENDING'
                ORDER BY created_at, id
                LIMIT 1
            ) AND status = 'PENDING'
            RETURNING *
            """,
            (worker_id, locked_until, now_text, now_text),
        ).fetchone()
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    if row is None:
        return None
    return AnalysisTaskLease(
        id=str(row["id"]),
        project_id=str(row["project_id"]),
        asset_id=str(row["asset_id"]),
        created_by_user_id=str(row["created_by_user_id"]),
        duration_seconds=float(row["duration_seconds"]),
        worker_id=worker_id,
    )


def prepare_analysis_task(
    conn: BusinessConnection,
    *,
    lease: AnalysisTaskLease,
    storage: StorageAdapter,
    provider: VideoAnalysisProvider | None = None,
) -> AnalysisTaskWork:
    row = conn.execute(
        """
        SELECT task.status, task.locked_by, asset.project_id,
               asset.storage_uri, asset.sha256, asset.size_bytes
        FROM analysis_tasks AS task
        JOIN assets AS asset ON asset.id = task.asset_id
        WHERE task.id = %s
        """,
        (lease.id,),
    ).fetchone()
    if (
        row is None
        or str(row["status"]) != "RUNNING"
        or str(row["locked_by"]) != lease.worker_id
        or str(row["project_id"]) != lease.project_id
        or not str(row["sha256"])
        or int(row["size_bytes"]) <= 0
    ):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "ANALYSIS_TASK_INPUT_UNAVAILABLE",
                "message": "参考视频状态已变化，请重新上传或重新拆解。",
                "retryable": True,
            },
        )
    resolved_provider = provider or get_video_analysis_provider(conn)
    asset_uri = str(row["storage_uri"])
    video_uri = asset_uri
    if resolved_provider.requires_https_video_url:
        video_uri = signed_video_url_for_provider(storage, asset_uri=asset_uri)
    conn.commit()
    return AnalysisTaskWork(
        lease=lease,
        provider=resolved_provider,
        video_uri=video_uri,
        asset_uri=asset_uri,
    )


def perform_analysis_task(work: AnalysisTaskWork) -> AnalysisResult:
    return analyze_video(
        video_uri=work.video_uri,
        video_duration_seconds=work.lease.duration_seconds,
        provider=work.provider,
    )


def complete_analysis_task(
    conn: BusinessConnection,
    *,
    work: AnalysisTaskWork,
    result: AnalysisResult,
) -> None:
    task = conn.execute(
        "SELECT status, locked_by FROM analysis_tasks WHERE id = %s",
        (work.lease.id,),
    ).fetchone()
    if (
        task is None
        or str(task["status"]) != "RUNNING"
        or str(task["locked_by"]) != work.lease.worker_id
    ):
        conn.rollback()
        return
    row, created = create_or_recover_analysis_version(
        conn,
        project_id=work.lease.project_id,
        asset_id=work.lease.asset_id,
        asset_uri=work.asset_uri,
        created_by_user_id=work.lease.created_by_user_id,
        result=result,
    )
    now_text = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        """
        UPDATE analysis_tasks
        SET status = 'SUCCEEDED', result_version_id = %s,
            error_code = NULL, error_message_redacted = NULL,
            failure_phase = NULL, retryable = 0,
            locked_by = NULL, locked_until = NULL,
            completed_at = %s, updated_at = %s
        WHERE id = %s AND status = 'RUNNING' AND locked_by = %s
        """,
        (str(row["id"]), now_text, now_text, work.lease.id, work.lease.worker_id),
    )
    write_audit(
        conn,
        actor=load_task_actor(conn, work.lease.created_by_user_id),
        action="analysis.task_succeeded" if created else "analysis.task_recovered",
        entity_type="analysis_task",
        entity_id=work.lease.id,
        metadata={
            "project_id": work.lease.project_id,
            "asset_id": work.lease.asset_id,
            "version_id": str(row["id"]),
        },
    )
    conn.commit()


def fail_analysis_task(
    conn: BusinessConnection,
    *,
    lease: AnalysisTaskLease,
    cause: Exception,
) -> None:
    code = "ANALYSIS_WORKER_FAILED"
    message = "视频拆解失败，请稍后重新拆解。"
    retryable = True
    failure_phase: str | None = None
    if isinstance(cause, AnalysisProviderFailed):
        mapped = analysis_provider_error(cause)
        detail: dict[str, Any] = mapped.detail if isinstance(mapped.detail, dict) else {}
        code = str(detail.get("code") or code)
        message = str(detail.get("message") or message)
        retryable = bool(detail.get("retryable", cause.retryable))
        failure_phase = cause.failure_phase
    elif isinstance(cause, HTTPException) and isinstance(cause.detail, dict):
        code = str(cause.detail.get("code") or code)
        message = str(cause.detail.get("message") or message)
        retryable = bool(cause.detail.get("retryable", True))
    now_text = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        """
        UPDATE analysis_tasks
        SET status = 'FAILED', error_code = %s,
            error_message_redacted = %s, failure_phase = %s,
            retryable = %s, locked_by = NULL, locked_until = NULL,
            completed_at = %s, updated_at = %s
        WHERE id = %s AND status = 'RUNNING' AND locked_by = %s
        """,
        (
            code,
            message,
            failure_phase,
            1 if retryable else 0,
            now_text,
            now_text,
            lease.id,
            lease.worker_id,
        ),
    )
    write_audit(
        conn,
        actor=load_task_actor(conn, lease.created_by_user_id),
        action="analysis.task_failed",
        entity_type="analysis_task",
        entity_id=lease.id,
        metadata={
            "project_id": lease.project_id,
            "asset_id": lease.asset_id,
            "error_code": code,
            "retryable": retryable,
        },
    )
    conn.commit()


def load_task_actor(conn: BusinessConnection, user_id: str) -> CurrentUser:
    row = conn.execute(
        "SELECT id, username, display_name, role FROM users WHERE id = %s",
        (user_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError("analysis task actor is unavailable")
    return CurrentUser(
        id=str(row["id"]),
        username=str(row["username"]),
        display_name=str(row["display_name"]),
        role=cast(Role, str(row["role"])),
    )


def load_analysis_task(conn: BusinessConnection, task_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM analysis_tasks WHERE id = %s", (task_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail={"code": "ANALYSIS_TASK_NOT_FOUND"})
    return cast(sqlite3.Row, row)


def analysis_task_response(row: sqlite3.Row) -> AnalysisTaskResponse:
    return AnalysisTaskResponse(
        id=str(row["id"]),
        project_id=str(row["project_id"]),
        asset_id=str(row["asset_id"]),
        status=str(row["status"]),
        attempt=int(row["attempt"]),
        result_version_id=(
            None if row["result_version_id"] is None else str(row["result_version_id"])
        ),
        error_code=None if row["error_code"] is None else str(row["error_code"]),
        error_message=(
            None if row["error_message_redacted"] is None else str(row["error_message_redacted"])
        ),
        failure_phase=None if row["failure_phase"] is None else str(row["failure_phase"]),
        retryable=bool(row["retryable"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        started_at=None if row["started_at"] is None else str(row["started_at"]),
        completed_at=None if row["completed_at"] is None else str(row["completed_at"]),
    )


def analysis_provider_error(failure: AnalysisProviderFailed) -> HTTPException:
    """Translate a provider failure into an actionable, secret-free desktop error."""
    if failure.http_status == 429:
        status_code = 429
        code = "ANALYSIS_PROVIDER_RATE_LIMITED"
        message = "视频拆解服务当前限流，请稍后重试。"
    elif failure.failure_phase == NETWORK_FAILURE_PHASE:
        status_code = 503
        code = "ANALYSIS_PROVIDER_UNREACHABLE"
        message = "无法连接视频拆解服务，请检查网络后重试。"
    else:
        status_code = 502
        code = "ANALYSIS_PROVIDER_FAILED"
        message = "视频拆解服务返回了无法解析的结果，请重试或更换参考视频。"
    return HTTPException(
        status_code=status_code,
        detail={
            "code": code,
            "message": message,
            "failure_phase": failure.failure_phase,
            "retryable": failure.retryable,
        },
    )


def load_analysis_version(conn: BusinessConnection, analysis_id: str) -> sqlite3.Row:
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
