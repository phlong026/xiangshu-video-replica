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
from app.db_portable import BusinessConnection
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
    monkeypatch.setenv("VIDEO_REPLICA_DB_PATH", str(db_path))
    monkeypatch.setenv(SETTINGS_KEY_ENV, Fernet.generate_key().decode("ascii"))
    monkeypatch.setenv("VIDEO_REPLICA_AUTH_MODE", "internal")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://internal.example")
    monkeypatch.setenv("ZPAY_GATEWAY_URL", "https://zpayz.cn/submit.php")
    monkeypatch.setenv(CONTROL_ADMIN_USER_ID_ENV, "admin_1")
    monkeypatch.setenv(
        CONTROL_PROXY_TOKEN_DIGEST_ENV,
        hashlib.sha256(CONTROL_TOKEN.encode()).hexdigest(),
    )

    with initialize_database(db_path) as raw:
        with BusinessConnection.sqlite(raw) as conn:
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

    def override_database() -> Iterator[BusinessConnection]:
        with BusinessConnection.sqlite(connect_database(db_path)) as conn:
            yield BusinessConnection.sqlite(conn)

    app.dependency_overrides[get_database] = override_database
    try:
        yield (
            TestClient(app),
            db_path,
            {
                "X-Control-Proxy-Token": CONTROL_TOKEN,
                "Idempotency-Key": "internal-control-write-test-key",
            },
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

    filtered_orders = client.get(
        "/api/control/recharge-orders?username=operator&channel=wxpay&created_from=2020-01-01",
        headers=control_headers,
    )
    assert filtered_orders.status_code == 200
    assert filtered_orders.json()["total"] == 1

    transactions = client.get(
        "/api/control/wallet-transactions?username=operator&created_from=2020-01-01",
        headers=control_headers,
    )
    assert transactions.status_code == 200
    assert transactions.json()["items"][0]["available_balance_after"] == 10
    assert transactions.json()["items"][0]["reserved_balance_after"] == 0

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


def test_control_date_only_end_filter_includes_the_whole_day() -> None:
    from app.control_routes import (
        _generation_record_filters,
        _order_filters,
        _transaction_filters,
    )

    expected_end = "2026-09-05 23:59:59.999999+00:00"
    assert _order_filters(
        status=None,
        user_id=None,
        created_to="2026-09-05",
    )[1] == (expected_end,)
    assert _transaction_filters(
        user_id=None,
        transaction_type=None,
        created_to="2026-09-05",
    )[1] == (expected_end,)
    assert _generation_record_filters(
        record_types=("VIDEO",),
        username=None,
        status=None,
        record_type=None,
        created_from=None,
        created_to="2026-09-05",
    )[1] == (expected_end,)


def test_wallet_balances_follow_ledger_sequence_for_same_timestamp(
    internal_admin_context: tuple[TestClient, Path, dict[str, str], dict[str, str]],
) -> None:
    client, db_path, control_headers, _ = internal_admin_context
    with BusinessConnection.sqlite(connect_database(db_path)) as conn:
        conn.execute(
            "INSERT INTO projects (id, owner_user_id, name) VALUES (%s, %s, %s)",
            ("project-sequenced", "user_1", "钱包顺序回归"),
        )
        conn.execute(
            """
            INSERT INTO generation_batches (
                id, project_id, created_by_user_id, idempotency_key,
                request_hash, request_snapshot_json, status
            ) VALUES (%s, %s, %s, %s, %s, %s, 'RUNNING')
            """,
            (
                "batch-sequenced",
                "project-sequenced",
                "user_1",
                "batch-sequenced-key",
                "batch-sequenced-hash",
                "{}",
            ),
        )
        conn.execute(
            """
            INSERT INTO generation_tasks (
                id, batch_id, generation_mode, provider, model, status
            ) VALUES (%s, %s, 'I2V', 'minimax', 'Hailuo-02', 'RUNNING')
            """,
            ("task-sequenced", "batch-sequenced"),
        )
        conn.execute(
            """
            INSERT INTO wallet_transactions (
                id, user_id, type, available_delta, reserved_delta,
                task_id, billing_round, idempotency_key, created_at
            ) VALUES (%s, 'user_1', 'RESERVE', -5, 5, %s, 1, %s, %s)
            """,
            ("z-reserve", "task-sequenced", "reserve:sequenced", "2026-09-05 12:00:00"),
        )
        conn.execute(
            """
            INSERT INTO wallet_transactions (
                id, user_id, type, available_delta, reserved_delta,
                task_id, billing_round, idempotency_key, created_at
            ) VALUES (%s, 'user_1', 'SETTLE', 0, -5, %s, 1, %s, %s)
            """,
            ("a-settle", "task-sequenced", "settle:sequenced", "2026-09-05 12:00:00"),
        )
        conn.commit()

    response = client.get(
        "/api/control/wallet-transactions?user_id=user_1&limit=20",
        headers=control_headers,
    )
    assert response.status_code == 200, response.text
    by_id = {item["id"]: item for item in response.json()["items"]}
    assert by_id["z-reserve"]["available_balance_after"] == 5
    assert by_id["z-reserve"]["reserved_balance_after"] == 5
    assert by_id["a-settle"]["available_balance_after"] == 5
    assert by_id["a-settle"]["reserved_balance_after"] == 0
    assert by_id["charge_paid"]["available_balance_after"] == 10
    assert by_id["charge_paid"]["reserved_balance_after"] == 0


def test_wallet_ledger_sequence_migration_is_reversible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from alembic import command
    from alembic.config import Config

    monkeypatch.delenv("VIDEO_REPLICA_DATABASE_URL", raising=False)
    db_path = tmp_path / "wallet-sequence-migration.db"
    server_dir = Path(__file__).resolve().parent.parent
    config = Config(str(server_dir / "alembic.ini"))
    config.set_main_option("script_location", str(server_dir / "migrations"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path.as_posix()}")

    command.upgrade(config, "head")
    with connect_database(db_path) as conn:
        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(wallet_transactions)")}
        trigger_count = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'trigger' AND name IN ("
            "'trg_wallet_transactions_assign_ledger_sequence', "
            "'trg_wallet_transactions_reject_explicit_ledger_sequence', "
            "'trg_wallet_transactions_reject_ledger_sequence_update')"
        ).fetchone()[0]
        index_names = {
            str(row[1]) for row in conn.execute("PRAGMA index_list(wallet_transactions)")
        }
    assert "ledger_sequence" in columns
    assert trigger_count == 3
    assert "idx_wallet_transactions_user_ledger_sequence" in index_names

    with BusinessConnection.sqlite(connect_database(db_path)) as conn:
        create_user(
            conn,
            user_id="ledger-user",
            username="ledger-user",
            display_name="Ledger User",
        )
        conn.execute(
            "INSERT INTO recharge_orders ("
            "id, user_id, merchant_order_no, provider, channel, status, pricing_scope, "
            "base_unit_price_fen_snapshot, charged_unit_price_fen_snapshot, "
            "min_recharge_fen_snapshot, recharge_step_fen_snapshot, amount_fen, credits"
            ") VALUES ('ledger-order', 'ledger-user', 'ledger-merchant', 'zpay', 'alipay', "
            "'PAID', 'INTERNAL', 1000, 1000, 10000, 1000, 10000, 10)"
        )
        conn.execute(
            "INSERT INTO recharge_orders ("
            "id, user_id, merchant_order_no, provider, channel, status, pricing_scope, "
            "base_unit_price_fen_snapshot, charged_unit_price_fen_snapshot, "
            "min_recharge_fen_snapshot, recharge_step_fen_snapshot, amount_fen, credits"
            ") VALUES ('ledger-order-explicit', 'ledger-user', 'ledger-merchant-explicit', "
            "'zpay', 'alipay', 'PAID', 'INTERNAL', 1000, 1000, 10000, 1000, 10000, 10)"
        )
        conn.execute(
            "INSERT INTO wallet_transactions ("
            "id, user_id, type, available_delta, reserved_delta, recharge_order_id, "
            "idempotency_key) VALUES ('ledger-tx', 'ledger-user', 'CHARGE', 10, 0, "
            "'ledger-order', 'ledger-key')"
        )
        sequence = conn.execute(
            "SELECT ledger_sequence FROM wallet_transactions WHERE id='ledger-tx'"
        ).fetchone()[0]
        assert sequence is not None
        with pytest.raises(sqlite3.IntegrityError, match="database assigned"):
            conn.execute(
                "INSERT INTO wallet_transactions ("
                "id, user_id, type, available_delta, reserved_delta, recharge_order_id, "
                "idempotency_key, ledger_sequence) VALUES ("
                "'ledger-tx-explicit', 'ledger-user', 'CHARGE', 10, 0, "
                "'ledger-order-explicit', 'ledger-key-explicit', 999999)"
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(
                "UPDATE wallet_transactions SET ledger_sequence=%s WHERE id='ledger-tx'",
                (int(sequence) + 1,),
            )

    command.downgrade(config, "062_activation_initial_free_seconds")
    with connect_database(db_path) as conn:
        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(wallet_transactions)")}
        trigger_count = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'trigger' AND name IN ("
            "'trg_wallet_transactions_assign_ledger_sequence', "
            "'trg_wallet_transactions_reject_explicit_ledger_sequence', "
            "'trg_wallet_transactions_reject_ledger_sequence_update')"
        ).fetchone()[0]
    assert "ledger_sequence" not in columns
    assert trigger_count == 0

    command.upgrade(config, "head")
    with connect_database(db_path) as conn:
        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(wallet_transactions)")}
    assert "ledger_sequence" in columns


def test_control_generation_records_include_paid_images_and_ai_scoring(
    internal_admin_context: tuple[TestClient, Path, dict[str, str], dict[str, str]],
) -> None:
    client, db_path, control_headers, _ = internal_admin_context
    with BusinessConnection.sqlite(connect_database(db_path)) as conn:
        conn.execute(
            "INSERT INTO projects (id, owner_user_id, name) VALUES (%s, %s, %s)",
            ("project-records", "user_1", "生成追溯项目"),
        )
        conn.execute(
            """
            INSERT INTO generation_batches (
                id, project_id, created_by_user_id, idempotency_key,
                request_hash, request_snapshot_json, status
            ) VALUES (%s, %s, %s, %s, %s, %s, 'RUNNING')
            """,
            (
                "batch-records",
                "project-records",
                "user_1",
                "batch-records-key",
                "batch-records-hash",
                "{}",
            ),
        )
        conn.execute(
            """
            INSERT INTO generation_tasks (
                id, batch_id, generation_mode, provider, model, status,
                estimated_cost, actual_cost, prompt_snapshot_json
            ) VALUES (%s, %s, 'I2V', 'minimax', 'Hailuo-02', 'RUNNING', %s, NULL, %s)
            """,
            ("video-estimated-record", "batch-records", 1.25, "{}"),
        )
        conn.execute(
            """
            INSERT INTO assets (
                id, project_id, kind, storage_uri, sha256, size_bytes,
                content_type, created_by_user_id
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                "reference-records",
                "project-records",
                "reference_video",
                "local://reference-records.mp4",
                "reference-hash",
                10,
                "video/mp4",
                "user_1",
            ),
        )
        conn.execute(
            """
            INSERT INTO versions (
                id, project_id, kind, version_number, payload_json, created_by_user_id
            ) VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                "first-frame-record-version",
                "project-records",
                "first_frame_candidates",
                1,
                '{"provider":"apilio","model":"gpt-image-2","candidates":[]}',
                "user_1",
            ),
        )
        conn.execute(
            """
            INSERT INTO versions (
                id, project_id, kind, version_number, payload_json, created_by_user_id
            ) VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                "corrupt-first-frame-version",
                "project-records",
                "first_frame_candidates",
                2,
                "not-json",
                "user_1",
            ),
        )
        conn.execute(
            """
            INSERT INTO versions (
                id, project_id, kind, version_number, payload_json, created_by_user_id
            ) VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                "source-score-record-version",
                "project-records",
                "source_frame_candidates",
                1,
                '{"semantic_quality_status":"VERIFIED","semantic_provider":"apilio_gemini",'
                '"semantic_model":"gemini-3.1-pro-preview","candidates":[]}',
                "user_1",
            ),
        )
        conn.execute(
            """
            INSERT INTO first_frame_tasks (
                id, project_id, created_by_user_id, idempotency_key, request_hash,
                request_json, status, result_version_id, completed_at
            ) VALUES (%s, %s, %s, %s, %s, %s, 'SUCCEEDED', %s, CURRENT_TIMESTAMP)
            """,
            (
                "first-frame-record",
                "project-records",
                "user_1",
                "first-frame-record-key",
                "first-frame-record-hash",
                '{"model":"gpt-image-2","quantity":1}',
                "first-frame-record-version",
            ),
        )
        conn.execute(
            """
            INSERT INTO source_frame_tasks (
                id, project_id, asset_id, created_by_user_id, idempotency_key,
                request_hash, request_json, result_version_id, status, completed_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'SUCCEEDED', CURRENT_TIMESTAMP)
            """,
            (
                "source-score-record",
                "project-records",
                "reference-records",
                "user_1",
                "source-score-record-key",
                "source-score-record-hash",
                '{"timestamps_seconds":[1.0]}',
                "source-score-record-version",
            ),
        )
        conn.execute(
            """
            INSERT INTO person_identities (
                id, owner_user_id, display_name, authorization_status,
                source_quality_status, status, created_by
            ) VALUES (%s, %s, %s, 'AUTHORIZED', 'PASSED', 'ACTIVE', %s)
            """,
            ("oral-record-identity", "user_1", "口播人物", "user_1"),
        )
        conn.execute(
            """
            INSERT INTO oral_avatars (
                id, identity_id, owner_user_id, title, vendor_avatar_id,
                status, source_kind, source_asset_id
            ) VALUES (%s, %s, %s, %s, %s, 'READY', 'IMAGE', %s)
            """,
            (
                "oral-record-avatar",
                "oral-record-identity",
                "user_1",
                "口播分身",
                "hifly-avatar-record",
                "oral-source-record",
            ),
        )
        conn.execute(
            """
            INSERT INTO oral_tasks (
                id, owner_user_id, identity_id, avatar_id, mode, title,
                status, vendor_task_id, estimated_cost_fen, error_message,
                idempotency_key, request_hash, submission_state,
                billing_round, provider_charge_state
            ) VALUES (%s, %s, %s, %s, 'TTS', %s, 'FAILED', %s, %s, %s,
                      %s, %s, 'FAILED', 1, 'CHARGED')
            """,
            (
                "oral-failed-record",
                "user_1",
                "oral-record-identity",
                "oral-record-avatar",
                "口播失败记录",
                "hifly-task-record",
                350,
                "Authorization: Bearer leaked-token; https://vendor.test/result?signature=leaked",
                "oral-failed-record-key",
                "oral-failed-record-hash",
            ),
        )
        conn.execute(
            """
            INSERT INTO oral_tasks (
                id, owner_user_id, identity_id, avatar_id, mode, title,
                status, vendor_task_id, result_asset_id, estimated_cost_fen,
                idempotency_key, request_hash, submission_state,
                provider_charge_state
            ) VALUES (%s, %s, %s, %s, 'AUDIO', %s, 'SUCCEEDED', %s, %s, %s,
                      %s, %s, 'SUBMITTED', 'CHARGED')
            """,
            (
                "oral-succeeded-record",
                "user_1",
                "oral-record-identity",
                "oral-record-avatar",
                "口播成功记录",
                "hifly-success-task-record",
                "oral-result-asset-record",
                350,
                "oral-succeeded-record-key",
                "oral-succeeded-record-hash",
            ),
        )
        conn.execute(
            """
            INSERT INTO wallet_transactions (
                id, user_id, type, available_delta, reserved_delta,
                oral_task_id, billing_round, idempotency_key
            ) VALUES (%s, %s, 'SETTLE', 0, -12, %s, 1, %s)
            """,
            (
                "oral-failed-record-settle",
                "user_1",
                "oral-failed-record",
                "oral-failed-record-settle-key",
            ),
        )
        conn.execute(
            """
            INSERT INTO first_frame_tasks (
                id, project_id, created_by_user_id, idempotency_key, request_hash,
                request_json, status, result_version_id, completed_at
            ) VALUES (%s, %s, %s, %s, %s, %s, 'SUCCEEDED', %s, CURRENT_TIMESTAMP)
            """,
            (
                "corrupt-first-frame-record",
                "project-records",
                "user_1",
                "corrupt-first-frame-key",
                "corrupt-first-frame-hash",
                '{"model":"gpt-image-2","quantity":1}',
                "corrupt-first-frame-version",
            ),
        )
        conn.execute(
            """
            INSERT INTO source_frame_tasks (
                id, project_id, asset_id, created_by_user_id, idempotency_key,
                request_hash, request_json, status
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, 'PENDING')
            """,
            (
                "source-local-record",
                "project-records",
                "reference-records",
                "user_1",
                "source-local-key",
                "source-local-hash",
                '{"timestamps_seconds":[2.0]}',
            ),
        )
        conn.execute(
            """
            INSERT INTO audit_logs (
                id, actor_user_id, action, entity_type, entity_id, metadata_json
            ) VALUES (%s, NULL, %s, %s, %s, %s)
            """,
            (
                "audit-source-local-000",
                "source_frame.semantic_quality_started",
                "source_frame_task",
                "source-local-record",
                '{"provider":"apilio_gemini","model":"gemini-3.1-pro-preview"}',
            ),
        )
        conn.executemany(
            """
            INSERT INTO audit_logs (
                id, actor_user_id, action, entity_type, entity_id, metadata_json
            ) VALUES (%s, NULL, %s, %s, %s, %s)
            """,
            [
                (
                    f"audit-unrelated-{index:03d}",
                    "source_frame.semantic_quality_started",
                    "source_frame_task",
                    f"unrelated-source-task-{index:03d}",
                    "{}",
                )
                for index in range(20)
            ],
        )
        conn.commit()

    response = client.get(
        "/api/control/generation-records?limit=20&offset=0",
        headers=control_headers,
    )

    assert response.status_code == 200
    records = response.json()["items"]
    by_id = {item["record_id"]: item for item in records}
    assert by_id["first-frame-record"]["username"] == "operator-1"
    assert by_id["first-frame-record"]["provider"] == "apilio"
    assert by_id["first-frame-record"]["model"] == "gpt-image-2"
    assert by_id["first-frame-record"]["provider_cost_status"] == "UNAVAILABLE"
    assert by_id["source-score-record"]["provider"] == "apilio_gemini"
    assert by_id["source-score-record"]["model"] == "gemini-3.1-pro-preview"
    assert by_id["source-score-record"]["charged_credits"] == 0
    assert by_id["video-estimated-record"]["provider_cost_status"] == "ESTIMATED"
    assert by_id["corrupt-first-frame-record"]["record_data_status"] == "CORRUPTED"
    assert by_id["source-local-record"]["record_type"] == "SOURCE_FRAME_AI_SCORE"
    assert by_id["source-local-record"]["provider"] == "apilio_gemini"
    assert by_id["source-local-record"]["provider_cost_status"] == "UNAVAILABLE"
    assert by_id["oral-failed-record"] == {
        "record_id": "oral-failed-record",
        "record_type": "ORAL_VIDEO",
        "operation": "TTS",
        "user_id": "user_1",
        "username": "operator-1",
        "display_name": "Operator One",
        "project_id": None,
        "project_name": None,
        "status": "FAILED",
        "provider": "hifly",
        "model": None,
        "provider_cost": None,
        "provider_cost_status": "UNAVAILABLE",
        "record_data_status": "VALID",
        "charged_credits": 12,
        "result_reference": None,
        "provider_reference": "hifly-task-record",
        "error_code": "ORAL_TASK_FAILED",
        "error_message": "数字人口播生成失败，请核对供应商任务和服务配置。",
        "created_at": by_id["oral-failed-record"]["created_at"],
        "completed_at": by_id["oral-failed-record"]["completed_at"],
    }
    assert by_id["oral-succeeded-record"]["result_reference"] == "oral-result-asset-record"
    assert by_id["oral-succeeded-record"]["provider_reference"] == "hifly-success-task-record"
    assert "leaked-token" not in response.text
    assert "signature=leaked" not in response.text

    videos = client.get(
        "/api/control/generation-records?record_type=VIDEO&username=operator&status=RUNNING",
        headers=control_headers,
    )
    assert videos.status_code == 200, videos.text
    assert videos.json()["total"] == 1
    assert [item["record_id"] for item in videos.json()["items"]] == ["video-estimated-record"]

    oral_failures = client.get(
        "/api/control/generation-records?record_type=ORAL_VIDEO&status=FAILED",
        headers=control_headers,
    )
    assert oral_failures.status_code == 200, oral_failures.text
    assert oral_failures.json()["total"] == 1
    assert [item["record_id"] for item in oral_failures.json()["items"]] == ["oral-failed-record"]

    process_records = client.get(
        "/api/control/generation-records?record_type=SOURCE_FRAME_PROCESS",
        headers=control_headers,
    )
    assert process_records.status_code == 200, process_records.text
    assert process_records.json()["total"] == 0
    assert process_records.json()["items"] == []

    deep_offset = client.get(
        "/api/control/generation-records?limit=20&offset=1001",
        headers=control_headers,
    )
    assert deep_offset.status_code == 200


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
            "confirm": True,
            "reason": "rotate the configured merchant account",
        },
    )

    assert updated.status_code == 200
    assert updated.json()["config"]["pid"] == "merchant-456"
    assert updated.json()["config"]["key"] == "********cret"
    with BusinessConnection.sqlite(connect_database(db_path)) as conn:
        assert SettingsRepository(conn).load_zpay_config()["key"] == "merchant-secret"

    forbidden_field = client.patch(
        "/api/control/settings/zpay",
        headers=control_headers,
        json={
            "pid": "merchant-456",
            "enabled_channels": ["wxpay"],
            "gateway_url": "https://evil.example/submit.php",
            "confirm": True,
            "reason": "verify read-only deployment fields",
        },
    )
    assert forbidden_field.status_code == 422


def test_control_settings_use_documented_gateway_without_gateway_environment(
    internal_admin_context: tuple[TestClient, Path, dict[str, str], dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _, control_headers, _ = internal_admin_context
    monkeypatch.delenv("ZPAY_GATEWAY_URL")

    snapshot = client.get("/api/control/settings", headers=control_headers)

    assert snapshot.status_code == 200
    assert snapshot.json()["deployment"] == {
        "gateway_url": "https://zpayz.cn/submit.php",
        "notify_url": "https://internal.example/api/payments/zpay/notify",
        "return_url": "https://internal.example/api/payments/zpay/return",
    }
    assert snapshot.json()["providers"]["metaso"]["provider"] == "metaso"


def test_internal_control_writes_require_the_admin_write_contract(
    internal_admin_context: tuple[TestClient, Path, dict[str, str], dict[str, str]],
) -> None:
    client, _, control_headers, _ = internal_admin_context
    payload = {
        "max_generation_count_per_batch": 2,
        "max_concurrent_h3_tasks": 1,
        "active_storage_provider": "cos",
    }

    missing_confirmation = client.patch(
        "/api/control/settings/runtime",
        headers=control_headers,
        json=payload,
    )
    missing_reason = client.patch(
        "/api/control/settings/runtime",
        headers=control_headers,
        json={**payload, "confirm": True},
    )
    missing_key = client.patch(
        "/api/control/settings/runtime",
        headers={"X-Control-Proxy-Token": CONTROL_TOKEN},
        json={**payload, "confirm": True, "reason": "test missing key"},
    )

    assert missing_confirmation.status_code == 400
    assert missing_confirmation.json()["detail"]["code"] == "CONFIRMATION_REQUIRED"
    assert missing_reason.status_code == 400
    assert missing_reason.json()["detail"]["code"] == "REASON_REQUIRED"
    assert missing_key.status_code == 400
    assert missing_key.json()["detail"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"


def test_control_settings_manage_encrypted_service_configs_and_runtime(
    internal_admin_context: tuple[TestClient, Path, dict[str, str], dict[str, str]],
) -> None:
    client, db_path, control_headers, _ = internal_admin_context

    saved = client.put(
        "/api/control/settings/providers/metaso",
        headers=control_headers,
        json={
            "config": {"api_key": "video-service-secret"},
            "confirm": True,
            "reason": "configure the metaso provider",
        },
    )
    snapshot = client.get("/api/control/settings", headers=control_headers)
    connection = client.post(
        "/api/control/settings/providers/metaso/connection-test",
        headers=control_headers,
    )
    runtime = client.patch(
        "/api/control/settings/runtime",
        headers=control_headers,
        json={
            "max_generation_count_per_batch": 2,
            "max_concurrent_h3_tasks": 1,
            "active_storage_provider": "cos",
            "confirm": True,
            "reason": "adjust generation runtime limits",
        },
    )

    assert saved.status_code == 200
    assert saved.json() == {
        "provider": "metaso",
        "configured": True,
        "config": {"api_key": "********cret"},
    }
    assert snapshot.status_code == 200
    assert snapshot.json()["providers"]["metaso"] == saved.json()
    assert "video-service-secret" not in snapshot.text
    assert connection.status_code == 200
    assert connection.json() == {
        "status": "configured_only",
        "provider": "metaso",
        "test_kind": "connection",
    }
    assert runtime.status_code == 200
    assert runtime.json() == {
        "max_generation_count_per_batch": 2,
        "max_concurrent_h3_tasks": 1,
        "active_storage_provider": "cos",
    }
    with BusinessConnection.sqlite(connect_database(db_path)) as conn:
        assert SettingsRepository(conn).load_provider_config("metaso") == {
            "api_key": "video-service-secret"
        }


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
            "confirm": True,
            "reason": "adjust internal billing settings",
        },
    )

    assert updated.status_code == 200
    assert updated.json() == {
        "internal_base_unit_price_fen": 500,
        "charged_unit_price_fen": 500,
        "min_recharge_fen": 10000,
        "recharge_step_fen": 1000,
    }
    with BusinessConnection.sqlite(connect_database(db_path)) as conn:
        stored = SettingsRepository(conn).read_billing_settings()
    assert stored == updated.json()

    below_minimum = client.patch(
        "/api/control/settings/billing",
        headers=control_headers,
        json={
            "internal_base_unit_price_fen": 500,
            "min_recharge_fen": 9900,
            "recharge_step_fen": 1000,
            "confirm": True,
            "reason": "verify billing lower bound",
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
            "confirm": True,
            "reason": "verify customer price cannot be changed here",
        },
    )

    assert below_minimum.status_code == 422
    assert customer_price_field.status_code == 422


def test_control_reconciliation_and_csv_are_read_only(
    internal_admin_context: tuple[TestClient, Path, dict[str, str], dict[str, str]],
) -> None:
    client, db_path, control_headers, _ = internal_admin_context
    with BusinessConnection.sqlite(connect_database(db_path)) as conn:
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

    with BusinessConnection.sqlite(connect_database(db_path)) as conn:
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
        "/api/control/settings/providers/{provider}",
        "/api/control/settings/providers/{provider}/connection-test",
        "/api/control/settings/runtime",
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


def test_control_ledger_exports_and_reconciliation_are_audited(
    internal_admin_context: tuple[TestClient, Path, dict[str, str], dict[str, str]],
) -> None:
    """A2（2026-09-02 评估）：账务导出与对账读必须留下操作者审计行."""
    client, db_path, control_headers, _ = internal_admin_context

    summary = client.get("/api/control/billing-reconciliation", headers=control_headers)
    orders_csv = client.get("/api/control/recharge-orders.csv", headers=control_headers)
    ledger_csv = client.get("/api/control/wallet-transactions.csv", headers=control_headers)
    assert summary.status_code == 200
    assert orders_csv.status_code == 200
    assert ledger_csv.status_code == 200

    with BusinessConnection.sqlite(connect_database(db_path)) as conn:
        rows = conn.execute(
            "SELECT action, entity_id FROM audit_logs "
            "WHERE action IN ('control.export', 'control.reconciliation.read') "
            "ORDER BY action, entity_id"
        ).fetchall()

    assert dict(rows).get("control.export") is not None
    actions = [str(row[0]) for row in rows]
    assert actions.count("control.export") == 2
    assert actions.count("control.reconciliation.read") == 1
    export_entities = sorted(str(row[1]) for row in rows if str(row[0]) == "control.export")
    assert export_entities == ["recharge_orders", "wallet_transactions"]


def test_control_ledger_export_rate_limit_dimension_is_registered() -> None:
    from app.security_rate_limit import (
        DIMENSION_CONTROL_EXPORT_ACCOUNT,
        RATE_LIMIT_DIMENSIONS,
        control_export_account_limit,
    )

    assert DIMENSION_CONTROL_EXPORT_ACCOUNT in RATE_LIMIT_DIMENSIONS
    assert control_export_account_limit() >= 1
