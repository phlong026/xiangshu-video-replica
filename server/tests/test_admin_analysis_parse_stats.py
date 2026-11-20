"""解析统计口径：空壳必须与「格式无效」分开计数（2026-11-20 F82C66E9）。

背景：空壳是「HTTP 200 + 合法 JSON + 内容为空」，靠「只修格式」的重问救不回来；
把它并进 invalid_format 会让「再次换回只约束信封的 schema」这类回归在监控里
完全看不见。本文件锁定聚合口径与请求参数。
"""

from __future__ import annotations

from typing import Any

from app.admin_analysis_parse_stats_routes import summarize_parse_stats


class _Result:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None


class _FakeConn:
    def __init__(self) -> None:
        self.queries: list[tuple[str, Any]] = []

    def execute(self, sql: str, params: Any = None) -> _Result:
        self.queries.append((sql, params))
        if "GROUP BY model, outcome" in sql:
            return _Result(
                [
                    ("gemini-3.8-flash", "SUCCEEDED", 10),
                    ("gemini-3.8-flash", "PROVIDER_ERROR", 2),
                ]
            )
        if "error_message_redacted LIKE" in sql:
            return _Result([(3,)])
        if "upstream_diagnostic_json->>'reason'" in sql:
            return _Result([(4,)])
        if "FROM analysis_task_attempts" in sql:
            return _Result([(20,)])
        raise AssertionError(f"unexpected query: {sql}")


def test_empty_results_are_counted_separately_from_invalid_format() -> None:
    conn = _FakeConn()

    stats = summarize_parse_stats(conn)

    assert stats["invalid_format_attempts"] == 3
    assert stats["empty_result_attempts"] == 4
    assert stats["total_attempts"] == 20
    assert stats["empty_result_rate"] == 0.2
    upstream = stats["upstream_calls"]
    assert isinstance(upstream, dict)
    assert upstream["total"] == 12
    assert upstream["succeeded"] == 10


def test_empty_result_query_covers_both_reasons() -> None:
    """首调即空与修复后仍空必须一起统计，否则失败会漏一半。"""
    conn = _FakeConn()

    summarize_parse_stats(conn)

    empty_params = [
        params for sql, params in conn.queries if "upstream_diagnostic_json->>'reason'" in sql
    ]
    assert empty_params == [
        (["empty_analysis", "empty_analysis_after_repair"],),
    ]


def test_zero_attempts_does_not_divide_by_zero() -> None:
    class _EmptyConn(_FakeConn):
        def execute(self, sql: str, params: Any = None) -> _Result:
            self.queries.append((sql, params))
            if "GROUP BY model, outcome" in sql:
                return _Result([])
            return _Result([(0,)])

    stats = summarize_parse_stats(_EmptyConn())

    assert stats["total_attempts"] == 0
    assert stats["empty_result_rate"] == 0.0
