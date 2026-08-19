from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.auth import get_database
from app.db import connect_database, initialize_database
from app.main import app


@pytest.fixture()
def wallet_db_path(tmp_path: Path) -> Iterator[Path]:
    path = tmp_path / "wallet-routes.db"
    with initialize_database(path) as conn:
        conn.executemany(
            "INSERT INTO users (id, username, display_name, role) VALUES (?, ?, ?, 'employee')",
            [
                ("user_1", "user_1", "User One"),
                ("user_2", "user_2", "User Two"),
            ],
        )
        conn.executemany(
            """
            INSERT INTO wallets (user_id, available_credits, reserved_credits)
            VALUES (?, ?, ?)
            """,
            [("user_1", 7, 2), ("user_2", 99, 0)],
        )
        for index, user_id in enumerate(["user_1", "user_1", "user_1", "user_2"], start=1):
            order_id = f"order_{index}"
            conn.execute(
                """
                INSERT INTO recharge_orders (
                    id, user_id, merchant_order_no, channel,
                    base_unit_price_fen_snapshot, charged_unit_price_fen_snapshot,
                    min_recharge_fen_snapshot, recharge_step_fen_snapshot,
                    amount_fen, credits
                ) VALUES (?, ?, ?, 'alipay', 1000, 1000, 10000, 1000, 10000, 10)
                """,
                (order_id, user_id, f"20260819{index:024d}"),
            )
            conn.execute(
                """
                INSERT INTO wallet_transactions (
                    id, user_id, type, available_delta, reserved_delta,
                    recharge_order_id, idempotency_key, created_at
                ) VALUES (?, ?, 'CHARGE', 10, 0, ?, ?, ?)
                """,
                (
                    f"transaction_{index}",
                    user_id,
                    order_id,
                    f"charge:{index}",
                    f"2026-08-19 10:00:0{index}",
                ),
            )
        conn.commit()
    yield path


@pytest.fixture()
def wallet_client(wallet_db_path: Path) -> Iterator[TestClient]:
    def database_override() -> Iterator[sqlite3.Connection]:
        conn = connect_database(wallet_db_path)
        try:
            yield conn
        finally:
            conn.close()

    app.dependency_overrides[get_database] = database_override
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def test_wallet_returns_only_the_authenticated_users_balance(wallet_client: TestClient) -> None:
    response = wallet_client.get("/api/wallet", headers={"X-Dev-User-Id": "user_1"})

    assert response.status_code == 200
    assert response.json() == {"available_credits": 7, "reserved_credits": 2}


def test_wallet_transactions_are_owner_scoped_and_paginated(wallet_client: TestClient) -> None:
    first_page = wallet_client.get(
        "/api/wallet/transactions?limit=2&offset=0",
        headers={"X-Dev-User-Id": "user_1"},
    )
    second_page = wallet_client.get(
        "/api/wallet/transactions?limit=2&offset=2",
        headers={"X-Dev-User-Id": "user_1"},
    )

    assert first_page.status_code == 200
    assert first_page.json()["total"] == 3
    assert first_page.json()["limit"] == 2
    assert first_page.json()["offset"] == 0
    assert [item["id"] for item in first_page.json()["items"]] == [
        "transaction_3",
        "transaction_2",
    ]
    assert second_page.status_code == 200
    assert [item["id"] for item in second_page.json()["items"]] == ["transaction_1"]
    assert all(item["user_id"] == "user_1" for item in first_page.json()["items"])
    assert all(item["id"] != "transaction_4" for item in first_page.json()["items"])


def test_wallet_pagination_rejects_unbounded_limits(wallet_client: TestClient) -> None:
    response = wallet_client.get(
        "/api/wallet/transactions?limit=101",
        headers={"X-Dev-User-Id": "user_1"},
    )

    assert response.status_code == 422
