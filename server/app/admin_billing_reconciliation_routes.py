"""卡单积分核对队列的管理端入口（T40）。

GET 出队给运营看：等待时长用 created_at 原文交前端换算，原因释义与下一步
指引由服务端给全。POST /scan 供管理员即时触发一轮扫描（worker 每 30 秒
节流跑一遍，这里只是不等下一个周期）。POST /resolve 只在底层计费确实终态
时收口；仍然 PENDING 的卡单会被 409 指回专项对账，防止一句备注掩盖没对清
的钱。
"""

from __future__ import annotations

from hashlib import sha256

import psycopg
from fastapi import APIRouter, Request, Response
from pydantic import Field

from app.admin_auth_routes import AdminReader, AdminWriter, FundsWriter, admin_to_current_user
from app.admin_write_contract import (
    IDEMPOTENCY_KEY_HEADER,
    AdminWriteContract,
    http_error,
    write_with_idempotency,
)
from app.billing_reconciliation import (
    enqueue_stuck_billing_operations,
    list_reconciliation_items,
    process_reconciliation_items,
    resolve_reconciliation_item,
)
from app.db_pg import pg_transaction
from app.db_portable import BusinessConnection
from app.generation import ReconcileGenerationTaskRequest, enqueue_generation_reconcile_operation
from app.permissions import write_audit

router = APIRouter(prefix="/api/control/billing-reconciliation", tags=["admin-billing"])


class ReconciliationResolveRequest(AdminWriteContract):
    # 与其他管理端写操作同一份契约（confirm + reason + Idempotency-Key 信封）。
    # 2026-11-15 审计发现队列页此前根本没有收口入口，而独立 extra="forbid"
    # 模型还会让走标准 adminWrite 信封的调用 422——闭环从两头断死。
    note: str = Field(min_length=1, max_length=2000)


# 路径用 /queue 而非 /items：control_routes 的旧「三类对账异常清单」已占用
# GET /billing-reconciliation/items（anomaly 必填），且在 main.py 先注册；
# 同路径的队列版会被遮蔽成运行时不可达（T36 评审发现）。
@router.get("/queue")
def list_items(
    actor: AdminReader,
    state: str | None = None,
    service: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, object]:
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        return list_reconciliation_items(
            conn, state=state, service=service, limit=limit, offset=offset
        )


@router.post("/scan")
def trigger_scan(actor: AdminWriter) -> dict[str, int]:
    # 与 worker 的对账循环共用同一把事务级 advisory lock：扫描与处理各自
    # 以不同顺序拿 billing_operations / 队列行锁，双跑可能有 AB-BA 死锁，
    # 拿不到锁说明 worker 正在对账，直接返回零计数即可。
    from app.generation_worker import _RECONCILE_LOCK_NAMESPACE

    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        if not conn.execute(
            "SELECT pg_try_advisory_xact_lock(hashtext(%s))",
            (_RECONCILE_LOCK_NAMESPACE,),
        ).fetchone()[0]:
            return {"enqueued": 0, "resolved": 0, "escalated": 0, "rechecked": 0}
        enqueued = enqueue_stuck_billing_operations(conn)
        stats = process_reconciliation_items(conn)
    return {"enqueued": enqueued, **stats}


@router.post("/items/{item_id}/resolve")
def resolve_item(
    item_id: str,
    payload: ReconciliationResolveRequest,
    request: Request,
    response: Response,
    actor: FundsWriter,
) -> dict[str, object]:
    def business(raw: psycopg.Connection, _request_id: str) -> dict[str, object]:
        conn = BusinessConnection.postgres(raw)
        result = resolve_reconciliation_item(
            conn,
            item_id=item_id,
            actor_id=actor.user_id,
            note=payload.note,
        )
        write_audit(
            conn,
            actor=admin_to_current_user(actor),
            action="billing_reconciliation.resolve",
            entity_type="billing_reconciliation_item",
            entity_id=item_id,
            metadata={
                "resolution": result.get("resolution"),
                "billing_state": result.get("billing_state"),
                "reason": payload.reason.strip(),
            },
            commit=False,
        )
        return result

    return write_with_idempotency(
        request,
        response,
        actor,
        payload,
        business,
        success_status=200,
        unavailable_code="BILLING_RECONCILE_UNAVAILABLE",
        unavailable_message="卡单对账暂不可用。",
    )


@router.post("/items/{item_id}/verify-generation", status_code=202)
def verify_generation(
    item_id: str,
    payload: AdminWriteContract,
    request: Request,
    response: Response,
    actor: FundsWriter,
) -> dict[str, object]:
    """把视频卡单交给已有对账 Worker，入队时保留账本状态。"""
    key = sha256(request.headers.get(IDEMPOTENCY_KEY_HEADER, "").strip().encode()).hexdigest()

    def business(raw: psycopg.Connection, _request_id: str) -> dict[str, object]:
        conn = BusinessConnection.postgres(raw)
        item = conn.execute(
            """
            SELECT task.id, task.batch_id, batch.project_id
            FROM billing_reconciliation_items AS item
            JOIN billing_operations AS bill ON bill.id = item.billing_operation_id
            JOIN generation_tasks AS task ON task.id = item.source_id
            JOIN generation_batches AS batch ON batch.id = task.batch_id
            WHERE item.id = %s AND item.source_kind = 'generation'
              AND item.reason = 'UNKNOWN_TIMEOUT' AND item.state <> 'RESOLVED'
              AND bill.state = 'PENDING' AND bill.service = item.service
              AND bill.source_id = task.id AND bill.billing_round = item.billing_round
              AND task.status = 'SUBMISSION_UNCERTAIN' AND task.superseded_by_task_id IS NULL
              AND task.provider_task_id IS NOT NULL AND trim(task.provider_task_id) <> ''
            FOR UPDATE OF task
            """,
            (item_id,),
        ).fetchone()
        if item is None:
            raise http_error(
                409,
                "BILLING_RECONCILE_NOT_QUERYABLE",
                "当前无法在这里查询视频结果，请刷新状态或到生成记录查看原任务。",
            )
        current_user = admin_to_current_user(actor)
        job = enqueue_generation_reconcile_operation(
            conn,
            task_id=str(item["id"]),
            batch_id=str(item["batch_id"]),
            project_id=str(item["project_id"]),
            actor=current_user,
            request=ReconcileGenerationTaskRequest(idempotency_key=key),
            commit=False,
        )
        write_audit(
            conn,
            actor=current_user,
            action="billing_reconciliation.verify_generation",
            entity_type="billing_reconciliation_item",
            entity_id=item_id,
            metadata={"operation_id": str(job["id"]), "reason": payload.reason.strip()},
            commit=False,
        )
        return {"operation_id": str(job["id"])}

    return write_with_idempotency(
        request,
        response,
        actor,
        payload,
        business,
        success_status=202,
        unavailable_code="BILLING_RECONCILE_UNAVAILABLE",
        unavailable_message="卡单对账暂不可用。",
    )
