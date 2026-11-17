"""长文档改写按文本预算切段，成功段检查点不随重试丢失。"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import uuid4

from fastapi import HTTPException

from app.auth import CurrentUser
from app.db_portable import BusinessConnection, BusinessRow
from app.permissions import require_not_auditor, require_project_access
from app.script_rewrite import (
    ConfirmedRewriteResponseError,
    ScriptRewriteRequest,
    ScriptRewriteResult,
    ScriptRewriteTaskLease,
    ValidatedScriptRewriteRequest,
    _canonical_json,
    _load_owned_ip_profile_snapshot,
    _script_rewrite_request_hash,
    _validated_ip_profile_snapshot,
    load_script_rewrite_configuration,
    request_deepseek_text,
    require_current_script_rewrite_source,
)
from app.segmented_asr import Connection
from app.text_documents import (
    PREVIEW_CHARS,
    DocumentRef,
    create_document,
    document_text,
    owned_document,
    snapshot_document,
)
from app.uncertain_auto_reopen import read_uncertain_auto_reopen_params
from app.usage_billing import (
    accept_operation,
    begin_attempt,
    complete_attempt,
    find_operation,
    finish_source,
)

MAX_PART_BYTES = 6000


def split_text(text: str, max_bytes: int = MAX_PART_BYTES) -> list[str]:
    """UTF-8 字节预算比粗略字符数更保守，优先在段落与句尾切分。"""
    parts: list[str] = []
    pending = ""
    for unit in re.split(r"(?<=[。！？.!?\n])", text):
        for char in unit:
            if len((pending + char).encode("utf-8")) > max_bytes:
                parts.append(pending)
                pending = ""
            pending += char
        if len(pending.encode("utf-8")) >= max_bytes * 0.7:
            parts.append(pending)
            pending = ""
    if pending:
        parts.append(pending)
    return parts or [""]


def enabled() -> bool:
    return os.environ.get("VIDEO_REPLICA_SEGMENTED_REWRITE_ENABLED", "0") == "1"


def enqueue_long_rewrite(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    project_id: str,
    request: ScriptRewriteRequest,
    idempotency_key: str,
) -> BusinessRow:
    require_not_auditor(
        conn,
        actor=actor,
        action="project.script_rewrite",
        entity_type="project",
        entity_id=project_id,
    )
    require_project_access(
        conn, actor=actor, project_id=project_id, action="project.script_rewrite"
    )
    conn.execute("SELECT id FROM projects WHERE id=%s FOR UPDATE", (project_id,))
    if not enabled():
        raise HTTPException(
            503,
            detail={
                "code": "SEGMENTED_REWRITE_DISABLED",
                "message": "长文案改写尚未启用，请联系管理员。",
            },
        )
    if request.source_asset_id is not None:
        require_current_script_rewrite_source(
            conn, actor=actor, project_id=project_id, source_asset_id=request.source_asset_id
        )
    _, _, model = load_script_rewrite_configuration(conn)
    if request.document_id is not None:
        document = owned_document(conn, actor, request.document_id, write=True)
        if str(document["project_id"]) != project_id:
            raise HTTPException(
                422,
                detail={
                    "code": "REWRITE_DOCUMENT_PROJECT_MISMATCH",
                    "message": "文案与当前项目不一致。",
                },
            )
        frozen = snapshot_document(
            conn, actor, request.document_id, int(request.document_revision or 0)
        )
        text = document_text(conn, frozen.id)
    else:
        text = request.text.strip()
        # 文本请求使用内容地址，重复受理不会创建重复大快照。
        from uuid import NAMESPACE_URL, uuid5

        frozen = create_document(
            conn,
            project_id=project_id,
            owner_user_id=actor.id,
            text=text,
            kind="snapshot",
            document_id=str(
                uuid5(
                    NAMESPACE_URL,
                    f"rewrite-input:{actor.id}:{project_id}:{hashlib.sha256(text.encode()).hexdigest()}",
                )
            ),
        )
    if not text.strip():
        raise HTTPException(
            422, detail={"code": "SCRIPT_REWRITE_TEXT_REQUIRED", "message": "文案内容为空。"}
        )
    snapshot = (
        None
        if request.identity_id is None
        else _load_owned_ip_profile_snapshot(conn, actor=actor, identity_id=request.identity_id)
    )
    profile_json = None if snapshot is None else _canonical_json(snapshot)
    profile_hash = (
        None if profile_json is None else hashlib.sha256(profile_json.encode()).hexdigest()
    )
    length_match = re.search(r"^目标约\s*(\d+)\s*字", request.instructions)
    target_words = request.target_words
    if target_words is None and length_match is not None and int(length_match[1]) <= 3000:
        target_words = int(length_match[1])
    payload: dict[str, object] = {
        "text": text[:PREVIEW_CHARS] if request.document_id else text,
        "document": frozen.model_dump(),
        "instructions": request.instructions.strip(),
        "source_asset_id": request.source_asset_id,
        "identity_id": request.identity_id,
        "ip_profile_snapshot": snapshot,
        "target_words": target_words,
        "model": model,
        "part_bytes": MAX_PART_BYTES,
    }
    digest = _script_rewrite_request_hash(payload)
    replay = conn.execute(
        "SELECT * FROM script_rewrite_tasks WHERE project_id=%s AND idempotency_key=%s",
        (project_id, idempotency_key),
    ).fetchone()
    if replay is not None:
        if str(replay["request_hash"]) != digest:
            raise HTTPException(
                409,
                detail={
                    "code": "SCRIPT_REWRITE_IDEMPOTENCY_CONFLICT",
                    "message": "改写来源或要求已变化，请重新提交。",
                },
            )
        return cast(BusinessRow, replay)
    active = conn.execute(
        "SELECT * FROM script_rewrite_tasks WHERE project_id=%s AND status IN ('PENDING','RUNNING')",
        (project_id,),
    ).fetchone()
    if active is not None:
        raise HTTPException(
            409,
            detail={
                "code": "SCRIPT_REWRITE_ALREADY_RUNNING",
                "message": "该项目已有后台改写，请等待完成。",
            },
        )
    task_id = str(uuid4())
    row = conn.execute(
        "INSERT INTO script_rewrite_tasks(id,project_id,created_by_user_id,idempotency_key,request_hash,"
        "request_json,identity_id,ip_profile_snapshot_json,ip_profile_hash,status,processing_version) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'PENDING','segmented-v1') RETURNING *",
        (
            task_id,
            project_id,
            actor.id,
            idempotency_key,
            digest,
            _canonical_json(payload),
            request.identity_id,
            profile_json,
            profile_hash,
        ),
    ).fetchone()
    for index, part in enumerate(split_text(text)):
        conn.execute(
            "INSERT INTO rewrite_segments(task_id,segment_index,source_text,kind) VALUES (%s,%s,%s,%s)",
            (task_id, index, part, "summary" if target_words is not None else "rewrite"),
        )
    accept_operation(conn, user_id=actor.id, service="rewrite", source_id=task_id, units=1)
    return cast(BusinessRow, row)


def validated_long_request(row: BusinessRow) -> ValidatedScriptRewriteRequest:
    try:
        payload = json.loads(str(row["request_json"]))
        reference = DocumentRef.model_validate(payload["document"])
        if (
            not reference.immutable
            or reference.char_count < 1
            or payload["part_bytes"] != MAX_PART_BYTES
        ):
            raise ValueError("长文案快照不可用。")
        if _script_rewrite_request_hash(payload) != str(row["request_hash"]):
            raise ValueError("长文案请求校验失败。")
        snapshot = (
            None
            if payload["ip_profile_snapshot"] is None
            else _validated_ip_profile_snapshot(payload["ip_profile_snapshot"])
        )
        if payload["identity_id"] != row["identity_id"]:
            raise ValueError("人物快照身份不一致。")
        if snapshot is not None:
            digest = hashlib.sha256(_canonical_json(snapshot).encode()).hexdigest()
            if digest != row["ip_profile_hash"] or snapshot != json.loads(
                str(row["ip_profile_snapshot_json"])
            ):
                raise ValueError("人物快照校验失败。")
        return ValidatedScriptRewriteRequest(
            source_text=str(payload["text"]),
            source_asset_id=payload["source_asset_id"],
            ip_profile_snapshot=snapshot,
            instructions=str(payload["instructions"]),
            document=reference.model_dump(),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("长文案请求校验失败。") from exc


def _owned(conn: BusinessConnection, lease: ScriptRewriteTaskLease) -> BusinessRow:
    row = conn.execute(
        "SELECT * FROM script_rewrite_tasks WHERE id=%s AND status='RUNNING' AND locked_by=%s AND attempt=%s "
        "AND locked_until::timestamptz>now() FOR UPDATE",
        (lease.id, lease.worker_id, lease.attempt),
    ).fetchone()
    if row is None:
        raise RuntimeError("改写任务租约已失效。")
    return cast(BusinessRow, row)


def _queue(
    conn: BusinessConnection, lease: ScriptRewriteTaskLease, phase: str, delay: int = 1
) -> None:
    _owned(conn, lease)
    conn.execute(
        "UPDATE script_rewrite_tasks SET status='PENDING',processing_phase=%s,locked_by=NULL,locked_until=NULL,"
        "updated_at=now(),next_attempt_at=%s WHERE id=%s",
        (
            phase,
            (datetime.now(UTC) + timedelta(seconds=delay)).strftime("%Y-%m-%d %H:%M:%S"),
            lease.id,
        ),
    )
    conn.commit()


def _finish(
    conn: BusinessConnection,
    lease: ScriptRewriteTaskLease,
    parent: BusinessRow,
    *,
    text: str | None = None,
) -> None:
    _owned(conn, lease)
    if text is not None:
        document = create_document(
            conn,
            project_id=str(parent["project_id"]),
            owner_user_id=lease.created_by_user_id,
            text=text,
            kind="rewrite",
        )
        result = ScriptRewriteResult(
            rewritten_text=text[:PREVIEW_CHARS],
            provider="text-ai",
            model=str(json.loads(str(parent["request_json"]))["model"]),
            document=document.model_dump(),
        )
        conn.execute(
            "UPDATE script_rewrite_tasks SET status='SUCCEEDED',result_json=%s,processing_phase='COMPLETE',completed_at=now(),"
            "updated_at=now(),locked_by=NULL,locked_until=NULL WHERE id=%s",
            (_canonical_json(result.model_dump()), lease.id),
        )
        finish_source(conn, lease.id, units=1, succeeded=True)
    else:
        cancelled = parent["cancel_requested_at"] is not None
        conn.execute(
            "UPDATE script_rewrite_tasks SET status='FAILED',processing_phase=%s,error_code=%s,"
            "error_message_redacted=%s,retryable=0,completed_at=now(),updated_at=now(),locked_by=NULL,locked_until=NULL WHERE id=%s",
            (
                "CANCELLED" if cancelled else "FAILED",
                "REWRITE_CANCELLED" if cancelled else "REWRITE_SEGMENT_FAILED",
                "改写已取消。" if cancelled else "文案分段改写未完成，已保留成功段，请检查后重试。",
                lease.id,
            ),
        )
        finish_source(conn, lease.id, units=0, succeeded=False)
    conn.commit()


def run_segmented_rewrite(lease: ScriptRewriteTaskLease, *, connection: Connection) -> None:
    with connection() as conn:
        parent = _owned(conn, lease)
        request = validated_long_request(parent)
        payload = json.loads(str(parent["request_json"]))
        conn.execute(
            "UPDATE rewrite_segments SET status='UNCERTAIN',error_code='REWRITE_SUBMISSION_UNCERTAIN' "
            "WHERE task_id=%s AND status='SUBMITTING'",
            (lease.id,),
        )
        uncertain = conn.execute(
            "SELECT * FROM rewrite_segments WHERE task_id=%s AND status='UNCERTAIN' ORDER BY segment_index LIMIT 1",
            (lease.id,),
        ).fetchone()
        if uncertain is not None:
            recovery = read_uncertain_auto_reopen_params(conn)
            submitted = uncertain["submitted_at"]
            if (
                recovery["threshold_minutes"] <= 0
                or recovery["max_reopens"] <= 0
                or datetime.now(UTC) - submitted < timedelta(minutes=recovery["threshold_minutes"])
            ):
                _queue(conn, lease, "WAITING_FOR_CONFIRMATION", delay=30)
                return
            complete_attempt(conn, attempt_id=str(uncertain["billing_attempt_id"]), usage=None)
            reopen = (
                parent["cancel_requested_at"] is None
                and int(parent["reopen_count"]) < recovery["max_reopens"]
            )
            conn.execute(
                "UPDATE rewrite_segments SET status=%s WHERE task_id=%s AND segment_index=%s",
                ("READY" if reopen else "FAILED", lease.id, uncertain["segment_index"]),
            )
            if reopen:
                conn.execute(
                    "UPDATE script_rewrite_tasks SET reopen_count=reopen_count+1 WHERE id=%s",
                    (lease.id,),
                )
                _queue(conn, lease, "RETRYING_FAILED_SEGMENT")
                return
        rows = conn.execute(
            "SELECT * FROM rewrite_segments WHERE task_id=%s ORDER BY segment_index", (lease.id,)
        ).fetchall()
        failed = next((row for row in rows if row["status"] == "FAILED"), None)
        if (
            failed is not None
            and parent["cancel_requested_at"] is None
            and int(parent["reopen_count"]) < 2
        ):
            conn.execute(
                "UPDATE rewrite_segments SET status='READY' WHERE task_id=%s AND segment_index=%s",
                (lease.id, failed["segment_index"]),
            )
            conn.execute(
                "UPDATE script_rewrite_tasks SET reopen_count=reopen_count+1 WHERE id=%s",
                (lease.id,),
            )
            _queue(conn, lease, "RETRYING_FAILED_SEGMENT")
            return
        if parent["cancel_requested_at"] is not None or any(
            row["status"] == "FAILED" for row in rows
        ):
            conn.execute(
                "UPDATE rewrite_segments SET status='CANCELLED' WHERE task_id=%s AND status='READY'",
                (lease.id,),
            )
            _finish(conn, lease, parent)
            return
        part = next((row for row in rows if row["status"] == "READY"), None)
        if part is None:
            stage = max(int(row["stage"]) for row in rows)
            latest = [row for row in rows if int(row["stage"]) == stage]
            text = "\n".join(str(row["result_text"]) for row in latest)
            if latest[0]["kind"] in {"rewrite", "final"}:
                _finish(conn, lease, parent, text=text)
                return
            final = len(text.encode("utf-8")) <= MAX_PART_BYTES
            next_index = max(int(row["segment_index"]) for row in rows) + 1
            for index, source in enumerate([text] if final else split_text(text)):
                conn.execute(
                    "INSERT INTO rewrite_segments(task_id,segment_index,stage,kind,source_text) VALUES (%s,%s,%s,%s,%s)",
                    (
                        lease.id,
                        next_index + index,
                        stage + 1,
                        "final" if final else "summary",
                        source,
                    ),
                )
            _queue(conn, lease, "REDUCING")
            return
        base_url, api_key, _ = load_script_rewrite_configuration(conn)
        conn.execute("SELECT pg_advisory_xact_lock(720151903)")
        active = conn.execute(
            "SELECT count(*) FROM rewrite_segments WHERE status IN ('SUBMITTING','UNCERTAIN')"
        ).fetchone()[0]
        if int(active) >= 2:
            _queue(conn, lease, "WAITING_FOR_CAPACITY", delay=10)
            return
        operation = find_operation(conn, lease.id)
        if operation is None:
            raise RuntimeError("改写计费单不存在。")
        attempt = begin_attempt(
            conn,
            operation_id=operation,
            attempt_key=f"part:{part['segment_index']}:{parent['reopen_count']}",
        )
        conn.execute(
            "UPDATE rewrite_segments SET status='SUBMITTING',billing_attempt_id=%s,submitted_at=now(),updated_at=now() "
            "WHERE task_id=%s AND segment_index=%s",
            (attempt, lease.id, part["segment_index"]),
        )
        conn.execute(
            "UPDATE script_rewrite_tasks SET locked_until=%s,processing_phase='REWRITING' WHERE id=%s",
            ((datetime.now(UTC) + timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S"), lease.id),
        )
        conn.commit()
    try:
        instructions = request.instructions
        if part["kind"] == "summary":
            instructions += "\n仅生成不超过 150 字的事实提纲，保留人物、数字、单位、时间和观点，不添加原文没有的事实。"
        elif part["kind"] == "final":
            instructions += f"\n根据这些事实提纲写成约 {payload['target_words']} 字的完整口播稿。"
        else:
            match = re.search(r"^目标约\s*(\d+)\s*字", instructions)
            if match is not None and request.document:
                count = max(1, int(str(request.document["char_count"])))
                local_words = max(1, round(int(match[1]) * len(str(part["source_text"])) / count))
                instructions = instructions.replace(match[0], f"本段目标约 {local_words} 字", 1)
            instructions += (
                "\n这是长稿的一段，保持信息与大致长度；衔接正文，不添加开场白、标题或结尾总结。"
            )
        rewritten = request_deepseek_text(
            base_url=base_url,
            api_key=api_key,
            model=str(payload["model"]),
            source_text=str(part["source_text"]),
            ip_profile_snapshot=request.ip_profile_snapshot,
            instructions=instructions,
        )
        if part["kind"] == "summary" and len(rewritten.encode("utf-8")) > 900:
            raise ConfirmedRewriteResponseError(
                502,
                detail={"code": "REWRITE_SUMMARY_TOO_LONG", "message": "文案提纲超出处理预算。"},
            )
        with connection() as conn:
            parent = _owned(conn, lease)
            complete_attempt(conn, attempt_id=attempt, usage=1)
            conn.execute(
                "UPDATE rewrite_segments SET status='SUCCEEDED',result_text=%s,updated_at=now() WHERE task_id=%s AND segment_index=%s",
                (rewritten, lease.id, part["segment_index"]),
            )
            _queue(conn, lease, "REWRITING")
    except Exception as exc:
        with connection() as conn:
            _owned(conn, lease)
            confirmed = isinstance(exc, ConfirmedRewriteResponseError)
            if confirmed:
                complete_attempt(conn, attempt_id=attempt, usage=1)
            conn.execute(
                "UPDATE rewrite_segments SET status=%s,error_code=%s,updated_at=now() WHERE task_id=%s AND segment_index=%s",
                (
                    "FAILED" if confirmed else "UNCERTAIN",
                    "REWRITE_RESPONSE_INVALID" if confirmed else "REWRITE_SUBMISSION_UNCERTAIN",
                    lease.id,
                    part["segment_index"],
                ),
            )
            _queue(conn, lease, "RECOVERING", delay=30)
