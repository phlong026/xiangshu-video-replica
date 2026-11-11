"""视频生成 16-30 秒档计费科目种子（默认停用）。

上游长视频按基础费用 2 倍计费，因此种子价 = 768P 现行售价与成本的 2 倍；
enabled 恒为 false——档位是否放开是经营决策，由后台费率管理显式开启。
仅在 768P 已有价目行时落默认值；全新环境由管理员直接配置。
"""

from alembic import op

revision = "20261101T1000_video_long_tier"
down_revision = "20261016T1000_replica_prompt_tasks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 客户计费运行时只存在于 PG（SQLite 侧无 billing_tariffs 业务路径）。
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("""
        INSERT INTO billing_tariffs(service, enabled, unit_credits, unit_cost_fen, unit_rounding)
        SELECT 'video_768p_long', false, unit_credits * 2, unit_cost_fen * 2, unit_rounding
        FROM billing_tariffs WHERE service = 'video_768p'
        ON CONFLICT (service) DO NOTHING
    """)


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    # 管理员改过价或启用过即属经营事实，不允许随回滚静默消失。
    op.execute("""
        DO $$ BEGIN
          IF EXISTS(SELECT 1 FROM billing_tariffs WHERE service = 'video_768p_long'
                    AND (enabled OR updated_by_user_id IS NOT NULL)) THEN
            RAISE EXCEPTION
              'video_768p_long tariff was configured by admin; forward repair required';
          END IF;
          DELETE FROM billing_tariffs WHERE service = 'video_768p_long';
        END $$;
    """)
