"""One-shot, fail-closed SQLite-to-PostgreSQL migration for T07 / DB-05.

The importer is intentionally not a dual-write bridge. It takes a read-only
SQLite snapshot during a confirmed maintenance window, imports all data in one
PostgreSQL transaction, reconciles counts/primary keys/hashes/billing/assets,
and commits only when every check passes.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import defaultdict, deque
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

import psycopg
from alembic.config import Config
from alembic.script import ScriptDirectory
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Json, Jsonb

from scripts.reconcile_customer_billing import (
    ReconciliationIssue,
    ReconciliationReport,
    _open_sqlite_readonly,
    _pg_columns,
    _sqlite_columns,
    _table_names,
    reconcile_connection_pair,
    validate_database_invariants,
)

from app.backup import SqliteSnapshot, create_readonly_snapshot

ImportStatus = Literal["imported", "already_reconciled"]
SERVER_DIR = Path(__file__).resolve().parent.parent
SEED_TABLES = frozenset({"runtime_settings"})


class MigrationPreconditionError(RuntimeError):
    """The source/target is not safe to migrate."""


class MigrationReconciliationError(RuntimeError):
    """The imported database failed one or more fail-closed checks."""


@dataclass(frozen=True)
class ImportedTable:
    table: str
    source_rows: int

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class ImportResult:
    status: ImportStatus
    snapshot_path: str
    source_sha256: str
    snapshot_sha256: str
    tables: tuple[ImportedTable, ...]
    reconciliation: ReconciliationReport

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "snapshot_path": self.snapshot_path,
            "source_sha256": self.source_sha256,
            "snapshot_sha256": self.snapshot_sha256,
            "tables": [table.to_dict() for table in self.tables],
            "reconciliation": self.reconciliation.to_dict(),
        }


def _alembic_script() -> ScriptDirectory:
    config = Config(str(SERVER_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(SERVER_DIR / "migrations"))
    return ScriptDirectory.from_config(config)


def _release_head_revision() -> str:
    head = _alembic_script().get_current_head()
    if head is None:
        raise MigrationPreconditionError("Alembic has no single migration head")
    return head


def _sqlite_revision(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
    if row is None:
        raise MigrationPreconditionError("source Alembic revision is missing")
    return str(row[0])


def _pg_revision(conn: psycopg.Connection[Any]) -> str:
    row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
    if row is None:
        raise MigrationPreconditionError("target Alembic revision is missing")
    return str(row["version_num"])


def _validate_revisions(
    sqlite_conn: sqlite3.Connection,
    pg_conn: psycopg.Connection[Any],
) -> None:
    expected_head = _release_head_revision()
    source_revision = _sqlite_revision(sqlite_conn)
    target_revision = _pg_revision(pg_conn)
    if source_revision != expected_head:
        raise MigrationPreconditionError(
            "source Alembic revision must match the release head before import "
            f"(expected {expected_head}, found {source_revision})"
        )
    if target_revision != expected_head:
        raise MigrationPreconditionError(
            "target PostgreSQL must be upgraded to the release Alembic head before import "
            f"(expected {expected_head}, found {target_revision})"
        )


def _validate_schema(
    sqlite_conn: sqlite3.Connection,
    pg_conn: psycopg.Connection[Any],
) -> list[str]:
    source_tables = _table_names(sqlite_conn, "sqlite")
    target_tables = set(_table_names(pg_conn, "postgresql"))
    missing = sorted(set(source_tables) - target_tables)
    extra = sorted(target_tables - set(source_tables))
    if missing or extra:
        raise MigrationPreconditionError(
            "source/target table contract differs "
            f"(missing={len(missing)}, extra={len(extra)})"
        )
    for table in source_tables:
        source_columns, source_pk = _sqlite_columns(sqlite_conn, table)
        target_columns, target_pk, _ = _pg_columns(pg_conn, table)
        if set(source_columns) != set(target_columns) or source_pk != target_pk:
            raise MigrationPreconditionError(
                f"source/target schema differs for table {table!r}"
            )
    return source_tables


def _issue_summary(issues: Sequence[ReconciliationIssue]) -> str:
    codes = sorted({issue.code for issue in issues})
    if "asset_reference_orphan" in codes:
        return "asset reference reconciliation failed (asset_reference_orphan)"
    return "database reconciliation failed (" + ", ".join(codes[:8]) + ")"


def _ensure_source_invariants(sqlite_conn: sqlite3.Connection) -> None:
    issues = validate_database_invariants(sqlite_conn, "sqlite", "source")
    if issues:
        raise MigrationReconciliationError(_issue_summary(issues))


def _target_has_non_seed_data(
    pg_conn: psycopg.Connection[Any], tables: Sequence[str]
) -> bool:
    for table in tables:
        if table in SEED_TABLES:
            continue
        count_query = sql.SQL("SELECT COUNT(*) AS count FROM {}").format(sql.Identifier(table))
        count = int(pg_conn.execute(count_query).fetchone()["count"])
        if count:
            return True
    return False


def _dependency_order(sqlite_conn: sqlite3.Connection, tables: Sequence[str]) -> list[str]:
    nodes = set(tables)
    dependencies: dict[str, set[str]] = {table: set() for table in tables}
    dependants: dict[str, set[str]] = defaultdict(set)
    for table in tables:
        escaped = '"' + table.replace('"', '""') + '"'
        for row in sqlite_conn.execute(f"PRAGMA foreign_key_list({escaped})").fetchall():
            parent = str(row[2])
            if parent in nodes and parent != table:
                dependencies[table].add(parent)
                dependants[parent].add(table)
    ready = deque(sorted(table for table, deps in dependencies.items() if not deps))
    ordered: list[str] = []
    while ready:
        table = ready.popleft()
        ordered.append(table)
        for dependant in sorted(dependants.get(table, set())):
            dependencies[dependant].discard(table)
            if not dependencies[dependant] and dependant not in ordered and dependant not in ready:
                ready.append(dependant)
    if len(ordered) != len(tables):
        blocked = sorted(set(tables) - set(ordered))
        raise MigrationPreconditionError(
            f"foreign-key dependency cycle prevents safe import: {len(blocked)} tables"
        )
    return ordered


def _convert_value(value: object, target_type: str) -> object:
    if value is None:
        return None
    normalized_type = target_type.lower()
    if normalized_type in {"text", "varchar", "bpchar", "name"}:
        return str(value)
    if normalized_type in {"int2", "int4", "int8", "smallint", "integer", "bigint"}:
        return int(value)
    if normalized_type in {"float4", "float8", "real", "double precision"}:
        return float(value)
    if normalized_type in {"numeric", "decimal"}:
        return Decimal(str(value))
    if normalized_type in {"bool", "boolean"}:
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)
    if normalized_type == "bytea":
        if isinstance(value, memoryview):
            return bytes(value)
        return value if isinstance(value, bytes) else bytes(value)
    if normalized_type == "uuid":
        return value if isinstance(value, UUID) else UUID(str(value))
    if normalized_type in {"json", "jsonb"}:
        parsed = json.loads(value) if isinstance(value, str) else value
        return Jsonb(parsed) if normalized_type == "jsonb" else Json(parsed)
    if normalized_type in {"date"} and isinstance(value, str):
        return date.fromisoformat(value)
    if normalized_type in {"timestamp", "timestamptz"} and isinstance(value, str):
        return datetime.fromisoformat(value)
    return value


def _sqlite_row_batches(
    sqlite_conn: sqlite3.Connection,
    table: str,
    columns: Sequence[str],
    batch_size: int,
) -> Iterator[list[sqlite3.Row]]:
    escaped_table = '"' + table.replace('"', '""') + '"'
    escaped_columns = ", ".join('"' + column.replace('"', '""') + '"' for column in columns)
    cursor = sqlite_conn.execute(f"SELECT {escaped_columns} FROM {escaped_table}")
    while True:
        rows = cursor.fetchmany(batch_size)
        if not rows:
            return
        yield rows


def _insert_table_rows(
    sqlite_conn: sqlite3.Connection,
    pg_conn: psycopg.Connection[Any],
    table: str,
    columns: Sequence[str],
    primary_key: Sequence[str],
    target_types: Mapping[str, str],
    *,
    batch_size: int = 1000,
) -> int:
    placeholders = sql.SQL(", ").join(sql.Placeholder() for _ in columns)
    base = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
        sql.Identifier(table),
        sql.SQL(", ").join(sql.Identifier(column) for column in columns),
        placeholders,
    )
    if table in SEED_TABLES:
        if not primary_key:
            raise MigrationPreconditionError(f"seed table {table!r} has no primary key")
        update_columns = [column for column in columns if column not in primary_key]
        if update_columns:
            updates = sql.SQL(", ").join(
                sql.SQL("{} = EXCLUDED.{}").format(
                    sql.Identifier(column), sql.Identifier(column)
                )
                for column in update_columns
            )
            query = base + sql.SQL(" ON CONFLICT ({}) DO UPDATE SET {}").format(
                sql.SQL(", ").join(sql.Identifier(column) for column in primary_key), updates
            )
        else:
            query = base + sql.SQL(" ON CONFLICT DO NOTHING")
    else:
        query = base

    total = 0
    for rows in _sqlite_row_batches(sqlite_conn, table, columns, batch_size):
        parameters = [
            tuple(_convert_value(row[column], target_types[column]) for column in columns)
            for row in rows
        ]
        pg_conn.executemany(query, parameters)
        total += len(rows)
    return total


def _reset_sequences(pg_conn: psycopg.Connection[Any], tables: Sequence[str]) -> None:
    rows = pg_conn.execute(
        """
        SELECT table_name, column_name
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND (is_identity = 'YES' OR column_default LIKE 'nextval(%')
        ORDER BY table_name, ordinal_position
        """
    ).fetchall()
    allowed = set(tables)
    for row in rows:
        table = str(row["table_name"])
        column = str(row["column_name"])
        if table not in allowed:
            continue
        sequence_row = pg_conn.execute(
            "SELECT pg_get_serial_sequence(%s, %s) AS sequence_name", (table, column)
        ).fetchone()
        sequence = None if sequence_row is None else sequence_row["sequence_name"]
        if not sequence:
            continue
        maximum_query = sql.SQL("SELECT MAX({}) AS maximum FROM {}").format(
            sql.Identifier(column), sql.Identifier(table)
        )
        maximum = pg_conn.execute(maximum_query).fetchone()["maximum"]
        if maximum is None:
            pg_conn.execute("SELECT setval(%s, 1, false)", (sequence,))
        else:
            pg_conn.execute("SELECT setval(%s, %s, true)", (sequence, int(maximum)))


def _default_snapshot_path(source: Path) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return source.with_name(f"{source.stem}.t07-snapshot-{stamp}{source.suffix or '.db'}")


def _write_report(path: str | Path | None, result: ImportResult) -> None:
    if path is None:
        return
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(result.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _result(
    status: ImportStatus,
    snapshot: SqliteSnapshot,
    tables: Sequence[ImportedTable],
    reconciliation: ReconciliationReport,
) -> ImportResult:
    return ImportResult(
        status=status,
        snapshot_path=str(snapshot.path),
        source_sha256=snapshot.source_sha256,
        snapshot_sha256=snapshot.snapshot_sha256,
        tables=tuple(tables),
        reconciliation=reconciliation,
    )


def import_sqlite_to_postgres(
    source_path: str | Path,
    postgres_dsn: str,
    *,
    snapshot_path: str | Path | None = None,
    report_path: str | Path | None = None,
    batch_size: int = 1000,
) -> ImportResult:
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    source = Path(source_path)
    snapshot = create_readonly_snapshot(source, snapshot_path or _default_snapshot_path(source))

    with _open_sqlite_readonly(snapshot.path) as sqlite_conn:
        _ensure_source_invariants(sqlite_conn)
        with psycopg.connect(postgres_dsn, row_factory=dict_row) as pg_conn:
            with pg_conn.transaction():
                _validate_revisions(sqlite_conn, pg_conn)
                tables = _validate_schema(sqlite_conn, pg_conn)

                current = reconcile_connection_pair(sqlite_conn, pg_conn)
                if current.ok:
                    result = _result("already_reconciled", snapshot, (), current)
                    _write_report(report_path, result)
                    return result
                if _target_has_non_seed_data(pg_conn, tables):
                    raise MigrationPreconditionError(
                        "target PostgreSQL contains non-seed data and does not reconcile exactly; "
                        "refusing to overwrite or merge divergent state"
                    )

                imported: list[ImportedTable] = []
                for table in _dependency_order(sqlite_conn, tables):
                    columns, primary_key = _sqlite_columns(sqlite_conn, table)
                    _, _, target_types = _pg_columns(pg_conn, table)
                    source_rows = _insert_table_rows(
                        sqlite_conn,
                        pg_conn,
                        table,
                        columns,
                        primary_key,
                        target_types,
                        batch_size=batch_size,
                    )
                    imported.append(ImportedTable(table=table, source_rows=source_rows))
                _reset_sequences(pg_conn, tables)

                reconciliation = reconcile_connection_pair(sqlite_conn, pg_conn)
                if not reconciliation.ok:
                    raise MigrationReconciliationError(_issue_summary(reconciliation.issues))
                result = _result("imported", snapshot, imported, reconciliation)
            _write_report(report_path, result)
            return result


def main() -> None:
    parser = argparse.ArgumentParser(description="One-shot SQLite to PostgreSQL migration")
    parser.add_argument("--sqlite", required=True, help="legacy SQLite database path")
    parser.add_argument("--postgres-url", required=True, help="PostgreSQL DSN (never printed)")
    parser.add_argument("--snapshot", help="explicit immutable snapshot output path")
    parser.add_argument("--report", help="optional JSON report path")
    parser.add_argument("--batch-size", type=int, default=1000)
    parser.add_argument(
        "--maintenance-window-confirmed",
        action="store_true",
        help="required acknowledgement that all SQLite writers are stopped",
    )
    args = parser.parse_args()
    if not args.maintenance_window_confirmed:
        parser.error(
            "--maintenance-window-confirmed is required; T07 forbids online dual-write migration"
        )

    result = import_sqlite_to_postgres(
        args.sqlite,
        args.postgres_url,
        snapshot_path=args.snapshot,
        report_path=args.report,
        batch_size=args.batch_size,
    )
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
