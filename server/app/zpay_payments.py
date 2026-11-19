from __future__ import annotations

import logging
from collections.abc import Callable, Collection
from dataclasses import dataclass
from typing import Literal, TypedDict, cast
from uuid import uuid4

from app.db_portable import BusinessConnection, BusinessRow, IntegrityConstraintError
from app.payment_provider import OrderQueryError
from app.recharge_packages import grant_discount_from_snapshot, parse_package_snapshot
from app.wechat_native_client import (
    NATIVE_MERCHANT_SWITCH_BLOCK_SECONDS,
    NATIVE_ORDER_VALIDITY_SECONDS,
    NATIVE_RECONCILIATION_GRACE_SECONDS,
    WeChatOrderQueryResult,
)
from app.zpay import ALLOWED_ZPAY_CHANNELS, ZPayOrderQueryError, ZPayOrderQueryResult

logger = logging.getLogger(__name__)

ZPAY_NOTIFY_BUSY_TIMEOUT_MS = 1000
RechargeStatus = Literal["PENDING", "PAID", "FAILED", "CLOSED"]

# ZPay 表单不带 time_expire，二维码本身长期可扫；但放着一笔没人付也没人问的
# PENDING 订单挂在列表和对账报表里没有任何价值。24 小时后由 sweep 关闭——
# 关闭只是记账动作，迟到支付仍走 confirm_recharge_payment 的幂等入账。
ZPAY_ORDER_VALIDITY_SECONDS = 24 * 60 * 60


@dataclass(frozen=True)
class SettlementProviderSpec:
    """Provider-specific knobs for the single shared recharge settlement routine.

    ``confirm_recharge_payment`` is the one idempotent settlement path for every
    provider; this spec carries the only per-provider variation so the fund logic
    never forks. ``trade_no_column`` names the ``recharge_orders`` column that stores
    the provider trade reference: ZPay uses ``provider_trade_no`` while WeChat Native
    uses ``transaction_id`` (migration 083 forces a wechat_native order's
    ``provider_trade_no`` to stay NULL and its ``transaction_id`` to be NOT NULL once
    PAID). The column name is a code-controlled constant, never user input.
    """

    provider_name: str
    provider_label: str
    error_prefix: str
    channel_universe: Collection[str]
    trade_no_column: str


# WeChat Native only ever settles the wxpay channel; kept local so this fund module
# does not import the provider module (avoids an import cycle at registration time).
_WECHAT_NATIVE_CHANNEL = "wxpay"
_WECHAT_NATIVE_CHANNEL_UNIVERSE = frozenset({_WECHAT_NATIVE_CHANNEL})

ZPAY_SETTLEMENT_SPEC = SettlementProviderSpec(
    provider_name="zpay",
    provider_label="ZPay",
    error_prefix="ZPAY",
    channel_universe=ALLOWED_ZPAY_CHANNELS,
    trade_no_column="provider_trade_no",
)
WECHAT_NATIVE_SETTLEMENT_SPEC = SettlementProviderSpec(
    provider_name="wechat_native",
    provider_label="WeChat Pay",
    error_prefix="WECHAT",
    channel_universe=_WECHAT_NATIVE_CHANNEL_UNIVERSE,
    trade_no_column="transaction_id",
)

# Allowlist guard for the column identifier interpolated into settlement SQL. It only
# ever comes from the frozen specs above, but a fund path validates the identifier it
# splices rather than trusting the call site.
_SETTLEMENT_TRADE_COLUMNS = frozenset({"provider_trade_no", "transaction_id"})

# 交易号绑定的数据库唯一兜底（022 的 zpay 泳道、20260924T0000 的 wechat 泳道）。
# 命中其一即等价于 bound_order 预检查失败，映射回 TRADE_ALREADY_BOUND 而非笼统的
# SETTLEMENT_CONFLICT。
_TRADE_BOUND_UNIQUE_INDEXES = frozenset(
    {"uq_recharge_orders_provider_trade_no", "uq_recharge_orders_wechat_transaction_id"}
)


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
    conn: BusinessConnection,
    *,
    merchant_order_no: str,
) -> BusinessRow | None:
    return cast(
        BusinessRow | None,
        conn.execute(
            """
            SELECT
                id, user_id, merchant_order_no, provider, provider_trade_no, channel, status,
                amount_fen, credits, notify_digest, created_at, paid_at
            FROM recharge_orders
            WHERE merchant_order_no = %s
            """,
            (merchant_order_no,),
        ).fetchone(),
    )


def _read_settlement_order(
    conn: BusinessConnection, *, merchant_order_no: str, trade_no_column: str
) -> BusinessRow | None:
    """Read a recharge order for settlement, aliasing the trade reference column.

    Same projection as ``read_recharge_order`` but the provider trade reference is
    selected from ``trade_no_column`` (``provider_trade_no`` for ZPay,
    ``transaction_id`` for WeChat Native) under the fixed alias ``trade_ref`` so the
    settlement routine stays column-agnostic. The public ``read_recharge_order`` keeps
    its ZPay-shaped ``provider_trade_no`` projection for the manual-sync route.
    """
    if trade_no_column not in _SETTLEMENT_TRADE_COLUMNS:
        raise ValueError(f"Unsupported settlement trade column: {trade_no_column}")
    return cast(
        BusinessRow | None,
        conn.execute(
            f"""
            SELECT
                id, user_id, merchant_order_no, provider, channel, status,
                amount_fen, credits, notify_digest, created_at, paid_at,
                package_snapshot_json,
                {trade_no_column} AS trade_ref
            FROM recharge_orders
            WHERE merchant_order_no = %s
            FOR UPDATE
            """,
            (merchant_order_no,),
        ).fetchone(),
    )


def serialize_recharge_order(row: BusinessRow) -> RechargeOrderData:
    return {
        "order_no": str(row["merchant_order_no"]),
        "status": cast(RechargeStatus, str(row["status"])),
        "amount_fen": int(row["amount_fen"]),
        "credits": int(row["credits"]),
        "channel": str(row["channel"]),
        "created_at": str(row["created_at"]),
        "paid_at": None if row["paid_at"] is None else str(row["paid_at"]),
    }


# "PENDING and older than N seconds" — the shared shape behind the expiry sweep
# (N = the payment window) and the reconciliation sweep (N = the callback grace).
_PENDING_NATIVE_OLDER_THAN = (
    "provider = 'wechat_native' AND status = 'PENDING' "
    "AND created_at::timestamptz + make_interval(secs => %s) <= now()"
)


def count_expired_native_orders(
    conn: BusinessConnection, *, validity_seconds: int = NATIVE_ORDER_VALIDITY_SECONDS
) -> int:
    """How many Native orders have outlived their payment window while still PENDING."""
    row = conn.execute(
        f"SELECT COUNT(*) FROM recharge_orders WHERE {_PENDING_NATIVE_OLDER_THAN}",  # noqa: S608
        (float(validity_seconds),),
    ).fetchone()
    assert row is not None
    return int(row[0])


def close_expired_native_orders(
    conn: BusinessConnection,
    *,
    limit: int,
    validity_seconds: int = NATIVE_ORDER_VALIDITY_SECONDS,
) -> int:
    """Retire Native orders nobody paid before their window lapsed; return the count.

    ``CLOSED`` is the same terminal state a customer-closed order reaches, and
    ``confirm_recharge_payment`` still settles from it, so a notification that
    arrives late — WeChat retries for hours — still credits the wallet. The sweep
    only stops orders that can no longer be paid from sitting in the orders page
    and the reconciliation reports as PENDING forever.

    It deliberately does *not* relax the merchant-identity guard in
    ``SettingsRepository.save_wechat_native_config`` or the credit-conversion
    check: both count CLOSED as well, precisely because a CLOSED order can still
    settle against the current merchant. Narrowing either one needs a decision
    about how long after expiry WeChat may still push a notification.

    ``SKIP LOCKED`` leaves any order a callback is currently settling to that
    callback rather than waiting on its lock.
    """
    updated = conn.execute(
        f"""
        UPDATE recharge_orders SET status = 'CLOSED', closed_at = CURRENT_TIMESTAMP
        WHERE id IN (
            SELECT id FROM recharge_orders
            WHERE {_PENDING_NATIVE_OLDER_THAN}
            ORDER BY created_at
            LIMIT %s
            FOR UPDATE SKIP LOCKED
        )
        """,  # noqa: S608
        (float(validity_seconds), limit),
    )
    return int(updated.rowcount)


def count_stale_pending_native_orders(
    conn: BusinessConnection, *, grace_seconds: int = NATIVE_RECONCILIATION_GRACE_SECONDS
) -> int:
    """How many Native orders have sat PENDING past the callback grace.

    The reconciliation sweep's backlog: same shape as the expiry predicate, but
    the window is the much shorter grace after which the callback path alone is
    no longer trusted.
    """
    row = conn.execute(
        f"SELECT COUNT(*) FROM recharge_orders WHERE {_PENDING_NATIVE_OLDER_THAN}",  # noqa: S608
        (float(grace_seconds),),
    ).fetchone()
    assert row is not None
    return int(row[0])


_MERCHANT_SWITCH_BLOCKING_NATIVE_PREDICATE = (
    "provider = 'wechat_native' AND status IN ('PENDING', 'CLOSED') "
    "AND created_at::timestamptz + make_interval(secs => %s) > now()"
)


def count_blocking_native_orders(
    conn: BusinessConnection, *, block_seconds: int = NATIVE_MERCHANT_SWITCH_BLOCK_SECONDS
) -> int:
    """Native orders still inside their payment-plus-callback window.

    The settlement-drain criterion behind the merchant-identity guard: an order
    blocks a merchant change only while it can still become PAID — payable QR
    until the window ends (``NATIVE_ORDER_VALIDITY_SECONDS``), retrying
    callbacks for roughly a day after that (``NATIVE_SETTLEMENT_DRAIN_SECONDS``).
    Past the horizon nothing can land anymore, so the order stops blocking and
    a merchant identity can always be changed in finite time.
    """
    row = conn.execute(
        f"SELECT COUNT(*) FROM recharge_orders WHERE {_MERCHANT_SWITCH_BLOCKING_NATIVE_PREDICATE}",  # noqa: S608
        (float(block_seconds),),
    ).fetchone()
    assert row is not None
    return int(row[0])


def list_stale_pending_native_orders(
    conn: BusinessConnection, *, grace_seconds: int, limit: int, offset: int = 0
) -> list[str]:
    """Order numbers of the oldest Native orders still PENDING past the grace.

    Read-only and lock-free on purpose: the caller must have no transaction open
    that would span the external query-order calls, so a concurrently arriving
    callback never waits on the sweep's snapshot. ``offset`` 供 sweep 轮转游标
    使用：固定取最老一批会让持续查单失败的单饿死后面的掉单。
    """
    rows = conn.execute(
        f"""
        SELECT merchant_order_no FROM recharge_orders
        WHERE {_PENDING_NATIVE_OLDER_THAN}
        ORDER BY created_at
        LIMIT %s OFFSET %s
        """,  # noqa: S608
        (float(grace_seconds), limit, max(0, offset)),
    ).fetchall()
    return [str(row[0]) for row in rows]


def count_stale_pending_zpay_orders(conn: BusinessConnection, *, grace_seconds: int) -> int:
    """Backlog size of the ZPay reconciliation sweep (rotation page count)."""
    row = conn.execute(
        f"SELECT COUNT(*) FROM recharge_orders WHERE {_PENDING_ZPAY_OLDER_THAN}",
        (float(grace_seconds),),
    ).fetchone()
    assert row is not None
    return int(row[0])


@dataclass(frozen=True)
class NativeReconciliationOutcome:
    """Counts-only result of one reconciliation sweep pass (the CLI prints these).

    ``unpaid_order_nos`` 带出本轮确认"网关说没付"的单号：过期关闭必须只关
    这些单。查单失败或没轮到查的单若被关闭，reconcile 只看 PENDING、管理端
    补单只收 PENDING，那笔可能已付的钱就永远失去补账机会（2026-11-15 审计
    M3 评审发现）。
    """

    queried: int
    settled: int
    unpaid: int
    skipped: int
    unpaid_order_nos: tuple[str, ...] = ()
    rate_limited: bool = False


def settle_wechat_order_from_verified_query(
    conn: BusinessConnection,
    *,
    order_no: str,
    paid: bool,
    transaction_id: str | None,
    amount_fen: int | None,
    source_digest: str,
) -> Literal["settled", "unpaid", "skipped"]:
    """把一条**已验签**的微信查单应答落到资金上（幂等，2026-11-16 F2/F3 共用）.

    客户删除订单的关单后补结算与 sweep 的 CLOSED 扫描段共用这一个入口：
    只有应答确认已付且单号/凭证完整才走 ``confirm_recharge_payment``（全套
    资金防线：商户/金额/渠道白名单/交易号绑定/PAID 短路原样生效）。错误
    文本永不作为入账证据——结算拒绝（PaymentConfirmationError）按 skipped
    处理并告警，不向上抛，避免一笔问题单拖垮调用方流程。
    """
    if not paid:
        return "unpaid"
    if transaction_id is None or amount_fen is None:
        logger.warning(
            "wechat query answer for order %s is paid but incomplete; settling nothing",
            order_no,
        )
        return "skipped"
    try:
        confirm_recharge_payment(
            conn,
            merchant_order_no=order_no,
            provider_trade_no=transaction_id,
            amount_fen=amount_fen,
            channel=_WECHAT_NATIVE_CHANNEL,
            source_digest=source_digest,
            provider_spec=WECHAT_NATIVE_SETTLEMENT_SPEC,
        )
    except PaymentConfirmationError as exc:
        logger.warning("refused to settle order %s from verified query: %s", order_no, exc.code)
        return "skipped"
    return "settled"


def reconcile_stale_pending_native_orders(
    conn: BusinessConnection,
    *,
    query_order: Callable[[str], WeChatOrderQueryResult],
    grace_seconds: int = NATIVE_RECONCILIATION_GRACE_SECONDS,
    limit: int = 200,
    offset: int = 0,
) -> NativeReconciliationOutcome:
    """Recover Native orders WeChat has paid but no callback ever confirmed.

    掉单兜底 — the lost-order fallback. The customer polls only our own database
    and WeChat retries a failed notification for hours at most, so a notify
    route that was down means money arrived that nothing on our side credits.
    The sweep asks WeChat about each stale PENDING order and settles the paid
    ones through ``confirm_recharge_payment`` — the same idempotent fund path,
    spec and guards as the callback — so a sweep racing a late callback is a
    PAID short-circuit, never a double credit.

    ``query_order`` performs the external call, so settlement row locks must
    never be held across it: the CLI runs the sweep on an autocommit connection
    (each settlement below is then its own short transaction), while tests may
    drive the whole pass inside one outer transaction. A query that fails,
    answers for a different order, or is refused by the settlement guards is
    skipped with a warning and retried on the next run; an unpaid answer stays
    PENDING and its order number is reported via ``unpaid_order_nos``——过期
    关闭只允许针对这些"确认没付"的单（见 NativeReconciliationOutcome）。
    """
    order_nos = list_stale_pending_native_orders(
        conn, grace_seconds=grace_seconds, limit=limit, offset=offset
    )
    queried = settled = unpaid = skipped = 0
    unpaid_order_nos: list[str] = []
    for order_no in order_nos:
        try:
            answer = query_order(order_no)
        except Exception as exc:  # noqa: BLE001 - one dead order must not strand the rest
            logger.warning(
                "reconciliation query for order %s failed (%s); it stays PENDING",
                order_no,
                exc,
            )
            skipped += 1
            continue
        queried += 1
        if answer.out_trade_no != order_no:
            logger.warning(
                "reconciliation query answered for %s while asking about %s; settling nothing",
                answer.out_trade_no,
                order_no,
            )
            skipped += 1
            continue
        if not answer.paid:
            unpaid += 1
            unpaid_order_nos.append(order_no)
            continue
        if answer.transaction_id is None or answer.amount_fen is None:
            logger.warning(
                "reconciliation query for order %s is paid but incomplete; settling nothing",
                order_no,
            )
            skipped += 1
            continue
        try:
            confirm_recharge_payment(
                conn,
                merchant_order_no=order_no,
                provider_trade_no=answer.transaction_id,
                amount_fen=answer.amount_fen,
                channel=_WECHAT_NATIVE_CHANNEL,
                source_digest=answer.response_digest,
                provider_spec=WECHAT_NATIVE_SETTLEMENT_SPEC,
            )
        except PaymentConfirmationError as exc:
            logger.warning(
                "reconciliation refused to settle order %s: %s; it stays PENDING",
                order_no,
                exc.code,
            )
            skipped += 1
            continue
        settled += 1
    return NativeReconciliationOutcome(
        queried=queried,
        settled=settled,
        unpaid=unpaid,
        skipped=skipped,
        unpaid_order_nos=tuple(unpaid_order_nos),
    )


# ZPay 侧的对账/过期兜底与 WeChat Native 完全同构（2026-11-15 审计 M3：生产
# 上 systemd maintenance timer 不在 compose 部署里跑，挂单 8 天无人处理；
# sweep 进 worker 常驻循环后两个 provider 一并覆盖）。计数结构沿用
# NativeReconciliationOutcome——它只是四个 int。
_PENDING_ZPAY_OLDER_THAN = (
    "provider = 'zpay' AND status = 'PENDING' "
    "AND created_at::timestamptz + make_interval(secs => %s) <= now()"
)


def list_stale_pending_zpay_orders(
    conn: BusinessConnection, *, grace_seconds: int, limit: int, offset: int = 0
) -> list[str]:
    """Order numbers of the oldest ZPay orders still PENDING past the grace.

    Read-only and lock-free on purpose, same as the Native twin: the caller must
    have no transaction open that would span the external query-order call.
    ``offset`` 同 Native 版——sweep 轮转游标防饿死。
    """
    rows = conn.execute(
        f"""
        SELECT merchant_order_no FROM recharge_orders
        WHERE {_PENDING_ZPAY_OLDER_THAN}
        ORDER BY created_at
        LIMIT %s OFFSET %s
        """,  # noqa: S608
        (float(grace_seconds), limit, max(0, offset)),
    ).fetchall()
    return [str(row[0]) for row in rows]


def close_expired_zpay_orders(
    conn: BusinessConnection,
    *,
    limit: int,
    validity_seconds: int = ZPAY_ORDER_VALIDITY_SECONDS,
) -> int:
    """Retire ZPay orders nobody paid within their window; return the count.

    Same semantics as the Native expiry sweep: CLOSED stays settleable for late
    payments, ``SKIP LOCKED`` leaves orders a callback is currently settling to
    that callback.
    """
    updated = conn.execute(
        f"""
        UPDATE recharge_orders SET status = 'CLOSED', closed_at = CURRENT_TIMESTAMP
        WHERE id IN (
            SELECT id FROM recharge_orders
            WHERE {_PENDING_ZPAY_OLDER_THAN}
            ORDER BY created_at
            LIMIT %s
            FOR UPDATE SKIP LOCKED
        )
        """,  # noqa: S608
        (float(validity_seconds), limit),
    )
    return int(updated.rowcount)


def reconcile_stale_pending_zpay_orders(
    conn: BusinessConnection,
    *,
    query_order: Callable[[str], ZPayOrderQueryResult],
    grace_seconds: int,
    limit: int = 200,
    allowed_channels: Collection[str] | None = None,
    offset: int = 0,
) -> NativeReconciliationOutcome:
    """Recover ZPay orders the gateway has marked paid but no callback confirmed.

    与 Native 版逐语义对齐：paid 且字段齐全才走 ``confirm_recharge_payment``
    （ZPAY_SETTLEMENT_SPEC，凭证落 provider_trade_no 列）；查不到/unpaid/
    守卫拒绝分别计数跳过，绝不代答。连接契约也一致——调用方用 autocommit
    连接，结算各自成为短事务，外呼期间不持任何锁。
    """
    order_nos = list_stale_pending_zpay_orders(
        conn, grace_seconds=grace_seconds, limit=limit, offset=offset
    )
    queried = settled = unpaid = skipped = 0
    unpaid_order_nos: list[str] = []
    rate_limited = False
    for order_no in order_nos:
        try:
            answer = query_order(order_no)
        except Exception as exc:  # noqa: BLE001 - one dead order must not strand the rest
            skipped += 1
            if isinstance(exc, (OrderQueryError, ZPayOrderQueryError)) and exc.status_code == 429:
                logger.warning("Recharge query rate limited; deferring remaining channel queries.")
                rate_limited = True
                break
            logger.warning(
                "Recharge reconciliation query for order %s failed (%s); it stays PENDING",
                order_no,
                type(exc).__name__,
            )
            continue
        queried += 1
        if answer.merchant_order_no != order_no:
            logger.warning(
                "zpay reconciliation query answered for %s while asking about %s; settling nothing",
                answer.merchant_order_no,
                order_no,
            )
            skipped += 1
            continue
        if not answer.paid:
            unpaid += 1
            unpaid_order_nos.append(order_no)
            continue
        if answer.provider_trade_no is None or answer.amount_fen is None or answer.channel is None:
            logger.warning(
                "zpay reconciliation query for order %s is paid but incomplete; settling nothing",
                order_no,
            )
            skipped += 1
            continue
        try:
            confirm_recharge_payment(
                conn,
                merchant_order_no=order_no,
                provider_trade_no=answer.provider_trade_no,
                amount_fen=answer.amount_fen,
                channel=answer.channel,
                source_digest=answer.response_digest,
                allowed_channels=allowed_channels,
                provider_spec=ZPAY_SETTLEMENT_SPEC,
            )
        except PaymentConfirmationError as exc:
            logger.warning(
                "zpay reconciliation refused to settle order %s: %s; it stays PENDING",
                order_no,
                exc.code,
            )
            skipped += 1
            continue
        settled += 1
    return NativeReconciliationOutcome(
        queried=queried,
        settled=settled,
        unpaid=unpaid,
        skipped=skipped,
        unpaid_order_nos=tuple(unpaid_order_nos),
        rate_limited=rate_limited,
    )


# ---------------------------------------------------------------------------
# F3（2026-11-16 漏账缺口修复）：CLOSED 订单的自动对账扫描段
# ---------------------------------------------------------------------------
# 关单不是「不可支付」的证明：zpay 网关无过期字段、微信存在查单/关单竞态与
# 客户删除后 2h 窗口——「关单后支付且回调丢失」此前无任何补账路径（人工
# 查单已由 F1 放开 CLOSED，这里是它的自动层）。两个通道的扫描窗口不同：
# 微信只扫码仍可能存活的窗口（关单后 validity+grace；再往后 time_expire 已
# 退休二维码，查单必然 unpaid）；zpay 无网关过期，给全窗（评审 Q2：7 天是
# 高频重试层，到期转待核对由低频长尾层/账单对账接手，不是资金责任截止线）。


CLOSED_RECONCILIATION_WINDOW_SECONDS = 7 * 24 * 60 * 60
# 长尾层（评审 P1 定案落地，二轮评审收敛为**无时间上界**）：高频窗出窗 ≠
# 资金责任截止。二维码在到期时退休，但「到期前的历史支付」查单永远答
# SUCCESS——worker 停机跨窗、zpay 第 8 天付款、迁移前 NULL 存量，任何时间
# 上界都会制造永久盲区（二轮评审 P1 实锤 181 天反例）。长尾候选=该通道的
# 全部 CLOSED 单（含 closed_at IS NULL 的历史存量，created_at 仅作分页锚点
# 不冒充关单时间）；无界集合靠慢频小限额轮转覆盖（sweep 的 long_tail 档），
# 体量随业务线性、单轮配额恒定。
_CLOSED_NATIVE_SCAN_PREDICATE = (
    "provider = 'wechat_native' AND status = 'CLOSED' AND closed_at IS NOT NULL "
    "AND closed_at::timestamptz + make_interval(secs => %s) > now()"
)
_CLOSED_ZPAY_SCAN_PREDICATE = (
    "provider = 'zpay' AND status = 'CLOSED' AND closed_at IS NOT NULL "
    "AND closed_at::timestamptz + make_interval(secs => %s) > now()"
)


def _native_closed_scan_window(*, window_seconds: int | None) -> int:
    """微信 CLOSED 扫描窗：缺省=关单后二维码的剩余可付窗（validity+grace）。"""
    if window_seconds is not None:
        return window_seconds
    return NATIVE_ORDER_VALIDITY_SECONDS + NATIVE_RECONCILIATION_GRACE_SECONDS


_CLOSED_LONG_TAIL_PREDICATE = "provider = %s AND status = 'CLOSED'"


def _count_closed_long_tail(conn: BusinessConnection, *, provider: str) -> int:
    row = conn.execute(
        f"SELECT COUNT(*) FROM recharge_orders WHERE {_CLOSED_LONG_TAIL_PREDICATE}",  # noqa: S608
        (provider,),
    ).fetchone()
    assert row is not None
    return int(row[0])


def _list_closed_long_tail(
    conn: BusinessConnection,
    *,
    provider: str,
    limit: int,
    offset: int,
) -> list[str]:
    rows = conn.execute(
        f"""
        SELECT merchant_order_no FROM recharge_orders
        WHERE {_CLOSED_LONG_TAIL_PREDICATE}
        ORDER BY closed_at NULLS FIRST, created_at
        LIMIT %s OFFSET %s
        """,  # noqa: S608
        (provider, limit, max(0, offset)),
    ).fetchall()
    return [str(row[0]) for row in rows]


def count_closed_native_long_tail(conn: BusinessConnection) -> int:
    return _count_closed_long_tail(conn, provider="wechat_native")


def count_closed_zpay_long_tail(conn: BusinessConnection) -> int:
    return _count_closed_long_tail(conn, provider="zpay")


def count_zpay_unresolved_orders(conn: BusinessConnection) -> dict[str, int]:
    """换绑影响面（F4）：zpay 渠道仍可能落到资金的订单计数.

    人工/自动查单都加载**当前**商户配置（换 pid 后旧单查单与回调验签一起
    失效，F1/F3 的恢复能力随之丢失），因此换绑前必须把影响面摆到台面上。
    两档**互斥**口径（评审二轮 P2：分档相加不得重复计数）：PENDING（未付，
    旧码仍可能被扫）与 CLOSED（全部关单——zpay 网关无过期、微信到期前的
    历史支付查单永远可查，不加时间窗，与长尾层同一口径）。同一事务快照内
    两次 COUNT，只读，供配置保存路由在拦截/审计时引用。
    """
    pending = conn.execute(
        "SELECT COUNT(*) FROM recharge_orders WHERE provider = 'zpay' AND status = 'PENDING'"
    ).fetchone()
    assert pending is not None
    closed = conn.execute(
        "SELECT COUNT(*) FROM recharge_orders WHERE provider = 'zpay' AND status = 'CLOSED'"
    ).fetchone()
    assert closed is not None
    return {"pending": int(pending[0]), "closed": int(closed[0])}


def count_recently_closed_native_orders(
    conn: BusinessConnection, *, window_seconds: int | None = None
) -> int:
    row = conn.execute(
        f"SELECT COUNT(*) FROM recharge_orders WHERE {_CLOSED_NATIVE_SCAN_PREDICATE}",  # noqa: S608
        (float(_native_closed_scan_window(window_seconds=window_seconds)),),
    ).fetchone()
    assert row is not None
    return int(row[0])


def list_recently_closed_native_orders(
    conn: BusinessConnection,
    *,
    window_seconds: int | None = None,
    limit: int,
    offset: int = 0,
) -> list[str]:
    rows = conn.execute(
        f"""
        SELECT merchant_order_no FROM recharge_orders
        WHERE {_CLOSED_NATIVE_SCAN_PREDICATE}
        ORDER BY closed_at
        LIMIT %s OFFSET %s
        """,  # noqa: S608
        (
            float(_native_closed_scan_window(window_seconds=window_seconds)),
            limit,
            max(0, offset),
        ),
    ).fetchall()
    return [str(row[0]) for row in rows]


def count_recently_closed_zpay_orders(
    conn: BusinessConnection, *, window_seconds: int = CLOSED_RECONCILIATION_WINDOW_SECONDS
) -> int:
    row = conn.execute(
        f"SELECT COUNT(*) FROM recharge_orders WHERE {_CLOSED_ZPAY_SCAN_PREDICATE}",  # noqa: S608
        (float(window_seconds),),
    ).fetchone()
    assert row is not None
    return int(row[0])


def list_recently_closed_zpay_orders(
    conn: BusinessConnection,
    *,
    window_seconds: int = CLOSED_RECONCILIATION_WINDOW_SECONDS,
    limit: int,
    offset: int = 0,
) -> list[str]:
    rows = conn.execute(
        f"""
        SELECT merchant_order_no FROM recharge_orders
        WHERE {_CLOSED_ZPAY_SCAN_PREDICATE}
        ORDER BY closed_at
        LIMIT %s OFFSET %s
        """,  # noqa: S608
        (float(window_seconds), limit, max(0, offset)),
    ).fetchall()
    return [str(row[0]) for row in rows]


def reconcile_recently_closed_native_orders(
    conn: BusinessConnection,
    *,
    query_order: Callable[[str], WeChatOrderQueryResult],
    window_seconds: int | None = None,
    limit: int = 200,
    offset: int = 0,
) -> NativeReconciliationOutcome:
    """扫 CLOSE 后仍可能被支付的微信单：已付即幂等入账，未付原样留在 CLOSED.

    与 PENDING 版的关键差异：这里没有「确认未付就关闭」的下游——订单已是
    CLOSED，未付应答只意味着下一轮继续问（窗内）。入账走
    :func:`settle_wechat_order_from_verified_query`（全套资金防线 + 错误文本
    不作结算证据）。连接契约同 PENDING 版：autocommit 连接，结算自开短事务，
    外呼期间不持任何锁。
    """
    return _reconcile_closed_native_order_nos(
        conn,
        order_nos=list_recently_closed_native_orders(
            conn, window_seconds=window_seconds, limit=limit, offset=offset
        ),
        query_order=query_order,
    )


def reconcile_closed_native_long_tail(
    conn: BusinessConnection,
    *,
    query_order: Callable[[str], WeChatOrderQueryResult],
    limit: int = 20,
    offset: int = 0,
) -> NativeReconciliationOutcome:
    """长尾层微信段：全部 CLOSED 微信单持续问网关（评审 P1，无时间上界）.

    到期前的历史支付查单永远答 SUCCESS，所以任何时间上界都是盲区；候选=
    全部 CLOSED 微信单（含 NULL 存量），逐单结算语义与高频段完全一致。调度
    上由 sweep 的慢频档驱动（worker 默认 30 分钟一档），每轮小限额轮转覆盖
    无界集合，网关配额恒定可控。
    """
    return _reconcile_closed_native_order_nos(
        conn,
        order_nos=_list_closed_long_tail(
            conn,
            provider="wechat_native",
            limit=limit,
            offset=offset,
        ),
        query_order=query_order,
    )


def _reconcile_closed_native_order_nos(
    conn: BusinessConnection,
    *,
    order_nos: list[str],
    query_order: Callable[[str], WeChatOrderQueryResult],
) -> NativeReconciliationOutcome:
    queried = settled = unpaid = skipped = 0
    unpaid_order_nos: list[str] = []
    for order_no in order_nos:
        try:
            answer = query_order(order_no)
        except Exception as exc:  # noqa: BLE001 — 一笔坏单不能拖垮整轮
            logger.warning(
                "closed-order query for %s failed (%s); it stays CLOSED",
                order_no,
                exc,
            )
            skipped += 1
            continue
        queried += 1
        if answer.out_trade_no != order_no:
            logger.warning(
                "closed-order query answered for %s while asking about %s; settling nothing",
                answer.out_trade_no,
                order_no,
            )
            skipped += 1
            continue
        outcome = settle_wechat_order_from_verified_query(
            conn,
            order_no=order_no,
            paid=answer.paid,
            transaction_id=answer.transaction_id,
            amount_fen=answer.amount_fen,
            source_digest=answer.response_digest,
        )
        if outcome == "settled":
            settled += 1
        elif outcome == "unpaid":
            unpaid += 1
            unpaid_order_nos.append(order_no)
        else:
            skipped += 1
    return NativeReconciliationOutcome(
        queried=queried,
        settled=settled,
        unpaid=unpaid,
        skipped=skipped,
        unpaid_order_nos=tuple(unpaid_order_nos),
    )


def reconcile_recently_closed_zpay_orders(
    conn: BusinessConnection,
    *,
    query_order: Callable[[str], ZPayOrderQueryResult],
    window_seconds: int = CLOSED_RECONCILIATION_WINDOW_SECONDS,
    limit: int = 200,
    allowed_channels: Collection[str] | None = None,
    offset: int = 0,
) -> NativeReconciliationOutcome:
    """zpay 的 CLOSED 扫描段（无网关过期 → 7 天全窗），与 Native 版逐语义对齐."""
    return _reconcile_closed_zpay_order_nos(
        conn,
        order_nos=list_recently_closed_zpay_orders(
            conn, window_seconds=window_seconds, limit=limit, offset=offset
        ),
        query_order=query_order,
        allowed_channels=allowed_channels,
    )


def reconcile_closed_zpay_long_tail(
    conn: BusinessConnection,
    *,
    query_order: Callable[[str], ZPayOrderQueryResult],
    limit: int = 20,
    allowed_channels: Collection[str] | None = None,
    offset: int = 0,
) -> NativeReconciliationOutcome:
    """长尾层 zpay 段：这是历史风险敞口的主承接（zpay 码无网关过期）.

    全部 CLOSED zpay 单（含高频窗 7 天出窗的、迁移前 closed_at IS NULL 的
    存量）持续问网关——「已付未入账」无论多久之前发生都有自动发现入口
    （评审二轮 P1：181 天盲区反例）。无界集合靠慢频小限额轮转覆盖。
    """
    return _reconcile_closed_zpay_order_nos(
        conn,
        order_nos=_list_closed_long_tail(
            conn,
            provider="zpay",
            limit=limit,
            offset=offset,
        ),
        query_order=query_order,
        allowed_channels=allowed_channels,
    )


def _reconcile_closed_zpay_order_nos(
    conn: BusinessConnection,
    *,
    order_nos: list[str],
    query_order: Callable[[str], ZPayOrderQueryResult],
    allowed_channels: Collection[str] | None,
) -> NativeReconciliationOutcome:
    queried = settled = unpaid = skipped = 0
    unpaid_order_nos: list[str] = []
    rate_limited = False
    for order_no in order_nos:
        try:
            answer = query_order(order_no)
        except Exception as exc:  # noqa: BLE001 — 一笔坏单不能拖垮整轮
            skipped += 1
            if isinstance(exc, (OrderQueryError, ZPayOrderQueryError)) and exc.status_code == 429:
                logger.warning("Recharge query rate limited; deferring remaining channel queries.")
                rate_limited = True
                break
            logger.warning(
                "Recharge reconciliation query for order %s failed (%s); it stays CLOSED",
                order_no,
                type(exc).__name__,
            )
            continue
        queried += 1
        if answer.merchant_order_no != order_no:
            logger.warning(
                "zpay closed-order query answered for %s while asking about %s; settling nothing",
                answer.merchant_order_no,
                order_no,
            )
            skipped += 1
            continue
        if not answer.paid:
            unpaid += 1
            unpaid_order_nos.append(order_no)
            continue
        if answer.provider_trade_no is None or answer.amount_fen is None or answer.channel is None:
            logger.warning(
                "zpay closed-order query for %s is paid but incomplete; settling nothing",
                order_no,
            )
            skipped += 1
            continue
        try:
            confirm_recharge_payment(
                conn,
                merchant_order_no=order_no,
                provider_trade_no=answer.provider_trade_no,
                amount_fen=answer.amount_fen,
                channel=answer.channel,
                source_digest=answer.response_digest,
                allowed_channels=allowed_channels,
                provider_spec=ZPAY_SETTLEMENT_SPEC,
            )
        except PaymentConfirmationError as exc:
            logger.warning(
                "zpay closed-order reconciliation refused to settle %s: %s",
                order_no,
                exc.code,
            )
            skipped += 1
            continue
        settled += 1
    return NativeReconciliationOutcome(
        queried=queried,
        settled=settled,
        unpaid=unpaid,
        skipped=skipped,
        unpaid_order_nos=tuple(unpaid_order_nos),
        rate_limited=rate_limited,
    )


def close_lapsed_orders_by_no(
    conn: BusinessConnection,
    *,
    provider: str,
    merchant_order_nos: Collection[str],
    validity_seconds: int,
) -> int:
    """只关闭"本轮确认网关说没付"的过期单；查单失败/没轮到查的一律不动。

    全量 close_expired_* 的危险面：它不区分"查过说没付"和"根本没查过"——
    后者可能是已付款的掉单，一旦 CLOSED，reconcile 与管理端补单都不再受理。
    sweep 的关闭阶段必须走这里（CLI 的每日 close 保留原样，那是既有部署契约）。
    """
    order_nos = list(merchant_order_nos)
    if not order_nos:
        return 0
    updated = conn.execute(
        """
        UPDATE recharge_orders SET status = 'CLOSED', closed_at = CURRENT_TIMESTAMP
        WHERE provider = %s AND status = 'PENDING'
          AND merchant_order_no = ANY(%s)
          AND created_at::timestamptz + make_interval(secs => %s) <= now()
        """,
        (provider, order_nos, float(validity_seconds)),
    )
    return int(updated.rowcount)


def confirm_recharge_payment(
    conn: BusinessConnection,
    *,
    merchant_order_no: str,
    provider_trade_no: str,
    amount_fen: int,
    channel: str,
    source_digest: str,
    allowed_channels: Collection[str] | None = None,
    provider_spec: SettlementProviderSpec = ZPAY_SETTLEMENT_SPEC,
) -> BusinessRow:
    """Idempotently settle a recharge order and credit the owner's wallet.

    ``provider_trade_no`` carries the provider's trade reference *value* regardless of
    which column stores it: ``provider_spec.trade_no_column`` names that column
    (``provider_trade_no`` for ZPay, ``transaction_id`` for WeChat Native). The default
    spec keeps every existing ZPay caller byte-identical.
    """
    spec = provider_spec
    prefix = spec.error_prefix
    label = spec.provider_label
    if not merchant_order_no or not provider_trade_no.strip():
        raise PaymentConfirmationError(
            f"{prefix}_PAYMENT_REFERENCE_INVALID",
            f"{label} order and trade numbers are required.",
            status_code=400,
        )

    # A PG nested transaction is a real savepoint. Route handlers may catch a
    # business error and return a normal response, so the rollback must happen
    # here before that catch. Releasing this savepoint never commits the outer
    # request transaction. BusinessConnection owns a PostgreSQL connection.
    transaction = conn.raw.transaction()
    try:
        with transaction:
            order = _read_settlement_order(
                conn, merchant_order_no=merchant_order_no, trade_no_column=spec.trade_no_column
            )
            if order is None:
                raise PaymentConfirmationError(
                    f"{prefix}_ORDER_NOT_FOUND",
                    "Recharge order does not exist.",
                    status_code=404,
                )
            if str(order["provider"]) != spec.provider_name:
                raise PaymentConfirmationError(
                    f"{prefix}_PROVIDER_MISMATCH",
                    f"Recharge order provider does not match {label}.",
                )
            if int(order["amount_fen"]) != amount_fen:
                raise PaymentConfirmationError(
                    f"{prefix}_AMOUNT_MISMATCH",
                    f"{label} amount does not match the stored recharge order.",
                )
            merchant_channels = set(allowed_channels or (str(order["channel"]),))
            if channel not in spec.channel_universe or channel not in merchant_channels:
                raise PaymentConfirmationError(
                    f"{prefix}_CHANNEL_MISMATCH",
                    f"{label} channel is not enabled for this merchant.",
                )

            bound_order = conn.execute(
                f"""
                SELECT merchant_order_no
                FROM recharge_orders
                WHERE {spec.trade_no_column} = %s AND merchant_order_no != %s
                """,
                (provider_trade_no, merchant_order_no),
            ).fetchone()
            if bound_order is not None:
                raise PaymentConfirmationError(
                    f"{prefix}_TRADE_ALREADY_BOUND",
                    f"{label} trade number is already bound to another recharge order.",
                )

            existing_trade_no = order["trade_ref"]
            if existing_trade_no is not None and str(existing_trade_no) != provider_trade_no:
                raise PaymentConfirmationError(
                    f"{prefix}_TRADE_NO_MISMATCH",
                    f"{label} trade number does not match the stored recharge order.",
                )
            if str(order["status"]) == "PAID":
                # The order lock serializes concurrent replays before any writes.
                return order
            if str(order["status"]) not in {"PENDING", "CLOSED"}:
                raise PaymentConfirmationError(
                    f"{prefix}_ORDER_NOT_SETTLEABLE",
                    "Recharge order is not waiting for settlement.",
                )

            updated = conn.execute(
                f"""
                UPDATE recharge_orders
                SET status = 'PAID',
                    {spec.trade_no_column} = %s,
                    notify_digest = %s,
                    payment_method = %s,
                    paid_at = CURRENT_TIMESTAMP
                WHERE id = %s AND status IN ('PENDING', 'CLOSED')
                """,
                (provider_trade_no, source_digest, channel, str(order["id"])),
            )
            if updated.rowcount != 1:
                raise PaymentConfirmationError(
                    f"{prefix}_ORDER_CHANGED",
                    "Recharge order changed while payment was being confirmed.",
                )

            conn.execute(
                """
                INSERT INTO wallet_transactions (
                    id, user_id, type, available_delta, reserved_delta,
                    recharge_order_id, task_id, billing_round, idempotency_key, auth_source
                ) VALUES (%s, %s, 'CHARGE', %s, 0, %s, NULL, NULL, %s, 'internal')
                """,
                (
                    str(uuid4()),
                    str(order["user_id"]),
                    int(order["credits"]),
                    str(order["id"]),
                    f"{spec.provider_name}:charge:{order['id']}",
                ),
            )
            wallet = conn.execute(
                """
                UPDATE wallets
                SET available_credits = available_credits + %s,
                    updated_at = CURRENT_TIMESTAMP
                WHERE user_id = %s AND available_credits <= 2147483647 - %s
                """,
                (int(order["credits"]), str(order["user_id"]), int(order["credits"])),
            )
            if wallet.rowcount != 1:
                # Either the wallet is missing or the credit would overflow int4
                # (the T23 admin-adjustment bound, M3 review LOW). Distinguish so
                # the callback answers a final 409 instead of a retried 500.
                exists = conn.execute(
                    "SELECT 1 FROM wallets WHERE user_id = %s", (str(order["user_id"]),)
                ).fetchone()
                if exists is None:
                    raise PaymentConfirmationError(
                        "WALLET_NOT_FOUND",
                        "Wallet record is missing for the recharge order owner.",
                        status_code=500,
                    )
                raise PaymentConfirmationError(
                    "WALLET_CREDIT_OVERFLOW",
                    "Wallet credit balance would overflow; settle manually.",
                    status_code=409,
                )

            # 套餐权益在资金入账后同事务授予（幂等：重放走上方 PAID 早退，不重复授予）。
            # 快照漂移（非法折扣率等）不得回滚已支付入账：记录告警并跳过授予，
            # 资金流水必须落定，权益由人工按订单快照补授。
            try:
                grant_discount_from_snapshot(
                    conn,
                    user_id=str(order["user_id"]),
                    source_recharge_order_id=str(order["id"]),
                    package_snapshot=parse_package_snapshot(order["package_snapshot_json"]),
                )
            except (ValueError, TypeError) as exc:
                logger.error(
                    "package discount grant skipped after payment settle: "
                    "merchant_order_no=%s user_id=%s error=%s",
                    merchant_order_no,
                    order["user_id"],
                    exc,
                )

            confirmed = _read_settlement_order(
                conn, merchant_order_no=merchant_order_no, trade_no_column=spec.trade_no_column
            )
            if confirmed is None:  # pragma: no cover - protected by the transaction above
                raise RuntimeError("confirmed recharge order disappeared")
            return confirmed
    except IntegrityConstraintError as exc:
        if exc.sqlstate != "23505":
            raise
        if exc.constraint_name in _TRADE_BOUND_UNIQUE_INDEXES:
            # 并发窗口下「先查后写」的 bound_order 预检查互相看不见对方的未提交
            # 行，靠交易号唯一索引兜底；这里把索引冲突映射回与预检查相同的
            # 客户可见错误码，回调方在两种路径下拿到一致的语义。
            raise PaymentConfirmationError(
                f"{prefix}_TRADE_ALREADY_BOUND",
                f"{label} trade number is already bound to another recharge order.",
            ) from exc
        raise PaymentConfirmationError(
            f"{prefix}_SETTLEMENT_CONFLICT",
            "Payment settlement conflicts with an existing ledger entry.",
        ) from exc
