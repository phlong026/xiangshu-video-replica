"""人物任务合并受理的持久请求 key，防止丢响应后创建第二个计费任务。"""

import sqlalchemy as sa
from alembic import op

revision = "20261012T0200_character_request_keys"
down_revision = "20261012T0100_admin_adjustment_operations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("""
        CREATE TABLE character_sheet_task_requests (
            created_by_user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            idempotency_key TEXT NOT NULL,
            request_hash TEXT NOT NULL,
            task_id TEXT NOT NULL REFERENCES character_sheet_tasks(id) ON DELETE CASCADE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (created_by_user_id, idempotency_key)
        )
    """)
    op.execute("""
        CREATE INDEX idx_character_sheet_task_requests_task
        ON character_sheet_task_requests(task_id)
    """)
    op.execute("""
        INSERT INTO character_sheet_task_requests
            (created_by_user_id, idempotency_key, request_hash, task_id)
        SELECT created_by_user_id, idempotency_key, request_hash, id
        FROM character_sheet_tasks
    """)


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    # 核对期间禁止新增别名，避免检查为空后又并发受理并被 DROP 丢弃。
    op.execute("LOCK TABLE character_sheet_task_requests IN ACCESS EXCLUSIVE MODE")
    # 原任务只保存主 key；合并窗口的 key 无法靠升级回填恢复，禁止静默丢弃。
    if (
        op.get_bind()
        .execute(
            sa.text("""
        SELECT EXISTS (
            SELECT 1 FROM character_sheet_task_requests k
            JOIN character_sheet_tasks t ON t.id = k.task_id
            WHERE k.idempotency_key <> t.idempotency_key
        )
    """)
        )
        .scalar()
    ):
        raise RuntimeError("人物任务已产生合并请求身份，不能删除防重记录。")
    op.execute("DROP TABLE character_sheet_task_requests")
