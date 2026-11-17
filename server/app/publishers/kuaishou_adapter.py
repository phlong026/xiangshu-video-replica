"""快手视频发布：将固定 MIT 上游的控件流程接入本项目的隔离浏览器。

Adapted from dreammis/social-auto-upload, uploader/ks_uploader/main.py at
0012d2c355f88f683cc38dde2a2db209e14091bc, Copyright (c) 2023 dreammis.
MIT license: see vendor/social_auto_upload/LICENSE (distributed with this module).

只复用有来源的上传、描述、封面与确认控件；登录态只在内存中传入标准 Playwright。
不自动重传素材、不重复点击发布、不保存网页或凭据；任务定时由本项目 worker 处理。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import parse_qs, urlparse

from .base import PublisherAuthError, PublishResult

if TYPE_CHECKING:
    from playwright.async_api import BrowserContext, Page, Response, StorageState

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

_UNKNOWN_MESSAGE = "快手发布已提交但结果未知，为避免重复发表已暂停自动重试，请人工核对"
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
        # 桌面浏览器会导出空值辅助 Cookie；结构合法性不能代替官方身份核验。
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


async def _check_account(
    context: BrowserContext, page: Page, expected_platform_user_id: str
) -> None:
    from app.publish_browser_engine import probe_kuaishou_identity

    await page.goto(UPLOAD_URL, wait_until="domcontentloaded", timeout=45000)
    login_button = page.get_by_text("立即登录").first
    if urlparse(page.url).netloc == "passport.kuaishou.com" or (
        await login_button.count() and await login_button.is_visible()
    ):
        raise PublisherAuthError(PLATFORM, "快手登录态已失效，请重新扫码连接账号")
    identity = await probe_kuaishou_identity(context)
    if identity is None:
        raise PublisherAuthError(PLATFORM, "快手登录态已失效，请重新扫码连接账号")
    if identity.get("platform_user_id") != expected_platform_user_id:
        raise PublisherAuthError(PLATFORM, "快手登录账号与任务绑定账号不一致，请重新连接原账号")
    if not _is_upload_page(page.url):
        raise RuntimeError("快手发布页面暂不可用")
    await page.locator(UPLOAD_BUTTON).wait_for(state="visible", timeout=10000)


async def _probe_in_browser(
    storage_state: dict[str, Any], expected_platform_user_id: str
) -> tuple[bool, None]:
    async with asyncio.timeout(PROBE_TIMEOUT_SECONDS):
        async with _browser_context(storage_state) as context:
            page = await context.new_page()
            await _check_account(context, page, expected_platform_user_id)
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


def _validate_options(options: dict[str, Any]) -> None:
    schedule_keys = {"scheduled_at", "publish_date", "publish_strategy", "timing"}
    if schedule_keys.intersection(options):
        raise ValueError("快手定时发布请设置任务的 scheduled_at，由系统到期投递")
    if set(options) - {"visibility"} or options.get("visibility", 0) not in (0, None):
        raise ValueError("快手发布暂不支持这些发布选项，请移除后重试")


async def _dismiss_guide(page: Page) -> None:
    know = page.locator('button[type="button"] span:text("我知道了")').first
    if await know.count() and await know.is_visible():
        await know.click(timeout=5000)
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
    label = page.get_by_text("描述")
    # 保留上游已经出现过的结构变体；只读定位失败时换策略，不重做上传或提交。
    for selector in (
        "xpath=following-sibling::div",
        "xpath=following::div[@contenteditable='true'][1]",
        "xpath=ancestor::*[1]//div[@contenteditable='true'][1]",
        "xpath=following::*[@contenteditable='true'][1]",
    ):
        editor = label.locator(selector).first
        try:
            await editor.wait_for(state="visible", timeout=8000)
            await editor.click(timeout=5000)
            break
        except Exception:  # noqa: BLE001 - 页面结构漂移时只尝试上游的其它定位方式
            continue
    else:
        raise RuntimeError("未能定位快手描述编辑区，请稍后重试")
    await page.keyboard.press("Control+KeyA")
    await page.keyboard.press("Delete")
    await page.keyboard.insert_text(caption)
    await page.keyboard.press("Enter")
    for tag in tags:
        await page.keyboard.insert_text(f"#{tag.lstrip('#')} ")
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
    """管理页跳转不能覆盖官方提交接口已经返回的拒绝或未知回执。"""

    def __init__(self, page: Page) -> None:
        self.page = page
        self.rejected = False
        self.tasks: set[asyncio.Task[None]] = set()
        page.on("response", self._on_response)

    def _on_response(self, response: Response) -> None:
        try:
            request = response.request
            if (
                not _is_submit_url(response.url)
                or not _is_submit_url(request.url)
                or request.method != "POST"
                or request.redirected_from is not None
                or request.frame is not self.page.main_frame
                or urlparse(request.frame.url).netloc != "cp.kuaishou.com"
                or urlparse(request.frame.url).scheme != "https"
            ):
                return
        except Exception:  # noqa: BLE001 - 非当前官方文档发起的响应不能充当发布证据
            return
        task = asyncio.create_task(self._read(response))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def _read(self, response: Response) -> None:
        try:
            body = await response.json() if response.ok else None
            if (
                not isinstance(body, dict)
                or type(body.get("result")) is not int
                or body["result"] != 1
            ):
                self.rejected = True
        except Exception:  # noqa: BLE001 - 无法识别的提交回执也必须交给人工核对
            self.rejected = True

    async def verify(self) -> None:
        # 给 response 回调一个调度机会，再等已经观察到的正文，避免管理页先到达而假成功。
        await asyncio.sleep(0)
        async with asyncio.timeout(10):
            if self.tasks:
                await asyncio.gather(*tuple(self.tasks))
        if self.rejected:
            raise RuntimeError("平台提交回执未确认成功，请人工核对")

    async def close(self) -> None:
        self.page.remove_listener("response", self._on_response)
        for task in self.tasks:
            task.cancel()
        if self.tasks:
            await asyncio.gather(*tuple(self.tasks), return_exceptions=True)


async def _wait_for_receipt(page: Page, evidence: _SubmissionEvidence) -> None:
    confirmed = False
    async with asyncio.timeout(RECEIPT_TIMEOUT_SECONDS):
        while not _is_publish_receipt(page.url):
            if evidence.rejected:
                raise RuntimeError("平台提交回执未确认成功，请人工核对")
            # 确认弹窗属于这一次提交；它迟到时也可处理，但绝不能再次点底层发布按钮。
            modal = page.locator(PUBLISH_CONFIRM).first
            if not confirmed and await modal.count() and await modal.is_visible():
                button = modal.locator("button.ant-btn-primary:visible").first
                await button.wait_for(state="visible", timeout=8000)
                confirmed = True
                await button.click(timeout=8000)
            await asyncio.sleep(0.25)
        await evidence.verify()


async def _publish_in_browser(
    storage_state: dict[str, Any],
    *,
    expected_platform_user_id: str,
    video_path: Path,
    cover_path: Path | None,
    caption: str,
    tags: list[str],
    attempt: _Attempt,
) -> PublishResult:
    async with asyncio.timeout(DELIVERY_TIMEOUT_SECONDS):
        async with _browser_context(storage_state) as context:
            page = await context.new_page()
            await _check_account(context, page, expected_platform_user_id)
            async with page.expect_file_chooser(timeout=10000) as chooser_info:
                await page.locator(UPLOAD_BUTTON).click(timeout=10000)
            chooser = await chooser_info.value
            await chooser.set_files(str(video_path))
            await _dismiss_guide(page)
            await _fill_description(page, caption, tags)
            await _wait_for_upload(page)
            if cover_path is not None:
                await _set_cover(page, cover_path)
            publish = page.get_by_text("发布", exact=True)
            await publish.wait_for(state="visible", timeout=10000)
            if not await publish.is_enabled():
                raise RuntimeError("快手发布按钮尚不可用，本次尚未提交发布")
            evidence = _SubmissionEvidence(page)
            try:
                # click 本身也可能在发送后超时；先设边界，后续任何不明结果都禁止重投。
                attempt.submission_started = True
                await publish.click(timeout=10000)
                await _wait_for_receipt(page, evidence)
                attempt.result = PublishResult(
                    platform=PLATFORM,
                    status="published",
                    message="平台已跳转到发布完成管理页；未返回作品 ID 或短链，请在平台查看",
                )
                return attempt.result
            finally:
                await evidence.close()


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
) -> PublishResult:
    """单次浏览器投递；只有明确提交前失败才允许现有 worker 安全重排。"""
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
        _validate_options(options or {})
        if len(tags) > MAX_TAGS:
            raise ValueError("快手发布渠道最多支持 3 个话题，请减少后重试")
        if not expected_platform_user_id:
            raise ValueError("缺少任务绑定的快手账号身份，请重新连接账号")
        if not video_path.is_file() or video_path.stat().st_size == 0:
            raise ValueError("待发布视频不存在或内容为空")
        if cover_path is not None and (not cover_path.is_file() or cover_path.stat().st_size == 0):
            raise ValueError("待发布封面不存在或内容为空")
    except (ValueError, OSError) as exc:
        return PublishResult(platform=PLATFORM, status="failed", message=str(exc))
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
            message="快手发布准备或素材上传失败，本次尚未提交发布，请稍后重试",
        )
