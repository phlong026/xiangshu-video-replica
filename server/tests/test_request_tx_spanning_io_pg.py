"""请求级事务不得横跨外呼/流式 I/O 的回归测试（2026-11-02 池饿死事故）.

``get_database`` 的请求级事务从依赖解析活到响应发完。凡依赖（数据源 client）
或路由体在这个窗口里做外部 I/O（上游 HTTP、COS head/get、流式回源、平台证书
下载），连接就停在 idle-in-transaction，被 60s 护栏击杀；上游一慢，池(max=8)
被借空饿死，全站 30s PoolTimeout（当天 139 次整批击杀）。

本文件用「事务深度探针」锁住生产真实路径：**外呼发生的那一刻，pg 事务深度
必须为 0**。任何把外呼写回事务窗口的回归都会在这里变红.
"""

# Imported pytest fixtures are injected by parameter name.
# ruff: noqa: F811
from __future__ import annotations

from contextlib import contextmanager
from urllib.parse import quote

import psycopg
import pytest
from test_customer_center import account  # noqa: F401
from test_customer_pricing import (  # noqa: F401
    client,
    registration_client,
    registration_dsn,
    route_state,
)
from test_viral_detail_billing_pg import (  # noqa: F401
    _seed_detail_video,
    detail_client,
)

_TX_MODULES = (
    "app.auth",
    "app.customer_fence",
    "app.payment_routes",
    "app.viral_routes",
    "app.viral_search_refresh_routes",
)


class _TxDepth:
    """pg_transaction 嵌套深度计：探针在外呼时刻采样 depth，断言为 0."""

    def __init__(self) -> None:
        self.depth = 0
        self.samples: list[tuple[str, int]] = []

    def record(self, where: str) -> None:
        self.samples.append((where, self.depth))


@pytest.fixture()
def tx_depth(monkeypatch: pytest.MonkeyPatch) -> _TxDepth:
    from app import db_pg

    holder = _TxDepth()
    real_pg_transaction = db_pg.pg_transaction

    @contextmanager
    def tracked(*, isolation: str | None = None):  # noqa: ANN001 - 镜像真实签名
        holder.depth += 1
        try:
            with real_pg_transaction(isolation=isolation) as conn:
                yield conn
        finally:
            holder.depth -= 1

    from importlib import import_module

    for name in _TX_MODULES:
        module = import_module(name)
        # 只 patch 已直接导入 pg_transaction 的模块；实现前后都保持可运行
        if hasattr(module, "pg_transaction"):
            monkeypatch.setattr(module, "pg_transaction", tracked)
    return holder


def _seed_tikhub_settings(dsn: str, uid: str) -> None:
    """直接 INSERT 与 _save_encrypted_config 同格式的加密配置.

    绕开 SettingsRepository.save：其 ``with self.conn`` 在 psycopg3 顶层退出会
    关闭连接（save 后紧接的 read 会撞「connection is closed」），种数据不值得
    踩这颗预埋雷。加密格式与 fernet_from_environment + sort_keys/separators
    逐字节对齐，应用侧 _load_encrypted_config 可正常解密。
    """
    import json
    import os

    from cryptography.fernet import Fernet

    fernet = Fernet(os.environ["VIDEO_REPLICA_SETTINGS_KEY"].encode("ascii"))
    encrypted = fernet.encrypt(
        json.dumps({"api_key": "tx-span-test-key"}, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).decode("ascii")
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO provider_settings "
            "(provider, encrypted_config, updated_by_user_id, created_at, updated_at) "
            "VALUES (%s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP) "
            "ON CONFLICT(provider) DO UPDATE SET encrypted_config = excluded.encrypted_config",
            ("tikhub", encrypted, uid),
        )


def _seed_refresh_tariff(raw: psycopg.Connection) -> None:
    raw.execute("DELETE FROM billing_tariffs WHERE service='viral_search_refresh'")
    raw.execute(
        "INSERT INTO billing_tariffs(service,enabled,unit_credits) "
        "VALUES('viral_search_refresh',true,1)"
    )


def _spy_stored(uri: str, key: str):
    from datetime import UTC, datetime
    from types import SimpleNamespace

    return SimpleNamespace(
        provider="local",
        bucket="tx-span",
        key=key,
        uri=uri,
        size=4,
        content_type="video/mp4",
        sha256="",
        updated_at=datetime.now(UTC),
    )


class _SpyStorage:
    """记录每次存储 I/O 发生时刻的事务深度；仅满足路由用到的接口面."""

    provider = "local"
    bucket = "tx-span"
    cache_namespace = "tx-span"

    def __init__(self, holder: _TxDepth, *, stored_uri: str, stored_key: str) -> None:
        self._holder = holder
        self._stored = _spy_stored(stored_uri, stored_key)

    def head_object(self, key: str):  # noqa: ANN201
        self._holder.record(f"head_object({key})")
        return self._stored

    def get_object(self, key: str) -> bytes:
        self._holder.record(f"get_object({key})")
        return b"\x89PNG-tx-span-fake"

    def iter_object(self, key: str, *, start: int = 0, end: int | None = None):
        self._holder.record(f"iter_object({key})")
        yield b"abcd"


# ---------------------------------------------------------------------------
# 1) 爆款视频媒体下载：签名校验 → 短事务读授权 → COS head/流式回源（无事务）
# ---------------------------------------------------------------------------


def test_viral_media_download_head_and_stream_outside_tx(
    detail_client,
    route_state: str,
    tx_depth: _TxDepth,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cryptography.fernet import Fernet

    from app.settings import settings_encryption_key
    from app.storage import local_download_signature
    from app.viral_routes import _VIRAL_SIGNATURE_ASSET, _VIRAL_SIGNATURE_EPOCH

    monkeypatch.setenv("VIDEO_REPLICA_SETTINGS_KEY", Fernet.generate_key().decode())
    headers, uid = account(detail_client, "tx_dl_user")
    with psycopg.connect(route_state) as raw:
        _seed_detail_video(raw, uid, video_id="v-tx-dl")
        raw.commit()

    key = "viral/douyin/v-tx-dl.mp4"
    expires = "9999999999"
    signature = local_download_signature(
        key,
        expires,
        user_id=uid,
        asset_id=_VIRAL_SIGNATURE_ASSET,
        session_epoch=_VIRAL_SIGNATURE_EPOCH,
        secret=settings_encryption_key(),
    )
    monkeypatch.setattr(
        "app.viral_routes.get_media_storage",
        lambda conn: _SpyStorage(tx_depth, stored_uri=f"local://{key}", stored_key=key),
    )

    response = detail_client.get(
        f"/api/viral/videos/media/file?key={quote(key)}&expires={expires}"
        f"&user_id={quote(uid)}&sig={signature}"
    )
    assert response.status_code == 200, response.text
    assert response.content == b"abcd"
    assert tx_depth.samples, "探针未触发：存储 I/O 未被采样"
    assert all(depth == 0 for _, depth in tx_depth.samples), tx_depth.samples


# ---------------------------------------------------------------------------
# 2) 爆款封面：短事务读可见性 → COS head/get（无事务）
# ---------------------------------------------------------------------------


def test_viral_cover_reads_outside_tx(
    detail_client,
    route_state: str,
    tx_depth: _TxDepth,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    headers, uid = account(detail_client, "tx_cover_user")
    with psycopg.connect(route_state) as raw:
        _seed_detail_video(raw, uid, video_id="v-tx-cv")
        raw.commit()

    monkeypatch.setattr(
        "app.viral_routes.get_media_storage",
        lambda conn: _SpyStorage(
            tx_depth,
            stored_uri="local://viral/covers/douyin/v-tx-cv",
            stored_key="viral/covers/douyin/v-tx-cv",
        ),
    )

    response = detail_client.get("/api/viral/covers/douyin/v-tx-cv")
    assert response.status_code == 200, response.text
    assert tx_depth.samples, "探针未触发：存储 I/O 未被采样"
    assert all(depth == 0 for _, depth in tx_depth.samples), tx_depth.samples


# ---------------------------------------------------------------------------
# 3) 源站直链：client 依赖的配置读取必须短事务，外呼时刻深度 0
# ---------------------------------------------------------------------------


def test_source_directlink_upstream_call_outside_tx(
    detail_client,
    route_state: str,
    tx_depth: _TxDepth,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dataclasses import replace

    from cryptography.fernet import Fernet

    from app.viral_tikhub import ViralSourceClient

    monkeypatch.setenv("VIDEO_REPLICA_SETTINGS_KEY", Fernet.generate_key().decode())
    headers, uid = account(detail_client, "tx_source_user")
    with psycopg.connect(route_state) as raw:
        _seed_detail_video(raw, uid, video_id="v-tx-src")
        _seed_refresh_tariff(raw)
        _seed_tikhub_settings(route_state, uid)
        raw.commit()

    canned = replace(
        _source_seed_video(), video_id="v-tx-src", play_url="https://cdn.example/v.mp4"
    )

    def fake_refresh(self, *, platform: str, video_id: str):  # noqa: ANN001
        tx_depth.record("douyin_refresh(source)")
        return [canned]

    monkeypatch.setattr(ViralSourceClient, "douyin_refresh", fake_refresh)

    response = detail_client.post(
        "/api/viral/videos/source",
        headers={**headers, "Idempotency-Key": "tx-span-source-1"},
        json={"platform": "douyin", "videoId": "v-tx-src"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["fullUrl"] == "https://cdn.example/v.mp4"
    assert ("douyin_refresh(source)", 0) in tx_depth.samples, tx_depth.samples


# ---------------------------------------------------------------------------
# 4) 单条刷新：同上，refresh 路由自己的 client 依赖
# ---------------------------------------------------------------------------


@pytest.fixture()
def refresh_client(detail_client):
    from app.viral_search_refresh_routes import router as refresh_router

    detail_client.app.include_router(refresh_router)
    yield detail_client
    detail_client.app.dependency_overrides.clear()


def test_search_refresh_upstream_call_outside_tx(
    refresh_client,
    route_state: str,
    tx_depth: _TxDepth,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dataclasses import replace

    from cryptography.fernet import Fernet

    from app.viral_tikhub import ViralSourceClient

    monkeypatch.setenv("VIDEO_REPLICA_SETTINGS_KEY", Fernet.generate_key().decode())
    headers, uid = account(refresh_client, "tx_refresh_user")
    with psycopg.connect(route_state) as raw:
        _seed_detail_video(raw, uid, video_id="v-tx-rf")
        _seed_refresh_tariff(raw)
        _seed_tikhub_settings(route_state, uid)
        raw.commit()

    canned = replace(
        _source_seed_video(), video_id="v-tx-rf", play_url="https://cdn.example/rf.mp4"
    )

    def fake_refresh(self, *, platform: str, video_id: str):  # noqa: ANN001
        tx_depth.record("douyin_refresh(refresh)")
        return [canned]

    monkeypatch.setattr(ViralSourceClient, "douyin_refresh", fake_refresh)

    # 封面归档基础设施：假存储 + 假图源（与 test_viral_search_pg 同一模式）
    from app import viral_media

    class _FakeCoverStorage:
        provider = "local"
        bucket = "tx-span"
        cache_namespace = "tx-span"

        def head_object(self, key):  # noqa: ANN001, ARG002
            return None

        def put_object(self, key, data, *, content_type=None):  # noqa: ANN001, ARG002
            pass

    def fake_iter_fetch(self, url):  # noqa: ANN001, ARG002
        yield b"\xff\xd8\xff\xe0fakejpeg"

    monkeypatch.setattr(viral_media.UrlFetcher, "iter_fetch", fake_iter_fetch)
    monkeypatch.setattr(
        "app.viral_search_refresh_routes.get_media_storage",
        lambda conn: _FakeCoverStorage(),
    )

    response = refresh_client.post(
        "/api/viral/search/refresh",
        headers={**headers, "Idempotency-Key": "tx-span-refresh-1"},
        json={"platform": "douyin", "videoId": "v-tx-rf"},
    )
    assert response.status_code == 200, response.text
    assert ("douyin_refresh(refresh)", 0) in tx_depth.samples, tx_depth.samples


# ---------------------------------------------------------------------------
# 5) 微信回调：证书下载/验签窗口与结算各自短事务，验签时刻深度 0
# ---------------------------------------------------------------------------


@pytest.fixture()
def notify_client(detail_client):
    from app.payment_routes import router as payment_router

    detail_client.app.include_router(payment_router)
    yield detail_client
    detail_client.app.dependency_overrides.clear()


def test_wechat_notify_verify_and_settle_outside_tx(
    notify_client,
    tx_depth: _TxDepth,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.payment_routes import get_wechat_provider

    class _Merchant:
        allowed_channels = ("wechat_native",)

    class _VerifyResult:
        authenticated = True
        error_code = None
        trade_state = "NOTPAY"  # 非终态 → ACK 早退，不进结算
        merchant_order_no = None
        provider_trade_no = None
        amount_fen = None
        channel = None
        source_digest = None

    class _StubProvider:
        def load_merchant_config(self, conn):  # noqa: ANN001
            tx_depth.record("load_merchant_config")
            return _Merchant()

        def verify_notification_raw(self, **kwargs):  # noqa: ANN003
            tx_depth.record("verify_notification_raw")
            return _VerifyResult()

    notify_client.app.dependency_overrides[get_wechat_provider] = lambda: _StubProvider()

    response = notify_client.post("/api/payments/wechat_native/notify", content=b"<xml/>")
    assert response.status_code == 200, response.text
    # 验签（可能下载平台证书）是外呼窗口，必须深度 0；
    # load_merchant_config 是 DB 读，按设计在短事务内（深度 1），不在此断言。
    assert ("verify_notification_raw", 0) in tx_depth.samples, tx_depth.samples


def _source_seed_video():
    from app.viral_tikhub import ViralVideo

    return ViralVideo(
        platform="douyin",
        video_id="v-tx-src",
        category="",
        title="T",
        author="A",
        author_avatar=None,
        verified=False,
        cover_url=None,
        duration_ms=15000,
        likes=10,
        comments=None,
        shares=None,
        collects=None,
        published_at=None,
        published_display=None,
        like_display=None,
    )


def test_wechat_notify_settlement_runs_outside_tx(
    notify_client,
    tx_depth: _TxDepth,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """终态 SUCCESS 的结算块同样必须自开短事务：外呼/等待不得骑进事务窗口."""
    from app import payment_routes
    from app.payment_routes import get_wechat_provider

    class _Merchant:
        allowed_channels = ("wechat_native",)

    class _VerifyResult:
        authenticated = True
        error_code = None
        trade_state = "SUCCESS"
        merchant_order_no = "tx-span-order-1"
        provider_trade_no = "wx-tx-span-1"
        amount_fen = 100
        channel = "wechat_native"
        source_digest = "tx-span-digest-1"

    class _StubProvider:
        def load_merchant_config(self, conn):  # noqa: ANN001, ARG002
            return _Merchant()

        def verify_notification_raw(self, **kwargs):  # noqa: ANN003, ARG002
            return _VerifyResult()

    def fake_confirm(conn, **kwargs):  # noqa: ANN001, ANN003, ARG002
        tx_depth.record("confirm_recharge_payment")
        return None

    notify_client.app.dependency_overrides[get_wechat_provider] = lambda: _StubProvider()
    monkeypatch.setattr(payment_routes, "confirm_recharge_payment", fake_confirm)

    response = notify_client.post("/api/payments/wechat_native/notify", content=b"<xml/>")
    assert response.status_code == 200, response.text
    # 结算是纯 DB 操作，自开短事务包裹（深度恰为 1 = 自己那一层）；
    # 回归形态是把结算包回请求级事务（深度变 2）——那里等订单行锁会拖死连接。
    assert ("confirm_recharge_payment", 1) in tx_depth.samples, tx_depth.samples
    assert all(depth <= 1 for _, depth in tx_depth.samples), tx_depth.samples
