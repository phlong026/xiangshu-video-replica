"""T13 / ACT-06 — first-activation concurrency and atomicity tests (fail-first).

PG integration cases over a dedicated migrated database (skip without the
fixture). The ACT-06 exit gate: *同一码、同用户名和同设备的 100 并发测试 —
仅一个成功事实、一个首充、一个当前 session*；SQLite / single-connection runs
are explicitly not acceptable evidence, so every case here runs against the
real PostgreSQL fixture with concurrent threads.

Locked behaviours:

- 100 threads, one barrier, one code, distinct Idempotency-Keys: exactly one
  201; the other 99 all answer the unified 400 ``ACTIVATION_UNAVAILABLE``;
  afterwards the database holds exactly one customer user, wallet, activation
  fact, slot-1 device, PAID order, CHARGE and session row (all-or-nothing);
- two threads sharing one Idempotency-Key and one body both succeed with the
  identical username / device token / session token and only one CHARGE
  (envelope recovery, §12.1);
- two threads racing the same device fingerprint with different codes leave
  one activation and one ``USER_ALREADY_ACTIVATED``;
- a business failure (suspended code) rolls the idempotency envelope back
  with the transaction, so the same key stays reusable;
- a colliding server-generated username is regenerated inside the same
  transaction instead of failing the activation (§12.1 note).
"""

from __future__ import annotations

import base64
import secrets
import threading
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.activation_code_service import (
    ACTIVATION_CODE_HMAC_KEY_ENV,
    compute_code_digest,
    generate_activation_code,
    mask_activation_code,
)
from app.db_pg import DATABASE_URL_ENV, close_pg_pool

DEFAULT_DSN = "postgresql://testuser:testpass@localhost:5433/customer_v3_test"
SKIP_REASON = "PostgreSQL fixture not reachable; start it via scripts/pg-fixture.sh start"

TEST_KEY = secrets.token_urlsafe(48)  # code HMAC key (v1), never a real secret
TEST_FINGERPRINT_KEY = secrets.token_urlsafe(48)  # device-fingerprint HMAC key
TEST_ENVELOPE_AEAD_KEY = secrets.token_bytes(32)  # 32 bytes, never a real secret

IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"
REPLAY_HEADER = "X-Idempotent-Replay"
ACTIVATE_PATH = "/api/customer/activate"
FUTURE_EXPIRY = "2099-01-01T00:00:00+00:00"


def _b64key(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _pg_available(dsn: str) -> bool:
    try:
        conn = psycopg.connect(dsn, connect_timeout=3)
        conn.close()
    except Exception:
        return False
    return True


T13C_DB_NAME = "t13c_customer_activation_concurrency"


def _pg_dsn() -> str:
    import os

    return os.environ.get("TEST_POSTGRESQL_URL", DEFAULT_DSN)


def _admin_dsn() -> str:
    return _pg_dsn().rsplit("/", 1)[0] + "/postgres"


def _t13c_dsn() -> str:
    return _pg_dsn().rsplit("/", 1)[0] + f"/{T13C_DB_NAME}"


@pytest.fixture(scope="module")
def concurrency_pg_dsn() -> Iterator[str]:
    """Dedicated migrated database with a seed operator for batch rows."""
    from alembic import command
    from alembic.config import Config

    if not _pg_available(_pg_dsn()):
        pytest.skip(SKIP_REASON)
    with psycopg.connect(_admin_dsn(), autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{T13C_DB_NAME}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{T13C_DB_NAME}"')
    server_dir = Path(__file__).resolve().parent.parent
    config = Config(str(server_dir / "alembic.ini"))
    config.set_main_option("script_location", str(server_dir / "migrations"))
    config.set_main_option(
        "sqlalchemy.url", _t13c_dsn().replace("postgresql://", "postgresql+psycopg://")
    )
    command.upgrade(config, "head")
    with psycopg.connect(_t13c_dsn(), autocommit=True) as conn:
        conn.execute(
            "INSERT INTO users (id, username, display_name, role) "
            "VALUES ('admin_u', 'admin_u', 'Admin User', 'admin') "
            "ON CONFLICT (id) DO NOTHING"
        )
    try:
        yield _t13c_dsn()
    finally:
        with psycopg.connect(_admin_dsn(), autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{T13C_DB_NAME}" WITH (FORCE)')


@pytest.fixture()
def clean_state(concurrency_pg_dsn: str) -> Iterator[str]:
    close_pg_pool()
    with psycopg.connect(concurrency_pg_dsn, autocommit=True) as conn:
        conn.execute(
            "TRUNCATE customer_session_events, customer_session_state, "
            "customer_idempotency_envelopes, "
            "customer_devices, activation_code_events, activation_code_activations, "
            "activation_code_deliveries, activation_code_exports, activation_codes, "
            "activation_code_batches, admin_write_idempotency, admin_sessions, "
            "wallet_transactions, recharge_orders, wallets, users CASCADE"
        )
        conn.execute(
            "INSERT INTO users (id, username, display_name, role) "
            "VALUES ('admin_u', 'admin_u', 'Admin User', 'admin')"
        )
    yield concurrency_pg_dsn
    close_pg_pool()


@pytest.fixture()
def customer_app(monkeypatch: pytest.MonkeyPatch, clean_state: str) -> Iterator[FastAPI]:
    from app.activation_code_routes import router as activation_code_router

    app = FastAPI()
    app.include_router(activation_code_router)
    monkeypatch.setenv(DATABASE_URL_ENV, clean_state)
    monkeypatch.delenv("VIDEO_REPLICA_CUSTOMER_PRODUCTION", raising=False)
    monkeypatch.setenv(ACTIVATION_CODE_HMAC_KEY_ENV, TEST_KEY)
    monkeypatch.setenv("VIDEO_REPLICA_DEVICE_FINGERPRINT_HMAC_KEY", TEST_FINGERPRINT_KEY)
    monkeypatch.setenv(
        "VIDEO_REPLICA_CUSTOMER_IDEMPOTENCY_AEAD_KEY", _b64key(TEST_ENVELOPE_AEAD_KEY)
    )
    yield app


@pytest.fixture()
def client(customer_app: FastAPI) -> Iterator[TestClient]:
    with TestClient(customer_app) as test_client:
        yield test_client


def _insert_code(
    conn: psycopg.Connection,
    *,
    code_id: str,
    batch_id: str,
    plaintext: str,
    status: str = "ISSUED",
) -> None:
    from datetime import UTC, datetime

    conn.execute(
        "INSERT INTO activation_code_batches "
        "(id, name, face_value_fen, unit_price_fen_snapshot, credits_snapshot, "
        "quantity, activation_expires_at, status, created_by_user_id) "
        "VALUES (%s, %s, 1500, 1000, 100, 1, %s, 'OPEN', 'admin_u')",
        (batch_id, f"批次 {batch_id}", FUTURE_EXPIRY),
    )
    now = datetime.now(UTC).replace(microsecond=0).isoformat()
    status_columns = {
        "ISSUED": (now, None, None, None, None),
        "SUSPENDED": (now, now, now, None, None),
    }
    issued_at, activated_at, suspended_at, revoked_at, expired_at = status_columns[status]
    # 027 couples bound_user_id with activated_at: a SUSPENDED code is a
    # previously activated code, so it carries a bound seed user (an employee,
    # never counted by the customer-role assertions).
    bound_user_id = None
    if status == "SUSPENDED":
        bound_user_id = "seed-bound-user"
        conn.execute(
            "INSERT INTO users (id, username, display_name, role) "
            "VALUES ('seed-bound-user', 'seed-bound-user', 'Seed', 'employee') "
            "ON CONFLICT (id) DO NOTHING"
        )
    conn.execute(
        "INSERT INTO activation_codes "
        "(id, batch_id, code_digest, digest_key_version, masked_code, status, "
        "bound_user_id, issued_at, activated_at, suspended_at, revoked_at, expired_at) "
        "VALUES (%s, %s, %s, 1, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            code_id,
            batch_id,
            compute_code_digest(plaintext, key=TEST_KEY.encode()),
            mask_activation_code(plaintext),
            status,
            bound_user_id,
            issued_at,
            activated_at,
            suspended_at,
            revoked_at,
            expired_at,
        ),
    )


def _activate_body(code: str, fingerprint: str) -> dict[str, str]:
    return {
        "activation_code": code,
        "device_fingerprint": fingerprint,
        "device_name": "并发设备",
        "device_platform": "windows",
    }


def _post_activate(client: TestClient, code: str, fingerprint: str, key: str) -> object:
    return client.post(
        ACTIVATE_PATH,
        json=_activate_body(code, fingerprint),
        headers={IDEMPOTENCY_KEY_HEADER: key},
    )


def _count(conn: psycopg.Connection, sql: str, params: tuple | list = ()) -> int:
    return int(conn.execute(sql, params).fetchone()[0])


# ---------------------------------------------------------------------------
# ACT-06: 100 concurrent activations of one code
# ---------------------------------------------------------------------------


def test_hundred_concurrent_same_code_exactly_one_activation(
    client: TestClient, clean_state: str
) -> None:
    plaintext = generate_activation_code()
    with psycopg.connect(clean_state) as conn:
        _insert_code(conn, code_id="code-race", batch_id="batch-race", plaintext=plaintext)

    threads_count = 100
    barrier = threading.Barrier(threads_count)
    results: list[tuple[int, str]] = []
    results_lock = threading.Lock()

    def worker(index: int) -> None:
        barrier.wait()
        response = _post_activate(client, plaintext, f"fp-race-{index}", f"key-race-{index}")
        with results_lock:
            results.append((response.status_code, response.text))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(threads_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)
        assert not thread.is_alive(), "a concurrent activation worker hung"

    successes = [body for status, body in results if status == 201]
    unavailable = [status for status, _ in results if status == 400]
    assert len(results) == threads_count
    assert len(successes) == 1, results
    assert len(unavailable) == threads_count - 1
    import json as _json

    winner = _json.loads(successes[0])
    assert winner["username"].startswith("customer-")

    with psycopg.connect(clean_state) as conn:
        # All-or-nothing: exactly one of every fact in the activation chain.
        assert _count(conn, "SELECT COUNT(*) FROM users WHERE role = 'customer'") == 1
        assert _count(conn, "SELECT COUNT(*) FROM wallets") == 1
        assert _count(conn, "SELECT COUNT(*) FROM activation_code_activations") == 1
        assert _count(conn, "SELECT COUNT(*) FROM customer_devices") == 1
        assert (
            _count(conn, "SELECT COUNT(*) FROM recharge_orders WHERE provider = 'activation_code'")
            == 1
        )
        assert _count(conn, "SELECT COUNT(*) FROM wallet_transactions") == 1
        assert _count(conn, "SELECT COUNT(*) FROM customer_session_state") == 1
        assert _count(conn, "SELECT COUNT(*) FROM activation_codes WHERE status = 'ACTIVE'") == 1
        wallet = conn.execute("SELECT available_credits FROM wallets").fetchone()
        assert wallet is not None and wallet[0] == 100


# ---------------------------------------------------------------------------
# Concurrent same-key writers: envelope recovery keeps one identity
# ---------------------------------------------------------------------------


def test_concurrent_same_key_same_body_returns_same_identity(
    client: TestClient, clean_state: str
) -> None:
    plaintext = generate_activation_code()
    with psycopg.connect(clean_state) as conn:
        _insert_code(conn, code_id="code-key", batch_id="batch-key", plaintext=plaintext)

    barrier = threading.Barrier(2)
    results: list[tuple[int, dict]] = []
    results_lock = threading.Lock()

    def worker() -> None:
        barrier.wait()
        response = _post_activate(client, plaintext, "fp-same-key", "key-same")
        with results_lock:
            results.append((response.status_code, response.json()))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
        assert not thread.is_alive()

    assert [status for status, _ in results] == [201, 201], results
    first, second = results[0][1], results[1][1]
    assert first["username"] == second["username"]
    assert first["device_token"] == second["device_token"]
    assert first["session_token"] == second["session_token"]

    with psycopg.connect(clean_state) as conn:
        assert _count(conn, "SELECT COUNT(*) FROM activation_code_activations") == 1
        assert _count(conn, "SELECT COUNT(*) FROM wallet_transactions WHERE type = 'CHARGE'") == 1
        assert _count(conn, "SELECT COUNT(*) FROM customer_session_state") == 1


# ---------------------------------------------------------------------------
# Concurrent second code on the same fingerprint
# ---------------------------------------------------------------------------


def test_concurrent_second_code_same_fingerprint_one_success(
    client: TestClient, clean_state: str
) -> None:
    first_code = generate_activation_code()
    second_code = generate_activation_code()
    with psycopg.connect(clean_state) as conn:
        _insert_code(conn, code_id="code-a", batch_id="batch-a", plaintext=first_code)
        _insert_code(conn, code_id="code-b", batch_id="batch-b", plaintext=second_code)

    barrier = threading.Barrier(2)
    results: list[tuple[int, str]] = []
    results_lock = threading.Lock()

    def worker(code: str, key: str) -> None:
        barrier.wait()
        response = _post_activate(client, code, "fp-shared", key)
        with results_lock:
            results.append((response.status_code, response.text))

    threads = [
        threading.Thread(target=worker, args=(first_code, "key-a")),
        threading.Thread(target=worker, args=(second_code, "key-b")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    statuses = sorted(status for status, _ in results)
    assert statuses == [201, 409], results
    conflict_body = [body for status, body in results if status == 409][0]
    assert "USER_ALREADY_ACTIVATED" in conflict_body

    with psycopg.connect(clean_state) as conn:
        assert _count(conn, "SELECT COUNT(*) FROM activation_code_activations") == 1
        assert (
            _count(conn, "SELECT COUNT(*) FROM recharge_orders WHERE provider = 'activation_code'")
            == 1
        )
        # The losing code is untouched.
        assert _count(conn, "SELECT COUNT(*) FROM activation_codes WHERE status = 'ISSUED'") == 1


# ---------------------------------------------------------------------------
# Business failure rolls the envelope back: the key stays reusable
# ---------------------------------------------------------------------------


def test_failed_attempt_releases_idempotency_key(client: TestClient, clean_state: str) -> None:
    suspended_code = generate_activation_code()
    valid_code = generate_activation_code()
    with psycopg.connect(clean_state) as conn:
        _insert_code(
            conn,
            code_id="code-susp",
            batch_id="batch-susp",
            plaintext=suspended_code,
            status="SUSPENDED",
        )
        _insert_code(conn, code_id="code-ok", batch_id="batch-ok", plaintext=valid_code)

    failed = _post_activate(client, suspended_code, "fp-retry", "key-recycled")
    assert failed.status_code == 400, failed.text
    assert failed.json()["detail"]["code"] == "ACTIVATION_UNAVAILABLE"

    # The suspended attempt rolled back completely (envelope included), so
    # the very same key can carry the corrected retry to success.
    retried = _post_activate(client, valid_code, "fp-retry", "key-recycled")
    assert retried.status_code == 201, retried.text

    with psycopg.connect(clean_state) as conn:
        assert _count(conn, "SELECT COUNT(*) FROM activation_code_activations") == 1
        assert _count(conn, "SELECT COUNT(*) FROM wallet_transactions WHERE type = 'CHARGE'") == 1


# ---------------------------------------------------------------------------
# Username collisions regenerate inside the same transaction (§12.1)
# ---------------------------------------------------------------------------


def test_username_collision_regenerates_in_transaction(
    client: TestClient,
    clean_state: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plaintext = generate_activation_code()
    with psycopg.connect(clean_state) as conn:
        _insert_code(conn, code_id="code-name", batch_id="batch-name", plaintext=plaintext)
        # The first generated username will collide with this seed row.
        conn.execute(
            "INSERT INTO users (id, username, display_name, role) "
            "VALUES ('seed-user', 'customer-collide0', 'Seed', 'customer')"
        )

    import app.activation_code_routes as routes

    calls = iter(["customer-collide0", "customer-fresh0"])
    monkeypatch.setattr(routes, "_generate_customer_username", lambda: next(calls), raising=True)

    response = _post_activate(client, plaintext, "fp-name", "key-name")
    assert response.status_code == 201, response.text
    assert response.json()["username"] == "customer-fresh0"

    with psycopg.connect(clean_state) as conn:
        assert _count(conn, "SELECT COUNT(*) FROM users WHERE role = 'customer'") == 2
        assert _count(conn, "SELECT COUNT(*) FROM activation_code_activations") == 1
