"""爆款视频路由测试：列表聚合、缓存护栏、媒体动作与供应商红线."""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from app.auth import Database, get_database
from app.db import connect_database, initialize_database
from app.db_portable import BusinessConnection
from app.main import app
from app.viral_routes import (
    VIRAL_LIST_CACHE_TTL,
    ViralSourceClient,
    _now,
    get_viral_source_client,
)


class StubViralClient(ViralSourceClient):
    """不发起网络请求的桩客户端."""

    def __init__(self) -> None:
        super().__init__(api_key="stub-key")
        self.douyin_calls: list[dict[str, Any]] = []
        self.wechat_calls: list[dict[str, Any]] = []

    def douyin_search(self, *, keyword, category="", sort_type="1", publish_time="7"):
        self.douyin_calls.append(
            {
                "keyword": keyword,
                "category": category,
                "sort_type": sort_type,
                "publish_time": publish_time,
            }
        )
        return [
            _video(
                platform="douyin",
                video_id=f"dy-{keyword}-{index}",
                category=category,
                audio_url=f"https://cdn.test/{keyword}-{index}.mp3",
            )
            for index in range(2)
        ]

    def wechat_search(self, *, keyword, category="", sort="hot", publish_time="week", pages=3):
        self.wechat_calls.append({"keyword": keyword, "category": category, "sort": sort})
        return [
            _video(
                platform="wechat_channels",
                video_id=f"wx-{keyword}-{index}",
                category=category,
            )
            for index in range(3)
        ]


_AUTH_HEADERS = {"X-Dev-User-Id": "employee_1"}


def _video(
    *,
    platform: str,
    video_id: str,
    category: str,
    audio_url: str | None = None,
) -> Any:
    from app.viral_tikhub import ViralVideo

    return ViralVideo(
        platform=platform,
        video_id=video_id,
        category=category,
        title=f"标题 {video_id} #标签一 #标签二",
        author="作者",
        author_avatar=None,
        verified=platform == "douyin",
        cover_url=f"https://cdn.test/{video_id}.jpg",
        duration_ms=11_000,
        likes=123,
        comments=4 if platform == "douyin" else None,
        shares=5 if platform == "douyin" else None,
        collects=6 if platform == "douyin" else None,
        published_at=1788602461,
        published_display="1天前" if platform == "wechat_channels" else None,
        like_display="1.2万" if platform == "wechat_channels" else None,
        tags=["标签一", "标签二"],
        play_url=f"https://cdn.test/{video_id}.mp4",
        audio_url=audio_url,
        native={"doc_id": video_id, "export_id": "export/e1"}
        if platform == "wechat_channels"
        else {"aweme_id": video_id, "_playback_version": 1},
    )


@pytest.fixture()
def client(tmp_path: Path) -> Iterator[tuple[TestClient, StubViralClient]]:
    database_path = tmp_path / "viral.db"
    with initialize_database(database_path) as conn:
        conn.execute(
            """
            INSERT INTO users (id, username, display_name, role)
            VALUES ('employee_1', 'employee_1', 'Employee One', 'employee')
            """
        )
        conn.execute(
            """
            INSERT INTO users (id, username, display_name, role)
            VALUES ('employee_2', 'employee_2', 'Employee Two', 'employee')
            """
        )

    def database_override() -> Iterator[BusinessConnection]:
        conn = BusinessConnection.sqlite(connect_database(database_path))
        try:
            yield conn
        finally:
            conn.close()

    stub = StubViralClient()

    def viral_client_override(conn: Database) -> ViralSourceClient:
        return stub

    app.dependency_overrides[get_database] = database_override
    app.dependency_overrides[get_viral_source_client] = viral_client_override
    try:
        with pytest.MonkeyPatch.context() as mp:
            # 后台封面落存储线程按环境变量自开连接，指向同一测试库。
            mp.setenv("VIDEO_REPLICA_DB_PATH", str(database_path))
            yield TestClient(app), stub
    finally:
        app.dependency_overrides.clear()


def test_list_douyin_videos_aggregates_categories(
    client: tuple[TestClient, StubViralClient],
) -> None:
    http, stub = client
    response = http.get("/api/viral/videos", params={"platform": "douyin"}, headers=_AUTH_HEADERS)
    assert response.status_code == 200
    payload = response.json()
    assert payload["platform"] == "douyin"
    assert payload["categories"] == ["建房预算", "户型设计", "施工避坑", "庭院案例"]
    # 每个分类 2 条，共 8 条（不同分类 video_id 不同，无去重合并）。
    assert len(payload["items"]) == 8
    assert stub.douyin_calls[0]["sort_type"] == "1"
    assert stub.douyin_calls[0]["publish_time"] == "7"
    first = payload["items"][0]
    assert first["platform"] == "douyin"
    assert first["tags"] == ["标签一", "标签二"]
    assert first["hasPlayableAudio"] is True
    assert first["isFavorite"] is False
    assert first["availability"] == "available"
    assert payload["total"] == 8
    assert payload["hasMore"] is False
    assert payload["nextCursor"] is None
    assert payload["refreshing"] is False
    assert payload["refreshError"] is None
    assert payload["dataVersion"] == payload["fetchedAt"]
    # 供应商红线：响应正文不得出现数据源供应商名称。
    assert "tikhub" not in response.text.lower()


def test_list_videos_uses_cache_within_ttl(client: tuple[TestClient, StubViralClient]) -> None:
    http, stub = client
    first = http.get("/api/viral/videos", params={"platform": "douyin"}, headers=_AUTH_HEADERS)
    assert first.status_code == 200
    calls_after_first = len(stub.douyin_calls)
    second = http.get("/api/viral/videos", params={"platform": "douyin"}, headers=_AUTH_HEADERS)
    assert second.status_code == 200
    assert len(stub.douyin_calls) == calls_after_first


def test_refresh_does_not_hold_database_connection_during_upstream_calls(
    client: tuple[TestClient, StubViralClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.viral_routes as routes

    held = False
    original_refresh_connection = routes._refresh_connection

    @contextmanager
    def tracked_refresh_connection(request_conn):
        nonlocal held
        with original_refresh_connection(request_conn) as refresh_conn:
            held = True
            try:
                yield refresh_conn
            finally:
                held = False

    original_fetch = routes._fetch_videos

    def guarded_fetch(*args, **kwargs):
        assert held is False
        return original_fetch(*args, **kwargs)

    monkeypatch.setattr(routes, "_refresh_connection", tracked_refresh_connection)
    monkeypatch.setattr(routes, "_fetch_videos", guarded_fetch)
    http, _ = client

    response = http.get("/api/viral/videos", params={"platform": "douyin"}, headers=_AUTH_HEADERS)

    assert response.status_code == 200


def test_list_videos_refetches_after_ttl(client: tuple[TestClient, StubViralClient]) -> None:
    http, stub = client
    assert (
        http.get(
            "/api/viral/videos", params={"platform": "douyin"}, headers=_AUTH_HEADERS
        ).status_code
        == 200
    )
    calls_after_first = len(stub.douyin_calls)
    # 把拉取状态拨回到 TTL 之外（库内条目保留，回源按视频 ID 去重刷新）。
    stale = (_now() - VIRAL_LIST_CACHE_TTL - timedelta(minutes=1)).isoformat()
    from app.viral_routes import _open_worker_connection

    db_conn, close = _open_worker_connection()
    try:
        db_conn.execute("UPDATE viral_fetch_state SET fetched_at = %s", (stale,))
        db_conn.commit()
    finally:
        close()
    assert (
        http.get(
            "/api/viral/videos", params={"platform": "douyin"}, headers=_AUTH_HEADERS
        ).status_code
        == 200
    )
    assert len(stub.douyin_calls) > calls_after_first


def test_collection_switch_keeps_cache_readable_without_upstream_calls(
    client: tuple[TestClient, StubViralClient],
) -> None:
    http, stub = client
    first = http.get(
        "/api/viral/videos",
        params={"platform": "douyin"},
        headers=_AUTH_HEADERS,
    )
    assert first.status_code == 200
    calls_after_first = len(stub.douyin_calls)
    from app.viral_routes import _open_worker_connection

    conn, close = _open_worker_connection()
    try:
        conn.execute("UPDATE viral_runtime_controls SET collection_enabled = 0 WHERE id = 1")
        conn.execute("UPDATE viral_fetch_state SET fetched_at = '2000-01-01 00:00:00'")
        conn.commit()
    finally:
        close()

    cached = http.get(
        "/api/viral/videos",
        params={"platform": "douyin"},
        headers=_AUTH_HEADERS,
    )
    assert cached.status_code == 200
    assert cached.json()["items"]
    assert cached.json()["stale"] is True
    assert len(stub.douyin_calls) == calls_after_first


def test_list_wechat_videos_and_latest_sort(client: tuple[TestClient, StubViralClient]) -> None:
    http, stub = client
    response = http.get(
        "/api/viral/videos",
        params={"platform": "wechat_channels", "sort": "latest"},
        headers=_AUTH_HEADERS,
    )
    assert response.status_code == 200
    assert len(response.json()["items"]) == 12
    assert stub.wechat_calls[0]["sort"] == "latest"
    latest_item = response.json()["items"][0]
    assert latest_item["likeDisplay"] == "1.2万"


def test_list_videos_supports_opaque_cursor(client: tuple[TestClient, StubViralClient]) -> None:
    http, stub = client
    first = http.get(
        "/api/viral/videos",
        params={"platform": "wechat_channels", "limit": 5},
        headers=_AUTH_HEADERS,
    )
    assert first.status_code == 200
    first_payload = first.json()
    assert len(first_payload["items"]) == 5
    assert first_payload["total"] == 12
    assert first_payload["hasMore"] is True
    assert isinstance(first_payload["nextCursor"], str)
    assert "wx-" not in first_payload["nextCursor"]

    second = http.get(
        "/api/viral/videos",
        params={
            "platform": "wechat_channels",
            "limit": 5,
            "cursor": first_payload["nextCursor"],
        },
        headers=_AUTH_HEADERS,
    )
    assert second.status_code == 200
    assert not (
        {item["videoId"] for item in first_payload["items"]}
        & {item["videoId"] for item in second.json()["items"]}
    )

    calls_before_invalid = len(stub.wechat_calls)
    invalid = http.get(
        "/api/viral/videos",
        params={"platform": "wechat_channels", "cursor": "not-a-cursor"},
        headers=_AUTH_HEADERS,
    )
    assert invalid.status_code == 400
    assert invalid.json()["detail"]["code"] == "VIRAL_CURSOR_INVALID"
    assert len(stub.wechat_calls) == calls_before_invalid


def test_detail_and_favorites_use_authenticated_actor(
    client: tuple[TestClient, StubViralClient],
) -> None:
    http, _ = client
    seeded = http.get(
        "/api/viral/videos",
        params={"platform": "douyin"},
        headers=_AUTH_HEADERS,
    )
    video_id = seeded.json()["items"][0]["videoId"]

    detail = http.get(f"/api/viral/videos/douyin/{video_id}", headers=_AUTH_HEADERS)
    assert detail.status_code == 200
    assert detail.json()["videoId"] == video_id
    assert detail.json()["isFavorite"] is False

    added = http.put(
        f"/api/viral/favorites/douyin/{video_id}?user_id=employee_2",
        headers=_AUTH_HEADERS,
    )
    assert added.status_code == 200
    assert added.json() == {"isFavorite": True}
    assert http.put(f"/api/viral/favorites/douyin/{video_id}", headers=_AUTH_HEADERS).json() == {
        "isFavorite": True
    }

    favorites = http.get("/api/viral/favorites", headers=_AUTH_HEADERS)
    assert favorites.status_code == 200
    assert favorites.json()["total"] == 1
    assert favorites.json()["items"][0]["videoId"] == video_id
    assert favorites.json()["items"][0]["isFavorite"] is True
    assert (
        http.get("/api/viral/favorites", headers={"X-Dev-User-Id": "employee_2"}).json()["total"]
        == 0
    )

    removed = http.delete(f"/api/viral/favorites/douyin/{video_id}", headers=_AUTH_HEADERS)
    assert removed.status_code == 200
    assert removed.json() == {"isFavorite": False}
    assert http.get("/api/viral/favorites", headers=_AUTH_HEADERS).json()["total"] == 0


def test_detail_supports_encoded_opaque_video_id(
    client: tuple[TestClient, StubViralClient],
) -> None:
    from app.viral_routes import _open_worker_connection
    from app.viral_store import upsert_viral_videos

    opaque_id = "opaque/id=value"
    conn, close = _open_worker_connection()
    try:
        upsert_viral_videos(
            conn,
            [_video(platform="douyin", video_id=opaque_id, category="施工避坑")],
        )
    finally:
        close()
    http, _ = client

    detail = http.get(
        f"/api/viral/videos/douyin/{quote(opaque_id, safe='')}",
        headers=_AUTH_HEADERS,
    )

    assert detail.status_code == 200
    assert detail.json()["videoId"] == opaque_id


def test_favorites_route_exposes_pagination_contract(
    client: tuple[TestClient, StubViralClient],
) -> None:
    http, _ = client
    seeded = http.get(
        "/api/viral/videos",
        params={"platform": "wechat_channels", "limit": 12},
        headers=_AUTH_HEADERS,
    )
    assert seeded.status_code == 200
    for item in seeded.json()["items"][:3]:
        assert (
            http.put(
                f"/api/viral/favorites/wechat_channels/{item['videoId']}",
                headers=_AUTH_HEADERS,
            ).status_code
            == 200
        )

    first = http.get(
        "/api/viral/favorites",
        params={"limit": 2},
        headers=_AUTH_HEADERS,
    )
    assert first.status_code == 200
    assert len(first.json()["items"]) == 2
    assert first.json()["total"] == 3
    assert first.json()["hasMore"] is True
    assert isinstance(first.json()["nextCursor"], str)

    second = http.get(
        "/api/viral/favorites",
        params={"limit": 2, "cursor": first.json()["nextCursor"]},
        headers=_AUTH_HEADERS,
    )
    assert second.status_code == 200
    assert len(second.json()["items"]) == 1
    assert second.json()["hasMore"] is False


def test_detail_reports_hidden_availability(client: tuple[TestClient, StubViralClient]) -> None:
    http, _ = client
    response = http.get(
        "/api/viral/videos",
        params={"platform": "douyin"},
        headers=_AUTH_HEADERS,
    )
    video_id = response.json()["items"][0]["videoId"]
    assert (
        http.put(f"/api/viral/favorites/douyin/{video_id}", headers=_AUTH_HEADERS).status_code
        == 200
    )
    from app.viral_routes import _open_worker_connection

    conn, close = _open_worker_connection()
    try:
        conn.execute(
            """
            INSERT INTO viral_video_visibility (platform, video_id, status)
            VALUES (%s, %s, 'HIDDEN')
            """,
            ("douyin", video_id),
        )
        conn.commit()
    finally:
        close()

    detail = http.get(f"/api/viral/videos/douyin/{video_id}", headers=_AUTH_HEADERS)
    assert detail.status_code == 200
    assert detail.json()["availability"] == "hidden"
    favorites = http.get(
        "/api/viral/favorites",
        params={"platform": "douyin"},
        headers=_AUTH_HEADERS,
    )
    assert favorites.status_code == 200
    assert len(favorites.json()["items"]) == 1
    favorite = favorites.json()["items"][0]
    assert favorite["videoId"] == video_id
    assert favorite["isFavorite"] is True
    assert favorite["availability"] == "hidden"
    listed = http.get("/api/viral/videos", params={"platform": "douyin"}, headers=_AUTH_HEADERS)
    assert video_id not in {item["videoId"] for item in listed.json()["items"]}
    media = http.post(
        "/api/viral/videos/media",
        json={"platform": "douyin", "videoId": video_id, "kind": "video"},
        headers=_AUTH_HEADERS,
    )
    assert media.status_code == 409
    assert media.json()["detail"]["code"] == "VIRAL_VIDEO_UNAVAILABLE"


def test_list_videos_rejects_invalid_platform(client: tuple[TestClient, StubViralClient]) -> None:
    http, _ = client
    response = http.get("/api/viral/videos", params={"platform": "kuaishou"}, headers=_AUTH_HEADERS)
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "VIRAL_PLATFORM_INVALID"


def test_media_returns_url_and_red_line_holds(
    client: tuple[TestClient, StubViralClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    http, _ = client

    class StubPipeline:
        def __init__(self, *, client, storage) -> None:
            pass

        def fetch(self, video, *, prefer=None):
            from app.viral_media import ViralMediaResult

            return ViralMediaResult(
                kind="audio",
                storage_uri=f"test://viral/douyin/{video.video_id}.mp3",
                url=f"https://storage.test/viral/douyin/{video.video_id}.mp3",
                size=1024,
                content_type="audio/mpeg",
                cache_hit=False,
            )

    import app.viral_routes as routes

    monkeypatch.setattr(routes, "ViralMediaPipeline", StubPipeline)
    monkeypatch.setattr(routes, "get_media_storage", lambda conn: object())
    response = http.post(
        "/api/viral/videos/media",
        json={"platform": "douyin", "videoId": "dy-自建房预算-0"},
        headers=_AUTH_HEADERS,
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["kind"] == "audio"
    assert payload["contentType"] == "audio/mpeg"
    assert payload["url"].startswith("https://storage.test/")
    assert "tikhub" not in response.text.lower()


def test_media_unknown_video_returns_404(client: tuple[TestClient, StubViralClient]) -> None:
    http, _ = client
    response = http.post(
        "/api/viral/videos/media",
        json={"platform": "douyin", "videoId": "missing-id"},
        headers=_AUTH_HEADERS,
    )
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "VIRAL_VIDEO_NOT_FOUND"


class _StubLocalStorage:
    """最小本地存储桩：head/stream 驱动签名文件路由."""

    def __init__(self, objects: dict[str, tuple[bytes, str]]) -> None:
        self.objects = objects

    def head_object(self, key):
        from types import SimpleNamespace

        entry = self.objects.get(key)
        if entry is None:
            return None
        content, content_type = entry
        return SimpleNamespace(
            key=key,
            uri=f"local://local-private/{key}",
            size=len(content),
            content_type=content_type,
        )

    def get_object(self, key):
        return self.objects[key][0]

    def iter_object(self, key, *, start=0, end=None, chunk_size=1024 * 1024):
        content = self.objects[key][0]
        stop = len(content) if end is None else end + 1
        for offset in range(start, stop, chunk_size):
            yield content[offset : min(stop, offset + chunk_size)]


def test_local_media_url_converts_to_signed_file_route(
    client: tuple[TestClient, StubViralClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    http, _ = client

    class StubPipeline:
        def __init__(self, *, client, storage) -> None:
            pass

        def fetch(self, video, *, prefer=None):
            from app.viral_media import ViralMediaResult

            return ViralMediaResult(
                kind="audio",
                storage_uri=f"local://local-private/viral/douyin/{video.video_id}.mp3",
                url=f"local://local-private/viral/douyin/{video.video_id}.mp3?method=GET&expires=1",
                size=16,
                content_type="audio/mpeg",
                cache_hit=False,
            )

    import app.viral_routes as routes

    monkeypatch.setattr(
        routes,
        "settings_encryption_key",
        lambda: "MTIzNDU2Nzg5MGFiY2RlZjAxMjM0NTY3ODkwYWJjZGVm",
    )
    video_id = "dy-自建房预算-0"
    storage_key = f"viral/douyin/{video_id}.mp3"
    monkeypatch.setattr(routes, "ViralMediaPipeline", StubPipeline)
    monkeypatch.setattr(
        routes,
        "get_media_storage",
        lambda conn: _StubLocalStorage({storage_key: (b"ID3-fake-audio", "audio/mpeg")}),
    )
    response = http.post(
        "/api/viral/videos/media",
        json={"platform": "douyin", "videoId": video_id},
        headers=_AUTH_HEADERS,
    )
    assert response.status_code == 200
    url = response.json()["url"]
    assert "/api/viral/videos/media/file?key=viral/douyin/" in url
    assert url.startswith("http://")  # 绝对地址：跨源前端可直接使用
    # 签名 URL 自带授权，浏览器直接 GET 可取回字节。
    file_response = http.get(url)
    assert file_response.status_code == 200
    assert file_response.content == b"ID3-fake-audio"
    assert file_response.headers["content-type"] == "audio/mpeg"
    assert file_response.headers["accept-ranges"] == "bytes"
    ranged = http.get(url, headers={"Range": "bytes=4-7"})
    assert ranged.status_code == 206
    assert ranged.content == b"ID3-fake-audio"[4:8]
    assert ranged.headers["content-range"] == "bytes 4-7/14"
    assert ranged.headers["content-length"] == "4"
    unsatisfiable = http.get(url, headers={"Range": "bytes=99-"})
    assert unsatisfiable.status_code == 416
    assert unsatisfiable.headers["content-range"] == "bytes */14"
    # 篡改签名被拒绝。
    tampered = url.replace("sig=", "sig=x")
    assert http.get(tampered).status_code == 403
    # 非 viral 前缀的对象 key 被拒绝。
    assert (
        http.get(
            "/api/viral/videos/media/file?key=projects%2Fx.bin&expires=9999999999&user_id=admin1&sig=abc"
        ).status_code
        == 403
    )


class _CoverStorage(_StubLocalStorage):
    """可写的存储桩：验证封面落存储与命中."""

    def __init__(self) -> None:
        super().__init__({})
        self.put_calls: list[str] = []

    def put_object(self, key, content, *, content_type):
        from types import SimpleNamespace

        self.objects[key] = (content, content_type)
        self.put_calls.append(key)
        return SimpleNamespace(
            key=key,
            uri=f"local://local-private/{key}",
            size=len(content),
            content_type=content_type,
        )


class _OkFetcher:
    def fetch(self, url: str) -> bytes:
        return b"fake-cover-bytes"


def test_list_enriches_covers_into_storage(
    client: tuple[TestClient, StubViralClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.viral_routes as routes

    storage = _CoverStorage()
    monkeypatch.setattr(routes, "get_media_storage", lambda conn: storage)
    monkeypatch.setattr(routes, "UrlFetcher", lambda *a, **kw: _OkFetcher())
    http = client[0]
    result = http.get("/api/viral/videos", params={"platform": "douyin"}, headers=_AUTH_HEADERS)
    assert result.status_code == 200
    items = result.json()["items"]
    assert items, "列表应有条目"

    # 后台落存储是异步的：轮询直到封面副本入库并回写 cover_key。
    deadline = time.time() + 10
    refreshed = items[0]
    while time.time() < deadline:
        again = http.get("/api/viral/videos", params={"platform": "douyin"}, headers=_AUTH_HEADERS)
        assert again.status_code == 200
        refreshed = again.json()["items"][0]
        if "/api/viral/covers/douyin/" in refreshed["coverUrl"]:
            break
        time.sleep(0.05)

    assert storage.put_calls, "后台封面落存储应发生"
    assert "/api/viral/covers/douyin/" in refreshed["coverUrl"], (
        "封面副本入库后列表应换成自有稳定地址"
    )
    # 稳定路由直接可取，无需登录。
    cover = http.get(refreshed["coverUrl"])
    assert cover.status_code == 200
    assert cover.content == b"fake-cover-bytes"
    assert cover.headers["content-type"].startswith("image/")

    # 命中存储副本后不再重复下载：清空记录，再次请求列表应零新增。
    storage.put_calls.clear()
    deadline = time.time() + 2
    while time.time() < deadline and not storage.put_calls:
        time.sleep(0.05)
    assert not storage.put_calls, "已有副本的视频不应重复下载"


def test_cover_route_rejects_bad_ids(
    client: tuple[TestClient, StubViralClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    http, _ = client
    monkeypatch.setattr("app.viral_routes.get_media_storage", lambda conn: _CoverStorage())
    assert http.get("/api/viral/covers/kuaishou/x").status_code == 404
    assert http.get("/api/viral/covers/douyin/..%2Fsecret").status_code == 404
    assert http.get("/api/viral/covers/douyin/missing-cover").status_code == 404


def test_cached_list_reports_persisted_fetch_time(client):
    http, stub = client
    first = http.get("/api/viral/videos?platform=douyin", headers=_AUTH_HEADERS)
    second = http.get("/api/viral/videos?platform=douyin", headers=_AUTH_HEADERS)
    assert second.json()["fetchedAt"] == first.json()["fetchedAt"]
    assert second.json()["source"] == "database"
    assert second.json()["stale"] is False
    assert len(stub.douyin_calls) == 4


def test_concurrent_cold_lists_share_one_refresh(client, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    http, stub = client
    original = stub.douyin_search
    barrier = Barrier(2)

    def slow_search(**kwargs):
        time.sleep(0.05)
        return original(**kwargs)

    monkeypatch.setattr(stub, "douyin_search", slow_search)

    def request():
        barrier.wait(timeout=5)
        return http.get("/api/viral/videos?platform=douyin", headers=_AUTH_HEADERS)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: request(), range(2)))
    assert all(result.status_code == 200 for result in results)
    assert len(stub.douyin_calls) == 4


def test_parallel_category_results_are_persisted_in_config_order(client, monkeypatch):
    http, stub = client

    def shared_video(*, keyword, category="", **kwargs):
        # 首分类最慢，强制 future 完成顺序与配置顺序相反。
        if category == "建房预算":
            time.sleep(0.08)
        elif category == "户型设计":
            time.sleep(0.04)
        stub.douyin_calls.append({"keyword": keyword, "category": category, **kwargs})
        return [_video(platform="douyin", video_id="shared", category=category)]

    monkeypatch.setattr(stub, "douyin_search", shared_video)

    response = http.get("/api/viral/videos?platform=douyin", headers=_AUTH_HEADERS)

    assert response.status_code == 200
    assert response.json()["items"][0]["category"] == "庭院案例"


def test_partial_category_failure_cools_down_then_retries_only_failed_category(client, monkeypatch):
    from app.viral_routes import _open_worker_connection
    from app.viral_tikhub import ViralSourceError

    http, stub = client
    original = stub.douyin_search
    failed_once = False

    def sometimes_fails(**kwargs):
        nonlocal failed_once
        if kwargs["category"] == "户型设计" and not failed_once:
            failed_once = True
            stub.douyin_calls.append(dict(kwargs))
            raise ViralSourceError("暂时失败")
        return original(**kwargs)

    monkeypatch.setattr(stub, "douyin_search", sometimes_fails)

    first = http.get("/api/viral/videos?platform=douyin", headers=_AUTH_HEADERS)
    assert first.status_code == 200
    assert len(first.json()["items"]) == 6
    assert len(stub.douyin_calls) == 4

    second = http.get("/api/viral/videos?platform=douyin", headers=_AUTH_HEADERS)
    assert second.status_code == 200
    assert len(stub.douyin_calls) == 4

    stale = (_now() - timedelta(minutes=2)).isoformat()
    conn, close = _open_worker_connection()
    try:
        conn.execute(
            """
            UPDATE viral_fetch_state SET fetched_at = %s
            WHERE platform = %s AND sort = %s
            """,
            (stale, "douyin", "hot:retry:户型设计"),
        )
        conn.commit()
    finally:
        close()

    third = http.get("/api/viral/videos?platform=douyin", headers=_AUTH_HEADERS)
    assert third.status_code == 200
    assert len(third.json()["items"]) == 8
    assert len(stub.douyin_calls) == 5
    assert stub.douyin_calls[-1]["category"] == "户型设计"


@pytest.mark.parametrize("error_type", [ValueError, TypeError])
def test_malformed_category_preserves_other_results(client, monkeypatch, error_type):
    http, stub = client
    original = stub.douyin_search

    def malformed_category(**kwargs):
        if kwargs["category"] == "户型设计":
            stub.douyin_calls.append(dict(kwargs))
            raise error_type("untrusted numeric field")
        return original(**kwargs)

    monkeypatch.setattr(stub, "douyin_search", malformed_category)

    first = http.get("/api/viral/videos?platform=douyin", headers=_AUTH_HEADERS)
    second = http.get("/api/viral/videos?platform=douyin", headers=_AUTH_HEADERS)

    assert first.status_code == 200
    assert len(first.json()["items"]) == 6
    assert second.status_code == 200
    assert len(stub.douyin_calls) == 4
    assert "untrusted" not in first.text.lower()


def test_refresh_failure_returns_database_without_advancing_timestamp(client, monkeypatch):
    from app.viral_routes import _open_worker_connection
    from app.viral_tikhub import ViralSourceError

    http, stub = client
    first = http.get("/api/viral/videos?platform=douyin", headers=_AUTH_HEADERS)
    stale = (_now() - VIRAL_LIST_CACHE_TTL - timedelta(minutes=1)).isoformat()
    conn, close = _open_worker_connection()
    try:
        conn.execute("UPDATE viral_fetch_state SET fetched_at = %s", (stale,))
        conn.commit()
    finally:
        close()

    def failed(**kwargs):
        raise ViralSourceError("数据源暂不可用")

    monkeypatch.setattr(stub, "douyin_search", failed)
    result = http.get("/api/viral/videos?platform=douyin", headers=_AUTH_HEADERS)
    assert result.status_code == 200
    assert len(result.json()["items"]) == len(first.json()["items"])
    assert result.json()["fetchedAt"] == stale
    assert result.json()["stale"] is True


def test_cover_route_recovers_missing_copy_from_persisted_source(client, monkeypatch):
    from app.viral_routes import _open_worker_connection
    from app.viral_store import upsert_viral_videos

    storage = _CoverStorage()
    monkeypatch.setattr("app.viral_routes.get_media_storage", lambda conn: storage)
    monkeypatch.setattr("app.viral_routes.UrlFetcher", lambda *a, **kw: _OkFetcher())
    conn, close = _open_worker_connection()
    try:
        upsert_viral_videos(
            conn, [_video(platform="douyin", video_id="recover", category="建房预算")]
        )
    finally:
        close()
    result = client[0].get("/api/viral/covers/douyin/recover")
    assert result.status_code == 200
    assert result.content == b"fake-cover-bytes"


def test_cover_route_accepts_persisted_wechat_base64_id_with_slash(client, monkeypatch):
    from urllib.parse import quote

    from app.viral_media import viral_cover_key
    from app.viral_routes import _open_worker_connection
    from app.viral_store import upsert_viral_videos

    video_id = "finderobjv0abc/def+ghi="
    storage = _CoverStorage()
    key = viral_cover_key("wechat_channels", video_id)
    storage.put_object(key, b"jpeg-cover", content_type="image/jpeg")
    monkeypatch.setattr("app.viral_routes.get_media_storage", lambda conn: storage)
    conn, close = _open_worker_connection()
    try:
        upsert_viral_videos(
            conn, [_video(platform="wechat_channels", video_id=video_id, category="建房预算")]
        )
    finally:
        close()
    response = client[0].get("/api/viral/covers/wechat_channels/" + quote(video_id, safe=""))
    assert response.status_code == 200
    assert response.content == b"jpeg-cover"
    assert client[0].get("/api/viral/covers/wechat_channels/finderobj/../secret").status_code == 404


def test_cover_route_accepts_real_wechat_id_with_consecutive_slashes(client, monkeypatch):
    from urllib.parse import quote

    from app.viral_media import viral_cover_key
    from app.viral_routes import _open_worker_connection
    from app.viral_store import upsert_viral_videos

    video_id = "finderobjv0POr//CKfOesFBgIVVkCT4LG1YHnvIIJ9FmgsD4gFi9o="
    storage = _CoverStorage()
    key = viral_cover_key("wechat_channels", video_id)
    storage.put_object(key, b"jpeg-cover", content_type="image/jpeg")
    monkeypatch.setattr("app.viral_routes.get_media_storage", lambda conn: storage)
    conn, close = _open_worker_connection()
    try:
        upsert_viral_videos(
            conn, [_video(platform="wechat_channels", video_id=video_id, category="建房预算")]
        )
    finally:
        close()

    response = client[0].get("/api/viral/covers/wechat_channels/" + quote(video_id, safe=""))

    assert response.status_code == 200
    assert response.content == b"jpeg-cover"
    assert "//" not in key


def test_list_remains_available_from_database_without_source_configuration(client):
    http, stub = client
    first = http.get("/api/viral/videos?platform=douyin", headers=_AUTH_HEADERS)
    app.dependency_overrides[get_viral_source_client] = lambda: None
    cached = http.get("/api/viral/videos?platform=douyin", headers=_AUTH_HEADERS)
    assert cached.status_code == 200
    assert cached.json()["items"] == first.json()["items"]
    assert len(stub.douyin_calls) == 4


def test_media_statistics_are_persisted_and_returned_to_client(client, monkeypatch):
    from types import SimpleNamespace

    from app.viral_media import ViralMediaResult

    class StatsPipeline:
        detail = SimpleNamespace(like_count=42, comment_count=0, forward_count=8, fav_count=9)

        def __init__(self, **kwargs):
            pass

        def fetch(self, video, *, prefer=None):
            return ViralMediaResult(
                "video", "test://cached", "https://storage.test/v.mp4", 12, "video/mp4", False
            )

    monkeypatch.setattr("app.viral_routes.ViralMediaPipeline", StatsPipeline)
    monkeypatch.setattr("app.viral_routes.get_media_storage", lambda conn: object())
    http, stub = client
    initial = http.get("/api/viral/videos?platform=wechat_channels", headers=_AUTH_HEADERS)
    video_id = initial.json()["items"][0]["videoId"]
    media = http.post(
        "/api/viral/videos/media",
        headers=_AUTH_HEADERS,
        json={"platform": "wechat_channels", "videoId": video_id, "kind": "video"},
    )
    assert media.status_code == 200
    updated = media.json()["video"]
    assert (updated["likes"], updated["comments"], updated["shares"], updated["collects"]) == (
        42,
        0,
        8,
        9,
    )
    assert updated["likeDisplay"] is None
    calls = len(stub.wechat_calls)
    again = http.get("/api/viral/videos?platform=wechat_channels", headers=_AUTH_HEADERS)
    assert next(v for v in again.json()["items"] if v["videoId"] == video_id)["comments"] == 0
    assert len(stub.wechat_calls) == calls


def test_media_failure_still_persists_detail_statistics(client, monkeypatch):
    from types import SimpleNamespace

    from app.viral_media import ViralMediaError

    class FailingCdnPipeline:
        def __init__(self, **kwargs):
            self.detail = None

        def fetch(self, video, *, prefer=None):
            self.detail = SimpleNamespace(
                like_count=51, comment_count=2, forward_count=3, fav_count=4
            )
            raise ViralMediaError("该视频素材暂时无法获取，请稍后重试")

    monkeypatch.setattr("app.viral_routes.ViralMediaPipeline", FailingCdnPipeline)
    monkeypatch.setattr("app.viral_routes.get_media_storage", lambda conn: object())
    http, _stub = client
    initial = http.get("/api/viral/videos?platform=wechat_channels", headers=_AUTH_HEADERS)
    video_id = initial.json()["items"][0]["videoId"]

    media = http.post(
        "/api/viral/videos/media",
        headers=_AUTH_HEADERS,
        json={"platform": "wechat_channels", "videoId": video_id, "kind": "video"},
    )
    refreshed = http.get(
        "/api/viral/videos?platform=wechat_channels", headers=_AUTH_HEADERS
    ).json()["items"]
    stored = next(item for item in refreshed if item["videoId"] == video_id)

    assert media.status_code == 502
    assert (stored["likes"], stored["comments"], stored["shares"], stored["collects"]) == (
        51,
        2,
        3,
        4,
    )


def test_concurrent_legacy_douyin_media_repairs_source_once(client, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from dataclasses import replace
    from threading import Barrier
    from types import SimpleNamespace

    from app.viral_media import ViralMediaResult
    from app.viral_routes import _open_worker_connection
    from app.viral_store import upsert_viral_videos

    class CachedPipeline:
        detail = None

        def __init__(self, **kwargs):
            pass

        def fetch(self, video, *, prefer=None):
            return ViralMediaResult(
                "video", "test://cached", "https://storage.test/v.mp4", 12, "video/mp4", True
            )

    http, stub = client
    conn, close = _open_worker_connection()
    try:
        upsert_viral_videos(
            conn,
            [
                replace(
                    _video(platform="douyin", video_id="legacy", category="建房预算"),
                    native={"aweme_id": "legacy"},
                )
            ],
        )
    finally:
        close()

    def slow_repair(**kwargs):
        stub.douyin_calls.append(dict(kwargs))
        time.sleep(0.08)
        return [
            replace(
                _video(platform="douyin", video_id="legacy", category="建房预算"),
                native={"aweme_id": "legacy", "_playback_version": 1},
            )
        ]

    monkeypatch.setattr(stub, "douyin_search", slow_repair)
    monkeypatch.setattr("app.viral_routes.ViralMediaPipeline", CachedPipeline)
    monkeypatch.setattr(
        "app.viral_routes.get_media_storage",
        lambda conn: SimpleNamespace(head_object=lambda key: None),
    )
    barrier = Barrier(2)

    def request():
        barrier.wait(timeout=5)
        return http.post(
            "/api/viral/videos/media",
            headers=_AUTH_HEADERS,
            json={"platform": "douyin", "videoId": "legacy", "kind": "video"},
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: request(), range(2)))

    assert all(response.status_code == 200 for response in responses)
    assert len(stub.douyin_calls) == 1


def test_legacy_douyin_repair_requires_target_and_cools_down_failure(client, monkeypatch):
    from dataclasses import replace
    from types import SimpleNamespace

    from app.viral_routes import _open_worker_connection
    from app.viral_store import upsert_viral_videos

    http, stub = client
    conn, close = _open_worker_connection()
    try:
        upsert_viral_videos(
            conn,
            [
                replace(
                    _video(platform="douyin", video_id="legacy-missing", category="建房预算"),
                    native={"aweme_id": "legacy-missing"},
                )
            ],
        )
    finally:
        close()

    def misses_target(**kwargs):
        stub.douyin_calls.append(dict(kwargs))
        return [_video(platform="douyin", video_id="other", category="建房预算")]

    monkeypatch.setattr(stub, "douyin_search", misses_target)
    monkeypatch.setattr(
        "app.viral_routes.get_media_storage",
        lambda conn: SimpleNamespace(head_object=lambda key: None),
    )
    monkeypatch.setattr(
        "app.viral_routes.ViralMediaPipeline",
        lambda **kwargs: pytest.fail("旧播放源不得进入媒体管线"),
    )

    responses = [
        http.post(
            "/api/viral/videos/media",
            headers=_AUTH_HEADERS,
            json={"platform": "douyin", "videoId": "legacy-missing", "kind": "video"},
        )
        for _ in range(2)
    ]

    assert [response.status_code for response in responses] == [502, 502]
    assert len(stub.douyin_calls) == 1
    assert all("tikhub" not in response.text.lower() for response in responses)


def test_legacy_douyin_audio_does_not_require_source_repair(client, monkeypatch):
    from dataclasses import replace

    from app.viral_media import ViralMediaResult
    from app.viral_routes import _open_worker_connection
    from app.viral_store import upsert_viral_videos

    class AudioPipeline:
        detail = None

        def __init__(self, **kwargs):
            pass

        def fetch(self, video, *, prefer=None):
            return ViralMediaResult(
                "audio", "test://cached", "https://storage.test/a.mp3", 12, "audio/mpeg", True
            )

    http, _stub = client
    conn, close = _open_worker_connection()
    try:
        upsert_viral_videos(
            conn,
            [
                replace(
                    _video(platform="douyin", video_id="legacy-audio", category="建房预算"),
                    native={"aweme_id": "legacy-audio"},
                    audio_url="https://cdn.test/legacy-audio.mp3",
                )
            ],
        )
    finally:
        close()

    app.dependency_overrides[get_viral_source_client] = lambda: None
    monkeypatch.setattr("app.viral_routes.ViralMediaPipeline", AudioPipeline)
    monkeypatch.setattr("app.viral_routes.get_media_storage", lambda conn: object())

    response = http.post(
        "/api/viral/videos/media",
        headers=_AUTH_HEADERS,
        json={"platform": "douyin", "videoId": "legacy-audio", "kind": "audio"},
    )

    assert response.status_code == 200
    assert response.json()["kind"] == "audio"


def test_local_media_url_decodes_storage_key_exactly_once(client, monkeypatch):
    from datetime import UTC, datetime
    from urllib.parse import parse_qs, urlsplit

    from app.storage import local_download_signature
    from app.viral_routes import (
        _VIRAL_SIGNATURE_ASSET,
        _VIRAL_SIGNATURE_EPOCH,
        _browser_playable_url,
    )

    secret = "test-signing-secret"
    monkeypatch.setattr("app.viral_routes.settings_encryption_key", lambda: secret)
    key = "viral/wechat_channels/finderobjabc/def+ghi=.mp4"
    from urllib.parse import quote

    url = _browser_playable_url("local://local-private/" + quote(key), "employee_1")
    query = parse_qs(urlsplit(url).query)
    assert query["key"] == [key]
    assert query["sig"] == [
        local_download_signature(
            key,
            query["expires"][0],
            user_id="employee_1",
            asset_id=_VIRAL_SIGNATURE_ASSET,
            session_epoch=_VIRAL_SIGNATURE_EPOCH,
            secret=secret,
        )
    ]
    assert int(query["expires"][0]) > datetime.now(UTC).timestamp()
