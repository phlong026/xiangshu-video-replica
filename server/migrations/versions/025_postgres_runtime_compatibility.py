"""PostgreSQL runtime compatibility fix for the terminal-round billing index.

M0 review C1 (docs/客户版V3-M0评审报告-2026-08-21.md §3): revision 022 declared
``uq_wallet_transactions_terminal_round`` with only ``sqlite_where``. On
PostgreSQL the predicate is silently dropped, so the index degrades into a
table-wide unique index over ``(task_id, billing_round)``. RESERVE and the
terminal SETTLE/RELEASE rows share that key by design
(``app/internal_billing.py``), so on PG no generation task could ever be
settled or released — verified by an on-instance reproduction
(UniqueViolation on the very first SETTLE after a RESERVE).

Per DB-04's No-Go rule ("任一历史迁移仅能追加修复，不得篡改已发布 revision")
the published 022 revision stays untouched; this append-only revision
recreates the index with the matching ``postgresql_where`` predicate. On
SQLite the 022 partial index is already correct, so this revision is a no-op
there and the legacy runtime schema is unchanged.

Revision ID: 025_postgres_runtime_compatibility
Revises: 024_wallet_backfill
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "025_postgres_runtime_compatibility"
down_revision = "024_wallet_backfill"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # SQLite honours sqlite_where, so the 022 index is already partial
        # and correct there; the defect only exists on PostgreSQL.
        return
    op.drop_index("uq_wallet_transactions_terminal_round", table_name="wallet_transactions")
    op.create_index(
        "uq_wallet_transactions_terminal_round",
        "wallet_transactions",
        ["task_id", "billing_round"],
        unique=True,
        postgresql_where=sa.text("type IN ('SETTLE', 'RELEASE')"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    # Restore the schema exactly as the published chain left it on PostgreSQL
    # (the degraded table-wide unique index), so downgrade/base/upgrade
    # rehearsals stay symmetric with 022 as shipped.
    op.drop_index("uq_wallet_transactions_terminal_round", table_name="wallet_transactions")
    op.create_index(
        "uq_wallet_transactions_terminal_round",
        "wallet_transactions",
        ["task_id", "billing_round"],
        unique=True,
    )
