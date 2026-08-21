from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.auth import get_database
from app.db import alembic_config, connect_database, initialize_database
from app.internal_accounts import create_user, issue_token
from app.main import app
from app.settings import SettingsRepository
from app.zpay import sign_zpay_params


def test_zpay_provider_migration_is_reversible(tmp_path: Path) -> None:
    db_path = tmp_path / "zpay-migration.db"
    with initialize_database(db_path) as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone()[0] == (
            "025_postgres_runtime_compatibility"
        )

    command.downgrade(alembic_config(db_path), "022_internal_billing")
    with connect_database(db_path) as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone()[0] == (
            "022_internal_billing"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO provider_settings (provider, encrypted_config) VALUES ('zpay', 'x')"
            )

    command.upgrade(alembic_config(db_path), "head")
    with connect_database(db_path) as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone()[0] == (
            "025_postgres_runtime_compatibility"
        )


@pytest.fixture()
def recharge_api(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[TestClient, Path, dict[str, str]]]:
    db_path = tmp_path / "recharge.db"
    settings_key = Fernet.generate_key().decode("ascii")
    monkeypatch.setenv("VIDEO_REPLICA_SETTINGS_KEY", settings_key)
    monkeypatch.setenv("VIDEO_REPLICA_AUTH_MODE", "internal")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://video.example")
    monkeypatch.setenv("ZPAY_GATEWAY_URL", "https://zpayz.cn/submit.php")

    with initialize_database(db_path) as conn:
        user = create_user(
            conn,
            username="operator_1",
            display_name="Operator One",
            user_id="user_1",
        )
        token = issue_token(conn, user_id=str(user["user_id"]), raw_token="test-token")
        SettingsRepository(conn).save_zpay_config(
            {
                "pid": "merchant-123",
                "key": "merchant-secret",
                "enabled_channels": "alipay,wxpay",
            },
            actor_user_id=str(user["user_id"]),
        )

    def override_database() -> Iterator[sqlite3.Connection]:
        with connect_database(db_path) as conn:
            yield conn

    app.dependency_overrides[get_database] = override_database
    try:
        yield TestClient(app), db_path, {"Authorization": f"Bearer {token['token']}"}
    finally:
        app.dependency_overrides.clear()


@pytest.mark.parametrize(
    ("amount_fen", "expected_credits"),
    [(10000, 10), (11000, 11), (20000, 20)],
)
def test_create_recharge_order_builds_server_owned_zpay_form(
    recharge_api: tuple[TestClient, Path, dict[str, str]],
    amount_fen: int,
    expected_credits: int,
) -> None:
    client, db_path, headers = recharge_api

    response = client.post(
        "/api/recharge-orders",
        headers=headers,
        json={"amount_fen": amount_fen},
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["status"] == "PENDING"
    assert payload["amount_fen"] == amount_fen
    assert payload["credits"] == expected_credits
    assert payload["gateway_url"] == "https://zpayz.cn/submit.php"
    assert payload["method"] == "POST"
    assert payload["form_fields"] == {
        "pid": "merchant-123",
        "type": "alipay",
        "out_trade_no": payload["order_no"],
        "notify_url": "https://video.example/api/payments/zpay/notify",
        "return_url": "https://video.example/api/payments/zpay/return",
        "name": f"内部视频生成条数充值 {expected_credits} 条",
        "money": f"{amount_fen // 100}.{amount_fen % 100:02d}",
        "sign": sign_zpay_params(payload["form_fields"], "merchant-secret"),
        "sign_type": "MD5",
    }
    assert payload["order_no"].isdigit()
    assert len(payload["order_no"]) <= 32
    assert "merchant-secret" not in response.text

    with connect_database(db_path) as conn:
        row = conn.execute(
            """
            SELECT user_id, merchant_order_no, provider_trade_no, channel, status,
                   pricing_scope, base_unit_price_fen_snapshot,
                   charged_unit_price_fen_snapshot, min_recharge_fen_snapshot,
                   recharge_step_fen_snapshot, amount_fen, credits
            FROM recharge_orders
            WHERE merchant_order_no = ?
            """,
            (payload["order_no"],),
        ).fetchone()
    assert dict(row) == {
        "user_id": "user_1",
        "merchant_order_no": payload["order_no"],
        "provider_trade_no": None,
        "channel": "alipay",
        "status": "PENDING",
        "pricing_scope": "INTERNAL",
        "base_unit_price_fen_snapshot": 1000,
        "charged_unit_price_fen_snapshot": 1000,
        "min_recharge_fen_snapshot": 10000,
        "recharge_step_fen_snapshot": 1000,
        "amount_fen": amount_fen,
        "credits": expected_credits,
    }


@pytest.mark.parametrize("amount_fen", [9900, 10100, "10000", 10000.0, True])
def test_create_recharge_order_rejects_invalid_amounts(
    recharge_api: tuple[TestClient, Path, dict[str, str]],
    amount_fen: object,
) -> None:
    client, db_path, headers = recharge_api

    response = client.post(
        "/api/recharge-orders",
        headers=headers,
        json={"amount_fen": amount_fen},
    )

    assert response.status_code == 422
    with connect_database(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM recharge_orders").fetchone()[0] == 0


def test_recharge_order_list_is_owner_scoped_and_paginated(
    recharge_api: tuple[TestClient, Path, dict[str, str]],
) -> None:
    client, db_path, headers = recharge_api
    first = client.post("/api/recharge-orders", headers=headers, json={"amount_fen": 10000})
    second = client.post("/api/recharge-orders", headers=headers, json={"amount_fen": 20000})
    assert first.status_code == 201
    assert second.status_code == 201

    with connect_database(db_path) as conn:
        create_user(
            conn,
            username="operator_2",
            display_name="Operator Two",
            user_id="user_2",
        )
        conn.execute(
            """
            INSERT INTO recharge_orders (
                id, user_id, merchant_order_no, channel, status, pricing_scope,
                base_unit_price_fen_snapshot, charged_unit_price_fen_snapshot,
                min_recharge_fen_snapshot, recharge_step_fen_snapshot,
                amount_fen, credits
            ) VALUES (
                'other_order', 'user_2', '202608199999999999999999999999',
                'alipay', 'PENDING', 'INTERNAL', 1000, 1000, 10000, 1000, 10000, 10
            )
            """
        )
        conn.commit()

    page = client.get(
        "/api/recharge-orders?limit=1&offset=0",
        headers=headers,
    )
    next_page = client.get(
        "/api/recharge-orders?limit=1&offset=1",
        headers=headers,
    )

    assert page.status_code == 200
    assert page.json()["total"] == 2
    assert page.json()["limit"] == 1
    assert page.json()["offset"] == 0
    assert len(page.json()["items"]) == 1
    assert len(next_page.json()["items"]) == 1
    assert {
        page.json()["items"][0]["order_no"],
        next_page.json()["items"][0]["order_no"],
    } == {first.json()["order_no"], second.json()["order_no"]}
    assert "202608199999999999999999999999" not in page.text + next_page.text

    assert (
        client.get(
            "/api/recharge-orders?limit=101",
            headers=headers,
        ).status_code
        == 422
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("credits", 999),
        ("charged_unit_price_fen_snapshot", 1),
        ("merchant_order_no", "1"),
        ("notify_url", "https://evil.example/notify"),
        ("return_url", "https://evil.example/return"),
        ("pid", "attacker"),
        ("gateway_url", "https://evil.example/submit.php"),
        ("type", "wxpay"),
    ],
)
def test_create_recharge_order_rejects_client_owned_fields(
    recharge_api: tuple[TestClient, Path, dict[str, str]],
    field: str,
    value: object,
) -> None:
    client, db_path, headers = recharge_api

    response = client.post(
        "/api/recharge-orders",
        headers=headers,
        json={"amount_fen": 10000, field: value},
    )

    assert response.status_code == 422
    with connect_database(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM recharge_orders").fetchone()[0] == 0


@pytest.mark.parametrize(
    ("environment_name", "value"),
    [
        ("PUBLIC_BASE_URL", "https://video.example?tenant=forged"),
        ("PUBLIC_BASE_URL", "https://:443"),
        ("ZPAY_GATEWAY_URL", "https://evil.example/submit.php"),
        ("ZPAY_GATEWAY_URL", "https://zpayz.cn/submit.php?redirect=evil"),
    ],
)
def test_create_recharge_order_rejects_unsafe_deployment_urls(
    recharge_api: tuple[TestClient, Path, dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
    environment_name: str,
    value: str,
) -> None:
    client, db_path, headers = recharge_api
    monkeypatch.setenv(environment_name, value)

    response = client.post(
        "/api/recharge-orders",
        headers=headers,
        json={"amount_fen": 10000},
    )

    assert response.status_code == 503
    with connect_database(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM recharge_orders").fetchone()[0] == 0


def test_create_recharge_order_retries_a_merchant_order_number_collision(
    recharge_api: tuple[TestClient, Path, dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _, headers = recharge_api
    order_numbers = iter(["1" * 32, "1" * 32, "2" * 32])
    monkeypatch.setattr(
        "app.recharge_routes.generate_merchant_order_no",
        lambda: next(order_numbers),
    )

    first = client.post("/api/recharge-orders", headers=headers, json={"amount_fen": 10000})
    second = client.post("/api/recharge-orders", headers=headers, json={"amount_fen": 10000})

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["order_no"] == "1" * 32
    assert second.json()["order_no"] == "2" * 32


def test_create_recharge_order_requires_encrypted_merchant_settings(
    recharge_api: tuple[TestClient, Path, dict[str, str]],
) -> None:
    client, db_path, headers = recharge_api
    with connect_database(db_path) as conn:
        conn.execute("DELETE FROM provider_settings WHERE provider = 'zpay'")

    response = client.post(
        "/api/recharge-orders",
        headers=headers,
        json={"amount_fen": 10000},
    )

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "ZPAY_CONFIGURATION_INVALID"
    with connect_database(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM recharge_orders").fetchone()[0] == 0


def test_zpay_config_is_encrypted_and_key_is_masked(
    recharge_api: tuple[TestClient, Path, dict[str, str]],
) -> None:
    _, db_path, _ = recharge_api

    with connect_database(db_path) as conn:
        encrypted = str(
            conn.execute(
                "SELECT encrypted_config FROM provider_settings WHERE provider = 'zpay'"
            ).fetchone()["encrypted_config"]
        )
        masked = SettingsRepository(conn).read_zpay_config()

    assert "merchant-secret" not in encrypted
    assert masked == {
        "provider": "zpay",
        "configured": True,
        "config": {
            "pid": "merchant-123",
            "key": "********cret",
            "enabled_channels": "alipay,wxpay",
        },
    }
    assert "merchant-secret" not in json.dumps(masked)
