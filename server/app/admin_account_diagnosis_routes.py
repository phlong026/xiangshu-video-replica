"""管理端账号诊断监控：只读列表 + 状态汇总（无写操作）.

为什么独立小路由而不塞进 control_routes：诊断报告是自有表，查询面很窄
（状态过滤 + 分页），独立文件让"这个功能的运维面"一眼可查；鉴权沿用
AdminReader（super admin 体系），绝不碰客户会话。
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict

from app.admin_auth_routes import AdminReader
from app.db_pg import pg_transaction

router = APIRouter(prefix="/api/control", tags=["admin-account-diagnosis"])

_REPORT_COLUMNS = (
    "id, user_id, wallet_owner_id, platform, sec_user_id, unique_id, input_url, "
    "status, data_level, score, tier, rating, error_code, error_message, "
    "is_free, idempotency_key, created_at, completed_at"
)


class AdminDiagnosisReportItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    user_id: str
    wallet_owner_id: str
    platform: str
    sec_user_id: str | None
    unique_id: str | None
    input_url: str
    status: str
    data_level: str | None
    score: int | None
    tier: str | None
    rating: str | None
    error_code: str | None
    error_message: str | None
    is_free: bool
    idempotency_key: str | None
    created_at: str
    completed_at: str | None


class AdminDiagnosisReportPage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[AdminDiagnosisReportItem]
    total: int
    limit: int
    offset: int
    status_counts: dict[str, int]


@router.get("/account-diagnosis/reports")
def list_account_diagnosis_reports(
    _actor: AdminReader,
    status_filter: Annotated[
        Literal["PENDING", "RUNNING", "SUCCEEDED", "FAILED"] | None,
        Query(alias="status"),
    ] = None,
    sec_user_id: Annotated[str | None, Query(max_length=120)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    conditions = ["1=1"]
    params: list[object] = []
    if status_filter is not None:
        conditions.append("status=%s")
        params.append(status_filter)
    if sec_user_id:
        conditions.append("sec_user_id=%s")
        params.append(sec_user_id)
    where = " AND ".join(conditions)

    with pg_transaction() as raw:
        total_row = raw.execute(
            f"SELECT count(*) FROM account_diagnosis_reports WHERE {where}", tuple(params)
        ).fetchone()
        rows = raw.execute(
            f"SELECT {_REPORT_COLUMNS} FROM account_diagnosis_reports WHERE {where} "
            "ORDER BY created_at DESC LIMIT %s OFFSET %s",
            (*params, limit, offset),
        ).fetchall()
        count_rows = raw.execute(
            "SELECT status, count(*) FROM account_diagnosis_reports GROUP BY status ORDER BY status"
        ).fetchall()

    items = [
        AdminDiagnosisReportItem(
            id=str(row[0]),
            user_id=str(row[1]),
            wallet_owner_id=str(row[2]),
            platform=str(row[3]),
            sec_user_id=row[4],
            unique_id=row[5],
            input_url=str(row[6]),
            status=str(row[7]),
            data_level=row[8],
            score=row[9],
            tier=row[10],
            rating=row[11],
            error_code=row[12],
            error_message=row[13],
            is_free=bool(row[14]),
            idempotency_key=row[15],
            created_at=row[16].isoformat(),
            completed_at=(row[17].isoformat() if row[17] is not None else None),
        ).model_dump()
        for row in rows
    ]
    return {
        "items": items,
        "total": int(total_row[0]) if total_row is not None else 0,
        "limit": limit,
        "offset": offset,
        "statusCounts": {str(row[0]): int(row[1]) for row in count_rows},
    }
