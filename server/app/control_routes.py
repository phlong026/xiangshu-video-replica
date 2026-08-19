from __future__ import annotations

import csv
import io
import sqlite3
from typing import Literal, cast

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from app.auth import Database, Role
from app.control_auth import ControlUser
from app.permissions import write_audit
from app.settings import SettingsRepository
from app.zpay import deployment_config_from_environment

router = APIRouter(prefix="/api/control", tags=["control"])

OrderStatus = Literal["PENDING", "PAID", "FAILED", "CLOSED"]
TransactionType = Literal["CHARGE", "RESERVE", "SETTLE", "RELEASE"]


class AccountWallet(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    username: str
    display_name: str
    role: Role
    is_active: bool
    available_credits: int
    reserved_credits: int
    active_token_count: int


class AccountWalletPage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[AccountWallet]
    total: int
    limit: int
    offset: int


class ControlRechargeOrder(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    user_id: str
    username: str
    display_name: str
    order_no: str
    status: OrderStatus
    amount_fen: int
    credits: int
    channel: str
    provider_trade_no: str | None
    created_at: str
    paid_at: str | None


class ControlRechargeOrderPage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[ControlRechargeOrder]
    total: int
    limit: int
    offset: int


class ControlWalletTransaction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    user_id: str
    username: str
    type: TransactionType
    available_delta: int
    reserved_delta: int
    recharge_order_id: str | None
    task_id: str | None
    billing_round: int | None
    created_at: str


class ControlWalletTransactionPage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[ControlWalletTransaction]
    total: int
    limit: int
    offset: int


class ReconciliationSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    wallet_count: int
    wallet_mismatch_count: int
    paid_order_without_charge_count: int
    charge_without_paid_order_count: int
    pending_order_count: int


class ZPaySettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pid: str = Field(min_length=1)
    key: str | None = None
    enabled_channels: list[Literal["alipay", "wxpay"]] = Field(min_length=1)


class BillingSettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    internal_base_unit_price_fen: StrictInt
    min_recharge_fen: StrictInt
    recharge_step_fen: StrictInt


class BillingSettingsSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    internal_base_unit_price_fen: int
    charged_unit_price_fen: int
    min_recharge_fen: int
    recharge_step_fen: int


class MaskedZPaySettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Literal["zpay"]
    configured: bool
    config: dict[str, str]


class DeploymentSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    gateway_url: str
    notify_url: str
    return_url: str


class ControlSettingsSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    billing: BillingSettingsSnapshot
    zpay: MaskedZPaySettings
    deployment: DeploymentSettings


@router.get("/accounts", response_model=AccountWalletPage)
def list_accounts(
    conn: Database,
    _actor: ControlUser,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> AccountWalletPage:
    total = int(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0])
    rows = conn.execute(
        """
        SELECT
            users.id,
            users.username,
            users.display_name,
            users.role,
            users.is_active,
            wallets.available_credits,
            wallets.reserved_credits,
            COUNT(internal_access_tokens.id) AS active_token_count
        FROM users
        JOIN wallets ON wallets.user_id = users.id
        LEFT JOIN internal_access_tokens
          ON internal_access_tokens.user_id = users.id
         AND internal_access_tokens.revoked_at IS NULL
        GROUP BY users.id
        ORDER BY users.username, users.id
        LIMIT ? OFFSET ?
        """,
        (limit, offset),
    ).fetchall()
    return AccountWalletPage(
        items=[
            AccountWallet(
                id=str(row["id"]),
                username=str(row["username"]),
                display_name=str(row["display_name"]),
                role=cast(Role, str(row["role"])),
                is_active=bool(row["is_active"]),
                available_credits=int(row["available_credits"]),
                reserved_credits=int(row["reserved_credits"]),
                active_token_count=int(row["active_token_count"]),
            )
            for row in rows
        ],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/recharge-orders", response_model=ControlRechargeOrderPage)
def list_recharge_orders(
    conn: Database,
    _actor: ControlUser,
    status: OrderStatus | None = None,
    user_id: str | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> ControlRechargeOrderPage:
    where, params = _order_filters(status=status, user_id=user_id)
    total = int(
        conn.execute(
            f"SELECT COUNT(*) FROM recharge_orders AS orders {where}",  # noqa: S608
            params,
        ).fetchone()[0]
    )
    rows = conn.execute(
        f"""
        SELECT
            orders.id,
            orders.user_id,
            users.username,
            users.display_name,
            orders.merchant_order_no AS order_no,
            orders.status,
            orders.amount_fen,
            orders.credits,
            COALESCE(orders.channel, '') AS channel,
            orders.provider_trade_no,
            orders.created_at,
            orders.paid_at
        FROM recharge_orders AS orders
        JOIN users ON users.id = orders.user_id
        {where}
        ORDER BY orders.created_at DESC, orders.id DESC
        LIMIT ? OFFSET ?
        """,  # noqa: S608
        (*params, limit, offset),
    ).fetchall()
    return ControlRechargeOrderPage(
        items=[ControlRechargeOrder(**dict(row)) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/wallet-transactions", response_model=ControlWalletTransactionPage)
def list_wallet_transactions(
    conn: Database,
    _actor: ControlUser,
    user_id: str | None = None,
    type: TransactionType | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> ControlWalletTransactionPage:
    where, params = _transaction_filters(user_id=user_id, transaction_type=type)
    total = int(
        conn.execute(
            f"SELECT COUNT(*) FROM wallet_transactions AS tx {where}",  # noqa: S608
            params,
        ).fetchone()[0]
    )
    rows = conn.execute(
        f"""
        SELECT
            tx.id,
            tx.user_id,
            users.username,
            tx.type,
            tx.available_delta,
            tx.reserved_delta,
            tx.recharge_order_id,
            tx.task_id,
            tx.billing_round,
            tx.created_at
        FROM wallet_transactions AS tx
        JOIN users ON users.id = tx.user_id
        {where}
        ORDER BY tx.created_at DESC, tx.id DESC
        LIMIT ? OFFSET ?
        """,  # noqa: S608
        (*params, limit, offset),
    ).fetchall()
    return ControlWalletTransactionPage(
        items=[ControlWalletTransaction(**dict(row)) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/billing-reconciliation", response_model=ReconciliationSummary)
def read_reconciliation(conn: Database, _actor: ControlUser) -> ReconciliationSummary:
    row = conn.execute(
        """
        SELECT
            (SELECT COUNT(*) FROM wallets) AS wallet_count,
            (
                SELECT COUNT(*)
                FROM wallets
                LEFT JOIN (
                    SELECT
                        user_id,
                        SUM(available_delta) AS available_total,
                        SUM(reserved_delta) AS reserved_total
                    FROM wallet_transactions
                    GROUP BY user_id
                ) AS ledger ON ledger.user_id = wallets.user_id
                WHERE wallets.available_credits != COALESCE(ledger.available_total, 0)
                   OR wallets.reserved_credits != COALESCE(ledger.reserved_total, 0)
            ) AS wallet_mismatch_count,
            (
                SELECT COUNT(*)
                FROM recharge_orders AS orders
                LEFT JOIN wallet_transactions AS tx
                  ON tx.recharge_order_id = orders.id AND tx.type = 'CHARGE'
                WHERE orders.status = 'PAID' AND tx.id IS NULL
            ) AS paid_order_without_charge_count,
            (
                SELECT COUNT(*)
                FROM wallet_transactions AS tx
                JOIN recharge_orders AS orders ON orders.id = tx.recharge_order_id
                WHERE tx.type = 'CHARGE' AND orders.status != 'PAID'
            ) AS charge_without_paid_order_count,
            (
                SELECT COUNT(*) FROM recharge_orders WHERE status = 'PENDING'
            ) AS pending_order_count
        """
    ).fetchone()
    return ReconciliationSummary(**dict(row))


@router.get("/settings", response_model=ControlSettingsSnapshot)
def read_control_settings(conn: Database, _actor: ControlUser) -> ControlSettingsSnapshot:
    deployment = deployment_config_from_environment()
    repo = SettingsRepository(conn)
    return ControlSettingsSnapshot(
        billing=BillingSettingsSnapshot(**repo.read_billing_settings()),
        zpay=MaskedZPaySettings(**repo.read_zpay_config()),
        deployment=DeploymentSettings(
            gateway_url=deployment.gateway_url,
            notify_url=deployment.notify_url,
            return_url=deployment.return_url,
        ),
    )


@router.patch("/settings/zpay", response_model=MaskedZPaySettings)
def update_zpay_settings(
    payload: ZPaySettingsUpdate,
    conn: Database,
    actor: ControlUser,
) -> MaskedZPaySettings:
    repo = SettingsRepository(conn)
    current = repo.load_zpay_config()
    incoming_key = (payload.key or "").strip()
    if incoming_key.startswith("********") or not incoming_key:
        incoming_key = current.get("key", "")
    try:
        result = repo.save_zpay_config(
            {
                "pid": payload.pid,
                "key": incoming_key,
                "enabled_channels": ",".join(payload.enabled_channels),
            },
            actor_user_id=actor.id,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "INVALID_ZPAY_SETTINGS", "message": str(exc)},
        ) from exc
    write_audit(
        conn,
        actor=actor,
        action="zpay_settings.update",
        entity_type="provider_settings",
        entity_id="zpay",
        metadata={"enabled_channels": payload.enabled_channels},
    )
    return MaskedZPaySettings(**result)


@router.patch("/settings/billing", response_model=BillingSettingsSnapshot)
def update_control_billing_settings(
    payload: BillingSettingsUpdate,
    conn: Database,
    actor: ControlUser,
) -> BillingSettingsSnapshot:
    try:
        result = SettingsRepository(conn).save_billing_settings(
            internal_base_unit_price_fen=payload.internal_base_unit_price_fen,
            min_recharge_fen=payload.min_recharge_fen,
            recharge_step_fen=payload.recharge_step_fen,
            actor_user_id=actor.id,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "INVALID_BILLING_SETTINGS", "message": str(exc)},
        ) from exc
    write_audit(
        conn,
        actor=actor,
        action="billing_settings.update",
        entity_type="runtime_settings",
        entity_id="1",
        metadata={"scope": "INTERNAL"},
    )
    return BillingSettingsSnapshot(**result)


@router.get("/recharge-orders.csv")
def export_recharge_orders_csv(
    conn: Database,
    _actor: ControlUser,
    status: OrderStatus | None = None,
    user_id: str | None = None,
    limit: int = Query(default=5000, ge=1, le=5000),
) -> Response:
    where, params = _order_filters(status=status, user_id=user_id)
    rows = conn.execute(
        f"""
        SELECT
            orders.merchant_order_no,
            orders.user_id,
            users.username,
            orders.amount_fen,
            orders.credits,
            orders.status,
            COALESCE(orders.channel, '') AS channel,
            COALESCE(orders.provider_trade_no, '') AS provider_trade_no,
            orders.created_at,
            COALESCE(orders.paid_at, '') AS paid_at
        FROM recharge_orders AS orders
        JOIN users ON users.id = orders.user_id
        {where}
        ORDER BY orders.created_at DESC, orders.id DESC
        LIMIT ?
        """,  # noqa: S608
        (*params, limit),
    ).fetchall()
    return _csv_response(
        filename="recharge-orders.csv",
        headers=(
            "order_no",
            "user_id",
            "username",
            "amount_fen",
            "credits",
            "status",
            "channel",
            "provider_trade_no",
            "created_at",
            "paid_at",
        ),
        rows=rows,
    )


@router.get("/wallet-transactions.csv")
def export_wallet_transactions_csv(
    conn: Database,
    _actor: ControlUser,
    user_id: str | None = None,
    type: TransactionType | None = None,
    limit: int = Query(default=5000, ge=1, le=5000),
) -> Response:
    where, params = _transaction_filters(user_id=user_id, transaction_type=type)
    rows = conn.execute(
        f"""
        SELECT
            tx.id,
            tx.user_id,
            users.username,
            tx.type,
            tx.available_delta,
            tx.reserved_delta,
            COALESCE(tx.recharge_order_id, '') AS recharge_order_id,
            COALESCE(tx.task_id, '') AS task_id,
            COALESCE(tx.billing_round, '') AS billing_round,
            tx.created_at
        FROM wallet_transactions AS tx
        JOIN users ON users.id = tx.user_id
        {where}
        ORDER BY tx.created_at DESC, tx.id DESC
        LIMIT ?
        """,  # noqa: S608
        (*params, limit),
    ).fetchall()
    return _csv_response(
        filename="wallet-transactions.csv",
        headers=(
            "id",
            "user_id",
            "username",
            "type",
            "available_delta",
            "reserved_delta",
            "recharge_order_id",
            "task_id",
            "billing_round",
            "created_at",
        ),
        rows=rows,
    )


def _order_filters(
    *,
    status: OrderStatus | None,
    user_id: str | None,
) -> tuple[str, tuple[str, ...]]:
    clauses: list[str] = []
    params: list[str] = []
    if status is not None:
        clauses.append("orders.status = ?")
        params.append(status)
    if user_id is not None:
        clauses.append("orders.user_id = ?")
        params.append(user_id)
    return (f"WHERE {' AND '.join(clauses)}" if clauses else "", tuple(params))


def _transaction_filters(
    *,
    user_id: str | None,
    transaction_type: TransactionType | None,
) -> tuple[str, tuple[str, ...]]:
    clauses: list[str] = []
    params: list[str] = []
    if user_id is not None:
        clauses.append("tx.user_id = ?")
        params.append(user_id)
    if transaction_type is not None:
        clauses.append("tx.type = ?")
        params.append(transaction_type)
    return (f"WHERE {' AND '.join(clauses)}" if clauses else "", tuple(params))


def _csv_response(
    *,
    filename: str,
    headers: tuple[str, ...],
    rows: list[sqlite3.Row],
) -> Response:
    output = io.StringIO()
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(headers)
    for row in rows:
        writer.writerow([_spreadsheet_safe_cell(row[index]) for index in range(len(headers))])
    return Response(
        content="\ufeff" + output.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _spreadsheet_safe_cell(value: object) -> object:
    if isinstance(value, str) and value.startswith(("=", "+", "-", "@", "\t", "\r")):
        return f"'{value}"
    return value
