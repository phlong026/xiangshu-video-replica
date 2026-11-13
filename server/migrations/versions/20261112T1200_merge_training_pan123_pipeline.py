"""合并培训素材链与复刻管线链两条并行迁移线。

Revision ID: 20261112T1200_merge_training_pan123_pipeline
Down revisions:
  - 20261112T0910_provider_runtime_state（培训素材域：网盘车道/白名单放宽/共享 token）
  - 20261111T1000_uncertain_auto_reopen（复刻管线域链尾，含 PR #117/#118 迁移）

两条线都从 20261102T0900_customer_session_last_seen 分叉：培训侧经
20261101T1000_training_materials → 0900_widen → 0910；管线侧经
20261111T1000_replica_pipeline_tasks → 20261112T1000_awaiting_since → 1100。
本迁移是无操作合并点，使 upgrade head 按官方多链机制收敛为单头；
无操作体（两侧 schema 互不重叠，合并后 schema 由两侧迁移叠加而成）。
"""

revision = "20261112T1200_merge_training_pan123_pipeline"
down_revision = (
    "20261112T0910_provider_runtime_state",
    "20261111T1000_uncertain_auto_reopen",
)
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 无操作合并点：两侧迁移的 schema 变更互不重叠，无需补偿。
    pass


def downgrade() -> None:
    # 无操作：回退即回到双头状态（alembic 会分别回退两条支线的 head）。
    pass
