"""快手浏览器渠道与 worker 分流契约；全部材料虚构，禁止真实网络与平台发布。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from cryptography.fernet import Fernet

from app import publish_browser_engine, publish_delivery, publish_worker
from app.publish_browser import BrowserProbeLease
from app.publish_records import PublishWork
from app.publishers import channels_adapter, kuaishou_adapter
from app.publishers.base import PublishResult

_TEST_UID = "synthetic-bound-account"
_TEST_STATE = {
    "cookies": [
        {
            "name": "kuaishou.server.web_st",
            "value": "synthetic-test-state-only",
            "domain": ".kuaishou.com",
            "path": "/",
            "expires": -1,
            "httpOnly": True,
            "secure": True,
            "sameSite": "Lax",
        }
    ],
    "origins": [],
}
_RECEIPT = "https://cp.kuaishou.com/article/manage/video?status=2&from=publish"


class _Control:
    def __init__(self, web: _Web, kind: str) -> None:
        self.web, self.kind = web, kind

    @property
    def first(self) -> _Control:
        return self

    def nth(self, index: int) -> _Control:
        assert index == 0
        return self

    def filter(self, *, has_text: str) -> _Control:
        assert self.kind == "cover-label" and has_text == "封面设置"
        return self

    def locator(self, selector: str) -> _Control:
        if self.kind == "description-label":
            assert selector.startswith("xpath=")
            return _Control(self.web, "editor")
        controls = {
            ("cover-label", "xpath=../following-sibling::div[1]"): "cover-container",
            ("cover-container", "div"): "cover-open",
            ("cover-modal", 'input[type="file"]'): "cover-file",
            ("publish-modal", "button.ant-btn-primary:visible"): "publish-confirm",
        }
        return _Control(self.web, controls[(self.kind, selector)])

    def get_by_text(self, text: str, **kwargs: Any) -> _Control:
        assert self.kind == "cover-modal" and text == "上传封面"
        return _Control(self.web, "cover-tab")

    def get_by_role(self, role: str, *, name: str, exact: bool) -> _Control:
        assert (self.kind, role, name, exact) == ("cover-modal", "button", "确认", True)
        return _Control(self.web, "cover-confirm")

    async def count(self) -> int:
        if self.kind in {"guide", "know", "uploading"}:
            return 0
        if self.kind == "login":
            return int(self.web.mode == "signed-out")
        if self.kind == "upload-failed":
            return int(self.web.mode == "upload-failed")
        if self.kind == "publish-modal":
            return int(self.web.modal_visible)
        return 1

    async def is_visible(self) -> bool:
        return bool(await self.count())

    async def is_enabled(self) -> bool:
        assert self.kind == "publish"
        return self.web.mode != "disabled"

    async def wait_for(self, *, state: str, **kwargs: Any) -> None:
        if self.kind == "editor" and self.web.mode == "missing-editor":
            raise TimeoutError("synthetic-sensitive-material")

    async def click(self, **kwargs: Any) -> None:
        self.web.actions.append(self.kind)
        if self.kind == "publish":
            self.web.submissions += 1
            if self.web.mode == "click-timeout":
                raise TimeoutError("synthetic-sensitive-material")
            if self.web.mode == "click-disconnected":
                raise ConnectionError("synthetic-sensitive-material")
            if self.web.mode in {"confirm", "confirm-timeout"}:
                self.web.modal_visible = True
            else:
                self.web.page.emit_submit()
                self.web.page.url = self.web.receipt
        elif self.kind == "publish-confirm":
            self.web.confirmations += 1
            self.web.modal_visible = False
            self.web.page.emit_submit()
            if self.web.mode != "confirm-timeout":
                self.web.page.url = self.web.receipt

    async def set_input_files(self, path: str) -> None:
        assert self.kind == "cover-file" and Path(path).read_bytes() == b"test-cover"
        self.web.covers.append(path)


class _Keyboard:
    def __init__(self, web: _Web) -> None:
        self.web = web

    async def press(self, key: str) -> None:
        self.web.keys.append(key)

    async def insert_text(self, text: str) -> None:
        self.web.text.append(text)


class _FileChooser:
    def __init__(self, web: _Web) -> None:
        self.web = web

    async def __aenter__(self) -> _FileChooser:
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    @property
    async def value(self) -> _FileChooser:
        return self

    async def set_files(self, path: str) -> None:
        assert Path(path).read_bytes() == b"test-video"
        self.web.videos.append(path)


class _Page:
    def __init__(self, web: _Web) -> None:
        self.web = web
        self.url = kuaishou_adapter.UPLOAD_URL
        self.main_frame = SimpleNamespace(url=self.url)
        self.keyboard = _Keyboard(web)
        self.listeners: list[Any] = []

    def on(self, event: str, callback: Any) -> None:
        assert event == "response"
        self.listeners.append(callback)

    def remove_listener(self, event: str, callback: Any) -> None:
        assert event == "response"
        self.listeners.remove(callback)

    def emit_submit(self) -> None:
        if not self.web.emit_response:
            return
        request = SimpleNamespace(
            url="https://cp.kuaishou.com" + kuaishou_adapter.SUBMIT_PATH,
            method=self.web.submit_method,
            redirected_from=None,
            frame=self.main_frame,
        )

        async def body() -> Any:
            return self.web.submit_body

        response = SimpleNamespace(
            request=request,
            url=request.url,
            ok=self.web.submit_status == 200,
            json=body,
        )
        for callback in self.listeners:
            callback(response)

    async def goto(self, url: str, **kwargs: Any) -> None:
        assert url == kuaishou_adapter.UPLOAD_URL
        if self.web.transaction_depth:
            raise AssertionError("platform I/O cannot hold a transaction")
        if self.web.mode == "navigation-timeout":
            raise TimeoutError("synthetic-sensitive-material")
        self.url = (
            "https://passport.kuaishou.com/pc/account/login/"
            if self.web.mode == "signed-out"
            else url
        )

    def locator(self, selector: str) -> _Control:
        controls = {
            kuaishou_adapter.UPLOAD_BUTTON: "upload",
            'button[type="button"] span:text("我知道了")': "know",
            'div[id^="react-joyride-step"] div[role="alertdialog"]': "guide",
            "text=上传失败": "upload-failed",
            "text=上传中": "uploading",
            "span": "cover-label",
            'div[role="document"].ant-modal': "cover-modal",
            kuaishou_adapter.PUBLISH_CONFIRM: "publish-modal",
        }
        return _Control(self.web, controls[selector])

    def get_by_text(self, text: str, **kwargs: Any) -> _Control:
        return _Control(
            self.web, {"立即登录": "login", "描述": "description-label", "发布": "publish"}[text]
        )

    def expect_file_chooser(self, **kwargs: Any) -> _FileChooser:
        return _FileChooser(self.web)


class _Web:
    def __init__(self) -> None:
        self.mode = "success"
        self.receipt = _RECEIPT
        self.identity: dict[str, str] | None = {
            "platform_user_id": _TEST_UID,
            "username": "虚构测试账号",
        }
        self.identity_error: Exception | None = None
        self.identity_stall = False
        self.actions: list[str] = []
        self.keys: list[str] = []
        self.text: list[str] = []
        self.videos: list[str] = []
        self.covers: list[str] = []
        self.submissions = 0
        self.confirmations = 0
        self.modal_visible = False
        self.emit_response = True
        self.submit_body: Any = {"result": 1}
        self.submit_method = "POST"
        self.submit_status = 200
        self.launches = 0
        self.contexts: list[dict[str, Any]] = []
        self.closed: list[str] = []
        self.transaction_depth = 0
        self.cleanup_error = False
        self.page = _Page(self)

    async def launch(self, **kwargs: Any) -> _Web:
        assert kwargs == {"headless": True}
        self.launches += 1
        return self

    async def new_context(self, **kwargs: Any) -> _Context:
        assert isinstance(kwargs["storage_state"], dict)
        self.contexts.append(kwargs)
        return _Context(self)

    async def close(self) -> None:
        self.closed.append("browser")
        if self.cleanup_error:
            raise RuntimeError("synthetic-sensitive-material")

    async def __aenter__(self) -> SimpleNamespace:
        return SimpleNamespace(chromium=self)

    async def __aexit__(self, *args: Any) -> None:
        self.closed.append("playwright")


class _Context:
    def __init__(self, web: _Web) -> None:
        self.web = web

    async def new_page(self) -> _Page:
        return self.web.page

    async def close(self) -> None:
        self.web.closed.append("context")
        if self.web.cleanup_error:
            raise RuntimeError("synthetic-sensitive-material")


@pytest.fixture
def web(monkeypatch: pytest.MonkeyPatch) -> _Web:
    from playwright import async_api

    browser = _Web()
    monkeypatch.setattr(async_api, "async_playwright", lambda: browser)

    async def identity(context: Any) -> dict[str, str] | None:
        if browser.identity_error:
            raise browser.identity_error
        if browser.identity_stall:
            await asyncio.Event().wait()
        return browser.identity

    monkeypatch.setattr(publish_browser_engine, "probe_kuaishou_identity", identity, raising=False)
    original_sleep = asyncio.sleep

    async def fast_sleep(delay: float) -> None:
        await original_sleep(0)

    monkeypatch.setattr(kuaishou_adapter.asyncio, "sleep", fast_sleep)
    monkeypatch.setattr(kuaishou_adapter, "RECEIPT_TIMEOUT_SECONDS", 0.1)
    return browser


@pytest.fixture
def media(tmp_path: Path) -> dict[str, Any]:
    video, cover = tmp_path / "video.mp4", tmp_path / "cover.jpg"
    video.write_bytes(b"test-video")
    cover.write_bytes(b"test-cover")
    return {
        "video_path": video,
        "cover_path": cover,
        "title": "保留标题",
        "description": "也保留正文",
        "tags": ["自建房", "别墅", "设计"],
    }


def _publish(media: dict[str, Any], **kwargs: Any) -> PublishResult:
    return kuaishou_adapter.publish_to_kuaishou(
        storage_state=_TEST_STATE,
        expected_platform_user_id=_TEST_UID,
        **(media | kwargs),
    )


@pytest.mark.parametrize("mode", ["success", "confirm"])
def test_full_browser_flow_preserves_content_cover_and_only_proven_success(
    web: _Web, media: dict[str, Any], mode: str
) -> None:
    web.mode = mode
    result = _publish(media)
    assert result.status == "published" and not result.account_invalid
    assert result.item_id is None and result.short_url is None
    assert result.message and "未返回作品 ID" in result.message
    assert web.text == ["保留标题\n也保留正文", "#自建房 ", "#别墅 ", "#设计 "]
    assert len(web.videos) == len(web.covers) == web.submissions == 1
    assert web.confirmations == int(mode == "confirm")
    assert web.launches == 1 and web.closed == ["context", "browser", "playwright"]
    assert not any("path" in options for options in web.contexts)


@pytest.mark.parametrize(
    "mode", ["missing-editor", "upload-failed", "disabled", "navigation-timeout"]
)
def test_pre_submission_failures_never_submit_or_invalidate_valid_account(
    web: _Web, media: dict[str, Any], mode: str, caplog: pytest.LogCaptureFixture
) -> None:
    web.mode = mode
    result = _publish(media)
    assert result.status == "failed" and not result.account_invalid
    assert web.submissions == web.confirmations == 0
    assert len(web.videos) <= 1
    assert "synthetic-sensitive-material" not in caplog.text + (result.message or "")


@pytest.mark.parametrize("mode", ["click-timeout", "click-disconnected", "confirm-timeout"])
def test_after_submission_timeout_is_unknown_without_second_click(
    web: _Web, media: dict[str, Any], mode: str
) -> None:
    web.mode = mode
    result = _publish(media)
    assert result.status == "unknown" and not result.account_invalid
    assert web.submissions == 1 and len(web.videos) == 1
    assert web.confirmations == int(mode == "confirm-timeout")
    assert result.message and "人工核对" in result.message
    assert result.item_id is None and result.short_url is None


@pytest.mark.parametrize(
    "receipt",
    [
        "https://cp.kuaishou.com/article/manage/video",
        "https://cp.kuaishou.com/article/manage/video?status=1&from=publish",
        "https://cp.kuaishou.com/article/manage/video?status=2&from=manual",
        "https://cp.kuaishou.com.example.org/article/manage/video?status=2&from=publish",
    ],
)
def test_other_pages_cannot_fabricate_a_success_receipt(
    web: _Web, media: dict[str, Any], receipt: str
) -> None:
    web.receipt = receipt
    result = _publish(media)
    assert result.status == "unknown" and web.submissions == 1


@pytest.mark.parametrize("body", [{"result": 109}, {"result": True}, {"result": "1"}, None])
def test_rejected_or_unknown_submit_response_cannot_be_overridden_by_management_redirect(
    web: _Web, media: dict[str, Any], body: Any
) -> None:
    web.submit_body = body
    result = _publish(media)
    assert web.page.url == _RECEIPT
    assert result.status == "unknown" and web.submissions == 1
    assert not result.account_invalid and result.item_id is None


def test_submit_http_failure_stays_unknown_even_with_management_redirect(
    web: _Web, media: dict[str, Any]
) -> None:
    web.submit_status = 500
    assert _publish(media).status == "unknown" and web.submissions == 1


def test_unrelated_response_cannot_veto_upstream_management_receipt(
    web: _Web, media: dict[str, Any]
) -> None:
    web.submit_method = "GET"
    web.submit_body = {"result": 109}
    assert _publish(media).status == "published" and web.submissions == 1


def test_upstream_management_receipt_remains_valid_when_no_submit_response_was_observed(
    web: _Web, media: dict[str, Any]
) -> None:
    web.emit_response = False
    result = _publish(media)
    assert result.status == "published" and result.item_id is None and result.short_url is None


def test_wrong_platform_account_is_rejected_before_upload(web: _Web, media: dict[str, Any]) -> None:
    web.identity = {"platform_user_id": "another-synthetic-account", "username": "另一个账号"}
    result = _publish(media)
    assert result.status == "failed" and result.account_invalid
    assert web.submissions == 0 and web.videos == []
    assert result.message and "绑定账号不一致" in result.message
    assert "another-synthetic-account" not in result.message


@pytest.mark.parametrize("mode", ["signed-out", "identity-rejected"])
def test_explicit_signed_out_marks_account_invalid(
    web: _Web, media: dict[str, Any], mode: str
) -> None:
    web.mode = mode
    web.identity = None
    result = _publish(media)
    assert result.status == "failed" and result.account_invalid
    assert not web.videos and not web.submissions


def test_identity_network_failure_keeps_account_and_probe_unknown(
    web: _Web, media: dict[str, Any]
) -> None:
    web.identity_error = ConnectionError("synthetic-sensitive-material")
    result = _publish(media)
    ok, message = kuaishou_adapter.probe_kuaishou(_TEST_STATE, expected_platform_user_id=_TEST_UID)
    assert result.status == "failed" and not result.account_invalid
    assert ok is None and message
    assert web.submissions == 0


def test_hard_deadline_before_submission_is_safe_failure(
    web: _Web, media: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    web.identity_stall = True
    monkeypatch.setattr(kuaishou_adapter, "DELIVERY_TIMEOUT_SECONDS", 0.002)
    result = _publish(media)
    assert result.status == "failed" and not result.account_invalid
    assert web.submissions == 0
    assert web.closed == ["context", "browser", "playwright"]


def test_cleanup_failure_does_not_erase_confirmed_publication(
    web: _Web, media: dict[str, Any]
) -> None:
    web.cleanup_error = True
    assert _publish(media).status == "published"
    assert web.submissions == 1 and web.closed == ["context", "browser", "playwright"]


@pytest.mark.parametrize(
    "state",
    [None, [], {}, {"cookies": [], "origins": []}, {"cookies": "invalid", "origins": []}],
)
def test_invalid_storage_state_stops_before_launch(
    web: _Web, media: dict[str, Any], state: Any
) -> None:
    result = kuaishou_adapter.publish_to_kuaishou(
        storage_state=state, expected_platform_user_id=_TEST_UID, **media
    )
    ok, _message = kuaishou_adapter.probe_kuaishou(state, expected_platform_user_id=_TEST_UID)
    assert result.status == "failed" and result.account_invalid and ok is False
    assert web.launches == 0


@pytest.mark.parametrize("identity_available", [True, False])
def test_empty_auxiliary_cookie_defers_to_official_identity(
    web: _Web, media: dict[str, Any], identity_available: bool
) -> None:
    state = json.loads(json.dumps(_TEST_STATE))
    auxiliary = state["cookies"][0] | {"name": "synthetic_empty_auxiliary", "value": ""}
    state["cookies"].append(auxiliary)
    assert kuaishou_adapter._prepare_storage_state(state)["cookies"] == state["cookies"]
    if not identity_available:
        web.identity_error = TimeoutError("synthetic identity unavailable")
    result = kuaishou_adapter.publish_to_kuaishou(
        storage_state=state, expected_platform_user_id=_TEST_UID, **media
    )
    ok, _message = kuaishou_adapter.probe_kuaishou(state, expected_platform_user_id=_TEST_UID)
    assert not result.account_invalid and web.launches == 2
    assert web.contexts[0]["storage_state"]["cookies"][-1] == auxiliary
    if identity_available:
        assert result.status == "published" and ok is True and web.submissions == 1
    else:
        assert result.status == "failed" and ok is None and web.submissions == 0


@pytest.mark.parametrize(
    ("name", "value"),
    [("", "synthetic"), ("synthetic", None), ("synthetic", 0), ("synthetic", False)],
)
def test_empty_cookie_name_and_non_string_cookie_value_remain_invalid(
    web: _Web, media: dict[str, Any], name: str, value: Any
) -> None:
    state = json.loads(json.dumps(_TEST_STATE))
    state["cookies"][0].update(name=name, value=value)
    with pytest.raises(ValueError):
        kuaishou_adapter._prepare_storage_state(state)
    result = kuaishou_adapter.publish_to_kuaishou(
        storage_state=state, expected_platform_user_id=_TEST_UID, **media
    )
    assert result.status == "failed" and result.account_invalid and web.launches == 0


@pytest.mark.parametrize(
    "changes",
    [
        {"tags": ["一", "二", "三", "四"]},
        {"options": {"publish_date": "2099-01-01"}},
        {"options": {"timing": 1}},
        {"options": {"visibility": 2}},
        {"title": ""},
    ],
)
def test_unsupported_input_is_not_silently_ignored(
    web: _Web, media: dict[str, Any], changes: dict[str, Any]
) -> None:
    result = _publish(media, **changes)
    assert result.status == "failed" and not result.account_invalid
    assert web.launches == web.submissions == 0


def test_kuaishou_bypasses_protocol_translation_and_fallback_flag(
    web: _Web, media: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("kuaishou must not use a protocol adapter")

    monkeypatch.setattr(publish_delivery, "credentials_for_platform", forbidden)
    monkeypatch.setattr(publish_delivery, "deliver_via_api", forbidden)
    monkeypatch.setenv(publish_delivery.BROWSER_FALLBACK_ENV, "0")
    outcome = publish_delivery.deliver(
        "kuaishou", _TEST_STATE, expected_platform_user_id=_TEST_UID, **media
    )
    assert outcome.mode == "browser" and outcome.result.status == "published"
    assert web.submissions == 1


def test_browser_boundary_exception_never_makes_retryable_failure(
    media: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def interrupted(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("unknown browser lifecycle phase")

    monkeypatch.setattr(publish_delivery, "deliver_via_browser", interrupted)
    outcome = publish_delivery.deliver("kuaishou", _TEST_STATE, **media)
    assert outcome.mode == "browser" and outcome.result.status == "unknown"


def test_xiaohongshu_remains_unavailable_for_delivery(
    media: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("XHS must not gain browser publication")

    monkeypatch.setattr(publish_delivery, "deliver_via_browser", forbidden)
    assert publish_delivery.deliver("xiaohongshu", _TEST_STATE, **media).result.status == "failed"


def _encrypted_lease(key: Fernet, state: Any = _TEST_STATE) -> BrowserProbeLease:
    return BrowserProbeLease(
        record_id="test-account",
        platform="kuaishou",
        storage_state_enc=key.encrypt(json.dumps(state).encode()).decode(),
        lease_token="test-probe-lease",
        attempt_count=1,
        platform_user_id=_TEST_UID,
    )


def test_worker_probe_uses_full_state_and_identity_instead_of_channels(
    web: _Web, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = Fernet(Fernet.generate_key())

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("kuaishou must not use channels or protocol credentials")

    monkeypatch.setattr(channels_adapter, "probe_channels", forbidden)
    monkeypatch.setattr("app.publish_credentials.credentials_for_platform", forbidden)
    ok, message = publish_worker._perform_browser_probe(_encrypted_lease(key), key)
    assert ok is True and message is None and web.launches == 1
    web.identity = {"platform_user_id": "wrong-account", "username": "其它账号"}
    ok, _message = publish_worker._perform_browser_probe(_encrypted_lease(key), key)
    assert ok is False and not web.submissions


class _Transaction:
    def execute(self, sql: str, *args: Any) -> SimpleNamespace:
        assert "publish_stats_sync_enabled" in sql
        return SimpleNamespace(fetchone=lambda: (0,))


class _Storage:
    def iter_object(self, name: str) -> Iterator[bytes]:
        yield {"video": b"test-video", "cover": b"test-cover"}[name]


@pytest.mark.parametrize("mode", ["success", "click-timeout", "upload-failed"])
def test_real_worker_round_routes_encrypted_account_and_fences_submission(
    web: _Web, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    from app import media_routes

    web.mode = mode
    key = Fernet(Fernet.generate_key())
    work = PublishWork(
        record_id="test-record",
        worker_id="test-worker",
        lease_token="test-publish-lease",
        attempt_count=1,
        row={
            "platform": "kuaishou",
            "platform_user_id": _TEST_UID,
            "storage_state_enc": key.encrypt(json.dumps(_TEST_STATE).encode()).decode(),
            "video_uri": "fake://test/video",
            "cover_uri": "fake://test/cover",
            "video_content_type": "video/mp4",
            "cover_content_type": "image/jpeg",
            "title": "worker标题",
            "description": "worker正文",
            "tags": ["测试"],
            "options": {},
            "submission_key": "synthetic-submission-key",
        },
    )
    events: list[str] = []
    outcomes: list[tuple[PublishResult, str]] = []

    @contextmanager
    def open_txn() -> Iterator[Any]:
        web.transaction_depth += 1
        try:
            yield _Transaction()
        finally:
            web.transaction_depth -= 1

    def mark(*args: Any, **kwargs: Any) -> bool:
        assert kwargs == {"record_id": work.record_id, "lease_token": work.lease_token}
        assert web.submissions == 0
        events.append("marked")
        return True

    def renew(*args: Any, **kwargs: Any) -> bool:
        events.append("renewed")
        return True

    def finalize(conn: Any, **kwargs: Any) -> str:
        assert kwargs["work"] is work and kwargs["delivery_mode"] == "browser"
        assert web.transaction_depth == 1
        outcomes.append((kwargs["result"], kwargs["delivery_mode"]))
        return kwargs["result"].status  # type: ignore[no-any-return]

    monkeypatch.setattr(publish_worker, "claim_account_verify_work", lambda *a, **k: None)
    monkeypatch.setattr(publish_worker, "claim_browser_probe_work", lambda *a, **k: None)
    monkeypatch.setattr(publish_worker, "claim_publish_work", lambda *a, **k: work)
    monkeypatch.setattr(publish_worker, "mark_record_submitted", mark)
    monkeypatch.setattr(publish_worker, "renew_publish_lease", renew)
    monkeypatch.setattr(publish_worker, "finalize_publish_work", finalize)
    monkeypatch.setattr(media_routes, "storage_for_asset", lambda *a, **k: _Storage())
    assert publish_worker.run_publish_round(open_txn, worker_id="test-worker", fernet=key) == 1
    expected = {"success": "published", "click-timeout": "unknown", "upload-failed": "failed"}
    assert len(outcomes) == 1 and outcomes[0][0].status == expected[mode]
    assert events == ["marked", "renewed"]
    assert web.submissions == int(mode != "upload-failed")
    assert not Path(web.videos[0]).exists() and not Path(web.videos[0]).parent.parent.exists()


@pytest.mark.parametrize("defer_owned", [True, False])
def test_unknown_worker_probe_is_deferred_without_finalizing_or_blocking_delivery(
    web: _Web, monkeypatch: pytest.MonkeyPatch, defer_owned: bool
) -> None:
    key = Fernet(Fernet.generate_key())
    lease = _encrypted_lease(key)
    web.identity_error = TimeoutError("synthetic-sensitive-material")
    phases: list[str] = []

    @contextmanager
    def open_txn() -> Iterator[Any]:
        web.transaction_depth += 1
        try:
            yield _Transaction()
        finally:
            web.transaction_depth -= 1

    def no_finalize(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("unknown probe cannot mark the account invalid")

    def defer(conn: Any, **kwargs: Any) -> bool:
        assert kwargs == {"lease": lease} and web.transaction_depth == 1
        phases.append("probe-defer")
        return defer_owned

    def no_publish(*args: Any, **kwargs: Any) -> None:
        phases.append("publish-claim")
        return None

    monkeypatch.setattr(publish_worker, "claim_account_verify_work", lambda *a, **k: None)
    monkeypatch.setattr(publish_worker, "claim_browser_probe_work", lambda *a, **k: lease)
    monkeypatch.setattr(publish_worker, "finalize_browser_probe", no_finalize)
    monkeypatch.setattr(publish_worker, "defer_browser_probe_work", defer)
    monkeypatch.setattr(publish_worker, "claim_publish_work", no_publish)
    assert publish_worker.run_publish_round(open_txn, worker_id="test-worker", fernet=key) == 1
    assert phases == ["probe-defer", "publish-claim"]


@pytest.mark.parametrize("platform", ["douyin", "wechat_channels"])
@pytest.mark.parametrize("ok", [True, False])
def test_protocol_browser_probes_keep_existing_adapter_and_finalization(
    web: _Web, monkeypatch: pytest.MonkeyPatch, platform: str, ok: bool
) -> None:
    from app.publishers import douyin_adapter

    key = Fernet(Fernet.generate_key())
    cookie_domain = ".douyin.com" if platform == "douyin" else ".weixin.qq.com"
    state = {
        "cookies": [{"name": "synthetic_session", "value": "synthetic", "domain": cookie_domain}],
        "origins": [],
    }
    lease = BrowserProbeLease(
        record_id="test-protocol-account",
        platform=platform,
        storage_state_enc=key.encrypt(json.dumps(state).encode()).decode(),
        lease_token="test-protocol-probe-lease",
        attempt_count=1,
    )
    adapters: list[str] = []
    finalized: list[bool] = []
    message = None if ok else "虚构的登录态失效回执"

    @contextmanager
    def open_txn() -> Iterator[Any]:
        web.transaction_depth += 1
        try:
            yield _Transaction()
        finally:
            web.transaction_depth -= 1

    def probe_douyin(cookie: str, sdk: Any) -> tuple[bool, str | None]:
        assert cookie == "synthetic_session=synthetic" and sdk is None
        adapters.append("douyin")
        return ok, message

    def probe_channels(cookie: str) -> tuple[bool, str | None]:
        assert cookie == "synthetic_session=synthetic"
        adapters.append("wechat_channels")
        return ok, message

    def finalize(conn: Any, **kwargs: Any) -> None:
        assert kwargs == {"lease": lease, "ok": ok, "message": message}
        assert web.transaction_depth == 1
        finalized.append(kwargs["ok"])

    def no_defer(*args: Any, **kwargs: Any) -> bool:
        raise AssertionError("known protocol probe outcomes must be finalized")

    monkeypatch.setattr(publish_worker, "claim_account_verify_work", lambda *a, **k: None)
    monkeypatch.setattr(publish_worker, "claim_browser_probe_work", lambda *a, **k: lease)
    monkeypatch.setattr(publish_worker, "claim_publish_work", lambda *a, **k: None)
    monkeypatch.setattr(publish_worker, "finalize_browser_probe", finalize)
    monkeypatch.setattr(publish_worker, "defer_browser_probe_work", no_defer)
    monkeypatch.setattr(douyin_adapter, "probe_douyin", probe_douyin)
    monkeypatch.setattr(channels_adapter, "probe_channels", probe_channels)
    assert publish_worker.run_publish_round(open_txn, worker_id="test-worker", fernet=key) == 1
    assert adapters == [platform] and finalized == [ok] and web.launches == 0


@pytest.mark.parametrize("mode", ["success", "click-timeout"])
def test_worker_media_cleanup_error_preserves_received_delivery_outcome(
    web: _Web, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    from app import media_routes

    web.mode = mode
    key = Fernet(Fernet.generate_key())
    work = PublishWork(
        record_id="test-record",
        worker_id="test-worker",
        lease_token="test-lease",
        attempt_count=1,
        row={
            "platform": "kuaishou",
            "platform_user_id": _TEST_UID,
            "storage_state_enc": key.encrypt(json.dumps(_TEST_STATE).encode()).decode(),
            "video_uri": "fake://test/video",
            "video_content_type": "video/mp4",
            "title": "测试标题",
            "description": "测试描述",
        },
    )
    materialized = publish_delivery.materialized_media

    @contextmanager
    def fail_cleanup(*args: Any, **kwargs: Any) -> Iterator[Any]:
        with materialized(*args, **kwargs) as paths:
            yield paths
        raise OSError("synthetic temp cleanup failure")

    @contextmanager
    def open_txn() -> Iterator[Any]:
        yield _Transaction()

    monkeypatch.setattr(publish_delivery, "materialized_media", fail_cleanup)
    monkeypatch.setattr(media_routes, "storage_for_asset", lambda *a, **k: _Storage())
    result, delivery_mode = publish_worker._perform_publish(open_txn, work=work, key=key)
    assert delivery_mode == "browser" and web.submissions == 1
    assert result.status == ("published" if mode == "success" else "unknown")
