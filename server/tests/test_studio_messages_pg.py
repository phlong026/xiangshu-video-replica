"""统一消息中心（/api/studio/messages）真实 PostgreSQL 矩阵。

四类消息全部读时投影：任务批次/口播终态、钱包流水、公告、会话事件。
本模块验证同一个用户看到的信封口径：

- 同一批次的多条任务合成一条消息（部分失败、消耗积分、用时）；
- 账单消息与钱包账本同源（充值 +、结算 −、预扣退回 +），一笔计费周期只出一条；
- 公告已读回执落在 ``announcement_reads``（客户端横幅与消息中心共享事实）；
- 单条已读写回执、「全部已读」写分类水位、静音类别不再计未读；
- 旧接口 ``/api/studio/notifications`` 行为不变（老版本桌面端兼容）。
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

os.environ.setdefault(
    "VIDEO_REPLICA_ADMIN_SESSION_HMAC_KEY",
    "test-key-for-studio-messages-tests-48-bytes-minimum",
)

import psycopg
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from pg_test_kit import (
    create_test_database,
    drop_test_database,
    require_pg_or_explicit_skip,
    upgrade_test_database_to_head,
)

from app.auth import CurrentUser, get_current_user, get_current_user_short_tx
from app.customer_fence import get_business_db
from app.db_pg import DATABASE_URL_ENV, close_pg_pool, pg_transaction
from app.db_portable import BusinessConnection

STUDIO_MESSAGES_TEST_DB = "studio_messages_test"


@pytest.fixture(scope="module")
def messages_dsn() -> Iterator[str]:
    require_pg_or_explicit_skip()
    dsn = create_test_database(STUDIO_MESSAGES_TEST_DB)
    upgrade_test_database_to_head(dsn)
    try:
        yield dsn
    finally:
        drop_test_database(STUDIO_MESSAGES_TEST_DB)


@pytest.fixture()
def messages_db(messages_dsn: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    close_pg_pool()
    monkeypatch.setenv(DATABASE_URL_ENV, messages_dsn)
    monkeypatch.setenv("VIDEO_REPLICA_SETTINGS_KEY", Fernet.generate_key().decode("ascii"))
    monkeypatch.delenv("VIDEO_REPLICA_CUSTOMER_PRODUCTION", raising=False)
    with psycopg.connect(messages_dsn, autocommit=True) as pg:
        # 会话事件表默认拒绝 TRUNCATE / 改写；测试清理时挂起触发器（§036）。
        pg.execute("SET session_replication_role = replica")
        pg.execute(
            "TRUNCATE studio_notification_preferences, "
            "wallet_transactions, billing_operations, recharge_orders, wallets, "
            "generation_record_status_events, generation_tasks, generation_batches, "
            "projects, announcements, announcement_reads, customer_session_events, "
            "users CASCADE"
        )
        pg.execute("SET session_replication_role = DEFAULT")
    yield messages_dsn
    close_pg_pool()


def _seed(dsn: str) -> None:
    with psycopg.connect(dsn, autocommit=True) as pg:
        pg.execute(
            "INSERT INTO users (id, username, display_name, role) "
            "VALUES ('u1','u1','客户一','customer'), "
            "('u2','u2','客户二','customer')"
        )
        pg.execute(
            "INSERT INTO wallets (user_id, available_credits, reserved_credits) "
            "VALUES ('u1', 1000, 0)"
        )
        pg.execute(
            "INSERT INTO projects (id, owner_user_id, name) "
            "VALUES ('p1','u1','回村建房3'), ('p2','u2','别人的项目')"
        )
        pg.execute(
            "INSERT INTO generation_batches "
            "(id, project_id, created_by_user_id, idempotency_key, request_hash, "
            " request_snapshot_json, status, display_name) "
            "VALUES ('b1','p1','u1','ik-b1','hash-b1','{}','SUCCEEDED','示范批次'), "
            "('b2','p2','u2','ik-b2','hash-b2','{}','SUCCEEDED',NULL)"
        )
        pg.execute(
            "INSERT INTO generation_tasks "
            "(id, batch_id, provider, model, status, created_at, updated_at) VALUES "
            "('t-ok','b1','metaso','MiniMax-H3','SUCCEEDED', "
            " (now() - interval '40 minutes')::text, (now() - interval '30 minutes')::text), "
            "('t-bad','b1','metaso','MiniMax-H3','FAILED', "
            " (now() - interval '40 minutes')::text, (now() - interval '20 minutes')::text), "
            "('t-other','b2','metaso','MiniMax-H3','SUCCEEDED', "
            " (now() - interval '10 minutes')::text, (now() - interval '9 minutes')::text)"
        )
        pg.execute(
            "INSERT INTO generation_record_status_events "
            "(id, record_type, record_id, old_status, new_status, occurred_at) VALUES "
            "('ev-ok','VIDEO','t-ok','RUNNING','SUCCEEDED', now() - interval '30 minutes'), "
            "('ev-bad','VIDEO','t-bad','RUNNING','FAILED', now() - interval '20 minutes')"
        )
        pg.execute(
            "INSERT INTO billing_operations "
            "(id, user_id, service, module, source_id, pricing_snapshot_json, unit, "
            " budget_units, actual_units, reserved_credits, charged_credits, state, "
            " completed_at) "
            "VALUES ('op-1','u1','video_768p','video','t-ok','{}','second',"
            " 10, 8, 20, 18, 'SUCCEEDED', now() - interval '30 minutes')"
        )
        pg.execute(
            "INSERT INTO wallet_transactions "
            "(id, user_id, type, available_delta, reserved_delta, task_id, "
            " billing_round, billing_operation_id, idempotency_key, auth_source) "
            "VALUES ('settle-1','u1','SETTLE',0,-1,'t-ok',1,'op-1','idem-settle-1','session'), "
            "('release-1','u1','RELEASE',1,-1,'t-ok',1,'op-1','idem-release-1','session')"
        )
        pg.execute(
            "INSERT INTO recharge_orders "
            "(id, user_id, merchant_order_no, provider, status, pricing_scope, "
            " base_unit_price_fen_snapshot, charged_unit_price_fen_snapshot, "
            " min_recharge_fen_snapshot, recharge_step_fen_snapshot, amount_fen, "
            " credits, paid_at, prepay_id, code_url, transaction_id) "
            "VALUES ('ro-1','u1','ZH20261120001','wechat_native','PAID','INTERNAL',"
            " 10, 10, 1000, 1000, 10000, 1000, (now() - interval '2 hours')::text,"
            " 'prepay-1','weixin://wxpay/bizpayurl?pr=test','txn-1')"
        )
        pg.execute(
            "INSERT INTO wallet_transactions "
            "(id, user_id, type, available_delta, reserved_delta, recharge_order_id, "
            " idempotency_key, auth_source) "
            "VALUES ('charge-1','u1','CHARGE',1000,0,'ro-1','idem-charge-1','internal')"
        )
        pg.execute(
            "INSERT INTO announcements "
            "(id, title, body, tone, starts_at, ends_at, pinned, created_by_user_id) "
            "VALUES ('a1','系统维护通知','10 月 20 日 02:00-04:00 系统维护，请提前保存。',"
            " 'maintenance', now() - interval '1 hour', NULL, 1, 'u1')"
        )
        # 会话事件外键指向激活码与设备：落真实最小种子（用户 u1 名下一码一设备）。
        pg.execute(
            "INSERT INTO activation_code_batches "
            "(id, name, face_value_fen, unit_price_fen_snapshot, credits_snapshot, "
            " quantity, activation_expires_at, created_by_user_id) "
            "VALUES ('batch-1','测试批次',10000,10000,1000,1,"
            " (now() + interval '30 days')::text,'u1')"
        )
        pg.execute(
            "INSERT INTO activation_codes "
            "(id, batch_id, code_digest, digest_key_version, masked_code, status, "
            " bound_user_id, issued_at, activated_at) "
            "VALUES ('code-1','batch-1','digest-1',1,'****-1','ACTIVE','u1',"
            " (now() - interval '30 days')::text, (now() - interval '30 days')::text)"
        )
        pg.execute(
            "INSERT INTO customer_devices "
            "(id, activation_code_id, user_id, slot_no, display_name, platform, "
            " fingerprint_hmac, fingerprint_key_version, token_digest, token_key_version, "
            " status, bound_at) "
            "VALUES ('device-1','code-1','u1',1,'演示机','windows','fp-1',1,'tok-1',1,"
            " 'BOUND', (now() - interval '30 days')::text)"
        )
        pg.execute(
            "INSERT INTO customer_session_events "
            "(id, event, user_id, activation_code_id, device_id, session_id, "
            " session_epoch, reason) "
            "VALUES ('login-1','LOGIN','u1','code-1','device-1','session-1',1,NULL)"
        )


def actor(user_id: str, role: str) -> CurrentUser:
    return CurrentUser(  # type: ignore[arg-type]
        id=user_id, username=user_id, display_name=user_id, role=role
    )


class ScopedTestDb:
    def __init__(self, user_id: str, role: str = "customer") -> None:
        self.user_id = user_id
        self.role = role

    @contextmanager
    def write(self) -> Iterator[tuple[BusinessConnection, CurrentUser]]:
        with pg_transaction() as raw:
            yield BusinessConnection.postgres(raw), actor(self.user_id, self.role)


def _client(user_id: str, role: str = "customer") -> TestClient:
    from app.main import app

    app.dependency_overrides[get_current_user_short_tx] = app.dependency_overrides[
        get_current_user
    ] = lambda: actor(user_id, role)
    app.dependency_overrides[get_business_db] = lambda: ScopedTestDb(user_id, role)
    return TestClient(app)


def _by_id(items: list[dict[str, object]]) -> dict[str, dict[str, object]]:
    return {str(item["id"]): item for item in items}


def test_message_center_unifies_four_categories(messages_db: str) -> None:
    _seed(messages_db)
    client = _client("u1")
    try:
        response = client.get("/api/studio/messages")
        assert response.status_code == 200
        page = response.json()
        items = _by_id(page["items"])

        task = items["generation:b1"]
        assert task["category"] == "task"
        assert task["tone"] == "warning"
        assert task["status_label"] == "部分失败"
        assert "2 条视频" in task["summary"]
        assert "1 成功 / 1 失败" in task["summary"]
        assert "消耗 18 积分" in task["summary"]
        assert task["amount_credits"] == 18
        assert task["action"]["task_kind"] == "generation_batch"
        assert task["action"]["task_id"] == "b1"

        settle = items["billing:op-1"]
        assert settle["title"] == "扣费结算"
        assert settle["amount_credits"] == -18
        assert "预扣 20，退回 2" in settle["summary"]

        charge = items["billing:charge-1"]
        assert charge["title"] == "充值到账"
        assert charge["amount_credits"] == 1000
        assert "微信支付" in charge["summary"]
        assert "¥100.00" in charge["summary"]

        announcement = items["announcement:a1"]
        assert announcement["title"] == "系统维护通知"
        assert announcement["status_label"] == "置顶"
        assert announcement["detail"] == "10 月 20 日 02:00-04:00 系统维护，请提前保存。"

        login = items["security:login-1"]
        assert login["title"] == "新设备登录"
        assert login["category"] == "security"

        # 别的用户的任务/账单不出现在 u1 的消息里。
        assert "generation:b2" not in items
        assert page["by_category"] == {
            "task": 1,
            "billing": 2,
            "announcement": 1,
            "security": 1,
        }
        assert page["unread_count"] == 5

        # 旧接口继续按任务终态返回（老版本桌面端兼容）。
        legacy = client.get("/api/studio/notifications").json()
        assert legacy["enabled"] is True
        assert legacy["unread_count"] == 2
        assert {item["id"] for item in legacy["items"]} == {"generation:t-ok", "generation:t-bad"}
    finally:
        from app.main import app

        app.dependency_overrides.clear()


def test_single_read_receipt_and_category_watermark(messages_db: str) -> None:
    _seed(messages_db)
    client = _client("u1")
    try:
        read = client.post("/api/studio/messages/read", json={"keys": ["generation:b1"]})
        assert read.status_code == 200
        assert read.json()["marked"] == 1

        summary = client.get("/api/studio/messages/summary").json()
        assert summary["by_category"]["task"] == 0
        assert summary["total_unread"] == 4

        # 重复标记幂等：不再增加。
        assert (
            client.post("/api/studio/messages/read", json={"keys": ["generation:b1"]}).json()[
                "marked"
            ]
            == 0
        )

        marked = client.post("/api/studio/messages/read", json={"category": "billing"})
        assert marked.status_code == 200
        summary = client.get("/api/studio/messages/summary").json()
        assert summary["by_category"]["billing"] == 0
        assert summary["total_unread"] == 2

        # 公告「全部已读」同时写 announcement_reads：客户端横幅同步消失。
        client.post("/api/studio/messages/read", json={"category": "announcement"})
        with psycopg.connect(messages_db, autocommit=True) as pg:
            row = pg.execute(
                "SELECT user_id FROM announcement_reads WHERE announcement_id = 'a1'"
            ).fetchone()
        assert row is not None and row[0] == "u1"
    finally:
        from app.main import app

        app.dependency_overrides.clear()


def test_muted_category_stops_counting_and_auditor_cannot_mark(
    messages_db: str,
) -> None:
    _seed(messages_db)
    client = _client("u1")
    try:
        updated = client.put(
            "/api/studio/notification-preferences",
            json={"muted": ["billing"], "billing_style": "per_item"},
        )
        assert updated.status_code == 200
        summary = client.get("/api/studio/messages/summary").json()
        assert summary["by_category"]["billing"] == 0
        assert summary["total_unread"] == 3

        # 只提交 enabled 不冲掉已有 muted（exclude_unset 的回归护栏）。
        client.put("/api/studio/notification-preferences", json={"enabled": True})
        assert client.get("/api/studio/notification-preferences").json()["muted"] == ["billing"]
    finally:
        from app.main import app

        app.dependency_overrides.clear()

    auditor = _client("u2", "auditor")
    try:
        response = auditor.post("/api/studio/messages/read", json={"keys": ["generation:b1"]})
        assert response.status_code in (401, 403)
    finally:
        from app.main import app

        app.dependency_overrides.clear()


def test_daily_billing_style_collapses_per_day(messages_db: str) -> None:
    _seed(messages_db)
    client = _client("u1")
    try:
        client.put(
            "/api/studio/notification-preferences",
            json={"billing_style": "daily"},
        )
        items = client.get("/api/studio/messages?category=billing").json()["items"]
        assert len(items) == 1
        assert items[0]["kind"] == "daily"
        assert items[0]["amount_credits"] == 982
        assert "收入 +1000" in items[0]["summary"]
        assert "支出 -18" in items[0]["summary"]
    finally:
        from app.main import app

        app.dependency_overrides.clear()


def test_mark_all_keeps_legacy_notification_endpoint_in_sync(messages_db: str) -> None:
    """新旧客户端混用：新「全部已读」必须让旧接口也归零。"""
    _seed(messages_db)
    client = _client("u1")
    try:
        assert client.post("/api/studio/messages/read", json={"category": "all"}).status_code == 200
        legacy = client.get("/api/studio/notifications").json()
        assert legacy["unread_count"] == 0
        assert client.get("/api/studio/messages/summary").json()["total_unread"] == 0
    finally:
        from app.main import app

        app.dependency_overrides.clear()
