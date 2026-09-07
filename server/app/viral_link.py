"""Resolve supported public links into the existing viral import source model."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from app.db_portable import BusinessConnection
from app.settings import SettingsRepository, SettingsUnavailableError

DOUYIDOU_BASE_URL = "https://gateway.diadi.cn"
DOUYIDOU_TIMEOUT_SECONDS = 25.0
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_URL_PATTERN = re.compile(r"https?://[^\s<>'\"]+")
_TRAILING_PUNCTUATION = ".,;:!?，。；：！？、)）]】}"
_WECHAT_HOSTS = {"channels.weixin.qq.com", "weixin.qq.com", "www.weixin.qq.com"}
_DOUYIN_HOSTS = {
    "douyin.com",
    "www.douyin.com",
    "v.douyin.com",
    "iesdouyin.com",
    "www.iesdouyin.com",
}

logger = logging.getLogger(__name__)


class ViralLinkError(Exception):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        retryable: bool = True,
        uncertain: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.retryable = retryable
        self.uncertain = uncertain


class NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, *args: object, **kwargs: object) -> None:
        return None


class DouyidouHttpTransport:
    def request(self, url: str, *, headers: Mapping[str, str]) -> bytes:
        raise NotImplementedError


class UrllibDouyidouHttpTransport(DouyidouHttpTransport):
    def __init__(self, *, timeout_seconds: float = DOUYIDOU_TIMEOUT_SECONDS) -> None:
        self.timeout_seconds = timeout_seconds

    def request(self, url: str, *, headers: Mapping[str, str]) -> bytes:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname != "gateway.diadi.cn":
            raise ViralLinkError(
                502,
                "VIRAL_LINK_GATEWAY_INVALID",
                "视频链接解析服务地址异常，请上传 MP4/MOV 文件。",
                retryable=False,
            )
        request = Request(url, headers=dict(headers), method="GET")
        opener = build_opener(NoRedirectHandler())
        try:
            with opener.open(request, timeout=self.timeout_seconds) as response:  # noqa: S310
                return cast(bytes, response.read(_MAX_RESPONSE_BYTES + 1))
        except HTTPError as exc:
            logger.warning("Link resolver returned HTTP %s", exc.code)
            raise
        except (TimeoutError, URLError, OSError) as exc:
            logger.warning("Link resolver network request failed: %s", type(exc).__name__)
            raise ViralLinkError(
                504,
                "VIRAL_LINK_SUBMISSION_UNCERTAIN",
                "视频链接解析结果未知，请使用原请求重试查询，或上传 MP4/MOV 文件。",
                retryable=False,
                uncertain=True,
            ) from exc


@dataclass(frozen=True)
class ResolvedViralLink:
    platform: Literal["douyin"]
    video_id: str
    title: str
    author: str
    cover_url: str | None
    video_url: str | None
    audio_url: str | None
    duration_ms: int
    source_description: str


def normalize_supported_link(raw: str) -> str:
    match = _URL_PATTERN.search(raw.strip())
    if match is None:
        raise ViralLinkError(
            422,
            "VIRAL_LINK_INVALID",
            "请输入完整的抖音视频链接，或上传 MP4/MOV 文件。",
            retryable=False,
        )
    url = match.group(0).rstrip(_TRAILING_PUNCTUATION)
    host = (urlsplit(url).hostname or "").lower()
    if host in _WECHAT_HOSTS or host.endswith(".weixin.qq.com"):
        raise ViralLinkError(
            422,
            "WECHAT_CHANNELS_LINK_UNSUPPORTED",
            "视频号链接暂不支持解析，请上传 MP4 或 MOV 文件。",
            retryable=False,
        )
    if host not in _DOUYIN_HOSTS and not host.endswith(".douyin.com"):
        raise ViralLinkError(
            422,
            "VIRAL_LINK_PLATFORM_UNSUPPORTED",
            "当前仅支持抖音视频链接，请上传 MP4 或 MOV 文件。",
            retryable=False,
        )
    return url


def _first_url(value: object) -> str | None:
    if not isinstance(value, list):
        return None
    for item in value:
        candidate: object = item
        if isinstance(item, dict):
            candidate = item.get("url") or item.get("play_url")
        if isinstance(candidate, str) and candidate.startswith(("https://", "http://")):
            return candidate
    return None


def _duration_ms(payload: dict[str, Any]) -> int:
    for key in ("duration", "video_duration", "time", "length"):
        raw_duration = payload.get(key)
        if isinstance(raw_duration, bool) or not isinstance(raw_duration, (int, float, str)):
            continue
        try:
            duration = float(raw_duration)
        except (TypeError, ValueError):
            continue
        if duration > 0:
            return round(duration if duration > 10_000 else duration * 1000)
    return 0


class DouyidouLinkClient:
    def __init__(
        self,
        *,
        app_id: str,
        app_secret: str,
        transport: DouyidouHttpTransport | None = None,
    ) -> None:
        self.app_id = app_id
        self.app_secret = app_secret
        self.transport = transport or UrllibDouyidouHttpTransport()

    def _request(self, source_url: str) -> dict[str, Any]:
        params = {"app_id": self.app_id, "url": source_url}
        query = urlencode(sorted(params.items()))
        signature = hashlib.md5(  # noqa: S324 - required vendor signature protocol
            f"{query}{self.app_secret}".encode(), usedforsecurity=False
        ).hexdigest()
        endpoint = f"{DOUYIDOU_BASE_URL}/api/parse?{query}"
        try:
            body = self.transport.request(endpoint, headers={"Sign": signature})
            if len(body) > _MAX_RESPONSE_BYTES:
                raise ValueError
            decoded = json.loads(body)
            if not isinstance(decoded, dict):
                raise ValueError
            return decoded
        except HTTPError as exc:
            uncertain = exc.code >= 500
            raise ViralLinkError(
                503 if uncertain else 422,
                "VIRAL_LINK_SUBMISSION_UNCERTAIN" if uncertain else "VIRAL_LINK_PROVIDER_FAILED",
                (
                    "视频链接解析结果未知，请使用原请求重试查询，或上传 MP4/MOV 文件。"
                    if uncertain
                    else "视频链接暂时无法解析，请检查链接或上传 MP4/MOV 文件。"
                ),
                retryable=False,
                uncertain=uncertain,
            ) from exc
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
            raise ViralLinkError(
                502,
                "VIRAL_LINK_RESPONSE_INVALID",
                "视频链接解析结果异常，请稍后重试或上传 MP4/MOV 文件。",
                retryable=False,
            ) from exc

    def resolve(self, raw_url: str, *, purpose: str) -> ResolvedViralLink:
        source_url = normalize_supported_link(raw_url)
        response = self._request(source_url)
        if response.get("code") != 0:
            message = str(response.get("message") or response.get("msg") or "")
            expired = any(word in message for word in ("过期", "失效", "不存在", "下架"))
            raise ViralLinkError(
                410 if expired else 422,
                "VIRAL_LINK_EXPIRED" if expired else "VIRAL_LINK_PARSE_FAILED",
                (
                    "视频链接已过期，请重新复制链接或上传 MP4/MOV 文件。"
                    if expired
                    else "视频链接无法解析，请检查链接或上传 MP4/MOV 文件。"
                ),
                retryable=False,
            )
        payload = response.get("data")
        if not isinstance(payload, dict):
            payload = {}
        video_url = _first_url(payload.get("video"))
        audio_url = _first_url(payload.get("audio"))
        if not video_url and (purpose == "replica" or not audio_url):
            raise ViralLinkError(
                422,
                "VIRAL_LINK_MEDIA_MISSING",
                "链接中没有可用视频，请上传 MP4 或 MOV 文件。",
                retryable=False,
            )
        source_id = next(
            (
                str(payload[key]).strip()
                for key in ("aweme_id", "item_id", "id")
                if payload.get(key)
            ),
            "",
        )
        if not re.fullmatch(r"\d{17,20}", source_id):
            raise ViralLinkError(
                502,
                "VIRAL_LINK_NATIVE_ID_INVALID",
                "链接未返回有效视频标识，请上传 MP4 或 MOV 文件。",
                retryable=False,
            )
        video_id = source_id
        author = payload.get("author")
        author_name = ""
        if isinstance(author, dict):
            author_name = str(author.get("nickname") or author.get("name") or "")
        elif isinstance(author, str):
            author_name = author
        text = str(payload.get("text") or "").strip()
        title = str(payload.get("title") or "").strip() or text[:40] or "链接视频"
        cover = payload.get("cover")
        return ResolvedViralLink(
            platform="douyin",
            video_id=video_id,
            title=title[:200],
            author=author_name[:120],
            cover_url=(str(cover) if isinstance(cover, str) and cover else None),
            video_url=video_url,
            audio_url=audio_url,
            duration_ms=_duration_ms(payload),
            source_description=text[:2000],
        )


def douyidou_link_client_from_settings(conn: BusinessConnection) -> DouyidouLinkClient:
    try:
        config = SettingsRepository(conn).load_provider_config("douyidou")
    except SettingsUnavailableError as exc:
        raise ViralLinkError(
            503,
            "VIRAL_LINK_SETTINGS_UNAVAILABLE",
            "视频链接解析服务配置不可用，请联系管理员或上传 MP4/MOV 文件。",
        ) from exc
    app_id = config.get("app_id", "").strip()
    app_secret = config.get("app_secret", "").strip()
    if not app_id or not app_secret:
        raise ViralLinkError(
            503,
            "VIRAL_LINK_NOT_CONFIGURED",
            "视频链接解析服务尚未配置，请联系管理员或上传 MP4/MOV 文件。",
        )
    return DouyidouLinkClient(app_id=app_id, app_secret=app_secret)
