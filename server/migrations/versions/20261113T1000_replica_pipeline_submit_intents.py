"""全自动复刻编排器：供应商发送意图表（M6b 生产启用前置 #1）。

方案 `docs/复刻全自动链路方案-2026-11-11.md` §6「提交许可与线性化 fencing」：
新增付费提交的线性化点定义为「发送意图落库」——worker 持父行锁（先父后子）
校验提交许可后，在 pipeline 行原子签发并消费一条意图记录；COMMIT 之后才
发起供应商调用。关闭/挂起/撤销/uncertain 都在同一父行锁下发生，因此：

- 意图已落库的调用视为已受理，继续查询/归档/结算，不制造孤儿调用；
- 意图落库前被拦截的调用永远拿不到许可，新增付费被挡。

表上不建 FK：与 pipeline 表同因——项目删除路径在应用层走统一撤销事务，
流水必须保留作为资金授权与审计证据（方案 §3.1/§6）。

幂等键 ``(pipeline_id, sub_task_id, domain, attempt)``：同一子任务同一 attempt
的重放（worker 崩溃后重领）不会重复签发；不同 attempt 是新提交点，允许新意图。
"""

import sqlalchemy as sa
from alembic import op

revision = "20261113T1000_replica_pipeline_submit_intents"
down_revision = "20261112T1200_merge_training_pan123_pipeline"
branch_labels = None
depends_on = None

# 与 app/replica_pipeline.INTENT_DOMAINS 同源；test_replica_pipeline_model
# 会在真库上比对 CHECK 字面量与常量模块，防两处漂移。
_DOMAIN_LIST = ("analysis", "first_frame", "generation")


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.create_table(
        "replica_pipeline_submit_intents",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("pipeline_id", sa.Text(), nullable=False),
        sa.Column("sub_task_id", sa.Text(), nullable=False),
        sa.Column("domain", sa.Text(), nullable=False),
        # 子任务 attempt（租约代次）：意图绑定具体一次提交点，重领/重试不串账。
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            f"domain IN ({', '.join(repr(value) for value in _DOMAIN_LIST)})",
            name="ck_replica_pipeline_submit_intents_domain",
        ),
        sa.UniqueConstraint(
            "pipeline_id",
            "sub_task_id",
            "domain",
            "attempt",
            name="uq_replica_pipeline_submit_intents_subtask",
        ),
    )
    # 删除保护/恢复事务按 pipeline 聚合查询；逐域回执查询按 (domain, sub_task_id)。
    op.create_index(
        "ix_replica_pipeline_submit_intents_pipeline",
        "replica_pipeline_submit_intents",
        ["pipeline_id"],
    )
    op.create_index(
        "ix_replica_pipeline_submit_intents_subtask",
        "replica_pipeline_submit_intents",
        ["domain", "sub_task_id"],
    )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    # 意图是资金授权与审计证据（方案 §6，无 FK 保留策略）：表非空时拒绝降级，
    # 防止降一版就永久销毁证据。确需销毁必须先完成可核验的归档再手动清表。
    row = (
        op.get_bind()
        .execute(sa.text("SELECT 1 FROM replica_pipeline_submit_intents LIMIT 1"))
        .fetchone()
    )
    if row is not None:
        raise RuntimeError(
            "replica_pipeline_submit_intents 非空，拒绝 downgrade："
            "发送意图是资金授权与审计证据，销毁前必须先完成可核验归档。"
        )
    op.drop_index(
        "ix_replica_pipeline_submit_intents_subtask",
        table_name="replica_pipeline_submit_intents",
    )
    op.drop_index(
        "ix_replica_pipeline_submit_intents_pipeline",
        table_name="replica_pipeline_submit_intents",
    )
    op.drop_table("replica_pipeline_submit_intents")
