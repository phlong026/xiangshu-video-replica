"""W15 — 总览仪表盘聚合端点（AdminReader 只读）。

一次性返回仪表盘所需的全部计数（避免前端拼十几个请求）：
- 今日生成数 / 成功数（Asia/Shanghai 日界，沿用 042 的 created_at_utc）；
- 在线设备（会话租约未过期的去重设备数）；
- 活跃客户（role=customer 且 is_active 的用户数）；
- 今日充值合计（PAID 订单金额，按 paid_at 的上海日界）；
- 近 7 日生成趋势（按日成功/失败）；
- 待办四项：待批准配对（PENDING 且未过期）、近 7 日失败任务、
  对账不一致合计（复用 billing-reconciliation 口径）、7 天内即将过期激活码；
- 设备槽位占用（BOUND 设备数 / 客户数 × 2）。

只读聚合，无写路径；PostgreSQL-only（客户域，027+ 同）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from app.admin_auth_routes import AdminReader
from app.db_pg import pg_transaction

router = APIRouter(prefix="/api/control", tags=["admin-dashboard"])

# 上海日界表达式：timestamptz → 上海挂钟日期文本
_SHANGHAI_DATE = "(%s AT TIME ZONE 'Asia/Shanghai')::date"


def _one(conn: Any, sql: str) -> Any:
    """单值聚合查询：fetchone 保证非空（聚合无 GROUP BY 恒返一行）。"""
    row = conn.execute(sql).fetchone()
    assert row is not None
    return row[0]


def _day_expr(column: str) -> str:
    return _SHANGHAI_DATE % f"{column}::timestamp AT TIME ZONE 'UTC'"


def _timestamptz_day_expr(column: str) -> str:
    return _SHANGHAI_DATE % column


@router.get("/dashboard/summary")
def dashboard_summary(_actor: AdminReader) -> dict[str, Any]:
    with pg_transaction() as conn:
        today_generation = conn.execute(
            f"""
            SELECT count(*) AS total,
                   count(*) FILTER (
                       WHERE status = 'SUCCEEDED'
                         AND archive_status IN ('ARCHIVED', 'DIRECT')
                   ) AS succeeded
            FROM generation_tasks
            WHERE {_day_expr("created_at_utc")} = {_timestamptz_day_expr("now()")}
            """
        ).fetchone()
        assert today_generation is not None

        trend_rows = conn.execute(
            f"""
            WITH days AS (
                SELECT generate_series(
                    (now() AT TIME ZONE 'Asia/Shanghai')::date - 6,
                    (now() AT TIME ZONE 'Asia/Shanghai')::date,
                    interval '1 day'
                )::date AS day
            ), generation_by_day AS (
                SELECT {_day_expr("created_at_utc")} AS day,
                       count(*) FILTER (
                           WHERE status = 'SUCCEEDED'
                             AND archive_status IN ('ARCHIVED', 'DIRECT')
                       ) AS succeeded,
                       count(*) FILTER (WHERE status = 'FAILED') AS failed
                FROM generation_tasks
                WHERE created_at_utc >= (
                    ((now() AT TIME ZONE 'Asia/Shanghai')::date - 6)::timestamp
                    AT TIME ZONE 'Asia/Shanghai'
                )
                GROUP BY 1
            )
            SELECT days.day,
                   COALESCE(generation_by_day.succeeded, 0),
                   COALESCE(generation_by_day.failed, 0)
            FROM days
            LEFT JOIN generation_by_day USING (day)
            ORDER BY days.day
            """
        ).fetchall()
        cost_trend_rows = conn.execute(
            """
            SELECT (occurred_at AT TIME ZONE 'Asia/Shanghai')::date AS day,
                   COALESCE(SUM(cost_fen) FILTER (WHERE status = 'ACTUAL'), 0)
            FROM operation_cost_records
            WHERE occurred_at >= (
                ((now() AT TIME ZONE 'Asia/Shanghai')::date - 6)::timestamp
                AT TIME ZONE 'Asia/Shanghai'
            )
            GROUP BY 1
            """
        ).fetchall()

        online_devices = int(
            _one(
                conn,
                """
                SELECT count(DISTINCT device_id) FROM customer_session_state
                WHERE lease_until::timestamptz > now()
                """,
            )
        )
        active_customers = int(
            _one(
                conn,
                """
                SELECT count(*) FROM users
                WHERE role = 'customer' AND is_active = 1
                """,
            )
        )
        today_recharge_fen = int(
            _one(
                conn,
                f"""
                SELECT COALESCE(SUM(amount_fen), 0) FROM recharge_orders
                WHERE status = 'PAID'
                  AND paid_at IS NOT NULL
                  AND {_day_expr("paid_at")} = {_timestamptz_day_expr("now()")}
                """,
            )
        )
        today_recharge_orders = int(
            _one(
                conn,
                f"""
                SELECT count(*) FROM recharge_orders
                WHERE status = 'PAID' AND paid_at IS NOT NULL
                  AND {_day_expr("paid_at")} = {_timestamptz_day_expr("now()")}
                """,
            )
        )
        today_cost = conn.execute(
            """
            SELECT COALESCE(SUM(cost_fen), 0),
                   COALESCE(SUM(usage_amount) FILTER (
                       WHERE subject IN ('video_generation_768p', 'video_generation_2k')
                         AND status = 'ACTUAL'
                   ), 0),
                   count(*) FILTER (WHERE status = 'UNKNOWN')
            FROM operation_cost_records
            WHERE (occurred_at AT TIME ZONE 'Asia/Shanghai')::date =
                  (now() AT TIME ZONE 'Asia/Shanghai')::date
            """
        ).fetchone()
        assert today_cost is not None
        today_revenue_fen = int(
            _one(
                conn,
                """
                WITH effective_price AS (
                    SELECT price_768p_fen, price_2k_fen
                    FROM daily_external_prices
                    WHERE price_date <= (now() AT TIME ZONE 'Asia/Shanghai')::date
                    ORDER BY price_date DESC
                    LIMIT 1
                )
                SELECT COALESCE(SUM(
                    (-wt.reserved_delta) * CASE
                        WHEN COALESCE(t.cost_rate_subject_snapshot, '') =
                             'video_generation_2k'
                        THEN p.price_2k_fen
                        ELSE p.price_768p_fen
                    END
                ), 0)
                FROM wallet_transactions wt
                JOIN generation_tasks t ON t.id = wt.task_id
                CROSS JOIN effective_price p
                WHERE wt.type = 'SETTLE'
                  AND ((wt.created_at::timestamp AT TIME ZONE 'UTC')
                       AT TIME ZONE 'Asia/Shanghai')::date =
                      (now() AT TIME ZONE 'Asia/Shanghai')::date
                """,
            )
        )

        pending_pairings = int(
            _one(
                conn,
                """
                SELECT count(*) FROM device_pairing_requests
                WHERE status = 'PENDING' AND expires_at::timestamptz > now()
                """,
            )
        )
        failed_tasks_7d = int(
            _one(
                conn,
                f"""
                SELECT
                    (SELECT count(*) FROM generation_tasks
                     WHERE status = 'FAILED'
                       AND created_at_utc >= (
                           ((now() AT TIME ZONE 'Asia/Shanghai')::date - 6)::timestamp
                           AT TIME ZONE 'Asia/Shanghai'
                       ))
                  + (SELECT count(*) FROM oral_tasks
                     WHERE status = 'FAILED'
                       AND {_day_expr("created_at")} >=
                           (now() AT TIME ZONE 'Asia/Shanghai')::date - 6)
                """,
            )
        )
        expiring_codes = int(
            _one(
                conn,
                """
                SELECT count(*) FROM activation_codes c
                JOIN activation_code_batches b ON b.id = c.batch_id
                WHERE c.status IN ('GENERATED', 'ISSUED')
                  AND b.activation_expires_at::timestamptz >= now()
                  AND b.activation_expires_at::timestamptz < now() + make_interval(days => 7)
                """,
            )
        )
        reconciliation_problems = int(
            _one(
                conn,
                """
                SELECT
                  (SELECT count(*) FROM wallets w
                     WHERE w.available_credits + w.reserved_credits <>
                           COALESCE((SELECT SUM(available_delta + reserved_delta)
                             FROM wallet_transactions wt WHERE wt.user_id = w.user_id), 0))
                + (SELECT count(*) FROM recharge_orders o
                     WHERE o.status = 'PAID' AND NOT EXISTS (
                         SELECT 1 FROM wallet_transactions wt
                         WHERE wt.recharge_order_id = o.id AND wt.type = 'CHARGE'))
                + (SELECT count(*) FROM wallet_transactions wt
                     WHERE wt.type = 'CHARGE' AND NOT EXISTS (
                         SELECT 1 FROM recharge_orders o
                         WHERE o.id = wt.recharge_order_id AND o.status = 'PAID'))
                """,
            )
        )
        bound_devices = int(
            _one(
                conn,
                "SELECT count(*) FROM customer_devices WHERE status = 'BOUND'",
            )
        )
        unconfigured_rates = int(
            _one(
                conn,
                """
                SELECT count(*) FROM (VALUES
                    ('video_generation_768p'), ('video_generation_2k'),
                    ('video_analysis_768p'), ('video_analysis_2k'),
                    ('first_frame_image'), ('character_sheet_image'), ('context_ir'),
                    ('external_price_768p'), ('external_price_2k')
                ) required(subject)
                WHERE NOT EXISTS (
                    SELECT 1 FROM operation_cost_rates rate
                    WHERE rate.subject = required.subject
                )
                """,
            )
        )

    cost_by_day = {str(row[0]): float(row[1]) for row in cost_trend_rows}
    trend = [
        {
            "day": str(row[0]),
            "succeeded": int(row[1]),
            "failed": int(row[2]),
            "cost_fen": cost_by_day.get(str(row[0]), 0.0),
        }
        for row in trend_rows
    ]
    generation_count = int(today_generation[0])
    succeeded = int(today_generation[1])
    cost_fen = float(today_cost[0])
    unknown_cost_records = int(today_cost[2])
    gross_fen = None if unknown_cost_records > 0 else today_revenue_fen - cost_fen
    return {
        "today": {
            "generation_count": generation_count,
            "succeeded": succeeded,
            "success_rate_pct": (
                None if generation_count == 0 else round(succeeded / generation_count * 100, 1)
            ),
            "output_seconds": float(today_cost[1]),
            "cost_fen": cost_fen,
            "revenue_fen": today_revenue_fen,
            "gross_fen": gross_fen,
            "margin_pct": (
                None
                if today_revenue_fen == 0 or gross_fen is None
                else round(gross_fen / today_revenue_fen * 100, 1)
            ),
            "online_devices": online_devices,
            "active_customers": active_customers,
            "recharge_fen": today_recharge_fen,
            "recharge_orders": today_recharge_orders,
        },
        "trend": trend,
        "todos": {
            "pending_pairings": pending_pairings,
            "failed_tasks_7d": failed_tasks_7d,
            "reconciliation_problems": reconciliation_problems,
            "expiring_codes_7d": expiring_codes,
            "unconfigured_rates": unconfigured_rates,
            "unknown_cost_records": unknown_cost_records,
        },
        "device_slots": {
            "bound": bound_devices,
            "total": active_customers * 2,
        },
    }
