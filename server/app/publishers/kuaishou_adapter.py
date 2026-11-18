"""快手视频发布：将固定 MIT 上游的控件流程接入本项目的隔离浏览器。

Adapted from dreammis/social-auto-upload, uploader/ks_uploader/main.py at
0012d2c355f88f683cc38dde2a2db209e14091bc, Copyright (c) 2023 dreammis.
MIT license: see vendor/social_auto_upload/LICENSE (distributed with this module).

只复用有来源的上传、描述、封面与确认控件。登录态只在内存中传入标准 Playwright。
发布任务在实际操作前提交本地标记，随后只执行一次即时发布。
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import parse_qs, urlparse

from .base import PublisherAuthError, PublishResult

if TYPE_CHECKING:
    from playwright.async_api import BrowserContext, Page, Request, Response, StorageState

    from app.publish_browser_engine import KuaishouIdentityCapture

logger = logging.getLogger(__name__)

PLATFORM = "kuaishou"
UPLOAD_URL = "https://cp.kuaishou.com/article/publish/video"
UPLOAD_BUTTON = "button[class^='_upload-btn']"
PUBLISH_CONFIRM = "div.ant-modal-confirm-centered:visible"
SUBMIT_PATH = "/rest/cp/works/v2/video/pc/submit"
# 整轮限制小于 900 秒发布租约，为资源关闭与结果落库留出余量。
DELIVERY_TIMEOUT_SECONDS = 600
PROBE_TIMEOUT_SECONDS = 60
UPLOAD_TIMEOUT_SECONDS = 480
RECEIPT_TIMEOUT_SECONDS = 60
MAX_TAGS = 3

_UNKNOWN_MESSAGE = "快手发布结果无法确认，请先到快手创作者平台核对作品。"
_UNAVAILABLE_MESSAGE = "暂时无法确认快手账号状态，请稍后重试"


@dataclass
class _Attempt:
    submission_started: bool = False
    result: PublishResult | None = None


def _prepare_storage_state(storage_state: Any) -> dict[str, Any]:
    # 与扫码流程共用域过滤；发布前再检查完整 Playwright 结构，避免无效材料启动浏览器。
    from app.publish_browser_engine import filter_kuaishou_storage_state

    if not isinstance(storage_state, dict) or set(storage_state) - {"cookies", "origins"}:
        raise ValueError("平台登录状态格式不正确")
    state = filter_kuaishou_storage_state(storage_state)
    cookies = state["cookies"]
    if not cookies:
        raise ValueError("平台登录状态为空")
    for cookie in cookies:
        # 平台可能导出空值的辅助 Cookie，实际登录身份仍由创作者页面确认。
        if (
            not cookie.get("name")
            or not isinstance(cookie.get("value"), str)
            or not str(cookie.get("path", "")).startswith("/")
            or not isinstance(cookie.get("expires"), int | float)
            or isinstance(cookie.get("expires"), bool)
            or not isinstance(cookie.get("httpOnly"), bool)
            or not isinstance(cookie.get("secure"), bool)
            or cookie.get("sameSite") not in ("Strict", "Lax", "None")
        ):
            raise ValueError("平台登录状态格式不正确")
    if not any(
        cookie["domain"].lstrip(".").lower() in {"kuaishou.com", "cp.kuaishou.com"}
        for cookie in cookies
    ):
        raise ValueError("没有可用于快手创作者平台的登录材料")
    for origin in state["origins"]:
        entries = origin.get("localStorage")
        if not isinstance(entries, list) or not all(
            isinstance(entry, dict)
            and isinstance(entry.get("name"), str)
            and isinstance(entry.get("value"), str)
            for entry in entries
        ):
            raise ValueError("平台登录状态格式不正确")
    return state


async def _close_resource(resource: Any) -> None:
    if resource is None:
        return
    try:
        async with asyncio.timeout(5):
            await resource.close()
    except Exception as exc:  # noqa: BLE001 - 清理不能覆盖已确认的发布事实
        logger.warning("kuaishou browser cleanup failed: %s", type(exc).__name__)


@asynccontextmanager
async def _browser_context(storage_state: dict[str, Any]) -> AsyncIterator[BrowserContext]:
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        context = None
        try:
            context = await browser.new_context(
                storage_state=cast("StorageState", storage_state),
                viewport={"width": 1080, "height": 780},
                locale="zh-CN",
            )
            yield context
        finally:
            await _close_resource(context)
            await _close_resource(browser)


def _is_upload_page(url: str) -> bool:
    parsed = urlparse(url)
    return (
        parsed.scheme == "https"
        and parsed.netloc == "cp.kuaishou.com"
        and parsed.path == "/article/publish/video"
    )


def _is_publish_receipt(url: str) -> bool:
    # 上游只证明跳转至已发布管理页，既没有作品 ID，也没有作品短链。
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    return (
        parsed.scheme == "https"
        and parsed.netloc == "cp.kuaishou.com"
        and parsed.path == "/article/manage/video"
        and query.get("status") == ["2"]
        and query.get("from") == ["publish"]
    )


async def _open_creator_page(
    page: Page, capture: KuaishouIdentityCapture, expected_platform_user_id: str
) -> None:
    from app.publish_browser_engine import KUAISHOU_PROBE_TIMEOUT_SECONDS, is_kuaishou_login_page

    await page.goto(UPLOAD_URL, wait_until="domcontentloaded", timeout=30000)
    if is_kuaishou_login_page(page.url):
        raise PublisherAuthError(PLATFORM, "快手登录态已失效，请重新扫码连接账号")
    if not _is_upload_page(page.url):
        raise RuntimeError("快手发布页面暂不可用")
    if await page.get_by_text("立即登录", exact=True).first.is_visible():
        raise PublisherAuthError(PLATFORM, "快手登录态已失效，请重新扫码连接账号")
    async with asyncio.timeout(KUAISHOU_PROBE_TIMEOUT_SECONDS):
        identity = await capture.result()
    if identity is None:
        raise PublisherAuthError(PLATFORM, "快手登录态已失效，请重新扫码连接账号")
    if identity["platform_user_id"] != expected_platform_user_id:
        raise PublisherAuthError(PLATFORM, "快手登录账号与任务绑定账号不一致，请重新连接原账号")


async def _probe_in_browser(
    storage_state: dict[str, Any], expected_platform_user_id: str
) -> tuple[bool, None]:
    from app.publish_browser_engine import KuaishouIdentityCapture

    async with asyncio.timeout(PROBE_TIMEOUT_SECONDS):
        async with _browser_context(storage_state) as context:
            page = await context.new_page()
            capture = KuaishouIdentityCapture(page)
            try:
                await _open_creator_page(page, capture, expected_platform_user_id)
            finally:
                await capture.close()
    return True, None


def probe_kuaishou(
    storage_state: dict[str, Any], *, expected_platform_user_id: str | None
) -> tuple[bool | None, str | None]:
    """True=有效，False=有证据的失效，None=暂不能确认，保留账号状态。"""
    try:
        state = _prepare_storage_state(storage_state)
    except ValueError:
        return False, "快手登录态材料无效，请重新扫码连接账号"
    except Exception as exc:  # noqa: BLE001 - 环境故障不能误判账号失效
        logger.warning("kuaishou state preparation unavailable: %s", type(exc).__name__)
        return None, _UNAVAILABLE_MESSAGE
    if not expected_platform_user_id:
        return None, "缺少绑定的快手账号身份，暂不能确认登录态"
    try:
        return asyncio.run(_probe_in_browser(state, expected_platform_user_id))
    except PublisherAuthError as exc:
        return False, exc.reason
    except Exception as exc:  # noqa: BLE001 - 不读取异常正文，避免带出登录材料
        logger.warning("kuaishou login probe unavailable: %s", type(exc).__name__)
        return None, _UNAVAILABLE_MESSAGE


def _caption(title: str, description: str) -> str:
    # 上游只有一个描述区；同时有标题和正文时必须都保留，不能让正文覆盖标题。
    title, description = title.strip(), description.strip()
    if not title:
        raise ValueError("快手发布需要填写标题")
    return f"{title}\n{description}" if description and description != title else title


async def _dismiss_guide(page: Page) -> None:
    tooltip = page.locator('div[id^="react-joyride-step"] div[role="alertdialog"]').first
    if await tooltip.count() and await tooltip.is_visible():
        for selector in (
            '[aria-label="Skip"], [data-action="skip"], button[title="Skip"]',
            'button:text("跳过")',
            'button:text("我知道了")',
            'button:text("关闭")',
        ):
            button = tooltip.locator(selector).first
            if await button.count() and await button.is_visible():
                await button.click(timeout=5000)
                break


async def _fill_description(page: Page, caption: str, tags: list[str]) -> None:
    label = page.get_by_text("描述", exact=True)
    # 直接向可编辑区域填写，避免容器能点击但正文实际未写入。
    for selector in (
        "xpath=following-sibling::div[@contenteditable='true']",
        "xpath=following-sibling::div//*[@contenteditable='true']",
        "xpath=ancestor::*[1]//*[@contenteditable='true'][1]",
        "xpath=following::*[@contenteditable='true'][1]",
    ):
        editor = label.locator(selector).first
        try:
            await editor.fill(caption, timeout=8000)
            break
        except Exception:  # noqa: BLE001 - 页面结构漂移时只尝试上游的其它定位方式
            continue
    else:
        raise RuntimeError("未能填写快手描述编辑区，请稍后重试")
    if not tags:
        return
    await editor.press("Control+End")
    await editor.press("Enter")
    for tag in tags:
        await page.keyboard.insert_text(f"#{tag.lstrip('#')}")
        await editor.press("Space")
        await asyncio.sleep(2)


async def _wait_for_upload(page: Page) -> None:
    async with asyncio.timeout(UPLOAD_TIMEOUT_SECONDS):
        while True:
            failed = page.locator("text=上传失败").first
            if await failed.count() and await failed.is_visible():
                raise RuntimeError("快手素材上传失败，本次尚未提交发布")
            uploading = page.locator("text=上传中").first
            if not await uploading.count() or not await uploading.is_visible():
                return
            await asyncio.sleep(1)


async def _set_cover(page: Page, cover_path: Path) -> None:
    cover_label = page.locator("span").filter(has_text="封面设置").first
    await cover_label.wait_for(state="visible", timeout=30000)
    await cover_label.locator("xpath=../following-sibling::div[1]").locator("div").nth(0).click()
    modal = page.locator('div[role="document"].ant-modal')
    await modal.wait_for(state="visible", timeout=30000)
    tab = modal.get_by_text("上传封面", exact=True)
    await tab.click(timeout=10000)
    file_input = modal.locator('input[type="file"]')
    await file_input.wait_for(state="attached", timeout=30000)
    await file_input.set_input_files(str(cover_path))
    confirm = modal.get_by_role("button", name="确认", exact=True)
    await confirm.click(timeout=10000)
    await modal.wait_for(state="hidden", timeout=30000)


def _is_submit_url(url: str) -> bool:
    parsed = urlparse(url)
    return (
        parsed.scheme == "https"
        and parsed.netloc == "cp.kuaishou.com"
        and parsed.path == SUBMIT_PATH
    )


class _SubmissionEvidence:
    """观察官方页面实际发起的发布请求与回执。"""

    def __init__(self, page: Page, capture: KuaishouIdentityCapture) -> None:
        self.page = page
        self.capture = capture
        self.generation = capture.generation
        assert capture.identity is not None
        self.platform_user_id = capture.identity["platform_user_id"]
        self.request: Request | None = None
        self.read_task: asyncio.Task[None] | None = None
        self.rejected = False
        self.accepted = False
        page.on("request", self._on_request)
        page.on("response", self._on_response)

    def on_verified_page(self) -> bool:
        identity = self.capture.identity
        return (
            self.capture.generation == self.generation
            and identity is not None
            and identity["platform_user_id"] == self.platform_user_id
            and _is_upload_page(self.page.url)
        )

    def _on_request(self, request: Request) -> None:
        try:
            trusted = (
                request.method == "POST"
                and request.redirected_from is None
                and _is_submit_url(request.url)
                and request.frame is self.page.main_frame
                and _is_upload_page(request.frame.url)
                and self.on_verified_page()
            )
        except Exception:  # noqa: BLE001 - 非官方主文档请求不能充当发布证据
            return
        if trusted:
            if self.request is not None:
                self.rejected = True
            else:
                self.request = request

    def _on_response(self, response: Response) -> None:
        if response.request is self.request:
            if self.read_task is not None or not _is_submit_url(response.url):
                self.rejected = True
                return
            self.read_task = asyncio.create_task(self._read(response))

    async def _read(self, response: Response) -> None:
        try:
            body = await response.json() if response.ok else None
            self.accepted = (
                isinstance(body, dict) and type(body.get("result")) is int and body["result"] == 1
            )
        except Exception:  # noqa: BLE001 - 无法读取正文时交给人工核对
            self.accepted = False
        if not self.accepted:
            self.rejected = True

    async def close(self) -> None:
        self.page.remove_listener("request", self._on_request)
        self.page.remove_listener("response", self._on_response)
        if self.read_task is not None:
            self.read_task.cancel()
            await asyncio.gather(self.read_task, return_exceptions=True)


async def _wait_for_receipt(page: Page, evidence: _SubmissionEvidence) -> None:
    confirmed = False
    async with asyncio.timeout(RECEIPT_TIMEOUT_SECONDS):
        while True:
            if evidence.rejected:
                raise RuntimeError("平台提交回执未确认成功，请人工核对")
            if _is_publish_receipt(page.url):
                if evidence.accepted:
                    return
            elif not _is_upload_page(page.url):
                raise RuntimeError("快手发布页面已跳转，请人工核对")
            elif not confirmed and evidence.request is None:
                # 只处理本次发布的专用确认框，其它未知提示由用户核对。
                modal = (
                    page.locator(PUBLISH_CONFIRM)
                    .filter(has_text=re.compile(r"确认发布|确定发布|是否发布"))
                    .first
                )
                button = modal.get_by_role("button", name="确认发布", exact=True).or_(
                    modal.get_by_role("button", name="确定", exact=True)
                )
                if (
                    await button.is_visible()
                    and evidence.request is None
                    and evidence.on_verified_page()
                ):
                    confirmed = True
                    await button.click(timeout=8000)
            await asyncio.sleep(0.25)


async def _publish_in_browser(
    storage_state: dict[str, Any],
    *,
    expected_platform_user_id: str,
    video_path: Path,
    cover_path: Path | None,
    caption: str,
    tags: list[str],
    attempt: _Attempt,
    before_submit: Callable[[], bool],
) -> PublishResult:
    from app.publish_browser_engine import KuaishouIdentityCapture

    async with asyncio.timeout(DELIVERY_TIMEOUT_SECONDS):
        async with _browser_context(storage_state) as context:
            page = await context.new_page()
            capture = KuaishouIdentityCapture(page)
            try:
                await _open_creator_page(page, capture, expected_platform_user_id)
                async with page.expect_file_chooser(timeout=10000) as chooser_info:
                    await page.locator(UPLOAD_BUTTON).click(timeout=10000)
                chooser = await chooser_info.value
                _require_current_account(page, capture, expected_platform_user_id)
                await chooser.set_files(str(video_path))
                await _dismiss_guide(page)
                await _fill_description(page, caption, tags)
                await _wait_for_upload(page)
                if cover_path is not None:
                    _require_current_account(page, capture, expected_platform_user_id)
                    await _set_cover(page, cover_path)
                publish = page.get_by_text("发布", exact=True)
                await publish.wait_for(state="visible", timeout=10000)
                if not await publish.is_enabled():
                    raise RuntimeError("快手发布按钮尚不可用")
                _require_current_account(page, capture, expected_platform_user_id)
                if not before_submit():
                    return PublishResult(
                        platform=PLATFORM, status="failed", message="快手发布任务或绑定账号已变更"
                    )
                evidence = _SubmissionEvidence(page, capture)
                try:
                    # 点击有可能在发送后超时，之后的未知结果只能人工核对。
                    attempt.submission_started = True
                    await publish.click(timeout=10000)
                    await _wait_for_receipt(page, evidence)
                    attempt.result = PublishResult(platform=PLATFORM, status="published")
                    return attempt.result
                finally:
                    await evidence.close()
            finally:
                await capture.close()


def _require_current_account(
    page: Page, capture: KuaishouIdentityCapture, expected_platform_user_id: str
) -> None:
    if (
        not _is_upload_page(page.url)
        or capture.identity is None
        or capture.identity["platform_user_id"] != expected_platform_user_id
    ):
        raise RuntimeError("快手发布页面或账号已变更")


def publish_to_kuaishou(
    *,
    storage_state: dict[str, Any],
    expected_platform_user_id: str | None,
    video_path: Path,
    cover_path: Path | None,
    title: str,
    description: str,
    tags: list[str],
    options: dict[str, Any] | None = None,
    submission_key: str | None = None,
    before_submit: Callable[[], bool] | None = None,
) -> PublishResult:
    """单次浏览器投递，提交结果不明时交给人工核对。"""
    try:
        state = _prepare_storage_state(storage_state)
    except ValueError:
        return PublishResult(
            platform=PLATFORM,
            status="failed",
            account_invalid=True,
            message="快手登录态材料无效，请重新扫码连接账号",
        )
    except Exception as exc:  # noqa: BLE001 - 缺少运行环境属于提交前故障
        logger.warning("kuaishou state preparation unavailable: %s", type(exc).__name__)
        return PublishResult(platform=PLATFORM, status="failed", message=_UNAVAILABLE_MESSAGE)
    try:
        caption = _caption(title, description)
        if options:
            raise ValueError("快手目前仅支持默认选项的立即发布")
        if before_submit is None:
            raise ValueError("缺少快手发布任务确认")
        if len(tags) > MAX_TAGS:
            raise ValueError("快手发布渠道最多支持 3 个话题，请减少后重试")
        if not expected_platform_user_id:
            raise ValueError("缺少任务绑定的快手账号身份，请重新连接账号")
        if not video_path.is_file() or video_path.stat().st_size == 0:
            raise ValueError("待发布视频不存在或内容为空")
        if cover_path is not None and (not cover_path.is_file() or cover_path.stat().st_size == 0):
            raise ValueError("待发布封面不存在或内容为空")
    except ValueError as exc:
        return PublishResult(platform=PLATFORM, status="failed", message=str(exc))
    except OSError:
        return PublishResult(platform=PLATFORM, status="failed", message="待发布素材无法读取")
    if submission_key:
        logger.info("kuaishou publish submission_key=%s", submission_key)
    attempt = _Attempt()
    try:
        return asyncio.run(
            _publish_in_browser(
                state,
                expected_platform_user_id=expected_platform_user_id,
                video_path=video_path,
                cover_path=cover_path,
                caption=caption,
                tags=tags,
                attempt=attempt,
                before_submit=before_submit,
            )
        )
    except Exception as exc:  # noqa: BLE001 - 不输出可能包含 Cookie/网页内容的异常正文
        logger.warning("kuaishou publish interrupted: %s", type(exc).__name__)
        if attempt.result is not None:
            return attempt.result
        if attempt.submission_started:
            return PublishResult(platform=PLATFORM, status="unknown", message=_UNKNOWN_MESSAGE)
        if isinstance(exc, PublisherAuthError):
            return PublishResult(
                platform=PLATFORM, status="failed", account_invalid=True, message=exc.reason
            )
        return PublishResult(
            platform=PLATFORM,
            status="failed",
            message="快手发布准备或素材上传失败，尚未提交发布。请检查后重新确认发布。",
        )
