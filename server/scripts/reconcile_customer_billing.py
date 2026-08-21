"""T07 reconciliation for the one-shot SQLite-to-PostgreSQL cutover.

Reports contain only counts and SHA-256 digests. Raw business values,
credentials, storage URLs, activation codes and tokens are never emitted.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import sqlite3
from collections import defaultdict
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal
from urllib.parse import unquote, urlsplit, urlunsplit
from uuid import UUID, uuid4

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

Dialect = Literal["sqlite", "postgresql"]
EXCLUDED_TABLES = frozenset({"alembic_version"})
JSON_ASSET_ID_COLUMNS = frozenset(
    {
        "reference_asset_ids_json",
        "recommended_asset_ids_json",
        "selected_asset_ids_json",
    }
)
_DIGEST_MODULUS = 1 << 256


@dataclass(frozen=True)
class ColumnSpec:
    name: str
    target_type: str | None
    nullable: bool = True


@dataclass(frozen=True)
class TableDigest:
    row_count: int
    primary_key_sha256: str | None
    row_sha256: str


@dataclass(frozen=True, eq=False)
class ReconciliationIssue:
    code: str
    scope: str
    detail: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)

    @property
    def identifier(self) -> str:
        if self.code in {
            "table_row_count_mismatch",
            "table_primary_key_mismatch",
            "table_hash_mismatch",
        }:
            return f"TABLE_DIGEST_MISMATCH:{self.scope}"
        if self.code.startswith("wallet_") or self.code in {
            "paid_order_charge_mismatch",
            "unpaid_order_has_charge",
            "charge_without_order",
            "generation_billing_round_mismatch",
            "generation_billing_owner_mismatch",
            "generation_billing_round_gap",
        }:
            prefix = "TARGET" if self.scope.startswith("target:") else "SOURCE"
            return f"{prefix}_WALLET_INVARIANT_FAILED"
        return f"{self.code.upper()}:{self.scope}"

    def __str__(self) -> str:
        return self.identifier

    def __eq__(self, other: object) -> bool:
        if isinstance(other, ReconciliationIssue):
            return (
                self.code,
                self.scope,
                self.detail,
            ) == (
                other.code,
                other.scope,
                other.detail,
            )
        if isinstance(other, str):
            return other in {self.identifier, self.code, f"{self.code}:{self.scope}"}
        return False

    def __hash__(self) -> int:
        return hash((self.code, self.scope, self.detail))


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

    @property
    def issue_ids(self) -> tuple[str, ...]:
        return tuple(issue.identifier for issue in self.issues)

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "tables": [table.to_dict() for table in self.tables],
            "issues": [issue.to_dict() for issue in self.issues],
        }


@dataclass(frozen=True)
class WalletInvariantResult:
    wallet_balance_mismatches: int
    paid_without_charge: int
    other_issue_count: int

    @property
    def ok(self) -> bool:
        return not (
            self.wallet_balance_mismatches or self.paid_without_charge or self.other_issue_count
        )


@dataclass(frozen=True)
class AssetReferenceResult:
    orphan_reference_count: int
    invalid_json_count: int
    object_storage_checked: bool = False

    @property
    def ok(self) -> bool:
        return self.orphan_reference_count == 0 and self.invalid_json_count == 0


def _quote_sqlite_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _row_value(row: object, key: str, index: int) -> object:
    if isinstance(row, sqlite3.Row):
        return row[key]
    if isinstance(row, Mapping):
        return row[key]
    if isinstance(row, Sequence) and not isinstance(row, (str, bytes, bytearray)):
        return row[index]
    raise TypeError(f"unsupported database row type: {type(row)!r}")


def _cursor_mappings(cursor: Any, *, batch_size: int = 1000) -> Iterator[dict[str, object]]:
    description = cursor.description or ()
    names = [str(column.name if hasattr(column, "name") else column[0]) for column in description]
    while True:
        rows = cursor.fetchmany(batch_size)
        if not rows:
            return
        for row in rows:
            if isinstance(row, sqlite3.Row):
                yield {key: row[key] for key in row.keys()}
            elif isinstance(row, Mapping):
                yield {str(key): value for key, value in row.items()}
            else:
                yield dict(zip(names, row, strict=True))


def connect_sqlite_readonly(path: str | Path) -> sqlite3.Connection:
    database = Path(path)
    if not database.is_file():
        raise FileNotFoundError(database)
    conn = sqlite3.connect(
        f"{database.resolve().as_uri()}?mode=ro",
        uri=True,
        timeout=5,
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


_open_sqlite_readonly = connect_sqlite_readonly


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
        return [
            str(_row_value(row, "name", 0))
            for row in rows
            if str(_row_value(row, "name", 0)) not in EXCLUDED_TABLES
        ]
    rows = conn.execute(
        """
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
        ORDER BY table_name
        """
    ).fetchall()
    return [
        str(_row_value(row, "table_name", 0))
        for row in rows
        if str(_row_value(row, "table_name", 0)) not in EXCLUDED_TABLES
    ]


def _sqlite_column_specs(conn: sqlite3.Connection, table: str) -> tuple[list[ColumnSpec], list[str]]:
    rows = conn.execute(f"PRAGMA table_info({_quote_sqlite_identifier(table)})").fetchall()
    specs = [
        ColumnSpec(
            name=str(_row_value(row, "name", 1)),
            target_type=str(_row_value(row, "type", 2) or "text").lower(),
            nullable=not bool(int(_row_value(row, "notnull", 3))),
        )
        for row in rows
    ]
    primary_key = [
        str(_row_value(row, "name", 1))
        for row in sorted(rows, key=lambda item: int(_row_value(item, "pk", 5)))
        if int(_row_value(row, "pk", 5))
    ]
    return specs, primary_key


def _sqlite_columns(conn: sqlite3.Connection, table: str) -> tuple[list[str], list[str]]:
    specs, primary_key = _sqlite_column_specs(conn, table)
    return [spec.name for spec in specs], primary_key


def _pg_column_specs(
    conn: psycopg.Connection[Any], table: str
) -> tuple[list[ColumnSpec], list[str]]:
    rows = conn.execute(
        """
        SELECT column_name, data_type, udt_name, is_nullable
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = %s
        ORDER BY ordinal_position
        """,
        (table,),
    ).fetchall()
    specs = [
        ColumnSpec(
            name=str(_row_value(row, "column_name", 0)),
            target_type=str(
                _row_value(row, "udt_name", 2) or _row_value(row, "data_type", 1)
            ).lower(),
            nullable=str(_row_value(row, "is_nullable", 3)).upper() == "YES",
        )
        for row in rows
    ]
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
    primary_key = [str(_row_value(row, "column_name", 0)) for row in pk_rows]
    return specs, primary_key


def _pg_columns(
    conn: psycopg.Connection[Any], table: str
) -> tuple[list[str], list[str], dict[str, str]]:
    specs, primary_key = _pg_column_specs(conn, table)
    return (
        [spec.name for spec in specs],
        primary_key,
        {spec.name: str(spec.target_type or "text") for spec in specs},
    )


def _canonical_decimal(value: object) -> str:
    number = value if isinstance(value, Decimal) else Decimal(str(value))
    if not number.is_finite():
        return str(number)
    if number == 0:
        return "0"
    return format(number.normalize(), "f")


def _canonical_datetime(value: object) -> str:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(
        str(value).replace("Z", "+00:00")
    )
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(UTC)
    return parsed.isoformat()


def canonical_value(value: object, target_type: str | None) -> object:
    if value is None:
        return None
    normalized_type = None if target_type is None else target_type.lower()
    if isinstance(value, memoryview):
        value = bytes(value)
    if normalized_type == "bytea" or isinstance(value, bytes):
        raw = value if isinstance(value, bytes) else bytes(value)
        return {"base64": base64.b64encode(raw).decode("ascii")}
    if normalized_type in {"bool", "boolean"}:
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"1", "true", "t", "yes", "on"}:
                return True
            if normalized in {"0", "false", "f", "no", "off"}:
                return False
            raise ValueError("invalid boolean value in reconciliation input")
        return bool(value)
    if normalized_type in {"int2", "int4", "int8", "smallint", "integer", "bigint"}:
        return int(value)
    if normalized_type in {"numeric", "decimal"}:
        return _canonical_decimal(value)
    if normalized_type in {"float4", "float8", "real", "double precision"}:
        number = float(value)
        if math.isnan(number):
            return "NaN"
        if math.isinf(number):
            return "Infinity" if number > 0 else "-Infinity"
        return format(number, ".17g")
    if normalized_type == "uuid":
        return str(value if isinstance(value, UUID) else UUID(str(value)))
    if normalized_type == "date":
        parsed_date = value if isinstance(value, date) else date.fromisoformat(str(value))
        return parsed_date.isoformat()
    if normalized_type in {"timestamp", "timestamptz"}:
        return _canonical_datetime(value)
    if normalized_type in {"time", "timetz"}:
        parsed_time = value if isinstance(value, time) else time.fromisoformat(str(value))
        return parsed_time.isoformat()
    if normalized_type in {"json", "jsonb"}:
        parsed = json.loads(value) if isinstance(value, str) else value
        return _canonical_json(parsed)
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, Decimal):
        return _canonical_decimal(value)
    if isinstance(value, float):
        if math.isnan(value):
            return "NaN"
        if math.isinf(value):
            return "Infinity" if value > 0 else "-Infinity"
        return format(value, ".17g")
    if isinstance(value, datetime):
        return _canonical_datetime(value)
    if isinstance(value, (date, time)):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, (dict, list, tuple)):
        return _canonical_json(value)
    return value if isinstance(value, str) else str(value)


_canonical_value = canonical_value


def _canonical_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {
            str(key): _canonical_json(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_canonical_json(item) for item in value]
    return canonical_value(value, None)


def _row_payload(
    row: Mapping[str, object], columns: Sequence[ColumnSpec]
) -> bytes:
    values = [canonical_value(row[column.name], column.target_type) for column in columns]
    return json.dumps(
        values,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _pk_payload(
    row: Mapping[str, object], by_name: Mapping[str, ColumnSpec], primary_key: Sequence[str]
) -> bytes:
    values = [canonical_value(row[column], by_name[column].target_type) for column in primary_key]
    return json.dumps(
        values,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


class _MultisetDigest:
    def __init__(self) -> None:
        self.count = 0
        self.xor = 0
        self.total = 0
        self.square_total = 0

    def add(self, digest_bytes: bytes) -> None:
        value = int.from_bytes(digest_bytes, "big")
        self.count += 1
        self.xor ^= value
        self.total = (self.total + value) % _DIGEST_MODULUS
        self.square_total = (self.square_total + value * value) % _DIGEST_MODULUS

    def hexdigest(self) -> str:
        payload = (
            self.count.to_bytes(16, "big")
            + self.xor.to_bytes(32, "big")
            + self.total.to_bytes(32, "big")
            + self.square_total.to_bytes(32, "big")
        )
        return hashlib.sha256(payload).hexdigest()


def _sqlite_digest_cursor(
    conn: sqlite3.Connection,
    table: str,
    columns: Sequence[ColumnSpec],
    primary_key: Sequence[str],
) -> Any:
    selected = ", ".join(_quote_sqlite_identifier(column.name) for column in columns)
    query = f"SELECT {selected} FROM {_quote_sqlite_identifier(table)}"
    if primary_key:
        query += " ORDER BY " + ", ".join(
            _quote_sqlite_identifier(column) for column in primary_key
        )
    return conn.execute(query)


def _pg_digest_cursor(
    conn: psycopg.Connection[Any],
    table: str,
    columns: Sequence[ColumnSpec],
    primary_key: Sequence[str],
) -> Any:
    query = sql.SQL("SELECT {} FROM {}").format(
        sql.SQL(", ").join(sql.Identifier(column.name) for column in columns),
        sql.Identifier(table),
    )
    if primary_key:
        query += sql.SQL(" ORDER BY {} ").format(
            sql.SQL(", ").join(sql.Identifier(column) for column in primary_key)
        )
    cursor = conn.cursor(name=f"t07_digest_{uuid4().hex}", row_factory=dict_row)
    cursor.execute(query)
    return cursor


def compute_table_digest(
    conn: sqlite3.Connection | psycopg.Connection[Any],
    table: str,
    columns: Sequence[ColumnSpec],
    primary_key: Sequence[str],
    *,
    batch_size: int = 1000,
) -> TableDigest:
    """Compute a deterministic digest in bounded batches.

    Primary-key tables are streamed in key order. Tables without a primary key
    use a commutative SHA-256 multiset accumulator, so insertion order does not
    affect the result and no table-sized Python list is created.
    """

    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    by_name = {column.name: column for column in columns}
    if set(primary_key) - set(by_name):
        raise ValueError("primary-key columns must be included in the digest columns")

    if isinstance(conn, sqlite3.Connection):
        cursor = _sqlite_digest_cursor(conn, table, columns, primary_key)
        close_cursor = True
    else:
        cursor = _pg_digest_cursor(conn, table, columns, primary_key)
        close_cursor = True

    row_stream = hashlib.sha256()
    pk_stream = hashlib.sha256() if primary_key else None
    multiset = _MultisetDigest()
    row_count = 0
    try:
        for row in _cursor_mappings(cursor, batch_size=batch_size):
            payload = _row_payload(row, columns)
            row_hash = hashlib.sha256(payload).digest()
            if primary_key:
                row_stream.update(row_hash)
                assert pk_stream is not None
                pk_stream.update(_pk_payload(row, by_name, primary_key))
                pk_stream.update(b"\n")
            else:
                multiset.add(row_hash)
            row_count += 1
    finally:
        if close_cursor:
            cursor.close()

    return TableDigest(
        row_count=row_count,
        primary_key_sha256=None if pk_stream is None else pk_stream.hexdigest(),
        row_sha256=row_stream.hexdigest() if primary_key else multiset.hexdigest(),
    )


def _table_reconciliation(
    sqlite_conn: sqlite3.Connection,
    pg_conn: psycopg.Connection[Any],
    table: str,
) -> tuple[TableReconciliation | None, list[ReconciliationIssue]]:
    source_specs, source_pk = _sqlite_column_specs(sqlite_conn, table)
    target_specs, target_pk = _pg_column_specs(pg_conn, table)
    source_names = [column.name for column in source_specs]
    target_by_name = {column.name: column for column in target_specs}
    issues: list[ReconciliationIssue] = []
    if set(source_names) != set(target_by_name):
        issues.append(
            ReconciliationIssue(
                code="table_column_mismatch",
                scope=table,
                detail=(
                    f"column sets differ: source={len(source_names)} target={len(target_by_name)}"
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

    digest_specs = [
        ColumnSpec(
            name=name,
            target_type=target_by_name[name].target_type,
            nullable=target_by_name[name].nullable,
        )
        for name in source_names
    ]
    source_digest = compute_table_digest(sqlite_conn, table, digest_specs, source_pk)
    target_digest = compute_table_digest(pg_conn, table, digest_specs, source_pk)
    result = TableReconciliation(
        table=table,
        source_count=source_digest.row_count,
        target_count=target_digest.row_count,
        source_pk_sha256=source_digest.primary_key_sha256,
        target_pk_sha256=target_digest.primary_key_sha256,
        source_rows_sha256=source_digest.row_sha256,
        target_rows_sha256=target_digest.row_sha256,
    )
    if source_digest.row_count != target_digest.row_count:
        issues.append(
            ReconciliationIssue(
                code="table_row_count_mismatch",
                scope=table,
                detail=(
                    f"source={source_digest.row_count} target={target_digest.row_count}"
                ),
            )
        )
    if source_digest.primary_key_sha256 != target_digest.primary_key_sha256:
        issues.append(
            ReconciliationIssue(
                code="table_primary_key_mismatch",
                scope=table,
                detail="primary-key set SHA-256 differs",
            )
        )
    if source_digest.row_sha256 != target_digest.row_sha256:
        issues.append(
            ReconciliationIssue(
                code="table_hash_mismatch",
                scope=table,
                detail="canonical row SHA-256 differs",
            )
        )
    return result, issues


def _all_mappings(cursor: Any) -> list[dict[str, object]]:
    return list(_cursor_mappings(cursor))


def _wallet_issues(
    conn: sqlite3.Connection | psycopg.Connection[Any], dialect: Dialect, side: str
) -> list[ReconciliationIssue]:
    tables = set(_table_names(conn, dialect))
    if not {"wallets", "wallet_transactions"}.issubset(tables):
        return []
    transactions = _all_mappings(
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
    wallets = _all_mappings(
        conn.execute("SELECT user_id, available_credits, reserved_credits FROM wallets")
    )
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
    orders = _all_mappings(
        conn.execute("SELECT id, user_id, status, credits FROM recharge_orders")
    )
    charges = _all_mappings(
        conn.execute(
            """
            SELECT recharge_order_id, user_id, available_delta, reserved_delta
            FROM wallet_transactions
            WHERE type = 'CHARGE'
            """
        )
    )
    by_order: dict[str, list[dict[str, object]]] = defaultdict(list)
    charges_without_order = 0
    for charge in charges:
        order_id = charge["recharge_order_id"]
        if order_id is None:
            charges_without_order += 1
        else:
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
    missing_order_count = charges_without_order + len(orphan_charges)
    if missing_order_count:
        issues.append(
            ReconciliationIssue(
                code="charge_without_order",
                scope=f"{side}:wallet_transactions",
                detail=f"CHARGE rows reference missing orders: {missing_order_count}",
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
    rows = _all_mappings(
        conn.execute(
            """
            SELECT task_id, billing_round, type, user_id
            FROM wallet_transactions
            WHERE task_id IS NOT NULL AND billing_round IS NOT NULL
            """
        )
    )
    owners = _all_mappings(
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


def _is_json_asset_column(column: str) -> bool:
    return column in JSON_ASSET_ID_COLUMNS or column.endswith("_asset_ids_json")


def _extract_asset_ids(value: object) -> tuple[set[str], bool]:
    if value is None or value == "":
        return set(), True
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError, json.JSONDecodeError):
        return set(), False

    identifiers: set[str] = set()

    def visit(item: object) -> None:
        if isinstance(item, str):
            identifiers.add(item)
        elif isinstance(item, Mapping):
            for nested in item.values():
                visit(nested)
        elif isinstance(item, (list, tuple, set)):
            for nested in item:
                visit(nested)

    visit(parsed)
    return identifiers, True


def _asset_ids_sqlite(conn: sqlite3.Connection) -> set[str]:
    cursor = conn.execute("SELECT id FROM assets")
    return {str(row["id"] if isinstance(row, sqlite3.Row) else row[0]) for row in cursor}


def _asset_ids_pg(conn: psycopg.Connection[Any]) -> set[str]:
    cursor = conn.execute("SELECT id FROM assets")
    return {str(_row_value(row, "id", 0)) for row in cursor}


def _asset_reference_issues_sqlite(
    conn: sqlite3.Connection, side: str
) -> list[ReconciliationIssue]:
    tables = _table_names(conn, "sqlite")
    if "assets" not in tables:
        return []
    asset_ids = _asset_ids_sqlite(conn)
    issues: list[ReconciliationIssue] = []
    for table in tables:
        if table == "assets":
            continue
        columns, _ = _sqlite_columns(conn, table)
        for column in columns:
            if column == "asset_id" or column.endswith("_asset_id"):
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
            if _is_json_asset_column(column):
                query = (
                    f"SELECT {_quote_sqlite_identifier(column)} "
                    f"FROM {_quote_sqlite_identifier(table)} "
                    f"WHERE {_quote_sqlite_identifier(column)} IS NOT NULL"
                )
                orphan_count = 0
                invalid_json_count = 0
                cursor = conn.execute(query)
                while True:
                    rows = cursor.fetchmany(1000)
                    if not rows:
                        break
                    for row in rows:
                        value = row[column] if isinstance(row, sqlite3.Row) else row[0]
                        references, valid = _extract_asset_ids(value)
                        if not valid:
                            invalid_json_count += 1
                        else:
                            orphan_count += len(references - asset_ids)
                if invalid_json_count:
                    issues.append(
                        ReconciliationIssue(
                            code="asset_reference_json_invalid",
                            scope=f"{side}:{table}.{column}",
                            detail=f"invalid JSON rows={invalid_json_count}",
                        )
                    )
                if orphan_count:
                    issues.append(
                        ReconciliationIssue(
                            code="asset_reference_orphan",
                            scope=f"{side}:{table}.{column}",
                            detail=f"JSON asset reference orphan count={orphan_count}",
                        )
                    )
    return issues


def _asset_reference_issues_pg(
    conn: psycopg.Connection[Any], side: str
) -> list[ReconciliationIssue]:
    tables = _table_names(conn, "postgresql")
    if "assets" not in tables:
        return []
    asset_ids = _asset_ids_pg(conn)
    issues: list[ReconciliationIssue] = []
    for table in tables:
        if table == "assets":
            continue
        columns, _, _ = _pg_columns(conn, table)
        for column in columns:
            if column == "asset_id" or column.endswith("_asset_id"):
                query = sql.SQL(
                    "SELECT COUNT(*) AS orphan_count FROM {} AS child "
                    "LEFT JOIN assets AS parent ON child.{} = parent.id "
                    "WHERE child.{} IS NOT NULL AND parent.id IS NULL"
                ).format(sql.Identifier(table), sql.Identifier(column), sql.Identifier(column))
                row = conn.execute(query).fetchone()
                if row is None:
                    raise RuntimeError("asset reconciliation count query returned no row")
                count = int(_row_value(row, "orphan_count", 0))
                if count:
                    issues.append(
                        ReconciliationIssue(
                            code="asset_reference_orphan",
                            scope=f"{side}:{table}.{column}",
                            detail=f"asset reference orphan count={count}",
                        )
                    )
            if _is_json_asset_column(column):
                query = sql.SQL("SELECT {} FROM {} WHERE {} IS NOT NULL").format(
                    sql.Identifier(column), sql.Identifier(table), sql.Identifier(column)
                )
                cursor = conn.cursor(name=f"t07_asset_{uuid4().hex}", row_factory=dict_row)
                cursor.execute(query)
                orphan_count = 0
                invalid_json_count = 0
                try:
                    while True:
                        rows = cursor.fetchmany(1000)
                        if not rows:
                            break
                        for row in rows:
                            references, valid = _extract_asset_ids(row[column])
                            if not valid:
                                invalid_json_count += 1
                            else:
                                orphan_count += len(references - asset_ids)
                finally:
                    cursor.close()
                if invalid_json_count:
                    issues.append(
                        ReconciliationIssue(
                            code="asset_reference_json_invalid",
                            scope=f"{side}:{table}.{column}",
                            detail=f"invalid JSON rows={invalid_json_count}",
                        )
                    )
                if orphan_count:
                    issues.append(
                        ReconciliationIssue(
                            code="asset_reference_orphan",
                            scope=f"{side}:{table}.{column}",
                            detail=f"JSON asset reference orphan count={orphan_count}",
                        )
                    )
    return issues


def validate_asset_references_sqlite(conn: sqlite3.Connection) -> AssetReferenceResult:
    issues = _asset_reference_issues_sqlite(conn, "source")
    orphan_count = sum(
        int(issue.detail.rsplit("=", 1)[-1])
        for issue in issues
        if issue.code == "asset_reference_orphan"
    )
    invalid_count = sum(
        int(issue.detail.rsplit("=", 1)[-1])
        for issue in issues
        if issue.code == "asset_reference_json_invalid"
    )
    return AssetReferenceResult(
        orphan_reference_count=orphan_count,
        invalid_json_count=invalid_count,
    )


def validate_wallet_invariants(
    conn: sqlite3.Connection | psycopg.Connection[Any],
    dialect: Dialect | None = None,
) -> WalletInvariantResult:
    resolved: Dialect = dialect or (
        "sqlite" if isinstance(conn, sqlite3.Connection) else "postgresql"
    )
    issues = [
        *_wallet_issues(conn, resolved, "source"),
        *_paid_charge_issues(conn, resolved, "source"),
        *_generation_billing_issues(conn, resolved, "source"),
    ]
    wallet_mismatches = sum(issue.code == "wallet_balance_mismatch" for issue in issues)
    paid_without_charge = sum(issue.code == "paid_order_charge_mismatch" for issue in issues)
    return WalletInvariantResult(
        wallet_balance_mismatches=wallet_mismatches,
        paid_without_charge=paid_without_charge,
        other_issue_count=len(issues) - wallet_mismatches - paid_without_charge,
    )


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


def reconcile_connections(
    sqlite_conn: sqlite3.Connection,
    pg_conn: psycopg.Connection[Any],
    *,
    source_snapshot_sha256: str | None = None,
    target_dsn: str | None = None,
) -> ReconciliationReport:
    del source_snapshot_sha256, target_dsn
    return reconcile_connection_pair(sqlite_conn, pg_conn)


def reconcile_databases(
    sqlite_path: str | Path,
    postgres_dsn: str,
) -> ReconciliationReport:
    with connect_sqlite_readonly(sqlite_path) as sqlite_conn:
        with psycopg.connect(postgres_dsn, row_factory=dict_row) as pg_conn:
            return reconcile_connection_pair(sqlite_conn, pg_conn)


def redact_postgres_dsn(dsn: str) -> str:
    parsed = urlsplit(dsn)
    if not parsed.scheme:
        return "<redacted-postgres-dsn>"
    host = parsed.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    port = "" if parsed.port is None else f":{parsed.port}"
    username = "" if parsed.username is None else f"{parsed.username}@"
    return urlunsplit((parsed.scheme, f"{username}{host}{port}", parsed.path, parsed.query, parsed.fragment))


def safe_error_message(error: BaseException, postgres_dsn: str) -> str:
    message = str(error)
    parsed = urlsplit(postgres_dsn)
    secrets_to_remove = {postgres_dsn}
    if parsed.password:
        secrets_to_remove.add(parsed.password)
        secrets_to_remove.add(unquote(parsed.password))
    for secret in sorted(secrets_to_remove, key=len, reverse=True):
        if secret:
            message = message.replace(secret, "<redacted>")
    return message.replace(postgres_dsn, redact_postgres_dsn(postgres_dsn))


def main() -> None:
    parser = argparse.ArgumentParser(description="Reconcile a legacy SQLite snapshot with PG")
    parser.add_argument("--sqlite", required=True, help="read-only SQLite snapshot path")
    parser.add_argument("--postgres-url", required=True, help="PostgreSQL DSN (never printed)")
    parser.add_argument("--output", help="optional JSON report path")
    args = parser.parse_args()

    try:
        report = reconcile_databases(args.sqlite, args.postgres_url)
    except Exception as error:
        raise SystemExit(safe_error_message(error, args.postgres_url)) from error
    payload = json.dumps(report.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(payload + "\n", encoding="utf-8")
    print(payload)
    raise SystemExit(0 if report.ok else 1)


if __name__ == "__main__":
    main()
