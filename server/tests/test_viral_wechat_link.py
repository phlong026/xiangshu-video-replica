"""视频号登录态解析链（viral_wechat_link）的桩测：三步链、映射与错误分类.

上游协议字段以真实响应为准（联调可能微调 _snapshot_from_feed 的候选键），
这里锁定的是：链路编排顺序、明文直链择优、native 溯源键回填、凭据不外泄
与中性错误码——这些是媒体管线与统计回落的契约面。
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.viral_link import ViralLinkError, normalize_supported_link, supported_link_platform
from app.viral_wechat_link import (
    PROVIDER_NAME,
    UrllibWechatLinkTransport,
    WechatLinkClient,
    WechatLinkCredentials,
    snapshot_to_detail,
    wechat_link_client_from_settings,
    wechat_link_client_from_settings_or_none,
)

_YUANBAO_PREFIX = "https://yuanbao.tencent.com"
_FEED_INFO_URL = "https://channels.weixin.qq.com/finder-preview/api/feed/get_feed_info"


class FakeWechatTransport:
    """按 URL 前缀返回桩响应；记录调用以断言请求头与链路顺序."""

    def __init__(self, responses: dict[str, Any]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict[str, str]]] = []

    def request(self, url: str, *, headers: dict[str, str], payload: bytes | None = None) -> bytes:
        self.calls.append((url, dict(headers)))
        for prefix, value in self.responses.items():
            if url.startswith(prefix):
                if isinstance(value, Exception):
                    raise value
                return json.dumps(value).encode()
        raise AssertionError(f"unexpected request url: {url}")


def _client(transport: FakeWechatTransport, *, agent_id: str = "agent-1") -> WechatLinkClient:
    return WechatLinkClient(
        credentials=WechatLinkCredentials(cookie="secret-cookie-value", agent_id=agent_id),
        transport=transport,  # type: ignore[arg-type]
    )


def _happy_transport() -> FakeWechatTransport:
    return FakeWechatTransport(
        {
            f"{_YUANBAO_PREFIX}/api/getuserinfo": {"userId": "u-123"},
            f"{_YUANBAO_PREFIX}/api/weixin/get_parse_result": {
                "code": 0,
                "data": {
                    "playable_url": (
                        "https://channels.weixin.qq.com/finder-preview/pages/feed"
                        "?token=tok-1&eid=eid-from-query"
                    ),
                    "wx_export_id": "export-1",
                },
            },
            _FEED_INFO_URL: {
                "data": {
                    "authorInfo": {
                        "nickname": "庭院设计老周",
                        "headImgUrl": "https://img.example/avatar.jpg",
                    },
                    "feedInfo": {
                        "description": "庭院设计三要点 #庭院案例 #乡墅",
                        "duration": 18_000,
                        "likeCount": 123,
                        "commentCount": 4,
                        "forwardCount": 5,
                        "favoriteCount": 6,
                        "createTime": 1_788_600_000,
                        "coverUrl": "https://img.example/cover.jpg",
                        "h264VideoInfo": {"videoUrl": "https://finder.example/h264.mp4"},
                        "h265VideoInfo": {"videoUrl": "https://finder.example/h265.mp4"},
                    },
                }
            },
        }
    )


def test_wechat_share_link_normalized_to_wechat_channels() -> None:
    url = "https://weixin.qq.com/sph/AbCdEf123"
    assert normalize_supported_link(f"复制这条信息 {url}") == url
    assert supported_link_platform(url) == "wechat_channels"
    # 抖音/小红书分类不受影响。
    assert supported_link_platform("https://www.douyin.com/video/123") == "douyin"


def test_fetch_snapshot_maps_feed_fields_and_prefers_h264() -> None:
    transport = _happy_transport()
    client = _client(transport)
    snapshot = client.fetch_snapshot("https://weixin.qq.com/sph/AbCdEf123")
    assert snapshot.export_id == "export-1"
    assert snapshot.share_url == "https://weixin.qq.com/sph/AbCdEf123"
    # 直链择优：h264 优先，不吃 h265。
    assert snapshot.video_url == "https://finder.example/h264.mp4"
    assert snapshot.duration_ms == 18_000
    assert (snapshot.likes, snapshot.comments, snapshot.shares, snapshot.collects) == (
        123,
        4,
        5,
        6,
    )
    assert snapshot.author == "庭院设计老周"
    assert "庭院案例" in snapshot.title
    # 链路顺序：登录 → 解析 → 详情。
    urls = [url for url, _ in transport.calls]
    assert urls[0].endswith("/api/getuserinfo")
    assert "/api/weixin/get_parse_result" in urls[1]
    assert urls[2] == _FEED_INFO_URL


def test_parse_request_carries_credentials_but_feed_request_does_not() -> None:
    """登录态 Cookie 只发解析接口；预览详情是公开页面，绝不携带 Cookie."""
    transport = _happy_transport()
    client = _client(transport, agent_id="agent-9")
    client.fetch_snapshot("https://weixin.qq.com/sph/AbCdEf123")
    parse_headers = transport.calls[1][1]
    feed_headers = transport.calls[2][1]
    assert parse_headers["Cookie"] == "secret-cookie-value"
    assert parse_headers["X-AgentID"] == "agent-9"
    assert parse_headers["T-UserID"] == "u-123"
    assert "Cookie" not in feed_headers
    assert feed_headers["Origin"] == "https://channels.weixin.qq.com"


def test_resolve_returns_native_trace_keys() -> None:
    client = _client(_happy_transport())
    resolved = client.resolve("https://weixin.qq.com/sph/AbCdEf123", purpose="replica")
    assert resolved.platform == "wechat_channels"
    assert resolved.video_id == "export-1"
    assert resolved.video_url == "https://finder.example/h264.mp4"
    assert resolved.metadata.likes == 123
    assert resolved.native == {
        "share_url": "https://weixin.qq.com/sph/AbCdEf123",
        "export_id": "export-1",
    }


def test_snapshot_to_detail_carries_counts_without_media() -> None:
    client = _client(_happy_transport())
    snapshot = client.fetch_snapshot("https://weixin.qq.com/sph/AbCdEf123")
    detail = snapshot_to_detail(snapshot)
    assert detail.like_count == 123
    assert detail.comment_count == 4
    assert detail.forward_count == 5
    assert detail.fav_count == 6
    assert detail.full_url is None
    assert detail.decode_key is None
    assert detail.object_id == "export-1"


def test_missing_user_identity_raises_auth_expired() -> None:
    transport = FakeWechatTransport({_YUANBAO_PREFIX: {"nickname": "n"}})
    client = _client(transport)
    with pytest.raises(ViralLinkError) as error:
        client.fetch_snapshot("https://weixin.qq.com/sph/AbCdEf123")
    assert error.value.code == "VIRAL_WECHAT_AUTH_EXPIRED"
    assert error.value.status_code == 502


def test_parse_failure_raises_parse_failed_with_neutral_message() -> None:
    transport = FakeWechatTransport(
        {
            f"{_YUANBAO_PREFIX}/api/getuserinfo": {"userId": "u-123"},
            f"{_YUANBAO_PREFIX}/api/weixin/get_parse_result": {"code": -1, "msg": "bad"},
        }
    )
    client = _client(transport)
    with pytest.raises(ViralLinkError) as error:
        client.fetch_snapshot("https://weixin.qq.com/sph/AbCdEf123")
    assert error.value.code == "VIRAL_WECHAT_PARSE_FAILED"
    assert error.value.status_code == 422
    # 中性文案：不出现数据源供应商名称。
    assert "元宝" not in error.value.message
    assert "腾讯" not in error.value.message


def test_missing_preview_params_raises_parse_failed() -> None:
    transport = FakeWechatTransport(
        {
            f"{_YUANBAO_PREFIX}/api/getuserinfo": {"userId": "u-123"},
            f"{_YUANBAO_PREFIX}/api/weixin/get_parse_result": {
                "code": 0,
                "data": {"playable_url": "https://channels.weixin.qq.com/x"},
            },
        }
    )
    client = _client(transport)
    with pytest.raises(ViralLinkError) as error:
        client.fetch_snapshot("https://weixin.qq.com/sph/AbCdEf123")
    assert error.value.code == "VIRAL_WECHAT_PARSE_FAILED"


def test_transport_rejects_non_allowlisted_host() -> None:
    transport = UrllibWechatLinkTransport()
    with pytest.raises(ViralLinkError) as error:
        transport.request("https://evil.example/api", headers={}, payload=None)
    assert error.value.code == "VIRAL_WECHAT_SERVICE_FAILED"


def test_http_403_maps_to_auth_expired() -> None:
    class HttpErrorStub(Exception):
        pass

    from urllib.error import HTTPError

    transport = FakeWechatTransport(
        {_YUANBAO_PREFIX: HTTPError("url", 403, "forbidden", None, None)}
    )
    client = _client(transport)
    with pytest.raises(ViralLinkError) as error:
        client.fetch_snapshot("https://weixin.qq.com/sph/AbCdEf123")
    assert error.value.code == "VIRAL_WECHAT_AUTH_EXPIRED"


def test_credentials_never_leak_into_repr_or_messages() -> None:
    credentials = WechatLinkCredentials(cookie="secret-cookie-value", agent_id="agent-1")
    assert "secret-cookie-value" not in str(credentials)
    assert "secret-cookie-value" not in repr(credentials)


def test_factory_requires_configured_channel(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeSettingsRepository:
        def __init__(self, _conn: Any) -> None:
            pass

        def load_provider_config(self, provider: str) -> dict[str, str]:
            assert provider == PROVIDER_NAME
            return {}

    monkeypatch.setattr("app.viral_wechat_link.SettingsRepository", FakeSettingsRepository)
    with pytest.raises(ViralLinkError) as error:
        wechat_link_client_from_settings(conn=None)  # type: ignore[arg-type]
    assert error.value.code == "VIRAL_WECHAT_NOT_CONFIGURED"
    assert wechat_link_client_from_settings_or_none(None) is None  # type: ignore[arg-type]


def test_factory_builds_client_from_provider_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeSettingsRepository:
        def __init__(self, _conn: Any) -> None:
            pass

        def load_provider_config(self, provider: str) -> dict[str, str]:
            return {"cookie": "c-1", "agent_id": "a-1"}

    monkeypatch.setattr("app.viral_wechat_link.SettingsRepository", FakeSettingsRepository)
    client = wechat_link_client_from_settings(conn=None)  # type: ignore[arg-type]
    assert client.credentials.agent_id == "a-1"
