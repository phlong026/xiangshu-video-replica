"""全自动复刻编排器：replica_pipeline_tasks 编排任务表。

方案 `docs/复刻全自动链路方案-2026-11-11.md` §3.1（评审修订稿 r10 通过）。
只增不改：一张新表 + 三个索引，不触碰任何既有表。

设计要点（为什么这样建）：
- 关联 ID 沿用现有 ``text`` 类型（users/projects/各任务表主键即 text，
  uuid 类型会产生不匹配）；不建数据库级 FK 强引用——项目删除路径在应用层
  走统一撤销事务，pipeline 行必须保留作为资金授权与审计证据（评审 r1#7）。
- 三个部分索引谓词 = 应用层 `app/replica_pipeline.py` 的三个调度集合
  （轮询白名单 / 在途约束 / ——过期扫描集合仅 AWAIT_CONFIRM，无索引需求，
  由独立扫描低频查询）。迁移是不可变快照，字面量内联；测试比对两者一致。
- 时间列 ``timestamptz``（DateTime(timezone=True)，044/041 先例）、JSON 列原生
  ``jsonb``（quote_snapshot/stage_detail，方案 §3.1 与 publish_records 先例——
  089 的「JSON 全存 TEXT」约定已被 publish_records 打破）；时间比较与 JSON
  操作的契约落在数据库类型上，不靠应用层解析（评审 m1-r1#1）。
- ``stage_retry_no`` 是业务重试序号（确定性失败才原子递增），与租约的
  ``attempt`` 是两个语义，绝不可互相替代（评审 r2#3）。
- 挂起是行属性而非独立状态（可叠加任意在途态），``suspended_seconds_total``
  累计历史挂起时长，期限判定按有效运行时长（评审 r3#6）。
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20261111T1000_replica_pipeline_tasks"
down_revision = "20261102T0900_customer_session_last_seen"
branch_labels = None
depends_on = None

# 与 app/replica_pipeline.ALL_STATUSES 同序同值；test_replica_pipeline_model
# 会在真库上比对 CHECK 字面量与常量模块，防两处漂移。
_STATUS_LIST = (
    "ENQUEUED",
    "IMPORTING",
    "ANALYZING",
    "SCRIPT_VERSIONING",
    "FIRST_FRAMING",
    "COMPOSING",
    "AWAIT_CONFIRM",
    "GENERATING",
    "NEEDS_REVIEW",
    "NEEDS_REVIEW_SETTLED",
    "DRAINING",
    "DELIVERED",
    "DELIVERED_PARTIAL",
    "FAILED",
    "CANCELLED",
    "EXPIRED",
)

# 轮询白名单（10 态）= 推进态 7 个 + 仅观察态 3 个。
_POLL_LIST = (
    "ENQUEUED",
    "IMPORTING",
    "ANALYZING",
    "SCRIPT_VERSIONING",
    "FIRST_FRAMING",
    "COMPOSING",
    "GENERATING",
    "NEEDS_REVIEW",
    "NEEDS_REVIEW_SETTLED",
    "DRAINING",
)

# 在途约束集合（10 态）= 「后台付费敞口未收口」：轮询白名单 + AWAIT_CONFIRM
# 减去 NEEDS_REVIEW_SETTLED（已收口无敞口）。
_ACTIVE_LIST = (
    "ENQUEUED",
    "IMPORTING",
    "ANALYZING",
    "SCRIPT_VERSIONING",
    "FIRST_FRAMING",
    "COMPOSING",
    "AWAIT_CONFIRM",
    "GENERATING",
    "NEEDS_REVIEW",
    "DRAINING",
)


def _in_list(column: str, values: tuple[str, ...]) -> str:
    rendered = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({rendered})"


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.create_table(
        "replica_pipeline_tasks",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("owner_user_id", sa.Text(), nullable=False),
        sa.Column("project_id", sa.Text(), nullable=False),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("video_id", sa.Text(), nullable=False),
        # 发起参数指纹（策略+规格）：重试一致性校验，防止重试拿到与原授权
        # 不同的策略组合（评审 r1#12）。
        sa.Column("request_hash", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default="ENQUEUED",
        ),
        sa.Column("stage", sa.Text(), nullable=False),
        sa.Column("stage_retry_no", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("character_version_id", sa.Text()),
        # 发起时冻结的人物版本与项目指纹：复用判断必须按指纹而非「存在产物
        # 即跳过」（评审 r1#11）。
        sa.Column("project_fingerprint", sa.Text()),
        sa.Column("suspended_at", sa.DateTime(timezone=True)),
        sa.Column("suspended_seconds_total", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        # 租约列（编排任务自身领取）：attempt 校验拒绝失租 worker 的迟到提交。
        sa.Column("locked_by", sa.Text()),
        sa.Column("locked_until", sa.DateTime(timezone=True)),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
        # 等待期调度键：到期才可被领取；等待期不持有租约、不心跳。
        sa.Column("next_poll_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("import_task_id", sa.Text()),
        sa.Column("analysis_task_id", sa.Text()),
        sa.Column("script_version_id", sa.Text()),
        sa.Column("first_frame_task_id", sa.Text()),
        sa.Column("prompt_version_id", sa.Text()),
        sa.Column("batch_id", sa.Text()),
        # AWAIT_CONFIRM 冻结的报价凭证与快照（金额上限、档位、名额数、总秒数、
        # 产物指纹）——确认点资金闸门的授权凭据（评审 r1#1）。
        sa.Column("quote_token", sa.Text()),
        sa.Column("quote_snapshot", postgresql.JSONB()),
        # 失败原因、重试线索、策略快照、uncertain 标记、撤销 reason 等。
        sa.Column("stage_detail", postgresql.JSONB()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("confirmed_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            f"status IN ({', '.join(repr(v) for v in _STATUS_LIST)})",
            name="ck_replica_pipeline_tasks_status",
        ),
    )
    op.create_unique_constraint(
        "uq_replica_pipeline_tasks_idempotency_key",
        "replica_pipeline_tasks",
        ["idempotency_key"],
    )
    # 可调度状态白名单（评审 r3#7 + r4#1/#2 + r7#3）：仅观察态也在集合内
    # （观察模式无付费动作）；AWAIT_CONFIRM 不进轮询（独立过期扫描驱动）。
    op.create_index(
        "ix_replica_pipeline_poll",
        "replica_pipeline_tasks",
        ["next_poll_at"],
        unique=False,
        postgresql_where=sa.text(_in_list("status", _POLL_LIST)),
    )
    # 每用户/每项目单在途：数据库级约束（评审 r1#13——应用层先查再建不可依赖；
    # r3#3/r4#2——NEEDS_REVIEW 与 DRAINING 时子任务敞口未收口，必须继续占用）。
    op.create_index(
        "uq_replica_pipeline_active_user",
        "replica_pipeline_tasks",
        ["owner_user_id"],
        unique=True,
        postgresql_where=sa.text(_in_list("status", _ACTIVE_LIST)),
    )
    op.create_index(
        "uq_replica_pipeline_active_project",
        "replica_pipeline_tasks",
        ["project_id"],
        unique=True,
        postgresql_where=sa.text(_in_list("status", _ACTIVE_LIST)),
    )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.drop_index("uq_replica_pipeline_active_project", table_name="replica_pipeline_tasks")
    op.drop_index("uq_replica_pipeline_active_user", table_name="replica_pipeline_tasks")
    op.drop_index("ix_replica_pipeline_poll", table_name="replica_pipeline_tasks")
    op.drop_constraint(
        "uq_replica_pipeline_tasks_idempotency_key",
        "replica_pipeline_tasks",
        type_="unique",
    )
    op.drop_table("replica_pipeline_tasks")
