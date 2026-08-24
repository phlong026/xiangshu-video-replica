"""T23 / BILL-02 — audited admin adjustments (审计化后台调账).

Fail-first tests for the frozen file ``server/app/admin_customer_routes.py``
(code checklist §3.2) and migration ``039_admin_adjustments``. Task list §5
T23 exit gate: *双确认、来源单、幂等、真实 actor；禁止直接改余额* — the
adjustment lands as one atomic transaction that writes the ``PAID``
``provider='admin_adjustment'`` recharge order (revision 026 shapes: created
PAID, no trade number), the wallet ``CHARGE`` ledger row, the atomic wallet
credit increment and one append-only ``admin_adjustments`` audit row naming
the real acting administrator (dev doc §15: real actor, reason,
confirmation, Idempotency-Key, request id).

Contract under test:

- ``POST /api/control/customers/{user_id}/adjustments`` requires the full
  admin write contract: a real admin session (auditors are read-only, 403
  ``AUDITOR_READ_ONLY``), ``Idempotency-Key`` (400 without it),
  ``confirm=true`` (400 ``CONFIRMATION_REQUIRED``) and a non-blank reason
  (400 ``REASON_REQUIRED``) — the T12/T18 precedent;
- every adjustment carries a source document (来源单): a frozen enum type
  plus a non-blank reference — 400 ``ADJUSTMENT_VALIDATION_FAILED`` shapes
  are refused, and revision 039's CHECK constraints refuse the malformed
  rows the route might someday let slip (defense in depth);
- the amount is derived from the *current* frozen billing snapshot, never
  typed in by the operator: ``amount_fen = credits *
  internal_base_unit_price_fen`` with the snapshot frozen on the order —
  there is no lane where an operator types an arbitrary amount, and the
  PRICE-01 floor holds by construction (charged == base);
- the pricing scope follows the target user: a customer bound to an
  activation code is ``CUSTOMER_STANDARD``, an internal account stays
  ``INTERNAL`` (revision 026 pairing);
- the ledger difference is zero (账本差额为零): after any adjustment the
  wallet balance grew by exactly ``credits``, the order is PAID with
  ``credits`` credits and one ``CHARGE`` row with ``available_delta =
  credits`` references it — no balance mutation without its ledger row
  (禁止直接改余额);
- idempotency (revision 031 snapshot layer): a same-key retry replays the
  sealed response (``X-Idempotent-Replay: true``) without a second order,
  CHARGE or credit increment; the same key against different parameters or
  a different target user answers 409 ``IDEMPOTENCY_CONFLICT``; a business
  failure (404) rolls the placeholder back so the key stays reusable;
- ``paid_at`` and the audit row timestamps come from the PostgreSQL
  transaction clock (SES-01 discipline — never the process clock);
- the SQLite/missing-DSN runtime answers 503
  ``ADJUSTMENT_SERVICE_UNAVAILABLE`` (fail-closed, the T12/T18 precedent);
- ``admin_adjustments`` is append-only: UPDATE and DELETE are refused by the
  revision 039 trigger and TRUNCATE by the shared 036 guard;
- ``GET /api/control/customers/{user_id}/adjustments`` lists the audit
  trail for operators and auditors (the T33 management page entry point).
"""

from __future__ import annotations

import os
import secrets
import uuid
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from psycopg.errors import CheckViolation

from app.admin_auth_routes import (
    ADMIN_CSRF_HEADER,
    ADMIN_SESSION_HMAC_KEY_ENV,
    issue_exchange_credential,
)
from app.db_pg import DATABASE_URL_ENV, close_pg_pool

DEFAULT_DSN = "postgresql://testuser:testpass@localhost:5433/customer_v3_test"
SKIP_REASON = "PostgreSQL fixture not reachable; start it via scripts/pg-fixture.sh start"

T23_DB_NAME = "t23_admin_adjustments_test"

TEST_ADMIN_SESSION_KEY = secrets.token_urlsafe(48)  # admin-session HMAC key

ADJUSTMENTS_PATH = "/api/control/customers/{user_id}/adjustments"
IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"
REQUEST_ID_HEADER = "X-Request-Id"
REPLAY_HEADER = "X-Idempotent-Replay"
FUTURE_EXPIRY = "2099-01-01T00:00:00+00:00"

# The 002/022 runtime_settings default billing snapshot (fen).
BASE_UNIT_PRICE_FEN = 1000
MIN_RECHARGE_FEN = 10000
RECHARGE_STEP_FEN = 1000

SOURCE_DOCUMENT_TYPES = (
    "CS_TICKET",
    "REFUND_APPROVAL",
    "COMPENSATION_APPROVAL",
    "LEDGER_CORRECTION",
)

CUSTOMER_USER_ID = "customer_u"
INTERNAL_USER_ID = "internal_u"
WALLETS_USER_ID = "walletless_u"


def _pg_dsn() -> str:
    return os.environ.get("TEST_POSTGRESQL_URL", DEFAULT_DSN)


def _admin_dsn() -> str:
    return _pg_dsn().rsplit("/", 1)[0] + "/postgres"


def _t23_dsn() -> str:
    return _pg_dsn().rsplit("/", 1)[0] + f"/{T23_DB_NAME}"


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
def adjustments_dsn() -> Iterator[str]:
    from alembic import command
    from alembic.config import Config

    if not _pg_available(_pg_dsn()):
        pytest.skip(SKIP_REASON)
    with psycopg.connect(_admin_dsn(), autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{T23_DB_NAME}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{T23_DB_NAME}"')
    server_dir = Path(__file__).resolve().parent.parent
    config = Config(str(server_dir / "alembic.ini"))
    config.set_main_option("script_location", str(server_dir / "migrations"))
    config.set_main_option(
        "sqlalchemy.url", _t23_dsn().replace("postgresql://", "postgresql+psycopg://")
    )
    command.upgrade(config, "head")
    try:
        yield _t23_dsn()
    finally:
        with psycopg.connect(_admin_dsn(), autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{T23_DB_NAME}" WITH (FORCE)')


def _seed_customer_activation(conn: psycopg.Connection) -> None:
    """Bind customer_u to an activation code (batch -> code -> activation)."""
    conn.execute(
        "INSERT INTO activation_code_batches "
        "(id, name, face_value_fen, unit_price_fen_snapshot, credits_snapshot, "
        " quantity, activation_expires_at, status, created_by_user_id) "
        "VALUES ('batch-cu', 'batch-cu', 1500, 1000, 100, 1, "
        f"'{FUTURE_EXPIRY}', 'OPEN', 'admin_u')"
    )
    # Create a dummy recharge order for the activation FK
    dummy_order_id = "dummy-activation-order"
    conn.execute(
        "INSERT INTO recharge_orders "
        "(id, user_id, merchant_order_no, provider, status, pricing_scope, "
        " base_unit_price_fen_snapshot, charged_unit_price_fen_snapshot, "
        " min_recharge_fen_snapshot, recharge_step_fen_snapshot, amount_fen, credits, paid_at) "
        "VALUES (%s, 'customer_u', %s, 'admin_adjustment', 'PAID', 'CUSTOMER_STANDARD', "
        "1000, 1000, 10000, 1000, 1000, 1, now())",
        (dummy_order_id, "DUMMY-activation-order"),
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
        "VALUES ('act-cu', 'code-cu', 'customer_u', NULL, %s)",
        (dummy_order_id,),
    )


@pytest.fixture()
def route_state(adjustments_dsn: str) -> Iterator[str]:
    close_pg_pool()
    with psycopg.connect(_t23_dsn(), autocommit=True) as conn:
        # 036 refuses TRUNCATE of the append-only audit tables; the replica
        # role suspends triggers for this cleanup sweep only.
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
        # TRUNCATE users CASCADE also swept runtime_settings (its
        # updated_by_user_id FK references users) — restore the singleton.
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
            f"('{CUSTOMER_USER_ID}', 'customer_u', 'Customer User', 'user'), "
            f"('{INTERNAL_USER_ID}', 'internal_u', 'Internal User', 'user'), "
            f"('{WALLETS_USER_ID}', 'walletless_u', 'Walletless User', 'user')"
        )
        conn.execute(
            "INSERT INTO wallets (user_id, available_credits, reserved_credits) VALUES "
            f"('{CUSTOMER_USER_ID}', 50, 0), "
            f"('{INTERNAL_USER_ID}', 10, 0)"
        )
        # Seed the opening balances as PAID orders + CHARGE rows so the
        # zero-ledger-difference invariant holds from the very first row
        # (账本差额为零：钱包余额必须有对应的 CHARGE 凭据).
        for owner, opening_credits in ((CUSTOMER_USER_ID, 50), (INTERNAL_USER_ID, 10)):
            opening_order_id = f"opening-{owner}"
            conn.execute(
                "INSERT INTO recharge_orders "
                "(id, user_id, merchant_order_no, provider, status, pricing_scope, "
                " base_unit_price_fen_snapshot, charged_unit_price_fen_snapshot, "
                " min_recharge_fen_snapshot, recharge_step_fen_snapshot, "
                " amount_fen, credits, paid_at) "
                "VALUES (%s, %s, %s, 'admin_adjustment', 'PAID', 'INTERNAL', "
                "1000, 1000, 10000, 1000, %s, %s, now())",
                (
                    opening_order_id,
                    owner,
                    f"OPENING-{owner}",
                    opening_credits * 1000,
                    opening_credits,
                ),
            )
            conn.execute(
                "INSERT INTO wallet_transactions "
                "(id, user_id, type, available_delta, reserved_delta, recharge_order_id, "
                " task_id, billing_round, idempotency_key) "
                "VALUES (%s, %s, 'CHARGE', %s, 0, %s, NULL, NULL, %s)",
                (
                    f"opening-charge-{owner}",
                    owner,
                    opening_credits,
                    opening_order_id,
                    f"opening-{owner}",
                ),
            )
        _seed_customer_activation(conn)
        # Freeze the billing snapshot the tests assert against.
        conn.execute(
            "UPDATE runtime_settings SET internal_base_unit_price_fen = 1000, "
            "min_recharge_fen = 10000, recharge_step_fen = 1000 WHERE id = 1"
        )
    yield adjustments_dsn
    close_pg_pool()


@pytest.fixture()
def admin_app(monkeypatch: pytest.MonkeyPatch, route_state: str) -> Iterator[FastAPI]:
    from app.admin_auth_routes import router as admin_auth_router
    from app.admin_customer_routes import router as admin_customer_router

    app = FastAPI()
    app.include_router(admin_auth_router)
    app.include_router(admin_customer_router)
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _admin_session(client: TestClient, actor: str = "admin_u") -> dict[str, str]:
    """Exchange a real admin session cookie + CSRF header (the T12 pattern)."""
    response = client.post(
        "/api/control/admin/session/exchange",
        json={"credential": issue_exchange_credential(actor, ttl_seconds=3600)},
    )
    assert response.status_code == 201, response.text
    return {ADMIN_CSRF_HEADER: response.json()["csrf_token"]}


def _adjustment_path(user_id: str) -> str:
    return ADJUSTMENTS_PATH.format(user_id=user_id)


def _create_adjustment(
    client: TestClient,
    admin_headers: dict[str, str],
    *,
    user_id: str = CUSTOMER_USER_ID,
    credits: int = 5,
    source_document_type: str = "CS_TICKET",
    source_document_ref: str = "TICKET-1001",
    reason: str = "客服补偿：拆解失败两次",
    confirm: bool = True,
    key: str | None = None,
) -> object:
    headers = dict(admin_headers)
    headers[IDEMPOTENCY_KEY_HEADER] = key or f"key-{uuid.uuid4()}"
    return client.post(
        _adjustment_path(user_id),
        json={
            "confirm": confirm,
            "reason": reason,
            "credits": credits,
            "source_document_type": source_document_type,
            "source_document_ref": source_document_ref,
        },
        headers=headers,
    )


def _fetch_one(query: str, params: tuple[object, ...] = ()) -> tuple | None:
    with psycopg.connect(_t23_dsn(), autocommit=True) as conn:
        return conn.execute(query, params).fetchone()


def _fetch_all(query: str, params: tuple[object, ...] = ()) -> list[tuple]:
    with psycopg.connect(_t23_dsn(), autocommit=True) as conn:
        return conn.execute(query, params).fetchall()


def _wallet_balance(user_id: str) -> tuple[int, int]:
    row = _fetch_one(
        "SELECT available_credits, reserved_credits FROM wallets WHERE user_id = %s",
        (user_id,),
    )
    assert row is not None
    return int(row[0]), int(row[1])


def _adjustment_audit_rows(user_id: str) -> list[tuple]:
    return _fetch_all(
        "SELECT id, recharge_order_id, target_user_id, admin_user_id, "
        "source_document_type, source_document_ref, reason, request_id "
        "FROM admin_adjustments WHERE target_user_id = %s ORDER BY created_at",
        (user_id,),
    )


def _order_row(order_id: str) -> tuple:
    row = _fetch_one(
        "SELECT provider, provider_trade_no, status, pricing_scope, "
        "base_unit_price_fen_snapshot, charged_unit_price_fen_snapshot, "
        "min_recharge_fen_snapshot, recharge_step_fen_snapshot, "
        "amount_fen, credits, paid_at FROM recharge_orders WHERE id = %s",
        (order_id,),
    )
    assert row is not None
    return row


def _charge_rows(order_id: str) -> list[tuple]:
    return _fetch_all(
        "SELECT type, available_delta, reserved_delta, recharge_order_id, task_id, "
        "billing_round, idempotency_key FROM wallet_transactions "
        "WHERE recharge_order_id = %s",
        (order_id,),
    )


# ---------------------------------------------------------------------------
# Schema / migration (revision 039)
# ---------------------------------------------------------------------------


def test_admin_adjustments_table_shape(adjustments_dsn: str) -> None:
    columns = {
        str(row[0])
        for row in _fetch_all(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'admin_adjustments'"
        )
    }
    assert {
        "id",
        "recharge_order_id",
        "target_user_id",
        "admin_user_id",
        "source_document_type",
        "source_document_ref",
        "reason",
        "request_id",
        "created_at",
    } <= columns
    # One audit row per adjustment order, never two.
    unique_indexes = _fetch_all(
        "SELECT indexdef FROM pg_indexes "
        "WHERE tablename = 'admin_adjustments' AND indexdef LIKE '%%UNIQUE%%'"
    )
    assert any("recharge_order_id" in str(row[0]) for row in unique_indexes), unique_indexes


def test_admin_adjustments_check_constraints(adjustments_dsn: str) -> None:
    with psycopg.connect(_t23_dsn()) as conn:
        conn.execute(
            "INSERT INTO users (id, username, display_name, role) "
            "VALUES ('schema_u', 'schema_u', 'Schema', 'user') "
            "ON CONFLICT DO NOTHING"
        )
        conn.execute(
            "INSERT INTO wallets (user_id, available_credits, reserved_credits) "
            "VALUES ('schema_u', 0, 0) ON CONFLICT DO NOTHING"
        )
        order_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO recharge_orders "
            "(id, user_id, merchant_order_no, provider, status, pricing_scope, "
            " base_unit_price_fen_snapshot, charged_unit_price_fen_snapshot, "
            " min_recharge_fen_snapshot, recharge_step_fen_snapshot, amount_fen, credits, paid_at) "
            "VALUES (%s, 'schema_u', %s, 'admin_adjustment', 'PAID', 'INTERNAL', "
            "1000, 1000, 10000, 1000, 1000, 1, now())",
            (order_id, f"ADJ-{uuid.uuid4().hex}"),
        )

        def _insert(*, doc_type: str, doc_ref: str, reason: str, request_id: str) -> None:
            conn.execute(
                "INSERT INTO admin_adjustments "
                "(id, recharge_order_id, target_user_id, admin_user_id, "
                " source_document_type, source_document_ref, reason, request_id) "
                "VALUES (%s, %s, 'schema_u', 'admin_u', %s, %s, %s, %s)",
                (str(uuid.uuid4()), order_id, doc_type, doc_ref, reason, request_id),
            )

        # Each violating INSERT aborts the surrounding transaction, so every
        # probe runs in its own transaction and the setup commits first.
        conn.commit()
        for doc_type, doc_ref, reason, request_id in (
            ("MYSTERY", "T-1", "r", "req-1"),
            ("CS_TICKET", "   ", "r", "req-1"),
            ("CS_TICKET", "T-1", "  ", "req-1"),
            ("CS_TICKET", "T-1", "r", " "),
        ):
            with pytest.raises(CheckViolation):
                try:
                    _insert(
                        doc_type=doc_type,
                        doc_ref=doc_ref,
                        reason=reason,
                        request_id=request_id,
                    )
                finally:
                    conn.rollback()


def test_admin_adjustments_append_only(adjustments_dsn: str) -> None:
    order_id = str(uuid.uuid4())
    with psycopg.connect(_t23_dsn(), autocommit=True) as conn:
        conn.execute(
            "INSERT INTO users (id, username, display_name, role) VALUES "
            "('append_u', 'append_u', 'Append', 'user'), "
            "('admin_u', 'admin_u', 'Admin User', 'admin') "
            "ON CONFLICT DO NOTHING"
        )
        conn.execute(
            "INSERT INTO wallets (user_id, available_credits, reserved_credits) "
            "VALUES ('append_u', 0, 0) ON CONFLICT DO NOTHING"
        )
        conn.execute(
            "INSERT INTO recharge_orders "
            "(id, user_id, merchant_order_no, provider, status, pricing_scope, "
            " base_unit_price_fen_snapshot, charged_unit_price_fen_snapshot, "
            " min_recharge_fen_snapshot, recharge_step_fen_snapshot, amount_fen, credits, paid_at) "
            f"VALUES ('{order_id}', 'append_u', 'ADJ-{uuid.uuid4().hex}', "
            "'admin_adjustment', 'PAID', 'INTERNAL', 1000, 1000, 10000, 1000, 1000, 1, now())"
        )
        adjustment_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO admin_adjustments "
            "(id, recharge_order_id, target_user_id, admin_user_id, "
            " source_document_type, source_document_ref, reason, request_id) "
            f"VALUES ('{adjustment_id}', '{order_id}', 'append_u', 'admin_u', "
            "'CS_TICKET', 'T-1', 'r', 'req-1')"
        )
    # The 039 trigger + the shared 036 TRUNCATE guard refuse every rewrite
    # path at the PostgreSQL level (the 029/036/038 trigger precedents).
    with psycopg.connect(_t23_dsn(), autocommit=True) as conn:
        with pytest.raises(psycopg.errors.RaiseException):
            conn.execute(
                "UPDATE admin_adjustments SET reason = 'rewritten' WHERE id = %s",
                (adjustment_id,),
            )
        with pytest.raises(psycopg.errors.RaiseException):
            conn.execute("DELETE FROM admin_adjustments WHERE id = %s", (adjustment_id,))
        with pytest.raises(psycopg.errors.RaiseException):
            conn.execute("TRUNCATE admin_adjustments")
        survivors = conn.execute("SELECT count(*) FROM admin_adjustments").fetchone()
    assert survivors is not None and int(survivors[0]) == 1


def test_audit_foreign_keys_refuse_cascade_delete(adjustments_dsn: str) -> None:
    """RESTRICT FKs (the PR review P3): CASCADE would silently erase audit rows
    whenever the row trigger is suspended (session_replication_role = replica),
    and SET NULL cannot apply to a NOT NULL actor. Every referencing delete
    fails loudly; the audit row survives."""
    fk_rows = _fetch_all(
        "SELECT confdeltype FROM pg_constraint "
        "WHERE conrelid = 'admin_adjustments'::regclass AND contype = 'f'"
    )
    assert len(fk_rows) == 3  # order + target user + admin user
    for (confdeltype,) in fk_rows:
        assert confdeltype in ("r", "a"), confdeltype  # RESTRICT / NO ACTION only

    order_id = str(uuid.uuid4())
    with psycopg.connect(_t23_dsn(), autocommit=True) as conn:
        conn.execute(
            "INSERT INTO users (id, username, display_name, role) VALUES "
            "('restrict_u', 'restrict_u', 'Restrict', 'user'), "
            "('admin_ru', 'admin_ru', 'Admin RU', 'admin') ON CONFLICT DO NOTHING"
        )
        conn.execute(
            "INSERT INTO recharge_orders "
            "(id, user_id, merchant_order_no, provider, status, pricing_scope, "
            " base_unit_price_fen_snapshot, charged_unit_price_fen_snapshot, "
            " min_recharge_fen_snapshot, recharge_step_fen_snapshot, amount_fen, credits, paid_at) "
            f"VALUES ('{order_id}', 'restrict_u', 'ADJ-{uuid.uuid4().hex}', "
            "'admin_adjustment', 'PAID', 'INTERNAL', 1000, 1000, 10000, 1000, 1000, 1, now())"
        )
        conn.execute(
            "INSERT INTO admin_adjustments "
            "(id, recharge_order_id, target_user_id, admin_user_id, "
            " source_document_type, source_document_ref, reason, request_id) "
            "VALUES ('adj-restrict-1', %s, 'restrict_u', 'admin_ru', "
            "'CS_TICKET', 'T-1', 'r', 'req-restrict-1')",
            (order_id,),
        )
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            conn.execute("DELETE FROM recharge_orders WHERE id = %s", (order_id,))
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            conn.execute("DELETE FROM users WHERE id = 'restrict_u'")
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            conn.execute("DELETE FROM users WHERE id = 'admin_ru'")
        survivors = conn.execute(
            "SELECT count(*) FROM admin_adjustments WHERE id = 'adj-restrict-1'"
        ).fetchone()
    assert survivors is not None and int(survivors[0]) == 1


# ---------------------------------------------------------------------------
# The adjustment happy path and its ledger invariants
# ---------------------------------------------------------------------------


def test_adjustment_creates_paid_order_charge_and_audit_row(
    client: TestClient,
) -> None:
    admin = _admin_session(client)
    response = _create_adjustment(client, admin, credits=5, key="adj-create-1")
    assert response.status_code == 201, response.text
    payload = response.json()
    for field in ("adjustment_id", "order_id", "request_id"):
        assert isinstance(payload.get(field), str) and payload[field], payload
    assert int(payload["credits"]) == 5
    assert int(payload["amount_fen"]) == 5 * BASE_UNIT_PRICE_FEN
    assert payload["pricing_scope"] == "CUSTOMER_STANDARD"
    assert payload["wallet_balance_after"] == 55  # 50 + 5
    assert payload["source_document_type"] == "CS_TICKET"
    assert payload["source_document_ref"] == "TICKET-1001"
    assert response.headers.get(REQUEST_ID_HEADER) == payload["request_id"]

    order = _order_row(payload["order_id"])
    assert order[0] == "admin_adjustment"  # provider
    assert order[1] is None  # no third-party trade number
    assert order[2] == "PAID"  # created PAID by the double-confirmed transaction
    assert order[3] == "CUSTOMER_STANDARD"
    assert order[4] == BASE_UNIT_PRICE_FEN  # base snapshot
    assert order[5] == BASE_UNIT_PRICE_FEN  # charged snapshot (PRICE-01 floor holds)
    assert order[6] == MIN_RECHARGE_FEN
    assert order[7] == RECHARGE_STEP_FEN
    assert order[8] == 5 * BASE_UNIT_PRICE_FEN
    assert order[9] == 5
    assert isinstance(order[10], str) and order[10], "paid_at must be stamped"

    charges = _charge_rows(payload["order_id"])
    assert len(charges) == 1
    assert charges[0][0] == "CHARGE"
    assert charges[0][1] == 5  # available_delta
    assert charges[0][2] == 0  # reserved_delta
    assert charges[0][4] is None  # task_id
    assert charges[0][5] is None  # billing_round
    assert charges[0][6] == f"admin_adjustment:charge:{payload['order_id']}"

    audit = _adjustment_audit_rows(CUSTOMER_USER_ID)
    assert len(audit) == 1
    assert audit[0][1] == payload["order_id"]
    assert audit[0][2] == CUSTOMER_USER_ID
    assert audit[0][3] == "admin_u"  # the real acting administrator
    assert audit[0][4] == "CS_TICKET"
    assert audit[0][5] == "TICKET-1001"
    assert audit[0][7] == payload["request_id"]

    assert _wallet_balance(CUSTOMER_USER_ID) == (55, 0)


def test_adjustment_internal_scope_for_user_without_activation(
    client: TestClient,
) -> None:
    admin = _admin_session(client)
    response = _create_adjustment(
        client,
        admin,
        user_id=INTERNAL_USER_ID,
        credits=2,
        source_document_type="LEDGER_CORRECTION",
        source_document_ref="CORR-77",
        reason="内部测试账户补充",
        key="adj-internal-1",
    )
    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["pricing_scope"] == "INTERNAL"
    assert payload["wallet_balance_after"] == 12  # 10 + 2
    assert _order_row(payload["order_id"])[3] == "INTERNAL"
    # The audit trail names the internal account too.
    assert len(_adjustment_audit_rows(INTERNAL_USER_ID)) == 1


def test_suspended_code_prices_as_customer_revoked_does_not(client: TestClient) -> None:
    """Pricing follows revision 027's current-binding rule (the PR review P3):
    ACTIVE or SUSPENDED count as a current binding; a REVOKED code keeps its
    binding row for audit only and must not price as CUSTOMER_STANDARD."""
    admin = _admin_session(client)
    # The 027 status-shape CHECK couples every state to its proof timestamp
    # (suspended_at / revoked_at), so the UPDATE stamps it too.
    with psycopg.connect(_t23_dsn(), autocommit=True) as conn:
        conn.execute(
            "UPDATE activation_codes SET status = 'SUSPENDED', "
            "suspended_at = '2026-01-02T00:00:00+00:00' WHERE id = 'code-cu'"
        )
    suspended = _create_adjustment(client, admin, credits=1, key="adj-scope-susp-1")
    assert suspended.status_code == 201, suspended.text
    assert suspended.json()["pricing_scope"] == "CUSTOMER_STANDARD"

    with psycopg.connect(_t23_dsn(), autocommit=True) as conn:
        conn.execute(
            "UPDATE activation_codes SET status = 'REVOKED', "
            "revoked_at = '2026-01-03T00:00:00+00:00' WHERE id = 'code-cu'"
        )
    revoked = _create_adjustment(client, admin, credits=1, key="adj-scope-rev-1")
    assert revoked.status_code == 201, revoked.text
    assert revoked.json()["pricing_scope"] == "INTERNAL"


def test_ledger_difference_is_zero_after_adjustment(client: TestClient) -> None:
    """账本差额为零：钱包、order 与 CHAGE 三方一致，无无凭据的余额变化."""
    admin = _admin_session(client)
    before_available, _ = _wallet_balance(CUSTOMER_USER_ID)
    first = _create_adjustment(client, admin, credits=3, key="adj-zero-1")
    second = _create_adjustment(client, admin, credits=7, key="adj-zero-2")
    assert first.status_code == 201 and second.status_code == 201

    after_available, after_reserved = _wallet_balance(CUSTOMER_USER_ID)
    assert after_available == before_available + 3 + 7
    assert after_reserved == 0

    rows = _fetch_all(
        "SELECT ro.credits, wt.available_delta "
        "FROM recharge_orders AS ro "
        "JOIN wallet_transactions AS wt ON wt.recharge_order_id = ro.id "
        "WHERE ro.provider = 'admin_adjustment' AND ro.user_id = %s "
        "AND ro.merchant_order_no NOT LIKE 'OPENING-%%'",
        (CUSTOMER_USER_ID,),
    )
    assert len(rows) == 2
    assert {row[0] for row in rows} == {3, 7}
    assert {row[1] for row in rows} == {3, 7}

    # Reconciliation: every adjustment order is PAID with exactly one CHARGE
    # and the summed deltas reconcile the wallet balance.
    summary = _fetch_one(
        "SELECT "
        "  (SELECT COALESCE(SUM(available_delta), 0) FROM wallet_transactions "
        "   WHERE user_id = %s AND type = 'CHARGE'), "
        "  (SELECT available_credits + reserved_credits FROM wallets WHERE user_id = %s)",
        (CUSTOMER_USER_ID, CUSTOMER_USER_ID),
    )
    assert summary is not None
    charged_total, wallet_total = int(summary[0]), int(summary[1])
    assert charged_total == wallet_total, "账本累计入账必须与钱包余额一致（差额为零）"


def test_adjustment_freezes_unit_price_snapshot(client: TestClient) -> None:
    admin = _admin_session(client)
    first = _create_adjustment(client, admin, credits=2, key="adj-price-1")
    assert first.status_code == 201, first.text
    with psycopg.connect(_t23_dsn(), autocommit=True) as conn:
        conn.execute("UPDATE runtime_settings SET internal_base_unit_price_fen = 2500 WHERE id = 1")
    try:
        second = _create_adjustment(client, admin, credits=2, key="adj-price-2")
        assert second.status_code == 201, second.text
        assert int(first.json()["amount_fen"]) == 2 * BASE_UNIT_PRICE_FEN
        assert int(second.json()["amount_fen"]) == 2 * 2500
        assert _order_row(first.json()["order_id"])[5] == BASE_UNIT_PRICE_FEN
        assert _order_row(second.json()["order_id"])[5] == 2500
    finally:
        with psycopg.connect(_t23_dsn(), autocommit=True) as conn:
            conn.execute(
                "UPDATE runtime_settings SET internal_base_unit_price_fen = 1000 WHERE id = 1"
            )


def test_response_balance_is_the_post_update_row(client: TestClient) -> None:
    """The response balance comes from UPDATE ... RETURNING (the PR review P3):
    a concurrent wallet write landing between any pre-read and the increment
    must be visible in the response — never a stale pre-read plus credits."""
    admin = _admin_session(client)
    # A concurrent writer moved the balance to 77 out-of-band.
    with psycopg.connect(_t23_dsn(), autocommit=True) as conn:
        conn.execute(
            "UPDATE wallets SET available_credits = 77 WHERE user_id = %s",
            (CUSTOMER_USER_ID,),
        )
    response = _create_adjustment(client, admin, credits=5, key="adj-returning-1")
    assert response.status_code == 201, response.text
    assert response.json()["wallet_balance_after"] == 82  # 77 + 5 — the real row
    assert _wallet_balance(CUSTOMER_USER_ID) == (82, 0)


def test_wallet_balance_overflow_is_rejected_not_500(client: TestClient) -> None:
    """A balance already at the int4 ceiling plus one more credit must answer
    a stable 400 — never a NumericValueOutOfRange-turned-500 (PR #54 review
    P2: the amount_fen guard alone cannot see the wallet-side overflow)."""
    admin = _admin_session(client)
    # Park the wallet at the int4 ceiling; credits=1 passes every earlier
    # check (amount_fen = 1 × 1000 well inside the ledger range) and only the
    # wallet-side addition would overflow.
    with psycopg.connect(_t23_dsn(), autocommit=True) as conn:
        conn.execute(
            "UPDATE wallets SET available_credits = 2147483647 WHERE user_id = %s",
            (CUSTOMER_USER_ID,),
        )
    response = _create_adjustment(client, admin, credits=1, key="adj-walloverflow-1")
    assert response.status_code == 400, response.text
    assert response.json()["detail"]["code"] == "ADJUSTMENT_VALIDATION_FAILED"
    # The refused write left nothing behind: the balance is untouched and the
    # whole transaction rolled back (no order, no CHARGE, no audit row).
    assert _wallet_balance(CUSTOMER_USER_ID) == (2147483647, 0)
    order_count = _fetch_one(
        "SELECT COUNT(*) FROM recharge_orders "
        "WHERE provider = 'admin_adjustment' AND merchant_order_no NOT LIKE 'OPENING-%%' "
        "AND merchant_order_no NOT LIKE 'DUMMY-%%'"
    )
    assert order_count is not None and int(order_count[0]) == 0
    assert _adjustment_audit_rows(CUSTOMER_USER_ID) == []


# ---------------------------------------------------------------------------
# The admin write contract (dev doc §15)
# ---------------------------------------------------------------------------


def test_missing_idempotency_key_is_rejected(client: TestClient) -> None:
    admin = _admin_session(client)
    headers = dict(admin)
    response = client.post(
        _adjustment_path(CUSTOMER_USER_ID),
        json={
            "confirm": True,
            "reason": "客服补偿",
            "credits": 1,
            "source_document_type": "CS_TICKET",
            "source_document_ref": "T-1",
        },
        headers=headers,
    )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"


def test_missing_confirmation_is_rejected(client: TestClient) -> None:
    admin = _admin_session(client)
    response = _create_adjustment(client, admin, confirm=False, key="adj-confirm-1")
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "CONFIRMATION_REQUIRED"
    assert _wallet_balance(CUSTOMER_USER_ID) == (50, 0)


def test_missing_reason_is_rejected(client: TestClient) -> None:
    admin = _admin_session(client)
    response = _create_adjustment(client, admin, reason="   ", key="adj-reason-1")
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "REASON_REQUIRED"


def test_auditor_cannot_adjust_but_can_read(client: TestClient) -> None:
    auditor = _admin_session(client, actor="auditor_u")
    response = _create_adjustment(client, auditor, credits=1, key="adj-auditor-1")
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "AUDITOR_READ_ONLY"
    assert _wallet_balance(CUSTOMER_USER_ID) == (50, 0)

    listing = client.get(_adjustment_path(CUSTOMER_USER_ID), headers=auditor)
    assert listing.status_code == 200, listing.text


def test_unauthenticated_write_is_rejected(client: TestClient) -> None:
    response = client.post(
        _adjustment_path(CUSTOMER_USER_ID),
        json={
            "confirm": True,
            "reason": "客服补偿",
            "credits": 1,
            "source_document_type": "CS_TICKET",
            "source_document_ref": "T-1",
        },
        headers={IDEMPOTENCY_KEY_HEADER: "adj-anon-1"},
    )
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# Business validation
# ---------------------------------------------------------------------------


def test_nonpositive_credits_is_rejected(client: TestClient) -> None:
    admin = _admin_session(client)
    for credits in (0, -3):
        response = _create_adjustment(client, admin, credits=credits, key=f"adj-cr-{credits}")
        assert response.status_code == 400, response.text
        assert response.json()["detail"]["code"] == "ADJUSTMENT_VALIDATION_FAILED"
    assert _wallet_balance(CUSTOMER_USER_ID) == (50, 0)


def test_oversized_credits_would_overflow_is_rejected(client: TestClient) -> None:
    """A credit count whose derived amount overflows the int4 ledger is refused."""
    admin = _admin_session(client)
    response = _create_adjustment(client, admin, credits=10_000_000, key="adj-overflow-1")
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "ADJUSTMENT_VALIDATION_FAILED"


def test_unknown_user_is_rejected(client: TestClient) -> None:
    admin = _admin_session(client)
    response = _create_adjustment(client, admin, user_id="ghost_u", key="adj-ghost-1")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "USER_NOT_FOUND"


def test_user_without_wallet_is_rejected(client: TestClient) -> None:
    admin = _admin_session(client)
    response = _create_adjustment(client, admin, user_id=WALLETS_USER_ID, key="adj-nowallet-1")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "WALLET_NOT_FOUND"


def test_invalid_source_document_is_rejected(client: TestClient) -> None:
    admin = _admin_session(client)
    bad_type = _create_adjustment(
        client, admin, source_document_type="SCRAP_OF_PAPER", key="adj-sdt-1"
    )
    assert bad_type.status_code == 400
    assert bad_type.json()["detail"]["code"] == "ADJUSTMENT_VALIDATION_FAILED"

    blank_ref = _create_adjustment(client, admin, source_document_ref="   ", key="adj-sdr-1")
    assert blank_ref.status_code == 400
    assert blank_ref.json()["detail"]["code"] == "ADJUSTMENT_VALIDATION_FAILED"
    assert _wallet_balance(CUSTOMER_USER_ID) == (50, 0)


# ---------------------------------------------------------------------------
# Idempotency (revision 031 snapshot layer)
# ---------------------------------------------------------------------------


def test_same_key_replays_once_without_double_charging(client: TestClient) -> None:
    admin = _admin_session(client)
    first = _create_adjustment(client, admin, credits=4, key="adj-replay-1")
    assert first.status_code == 201, first.text
    second = _create_adjustment(client, admin, credits=4, key="adj-replay-1")
    assert second.status_code == 201, second.text
    assert second.headers.get(REPLAY_HEADER) == "true"
    assert second.json() == first.json()

    assert _wallet_balance(CUSTOMER_USER_ID) == (54, 0)  # 50 + 4 exactly once
    assert len(_adjustment_audit_rows(CUSTOMER_USER_ID)) == 1
    order_count = _fetch_one(
        "SELECT COUNT(*) FROM recharge_orders "
        "WHERE provider = 'admin_adjustment' AND merchant_order_no NOT LIKE 'OPENING-%%' "
        "AND merchant_order_no NOT LIKE 'DUMMY-%%'"
    )
    assert order_count is not None and int(order_count[0]) == 1


def test_same_key_conflicting_params_is_rejected(client: TestClient) -> None:
    admin = _admin_session(client)
    first = _create_adjustment(client, admin, credits=4, key="adj-conflict-1")
    assert first.status_code == 201, first.text
    conflicting = _create_adjustment(client, admin, credits=9, key="adj-conflict-1")
    assert conflicting.status_code == 409
    assert conflicting.json()["detail"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert _wallet_balance(CUSTOMER_USER_ID) == (54, 0)


def test_same_key_different_target_user_is_rejected(client: TestClient) -> None:
    """The path parameters are part of the fingerprint (PR #43 review P2)."""
    admin = _admin_session(client)
    first = _create_adjustment(client, admin, credits=4, key="adj-target-1")
    assert first.status_code == 201, first.text
    cross_target = _create_adjustment(
        client, admin, user_id=INTERNAL_USER_ID, credits=4, key="adj-target-1"
    )
    assert cross_target.status_code == 409
    assert cross_target.json()["detail"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert _wallet_balance(INTERNAL_USER_ID) == (10, 0)


def test_failed_write_does_not_burn_the_key(client: TestClient) -> None:
    admin = _admin_session(client)
    missing = _create_adjustment(client, admin, user_id="later_u", credits=1, key="adj-retry-1")
    assert missing.status_code == 404, missing.text
    # The failed write rolled its placeholder back: the same key now works
    # once the target user exists.
    with psycopg.connect(_t23_dsn(), autocommit=True) as conn:
        conn.execute(
            "INSERT INTO users (id, username, display_name, role) "
            "VALUES ('later_u', 'later_u', 'Later', 'user')"
        )
        conn.execute(
            "INSERT INTO wallets (user_id, available_credits, reserved_credits) "
            "VALUES ('later_u', 0, 0)"
        )
    retried = _create_adjustment(client, admin, user_id="later_u", credits=1, key="adj-retry-1")
    assert retried.status_code == 201, retried.text
    assert _wallet_balance("later_u") == (1, 0)


def test_half_committed_placeholder_answers_409_not_500(client: TestClient) -> None:
    """A committed placeholder whose response snapshot never landed (the
    envelope's malformed state) must answer 409 on key reuse — never a
    TypeError-turned-500 (the PR review P3)."""
    from app.admin_activation_routes import _idempotency_key_digest, _request_hash
    from app.admin_customer_routes import AdjustmentRequest

    admin = _admin_session(client)
    key = "adj-halfcommit-1"
    body = AdjustmentRequest(
        confirm=True,
        reason="客服补偿",
        credits=3,
        source_document_type="CS_TICKET",
        source_document_ref="T-HALF",
    )
    route = "POST /api/control/customers/{user_id}/adjustments"
    request_hash = _request_hash(route, {"user_id": CUSTOMER_USER_ID}, body)
    with psycopg.connect(_t23_dsn(), autocommit=True) as conn:
        conn.execute(
            "INSERT INTO admin_write_idempotency "
            "(id, actor_user_id, route, idempotency_key_digest, request_hash) "
            "VALUES (%s, %s, %s, %s, %s)",
            (
                "placeholder-halfcommit",
                "admin_u",
                route,
                _idempotency_key_digest(key),
                request_hash,
            ),
        )

    replay = _create_adjustment(
        client, admin, credits=3, source_document_ref="T-HALF", reason="客服补偿", key=key
    )
    assert replay.status_code == 409, replay.text
    assert replay.json()["detail"]["code"] == "IDEMPOTENCY_CONFLICT"
    # No adjustment landed behind the refused replay.
    assert _wallet_balance(CUSTOMER_USER_ID) == (50, 0)


# ---------------------------------------------------------------------------
# Fail-closed runtime
# ---------------------------------------------------------------------------


def test_missing_pg_runtime_fails_closed(monkeypatch: pytest.MonkeyPatch, route_state: str) -> None:
    """No database configuration at all → 503, never a partial answer.

    The admin session dependency is T12's contract (its own fail-closed
    lane lives in test_admin_auth.py): it is stubbed here so the request
    reaches the adjustment route body, whose PG guard is the unit under
    test (the T18 customer-route fail-closed precedent).
    """
    from app import admin_auth_routes
    from app.admin_auth_routes import AdminActor
    from app.admin_auth_routes import router as admin_auth_router
    from app.admin_customer_routes import router as admin_customer_router

    app = FastAPI()
    app.include_router(admin_auth_router)
    app.include_router(admin_customer_router)
    monkeypatch.setenv(ADMIN_SESSION_HMAC_KEY_ENV, TEST_ADMIN_SESSION_KEY)
    monkeypatch.delenv(DATABASE_URL_ENV, raising=False)
    monkeypatch.delenv("VIDEO_REPLICA_DB_PATH", raising=False)
    monkeypatch.delenv("VIDEO_REPLICA_CUSTOMER_PRODUCTION", raising=False)
    monkeypatch.delenv("VIDEO_REPLICA_AUTH_MODE", raising=False)
    monkeypatch.delenv("VIDEO_REPLICA_ALLOW_DEV_IDENTITY_HEADER", raising=False)
    stub_actor = AdminActor(
        user_id="admin_u",
        username="admin_u",
        display_name="Admin User",
        role="admin",
        session_id="sess-nopg",
        session_expires_at="2099-01-01T00:00:00+00:00",
        last_activity_at="2026-01-01T00:00:00+00:00",
    )

    def _stub_actor(request: Request) -> AdminActor:  # noqa: ARG001
        return stub_actor

    monkeypatch.setattr(admin_auth_routes, "get_admin_actor", _stub_actor)
    close_pg_pool()
    with TestClient(app, raise_server_exceptions=False) as test_client:
        response = _create_adjustment(test_client, {}, credits=1, key="adj-nopg-1")
    assert response.status_code == 503, response.text
    assert response.json()["detail"]["code"] == "ADJUSTMENT_SERVICE_UNAVAILABLE"


# ---------------------------------------------------------------------------
# The audit listing (read path)
# ---------------------------------------------------------------------------


def test_list_adjustments_returns_audit_trail(client: TestClient) -> None:
    admin = _admin_session(client)
    first = _create_adjustment(
        client, admin, credits=1, source_document_ref="T-A", key="adj-list-1"
    )
    second = _create_adjustment(
        client,
        admin,
        credits=2,
        source_document_type="COMPENSATION_APPROVAL",
        source_document_ref="COMP-B",
        key="adj-list-2",
    )
    assert first.status_code == 201 and second.status_code == 201

    listing = client.get(_adjustment_path(CUSTOMER_USER_ID), headers=admin)
    assert listing.status_code == 200, listing.text
    payload = listing.json()
    items = payload["items"]
    assert len(items) == 2
    by_ref = {item["source_document_ref"]: item for item in items}
    assert set(by_ref) == {"T-A", "COMP-B"}
    assert by_ref["T-A"]["credits"] == 1
    assert by_ref["COMP-B"]["credits"] == 2
    assert by_ref["T-A"]["amount_fen"] == BASE_UNIT_PRICE_FEN
    assert by_ref["COMP-B"]["amount_fen"] == 2 * BASE_UNIT_PRICE_FEN
    assert by_ref["T-A"]["admin_user_id"] == "admin_u"
    assert by_ref["T-A"]["source_document_type"] == "CS_TICKET"
    assert by_ref["COMP-B"]["source_document_type"] == "COMPENSATION_APPROVAL"
    for item in items:
        assert item["status"] == "PAID"
        assert item["request_id"]
        assert item["adjustment_id"]
        assert item["order_id"]

    # Pagination is bounded.
    paged = client.get(
        _adjustment_path(CUSTOMER_USER_ID),
        params={"limit": 1, "offset": 1},
        headers=admin,
    )
    assert paged.status_code == 200, paged.text
    assert len(paged.json()["items"]) == 1
    assert paged.json()["items"][0]["source_document_ref"] == "COMP-B"


def test_list_adjustments_for_unknown_user_is_empty(client: TestClient) -> None:
    admin = _admin_session(client)
    listing = client.get(_adjustment_path("ghost_u"), headers=admin)
    assert listing.status_code == 200
    assert listing.json()["items"] == []
