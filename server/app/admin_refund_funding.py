"""退款只撤回明确原充值的可用来源；不重算或修补历史账务。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, NamedTuple, Never

import psycopg

from app.admin_write_contract import http_error


class RefundSource(NamedTuple):
    order_id: str
    user_id: str
    status: str
    provider: str
    amount_fen: int
    credits: int
    package_id: str | None
    package_snapshot: str | None
    provider_trade_no: str | None
    transaction_id: str | None
    paid_at: str | None
    charge_id: str
    charge_user_id: str
    charge_type: str
    charge_credits: int
    charge_reserved: int
    lot_id: str
    lot_user_id: str
    lot_credits: int
    lot_remaining: int
    lot_amount_fen: Decimal | None
    charge_created_at: str


@dataclass(frozen=True)
class RefundFunding:
    original_recharge_order_id: str
    original_recharge_order_no: str
    credit_lot_id: str
    credits: int
    source_credits: int
    source_amount_fen: Decimal
    lot_remaining_before: int
    lot_remaining_after: int

    def payload(self) -> dict[str, object]:
        return {
            "original_recharge_order_id": self.original_recharge_order_id,
            "original_recharge_order_no": self.original_recharge_order_no,
            "credit_lot_id": self.credit_lot_id,
            "credits": self.credits,
            "source_credits": self.source_credits,
            "source_amount_fen": str(self.source_amount_fen),
            "lot_remaining_before": self.lot_remaining_before,
            "lot_remaining_after": self.lot_remaining_after,
        }


def _unverified() -> Never:
    raise http_error(
        422, "REFUND_SOURCE_UNVERIFIED", "无法核对该客户原充值的纯实付积分来源，请转人工核对。"
    )


def _unreconciled() -> Never:
    raise http_error(
        422,
        "REFUND_SOURCE_RECONCILIATION_REQUIRED",
        "原充值来源账存在缺失或未核对的历史变动，已暂停本次扣减，请转人工核对。",
    )


def _integer(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        _unreconciled()
    return value


def source_commitments(
    operations: list[Any], *, lot_id: str, source_credits: int, source_amount_fen: Decimal
) -> tuple[int, int]:
    """只读已冻结的分配证据，按真实结算顺序核对原来源的预留和消耗。"""
    held = consumed = 0
    for row in operations:
        _operation_id, state, budget, charged, funding_json = row
        reserved = _integer(budget)
        actual = _integer(charged)
        if (
            state not in {"PENDING", "SUCCEEDED", "FAILED", "CANCELLED"}
            or actual > reserved
            or (state in {"PENDING", "FAILED", "CANCELLED"} and actual != 0)
        ):
            _unreconciled()
        try:
            parts = json.loads(str(funding_json))
        except (ValueError, TypeError):
            _unreconciled()
        if not isinstance(parts, list):
            _unreconciled()
        total = 0
        remaining = actual
        for part in parts:
            if not isinstance(part, dict):
                _unreconciled()
            if not {"lot_id", "credits", "lot_credits", "amount_fen"}.issubset(part):
                _unreconciled()
            count = _integer(part.get("credits"))
            lot_credits = _integer(part.get("lot_credits"))
            if count <= 0 or lot_credits < count:
                _unreconciled()
            if part["lot_id"] is not None and not isinstance(part["lot_id"], str):
                _unreconciled()
            if part["amount_fen"] is not None:
                try:
                    part_amount = Decimal(str(part["amount_fen"]))
                except InvalidOperation:
                    _unreconciled()
                if not part_amount.is_finite() or part_amount < 0:
                    _unreconciled()
            if part["lot_id"] is None and (part["amount_fen"] is not None or lot_credits != count):
                _unreconciled()
            total += count
            used = min(remaining, count)
            remaining -= used
            if part.get("lot_id") != lot_id:
                continue
            try:
                basis = Decimal(str(part.get("amount_fen")))
            except InvalidOperation:
                _unreconciled()
            if (
                not basis.is_finite()
                or part.get("lot_credits") != source_credits
                or basis != source_amount_fen
            ):
                _unreconciled()
            if state == "PENDING":
                held += count
            else:
                consumed += used
        if total != reserved or remaining:
            _unreconciled()
    return held, consumed


def _evidence_time(value: object) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (ValueError, TypeError):
        _unreconciled()
    if parsed.tzinfo is None:
        _unreconciled()
    return parsed


def deduct_refund_source(
    conn: psycopg.Connection, *, user_id: str, order_no: str, credits: int
) -> RefundFunding:
    """调用方同一事务内写退款流水、钱包、审计；任何拒绝都会回滚全部变化。"""
    if credits <= 0:
        raise ValueError("退款来源扣减必须为正整数")
    wallet = conn.execute(
        "SELECT available_credits, reserved_credits FROM wallets WHERE user_id=%s FOR UPDATE",
        (user_id,),
    ).fetchone()
    if wallet is None:
        raise http_error(404, "WALLET_NOT_FOUND", "客户钱包不存在。")

    # 订单和支付事实只读；只锁来源 lot，避免与支付回调的订单→钱包锁顺序相反。
    rows = conn.execute(
        "SELECT ro.id,ro.user_id,ro.status,ro.provider,ro.amount_fen,ro.credits,"
        "ro.package_id,ro.package_snapshot_json,ro.provider_trade_no,ro.transaction_id,ro.paid_at,"
        "tx.id,tx.user_id,tx.type,tx.available_delta,tx.reserved_delta,"
        "lot.id,lot.user_id,lot.credits,lot.remaining_credits,lot.amount_fen,tx.created_at "
        "FROM recharge_orders ro "
        "JOIN wallet_transactions tx ON tx.recharge_order_id=ro.id AND tx.type='CHARGE' "
        "JOIN billing_credit_lots lot ON lot.id=tx.id "
        "WHERE ro.merchant_order_no=%s AND ro.user_id=%s FOR UPDATE OF lot",
        (order_no, user_id),
    ).fetchall()
    if len(rows) != 1:
        _unverified()
    source = RefundSource(*rows[0])
    # 套餐仅有合并积分，没有付费/赠送的不可变拆分；不能把有金额误当纯现金。
    if (
        source.user_id != user_id
        or source.charge_user_id != user_id
        or source.lot_user_id != user_id
        or source.status != "PAID"
        or not str(source.paid_at or "").strip()
        or source.provider not in {"zpay", "wechat_native"}
        or not str(
            (source.provider_trade_no if source.provider == "zpay" else source.transaction_id) or ""
        ).strip()
        or source.package_id is not None
        or source.package_snapshot is not None
        or source.amount_fen <= 0
        or source.credits <= 0
        or source.charge_type != "CHARGE"
        or source.charge_reserved != 0
        or source.charge_id != source.lot_id
        or source.credits != source.charge_credits
        or source.credits != source.lot_credits
        or source.lot_amount_fen is None
        or not source.lot_amount_fen.is_finite()
        or source.lot_amount_fen != Decimal(source.amount_fen)
    ):
        _unverified()

    # 旧 REFUND 没有原充值分配，不能靠总余额相等或消费 FIFO 猜它冲了哪张单。
    ambiguous = conn.execute(
        "SELECT 1 FROM wallet_transactions tx "
        "LEFT JOIN admin_refund_allocations ra ON ra.refund_transaction_id=tx.id "
        "WHERE tx.user_id=%s AND ((tx.type='REFUND' AND tx.available_delta<0 "
        "AND ra.adjustment_id IS NULL) OR tx.type='CONVERSION') LIMIT 1",
        (user_id,),
    ).fetchone()
    if ambiguous is not None:
        _unreconciled()
    legacy = conn.execute(
        "SELECT created_at FROM wallet_transactions WHERE user_id=%s "
        "AND type IN ('RESERVE','SETTLE','RELEASE') AND billing_operation_id IS NULL",
        (user_id,),
    ).fetchall()
    if legacy:
        # 旧版消耗未记录 lot 分配；只有时间明确早于这笔充值才可排除影响。
        # 非法或缺时区的时间转人工，不能让数据库强转异常变成模糊的 500。
        charged_at = _evidence_time(source.charge_created_at)
        if any(_evidence_time(row[0]) >= charged_at for row in legacy):
            _unreconciled()
    lots_total = conn.execute(
        "SELECT COALESCE(SUM(remaining_credits),0) FROM billing_credit_lots WHERE user_id=%s",
        (user_id,),
    ).fetchone()
    if lots_total is None or int(lots_total[0]) > int(wallet[0]):
        _unreconciled()
    operations = conn.execute(
        "SELECT id,state,reserved_credits,charged_credits,funding_json FROM billing_operations "
        "WHERE strpos(funding_json,%s)>0",
        (source.lot_id,),
    ).fetchall()
    held, consumed = source_commitments(
        operations,
        lot_id=source.lot_id,
        source_credits=source.credits,
        source_amount_fen=Decimal(source.amount_fen),
    )
    previous = conn.execute(
        "SELECT COALESCE(SUM(credits),0) FROM admin_refund_allocations WHERE credit_lot_id=%s",
        (source.lot_id,),
    ).fetchone()
    if (
        previous is None
        or held > int(wallet[1])
        or source.lot_remaining + held + consumed + int(previous[0]) != source.credits
    ):
        _unreconciled()
    if credits > source.lot_remaining or credits > int(wallet[0]):
        raise http_error(
            422,
            "REFUND_SOURCE_BALANCE_INSUFFICIENT",
            "扣减超过该原充值尚未使用、未预留的积分，已暂停本次扣减，请转人工核对。",
        )
    updated = conn.execute(
        "UPDATE billing_credit_lots SET remaining_credits=remaining_credits-%s "
        "WHERE id=%s AND remaining_credits=%s AND remaining_credits>=%s",
        (credits, source.lot_id, source.lot_remaining, credits),
    )
    if updated.rowcount != 1:
        _unreconciled()
    return RefundFunding(
        source.order_id,
        order_no,
        source.lot_id,
        credits,
        source.credits,
        Decimal(source.amount_fen),
        source.lot_remaining,
        source.lot_remaining - credits,
    )


def record_refund_allocation(
    conn: psycopg.Connection, *, adjustment_id: str, funding: RefundFunding
) -> None:
    conn.execute(
        "INSERT INTO admin_refund_allocations "
        "(adjustment_id,refund_transaction_id,original_recharge_order_id,credit_lot_id,credits,"
        "source_credits,source_amount_fen,lot_remaining_before,lot_remaining_after) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (
            adjustment_id,
            "admin_adjustment:refund:" + adjustment_id,
            funding.original_recharge_order_id,
            funding.credit_lot_id,
            funding.credits,
            funding.source_credits,
            funding.source_amount_fen,
            funding.lot_remaining_before,
            funding.lot_remaining_after,
        ),
    )
