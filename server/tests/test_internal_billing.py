from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from cryptography.fernet import Fernet

from app.db import alembic_config, connect_database, initialize_database
from app.settings import SettingsRepository

HEAD_REVISION = "025_postgres_runtime_compatibility"


def seed_subjects(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO users (id, username, display_name, role) VALUES (?, ?, ?, ?)",
        ("user_1", "user_1", "User One", "employee"),
    )
    conn.execute(
        "INSERT INTO projects (id, owner_user_id, name) VALUES (?, ?, ?)",
        ("project_1", "user_1", "Project One"),
    )
    conn.execute(
        """
        INSERT INTO generation_batches (
            id, project_id, created_by_user_id, idempotency_key,
            request_hash, request_snapshot_json
        ) VALUES ('batch_1', 'project_1', 'user_1', 'batch-key', 'hash', '{}')
        """
    )
    conn.execute(
        """
        INSERT INTO generation_tasks (id, batch_id, generation_mode, provider, model, status)
        VALUES ('task_1', 'batch_1', 'I2V', 'metaso', 'MiniMax-H3', 'PENDING')
        """
    )
    conn.execute("INSERT INTO wallets (user_id) VALUES ('user_1')")
    conn.commit()


def insert_order(
    conn: sqlite3.Connection,
    order_id: str,
    merchant_order_no: str,
    provider_trade_no: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO recharge_orders (
            id, user_id, merchant_order_no, provider_trade_no, channel,
            base_unit_price_fen_snapshot, charged_unit_price_fen_snapshot,
            min_recharge_fen_snapshot, recharge_step_fen_snapshot, amount_fen, credits
        ) VALUES (?, 'user_1', ?, ?, 'alipay', 1000, 1000, 10000, 1000, 10000, 10)
        """,
        (order_id, merchant_order_no, provider_trade_no),
    )


def insert_transaction(
    conn: sqlite3.Connection,
    transaction_id: str,
    transaction_type: str,
    idempotency_key: str,
    *,
    recharge_order_id: str | None = None,
    task_id: str | None = None,
    billing_round: int | None = 1,
) -> None:
    deltas = {
        "CHARGE": (10, 0),
        "RESERVE": (-1, 1),
        "SETTLE": (0, -1),
        "RELEASE": (1, -1),
    }
    if transaction_type == "CHARGE":
        billing_round = None
    conn.execute(
        """
        INSERT INTO wallet_transactions (
            id, user_id, type, available_delta, reserved_delta,
            recharge_order_id, task_id, billing_round, idempotency_key
        ) VALUES (?, 'user_1', ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            transaction_id,
            transaction_type,
            *deltas[transaction_type],
            recharge_order_id,
            task_id,
            billing_round,
            idempotency_key,
        ),
    )


def test_internal_billing_migration_creates_required_tables_and_defaults(tmp_path: Path) -> None:
    with initialize_database(tmp_path / "billing.db") as conn:
        version = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            )
        }
        runtime = conn.execute(
            """
            SELECT internal_base_unit_price_fen, min_recharge_fen, recharge_step_fen
            FROM runtime_settings WHERE id = 1
            """
        ).fetchone()

    assert version == HEAD_REVISION
    assert {
        "internal_access_tokens",
        "wallets",
        "wallet_transactions",
        "recharge_orders",
    }.issubset(tables)
    assert dict(runtime) == {
        "internal_base_unit_price_fen": 1000,
        "min_recharge_fen": 10000,
        "recharge_step_fen": 1000,
    }


def test_internal_billing_migration_is_reversible(tmp_path: Path) -> None:
    db_path = tmp_path / "reversible.db"
    initialize_database(db_path).close()
    command.downgrade(alembic_config(db_path), "021_generation_batch_display_name")

    with connect_database(db_path) as conn:
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        columns = {row[1] for row in conn.execute("PRAGMA table_info(runtime_settings)")}
    assert "wallets" not in tables
    assert "internal_access_tokens" not in tables
    assert "internal_base_unit_price_fen" not in columns

    command.upgrade(alembic_config(db_path), "head")
    with connect_database(db_path) as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone()[0] == (
            HEAD_REVISION
        )


def test_wallet_backfill_covers_users_created_before_internal_billing(tmp_path: Path) -> None:
    db_path = tmp_path / "wallet-backfill.db"
    command.upgrade(alembic_config(db_path), "021_generation_batch_display_name")
    with connect_database(db_path) as conn:
        conn.execute(
            """
            INSERT INTO users (id, username, display_name, role)
            VALUES ('existing_user', 'existing_user', 'Existing User', 'employee')
            """
        )
        conn.commit()

    command.upgrade(alembic_config(db_path), "head")

    with connect_database(db_path) as conn:
        wallet = conn.execute(
            """
            SELECT available_credits, reserved_credits
            FROM wallets WHERE user_id = 'existing_user'
            """
        ).fetchone()

    assert dict(wallet) == {"available_credits": 0, "reserved_credits": 0}


def test_wallets_reject_negative_balances(tmp_path: Path) -> None:
    with initialize_database(tmp_path / "wallet.db") as conn:
        seed_subjects(conn)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE wallets SET available_credits = -1 WHERE user_id = 'user_1'")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE wallets SET reserved_credits = -1 WHERE user_id = 'user_1'")


def test_recharge_orders_reject_duplicate_merchant_and_provider_numbers(tmp_path: Path) -> None:
    with initialize_database(tmp_path / "orders.db") as conn:
        seed_subjects(conn)
        insert_order(conn, "order_1", "20260819000000000000000000000001", "zpay_1")
        insert_order(conn, "order_2", "20260819000000000000000000000002")
        with pytest.raises(sqlite3.IntegrityError):
            insert_order(conn, "order_3", "20260819000000000000000000000001")
        with pytest.raises(sqlite3.IntegrityError):
            insert_order(conn, "order_4", "20260819000000000000000000000004", "zpay_1")
        with pytest.raises(sqlite3.IntegrityError):
            insert_order(conn, "order_5", "20260819000000000000000000000005", "")


def test_wallet_transactions_reject_duplicate_charge_reserve_and_terminal(
    tmp_path: Path,
) -> None:
    with initialize_database(tmp_path / "transactions.db") as conn:
        seed_subjects(conn)
        insert_order(conn, "order_1", "20260819000000000000000000000001")
        insert_transaction(conn, "charge_1", "CHARGE", "charge:1", recharge_order_id="order_1")
        insert_transaction(conn, "reserve_1", "RESERVE", "reserve:1", task_id="task_1")
        insert_transaction(conn, "terminal_1", "SETTLE", "settle:1", task_id="task_1")

        with pytest.raises(sqlite3.IntegrityError):
            insert_transaction(conn, "charge_2", "CHARGE", "charge:2", recharge_order_id="order_1")
        with pytest.raises(sqlite3.IntegrityError):
            insert_transaction(conn, "reserve_2", "RESERVE", "reserve:2", task_id="task_1")
        with pytest.raises(sqlite3.IntegrityError):
            insert_transaction(conn, "terminal_2", "RELEASE", "release:1", task_id="task_1")


def test_billing_defaults_and_order_snapshot_are_internal_p0_prices(tmp_path: Path) -> None:
    with initialize_database(tmp_path / "settings.db") as conn:
        seed_subjects(conn)
        repo = SettingsRepository(conn, fernet=Fernet(Fernet.generate_key()))
        settings = repo.read_billing_settings()
        insert_order(conn, "order_1", "20260819000000000000000000000001")
        row = conn.execute(
            """
            SELECT pricing_scope, base_unit_price_fen_snapshot,
                   charged_unit_price_fen_snapshot, min_recharge_fen_snapshot,
                   recharge_step_fen_snapshot
            FROM recharge_orders WHERE id = 'order_1'
            """
        ).fetchone()

    assert settings == {
        "internal_base_unit_price_fen": 1000,
        "charged_unit_price_fen": 1000,
        "min_recharge_fen": 10000,
        "recharge_step_fen": 1000,
    }
    assert dict(row) == {
        "pricing_scope": "INTERNAL",
        "base_unit_price_fen_snapshot": 1000,
        "charged_unit_price_fen_snapshot": 1000,
        "min_recharge_fen_snapshot": 10000,
        "recharge_step_fen_snapshot": 1000,
    }


@pytest.mark.parametrize(
    ("base_price", "minimum", "step"),
    [
        (0, 10000, 1000),
        (1000, 9999, 1000),
        (1000, 10000, 999),
        (1000, 10500, 1000),
        (600, 10000, 1000),
    ],
)
def test_billing_settings_reject_invalid_internal_p0_rules(
    tmp_path: Path,
    base_price: int,
    minimum: int,
    step: int,
) -> None:
    with initialize_database(tmp_path / f"invalid-{base_price}-{minimum}-{step}.db") as conn:
        repo = SettingsRepository(conn, fernet=Fernet(Fernet.generate_key()))
        with pytest.raises(ValueError):
            repo.save_billing_settings(
                internal_base_unit_price_fen=base_price,
                min_recharge_fen=minimum,
                recharge_step_fen=step,
                actor_user_id=None,
            )
