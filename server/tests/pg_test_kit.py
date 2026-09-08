"""Shared PostgreSQL test foundation (CW-007).

Single home for the test-resource contract that every TEST-PG suite shares:

- DSN resolution and an unreachable-database **hard gate** (``pytest.fail``
  by default; skipping requires an explicit developer opt-in via
  ``VIDEO_REPLICA_TEST_ALLOW_PG_SKIP=1`` and never counts as evidence).
- A recorded test-database allowlist: create/drop helpers refuse any name
  outside the registered prefixes so a mistyped DSN can never truncate or
  drop a developer or production database.
- A repeatable two-customer / three-device seed scenario.
- A file lock that keeps the shared full-suite fixture single-suite.

The kit never falls back to SQLite and never creates non-``*_test``
databases.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import psycopg
import pytest

# The canonical local fixture (scripts/pg-fixture.sh start) maps host 5433
# to the container's 5432 with trust auth for user postgres.
DEFAULT_TEST_DSN = "postgresql://testuser:testpass@localhost:5433/customer_v3_test"

# Every database name any suite may create.  Create/drop helpers refuse
# names outside this list so cleanup can never touch a non-test database.
RECORDED_TEST_DATABASES: frozenset[str] = frozenset(
    {
        "t13_customer_activation_test",
        "customer_v3_test",
        "cw007_kit_alpha_test",
        "cw007_kit_beta_test",
        # Suites still doing their own admin CREATE/DROP with legacy names
        # lacking the _test suffix (rename + kit-helper adoption is owed by a
        # later CW before they may use create_test_database/drop_test_database):
        #   t11_activation_code_service, t34_chain_e2e,
        #   t13c_customer_activation_concurrency, t22r_customer_recharge
    }
)

SHARED_SUITE_DB = "customer_v3_test"

SHARED_SUITE_LOCK_PATH = Path(
    os.environ.get("VIDEO_REPLICA_TEST_SHARED_LOCK", "/tmp/video-replica-pg-shared-suite.lock")
)

ALLOW_PG_SKIP_ENV = "VIDEO_REPLICA_TEST_ALLOW_PG_SKIP"

SEED_USERS = ("cw007-cust-a", "cw007-cust-b")
SEED_DEVICES = (
    # (device_id, owner, code_key, code, slot, fingerprint)
    ("cw007-dev-a1", "cw007-cust-a", "A", "CW07-A-CODE", 1, "fp-cw007-a1"),
    ("cw007-dev-a2", "cw007-cust-a", "A", "CW07-A-CODE", 2, "fp-cw007-a2"),
    ("cw007-dev-b1", "cw007-cust-b", "B", "CW07-B-CODE", 1, "fp-cw007-b1"),
)

_FINGERPRINT_KEY = b"cw007-test-fingerprint-key"
_TOKEN_KEY = b"cw007-test-token-key"


class PgPreflightError(RuntimeError):
    """Raised when the PG test preflight rejects the requested resource."""


def resolve_test_dsn() -> str:
    """Return the test DSN from TEST_POSTGRESQL_URL or the local fixture."""
    return os.environ.get("TEST_POSTGRESQL_URL", DEFAULT_TEST_DSN)


def database_name_of(dsn: str) -> str:
    return dsn.rsplit("/", 1)[-1].split("?", 1)[0]


def admin_dsn_of(dsn: str) -> str:
    return dsn.rsplit("/", 1)[0] + "/postgres"


def assert_safe_test_database(name: str) -> None:
    """Refuse any database outside the recorded test allowlist."""
    if name in {"postgres", "template0", "template1"}:
        raise PgPreflightError(f"refusing to manage system database {name!r}")
    if not name.endswith("_test"):
        raise PgPreflightError(f"refusing to manage {name!r}: test databases must end with '_test'")
    if name not in RECORDED_TEST_DATABASES:
        raise PgPreflightError(
            f"database {name!r} is not in RECORDED_TEST_DATABASES; register it in "
            "server/tests/pg_test_kit.py before use"
        )


def pg_reachable(dsn: str, timeout: float = 3.0) -> bool:
    try:
        conn = psycopg.connect(dsn, connect_timeout=int(timeout))
    except Exception:
        return False
    conn.close()
    return True


def require_pg_or_explicit_skip(dsn: str | None = None) -> str:
    """Hard-gate: fail when PG is unreachable unless skipping is explicit.

    Returns the usable DSN.  With ``VIDEO_REPLICA_TEST_ALLOW_PG_SKIP=1``
    the caller may skip (developer fast-loop only — skipped suites never
    count toward PG acceptance evidence); any other configuration fails.
    """
    target = dsn or resolve_test_dsn()
    # CW-007: a hand-typed TEST_POSTGRESQL_URL must never point the suites
    # (and their admin DROP/CREATE cleanups) at a non-test database.
    assert_safe_test_database(database_name_of(target))
    try:
        conn = psycopg.connect(target, connect_timeout=3)
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}".replace("\n", " ")[:200]
        reason = (
            "PostgreSQL test fixture is not reachable at "
            f"{target}; start it via scripts/pg-fixture.sh start ({detail})"
        )
        if os.environ.get(ALLOW_PG_SKIP_ENV) == "1":
            pytest.skip(reason + " (explicit skip via " + ALLOW_PG_SKIP_ENV + "=1)")
        pytest.fail(reason, pytrace=False)
        raise AssertionError("unreachable")  # pragma: no cover - pytest.fail exits
    conn.close()
    return target


def create_test_database(name: str, *, admin_dsn: str | None = None) -> str:
    """Recreate an allowlisted test database; return its DSN."""
    assert_safe_test_database(name)
    admin = admin_dsn or admin_dsn_of(resolve_test_dsn())
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{name}"')
    return admin.rsplit("/", 1)[0] + f"/{name}"


def drop_test_database(name: str, *, admin_dsn: str | None = None) -> None:
    """Drop an allowlisted test database if it exists."""
    assert_safe_test_database(name)
    admin = admin_dsn or admin_dsn_of(resolve_test_dsn())
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def upgrade_test_database_to_head(dsn: str) -> None:
    """Run Alembic ``upgrade head`` against a test database."""
    from alembic import command
    from alembic.config import Config

    server_dir = Path(__file__).resolve().parent.parent
    config = Config(str(server_dir / "alembic.ini"))
    config.set_main_option("script_location", str(server_dir / "migrations"))
    config.set_main_option(
        "sqlalchemy.url",
        dsn.replace("postgresql://", "postgresql+psycopg://"),
    )
    command.upgrade(config, "head")


def _digest(payload: str, key: bytes) -> str:
    return hashlib.sha256(key + payload.encode("utf-8")).hexdigest()


def seed_customer_scenario(dsn: str, *, reset: bool = True) -> dict[str, str]:
    """Seed the shared two-customer / three-device scenario, repeatably.

    Creates users ``cw007-cust-a``/``-b`` with one wallet each, one open
    activation batch per customer code, and three BOUND devices
    (A-slot1, A-slot2, B-slot1).  With ``reset=True`` every seeded table is
    truncated first so calling it twice on the same database yields exactly
    the same rows (the CW-007 repeatability contract).
    """
    tables = (
        "customer_session_events, customer_session_state, customer_idempotency_envelopes, "
        "customer_devices, activation_code_events, activation_code_activations, "
        "activation_code_deliveries, activation_code_exports, activation_codes, "
        "activation_code_batches, wallets, users"
    )
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("SET session_replication_role = replica")
        if reset:
            conn.execute(f"TRUNCATE {tables} CASCADE")
        conn.execute("SET session_replication_role = DEFAULT")

        conn.execute(
            "INSERT INTO users (id, username, display_name, role) VALUES "
            "('admin_u', 'admin_u', 'Admin User', 'admin'), "
            "('cw007-cust-a', 'cw007-cust-a', 'Customer A', 'customer'), "
            "('cw007-cust-b', 'cw007-cust-b', 'Customer B', 'customer') "
            "ON CONFLICT (id) DO NOTHING"
        )
        for key, code in (("A", "CW07-A-CODE"), ("B", "CW07-B-CODE")):
            batch_id = f"cw007-batch-{key}"
            conn.execute(
                "INSERT INTO activation_code_batches "
                "(id, name, face_value_fen, unit_price_fen_snapshot, credits_snapshot, "
                "quantity, activation_expires_at, status, created_by_user_id) VALUES "
                "(%s, %s, 1500, 1000, 100, 1, '2099-01-01T00:00:00+00:00', 'OPEN', 'admin_u') "
                "ON CONFLICT (id) DO NOTHING",
                (batch_id, f"cw007-batch-{key}"),
            )
            conn.execute(
                "INSERT INTO activation_codes "
                "(id, batch_id, code_digest, digest_key_version, masked_code, "
                "status, issued_at) VALUES "
                "(%s, %s, %s, 1, 'CW07-****', 'ISSUED', '2026-01-01T00:00:00+00:00') "
                "ON CONFLICT (id) DO NOTHING",
                (f"cw007-code-{key}", batch_id, _digest(code, b"cw007-code-key")),
            )
        for device_id, owner, code_key, code, slot, fingerprint in SEED_DEVICES:
            conn.execute(
                "INSERT INTO customer_devices "
                "(id, activation_code_id, user_id, slot_no, display_name, platform, "
                "fingerprint_hmac, fingerprint_key_version, token_digest, "
                "token_key_version, status) VALUES "
                "(%s, %s, %s, %s, %s, 'windows', %s, 1, %s, 1, 'BOUND') "
                "ON CONFLICT (id) DO NOTHING",
                (
                    device_id,
                    f"cw007-code-{code_key}",
                    owner,
                    slot,
                    f"CW007 {owner} dev{slot}",
                    _digest(fingerprint, _FINGERPRINT_KEY),
                    _digest(f"token-{device_id}", _TOKEN_KEY),
                ),
            )
        for user in SEED_USERS:
            conn.execute(
                "INSERT INTO wallets (user_id, available_credits, reserved_credits) "
                "VALUES (%s, 100, 0) ON CONFLICT (user_id) DO NOTHING",
                (user,),
            )

        return seed_scenario_facts(dsn)


def seed_scenario_facts(dsn: str) -> dict[str, int]:
    """Return the seeded row counts used by repeatability assertions."""
    with psycopg.connect(dsn) as conn:
        row = conn.execute(
            "SELECT "
            "(SELECT count(*) FROM users WHERE id LIKE 'cw007-%' OR id = 'admin_u'), "
            "(SELECT count(*) FROM customer_devices WHERE id LIKE 'cw007-dev-%'), "
            "(SELECT count(*) FROM activation_codes WHERE id LIKE 'cw007-code-%'), "
            "(SELECT count(*) FROM wallets WHERE user_id LIKE 'cw007-%')"
        ).fetchone()
        assert row is not None
        return {
            "users": row[0],
            "devices": row[1],
            "codes": row[2],
            "wallets": row[3],
        }


@contextmanager
def shared_suite_lock() -> Iterator[None]:
    # fcntl is POSIX-only; CI runs pytest on Linux only (windows-nsis builds
    # the desktop app without executing the Python suites).
    """Serialize suites that share the full-suite database (single suite)."""
    SHARED_SUITE_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    handle = os.open(SHARED_SUITE_LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        os.close(handle)
