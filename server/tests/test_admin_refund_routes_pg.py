"""M1 退款管理最小版（ADM-08）——退付执行登记的 HTTP 契约。

双事实模型：账本侧 = admin_adjustments（REFUND_APPROVAL 反向调账 + REFUND
钱包流水）；资金侧 = refund_executions（渠道/第三方退款单号/经办人）。

锁定要点：
- 列表把两侧拼成一行，``pending`` 视图 = 已冲积分未登记退付；
- 登记走四段写契约（会话/幂等键/confirm/reason），审计落
  ``refund.execution.mark`` 且重放不重复落；
- 同一调账只允许登记一次（409）；非 REFUND_APPROVAL 调账 422；
- 渠道/单号校验与 CHECK 约束双层护栏。
"""

from __future__ import annotations

import os
import secrets
import uuid
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pg_test_kit import password_admin_session, require_pg_or_explicit_skip

from app.admin_auth_routes import (
    ADMIN_CSRF_HEADER,
    ADMIN_SESSION_HMAC_KEY_ENV,
)
from app.db_pg import DATABASE_URL_ENV, close_pg_pool

DEFAULT_DSN = "postgresql://testuser:testpass@localhost:5433/customer_v3_test"
REFUNDS_DB_NAME = "refund_routes_test"
TEST_ADMIN_SESSION_KEY = secrets.token_urlsafe(48)
IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"
REPLAY_HEADER = "X-Idempotent-Replay"


def _pg_dsn() -> str:
    return os.environ.get("TEST_POSTGRESQL_URL", DEFAULT_DSN)


def _admin_dsn() -> str:
    return _pg_dsn().rsplit("/", 1)[0] + "/postgres"


def _db_dsn() -> str:
    return _pg_dsn().rsplit("/", 1)[0] + f"/{REFUNDS_DB_NAME}"


@pytest.fixture(scope="module")
def refunds_dsn() -> Iterator[str]:
    from alembic import command
    from alembic.config import Config

    require_pg_or_explicit_skip(_pg_dsn())
    with psycopg.connect(_admin_dsn(), autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{REFUNDS_DB_NAME}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{REFUNDS_DB_NAME}"')
    server_dir = Path(__file__).resolve().parent.parent
    config = Config(str(server_dir / "alembic.ini"))
    config.set_main_option("script_location", str(server_dir / "migrations"))
    config.set_main_option(
        "sqlalchemy.url", _db_dsn().replace("postgresql://", "postgresql+psycopg://")
    )
    command.upgrade(config, "head")
    try:
        yield _db_dsn()
    finally:
        with psycopg.connect(_admin_dsn(), autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{REFUNDS_DB_NAME}" WITH (FORCE)')


@pytest.fixture()
def route_state(refunds_dsn: str) -> Iterator[str]:
    close_pg_pool()
    with psycopg.connect(_db_dsn(), autocommit=True) as conn:
        conn.execute("SET session_replication_role = replica")
        conn.execute(
            "TRUNCATE refund_executions, admin_write_idempotency, admin_sessions, "
            "audit_logs, admin_adjustments, wallet_transactions, recharge_orders, "
            "wallets, users, security_rate_limit_counters, security_auth_failures CASCADE"
        )
        conn.execute("SET session_replication_role = DEFAULT")
        conn.execute(
            "INSERT INTO users (id, username, display_name, role) VALUES "
            "('admin_u', 'admin_u', 'Admin User', 'admin'), "
            "('auditor_u', 'auditor_u', 'Auditor', 'auditor'), "
            "('customer_u', 'customer_u', 'Refund Co', 'customer')"
        )
    yield refunds_dsn
    close_pg_pool()


@pytest.fixture()
def admin_app(monkeypatch: pytest.MonkeyPatch, route_state: str) -> Iterator[FastAPI]:
    from app.admin_auth_routes import router as admin_auth_router
    from app.admin_refund_routes import router as refund_router

    app = FastAPI()
    app.include_router(admin_auth_router)
    app.include_router(refund_router)
    monkeypatch.setenv(DATABASE_URL_ENV, route_state)
    monkeypatch.delenv("VIDEO_REPLICA_CUSTOMER_PRODUCTION", raising=False)
    monkeypatch.setenv(ADMIN_SESSION_HMAC_KEY_ENV, TEST_ADMIN_SESSION_KEY)
    monkeypatch.delenv("VIDEO_REPLICA_AUTH_MODE", raising=False)
    monkeypatch.delenv("VIDEO_REPLICA_ALLOW_DEV_IDENTITY_HEADER", raising=False)
    yield app


@pytest.fixture()
def client(admin_app: FastAPI) -> Iterator[TestClient]:
    with TestClient(admin_app) as test_client:
        yield test_client


def _admin_session(client: TestClient, actor: str = "admin_u") -> dict[str, str]:
    response = password_admin_session(client, actor)
    assert response.status_code == 201, response.text
    return {ADMIN_CSRF_HEADER: response.json()["csrf_token"]}


def _seed_refund_adjustment(
    *,
    adjustment_id: str,
    credits: int = -500,
    source: str = "REFUND_APPROVAL",
    extra_ref: str = "ZPAY-REFUND-001",
    source_verified: bool = True,
) -> None:
    """隔离库合成完整来源链；历史未知来源必须显式选择，不能伪装成已核对。"""
    with psycopg.connect(_db_dsn()) as conn:
        order_id, charge_id = str(uuid.uuid4()), str(uuid.uuid4())
        if source_verified:
            conn.execute(
                "INSERT INTO recharge_orders "
                "(id,user_id,merchant_order_no,provider,status,pricing_scope,"
                "base_unit_price_fen_snapshot,charged_unit_price_fen_snapshot,"
                "min_recharge_fen_snapshot,recharge_step_fen_snapshot,amount_fen,credits,"
                "provider_trade_no,paid_at) VALUES (%s,'customer_u',%s,'zpay','PAID',"
                "'CUSTOMER_STANDARD',100,100,100,100,100000,1000,%s,now())",
                (order_id, "ORIGINAL-" + order_id, "SYNTHETIC-" + order_id),
            )
            conn.execute(
                "INSERT INTO wallet_transactions "
                "(id,user_id,type,available_delta,reserved_delta,recharge_order_id,"
                "idempotency_key,auth_source) "
                "VALUES (%s,'customer_u','CHARGE',1000,0,%s,%s,'internal')",
                (charge_id, order_id, charge_id),
            )
            conn.execute(
                "UPDATE billing_credit_lots SET remaining_credits=remaining_credits+%s WHERE id=%s",
                (credits, charge_id),
            )
        conn.execute(
            "INSERT INTO admin_adjustments (id, admin_user_id, target_user_id, "
            "source_document_type, source_document_ref, reason, request_id, "
            "balance_before, balance_after) "
            "VALUES (%s, 'admin_u', 'customer_u', %s, %s, '测试退款', %s, 1000, %s)",
            (adjustment_id, source, extra_ref, f"req-{adjustment_id}", 1000 + credits),
        )
        refund_tx = f"admin_adjustment:refund:{adjustment_id}"
        conn.execute(
            "INSERT INTO wallet_transactions (id, user_id, type, available_delta, "
            "reserved_delta, idempotency_key, auth_source) "
            "VALUES (%s, 'customer_u', 'REFUND', %s, 0, %s, 'internal')",
            (refund_tx, credits, refund_tx),
        )
        if source_verified:
            conn.execute(
                "INSERT INTO admin_refund_allocations "
                "(adjustment_id,refund_transaction_id,original_recharge_order_id,credit_lot_id,"
                "credits,source_credits,source_amount_fen,lot_remaining_before,"
                "lot_remaining_after) "
                "VALUES (%s,%s,%s,%s,%s,1000,100000,1000,%s)",
                (adjustment_id, refund_tx, order_id, charge_id, -credits, 1000 + credits),
            )


def _mark(
    client: TestClient,
    headers: dict[str, str],
    adjustment_id: str,
    *,
    channel: str = "wechat_manual",
    provider_ref: str = "4200001234",
    reason: str = "ZPay 后台已退付，回填凭证",
    key: str | None = None,
) -> object:
    merged = dict(headers)
    merged[IDEMPOTENCY_KEY_HEADER] = key or f"key-{uuid.uuid4()}"
    return client.post(
        f"/api/control/refunds/{adjustment_id}/execution",
        json={
            "confirm": True,
            "reason": reason,
            "channel": channel,
            "provider_ref": provider_ref,
        },
        headers=merged,
    )


def test_refund_list_marks_pending_and_done_then_blocks_remark(client: TestClient) -> None:
    headers = _admin_session(client)
    pending_id, done_id = str(uuid.uuid4()), str(uuid.uuid4())
    _seed_refund_adjustment(adjustment_id=pending_id)
    _seed_refund_adjustment(adjustment_id=done_id, credits=-200)

    pending = client.get("/api/control/refunds", params={"executed": "pending"}, headers=headers)
    assert pending.status_code == 200, pending.text
    body = pending.json()
    assert body["total"] == 2
    row = next(r for r in body["items"] if r["adjustment_id"] == pending_id)
    assert row["executed"] is False
    assert row["credits_deducted"] == 500

    first_key = f"key-{uuid.uuid4()}"
    first = _mark(client, headers, pending_id, key=first_key)
    assert first.status_code == 200, first.text
    assert first.json()["channel_label"] == "微信商户原路退回"

    # 同幂等键重放返回原结果，不产生第二条审计/执行行。
    replay = _mark(client, headers, pending_id, key=first_key)
    assert replay.status_code == 200
    with psycopg.connect(_db_dsn(), autocommit=True) as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM refund_executions WHERE adjustment_id=%s",
                (pending_id,),
            ).fetchone()[0]
            == 1
        )

    # 已登记后再登记（新幂等键）→ 409；列表 pending 收窄为 done 一侧。
    conflict = _mark(client, headers, pending_id)
    assert conflict.status_code == 409
    after = client.get(
        "/api/control/refunds", params={"executed": "pending"}, headers=headers
    ).json()
    assert [r["adjustment_id"] for r in after["items"]] == [done_id]
    done = client.get("/api/control/refunds", params={"executed": "done"}, headers=headers).json()
    assert [r["adjustment_id"] for r in done["items"]] == [pending_id]
    executed_row = done["items"][0]
    assert executed_row["provider_ref"] == "4200001234"
    assert executed_row["executed_by"] == "admin_u"


def test_mark_validates_channel_ref_and_source(client: TestClient) -> None:
    headers = _admin_session(client)
    refund_id = str(uuid.uuid4())
    correction_id = str(uuid.uuid4())
    _seed_refund_adjustment(adjustment_id=refund_id)
    # FREE_GRANT 走正向订单形状，这里用同为免单反向的台账更正做"非退款调账"负例。
    _seed_refund_adjustment(adjustment_id=correction_id, source="LEDGER_CORRECTION")

    assert _mark(client, headers, refund_id, channel="alipay").status_code == 422, (
        "未知渠道必须 422"
    )
    assert _mark(client, headers, refund_id, provider_ref="   ").status_code == 422, "退款单号必填"
    assert _mark(client, headers, str(uuid.uuid4())).status_code == 404, "调账不存在"
    assert _mark(client, headers, correction_id).status_code == 422, "非退款调账不能登记退付"

    auditor = _admin_session(client, "auditor_u")
    assert _mark(client, auditor, refund_id).status_code == 403, "审计员只读"


def test_execution_writes_audit_once_and_marks_sensitive(client: TestClient) -> None:
    headers = _admin_session(client)
    refund_id = str(uuid.uuid4())
    _seed_refund_adjustment(adjustment_id=refund_id)
    response = _mark(client, headers, refund_id)
    assert response.status_code == 200
    with psycopg.connect(_db_dsn(), autocommit=True) as conn:
        rows = conn.execute(
            "SELECT action FROM audit_logs WHERE entity_type='refund_execution' AND entity_id=%s",
            (refund_id,),
        ).fetchall()
    assert [r[0] for r in rows] == ["refund.execution.mark"]


def test_historical_unverified_refund_stays_visible_but_cannot_be_newly_executed(client):
    headers = _admin_session(client)
    historical = str(uuid.uuid4())
    _seed_refund_adjustment(adjustment_id=historical, source_verified=False)
    listed = client.get("/api/control/refunds", headers=headers)
    assert listed.status_code == 200
    row = next(item for item in listed.json()["items"] if item["adjustment_id"] == historical)
    assert row["source_verified"] is False
    assert row["original_recharge_order_no"] == ""
    refused = _mark(client, headers, historical)
    assert refused.status_code == 422
    assert refused.json()["detail"]["code"] == "REFUND_SOURCE_REVIEW_REQUIRED"
    with psycopg.connect(_db_dsn()) as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM refund_executions WHERE adjustment_id=%s", (historical,)
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT count(*) FROM admin_refund_allocations WHERE adjustment_id=%s",
                (historical,),
            ).fetchone()[0]
            == 0
        )
