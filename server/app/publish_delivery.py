"""One delivery attempt through the platform's supported channel.

Kuaishou uses the browser channel directly; the protocol platforms retain
API-first delivery with an optional browser fallback. The worker supplies a
decrypted in-memory ``storage_state`` and runs adapters outside transactions.
Heavy vendor / Playwright imports stay inside the adapters.

A rejected login state or an unknown submission outcome is terminal for the
attempt: falling back after either could repeat an irreversible publication.
"""

from __future__ import annotations

import logging
import os
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from app.media import storage_key_from_uri
from app.publish_credentials import PublishCredentials, credentials_for_platform
from app.publishers.base import PublisherUnavailableError, PublishResult
from app.storage import StorageAdapter

logger = logging.getLogger(__name__)

BROWSER_FALLBACK_ENV = "VIDEO_REPLICA_PUBLISH_BROWSER_FALLBACK"

DeliveryMode = Literal["api", "browser"]


@dataclass(frozen=True)
class DeliveryOutcome:
    result: PublishResult
    mode: DeliveryMode | None


def browser_fallback_enabled() -> bool:
    value = (os.environ.get(BROWSER_FALLBACK_ENV) or "1").strip().lower()
    return value not in {"0", "off", "false", "no"}


def _suffix_for(content_type: str | None, default: str) -> str:
    mapping = {
        "video/mp4": ".mp4",
        "video/quicktime": ".mov",
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
    }
    return mapping.get((content_type or "").split(";")[0].strip().lower(), default)


def materialize_asset(
    storage: StorageAdapter,
    storage_uri: str,
    *,
    directory: Path,
    suffix: str,
) -> Path:
    """Stream one stored object into ``directory``; returns the local path."""
    key = storage_key_from_uri(storage_uri)
    target = directory / f"asset{suffix}"
    with target.open("wb") as handle:
        for chunk in storage.iter_object(key):
            handle.write(chunk)
    if target.stat().st_size <= 0:
        raise RuntimeError("stored object is empty")
    return target


@contextmanager
def materialized_media(
    storage: StorageAdapter,
    *,
    video_uri: str,
    video_content_type: str | None,
    cover_uri: str | None,
    cover_content_type: str | None,
) -> Iterator[tuple[Path, Path | None]]:
    with tempfile.TemporaryDirectory(prefix="publish-") as tmp:
        root = Path(tmp)
        video_dir = root / "video"
        video_dir.mkdir()
        video = materialize_asset(
            storage,
            video_uri,
            directory=video_dir,
            suffix=_suffix_for(video_content_type, ".mp4"),
        )
        cover: Path | None = None
        if cover_uri:
            cover_dir = root / "cover"
            cover_dir.mkdir()
            cover = materialize_asset(
                storage,
                cover_uri,
                directory=cover_dir,
                suffix=_suffix_for(cover_content_type, ".jpg"),
            )
        yield video, cover


def deliver_via_api(
    platform: str,
    credentials: PublishCredentials,
    *,
    video_path: Path,
    cover_path: Path | None,
    title: str,
    description: str,
    tags: list[str],
    options: dict[str, Any],
    submission_key: str | None = None,
) -> PublishResult:
    if platform == "douyin":
        from app.publishers import douyin_adapter

        return douyin_adapter.publish_to_douyin(
            cookie=credentials.cookie,
            security_sdk=credentials.security_sdk,
            video_path=video_path,
            title=title,
            description=description,
            tags=tags,
            cover_path=cover_path,
            visibility=int(options.get("visibility", 0) or 0),
            submission_key=submission_key,
        )
    if platform == "wechat_channels":
        from app.publishers import channels_adapter

        return channels_adapter.publish_to_channels(
            cookie=credentials.cookie,
            video_path=video_path,
            title=title,
            description=description,
            tags=tags,
            cover_path=cover_path,
            submission_key=submission_key,
        )
    return PublishResult(platform=platform, status="failed", message="该平台暂不支持自动发布")


def deliver_via_browser(
    platform: str,
    storage_state: dict[str, Any],
    *,
    video_path: Path,
    cover_path: Path | None,
    title: str,
    description: str,
    tags: list[str],
    options: dict[str, Any],
    expected_platform_user_id: str | None = None,
    submission_key: str | None = None,
    before_submit: Callable[[], bool] | None = None,
) -> PublishResult:
    if platform == "kuaishou":
        from app.publishers import kuaishou_adapter

        return kuaishou_adapter.publish_to_kuaishou(
            storage_state=storage_state,
            expected_platform_user_id=expected_platform_user_id,
            video_path=video_path,
            cover_path=cover_path,
            title=title,
            description=description,
            tags=tags,
            options=options,
            submission_key=submission_key,
            before_submit=before_submit,
        )
    raise PublisherUnavailableError(platform, "浏览器兜底发布尚未启用")


def deliver(
    platform: str,
    storage_state: dict[str, Any],
    *,
    video_path: Path,
    cover_path: Path | None,
    title: str,
    description: str,
    tags: list[str],
    options: dict[str, Any] | None = None,
    submission_key: str | None = None,
    expected_platform_user_id: str | None = None,
    before_submit: Callable[[], bool] | None = None,
) -> DeliveryOutcome:
    opts = options or {}
    # 快手仅走浏览器渠道；协议凭据翻译不支持它，不能据此误判扫码账号失效。
    if platform == "kuaishou":
        try:
            result = deliver_via_browser(
                platform,
                storage_state,
                video_path=video_path,
                cover_path=cover_path,
                title=title,
                description=description,
                tags=tags,
                options=opts,
                expected_platform_user_id=expected_platform_user_id,
                submission_key=submission_key,
                before_submit=before_submit,
            )
        except PublisherUnavailableError as exc:
            result = PublishResult(platform=platform, status="failed", message=exc.reason)
        except Exception as exc:  # noqa: BLE001 - 无法判断浏览器是否已经提交，禁止重投
            logger.warning("kuaishou browser delivery interrupted: %s", type(exc).__name__)
            result = PublishResult(
                platform=platform,
                status="unknown",
                message="快手发布结果无法确认，为避免重复发表已暂停自动重试，请人工核对",
            )
        return DeliveryOutcome(result, "browser")
    try:
        credentials = credentials_for_platform(platform, storage_state)
    except ValueError as exc:
        return DeliveryOutcome(
            PublishResult(
                platform=platform, status="failed", message=str(exc), account_invalid=True
            ),
            None,
        )
    api_result = deliver_via_api(
        platform,
        credentials,
        video_path=video_path,
        cover_path=cover_path,
        title=title,
        description=description,
        tags=tags,
        options=opts,
        submission_key=submission_key,
    )
    # unknown（T23）：提交可能已到达平台，浏览器兜底等于第二次投递，必须停。
    if (
        api_result.status == "published"
        or api_result.status == "unknown"
        or api_result.account_invalid
    ):
        return DeliveryOutcome(api_result, "api")
    if not browser_fallback_enabled():
        return DeliveryOutcome(api_result, "api")
    logger.info("publish api attempt failed on %s; trying browser fallback", platform)
    try:
        browser_result = deliver_via_browser(
            platform,
            storage_state,
            video_path=video_path,
            cover_path=cover_path,
            title=title,
            description=description,
            tags=tags,
            options=opts,
        )
    except PublisherUnavailableError as exc:
        logger.info("browser fallback unavailable: %s", exc.reason)
        return DeliveryOutcome(api_result, "api")
    except Exception as exc:  # noqa: BLE001 - delivery boundary
        logger.warning("browser fallback failed: %s", type(exc).__name__)
        return DeliveryOutcome(
            PublishResult(
                platform=platform,
                status="failed",
                message=f"{api_result.message or '发布失败'}；浏览器兜底也未成功",
            ),
            "browser",
        )
    return DeliveryOutcome(browser_result, "browser")
