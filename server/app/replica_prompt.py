"""长视频复刻提示词（replica_prompt）任务族：入队 / 租约 / 完成 / 失败.

4–180 秒源视频 → 一段完整复刻提示词 + 逐句口播 + 全片摘要（无分镜卡）。
与 analysis_tasks 刻意保持两个任务族：长视频单次 provider 调用可达十分钟以上，
需要独立的租约与调用超时；完成载荷也没有分镜卡。隔离演进，避免触碰复刻
付费主链路（4–30 秒拆解）的状态机与结算口径。

v1 不建 attempt 历史表：失败原因保留在任务行上直到下一次尝试覆盖；若需要
attempt 级留痕，参照 analysis_task_attempts 的迁移与读写补齐。
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import uuid4

from fastapi import HTTPException

from app.analysis import (
    REPLICA_PROMPT_KIND,
    AnalysisProviderFailed,
    UrllibApilioChatTransport,
    VideoAnalysisProvider,
    analysis_task_context,
    insert_version,
    parse_replica_prompt_payload,
)
from app.analysis_routes import (
    analysis_provider_error,
    get_video_analysis_provider,
    load_task_actor,
    signed_video_url_for_provider,
)
from app.auth import CurrentUser, Database
from app.db_portable import BusinessConnection
from app.media import (
    DURATION_ROUNDING_TOLERANCE_SECONDS,
    MAX_REPLICA_PROMPT_DURATION_SECONDS,
    is_reference_video_asset,
)
from app.permissions import (
    require_not_auditor,
    require_project_access,
    write_audit,
)
from app.storage import StorageAdapter

logger = logging.getLogger(__name__)

# 与上传门禁同口径：180 秒 + 0.1 秒舍入容差。
MAX_TASK_DURATION_SECONDS = (
    MAX_REPLICA_PROMPT_DURATION_SECONDS + DURATION_ROUNDING_TOLERANCE_SECONDS
)
# 15 秒源片实测单次调用 71–91 秒，按比例 180 秒可到 15 分钟量级；租约必须
# 覆盖「传输 + 调用 + 落库」全程，且大于 provider 调用超时。
REPLICA_PROMPT_TASK_LEASE_MINUTES = 25
REPLICA_PROMPT_TIMEOUT_ENV = "VIDEO_REPLICA_REPLICA_PROMPT_TIMEOUT_SECONDS"
DEFAULT_PROVIDER_TIMEOUT_SECONDS = 900.0
# 签名 URL 必须活过整个 provider 调用；拆解链路的 15 分钟在长任务上不够。
REPLICA_PROMPT_URL_EXPIRES_MINUTES = 30

ERROR_INTERRUPTED = "REPLICA_PROMPT_WORKER_INTERRUPTED"
ERROR_CANCELLED = "REPLICA_PROMPT_TASK_CANCELLED"
ERROR_FAILED = "REPLICA_PROMPT_WORKER_FAILED"


def provider_timeout_seconds() -> float:
    """长任务 provider 调用超时；默认 900 秒，环境变量可覆盖（配置惯例 *_ENV）."""
    raw = (os.environ.get(REPLICA_PROMPT_TIMEOUT_ENV) or "").strip()
    if not raw:
        return DEFAULT_PROVIDER_TIMEOUT_SECONDS
    try:
        timeout = float(raw)
    except ValueError:
        logger.warning("invalid %s=%r; falling back to default", REPLICA_PROMPT_TIMEOUT_ENV, raw)
        return DEFAULT_PROVIDER_TIMEOUT_SECONDS
    return max(timeout, 60.0)


def build_long_video_provider(conn: Database) -> VideoAnalysisProvider:
    """与拆解同一套 apilio 配置，仅注入更长的调用超时."""
    return get_video_analysis_provider(
        conn,
        transport=UrllibApilioChatTransport(timeout_seconds=provider_timeout_seconds()),
    )


@dataclass(frozen=True)
class ReplicaPromptTaskLease:
    id: str
    project_id: str
    asset_id: str
    created_by_user_id: str
    duration_seconds: float
    worker_id: str
    attempt: int = 1
    request_id: str | None = None


@dataclass(frozen=True)
class ReplicaPromptTaskWork:
    lease: ReplicaPromptTaskLease
    provider: VideoAnalysisProvider
    video_uri: str
    asset_uri: str
    media_info: dict[str, Any]
    storage: StorageAdapter | None = None


def load_replica_prompt_task(conn: BusinessConnection, task_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM replica_prompt_tasks WHERE id = %s", (task_id,)).fetchone()
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "REPLICA_PROMPT_TASK_NOT_FOUND"},
        )
    return cast(sqlite3.Row, row)


def find_latest_replica_prompt_task(
    conn: BusinessConnection,
    *,
    project_id: str,
    asset_id: str,
) -> sqlite3.Row | None:
    row = conn.execute(
        """
        SELECT * FROM replica_prompt_tasks
        WHERE project_id = %s AND asset_id = %s
        ORDER BY created_at DESC, id DESC
        LIMIT 1
        """,
        (project_id, asset_id),
    ).fetchone()
    return None if row is None else cast(sqlite3.Row, row)


def find_latest_replica_prompt_version(
    conn: BusinessConnection,
    *,
    project_id: str,
) -> sqlite3.Row | None:
    row = conn.execute(
        """
        SELECT id, project_id, asset_id, kind, version_number, payload_json,
               created_by_user_id, created_at
        FROM versions
        WHERE project_id = %s AND kind = %s
        ORDER BY version_number DESC
        LIMIT 1
        """,
        (project_id, REPLICA_PROMPT_KIND),
    ).fetchone()
    return None if row is None else cast(sqlite3.Row, row)


def enqueue_replica_prompt_task(
    conn: BusinessConnection,
    *,
    project_id: str,
    asset_id: str,
    created_by_user_id: str,
    duration_seconds: float,
    request_id: str | None = None,
) -> tuple[sqlite3.Row, bool]:
    """入队一个长视频拆解任务；同资产的活跃任务直接复用，不重复计费."""
    existing = conn.execute(
        """
        SELECT * FROM replica_prompt_tasks
        WHERE project_id = %s AND asset_id = %s
          AND status IN ('PENDING', 'RUNNING')
        ORDER BY created_at DESC, id DESC
        LIMIT 1
        """,
        (project_id, asset_id),
    ).fetchone()
    if existing is not None:
        return cast(sqlite3.Row, existing), False

    task_id = str(uuid4())
    conn.execute(
        """
        INSERT INTO replica_prompt_tasks (
            id, project_id, asset_id, created_by_user_id,
            duration_seconds, status, request_id
        ) VALUES (%s, %s, %s, %s, %s, 'PENDING', %s)
        ON CONFLICT DO NOTHING
        """,
        (task_id, project_id, asset_id, created_by_user_id, duration_seconds, request_id),
    )
    inserted = conn.execute(
        "SELECT * FROM replica_prompt_tasks WHERE id = %s",
        (task_id,),
    ).fetchone()
    if inserted is not None:
        from app.usage_billing import accept_operation

        # v1 与拆解同价（analysis 服务目录、每次 1 单位）；切按秒计价时
        # 只改 units 与服务目录，入队/结算的形状不变。
        accept_operation(
            conn, user_id=created_by_user_id, service="analysis", source_id=task_id, units=1
        )
        return cast(sqlite3.Row, inserted), True

    concurrent = conn.execute(
        """
        SELECT * FROM replica_prompt_tasks
        WHERE project_id = %s AND asset_id = %s
          AND status IN ('PENDING', 'RUNNING')
        ORDER BY created_at DESC, id DESC
        LIMIT 1
        """,
        (project_id, asset_id),
    ).fetchone()
    if concurrent is None:
        raise RuntimeError("replica prompt task enqueue conflict left no active task")
    return cast(sqlite3.Row, concurrent), False


def acquire_replica_prompt_task(
    conn: BusinessConnection,
    *,
    worker_id: str,
) -> ReplicaPromptTaskLease | None:
    now = datetime.now(UTC)
    now_text = now.strftime("%Y-%m-%d %H:%M:%S")
    locked_until = (now + timedelta(minutes=REPLICA_PROMPT_TASK_LEASE_MINUTES)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    try:
        # 过期 RUNNING 视为 worker 中断：置为可重试失败，终态写不进历史表
        # （v1 无 attempt 历史），错误字段保留在任务行上供用户与客服查看。
        conn.execute(
            """
            UPDATE replica_prompt_tasks
            SET status = 'FAILED',
                error_code = %s,
                error_message_redacted = '长视频拆解任务执行中断，请重新开始。',
                retryable = 1,
                locked_by = NULL,
                locked_until = NULL,
                completed_at = %s,
                updated_at = %s
            WHERE status = 'RUNNING' AND locked_until IS NOT NULL AND locked_until <= %s
            """,
            (ERROR_INTERRUPTED, now_text, now_text, now_text),
        )
        # 认领前清掉 PENDING 行上残留的旧失败字段：新尝试从干净状态开始。
        conn.execute(
            """
            UPDATE replica_prompt_tasks
            SET error_code = NULL, error_message_redacted = NULL,
                failure_phase = NULL, retryable = 0
            WHERE status = 'PENDING'
              AND (error_code IS NOT NULL OR failure_phase IS NOT NULL)
            """,
        )
        row = conn.execute(
            """
            UPDATE replica_prompt_tasks
            SET status = 'RUNNING',
                attempt = attempt + 1,
                locked_by = %s,
                locked_until = %s,
                started_at = COALESCE(started_at, %s),
                updated_at = %s,
                error_code = NULL,
                error_message_redacted = NULL,
                failure_phase = NULL,
                retryable = 0
            WHERE id = (
                SELECT id FROM replica_prompt_tasks
                WHERE status = 'PENDING'
                ORDER BY created_at, id
                LIMIT 1
            ) AND status = 'PENDING'
            RETURNING *
            """,
            (worker_id, locked_until, now_text, now_text),
        ).fetchone()
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    if row is None:
        return None
    return ReplicaPromptTaskLease(
        id=str(row["id"]),
        project_id=str(row["project_id"]),
        asset_id=str(row["asset_id"]),
        created_by_user_id=str(row["created_by_user_id"]),
        duration_seconds=float(row["duration_seconds"]),
        worker_id=worker_id,
        attempt=int(row["attempt"]),
        request_id=None if row["request_id"] is None else str(row["request_id"]),
    )


def prepare_replica_prompt_task(
    conn: BusinessConnection,
    *,
    lease: ReplicaPromptTaskLease,
    storage: StorageAdapter,
    provider: VideoAnalysisProvider | None = None,
) -> ReplicaPromptTaskWork:
    """核验租约与素材状态，生成签名 URL；付费调用在调用方的事务外进行."""
    row = conn.execute(
        """
        SELECT task.status, task.locked_by, asset.project_id,
               asset.storage_uri, asset.sha256, asset.size_bytes, asset.metadata_json
        FROM replica_prompt_tasks AS task
        JOIN assets AS asset ON asset.id = task.asset_id
        WHERE task.id = %s
        """,
        (lease.id,),
    ).fetchone()
    if (
        row is None
        or str(row["status"]) != "RUNNING"
        or str(row["locked_by"]) != lease.worker_id
        or str(row["project_id"]) != lease.project_id
        or not str(row["sha256"])
        or int(row["size_bytes"]) <= 0
    ):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "REPLICA_PROMPT_TASK_INPUT_UNAVAILABLE",
                "message": "参考视频状态已变化，请重新上传后重试。",
                "retryable": True,
            },
        )
    resolved_provider = provider or build_long_video_provider(conn)
    asset_uri = str(row["storage_uri"])
    video_uri = asset_uri
    if resolved_provider.requires_https_video_url:
        video_uri = signed_video_url_for_provider(
            storage,
            asset_uri=asset_uri,
            expires_in=timedelta(minutes=REPLICA_PROMPT_URL_EXPIRES_MINUTES),
        )
    media_info: dict[str, Any] = {"duration_seconds": lease.duration_seconds}
    try:
        metadata = json.loads(str(row["metadata_json"] or "{}"))
    except (json.JSONDecodeError, ValueError):
        metadata = {}
    for key in ("fps", "resolution", "aspect_ratio"):
        if metadata.get(key) is not None:
            media_info[key] = metadata[key]
    conn.commit()
    return ReplicaPromptTaskWork(
        lease=lease,
        provider=resolved_provider,
        video_uri=video_uri,
        asset_uri=asset_uri,
        media_info=media_info,
        storage=storage,
    )


def perform_replica_prompt_task(
    work: ReplicaPromptTaskWork, *, on_provider_result: Any = None
) -> dict[str, Any]:
    """跑付费 provider 调用并把出参解析成 replica-prompt.v1 载荷."""
    with analysis_task_context(
        task_id=work.lease.id,
        project_id=work.lease.project_id,
        asset_id=work.lease.asset_id,
        attempt=work.lease.attempt,
    ):
        method = getattr(work.provider, "analyze_replica_prompt", None)
        if callable(method):
            response = method(
                video_uri=work.video_uri,
                duration_seconds=work.lease.duration_seconds,
                media_info=work.media_info,
            )
        else:
            # 只实现字节拆解协议的测试替身：走通用入口再按同一 schema 解析。
            response = work.provider.analyze(
                video_uri=work.video_uri,
                duration_seconds=work.lease.duration_seconds,
            )
        if on_provider_result is not None:
            on_provider_result()
        return parse_replica_prompt_payload(response.text)


def complete_replica_prompt_task(
    conn: BusinessConnection,
    *,
    work: ReplicaPromptTaskWork,
    payload: dict[str, Any],
) -> None:
    task = conn.execute(
        "SELECT status, locked_by, attempt FROM replica_prompt_tasks WHERE id = %s FOR UPDATE",
        (work.lease.id,),
    ).fetchone()
    if (
        task is None
        or str(task["status"]) != "RUNNING"
        or str(task["locked_by"]) != work.lease.worker_id
        or int(task["attempt"]) != work.lease.attempt
    ):
        # 租约已被取消/接管：迟到结果按 CAS 丢弃，绝不覆盖新事实。
        conn.rollback()
        return
    row = insert_version(
        conn,
        project_id=work.lease.project_id,
        asset_id=work.lease.asset_id,
        kind=REPLICA_PROMPT_KIND,
        created_by_user_id=work.lease.created_by_user_id,
        payload={
            **payload,
            "source_asset": {
                "id": work.lease.asset_id,
                "storage_uri": work.asset_uri,
            },
        },
        commit=False,
    )
    now_text = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        """
        UPDATE replica_prompt_tasks
        SET status = 'SUCCEEDED', result_version_id = %s,
            error_code = NULL, error_message_redacted = NULL,
            failure_phase = NULL, retryable = 0,
            locked_by = NULL, locked_until = NULL,
            completed_at = %s, updated_at = %s
        WHERE id = %s AND status = 'RUNNING' AND locked_by = %s
        """,
        (str(row["id"]), now_text, now_text, work.lease.id, work.lease.worker_id),
    )
    write_audit(
        conn,
        actor=load_task_actor(conn, work.lease.created_by_user_id),
        action="replica_prompt.task_succeeded",
        entity_type="replica_prompt_task",
        entity_id=work.lease.id,
        metadata={
            "project_id": work.lease.project_id,
            "asset_id": work.lease.asset_id,
            "version_id": str(row["id"]),
            "request_id": work.lease.request_id,
        },
        commit=False,
    )
    from app.usage_billing import finish_source

    finish_source(conn, work.lease.id, units=1, succeeded=True)
    conn.commit()


def fail_replica_prompt_task(
    conn: BusinessConnection,
    *,
    lease: ReplicaPromptTaskLease,
    cause: Exception,
) -> None:
    code = ERROR_FAILED
    message = "长视频拆解失败，请稍后重新开始。"
    retryable = True
    failure_phase: str | None = None
    if isinstance(cause, AnalysisProviderFailed):
        mapped = analysis_provider_error(cause)
        detail: dict[str, Any] = mapped.detail if isinstance(mapped.detail, dict) else {}
        code = str(detail.get("code") or code)
        message = str(detail.get("message") or message)
        retryable = bool(detail.get("retryable", cause.retryable))
        failure_phase = cause.failure_phase
    elif isinstance(cause, HTTPException) and isinstance(cause.detail, dict):
        code = str(cause.detail.get("code") or code)
        message = str(cause.detail.get("message") or message)
        retryable = bool(cause.detail.get("retryable", True))
        failure_phase = cause.detail.get("failure_phase")
    unexpected_failure = not isinstance(cause, (AnalysisProviderFailed, HTTPException))
    logger.warning(
        "replica prompt task failed: task=%s project=%s asset=%s attempt=%s request=%s "
        "code=%s phase=%s retryable=%s",
        lease.id,
        lease.project_id,
        lease.asset_id,
        lease.attempt,
        lease.request_id or "-",
        code,
        failure_phase or "-",
        retryable,
        exc_info=cause if unexpected_failure else None,
    )
    now_text = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
    updated = conn.execute(
        """
        UPDATE replica_prompt_tasks
        SET status = 'FAILED', error_code = %s,
            error_message_redacted = %s, failure_phase = %s,
            retryable = %s,
            locked_by = NULL, locked_until = NULL,
            completed_at = %s, updated_at = %s
        WHERE id = %s AND status = 'RUNNING' AND locked_by = %s AND attempt = %s
        """,
        (
            code,
            message,
            failure_phase,
            1 if retryable else 0,
            now_text,
            now_text,
            lease.id,
            lease.worker_id,
            lease.attempt,
        ),
    )
    if updated.rowcount != 1:
        logger.warning(
            "replica prompt task failure write was discarded: task=%s attempt=%s",
            lease.id,
            lease.attempt,
        )
        conn.rollback()
        return
    write_audit(
        conn,
        actor=load_task_actor(conn, lease.created_by_user_id),
        action="replica_prompt.task_failed",
        entity_type="replica_prompt_task",
        entity_id=lease.id,
        metadata={
            "project_id": lease.project_id,
            "asset_id": lease.asset_id,
            "error_code": code,
            "retryable": retryable,
            "request_id": lease.request_id,
            "failure_phase": failure_phase,
            "attempt": lease.attempt,
            "error_message": message,
        },
    )
    from app.usage_billing import finish_source

    finish_source(conn, lease.id, units=0, succeeded=False)
    conn.commit()


def cancel_replica_prompt_task(
    conn: BusinessConnection,
    *,
    task_id: str,
    actor: CurrentUser,
) -> sqlite3.Row:
    """取消排队中/运行中的长视频拆解并释放预扣.

    状态 CHECK 没有 CANCELLED：与拆解同口径，取消落为 FAILED + 专用 error_code，
    前端把该 code 映射为「已取消」。迟到结果被 complete 的 CAS 丢弃，不会
    重复发布或结算。
    """
    row = load_replica_prompt_task(conn, task_id)
    project_id = str(row["project_id"])
    require_not_auditor(
        conn,
        actor=actor,
        action="replica_prompt.task.cancel",
        entity_type="replica_prompt_task",
        entity_id=task_id,
    )
    require_project_access(
        conn,
        actor=actor,
        project_id=project_id,
        action="replica_prompt.task.cancel",
    )
    if str(row["status"]) not in {"PENDING", "RUNNING"}:
        return row
    now_text = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
    updated = conn.execute(
        """
        UPDATE replica_prompt_tasks
        SET status = 'FAILED', error_code = %s,
            error_message_redacted = '长视频拆解任务已取消，可以重新开始。',
            failure_phase = NULL, retryable = 1,
            locked_by = NULL, locked_until = NULL,
            completed_at = %s, updated_at = %s
        WHERE id = %s AND status IN ('PENDING', 'RUNNING')
        RETURNING *
        """,
        (ERROR_CANCELLED, now_text, now_text, task_id),
    ).fetchone()
    if updated is None:
        return load_replica_prompt_task(conn, task_id)
    write_audit(
        conn,
        actor=actor,
        action="replica_prompt.task_cancelled",
        entity_type="replica_prompt_task",
        entity_id=task_id,
        metadata={"project_id": project_id, "asset_id": str(row["asset_id"])},
    )
    from app.usage_billing import finish_source

    finish_source(conn, task_id, units=0, succeeded=False, cancelled=True)
    conn.commit()
    return cast(sqlite3.Row, updated)


def assert_replica_prompt_enqueue_allowed(asset: sqlite3.Row, *, measured_duration: float) -> None:
    """任务入口的时长守护：与上传门禁同口径（4–180 秒 + 舍入容差）."""
    if not is_reference_video_asset(asset):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "REPLICA_PROMPT_ASSET_NOT_REFERENCE_VIDEO",
                "message": "长视频拆解需要参考视频素材。",
            },
        )
    if not str(asset["sha256"]) or int(asset["size_bytes"]) <= 0:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "REPLICA_PROMPT_VIDEO_NOT_READY",
                "message": "参考视频上传未完成，请稍后再试。",
            },
        )
    if measured_duration > MAX_TASK_DURATION_SECONDS:
        limit = round(MAX_TASK_DURATION_SECONDS)
        raise HTTPException(
            status_code=422,
            detail={
                "code": "REPLICA_PROMPT_DURATION_EXCEEDED",
                "message": (
                    f"视频时长 {round(measured_duration)} 秒，超过 {limit}"
                    " 秒上限，无法生成长视频复刻提示词。"
                ),
            },
        )
