"""管理端计费报表导出（CSV）。

对齐项目管理端惯例：AdminReader 鉴权（app.admin_auth_routes）、/api/control 前缀、
pg_transaction + BusinessConnection。按 CLAUDE.md「无明确请求不引入新依赖」，
xlsx 依赖的 openpyxl 不在 server 依赖组内，故仅支持 CSV（gzip 压缩）。
数据模型无 customer email（见 admin_customer_routes.py §T33），导出以 username 标识用户。
"""

from __future__ import annotations

import csv
import gzip
import io
import json
import logging
from datetime import UTC, date, datetime
from typing import Literal

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.admin_auth_routes import AdminExporter, AdminReader
from app.admin_customer_metrics import utc_text_timestamp
from app.admin_export_guard import guard_admin_export
from app.billing_reports import UNMETERED_DELIVERED_CHARGE, date_bounds
from app.csv_export import spreadsheet_safe_cell
from app.db_pg import pg_transaction
from app.db_portable import BusinessConnection

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/control", tags=["admin-export"])

# 92 天：一个自然季度最长 92 天（7–9 月 / 10–12 月），账务明细要能按季导出。
_MAX_EXPORT_DAYS = 92
_SUPPORTED_FORMATS = ("csv",)


class ExportRequest(BaseModel):
    """报表导出参数。"""

    format: str = Field(default="csv", description="输出格式（当前仅支持 csv）")
    start_date: date = Field(..., description="起始日期（含）")
    end_date: date = Field(..., description="结束日期（含）")
    service_types: list[str] = Field(default=["all"], description="按服务过滤")
    user_ids: list[str] | None = Field(None, description="按用户过滤（None=全部）")
    min_revenue_fen: int | None = Field(None, ge=0, description="最小费用过滤")
    max_revenue_fen: int | None = Field(None, ge=0, description="最大费用过滤")


def validate_request(request: ExportRequest) -> tuple[bool, str]:
    """校验导出参数；返回 (是否合法, 不合法原因)。"""
    if request.start_date > request.end_date:
        return False, "start_date must be before or equal to end_date"
    if (request.end_date - request.start_date).days >= _MAX_EXPORT_DAYS:
        return False, "Date range cannot exceed 92 days"
    if request.format not in _SUPPORTED_FORMATS:
        return False, f"Unsupported format: {request.format}"
    return True, ""


def generate_csv_content(conn: BusinessConnection, request: ExportRequest) -> bytes:
    """按北京时间导出流水，实际成本和收入只挂在一条结算流水上。"""
    # 含 end_date 当天：右开区间到次日零点。账务日按北京时间自然日切分，与看板、
    # 月报同一日界；wallet_transactions.created_at 是文本时间戳，必须先转 timestamptz 再比较
    # （此前直接拿日期去比文本列，在真实 PG 上报 operator does not exist）。
    start_inclusive, end_exclusive = date_bounds(request.start_date, request.end_date)
    service_filter = [] if request.service_types == ["all"] else request.service_types

    rows = conn.execute(
        f"""
        WITH costs AS (
          SELECT operation_id,count(*) AS calls,
                 count(*) FILTER(WHERE effective_cost_fen IS NULL) AS unknown_count,
                 sum(effective_cost_fen) AS confirmed_cost,
                 CASE WHEN count(*)=1 AND count(evidence_reference)=0 THEN min(unit_cost_fen)
                 END AS unit_cost_fen
          FROM billing_effective_attempts GROUP BY operation_id
        )
        SELECT
            wt.id,
            wt.user_id,
            u.username AS username,
            DATE(timezone('Asia/Shanghai', {utc_text_timestamp("wt.created_at")})) AS billing_date,
            o.service AS service_type,
            wt.available_delta AS credits_delta,
            ABS(wt.available_delta) AS credits_used,
            CASE WHEN cost.total IS NOT NULL THEN costs.unit_cost_fen END AS unit_cost_fen,
            cost.total AS cost_fen,
            CASE WHEN wt.id=posted.id THEN o.effective_revenue_fen END AS revenue_fen,
            COALESCE(wt.id=posted.id,false) AS is_posting,
            wt.billing_round
        FROM wallet_transactions wt
        JOIN billing_effective_operations o ON wt.billing_operation_id=o.id
        LEFT JOIN costs ON costs.operation_id=o.id
        LEFT JOIN LATERAL (
            SELECT id FROM wallet_transactions
            WHERE billing_operation_id=o.id AND type IN ('SETTLE','RELEASE')
            ORDER BY (type='SETTLE') DESC, created_at,id LIMIT 1
        ) posted ON true
        CROSS JOIN LATERAL (SELECT CASE
          WHEN wt.id=posted.id AND o.state<>'PENDING' AND COALESCE(costs.unknown_count,0)=0
            AND NOT {UNMETERED_DELIVERED_CHARGE.format(calls="costs.calls")}
          THEN COALESCE(costs.confirmed_cost,0) END AS total) cost
        LEFT JOIN users u ON wt.user_id = u.id
        WHERE {utc_text_timestamp("wt.created_at")} >= %s
          AND {utc_text_timestamp("wt.created_at")} < %s
          AND (%s::text[] IS NULL OR o.service = ANY(%s::text[]))
          AND (%s::text[] IS NULL OR wt.user_id = ANY(%s::text[]))
          AND (%s::numeric IS NULL OR cost.total >= %s::numeric)
          AND (%s::numeric IS NULL OR cost.total <= %s::numeric)
        ORDER BY {utc_text_timestamp("wt.created_at")} DESC, wt.id
        """,
        (
            start_inclusive,
            end_exclusive,
            service_filter or None,
            service_filter or None,
            request.user_ids,
            request.user_ids,
            request.min_revenue_fen,
            request.min_revenue_fen,
            request.max_revenue_fen,
            request.max_revenue_fen,
        ),
    ).fetchall()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "Transaction ID",
            "User ID",
            "Username",
            "Billing Date",
            "Service Type",
            "Credits Delta",
            "Credits Used",
            "Unit Cost (Fen)",
            "Total Cost (Fen)",
            "Billing Round",
            "Confirmed Revenue (Fen)",
            "Cost Status",
        ]
    )
    for row in rows:
        # username 是客户可控输入，与其它导出 lane 一致走六前缀防护，防公式注入。
        writer.writerow(
            [
                spreadsheet_safe_cell(cell)
                for cell in (
                    str(row["id"]),
                    str(row["user_id"] or "N/A"),
                    row["username"] or "N/A",
                    row["billing_date"].isoformat(),
                    row["service_type"],
                    int(row["credits_delta"]),
                    int(row["credits_used"]),
                    row["unit_cost_fen"],
                    row["cost_fen"],
                    int(row["billing_round"]),
                    row["revenue_fen"],
                    ("已确认" if row["cost_fen"] is not None else "待核对")
                    if row["is_posting"]
                    else "",
                )
            ]
        )
    return gzip.compress(output.getvalue().encode("utf-8"))


# 导出审计里的 entity_id → 面向运营的中文名；未登记的类型原样显示，便于发现新导出。
_EXPORT_KIND_LABELS = {
    "billing_operations": "成本核对明细",
    "billing_reviews": "凭证核对清单",
    "billing_statistics": "账务明细",
    "summary_business": "经营报表",
    "summary_funds": "资金报表",
    "wallet_transactions": "积分流水",
    "recharge_orders": "充值订单",
    "audit_log": "管理操作日志",
    "generation_records": "生成记录",
    "customers": "客户清单",
    "adjustments": "人工调整",
}
_RECENT_EXPORT_LIMIT = 10


@router.get("/reports/exports/recent")
def recent_exports(actor: AdminReader) -> dict[str, object]:
    """最近 10 次导出（谁、何时、什么范围），来自 ``control.export`` 审计。

    报表中心展示它，导出不再是「点完就没人知道」；只给筛选范围，不含导出内容。
    """
    del actor
    with pg_transaction() as raw:
        rows = raw.execute(
            "SELECT al.created_at, COALESCE(NULLIF(u.display_name, ''), u.username, "
            "al.actor_user_id), al.entity_id, al.metadata_json "
            "FROM audit_logs al LEFT JOIN users u ON u.id = al.actor_user_id "
            "WHERE al.action = 'control.export' ORDER BY al.created_at DESC, al.id DESC "
            "LIMIT %s",
            (_RECENT_EXPORT_LIMIT,),
        ).fetchall()
    items: list[dict[str, object]] = []
    for created_at, who, kind, metadata in rows:
        parsed = json.loads(metadata) if isinstance(metadata, str) else (metadata or {})
        filters = parsed.get("filters") if isinstance(parsed, dict) else None
        items.append(
            {
                "at": created_at.isoformat()
                if hasattr(created_at, "isoformat")
                else str(created_at),
                "actor": str(who or ""),
                "kind": str(kind),
                "label": _EXPORT_KIND_LABELS.get(str(kind), str(kind)),
                "filters": {
                    str(k): str(v)
                    for k, v in filters.items()
                    if k != "subject_id" and v not in (None, "")
                }
                if isinstance(filters, dict)
                else {},
            }
        )
    return {"items": items}


@router.post("/reports/export")
def export_statistics(request: ExportRequest, actor: AdminExporter) -> Response:
    """导出计费统计报表（CSV，gzip 压缩）。

    只读查询走 REPEATABLE READ 快照，导出行集与账务时点一致。
    """
    valid, message = validate_request(request)
    if not valid:
        raise HTTPException(status_code=400, detail=message)
    # 导出对审计员开放（T19）：每次导出留审计并过限流。
    guard_admin_export(
        actor,
        kind="billing_statistics",
        filters={"start": request.start_date, "end": request.end_date, "format": request.format},
    )

    with pg_transaction(isolation="REPEATABLE READ") as raw:
        conn = BusinessConnection.postgres(raw)
        content = generate_csv_content(conn, request)

    timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    filename = f"billing_export_{timestamp}.csv.gz"
    logger.info(
        "billing export: actor=%s range=%s..%s format=%s",
        actor.username,
        request.start_date,
        request.end_date,
        request.format,
    )
    return Response(
        content=content,
        media_type="application/gzip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Content-Type-Options": "nosniff",
        },
    )


class SummaryExportRequest(BaseModel):
    kind: Literal["business", "funds"]
    start_date: date
    end_date: date


@router.post("/reports/summary/export")
def export_summary(request: SummaryExportRequest, actor: AdminExporter) -> Response:
    """直接复用看板事实与日界；未知金额留中文标识，避免导出变成零。"""
    from app.admin_dashboard_routes import business_overview, funds_summary

    if request.end_date < request.start_date or (request.end_date - request.start_date).days > 366:
        raise HTTPException(422, detail="报表区间应为有效日期且最多 366 天")
    guard_admin_export(
        actor,
        kind=f"summary_{request.kind}",
        filters={"start": request.start_date, "end": request.end_date},
    )
    report = (
        business_overview(request.start_date, request.end_date, actor)
        if request.kind == "business"
        else funds_summary(request.start_date, request.end_date, actor)
    )
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(["区间开始", report["start"], "区间结束", report["end"], "时区", "北京时间"])
    writer.writerow(["说明", "金额单位为分；预收余额与对账异常为导出时点数，非历史月末快照"])

    def write(label: str, value: object) -> None:
        writer.writerow(
            [
                spreadsheet_safe_cell(label),
                "待核对" if value is None else spreadsheet_safe_cell(value),
            ]
        )

    labels = {
        "recharge_fen": "充值实收（分）",
        "revenue_fen": "已知确认收入（分）",
        "cost_fen": "已知成本（分）",
        "gross_fen": "毛利（分）",
        "margin_pct": "毛利率（%）",
        "paying_customers": "付费客户数",
        "new_paying_customers": "新增付费客户",
        "unknown_cost_count": "成本待核对数",
        "unknown_revenue_count": "收入待核对数",
        "pending_count": "待结算数",
        "legacy_cost_count": "历史未关联成本记录数",
        "legacy_settlement_count": "历史未关联结算记录数",
        "prepaid_credits": "当前预收积分余额",
        "prepaid_fen": "当前预收折合金额（分）",
        "orders": "实付订单数",
        "unverified_manual_orders": "历史人工订单待核对数（未计实收）",
        "unverified_manual_fen": "历史人工订单记录金额（待核对，分）",
        "offline_fen": "线下收款（分）",
        "grant_credits": "赠送积分",
        "refund_credits": "退款扣减积分",
        "refund_fen": "退款折合金额（分）",
        "net_fen": "净收入（分）",
        "reconciliation_problems": "当前对账异常数",
    }
    metrics = report["metrics"] if request.kind == "business" else report
    for field, label in labels.items():
        if field in metrics:
            write(label, metrics[field])
    if request.kind == "business":
        write(
            "客单价（分）",
            (
                metrics["recharge_fen"] / metrics["paying_customers"]
                if metrics["paying_customers"]
                else None
            ),
        )
        writer.writerow(["按日趋势", "已知确认收入（分）", "已知成本（分）", "毛利率（%）"])
        for item in report["daily"]:
            writer.writerow(
                [
                    item["day"],
                    item["known_revenue_fen"],
                    item["known_cost_fen"],
                    "待核对" if item["margin_pct"] is None else item["margin_pct"],
                ]
            )
        writer.writerow(["业务构成", "已知确认收入（分）", "已知成本（分）", "待核对成本数"])
        for item in report["modules"]:
            writer.writerow(
                [
                    spreadsheet_safe_cell(item["label"]),
                    item["revenue_fen"],
                    item["cost_fen"],
                    item["unknown_cost_count"],
                ]
            )
        writer.writerow(["客户编号", "客户名称", "用户名", "确认收入（分）"])
        for item in report["top_customers"]:
            writer.writerow(
                [
                    spreadsheet_safe_cell(item[k])
                    for k in ("user_id", "display_name", "username", "revenue_fen")
                ]
            )
    else:
        writer.writerow(["支付方式", "实付订单数", "金额（分）"])
        names = {
            "alipay": "支付宝",
            "wxpay": "微信",
            "offline": "线下转账",
            "unknown": "支付方式未知",
        }
        for item in report["by_method"]:
            writer.writerow([names[item["method"]], item["orders"], item["amount_fen"]])
    logger.info(
        "summary export: actor=%s kind=%s range=%s..%s",
        actor.username,
        request.kind,
        request.start_date,
        request.end_date,
    )
    filename = f"{request.kind}_{request.start_date}_{request.end_date}.csv"
    return Response(
        output.getvalue().encode("utf-8-sig"),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-store",
        },
    )
