"""T21 / SES-04 — the portable business-database translation (frozen name).

Fail-first tests for ``server/app/db_portable.py`` (the single-implementation
foundation): the fail-closed SQLite translator over the bounded dialect set,
and the ``BusinessConnection`` facade surface the business services see. The
existing SQLite test suite is the runtime oracle for the translation — these
tests lock the *rules* so a regression in the translator cannot silently
change what the desktop lane executes.
"""

from __future__ import annotations

import sqlite3

import pytest

from app.db_portable import BusinessConnection, translate_to_sqlite

# ---------------------------------------------------------------------------
# translate_to_sqlite — placeholders
# ---------------------------------------------------------------------------


def test_placeholder_becomes_question_mark() -> None:
    assert (
        translate_to_sqlite("INSERT INTO projects (id, owner_user_id, name) VALUES (%s, %s, %s)")
        == "INSERT INTO projects (id, owner_user_id, name) VALUES (?, ?, ?)"
    )


def test_placeholder_inside_a_string_literal_is_untouched() -> None:
    assert (
        translate_to_sqlite("SELECT * FROM t WHERE note = '100% savings' AND id = %s")
        == "SELECT * FROM t WHERE note = '100% savings' AND id = ?"
    )


def test_placeholder_inside_a_comment_is_untouched() -> None:
    assert (
        translate_to_sqlite("SELECT %s FROM t -- %s stays a comment\nWHERE id = %s")
        == "SELECT ? FROM t -- %s stays a comment\nWHERE id = ?"
    )
    assert (
        translate_to_sqlite("SELECT %s /* %s block */ FROM t WHERE id = %s")
        == "SELECT ? /* %s block */ FROM t WHERE id = ?"
    )


def test_placeholder_inside_a_double_quoted_identifier_is_untouched() -> None:
    assert (
        translate_to_sqlite('SELECT "%s" FROM t WHERE id = %s') == 'SELECT "%s" FROM t WHERE id = ?'
    )


def test_unknown_placeholder_style_fails_closed() -> None:
    with pytest.raises(ValueError, match="placeholder"):
        translate_to_sqlite("SELECT %d FROM t")


def test_unterminated_quote_or_comment_fails_closed() -> None:
    with pytest.raises(ValueError, match="unterminated"):
        translate_to_sqlite("SELECT 'oops")
    with pytest.raises(ValueError, match="unterminated"):
        translate_to_sqlite("SELECT /* oops")


# ---------------------------------------------------------------------------
# translate_to_sqlite — row-lock, interval, casts
# ---------------------------------------------------------------------------


def test_for_update_is_removed() -> None:
    assert (
        translate_to_sqlite("SELECT id FROM customer_session_state WHERE id = %s FOR UPDATE")
        == "SELECT id FROM customer_session_state WHERE id = ?"
    )
    assert (
        translate_to_sqlite("SELECT id FROM queue WHERE worker = %s FOR UPDATE SKIP LOCKED")
        == "SELECT id FROM queue WHERE worker = ?"
    )


def test_interval_is_translated() -> None:
    assert (
        translate_to_sqlite("SET lease_until = now() + interval '60 seconds'")
        == "SET lease_until = datetime('now', '+60 seconds')"
    )
    assert (
        translate_to_sqlite("WHERE now() - interval '30 minutes' <= updated_at")
        == "WHERE datetime('now', '-30 minutes') <= updated_at"
    )


def test_pg_cast_is_stripped() -> None:
    assert (
        translate_to_sqlite("SELECT lease_until::text FROM customer_session_state")
        == "SELECT lease_until FROM customer_session_state"
    )
    assert translate_to_sqlite("WHERE created_at::timestamptz >= %s") == "WHERE created_at >= ?"


def test_unhandled_cast_fails_closed() -> None:
    with pytest.raises(ValueError, match="cast"):
        translate_to_sqlite("SELECT x::numeric(10,2) FROM t")


def test_upsert_and_returning_pass_through() -> None:
    # SQLite >= 3.24 / 3.35 supports both natively; the translator leaves them.
    sql = "INSERT INTO t (id, v) VALUES (%s, %s) ON CONFLICT (id) DO NOTHING RETURNING id"
    assert (
        translate_to_sqlite(sql) == "INSERT INTO t (id, v) VALUES (?, ?) "
        "ON CONFLICT (id) DO NOTHING RETURNING id"
    )


# ---------------------------------------------------------------------------
# BusinessConnection — the sqlite3-shaped facade
# ---------------------------------------------------------------------------


@pytest.fixture()
def sqlite_business() -> BusinessConnection:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE items (id TEXT PRIMARY KEY, owner TEXT, note TEXT)")
    return BusinessConnection.sqlite(conn)


def test_execute_translates_and_commits_through_with(sqlite_business: BusinessConnection) -> None:
    with sqlite_business:
        sqlite_business.execute(
            "INSERT INTO items (id, owner, note) VALUES (%s, %s, %s)",
            ("a", "u1", "100% done"),
        )
    row = sqlite_business.execute(
        "SELECT note FROM items WHERE id = %s AND owner = %s", ("a", "u1")
    ).fetchone()
    assert row[0] == "100% done"


def test_with_rolls_back_on_exception(sqlite_business: BusinessConnection) -> None:
    try:
        with sqlite_business:
            sqlite_business.execute("INSERT INTO items (id, owner) VALUES (%s, %s)", ("b", "u2"))
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    count = sqlite_business.execute("SELECT count(*) FROM items").fetchone()[0]
    assert count == 0


def test_explicit_transaction_begin_immediate(sqlite_business: BusinessConnection) -> None:
    with sqlite_business.transaction():
        sqlite_business.execute("INSERT INTO items (id, owner) VALUES (%s, %s)", ("c", "u3"))
    count = sqlite_business.execute("SELECT count(*) FROM items").fetchone()[0]
    assert count == 1


def test_postgres_commit_and_rollback_are_no_ops() -> None:
    calls: list[str] = []

    class _StubBackend:  # the PostgresBackend contract
        def __init__(self) -> None:
            self._stub = _Stub()

        def execute(self, *args: object, **kwargs: object) -> None: ...

        def executemany(self, *args: object, **kwargs: object) -> None: ...

        @property
        def raw(self) -> _Stub:
            return self._stub

    class _Stub:
        def commit(self) -> None:
            calls.append("commit")

        def rollback(self) -> None:
            calls.append("rollback")

    conn = BusinessConnection(_StubBackend())  # type: ignore[arg-type]
    conn.commit()
    conn.rollback()
    assert calls == []  # no-op on the PG backend: commit authority stays with the fenced tx


def test_postgres_backend_swallows_begin_immediate() -> None:
    """PR #56 P1: migrated write services still spell their write lock as
    BEGIN IMMEDIATE; on the PG lane the fenced transaction is already open so
    the statement must be a no-op (forwarding it to psycopg is a syntax error)."""
    calls: list[str] = []

    class _Result:
        def fetchone(self) -> None:
            return None

        def fetchall(self) -> list[object]:
            return []

        @property
        def rowcount(self) -> int:
            return -1

    class _StubPsycopg:
        row_factory: object = None

        def execute(self, sql: str, params: object = ()) -> _Result:
            calls.append(sql)
            return _Result()

    from app.db_portable import BusinessConnection, PostgresBackend

    conn = BusinessConnection(PostgresBackend(_StubPsycopg()))  # type: ignore[arg-type]
    result = conn.execute("BEGIN IMMEDIATE")
    assert calls == []  # swallowed on the PG lane
    assert result.fetchone() is None
    conn.execute("SELECT 1")
    assert calls == ["SELECT 1"]
