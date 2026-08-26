"""T37 — bounded observability indexes and fencing audit vocabulary.

The cluster-global anomaly probe runs from one advisory-lock-protected timer,
not from each API process. This revision gives its application-table queries
index-backed entry points and extends the existing append-only
``security_auth_failures`` fact stream with ``session:fencing``. No
``pg_monitor`` or ``pg_stat_*`` privilege is required.

Revision ID: 042_t37_observability_indexes
Revises: 041_user_fair_queue
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "042_t37_observability_indexes"
down_revision = "041_user_fair_queue"
branch_labels = None
depends_on = None

_OLD_DIMENSIONS = "dimension IN ('activate:ip', 'activate:code', 'login:ip', 'login:account')"
_NEW_DIMENSIONS = (
    "dimension IN ('activate:ip', 'activate:code', 'login:ip', 'login:account', "
    "'admin:exchange:ip', 'session:fencing')"
)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # T37 observability is customer multi-instance infrastructure. The
        # internal SQLite P0 lane keeps its existing schema and local logs.
        return

    op.drop_constraint(
        "ck_security_auth_failures_dimension",
        "security_auth_failures",
        type_="check",
    )
    op.create_check_constraint(
        "ck_security_auth_failures_dimension",
        "security_auth_failures",
        _NEW_DIMENSIONS,
    )
    op.create_index(
        "idx_customer_session_events_event_created_at",
        "customer_session_events",
        ["event", "created_at"],
    )
    op.create_index(
        "idx_recharge_orders_status_paid_at",
        "recharge_orders",
        ["status", "paid_at"],
    )
    op.create_index(
        "idx_wallet_transactions_user_created_at",
        "wallet_transactions",
        ["user_id", "created_at"],
    )
    op.create_index(
        "idx_wallets_updated_at_user",
        "wallets",
        ["updated_at", "user_id"],
    )
    op.create_index(
        "idx_audit_logs_action_created_at",
        "audit_logs",
        ["action", "created_at"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    has_t37_evidence = bind.execute(
        sa.text(
            "SELECT EXISTS ("
            "SELECT 1 FROM security_auth_failures "
            "WHERE dimension IN ('admin:exchange:ip', 'session:fencing') LIMIT 1)"
        )
    ).scalar()
    if has_t37_evidence:
        raise RuntimeError(
            "cannot downgrade 042_t37_observability_indexes: append-only "
            "T37 security evidence exists and the prior dimension constraint "
            "cannot represent it. Keep revision 042 or export and retain the "
            "security audit trail before a manually supervised rollback."
        )

    op.drop_index("idx_audit_logs_action_created_at", table_name="audit_logs")
    op.drop_index("idx_wallets_updated_at_user", table_name="wallets")
    op.drop_index(
        "idx_wallet_transactions_user_created_at",
        table_name="wallet_transactions",
    )
    op.drop_index(
        "idx_recharge_orders_status_paid_at",
        table_name="recharge_orders",
    )
    op.drop_index(
        "idx_customer_session_events_event_created_at",
        table_name="customer_session_events",
    )
    op.drop_constraint(
        "ck_security_auth_failures_dimension",
        "security_auth_failures",
        type_="check",
    )
    op.create_check_constraint(
        "ck_security_auth_failures_dimension",
        "security_auth_failures",
        _OLD_DIMENSIONS,
    )
