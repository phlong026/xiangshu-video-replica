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
import math
import random
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
    CLOSED_RECONCILIATION_WINDOW_SECONDS,
    ZPAY_ORDER_VALIDITY_SECONDS,
    NativeReconciliationOutcome,
    close_lapsed_orders_by_no,
    count_closed_native_long_tail,
    count_closed_zpay_long_tail,
    count_recently_closed_native_orders,
    count_recently_closed_zpay_orders,
    count_stale_pending_native_orders,
    count_stale_pending_zpay_orders,
    reconcile_closed_native_long_tail,
    reconcile_closed_zpay_long_tail,
    reconcile_recently_closed_native_orders,
    reconcile_recently_closed_zpay_orders,
    reconcile_stale_pending_native_orders,
    reconcile_stale_pending_zpay_orders,
)

logger = logging.getLogger(__name__)

# 经此模块对外提供的查询闭包签名：给单号，回供应商应答（或抛错——单笔失败
# 只跳过本单，由 reconcile 实现各自计数）。zpay 侧收敛到专有应答类：
# provider 抽象返回的通用 OrderQueryResult 由调用方闭包显式适配成
# ZPayOrderQueryResult（字段逐个搬，静态检查全程有具体类型）。
WeChatQuery = Callable[[str], WeChatOrderQueryResult]
ZpayQuery = Callable[[str], ZPayOrderQueryResult]


@dataclass(frozen=True)
class RechargeSweepOutcome:
    """一次 sweep 的聚合计数；两个 reconcile 分量保留供日志细分。

    ``closed_native``/``closed_zpay`` 是 F3 的 CLOSED 高频扫描段（关单后仍
    可能被支付的掉单兜底）；``tail_native``/``tail_zpay`` 是慢频长尾段
    （高频窗出窗与 NULL 存量的承接，仅 ``long_tail=True`` 的轮次填充），
    字段结构与 PENDING 版一致，便于日志同构打印。
    """

    queried: int
    settled: int
    unpaid: int
    skipped: int
    closed: int
    native: NativeReconciliationOutcome
    zpay: NativeReconciliationOutcome
    closed_native: NativeReconciliationOutcome = NativeReconciliationOutcome(
        queried=0, settled=0, unpaid=0, skipped=0
    )
    closed_zpay: NativeReconciliationOutcome = NativeReconciliationOutcome(
        queried=0, settled=0, unpaid=0, skipped=0
    )
    tail_native: NativeReconciliationOutcome = NativeReconciliationOutcome(
        queried=0, settled=0, unpaid=0, skipped=0
    )
    tail_zpay: NativeReconciliationOutcome = NativeReconciliationOutcome(
        queried=0, settled=0, unpaid=0, skipped=0
    )


def sweep_pending_recharge_orders(
    conn: BusinessConnection,
    *,
    wechat_query_order: WeChatQuery | None,
    zpay_query_order: ZpayQuery | None,
    reconcile_grace_seconds: int = NATIVE_RECONCILIATION_GRACE_SECONDS,
    native_validity_seconds: int = NATIVE_ORDER_VALIDITY_SECONDS,
    zpay_validity_seconds: int = ZPAY_ORDER_VALIDITY_SECONDS,
    zpay_allowed_channels: tuple[str, ...] | None = None,
    closed_zpay_window_seconds: int = CLOSED_RECONCILIATION_WINDOW_SECONDS,
    long_tail: bool = False,
    limit: int = 200,
    rotation_index: int | None = None,
) -> RechargeSweepOutcome:
    """跑一遍完整的待支付订单兜底，返回聚合计数（只打印用，无敏感字段）。

    ``rotation_index`` 是轮转游标：候选按 created_at 分页，每轮取第
    rotation_index % pages 页。固定取最老一批会让持续查单失败的单饿死
    后面的掉单（评审 P1）。缺省用随机页——随机轮转无状态、不会与调度节奏
    走样（时间分桶在慢调度下会恒定命中同一页，评审 P1 二轮），持续饿死
    某单的概率随轮数指数衰减；测试传固定值以保证可复现。

    F3 追加 CLOSED 扫描段：PENDING 补账与关单之后，对窗内（关单时间在各自
    窗口内）的 CLOSED 订单继续问网关——关单不是「不可支付」的证明（zpay 无
    网关过期、微信有查单/关单竞态与客户删除后 2h 窗口）。微信窗=关单后
    validity+grace（再远 time_expire 已退休二维码）；zpay 窗=7 天高频重试层。
    ``long_tail=True`` 时**追加**慢频长尾段：该通道**全部** CLOSED 单（含高频
    窗出窗的与迁移前 closed_at IS NULL 存量，无时间上界——到期前的历史支付
    查单永远可查，二轮评审 P1 实锤时间窗盲区）持续被发现。高频窗出窗不是
    资金责任截止线。CLOSED 段没有「确认未付就关」的下游——订单已经是
    CLOSED。
    """
    if rotation_index is None:
        rotation_index = random.randrange(1 << 30)
    noop = NativeReconciliationOutcome(queried=0, settled=0, unpaid=0, skipped=0)
    if wechat_query_order is not None:
        native_total = count_stale_pending_native_orders(
            conn, grace_seconds=reconcile_grace_seconds
        )
        native_pages = max(1, math.ceil(native_total / limit))
        native_offset = (max(0, rotation_index) % native_pages) * limit
        native = reconcile_stale_pending_native_orders(
            conn,
            query_order=wechat_query_order,
            grace_seconds=reconcile_grace_seconds,
            limit=limit,
            offset=native_offset,
        )
    else:
        native = noop
    if zpay_query_order is not None:
        zpay_total = count_stale_pending_zpay_orders(conn, grace_seconds=reconcile_grace_seconds)
        zpay_pages = max(1, math.ceil(zpay_total / limit))
        zpay_offset = (max(0, rotation_index) % zpay_pages) * limit
        zpay = reconcile_stale_pending_zpay_orders(
            conn,
            query_order=zpay_query_order,
            grace_seconds=reconcile_grace_seconds,
            limit=limit,
            allowed_channels=zpay_allowed_channels,
            offset=zpay_offset,
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
    # F3 的 CLOSED 扫描段放在关闭之后：刚退休的单立刻进入窗内候选，竞态掉单
    # （查单 NOTPAY → 到期前付款 → 关单）在下一轮 sweep 就能被捞回。轮转游标
    # 与 PENDING 段共用一个随机源（各自独立取页），防饿死语义一致。
    noop_outcome = NativeReconciliationOutcome(queried=0, settled=0, unpaid=0, skipped=0)
    if wechat_query_order is not None:
        closed_native_total = count_recently_closed_native_orders(conn)
        closed_native_pages = max(1, math.ceil(closed_native_total / limit))
        closed_native_offset = (max(0, rotation_index) % closed_native_pages) * limit
        closed_native = reconcile_recently_closed_native_orders(
            conn,
            query_order=wechat_query_order,
            limit=limit,
            offset=closed_native_offset,
        )
    else:
        closed_native = noop_outcome
    if zpay_query_order is not None:
        closed_zpay_total = count_recently_closed_zpay_orders(
            conn, window_seconds=closed_zpay_window_seconds
        )
        closed_zpay_pages = max(1, math.ceil(closed_zpay_total / limit))
        closed_zpay_offset = (max(0, rotation_index) % closed_zpay_pages) * limit
        closed_zpay = reconcile_recently_closed_zpay_orders(
            conn,
            query_order=zpay_query_order,
            window_seconds=closed_zpay_window_seconds,
            limit=limit,
            allowed_channels=zpay_allowed_channels,
            offset=closed_zpay_offset,
        )
    else:
        closed_zpay = noop_outcome
    # 长尾段（慢频轮次才跑）：候选=该通道全部 CLOSED 单（无时间上界——到期前
    # 的历史支付查单永远可查，二轮评审 P1 实锤时间窗盲区），小限额轮转控制
    # 网关配额；轮转游标同源，防饿死语义一致。
    if long_tail and wechat_query_order is not None:
        tail_native_total = count_closed_native_long_tail(conn)
        tail_native_pages = max(1, math.ceil(tail_native_total / limit))
        tail_native_offset = (max(0, rotation_index) % tail_native_pages) * limit
        tail_native = reconcile_closed_native_long_tail(
            conn,
            query_order=wechat_query_order,
            limit=limit,
            offset=tail_native_offset,
        )
    else:
        tail_native = noop_outcome
    if long_tail and zpay_query_order is not None:
        tail_zpay_total = count_closed_zpay_long_tail(conn)
        tail_zpay_pages = max(1, math.ceil(tail_zpay_total / limit))
        tail_zpay_offset = (max(0, rotation_index) % tail_zpay_pages) * limit
        tail_zpay = reconcile_closed_zpay_long_tail(
            conn,
            query_order=zpay_query_order,
            limit=limit,
            allowed_channels=zpay_allowed_channels,
            offset=tail_zpay_offset,
        )
    else:
        tail_zpay = noop_outcome
    return RechargeSweepOutcome(
        queried=native.queried + zpay.queried,
        settled=native.settled + zpay.settled,
        unpaid=native.unpaid + zpay.unpaid,
        skipped=native.skipped + zpay.skipped,
        closed=closed,
        native=native,
        zpay=zpay,
        closed_native=closed_native,
        closed_zpay=closed_zpay,
        tail_native=tail_native,
        tail_zpay=tail_zpay,
    )
