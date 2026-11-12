"""平台培训素材库（123网盘车道）HTTP 契约测试。

覆盖：管理端 multipart 上传（四组白名单/超限/写契约/标签必填/幂等重放/
审计员只读）、部分更新（含 tags）与软删、客户只读列表的可见性/类型与标签
筛选/标签面/分页、防盗链直链签发与下载计数、管理端 direct-link 不计数、
上传提交时的配置指纹复核（换配置拒绝入库）、CHECK 放宽迁移的带行回滚。
存储全部走 FakePan123Storage（离线替身），不触网。
"""

from __future__ import annotations

import base64
import os
import secrets
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

os.environ.setdefault(
    "VIDEO_REPLICA_ADMIN_SESSION_HMAC_KEY",
    "test-key-for-training-material-tests-minimum-48-bytes-123456",
)

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
from app.pan123_client import Pan123File

DEFAULT_DSN = "postgresql://testuser:testpass@localhost:5433/customer_v3_test"
DB_NAME = "training_materials_test"
TEST_ADMIN_SESSION_KEY = secrets.token_urlsafe(48)
TEST_ACTIVATION_KEY = secrets.token_urlsafe(48)
ACTIVATION_CODE_HMAC_KEY_ENV = "VIDEO_REPLICA_ACTIVATION_CODE_HMAC_KEY"
FUTURE_EXPIRY = "2099-01-01T00:00:00+00:00"
IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"

# 各格式魔数开头；上传白名单按 (后缀, content-type) 成对判定。
MP4_BYTES = b"\x00\x00\x00\x18ftypisom" + b"x" * 64
PDF_BYTES = b"%PDF-1.7\n" + b"y" * 64
MP3_BYTES = b"ID3" + b"m" * 64
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"p" * 64


class FakePan123Storage:
    """路由用到的最小存储面离线替身（put_file/resolve_direct_link）。"""

    provider = "pan123"

    def __init__(self) -> None:
        self._seq = 0
        self.remote_names: list[str] = []
        self.links_served = 0

    def put_file(self, remote_name: str, path, *, content_type: str) -> Pan123File:
        self.remote_names.append(remote_name)
        self._seq += 1
        return Pan123File(file_id=self._seq, size=path.stat().st_size)

    def resolve_direct_link(self, file_id: int, *, ttl_seconds: int = 1800) -> tuple[str, int]:
        self.links_served += 1
        expire = int(time.time()) + ttl_seconds
        url = f"https://cdn.test.invalid/direct/1000/{file_id}/f?auth_key={expire}-r-1-0"
        return url, expire


def _pg_dsn() -> str:
    return os.environ.get("TEST_POSTGRESQL_URL", DEFAULT_DSN)


def _admin_dsn() -> str:
    return _pg_dsn().rsplit("/", 1)[0] + "/postgres"


def _db_dsn() -> str:
    return _pg_dsn().rsplit("/", 1)[0] + f"/{DB_NAME}"


@pytest.fixture(scope="module")
def training_materials_dsn() -> Iterator[str]:
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
def route_state(training_materials_dsn: str) -> Iterator[str]:
    close_pg_pool()
    with psycopg.connect(_db_dsn(), autocommit=True) as conn:
        conn.execute("SET session_replication_role = replica")
        conn.execute(
            "TRUNCATE training_materials, customer_session_events, "
            "customer_session_state, customer_idempotency_envelopes, "
            "device_pairing_requests, customer_devices, activation_code_events, "
            "activation_code_activations, activation_code_deliveries, "
            "activation_code_exports, activation_codes, activation_code_batches, "
            "admin_write_idempotency, admin_sessions, admin_adjustments, "
            "wallet_transactions, recharge_orders, wallets, audit_logs, users, "
            "projects, customer_fencing_write_evidence, "
            "security_rate_limit_counters, security_auth_failures, "
            "runtime_settings, provider_settings, provider_runtime_state CASCADE"
        )
        conn.execute("SET session_replication_role = DEFAULT")
        conn.execute(
            "INSERT INTO users (id, username, display_name, role) VALUES "
            "('admin_u', 'admin_u', 'Admin User', 'admin'), "
            "('auditor_u', 'auditor_u', 'Auditor User', 'auditor'), "
            "('customer_u', 'customer_u', 'Customer User', 'customer')"
        )
        conn.execute(
            "INSERT INTO runtime_settings "
            "(id, max_generation_count_per_batch, max_concurrent_h3_tasks, "
            " internal_base_unit_price_fen, min_recharge_fen, recharge_step_fen) "
            "VALUES (1, 4, 2, 1000, 10000, 1000)"
        )
    yield training_materials_dsn
    close_pg_pool()


@pytest.fixture()
def app_under_test(
    route_state: str, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[FastAPI, FakePan123Storage, dict[str, str]]]:
    from app import training_material_routes
    from app.activation_code_routes import router as activation_code_router
    from app.admin_auth_routes import router as admin_auth_router
    from app.customer_session_routes import router as customer_session_router
    from app.training_material_routes import router as training_material_router

    app = FastAPI()
    for router in (
        admin_auth_router,
        customer_session_router,
        activation_code_router,
        training_material_router,
    ):
        app.include_router(router)
    monkeypatch.setenv(DATABASE_URL_ENV, route_state)
    monkeypatch.delenv("VIDEO_REPLICA_CUSTOMER_PRODUCTION", raising=False)
    monkeypatch.setenv(ADMIN_SESSION_HMAC_KEY_ENV, TEST_ADMIN_SESSION_KEY)
    monkeypatch.setenv(ACTIVATION_CODE_HMAC_KEY_ENV, TEST_ACTIVATION_KEY)
    monkeypatch.setenv(
        "VIDEO_REPLICA_DEVICE_FINGERPRINT_HMAC_KEY",
        "training-material-device-key-0123456789abcdef",
    )
    # AEAD 密钥必须是 base64url 且解码恰 32 字节（AESGCM 要求）。
    monkeypatch.setenv(
        "VIDEO_REPLICA_CUSTOMER_IDEMPOTENCY_AEAD_KEY",
        base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii").rstrip("="),
    )
    # settings 密钥须能构造 Fernet（32 字节、带 padding 的 urlsafe base64）。
    monkeypatch.setenv(
        "VIDEO_REPLICA_SETTINGS_KEY",
        base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii"),
    )
    monkeypatch.delenv("VIDEO_REPLICA_AUTH_MODE", raising=False)
    monkeypatch.delenv("VIDEO_REPLICA_ALLOW_DEV_IDENTITY_HEADER", raising=False)
    # 网盘车道替身 + 可变的配置指纹（指纹复核用例切换它）。种子 pan123 行
    # 使「业务提交时读 DB 版本」与注入版本同源（与生产读真实配置一致）。
    with psycopg.connect(_db_dsn(), autocommit=True) as conn:
        conn.execute(
            "INSERT INTO provider_settings (provider, encrypted_config, "
            "updated_by_user_id, created_at, updated_at) "
            "VALUES ('pan123', 'seed', 'admin_u', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP) "
            "ON CONFLICT (provider) DO NOTHING"
        )
        seeded = conn.execute(
            "SELECT updated_at FROM provider_settings WHERE provider = 'pan123'"
        ).fetchone()
    fake_storage = FakePan123Storage()
    version_holder: dict[str, str] = {"version": str(seeded[0])}
    monkeypatch.setattr(
        training_material_routes,
        "get_training_material_storage",
        lambda: (fake_storage, version_holder["version"]),
    )
    yield app, fake_storage, version_holder
    app.dependency_overrides.clear()


@pytest.fixture()
def client(
    app_under_test: tuple[FastAPI, FakePan123Storage, dict[str, str]],
) -> Iterator[TestClient]:
    with TestClient(app_under_test[0]) as test_client:
        yield test_client


def _admin_session(client: TestClient, actor: str = "admin_u") -> dict[str, str]:
    response = password_admin_session(client, actor)
    assert response.status_code == 201, response.text
    return {ADMIN_CSRF_HEADER: response.json()["csrf_token"]}


def _upload_material(
    client: TestClient,
    headers: dict[str, str],
    *,
    filename: str = "新手教学.mp4",
    content: bytes = MP4_BYTES,
    content_type: str = "video/mp4",
    title: str = "新手教学",
    tags: str = '["入门教程"]',
    visible: bool = True,
    idempotency_key: str | None = None,
) -> object:
    merged = dict(headers)
    merged[IDEMPOTENCY_KEY_HEADER] = idempotency_key or f"key-{uuid.uuid4()}"
    return client.post(
        "/api/control/training-materials",
        data={
            "title": title,
            "description": "平台统一提供的培训内容",
            "tags": tags,
            "sort_order": 0,
            "visible": "true" if visible else "false",
            "confirm": "true",
            "reason": "上传培训素材",
        },
        files={"file": (filename, content, content_type)},
        headers=merged,
    )


def _customer_token(client: TestClient) -> str:
    from app.activation_code_service import compute_code_digest

    code = "XS04-TM0TEST-BBBBBBB-CCCCCCC-DDDDDDD"
    with psycopg.connect(_db_dsn(), autocommit=True) as conn:
        conn.execute(
            "INSERT INTO activation_code_batches "
            "(id, name, face_value_fen, unit_price_fen_snapshot, credits_snapshot, "
            "quantity, activation_expires_at, status, created_by_user_id) "
            "VALUES ('tm-batch', 'tm-batch', 1500, 1000, 100, 1, "
            f"'{FUTURE_EXPIRY}', 'OPEN', 'admin_u') ON CONFLICT (id) DO NOTHING"
        )
        conn.execute(
            "INSERT INTO activation_codes "
            "(id, batch_id, code_digest, digest_key_version, masked_code, "
            "status, issued_at) VALUES ('code-tm', 'tm-batch', %s, "
            "1, 'XS04-****', 'ISSUED', '2026-01-01T00:00:00+00:00') "
            "ON CONFLICT (id) DO NOTHING",
            (compute_code_digest(code, key=TEST_ACTIVATION_KEY.encode("utf-8")),),
        )
    response = client.post(
        "/api/customer/activate",
        json={
            "activation_code": code,
            "device_fingerprint": "tm-fp",
            "device_name": "TM device",
            "device_platform": "windows",
        },
        headers={"Idempotency-Key": "tm-activate"},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["session_token"])


def test_admin_upload_validation_idempotency_and_tags(
    client: TestClient,
    app_under_test: tuple[FastAPI, FakePan123Storage, dict[str, str]],
) -> None:
    storage = app_under_test[1]
    headers = _admin_session(client)

    created = _upload_material(client, headers)
    assert created.status_code == 200, created.text
    material_id = created.json()["id"]
    # 远端名唯一（material_id 前缀）且已上报网盘替身。
    assert len(storage.remote_names) == 1
    assert storage.remote_names[0].endswith("-新手教学.mp4")
    with psycopg.connect(_db_dsn(), autocommit=True) as conn:
        row = conn.execute(
            "SELECT remote_file_id, tags, kind FROM training_materials WHERE id = %s",
            (material_id,),
        ).fetchone()
        assert int(row[0]) == 1
        assert list(row[1]) == ["入门教程"]
        assert row[2] == "video"

    # 四组白名单：audio/image 同样放行（kind 推导）。
    assert (
        _upload_material(
            client,
            headers,
            filename="课.mp3",
            content=MP3_BYTES,
            content_type="audio/mpeg",
            title="口播音",
            tags='["音频课"]',
        ).status_code
        == 200
    )
    assert (
        _upload_material(
            client,
            headers,
            filename="图.png",
            content=PNG_BYTES,
            content_type="image/png",
            title="封面图",
            tags='["海报"]',
        ).status_code
        == 200
    )

    # 白名单外的 (后缀, content-type) 组合 → 415；混搭同样拒绝。
    assert (
        _upload_material(
            client,
            headers,
            filename="a.exe",
            content=b"MZ...",
            content_type="application/octet-stream",
        ).status_code
        == 415
    )
    assert _upload_material(client, headers, content_type="application/pdf").status_code == 415
    # 标题缺失 / 标签缺失 / 标签超限 / 单标签过长 → 422；审计员无写权限 → 403。
    assert _upload_material(client, headers, title="  ").status_code == 422
    assert _upload_material(client, headers, tags="[]").status_code == 422
    assert (
        _upload_material(client, headers, tags='["a", "b", "c", "d", "e", "f"]').status_code == 422
    )
    assert (
        _upload_material(
            client, headers, tags='["这一条标签内容明显超过了二十个字的长度限制"]'
        ).status_code
        == 422
    )
    auditor = _admin_session(client, "auditor_u")
    assert _upload_material(client, auditor).status_code == 403
    # TestClient 的 cookie jar 已被 auditor 会话覆盖，换回管理员会话再验证契约。
    headers = _admin_session(client)
    # 缺写契约（无 confirm/reason 的裸 multipart）→ 400。
    merged = dict(headers)
    merged[IDEMPOTENCY_KEY_HEADER] = f"key-{uuid.uuid4()}"
    assert (
        client.post(
            "/api/control/training-materials",
            data={"title": "x", "tags": '["x"]', "confirm": "false", "reason": ""},
            files={"file": ("a.mp4", MP4_BYTES, "video/mp4")},
            headers=merged,
        ).status_code
        == 400
    )

    # 幂等重放：同键同请求 → 回放，不产生第二行、不再上传。
    replay_key = f"replay-{uuid.uuid4()}"
    first = _upload_material(client, headers, title="重放用", idempotency_key=replay_key)
    assert first.status_code == 200
    second = _upload_material(client, headers, title="重放用", idempotency_key=replay_key)
    assert second.status_code in (200, 201)
    assert second.headers.get("X-Idempotent-Replay") == "true"
    with psycopg.connect(_db_dsn(), autocommit=True) as conn:
        rows = conn.execute(
            "SELECT count(*) FROM training_materials WHERE title = '重放用'"
        ).fetchone()
        assert rows[0] == 1

    # 同键不同文件 / 同键不同标签 → 409（内容指纹与规范化标签都在契约体）。
    conflict = _upload_material(
        client,
        headers,
        title="重放用",
        content=MP4_BYTES + b"different",
        idempotency_key=replay_key,
    )
    assert conflict.status_code == 409
    conflict_tags = _upload_material(
        client,
        headers,
        title="重放用",
        tags='["别的标签"]',
        idempotency_key=replay_key,
    )
    assert conflict_tags.status_code == 409

    with psycopg.connect(_db_dsn(), autocommit=True) as conn:
        audit = conn.execute(
            "SELECT action FROM audit_logs "
            "WHERE entity_type = 'training_material' AND entity_id = %s",
            (material_id,),
        ).fetchall()
        assert {row[0] for row in audit} == {"training_material.create"}


def test_customer_listing_tag_filter_facet_and_direct_link(
    client: TestClient,
    app_under_test: tuple[FastAPI, FakePan123Storage, dict[str, str]],
) -> None:
    storage = app_under_test[1]
    headers = _admin_session(client)
    video_id = _upload_material(
        client, headers, title="封面视频", tags='["入门教程", "剪映"]'
    ).json()["id"]
    _upload_material(
        client,
        headers,
        title="讲义",
        filename="讲义.pdf",
        content=PDF_BYTES,
        content_type="application/pdf",
        tags='["结算"]',
    )
    _upload_material(
        client,
        headers,
        title="口播音",
        filename="课.mp3",
        content=MP3_BYTES,
        content_type="audio/mpeg",
        tags='["入门教程"]',
        visible=False,
    )

    token = _customer_token(client)
    auth = {"Authorization": f"Bearer {token}"}

    # 未登录不可读列表。
    assert client.get("/api/training-materials").status_code == 401
    listing = client.get("/api/training-materials", headers=auth)
    assert listing.status_code == 200, listing.text
    payload = listing.json()
    titles = {item["title"] for item in payload["items"]}
    assert titles == {"封面视频", "讲义"}
    assert payload["total"] == 2
    kinds = {item["title"]: item["kind"] for item in payload["items"]}
    assert kinds["封面视频"] == "video"
    assert kinds["讲义"] == "doc"
    # 标签面按分页前可见集合聚合（下架素材的标签不入面）。
    assert sorted(payload["tag_facet"]) == sorted(["入门教程", "剪映", "结算"])

    # 类型筛选 + 标签筛选（可叠加）+ 无效类型。
    video_only = client.get(
        "/api/training-materials", params={"kind": "video"}, headers=auth
    ).json()
    assert [item["title"] for item in video_only["items"]] == ["封面视频"]
    tagged = client.get("/api/training-materials", params={"tag": "入门教程"}, headers=auth).json()
    assert [item["title"] for item in tagged["items"]] == ["封面视频"]
    both = client.get(
        "/api/training-materials",
        params={"kind": "doc", "tag": "结算"},
        headers=auth,
    ).json()
    assert [item["title"] for item in both["items"]] == ["讲义"]
    assert (
        client.get("/api/training-materials", params={"kind": "neon"}, headers=auth).status_code
        == 422
    )

    # 直链签发：防盗链签名 URL + 到期时间；下载计数自增。
    before_page = client.get("/api/control/training-materials", headers=headers).json()
    before = {item["title"]: item["download_count"] for item in before_page["items"]}
    download = client.post(f"/api/training-materials/{video_id}/download-url", headers=auth)
    assert download.status_code == 200, download.text
    body = download.json()
    assert "auth_key=" in body["url"]
    assert int(body["expires_at"]) > int(time.time())
    assert storage.links_served == 1
    after = client.get("/api/control/training-materials", headers=headers).json()
    after_counts = {item["title"]: item["download_count"] for item in after["items"]}
    assert after_counts["封面视频"] == before["封面视频"] + 1

    # 管理端 direct-link：可签（AdminReader 权限）、不递增计数。
    direct = client.get(f"/api/control/training-materials/{video_id}/direct-link", headers=headers)
    assert direct.status_code == 200, direct.text
    assert "auth_key=" in direct.json()["url"]
    final = client.get("/api/control/training-materials", headers=headers).json()
    final_counts = {item["title"]: item["download_count"] for item in final["items"]}
    assert final_counts["封面视频"] == after_counts["封面视频"]


def test_update_soft_delete_blocks_resign(
    client: TestClient,
    app_under_test: tuple[FastAPI, FakePan123Storage, dict[str, str]],
) -> None:
    storage = app_under_test[1]
    headers = _admin_session(client)
    material_id = _upload_material(client, headers, title="待更新").json()["id"]
    token = _customer_token(client)
    auth = {"Authorization": f"Bearer {token}"}

    # 编辑（含 tags 修改）：PATCH tags 进契约。
    merged = dict(headers)
    merged[IDEMPOTENCY_KEY_HEADER] = f"key-{uuid.uuid4()}"
    patched = client.patch(
        f"/api/control/training-materials/{material_id}",
        json={
            "confirm": True,
            "reason": "改标签",
            "tags": ["进阶课"],
            "visible": False,
        },
        headers=merged,
    )
    assert patched.status_code == 200, patched.text
    with psycopg.connect(_db_dsn(), autocommit=True) as conn:
        tags = conn.execute(
            "SELECT tags, visible FROM training_materials WHERE id = %s",
            (material_id,),
        ).fetchone()
        assert list(tags[0]) == ["进阶课"]
        assert int(tags[1]) == 0
    assert client.get("/api/training-materials", headers=auth).json()["total"] == 0
    assert (
        client.post(f"/api/training-materials/{material_id}/download-url", headers=auth).status_code
        == 404
    )

    # 重新上架后客户可签；软删后客户 404、管理端 direct-link 也拒绝。
    merged[IDEMPOTENCY_KEY_HEADER] = f"key-{uuid.uuid4()}"
    assert (
        client.patch(
            f"/api/control/training-materials/{material_id}",
            json={"confirm": True, "reason": "重新上架", "visible": True},
            headers=merged,
        ).status_code
        == 200
    )
    assert (
        client.post(f"/api/training-materials/{material_id}/download-url", headers=auth).status_code
        == 200
    )
    merged[IDEMPOTENCY_KEY_HEADER] = f"key-{uuid.uuid4()}"
    deleted = client.request(
        "DELETE",
        f"/api/control/training-materials/{material_id}",
        json={"confirm": True, "reason": "清理过期素材"},
        headers=merged,
    )
    assert deleted.status_code == 200, deleted.text
    admin_items = client.get("/api/control/training-materials", headers=headers).json()["items"]
    assert admin_items == []
    assert (
        client.post(f"/api/training-materials/{material_id}/download-url", headers=auth).status_code
        == 404
    )
    assert (
        client.get(
            f"/api/control/training-materials/{material_id}/direct-link", headers=headers
        ).status_code
        == 404
    )
    # 软删不回收网盘对象（可恢复口径）：替身里的远端名仍保留。
    assert storage.remote_names


def test_upload_rejected_when_config_changes_in_flight(
    client: TestClient,
    app_under_test: tuple[FastAPI, FakePan123Storage, dict[str, str]],
) -> None:
    """指纹复核：上传期间配置版本变化（换号/换密钥）→ 拒绝入库 + 孤儿日志。"""
    _app, storage, version_holder = app_under_test
    headers = _admin_session(client)
    uploads_before = len(storage.remote_names)

    ok = _upload_material(client, headers, title="正常入库")
    assert ok.status_code == 200, ok.text

    # 模拟上传解析发生在旧版本、业务提交前配置被保存（版本已变）。
    version_holder["version"] = "cfg-v2"
    rejected = _upload_material(client, headers, title="期间换配置")
    assert rejected.status_code == 503, rejected.text
    assert rejected.json()["detail"]["code"] == "TRAINING_MATERIAL_CONFIG_CHANGED"
    with psycopg.connect(_db_dsn(), autocommit=True) as conn:
        rows = conn.execute(
            "SELECT count(*) FROM training_materials WHERE title = '期间换配置'"
        ).fetchone()
        assert rows[0] == 0
    # 网盘侧留下了无引用文件（唯一远端名），由孤儿日志口径运维清理。
    assert len(storage.remote_names) == uploads_before + 2


def test_provider_widen_migration_downgrade_fail_fast(
    training_materials_dsn: str,
) -> None:
    """CHECK 放宽迁移回滚契约：pan123 行被清理；旧白名单外行（yuanbao）
    存在时 downgrade 显式失败并给清理步骤，清掉后才能成功（方案 §3.5）。"""
    from alembic import command
    from alembic.config import Config

    server_dir = Path(__file__).resolve().parent.parent
    config = Config(str(server_dir / "alembic.ini"))
    config.set_main_option("script_location", str(server_dir / "migrations"))
    config.set_main_option(
        "sqlalchemy.url", training_materials_dsn.replace("postgresql://", "postgresql+psycopg://")
    )
    target = "20261102T0900_customer_session_last_seen"
    with psycopg.connect(training_materials_dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO provider_settings (provider, encrypted_config, updated_by_user_id) "
            "VALUES ('yuanbao', 'x', 'admin_u'), ('pan123', 'x', 'admin_u') "
            "ON CONFLICT (provider) DO NOTHING"
        )
    # yuanbao 行存在 → 退到 CHECK 放宽迁移本身必须失败（fail-fast）。
    with pytest.raises(Exception, match="受控清理|旧白名单"):
        command.downgrade(config, target)
    with psycopg.connect(training_materials_dsn, autocommit=True) as conn:
        conn.execute("DELETE FROM provider_settings WHERE provider = 'yuanbao'")
    # 清理 yuanbao 后 downgrade 成功（pan123 行由迁移自身删除），再升回 head。
    command.downgrade(config, target)
    with psycopg.connect(training_materials_dsn, autocommit=True) as conn:
        names = {
            row[0] for row in conn.execute("SELECT provider FROM provider_settings").fetchall()
        }
        assert "pan123" not in names
        constraint = conn.execute(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conname = 'ck_provider_settings_supported_provider'"
        ).fetchone()
        assert "pan123" not in constraint[0]
    command.upgrade(config, "head")
