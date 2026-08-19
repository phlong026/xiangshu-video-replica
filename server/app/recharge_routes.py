from __future__ import annotations

import sqlite3
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ConfigDict, StrictInt

from app.auth import AuthenticatedUser, Database
from app.settings import SettingsRepository
from app.zpay import (
    build_zpay_payment_form,
    deployment_config_from_environment,
    generate_merchant_order_no,
    merchant_config_from_settings,
)

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


@router.post(
    "/recharge-orders",
    response_model=RechargeOrderResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_recharge_order(
    payload: CreateRechargeOrderRequest,
    conn: Database,
    user: AuthenticatedUser,
) -> RechargeOrderResponse:
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

    for _ in range(MAX_ORDER_NUMBER_ATTEMPTS):
        merchant_order_no = generate_merchant_order_no()
        form_fields = build_zpay_payment_form(
            merchant_order_no=merchant_order_no,
            amount_fen=payload.amount_fen,
            credits=credits,
            merchant=merchant,
            deployment=deployment,
        )
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO recharge_orders (
                        id, user_id, merchant_order_no, provider, provider_trade_no,
                        channel, status, pricing_scope,
                        base_unit_price_fen_snapshot, charged_unit_price_fen_snapshot,
                        min_recharge_fen_snapshot, recharge_step_fen_snapshot,
                        amount_fen, credits
                    ) VALUES (?, ?, ?, 'zpay', NULL, ?, 'PENDING', 'INTERNAL', ?, ?, ?, ?, ?, ?)
                    """,
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
        except sqlite3.IntegrityError as exc:
            if "recharge_orders.merchant_order_no" in str(exc):
                continue
            raise

        return RechargeOrderResponse(
            order_no=merchant_order_no,
            status="PENDING",
            amount_fen=payload.amount_fen,
            credits=credits,
            gateway_url=deployment.gateway_url,
            method="POST",
            form_fields=form_fields,
        )

    raise HTTPException(
        status_code=503,
        detail={"code": "ORDER_NUMBER_UNAVAILABLE", "message": "Unable to allocate order number."},
    )


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
