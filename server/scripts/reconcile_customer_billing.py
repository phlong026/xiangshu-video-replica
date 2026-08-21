"""T07 database reconciliation for the SQLite-to-PostgreSQL cutover.

The report contains only counts and SHA-256 digests. It deliberately avoids
raw business values, credentials, storage URLs, activation codes, and tokens.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

Dialect = Literal["sqlite", "postgresql"]
EXCLUDED_TABLES = frozenset({"alembic_version"})


@dataclass(frozen=True)
class ReconciliationIssue:
    code: str
    scope: str
    detail: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class TableReconciliation:
    table: str
    source_count: int
    target_count: int
    source_pk_sha256: str | None
    target_pk_sha256: str | None
    source_rows_sha256: str
    target_rows_sha256: str

    @property
    def matches(self) -> bool:
        return (
            self.source_count == self.target_count
            and self.source_pk_sha256 == self.target_pk_sha256
            and self.source_rows_sha256 == self.target_rows_sha256
        )

    def to_dict(self) -> dict[str, object]:
        result = asdict(self)
        result["matches"] = self.matches
        return result


@dataclass(frozen=True)
class ReconciliationReport:
    tables: tuple[TableReconciliation, ...]
    issues: tuple[ReconciliationIssue, ...]

    @property
    def ok(self) -> bool:
        return not self.issues and all(table.matches for table in self.tables)

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "tables": [table.to_dict() for table in self.tables],
            "issues": [issue.to_dict() for issue in self.issues],
        }


def _quote_sqlite_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _open_sqlite_readonly(path: str | Path) -> sqlite3.Connection:
    database = Path(path)
    if not database.is_file():
        raise FileNotFoundError(database)
    conn = sqlite3.connect(
        f"{database.resolve().as_uri()}?mode=ro",
        uri=True,
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _table_names(conn: sqlite3.Connection | psycopg.Connection[Any], dialect: Dialect) -> list[str]:
    if dialect == "sqlite":
        rows = conn.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
            ORDER BY name
            """
        ).fetchall()
        return [str(row[0]) for row in rows if str(row[0]) not in EXCLUDED_TABLES]
    rows = conn.execute(
        """
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
        ORDER BY table_name
        """
    ).fetchall()
    return [str(row["table_name"]) for row in rows if str(row["table_name"]) not in EXCLUDED_TABLES]


def _sqlite_columns(conn: sqlite3.Connection, table: str) -> tuple[list[str], list[str]]:
    rows = conn.execute(f"PRAGMA table_info({_quote_sqlite_identifier(table)})").fetchall()
    columns = [str(row[1]) for row in rows]
    primary_key = [str(row[1]) for row in sorted(rows, key=lambda row: int(row[5])) if int(row[5])]
    return columns, primary_key


def _pg_columns(
    conn: psycopg.Connection[Any], table: str
) -> tuple[list[str], list[str], dict[str, str]]:
    rows = conn.execute(
        """
        SELECT column_name, data_type, udt_name
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = %s
        ORDER BY ordinal_position
        """,
        (table,),
    ).fetchall()
    columns = [str(row["column_name"]) for row in rows]
    types = {
        str(row["column_name"]): str(row["udt_name"] or row["data_type"]).lower() for row in rows
    }
    pk_rows = conn.execute(
        """
        SELECT kcu.column_name
        FROM information_schema.table_constraints AS tc
        JOIN information_schema.key_column_usage AS kcu
          ON tc.constraint_name = kcu.constraint_name
         AND tc.table_schema = kcu.table_schema
        WHERE tc.table_schema = 'public'
          AND tc.table_name = %s
          AND tc.constraint_type = 'PRIMARY KEY'
        ORDER BY kcu.ordinal_position
        """,
        (table,),
    ).fetchall()
    primary_key = [str(row["column_name"]) for row in pk_rows]
    return columns, primary_key, types


def _canonical_value(value: object, target_type: str | None) -> object:
    if value is None:
        return None
    if isinstance(value, memoryview):
        value = bytes(value)
    if isinstance(value, bytes):
        return {"bytes_sha256": hashlib.sha256(value).hexdigest(), "size": len(value)}
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, float):
        if math.isnan(value):
            return "NaN"
        if math.isinf(value):
            return "Infinity" if value > 0 else "-Infinity"
        return format(value, ".17g")
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if target_type in {"json", "jsonb"}:
        parsed = json.loads(value) if isinstance(value, str) else value
        return _canonical_json(parsed)
    if isinstance(value, (dict, list, tuple)):
        return _canonical_json(value)
    return str(value) if not isinstance(value, str) else value


def _canonical_json(value: object) -> object:
    if isinstance(value, Mapping):
        ordered = sorted(value.items(), key=lambda item: str(item[0]))
        return {str(key): _canonical_json(item) for key, item in ordered}
    if isinstance(value, (list, tuple)):
        return [_canonical_json(item) for item in value]
    return _canonical_value(value, None)


def _digest_lines(lines: Iterable[str]) -> str:
    digest = hashlib.sha256()
    for line in sorted(lines):
        digest.update(line.encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _row_payload(
    row: Mapping[str, object], columns: Sequence[str], target_types: Mapping[str, str]
) -> str:
    values = [_canonical_value(row[column], target_types.get(column)) for column in columns]
    return json.dumps(values, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _fetch_sqlite_rows(
    conn: sqlite3.Connection, table: str, columns: Sequence[str]
) -> list[dict[str, object]]:
    names = ", ".join(_quote_sqlite_identifier(column) for column in columns)
    rows = conn.execute(f"SELECT {names} FROM {_quote_sqlite_identifier(table)}").fetchall()
    return [{key: row[key] for key in row.keys()} for row in rows]


def _fetch_pg_rows(
    conn: psycopg.Connection[Any], table: str, columns: Sequence[str]
) -> list[dict[str, object]]:
    query = sql.SQL("SELECT {} FROM {}").format(
        sql.SQL(", ").join(sql.Identifier(column) for column in columns),
        sql.Identifier(table),
    )
    rows = conn.execute(query).fetchall()
    return [dict(row) for row in rows]


def _fingerprint(
    rows: Sequence[Mapping[str, object]],
    columns: Sequence[str],
    primary_key: Sequence[str],
    target_types: Mapping[str, str],
) -> tuple[int, str | None, str]:
    row_payloads = [_row_payload(row, columns, target_types) for row in rows]
    row_hash = _digest_lines(
        hashlib.sha256(payload.encode("utf-8")).hexdigest() for payload in row_payloads
    )
    pk_hash: str | None = None
    if primary_key:
        pk_payloads = [
            json.dumps(
                [_canonical_value(row[column], target_types.get(column)) for column in primary_key],
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            for row in rows
        ]
        pk_hash = _digest_lines(pk_payloads)
    return len(rows), pk_hash, row_hash


def _table_reconciliation(
    sqlite_conn: sqlite3.Connection,
    pg_conn: psycopg.Connection[Any],
    table: str,
) -> tuple[TableReconciliation | None, list[ReconciliationIssue]]:
    issues: list[ReconciliationIssue] = []
    source_columns, source_pk = _sqlite_columns(sqlite_conn, table)
    target_columns, target_pk, target_types = _pg_columns(pg_conn, table)
    if set(source_columns) != set(target_columns):
        issues.append(
            ReconciliationIssue(
                code="table_column_mismatch",
                scope=table,
                detail=(
                    f"column sets differ: source={len(source_columns)} target={len(target_columns)}"
                ),
            )
        )
        return None, issues
    if source_pk != target_pk:
        issues.append(
            ReconciliationIssue(
                code="table_primary_key_contract_mismatch",
                scope=table,
                detail="source and target primary-key column order differs",
            )
        )
        return None, issues

    source_rows = _fetch_sqlite_rows(sqlite_conn, table, source_columns)
    target_rows = _fetch_pg_rows(pg_conn, table, source_columns)
    source_count, source_pk_hash, source_rows_hash = _fingerprint(
        source_rows, source_columns, source_pk, target_types
    )
    target_count, target_pk_hash, target_rows_hash = _fingerprint(
        target_rows, source_columns, source_pk, target_types
    )
    result = TableReconciliation(
        table=table,
        source_count=source_count,
        target_count=target_count,
        source_pk_sha256=source_pk_hash,
        target_pk_sha256=target_pk_hash,
        source_rows_sha256=source_rows_hash,
        target_rows_sha256=target_rows_hash,
    )
    if source_count != target_count:
        issues.append(
            ReconciliationIssue(
                code="table_row_count_mismatch",
                scope=table,
                detail=f"source={source_count} target={target_count}",
            )
        )
    if source_pk_hash != target_pk_hash:
        issues.append(
            ReconciliationIssue(
                code="table_primary_key_mismatch",
                scope=table,
                detail="primary-key set SHA-256 differs",
            )
        )
    if source_rows_hash != target_rows_hash:
        issues.append(
            ReconciliationIssue(
                code="table_hash_mismatch",
                scope=table,
                detail="canonical row SHA-256 differs",
            )
        )
    return result, issues


def _mapping_rows(cursor: Any) -> list[dict[str, object]]:
    rows = cursor.fetchall()
    result: list[dict[str, object]] = []
    for row in rows:
        if isinstance(row, sqlite3.Row):
            result.append({key: row[key] for key in row.keys()})
        else:
            result.append(dict(row))
    return result


def _wallet_issues(
    conn: sqlite3.Connection | psycopg.Connection[Any], dialect: Dialect, side: str
) -> list[ReconciliationIssue]:
    tables = set(_table_names(conn, dialect))
    if not {"wallets", "wallet_transactions"}.issubset(tables):
        return []
    transactions = _mapping_rows(
        conn.execute(
            """
            SELECT user_id,
                   COALESCE(SUM(available_delta), 0) AS available_total,
                   COALESCE(SUM(reserved_delta), 0) AS reserved_total
            FROM wallet_transactions
            GROUP BY user_id
            """
        )
    )
    totals = {
        str(row["user_id"]): (int(row["available_total"]), int(row["reserved_total"]))
        for row in transactions
    }
    wallets = _mapping_rows(conn.execute("SELECT user_id, available_credits, reserved_credits FROM wallets"))
    issues: list[ReconciliationIssue] = []
    wallet_users: set[str] = set()
    for wallet in wallets:
        user_id = str(wallet["user_id"])
        wallet_users.add(user_id)
        expected_available, expected_reserved = totals.get(user_id, (0, 0))
        actual_available = int(wallet["available_credits"])
        actual_reserved = int(wallet["reserved_credits"])
        if (
            actual_available != expected_available
            or actual_reserved != expected_reserved
            or actual_available < 0
            or actual_reserved < 0
        ):
            issues.append(
                ReconciliationIssue(
                    code="wallet_balance_mismatch",
                    scope=f"{side}:wallets",
                    detail=(
                        "wallet aggregate differs from append-only transaction deltas "
                        f"for 1 user (available={actual_available}/{expected_available}, "
                        f"reserved={actual_reserved}/{expected_reserved})"
                    ),
                )
            )
    missing_wallets = set(totals) - wallet_users
    if missing_wallets:
        issues.append(
            ReconciliationIssue(
                code="wallet_missing_for_ledger_owner",
                scope=f"{side}:wallets",
                detail=f"ledger owners without wallets: {len(missing_wallets)}",
            )
        )
    return issues


def _paid_charge_issues(
    conn: sqlite3.Connection | psycopg.Connection[Any], dialect: Dialect, side: str
) -> list[ReconciliationIssue]:
    tables = set(_table_names(conn, dialect))
    if not {"recharge_orders", "wallet_transactions"}.issubset(tables):
        return []
    orders = _mapping_rows(
        conn.execute("SELECT id, user_id, status, credits FROM recharge_orders")
    )
    charges = _mapping_rows(
        conn.execute(
            """
            SELECT recharge_order_id, user_id, available_delta, reserved_delta
            FROM wallet_transactions
            WHERE type = 'CHARGE'
            """
        )
    )
    by_order: dict[str, list[dict[str, object]]] = defaultdict(list)
    for charge in charges:
        order_id = charge["recharge_order_id"]
        if order_id is not None:
            by_order[str(order_id)].append(charge)
    order_by_id = {str(order["id"]): order for order in orders}
    issues: list[ReconciliationIssue] = []
    for order_id, order in order_by_id.items():
        linked = by_order.get(order_id, [])
        if str(order["status"]) == "PAID":
            valid = (
                len(linked) == 1
                and str(linked[0]["user_id"]) == str(order["user_id"])
                and int(linked[0]["available_delta"]) == int(order["credits"])
                and int(linked[0]["reserved_delta"]) == 0
            )
            if not valid:
                issues.append(
                    ReconciliationIssue(
                        code="paid_order_charge_mismatch",
                        scope=f"{side}:recharge_orders",
                        detail="a PAID order does not have exactly one shape-matching CHARGE",
                    )
                )
        elif linked:
            issues.append(
                ReconciliationIssue(
                    code="unpaid_order_has_charge",
                    scope=f"{side}:recharge_orders",
                    detail="a non-PAID order has one or more CHARGE rows",
                )
            )
    orphan_charges = set(by_order) - set(order_by_id)
    if orphan_charges:
        issues.append(
            ReconciliationIssue(
                code="charge_without_order",
                scope=f"{side}:wallet_transactions",
                detail=f"CHARGE rows reference missing orders: {len(orphan_charges)}",
            )
        )
    return issues


def _generation_billing_issues(
    conn: sqlite3.Connection | psycopg.Connection[Any], dialect: Dialect, side: str
) -> list[ReconciliationIssue]:
    tables = set(_table_names(conn, dialect))
    required = {"wallet_transactions", "generation_tasks", "generation_batches"}
    if not required.issubset(tables):
        return []
    rows = _mapping_rows(
        conn.execute(
            """
            SELECT task_id, billing_round, type, user_id
            FROM wallet_transactions
            WHERE task_id IS NOT NULL AND billing_round IS NOT NULL
            """
        )
    )
    owners = _mapping_rows(
        conn.execute(
            """
            SELECT task.id AS task_id, batch.created_by_user_id AS owner_user_id
            FROM generation_tasks AS task
            JOIN generation_batches AS batch ON batch.id = task.batch_id
            """
        )
    )
    owner_by_task = {str(row["task_id"]): str(row["owner_user_id"]) for row in owners}
    grouped: dict[tuple[str, int], list[dict[str, object]]] = defaultdict(list)
    rounds_by_task: dict[str, set[int]] = defaultdict(set)
    for row in rows:
        task_id = str(row["task_id"])
        billing_round = int(row["billing_round"])
        grouped[(task_id, billing_round)].append(row)
        rounds_by_task[task_id].add(billing_round)
    issues: list[ReconciliationIssue] = []
    for (task_id, billing_round), entries in grouped.items():
        reserves = [entry for entry in entries if str(entry["type"]) == "RESERVE"]
        terminals = [entry for entry in entries if str(entry["type"]) in {"SETTLE", "RELEASE"}]
        if len(reserves) != 1 or len(terminals) > 1:
            issues.append(
                ReconciliationIssue(
                    code="generation_billing_round_mismatch",
                    scope=f"{side}:wallet_transactions",
                    detail=(
                        "a task billing round lacks exactly one RESERVE or has multiple terminals "
                        f"(round={billing_round})"
                    ),
                )
            )
        owner = owner_by_task.get(task_id)
        if owner is None or any(str(entry["user_id"]) != owner for entry in entries):
            issues.append(
                ReconciliationIssue(
                    code="generation_billing_owner_mismatch",
                    scope=f"{side}:wallet_transactions",
                    detail="a task billing round is not owned by the generation batch owner",
                )
            )
    for task_id, rounds in rounds_by_task.items():
        expected = set(range(1, max(rounds) + 1))
        if rounds != expected:
            issues.append(
                ReconciliationIssue(
                    code="generation_billing_round_gap",
                    scope=f"{side}:wallet_transactions",
                    detail=f"billing rounds are not contiguous for 1 task ({len(rounds)} rows)",
                )
            )
    return issues


def _asset_reference_issues_sqlite(
    conn: sqlite3.Connection, side: str
) -> list[ReconciliationIssue]:
    tables = _table_names(conn, "sqlite")
    if "assets" not in tables:
        return []
    issues: list[ReconciliationIssue] = []
    for table in tables:
        if table == "assets":
            continue
        columns, _ = _sqlite_columns(conn, table)
        for column in columns:
            if column != "asset_id" and not column.endswith("_asset_id"):
                continue
            query = (
                f"SELECT COUNT(*) FROM {_quote_sqlite_identifier(table)} AS child "
                "LEFT JOIN assets AS parent ON "
                f"child.{_quote_sqlite_identifier(column)} = parent.id "
                f"WHERE child.{_quote_sqlite_identifier(column)} IS NOT NULL "
                "AND parent.id IS NULL"
            )
            count = int(conn.execute(query).fetchone()[0])
            if count:
                issues.append(
                    ReconciliationIssue(
                        code="asset_reference_orphan",
                        scope=f"{side}:{table}.{column}",
                        detail=f"asset reference orphan count={count}",
                    )
                )
    return issues


def _asset_reference_issues_pg(
    conn: psycopg.Connection[Any], side: str
) -> list[ReconciliationIssue]:
    tables = _table_names(conn, "postgresql")
    if "assets" not in tables:
        return []
    issues: list[ReconciliationIssue] = []
    for table in tables:
        if table == "assets":
            continue
        columns, _, _ = _pg_columns(conn, table)
        for column in columns:
            if column != "asset_id" and not column.endswith("_asset_id"):
                continue
            query = sql.SQL(
                "SELECT COUNT(*) FROM {} AS child "
                "LEFT JOIN assets AS parent ON child.{} = parent.id "
                "WHERE child.{} IS NOT NULL AND parent.id IS NULL"
            ).format(sql.Identifier(table), sql.Identifier(column), sql.Identifier(column))
            count = int(conn.execute(query).fetchone()[0])
            if count:
                issues.append(
                    ReconciliationIssue(
                        code="asset_reference_orphan",
                        scope=f"{side}:{table}.{column}",
                        detail=f"asset reference orphan count={count}",
                    )
                )
    return issues


def validate_database_invariants(
    conn: sqlite3.Connection | psycopg.Connection[Any], dialect: Dialect, side: str
) -> tuple[ReconciliationIssue, ...]:
    issues: list[ReconciliationIssue] = []
    if dialect == "sqlite":
        assert isinstance(conn, sqlite3.Connection)
        issues.extend(_asset_reference_issues_sqlite(conn, side))
    else:
        issues.extend(_asset_reference_issues_pg(conn, side))  # type: ignore[arg-type]
    issues.extend(_wallet_issues(conn, dialect, side))
    issues.extend(_paid_charge_issues(conn, dialect, side))
    issues.extend(_generation_billing_issues(conn, dialect, side))
    return tuple(issues)


def reconcile_connection_pair(
    sqlite_conn: sqlite3.Connection,
    pg_conn: psycopg.Connection[Any],
) -> ReconciliationReport:
    source_tables = set(_table_names(sqlite_conn, "sqlite"))
    target_tables = set(_table_names(pg_conn, "postgresql"))
    issues: list[ReconciliationIssue] = []
    missing = sorted(source_tables - target_tables)
    extra = sorted(target_tables - source_tables)
    if missing:
        issues.append(
            ReconciliationIssue(
                code="target_table_missing",
                scope="schema",
                detail=f"target is missing source tables: {len(missing)}",
            )
        )
    if extra:
        issues.append(
            ReconciliationIssue(
                code="target_table_extra",
                scope="schema",
                detail=f"target has tables absent from source: {len(extra)}",
            )
        )

    tables: list[TableReconciliation] = []
    for table in sorted(source_tables & target_tables):
        result, table_issues = _table_reconciliation(sqlite_conn, pg_conn, table)
        if result is not None:
            tables.append(result)
        issues.extend(table_issues)
    issues.extend(validate_database_invariants(sqlite_conn, "sqlite", "source"))
    issues.extend(validate_database_invariants(pg_conn, "postgresql", "target"))
    return ReconciliationReport(tables=tuple(tables), issues=tuple(issues))


def reconcile_databases(
    sqlite_path: str | Path,
    postgres_dsn: str,
) -> ReconciliationReport:
    with _open_sqlite_readonly(sqlite_path) as sqlite_conn:
        with psycopg.connect(postgres_dsn, row_factory=dict_row) as pg_conn:
            return reconcile_connection_pair(sqlite_conn, pg_conn)


def main() -> None:
    parser = argparse.ArgumentParser(description="Reconcile a legacy SQLite snapshot with PG")
    parser.add_argument("--sqlite", required=True, help="read-only SQLite snapshot path")
    parser.add_argument("--postgres-url", required=True, help="PostgreSQL DSN (never printed)")
    parser.add_argument("--output", help="optional JSON report path")
    args = parser.parse_args()

    report = reconcile_databases(args.sqlite, args.postgres_url)
    payload = json.dumps(report.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(payload + "\n", encoding="utf-8")
    print(payload)
    raise SystemExit(0 if report.ok else 1)


if __name__ == "__main__":
    main()
