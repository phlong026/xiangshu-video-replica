"""W8 — 经营分析路由测试（每日售价 upsert + 日利润聚合）。

独立 PG 库（w08_profit_test）迁移到 head；种子：
- 一个客户 + 钱包 + 视频任务（768P，10 秒，SUCCEEDED/DIRECT）；
- 对应 SETTLE 流水（预留 -10/+10 → 结算 0/-10）。
覆盖：售价 upsert 契约与审计、利润聚合（收入=当日售价×结算秒数、
成本=actual_cost 合计、毛利/利润率）、未知日期口径（carry-forward）。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pg_test_kit import require_pg_or_explicit_skip

from app.admin_auth_routes import (
    ADMIN_CSRF_HEADER,
    ADMIN_SESSION_HMAC_KEY_ENV,
    issue_exchange_credential,
)
from app.admin_write_contract import IDEMPOTENCY_KEY_HEADER
from app.db_pg import DATABASE_URL_ENV, close_pg_pool

TEST_KEY = "w08-profit-test-hmac-key-0123456789abcdef"


DEFAULT_DSN = "postgresql://testuser:testpass@localhost:5433/customer_v3_test"
W08_DB_NAME = "w08_profit_test"


def _pg_dsn() -> str:
    import os

    return os.environ.get("TEST_POSTGRESQL_URL", DEFAULT_DSN)


def _admin_dsn() -> str:
    return _pg_dsn().rsplit("/", 1)[0] + "/postgres"


def _w08_dsn() -> str:
    return _pg_dsn().rsplit("/", 1)[0] + f"/{W08_DB_NAME}"


@pytest.fixture(scope="module")
def profit_pg_dsn() -> Iterator[str]:
    from alembic import command
    from alembic.config import Config

    require_pg_or_explicit_skip(_pg_dsn())
    with psycopg.connect(_admin_dsn(), autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{W08_DB_NAME}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{W08_DB_NAME}"')
    server_dir = Path(__file__).resolve().parent.parent
    config = Config(str(server_dir / "alembic.ini"))
    config.set_main_option("script_location", str(server_dir / "migrations"))
    config.set_main_option(
        "sqlalchemy.url", _w08_dsn().replace("postgresql://", "postgresql+psycopg://")
    )
    command.upgrade(config, "head")
    with psycopg.connect(_w08_dsn(), autocommit=True) as conn:
        for user_id, role in (("admin_u", "admin"), ("auditor_u", "auditor")):
            conn.execute(
                "INSERT INTO users (id, username, display_name, role) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT (id) DO NOTHING",
                (user_id, user_id, user_id.replace("_", " ").title(), role),
            )
    try:
        yield _w08_dsn()
    finally:
        with psycopg.connect(_admin_dsn(), autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{W08_DB_NAME}" WITH (FORCE)')


@pytest.fixture()
def profit_app(monkeypatch: pytest.MonkeyPatch, profit_pg_dsn: str) -> Iterator[FastAPI]:
    from app.admin_auth_routes import router as admin_auth_router
    from app.admin_profit_routes import router as admin_profit_router

    close_pg_pool()
    monkeypatch.setenv(DATABASE_URL_ENV, profit_pg_dsn)
    monkeypatch.setenv(ADMIN_SESSION_HMAC_KEY_ENV, TEST_KEY)
    monkeypatch.delenv("VIDEO_REPLICA_CUSTOMER_PRODUCTION", raising=False)
    app = FastAPI()
    app.include_router(admin_auth_router)
    app.include_router(admin_profit_router)
    try:
        yield app
    finally:
        close_pg_pool()


@pytest.fixture()
def client(profit_app: FastAPI) -> Iterator[TestClient]:
    with TestClient(profit_app) as test_client:
        yield test_client


def _exchange(client: TestClient, actor: str = "admin_u") -> dict[str, str]:
    response = client.post(
        "/api/control/admin/session/exchange",
        json={"credential": issue_exchange_credential(actor, ttl_seconds=3600)},
    )
    assert response.status_code == 201, response.text
    return {ADMIN_CSRF_HEADER: response.json()["csrf_token"]}


@pytest.fixture()
def admin_headers(client: TestClient) -> dict[str, str]:
    return _exchange(client)


def _write_headers(base: dict[str, str]) -> dict[str, str]:
    import uuid

    return {**base, IDEMPOTENCY_KEY_HEADER: f"key-{uuid.uuid4()}"}


def test_daily_price_upsert_requires_contract(
    admin_headers: dict[str, str], client: TestClient
) -> None:
    payload = {
        "price_date": str((dt.datetime.now() + dt.timedelta(days=1)).date()),
        "price_768p_fen": 12,
        "price_2k_fen": 20,
    }
    response = client.put("/api/control/profit/daily-price", json=payload, headers=admin_headers)
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"

    response = client.put(
        "/api/control/profit/daily-price",
        json={**payload, "confirm": True},
        headers={ADMIN_CSRF_HEADER: admin_headers[ADMIN_CSRF_HEADER]},
    )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"


@pytest.fixture()
def auditor_headers(client: TestClient) -> dict[str, str]:
    return _exchange(client, "auditor_u")


def test_daily_price_upsert_rejects_auditor(
    auditor_headers: dict[str, str], client: TestClient
) -> None:
    response = client.put(
        "/api/control/profit/daily-price",
        json={
            "price_date": str((dt.datetime.now() + dt.timedelta(days=1)).date()),
            "price_768p_fen": 12,
            "price_2k_fen": 20,
            "confirm": True,
            "reason": "审计员不应能定价",
        },
        headers=_write_headers(auditor_headers),
    )
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "AUDITOR_READ_ONLY"


def test_daily_price_upsert_and_overwrite(
    admin_headers: dict[str, str], client: TestClient
) -> None:
    body = {
        "price_date": str((dt.datetime.now() + dt.timedelta(days=1)).date()),
        "price_768p_fen": 12,
        "price_2k_fen": 20,
        "note": "促销定价",
        "confirm": True,
        "reason": "客户续费定价",
    }
    response = client.put(
        "/api/control/profit/daily-price", json=body, headers=_write_headers(admin_headers)
    )
    assert response.status_code == 200, response.text
    prices = response.json()
    assert prices[0]["price_date"] == str((dt.datetime.now() + dt.timedelta(days=1)).date())
    assert prices[0]["price_768p_fen"] == 12

    # 同日改价 = 覆盖（upsert），列表仍只有一条该日期。
    response = client.put(
        "/api/control/profit/daily-price",
        json={**body, "price_768p_fen": 15, "reason": "改价"},
        headers=_write_headers(admin_headers),
    )
    assert response.status_code == 200
    same_day = [
        p
        for p in response.json()
        if p["price_date"] == str((dt.datetime.now() + dt.timedelta(days=1)).date())
    ]
    assert len(same_day) == 1
    assert same_day[0]["price_768p_fen"] == 15


def test_daily_price_rejects_invalid_date(
    admin_headers: dict[str, str], client: TestClient
) -> None:
    response = client.put(
        "/api/control/profit/daily-price",
        json={
            "price_date": "not-a-date",
            "price_768p_fen": 12,
            "price_2k_fen": 20,
            "confirm": True,
            "reason": "测试",
        },
        headers=_write_headers(admin_headers),
    )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "PROFIT_DATE_INVALID"


def test_profit_overview_aggregates_revenue_cost_margin(
    admin_headers: dict[str, str], client: TestClient, profit_pg_dsn: str
) -> None:
    import psycopg

    # 种子：一天前录入售价（768P 0.10 元/秒）；客户/项目/批次/任务 + 结算流水。
    with psycopg.connect(profit_pg_dsn, autocommit=True) as conn:
        conn.execute(
            """
            INSERT INTO daily_external_prices
                (price_date, price_768p_fen, price_2k_fen, created_at)
            VALUES (((now() AT TIME ZONE 'Asia/Shanghai')::date - 1), 10, 20, now())
            ON CONFLICT (price_date) DO UPDATE SET price_768p_fen = 10
            """
        )
        conn.execute(
            """
            INSERT INTO users (id, username, display_name, role)
            VALUES ('cust_1', 'customer-1', '客户一', 'customer')
            ON CONFLICT (id) DO NOTHING
            """
        )
        conn.execute(
            """
            INSERT INTO projects (id, owner_user_id, name)
            VALUES ('p1', 'cust_1', '项目')
            ON CONFLICT (id) DO NOTHING
            """
        )
        conn.execute(
            """
            INSERT INTO generation_batches (id, project_id, created_by_user_id,
                idempotency_key, request_hash, request_snapshot_json)
            VALUES ('b1', 'p1', 'cust_1', 'k1', 'h1', '{}')
            ON CONFLICT (id) DO NOTHING
            """
        )
        conn.execute(
            """
            INSERT INTO generation_tasks (id, batch_id, generation_mode, provider, model,
                status, archive_status, prompt_snapshot_json, actual_cost,
                completed_at, billed_seconds)
            VALUES ('t1', 'b1', 'I2V', 'metaso', 'MiniMax-H3', 'SUCCEEDED', 'DIRECT',
                    '{"resolution": "768P", "output_duration_seconds": 10}'::json,
                    0.90,
                    to_char(now() - interval '1 day' + interval '2 hours',
                            'YYYY-MM-DD HH24:MI:SS'),
                    10)
            ON CONFLICT (id) DO NOTHING
            """
        )
        conn.execute(
            """
            INSERT INTO wallets (user_id, available_credits, reserved_credits)
            VALUES ('cust_1', 100, 0) ON CONFLICT (user_id) DO NOTHING
            """
        )
        conn.execute(
            """
            INSERT INTO wallet_transactions (
                id, user_id, type, available_delta, reserved_delta,
                task_id, billing_round, idempotency_key, created_at
            ) VALUES (
                'tx_settle_1', 'cust_1', 'RESERVE', -10, 10, 't1', 1,
                'reserve:t1:1',
                to_char(now() - interval '1 day' + interval '2 hours',
                        'YYYY-MM-DD HH24:MI:SS')
            ), (
                'tx_settle_2', 'cust_1', 'SETTLE', 0, -10, 't1', 1,
                'settle:t1:1',
                to_char(now() - interval '1 day' + interval '2 hours',
                        'YYYY-MM-DD HH24:MI:SS')
            ) ON CONFLICT (id) DO NOTHING
            """
        )
        conn.execute(
            """
            INSERT INTO operation_cost_records (
                id, source_type, source_id, subject, user_id, generation_task_id,
                resolution, unit, usage_amount, unit_price_fen, cost_fen,
                status, occurred_at, completed_at
            ) VALUES (
                'cost_t1', 'generation_task', 't1', 'video_generation_768p',
                'cust_1', 't1', '768P', 'second', 10, 9, 90, 'ACTUAL',
                now() - interval '1 day' + interval '2 hours', now()
            ) ON CONFLICT (id) DO NOTHING
            """
        )

    response = client.get("/api/control/profit/overview?lookback_days=7", headers=admin_headers)
    assert response.status_code == 200, response.text
    payload = response.json()

    day = next(d for d in payload["days"] if d["settled_seconds"] == 10)
    # 标准收入：结算 10 秒 × 768P 售价 0.10 元 = 1.00 元 = 100 分。
    assert day["revenue_fen"] == 100
    # 成本：actual_cost = 10 秒 × 0.09 元 = 0.90 元 = 90 分。
    assert day["cost_fen"] == 90
    assert day["gross_fen"] == 10
    assert day["margin_pct"] == 10.0
    assert day["video_count"] == 1

    # 售价列表包含录入的日期。
    assert any(p["price_date"] and p["price_768p_fen"] == 10 for p in payload["prices"])
    assert "不回填" in payload["cost_coverage_note"]


def test_cost_overview_and_csv(admin_headers: dict[str, str], client: TestClient) -> None:
    response = client.get("/api/control/profit/costs?lookback_days=7", headers=admin_headers)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["total_cost_fen"] == 90
    assert payload["total_output_seconds"] == 10
    assert payload["average_video_cost_per_second_fen"] == 9
    assert payload["unknown_count"] == 0
    assert payload["record_total"] == 1
    assert payload["records_truncated"] is False
    assert payload["days"][0]["video_768p_fen"] == 90

    csv_response = client.get(
        "/api/control/profit/costs.csv?lookback_days=7", headers=admin_headers
    )
    assert csv_response.status_code == 200
    assert csv_response.content.startswith(b"\xef\xbb\xbf")
    assert "输出秒数" in csv_response.text

    profit_csv = client.get(
        "/api/control/profit/overview.csv?lookback_days=7", headers=admin_headers
    )
    assert profit_csv.status_code == 200
    assert profit_csv.content.startswith(b"\xef\xbb\xbf")
    assert "未知成本数" in profit_csv.text


def test_average_video_cost_excludes_non_video_subjects(
    profit_pg_dsn: str, admin_headers: dict[str, str], client: TestClient
) -> None:
    with psycopg.connect(profit_pg_dsn, autocommit=True) as conn:
        conn.execute(
            """
            INSERT INTO operation_cost_records (
                id, source_type, source_id, subject, unit, usage_amount,
                unit_price_fen, cost_fen, status, occurred_at, completed_at
            ) VALUES (
                'analysis_average_guard', 'analysis_task', 'analysis-average-guard',
                'video_analysis_768p', 'second', 10, 10, 100, 'ACTUAL', now(), now()
            )
            """
        )
    try:
        response = client.get("/api/control/profit/costs?lookback_days=7", headers=admin_headers)
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["total_cost_fen"] == 190
        assert payload["average_video_cost_per_second_fen"] == 9
    finally:
        with psycopg.connect(profit_pg_dsn, autocommit=True) as conn:
            conn.execute("DELETE FROM operation_cost_records WHERE id = 'analysis_average_guard'")


def test_cost_records_report_truncation(
    profit_pg_dsn: str, admin_headers: dict[str, str], client: TestClient
) -> None:
    with psycopg.connect(profit_pg_dsn, autocommit=True) as conn:
        conn.execute(
            """
            INSERT INTO operation_cost_records (
                id, source_type, source_id, subject, unit, unit_price_fen,
                status, occurred_at
            )
            SELECT 'truncated-' || value, 'test', 'truncated-' || value,
                   'context_ir', 'call', 5, 'UNKNOWN', now()
            FROM generate_series(1, 1001) AS value
            """
        )
    try:
        response = client.get("/api/control/profit/costs?lookback_days=7", headers=admin_headers)
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["record_total"] == 1002
        assert len(payload["records"]) == 1000
        assert payload["records_truncated"] is True
    finally:
        with psycopg.connect(profit_pg_dsn, autocommit=True) as conn:
            conn.execute("DELETE FROM operation_cost_records WHERE id LIKE 'truncated-%'")


def test_profit_and_cost_include_complete_first_shanghai_day(
    profit_pg_dsn: str, admin_headers: dict[str, str], client: TestClient
) -> None:
    with psycopg.connect(profit_pg_dsn, autocommit=True) as conn:
        conn.execute(
            """
            INSERT INTO generation_tasks (
                id, batch_id, generation_mode, provider, model, status, archive_status,
                prompt_snapshot_json, billed_seconds
            ) VALUES (
                't_first_shanghai_day', 'b1', 'I2V', 'metaso', 'MiniMax-H3',
                'SUCCEEDED', 'DIRECT', '{"resolution": "768P"}'::json, 3
            )
            """
        )
        conn.execute(
            """
            INSERT INTO daily_external_prices (
                price_date, price_768p_fen, price_2k_fen, created_at
            ) VALUES (
                (now() AT TIME ZONE 'Asia/Shanghai')::date - 6, 10, 20, now()
            ) ON CONFLICT (price_date) DO UPDATE SET price_768p_fen = 10
            """
        )
        conn.execute(
            """
            INSERT INTO wallet_transactions (
                id, user_id, type, available_delta, reserved_delta,
                task_id, billing_round, idempotency_key, created_at
            ) VALUES (
                'settle_first_shanghai_day', 'cust_1', 'SETTLE', 0, -3,
                't_first_shanghai_day', 1, 'settle:first-shanghai-day',
                to_char(
                    ((((now() AT TIME ZONE 'Asia/Shanghai')::date - 6 + time '00:10')
                        AT TIME ZONE 'Asia/Shanghai') AT TIME ZONE 'UTC'),
                    'YYYY-MM-DD HH24:MI:SS'
                )
            )
            """
        )
        conn.execute(
            """
            INSERT INTO operation_cost_records (
                id, source_type, source_id, subject, generation_task_id,
                unit, usage_amount, unit_price_fen, cost_fen, status,
                occurred_at, completed_at
            ) VALUES (
                'cost_first_shanghai_day', 'generation_task', 't_first_shanghai_day',
                'video_generation_768p', 't_first_shanghai_day', 'second',
                3, 9, 27, 'ACTUAL',
                (((now() AT TIME ZONE 'Asia/Shanghai')::date - 6 + time '00:10')
                    AT TIME ZONE 'Asia/Shanghai'),
                now()
            )
            """
        )
    try:
        profit = client.get("/api/control/profit/overview?lookback_days=7", headers=admin_headers)
        costs = client.get("/api/control/profit/costs?lookback_days=7", headers=admin_headers)
        assert profit.status_code == 200, profit.text
        assert costs.status_code == 200, costs.text
        first_day = str(
            (dt.datetime.now(dt.UTC) + dt.timedelta(hours=8)).date() - dt.timedelta(days=6)
        )
        profit_day = next(day for day in profit.json()["days"] if day["day"] == first_day)
        cost_day = next(day for day in costs.json()["days"] if day["day"] == first_day)
        assert profit_day["settled_seconds"] == 3
        assert profit_day["cost_fen"] == 27
        assert cost_day["video_768p_fen"] == 27
    finally:
        with psycopg.connect(profit_pg_dsn, autocommit=True) as conn:
            conn.execute("DELETE FROM operation_cost_records WHERE id = 'cost_first_shanghai_day'")
            conn.execute("DELETE FROM wallet_transactions WHERE id = 'settle_first_shanghai_day'")
            conn.execute("DELETE FROM generation_tasks WHERE id = 't_first_shanghai_day'")


def test_unknown_provider_usage_keeps_profit_unresolved(
    profit_pg_dsn: str, admin_headers: dict[str, str], client: TestClient
) -> None:
    with psycopg.connect(profit_pg_dsn, autocommit=True) as conn:
        conn.execute(
            """
            INSERT INTO operation_cost_records (
                id, source_type, source_id, subject, unit, unit_price_fen,
                status, occurred_at
            ) VALUES (
                'unknown_context_t1', 'generation_task', 't1', 'context_ir',
                'call', 5, 'UNKNOWN', now() - interval '1 day' + interval '2 hours'
            )
            """
        )

    response = client.get("/api/control/profit/overview?lookback_days=7", headers=admin_headers)
    assert response.status_code == 200, response.text
    day = next(item for item in response.json()["days"] if item["settled_seconds"] == 10)
    assert day["cost_fen"] == 90
    assert day["cost_unknown_count"] == 1
    assert day["gross_fen"] is None
    assert day["margin_pct"] is None


def test_utc_text_day_is_independent_of_database_session_timezone(profit_pg_dsn: str) -> None:
    from app.admin_profit_routes import _shanghai_day_utc_expression

    expression = _shanghai_day_utc_expression("'2026-09-04 10:00:00'")
    with psycopg.connect(profit_pg_dsn, autocommit=True) as conn:
        conn.execute("SET TIME ZONE 'America/Los_Angeles'")
        day = conn.execute(f"SELECT ({expression})::date").fetchone()[0]

    assert day.isoformat() == "2026-09-04"
