"""Collapse the two overlapping provider_settings CHECK constraints.

Migration 002 declared an unnamed column CHECK
    provider IN ('apilio', 'metaso', 'cos', 'oss')
which PostgreSQL auto-named ``provider_settings_provider_check``. Migration 018
(retiring oss) never touched that constraint; it ADDED a second, narrower,
properly named one — ``ck_provider_settings_supported_provider`` over
('apilio', 'metaso', 'cos'). Migrations 020/023 correctly dropped and rebuilt
that named constraint to admit deepseek and then zpay, but the 002 auto-named
constraint kept its original four-provider list the whole time, so inserts of
provider='deepseek' (since 020) and provider='zpay' (since 023) were rejected
on the PostgreSQL lane — which is why T22 could not seed its ZPay config.

This migration drops BOTH constraints (both provably exist at this point in
the linear chain: 002 created the auto-named one, 018+023 the named one) and
recreates a single named constraint admitting all five providers.

Downgrade restores the pre-023 allowlist (no zpay) after deleting any zpay
rows first — the 020-lineage protection: rebuilding a narrower constraint
over surviving rows would violate it. It also re-creates the 002 auto-named
constraint (four providers incl. oss) so the 040 -> 039 -> 040 round trip is
symmetric.
"""

from __future__ import annotations

from alembic import op

revision = "040_fix_provider_settings_constraint"
down_revision = "039_admin_adjustments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # SQLite lane: provider_settings writes never reach this table there.
        return

    with op.batch_alter_table("provider_settings") as batch_op:
        batch_op.drop_constraint("provider_settings_provider_check", type_="check")
        batch_op.drop_constraint("ck_provider_settings_supported_provider", type_="check")
        batch_op.create_check_constraint(
            "ck_provider_settings_supported_provider",
            "provider IN ('apilio', 'metaso', 'cos', 'deepseek', 'zpay')",
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    op.execute("DELETE FROM provider_settings WHERE provider = 'zpay'")
    with op.batch_alter_table("provider_settings") as batch_op:
        batch_op.drop_constraint("ck_provider_settings_supported_provider", type_="check")
        batch_op.create_check_constraint(
            "ck_provider_settings_supported_provider",
            "provider IN ('apilio', 'metaso', 'cos', 'deepseek')",
        )
        # Restore the 002 column-CHECK under its PostgreSQL auto-name so a
        # later re-upgrade of 040 finds the constraint it expects to drop.
        batch_op.create_check_constraint(
            "provider_settings_provider_check",
            "provider IN ('apilio', 'metaso', 'cos', 'oss')",
        )
