"""拆解解析成功率观测（只读聚合，零迁移）。

LLM 结构化输出的失败有四类：上游报错（HTTP 非 200）、超时、「HTTP 200 但
内容格式坏」（解析层拒绝）、以及「HTTP 200、格式合法、内容为空」。前三类在
external_call_logs 的 outcome 列或失败原因文案里可见；第四类是 2026-11-20
（F82C66E9）才被单独归类的空壳，落在 analysis_task_attempts 的
``upstream_diagnostic_json.reason`` 里。

本端点把这些事实按「模型 × 结果」聚合成一个口径，供换模型/网关行为变化时
对照解析成功率（EB1CA38A：gemini-3.8 的裸换行注入就是换版后引入的；
F82C66E9：json_schema 信封约束导致 analysis 输出空对象就是换格式后引入的）。

全部为 COUNT 聚合读；数据库不可用时 503 fail-closed。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from app.admin_auth_routes import AdminReader
from app.admin_write_contract import http_error as _http
from app.db_pg import MissingDatabaseConfigError, pg_transaction

router = APIRouter(prefix="/api/control", tags=["admin-analysis-parse-stats"])

_LOOKBACK_DAYS = 30
# 「格式无效」两类文案（EB1CA38A 修复前后的措辞都覆盖；字段级匹配用前缀，
# 文案尾部随自动修复状态变化）。
_INVALID_FORMAT_PREFIX = "拆解结果格式无效"
# 空壳的两种归类：首调即空、修复后仍空（见 app.analysis.EmptyAnalysisPayload）。
_EMPTY_RESULT_REASONS = ("empty_analysis", "empty_analysis_after_repair")
_LOOKBACK_START = "to_char(now() - interval '30 days', 'YYYY-MM-DD HH24:MI:SS')"


def summarize_parse_stats(conn: Any) -> dict[str, object]:
    """把一次窗口内的解析事实聚合成对外口径（纯读，便于单测替换连接）。"""
    calls = conn.execute(
        f"""
        SELECT model, outcome, count(*)
        FROM external_call_logs
        WHERE task_type IN ('ANALYSIS', 'REPLICA_PROMPT')
          AND created_at > {_LOOKBACK_START}
        GROUP BY model, outcome
        ORDER BY count(*) DESC
        """
    ).fetchall()
    invalid_attempts = conn.execute(
        f"""
        SELECT count(*)
        FROM analysis_task_attempts
        WHERE error_message_redacted LIKE %s
          AND created_at > {_LOOKBACK_START}
        """,
        (_INVALID_FORMAT_PREFIX + "%",),
    ).fetchone()
    empty_attempts = conn.execute(
        f"""
        SELECT count(*)
        FROM analysis_task_attempts
        WHERE upstream_diagnostic_json->>'reason' = ANY(%s)
          AND created_at > {_LOOKBACK_START}
        """,
        (list(_EMPTY_RESULT_REASONS),),
    ).fetchone()
    total_attempts = conn.execute(
        f"""
        SELECT count(*)
        FROM analysis_task_attempts
        WHERE created_at > {_LOOKBACK_START}
        """
    ).fetchone()

    call_rows = [{"model": row[0], "outcome": row[1], "count": int(row[2])} for row in calls]
    total_calls = sum(row["count"] for row in call_rows)
    ok_calls = sum(row["count"] for row in call_rows if row["outcome"] == "SUCCEEDED")
    total = int(total_attempts[0]) if total_attempts else 0
    empty = int(empty_attempts[0]) if empty_attempts else 0
    return {
        "lookback_days": _LOOKBACK_DAYS,
        "upstream_calls": {
            "total": total_calls,
            "succeeded": ok_calls,
            "rows": call_rows,
        },
        "invalid_format_attempts": int(invalid_attempts[0]) if invalid_attempts else 0,
        # 空壳单独成列：格式修复对它无效，混进 invalid_format 会掩盖真实的
        # 上游行为变化（例如再次换回只约束信封的 schema）。
        "empty_result_attempts": empty,
        "empty_result_rate": round(empty / total, 4) if total else 0.0,
        "total_attempts": total,
    }


@router.get("/analysis-parse-stats")
def read_analysis_parse_stats(actor: AdminReader) -> dict[str, object]:
    del actor
    try:
        with pg_transaction() as conn:
            return summarize_parse_stats(conn)
    except MissingDatabaseConfigError as exc:
        raise _http(
            503,
            "ANALYSIS_PARSE_STATS_UNAVAILABLE",
            "拆解解析统计暂不可用（数据库配置缺失）。",
        ) from exc
