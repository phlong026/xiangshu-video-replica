from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from typing import Annotated, Literal, cast
from uuid import uuid4

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict, StrictInt

from app.auth import AuthenticatedUser, Database
from app.customer_fence import (
    BusinessDbDep,
    CustomerSessionSnapshot,
    customer_session_snapshot,
    fenced_pg_transaction,
)
from app.customer_idempotency import (
    EnvelopeRecord,
    IdempotencyKeyError,
    complete_envelope,
    customer_aead_key,
    envelope_aad,
    highest_customer_aead_key,
    idempotency_key_digest,
    insert_envelope,
    load_envelope,
    open_response,
    recovery_window_seconds,
    request_hash,
    seal_response,
)
from app.db_portable import BusinessConnection
from app.ops_metrics import set_current_trace_fields
from app.security_rate_limit import _server_now, client_ip_from_request
from app.settings import SettingsRepository, effective_customer_billing_settings
from app.wallet_routes import WalletResponse, WalletTransactionPage, WalletTransactionResponse
from app.zpay import (
    ZPayDeploymentConfig,
    ZPayMerchantConfig,
    ZPayPaymentCodeClient,
    ZPayPaymentCodeError,
    build_zpay_payment_form,
    deployment_config_from_environment,
    generate_merchant_order_no,
    merchant_config_from_settings,
)
from app.zpay_payments import read_recharge_order, serialize_recharge_order

router = APIRouter(prefix="/api", tags=["recharge"])
MAX_ORDER_NUMBER_ATTEMPTS = 3
IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"
REPLAY_HEADER = "X-Idempotent-Replay"
RECHARGE_OPERATION = "recharge:create"

# ``recharge_orders.amount_fen`` is an int4 column (migration 022); a value
# beyond this bound would surface as a PostgreSQL IntegerFieldOverflow 500
# instead of a 422 - the same class of gap PR #54 closed for admin
# adjustments (wallet int4 overflow). ``credits`` is bounded by the same
# constant because ``credits = amount_fen // charged_unit_price_fen``.
INT4_MAX_FEN = 2_147_483_647


class CreateRechargeOrderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    amount_fen: StrictInt


class RechargeOrderResponse(BaseModel):
    order_no: str
    status: Literal["PENDING"]
    amount_fen: int
    credits: int
    gateway_url: str
    method: Literal["POST"]
    form_fields: dict[str, str]


class RechargeOrderStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order_no: str
    status: Literal["PENDING", "PAID", "FAILED", "CLOSED"]
    amount_fen: int
    credits: int
    channel: str
    created_at: str
    paid_at: str | None


class RechargeOrderPage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[RechargeOrderStatusResponse]
    total: int
    limit: int
    offset: int


class CustomerPaymentCodeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order_no: str
    amount_fen: int
    credits: int
    qr_image_url: str
    payment_url: str


class CustomerProfileResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str
    username: str
    display_name: str
    joined_at: str
    activation_code_masked: str | None
    activation_status: str | None
    activated_at: str | None
    device_slots_used: int
    device_slots_total: int


class UpdateCustomerProfileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str


def get_zpay_payment_code_client() -> ZPayPaymentCodeClient:
    return ZPayPaymentCodeClient()


PaymentCodeClientDep = Annotated[
    ZPayPaymentCodeClient,
    Depends(get_zpay_payment_code_client),
]


# ---------------------------------------------------------------------------
# Shared recharge-order creation core (T22 review: the customer route and the
# internal route previously duplicated ~99 lines and the copy drifted - the
# drift broke the collision retry on PostgreSQL twice over). Both routes now
# share the staged helpers below and keep the transaction/retry shape
# identical: the retry loop sits OUTSIDE ``db.write()`` so every attempt runs
# in a fresh fenced transaction (a failed INSERT aborts the PG transaction,
# an in-transaction retry would hit InFailedSqlTransaction).
# ---------------------------------------------------------------------------


def _stage_recharge_preconditions(
    conn: BusinessConnection,
    *,
    amount_fen: int,
    customer_user_id: str | None = None,
) -> tuple[dict[str, int], ZPayMerchantConfig, ZPayDeploymentConfig]:
    """Billing settings + amount validation + ZPay configuration, shared."""
    settings_repo = SettingsRepository(conn)
    billing = (
        settings_repo.read_customer_billing_settings(user_id=customer_user_id)
        if customer_user_id is not None
        else settings_repo.read_billing_settings()
    )
    if customer_user_id is not None:
        try:
            billing = effective_customer_billing_settings(
                billing,
                user_id=customer_user_id,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "ACCEPTANCE_PAYMENT_CONFIGURATION_INVALID",
                    "message": "The controlled payment rehearsal is not configured safely.",
                },
            ) from exc
    validate_recharge_amount(amount_fen, billing)
    try:
        merchant = merchant_config_from_settings(settings_repo.load_zpay_config())
        deployment = deployment_config_from_environment()
    except ValueError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "ZPAY_CONFIGURATION_INVALID", "message": str(exc)},
        ) from exc
    return billing, merchant, deployment


def _insert_recharge_order(
    conn: BusinessConnection,
    *,
    user_id: str,
    amount_fen: int,
    pricing_scope: Literal["INTERNAL", "CUSTOMER_STANDARD"],
    merchant_order_no: str,
    billing: dict[str, int],
    merchant: ZPayMerchantConfig,
    deployment: ZPayDeploymentConfig,
) -> RechargeOrderResponse:
    """Insert one PENDING recharge order and build its payment form."""
    charged_unit_price_fen = billing["charged_unit_price_fen"]
    credits = amount_fen // charged_unit_price_fen
    form_fields = build_zpay_payment_form(
        merchant_order_no=merchant_order_no,
        amount_fen=amount_fen,
        credits=credits,
        merchant=merchant,
        deployment=deployment,
    )
    with conn:
        conn.execute(
            "INSERT INTO recharge_orders (\n"
            "    id, user_id, merchant_order_no, provider, provider_trade_no,\n"
            "    channel, status, pricing_scope,\n"
            "    base_unit_price_fen_snapshot, charged_unit_price_fen_snapshot,\n"
            "    min_recharge_fen_snapshot, recharge_step_fen_snapshot,\n"
            "    amount_fen, credits\n"
            ") VALUES (%s, %s, %s, 'zpay', NULL, %s, 'PENDING', %s, "
            "%s, %s, %s, %s, %s, %s)\n",
            (
                str(uuid4()),
                user_id,
                merchant_order_no,
                merchant.channel,
                pricing_scope,
                billing["internal_base_unit_price_fen"],
                charged_unit_price_fen,
                billing["min_recharge_fen"],
                billing["recharge_step_fen"],
                amount_fen,
                credits,
            ),
        )
    set_current_trace_fields(user_id=user_id, order_id=merchant_order_no)
    return RechargeOrderResponse(
        order_no=merchant_order_no,
        status="PENDING",
        amount_fen=amount_fen,
        credits=credits,
        gateway_url=deployment.gateway_url,
        method="POST",
        form_fields=form_fields,
    )


def _retryable_merchant_order_collision(exc: Exception) -> bool:
    """True when the integrity failure is the retryable order-number draw.

    Matches both dialect messages: SQLite spells it
    ``UNIQUE constraint failed: recharge_orders.merchant_order_no`` while
    PostgreSQL reports the constraint name
    (``recharge_orders_merchant_order_key``) - the dotted form only exists on
    SQLite, so the narrower match silently broke the PG retry (review P1).
    """
    return "merchant_order_no" in str(exc)


@router.post(
    "/recharge-orders",
    response_model=RechargeOrderResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_recharge_order(
    payload: CreateRechargeOrderRequest,
    db: BusinessDbDep,
) -> RechargeOrderResponse:
    for _ in range(MAX_ORDER_NUMBER_ATTEMPTS):
        merchant_order_no = generate_merchant_order_no()
        try:
            with db.write() as (conn, user):
                billing, merchant, deployment = _stage_recharge_preconditions(
                    conn,
                    amount_fen=payload.amount_fen,
                )
                return _insert_recharge_order(
                    conn,
                    user_id=user.id,
                    amount_fen=payload.amount_fen,
                    pricing_scope="INTERNAL",
                    merchant_order_no=merchant_order_no,
                    billing=billing,
                    merchant=merchant,
                    deployment=deployment,
                )
        except (sqlite3.IntegrityError, psycopg.errors.UniqueViolation) as exc:
            # The merchant-order-number collision is a retryable random draw; any
            # other integrity failure is a real bug and must surface.
            if _retryable_merchant_order_collision(exc):
                continue
            raise

    raise HTTPException(
        status_code=503,
        detail={"code": "ORDER_NUMBER_UNAVAILABLE", "message": "Unable to allocate order number."},
    )


# ============================================================================
# T22 / BILL-01: Customer top-up route (session-authenticated recharge)
# ============================================================================


@router.post(
    "/customer/recharge-orders",
    response_model=RechargeOrderResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_customer_recharge_order(
    payload: CreateRechargeOrderRequest,
    request: Request,
    response: Response,
    db: BusinessDbDep,
) -> RechargeOrderResponse:
    """T22: Customer can reuse ZPay to top-up the same wallet under their session.

    Key invariant guarantees (BILL-01):
    - Recharge does NOT change main code, device slots, session or user concurrency
    - Idempotency-Key envelope (T14 engine): a retry with the same key replays
      the sealed response without creating a second order; a different request
      under a spent key is a 409
    - Credits enter the same customer wallet (not P0 internal wallet)
    """
    idempotency_key = request.headers.get(IDEMPOTENCY_KEY_HEADER, "").strip()
    if not idempotency_key:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "IDEMPOTENCY_KEY_REQUIRED",
                "message": "An Idempotency-Key header is required.",
            },
        )
    key_digest = idempotency_key_digest(idempotency_key)
    req_hash = request_hash({"amount_fen": str(payload.amount_fen)})
    try:
        aead_key_version, aead_key = highest_customer_aead_key()
    except IdempotencyKeyError:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "IDEMPOTENCY_KEYS_UNAVAILABLE",
                "message": "Idempotency keys are not configured; recharge is refused.",
            },
        ) from None

    for _ in range(MAX_ORDER_NUMBER_ATTEMPTS):
        merchant_order_no = generate_merchant_order_no()
        try:
            with db.write() as (conn, user):
                scope = f"recharge:{user.id}"
                record = load_envelope(
                    _pg_conn(conn),
                    operation=RECHARGE_OPERATION,
                    scope=scope,
                    key_digest=key_digest,
                )
                envelope_id: str | None = None
                if record is None:
                    envelope_id = insert_envelope(
                        _pg_conn(conn),
                        operation=RECHARGE_OPERATION,
                        scope=scope,
                        key_digest=key_digest,
                        request_hash=req_hash,
                    )
                    if envelope_id is None:
                        # Concurrent same-key writer won the placeholder insert;
                        # load the committed envelope and treat it as a replay.
                        record = load_envelope(
                            _pg_conn(conn),
                            operation=RECHARGE_OPERATION,
                            scope=scope,
                            key_digest=key_digest,
                        )
                if record is not None:
                    _enforce_envelope_conflicts(record, req_hash=req_hash, conn=conn)
                    replayed = _open_recharge_envelope(record, scope=scope, key_digest=key_digest)
                    replayed_order = RechargeOrderResponse.model_validate(replayed)
                    set_current_trace_fields(user_id=user.id, order_id=replayed_order.order_no)
                    response.headers[REPLAY_HEADER] = "true"
                    return replayed_order

                billing, merchant, deployment = _stage_recharge_preconditions(
                    conn,
                    amount_fen=payload.amount_fen,
                    customer_user_id=user.id,
                )
                order = _insert_recharge_order(
                    conn,
                    user_id=user.id,
                    amount_fen=payload.amount_fen,
                    pricing_scope="CUSTOMER_STANDARD",
                    merchant_order_no=merchant_order_no,
                    billing=billing,
                    merchant=merchant,
                    deployment=deployment,
                )
                assert envelope_id is not None
                recovery_expires_at = (
                    (_server_now(_pg_conn(conn)) + timedelta(seconds=recovery_window_seconds()))
                    .replace(microsecond=0)
                    .isoformat()
                )
                sealed_ciphertext = seal_response(
                    order.model_dump(),
                    key=aead_key,
                    aad=envelope_aad(RECHARGE_OPERATION, scope, key_digest),
                )
                complete_envelope(
                    _pg_conn(conn),
                    envelope_id,
                    ciphertext=sealed_ciphertext,
                    key_version=aead_key_version,
                    recovery_expires_at=recovery_expires_at,
                )
                return order
        except (sqlite3.IntegrityError, psycopg.errors.UniqueViolation) as exc:
            if _retryable_merchant_order_collision(exc):
                continue
            raise

    raise HTTPException(
        status_code=503,
        detail={"code": "ORDER_NUMBER_UNAVAILABLE", "message": "Unable to allocate order number."},
    )


def _pg_conn(conn: BusinessConnection) -> psycopg.Connection:
    """Narrow the BusinessConnection backend to the PostgreSQL connection the
    idempotency engine and the server clock operate on (customer lane only)."""
    return cast(psycopg.Connection, conn.raw)


def _enforce_envelope_conflicts(
    record: EnvelopeRecord,
    *,
    req_hash: str,
    conn: BusinessConnection,
) -> None:
    """The T14 replay contract: hash conflict / unrecoverable / expired -> 409."""
    if record.request_hash != req_hash:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "IDEMPOTENCY_CONFLICT",
                "message": "This idempotency key was already used for a different request.",
            },
        )
    if record.ciphertext is None or record.key_version is None:
        # Purged or never completed: the key is spent and the response is no
        # longer recoverable (T14 / ACT-07 contract).
        raise HTTPException(
            status_code=409,
            detail={
                "code": "IDEMPOTENCY_CONFLICT",
                "message": "This idempotency key is no longer recoverable.",
            },
        )
    recovery_expires_at = record.recovery_expires_at
    if recovery_expires_at is not None and (
        datetime.fromisoformat(str(recovery_expires_at)) <= _server_now(_pg_conn(conn))
    ):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "IDEMPOTENCY_CONFLICT",
                "message": "The recovery window for this key has expired.",
            },
        )


def _open_recharge_envelope(
    record: EnvelopeRecord,
    *,
    scope: str,
    key_digest: str,
) -> dict[str, object]:
    """Unseal a replayable response; an unopenable seal is unrecoverable."""
    assert record.ciphertext is not None and record.key_version is not None
    try:
        return open_response(
            record.ciphertext,
            key=customer_aead_key(record.key_version),
            aad=envelope_aad(RECHARGE_OPERATION, scope, key_digest),
        )
    except IdempotencyKeyError:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "IDEMPOTENCY_CONFLICT",
                "message": "This idempotency key is no longer recoverable.",
            },
        ) from None


@router.get(
    "/customer/recharge-orders/{order_no}",
    response_model=RechargeOrderStatusResponse,
)
def read_customer_recharge_order_status(
    order_no: str,
    request: Request,
) -> RechargeOrderStatusResponse:
    """Customer-lane order status: the session is re-verified inside the
    fenced read transaction and another user's order number is a 404 (no
    existence leak), mirroring the internal-lane route's ownership check."""
    snapshot = customer_session_snapshot(request)
    if snapshot is None:
        raise HTTPException(
            status_code=401,
            detail={
                "code": "SESSION_REQUIRED",
                "message": "A customer session token is required.",
            },
        )
    with fenced_pg_transaction(snapshot) as (conn, ctx):
        business_conn = BusinessConnection.postgres(conn)
        order = read_recharge_order(business_conn, merchant_order_no=order_no)
        if order is None or str(order["user_id"]) != ctx.user_id:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "RECHARGE_ORDER_NOT_FOUND",
                    "message": "Recharge order does not exist.",
                },
            )
        return RechargeOrderStatusResponse(**serialize_recharge_order(order))


@router.delete(
    "/customer/recharge-orders/{order_no}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def close_customer_recharge_order(order_no: str, request: Request) -> Response:
    """Close an unpaid order without erasing its accounting lineage.

    The UI calls this action "delete", while the database keeps the order as
    CLOSED so callbacks, support and reconciliation retain one authoritative
    record. Repeating the request is intentionally idempotent.
    """
    snapshot = _require_customer_snapshot(request)
    with fenced_pg_transaction(snapshot) as (conn, ctx):
        row = conn.execute(
            "SELECT id, status FROM recharge_orders "
            "WHERE merchant_order_no = %s AND user_id = %s FOR UPDATE",
            (order_no, ctx.user_id),
        ).fetchone()
        if row is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "RECHARGE_ORDER_NOT_FOUND",
                    "message": "Recharge order does not exist.",
                },
            )
        current_status = str(row[1])
        if current_status == "CLOSED":
            return Response(status_code=status.HTTP_204_NO_CONTENT)
        if current_status != "PENDING":
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "RECHARGE_ORDER_NOT_PENDING",
                    "message": "Only an unpaid recharge order can be closed.",
                },
            )
        conn.execute(
            "UPDATE recharge_orders SET status = 'CLOSED' WHERE id = %s",
            (str(row[0]),),
        )
        _insert_customer_audit(
            conn,
            user_id=ctx.user_id,
            action="customer.recharge_order.closed",
            entity_type="recharge_order",
            entity_id=order_no,
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/customer/recharge-orders/{order_no}/payment-code",
    response_model=CustomerPaymentCodeResponse,
)
def create_customer_payment_code(
    order_no: str,
    request: Request,
    payment_client: PaymentCodeClientDep,
) -> CustomerPaymentCodeResponse:
    """Return a display-ready QR image for one owned pending order.

    Merchant credentials and signed protocol fields stay server-side. The
    PostgreSQL connection is released before the external request so a slow
    payment provider cannot consume the shared database pool.
    """
    snapshot = customer_session_snapshot(request)
    if snapshot is None:
        raise HTTPException(
            status_code=401,
            detail={
                "code": "SESSION_REQUIRED",
                "message": "A customer session token is required.",
            },
        )
    with fenced_pg_transaction(snapshot) as (conn, ctx):
        business_conn = BusinessConnection.postgres(conn)
        order = read_recharge_order(business_conn, merchant_order_no=order_no)
        if order is None or str(order["user_id"]) != ctx.user_id:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "RECHARGE_ORDER_NOT_FOUND",
                    "message": "Recharge order does not exist.",
                },
            )
        if str(order["status"]) != "PENDING":
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "RECHARGE_ORDER_NOT_PENDING",
                    "message": "This recharge order is no longer pending.",
                },
            )
        try:
            merchant = merchant_config_from_settings(
                SettingsRepository(business_conn).load_zpay_config()
            )
            deployment = deployment_config_from_environment()
        except ValueError as exc:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "PAYMENT_CONFIGURATION_UNAVAILABLE",
                    "message": "支付服务暂不可用，请稍后重试。",
                },
            ) from exc
        amount_fen = int(order["amount_fen"])
        credits = int(order["credits"])

    try:
        payment_code = payment_client.create_payment_code(
            merchant=merchant,
            deployment=deployment,
            merchant_order_no=order_no,
            amount_fen=amount_fen,
            credits=credits,
            client_ip=client_ip_from_request(request),
        )
    except ZPayPaymentCodeError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={
                "code": "PAYMENT_CODE_UNAVAILABLE",
                "message": "支付二维码暂时无法生成，请稍后重试。",
            },
        ) from exc
    return CustomerPaymentCodeResponse(
        order_no=order_no,
        amount_fen=amount_fen,
        credits=credits,
        qr_image_url=payment_code.qr_image_url,
        payment_url=payment_code.payment_url,
    )


def _customer_profile(conn: psycopg.Connection, *, user_id: str) -> CustomerProfileResponse:
    row = conn.execute(
        """
        SELECT u.username, u.display_name, u.created_at,
               code.masked_code, code.status, code.activated_at,
               (
                   SELECT COUNT(*)
                   FROM customer_devices device
                   WHERE device.user_id = u.id AND device.status = 'BOUND'
               ) AS device_slots_used
        FROM users u
        LEFT JOIN LATERAL (
            SELECT masked_code, status, activated_at
            FROM activation_codes
            WHERE bound_user_id = u.id
            ORDER BY activated_at DESC NULLS LAST, id DESC
            LIMIT 1
        ) code ON TRUE
        WHERE u.id = %s
        """,
        (user_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "CUSTOMER_PROFILE_NOT_FOUND",
                "message": "Customer profile does not exist.",
            },
        )
    return CustomerProfileResponse(
        user_id=user_id,
        username=str(row[0]),
        display_name=str(row[1]),
        joined_at=str(row[2]),
        activation_code_masked=str(row[3]) if row[3] is not None else None,
        activation_status=str(row[4]) if row[4] is not None else None,
        activated_at=str(row[5]) if row[5] is not None else None,
        device_slots_used=int(row[6]),
        device_slots_total=2,
    )


def _insert_customer_audit(
    conn: psycopg.Connection,
    *,
    user_id: str,
    action: str,
    entity_type: str,
    entity_id: str,
    metadata: dict[str, object] | None = None,
) -> None:
    conn.execute(
        "INSERT INTO audit_logs "
        "(id, actor_user_id, action, entity_type, entity_id, metadata_json) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (
            str(uuid4()),
            user_id,
            action,
            entity_type,
            entity_id,
            json.dumps(metadata or {}, ensure_ascii=True, sort_keys=True),
        ),
    )


def _require_customer_snapshot(request: Request) -> CustomerSessionSnapshot:
    snapshot = customer_session_snapshot(request)
    if snapshot is None:
        raise HTTPException(
            status_code=401,
            detail={
                "code": "SESSION_REQUIRED",
                "message": "A customer session token is required.",
            },
        )
    return snapshot


@router.get("/customer/profile", response_model=CustomerProfileResponse)
def read_customer_profile(request: Request) -> CustomerProfileResponse:
    snapshot = _require_customer_snapshot(request)
    with fenced_pg_transaction(snapshot) as (conn, ctx):
        return _customer_profile(conn, user_id=ctx.user_id)


@router.patch("/customer/profile", response_model=CustomerProfileResponse)
def update_customer_profile(
    payload: UpdateCustomerProfileRequest,
    request: Request,
) -> CustomerProfileResponse:
    display_name = payload.display_name.strip()
    if not display_name or len(display_name) > 50:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "INVALID_DISPLAY_NAME",
                "message": "Display name must contain 1 to 50 characters.",
            },
        )
    snapshot = _require_customer_snapshot(request)
    with fenced_pg_transaction(snapshot) as (conn, ctx):
        updated = conn.execute(
            "UPDATE users SET display_name = %s WHERE id = %s AND role = 'customer'",
            (display_name, ctx.user_id),
        )
        if updated.rowcount != 1:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "CUSTOMER_PROFILE_NOT_FOUND",
                    "message": "Customer profile does not exist.",
                },
            )
        _insert_customer_audit(
            conn,
            user_id=ctx.user_id,
            action="customer.profile.updated",
            entity_type="user",
            entity_id=ctx.user_id,
            metadata={"display_name_length": len(display_name)},
        )
        return _customer_profile(conn, user_id=ctx.user_id)


@router.get("/customer/wallet", response_model=WalletResponse)
def read_customer_wallet(request: Request) -> WalletResponse:
    """Customer-lane wallet read: balance + billing under the fenced session.

    Mirrors the internal /api/wallet read but re-verifies the customer session
    inside the transaction (BILL-01: credits live in the same customer wallet
    the activation grant funded)."""
    snapshot = customer_session_snapshot(request)
    if snapshot is None:
        raise HTTPException(
            status_code=401,
            detail={
                "code": "SESSION_REQUIRED",
                "message": "A customer session token is required.",
            },
        )
    with fenced_pg_transaction(snapshot) as (conn, ctx):
        row = conn.execute(
            "SELECT available_credits, reserved_credits FROM wallets WHERE user_id = %s",
            (ctx.user_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "WALLET_NOT_FOUND", "message": "Wallet does not exist."},
            )
        billing = SettingsRepository(
            BusinessConnection.postgres(conn)
        ).read_customer_billing_settings(user_id=ctx.user_id)
        try:
            billing = effective_customer_billing_settings(billing, user_id=ctx.user_id)
        except ValueError as exc:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "ACCEPTANCE_PAYMENT_CONFIGURATION_INVALID",
                    "message": "The controlled payment rehearsal is not configured safely.",
                },
            ) from exc
        return WalletResponse(
            available_credits=int(row[0]),
            reserved_credits=int(row[1]),
            # Keep the legacy response field for desktop compatibility; on
            # the customer lane it represents the effective sale price.
            internal_unit_price_fen=billing["charged_unit_price_fen"],
            min_recharge_fen=billing["min_recharge_fen"],
            recharge_step_fen=billing["recharge_step_fen"],
        )


@router.get("/customer/wallet/transactions", response_model=WalletTransactionPage)
def list_customer_wallet_transactions(
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> WalletTransactionPage:
    snapshot = customer_session_snapshot(request)
    if snapshot is None:
        raise HTTPException(
            status_code=401,
            detail={
                "code": "SESSION_REQUIRED",
                "message": "A customer session token is required.",
            },
        )
    with fenced_pg_transaction(snapshot) as (conn, ctx):
        total_row = conn.execute(
            "SELECT COUNT(*) FROM wallet_transactions WHERE user_id = %s",
            (ctx.user_id,),
        ).fetchone()
        assert total_row is not None
        total = int(total_row[0])
        rows = conn.execute(
            """
            SELECT id, user_id, type, available_delta, reserved_delta,
                   recharge_order_id, task_id, billing_round, created_at
            FROM wallet_transactions
            WHERE user_id = %s
            ORDER BY created_at DESC, id DESC
            LIMIT %s OFFSET %s
            """,
            (ctx.user_id, limit, offset),
        ).fetchall()
        return WalletTransactionPage(
            items=[
                WalletTransactionResponse(
                    id=str(row[0]),
                    user_id=str(row[1]),
                    type=row[2],
                    available_delta=int(row[3]),
                    reserved_delta=int(row[4]),
                    recharge_order_id=str(row[5]) if row[5] is not None else None,
                    task_id=str(row[6]) if row[6] is not None else None,
                    billing_round=int(row[7]) if row[7] is not None else None,
                    created_at=str(row[8]),
                )
                for row in rows
            ],
            total=total,
            limit=limit,
            offset=offset,
        )


@router.get("/customer/recharge-orders", response_model=RechargeOrderPage)
def list_customer_recharge_orders(
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> RechargeOrderPage:
    """Customer-lane order list: only this session's orders, ownership-checked
    inside the fenced read transaction."""
    snapshot = customer_session_snapshot(request)
    if snapshot is None:
        raise HTTPException(
            status_code=401,
            detail={
                "code": "SESSION_REQUIRED",
                "message": "A customer session token is required.",
            },
        )
    with fenced_pg_transaction(snapshot) as (conn, ctx):
        # Codex P1 (PR #65): serialize_recharge_order reads named columns, so
        # the rows must come from a connection with the named-row factory
        # installed. BusinessConnection.postgres() sets it; a raw pooled
        # psycopg connection returns plain tuples and would 500 on a fresh
        # connection that no earlier request had already mutated.
        business_conn = BusinessConnection.postgres(conn)
        total_row = business_conn.execute(
            "SELECT COUNT(*) FROM recharge_orders WHERE user_id = %s",
            (ctx.user_id,),
        ).fetchone()
        assert total_row is not None
        total = int(total_row[0])
        rows = business_conn.execute(
            """
            SELECT id, user_id, merchant_order_no, provider, provider_trade_no,
                   channel, status, amount_fen, credits, notify_digest, created_at, paid_at
            FROM recharge_orders
            WHERE user_id = %s
            ORDER BY created_at DESC, id DESC
            LIMIT %s OFFSET %s
            """,
            (ctx.user_id, limit, offset),
        ).fetchall()
        return RechargeOrderPage(
            items=[
                RechargeOrderStatusResponse(**serialize_recharge_order(cast(sqlite3.Row, row)))
                for row in rows
            ],
            total=total,
            limit=limit,
            offset=offset,
        )


@router.get("/recharge-orders", response_model=RechargeOrderPage)
def list_recharge_orders(
    conn: Database,
    actor: AuthenticatedUser,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> RechargeOrderPage:
    total = int(
        conn.execute(
            "SELECT COUNT(*) FROM recharge_orders WHERE user_id = %s",
            (actor.id,),
        ).fetchone()[0]
    )
    rows = conn.execute(
        """
        SELECT
            id, user_id, merchant_order_no, provider, provider_trade_no, channel, status,
            amount_fen, credits, notify_digest, created_at, paid_at
        FROM recharge_orders
        WHERE user_id = %s
        ORDER BY created_at DESC, id DESC
        LIMIT %s OFFSET %s
        """,
        (actor.id, limit, offset),
    ).fetchall()
    return RechargeOrderPage(
        items=[RechargeOrderStatusResponse(**serialize_recharge_order(row)) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/recharge-orders/{order_no}", response_model=RechargeOrderStatusResponse)
def read_recharge_order_status(
    order_no: str,
    conn: Database,
    actor: AuthenticatedUser,
) -> RechargeOrderStatusResponse:
    order = read_recharge_order(conn, merchant_order_no=order_no)
    if order is None or str(order["user_id"]) != actor.id:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "RECHARGE_ORDER_NOT_FOUND",
                "message": "Recharge order does not exist.",
            },
        )
    return RechargeOrderStatusResponse(**serialize_recharge_order(order))


def validate_recharge_amount(amount_fen: int, billing: dict[str, int]) -> None:
    if (
        amount_fen < billing["min_recharge_fen"]
        or amount_fen % billing["recharge_step_fen"] != 0
        or amount_fen % billing["charged_unit_price_fen"] != 0
        or amount_fen > INT4_MAX_FEN
    ):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "INVALID_RECHARGE_AMOUNT",
                "message": "Recharge amount must meet the configured minimum and step.",
            },
        )
