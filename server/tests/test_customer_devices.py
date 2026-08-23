"""T16 / DEV-01 — two current device slots, credentials and unbind history.

Fail-first tests for the frozen files ``server/app/customer_device_service.py``
and ``server/app/customer_device_routes.py`` (code checklist §3.2 / §3.3):
the device-slot half of the customer runtime. Revision 028 already proved the
slot invariants in the database (partial unique indexes); this task delivers
the application layer on top — device credential authentication, the
two-slot status view, unbinding with its preserved audit history, and the
third-device block that the second-device enroll flow (T17) will consult.

Contract under test (task list §3 T16; dev doc §3.2 / §6.1 / §13.2):

- the device credential is the long-lived secret returned once at bind time;
  it authenticates device-management requests via ``Authorization: Bearer``
  and only its keyed digest ever reaches the database;
- ``GET /api/customer/devices`` answers the two-slot status: slot 1 and slot
  2 each hold at most one currently ``BOUND`` device, plus the unbind history
  that outlives slot reuse (rows are never deleted — dev doc §3.2);
- ``DELETE /api/customer/devices/{id}`` unbinds one of the caller's own
  devices: the row flips to ``UNBOUND``, the slot becomes reusable, and any
  live session riding that device is revoked atomically (epoch bump + past
  lease + ``LOGOUT`` event);
- with both slots ``BOUND`` there is no free slot (``next_free_slot`` is
  ``None``) and PostgreSQL itself refuses a third ``BOUND`` row — the
  third-device block;
- stable error codes: 401 ``DEVICE_CREDENTIAL_REQUIRED`` /
  ``DEVICE_CREDENTIAL_INVALID`` / ``DEVICE_REVOKED``, 404 ``DEVICE_NOT_FOUND``
  (missing or foreign device — one answer, no IDOR oracle), 409
  ``DEVICE_ALREADY_UNBOUND``;
- the DELETE carries a mandatory ``Idempotency-Key`` (PR #47 Codex review
  P2): a client that lost the 204 retries with the same key + same target
  and replays the sealed 204 — even though its own credential is by then no
  longer resolvable — while the same key against a different target answers
  409 ``IDEMPOTENCY_CONFLICT`` (400 ``IDEMPOTENCY_KEY_REQUIRED`` otherwise).
"""

from __future__ import annotations

import base64
import os
import secrets
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from psycopg.errors import CheckViolation, UniqueViolation

from app.activation_code_service import (
    ACTIVATION_CODE_HMAC_KEY_ENV,
    compute_code_digest,
)
from app.db_pg import DATABASE_URL_ENV, close_pg_pool

DEFAULT_DSN = "postgresql://testuser:testpass@localhost:5433/customer_v3_test"
SKIP_REASON = "PostgreSQL fixture not reachable; start it via scripts/pg-fixture.sh start"

T16_DB_NAME = "t16_customer_devices_test"

TEST_KEY = secrets.token_urlsafe(48)  # code HMAC key (v1), never a real secret
TEST_FINGERPRINT_KEY_V1 = secrets.token_urlsafe(48)
TEST_FINGERPRINT_KEY_V2 = secrets.token_urlsafe(48)
TEST_ENVELOPE_AEAD_KEY = secrets.token_bytes(32)

ACTIVATE_PATH = "/api/customer/activate"
DEVICES_PATH = "/api/customer/devices"
IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"
REQUEST_ID_HEADER = "X-Request-Id"
REPLAY_HEADER = "X-Idempotent-Replay"
AUTHORIZATION_HEADER = "Authorization"
FUTURE_EXPIRY = "2099-01-01T00:00:00+00:00"

# Canonical Crockford-shaped codes (prefix + 4 groups x 7 characters).
FIRST_CODE = "XS04-AAAAAAA-BBBBBBB-CCCCCCC-DDDDDDD"
SECOND_CODE = "XS04-CCCCCCC-DDDDDDD-EEEEEEE-FFFFFFF"
# Dedicated to the service-level tests that skip the route_state fixture
# (and therefore run against whatever the routed tests last seeded).
SLOT_PROBE_CODE = "XS04-EEEEEEE-FFFFFFF-AAAAAAA-BBBBBBB"
THIRD_DEVICE_CODE = "XS04-FFFFFFF-AAAAAAA-BBBBBBB-CCCCCCC"

COUNTERS_TABLE = "security_rate_limit_counters"
FAILURES_TABLE = "security_auth_failures"


def _pg_dsn() -> str:
    return os.environ.get("TEST_POSTGRESQL_URL", DEFAULT_DSN)


def _admin_dsn() -> str:
    return _pg_dsn().rsplit("/", 1)[0] + "/postgres"


def _t16_dsn() -> str:
    return _pg_dsn().rsplit("/", 1)[0] + f"/{T16_DB_NAME}"


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


def test_keyed_digest_is_deterministic_and_key_sensitive() -> None:
    from app.customer_device_service import keyed_digest

    first = keyed_digest(b"a" * 32, "token-one")
    second = keyed_digest(b"a" * 32, "token-one")
    other_key = keyed_digest(b"b" * 32, "token-one")
    other_value = keyed_digest(b"a" * 32, "token-two")
    assert first == second, "same key and token must produce the same digest"
    assert first != other_key, "a different key version must change the digest"
    assert first != other_value, "a different token must change the digest"


def test_max_slots_constant_is_two() -> None:
    from app.customer_device_service import MAX_DEVICE_SLOTS

    assert MAX_DEVICE_SLOTS == 2


# ---------------------------------------------------------------------------
# PostgreSQL integration (dedicated migrated fixture database)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def devices_dsn() -> Iterator[str]:
    from alembic import command
    from alembic.config import Config

    if not _pg_available(_pg_dsn()):
        pytest.skip(SKIP_REASON)
    with psycopg.connect(_admin_dsn(), autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{T16_DB_NAME}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{T16_DB_NAME}"')
    server_dir = Path(__file__).resolve().parent.parent
    config = Config(str(server_dir / "alembic.ini"))
    config.set_main_option("script_location", str(server_dir / "migrations"))
    config.set_main_option(
        "sqlalchemy.url", _t16_dsn().replace("postgresql://", "postgresql+psycopg://")
    )
    command.upgrade(config, "head")
    try:
        yield _t16_dsn()
    finally:
        with psycopg.connect(_admin_dsn(), autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{T16_DB_NAME}" WITH (FORCE)')


@pytest.fixture()
def route_state(devices_dsn: str) -> Iterator[str]:
    close_pg_pool()
    with psycopg.connect(_t16_dsn(), autocommit=True) as conn:
        # 036 refuses TRUNCATE of the append-only audit tables; the replica
        # role suspends triggers for this cleanup sweep only.
        conn.execute("SET session_replication_role = replica")
        conn.execute(
            "TRUNCATE customer_session_events, customer_session_state, "
            "customer_idempotency_envelopes, "
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
    yield devices_dsn
    close_pg_pool()


@pytest.fixture()
def customer_app(monkeypatch: pytest.MonkeyPatch, route_state: str) -> Iterator[FastAPI]:
    from app.activation_code_routes import router as activation_code_router
    from app.customer_device_routes import router as customer_device_router

    app = FastAPI()
    app.include_router(activation_code_router)
    app.include_router(customer_device_router)
    monkeypatch.setenv(DATABASE_URL_ENV, route_state)
    monkeypatch.delenv("VIDEO_REPLICA_CUSTOMER_PRODUCTION", raising=False)
    monkeypatch.setenv(ACTIVATION_CODE_HMAC_KEY_ENV, TEST_KEY)
    monkeypatch.setenv("VIDEO_REPLICA_DEVICE_FINGERPRINT_HMAC_KEY", TEST_FINGERPRINT_KEY_V1)
    monkeypatch.setenv("VIDEO_REPLICA_DEVICE_FINGERPRINT_HMAC_KEY_V2", TEST_FINGERPRINT_KEY_V2)
    monkeypatch.setenv(
        "VIDEO_REPLICA_CUSTOMER_IDEMPOTENCY_AEAD_KEY",
        base64.urlsafe_b64encode(TEST_ENVELOPE_AEAD_KEY).decode("ascii").rstrip("="),
    )
    # Roomy budgets: these tests exercise the device routes, not the limiter.
    monkeypatch.setenv("VIDEO_REPLICA_RATE_LIMIT_ACTIVATE_IP", "1000")
    monkeypatch.setenv("VIDEO_REPLICA_RATE_LIMIT_ACTIVATE_CODE", "1000")
    monkeypatch.setenv("VIDEO_REPLICA_RATE_LIMIT_WINDOW_SECONDS", "300")
    monkeypatch.setattr("app.activation_code_routes.apply_anti_enumeration_delay", lambda: None)
    yield app


@pytest.fixture()
def client(customer_app: FastAPI) -> Iterator[TestClient]:
    with TestClient(customer_app) as test_client:
        yield test_client


def _seed_issuable_code(code: str, *, code_id: str, batch_id: str) -> None:
    """Insert one OPEN batch + ISSUED code pair (T12 shapes)."""
    with psycopg.connect(_t16_dsn(), autocommit=True) as conn:
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


def _bearer(token: str) -> dict[str, str]:
    return {AUTHORIZATION_HEADER: f"Bearer {token}"}


def _second_device_row(
    *,
    user_id: str,
    activation_code_id: str,
    device_id: str,
    slot_no: int,
) -> str:
    """Insert a second BOUND device directly (T17 enroll is out of scope).

    The credential digest is computed with the highest configured device
    key version, exactly like the activation transaction does. Returns the
    plaintext token the caller then presents.
    """
    from app.customer_device_service import highest_device_domain_key, keyed_digest

    token = secrets.token_urlsafe(32)
    version, key = highest_device_domain_key()
    digest = keyed_digest(key, token)
    with psycopg.connect(_t16_dsn(), autocommit=True) as conn:
        conn.execute(
            "INSERT INTO customer_devices "
            "(id, activation_code_id, user_id, slot_no, display_name, platform, "
            " fingerprint_hmac, fingerprint_key_version, token_digest, token_key_version) "
            "VALUES (%s, %s, %s, %s, 'Second Device', 'macos', %s, %s, %s, %s)",
            (
                device_id,
                activation_code_id,
                user_id,
                slot_no,
                keyed_digest(key, f"fp-second-{device_id}"),
                version,
                digest,
                version,
            ),
        )
    return token


# ---------------------------------------------------------------------------
# GET /api/customer/devices — the two-slot status view
# ---------------------------------------------------------------------------


def test_list_devices_requires_bearer_token(client: TestClient) -> None:
    response = client.get(DEVICES_PATH)
    assert response.status_code == 401, response.text
    assert response.json()["detail"]["code"] == "DEVICE_CREDENTIAL_REQUIRED"


def test_list_devices_rejects_malformed_authorization(client: TestClient) -> None:
    for header in ("Token abc", "Bearer", "Bearer   ", "Basic Zm9vOmJhcg=="):
        response = client.get(DEVICES_PATH, headers={AUTHORIZATION_HEADER: header})
        assert response.status_code == 401, header
        assert response.json()["detail"]["code"] == "DEVICE_CREDENTIAL_REQUIRED"


def test_list_devices_rejects_unknown_token(client: TestClient) -> None:
    response = client.get(DEVICES_PATH, headers=_bearer(secrets.token_urlsafe(32)))
    assert response.status_code == 401, response.text
    assert response.json()["detail"]["code"] == "DEVICE_CREDENTIAL_INVALID"


def test_list_devices_shows_slot_one_bound_slot_two_free(client: TestClient) -> None:
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-dev-1", suffix="d1")
    response = client.get(DEVICES_PATH, headers=_bearer(customer["device_token"]))
    assert response.status_code == 200, response.text
    body = response.json()

    assert [slot["slot_no"] for slot in body["slots"]] == [1, 2]
    slot_one = body["slots"][0]
    slot_two = body["slots"][1]
    assert slot_one["device"] is not None
    assert slot_one["device"]["id"] == customer["device_id"]
    assert slot_one["device"]["status"] == "BOUND"
    assert slot_one["device"]["is_current"] is True
    assert slot_one["device"]["bound_at"]
    assert slot_two["device"] is None
    assert body["history"] == []


# ---------------------------------------------------------------------------
# DELETE /api/customer/devices/{id} — unbind, history and slot reuse
# ---------------------------------------------------------------------------


def test_unbind_releases_slot_keeps_history_and_allows_reuse(
    client: TestClient,
) -> None:
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-dev-2", suffix="d2")
    device_id = customer["device_id"]
    token = customer["device_token"]

    response = client.delete(
        f"{DEVICES_PATH}/{device_id}",
        headers={**_bearer(token), IDEMPOTENCY_KEY_HEADER: "idem-unbind-d2"},
    )
    assert response.status_code == 204, response.text

    # The history row survives: audit is append-only, never deleted.
    with psycopg.connect(_t16_dsn(), autocommit=True) as conn:
        row = conn.execute(
            "SELECT status, unbound_at FROM customer_devices WHERE id = %s",
            (device_id,),
        ).fetchone()
    assert row is not None, "the unbound history row must not be deleted"
    assert row[0] == "UNBOUND"
    assert row[1] is not None

    # The slot view reports slot 1 free and the row in history.
    listing = client.get(DEVICES_PATH, headers=_bearer(token))
    assert listing.status_code == 401
    assert listing.json()["detail"]["code"] == "DEVICE_REVOKED"
    with psycopg.connect(_t16_dsn(), autocommit=True) as conn:
        slots, history = _raw_slots_and_history(conn, user_id=customer["user_id"])
    assert slots == {1: None, 2: None}
    assert [device["id"] for device in history] == [device_id]

    # Slot reuse: a fresh BOUND row on slot 1 must be accepted (the partial
    # unique index only guards *current* occupancy — DEV-01 No-Go).
    _second_device_row(
        user_id=customer["user_id"],
        activation_code_id=_code_id_of_user(customer["user_id"]),
        device_id=str(uuid.uuid4()),
        slot_no=1,
    )


def test_unbind_revokes_session_atomically(client: TestClient) -> None:
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-dev-3", suffix="d3")
    device_id = customer["device_id"]
    token = customer["device_token"]

    response = client.delete(
        f"{DEVICES_PATH}/{device_id}",
        headers={
            **_bearer(token),
            IDEMPOTENCY_KEY_HEADER: "idem-unbind-d3",
            REQUEST_ID_HEADER: "req-unbind-d3",
        },
    )
    assert response.status_code == 204, response.text

    with psycopg.connect(_t16_dsn(), autocommit=True) as conn:
        session = conn.execute(
            "SELECT session_epoch, lease_until FROM customer_session_state WHERE user_id = %s",
            (customer["user_id"],),
        ).fetchone()
        logout = conn.execute(
            "SELECT event, reason, request_id FROM customer_session_events "
            "WHERE user_id = %s ORDER BY created_at DESC, id DESC LIMIT 1",
            (customer["user_id"],),
        ).fetchone()
    assert session is not None
    # Epoch bumped from 1 to 2 and the lease is already expired at read
    # time: the device that just lost its binding cannot ride the old
    # session. The revoked lease keeps the full PostgreSQL microsecond
    # precision (PR #47 Codex review P2) — it must not survive into the
    # future, so the assertion carries no forward allowance: the unbind
    # transaction committed before this read began, and the fixture PG
    # clock shares the host clock source.
    assert session[0] == 2
    lease_until = datetime.fromisoformat(str(session[1]))
    assert lease_until <= datetime.now(UTC), (
        f"revoked lease must already be expired, got {lease_until!s}"
    )
    assert logout is not None
    assert logout[0] == "LOGOUT"
    assert logout[1] == "device_unbound"
    assert logout[2] == "req-unbind-d3"


def test_delete_missing_device_answers_not_found(client: TestClient) -> None:
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-dev-4", suffix="d4")
    response = client.delete(
        f"{DEVICES_PATH}/{uuid.uuid4()}",
        headers={**_bearer(customer["device_token"]), IDEMPOTENCY_KEY_HEADER: "idem-unbind-d4"},
    )
    assert response.status_code == 404, response.text
    assert response.json()["detail"]["code"] == "DEVICE_NOT_FOUND"


def test_delete_foreign_users_device_answers_not_found(client: TestClient) -> None:
    """IDOR: one device credential must never touch another customer's row."""
    first = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-dev-5a", suffix="d5a")
    second = _activated_customer(client, code=SECOND_CODE, fingerprint="fp-dev-5b", suffix="d5b")
    response = client.delete(
        f"{DEVICES_PATH}/{second['device_id']}",
        headers={**_bearer(first["device_token"]), IDEMPOTENCY_KEY_HEADER: "idem-unbind-d5a"},
    )
    assert response.status_code == 404, response.text
    assert response.json()["detail"]["code"] == "DEVICE_NOT_FOUND"
    # The foreign device is untouched.
    with psycopg.connect(_t16_dsn(), autocommit=True) as conn:
        status = conn.execute(
            "SELECT status FROM customer_devices WHERE id = %s",
            (second["device_id"],),
        ).fetchone()
    assert status is not None and status[0] == "BOUND"


def test_delete_already_unbound_answers_conflict(client: TestClient) -> None:
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-dev-6", suffix="d6")
    device_id = customer["device_id"]
    # Unbind with the *other* device's credential would not exist here, so
    # unbind own device via a second bound device first? Simpler: a second
    # device holds the slot-2 binding and performs the delete of device 1,
    # then retries the same delete against the now-UNBOUND row.
    _code_id = _code_id_of_user(customer["user_id"])
    second_token = _second_device_row(
        user_id=customer["user_id"],
        activation_code_id=_code_id,
        device_id=str(uuid.uuid4()),
        slot_no=2,
    )
    first_delete = client.delete(
        f"{DEVICES_PATH}/{device_id}",
        headers={**_bearer(second_token), IDEMPOTENCY_KEY_HEADER: "idem-unbind-d6"},
    )
    assert first_delete.status_code == 204, first_delete.text
    # The retry burns a *fresh* key: the same key would legitimately replay
    # the sealed 204 (the idempotent-unbind contract below), which is not
    # what this test pins — it pins the fresh-unbind conflict answer.
    retry = client.delete(
        f"{DEVICES_PATH}/{device_id}",
        headers={**_bearer(second_token), IDEMPOTENCY_KEY_HEADER: "idem-unbind-d6-retry"},
    )
    assert retry.status_code == 409, retry.text
    assert retry.json()["detail"]["code"] == "DEVICE_ALREADY_UNBOUND"


def test_unbind_other_device_keeps_own_slot_and_session(client: TestClient) -> None:
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-dev-7", suffix="d7")
    second_device_id = str(uuid.uuid4())
    _second_device_row(
        user_id=customer["user_id"],
        activation_code_id=_code_id_of_user(customer["user_id"]),
        device_id=second_device_id,
        slot_no=2,
    )
    # The activation session rides device 1; unbinding device 2 must not
    # disturb it (one live session per user, device-scoped revocation).
    response = client.delete(
        f"{DEVICES_PATH}/{second_device_id}",
        headers={
            **_bearer(customer["device_token"]),
            IDEMPOTENCY_KEY_HEADER: "idem-unbind-d7",
        },
    )
    assert response.status_code == 204, response.text

    with psycopg.connect(_t16_dsn(), autocommit=True) as conn:
        own = conn.execute(
            "SELECT status FROM customer_devices WHERE id = %s",
            (customer["device_id"],),
        ).fetchone()
        session = conn.execute(
            "SELECT session_epoch, device_id FROM customer_session_state WHERE user_id = %s",
            (customer["user_id"],),
        ).fetchone()
    assert own is not None and own[0] == "BOUND"
    assert session is not None
    assert session[0] == 1, "unbinding the other device must not bump the epoch"
    assert str(session[1]) == customer["device_id"]

    # The caller's own listing still shows slot 1 bound and slot 2 free,
    # with device 2 moved into history.
    listing = client.get(DEVICES_PATH, headers=_bearer(customer["device_token"]))
    assert listing.status_code == 200, listing.text
    body = listing.json()
    assert body["slots"][0]["device"] is not None
    assert body["slots"][1]["device"] is None
    assert [device["id"] for device in body["history"]] == [second_device_id]


def test_unbound_credential_answers_device_revoked(client: TestClient) -> None:
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-dev-8", suffix="d8")
    second_token = _second_device_row(
        user_id=customer["user_id"],
        activation_code_id=_code_id_of_user(customer["user_id"]),
        device_id=str(uuid.uuid4()),
        slot_no=2,
    )
    # Unbind the second device using the first device's credential…
    device_ids = _device_ids_of_user(customer["user_id"])
    second_device_id = [i for i in device_ids if i != customer["device_id"]][0]
    response = client.delete(
        f"{DEVICES_PATH}/{second_device_id}",
        headers={**_bearer(customer["device_token"]), IDEMPOTENCY_KEY_HEADER: "idem-unbind-d8"},
    )
    assert response.status_code == 204, response.text
    # …then the second device's own credential must report DEVICE_REVOKED —
    # the client-side signal to wipe its stored credentials (§13.2).
    afterwards = client.get(DEVICES_PATH, headers=_bearer(second_token))
    assert afterwards.status_code == 401, afterwards.text
    assert afterwards.json()["detail"]["code"] == "DEVICE_REVOKED"


# ---------------------------------------------------------------------------
# Third-device block: both slots full
# ---------------------------------------------------------------------------


def test_next_free_slot_reports_availability(devices_dsn: str) -> None:
    from app.customer_device_service import next_free_slot

    close_pg_pool()
    with psycopg.connect(_t16_dsn(), autocommit=True) as conn:
        conn.execute(
            "INSERT INTO activation_code_batches "
            "(id, name, face_value_fen, unit_price_fen_snapshot, credits_snapshot, "
            "quantity, activation_expires_at, status, created_by_user_id) "
            f"VALUES ('batch-slot', 'slot', 1500, 1000, 100, 1, "
            f"'{FUTURE_EXPIRY}', 'OPEN', 'admin_u')"
        )
        digest = compute_code_digest(SLOT_PROBE_CODE, key=TEST_KEY.encode("utf-8"))
        conn.execute(
            "INSERT INTO activation_codes "
            "(id, batch_id, code_digest, digest_key_version, masked_code, "
            "status, issued_at) "
            "VALUES ('code-slot', 'batch-slot', %s, 1, 'XS04-****', "
            "'ISSUED', '2026-01-01T00:00:00+00:00')",
            (digest,),
        )
    try:
        with psycopg.connect(_t16_dsn()) as conn:
            assert next_free_slot(conn, "code-slot") == 1, "empty code: slot 1 is free"
            conn.execute(
                "INSERT INTO users (id, username, display_name, role) "
                "VALUES ('slot_u', 'slot_u', 'Slot User', 'customer')"
            )
            _bind_raw(conn, "code-slot", "slot_u", "dev-slot-1", 1)
            assert next_free_slot(conn, "code-slot") == 2, "slot 1 taken: slot 2 is free"
            _bind_raw(conn, "code-slot", "slot_u", "dev-slot-2", 2)
            assert next_free_slot(conn, "code-slot") is None, "both slots taken: no free slot"
            # Releasing a slot frees it again for reuse.
            conn.execute(
                "UPDATE customer_devices SET status = 'UNBOUND', unbound_at = %s "
                "WHERE id = 'dev-slot-1'",
                (datetime.now(UTC).replace(microsecond=0).isoformat(),),
            )
            assert next_free_slot(conn, "code-slot") == 1, "unbound slot must be reusable"
    finally:
        close_pg_pool()


def test_third_bound_row_is_refused_by_the_database(devices_dsn: str) -> None:
    """The third-device block is proven by PostgreSQL itself (§11.3)."""
    close_pg_pool()
    with psycopg.connect(_t16_dsn(), autocommit=True) as conn:
        conn.execute(
            "INSERT INTO activation_code_batches "
            "(id, name, face_value_fen, unit_price_fen_snapshot, credits_snapshot, "
            "quantity, activation_expires_at, status, created_by_user_id) "
            f"VALUES ('batch-third', 'third', 1500, 1000, 100, 1, "
            f"'{FUTURE_EXPIRY}', 'OPEN', 'admin_u')"
        )
        digest = compute_code_digest(THIRD_DEVICE_CODE, key=TEST_KEY.encode("utf-8"))
        conn.execute(
            "INSERT INTO activation_codes "
            "(id, batch_id, code_digest, digest_key_version, masked_code, "
            "status, issued_at) "
            "VALUES ('code-third', 'batch-third', %s, 1, 'XS04-****', "
            "'ISSUED', '2026-01-01T00:00:00+00:00')",
            (digest,),
        )
        conn.execute(
            "INSERT INTO users (id, username, display_name, role) "
            "VALUES ('third_u', 'third_u', 'Third User', 'customer')"
        )
    try:
        with psycopg.connect(_t16_dsn()) as conn:
            _bind_raw(conn, "code-third", "third_u", "dev-third-1", 1)
            _bind_raw(conn, "code-third", "third_u", "dev-third-2", 2)
            # A third BOUND device cannot occupy slot 1 or slot 2.
            with pytest.raises(UniqueViolation):
                _bind_raw(conn, "code-third", "third_u", "dev-third-3", 1)
            with pytest.raises(UniqueViolation):
                _bind_raw(conn, "code-third", "third_u", "dev-third-4", 2)
            # The slot_no CHECK itself refuses a third slot number outright.
            with pytest.raises(CheckViolation, match="ck_customer_devices_slot_range"):
                _bind_raw(conn, "code-third", "third_u", "dev-third-5", 3)
    finally:
        close_pg_pool()


def test_authentication_accepts_retained_key_versions(client: TestClient) -> None:
    """A credential issued under a retained key version still authenticates.

    The activation transaction signs with the *highest* configured version
    (V2 here); authentication must probe every configured version so a V1
    credential stays valid through a rotation window (PR #44 review P1
    precedent on the fingerprint dimension).
    """
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-dev-9", suffix="d9")
    token = customer["device_token"]

    # Sanity: the token authenticates while both versions are configured…
    response = client.get(DEVICES_PATH, headers=_bearer(token))
    assert response.status_code == 200, response.text

    # …and the digest stored for it is the V2 one.
    from app.customer_device_service import keyed_digest

    v2_digest = keyed_digest(TEST_FINGERPRINT_KEY_V2.encode("utf-8"), token)
    with psycopg.connect(_t16_dsn(), autocommit=True) as conn:
        stored = conn.execute(
            "SELECT token_digest, token_key_version FROM customer_devices WHERE id = %s",
            (customer["device_id"],),
        ).fetchone()
    assert stored is not None
    assert str(stored[0]) == v2_digest
    assert int(stored[1]) == 2

    # Drop V2 from the environment: only V1 remains configured, and the V2
    # credential no longer resolves (rotation must retire, not break, old
    # keys — the probe list simply no longer contains the V2 digest).
    previous = os.environ.get("VIDEO_REPLICA_DEVICE_FINGERPRINT_HMAC_KEY_V2")
    try:
        os.environ.pop("VIDEO_REPLICA_DEVICE_FINGERPRINT_HMAC_KEY_V2", None)
        demoted = client.get(DEVICES_PATH, headers=_bearer(token))
        assert demoted.status_code == 401, demoted.text
        assert demoted.json()["detail"]["code"] == "DEVICE_CREDENTIAL_INVALID"
    finally:
        if previous is not None:
            os.environ["VIDEO_REPLICA_DEVICE_FINGERPRINT_HMAC_KEY_V2"] = previous


# ---------------------------------------------------------------------------
# Review-locked contracts: fail-closed 503s and the REVOKED credential
# ---------------------------------------------------------------------------


def test_device_routes_fail_closed_without_pg_runtime(
    monkeypatch: pytest.MonkeyPatch, route_state: str
) -> None:
    """No PG runtime → 503, never a partial or SQLite-lane answer."""
    from app.customer_device_routes import router as customer_device_router

    app = FastAPI()
    app.include_router(customer_device_router)
    monkeypatch.delenv(DATABASE_URL_ENV, raising=False)
    close_pg_pool()
    with TestClient(app) as fresh_client:
        response = fresh_client.get(DEVICES_PATH, headers=_bearer("any-token"))
    assert response.status_code == 503, response.text
    assert response.json()["detail"]["code"] == "DEVICE_SERVICE_UNAVAILABLE"


def test_unconfigured_device_keys_answer_service_unavailable(
    monkeypatch: pytest.MonkeyPatch, route_state: str
) -> None:
    """Review P2: key misconfiguration must answer 503, never a 401.

    A 401 here would trick the client into wiping perfectly valid stored
    credentials (the §13.2 client contract) because of an operator-side
    configuration failure — the activation-route precedent answers 503 for
    the same class of failure.
    """
    from app.customer_device_routes import router as customer_device_router

    app = FastAPI()
    app.include_router(customer_device_router)
    monkeypatch.setenv(DATABASE_URL_ENV, route_state)
    monkeypatch.delenv("VIDEO_REPLICA_DEVICE_FINGERPRINT_HMAC_KEY", raising=False)
    monkeypatch.delenv("VIDEO_REPLICA_DEVICE_FINGERPRINT_HMAC_KEY_V2", raising=False)
    close_pg_pool()
    try:
        with TestClient(app) as fresh_client:
            response = fresh_client.get(DEVICES_PATH, headers=_bearer("any-token"))
    finally:
        close_pg_pool()
    assert response.status_code == 503, response.text
    assert response.json()["detail"]["code"] == "DEVICE_SERVICE_UNAVAILABLE"


def test_revoked_status_credential_answers_device_revoked(client: TestClient) -> None:
    """A REVOKED (not just UNBOUND) row also reports DEVICE_REVOKED."""
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-dev-10", suffix="d10")
    token = customer["device_token"]
    # Flip the row to REVOKED directly (the admin revocation lane is T18).
    with psycopg.connect(_t16_dsn(), autocommit=True) as conn:
        conn.execute(
            "UPDATE customer_devices SET status = 'REVOKED', revoked_at = %s WHERE id = %s",
            (
                datetime.now(UTC).replace(microsecond=0).isoformat(),
                customer["device_id"],
            ),
        )
    response = client.get(DEVICES_PATH, headers=_bearer(token))
    assert response.status_code == 401, response.text
    assert response.json()["detail"]["code"] == "DEVICE_REVOKED"


# ---------------------------------------------------------------------------
# Idempotent unbind (PR #47 Codex review P2): the sealed-204 recovery
# ---------------------------------------------------------------------------


def test_unbind_requires_idempotency_key(client: TestClient) -> None:
    """The DELETE without an Idempotency-Key answers 400, before anything."""
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-dev-11", suffix="d11")
    response = client.delete(
        f"{DEVICES_PATH}/{customer['device_id']}", headers=_bearer(customer["device_token"])
    )
    assert response.status_code == 400, response.text
    assert response.json()["detail"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"
    # The check precedes the credential layer and any envelope write: the
    # device row is untouched and no envelope was created for the attempt.
    with psycopg.connect(_t16_dsn(), autocommit=True) as conn:
        status = conn.execute(
            "SELECT status FROM customer_devices WHERE id = %s",
            (customer["device_id"],),
        ).fetchone()
        envelopes = conn.execute(
            "SELECT count(*) FROM customer_idempotency_envelopes WHERE operation = 'device_unbind'",
        ).fetchone()
    assert status is not None and status[0] == "BOUND"
    assert envelopes is not None and int(envelopes[0]) == 0


def test_unbind_replays_sealed_204_after_response_loss(client: TestClient) -> None:
    """The lost-204 recovery: same key + same target replays the sealed 204.

    The caller unbound its *own* session-riding device, so the credential it
    retries with is by design no longer resolvable — the recovery probe must
    run before authentication, and the replay must not re-execute the unbind
    (one row flip, one epoch bump, one LOGOUT event).
    """
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-dev-12", suffix="d12")
    device_id = customer["device_id"]
    token = customer["device_token"]
    key = "idem-unbind-replay-d12"
    first = client.delete(
        f"{DEVICES_PATH}/{device_id}",
        headers={
            **_bearer(token),
            IDEMPOTENCY_KEY_HEADER: key,
            REQUEST_ID_HEADER: "req-replay-d12",
        },
    )
    assert first.status_code == 204, first.text
    assert REPLAY_HEADER not in first.headers

    # The retry rides the same — now unresolvable — credential and the same
    # key; the sealed envelope answers before authentication ever runs, and
    # the echoed request id is the sealed one, not a freshly minted one.
    retry = client.delete(
        f"{DEVICES_PATH}/{device_id}",
        headers={**_bearer(token), IDEMPOTENCY_KEY_HEADER: key},
    )
    assert retry.status_code == 204, retry.text
    assert retry.headers.get(REPLAY_HEADER) == "true"
    assert retry.headers.get(REQUEST_ID_HEADER) == "req-replay-d12"

    # The replay re-executed nothing: exactly one row flip, one epoch bump
    # (1 -> 2, not 3) and one LOGOUT event.
    with psycopg.connect(_t16_dsn(), autocommit=True) as conn:
        row = conn.execute(
            "SELECT status, unbound_at, revoked_at FROM customer_devices WHERE id = %s",
            (device_id,),
        ).fetchone()
        session = conn.execute(
            "SELECT session_epoch FROM customer_session_state WHERE user_id = %s",
            (customer["user_id"],),
        ).fetchone()
        logouts = conn.execute(
            "SELECT count(*) FROM customer_session_events WHERE user_id = %s AND event = 'LOGOUT'",
            (customer["user_id"],),
        ).fetchone()
    assert row is not None and row[0] == "UNBOUND" and row[2] is None
    assert session is not None and session[0] == 2
    assert logouts is not None and int(logouts[0]) == 1


def test_unbind_same_key_different_target_conflicts(client: TestClient) -> None:
    """The key identifies one submission: a different target answers 409."""
    customer = _activated_customer(client, code=FIRST_CODE, fingerprint="fp-dev-13", suffix="d13")
    second_device_id = str(uuid.uuid4())
    _second_device_row(
        user_id=customer["user_id"],
        activation_code_id=_code_id_of_user(customer["user_id"]),
        device_id=second_device_id,
        slot_no=2,
    )
    key = "idem-unbind-conflict-d13"
    headers = {**_bearer(customer["device_token"]), IDEMPOTENCY_KEY_HEADER: key}
    first = client.delete(f"{DEVICES_PATH}/{second_device_id}", headers=headers)
    assert first.status_code == 204, first.text

    # Same key, but the target is now the caller's own device: the request
    # hash differs, so the spent key answers a conflict instead of unbinding.
    conflict = client.delete(f"{DEVICES_PATH}/{customer['device_id']}", headers=headers)
    assert conflict.status_code == 409, conflict.text
    assert conflict.json()["detail"]["code"] == "IDEMPOTENCY_CONFLICT"

    # The second target was never touched.
    with psycopg.connect(_t16_dsn(), autocommit=True) as conn:
        status = conn.execute(
            "SELECT status FROM customer_devices WHERE id = %s",
            (customer["device_id"],),
        ).fetchone()
    assert status is not None and status[0] == "BOUND"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _code_id_of_user(user_id: str) -> str:
    with psycopg.connect(_t16_dsn(), autocommit=True) as conn:
        row = conn.execute(
            "SELECT d.activation_code_id FROM customer_devices d "
            "WHERE d.user_id = %s ORDER BY d.created_at LIMIT 1",
            (user_id,),
        ).fetchone()
    assert row is not None
    return str(row[0])


def _device_ids_of_user(user_id: str) -> list[str]:
    with psycopg.connect(_t16_dsn(), autocommit=True) as conn:
        rows = conn.execute(
            "SELECT id FROM customer_devices WHERE user_id = %s ORDER BY created_at",
            (user_id,),
        ).fetchall()
    return [str(row[0]) for row in rows]


def _raw_slots_and_history(conn: psycopg.Connection, *, user_id: str) -> tuple[dict, list]:
    """Service-free view for asserting on rows directly."""
    rows = conn.execute(
        "SELECT slot_no, id, status FROM customer_devices WHERE user_id = %s ORDER BY created_at",
        (user_id,),
    ).fetchall()
    slots: dict[int, str | None] = {1: None, 2: None}
    history: list[dict] = []
    for slot_no, device_id, status in rows:
        if str(status) == "BOUND":
            slots[int(slot_no)] = str(device_id)
        else:
            history.append({"id": str(device_id), "status": str(status)})
    return slots, history


def _bind_raw(
    conn: psycopg.Connection,
    code_id: str,
    user_id: str,
    device_id: str,
    slot_no: int,
) -> None:
    """Insert one BOUND device row directly with V1-keyed digests.

    The insert runs in its own savepoint so the UniqueViolation cases of
    the third-device tests leave the surrounding transaction usable.
    """
    from app.customer_device_service import keyed_digest

    key_v1 = TEST_FINGERPRINT_KEY_V1.encode("utf-8")
    with conn.transaction():
        conn.execute(
            "INSERT INTO customer_devices "
            "(id, activation_code_id, user_id, slot_no, display_name, platform, "
            " fingerprint_hmac, fingerprint_key_version, token_digest, token_key_version) "
            "VALUES (%s, %s, %s, %s, 'Raw Device', 'windows', %s, 1, %s, 1)",
            (
                device_id,
                code_id,
                user_id,
                slot_no,
                keyed_digest(key_v1, f"fp-raw-{device_id}"),
                keyed_digest(key_v1, f"tok-raw-{device_id}"),
            ),
        )
