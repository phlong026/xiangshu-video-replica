"""T09 / DB-08 — per-operator admin session, CSRF, RBAC and fail-closed startup.

Unit cases (no PG): exchange-credential issue/verify matrix and the
customer-production security gate. PG cases (skip without the fixture): the
admin session exchange endpoint, cookie/CSRF enforcement, RBAC (admin writes /
auditor read-only), revocation and the legacy single-admin control path.

The session data layer lives in revision 026 (``admin_sessions``); this task
implements the application layer on top of it.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.admin_auth_routes import (
    ADMIN_CSRF_HEADER,
    ADMIN_SESSION_COOKIE,
    ADMIN_SESSION_HMAC_KEY_ENV,
    AdminWriter,
    ExchangeCredentialError,
    admin_hmac_key,
    issue_exchange_credential,
    parse_and_verify_exchange_credential,
    resolve_admin_session_ttl_seconds,
)
from app.bootstrap import assert_customer_production_security
from app.db_pg import DATABASE_URL_ENV

DEFAULT_DSN = "postgresql://testuser:testpass@localhost:5433/customer_v3_test"
PG_DSN = os.environ.get("TEST_POSTGRESQL_URL", DEFAULT_DSN)
TEST_KEY = secrets.token_urlsafe(48)  # ≥ 32 bytes, never a real secret


def _pg_available(dsn: str) -> bool:
    try:

        def probe() -> None:
            conn = psycopg.connect(dsn, connect_timeout=3)
            conn.close()

        asyncio.run(asyncio.wait_for(asyncio.to_thread(probe), timeout=5))
    except Exception:
        return False
    return True


@contextmanager
def _env(**overrides: str) -> Iterator[None]:
    """Temporarily set/unset environment variables (``""`` unsets)."""
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


def _clean_production_env() -> dict[str, str]:
    """Customer-production-shaped env with every dev/legacy knob removed."""
    return {
        "VIDEO_REPLICA_CUSTOMER_PRODUCTION": "true",
        DATABASE_URL_ENV: "postgresql://u:p@db.example.com:5432/production",
        ADMIN_SESSION_HMAC_KEY_ENV: TEST_KEY,
        "VIDEO_REPLICA_AUTH_MODE": "internal",
        "VIDEO_REPLICA_ALLOW_DEV_IDENTITY_HEADER": "0",
        "VIDEO_REPLICA_DESKTOP_USER_ID": "",
        "VIDEO_REPLICA_STORAGE_ROOT": "",
        "VIDEO_REPLICA_DB_PATH": "",
        "CONTROL_PROXY_TOKEN_DIGEST": "",
        "CONTROL_ADMIN_USER_ID": "",
    }


# ---------------------------------------------------------------------------
# Exchange credential primitives (pure unit, no PG)
# ---------------------------------------------------------------------------


def test_exchange_credential_roundtrip() -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    credential = issue_exchange_credential(
        "admin_u", ttl_seconds=900, key=TEST_KEY.encode(), now=now
    )
    payload = parse_and_verify_exchange_credential(
        credential, now=now + timedelta(seconds=1), key=TEST_KEY.encode()
    )
    assert payload.actor_user_id == "admin_u"
    assert payload.expires_at == now + timedelta(seconds=900)
    assert len(payload.nonce) == 32
    assert payload.key_version == 1


def test_exchange_credential_expired_rejected() -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    credential = issue_exchange_credential(
        "admin_u", ttl_seconds=60, key=TEST_KEY.encode(), now=now
    )
    with pytest.raises(ExchangeCredentialError, match="expired"):
        parse_and_verify_exchange_credential(
            credential, now=now + timedelta(seconds=61), key=TEST_KEY.encode()
        )


def test_exchange_credential_tampering_rejected() -> None:
    import base64
    import json

    now = datetime.now(UTC).replace(microsecond=0)
    credential = issue_exchange_credential(
        "admin_u", ttl_seconds=60, key=TEST_KEY.encode(), now=now
    )
    prefix, body, signature = credential.split(".")

    # Tampered payload (privilege escalation attempt).
    decoded = json.loads(base64.urlsafe_b64decode(body + "=="))
    decoded["actor"] = "another_admin"
    forged_body = base64.urlsafe_b64encode(json.dumps(decoded).encode()).decode().rstrip("=")
    with pytest.raises(ExchangeCredentialError, match="signature"):
        parse_and_verify_exchange_credential(
            f"{prefix}.{forged_body}.{signature}", now=now, key=TEST_KEY.encode()
        )

    # Wrong key.
    with pytest.raises(ExchangeCredentialError, match="signature"):
        parse_and_verify_exchange_credential(credential, now=now, key=secrets.token_bytes(48))


def test_exchange_credential_malformed_rejected() -> None:
    now = datetime.now(UTC)
    key = TEST_KEY.encode()
    for malformed in ("", "garbage", "ASX1.only-two", "ASX2.a.b", "ASX1.a.b.c", "x" * 1024):
        with pytest.raises(ExchangeCredentialError):
            parse_and_verify_exchange_credential(malformed, now=now, key=key)


def test_admin_hmac_key_versioned_env_resolution() -> None:
    with _env(
        **{
            ADMIN_SESSION_HMAC_KEY_ENV: "",
            f"{ADMIN_SESSION_HMAC_KEY_ENV}_V1": TEST_KEY,
            f"{ADMIN_SESSION_HMAC_KEY_ENV}_V2": TEST_KEY,
        }
    ):
        assert admin_hmac_key(1) == TEST_KEY.encode()
        assert admin_hmac_key(2) == TEST_KEY.encode()
        with pytest.raises(ExchangeCredentialError, match="key version 3"):
            admin_hmac_key(3)


def test_admin_hmac_key_requires_minimum_strength() -> None:
    with _env(**{ADMIN_SESSION_HMAC_KEY_ENV: "short"}):
        with pytest.raises(ValueError, match="at least 32 bytes"):
            admin_hmac_key(1)


def test_admin_session_ttl_bounds_enforced() -> None:
    with _env(**{"VIDEO_REPLICA_ADMIN_SESSION_TTL_SECONDS": ""}):
        assert resolve_admin_session_ttl_seconds() == 8 * 3600
    with _env(**{"VIDEO_REPLICA_ADMIN_SESSION_TTL_SECONDS": "3600"}):
        assert resolve_admin_session_ttl_seconds() == 3600
    for invalid in ("0", "-5", "not-a-number", str(24 * 3600 + 1)):
        with _env(**{"VIDEO_REPLICA_ADMIN_SESSION_TTL_SECONDS": invalid}):
            with pytest.raises(ValueError):
                resolve_admin_session_ttl_seconds()


# ---------------------------------------------------------------------------
# Customer-production security gate (pure unit, no PG)
# ---------------------------------------------------------------------------


def test_security_gate_passes_with_clean_production_env() -> None:
    with _env(**_clean_production_env()):
        assert_customer_production_security()  # must not raise


def test_security_gate_skipped_outside_customer_production() -> None:
    """Internal mode keeps working with dev identity and local assets."""
    with _env(
        **{
            "VIDEO_REPLICA_CUSTOMER_PRODUCTION": "",
            "VIDEO_REPLICA_AUTH_MODE": "development",
            "VIDEO_REPLICA_ALLOW_DEV_IDENTITY_HEADER": "1",
            "VIDEO_REPLICA_DESKTOP_USER_ID": "dev_admin",
            "VIDEO_REPLICA_STORAGE_ROOT": "/tmp/storage",
            "CONTROL_PROXY_TOKEN_DIGEST": "a" * 64,
            "CONTROL_ADMIN_USER_ID": "admin_1",
        }
    ):
        assert_customer_production_security()  # must not raise


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"CONTROL_PROXY_TOKEN_DIGEST": "b" * 64}, "CONTROL_PROXY_TOKEN_DIGEST"),
        ({"CONTROL_ADMIN_USER_ID": "admin_1"}, "CONTROL_ADMIN_USER_ID"),
        ({"VIDEO_REPLICA_AUTH_MODE": "desktop"}, "VIDEO_REPLICA_AUTH_MODE"),
        ({"VIDEO_REPLICA_AUTH_MODE": "development"}, "VIDEO_REPLICA_AUTH_MODE"),
        ({"VIDEO_REPLICA_ALLOW_DEV_IDENTITY_HEADER": "1"}, "DEV_IDENTITY_HEADER"),
        ({"VIDEO_REPLICA_DESKTOP_USER_ID": "admin_1"}, "DESKTOP_USER_ID"),
        ({"VIDEO_REPLICA_STORAGE_ROOT": "/var/lib/assets"}, "STORAGE_ROOT"),
        ({ADMIN_SESSION_HMAC_KEY_ENV: ""}, "ADMIN_SESSION_HMAC_KEY"),
    ],
)
def test_security_gate_rejects_each_violation(overrides: dict[str, str], message: str) -> None:
    env = _clean_production_env()
    env.update(overrides)
    with _env(**env):
        with pytest.raises(RuntimeError, match=message):
            assert_customer_production_security()


def test_security_gate_reports_all_violations_at_once() -> None:
    env = _clean_production_env()
    env.update(
        {
            "CONTROL_ADMIN_USER_ID": "admin_1",
            "VIDEO_REPLICA_STORAGE_ROOT": "/var/lib/assets",
            ADMIN_SESSION_HMAC_KEY_ENV: "",
        }
    )
    with _env(**env):
        with pytest.raises(RuntimeError) as excinfo:
            assert_customer_production_security()
    message = str(excinfo.value)
    assert "CONTROL_ADMIN_USER_ID" in message
    assert "STORAGE_ROOT" in message
    assert "ADMIN_SESSION_HMAC_KEY" in message


def test_legacy_control_identity_rejected_at_runtime_in_customer_production() -> None:
    """Even if a misconfigured process slips past startup, the legacy
    proxy-token control path must refuse to authenticate operators."""
    from fastapi import HTTPException

    from app.control_auth import get_control_user

    with _env(
        **{
            "VIDEO_REPLICA_CUSTOMER_PRODUCTION": "true",
            "CONTROL_PROXY_TOKEN_DIGEST": hashlib.sha256(b"proxy-token").hexdigest(),
            "CONTROL_ADMIN_USER_ID": "admin_1",
        }
    ):
        with pytest.raises(HTTPException) as excinfo:
            get_control_user(None, "proxy-token")  # type: ignore[arg-type]
    assert excinfo.value.status_code == 403
    assert excinfo.value.detail["code"] == "LEGACY_CONTROL_IDENTITY_FORBIDDEN"


# ---------------------------------------------------------------------------
# PG integration — admin session exchange / cookie / CSRF / RBAC
# ---------------------------------------------------------------------------

pytestmark_pg = pytest.mark.skipif(
    not _pg_available(PG_DSN),
    reason="PostgreSQL fixture not reachable; scripts/pg-fixture.sh start",
)

T09_DB_NAME = "t09_admin_session_test"


def _admin_dsn() -> str:
    return PG_DSN.rsplit("/", 1)[0] + "/postgres"


def _t09_dsn() -> str:
    return PG_DSN.rsplit("/", 1)[0] + f"/{T09_DB_NAME}"


def _alembic_upgrade(dsn: str) -> None:
    from alembic import command
    from alembic.config import Config

    server_dir = Path(__file__).resolve().parent.parent
    config = Config(str(server_dir / "alembic.ini"))
    config.set_main_option("script_location", str(server_dir / "migrations"))
    config.set_main_option("sqlalchemy.url", dsn.replace("postgresql://", "postgresql+psycopg://"))
    command.upgrade(config, "head")


@pytest.fixture(scope="module")
def admin_pg_dsn() -> Iterator[str]:
    """Dedicated migrated database with operator seed users."""
    if not _pg_available(PG_DSN):
        pytest.skip("PostgreSQL fixture not reachable")
    with psycopg.connect(_admin_dsn(), autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{T09_DB_NAME}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{T09_DB_NAME}"')
    _alembic_upgrade(_t09_dsn())
    with psycopg.connect(_t09_dsn(), autocommit=True) as conn:
        # Freshly created database: plain DELETE avoids TRUNCATE's FK checks
        # against referencing tables (projects et al.) which are empty here.
        conn.execute("DELETE FROM users")
        for user_id, role, active in (
            ("admin_u", "admin", 1),
            ("auditor_u", "auditor", 1),
            ("employee_u", "employee", 1),
            ("inactive_admin", "admin", 0),
        ):
            conn.execute(
                "INSERT INTO users (id, username, display_name, role, is_active) "
                "VALUES (%s, %s, %s, %s, %s)",
                (user_id, user_id, user_id.replace("_", " ").title(), role, active),
            )
    try:
        yield _t09_dsn()
    finally:
        with psycopg.connect(_admin_dsn(), autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{T09_DB_NAME}" WITH (FORCE)')


@pytest.fixture()
def clean_sessions(admin_pg_dsn: str) -> Iterator[str]:
    """Per-test isolation: truncate admin_sessions and reset the module pool."""
    from app.db_pg import close_pg_pool

    close_pg_pool()
    with psycopg.connect(admin_pg_dsn, autocommit=True) as conn:
        conn.execute("TRUNCATE admin_sessions")
    yield admin_pg_dsn
    close_pg_pool()


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch, clean_sessions: str) -> Iterator[TestClient]:
    from app.admin_auth_routes import router as admin_router

    app = FastAPI()
    app.include_router(admin_router)

    # Mounted under the admin cookie path so the session cookie is actually
    # sent; ``AdminWriter`` must stay module-level for FastAPI's annotation
    # resolution (get_type_hints uses module globals, not closure locals).
    @app.post("/api/control/_test/admin-write")
    def admin_write(actor: AdminWriter) -> dict[str, str]:
        return {"actor": actor.user_id}

    monkeypatch.setenv(DATABASE_URL_ENV, clean_sessions)
    monkeypatch.delenv("VIDEO_REPLICA_CUSTOMER_PRODUCTION", raising=False)
    monkeypatch.setenv(ADMIN_SESSION_HMAC_KEY_ENV, TEST_KEY)
    # Isolate from conftest's dev-identity defaults.
    monkeypatch.delenv("VIDEO_REPLICA_AUTH_MODE", raising=False)
    monkeypatch.delenv("VIDEO_REPLICA_ALLOW_DEV_IDENTITY_HEADER", raising=False)
    with TestClient(app) as test_client:
        yield test_client


def _issue(actor_user_id: str = "admin_u", ttl: int = 3600) -> str:
    return issue_exchange_credential(actor_user_id, ttl_seconds=ttl)


@pytest.fixture()
def admin_session(client: TestClient) -> dict[str, str]:
    response = client.post(
        "/api/control/admin/session/exchange", json={"credential": _issue("admin_u")}
    )
    assert response.status_code == 201, response.text
    body = response.json()
    return {
        "csrf_token": body["csrf_token"],
        "session_id": body["session_id"],
        "cookie_value": response.cookies.get(ADMIN_SESSION_COOKIE, ""),
    }


def test_exchange_issues_session_with_secure_cookie_shape(
    client: TestClient, clean_sessions: str
) -> None:
    for actor in ("admin_u", "auditor_u"):
        response = client.post(
            "/api/control/admin/session/exchange",
            json={"credential": _issue(actor)},
            headers={"User-Agent": "t09-test-agent"},
        )
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["actor"]["user_id"] == actor
        assert body["csrf_token"]
        assert body["expires_at"]

        set_cookie = response.headers["set-cookie"]
        assert f"{ADMIN_SESSION_COOKIE}=" in set_cookie
        assert "httponly" in set_cookie.lower()
        assert "samesite=strict" in set_cookie.lower()
        assert "path=/api/control" in set_cookie.lower()
        # Internal (non-customer-production) deployments serve behind the loopback
        # desktop boundary; the Secure attribute is asserted on the production env.
        assert "secure" not in set_cookie.lower()

        # The database only ever holds digests, never raw secrets.
        with psycopg.connect(clean_sessions) as conn:
            row = conn.execute(
                "SELECT session_digest, csrf_digest, created_ip_digest, created_ua_digest, "
                "actor_user_id FROM admin_sessions WHERE id = %s",
                (body["session_id"],),
            ).fetchone()
        assert row is not None
        cookie_value = response.cookies.get(ADMIN_SESSION_COOKIE, "")
        assert row[0] == hashlib.sha256(cookie_value.encode()).hexdigest()
        assert row[1] == hashlib.sha256(body["csrf_token"].encode()).hexdigest()
        assert row[2] == hashlib.sha256(b"testclient").hexdigest()
        assert row[3] == hashlib.sha256(b"t09-test-agent").hexdigest()
        assert row[4] == actor
        with psycopg.connect(clean_sessions, autocommit=True) as conn:
            conn.execute("TRUNCATE admin_sessions")
        client.cookies.clear()


def test_exchange_credential_single_use(client: TestClient) -> None:
    credential = _issue("admin_u")
    first = client.post("/api/control/admin/session/exchange", json={"credential": credential})
    assert first.status_code == 201
    replay = client.post("/api/control/admin/session/exchange", json={"credential": credential})
    assert replay.status_code == 401
    assert replay.json()["detail"]["code"] == "EXCHANGE_CREDENTIAL_REUSED"


def test_exchange_rejects_expired_credential(client: TestClient) -> None:
    credential = issue_exchange_credential(
        "admin_u", ttl_seconds=60, now=datetime.now(UTC) - timedelta(seconds=120)
    )
    response = client.post("/api/control/admin/session/exchange", json={"credential": credential})
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "EXCHANGE_CREDENTIAL_INVALID"


def test_exchange_rejects_employee_role(client: TestClient) -> None:
    response = client.post(
        "/api/control/admin/session/exchange", json={"credential": _issue("employee_u")}
    )
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "ADMIN_ROLE_REQUIRED"


def test_exchange_rejects_inactive_user(client: TestClient) -> None:
    response = client.post(
        "/api/control/admin/session/exchange", json={"credential": _issue("inactive_admin")}
    )
    assert response.status_code == 401


def test_exchange_rejects_unknown_actor(client: TestClient) -> None:
    response = client.post(
        "/api/control/admin/session/exchange", json={"credential": _issue("ghost_u")}
    )
    assert response.status_code == 401


def test_whoami_returns_real_actor(client: TestClient, admin_session: dict[str, str]) -> None:
    response = client.get("/api/control/admin/session")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["actor"]["user_id"] == "admin_u"
    assert body["actor"]["role"] == "admin"
    assert body["session_id"] == admin_session["session_id"]


def test_whoami_requires_valid_session_cookie(client: TestClient) -> None:
    no_cookie = client.get("/api/control/admin/session")
    assert no_cookie.status_code == 401

    client.cookies.set(ADMIN_SESSION_COOKIE, "forged-token-value")
    forged = client.get("/api/control/admin/session")
    assert forged.status_code == 401


def test_logout_revokes_session(client: TestClient, admin_session: dict[str, str]) -> None:
    response = client.delete(
        "/api/control/admin/session", headers={ADMIN_CSRF_HEADER: admin_session["csrf_token"]}
    )
    assert response.status_code == 204
    after = client.get("/api/control/admin/session")
    assert after.status_code == 401


def test_logout_requires_csrf_header(client: TestClient, admin_session: dict[str, str]) -> None:
    missing = client.delete("/api/control/admin/session")
    assert missing.status_code == 403
    assert missing.json()["detail"]["code"] == "ADMIN_CSRF_REQUIRED"

    wrong = client.delete("/api/control/admin/session", headers={ADMIN_CSRF_HEADER: "wrong"})
    assert wrong.status_code == 403
    assert wrong.json()["detail"]["code"] == "ADMIN_CSRF_INVALID"


def test_expired_session_rejected(
    client: TestClient, admin_session: dict[str, str], clean_sessions: str
) -> None:
    # Rewind the whole row into the past: revision 026 guards
    # expires_at > created_at, so created_at must move back with it.
    past = datetime.now(UTC) - timedelta(hours=2)
    expired = datetime.now(UTC) - timedelta(seconds=1)
    with psycopg.connect(clean_sessions, autocommit=True) as conn:
        conn.execute(
            "UPDATE admin_sessions SET created_at = %s, last_activity_at = %s, "
            "expires_at = %s WHERE id = %s",
            (past.isoformat(), past.isoformat(), expired.isoformat(), admin_session["session_id"]),
        )
    response = client.get("/api/control/admin/session")
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "ADMIN_SESSION_EXPIRED"


def test_revoked_session_rejected(
    client: TestClient, admin_session: dict[str, str], clean_sessions: str
) -> None:
    with psycopg.connect(clean_sessions, autocommit=True) as conn:
        conn.execute(
            "UPDATE admin_sessions SET revoked_at = %s WHERE id = %s",
            (datetime.now(UTC).isoformat(), admin_session["session_id"]),
        )
    response = client.get("/api/control/admin/session")
    assert response.status_code == 401


def test_disabled_actor_invalidates_session(
    client: TestClient, admin_session: dict[str, str], clean_sessions: str
) -> None:
    with psycopg.connect(clean_sessions, autocommit=True) as conn:
        conn.execute("UPDATE users SET is_active = 0 WHERE id = 'admin_u'")
    response = client.get("/api/control/admin/session")
    assert response.status_code == 401
    with psycopg.connect(clean_sessions, autocommit=True) as conn:
        conn.execute("UPDATE users SET is_active = 1 WHERE id = 'admin_u'")


def test_auditor_is_read_only(client: TestClient) -> None:
    response = client.post(
        "/api/control/admin/session/exchange", json={"credential": _issue("auditor_u")}
    )
    assert response.status_code == 201, response.text
    csrf_token = response.json()["csrf_token"]

    blocked = client.post("/api/control/_test/admin-write", headers={ADMIN_CSRF_HEADER: csrf_token})
    assert blocked.status_code == 403, blocked.text
    assert blocked.json()["detail"]["code"] == "AUDITOR_READ_ONLY"

    reading = client.get("/api/control/admin/session")
    assert reading.status_code == 200
    assert reading.json()["actor"]["role"] == "auditor"


def test_admin_writer_dependency_allows_admin(
    client: TestClient, admin_session: dict[str, str]
) -> None:
    response = client.post(
        "/api/control/_test/admin-write", headers={ADMIN_CSRF_HEADER: admin_session["csrf_token"]}
    )
    assert response.status_code == 200, response.text
    assert response.json()["actor"] == "admin_u"


def test_secure_cookie_in_customer_production(
    monkeypatch: pytest.MonkeyPatch, clean_sessions: str
) -> None:
    from app.admin_auth_routes import router as admin_router

    app = FastAPI()
    app.include_router(admin_router)
    monkeypatch.setenv(DATABASE_URL_ENV, clean_sessions)
    monkeypatch.setenv("VIDEO_REPLICA_CUSTOMER_PRODUCTION", "true")
    monkeypatch.setenv(ADMIN_SESSION_HMAC_KEY_ENV, TEST_KEY)
    monkeypatch.delenv("VIDEO_REPLICA_AUTH_MODE", raising=False)
    monkeypatch.delenv("VIDEO_REPLICA_ALLOW_DEV_IDENTITY_HEADER", raising=False)
    monkeypatch.delenv("VIDEO_REPLICA_STORAGE_ROOT", raising=False)

    with TestClient(app) as test_client:
        response = test_client.post(
            "/api/control/admin/session/exchange",
            json={"credential": _issue("admin_u")},
        )
        assert response.status_code == 201, response.text
        assert "secure" in response.headers["set-cookie"].lower()


def test_pg_unconfigured_returns_service_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a PG runtime the admin-session surface must fail closed with a
    503 (the internal P0 control plane keeps its proxy-token path)."""
    from app.admin_auth_routes import router as admin_router

    app = FastAPI()
    app.include_router(admin_router)
    monkeypatch.setenv(ADMIN_SESSION_HMAC_KEY_ENV, TEST_KEY)
    monkeypatch.setenv("VIDEO_REPLICA_DB_PATH", "/tmp/internal-only.db")
    monkeypatch.delenv(DATABASE_URL_ENV, raising=False)

    with TestClient(app) as test_client:
        response = test_client.post(
            "/api/control/admin/session/exchange",
            json={"credential": _issue("admin_u")},
        )
        assert response.status_code == 503
        assert response.json()["detail"]["code"] == "ADMIN_SESSIONS_UNAVAILABLE"
