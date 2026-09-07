"""T21 / SES-04 — the portable business-database facade (dev doc §12.4, plan A1).

The business services are being migrated to PG-canonical SQL (``%s``
placeholders, ``RETURNING``, ``ON CONFLICT``, ``now() + interval``, no
``rowid``). Customer production runs that SQL natively on PostgreSQL; the
internal SQLite desktop lane runs the *same* SQL through a bounded
translation layer — exactly one SQL source, no dual variants (the
single-implementation red line).

- ``translate_to_sqlite`` — a fail-closed translator over the bounded set of
  dialect differences. Anything it does not recognise raises ``ValueError``
  rather than silently passing through: the desktop lane must never run SQL
  the translator has not vetted.
- ``SQLiteBackend`` / ``PostgresBackend`` — execute() wrappers.
- ``BusinessConnection`` — the uniform facade the business services see:
  ``execute`` / ``transaction`` / ``commit`` / ``rollback`` with backend
  dispatch, plus ``.raw`` for the storage/ffprobe adapters and ``.ctx`` for
  the customer session context (filled once T21 wires the fencing).
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from typing import Any, Protocol, cast

import psycopg

# ---------------------------------------------------------------------------
# Translation
# ---------------------------------------------------------------------------

_INTERVAL_RE = re.compile(r"""now\s*\(\s*\)\s*(?P<op>[+-])\s*interval\s*'(?P<body>[^']+)'""")


def _translate_interval(match: re.Match[str]) -> str:
    """``now() + interval '60 seconds'`` → ``datetime('now', '+60 seconds')``.

    The interval body is a duration like ``60 seconds`` / ``2 days``; the
    leading sign on the SQLite modifier mirrors the operator.
    """
    body = match.group("body").strip()
    sign = "+" if match.group("op") == "+" else "-"
    return f"datetime('now', '{sign}{body}')"


def _consume_trailing_identifier(out: list[str]) -> str | None:
    """Pop the identifier at the tail of the translated output.

    The streaming translator appends each identifier character as its own
    element, so the tail of ``out`` is the identifier's characters in order.
    Used by the ``::timestamptz`` rule to wrap a bare or table-qualified
    column reference with SQLite's ``datetime()`` (see ``translate_to_sqlite``).
    Returns None when the tail is not a plain identifier (a ``?`` parameter or
    an expression), in which case the cast is simply dropped as before.
    """
    translated = "".join(out)
    match = re.search(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$", translated)
    if match is None:
        return None
    out[:] = [translated[: match.start()]]
    return match.group()


def translate_to_sqlite(sql: str) -> str:
    """Translate PG-canonical SQL to SQLite over the bounded dialect set.

    Rules (source SQL is always PG-canonical; the translator only *degrades*
    to SQLite for the desktop lane):

    - ``%s`` placeholder → ``?`` (outside string literals / comments);
    - ``FOR UPDATE [SKIP LOCKED]`` → removed (SQLite is single-writer and
      ``BEGIN IMMEDIATE`` already serialises writes);
    - ``now() [+|-] interval '<dur>'`` → ``datetime('now', '<+|-dur>')``;
    - ``::type`` cast → removed (the source must spell portable casts with
      ``CAST(x AS T)``; a non-plain cast raises ``ValueError``).

    Fail-closed: an unhandled Postgres cast or any ``%`` run the translator
    cannot account for raises ``ValueError`` instead of silently passing
    SQLite a query it may mis-execute.
    """
    out: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        # --- quoted / commented regions pass through untouched ---
        if ch == "'":
            # A string literal passes through verbatim. An unterminated
            # literal is a source bug — fail closed instead of mis-translating
            # (an unguarded ``end == -1`` previously looped forever).
            close = sql.find("'", i + 1)
            while close != -1 and close + 1 < n and sql[close + 1] == "'":
                # '' is an escaped quote inside the literal — keep scanning.
                close = sql.find("'", close + 2)
            if close == -1:
                raise ValueError(f"unterminated string literal in SQL: {sql!r}")
            out.append(sql[i : close + 1])
            i = close + 1
            continue
        if ch == '"':
            end = sql.find('"', i + 1)
            if end == -1:
                raise ValueError(f"unterminated double-quoted identifier in SQL: {sql!r}")
            out.append(sql[i : end + 1])
            i = end + 1
            continue
        if sql.startswith("--", i):
            end = sql.find("\n", i)
            if end == -1:
                out.append(sql[i:])
                break
            out.append(sql[i : end + 1])
            i = end + 1
            continue
        if sql.startswith("/*", i):
            end = sql.find("*/", i + 2)
            if end == -1:
                raise ValueError(f"unterminated block comment in SQL: {sql!r}")
            out.append(sql[i : end + 2])
            i = end + 2
            continue
        # --- normal region ---
        if ch == "%" and sql.startswith("%s", i):
            out.append("?")
            i += 2
            continue
        if ch == "%":
            # A lone '%' (modulo) is fine; a '%s'-shaped run that reached here
            # with a format we don't know must not be silently mis-executed.
            if i + 1 < n and sql[i + 1] in "sdiuxfg":
                raise ValueError(f"unhandled placeholder style in SQL: {sql[i : i + 2]!r}")
            out.append(ch)
            i += 1
            continue
        if ch == ":" and sql.startswith("::", i):
            m = re.match(r"::[A-Za-z_][A-Za-z0-9_]*", sql[i:])
            if m is None:
                raise ValueError(f"unhandled cast in SQL near {sql[i : i + 12]!r}")
            # A parameterised type (numeric(10,2), varchar(20), …) is not a
            # plain cast and must never be silently dropped.
            if i + m.end() < n and sql[i + m.end()] == "(":
                raise ValueError(f"unhandled parameterised cast in SQL near {sql[i : i + 16]!r}")
            if m.group() == "::timestamptz":
                # ident::timestamptz → datetime(ident), and %s::timestamptz →
                # datetime(?): SQLite has no casts and the ISO-8601 text lives
                # in TEXT columns, so a timestamp comparison must parse both
                # sides — datetime() accepts every storage format (SQLite
                # ``datetime('now', ...)`` output and Python ``.isoformat()``),
                # matching the PG cast semantics regardless of which writer
                # produced the column (T25 fair queue; the desktop lane
                # compares by parsing, never by string order).
                ident = _consume_trailing_identifier(out)
                if ident is not None:
                    out.append(f"datetime({ident})")
                elif out and out[-1] == "?":
                    out[-1] = "datetime(?)"
                i += m.end()
                continue
            i += m.end()
            continue
        if sql[i : i + 10].upper() == "FOR UPDATE":
            # Match "FOR UPDATE" / "FOR UPDATE SKIP LOCKED" as a standalone
            # clause (the session-row lock the PG lane relies on; SQLite's
            # single-writer BEGIN IMMEDIATE covers the same serialisation).
            skip = re.match(r"(?i)FOR UPDATE(?:\s+SKIP LOCKED)?", sql[i:])
            if skip:
                # Drop the whitespace that preceded the clause so no dangling
                # space survives the removal.
                while out and out[-1].isspace():
                    out.pop()
                i += skip.end()
                continue
        if sql[i : i + 3].upper() == "NOW":
            m = _INTERVAL_RE.match(sql[i:])
            if m:
                out.append(_translate_interval(m))
                i += m.end()
                continue
            if sql[i + 3 : i + 5] == "()":
                # Bare ``now()`` (no interval): SQLite has no now() function;
                # datetime('now') is the UTC current time in the same textual
                # shape every SQLite timestamp writer produces (T25).
                out.append("datetime('now')")
                i += 5
                continue
        out.append(ch)
        i += 1
    return "".join(out)


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------


class _BusinessCursor(Protocol):
    """The minimal cursor surface the business services use.

    ``Any`` returns mirror the sqlite3 typeshed (and the current ``conn:
    sqlite3.Connection`` call sites, where ``row[0]`` is already ``Any``) so
    the migrated services keep type-checking unchanged.
    """

    def fetchone(self) -> Any: ...

    def fetchall(self) -> list[Any]: ...

    @property
    def rowcount(self) -> int: ...


class SQLiteBackend:
    """Wraps a sqlite3 connection; every execute runs the translation."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def execute(self, sql: str, params: Sequence[object] = ()) -> sqlite3.Cursor:
        return self._conn.execute(translate_to_sqlite(sql), params)

    @property
    def raw(self) -> sqlite3.Connection:
        return self._conn


class PostgresBackend:
    """Wraps a psycopg connection; execute runs the native ``%s`` SQL.

    The connection carries a ``sqlite3.Row``-shaped row factory — the
    business services read rows both by position (``row[0]``) and by column
    name (``row["owner_user_id"]``), and psycopg's plain tuples only support
    position.
    """

    def __init__(self, conn: psycopg.Connection) -> None:
        self._conn = conn
        conn.row_factory = cast(Any, _named_row_factory)

    def execute(self, sql: str, params: Sequence[object] = ()) -> psycopg.Cursor:
        return self._conn.execute(sql, params)

    @property
    def raw(self) -> psycopg.Connection:
        return self._conn


class _NamedRow:
    """A psycopg row that also answers ``row["column"]`` (the sqlite3.Row shape).

    The business services read rows both by position (``row[0]``) and by
    column name (``row["owner_user_id"]``); psycopg's plain tuples only
    support position, so the PG backend returns these. ``__iter__``/``len``
    keep tuple-shaped call sites working; unknown names raise ValueError.
    """

    __slots__ = ("_values", "_names")

    def __init__(self, values: tuple[object, ...], names: tuple[str, ...]) -> None:
        self._values = values
        self._names = names

    def __getitem__(self, key: object) -> object:
        if isinstance(key, str):
            return self._values[self._names.index(key)]
        return self._values[cast(int, key)]

    def __iter__(self) -> Iterator[object]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def keys(self) -> tuple[str, ...]:
        return self._names


def _named_row_factory(cursor: psycopg.Cursor) -> Callable[[Sequence[object]], _NamedRow]:
    """psycopg3 row factory: called once with the cursor, returns the per-row
    maker bound to that query's column names."""
    names = tuple(col[0] for col in cursor.description) if cursor.description else ()

    def maker(values: Sequence[object]) -> _NamedRow:
        return _NamedRow(tuple(values), names)

    return maker


# ---------------------------------------------------------------------------
# The business facade
# ---------------------------------------------------------------------------


def _is_begin_immediate(sql: str) -> bool:
    return sql.strip().upper() == "BEGIN IMMEDIATE"


def _is_sqlite_pragma(sql: str) -> bool:
    return sql.strip().upper().startswith("PRAGMA")


class _NoopCursor:
    """The result of a swallowed SQLite-only statement on the PG lane."""

    def fetchone(self) -> None:
        return None

    def fetchall(self) -> list[Any]:
        return []

    @property
    def rowcount(self) -> int:
        return -1


_NOOP_CURSOR = _NoopCursor()


class BusinessConnection:
    """The uniform connection the business services see.

    Presents the sqlite3-shaped surface the services already use — execute,
    ``with conn:``, commit, rollback — and dispatches to the active backend.
    ``.transaction()`` is the explicit write transaction: BEGIN IMMEDIATE on
    SQLite, a no-op on PostgreSQL where the outer ``fenced_pg_transaction``
    already holds the transaction. ``commit()``/``rollback()`` are no-ops on
    PostgreSQL (commit authority stays with the fenced transaction).
    """

    def __init__(self, backend: SQLiteBackend | PostgresBackend) -> None:
        self._backend = backend
        self.ctx: object | None = None  # CustomerSessionContext (T21 fencing)

    # --- sqlite3-shaped surface ---

    def execute(self, sql: str, params: Sequence[object] = ()) -> _BusinessCursor:
        if isinstance(self._backend, PostgresBackend) and _is_begin_immediate(sql):
            # Migrated write services still spell their write lock as
            # ``BEGIN IMMEDIATE`` (the SQLite idiom). On the customer lane the
            # fenced transaction is already open, so the statement must be a
            # no-op — forwarding it to psycopg is a syntax error (PR #56 P1).
            return _NOOP_CURSOR
        if isinstance(self._backend, PostgresBackend) and _is_sqlite_pragma(sql):
            # SQLite tuning statements (busy_timeout & friends) carry no
            # meaning on the PG lane — pool timeouts own that concern.
            return _NOOP_CURSOR
        return self._backend.execute(sql, params)

    def executemany(self, sql: str, seq: list[Sequence[object]]) -> None:
        """sqlite3-shaped batch insert; test seeding uses it. No-op on PG
        (psycopg has no executemany — the PG lane never needs it)."""
        if isinstance(self._backend, SQLiteBackend):
            self._backend.raw.executemany(translate_to_sqlite(sql), seq)

    def commit(self) -> None:
        if isinstance(self._backend, SQLiteBackend):
            self._backend.raw.commit()

    def rollback(self) -> None:
        if isinstance(self._backend, SQLiteBackend):
            self._backend.raw.rollback()

    @property
    def is_postgres(self) -> bool:
        """Lane probe for services that must behave by backend (the fair-queue
        cursor maintenance runs unconditionally on PostgreSQL but has no table
        on the desktop SQLite lane)."""
        return isinstance(self._backend, PostgresBackend)

    @property
    def in_transaction(self) -> bool:
        """The sqlite3-shaped transaction-state probe the services use.

        SQLite: delegates to the underlying connection. PostgreSQL: the fenced
        transaction owns the state; report ``True`` once a transaction is open
        (the psycopg info parity) so callers that guard on it keep working.
        """
        if isinstance(self._backend, SQLiteBackend):
            return self._backend.raw.in_transaction
        return bool(self._backend.raw.info.transaction_status)

    def close(self) -> None:
        if isinstance(self._backend, SQLiteBackend):
            self._backend.raw.close()

    def set_trace_callback(self, callback: Callable[[str], object] | None) -> None:
        """sqlite3-shaped SQL trace hook (tests count statements). No-op on PG."""
        if isinstance(self._backend, SQLiteBackend):
            self._backend.raw.set_trace_callback(callback)

    def iterdump(self) -> Iterator[str]:
        """sqlite3-shaped whole-database dump (tests check no secret is stored)."""
        if isinstance(self._backend, SQLiteBackend):
            return self._backend.raw.iterdump()
        return iter(())

    # --- context-manager: `with conn:` commits on success, rolls back on error
    # --- (the sqlite3 contract the services already rely on)
    def __enter__(self) -> BusinessConnection:
        return self

    def __exit__(self, exc_type: object, _exc: object, _tb: object) -> None:
        if isinstance(self._backend, SQLiteBackend):
            if exc_type is None:
                self._backend.raw.commit()
            else:
                self._backend.raw.rollback()

    @contextmanager
    def transaction(self, isolation: str | None = None) -> Iterator[BusinessConnection]:
        """Explicit write transaction. SQLite: ``BEGIN IMMEDIATE``. PG: no-op —
        the outer ``fenced_pg_transaction`` owns the transaction, and
        ``isolation`` is forwarded there by the wiring (T21)."""
        if isinstance(self._backend, SQLiteBackend):
            raw = self._backend.raw
            raw.execute("BEGIN IMMEDIATE")
            try:
                yield self
                raw.commit()
            except BaseException:
                raw.rollback()
                raise
        else:
            yield self

    @property
    def raw(self) -> sqlite3.Connection | psycopg.Connection:
        return self._backend.raw

    # --- factory ---

    @classmethod
    def sqlite(cls, conn: sqlite3.Connection) -> BusinessConnection:
        return cls(SQLiteBackend(conn))

    @classmethod
    def postgres(cls, conn: psycopg.Connection) -> BusinessConnection:
        return cls(PostgresBackend(conn))
