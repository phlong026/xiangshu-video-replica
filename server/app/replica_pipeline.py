"""全自动复刻编排器（replica pipeline）的状态常量与期限定义.

单一来源：16 态状态机、三个调度集合（轮询白名单 / 在途约束 / 过期扫描）、
阶段枚举与各类期限值全部定义在此，应用代码与测试一律引用本模块，
不允许字面量散落。DDL 的冻结字面量在迁移文件里内联（迁移是不可变快照），
``test_replica_pipeline_model`` 会比对两者一致——改这里的集合必须同步重录
迁移并更新清单（方案 §3.1「三个独立状态集合」与 §3.2 转换表）。
"""

from __future__ import annotations

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
