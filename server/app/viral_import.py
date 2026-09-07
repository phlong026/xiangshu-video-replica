"""Durable import of a cached viral video into a user's project."""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast
from uuid import uuid4

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.auth import CurrentUser
from app.db_portable import BusinessConnection
from app.permissions import insert_audit, require_not_auditor, require_project_access
from app.storage import (
    StorageAdapter,
    StoredObject,
    require_storage_match,
    storage_object_ref_from_uri,
)
from app.viral_media import ViralMediaPipeline, ViralMediaResult, viral_media_key
from app.viral_store import get_viral_video, viral_video_availability
from app.viral_tikhub import ViralSourceUnavailable, ViralVideo, viral_source_client_from_settings

logger = logging.getLogger(__name__)
VIRAL_IMPORT_LEASE_MINUTES = 20


class ViralImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    platform: Literal["douyin", "wechat_channels"]
    video_id: str = Field(alias="videoId", min_length=1, max_length=256)
    purpose: Literal["copy", "replica"]
    project_id: str | None = Field(default=None, alias="projectId", max_length=128)


class ViralImportTaskResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    status: str
    platform: str
    video_id: str = Field(alias="videoId")
    purpose: str
    project_id: str = Field(alias="projectId")
    source_asset_id: str | None = Field(alias="sourceAssetId")
    media_kind: str | None = Field(alias="mediaKind")
    can_transcribe: bool = Field(alias="canTranscribe")
    can_analyze: bool = Field(alias="canAnalyze")
    error_code: str | None = Field(alias="errorCode")
    error_message: str | None = Field(alias="errorMessage")
    retryable: bool
    created_at: str = Field(alias="createdAt")
    updated_at: str = Field(alias="updatedAt")


class ViralImportError(HTTPException):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        retryable: bool = True,
    ) -> None:
        super().__init__(status_code=status_code, detail={"code": code, "message": message})
        self.retryable = retryable


def viral_import_task_response(row: sqlite3.Row) -> ViralImportTaskResponse:
    result = json.loads(str(row["result_json"])) if row["result_json"] else {}
    media_kind = result.get("mediaKind")
    return ViralImportTaskResponse(
        id=str(row["id"]),
        status=str(row["status"]),
        platform=str(row["platform"]),
        videoId=str(row["video_id"]),
        purpose=str(row["purpose"]),
        projectId=str(row["project_id"]),
        sourceAssetId=(str(row["source_asset_id"]) if row["source_asset_id"] else None),
        mediaKind=str(media_kind) if media_kind else None,
        canTranscribe=bool(result.get("canTranscribe", False)),
        canAnalyze=bool(result.get("canAnalyze", False)),
        errorCode=str(row["error_code"]) if row["error_code"] else None,
        errorMessage=(
            str(row["error_message_redacted"]) if row["error_message_redacted"] else None
        ),
        retryable=bool(row["retryable"]),
        createdAt=str(row["created_at"]),
        updatedAt=str(row["updated_at"]),
    )


def _error(
    status_code: int,
    code: str,
    message: str,
    *,
    retryable: bool = True,
) -> ViralImportError:
    return ViralImportError(status_code, code, message, retryable=retryable)


def _request_hash(payload: dict[str, str | None]) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _require_import_enabled(conn: BusinessConnection) -> None:
    control = conn.execute(
        "SELECT import_enabled FROM viral_runtime_controls WHERE id = 1"
    ).fetchone()
    if control is None or not bool(control["import_enabled"]):
        raise _error(503, "VIRAL_IMPORT_DISABLED", "爆款视频导入已暂停，请稍后重试。")


def enqueue_viral_import_task(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    request: ViralImportRequest,
    idempotency_key: str,
) -> sqlite3.Row:
    _require_import_enabled(conn)
    require_not_auditor(
        conn,
        actor=actor,
        action="viral.import",
        entity_type="viral_video",
        entity_id=f"{request.platform}:{request.video_id}",
    )
    video = get_viral_video(conn, platform=request.platform, video_id=request.video_id)
    if video is None:
        raise _error(404, "VIRAL_VIDEO_NOT_FOUND", "爆款视频不存在或已下架。")
    if (
        viral_video_availability(conn, platform=request.platform, video_id=request.video_id)
        != "available"
    ):
        raise _error(409, "VIRAL_VIDEO_UNAVAILABLE", "该爆款视频当前不可用于创作。")
    request_payload = {
        "platform": request.platform,
        "videoId": request.video_id,
        "purpose": request.purpose,
        "projectId": request.project_id,
    }
    request_hash = _request_hash(request_payload)
    replay = conn.execute(
        "SELECT * FROM viral_import_tasks WHERE owner_user_id = %s AND idempotency_key = %s",
        (actor.id, idempotency_key),
    ).fetchone()
    if replay is not None:
        if str(replay["request_hash"]) != request_hash:
            raise _error(409, "VIRAL_IMPORT_IDEMPOTENCY_CONFLICT", "幂等键已用于不同请求。")
        if str(replay["status"]) == "FAILED" and bool(replay["retryable"]):
            conn.execute(
                """
                UPDATE viral_import_tasks SET status = 'PENDING', retryable = 0,
                    error_code = NULL, error_message_redacted = NULL,
                    locked_by = NULL, locked_until = NULL, completed_at = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = %s AND status = 'FAILED' AND retryable = 1
                """,
                (replay["id"],),
            )
            replay = conn.execute(
                "SELECT * FROM viral_import_tasks WHERE id = %s", (replay["id"],)
            ).fetchone()
            if replay is None:
                raise _error(409, "VIRAL_IMPORT_ENQUEUE_CONFLICT", "导入任务状态已变化。")
        return cast(sqlite3.Row, replay)

    created_project = request.project_id is None
    project_id = request.project_id or str(uuid4())
    if created_project:
        conn.execute(
            "INSERT INTO projects (id, owner_user_id, name) VALUES (%s, %s, %s)",
            (project_id, actor.id, (video.title.strip() or "爆款视频复刻")[:120]),
        )
    else:
        project = require_project_access(
            conn, actor=actor, project_id=project_id, action="viral.import"
        )
        if str(project["owner_user_id"]) != actor.id:
            raise _error(403, "VIRAL_IMPORT_PROJECT_FORBIDDEN", "只能导入到当前用户的项目。")

    task_id = str(uuid4())
    inserted = conn.execute(
        """
        INSERT INTO viral_import_tasks (
            id, owner_user_id, project_id, platform, video_id, purpose,
            idempotency_key, request_hash, request_json, status
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'PENDING')
        ON CONFLICT (owner_user_id, idempotency_key) DO NOTHING
        """,
        (
            task_id,
            actor.id,
            project_id,
            request.platform,
            request.video_id,
            request.purpose,
            idempotency_key,
            request_hash,
            json.dumps(request_payload, ensure_ascii=False, sort_keys=True),
        ),
    )
    if inserted.rowcount == 0:
        if created_project:
            conn.execute("DELETE FROM projects WHERE id = %s", (project_id,))
        replay = conn.execute(
            "SELECT * FROM viral_import_tasks WHERE owner_user_id = %s AND idempotency_key = %s",
            (actor.id, idempotency_key),
        ).fetchone()
        if replay is None:
            raise _error(409, "VIRAL_IMPORT_ENQUEUE_CONFLICT", "导入任务状态已变化，请重试。")
        if str(replay["request_hash"]) != request_hash:
            raise _error(409, "VIRAL_IMPORT_IDEMPOTENCY_CONFLICT", "幂等键已用于不同请求。")
        return cast(sqlite3.Row, replay)
    row = conn.execute("SELECT * FROM viral_import_tasks WHERE id = %s", (task_id,)).fetchone()
    if row is None:
        raise _error(409, "VIRAL_IMPORT_ENQUEUE_CONFLICT", "导入任务状态已变化，请重试。")
    insert_audit(
        conn,
        actor=actor,
        action="viral.import_enqueued",
        entity_type="viral_import_task",
        entity_id=task_id,
        metadata={"project_id": project_id, "platform": request.platform},
    )
    return cast(sqlite3.Row, row)


def load_viral_import_task(conn: BusinessConnection, task_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM viral_import_tasks WHERE id = %s", (task_id,)).fetchone()
    if row is None:
        raise _error(404, "VIRAL_IMPORT_TASK_NOT_FOUND", "导入任务不存在。")
    return cast(sqlite3.Row, row)


@dataclass(frozen=True)
class ViralImportLease:
    id: str
    worker_id: str
    owner_user_id: str
    project_id: str
    platform: str
    video_id: str
    purpose: str
    attempt: int


@dataclass(frozen=True)
class ViralMediaPreparationLease:
    id: str
    worker_id: str
    owner_task_id: str
    platform: str
    video_id: str
    media_kind: Literal["audio", "video"]
    attempt: int


@dataclass(frozen=True)
class ViralImportWork:
    lease: ViralImportLease
    video: ViralVideo
    client: Any
    storage: StorageAdapter
    prefer: Literal["audio", "video"]
    media_preparation: ViralMediaPreparationLease | None


@dataclass(frozen=True)
class ViralImportOutcome:
    stored: StoredObject
    media_kind: Literal["audio", "video"]
    duration_seconds: float | None
    media_preparation: ViralMediaPreparationLease | None = None


def discard_viral_import_outcome(
    storage: StorageAdapter, *, outcome: ViralImportOutcome, actor_id: str
) -> None:
    """Delete a private project copy that never received a committed asset row."""
    try:
        storage.delete_object(outcome.stored.key, actor_id=actor_id)
    except Exception as exc:
        logger.warning(
            "viral import cleanup deferred for %s: %s",
            outcome.stored.key,
            type(exc).__name__,
        )


def _time_text(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S")


def _claim_viral_media_preparation(
    conn: BusinessConnection,
    *,
    lease: ViralImportLease,
    storage: StorageAdapter,
    media_kind: Literal["audio", "video"],
) -> ViralMediaPreparationLease | None:
    cache_key = viral_media_key(lease.platform, lease.video_id, media_kind)
    if storage.head_object(cache_key) is not None:
        conn.execute(
            """
            UPDATE viral_media_preparations SET status = 'SUCCEEDED', locked_by = NULL,
                locked_until = NULL, owner_task_id = %s, error_code = NULL,
                completed_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
            WHERE platform = %s AND video_id = %s AND media_kind = %s
            """,
            (lease.id, lease.platform, lease.video_id, media_kind),
        )
        conn.commit()
        return None

    preparation_id = str(uuid4())
    conn.execute(
        """
        INSERT INTO viral_media_preparations (
            id, platform, video_id, media_kind, status, owner_task_id
        ) VALUES (%s, %s, %s, %s, 'PENDING', %s)
        ON CONFLICT (platform, video_id, media_kind) DO NOTHING
        """,
        (preparation_id, lease.platform, lease.video_id, media_kind, lease.id),
    )
    now = _time_text(datetime.now(UTC))
    locked_until = _time_text(datetime.now(UTC) + timedelta(minutes=VIRAL_IMPORT_LEASE_MINUTES))
    row = conn.execute(
        """
        UPDATE viral_media_preparations SET status = 'RUNNING', attempt = attempt + 1,
            locked_by = %s, locked_until = %s, owner_task_id = %s,
            error_code = NULL, completed_at = NULL, updated_at = %s
        WHERE platform = %s AND video_id = %s AND media_kind = %s
            AND (status IN ('PENDING', 'FAILED', 'SUCCEEDED')
                OR (status = 'RUNNING' AND locked_until IS NOT NULL AND locked_until <= %s))
        RETURNING *
        """,
        (
            lease.worker_id,
            locked_until,
            lease.id,
            now,
            lease.platform,
            lease.video_id,
            media_kind,
            now,
        ),
    ).fetchone()
    conn.commit()
    if row is None:
        raise _error(409, "VIRAL_MEDIA_PREPARATION_BUSY", "该视频素材正在准备中，请稍后重试。")
    return ViralMediaPreparationLease(
        id=str(row["id"]),
        worker_id=lease.worker_id,
        owner_task_id=lease.id,
        platform=lease.platform,
        video_id=lease.video_id,
        media_kind=media_kind,
        attempt=int(row["attempt"]),
    )


def _complete_viral_media_preparation(
    conn: BusinessConnection, preparation: ViralMediaPreparationLease | None
) -> None:
    if preparation is None:
        return
    now = _time_text(datetime.now(UTC))
    updated = conn.execute(
        """
        UPDATE viral_media_preparations SET status = 'SUCCEEDED', locked_by = NULL,
            locked_until = NULL, error_code = NULL, completed_at = %s, updated_at = %s
        WHERE id = %s AND status = 'RUNNING' AND locked_by = %s
            AND owner_task_id = %s AND attempt = %s
            AND locked_until IS NOT NULL AND locked_until > %s
        """,
        (
            now,
            now,
            preparation.id,
            preparation.worker_id,
            preparation.owner_task_id,
            preparation.attempt,
            now,
        ),
    )
    if updated.rowcount != 1:
        raise _error(409, "VIRAL_MEDIA_PREPARATION_LEASE_LOST", "素材准备任务租约已失效。")


def fail_viral_media_preparation(
    conn: BusinessConnection,
    *,
    preparation: ViralMediaPreparationLease | None,
) -> None:
    if preparation is None:
        return
    now = _time_text(datetime.now(UTC))
    conn.execute(
        """
        UPDATE viral_media_preparations SET status = 'FAILED', locked_by = NULL,
            locked_until = NULL, error_code = 'VIRAL_MEDIA_PREPARATION_FAILED',
            completed_at = %s, updated_at = %s
        WHERE id = %s AND status = 'RUNNING' AND locked_by = %s
            AND owner_task_id = %s AND attempt = %s
        """,
        (
            now,
            now,
            preparation.id,
            preparation.worker_id,
            preparation.owner_task_id,
            preparation.attempt,
        ),
    )


def acquire_viral_import_task(
    conn: BusinessConnection, *, worker_id: str
) -> ViralImportLease | None:
    now = _time_text(datetime.now(UTC))
    locked_until = _time_text(datetime.now(UTC) + timedelta(minutes=VIRAL_IMPORT_LEASE_MINUTES))
    conn.execute(
        """
        UPDATE viral_import_tasks SET status = 'PENDING', locked_by = NULL,
            locked_until = NULL, error_code = NULL, error_message_redacted = NULL,
            retryable = 0, updated_at = %s
        WHERE status = 'RUNNING' AND locked_until IS NOT NULL AND locked_until <= %s
        """,
        (now, now),
    )
    row = conn.execute(
        """
        UPDATE viral_import_tasks SET status = 'RUNNING', attempt = attempt + 1,
            locked_by = %s, locked_until = %s, started_at = COALESCE(started_at, %s),
            updated_at = %s, error_code = NULL, error_message_redacted = NULL, retryable = 0
        WHERE id = (
            SELECT id FROM viral_import_tasks WHERE status = 'PENDING'
            ORDER BY created_at, id LIMIT 1 FOR UPDATE SKIP LOCKED
        ) AND status = 'PENDING' RETURNING *
        """,
        (worker_id, locked_until, now, now),
    ).fetchone()
    conn.commit()
    if row is None:
        return None
    return ViralImportLease(
        id=str(row["id"]),
        worker_id=worker_id,
        owner_user_id=str(row["owner_user_id"]),
        project_id=str(row["project_id"]),
        platform=str(row["platform"]),
        video_id=str(row["video_id"]),
        purpose=str(row["purpose"]),
        attempt=int(row["attempt"]),
    )


def _require_lease(conn: BusinessConnection, lease: ViralImportLease) -> sqlite3.Row:
    now = _time_text(datetime.now(UTC))
    row = conn.execute(
        """
        SELECT * FROM viral_import_tasks
        WHERE id = %s AND status = 'RUNNING' AND locked_by = %s
            AND attempt = %s AND locked_until IS NOT NULL AND locked_until > %s
        """,
        (lease.id, lease.worker_id, lease.attempt, now),
    ).fetchone()
    if row is None:
        raise _error(409, "VIRAL_IMPORT_LEASE_LOST", "导入任务租约已失效。")
    return cast(sqlite3.Row, row)


def _require_project_owner(conn: BusinessConnection, lease: ViralImportLease) -> None:
    project = conn.execute(
        "SELECT owner_user_id FROM projects WHERE id = %s", (lease.project_id,)
    ).fetchone()
    if project is None or str(project["owner_user_id"]) != lease.owner_user_id:
        raise _error(
            409,
            "VIRAL_IMPORT_PROJECT_CHANGED",
            "目标项目已不存在或归属已变化。",
            retryable=False,
        )


def prepare_viral_import_task(
    conn: BusinessConnection, *, lease: ViralImportLease, storage: StorageAdapter
) -> ViralImportWork:
    _require_lease(conn, lease)
    _require_import_enabled(conn)
    _require_project_owner(conn, lease)
    video = get_viral_video(conn, platform=lease.platform, video_id=lease.video_id)
    if video is None:
        raise _error(
            404,
            "VIRAL_VIDEO_NOT_FOUND",
            "爆款视频不存在或已下架。",
            retryable=False,
        )
    if (
        viral_video_availability(conn, platform=lease.platform, video_id=lease.video_id)
        != "available"
    ):
        raise _error(
            409,
            "VIRAL_VIDEO_UNAVAILABLE",
            "该爆款视频当前不可用于创作。",
            retryable=False,
        )
    verified_audio = bool(video.audio_url and video.native.get("source_audio_verified") is True)
    prefer: Literal["audio", "video"] = (
        "audio" if lease.purpose == "copy" and verified_audio else "video"
    )
    media_kind: Literal["audio", "video"] = "audio" if prefer == "audio" else "video"
    media_preparation = _claim_viral_media_preparation(
        conn,
        lease=lease,
        storage=storage,
        media_kind=media_kind,
    )
    try:
        client = viral_source_client_from_settings(conn)
    except ViralSourceUnavailable:
        # Any already cached object remains importable while the source is offline.
        client = None
    return ViralImportWork(
        lease=lease,
        video=video,
        client=client,
        storage=storage,
        prefer=prefer,
        media_preparation=media_preparation,
    )


def perform_viral_import_task(work: ViralImportWork) -> ViralImportOutcome:
    media: ViralMediaResult = ViralMediaPipeline(client=work.client, storage=work.storage).fetch(
        work.video, prefer=work.prefer
    )
    if media.kind not in {"audio", "video"}:
        raise RuntimeError("viral import returned an unsupported media kind")
    if work.lease.purpose == "replica" and media.kind != "video":
        raise RuntimeError("viral replica import requires video media")
    source = storage_object_ref_from_uri(media.storage_uri)
    require_storage_match(work.storage, source)
    extension = "mp3" if media.kind == "audio" else "mp4"
    destination = (
        f"projects/{work.lease.project_id}/viral-imports/{work.lease.id}/source.{extension}"
    )
    stored = work.storage.copy_object(source.key, destination)
    if (
        stored.size != media.size
        or not stored.sha256
        or (media.sha256 and stored.sha256 != media.sha256)
    ):
        work.storage.delete_object(destination, actor_id=work.lease.owner_user_id)
        raise RuntimeError("viral import storage verification failed")
    return ViralImportOutcome(
        stored=stored,
        media_kind=cast(Literal["audio", "video"], media.kind),
        duration_seconds=(work.video.duration_ms / 1000 if work.video.duration_ms > 0 else None),
        media_preparation=work.media_preparation,
    )


def complete_viral_import_task(
    conn: BusinessConnection, *, lease: ViralImportLease, outcome: ViralImportOutcome
) -> None:
    _require_lease(conn, lease)
    _require_project_owner(conn, lease)
    _complete_viral_media_preparation(conn, outcome.media_preparation)
    asset_id = str(uuid4())
    conn.execute(
        """
        INSERT INTO assets (
            id, project_id, kind, storage_uri, sha256, size_bytes,
            content_type, created_by_user_id, metadata_json
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            asset_id,
            lease.project_id,
            "reference_audio" if outcome.media_kind == "audio" else "reference_video",
            outcome.stored.uri,
            outcome.stored.sha256,
            outcome.stored.size,
            outcome.stored.content_type,
            lease.owner_user_id,
            json.dumps(
                {
                    "duration_seconds": outcome.duration_seconds,
                    "platform": lease.platform,
                    "video_id": lease.video_id,
                    "import_task_id": lease.id,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
        ),
    )
    result = {
        "projectId": lease.project_id,
        "sourceAssetId": asset_id,
        "mediaKind": outcome.media_kind,
        "canTranscribe": True,
        "canAnalyze": outcome.media_kind == "video",
    }
    now = _time_text(datetime.now(UTC))
    updated = conn.execute(
        """
        UPDATE viral_import_tasks SET status = 'SUCCEEDED', source_asset_id = %s,
            result_json = %s, locked_by = NULL, locked_until = NULL, retryable = 0,
            error_code = NULL, error_message_redacted = NULL, completed_at = %s, updated_at = %s
        WHERE id = %s AND status = 'RUNNING' AND locked_by = %s
            AND attempt = %s AND locked_until IS NOT NULL AND locked_until > %s
        """,
        (
            asset_id,
            json.dumps(result, ensure_ascii=False, sort_keys=True),
            now,
            now,
            lease.id,
            lease.worker_id,
            lease.attempt,
            now,
        ),
    )
    if updated.rowcount != 1:
        raise _error(409, "VIRAL_IMPORT_LEASE_LOST", "导入任务租约已失效。")
    conn.commit()


def fail_viral_import_task(
    conn: BusinessConnection, *, lease: ViralImportLease, cause: Exception
) -> None:
    logger.warning("viral import task %s failed: %s", lease.id, type(cause).__name__)
    now = _time_text(datetime.now(UTC))
    message = "爆款视频导入失败，请稍后重试。"
    error_code = "VIRAL_IMPORT_FAILED"
    retryable = True
    if isinstance(cause, ViralImportError) and isinstance(cause.detail, dict):
        message = str(cause.detail.get("message") or message)
        error_code = str(cause.detail.get("code") or error_code)
        retryable = cause.retryable
    conn.execute(
        """
        UPDATE viral_import_tasks SET status = 'FAILED', locked_by = NULL,
            locked_until = NULL, error_code = %s,
            error_message_redacted = %s, retryable = %s, completed_at = %s, updated_at = %s
        WHERE id = %s AND status = 'RUNNING' AND locked_by = %s
            AND attempt = %s AND locked_until IS NOT NULL AND locked_until > %s
        """,
        (
            error_code,
            message,
            1 if retryable else 0,
            now,
            now,
            lease.id,
            lease.worker_id,
            lease.attempt,
            now,
        ),
    )
    conn.commit()
