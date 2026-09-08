"""工作台"提取文案"链路：上传视频 → 抽音轨 → ASR 转写（异步任务）。

任务编排与 ``script_rewrite`` 同款（PENDING→RUNNING→终态 + 租约恢复 +
SUBMISSION_UNCERTAIN）。管线：存储取原视频字节 → 本机 ffmpeg 抽小音轨 →
临时对象上传存储并拿签名 URL → ASR 转写 → **临时音频即删**（成功与失败
终态都删除，重试时重新抽取）。转写全文放在任务 result_json——原始上传
没有镜头卡版本，无法写生成门禁管制的 ``/projects/{id}/scripts``，文案
工坊从任务结果取文本回填草稿，终稿发布仍走唯一的显式脚本版本路径。
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from app.asr import (
    AsrProvider,
    AsrProviderError,
    TranscriptResult,
    get_asr_provider,
)
from app.db_portable import BusinessConnection
from app.media_tools import (
    MediaToolFailed,
    MediaToolUnavailable,
    extract_audio,
    probe_duration_seconds,
    resolve_media_binary,
)
from app.permissions import require_not_auditor, require_project_access
from app.storage import StorageAdapter

logger = logging.getLogger("app.script_from_audio")

SCRIPT_FROM_AUDIO_TASK_LEASE_MINUTES = 20
SCRIPT_FROM_AUDIO_MAX_SOURCE_BYTES = 2_000_000_000
_DOWNLOAD_INTENT_EXPIRES = timedelta(minutes=30)


class ScriptFromAudioRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_asset_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str | None = Field(default=None, max_length=128)


class ScriptFromAudioResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str
    duration_sec: float | None = None
    language: str | None = None


class ScriptFromAudioTaskResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    project_id: str
    status: str
    attempt: int
    result: ScriptFromAudioResult | None
    error_code: str | None
    error_message: str | None
    retryable: bool
    created_at: str
    updated_at: str
    started_at: str | None
    completed_at: str | None


def script_from_audio_task_response(row: sqlite3.Row) -> ScriptFromAudioTaskResponse:
    result_json = row["result_json"]
    result: ScriptFromAudioResult | None = None
    if result_json is not None:
        payload = json.loads(str(result_json))
        result = ScriptFromAudioResult(
            text=str(payload.get("text", "")),
            duration_sec=(
                None if payload.get("duration_sec") is None else float(payload["duration_sec"])
            ),
            language=(None if payload.get("language") is None else str(payload["language"])),
        )
    return ScriptFromAudioTaskResponse(
        id=str(row["id"]),
        project_id=str(row["project_id"]),
        status=str(row["status"]),
        attempt=int(row["attempt"]),
        result=result,
        error_code=None if row["error_code"] is None else str(row["error_code"]),
        error_message=(
            None if row["error_message_redacted"] is None else str(row["error_message_redacted"])
        ),
        retryable=bool(row["retryable"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        started_at=None if row["started_at"] is None else str(row["started_at"]),
        completed_at=(None if row["completed_at"] is None else str(row["completed_at"])),
    )


@dataclass(frozen=True)
class ScriptFromAudioTaskLease:
    id: str
    project_id: str
    created_by_user_id: str
    worker_id: str
    attempt: int


@dataclass
class PreparedScriptFromAudio:
    task_id: str
    project_id: str
    asset_id: str
    object_key: str
    storage: StorageAdapter
    asr: AsrProvider
    ffmpeg_path: str
    ffprobe_path: str | None


def script_from_audio_error(status_code: int, code: str, message: str) -> Exception:
    from fastapi import HTTPException

    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


def _time_text(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S")


def enqueue_script_from_audio_task(
    conn: BusinessConnection,
    *,
    actor: object,
    project_id: str,
    source_asset_id: str,
    idempotency_key: str,
) -> sqlite3.Row:
    require_not_auditor(
        conn,
        actor=actor,  # type: ignore[arg-type]
        action="project.script_from_audio",
        entity_type="project",
        entity_id=project_id,
    )
    require_project_access(
        conn,
        actor=actor,  # type: ignore[arg-type]
        project_id=project_id,
        action="project.script_from_audio",
    )
    # Fail fast before a task is accepted; the worker re-resolves the
    # provider later and never persists credentials in request_json.
    get_asr_provider(conn)
    asset = conn.execute(
        "SELECT id FROM assets WHERE id = %s AND project_id = %s",
        (source_asset_id, project_id),
    ).fetchone()
    if asset is None:
        raise script_from_audio_error(
            404,
            "SCRIPT_FROM_AUDIO_SOURCE_ASSET_MISSING",
            "来源视频不存在或已删除，请重新上传。",
        )
    request_payload = {"source_asset_id": source_asset_id}
    request_hash = hashlib.sha256(
        json.dumps(
            request_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    replay = conn.execute(
        """
        SELECT * FROM script_from_audio_tasks
        WHERE project_id = %s AND idempotency_key = %s
        """,
        (project_id, idempotency_key),
    ).fetchone()
    if replay is not None:
        if str(replay["request_hash"]) != request_hash:
            raise script_from_audio_error(
                409,
                "SCRIPT_FROM_AUDIO_IDEMPOTENCY_CONFLICT",
                "提取内容已经变化，请重新提交。",
            )
        return cast(sqlite3.Row, replay)
    active = conn.execute(
        """
        SELECT * FROM script_from_audio_tasks
        WHERE project_id = %s AND status IN ('PENDING','RUNNING')
        ORDER BY created_at DESC, id DESC LIMIT 1
        """,
        (project_id,),
    ).fetchone()
    if active is not None:
        if str(active["request_hash"]) != request_hash:
            raise script_from_audio_error(
                409,
                "SCRIPT_FROM_AUDIO_ALREADY_RUNNING",
                "该项目已有文案提取任务在进行，请等待完成。",
            )
        return cast(sqlite3.Row, active)

    task_id = str(uuid4())
    conn.execute(
        """
        INSERT INTO script_from_audio_tasks (
            id, project_id, source_asset_id, created_by_user_id,
            idempotency_key, request_hash, request_json, status
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, 'PENDING')
        ON CONFLICT DO NOTHING
        """,
        (
            task_id,
            project_id,
            source_asset_id,
            actor.id,  # type: ignore[attr-defined]
            idempotency_key,
            request_hash,
            json.dumps(request_payload, ensure_ascii=False, sort_keys=True),
        ),
    )
    row = conn.execute(
        "SELECT * FROM script_from_audio_tasks WHERE id = %s",
        (task_id,),
    ).fetchone()
    if row is None:
        row = conn.execute(
            """
            SELECT * FROM script_from_audio_tasks
            WHERE project_id = %s AND status IN ('PENDING','RUNNING')
            ORDER BY created_at DESC, id DESC LIMIT 1
            """,
            (project_id,),
        ).fetchone()
    if row is None:
        raise script_from_audio_error(
            409,
            "SCRIPT_FROM_AUDIO_ENQUEUE_CONFLICT",
            "提取任务状态已经变化，请重试。",
        )
    from app.permissions import write_audit

    write_audit(
        conn,
        actor=actor,  # type: ignore[arg-type]
        action="project.script_from_audio_enqueued",
        entity_type="script_from_audio_task",
        entity_id=str(row["id"]),
        metadata={"project_id": project_id, "request_hash": request_hash},
    )
    return cast(sqlite3.Row, row)


def acquire_script_from_audio_task(
    conn: BusinessConnection,
    *,
    worker_id: str,
) -> ScriptFromAudioTaskLease | None:
    now = _time_text(datetime.now(UTC))
    locked_until = _time_text(
        datetime.now(UTC) + timedelta(minutes=SCRIPT_FROM_AUDIO_TASK_LEASE_MINUTES)
    )
    conn.execute(
        """
        UPDATE script_from_audio_tasks
        SET status = 'PENDING', locked_by = NULL, locked_until = NULL,
            error_code = NULL, error_message_redacted = NULL, retryable = 0,
            updated_at = %s
        WHERE status = 'RUNNING' AND provider_started_at IS NULL
          AND locked_until IS NOT NULL AND locked_until <= %s
        """,
        (now, now),
    )
    conn.execute(
        """
        UPDATE script_from_audio_tasks
        SET status = 'SUBMISSION_UNCERTAIN', locked_by = NULL, locked_until = NULL,
            error_code = 'SCRIPT_FROM_AUDIO_SUBMISSION_UNCERTAIN',
            error_message_redacted = %s, retryable = 0,
            completed_at = %s, updated_at = %s
        WHERE status = 'RUNNING' AND provider_started_at IS NOT NULL
          AND locked_until IS NOT NULL AND locked_until <= %s
        """,
        (
            "语音转写请求可能已经送达服务商，请人工确认后再决定是否重试。",
            now,
            now,
            now,
        ),
    )
    row = conn.execute(
        """
        UPDATE script_from_audio_tasks
        SET status = 'RUNNING', attempt = attempt + 1,
            locked_by = %s, locked_until = %s,
            started_at = COALESCE(started_at, %s), updated_at = %s,
            error_code = NULL, error_message_redacted = NULL, retryable = 0
        WHERE id = (
            SELECT id FROM script_from_audio_tasks
            WHERE status = 'PENDING'
            ORDER BY created_at, id LIMIT 1 FOR UPDATE SKIP LOCKED
        ) AND status = 'PENDING'
        RETURNING *
        """,
        (worker_id, locked_until, now, now),
    ).fetchone()
    conn.commit()
    if row is None:
        return None
    return ScriptFromAudioTaskLease(
        id=str(row["id"]),
        project_id=str(row["project_id"]),
        created_by_user_id=str(row["created_by_user_id"]),
        worker_id=worker_id,
        attempt=int(row["attempt"]),
    )


def _require_leased_task(conn: BusinessConnection, lease: ScriptFromAudioTaskLease) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM script_from_audio_tasks WHERE id = %s AND status = 'RUNNING'",
        (lease.id,),
    ).fetchone()
    if row is None or str(row["locked_by"]) != lease.worker_id:
        raise script_from_audio_error(409, "SCRIPT_FROM_AUDIO_LEASE_LOST", "任务租约已失效。")
    return cast(sqlite3.Row, row)


def prepare_script_from_audio_task(
    conn: BusinessConnection,
    *,
    lease: ScriptFromAudioTaskLease,
    storage: StorageAdapter,
) -> PreparedScriptFromAudio:
    row = _require_leased_task(conn, lease)
    payload = json.loads(str(row["request_json"]))
    asset_id = str(payload.get("source_asset_id", ""))
    asset = conn.execute(
        "SELECT * FROM assets WHERE id = %s AND project_id = %s",
        (asset_id, lease.project_id),
    ).fetchone()
    if asset is None:
        raise script_from_audio_error(
            404,
            "SCRIPT_FROM_AUDIO_SOURCE_ASSET_MISSING",
            "来源视频不存在或已删除，请重新上传。",
        )
    storage_uri = str(asset["storage_uri"])
    object_key = _object_key_from_uri(storage_uri)
    try:
        ffmpeg_path = resolve_media_binary("ffmpeg")
        ffprobe_path = resolve_media_binary("ffprobe")
    except MediaToolUnavailable as exc:
        raise script_from_audio_error(
            503,
            "SCRIPT_FROM_AUDIO_MEDIA_TOOL_MISSING",
            str(exc),
        ) from exc
    return PreparedScriptFromAudio(
        task_id=lease.id,
        project_id=lease.project_id,
        asset_id=asset_id,
        object_key=object_key,
        storage=storage,
        asr=get_asr_provider(conn),
        ffmpeg_path=ffmpeg_path,
        ffprobe_path=ffprobe_path,
    )


def mark_script_from_audio_submission_started(
    conn: BusinessConnection,
    *,
    lease: ScriptFromAudioTaskLease,
) -> None:
    conn.execute(
        """
        UPDATE script_from_audio_tasks
        SET provider_started_at = %s, updated_at = %s
        WHERE id = %s
        """,
        (_time_text(datetime.now(UTC)), _time_text(datetime.now(UTC)), lease.id),
    )
    conn.commit()


def perform_script_from_audio_task(
    work: PreparedScriptFromAudio,
) -> TranscriptResult:
    """抽取 → 临时对象 → 转写 → 即删。临时对象在 finally 中无条件清理。"""
    video_bytes = work.storage.get_object(work.object_key)
    tmp_key = f"tmp/asr/{work.project_id}/{hashlib.sha256(video_bytes).hexdigest()[:32]}.m4a"
    with tempfile.TemporaryDirectory(prefix="script-from-audio-") as tmp_dir:
        video_path = Path(tmp_dir) / "source-video"
        audio_path = Path(tmp_dir) / "extracted-audio.m4a"
        video_path.write_bytes(video_bytes)
        try:
            extract_audio(work.ffmpeg_path, video_path, audio_path)
            audio_bytes = audio_path.read_bytes()
        except MediaToolFailed as exc:
            raise AsrProviderError(f"音轨抽取失败：{exc}") from exc
        duration = (
            probe_duration_seconds(work.ffprobe_path, audio_path) if work.ffprobe_path else None
        )
        work.storage.put_object(tmp_key, audio_bytes, content_type="audio/mp4")
        try:
            intent = work.storage.create_download_intent(
                tmp_key,
                expires_in=_DOWNLOAD_INTENT_EXPIRES,
                can_read=True,
            )
            transcript = work.asr.transcribe(intent.url, duration_sec=duration)
        finally:
            # 转写用完即删（决策 #6）：成功、失败一律清理，不留音频残留。
            try:
                work.storage.delete_object(tmp_key, actor_id="script-from-audio-worker")
            except Exception:  # pragma: no cover - cleanup best effort
                logger.warning("temporary ASR audio cleanup failed for key %s", tmp_key)
    return transcript


def complete_script_from_audio_task(
    conn: BusinessConnection,
    *,
    lease: ScriptFromAudioTaskLease,
    result: TranscriptResult,
) -> None:
    now = _time_text(datetime.now(UTC))
    result_payload = {
        "text": result.text,
        "duration_sec": result.duration_sec,
        "language": result.language,
    }
    conn.execute(
        """
        UPDATE script_from_audio_tasks
        SET status = 'SUCCEEDED', result_json = %s,
            error_code = NULL, error_message_redacted = NULL, retryable = 0,
            completed_at = %s, updated_at = %s, locked_by = NULL,
            locked_until = NULL, provider_started_at = NULL
        WHERE id = %s
        """,
        (
            json.dumps(result_payload, ensure_ascii=False, sort_keys=True),
            now,
            now,
            lease.id,
        ),
    )
    conn.commit()


def fail_script_from_audio_task(
    conn: BusinessConnection,
    *,
    lease: ScriptFromAudioTaskLease,
    cause: Exception,
    submission_started: bool,
) -> None:
    logger.warning("script-from-audio task %s failed: %s", lease.id, type(cause).__name__)
    now = _time_text(datetime.now(UTC))
    retryable = 1 if not submission_started else 0
    if submission_started and isinstance(cause, AsrProviderError):
        status = "FAILED"
        code = "SCRIPT_FROM_AUDIO_PROVIDER_FAILED"
    elif submission_started:
        status = "SUBMISSION_UNCERTAIN"
        code = "SCRIPT_FROM_AUDIO_SUBMISSION_UNCERTAIN"
    else:
        status = "FAILED"
        code = "SCRIPT_FROM_AUDIO_PIPELINE_FAILED"
    conn.execute(
        """
        UPDATE script_from_audio_tasks
        SET status = %s, error_code = %s,
            error_message_redacted = %s, retryable = %s,
            completed_at = %s, updated_at = %s, locked_by = NULL,
            locked_until = NULL
        WHERE id = %s
        """,
        (
            status,
            code,
            _redacted_message(cause),
            retryable,
            now,
            now,
            lease.id,
        ),
    )
    conn.commit()


def load_script_from_audio_task(conn: BusinessConnection, task_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM script_from_audio_tasks WHERE id = %s",
        (task_id,),
    ).fetchone()
    if row is None:
        raise script_from_audio_error(404, "SCRIPT_FROM_AUDIO_TASK_NOT_FOUND", "提取任务不存在。")
    return cast(sqlite3.Row, row)


def latest_script_from_audio_task(
    conn: BusinessConnection, *, project_id: str
) -> sqlite3.Row | None:
    row = conn.execute(
        """
        SELECT * FROM script_from_audio_tasks
        WHERE project_id = %s
        ORDER BY created_at DESC, id DESC LIMIT 1
        """,
        (project_id,),
    ).fetchone()
    return None if row is None else cast(sqlite3.Row, row)


def _redacted_message(cause: Exception) -> str:
    if isinstance(cause, AsrProviderError):
        return str(cause)
    from fastapi import HTTPException

    if isinstance(cause, HTTPException):
        detail = cause.detail
        if isinstance(detail, dict) and detail.get("message"):
            return str(detail["message"])
    return "文案提取管线执行失败，请稍后重试。"


def _object_key_from_uri(storage_uri: str) -> str:
    """``provider://bucket/key`` → ``key``（key 内段保留原样）。"""
    marker = "://"
    if marker not in storage_uri:
        return storage_uri
    after_scheme = storage_uri.split(marker, 1)[1]
    if "/" not in after_scheme:
        return storage_uri
    return after_scheme.split("/", 1)[1]
