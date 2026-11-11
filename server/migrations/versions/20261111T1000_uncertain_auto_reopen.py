"""2026-11-11 极简化改造 WP-C：SUBMISSION_UNCERTAIN 自动处置参数与重开计数.

共享文案缓存退役后，转写/改写任务提交结果不可证明时不再依赖人工核对：
超阈值（默认 30 分钟）自动重开新一轮计费，重开次数封顶（默认 2 次），耗尽
落 FAILED 并在观测页可见。runtime_settings 追加两个参数列；两张任务表各追
加 ``reopen_count`` 计数列（重开 CAS 的 ``reopen_count<上限`` 守卫用）。
只追加列与 CHECK，不改已发布 revision。
"""

from alembic import op

revision = "20261111T1000_uncertain_auto_reopen"
down_revision = "20261102T0900_customer_session_last_seen"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # These additions belong to the PG runtime, not offline legacy source snapshots.
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        "ALTER TABLE runtime_settings ADD COLUMN uncertain_auto_reopen_minutes "
        "INTEGER NOT NULL DEFAULT 30 CONSTRAINT "
        "ck_runtime_settings_uncertain_auto_reopen_minutes CHECK "
        "(uncertain_auto_reopen_minutes BETWEEN 0 AND 1440)"
    )
    op.execute(
        "ALTER TABLE runtime_settings ADD COLUMN uncertain_auto_reopen_max "
        "INTEGER NOT NULL DEFAULT 2 CONSTRAINT "
        "ck_runtime_settings_uncertain_auto_reopen_max CHECK "
        "(uncertain_auto_reopen_max BETWEEN 0 AND 10)"
    )
    op.execute(
        "ALTER TABLE script_from_audio_tasks ADD COLUMN reopen_count "
        "INTEGER NOT NULL DEFAULT 0"
    )
    op.execute(
        "ALTER TABLE script_rewrite_tasks ADD COLUMN reopen_count "
        "INTEGER NOT NULL DEFAULT 0"
    )


def downgrade() -> None:
    # These additions belong to the PG runtime, not offline legacy source snapshots.
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("ALTER TABLE script_rewrite_tasks DROP COLUMN reopen_count")
    op.execute("ALTER TABLE script_from_audio_tasks DROP COLUMN reopen_count")
    op.execute("ALTER TABLE runtime_settings DROP COLUMN uncertain_auto_reopen_max")
    op.execute("ALTER TABLE runtime_settings DROP COLUMN uncertain_auto_reopen_minutes")
