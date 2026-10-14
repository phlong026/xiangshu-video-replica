"""管理端整改 T14/T15：工单优先级与关联对象。

support_tickets 追加两列：
- ``priority``：P0–P3（默认 P2），未结案工单按优先级排序，P0/P1 进总览待办口径；
- ``related_ref``：关联的生成任务号或充值订单号（``task:<id>`` / ``order:<no>``），
  处理人可一步跳到对应记录，不再在描述里手抄编号。

只追加列与 CHECK，不改已发布 revision。

集成说明（2026-10-15 rebase 到 main）：本 revision 在分支上原本接
``20261008T1600_support_tickets``，而 main 已从同一节点继续到
``20261013T1020_publish_submission_safety``（该 revision 尚未发布，改接不算篡改
已发布链）。这里改接 main 现头，避免迁移图分成两个 head。
"""

from alembic import op

revision = "20261014T1000_support_ticket_priority"
down_revision = "20261013T1020_publish_submission_safety"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # These additions belong to the PG runtime, not offline legacy source snapshots.
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        "ALTER TABLE support_tickets ADD COLUMN priority TEXT NOT NULL DEFAULT 'P2' "
        "CONSTRAINT ck_support_tickets_priority CHECK (priority IN ('P0','P1','P2','P3'))"
    )
    op.execute(
        "ALTER TABLE support_tickets ADD COLUMN related_ref TEXT NOT NULL DEFAULT '' "
        "CONSTRAINT ck_support_tickets_related_ref CHECK ("
        "related_ref = '' OR related_ref ~ '^(task|order):[A-Za-z0-9_:.-]{1,100}$')"
    )


def downgrade() -> None:
    # These additions belong to the PG runtime, not offline legacy source snapshots.
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("ALTER TABLE support_tickets DROP COLUMN related_ref")
    op.execute("ALTER TABLE support_tickets DROP COLUMN priority")
