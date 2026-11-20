"""账号诊断：报告快照表 + 首单免费额度表.

为什么单独建表而不复用任务表：诊断报告要长期保存（历史分数曲线是留存抓手），
而任务表会按既有策略清理；报告是"结果"不是"过程"，生命周期不同。

``account_diagnosis_free_grants`` 用两个唯一索引实现反刷：
``user_id`` 唯一保证每个付费主体只有一次免费；``device_id`` 部分唯一保证同一台
设备换号也拿不到第二次。占用与释放都以本行为准，任务失败会删除对应行。
"""

import sqlalchemy as sa
from alembic import op

revision = "20261120T1000_account_diagnosis"
down_revision = "20261117T1000_kuaishou_publish_platform"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 本功能只存在于 PostgreSQL 客户车道；离线归档工具不会遇到这两张表。
    if op.get_bind().dialect.name != "postgresql":
        return

    op.create_table(
        "account_diagnosis_reports",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("wallet_owner_id", sa.Text(), nullable=False),
        sa.Column("platform", sa.Text(), nullable=False, server_default="douyin"),
        sa.Column("sec_user_id", sa.Text(), nullable=True),
        sa.Column("unique_id", sa.Text(), nullable=True),
        sa.Column("input_url", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="PENDING"),
        sa.Column("data_level", sa.Text(), nullable=True),
        sa.Column("score", sa.Integer(), nullable=True),
        sa.Column("tier", sa.Text(), nullable=True),
        sa.Column("rating", sa.Text(), nullable=True),
        sa.Column("report_json", sa.Text(), nullable=True),
        sa.Column("narrative_json", sa.Text(), nullable=True),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("billed_credits", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_free", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("idempotency_key", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["wallet_owner_id"], ["users.id"]),
    )
    op.create_index(
        "ix_account_diagnosis_reports_owner_created",
        "account_diagnosis_reports",
        ["wallet_owner_id", "created_at"],
    )
    op.create_index(
        "ix_account_diagnosis_reports_status_created",
        "account_diagnosis_reports",
        ["status", "created_at"],
    )
    op.create_index(
        "ix_account_diagnosis_reports_sec_user_created",
        "account_diagnosis_reports",
        ["sec_user_id", "created_at"],
    )
    op.create_index(
        "uq_account_diagnosis_reports_idempotency",
        "account_diagnosis_reports",
        ["wallet_owner_id", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )

    op.create_table(
        "account_diagnosis_free_grants",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("device_id", sa.Text(), nullable=True),
        sa.Column("report_id", sa.Text(), nullable=True),
        sa.Column(
            "claimed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
    )
    op.create_index(
        "uq_account_diagnosis_free_grants_user",
        "account_diagnosis_free_grants",
        ["user_id"],
        unique=True,
    )
    # 同一设备只贡献一次免费额度；device_id 为空的匿名会话不参与设备去重。
    op.create_index(
        "uq_account_diagnosis_free_grants_device",
        "account_diagnosis_free_grants",
        ["device_id"],
        unique=True,
        postgresql_where=sa.text("device_id IS NOT NULL"),
    )

    # 默认售价 8 积分/次、成本 0.05 元/次：管理员随后可在「价格与套餐」直接改。
    # 种子必须 enabled=true，否则新功能上线后还要人工开价才能用。
    op.execute("""
        INSERT INTO billing_tariffs(service, enabled, unit_credits, unit_cost_fen, unit_rounding)
        VALUES ('account_diagnosis', true, 8, 5, 'exact')
        ON CONFLICT (service) DO NOTHING
    """)


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    # 已经产生过诊断计费就不允许静默删价：那是经营事实，必须先对账。
    op.execute("""
        DO $$ BEGIN
          IF EXISTS (
            SELECT 1 FROM billing_operations WHERE service = 'account_diagnosis'
          ) THEN
            RAISE EXCEPTION
              'account_diagnosis has billing history; archive reports before downgrade';
          END IF;
          DELETE FROM billing_tariffs WHERE service = 'account_diagnosis';
        END $$;
    """)
    op.drop_index(
        "uq_account_diagnosis_free_grants_device", table_name="account_diagnosis_free_grants"
    )
    op.drop_index(
        "uq_account_diagnosis_free_grants_user", table_name="account_diagnosis_free_grants"
    )
    op.drop_table("account_diagnosis_free_grants")
    op.drop_index(
        "uq_account_diagnosis_reports_idempotency", table_name="account_diagnosis_reports"
    )
    op.drop_index(
        "ix_account_diagnosis_reports_sec_user_created", table_name="account_diagnosis_reports"
    )
    op.drop_index(
        "ix_account_diagnosis_reports_status_created", table_name="account_diagnosis_reports"
    )
    op.drop_index(
        "ix_account_diagnosis_reports_owner_created", table_name="account_diagnosis_reports"
    )
    op.drop_table("account_diagnosis_reports")
