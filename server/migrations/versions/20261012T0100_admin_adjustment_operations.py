"""调账业务身份跨操作者防重；既有账本和已发布迁移保持只读。"""

import sqlalchemy as sa
from alembic import op

revision = "20261012T0100_admin_adjustment_operations"
down_revision = "20261008T1600_support_tickets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("""
        CREATE TABLE admin_adjustment_operations (
            business_identity text PRIMARY KEY CHECK(length(business_identity)=64),
            request_hash text NOT NULL CHECK(length(request_hash)=64),
            adjustment_id text NOT NULL UNIQUE REFERENCES admin_adjustments(id) ON DELETE RESTRICT,
            response_body text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE TABLE admin_adjustment_operation_keys (
            operation_id text PRIMARY KEY CHECK(length(trim(operation_id)) BETWEEN 1 AND 128),
            business_identity text NOT NULL
                REFERENCES admin_adjustment_operations(business_identity) ON DELETE RESTRICT,
            created_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX idx_adjustment_operation_keys_identity
            ON admin_adjustment_operation_keys(business_identity);
        CREATE TRIGGER trg_adjustment_operations_append_only
            BEFORE UPDATE OR DELETE ON admin_adjustment_operations
            FOR EACH ROW EXECUTE FUNCTION admin_adjustments_refuse_rewrite();
        CREATE TRIGGER trg_adjustment_operation_keys_append_only
            BEFORE UPDATE OR DELETE ON admin_adjustment_operation_keys
            FOR EACH ROW EXECUTE FUNCTION admin_adjustments_refuse_rewrite();
        CREATE TRIGGER trg_adjustment_operations_no_truncate
            BEFORE TRUNCATE ON admin_adjustment_operations
            FOR EACH STATEMENT EXECUTE FUNCTION refuse_truncate_of_audit_tables();
        CREATE TRIGGER trg_adjustment_operation_keys_no_truncate
            BEFORE TRUNCATE ON admin_adjustment_operation_keys
            FOR EACH STATEMENT EXECUTE FUNCTION refuse_truncate_of_audit_tables();
    """)


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    # 防止首次调账在空表检查之后落库，回滚不能删除刚生成的防重证据。
    op.execute(
        "LOCK TABLE admin_adjustment_operations, admin_adjustment_operation_keys "
        "IN ACCESS EXCLUSIVE MODE"
    )
    if (
        op.get_bind()
        .execute(sa.text("SELECT EXISTS (SELECT 1 FROM admin_adjustment_operations)"))
        .scalar()
    ):
        raise RuntimeError("业务防重记录已产生，不能删除调账审计身份。")
    op.execute(
        "DROP TABLE admin_adjustment_operation_keys; DROP TABLE admin_adjustment_operations;"
    )
