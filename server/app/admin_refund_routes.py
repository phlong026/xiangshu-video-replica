"""M1 退款管理最小版（ADM-08，2026-10-08 owner 批量开发指令）。

退款链路的两段事实：

- **账本侧**（已有，不重复建设）：``POST /api/control/customers/{id}/source-refunds``
  以 ``source_document_type='REFUND_APPROVAL'`` 反向调账冲减积分，仅使用可核对原充值的
  尚未消费、尚未预留余额；来源不明转人工，不建新充值单。
- **资金侧**（本模块）：ZPay 后台人工退付后，运营回到管理端登记退付执行
  结果——渠道、第三方退款单号、经办人。一笔调账至多登记一次；审计写
  ``refund.execution.mark`` 并入高敏事件清单。

列表端点把两侧拼成一行：退款调账（负向 REFUND_APPROVAL）LEFT JOIN 执行
登记，``executed=false`` 即「已冲积分、待退付」，是运营每天要清的队列。
"""

from __future__ import annotations

import json
import logging
import uuid

import psycopg
from fastapi import APIRouter, Request, Response

from app.admin_auth_routes import AdminReader, AdminWriter

# 列表分页与 /customers/{id}/adjustments 共用同一套常量与边界函数。
from app.admin_customer_routes import (
    DEFAULT_LIST_LIMIT,
    MAX_LIST_LIMIT,
    _write_with_idempotency,
)
from app.admin_write_contract import AdminWriteContract as AdminWriteRequest
from app.admin_write_contract import http_error as _http
from app.db_pg import MissingDatabaseConfigError, pg_transaction
from app.sql_pagination import page_bounds

router = APIRouter(prefix="/api/control", tags=["admin-refunds"])
logger = logging.getLogger(__name__)

REFUND_CHANNELS = ("wechat_manual", "offline_transfer", "other")
_CHANNEL_LABELS = {
    "wechat_manual": "微信商户原路退回",
    "offline_transfer": "线下转账退回",
    "other": "其他渠道",
}
MAX_PROVIDER_REF_CHARS = 128
MAX_NOTE_CHARS = 500

_UNAVAILABLE_CODE = "REFUND_SERVICE_UNAVAILABLE"
_UNAVAILABLE_MESSAGE = "退款管理需要 PostgreSQL 运行时。"


# 列表、计数和登记必须使用同一个负向事实，来源单标签本身不能证明发生过冲减。
REFUND_FACT_JOIN = """
JOIN wallet_transactions tx
  ON tx.id = 'admin_adjustment:refund:' || aa.id
 AND tx.user_id = aa.target_user_id
 AND tx.type = 'REFUND' AND tx.available_delta < 0 AND tx.reserved_delta = 0
 AND aa.recharge_order_id IS NULL
"""


# 只认新退款同事务追加的来源证据；历史记录保持可见，但不能自动获得可退付资格。
REFUND_SOURCE_EVIDENCE = """EXISTS (
 SELECT 1 FROM admin_refund_allocations proof
 JOIN billing_credit_lots source_lot ON source_lot.id=proof.credit_lot_id
 JOIN wallet_transactions source_charge ON source_charge.id=source_lot.id
 JOIN recharge_orders source_order ON source_order.id=proof.original_recharge_order_id
 WHERE proof.adjustment_id=aa.id AND proof.refund_transaction_id=tx.id
   AND proof.credits=-tx.available_delta
   AND source_charge.recharge_order_id=source_order.id
   AND source_charge.type='CHARGE' AND source_charge.reserved_delta=0
   AND source_charge.user_id=aa.target_user_id AND source_lot.user_id=aa.target_user_id
   AND source_order.user_id=aa.target_user_id AND source_order.status='PAID'
   AND source_order.package_id IS NULL AND source_order.package_snapshot_json IS NULL
   AND source_order.amount_fen>0 AND source_order.paid_at IS NOT NULL
   AND ((source_order.provider='zpay' AND length(trim(source_order.provider_trade_no))>0)
     OR (source_order.provider='wechat_native' AND length(trim(source_order.transaction_id))>0))
   AND proof.source_credits=source_order.credits
   AND proof.source_credits=source_charge.available_delta
   AND proof.source_credits=source_lot.credits
   AND proof.source_amount_fen=source_order.amount_fen
   AND proof.source_amount_fen=source_lot.amount_fen
   AND proof.lot_remaining_before-proof.lot_remaining_after=proof.credits
   AND aa.balance_before-aa.balance_after=proof.credits
)"""


class RefundExecutionRequest(AdminWriteRequest):
    """登记退付执行：channel + 第三方退款单号必填，note 可选。"""

    channel: str = ""
    provider_ref: str = ""
    note: str = ""


@router.get("/refunds")
def list_refunds(
    actor: AdminReader,
    target_username: str = "",
    executed: str = "",
    limit: int = DEFAULT_LIST_LIMIT,
    offset: int = 0,
) -> dict[str, object]:
    """退款双事实清单：负向 REFUND_APPROVAL 调账 × 退付执行登记。

    ``executed``：空 = 全部；``pending`` = 已冲积分未登记退付（运营清队列的
    默认视图）；``done`` = 双事实齐。
    """
    del actor
    bounded_limit, bounded_offset = page_bounds(limit, offset, max_limit=MAX_LIST_LIMIT)
    clauses = ["aa.source_document_type = 'REFUND_APPROVAL'"]
    params: list[object] = []
    if target_username.strip():
        clauses.append("(target_user.username ILIKE %s OR target_user.display_name ILIKE %s)")
        params.extend([f"%{target_username.strip()}%", f"%{target_username.strip()}%"])
    if executed == "pending":
        clauses.append("re.adjustment_id IS NULL")
    elif executed == "done":
        clauses.append("re.adjustment_id IS NOT NULL")
    where = f"WHERE {' AND '.join(clauses)}"
    try:
        with pg_transaction() as conn:
            rows = conn.execute(
                f"""
                SELECT aa.id, target_user.id, target_user.username,
                       target_user.display_name, aa.source_document_ref, aa.reason,
                       aa.created_at, admin_user.username,
                       COALESCE(tx.available_delta, 0),
                       re.channel, re.provider_ref, re.executed_by_user_id,
                       re.executed_at, executor.username, re.note,
                       {REFUND_SOURCE_EVIDENCE}, original_order.merchant_order_no
                FROM admin_adjustments aa
                JOIN users target_user ON target_user.id = aa.target_user_id
                JOIN users admin_user ON admin_user.id = aa.admin_user_id
                {REFUND_FACT_JOIN}
                LEFT JOIN refund_executions re ON re.adjustment_id = aa.id
                LEFT JOIN users executor ON executor.id = re.executed_by_user_id
                LEFT JOIN admin_refund_allocations allocation ON allocation.adjustment_id=aa.id
                LEFT JOIN recharge_orders original_order
                  ON original_order.id=allocation.original_recharge_order_id
                {where}
                ORDER BY aa.created_at DESC, aa.id DESC
                LIMIT %s OFFSET %s
                """,  # noqa: S608
                (*params, bounded_limit, bounded_offset),
            ).fetchall()
            total_row = conn.execute(
                f"""
                SELECT COUNT(*) FROM admin_adjustments aa
                JOIN users target_user ON target_user.id = aa.target_user_id
                {REFUND_FACT_JOIN}
                LEFT JOIN refund_executions re ON re.adjustment_id = aa.id
                {where}
                """,  # noqa: S608
                params,
            ).fetchone()
    except (RuntimeError, MissingDatabaseConfigError) as exc:
        raise _http(503, _UNAVAILABLE_CODE, _UNAVAILABLE_MESSAGE) from exc
    return {
        "items": [
            {
                "adjustment_id": str(row[0]),
                "target_user_id": str(row[1]),
                "target_username": str(row[2]),
                "target_display_name": str(row[3] or ""),
                "source_document_ref": str(row[4]),
                "reason": str(row[5]),
                "created_at": str(row[6]),
                "admin_username": str(row[7]),
                # REFUND 流水为负数（冲减），列表展示绝对值。
                "credits_deducted": abs(int(row[8])),
                "executed": row[9] is not None,
                "channel": str(row[9] or ""),
                "channel_label": _CHANNEL_LABELS.get(str(row[9] or ""), ""),
                "provider_ref": str(row[10] or ""),
                "executed_at": "" if row[12] is None else str(row[12]),
                "executed_by": str(row[13] or ""),
                "note": str(row[14] or ""),
                "source_verified": bool(row[15]),
                "original_recharge_order_no": str(row[16] or "") if row[15] else "",
            }
            for row in rows
        ],
        "total": int(total_row[0]) if total_row else 0,
        "limit": bounded_limit,
        "offset": bounded_offset,
    }


@router.post("/refunds/{adjustment_id}/execution")
def mark_refund_execution(
    adjustment_id: str,
    body: RefundExecutionRequest,
    request: Request,
    response: Response,
    actor: AdminWriter,
) -> dict[str, object]:
    """登记一笔退付执行结果（幂等键防重放；同一调账只允许登记一次）。"""

    def business(conn: psycopg.Connection, request_id: str) -> dict[str, object]:
        row = conn.execute(
            "SELECT id, target_user_id, source_document_type FROM admin_adjustments "
            "WHERE id = %s FOR UPDATE",
            (adjustment_id,),
        ).fetchone()
        if row is None:
            raise _http(404, "REFUND_ADJUSTMENT_NOT_FOUND", "退款调账记录不存在。")
        if str(row[2]) != "REFUND_APPROVAL":
            raise _http(
                422,
                "REFUND_ADJUSTMENT_TYPE_INVALID",
                "只有退款审批（REFUND_APPROVAL）调账才能登记退付执行。",
            )
        if (
            conn.execute(
                f"SELECT 1 FROM admin_adjustments aa {REFUND_FACT_JOIN} WHERE aa.id=%s",
                (adjustment_id,),
            ).fetchone()
            is None
        ):
            raise _http(
                422,
                "REFUND_NEGATIVE_LEDGER_REQUIRED",
                "该调账没有对应的负向退款流水，不能登记退付执行。",
            )
        if (
            conn.execute(
                "SELECT 1 FROM refund_executions WHERE adjustment_id = %s",
                (adjustment_id,),
            ).fetchone()
            is not None
        ):
            raise _http(
                409,
                "REFUND_EXECUTION_ALREADY_MARKED",
                "该笔退款已登记过退付执行；如需更正请走调账更正流程。",
            )
        if (
            conn.execute(
                f"SELECT 1 FROM admin_adjustments aa {REFUND_FACT_JOIN} "
                f"WHERE aa.id=%s AND {REFUND_SOURCE_EVIDENCE}",
                (adjustment_id,),
            ).fetchone()
            is None
        ):
            raise _http(
                422,
                "REFUND_SOURCE_REVIEW_REQUIRED",
                "该笔历史退款缺少有效原充值来源证据，不能新登记退付执行，请转人工核对。",
            )
        channel = body.channel.strip()
        if channel not in REFUND_CHANNELS:
            raise _http(
                422,
                "REFUND_EXECUTION_VALIDATION_FAILED",
                "退付渠道无效：必须是微信原路 / 线下转账 / 其他渠道之一。",
            )
        provider_ref = body.provider_ref.strip()
        if not provider_ref or len(provider_ref) > MAX_PROVIDER_REF_CHARS:
            raise _http(
                422,
                "REFUND_EXECUTION_VALIDATION_FAILED",
                f"第三方退款单号必填且不超过 {MAX_PROVIDER_REF_CHARS} 个字符。",
            )
        note = body.note.strip()[:MAX_NOTE_CHARS]
        conn.execute(
            """
            INSERT INTO refund_executions
                (adjustment_id, channel, provider_ref, executed_by_user_id, note)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (adjustment_id, channel, provider_ref, actor.user_id, note),
        )
        conn.execute(
            "INSERT INTO audit_logs "
            "(id, actor_user_id, action, entity_type, entity_id, metadata_json) "
            "VALUES (%s, %s, 'refund.execution.mark', 'refund_execution', %s, %s)",
            (
                str(uuid.uuid4()),
                actor.user_id,
                adjustment_id,
                json.dumps(
                    {
                        "channel": channel,
                        "channel_label": _CHANNEL_LABELS[channel],
                        "provider_ref": provider_ref,
                        "reason": body.reason.strip(),
                        "request_id": request_id,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            ),
        )
        logger.info(
            "refund execution marked: adjustment=%s channel=%s actor=%s request=%s",
            adjustment_id,
            channel,
            actor.user_id,
            request_id,
        )
        return {
            "adjustment_id": adjustment_id,
            "channel": channel,
            "channel_label": _CHANNEL_LABELS[channel],
            "provider_ref": provider_ref,
            "executed": True,
            "request_id": request_id,
        }

    return _write_with_idempotency(
        request,
        response,
        actor,
        body,
        business,
        success_status=200,
        unavailable_code=_UNAVAILABLE_CODE,
        unavailable_message=_UNAVAILABLE_MESSAGE,
    )
