"""M3 客户运营 v2（ADM-10）：生命周期分层与健康分（只读聚合）。

规则（口径随响应下发，前端展示同文案）：

- **生命周期**：new = 注册 14 天内；active = 30 天内有任务或消费；dormant =
  30–90 天无活动；churned = 超过 90 天无活动。停用账号不参与分层。
- **健康分**（0–100，越高越健康）：近 30 天有成功任务 +40；余额 >100 积分
  +30（够一次最低消耗）；近 7 天无失败 +20；近 30 天有充值 +10。
  低分（<40）= 需要运营介入的信号组合。

数据全部来自既有 CUSTOMER_FACTS_CTE / CUSTOMER_METRICS_JOINS 聚合，本模块
零新表零迁移。批量赠送走既有逐户调账端点（前端按幂等键串行），不在服务端
复制钱包写入逻辑。
"""

from __future__ import annotations

import psycopg
from fastapi import APIRouter

from app.admin_auth_routes import AdminReader
from app.admin_cash_evidence import CASH_ORDER_ELIGIBLE
from app.admin_customer_metrics import (
    CUSTOMER_FACTS_CTE,
    CUSTOMER_METRICS_JOINS,
    utc_text_timestamp,
)
from app.admin_customer_routes import DEFAULT_LIST_LIMIT, MAX_LIST_LIMIT
from app.admin_write_contract import http_error as _http
from app.db_pg import MissingDatabaseConfigError, pg_transaction
from app.sql_pagination import page_bounds

router = APIRouter(prefix="/api/control", tags=["admin-customer-lifecycle"])

LIFECYCLE_RULE = (
    "new = 注册 14 天内；active = 30 天内有任务或消费；"
    "dormant = 30–90 天无活动；churned = 超过 90 天无活动。停用账号不参与分层。"
)
HEALTH_RULE = (
    "健康分（0–100）：近 30 天成功任务 +40；余额 >100 积分 +30；"
    "近 7 天无失败 +20；近 30 天有充值 +10。低于 40 视为需运营介入。"
)

_STAGE_SQL = f"""
CASE
  WHEN {utc_text_timestamp("u.created_at")} >= now()-interval '14 days' THEN 'new'
  WHEN COALESCE(activity.last_at, {utc_text_timestamp("u.created_at")})
       >= now()-interval '30 days' THEN 'active'
  WHEN COALESCE(activity.last_at, {utc_text_timestamp("u.created_at")})
       >= now()-interval '90 days' THEN 'dormant'
  ELSE 'churned'
END
"""

_HEALTH_SQL = """
(CASE WHEN COALESCE(recent.succeeded_30d, 0) > 0 THEN 40 ELSE 0 END
 + CASE WHEN COALESCE(w.available_credits, 0) > 100 THEN 30 ELSE 0 END
 + CASE WHEN COALESCE(recent.failed_7d, 0) = 0 THEN 20 ELSE 0 END
 + CASE WHEN COALESCE(recharge.total_fen, 0) > 0
          AND recharge.last_at >= now()-interval '30 days' THEN 10 ELSE 0 END)
"""


@router.get("/customers/lifecycle")
def list_customer_lifecycle(
    actor: AdminReader,
    stage: str = "",
    limit: int = DEFAULT_LIST_LIMIT,
    offset: int = 0,
) -> dict[str, object]:
    """生命周期分层清单 + 分布汇总；stage 为空时返回全部分层。"""
    del actor
    bounded_limit, bounded_offset = page_bounds(limit, offset, max_limit=MAX_LIST_LIMIT)
    params: list[object] = []
    stage_filter = ""
    # rows 查询对投影列 stage 过滤需包一层子查询；分布查询在 grouped 外层 WHERE。
    if stage.strip():
        stage_filter = "WHERE stage = %s"
        params.append(stage.strip())
    row_params: list[object] = []
    row_filter = ""
    if stage.strip():
        row_filter = "WHERE stage = %s"
        row_params.append(stage.strip())
    try:
        with pg_transaction(isolation="REPEATABLE READ") as conn:
            rows = conn.execute(
                CUSTOMER_FACTS_CTE
                + f"""
                SELECT * FROM (
                  SELECT u.id, u.username, COALESCE(u.display_name, ''),
                         {utc_text_timestamp("u.created_at")} AS created_at,
                         COALESCE(activity.last_at, {utc_text_timestamp("u.created_at")}),
                         COALESCE(w.available_credits, 0),
                         COALESCE(recharge.total_fen, 0),
                         COALESCE(recent.succeeded_30d, 0),
                         COALESCE(recent.failed_30d, 0),
                         {_STAGE_SQL} AS stage,
                         {_HEALTH_SQL} AS health_score
                  FROM users u
                  LEFT JOIN wallets w ON w.user_id = u.id
                  LEFT JOIN LATERAL (
                    SELECT COALESCE(SUM(ro.amount_fen), 0) AS total_fen,
                           MAX({utc_text_timestamp("ro.paid_at")}) AS last_at
                    FROM recharge_orders ro
                    WHERE ro.user_id = u.id AND ro.status = 'PAID'
                      AND {CASH_ORDER_ELIGIBLE}
                  ) recharge ON TRUE
                  {CUSTOMER_METRICS_JOINS}
                  WHERE u.role = 'customer' AND u.is_active = 1
                ) staged
                {row_filter}
                ORDER BY health_score ASC, created_at DESC
                LIMIT %s OFFSET %s
                """,
                (*row_params, bounded_limit, bounded_offset),
            ).fetchall()
            # T30：真实总数。此前响应没有 total，前端只能拿「本页行数+offset」
            # 伪造——满一页时永远显示 20 条、无法翻页。
            total = conn.execute(
                CUSTOMER_FACTS_CTE
                + f"""
                SELECT count(*) FROM (
                  SELECT u.id, {_STAGE_SQL} AS stage
                  FROM users u
                  LEFT JOIN wallets w ON w.user_id = u.id
                  LEFT JOIN LATERAL (
                    SELECT COALESCE(SUM(ro.amount_fen), 0) AS total_fen,
                           MAX({utc_text_timestamp("ro.paid_at")}) AS last_at
                    FROM recharge_orders ro
                    WHERE ro.user_id = u.id AND ro.status = 'PAID'
                      AND {CASH_ORDER_ELIGIBLE}
                  ) recharge ON TRUE
                  {CUSTOMER_METRICS_JOINS}
                  WHERE u.role = 'customer' AND u.is_active = 1
                ) staged
                {row_filter}
                """,
                tuple(row_params),
            ).fetchone()
            total_count = int(total[0]) if total is not None else 0
            distribution = conn.execute(
                CUSTOMER_FACTS_CTE
                + f"""
                SELECT stage, count(*), min(score), max(score) FROM (
                  SELECT {_STAGE_SQL} AS stage, {_HEALTH_SQL} AS score
                  FROM users u
                  LEFT JOIN wallets w ON w.user_id = u.id
                  LEFT JOIN LATERAL (
                    SELECT COALESCE(SUM(ro.amount_fen), 0) AS total_fen,
                           MAX({utc_text_timestamp("ro.paid_at")}) AS last_at
                    FROM recharge_orders ro
                    WHERE ro.user_id = u.id AND ro.status = 'PAID'
                      AND {CASH_ORDER_ELIGIBLE}
                  ) recharge ON TRUE
                  {CUSTOMER_METRICS_JOINS}
                  WHERE u.role = 'customer' AND u.is_active = 1
                ) grouped {stage_filter} GROUP BY stage ORDER BY stage
                """,
                tuple(params),
            ).fetchall()
    except (RuntimeError, MissingDatabaseConfigError) as exc:
        raise _http(
            503, "CUSTOMER_LIFECYCLE_UNAVAILABLE", "客户运营分析需要 PostgreSQL 运行时。"
        ) from exc
    return {
        "items": _lifecycle_rows(rows),
        "total": total_count,
        "distribution": [
            {"stage": str(r[0]), "count": int(r[1]), "min_score": int(r[2]), "max_score": int(r[3])}
            for r in distribution
        ],
        "lifecycleRule": LIFECYCLE_RULE,
        "healthRule": HEALTH_RULE,
        "limit": bounded_limit,
        "offset": bounded_offset,
    }


def _lifecycle_rows(rows: list[tuple[object, ...]]) -> list[dict[str, object]]:
    items: list[dict[str, object]] = []
    for row in rows:
        items.append(
            {
                "user_id": str(row[0]),
                "username": str(row[1]),
                "display_name": str(row[2]),
                "created_at": row[3].isoformat() if hasattr(row[3], "isoformat") else str(row[3]),
                "last_active_at": row[4].isoformat()
                if hasattr(row[4], "isoformat")
                else str(row[4]),
                "available_credits": int(row[5]),  # type: ignore[call-overload]
                "recharge_total_fen": int(row[6]),  # type: ignore[call-overload]
                "succeeded_30d": int(row[7]),  # type: ignore[call-overload]
                "failed_30d": int(row[8]),  # type: ignore[call-overload]
                "stage": str(row[9]),
                "health_score": int(row[10]),  # type: ignore[call-overload]
            }
        )
    return items


@router.get("/customers/retention")
def read_customer_retention(actor: AdminReader, months: int = 6) -> dict[str, object]:
    """ADM-23 客户队列留存：按激活（或注册）月分桶 × 近 N 月活跃矩阵。

    活跃沿用生命周期同源事实（钱包流水/任务/会话事件）。留存口径：
    每个队列在第 m 月的活跃客户数 / 队列总客户数；只统计在役客户。
    """
    del actor
    bounded_months = max(1, min(int(months), 12))
    try:
        with pg_transaction() as conn:
            rows = conn.execute(
                CUSTOMER_FACTS_CTE
                # CUSTOMER_FACTS_CTE 已以 WITH 开头：链式 CTE 用逗号续接。
                # CUSTOMER_FACTS_CTE 以右括号收尾、无尾逗号：链式 CTE 必须
                # 自带逗号（缺失会让整段 SQL 在 PG 报 syntax error）。
                + f""",
                cohorts AS (
                  SELECT u.id,
                         date_trunc('month',
                           COALESCE(
                             {utc_text_timestamp("aca.activated_at")},
                             {utc_text_timestamp("u.created_at")}
                           )) AS cohort_month
                  FROM users u
                  -- T32 修复：此前引用了从未 join 的别名 aca（UndefinedTable）；
                  -- 修复 join 后又暴露第二层问题——created_at/activated_at 都是
                  -- text 列，date_trunc 前必须转 timestamptz，否则 UndefinedFunction。
                  LEFT JOIN activation_code_activations aca ON aca.user_id = u.id
                  LEFT JOIN wallets w ON w.user_id = u.id
                  {CUSTOMER_METRICS_JOINS}
                  WHERE u.role = 'customer' AND u.is_active = 1
                ), activity_months AS (
                  -- customer_activity_facts 的 created_at 在 CTE 内已是
                  -- timestamptz，不能再走 text→timestamptz 转换（运算符不存在）。
                  SELECT user_id,
                         date_trunc('month', created_at) AS m
                  FROM customer_activity_facts
                  WHERE created_at >= date_trunc('month', now()) - make_interval(months => %s)
                )
                SELECT c.cohort_month, am.m AS active_month, count(DISTINCT c.id)
                FROM cohorts c
                JOIN activity_months am ON am.user_id = c.id
                WHERE am.m >= c.cohort_month
                GROUP BY 1, 2 ORDER BY 1, 2
                """,
                (bounded_months,),
            ).fetchall()
            cohort_sizes = conn.execute(
                CUSTOMER_FACTS_CTE
                + f"""
                SELECT date_trunc('month',
                         COALESCE(
                           {utc_text_timestamp("aca.activated_at")},
                           {utc_text_timestamp("u.created_at")}
                         )),
                       count(*)
                FROM users u
                LEFT JOIN activation_code_activations aca ON aca.user_id = u.id
                LEFT JOIN wallets w ON w.user_id = u.id
                {CUSTOMER_METRICS_JOINS}
                WHERE u.role = 'customer' AND u.is_active = 1
                GROUP BY 1 ORDER BY 1
                """,
            ).fetchall()
    except (RuntimeError, MissingDatabaseConfigError) as exc:
        raise _http(
            503, "CUSTOMER_RETENTION_UNAVAILABLE", "客户留存分析需要 PostgreSQL 运行时。"
        ) from exc
    except psycopg.ProgrammingError as exc:
        # T32：SQL 结构错误必须显式 503（带错误码），不能裸 500 被前端静默吞掉。
        raise _http(
            503, "CUSTOMER_RETENTION_UNAVAILABLE", "留存查询暂不可用，请稍后重试。"
        ) from exc
    sizes = {str(r[0])[:7]: int(r[1]) for r in cohort_sizes}
    active: dict[str, dict[str, int]] = {}
    for row in rows:
        cohort = str(row[0])[:7]
        month = str(row[1])[:7]
        active.setdefault(cohort, {})[month] = int(row[2])
    return {
        "months": bounded_months,
        "cohorts": [
            {
                "cohort": cohort,
                "size": size,
                "activeByMonth": active.get(cohort, {}),
            }
            for cohort, size in sorted(sizes.items(), reverse=True)[:12]
        ],
        "rule": (
            "队列=激活（或注册）所在自然月；活跃=当月有钱包流水、任务或会话事件"
            "（与生命周期分层同源）。仅统计在役客户。"
        ),
    }
