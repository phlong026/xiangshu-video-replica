"""卡单积分核对队列的管理端入口（T40）。

GET 出队给运营看：等待时长用 created_at 原文交前端换算，原因释义与下一步
指引由服务端给全。POST /scan 供管理员即时触发一轮扫描（worker 每 30 秒
节流跑一遍，这里只是不等下一个周期）。POST /resolve 只在底层计费确实终态
时收口；仍然 PENDING 的卡单会被 409 指回专项对账，防止一句备注掩盖没对清
的钱。
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from app.admin_auth_routes import AdminReader, AdminWriter, admin_to_current_user
from app.billing_reconciliation import (
    enqueue_stuck_billing_operations,
    list_reconciliation_items,
    process_reconciliation_items,
    resolve_reconciliation_item,
)
from app.db_pg import pg_transaction
from app.db_portable import BusinessConnection
from app.permissions import write_audit

router = APIRouter(prefix="/api/control/billing-reconciliation", tags=["admin-billing"])


class ReconciliationResolveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

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
    request: ReconciliationResolveRequest,
    actor: AdminWriter,
) -> dict[str, object]:
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        result = resolve_reconciliation_item(
            conn,
            item_id=item_id,
            actor_id=actor.user_id,
            note=request.note,
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
            },
            commit=False,
        )
    return result
