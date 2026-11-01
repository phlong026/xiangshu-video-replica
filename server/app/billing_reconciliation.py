"""卡单积分的核对与结算闭环（T40）。

internal_billing 的证据门禁会按设计把部分预留无限期留在 PENDING：视频成功缺
成片秒数、口播缺交付证据、first_frame 的交付数只认发布事务、提交结果未知等。
这些"卡单"此前只有 30 秒一轮的 reconcile_operations 按任务终态条件收口，条件
不满足的分支永远悬着，也看不到等待了多久、下一步该做什么。

本模块把卡单收进一张可追踪队列：定时扫描入队（含 T38 人物删除之前的孤儿
预留）、自动重查证据齐了的条目、超时只升级人工——未知结果绝不自动重投、
未知费用绝不自动退回。SETTLE/RELEASE 仍由 usage_billing / internal_billing
的既有路径写出，后台真实记账语义不变。
"""

from __future__ import annotations

import json
import os
from typing import Any
from uuid import uuid4

from app.api_errors import http_error as _http
from app.db_portable import BusinessConnection
from app.usage_billing import finalize_pending_operation

# 只扫真金白银的服务线（768P 16-30 秒长档与 768P 同走 generation_tasks）；
# viral_* 等小额服务有自己的 30 分钟释放窗口，且它们的源行不在这几张任务表里，
# 混进来会把正常条目误报成孤儿。
_MONEY_SERVICES = (
    "video_768p",
    "video_768p_long",
    "video_2k",
    "oral",
    "first_frame",
    "asr",
    "character",
)

# 这些原因禁止自动收口：未知结果必须先核对（不自动重投也不自动退分），
# first_frame 的交付数只认发布事务，孤儿没有源任务可核对。
_NEVER_AUTO_FINALIZE = frozenset(
    {"UNKNOWN_TIMEOUT", "DELIVERY_UNFINALIZED", "ORPHAN_SOURCE_MISSING"}
)

_REASON_LABELS = {
    "SUCCESS_MISSING_DURATION": "任务成功但缺成片时长，等待计费证据落账",
    "SUCCESS_EVIDENCE_MISSING": "任务成功但交付/归档证据不全，无法结算",
    "UNKNOWN_TIMEOUT": "提交结果未知超时，需人工核对供应商实况",
    "DELIVERY_UNFINALIZED": "任务成功但发布事务未收口交付数",
    "ORPHAN_SOURCE_MISSING": "计费预留找不到源任务记录",
}

_STUCK_MINUTES_ENV = "VIDEO_REPLICA_BILLING_STUCK_AFTER_MINUTES"
_RECHECK_MINUTES_ENV = "VIDEO_REPLICA_BILLING_RECHECK_AFTER_MINUTES"
_ESCALATE_HOURS_ENV = "VIDEO_REPLICA_BILLING_ESCALATE_AFTER_HOURS"


def _env_number(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    # 0 是合法值（重查间隔 0 = 每次都重查），只拒绝负数。
    return value if value >= 0 else default


def _stuck_after_minutes() -> float:
    return _env_number(_STUCK_MINUTES_ENV, 60.0)


def _recheck_after_minutes() -> float:
    return _env_number(_RECHECK_MINUTES_ENV, 30.0)


def _escalate_after_hours() -> float:
    return _env_number(_ESCALATE_HOURS_ENV, 24.0)


def next_step_for(state: str, reason: str) -> str:
    """队列页给运营的下一步指引；与自动/人工边界一一对应。"""
    if state == "RESOLVED":
        return "已收口，可在明细里核对最终账务。"
    if state == "ESCALATED":
        if reason == "ORPHAN_SOURCE_MISSING":
            return "已升级人工：源任务缺失需逐笔核证，调账须走调账审批，不可凭缺源退分。"
        if reason == "UNKNOWN_TIMEOUT":
            return "已升级人工：请先用专项对账接口核对供应商实况，再回到这里收口。"
        return "已升级人工：请通过专项对账或调账审批收口，本队列不会自动退分。"
    if reason in _NEVER_AUTO_FINALIZE:
        return "等待人工核对；系统只重查不代答。"
    return "等待自动重查：证据补齐后自动结算或释放。"


def _classify(
    service: str,
    row: Any,
) -> tuple[str, str] | None:
    """返回 (reason, source_kind)；None 表示该条不属卡单，交回 reconcile。

    每个服务的 source_kind 固定（同一服务只可能落在对应任务表上），
    状态判断直接读行里的状态列。
    """
    if service in ("video_768p", "video_768p_long", "video_2k"):
        if row["gen_status"] is None:
            return ("ORPHAN_SOURCE_MISSING", "generation")
        if row["gen_status"] == "SUCCEEDED":
            if row["gen_seconds"] is None:
                return ("SUCCESS_MISSING_DURATION", "generation")
            return ("SUCCESS_EVIDENCE_MISSING", "generation")
        if row["gen_status"] in ("SUBMISSION_UNCERTAIN", "ARCHIVE_FAILED"):
            if row["gen_status"] == "SUBMISSION_UNCERTAIN":
                return ("UNKNOWN_TIMEOUT", "generation")
            return ("SUCCESS_EVIDENCE_MISSING", "generation")
        return None
    if service == "oral":
        if row["ora_status"] is None:
            return ("ORPHAN_SOURCE_MISSING", "oral")
        if row["ora_status"] == "SUBMISSION_UNCERTAIN":
            return ("UNKNOWN_TIMEOUT", "oral")
        if row["ora_status"] in ("ARCHIVING", "ARCHIVE_FAILED", "SUCCEEDED"):
            return ("SUCCESS_EVIDENCE_MISSING", "oral")
        return None
    if service == "first_frame":
        if row["ff_status"] is None:
            return ("ORPHAN_SOURCE_MISSING", "first_frame")
        if row["ff_status"] == "SUCCEEDED":
            return ("DELIVERY_UNFINALIZED", "first_frame")
        return None
    if service == "asr":
        if row["asr_status"] is None:
            return ("ORPHAN_SOURCE_MISSING", "asr")
        # 只有「成功但缺时长」是卡单；FAILED/CANCELLED/SUBMISSION_UNCERTAIN
        # 属 reconcile 资格内（会被释放），不能误标成成功类原因。
        if row["asr_status"] == "SUCCEEDED" and row["asr_duration"] is None:
            return ("SUCCESS_MISSING_DURATION", "asr")
        return None
    if service == "character":
        if row["csheet_status"] is not None:
            # sheet 任务的全部终态（含 SUBMISSION_UNCERTAIN）都在 reconcile 资格内。
            return None
        if row["cgen_status"] is not None:
            return None
        return ("ORPHAN_SOURCE_MISSING", "character")
    return None


def enqueue_stuck_billing_operations(conn: BusinessConnection, *, limit: int = 100) -> int:
    """把卡了超过时限的 PENDING 计费收进核对队列，返回新建条数。

    已在队列里的条目只刷新原因与证据快照（部分唯一索引兜底，不会重复建）；
    队列已收口的轮次若再出现新的 PENDING（新一轮预留），部分唯一索引不约束
    已 RESOLVED 行，会另建新条目。供应商请求号不进扫描查询——task_diagnostic_refs
    的主键是 (task_type, task_id)，按 task_id 单列捞会让卡单存续期内的每轮
    扫描都退化为全表扫；这里只对已判定的卡单行（每轮最多 limit 条）按
    (task_type, task_id) 做主键点查。
    """
    rows = conn.execute(
        """
        SELECT o.id AS operation_id, o.service, o.source_id, o.user_id,
               COALESCE(o.billing_round, 1) AS billing_round, o.created_at,
               gen.status AS gen_status, gen.actual_output_seconds AS gen_seconds,
               ora.status AS ora_status,
               asr.status AS asr_status,
               asr.result_json::jsonb->>'duration_sec' AS asr_duration,
               ff.status AS ff_status,
               csheet.status AS csheet_status,
               cgen.status AS cgen_status
        FROM billing_operations o
        LEFT JOIN generation_tasks gen ON gen.id = o.source_id
        LEFT JOIN oral_tasks ora ON ora.id = o.source_id
        LEFT JOIN script_from_audio_tasks asr ON asr.id = o.source_id
        LEFT JOIN first_frame_tasks ff ON ff.id = o.source_id
        LEFT JOIN character_sheet_tasks csheet ON csheet.id = o.source_id
        LEFT JOIN character_generation_tasks cgen ON cgen.id = o.source_id
        WHERE o.state = 'PENDING' AND o.user_id IS NOT NULL
          AND o.service = ANY(%s)
          AND o.created_at < now() - (%s * interval '1 minute')
        ORDER BY o.created_at, o.id
        LIMIT %s
        FOR UPDATE OF o SKIP LOCKED
        """,
        (list(_MONEY_SERVICES), _stuck_after_minutes(), limit),
    ).fetchall()
    enqueued = 0
    for row in rows:
        classified = _classify(str(row["service"]), row)
        if classified is None:
            continue
        reason, source_kind = classified
        detail: dict[str, Any] = {}
        if row["gen_status"] is not None:
            detail["task_status"] = row["gen_status"]
            if row["gen_seconds"] is not None:
                detail["actual_output_seconds"] = float(row["gen_seconds"])
        elif row["ora_status"] is not None:
            detail["task_status"] = row["ora_status"]
        elif row["asr_status"] is not None:
            detail["task_status"] = row["asr_status"]
            if row["asr_duration"] is not None:
                detail["duration_sec"] = float(row["asr_duration"])
        elif row["ff_status"] is not None:
            detail["task_status"] = row["ff_status"]
        elif row["csheet_status"] is not None:
            detail["task_status"] = row["csheet_status"]
        elif row["cgen_status"] is not None:
            detail["task_status"] = row["cgen_status"]
        result = conn.execute(
            """
            INSERT INTO billing_reconciliation_items (
                id, service, source_kind, source_id, billing_operation_id,
                billing_round, owner_user_id, reason, detail_json,
                provider_receipt_id, state, created_at, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'OPEN',
                      %s, now())
            ON CONFLICT (service, source_id, billing_round) WHERE state = 'OPEN'
            DO UPDATE SET reason = EXCLUDED.reason,
                          detail_json = EXCLUDED.detail_json,
                          updated_at = now()
            RETURNING (xmax = 0) AS inserted
            """,
            (
                str(uuid4()),
                str(row["service"]),
                source_kind,
                str(row["source_id"]),
                str(row["operation_id"]),
                int(row["billing_round"]),
                None if row["user_id"] is None else str(row["user_id"]),
                reason,
                json.dumps(detail, ensure_ascii=False),
                _provider_request_id(conn, str(row["service"]), str(row["source_id"])),
                row["created_at"],
            ),
        ).fetchone()
        enqueued += int(result[0]) if result is not None else 0
    return enqueued


# 服务 → task_diagnostic_refs 的 (task_type, 候选任务表) 映射；与迁移
# 20261002T0630 的登记口径一致。asr 任务不登记诊断引用，恒为 None。
_DIAGNOSTIC_REFS: dict[str, tuple[tuple[str, str], ...]] = {
    "video_768p": (("generation_tasks", "VIDEO"),),
    "video_768p_long": (("generation_tasks", "VIDEO"),),
    "video_2k": (("generation_tasks", "VIDEO"),),
    "oral": (("oral_tasks", "ORAL_VIDEO"),),
    "first_frame": (("first_frame_tasks", "FIRST_FRAME_IMAGE"),),
    "character": (
        ("character_sheet_tasks", "CHARACTER_SHEET_IMAGE"),
        ("character_generation_tasks", "CHARACTER_VIEW_IMAGE"),
    ),
}


def _provider_request_id(conn: BusinessConnection, service: str, source_id: str) -> str | None:
    """按主键查供应商请求号；查不到（含 asr 无登记）返回 None，不阻塞入队。"""
    for task_type_key, task_type in _DIAGNOSTIC_REFS.get(service, ()):
        found = conn.execute(
            f"""
            SELECT ref.request_id FROM task_diagnostic_refs ref
            JOIN {task_type_key} task ON task.id = %s
            WHERE ref.task_type = %s AND ref.task_id = task.id
            LIMIT 1
            """,  # noqa: S608 - 固定表名白名单
            (source_id, task_type),
        ).fetchone()
        if found is not None:
            return str(found["request_id"])
    return None


def process_reconciliation_items(conn: BusinessConnection, *, limit: int = 50) -> dict[str, int]:
    """重查到期条目：证据齐了自动收口，超时升级人工，终态只写一次。

    升级只改变队列状态（提醒人工接手），绝不代替人做退分或调账；ESCALATED
    条目停止自动收口，只在底层计费已终态时被动对齐（迟到回调场景）。
    """
    rows = conn.execute(
        """
        SELECT id, service, source_id, billing_operation_id, reason, state,
               created_at
        FROM billing_reconciliation_items
        WHERE state IN ('OPEN', 'ESCALATED')
          AND (last_checked_at IS NULL
               OR last_checked_at < now() - (%s * interval '1 minute'))
        ORDER BY created_at, id
        LIMIT %s
        FOR UPDATE SKIP LOCKED
        """,
        (_recheck_after_minutes(), limit),
    ).fetchall()
    stats = {"resolved": 0, "escalated": 0, "rechecked": 0}
    for item in rows:
        operation_id = item["billing_operation_id"]
        operation_state: str | None = None
        if operation_id is not None:
            found = conn.execute(
                "SELECT state FROM billing_operations WHERE id = %s", (str(operation_id),)
            ).fetchone()
            operation_state = None if found is None else str(found["state"])
        if operation_state is not None and operation_state != "PENDING":
            conn.execute(
                """
                UPDATE billing_reconciliation_items
                SET state = 'RESOLVED', resolution = %s, resolved_by = 'auto',
                    resolved_at = now(), updated_at = now()
                WHERE id = %s
                """,
                (f"AUTO_TERMINAL_{operation_state}", str(item["id"])),
            )
            stats["resolved"] += 1
            continue
        reason = str(item["reason"])
        settled = False
        # 正常入队路径 billing_operation_id 恒非空；外部插行的空值只重查不代答。
        if (
            operation_id is not None
            and str(item["state"]) == "OPEN"
            and reason not in _NEVER_AUTO_FINALIZE
        ):
            # 证据门禁在 finalize 内部：缺证据时保持 PENDING，这里只会失败，
            # 不会在证据不全时把成功任务结算掉。
            settled = finalize_pending_operation(
                conn,
                operation_id=str(operation_id),
                service=str(item["service"]),
                source_id=str(item["source_id"]),
            )
        if settled:
            conn.execute(
                """
                UPDATE billing_reconciliation_items
                SET state = 'RESOLVED', resolution = 'AUTO_FINALIZED',
                    resolved_by = 'auto', resolved_at = now(),
                    check_count = check_count + 1, last_checked_at = now(),
                    updated_at = now()
                WHERE id = %s
                """,
                (str(item["id"]),),
            )
            stats["resolved"] += 1
            continue
        was_open = str(item["state"]) == "OPEN"
        escalated = conn.execute(
            """
            UPDATE billing_reconciliation_items
            SET check_count = check_count + 1, last_checked_at = now(),
                state = CASE WHEN state = 'OPEN'
                              AND created_at < now() - (%s * interval '1 hour')
                         THEN 'ESCALATED' ELSE state END,
                escalated_at = CASE WHEN state = 'OPEN'
                                     AND created_at < now() - (%s * interval '1 hour')
                                THEN now() ELSE escalated_at END,
                updated_at = now()
            WHERE id = %s
            RETURNING state
            """,
            (_escalate_after_hours(), _escalate_after_hours(), str(item["id"])),
        ).fetchone()
        if escalated is not None and str(escalated["state"]) == "ESCALATED":
            # 只在 OPEN → ESCALATED 的转变当次计数，重查不重复计入。
            if was_open:
                stats["escalated"] += 1
            else:
                stats["rechecked"] += 1
        else:
            stats["rechecked"] += 1
    return stats


def list_reconciliation_items(
    conn: BusinessConnection,
    *,
    state: str | None,
    service: str | None,
    limit: int,
    offset: int,
) -> dict[str, object]:
    """队列页数据：原因中文释义与下一步指引在服务端给全，运营不读 JSON。"""
    filters = ["1 = 1"]
    params: list[object] = []
    if state:
        filters.append("state = %s")
        params.append(state)
    if service:
        filters.append("service = %s")
        params.append(service)
    where = " AND ".join(filters)
    rows = conn.execute(
        f"""
        SELECT id, service, source_kind, source_id, billing_operation_id,
               billing_round, owner_user_id, reason, detail_json,
               provider_receipt_id, state, check_count, last_checked_at,
               escalated_at, resolution, resolution_note, resolved_by,
               resolved_at, created_at, updated_at
        FROM billing_reconciliation_items
        WHERE {where}
        ORDER BY CASE state WHEN 'ESCALATED' THEN 0 WHEN 'OPEN' THEN 1 ELSE 2 END,
                 created_at, id
        LIMIT %s OFFSET %s
        """,  # noqa: S608 - fixed column list
        (*params, limit, offset),
    ).fetchall()
    total = conn.execute(
        f"SELECT COUNT(*) FROM billing_reconciliation_items WHERE {where}",  # noqa: S608
        tuple(params),
    ).fetchone()[0]
    items = []
    for row in rows:
        reason = str(row["reason"])
        state_value = str(row["state"])
        # jsonb 在 psycopg 侧已是 dict；文本方言兜底再解析一次。
        detail_raw = row["detail_json"]
        if isinstance(detail_raw, dict):
            detail = detail_raw
        else:
            detail = json.loads(str(detail_raw or "{}"))
        items.append(
            {
                "id": str(row["id"]),
                "service": str(row["service"]),
                "source_kind": str(row["source_kind"]),
                "source_id": str(row["source_id"]),
                "billing_operation_id": (
                    None
                    if row["billing_operation_id"] is None
                    else str(row["billing_operation_id"])
                ),
                "billing_round": int(row["billing_round"]),
                "owner_user_id": (
                    None if row["owner_user_id"] is None else str(row["owner_user_id"])
                ),
                "reason": reason,
                "reason_label": _REASON_LABELS.get(reason, reason),
                "detail": detail,
                "provider_receipt_id": (
                    None if row["provider_receipt_id"] is None else str(row["provider_receipt_id"])
                ),
                "state": state_value,
                "check_count": int(row["check_count"]),
                "last_checked_at": (
                    None if row["last_checked_at"] is None else str(row["last_checked_at"])
                ),
                "escalated_at": (None if row["escalated_at"] is None else str(row["escalated_at"])),
                "resolution": None if row["resolution"] is None else str(row["resolution"]),
                "resolution_note": (
                    None if row["resolution_note"] is None else str(row["resolution_note"])
                ),
                "resolved_by": None if row["resolved_by"] is None else str(row["resolved_by"]),
                "resolved_at": None if row["resolved_at"] is None else str(row["resolved_at"]),
                "created_at": str(row["created_at"]),
                "updated_at": str(row["updated_at"]),
                "next_step": next_step_for(state_value, reason),
            }
        )
    return {"items": items, "total": int(total)}


def resolve_reconciliation_item(
    conn: BusinessConnection,
    *,
    item_id: str,
    actor_id: str,
    note: str,
) -> dict[str, object]:
    """人工收口一条队列记录。

    只在底层计费确实已终态（或证据门禁放行本次 finalize）时才允许关闭；
    仍然 PENDING 的卡单返回 409 并指回专项对账/调账审批，不能凭一句备注
    把没对清的钱"标记解决"。
    """
    item = conn.execute(
        """
        SELECT id, service, source_id, billing_operation_id, reason, state
        FROM billing_reconciliation_items WHERE id = %s FOR UPDATE
        """,
        (item_id,),
    ).fetchone()
    if item is None:
        raise _http(404, "BILLING_RECONCILE_ITEM_NOT_FOUND", "核对记录不存在或已被清理。")
    if str(item["state"]) == "RESOLVED":
        raise _http(409, "BILLING_RECONCILE_ALREADY_RESOLVED", "该核对记录已收口，无需重复处理。")
    operation_id = item["billing_operation_id"]
    operation_state: str | None = None
    if operation_id is not None:
        found = conn.execute(
            "SELECT state FROM billing_operations WHERE id = %s FOR UPDATE",
            (str(operation_id),),
        ).fetchone()
        operation_state = None if found is None else str(found["state"])
    reason = str(item["reason"])
    finalized_now = False
    if (
        operation_state == "PENDING"
        and reason not in _NEVER_AUTO_FINALIZE
        and operation_id is not None
    ):
        finalized_now = finalize_pending_operation(
            conn,
            operation_id=str(operation_id),
            service=str(item["service"]),
            source_id=str(item["source_id"]),
        )
        if finalized_now:
            refreshed = conn.execute(
                "SELECT state FROM billing_operations WHERE id = %s", (str(operation_id),)
            ).fetchone()
            operation_state = None if refreshed is None else str(refreshed["state"])
    if operation_state is None or operation_state == "PENDING":
        raise _http(
            409,
            "BILLING_RECONCILE_NOT_CLOSEABLE",
            "计费仍未收口：请先用专项对账接口核对供应商实况或补齐计费证据；"
            "涉及真实调账须走调账审批，本队列不能直接退分。",
        )
    resolution = "MANUAL_VERIFIED" if not finalized_now else "MANUAL_FINALIZED"
    conn.execute(
        """
        UPDATE billing_reconciliation_items
        SET state = 'RESOLVED', resolution = %s, resolution_note = %s,
            resolved_by = %s, resolved_at = now(), updated_at = now()
        WHERE id = %s
        """,
        (resolution, note, actor_id, item_id),
    )
    return {
        "id": item_id,
        "state": "RESOLVED",
        "resolution": resolution,
        "billing_state": operation_state,
    }
