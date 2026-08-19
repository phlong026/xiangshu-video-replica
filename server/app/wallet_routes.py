from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict

from app.auth import AuthenticatedUser, Database

router = APIRouter(prefix="/api/wallet", tags=["wallet"])


class WalletResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    available_credits: int
    reserved_credits: int


class WalletTransactionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    user_id: str
    type: Literal["CHARGE", "RESERVE", "SETTLE", "RELEASE"]
    available_delta: int
    reserved_delta: int
    recharge_order_id: str | None
    task_id: str | None
    billing_round: int | None
    created_at: str


class WalletTransactionPage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[WalletTransactionResponse]
    total: int
    limit: int
    offset: int


@router.get("", response_model=WalletResponse)
def read_wallet(conn: Database, actor: AuthenticatedUser) -> WalletResponse:
    row = conn.execute(
        """
        SELECT available_credits, reserved_credits
        FROM wallets
        WHERE user_id = ?
        """,
        (actor.id,),
    ).fetchone()
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "WALLET_NOT_FOUND", "message": "Wallet does not exist."},
        )
    return WalletResponse(
        available_credits=int(row["available_credits"]),
        reserved_credits=int(row["reserved_credits"]),
    )


@router.get("/transactions", response_model=WalletTransactionPage)
def list_wallet_transactions(
    conn: Database,
    actor: AuthenticatedUser,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> WalletTransactionPage:
    total = int(
        conn.execute(
            "SELECT COUNT(*) FROM wallet_transactions WHERE user_id = ?",
            (actor.id,),
        ).fetchone()[0]
    )
    rows = conn.execute(
        """
        SELECT
            id, user_id, type, available_delta, reserved_delta,
            recharge_order_id, task_id, billing_round, created_at
        FROM wallet_transactions
        WHERE user_id = ?
        ORDER BY created_at DESC, id DESC
        LIMIT ? OFFSET ?
        """,
        (actor.id, limit, offset),
    ).fetchall()
    return WalletTransactionPage(
        items=[WalletTransactionResponse(**dict(row)) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )
