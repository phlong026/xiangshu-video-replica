"""短时有效的报价确认；只有受理成功的账务快照才承诺锁价。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException

from app.billing_catalog import amount, retail_snapshot
from app.db_portable import BusinessConnection
from app.usage_billing import resolve_wallet_owner

QUOTE_LIFETIME_SECONDS = 300
_QUOTE_PURPOSE = "billing-quote-v1"


def _now(conn: BusinessConnection) -> int:
    # 报价到期与折扣生效同用数据库时钟，避免应用节点时钟差异改变受理结果。
    row = conn.execute("SELECT clock_timestamp()").fetchone()
    return int((row[0] if row else datetime.now(UTC)).timestamp())


def _cipher() -> Fernet:
    from app.settings import SettingsKeyInvalid, SettingsKeyMissing, settings_encryption_key

    # 独立用途派生，报价不能当作平台设置密文使用，也不增加部署密钥。
    try:
        key = base64.urlsafe_b64decode(settings_encryption_key().encode("ascii"))
    except (SettingsKeyMissing, SettingsKeyInvalid) as exc:
        raise HTTPException(
            503,
            detail={"code": "QUOTE_UNAVAILABLE", "message": "报价服务暂不可用，请稍后重试。"},
        ) from exc
    derived = hmac.new(key, _QUOTE_PURPOSE.encode("ascii"), hashlib.sha256).digest()
    return Fernet(base64.urlsafe_b64encode(derived))


def _reject(code: str, message: str) -> HTTPException:
    return HTTPException(409, detail={"code": code, "message": message})


def oral_quote_specification(
    *, script_text: str | None, audio_asset_id: str | None
) -> dict[str, str | None]:
    return {
        "audio_asset_id": audio_asset_id,
        "script_sha256": None
        if audio_asset_id
        else hashlib.sha256((script_text or "").encode("utf-8")).hexdigest(),
    }


def issue_quote(
    conn: BusinessConnection,
    *,
    user_id: str,
    service: str,
    units: Decimal | str | int | float,
    quantity: int = 1,
    specification: dict[str, Any] | None = None,
) -> dict[str, Any]:
    wallet_owner_id = resolve_wallet_owner(conn, user_id)
    snapshot = retail_snapshot(conn, service, units, user_id=wallet_owner_id)
    maximum = int(str(snapshot["credits"])) * quantity
    if quantity < 1 or maximum > 2_147_483_647:
        raise HTTPException(
            422, detail={"code": "CREDIT_AMOUNT_OUT_OF_RANGE", "message": "积分金额超出允许范围。"}
        )
    now = _now(conn)
    expires_at = now + QUOTE_LIFETIME_SECONDS
    contract = {
        "purpose": _QUOTE_PURPOSE,
        "user_id": user_id,
        "wallet_owner_id": wallet_owner_id,
        "service": service,
        "units": str(amount(units)),
        "quantity": quantity,
        "specification": specification or {},
        "pricing_snapshot": snapshot,
        "max_credits": maximum,
        "expires_at": expires_at,
    }
    token = (
        _cipher()
        .encrypt_at_time(
            json.dumps(contract, sort_keys=True, separators=(",", ":")).encode("utf-8"), now
        )
        .decode("ascii")
    )
    return {
        "quote_token": token,
        "quote_expires_at": expires_at,
        "max_credits": maximum,
        "pricing_snapshot": snapshot,
    }


def validate_quote(
    conn: BusinessConnection,
    *,
    user_id: str,
    service: str,
    units: Decimal | str | int | float,
    quantity: int = 1,
    quote_token: str | None = None,
    max_credits: int | None = None,
    specification: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """在创建事务、幂等回放检查之后调用；返回本次各任务共用的固定单价。"""
    wallet_owner_id = resolve_wallet_owner(conn, user_id)
    current = retail_snapshot(conn, service, units, user_id=wallet_owner_id)
    if not quote_token:
        if int(str(current["credits"])) > 0 or max_credits is not None:
            raise _reject(
                "QUOTE_REQUIRED", "缺少报价凭证，请升级客户端并重新获取、确认报价后再提交。"
            )
        # 免费功能保留兼容；一旦开始收费，必须先确认报价，不能沿用无凭证路径。
        return dict(current)
    now = _now(conn)
    try:
        contract = json.loads(_cipher().decrypt(quote_token.encode("ascii")))
    except (InvalidToken, ValueError, UnicodeError, TypeError) as exc:
        raise _reject("QUOTE_INVALID", "报价凭证无效，请重新获取并确认报价。") from exc
    if not isinstance(contract, dict) or contract.get("purpose") != _QUOTE_PURPOSE:
        raise _reject("QUOTE_INVALID", "报价凭证无效，请重新获取并确认报价。")
    if not isinstance(contract.get("expires_at"), int) or now >= contract["expires_at"]:
        raise _reject("QUOTE_EXPIRED", "报价已过期，请重新获取并确认报价。")
    expected = {
        "user_id": user_id,
        "wallet_owner_id": wallet_owner_id,
        "service": service,
        "units": str(amount(units)),
        "quantity": quantity,
        "specification": specification or {},
    }
    if any(contract.get(key) != value for key, value in expected.items()):
        raise _reject("QUOTE_MISMATCH", "账号或生成参数已改变，请重新获取并确认报价。")
    if contract.get("pricing_snapshot") != current:
        raise _reject("QUOTE_CHANGED", "价格或折扣已改变，请重新获取并确认报价。")
    total = int(str(current["credits"])) * quantity
    if max_credits is None or max_credits < total or max_credits > contract["max_credits"]:
        raise _reject("QUOTE_LIMIT_EXCEEDED", "本次积分超过已确认上限，请重新确认报价。")
    return {
        **current,
        "quote_wallet_owner_id": wallet_owner_id,
        "quote_actor_user_id": user_id,
        "quote_max_credits": max_credits,
        "quote_quantity": quantity,
        "quote_expires_at": contract["expires_at"],
    }
