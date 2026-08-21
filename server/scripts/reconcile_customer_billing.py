"""T07 reconciliation for the one-shot SQLite-to-PostgreSQL cutover.

Reports contain counts and SHA-256 digests only. Raw business values,
credentials, storage URLs, activation codes, and tokens are never emitted.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
from collections import defaultdict
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qsl, quote, unquote, urlsplit, urlunsplit
from uuid import UUID, uuid4

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

Dialect = Literal["sqlite", "postgresql"]
EXCLUDED_TABLES = frozenset({"alembic_version"})
DEFAULT_DIGEST_BATCH_SIZE = 1000
_DIGEST_MODULUS = 1 << 256


@dataclass(frozen=True)
class ColumnSpec:
    name: str
    target_type: str
    nullable: bool = True


@dataclass(frozen=True)
class TableDigest:
    row_count: int
    primary_key_sha256: str | None
    row_sha256: str


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


class _UnorderedDigest:
    """Bounded-memory multiset digest.

    Each canonical row is SHA-256 hashed, then accumulated with independent
    commutative moments. The final SHA-256 binds count, sum, xor, and squared
    sum. This keeps memory constant while remaining insensitive to row order
    and sensitive to duplicate multiplicity.
    """

    __slots__ = ("count", "total", "xor", "squares")

    def __init__(self) -> None:
        self.count = 0
        self.total = 0
        self.xor = 0
        self.squares = 0

    def update(self, payload: bytes) -> None:
        value = int.from_bytes(hashlib.sha256(payload).digest(), "big")
        self.count += 1
        self.total = (self.total + value) % _DIGEST_MODULUS
        self.xor ^= value
        self.squares = (self.squares + value * value) % _DIGEST_MODULUS

    def hexdigest(self, domain: bytes) -> str:
        digest = hashlib.sha256()
        digest.update(domain)
        digest.update(self.count.to_bytes(16, "big"))
        digest.update(self.total.to_bytes(32, "big"))
        digest.update(self.xor.to_bytes(32, "big"))
        digest.update(self.squares.to_bytes(32, "big"))
        return digest.hexdigest()


def redact_postgres_dsn(dsn: str) -> str:
    "Remove credentials and optional DSN parameters before logging."

    try:
        parts = urlsplit(dsn)
        if not parts.scheme.startswith("postgres"):
            return "<redacted-postgres-dsn>"
        hostname = parts.hostname or ""
        port = parts.port
        username = parts.username
    except (UnicodeError, ValueError):
        return "<redacted-postgres-dsn>"
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    netloc = hostname
    if port is not None:
        netloc += f":{port}"
    if username:
        netloc = f"{quote(unquote(username), safe='')}@{netloc}"
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def _dsn_sensitive_values(dsn: str) -> set[str]:
    candidates = {dsn}
    try:
        parts = urlsplit(dsn)
    except ValueError:
        return candidates
    for value in (parts.username, parts.password, parts.fragment):
        if value:
            decoded = unquote(value)
            candidates.update({value, decoded, quote(decoded, safe="")})
    try:
        query_pairs = parse_qsl(parts.query, keep_blank_values=True, strict_parsing=False)
    except ValueError:
        query_pairs = []
    for key, value in query_pairs:
        if key:
            decoded_key = unquote(key)
            candidates.update({key, decoded_key, quote(decoded_key, safe="")})
        if value:
            decoded_value = unquote(value)
            candidates.update({value, decoded_value, quote(decoded_value, safe="")})
    return candidates


def safe_error_message(error: Exception, dsn: str) -> str:
    message = str(error)
    for candidate in sorted(
        (item for item in _dsn_sensitive_values(dsn) if item),
        key=len,
        reverse=True,
    ):
        message = message.replace(candidate, "<redacted>")
    return message


def _quote_sqlite_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def connect_sqlite_readonly(path: str | Path) -> sqlite3.Connection:
    database = Path(path)
    if not database.is_file():
        raise FileNotFoundError(database)
    conn = sqlite3.connect(
        f"{database.resolve().as_uri()}?mode=ro&immutable=1",
        uri=True,
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


_open_sqlite_readonly = connect_sqlite_readonly


def _row_value(row: object, key: str, index: int = 0) -> object:
    if isinstance(row, sqlite3.Row):
        return row[key]
    if isinstance(row, Mapping):
        return row[key]
    return row[index]  # type: ignore[index]


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
            str(_row_value(row, "name"))
            for row in rows
            if str(_row_value(row, "name")) not in EXCLUDED_TABLES
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
        str(_row_value(row, "table_name"))
        for row in rows
        if str(_row_value(row, "table_name")) not in EXCLUDED_TABLES
    ]


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
        SELECT column_name, data_type, udt_name, is_nullable
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = %s
        ORDER BY ordinal_position
        """,
        (table,),
    ).fetchall()
    columns = [str(_row_value(row, "column_name")) for row in rows]
    types = {
        str(_row_value(row, "column_name")): str(
            _row_value(row, "udt_name", 2) or _row_value(row, "data_type", 1)
        ).lower()
        for row in rows
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
    primary_key = [str(_row_value(row, "column_name")) for row in pk_rows]
    return columns, primary_key, types


def canonical_value(value: object, target_type: str | None) -> object:
    if value is None:
        return None
    normalized_type = None if target_type is None else target_type.lower()
    if isinstance(value, memoryview):
        value = bytes(value)
    if normalized_type == "bytea" or isinstance(value, bytes):
        raw = value if isinstance(value, bytes) else bytes(value)
        return {"bytes_sha256": hashlib.sha256(raw).hexdigest(), "size": len(raw)}
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
        number = value if isinstance(value, Decimal) else Decimal(str(value))
        if not number.is_finite():
            return str(number)
        if number == 0:
            return "0"
        return format(number.normalize(), "f")
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
        parsed_datetime = (
            value
            if isinstance(value, datetime)
            else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        )
        return parsed_datetime.isoformat()
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
        return canonical_value(value, "numeric")
    if isinstance(value, float):
        return canonical_value(value, "float8")
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, (dict, list, tuple)):
        return _canonical_json(value)
    return str(value) if not isinstance(value, str) else value


_canonical_value = canonical_value


def _canonical_json(value: object) -> object:
    if isinstance(value, Mapping):
        ordered = sorted(value.items(), key=lambda item: str(item[0]))
        return {str(key): _canonical_json(item) for key, item in ordered}
    if isinstance(value, (list, tuple)):
        return [_canonical_json(item) for item in value]
    return canonical_value(value, None)


def _row_payload(row: Mapping[str, object], columns: Sequence[ColumnSpec]) -> bytes:
    values = [canonical_value(row[column.name], column.target_type) for column in columns]
    return json.dumps(
        values,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _pk_payload(
    row: Mapping[str, object], columns: Mapping[str, ColumnSpec], primary_key: Sequence[str]
) -> bytes:
    values = [canonical_value(row[name], columns[name].target_type) for name in primary_key]
    return json.dumps(
        values,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _iter_sqlite_rows(
    conn: sqlite3.Connection,
    table: str,
    columns: Sequence[str],
    batch_size: int,
) -> Iterator[dict[str, object]]:
    names = ", ".join(_quote_sqlite_identifier(column) for column in columns)
    cursor = conn.execute(f"SELECT {names} FROM {_quote_sqlite_identifier(table)}")
    while True:
        rows = cursor.fetchmany(batch_size)
        if not rows:
            return
        for row in rows:
            yield {key: row[key] for key in row.keys()}


def _iter_pg_rows(
    conn: psycopg.Connection[Any],
    table: str,
    columns: Sequence[str],
    batch_size: int,
) -> Iterator[dict[str, object]]:
    query = sql.SQL("SELECT {} FROM {}").format(
        sql.SQL(", ").join(sql.Identifier(column) for column in columns),
        sql.Identifier(table),
    )
    cursor_name = f"t07_reconcile_{uuid4().hex}"
    with conn.cursor(name=cursor_name, row_factory=dict_row) as cursor:
        cursor.itersize = batch_size
        cursor.execute(query)
        while True:
            rows = cursor.fetchmany(batch_size)
            if not rows:
                return
            for row in rows:
                yield dict(row)


def compute_table_digest(
    conn: sqlite3.Connection | psycopg.Connection[Any],
    table: str,
    columns: Sequence[ColumnSpec],
    primary_key: Sequence[str],
    *,
    dialect: Dialect | None = None,
    batch_size: int = DEFAULT_DIGEST_BATCH_SIZE,
) -> TableDigest:
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    selected_dialect: Dialect = (
        dialect
        if dialect is not None
        else ("sqlite" if isinstance(conn, sqlite3.Connection) else "postgresql")
    )
    names = [column.name for column in columns]
    by_name = {column.name: column for column in columns}
    missing_pk = set(primary_key) - set(by_name)
    if missing_pk:
        raise ValueError(f"primary-key columns absent from digest specification: {len(missing_pk)}")

    rows: Iterable[dict[str, object]]
    if selected_dialect == "sqlite":
        assert isinstance(conn, sqlite3.Connection)
        rows = _iter_sqlite_rows(conn, table, names, batch_size)
    else:
        rows = _iter_pg_rows(conn, table, names, batch_size)  # type: ignore[arg-type]

    row_digest = _UnorderedDigest()
    pk_digest = _UnorderedDigest() if primary_key else None
    for row in rows:
        row_digest.update(_row_payload(row, columns))
        if pk_digest is not None:
            pk_digest.update(_pk_payload(row, by_name, primary_key))
    return TableDigest(
        row_count=row_digest.count,
        primary_key_sha256=(
            None if pk_digest is None else pk_digest.hexdigest(b"t07-primary-key-v1")
        ),
        row_sha256=row_digest.hexdigest(b"t07-canonical-row-v1"),
    )


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

    specs = tuple(ColumnSpec(name, target_types[name]) for name in source_columns)
    source = compute_table_digest(
        sqlite_conn,
        table,
        specs,
        source_pk,
        dialect="sqlite",
    )
    target = compute_table_digest(
        pg_conn,
        table,
        specs,
        source_pk,
        dialect="postgresql",
    )
    result = TableReconciliation(
        table=table,
        source_count=source.row_count,
        target_count=target.row_count,
        source_pk_sha256=source.primary_key_sha256,
        target_pk_sha256=target.primary_key_sha256,
        source_rows_sha256=source.row_sha256,
        target_rows_sha256=target.row_sha256,
    )
    if source.row_count != target.row_count:
        issues.append(
            ReconciliationIssue(
                code="table_row_count_mismatch",
                scope=table,
                detail=f"source={source.row_count} target={target.row_count}",
            )
        )
    if source.primary_key_sha256 != target.primary_key_sha256:
        issues.append(
            ReconciliationIssue(
                code="table_primary_key_mismatch",
                scope=table,
                detail="primary-key multiset SHA-256 differs",
            )
        )
    if source.row_sha256 != target.row_sha256:
        issues.append(
            ReconciliationIssue(
                code="table_hash_mismatch",
                scope=table,
                detail="canonical row multiset SHA-256 differs",
            )
        )
    return result, issues


def _mapping_rows(cursor: Any) -> list[dict[str, object]]:
    rows = cursor.fetchall()
    result: list[dict[str, object]] = []
    for row in rows:
        if isinstance(row, sqlite3.Row):
            result.append({key: row[key] for key in row.keys()})
        elif isinstance(row, Mapping):
            result.append(dict(row))
        else:
            raise TypeError("database connection must return mapping rows")
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
    wallets = _mapping_rows(
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
    orders = _mapping_rows(conn.execute("SELECT id, user_id, status, credits FROM recharge_orders"))
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
    for rounds in rounds_by_task.values():
        expected = set(range(1, max(rounds) + 1))
        if rounds != expected:
            issues.append(
                ReconciliationIssue(
                    code="generation_billing_round_gap",
                    scope=f"{side}:wallet_transactions",
                    detail=f"billing rounds are not contiguous for 1 task ({len(rounds)} rounds)",
                )
            )
    return issues


def _asset_ids_sqlite(conn: sqlite3.Connection) -> set[str]:
    return {str(row[0]) for row in conn.execute("SELECT id FROM assets").fetchall()}


def _asset_ids_pg(conn: psycopg.Connection[Any]) -> set[str]:
    return {str(_row_value(row, "id")) for row in conn.execute("SELECT id FROM assets").fetchall()}


def _parse_asset_id_list(raw: object) -> tuple[list[str], bool]:
    if raw is None:
        return [], False
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, json.JSONDecodeError):
        return [], True
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        return [], True
    return value, False


def _json_asset_reference_issues_sqlite(
    conn: sqlite3.Connection,
    tables: Sequence[str],
    known_assets: set[str],
    side: str,
) -> list[ReconciliationIssue]:
    issues: list[ReconciliationIssue] = []
    for table in tables:
        columns, _ = _sqlite_columns(conn, table)
        for column in (name for name in columns if name.endswith("_asset_ids_json")):
            query = (
                f"SELECT {_quote_sqlite_identifier(column)} FROM {_quote_sqlite_identifier(table)}"
            )
            invalid = 0
            orphan = 0
            cursor = conn.execute(query)
            while True:
                rows = cursor.fetchmany(DEFAULT_DIGEST_BATCH_SIZE)
                if not rows:
                    break
                for row in rows:
                    asset_ids, malformed = _parse_asset_id_list(row[0])
                    invalid += int(malformed)
                    orphan += sum(asset_id not in known_assets for asset_id in asset_ids)
            if invalid:
                issues.append(
                    ReconciliationIssue(
                        code="asset_reference_json_invalid",
                        scope=f"{side}:{table}.{column}",
                        detail=f"invalid asset reference JSON rows={invalid}",
                    )
                )
            if orphan:
                issues.append(
                    ReconciliationIssue(
                        code="asset_reference_orphan",
                        scope=f"{side}:{table}.{column}",
                        detail=f"asset reference orphan count={orphan}",
                    )
                )
    return issues


def _json_asset_reference_issues_pg(
    conn: psycopg.Connection[Any],
    tables: Sequence[str],
    known_assets: set[str],
    side: str,
) -> list[ReconciliationIssue]:
    issues: list[ReconciliationIssue] = []
    for table in tables:
        columns, _, _ = _pg_columns(conn, table)
        for column in (name for name in columns if name.endswith("_asset_ids_json")):
            query = sql.SQL("SELECT {} FROM {}").format(
                sql.Identifier(column), sql.Identifier(table)
            )
            invalid = 0
            orphan = 0
            cursor_name = f"t07_asset_json_{uuid4().hex}"
            with conn.cursor(name=cursor_name) as cursor:
                cursor.itersize = DEFAULT_DIGEST_BATCH_SIZE
                cursor.execute(query)
                while True:
                    rows = cursor.fetchmany(DEFAULT_DIGEST_BATCH_SIZE)
                    if not rows:
                        break
                    for row in rows:
                        asset_ids, malformed = _parse_asset_id_list(_row_value(row, column, 0))
                        invalid += int(malformed)
                        orphan += sum(asset_id not in known_assets for asset_id in asset_ids)
            if invalid:
                issues.append(
                    ReconciliationIssue(
                        code="asset_reference_json_invalid",
                        scope=f"{side}:{table}.{column}",
                        detail=f"invalid asset reference JSON rows={invalid}",
                    )
                )
            if orphan:
                issues.append(
                    ReconciliationIssue(
                        code="asset_reference_orphan",
                        scope=f"{side}:{table}.{column}",
                        detail=f"asset reference orphan count={orphan}",
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
    issues.extend(_json_asset_reference_issues_sqlite(conn, tables, _asset_ids_sqlite(conn), side))
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
                "SELECT COUNT(*) AS orphan_count FROM {} AS child "
                "LEFT JOIN assets AS parent ON child.{} = parent.id "
                "WHERE child.{} IS NOT NULL AND parent.id IS NULL"
            ).format(sql.Identifier(table), sql.Identifier(column), sql.Identifier(column))
            row = conn.execute(query).fetchone()
            if row is None:
                raise RuntimeError("asset reconciliation count query returned no row")
            count = int(_row_value(row, "orphan_count"))
            if count:
                issues.append(
                    ReconciliationIssue(
                        code="asset_reference_orphan",
                        scope=f"{side}:{table}.{column}",
                        detail=f"asset reference orphan count={count}",
                    )
                )
    issues.extend(_json_asset_reference_issues_pg(conn, tables, _asset_ids_pg(conn), side))
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
    with closing(connect_sqlite_readonly(sqlite_path)) as sqlite_conn:
        with psycopg.connect(postgres_dsn, row_factory=dict_row) as pg_conn:
            return reconcile_connection_pair(sqlite_conn, pg_conn)


def main() -> None:
    parser = argparse.ArgumentParser(description="Reconcile a legacy SQLite snapshot with PG")
    parser.add_argument("--sqlite", required=True, help="read-only SQLite snapshot path")
    parser.add_argument("--postgres-url", required=True, help="PostgreSQL DSN (never printed)")
    parser.add_argument("--output", help="optional JSON report path")
    args = parser.parse_args()

    try:
        report = reconcile_databases(args.sqlite, args.postgres_url)
    except Exception as error:
        parser.exit(1, safe_error_message(error, args.postgres_url) + "\n")
    payload = json.dumps(report.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(payload + "\n", encoding="utf-8")
    print(payload)
    raise SystemExit(0 if report.ok else 1)


if __name__ == "__main__":
    main()
