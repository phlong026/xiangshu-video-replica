"""爆款视频「查看详情」计费：独立科目 + 按付费账号去重台账.

TEST-PG：复用 test_customer_pricing 的夹具链（cw076_registration_test 专属库，
module 级建库 → alembic head → 每用例 TRUNCATE wallets/users）。去重台账复用
``billing_operations`` 既有的唯一键与索引，因此本模块不覆盖新迁移。

2026-11-11 极简化：获取文案（viral_copy）交付计费整体退役，原「获取文案」段
（claim/并发首提/提取记录/状态只读）随之删除；文案语义回归见
test_viral_copy_simplify_pg.py。本文件只保留详情计费与公开端点可见性守卫。
"""

# Imported pytest fixtures are injected by parameter name.
# ruff: noqa: F811, F401

import json
from datetime import UTC, datetime

import psycopg
import pytest
from test_customer_pricing import (  # noqa: F401
    account,
    client,
    registration_client,
    registration_dsn,
    route_state,
)

from app.db_portable import BusinessConnection
from app.usage_billing import accept_operation, finish_source

_DETAIL_SERVICE = "viral_detail"


@pytest.fixture()
def detail_client(client):
    """测试 app 是精简的，viral_routes 不在默认路由表里，必须显式挂上。"""
    from app.viral_routes import router as viral_routes_router

    client.app.include_router(viral_routes_router)
    yield client
    client.app.dependency_overrides.clear()


def _publish_ready(raw, *, platform: str, video_id: str) -> None:
    raw.execute(
        "UPDATE viral_videos SET collection_published=1,homepage_featured=0,pushed_at=now(), "
        "homepage_starts_at=NULL,homepage_ends_at=NULL,cover_url=NULL,deleted_at=NULL "
        "WHERE platform=%s AND video_id=%s",
        (platform, video_id),
    )
    raw.execute(
        "INSERT INTO viral_media_preparations(id,platform,video_id,media_kind,status,storage_uri) "
        "VALUES(%s,%s,%s,'video','SUCCEEDED',%s) "
        "ON CONFLICT(platform,video_id,media_kind) DO UPDATE SET "
        "status='SUCCEEDED',storage_uri=excluded.storage_uri",
        (
            f"detail-{platform}-{video_id}",
            platform,
            video_id,
            f"local://viral/{platform}/{video_id}.mp4",
        ),
    )
    raw.execute(
        "DELETE FROM viral_video_visibility WHERE platform=%s AND video_id=%s", (platform, video_id)
    )


def _seed_detail_video(
    raw,
    uid: str,
    *,
    platform: str = "douyin",
    video_id: str,
    tariff: int | None = 2,
    visibility: str | None = None,
) -> None:
    raw.execute(
        "INSERT INTO viral_videos "
        "(platform, video_id, title, author, comments, shares, collects, native_json) "
        "VALUES (%s, %s, 'T', 'A', 3, 4, 5, %s) ON CONFLICT (platform, video_id) "
        "DO UPDATE SET deleted_at = NULL",
        (platform, video_id, json.dumps({})),
    )
    _publish_ready(raw, platform=platform, video_id=video_id)
    raw.execute(
        "UPDATE wallets SET available_credits=50, reserved_credits=0 WHERE user_id=%s", (uid,)
    )
    raw.execute("DELETE FROM billing_tariffs WHERE service=%s", (_DETAIL_SERVICE,))
    if tariff is not None:
        raw.execute(
            "INSERT INTO billing_tariffs(service,enabled,unit_credits) VALUES(%s,true,%s)",
            (_DETAIL_SERVICE, tariff),
        )
    if visibility is not None:
        raw.execute(
            "INSERT INTO viral_video_visibility (platform, video_id, status, reason) "
            "VALUES (%s, %s, %s, 'detail-test') ON CONFLICT (platform, video_id) "
            "DO UPDATE SET status = excluded.status",
            (platform, video_id, visibility),
        )
    raw.commit()


def _open_detail(http_client, headers, *, platform: str = "douyin", video_id: str):
    return http_client.post(
        "/api/viral/videos/detail",
        headers=headers,
        json={"platform": platform, "videoId": video_id},
    )


def _wallet(raw, uid: str) -> tuple[int, int]:
    row = raw.execute(
        "SELECT available_credits, reserved_credits FROM wallets WHERE user_id=%s", (uid,)
    ).fetchone()
    return (int(row[0]), int(row[1])) if row is not None else (0, 0)


def _detail_operations(raw, source_id: str) -> list[tuple[str, int]]:
    rows = raw.execute(
        "SELECT state, charged_credits FROM billing_operations "
        "WHERE service=%s AND source_id=%s ORDER BY billing_round",
        (_DETAIL_SERVICE, source_id),
    ).fetchall()
    return [(str(row[0]), int(row[1])) for row in rows]


@pytest.mark.parametrize("platform", ["douyin", "wechat_channels", "xiaohongshu"])
def test_published_list_uses_one_database_connection(
    detail_client, route_state: str, monkeypatch: pytest.MonkeyPatch, platform: str
) -> None:
    """列表只读内容池；持有会话锁时再借连接会阻塞首页的并发预览请求。"""
    from app.db_pg import close_pg_pool

    headers, uid = account(detail_client, f"list_single_conn_{platform}")
    video_id = f"single-connection-{platform}"
    with psycopg.connect(route_state) as raw:
        _seed_detail_video(raw, uid, platform=platform, video_id=video_id)

    close_pg_pool()
    monkeypatch.setenv("VIDEO_REPLICA_PG_POOL_MIN", "1")
    monkeypatch.setenv("VIDEO_REPLICA_PG_POOL_MAX", "1")
    monkeypatch.setenv("VIDEO_REPLICA_PG_POOL_TIMEOUT", "0.2")
    try:
        response = detail_client.get(
            "/api/viral/videos", headers=headers, params={"platform": platform}
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["source"] == "database"
        assert video_id in [item["videoId"] for item in payload["items"]]
    finally:
        close_pg_pool()


def test_viral_detail_service_registered_in_billing_catalog() -> None:
    """viral_detail 科目：call/platform/viral，可客户收费、复用 viral_extract 折扣接口."""
    from app.billing_catalog import (
        INTERFACE_KEYS,
        SERVICE_INTERFACE,
        SERVICES,
        interface_for_service,
    )

    service = SERVICES["viral_detail"]
    assert service.name == "爆款视频详情"
    assert service.unit == "call"
    # 详情读的是已归档内容，没有按次供应商成本，provider 记平台本身。
    assert service.provider == "platform"
    assert service.module == "viral"
    assert service.customer_charge_allowed is True
    assert interface_for_service("viral_detail") == "viral_extract"
    assert SERVICE_INTERFACE["viral_detail"] in INTERFACE_KEYS


def test_detail_is_free_and_replays_free(detail_client, route_state) -> None:
    """查看详情免费（2026-10-13 拍板）：首次与重复查看都不扣费、不落台账."""
    headers, uid = account(detail_client, "detail_first")
    with psycopg.connect(route_state) as raw:
        _seed_detail_video(raw, uid, video_id="v-detail-first")
    for _ in range(2):
        response = _open_detail(detail_client, headers, video_id="v-detail-first")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["billing"] == {
            "charged": 0,
            "unit": "call",
            "deduped": False,
            "billable": False,
        }
        assert body["item"]["detailCharged"] is False
        # 交互统计字段随详情一并下发。
        assert body["item"]["comments"] == 3
        assert body["item"]["shares"] == 4
        assert body["item"]["collects"] == 5
    with psycopg.connect(route_state) as raw:
        assert _wallet(raw, uid) == (50, 0)
        assert _detail_operations(raw, f"viral-detail:{uid}:douyin:v-detail-first") == []


def test_detail_stays_open_without_tariff(detail_client, route_state) -> None:
    """资费停用 / 未配置不再让详情页 503：免费口径下详情与资费配置解耦."""
    headers, uid = account(detail_client, "detail_unpriced")
    with psycopg.connect(route_state) as raw:
        _seed_detail_video(raw, uid, video_id="v-detail-unpriced", tariff=None)
    response = _open_detail(detail_client, headers, video_id="v-detail-unpriced")
    assert response.status_code == 200, response.text
    assert response.json()["billing"]["charged"] == 0
    with psycopg.connect(route_state) as raw:
        assert _wallet(raw, uid) == (50, 0)
        assert (
            raw.execute(
                "SELECT count(*) FROM billing_operations WHERE user_id=%s AND service=%s",
                (uid, _DETAIL_SERVICE),
            ).fetchone()[0]
            == 0
        )


def test_detail_unavailable_content_is_denied_without_charge(detail_client, route_state) -> None:
    """下架内容没有新的详情读取授权，不交付也不计费."""
    headers, uid = account(detail_client, "detail_unavailable")
    with psycopg.connect(route_state) as raw:
        _seed_detail_video(raw, uid, video_id="v-detail-gone", visibility="UNAVAILABLE")
    response = _open_detail(detail_client, headers, video_id="v-detail-gone")
    assert response.status_code == 404, response.text
    with psycopg.connect(route_state) as raw:
        assert _wallet(raw, uid) == (50, 0)
        assert (
            raw.execute(
                "SELECT count(*) FROM billing_operations WHERE user_id=%s AND service=%s",
                (uid, _DETAIL_SERVICE),
            ).fetchone()[0]
            == 0
        )


def test_detail_rejects_unknown_video_and_platform_without_charge(
    detail_client, route_state
) -> None:
    """读不到内容的请求不产生费用：缺视频 404、平台非法 400，钱包与台账都不动."""
    headers, uid = account(detail_client, "detail_missing")
    with psycopg.connect(route_state) as raw:
        _seed_detail_video(raw, uid, video_id="v-detail-known")
    missing = _open_detail(detail_client, headers, video_id="v-detail-absent")
    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "VIRAL_VIDEO_NOT_FOUND"
    invalid = _open_detail(detail_client, headers, platform="kuaishou", video_id="v-detail-known")
    assert invalid.status_code == 400
    assert invalid.json()["detail"]["code"] == "VIRAL_PLATFORM_INVALID"
    with psycopg.connect(route_state) as raw:
        assert _wallet(raw, uid) == (50, 0)
        assert (
            raw.execute(
                "SELECT count(*) FROM billing_operations WHERE user_id=%s AND service=%s",
                (uid, _DETAIL_SERVICE),
            ).fetchone()[0]
            == 0
        )


def test_detail_history_purchase_still_reported(detail_client, route_state) -> None:
    """口径翻转前的历史购买仍按付费账号回填（子账号买过，母账号看到 deduped），但不再扣费."""
    master_headers, master_id = account(detail_client, "detail_master")
    child_headers, child_id = account(detail_client, "detail_child")
    source_id = f"viral-detail:{master_id}:douyin:v-detail-shared"
    with psycopg.connect(route_state) as raw:
        raw.execute(
            "UPDATE users SET account_type='SUB', parent_user_id=%s WHERE id=%s",
            (master_id, child_id),
        )
        _seed_detail_video(raw, master_id, video_id="v-detail-shared")
        # 口径翻转前的真实购买路径：预留 + 结算，扣母账号 2 积分。
        business = BusinessConnection.postgres(raw)
        accept_operation(
            business,
            user_id=master_id,
            service=_DETAIL_SERVICE,
            source_id=source_id,
            units=1,
            request_fingerprint="douyin:v-detail-shared",
        )
        finish_source(business, source_id, units=1, succeeded=True, service=_DETAIL_SERVICE)
    for headers in (child_headers, master_headers):
        view = _open_detail(detail_client, headers, video_id="v-detail-shared")
        assert view.status_code == 200, view.text
        assert view.json()["billing"] == {
            "charged": 0,
            "unit": "call",
            "deduped": True,
            "billable": False,
        }
        assert view.json()["item"]["detailCharged"] is True
    with psycopg.connect(route_state) as raw:
        # 只有历史那一次扣费，打开详情没有再扣。
        assert _wallet(raw, master_id) == (48, 0)
        assert _detail_operations(raw, source_id) == [("SUCCEEDED", 2)]


def test_detail_auditor_never_starts_a_charge(detail_client, route_state) -> None:
    """非客户角色不得产生新的客户扣费.

    客户会话闸门在进入业务路由前就拒掉 ``role != 'customer'`` 的会话（401），
    端点内的 ``require_not_auditor`` 是更深一层的兜底（与其余计费端点同一口径，
    API-Key 轨把角色钉死为 customer，因此该兜底当前不可达）。这里断言可观察的
    结果：换角色后既拿不到详情，也不会留下任何计费行。
    """
    headers, uid = account(detail_client, "detail_audit")
    with psycopg.connect(route_state) as raw:
        _seed_detail_video(raw, uid, video_id="v-detail-audited")
    with psycopg.connect(route_state) as raw:
        raw.execute("UPDATE users SET role='auditor' WHERE id=%s", (uid,))
    response = _open_detail(detail_client, headers, video_id="v-detail-audited")
    assert response.status_code == 401
    with psycopg.connect(route_state) as raw:
        assert _wallet(raw, uid) == (50, 0)
        assert (
            raw.execute(
                "SELECT count(*) FROM billing_operations WHERE user_id=%s AND service=%s",
                (uid, _DETAIL_SERVICE),
            ).fetchone()[0]
            == 0
        )


def _seed_featured_video(raw, *, video_id: str) -> None:
    """首页展示网格的完整可见条件：已归档封面 + 视频媒体就绪 + 已入集."""
    raw.execute(
        "INSERT INTO viral_videos (platform, video_id, title, author, native_json) "
        "VALUES ('douyin', %s, %s, 'A', '{}') ON CONFLICT (platform, video_id) "
        "DO UPDATE SET deleted_at = NULL",
        (video_id, f"P1 {video_id}"),
    )
    raw.execute(
        "INSERT INTO viral_media_preparations "
        "(id, platform, video_id, media_kind, status, storage_uri) "
        "VALUES (%s, 'douyin', %s, 'video', 'SUCCEEDED', %s) "
        "ON CONFLICT (platform, video_id, media_kind) DO UPDATE SET "
        "status='SUCCEEDED', storage_uri=excluded.storage_uri",
        (f"mp-{video_id}", video_id, f"local://media/viral/{video_id}.mp4"),
    )
    raw.execute(
        "UPDATE viral_videos SET published_at=%s, collection_published=1, "
        "homepage_featured=1, cover_key='viral/covers/douyin/' || video_id "
        "WHERE platform='douyin' AND video_id=%s",
        (int(datetime.now(UTC).timestamp()), video_id),
    )


def test_list_and_free_detail_never_mark_charged(detail_client, route_state) -> None:
    """免费口径下打开详情不会让列表 / 详情的 detailCharged 变成 true."""
    headers, uid = account(detail_client, "detail_backfill")
    with psycopg.connect(route_state) as raw:
        _seed_detail_video(raw, uid, video_id="v-detail-bought")
        _seed_featured_video(raw, video_id="v-detail-bought")
        _seed_featured_video(raw, video_id="v-detail-fresh")
    prepared = _open_detail(detail_client, headers, video_id="v-detail-bought")
    assert prepared.status_code == 200, prepared.text
    listing = detail_client.get(
        "/api/viral/videos",
        headers=headers,
        params={"platform": "douyin", "sort": "hot", "limit": 50, "featured_only": "true"},
    )
    assert listing.status_code == 200, listing.text
    flags = {item["videoId"]: item["detailCharged"] for item in listing.json()["items"]}
    # 查看详情已免费：打开详情不再形成「已购」标记，也不落台账。
    assert flags == {"v-detail-bought": False, "v-detail-fresh": False}
    free_detail = detail_client.get("/api/viral/videos/douyin/v-detail-bought", headers=headers)
    assert free_detail.status_code == 200, free_detail.text
    assert free_detail.json()["detailCharged"] is False
    with psycopg.connect(route_state) as raw:
        assert _wallet(raw, uid) == (50, 0)
        assert _detail_operations(raw, f"viral-detail:{uid}:douyin:v-detail-bought") == []


def test_cache_reclaim_returns_server_side_truth(detail_client, route_state) -> None:
    """本地缓存回收判据由服务端回答：删除 / 下架 / 不再下发的回收，仍在发与收藏的保留.

    客户端手里只有翻过的页，拿分页结果当「还在下发」的依据会误删仍然有效的缓存，
    所以回收必须问服务端。该端点只读、不计费：回收发生在用户机器上，不该因此收费。
    """
    headers, uid = account(detail_client, "cache_reclaim")
    with psycopg.connect(route_state) as raw:
        _seed_detail_video(raw, uid, video_id="keep-live")
        _seed_featured_video(raw, video_id="keep-live")
        # 收藏不受首页窗口与归档状态影响：即使当前不在下发，也不能回收。
        _seed_detail_video(raw, uid, video_id="keep-favorite")
        _seed_featured_video(raw, video_id="drop-deleted")
        raw.execute(
            "UPDATE viral_videos SET deleted_at=CURRENT_TIMESTAMP "
            "WHERE platform='douyin' AND video_id='drop-deleted'"
        )
        _seed_featured_video(raw, video_id="drop-hidden")
        raw.execute(
            "INSERT INTO viral_video_visibility (platform, video_id, status) "
            "VALUES ('douyin', 'drop-hidden', 'HIDDEN')"
        )
    favorited = detail_client.put("/api/viral/favorites/douyin/keep-favorite", headers=headers)
    assert favorited.status_code == 200, favorited.text
    response = detail_client.post(
        "/api/viral/videos/cache-reclaim",
        headers=headers,
        json={
            "platform": "douyin",
            # 重复上报的条目按一次处理，客户端不必先去重。
            "videoIds": [
                "keep-live",
                "keep-favorite",
                "drop-deleted",
                "drop-hidden",
                "drop-unknown",
                "keep-live",
            ],
        },
    )
    assert response.status_code == 200, response.text
    assert response.json() == {
        "platform": "douyin",
        "reclaim": ["drop-deleted", "drop-hidden", "drop-unknown"],
    }
    invalid = detail_client.post(
        "/api/viral/videos/cache-reclaim",
        headers=headers,
        json={"platform": "kuaishou", "videoIds": ["keep-live"]},
    )
    assert invalid.status_code == 400
    assert invalid.json()["detail"]["code"] == "VIRAL_PLATFORM_INVALID"
    empty = detail_client.post(
        "/api/viral/videos/cache-reclaim",
        headers=headers,
        json={"platform": "douyin", "videoIds": []},
    )
    assert empty.status_code == 422
    with psycopg.connect(route_state) as raw:
        assert _wallet(raw, uid) == (50, 0)
        assert (
            raw.execute(
                "SELECT count(*) FROM billing_operations WHERE user_id=%s", (uid,)
            ).fetchone()[0]
            == 0
        )


# ---------------------------------------------------------------- 可见性守卫（公开端点组）


def _seed_copy_video(
    raw,
    uid: str,
    *,
    platform: str = "douyin",
    video_id: str,
) -> None:
    """内容池里的一条已就绪视频（可见性守卫用例只依赖它存在且有钱）."""
    raw.execute(
        "INSERT INTO viral_videos (platform, video_id, title, author, native_json) "
        "VALUES (%s, %s, 'T', 'A', '{}') ON CONFLICT (platform, video_id) "
        "DO UPDATE SET deleted_at = NULL",
        (platform, video_id),
    )
    _publish_ready(raw, platform=platform, video_id=video_id)
    raw.execute(
        "UPDATE wallets SET available_credits=50, reserved_credits=0 WHERE user_id=%s", (uid,)
    )
    raw.commit()


def test_http_unpublished_detail_denies_both_accounts_without_billing(detail_client, route_state):
    headers_a, uid_a = account(detail_client, "visibility_http_a")
    headers_b, uid_b = account(detail_client, "visibility_http_b")
    with psycopg.connect(route_state) as raw:
        _seed_detail_video(raw, uid_a, video_id="visibility-http-unpublished")
        raw.execute(
            "UPDATE viral_videos SET collection_published=0,homepage_featured=0 "
            "WHERE video_id='visibility-http-unpublished'"
        )
        before = {uid: _wallet(raw, uid) for uid in (uid_a, uid_b)}
    for headers in (headers_a, headers_b):
        free = detail_client.get(
            "/api/viral/videos/douyin/visibility-http-unpublished", headers=headers
        )
        paid = _open_detail(detail_client, headers, video_id="visibility-http-unpublished")
        assert (free.status_code, paid.status_code) == (404, 404), paid.text
    with psycopg.connect(route_state) as raw:
        assert {uid: _wallet(raw, uid) for uid in (uid_a, uid_b)} == before
        for uid in (uid_a, uid_b):
            assert (
                _detail_operations(raw, f"viral-detail:{uid}:douyin:visibility-http-unpublished")
                == []
            )


@pytest.mark.parametrize(
    "state", ["unpublished", "preparing", "future", "expired", "hidden", "deleted", "private"]
)
def test_http_visibility_decisions_match_for_two_accounts(detail_client, route_state, state):
    from app.studio_routes import router as studio_router

    detail_client.app.include_router(studio_router)
    headers_a, uid_a = account(detail_client, f"visibility_a_{state}")
    headers_b, uid_b = account(detail_client, f"visibility_b_{state}")
    video_id = f"visibility-{state}"
    with psycopg.connect(route_state) as raw:
        _seed_detail_video(raw, uid_a, video_id=video_id)
        raw.execute("UPDATE viral_videos SET title=%s WHERE video_id=%s", (video_id, video_id))
        if state in ("unpublished", "private"):
            raw.execute(
                "UPDATE viral_videos SET collection_published=0,homepage_featured=0 "
                "WHERE video_id=%s",
                (video_id,),
            )
        if state == "preparing":
            raw.execute(
                "UPDATE viral_media_preparations SET status='PENDING' WHERE video_id=%s",
                (video_id,),
            )
        if state == "future":
            raw.execute(
                "UPDATE viral_videos SET homepage_starts_at=now()+interval '1 day' "
                "WHERE video_id=%s",
                (video_id,),
            )
        if state == "expired":
            raw.execute(
                "UPDATE viral_videos SET homepage_ends_at=now()-interval '1 day' WHERE video_id=%s",
                (video_id,),
            )
        if state == "hidden":
            raw.execute(
                "INSERT INTO viral_video_visibility(platform,video_id,status) "
                "VALUES('douyin',%s,'HIDDEN')",
                (video_id,),
            )
        if state == "deleted":
            raw.execute("UPDATE viral_videos SET deleted_at=now() WHERE video_id=%s", (video_id,))
        if state == "private":
            raw.execute(
                "INSERT INTO viral_import_tasks(id,owner_user_id,platform,video_id,purpose,"
                "idempotency_key,request_hash,request_json,status) VALUES"
                "(%s,%s,'douyin',%s,'replica',%s,'test','{}','SUCCEEDED')",
                (video_id, uid_a, video_id, video_id),
            )
        before = {uid: _wallet(raw, uid) for uid in (uid_a, uid_b)}
    for headers, uid in ((headers_b, uid_b), (headers_a, uid_a)):
        allowed = state == "private" and uid == uid_a
        free = detail_client.get(f"/api/viral/videos/douyin/{video_id}", headers=headers)
        paid = _open_detail(detail_client, headers, video_id=video_id)
        assert (free.status_code, paid.status_code) == ((200, 200) if allowed else (404, 404)), (
            paid.text
        )
        search = detail_client.get(
            "/api/studio/search", headers=headers, params={"q": video_id, "kind": "video"}
        )
        assert search.status_code == 200, search.text
        assert search.json()["total"] == int(allowed)
        with psycopg.connect(route_state) as raw:
            if allowed:
                # 查看详情免费（2026-10-13 拍板）：可见即放行，且不再产生任何计费回执。
                assert paid.json()["billing"]["charged"] == 0
                assert paid.json()["billing"]["billable"] is False
            assert _wallet(raw, uid) == before[uid]
            assert _detail_operations(raw, f"viral-detail:{uid}:douyin:{video_id}") == []
            assert (
                raw.execute(
                    "SELECT count(*) FROM wallet_transactions WHERE user_id=%s "
                    "AND type IN ('RESERVE','SETTLE')",
                    (uid,),
                ).fetchone()[0]
                == 0
            )


@pytest.mark.parametrize(
    "endpoint",
    ["source", "media", "cover", "download", "favorite", "statistics"],
)
def test_http_known_id_cannot_bypass_unpublished_guard(
    detail_client, route_state, monkeypatch, endpoint
):
    """已知 videoId 不能绕过未发布守卫；claim/copy_status 端点已退役不再入列."""
    import time

    from cryptography.fernet import Fernet

    from app import viral_routes
    from app.storage import FakeStorageAdapter, local_download_signature

    monkeypatch.setenv("VIDEO_REPLICA_SETTINGS_KEY", Fernet.generate_key().decode())

    headers_a, uid_a = account(detail_client, f"known_owner_{endpoint}")
    headers_b, uid_b = account(detail_client, f"known_other_{endpoint}")
    platform, video_id = "wechat_channels", f"known-{endpoint}"
    key = f"viral/{platform}/{video_id}.mp4"
    storage = FakeStorageAdapter(provider="fake", bucket="visibility-test")
    storage.put_object(key, b"isolated-test-video", content_type="video/mp4")
    storage.put_object(
        viral_routes.viral_cover_key(platform, video_id),
        b"isolated-cover",
        content_type="image/jpeg",
    )
    monkeypatch.setattr(viral_routes, "get_media_storage", lambda conn: storage)

    def forbidden_external(*args, **kwargs):
        raise AssertionError("denied video must not start a source or media request")

    monkeypatch.setattr(viral_routes, "_resolve_source_link", forbidden_external)
    monkeypatch.setattr(viral_routes.ViralMediaPipeline, "fetch", forbidden_external)
    with psycopg.connect(route_state) as raw:
        _seed_copy_video(raw, uid_a, platform=platform, video_id=video_id)
        raw.execute(
            "UPDATE viral_videos SET collection_published=0,homepage_featured=0 WHERE video_id=%s",
            (video_id,),
        )
        before = {uid: _wallet(raw, uid) for uid in (uid_a, uid_b)}
    for headers, uid in ((headers_a, uid_a), (headers_b, uid_b)):
        headers = {**headers, "Idempotency-Key": f"known-{endpoint}-{uid}"}
        payload = {"platform": platform, "videoId": video_id}
        if endpoint in ("source", "media"):
            result = detail_client.post(
                f"/api/viral/videos/{endpoint}", headers=headers, json=payload
            )
        elif endpoint == "cover":
            result = detail_client.get(f"/api/viral/covers/{platform}/{video_id}", headers=headers)
        elif endpoint == "favorite":
            result = detail_client.put(
                f"/api/viral/favorites/{platform}/{video_id}", headers=headers
            )
        elif endpoint == "statistics":
            result = detail_client.post(
                "/api/viral/videos/statistics", headers=headers, json={"videoIds": [video_id]}
            )
        else:
            expires = str(int(time.time()) + 300)
            signature = local_download_signature(
                key,
                expires,
                user_id=uid,
                asset_id="viral-media",
                session_epoch="viral",
                secret=viral_routes.settings_encryption_key(),
            )
            result = detail_client.get(
                "/api/viral/videos/media/file",
                headers=headers,
                params={"key": key, "expires": expires, "user_id": uid, "sig": signature},
            )
        if endpoint == "statistics":
            assert result.status_code == 200 and result.json() == {"items": []}
        else:
            assert result.status_code == 404, (endpoint, result.status_code, result.text)
        with psycopg.connect(route_state) as raw:
            assert _wallet(raw, uid) == before[uid]
            assert (
                raw.execute(
                    "SELECT count(*) FROM billing_operations WHERE user_id=%s", (uid,)
                ).fetchone()[0]
                == 0
            )
            assert (
                raw.execute(
                    "SELECT count(*) FROM viral_video_favorites WHERE user_id=%s AND video_id=%s",
                    (uid, video_id),
                ).fetchone()[0]
                == 0
            )


@pytest.mark.parametrize("state", ["public", "private", "withdrawn", "admin_preview"])
def test_http_signed_download_rechecks_current_visibility_and_cover_is_public_only(
    detail_client, route_state, monkeypatch, state
):
    import time

    from cryptography.fernet import Fernet

    from app import viral_routes
    from app.storage import FakeStorageAdapter, local_download_signature

    monkeypatch.setenv("VIDEO_REPLICA_SETTINGS_KEY", Fernet.generate_key().decode())

    headers_a, uid_a = account(detail_client, f"download_owner_{state}")
    headers_b, uid_b = account(detail_client, f"download_other_{state}")
    video_id = f"download-{state}"
    key = f"viral/douyin/{video_id}.mp4"
    storage = FakeStorageAdapter(provider="fake", bucket="download-http")
    stored = storage.put_object(key, b"isolated-media-bytes", content_type="video/mp4")
    storage.put_object(
        viral_routes.viral_cover_key("douyin", video_id),
        b"isolated-cover",
        content_type="image/jpeg",
    )
    monkeypatch.setattr(viral_routes, "get_media_storage", lambda conn: storage)
    expires = str(int(time.time()) + 300)
    signatures = {
        uid: local_download_signature(
            key,
            expires,
            user_id=uid,
            asset_id="viral-media",
            session_epoch="viral",
            secret=viral_routes.settings_encryption_key(),
        )
        for uid in (uid_a, uid_b)
    }
    with psycopg.connect(route_state) as raw:
        _seed_detail_video(raw, uid_a, video_id=video_id)
        raw.execute(
            "UPDATE viral_media_preparations SET storage_uri=%s WHERE video_id=%s",
            (stored.uri, video_id),
        )
        if state == "admin_preview":
            raw.execute("UPDATE users SET role='admin' WHERE id=%s", (uid_a,))
            raw.execute(
                "UPDATE viral_videos SET collection_published=0 WHERE video_id=%s", (video_id,)
            )
        if state == "private":
            raw.execute(
                "UPDATE viral_videos SET collection_published=0 WHERE video_id=%s", (video_id,)
            )
            raw.execute(
                "INSERT INTO viral_import_tasks(id,owner_user_id,platform,video_id,purpose,"
                "idempotency_key,request_hash,request_json,status) VALUES"
                "(%s,%s,'douyin',%s,'replica',%s,'test','{}','SUCCEEDED')",
                (video_id, uid_a, video_id, video_id),
            )
        elif state == "withdrawn":
            raw.execute(
                "INSERT INTO viral_video_visibility(platform,video_id,status) "
                "VALUES('douyin',%s,'HIDDEN')",
                (video_id,),
            )
    for headers, uid in ((headers_a, uid_a), (headers_b, uid_b)):
        result = detail_client.get(
            "/api/viral/videos/media/file",
            headers=headers,
            params={"key": key, "expires": expires, "user_id": uid, "sig": signatures[uid]},
        )
        allowed = state == "public" or state in ("private", "admin_preview") and uid == uid_a
        assert result.status_code == (200 if allowed else 404), result.text
        if allowed:
            assert result.content == b"isolated-media-bytes"
    cover = detail_client.get(f"/api/viral/covers/douyin/{video_id}")
    assert cover.status_code == (200 if state == "public" else 404), cover.text
    if state == "public":
        assert cover.content == b"isolated-cover"


def test_http_source_withdrawn_during_external_call_releases_reservation(
    detail_client, route_state, monkeypatch
):
    from app import viral_routes

    headers, uid = account(detail_client, "source_during_withdraw")
    video_id = "source-during-withdraw"
    with psycopg.connect(route_state) as raw:
        _seed_detail_video(raw, uid, video_id=video_id)
        raw.execute(
            "INSERT INTO billing_tariffs(service,enabled,unit_credits) "
            "VALUES('viral_search_refresh',true,2) ON CONFLICT(service) "
            "DO UPDATE SET enabled=true,unit_credits=2"
        )
    calls = []

    def withdraw(*args, **kwargs):
        calls.append(video_id)
        with psycopg.connect(route_state) as raw:
            assert _wallet(raw, uid) == (48, 2)
            raw.execute(
                "INSERT INTO viral_video_visibility(platform,video_id,status) "
                "VALUES('douyin',%s,'HIDDEN')",
                (video_id,),
            )
        return "https://example.invalid/never-fetched.mp4", None

    monkeypatch.setattr(viral_routes, "_resolve_source_link", withdraw)
    detail_client.app.dependency_overrides[viral_routes.get_viral_source_client] = lambda: object()
    result = detail_client.post(
        "/api/viral/videos/source",
        headers={**headers, "Idempotency-Key": "withdraw-during-call"},
        json={"platform": "douyin", "videoId": video_id},
    )
    assert result.status_code == 404, result.text
    assert calls == [video_id]
    with psycopg.connect(route_state) as raw:
        assert _wallet(raw, uid) == (50, 0)
        rows = raw.execute(
            "SELECT state,charged_credits FROM billing_operations "
            "WHERE user_id=%s AND service='viral_search_refresh'",
            (uid,),
        ).fetchall()
        assert rows == [("FAILED", 0)]
