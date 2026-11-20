"""账号诊断的使用门禁：首单免费（按客户 + 设备）+ 付费套餐准入.

产品口径（2026-11-20 定案）：

- 新注册客户首单免费 1 次；首单失败要归还，不能因为上游抖一下就吃掉免费机会。
- 之后的每一次诊断都要求"买过后台配置的充值套餐，且套餐里有积分"，再按
  ``account_diagnosis`` 科目扣积分。
- 免费额度按**钱包主体**（主账号）计，不按登录的从账号计——否则主账号开几个
  从账号就能刷几次免费。
- 设备维度只用于免费额度：同一台设备只能给一个客户贡献首单免费，批量注册
  刷免费额度的成本会明显变高。
"""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

from fastapi import HTTPException

from app.db_portable import BusinessConnection
from app.usage_billing import resolve_wallet_owner

MEMBER_REQUIRED = "ACCOUNT_DIAGNOSIS_MEMBER_REQUIRED"
MEMBER_REQUIRED_MESSAGE = "账号诊断需要先购买充值套餐，当前账号还没有可用套餐。"


def member_required_error() -> HTTPException:
    return HTTPException(
        402,
        detail={"code": MEMBER_REQUIRED, "message": MEMBER_REQUIRED_MESSAGE},
    )


def wallet_owner(conn: BusinessConnection, user_id: str) -> str:
    """付费主体：从账号的钱包与套餐都挂在主账号下."""

    return resolve_wallet_owner(conn, user_id)


def current_device_id(conn: BusinessConnection, user_id: str) -> str | None:
    """取该客户最近活跃的会话设备。

    不让客户端自报 device_id：报上来的值可以随手改，反刷就失去意义。会话表里
    的 device_id 是激活/登录时由服务端写入的，更可信。
    """

    row = conn.execute(
        "SELECT device_id FROM customer_session_state "
        "WHERE user_id=%s ORDER BY last_seen_at DESC NULLS LAST LIMIT 1",
        (user_id,),
    ).fetchone()
    if row is None:
        return None
    value = str(row[0]).strip()
    return value or None


def _snapshot_credits(snapshot: object) -> int:
    if isinstance(snapshot, str):
        try:
            payload: Any = json.loads(snapshot)
        except ValueError:
            return 0
    else:
        payload = snapshot
    if not isinstance(payload, dict):
        return 0
    try:
        return int(payload.get("credits") or 0)
    except (TypeError, ValueError):
        return 0


def has_paid_package(conn: BusinessConnection, owner_id: str) -> bool:
    """是否买过后台配置的、含积分的充值套餐.

    读订单冻结的 ``package_snapshot_json`` 而不是当前套餐配置：管理员后来把套餐
    改成 0 积分，不该追溯剥夺老客户的会员资格。0 元赠送/补偿订单没有 package_id，
    因此天然不算会员。
    """

    rows = conn.execute(
        "SELECT package_snapshot_json, credits FROM recharge_orders "
        "WHERE user_id=%s AND status='PAID' AND package_id IS NOT NULL "
        "ORDER BY paid_at DESC NULLS LAST LIMIT 20",
        (owner_id,),
    ).fetchall()
    for row in rows:
        credits = _snapshot_credits(row[0])
        if credits <= 0:
            try:
                credits = int(row[1] or 0)
            except (TypeError, ValueError):
                credits = 0
        if credits > 0:
            return True
    return False


def free_quota_claimed(conn: BusinessConnection, owner_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM account_diagnosis_free_grants WHERE user_id=%s LIMIT 1",
        (owner_id,),
    ).fetchone()
    return row is not None


def claim_free_quota(
    conn: BusinessConnection,
    *,
    owner_id: str,
    device_id: str | None,
    report_id: str,
) -> bool:
    """原子占用首单免费额度；已被占用（含同设备其他账号）时返回 False.

    两个唯一索引（user_id、device_id）共同保证：并发双开只有一个能插入成功，
    同一设备换号也拿不到第二次免费。
    """

    inserted = conn.execute(
        "INSERT INTO account_diagnosis_free_grants (id, user_id, device_id, report_id) "
        "VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
        (str(uuid4()), owner_id, device_id, report_id),
    )
    return inserted.rowcount == 1


def release_free_quota(
    conn: BusinessConnection,
    *,
    owner_id: str,
    report_id: str,
) -> None:
    """任务失败时归还免费额度。

    只删本次任务自己占用的那一行：并发场景下不能把别人刚占的额度删掉。
    """

    conn.execute(
        "DELETE FROM account_diagnosis_free_grants WHERE user_id=%s AND report_id=%s",
        (owner_id, report_id),
    )
