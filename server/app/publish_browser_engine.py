"""QR selectors adapted from social-auto-upload; see publishers/vendor/social_auto_upload.

Use stock Playwright and platform-provided QR images. Never bypass verification.
Each login has a fresh, non-persistent browser context; only encrypted state survives.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from collections.abc import AsyncGenerator
from contextlib import aclosing
from dataclasses import dataclass
from typing import Any, cast
from urllib.parse import urlparse

from playwright.async_api import (
    BrowserContext,
    Frame,
    Locator,
    Page,
    Request,
    Response,
    StorageState,
    async_playwright,
)

from app.publish_browser import Platform

ORIGINS = {
    "douyin": "https://creator.douyin.com",
    "wechat_channels": "https://channels.weixin.qq.com",
    "xiaohongshu": "https://creator.xiaohongshu.com",
    "kuaishou": "https://cp.kuaishou.com",
}
ENDPOINTS = {
    "douyin": "/web/api/media/user/info/",
    "wechat_channels": "/auth/auth_data",
    "xiaohongshu": "/api/galaxy/user/info",
    "kuaishou": "/rest/v2/creator/pc/authority/account/current",
}
DOUYIN_IDENTITY_URL = "https://creator.douyin.com/web/api/media/user/info/?aid=1128"
DOUYIN_PROBE_INTERVAL_SECONDS = 3.0
KUAISHOU_LOGIN_ORIGIN = "https://passport.kuaishou.com"
KUAISHOU_LOGIN_PATHS = frozenset({"/pc/account/login", "/pc/account/login/"})
KUAISHOU_COOKIE_DOMAINS = frozenset({"kuaishou.com", "cp.kuaishou.com", "passport.kuaishou.com"})
KUAISHOU_STORAGE_ORIGINS = frozenset({ORIGINS["kuaishou"], KUAISHOU_LOGIN_ORIGIN})
KUAISHOU_PROBE_TIMEOUT_SECONDS = 25.0

# 视频号登录二维码只在 /platform 控制台路径下直接渲染；根路径落地页需交互才出码，
# 是网页版视频号取不到码的主因之一。抖音创作者后台根路径已能直接出码（测试通过），
# 小红书保持不变，避免回退已验证的路径。
LOGIN_PATHS = {
    "douyin": "",
    "wechat_channels": "/platform",
    "xiaohongshu": "",
    "kuaishou": "/article/publish/video",
}

# 无头 Chromium 默认暴露 navigator.webdriver 等自动化特征，微信风控据此可能不渲染
# 二维码或直接拦截。注入最小 stealth 脚本 + 常规 UA/语言，降低被识别为无头的概率。
CHROMIUM_ARGS = ("--disable-blink-features=AutomationControlled",)
STEALTH_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
STEALTH_INIT_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
window.chrome = window.chrome || { runtime: {} };
Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh'] });
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
"""

# 找不到二维码元素时的整页截图兜底：登录页稳定后（grace）按 interval 节流地把整页
# 当作可扫图像推给前端，用户直接扫页面里的码。三平台统一启用。
FULLPAGE_FALLBACK_GRACE_SECONDS = 6.0
FULLPAGE_FALLBACK_INTERVAL_SECONDS = 3.0

logger = logging.getLogger(__name__)


@dataclass
class BrowserEvent:
    phase: str
    image: str | None = None
    identity: dict[str, str] | None = None
    storage: dict[str, Any] | None = None


def parse_identity(platform: Platform, body: Any) -> dict[str, str] | None:
    if not isinstance(body, dict):
        return None
    try:
        status_code = body.get("status_code")
        if platform == "douyin" and (
            (type(status_code) is int and status_code == 0)
            or (type(status_code) is str and status_code == "0")
        ):
            user = body.get("user", {})
            uid, name = user.get("uid") or user.get("user_id"), user.get("nickname")
        elif platform == "wechat_channels" and body.get("errCode", body.get("errcode")) == 0:
            data = body.get("data", {})
            user = (
                data.get("finderUser")
                or data.get("userAttr")
                or (data.get("finderList") or [{}])[0]
            )
            uid, name = user.get("finderUsername"), user.get("nickname", user.get("nickName"))
        elif platform == "xiaohongshu" and (
            body.get("success") is True or ("success" not in body and body.get("code") == 0)
        ):
            user = body.get("data", {})
            uid, name = user.get("userId"), user.get("userName")
        elif platform == "kuaishou" and type(body.get("result")) is int and body["result"] == 1:
            # 官方 bootstrap 将 data 原样写入 user；显示名取 userName，不能拿快手号代替 UID。
            user = body.get("data", {})
            if user.get("logined") is False:
                return None
            uid, name = user.get("userId"), user.get("userName")
            if uid == 0 or (isinstance(uid, str) and uid.strip() == "0"):
                return None
            if isinstance(uid, int) and abs(uid) > 9_007_199_254_740_991:
                return None
        else:
            return None
        if isinstance(uid, bool) or not isinstance(uid, str | int) or not isinstance(name, str):
            return None
        if not str(uid).strip() or not name.strip() or len(str(uid)) > 256 or len(name) > 256:
            return None
        return {"platform_user_id": str(uid).strip(), "username": name.strip()}
    except (AttributeError, TypeError, IndexError):
        return None


class KuaishouIdentityUnavailableError(RuntimeError):
    """身份暂不可核实；调用方必须保留原有登录状态，不能据此标记失效。"""


def _origin(url: str) -> str:
    try:
        parsed = urlparse(url)
        return f"{parsed.scheme}://{parsed.netloc}"
    except ValueError:
        return ""


def is_kuaishou_login_page(url: str) -> bool:
    return _origin(url) == KUAISHOU_LOGIN_ORIGIN and urlparse(url).path in KUAISHOU_LOGIN_PATHS


def _is_kuaishou_identity_url(url: str) -> bool:
    return _origin(url) == ORIGINS["kuaishou"] and urlparse(url).path == ENDPOINTS["kuaishou"]


def filter_kuaishou_storage_state(storage: dict[str, Any]) -> dict[str, Any]:
    """只导出两处官方登录源及共同父域；不保留浏览器途经的其他站点状态。"""
    cookies, origins = storage.get("cookies", []), storage.get("origins", [])
    if not isinstance(cookies, list) or not isinstance(origins, list):
        raise ValueError("平台登录状态格式不正确")
    selected: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for cookie in cookies:
        if not isinstance(cookie, dict):
            continue
        domain = cookie.get("domain")
        name, value, path = cookie.get("name"), cookie.get("value"), cookie.get("path", "/")
        if (
            not isinstance(domain, str)
            or domain.removeprefix(".").lower() not in KUAISHOU_COOKIE_DOMAINS
            or not isinstance(name, str)
            or not isinstance(value, str)
            or not isinstance(path, str)
        ):
            continue
        key = (name, domain.lower(), path)
        if key not in seen:
            selected.append(dict(cookie))
            seen.add(key)
    return {
        "cookies": selected,
        "origins": [
            dict(entry)
            for entry in origins
            if isinstance(entry, dict)
            and isinstance(entry.get("origin"), str)
            and entry["origin"] in KUAISHOU_STORAGE_ORIGINS
        ],
    }


class KuaishouIdentityCapture:
    """只观察当前官方创作者页面自行发起的身份请求，导航即作废在途回执。"""

    def __init__(self, page: Page) -> None:
        self.page = page
        self.generation = 0
        self.navigation_generation = 0
        self.identity: dict[str, str] | None = None
        self._requests: dict[Request, int] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._changed = asyncio.Event()
        self._outcome: (
            tuple[int, dict[str, str] | None | KuaishouIdentityUnavailableError] | None
        ) = None
        page.on("request", self._on_request)
        page.on("response", self._on_response)
        page.on("framenavigated", self._on_navigation)

    def _current(self, generation: int) -> bool:
        return (
            generation == self.generation
            and not self.page.is_closed()
            and _origin(self.page.url) == ORIGINS["kuaishou"]
        )

    def _trusted_request(self, request: Request) -> bool:
        try:
            return (
                request.method == "POST"
                and _is_kuaishou_identity_url(request.url)
                and request.redirected_from is None
                and request.frame is self.page.main_frame
                and _origin(request.frame.url) == ORIGINS["kuaishou"]
                and self._current(self.generation)
            )
        except Exception:
            return False

    def _on_navigation(self, frame: Frame) -> None:
        self.navigation_generation += 1
        if frame is self.page.main_frame:
            self.reset_identity()

    def reset_identity(self) -> None:
        self.generation += 1
        self.identity = None
        self._requests.clear()
        self._outcome = None
        self._changed.clear()

    def _on_request(self, request: Request) -> None:
        if self._trusted_request(request):
            self._requests[request] = self.generation

    def _on_response(self, response: Response) -> None:
        generation = self._requests.pop(response.request, None)
        if generation is None or not self._current(generation):
            return
        task = asyncio.create_task(self._capture(response, generation))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _capture(self, response: Response, generation: int) -> None:
        if not self._trusted_request(response.request) or not _is_kuaishou_identity_url(
            response.url
        ):
            return
        outcome: dict[str, str] | None | KuaishouIdentityUnavailableError
        try:
            if not response.ok or not 200 <= response.status < 300:
                raise KuaishouIdentityUnavailableError("平台身份暂时无法核实，请稍后重试")
            body = await response.json()
            identity = parse_identity("kuaishou", body)
            if identity is not None:
                outcome = identity
            elif (
                isinstance(body, dict)
                and type(body.get("result")) is int
                and (
                    body["result"] == 109
                    or (
                        body["result"] == 1
                        and isinstance(body.get("data"), dict)
                        and body["data"].get("logined") is False
                    )
                )
            ):
                outcome = None
            else:
                raise KuaishouIdentityUnavailableError("平台身份响应暂时无法识别，请稍后重试")
        except KuaishouIdentityUnavailableError as exc:
            outcome = exc
        except Exception:
            outcome = KuaishouIdentityUnavailableError("平台身份暂时无法核实，请稍后重试")
        if not self._current(generation):
            return
        self.identity = outcome if isinstance(outcome, dict) else None
        self._outcome = (generation, outcome)
        self._changed.set()

    async def result(self) -> dict[str, str] | None:
        while True:
            await self._changed.wait()
            self._changed.clear()
            if self._outcome is None or not self._current(self._outcome[0]):
                continue
            outcome = self._outcome[1]
            if isinstance(outcome, KuaishouIdentityUnavailableError):
                raise outcome
            return outcome

    async def close(self) -> None:
        self.page.remove_listener("request", self._on_request)
        self.page.remove_listener("response", self._on_response)
        self.page.remove_listener("framenavigated", self._on_navigation)
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)


def _is_douyin_creator_page(url: str) -> bool:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}" == ORIGINS["douyin"] and (
        parsed.path == "/creator-micro" or parsed.path.startswith("/creator-micro/")
    )


async def _probe_douyin_identity(context: BrowserContext) -> dict[str, str] | None:
    response = None
    try:
        response = await context.request.get(
            DOUYIN_IDENTITY_URL,
            timeout=5000,
            max_redirects=0,
        )
        if not response.ok:
            logger.warning(
                "douyin identity probe rejected at creator backend: http_status=%s",
                response.status,
            )
            return None
        return parse_identity("douyin", await response.json())
    except Exception as exc:
        logger.warning("douyin identity probe failed at creator backend: %s", type(exc).__name__)
        return None
    finally:
        if response is not None:
            try:
                await response.dispose()
            except Exception as exc:
                logger.warning(
                    "douyin identity probe cleanup failed at creator backend: %s",
                    type(exc).__name__,
                )


async def qr_locator(page: Page, platform: Platform) -> Locator | None:
    # Selectors and XHS scan-panel switch ported from pinned MIT upstream.
    candidates: list[Locator] = []
    if platform == "douyin":
        candidates = [
            page.locator(selector).first
            for selector in (
                'div#animate_qrcode_container img[src^="data:image"]',
                'div[class*="animate_qrcode_container"] img[src^="data:image"]',
                'div[class*="scan_qrcode_login_content"] img[src^="data:image"]',
                'img[aria-label="二维码"]',
            )
        ]
    elif platform == "xiaohongshu":
        box = page.locator("div[class*='login-box']").first
        if await box.count() and not await box.get_by_text("扫一扫", exact=False).count():
            toggle = box.locator("img.css-wemwzq").first
            if await toggle.is_visible():
                await toggle.click(timeout=1500)
        candidates = [
            page.locator(".login-box-container")
            .get_by_text("APP扫一扫登录")
            .first.locator("xpath=..//following-sibling::div//img")
            .first,
            box.locator('img[src^="data:image"]').first,
        ]
    elif platform == "kuaishou":
        if _origin(page.url) != ORIGINS["kuaishou"] and not is_kuaishou_login_page(page.url):
            return None
        for frame in page.frames:
            if not is_kuaishou_login_page(frame.url):
                continue
            # 固定上游只在此登录区域读码，二维码面板已可见时不反复切换。
            region = frame.locator("main#login-form div.qr-login").first
            if not await region.is_visible():
                toggle = frame.locator(
                    "main#login-form div.platform-switch.platform-switch-pc"
                ).first
                if await toggle.is_visible():
                    await toggle.click(timeout=1500)
            candidates.append(frame.locator('main#login-form div.qr-login img[alt="qrcode"]').first)
    else:
        # Both legacy and July-2026 qrconnect frames; screenshot resolves relative URLs.
        for frame in page.frames:
            url = urlparse(frame.url)
            if (
                url.scheme == "https"
                and url.netloc == "open.weixin.qq.com"
                and url.path == "/connect/qrconnect"
            ) or (
                url.scheme == "https"
                and url.netloc == "channels.weixin.qq.com"
                and "login-for-iframe" in url.path
            ):
                candidates.extend(await frame.locator("img.qrcode, img.js_qrcode_img").all())
        candidates.extend(
            page.locator(selector).first
            for selector in (
                "div.login-qrcode-wrap img.qrcode",
                "div.qrcode-wrap img.qrcode",
                "img.qrcode",
                "img.js_qrcode_img",
            )
        )
    for candidate in candidates:
        if await candidate.is_visible():
            bounds = await candidate.bounding_box()
            if bounds and bounds["width"] >= 100 and bounds["height"] >= 100:
                return candidate
    return None


async def _kuaishou_login_events() -> AsyncGenerator[BrowserEvent, None]:
    # 新浏览器上下文只展示官方二维码，不加载既存登录态或截图整个页面。
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            context = await browser.new_context(
                viewport={"width": 1080, "height": 780}, locale="zh-CN"
            )
            page = await context.new_page()
            capture = KuaishouIdentityCapture(page)
            try:
                await page.goto(
                    ORIGINS["kuaishou"] + LOGIN_PATHS["kuaishou"],
                    wait_until="commit",
                    timeout=45000,
                )
                deadline = time.monotonic() + 280
                qr_shown = login_opened = False
                while time.monotonic() < deadline:
                    identity = capture.identity
                    if qr_shown and identity is not None:
                        generation = capture.generation
                        state = filter_kuaishou_storage_state(dict(await context.storage_state()))
                        # 身份回执与导出的登录态必须来自同一官方文档。
                        if generation == capture.generation and capture.identity is identity:
                            if any(
                                cookie["domain"].removeprefix(".").lower()
                                in {"cp.kuaishou.com", "kuaishou.com"}
                                for cookie in state["cookies"]
                            ):
                                yield BrowserEvent("connected", identity=identity, storage=state)
                                return
                            yield BrowserEvent("action_required")
                            await asyncio.sleep(1.5)
                            continue

                    creator = _origin(page.url) == ORIGINS["kuaishou"]
                    generation = capture.navigation_generation
                    event = BrowserEvent("confirming")
                    try:
                        if not creator and not is_kuaishou_login_page(page.url):
                            event = BrowserEvent("action_required")
                        elif (phase := await _kuaishou_qr_phase(page)) is not None:
                            event = BrowserEvent(phase)
                        elif (qr := await qr_locator(page, "kuaishou")) is not None:
                            png = await qr.screenshot(type="png", timeout=3000)
                            image = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
                            event = (
                                BrowserEvent("qr_ready", image=image)
                                if len(image) <= 600000
                                else BrowserEvent("action_required")
                            )
                        elif creator and not (qr_shown or login_opened):
                            button = page.get_by_role("button", name="立即登录", exact=True).first
                            if (
                                await button.is_visible()
                                and generation == capture.navigation_generation
                            ):
                                login_opened = True
                                await button.click(timeout=1500)
                    except Exception:
                        event = BrowserEvent("action_required")
                    if generation != capture.navigation_generation:
                        event = BrowserEvent("action_required")
                    if event.phase == "qr_ready" and not qr_shown:
                        capture.reset_identity()
                        qr_shown = True
                    yield event
                    if event.phase == "expired":
                        return
                    await asyncio.sleep(1.5)
                yield BrowserEvent("expired")
            finally:
                await capture.close()
        finally:
            await browser.close()


async def _kuaishou_qr_phase(page: Page) -> str | None:
    for frame in page.frames:
        if not is_kuaishou_login_page(frame.url):
            continue
        if await frame.locator("div.qrcode-status.qrcode-status-timeout").first.is_visible():
            return "expired"
        if await frame.locator("div.qrcode-status.qrcode-status-confirm").first.is_visible():
            return "confirming"
    return None


async def login_events(
    platform: Platform,
    storage: dict[str, Any] | None = None,
) -> AsyncGenerator[BrowserEvent, None]:
    if platform == "kuaishou":
        async with aclosing(_kuaishou_login_events()) as events:
            async for event in events:
                yield event
        return

    identity: dict[str, str] | None = None
    last_douyin_probe_at: float | None = None
    tasks: set[asyncio.Task[None]] = set()
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True, args=list(CHROMIUM_ARGS))
        try:
            context = await browser.new_context(
                storage_state=cast(StorageState | None, storage),
                viewport={"width": 1080, "height": 780},
                user_agent=STEALTH_USER_AGENT,
                locale="zh-CN",
            )
            await context.add_init_script(STEALTH_INIT_SCRIPT)
            page = await context.new_page()

            async def capture(response: Response) -> None:
                nonlocal identity
                parsed = urlparse(response.url)
                if f"{parsed.scheme}://{parsed.netloc}" != ORIGINS[
                    platform
                ] or not parsed.path.endswith(ENDPOINTS[platform]):
                    return
                try:
                    parsed_identity = parse_identity(platform, await response.json())
                    if platform != "douyin" or parsed_identity is not None:
                        identity = parsed_identity
                except Exception:
                    pass

            def on_response(response: Response) -> None:
                task = asyncio.create_task(capture(response))
                tasks.add(task)
                task.add_done_callback(tasks.discard)

            page.on("response", on_response)
            # The login shell may keep loading optional scripts while its QR is ready.
            # Start observing after the response commits; each locator tolerates loading DOM.
            await page.goto(
                ORIGINS[platform] + LOGIN_PATHS[platform], wait_until="commit", timeout=45000
            )
            started_at = time.monotonic()
            last_fallback_at: float | None = None
            deadline = started_at + 280
            while time.monotonic() < deadline:
                on_douyin_creator_page = platform == "douyin" and _is_douyin_creator_page(page.url)
                now = time.monotonic()
                if (
                    on_douyin_creator_page
                    and identity is None
                    and (
                        last_douyin_probe_at is None
                        or now - last_douyin_probe_at >= DOUYIN_PROBE_INTERVAL_SECONDS
                    )
                ):
                    last_douyin_probe_at = now
                    probed_identity = await _probe_douyin_identity(context)
                    if probed_identity is not None:
                        identity = probed_identity
                current = urlparse(page.url)
                on_douyin_creator_page = platform == "douyin" and _is_douyin_creator_page(page.url)
                if (
                    identity is not None
                    and f"{current.scheme}://{current.netloc}" == ORIGINS[platform]
                ):
                    state = dict(await context.storage_state(indexed_db=True))
                    yield BrowserEvent("connected", identity=identity, storage=state)
                    return
                if on_douyin_creator_page:
                    yield BrowserEvent("confirming")
                    await asyncio.sleep(1.5)
                    continue
                try:
                    for frame in page.frames:
                        parsed = urlparse(frame.url)
                        if parsed.netloc not in {
                            urlparse(ORIGINS[platform]).netloc,
                            "open.weixin.qq.com",
                        }:
                            continue
                        expired = (
                            frame.get_by_text("二维码已过期", exact=False)
                            .or_(frame.get_by_text("二维码已失效", exact=False))
                            .first
                        )
                        if await expired.is_visible():
                            yield BrowserEvent("expired")
                            return
                    qr = await qr_locator(page, platform)
                    if qr is not None:
                        png = await qr.screenshot(type="png", timeout=3000)
                        image = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
                        yield (
                            BrowserEvent("qr_ready", image=image)
                            if len(image) <= 600000
                            else BrowserEvent("action_required")
                        )
                    else:
                        fallback_due = now - started_at >= FULLPAGE_FALLBACK_GRACE_SECONDS and (
                            last_fallback_at is None
                            or now - last_fallback_at >= FULLPAGE_FALLBACK_INTERVAL_SECONDS
                        )
                        if fallback_due:
                            last_fallback_at = now
                            png = await page.screenshot(type="png", timeout=5000)
                            image = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
                            yield (
                                BrowserEvent("qr_ready", image=image)
                                if len(image) <= 600000
                                else BrowserEvent("action_required")
                            )
                        else:
                            yield BrowserEvent("loading")
                except Exception:
                    yield BrowserEvent("action_required")
                await asyncio.sleep(1.5)
            if platform == "douyin":
                logger.warning(
                    "douyin login expired: creator_backend=%s identity_captured=%s",
                    _is_douyin_creator_page(page.url),
                    identity is not None,
                )
            yield BrowserEvent("expired")
        finally:
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            await browser.close()
