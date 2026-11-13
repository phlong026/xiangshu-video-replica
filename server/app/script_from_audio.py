"""工作台"提取文案"链路：上传视频 → 抽音轨 → ASR 转写（异步任务）。

任务编排使用 PENDING→RUNNING→终态、租约与提交不确定门禁。首次执行：
存储取原视频 → ffmpeg 抽音轨 → 登记并上传临时对象 → ASR 转写。异步回执
即刻入库，恢复时继续查询原任务；成功或明确失败后删除临时对象，清理失败
由 Worker 复删。无回执的不确定任务保留音频至签名 URL 过期。转写全文放在
任务 result_json——原始上传
没有镜头卡版本，无法写生成门禁管制的 ``/projects/{id}/scripts``，文案
工坊从任务结果取文本回填草稿，终稿发布仍走唯一的显式脚本版本路径。

2026-11-11 极简化改造：共享文案缓存（viral_script_cache）整体退役——
每个用户提取文案各自转写、各自付费，正文随自己的任务 result_json 直接
下发；``request_json.viral_source`` 仅作为内容经营报表的爆款身份归因
保留（来自服务端导入任务关联，不参与任何资格判定）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import uuid4

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app import content_store
from app.asr import (
    AsrConfirmedFailure,
    AsrProvider,
    AsrProviderError,
    AsrSubmissionUncertain,
    AsrTaskPending,
    DashScopeFunAsr,
    TranscriptResult,
    get_asr_provider,
)
from app.db_portable import BusinessConnection, BusinessRow
from app.media_tools import (
    MediaToolFailed,
    MediaToolUnavailable,
    extract_audio,
    probe_duration_seconds,
    resolve_media_binary,
)
from app.permissions import require_not_auditor, require_project_access
from app.storage import StorageAdapter
from app.task_lease import claim_script_from_audio_task

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

    # 2026-11-11 极简化：共享文案缓存退役后任务即本人付费转写的唯一交付点，
    # 正文恒随任务下发（不再有「爆款任务置空等 claim」的口径）。
    text: str | None = None
    duration_sec: float | None = None
    language: str | None = None


class ScriptFromAudioTaskResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    project_id: str
    source_asset_id: str
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


# 归档指纹同样存在于真实生产任务，只能结合结算依据判定旧正文权益。
# 保持任务响应和提取历史共用同一谓词；无结算证明的共享命中仍不下发正文。
LEGACY_SCRIPT_TEXT_ACCESS_SQL = """(
    NOT (t.request_json::jsonb ? 'viral_archive_sha256')
    OR EXISTS (
        SELECT 1 FROM billing_operations b
        WHERE b.service='asr' AND b.source_id=t.id
          AND b.user_id=t.created_by_user_id AND b.state='SUCCEEDED'
    )
    OR EXISTS (
        SELECT 1 FROM billing_operations b
        WHERE b.service='viral_copy' AND b.state='SUCCEEDED'
          AND b.source_id = 'viral-copy:' ||
              COALESCE((SELECT parent_user_id FROM users WHERE id=t.created_by_user_id),
                       t.created_by_user_id) || ':' ||
              (t.request_json::jsonb->'viral_source'->>0) || ':' ||
              (t.request_json::jsonb->'viral_source'->>1)
    )
)"""


def _legacy_cache_artifact(row: BusinessRow, conn: BusinessConnection | None = None) -> bool:
    """只隐藏没有本人转写结算或旧交付购买凭据的历史共享正文。"""
    try:
        payload = json.loads(str(row["request_json"] or "{}"))
    except (ValueError, TypeError):
        return False
    if not isinstance(payload, dict) or "viral_archive_sha256" not in payload:
        return False
    if conn is None:
        return True
    entitled = conn.execute(
        "SELECT 1 FROM script_from_audio_tasks t WHERE t.id=%s AND "
        + LEGACY_SCRIPT_TEXT_ACCESS_SQL,
        (row["id"],),
    ).fetchone()
    return entitled is None


def script_from_audio_task_response(
    row: BusinessRow, *, conn: BusinessConnection | None = None
) -> ScriptFromAudioTaskResponse:
    result_json = row["result_json"]
    result: ScriptFromAudioResult | None = None
    if result_json is not None:
        payload = json.loads(str(result_json))
        # 历史已结算成果继续交付；不能要求已付费用户重新转写。
        text = None if _legacy_cache_artifact(row, conn) else str(payload.get("text", ""))
        result = ScriptFromAudioResult(
            text=text,
            duration_sec=(
                None if payload.get("duration_sec") is None else float(payload["duration_sec"])
            ),
            language=(None if payload.get("language") is None else str(payload["language"])),
        )
    return ScriptFromAudioTaskResponse(
        id=str(row["id"]),
        project_id=str(row["project_id"]),
        source_asset_id=str(row["source_asset_id"]),
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
    asr: AsrProvider | None
    ffmpeg_path: str
    ffprobe_path: str | None
    audio_object_key: str
    provider_task_id: str | None = None
    audio_deleted: bool = False
    source_sha256: str | None = None
    source_size: int | None = None


def _iso_updated_at(value: object) -> str:
    return value.isoformat() if isinstance(value, datetime) else str(value)


def script_from_audio_error(status_code: int, code: str, message: str) -> Exception:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


def _time_text(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S")


def private_source_task(
    conn: BusinessConnection, *, owner_user_id: str, sha256: str, size_bytes: int
) -> BusinessRow | None:
    """私有音轨也按真实内容串行核对；更换项目或幂等键不能绕过未知提交。"""
    conn.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (f"private-asr:{owner_user_id}:{sha256}:{size_bytes}",),
    )
    row = conn.execute(
        "SELECT t.* FROM script_from_audio_tasks t JOIN assets a ON a.id=t.source_asset_id "
        "AND a.project_id=t.project_id WHERE t.created_by_user_id=%s AND a.sha256=%s "
        "AND a.size_bytes=%s AND (t.status IN ('PENDING','RUNNING','SUBMISSION_UNCERTAIN') "
        "OR (t.status='FAILED' AND t.provider_started_at IS NOT NULL "
        "AND COALESCE(t.error_code,'')<>'SCRIPT_FROM_AUDIO_PROVIDER_CONFIRMED_FAILED')) "
        "ORDER BY CASE WHEN t.status='SUBMISSION_UNCERTAIN' OR t.status='FAILED' "
        "THEN 0 ELSE 1 END,t.created_at,t.id LIMIT 1",
        (owner_user_id, sha256, size_bytes),
    ).fetchone()
    if row is not None and row["status"] in {"SUBMISSION_UNCERTAIN", "FAILED"}:
        raise script_from_audio_error(
            409,
            "SCRIPT_FROM_AUDIO_SOURCE_UNCERTAIN",
            "该音轨的上次提交结果尚未确认，请核对原任务；更换请求不能重新提交。",
        )
    return None if row is None else cast(BusinessRow, row)


def enqueue_script_from_audio_task(
    conn: BusinessConnection,
    *,
    actor: object,
    project_id: str,
    source_asset_id: str,
    idempotency_key: str,
) -> BusinessRow:
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
    asset = conn.execute(
        "SELECT * FROM assets WHERE id = %s AND project_id = %s",
        (source_asset_id, project_id),
    ).fetchone()
    if asset is None:
        raise script_from_audio_error(
            404,
            "SCRIPT_FROM_AUDIO_SOURCE_ASSET_MISSING",
            "来源视频不存在或已删除，请重新上传。",
        )
    _validate_source(asset)
    request_payload: dict[str, object] = {"source_asset_id": source_asset_id}
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
        return cast(BusinessRow, replay)
    source_task = private_source_task(
        conn,
        owner_user_id=str(getattr(actor, "id")),
        sha256=str(asset["sha256"]),
        size_bytes=int(asset["size_bytes"]),
    )
    if source_task is not None:
        return source_task
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
        return cast(BusinessRow, active)

    _configured_asr(conn)
    # 归因：内容经营报表按爆款身份聚合 ASR 收入/成本（viral_content_overview）。
    # 只作记录，不参与任何资格与计费判定；身份来自服务端导入任务关联，
    # 客户端自报的元数据不算数（2026-11-11 共享缓存退役后仅存的 viral_source）。
    attribution = conn.execute(
        "SELECT i.platform,i.video_id FROM viral_import_tasks i "
        "WHERE i.source_asset_id=%s AND i.project_id=%s AND i.status='SUCCEEDED' "
        "ORDER BY i.created_at,i.id LIMIT 1",
        (source_asset_id, project_id),
    ).fetchone()
    if attribution is not None:
        request_payload["viral_source"] = [
            str(attribution["platform"]),
            str(attribution["video_id"]),
        ]

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
            "SELECT * FROM script_from_audio_tasks WHERE project_id=%s AND idempotency_key=%s",
            (project_id, idempotency_key),
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
    if str(row["request_hash"]) != request_hash:
        raise script_from_audio_error(
            409, "SCRIPT_FROM_AUDIO_ENQUEUE_CONFLICT", "该项目已有不同来源的提取任务，请等待完成。"
        )
    if str(row["id"]) != task_id:
        return cast(BusinessRow, row)
    from app.permissions import write_audit

    # 每个用户为真实转写付自己的 ASR 按秒费（2026-11-11 极简化：无共享缓存，
    # 不再有「命中免费/交付费」口径）。
    from app.usage_billing import accept_operation

    metadata = json.loads(str(asset["metadata_json"] or "{}"))
    duration = metadata.get("duration_seconds") or metadata.get("duration_sec") or 0
    accept_operation(
        conn,
        user_id=actor.id,  # type: ignore[attr-defined]
        service="asr",
        source_id=str(row["id"]),
        units=duration,
    )
    write_audit(
        conn,
        actor=actor,  # type: ignore[arg-type]
        action="project.script_from_audio_enqueued",
        entity_type="script_from_audio_task",
        entity_id=str(row["id"]),
        metadata={
            "project_id": project_id,
            "request_hash": request_hash,
            "viral_attribution": attribution is not None,
        },
    )
    return cast(BusinessRow, row)


def acquire_script_from_audio_task(
    conn: BusinessConnection,
    *,
    worker_id: str,
) -> ScriptFromAudioTaskLease | None:
    """租约 SQL 统一住在 app.task_lease（T7）；本域凭据含 project_id。"""
    row = claim_script_from_audio_task(
        conn, worker_id=worker_id, lease_minutes=SCRIPT_FROM_AUDIO_TASK_LEASE_MINUTES
    )
    if row is None:
        return None
    return ScriptFromAudioTaskLease(
        id=str(row["id"]),
        project_id=str(row["project_id"]),
        created_by_user_id=str(row["created_by_user_id"]),
        worker_id=worker_id,
        attempt=int(row["attempt"]),
    )


def _require_leased_task(conn: BusinessConnection, lease: ScriptFromAudioTaskLease) -> BusinessRow:
    row = conn.execute(
        "SELECT * FROM script_from_audio_tasks WHERE id = %s AND status = 'RUNNING'",
        (lease.id,),
    ).fetchone()
    if (
        row is None
        or str(row["locked_by"]) != lease.worker_id
        or int(row["attempt"]) != lease.attempt
        or str(row["locked_until"] or "") <= _time_text(datetime.now(UTC))
    ):
        raise script_from_audio_error(409, "SCRIPT_FROM_AUDIO_LEASE_LOST", "任务租约已失效。")
    return cast(BusinessRow, row)


def _configured_asr(conn: BusinessConnection) -> AsrProvider:
    try:
        return get_asr_provider(conn)
    except (AsrProviderError, ValueError) as exc:
        logger.warning("ASR configuration unavailable: %s", type(exc).__name__)
        raise script_from_audio_error(
            503,
            "SCRIPT_FROM_AUDIO_SERVICE_UNAVAILABLE",
            "语音转写服务暂不可用，请联系管理员检查配置。",
        ) from exc


def _validate_source(asset: BusinessRow) -> None:
    if not str(asset["content_type"] or "").lower().startswith(("audio/", "video/")):
        raise script_from_audio_error(
            422, "SCRIPT_FROM_AUDIO_SOURCE_TYPE_INVALID", "请选择有效的视频或音频文件。"
        )
    if int(asset["size_bytes"] or 0) > SCRIPT_FROM_AUDIO_MAX_SOURCE_BYTES:
        raise script_from_audio_error(
            413, "SCRIPT_FROM_AUDIO_SOURCE_TOO_LARGE", "来源文件过大，请压缩后重新上传。"
        )


def _verified_task_payload(conn: BusinessConnection, row: BusinessRow) -> dict[str, object]:
    """任务载荷直接解析；``viral_source`` 仅是内容经营报表的归因记录（服务端写入）."""
    payload: dict[str, object] = json.loads(str(row["request_json"]))
    return payload


def prepare_script_from_audio_task(
    conn: BusinessConnection,
    *,
    lease: ScriptFromAudioTaskLease,
    storage: StorageAdapter,
) -> PreparedScriptFromAudio:
    row = _require_leased_task(conn, lease)
    payload = _verified_task_payload(conn, row)
    asset_id = str(payload.get("source_asset_id", ""))
    if row["provider_task_id"] is not None:
        return PreparedScriptFromAudio(
            task_id=lease.id,
            project_id=lease.project_id,
            asset_id=asset_id,
            object_key="",
            storage=storage,
            asr=_configured_asr(conn),
            ffmpeg_path="",
            ffprobe_path=None,
            audio_object_key=str(row["audio_object_key"] or ""),
            provider_task_id=str(row["provider_task_id"]),
        )
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
    _validate_source(asset)
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
    audio_key = str(row["audio_object_key"] or f"projects/{lease.project_id}/asr/{lease.id}.m4a")
    conn.execute(
        "UPDATE script_from_audio_tasks SET audio_object_key=%s WHERE id=%s "
        "AND status='RUNNING' AND locked_by=%s AND attempt=%s",
        (audio_key, lease.id, lease.worker_id, lease.attempt),
    )
    conn.commit()
    return PreparedScriptFromAudio(
        task_id=lease.id,
        project_id=lease.project_id,
        asset_id=asset_id,
        object_key=object_key,
        storage=storage,
        asr=_configured_asr(conn),
        ffmpeg_path=ffmpeg_path,
        ffprobe_path=ffprobe_path,
        audio_object_key=audio_key,
        provider_task_id=None if row["provider_task_id"] is None else str(row["provider_task_id"]),
        source_sha256=str(asset["sha256"]) if asset["sha256"] else None,
        source_size=int(asset["size_bytes"]) if asset["size_bytes"] is not None else None,
    )


def mark_script_from_audio_submission_started(
    conn: BusinessConnection,
    *,
    lease: ScriptFromAudioTaskLease,
) -> None:
    updated = conn.execute(
        """
        UPDATE script_from_audio_tasks
        SET provider_started_at = %s, updated_at = %s
        WHERE id = %s AND status='RUNNING' AND locked_by=%s AND attempt=%s
          AND locked_until > %s
        """,
        (
            _time_text(datetime.now(UTC)),
            _time_text(datetime.now(UTC)),
            lease.id,
            lease.worker_id,
            lease.attempt,
            _time_text(datetime.now(UTC)),
        ),
    )
    if updated.rowcount != 1:
        raise script_from_audio_error(409, "SCRIPT_FROM_AUDIO_LEASE_LOST", "任务租约已失效。")
    from app.usage_billing import begin_source_attempt

    begin_source_attempt(conn, lease.id)
    conn.commit()


def checkpoint_script_from_audio_task(
    conn: BusinessConnection,
    *,
    lease: ScriptFromAudioTaskLease,
    provider_task_id: str | None = None,
) -> None:
    now = datetime.now(UTC)
    updated = conn.execute(
        "UPDATE script_from_audio_tasks SET provider_task_id=COALESCE(%s,provider_task_id), "
        "locked_until=%s, updated_at=%s WHERE id=%s AND status='RUNNING' "
        "AND locked_by=%s AND attempt=%s AND locked_until>%s",
        (
            provider_task_id,
            _time_text(now + timedelta(minutes=SCRIPT_FROM_AUDIO_TASK_LEASE_MINUTES)),
            _time_text(now),
            lease.id,
            lease.worker_id,
            lease.attempt,
            _time_text(now),
        ),
    )
    if updated.rowcount != 1:
        raise script_from_audio_error(409, "SCRIPT_FROM_AUDIO_LEASE_LOST", "任务租约已失效。")
    conn.commit()


def perform_script_from_audio_task(
    work: PreparedScriptFromAudio,
    *,
    before_provider_call: Callable[[], None] | None = None,
    on_submitted: Callable[[str], None] | None = None,
    heartbeat: Callable[[], None] | None = None,
) -> TranscriptResult:
    """Known receipts resume without another upload/POST; pending work retains its audio."""
    if work.asr is None:
        raise AsrProviderError("转写服务暂不可用。")
    keep_audio = False
    try:
        if heartbeat is not None:
            heartbeat()
        if work.provider_task_id is not None:
            if not isinstance(work.asr, DashScopeFunAsr):
                raise AsrProviderError("当前转写配置无法恢复已有任务，请检查配置。")
            return work.asr.resume(work.provider_task_id, heartbeat=heartbeat)
        metadata = work.storage.head_object(work.object_key)
        if metadata is not None and metadata.size > SCRIPT_FROM_AUDIO_MAX_SOURCE_BYTES:
            raise script_from_audio_error(
                413, "SCRIPT_FROM_AUDIO_SOURCE_TOO_LARGE", "来源文件过大，请压缩后重新上传。"
            )
        video_bytes = work.storage.get_object(work.object_key)
        if len(video_bytes) > SCRIPT_FROM_AUDIO_MAX_SOURCE_BYTES:
            raise script_from_audio_error(
                413, "SCRIPT_FROM_AUDIO_SOURCE_TOO_LARGE", "来源文件过大，请压缩后重新上传。"
            )
        if (work.source_size is not None and len(video_bytes) != work.source_size) or (
            work.source_sha256 is not None
            and hashlib.sha256(video_bytes).hexdigest() != work.source_sha256
        ):
            raise script_from_audio_error(
                409, "SCRIPT_FROM_AUDIO_SOURCE_CHANGED", "来源素材校验失败，请重新上传。"
            )
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
            if heartbeat is not None:
                heartbeat()
            work.storage.put_object(work.audio_object_key, audio_bytes, content_type="audio/mp4")
            intent = work.storage.create_download_intent(
                work.audio_object_key,
                expires_in=_DOWNLOAD_INTENT_EXPIRES,
                can_read=True,
            )
            audio_input = intent.url
            if isinstance(work.asr, DashScopeFunAsr):
                audio_input = work.asr.prepare_audio_input(
                    audio_input, audio_bytes=audio_bytes, duration_sec=duration
                )
            if before_provider_call is not None:
                before_provider_call()
            if isinstance(work.asr, DashScopeFunAsr):
                return work.asr.transcribe(
                    audio_input,
                    duration_sec=duration,
                    on_submitted=on_submitted,
                    heartbeat=heartbeat,
                )
            return work.asr.transcribe(intent.url, duration_sec=duration)
    except (AsrTaskPending, AsrSubmissionUncertain):
        keep_audio = True
        raise
    finally:
        if not keep_audio and work.audio_object_key:
            try:
                # A superseded worker must not delete the current attempt's input.
                if heartbeat is not None:
                    heartbeat()
                content_store.delete_object_outside_content_namespace(
                    work.storage,
                    work.audio_object_key,
                    actor_id="script-from-audio-worker",
                )
                work.audio_deleted = True
            except Exception:
                logger.warning("temporary ASR audio cleanup deferred for task %s", work.task_id)


def complete_script_from_audio_task(
    conn: BusinessConnection,
    *,
    lease: ScriptFromAudioTaskLease,
    result: TranscriptResult,
    audio_deleted: bool = False,
) -> None:
    now = _time_text(datetime.now(UTC))
    result_payload: dict[str, object] = {
        "text": result.text,
        "duration_sec": result.duration_sec,
        "language": result.language,
    }
    # 租约守卫：不在持锁状态即抛 SCRIPT_FROM_AUDIO_LEASE_LOST。
    _require_leased_task(conn, lease)
    result_json = json.dumps(result_payload, ensure_ascii=False, sort_keys=True)
    updated = conn.execute(
        """
        UPDATE script_from_audio_tasks
        SET status = 'SUCCEEDED', result_json = %s,
            error_code = NULL, error_message_redacted = NULL, retryable = 0,
            completed_at = %s, updated_at = %s, locked_by = NULL,
            locked_until = NULL, provider_started_at = NULL,
            audio_object_key = CASE WHEN %s=1 THEN NULL ELSE audio_object_key END
        WHERE id = %s AND status='RUNNING' AND locked_by=%s AND attempt=%s AND locked_until>%s
        """,
        (
            result_json,
            now,
            now,
            int(audio_deleted),
            lease.id,
            lease.worker_id,
            lease.attempt,
            now,
        ),
    )
    if updated.rowcount != 1:
        raise script_from_audio_error(409, "SCRIPT_FROM_AUDIO_LEASE_LOST", "任务租约已失效。")
    from app.usage_billing import complete_source_attempt, finish_source

    complete_source_attempt(conn, lease.id, usage=result.duration_sec)
    if result.duration_sec is not None:
        finish_source(conn, lease.id, units=result.duration_sec, succeeded=True)
    else:
        # 供应商没回时长的成功结果按预留预算结算：文案已经交付，绝不能
        # 把计费单留在 PENDING（交付未扣费，且对账器按「SUCCEEDED 且有
        # 时长」过滤，永远兜不到这一行）。
        budget = conn.execute(
            "SELECT budget_units FROM billing_operations WHERE source_id=%s AND service='asr' "
            "ORDER BY billing_round DESC LIMIT 1",
            (lease.id,),
        ).fetchone()
        if budget is None or budget[0] is None:
            raise script_from_audio_error(
                503,
                "SCRIPT_FROM_AUDIO_BILLING_MISSING",
                "转写计费单缺失，请联系管理员核实。",
            )
        finish_source(conn, lease.id, units=budget[0], succeeded=True)
    conn.commit()


def fail_script_from_audio_task(
    conn: BusinessConnection,
    *,
    lease: ScriptFromAudioTaskLease,
    cause: Exception,
    submission_started: bool,
    audio_deleted: bool = False,
) -> None:
    logger.warning("script-from-audio task %s failed: %s", lease.id, type(cause).__name__)
    now = _time_text(datetime.now(UTC))
    row = conn.execute(
        "SELECT provider_task_id FROM script_from_audio_tasks WHERE id=%s "
        "AND status='RUNNING' AND locked_by=%s AND attempt=%s AND locked_until>%s",
        (lease.id, lease.worker_id, lease.attempt, now),
    ).fetchone()
    if row is None:
        return
    has_receipt = row["provider_task_id"] is not None
    retryable = 1 if not submission_started else 0
    next_attempt_at = None
    if has_receipt and (
        isinstance(cause, AsrTaskPending)
        or (isinstance(cause, HTTPException) and cause.status_code == 503)
    ):
        status = "PENDING"
        code = "SCRIPT_FROM_AUDIO_RESUMING"
        retryable = 0
        next_attempt_at = _time_text(datetime.now(UTC) + timedelta(seconds=10))
    elif isinstance(cause, (AsrTaskPending, AsrSubmissionUncertain)):
        status = "SUBMISSION_UNCERTAIN"
        code = "SCRIPT_FROM_AUDIO_SUBMISSION_UNCERTAIN"
        retryable = 0
    elif isinstance(cause, AsrConfirmedFailure):
        status = "FAILED"
        code = "SCRIPT_FROM_AUDIO_PROVIDER_CONFIRMED_FAILED"
        retryable = 0
    elif submission_started and isinstance(cause, AsrProviderError):
        status = "FAILED"
        code = "SCRIPT_FROM_AUDIO_PROVIDER_FAILED"
    elif submission_started:
        status = "SUBMISSION_UNCERTAIN"
        code = "SCRIPT_FROM_AUDIO_SUBMISSION_UNCERTAIN"
    else:
        status = "FAILED"
        code = "SCRIPT_FROM_AUDIO_PIPELINE_FAILED"
    updated = conn.execute(
        """
        UPDATE script_from_audio_tasks
        SET status = %s, error_code = %s,
            error_message_redacted = %s, retryable = %s,
            completed_at = %s, updated_at = %s, locked_by = NULL,
            locked_until = NULL, next_attempt_at=%s,
            audio_object_key = CASE WHEN %s=1 THEN NULL ELSE audio_object_key END
        WHERE id = %s AND status='RUNNING' AND locked_by=%s AND attempt=%s AND locked_until>%s
        """,
        (
            status,
            code,
            _redacted_message(cause),
            retryable,
            None if status == "PENDING" else now,
            now,
            next_attempt_at,
            int(audio_deleted),
            lease.id,
            lease.worker_id,
            lease.attempt,
            now,
        ),
    )
    if updated.rowcount != 1:
        conn.rollback()
        return
    if status in {"FAILED", "SUBMISSION_UNCERTAIN"}:
        from app.usage_billing import complete_source_attempt, finish_source

        complete_source_attempt(
            conn,
            lease.id,
            usage=cause.usage_seconds if isinstance(cause, AsrProviderError) else None,
        )
        finish_source(conn, lease.id, units=0, succeeded=False)
    conn.commit()


def load_script_from_audio_task(conn: BusinessConnection, task_id: str) -> BusinessRow:
    row = conn.execute(
        "SELECT * FROM script_from_audio_tasks WHERE id = %s",
        (task_id,),
    ).fetchone()
    if row is None:
        raise script_from_audio_error(404, "SCRIPT_FROM_AUDIO_TASK_NOT_FOUND", "提取任务不存在。")
    return cast(BusinessRow, row)


def latest_script_from_audio_task(
    conn: BusinessConnection, *, project_id: str
) -> BusinessRow | None:
    row = conn.execute(
        """
        SELECT * FROM script_from_audio_tasks
        WHERE project_id = %s
        ORDER BY created_at DESC, id DESC LIMIT 1
        """,
        (project_id,),
    ).fetchone()
    return None if row is None else cast(BusinessRow, row)


def _redacted_message(cause: Exception) -> str:
    if isinstance(cause, AsrProviderError):
        return str(cause)
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
