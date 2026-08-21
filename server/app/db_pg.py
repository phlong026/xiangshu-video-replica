"""PostgreSQL runtime entry for the customer edition (T05 / DB-03).

This module owns the database *mode* resolution and the PG-side runtime
primitives: DSN handling, a psycopg3 connection pool, transaction contexts
(including the SERIALIZABLE replacement for SQLite ``BEGIN IMMEDIATE``),
server-side time, and the customer-production fail-closed guard.

Design constraints (docs/客户版代码开发清单-V3.md §8.1):
- No ORM, no Redis, no queue framework: psycopg3 + psycopg_pool only.
- The SQLite runtime stays untouched for the internal P0 mode; this module is
  additive until business modules migrate lane by lane.
- Customer production must refuse to start on SQLite or without a PG URL.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

import psycopg
from psycopg_pool import ConnectionPool

logger = logging.getLogger(__name__)

DATABASE_URL_ENV = "VIDEO_REPLICA_DATABASE_URL"
DB_PATH_ENV = "VIDEO_REPLICA_DB_PATH"
CUSTOMER_PRODUCTION_ENV = "VIDEO_REPLICA_CUSTOMER_PRODUCTION"
POOL_MIN_ENV = "VIDEO_REPLICA_PG_POOL_MIN"
POOL_MAX_ENV = "VIDEO_REPLICA_PG_POOL_MAX"

DEFAULT_POOL_MIN = 1
DEFAULT_POOL_MAX = 8
PG_URL_SCHEMES = ("postgresql://", "postgres://")
SQLITE_URL_SCHEMES = ("sqlite:///", "sqlite://")
_TRUTHY = {"1", "true", "yes", "on"}


class DatabaseMode(Enum):
    SQLITE = "sqlite"
    POSTGRESQL = "postgresql"


@dataclass(frozen=True)
class DatabaseConfig:
    mode: DatabaseMode
    dsn: str | None
    sqlite_path: str | None


@dataclass(frozen=True)
class PgReadyInfo:
    dsn: str
    server_now: datetime
    pool_size: int


def _is_customer_production() -> bool:
    return os.environ.get(CUSTOMER_PRODUCTION_ENV, "").strip().lower() in _TRUTHY


def _sqlite_path_from_url(url: str) -> str:
    if url.startswith("sqlite:///"):
        return url.split("sqlite:///", 1)[-1]
    return url.split("sqlite://", 1)[-1]


def _fetch_scalar(conn: psycopg.Connection, sql: str) -> object:
    """Fetch a single-row scalar, narrowing the Optional from fetchone()."""
    row = conn.execute(sql).fetchone()
    if row is None:
        raise RuntimeError(f"query returned no rows: {sql}")
    return row[0]


def resolve_database_config() -> DatabaseConfig:
    """Resolve the active database mode from the environment.

    ``VIDEO_REPLICA_DATABASE_URL`` wins when set: ``postgresql://`` (or the
    ``postgres://`` alias) selects the PG runtime, a ``sqlite://`` URL selects
    the legacy SQLite runtime. Without it, ``VIDEO_REPLICA_DB_PATH`` keeps the
    internal P0 SQLite behaviour so existing deployments stay unchanged.
    """
    url = os.environ.get(DATABASE_URL_ENV, "").strip()
    if url:
        if url.startswith(PG_URL_SCHEMES):
            return DatabaseConfig(mode=DatabaseMode.POSTGRESQL, dsn=url, sqlite_path=None)
        if url.startswith(SQLITE_URL_SCHEMES):
            path = _sqlite_path_from_url(url)
            return DatabaseConfig(mode=DatabaseMode.SQLITE, dsn=None, sqlite_path=path)
        scheme = url.split(":", 1)[0]
        raise ValueError(
            f"unsupported database URL scheme: {scheme}:// (expected postgresql:// or sqlite://)"
        )

    db_path = os.environ.get(DB_PATH_ENV, "").strip()
    if db_path:
        return DatabaseConfig(mode=DatabaseMode.SQLITE, dsn=None, sqlite_path=db_path)

    missing = f"neither {DATABASE_URL_ENV} nor {DB_PATH_ENV} is set; cannot resolve database mode"
    if _is_customer_production():
        # Fail closed with the production-facing message instead of a generic
        # configuration error.
        raise RuntimeError(
            f"customer production requires PostgreSQL: {missing} "
            f"(set {DATABASE_URL_ENV} to a postgresql:// DSN)"
        )
    raise ValueError(missing)


def validate_customer_production(config: DatabaseConfig) -> None:
    """Fail closed when a customer-production boot would run on SQLite.

    ``VIDEO_REPLICA_CUSTOMER_PRODUCTION`` marks the customer boundary. In that
    mode a missing URL or a SQLite target must abort startup instead of
    silently falling back to the single-file internal runtime. A leftover
    ``VIDEO_REPLICA_DB_PATH`` alongside a PG URL is likewise rejected: an
    ambiguous customer-production configuration is an error, not a hint.
    """
    if not _is_customer_production():
        return
    if config.mode is not DatabaseMode.POSTGRESQL:
        raise RuntimeError(
            "customer production requires PostgreSQL: set "
            f"{DATABASE_URL_ENV} to a postgresql:// DSN (SQLite startup is rejected)"
        )
    leftover_db_path = os.environ.get(DB_PATH_ENV, "").strip()
    if leftover_db_path:
        raise RuntimeError(
            "customer production must not set the legacy "
            f"{DB_PATH_ENV}; remove it and keep only {DATABASE_URL_ENV} "
            "(ambiguous database configuration is rejected)"
        )


_pool: ConnectionPool | None = None
_pool_lock = threading.Lock()


def _pool_bounds() -> tuple[int, int]:
    def _int_env(name: str, default: int) -> int:
        raw = os.environ.get(name, "").strip()
        try:
            return int(raw) if raw else default
        except ValueError:
            logger.warning("invalid %s=%r, using default %d", name, raw, default)
            return default

    pool_min = max(0, _int_env(POOL_MIN_ENV, DEFAULT_POOL_MIN))
    pool_max = max(pool_min, _int_env(POOL_MAX_ENV, DEFAULT_POOL_MAX))
    return pool_min, pool_max


def get_pg_pool() -> ConnectionPool:
    """Return the process-wide PG connection pool (lazily created)."""
    global _pool
    with _pool_lock:
        if _pool is None or _pool.closed:
            config = resolve_database_config()
            if config.mode is not DatabaseMode.POSTGRESQL or not config.dsn:
                raise RuntimeError(
                    "PG pool requested but the resolved database mode is not PostgreSQL"
                )
            pool_min, pool_max = _pool_bounds()
            pool = ConnectionPool(
                config.dsn,
                min_size=pool_min,
                max_size=pool_max,
                open=True,
                name="video-replica-pg",
            )
            _pool = pool
        return _pool


def close_pg_pool() -> None:
    """Close the pool (used by tests and graceful shutdown)."""
    global _pool
    with _pool_lock:
        if _pool is not None and not _pool.closed:
            _pool.close()
        _pool = None


@contextmanager
def pg_transaction(*, isolation: str | None = None) -> Iterator[psycopg.Connection]:
    """Run a transaction on a pooled connection.

    ``isolation="SERIALIZABLE"`` is the replacement for SQLite's
    ``BEGIN IMMEDIATE`` write fencing: committers that lose a concurrent
    write race fail with ``psycopg.errors.SerializationFailure`` and the
    caller decides how to retry.
    """
    pool = get_pg_pool()
    with pool.connection() as conn:
        try:
            with conn.transaction():
                if isolation is not None:
                    conn.execute(f"SET TRANSACTION ISOLATION LEVEL {isolation}")
                yield conn
        except Exception:
            # pool.connection() returns the connection to the pool; a failed
            # transaction was already rolled back by conn.transaction().
            raise


def pg_server_now() -> datetime:
    """Return PostgreSQL server time (the only trusted clock, SES-01)."""
    pool = get_pg_pool()
    with pool.connection() as conn:
        value = _fetch_scalar(conn, "SELECT now()")
        return datetime.fromisoformat(str(value))


def check_pg_ready() -> PgReadyInfo:
    """Ready check for API/Worker startup: pool warm-up + server round-trip."""
    validate_customer_production(resolve_database_config())
    pool = get_pg_pool()
    with pool.connection() as conn:
        server_now = datetime.fromisoformat(str(_fetch_scalar(conn, "SELECT now()")))
        _fetch_scalar(conn, "SELECT 1")
    dsn = resolve_database_config().dsn or ""
    return PgReadyInfo(dsn=dsn, server_now=server_now, pool_size=pool.max_size)
