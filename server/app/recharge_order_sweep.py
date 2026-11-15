"""待支付充值订单的兜底 sweep（审计修复 2026-11-15，模块 M3）。

背景：查单补账与过期关闭的 CLI（reconcile_pending_native_orders /
close_expired_native_orders）挂在 systemd maintenance timer 上，一天只跑一次；
而生产是 docker compose 部署，这些 timer 根本不存在——2026-11-15 实锤 34 笔
待支付订单（含 ¥2980）挂 8 天无人处理，管理员只能手动「查单补单」。

本模块把四个既有动作聚合成一次可周期调用的 sweep，供 worker 常驻循环按
节流周期驱动（compose 部署由此获得与 systemd 等价的兜底）：

1. wechat_native 掉单补账：问微信每笔过期宽限的 PENDING 单，已付的走
   confirm_recharge_payment 幂等入账（reconcile_stale_pending_native_orders）；
2. zpay 掉单补账：同构（reconcile_stale_pending_zpay_orders）；
3. wechat_native 过期关闭：付款窗口过后 PENDING → CLOSED
   （close_expired_native_orders）；
4. zpay 过期关闭：同构（close_expired_zpay_orders）。

连接契约：调用方传入 autocommit 连接（每个结算/关闭各自成为短事务），
外呼 query_order 期间绝不持有任何事务或行锁；微信回调与 sweep 双跑是
PAID 短路，永远不会双入账。供应商闭包传 None 表示该通道未配置，整段
跳过（只在 skipped 里报数，不报错）。
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
    close_expired_native_orders,
    close_expired_zpay_orders,
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
    """一次 sweep 的聚合计数；四个 reconcile 分量保留供日志细分。"""

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
        )
    else:
        zpay = noop
    # 关闭放最后：先补账再关闭，同一次 sweep 内刚查过"没付"的单立刻按窗口
    # 退休，不会把已付单关在门外（结算守卫对 CLOSED 仍放行，双保险）。
    closed = close_expired_native_orders(
        conn, limit=limit, validity_seconds=native_validity_seconds
    ) + close_expired_zpay_orders(conn, limit=limit, validity_seconds=zpay_validity_seconds)
    return RechargeSweepOutcome(
        queried=native.queried + zpay.queried,
        settled=native.settled + zpay.settled,
        unpaid=native.unpaid + zpay.unpaid,
        skipped=native.skipped + zpay.skipped,
        closed=closed,
        native=native,
        zpay=zpay,
    )
