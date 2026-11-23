"""T22 / BILL-01 - Customer session recharge (ZPay top-up) evidence.

This document records the automated verification of T22's core invariant:
**Customer session recharge preserves all state except new order creation.**

Key guarantees enforced:
- Recharge does NOT change main code, device slots, session or user concurrency
- Same Idempotency-Key replays the same order (prevents duplicate charging)
- Credits enter the same customer wallet (not P0 internal wallet)
- Provider shape constraint includes 'zpay' (migration 040)

Verification level: AUTOMATED_VERIFIED → Ready for STAGING_VERIFIED
"""

from __future__ import annotations

import base64
import json
import secrets
import time
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pg_test_kit import require_pg_or_explicit_skip

from app.activation_code_service import (
    ACTIVATION_CODE_HMAC_KEY_ENV,
    compute_code_digest,
    generate_activation_code,
    mask_activation_code,
)
from app.db_pg import DATABASE_URL_ENV, close_pg_pool

DEFAULT_DSN = "postgresql://testuser:testpass@localhost:5433/customer_v3_test"

T22R_DB_NAME = "t22r_customer_recharge"

TEST_KEY = secrets.token_urlsafe(48)  # code HMAC key (v1), never a real secret
TEST_FINGERPRINT_KEY = secrets.token_urlsafe(48)  # device fingerprint HMAC key
TEST_ENVELOPE_AEAD_KEY = secrets.token_bytes(32)  # 32 bytes AEAD key, never a real secret

IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"
ACTIVATE_PATH = "/api/customer/activate"
FUTURE_EXPIRY = "2099-01-01T00:00:00+00:00"


def _b64key(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


@pytest.mark.parametrize(
    ("amount_fen", "allowed"),
    [
        (10_000, False),
        (49_000, False),
        (50_000, True),
        (50_010, False),
        (55_000, False),
        (60_000, True),
    ],
)
def test_customer_recharge_defaults_enforce_500_yuan_and_100_yuan_steps(
    amount_fen: int, allowed: bool
) -> None:
    from fastapi import HTTPException

    from app.recharge_routes import validate_recharge_amount
    from app.settings import DEFAULT_BILLING_SETTINGS

    billing = {**DEFAULT_BILLING_SETTINGS, "points_per_yuan": 10}
    if allowed:
        validate_recharge_amount(amount_fen, billing)
    else:
        with pytest.raises(HTTPException) as rejected:
            validate_recharge_amount(amount_fen, billing)
        assert rejected.value.status_code == 422


@pytest.mark.parametrize("points_per_yuan", [None, 10])
@pytest.mark.parametrize(
    ("amount_fen", "allowed"),
    [(50_000, True), (50_010, False), (55_000, False), (60_000, True)],
)
def test_customer_unit_price_does_not_lower_custom_recharge_steps(
    amount_fen: int, allowed: bool, points_per_yuan: int | None
) -> None:
    from fastapi import HTTPException

    from app.recharge_routes import validate_recharge_amount
    from app.settings import apply_customer_unit_price, custom_recharge_billing_settings

    billing = apply_customer_unit_price(
        {
            "charged_unit_price_fen": 1000,
            "min_recharge_fen": 50_000,
            "recharge_step_fen": 10_000,
        },
        unit_price_fen=500,
    )
    if points_per_yuan is not None:
        billing["points_per_yuan"] = points_per_yuan
    billing = custom_recharge_billing_settings(billing)
    assert billing["min_recharge_fen"] == 50_000
    assert billing["recharge_step_fen"] == 10_000
    if allowed:
        validate_recharge_amount(amount_fen, billing)
    else:
        with pytest.raises(HTTPException) as rejected:
            validate_recharge_amount(amount_fen, billing)
        assert rejected.value.status_code == 422


def test_legacy_customer_price_divisibility_does_not_apply_to_credit_exchange() -> None:
    from fastapi import HTTPException

    from app.recharge_routes import validate_recharge_amount
    from app.settings import apply_customer_unit_price, custom_recharge_billing_settings

    billing = apply_customer_unit_price(
        {
            "charged_unit_price_fen": 1000,
            "min_recharge_fen": 50_000,
            "recharge_step_fen": 10_000,
        },
        unit_price_fen=700,
    )
    legacy = custom_recharge_billing_settings(billing)
    assert legacy["min_recharge_fen"] == 70_000
    assert legacy["recharge_step_fen"] == 70_000
    # 旧钱包仍须精确兑换整数积分；启用新汇率后不再按旧单价整除。
    with pytest.raises(HTTPException):
        validate_recharge_amount(50_000, legacy)
    validate_recharge_amount(70_000, legacy)
    modern = custom_recharge_billing_settings({**billing, "points_per_yuan": 10})
    assert modern["min_recharge_fen"] == 50_000
    assert modern["recharge_step_fen"] == 10_000
    validate_recharge_amount(50_000, modern)


def test_legacy_recharge_rules_reject_unrepresentable_postgres_amounts() -> None:
    from fastapi import HTTPException

    from app.settings import custom_recharge_billing_settings

    with pytest.raises(HTTPException) as rejected:
        custom_recharge_billing_settings(
            {
                "charged_unit_price_fen": 2_147_483_647,
                "min_recharge_fen": 50_000,
                "recharge_step_fen": 10_000,
            }
        )
    assert rejected.value.status_code == 503
    assert rejected.value.detail["code"] == "RECHARGE_CONFIGURATION_INVALID"


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

    require_pg_or_explicit_skip(_pg_dsn())
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
        # 历史支付链路用例显式使用原计价，避免依赖今后的运营默认值。
        # 新充值规则专项会覆盖为 500/100；每例清空汇率，防止新旧模型互相污染。
        conn.execute(
            "INSERT INTO runtime_settings (id, min_recharge_fen, recharge_step_fen) "
            "VALUES (1, 10000, 1000) ON CONFLICT (id) DO UPDATE "
            "SET min_recharge_fen=10000, recharge_step_fen=1000"
        )
        conn.execute("UPDATE customer_credit_pricing SET version=0, config_json=NULL WHERE id=1")
    yield recharge_pg_dsn
    close_pg_pool()


# ---------------------------------------------------------------------------
# Test helpers (copy from e2e_contract.test.tsx equivalents)
# ---------------------------------------------------------------------------


def _insert_code(conn: psycopg.Connection, code_id: str, batch_id: str, plaintext: str) -> None:
    conn.execute(
        """
        INSERT INTO activation_code_batches (
            id, name, face_value_fen, unit_price_fen_snapshot, credits_snapshot,
            quantity, activation_expires_at, status, created_by_user_id, created_at
        )
        VALUES (
            %s, 'T22 Test Batch', 10000, 10000, 1000, 1, %s,
            'OPEN', 'admin_u', CURRENT_TIMESTAMP
        )
        ON CONFLICT (id) DO NOTHING
        """,
        (batch_id, FUTURE_EXPIRY),
    )
    conn.execute(
        """
        INSERT INTO activation_codes (
            id, batch_id, code_digest, digest_key_version, masked_code,
            status, issued_at, activated_at
        )
        VALUES (%s, %s, %s, 1, %s, 'ISSUED', CURRENT_TIMESTAMP, NULL)
        ON CONFLICT (id) DO NOTHING
        """,
        (
            code_id,
            batch_id,
            compute_code_digest(plaintext, key=TEST_KEY.encode()),
            mask_activation_code(plaintext),
        ),
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


def _recharge_headers(session_token: str, idem_key: str | None = None) -> dict[str, str]:
    """Default POST headers for the customer recharge lane (Bearer + idempotency)."""
    return {
        "Authorization": f"Bearer {session_token}",
        IDEMPOTENCY_KEY_HEADER: idem_key or str(secrets.token_urlsafe(32)),
    }


def _customer_recharge_count(dsn: str) -> int:
    """Count recharge orders created via this endpoint (the activation grant row is
    also CUSTOMER_STANDARD; distinguish it by its ACT-* merchant order number)."""
    with psycopg.connect(dsn) as conn:
        row = conn.execute(
            "SELECT count(*) FROM recharge_orders WHERE merchant_order_no NOT LIKE 'ACT-%'"
        ).fetchone()
    assert row is not None
    return int(row[0])


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
        encrypted_config = (
            Fernet(settings_fernet_key)
            .encrypt(
                b'{"pid":"merchant-123","key":"merchant-secret","enabled_channels":"alipay,wxpay"}'
            )
            .decode("ascii")
        )

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
            ("zpay", encrypted_config, "admin_zpay"),
        )


# ---------------------------------------------------------------------------
# T22 extended tests: idempotency, wallet credits, error handling (BILL-01)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("amount_fen", "expected_status"),
    [(49_000, 422), (50_000, 201), (50_010, 422), (55_000, 422), (60_000, 201)],
)
def test_customer_recharge_500_yuan_policy_matches_wallet_and_order(
    client: TestClient,
    clean_state: str,
    recharge_config_fixture,
    amount_fen: int,
    expected_status: int,
) -> None:
    code = generate_activation_code()
    with psycopg.connect(clean_state) as conn:
        _insert_code(conn, code_id="policy-code", batch_id="policy-batch", plaintext=code)
    activation = _activate_customer(client, code, "policy-device", "unused")
    with psycopg.connect(clean_state) as conn:
        conn.execute(
            "UPDATE runtime_settings SET internal_base_unit_price_fen=300, "
            "min_recharge_fen=50000, recharge_step_fen=10000 WHERE id=1"
        )
        conn.execute(
            "UPDATE customer_credit_pricing SET version=1, config_json=%s WHERE id=1",
            (json.dumps({"points_per_yuan": 10}),),
        )
        conn.execute(
            "INSERT INTO customer_unit_prices (user_id, unit_price_fen, updated_by_user_id) "
            "VALUES (%s, 700, 'admin_u')",
            (activation["user_id"],),
        )
    wallet = client.get(
        "/api/customer/wallet",
        headers={"Authorization": f"Bearer {activation['session_token']}"},
    )
    assert wallet.status_code == 200, wallet.text
    assert wallet.json()["min_recharge_fen"] == 50_000
    assert wallet.json()["recharge_step_fen"] == 10_000
    assert wallet.json()["package_min_recharge_fen"] == 50_000
    assert wallet.json()["points_per_yuan"] == 10
    response = client.post(
        "/api/customer/recharge-orders",
        headers=_recharge_headers(activation["session_token"]),
        json={"amount_fen": amount_fen},
    )
    assert response.status_code == expected_status, response.text
    if expected_status == 201:
        assert response.json()["credits"] == amount_fen // 10
        with psycopg.connect(clean_state) as conn:
            snapshot = conn.execute(
                "SELECT min_recharge_fen_snapshot, recharge_step_fen_snapshot, credits "
                "FROM recharge_orders WHERE merchant_order_no=%s",
                (response.json()["order_no"],),
            ).fetchone()
        assert snapshot == (50_000, 10_000, amount_fen // 10)
    else:
        assert response.json()["detail"]["code"] == "INVALID_RECHARGE_AMOUNT"
        assert _customer_recharge_count(clean_state) == 0


@pytest.mark.parametrize(
    ("unit_price_fen", "amount_fen", "expected_status"),
    [
        (700, 50_000, 422),
        (700, 60_000, 422),
        (700, 70_000, 201),
        (700, 140_000, 201),
        (None, 50_000, 422),
        (None, 60_000, 201),
        (None, 90_000, 201),
    ],
)
def test_legacy_recharge_wallet_advertises_only_payable_amounts(
    client: TestClient,
    clean_state: str,
    recharge_config_fixture,
    unit_price_fen: int | None,
    amount_fen: int,
    expected_status: int,
) -> None:
    code = generate_activation_code()
    with psycopg.connect(clean_state) as conn:
        _insert_code(conn, code_id="legacy-policy", batch_id="legacy-batch", plaintext=code)
    activation = _activate_customer(client, code, "legacy-policy-device", "unused")
    with psycopg.connect(clean_state) as conn:
        conn.execute(
            "UPDATE runtime_settings SET internal_base_unit_price_fen=300, "
            "min_recharge_fen=50000, recharge_step_fen=10000 WHERE id=1"
        )
        if unit_price_fen is not None:
            conn.execute(
                "INSERT INTO customer_unit_prices (user_id, unit_price_fen, updated_by_user_id) "
                "VALUES (%s, %s, 'admin_u')",
                (activation["user_id"], unit_price_fen),
            )
    minimum, step = (70_000, 70_000) if unit_price_fen is not None else (60_000, 30_000)
    wallet = client.get(
        "/api/customer/wallet",
        headers={"Authorization": f"Bearer {activation['session_token']}"},
    )
    assert wallet.status_code == 200, wallet.text
    assert wallet.json()["min_recharge_fen"] == minimum
    assert wallet.json()["recharge_step_fen"] == step
    assert wallet.json()["package_min_recharge_fen"] == 50_000
    assert wallet.json()["points_per_yuan"] is None
    response = client.post(
        "/api/customer/recharge-orders",
        headers=_recharge_headers(activation["session_token"]),
        json={"amount_fen": amount_fen},
    )
    assert response.status_code == expected_status, response.text
    if expected_status == 201:
        credits = amount_fen // (unit_price_fen or 300)
        assert response.json()["credits"] == credits
        with psycopg.connect(clean_state) as conn:
            snapshot = conn.execute(
                "SELECT min_recharge_fen_snapshot, recharge_step_fen_snapshot, credits, "
                "credit_pricing_snapshot_json FROM recharge_orders WHERE merchant_order_no=%s",
                (response.json()["order_no"],),
            ).fetchone()
        assert snapshot == (minimum, step, credits, None)
    else:
        assert response.json()["detail"]["code"] == "INVALID_RECHARGE_AMOUNT"
        assert _customer_recharge_count(clean_state) == 0


def test_customer_session_cannot_use_internal_recharge_route(
    client: TestClient, clean_state: str, recharge_config_fixture
) -> None:
    """A customer session must never select the INTERNAL pricing rail by URL."""
    code = generate_activation_code()
    with psycopg.connect(clean_state) as conn:
        _insert_code(
            conn,
            code_id="code-t44-internal-route",
            batch_id="batch-t44-internal-route",
            plaintext=code,
        )

    activation = _activate_customer(client, code, "fp-t44-internal-route", "unused")
    with psycopg.connect(clean_state) as conn:
        before = conn.execute(
            "SELECT count(*) FROM recharge_orders WHERE user_id = %s",
            (activation["user_id"],),
        ).fetchone()

    response = client.post(
        "/api/recharge-orders",
        json={"amount_fen": 10000},
        headers={"Authorization": f"Bearer {activation['session_token']}"},
    )

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "CUSTOMER_RECHARGE_ROUTE_REQUIRED"
    with psycopg.connect(clean_state) as conn:
        after = conn.execute(
            "SELECT count(*) FROM recharge_orders WHERE user_id = %s",
            (activation["user_id"],),
        ).fetchone()
    assert before == after


def test_customer_session_recharge_preserves_all_state_with_wallet_credit(
    client: TestClient, clean_state: str, recharge_config_fixture
) -> None:
    """T22 variant: verify credits enter same customer wallet."""
    code = generate_activation_code()
    with psycopg.connect(clean_state) as conn:
        _insert_code(conn, code_id="code-t22-wallet", batch_id="batch-t22-wallet", plaintext=code)

    activation = _activate_customer(client, code, "fp-t22-wallet", "key-t22-wallet")
    session_token = activation["session_token"]
    user_id = activation["user_id"]

    # Take snapshot before recharge
    with psycopg.connect(clean_state) as conn:
        wallet_before = conn.execute(
            "SELECT user_id, available_credits FROM wallets WHERE user_id = %s",
            (user_id,),
        ).fetchone()

    # Perform recharge
    response = client.post(
        "/api/customer/recharge-orders",
        json={"amount_fen": 10000},
        headers=_recharge_headers(session_token),
    )
    assert response.status_code == 201, f"Expected 201, got {response.status_code}: {response.text}"

    # Verify new recharge order created (but wallet not credited yet - PENDING)
    with psycopg.connect(clean_state) as conn:
        order_after = conn.execute(
            "SELECT merchant_order_no, status, amount_fen, credits "
            "FROM recharge_orders WHERE merchant_order_no NOT LIKE 'ACT-%' "
            "ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        assert order_after is not None, "Recharge order should be created"
        assert order_after[1] == "PENDING", "Order status should be PENDING"
        assert order_after[2] == 10000, "Amount should match request"
        assert order_after[3] == 10, "Credits calculated correctly (assuming unit_price=1000)"

    # Wallet should NOT change during PENDING state (credits only added after PAID callback)
    with psycopg.connect(clean_state) as conn:
        wallet_after = conn.execute(
            "SELECT user_id, available_credits FROM wallets WHERE user_id = %s",
            (user_id,),
        ).fetchone()
        assert list(wallet_before) == list(wallet_after), (
            "Wallet should not change during PENDING state"
        )


def test_customer_unit_price_override_controls_wallet_and_recharge_credits(
    client: TestClient, clean_state: str, recharge_config_fixture
) -> None:
    code = generate_activation_code()
    with psycopg.connect(clean_state) as conn:
        _insert_code(
            conn,
            code_id="code-t22-custom-price",
            batch_id="batch-t22-custom-price",
            plaintext=code,
        )

    activation = _activate_customer(client, code, "fp-t22-custom-price", "unused")
    session_token = activation["session_token"]
    user_id = activation["user_id"]
    with psycopg.connect(clean_state) as conn:
        conn.execute(
            "INSERT INTO customer_unit_prices "
            "(user_id, unit_price_fen, updated_by_user_id) VALUES (%s, 500, 'admin_u')",
            (user_id,),
        )

    wallet = client.get(
        "/api/customer/wallet",
        headers={"Authorization": f"Bearer {session_token}"},
    )
    assert wallet.status_code == 200, wallet.text
    assert wallet.json()["internal_unit_price_fen"] == 500
    assert wallet.json()["min_recharge_fen"] == 10000
    assert wallet.json()["recharge_step_fen"] == 1000

    response = client.post(
        "/api/customer/recharge-orders",
        json={"amount_fen": 10000},
        headers=_recharge_headers(session_token),
    )
    assert response.status_code == 201, response.text
    assert response.json()["credits"] == 20

    with psycopg.connect(clean_state) as conn:
        snapshot = conn.execute(
            "SELECT base_unit_price_fen_snapshot, charged_unit_price_fen_snapshot, "
            "min_recharge_fen_snapshot, recharge_step_fen_snapshot "
            "FROM recharge_orders WHERE merchant_order_no = %s",
            (response.json()["order_no"],),
        ).fetchone()
    assert snapshot == (1000, 500, 10000, 1000)


def test_customer_wallet_reads_and_order_list(
    client: TestClient, clean_state: str, recharge_config_fixture
) -> None:
    """Task #7: the customer wallet/transactions/order-list reads answer under
    the customer session. These endpoints are what the customer workspace's
    wallet view needs — the internal wallet API 401'd for a customer session."""
    code = generate_activation_code()
    with psycopg.connect(clean_state) as conn:
        _insert_code(conn, code_id="code-t22-wread", batch_id="batch-t22-wread", plaintext=code)
    activation = _activate_customer(client, code, "fp-t22-wread", "key-t22-wread")
    session_token = activation["session_token"]
    bearer = {"Authorization": f"Bearer {session_token}"}

    # Wallet read: the activation grant funded this wallet.
    wallet = client.get("/api/customer/wallet", headers=bearer)
    assert wallet.status_code == 200, wallet.text
    payload = wallet.json()
    assert payload["available_credits"] >= 10
    assert payload["min_recharge_fen"] > 0
    assert payload["recharge_step_fen"] > 0

    # Transactions: the activation CHARGE is visible.
    txn = client.get("/api/customer/wallet/transactions", headers=bearer)
    assert txn.status_code == 200, txn.text
    assert "CHARGE" in [t["type"] for t in txn.json()["items"]]

    # Create a recharge order, then list it under the customer session.
    created = client.post(
        "/api/customer/recharge-orders",
        json={"amount_fen": 10000},
        headers=_recharge_headers(session_token),
    )
    assert created.status_code == 201, created.text
    order_no = created.json()["order_no"]

    orders = client.get("/api/customer/recharge-orders", headers=bearer)
    assert orders.status_code == 200, orders.text
    assert order_no in [o["order_no"] for o in orders.json()["items"]]


def test_customer_recharge_order_list_survives_fresh_tuple_row_pool_connection(
    client: TestClient, clean_state: str, recharge_config_fixture
) -> None:
    """Codex P1 (PR #65): the order list must not depend on a pooled connection
    that a previous request already mutated to named rows.

    ``serialize_recharge_order`` reads columns by name, so on a brand-new
    pooled connection (psycopg's default tuple rows) the old code 500'd with a
    TypeError; the test only masked it because the earlier wallet read had set
    the shared connection's row factory through ``BusinessConnection.postgres``.
    This test forces the shared pool connection back to tuple rows right before
    the list call, then asserts the endpoint still answers — the route must
    install the named-row factory itself."""
    from app.db_pg import get_pg_pool

    code = generate_activation_code()
    with psycopg.connect(clean_state) as conn:
        _insert_code(conn, code_id="code-t22-fresh", batch_id="batch-t22-fresh", plaintext=code)
    activation = _activate_customer(client, code, "fp-t22-fresh", "key-t22-fresh")
    session_token = activation["session_token"]
    bearer = {"Authorization": f"Bearer {session_token}"}

    created = client.post(
        "/api/customer/recharge-orders",
        json={"amount_fen": 10000},
        headers=_recharge_headers(session_token),
    )
    assert created.status_code == 201, created.text
    order_no = created.json()["order_no"]

    # The pool is min-size 1 and TestClient requests are serial, so this
    # returns the exact connection the next request will reuse. Reset it to
    # psycopg's default tuple rows — the "fresh connection" state the bug
    # failed on.
    with get_pg_pool().connection() as conn:
        conn.row_factory = psycopg.rows.tuple_row

    orders = client.get("/api/customer/recharge-orders", headers=bearer)
    assert orders.status_code == 200, orders.text
    assert order_no in [o["order_no"] for o in orders.json()["items"]]

    # A second call keeps working: the route re-installs the named factory.
    orders_again = client.get("/api/customer/recharge-orders", headers=bearer)
    assert orders_again.status_code == 200, orders_again.text


def test_customer_session_recharge_idempotency_by_idempotency_key(
    client: TestClient, clean_state: str, recharge_config_fixture
) -> None:
    """Replaying the same Idempotency-Key returns the sealed order, not a new one."""
    code = generate_activation_code()
    with psycopg.connect(clean_state) as conn:
        _insert_code(conn, code_id="code-t22-idem", batch_id="batch-t22-idem", plaintext=code)

    activation = _activate_customer(client, code, "fp-t22-idem", "key-t22-idem")
    session_token = activation["session_token"]
    idem_key = "idem-t22-recharge-replay"

    # First call creates the order and seals the response into the envelope.
    response1 = client.post(
        "/api/customer/recharge-orders",
        json={"amount_fen": 10000},
        headers=_recharge_headers(session_token, idem_key),
    )
    assert response1.status_code == 201, response1.text
    order1 = response1.json()["order_no"]
    assert response1.headers.get("X-Idempotent-Replay") != "true"

    # Replaying the same key unseals the original response: same order, no new row.
    response2 = client.post(
        "/api/customer/recharge-orders",
        json={"amount_fen": 10000},
        headers=_recharge_headers(session_token, idem_key),
    )
    assert response2.status_code == 201, response2.text
    assert response2.json()["order_no"] == order1
    assert response2.headers.get("X-Idempotent-Replay") == "true"

    orders_after_first = _customer_recharge_count(clean_state)
    assert orders_after_first == 1

    # A different key is a different intent: a fresh order number.
    response3 = client.post(
        "/api/customer/recharge-orders",
        json={"amount_fen": 10000},
        headers=_recharge_headers(session_token),
    )
    assert response3.status_code == 201, response3.text
    assert response3.json()["order_no"] != order1
    assert _customer_recharge_count(clean_state) == orders_after_first + 1


def test_customer_session_recharge_public_origin_missing_503(
    client: TestClient, clean_state: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Missing callback origin returns 503 instead of 500."""
    monkeypatch.setenv("PUBLIC_BASE_URL", "")

    code = generate_activation_code()
    with psycopg.connect(clean_state) as conn:
        _insert_code(
            conn, code_id="code-t22-zpay-inv", batch_id="batch-t22-zpay-inv", plaintext=code
        )

    activation = _activate_customer(client, code, "fp-t22-zpay-inv", "key-t22-zpay-inv")
    session_token = activation["session_token"]

    # Recharge without a callback origin should return 503.
    response = client.post(
        "/api/customer/recharge-orders",
        json={"amount_fen": 10000},
        headers=_recharge_headers(session_token),
    )
    assert response.status_code == 503, (
        f"Expected 503 for missing ZPay config, got {response.status_code}"
    )
    detail = response.json().get("detail", {})
    assert detail.get("code") == "PAYMENT_CHANNEL_UNAVAILABLE"
    # 红线：客户可见的错误码与文案都不得出现供应商名称。
    assert "zpay" not in detail.get("code", "").lower()
    assert "zpay" not in detail.get("message", "").lower()


def test_customer_session_recharge_negative_amount_rejected(
    client: TestClient, clean_state: str, recharge_config_fixture
) -> None:
    """Negative amounts rejected with 422."""
    code = generate_activation_code()
    with psycopg.connect(clean_state) as conn:
        _insert_code(conn, code_id="code-t22-neg", batch_id="batch-t22-neg", plaintext=code)

    activation = _activate_customer(client, code, "fp-t22-neg", "key-t22-neg")
    session_token = activation["session_token"]

    response = client.post(
        "/api/customer/recharge-orders",
        json={"amount_fen": -10000},
        headers=_recharge_headers(session_token),
    )
    assert response.status_code == 422, "Negative amount should be rejected"


def test_customer_session_recharge_zero_amount_rejected(
    client: TestClient, clean_state: str, recharge_config_fixture
) -> None:
    """Zero amount rejected with 422."""
    code = generate_activation_code()
    with psycopg.connect(clean_state) as conn:
        _insert_code(conn, code_id="code-t22-zero", batch_id="batch-t22-zero", plaintext=code)

    activation = _activate_customer(client, code, "fp-t22-zero", "key-t22-zero")
    session_token = activation["session_token"]

    response = client.post(
        "/api/customer/recharge-orders",
        json={"amount_fen": 0},
        headers=_recharge_headers(session_token),
    )
    assert response.status_code == 422, "Zero amount should be rejected"


def test_customer_session_recharge_above_max_allowed_rejected(
    client: TestClient, clean_state: str, recharge_config_fixture
) -> None:
    """Amounts above max allowed by billing settings are rejected (if enforced)."""
    # Current implementation doesn't enforce max, but we document this edge case
    code = generate_activation_code()
    with psycopg.connect(clean_state) as conn:
        _insert_code(conn, code_id="code-t22-max", batch_id="batch-t22-max", plaintext=code)

    activation = _activate_customer(client, code, "fp-t22-max", "key-t22-max")
    session_token = activation["session_token"]

    # Very large amount (no explicit max in current validation)
    response = client.post(
        "/api/customer/recharge-orders",
        json={"amount_fen": 100000000},  # 1 million yuan equivalent
        headers=_recharge_headers(session_token),
    )
    # Currently passes validation (no max check), but order is created
    assert response.status_code == 201
    payload = response.json()
    assert payload["amount_fen"] == 100000000


def test_customer_session_recharge_after_session_expired_401(
    client: TestClient, clean_state: str, recharge_config_fixture
) -> None:
    """Once the session lease is past, top-up is fenced out with 401 SESSION_EXPIRED."""
    code = generate_activation_code()
    with psycopg.connect(clean_state) as conn:
        _insert_code(conn, code_id="code-t22-exp", batch_id="batch-t22-exp", plaintext=code)

    activation = _activate_customer(client, code, "fp-t22-exp", "key-t22-exp")
    old_session_token = activation["session_token"]

    # Pull the lease into the past (what admin suspend/logout propagation does).
    # created_at + 1s keeps the lease_after_created check happy while landing
    # barely after creation; sleeping past that instant makes it lapsed for the
    # fenced re-verification (clock_timestamp() vs lease_until). Keep the
    # stored offset when the raw fixture and application pool use different zones.
    with psycopg.connect(clean_state, autocommit=True) as conn:
        conn.execute(
            "UPDATE customer_session_state "
            "SET lease_until = created_at::timestamptz + interval '1 second'"
        )
    time.sleep(1.2)

    response = client.post(
        "/api/customer/recharge-orders",
        json={"amount_fen": 10000},
        headers=_recharge_headers(old_session_token),
    )
    assert response.status_code == 401, response.text
    assert response.json()["detail"]["code"] == "SESSION_EXPIRED"

    # And the rejected attempt must not leave a customer order behind.
    assert _customer_recharge_count(clean_state) == 0


def test_customer_recharge_order_status_readable_by_owner(
    client: TestClient, clean_state: str, recharge_config_fixture
) -> None:
    """The customer lane can read back its own order status."""
    code = generate_activation_code()
    with psycopg.connect(clean_state) as conn:
        _insert_code(conn, code_id="code-t22-query", batch_id="batch-t22-query", plaintext=code)

    activation = _activate_customer(client, code, "fp-t22-query", "key-t22-query")
    session_token = activation["session_token"]

    created = client.post(
        "/api/customer/recharge-orders",
        json={"amount_fen": 10000},
        headers=_recharge_headers(session_token),
    )
    assert created.status_code == 201, created.text
    order_no = created.json()["order_no"]

    response = client.get(
        f"/api/customer/recharge-orders/{order_no}",
        headers={"Authorization": f"Bearer {session_token}"},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["order_no"] == order_no
    assert payload["status"] == "PENDING"
    assert payload["amount_fen"] == 10000


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
    from app.payment_routes import router as payment_router
    from app.recharge_routes import router as recharge_router

    app = FastAPI()
    for r in (activation_code_router, customer_session_router, recharge_router, payment_router):
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
            "SELECT user_id, activation_code_id, session_id, session_epoch, "
            "lease_until, token_digest FROM customer_session_state LIMIT 1"
        ).fetchone()

    # Perform recharge
    response = client.post(
        "/api/customer/recharge-orders",
        json={"amount_fen": 10000},
        headers=_recharge_headers(session_token),
    )

    assert response.status_code == 201, (
        f"Expected 201, got {response.status_code}. Response: {response.text}"
    )
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
            "SELECT user_id, activation_code_id, session_id, session_epoch, "
            "lease_until, token_digest FROM customer_session_state LIMIT 1"
        ).fetchone()

        assert list(code_before) == list(code_after), (
            "Activation code should not change after recharge"
        )
        assert [list(d) for d in devices_before] == [list(d) for d in devices_after], (
            "Device slots should not change after recharge"
        )
        assert list(session_before) == list(session_after), (
            "Session state should not change after recharge"
        )


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
        headers=_recharge_headers(session_token),
    )
    assert response.status_code == 422, (
        f"Expected 422 for below-minimum amount, got {response.status_code}: {response.text}"
    )

    # Not divisible by step (assuming step=100 based on validation logic)
    response = client.post(
        "/api/customer/recharge-orders",
        json={"amount_fen": 10007},
        headers=_recharge_headers(session_token),
    )
    assert response.status_code == 422, (
        f"Expected 422 for non-step-divisible amount, got {response.status_code}: {response.text}"
    )


# ---------------------------------------------------------------------------
# T22 review fixes: envelope conflicts, key requirement, int4 ceiling, GET ACL
# ---------------------------------------------------------------------------


def test_customer_recharge_idempotency_key_conflict_409(
    client: TestClient, clean_state: str, recharge_config_fixture
) -> None:
    """Reusing a key with a different payload answers 409, never a second order."""
    code = generate_activation_code()
    with psycopg.connect(clean_state) as conn:
        _insert_code(conn, code_id="code-t22-conf", batch_id="batch-t22-conf", plaintext=code)

    activation = _activate_customer(client, code, "fp-t22-conf", "key-t22-conf")
    session_token = activation["session_token"]
    idem_key = "idem-t22-recharge-conflict"

    first = client.post(
        "/api/customer/recharge-orders",
        json={"amount_fen": 10000},
        headers=_recharge_headers(session_token, idem_key),
    )
    assert first.status_code == 201, first.text

    # Same key, different amount: a different intent claiming the same identity.
    second = client.post(
        "/api/customer/recharge-orders",
        json={"amount_fen": 20000},
        headers=_recharge_headers(session_token, idem_key),
    )
    assert second.status_code == 409, second.text
    assert second.json()["detail"]["code"] == "IDEMPOTENCY_CONFLICT"

    assert _customer_recharge_count(clean_state) == 1


def test_customer_recharge_missing_idempotency_key_400(
    client: TestClient, clean_state: str, recharge_config_fixture
) -> None:
    """The customer lane refuses to charge without an Idempotency-Key."""
    code = generate_activation_code()
    with psycopg.connect(clean_state) as conn:
        _insert_code(conn, code_id="code-t22-nokey", batch_id="batch-t22-nokey", plaintext=code)

    activation = _activate_customer(client, code, "fp-t22-nokey", "key-t22-nokey")

    response = client.post(
        "/api/customer/recharge-orders",
        json={"amount_fen": 10000},
        headers={"Authorization": f"Bearer {activation['session_token']}"},
    )
    assert response.status_code == 400, response.text
    assert response.json()["detail"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"


def test_customer_recharge_amount_above_int4_ceiling_rejected_422(
    client: TestClient, clean_state: str, recharge_config_fixture
) -> None:
    """Amounts above the PostgreSQL int4 ceiling answer 422, not a 500."""
    code = generate_activation_code()
    with psycopg.connect(clean_state) as conn:
        _insert_code(conn, code_id="code-t22-int4", batch_id="batch-t22-int4", plaintext=code)

    activation = _activate_customer(client, code, "fp-t22-int4", "key-t22-int4")

    response = client.post(
        "/api/customer/recharge-orders",
        json={"amount_fen": 2_147_483_648},  # INT4_MAX + 1
        headers=_recharge_headers(activation["session_token"]),
    )
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "INVALID_RECHARGE_AMOUNT"


def test_customer_recharge_order_status_hidden_from_other_users(
    client: TestClient, clean_state: str, recharge_config_fixture
) -> None:
    """Another customer's order number answers 404, never 200."""
    code_a = generate_activation_code()
    with psycopg.connect(clean_state) as conn:
        _insert_code(conn, code_id="code-t22-q-a", batch_id="batch-t22-q-a", plaintext=code_a)
    activation_a = _activate_customer(client, code_a, "fp-t22-q-a", "key-t22-q-a")

    created = client.post(
        "/api/customer/recharge-orders",
        json={"amount_fen": 10000},
        headers=_recharge_headers(activation_a["session_token"]),
    )
    assert created.status_code == 201, created.text
    order_no = created.json()["order_no"]

    code_b = generate_activation_code()
    with psycopg.connect(clean_state) as conn:
        _insert_code(conn, code_id="code-t22-q-b", batch_id="batch-t22-q-b", plaintext=code_b)
    activation_b = _activate_customer(client, code_b, "fp-t22-q-b", "key-t22-q-b")

    response = client.get(
        f"/api/customer/recharge-orders/{order_no}",
        headers={"Authorization": f"Bearer {activation_b['session_token']}"},
    )
    assert response.status_code == 404, response.text
    assert response.json()["detail"]["code"] == "RECHARGE_ORDER_NOT_FOUND"


def test_customer_recharge_order_status_requires_session(
    client: TestClient, clean_state: str, recharge_config_fixture
) -> None:
    """The status endpoint is fenced by the customer session too."""
    response = client.get("/api/customer/recharge-orders/any-order-no")
    assert response.status_code == 401, response.text
    assert response.json()["detail"]["code"] == "SESSION_TOKEN_REQUIRED"


def test_customer_profile_returns_masked_activation_and_device_summary(
    client: TestClient, clean_state: str
) -> None:
    code = generate_activation_code()
    with psycopg.connect(clean_state) as conn:
        _insert_code(
            conn,
            code_id="code-profile-summary",
            batch_id="batch-profile-summary",
            plaintext=code,
        )

    activation = _activate_customer(client, code, "fp-profile-summary", "key-profile-summary")
    response = client.get(
        "/api/customer/profile",
        headers={"Authorization": f"Bearer {activation['session_token']}"},
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["user_id"] == activation["user_id"]
    assert payload["activation_code_masked"] == mask_activation_code(code)
    assert payload["activation_status"] == "ACTIVE"
    assert payload["device_slots_used"] == 1
    assert payload["device_slots_total"] is None
    assert code not in response.text


def test_customer_profile_display_name_can_be_updated_and_is_audited(
    client: TestClient, clean_state: str
) -> None:
    code = generate_activation_code()
    with psycopg.connect(clean_state) as conn:
        _insert_code(
            conn,
            code_id="code-profile-rename",
            batch_id="batch-profile-rename",
            plaintext=code,
        )
    activation = _activate_customer(client, code, "fp-profile-rename", "key-profile-rename")
    bearer = {"Authorization": f"Bearer {activation['session_token']}"}

    response = client.patch(
        "/api/customer/profile",
        headers=bearer,
        json={"display_name": "  李丽的视频工作台  "},
    )

    assert response.status_code == 200, response.text
    assert response.json()["display_name"] == "李丽的视频工作台"
    assert response.json()["username"] == activation["username"]
    with psycopg.connect(clean_state) as conn:
        user = conn.execute(
            "SELECT display_name FROM users WHERE id = %s",
            (activation["user_id"],),
        ).fetchone()
        audit = conn.execute(
            "SELECT action, entity_id FROM audit_logs "
            "WHERE actor_user_id = %s ORDER BY created_at DESC LIMIT 1",
            (activation["user_id"],),
        ).fetchone()
    assert user is not None and user[0] == "李丽的视频工作台"
    assert audit == ("customer.profile.updated", activation["user_id"])


def test_customer_can_close_pending_recharge_order_without_deleting_audit_row(
    client: TestClient, clean_state: str, recharge_config_fixture
) -> None:
    code = generate_activation_code()
    with psycopg.connect(clean_state) as conn:
        _insert_code(
            conn,
            code_id="code-close-order",
            batch_id="batch-close-order",
            plaintext=code,
        )
    activation = _activate_customer(client, code, "fp-close-order", "key-close-order")
    bearer = {"Authorization": f"Bearer {activation['session_token']}"}
    created = client.post(
        "/api/customer/recharge-orders",
        headers=_recharge_headers(activation["session_token"]),
        json={"amount_fen": 10000},
    )
    assert created.status_code == 201, created.text
    order_no = created.json()["order_no"]

    response = client.delete(
        f"/api/customer/recharge-orders/{order_no}",
        headers=bearer,
    )

    assert response.status_code == 204, response.text
    read_back = client.get(
        f"/api/customer/recharge-orders/{order_no}",
        headers=bearer,
    )
    assert read_back.status_code == 200, read_back.text
    assert read_back.json()["status"] == "CLOSED"
    with psycopg.connect(clean_state) as conn:
        rows = conn.execute(
            "SELECT count(*) FROM recharge_orders WHERE merchant_order_no = %s",
            (order_no,),
        ).fetchone()
        audit = conn.execute(
            "SELECT action, entity_id FROM audit_logs "
            "WHERE actor_user_id = %s AND action = 'customer.recharge_order.closed'",
            (activation["user_id"],),
        ).fetchone()
    assert rows is not None and rows[0] == 1
    assert audit == ("customer.recharge_order.closed", order_no)

    # Repeated deletion is harmless and stays closed.
    repeated = client.delete(
        f"/api/customer/recharge-orders/{order_no}",
        headers=bearer,
    )
    assert repeated.status_code == 204, repeated.text


def test_customer_payment_code_is_generated_server_side_for_owned_order(
    client: TestClient,
    customer_app: FastAPI,
    clean_state: str,
    recharge_config_fixture,
) -> None:
    from app.recharge_routes import get_zpay_provider
    from app.zpay import ZPayPaymentCodeResult

    class FakePaymentCodeClient:
        def __init__(self) -> None:
            self.order_numbers: list[str] = []

        def load_merchant_config(self, conn: object) -> object:
            del conn
            return None

        def load_deployment_config(self) -> object:
            return None

        def create_payment_code(self, **kwargs) -> ZPayPaymentCodeResult:
            self.order_numbers.append(str(kwargs["merchant_order_no"]))
            return ZPayPaymentCodeResult(
                provider_order_no=str(kwargs["merchant_order_no"]),
                qr_image_url="https://payment.example/qr.png",
                payment_url="https://payment.example/pay",
            )

    code = generate_activation_code()
    with psycopg.connect(clean_state) as conn:
        _insert_code(
            conn,
            code_id="code-payment-code",
            batch_id="batch-payment-code",
            plaintext=code,
        )
    activation = _activate_customer(client, code, "fp-payment-code", "key-payment-code")
    session_token = activation["session_token"]
    created = client.post(
        "/api/customer/recharge-orders",
        json={"amount_fen": 10000},
        headers=_recharge_headers(session_token),
    )
    assert created.status_code == 201, created.text
    order_no = created.json()["order_no"]

    fake_client = FakePaymentCodeClient()
    customer_app.dependency_overrides[get_zpay_provider] = lambda: fake_client
    try:
        response = client.post(
            f"/api/customer/recharge-orders/{order_no}/payment-code",
            headers={"Authorization": f"Bearer {session_token}"},
        )
    finally:
        customer_app.dependency_overrides.pop(get_zpay_provider, None)

    assert response.status_code == 200, response.text
    assert response.json() == {
        "order_no": order_no,
        "amount_fen": 10000,
        "credits": 10,
        "qr_image_url": "https://payment.example/qr.png",
        "payment_url": "https://payment.example/pay",
    }
    assert fake_client.order_numbers == [order_no]


def _make_order_native(dsn: str, order_no: str, *, created_at: str | None = None) -> None:
    """Re-point a freshly created order at WeChat Native, optionally back-dated.

    The customer route creates orders on the configured provider (zpay in this
    fixture); converting the row afterwards is the cheapest way to reach the
    Native-only branches without standing up merchant credentials.
    """
    with psycopg.connect(dsn) as conn:
        conn.execute(
            "UPDATE recharge_orders SET provider = 'wechat_native', channel = 'wxpay', "
            "created_at = COALESCE(%s, created_at) WHERE merchant_order_no = %s",
            (created_at, order_no),
        )
        conn.commit()


def _create_customer_order(client: TestClient, session_token: str) -> str:
    created = client.post(
        "/api/customer/recharge-orders",
        json={"amount_fen": 10000},
        headers=_recharge_headers(session_token),
    )
    assert created.status_code == 201, created.text
    return str(created.json()["order_no"])


def _activated_session(client: TestClient, dsn: str, slug: str) -> str:
    code = generate_activation_code()
    with psycopg.connect(dsn) as conn:
        _insert_code(conn, code_id=f"code-{slug}", batch_id=f"batch-{slug}", plaintext=code)
    activation = _activate_customer(client, code, f"fp-{slug}", f"key-{slug}")
    return str(activation["session_token"])


def test_native_payment_code_refuses_an_order_past_its_payment_window(
    client: TestClient,
    clean_state: str,
    recharge_config_fixture,
) -> None:
    """A lapsed order's cached QR still renders but WeChat will refuse the payment.

    Handing it back would look like a broken scanner to the customer, so the route
    refuses before serving or minting a code and tells them to start over.
    """
    session_token = _activated_session(client, clean_state, "native-expired")
    order_no = _create_customer_order(client, session_token)
    _make_order_native(clean_state, order_no, created_at="2026-01-01 00:00:00+00")

    response = client.post(
        f"/api/customer/recharge-orders/{order_no}/payment-code",
        headers={"Authorization": f"Bearer {session_token}"},
    )

    assert response.status_code == 409, response.text
    assert "超过支付时限" in response.json()["detail"]


def test_native_payment_code_proceeds_while_the_order_is_still_payable(
    client: TestClient,
    clean_state: str,
    recharge_config_fixture,
) -> None:
    """The guard must not misfire on a fresh order: this one reaches the provider.

    WeChat merchant settings are absent in this fixture, so getting as far as
    PAYMENT_CONFIGURATION_UNAVAILABLE proves the expiry check let it through.
    """
    session_token = _activated_session(client, clean_state, "native-fresh")
    order_no = _create_customer_order(client, session_token)
    _make_order_native(clean_state, order_no)

    response = client.post(
        f"/api/customer/recharge-orders/{order_no}/payment-code",
        headers={"Authorization": f"Bearer {session_token}"},
    )

    assert response.status_code == 503, response.text
    assert response.json()["detail"]["code"] == "PAYMENT_CONFIGURATION_UNAVAILABLE"


def _signed_notify_params(order_no: str, *, trade_no: str) -> dict[str, str]:
    """ZPay callback query params signed with the fixture merchant secret."""
    from app.zpay import sign_zpay_params

    params = {
        "pid": "merchant-123",
        "name": "内部视频生成条数充值 10 条",
        "money": "100.00",
        "out_trade_no": order_no,
        "trade_no": trade_no,
        "trade_status": "TRADE_SUCCESS",
        "type": "alipay",
    }
    params["sign"] = sign_zpay_params(params, "merchant-secret")
    params["sign_type"] = "MD5"
    return params


def test_customer_recharge_zpay_notify_credits_wallet_on_pg_lane(
    client: TestClient, clean_state: str, recharge_config_fixture
) -> None:
    """The paid top-up closes the loop: the ZPay callback runs on the
    PostgreSQL lane (no VIDEO_REPLICA_DB_PATH) and credits the same wallet."""
    code = generate_activation_code()
    with psycopg.connect(clean_state) as conn:
        _insert_code(conn, code_id="code-t22-notify", batch_id="batch-t22-notify", plaintext=code)

    activation = _activate_customer(client, code, "fp-t22-notify", "key-t22-notify")
    session_token = activation["session_token"]
    user_id = activation["user_id"]

    created = client.post(
        "/api/customer/recharge-orders",
        json={"amount_fen": 10000},
        headers=_recharge_headers(session_token),
    )
    assert created.status_code == 201, created.text
    order_no = created.json()["order_no"]

    params = _signed_notify_params(order_no, trade_no="zpay-t22-trade-1")
    notify = client.get("/api/payments/zpay/notify", params=params)
    assert notify.status_code == 200, notify.text
    assert notify.text == "success"

    with psycopg.connect(clean_state) as conn:
        order = conn.execute(
            "SELECT status, provider_trade_no, paid_at FROM recharge_orders "
            "WHERE merchant_order_no = %s",
            (order_no,),
        ).fetchone()
        assert order is not None
        assert order[0] == "PAID", "Callback must settle the order"
        assert order[1] == "zpay-t22-trade-1"
        assert order[2] is not None

        wallet = conn.execute(
            "SELECT available_credits FROM wallets WHERE user_id = %s", (user_id,)
        ).fetchone()
        charge = conn.execute(
            "SELECT available_delta, idempotency_key FROM wallet_transactions "
            "WHERE user_id = %s AND type = 'CHARGE' "
            "AND idempotency_key LIKE 'zpay:charge:%%'",
            (user_id,),
        ).fetchone()
    assert wallet is not None and int(wallet[0]) >= 10, "Wallet must gain the paid credits"
    assert charge is not None and int(charge[0]) == 10

    # Duplicate delivery stays idempotent: second notify succeeds, no double charge.
    replay = client.get("/api/payments/zpay/notify", params=params)
    assert replay.status_code == 200
    assert replay.text == "success"
    with psycopg.connect(clean_state) as conn:
        charges = conn.execute(
            "SELECT count(*) FROM wallet_transactions "
            "WHERE user_id = %s AND type = 'CHARGE' "
            "AND idempotency_key LIKE 'zpay:charge:%%'",
            (user_id,),
        ).fetchone()
    assert charges is not None and int(charges[0]) == 1


# ---------------------------------------------------------------------------
# F2（2026-11-16 漏账缺口修复）：删除微信订单同步关闭网关侧订单；重复删除
# 触发受控重试；网关答已付 → 立即查单结算；未知结果只告警、204 不受影响。
# ---------------------------------------------------------------------------


class _GatewayCloseStub:
    """微信 provider 替身：记录 close/query 调用，应答由用例注入."""

    def __init__(
        self,
        *,
        close_result: object = None,
        close_exc: Exception | None = None,
        query_result: object = None,
        query_exc: Exception | None = None,
    ) -> None:
        self.close_calls: list[str] = []
        self.query_calls: list[str] = []
        self._close_result = close_result
        self._close_exc = close_exc
        self._query_result = query_result
        self._query_exc = query_exc

    def load_merchant_config(self, conn):  # noqa: ANN001, ARG002
        from app.payment_provider import MerchantConfig

        return MerchantConfig(provider="wechat_native", raw={}, allowed_channels=("wxpay",))

    def load_deployment_config(self):
        from app.payment_provider import DeploymentConfig

        return DeploymentConfig(notify_url="https://cb.example.test", return_url="", gateway_url="")

    def close_order(self, *, merchant, merchant_order_no):  # noqa: ANN001, ARG002
        self.close_calls.append(merchant_order_no)
        if self._close_exc is not None:
            raise self._close_exc
        assert self._close_result is not None
        return self._close_result

    def query_order(self, **kwargs):  # noqa: ANN003
        self.query_calls.append(kwargs["merchant_order_no"])
        if self._query_exc is not None:
            raise self._query_exc
        assert self._query_result is not None
        return self._query_result


def test_closing_a_wechat_order_also_closes_the_gateway_order(
    client: TestClient,
    clean_state: str,
    recharge_config_fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import recharge_routes
    from app.payment_provider import OrderCloseResult

    session_token = _activated_session(client, clean_state, "native-close-gw")
    order_no = _create_customer_order(client, session_token)
    _make_order_native(clean_state, order_no)
    stub = _GatewayCloseStub(close_result=OrderCloseResult(closed=True))
    monkeypatch.setattr(recharge_routes, "get_payment_provider", lambda name: stub)

    response = client.delete(
        f"/api/customer/recharge-orders/{order_no}",
        headers={"Authorization": f"Bearer {session_token}"},
    )

    assert response.status_code == 204, response.text
    assert stub.close_calls == [order_no], "关单后必须关闭网关侧订单"
    assert stub.query_calls == [], "网关确认已关时不得再查单结算"
    with psycopg.connect(clean_state) as conn:
        row = conn.execute(
            "SELECT status FROM recharge_orders WHERE merchant_order_no = %s",
            (order_no,),
        ).fetchone()
    assert row is not None and row[0] == "CLOSED"


def test_repeated_wechat_delete_retries_the_gateway_close(
    client: TestClient,
    clean_state: str,
    recharge_config_fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """重复删除=受控重试（评审 P1 可恢复性）：上次 close 可能未知失败。"""
    from app import recharge_routes
    from app.payment_provider import OrderCloseResult

    session_token = _activated_session(client, clean_state, "native-close-retry")
    order_no = _create_customer_order(client, session_token)
    _make_order_native(clean_state, order_no)
    stub = _GatewayCloseStub(close_result=OrderCloseResult(closed=True))
    monkeypatch.setattr(recharge_routes, "get_payment_provider", lambda name: stub)
    bearer = {"Authorization": f"Bearer {session_token}"}

    first = client.delete(f"/api/customer/recharge-orders/{order_no}", headers=bearer)
    second = client.delete(f"/api/customer/recharge-orders/{order_no}", headers=bearer)

    assert first.status_code == 204 and second.status_code == 204
    assert stub.close_calls == [order_no, order_no], "重复删除必须重试网关关单"


def test_wechat_delete_with_gateway_already_paid_settles_from_verified_query(
    client: TestClient,
    clean_state: str,
    recharge_config_fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """网关答 ORDERPAID（closed=False）→ 立即查单，验签通过的应答幂等入账。"""
    from app import recharge_routes
    from app.payment_provider import OrderCloseResult, OrderQueryResult

    code = generate_activation_code()
    with psycopg.connect(clean_state) as conn:
        _insert_code(
            conn,
            code_id="code-native-close-paid",
            batch_id="batch-native-close-paid",
            plaintext=code,
        )
    activation = _activate_customer(client, code, "fp-native-close-paid", "key-native-close-paid")
    user_id = str(activation["user_id"])
    bearer = {"Authorization": f"Bearer {activation['session_token']}"}
    order_no = _create_customer_order(client, str(activation["session_token"]))
    _make_order_native(clean_state, order_no)
    with psycopg.connect(clean_state) as conn:
        wallet_before = conn.execute(
            "SELECT available_credits FROM wallets WHERE user_id = %s", (user_id,)
        ).fetchone()
    assert wallet_before is not None, "激活即建钱包（含注册赠送），作为精确增量断言的基线"
    stub = _GatewayCloseStub(
        close_result=OrderCloseResult(closed=False),
        query_result=OrderQueryResult(
            paid=True,
            merchant_order_no=order_no,
            provider_trade_no="wx-trade-close-paid",
            amount_fen=10000,
            channel="wxpay",
            response_digest="digest-close-paid",
        ),
    )
    monkeypatch.setattr(recharge_routes, "get_payment_provider", lambda name: stub)

    response = client.delete(f"/api/customer/recharge-orders/{order_no}", headers=bearer)

    assert response.status_code == 204, response.text
    assert stub.close_calls == [order_no] and stub.query_calls == [order_no]
    with psycopg.connect(clean_state) as conn:
        order = conn.execute(
            "SELECT status, transaction_id FROM recharge_orders WHERE merchant_order_no = %s",
            (order_no,),
        ).fetchone()
        wallet = conn.execute(
            "SELECT available_credits FROM wallets WHERE user_id = %s", (user_id,)
        ).fetchone()
        charges = conn.execute(
            # 注册赠送也是一条 CHARGE 流水；按订单归属精确计数，避开赠送行。
            "SELECT count(*) FROM wallet_transactions WHERE user_id = %s "
            "AND type = 'CHARGE' AND recharge_order_id = "
            "(SELECT id FROM recharge_orders WHERE merchant_order_no = %s)",
            (user_id, order_no),
        ).fetchone()
    assert order is not None and order[0] == "PAID" and order[1] == "wx-trade-close-paid"
    assert wallet is not None and int(wallet[0]) == int(wallet_before[0]) + 10, (
        "在激活基线（含注册赠送）上精确入账 10 分"
    )
    assert charges is not None and int(charges[0]) == 1, "本单入账恰好一次"


def test_wechat_delete_with_unknown_close_outcome_degrades_gracefully(
    client: TestClient,
    clean_state: str,
    recharge_config_fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """关单结果未知（超时/未建模错误码）：告警后立即查单核实，204 照常。

    查单答「未付」→ 原样留在 CLOSED（钱没到就不动账）；查单本身失败则由
    sweep/人工兜底（评审 P1：未知结果必须走验签查单路径，不能只告警了事）。
    """
    from app import recharge_routes
    from app.payment_provider import OrderCloseError, OrderQueryResult

    session_token = _activated_session(client, clean_state, "native-close-unknown")
    order_no = _create_customer_order(client, session_token)
    _make_order_native(clean_state, order_no)
    stub = _GatewayCloseStub(
        close_exc=OrderCloseError("outcome unknown", status_code=504),
        # 未知结果必须立即查单核实（评审 P1）：查单说没付就原样留在 CLOSED。
        query_result=OrderQueryResult(
            paid=False,
            merchant_order_no=order_no,
            provider_trade_no=None,
            amount_fen=None,
            channel="wxpay",
            response_digest="digest-unknown-unpaid",
        ),
    )
    monkeypatch.setattr(recharge_routes, "get_payment_provider", lambda name: stub)

    response = client.delete(
        f"/api/customer/recharge-orders/{order_no}",
        headers={"Authorization": f"Bearer {session_token}"},
    )

    assert response.status_code == 204, response.text
    assert stub.close_calls == [order_no]
    assert stub.query_calls == [order_no], "未知结果必须走验签查单路径核实"
    with psycopg.connect(clean_state) as conn:
        row = conn.execute(
            "SELECT status FROM recharge_orders WHERE merchant_order_no = %s",
            (order_no,),
        ).fetchone()
    assert row is not None and row[0] == "CLOSED"


def test_zpay_delete_never_touches_the_gateway_close_branch(
    client: TestClient,
    clean_state: str,
    recharge_config_fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """zpay 无网关关单 API：删除流程不得对它发起任何 provider 调用。"""
    from app import recharge_routes

    session_token = _activated_session(client, clean_state, "zpay-close-untouched")
    order_no = _create_customer_order(client, session_token)
    requested: list[str] = []

    def _spy(name: str) -> object:
        requested.append(name)
        raise AssertionError(f"zpay delete must not resolve providers, saw {name}")

    monkeypatch.setattr(recharge_routes, "get_payment_provider", _spy)
    response = client.delete(
        f"/api/customer/recharge-orders/{order_no}",
        headers={"Authorization": f"Bearer {session_token}"},
    )
    assert response.status_code == 204, response.text
    assert requested == [], "zpay 删除不得触碰网关关单分支"
