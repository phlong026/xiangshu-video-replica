"""T34 — admin customer session API tests (adapted to the 029 model).

Tests cover:
- GET /api/control/customers/{user_id}/sessions — list the live session state
- Pagination (limit/offset) and device-status filtering
- Admin/auditor role access control

The fixture database is migrated with alembic upgrade head (the T23
precedent): the 029 model keeps exactly one live session row per user in
``customer_session_state`` joined to the bound ``customer_devices`` row.
"""

from __future__ import annotations

import os
import secrets
from collections.abc import Iterator
from pathlib import Path

# Set HMAC key before importing app modules
os.environ.setdefault(
    "VIDEO_REPLICA_ADMIN_SESSION_HMAC_KEY",
    "test-key-for-t34-session-tests-minimum-48-bytes-long-1234567890",
)

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.admin_auth_routes import (
    ADMIN_CSRF_HEADER,
    ADMIN_SESSION_HMAC_KEY_ENV,
    issue_exchange_credential,
)
from app.db_pg import DATABASE_URL_ENV, close_pg_pool

DEFAULT_DSN = "postgresql://testuser:testpass@localhost:5433/customer_v3_test"
SKIP_REASON = "PostgreSQL fixture not reachable; start it via scripts/pg-fixture.sh start"

T34_DB_NAME = "t34_admin_sessions_test"

TEST_ADMIN_SESSION_KEY = secrets.token_urlsafe(48)

FUTURE_EXPIRY = "2099-01-01T00:00:00+00:00"
SESSIONS_PATH = "/api/control/customers/{user_id}/sessions"


def _pg_dsn() -> str:
    return os.environ.get("TEST_POSTGRESQL_URL", DEFAULT_DSN)


def _admin_dsn() -> str:
    return _pg_dsn().rsplit("/", 1)[0] + "/postgres"


def _t34_dsn() -> str:
    return _pg_dsn().rsplit("/", 1)[0] + f"/{T34_DB_NAME}"


def _pg_available(dsn: str) -> bool:
    try:
        conn = psycopg.connect(dsn, connect_timeout=3)
        conn.close()
    except Exception:
        return False
    return True


# ---------------------------------------------------------------------------
# PostgreSQL integration (dedicated migrated fixture database)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def sessions_dsn() -> Iterator[str]:
    from alembic import command
    from alembic.config import Config

    if not _pg_available(_pg_dsn()):
        pytest.skip(SKIP_REASON)
    with psycopg.connect(_admin_dsn(), autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{T34_DB_NAME}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{T34_DB_NAME}"')
    server_dir = Path(__file__).resolve().parent.parent
    config = Config(str(server_dir / "alembic.ini"))
    config.set_main_option("script_location", str(server_dir / "migrations"))
    config.set_main_option(
        "sqlalchemy.url", _t34_dsn().replace("postgresql://", "postgresql+psycopg://")
    )
    command.upgrade(config, "head")
    try:
        yield _t34_dsn()
    finally:
        with psycopg.connect(_admin_dsn(), autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{T34_DB_NAME}" WITH (FORCE)')


def _seed_customer_with_session(conn: psycopg.Connection) -> None:
    """Activation chain (batch -> code -> activation) + device + live session."""
    conn.execute(
        "INSERT INTO activation_code_batches "
        "(id, name, face_value_fen, unit_price_fen_snapshot, credits_snapshot, "
        " quantity, activation_expires_at, status, created_by_user_id) "
        "VALUES ('batch-cu', 'batch-cu', 1500, 1000, 100, 1, "
        f"'{FUTURE_EXPIRY}', 'OPEN', 'admin_u')"
    )
    conn.execute(
        "INSERT INTO recharge_orders "
        "(id, user_id, merchant_order_no, provider, status, pricing_scope, "
        " base_unit_price_fen_snapshot, charged_unit_price_fen_snapshot, "
        " min_recharge_fen_snapshot, recharge_step_fen_snapshot, amount_fen, credits, paid_at) "
        "VALUES ('dummy-activation-order', 'customer_u', 'DUMMY-activation-order', "
        "'admin_adjustment', 'PAID', 'CUSTOMER_STANDARD', "
        "1000, 1000, 10000, 1000, 1000, 1, now())"
    )
    conn.execute(
        "INSERT INTO activation_codes "
        "(id, batch_id, code_digest, digest_key_version, masked_code, status, "
        " issued_at, bound_user_id, activated_at) "
        "VALUES ('code-cu', 'batch-cu', 'digest-cu', 1, 'XS04-****', "
        "'ACTIVE', '2026-01-01T00:00:00+00:00', 'customer_u', '2026-01-01T00:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO activation_code_activations "
        "(id, code_id, user_id, first_device_id, recharge_order_id) "
        "VALUES ('act-cu', 'code-cu', 'customer_u', NULL, 'dummy-activation-order')"
    )
    conn.execute(
        "INSERT INTO customer_devices "
        "(id, activation_code_id, user_id, slot_no, display_name, platform, "
        " fingerprint_hmac, fingerprint_key_version, token_digest, token_key_version, "
        " status, bound_at) "
        "VALUES ('device-1', 'code-cu', 'customer_u', 1, '测试设备', 'windows', "
        "'fp-hmac-1', 1, 'tok-digest-1', 1, 'BOUND', '2026-08-01T00:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO customer_session_state "
        "(user_id, activation_code_id, device_id, session_id, token_digest, "
        " session_epoch, lease_until) "
        "VALUES ('customer_u', 'code-cu', 'device-1', 'session-1', 'session-tok-digest-1', "
        "3, '2099-06-01T00:00:00+00:00')"
    )
    conn.commit()


@pytest.fixture()
def route_state(sessions_dsn: str) -> Iterator[str]:
    close_pg_pool()
    with psycopg.connect(_t34_dsn(), autocommit=True) as conn:
        conn.execute("SET session_replication_role = replica")
        conn.execute(
            "TRUNCATE admin_adjustments, admin_device_events, "
            "device_pairing_requests, customer_session_events, "
            "customer_session_state, customer_idempotency_envelopes, "
            "customer_devices, activation_code_events, activation_code_activations, "
            "activation_code_deliveries, activation_code_exports, activation_codes, "
            "activation_code_batches, admin_write_idempotency, admin_sessions, "
            "wallet_transactions, recharge_orders, wallets, users CASCADE"
        )
        conn.execute("SET session_replication_role = DEFAULT")
        conn.execute(
            "INSERT INTO runtime_settings "
            "(id, max_generation_count_per_batch, max_concurrent_h3_tasks, "
            " internal_base_unit_price_fen, min_recharge_fen, recharge_step_fen) "
            "VALUES (1, 4, 2, 1000, 10000, 1000)"
        )
        conn.execute(
            "INSERT INTO users (id, username, display_name, role) VALUES "
            "('admin_u', 'admin_u', 'Admin User', 'admin'), "
            "('auditor_u', 'auditor_u', 'Auditor User', 'auditor'), "
            "('customer_u', 'customer_u', 'Customer User', 'user')"
        )
        _seed_customer_with_session(conn)
    yield sessions_dsn
    close_pg_pool()


@pytest.fixture()
def admin_app(monkeypatch: pytest.MonkeyPatch, route_state: str) -> Iterator[FastAPI]:
    from app.admin_auth_routes import router as admin_auth_router
    from app.admin_runtime_routes import router as admin_runtime_router
    from app.admin_session_routes import router as admin_session_router

    app = FastAPI()
    app.include_router(admin_auth_router)
    app.include_router(admin_session_router)
    app.include_router(admin_runtime_router)
    monkeypatch.setenv(DATABASE_URL_ENV, route_state)
    monkeypatch.delenv("VIDEO_REPLICA_CUSTOMER_PRODUCTION", raising=False)
    monkeypatch.setenv(ADMIN_SESSION_HMAC_KEY_ENV, TEST_ADMIN_SESSION_KEY)
    # The admin lanes must run on real admin sessions, never a dev identity
    # header shortcut (the T12/T16 fixture precedent).
    monkeypatch.delenv("VIDEO_REPLICA_AUTH_MODE", raising=False)
    monkeypatch.delenv("VIDEO_REPLICA_ALLOW_DEV_IDENTITY_HEADER", raising=False)
    yield app


@pytest.fixture()
def client(admin_app: FastAPI) -> Iterator[TestClient]:
    with TestClient(admin_app) as test_client:
        yield test_client


def _admin_session(client: TestClient, actor: str = "admin_u") -> dict[str, str]:
    """Exchange a real admin session cookie + CSRF header (the T12 pattern)."""
    response = client.post(
        "/api/control/admin/session/exchange",
        json={"credential": issue_exchange_credential(actor, ttl_seconds=3600)},
    )
    assert response.status_code == 201, response.text
    return {ADMIN_CSRF_HEADER: response.json()["csrf_token"]}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.pg
def test_list_customer_sessions_requires_admin(client: TestClient):
    """Unauthenticated requests must be rejected with 401."""
    response = client.get(SESSIONS_PATH.format(user_id="customer_u"))
    assert response.status_code == 401


@pytest.mark.pg
def test_list_customer_sessions_returns_live_session(client: TestClient):
    """The 029 model: one live session row per user with its device columns."""
    _admin_session(client)
    response = client.get(SESSIONS_PATH.format(user_id="customer_u"))
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 1
    assert data["limit"] == 20
    assert data["offset"] == 0
    item = data["items"][0]
    assert item["session_id"] == "session-1"
    assert item["user_id"] == "customer_u"
    assert item["username"] == "customer_u"
    assert item["device_id"] == "device-1"
    assert item["session_epoch"] == 3
    assert item["device_name"] == "测试设备"
    assert item["platform"] == "windows"
    assert item["slot_no"] == 1
    assert item["device_status"] == "BOUND"


@pytest.mark.pg
def test_list_customer_sessions_returns_empty_for_unknown_user(client: TestClient):
    """A customer without a live session row should return an empty list."""
    _admin_session(client)
    response = client.get(SESSIONS_PATH.format(user_id="no-such-user"))
    assert response.status_code == 200
    data = response.json()
    assert data["items"] == []
    assert data["total"] == 0


@pytest.mark.pg
def test_list_customer_sessions_filters_by_device_status(client: TestClient):
    """status filters on the bound device status (BOUND/UNBOUND/REVOKED)."""
    _admin_session(client)
    bound = client.get(SESSIONS_PATH.format(user_id="customer_u"), params={"status": "BOUND"})
    assert bound.status_code == 200
    assert bound.json()["total"] == 1

    revoked = client.get(SESSIONS_PATH.format(user_id="customer_u"), params={"status": "REVOKED"})
    assert revoked.status_code == 200
    assert revoked.json()["total"] == 0


@pytest.mark.pg
def test_auditor_can_list_sessions(client: TestClient):
    """Auditors should be able to list sessions (read-only)."""
    _admin_session(client, "auditor_u")
    response = client.get(SESSIONS_PATH.format(user_id="customer_u"))
    assert response.status_code == 200
    assert response.json()["total"] == 1


@pytest.mark.pg
def test_list_customer_sessions_filters_out_expired_lease(client: TestClient):
    """Logout/revocation/expiry keep the row but pull the lease into the
    past; the endpoint must not report it as a live session (Codex review
    P2 on the admin session API).

    The seeded row is created at runtime, so the UPDATE also rewinds
    created_at to keep the 029 ``lease_after_created`` CHECK satisfied —
    the real logout path uses GREATEST(now, created_at+1us) for the same
    reason."""
    _admin_session(client)
    with psycopg.connect(_t34_dsn(), autocommit=True) as conn:
        conn.execute(
            "UPDATE customer_session_state "
            "SET lease_until = to_char(now() - interval '1 hour', "
            '  \'YYYY-MM-DD"T"HH24:MI:SS"+00:00"\'), '
            "created_at = to_char(now() - interval '2 hour', "
            '  \'YYYY-MM-DD"T"HH24:MI:SS"+00:00"\') '
            "WHERE user_id = 'customer_u'"
        )
    response = client.get(SESSIONS_PATH.format(user_id="customer_u"))
    assert response.status_code == 200
    data = response.json()
    assert data["items"] == []
    assert data["total"] == 0


# ---------------------------------------------------------------------------
# Queue-mode switch (M4/M5 review M2 follow-up, PR #68 Codex P1): the
# production control-plane write path for fair_queue_enabled.
# ---------------------------------------------------------------------------

QUEUE_MODE_PATH = "/api/control/settings/queue-mode"


@pytest.mark.pg
def test_queue_mode_requires_admin_session(client: TestClient):
    """Unauthenticated requests must be rejected — no legacy identity path."""
    assert client.get(QUEUE_MODE_PATH).status_code == 401
    assert client.patch(QUEUE_MODE_PATH, json={"fair_queue_enabled": True}).status_code == 401


@pytest.mark.pg
def test_admin_reads_and_flips_queue_mode_with_audit(client: TestClient):
    """A cookie+CSRF admin flips the switch through the audited route; the
    runtime_settings row, the queue gate's own probe and the audit log all
    agree on the outcome."""
    from app.db_pg import pg_transaction
    from app.db_portable import BusinessConnection
    from app.generation import _fair_queue_enabled

    headers = _admin_session(client)
    initial = client.get(QUEUE_MODE_PATH, headers=headers)
    assert initial.status_code == 200, initial.text
    assert initial.json() == {"fair_queue_enabled": False}

    flipped = client.patch(QUEUE_MODE_PATH, headers=headers, json={"fair_queue_enabled": True})
    assert flipped.status_code == 200, flipped.text
    assert flipped.json() == {"fair_queue_enabled": True}

    # The stored row, the queue's own gate probe and the audit log all agree.
    with psycopg.connect(_t34_dsn()) as conn:
        stored = conn.execute(
            "SELECT fair_queue_enabled FROM runtime_settings WHERE id = 1"
        ).fetchone()
        assert stored is not None and bool(stored[0]) is True
        audit_rows = conn.execute(
            "SELECT actor_user_id, action, metadata_json FROM audit_logs "
            "WHERE action = 'runtime_settings.update' AND entity_id = '1' "
            "ORDER BY created_at DESC LIMIT 1"
        ).fetchall()
        assert audit_rows and audit_rows[0][0] == "admin_u"
        assert '"fair_queue_enabled": true' in str(audit_rows[0][2])

    # admin_app has DATABASE_URL_ENV pointed at this fixture database, so the
    # pool-backed probe reads the same switch the queue will.
    with pg_transaction() as raw:
        assert _fair_queue_enabled(BusinessConnection.postgres(raw)) is True

    # Back off through the same audited path (the rollout rollback path).
    off = client.patch(QUEUE_MODE_PATH, headers=headers, json={"fair_queue_enabled": False})
    assert off.status_code == 200
    assert off.json() == {"fair_queue_enabled": False}


@pytest.mark.pg
def test_auditor_cannot_flip_queue_mode(client: TestClient):
    """Auditors are strictly read-only on the control plane."""
    headers = _admin_session(client, "auditor_u")
    assert client.get(QUEUE_MODE_PATH, headers=headers).status_code == 200
    denied = client.patch(QUEUE_MODE_PATH, headers=headers, json={"fair_queue_enabled": True})
    assert denied.status_code == 403
    assert denied.json()["detail"]["code"] == "AUDITOR_READ_ONLY"
