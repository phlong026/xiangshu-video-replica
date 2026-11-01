"""视频号分享链接解析：登录态通道把分享口令换成无水印直链与统计。

协议为三步链：登录态校验（``/api/getuserinfo``）→ 分享链接换取预览参数
（``/api/weixin/get_parse_result``，返回 ``playable_url`` 里的 token 与
exportId）→ 预览详情（``finder-preview/api/feed/get_feed_info``，返回
标题/作者/时长/互动数据与 h264/h265 直链）。直链是明文 MP4，可直接下载，
无需走加密流解密——这正是链接导入条目与关键词采集条目（加密流）在媒体
管线里分道的原因。

凭据（登录态 cookie 与会话 agent_id）只从加密 provider_settings 读取；
请求头之外绝不落日志——external_calls 的脱敏层会自动遮蔽 Cookie 头。
错误码与文案保持中性，不出现数据源供应商名称。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, cast
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, build_opener

from app.db_portable import BusinessConnection
from app.settings import SettingsRepository, SettingsUnavailableError
from app.viral_link import (
    NoRedirectHandler,
    ResolvedViralLink,
    ResolvedViralMetadata,
    ViralLinkError,
    normalize_supported_link,
)
from app.viral_tikhub import (
    WechatVideoDetail,
    extract_wechat_tags,
    parse_compact_count,
    parse_wechat_duration_ms,
    strip_highlight,
)

PROVIDER_NAME = "yuanbao"
YUANBAO_BASE_URL = "https://yuanbao.tencent.com"
FINDER_PREVIEW_BASE_URL = "https://channels.weixin.qq.com"
WECHAT_LINK_TIMEOUT_SECONDS = 15.0
_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_DESKTOP_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

logger = logging.getLogger(__name__)


def _parse_failed(
    message: str = "视频号链接无法解析，请确认链接可正常访问后重试。",
) -> ViralLinkError:
    return ViralLinkError(422, "VIRAL_WECHAT_PARSE_FAILED", message, retryable=False)


def _auth_expired() -> ViralLinkError:
    return ViralLinkError(
        502,
        "VIRAL_WECHAT_AUTH_EXPIRED",
        "视频号采集登录态已失效，请联系管理员更新采集凭据。",
        retryable=False,
    )


def _service_failed(status_code: int = 502) -> ViralLinkError:
    return ViralLinkError(
        502 if status_code < 500 else 503,
        "VIRAL_WECHAT_SERVICE_FAILED",
        "视频号解析服务暂时不可用，请稍后重试。",
    )


class WechatLinkTransport:
    def request(
        self, url: str, *, headers: Mapping[str, str], payload: bytes | None = None
    ) -> bytes:
        raise NotImplementedError


class UrllibWechatLinkTransport(WechatLinkTransport):
    """仅允许解析链路两个白名单域；禁重定向，防登录态随 302 外流."""

    _ALLOWED_HOSTS = frozenset({"yuanbao.tencent.com", "channels.weixin.qq.com"})

    def __init__(self, *, timeout_seconds: float = WECHAT_LINK_TIMEOUT_SECONDS) -> None:
        self.timeout_seconds = timeout_seconds

    def request(
        self, url: str, *, headers: Mapping[str, str], payload: bytes | None = None
    ) -> bytes:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname not in self._ALLOWED_HOSTS:
            raise ViralLinkError(
                502,
                "VIRAL_WECHAT_SERVICE_FAILED",
                "视频号解析服务地址异常，请稍后重试。",
                retryable=False,
            )
        method = "POST" if payload is not None else "GET"
        request = Request(url, data=payload, headers=dict(headers), method=method)
        opener = build_opener(NoRedirectHandler())
        try:
            with opener.open(request, timeout=self.timeout_seconds) as response:  # noqa: S310
                return cast(bytes, response.read(_MAX_RESPONSE_BYTES + 1))
        except HTTPError as exc:
            # 只记状态码：错误体与 URL 可能携带查询参数，不进日志。
            logger.warning("Wechat link resolver returned HTTP %s", exc.code)
            raise
        except (TimeoutError, URLError, OSError) as exc:
            logger.warning("Wechat link resolver network request failed: %s", type(exc).__name__)
            raise ViralLinkError(
                504,
                "VIRAL_WECHAT_RESULT_UNKNOWN",
                "视频号解析结果未知，请稍后重试。",
                uncertain=True,
            ) from exc


@dataclass(frozen=True)
class WechatLinkCredentials:
    """解析通道登录态凭据；repr/str 一律脱敏，防止凭据经日志外泄."""

    cookie: str = field(repr=False)
    agent_id: str = ""

    def __str__(self) -> str:
        return "WechatLinkCredentials(<redacted>)"


@dataclass(frozen=True)
class _ParsedShare:
    token: str
    export_id: str


@dataclass(frozen=True)
class WechatFeedSnapshot:
    """解析链路的归一结果：直链 + 展示信息 + 互动统计（缺失为 None）.

    互动字段绝不填默认值冒充真实数据——链接导入的价值是媒体本身，
    统计是加分项，缺失就让前端按「暂无数据」展示。
    """

    export_id: str
    share_url: str
    title: str
    description: str
    author: str
    cover_url: str | None
    avatar_url: str | None
    video_url: str | None
    duration_ms: int
    likes: int | None
    comments: int | None
    shares: int | None
    collects: int | None
    published_at: int | None


def _first_str(source: Mapping[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = source.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _feed_count(source: Mapping[str, Any], keys: tuple[str, ...]) -> int | None:
    """互动计数宽容解析：整数或 "1.2万" 形态皆可，缺失/负数一律 None."""
    for key in keys:
        value = source.get(key)
        if isinstance(value, bool) or value is None:
            continue
        parsed = parse_compact_count(value) if isinstance(value, (str, int)) else None
        if parsed is not None and parsed >= 0:
            return parsed
    return None


def _feed_duration_ms(feed_info: Mapping[str, Any]) -> int:
    """时长字段形态未完全契约化：毫秒整数、秒整数、``00:11`` 字符串都兜住."""
    for key in ("duration", "durationMs", "duration_ms"):
        value = feed_info.get(key)
        if isinstance(value, (int, float)) and value > 0:
            # 三位数以内的裸数值按秒处理（视频号短视频为分钟/秒量级）。
            return int(value * 1000) if value < 1000 else int(value)
    text = _first_str(feed_info, ("durationStr", "durationText", "duration"))
    return parse_wechat_duration_ms(text) or 0


def _nested(source: Mapping[str, Any], *path: str) -> Mapping[str, Any]:
    current: Any = source
    for key in path:
        if not isinstance(current, Mapping):
            return {}
        current = current.get(key)
    return current if isinstance(current, Mapping) else {}


def _feed_video_url(feed_info: Mapping[str, Any]) -> str | None:
    """直链择优：h264 → 顶层 → h265（与参考实现的择优顺序一致）."""
    for block_path in (("h264VideoInfo",), (), ("h265VideoInfo",), ("media",)):
        block = _nested(feed_info, *block_path) if block_path else feed_info
        url = _first_str(block, ("videoUrl", "video_url", "url"))
        if url:
            return url
    return None


def _feed_cover_url(feed_info: Mapping[str, Any]) -> str | None:
    for block_path in ((), ("coverUrl",), ("media",)):
        block = _nested(feed_info, *block_path) if block_path else feed_info
        url = _first_str(block, ("coverUrl", "cover_img_url", "thumbUrl", "cover"))
        if url:
            return url
    return None


def _feed_published_at(feed_info: Mapping[str, Any]) -> int | None:
    for key in ("createTime", "create_time", "publishTime"):
        value = feed_info.get(key)
        if isinstance(value, (int, float)) and value > 0:
            # 秒级与毫秒级时间戳都出现过：按量级归一到秒。
            seconds = value / 1000 if value > 10**12 else value
            return int(seconds)
    return None


def _snapshot_from_feed(
    data: Mapping[str, Any], *, export_id: str, share_url: str
) -> WechatFeedSnapshot:
    feed_info = _nested(data, "feedInfo")
    if not feed_info:
        raise _parse_failed()
    author_info = _nested(data, "authorInfo")
    description = strip_highlight(_first_str(feed_info, ("description", "desc", "title")))
    return WechatFeedSnapshot(
        export_id=export_id,
        share_url=share_url,
        title=description[:200] or "视频号视频",
        description=description[:2000],
        author=_first_str(author_info, ("nickname", "nickName", "name"))[:120],
        cover_url=_feed_cover_url(feed_info),
        avatar_url=_first_str(author_info, ("headImgUrl", "avatar", "avatarUrl")) or None,
        video_url=_feed_video_url(feed_info),
        duration_ms=_feed_duration_ms(feed_info),
        likes=_feed_count(feed_info, ("likeCount", "likes", "like_count")),
        comments=_feed_count(feed_info, ("commentCount", "comments", "comment_count")),
        shares=_feed_count(feed_info, ("forwardCount", "shareCount", "shares")),
        collects=_feed_count(feed_info, ("favoriteCount", "favCount", "collectCount")),
        published_at=_feed_published_at(feed_info),
    )


def to_resolved_link(snapshot: WechatFeedSnapshot) -> ResolvedViralLink:
    """快照 → 现有链接导入模型；native 带上 share_url/export_id 两把钥匙.

    share_url 供媒体管线在直链过期后重取，export_id 供统计链路识别条目。
    """
    metadata = ResolvedViralMetadata(
        author_avatar=snapshot.avatar_url,
        likes=snapshot.likes,
        comments=snapshot.comments,
        shares=snapshot.shares,
        collects=snapshot.collects,
        published_at=snapshot.published_at,
        tags=tuple(extract_wechat_tags(snapshot.description)),
    )
    return ResolvedViralLink(
        platform="wechat_channels",
        video_id=snapshot.export_id,
        title=snapshot.title,
        author=snapshot.author,
        cover_url=snapshot.cover_url,
        video_url=snapshot.video_url,
        audio_url=None,
        duration_ms=snapshot.duration_ms,
        source_description=snapshot.description,
        metadata=metadata,
        native={"share_url": snapshot.share_url, "export_id": snapshot.export_id},
    )


def snapshot_to_detail(snapshot: WechatFeedSnapshot) -> WechatVideoDetail:
    """快照 → 统计链路的详情形态：只承载计数与展示字段，不携带媒体."""
    return WechatVideoDetail(
        object_id=snapshot.export_id,
        object_nonce_id=None,
        title=snapshot.title,
        description=snapshot.description or None,
        nickname=snapshot.author,
        username=None,
        create_time=snapshot.published_at,
        like_count=snapshot.likes,
        fav_count=snapshot.collects,
        forward_count=snapshot.shares,
        comment_count=snapshot.comments,
        city=None,
        full_url=None,
        decode_key=None,
        cover_url=snapshot.cover_url,
        duration_ms=snapshot.duration_ms or None,
        width=None,
        height=None,
    )


class WechatLinkClient:
    """三步链编排；``resolve`` 与 douyidou 客户端同签名，路由层可无缝分发."""

    def __init__(
        self,
        *,
        credentials: WechatLinkCredentials,
        transport: WechatLinkTransport | None = None,
        user_agent: str = _DESKTOP_USER_AGENT,
    ) -> None:
        self.credentials = credentials
        self.transport = transport or UrllibWechatLinkTransport()
        self._user_agent = user_agent
        self._user_id = ""

    def resolve(self, raw_url: str, *, purpose: str) -> ResolvedViralLink:
        return to_resolved_link(self.fetch_snapshot(raw_url))

    def check_login(self) -> str:
        """校验登录态并返回用户 ID；失效抛 VIRAL_WECHAT_AUTH_EXPIRED."""
        return self._ensure_user_id()

    def fetch_snapshot(self, raw_url: str) -> WechatFeedSnapshot:
        share_url = normalize_supported_link(raw_url)
        user_id = self._ensure_user_id()
        parsed = self._parse_share_url(share_url, user_id=user_id)
        feed = self._fetch_feed(parsed)
        return _snapshot_from_feed(feed, export_id=parsed.export_id, share_url=share_url)

    def fetch_statistics(self, share_url: str) -> WechatVideoDetail:
        """统计刷新回落：只补互动数与展示字段，不触发媒体下载."""
        return snapshot_to_detail(self.fetch_snapshot(share_url))

    # -- 内部 ------------------------------------------------------------------

    def _headers(self, *, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        headers = {
            "User-Agent": self._user_agent,
            "X-Source": "web",
            "X-Requested-With": "XMLHttpRequest",
            "Origin": YUANBAO_BASE_URL,
            # Referer 与凭据同源： Cookie 只发给白名单域，随域校验一起兜底。
            "Referer": f"{YUANBAO_BASE_URL}/chat/{self.credentials.agent_id}",
            "Cookie": self.credentials.cookie,
        }
        if extra:
            headers.update(extra)
        return headers

    def _finder_headers(self, *, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        # 预览详情是公开页面上下文：不需要登录态，也绝不能把 Cookie 发出去。
        headers = {
            "User-Agent": self._user_agent,
            "Origin": FINDER_PREVIEW_BASE_URL,
            "Referer": f"{FINDER_PREVIEW_BASE_URL}/finder-preview/pages/feed",
        }
        if extra:
            headers.update(extra)
        return headers

    def _request_json(self, url: str, *, headers: Mapping[str, str], payload: bytes | None) -> Any:
        try:
            body = self.transport.request(url, headers=headers, payload=payload)
            if len(body) > _MAX_RESPONSE_BYTES:
                raise ValueError
            return json.loads(body)
        except HTTPError as exc:
            if exc.code in (401, 403):
                raise _auth_expired() from exc
            raise _service_failed(exc.code) from exc
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
            raise _service_failed() from exc

    def _ensure_user_id(self) -> str:
        if self._user_id:
            return self._user_id
        data = self._request_json(
            f"{YUANBAO_BASE_URL}/api/getuserinfo", headers=self._headers(), payload=None
        )
        user_id = ""
        if isinstance(data, Mapping):
            user_id = _first_str(data, ("userId", "userid", "user_id", "id"))
        if not user_id:
            # 拿不到用户信息即登录态无效：先报凭据问题，避免误判为链接问题。
            raise _auth_expired()
        self._user_id = user_id
        return user_id

    def _parse_share_url(self, share_url: str, *, user_id: str) -> _ParsedShare:
        payload = json.dumps(
            {"type": "video_channel_url", "url": share_url, "scene": 1}, ensure_ascii=False
        ).encode()
        headers = self._headers(
            extra={
                "X-AgentID": self.credentials.agent_id,
                "T-UserID": user_id,
                "X-ID": user_id,
                "Content-Type": "application/json",
            }
        )
        data = self._request_json(
            f"{YUANBAO_BASE_URL}/api/weixin/get_parse_result",
            headers=headers,
            payload=payload,
        )
        if not isinstance(data, Mapping) or data.get("code") != 0:
            raise _parse_failed()
        raw_inner = data.get("data") if isinstance(data, Mapping) else None
        inner: Mapping[str, Any] = raw_inner if isinstance(raw_inner, Mapping) else {}
        playable = _first_str(inner, ("playable_url", "playableUrl"))
        query = parse_qs(urlsplit(playable).query)
        token = (query.get("token") or [""])[0]
        export_id = (
            _first_str(inner, ("wx_export_id", "export_id")) or (query.get("eid") or [""])[0]
        )
        if not token or not export_id:
            # 预览参数缺失：可能是内容无权限或上游字段变化，按链接问题上报。
            raise _parse_failed()
        return _ParsedShare(token=token, export_id=export_id)

    def _fetch_feed(self, parsed: _ParsedShare) -> Mapping[str, Any]:
        payload = json.dumps(
            {"baseReq": {"generalToken": parsed.token}, "exportId": parsed.export_id}
        ).encode()
        headers = self._finder_headers(extra={"Content-Type": "application/json"})
        data = self._request_json(
            f"{FINDER_PREVIEW_BASE_URL}/finder-preview/api/feed/get_feed_info",
            headers=headers,
            payload=payload,
        )
        feed = data.get("data") if isinstance(data, Mapping) else None
        if not isinstance(feed, Mapping):
            raise _parse_failed()
        return feed


def wechat_link_client_from_settings(conn: BusinessConnection) -> WechatLinkClient:
    try:
        config = SettingsRepository(conn).load_provider_config(PROVIDER_NAME)
    except SettingsUnavailableError as exc:
        raise ViralLinkError(
            503,
            "VIRAL_WECHAT_NOT_CONFIGURED",
            "视频号解析通道配置不可用，请联系管理员。",
        ) from exc
    cookie = config.get("cookie", "").strip()
    agent_id = config.get("agent_id", "").strip()
    if not cookie or not agent_id:
        raise ViralLinkError(
            503,
            "VIRAL_WECHAT_NOT_CONFIGURED",
            "视频号解析通道尚未配置，请联系管理员。",
        )
    return WechatLinkClient(credentials=WechatLinkCredentials(cookie=cookie, agent_id=agent_id))


def wechat_link_client_from_settings_or_none(conn: BusinessConnection) -> WechatLinkClient | None:
    """尽力构造：通道未配置返回 None，调用方退化为直链下载（不重取）."""
    try:
        return wechat_link_client_from_settings(conn)
    except ViralLinkError:
        return None


class YuanbaoProviderTester:
    """管理端「测试连接」：只校验登录态是否有效，不产生解析外呼."""

    def __init__(self, *, fallback: Any) -> None:
        self.fallback = fallback

    def connection_test(self, provider: str, config: dict[str, str]) -> Any:
        from fastapi import HTTPException

        from app.settings import ProviderTestResult

        if provider != PROVIDER_NAME or not config:
            return self.fallback.connection_test(provider, config)
        cookie = config.get("cookie", "").strip()
        agent_id = config.get("agent_id", "").strip()
        if not cookie or not agent_id:
            return self.fallback.connection_test(provider, config)
        client = WechatLinkClient(
            credentials=WechatLinkCredentials(cookie=cookie, agent_id=agent_id)
        )
        try:
            client.check_login()
        except ViralLinkError as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail={"code": exc.code, "message": exc.message},
            ) from exc
        return ProviderTestResult(status="ok", provider=provider, test_kind="connection")

    def paid_test(self, provider: str, config: dict[str, str]) -> Any:
        return self.fallback.paid_test(provider, config)
