"""运营核对记录和只追加的收入凭证。"""

from alembic import op

revision = "20261125T1500_billing_review"
down_revision = "20261126T1900_asr_request_aliases"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return

    op.execute("""
        CREATE TABLE billing_review_cases (
          subject_id text NOT NULL,
          kind text NOT NULL CHECK(kind IN ('cost','revenue','legacy_settlement')),
          state text NOT NULL CHECK(state IN ('evidence','review','verified','unverifiable')),
          owner_id text NOT NULL REFERENCES users(id),
          next_step text NOT NULL,
          next_review_on date,
          evidence_ref text NOT NULL DEFAULT '',
          reviewed_by text REFERENCES users(id),
          updated_at timestamptz NOT NULL DEFAULT now(),
          version integer NOT NULL CHECK(version>0),
          PRIMARY KEY(kind,subject_id),
          CHECK(state <> 'verified' OR (reviewed_by IS NOT NULL AND length(trim(evidence_ref))>0))
        );
        CREATE TABLE billing_revenue_confirmations (
          operation_id text PRIMARY KEY REFERENCES billing_operations(id),
          revenue_fen numeric(24,8) NOT NULL CHECK(revenue_fen>=0),
          proof_json jsonb NOT NULL,
          reference text NOT NULL CHECK(length(trim(reference))>0),
          actor_user_id text NOT NULL REFERENCES users(id),
          created_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE FUNCTION billing_reject_revenue_confirmation_change()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          RAISE EXCEPTION 'billing_revenue_confirmations is append-only'
            USING ERRCODE = '23514';
        END
        $$;
        CREATE TRIGGER trg_billing_revenue_confirmations_immutable
          BEFORE UPDATE OR DELETE ON billing_revenue_confirmations
          FOR EACH ROW EXECUTE FUNCTION billing_reject_revenue_confirmation_change();
        CREATE TRIGGER trg_billing_revenue_confirmations_no_truncate
          BEFORE TRUNCATE ON billing_revenue_confirmations
          FOR EACH STATEMENT EXECUTE FUNCTION billing_reject_revenue_confirmation_change();
        CREATE VIEW billing_effective_operations AS
          SELECT operation.*, COALESCE(operation.revenue_fen, proof.revenue_fen)
            AS effective_revenue_fen
          FROM billing_operations AS operation
          LEFT JOIN billing_revenue_confirmations AS proof ON proof.operation_id=operation.id;
    """)


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return

    op.execute("""
        LOCK TABLE billing_revenue_confirmations, billing_review_cases IN ACCESS EXCLUSIVE MODE;
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM billing_revenue_confirmations)
             OR EXISTS (SELECT 1 FROM billing_review_cases) THEN
            RAISE EXCEPTION 'cannot downgrade billing_review with saved review evidence';
          END IF;
        END
        $$;
        DROP VIEW billing_effective_operations;
        DROP TABLE billing_revenue_confirmations;
        DROP TABLE billing_review_cases;
        DROP FUNCTION billing_reject_revenue_confirmation_change();
    """)
