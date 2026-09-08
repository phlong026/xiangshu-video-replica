"""爆款媒体管线测试：音频优先、低清兜底、视频号解密与缓存命中."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier, Lock
from typing import Any

import pytest

from app import viral_media
from app.storage import LocalStorageAdapter
from app.viral_decrypt import keystream
from app.viral_media import (
    UrlFetcher,
    ViralMediaError,
    ViralMediaPipeline,
    viral_cover_key,
    viral_media_key,
)
from app.viral_tikhub import ViralSourceClient, ViralVideo

_DECODE_KEY = "1789473271"  # 公开样本 key，用作测试向量


class FakeViralStorage:
    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str]] = {}

    def head_object(self, key: str) -> Any:
        entry = self.objects.get(key)
        if entry is None:
            return None
        content, content_type = entry
        return _Stored(key, len(content), content_type)

    def put_object(self, key: str, content: bytes, *, content_type: str) -> Any:
        self.objects[key] = (content, content_type)
        return _Stored(key, len(content), content_type)

    def create_download_intent(self, key: str, *, expires_in, can_read) -> Any:
        assert can_read is True
        return _Intent(url=f"https://storage.test/{key}")


class _Stored:
    def __init__(self, key: str, size: int, content_type: str) -> None:
        self.key = key
        self.uri = f"test://{key}"
        self.size = size
        self.content_type = content_type
        self.updated_at = datetime.now(UTC)


class _Intent:
    def __init__(self, *, url: str) -> None:
        self.url = url
        self.key = ""
        self.expires_at = datetime.now(UTC) + timedelta(hours=1)
        self.method = "GET"


class FakeFetcher:
    def __init__(self, payloads: dict[str, bytes]) -> None:
        self.payloads = payloads
        self.calls: list[str] = []

    def fetch(self, url: str) -> bytes:
        self.calls.append(url)
        if url not in self.payloads:
            raise AssertionError(f"unexpected fetch: {url}")
        return self.payloads[url]


class FakeDetailTransport:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.bodies: list[dict[str, Any]] = []

    @property
    def last_body(self) -> dict[str, Any]:
        return self.bodies[-1]

    @property
    def last_body_count(self) -> int:
        return len(self.bodies)

    def request(self, method: str, url: str, *, headers, body=None) -> bytes:
        import json

        self.bodies.append(json.loads(body or b"{}"))
        return json.dumps(self.payload, ensure_ascii=False).encode("utf-8")


def _video(platform: str, video_id: str = "v1", **native: Any) -> ViralVideo:
    return ViralVideo(
        platform=platform,
        video_id=video_id,
        category="测试",
        title="标题",
        author="作者",
        author_avatar=None,
        verified=False,
        cover_url=None,
        duration_ms=11_000,
        likes=1,
        comments=None,
        shares=None,
        collects=None,
        published_at=None,
        published_display=None,
        like_display=None,
        tags=["标签"],
        play_url=f"https://cdn.test/{video_id}.mp4" if platform == "douyin" else None,
        audio_url=f"https://cdn.test/{video_id}.mp3" if platform == "douyin" else None,
        native=dict(native),
    )


def _pipeline(
    *,
    storage: FakeViralStorage | None = None,
    fetcher: FakeFetcher | None = None,
    detail_payload: dict[str, Any] | None = None,
) -> tuple[ViralMediaPipeline, FakeViralStorage, FakeFetcher, FakeDetailTransport]:
    storage = storage or FakeViralStorage()
    fetcher = fetcher or FakeFetcher({})
    detail_transport = FakeDetailTransport(detail_payload or {})
    client = ViralSourceClient(
        api_key="test-key",
        transport=FakeDetailTransport({"code": 200, "data": {}}),
        detail_transport=detail_transport,
    )
    pipeline = ViralMediaPipeline(client=client, storage=storage, fetcher=fetcher)
    return pipeline, storage, fetcher, detail_transport


def test_viral_media_key_naming() -> None:
    assert viral_media_key("douyin", "v1", "audio") == "viral/douyin/v1.mp3"
    assert viral_media_key("wechat_channels", "v2", "video") == ("viral/wechat_channels/v2.mp4")


def test_unsafe_video_id_uses_stable_flat_storage_name(tmp_path: Path) -> None:
    video_id = "finderobjv0POr//CKfOesFBgIVVkCT4LG1YHnvIIJ9FmgsD4gFi9o="

    media_key = viral_media_key("wechat_channels", video_id, "video")
    cover_key = viral_cover_key("wechat_channels", video_id)

    assert "//" not in media_key
    assert "//" not in cover_key
    assert media_key == viral_media_key("wechat_channels", video_id, "video")
    assert cover_key == viral_cover_key("wechat_channels", video_id)
    assert viral_media_key("wechat_channels", "legal/id", "video") == (
        "viral/wechat_channels/legal/id.mp4"
    )
    storage = LocalStorageAdapter(root=tmp_path)
    storage.put_object(media_key, b"video", content_type="video/mp4")
    storage.put_object(cover_key, b"cover", content_type="image/jpeg")
    assert storage.head_object(media_key) is not None
    assert storage.head_object(cover_key) is not None


@pytest.mark.parametrize(
    "video_id",
    ["opaque//id", "opaque/./id", "opaque/../id", "opaque\\id", "opaque\x00id"],
)
def test_all_unsafe_path_forms_use_deterministic_safe_keys(video_id: str) -> None:
    first = viral_media_key("wechat_channels", video_id, "video")
    second = viral_media_key("wechat_channels", video_id, "video")
    storage_name = first.removeprefix("viral/wechat_channels/").removesuffix(".mp4")

    assert first == second
    assert storage_name.startswith("unsafe-")
    assert "/" not in storage_name
    assert "\\" not in storage_name
    assert "\x00" not in storage_name


def test_douyin_prefers_audio() -> None:
    fetcher = FakeFetcher({"https://cdn.test/v1.mp3": b"ID3-audio-bytes"})
    pipeline, storage, _, _ = _pipeline(fetcher=fetcher)
    result = pipeline.fetch(_video("douyin"))
    assert result.kind == "audio"
    assert result.content_type == "audio/mpeg"
    assert result.url == "https://storage.test/viral/douyin/v1.mp3"
    assert result.cache_hit is False
    assert storage.objects["viral/douyin/v1.mp3"][0] == b"ID3-audio-bytes"
    assert fetcher.calls == ["https://cdn.test/v1.mp3"]


def test_douyin_falls_back_to_low_resolution_video() -> None:
    fetcher = FakeFetcher({"https://cdn.test/v1.mp4": b"\x00\x00\x00 ftypisom"})
    pipeline, storage, fetcher_ref, _ = _pipeline(fetcher=fetcher)
    video = _video("douyin")
    object.__setattr__(video, "audio_url", None)
    result = pipeline.fetch(video)
    assert result.kind == "video"
    assert storage.objects["viral/douyin/v1.browser.mp4"][0] == b"\x00\x00\x00 ftypisom"
    assert fetcher_ref.calls == ["https://cdn.test/v1.mp4"]


def test_douyin_without_any_media_raises() -> None:
    pipeline, _, _, _ = _pipeline()
    video = _video("douyin")
    object.__setattr__(video, "audio_url", None)
    object.__setattr__(video, "play_url", None)
    with pytest.raises(ViralMediaError):
        pipeline.fetch(video)


def test_douyin_video_content_must_be_plain_mp4() -> None:
    fetcher = FakeFetcher({"https://cdn.test/v1.mp4": b"encrypted-garbage"})
    pipeline, _, _, _ = _pipeline(fetcher=fetcher)
    video = _video("douyin")
    object.__setattr__(video, "audio_url", None)
    with pytest.raises(ViralMediaError):
        pipeline.fetch(video)


def _detail_payload(full_url: str, decode_key: str) -> dict[str, Any]:
    return {
        "code": 200,
        "data": {
            "id": 15003884913433053492,
            "nickname": "作者",
            "title": "标题",
            "media": {
                "full_url": full_url,
                "decode_key": decode_key,
                "duration": 11,
                "width": 1080,
                "height": 1920,
            },
        },
    }


def test_wechat_decrypts_and_stores() -> None:
    plain = b"\x00\x00\x00 ftypisom" + bytes(range(256)) * 8
    stream = keystream(_DECODE_KEY, len(plain))
    encrypted = bytes(p ^ s for p, s in zip(plain, stream))
    fetcher = FakeFetcher({"http://wxapp.tc.qq.com/file": encrypted})
    detail = _detail_payload("http://wxapp.tc.qq.com/file", _DECODE_KEY)
    pipeline, storage, _, detail_transport = _pipeline(fetcher=fetcher, detail_payload=detail)
    video = _video(
        "wechat_channels",
        video_id="doc-1",
        export_id="export/e1",
        object_nonce_id="4488625110168773069",
    )
    result = pipeline.fetch(video)
    assert result.kind == "video"
    assert storage.objects["viral/wechat_channels/doc-1.mp4"][0] == plain
    assert fetcher.calls == ["http://wxapp.tc.qq.com/file"]
    body = detail_transport.last_body
    assert body["export_id"] == "export/e1"
    assert body["object_nonce_id"] == "4488625110168773069"


def test_wechat_plain_content_passes_through() -> None:
    plain = b"\x00\x00\x00 ftypisom" + b"\x00" * 64
    fetcher = FakeFetcher({"http://wxapp.tc.qq.com/file": plain})
    detail = _detail_payload("http://wxapp.tc.qq.com/file", _DECODE_KEY)
    pipeline, storage, _, _ = _pipeline(fetcher=fetcher, detail_payload=detail)
    video = _video("wechat_channels", video_id="doc-2", export_id="export/e2")
    result = pipeline.fetch(video)
    assert storage.objects["viral/wechat_channels/doc-2.mp4"][0] == plain
    assert result.size == len(plain)


def test_wechat_decrypt_mismatch_raises() -> None:
    plain = b"\x00\x00\x00 ftypisom" + b"\x00" * 64
    stream = keystream("2136343393", len(plain))  # 与 detail 返回的 key 不同
    encrypted = bytes(p ^ s for p, s in zip(plain, stream))
    fetcher = FakeFetcher({"http://wxapp.tc.qq.com/file": encrypted})
    detail = _detail_payload("http://wxapp.tc.qq.com/file", _DECODE_KEY)
    pipeline, _, _, _ = _pipeline(fetcher=fetcher, detail_payload=detail)
    video = _video("wechat_channels", video_id="doc-3", export_id="export/e3")
    with pytest.raises(ViralMediaError):
        pipeline.fetch(video)


def test_wechat_without_export_id_raises() -> None:
    pipeline, _, fetcher, _ = _pipeline()
    video = _video("wechat_channels", video_id="doc-4")
    with pytest.raises(ViralMediaError):
        pipeline.fetch(video)
    assert fetcher.calls == []


def test_second_fetch_hits_storage_cache() -> None:
    plain = b"\x00\x00\x00 ftypisom" + b"\x00" * 64
    stream = keystream(_DECODE_KEY, len(plain))
    encrypted = bytes(p ^ s for p, s in zip(plain, stream))
    fetcher = FakeFetcher({"http://wxapp.tc.qq.com/file": encrypted})
    detail = _detail_payload("http://wxapp.tc.qq.com/file", _DECODE_KEY)
    pipeline, storage, fetcher_ref, detail_transport = _pipeline(
        fetcher=fetcher, detail_payload=detail
    )
    video = _video("wechat_channels", video_id="doc-5", export_id="export/e5")
    first = pipeline.fetch(video)
    second = pipeline.fetch(video)
    assert first.cache_hit is False
    assert second.cache_hit is True
    assert second.url == first.url
    assert fetcher_ref.calls.count("http://wxapp.tc.qq.com/file") == 1
    assert detail_transport.last_body_count == 1
    assert "viral/wechat_channels/doc-5.mp4" in storage.objects


def test_wechat_id_with_empty_path_segment_hits_media_cache() -> None:
    video_id = "finderobjv0POr//CKfOesFBgIVVkCT4LG1YHnvIIJ9FmgsD4gFi9o="
    plain = b"\x00\x00\x00 ftypisom" + b"\x00" * 64
    fetcher = FakeFetcher({"http://wxapp.tc.qq.com/file": plain})
    detail = _detail_payload("http://wxapp.tc.qq.com/file", _DECODE_KEY)
    pipeline, storage, fetcher_ref, detail_transport = _pipeline(
        fetcher=fetcher, detail_payload=detail
    )
    video = _video("wechat_channels", video_id=video_id, export_id="export/unsafe-id")

    first = pipeline.fetch(video)
    second = pipeline.fetch(video)

    key = viral_media_key("wechat_channels", video_id, "video")
    assert first.cache_hit is False
    assert second.cache_hit is True
    assert "//" not in key
    assert key in storage.objects
    assert fetcher_ref.calls == ["http://wxapp.tc.qq.com/file"]
    assert detail_transport.last_body_count == 1


def test_concurrent_fetches_share_one_download_and_storage_write() -> None:
    class SlowFetcher(FakeFetcher):
        def __init__(self) -> None:
            super().__init__({"https://cdn.test/v1.mp4": b"\x00\x00\x00 ftypisom"})
            self._lock = Lock()

        def fetch(self, url: str) -> bytes:
            with self._lock:
                self.calls.append(url)
            time.sleep(0.08)
            return self.payloads[url]

    storage = FakeViralStorage()
    fetcher = SlowFetcher()
    first_pipeline, _, _, _ = _pipeline(storage=storage, fetcher=fetcher)
    second_pipeline, _, _, _ = _pipeline(storage=storage, fetcher=fetcher)
    video = _video("douyin")
    barrier = Barrier(2)

    def fetch(pipeline: ViralMediaPipeline):
        barrier.wait(timeout=5)
        return pipeline.fetch(video, prefer="video")

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(fetch, [first_pipeline, second_pipeline]))

    assert fetcher.calls == ["https://cdn.test/v1.mp4"]
    assert sorted(result.cache_hit for result in results) == [False, True]


def test_douyin_prefer_video_stores_mp4_even_with_audio() -> None:
    fetcher = FakeFetcher(
        {
            "https://cdn.test/v9.mp3": b"ID3-audio",
            "https://cdn.test/v9.mp4": b"\x00\x00\x00 ftypisom",
        }
    )
    pipeline, storage, _, _ = _pipeline(fetcher=fetcher)
    video = _video("douyin", video_id="v9")
    result = pipeline.fetch(video, prefer="video")
    assert result.kind == "video"
    assert "viral/douyin/v9.browser.mp4" in storage.objects
    assert "viral/douyin/v9.mp3" not in storage.objects
    # 默认（不传 prefer）仍音频优先。
    default = pipeline.fetch(video)
    assert default.kind == "audio"
    assert "viral/douyin/v9.mp3" in storage.objects


def test_wechat_retains_detail_statistics_for_database_feedback():
    detail = _detail_payload("http://wxapp.tc.qq.com/file", _DECODE_KEY)
    detail["data"].update(like_count=123, comment_count=0, forward_count=7, fav_count=8)
    pipeline, _, _, transport = _pipeline(
        detail_payload=detail,
        fetcher=FakeFetcher({"http://wxapp.tc.qq.com/file": b"\x00\x00\x00 ftypisom"}),
    )
    pipeline.fetch(_video("wechat_channels", export_id="export/feedback"))
    assert pipeline.detail.comment_count == 0
    assert pipeline.detail.forward_count == 7
    assert pipeline.detail.fav_count == 8
    assert transport.last_body_count == 1


def test_url_fetcher_rejects_non_public_targets() -> None:
    """上游返回的半可信 URL 不得驱动服务端 SSRF（2026-09-07 安全专项 P1）."""
    fetcher = UrlFetcher()
    for url in (
        "file:///etc/passwd",
        "http://169.254.169.254/latest/meta-data/",
        "http://127.0.0.1:8000/secret",
        "http://10.0.0.8/inner.mp4",
        "ftp://cdn.example.com/video.mp4",
    ):
        with pytest.raises(ViralMediaError):
            fetcher.fetch(url)


def test_url_fetcher_rejects_redirect_to_private_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def resolve(host: str, *args: Any, **kwargs: Any):
        ip = "93.184.216.34" if host == "cdn.example.com" else "127.0.0.1"
        return [(2, 1, 6, "", (ip, 443))]

    monkeypatch.setattr(viral_media.socket, "getaddrinfo", resolve)
    with pytest.raises(ViralMediaError, match="公网"):
        UrlFetcher(
            connection_factory=lambda *args: _PinnedConnection(
                _PinnedResponse(302, location="https://private.example/internal")
            )
        ).fetch("https://cdn.example.com/video.mp4")


class _PinnedResponse:
    def __init__(self, status: int, *, body: bytes = b"video", location: str | None = None):
        self.status = status
        self._body = body
        self._read = False
        self.headers = {"Content-Type": "video/mp4"}
        if location is not None:
            self.headers["Location"] = location

    def read(self, _size: int = -1) -> bytes:
        if self._read:
            return b""
        self._read = True
        return self._body


class _PinnedConnection:
    def __init__(self, response: _PinnedResponse):
        self.response = response
        self.requests: list[tuple[str, str, dict[str, str]]] = []

    def request(self, method: str, target: str, *, headers: dict[str, str]) -> None:
        self.requests.append((method, target, headers))

    def getresponse(self) -> _PinnedResponse:
        return self.response

    def close(self) -> None:
        pass


def test_url_fetcher_pins_validated_ip_against_dns_rebinding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dns_answers = iter(("93.184.216.34", "127.0.0.1"))
    dns_calls: list[str] = []

    def resolve(host: str, *args: Any, **kwargs: Any):
        dns_calls.append(host)
        ip = next(dns_answers)
        return [(2, 1, 6, "", (ip, 443))]

    connections: list[tuple[str, str, int, str]] = []

    def connect(scheme: str, host: str, port: int, ip: str, timeout: float):
        connections.append((scheme, host, port, ip))
        # A normal hostname connection would now resolve to private loopback.
        assert resolve(host)[0][4][0] == "127.0.0.1"
        return _PinnedConnection(_PinnedResponse(200))

    monkeypatch.setattr(viral_media.socket, "getaddrinfo", resolve)
    fetcher = UrlFetcher(connection_factory=connect)

    assert fetcher.fetch("https://media.example/video.mp4") == b"video"
    assert dns_calls == ["media.example", "media.example"]
    assert connections == [("https", "media.example", 443, "93.184.216.34")]


def test_url_fetcher_revalidates_and_pins_each_redirect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answers = {
        "first.example": "93.184.216.34",
        "second.example": "93.184.216.35",
    }

    def resolve(host: str, *args: Any, **kwargs: Any):
        return [(2, 1, 6, "", (answers[host], 443))]

    responses = iter(
        (
            _PinnedResponse(302, location="https://second.example/final.mp4"),
            _PinnedResponse(200, body=b"final"),
        )
    )
    connections: list[tuple[str, str]] = []

    def connect(scheme: str, host: str, port: int, ip: str, timeout: float):
        connections.append((host, ip))
        return _PinnedConnection(next(responses))

    monkeypatch.setattr(viral_media.socket, "getaddrinfo", resolve)
    fetcher = UrlFetcher(connection_factory=connect)

    assert fetcher.fetch("https://first.example/start.mp4") == b"final"
    assert connections == [
        ("first.example", "93.184.216.34"),
        ("second.example", "93.184.216.35"),
    ]


@pytest.mark.parametrize(
    "url",
    ("http://media.example:8080/video.mp4", "https://media.example:8443/video.mp4"),
)
def test_url_fetcher_rejects_nonstandard_initial_ports(
    url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        viral_media.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(2, 1, 6, "", ("93.184.216.34", 443))],
    )

    with pytest.raises(ViralMediaError, match="端口"):
        UrlFetcher(
            connection_factory=lambda *args: (_ for _ in ()).throw(
                AssertionError("nonstandard port must be rejected before connection")
            )
        ).fetch(url)


def test_url_fetcher_rejects_redirect_to_nonstandard_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        viral_media.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(2, 1, 6, "", ("93.184.216.34", 443))],
    )
    connections = 0

    def connect(*args: object):
        nonlocal connections
        connections += 1
        return _PinnedConnection(
            _PinnedResponse(302, location="https://second.example:8443/final.mp4")
        )

    with pytest.raises(ViralMediaError, match="端口"):
        UrlFetcher(connection_factory=connect).fetch("https://first.example/start.mp4")
    assert connections == 1


def test_url_fetcher_rejects_mixed_public_and_private_dns_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        viral_media.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (2, 1, 6, "", ("93.184.216.34", 443)),
            (2, 1, 6, "", ("127.0.0.1", 443)),
        ],
    )

    with pytest.raises(ViralMediaError, match="公网"):
        UrlFetcher(
            connection_factory=lambda *args: (_ for _ in ()).throw(
                AssertionError("private candidate must prevent connection")
            )
        ).fetch("https://mixed.example/video.mp4")


def test_default_pinned_connections_use_ip_but_keep_https_sni(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Socket:
        def getpeername(self):
            return ("93.184.216.34", 443)

        def close(self) -> None:
            pass

    class Context:
        def __init__(self) -> None:
            self.server_names: list[str] = []

        def wrap_socket(self, sock: Socket, *, server_hostname: str):
            self.server_names.append(server_hostname)
            return sock

    connected: list[tuple[tuple[str, int], float]] = []
    context = Context()

    def create_connection(target: tuple[str, int], timeout: float):
        connected.append((target, timeout))
        return Socket()

    monkeypatch.setattr(viral_media.socket, "create_connection", create_connection)
    monkeypatch.setattr(viral_media.ssl, "create_default_context", lambda: context)
    connection = viral_media._pinned_connection("https", "media.example", 443, "93.184.216.34", 4.0)

    connection.connect()

    assert connected == [(("93.184.216.34", 443), 4.0)]
    assert context.server_names == ["media.example"]
