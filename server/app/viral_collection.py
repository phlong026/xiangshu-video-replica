"""Weekly keyword collection and background media archiving; customer reads never enqueue."""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.error import HTTPError, URLError
from uuid import uuid4

import psycopg

from app.billing_meter import collection_billing_context, video_billing_context
from app.db_pg import pg_transaction
from app.db_portable import BusinessConnection
from app.external_calls import Outcome, record_external_call
from app.storage import StorageAdapter
from app.usage_billing import (
    accept_platform_operation,
    begin_attempt,
    complete_attempt,
    finalize_interrupted_media_refresh_operation,
    finish_operation,
)
from app.viral_collection_billing import (
    FrozenCollectionBilling,
    create_collection_batch,
    eligible_collection_users,
    freeze_collection_billing,
)
from app.viral_collection_budget import collection_budget
from app.viral_collection_failures import classify_collection_failure
from app.viral_collection_schedule import advance_collection_time
from app.viral_content_observations import record_content_source, record_content_stage
from app.viral_keywords import ViralKeywordConfig
from app.viral_link import (
    DOUYIN_CANONICAL_URL_TEMPLATE,
    DouyidouLinkClient,
    ViralLinkError,
    douyidou_link_client_from_settings,
)
from app.viral_media import CoverEnricher, UrlFetcher, ViralMediaPipeline
from app.viral_media_preparation import ViralMediaBusy, ViralMediaLeaseLost
from app.viral_refresh import ViralBatchPartialError, ViralRefreshLease, _require_lease
from app.viral_store import (
    get_viral_video,
    update_viral_cover,
    update_viral_statistics,
    upsert_viral_videos,
    viral_runtime_controls,
    viral_video_availability,
)
from app.viral_tikhub import (
    PLATFORM_DOUYIN,
    PLATFORM_WECHAT,
    ViralSourceError,
    ViralVideo,
    viral_source_client_from_settings,
)
from app.viral_wechat_link import wechat_link_client_from_settings_or_none


@dataclass(frozen=True)
class ViralQualityRules:
    """采集质量门槛（方案 P1 采集设置）：入池前过滤低质内容。

    四项都是「不设即不限」。这些值由管理端写入 ``viral_runtime_controls``；此前只存不用，
    运营配了「点赞不低于 5000」，低于它的视频照样被采集、下载、占存储和成本。
    """

    min_likes: int | None = None
    duration_min_ms: int | None = None
    duration_max_ms: int | None = None
    exclude_words: tuple[str, ...] = ()

    def rejects(self, video: ViralVideo) -> bool:
        if self.min_likes is not None and video.likes is not None and video.likes < self.min_likes:
            return True
        # 时长为 0 表示上游没给（并非真的 0 毫秒）：无法判断就不因时长规则误杀，
        # 否则一个不返回时长的平台会被「最短 15 秒」整批拒光。
        if video.duration_ms > 0:
            if self.duration_min_ms is not None and video.duration_ms < self.duration_min_ms:
                return True
            if self.duration_max_ms is not None and video.duration_ms > self.duration_max_ms:
                return True
        if self.exclude_words:
            text = " ".join(
                [
                    video.title,
                    *video.tags,
                    str(video.native.get("source_description") or ""),
                ]
            ).lower()
            if any(word.lower() in text for word in self.exclude_words):
                return True
        return False


def load_quality_rules(conn: BusinessConnection) -> ViralQualityRules:
    row = conn.execute(
        "SELECT quality_min_likes, quality_duration_min_ms, quality_duration_max_ms, "
        "quality_exclude_words_json FROM viral_runtime_controls WHERE id=1"
    ).fetchone()
    if row is None:
        return ViralQualityRules()
    try:
        words = json.loads(row["quality_exclude_words_json"] or "[]")
    except json.JSONDecodeError:
        words = []
    return ViralQualityRules(
        min_likes=row["quality_min_likes"],
        duration_min_ms=row["quality_duration_min_ms"],
        duration_max_ms=row["quality_duration_max_ms"],
        exclude_words=tuple(str(word) for word in words if str(word).strip()),
    )


def enqueue_due_viral_collections(
    conn: BusinessConnection,
    *,
    manual: bool = False,
    frozen_billing: FrozenCollectionBilling | None = None,
) -> bool:
    """Singleton row lock makes each daily/weekly schedule unique across workers.

    返回本次调用是否真的入队了一批采集任务（False：未启用 / 无关键词 / 预算已满 /
    未到期 / 已有任务在队）。手动「立即采集」据此如实回答运营，而不是不管有没有入队
    都说「已入队」。``manual=True`` 表示运营知情下的手动触发，不受月度预算门槛拦截。
    """
    row = conn.execute(
        "SELECT * FROM viral_runtime_controls WHERE id=1 AND collection_enabled=1 "
        "FOR UPDATE SKIP LOCKED"
    ).fetchone()
    if row is None:
        return False
    keywords = [
        ViralKeywordConfig.model_validate(item) for item in json.loads(row["keywords_json"])
    ]
    keywords = [item for item in keywords if item.enabled]
    if not keywords:
        return False
    # 月度预算门槛（方案 P1 采集设置）：本月平台侧采集成本已达预算时跳过
    # 定时入队；手动「立即采集」不拦——运营知情下的手动动作仍可用。
    if not manual and collection_budget(conn)["budget_status"] == "exhausted":
        return False
    due = conn.execute(
        "SELECT 1 FROM viral_runtime_controls WHERE id=1 "
        "AND (next_collection_at IS NULL OR next_collection_at <= CURRENT_TIMESTAMP)"
    ).fetchone()
    if due or manual:
        busy = conn.execute(
            "SELECT 1 FROM viral_refresh_tasks WHERE status IN ('PENDING','RUNNING') LIMIT 1"
        ).fetchone()
        if busy is not None:
            return False
        window_end = conn.execute(
            "SELECT extract(epoch FROM CURRENT_TIMESTAMP)::bigint"
        ).fetchone()[0]
        frozen = frozen_billing if frozen_billing is not None else freeze_collection_billing(conn)
        for platform in dict.fromkeys(item.platform for item in keywords):
            batch_config = {
                "keywords": [item.model_dump() for item in keywords if item.platform == platform],
                "limit": int(row["per_keyword_limit"]),
                "window_end": int(window_end),
                "trigger_kind": "manual" if manual else "scheduled",
            }
            batch_id = create_collection_batch(
                conn,
                platform=platform,
                config=batch_config,
                user_ids=frozen.user_ids,
                pricing_json=frozen.pricing_json,
            )
            config = json.dumps({**batch_config, "billing_batch_id": batch_id})
            task = conn.execute(
                """INSERT INTO viral_refresh_tasks(id,platform,sort,collection_config_json)
                VALUES(%s,%s,'hot',%s) ON CONFLICT(platform,sort) DO UPDATE SET status='PENDING',
                    collection_config_json=excluded.collection_config_json, checkpoint_json='{}',
                    retry_count=0, locked_by=NULL, locked_until=NULL, error_code=NULL,
                    error_message_redacted=NULL, completed_at=NULL, updated_at=CURRENT_TIMESTAMP
                    RETURNING id""",
                (str(uuid4()), platform, config),
            ).fetchone()
            assert task is not None
            conn.execute(
                "UPDATE viral_collection_batches SET related_task_id=%s WHERE id=%s",
                (str(task[0]), batch_id),
            )
        if not manual:
            if row["collection_time"] is not None:
                now = conn.execute("SELECT now()").fetchone()[0]
                conn.execute(
                    "UPDATE viral_runtime_controls SET next_collection_at=%s WHERE id=1",
                    (
                        advance_collection_time(
                            now,
                            int(row["collection_interval_days"]),
                            row["collection_time"],
                            datetime.fromisoformat(str(row["next_collection_at"]))
                            if row["next_collection_at"] is not None
                            else None,
                        ),
                    ),
                )
            else:
                conn.execute(
                    "UPDATE viral_runtime_controls SET next_collection_at="
                    "CURRENT_TIMESTAMP + (%s * interval '1 day') WHERE id=1",
                    (int(row["collection_interval_days"]),),
                )
        return True
    else:
        # Two retries per scheduled batch. Completed keywords and media remain
        # checkpointed, so retries only resume missing work, never restart a batch.
        # 人工单条转存（single_archive）同样纳入：新发抖音视频的搜索索引收录
        # 有滞后，fresh-video 的 douyin_refresh 首轮必然 miss（2026-11-17 Codex
        # 视频实锤：导入 1 分钟后转存失败、retryable=1 却永不自动重试），15
        # 分钟冷却后的自动重试让收录后的重取通道接管。
        conn.execute(
            """UPDATE viral_refresh_tasks SET status='PENDING', retry_count=retry_count+1,
                locked_by=NULL, locked_until=NULL, updated_at=CURRENT_TIMESTAMP
            WHERE status='FAILED' AND retryable=1 AND retry_count < 2
                AND collection_config_json != '{}'
                AND updated_at::timestamptz <=
                    CURRENT_TIMESTAMP - interval '15 minutes'"""
        )
        return False


@contextmanager
def _connection() -> Iterator[BusinessConnection]:
    with pg_transaction() as raw:
        yield BusinessConnection.postgres(raw)


def _manual_kind(conn: BusinessConnection, lease: ViralRefreshLease) -> bool:
    """任务是否为人工触发（单条转存/批量准备/推送）：不受定时采集开关约束."""
    from app.viral_push import MANUAL_TASK_KINDS

    row = conn.execute(
        "SELECT COALESCE(collection_config_json::jsonb->>'kind','') FROM viral_refresh_tasks "
        "WHERE id=%s",
        (lease.id,),
    ).fetchone()
    return row is not None and str(row[0]) in MANUAL_TASK_KINDS


@contextmanager
def _keep_lease(lease: ViralRefreshLease) -> Iterator[Callable[[], None]]:
    stop, lost = threading.Event(), threading.Event()

    def heartbeat() -> None:
        while not stop.wait(20):
            try:
                with _connection() as conn:
                    _require_lease(conn, lease)
                    if not viral_runtime_controls(conn)[0] and not _manual_kind(conn, lease):
                        lost.set()
                        return
                    conn.execute(
                        "UPDATE viral_refresh_tasks SET locked_until=%s WHERE id=%s",
                        (
                            (datetime.now(UTC) + timedelta(minutes=10)).strftime(
                                "%Y-%m-%d %H:%M:%S"
                            ),
                            lease.id,
                        ),
                    )
            except Exception:
                lost.set()
                return

    def check() -> None:
        if lost.is_set():
            raise ViralSourceError("后台采集已暂停或任务租约已失效。")
        with _connection() as conn:
            _require_lease(conn, lease)
            if not viral_runtime_controls(conn)[0] and not _manual_kind(conn, lease):
                raise ViralSourceError("后台采集已暂停。")

    thread = threading.Thread(target=heartbeat, daemon=True, name="viral-collection-lease")
    thread.start()
    try:
        yield check
    finally:
        stop.set()
        thread.join()


def _checkpoint(
    conn: BusinessConnection, lease: ViralRefreshLease, progress: dict[str, Any]
) -> None:
    _require_lease(conn, lease)
    row = conn.execute(
        "SELECT checkpoint_json FROM viral_refresh_tasks WHERE id=%s", (lease.id,)
    ).fetchone()
    current = json.loads(row["checkpoint_json"] or "{}") if row is not None else {}
    # 批量进度可能是旧快照，不能覆盖另一短事务写入的发送标记和成本记录。
    for key in ("media_refresh_requests", "media_refresh_accounting", "media_refresh_results"):
        if key in current:
            progress[key] = current[key]
    conn.execute(
        "UPDATE viral_refresh_tasks SET checkpoint_json=%s WHERE id=%s",
        (json.dumps(progress, ensure_ascii=False), lease.id),
    )
    conn.execute(
        "UPDATE viral_collection_batches SET failed_video_count=%s,video_outcomes_json=%s WHERE id="
        "(SELECT collection_config_json::jsonb->>'billing_batch_id' "
        "FROM viral_refresh_tasks WHERE id=%s)",
        (
            len(progress.get("failed_video_ids", [])),
            json.dumps(progress.get("video_outcomes", {}), ensure_ascii=False),
            lease.id,
        ),
    )


def _settle_interrupted_media_refreshes(lease: ViralRefreshLease) -> None:
    """接管时先收尾旧解析费用，缓存命中也不能遗留进行中的账目。"""
    with _connection() as conn:
        _require_lease(conn, lease)
        row = conn.execute(
            "SELECT checkpoint_json FROM viral_refresh_tasks WHERE id=%s", (lease.id,)
        ).fetchone()
        progress = json.loads(row["checkpoint_json"] or "{}")
        requests = progress.get("media_refresh_requests", {})
        accounting = progress.get("media_refresh_accounting", {})
        if not isinstance(requests, dict) or not isinstance(accounting, dict):
            return
        changed = False
        for video_id, state in requests.items():
            details = accounting.get(video_id)
            if (
                state == "SENDING"
                and isinstance(details, dict)
                and details.get("task_attempt") != lease.attempt
            ):
                finalize_interrupted_media_refresh_operation(
                    conn, operation_id=str(details["operation_id"])
                )
                requests[video_id] = "BLOCKED"
                changed = True
        if changed:
            conn.execute(
                "UPDATE viral_refresh_tasks SET checkpoint_json=%s WHERE id=%s",
                (json.dumps(progress, ensure_ascii=False), lease.id),
            )


def _mark_media_refresh(
    lease: ViralRefreshLease,
    video_id: str,
    state: str,
    *,
    succeeded: bool = False,
    original_url: str | None = None,
    refreshed_url: str | None = None,
) -> None:
    """发送标记和平台成本同事务落库，未知结果不得自动再次请求。"""
    unresolved = False
    with _connection() as conn:
        _require_lease(conn, lease)
        row = conn.execute(
            "SELECT checkpoint_json FROM viral_refresh_tasks WHERE id=%s", (lease.id,)
        ).fetchone()
        progress = json.loads(row["checkpoint_json"] or "{}")
        requests = progress.get("media_refresh_requests")
        accounting = progress.get("media_refresh_accounting")
        results = progress.get("media_refresh_results")
        if not isinstance(requests, dict):
            requests = {}
        if not isinstance(accounting, dict):
            accounting = {}
        if not isinstance(results, dict):
            results = {}
        previous = requests.get(video_id)
        details = accounting.get(video_id)
        if state == "SENDING" and previous in ("SENDING", "BLOCKED"):
            # 接管旧任务时将已发送但无结果的费用留为未知，不能误记零成本。
            if previous == "SENDING" and isinstance(details, dict):
                if details.get("task_attempt") != lease.attempt:
                    finalize_interrupted_media_refresh_operation(
                        conn, operation_id=str(details["operation_id"])
                    )
                    requests[video_id] = "BLOCKED"
            unresolved = True
        elif state == "SENDING":
            operation_id = accept_platform_operation(
                conn,
                service="link_resolution",
                source_id=str(uuid4()),
                api_metadata={
                    "api_type": "viral_media_refresh",
                    "video_platform": lease.platform,
                    "video_id": video_id,
                    "refresh_task_id": lease.id,
                    "refresh_task_attempt": str(lease.attempt),
                },
            )
            attempt_id = begin_attempt(conn, operation_id=operation_id, attempt_key="request")
            accounting[video_id] = {
                "operation_id": operation_id,
                "attempt_id": attempt_id,
                "task_attempt": lease.attempt,
            }
            requests[video_id] = "SENDING"
            results.pop(video_id, None)
        else:
            if previous != "SENDING" or not isinstance(details, dict):
                raise ViralSourceError("视频地址解析记录已更新，请重新执行。")
            conn.execute(
                "SELECT id FROM billing_operations WHERE id=%s FOR UPDATE",
                (str(details["operation_id"]),),
            )
            complete_attempt(
                conn, attempt_id=str(details["attempt_id"]), usage=1 if succeeded else None
            )
            finish_operation(
                conn,
                operation_id=str(details["operation_id"]),
                units=1 if succeeded else 0,
                succeeded=succeeded,
            )
            requests[video_id] = state
            if succeeded and refreshed_url:
                # 与视频表相同的临时直链只保存在服务端检查点，不写日志。
                results[video_id] = {"original_url": original_url, "play_url": refreshed_url}
        progress["media_refresh_requests"] = requests
        progress["media_refresh_accounting"] = accounting
        progress["media_refresh_results"] = results
        conn.execute(
            "UPDATE viral_refresh_tasks SET checkpoint_json=%s WHERE id=%s",
            (json.dumps(progress, ensure_ascii=False), lease.id),
        )
    if unresolved:
        raise ViralLinkError(
            409,
            "VIRAL_MEDIA_REFRESH_UNCERTAIN",
            "上次视频地址解析结果待核对，请确认后重新执行。",
            retryable=False,
            uncertain=True,
        )


def _can_refresh_douyin(video: ViralVideo) -> bool:
    return (
        video.platform == PLATFORM_DOUYIN
        and video.video_id.isascii()
        and video.video_id.isdecimal()
        and 15 <= len(video.video_id) <= 22
    )


def _refresh_douyin_play_url(client: DouyidouLinkClient, video: ViralVideo) -> ViralVideo | None:
    """重解析失效的直链，只接受与原视频身份一致的结果。"""
    if not _can_refresh_douyin(video):
        return None
    started = time.monotonic()
    try:
        resolved = client.resolve(
            DOUYIN_CANONICAL_URL_TEMPLATE.format(video_id=video.video_id), purpose="replica"
        )
    except ViralLinkError as exc:
        cause = exc.__cause__
        outcome: Outcome
        if isinstance(cause, HTTPError):
            outcome = "PROVIDER_ERROR"
        elif isinstance(cause, TimeoutError) or (
            isinstance(cause, URLError) and isinstance(cause.reason, TimeoutError)
        ):
            outcome = "TIMEOUT"
        elif isinstance(cause, (URLError, OSError)):
            outcome = "NETWORK_ERROR"
        else:
            outcome = "PROVIDER_ERROR"
        record_external_call(
            provider="douyidou",
            endpoint="api/parse",
            method="GET",
            url=None,
            outcome=outcome,
            error_code=exc.code,
            exception_type=type(exc).__name__,
            latency_ms=round((time.monotonic() - started) * 1000),
        )
        raise
    valid = (
        resolved.platform == video.platform
        and resolved.video_id == video.video_id
        and bool(resolved.video_url)
    )
    record_external_call(
        provider="douyidou",
        endpoint="api/parse",
        method="GET",
        url=None,
        outcome="SUCCEEDED" if valid else "PARSE_ERROR",
        error_code=None if valid else "VIRAL_LINK_MEDIA_MISMATCH",
        latency_ms=round((time.monotonic() - started) * 1000),
    )
    return replace(video, play_url=resolved.video_url) if valid else None


def _store_douyin_play_url(
    original: ViralVideo, refreshed: ViralVideo, *, lease: ViralRefreshLease, storage_uri: str
) -> ViralVideo | None:
    """归档成功后只回写直链，并校验该归档结果仍是当前版本。"""
    for attempt in range(5):
        try:
            with _connection() as conn:
                # 避免与导入、任务接管的不同加锁顺序形成无限等待。
                conn.execute("SET LOCAL lock_timeout = '250ms'")
                media = conn.execute(
                    "SELECT id FROM viral_media_preparations WHERE platform=%s AND video_id=%s "
                    "AND media_kind='video' AND status='SUCCEEDED' AND storage_uri=%s FOR UPDATE",
                    (original.platform, original.video_id, storage_uri),
                ).fetchone()
                if media is None:
                    raise ViralMediaLeaseLost("素材记录已更新，请重新读取。")
                current = conn.execute(
                    "SELECT play_url FROM viral_videos "
                    "WHERE platform=%s AND video_id=%s FOR UPDATE",
                    (original.platform, original.video_id),
                ).fetchone()
                if current is None:
                    return None
                _require_lease(conn, lease)
                if (
                    viral_video_availability(
                        conn, platform=original.platform, video_id=original.video_id
                    )
                    != "available"
                ):
                    return None
                # 解析期间若有新导入，以新导入的地址为准，不能覆盖并发更新。
                if current["play_url"] == original.play_url:
                    conn.execute(
                        "UPDATE viral_videos SET play_url=%s, updated_at=CURRENT_TIMESTAMP "
                        "WHERE platform=%s AND video_id=%s AND play_url IS NOT DISTINCT FROM %s",
                        (
                            refreshed.play_url,
                            original.platform,
                            original.video_id,
                            original.play_url,
                        ),
                    )
                latest = get_viral_video(
                    conn, platform=original.platform, video_id=original.video_id
                )
            return latest if latest is not None and latest.play_url else None
        except (psycopg.errors.LockNotAvailable, psycopg.errors.DeadlockDetected) as exc:
            if attempt == 4:
                raise ViralMediaBusy("素材正在更新，请稍后重试。") from exc
            time.sleep(0.1)
    raise AssertionError("unreachable")


def _run_single_archive(lease: ViralRefreshLease, storage: StorageAdapter, video_id: str) -> None:
    """Prepare an existing item only; never search, bill customers or replace a list."""
    with _keep_lease(lease) as check, video_billing_context(lease.platform, video_id):
        check()
        _settle_interrupted_media_refreshes(lease)
        with _connection() as conn:
            video = get_viral_video(conn, platform=lease.platform, video_id=video_id)
            if (
                video is None
                or viral_video_availability(conn, platform=lease.platform, video_id=video_id)
                != "available"
            ):
                raise ViralSourceError("视频已删除、下架或屏蔽，停止准备。")
            # 抖音、小红书与视频号分享链接均可直下，只有视频号加密详情需要数据源客户端。
            client = (
                viral_source_client_from_settings(conn)
                if video.platform == PLATFORM_WECHAT and not (video.native or {}).get("share_url")
                else None
            )
            wechat_link_client = wechat_link_client_from_settings_or_none(conn)
            task = conn.execute(
                "SELECT collection_config_json FROM viral_refresh_tasks WHERE id=%s", (lease.id,)
            ).fetchone()
            task_config = json.loads(task["collection_config_json"] or "{}") if task else {}
        refreshed_video: ViralVideo | None = None
        original_for_refresh = video

        def reuse_resolved_video(current: ViralVideo) -> ViralVideo:
            nonlocal refreshed_video, original_for_refresh
            with _connection() as conn:
                _require_lease(conn, lease)
                row = conn.execute(
                    "SELECT checkpoint_json FROM viral_refresh_tasks WHERE id=%s", (lease.id,)
                ).fetchone()
                progress = json.loads(row["checkpoint_json"] or "{}")
                latest = get_viral_video(conn, platform=current.platform, video_id=current.video_id)
            if latest and latest.play_url and latest.play_url != current.play_url:
                original_for_refresh = latest
                refreshed_video = latest
                return latest
            results = progress.get("media_refresh_results", {})
            saved = results.get(current.video_id) if isinstance(results, dict) else None
            if (
                isinstance(saved, dict)
                and saved.get("original_url") == current.play_url
                and isinstance(saved.get("play_url"), str)
                and saved["play_url"]
            ):
                original_for_refresh = current
                refreshed_video = replace(current, play_url=saved["play_url"])
                return refreshed_video
            return current

        def recover_video(current: ViralVideo) -> ViralVideo | None:
            nonlocal refreshed_video, original_for_refresh
            if not _can_refresh_douyin(current):
                return None
            check()
            try:
                with _connection() as conn:
                    latest = get_viral_video(
                        conn, platform=current.platform, video_id=current.video_id
                    )
                    if latest is None:
                        return None
                    original_for_refresh = latest
                    link_client = douyidou_link_client_from_settings(conn)
            except ViralLinkError as exc:
                if exc.uncertain or not exc.retryable:
                    raise
                return None
            _mark_media_refresh(lease, current.video_id, "SENDING")
            try:
                refreshed_video = _refresh_douyin_play_url(link_client, current)
            except ViralLinkError as exc:
                blocked = exc.uncertain or not exc.retryable
                _mark_media_refresh(lease, current.video_id, "BLOCKED" if blocked else "DONE")
                if blocked:
                    raise
                return None
            except Exception as exc:
                _mark_media_refresh(lease, current.video_id, "BLOCKED")
                raise ViralLinkError(
                    503,
                    "VIRAL_MEDIA_REFRESH_UNCERTAIN",
                    "视频地址解析结果待核对，请确认后再操作。",
                    retryable=False,
                    uncertain=True,
                ) from exc
            _mark_media_refresh(
                lease,
                current.video_id,
                "DONE",
                succeeded=True,
                original_url=original_for_refresh.play_url,
                refreshed_url=refreshed_video.play_url if refreshed_video else None,
            )
            return refreshed_video

        pipeline = ViralMediaPipeline(
            client=client,
            storage=storage,
            shared=True,
            refresh_video=reuse_resolved_video if video.platform == PLATFORM_DOUYIN else None,
            recover_video=recover_video if video.platform == PLATFORM_DOUYIN else None,
            failed_wait_seconds=0 if task_config.get("kind") == "archive_batch" else 7,
            cancellation_check=check,
            wechat_link_client=wechat_link_client,
        )
        media = pipeline.fetch(video, prefer="video")
        if refreshed_video is None and video.platform == PLATFORM_DOUYIN:
            # 发布后、地址回写前中断时，下轮缓存命中也要补上地址更新。
            reuse_resolved_video(video)
        if refreshed_video is not None:
            latest = _store_douyin_play_url(
                original_for_refresh, refreshed_video, lease=lease, storage_uri=media.storage_uri
            )
            if latest is None:
                raise ViralSourceError("视频已删除、下架或屏蔽，停止准备。")
            video = latest
        # 首次下载取得的详情（视频号解密详情等）要跨过下面的音频回访保留：
        # pipeline.fetch 每次都会把 detail 清空，音频回访即使等价命中缓存也会
        # 抹掉它，统计回写就会丢失（Codex 收尾评审 P2）。
        media_detail = pipeline.detail
        check()
        # 顺带归档音频形态（2026-11 改造 backlog）：抖音/小红书的文案导入优先
        # 消费音频归档，转存时提前备好可省去转写链路的整片下载与服务端抽音轨；
        # 视频号恒视频形态（_resolve_kind 恒 video）与缺 audio_url 的条目自然
        # 落回刚归档的视频对象，等价缓存命中。只捕获 ViralSourceError（上游
        # 媒体不可得/准备未就绪）——存储或数据库异常照常抛出使任务失败进入
        # 既有重试；音频失败被降级后任务仍成功，缺音频由首次文案导入按需准备。
        try:
            pipeline.fetch(video, prefer="audio")
        except ViralSourceError as exc:
            logging.getLogger(__name__).warning(
                "单条转存顺带归档音频失败（视频形态已就绪）%s/%s: %s",
                lease.platform,
                video_id,
                exc,
            )
        pipeline.detail = media_detail
        check()
        cover = CoverEnricher(
            storage=storage, fetcher=UrlFetcher(max_bytes=10 * 1024 * 1024), metered=True
        ).enrich(video)
        check()
        with _connection() as conn:
            _require_lease(conn, lease)
            if get_viral_video(conn, platform=lease.platform, video_id=video_id) is None:
                raise ViralSourceError("视频已删除，停止归档。")
            if pipeline.detail is not None:
                update_viral_statistics(
                    conn, platform=lease.platform, video_id=video_id, detail=pipeline.detail
                )
            if cover.cover_key:
                update_viral_cover(
                    conn, platform=lease.platform, video_id=video_id, cover_key=cover.cover_key
                )
            # 元数据快照与媒体对象同批落存储（生产线 #48 批次）；数据库仍是唯一
            # 真源。2026-11-11 共享缓存退役后快照不再携带共享文案字段。
            from app.viral_push import write_viral_metadata_snapshot

            refreshed = get_viral_video(conn, platform=lease.platform, video_id=video_id)
            if refreshed is not None:
                write_viral_metadata_snapshot(storage, refreshed)
            # 单条转存与批量转存同样标记素材就绪（ADM-C 批次）：此前只有批量路径
            # 记录，单条准备的素材在漏斗"素材就绪"阶段永久缺失。
            record_content_stage(conn, platform=lease.platform, video_id=video_id, stage="prepared")
        if video.cover_url and not cover.cover_key:
            raise ViralSourceError("视频已转存，封面尚未完成，请重试。")


def run_viral_collection(lease: ViralRefreshLease, storage: StorageAdapter) -> None:
    with _connection() as conn:
        _require_lease(conn, lease)
        row = conn.execute(
            "SELECT collection_config_json,checkpoint_json FROM viral_refresh_tasks WHERE id=%s",
            (lease.id,),
        ).fetchone()
        config, progress = json.loads(row[0]), json.loads(row[1])
    if config.get("kind") == "single_archive":
        _run_single_archive(lease, storage, str(config["video_id"]))
        # 采集即发布：管理端粘贴链接时登记 publish_after 意图，转存完成即
        # 自动上架（列表 + 首页置顶）。与 archive_batch 的 publish_after 同一
        # 纪律：advisory lock 串行化、租约校验、守卫未过不算任务失败。
        publish_after = config.get("publish_after") or None
        if publish_after:
            with _connection() as conn:
                conn.execute("SELECT pg_advisory_xact_lock(%s)", (202609260001,))
                _require_lease(conn, lease)
                from app.viral_push import auto_publish_viral_video

                auto_publish_viral_video(
                    conn,
                    platform=lease.platform,
                    video_id=str(config["video_id"]),
                    actor_user_id=str(publish_after.get("actor_user_id") or ""),
                    reason=str(publish_after.get("reason") or ""),
                    request_id=str(publish_after.get("request_id") or ""),
                )
        return
    if config.get("kind") == "archive_batch":
        # 批量素材准备（方案 P1 内容模块 C-2）：逐条复用单条转存管线；
        # 带 feature_after 意图时，每条就绪后当场上首页并按原始操作人写审计
        # ——操作发生在后台，但责任落在发起「上首页」的那个人身上。
        # publish_after（人工「推送到前端」）同理：就绪即上架到客户列表。
        feature_after = config.get("feature_after") or None
        publish_after = config.get("publish_after") or None
        failed = []
        nonretryable_error: ViralLinkError | None = None
        retryable_failure = False
        for video_id in config.get("video_ids", []):
            try:
                _run_single_archive(lease, storage, str(video_id))
            except Exception as exc:
                # 不确定的解析不能自动重试，但其余独立视频仍需如实处理和记录。
                if isinstance(exc, ViralLinkError) and (exc.uncertain or not exc.retryable):
                    if nonretryable_error is None or exc.uncertain:
                        nonretryable_error = exc
                else:
                    retryable_failure = True
                # 逐条保留结果；租约丢失必须中止，普通素材失败不阻塞后面的条目。
                with _connection() as conn:
                    _require_lease(conn, lease)
                    if not viral_runtime_controls(conn)[0] and not _manual_kind(conn, lease):
                        raise
                    failed.append(str(video_id))
                    progress["failed_video_ids"] = failed
                    _checkpoint(conn, lease, progress)
                continue
            if not feature_after and not publish_after:
                with _connection() as conn:
                    record_content_stage(
                        conn, platform=lease.platform, video_id=str(video_id), stage="prepared"
                    )
                continue
            if publish_after:
                with _connection() as conn:
                    conn.execute("SELECT pg_advisory_xact_lock(%s)", (202609260001,))
                    _require_lease(conn, lease)
                    from app.viral_push import mark_viral_push_published

                    published = mark_viral_push_published(
                        conn,
                        platform=lease.platform,
                        video_id=str(video_id),
                        actor_user_id=str(publish_after.get("actor_user_id") or ""),
                        reason=str(publish_after.get("reason") or ""),
                        request_id=str(publish_after.get("request_id") or ""),
                    )
                    if not published:
                        # 守卫未过（如就绪前被下架）：如实留在未上架态，不算任务失败。
                        continue
                    record_content_stage(
                        conn, platform=lease.platform, video_id=str(video_id), stage="prepared"
                    )
                continue
            assert feature_after is not None  # 上方分支排除后仅剩 feature 意图路径
            with _connection() as conn:
                conn.execute("SELECT pg_advisory_xact_lock(%s)", (202609260001,))
                _require_lease(conn, lease)
                applied = conn.execute(
                    "UPDATE viral_videos v SET homepage_featured=1, collection_published=1, "
                    "homepage_featured_at=COALESCE(homepage_featured_at,now()) "
                    "WHERE platform=%s AND video_id=%s AND deleted_at IS NULL "
                    "AND homepage_intent_version=%s AND homepage_featured=0 "
                    "AND (homepage_ends_at IS NULL OR homepage_ends_at>now()) "
                    "AND NOT EXISTS (SELECT 1 FROM viral_video_visibility vis WHERE "
                    "vis.platform=v.platform AND vis.video_id=v.video_id "
                    "AND vis.status!='AVAILABLE') "
                    "AND EXISTS (SELECT 1 FROM viral_media_preparations m "
                    "WHERE m.platform=v.platform "
                    "AND m.video_id=v.video_id AND m.media_kind='video' AND m.status='SUCCEEDED' "
                    "AND m.storage_uri IS NOT NULL) "
                    "AND (COALESCE(v.cover_url,'')='' OR v.cover_key IS NOT NULL) "
                    "RETURNING platform, video_id",
                    (
                        lease.platform,
                        str(video_id),
                        int(feature_after.get("versions", {}).get(str(video_id), 0)),
                    ),
                ).fetchone()
                if applied is None:
                    continue
                record_content_stage(
                    conn, platform=lease.platform, video_id=str(video_id), stage="prepared"
                )
                record_content_stage(
                    conn, platform=lease.platform, video_id=str(video_id), stage="homepage"
                )
                conn.execute(
                    """INSERT INTO audit_logs(
                        id,actor_user_id,action,entity_type,entity_id,metadata_json)
                    VALUES(%s,%s,'viral_video.curation','viral_video',%s,%s)""",
                    (
                        str(uuid4()),
                        str(feature_after.get("actor_user_id") or ""),
                        f"{lease.platform}:{video_id}",
                        json.dumps(
                            {
                                "action": "feature",
                                "reason": str(feature_after.get("reason") or ""),
                                "request_id": str(feature_after.get("request_id") or ""),
                                "prepared_then_featured": True,
                            },
                            ensure_ascii=False,
                        ),
                    ),
                )
        if nonretryable_error is not None:
            if retryable_failure:
                raise ViralBatchPartialError(uncertain=nonretryable_error.uncertain)
            raise nonretryable_error
        if failed:
            raise ViralSourceError(f"有 {len(failed)} 条素材准备失败，其他就绪视频已处理；可重试。")
        return
    with _connection() as conn:
        _require_lease(conn, lease)
        # Compatibility for already-queued development tasks: establish the batch
        # once under the task lease, never use the reused refresh-task ID as a bill.
        if "billing_batch_id" not in config:
            config["billing_batch_id"] = create_collection_batch(
                conn,
                platform=lease.platform,
                config=config,
                user_ids=eligible_collection_users(conn),
                # 旧检查点可能已跳过来源写入，不能因为本次新建账单就宣称统计完整。
                stats_complete=not bool(progress.get("keywords") or progress.get("prepared")),
            )
            conn.execute(
                "UPDATE viral_refresh_tasks SET collection_config_json=%s WHERE id=%s",
                (json.dumps(config), lease.id),
            )
        client = viral_source_client_from_settings(conn)
        # 质量门槛按本轮开始时的设置执行；跑到一半改设置不影响这一轮的一致性。
        quality_rules = load_quality_rules(conn)
    entries = [ViralKeywordConfig.model_validate(item) for item in config.get("keywords", [])]
    if not entries:
        raise ViralSourceError("请先在管理后台配置采集关键词。")
    keywords_done = progress.setdefault("keywords", {})
    prepared = set(progress.setdefault("prepared", []))
    detailed = set(progress.setdefault("detailed", []))
    failed_video_ids = set(progress.setdefault("failed_video_ids", []))
    failures = 0
    with _keep_lease(lease) as check, collection_billing_context(config["billing_batch_id"]):
        for index, entry in enumerate(entries):
            check()
            step = str(index)
            if step not in keywords_done:
                with _connection() as conn:
                    _require_lease(conn, lease)
                    conn.execute(
                        "INSERT INTO viral_keyword_runs(batch_id,platform,keyword,status) "
                        "VALUES(%s,%s,%s,'RUNNING') ON CONFLICT(batch_id,platform,keyword) "
                        "DO UPDATE SET status='RUNNING',started_at=clock_timestamp(),"
                        "finished_at=NULL,failure_code=NULL,"
                        "attempt_count=viral_keyword_runs.attempt_count+1,video_count=NULL",
                        (config["billing_batch_id"], entry.platform, entry.keyword),
                    )
                try:
                    videos = (
                        client.douyin_search(keyword=entry.keyword, category=entry.category)
                        if entry.platform == "douyin"
                        else client.wechat_search(keyword=entry.keyword, category=entry.category)
                    )
                except Exception as cause:
                    with _connection() as conn:
                        _require_lease(conn, lease)
                        conn.execute(
                            "UPDATE viral_keyword_runs SET status='FAILED',"
                            "finished_at=clock_timestamp(),failure_code=%s "
                            "WHERE batch_id=%s AND platform=%s AND keyword=%s",
                            (
                                classify_collection_failure(cause),
                                config["billing_batch_id"],
                                entry.platform,
                                entry.keyword,
                            ),
                        )
                    failures += 1
                    continue
                # 先过质量门槛再取每词上限：上限应当数「合格的」，而不是让被拒的
                # 视频占掉名额、合格的反而被截掉。被拒的既不入池也不下载。
                videos = [video for video in videos if not quality_rules.rejects(video)][
                    : entry.limit if entry.limit is not None else int(config["limit"])
                ]
                keywords_done[step] = list(dict.fromkeys(video.video_id for video in videos))
                with _connection() as conn:
                    _require_lease(conn, lease)
                    conn.execute(
                        "UPDATE viral_keyword_runs SET status='SUCCEEDED',"
                        "finished_at=clock_timestamp(),failure_code=NULL,"
                        "video_count=%s WHERE batch_id=%s AND platform=%s AND keyword=%s",
                        (len(videos), config["billing_batch_id"], entry.platform, entry.keyword),
                    )
                    inserted = upsert_viral_videos(conn, videos, commit=False)
                    for collected in videos:
                        record_content_source(
                            conn,
                            batch_id=str(config["billing_batch_id"]),
                            source_kind="collection",
                            platform=entry.platform,
                            video_id=collected.video_id,
                            keyword=entry.keyword,
                            is_new=(entry.platform, collected.video_id) in inserted,
                        )
                    _checkpoint(conn, lease, progress)
            for video_id in keywords_done[step]:
                if video_id in prepared:
                    continue
                check()
                with _connection() as conn:
                    video = get_viral_video(conn, platform=entry.platform, video_id=video_id)
                    available = viral_video_availability(
                        conn, platform=entry.platform, video_id=video_id
                    )
                if video is None or available != "available":
                    # An administrator can remove a staged item while collecting.
                    # Its tombstone prevents both re-publication and re-download.
                    continue
                pipeline = ViralMediaPipeline(
                    client=client, storage=storage, shared=True, cancellation_check=check
                )
                try:
                    with video_billing_context(entry.platform, video_id):
                        cover = CoverEnricher(
                            storage=storage,
                            fetcher=UrlFetcher(max_bytes=10 * 1024 * 1024),
                            metered=True,
                        ).enrich(video)
                        check()
                        if entry.platform == "wechat_channels" and video_id not in detailed:
                            detail = client.wechat_video_detail(
                                export_id=str(video.native.get("export_id") or ""),
                                object_nonce_id=video.native.get("object_nonce_id") or None,
                            )
                            with _connection() as conn:
                                _require_lease(conn, lease)
                                update_viral_statistics(
                                    conn, platform=entry.platform, video_id=video_id, detail=detail
                                )
                                next_detailed = detailed | {video_id}
                                next_progress = {**progress, "detailed": sorted(next_detailed)}
                                _checkpoint(conn, lease, next_progress)
                            detailed, progress = next_detailed, next_progress
                        check()
                        pipeline.fetch(video, prefer="video")
                        check()
                        with _connection() as conn:
                            _require_lease(conn, lease)
                            if cover.cover_key:
                                update_viral_cover(
                                    conn,
                                    platform=entry.platform,
                                    video_id=video_id,
                                    cover_key=cover.cover_key,
                                )
                            if video.cover_url and not cover.cover_key:
                                raise ViralSourceError("封面转存尚未完成。")
                            next_prepared = prepared | {video_id}
                            failed_video_ids.discard(video_id)
                            record_content_stage(
                                conn, platform=entry.platform, video_id=video_id, stage="prepared"
                            )
                            progress.setdefault("video_outcomes", {})[video_id] = {
                                "status": "SUCCEEDED",
                                "failure_code": None,
                                "title": video.title,
                            }
                            next_progress = {
                                **progress,
                                "prepared": sorted(next_prepared),
                                "failed_video_ids": sorted(failed_video_ids),
                            }
                            _checkpoint(conn, lease, next_progress)
                        prepared, progress = next_prepared, next_progress
                except Exception as cause:
                    failures += 1
                    progress.setdefault("video_outcomes", {})[video_id] = {
                        "status": "FAILED",
                        "failure_code": classify_collection_failure(cause),
                        "title": video.title,
                    }
                    failed_video_ids.add(video_id)
                    with _connection() as conn:
                        _require_lease(conn, lease)
                        progress = {**progress, "failed_video_ids": sorted(failed_video_ids)}
                        _checkpoint(conn, lease, progress)
                finally:
                    if pipeline.detail is not None:
                        with _connection() as conn:
                            _require_lease(conn, lease)
                            update_viral_statistics(
                                conn,
                                platform=entry.platform,
                                video_id=video_id,
                                detail=pipeline.detail,
                            )
        check()
        if failures:
            raise ViralSourceError(
                f"有 {failures} 项采集或转存尚未完成，后台将重试；保留上轮列表。"
            )
        with _connection() as conn:
            _require_lease(conn, lease)
            conn.execute(
                "UPDATE viral_videos SET collection_published=0 WHERE platform=%s",
                (lease.platform,),
            )
            for video_id in prepared:
                conn.execute(
                    "UPDATE viral_videos SET collection_published=1 "
                    "WHERE platform=%s AND video_id=%s",
                    (lease.platform, video_id),
                )
            stamp = datetime.now(UTC).isoformat()
            window = datetime.fromtimestamp(int(config["window_end"]), UTC).isoformat()
            for sort, value in (("hot", stamp), ("latest", stamp), ("weekly_window", window)):
                conn.execute(
                    """INSERT INTO viral_fetch_state(platform,sort,fetched_at) VALUES(%s,%s,%s)
                    ON CONFLICT(platform,sort) DO UPDATE SET fetched_at=excluded.fetched_at""",
                    (lease.platform, sort, value),
                )
