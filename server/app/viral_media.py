"""爆款视频媒体管线（C4 重启）.

把爆款视频的媒体文件取回并落到主存储（COS / 本地盘），供提取文案、
视频复刻与详情页播放复用：

- 抖音：音频优先（``music.play_url`` 原声 mp3），无音频直链时取最低
  分辨率 MP4（bit_rate 最低档，客户端层已选好）。
- 视频号：无音频直链 → 详情接口取 ``full_url`` + ``decode_key`` →
  内存解密（仅前 128 KiB 变换，其余透传，不落盘）。

对象 key 确定性命名（``viral/{platform}/{video_id}.{mp3|mp4}``）：
同平台同视频只拉取/解密一次，重复请求命中已存对象后直接签名返回。
调用方须传入**新鲜**的 ``ViralVideo``（视频号 exportId 会过期，路由层
负责在缓存过期时重新搜索刷新）。
"""

from __future__ import annotations

import hashlib
import http.client
import ipaddress
import logging
import socket
import ssl
import threading
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import timedelta
from typing import Protocol
from urllib.parse import quote, urljoin, urlsplit

from app.storage import DownloadIntent, StoredObject
from app.viral_decrypt import decrypt_head, is_encrypted_mp4
from app.viral_tikhub import (
    PLATFORM_DOUYIN,
    PLATFORM_WECHAT,
    ViralSourceClient,
    ViralSourceError,
    ViralVideo,
    WechatVideoDetail,
)

VIRAL_STORAGE_PREFIX = "viral"
VIRAL_MEDIA_URL_TTL = timedelta(hours=6)
_DEFAULT_FETCH_TIMEOUT_SECONDS = 60.0
_USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"
# 单文件下载上限：短视频/封面远超此值的必然是异常响应，防止把响应体整读进
# 内存时被恶意或异常源站打爆 API 进程。
_MAX_DOWNLOAD_BYTES = 512 * 1024 * 1024

logger = logging.getLogger(__name__)
_MEDIA_LOCKS = tuple(threading.Lock() for _ in range(32))


class ViralMediaError(ViralSourceError):
    """爆款媒体获取失败（文案中性，不含供应商名称）。"""


@dataclass(frozen=True)
class ViralMediaResult:
    kind: str  # "audio" | "video"
    storage_uri: str
    url: str
    size: int
    content_type: str
    cache_hit: bool
    sha256: str = ""


def _storage_video_id(video_id: str) -> str:
    """保留历史合法 key；仅把含危险路径段的 opaque ID 映射为稳定名称。"""
    parts = video_id.split("/")
    if (
        "\\" not in video_id
        and "\x00" not in video_id
        and all(part not in {"", ".", ".."} for part in parts)
    ):
        return video_id
    digest = hashlib.sha256(video_id.encode("utf-8")).hexdigest()
    return f"unsafe-{digest}"


def viral_media_key(platform: str, video_id: str, kind: str) -> str:
    extension = "mp3" if kind == "audio" else "mp4"
    if platform == PLATFORM_DOUYIN and kind == "video":
        extension = "browser.mp4"
    return f"{VIRAL_STORAGE_PREFIX}/{platform}/{_storage_video_id(video_id)}.{extension}"


def viral_cover_key(platform: str, video_id: str) -> str:
    return f"{VIRAL_STORAGE_PREFIX}/cover/{platform}/{_storage_video_id(video_id)}"


class ViralStorage(Protocol):
    """媒体管线需要的最小存储面（``StorageAdapter`` 的结构子集）."""

    def head_object(self, key: str) -> StoredObject | None: ...

    def put_object(self, key: str, content: bytes, *, content_type: str) -> StoredObject: ...

    def get_object(self, key: str) -> bytes: ...

    def create_download_intent(
        self, key: str, *, expires_in: timedelta, can_read: bool
    ) -> DownloadIntent: ...


class UrlFetcher:
    """下载远端媒体字节（可注入以便测试）.

    上游返回的媒体/封面 URL 属于半可信输入：真实视频号 CDN 链接就是
    ``http://``，因此不能强制 https，但必须拒绝非 http(s) 协议与解析到
    私网/环回/链路本地的地址（``file://``、云元数据 169.254.169.254 等），
    否则被污染的数据源响应可以驱动服务端 SSRF。连接固定到本次校验得到的
    公网 IP；HTTPS 仍使用原主机名做 SNI 与证书校验，每次重定向重新校验。
    """

    def __init__(
        self,
        *,
        timeout_seconds: float = _DEFAULT_FETCH_TIMEOUT_SECONDS,
        max_bytes: int = _MAX_DOWNLOAD_BYTES,
        connection_factory: Callable[[str, str, int, str, float], http.client.HTTPConnection]
        | None = None,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.max_bytes = max_bytes
        self.last_content_type: str | None = None
        self._connection_factory = connection_factory or _pinned_connection

    def fetch(self, url: str) -> bytes:
        current_url = url
        for _redirect in range(6):
            scheme, hostname, port, connect_ip = _resolve_public_http_url(current_url)
            parsed = urlsplit(current_url)
            target = parsed.path or "/"
            if parsed.query:
                target += f"?{parsed.query}"
            host_header = f"[{hostname}]" if ":" in hostname else hostname
            if port != (443 if scheme == "https" else 80):
                host_header = f"{host_header}:{port}"
            connection = self._connection_factory(
                scheme, hostname, port, connect_ip, self.timeout_seconds
            )
            try:
                connection.request(
                    "GET",
                    target,
                    headers={"Host": host_header, "User-Agent": _USER_AGENT},
                )
                response = connection.getresponse()
                if response.status in {301, 302, 303, 307, 308}:
                    location = response.headers.get("Location")
                    if not location:
                        raise ViralMediaError("媒体地址重定向无效")
                    next_url = urljoin(current_url, location)
                    if scheme == "https" and urlsplit(next_url).scheme != "https":
                        raise ViralMediaError("媒体地址禁止降级到不安全连接")
                    current_url = next_url
                    continue
                if response.status < 200 or response.status >= 300:
                    raise ViralMediaError("媒体地址返回异常状态")
                return self._read_response(response)
            finally:
                connection.close()
        raise ViralMediaError("媒体地址重定向次数过多")

    def _read_response(self, response: http.client.HTTPResponse) -> bytes:
        self.last_content_type = response.headers.get("Content-Type")
        declared = response.headers.get("Content-Length")
        try:
            if declared and int(declared) > self.max_bytes:
                raise ViralMediaError("媒体文件超出可下载大小上限")
        except ValueError as exc:
            raise ViralMediaError("媒体地址返回无效文件长度") from exc
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > self.max_bytes:
                raise ViralMediaError("媒体文件超出可下载大小上限")
            chunks.append(chunk)
        return b"".join(chunks)


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host: str, port: int, connect_ip: str, timeout: float) -> None:
        super().__init__(host, port=port, timeout=timeout)
        self._connect_ip = connect_ip

    def connect(self) -> None:
        self.sock = socket.create_connection((self._connect_ip, self.port), self.timeout)
        _verify_peer(self.sock, self._connect_ip)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, port: int, connect_ip: str, timeout: float) -> None:
        tls_context = ssl.create_default_context()
        super().__init__(host, port=port, timeout=timeout, context=tls_context)
        self._connect_ip = connect_ip
        self._tls_context = tls_context

    def connect(self) -> None:
        sock = socket.create_connection((self._connect_ip, self.port), self.timeout)
        _verify_peer(sock, self._connect_ip)
        try:
            self.sock = self._tls_context.wrap_socket(sock, server_hostname=self.host)
        except BaseException:
            sock.close()
            raise


def _verify_peer(sock: socket.socket, expected_ip: str) -> None:
    actual_ip = str(sock.getpeername()[0])
    if ipaddress.ip_address(actual_ip) != ipaddress.ip_address(expected_ip):
        sock.close()
        raise ViralMediaError("媒体连接地址与已验证地址不一致")


def _pinned_connection(
    scheme: str, hostname: str, port: int, connect_ip: str, timeout: float
) -> http.client.HTTPConnection:
    if scheme == "https":
        return _PinnedHTTPSConnection(hostname, port, connect_ip, timeout)
    return _PinnedHTTPConnection(hostname, port, connect_ip, timeout)


def _resolve_public_http_url(url: str) -> tuple[str, str, int, str]:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"}:
        raise ViralMediaError("媒体地址协议不受支持")
    hostname = parsed.hostname
    if not hostname:
        raise ViralMediaError("媒体地址缺少主机名")
    if parsed.username is not None or parsed.password is not None:
        raise ViralMediaError("媒体地址不得包含凭据")
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise ViralMediaError("媒体地址端口无效") from exc
    expected_port = 443 if parsed.scheme == "https" else 80
    if port != expected_port:
        raise ViralMediaError("媒体地址端口不受支持")
    try:
        addresses = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ViralMediaError("媒体地址无法解析") from exc
    if not addresses:
        raise ViralMediaError("媒体地址无法解析")
    public_ips: list[str] = []
    for address in addresses:
        ip_text = str(address[4][0])
        ip = ipaddress.ip_address(ip_text)
        if not ip.is_global:
            raise ViralMediaError("媒体地址必须指向公网主机")
        if ip_text not in public_ips:
            public_ips.append(ip_text)
    return parsed.scheme, hostname, port, public_ips[0]


def guess_image_content_type(content: bytes, url: str = "") -> str:
    """按魔数判定图片类型，扩展名兜底（部分源站 URL 无扩展名）."""
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if content.startswith(b"RIFF") and content[8:12] == b"WEBP":
        return "image/webp"
    if content.startswith(b"\x89PNG"):
        return "image/png"
    if content.startswith(b"GIF8"):
        return "image/gif"
    from mimetypes import guess_type

    guessed = guess_type(urlsplit(url).path)[0]
    return guessed if guessed and guessed.startswith("image/") else "image/jpeg"


def _fetch_or_raise(fetcher: UrlFetcher, url: str) -> bytes:
    try:
        return fetcher.fetch(url)
    except ViralSourceError:
        raise
    except Exception as exc:  # noqa: BLE001 - 远端 CDN 可能抛出任意异常
        logger.warning("Viral media download failed: %s", type(exc).__name__)
        raise ViralMediaError("该视频素材暂时无法获取，请稍后重试") from exc


class CoverEnricher:
    """把数据源封面落成自有存储的长期副本（源站签名链接会过期）.

    每个封面只下载一次（head 去重）；下载失败保留源站链接兜底。
    """

    def __init__(self, *, storage: ViralStorage, fetcher: UrlFetcher) -> None:
        self._storage = storage
        self._fetcher = fetcher

    def stable_url(self, platform: str, video_id: str) -> str:
        return f"/api/viral/covers/{platform}/{quote(video_id, safe='')}"

    def enrich(self, video: ViralVideo) -> ViralVideo:
        if not video.cover_url:
            return video
        key = viral_cover_key(video.platform, video.video_id)
        try:
            if self._storage.head_object(key) is None:
                content = _fetch_or_raise(self._fetcher, video.cover_url)
                self._storage.put_object(
                    key,
                    content,
                    content_type=guess_image_content_type(content, video.cover_url),
                )
        except Exception as exc:
            logger.warning(
                "Viral cover unavailable for %s/%s: %s",
                video.platform,
                video.video_id,
                type(exc).__name__,
            )
            return video
        return replace(
            video,
            cover_key=key,
            cover_url=self.stable_url(video.platform, video.video_id),
        )


class ViralMediaPipeline:
    """按平台把爆款视频媒体取回主存储并返回可播放/下载的签名地址."""

    def __init__(
        self,
        *,
        client: ViralSourceClient | None,
        storage: ViralStorage,
        fetcher: UrlFetcher | None = None,
        validator: Callable[[bytes, str, str | None], None] | None = None,
    ) -> None:
        self._client = client
        self._storage = storage
        self._fetcher = fetcher or UrlFetcher()
        self._validator = validator
        self.detail: WechatVideoDetail | None = None

    def fetch(self, video: ViralVideo, *, prefer: str | None = None) -> ViralMediaResult:
        kind, content_type = self._resolve_kind(video, prefer)
        key = viral_media_key(video.platform, video.video_id, kind)
        # 有界锁槽：同一文件的并发播放等待首份副本，不重复付费、下载或覆盖。
        with _MEDIA_LOCKS[hash(key) % len(_MEDIA_LOCKS)]:
            self.detail = None
            existing = self._storage.head_object(key)
            if existing is not None:
                if self._validator is not None:
                    self._validator(self._storage.get_object(key), kind, existing.content_type)
                return self._result(key, existing, kind, content_type, cache_hit=True)
            content = self._download_content(video, kind)
            if self._validator is not None:
                self._validator(
                    content,
                    kind,
                    getattr(self._fetcher, "last_content_type", None),
                )
            stored = self._storage.put_object(key, content, content_type=content_type)
            return self._result(key, stored, kind, content_type, cache_hit=False)

    # -- 内部 -----------------------------------------------------------------

    def _resolve_kind(self, video: ViralVideo, prefer: str | None = None) -> tuple[str, str]:
        if video.platform == PLATFORM_DOUYIN:
            if prefer == "video":
                if video.play_url:
                    return "video", "video/mp4"
                raise ViralMediaError("该视频暂无可用的媒体地址")
            if video.audio_url:
                return "audio", "audio/mpeg"
            if video.play_url:
                return "video", "video/mp4"
            raise ViralMediaError("该视频暂无可用的媒体地址")
        if video.platform == PLATFORM_WECHAT:
            return "video", "video/mp4"
        raise ViralMediaError("暂不支持的视频平台")

    def _download_content(self, video: ViralVideo, kind: str) -> bytes:
        if video.platform == PLATFORM_DOUYIN:
            url = video.audio_url if kind == "audio" else video.play_url
            if not url:
                raise ViralMediaError("该视频暂无可用的媒体地址")
            content = _fetch_or_raise(self._fetcher, url)
            if kind == "video" and is_encrypted_mp4(content[:8]):
                raise ViralMediaError("该视频素材暂时无法获取，请稍后重试")
            return content
        if video.platform == PLATFORM_WECHAT:
            return self._download_wechat_video(video)
        raise ViralMediaError("暂不支持的视频平台")

    def _wechat_detail(self, video: ViralVideo) -> WechatVideoDetail:
        export_id = str(video.native.get("export_id") or "")
        if not export_id or self._client is None:
            raise ViralMediaError("该视频素材暂时无法获取，请稍后重试")
        nonce = video.native.get("object_nonce_id") or None
        try:
            return self._client.wechat_video_detail(export_id=export_id, object_nonce_id=nonce)
        except ViralSourceError as exc:
            raise ViralMediaError("该视频素材暂时无法获取，请稍后重试") from exc

    def _download_wechat_video(self, video: ViralVideo) -> bytes:
        detail = self._wechat_detail(video)
        self.detail = detail
        if not detail.full_url or not detail.decode_key:
            raise ViralMediaError("该视频素材暂时无法获取，请稍后重试")
        content = _fetch_or_raise(self._fetcher, detail.full_url)
        if is_encrypted_mp4(content[:8]):
            content = decrypt_head(content, detail.decode_key)
        if is_encrypted_mp4(content[:8]):
            # 解密后仍不是标准 MP4：decode_key 不匹配或文件异常。
            raise ViralMediaError("该视频素材暂时无法获取，请稍后重试")
        return content

    def _result(
        self,
        key: str,
        stored: StoredObject,
        kind: str,
        content_type: str,
        *,
        cache_hit: bool,
    ) -> ViralMediaResult:
        intent = self._storage.create_download_intent(
            key, expires_in=VIRAL_MEDIA_URL_TTL, can_read=True
        )
        return ViralMediaResult(
            kind=kind,
            storage_uri=stored.uri,
            url=intent.url,
            size=stored.size,
            content_type=stored.content_type,
            cache_hit=cache_hit,
            sha256=str(getattr(stored, "sha256", "")),
        )
