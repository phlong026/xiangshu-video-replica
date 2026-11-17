"""拆解解析成功率观测（只读聚合，零迁移）。

LLM 结构化输出的失败有三类：上游报错（HTTP 非 200）、超时、以及最隐蔽的
「HTTP 200 但内容格式坏」（解析层拒绝）。前两类在 external_call_logs 的
outcome 列可见；第三类散落在 analysis_task_attempts 的失败原因文案里。
本端点把两类事实按「模型 × 结果」聚合成一个口径，供换模型/网关行为变化
时对照解析成功率（EB1CA38A：gemini-3.8 的裸换行注入就是换版后引入的）。

全部为 COUNT 聚合读；数据库不可用时 503 fail-closed。
"""

from __future__ import annotations

from fastapi import APIRouter

from app.admin_auth_routes import AdminReader
from app.admin_write_contract import http_error as _http
from app.db_pg import MissingDatabaseConfigError, pg_transaction

router = APIRouter(prefix="/api/control", tags=["admin-analysis-parse-stats"])

_LOOKBACK_DAYS = 30
# 「格式无效」两类文案（EB1CA38A 修复前后的措辞都覆盖；字段级匹配用前缀，
# 文案尾部随自动修复状态变化）。
_INVALID_FORMAT_PREFIX = "拆解结果格式无效"


@router.get("/analysis-parse-stats")
def read_analysis_parse_stats(actor: AdminReader) -> dict[str, object]:
    del actor
    try:
        with pg_transaction() as conn:
            calls = conn.execute(
                """
                SELECT model, outcome, count(*)
                FROM external_call_logs
                WHERE task_type IN ('ANALYSIS', 'REPLICA_PROMPT')
                  AND created_at > to_char(now() - interval '30 days', 'YYYY-MM-DD HH24:MI:SS')
                GROUP BY model, outcome
                ORDER BY count(*) DESC
                """
            ).fetchall()
            invalid_attempts = conn.execute(
                """
                SELECT count(*)
                FROM analysis_task_attempts
                WHERE error_message_redacted LIKE %s
                  AND created_at > to_char(now() - interval '30 days', 'YYYY-MM-DD HH24:MI:SS')
                """,
                (_INVALID_FORMAT_PREFIX + "%",),
            ).fetchone()
            total_attempts = conn.execute(
                """
                SELECT count(*)
                FROM analysis_task_attempts
                WHERE created_at > to_char(now() - interval '30 days', 'YYYY-MM-DD HH24:MI:SS')
                """
            ).fetchone()
    except MissingDatabaseConfigError as exc:
        raise _http(
            503,
            "ANALYSIS_PARSE_STATS_UNAVAILABLE",
            "拆解解析统计暂不可用（数据库配置缺失）。",
        ) from exc
    call_rows = [
        {"model": row[0], "outcome": row[1], "count": int(row[2])} for row in calls
    ]
    total_calls = sum(row["count"] for row in call_rows)
    ok_calls = sum(row["count"] for row in call_rows if row["outcome"] == "SUCCEEDED")
    return {
        "lookback_days": _LOOKBACK_DAYS,
        "upstream_calls": {
            "total": total_calls,
            "succeeded": ok_calls,
            "rows": call_rows,
        },
        "invalid_format_attempts": int(invalid_attempts[0]) if invalid_attempts else 0,
        "total_attempts": int(total_attempts[0]) if total_attempts else 0,
    }
