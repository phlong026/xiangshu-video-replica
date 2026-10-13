"""Publisher abstraction over the vendored platform delivery libraries.

The vendored douyin/channels publishers are reverse-engineered HTTP clients.
This module is the only surface the rest of the app depends on, so the heavy
vendor imports stay inside the adapters and only the publish worker ever
loads them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class PublishResult:
    """Outcome of one platform delivery attempt.

    ``status="unknown"``（T23）表示提交已发出但结果未知（读超时、连接中断）：
    平台可能已接收，绝不能自动重投，只能转入人工核对收敛。
    """

    platform: str
    status: Literal["published", "failed", "unknown"]
    item_id: str | None = None
    short_url: str | None = None
    message: str | None = None
    account_invalid: bool = False


def classify_delivery_failure(exc: Exception, *, platform: str, message: str) -> PublishResult:
    """把投递异常按「提交前 / 提交后」分开（T23）。

    连接超时（ConnectTimeout）：TCP 都没建上，请求确定没到平台，按失败处理、
    可安全自动重试；读超时与连接中断：请求体可能已经发出去，一律按 unknown
    交给人工核对——重复发表的代价远高于多一次人工确认。其余异常维持既有
    语义（failed）。

    抖音 vendor 默认跑 curl_cffi（指纹会话），其异常家族与 requests **无继承
    关系**，必须同样识别；无法归类的裸 CurlError 按错误码分：连接建立类确定
    未发送（6 解析失败 / 7 连不上 / 35 SSL 握手失败）可重试，其余传输类错误
    保守按 unknown。两个库都不可导入时按 failed（此时投递本来就不可能发出）。
    curl_cffi 的导入保持懒加载：FastAPI 进程不拉 curl 机制。
    """
    pre_send: list[type[Exception]] = []
    post_send: list[type[Exception]] = []
    try:
        import requests as _requests

        pre_send.append(_requests.exceptions.ConnectTimeout)
        post_send.extend([_requests.exceptions.ReadTimeout, _requests.exceptions.ConnectionError])
    except ImportError:  # pragma: no cover - requests 是服务端必装依赖
        pass
    try:
        from curl_cffi.requests import exceptions as _curl_exceptions

        pre_send.append(_curl_exceptions.ConnectTimeout)
        post_send.extend([_curl_exceptions.ReadTimeout, _curl_exceptions.ConnectionError])
    except ImportError:  # pragma: no cover - curl_cffi 缺失时 requests 回退模式
        pass
    if any(isinstance(exc, kind) for kind in pre_send):
        return PublishResult(platform=platform, status="failed", message=message)
    if any(isinstance(exc, kind) for kind in post_send):
        return _unknown_result(platform, message)
    code = getattr(exc, "code", None)
    if isinstance(code, int) and exc.__class__.__name__ == "CurlError":
        if code in {6, 7, 35}:
            return PublishResult(platform=platform, status="failed", message=message)
        return _unknown_result(platform, message)
    return PublishResult(platform=platform, status="failed", message=message)


def _unknown_result(platform: str, message: str) -> PublishResult:
    return PublishResult(
        platform=platform,
        status="unknown",
        message=f"{message}；平台可能已接收本次发布，为避免重复发表已暂停自动重试，请人工核对",
    )


class PublisherUnavailableError(RuntimeError):
    """The platform adapter cannot run in this environment (e.g. missing Node)."""

    def __init__(self, platform: str, reason: str) -> None:
        super().__init__(f"{platform} publisher unavailable: {reason}")
        self.platform = platform
        self.reason = reason


class PublisherAuthError(RuntimeError):
    """The platform rejected the stored login state (cookie expired etc.)."""

    def __init__(self, platform: str, reason: str) -> None:
        super().__init__(f"{platform} login state rejected: {reason}")
        self.platform = platform
        self.reason = reason


class PublisherError(RuntimeError):
    """Any other platform-side delivery failure."""

    def __init__(self, platform: str, reason: str) -> None:
        super().__init__(f"{platform} publish failed: {reason}")
        self.platform = platform
        self.reason = reason


class PublisherProbeResult:
    """Login-state probe result for one stored account."""

    __slots__ = ("ok", "nickname", "message")

    def __init__(self, *, ok: bool, nickname: str | None = None, message: str = "") -> None:
        self.ok = ok
        self.nickname = nickname
        self.message = message
