"""新增原充值退款来源证据；不迁移、回填或改写任何客户历史账。"""

import sqlalchemy as sa
from alembic import op

revision = "20261012T0300_refund_source_allocations"
down_revision = "20261012T0200_character_request_keys"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("""
        CREATE TABLE admin_refund_allocations (
            adjustment_id text PRIMARY KEY REFERENCES admin_adjustments(id) ON DELETE RESTRICT,
            refund_transaction_id text NOT NULL UNIQUE
                REFERENCES wallet_transactions(id) ON DELETE RESTRICT,
            original_recharge_order_id text NOT NULL
                REFERENCES recharge_orders(id) ON DELETE RESTRICT,
            credit_lot_id text NOT NULL REFERENCES billing_credit_lots(id) ON DELETE RESTRICT,
            credits integer NOT NULL CHECK(credits>0),
            source_credits integer NOT NULL CHECK(source_credits>0),
            source_amount_fen numeric(24,8) NOT NULL CHECK(source_amount_fen>0),
            lot_remaining_before integer NOT NULL CHECK(lot_remaining_before>=0),
            lot_remaining_after integer NOT NULL CHECK(lot_remaining_after>=0),
            created_at timestamptz NOT NULL DEFAULT now(),
            CHECK(lot_remaining_before<=source_credits),
            CHECK(lot_remaining_before-lot_remaining_after=credits),
            CHECK(refund_transaction_id='admin_adjustment:refund:' || adjustment_id)
        );
        CREATE INDEX idx_refund_allocations_source ON admin_refund_allocations(credit_lot_id);
        CREATE TRIGGER trg_refund_allocations_append_only
            BEFORE UPDATE OR DELETE ON admin_refund_allocations
            FOR EACH ROW EXECUTE FUNCTION admin_adjustments_refuse_rewrite();
        CREATE TRIGGER trg_refund_allocations_no_truncate
            BEFORE TRUNCATE ON admin_refund_allocations
            FOR EACH STATEMENT EXECUTE FUNCTION refuse_truncate_of_audit_tables();
    """)


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("LOCK TABLE admin_refund_allocations IN ACCESS EXCLUSIVE MODE")
    if (
        op.get_bind()
        .execute(sa.text("SELECT EXISTS (SELECT 1 FROM admin_refund_allocations)"))
        .scalar()
    ):
        raise RuntimeError("退款来源证据已产生，不能删除历史分配记录。")
    op.execute("DROP TABLE admin_refund_allocations")
