"""T08 / DB-07 — billing provider, pricing_scope and conditional constraints.

The published 022 revision locked ``recharge_orders`` to the internal shape
(``provider = 'zpay'``, ``pricing_scope = 'INTERNAL'``) because that was the
only funding source in the internal P0 product. The customer V3 line adds:

- ``zpay`` — internal recharges (PENDING -> PAID/CLOSED/FAILED with a provider
  trade number once paid) and, per T22, customer top-ups in the
  ``CUSTOMER_STANDARD`` price scope;
- ``activation_code`` — the first-charge order created atomically inside the
  activation transaction (dev doc §12.1: "创建 provider=activation_code、
  status=PAID 的充值单"), customer-priced by the batch face value, with no
  third-party trade number;
- ``admin_adjustment`` — audited admin credits (T23/BILL-02), created PAID by
  the double-confirmed adjustment transaction, no trade number.

Per DB-07's exit gate these shapes are enforced by PostgreSQL CHECK
constraints, not application code, and each provider keeps only its legal
shape: scope pairing, paid-on-creation for non-zpay providers, trade-number
presence rules, a customer price floor (PRICE-01: customer prices never
undercut the internal base price), and min/step ladder applicability limited
to zpay (activation face values and adjustment amounts are defined by their
source documents).

PostgreSQL is the customer production source of truth, so this revision only
executes there (025 precedent). SQLite remains the internal P0 runtime and
the T07 read-only import source; its recharge_orders only ever holds
zpay/INTERNAL rows, which the 022 constraints already model exactly.

Revision ID: 026_customer_security_and_billing
Revises: 025_postgres_runtime_compatibility
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "026_customer_security_and_billing"
down_revision = "025_postgres_runtime_compatibility"
branch_labels = None
depends_on = None


_PROVIDER_ENUM = "provider IN ('zpay', 'activation_code', 'admin_adjustment')"
_PRICING_SCOPE_ENUM = "pricing_scope IN ('INTERNAL', 'CUSTOMER_STANDARD')"
# activation_code is a customer product only; zpay and admin_adjustment work
# in both scopes (internal recharges and customer top-ups/credits).
_PROVIDER_SCOPE_PAIRING = (
    "(provider = 'activation_code' AND pricing_scope = 'CUSTOMER_STANDARD') OR "
    "(provider IN ('zpay', 'admin_adjustment') "
    "AND pricing_scope IN ('INTERNAL', 'CUSTOMER_STANDARD'))"
)
# Non-zpay orders are created inside their atomic business transaction and
# land PAID; only zpay runs a PENDING -> PAID payment lifecycle.
_PROVIDER_STATUS = (
    "provider = 'zpay' OR (provider IN ('activation_code', 'admin_adjustment') AND status = 'PAID')"
)
# A paid ZPay order always carries its third-party trade number (written by the
# verified notify flow); activation/adjustment orders have no third party.
_PROVIDER_TRADE_NO = (
    "(provider = 'zpay' AND (status != 'PAID' OR provider_trade_no IS NOT NULL)) OR "
    "(provider IN ('activation_code', 'admin_adjustment') AND provider_trade_no IS NULL)"
)
# PRICE-01 floor: a customer price must never undercut the internal base price.
_CUSTOMER_PRICE_FLOOR = (
    "pricing_scope = 'INTERNAL' OR charged_unit_price_fen_snapshot >= base_unit_price_fen_snapshot"
)
# The min/step recharge ladder only governs zpay orders; activation face
# values and audited adjustment amounts are defined by their source documents.
_AMOUNT_MINIMUM = "provider != 'zpay' OR amount_fen >= min_recharge_fen_snapshot"
_AMOUNT_STEP = "provider != 'zpay' OR amount_fen % recharge_step_fen_snapshot = 0"

# Constraints replaced by this revision (the rest of 022 stays untouched).
_REPLACED_CONSTRAINTS = (
    "ck_recharge_orders_provider",
    "ck_recharge_orders_pricing_scope",
    "ck_recharge_orders_amount_minimum",
    "ck_recharge_orders_amount_step",
)

_NEW_CONSTRAINTS: tuple[tuple[str, str], ...] = (
    ("ck_recharge_orders_provider", _PROVIDER_ENUM),
    ("ck_recharge_orders_pricing_scope", _PRICING_SCOPE_ENUM),
    ("ck_recharge_orders_provider_scope", _PROVIDER_SCOPE_PAIRING),
    ("ck_recharge_orders_provider_status", _PROVIDER_STATUS),
    ("ck_recharge_orders_provider_trade_no", _PROVIDER_TRADE_NO),
    ("ck_recharge_orders_customer_price_floor", _CUSTOMER_PRICE_FLOOR),
    ("ck_recharge_orders_amount_minimum", _AMOUNT_MINIMUM),
    ("ck_recharge_orders_amount_step", _AMOUNT_STEP),
)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # SQLite: internal P0 runtime / T07 import source only — every row is
        # zpay/INTERNAL, which the published 022 constraints already enforce.
        return
    for name in _REPLACED_CONSTRAINTS:
        op.drop_constraint(name, "recharge_orders", type_="check")
    for name, condition in _NEW_CONSTRAINTS:
        op.create_check_constraint(name, "recharge_orders", condition)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    # Once activation-code or adjustment orders exist, the 022 constraint set
    # (zpay/INTERNAL only) cannot hold them. Confirmed billing rows must never
    # be deleted (No-Go rule), so fail loudly with the recovery path spelled
    # out instead of bricking the rollback; a zpay/INTERNAL-only ledger still
    # downgrades symmetrically with the published chain.
    has_customer_rows = bind.execute(
        sa.text(
            "SELECT EXISTS ("
            "  SELECT 1 FROM recharge_orders"
            "  WHERE provider != 'zpay' OR pricing_scope != 'INTERNAL')"
        )
    ).scalar()
    if has_customer_rows:
        raise RuntimeError(
            "cannot downgrade 026_customer_security_and_billing: "
            "recharge_orders already holds activation_code/admin_adjustment or "
            "CUSTOMER_STANDARD rows, which the 022 zpay/INTERNAL-only constraints "
            "cannot hold. Keep revision 026, or resolve the ledger manually "
            "(per the No-Go rule, confirmed billing rows must never be deleted "
            "to make a downgrade pass) before rolling back."
        )
    for name, _condition in _NEW_CONSTRAINTS:
        op.drop_constraint(name, "recharge_orders", type_="check")
    # Restore the published 022 shapes verbatim.
    op.create_check_constraint(
        "ck_recharge_orders_provider", "recharge_orders", "provider = 'zpay'"
    )
    op.create_check_constraint(
        "ck_recharge_orders_pricing_scope", "recharge_orders", "pricing_scope = 'INTERNAL'"
    )
    op.create_check_constraint(
        "ck_recharge_orders_amount_minimum",
        "recharge_orders",
        "amount_fen >= min_recharge_fen_snapshot",
    )
    op.create_check_constraint(
        "ck_recharge_orders_amount_step",
        "recharge_orders",
        "amount_fen % recharge_step_fen_snapshot = 0",
    )
