"""长视频拆解分镜表模式：replica_prompt_tasks 追加 mode 列。

2026-11-22 需求（推翻 2026-11-11 D3「>30s 拆解沿用一段式不立项分镜卡」）：
>30s 拆解除一段式复刻提示词外，新增 storyboard 模式——同通道（gemini-4-carbon
吃 30-180s 整片）输出 12 字段 + 完整 shots 时间轴的分镜表，结果落 versions
kind='long_analysis'。mode 决定 worker 选哪套提示词与结果解析器：

- 'prompt'（默认，既有行为）：一段式复刻提示词 + 口播 + 摘要；
- 'storyboard'：分镜表 + 复刻提示词 + 口播 + 摘要（并存，一次调用同出）。

存量行为不变：NOT NULL DEFAULT 'prompt' 让既有行与既有调用路径零感知。
mode 的合法值校验在 API 层用 Literal 强制（本迁移不加 CHECK，避免约束
计数漂移；写入路径只有 worker 认领后的既有行更新与入队 INSERT，入队值
已在路由层收口）。
"""

import sqlalchemy as sa
from alembic import op

revision = "20261122T1000_replica_prompt_mode"
down_revision = "20261120T1000_account_diagnosis"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.add_column(
        "replica_prompt_tasks",
        sa.Column("mode", sa.Text(), nullable=False, server_default="prompt"),
    )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.drop_column("replica_prompt_tasks", "mode")
