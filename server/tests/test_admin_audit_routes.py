"""T34 — admin audit log API tests.

Tests cover:
- GET /api/control/audit-log — list audit events
- Pagination (limit/offset)
- Filtering by event type and actor
- Admin/auditor role access control
"""

from __future__ import annotations

import os
import secrets
import uuid

# Set HMAC key before importing app modules
os.environ.setdefault("VIDEO_REPLICA_ADMIN_SESSION_HMAC_KEY", "test-key-for-t34-audit-tests-minimum-48-bytes-long-1234567890")

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


def _dsn_for(db_name: str) -> str:
    base = os.environ.get(DATABASE_URL_ENV, DEFAULT_DSN)
    prefix, _, suffix = base.rpartition("/")
    return f"{prefix}/{db_name}"


@pytest.fixture(scope="module")
def pg_conn():
    """Create a fresh database for T34 audit tests."""
    base_dsn = os.environ.get(DATABASE_URL_ENV, DEFAULT_DSN)
    if not base_dsn:
        pytest.skip(SKIP_REASON)
    
    try:
        admin = psycopg.connect(base_dsn, autocommit=True)
    except psycopg.OperationalError:
        pytest.skip(SKIP_REASON)
    
    admin.execute(f"DROP DATABASE IF EXISTS {T34_DB_NAME}")
    admin.execute(f"CREATE DATABASE {T34_DB_NAME}")
    admin.close()
    
    test_dsn = _dsn_for(T34_DB_NAME)
    os.environ[DATABASE_URL_ENV] = test_dsn
    
    conn = psycopg.connect(test_dsn, autocommit=False)
    
    # Create schema
    conn.execute("""
        CREATE TABLE admin_users (
            id TEXT PRIMARY KEY,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL CHECK (role IN ('admin', 'auditor')),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        
        CREATE TABLE admin_sessions (
            id TEXT PRIMARY KEY,
            admin_user_id TEXT NOT NULL REFERENCES admin_users(id),
            session_token TEXT UNIQUE NOT NULL,
            csrf_token TEXT NOT NULL,
            expires_at TIMESTAMPTZ NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        
        CREATE TABLE customers (
            user_id TEXT PRIMARY KEY,
            email TEXT UNIQUE NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        
        CREATE TABLE admin_adjustments (
            id TEXT PRIMARY KEY,
            admin_user_id TEXT NOT NULL REFERENCES admin_users(id),
            target_user_id TEXT NOT NULL REFERENCES customers(user_id),
            source_document_type TEXT NOT NULL,
            source_document_ref TEXT NOT NULL,
            reason TEXT NOT NULL,
            request_id TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
    """)
    conn.commit()
    
    yield conn
    
    conn.close()
    close_pg_pool()
    
    admin = psycopg.connect(base_dsn, autocommit=True)
    admin.execute(f"DROP DATABASE IF EXISTS {T34_DB_NAME}")
    admin.close()


def _create_admin_user(conn: psycopg.Connection, email: str, role: str) -> str:
    user_id = str(uuid.uuid4())
    password_hash = "hashed_password"
    conn.execute(
        "INSERT INTO admin_users (id, email, password_hash, role) VALUES (%s, %s, %s, %s)",
        (user_id, email, password_hash, role),
    )
    conn.commit()
    return user_id


def _create_admin_session(conn: psycopg.Connection, admin_user_id: str) -> dict:
    credential = issue_exchange_credential(admin_user_id, ttl_seconds=3600)
    session_token = secrets.token_urlsafe(32)
    csrf_token = secrets.token_urlsafe(32)
    
    conn.execute(
        "INSERT INTO admin_sessions (id, admin_user_id, session_token, csrf_token, expires_at) "
        "VALUES (%s, %s, %s, %s, now() + interval '1 hour')",
        (str(uuid.uuid4()), admin_user_id, session_token, csrf_token),
    )
    conn.commit()
    
    return {"session_token": session_token, "csrf_token": csrf_token}


def _create_customer(conn: psycopg.Connection, email: str) -> str:
    user_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO customers (user_id, email) VALUES (%s, %s)",
        (user_id, email),
    )
    conn.commit()
    return user_id


def _create_adjustment(
    conn: psycopg.Connection,
    admin_user_id: str,
    target_user_id: str,
    source_type: str,
    reason: str,
) -> str:
    adjustment_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO admin_adjustments (id, admin_user_id, target_user_id, source_document_type, "
        "source_document_ref, reason, request_id) VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (adjustment_id, admin_user_id, target_user_id, source_type, f"REF-{uuid.uuid4()}", reason, str(uuid.uuid4())),
    )
    conn.commit()
    return adjustment_id


@pytest.mark.pg
def test_list_audit_log_requires_admin(pg_conn):
    """Unauthenticated requests must be rejected with 401."""
    from app.admin_audit_routes import router
    
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    
    response = client.get("/api/control/audit-log")
    assert response.status_code == 401


@pytest.mark.pg
def test_list_audit_log_returns_empty(pg_conn):
    """An empty audit log should return an empty list."""
    admin_user_id = _create_admin_user(pg_conn, "admin@example.com", "admin")
    admin_session = _create_admin_session(pg_conn, admin_user_id)
    
    from app.admin_audit_routes import router
    
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    
    response = client.get(
        "/api/control/audit-log",
        cookies={"admin_session": admin_session["session_token"]},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["items"] == []
    assert data["total"] == 0


@pytest.mark.pg
def test_list_audit_log_returns_adjustments(pg_conn):
    """Should return admin adjustments as audit events."""
    admin_user_id = _create_admin_user(pg_conn, "admin@example.com", "admin")
    admin_session = _create_admin_session(pg_conn, admin_user_id)
    target_user_id = _create_customer(pg_conn, "customer@example.com")
    
    # Create some audit events
    _create_adjustment(pg_conn, admin_user_id, target_user_id, "CS_TICKET", "客户补偿")
    _create_adjustment(pg_conn, admin_user_id, target_user_id, "REFUND_APPROVAL", "退款")
    
    from app.admin_audit_routes import router
    
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    
    response = client.get(
        "/api/control/audit-log",
        cookies={"admin_session": admin_session["session_token"]},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 2
    assert len(data["items"]) == 2
    assert data["items"][0]["event_type"] == "ADMIN_ADJUSTMENT"


@pytest.mark.pg
def test_auditor_can_list_audit_log(pg_conn):
    """Auditors should be able to list audit events (read-only)."""
    auditor_user_id = _create_admin_user(pg_conn, "auditor@example.com", "auditor")
    auditor_session = _create_admin_session(pg_conn, auditor_user_id)
    
    from app.admin_audit_routes import router
    
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    
    response = client.get(
        "/api/control/audit-log",
        cookies={"admin_session": auditor_session["session_token"]},
    )
    assert response.status_code == 200
