"""快手登录的离线合同；官方页面/请求为替身，不登录、不访问网络或数据库。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from copy import deepcopy
from typing import Any, cast

import pytest
from playwright.async_api import BrowserContext, Page

from app import publish_browser_engine as engine

CREATOR = "https://cp.kuaishou.com"
LOGIN = "https://passport.kuaishou.com/pc/account/login/"
ENDPOINT = "/rest/v2/creator/pc/authority/account/current"
IDENTITY_URL = CREATOR + ENDPOINT
PUBLISH_URL = CREATOR + "/article/publish/video"
QR_REGION = "main#login-form div.qr-login"
QR_IMAGE = 'main#login-form div.qr-login img[alt="qrcode"]'
QR_SWITCH = "main#login-form div.platform-switch.platform-switch-pc"
EXPIRED = "div.qrcode-status.qrcode-status-timeout"
CONFIRMING = "div.qrcode-status.qrcode-status-confirm"
IDENTITY = {
    "result": 1,
    "data": {
        "userId": 123456,
        "userName": "快手昵称",
        "userKwaiId": "不可替代UID",
        "userAvatar": "https://p3.a.kwimgs.com/synthetic-avatar.jpeg",
        "logined": True,
        "session": "synthetic-private-value",
    },
}
EXPECTED = {
    "platform_user_id": "123456",
    "username": "快手昵称",
    "avatar_url": "https://p3.a.kwimgs.com/synthetic-avatar.jpeg",
}


async def _drain() -> None:
    await asyncio.tasks.sleep(0)
    await asyncio.tasks.sleep(0)


class _Locator:
    def __init__(self, frame: _Frame, selector: str) -> None:
        self.frame, self.selector = frame, selector

    @property
    def first(self) -> _Locator:
        return self

    async def is_visible(self) -> bool:
        return self.selector in self.frame.visible

    async def bounding_box(self) -> dict[str, float] | None:
        return {"width": 200, "height": 200} if await self.is_visible() else None

    async def click(self, **_: Any) -> None:
        self.frame.clicks.append(self.selector)
        self.frame.visible.update({QR_REGION, QR_IMAGE})

    async def screenshot(self, **_: Any) -> bytes:
        self.frame.page.screenshots += 1
        if self.frame.page.image_hook:
            self.frame.page.image_hook()
        return b"ks-qr"


class _Frame:
    def __init__(self, page: _Page, url: str | None = None) -> None:
        self.page, self._url = page, url
        self.visible: set[str] = set()
        self.selectors: list[str] = []
        self.clicks: list[str] = []

    @property
    def url(self) -> str:
        return self.page.url if self._url is None else self._url

    def locator(self, selector: str) -> _Locator:
        self.selectors.append(selector)
        return _Locator(self, selector)


class _Request:
    def __init__(
        self,
        page: _Page,
        *,
        url: str = IDENTITY_URL,
        method: str = "POST",
        frame: _Frame | None = None,
        redirected: bool = False,
    ) -> None:
        self.url, self.method = url, method
        self.frame = frame or page.main_frame
        self.redirected_from = object() if redirected else None


class _Response:
    def __init__(
        self,
        request: _Request,
        body: Any,
        *,
        url: str | None = None,
        status: int = 200,
        on_json: Callable[[], Awaitable[Any]] | None = None,
    ) -> None:
        self.request, self.body = request, body
        self.url = url or request.url
        self.status, self.ok = status, 200 <= status < 300
        self.on_json = on_json
        self.json_calls = 0

    async def json(self) -> Any:
        self.json_calls += 1
        return await self.on_json() if self.on_json else self.body


class _Page:
    def __init__(self, after_goto: str = PUBLISH_URL) -> None:
        self.url, self.after_goto = "about:blank", after_goto
        self.main_frame = _Frame(self)
        self.frames = [self.main_frame]
        self.handlers: dict[str, list[Callable[..., Any]]] = {}
        self.on_goto: Callable[[_Page], Awaitable[None]] | None = None
        self.goto_error: Exception | None = None
        self.goto_calls: list[dict[str, Any]] = []
        self.closed = False
        self.screenshots = 0
        self.image_hook: Callable[[], None] | None = None

    def on(self, event: str, handler: Callable[..., Any]) -> None:
        self.handlers.setdefault(event, []).append(handler)

    def remove_listener(self, event: str, handler: Callable[..., Any]) -> None:
        self.handlers[event].remove(handler)

    def emit(self, event: str, value: Any) -> None:
        for handler in list(self.handlers.get(event, [])):
            handler(value)

    def navigate(self, url: str) -> None:
        self.url = url
        self.emit("framenavigated", self.main_frame)

    async def goto(self, url: str, **options: Any) -> None:
        self.goto_calls.append({"url": url, **options})
        if self.goto_error:
            raise self.goto_error
        self.navigate(self.after_goto)
        if self.on_goto:
            await self.on_goto(self)

    def is_closed(self) -> bool:
        return self.closed

    async def close(self) -> None:
        self.closed = True

    async def screenshot(self, **_: Any) -> bytes:
        self.screenshots += 1
        if self.image_hook:
            self.image_hook()
        return b"official-login-page"

    async def identity(
        self,
        body: Any,
        *,
        request: _Request | None = None,
        record_request: bool = True,
        **kwargs: Any,
    ) -> _Response:
        request = request or _Request(self)
        if record_request:
            self.emit("request", request)
        response = _Response(request, body, **kwargs)
        self.emit("response", response)
        await _drain()
        return response


class _Context:
    def __init__(self, page: _Page) -> None:
        self.page = page
        self.scripts: list[str] = []
        self.storage_hook: Callable[[], None] | None = None
        self.storage = {
            "cookies": [
                {"name": "sid", "value": "synthetic", "domain": ".kuaishou.com", "path": "/"},
                {"name": "other", "value": "unrelated", "domain": ".example.test", "path": "/"},
            ],
            "origins": [
                {"origin": CREATOR, "localStorage": []},
                {"origin": "https://passport.kuaishou.com", "localStorage": []},
                {"origin": "https://example.test", "localStorage": []},
            ],
        }

    async def new_page(self) -> _Page:
        return self.page

    async def add_init_script(self, script: str) -> None:
        self.scripts.append(script)

    async def storage_state(self, **_: Any) -> dict[str, Any]:
        if self.storage_hook:
            self.storage_hook()
        return deepcopy(self.storage)


class _Browser:
    def __init__(self, context: _Context) -> None:
        self.context, self.closed = context, False
        self.options: dict[str, Any] = {}

    async def new_context(self, **kwargs: Any) -> _Context:
        self.options = kwargs
        return self.context

    async def close(self) -> None:
        self.closed = True


class _Playwright:
    def __init__(self, browser: _Browser) -> None:
        self.browser, self.chromium = browser, self

    async def launch(self, **_: Any) -> _Browser:
        return self.browser

    async def __aenter__(self) -> _Playwright:
        return self

    async def __aexit__(self, *_: Any) -> None:
        pass


def _install_login(monkeypatch: pytest.MonkeyPatch, page: _Page) -> _Browser:
    browser = _Browser(_Context(page))
    clock = [100.0]
    monkeypatch.setattr(engine, "async_playwright", lambda: _Playwright(browser))
    monkeypatch.setattr(engine.time, "monotonic", lambda: clock[0])

    async def advance(seconds: float) -> None:
        clock[0] += seconds
        await _drain()

    monkeypatch.setattr(engine.asyncio, "sleep", advance)
    return browser


def test_exact_kuaishou_login_and_identity_contract() -> None:
    assert engine.ORIGINS["kuaishou"] == CREATOR
    assert engine.ENDPOINTS["kuaishou"] == ENDPOINT
    assert engine.LOGIN_PATHS["kuaishou"] == "/article/publish/video"
    assert engine.parse_identity("kuaishou", IDENTITY) == EXPECTED
    assert "synthetic-private-value" not in str(engine.parse_identity("kuaishou", IDENTITY))


@pytest.mark.parametrize("uid", [0, "0", False, True, " ", None, [], 9_007_199_254_740_992])
def test_default_or_forged_uid_is_not_an_identity(uid: Any) -> None:
    body = deepcopy(IDENTITY)
    body["data"]["userId"] = uid
    assert engine.parse_identity("kuaishou", body) is None


@pytest.mark.parametrize(
    "body",
    [
        {"result": "1", "data": IDENTITY["data"]},
        {"result": True, "data": IDENTITY["data"]},
        {"result": 109, "data": IDENTITY["data"]},
        {"result": 1, "data": {"userKwaiId": "forged", "nickname": "其他字段"}},
        {"result": 1, "data": {"userId": "uid", "userName": " "}},
        {"result": 1, "data": {"userId": "uid", "userName": "a" * 257}},
        {"result": 1, "data": None},
        {"result": 1, "data": {"userId": "uid", "userName": "昵称", "logined": False}},
        [],
    ],
)
def test_rejects_unknown_and_unauthenticated_identity(body: Any) -> None:
    assert engine.parse_identity("kuaishou", body) is None


@pytest.mark.parametrize(
    "body", [IDENTITY, {"result": 109}, {"result": 1, "data": {"logined": False}}]
)
async def test_probe_observes_real_bootstrap_post_and_closes_only_temporary_page(body: Any) -> None:
    page = _Page()

    async def bootstrap(current: _Page) -> None:
        await current.identity(body)

    page.on_goto = bootstrap
    context = _Context(page)
    result = await engine.probe_kuaishou_identity(cast(BrowserContext, context))
    assert result == (EXPECTED if body is IDENTITY else None)
    assert page.goto_calls == [{"url": PUBLISH_URL, "wait_until": "commit", "timeout": 20000}]
    assert page.closed
    assert all(not handlers for handlers in page.handlers.values())


@pytest.mark.parametrize(
    "body",
    [
        {"result": 0},
        {"result": "109"},
        {"result": "1", "data": IDENTITY["data"]},
        {"result": True, "data": IDENTITY["data"]},
        {"result": 1, "data": {"userId": 0, "userName": "默认昵称"}},
        {"result": 1, "data": {"userId": "uid", "userName": ""}},
        {"result": 1, "data": {"organizationList": [{"id": "org"}]}},
        {"result": 1},
        [],
    ],
)
async def test_probe_unknown_is_unavailable_instead_of_invalid(body: Any) -> None:
    page = _Page()

    async def bootstrap(current: _Page) -> None:
        await current.identity(body)

    page.on_goto = bootstrap
    with pytest.raises(engine.KuaishouIdentityUnavailableError):
        await engine.probe_kuaishou_identity(cast(BrowserContext, _Context(page)))
    assert page.closed


@pytest.mark.parametrize("status", [302, 401, 403, 429, 500, 503])
async def test_probe_http_failure_does_not_mean_logged_out(status: int) -> None:
    page = _Page()

    async def bootstrap(current: _Page) -> None:
        response = await current.identity(IDENTITY, status=status)
        assert response.json_calls == 0

    page.on_goto = bootstrap
    with pytest.raises(engine.KuaishouIdentityUnavailableError):
        await engine.probe_kuaishou_identity(cast(BrowserContext, _Context(page)))
    assert page.closed


@pytest.mark.parametrize(
    "case",
    ["get", "request_origin", "response_origin", "suffix", "redirect", "child", "unobserved"],
)
async def test_probe_requires_both_official_sources_and_exact_post(
    monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    monkeypatch.setattr(engine, "KUAISHOU_PROBE_TIMEOUT_SECONDS", 0.02)
    page = _Page()

    async def bootstrap(current: _Page) -> None:
        request = _Request(
            current,
            method="GET" if case == "get" else "POST",
            url=(
                "https://evil.test" + ENDPOINT
                if case == "request_origin"
                else CREATOR + "/prefix" + ENDPOINT
                if case == "suffix"
                else IDENTITY_URL
            ),
            redirected=case == "redirect",
            frame=_Frame(current, CREATOR + "/embedded") if case == "child" else None,
        )
        response = await current.identity(
            IDENTITY,
            request=request,
            record_request=case != "unobserved",
            url="https://evil.test" + ENDPOINT if case == "response_origin" else IDENTITY_URL,
        )
        assert response.json_calls == 0

    page.on_goto = bootstrap
    with pytest.raises(engine.KuaishouIdentityUnavailableError):
        await engine.probe_kuaishou_identity(cast(BrowserContext, _Context(page)))
    assert page.closed


@pytest.mark.parametrize("moment", ["before_response", "during_json", "after_receipt"])
async def test_navigation_away_and_back_invalidates_late_bootstrap(
    monkeypatch: pytest.MonkeyPatch, moment: str
) -> None:
    monkeypatch.setattr(engine, "KUAISHOU_PROBE_TIMEOUT_SECONDS", 0.02)
    page = _Page()

    def navigate() -> None:
        page.navigate("https://evil.test/")
        page.navigate(PUBLISH_URL)

    async def bootstrap(current: _Page) -> None:
        if moment == "before_response":
            request = _Request(current)
            current.emit("request", request)
            navigate()
            await current.identity(IDENTITY, request=request, record_request=False)
        elif moment == "during_json":

            async def delayed() -> Any:
                navigate()
                return IDENTITY

            await current.identity(IDENTITY, on_json=delayed)
        else:
            await current.identity(IDENTITY)
            navigate()

    page.on_goto = bootstrap
    with pytest.raises(engine.KuaishouIdentityUnavailableError):
        await engine.probe_kuaishou_identity(cast(BrowserContext, _Context(page)))
    assert page.closed


async def test_probe_network_error_is_redacted_and_cancel_also_cleans_up() -> None:
    page = _Page()
    page.goto_error = RuntimeError("synthetic-private-network-detail")
    with pytest.raises(engine.KuaishouIdentityUnavailableError) as caught:
        await engine.probe_kuaishou_identity(cast(BrowserContext, _Context(page)))
    assert "synthetic-private-network-detail" not in str(caught.value)
    assert page.closed
    pending_page = _Page()
    task = asyncio.create_task(
        engine.probe_kuaishou_identity(cast(BrowserContext, _Context(pending_page)))
    )
    await _drain()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert pending_page.closed


def test_storage_filters_exact_domains_and_origins_and_deduplicates_cookie_paths() -> None:
    def cookie(domain: str, path: str = "/") -> dict[str, str]:
        return {"name": "sid", "value": "synthetic", "domain": domain, "path": path}

    state = {
        "cookies": [
            cookie(".kuaishou.com"),
            cookie(".kuaishou.com"),
            cookie(".kuaishou.com", "/rest"),
            cookie("cp.kuaishou.com"),
            cookie("passport.kuaishou.com"),
            cookie("foo.kuaishou.com"),
            cookie("..kuaishou.com"),
            cookie("kuaishou.com.evil.test"),
            cookie(".example.test"),
        ],
        "origins": [
            {"origin": CREATOR, "localStorage": [{"name": "a", "value": "synthetic"}]},
            {"origin": "https://passport.kuaishou.com", "localStorage": []},
            {"origin": "https://foo.kuaishou.com", "localStorage": []},
            {"origin": "http://cp.kuaishou.com", "localStorage": []},
            {"origin": CREATOR + "/", "localStorage": []},
            {"origin": [], "localStorage": []},
        ],
        "extra": "not-exported",
    }
    original = deepcopy(state)
    filtered = engine.filter_kuaishou_storage_state(state)
    assert len(filtered["cookies"]) == 4
    assert [entry["origin"] for entry in filtered["origins"]] == [
        CREATOR,
        "https://passport.kuaishou.com",
    ]
    assert set(filtered) == {"cookies", "origins"}
    assert state == original


async def test_login_connects_only_with_official_identity_and_filters_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = _Page()

    async def bootstrap(current: _Page) -> None:
        await current.identity(IDENTITY)

    page.on_goto = bootstrap
    browser = _install_login(monkeypatch, page)
    stream = engine.login_events("kuaishou", storage=browser.context.storage)
    try:
        event = await anext(stream)
    finally:
        await stream.aclose()
    assert event.phase == "connected" and event.identity == EXPECTED
    assert event.storage == engine.filter_kuaishou_storage_state(browser.context.storage)
    assert browser.options["storage_state"] == event.storage
    assert browser.closed


async def test_cookie_export_navigation_cannot_pair_old_uid_with_new_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = _Page()

    async def bootstrap(current: _Page) -> None:
        await current.identity(IDENTITY)

    page.on_goto = bootstrap
    browser = _install_login(monkeypatch, page)
    browser.context.storage_hook = lambda: page.navigate(PUBLISH_URL)
    stream = engine.login_events("kuaishou")
    try:
        event = await anext(stream)
    finally:
        await stream.aclose()
    assert event.phase == "confirming" and event.identity is None
    assert browser.closed


@pytest.mark.parametrize(
    "status,phase", [(None, "qr_ready"), (EXPIRED, "expired"), (CONFIRMING, "confirming")]
)
async def test_passport_qr_and_status_do_not_confirm_identity(
    monkeypatch: pytest.MonkeyPatch, status: str | None, phase: str
) -> None:
    page = _Page(LOGIN)
    page.main_frame.visible.update({QR_REGION, QR_IMAGE})
    if status:
        page.main_frame.visible.add(status)

    async def forged_bootstrap(current: _Page) -> None:
        await current.identity(IDENTITY)

    page.on_goto = forged_bootstrap
    browser = _install_login(monkeypatch, page)
    stream = engine.login_events("kuaishou")
    try:
        event = await anext(stream)
    finally:
        await stream.aclose()
    assert event.phase == phase and event.identity is None and event.storage is None
    assert (event.image is not None) == (phase == "qr_ready")
    assert browser.closed


@pytest.mark.parametrize(
    "url",
    [
        "https://passport.kuaishou.com/pc/account/login/other",
        "https://passport.kuaishou.com.evil.test/pc/account/login/",
        "https://passport.kuaishou.com:8443/pc/account/login/",
        "http://passport.kuaishou.com/pc/account/login/",
        "https://sub.cp.kuaishou.com/article/publish/video",
    ],
)
async def test_foreign_or_unproven_login_pages_never_emit_qr(
    monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    page = _Page(url)
    page.main_frame.visible.update({QR_REGION, QR_IMAGE})
    browser = _install_login(monkeypatch, page)
    stream = engine.login_events("kuaishou")
    try:
        event = await anext(stream)
    finally:
        await stream.aclose()
    assert event.phase == "action_required" and event.image is None
    assert page.screenshots == 0 and browser.closed


async def test_creator_upload_page_without_bootstrap_never_becomes_qr_or_connected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = _Page()
    browser = _install_login(monkeypatch, page)
    stream = engine.login_events("kuaishou")
    try:
        events = [await anext(stream) for _ in range(6)]
    finally:
        await stream.aclose()
    assert all(event.phase == "confirming" and event.image is None for event in events)
    assert page.screenshots == 0 and browser.closed


async def test_qr_switches_only_pc_mode_and_skips_untrusted_frames() -> None:
    page = _Page()
    page.navigate(CREATOR)
    foreign = _Frame(page, "https://passport.kuaishou.com/pc/account/login/other")
    official = _Frame(page, LOGIN)
    official.visible.add(QR_SWITCH)
    page.frames.extend([foreign, official])
    first = await engine.qr_locator(cast(Page, page), "kuaishou")
    second = await engine.qr_locator(cast(Page, page), "kuaishou")
    assert first is not None and second is not None
    assert official.clicks == [QR_SWITCH]
    assert foreign.selectors == []


async def test_navigation_during_qr_capture_drops_the_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = _Page(LOGIN)
    page.main_frame.visible.update({QR_REGION, QR_IMAGE})
    page.image_hook = lambda: page.navigate("https://evil.test/")
    browser = _install_login(monkeypatch, page)
    stream = engine.login_events("kuaishou")
    try:
        event = await anext(stream)
    finally:
        await stream.aclose()
    assert event.phase == "action_required" and event.image is None
    assert browser.closed
