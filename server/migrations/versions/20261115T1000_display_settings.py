"""客户端展示配置表（键值）：运营在管理端改，客户只读。

首键 ``prompt_tutorial_url``：参考生视频页「提示词教程」外链此前写死在
前端常量里，正式飞书链接每换一次就要发一次版；搬进数据库后运营在系统
设置里改即可即时生效。键名白名单控制客户端可读范围（见
``app.display_settings.PUBLIC_KEYS``），非白名单键只在管理端可见。

KV 而非 runtime_settings 加列：展示配置与运行参数生命周期不同（运行参数
是强类型运营数值，展示配置是自由文本且会持续增键），拆开互不干扰。
"""

import sqlalchemy as sa
from alembic import op

revision = "20261115T1000_display_settings"
down_revision = "20261113T1000_replica_pipeline_submit_intents"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.create_table(
        "display_settings",
        sa.Column("key", sa.Text(), primary_key=True),
        sa.Column("value", sa.Text(), nullable=False, server_default=""),
        sa.Column("updated_by_user_id", sa.Text(), nullable=True),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.drop_table("display_settings")
