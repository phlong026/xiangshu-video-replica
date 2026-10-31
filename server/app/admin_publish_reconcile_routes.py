"""结果未知的发布记录：运营核对队列与人工收敛（T23）。

提交已发出但结果未知（读超时/连接中断/worker 回写前崩溃）的记录停在
submission_unknown 终态——自动重投会重复发表，唯一的出口是人工到平台核对：
确认已发表凭平台作品 ID 收敛为 published；确认未发表收敛为 failed（此后
用户走既有 failed→重试路径，构成一次全新的外部提交意图）。
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from app.admin_auth_routes import AdminActor, AdminReader, AdminWriter, admin_to_current_user
from app.db_pg import pg_transaction
from app.db_portable import BusinessConnection
from app.publish_records import (
    list_submission_unknown_records,
    reconcile_submission_unknown,
    record_response,
)

router = APIRouter(prefix="/api/control/publish-records", tags=["admin-publish-reconcile"])


class PublishReconcileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outcome: Literal["published", "failed"]
    platform_item_id: str | None = Field(default=None, max_length=256)
    note: str | None = Field(default=None, max_length=500)


@router.get("/submission-unknown")
def list_unknown(actor: AdminReader, limit: int = 50, offset: int = 0) -> dict[str, object]:
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        rows = list_submission_unknown_records(conn, limit=limit, offset=offset)
        total = conn.execute(
            "SELECT count(*) FROM publish_records WHERE status = 'submission_unknown'"
        ).fetchone()[0]
    return {
        "records": [record_response(row) for row in rows],
        "total": int(total),
    }


@router.post("/{record_id}/reconcile")
def reconcile_record(
    record_id: str,
    request: PublishReconcileRequest,
    actor: AdminWriter,
) -> dict[str, object]:
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        # 审计由域函数（reconcile_submission_unknown）写，路由不重复记。
        record = reconcile_submission_unknown(
            conn,
            record_id=record_id,
            actor=admin_to_current_user(actor),
            outcome=request.outcome,
            platform_item_id=request.platform_item_id,
            note=request.note,
        )
    return {"record": record}
