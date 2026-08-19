from __future__ import annotations

import sqlite3
from typing import Literal, TypedDict, cast
from uuid import uuid4

ZPAY_NOTIFY_BUSY_TIMEOUT_MS = 1000
RechargeStatus = Literal["PENDING", "PAID", "FAILED", "CLOSED"]


class RechargeOrderData(TypedDict):
    order_no: str
    status: RechargeStatus
    amount_fen: int
    credits: int
    channel: str
    created_at: str
    paid_at: str | None


class PaymentConfirmationError(RuntimeError):
    def __init__(self, code: str, message: str, *, status_code: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def read_recharge_order(
    conn: sqlite3.Connection,
    *,
    merchant_order_no: str,
) -> sqlite3.Row | None:
    return cast(
        sqlite3.Row | None,
        conn.execute(
            """
            SELECT
                id, user_id, merchant_order_no, provider, provider_trade_no, channel, status,
                amount_fen, credits, notify_digest, created_at, paid_at
            FROM recharge_orders
            WHERE merchant_order_no = ?
            """,
            (merchant_order_no,),
        ).fetchone(),
    )


def serialize_recharge_order(row: sqlite3.Row) -> RechargeOrderData:
    return {
        "order_no": str(row["merchant_order_no"]),
        "status": cast(RechargeStatus, str(row["status"])),
        "amount_fen": int(row["amount_fen"]),
        "credits": int(row["credits"]),
        "channel": str(row["channel"]),
        "created_at": str(row["created_at"]),
        "paid_at": None if row["paid_at"] is None else str(row["paid_at"]),
    }


def confirm_recharge_payment(
    conn: sqlite3.Connection,
    *,
    merchant_order_no: str,
    provider_trade_no: str,
    amount_fen: int,
    channel: str,
    source_digest: str,
) -> sqlite3.Row:
    if not merchant_order_no or not provider_trade_no.strip():
        raise PaymentConfirmationError(
            "ZPAY_PAYMENT_REFERENCE_INVALID",
            "ZPay order and trade numbers are required.",
            status_code=400,
        )

    conn.execute(f"PRAGMA busy_timeout = {ZPAY_NOTIFY_BUSY_TIMEOUT_MS}")
    try:
        conn.execute("BEGIN IMMEDIATE")
        order = read_recharge_order(conn, merchant_order_no=merchant_order_no)
        if order is None:
            raise PaymentConfirmationError(
                "ZPAY_ORDER_NOT_FOUND",
                "Recharge order does not exist.",
                status_code=404,
            )
        if str(order["provider"]) != "zpay":
            raise PaymentConfirmationError(
                "ZPAY_PROVIDER_MISMATCH",
                "Recharge order provider does not match ZPay.",
            )
        if int(order["amount_fen"]) != amount_fen:
            raise PaymentConfirmationError(
                "ZPAY_AMOUNT_MISMATCH",
                "ZPay amount does not match the stored recharge order.",
            )
        if str(order["channel"]) != channel:
            raise PaymentConfirmationError(
                "ZPAY_CHANNEL_MISMATCH",
                "ZPay channel does not match the stored recharge order.",
            )

        bound_order = conn.execute(
            """
            SELECT merchant_order_no
            FROM recharge_orders
            WHERE provider_trade_no = ? AND merchant_order_no != ?
            """,
            (provider_trade_no, merchant_order_no),
        ).fetchone()
        if bound_order is not None:
            raise PaymentConfirmationError(
                "ZPAY_TRADE_ALREADY_BOUND",
                "ZPay trade number is already bound to another recharge order.",
            )

        existing_trade_no = order["provider_trade_no"]
        if existing_trade_no is not None and str(existing_trade_no) != provider_trade_no:
            raise PaymentConfirmationError(
                "ZPAY_TRADE_NO_MISMATCH",
                "ZPay trade number does not match the stored recharge order.",
            )
        if str(order["status"]) == "PAID":
            conn.rollback()
            return order
        if str(order["status"]) != "PENDING":
            raise PaymentConfirmationError(
                "ZPAY_ORDER_NOT_SETTLEABLE",
                "Recharge order is not waiting for settlement.",
            )

        updated = conn.execute(
            """
            UPDATE recharge_orders
            SET status = 'PAID',
                provider_trade_no = ?,
                notify_digest = ?,
                paid_at = CURRENT_TIMESTAMP
            WHERE id = ? AND status = 'PENDING'
            """,
            (provider_trade_no, source_digest, str(order["id"])),
        )
        if updated.rowcount != 1:
            raise PaymentConfirmationError(
                "ZPAY_ORDER_CHANGED",
                "Recharge order changed while payment was being confirmed.",
            )

        conn.execute(
            """
            INSERT INTO wallet_transactions (
                id, user_id, type, available_delta, reserved_delta,
                recharge_order_id, task_id, billing_round, idempotency_key
            ) VALUES (?, ?, 'CHARGE', ?, 0, ?, NULL, NULL, ?)
            """,
            (
                str(uuid4()),
                str(order["user_id"]),
                int(order["credits"]),
                str(order["id"]),
                f"zpay:charge:{order['id']}",
            ),
        )
        wallet = conn.execute(
            """
            UPDATE wallets
            SET available_credits = available_credits + ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE user_id = ?
            """,
            (int(order["credits"]), str(order["user_id"])),
        )
        if wallet.rowcount != 1:
            raise PaymentConfirmationError(
                "WALLET_NOT_FOUND",
                "Wallet record is missing for the recharge order owner.",
                status_code=500,
            )

        conn.commit()
        confirmed = read_recharge_order(conn, merchant_order_no=merchant_order_no)
        if confirmed is None:  # pragma: no cover - protected by the transaction above
            raise RuntimeError("confirmed recharge order disappeared")
        return confirmed
    except PaymentConfirmationError:
        conn.rollback()
        raise
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        raise PaymentConfirmationError(
            "ZPAY_SETTLEMENT_CONFLICT",
            "Payment settlement conflicts with an existing ledger entry.",
        ) from exc
    except Exception:
        conn.rollback()
        raise
