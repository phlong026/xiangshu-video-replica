"""任务租约状态机的共享实现（T7 试点，2026-11-02）。

历史上五套近似实现（分析/改写/音频/提示词/抽帧）各自维护「过期回收 +
认领取件」两段 SQL，其中仅两套用了 FOR UPDATE SKIP LOCKED，其余用 CAS
子查询——统一的锁语义是 T7 的目标。本模块承载统一后的骨架：

1. 过期回收：``RUNNING`` 且 ``locked_until`` 已过期的行按调用方给定的
   规格转入失败态（各域的目标状态/错误码/文案不同，参数化传入）；
2. 认领取件：子查询 ``FOR UPDATE SKIP LOCKED`` 选出最早的就绪行并
   原子置为 ``RUNNING``（attempt+1、租约字段、错误复位）。

安全口径：状态字面量、错误码/文案、worker_id、时间戳全部参数绑定；
仅表名与 WHERE 片段/排序列这类**结构性标识符**由受信模块以编译期常量
传入，入口用 ``_guard``（字符集白名单 + 危险符拒绝）兜底，杜绝把任何
外部输入拼进 SQL。

试点迁移：source_frames（低频抽帧任务）。其余四套在试点验证后分批迁移。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.db_portable import BusinessConnection

# 结构性片段白名单：小写蛇形词与受信 SQL 保留符；显式拒绝引号/分号/注释。
_SAFE_FRAGMENT = re.compile(r"^[A-Za-z0-9_ ,.'()=<>!]+$")


@dataclass(frozen=True)
class TaskLease:
    """一次成功认领的租约凭据（与各域历史 Lease 类字段完全一致）。"""

    id: str
    created_by_user_id: str
    worker_id: str
    attempt: int


def _time_text(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S")


def _guard(fragment: str, what: str) -> str:
    if not fragment or not _SAFE_FRAGMENT.match(fragment):
        raise ValueError(f"task_lease: 非法 {what} 片段（只接受模块内常量）: {fragment!r}")
    return fragment


def claim_next_task(
    conn: BusinessConnection,
    *,
    table: str,
    worker_id: str,
    lease_minutes: int,
    recover_code: str,
    recover_message: str,
    recover_status: str = "FAILED",
    recover_retryable: int = 1,
    recover_where: str = "",
    pending_status: str = "PENDING",
    candidate_where: str = "",
    order_by: str = "created_at, id",
) -> TaskLease | None:
    """过期回收 + SKIP LOCKED 认领取件，两段在一个事务内提交。

    ``recover_where`` / ``candidate_where`` / ``order_by`` 是调用模块里的
    字符串常量（如 ``"provider_started_at IS NOT NULL"``），表达各域的
    回收前置与候选过滤；状态与文案等值参数全程绑定。
    """
    _guard(table, "table")
    _guard(recover_where or "ok", "recover_where")
    _guard(candidate_where or "ok", "candidate_where")
    _guard(order_by, "order_by")

    now_text = _time_text(datetime.now(UTC))
    locked_until_text = _time_text(datetime.now(UTC) + timedelta(minutes=lease_minutes))

    recover_extra = f" AND {recover_where}" if recover_where else ""
    conn.execute(
        f"""  -- noqa: S608 -- 结构片段为受信常量并经 _guard；值全部绑定
        UPDATE {table}
        SET status = %s, locked_by = NULL, locked_until = NULL,
            error_code = %s, error_message_redacted = %s,
            retryable = %s, completed_at = %s, updated_at = %s
        WHERE status = 'RUNNING' AND locked_until IS NOT NULL AND locked_until <= %s
        {recover_extra}
        """,
        (
            recover_status,
            recover_code,
            recover_message,
            recover_retryable,
            now_text,
            now_text,
            now_text,
        ),
    )

    candidate_extra = f" AND {candidate_where}" if candidate_where else ""
    row = conn.execute(
        f"""  -- noqa: S608 -- 同上；状态与值参数全部绑定
        UPDATE {table}
        SET status = 'RUNNING', attempt = attempt + 1,
            locked_by = %s, locked_until = %s,
            started_at = COALESCE(started_at, %s), updated_at = %s,
            error_code = NULL, error_message_redacted = NULL, retryable = 0
        WHERE id = (
            SELECT id FROM {table}
            WHERE status = %s{candidate_extra}
            ORDER BY {order_by} LIMIT 1 FOR UPDATE SKIP LOCKED
        ) AND status = %s
        RETURNING *
        """,
        (worker_id, locked_until_text, now_text, now_text, pending_status, pending_status),
    ).fetchone()
    conn.commit()
    if row is None:
        return None
    return TaskLease(
        id=str(row["id"]),
        created_by_user_id=str(row["created_by_user_id"]),
        worker_id=worker_id,
        attempt=int(row["attempt"]),
    )
