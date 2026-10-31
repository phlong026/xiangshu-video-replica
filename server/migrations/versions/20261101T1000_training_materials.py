"""平台培训素材库（IA 改版第 5 项）：后台统一上传、客户只读。

一张表 ``training_materials``：运营上传的培训文档与示例视频的元数据行，
文件本体走 StorageAdapter（对象键 ``training-materials/{id}/{sha256}/{文件名}``，
内容寻址，同内容重传不产生第二份对象）。客户工作台素材库页面整体替换为本表
的只读视图，用户自传素材链路（参考视频/人物图/发布封面）不经过本表。

- ``kind`` 由上传白名单推导：视频文件 = ``video``，文档 = ``training``，
  不接受管理员手填，避免文件类型与栏目错位；
- 软删（``deleted_at``）与上下架（``visible``）分离：下架可随时恢复，
  删除对客户立即不可见；
- ``download_count`` 在签发下载链接时原子自增，作为运营侧热度信号。
"""

from alembic import op

revision = "20261101T1000_training_materials"
down_revision = "20261016T1000_replica_prompt_tasks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # These additions belong to the PG runtime, not offline legacy source snapshots.
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("""CREATE TABLE training_materials (
        id TEXT PRIMARY KEY,
        title TEXT NOT NULL CHECK(length(trim(title)) BETWEEN 1 AND 120),
        description TEXT NOT NULL DEFAULT '' CHECK(length(description) <= 1000),
        kind TEXT NOT NULL CHECK(kind IN ('video','training')),
        object_key TEXT NOT NULL,
        content_type TEXT NOT NULL,
        file_suffix TEXT NOT NULL,
        size_bytes BIGINT NOT NULL CHECK(size_bytes > 0),
        sha256 TEXT NOT NULL CHECK(length(sha256) = 64),
        sort_order INTEGER NOT NULL DEFAULT 0,
        visible INTEGER NOT NULL DEFAULT 1 CHECK(visible IN (0,1)),
        download_count BIGINT NOT NULL DEFAULT 0,
        created_by_user_id TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        deleted_at TIMESTAMPTZ
    )""")
    # 客户列表固定走「未删 + 上架 + 置顶序 + 新→旧」，与列表 SQL 完全同序。
    op.execute(
        "CREATE INDEX training_materials_live_idx ON "
        "training_materials(deleted_at, visible, kind, sort_order DESC, created_at DESC)"
    )


def downgrade() -> None:
    # These additions belong to the PG runtime, not offline legacy source snapshots.
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("DROP TABLE training_materials")
