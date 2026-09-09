"""W15 — 总览仪表盘聚合端点测试。

独立 PG 库（w15_dashboard_test）；种子：客户用户、今日一成一败两个任务、
PENDING 配对、5 天内过期的可激活码、今日 PAID 充值单。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.admin_auth_routes import (
    ADMIN_CSRF_HEADER,
    ADMIN_SESSION_HMAC_KEY_ENV,
    issue_exchange_credential,
)
from app.db_pg import DATABASE_URL_ENV, close_pg_pool

TEST_KEY = "w15-dash-test-hmac-key-0123456789abcdef"

DEFAULT_DSN = "postgresql://testuser:testpass@localhost:5433/customer_v3_test"
W15_DB_NAME = "w15_dashboard_test"


def _pg_available(dsn: str) -> bool:
    try:
        conn = psycopg.connect(dsn, connect_timeout=3)
        conn.close()
    except Exception:
        return False
    return True


def _pg_dsn() -> str:
    import os

    return os.environ.get("TEST_POSTGRESQL_URL", DEFAULT_DSN)


def _admin_dsn() -> str:
    return _pg_dsn().rsplit("/", 1)[0] + "/postgres"


def _w15_dsn() -> str:
    return _pg_dsn().rsplit("/", 1)[0] + f"/{W15_DB_NAME}"


@pytest.fixture(scope="module")
def dashboard_pg_dsn() -> Iterator[str]:
    from alembic import command
    from alembic.config import Config

    if not _pg_available(_pg_dsn()):
        pytest.skip("PostgreSQL fixture is not available")
    with psycopg.connect(_admin_dsn(), autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{W15_DB_NAME}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{W15_DB_NAME}"')
    server_dir = Path(__file__).resolve().parent.parent
    config = Config(str(server_dir / "alembic.ini"))
    config.set_main_option("script_location", str(server_dir / "migrations"))
    config.set_main_option(
        "sqlalchemy.url", _w15_dsn().replace("postgresql://", "postgresql+psycopg://")
    )
    command.upgrade(config, "head")
    with psycopg.connect(_w15_dsn(), autocommit=True) as conn:
        conn.execute(
            """
            INSERT INTO users (id, username, display_name, role, is_active)
            VALUES ('cust_1', 'customer-1', '客户一', 'customer', 1),
                   ('admin_u', 'admin_u', '管理员', 'admin', 1)
            """
        )
        conn.execute(
            """
            INSERT INTO projects (id, owner_user_id, name)
            VALUES ('p1', 'cust_1', '项目')
            """
        )
        conn.execute(
            """
            INSERT INTO generation_batches (id, project_id, created_by_user_id,
                idempotency_key, request_hash, request_snapshot_json)
            VALUES ('b1', 'p1', 'cust_1', 'k', 'h', '{}')
            """
        )
        # 今日一成一败
        conn.execute(
            """
            INSERT INTO generation_tasks (id, batch_id, generation_mode, provider,
                model, status, archive_status, created_at_utc)
            VALUES ('t_ok', 'b1', 'I2V', 'metaso', 'MiniMax-H3', 'SUCCEEDED',
                    'DIRECT', now()),
                   ('t_bad', 'b1', 'I2V', 'metaso', 'MiniMax-H3', 'FAILED',
                    'PENDING', now())
            """
        )
        # PAID 充值单（今日）
        conn.execute(
            """
            INSERT INTO recharge_orders (id, user_id, provider, amount_fen, credits,
                status, merchant_order_no, provider_trade_no,
                base_unit_price_fen_snapshot, charged_unit_price_fen_snapshot,
                min_recharge_fen_snapshot, recharge_step_fen_snapshot,
                paid_at, created_at)
            VALUES ('order_1', 'cust_1', 'zpay', 10000, 10, 'PAID',
                    'W15-ORDER-1', 'trade-w15-0001', 1000, 1000, 10000, 1000,
                    to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS'),
                    to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS'))
            """
        )
        # 钱包 + 与 PAID 订单配平的 CHARGE 流水（对账一致性 = 0）
        conn.execute(
            """
            INSERT INTO wallets (user_id, available_credits, reserved_credits)
            VALUES ('cust_1', 10, 0)
            """
        )
        conn.execute(
            """
            INSERT INTO wallet_transactions (id, user_id, type, available_delta,
                reserved_delta, recharge_order_id, idempotency_key, created_at)
            VALUES ('tx_charge_1', 'cust_1', 'CHARGE', 10, 0, 'order_1',
                    'zpay:charge:order_1',
                    to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS'))
            """
        )
        # 激活码批次 + 5 天内过期的可激活码
        conn.execute(
            """
            INSERT INTO activation_code_batches (id, name, face_value_fen,
                unit_price_fen_snapshot, credits_snapshot, quantity,
                activation_expires_at, status, created_by_user_id)
            VALUES ('batch_exp', '即将过期批次', 0, 0, 0, 1,
                    now() + make_interval(days => 5), 'OPEN', 'admin_u')
            """
        )
        conn.execute(
            """
            INSERT INTO activation_codes (id, batch_id, code_digest,
                digest_key_version, masked_code, status, issued_at)
            VALUES ('code_exp', 'batch_exp', 'digest-exp', 1,
                    'XS04-EXPI***', 'ISSUED', now())
            """
        )
        # PENDING 且未过期的配对申请
        conn.execute(
            """
            INSERT INTO device_pairing_requests (id, activation_code_id,
                candidate_fingerprint_hmac, candidate_fingerprint_key_version,
                display_name, platform, status, expires_at)
            VALUES ('pair_1', 'code_exp', 'hmac-pending', 1,
                    '新笔记本', 'windows', 'PENDING',
                    now() + interval '10 minutes')
            """
        )
    try:
        yield _w15_dsn()
    finally:
        with psycopg.connect(_admin_dsn(), autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{W15_DB_NAME}" WITH (FORCE)')


@pytest.fixture()
def dash_app(monkeypatch: pytest.MonkeyPatch, dashboard_pg_dsn: str) -> Iterator[FastAPI]:
    from app.admin_auth_routes import router as admin_auth_router
    from app.admin_dashboard_routes import router as admin_dashboard_router

    close_pg_pool()
    monkeypatch.setenv(DATABASE_URL_ENV, dashboard_pg_dsn)
    monkeypatch.setenv(ADMIN_SESSION_HMAC_KEY_ENV, TEST_KEY)
    monkeypatch.delenv("VIDEO_REPLICA_CUSTOMER_PRODUCTION", raising=False)
    app = FastAPI()
    app.include_router(admin_auth_router)
    app.include_router(admin_dashboard_router)
    try:
        yield app
    finally:
        close_pg_pool()


@pytest.fixture()
def client(dash_app: FastAPI) -> Iterator[TestClient]:
    with TestClient(dash_app) as test_client:
        yield test_client


@pytest.fixture()
def admin_headers(client: TestClient) -> dict[str, str]:
    return _exchange(client)


def _exchange(client: TestClient, actor: str = "admin_u") -> dict[str, str]:
    response = client.post(
        "/api/control/admin/session/exchange",
        json={"credential": issue_exchange_credential(actor, ttl_seconds=3600)},
    )
    assert response.status_code == 201, response.text
    return {ADMIN_CSRF_HEADER: response.json()["csrf_token"]}


def test_dashboard_requires_admin_session(client: TestClient) -> None:
    assert client.get("/api/control/dashboard/summary").status_code == 401


def test_dashboard_summary_counts(admin_headers: dict[str, str], client: TestClient) -> None:
    response = client.get("/api/control/dashboard/summary", headers=admin_headers)
    assert response.status_code == 200, response.text
    payload = response.json()

    assert payload["today"]["generation_count"] == 2
    assert payload["today"]["succeeded"] == 1
    assert payload["today"]["success_rate_pct"] == 50.0
    assert payload["today"]["output_seconds"] == 0
    assert payload["today"]["cost_fen"] == 0
    assert payload["today"]["gross_fen"] == 0
    assert payload["today"]["active_customers"] == 1
    assert payload["today"]["recharge_fen"] == 10000
    assert payload["today"]["recharge_orders"] == 1

    todos = payload["todos"]
    assert todos["pending_pairings"] == 1
    assert todos["failed_tasks_7d"] == 1
    assert todos["expiring_codes_7d"] == 1
    assert todos["reconciliation_problems"] == 0
    assert todos["unconfigured_rates"] == 0
    assert todos["unknown_cost_records"] == 0

    assert payload["device_slots"]["total"] == 2
    # 无会话租约 → 在线 0
    assert payload["today"]["online_devices"] == 0

    # 近 7 日趋势包含今日（一成一败）
    assert len(payload["trend"]) == 7
    today_trend = [t for t in payload["trend"] if t["succeeded"] == 1]
    assert len(today_trend) == 1
    assert today_trend[0]["failed"] == 1
    assert today_trend[0]["cost_fen"] == 0


def test_failed_todo_counts_oral_failures_when_no_video_failed(
    admin_headers: dict[str, str], client: TestClient, dashboard_pg_dsn: str
) -> None:
    with psycopg.connect(dashboard_pg_dsn, autocommit=True) as conn:
        conn.execute("UPDATE generation_tasks SET status = 'SUCCEEDED' WHERE id = 't_bad'")
        conn.execute(
            """
            INSERT INTO person_identities (
                id, owner_user_id, display_name, authorization_status,
                source_quality_status, status, created_by
            ) VALUES (
                'dashboard-oral-identity', 'cust_1', '口播人物',
                'AUTHORIZED', 'PASSED', 'ACTIVE', 'cust_1'
            )
            """
        )
        conn.execute(
            """
            INSERT INTO oral_avatars (
                id, identity_id, owner_user_id, title, status,
                source_kind, source_asset_id
            ) VALUES (
                'dashboard-oral-avatar', 'dashboard-oral-identity', 'cust_1',
                '口播分身', 'READY', 'IMAGE', 'dashboard-source'
            )
            """
        )
        conn.execute(
            """
            INSERT INTO oral_tasks (
                id, owner_user_id, identity_id, avatar_id, mode, title,
                status, estimated_cost_fen, error_message, idempotency_key,
                request_hash, submission_state, provider_charge_state
            ) VALUES (
                'dashboard-oral-failed', 'cust_1', 'dashboard-oral-identity',
                'dashboard-oral-avatar', 'TTS', '失败口播', 'FAILED', 350,
                '数字人服务生成失败', 'dashboard-oral-failed-key',
                'dashboard-oral-failed-hash', 'FAILED', 'NOT_CHARGED'
            )
            """
        )
    try:
        response = client.get("/api/control/dashboard/summary", headers=admin_headers)

        assert response.status_code == 200, response.text
        assert response.json()["todos"]["failed_tasks_7d"] == 1
    finally:
        with psycopg.connect(dashboard_pg_dsn, autocommit=True) as conn:
            conn.execute("DELETE FROM oral_tasks WHERE id = 'dashboard-oral-failed'")
            conn.execute("DELETE FROM oral_avatars WHERE id = 'dashboard-oral-avatar'")
            conn.execute("DELETE FROM person_identities WHERE id = 'dashboard-oral-identity'")
            conn.execute("UPDATE generation_tasks SET status = 'FAILED' WHERE id = 't_bad'")


def test_dashboard_day_expressions_ignore_database_session_timezone(
    dashboard_pg_dsn: str,
) -> None:
    from app.admin_dashboard_routes import _day_expr, _timestamptz_day_expr

    utc_text = _day_expr("'2026-09-04 18:00:00'")
    instant = _timestamptz_day_expr("TIMESTAMPTZ '2026-09-04 18:00:00+00'")
    with psycopg.connect(dashboard_pg_dsn, autocommit=True) as conn:
        conn.execute("SET TIME ZONE 'America/Los_Angeles'")
        row = conn.execute(f"SELECT {utc_text}, {instant}").fetchone()

    assert row[0].isoformat() == "2026-09-05"
    assert row[1].isoformat() == "2026-09-05"


def test_cost_trend_includes_the_complete_first_shanghai_day(
    admin_headers: dict[str, str], client: TestClient, dashboard_pg_dsn: str
) -> None:
    with psycopg.connect(dashboard_pg_dsn, autocommit=True) as conn:
        conn.execute(
            """
            INSERT INTO generation_tasks (
                id, batch_id, generation_mode, provider, model, status,
                archive_status, created_at_utc
            ) VALUES (
                'task_first_shanghai_day', 'b1', 'I2V', 'metaso', 'MiniMax-H3',
                'SUCCEEDED', 'DIRECT',
                (((now() AT TIME ZONE 'Asia/Shanghai')::date - 6 + time '00:10')
                    AT TIME ZONE 'Asia/Shanghai')
            ), (
                'failed_first_shanghai_day', 'b1', 'I2V', 'metaso', 'MiniMax-H3',
                'FAILED', 'PENDING',
                (((now() AT TIME ZONE 'Asia/Shanghai')::date - 6 + time '00:10')
                    AT TIME ZONE 'Asia/Shanghai')
            )
            """
        )
        conn.execute(
            """
            INSERT INTO operation_cost_records (
                id, source_type, source_id, subject, user_id,
                resolution, unit, usage_amount, unit_price_fen, cost_fen,
                status, occurred_at, completed_at
            ) VALUES (
                'cost_first_shanghai_day', 'test', 'first-day',
                'video_generation_768p', 'cust_1', '768P', 'second',
                1, 77, 77, 'ACTUAL',
                (((now() AT TIME ZONE 'Asia/Shanghai')::date - 6 + time '00:10')
                    AT TIME ZONE 'Asia/Shanghai'),
                now()
            )
            """
        )

    response = client.get("/api/control/dashboard/summary", headers=admin_headers)

    assert response.status_code == 200, response.text
    assert response.json()["trend"][0]["cost_fen"] == 77
    assert response.json()["trend"][0]["succeeded"] == 1
    assert response.json()["trend"][0]["failed"] == 1
    assert response.json()["todos"]["failed_tasks_7d"] == 2
