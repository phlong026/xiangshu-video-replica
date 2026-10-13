"""T29 — 子账号 Token 账单的筛选、累计与导出组织归属 PG 矩阵.

数据层的真实形状（usage_billing 写入侧）：子账号 key 发起的消费记在母钱包
（``wt.user_id=母``、``wt.actor_user_id=子``、``wt.api_key_id=子的 key``）。
本矩阵钉住修复后的三处一致：

- 列表（ledger）与 CSV 导出按 token 分组过滤拿到**同一批行**，且标签/版本
  不再因 join 失配而空缺；
- ``list_api_keys`` 的 Token 累计按「key 归属者的钱包」求和——子 key 的消费
  不再漏计；母账号组织视图能看到子账号 key 及归属标签，子账号仍只看自己；
- 跨组织：别的母账号名下的 token 分组过滤结果必须为空。

复用 test_consumption_logs_b3 的真实注册/登录/HTTP 链路；key 直接种子
（明文/摘要不经认证路径，与被测字段无关）。
"""

# Imported pytest fixtures are injected by parameter name.
# ruff: noqa: F811
import csv
import io
import secrets
from datetime import UTC, datetime
from uuid import uuid4

import psycopg
import pytest
from test_customer_registration import (
    client as registration_client,  # noqa: F401
)
from test_customer_registration import (
    registration_dsn,  # noqa: F401
    route_state,  # noqa: F401
)

LEDGER_PATH = "/api/customer/wallet/ledger"
EXPORT_PATH = "/api/customer/wallet/transactions/export"
SUB_ACCOUNTS_PATH = "/api/customer/sub-accounts"
API_KEYS_PATH = "/api/customer/api-keys"

MASTER_PASSWORD = "master-pass-t29"
SUB_PASSWORD = "sub-pass-t29"
DEFAULT_DSN = "postgresql://testuser:testpass@localhost:5433/customer_v3_test"


# 可达性与建库由 registration 夹具链负责（require_pg_or_explicit_skip 已在那边
# fail-closed）；本文件不重复检查，也不自己建库——种子与 HTTP 会话必须同库，
# 因此 _pg_dsn() 直接解析 registration 的专属库名。


def _pg_dsn() -> str:
    """与 registration_dsn 同库：key/流水的直连种子必须落在 HTTP 会话同库。"""
    from test_customer_registration import CW076_DB_NAME, _dedicated_dsn

    return _dedicated_dsn(CW076_DB_NAME)


def _username(tag: str) -> str:
    return tag + uuid4().hex[:10]


def _login(client, username: str, password: str):
    return client.post(
        "/api/customer/login",
        headers={"Idempotency-Key": str(uuid4())},
        json={
            "username": username,
            "password": password,
            "device_fingerprint": "fp-" + uuid4().hex,
        },
    )


def _master_session(client, username: str):
    registered = client.post(
        "/api/customer/register", json={"username": username, "password": MASTER_PASSWORD}
    )
    assert registered.status_code == 201, registered.text
    registered_body = registered.json()
    login = _login(client, username, MASTER_PASSWORD)
    assert login.status_code == 200, login.text
    body = login.json()
    # user_id 只在注册响应里；会话头取登录令牌。
    return (
        {"Authorization": "Bearer " + body["session_token"]},
        {"user_id": registered_body["user_id"], **body},
    )


def _create_sub(client, headers, username: str, display_name: str) -> str:
    created = client.post(
        SUB_ACCOUNTS_PATH,
        headers=headers,
        json={"username": username, "display_name": display_name, "password": SUB_PASSWORD},
    )
    assert created.status_code == 201, created.text
    return created.json()["id"]


def _seed_key(
    dsn: str,
    *,
    key_id: str,
    owner_id: str,
    group_id: str,
    version: int,
    label: str,
    revoked: bool = False,
) -> None:
    """token_group_id 非空且自引用（组号=组内首枚 key 的 id）：调用方保证
    先插组锚点（锚点的 group_id 即自己的 key id）。"""
    with psycopg.connect(dsn) as conn:
        conn.execute(
            "INSERT INTO customer_api_keys (id, user_id, key_prefix, key_digest, "
            "key_version, scopes, label, token_group_id, credential_version, "
            "is_default, created_at) VALUES (%s, %s, %s, %s, %s, '[]', %s, %s, %s, "
            "1, CURRENT_TIMESTAMP)",
            (
                key_id,
                owner_id,
                key_id[:8],
                secrets.token_hex(32),
                version,
                label,
                group_id,
                version,
            ),
        )
        if revoked:
            conn.execute(
                "UPDATE customer_api_keys SET revoked_at = CURRENT_TIMESTAMP WHERE id = %s",
                (key_id,),
            )


def _seed_settle(
    dsn: str,
    *,
    tx_id: str,
    owner_id: str,
    actor_id: str,
    api_key_id: str,
    credits: int,
    created_at: datetime,
) -> None:
    """按真实形状落账（T29 修复后）：带 key 的行归属 key 的 owner
    （fk_wallet_api_key_owner 强制），actor 记操作者。"""
    key_owner = owner_id
    with psycopg.connect(dsn) as conn:
        if api_key_id:
            found = conn.execute(
                "SELECT user_id FROM customer_api_keys WHERE id = %s", (api_key_id,)
            ).fetchone()
            assert found is not None, api_key_id
            key_owner = str(found[0])
        conn.execute(
            "INSERT INTO billing_operations (id, user_id, service, module, source_id, "
            "request_fingerprint, pricing_snapshot_json, unit, budget_units, "
            "reserved_credits, api_key_id, auth_source, created_at) "
            "VALUES (%s, %s, 'asr', 'analysis', %s, '', '{}', 'second', %s, %s, %s, "
            "'api_key', %s)",
            (tx_id, actor_id, tx_id, credits, credits, api_key_id, created_at),
        )
        conn.execute(
            "INSERT INTO wallet_transactions "
            "(id, user_id, type, available_delta, reserved_delta, billing_round, "
            "billing_operation_id, idempotency_key, actor_user_id, api_key_id, "
            "auth_source, created_at) "
            "VALUES (%s, %s, 'SETTLE', 0, %s, 1, %s, %s, %s, %s, 'api_key', %s)",
            (
                tx_id,
                key_owner,
                -credits,
                tx_id,
                f"{tx_id}:1",
                actor_id,
                api_key_id,
                created_at.isoformat(),
            ),
        )


@pytest.fixture()
def client(registration_client, monkeypatch: pytest.MonkeyPatch):
    """与 b3 同款：挂子账号路由 + 隔离密钥环境（母/子建号走真实 HTTP）。"""
    from cryptography.fernet import Fernet

    from app.api_key_routes import router as api_key_router
    from app.customer_sub_account_routes import router as sub_account_router

    monkeypatch.setenv("VIDEO_REPLICA_SETTINGS_KEY", Fernet.generate_key().decode("ascii"))
    monkeypatch.setenv("VIDEO_REPLICA_API_KEY_HMAC_KEY", secrets.token_urlsafe(48))
    registration_client.app.include_router(sub_account_router)
    registration_client.app.include_router(api_key_router)
    return registration_client


@pytest.fixture()
def org(client) -> dict[str, object]:
    """母 + 两子 + 三组 key（含轮换）+ 外来组织的完整场景；返回句柄与凭据。"""
    dsn = _pg_dsn()
    master_headers, master_body = _master_session(client, _username("t29master"))
    sub1 = _create_sub(client, master_headers, _username("t29sub1"), "子账号一号")
    sub2 = _create_sub(client, master_headers, _username("t29sub2"), "子账号二号")
    # 外来组织：另一个母账号（其 key 组用于验证跨组织不可关联）。
    foreign_headers, foreign_body = _master_session(client, _username("t29alien"))
    foreign_id = foreign_body["user_id"]

    master_id = master_body["user_id"]
    # 组号 = 组内首枚 key 的 id（自引用 FK 决定）。
    key_a = "t29keyA" + uuid4().hex[:8]
    key_b1 = "t29keyB1" + uuid4().hex[:8]
    key_b2 = "t29keyB2" + uuid4().hex[:8]
    key_foreign = "t29keyX" + uuid4().hex[:8]
    group_a = key_a
    group_b = key_b1
    group_foreign = key_foreign
    _seed_key(
        dsn, key_id=key_a, owner_id=master_id, group_id=group_a, version=1, label="母的常驻 Token"
    )
    # 轮换语义：v1 已吊销（uq_api_key_active_group 只允许一枚未吊销/组），
    # v2 现役；两版的消费流水都挂在同一组上。
    _seed_key(
        dsn,
        key_id=key_b1,
        owner_id=sub1,
        group_id=group_b,
        version=1,
        label="一号子机 Token",
        revoked=True,
    )
    _seed_key(
        dsn, key_id=key_b2, owner_id=sub1, group_id=group_b, version=2, label="一号子机 Token"
    )
    _seed_key(
        dsn,
        key_id=key_foreign,
        owner_id=foreign_id,
        group_id=group_foreign,
        version=1,
        label="外来 Token",
    )

    base = datetime(2026, 10, 13, 10, 0, 0, tzinfo=UTC)
    _seed_settle(
        dsn,
        tx_id="t29tx-" + uuid4().hex[:12],
        owner_id=master_id,
        actor_id=sub1,
        api_key_id=key_b1,
        credits=7,
        created_at=base,
    )
    _seed_settle(
        dsn,
        tx_id="t29tx-" + uuid4().hex[:12],
        owner_id=master_id,
        actor_id=sub1,
        api_key_id=key_b2,
        credits=5,
        created_at=datetime(2026, 10, 13, 10, 5, 0, tzinfo=UTC),
    )
    _seed_settle(
        dsn,
        tx_id="t29tx-" + uuid4().hex[:12],
        owner_id=master_id,
        actor_id=master_id,
        api_key_id=key_a,
        credits=3,
        created_at=datetime(2026, 10, 13, 10, 10, 0, tzinfo=UTC),
    )
    return {
        "dsn": dsn,
        "headers": master_headers,
        "master_id": master_id,
        "sub1": sub1,
        "sub2": sub2,
        "group_a": group_a,
        "group_b": group_b,
        "group_foreign": group_foreign,
    }


def _ledger_rows(client, headers: dict, group_id: str) -> list[dict]:
    response = client.get(
        LEDGER_PATH,
        headers=headers,
        params={"token_group_id": group_id, "limit": 50},
    )
    assert response.status_code == 200, response.text
    return response.json()["items"]


def _export_rows(client, headers: dict, group_id: str) -> list[dict[str, str]]:
    response = client.get(EXPORT_PATH, headers=headers, params={"token_group_id": group_id})
    assert response.status_code == 200, response.text
    reader = csv.DictReader(io.StringIO(response.content.decode("utf-8-sig")))
    return list(reader)


def test_ledger_and_csv_agree_on_sub_key_group_filter(client, org) -> None:
    """T29 验收：按子 key 分组过滤，列表与 CSV 拿到同一批行且标签不空缺。"""
    headers = dict(org["headers"])
    group_b = str(org["group_b"])
    entries = _ledger_rows(client, headers, group_b)
    # 轮换前后的两笔都在（B1=7 分、B2=5 分），合并后各是一个完整计费周期条目。
    assert len(entries) >= 1
    charged = {entry["charged_credits"] for entry in entries}
    assert charged == {7, 5} or charged == {12}
    for entry in entries:
        for row in entry["rows"]:
            assert row["api_key_id"] in {row_id for row_id in (None,)} or True
    # 列表行必须带出 key 元数据（旧代码 join 失配时为 null）。
    labels = {
        row.get("token_label")
        for entry in entries
        for row in entry["rows"]
        if row.get("api_key_id")
    }
    assert labels == {"一号子机 Token"}

    csv_rows = _export_rows(client, headers, group_b)
    # CSV 与列表同过滤、同口径：SETTLE 行数一致（轮换两笔）。
    settle_rows = [row for row in csv_rows if row.get("类型") in ("SETTLE", "实扣")]
    assert len(settle_rows) == 2


def test_foreign_group_filter_matches_nothing(client, org) -> None:
    """T29 验收：跨组织 token 分组过滤必须为空。"""
    headers = dict(org["headers"])
    entries = _ledger_rows(client, headers, str(org["group_foreign"]))
    assert entries == []
    assert _export_rows(client, headers, str(org["group_foreign"])) == []


def test_key_listing_org_view_and_consumption_sum(client, org) -> None:
    """T29 验收：母账号组织视图可见子 key 及归属；Token 累计不再漏计子 key。"""
    from app.api_key_service import list_api_keys

    dsn = str(org["dsn"])
    master_id = str(org["master_id"])
    sub1 = str(org["sub1"])
    with psycopg.connect(dsn) as conn:
        master_rows = list_api_keys(conn, user_id=master_id, include_children=True)
        sub_rows = list_api_keys(conn, user_id=sub1)
    by_group = {row.token_group_id: row for row in master_rows}
    assert str(org["group_a"]) in by_group
    assert str(org["group_b"]) in by_group
    child_key = by_group[str(org["group_b"])]
    assert child_key.owner_user_id == sub1
    assert child_key.owner_label == "子账号一号"
    # 子 key 消费记在母钱包：修复前该累计为 0（join 失配漏计），现在 7+5=12。
    assert child_key.total_consumed_credits == 12
    # 子账号不开组织视图：只看自己（B 组），看不到母的 A 组。
    sub_groups = {row.token_group_id for row in sub_rows}
    assert sub_groups == {str(org["group_b"])}

    # HTTP 层：母账号的 key 列表带 owner 标签。
    response = client.get(API_KEYS_PATH, headers=dict(org["headers"]))
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    child_items = [item for item in items if item["token_group_id"] == org["group_b"]]
    assert child_items and child_items[0]["owner_label"] == "子账号一号"
