"""待支付充值订单的兜底 sweep（审计修复 2026-11-15，模块 M3）。

背景：查单补账与过期关闭的 CLI（reconcile_pending_native_orders /
close_expired_native_orders）挂在 systemd maintenance timer 上，一天只跑一次；
而生产是 docker compose 部署，这些 timer 根本不存在——2026-11-15 实锤 34 笔
待支付订单（含 ¥2980）挂 8 天无人处理，管理员只能手动「查单补单」。

本模块把双 provider 的兜底聚合成一次可周期调用的 sweep，供 worker 常驻
循环按节流周期驱动（compose 部署由此获得与 systemd 等价的兜底）：

1. wechat_native 掉单补账：问微信每笔过期宽限的 PENDING 单，已付的走
   confirm_recharge_payment 幂等入账（reconcile_stale_pending_native_orders）；
2. zpay 掉单补账：同构（reconcile_stale_pending_zpay_orders，带商户通道
   白名单结算）；
3. 过期关闭：**只关本轮确认"网关说没付"的单**（close_lapsed_orders_by_no）。
   绝不使用全量 close_expired_*——那会把查单失败或没轮到查的单（可能是
   已付款的掉单）关在门外：reconcile 只看 PENDING、管理端补单只收 PENDING，
   一旦 CLOSED 那笔钱就永远失去补账机会（评审 P1 实锤）。查单失败的单留在
   PENDING 由下一轮 sweep 重试，运营在队列里始终可见。

连接契约：调用方传入 autocommit 连接（每个结算/关闭各自成为短事务），
外呼 query_order 期间绝不持有任何事务或行锁；微信回调与 sweep 双跑是
PAID 短路，永远不会双入账。供应商闭包传 None 表示该通道未配置，对账段
整段跳过（该通道自然也没有可关的单），不报错。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from app.db_portable import BusinessConnection
from app.wechat_native_client import (
    NATIVE_ORDER_VALIDITY_SECONDS,
    NATIVE_RECONCILIATION_GRACE_SECONDS,
    WeChatOrderQueryResult,
)
from app.zpay import ZPayOrderQueryResult
from app.zpay_payments import (
    ZPAY_ORDER_VALIDITY_SECONDS,
    NativeReconciliationOutcome,
    close_lapsed_orders_by_no,
    reconcile_stale_pending_native_orders,
    reconcile_stale_pending_zpay_orders,
)

logger = logging.getLogger(__name__)

# 经此模块对外提供的查询闭包签名：给单号，回供应商应答（或抛错——单笔失败
# 只跳过本单，由 reconcile 实现各自计数）。
WeChatQuery = Callable[[str], WeChatOrderQueryResult]
ZpayQuery = Callable[[str], ZPayOrderQueryResult]


@dataclass(frozen=True)
class RechargeSweepOutcome:
    """一次 sweep 的聚合计数；两个 reconcile 分量保留供日志细分。"""

    queried: int
    settled: int
    unpaid: int
    skipped: int
    closed: int
    native: NativeReconciliationOutcome
    zpay: NativeReconciliationOutcome


def sweep_pending_recharge_orders(
    conn: BusinessConnection,
    *,
    wechat_query_order: WeChatQuery | None,
    zpay_query_order: ZpayQuery | None,
    reconcile_grace_seconds: int = NATIVE_RECONCILIATION_GRACE_SECONDS,
    native_validity_seconds: int = NATIVE_ORDER_VALIDITY_SECONDS,
    zpay_validity_seconds: int = ZPAY_ORDER_VALIDITY_SECONDS,
    zpay_allowed_channels: tuple[str, ...] | None = None,
    limit: int = 200,
) -> RechargeSweepOutcome:
    """跑一遍完整的待支付订单兜底，返回聚合计数（只打印用，无敏感字段）。"""
    noop = NativeReconciliationOutcome(queried=0, settled=0, unpaid=0, skipped=0)
    if wechat_query_order is not None:
        native = reconcile_stale_pending_native_orders(
            conn,
            query_order=wechat_query_order,
            grace_seconds=reconcile_grace_seconds,
            limit=limit,
        )
    else:
        native = noop
    if zpay_query_order is not None:
        zpay = reconcile_stale_pending_zpay_orders(
            conn,
            query_order=zpay_query_order,
            grace_seconds=reconcile_grace_seconds,
            limit=limit,
            allowed_channels=zpay_allowed_channels,
        )
    else:
        zpay = noop
    # 关闭放最后、且只针对本轮确认 unpaid 的单：先补账再关闭，同一轮里刚查过
    # "没付"且过了窗口的单按各自 provider 的窗口退休；查单失败/未查的单留在
    # PENDING 等下一轮（见模块 docstring 的评审教训）。
    closed = close_lapsed_orders_by_no(
        conn,
        provider="wechat_native",
        merchant_order_nos=native.unpaid_order_nos,
        validity_seconds=native_validity_seconds,
    ) + close_lapsed_orders_by_no(
        conn,
        provider="zpay",
        merchant_order_nos=zpay.unpaid_order_nos,
        validity_seconds=zpay_validity_seconds,
    )
    return RechargeSweepOutcome(
        queried=native.queried + zpay.queried,
        settled=native.settled + zpay.settled,
        unpaid=native.unpaid + zpay.unpaid,
        skipped=native.skipped + zpay.skipped,
        closed=closed,
        native=native,
        zpay=zpay,
    )
