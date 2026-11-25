"""Add annual software authorization and backfill existing customer accounts."""

import sqlalchemy as sa
from alembic import op

revision = "20261125T1000_software_licenses"
down_revision = "20261124T1000_recharge_minimum_step"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.add_column(
        "runtime_settings",
        sa.Column(
            "software_activation_auto_extend",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.create_table(
        "software_licenses",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("masked_code", sa.Text(), nullable=False),
        sa.Column("code_digest", sa.Text(), unique=True),
        sa.Column("ciphertext", sa.Text()),
        sa.Column("export_key_version", sa.Integer()),
        sa.Column("activation_expires_at", sa.DateTime(timezone=True)),
        sa.Column("authorization_years", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Text(), sa.ForeignKey("users.id", ondelete="CASCADE"), unique=True),
        sa.Column("bound_at", sa.DateTime(timezone=True)),
        sa.Column("authorized_until", sa.DateTime(timezone=True)),
        sa.Column("created_by_user_id", sa.Text(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("authorization_years BETWEEN 1 AND 3", name="ck_software_license_years"),
        sa.CheckConstraint(
            "(user_id IS NULL AND bound_at IS NULL AND authorized_until IS NULL "
            "AND code_digest IS NOT NULL) "
            "OR (user_id IS NOT NULL AND bound_at IS NOT NULL AND authorized_until IS NOT NULL)",
            name="ck_software_license_binding",
        ),
        sa.CheckConstraint(
            "(code_digest IS NULL AND ciphertext IS NULL AND export_key_version IS NULL "
            "AND activation_expires_at IS NULL) OR "
            "(code_digest IS NOT NULL AND ciphertext IS NOT NULL "
            "AND export_key_version IS NOT NULL AND activation_expires_at IS NOT NULL)",
            name="ck_software_license_code",
        ),
    )
    op.create_index("ix_software_licenses_created", "software_licenses", ["created_at", "id"])
    # 存量主账号统一从迁移起算一年。已绑定的授权记录没有可再次兑换的凭据。
    op.execute(
        """
        WITH masters AS (
            SELECT u.id AS user_id, gen_random_uuid()::text AS license_id, c.masked_code
            FROM users u
            LEFT JOIN activation_code_activations a ON a.user_id = u.id
            LEFT JOIN activation_codes c ON c.id = a.code_id
            WHERE u.role = 'customer' AND u.account_type = 'MASTER'
        )
        INSERT INTO software_licenses
            (id, masked_code, authorization_years, user_id, bound_at, authorized_until)
        SELECT license_id, COALESCE(masked_code, '已绑定-' || upper(left(license_id, 8))),
            1, user_id, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP + interval '1 year'
        FROM masters
        """
    )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.drop_table("software_licenses")
    op.drop_column("runtime_settings", "software_activation_auto_extend")
