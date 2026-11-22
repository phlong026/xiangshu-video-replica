"""统一消息中心（2026-11-20）。

四类消息全部**读时投影**自既有业务事实，不新建事件表、不向计费/支付/
任务状态机写入任何数据：

- 任务动态：视频批次与数字人口播的终态（时间取状态触发器的事件时刻，
  归档/改名不会把同一条任务重新冒泡）。
- 账单通知：钱包流水（充值到账、结算扣费、预扣退回、退款、平台调账），
  金额直接读 ``recharge_orders`` / ``billing_operations``，与钱包账本同源。
- 平台公告：``announcements``，已读回执复用 ``announcement_reads``，和
  客户端公告横幅共享同一份事实。
- 账号与安全：``customer_session_events`` 里该用户的新设备登录、改密、
  全设备退出。

已读有两层：
- 单条已读 → ``studio_notification_preferences.prefs_json`` 的 receipts 映射；
- 「全部已读」→ ``studio_notification_preferences.prefs_json`` 的分类水位。

消息保留最近 180 天；更早的历史仍可在任务记录与钱包流水中查看。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, Field

from app.auth import CurrentUser, ReadDatabase, ShortAuthUser
from app.billing_catalog import SERVICES
from app.customer_fence import BusinessDbDep
from app.db_portable import BusinessConnection
from app.permissions import require_not_auditor
from app.studio_routes import (
    MESSAGE_CATEGORIES,
    MessageCategory,
    NotificationPreferenceState,
    load_notification_preferences,
)

router = APIRouter(prefix="/api")

MessageCategoryFilter = Literal["all", "task", "billing", "announcement", "security"]

RETENTION_DAYS = 180
_SOURCE_LIMIT = 200
_BEIJING = timezone(timedelta(hours=8))

PROVIDER_LABELS: dict[str, str] = {
    "wechat_native": "微信支付",
    "zpay": "在线支付",
    "offline": "线下充值",
    "admin_adjustment": "平台调账",
    "activation_code": "激活码",
}


# ---------------------------------------------------------------------------
# API models
# ---------------------------------------------------------------------------


class StudioMessageAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["task", "wallet", "profile"]
    label: str
    # 任务详情需要的三个字段，与旧通知的 navigate 参数一一对应。
    task_kind: str | None = None
    task_id: str | None = None


class StudioMessageItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    category: str
    kind: str
    title: str
    summary: str
    tone: str
    status_label: str
    occurred_at: str
    unread: bool
    amount_credits: int | None = None
    detail: str | None = None
    action: StudioMessageAction | None = None


class StudioMessagesSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total_unread: int
    by_category: dict[str, int]
    enabled: bool
    muted: list[MessageCategory]
    billing_style: str


class StudioMessagesPage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[StudioMessageItem]
    total: int
    unread_count: int
    by_category: dict[str, int]
    has_more: bool
    enabled: bool
    muted: list[MessageCategory]
    billing_style: str


class StudioMessagesReadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    keys: list[str] = Field(default_factory=list, max_length=500)
    category: MessageCategoryFilter | None = None


class StudioMessagesReadResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    marked: int
    read_before: dict[str, str]


# ---------------------------------------------------------------------------
# Internal message shape
# ---------------------------------------------------------------------------


@dataclass
class _Message:
    key: str
    category: str
    kind: str
    title: str
    summary: str
    tone: str
    status_label: str
    occurred_at: datetime
    action: StudioMessageAction | None = None
    amount_credits: int | None = None
    detail: str | None = None
    unread: bool = False


_TONES = {
    "SUCCEEDED": ("success", "已完成"),
    "FAILED": ("danger", "失败"),
    "PARTIAL_FAILED": ("warning", "部分失败"),
    "SUBMISSION_UNCERTAIN": ("warning", "待确认"),
    "ARCHIVE_FAILED": ("warning", "归档失败"),
}


def _as_datetime(value: object) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    parsed = datetime.fromisoformat(str(value))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _retention_cutoff() -> str:
    """TEXT 时间列（created_at / updated_at）的 UTC 截断值。

    这些列由 ``CURRENT_TIMESTAMP`` 写成 ``YYYY-MM-DD HH:MM:SS[.ffffff+TZ]``，
    与同格式字符串做字典序比较即等价于时间比较，且能走 (user_id, created_at)
    索引——不把函数套在列上。
    """
    moment = datetime.now(UTC) - timedelta(days=RETENTION_DAYS)
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def _format_duration(seconds: float) -> str:
    total = max(0, int(round(seconds)))
    if total >= 3600:
        return f"{total // 3600} 小时 {(total % 3600) // 60} 分"
    if total >= 60:
        return f"{total // 60} 分 {total % 60} 秒"
    return f"{total} 秒"


def _yuan(fen: int | None) -> str:
    if fen is None:
        return ""
    return f"¥{fen / 100:.2f}"


def _truncate(value: str, limit: int) -> str:
    text = " ".join(value.split())
    return text if len(text) <= limit else f"{text[:limit]}…"


# ---------------------------------------------------------------------------
# 任务动态
# ---------------------------------------------------------------------------


def _generation_messages(conn: BusinessConnection, user_id: str) -> list[_Message]:
    cutoff = _retention_cutoff()
    rows = conn.execute(
        """
        WITH my_batches AS (
          SELECT b.id AS batch_id,
                 COALESCE(NULLIF(b.display_name, ''), p.name, '视频生成') AS title
          FROM generation_batches b
          LEFT JOIN projects p ON p.id = b.project_id
          WHERE (p.owner_user_id = %s OR (b.project_id IS NULL AND b.created_by_user_id = %s))
            AND NOT EXISTS (
              SELECT 1 FROM customer_batch_visibility v
              WHERE v.user_id = %s AND v.batch_id = b.id
            )
        ), terminal_tasks AS (
          SELECT t.id, t.batch_id, t.status, t.archive_status,
                 t.created_at::timestamptz AS started_at,
                 COALESCE(ev.occurred_at, t.updated_at::timestamptz) AS terminal_at
          FROM generation_tasks t
          LEFT JOIN LATERAL (
            SELECT e.occurred_at
            FROM generation_record_status_events e
            WHERE e.record_type = 'VIDEO' AND e.record_id = t.id
              AND e.new_status IN ('SUCCEEDED', 'FAILED', 'SUBMISSION_UNCERTAIN')
            ORDER BY e.occurred_at DESC, e.id DESC
            LIMIT 1
          ) ev ON TRUE
          WHERE t.superseded_by_task_id IS NULL
            AND t.status IN ('SUCCEEDED', 'FAILED', 'SUBMISSION_UNCERTAIN')
            AND t.updated_at >= %s
        ), task_credits AS (
          SELECT wt.task_id, SUM(op.charged_credits) AS charged
          FROM wallet_transactions wt
          JOIN billing_operations op ON op.id = wt.billing_operation_id
          WHERE wt.user_id = %s AND wt.type = 'SETTLE' AND wt.created_at >= %s
          GROUP BY wt.task_id
        )
        SELECT b.batch_id, b.title,
               MAX(r.terminal_at) AS occurred_at,
               MIN(r.started_at) AS started_at,
               COUNT(*) AS total,
               COUNT(*) FILTER (
                 WHERE r.status = 'SUCCEEDED'
                   AND r.archive_status IS DISTINCT FROM 'ARCHIVE_FAILED'
               ) AS succeeded,
               COUNT(*) FILTER (WHERE r.status = 'FAILED') AS failed,
               COUNT(*) FILTER (WHERE r.status = 'SUBMISSION_UNCERTAIN') AS uncertain,
               COUNT(*) FILTER (WHERE r.archive_status = 'ARCHIVE_FAILED') AS archive_failed,
               COALESCE(SUM(c.charged), 0) AS credits
        FROM my_batches b
        JOIN terminal_tasks r ON r.batch_id = b.batch_id
        LEFT JOIN task_credits c ON c.task_id = r.id
        GROUP BY b.batch_id, b.title
        HAVING MAX(r.terminal_at) >= now() - (%s || ' days')::interval
           AND NOT EXISTS (
             SELECT 1 FROM generation_tasks x
             WHERE x.batch_id = b.batch_id
               AND x.superseded_by_task_id IS NULL
               AND x.status NOT IN ('SUCCEEDED', 'FAILED', 'SUBMISSION_UNCERTAIN')
           )
        ORDER BY MAX(r.terminal_at) DESC, b.batch_id DESC
        LIMIT %s
        """,
        (
            user_id,
            user_id,
            user_id,
            cutoff,
            user_id,
            cutoff,
            str(RETENTION_DAYS),
            _SOURCE_LIMIT,
        ),
    ).fetchall()

    messages: list[_Message] = []
    for row in rows:
        total = int(row["total"])
        succeeded = int(row["succeeded"])
        failed = int(row["failed"])
        uncertain = int(row["uncertain"])
        archive_failed = int(row["archive_failed"])
        if failed and (succeeded or uncertain or archive_failed):
            status = "PARTIAL_FAILED"
        elif failed:
            status = "FAILED"
        elif uncertain:
            status = "SUBMISSION_UNCERTAIN"
        elif archive_failed:
            status = "ARCHIVE_FAILED"
        else:
            status = "SUCCEEDED"
        tone, label = _TONES[status]
        occurred_at = _as_datetime(row["occurred_at"])
        started_at = _as_datetime(row["started_at"])
        credits = int(row["credits"] or 0)

        parts = [str(row["title"]), f"{total} 条视频"]
        if failed and (succeeded or uncertain):
            parts.append(f"{succeeded} 成功 / {failed} 失败")
        if uncertain:
            parts.append(f"{uncertain} 条待确认")
        if credits:
            parts.append(f"消耗 {credits} 积分")
        parts.append(f"用时 {_format_duration((occurred_at - started_at).total_seconds())}")
        headline = {
            "SUCCEEDED": "视频生成完成",
            "FAILED": "视频生成失败",
            "PARTIAL_FAILED": "视频生成部分失败",
            "SUBMISSION_UNCERTAIN": "视频生成待确认",
            "ARCHIVE_FAILED": "视频归档失败",
        }[status]
        messages.append(
            _Message(
                key=f"generation:{row['batch_id']}",
                category="task",
                kind="video",
                title=headline,
                summary=" · ".join(parts),
                tone=tone,
                status_label=label,
                occurred_at=occurred_at,
                action=StudioMessageAction(
                    kind="task",
                    label="查看任务",
                    task_kind="generation_batch",
                    task_id=str(row["batch_id"]),
                ),
                amount_credits=credits or None,
            )
        )
    return messages


def _oral_messages(conn: BusinessConnection, user_id: str) -> list[_Message]:
    cutoff = _retention_cutoff()
    rows = conn.execute(
        """
        WITH task_credits AS (
          SELECT wt.oral_task_id, SUM(op.charged_credits) AS charged
          FROM wallet_transactions wt
          JOIN billing_operations op ON op.id = wt.billing_operation_id
          WHERE wt.user_id = %s AND wt.type = 'SETTLE' AND wt.created_at >= %s
          GROUP BY wt.oral_task_id
        )
        SELECT oral.id, oral.title, oral.status, oral.duration_sec,
               COALESCE(ev.occurred_at, oral.updated_at::timestamptz) AS occurred_at,
               COALESCE(c.charged, 0) AS credits
        FROM oral_tasks oral
        LEFT JOIN LATERAL (
          SELECT e.occurred_at
          FROM generation_record_status_events e
          WHERE e.record_type = 'ORAL_VIDEO' AND e.record_id = oral.id
            AND e.new_status IN ('SUCCEEDED', 'FAILED', 'SUBMISSION_UNCERTAIN')
          ORDER BY e.occurred_at DESC, e.id DESC
          LIMIT 1
        ) ev ON TRUE
        LEFT JOIN task_credits c ON c.oral_task_id = oral.id
        WHERE oral.owner_user_id = %s
          AND oral.updated_at >= %s
          AND oral.status IN ('SUCCEEDED', 'FAILED', 'SUBMISSION_UNCERTAIN', 'ARCHIVE_FAILED')
          AND COALESCE(ev.occurred_at, oral.updated_at::timestamptz)
              >= now() - (%s || ' days')::interval
        ORDER BY occurred_at DESC, oral.id DESC
        LIMIT %s
        """,
        (user_id, cutoff, user_id, cutoff, str(RETENTION_DAYS), _SOURCE_LIMIT),
    ).fetchall()

    messages: list[_Message] = []
    for row in rows:
        status = str(row["status"])
        tone, label = _TONES.get(status, ("info", "待处理"))
        parts = [str(row["title"])]
        if row["duration_sec"]:
            parts.append(f"{int(row['duration_sec'])}s")
        credits = int(row["credits"] or 0)
        if credits:
            parts.append(f"消耗 {credits} 积分")
        if status == "FAILED":
            parts.append("可查看详情后重试")
        headline = {
            "SUCCEEDED": "数字人口播完成",
            "FAILED": "数字人口播失败",
            "SUBMISSION_UNCERTAIN": "数字人口播待确认",
            "ARCHIVE_FAILED": "数字人口播归档失败",
        }.get(status, "数字人口播")
        messages.append(
            _Message(
                key=f"oral:{row['id']}",
                category="task",
                kind="oral",
                title=headline,
                summary=" · ".join(parts),
                tone=tone,
                status_label=label,
                occurred_at=_as_datetime(row["occurred_at"]),
                action=StudioMessageAction(
                    kind="task",
                    label="查看任务",
                    task_kind="oral_task",
                    task_id=str(row["id"]),
                ),
                amount_credits=credits or None,
            )
        )
    return messages


# ---------------------------------------------------------------------------
# 账单通知
# ---------------------------------------------------------------------------


def _billing_rows(conn: BusinessConnection, user_id: str) -> list[_Message]:
    cutoff = _retention_cutoff()
    rows = conn.execute(
        """
        SELECT wt.id, wt.type, wt.available_delta, wt.billing_operation_id,
               wt.recharge_order_id,
               wt.created_at::timestamptz AS occurred_at,
               o.charged_credits, o.reserved_credits, o.service,
               ro.credits AS recharge_credits, ro.amount_fen, ro.provider,
               ro.merchant_order_no,
               aa.reason AS adjustment_reason
        FROM wallet_transactions wt
        LEFT JOIN billing_operations o ON o.id = wt.billing_operation_id
        LEFT JOIN recharge_orders ro ON ro.id = wt.recharge_order_id
             AND ro.user_id = wt.user_id
        LEFT JOIN admin_adjustments aa ON aa.recharge_order_id = wt.recharge_order_id
             AND aa.target_user_id = wt.user_id
        WHERE wt.user_id = %s
          AND wt.type IN ('CHARGE', 'SETTLE', 'RELEASE', 'REFUND', 'CONVERSION')
          AND wt.created_at >= %s
        ORDER BY wt.created_at::timestamptz DESC, wt.id DESC
        LIMIT %s
        """,
        (user_id, cutoff, _SOURCE_LIMIT),
    ).fetchall()

    cycles: dict[str, list[Any]] = {}
    singles: list[Any] = []
    for row in rows:
        operation_id = row["billing_operation_id"]
        if row["type"] in ("SETTLE", "RELEASE") and operation_id:
            cycles.setdefault(str(operation_id), []).append(row)
        elif row["type"] in ("CHARGE", "REFUND", "CONVERSION"):
            singles.append(row)

    messages: list[_Message] = []
    for operation_id, cycle in cycles.items():
        settle = next((row for row in cycle if row["type"] == "SETTLE"), None)
        release = next((row for row in cycle if row["type"] == "RELEASE"), None)
        reference = settle or release
        if reference is None:
            continue
        reserved = int(reference["reserved_credits"] or 0)
        charged = int(settle["charged_credits"] or 0) if settle is not None else 0
        occurred_at = _as_datetime(reference["occurred_at"])
        service = reference["service"]
        service_name = SERVICES[service].name if service in SERVICES else None
        if settle is not None:
            released = max(0, reserved - charged)
            parts = [f"实扣 {charged} 积分"]
            if released:
                parts.append(f"预扣 {reserved}，退回 {released}")
            if service_name:
                parts.append(service_name)
            messages.append(
                _Message(
                    key=f"billing:{operation_id}",
                    category="billing",
                    kind="settle",
                    title="扣费结算" if charged else "本次未扣费",
                    summary=" · ".join(parts),
                    tone="info",
                    status_label="已结算",
                    occurred_at=occurred_at,
                    amount_credits=-charged if charged else 0,
                    action=StudioMessageAction(kind="wallet", label="查看账单"),
                )
            )
        else:
            messages.append(
                _Message(
                    key=f"billing:{operation_id}",
                    category="billing",
                    kind="release",
                    title="预扣积分已退回",
                    summary=f"本次生成未扣费，已退回 {reserved} 积分"
                    + (f" · {service_name}" if service_name else ""),
                    tone="info",
                    status_label="已退回",
                    occurred_at=occurred_at,
                    amount_credits=reserved,
                    action=StudioMessageAction(kind="wallet", label="查看账单"),
                )
            )

    for row in singles:
        row_type = str(row["type"])
        occurred_at = _as_datetime(row["occurred_at"])
        if row_type == "CHARGE":
            credits = int(row["recharge_credits"] or 0)
            provider = str(row["provider"] or "")
            order_no = str(row["merchant_order_no"] or "")
            if provider == "admin_adjustment":
                reason = str(row["adjustment_reason"] or "管理员调整")
                messages.append(
                    _Message(
                        key=f"billing:{row['id']}",
                        category="billing",
                        kind="adjustment",
                        title="平台调账",
                        summary=f"{reason} · 增加 {credits} 积分",
                        tone="info",
                        status_label="已入账",
                        occurred_at=occurred_at,
                        amount_credits=credits,
                        action=StudioMessageAction(kind="wallet", label="查看账单"),
                    )
                )
            else:
                channel = PROVIDER_LABELS.get(provider, "在线充值")
                detail = " · ".join(
                    part
                    for part in (
                        channel,
                        _yuan(row["amount_fen"]),
                        f"订单 {order_no}" if order_no else "",
                    )
                    if part
                )
                messages.append(
                    _Message(
                        key=f"billing:{row['id']}",
                        category="billing",
                        kind="credit",
                        title="充值到账",
                        summary=detail,
                        tone="success",
                        status_label="已到账",
                        occurred_at=occurred_at,
                        amount_credits=credits,
                        action=StudioMessageAction(kind="wallet", label="查看账单"),
                    )
                )
        elif row_type == "REFUND":
            removed = -int(row["available_delta"] or 0)
            has_order = bool(row["recharge_order_id"])
            messages.append(
                _Message(
                    key=f"billing:{row['id']}",
                    category="billing",
                    kind="refund",
                    title="退款扣减",
                    summary=(
                        f"对应充值退款，扣除 {removed} 积分"
                        if has_order
                        else f"管理员调整，扣除 {removed} 积分"
                    ),
                    tone="warning",
                    status_label="已扣减",
                    occurred_at=occurred_at,
                    amount_credits=-removed,
                    action=StudioMessageAction(kind="wallet", label="查看账单"),
                )
            )
        elif row_type == "CONVERSION":
            delta = int(row["available_delta"] or 0)
            messages.append(
                _Message(
                    key=f"billing:{row['id']}",
                    category="billing",
                    kind="conversion",
                    title="历史积分转换",
                    summary=f"历史余额换算{'增加' if delta >= 0 else '减少'} {abs(delta)} 积分",
                    tone="info",
                    status_label="已转换",
                    occurred_at=occurred_at,
                    amount_credits=delta,
                    action=StudioMessageAction(kind="wallet", label="查看账单"),
                )
            )
    return messages


def _daily_billing(messages: list[_Message]) -> list[_Message]:
    """按北京时间自然日合并成一条「今日积分变动」。"""
    buckets: dict[str, list[_Message]] = {}
    for message in messages:
        day = message.occurred_at.astimezone(_BEIJING).strftime("%Y-%m-%d")
        buckets.setdefault(day, []).append(message)
    merged: list[_Message] = []
    for day, rows in buckets.items():
        income = sum(max(0, row.amount_credits or 0) for row in rows)
        expense = sum(min(0, row.amount_credits or 0) for row in rows)
        latest = max(rows, key=lambda row: row.occurred_at)
        merged.append(
            _Message(
                key=f"billing:daily:{day}",
                category="billing",
                kind="daily",
                title="今日积分变动",
                summary=(
                    f"共 {len(rows)} 笔 · 收入 +{income} 积分"
                    + (f" · 支出 {expense} 积分" if expense else "")
                ),
                tone="info",
                status_label=day,
                occurred_at=latest.occurred_at,
                amount_credits=income + expense,
                action=StudioMessageAction(kind="wallet", label="查看账单"),
            )
        )
    return merged


def _balance_warning(
    conn: BusinessConnection, user_id: str, messages: list[_Message], threshold: int
) -> list[_Message]:
    if threshold <= 0 or not any(message.kind == "settle" for message in messages):
        return []
    row = conn.execute(
        "SELECT available_credits FROM wallets WHERE user_id = %s", (user_id,)
    ).fetchone()
    if row is None:
        return []
    balance = int(row["available_credits"])
    if balance >= threshold:
        return []
    latest = max(messages, key=lambda message: message.occurred_at)
    day = latest.occurred_at.astimezone(_BEIJING).strftime("%Y-%m-%d")
    return [
        _Message(
            key=f"billing:low:{day}",
            category="billing",
            kind="low_balance",
            title="余额不足提醒",
            summary=f"当前余额 {balance} 积分，低于提醒阈值 {threshold} 积分",
            tone="warning",
            status_label="需充值",
            occurred_at=latest.occurred_at,
            action=StudioMessageAction(kind="wallet", label="去充值"),
        )
    ]


# ---------------------------------------------------------------------------
# 平台公告
# ---------------------------------------------------------------------------


def _announcement_messages(conn: BusinessConnection, user_id: str) -> list[_Message]:
    rows = conn.execute(
        """
        SELECT a.id, a.title, a.body, a.tone, a.pinned, a.starts_at,
               CASE WHEN r.user_id IS NULL THEN 0 ELSE 1 END AS read_flag
        FROM announcements a
        LEFT JOIN announcement_reads r
          ON r.announcement_id = a.id AND r.user_id = %s
        WHERE a.deleted_at IS NULL
          AND a.starts_at <= now()
          AND (a.ends_at IS NULL OR a.ends_at > now())
        ORDER BY a.pinned DESC, a.starts_at DESC, a.id DESC
        LIMIT %s
        """,
        (user_id, 50),
    ).fetchall()
    tone_map = {"maintenance": ("warning", "维护"), "promo": ("success", "活动")}
    messages: list[_Message] = []
    for row in rows:
        tone, label = tone_map.get(str(row["tone"]), ("info", "公告"))
        if row["pinned"]:
            label = "置顶"
        messages.append(
            _Message(
                key=f"announcement:{row['id']}",
                category="announcement",
                kind="announcement",
                title=str(row["title"]),
                summary=_truncate(str(row["body"]), 120),
                tone=tone,
                status_label=label,
                occurred_at=_as_datetime(row["starts_at"]),
                detail=str(row["body"]),
                unread=not bool(row["read_flag"]),
            )
        )
    return messages


# ---------------------------------------------------------------------------
# 账号与安全
# ---------------------------------------------------------------------------


def _security_messages(conn: BusinessConnection, user_id: str) -> list[_Message]:
    cutoff = _retention_cutoff()
    rows = conn.execute(
        """
        WITH first_login AS (
          SELECT id,
                 row_number() OVER (PARTITION BY device_id ORDER BY created_at, id) AS rn
          FROM customer_session_events
          WHERE user_id = %s AND event = 'LOGIN'
        )
        SELECT e.id, e.event, e.reason,
               e.created_at::timestamptz AS occurred_at,
               d.display_name, d.platform
        FROM customer_session_events e
        LEFT JOIN customer_devices d ON d.id = e.device_id
        LEFT JOIN first_login f ON f.id = e.id
        WHERE e.user_id = %s
          AND e.created_at >= %s
          AND (
            (e.event = 'LOGIN' AND f.rn = 1)
            OR (
              e.event = 'LOGOUT'
              AND e.reason IN ('password_changed', 'all_devices_revoked', 'password_reset')
            )
          )
        ORDER BY e.created_at::timestamptz DESC, e.id DESC
        LIMIT %s
        """,
        (user_id, user_id, cutoff, _SOURCE_LIMIT),
    ).fetchall()

    messages: list[_Message] = []
    for row in rows:
        event = str(row["event"])
        reason = str(row["reason"] or "")
        occurred_at = _as_datetime(row["occurred_at"])
        if event == "LOGIN":
            device = (
                " · ".join(part for part in (row["platform"], row["display_name"]) if part)
                or "未知设备"
            )
            messages.append(
                _Message(
                    key=f"security:{row['id']}",
                    category="security",
                    kind="login",
                    title="新设备登录",
                    summary=f"{device}，如非本人操作请立即修改密码",
                    tone="warning",
                    status_label="安全提醒",
                    occurred_at=occurred_at,
                    action=StudioMessageAction(kind="profile", label="查看登录记录"),
                )
            )
            continue
        if reason == "password_changed":
            title = "登录密码已修改"
            summary = "其他设备已退出登录，请使用新密码重新登录"
        elif reason == "password_reset":
            title = "登录密码已重置"
            summary = "所有设备已退出，请使用新密码重新登录"
        else:
            title = "已退出全部设备"
            summary = "所有设备上的登录状态已失效"
        messages.append(
            _Message(
                key=f"security:{row['id']}",
                category="security",
                kind="password",
                title=title,
                summary=summary,
                tone="info",
                status_label="已生效",
                occurred_at=occurred_at,
                action=StudioMessageAction(kind="profile", label="查看登录记录"),
            )
        )
    return messages


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


def _account_scope(conn: BusinessConnection, user_id: str) -> str | None:
    """返回母账号 id：子账号（parent_user_id 非空）看到的是母账号钱包。"""
    row = conn.execute("SELECT parent_user_id FROM users WHERE id = %s", (user_id,)).fetchone()
    if row is None or row["parent_user_id"] is None:
        return None
    return str(row["parent_user_id"])


def collect_messages(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    state: NotificationPreferenceState,
) -> list[_Message]:
    preferences = state.preferences
    if not preferences.enabled:
        return []
    auditor = actor.role == "auditor"
    messages: list[_Message] = []
    messages.extend(_generation_messages(conn, actor.id))
    messages.extend(_oral_messages(conn, actor.id))
    messages.extend(_announcement_messages(conn, actor.id))
    if not auditor:
        messages.extend(_security_messages(conn, actor.id))
        wallet_owner = _account_scope(conn, actor.id)
        # 子账号共用母钱包但无权充值：账务消息只发给母账号。
        if wallet_owner is None:
            billing = _billing_rows(conn, actor.id)
            if preferences.billing_style == "daily":
                billing = _daily_billing(billing)
            elif preferences.billing_style == "off":
                billing = []
            if billing and preferences.billing_style != "off":
                billing.extend(
                    _balance_warning(conn, actor.id, billing, preferences.balance_threshold)
                )
            messages.extend(billing)

    receipts = state.receipts
    for message in messages:
        if message.category == "announcement":
            # 公告的已读事实以 announcement_reads 为准（消息中心已读 =
            # 客户端横幅消失）；本表单条回执同样视为已读。
            message.unread = message.unread and message.key not in receipts
        else:
            # 回执只覆盖阅读当时的版本，分类和全局水位也按最新已读时刻共同生效。
            cutoffs = [
                _as_datetime(value)
                for value in (
                    receipts.get(message.key),
                    state.watermarks.get(message.category),
                    state.watermarks.get("all"),
                )
                if value
            ]
            message.unread = not cutoffs or message.occurred_at > max(cutoffs)
        if message.category in preferences.muted:
            message.unread = False
    return messages


def _item(message: _Message) -> StudioMessageItem:
    return StudioMessageItem(
        id=message.key,
        category=message.category,
        kind=message.kind,
        title=message.title,
        summary=message.summary,
        tone=message.tone,
        status_label=message.status_label,
        occurred_at=message.occurred_at.astimezone(UTC).isoformat(),
        unread=message.unread,
        amount_credits=message.amount_credits,
        detail=message.detail,
        action=message.action,
    )


def _counts(messages: list[_Message]) -> dict[str, int]:
    counts = {category: 0 for category in MESSAGE_CATEGORIES}
    for message in messages:
        if message.unread:
            counts[message.category] = counts.get(message.category, 0) + 1
    return counts


def build_summary(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    state: NotificationPreferenceState,
) -> StudioMessagesSummary:
    preferences = state.preferences
    messages = collect_messages(conn, actor=actor, state=state)
    counts = _counts(messages)
    total = sum(count for category, count in counts.items() if category not in preferences.muted)
    return StudioMessagesSummary(
        total_unread=total,
        by_category=counts,
        enabled=preferences.enabled,
        muted=preferences.muted,
        billing_style=preferences.billing_style,
    )


def build_page(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    state: NotificationPreferenceState,
    category: MessageCategoryFilter,
    unread_only: bool,
    limit: int,
    offset: int,
) -> StudioMessagesPage:
    preferences = state.preferences
    messages = collect_messages(conn, actor=actor, state=state)
    counts = _counts(messages)
    total_unread = sum(count for name, count in counts.items() if name not in preferences.muted)
    if category != "all":
        messages = [message for message in messages if message.category == category]
    if unread_only:
        messages = [message for message in messages if message.unread]
    messages.sort(key=lambda message: (message.occurred_at, message.key), reverse=True)
    window = messages[offset : offset + limit]
    return StudioMessagesPage(
        items=[_item(message) for message in window],
        total=len(messages),
        unread_count=total_unread,
        by_category=counts,
        has_more=offset + limit < len(messages),
        enabled=preferences.enabled,
        muted=preferences.muted,
        billing_style=preferences.billing_style,
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/studio/messages/summary", response_model=StudioMessagesSummary)
def read_studio_messages_summary(
    conn: ReadDatabase,
    actor: ShortAuthUser,
) -> StudioMessagesSummary:
    state = load_notification_preferences(conn, actor.id)
    return build_summary(conn, actor=actor, state=state)


@router.get("/studio/messages", response_model=StudioMessagesPage)
def read_studio_messages(
    conn: ReadDatabase,
    actor: ShortAuthUser,
    category: MessageCategoryFilter = "all",
    unread_only: bool = False,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
    offset: Annotated[int, Query(ge=0, le=10_000)] = 0,
) -> StudioMessagesPage:
    state = load_notification_preferences(conn, actor.id)
    return build_page(
        conn,
        actor=actor,
        state=state,
        category=category,
        unread_only=unread_only,
        limit=limit,
        offset=offset,
    )


@router.post("/studio/messages/read", response_model=StudioMessagesReadResponse)
def mark_studio_messages_read(
    payload: StudioMessagesReadRequest,
    db: BusinessDbDep,
) -> StudioMessagesReadResponse:
    """单条已读写回执，分类已读写水位。

    回执与水位同存 ``studio_notification_preferences.prefs_json``：消息中心的
    逐条已读不需要再建一张业务表（避免迁移链与冻结 schema 变更）。写入是
    「读当前 JSON → 合并 → 整体回写」，同一用户并发标记只会互相保留，不会丢。
    """
    with db.write() as (conn, actor):
        require_not_auditor(
            conn,
            actor=actor,
            action="studio.messages_read",
            entity_type="studio_notification_preferences",
            entity_id=actor.id,
        )
        state = load_notification_preferences(conn, actor.id)
        receipts = dict(state.receipts)
        watermarks = dict(state.watermarks)
        keys = sorted({key for key in payload.keys if key})

        marked = 0
        if keys:
            row = conn.execute("SELECT clock_timestamp() AS cutoff").fetchone()
            read_at = str(row["cutoff"].isoformat())
            for key in keys:
                if key not in receipts:
                    marked += 1
                # 同一批次/日账单会更新；重复阅读也要推进时间，计数仍只统计新增键。
                receipts[key] = read_at
            announcement_ids = [
                key.split(":", 1)[1]
                for key in keys
                if key.startswith("announcement:") and ":" in key
            ]
            for announcement_id in announcement_ids:
                conn.execute(
                    "INSERT INTO announcement_reads (announcement_id, user_id) "
                    "SELECT a.id, %s FROM announcements a WHERE a.id = %s "
                    "ON CONFLICT (announcement_id, user_id) DO NOTHING",
                    (actor.id, announcement_id),
                )
            # 回执只保留消息保留期内的条目；极端情况下再按时间截断到 1000 条。
            retention = (datetime.now(UTC) - timedelta(days=RETENTION_DAYS)).isoformat()
            receipts = {
                key: value
                for key, value in receipts.items()
                if _as_datetime(value) >= _as_datetime(retention)
            }
            if len(receipts) > 1000:
                keep = sorted(receipts.items(), key=lambda item: item[1], reverse=True)[:1000]
                receipts = dict(keep)

        categories: list[str]
        if payload.category is None:
            categories = []
        elif payload.category == "all":
            categories = list(MESSAGE_CATEGORIES)
        else:
            categories = [payload.category]
        if categories:
            row = conn.execute("SELECT clock_timestamp() AS cutoff").fetchone()
            cutoff = str(row["cutoff"].isoformat())
            for name in categories:
                watermarks[name] = cutoff
            # 旧接口 GET /studio/notifications 只认全局水位：全部已读时补上，
            # 让新旧客户端混用时老版本也不会重新显示红点。
            if payload.category == "all":
                watermarks["all"] = cutoff
            # 公告的已读事实落在 announcement_reads：全部已读要让客户端横幅同步消失。
            if "announcement" in categories:
                conn.execute(
                    "INSERT INTO announcement_reads (announcement_id, user_id) "
                    "SELECT a.id, %s FROM announcements a "
                    "WHERE a.deleted_at IS NULL AND a.starts_at <= now() "
                    "AND (a.ends_at IS NULL OR a.ends_at > now()) "
                    "ON CONFLICT (announcement_id, user_id) DO NOTHING",
                    (actor.id,),
                )

        if keys or categories:
            conn.execute(
                """
                INSERT INTO studio_notification_preferences (user_id, prefs_json)
                VALUES (%s, %s)
                ON CONFLICT (user_id) DO UPDATE
                SET prefs_json = (studio_notification_preferences.prefs_json::jsonb
                    || excluded.prefs_json::jsonb)::text,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    actor.id,
                    json.dumps(
                        {"receipts": receipts, "read_before": watermarks},
                        ensure_ascii=False,
                    ),
                ),
            )
    return StudioMessagesReadResponse(marked=marked, read_before=watermarks)


__all__ = [
    "PROVIDER_LABELS",
    "RETENTION_DAYS",
    "build_page",
    "build_summary",
    "collect_messages",
    "router",
    "StudioMessagesPage",
    "StudioMessagesSummary",
]
