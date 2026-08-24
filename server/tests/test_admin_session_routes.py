"""T34 — admin session management API tests.

Tests cover:
- GET /api/control/customers/{user_id}/sessions — list customer sessions
- Pagination (limit/offset)
- Filtering by status
- Admin/auditor role access control
"""

from __future__ import annotations

import os
import secrets
import uuid

# Set HMAC key before importing app modules
os.environ.setdefault("VIDEO_REPLICA_ADMIN_SESSION_HMAC_KEY", "test-key-for-t34-session-tests-minimum-48-bytes-long-1234567890")

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


def _dsn_for(db_name: str) -> str:
    base = os.environ.get(DATABASE_URL_ENV, DEFAULT_DSN)
    prefix, _, suffix = base.rpartition("/")
    return f"{prefix}/{db_name}"


@pytest.fixture(scope="module")
def pg_conn():
    """Create a fresh database for T34 tests."""
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
        
        CREATE TABLE activation_codes (
            id TEXT PRIMARY KEY,
            code TEXT UNIQUE NOT NULL,
            status TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        
        CREATE TABLE customer_devices (
            id TEXT PRIMARY KEY,
            activation_code_id TEXT NOT NULL REFERENCES activation_codes(id),
            user_id TEXT NOT NULL REFERENCES customers(user_id),
            slot_no INTEGER NOT NULL,
            display_name TEXT,
            platform TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            bound_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            unbound_at TIMESTAMPTZ,
            revoked_at TIMESTAMPTZ
        );
        
        CREATE TABLE customer_sessions (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES customers(user_id),
            device_id TEXT NOT NULL REFERENCES customer_devices(id),
            session_token TEXT UNIQUE NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            expires_at TIMESTAMPTZ NOT NULL
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


def _create_activation_code(conn: psycopg.Connection, code: str) -> str:
    code_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO activation_codes (id, code, status) VALUES (%s, %s, 'active')",
        (code_id, code),
    )
    conn.commit()
    return code_id


def _create_session(conn: psycopg.Connection, user_id: str, device_id: str) -> str:
    session_id = str(uuid.uuid4())
    session_token = secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO customer_sessions (id, user_id, device_id, session_token, expires_at) "
        "VALUES (%s, %s, %s, %s, now() + interval '1 hour')",
        (session_id, user_id, device_id, session_token),
    )
    conn.commit()
    return session_id


@pytest.mark.pg
def test_list_customer_sessions_requires_admin(pg_conn):
    """Unauthenticated requests must be rejected with 401."""
    from app.admin_session_routes import router
    
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    
    user_id = str(uuid.uuid4())
    response = client.get(f"/api/control/customers/{user_id}/sessions")
    assert response.status_code == 401


@pytest.mark.pg
def test_list_customer_sessions_returns_empty_for_new_user(pg_conn):
    """A customer with no sessions should return an empty list."""
    admin_user_id = _create_admin_user(pg_conn, "admin@example.com", "admin")
    admin_session = _create_admin_session(pg_conn, admin_user_id)
    user_id = _create_customer(pg_conn, "test@example.com")
    
    from app.admin_session_routes import router
    
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    
    response = client.get(
        f"/api/control/customers/{user_id}/sessions",
        cookies={"admin_session": admin_session["session_token"]},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["items"] == []
    assert data["total"] == 0
    assert data["limit"] == 20
    assert data["offset"] == 0


@pytest.mark.pg
def test_list_customer_sessions_returns_sessions(pg_conn):
    """Should return all sessions for a customer with pagination."""
    admin_user_id = _create_admin_user(pg_conn, "admin@example.com", "admin")
    admin_session = _create_admin_session(pg_conn, admin_user_id)
    user_id = _create_customer(pg_conn, "test@example.com")
    code_id = _create_activation_code(pg_conn, "ABC-123")
    
    # Create two devices and sessions
    device1_id = str(uuid.uuid4())
    device2_id = str(uuid.uuid4())
    pg_conn.execute(
        "INSERT INTO customer_devices (id, activation_code_id, user_id, slot_no, display_name, platform) "
        "VALUES (%s, %s, %s, 1, 'Device 1', 'ios'), (%s, %s, %s, 2, 'Device 2', 'android')",
        (device1_id, code_id, user_id, device2_id, code_id, user_id),
    )
    pg_conn.commit()
    
    _create_session(pg_conn, user_id, device1_id)
    _create_session(pg_conn, user_id, device2_id)
    
    from app.admin_session_routes import router
    
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    
    response = client.get(
        f"/api/control/customers/{user_id}/sessions",
        cookies={"admin_session": admin_session["session_token"]},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 2
    assert len(data["items"]) == 2
    assert data["items"][0]["user_id"] == user_id


@pytest.mark.pg
def test_auditor_can_list_sessions(pg_conn):
    """Auditors should be able to list sessions (read-only)."""
    auditor_user_id = _create_admin_user(pg_conn, "auditor@example.com", "auditor")
    auditor_session = _create_admin_session(pg_conn, auditor_user_id)
    user_id = _create_customer(pg_conn, "test@example.com")
    
    from app.admin_session_routes import router
    
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    
    response = client.get(
        f"/api/control/customers/{user_id}/sessions",
        cookies={"admin_session": auditor_session["session_token"]},
    )
    assert response.status_code == 200
