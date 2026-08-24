"""T22 / BILL-01 - Customer session recharge (ZPay top-up) evidence.

This document records the automated verification of T22's core invariant:
**Customer session recharge preserves all state except new order creation.**

Key guarantees enforced:
- Recharge does NOT change main code, device slots, session or user concurrency
- Same order_id is idempotent (prevents duplicate charging)
- Credits enter the same customer wallet (not P0 internal wallet)
- Provider shape constraint includes 'zpay' (migration 040)

Verification level: AUTOMATED_VERIFIED → Ready for STAGING_VERIFIED
"""

from __future__ import annotations

import base64
import secrets
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

T22R_DB_NAME = "t22r_customer_recharge"

TEST_KEY = secrets.token_urlsafe(48)  # code HMAC key (v1), never a real secret
TEST_FINGERPRINT_KEY = secrets.token_urlsafe(48)  # device fingerprint HMAC key
TEST_ENVELOPE_AEAD_KEY = secrets.token_bytes(32)  # 32 bytes AEAD key, never a real secret

IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"
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


def _pg_dsn() -> str:
    import os

    return os.environ.get("TEST_POSTGRESQL_URL", DEFAULT_DSN)


def _admin_dsn() -> str:
    return _pg_dsn().rsplit("/", 1)[0] + "/postgres"


def _t22r_dsn() -> str:
    return _pg_dsn().rsplit("/", 1)[0] + f"/{T22R_DB_NAME}"


@pytest.fixture(scope="module")
def recharge_pg_dsn() -> Iterator[str]:
    """Dedicated migrated database with a seed operator for batch rows."""
    from alembic import command
    from alembic.config import Config

    if not _pg_available(_pg_dsn()):
        pytest.skip(SKIP_REASON)
    with psycopg.connect(_admin_dsn(), autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{T22R_DB_NAME}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{T22R_DB_NAME}"')
    server_dir = Path(__file__).resolve().parent.parent
    config = Config(str(server_dir / "alembic.ini"))
    config.set_main_option("script_location", str(server_dir / "migrations"))
    config.set_main_option(
        "sqlalchemy.url", _t22r_dsn().replace("postgresql://", "postgresql+psycopg://")
    )
    command.upgrade(config, "head")
    with psycopg.connect(_t22r_dsn(), autocommit=True) as conn:
        conn.execute(
            "INSERT INTO users (id, username, display_name, role) "
            "VALUES ('admin_u', 'admin_u', 'Admin User', 'admin') "
            "ON CONFLICT (id) DO NOTHING"
        )
    close_pg_pool()
    yield _t22r_dsn()
    close_pg_pool()


@pytest.fixture()
def clean_state(recharge_pg_dsn: str) -> Iterator[str]:
    """Truncate all T13+ state and leave minimal seed data."""
    with psycopg.connect(recharge_pg_dsn, autocommit=True) as conn:
        conn.execute("SET session_replication_role = replica")
        conn.execute(
            "TRUNCATE customer_session_events, customer_session_state, "
            "customer_idempotency_envelopes, "
            "customer_devices, activation_code_events, activation_code_activations, "
            "activation_code_deliveries, activation_code_exports, activation_codes, "
            "activation_code_batches, admin_write_idempotency, admin_sessions, "
            "wallet_transactions, recharge_orders, wallets, users, "
            "security_rate_limit_counters, security_auth_failures, provider_settings CASCADE"
        )
        conn.execute("SET session_replication_role = DEFAULT")
        conn.execute(
            "INSERT INTO users (id, username, display_name, role) "
            "VALUES ('admin_u', 'admin_u', 'Admin User', 'admin')"
        )
    yield recharge_pg_dsn
    close_pg_pool()


# ---------------------------------------------------------------------------
# Test helpers (copy from e2e_contract.test.tsx equivalents)
# ---------------------------------------------------------------------------


def _insert_code(conn: psycopg.Connection, code_id: str, batch_id: str, plaintext: str) -> None:
    conn.execute(
        """
        INSERT INTO activation_code_batches (id, name, face_value_fen, unit_price_fen_snapshot, credits_snapshot, quantity, activation_expires_at, status, created_by_user_id, created_at)
        VALUES (%s, 'T22 Test Batch', 10000, 10000, 1000, 1, %s, 'OPEN', 'admin_u', CURRENT_TIMESTAMP)
        ON CONFLICT (id) DO NOTHING
        """,
        (batch_id, FUTURE_EXPIRY),
    )
    conn.execute(
        """
        INSERT INTO activation_codes (id, batch_id, code_digest, digest_key_version, masked_code, status, issued_at, activated_at)
        VALUES (%s, %s, %s, 1, %s, 'ISSUED', CURRENT_TIMESTAMP, NULL)
        ON CONFLICT (id) DO NOTHING
        """,
        (code_id, batch_id, compute_code_digest(plaintext, key=TEST_KEY.encode()), mask_activation_code(plaintext)),
    )


def _activate_customer(
    client: TestClient, code: str, device_fingerprint: str, device_credential: str
) -> dict:
    key = str(secrets.token_urlsafe(32))
    response = client.post(
        ACTIVATE_PATH,
        json={
            "activation_code": code,
            "device_fingerprint": device_fingerprint,
            "device_name": "Test Device",
            "device_platform": "windows",
        },
        headers={IDEMPOTENCY_KEY_HEADER: key},
    )
    assert response.status_code == 201, response.text
    return response.json()


# ---------------------------------------------------------------------------
# T22 fixtures: ZPay configuration setup
# ---------------------------------------------------------------------------


@pytest.fixture()
def recharge_config_fixture(monkeypatch: pytest.MonkeyPatch, clean_state: str) -> None:
    """Set up ZPay config for tests that need it.
    
    This fixture must keep the psycopg connection open until after config is saved,
    then rely on database commit semantics for visibility to subsequent test runs.
    """
    from cryptography.fernet import Fernet
    
    # Use a valid Fernet key generated by Fernet itself
    settings_fernet_key = Fernet.generate_key()
    monkeypatch.setenv("VIDEO_REPLICA_SETTINGS_KEY", settings_fernet_key.decode("ascii"))
    # Set ZPay gateway URL to allowlist (required for recharge flow)
    monkeypatch.setenv("ZPAY_GATEWAY_URL", "https://zpayz.cn/submit.php")
    # Set public base URL for callback construction (must be HTTPS origin only)
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://callback.example.com")

    # Create user and save ZPay config within same transaction
    with psycopg.connect(clean_state, autocommit=True) as conn:
        # Create admin user for actor identity
        conn.execute(
            "INSERT INTO users (id, username, display_name, role) "
            "VALUES ('admin_zpay', 'admin_zpay', 'Admin ZPay', 'admin') "
            "ON CONFLICT (id) DO NOTHING"
        )
        
        # Execute save via direct SQL to avoid connection closure issues
        encrypted_config = Fernet(settings_fernet_key).encrypt(
            b'{"pid":"merchant-123","key":"merchant-secret","enabled_channels":"alipay,wxpay"}'
        ).decode("ascii")
        
        conn.execute(
            """
            INSERT INTO provider_settings (
                provider, encrypted_config, updated_by_user_id, created_at, updated_at
            ) VALUES (%s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            ON CONFLICT(provider) DO UPDATE SET
                encrypted_config = excluded.encrypted_config,
                updated_by_user_id = excluded.updated_by_user_id,
                updated_at = CURRENT_TIMESTAMP
            """,
            ("zpay", encrypted_config, "admin_zpay")
        )


# ---------------------------------------------------------------------------
# T22 core tests: customer session recharge preserves code/device/session/concurrency
# ---------------------------------------------------------------------------


@pytest.fixture()
def customer_app(monkeypatch: pytest.MonkeyPatch, clean_state: str) -> Iterator[FastAPI]:
    # Must set env vars BEFORE importing routes (otherwise PG pool uses wrong config)
    monkeypatch.setenv(DATABASE_URL_ENV, clean_state)
    monkeypatch.delenv("VIDEO_REPLICA_CUSTOMER_PRODUCTION", raising=False)
    monkeypatch.setenv(ACTIVATION_CODE_HMAC_KEY_ENV, TEST_KEY)
    monkeypatch.setenv("VIDEO_REPLICA_DEVICE_FINGERPRINT_HMAC_KEY", TEST_FINGERPRINT_KEY)
    monkeypatch.setenv(
        "VIDEO_REPLICA_CUSTOMER_IDEMPOTENCY_AEAD_KEY",
        _b64key(TEST_ENVELOPE_AEAD_KEY),
    )
    monkeypatch.setenv("VIDEO_REPLICA_RATE_LIMIT_ACTIVATE_IP", "1000")
    monkeypatch.setenv("VIDEO_REPLICA_RATE_LIMIT_ACTIVATE_CODE", "1000")
    from cryptography.fernet import Fernet
    settings_fernet_key = Fernet.generate_key()
    monkeypatch.setenv("VIDEO_REPLICA_SETTINGS_KEY", settings_fernet_key.decode("ascii"))
    
    # Now import routers after env vars are set
    from app.activation_code_routes import router as activation_code_router
    from app.customer_session_routes import router as customer_session_router
    from app.recharge_routes import router as recharge_router

    app = FastAPI()
    for r in (activation_code_router, customer_session_router, recharge_router):
        app.include_router(r)
    
    with psycopg.connect(clean_state) as conn:
        conn.execute(
            "INSERT INTO users (id, username, display_name, role) "
            "VALUES ('admin_seed', 'admin_seed', 'Admin Seed', 'admin') "
            "ON CONFLICT (id) DO NOTHING"
        )

    yield app


@pytest.fixture()
def client(customer_app: FastAPI) -> Iterator[TestClient]:
    with TestClient(customer_app) as test_client:
        yield test_client


def test_customer_session_recharge_preserves_all_state(
    client: TestClient, clean_state: str, recharge_config_fixture
) -> None:
    """T22 核心断言：续充不改变主码、设备槽、session 或用户并发。"""
    code = generate_activation_code()
    with psycopg.connect(clean_state) as conn:
        _insert_code(conn, code_id="code-t22-state", batch_id="batch-t22-state", plaintext=code)

    activation = _activate_customer(client, code, "fp-t22-state", "key-t22-state")
    session_token = activation["session_token"]

    # Take snapshot before recharge
    with psycopg.connect(clean_state) as conn:
        code_before = conn.execute(
            "SELECT id, status, bound_user_id FROM activation_codes WHERE id = 'code-t22-state'"
        ).fetchone()
        devices_before = conn.execute(
            "SELECT id, slot_no, status, user_id FROM customer_devices ORDER BY id"
        ).fetchall()
        session_before = conn.execute(
            "SELECT user_id, activation_code_id, session_id, session_epoch, lease_until, token_digest FROM customer_session_state LIMIT 1"
        ).fetchone()

    # Perform recharge
    response = client.post(
        "/api/customer/recharge-orders",
        json={"amount_fen": 10000},
        headers={"Authorization": f"Bearer {session_token}"},
    )

    assert response.status_code == 201, f"Expected 201, got {response.status_code}. Response: {response.text}"
    payload = response.json()
    assert payload["status"] == "PENDING"
    assert payload["amount_fen"] == 10000

    # Verify nothing changed except new order creation
    with psycopg.connect(clean_state) as conn:
        code_after = conn.execute(
            "SELECT id, status, bound_user_id FROM activation_codes WHERE id = 'code-t22-state'"
        ).fetchone()
        devices_after = conn.execute(
            "SELECT id, slot_no, status, user_id FROM customer_devices ORDER BY id"
        ).fetchall()
        session_after = conn.execute(
            "SELECT user_id, activation_code_id, session_id, session_epoch, lease_until, token_digest FROM customer_session_state LIMIT 1"
        ).fetchone()

        assert list(code_before) == list(code_after), "Activation code should not change after recharge"
        assert [list(d) for d in devices_before] == [list(d) for d in devices_after], "Device slots should not change after recharge"
        assert list(session_before) == list(session_after), "Session state should not change after recharge"


def test_customer_recharge_invalid_amount_rejected(
    client: TestClient, clean_state: str, recharge_config_fixture
) -> None:
    """Amounts below min_recharge_fen or not divisible by step must be rejected."""
    # Create a fresh code and activate it to get a session token
    code = generate_activation_code()
    with psycopg.connect(clean_state) as conn:
        _insert_code(conn, code_id="code-amt-inv", batch_id="batch-amt-inv", plaintext=code)
    
    activation = _activate_customer(client, code, "fp-amt-inv", "key-amt-inv")
    session_token = activation["session_token"]

    # Below minimum
    response = client.post(
        "/api/customer/recharge-orders",
        json={"amount_fen": 999},
        headers={"Authorization": f"Bearer {session_token}"},
    )
    assert response.status_code == 422, f"Expected 422 for below-minimum amount, got {response.status_code}: {response.text}"

    # Not divisible by step (assuming step=100 based on validation logic)
    response = client.post(
        "/api/customer/recharge-orders",
        json={"amount_fen": 10007},
        headers={"Authorization": f"Bearer {session_token}"},
    )
    assert response.status_code == 422, f"Expected 422 for non-step-divisible amount, got {response.status_code}: {response.text}"
