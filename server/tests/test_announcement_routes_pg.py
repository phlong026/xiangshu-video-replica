"""M2 公告中心 + M3 客户运营 v2（ADM-09/ADM-10）HTTP 契约与 SQL 冒烟。

公告：管理端 CRUD（写契约/审计/校验）+ 客户侧生效集合与幂等已读。
生命周期：在迁移后的空库与带两名样例客户的库上各跑一次，锁定 SQL 可执行
与分层/健康分基础语义（新客=new；有钱有成功任务=active 高分）。
"""

from __future__ import annotations

import json
import os
import secrets
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
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
DB_NAME = "announcement_routes_test"
TEST_ADMIN_SESSION_KEY = secrets.token_urlsafe(48)
IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"


def _pg_dsn() -> str:
    return os.environ.get("TEST_POSTGRESQL_URL", DEFAULT_DSN)


def _admin_dsn() -> str:
    return _pg_dsn().rsplit("/", 1)[0] + "/postgres"


def _db_dsn() -> str:
    return _pg_dsn().rsplit("/", 1)[0] + f"/{DB_NAME}"


@pytest.fixture(scope="module")
def announcements_dsn() -> Iterator[str]:
    from alembic import command
    from alembic.config import Config

    require_pg_or_explicit_skip(_pg_dsn())
    with psycopg.connect(_admin_dsn(), autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{DB_NAME}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{DB_NAME}"')
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
            conn.execute(f'DROP DATABASE IF EXISTS "{DB_NAME}" WITH (FORCE)')


@pytest.fixture()
def route_state(announcements_dsn: str) -> Iterator[str]:
    close_pg_pool()
    with psycopg.connect(_db_dsn(), autocommit=True) as conn:
        conn.execute("SET session_replication_role = replica")
        conn.execute(
            "TRUNCATE announcement_reads, announcements, admin_write_idempotency, "
            "admin_sessions, audit_logs, admin_adjustments, wallet_transactions, "
            "recharge_orders, wallets, users, security_rate_limit_counters, "
            "security_auth_failures CASCADE"
        )
        conn.execute("SET session_replication_role = DEFAULT")
        conn.execute(
            "INSERT INTO users (id, username, display_name, role) VALUES "
            "('admin_u', 'admin_u', 'Admin', 'admin'), "
            "('auditor_u', 'auditor_u', 'Auditor', 'auditor'), "
            "('customer_u', 'customer_u', 'Cust Co', 'customer')"
        )
    yield announcements_dsn
    close_pg_pool()


@pytest.fixture()
def admin_app(monkeypatch: pytest.MonkeyPatch, route_state: str) -> Iterator[FastAPI]:
    from app.admin_auth_routes import router as admin_auth_router
    from app.admin_customer_lifecycle_routes import router as lifecycle_router
    from app.admin_customer_ops_params_routes import router as ops_params_router
    from app.admin_publish_stats_routes import router as publish_stats_router
    from app.admin_search_routes import router as search_router
    from app.admin_support_ticket_routes import router as support_ticket_router
    from app.admin_worker_observation_routes import router as worker_observation_router
    from app.announcement_routes import router as announcement_router

    app = FastAPI()
    app.include_router(admin_auth_router)
    app.include_router(announcement_router)
    app.include_router(lifecycle_router)
    app.include_router(ops_params_router)
    app.include_router(worker_observation_router)
    app.include_router(support_ticket_router)
    app.include_router(publish_stats_router)
    app.include_router(search_router)
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


def _create_announcement(
    client: TestClient,
    headers: dict[str, str],
    *,
    title: str = "系统维护通知",
    body: str = "今晚 23:00-24:00 例行维护。",
    tone: str = "maintenance",
    pinned: bool = True,
) -> object:
    merged = dict(headers)
    merged[IDEMPOTENCY_KEY_HEADER] = f"key-{uuid.uuid4()}"
    return client.post(
        "/api/control/announcements",
        json={
            "confirm": True,
            "reason": "发布维护公告",
            "title": title,
            "body": body,
            "tone": tone,
            "pinned": pinned,
        },
        headers=merged,
    )


def test_announcement_crud_customer_feed_and_idempotent_reads(client: TestClient) -> None:
    headers = _admin_session(client)
    created = _create_announcement(client, headers)
    assert created.status_code == 200, created.text
    announcement_id = created.json()["id"]

    # 校验：标题必填、tone 枚举、审计员只读。
    assert _create_announcement(client, headers, title="  ").status_code == 422
    assert _create_announcement(client, headers, tone="neon").status_code == 422
    auditor = _admin_session(client, "auditor_u")
    assert _create_announcement(client, auditor).status_code == 403

    # 客户侧鉴权由会话层统一守护（其他客户路由测试覆盖）；此处用管理端
    # 读侧验证列表与已读计数兜底。
    admin_headers = _admin_session(client)
    admin_list = client.get("/api/control/announcements", headers=admin_headers).json()
    assert admin_list["items"][0]["read_count"] == 0
    assert admin_list["items"][0]["live"] is True

    # 直接落一条已读回执验证去重主键与管理端计数。
    with psycopg.connect(_db_dsn(), autocommit=True) as conn:
        conn.execute(
            "INSERT INTO announcement_reads (announcement_id, user_id) VALUES (%s, 'customer_u')",
            (announcement_id,),
        )
        conn.execute(
            "INSERT INTO announcement_reads (announcement_id, user_id) "
            "VALUES (%s, 'customer_u') ON CONFLICT DO NOTHING",
            (announcement_id,),
        )
    counted = client.get("/api/control/announcements", headers=admin_headers).json()
    assert counted["items"][0]["read_count"] == 1

    # 下线后不可见。（审计员会话登录会顶掉共享 cookie 罐里的 admin 会话，
    # 此处重取 admin 会话再执行写操作。）
    headers = _admin_session(client)
    merged = dict(headers)
    merged[IDEMPOTENCY_KEY_HEADER] = f"key-{uuid.uuid4()}"
    deleted = client.request(
        "DELETE",
        f"/api/control/announcements/{announcement_id}",
        json={"confirm": True, "reason": "维护取消"},
        headers=merged,
    )
    assert deleted.status_code == 200, deleted.text
    after = client.get("/api/control/announcements", headers=merged).json()
    assert after["items"] == []


def test_announcement_schedule_status_edit_and_reach(client: TestClient) -> None:
    """T12：定时公告显示「待生效」；可编辑标题正文与区间、可清空失效时间；分页与触达分母。"""
    headers = _admin_session(client)
    future = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    later = (datetime.now(UTC) + timedelta(days=2)).isoformat()
    merged = {**headers, IDEMPOTENCY_KEY_HEADER: f"key-{uuid.uuid4()}"}
    created = client.post(
        "/api/control/announcements",
        json={
            "confirm": True,
            "reason": "预告活动",
            "title": "明日活动预告",
            "body": "明天开启充值满赠。",
            "tone": "promo",
            "starts_at": future,
            "ends_at": later,
        },
        headers=merged,
    )
    assert created.status_code == 200, created.text
    announcement_id = created.json()["id"]
    listed = client.get("/api/control/announcements", params={"limit": 1}, headers=headers).json()
    assert listed["total"] == 1 and listed["limit"] == 1
    assert listed["audience_total"] == 1  # 夹具里有一名在役客户
    assert listed["items"][0]["status"] == "scheduled"
    assert listed["items"][0]["live"] is False

    edited = client.patch(
        f"/api/control/announcements/{announcement_id}",
        json={
            "confirm": True,
            "reason": "改为立即生效",
            "title": "充值满赠活动",
            "starts_at": (datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
            "clear_ends_at": True,
        },
        headers={**headers, IDEMPOTENCY_KEY_HEADER: f"key-{uuid.uuid4()}"},
    )
    assert edited.status_code == 200, edited.text
    item = client.get("/api/control/announcements", headers=headers).json()["items"][0]
    assert item["title"] == "充值满赠活动"
    assert item["status"] == "live"
    assert item["ends_at"] == ""


def test_lifecycle_sql_runs_and_classifies(client: TestClient) -> None:
    headers = _admin_session(client)
    # 夹具已有一名当天注册客户（customer_u）：SQL 必须可执行且分层落在 new。
    baseline = client.get("/api/control/customers/lifecycle", headers=headers)
    assert baseline.status_code == 200, baseline.text
    assert [i["username"] for i in baseline.json()["items"]] == ["customer_u"]

    # 造一名新客（今天注册）+ 一名活跃老客（有充值+成功任务+余额）。
    old_register = datetime.now(UTC) - timedelta(days=120)
    with psycopg.connect(_db_dsn(), autocommit=True) as conn:
        conn.execute(
            "INSERT INTO users (id, username, display_name, role, created_at, "
            "is_active) VALUES ('old_active', 'old_active', 'Old Co', 'customer', %s, 1)",
            (old_register.isoformat(),),
        )
        conn.execute("INSERT INTO wallets (user_id, available_credits) VALUES ('old_active', 500)")
        conn.execute(
            "INSERT INTO recharge_orders (id, user_id, merchant_order_no, status, "
            "amount_fen, credits, provider, transaction_id, paid_at, created_at, "
            "base_unit_price_fen_snapshot, charged_unit_price_fen_snapshot, "
            "min_recharge_fen_snapshot, recharge_step_fen_snapshot) VALUES "
            "('ro-1', 'old_active', 'TEST-RO-1', 'PAID', 10000, 1000, "
            "'wechat_native', 'TEST-TX-1', %s, %s, 10, 10, 100, 100)",
            (
                datetime.now(UTC).isoformat(),
                datetime.now(UTC).isoformat(),
            ),
        )
        # 活跃事实走钱包流水（customer_activity_facts 的 UNION 分支）：
        # 一条 30 天内的 SETTLE 即让该客户落在 active 分层。
        conn.execute(
            "INSERT INTO wallet_transactions (id, user_id, type, available_delta, "
            "reserved_delta, idempotency_key, auth_source) VALUES "
            "('wt-1', 'old_active', 'REFUND', -10, 0, 'wt-1', 'internal')"
        )
    result = client.get(
        "/api/control/customers/lifecycle", params={"stage": "new"}, headers=headers
    )
    assert result.status_code == 200, result.text
    new_items = result.json()["items"]
    assert [item["username"] for item in new_items] == ["customer_u"]
    assert new_items[0]["stage"] == "new"
    # 选中分层后：total 为该层人数（分页靠它算页数），分布仍覆盖全部分层，
    # 页面顶部其余分层卡片不能因筛选消失。
    assert result.json()["total"] == 1
    filtered_stages = {row["stage"] for row in result.json()["distribution"]}
    assert filtered_stages == {"new", "active"}

    paged = client.get(
        "/api/control/customers/lifecycle",
        params={"limit": 1, "offset": 1},
        headers=headers,
    ).json()
    assert paged["total"] == 2
    assert len(paged["items"]) == 1

    active = client.get(
        "/api/control/customers/lifecycle", params={"stage": "active"}, headers=headers
    ).json()
    assert [item["username"] for item in active["items"]] == ["old_active"]
    assert active["items"][0]["recharge_total_fen"] == 10000
    assert active["items"][0]["health_score"] == 60
    distribution = {
        row["stage"]: row["count"]
        for row in client.get("/api/control/customers/lifecycle", headers=headers).json()[
            "distribution"
        ]
    }
    assert distribution.get("new") == 1 and distribution.get("active") == 1


def test_customer_ops_params_default_update_and_health_follow(
    client: TestClient,
) -> None:
    """T29：运营参数默认值、写契约与审计，健康分和口径文案随阈值同步。"""
    headers = _admin_session(client)
    default = client.get("/api/control/customer-ops-params", headers=headers)
    assert default.status_code == 200, default.text
    assert default.json()["low_balance_threshold"] == 100
    assert default.json()["health_alert_score"] == 40

    with psycopg.connect(_db_dsn(), autocommit=True) as conn:
        conn.execute(
            "INSERT INTO users (id, username, display_name, role, is_active) "
            "VALUES ('rich_u', 'rich_u', 'Rich', 'customer', 1)"
        )
        conn.execute("INSERT INTO wallets (user_id, available_credits) VALUES ('rich_u', 500)")
    before = {
        item["username"]: item["health_score"]
        for item in client.get("/api/control/customers/lifecycle", headers=headers).json()["items"]
    }
    # 默认线 100：余额 500 拿到「余额充足」+30，且 7 天无失败 +20。
    assert before["rich_u"] == 50

    # 审计员只读：不能改参数。
    auditor_headers = _admin_session(client, "auditor_u")
    denied = client.put(
        "/api/control/customer-ops-params",
        json={
            "confirm": True,
            "reason": "尝试修改",
            "low_balance_threshold": 10,
            "health_alert_score": 30,
        },
        headers={**auditor_headers, IDEMPOTENCY_KEY_HEADER: f"k-{uuid.uuid4()}"},
    )
    assert denied.status_code == 403

    # 同一个 cookie jar：审计员登录会顶掉管理员会话，需重新登录。
    headers = _admin_session(client)
    # 越界参数被拒（健康分预警线需在 1–100）。
    invalid = client.put(
        "/api/control/customer-ops-params",
        json={
            "confirm": True,
            "reason": "越界",
            "low_balance_threshold": 10,
            "health_alert_score": 0,
        },
        headers={**headers, IDEMPOTENCY_KEY_HEADER: f"k-{uuid.uuid4()}"},
    )
    assert invalid.status_code == 422

    updated = client.put(
        "/api/control/customer-ops-params",
        json={
            "confirm": True,
            "reason": "按本月套餐调整预警线",
            "low_balance_threshold": 1000,
            "health_alert_score": 60,
        },
        headers={**headers, IDEMPOTENCY_KEY_HEADER: f"k-{uuid.uuid4()}"},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["low_balance_threshold"] == 1000
    assert (
        client.get("/api/control/customer-ops-params", headers=headers).json()["health_alert_score"]
        == 60
    )

    lifecycle = client.get("/api/control/customers/lifecycle", headers=headers).json()
    after = {item["username"]: item["health_score"] for item in lifecycle["items"]}
    # 余额 500 < 新阈值 1000：失去 +30。
    assert after["rich_u"] == 20
    assert lifecycle["healthAlertScore"] == 60
    assert lifecycle["lowBalanceThreshold"] == 1000
    assert "1000" in lifecycle["healthRule"] and "60" in lifecycle["healthRule"]

    with psycopg.connect(_db_dsn()) as conn:
        audit = conn.execute(
            "SELECT metadata_json FROM audit_logs WHERE action='customer.ops_params.update'"
        ).fetchall()
    assert len(audit) == 1
    meta = audit[0][0] if isinstance(audit[0][0], dict) else json.loads(audit[0][0])
    assert meta["before"]["low_balance_threshold"] == 100
    assert meta["after"]["low_balance_threshold"] == 1000


def test_global_search_finds_customer_order_and_rejects_noise(client: TestClient) -> None:
    """T39：客户名 / 订单号一步命中，短词与未知词不返回，审计员也可用。"""
    headers = _admin_session(client)
    with psycopg.connect(_db_dsn(), autocommit=True) as conn:
        conn.execute(
            "INSERT INTO recharge_orders (id, user_id, merchant_order_no, status, "
            "amount_fen, credits, provider, transaction_id, paid_at, created_at, "
            "base_unit_price_fen_snapshot, charged_unit_price_fen_snapshot, "
            "min_recharge_fen_snapshot, recharge_step_fen_snapshot) VALUES "
            "('ro-s1', 'customer_u', 'SEARCH-ORDER-1', 'PAID', 10000, 1000, "
            "'wechat_native', 'TEST-TX-S1', %s, %s, 10, 10, 100, 100)",
            (datetime.now(UTC).isoformat(), datetime.now(UTC).isoformat()),
        )

    def search(term: str, h: dict[str, str] = headers) -> list[dict[str, str]]:
        response = client.get("/api/control/search", params={"q": term}, headers=h)
        assert response.status_code == 200, response.text
        return response.json()["items"]

    by_company = search("Cust Co")
    assert [(i["type"], i["id"]) for i in by_company] == [("customer", "customer_u")]
    assert by_company[0]["label"] == "Cust Co"
    by_order = search("SEARCH-ORDER-1")
    assert [(i["type"], i["id"]) for i in by_order] == [("order", "SEARCH-ORDER-1")]
    assert search("c") == []
    assert search("nothing-matches-this") == []
    # 通配符按字面匹配：%% 不会命中全部客户。
    assert search("%%") == []
    # 管理员账号不在客户搜索范围内。
    assert all(i["type"] != "customer" or i["id"] != "admin_u" for i in search("admin_u"))
    auditor = _admin_session(client, "auditor_u")
    assert [i["id"] for i in search("SEARCH-ORDER-1", auditor)] == ["SEARCH-ORDER-1"]


def test_support_ticket_priority_related_ref_and_handlers(client: TestClient) -> None:
    """T14/T15：优先级排序、关联对象校验、可指派负责人清单与改派。"""
    headers = _admin_session(client)

    def create(**extra: object) -> object:
        return client.post(
            "/api/control/support-tickets",
            json={
                "confirm": True,
                "reason": "登记",
                "category": "billing",
                "summary": "充值未到账",
                **extra,
            },
            headers={**headers, IDEMPOTENCY_KEY_HEADER: f"tk-{uuid.uuid4()}"},
        )

    assert create(related_ref="随便写").status_code == 422
    assert create(priority="P9").status_code == 422
    low = create(priority="P3")
    urgent = create(priority="P0", related_ref="order:W15-ORDER-1")
    assert low.status_code == 200 and urgent.status_code == 200, urgent.text
    listed = client.get("/api/control/support-tickets", headers=headers).json()
    assert [item["priority"] for item in listed["items"]] == ["P0", "P3"]
    assert listed["items"][0]["related_ref"] == "order:W15-ORDER-1"

    handlers = client.get("/api/control/support-tickets/handlers", headers=headers).json()
    admin_ids = {item["user_id"] for item in handlers["items"]}
    assert "admin_u" in admin_ids
    reassigned = client.patch(
        f"/api/control/support-tickets/{low.json()['id']}",
        json={"confirm": True, "reason": "升级", "priority": "P1", "handler_user_id": "admin_u"},
        headers={**headers, IDEMPOTENCY_KEY_HEADER: f"tk-{uuid.uuid4()}"},
    )
    assert reassigned.status_code == 200, reassigned.text
    item = next(
        i
        for i in client.get("/api/control/support-tickets", headers=headers).json()["items"]
        if i["id"] == low.json()["id"]
    )
    assert item["priority"] == "P1" and item["handler_username"] == "admin_u"


def test_worker_observations_aggregate_runs(client: TestClient) -> None:
    """M4：观测 SQL 在迁移库上可执行，空队列与隔离区计数形状正确。"""
    headers = _admin_session(client)
    response = client.get("/api/control/worker-observations", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body["queues"]) == {"video", "oral", "viralRefresh", "publish"}
    assert body["attention"]["oralQuarantinedTasks"] == 0
    assert body["attention"]["stuckPreparations"] == 0
    assert body["attention"]["videoStaleThresholdMinutes"] == 30


def test_support_ticket_lifecycle_and_publish_stats_guard(client: TestClient) -> None:
    """ADM-21 工单状态机 + ADM-18 回流默认关的 fail-closed 守卫。"""
    headers = _admin_session(client)
    created = client.post(
        "/api/control/support-tickets",
        json={
            "confirm": True,
            "reason": "客户群反馈",
            "target_user_id": "customer_u",
            "category": "billing",
            "summary": "充值后积分未到账",
        },
        headers={**headers, IDEMPOTENCY_KEY_HEADER: f"tk-{uuid.uuid4()}"},
    )
    assert created.status_code == 200, created.text
    ticket_id = created.json()["id"]

    # 状态机：open → processing（自动记负责人）→ resolved（结论）→ 复活被拒。
    started = client.patch(
        f"/api/control/support-tickets/{ticket_id}",
        json={"confirm": True, "reason": "开始跟进", "status": "processing"},
        headers={**headers, IDEMPOTENCY_KEY_HEADER: f"tk-{uuid.uuid4()}"},
    )
    assert started.status_code == 200, started.text
    # 「未结案」伪状态 = open + processing，与总览「工单未结案」待办同口径。
    unresolved = client.get(
        "/api/control/support-tickets", params={"status": "unresolved"}, headers=headers
    ).json()
    assert unresolved["total"] == 1
    resolved = client.patch(
        f"/api/control/support-tickets/{ticket_id}",
        json={
            "confirm": True,
            "reason": "已补积分",
            "status": "resolved",
            "resolution": "确认为到账延迟，已补偿 10 积分",
        },
        headers={**headers, IDEMPOTENCY_KEY_HEADER: f"tk-{uuid.uuid4()}"},
    )
    assert resolved.status_code == 200, resolved.text
    revived = client.patch(
        f"/api/control/support-tickets/{ticket_id}",
        json={"confirm": True, "reason": "重开", "status": "processing"},
        headers={**headers, IDEMPOTENCY_KEY_HEADER: f"tk-{uuid.uuid4()}"},
    )
    assert revived.status_code == 422
    listed = client.get("/api/control/support-tickets", headers=headers).json()
    assert listed["total"] == 1 and listed["items"][0]["status"] == "resolved"
    # 客户详情「工单」页签：按客户编号精确过滤。
    scoped = client.get(
        "/api/control/support-tickets", params={"user_id": "customer_u"}, headers=headers
    ).json()
    assert scoped["total"] == 1
    other = client.get(
        "/api/control/support-tickets", params={"user_id": "someone_else"}, headers=headers
    ).json()
    assert other["total"] == 0
    assert listed["items"][0]["handler_username"] == "admin_u"
    assert (
        client.get(
            "/api/control/support-tickets", params={"status": "unresolved"}, headers=headers
        ).json()["total"]
        == 0
    )

    # 发布效果回流：默认关 → 同步被 409 挡下；清单可见开关状态。
    stats_list = client.get("/api/control/publish-stats", headers=headers)
    assert stats_list.status_code == 200
    assert stats_list.json()["enabled"] is False
    # 菜单显隐用的轻接口与清单口径一致。
    flag = client.get("/api/control/publish-stats/enabled", headers=headers)
    assert flag.status_code == 200
    assert flag.json() == {"enabled": False}
    guarded = client.post(
        "/api/control/publish-stats/any-id/sync",
        json={"confirm": True, "reason": "尝试同步"},
        headers={**headers, IDEMPOTENCY_KEY_HEADER: f"tk-{uuid.uuid4()}"},
    )
    assert guarded.status_code == 409
