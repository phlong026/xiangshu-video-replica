"""平台培训素材库（IA 改版第 5 项）：后台统一上传、客户只读。

一张表 ``training_materials``：运营上传的培训素材（视频/音频/图片/文档）的
元数据行，文件本体存 123 网盘（``remote_file_id`` 为网盘 fileID，远端文件名
``{material_id}-{safe_name}`` 全局唯一）。客户工作台素材库页面整体替换为本表
的只读视图，用户自传素材链路（参考视频/人物图/发布封面）不经过本表。

- ``kind`` 由上传白名单推导四组（video/audio/image/doc），不接受管理员手填，
  避免文件类型与栏目错位；
- ``tags`` 为必填字符串数组（1-5 个、单个 ≤20 字），GIN 索引支撑 ``@>``
  包含查询做标签筛选；
- 软删（``deleted_at``）与上下架（``visible``）分离：下架可随时恢复，
  删除对客户立即不可见；
- ``download_count`` 在签发下载链接时原子自增，作为运营侧热度信号。

本 revision 未发布（分支未合 main），父指针直接重挂到当前 main 头
``20261102T0900_customer_session_last_seen``，保持迁移链单头。
"""

from alembic import op

revision = "20261101T1000_training_materials"
down_revision = "20261102T0900_customer_session_last_seen"
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
        kind TEXT NOT NULL CHECK(kind IN ('video','audio','image','doc')),
        object_key TEXT NOT NULL,
        remote_file_id TEXT NOT NULL,
        content_type TEXT NOT NULL,
        file_suffix TEXT NOT NULL,
        size_bytes BIGINT NOT NULL CHECK(size_bytes > 0),
        sha256 TEXT NOT NULL CHECK(length(sha256) = 64),
        tags JSONB NOT NULL DEFAULT '[]' CHECK(jsonb_typeof(tags) = 'array'),
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
    # 标签筛选走 jsonb contains（tags @> '["标签"]'），GIN 是它的唯一索引形态。
    op.execute(
        "CREATE INDEX training_materials_tags_idx ON "
        "training_materials USING gin(tags)"
    )


def downgrade() -> None:
    # These additions belong to the PG runtime, not offline legacy source snapshots.
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("DROP TABLE training_materials")
