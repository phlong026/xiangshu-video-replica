from __future__ import annotations

import pytest

from app.account_link import AccountLinkError, looks_like_account_link, parse_account_target

SEC_UID = "MS4wLjABAAAAxakJHNxB0cmKZ6c6FthE2vA7he6kHmgSBp9YjggHR8"


def test_parses_full_profile_link() -> None:
    target = parse_account_target(f"https://www.douyin.com/user/{SEC_UID}?from_tab_name=main")
    assert target.platform == "douyin"
    assert target.sec_uid == SEC_UID
    assert target.unique_id is None


def test_parses_link_embedded_in_share_text() -> None:
    text = f"8.88 复制打开抖音，看看【某某的主页】 https://www.douyin.com/user/{SEC_UID} 复制此链接"
    target = parse_account_target(text)
    assert target.sec_uid == SEC_UID


def test_parses_link_without_scheme_and_trailing_punctuation() -> None:
    target = parse_account_target(f"www.douyin.com/user/{SEC_UID}，")
    assert target.sec_uid == SEC_UID


def test_parses_unique_id_input() -> None:
    target = parse_account_target("@zsj_001")
    assert target.sec_uid is None
    assert target.unique_id == "zsj_001"
    assert target.identity == "unique:zsj_001"


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ("", "ACCOUNT_LINK_EMPTY"),
        ("   ", "ACCOUNT_LINK_EMPTY"),
        (
            "https://www.douyin.com/video/7600000000000000001",
            "ACCOUNT_LINK_IS_VIDEO",
        ),
        ("https://v.douyin.com/iAbCdEf/", "ACCOUNT_LINK_SHORT_UNSUPPORTED"),
        ("https://www.xiaohongshu.com/user/profile/abc", "ACCOUNT_LINK_UNSUPPORTED_PLATFORM"),
        ("https://www.douyin.com/user/abc", "ACCOUNT_LINK_NOT_PROFILE"),
        ("随便一段没有链接的文字", "ACCOUNT_LINK_UNSUPPORTED"),
    ],
)
def test_rejects_unusable_input_with_specific_codes(raw: str, code: str) -> None:
    with pytest.raises(AccountLinkError) as excinfo:
        parse_account_target(raw)
    assert excinfo.value.code == code
    assert excinfo.value.status_code == 422


def test_looks_like_account_link_only_matches_profile_links() -> None:
    assert looks_like_account_link(f"https://www.douyin.com/user/{SEC_UID}") is True
    assert looks_like_account_link("https://www.douyin.com/video/7600000000000000001") is False
    assert looks_like_account_link("zsj_001") is False
