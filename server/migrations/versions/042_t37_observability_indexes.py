"""T37 — bounded observability indexes plus fencing and authorization evidence.

The cluster-global anomaly probe runs from one session-lock-protected timer,
not from each API process. Shared ``ops_alert_state`` makes fired/resolved
edges consistent even if two OPS hosts run at different times. This revision
also gives its application-table queries index-backed entry points and extends
the existing append-only ``security_auth_failures`` fact stream with
``session:fencing``. A compact,
append-only fencing-write table keeps one expected/verified epoch fact per
session tuple so the probe can detect a stale write that actually committed.
No ``pg_monitor`` or ``pg_stat_*`` privilege is required.

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
    op.create_table(
        "ops_alert_state",
        sa.Column("alert_name", sa.Text(), primary_key=True),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("clock_timestamp()"),
        ),
        sa.CheckConstraint(
            "alert_name IN ("
            "'double_online', 'duplicate_charge', 'duplicate_provider_trade_no', "
            "'paid_without_charge', 'charge_without_paid_source', "
            "'wallet_balance_mismatch', 'fencing_rejected', "
            "'stale_write_committed', 'cross_user_access', "
            "'queue_starvation', 'dangling_reserve')",
            name="ck_ops_alert_state_name",
        ),
    )
    op.create_table(
        "customer_authorization_evidence",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("request_id", sa.Text(), nullable=False),
        sa.Column("resource_type", sa.Text(), nullable=False),
        sa.Column("actor_digest", sa.Text(), nullable=False),
        sa.Column("owner_digest", sa.Text(), nullable=False),
        sa.Column(
            "observed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("clock_timestamp()"),
        ),
        sa.CheckConstraint(
            "char_length(request_id) BETWEEN 1 AND 128",
            name="ck_customer_authorization_request_id",
        ),
        sa.CheckConstraint(
            "resource_type IN ('project', 'asset')",
            name="ck_customer_authorization_resource_type",
        ),
        sa.CheckConstraint(
            "actor_digest ~ '^[0-9a-f]{64}$' AND owner_digest ~ '^[0-9a-f]{64}$'",
            name="ck_customer_authorization_digests",
        ),
        sa.UniqueConstraint(
            "resource_type",
            "actor_digest",
            "owner_digest",
            name="uq_customer_authorization_digest_pair",
        ),
    )
    op.create_index(
        "idx_customer_authorization_mismatch",
        "customer_authorization_evidence",
        ["observed_at", "id"],
        postgresql_where=sa.text("actor_digest <> owner_digest"),
    )
    op.execute(
        """
        CREATE FUNCTION customer_authorization_evidence_refuse_rewrite()
        RETURNS trigger AS $refuse$
        BEGIN
            RAISE EXCEPTION 'customer_authorization_evidence is append-only';
        END;
        $refuse$ LANGUAGE plpgsql;

        CREATE TRIGGER trg_customer_authorization_evidence_append_only
        BEFORE UPDATE OR DELETE ON customer_authorization_evidence
        FOR EACH ROW EXECUTE FUNCTION customer_authorization_evidence_refuse_rewrite();

        CREATE TRIGGER trg_customer_authorization_evidence_no_truncate
        BEFORE TRUNCATE ON customer_authorization_evidence
        FOR EACH STATEMENT EXECUTE FUNCTION refuse_truncate_of_audit_tables();
        """
    )
    op.create_table(
        "customer_fencing_write_evidence",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("request_id", sa.Text(), nullable=False),
        sa.Column("subject_digest", sa.Text(), nullable=False),
        sa.Column("expected_session_epoch", sa.BigInteger(), nullable=False),
        sa.Column("verified_session_epoch", sa.BigInteger(), nullable=False),
        sa.Column(
            "committed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("clock_timestamp()"),
        ),
        sa.CheckConstraint(
            "char_length(request_id) BETWEEN 1 AND 128",
            name="ck_customer_fencing_write_request_id",
        ),
        sa.CheckConstraint(
            "subject_digest ~ '^[0-9a-f]{64}$'",
            name="ck_customer_fencing_write_subject_digest",
        ),
        sa.CheckConstraint(
            "expected_session_epoch >= 1 AND verified_session_epoch >= 1",
            name="ck_customer_fencing_write_epochs",
        ),
        sa.UniqueConstraint(
            "subject_digest",
            "expected_session_epoch",
            "verified_session_epoch",
            name="uq_customer_fencing_write_epoch_fact",
        ),
    )
    op.create_index(
        "idx_customer_fencing_write_mismatch",
        "customer_fencing_write_evidence",
        ["committed_at", "id"],
        postgresql_where=sa.text("expected_session_epoch <> verified_session_epoch"),
    )
    op.execute(
        """
        CREATE FUNCTION customer_fencing_write_evidence_refuse_rewrite()
        RETURNS trigger AS $refuse$
        BEGIN
            RAISE EXCEPTION 'customer_fencing_write_evidence is append-only';
        END;
        $refuse$ LANGUAGE plpgsql;

        CREATE TRIGGER trg_customer_fencing_write_evidence_append_only
        BEFORE UPDATE OR DELETE ON customer_fencing_write_evidence
        FOR EACH ROW EXECUTE FUNCTION customer_fencing_write_evidence_refuse_rewrite();

        CREATE TRIGGER trg_customer_fencing_write_evidence_no_truncate
        BEFORE TRUNCATE ON customer_fencing_write_evidence
        FOR EACH STATEMENT EXECUTE FUNCTION refuse_truncate_of_audit_tables();
        """
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    has_t37_evidence = bind.execute(
        sa.text(
            "SELECT EXISTS ("
            "SELECT 1 FROM security_auth_failures "
            "WHERE dimension IN ('admin:exchange:ip', 'session:fencing') LIMIT 1) "
            "OR EXISTS (SELECT 1 FROM customer_fencing_write_evidence LIMIT 1) "
            "OR EXISTS (SELECT 1 FROM customer_authorization_evidence LIMIT 1)"
        )
    ).scalar()
    if has_t37_evidence:
        raise RuntimeError(
            "cannot downgrade 042_t37_observability_indexes: append-only "
            "T37 security, authorization, or committed-write evidence exists and the prior "
            "schema cannot represent it. Keep revision 042 or export and retain "
            "the security audit trail before a manually supervised rollback."
        )

    op.execute(
        "DROP TRIGGER IF EXISTS trg_customer_fencing_write_evidence_no_truncate "
        "ON customer_fencing_write_evidence"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_customer_fencing_write_evidence_append_only "
        "ON customer_fencing_write_evidence"
    )
    op.execute("DROP FUNCTION IF EXISTS customer_fencing_write_evidence_refuse_rewrite()")
    op.drop_index(
        "idx_customer_fencing_write_mismatch",
        table_name="customer_fencing_write_evidence",
    )
    op.drop_table("customer_fencing_write_evidence")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_customer_authorization_evidence_no_truncate "
        "ON customer_authorization_evidence"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_customer_authorization_evidence_append_only "
        "ON customer_authorization_evidence"
    )
    op.execute("DROP FUNCTION IF EXISTS customer_authorization_evidence_refuse_rewrite()")
    op.drop_index(
        "idx_customer_authorization_mismatch",
        table_name="customer_authorization_evidence",
    )
    op.drop_table("customer_authorization_evidence")
    op.drop_table("ops_alert_state")
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
