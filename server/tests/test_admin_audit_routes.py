"""T34 — admin audit log API tests (adapted to the 039/users model).

Tests cover:
- GET /api/control/audit-log — list audit events
- Pagination (limit/offset) and actor_user_id filtering
- Admin/auditor role access control

The fixture database is migrated with alembic upgrade head (the T23
precedent) so every query runs against the real revision-039 schema where
admin_adjustments.admin_user_id references users.id — there is no separate
``admin_users`` table.
"""

from __future__ import annotations

import os
import secrets
import uuid
from collections.abc import Iterator
from pathlib import Path

# Set HMAC key before importing app modules
os.environ.setdefault(
    "VIDEO_REPLICA_ADMIN_SESSION_HMAC_KEY",
    "test-key-for-t34-audit-tests-minimum-48-bytes-long-1234567890",
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

T34_DB_NAME = "t34_admin_audit_test"

TEST_ADMIN_SESSION_KEY = secrets.token_urlsafe(48)

AUDIT_PATH = "/api/control/audit-log"


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
def audit_dsn() -> Iterator[str]:
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


@pytest.fixture()
def route_state(audit_dsn: str) -> Iterator[str]:
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
    yield audit_dsn
    close_pg_pool()


@pytest.fixture()
def admin_app(monkeypatch: pytest.MonkeyPatch, route_state: str) -> Iterator[FastAPI]:
    from app.admin_audit_routes import router as admin_audit_router
    from app.admin_auth_routes import router as admin_auth_router

    app = FastAPI()
    app.include_router(admin_auth_router)
    app.include_router(admin_audit_router)
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


def _insert_adjustment(
    conn: psycopg.Connection,
    *,
    actor: str,
    target: str,
    source_type: str,
    reason: str,
) -> str:
    """Create one PAID adjustment-style order plus its audit row.

    ``admin_adjustments.recharge_order_id`` is unique (one audit row per
    order) so every row gets its own order.
    """
    adjustment_id = str(uuid.uuid4())
    order_id = f"audit-order-{adjustment_id}"
    conn.execute(
        "INSERT INTO recharge_orders "
        "(id, user_id, merchant_order_no, provider, status, pricing_scope, "
        " base_unit_price_fen_snapshot, charged_unit_price_fen_snapshot, "
        " min_recharge_fen_snapshot, recharge_step_fen_snapshot, "
        " amount_fen, credits, paid_at) "
        "VALUES (%s, %s, %s, 'admin_adjustment', 'PAID', 'CUSTOMER_STANDARD', "
        "1000, 1000, 10000, 1000, 1000, 1, now())",
        (order_id, target, f"AUDIT-{adjustment_id}"),
    )
    conn.execute(
        "INSERT INTO admin_adjustments "
        "(id, recharge_order_id, target_user_id, admin_user_id, "
        " source_document_type, source_document_ref, reason, request_id) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
        (
            adjustment_id,
            order_id,
            target,
            actor,
            source_type,
            f"REF-{uuid.uuid4()}",
            reason,
            str(uuid.uuid4()),
        ),
    )
    conn.commit()
    return adjustment_id


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.pg
def test_list_audit_log_requires_admin(client: TestClient):
    """Unauthenticated requests must be rejected with 401."""
    response = client.get(AUDIT_PATH)
    assert response.status_code == 401


@pytest.mark.pg
def test_list_audit_log_returns_empty(client: TestClient, route_state: str):
    """An empty audit log should return an empty list."""
    _admin_session(client)
    response = client.get(AUDIT_PATH)
    assert response.status_code == 200
    data = response.json()
    assert data["items"] == []
    assert data["total"] == 0


@pytest.mark.pg
def test_list_audit_log_returns_adjustments(client: TestClient, route_state: str):
    """Should return admin adjustments as audit events with the real actor."""
    _admin_session(client, "admin_u")
    with psycopg.connect(_t34_dsn(), autocommit=True) as conn:
        _insert_adjustment(
            conn,
            actor="admin_u",
            target="customer_u",
            source_type="CS_TICKET",
            reason="客户补偿：拆解失败两次",
        )
        _insert_adjustment(
            conn,
            actor="admin_u",
            target="customer_u",
            source_type="REFUND_APPROVAL",
            reason="退款",
        )

    response = client.get(AUDIT_PATH)
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 2
    assert len(data["items"]) == 2
    # ORDER BY created_at DESC, id — created_at comes from the transaction
    # clock and the tie-break is the random row id, so assert as a set.
    assert {item["event_type"] for item in data["items"]} == {"ADMIN_ADJUSTMENT"}
    assert {item["actor_username"] for item in data["items"]} == {"admin_u"}
    assert {item["target_user_id"] for item in data["items"]} == {"customer_u"}
    assert {item["reason"] for item in data["items"]} == {
        "客户补偿：拆解失败两次",
        "退款",
    }


@pytest.mark.pg
def test_list_audit_log_filters_by_actor_and_paginates(client: TestClient, route_state: str):
    """actor_user_id filter and limit/offset pagination."""
    _admin_session(client, "admin_u")
    with psycopg.connect(_t34_dsn(), autocommit=True) as conn:
        _insert_adjustment(
            conn, actor="admin_u", target="customer_u", source_type="CS_TICKET", reason="r1"
        )
        _insert_adjustment(
            conn, actor="admin_u", target="customer_u", source_type="CS_TICKET", reason="r2"
        )
        _insert_adjustment(
            conn, actor="auditor_u", target="customer_u", source_type="CS_TICKET", reason="r3"
        )

    filtered = client.get(AUDIT_PATH, params={"actor_user_id": "admin_u"})
    assert filtered.status_code == 200
    filtered_data = filtered.json()
    assert filtered_data["total"] == 2

    page = client.get(AUDIT_PATH, params={"limit": 1, "offset": 1})
    assert page.status_code == 200
    page_data = page.json()
    assert page_data["total"] == 3
    assert len(page_data["items"]) == 1
    assert page_data["limit"] == 1
    assert page_data["offset"] == 1


@pytest.mark.pg
def test_auditor_can_list_audit_log(client: TestClient, route_state: str):
    """Auditors should be able to list audit events (read-only)."""
    _admin_session(client, "auditor_u")
    response = client.get(AUDIT_PATH)
    assert response.status_code == 200


@pytest.mark.pg
def test_list_audit_log_rejects_unknown_event_type(client: TestClient, route_state: str):
    """An unsupported event_type must answer 400, never silently the full log."""
    _admin_session(client)
    response = client.get(AUDIT_PATH, params={"event_type": "DEVICE_UNBIND"})
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "INVALID_EVENT_TYPE"

    ok = client.get(AUDIT_PATH, params={"event_type": "ADMIN_ADJUSTMENT"})
    assert ok.status_code == 200
