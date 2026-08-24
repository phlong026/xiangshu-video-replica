from __future__ import annotations

import sqlite3
from typing import Literal
from uuid import uuid4

import psycopg
from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, StrictInt

from app.auth import AuthenticatedUser, Database
from app.customer_fence import BusinessDbDep
from app.settings import SettingsRepository
from app.zpay import (
    build_zpay_payment_form,
    deployment_config_from_environment,
    generate_merchant_order_no,
    merchant_config_from_settings,
)
from app.zpay_payments import read_recharge_order, serialize_recharge_order

router = APIRouter(prefix="/api", tags=["recharge"])
MAX_ORDER_NUMBER_ATTEMPTS = 3


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
                settings_repo = SettingsRepository(conn)
                billing = settings_repo.read_billing_settings()
                validate_recharge_amount(payload.amount_fen, billing)

                try:
                    merchant = merchant_config_from_settings(settings_repo.load_zpay_config())
                    deployment = deployment_config_from_environment()
                except ValueError as exc:
                    raise HTTPException(
                        status_code=503,
                        detail={"code": "ZPAY_CONFIGURATION_INVALID", "message": str(exc)},
                    ) from exc

                charged_unit_price_fen = billing["charged_unit_price_fen"]
                credits = payload.amount_fen // charged_unit_price_fen
                form_fields = build_zpay_payment_form(
                    merchant_order_no=merchant_order_no,
                    amount_fen=payload.amount_fen,
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
                        ") VALUES (%s, %s, %s, 'zpay', NULL, %s, 'PENDING', 'INTERNAL', "
                        "%s, %s, %s, %s, %s, %s)\n",
                        (
                            str(uuid4()),
                            user.id,
                            merchant_order_no,
                            merchant.channel,
                            billing["internal_base_unit_price_fen"],
                            charged_unit_price_fen,
                            billing["min_recharge_fen"],
                            billing["recharge_step_fen"],
                            payload.amount_fen,
                            credits,
                        ),
                    )
                return RechargeOrderResponse(
                    order_no=merchant_order_no,
                    status="PENDING",
                    amount_fen=payload.amount_fen,
                    credits=credits,
                    gateway_url=deployment.gateway_url,
                    method="POST",
                    form_fields=form_fields,
                )
        except (sqlite3.IntegrityError, psycopg.errors.UniqueViolation) as exc:
            # The merchant-order-number collision is a retryable random draw; any
            # other integrity failure is a real bug and must surface.
            if "merchant_order_no" in str(exc):
                continue
            raise

    raise HTTPException(
        status_code=503,
        detail={"code": "ORDER_NUMBER_UNAVAILABLE", "message": "Unable to allocate order number."},
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
    ):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "INVALID_RECHARGE_AMOUNT",
                "message": "Recharge amount must meet the configured minimum and step.",
            },
        )
