"""123网盘客户端单测：离线 fake opener + fake token store，不触网不触 DB。

覆盖方案 §8 单测行：签名拼接（解码路径/既有 query/旧 auth_key 剥离/中文名）、
远端名清洗、秒传/分片/complete 有界轮询、401 受限重取、429/5066 错误映射、
直链缓存与 config_version 失效、探针防盗链负例。共享 token 的 DB 语义
（client_id 绑定/原子清除）由 PG 集成测试与设置测试覆盖。
"""

import io
import json
import time
from collections.abc import Callable
from contextlib import contextmanager
from urllib.error import HTTPError

import pytest

from app.pan123_client import (
    PAN123_BASE_URL,
    Pan123Client,
    Pan123FileMissing,
    Pan123Unavailable,
    sanitize_remote_name,
    signed_direct_link,
)

CONFIG = {
    "client_id": "cid-1",
    "client_secret": "sec-1",
    "uid": "1000",
    "sign_secret": "signkey",
    "folder_id": "42",
}


class _FakeResponse:
    def __init__(self, body: bytes, status: int = 200) -> None:
        self._body = body
        self.status = status
        self.headers = {"content-type": "application/json"}

    def read(self, limit: int | None = None) -> bytes:
        return self._body if limit is None else self._body[:limit]


class FakeOpener:
    """按 (method, url 前缀) 分发的假 opener；handler 返回 outcome 三元组。

    outcome 形态：``("json", payload, status)`` / ``("raw", body_bytes, status)``；
    status >= 400 时抛 HTTPError（模拟防盗链拒绝等非 200 行为）。
    """

    def __init__(self) -> None:
        self.routes: list[
            tuple[Callable[[str, str], bool], Callable[[], tuple[str, object, int]]]
        ] = []
        self.calls: list[tuple[str, str]] = []

    def on(
        self, matcher: Callable[[str, str], bool], handler: Callable[[], tuple[str, object, int]]
    ) -> None:
        self.routes.append((matcher, handler))

    def count(self, prefix: str) -> int:
        return sum(1 for _method, url in self.calls if prefix in url)

    def __call__(self, request: object, timeout: float | None = None) -> object:
        return self._enter(request)

    @contextmanager
    def _enter(self, request: object):
        url = str(getattr(request, "full_url", request))
        method = request.get_method() if hasattr(request, "get_method") else "GET"
        self.calls.append((method, url))
        for matcher, handler in self.routes:
            if matcher(method, url):
                kind, payload, status = handler()
                break
        else:
            kind, payload, status = "json", {"code": 500, "message": "unrouted"}, 200
        if kind == "raw":
            body = bytes(payload)  # type: ignore[arg-type]
        else:
            body = json.dumps(payload).encode()
        if status >= 400:
            raise HTTPError(url, status, "error", None, io.BytesIO(body))
        yield _FakeResponse(body, status)


class FakeTokenStore:
    """SharedTokenStore 的离线替身：只保留 client 依赖的接口面。"""

    def __init__(self) -> None:
        self.shared: object | None = None
        self.memory: object | None = None
        self.mint_calls = 0

    def cached_for(self, client_id: str, *, now: Callable[[], int] = lambda: int(time.time())):
        state = self.memory
        if state is None or state.client_id != client_id or state.expires_at <= now() + 60:
            return None
        return state

    def cache(self, state: object) -> None:
        self.memory = state

    def invalidate(self) -> None:
        self.memory = None

    def read_shared(self, client_id: str):
        state = self.shared
        if state is None or state.client_id != client_id:
            return None
        return state

    def mint_single_flight(self, client_id: str, mint: Callable[[], object]) -> object:
        self.mint_calls += 1
        minted = mint()
        self.shared = minted
        return minted

    def clear_shared(self) -> None:
        self.shared = None
        self.invalidate()


def _make_client(
    opener: FakeOpener, store: FakeTokenStore | None = None, config_version: str = "v1"
) -> tuple[Pan123Client, FakeTokenStore]:
    token_store = store or FakeTokenStore()
    client = Pan123Client(
        CONFIG, config_version=config_version, shared_token=token_store, opener=opener
    )
    return client, token_store


def _route_access_token(opener: FakeOpener, token: str = "tok-1") -> None:
    expires = int(time.time()) + 86_400

    def matcher(method: str, url: str) -> bool:
        return url == f"{PAN123_BASE_URL}/api/v1/access_token"

    def handler() -> tuple[str, object, int]:
        return ("json", {"code": 0, "data": {"accessToken": token, "expiredAt": expires}}, 200)

    opener.on(matcher, handler)


# -- 纯函数 -------------------------------------------------------------------


def test_sanitize_remote_name_replaces_forbidden_and_clamps() -> None:
    # Path().name 先剥路径（/ 与 \ 不入结果），随后禁字符替换为下划线。
    assert sanitize_remote_name("a/b\\c:d*e?f|g><.mp4") == "c_d_e_f_g__.mp4"
    assert sanitize_remote_name("/path/to/资料 视频.mp4") == "资料 视频.mp4"
    assert sanitize_remote_name("   ") == "material"
    assert len(sanitize_remote_name("长" * 300)) == 255


def test_signed_direct_link_decodes_path_and_preserves_query() -> None:
    url = "https://cdn.example.invalid/direct/1/2/%E4%B8%AD%E6%96%87%20a.mp4?rate=2"
    signed, expire = signed_direct_link(
        url, "1000", "signkey", ttl_seconds=60, rand="rand0", now=lambda: 1_000
    )
    assert expire == 1_060
    # 既有 query 保留；auth_key 以查询参数追加。
    assert "rate=2" in signed
    assert "auth_key=1060-rand0-1000-" in signed
    # 摘要输入是 URL 解码后的 pathname（官方示例口径）——固定密钥下可复算。
    import hashlib

    digest = hashlib.md5(  # noqa: S324 - 与被测协议一致
        b"/direct/1/2/\xe4\xb8\xad\xe6\x96\x87 a.mp4-1060-rand0-1000-signkey",
        usedforsecurity=False,
    ).hexdigest()
    assert signed.endswith(f"auth_key=1060-rand0-1000-{digest}")


def test_signed_direct_link_replaces_stale_auth_key() -> None:
    url = "https://cdn.example.invalid/a.mp4?auth_key=1-old-uid-old&keep=1"
    signed, _expire = signed_direct_link(
        url, "1000", "signkey", ttl_seconds=60, rand="r", now=lambda: 1_000
    )
    assert "old-uid-old" not in signed
    assert "keep=1" in signed


# -- 上传 ---------------------------------------------------------------------


def test_upload_reuse_short_circuits_slices() -> None:
    opener = FakeOpener()
    _route_access_token(opener)
    opener.on(
        lambda m, u: u == f"{PAN123_BASE_URL}/upload/v2/file/domain",
        lambda: ("json", {"code": 0, "data": ["https://upload.example.invalid"]}, 200),
    )
    opener.on(
        lambda m, u: u == f"{PAN123_BASE_URL}/upload/v2/file/create",
        lambda: (
            "json",
            {"code": 0, "data": {"reuse": True, "fileId": 777}},
            200,
        ),
    )
    client, store = _make_client(opener)
    import tempfile
    from pathlib import Path

    path = Path(tempfile.gettempdir()) / "pan123-reuse-test.txt"
    path.write_bytes(b"hello")
    try:
        uploaded = client.upload_file("hello.txt", path, content_type="text/plain")
    finally:
        path.unlink(missing_ok=True)
    assert uploaded.file_id == 777
    assert opener.count(f"{PAN123_BASE_URL}/upload/v2/file/slice") == 0
    assert store.mint_calls == 1


def test_upload_slices_and_polls_complete(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    opener = FakeOpener()
    _route_access_token(opener)
    opener.on(
        lambda m, u: u == f"{PAN123_BASE_URL}/upload/v2/file/domain",
        lambda: ("json", {"code": 0, "data": ["https://upload.example.invalid"]}, 200),
    )
    opener.on(
        lambda m, u: u == f"{PAN123_BASE_URL}/upload/v2/file/create",
        lambda: (
            "json",
            {"code": 0, "data": {"reuse": False, "preuploadID": "pre-1", "sliceSize": 4}},
            200,
        ),
    )
    slice_calls: list[int] = []

    def slice_handler() -> tuple[str, object, int]:
        slice_calls.append(len(slice_calls) + 1)
        return ("json", {"code": 0, "data": None}, 200)

    opener.on(lambda m, u: "/upload/v2/file/slice" in u, slice_handler)
    complete_state = {"round": 0}

    def complete_handler() -> tuple[str, object, int]:
        complete_state["round"] += 1
        completed = complete_state["round"] >= 2
        return (
            "json",
            {"code": 0, "data": {"completed": completed, "fileID": 888}},
            200,
        )

    opener.on(
        lambda m, u: u == f"{PAN123_BASE_URL}/upload/v2/file/upload_complete", complete_handler
    )
    client, _store = _make_client(opener)
    import tempfile
    from pathlib import Path

    path = Path(tempfile.gettempdir()) / "pan123-slice-test.bin"
    path.write_bytes(b"aabbccdd")  # 8 字节 / sliceSize 4 → 2 片
    try:
        uploaded = client.upload_file("x.bin", path, content_type="application/octet-stream")
    finally:
        path.unlink(missing_ok=True)
    assert uploaded.file_id == 888
    assert len(slice_calls) == 2
    assert complete_state["round"] == 2


# -- 鉴权与错误映射 -------------------------------------------------------------


def test_401_refreshes_token_once_and_replays() -> None:
    opener = FakeOpener()
    _route_access_token(opener, token="tok-1")
    direct_state = {"round": 0}

    def direct_handler() -> tuple[str, object, int]:
        direct_state["round"] += 1
        if direct_state["round"] == 1:
            return ("json", {"code": 401, "message": "token 无效"}, 200)
        return (
            "json",
            {"code": 0, "data": {"url": "https://cdn.example.invalid/a.mp4"}},
            200,
        )

    opener.on(
        lambda m, u: u == f"{PAN123_BASE_URL}/api/v1/direct-link/url?fileID=1", direct_handler
    )
    client, store = _make_client(opener)
    url, _expire = client.resolve_direct_link(1)
    assert "auth_key=" in url
    # 第一次 401 后强制重取：mint 两次（初次 + force refresh），业务接口重放一次。
    assert store.mint_calls == 2
    assert direct_state["round"] == 2


def test_error_mapping_rate_limit_and_missing() -> None:
    opener = FakeOpener()
    _route_access_token(opener)
    opener.on(
        lambda m, u: u.endswith("api/v1/direct-link/url?fileID=2"),
        lambda: ("json", {"code": 429, "message": "请求太频繁"}, 200),
    )
    opener.on(
        lambda m, u: u.endswith("api/v1/direct-link/url?fileID=3"),
        lambda: ("json", {"code": 5066, "message": "文件不存在"}, 200),
    )
    client, _store = _make_client(opener)
    with pytest.raises(Pan123Unavailable, match="频繁"):
        client.resolve_direct_link(2)
    with pytest.raises(Pan123FileMissing):
        client.resolve_direct_link(3)


def test_direct_link_cache_invalidated_by_config_version() -> None:
    opener = FakeOpener()
    _route_access_token(opener)
    opener.on(
        lambda m, u: "/api/v1/direct-link/url" in u,
        lambda: (
            "json",
            {"code": 0, "data": {"url": "https://cdn.example.invalid/a.mp4"}},
            200,
        ),
    )
    client, _store = _make_client(opener, config_version="v1")
    client.resolve_direct_link(9)
    client.resolve_direct_link(9)
    assert opener.count("/api/v1/direct-link/url") == 1
    # config_version 变化（等价换客户端实例）后重新取链。
    client2, _store2 = _make_client(opener, config_version="v2")
    client2.resolve_direct_link(9)
    assert opener.count("/api/v1/direct-link/url") == 2


# -- 探针（防盗链负例）-----------------------------------------------------------


def _route_probe_chain(opener: FakeOpener, *, enforce_hotlink: bool) -> None:
    """probe 全链路：上传（秒传短路）→ 直链 → 链接读取（可配置防盗链是否生效）。"""
    _route_access_token(opener)
    opener.on(
        lambda m, u: u == f"{PAN123_BASE_URL}/upload/v2/file/domain",
        lambda: ("json", {"code": 0, "data": ["https://upload.example.invalid"]}, 200),
    )
    opener.on(
        lambda m, u: u == f"{PAN123_BASE_URL}/upload/v2/file/create",
        lambda: ("json", {"code": 0, "data": {"reuse": True, "fileId": 42}}, 200),
    )
    opener.on(
        lambda m, u: "/api/v1/direct-link/url" in u,
        lambda: (
            "json",
            {"code": 0, "data": {"url": "https://cdn.example.invalid/d/42/probe.txt"}},
            200,
        ),
    )

    # probe 的直链读取顺序固定：正例 → 缺失 → 篡改 → 过期（§3.2 probe 契约）。
    reads = {"count": 0}

    def read_link() -> tuple[str, object, int]:
        reads["count"] += 1
        if not enforce_hotlink:
            # 防盗链未开启：任何请求（含负例）都放行 200。
            return ("raw", b"0123456789abcdef", 200)
        # 正例可读（206 部分内容），三个负例全部 403 拒绝。
        return ("raw", b"0123456789abcdef", 206 if reads["count"] == 1 else 403)

    opener.on(lambda m, u: u.startswith("https://cdn.example.invalid/"), read_link)
    opener.on(
        lambda m, u: u == f"{PAN123_BASE_URL}/api/v1/file/delete",
        lambda: ("json", {"code": 0, "data": None}, 200),
    )


def test_probe_passes_when_hotlink_enforced() -> None:
    opener = FakeOpener()
    _route_probe_chain(opener, enforce_hotlink=True)
    client, _store = _make_client(opener)
    client.probe()  # 正例可读 + 三负例全拒 → 通过


def test_probe_fails_when_hotlink_not_enforced() -> None:
    opener = FakeOpener()
    _route_probe_chain(opener, enforce_hotlink=False)
    client, _store = _make_client(opener)
    with pytest.raises(Pan123Unavailable, match="防盗链未生效"):
        client.probe()
