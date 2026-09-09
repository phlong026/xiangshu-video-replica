"""
T05 / DB-03 - PostgreSQL runtime entry tests.

Covers the DSN resolution layer, the customer-production fail-closed matrix,
the psycopg3 connection pool, transaction semantics (including the
SERIALIZABLE replacement for SQLite BEGIN IMMEDIATE), and PG server time.
PG-dependent cases skip automatically when the fixture is not reachable.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock

import psycopg
import pytest
from fastapi import HTTPException, Request
from pg_test_kit import require_pg_or_explicit_skip
from psycopg_pool import ConnectionPool

from app.db_pg import (
    DATABASE_URL_ENV,
    PG_IDLE_IN_TRANSACTION_TIMEOUT_MS,
    PG_STATEMENT_TIMEOUT_MS,
    DatabaseMode,
    MissingDatabaseConfigError,
    check_pg_ready,
    close_pg_pool,
    pg_server_now,
    pg_transaction,
    resolve_database_config,
    validate_customer_production,
)

DEFAULT_DSN = "postgresql://testuser:testpass@localhost:5433/customer_v3_test"


PG_DSN = os.environ.get("TEST_POSTGRESQL_URL", DEFAULT_DSN)


@contextmanager
def _env(**overrides: str) -> Iterator[None]:
    """Temporarily set/unset environment variables."""
    saved: dict[str, str | None] = {}
    for key, value in overrides.items():
        saved[key] = os.environ.get(key)
        if value == "":
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


# ---------------------------------------------------------------------------
# DSN resolution (pure, no PG required)
# ---------------------------------------------------------------------------


def test_resolve_pg_url_selects_postgres_mode() -> None:
    with _env(**{DATABASE_URL_ENV: "postgresql://u:p@host:5432/db"}):
        config = resolve_database_config()
    assert config.mode is DatabaseMode.POSTGRESQL
    assert config.dsn == "postgresql://u:p@host:5432/db"


def test_resolve_postgres_scheme_alias() -> None:
    with _env(**{DATABASE_URL_ENV: "postgres://u:p@host:5432/db"}):
        config = resolve_database_config()
    assert config.mode is DatabaseMode.POSTGRESQL


def test_resolve_sqlite_fallback_keeps_internal_mode() -> None:
    with _env(**{DATABASE_URL_ENV: "", "VIDEO_REPLICA_DB_PATH": "/tmp/app.db"}):
        config = resolve_database_config()
    assert config.mode is DatabaseMode.SQLITE
    assert config.sqlite_path == "/tmp/app.db"


@pytest.mark.parametrize("database_url", ["", "sqlite:///internal.db"])
def test_customer_snapshot_ignores_cached_pg_pool_on_internal_lane(
    monkeypatch: pytest.MonkeyPatch, database_url: str
) -> None:
    from app import customer_fence

    monkeypatch.setenv(DATABASE_URL_ENV, database_url)
    cached_pool = Mock(return_value=object())
    monkeypatch.setattr(customer_fence, "get_pg_pool", cached_pool)
    request = Request({"type": "http", "headers": []})

    assert customer_fence.customer_session_snapshot(request) is None
    cached_pool.assert_not_called()


@pytest.mark.parametrize("database_url", ["", "sqlite://", "sqlite:///"])
def test_business_read_ignores_cached_pg_pool_on_internal_lane(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, database_url: str
) -> None:
    from app import customer_fence

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(
        DATABASE_URL_ENV,
        f"{database_url}url.db" if database_url else "",
    )
    monkeypatch.setenv("VIDEO_REPLICA_DB_PATH", str(tmp_path / "internal.db"))
    monkeypatch.setattr(customer_fence, "get_pg_pool", Mock(return_value=object()))
    pg_read = Mock(side_effect=AssertionError("Internal reads must not open PostgreSQL"))
    monkeypatch.setattr(customer_fence, "pg_transaction", pg_read)

    with contextmanager(customer_fence.get_business_read_conn)() as conn:
        assert not conn.is_postgres
        assert conn.execute("SELECT 42").fetchone()[0] == 42
    pg_read.assert_not_called()


@pytest.mark.parametrize("operation", ["read", "write"])
@pytest.mark.parametrize(
    ("url_prefix", "has_legacy_path"),
    [
        ("sqlite://", False),
        ("sqlite://", True),
        ("sqlite:///", False),
        ("sqlite:///", True),
        ("", True),
    ],
)
def test_business_sqlite_connections_follow_resolved_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operation: str,
    url_prefix: str,
    has_legacy_path: bool,
) -> None:
    from app import customer_fence

    target = tmp_path / "selected.db"
    legacy = tmp_path / "legacy.db"
    monkeypatch.chdir(tmp_path)
    for path, marker in ((target, "selected"), (legacy, "legacy")):
        with sqlite3.connect(path) as conn:
            conn.execute(
                "CREATE TABLE users (id TEXT, username TEXT, display_name TEXT, "
                "role TEXT, is_active INTEGER)"
            )
            conn.execute(
                "INSERT INTO users VALUES ('internal_u', ?, ?, 'employee', 1)",
                (marker, marker),
            )
            conn.execute("CREATE TABLE markers (value TEXT)")
            conn.execute("INSERT INTO markers VALUES (?)", (marker,))
    monkeypatch.delenv("VIDEO_REPLICA_CUSTOMER_PRODUCTION", raising=False)
    monkeypatch.setenv("VIDEO_REPLICA_AUTH_MODE", "desktop")
    monkeypatch.setenv("VIDEO_REPLICA_DESKTOP_USER_ID", "internal_u")
    url_path = target.as_posix() if url_prefix == "sqlite:///" else target.name
    monkeypatch.setenv(DATABASE_URL_ENV, f"{url_prefix}{url_path}" if url_prefix else "")
    if has_legacy_path:
        monkeypatch.setenv("VIDEO_REPLICA_DB_PATH", str(legacy if url_prefix else target))
    else:
        monkeypatch.delenv("VIDEO_REPLICA_DB_PATH", raising=False)

    if operation == "read":
        with contextmanager(customer_fence.get_business_read_conn)() as conn:
            assert conn.execute("SELECT value FROM markers").fetchone()[0] == "selected"
    else:
        request = Request({"type": "http", "headers": []})
        with customer_fence.get_business_db(request).write() as (conn, actor):
            assert actor.username == "selected"
            conn.execute("INSERT INTO markers VALUES ('written')")
            conn.commit()
        with sqlite3.connect(target) as conn:
            assert conn.execute("SELECT value FROM markers").fetchall() == [
                ("selected",),
                ("written",),
            ]
    with sqlite3.connect(legacy) as conn:
        assert conn.execute("SELECT value FROM markers").fetchall() == [("legacy",)]


@pytest.mark.parametrize("operation", ["read", "write"])
@pytest.mark.parametrize("production", [False, True])
def test_business_sqlite_rejects_missing_or_production_configuration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, operation: str, production: bool
) -> None:
    from app import customer_fence

    monkeypatch.delenv("VIDEO_REPLICA_DB_PATH", raising=False)
    if production:
        monkeypatch.setenv("VIDEO_REPLICA_DB_PATH", str(tmp_path / "legacy.db"))
    monkeypatch.setenv("VIDEO_REPLICA_CUSTOMER_PRODUCTION", "true" if production else "")
    monkeypatch.setenv(
        DATABASE_URL_ENV,
        f"sqlite:///{(tmp_path / 'forbidden.db').as_posix()}" if production else "",
    )
    connect_sqlite = Mock()
    monkeypatch.setattr(customer_fence, "connect_database", connect_sqlite)

    with pytest.raises(HTTPException) as error:
        if operation == "read":
            next(customer_fence.get_business_read_conn())
        else:
            with customer_fence.BusinessDb(None, None, None).write():
                pytest.fail("Invalid configuration must not open a business connection")
    assert error.value.status_code == 503
    assert error.value.detail["code"] == "DATABASE_NOT_CONFIGURED"
    connect_sqlite.assert_not_called()


def test_business_write_without_snapshot_never_falls_back_from_pg(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from app import customer_fence

    monkeypatch.setenv(DATABASE_URL_ENV, PG_DSN)
    monkeypatch.setenv("VIDEO_REPLICA_DB_PATH", str(tmp_path / "legacy.db"))
    connect_sqlite = Mock()
    monkeypatch.setattr(customer_fence, "connect_database", connect_sqlite)

    with pytest.raises(HTTPException) as error:
        with customer_fence.BusinessDb(None, None, None).write():
            pytest.fail("A PG writer requires a customer session snapshot")
    assert error.value.status_code == 503
    assert error.value.detail["code"] == "DATABASE_NOT_CONFIGURED"
    connect_sqlite.assert_not_called()


@pytest.mark.parametrize("error_type", [RuntimeError, ValueError])
def test_customer_snapshot_pg_pool_failure_is_fail_closed(
    monkeypatch: pytest.MonkeyPatch, error_type: type[Exception]
) -> None:
    from app import customer_fence

    monkeypatch.setenv(DATABASE_URL_ENV, PG_DSN)
    monkeypatch.setattr(customer_fence, "get_pg_pool", Mock(side_effect=error_type("PG failed")))
    request = Request({"type": "http", "headers": []})

    with pytest.raises(HTTPException) as error:
        customer_fence.get_business_db(request)
    assert error.value.status_code == 503
    assert error.value.detail["code"] == "SESSION_SERVICE_UNAVAILABLE"


@pytest.mark.parametrize("error_type", [RuntimeError, ValueError])
def test_business_read_pg_failure_does_not_fall_back_to_sqlite(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, error_type: type[Exception]
) -> None:
    from app import customer_fence, db_pg

    monkeypatch.setenv(DATABASE_URL_ENV, PG_DSN)
    monkeypatch.setenv("VIDEO_REPLICA_DB_PATH", str(tmp_path / "internal.db"))
    unavailable_pool = Mock(side_effect=error_type("PG failed"))
    monkeypatch.setattr(customer_fence, "get_pg_pool", unavailable_pool)
    monkeypatch.setattr(db_pg, "get_pg_pool", unavailable_pool)
    sqlite_connect = Mock()
    monkeypatch.setattr(customer_fence, "connect_database", sqlite_connect)

    with pytest.raises(error_type, match="PG failed"):
        next(customer_fence.get_business_read_conn())
    sqlite_connect.assert_not_called()


def test_resolve_rejects_unsupported_scheme() -> None:
    with _env(**{DATABASE_URL_ENV: "mysql://u:p@host/db"}):
        with pytest.raises(ValueError, match="unsupported database URL scheme"):
            resolve_database_config()


def test_resolve_missing_internal_raises_missing_database_config_error() -> None:
    """The internal lane may legitimately boot without any database env
    (the legacy lane resolves per-request), so the missing-config path
    raises the narrow MissingDatabaseConfigError — never the generic
    ValueError also used for unsupported schemes, which the lifespan
    must not swallow (Codex P1)."""
    with _env(**{DATABASE_URL_ENV: "", "VIDEO_REPLICA_DB_PATH": ""}):
        with pytest.raises(MissingDatabaseConfigError):
            resolve_database_config()


# ---------------------------------------------------------------------------
# Customer-production fail-closed matrix (pure, no PG required)
# ---------------------------------------------------------------------------


def test_production_requires_database_url() -> None:
    with _env(**{"VIDEO_REPLICA_CUSTOMER_PRODUCTION": "true", DATABASE_URL_ENV: ""}):
        with pytest.raises(RuntimeError, match="DATABASE_URL"):
            validate_customer_production(resolve_database_config())


def test_production_rejects_sqlite() -> None:
    with _env(
        **{
            "VIDEO_REPLICA_CUSTOMER_PRODUCTION": "true",
            DATABASE_URL_ENV: "sqlite:////data/app.db",
        }
    ):
        with pytest.raises(RuntimeError, match="SQLite"):
            validate_customer_production(resolve_database_config())


def test_production_rejects_leftover_db_path() -> None:
    """Customer production + PG URL + legacy DB_PATH is ambiguous and must
    fail closed instead of silently preferring the URL (PR #32 review P1)."""
    with _env(
        **{
            "VIDEO_REPLICA_CUSTOMER_PRODUCTION": "true",
            DATABASE_URL_ENV: "postgresql://u:p@host:5432/db?sslmode=verify-full",
            "VIDEO_REPLICA_DB_PATH": "/leftover/app.db",
        }
    ):
        with pytest.raises(RuntimeError, match="VIDEO_REPLICA_DB_PATH"):
            validate_customer_production(resolve_database_config())


@pytest.mark.parametrize("sslmode", ["", "disable", "allow", "prefer"])
def test_production_rejects_postgres_without_enforced_tls(sslmode: str) -> None:
    dsn = "postgresql://u:secret@host:5432/db"
    if sslmode:
        dsn = f"{dsn}?sslmode={sslmode}"

    with _env(
        **{
            "VIDEO_REPLICA_CUSTOMER_PRODUCTION": "true",
            DATABASE_URL_ENV: dsn,
            "VIDEO_REPLICA_DB_PATH": "",
        }
    ):
        with pytest.raises(RuntimeError, match="must enforce TLS") as exc_info:
            validate_customer_production(resolve_database_config())

    assert "secret" not in str(exc_info.value)


@pytest.mark.parametrize("sslmode", ["require", "verify-ca", "verify-full"])
def test_production_accepts_postgres_tls_enforcing_modes(sslmode: str) -> None:
    with _env(
        **{
            "VIDEO_REPLICA_CUSTOMER_PRODUCTION": "true",
            DATABASE_URL_ENV: f"postgresql://u:p@host:5432/db?sslmode={sslmode}",
            "VIDEO_REPLICA_DB_PATH": "",
        }
    ):
        validate_customer_production(resolve_database_config())


def test_production_rejects_ambiguous_postgres_sslmode() -> None:
    with _env(
        **{
            "VIDEO_REPLICA_CUSTOMER_PRODUCTION": "true",
            DATABASE_URL_ENV: ("postgresql://u:p@host:5432/db?sslmode=require&sslmode=disable"),
            "VIDEO_REPLICA_DB_PATH": "",
        }
    ):
        with pytest.raises(RuntimeError, match="must enforce TLS"):
            validate_customer_production(resolve_database_config())


def test_non_production_allows_sqlite() -> None:
    with _env(**{"VIDEO_REPLICA_CUSTOMER_PRODUCTION": "", DATABASE_URL_ENV: "sqlite:////tmp/x.db"}):
        config = resolve_database_config()
        # Must not raise in internal mode.
        validate_customer_production(config)


# ---------------------------------------------------------------------------
# Pool / transactions / server time (require the PG fixture)
# ---------------------------------------------------------------------------

pytestmark_pg = pytest.mark.usefixtures("pg_hard_gate")


@pytest.fixture(scope="module")
def pg_hard_gate() -> None:
    """CW-007 hard gate: unreachable PG fails the suite (explicit opt-in may skip)."""
    require_pg_or_explicit_skip()


@pytestmark_pg
def test_check_pg_ready_uses_pool_and_server_time() -> None:
    with _env(**{DATABASE_URL_ENV: PG_DSN}):
        ready = check_pg_ready()
    # The DSN is redacted at the PgReadyInfo boundary (M1 review LOW).
    assert ready.dsn != PG_DSN
    assert "testpass" not in ready.dsn
    assert isinstance(ready.server_now, datetime)
    assert ready.pool_size >= 1


def test_check_pg_ready_rejects_a_read_only_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import db_pg

    statements: list[str] = []

    class ScalarCursor:
        def __init__(self, value: object) -> None:
            self._value = value

        def fetchone(self) -> tuple[object]:
            return (self._value,)

    class ReadOnlyConnection:
        def execute(self, sql: str) -> ScalarCursor:
            statements.append(sql)
            values: dict[str, object] = {
                "SELECT now()": datetime.now(),
                "SELECT 1": 1,
                "SHOW transaction_read_only": "on",
            }
            return ScalarCursor(values[sql])

    class ReadOnlyPool:
        max_size = 4

        @contextmanager
        def connection(self) -> Iterator[ReadOnlyConnection]:
            yield ReadOnlyConnection()

    monkeypatch.setenv(DATABASE_URL_ENV, "postgresql://app@pg-ha/customer")
    monkeypatch.setattr(db_pg, "get_pg_pool", lambda: ReadOnlyPool())

    with pytest.raises(RuntimeError, match="read-only"):
        db_pg.check_pg_ready()

    assert "SHOW transaction_read_only" in statements


@pytestmark_pg
def test_pg_transaction_commits() -> None:
    with _env(**{DATABASE_URL_ENV: PG_DSN}):
        with pg_transaction() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS t05_tx ("
                "id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY, value TEXT)"
            )
            conn.execute("TRUNCATE t05_tx")
            conn.execute("INSERT INTO t05_tx (value) VALUES (%s)", ("committed",))
        with pg_transaction() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM t05_tx WHERE value = %s", ("committed",)
            ).fetchone()[0]
            assert count == 1


@pytestmark_pg
def test_pg_pool_sets_leaked_transaction_guardrails() -> None:
    """2026-09-07 评审 §7-0：泄漏的未提交 claim 事务曾在共享容量行锁上
    挂住并发 worker 19 分钟。池连接必须携带 statement / idle-in-transaction
    超时，把这类故障从无限排队变成数据库侧快速失败。"""
    with _env(**{DATABASE_URL_ENV: PG_DSN}):
        close_pg_pool()
        try:
            with pg_transaction() as conn:
                # pg_settings.setting 是 GUC 基础单位（ms）的原始值；SHOW /
                # current_setting 会把它单位化成 "5min" 这类显示串。
                rows = conn.execute(
                    "SELECT name, setting FROM pg_settings WHERE name = ANY(%s)",
                    (["statement_timeout", "idle_in_transaction_session_timeout"],),
                ).fetchall()
        finally:
            close_pg_pool()
    values = {str(row[0]): int(row[1]) for row in rows}
    assert values["statement_timeout"] == PG_STATEMENT_TIMEOUT_MS
    assert values["idle_in_transaction_session_timeout"] == (PG_IDLE_IN_TRANSACTION_TIMEOUT_MS)


@pytestmark_pg
def test_pg_transaction_rolls_back_on_error() -> None:
    with _env(**{DATABASE_URL_ENV: PG_DSN}):
        with pytest.raises(RuntimeError, match="boom"):
            with pg_transaction() as conn:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS t05_tx ("
                    "id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY, value TEXT)"
                )
                conn.execute("INSERT INTO t05_tx (value) VALUES (%s)", ("rolled-back",))
                raise RuntimeError("boom")
        with pg_transaction() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM t05_tx WHERE value = %s", ("rolled-back",)
            ).fetchone()[0]
            assert count == 0


@pytestmark_pg
def test_pg_transaction_serializable_write_conflict() -> None:
    """SERIALIZABLE replaces SQLite BEGIN IMMEDIATE: a transaction whose snapshot
    is invalidated by a committed concurrent write to the same key must fail
    with SerializationFailure instead of silently overwriting."""
    with _env(**{DATABASE_URL_ENV: PG_DSN}):
        with pg_transaction() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS t05_conflict (id INTEGER PRIMARY KEY, v INTEGER)"
            )
            conn.execute("TRUNCATE t05_conflict")
            conn.execute("INSERT INTO t05_conflict (id, v) VALUES (1, 0)")

        # Writer B opens a SERIALIZABLE transaction and reads the key (snapshot v=0),
        # then keeps the transaction open while writer A commits on another
        # pooled connection (read-then-write on the same key).
        with pytest.raises(psycopg.errors.SerializationFailure):
            with pg_transaction(isolation="SERIALIZABLE") as conn_b:
                conn_b.execute("SELECT v FROM t05_conflict WHERE id = 1").fetchone()
                # Concurrent committer A on a second pooled connection.
                with pg_transaction() as conn_a:
                    conn_a.execute("SELECT v FROM t05_conflict WHERE id = 1").fetchone()
                    conn_a.execute("UPDATE t05_conflict SET v = 1 WHERE id = 1")
                # B writes the same key based on its stale snapshot.
                conn_b.execute("UPDATE t05_conflict SET v = 2 WHERE id = 1")
                # Leaving the block commits B → rejected with SQLSTATE 40001.


@pytestmark_pg
def test_pg_server_now_is_monotonic_and_not_client_clock() -> None:
    with _env(**{DATABASE_URL_ENV: PG_DSN}):
        first = pg_server_now()
        second = pg_server_now()
    assert isinstance(first, datetime)
    assert second >= first


@pytestmark_pg
def test_pool_returns_connections_and_is_observable() -> None:
    with _env(**{DATABASE_URL_ENV: PG_DSN}):
        from app.db_pg import get_pg_pool

        pool = get_pg_pool()
        assert isinstance(pool, ConnectionPool)
        with pool.connection() as conn:
            value = conn.execute("SELECT 1").fetchone()[0]
            assert value == 1
        assert not pool.closed


@pytestmark_pg
def test_pool_applies_hygiene_parameters() -> None:
    """M0 review M1: the pool must check connections and recycle them by
    lifetime/idle bounds instead of handing out possibly-dead sockets."""
    with _env(**{DATABASE_URL_ENV: PG_DSN}):
        from app.db_pg import (
            DEFAULT_POOL_MAX_IDLE,
            DEFAULT_POOL_MAX_LIFETIME,
            DEFAULT_POOL_TIMEOUT,
            get_pg_pool,
        )

        pool = get_pg_pool()
        # NB: ``pool.check`` is a *method* (runs the checks on demand); the
        # configured callback lives on the private ``_check`` attribute.
        assert pool._check == ConnectionPool.check_connection
        assert pool.max_lifetime == DEFAULT_POOL_MAX_LIFETIME
        assert pool.max_idle == DEFAULT_POOL_MAX_IDLE
        assert pool.timeout == DEFAULT_POOL_TIMEOUT


def test_pg_transaction_rejects_unknown_isolation_level() -> None:
    """M0 review M3: the isolation level feeds a SET statement, so anything
    outside the closed allow-list must be rejected before touching the DB."""
    from app.db_pg import _ALLOWED_ISOLATION_LEVELS  # noqa: PLC2701 - assert surface

    with pytest.raises(ValueError, match="unsupported isolation level"):
        with pg_transaction(isolation="SERIALIZABLE; DROP TABLE users"):  # type: ignore[arg-type]
            pass

    assert "SERIALIZABLE" in _ALLOWED_ISOLATION_LEVELS
    assert "READ COMMITTED" in _ALLOWED_ISOLATION_LEVELS
    assert "REPEATABLE READ" in _ALLOWED_ISOLATION_LEVELS


@pytest.fixture(autouse=True)
def _close_pool_between_tests() -> Iterator[None]:
    """Reset the module-level pool so each test binds its own DSN."""
    from app.db_pg import close_pg_pool

    close_pg_pool()
    yield
    close_pg_pool()


# ---------------------------------------------------------------------------
# API/Worker bootstrap integration (require the PG fixture)
# ---------------------------------------------------------------------------


@pytestmark_pg
def test_api_bootstrap_completes_in_pg_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """`python -m app.bootstrap` must finish the PG ready check without
    touching any SQLite file (API startup path for the PG lane)."""
    from app import bootstrap as bootstrap_module

    monkeypatch.setenv("VIDEO_REPLICA_DATABASE_URL", PG_DSN)
    monkeypatch.delenv("VIDEO_REPLICA_DB_PATH", raising=False)
    monkeypatch.delenv("VIDEO_REPLICA_CUSTOMER_PRODUCTION", raising=False)
    bootstrap_module.main([])  # must return cleanly after check_pg_ready()


@pytestmark_pg
def test_worker_main_dispatches_to_pg_forever_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """The generation worker entry must complete the PG ready check and hand
    over to the T25 fair-queue loop (``run_forever_pg``) — never the SQLite
    task loop.

    M0 review H1 required a non-zero exit while the PG loop was still
    unimplemented ("restart on failure must not mistake the unimplemented PG
    lane for a healthy idle worker"). T25 landed the PG loop, so the worker
    now stays in it; the supervisor's health signal is the loop's liveness,
    not an exit code.

    Asserted by behaviour (not log output, which is vulnerable to global
    logging state left behind by other tests): neither the one-shot loop nor
    the SQLite forever loop may run in PG mode, and the pool stays usable
    once the worker releases it.
    """
    from app import generation_worker as worker_module
    from app.db_pg import get_pg_pool

    calls: list[str] = []
    monkeypatch.setenv("VIDEO_REPLICA_DATABASE_URL", PG_DSN)
    monkeypatch.delenv("VIDEO_REPLICA_DB_PATH", raising=False)
    monkeypatch.setattr("sys.argv", ["generation_worker"])
    monkeypatch.setattr(
        worker_module, "run_forever_pg", lambda **kwargs: calls.append("run_forever_pg")
    )
    monkeypatch.setattr(
        worker_module, "run_forever", lambda **kwargs: calls.append("run_forever"), raising=True
    )
    monkeypatch.setattr(
        worker_module,
        "run_worker_once",
        lambda *args, **kwargs: calls.append("run_worker_once"),
        raising=True,
    )

    worker_module.main()

    assert calls == ["run_forever_pg"], f"PG-mode worker must dispatch to the PG loop, got {calls}"
    # The worker closes its pool after the loop returns; a fresh pool must
    # still be creatable from the same configuration.
    assert not get_pg_pool().closed


# ---------------------------------------------------------------------------
# M0 review H3 — VIDEO_REPLICA_DATABASE_URL drives `alembic upgrade head`
# ---------------------------------------------------------------------------


def _alembic_head() -> str:
    from alembic.script import ScriptDirectory

    server_dir = Path(__file__).resolve().parent.parent
    return ScriptDirectory(str(server_dir / "migrations")).get_current_head()


def _run_alembic_upgrade_head() -> subprocess.CompletedProcess[str]:
    server_dir = Path(__file__).resolve().parent.parent
    # Alembic echoes migration comments that may contain non-ASCII bytes;
    # decode as UTF-8 with replacement instead of the ambient Windows code
    # page, whose reader thread would otherwise die mid-decode and leave
    # stderr as None.
    return subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=server_dir,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
        check=False,
    )


def test_alembic_env_var_targets_sqlite_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """H3: the env var must override the alembic.ini default, so `alembic
    upgrade head` targets the configured SQLite file instead of data/app.db."""
    db_path = tmp_path / "env-var.db"
    monkeypatch.setenv(DATABASE_URL_ENV, f"sqlite:///{db_path}")

    result = _run_alembic_upgrade_head()

    assert result.returncode == 0, result.stderr
    assert db_path.exists(), "env var must redirect migrations away from data/app.db"
    with sqlite3.connect(db_path) as conn:
        version = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
    assert version == _alembic_head()


@pytestmark_pg
def test_alembic_env_var_dsn_runs_migrations_on_pg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """H3: a bare ``postgresql://`` env var must be rewritten to the psycopg3
    driver (bare URLs resolve to psycopg2, which is not installed) and drive
    the real `alembic upgrade head` against a fresh PG database — the exact
    operator path the ini-file default used to block."""
    db_name = "h3_env_url_test"
    admin_dsn = PG_DSN.rsplit("/", 1)[0] + "/postgres"
    dsn = PG_DSN.rsplit("/", 1)[0] + f"/{db_name}"
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{db_name}"')

    # Deliberately the bare postgresql:// scheme: the rewrite is part of the
    # behaviour under test.
    monkeypatch.setenv(DATABASE_URL_ENV, dsn)
    try:
        result = _run_alembic_upgrade_head()
        assert result.returncode == 0, result.stderr
        with psycopg.connect(dsn) as conn:
            version = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        assert version == _alembic_head()
    finally:
        with psycopg.connect(admin_dsn, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)')


def test_pool_max_is_capped_at_a_hard_ceiling() -> None:
    """A misconfigured POOL_MAX must not drain the server's connections (M1 review LOW)."""
    from app.db_pg import POOL_MAX_ENV, _pool_bounds

    with _env(**{POOL_MAX_ENV: "100000"}):
        pool_min, pool_max = _pool_bounds()
    assert pool_max == 64


def test_pool_min_is_capped_at_the_hard_ceiling() -> None:
    """A POOL_MIN above the ceiling must not yield an invalid (min > max)
    pair: ConnectionPool rejects max_size smaller than min_size, turning
    the connection-budget safeguard into a startup failure (Codex P2)."""
    from app.db_pg import POOL_MAX_ENV, POOL_MIN_ENV, _pool_bounds

    with _env(**{POOL_MIN_ENV: "100000", POOL_MAX_ENV: "32"}):
        pool_min, pool_max = _pool_bounds()
    assert (pool_min, pool_max) == (64, 64)


@pytest.mark.usefixtures("pg_hard_gate")
def test_check_pg_ready_redacts_dsn_credentials() -> None:
    """PgReadyInfo.dsn must never carry the password (M1 review LOW)."""
    with _env(**{DATABASE_URL_ENV: PG_DSN}):
        ready = check_pg_ready()
    assert ready.dsn != PG_DSN
    assert "testpass" not in ready.dsn
    assert "customer_v3_test" in ready.dsn
