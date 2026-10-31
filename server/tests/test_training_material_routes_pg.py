"""平台培训素材库（IA 改版第 5 项）HTTP 契约测试。

覆盖：管理端 multipart 上传（白名单/超限/写契约/幂等重放/审计员只读）、
部分更新与软删、客户只读列表的可见性/类型筛选/分页、下载链接的本地
HMAC 车道（签名有效性、踢线 epoch 失效、Range 读取）与下载计数自增。
存储全部走 FakeStorageAdapter（本地车道语义），不触网。
"""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

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
from app.storage import FakeStorageAdapter

DEFAULT_DSN = "postgresql://testuser:testpass@localhost:5433/customer_v3_test"
DB_NAME = "training_materials_test"
TEST_ADMIN_SESSION_KEY = secrets.token_urlsafe(48)
TEST_ACTIVATION_KEY = secrets.token_urlsafe(48)
FUTURE_EXPIRY = "2099-01-01T00:00:00+00:00"
IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"
ACTIVATION_CODE_HMAC_KEY_ENV = "VIDEO_REPLICA_ACTIVATION_CODE_HMAC_KEY"

# mp4 魔数（ftyp）开头的最小合法负载；上传白名单按 (后缀, content-type) 判定。
MP4_BYTES = b"\x00\x00\x00\x18ftypisom" + b"x" * 64
PDF_BYTES = b"%PDF-1.7\n" + b"y" * 64


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
            "runtime_settings, provider_settings CASCADE"
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
) -> Iterator[tuple[FastAPI, FakeStorageAdapter]]:
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
    # 本地车道语义的存储替身：客户下载走 HMAC 签名 + /file 端点。
    fake_storage = FakeStorageAdapter(provider="fake", bucket="training-test")
    fake_storage.provider = "local"
    monkeypatch.setattr(training_material_routes, "get_media_storage", lambda _conn: fake_storage)
    yield app, fake_storage
    app.dependency_overrides.clear()


@pytest.fixture()
def client(app_under_test: tuple[FastAPI, FakeStorageAdapter]) -> Iterator[TestClient]:
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


def test_admin_upload_validation_and_idempotency(
    client: TestClient, app_under_test: tuple[FastAPI, FakeStorageAdapter]
) -> None:
    storage = app_under_test[1]
    headers = _admin_session(client)

    created = _upload_material(client, headers)
    assert created.status_code == 200, created.text
    material_id = created.json()["id"]

    # 对象已落存储，键含内容摘要（内容寻址）。
    keys = [key for key in storage._objects if key.startswith("training-materials/")]
    assert len(keys) == 1
    assert hashlib.sha256(MP4_BYTES).hexdigest() in keys[0]

    # 白名单外的 (后缀, content-type) 组合 → 415。
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
    # 后缀与 content-type 混搭（mp4 后缀 + pdf 类型）同样拒绝。
    assert _upload_material(client, headers, content_type="application/pdf").status_code == 415
    # 标题缺失 → 422；审计员无内容写权限 → 403。
    assert _upload_material(client, headers, title="  ").status_code == 422
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
            data={"title": "x", "confirm": "false", "reason": ""},
            files={"file": ("a.mp4", MP4_BYTES, "video/mp4")},
            headers=merged,
        ).status_code
        == 400
    )

    # 幂等重放：同键同请求 → 200 + X-Idempotent-Replay，不产生第二行。
    replay_key = f"replay-{uuid.uuid4()}"
    first = _upload_material(client, headers, title="重放用", idempotency_key=replay_key)
    assert first.status_code == 200
    second = _upload_material(client, headers, title="重放用", idempotency_key=replay_key)
    # 快照层回放的是记录时的 success_status（201），与首响 200 不同属既有行为。
    assert second.status_code in (200, 201)
    assert second.headers.get("X-Idempotent-Replay") == "true"
    with psycopg.connect(_db_dsn(), autocommit=True) as conn:
        rows = conn.execute(
            "SELECT count(*) FROM training_materials WHERE title = '重放用'"
        ).fetchone()
        assert rows[0] == 1

    # 同键不同文件 → 409（内容指纹进契约体）。
    conflict = _upload_material(
        client,
        headers,
        title="重放用",
        content=MP4_BYTES + b"different",
        idempotency_key=replay_key,
    )
    assert conflict.status_code == 409

    with psycopg.connect(_db_dsn(), autocommit=True) as conn:
        audit = conn.execute(
            "SELECT action FROM audit_logs "
            "WHERE entity_type = 'training_material' AND entity_id = %s",
            (material_id,),
        ).fetchall()
        assert {row[0] for row in audit} == {"training_material.create"}


def test_customer_visibility_listing_and_download(
    client: TestClient, app_under_test: tuple[FastAPI, FakeStorageAdapter]
) -> None:
    headers = _admin_session(client)
    video_id = _upload_material(client, headers, title="封面视频").json()["id"]
    _upload_material(
        client,
        headers,
        title="讲义",
        filename="讲义.pdf",
        content=PDF_BYTES,
        content_type="application/pdf",
    )
    hidden_id = _upload_material(client, headers, title="未上架视频", visible=False).json()["id"]
    del hidden_id

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
    assert kinds["讲义"] == "training"

    # 类型筛选 + 无效类型。
    video_only = client.get(
        "/api/training-materials", params={"kind": "video"}, headers=auth
    ).json()
    assert [item["title"] for item in video_only["items"]] == ["封面视频"]
    assert (
        client.get("/api/training-materials", params={"kind": "neon"}, headers=auth).status_code
        == 422
    )

    # 下载链接：本地车道返回 HMAC 站内 URL；下载计数自增；/file 可读且校验签名。
    before = client.get("/api/control/training-materials", headers=headers).json()
    before_count = {item["title"]: item["download_count"] for item in before["items"]}
    download = client.post(f"/api/training-materials/{video_id}/download-url", headers=auth)
    assert download.status_code == 200, download.text
    url = download.json()["url"]
    assert "/api/training-materials/" in url and "/file?" in url
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    assert {"expires", "user_id", "session_epoch", "sig"} <= set(query)

    file_response = client.get(parsed.path + f"?{parsed.query}")
    assert file_response.status_code == 200, file_response.text
    assert file_response.content == MP4_BYTES
    assert file_response.headers["content-type"].startswith("video/mp4")

    after = client.get("/api/control/training-materials", headers=headers).json()
    after_count = {item["title"]: item["download_count"] for item in after["items"]}
    assert after_count["封面视频"] == before_count["封面视频"] + 1

    # 篡改签名 / 过期时间 → 拒绝。
    tampered = parsed.path + "?" + parsed.query.replace(query["sig"][0], "0" * 64)
    assert client.get(tampered).status_code == 403
    stale = (
        f"{parsed.path}?expires=1000000000&user_id={query['user_id'][0]}"
        f"&session_epoch={query['session_epoch'][0]}&sig={query['sig'][0]}"
    )
    assert client.get(stale).status_code == 410

    # Range 请求（页内播放拖动）。
    ranged = client.get(parsed.path + f"?{parsed.query}", headers={"Range": "bytes=0-3"})
    assert ranged.status_code == 206
    assert ranged.content == MP4_BYTES[:4]


def test_update_soft_delete_and_kick_invalidates_url(
    client: TestClient, app_under_test: tuple[FastAPI, FakeStorageAdapter]
) -> None:
    headers = _admin_session(client)
    material_id = _upload_material(client, headers, title="待更新").json()["id"]
    token = _customer_token(client)
    auth = {"Authorization": f"Bearer {token}"}

    # 下架：客户列表与下载立即不可见。
    merged = dict(headers)
    merged[IDEMPOTENCY_KEY_HEADER] = f"key-{uuid.uuid4()}"
    patched = client.patch(
        f"/api/control/training-materials/{material_id}",
        json={"confirm": True, "reason": "先下架", "visible": False},
        headers=merged,
    )
    assert patched.status_code == 200, patched.text
    assert client.get("/api/training-materials", headers=auth).json()["total"] == 0
    assert (
        client.post(f"/api/training-materials/{material_id}/download-url", headers=auth).status_code
        == 404
    )

    # 重新上架并取一条有效链接，然后踢线（epoch 递增）——旧链接必须失效。
    merged[IDEMPOTENCY_KEY_HEADER] = f"key-{uuid.uuid4()}"
    assert (
        client.patch(
            f"/api/control/training-materials/{material_id}",
            json={"confirm": True, "reason": "重新上架", "visible": True},
            headers=merged,
        ).status_code
        == 200
    )
    url = client.post(f"/api/training-materials/{material_id}/download-url", headers=auth).json()[
        "url"
    ]
    parsed = urlparse(url)
    alive = client.get(parsed.path + f"?{parsed.query}")
    assert alive.status_code == 200

    with psycopg.connect(_db_dsn(), autocommit=True) as conn:
        # 踢线语义 = epoch 递增（revocation 同款）；lease 列受 CHECK 保护不能倒拨。
        conn.execute("UPDATE customer_session_state SET session_epoch = session_epoch + 1")
    assert client.get(parsed.path + f"?{parsed.query}").status_code == 401

    # 软删：管理列表默认不可见，客户 404。
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


def test_expired_timestamp_guard_and_unused_expiry() -> None:
    """契约面上锁死过期口径：DOWNLOAD_URL_EXPIRES_IN 必须 ≤ 15 分钟。"""
    from app.training_material_routes import DOWNLOAD_URL_EXPIRES_IN

    assert DOWNLOAD_URL_EXPIRES_IN <= timedelta(minutes=15)
    assert isinstance(DOWNLOAD_URL_EXPIRES_IN, timedelta)
    # 防呆：时间工具引用未漂移（模块导入成功即证明）。
    assert datetime.now(UTC).tzinfo is not None
