"""全自动复刻编排器（replica pipeline）的状态常量与期限定义.

单一来源：16 态状态机、三个调度集合（轮询白名单 / 在途约束 / 过期扫描）、
阶段枚举与各类期限值全部定义在此，应用代码与测试一律引用本模块，
不允许字面量散落。DDL 的冻结字面量在迁移文件里内联（迁移是不可变快照），
``test_replica_pipeline_model`` 会比对两者一致——改这里的集合必须同步重录
迁移并更新清单（方案 §3.1「三个独立状态集合」与 §3.2 转换表）。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast

from app.db_portable import (
    BusinessConnection,
    BusinessRow,
    IntegrityConstraintError,
)

# --------------------------------------------------------------- 状态机（16 态）

STATUS_ENQUEUED = "ENQUEUED"
STATUS_IMPORTING = "IMPORTING"
STATUS_ANALYZING = "ANALYZING"
STATUS_SCRIPT_VERSIONING = "SCRIPT_VERSIONING"
STATUS_FIRST_FRAMING = "FIRST_FRAMING"
STATUS_COMPOSING = "COMPOSING"
STATUS_AWAIT_CONFIRM = "AWAIT_CONFIRM"
STATUS_GENERATING = "GENERATING"
STATUS_NEEDS_REVIEW = "NEEDS_REVIEW"
STATUS_NEEDS_REVIEW_SETTLED = "NEEDS_REVIEW_SETTLED"
STATUS_DRAINING = "DRAINING"
STATUS_DELIVERED = "DELIVERED"
STATUS_DELIVERED_PARTIAL = "DELIVERED_PARTIAL"
STATUS_FAILED = "FAILED"
STATUS_CANCELLED = "CANCELLED"
STATUS_EXPIRED = "EXPIRED"

# 16 态全集：DDL CHECK 与本集合必须一致（迁移快照内联同一字面量列表）。
ALL_STATUSES: tuple[str, ...] = (
    STATUS_ENQUEUED,
    STATUS_IMPORTING,
    STATUS_ANALYZING,
    STATUS_SCRIPT_VERSIONING,
    STATUS_FIRST_FRAMING,
    STATUS_COMPOSING,
    STATUS_AWAIT_CONFIRM,
    STATUS_GENERATING,
    STATUS_NEEDS_REVIEW,
    STATUS_NEEDS_REVIEW_SETTLED,
    STATUS_DRAINING,
    STATUS_DELIVERED,
    STATUS_DELIVERED_PARTIAL,
    STATUS_FAILED,
    STATUS_CANCELLED,
    STATUS_EXPIRED,
)

# 终态：release 在途约束，退出一切调度集合。
TERMINAL_STATUSES: frozenset[str] = frozenset(
    {
        STATUS_DELIVERED,
        STATUS_DELIVERED_PARTIAL,
        STATUS_FAILED,
        STATUS_CANCELLED,
        STATUS_EXPIRED,
    }
)

# 推进态（7 个）：后台编排会实际驱动阶段前进的状态。
PROGRESS_STATUSES: frozenset[str] = frozenset(
    {
        STATUS_ENQUEUED,
        STATUS_IMPORTING,
        STATUS_ANALYZING,
        STATUS_SCRIPT_VERSIONING,
        STATUS_FIRST_FRAMING,
        STATUS_COMPOSING,
        STATUS_GENERATING,
    }
)

# 仅观察态（3 个）：领取后只做收口/恢复检查与状态迁移，无任何付费动作
# （方案 §3.1 索引注释——NEEDS_REVIEW 观察「子任务收口」，SETTLED 观察「产物恢复」，
# DRAINING 观察「未收口子任务结清」以便落终态释放资格）。
OBSERVATION_STATUSES: frozenset[str] = frozenset(
    {
        STATUS_NEEDS_REVIEW,
        STATUS_NEEDS_REVIEW_SETTLED,
        STATUS_DRAINING,
    }
)

# 轮询白名单（10 态）：ix_replica_pipeline_poll 的谓词与领取查询共用。
POLL_STATUSES: frozenset[str] = PROGRESS_STATUSES | OBSERVATION_STATUSES

# 在途约束集合（10 态）：「后台付费敞口未收口」——轮询白名单 + AWAIT_CONFIRM
# 减去 NEEDS_REVIEW_SETTLED（已收口无敞口）。uq_replica_pipeline_active_* 谓词。
ACTIVE_STATUSES: frozenset[str] = (POLL_STATUSES - {STATUS_NEEDS_REVIEW_SETTLED}) | {
    STATUS_AWAIT_CONFIRM
}

# 过期扫描集合（1 态）：AWAIT_CONFIRM 的 72h 确认期限由独立扫描驱动，
# 不进轮询（等待确认不占执行槽）。
EXPIRE_SCAN_STATUSES: frozenset[str] = frozenset({STATUS_AWAIT_CONFIRM})

# --------------------------------------------------------------- 发送意图（M6b）

# 三个付费供应商域（方案 §6 三态证据表的域口径）。DDL CHECK 与本集合同源。
INTENT_DOMAIN_ANALYSIS = "analysis"
INTENT_DOMAIN_FIRST_FRAME = "first_frame"
INTENT_DOMAIN_GENERATION = "generation"
INTENT_DOMAINS: frozenset[str] = frozenset(
    {INTENT_DOMAIN_ANALYSIS, INTENT_DOMAIN_FIRST_FRAME, INTENT_DOMAIN_GENERATION}
)

# 允许子任务提交的父编排状态集（方案 §6 提交许可谓词①）：
# 推进态 + NEEDS_REVIEW（无 uncertain——既有授权内剩余排队任务必须继续提交
# 才能收口）。DRAINING 不在集合：其剩余排队任务走主动取消而不是继续付费。
SUBMIT_PERMIT_STATUSES: frozenset[str] = PROGRESS_STATUSES | {STATUS_NEEDS_REVIEW}

# 子任务 → pipeline 引用列的固定映射（SQL 列名只来自本表，杜绝拼接注入）。
_INTENT_SUBTASK_COLUMNS: dict[str, str] = {
    INTENT_DOMAIN_ANALYSIS: "analysis_task_id",
    INTENT_DOMAIN_FIRST_FRAME: "first_frame_task_id",
}

# --------------------------------------------------------------- 阶段

STAGE_INITIATED = "initiated"
STAGE_IMPORT = "import"
STAGE_ANALYZE = "analyze"
STAGE_SCRIPT = "script"
STAGE_FIRST_FRAME = "first_frame"
STAGE_COMPOSE = "compose"
STAGE_CONFIRM = "confirm"
STAGE_GENERATE = "generate"
STAGE_DELIVER = "deliver"

# --------------------------------------------------------------- 期限与调度参数

# 执行期限：自发起（按有效运行时长）到 AWAIT_CONFIRM 的上限（方案 §3.2.3）。
EXECUTION_DEADLINE_SECONDS = 24 * 3600
# 无进展期限：同阶段连续无状态变化的判定窗口。
NO_PROGRESS_SECONDS = 30 * 60
# 每阶段自动重试预算（计入发起时披露的有界授权；确定性失败才递增 retry_no）。
MAX_STAGE_RETRIES = 2
# AWAIT_CONFIRM 确认期限：恒定计时，不排除挂起（确认是用户义务）。
CONFIRM_WINDOW_SECONDS = 72 * 3600
# 等待子任务时的轮询退避：30s 起步、翻倍、5min 封顶（方案 §3.3）。
POLL_BACKOFF_INITIAL_SECONDS = 30
POLL_BACKOFF_CAP_SECONDS = 5 * 60


# ============================== M2：编排驱动核心 ==============================
# 轮询推进（方案 §3.3）：编排任务以第四种任务身份加入 generation worker 池。
# 等待子任务期间不持有租约、不心跳——唯一动作是 next_poll_at 到期后重新可领。

_LEASE_MINUTES = 10


class ReplicaPipelineError(Exception):
    """编排域错误：对外携带 HTTP 语义（状态码/中性文案/可否重试）."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.retryable = retryable


@dataclass(frozen=True)
class ReplicaPipelineLease:
    """编排任务租约：attempt 校验拒绝失租 worker 的迟到提交（r1#8）."""

    id: str
    worker_id: str
    attempt: int
    owner_user_id: str
    project_id: str
    platform: str
    video_id: str
    request_hash: str
    character_version_id: str | None
    status: str
    stage: str
    stage_retry_no: int


def stage_operation_key(pipeline_id: str, stage: str, stage_retry_no: int) -> str:
    """阶段操作键：子任务受理的幂等凭据（方案 §3.3，r2#3）.

    业务重试序号入键——同一阶段失败重试换新键重新入队（首帧域对旧键只会
    重放失败终态）；崩溃恢复复用同一序号按键找回，不重复付费。
    """
    return f"pipeline:{pipeline_id}:{stage}:{stage_retry_no}"


def _backoff_seconds(failures: int) -> int:
    """等待子任务的轮询退避：30s 起步、翻倍、5min 封顶（方案 §3.3）."""
    return int(
        min(POLL_BACKOFF_INITIAL_SECONDS * (2 ** max(failures - 1, 0)), POLL_BACKOFF_CAP_SECONDS)
    )


def effective_elapsed_seconds(row: BusinessRow, *, now: datetime) -> float:
    """有效运行时长：排除累计挂起与当前挂起时段（方案 §3.2.3 期限口径）."""
    created_raw = row["created_at"]
    created = (
        created_raw
        if isinstance(created_raw, datetime)
        else datetime.fromisoformat(str(created_raw))
    )
    elapsed = (now - created).total_seconds()
    elapsed -= cast(int, row["suspended_seconds_total"] or 0)
    suspended_at = row["suspended_at"]
    if suspended_at is not None:
        if isinstance(suspended_at, str):
            suspended_at = datetime.fromisoformat(suspended_at)
        elapsed -= (now - suspended_at).total_seconds()
    return float(elapsed)


def enqueue_replica_pipeline(
    conn: BusinessConnection,
    *,
    owner_user_id: str,
    platform: str,
    video_id: str,
    character_version_id: str,
    idempotency_key: str,
    request_hash: str,
    pipeline_id: str | None = None,
) -> dict[str, object]:
    """发起全自动复刻：幂等受理 + 项目同事务创建（project_id NOT NULL 落库）.

    幂等键用户作用域（r1#12）：同键重放原尝试（返回 replay 标志），同键不同
    request_hash 视为键误用拒绝——绝不复活终态、绝不静默换参。
    """
    scoped_key = f"{owner_user_id}:{idempotency_key}"
    replay_or_none = _replay_if_exists(conn, scoped_key, request_hash)
    if replay_or_none is not None:
        return replay_or_none
    new_id = pipeline_id or f"rplc-{uuid.uuid4().hex[:16]}"
    project_id = f"proj-{uuid.uuid4().hex[:16]}"
    fingerprint = f"{platform}:{video_id}:{character_version_id}:{request_hash}"
    # 并发同键（m2-r2#1）：PG 下 conn.rollback() 是 no-op（commit 权威在外层
    # fenced 事务），冲突后事务仍 aborted、重读会抛 25P02——project 与任务两
    # 个 INSERT 必须包进同一真实 savepoint，冲突回滚到保存点（撤销多余
    # project）后外层事务保持可用，再重读赢家给出重放或异参拒绝。
    conn.execute("SAVEPOINT replica_enqueue")
    try:
        conn.execute(
            "INSERT INTO projects (id, owner_user_id, name) VALUES (%s, %s, %s)",
            (project_id, owner_user_id, "全自动复刻"),
        )
        conn.execute(
            """
            INSERT INTO replica_pipeline_tasks (
                id, owner_user_id, project_id, platform, video_id, request_hash,
                status, stage, character_version_id, project_fingerprint,
                idempotency_key, next_poll_at, stage_detail
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now(), %s)
            """,
            (
                new_id,
                owner_user_id,
                project_id,
                platform,
                video_id,
                request_hash,
                STATUS_ENQUEUED,
                STAGE_INITIATED,
                character_version_id,
                fingerprint,
                scoped_key,
                json.dumps({"strategy": "fixed_v1"}),
            ),
        )
    except IntegrityConstraintError as exc:
        conn.execute("ROLLBACK TO SAVEPOINT replica_enqueue")
        conn.execute("RELEASE SAVEPOINT replica_enqueue")
        replay = _replay_if_exists(conn, scoped_key, request_hash)
        if replay is None:
            raise ReplicaPipelineError(
                409, "REPLICA_PIPELINE_RACE_LOST", "受理竞争失败，请重试。"
            ) from exc
        return replay
    conn.execute("RELEASE SAVEPOINT replica_enqueue")
    return {"id": new_id, "projectId": project_id, "status": STATUS_ENQUEUED, "replay": False}


def _replay_if_exists(
    conn: BusinessConnection, scoped_key: str, request_hash: str
) -> dict[str, object] | None:
    """同键重放判定：存在即校验参数一致性（重放 / 异参拒绝），不存在返回 None."""
    row = cast(
        "BusinessRow | None",
        conn.execute(
            "SELECT id, request_hash, status, project_id FROM replica_pipeline_tasks "
            "WHERE idempotency_key = %s",
            (scoped_key,),
        ).fetchone(),
    )
    if row is None:
        return None
    if str(row["request_hash"]) != request_hash:
        raise ReplicaPipelineError(
            422,
            "REPLICA_PIPELINE_IDEMPOTENCY_CONFLICT",
            "该幂等键已用于不同的发起参数，请刷新后重试。",
        )
    return {
        "id": str(row["id"]),
        "projectId": str(row["project_id"]),
        "status": str(row["status"]),
        "replay": True,
    }


def _require_lease(conn: BusinessConnection, lease: ReplicaPipelineLease) -> BusinessRow:
    """顾问性读取：按 id 取编排行供迁移合法性判断.

    租约有效性不由本读取判定——唯一权威闸门是各写函数的条件 UPDATE
    （WHERE 带 attempt/locked_by/clock_timestamp 条件 + 影响行数断言）。
    在此处再查租约会遮蔽条件 UPDATE 的跨期保护（评审 m2-r3#2），故本读取
    不带任何租约条件。
    """
    row = conn.execute(
        "SELECT * FROM replica_pipeline_tasks WHERE id = %s",
        (lease.id,),
    ).fetchone()
    if row is None:
        raise ReplicaPipelineError(409, "REPLICA_PIPELINE_LEASE_LOST", "编排任务已不存在。")
    return cast(BusinessRow, row)


def _assert_lease_write_applied(moved: int) -> None:
    if moved != 1:
        raise ReplicaPipelineError(409, "REPLICA_PIPELINE_LEASE_LOST", "编排任务租约已失效。")


@dataclass(frozen=True)
class AdvanceDecision:
    """阶段 handler 的推进裁决（M3 注入具体逻辑，M2 用桩测试骨架）."""

    kind: str  # "completed" | "waiting" | "failed"
    next_status: str | None = None
    next_stage: str | None = None
    sub_task_id: str | None = None  # 该阶段产出的子任务/版本 id
    sub_task_column: str | None = None  # 回填列名（产出阶段的列，非进入阶段）
    failure_reason: str = ""
    failure_code: str = ""
    retryable: bool = False


# 转换表（方案 §3.2，r5#7/r7#4 统一口径 + m2-r1#1 撤销出口）：合法迁移集合.
TRANSITIONS: dict[str, frozenset[str]] = {
    # 推进态：主链出口 + 撤销事件出口（DRAINING，r4#2/r5#2）+ 失败出口（FAILED
    # 仅当无未收口子任务——运行时由 _failure_exit_status 裁决，转换表允许）。
    # ENQUEUED 的 FAILED 出口覆盖排队超时（m2-r1#1）。
    STATUS_ENQUEUED: frozenset({STATUS_IMPORTING, STATUS_DRAINING, STATUS_FAILED}),
    STATUS_IMPORTING: frozenset({STATUS_ANALYZING, STATUS_DRAINING, STATUS_FAILED}),
    STATUS_ANALYZING: frozenset({STATUS_SCRIPT_VERSIONING, STATUS_DRAINING, STATUS_FAILED}),
    STATUS_SCRIPT_VERSIONING: frozenset({STATUS_FIRST_FRAMING, STATUS_DRAINING, STATUS_FAILED}),
    STATUS_FIRST_FRAMING: frozenset({STATUS_COMPOSING, STATUS_DRAINING, STATUS_FAILED}),
    STATUS_COMPOSING: frozenset({STATUS_AWAIT_CONFIRM, STATUS_DRAINING, STATUS_FAILED}),
    STATUS_AWAIT_CONFIRM: frozenset(
        {STATUS_GENERATING, STATUS_CANCELLED, STATUS_EXPIRED, STATUS_DRAINING}
    ),
    # 生成域结果映射（方案 §5.1）：非干净成功落 NEEDS_REVIEW，无 FAILED 直出口。
    STATUS_GENERATING: frozenset(
        {STATUS_DELIVERED, STATUS_NEEDS_REVIEW, STATUS_NEEDS_REVIEW_SETTLED, STATUS_DRAINING}
    ),
    STATUS_NEEDS_REVIEW: frozenset(
        {STATUS_DELIVERED, STATUS_NEEDS_REVIEW_SETTLED, STATUS_DRAINING}
    ),
    STATUS_NEEDS_REVIEW_SETTLED: frozenset(
        {STATUS_DELIVERED, STATUS_DELIVERED_PARTIAL, STATUS_GENERATING, STATUS_CANCELLED}
    ),
    STATUS_DRAINING: frozenset({STATUS_FAILED, STATUS_CANCELLED}),
}


def acquire_replica_pipeline(
    conn: BusinessConnection, *, worker_id: str
) -> ReplicaPipelineLease | None:
    """领取编排任务：轮询白名单 + 到期 + 未挂起；租约互斥 SKIP LOCKED.

    签发与回收判定统一 clock_timestamp（m2-r3#1）：长事务内 now() 固定为
    事务开始时间，跨过到期点后既无法回收旧租约、也可能签发已过期的新租约。
    """
    row = cast(
        "BusinessRow | None",
        conn.execute(
            f"""
            UPDATE replica_pipeline_tasks SET attempt = attempt + 1,
                locked_by = %s,
                locked_until = clock_timestamp() + interval '{_LEASE_MINUTES} minutes',
                updated_at = now()
            WHERE id = (
                SELECT id FROM replica_pipeline_tasks
                WHERE status = ANY(%s) AND next_poll_at <= clock_timestamp()
                    AND suspended_at IS NULL
                    AND (locked_until IS NULL OR locked_until <= clock_timestamp())
                ORDER BY next_poll_at, id LIMIT 1 FOR UPDATE SKIP LOCKED
            ) RETURNING *
            """,
            (worker_id, sorted(POLL_STATUSES)),
        ).fetchone(),
    )
    if row is None:
        return None
    return ReplicaPipelineLease(
        id=str(row["id"]),
        worker_id=worker_id,
        attempt=int(row["attempt"]),
        owner_user_id=str(row["owner_user_id"]),
        project_id=str(row["project_id"]),
        platform=str(row["platform"]),
        video_id=str(row["video_id"]),
        request_hash=str(row["request_hash"]),
        character_version_id=(
            str(row["character_version_id"]) if row["character_version_id"] else None
        ),
        status=str(row["status"]),
        stage=str(row["stage"]),
        stage_retry_no=int(row["stage_retry_no"] or 0),
    )


def release_lease(conn: BusinessConnection, lease: ReplicaPipelineLease) -> None:
    """释放租约（等待期唯一动作——不心跳、不续约）。"""
    moved = conn.execute(
        "UPDATE replica_pipeline_tasks SET locked_by = NULL, locked_until = NULL, "
        "updated_at = now() "
        "WHERE id = %s AND attempt = %s AND locked_by = %s "
        "AND locked_until IS NOT NULL AND locked_until > clock_timestamp()",
        (lease.id, lease.attempt, lease.worker_id),
    ).rowcount
    _assert_lease_write_applied(moved)


def set_next_poll(
    conn: BusinessConnection,
    lease: ReplicaPipelineLease,
    *,
    wait_failures: int = 1,
) -> None:
    """子任务未完成：置退避后的下次可领时间并立即释放租约（方案 §3.3）."""
    delay = _backoff_seconds(wait_failures)
    moved = conn.execute(
        "UPDATE replica_pipeline_tasks SET next_poll_at = clock_timestamp() "
        "+ (%s || ' seconds')::interval, "
        "locked_by = NULL, locked_until = NULL, updated_at = now() "
        "WHERE id = %s AND attempt = %s AND locked_by = %s "
        "AND locked_until IS NOT NULL AND locked_until > clock_timestamp()",
        (str(delay), lease.id, lease.attempt, lease.worker_id),
    ).rowcount
    _assert_lease_write_applied(moved)


def advance_status(
    conn: BusinessConnection,
    lease: ReplicaPipelineLease,
    *,
    decision: AdvanceDecision,
) -> None:
    """状态机单向迁移：转换表校验 + 断点/引用回填，非法迁移视为内部违例."""
    row = _require_lease(conn, lease)
    current = str(row["status"])
    if decision.kind != "completed" or decision.next_status is None:
        raise ReplicaPipelineError(500, "REPLICA_PIPELINE_ADVANCE_MISUSE", "推进裁决不完整。")
    if decision.next_status not in TRANSITIONS.get(current, frozenset()):
        raise ReplicaPipelineError(
            500,
            "REPLICA_PIPELINE_ILLEGAL_TRANSITION",
            f"非法状态迁移：{current} -> {decision.next_status}",
        )
    updates = [
        "status = %s",
        "updated_at = now()",
        "next_poll_at = now()",
        "stage_retry_no = 0",  # 阶段推进重置业务重试预算（评审 A-r2#9）
        "locked_by = NULL",
        "locked_until = NULL",
    ]
    params: list[object] = [decision.next_status]
    if decision.next_stage:
        updates.insert(1, "stage = %s")
        params.append(decision.next_stage)
    if decision.sub_task_id is not None:
        column = decision.sub_task_column
        # 严格白名单防止列名注入（AdvanceDecision 由本模块构造，仍不信任字面量）。
        if column is not None and column in _SUB_TASK_COLUMNS:
            updates.insert(2, f"{column} = COALESCE({column}, %s)")
            params.append(decision.sub_task_id)
    params.extend([lease.id, lease.attempt, lease.worker_id])
    moved = conn.execute(
        f"UPDATE replica_pipeline_tasks SET {', '.join(updates)} "
        "WHERE id = %s AND attempt = %s AND locked_by = %s "
        "AND locked_until IS NOT NULL AND locked_until > clock_timestamp()",
        tuple(params),
    ).rowcount
    _assert_lease_write_applied(moved)


# 子任务/版本引用可回填列的白名单（列名来自本文件常量，SQL 拼接安全）。
_SUB_TASK_COLUMNS: frozenset[str] = frozenset(
    {
        "import_task_id",
        "analysis_task_id",
        "script_version_id",
        "first_frame_task_id",
        "prompt_version_id",
        "batch_id",
    }
)


def settle_failure(
    conn: BusinessConnection,
    lease: ReplicaPipelineLease,
    *,
    code: str,
    message: str,
    retryable: bool = False,
) -> str:
    """失败出口：运行中子任务/未收口预占存在时先 DRAINING（r4#2）.

    撤销推进权立即生效（状态迁移到 DRAINING 即拒绝后续推进），释放在途
    资格延后到收口观察轮询落终态。M2 阶段无子任务敞口接入（M6 接管），
    收口判定恒空 → 直接落 FAILED；结构留 DRAINING 出口供 M6 复用。
    """
    row = _require_lease(conn, lease)
    current = str(row["status"])
    target = _failure_exit_status(conn, lease.id)
    if target not in TRANSITIONS.get(current, frozenset()):
        raise ReplicaPipelineError(500, "REPLICA_PIPELINE_ILLEGAL_TRANSITION", "非法失败迁移。")
    detail = json.dumps({"failureCode": code, "failureMessage": message, "retryable": retryable})
    moved = conn.execute(
        """
        UPDATE replica_pipeline_tasks SET status = %s,
            stage = CASE WHEN %s = %s THEN stage || ':failed' ELSE stage END,
            stage_detail = %s,
            locked_by = NULL, locked_until = NULL,
            finished_at = CASE WHEN %s IN (%s, %s) THEN now() ELSE finished_at END,
            updated_at = now(), next_poll_at = now()
        WHERE id = %s AND attempt = %s AND locked_by = %s
            AND locked_until IS NOT NULL AND locked_until > clock_timestamp()
        """,
        (
            target,
            target,
            STATUS_FAILED,
            detail,
            target,
            STATUS_FAILED,
            STATUS_CANCELLED,
            lease.id,
            lease.attempt,
            lease.worker_id,
        ),
    ).rowcount
    _assert_lease_write_applied(moved)
    return target


def _failure_exit_status(conn: BusinessConnection, pipeline_id: str) -> str:
    """未收口子任务存在 → DRAINING（r4#2）；全收口 → FAILED.

    M2 阶段子任务引用列恒空（M3 起回填、M6 接入敞口判定），恒返回 FAILED；
    函数是 M6 的扩展点，不是装饰。
    """
    row = cast(
        "BusinessRow | None",
        conn.execute(
            """
            SELECT EXISTS (
                SELECT 1 FROM viral_import_tasks i
                    JOIN replica_pipeline_tasks p ON p.import_task_id = i.id
                    WHERE p.id = %s AND i.status NOT IN ('SUCCEEDED','FAILED','CANCELLED')
            ) OR EXISTS (
                SELECT 1 FROM analysis_tasks a
                    JOIN replica_pipeline_tasks p ON p.analysis_task_id = a.id
                    WHERE p.id = %s AND a.status NOT IN ('SUCCEEDED','FAILED','CANCELLED')
            ) OR EXISTS (
                SELECT 1 FROM first_frame_tasks f
                    JOIN replica_pipeline_tasks p ON p.first_frame_task_id = f.id
                    WHERE p.id = %s AND f.status NOT IN ('SUCCEEDED','FAILED','CANCELLED')
            ) AS has_open_subtasks
            FROM replica_pipeline_tasks WHERE id = %s
            """,
            (pipeline_id, pipeline_id, pipeline_id, pipeline_id),
        ).fetchone(),
    )
    if row is not None and bool(row["has_open_subtasks"]):
        return STATUS_DRAINING
    return STATUS_FAILED


def check_deadlines(conn: BusinessConnection, row: BusinessRow, *, now: datetime) -> bool:
    """执行期限（有效运行时长 > 24h 未到 AWAIT_CONFIRM）→ 失败出口（方案 §3.2.3）.

    返回 True 表示已触发期限（调用方应执行 settle_failure）；无进展期限与
    重试预算由阶段 handler 依据子任务观测推进（M3）。
    """
    # 方案口径「24h 内未到 AWAIT_CONFIRM」：期限只约束确认点之前的推进态；
    # 已确认（GENERATING 起）、等待确认、观察态均不在该期限下——晚确认两天
    # 后 GENERATING 不得立即判超时（m2-r1#4）。
    if row["status"] not in (PROGRESS_STATUSES - {STATUS_GENERATING}):
        return False
    return effective_elapsed_seconds(row, now=now) > EXECUTION_DEADLINE_SECONDS


def mark_suspended(conn: BusinessConnection, pipeline_id: str) -> None:
    """总开关关闭：置挂起起点（幂等——已在挂起中不覆盖起点）。"""
    conn.execute(
        "UPDATE replica_pipeline_tasks SET suspended_at = COALESCE(suspended_at, now()), "
        "updated_at = now() WHERE id = %s",
        (pipeline_id,),
    )


def resume_from_suspension(conn: BusinessConnection, pipeline_id: str) -> None:
    """总开关重开：累计挂起时长、清挂起起点、重设轮询时间（方案 §3.2.3）.

    挂起产物的版本链恢复在 M6 的受保护恢复事务中执行（r3#5/r5#6）；此处
    负责行级挂起属性的清算，并把本管线被 fencing 拦下的任务重新开放领取
    （Codex r-P1：hold 打的 REPLICA_PIPELINE_SUSPENDED 标记会被领取谓词
    排除——不清掉，开关恢复后任务永远不可领取）。
    """
    conn.execute(
        """
        UPDATE replica_pipeline_tasks SET
            suspended_seconds_total = suspended_seconds_total +
                CASE WHEN suspended_at IS NULL THEN 0
                     ELSE FLOOR(EXTRACT(EPOCH FROM (now() - suspended_at)))::int END,
            suspended_at = NULL,
            next_poll_at = now(),
            updated_at = now()
        WHERE id = %s AND suspended_at IS NOT NULL
        """,
        (pipeline_id,),
    )
    conn.execute(
        """
        UPDATE first_frame_tasks SET error_code = NULL,
            error_message_redacted = NULL, updated_at = CURRENT_TIMESTAMP
        WHERE error_code = 'REPLICA_PIPELINE_SUSPENDED'
          AND EXISTS (SELECT 1 FROM replica_pipeline_tasks p
                      WHERE p.first_frame_task_id = first_frame_tasks.id AND p.id = %s)
        """,
        (pipeline_id,),
    )
    conn.execute(
        """
        UPDATE analysis_tasks SET error_code = NULL,
            error_message_redacted = NULL, updated_at = %s
        WHERE error_code = 'REPLICA_PIPELINE_SUSPENDED'
          AND EXISTS (SELECT 1 FROM replica_pipeline_tasks p
                      WHERE p.analysis_task_id = analysis_tasks.id AND p.id = %s)
        """,
        (datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S"), pipeline_id),
    )
    conn.execute(
        """
        UPDATE generation_tasks SET error_code = NULL,
            error_message_redacted = NULL, updated_at = CURRENT_TIMESTAMP
        WHERE error_code = 'REPLICA_PIPELINE_SUSPENDED'
          AND EXISTS (SELECT 1 FROM replica_pipeline_tasks p
                      WHERE p.batch_id = generation_tasks.batch_id AND p.id = %s)
        """,
        (pipeline_id,),
    )


def pipeline_enabled(conn: BusinessConnection) -> bool:
    """总开关：runtime_settings.replica_pipeline_enabled（方案 §6/§8，默认关）."""
    row = conn.execute(
        "SELECT replica_pipeline_enabled FROM runtime_settings WHERE id = 1"
    ).fetchone()
    if row is None:
        return False
    return bool(row["replica_pipeline_enabled"])


def require_pipeline_enabled(conn: BusinessConnection) -> None:
    """新发起入口的开关门（M7 路由与 M3 drive 共用）."""
    if not pipeline_enabled(conn):
        raise ReplicaPipelineError(
            422,
            "REPLICA_PIPELINE_DISABLED",
            "全自动复刻暂未开放。",
        )


def pipeline_status_of(conn: BusinessConnection, pipeline_id: str) -> str | None:
    """读父编排当前状态（fencing 拒绝分支区分 hold / DRAINING 收口取消用）."""
    row = conn.execute(
        "SELECT status FROM replica_pipeline_tasks WHERE id = %s", (pipeline_id,)
    ).fetchone()
    return str(row["status"]) if row is not None else None


# ============================== M6b：提交点 fencing ============================


def pipeline_id_for_sub_task(
    conn: BusinessConnection, *, domain: str, sub_task_id: str
) -> str | None:
    """子任务 → 所属 pipeline（无归属返回 None，手工任务不经管线许可）.

    生成域按 batch_id 归属：pipeline.batch_id 是受理时回填的批次引用；
    子任务本身来自该批次。拆解/首帧直接按回填列匹配。
    """
    if domain == INTENT_DOMAIN_GENERATION:
        task = conn.execute(
            "SELECT batch_id FROM generation_tasks WHERE id = %s", (sub_task_id,)
        ).fetchone()
        if task is None or task["batch_id"] is None:
            return None
        column, value = "batch_id", str(task["batch_id"])
    elif domain in _INTENT_SUBTASK_COLUMNS:
        column, value = _INTENT_SUBTASK_COLUMNS[domain], sub_task_id
    else:
        raise ReplicaPipelineError(500, "REPLICA_INTENT_DOMAIN_INVALID", "发送意图域无效。")
    row = conn.execute(
        f"SELECT id FROM replica_pipeline_tasks WHERE {column} = %s "
        "ORDER BY created_at DESC, id DESC LIMIT 1",
        (value,),
    ).fetchone()
    return str(row["id"]) if row is not None else None


def _pipeline_generation_uncertain(conn: BusinessConnection, pipeline_id: str) -> bool:
    """生成域提交未知/待核对的**签发侧实时复核**（Codex r-P1）.

    父行 ``stage_detail.uncertain`` 由编排轮询延迟写入：某任务进入
    SUBMISSION_UNCERTAIN 后、父标志落库前，同批次其他任务仍可能签发意图
    继续付费。签发侧直接按关联批次任务行的真实状态判定，不依赖延迟标志。
    """
    row = conn.execute(
        """
        SELECT 1
        FROM replica_pipeline_tasks p
        JOIN generation_tasks t ON t.batch_id = p.batch_id
        WHERE p.id = %s AND p.batch_id IS NOT NULL
          AND (t.status IN ('SUBMISSION_UNCERTAIN', 'NEEDS_ATTENTION')
               OR t.error_code = 'LEASE_EXPIRED_NEEDS_ATTENTION')
        LIMIT 1
        """,
        (pipeline_id,),
    ).fetchone()
    return row is not None


def issue_submit_intent(
    conn: BusinessConnection,
    *,
    pipeline_id: str,
    sub_task_id: str,
    domain: str,
    attempt: int,
) -> bool:
    """持父行锁签发/消费发送意图；返回 True 才允许发起供应商调用（方案 §6）.

    线性化点语义：本函数返回并 COMMIT 之后，该次提交即视为「已受理」——
    关闭/挂起/撤销在同一父行锁下发生，只能拦住尚未落意图的调用。因此
    调用方必须在**真正发起供应商调用前**执行本函数，且不得在拒绝时调用。

    总开关的线性化论证（Codex r-P1 评审闭环）：挂起写点（mark_suspended 的
    pipeline UPDATE）会阻塞在本函数持有的父行锁上，因此「关闭先提交、
    签发后落地」不可能——settings 的 ON 读必然发生在挂起提交之前，本次
    签发按设计线性化在挂起之前，属「意图已落库视为已受理」的许可语义。

    幂等：同一 (pipeline, 子任务, 域, attempt) 重放返回 True（意图已在案），
    不重复签发；不同 attempt 是新提交点。
    """
    if domain not in INTENT_DOMAINS:
        raise ReplicaPipelineError(500, "REPLICA_INTENT_DOMAIN_INVALID", "发送意图域无效。")
    # 先父后子：父行锁是关闭/挂起/撤销/恢复的同一把锁（方案 §5.3 固定锁序）。
    row = conn.execute(
        "SELECT status, suspended_at, stage_detail FROM replica_pipeline_tasks "
        "WHERE id = %s FOR UPDATE",
        (pipeline_id,),
    ).fetchone()
    if row is None:
        return False
    status = str(row["status"])
    if status not in SUBMIT_PERMIT_STATUSES:
        return False
    if row["suspended_at"] is not None:
        return False
    detail = row["stage_detail"] if isinstance(row["stage_detail"], dict) else {}
    if detail.get("uncertain") is True:
        return False
    # 生成域签发侧实时复核（Codex r-P1）：父 uncertain 标志由编排轮询延迟
    # 写入，不能作为止付依据——直接查关联批次任务行的真实状态。
    if domain == INTENT_DOMAIN_GENERATION and _pipeline_generation_uncertain(conn, pipeline_id):
        return False
    # 总开关关闭 = 一切付费动作不发生；与 mark_suspended 在同一父行锁下线性化。
    if not pipeline_enabled(conn):
        return False
    conn.execute(
        """
        INSERT INTO replica_pipeline_submit_intents
            (id, pipeline_id, sub_task_id, domain, attempt)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (pipeline_id, sub_task_id, domain, attempt) DO NOTHING
        """,
        (uuid.uuid4().hex, pipeline_id, sub_task_id, domain, attempt),
    )
    return True


def has_submit_intent(
    conn: BusinessConnection,
    *,
    pipeline_id: str,
    sub_task_id: str,
    domain: str,
    attempt: int,
) -> bool:
    """三态证据查询：该提交点是否已有意图（删除保护/对账承接用）."""
    row = conn.execute(
        "SELECT 1 FROM replica_pipeline_submit_intents "
        "WHERE pipeline_id = %s AND sub_task_id = %s AND domain = %s AND attempt = %s",
        (pipeline_id, sub_task_id, domain, attempt),
    ).fetchone()
    return row is not None
