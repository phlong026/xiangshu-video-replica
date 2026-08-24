"""Fix provider_settings check constraint naming inconsistency.

The original migration 002 created an unnamed CHECK constraint:
    provider IN ('apilio', 'metaso', 'cos', 'oss')

PostgreSQL automatically named this as "provider_settings_provider_check" instead
of the conventional "ck_provider_settings_supported_provider". Subsequent migrations
(018, 020, 023) attempted to drop/rename a non-existent constraint called
"ck_provider_settings_supported_provider", which failed silently.

This migration:
1. Drops the auto-generated "provider_settings_provider_check" constraint
2. Recreates it with the proper name and updated provider list including 'zpay'
3. Ensures future migrations can properly manage this constraint

This fixes T22's inability to insert provider='zpay' rows in test environments.
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
        # SQLite doesn't use provider_settings table
        return
    
    # Drop ALL existing constraints on provider column (both old and new names)
    try:
        with op.batch_alter_table("provider_settings") as batch_op:
            batch_op.drop_constraint(
                "provider_settings_provider_check", 
                type_="check"
            )
    except Exception:
        # Constraint might not exist (fresh install), ignore error
        pass
    
    try:
        with op.batch_alter_table("provider_settings") as batch_op:
            batch_op.drop_constraint(
                "ck_provider_settings_supported_provider", 
                type_="check"
            )
    except Exception:
        # Might have been created by a previous failed attempt, ignore
        pass
    
    # Recreate with proper name and current supported providers
    with op.batch_alter_table("provider_settings") as batch_op:
        batch_op.create_check_constraint(
            "ck_provider_settings_supported_provider",
            "provider IN ('apilio', 'metaso', 'cos', 'deepseek', 'zpay')",
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    
    # Revert to pre-023 state (without zpay)
    try:
        with op.batch_alter_table("provider_settings") as batch_op:
            batch_op.drop_constraint(
                "ck_provider_settings_supported_provider", 
                type_="check"
            )
            batch_op.create_check_constraint(
                "ck_provider_settings_supported_provider",
                "provider IN ('apilio', 'metaso', 'cos', 'deepseek')",
            )
    except Exception:
        # Constraint might not exist, ignore error
        pass
