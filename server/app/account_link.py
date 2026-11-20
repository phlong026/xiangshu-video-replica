"""账号诊断的输入解析：把用户粘贴的内容还原成可查询的账号标识.

为什么不复用视频链接解析：

- 视频解析器（``viral_link``）的目标是"拿到一条视频"，它把 ``/user/`` 链接判成
  "没有可用视频"；账号诊断需要的恰恰是 ``/user/{sec_uid}`` 里的 sec_uid。
- 账号链接是纯文本形态，本地正则即可，不产生任何上游调用——这也是"第二次使用
  才计费"的前提：用户粘错链接不能白花一次调用。
- 短链（``v.douyin.com``）必须跟随 302 才能知道是视频还是主页，本地无法判断，
  因此明确拒绝并引导用户复制主页链接，而不是猜。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit

AccountPlatform = Literal["douyin"]

_DOUYIN_HOSTS = {
    "douyin.com",
    "www.douyin.com",
    "m.douyin.com",
    "iesdouyin.com",
    "www.iesdouyin.com",
}
_SHORT_LINK_HOSTS = {"v.douyin.com", "z.douyin.com"}
_DOUYIN_USER_PATTERN = re.compile(r"/user/([A-Za-z0-9_-]{20,120})")
# 抖音号：字母/数字/下划线，不含点号——含点的输入更可能是域名或邮箱，不做猜测。
_UNIQUE_ID_PATTERN = re.compile(r"[A-Za-z0-9_]{2,30}")
_URL_PATTERN = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_TRAILING_PUNCTUATION = ".,;:!?，。；：！？、)）]】}"


class AccountLinkError(Exception):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


@dataclass(frozen=True)
class AccountTarget:
    platform: AccountPlatform
    sec_uid: str | None
    unique_id: str | None

    @property
    def identity(self) -> str:
        """稳定标识：优先 sec_uid；只有抖音号时用它做缓存键，命中后会被补齐."""

        return self.sec_uid or f"unique:{self.unique_id}"


def _extract_url(raw: str) -> str | None:
    match = _URL_PATTERN.search(raw)
    if match:
        return match.group(0).rstrip(_TRAILING_PUNCTUATION)
    stripped = raw.strip().rstrip(_TRAILING_PUNCTUATION)
    lowered = stripped.lower()
    if lowered.startswith(("douyin.com/", "www.douyin.com/", "m.douyin.com/")):
        return f"https://{stripped}"
    return None


def _host_is(host: str, candidates: set[str]) -> bool:
    return host.lower().split(":", 1)[0] in candidates


def parse_account_target(raw: str) -> AccountTarget:
    """解析用户输入，返回诊断目标；无法识别时抛 ``AccountLinkError``."""

    text = (raw or "").strip()
    if not text:
        raise AccountLinkError(422, "ACCOUNT_LINK_EMPTY", "请粘贴抖音账号主页链接或输入抖音号。")

    url = _extract_url(text)
    if url is None:
        candidate = text.lstrip("@").strip()
        if _UNIQUE_ID_PATTERN.fullmatch(candidate):
            return AccountTarget(platform="douyin", sec_uid=None, unique_id=candidate)
        raise AccountLinkError(
            422,
            "ACCOUNT_LINK_UNSUPPORTED",
            "无法识别该输入，请粘贴抖音主页链接（形如 douyin.com/user/...）或直接输入抖音号。",
        )

    parsed = urlsplit(url)
    if not _host_is(parsed.netloc, _DOUYIN_HOSTS):
        if _host_is(parsed.netloc, _SHORT_LINK_HOSTS):
            raise AccountLinkError(
                422,
                "ACCOUNT_LINK_SHORT_UNSUPPORTED",
                "这是抖音分享短链，无法直接识别。请在抖音打开「我」→ 右上角分享 → 复制主页链接。",
            )
        raise AccountLinkError(
            422,
            "ACCOUNT_LINK_UNSUPPORTED_PLATFORM",
            "目前仅支持抖音账号主页链接。",
        )

    matched = _DOUYIN_USER_PATTERN.search(parsed.path)
    if matched:
        return AccountTarget(platform="douyin", sec_uid=matched.group(1), unique_id=None)

    # 明确区分"贴成了视频链接"，前端据此引导到视频拆解，而不是让用户以为诊断坏了。
    if "/video/" in parsed.path or "modal_id=" in parsed.query or "/note/" in parsed.path:
        raise AccountLinkError(
            422,
            "ACCOUNT_LINK_IS_VIDEO",
            "这是视频链接，不是账号主页链接。如需拆解单条视频，请使用「视频拆解」。",
        )
    raise AccountLinkError(
        422,
        "ACCOUNT_LINK_NOT_PROFILE",
        "链接里没有账号标识，请复制抖音「我」页面的主页链接。",
    )


def looks_like_account_link(raw: str) -> bool:
    """供工作台分流使用：能在本地判定为账号主页链接时返回 True，不抛异常."""

    try:
        target = parse_account_target(raw)
    except AccountLinkError:
        return False
    return target.sec_uid is not None
