"""T19 / SES-01 — login, heartbeat, logout and the 30/90-second DB lease.

Fail-first tests for the frozen files ``server/app/customer_session_service.py``
and ``server/app/customer_session_routes.py`` (code checklist §3.2 / §3.3):
the single-online session half of the customer runtime, building on the T13
activation transaction (which grants the first epoch-1 lease) and the T16
device credential layer (which authenticates the login caller).

Contract under test (task list §4 T19; dev doc §3.3 / §6.1 / §12.3 / §13.2;
acceptance spec §2.3 / §3.4):

- ``POST /api/customer/sessions/login`` authenticates the *device* credential
  (Bearer) and drives the dev-doc §12.3 state machine under a row lock on
  ``customer_session_state``:

  * no session row / expired lease -> establish the caller's session
    (epoch + 1, fresh session token, lease = now + 90 s, ``LOGIN`` event; a
    lapsed prior lease additionally records the system ``TIMEOUT`` event);
  * same device + currently valid session token presented in the body ->
    renew only: same token, same epoch, lease pushed to now + 90 s;
  * same device without a usable session token -> epoch + 1 and a fresh
    token (the recovery path — the old token can never heartbeat again);
  * another device online with a live lease -> 409 ``OTHER_DEVICE_ONLINE``
    with the masked device name and the remaining lease (never a silent
    kick, dev doc §3.3);

- ``POST /api/customer/sessions/heartbeat`` authenticates the *session* token
  (Bearer): a matching token under a live lease renews the lease (epoch
  untouched — §12.3 same-device lease extension) and appends a ``HEARTBEAT``
  event; a token that no longer matches the row answers 401
  ``SESSION_REPLACED``; a matching token under a lapsed lease answers 401
  ``SESSION_EXPIRED`` and must never resurrect the session (acceptance §3.4);

- ``POST /api/customer/sessions/logout`` authenticates the session token,
  pulls the lease into the past (the T16 unbind precedent) and appends a
  ``LOGOUT`` event: the released slot is immediately re-loggable by the other
  device, the old token's heartbeat fails with 401 ``SESSION_EXPIRED``, and a
  *late* logout after another device took over answers 401
  ``SESSION_REPLACED`` without touching the new session;

- login and logout carry a mandatory ``Idempotency-Key`` (dev doc §6.3): a
  lost response replays through the sealed envelope (same token, same epoch,
  no second ``LOGIN`` event, ``X-Idempotent-Replay: true``), while the same
  key against a different request body answers 409 ``IDEMPOTENCY_CONFLICT``;

- login is rate limited per IP through the T15 shared counters (the
  ``login:ip`` dimension the security module already defines) and answers
  429 ``RATE_LIMITED`` once the budget is spent — while a fully validated
  idempotent replay short-circuits *before* the limiter and spends no budget
  (the activation-route T15 review rule);

- A/B concurrent logins from an expired-lease state leave exactly one
  current device (acceptance §3.4) — the row lock serializes the writers.
"""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.activation_code_service import (
    ACTIVATION_CODE_HMAC_KEY_ENV,
    compute_code_digest,
)
from app.db_pg import DATABASE_URL_ENV, close_pg_pool

DEFAULT_DSN = "postgresql://testuser:testpass@localhost:5433/customer_v3_test"
SKIP_REASON = "PostgreSQL fixture not reachable; start it via scripts/pg-fixture.sh start"

T19_DB_NAME = "t19_customer_sessions_test"

TEST_KEY = secrets.token_urlsafe(48)  # code HMAC key (v1), never a real secret
TEST_FINGERPRINT_KEY_V1 = secrets.token_urlsafe(48)
TEST_FINGERPRINT_KEY_V2 = secrets.token_urlsafe(48)
TEST_ENVELOPE_AEAD_KEY = secrets.token_bytes(32)

ACTIVATE_PATH = "/api/customer/activate"
LOGIN_PATH = "/api/customer/sessions/login"
HEARTBEAT_PATH = "/api/customer/sessions/heartbeat"
LOGOUT_PATH = "/api/customer/sessions/logout"
IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"
REQUEST_ID_HEADER = "X-Request-Id"
REPLAY_HEADER = "X-Idempotent-Replay"
RETRY_AFTER_HEADER = "Retry-After"
AUTHORIZATION_HEADER = "Authorization"
FUTURE_EXPIRY = "2099-01-01T00:00:00+00:00"

# Canonical Crockford-shaped codes (prefix + 4 groups x 7 characters).
FIRST_CODE = "XS04-AAAAAAA-BBBBBBB-CCCCCCC-DDDDDDD"
SECOND_CODE = "XS04-CCCCCCC-DDDDDDD-EEEEEEE-FFFFFFF"

COUNTERS_TABLE = "security_rate_limit_counters"
FAILURES_TABLE = "security_auth_failures"


def _pg_dsn() -> str:
    return os.environ.get("TEST_POSTGRESQL_URL", DEFAULT_DSN)


def _admin_dsn() -> str:
    return _pg_dsn().rsplit("/", 1)[0] + "/postgres"


def _t19_dsn() -> str:
    return _pg_dsn().rsplit("/", 1)[0] + f"/{T19_DB_NAME}"


def _pg_available(dsn: str) -> bool:
    try:
        conn = psycopg.connect(dsn, connect_timeout=3)
        conn.close()
    except Exception:
        return False
    return True


# ---------------------------------------------------------------------------
# Module-level units (no database) — always run
# ---------------------------------------------------------------------------


def test_lease_seconds_matches_the_frozen_30_90_contract() -> None:
    from app.customer_session_service import SESSION_LEASE_SECONDS

    assert SESSION_LEASE_SECONDS == 90


def test_mask_device_name_keeps_a_short_hint_only() -> None:
    from app.customer_session_service import mask_device_name

    masked = mask_device_name("Zhang-san MacBook Pro")
    assert "Zhang" not in masked
    assert "MacBook" not in masked
    assert masked and set(masked.replace("*", "")) <= set("Zhang-san MacBook Pro")


def test_mask_device_name_handles_short_names() -> None:
    from app.customer_session_service import mask_device_name

    assert mask_device_name("") == ""
    assert mask_device_name("A") == "*"
    assert mask_device_name("AB") == "*"


def test_login_ip_limit_env_falls_back_to_safe_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CodeReview P3-5: the T15 _positive_int_env semantics — an env typo or
    a non-positive value can never disable or strangle the login limiter."""
    from app.security_rate_limit import DEFAULT_LOGIN_IP_LIMIT, login_ip_limit

    monkeypatch.delenv("VIDEO_REPLICA_RATE_LIMIT_LOGIN_IP", raising=False)
    assert login_ip_limit() == DEFAULT_LOGIN_IP_LIMIT == 10
    for bad in ("0", "-5", "abc", ""):
        monkeypatch.setenv("VIDEO_REPLICA_RATE_LIMIT_LOGIN_IP", bad)
        assert login_ip_limit() == DEFAULT_LOGIN_IP_LIMIT, bad
    monkeypatch.setenv("VIDEO_REPLICA_RATE_LIMIT_LOGIN_IP", "7")
    assert login_ip_limit() == 7


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
        conn.execute(f'DROP DATABASE IF EXISTS "{T19_DB_NAME}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{T19_DB_NAME}"')
    server_dir = Path(__file__).resolve().parent.parent
    config = Config(str(server_dir / "alembic.ini"))
    config.set_main_option("script_location", str(server_dir / "migrations"))
    config.set_main_option(
        "sqlalchemy.url", _t19_dsn().replace("postgresql://", "postgresql+psycopg://")
    )
    command.upgrade(config, "head")
    try:
        yield _t19_dsn()
    finally:
        with psycopg.connect(_admin_dsn(), autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{T19_DB_NAME}" WITH (FORCE)')


@pytest.fixture()
def route_state(sessions_dsn: str) -> Iterator[str]:
    close_pg_pool()
    with psycopg.connect(_t19_dsn(), autocommit=True) as conn:
        # 036 refuses TRUNCATE of the append-only audit tables; the replica
        # role suspends triggers for this cleanup sweep only.
        conn.execute("SET session_replication_role = replica")
        conn.execute(
            "TRUNCATE customer_session_events, customer_session_state, "
            "customer_idempotency_envelopes, device_pairing_requests, "
            "customer_devices, activation_code_events, activation_code_activations, "
            "activation_code_deliveries, activation_code_exports, activation_codes, "
            "activation_code_batches, admin_write_idempotency, admin_sessions, "
            "wallet_transactions, recharge_orders, wallets, users, "
            f"{COUNTERS_TABLE}, {FAILURES_TABLE} CASCADE"
        )
        conn.execute("SET session_replication_role = DEFAULT")
        conn.execute(
            "INSERT INTO users (id, username, display_name, role) "
            "VALUES ('admin_u', 'admin_u', 'Admin User', 'admin')"
        )
    yield sessions_dsn
    close_pg_pool()


@pytest.fixture()
def customer_app(monkeypatch: pytest.MonkeyPatch, route_state: str) -> Iterator[FastAPI]:
    from app.activation_code_routes import router as activation_code_router
    from app.customer_session_routes import router as customer_session_router

    app = FastAPI()
    app.include_router(activation_code_router)
    app.include_router(customer_session_router)
    monkeypatch.setenv(DATABASE_URL_ENV, route_state)
    monkeypatch.delenv("VIDEO_REPLICA_CUSTOMER_PRODUCTION", raising=False)
    monkeypatch.setenv(ACTIVATION_CODE_HMAC_KEY_ENV, TEST_KEY)
    monkeypatch.setenv("VIDEO_REPLICA_DEVICE_FINGERPRINT_HMAC_KEY", TEST_FINGERPRINT_KEY_V1)
    monkeypatch.setenv("VIDEO_REPLICA_DEVICE_FINGERPRINT_HMAC_KEY_V2", TEST_FINGERPRINT_KEY_V2)
    monkeypatch.setenv(
        "VIDEO_REPLICA_CUSTOMER_IDEMPOTENCY_AEAD_KEY",
        base64.urlsafe_b64encode(TEST_ENVELOPE_AEAD_KEY).decode("ascii").rstrip("="),
    )
    # Roomy budgets: these tests exercise the session routes, not the limiter
    # (one dedicated test narrows the budget on purpose).
    monkeypatch.setenv("VIDEO_REPLICA_RATE_LIMIT_ACTIVATE_IP", "1000")
    monkeypatch.setenv("VIDEO_REPLICA_RATE_LIMIT_ACTIVATE_CODE", "1000")
    monkeypatch.setenv("VIDEO_REPLICA_RATE_LIMIT_LOGIN_IP", "1000")
    monkeypatch.setenv("VIDEO_REPLICA_RATE_LIMIT_WINDOW_SECONDS", "300")
    yield app


@pytest.fixture()
def client(customer_app: FastAPI) -> Iterator[TestClient]:
    with TestClient(customer_app) as test_client:
        yield test_client


# ---------------------------------------------------------------------------
# Seed helpers (the T16 test precedents)
# ---------------------------------------------------------------------------


def _seed_issuable_code(code: str, *, code_id: str, batch_id: str) -> None:
    """Insert one OPEN batch + ISSUED code pair (T12 shapes)."""
    with psycopg.connect(_t19_dsn(), autocommit=True) as conn:
        conn.execute(
            "INSERT INTO activation_code_batches "
            "(id, name, face_value_fen, unit_price_fen_snapshot, credits_snapshot, "
            "quantity, activation_expires_at, status, created_by_user_id) "
            f"VALUES ('{batch_id}', 'batch-{batch_id}', 1500, 1000, 100, 1, "
            f"'{FUTURE_EXPIRY}', 'OPEN', 'admin_u')"
        )
        digest = compute_code_digest(code, key=TEST_KEY.encode("utf-8"))
        conn.execute(
            "INSERT INTO activation_codes "
            "(id, batch_id, code_digest, digest_key_version, masked_code, "
            "status, issued_at) "
            f"VALUES ('{code_id}', '{batch_id}', %s, 1, 'XS04-****', "
            "'ISSUED', '2026-01-01T00:00:00+00:00')",
            (digest,),
        )


def _activate(client: TestClient, code: str, fingerprint: str, key_suffix: str) -> object:
    return client.post(
        ACTIVATE_PATH,
        json={
            "activation_code": code,
            "device_fingerprint": fingerprint,
            "device_name": f"Device {key_suffix}",
            "device_platform": "windows",
        },
        headers={IDEMPOTENCY_KEY_HEADER: f"idem-{key_suffix}"},
    )


def _activated_customer(client: TestClient, *, code: str, fingerprint: str, suffix: str) -> dict:
    """Seed + activate one customer; assert success and return the payload."""
    _seed_issuable_code(code, code_id=f"code-{suffix}", batch_id=f"batch-{suffix}")
    response = _activate(client, code, fingerprint, suffix)
    assert response.status_code == 201, response.text
    payload = response.json()
    for field in ("user_id", "device_id", "device_token", "session_token"):
        assert isinstance(payload.get(field), str) and payload[field], payload
    return payload


def _second_device_row(
    *,
    user_id: str,
    activation_code_id: str,
    device_id: str,
    slot_no: int,
    display_name: str = "Second Device",
) -> str:
    """Insert a second BOUND device directly (the T17 enroll path is out of
    scope here). Returns the plaintext token the caller then presents."""
    from app.customer_device_service import highest_device_domain_key, keyed_digest

    token = secrets.token_urlsafe(32)
    version, key = highest_device_domain_key()
    digest = keyed_digest(key, token)
    with psycopg.connect(_t19_dsn(), autocommit=True) as conn:
        conn.execute(
            "INSERT INTO customer_devices "
            "(id, activation_code_id, user_id, slot_no, display_name, platform, "
            " fingerprint_hmac, fingerprint_key_version, token_digest, token_key_version) "
            "VALUES (%s, %s, %s, %s, %s, 'macos', %s, %s, %s, %s)",
            (
                device_id,
                activation_code_id,
                user_id,
                slot_no,
                display_name,
                keyed_digest(key, f"fp-second-{device_id}"),
                version,
                digest,
                version,
            ),
        )
    return token


def _bearer(token: str) -> dict[str, str]:
    return {AUTHORIZATION_HEADER: f"Bearer {token}"}


def _session_row() -> tuple[str, str, int, str, str]:
    """The live session row: (device_id, session_id, epoch, lease_until, token_digest)."""
    with psycopg.connect(_t19_dsn(), autocommit=True) as conn:
        row = conn.execute(
            "SELECT device_id, session_id, session_epoch, lease_until, token_digest "
            "FROM customer_session_state"
        ).fetchone()
    assert row is not None, "expected exactly one session row"
    return str(row[0]), str(row[1]), int(row[2]), str(row[3]), str(row[4])


def _session_events() -> list[tuple[str, int, str | None]]:
    """All session events: (event, epoch, reason) ordered by creation."""
    with psycopg.connect(_t19_dsn(), autocommit=True) as conn:
        rows = conn.execute(
            "SELECT event, session_epoch, reason FROM customer_session_events "
            "ORDER BY created_at, id"
        ).fetchall()
    return [(str(r[0]), int(r[1]), r[2] if r[2] is None else str(r[2])) for r in rows]


def _expire_lease(device_id: str) -> None:
    """Pull the live lease into the past (the crash-recovery simulation).

    The GREATEST backstop mirrors the production logout pattern: the
    lease_after_created CHECK holds even for a lapsed lease (a just-created
    session's now()-10s lands before its created_at).
    """
    with psycopg.connect(_t19_dsn(), autocommit=True) as conn:
        conn.execute(
            "UPDATE customer_session_state "
            "SET lease_until = GREATEST("
            "(now() - interval '10 seconds'), "
            "created_at::timestamptz + interval '1 microsecond')::text "
            "WHERE device_id = %s",
            (device_id,),
        )


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# POST /api/customer/sessions/login — authentication and contract gates
# ---------------------------------------------------------------------------


def test_login_requires_bearer_device_credential(client: TestClient) -> None:
    response = client.post(LOGIN_PATH, json={}, headers={IDEMPOTENCY_KEY_HEADER: "idem-login-1"})
    assert response.status_code == 401, response.text
    assert response.json()["detail"]["code"] == "DEVICE_CREDENTIAL_REQUIRED"


def test_login_rejects_unknown_device_credential(client: TestClient) -> None:
    response = client.post(
        LOGIN_PATH,
        json={},
        headers={
            **_bearer(secrets.token_urlsafe(32)),
            IDEMPOTENCY_KEY_HEADER: "idem-login-2",
        },
    )
    assert response.status_code == 401, response.text
    assert response.json()["detail"]["code"] == "DEVICE_CREDENTIAL_INVALID"


def test_login_rejects_released_device_credential(client: TestClient) -> None:
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-a", suffix="a")
    # Release the caller's own device (unbind revokes the credential; the
    # status shape requires the unbind timestamp).
    with psycopg.connect(_t19_dsn(), autocommit=True) as conn:
        conn.execute(
            "UPDATE customer_devices SET status = 'UNBOUND', unbound_at = %s WHERE id = %s",
            ("2026-01-01T00:00:00+00:00", customer["device_id"]),
        )
    response = client.post(
        LOGIN_PATH,
        json={},
        headers={
            **_bearer(customer["device_token"]),
            IDEMPOTENCY_KEY_HEADER: "idem-login-3",
        },
    )
    assert response.status_code == 401, response.text
    assert response.json()["detail"]["code"] == "DEVICE_REVOKED"


def test_login_requires_idempotency_key(client: TestClient) -> None:
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-a", suffix="a")
    response = client.post(LOGIN_PATH, json={}, headers=_bearer(customer["device_token"]))
    assert response.status_code == 400, response.text
    assert response.json()["detail"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"


def test_login_is_rate_limited_per_ip(monkeypatch: pytest.MonkeyPatch, route_state: str) -> None:
    from app.activation_code_routes import router as activation_code_router
    from app.customer_session_routes import router as customer_session_router

    app = FastAPI()
    app.include_router(activation_code_router)
    app.include_router(customer_session_router)
    monkeypatch.setenv(DATABASE_URL_ENV, route_state)
    monkeypatch.delenv("VIDEO_REPLICA_CUSTOMER_PRODUCTION", raising=False)
    monkeypatch.setenv(ACTIVATION_CODE_HMAC_KEY_ENV, TEST_KEY)
    monkeypatch.setenv("VIDEO_REPLICA_DEVICE_FINGERPRINT_HMAC_KEY", TEST_FINGERPRINT_KEY_V1)
    monkeypatch.setenv("VIDEO_REPLICA_DEVICE_FINGERPRINT_HMAC_KEY_V2", TEST_FINGERPRINT_KEY_V2)
    monkeypatch.setenv(
        "VIDEO_REPLICA_CUSTOMER_IDEMPOTENCY_AEAD_KEY",
        base64.urlsafe_b64encode(TEST_ENVELOPE_AEAD_KEY).decode("ascii").rstrip("="),
    )
    monkeypatch.setenv("VIDEO_REPLICA_RATE_LIMIT_ACTIVATE_IP", "1000")
    monkeypatch.setenv("VIDEO_REPLICA_RATE_LIMIT_ACTIVATE_CODE", "1000")
    monkeypatch.setenv("VIDEO_REPLICA_RATE_LIMIT_LOGIN_IP", "2")
    monkeypatch.setenv("VIDEO_REPLICA_RATE_LIMIT_WINDOW_SECONDS", "300")

    with TestClient(app) as tight_client:
        customer = _activated_customer(
            tight_client, code=FIRST_CODE, fingerprint="fp-a", suffix="a"
        )
        headers = {
            **_bearer(customer["device_token"]),
            IDEMPOTENCY_KEY_HEADER: "idem-budget",
        }
        # Two requests fit the budget of 2; the third is refused.
        first = tight_client.post(LOGIN_PATH, json={}, headers=headers)
        second = tight_client.post(
            LOGIN_PATH, json={}, headers={**headers, IDEMPOTENCY_KEY_HEADER: "idem-budget-2"}
        )
        third = tight_client.post(
            LOGIN_PATH, json={}, headers={**headers, IDEMPOTENCY_KEY_HEADER: "idem-budget-3"}
        )
        assert first.status_code in (200, 201), first.text
        assert second.status_code in (200, 201), second.text
        assert third.status_code == 429, third.text
        assert third.json()["detail"]["code"] == "RATE_LIMITED"
        assert third.headers.get(RETRY_AFTER_HEADER) is not None


def test_login_idempotent_replay_spends_no_rate_limit_budget(
    monkeypatch: pytest.MonkeyPatch, route_state: str
) -> None:
    """CodeReview P2-2: a fully validated replay short-circuits *before* the
    limiter — network retries may never lock a legal user out of their own
    cached response (the activation-route T15 review rule)."""
    from app.activation_code_routes import router as activation_code_router
    from app.customer_session_routes import router as customer_session_router

    app = FastAPI()
    app.include_router(activation_code_router)
    app.include_router(customer_session_router)
    monkeypatch.setenv(DATABASE_URL_ENV, route_state)
    monkeypatch.delenv("VIDEO_REPLICA_CUSTOMER_PRODUCTION", raising=False)
    monkeypatch.setenv(ACTIVATION_CODE_HMAC_KEY_ENV, TEST_KEY)
    monkeypatch.setenv("VIDEO_REPLICA_DEVICE_FINGERPRINT_HMAC_KEY", TEST_FINGERPRINT_KEY_V1)
    monkeypatch.setenv("VIDEO_REPLICA_DEVICE_FINGERPRINT_HMAC_KEY_V2", TEST_FINGERPRINT_KEY_V2)
    monkeypatch.setenv(
        "VIDEO_REPLICA_CUSTOMER_IDEMPOTENCY_AEAD_KEY",
        base64.urlsafe_b64encode(TEST_ENVELOPE_AEAD_KEY).decode("ascii").rstrip("="),
    )
    monkeypatch.setenv("VIDEO_REPLICA_RATE_LIMIT_ACTIVATE_IP", "1000")
    monkeypatch.setenv("VIDEO_REPLICA_RATE_LIMIT_ACTIVATE_CODE", "1000")
    monkeypatch.setenv("VIDEO_REPLICA_RATE_LIMIT_LOGIN_IP", "2")
    monkeypatch.setenv("VIDEO_REPLICA_RATE_LIMIT_WINDOW_SECONDS", "300")

    with TestClient(app) as tight_client:
        customer = _activated_customer(
            tight_client, code=FIRST_CODE, fingerprint="fp-a", suffix="a"
        )
        headers = {
            **_bearer(customer["device_token"]),
            IDEMPOTENCY_KEY_HEADER: "idem-replay-budget",
        }
        # Spend the whole budget of 2 (fresh keys — nothing to replay yet).
        first = tight_client.post(LOGIN_PATH, json={}, headers=headers)
        second = tight_client.post(
            LOGIN_PATH,
            json={},
            headers={**headers, IDEMPOTENCY_KEY_HEADER: "idem-replay-budget-2"},
        )
        assert first.status_code in (200, 201), first.text
        assert second.status_code in (200, 201), second.text

        # The budget is spent; a fresh key is now refused.
        blocked = tight_client.post(
            LOGIN_PATH,
            json={},
            headers={**headers, IDEMPOTENCY_KEY_HEADER: "idem-replay-budget-3"},
        )
        assert blocked.status_code == 429, blocked.text

        # …but the retry of the first key replays its sealed response — the
        # probe precedes the limiter, so the replay spends no budget.
        replay = tight_client.post(LOGIN_PATH, json={}, headers=headers)
        assert replay.status_code == first.status_code, replay.text
        assert replay.json() == first.json()
        assert replay.headers.get(REPLAY_HEADER) == "true"


# ---------------------------------------------------------------------------
# POST /api/customer/sessions/login — the §12.3 state machine
# ---------------------------------------------------------------------------


def test_login_same_device_with_valid_session_token_renews_only(client: TestClient) -> None:
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-a", suffix="a")
    device_id, old_session_id, old_epoch, old_lease, _ = _session_row()

    response = client.post(
        LOGIN_PATH,
        json={"session_token": customer["session_token"]},
        headers={
            **_bearer(customer["device_token"]),
            IDEMPOTENCY_KEY_HEADER: "idem-renew-1",
        },
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    # Renewal: same token, same epoch, same session id — lease pushed out.
    assert payload["session_token"] == customer["session_token"]
    assert payload["session_epoch"] == old_epoch == 1
    assert payload["session_id"] == old_session_id
    assert payload["device_id"] == device_id
    assert payload["session_lease_expires_at"] >= old_lease

    row_device, _, row_epoch, _, _ = _session_row()
    assert row_device == device_id and row_epoch == 1
    events = _session_events()
    assert [e[0] for e in events] == ["ACTIVATED", "LOGIN"]


def test_login_same_device_without_session_token_recovers_with_epoch_bump(
    client: TestClient,
) -> None:
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-a", suffix="a")

    response = client.post(
        LOGIN_PATH,
        json={},
        headers={
            **_bearer(customer["device_token"]),
            IDEMPOTENCY_KEY_HEADER: "idem-recover-1",
        },
    )
    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["session_epoch"] == 2
    assert payload["session_token"] != customer["session_token"]

    # The replaced token can never heartbeat again (§3.4: old token fails).
    stale = client.post(HEARTBEAT_PATH, headers=_bearer(customer["session_token"]))
    assert stale.status_code == 401, stale.text
    assert stale.json()["detail"]["code"] == "SESSION_REPLACED"


def test_login_other_device_online_answers_409_with_masked_hint(client: TestClient) -> None:
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-a", suffix="a")
    second_token = _second_device_row(
        user_id=customer["user_id"],
        activation_code_id="code-a",
        device_id="device-b",
        slot_no=2,
        display_name="Office MacBook Pro",
    )

    response = client.post(
        LOGIN_PATH,
        json={},
        headers={
            **_bearer(second_token),
            IDEMPOTENCY_KEY_HEADER: "idem-conflict-1",
        },
    )
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "OTHER_DEVICE_ONLINE"
    # The masked hint must not leak the full device name (§13.2).
    assert "Office" not in response.text
    assert "MacBook" not in response.text
    assert detail.get("online_device_name_masked")
    assert detail.get("lease_expires_at")

    # The current session still belongs to the first device, untouched.
    row_device, _, row_epoch, _, _ = _session_row()
    assert row_device == customer["device_id"]
    assert row_epoch == 1
    # No LOGIN event was recorded for the refused device.
    assert [e[0] for e in _session_events()] == ["ACTIVATED"]


def test_login_after_lease_expiry_takes_over_and_records_timeout(client: TestClient) -> None:
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-a", suffix="a")
    second_token = _second_device_row(
        user_id=customer["user_id"],
        activation_code_id="code-a",
        device_id="device-b",
        slot_no=2,
    )
    _expire_lease(customer["device_id"])

    response = client.post(
        LOGIN_PATH,
        json={},
        headers={
            **_bearer(second_token),
            IDEMPOTENCY_KEY_HEADER: "idem-takeover-1",
        },
    )
    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["device_id"] == "device-b"
    assert payload["session_epoch"] == 2

    # The takeover appends the system TIMEOUT event (no acting user, bound to
    # the lapsed epoch-1 session) and the LOGIN event (epoch 2); the stale
    # first-device token stays dead. Both new events share one transaction
    # timestamp, so the assertion keys on the event set and epoch binding
    # instead of insertion order.
    events = _session_events()
    assert sorted(e[0] for e in events) == ["ACTIVATED", "LOGIN", "TIMEOUT"]
    timeout_rows = [e for e in events if e[0] == "TIMEOUT"]
    assert len(timeout_rows) == 1 and timeout_rows[0][1] == 1
    login_rows = [e for e in events if e[0] == "LOGIN"]
    assert len(login_rows) == 1 and login_rows[0][1] == 2
    with psycopg.connect(_t19_dsn(), autocommit=True) as conn:
        timeout_actor = conn.execute(
            "SELECT actor_user_id FROM customer_session_events WHERE event = 'TIMEOUT'"
        ).fetchone()
    assert timeout_actor is not None and timeout_actor[0] is None

    stale = client.post(HEARTBEAT_PATH, headers=_bearer(customer["session_token"]))
    assert stale.status_code == 401, stale.text
    assert stale.json()["detail"]["code"] == "SESSION_REPLACED"


def test_login_missing_session_row_establishes_epoch_one(client: TestClient) -> None:
    """Defensive branch: the activation row was lost (never happens in the
    happy chain) — login still establishes a sound epoch-1 session."""
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-a", suffix="a")
    with psycopg.connect(_t19_dsn(), autocommit=True) as conn:
        conn.execute("SET session_replication_role = replica")
        conn.execute("DELETE FROM customer_session_state")
        conn.execute("SET session_replication_role = DEFAULT")

    response = client.post(
        LOGIN_PATH,
        json={},
        headers={
            **_bearer(customer["device_token"]),
            IDEMPOTENCY_KEY_HEADER: "idem-orphan-1",
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["session_epoch"] == 1


# ---------------------------------------------------------------------------
# POST /api/customer/sessions/login — idempotency envelope
# ---------------------------------------------------------------------------


def test_login_lost_response_replays_same_token_and_epoch(client: TestClient) -> None:
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-a", suffix="a")
    headers = {
        **_bearer(customer["device_token"]),
        IDEMPOTENCY_KEY_HEADER: "idem-replay-1",
    }
    first = client.post(LOGIN_PATH, json={}, headers=headers)
    assert first.status_code == 201, first.text

    # The client lost the 201 and retries with the same key + same body.
    replay = client.post(LOGIN_PATH, json={}, headers=headers)
    assert replay.status_code == 201, replay.text
    assert replay.headers.get(REPLAY_HEADER) == "true"
    assert replay.json() == first.json()

    # Exactly one LOGIN event — the replay added nothing (acceptance §3.4).
    assert [e[0] for e in _session_events()] == ["ACTIVATED", "LOGIN"]


def test_replay_recovery_window_uses_the_postgresql_clock(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PR #51 review P2: the envelope recovery window is judged on the
    PostgreSQL clock, never on the application process clock — an API node
    whose clock runs ahead of PostgreSQL must not reject a still-valid
    lost-response replay (nor may a lagging node accept an expired one).
    The deadline was minted from ``SELECT now()`` and the verdict compares
    against the same server-side clock sampled in the envelope-read
    transaction (SES-01; the activation-route ``_server_now`` precedent)."""
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-a", suffix="a")
    headers = {
        **_bearer(customer["device_token"]),
        IDEMPOTENCY_KEY_HEADER: "idem-clock-skew-1",
    }
    first = client.post(LOGIN_PATH, json={}, headers=headers)
    assert first.status_code == 201, first.text

    class _SkewedDatetime(datetime):
        """An API node whose process clock is a decade ahead of PostgreSQL."""

        @classmethod
        def now(cls, tz: object = None) -> datetime:
            return datetime.now(UTC) + timedelta(days=3650)

    monkeypatch.setattr("app.customer_session_routes.datetime", _SkewedDatetime)
    replay = client.post(LOGIN_PATH, json={}, headers=headers)
    assert replay.status_code == 201, replay.text
    assert replay.headers.get(REPLAY_HEADER) == "true"
    assert replay.json() == first.json()


def test_login_same_key_different_body_answers_idempotency_conflict(client: TestClient) -> None:
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-a", suffix="a")
    headers = {
        **_bearer(customer["device_token"]),
        IDEMPOTENCY_KEY_HEADER: "idem-conflicting-1",
    }
    first = client.post(LOGIN_PATH, json={}, headers=headers)
    assert first.status_code == 201, first.text

    conflict = client.post(
        LOGIN_PATH,
        json={"session_token": "a-different-body-token"},
        headers=headers,
    )
    assert conflict.status_code == 409, conflict.text
    assert conflict.json()["detail"]["code"] == "IDEMPOTENCY_CONFLICT"


def test_login_conflict_does_not_spend_the_idempotency_key(client: TestClient) -> None:
    """A refused login (409 OTHER_DEVICE_ONLINE) rolls the envelope back with
    the transaction — the key stays reusable once the lease actually lapses."""
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-a", suffix="a")
    second_token = _second_device_row(
        user_id=customer["user_id"],
        activation_code_id="code-a",
        device_id="device-b",
        slot_no=2,
    )
    headers = {
        **_bearer(second_token),
        IDEMPOTENCY_KEY_HEADER: "idem-reusable-1",
    }
    refused = client.post(LOGIN_PATH, json={}, headers=headers)
    assert refused.status_code == 409, refused.text

    _expire_lease(customer["device_id"])
    accepted = client.post(LOGIN_PATH, json={}, headers=headers)
    assert accepted.status_code == 201, accepted.text


# ---------------------------------------------------------------------------
# POST /api/customer/sessions/heartbeat
# ---------------------------------------------------------------------------


def test_heartbeat_requires_bearer_session_token(client: TestClient) -> None:
    response = client.post(HEARTBEAT_PATH)
    assert response.status_code == 401, response.text
    assert response.json()["detail"]["code"] == "SESSION_TOKEN_REQUIRED"


def test_heartbeat_renews_the_lease_without_touching_epoch(client: TestClient) -> None:
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-a", suffix="a")
    _, old_session_id, _, old_lease, _ = _session_row()

    response = client.post(HEARTBEAT_PATH, headers=_bearer(customer["session_token"]))
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["session_epoch"] == 1
    assert payload["session_id"] == old_session_id
    assert payload["lease_expires_at"] >= old_lease

    _, _, row_epoch, _, _ = _session_row()
    assert row_epoch == 1
    assert [e[0] for e in _session_events()] == ["ACTIVATED", "HEARTBEAT"]


def test_heartbeat_rejects_forged_session_token(client: TestClient) -> None:
    _activated_customer(client, code=FIRST_CODE, fingerprint="fp-a", suffix="a")
    response = client.post(HEARTBEAT_PATH, headers=_bearer(secrets.token_urlsafe(32)))
    assert response.status_code == 401, response.text
    assert response.json()["detail"]["code"] == "SESSION_REPLACED"


def test_heartbeat_on_lapsed_lease_never_resurrects_the_session(client: TestClient) -> None:
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-a", suffix="a")
    _expire_lease(customer["device_id"])

    response = client.post(HEARTBEAT_PATH, headers=_bearer(customer["session_token"]))
    assert response.status_code == 401, response.text
    assert response.json()["detail"]["code"] == "SESSION_EXPIRED"

    # And the refused heartbeat wrote no HEARTBEAT event.
    assert [e[0] for e in _session_events()] == ["ACTIVATED"]


def test_heartbeat_after_logout_fails_with_session_expired(client: TestClient) -> None:
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-a", suffix="a")
    out = client.post(
        LOGOUT_PATH,
        headers={
            **_bearer(customer["session_token"]),
            IDEMPOTENCY_KEY_HEADER: "idem-logout-hb",
        },
    )
    assert out.status_code == 204, out.text

    stale = client.post(HEARTBEAT_PATH, headers=_bearer(customer["session_token"]))
    assert stale.status_code == 401, stale.text
    assert stale.json()["detail"]["code"] == "SESSION_EXPIRED"


# ---------------------------------------------------------------------------
# POST /api/customer/sessions/logout
# ---------------------------------------------------------------------------


def test_logout_requires_idempotency_key(client: TestClient) -> None:
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-a", suffix="a")
    response = client.post(LOGOUT_PATH, headers=_bearer(customer["session_token"]))
    assert response.status_code == 400, response.text
    assert response.json()["detail"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"


def test_logout_releases_the_lease_and_records_the_event(client: TestClient) -> None:
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-a", suffix="a")
    response = client.post(
        LOGOUT_PATH,
        headers={
            **_bearer(customer["session_token"]),
            IDEMPOTENCY_KEY_HEADER: "idem-logout-1",
        },
    )
    assert response.status_code == 204, response.text

    # The lease is in the past and the LOGOUT event is on the audit trail.
    _, _, row_epoch, row_lease, _ = _session_row()
    assert row_epoch == 1
    assert datetime.fromisoformat(row_lease) <= datetime.now(UTC) + timedelta(seconds=1)
    events = [e[0] for e in _session_events()]
    assert events == ["ACTIVATED", "LOGOUT"]


def test_logout_lets_the_other_device_log_in_immediately(client: TestClient) -> None:
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-a", suffix="a")
    second_token = _second_device_row(
        user_id=customer["user_id"],
        activation_code_id="code-a",
        device_id="device-b",
        slot_no=2,
    )
    out = client.post(
        LOGOUT_PATH,
        headers={
            **_bearer(customer["session_token"]),
            IDEMPOTENCY_KEY_HEADER: "idem-logout-2",
        },
    )
    assert out.status_code == 204, out.text

    login = client.post(
        LOGIN_PATH,
        json={},
        headers={
            **_bearer(second_token),
            IDEMPOTENCY_KEY_HEADER: "idem-after-logout",
        },
    )
    assert login.status_code == 201, login.text
    assert login.json()["device_id"] == "device-b"


def test_logout_lost_response_replays_the_204(client: TestClient) -> None:
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-a", suffix="a")
    headers = {
        **_bearer(customer["session_token"]),
        IDEMPOTENCY_KEY_HEADER: "idem-logout-replay",
    }
    first = client.post(LOGOUT_PATH, headers=headers)
    assert first.status_code == 204, first.text

    # The client lost the 204; the same key replays it even though the
    # session token is now expired (the unbind-envelope precedent).
    replay = client.post(LOGOUT_PATH, headers=headers)
    assert replay.status_code == 204, replay.text
    assert replay.headers.get(REPLAY_HEADER) == "true"

    # No second LOGOUT event.
    assert [e[0] for e in _session_events()] == ["ACTIVATED", "LOGOUT"]


def test_late_logout_after_takeover_never_touches_the_new_session(client: TestClient) -> None:
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-a", suffix="a")
    second_token = _second_device_row(
        user_id=customer["user_id"],
        activation_code_id="code-a",
        device_id="device-b",
        slot_no=2,
    )
    _expire_lease(customer["device_id"])
    takeover = client.post(
        LOGIN_PATH,
        json={},
        headers={
            **_bearer(second_token),
            IDEMPOTENCY_KEY_HEADER: "idem-takeover-late",
        },
    )
    assert takeover.status_code == 201, takeover.text
    new_lease = _session_row()[3]

    # The first device's late logout must not clear the second device's lease.
    late = client.post(
        LOGOUT_PATH,
        headers={
            **_bearer(customer["session_token"]),
            IDEMPOTENCY_KEY_HEADER: "idem-late-logout",
        },
    )
    assert late.status_code == 401, late.text
    assert late.json()["detail"]["code"] == "SESSION_REPLACED"
    assert _session_row()[3] == new_lease
    assert _session_row()[0] == "device-b"


def test_logout_on_lapsed_lease_answers_session_expired(client: TestClient) -> None:
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-a", suffix="a")
    _expire_lease(customer["device_id"])
    response = client.post(
        LOGOUT_PATH,
        headers={
            **_bearer(customer["session_token"]),
            IDEMPOTENCY_KEY_HEADER: "idem-logout-late",
        },
    )
    assert response.status_code == 401, response.text
    assert response.json()["detail"]["code"] == "SESSION_EXPIRED"


def test_heartbeat_without_device_keys_fails_closed_503(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CodeReview P2-1: a missing device-domain key is a server-side outage —
    503 fail-closed, never a 500 and never a client-credential 401 (which
    would make the client wipe perfectly valid credentials)."""
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-a", suffix="a")
    monkeypatch.delenv("VIDEO_REPLICA_DEVICE_FINGERPRINT_HMAC_KEY", raising=False)
    monkeypatch.delenv("VIDEO_REPLICA_DEVICE_FINGERPRINT_HMAC_KEY_V2", raising=False)
    response = client.post(HEARTBEAT_PATH, headers=_bearer(customer["session_token"]))
    assert response.status_code == 503, response.text
    assert response.json()["detail"]["code"] == "SESSION_SERVICE_UNAVAILABLE"


def test_user_driven_events_record_the_acting_user(client: TestClient) -> None:
    """CodeReview P3-1: LOGIN/HEARTBEAT/LOGOUT are user-driven audit events —
    their actor_user_id names the user, so the trail distinguishes them from
    system events (only TIMEOUT runs actor-less, 029's comment)."""
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-a", suffix="a")
    client.post(
        LOGIN_PATH,
        json={"session_token": customer["session_token"]},
        headers={
            **_bearer(customer["device_token"]),
            IDEMPOTENCY_KEY_HEADER: "idem-actor-1",
        },
    )
    client.post(HEARTBEAT_PATH, headers=_bearer(customer["session_token"]))
    client.post(
        LOGOUT_PATH,
        headers={
            **_bearer(customer["session_token"]),
            IDEMPOTENCY_KEY_HEADER: "idem-actor-2",
        },
    )
    with psycopg.connect(_t19_dsn(), autocommit=True) as conn:
        rows = conn.execute(
            "SELECT event, actor_user_id FROM customer_session_events ORDER BY created_at, id"
        ).fetchall()
    user_driven = {str(event): actor for event, actor in rows if str(event) != "ACTIVATED"}
    assert set(user_driven) == {"LOGIN", "HEARTBEAT", "LOGOUT"}
    for event, actor in user_driven.items():
        assert actor == customer["user_id"], (event, actor)


# ---------------------------------------------------------------------------
# Concurrency — acceptance §3.4: one current device from a lapsed state
# ---------------------------------------------------------------------------


def test_concurrent_logins_from_lapsed_state_leave_one_current_device(
    client: TestClient,
) -> None:
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-a", suffix="a")
    second_token = _second_device_row(
        user_id=customer["user_id"],
        activation_code_id="code-a",
        device_id="device-b",
        slot_no=2,
    )
    _expire_lease(customer["device_id"])

    outcomes: dict[str, int | None] = {"first": None, "second": None}
    # CodeReview P3-3: align both request starts on a barrier — the row lock
    # must serialize a genuinely overlapping race, not a lucky thread order.
    barrier = threading.Barrier(2)

    def _login(tag: str, token: str) -> None:
        with TestClient(client.app) as concurrent:
            barrier.wait()
            response = concurrent.post(
                LOGIN_PATH,
                json={},
                headers={
                    **_bearer(token),
                    IDEMPOTENCY_KEY_HEADER: f"idem-race-{tag}",
                },
            )
            outcomes[tag] = response.status_code

    threads = [
        threading.Thread(target=_login, args=("first", customer["device_token"])),
        threading.Thread(target=_login, args=("second", second_token)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    # Exactly one writer won the row lock and took over; the other either saw
    # the fresh lease (409) or lost cleanly. The database holds ONE current
    # device either way.
    statuses = sorted(v for v in outcomes.values() if v is not None)
    assert 201 in statuses, outcomes
    assert all(status in (201, 409) for status in statuses), outcomes

    row_device, _, _, row_lease, _ = _session_row()
    assert row_device in (customer["device_id"], "device-b")
    assert datetime.fromisoformat(row_lease) > datetime.now(UTC)

    # And the epoch advanced exactly once (single takeover).
    with psycopg.connect(_t19_dsn(), autocommit=True) as conn:
        epoch = conn.execute("SELECT session_epoch FROM customer_session_state").fetchone()
    assert int(epoch[0]) == 2


def test_concurrent_first_logins_on_a_missing_row_never_500(client: TestClient) -> None:
    """CodeReview P3-4: two first-writers racing the defensive insert (the
    session row is missing) settle on the winner's row — the loser re-drives
    the state machine instead of dying on UniqueViolation."""
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-a", suffix="a")
    # No production path deletes session rows; simulate the lost row directly
    # (customer_session_state has no append-only trigger — only events do).
    with psycopg.connect(_t19_dsn(), autocommit=True) as conn:
        conn.execute("DELETE FROM customer_session_state")

    outcomes: dict[str, int | None] = {"first": None, "second": None}
    barrier = threading.Barrier(2)

    def _login(tag: str) -> None:
        with TestClient(client.app) as concurrent:
            barrier.wait()
            response = concurrent.post(
                LOGIN_PATH,
                json={},
                headers={
                    **_bearer(customer["device_token"]),
                    IDEMPOTENCY_KEY_HEADER: f"idem-lost-row-{tag}",
                },
            )
            outcomes[tag] = response.status_code

    threads = [
        threading.Thread(target=_login, args=("first",)),
        threading.Thread(target=_login, args=("second",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    # Both requests answered — no UniqueViolation 500 — and each established
    # (then recovered) against the single row: the epoch advanced exactly
    # twice with two LOGIN events.
    assert outcomes["first"] == 201, outcomes
    assert outcomes["second"] == 201, outcomes
    _, _, row_epoch, _, _ = _session_row()
    assert row_epoch == 2
    assert [e[0] for e in _session_events()].count("LOGIN") == 2


# ---------------------------------------------------------------------------
# No-Go red lines — no credential material in events or envelopes
# ---------------------------------------------------------------------------


def test_no_plaintext_credentials_in_session_events(client: TestClient) -> None:
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-a", suffix="a")
    client.post(
        LOGIN_PATH,
        json={},
        headers={
            **_bearer(customer["device_token"]),
            IDEMPOTENCY_KEY_HEADER: "idem-redline-1",
        },
    )
    client.post(HEARTBEAT_PATH, headers=_bearer(customer["session_token"]))

    with psycopg.connect(_t19_dsn(), autocommit=True) as conn:
        rows = conn.execute(
            "SELECT event, reason, request_id FROM customer_session_events"
        ).fetchall()
    for row in rows:
        blob = " ".join(str(value) for value in row if value is not None)
        assert customer["session_token"] not in blob
        assert customer["device_token"] not in blob
