"""账本不变量的 PG 侧测试 oracle（T3 批 C-4 从退役的 reconcile_customer_billing 提取）。

历史工具 sqlite_to_postgres / reconcile_customer_billing 已随 SQLite lane 于
2026-11-01 删除，但「退款/调账后账本不变量仍成立」的行为覆盖要保留——本模块
以逐字等价的 SQL 承载原 validate_database_invariants 的 PostgreSQL 侧四组检查
（资产引用 / 钱包汇总 / PAID 单↔CHARGE 配对 / 计费轮次），供测试当断言 oracle。

仅测试使用：不做双方言分派、不做表级对账（那是一性割接工具的职责，
已随工具退役）。查询保持聚合/反连接形态，账本再大也不在 Python 物化。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg import sql

EXCLUDED_TABLES = frozenset({"alembic_version"})


@dataclass(frozen=True)
class InvariantIssue:
    code: str
    scope: str
    detail: str


def _row_value(row: object, key: str, index: int = 0) -> object:
    if isinstance(row, Mapping):
        return row[key]
    return row[index]  # type: ignore[index]


def _count_rows(conn: psycopg.Connection[Any], query: str | sql.Composed) -> int:
    row = conn.execute(query).fetchone()
    if row is None:
        raise RuntimeError("invariant count query returned no row")
    return int(_row_value(row, 0))


def _table_names(conn: psycopg.Connection[Any]) -> list[str]:
    rows = conn.execute(
        """
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
        ORDER BY table_name
        """
    ).fetchall()
    return [
        str(_row_value(row, "table_name"))
        for row in rows
        if str(_row_value(row, "table_name")) not in EXCLUDED_TABLES
    ]


def _pg_columns(conn: psycopg.Connection[Any], table: str) -> list[str]:
    rows = conn.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = %s
        ORDER BY ordinal_position
        """,
        (table,),
    ).fetchall()
    return [str(_row_value(row, "column_name")) for row in rows]


# ---------------------------------------------------------------------------
# 钱包汇总：wallets 必须等于其 append-only 账本的带符号聚合
# ---------------------------------------------------------------------------

_WALLET_AGGREGATE_MISMATCH_QUERY = """
SELECT COUNT(*) AS bad
FROM wallets AS w
LEFT JOIN (
    SELECT user_id,
           COALESCE(SUM(available_delta), 0) AS available_total,
           COALESCE(SUM(reserved_delta), 0) AS reserved_total
    FROM wallet_transactions
    GROUP BY user_id
) AS ledger ON ledger.user_id = w.user_id
WHERE w.available_credits != COALESCE(ledger.available_total, 0)
   OR w.reserved_credits != COALESCE(ledger.reserved_total, 0)
   OR w.available_credits < 0
   OR w.reserved_credits < 0
"""

_WALLET_MISSING_FOR_LEDGER_OWNER_QUERY = """
SELECT COUNT(*) AS bad
FROM (SELECT DISTINCT user_id FROM wallet_transactions) AS ledger
LEFT JOIN wallets AS w ON w.user_id = ledger.user_id
WHERE w.user_id IS NULL
"""


def _wallet_issues(conn: psycopg.Connection[Any], side: str) -> list[InvariantIssue]:
    tables = set(_table_names(conn))
    if not {"wallets", "wallet_transactions"}.issubset(tables):
        return []
    issues: list[InvariantIssue] = []
    mismatched = _count_rows(conn, _WALLET_AGGREGATE_MISMATCH_QUERY)
    if mismatched:
        issues.append(
            InvariantIssue(
                code="wallet_balance_mismatch",
                scope=f"{side}:wallets",
                detail=(
                    "wallets whose balances differ from the append-only "
                    f"transaction aggregates: {mismatched}"
                ),
            )
        )
    missing = _count_rows(conn, _WALLET_MISSING_FOR_LEDGER_OWNER_QUERY)
    if missing:
        issues.append(
            InvariantIssue(
                code="wallet_missing_for_ledger_owner",
                scope=f"{side}:wallets",
                detail=f"ledger owners without wallets: {missing}",
            )
        )
    return issues


# ---------------------------------------------------------------------------
# PAID 充值单 ↔ CHARGE 行：一单一笔、金额/属主一致
# ---------------------------------------------------------------------------

_PAID_ORDER_CHARGE_MISMATCH_QUERY = """
SELECT COUNT(*) AS bad
FROM recharge_orders AS o
WHERE o.status = 'PAID'
  AND (
    (
      SELECT COUNT(*) FROM wallet_transactions AS c
      WHERE c.type = 'CHARGE' AND c.recharge_order_id = o.id
    ) != 1
    OR NOT EXISTS (
      SELECT 1 FROM wallet_transactions AS c
      WHERE c.type = 'CHARGE'
        AND c.recharge_order_id = o.id
        AND c.user_id = o.user_id
        AND c.available_delta = o.credits
        AND c.reserved_delta = 0
    )
  )
"""

_UNPAID_ORDER_WITH_CHARGE_QUERY = """
SELECT COUNT(*) AS bad
FROM recharge_orders AS o
WHERE o.status != 'PAID'
  AND EXISTS (
    SELECT 1 FROM wallet_transactions AS c
    WHERE c.type = 'CHARGE' AND c.recharge_order_id = o.id
  )
"""

_CHARGE_WITHOUT_ORDER_QUERY = """
SELECT COUNT(*) AS bad
FROM wallet_transactions AS c
LEFT JOIN recharge_orders AS o ON o.id = c.recharge_order_id
WHERE c.type = 'CHARGE' AND (c.recharge_order_id IS NULL OR o.id IS NULL)
"""


def _paid_charge_issues(conn: psycopg.Connection[Any], side: str) -> list[InvariantIssue]:
    tables = set(_table_names(conn))
    if not {"recharge_orders", "wallet_transactions"}.issubset(tables):
        return []
    issues: list[InvariantIssue] = []
    paid_mismatched = _count_rows(conn, _PAID_ORDER_CHARGE_MISMATCH_QUERY)
    if paid_mismatched:
        issues.append(
            InvariantIssue(
                code="paid_order_charge_mismatch",
                scope=f"{side}:recharge_orders",
                detail=(
                    f"PAID orders without exactly one shape-matching CHARGE: {paid_mismatched}"
                ),
            )
        )
    unpaid_with_charge = _count_rows(conn, _UNPAID_ORDER_WITH_CHARGE_QUERY)
    if unpaid_with_charge:
        issues.append(
            InvariantIssue(
                code="unpaid_order_has_charge",
                scope=f"{side}:recharge_orders",
                detail=f"non-PAID orders with CHARGE rows: {unpaid_with_charge}",
            )
        )
    charge_without_order = _count_rows(conn, _CHARGE_WITHOUT_ORDER_QUERY)
    if charge_without_order:
        issues.append(
            InvariantIssue(
                code="charge_without_order",
                scope=f"{side}:wallet_transactions",
                detail=f"CHARGE rows reference missing orders: {charge_without_order}",
            )
        )
    return issues


# ---------------------------------------------------------------------------
# 生成计费轮次：一轮一 RESERVE、终态至多一笔、属主与批次一致、轮次连续
# ---------------------------------------------------------------------------

_BILLING_ROUND_MISMATCH_QUERY = """
SELECT COUNT(*) AS bad
FROM (
    SELECT task_id, billing_round,
           SUM(CASE WHEN type = 'RESERVE' THEN 1 ELSE 0 END) AS reserves,
           SUM(CASE WHEN type IN ('SETTLE', 'RELEASE') THEN 1 ELSE 0 END) AS terminals
    FROM wallet_transactions
    WHERE task_id IS NOT NULL AND billing_round IS NOT NULL
    GROUP BY task_id, billing_round
) AS rounds
WHERE rounds.reserves != 1 OR rounds.terminals > 1
"""

_BILLING_OWNER_MISMATCH_QUERY = """
SELECT COUNT(*) AS bad
FROM wallet_transactions AS wt
LEFT JOIN generation_tasks AS t ON t.id = wt.task_id
LEFT JOIN generation_batches AS b ON b.id = t.batch_id
WHERE wt.task_id IS NOT NULL AND wt.billing_round IS NOT NULL
  AND (t.id IS NULL OR b.id IS NULL
       OR b.created_by_user_id IS NULL
       OR b.created_by_user_id != wt.user_id)
"""

_BILLING_ROUND_GAP_QUERY = """
SELECT COUNT(*) AS bad
FROM (
    SELECT task_id,
           MIN(billing_round) AS first_round,
           MAX(billing_round) AS last_round,
           COUNT(DISTINCT billing_round) AS distinct_rounds
    FROM wallet_transactions
    WHERE task_id IS NOT NULL AND billing_round IS NOT NULL
    GROUP BY task_id
) AS spans
WHERE spans.first_round != 1 OR spans.distinct_rounds != spans.last_round
"""


def _generation_billing_issues(conn: psycopg.Connection[Any], side: str) -> list[InvariantIssue]:
    tables = set(_table_names(conn))
    required = {"wallet_transactions", "generation_tasks", "generation_batches"}
    if not required.issubset(tables):
        return []
    issues: list[InvariantIssue] = []
    round_mismatched = _count_rows(conn, _BILLING_ROUND_MISMATCH_QUERY)
    if round_mismatched:
        issues.append(
            InvariantIssue(
                code="generation_billing_round_mismatch",
                scope=f"{side}:wallet_transactions",
                detail=(
                    "task billing rounds without exactly one RESERVE or with "
                    f"multiple terminals: {round_mismatched}"
                ),
            )
        )
    owner_mismatched = _count_rows(conn, _BILLING_OWNER_MISMATCH_QUERY)
    if owner_mismatched:
        issues.append(
            InvariantIssue(
                code="generation_billing_owner_mismatch",
                scope=f"{side}:wallet_transactions",
                detail=(
                    f"task billing rows not owned by the generation batch owner: {owner_mismatched}"
                ),
            )
        )
    round_gaps = _count_rows(conn, _BILLING_ROUND_GAP_QUERY)
    if round_gaps:
        issues.append(
            InvariantIssue(
                code="generation_billing_round_gap",
                scope=f"{side}:wallet_transactions",
                detail=f"tasks with non-contiguous billing rounds: {round_gaps}",
            )
        )
    return issues


# ---------------------------------------------------------------------------
# 资产引用：*_asset_id 外键形状与 *_asset_ids_json 的 JSON 引用都不落孤儿
# ---------------------------------------------------------------------------


def _asset_reference_issues_pg(conn: psycopg.Connection[Any], side: str) -> list[InvariantIssue]:
    tables = _table_names(conn)
    if "assets" not in tables:
        return []
    issues: list[InvariantIssue] = []
    for table in tables:
        if table == "assets":
            continue
        columns = _pg_columns(conn, table)
        for column in columns:
            if column != "asset_id" and not column.endswith("_asset_id"):
                continue
            query = sql.SQL(
                "SELECT COUNT(*) AS orphan_count FROM {} AS child "
                "LEFT JOIN assets AS parent ON child.{} = parent.id "
                "WHERE child.{} IS NOT NULL AND parent.id IS NULL"
            ).format(sql.Identifier(table), sql.Identifier(column), sql.Identifier(column))
            count = _count_rows(conn, query)
            if count:
                issues.append(
                    InvariantIssue(
                        code="asset_reference_orphan",
                        scope=f"{side}:{table}.{column}",
                        detail=f"asset reference orphan count={count}",
                    )
                )
    issues.extend(_json_asset_reference_issues(conn, tables, side))
    return issues


def _json_asset_reference_issues(
    conn: psycopg.Connection[Any],
    tables: Sequence[str],
    side: str,
) -> list[InvariantIssue]:
    """校验 ``*_asset_ids_json`` 列：库内 JSON 展开，不物化到 Python。

    ``pg_input_is_valid``（PG16 基线）做合法性预检；``CASE`` 把每次 cast 都
    挡在预检之后——PG 不保证 AND/OR 链内的短路求值。
    """
    issues: list[InvariantIssue] = []
    for table in tables:
        columns = _pg_columns(conn, table)
        for column in (name for name in columns if name.endswith("_asset_ids_json")):
            identifiers = {"table": sql.Identifier(table), "column": sql.Identifier(column)}
            invalid = _count_rows(
                conn,
                sql.SQL(
                    """
                    SELECT COUNT(*) FROM {table} AS row_src
                    WHERE row_src.{column} IS NOT NULL
                      AND CASE
                            WHEN NOT pg_input_is_valid(row_src.{column}, 'jsonb') THEN true
                            WHEN jsonb_typeof(row_src.{column}::jsonb) IS DISTINCT FROM 'array'
                              THEN true
                            WHEN EXISTS (
                              SELECT 1
                              FROM jsonb_array_elements(row_src.{column}::jsonb) AS element
                              WHERE jsonb_typeof(element) IS DISTINCT FROM 'string'
                            ) THEN true
                            ELSE false
                          END
                    """
                ).format(**identifiers),
            )
            orphan = _count_rows(
                conn,
                sql.SQL(
                    """
                    SELECT COUNT(*) FROM {table} AS row_src
                    WHERE row_src.{column} IS NOT NULL
                      AND CASE
                            WHEN NOT pg_input_is_valid(row_src.{column}, 'jsonb') THEN false
                            WHEN jsonb_typeof(row_src.{column}::jsonb) = 'array' THEN EXISTS (
                              SELECT 1
                              FROM jsonb_array_elements_text(row_src.{column}::jsonb) AS element
                              WHERE element.value NOT IN (SELECT id FROM assets)
                            )
                            ELSE false
                          END
                    """
                ).format(**identifiers),
            )
            if invalid:
                issues.append(
                    InvariantIssue(
                        code="asset_reference_json_invalid",
                        scope=f"{side}:{table}.{column}",
                        detail=f"invalid asset reference JSON rows={invalid}",
                    )
                )
            if orphan:
                issues.append(
                    InvariantIssue(
                        code="asset_reference_json_orphan",
                        scope=f"{side}:{table}.{column}",
                        detail=f"orphan asset reference JSON rows={orphan}",
                    )
                )
    return issues


def validate_pg_invariants(
    conn: psycopg.Connection[Any], *, side: str = "target"
) -> tuple[InvariantIssue, ...]:
    """原 validate_database_invariants 的 PostgreSQL 侧等价实现（四组检查齐上）。"""
    issues: list[InvariantIssue] = []
    issues.extend(_asset_reference_issues_pg(conn, side))
    issues.extend(_wallet_issues(conn, side))
    issues.extend(_paid_charge_issues(conn, side))
    issues.extend(_generation_billing_issues(conn, side))
    return tuple(issues)
