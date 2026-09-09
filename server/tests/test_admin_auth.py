"""T09 / DB-08 — per-operator admin session, CSRF, RBAC and fail-closed startup.

Unit cases (no PG): exchange-credential issue/verify matrix and the
customer-production security gate. PG cases (skip without the fixture): the
admin session exchange endpoint, cookie/CSRF enforcement, RBAC (admin writes /
auditor read-only), revocation and the legacy single-admin control path.

The session data layer lives in revision 026 (``admin_sessions``); this task
implements the application layer on top of it.
"""

from __future__ import annotations

import base64
import hashlib
import json
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
from pg_test_kit import require_pg_or_explicit_skip

from app.admin_auth_routes import (
    ADMIN_CSRF_HEADER,
    ADMIN_SESSION_COOKIE,
    ADMIN_SESSION_HMAC_KEY_ENV,
    AdminWriter,
    ExchangeCredentialError,
    admin_hmac_key,
    hash_admin_password,
    issue_exchange_credential,
    parse_and_verify_exchange_credential,
    resolve_admin_session_idle_timeout_seconds,
    resolve_admin_session_ttl_seconds,
    verify_admin_password,
)
from app.bootstrap import assert_customer_production_security
from app.control_auth import ControlUser
from app.db_pg import DATABASE_URL_ENV

DEFAULT_DSN = "postgresql://testuser:testpass@localhost:5433/customer_v3_test"
PG_DSN = os.environ.get("TEST_POSTGRESQL_URL", DEFAULT_DSN)
TEST_KEY = secrets.token_urlsafe(48)  # ≥ 32 bytes, never a real secret
TEST_AEAD_KEY = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")


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
        DATABASE_URL_ENV: ("postgresql://u:p@db.example.com:5432/production?sslmode=verify-full"),
        ADMIN_SESSION_HMAC_KEY_ENV: TEST_KEY,
        "VIDEO_REPLICA_AUTH_MODE": "internal",
        "VIDEO_REPLICA_ALLOW_DEV_IDENTITY_HEADER": "0",
        "VIDEO_REPLICA_DESKTOP_USER_ID": "",
        "VIDEO_REPLICA_STORAGE_ROOT": "",
        "VIDEO_REPLICA_DB_PATH": "",
        "CONTROL_PROXY_TOKEN_DIGEST": "",
        "CONTROL_ADMIN_USER_ID": "",
        "VIDEO_REPLICA_PUBLIC_ORIGIN": "https://app.example.test",
        "PUBLIC_BASE_URL": "https://app.example.test",
        "VIDEO_REPLICA_TRUSTED_PROXY_CIDRS": "127.0.0.1/32,::1/128",
        "VIDEO_REPLICA_ACTIVATION_CODE_HMAC_KEY": TEST_KEY,
        "VIDEO_REPLICA_ACTIVATION_EXPORT_AEAD_KEY": TEST_AEAD_KEY,
        "VIDEO_REPLICA_DEVICE_FINGERPRINT_HMAC_KEY": TEST_KEY,
        "VIDEO_REPLICA_CUSTOMER_IDEMPOTENCY_AEAD_KEY": TEST_AEAD_KEY,
        "VIDEO_REPLICA_SETTINGS_KEY": TEST_AEAD_KEY,
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


def test_exchange_credential_non_object_json_rejected() -> None:
    """PR review P2: valid non-object JSON bodies (null/number/string/array)
    must reject as malformed instead of raising TypeError from dict()."""
    import base64

    now = datetime.now(UTC)
    key = TEST_KEY.encode()
    for raw in (b"null", b"1", b'"x"', b"[1,2]"):
        body = base64.urlsafe_b64encode(raw).decode().rstrip("=")
        signature = base64.urlsafe_b64encode(b"\x00" * 32).decode().rstrip("=")
        with pytest.raises(ExchangeCredentialError):
            parse_and_verify_exchange_credential(f"ASX1.{body}.{signature}", now=now, key=key)


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


def test_admin_session_idle_timeout_bounds_enforced() -> None:
    key = "VIDEO_REPLICA_ADMIN_SESSION_IDLE_TIMEOUT_SECONDS"
    with _env(**{key: ""}):
        assert resolve_admin_session_idle_timeout_seconds() == 30 * 60
    with _env(**{key: "3600"}):
        assert resolve_admin_session_idle_timeout_seconds() == 3600
    for invalid in ("0", "-5", "not-a-number", str(24 * 3600 + 1)):
        with _env(**{key: invalid}):
            with pytest.raises(ValueError):
                resolve_admin_session_idle_timeout_seconds()


def test_admin_password_hash_is_memory_hard_salted_and_verifiable() -> None:
    password = "correct horse battery staple"

    first = hash_admin_password(password)
    second = hash_admin_password(password)

    assert first.startswith("scrypt$")
    assert first != second
    assert password not in first
    assert verify_admin_password(password, first) is True
    assert verify_admin_password("wrong password", first) is False
    assert verify_admin_password(password, "malformed") is False


@pytest.mark.parametrize(
    "password",
    ["short", " " * 20, "a" * 129],
)
def test_admin_password_policy_rejects_weak_or_oversized_values(password: str) -> None:
    with pytest.raises(ValueError):
        hash_admin_password(password)


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
        ({"VIDEO_REPLICA_PUBLIC_ORIGIN": ""}, "PUBLIC_ORIGIN"),
        ({"VIDEO_REPLICA_PUBLIC_ORIGIN": "http://app.example.test"}, "PUBLIC_ORIGIN"),
        (
            {"VIDEO_REPLICA_PUBLIC_ORIGIN": "https://app.example.test/customer"},
            "PUBLIC_ORIGIN",
        ),
        (
            {
                "VIDEO_REPLICA_PUBLIC_ORIGIN": "https://app.example.test:8443",
                "PUBLIC_BASE_URL": "https://app.example.test:8443",
            },
            "PUBLIC_ORIGIN",
        ),
        ({"PUBLIC_BASE_URL": ""}, "PUBLIC_BASE_URL"),
        ({"PUBLIC_BASE_URL": "https://payments.example.test"}, "PUBLIC_BASE_URL"),
        ({"VIDEO_REPLICA_TRUSTED_PROXY_CIDRS": ""}, "TRUSTED_PROXY_CIDRS"),
        ({"VIDEO_REPLICA_TRUSTED_PROXY_CIDRS": "not-a-cidr"}, "TRUSTED_PROXY_CIDRS"),
        ({"VIDEO_REPLICA_TRUSTED_PROXY_CIDRS": "0.0.0.0/0"}, "TRUSTED_PROXY_CIDRS"),
        ({"VIDEO_REPLICA_ACTIVATION_CODE_HMAC_KEY": ""}, "ACTIVATION_CODE_HMAC_KEY"),
        ({"VIDEO_REPLICA_ACTIVATION_CODE_HMAC_KEY": "short"}, "ACTIVATION_CODE_HMAC_KEY"),
        ({"VIDEO_REPLICA_ACTIVATION_EXPORT_AEAD_KEY": ""}, "ACTIVATION_EXPORT_AEAD_KEY"),
        (
            {"VIDEO_REPLICA_ACTIVATION_EXPORT_AEAD_KEY": "not-a-32-byte-key"},
            "ACTIVATION_EXPORT_AEAD_KEY",
        ),
        ({"VIDEO_REPLICA_DEVICE_FINGERPRINT_HMAC_KEY": ""}, "DEVICE_FINGERPRINT_HMAC_KEY"),
        ({"VIDEO_REPLICA_DEVICE_FINGERPRINT_HMAC_KEY": "short"}, "DEVICE_FINGERPRINT_HMAC_KEY"),
        (
            {"VIDEO_REPLICA_CUSTOMER_IDEMPOTENCY_AEAD_KEY": ""},
            "CUSTOMER_IDEMPOTENCY_AEAD_KEY",
        ),
        (
            {"VIDEO_REPLICA_CUSTOMER_IDEMPOTENCY_AEAD_KEY": "not-a-32-byte-key"},
            "CUSTOMER_IDEMPOTENCY_AEAD_KEY",
        ),
        ({"VIDEO_REPLICA_SETTINGS_KEY": ""}, "SETTINGS_KEY"),
        ({"VIDEO_REPLICA_SETTINGS_KEY": "not-a-fernet-key"}, "SETTINGS_KEY"),
        (
            {"VIDEO_REPLICA_ADMIN_SESSION_TTL_SECONDS": str(24 * 3600 + 1)},
            "ADMIN_SESSION_TTL_SECONDS",
        ),
        ({"VIDEO_REPLICA_ADMIN_SESSION_TTL_SECONDS": "0"}, "ADMIN_SESSION_TTL_SECONDS"),
        ({"VIDEO_REPLICA_ADMIN_SESSION_TTL_SECONDS": "not-a-number"}, "ADMIN_SESSION_TTL_SECONDS"),
    ],
)
def test_security_gate_rejects_each_violation(overrides: dict[str, str], message: str) -> None:
    env = _clean_production_env()
    env.update(overrides)
    with _env(**env):
        with pytest.raises(RuntimeError, match=message):
            assert_customer_production_security()


def test_security_gate_accepts_in_range_admin_session_ttl() -> None:
    """M1 review M2: an in-range TTL passes the startup gate (the boundary
    values themselves stay legal)."""
    env = _clean_production_env()
    env["VIDEO_REPLICA_ADMIN_SESSION_TTL_SECONDS"] = "3600"
    with _env(**env):
        assert_customer_production_security()  # must not raise


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


def test_security_gate_allows_startup_with_only_rotated_v2_key() -> None:
    """PR review P2: after rotation retires V1, a configured later key
    version must keep customer production booting instead of tripping a
    hardcoded V1-only existence check."""
    env = _clean_production_env()
    env[ADMIN_SESSION_HMAC_KEY_ENV] = ""
    env[f"{ADMIN_SESSION_HMAC_KEY_ENV}_V1"] = ""
    env[f"{ADMIN_SESSION_HMAC_KEY_ENV}_V2"] = TEST_KEY
    with _env(**env):
        assert_customer_production_security()  # must not raise


def test_security_gate_allows_only_rotated_v2_for_every_customer_key() -> None:
    """T35: retiring all V1 aliases must not break a valid V2-only boot."""
    env = _clean_production_env()
    for base_env in (
        "VIDEO_REPLICA_ACTIVATION_CODE_HMAC_KEY",
        "VIDEO_REPLICA_ACTIVATION_EXPORT_AEAD_KEY",
        "VIDEO_REPLICA_DEVICE_FINGERPRINT_HMAC_KEY",
        "VIDEO_REPLICA_CUSTOMER_IDEMPOTENCY_AEAD_KEY",
    ):
        env[base_env] = ""
    env["VIDEO_REPLICA_ACTIVATION_CODE_HMAC_KEY_V2"] = TEST_KEY
    env["VIDEO_REPLICA_ACTIVATION_EXPORT_AEAD_KEY_V2"] = TEST_AEAD_KEY
    env["VIDEO_REPLICA_DEVICE_FINGERPRINT_HMAC_KEY_V2"] = TEST_KEY
    env["VIDEO_REPLICA_CUSTOMER_IDEMPOTENCY_AEAD_KEY_V2"] = TEST_AEAD_KEY

    with _env(**env):
        assert_customer_production_security()  # must not raise


def test_security_gate_rejects_short_admin_key_at_startup() -> None:
    """PR review P2: a configured-but-weak key must fail the boot itself,
    not surface later as a runtime ValueError from admin_hmac_key()."""
    env = _clean_production_env()
    env[ADMIN_SESSION_HMAC_KEY_ENV] = "short"
    with _env(**env):
        with pytest.raises(RuntimeError, match="at least 32 bytes"):
            assert_customer_production_security()


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

pytestmark_pg = pytest.mark.usefixtures("pg_hard_gate")


@pytest.fixture(scope="module")
def pg_hard_gate() -> None:
    """CW-007 hard gate: unreachable PG fails the suite (explicit opt-in may skip)."""
    require_pg_or_explicit_skip()


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
    require_pg_or_explicit_skip(PG_DSN)
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
        conn.execute("SET session_replication_role = replica")
        conn.execute(
            "TRUNCATE admin_sessions, admin_password_credentials, audit_logs, "
            "security_rate_limit_counters, security_auth_failures"
        )
        conn.execute("SET session_replication_role = DEFAULT")
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


@pytest.fixture()
def customer_production_control_client(
    monkeypatch: pytest.MonkeyPatch, clean_sessions: str
) -> Iterator[TestClient]:
    """A production-shaped control plane with the real legacy route set mounted.

    The route implementations remain shared with the internal product, but in
    customer production they must resolve the current per-operator session,
    never the retired proxy-token identity.
    """
    from app.admin_auth_routes import router as admin_router
    from app.control_routes import router as control_router

    app = FastAPI()
    app.include_router(admin_router)
    app.include_router(control_router)

    @app.patch("/api/control/_test/control-write")
    def control_write(actor: ControlUser) -> dict[str, str]:
        return {"actor": actor.id}

    monkeypatch.setenv(DATABASE_URL_ENV, clean_sessions)
    monkeypatch.setenv("VIDEO_REPLICA_CUSTOMER_PRODUCTION", "true")
    monkeypatch.setenv(ADMIN_SESSION_HMAC_KEY_ENV, TEST_KEY)
    monkeypatch.setenv("VIDEO_REPLICA_SETTINGS_KEY", TEST_AEAD_KEY)
    monkeypatch.delenv("VIDEO_REPLICA_AUTH_MODE", raising=False)
    monkeypatch.delenv("VIDEO_REPLICA_ALLOW_DEV_IDENTITY_HEADER", raising=False)
    with TestClient(app, base_url="https://testserver") as test_client:
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
        assert response.headers["cache-control"] == "no-store"

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


def test_exchange_credential_single_use(client: TestClient, clean_sessions: str) -> None:
    credential = _issue("admin_u")
    first = client.post("/api/control/admin/session/exchange", json={"credential": credential})
    assert first.status_code == 201
    replay = client.post("/api/control/admin/session/exchange", json={"credential": credential})
    assert replay.status_code == 401
    assert replay.json()["detail"]["code"] == "EXCHANGE_CREDENTIAL_REUSED"
    with psycopg.connect(clean_sessions) as conn:
        failure_count = conn.execute(
            "SELECT count(*) FROM security_auth_failures WHERE dimension = 'admin:exchange:ip'"
        ).fetchone()[0]
    assert int(failure_count) == 1


def test_admin_session_idle_expiry_revokes_cookie_and_is_audited(
    client: TestClient,
    clean_sessions: str,
    admin_session: dict[str, str],
) -> None:
    with psycopg.connect(clean_sessions, autocommit=True) as conn:
        conn.execute(
            "UPDATE admin_sessions SET last_activity_at = now() - interval '31 minutes' "
            "WHERE id = %s",
            (admin_session["session_id"],),
        )

    expired = client.get("/api/control/admin/session")

    assert expired.status_code == 401
    assert expired.json()["detail"]["code"] == "ADMIN_SESSION_IDLE_EXPIRED"
    with psycopg.connect(clean_sessions) as conn:
        row = conn.execute(
            "SELECT revoked_at FROM admin_sessions WHERE id = %s",
            (admin_session["session_id"],),
        ).fetchone()
        audit = conn.execute(
            "SELECT metadata_json FROM audit_logs "
            "WHERE action = 'admin_session.security_rejected' AND entity_id = %s",
            (admin_session["session_id"],),
        ).fetchone()
    assert row is not None and row[0] is not None
    assert audit is not None
    assert json.loads(str(audit[0]))["code"] == "ADMIN_SESSION_IDLE_EXPIRED"


def test_admin_session_context_change_revokes_cookie_and_is_audited(
    client: TestClient,
    clean_sessions: str,
    admin_session: dict[str, str],
) -> None:
    changed = client.get(
        "/api/control/admin/session",
        headers={"User-Agent": "different-admin-browser"},
    )

    assert changed.status_code == 401
    assert changed.json()["detail"]["code"] == "ADMIN_SESSION_CONTEXT_CHANGED"
    with psycopg.connect(clean_sessions) as conn:
        row = conn.execute(
            "SELECT revoked_at FROM admin_sessions WHERE id = %s",
            (admin_session["session_id"],),
        ).fetchone()
        audit = conn.execute(
            "SELECT metadata_json FROM audit_logs "
            "WHERE action = 'admin_session.security_rejected' AND entity_id = %s",
            (admin_session["session_id"],),
        ).fetchone()
    assert row is not None and row[0] is not None
    assert audit is not None
    assert json.loads(str(audit[0]))["code"] == "ADMIN_SESSION_CONTEXT_CHANGED"


def test_exchange_recovery_sets_password_then_password_login_survives_refresh(
    client: TestClient,
    clean_sessions: str,
) -> None:
    raw_password = "Admin Login Passphrase 2026!"
    exchange = client.post(
        "/api/control/admin/session/exchange",
        json={"credential": _issue("admin_u")},
    )
    assert exchange.status_code == 201, exchange.text

    configured = client.put(
        "/api/control/admin/password",
        headers={ADMIN_CSRF_HEADER: exchange.json()["csrf_token"]},
        json={"password": raw_password},
    )
    assert configured.status_code == 204, configured.text
    assert client.get("/api/control/admin/session").status_code == 401

    with psycopg.connect(clean_sessions) as conn:
        password_row = conn.execute(
            "SELECT password_hash FROM admin_password_credentials WHERE user_id = 'admin_u'"
        ).fetchone()
        recovery_session = conn.execute(
            "SELECT auth_method, revoked_at FROM admin_sessions WHERE id = %s",
            (exchange.json()["session_id"],),
        ).fetchone()
        audit = conn.execute(
            "SELECT action FROM audit_logs WHERE actor_user_id = 'admin_u' "
            "AND action = 'admin_password.recover'"
        ).fetchone()
    assert password_row is not None
    assert raw_password not in str(password_row[0])
    assert verify_admin_password(raw_password, str(password_row[0])) is True
    assert recovery_session is not None
    assert (str(recovery_session[0]), recovery_session[1] is not None) == ("exchange", True)
    assert audit is not None

    login = client.post(
        "/api/control/admin/session/password",
        json={"username": "admin_u", "password": raw_password},
    )
    assert login.status_code == 201, login.text
    assert login.json()["actor"]["username"] == "admin_u"
    assert login.json()["csrf_token"]

    refreshed = client.get("/api/control/admin/session")
    assert refreshed.status_code == 200, refreshed.text
    assert refreshed.headers["cache-control"] == "no-store"
    assert refreshed.json()["csrf_token"] == login.json()["csrf_token"]
    allowed = client.post(
        "/api/control/_test/admin-write",
        headers={ADMIN_CSRF_HEADER: refreshed.json()["csrf_token"]},
    )
    assert allowed.status_code == 200, allowed.text
    with psycopg.connect(clean_sessions) as conn:
        password_session = conn.execute(
            "SELECT auth_method FROM admin_sessions WHERE id = %s",
            (login.json()["session_id"],),
        ).fetchone()
    assert password_session is not None and str(password_session[0]) == "password"


def test_password_login_has_unified_failure_and_shared_ip_account_budgets(
    client: TestClient,
    clean_sessions: str,
) -> None:
    exchange = client.post(
        "/api/control/admin/session/exchange",
        json={"credential": _issue("admin_u")},
    )
    assert exchange.status_code == 201
    assert (
        client.put(
            "/api/control/admin/password",
            headers={ADMIN_CSRF_HEADER: exchange.json()["csrf_token"]},
            json={"password": "Another Admin Passphrase 2026!"},
        ).status_code
        == 204
    )

    wrong = client.post(
        "/api/control/admin/session/password",
        json={"username": "admin_u", "password": "Wrong Admin Passphrase!"},
    )
    unknown = client.post(
        "/api/control/admin/session/password",
        json={"username": "ghost_admin", "password": "Wrong Admin Passphrase!"},
    )

    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json()
    assert wrong.json()["detail"]["code"] == "ADMIN_LOGIN_INVALID"
    with psycopg.connect(clean_sessions) as conn:
        counters = conn.execute(
            "SELECT bucket_key FROM security_rate_limit_counters "
            "WHERE bucket_key LIKE 'login:%' ORDER BY bucket_key"
        ).fetchall()
        failures = conn.execute(
            "SELECT dimension, identifier FROM security_auth_failures "
            "WHERE dimension IN ('login:ip', 'login:account')"
        ).fetchall()
    assert {str(row[0]).split("|", 1)[0] for row in counters} == {
        "login:account",
        "login:ip",
    }
    assert len(failures) == 4
    assert all(len(str(row[1])) == 64 for row in failures)
    assert all("admin_u" not in str(row) and "ghost_admin" not in str(row) for row in failures)


def test_password_session_cannot_reset_password_without_recovery_exchange(
    client: TestClient,
) -> None:
    exchange = client.post(
        "/api/control/admin/session/exchange",
        json={"credential": _issue("admin_u")},
    )
    assert exchange.status_code == 201
    assert (
        client.put(
            "/api/control/admin/password",
            headers={ADMIN_CSRF_HEADER: exchange.json()["csrf_token"]},
            json={"password": "Initial Admin Passphrase 2026!"},
        ).status_code
        == 204
    )
    login = client.post(
        "/api/control/admin/session/password",
        json={"username": "admin_u", "password": "Initial Admin Passphrase 2026!"},
    )
    assert login.status_code == 201

    reset = client.put(
        "/api/control/admin/password",
        headers={ADMIN_CSRF_HEADER: login.json()["csrf_token"]},
        json={"password": "Changed Admin Passphrase 2026!"},
    )

    assert reset.status_code == 403
    assert reset.json()["detail"]["code"] == "ADMIN_PASSWORD_RECOVERY_REQUIRED"


def test_exchange_rejects_expired_credential(client: TestClient) -> None:
    credential = issue_exchange_credential(
        "admin_u", ttl_seconds=60, now=datetime.now(UTC) - timedelta(seconds=120)
    )
    response = client.post("/api/control/admin/session/exchange", json={"credential": credential})
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "EXCHANGE_CREDENTIAL_INVALID"


def test_exchange_uses_the_shared_postgres_ip_budget(
    client: TestClient,
    clean_sessions: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VIDEO_REPLICA_RATE_LIMIT_ADMIN_EXCHANGE_IP", "2")

    first = client.post(
        "/api/control/admin/session/exchange",
        json={"credential": "invalid-one"},
    )
    second = client.post(
        "/api/control/admin/session/exchange",
        json={"credential": "invalid-two"},
    )
    blocked = client.post(
        "/api/control/admin/session/exchange",
        json={"credential": "invalid-three"},
    )

    assert first.status_code == 401
    assert second.status_code == 401
    assert blocked.status_code == 429
    assert blocked.json()["detail"]["code"] == "RATE_LIMITED"
    assert int(blocked.headers["Retry-After"]) >= 1

    with psycopg.connect(clean_sessions) as conn:
        bucket = conn.execute(
            "SELECT hit_count FROM security_rate_limit_counters "
            "WHERE bucket_key LIKE 'admin:exchange:ip|%'"
        ).fetchone()
        failures = conn.execute(
            "SELECT count(*), bool_and(length(identifier) = 64) "
            "FROM security_auth_failures WHERE dimension = 'admin:exchange:ip'"
        ).fetchone()
    assert bucket is not None and int(bucket[0]) == 3
    assert failures is not None and (int(failures[0]), bool(failures[1])) == (3, True)


def test_exchange_rejects_employee_role(client: TestClient, clean_sessions: str) -> None:
    response = client.post(
        "/api/control/admin/session/exchange", json={"credential": _issue("employee_u")}
    )
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "ADMIN_ROLE_REQUIRED"
    with psycopg.connect(clean_sessions) as conn:
        failure_count = conn.execute(
            "SELECT count(*) FROM security_auth_failures WHERE dimension = 'admin:exchange:ip'"
        ).fetchone()[0]
    assert int(failure_count) == 1


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


def test_session_lifecycle_writes_persistent_audit_log(
    client: TestClient, admin_session: dict[str, str], clean_sessions: str
) -> None:
    """M1 review M1: exchange (credential consumption / login) and revoke are
    security-critical lifecycle events — they must land in audit_logs with a
    queryable timeline, not only in application logs."""

    def audit_rows(session_id: str) -> list[tuple[str, str, str, str]]:
        with psycopg.connect(clean_sessions) as conn:
            return [
                (str(row[0]), str(row[1]), str(row[2]), str(row[3]))
                for row in conn.execute(
                    "SELECT actor_user_id, action, entity_type, entity_id "
                    "FROM audit_logs WHERE entity_id = %s ORDER BY created_at, id",
                    (session_id,),
                ).fetchall()
            ]

    session_id = admin_session["session_id"]
    assert audit_rows(session_id) == [
        ("admin_u", "admin_session.exchange", "admin_session", session_id)
    ]
    logout = client.delete(
        "/api/control/admin/session", headers={ADMIN_CSRF_HEADER: admin_session["csrf_token"]}
    )
    assert logout.status_code == 204
    assert audit_rows(session_id) == [
        ("admin_u", "admin_session.exchange", "admin_session", session_id),
        ("admin_u", "admin_session.revoke", "admin_session", session_id),
    ]


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
    # Keep a generous margin from the database clock. A one-second offset can
    # race the PostgreSQL container clock and accidentally exercise idle expiry.
    expired = datetime.now(UTC) - timedelta(minutes=5)
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


@pytestmark_pg
def test_customer_production_control_routes_use_operator_session(
    customer_production_control_client: TestClient,
) -> None:
    """The original account/wallet screen must be usable after session login.

    This locks the regression that sent the page through ``get_control_user``
    and therefore rejected every customer-production request as legacy 403.
    """
    client = customer_production_control_client
    exchange = client.post(
        "/api/control/admin/session/exchange", json={"credential": _issue("admin_u")}
    )
    assert exchange.status_code == 201, exchange.text

    accounts = client.get("/api/control/accounts")
    assert accounts.status_code == 200, accounts.text

    missing_csrf = client.patch("/api/control/_test/control-write")
    assert missing_csrf.status_code == 403
    assert missing_csrf.json()["detail"]["code"] == "ADMIN_CSRF_REQUIRED"

    allowed = client.patch(
        "/api/control/_test/control-write",
        headers={ADMIN_CSRF_HEADER: exchange.json()["csrf_token"]},
    )
    assert allowed.status_code == 200, allowed.text
    assert allowed.json() == {"actor": "admin_u"}

    client.cookies.clear()
    rejected = client.get(
        "/api/control/accounts", headers={"X-Control-Proxy-Token": "legacy-token"}
    )
    assert rejected.status_code == 401
    assert rejected.json()["detail"]["code"] == "ADMIN_SESSION_INVALID"


@pytestmark_pg
def test_customer_production_control_routes_keep_auditors_read_only(
    customer_production_control_client: TestClient,
) -> None:
    client = customer_production_control_client
    exchange = client.post(
        "/api/control/admin/session/exchange", json={"credential": _issue("auditor_u")}
    )
    assert exchange.status_code == 201, exchange.text

    assert client.get("/api/control/accounts").status_code == 200
    blocked = client.patch(
        "/api/control/_test/control-write",
        headers={ADMIN_CSRF_HEADER: exchange.json()["csrf_token"]},
    )
    assert blocked.status_code == 403
    assert blocked.json()["detail"]["code"] == "AUDITOR_READ_ONLY"


@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [
        (
            "PATCH",
            "/api/control/settings/runtime",
            {
                "max_generation_count_per_batch": 4,
                "max_concurrent_h3_tasks": 2,
                "active_storage_provider": "cos",
            },
        ),
        (
            "PATCH",
            "/api/control/settings/zpay",
            {
                "pid": "merchant-1",
                "key": "merchant-secret",
                "enabled_channels": ["alipay"],
            },
        ),
        (
            "PATCH",
            "/api/control/settings/billing",
            {
                "internal_base_unit_price_fen": 1000,
                "oral_unit_price_fen": 1000,
                "min_recharge_fen": 10000,
                "recharge_step_fen": 1000,
            },
        ),
        (
            "PUT",
            "/api/control/settings/providers/metaso",
            {"config": {"api_key": "metaso-secret"}},
        ),
    ],
)
@pytestmark_pg
def test_customer_production_control_settings_writes_require_admin_write_contract(
    customer_production_control_client: TestClient,
    method: str,
    path: str,
    payload: dict[str, object],
) -> None:
    client = customer_production_control_client
    exchange = client.post(
        "/api/control/admin/session/exchange", json={"credential": _issue("admin_u")}
    )
    assert exchange.status_code == 201, exchange.text
    headers = {ADMIN_CSRF_HEADER: exchange.json()["csrf_token"]}

    missing_key = client.request(
        method,
        path,
        headers=headers,
        json={**payload, "confirm": True, "reason": "生产控制台更新"},
    )
    assert missing_key.status_code == 400
    assert missing_key.json()["detail"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"

    missing_confirm = client.request(
        method,
        path,
        headers={**headers, "Idempotency-Key": f"missing-confirm-{method}-{path}"},
        json={**payload, "reason": "生产控制台更新"},
    )
    assert missing_confirm.status_code == 400
    assert missing_confirm.json()["detail"]["code"] == "CONFIRMATION_REQUIRED"

    blank_reason = client.request(
        method,
        path,
        headers={**headers, "Idempotency-Key": f"blank-reason-{method}-{path}"},
        json={**payload, "confirm": True, "reason": "   "},
    )
    assert blank_reason.status_code == 400
    assert blank_reason.json()["detail"]["code"] == "REASON_REQUIRED"


@pytestmark_pg
def test_customer_production_control_runtime_write_replays_and_conflicts_by_idempotency_key(
    customer_production_control_client: TestClient,
    clean_sessions: str,
) -> None:
    client = customer_production_control_client
    exchange = client.post(
        "/api/control/admin/session/exchange", json={"credential": _issue("admin_u")}
    )
    assert exchange.status_code == 201, exchange.text
    headers = {
        ADMIN_CSRF_HEADER: exchange.json()["csrf_token"],
        "Idempotency-Key": "control-runtime-idem-1",
    }
    payload = {
        "max_generation_count_per_batch": 4,
        "max_concurrent_h3_tasks": 2,
        "active_storage_provider": "cos",
        "confirm": True,
        "reason": "收紧生产并发",
    }

    first = client.patch("/api/control/settings/runtime", headers=headers, json=payload)
    replay = client.patch("/api/control/settings/runtime", headers=headers, json=payload)
    conflict = client.patch(
        "/api/control/settings/runtime",
        headers=headers,
        json={**payload, "max_concurrent_h3_tasks": 1},
    )

    assert first.status_code == 200, first.text
    assert first.json() == {
        "max_generation_count_per_batch": 4,
        "max_concurrent_h3_tasks": 2,
        "active_storage_provider": "cos",
    }
    assert replay.status_code == 200, replay.text
    assert replay.json() == first.json()
    assert replay.headers["X-Idempotent-Replay"] == "true"
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "IDEMPOTENCY_CONFLICT"

    with psycopg.connect(clean_sessions) as conn:
        snapshots = conn.execute(
            "SELECT count(*) FROM admin_write_idempotency "
            "WHERE actor_user_id = 'admin_u' AND route = 'PATCH /api/control/settings/runtime'"
        ).fetchone()
        audits = conn.execute(
            "SELECT count(*), max(metadata_json) FROM audit_logs "
            "WHERE actor_user_id = 'admin_u' AND action = 'runtime_settings.update'"
        ).fetchone()
    assert snapshots is not None and int(snapshots[0]) == 1
    assert audits is not None and int(audits[0]) == 1
    audit_metadata = json.loads(str(audits[1]))
    assert audit_metadata["reason"] == "收紧生产并发"
    assert audit_metadata["request_id"]


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


# ---------------------------------------------------------------------------
# M1 review H1: the real app lifespan must run the database-mode fail-closed
# check, not only the key/identity security gate.
# ---------------------------------------------------------------------------


def test_api_lifespan_fails_closed_on_sqlite_lane_in_customer_production() -> None:
    """A direct uvicorn-style boot of the real ``app.main`` app with a
    customer-production env that still resolves to the SQLite lane must abort
    startup. The key/identity gate passes with this env, so only the
    database-mode check can catch it."""
    from app.main import app as real_app

    env = _clean_production_env()
    env[DATABASE_URL_ENV] = "sqlite:///data/app.db"
    with _env(**env):
        with pytest.raises(RuntimeError, match="requires PostgreSQL"):
            with TestClient(real_app):
                pass


def test_api_lifespan_fails_closed_on_missing_dsn_in_customer_production() -> None:
    """Customer production without any database URL must abort the real app's
    lifespan with the production-facing message (resolve raises RuntimeError
    for the customer boundary; the generic internal ValueError must not leak
    a boot)."""
    from app.main import app as real_app

    env = _clean_production_env()
    env[DATABASE_URL_ENV] = ""
    with _env(**env):
        with pytest.raises(RuntimeError, match="customer production requires PostgreSQL"):
            with TestClient(real_app):
                pass


def test_api_lifespan_fails_closed_on_postgres_without_tls_in_customer_production() -> None:
    """A syntactically valid PG URL must not inherit libpq's plaintext-capable
    ``sslmode=prefer`` default at the real customer API startup boundary."""
    from app.main import app as real_app

    env = _clean_production_env()
    env[DATABASE_URL_ENV] = "postgresql://u:p@db.example.com:5432/production"
    with _env(**env):
        with pytest.raises(RuntimeError, match="must enforce TLS"):
            with TestClient(real_app):
                pass


def test_api_lifespan_tolerates_internal_lane_without_database_env() -> None:
    """Regression lock: the new lifespan check must not break the internal /
    test lane that sets no database environment at all (the legacy lane
    resolves per-request; only the customer boundary fails closed here)."""
    from app.main import app as real_app

    with _env(
        **{
            DATABASE_URL_ENV: "",
            "VIDEO_REPLICA_DB_PATH": "",
            "VIDEO_REPLICA_CUSTOMER_PRODUCTION": "",
        }
    ):
        with TestClient(real_app):
            pass


def test_api_lifespan_fails_closed_on_unsupported_scheme_in_customer_production() -> None:
    """A mistyped database URL scheme in customer production must abort the
    real app's lifespan: the unsupported-scheme ValueError is a configuration
    error, not the tolerated missing-config case, so a healthy startup must
    never be advertised without a usable PostgreSQL runtime (Codex P1)."""
    from app.main import app as real_app

    env = _clean_production_env()
    env[DATABASE_URL_ENV] = "mysql://u:p@db.example.com:5432/production"
    with _env(**env):
        with pytest.raises(ValueError, match="unsupported database URL scheme"):
            with TestClient(real_app):
                pass


def test_short_admin_hmac_key_raises_exchange_credential_error() -> None:
    """A short key must reach the exchange 401 channel, not surface as a 500.

    ``ExchangeCredentialError`` is a ``ValueError`` subclass; the plain
    ``ValueError`` the resolver currently raises slips past the route's
    handler and answers 500 (M1 review LOW).
    """
    from app.admin_auth_routes import ADMIN_SESSION_HMAC_KEY_ENV, admin_hmac_key

    with pytest.raises(ExchangeCredentialError, match="at least"):
        admin_hmac_key(1, environ={ADMIN_SESSION_HMAC_KEY_ENV: "short"})


def test_admin_key_discovery_rejects_zero_padded_version_suffixes() -> None:
    """``_V01`` must not boot the door open (M1 review LOW).

    Zero-padded suffixes pass ``int(suffix) >= 1`` but ``admin_hmac_key(1)``
    only reads ``_V1`` — accepting ``_V01`` in discovery yields the
    "boots fine, every login 401" configuration trap.
    """
    from app.bootstrap import _ADMIN_KEY_VERSION_PREFIX, _configured_admin_session_keys

    key = "k" * 64
    padded = f"{_ADMIN_KEY_VERSION_PREFIX}01"
    assert _configured_admin_session_keys({padded: key}) == []
    legal = f"{_ADMIN_KEY_VERSION_PREFIX}2"
    assert _configured_admin_session_keys({legal: key}) == [(legal, key)]
