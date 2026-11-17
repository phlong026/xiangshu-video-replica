"""长音轨的持久化调度：一次提交或一次查询后归还工作线程。"""

from __future__ import annotations

import json
import logging
import math
import tempfile
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

from app import content_store
from app.asr import (
    AsrConfirmedFailure,
    AsrProvider,
    AsrProviderError,
    AsrTaskPending,
    DashScopeFunAsr,
    TranscriptResult,
)
from app.db_portable import BusinessConnection, BusinessRow
from app.media_tools import probe_duration_seconds, resolve_media_binary
from app.script_from_audio import (
    SCRIPT_FROM_AUDIO_MAX_SOURCE_BYTES,
    ScriptFromAudioTaskLease,
    _configured_asr,
    _object_key_from_uri,
    checkpoint_script_from_audio_task,
    complete_script_from_audio_task,
)
from app.segmented_media import extract_segment, stream_source, temp_root
from app.storage import StorageAdapter
from app.text_documents import PREVIEW_CHARS, create_document
from app.uncertain_auto_reopen import read_uncertain_auto_reopen_params
from app.usage_billing import begin_attempt, complete_attempt, find_operation, finish_source

logger = logging.getLogger(__name__)
Connection = Callable[[], AbstractContextManager[BusinessConnection]]
CORE_SECONDS = 600.0
OVERLAP_SECONDS = 1.0
MAX_INFLIGHT = 4


def _provider(conn: BusinessConnection, parent: BusinessRow) -> AsrProvider:
    provider = _configured_asr(conn)
    if isinstance(provider, DashScopeFunAsr) and parent["processing_policy_json"]:
        return provider.with_model_policy(json.loads(str(parent["processing_policy_json"])))
    return provider


class ProcessingCancelled(RuntimeError):
    pass


def segment_plan(duration: float) -> list[tuple[float, float, float, float]]:
    if not math.isfinite(duration) or not 0 < duration <= 43200:
        raise ValueError("来源时长必须在 12 小时以内。")
    return [
        (
            start,
            min(start + CORE_SECONDS, duration),
            max(0.0, start - OVERLAP_SECONDS),
            min(duration, start + CORE_SECONDS + OVERLAP_SECONDS),
        )
        for start in (index * CORE_SECONDS for index in range(math.ceil(duration / CORE_SECONDS)))
    ]


def _owned(conn: BusinessConnection, lease: ScriptFromAudioTaskLease) -> BusinessRow:
    row = conn.execute(
        "SELECT * FROM script_from_audio_tasks WHERE id=%s AND status='RUNNING' "
        "AND locked_by=%s AND attempt=%s AND locked_until::timestamptz>now() FOR UPDATE",
        (lease.id, lease.worker_id, lease.attempt),
    ).fetchone()
    if row is None:
        raise RuntimeError("转写任务租约已失效。")
    return cast(BusinessRow, row)


def _queue(
    conn: BusinessConnection, lease: ScriptFromAudioTaskLease, *, phase: str, delay: int = 5
) -> None:
    _owned(conn, lease)
    conn.execute(
        "UPDATE script_from_audio_tasks SET status='PENDING',processing_phase=%s,locked_by=NULL,"
        "locked_until=NULL,next_attempt_at=%s,updated_at=now() WHERE id=%s",
        (
            phase,
            (datetime.now(UTC) + timedelta(seconds=delay)).strftime("%Y-%m-%d %H:%M:%S"),
            lease.id,
        ),
    )
    conn.commit()


def _terminal(conn: BusinessConnection, lease: ScriptFromAudioTaskLease, code: str) -> None:
    _owned(conn, lease)
    conn.execute(
        "UPDATE script_from_audio_tasks SET status='FAILED',processing_phase=%s,"
        "error_code=%s,error_message_redacted=%s,retryable=0,completed_at=now(),updated_at=now(),"
        "locked_by=NULL,locked_until=NULL WHERE id=%s",
        (
            "CANCELLED" if code == "ASR_CANCELLED" else "FAILED",
            code,
            "文案提取已取消。"
            if code == "ASR_CANCELLED"
            else "文案分段处理未完成，请检查任务详情后重试。",
            lease.id,
        ),
    )
    finish_source(conn, lease.id, units=0, succeeded=False)
    conn.commit()


def merge_segments(rows: list[BusinessRow]) -> tuple[str, bool]:
    texts: list[str] = []
    exact = True
    for row in rows:
        result = json.loads(str(row["result_json"]))
        spans = result.get("spans") or []
        if spans:
            start, end, offset = (
                float(row["core_start"]),
                float(row["core_end"]),
                float(row["input_start"]),
            )
            text = "".join(
                str(span["text"])
                for span in spans
                if start <= offset + (float(span["start"]) + float(span["end"])) / 2 < end
            )
        else:
            # 无时间戳时宁可保留接缝的重复，也不误删讲者真实重复的内容。
            if str(result["text"]).strip():
                exact = False
            text = str(result["text"])
        texts.append(text)
    return "\n".join(texts), exact


def _prepare(
    lease: ScriptFromAudioTaskLease, connection: Connection, storage: StorageAdapter
) -> bool:
    with connection() as conn:
        parent = _owned(conn, lease)
        conn.execute("SELECT pg_advisory_xact_lock(720151901)")
        busy = conn.execute(
            "SELECT 1 FROM script_from_audio_tasks WHERE id<>%s AND status='RUNNING' "
            "AND processing_phase='PREPARING' AND locked_until::timestamptz>now() "
            "UNION ALL SELECT 1 FROM source_trim_tasks WHERE status='RUNNING' AND locked_until>now() LIMIT 1",
            (lease.id,),
        ).fetchone()
        if busy:
            _queue(conn, lease, phase="WAITING_FOR_MEDIA", delay=10)
            return False
        asset = conn.execute(
            "SELECT * FROM assets WHERE id=%s AND project_id=%s",
            (parent["source_asset_id"], lease.project_id),
        ).fetchone()
        if asset is None:
            raise ValueError("来源素材已删除。")
        metadata = json.loads(str(asset["metadata_json"] or "{}"))
        duration = float(metadata.get("duration_seconds") or metadata.get("duration_sec") or 0)
        for index, (start, end, input_start, input_end) in enumerate(segment_plan(duration)):
            conn.execute(
                "INSERT INTO "
                "asr_segments(task_id,segment_index,core_start,core_end,input_start,input_end,object_key) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (
                    lease.id,
                    index,
                    start,
                    end,
                    input_start,
                    input_end,
                    f"projects/{lease.project_id}/asr/{lease.id}/segment-{index}.m4a",
                ),
            )
        segments = conn.execute(
            "SELECT * FROM asr_segments WHERE task_id=%s AND "
            "status='PREPARING' ORDER BY segment_index",
            (lease.id,),
        ).fetchall()
        for segment in segments:
            conn.execute(
                "INSERT INTO media_temp_objects(object_key,task_id,storage_namespace) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                (segment["object_key"], lease.id, storage.cache_namespace),
            )
        if not segments:
            conn.commit()
            return True
        conn.execute(
            "UPDATE script_from_audio_tasks SET processing_phase='PREPARING' WHERE id=%s",
            (lease.id,),
        )
        conn.commit()

    def heartbeat() -> None:
        with connection() as conn:
            row = _owned(conn, lease)
            if row["cancel_requested_at"] is not None:
                raise ProcessingCancelled()
            checkpoint_script_from_audio_task(conn, lease=lease)

    with tempfile.TemporaryDirectory(prefix="segmented-asr-", dir=temp_root()) as directory:
        source = Path(directory) / "source"
        stream_source(
            storage,
            _object_key_from_uri(str(asset["storage_uri"])),
            source,
            expected_size=int(asset["size_bytes"]),
            expected_sha256=str(asset["sha256"]),
            max_bytes=SCRIPT_FROM_AUDIO_MAX_SOURCE_BYTES,
            heartbeat=heartbeat,
        )
        measured = probe_duration_seconds(resolve_media_binary("ffprobe"), source)
        if (
            measured is None
            or not math.isfinite(measured)
            or abs(measured - duration) > max(0.5, duration * 0.001)
        ):
            raise ValueError("来源时长与上传校验结果不一致。")
        for segment in segments:
            heartbeat()
            audio = Path(directory) / "segment.m4a"
            extract_segment(
                resolve_media_binary("ffmpeg"),
                source,
                audio,
                start=float(segment["input_start"]),
                end=float(segment["input_end"]),
                heartbeat=heartbeat,
            )
            storage.put_file(str(segment["object_key"]), audio, content_type="audio/mp4")
            with connection() as conn:
                _owned(conn, lease)
                conn.execute(
                    "UPDATE asr_segments SET "
                    "status='READY',updated_at=now() WHERE task_id=%s AND "
                    "segment_index=%s AND status='PREPARING'",
                    (lease.id, segment["segment_index"]),
                )
                conn.commit()
            audio.unlink(missing_ok=True)
    return True


def _persist_result(
    conn: BusinessConnection,
    lease: ScriptFromAudioTaskLease,
    segment: BusinessRow,
    result: TranscriptResult,
) -> None:
    _owned(conn, lease)
    payload = {
        "text": result.text,
        "language": result.language,
        "spans": [{"text": s.text, "start": s.start, "end": s.end} for s in result.spans],
    }
    conn.execute(
        "UPDATE asr_segments SET status='SUCCEEDED',result_json=%s,updated_at=now() "
        "WHERE task_id=%s AND segment_index=%s",
        (json.dumps(payload, ensure_ascii=False), lease.id, segment["segment_index"]),
    )
    complete_attempt(conn, attempt_id=str(segment["billing_attempt_id"]), usage=result.duration_sec)
    conn.execute(
        "UPDATE media_temp_objects SET keep_until=now() WHERE object_key=%s",
        (segment["object_key"],),
    )
    conn.commit()


def _poll(lease: ScriptFromAudioTaskLease, segment: BusinessRow, connection: Connection) -> None:
    try:
        with connection() as conn:
            provider = _provider(conn, _owned(conn, lease))
        if not isinstance(provider, DashScopeFunAsr):
            raise AsrTaskPending("已有分段回执需要兼容的转写配置。")
        result = provider.poll_segment(str(segment["provider_task_id"]))
        with connection() as conn:
            _owned(conn, lease)
            if result is not None:
                _persist_result(conn, lease, segment, result)
            else:
                conn.execute(
                    "UPDATE asr_segments SET next_poll_at=now()+interval "
                    "'10 seconds' WHERE task_id=%s AND segment_index=%s",
                    (lease.id, segment["segment_index"]),
                )
                conn.commit()
    except AsrConfirmedFailure as exc:
        with connection() as conn:
            _owned(conn, lease)
            complete_attempt(
                conn, attempt_id=str(segment["billing_attempt_id"]), usage=exc.usage_seconds
            )
            conn.execute(
                "UPDATE asr_segments SET "
                "status='FAILED',error_code='ASR_PROVIDER_FAILED',updated_at=now() WHERE task_id=%s AND segment_index=%s",
                (lease.id, segment["segment_index"]),
            )
            conn.commit()
    except Exception:
        with connection() as conn:
            _owned(conn, lease)
            conn.execute(
                "UPDATE asr_segments SET next_poll_at=now()+interval '30 "
                "seconds',error_code='ASR_POLL_RETRY' WHERE task_id=%s AND "
                "segment_index=%s",
                (lease.id, segment["segment_index"]),
            )
            conn.commit()


def _submit(
    lease: ScriptFromAudioTaskLease,
    segment: BusinessRow,
    connection: Connection,
    storage: StorageAdapter,
) -> bool:
    with connection() as conn:
        parent = _owned(conn, lease)
        if parent["cancel_requested_at"] is not None:
            return False
        provider = _provider(conn, parent)
        intent = storage.create_download_intent(
            str(segment["object_key"]), expires_in=timedelta(hours=6), can_read=True
        )
        if isinstance(provider, DashScopeFunAsr):
            provider._validate_audio_input(intent.url, duration_sec=None)
        # 事务锁把全局准入和 SUBMITTING 标记作为一次操作，跨 Worker 也不超额。
        conn.execute("SELECT pg_advisory_xact_lock(720151902)")
        active = conn.execute(
            "SELECT count(*) FROM asr_segments WHERE status IN ('SUBMITTING','POLLING','UNCERTAIN')"
        ).fetchone()[0]
        own_active = conn.execute(
            "SELECT count(*) FROM asr_segments WHERE task_id=%s AND status "
            "IN ('SUBMITTING','POLLING','UNCERTAIN')",
            (lease.id,),
        ).fetchone()[0]
        if int(active) >= MAX_INFLIGHT or int(own_active) >= 2:
            conn.commit()
            return False
        operation = find_operation(conn, lease.id)
        if operation is None:
            raise RuntimeError("提取计费单不存在。")
        attempt = begin_attempt(
            conn,
            operation_id=operation,
            attempt_key=f"segment:{segment['segment_index']}:{parent['reopen_count']}",
        )
        conn.execute(
            "UPDATE asr_segments SET "
            "status='SUBMITTING',billing_attempt_id=%s,submitted_at=now(),updated_at=now() WHERE task_id=%s AND segment_index=%s AND status='READY'",
            (attempt, lease.id, segment["segment_index"]),
        )
        conn.execute(
            "UPDATE media_temp_objects SET keep_until=now()+interval '6 hours' WHERE object_key=%s",
            (segment["object_key"],),
        )
        conn.commit()
    try:
        if isinstance(provider, DashScopeFunAsr):
            receipt = provider.submit_segment(intent.url)
            with connection() as conn:
                _owned(conn, lease)
                conn.execute(
                    "UPDATE asr_segments SET "
                    "status='POLLING',provider_task_id=%s,updated_at=now(),next_poll_at=now() WHERE task_id=%s AND segment_index=%s",
                    (receipt, lease.id, segment["segment_index"]),
                )
                conn.commit()
        else:
            result = provider.transcribe(
                intent.url, duration_sec=float(segment["input_end"]) - float(segment["input_start"])
            )
            with connection() as conn:
                current = conn.execute(
                    "SELECT * FROM asr_segments WHERE task_id=%s AND segment_index=%s",
                    (lease.id, segment["segment_index"]),
                ).fetchone()
                _persist_result(conn, lease, current, result)
        return True
    except Exception as exc:
        with connection() as conn:
            _owned(conn, lease)
            # 明确拒绝与无回执的传输不确定分开，不确定窗口不能立即重复计费。
            confirmed = isinstance(exc, AsrConfirmedFailure)
            conn.execute(
                "UPDATE asr_segments SET "
                "status=%s,error_code=%s,updated_at=now() WHERE task_id=%s "
                "AND segment_index=%s AND provider_task_id IS NULL",
                (
                    "FAILED" if confirmed else "UNCERTAIN",
                    "ASR_SUBMIT_FAILED" if confirmed else "ASR_SUBMISSION_UNCERTAIN",
                    lease.id,
                    segment["segment_index"],
                ),
            )
            if confirmed:
                complete_attempt(
                    conn, attempt_id=attempt, usage=cast(AsrProviderError, exc).usage_seconds
                )
            conn.commit()
        return True


def run_segmented_audio(
    lease: ScriptFromAudioTaskLease, *, connection: Connection, storage: StorageAdapter
) -> None:
    try:
        _run_step(lease, connection=connection, storage=storage)
    except Exception as exc:
        logger.warning("分段转写步骤中断 task=%s type=%s", lease.id, type(exc).__name__)
        with connection() as conn:
            try:
                _owned(conn, lease)
            except RuntimeError:
                return
            submitted = conn.execute(
                "SELECT 1 FROM asr_segments WHERE task_id=%s AND status IN "
                "('SUBMITTING','POLLING','UNCERTAIN') LIMIT 1",
                (lease.id,),
            ).fetchone()
            if submitted or isinstance(exc, ProcessingCancelled):
                _queue(
                    conn,
                    lease,
                    phase="DRAINING" if isinstance(exc, ProcessingCancelled) else "RECOVERING",
                    delay=30,
                )
            else:
                _terminal(conn, lease, "ASR_PREPARATION_FAILED")


def _run_step(
    lease: ScriptFromAudioTaskLease, *, connection: Connection, storage: StorageAdapter
) -> None:
    with connection() as conn:
        parent = _owned(conn, lease)
        # 只有重新取得父租约才会走到这里；无回执的 SUBMITTING 是上次提交窗口中断。
        recovery = read_uncertain_auto_reopen_params(conn)
        conn.execute(
            "UPDATE asr_segments SET "
            "status='UNCERTAIN',error_code='ASR_SUBMISSION_UNCERTAIN',updated_at=now() WHERE task_id=%s AND status='SUBMITTING' AND provider_task_id IS NULL",
            (lease.id,),
        )
        rows = conn.execute(
            "SELECT * FROM asr_segments WHERE task_id=%s ORDER BY segment_index", (lease.id,)
        ).fetchall()
        cancelled = parent["cancel_requested_at"] is not None
        failed = any(row["status"] == "FAILED" for row in rows)
        for row in rows:
            if row["status"] != "UNCERTAIN":
                continue
            if recovery["threshold_minutes"] <= 0 or recovery["max_reopens"] <= 0:
                continue
            submitted_at = row["submitted_at"]
            if isinstance(submitted_at, str):
                submitted_at = datetime.fromisoformat(submitted_at).replace(tzinfo=UTC)
            if submitted_at is not None and datetime.now(UTC) - submitted_at < timedelta(
                minutes=recovery["threshold_minutes"]
            ):
                continue
            complete_attempt(conn, attempt_id=str(row["billing_attempt_id"]), usage=None)
            reopen = (
                not cancelled
                and not failed
                and int(parent["reopen_count"]) < recovery["max_reopens"]
            )
            conn.execute(
                "UPDATE asr_segments SET status=%s,updated_at=now() WHERE "
                "task_id=%s AND segment_index=%s",
                ("READY" if reopen else "FAILED", lease.id, row["segment_index"]),
            )
            if reopen:
                conn.execute(
                    "UPDATE script_from_audio_tasks SET reopen_count=reopen_count+1 WHERE id=%s",
                    (lease.id,),
                )
                parent = conn.execute(
                    "SELECT * FROM script_from_audio_tasks WHERE id=%s", (lease.id,)
                ).fetchone()
            else:
                failed = True
        if cancelled or failed:
            conn.execute(
                "UPDATE asr_segments SET "
                "status='CANCELLED',updated_at=now() WHERE task_id=%s AND "
                "status IN ('READY','PREPARING')",
                (lease.id,),
            )
        conn.commit()
    if (
        not cancelled
        and not failed
        and (not rows or any(row["status"] == "PREPARING" for row in rows))
    ):
        if not _prepare(lease, connection, storage):
            return
    with connection() as conn:
        _owned(conn, lease)
        rows = conn.execute(
            "SELECT * FROM asr_segments WHERE task_id=%s ORDER BY segment_index", (lease.id,)
        ).fetchall()
        poll = next(
            (
                row
                for row in rows
                if row["status"] == "POLLING"
                and (row["next_poll_at"] is None or row["next_poll_at"] <= datetime.now(UTC))
            ),
            None,
        )
        ready = next((row for row in rows if row["status"] == "READY"), None)
        conn.commit()
    if poll is not None:
        _poll(lease, poll, connection)
    elif ready is not None and not cancelled and not failed:
        _submit(lease, ready, connection, storage)
    with connection() as conn:
        parent = _owned(conn, lease)
        rows = conn.execute(
            "SELECT * FROM asr_segments WHERE task_id=%s ORDER BY segment_index", (lease.id,)
        ).fetchall()
        if (
            rows
            and all(row["status"] == "SUCCEEDED" for row in rows)
            and parent["cancel_requested_at"] is None
        ):
            text, exact = merge_segments(rows)
            if not text.strip():
                _terminal(conn, lease, "ASR_NO_SPEECH")
                return
            document = create_document(
                conn, project_id=lease.project_id, owner_user_id=lease.created_by_user_id, text=text
            )
            conn.execute(
                "UPDATE script_from_audio_tasks SET processing_phase=%s WHERE id=%s",
                ("COMPLETE" if exact else "COMPLETE_CHECK_BOUNDARIES", lease.id),
            )
            complete_script_from_audio_task(
                conn,
                lease=lease,
                result=TranscriptResult(
                    text if len(text) <= 20000 else text[:PREVIEW_CHARS],
                    sum(float(r["core_end"]) - float(r["core_start"]) for r in rows),
                    "zh",
                ),
                document=document.model_dump(),
                segmented=True,
            )
        elif all(row["status"] in {"SUCCEEDED", "FAILED", "CANCELLED"} for row in rows):
            _terminal(
                conn,
                lease,
                "ASR_CANCELLED" if parent["cancel_requested_at"] else "ASR_SEGMENT_FAILED",
            )
        else:
            phase = "DRAINING" if cancelled or failed else "TRANSCRIBING"
            if any(row["status"] == "UNCERTAIN" for row in rows):
                phase = "WAITING_FOR_CONFIRMATION"
            _queue(conn, lease, phase=phase)


def cleanup_segment_audio(connection: Connection, storage: StorageAdapter) -> None:
    with connection() as conn:
        rows = conn.execute(
            "SELECT m.object_key,m.task_id FROM media_temp_objects m "
            "LEFT JOIN script_from_audio_tasks t ON t.id=m.task_id "
            "WHERE m.storage_namespace=%s AND m.keep_until<=now() "
            "AND (t.id IS NULL OR t.status IN ('SUCCEEDED','FAILED')) LIMIT 10",
            (storage.cache_namespace,),
        ).fetchall()
    for row in rows:
        try:
            content_store.delete_object_outside_content_namespace(
                storage, str(row["object_key"]), actor_id="segmented-asr-cleanup"
            )
            with connection() as conn:
                conn.execute(
                    "UPDATE asr_segments SET object_key=NULL WHERE task_id=%s AND object_key=%s",
                    (row["task_id"], row["object_key"]),
                )
                conn.execute(
                    "DELETE FROM media_temp_objects WHERE object_key=%s AND keep_until<=now()",
                    (row["object_key"],),
                )
                conn.commit()
        except Exception:
            logger.warning("分段音频清理延后 task=%s", row["task_id"])
