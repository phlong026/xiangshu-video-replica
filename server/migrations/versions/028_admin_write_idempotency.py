"""T12 / ACT-04 — admin write idempotency.

The code checklist §3.1 freezes migration *themes*, not numbers
("实际编号以实施当日 Alembic head 为准"), and the admin-write idempotency
table has no frozen column-level design in any published revision — dev doc
§11.3 only fixes the invariant ("管理写幂等键按 actor、canonical route 和
key digest 唯一"). This revision lands that data layer at the current head
(026 precedent: the shared rate-limit data lands under the same clause when
T15 arrives, taking the then-current head as well).

``admin_write_idempotency`` snapshots one admin write per
(actor, canonical route, idempotency key digest):

- ``route`` is the canonical route template ("POST
  /api/control/activation-code-batches"), so the same key against a
  different resource path can never collide silently;
- the raw key never reaches the database (sha256 digest, the
  ``admin_sessions`` precedent);
- ``request_hash`` freezes the canonical request (route + JSON body), so the
  same key with a different body is rejected as an idempotency conflict
  instead of replaying an unrelated stored response;
- ``response_status`` / ``response_body`` persist the committed response so
  a retry after a lost response replays the same business output.

Rows live inside the same transaction as the business write: a placeholder
``INSERT ... ON CONFLICT DO NOTHING`` serializes concurrent same-key
writers (PostgreSQL waits on the conflicting insert), the winner back-fills
the response snapshot before commit, and a rolled-back business failure
releases the key with the transaction. The paired NULL / NOT NULL response
columns make an unfinished placeholder visible to the CHECK itself.

PostgreSQL only (025–027 precedent): admin activation writes are a
customer-production concern; the internal SQLite lane keeps its legacy
control path and never grows this table.

Revision ID: 028_admin_write_idempotency
Revises: 027_activation_code_catalog
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "028_admin_write_idempotency"
down_revision = "027_activation_code_catalog"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # SQLite: internal P0 runtime — the admin activation API and its
        # idempotency snapshots are customer-production only.
        return
    op.create_table(
        "admin_write_idempotency",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "actor_user_id",
            sa.Text(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("route", sa.Text(), nullable=False),
        sa.Column("idempotency_key_digest", sa.Text(), nullable=False),
        sa.Column("request_hash", sa.Text(), nullable=False),
        sa.Column("response_status", sa.Integer()),
        sa.Column("response_body", sa.Text()),
        sa.Column(
            "created_at",
            sa.Text(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "length(trim(route)) > 0",
            name="ck_admin_write_idempotency_route_not_blank",
        ),
        sa.CheckConstraint(
            "length(trim(idempotency_key_digest)) > 0",
            name="ck_admin_write_idempotency_key_digest_not_blank",
        ),
        sa.CheckConstraint(
            "length(trim(request_hash)) > 0",
            name="ck_admin_write_idempotency_request_hash_not_blank",
        ),
        sa.CheckConstraint(
            # A finished snapshot carries both response halves; a placeholder
            # carries neither. Half-written rows fail closed here.
            "(response_status IS NULL) = (response_body IS NULL)",
            name="ck_admin_write_idempotency_response_pair",
        ),
    )
    op.create_index(
        "uq_admin_write_idempotency_actor_route_key",
        "admin_write_idempotency",
        ["actor_user_id", "route", "idempotency_key_digest"],
        unique=True,
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    # Idempotency snapshots are replay caches, not business facts — dropping
    # them only costs operators a re-issue, so the downgrade is symmetric.
    op.drop_index(
        "uq_admin_write_idempotency_actor_route_key",
        table_name="admin_write_idempotency",
    )
    op.drop_table("admin_write_idempotency")
