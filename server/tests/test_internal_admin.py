from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.auth import get_database
from app.control_auth import CONTROL_ADMIN_USER_ID_ENV, CONTROL_PROXY_TOKEN_DIGEST_ENV
from app.control_routes import _spreadsheet_safe_cell
from app.db import connect_database, initialize_database
from app.internal_accounts import create_user, issue_token
from app.main import app
from app.settings import SETTINGS_KEY_ENV, SettingsRepository

CONTROL_TOKEN = "control-proxy-only-token"


@pytest.mark.parametrize("prefix", ["=", "+", "-", "@", "\t", "\r"])
def test_spreadsheet_cells_with_formula_prefixes_are_escaped(prefix: str) -> None:
    value = f"{prefix}2+2"

    assert _spreadsheet_safe_cell(value) == f"'{value}"


@pytest.fixture()
def internal_admin_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[TestClient, Path, dict[str, str], dict[str, str]]]:
    db_path = tmp_path / "internal-admin.db"
    monkeypatch.setenv(SETTINGS_KEY_ENV, Fernet.generate_key().decode("ascii"))
    monkeypatch.setenv("VIDEO_REPLICA_AUTH_MODE", "internal")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://internal.example")
    monkeypatch.setenv("ZPAY_GATEWAY_URL", "https://zpayz.cn/submit.php")
    monkeypatch.setenv(CONTROL_ADMIN_USER_ID_ENV, "admin_1")
    monkeypatch.setenv(
        CONTROL_PROXY_TOKEN_DIGEST_ENV,
        hashlib.sha256(CONTROL_TOKEN.encode()).hexdigest(),
    )

    with initialize_database(db_path) as conn:
        create_user(
            conn,
            user_id="admin_1",
            username="internal-admin",
            display_name="Internal Admin",
            role="admin",
        )
        create_user(
            conn,
            user_id="user_1",
            username="operator-1",
            display_name="Operator One",
        )
        business_token = issue_token(
            conn,
            user_id="user_1",
            raw_token="business-user-token",
        )
        SettingsRepository(conn).save_zpay_config(
            {
                "pid": "merchant-123",
                "key": "merchant-secret",
                "enabled_channels": "alipay,wxpay",
            },
            actor_user_id="admin_1",
        )
        conn.execute("UPDATE wallets SET available_credits = 10 WHERE user_id = 'user_1'")
        conn.execute(
            """
            INSERT INTO recharge_orders (
                id, user_id, merchant_order_no, channel, status,
                provider_trade_no, pricing_scope,
                base_unit_price_fen_snapshot, charged_unit_price_fen_snapshot,
                min_recharge_fen_snapshot, recharge_step_fen_snapshot,
                amount_fen, credits, paid_at
            ) VALUES (
                'order_paid', 'user_1', '202608190000000000000000000001',
                'alipay', 'PAID', 'zpay-trade-1', 'INTERNAL',
                1000, 1000, 10000, 1000, 10000, 10, CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            INSERT INTO wallet_transactions (
                id, user_id, type, available_delta, reserved_delta,
                recharge_order_id, idempotency_key
            ) VALUES (
                'charge_paid', 'user_1', 'CHARGE', 10, 0,
                'order_paid', 'charge:order_paid'
            )
            """
        )
        conn.execute(
            """
            INSERT INTO recharge_orders (
                id, user_id, merchant_order_no, channel, status, pricing_scope,
                base_unit_price_fen_snapshot, charged_unit_price_fen_snapshot,
                min_recharge_fen_snapshot, recharge_step_fen_snapshot,
                amount_fen, credits
            ) VALUES (
                'order_pending', 'user_1', '202608190000000000000000000002',
                'wxpay', 'PENDING', 'INTERNAL',
                1000, 1000, 10000, 1000, 20000, 20
            )
            """
        )
        conn.commit()

    def override_database() -> Iterator[sqlite3.Connection]:
        with connect_database(db_path) as conn:
            yield conn

    app.dependency_overrides[get_database] = override_database
    try:
        yield (
            TestClient(app),
            db_path,
            {"X-Control-Proxy-Token": CONTROL_TOKEN},
            {"Authorization": f"Bearer {business_token['token']}"},
        )
    finally:
        app.dependency_overrides.clear()


def test_control_accounts_and_orders_are_proxy_only_and_paginated(
    internal_admin_context: tuple[TestClient, Path, dict[str, str], dict[str, str]],
) -> None:
    client, _, control_headers, business_headers = internal_admin_context

    accounts = client.get(
        "/api/control/accounts?limit=1&offset=1",
        headers=control_headers,
    )
    orders = client.get(
        "/api/control/recharge-orders?status=PENDING&limit=10&offset=0",
        headers=control_headers,
    )

    assert accounts.status_code == 200
    assert accounts.json()["total"] == 2
    assert accounts.json()["items"] == [
        {
            "id": "user_1",
            "username": "operator-1",
            "display_name": "Operator One",
            "role": "employee",
            "is_active": True,
            "available_credits": 10,
            "reserved_credits": 0,
            "active_token_count": 1,
        }
    ]
    assert orders.status_code == 200
    assert orders.json()["total"] == 1
    assert orders.json()["items"][0]["order_no"].endswith("2")
    assert orders.json()["items"][0]["username"] == "operator-1"

    assert client.get("/api/control/accounts", headers=business_headers).status_code == 401
    assert client.get("/api/control/accounts", headers={}).status_code == 401
    assert (
        client.get(
            "/api/control/accounts",
            headers={"X-Control-Proxy-Token": "forged"},
        ).status_code
        == 401
    )
    assert (
        client.get(
            "/api/control/accounts?limit=101",
            headers=control_headers,
        ).status_code
        == 422
    )


def test_control_settings_mask_zpay_secret_and_keep_deployment_read_only(
    internal_admin_context: tuple[TestClient, Path, dict[str, str], dict[str, str]],
) -> None:
    client, db_path, control_headers, _ = internal_admin_context

    snapshot = client.get("/api/control/settings", headers=control_headers)

    assert snapshot.status_code == 200
    assert snapshot.json()["zpay"] == {
        "provider": "zpay",
        "configured": True,
        "config": {
            "pid": "merchant-123",
            "key": "********cret",
            "enabled_channels": "alipay,wxpay",
        },
    }
    assert snapshot.json()["billing"]["internal_base_unit_price_fen"] == 1000
    assert snapshot.json()["deployment"] == {
        "gateway_url": "https://zpayz.cn/submit.php",
        "notify_url": "https://internal.example/api/payments/zpay/notify",
        "return_url": "https://internal.example/api/payments/zpay/return",
    }
    assert "merchant-secret" not in snapshot.text

    updated = client.patch(
        "/api/control/settings/zpay",
        headers=control_headers,
        json={
            "pid": "merchant-456",
            "key": "",
            "enabled_channels": ["wxpay"],
        },
    )

    assert updated.status_code == 200
    assert updated.json()["config"]["pid"] == "merchant-456"
    assert updated.json()["config"]["key"] == "********cret"
    with connect_database(db_path) as conn:
        assert SettingsRepository(conn).load_zpay_config()["key"] == "merchant-secret"

    forbidden_field = client.patch(
        "/api/control/settings/zpay",
        headers=control_headers,
        json={
            "pid": "merchant-456",
            "enabled_channels": ["wxpay"],
            "gateway_url": "https://evil.example/submit.php",
        },
    )
    assert forbidden_field.status_code == 422


def test_control_billing_settings_only_update_internal_price_rules(
    internal_admin_context: tuple[TestClient, Path, dict[str, str], dict[str, str]],
) -> None:
    client, db_path, control_headers, _ = internal_admin_context

    updated = client.patch(
        "/api/control/settings/billing",
        headers=control_headers,
        json={
            "internal_base_unit_price_fen": 500,
            "min_recharge_fen": 10000,
            "recharge_step_fen": 1000,
        },
    )

    assert updated.status_code == 200
    assert updated.json() == {
        "internal_base_unit_price_fen": 500,
        "charged_unit_price_fen": 500,
        "min_recharge_fen": 10000,
        "recharge_step_fen": 1000,
    }
    with connect_database(db_path) as conn:
        stored = SettingsRepository(conn).read_billing_settings()
    assert stored == updated.json()

    below_minimum = client.patch(
        "/api/control/settings/billing",
        headers=control_headers,
        json={
            "internal_base_unit_price_fen": 500,
            "min_recharge_fen": 9900,
            "recharge_step_fen": 1000,
        },
    )
    customer_price_field = client.patch(
        "/api/control/settings/billing",
        headers=control_headers,
        json={
            "internal_base_unit_price_fen": 500,
            "charged_unit_price_fen": 1500,
            "min_recharge_fen": 10000,
            "recharge_step_fen": 1000,
        },
    )

    assert below_minimum.status_code == 422
    assert customer_price_field.status_code == 422


def test_control_reconciliation_and_csv_are_read_only(
    internal_admin_context: tuple[TestClient, Path, dict[str, str], dict[str, str]],
) -> None:
    client, db_path, control_headers, _ = internal_admin_context
    with connect_database(db_path) as conn:
        conn.execute("UPDATE users SET username = '=2+2' WHERE id = 'user_1'")
        conn.commit()
        before = (
            conn.execute(
                "SELECT available_credits, reserved_credits FROM wallets WHERE user_id='user_1'"
            ).fetchone(),
            conn.execute("SELECT COUNT(*) FROM wallet_transactions").fetchone()[0],
        )

    summary = client.get("/api/control/billing-reconciliation", headers=control_headers)
    orders_csv = client.get("/api/control/recharge-orders.csv", headers=control_headers)
    ledger_csv = client.get("/api/control/wallet-transactions.csv", headers=control_headers)

    assert summary.status_code == 200
    assert summary.json() == {
        "wallet_count": 2,
        "wallet_mismatch_count": 0,
        "paid_order_without_charge_count": 0,
        "charge_without_paid_order_count": 0,
        "pending_order_count": 1,
    }
    assert orders_csv.status_code == 200
    assert orders_csv.headers["content-type"].startswith("text/csv")
    assert "attachment;" in orders_csv.headers["content-disposition"]
    assert "202608190000000000000000000001" in orders_csv.text
    assert "'=2+2" in orders_csv.text
    assert "merchant-secret" not in orders_csv.text
    assert ledger_csv.status_code == 200
    assert "charge_paid" in ledger_csv.text

    with connect_database(db_path) as conn:
        after = (
            conn.execute(
                "SELECT available_credits, reserved_credits FROM wallets WHERE user_id='user_1'"
            ).fetchone(),
            conn.execute("SELECT COUNT(*) FROM wallet_transactions").fetchone()[0],
        )
    assert tuple(before[0]) == tuple(after[0])
    assert before[1] == after[1]


def test_control_proxy_identity_cannot_be_used_as_a_business_identity(
    internal_admin_context: tuple[TestClient, Path, dict[str, str], dict[str, str]],
) -> None:
    client, _, control_headers, _ = internal_admin_context

    response = client.post(
        "/api/recharge-orders",
        headers=control_headers,
        json={"amount_fen": 10000},
    )

    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "AUTH_TOKEN_REQUIRED"

    project_response = client.post(
        "/api/projects",
        headers=control_headers,
        json={"name": "control-token-must-not-create-projects"},
    )
    assert project_response.status_code == 401
    assert project_response.json()["detail"]["code"] == "AUTH_TOKEN_REQUIRED"

    dev_header_response = client.get(
        "/api/wallet",
        headers={"X-Dev-User-Id": "user_1"},
    )
    assert dev_header_response.status_code == 401
    assert dev_header_response.json()["detail"]["code"] == "AUTH_TOKEN_REQUIRED"


def test_control_routes_do_not_expose_a_wallet_mutation_endpoint() -> None:
    methods_by_path = {
        route.path: route.methods
        for route in app.routes
        if getattr(route, "path", "").startswith("/api/control")
    }

    assert "/api/control/wallets/{user_id}" not in methods_by_path
    assert "/api/control/wallet-adjustments" not in methods_by_path

    openapi_paths = set(app.openapi()["paths"])
    assert {
        "/api/control/accounts",
        "/api/control/recharge-orders",
        "/api/control/wallet-transactions",
        "/api/control/billing-reconciliation",
        "/api/control/settings",
        "/api/control/settings/zpay",
        "/api/control/settings/billing",
        "/api/control/recharge-orders.csv",
        "/api/control/wallet-transactions.csv",
    } <= openapi_paths
    assert "/api/control/wallet-adjustments" not in openapi_paths


def test_nginx_example_requires_both_network_and_basic_auth_and_overwrites_control_header() -> None:
    config_path = Path(__file__).parents[2] / "deploy/nginx/internal-p0.conf.example"
    config = config_path.read_text(encoding="utf-8")
    admin = _nginx_location(config, "location ^~ /admin {")
    control = _nginx_location(config, "location ^~ /api/control/ {")
    notify = _nginx_location(config, "location = /api/payments/zpay/notify {")
    payment_return = _nginx_location(config, "location = /api/payments/zpay/return {")

    for protected in (admin, control):
        assert "satisfy all;" in protected
        assert 'auth_basic "Internal control";' in protected
        assert "allow 10.0.0.0/8;" in protected
        assert "deny all;" in protected

    assert 'proxy_set_header Authorization "";' in control
    assert 'proxy_set_header X-Control-Proxy-Token "REPLACE_WITH_32_BYTE_RANDOM_TOKEN";' in control
    assert "$http_x_control_proxy_token" not in config
    for public_callback in (notify, payment_return):
        assert "auth_basic" not in public_callback
        assert 'proxy_set_header X-Control-Proxy-Token "";' in public_callback


def _nginx_location(config: str, marker: str) -> str:
    start = config.index(marker)
    end = config.index("\n    }", start)
    return config[start:end]
