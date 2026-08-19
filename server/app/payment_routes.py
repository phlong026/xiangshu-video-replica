from __future__ import annotations

import hashlib
import hmac
import logging
import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse

from app.auth import Database
from app.control_auth import ControlUser
from app.recharge_routes import RechargeOrderStatusResponse
from app.settings import SettingsRepository
from app.zpay import (
    ZPayMerchantConfig,
    ZPayOrderQueryClient,
    ZPayOrderQueryError,
    deployment_config_from_environment,
    merchant_config_from_settings,
    parse_zpay_money_to_fen,
    sign_zpay_params,
    zpay_signing_string,
)
from app.zpay_payments import (
    PaymentConfirmationError,
    confirm_recharge_payment,
    read_recharge_order,
    serialize_recharge_order,
)

router = APIRouter(prefix="/api", tags=["payments"])
logger = logging.getLogger(__name__)


def get_zpay_order_query_client() -> ZPayOrderQueryClient:
    return ZPayOrderQueryClient()


ZPayOrderQuery = Annotated[ZPayOrderQueryClient, Depends(get_zpay_order_query_client)]


@router.get("/payments/zpay/notify", response_class=PlainTextResponse)
def zpay_notify(request: Request, conn: Database) -> PlainTextResponse:
    params = _unique_query_params(request)
    merchant = _load_merchant_config(conn)

    signature = params.get("sign", "")
    if params.get("sign_type", "").upper() != "MD5" or not hmac.compare_digest(
        sign_zpay_params(params, merchant.key), signature
    ):
        return PlainTextResponse("failure", status_code=400)
    if params.get("pid") != merchant.pid:
        return PlainTextResponse("failure", status_code=400)
    if params.get("trade_status") != "TRADE_SUCCESS":
        return PlainTextResponse("failure", status_code=400)

    merchant_order_no = params.get("out_trade_no", "")
    provider_trade_no = params.get("trade_no", "")
    channel = params.get("type", "")
    try:
        amount_fen = parse_zpay_money_to_fen(params.get("money", ""))
    except ValueError:
        return PlainTextResponse("failure", status_code=400)
    if not merchant_order_no or not provider_trade_no or not channel:
        return PlainTextResponse("failure", status_code=400)

    source_digest = hashlib.sha256(zpay_signing_string(params).encode("utf-8")).hexdigest()
    try:
        confirm_recharge_payment(
            conn,
            merchant_order_no=merchant_order_no,
            provider_trade_no=provider_trade_no,
            amount_fen=amount_fen,
            channel=channel,
            source_digest=source_digest,
        )
    except PaymentConfirmationError as exc:
        logger.warning("ZPay callback rejected: %s", exc.code)
        return PlainTextResponse("failure", status_code=exc.status_code)
    except sqlite3.OperationalError:
        logger.warning("ZPay callback deferred because the payment database is busy")
        return PlainTextResponse("retry", status_code=503)
    return PlainTextResponse("success")


@router.get("/payments/zpay/return", response_class=HTMLResponse)
def zpay_return() -> HTMLResponse:
    return HTMLResponse(
        "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
        "<title>支付确认中</title></head><body><main><h1>正在确认支付</h1>"
        "<p>请返回内部系统查看充值状态。</p></main></body></html>"
    )


@router.post(
    "/control/recharge-orders/{order_no}/sync",
    response_model=RechargeOrderStatusResponse,
)
def sync_recharge_order_with_zpay(
    order_no: str,
    conn: Database,
    _actor: ControlUser,
    query_client: ZPayOrderQuery,
) -> RechargeOrderStatusResponse:
    local_order = read_recharge_order(conn, merchant_order_no=order_no)
    if local_order is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "RECHARGE_ORDER_NOT_FOUND",
                "message": "Recharge order does not exist.",
            },
        )
    if str(local_order["status"]) == "PAID":
        return RechargeOrderStatusResponse(**serialize_recharge_order(local_order))
    if str(local_order["status"]) != "PENDING":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "RECHARGE_ORDER_NOT_SYNCABLE",
                "message": "Recharge order is not waiting for payment.",
            },
        )

    merchant = _load_merchant_config(conn)
    try:
        deployment = deployment_config_from_environment()
    except ValueError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "ZPAY_CONFIGURATION_INVALID", "message": str(exc)},
        ) from exc
    try:
        remote_order = query_client.query_order(
            merchant=merchant,
            deployment=deployment,
            merchant_order_no=order_no,
        )
    except ZPayOrderQueryError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={
                "code": "ZPAY_QUERY_FAILED",
                "message": "ZPay order status could not be confirmed.",
            },
        ) from exc

    if not remote_order.paid:
        return RechargeOrderStatusResponse(**serialize_recharge_order(local_order))
    if (
        remote_order.merchant_order_no != order_no
        or remote_order.provider_trade_no is None
        or remote_order.amount_fen is None
        or remote_order.channel is None
    ):
        raise HTTPException(
            status_code=502,
            detail={
                "code": "ZPAY_QUERY_RESPONSE_INVALID",
                "message": "ZPay paid order response is incomplete.",
            },
        )

    try:
        confirmed = confirm_recharge_payment(
            conn,
            merchant_order_no=order_no,
            provider_trade_no=remote_order.provider_trade_no,
            amount_fen=remote_order.amount_fen,
            channel=remote_order.channel,
            source_digest=remote_order.response_digest,
        )
    except PaymentConfirmationError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    except sqlite3.OperationalError as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "PAYMENT_DATABASE_BUSY",
                "message": "Payment settlement is temporarily busy.",
            },
        ) from exc
    return RechargeOrderStatusResponse(**serialize_recharge_order(confirmed))


def _load_merchant_config(conn: sqlite3.Connection) -> ZPayMerchantConfig:
    try:
        return merchant_config_from_settings(SettingsRepository(conn).load_zpay_config())
    except ValueError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "ZPAY_CONFIGURATION_INVALID", "message": str(exc)},
        ) from exc


def _unique_query_params(request: Request) -> dict[str, str]:
    params: dict[str, str] = {}
    for name, value in request.query_params.multi_items():
        if name in params:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "ZPAY_DUPLICATE_PARAMETER",
                    "message": "ZPay callback contains duplicate parameters.",
                },
            )
        params[name] = value
    return params
