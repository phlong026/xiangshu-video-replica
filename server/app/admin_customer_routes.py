"""T23 / BILL-02 — audited admin adjustments API.

Application layer on top of the T26/T27 billing schema (recharge_orders, wallets,
wallet_transactions). Every adjustment lands as one atomic transaction that writes:

1. A `provider='admin_adjustment'` `status='PAID'` recharge order (revision 026 shapes;
   no third-party trade number, created PAID by double confirmation)
2. A wallet `CHARGE` ledger row with `available_delta = credits`, `task_id=NULL`
3. The atomic wallet credit increment (available_credits += credits)
4. One append-only `admin_adjustments` audit row naming the real administrator (§15)

Write contract (dev doc §15): every write behind the full admin gate — real admin
session (auditors are read-only), Idempotency-Key header (400 without it),
confirm=true (400 CONFIRMATION_REQUIRED), non-blank reason (400 REASON_REQUIRED).

Idempotency (revision 031 snapshot layer): each business write runs inside a
PostgreSQL transaction that inserts an `admin_write_idempotency` placeholder keyed by
(actor, route, key digest). The winner back-fills the response snapshot before commit;
same-key retry replays the stored response (X-Idempotent-Replay: true); same key against
different params answers 409 IDEMPOTENCY_CONFLICT. Business failure rolls the placeholder
back so the key stays reusable.

Amount calculation: amount_fen = credits * the customer's effective unit price frozen
on the order. The internal base price remains a separate reporting snapshot and does
not constrain the customer sale price.

Pricing scope inference: a target user bound to an activation code is CUSTOMER_STANDARD;
an internal account stays INTERNAL (revision 026 pairing).

Zero ledger difference invariant: after any adjustment the wallet balance grew by exactly
credits, the order is PAID with credits, and one CHARGE row references it — no balance
mutation without its ledger row (禁止直接改余额).

Fail-closed runtime: SQLite/missing DSN returns 503 ADJUSTMENT_SERVICE_UNAVAILABLE instead
of falling back to legacy control identity (the T12/T18 precedent).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from typing import Never, cast

import psycopg
from fastapi import APIRouter, Request, Response
from fastapi.responses import Response as HttpResponse
from pydantic import BaseModel, ConfigDict, StrictInt

from app.admin_auth_routes import AdminReader, AdminWriter
from app.admin_write_contract import (
    AdminWriteActor,
    DeferredHTTPWriteError,
)
from app.admin_write_contract import (
    AdminWriteContract as AdminWriteRequest,
)
from app.admin_write_contract import (
    http_error as _http,
)
from app.admin_write_contract import (
    transaction_now_iso as _transaction_now_iso,
)
from app.admin_write_contract import (
    write_with_idempotency as _shared_write_with_idempotency,
)
from app.auth import CurrentUser, Role
from app.db_pg import MissingDatabaseConfigError, pg_transaction
from app.db_portable import BusinessConnection
from app.permissions import write_audit
from app.security_rate_limit import (
    DIMENSION_CONTROL_EXPORT_ACCOUNT,
    consume_rate_limit,
    control_export_account_limit,
    rate_limit_window_seconds,
)
from app.settings import apply_customer_unit_price

router = APIRouter(prefix="/api/control", tags=["admin-customers"])


def _sqlite_lane() -> bool:
    """True on the internal SQLite lane (no VIDEO_REPLICA_DATABASE_URL)."""
    import os

    from app.db_pg import DATABASE_URL_ENV

    return not bool(os.environ.get(DATABASE_URL_ENV, "").strip())


def _write_with_idempotency(
    request: Request,
    response: Response,
    actor: AdminWriteActor,
    body: AdminWriteRequest,
    business: Callable[[psycopg.Connection, str], dict[str, object]],
    *,
    success_status: int = 201,
    unavailable_code: str = "ADJUSTMENT_SERVICE_UNAVAILABLE",
    unavailable_message: str = "Admin adjustments require the PostgreSQL runtime.",
) -> dict[str, object]:
    """The shared envelope bound to the customer lane's fail-closed defaults."""
    return _shared_write_with_idempotency(
        request,
        response,
        actor,
        body,
        business,
        success_status=success_status,
        unavailable_code=unavailable_code,
        unavailable_message=unavailable_message,
    )


# The frozen source-document enum (来源单类型, revision 039 CHECK constraint).
SOURCE_DOCUMENT_TYPES = (
    "CS_TICKET",
    "REFUND_APPROVAL",
    "COMPENSATION_APPROVAL",
    "LEDGER_CORRECTION",
    # 运营发放免费生成条数（054）：不产生支付金额，amount_fen 记 0。
    "FREE_GRANT",
)


# ---------------------------------------------------------------------------
# Admin write contract (dev doc §15)
# ---------------------------------------------------------------------------


class AdjustmentRequest(AdminWriteRequest):
    """Shared request shape for every admin adjustment write."""

    credits: int = 0
    source_document_type: str = ""
    source_document_ref: str = ""


# ---------------------------------------------------------------------------
# Pricing scope inference
# ---------------------------------------------------------------------------


def _infer_pricing_scope(conn: psycopg.Connection, user_id: str) -> str:
    """Infer pricing scope from whether the target user has activation binding.

    A user bound to a *current* activation code is CUSTOMER_STANDARD — the
    same current-binding rule as revision 027's partial unique index
    (ACTIVE or SUSPENDED; a REVOKED code keeps its binding for audit only
    and does not count as a current binding). Otherwise INTERNAL.
    """
    row = conn.execute(
        """
        SELECT 1 FROM activation_code_activations acca
        JOIN activation_codes acaba ON acaba.id = acca.code_id
        WHERE acca.user_id = %s AND acaba.status IN ('ACTIVE', 'SUSPENDED')
        LIMIT 1
        """,
        (user_id,),
    ).fetchone()
    return "CUSTOMER_STANDARD" if row is not None else "INTERNAL"


def _deny_admin_self_service(
    conn: psycopg.Connection,
    *,
    actor_user_id: str,
    target_user_id: str,
    attempted_action: str,
    reason: str,
    request_id: str,
) -> Never:
    conn.execute(
        """
        INSERT INTO audit_logs
            (id, actor_user_id, action, entity_type, entity_id, metadata_json)
        VALUES (%s, %s, 'security.admin_self_service_denied', 'user', %s, %s)
        """,
        (
            str(uuid.uuid4()),
            actor_user_id,
            target_user_id,
            json.dumps(
                {
                    "attempted_action": attempted_action,
                    "reason": reason,
                    "request_id": request_id,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        ),
    )
    raise DeferredHTTPWriteError(
        403,
        "ADMIN_SELF_SERVICE_FORBIDDEN",
        "An administrator cannot change their own balance or pricing.",
    )


class CustomerUnitPriceUpdateRequest(AdminWriteRequest):
    model_config = ConfigDict(extra="forbid")

    # ``null`` removes the override and returns the customer to the global
    # default. StrictInt prevents booleans and numeric strings from silently
    # becoming financial values.
    unit_price_fen: StrictInt | None


class CustomerUnitPriceResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str
    unit_price_fen: int
    custom_unit_price_fen: int | None
    default_unit_price_fen: int
    min_recharge_fen: int
    recharge_step_fen: int
    updated_at: str | None
    request_id: str | None = None


def _customer_unit_price_payload(
    conn: psycopg.Connection,
    *,
    user_id: str,
    request_id: str | None = None,
) -> dict[str, object]:
    row = conn.execute(
        """
        SELECT u.id,
               rs.internal_base_unit_price_fen,
               rs.min_recharge_fen,
               rs.recharge_step_fen,
               cup.unit_price_fen,
               cup.updated_at
        FROM users u
        CROSS JOIN runtime_settings rs
        LEFT JOIN customer_unit_prices cup ON cup.user_id = u.id
        WHERE u.id = %s
          AND rs.id = 1
          AND EXISTS (
              SELECT 1
              FROM activation_code_activations aca
              JOIN activation_codes ac ON ac.id = aca.code_id
              WHERE aca.user_id = u.id
                AND ac.status IN ('ACTIVE', 'SUSPENDED')
          )
        """,
        (user_id,),
    ).fetchone()
    if row is None:
        raise _http(404, "CUSTOMER_NOT_FOUND", "Activated customer not found.")

    default_unit_price_fen = int(row[1])
    custom_unit_price_fen = None if row[4] is None else int(row[4])
    billing = {
        "internal_base_unit_price_fen": default_unit_price_fen,
        "charged_unit_price_fen": default_unit_price_fen,
        "min_recharge_fen": int(row[2]),
        "recharge_step_fen": int(row[3]),
    }
    if custom_unit_price_fen is not None:
        billing = apply_customer_unit_price(
            billing,
            unit_price_fen=custom_unit_price_fen,
        )
    return {
        "user_id": str(row[0]),
        "unit_price_fen": billing["charged_unit_price_fen"],
        "custom_unit_price_fen": custom_unit_price_fen,
        "default_unit_price_fen": default_unit_price_fen,
        "min_recharge_fen": billing["min_recharge_fen"],
        "recharge_step_fen": billing["recharge_step_fen"],
        "updated_at": None if row[5] is None else str(row[5]),
        "request_id": request_id,
    }


@router.get(
    "/customers/{user_id}/unit-price",
    response_model=CustomerUnitPriceResponse,
)
def read_customer_unit_price(
    user_id: str,
    actor: AdminReader,
) -> dict[str, object]:
    del actor
    try:
        with pg_transaction() as conn:
            return _customer_unit_price_payload(conn, user_id=user_id)
    except (RuntimeError, MissingDatabaseConfigError) as exc:
        raise _http(
            503,
            "CUSTOMER_PRICING_UNAVAILABLE",
            "Customer pricing requires the PostgreSQL runtime.",
        ) from exc


@router.put(
    "/customers/{user_id}/unit-price",
    response_model=CustomerUnitPriceResponse,
)
def update_customer_unit_price(
    user_id: str,
    body: CustomerUnitPriceUpdateRequest,
    request: Request,
    response: Response,
    actor: AdminWriter,
) -> dict[str, object]:
    """Set or clear a customer's sale price without applying a cost floor."""

    def business(conn: psycopg.Connection, request_id: str) -> dict[str, object]:
        if user_id == actor.user_id:
            _deny_admin_self_service(
                conn,
                actor_user_id=actor.user_id,
                target_user_id=user_id,
                attempted_action="customer_unit_price.update",
                reason=body.reason.strip(),
                request_id=request_id,
            )
        current = _customer_unit_price_payload(conn, user_id=user_id)
        unit_price_fen = body.unit_price_fen
        if unit_price_fen is not None and not 1 <= unit_price_fen <= 2_147_483_647:
            raise _http(
                400,
                "CUSTOMER_PRICE_INVALID",
                "unit_price_fen must be between 1 and 2147483647.",
            )

        if unit_price_fen is None:
            conn.execute("DELETE FROM customer_unit_prices WHERE user_id = %s", (user_id,))
            action = "customer_unit_price.reset"
        else:
            conn.execute(
                """
                INSERT INTO customer_unit_prices
                    (user_id, unit_price_fen, updated_by_user_id)
                VALUES (%s, %s, %s)
                ON CONFLICT (user_id) DO UPDATE
                SET unit_price_fen = EXCLUDED.unit_price_fen,
                    updated_by_user_id = EXCLUDED.updated_by_user_id,
                    updated_at = clock_timestamp()
                """,
                (user_id, unit_price_fen, actor.user_id),
            )
            action = "customer_unit_price.update"

        conn.execute(
            """
            INSERT INTO audit_logs
                (id, actor_user_id, action, entity_type, entity_id, metadata_json)
            VALUES (%s, %s, %s, 'customer_unit_price', %s, %s)
            """,
            (
                str(uuid.uuid4()),
                actor.user_id,
                action,
                user_id,
                json.dumps(
                    {
                        "old_unit_price_fen": current["custom_unit_price_fen"],
                        "new_unit_price_fen": unit_price_fen,
                        "reason": body.reason.strip(),
                        "request_id": request_id,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            ),
        )
        return _customer_unit_price_payload(
            conn,
            user_id=user_id,
            request_id=request_id,
        )

    return _write_with_idempotency(
        request,
        response,
        actor,
        body,
        business,
        success_status=200,
        unavailable_code="CUSTOMER_PRICING_UNAVAILABLE",
        unavailable_message="Customer pricing requires the PostgreSQL runtime.",
    )


# ---------------------------------------------------------------------------
# Adjustment creation (happy path + validations)
# ---------------------------------------------------------------------------


@router.post("/customers/{user_id}/adjustments", status_code=201)
def create_admin_adjustment(
    user_id: str,
    body: AdjustmentRequest,
    request: Request,
    response: Response,
    actor: AdminWriter,
) -> dict[str, object]:
    """Create an admin adjustment: PAID order + CHARGE + wallet + audit row."""

    def business(conn: psycopg.Connection, request_id: str) -> dict[str, object]:
        if user_id == actor.user_id:
            _deny_admin_self_service(
                conn,
                actor_user_id=actor.user_id,
                target_user_id=user_id,
                attempted_action="customer_adjustment.create",
                reason=body.reason.strip(),
                request_id=request_id,
            )
        # Validate credits (positive; the amount-overflow guard runs after the
        # unit price snapshot is loaded, because int4 overflow depends on it)
        if body.credits <= 0:
            raise _http(400, "ADJUSTMENT_VALIDATION_FAILED", "Credits must be positive.")

        # Validate the source document (来源单): frozen enum + non-blank ref —
        # the revision 039 CHECK constraints are the defense in depth, the
        # route answers the operator with a 400 before touching the ledger.
        source_document_type = body.source_document_type.strip()
        source_document_ref = body.source_document_ref.strip()
        if source_document_type not in SOURCE_DOCUMENT_TYPES:
            raise _http(
                400,
                "ADJUSTMENT_VALIDATION_FAILED",
                "source_document_type must be one of: " + ", ".join(SOURCE_DOCUMENT_TYPES),
            )
        if not source_document_ref:
            raise _http(
                400,
                "ADJUSTMENT_VALIDATION_FAILED",
                "source_document_ref must not be blank.",
            )

        # Check target user exists
        user_row = conn.execute("SELECT 1 FROM users WHERE id = %s", (user_id,)).fetchone()
        if not user_row:
            raise _http(404, "USER_NOT_FOUND", "Target user not found.")

        # Check wallet exists
        wallet_exists = conn.execute(
            "SELECT 1 FROM wallets WHERE user_id = %s", (user_id,)
        ).fetchone()
        if not wallet_exists:
            raise _http(404, "WALLET_NOT_FOUND", "Wallet not found for target user.")

        # Infer pricing scope from target user's activation status
        pricing_scope = _infer_pricing_scope(conn, user_id)

        # Get current billing snapshot
        snapshot = conn.execute(
            "SELECT internal_base_unit_price_fen, min_recharge_fen, recharge_step_fen "
            "FROM runtime_settings WHERE id = 1"
        ).fetchone()
        if not snapshot:
            raise _http(503, "BILLING_SNAPSHOT_UNAVAILABLE", "Billing snapshot not configured.")

        base_unit_price_fen = int(snapshot[0])
        billing = {
            "internal_base_unit_price_fen": base_unit_price_fen,
            "charged_unit_price_fen": base_unit_price_fen,
            "min_recharge_fen": int(snapshot[1]),
            "recharge_step_fen": int(snapshot[2]),
        }
        if pricing_scope == "CUSTOMER_STANDARD":
            custom_price = conn.execute(
                "SELECT unit_price_fen FROM customer_unit_prices WHERE user_id = %s",
                (user_id,),
            ).fetchone()
            if custom_price is not None:
                billing = apply_customer_unit_price(
                    billing,
                    unit_price_fen=int(custom_price[0]),
                )
        unit_price_fen = billing["charged_unit_price_fen"]
        min_recharge_fen = billing["min_recharge_fen"]
        recharge_step_fen = billing["recharge_step_fen"]

        # Calculate amount from credits × unit price (frozen snapshot).
        # FREE_GRANT（054）是未收款的免费发放：账面金额必须如实记 0，
        # 按面值计金额会在账务里虚构收入；价格快照照存，审计行留痕。
        credits = body.credits
        if source_document_type == "FREE_GRANT":
            amount_fen = 0
        else:
            amount_fen = credits * unit_price_fen

        # int4 ledger overflow guard: recharge_orders.amount_fen is integer,
        # so a credits count whose derived amount overflows 2^31-1 must be
        # refused before the INSERT (PostgreSQL would answer a raw 500).
        if amount_fen > 2147483647:
            raise _http(
                400,
                "ADJUSTMENT_VALIDATION_FAILED",
                "The credits amount would overflow the ledger integer range.",
            )

        # Note: the min/step recharge ladder only governs zpay orders
        # (revision 026 constraints); audited adjustment amounts are defined
        # by their source documents, so no min/step enforcement here.

        # Generate identifiers
        adjustment_id = str(uuid.uuid4())
        order_id = str(uuid.uuid4())

        # Timestamp from PostgreSQL transaction clock (SES-01)
        paid_at = _transaction_now_iso(conn)
        now = paid_at

        # Insert recharge order (PAID, no third-party trade for admin_adjustment)
        conn.execute(
            """
            INSERT INTO recharge_orders
            (id, user_id, merchant_order_no, provider, status, pricing_scope,
             base_unit_price_fen_snapshot, charged_unit_price_fen_snapshot,
             min_recharge_fen_snapshot, recharge_step_fen_snapshot,
             amount_fen, credits, paid_at)
            VALUES (%s, %s, %s, 'admin_adjustment', 'PAID', %s,
                    %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                order_id,
                user_id,
                f"ADJ-{adjustment_id}",  # Local trade number format
                pricing_scope,
                base_unit_price_fen,
                unit_price_fen,
                min_recharge_fen,
                recharge_step_fen,
                amount_fen,
                credits,
                paid_at,
            ),
        )

        # Insert wallet CHARGE ledger row (task_id=NULL, billing_round=NULL for manual)
        charge_id = f"admin_adjustment:charge:{order_id}"
        conn.execute(
            """
            INSERT INTO wallet_transactions
            (id, user_id, type, available_delta, reserved_delta, recharge_order_id,
             task_id, billing_round, idempotency_key)
            VALUES (%s, %s, 'CHARGE', %s, 0, %s, NULL, NULL, %s)
            """,
            (charge_id, user_id, credits, order_id, charge_id),
        )

        # Atomic wallet credit increment — RETURNING the post-update balance so
        # the response never reports a stale pre-read plus credits (a concurrent
        # charge/settle on the same wallet would otherwise be invisible here).
        # The WHERE bound keeps the post-increment balance inside the int4
        # column range: PostgreSQL would otherwise raise NumericValueOutOfRange
        # (a raw 500 on a financial endpoint — the PR #54 connector review P2).
        # credits is a validated positive int, so 2147483647 - credits never
        # underflows in Python; a huge credits simply makes the bound negative
        # and every non-negative balance fails it, refusing the write.
        updated = conn.execute(
            "UPDATE wallets SET available_credits = available_credits + %s "
            "WHERE user_id = %s AND available_credits <= 2147483647 - %s "
            "RETURNING available_credits",
            (credits, user_id, credits),
        ).fetchone()
        if updated is None:
            # Distinguish the two refusal shapes: a wallet that vanished between
            # the existence check and the increment (out-of-band maintenance)
            # versus a balance that would overflow the int4 column.
            wallet_exists = conn.execute(
                "SELECT 1 FROM wallets WHERE user_id = %s", (user_id,)
            ).fetchone()
            if not wallet_exists:
                raise _http(404, "WALLET_NOT_FOUND", "Wallet not found for target user.")
            raise _http(
                400,
                "ADJUSTMENT_VALIDATION_FAILED",
                "The credits amount would overflow the wallet balance integer range.",
            )
        balance_after = int(updated[0])

        # Insert audit row (append-only, names the real admin)
        conn.execute(
            """
            INSERT INTO admin_adjustments
            (id, recharge_order_id, target_user_id, admin_user_id,
             source_document_type, source_document_ref, reason, request_id, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                adjustment_id,
                order_id,
                user_id,
                actor.user_id,  # Real acting administrator (§15)
                source_document_type,
                source_document_ref,
                body.reason.strip(),
                request_id,
                now,
            ),
        )

        # Log (no sensitive data in logs)
        from app.admin_activation_routes import logger

        logger.info(
            "admin adjustment created: adjustment=%s order=%s user=%s "
            "credits=%d actor=%s request=%s",
            adjustment_id,
            order_id,
            user_id,
            credits,
            actor.user_id,
            request_id,
        )

        # Return success response
        return {
            "adjustment_id": adjustment_id,
            "order_id": order_id,
            "credits": str(credits),
            "amount_fen": str(amount_fen),
            "pricing_scope": pricing_scope,
            "wallet_balance_after": balance_after,
            "source_document_type": source_document_type,
            "source_document_ref": source_document_ref,
            "request_id": request_id,
        }

    return _write_with_idempotency(request, response, actor, body, business, success_status=201)


# ---------------------------------------------------------------------------
# Read-only listing endpoint (audit trail)
# ---------------------------------------------------------------------------


DEFAULT_LIST_LIMIT = 100
MAX_LIST_LIMIT = 200


@router.get("/customers/{user_id}/adjustments")
def list_admin_adjustments(
    user_id: str,
    actor: AdminReader,
    limit: int = DEFAULT_LIST_LIMIT,
    offset: int = 0,
) -> dict[str, object]:
    """List all adjustments for a target user (audit trail for operators and auditors)."""
    bounded_limit = max(0, min(limit, MAX_LIST_LIMIT))
    bounded_offset = max(0, offset)

    try:
        with pg_transaction() as conn:
            rows = conn.execute(
                """
                SELECT aa.id, aa.recharge_order_id, aa.admin_user_id,
                       aa.source_document_type, aa.source_document_ref,
                       aa.reason, aa.request_id, aa.created_at,
                       ro.amount_fen, ro.credits, ro.pricing_scope, ro.status
                FROM admin_adjustments aa
                JOIN recharge_orders ro ON ro.id = aa.recharge_order_id
                WHERE aa.target_user_id = %s
                ORDER BY aa.created_at, aa.id
                LIMIT %s OFFSET %s
                """,
                (user_id, bounded_limit, bounded_offset),
            ).fetchall()
            total_row = conn.execute(
                "SELECT COUNT(*) FROM admin_adjustments WHERE target_user_id = %s",
                (user_id,),
            ).fetchone()
    except (RuntimeError, MissingDatabaseConfigError) as exc:
        raise _http(
            503,
            "ADJUSTMENT_SERVICE_UNAVAILABLE",
            "Admin adjustments require the PostgreSQL runtime.",
        ) from exc

    items = [
        {
            "adjustment_id": str(row[0]),
            "order_id": str(row[1]),
            "admin_user_id": str(row[2]),
            "source_document_type": str(row[3]),
            "source_document_ref": str(row[4]),
            "reason": str(row[5]),
            "request_id": str(row[6]),
            "created_at": str(row[7]) if row[7] is not None else "",
            "amount_fen": int(row[8]),
            "credits": int(row[9]),
            "pricing_scope": str(row[10]),
            "status": str(row[11]),
        }
        for row in rows
    ]

    total = int(total_row[0]) if total_row is not None else 0
    return {"items": items, "total": total, "limit": bounded_limit, "offset": bounded_offset}


DEFAULT_CUSTOMER_PAGE_SIZE = 20
MAX_CUSTOMER_PAGE_SIZE = 100


@router.get("/customers")
def list_customers(
    actor: AdminReader,
    limit: int = DEFAULT_CUSTOMER_PAGE_SIZE,
    offset: int = 0,
    username: str = "",
) -> dict[str, object]:
    """Every activated customer for operators and auditors (ADM-02 read path).

    A customer is the activation fact (one code, one user): the list carries
    display metadata only — masked code, username, activation time and the
    code status. The identity fields live on users / activation_codes; the
    data model has no customer email, so the T33 contract uses username.

    A5（2026-09-02 评估）: the page/page_size + ``{customers,…}`` shape is
    retired for the management-wide ``limit/offset`` + ``{items,total,…}``
    envelope, so every admin list paginates the same way.
    """
    bounded_limit = max(1, min(limit, MAX_CUSTOMER_PAGE_SIZE))
    bounded_offset = max(0, offset)

    clauses: list[str] = []
    params: list[object] = []
    if username.strip():
        clauses.append("u.username ILIKE %s")
        # Escape LIKE wildcards so a username containing % or _ is matched
        # literally (PostgreSQL LIKE treats backslash as the default escape).
        literal = username.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        params.append(f"%{literal}%")

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    try:
        with pg_transaction() as conn:
            rows = conn.execute(
                "SELECT aca.user_id, u.username, aca.activated_at, "
                "ac.id, ac.masked_code, ac.status, "
                "COALESCE(usage.generation_total, 0), "
                "COALESCE(usage.generation_succeeded, 0), "
                "COALESCE(usage.generation_failed, 0), "
                "COALESCE(usage.generation_in_progress, 0), "
                "COALESCE(usage.generation_attention, 0), "
                "COALESCE(spend.credits_spent, 0) "
                "FROM activation_code_activations aca "
                "JOIN users u ON u.id = aca.user_id "
                "JOIN activation_codes ac ON ac.id = aca.code_id "
                "LEFT JOIN ("
                "  SELECT gb.created_by_user_id AS user_id, "
                "    COUNT(*) AS generation_total, "
                "    COUNT(*) FILTER (WHERE gt.status = 'SUCCEEDED' "
                "      AND gt.archive_status = 'ARCHIVED') AS generation_succeeded, "
                "    COUNT(*) FILTER (WHERE gt.status IN ('FAILED', 'CANCELLED')) "
                "      AS generation_failed, "
                "    COUNT(*) FILTER (WHERE gt.status = 'SUBMISSION_UNCERTAIN' "
                "      OR gt.archive_status = 'ARCHIVE_FAILED' "
                "      OR gt.quality_status IN ("
                "        'AUDIO_QUALITY_FAILED', 'VISUAL_QUALITY_FAILED'"
                "      )) AS generation_attention, "
                "    COUNT(*) FILTER (WHERE NOT ("
                "      gt.status = 'SUCCEEDED' AND gt.archive_status = 'ARCHIVED'"
                "    ) AND gt.status NOT IN ('FAILED', 'CANCELLED', 'SUBMISSION_UNCERTAIN') "
                "      AND gt.archive_status != 'ARCHIVE_FAILED' "
                "      AND gt.quality_status NOT IN ("
                "        'AUDIO_QUALITY_FAILED', 'VISUAL_QUALITY_FAILED'"
                "      )) "
                "      AS generation_in_progress "
                "  FROM generation_batches gb "
                "  JOIN generation_tasks gt ON gt.batch_id = gb.id "
                "  GROUP BY gb.created_by_user_id"
                ") usage ON usage.user_id = aca.user_id "
                "LEFT JOIN ("
                "  SELECT user_id, COUNT(*) AS credits_spent "
                "  FROM wallet_transactions WHERE type = 'SETTLE' GROUP BY user_id"
                ") spend ON spend.user_id = aca.user_id "
                f"{where} "
                "ORDER BY aca.activated_at, aca.id "
                "LIMIT %s OFFSET %s",
                (*params, bounded_limit, bounded_offset),
            ).fetchall()
            total_row = conn.execute(
                "SELECT COUNT(*) FROM activation_code_activations aca "
                "JOIN users u ON u.id = aca.user_id "
                "JOIN activation_codes ac ON ac.id = aca.code_id "
                f"{where}",
                params,
            ).fetchone()
    except (RuntimeError, MissingDatabaseConfigError) as exc:
        raise _http(
            503,
            "CUSTOMER_SERVICE_UNAVAILABLE",
            "Customer management requires the PostgreSQL runtime.",
        ) from exc

    customers = [
        {
            "user_id": str(row[0]),
            "username": str(row[1]),
            "created_at": str(row[2]) if row[2] is not None else "",
            "activation_code": str(row[4]),
            "status": str(row[5]),
            "generation_total": int(row[6]),
            "generation_succeeded": int(row[7]),
            "generation_failed": int(row[8]),
            "generation_in_progress": int(row[9]),
            "generation_attention": int(row[10]),
            "credits_spent": int(row[11]),
        }
        for row in rows
    ]
    total = int(total_row[0]) if total_row is not None else 0
    return {
        "items": customers,
        "total": total,
        "limit": bounded_limit,
        "offset": bounded_offset,
    }


@router.get("/customers.csv")
def export_customers_csv(
    actor: AdminWriter,
    status: str | None = None,
    username: str = "",
    limit: int = 5000,
) -> HttpResponse:
    """Export the customer list as CSV (C6) — audited + rate limited (A2).

    Replaces the console's client-side "current page only" export: the whole
    (filtered) list leaves through one audited dump with the same columns the
    operator saw in the table.
    """
    import csv as csv_mod
    import hashlib as hashlib_mod
    import io as io_mod

    if _sqlite_lane():
        raise _http(
            503,
            "CUSTOMER_SERVICE_UNAVAILABLE",
            "Customer management requires the PostgreSQL runtime.",
        )

    try:
        with pg_transaction() as conn:
            decision = consume_rate_limit(
                conn,
                dimension=DIMENSION_CONTROL_EXPORT_ACCOUNT,
                identifier=hashlib_mod.sha256(actor.user_id.encode("utf-8")).hexdigest(),
                limit=control_export_account_limit(),
                window_seconds=rate_limit_window_seconds(),
            )
            if not decision.allowed:
                raise _http(
                    429,
                    "CONTROL_EXPORT_RATE_LIMITED",
                    "Too many ledger exports; retry after the cooldown.",
                )
            clauses: list[str] = []
            params: list[object] = []
            if username.strip():
                literal = (
                    username.strip().replace("\\", "\\\\").replace("%", "\%").replace("_", "\_")
                )
                clauses.append("u.username ILIKE %s")
                params.append(f"%{literal}%")
            # PR #85 review P2: the UI dropdown sends lowercase status values
            # while the database enum is uppercase — normalize server-side so
            # every caller (not just this console) matches real rows instead
            # of exporting a header-only CSV.
            normalized_status = status.strip().upper() if status else ""
            if normalized_status:
                clauses.append("ac.status = %s")
                params.append(normalized_status)
            where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
            rows = conn.execute(
                "SELECT u.username, ac.masked_code, aca.activated_at, ac.status "
                "FROM activation_code_activations aca "
                "JOIN users u ON u.id = aca.user_id "
                "JOIN activation_codes ac ON ac.id = aca.code_id "
                f"{where} ORDER BY aca.activated_at, aca.id LIMIT %s",
                (*params, max(1, min(limit, 5000))),
            ).fetchall()
            write_audit(
                BusinessConnection.postgres(conn),
                actor=CurrentUser(
                    id=actor.user_id,
                    username=actor.username,
                    display_name=actor.display_name,
                    role=cast(Role, actor.role),
                ),
                action="control.export",
                entity_type="control_ledger",
                entity_id="customers",
                metadata={
                    "filters": {"status": normalized_status, "username": username},
                    "limit": limit,
                },
            )
    except (RuntimeError, MissingDatabaseConfigError) as exc:
        raise _http(
            503,
            "CUSTOMER_SERVICE_UNAVAILABLE",
            "Customer management requires the PostgreSQL runtime.",
        ) from exc

    buffer = io_mod.StringIO()
    writer = csv_mod.writer(buffer)
    writer.writerow(["username", "masked_code", "activated_at", "status"])
    for row in rows:
        writer.writerow([str(value) for value in row])
    payload = buffer.getvalue().encode("utf-8")
    return HttpResponse(
        content=payload,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="customers.csv"'},
    )
