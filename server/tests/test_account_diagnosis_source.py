from __future__ import annotations

import json
from typing import Any

import pytest

from app.viral_tikhub import (
    DOUYIN_USER_POSTS_PATH,
    DOUYIN_USER_PROFILE_BY_UNIQUE_ID_PATH,
    DOUYIN_USER_PROFILE_PATH,
    ViralSourceClient,
    ViralSourceError,
    account_diagnosis_disabled,
)

SEC_UID = "MS4wLjABAAAAxakJHNxB0cmKZ6c6FthE2vA7he6kHmgSBp9YjggHR8"


class _GetTransport:
    """记录 GET 请求并把预置 data 段包装成上游信封."""

    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data
        self.requests: list[dict[str, Any]] = []

    def request(self, method: str, url: str, *, headers, body=None) -> bytes:
        self.requests.append({"method": method, "url": url, "body": body, "headers": dict(headers)})
        return json.dumps({"code": 200, "data": self.data}, ensure_ascii=False).encode("utf-8")


def _profile_payload() -> dict[str, Any]:
    return {
        "user": {
            "sec_uid": SEC_UID,
            "unique_id": "zsj001",
            "nickname": "众墅之家",
            "avatar_larger": {"url_list": ["https://cdn.example/avatar.jpg"]},
            "signature": "分享装修干货",
            "province": "浙江",
            "city": "杭州",
            "ip_location": "浙江",
            "follower_count": 12345,
            "following_count": 67,
            "total_favorited": 89012,
            "aweme_count": 345,
        }
    }


def test_user_profile_uses_get_without_body() -> None:
    transport = _GetTransport(_profile_payload())
    client = ViralSourceClient(api_key="test-key", transport=transport)
    profile = client.douyin_user_profile(sec_user_id=SEC_UID)

    assert profile.sec_user_id == SEC_UID
    assert profile.unique_id == "zsj001"
    assert profile.nickname == "众墅之家"
    assert profile.avatar_url == "https://cdn.example/avatar.jpg"
    assert profile.province == "浙江"
    assert profile.ip_location == "浙江"
    assert profile.follower_count == 12345
    assert profile.total_favorited == 89012
    assert profile.aweme_count == 345

    request = transport.requests[0]
    assert request["method"] == "GET"
    assert request["body"] is None
    assert DOUYIN_USER_PROFILE_PATH in request["url"]
    assert f"sec_user_id={SEC_UID}" in request["url"]


def test_user_profile_by_unique_id_hits_the_v2_path() -> None:
    transport = _GetTransport({"user_info": {"nickname": "另一个号"}})
    client = ViralSourceClient(api_key="test-key", transport=transport)
    profile = client.douyin_user_profile_by_unique_id(unique_id="@abc_123")

    assert profile.nickname == "另一个号"
    # 上游没回 sec_uid 时保留空串，由上层决定是否二次解析。
    assert profile.sec_user_id == ""
    assert DOUYIN_USER_PROFILE_BY_UNIQUE_ID_PATH in transport.requests[0]["url"]
    assert "unique_id=abc_123" in transport.requests[0]["url"]


def test_user_posts_parse_statistics_and_missing_play_count() -> None:
    transport = _GetTransport(
        {
            "aweme_list": [
                {
                    "aweme_id": "7600000000000000001",
                    "desc": "三层小别墅开工",
                    "create_time": 1_700_000_000,
                    "statistics": {
                        "digg_count": 1200,
                        "comment_count": 34,
                        "share_count": 12,
                        "collect_count": 56,
                    },
                    "video": {"duration": 18_000},
                },
                {"aweme_id": "", "desc": "缺少 id 的脏数据"},
            ],
            "max_cursor": 1_699_000_000,
            "has_more": 1,
        }
    )
    client = ViralSourceClient(api_key="test-key", transport=transport)
    page = client.douyin_user_posts(sec_user_id=SEC_UID, count=99, sort_type=7)

    assert len(page.posts) == 1
    post = page.posts[0]
    assert post.aweme_id == "7600000000000000001"
    assert post.title == "三层小别墅开工"
    assert post.created_at == 1_700_000_000
    assert post.digg_count == 1200
    assert post.comment_count == 34
    assert post.share_count == 12
    assert post.collect_count == 56
    assert post.play_count is None
    assert post.duration_ms == 18_000
    assert page.max_cursor == 1_699_000_000
    assert page.has_more is True

    url = transport.requests[0]["url"]
    assert DOUYIN_USER_POSTS_PATH in url
    assert "count=20" in url  # 上游上限 20，超出必须在本地收口
    assert "sort_type=1" in url


def test_user_posts_accepts_nested_data_block() -> None:
    transport = _GetTransport(
        {"data": {"aweme_list": [{"aweme_id": "1", "desc": "嵌套", "create_time": 1}]}}
    )
    client = ViralSourceClient(api_key="test-key", transport=transport)
    page = client.douyin_user_posts(sec_user_id=SEC_UID)
    assert [post.aweme_id for post in page.posts] == ["1"]
    assert page.posts[0].play_count is None


def test_empty_sec_uid_is_rejected_before_any_request() -> None:
    transport = _GetTransport({})
    client = ViralSourceClient(api_key="test-key", transport=transport)
    with pytest.raises(ViralSourceError):
        client.douyin_user_profile(sec_user_id="  ")
    with pytest.raises(ViralSourceError):
        client.douyin_user_posts(sec_user_id="")
    assert transport.requests == []


def test_account_diagnosis_switch_is_independent_of_viral_retirement(monkeypatch) -> None:
    monkeypatch.delenv("ACCOUNT_DIAGNOSIS_DISABLED", raising=False)
    assert account_diagnosis_disabled() is False
    monkeypatch.setenv("ACCOUNT_DIAGNOSIS_DISABLED", "1")
    assert account_diagnosis_disabled() is True
