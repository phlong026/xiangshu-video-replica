"""管理端整表导出的统一守卫：限流 + ``control.export`` 审计（整改 T19）。

决策 D2 放行审计员导出，前提是每一次导出都**必须**留下审计并过同一限流桶。
customers.csv / adjustments.csv / audit-log.csv / 控制面账本导出早已各自做了
这两件事；报表导出（reports/export、reports/summary/export）与计费明细导出
（billing/export）此前只有一行日志，既无审计也无限流——在导出对审计员开放
之前必须补齐，否则是「放权却没有留痕」。
"""

from __future__ import annotations

import hashlib
from typing import Any, cast

from fastapi import HTTPException

from app.admin_auth_routes import AdminActor
from app.auth import CurrentUser, Role
from app.db_pg import pg_transaction
from app.db_portable import BusinessConnection
from app.permissions import write_audit
from app.security_rate_limit import (
    DIMENSION_CONTROL_EXPORT_ACCOUNT,
    consume_rate_limit,
    control_export_account_limit,
    rate_limit_window_seconds,
)


def guard_admin_export(actor: AdminActor, *, kind: str, filters: dict[str, Any]) -> None:
    """扣一次导出限流额度并写 ``control.export`` 审计；额度耗尽返回 429。"""
    with pg_transaction() as raw:
        decision = consume_rate_limit(
            raw,
            dimension=DIMENSION_CONTROL_EXPORT_ACCOUNT,
            identifier=hashlib.sha256(actor.user_id.encode("utf-8")).hexdigest(),
            limit=control_export_account_limit(),
            window_seconds=rate_limit_window_seconds(),
        )
        if not decision.allowed:
            raise HTTPException(
                status_code=429,
                detail={
                    "code": "CONTROL_EXPORT_RATE_LIMITED",
                    "message": "导出过于频繁，请稍后再试。",
                },
                headers={"Retry-After": str(decision.retry_after_seconds)},
            )
        write_audit(
            BusinessConnection.postgres(raw),
            actor=CurrentUser(
                id=actor.user_id,
                username=actor.username,
                display_name=actor.display_name,
                role=cast(Role, actor.role),
            ),
            action="control.export",
            entity_type="control_report",
            entity_id=kind,
            metadata={"filters": {k: str(v) for k, v in filters.items()}},
        )
