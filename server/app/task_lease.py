"""任务租约状态机的统一家目录（T7，2026-11-02 试点后全量收敛）。

历史上五套近似实现（分析/改写/音频/提示词/抽帧）散落各域，锁语义分叉
（仅两套用 FOR UPDATE SKIP LOCKED）。T7 的目标是**一个模块、一次评审面**：
本文件承载五域的「过期回收 + 认领取件」全部 SQL——每段都是从原实现
**逐字搬移**的字面量（值一律 %s 绑定，无任何运行时拼接；source_frames
与 replica_prompt 的认领子查询按统一口径补上 FOR UPDATE SKIP LOCKED，
与改写/转写域一致），租约凭据统一为 :class:`TaskLease`。

各域 acquire_* 函数改为对本模块的一行委托；analysis 的认领仍在本域
（attempt 历史/预取 FOR UPDATE 耦合属域逻辑），仅过期回收段移入。

后续新增任务域：在此追加一段字面量 SQL + 委托，不再各写一份。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import cast

from app.db_portable import BusinessConnection, BusinessRow

_SAFE_TABLE = re.compile(r"^[a-z_][a-z0-9_]*$")  # 供域内自检复用


@dataclass(frozen=True)
class TaskLease:
    """一次成功认领的租约凭据（与各域历史 Lease 类字段完全一致）。"""

    id: str
    created_by_user_id: str
    worker_id: str
    attempt: int


def _time_text(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S")


def _lease_from_row(row: object, worker_id: str) -> TaskLease:
    return TaskLease(
        id=str(row["id"]),  # type: ignore[index]
        created_by_user_id=str(row["created_by_user_id"]),  # type: ignore[index]
        worker_id=worker_id,
        attempt=int(row["attempt"]),  # type: ignore[index]
    )


# ===========================================================================
# 抽帧（source_frames）：单段回收（中断→可重试失败）+ 认领
# ===========================================================================


def claim_source_frame_task(
    conn: BusinessConnection, *, worker_id: str, lease_minutes: int
) -> TaskLease | None:
    now = _time_text(datetime.now(UTC))
    locked_until = _time_text(datetime.now(UTC) + timedelta(minutes=lease_minutes))
    conn.execute(
        """
        UPDATE source_frame_tasks
        SET status = 'FAILED', locked_by = NULL, locked_until = NULL,
            error_code = 'SOURCE_FRAME_TASK_RECOVERY_REQUIRED',
            error_message_redacted = '取帧任务执行中断，请重新开始。',
            retryable = 1, completed_at = %s, updated_at = %s
        WHERE status = 'RUNNING' AND locked_until IS NOT NULL AND locked_until <= %s
        """,
        (now, now, now),
    )
    row = conn.execute(
        """
        UPDATE source_frame_tasks
        SET status = 'RUNNING', attempt = attempt + 1,
            locked_by = %s, locked_until = %s,
            started_at = COALESCE(started_at, %s), updated_at = %s,
            error_code = NULL, error_message_redacted = NULL, retryable = 0
        WHERE id = (
            SELECT id FROM source_frame_tasks
            WHERE status = 'PENDING'
            ORDER BY created_at, id LIMIT 1 FOR UPDATE SKIP LOCKED
        ) AND status = 'PENDING'
        RETURNING *
        """,
        (worker_id, locked_until, now, now),
    ).fetchone()
    conn.commit()
    if row is None:
        return None
    return _lease_from_row(row, worker_id)


# ===========================================================================
# 改写（script_rewrite）：两段回收（未送达→重排 / 已送达→不确定）+ 认领
# ===========================================================================


def claim_script_rewrite_task(
    conn: BusinessConnection, *, worker_id: str, lease_minutes: int
) -> TaskLease | None:
    now = _time_text(datetime.now(UTC))
    locked_until = _time_text(datetime.now(UTC) + timedelta(minutes=lease_minutes))
    conn.execute(
        """
        UPDATE script_rewrite_tasks
        SET status = 'PENDING', locked_by = NULL, locked_until = NULL,
            error_code = NULL, error_message_redacted = NULL, retryable = 0,
            updated_at = %s
        WHERE status = 'RUNNING' AND provider_started_at IS NULL
          AND locked_until IS NOT NULL AND locked_until <= %s
        """,
        (now, now),
    )
    conn.execute(
        """
        UPDATE script_rewrite_tasks
        SET status = 'SUBMISSION_UNCERTAIN', locked_by = NULL, locked_until = NULL,
            error_code = 'SCRIPT_REWRITE_SUBMISSION_UNCERTAIN',
            error_message_redacted = %s, retryable = 0,
            completed_at = %s, updated_at = %s
        WHERE status = 'RUNNING' AND provider_started_at IS NOT NULL
          AND locked_until IS NOT NULL AND locked_until <= %s
        """,
        (
            "AI 改写请求可能已经送达服务商，请人工确认后再决定是否重试。",
            now,
            now,
            now,
        ),
    )
    row = conn.execute(
        """
        UPDATE script_rewrite_tasks
        SET status = 'RUNNING', attempt = attempt + 1,
            locked_by = %s, locked_until = %s,
            started_at = COALESCE(started_at, %s), updated_at = %s,
            error_code = NULL, error_message_redacted = NULL, retryable = 0
        WHERE id = (
            SELECT id FROM script_rewrite_tasks
            WHERE status = 'PENDING'
            ORDER BY created_at, id LIMIT 1 FOR UPDATE SKIP LOCKED
        ) AND status = 'PENDING'
        RETURNING *
        """,
        (worker_id, locked_until, now, now),
    ).fetchone()
    conn.commit()
    if row is None:
        return None
    return _lease_from_row(row, worker_id)


# ===========================================================================
# 语音转写（script_from_audio）：两段回收（重排条件含已取供应商号）+ 认领
# ===========================================================================


def claim_script_from_audio_task(
    conn: BusinessConnection, *, worker_id: str, lease_minutes: int
) -> BusinessRow | None:
    """转写域取件；返回原始行（域凭据含 project_id，由调用方构造）。"""
    now = _time_text(datetime.now(UTC))
    locked_until = _time_text(datetime.now(UTC) + timedelta(minutes=lease_minutes))
    conn.execute(
        """
        UPDATE script_from_audio_tasks
        SET status = 'PENDING', locked_by = NULL, locked_until = NULL,
            error_code = NULL, error_message_redacted = NULL, retryable = 0,
            updated_at = %s
        WHERE status = 'RUNNING' AND (provider_started_at IS NULL OR provider_task_id IS NOT NULL)
          AND locked_until IS NOT NULL AND locked_until <= %s
        """,
        (now, now),
    )
    conn.execute(
        """
        UPDATE script_from_audio_tasks
        SET status = 'SUBMISSION_UNCERTAIN', locked_by = NULL, locked_until = NULL,
            error_code = 'SCRIPT_FROM_AUDIO_SUBMISSION_UNCERTAIN',
            error_message_redacted = %s, retryable = 0,
            completed_at = %s, updated_at = %s
        WHERE status = 'RUNNING' AND provider_started_at IS NOT NULL AND provider_task_id IS NULL
          AND locked_until IS NOT NULL AND locked_until <= %s
        """,
        (
            "语音转写请求可能已经送达服务商，请人工确认后再决定是否重试。",
            now,
            now,
            now,
        ),
    )
    row = conn.execute(
        """
        UPDATE script_from_audio_tasks
        SET status = 'RUNNING', attempt = attempt + 1,
            locked_by = %s, locked_until = %s,
            started_at = COALESCE(started_at, %s), updated_at = %s,
            error_code = NULL, error_message_redacted = NULL, retryable = 0
        WHERE id = (
            SELECT id FROM script_from_audio_tasks
            WHERE status = 'PENDING' AND (next_attempt_at IS NULL OR next_attempt_at <= %s)
            ORDER BY created_at, id LIMIT 1 FOR UPDATE SKIP LOCKED
        ) AND status = 'PENDING'
        RETURNING *
        """,
        (worker_id, locked_until, now, now, now),
    ).fetchone()
    conn.commit()
    return cast(BusinessRow | None, row)


# ===========================================================================
# 提示词（replica_prompt）：单段回收 + PENDING 残留清理 + 认领（含复位列）
# ===========================================================================


def claim_replica_prompt_task(
    conn: BusinessConnection, *, worker_id: str, lease_minutes: int
) -> BusinessRow | None:
    """提示词域取件；返回原始行（域凭据含 project/asset/时长等，由调用方构造）。"""
    now = _time_text(datetime.now(UTC))
    locked_until = _time_text(datetime.now(UTC) + timedelta(minutes=lease_minutes))
    # 过期 RUNNING 视为 worker 中断：置为可重试失败（错误字段留在任务行供查看）。
    conn.execute(
        """
        UPDATE replica_prompt_tasks
        SET status = 'FAILED',
            error_code = 'REPLICA_PROMPT_WORKER_INTERRUPTED',
            error_message_redacted = '长视频拆解任务执行中断，请重新开始。',
            retryable = 1,
            locked_by = NULL,
            locked_until = NULL,
            completed_at = %s,
            updated_at = %s
        WHERE status = 'RUNNING' AND locked_until IS NOT NULL AND locked_until <= %s
        """,
        (now, now, now),
    )
    # 认领前清掉 PENDING 行残留的旧失败字段：新尝试从干净状态开始。
    conn.execute(
        """
        UPDATE replica_prompt_tasks
        SET error_code = NULL, error_message_redacted = NULL,
            failure_phase = NULL, retryable = 0
        WHERE status = 'PENDING'
          AND (error_code IS NOT NULL OR failure_phase IS NOT NULL)
        """
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
            LIMIT 1 FOR UPDATE SKIP LOCKED
        ) AND status = 'PENDING'
        RETURNING *
        """,
        (worker_id, locked_until, now, now),
    ).fetchone()
    conn.commit()
    return cast(BusinessRow | None, row)


# ===========================================================================
# 拆解（analysis）：仅过期回收段移入（RETURNING 供域内 attempt 历史留痕）；
# 认领因与预取 FOR UPDATE/attempt 簿记耦合保留在 analysis_routes 本域。
# ===========================================================================


def recover_analysis_interrupted(conn: BusinessConnection) -> list[BusinessRow]:
    """回收过期 RUNNING 的拆解任务并返回 (id, attempt, request_id) 行。"""
    now = _time_text(datetime.now(UTC))
    return list(
        conn.execute(
            """
            UPDATE analysis_tasks
            SET status = 'FAILED',
                error_code = 'ANALYSIS_WORKER_INTERRUPTED',
                error_message_redacted = '拆解任务执行中断，请重新拆解。',
                retryable = 1,
                locked_by = NULL,
                locked_until = NULL,
                completed_at = %s,
                updated_at = %s
            WHERE status = 'RUNNING' AND locked_until IS NOT NULL AND locked_until <= %s
            RETURNING id, attempt, request_id
            """,
            (now, now, now),
        ).fetchall()
    )
