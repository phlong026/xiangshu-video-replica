"""Annual authorization shared by signup, customer authentication and operations."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import psycopg
from fastapi import HTTPException

from app.activation_code_service import (
    ActivationKeyError,
    InvalidActivationCodeError,
    iter_code_digests,
)
from app.api_errors import http_error

LICENSE_EXPIRED = "SOFTWARE_LICENSE_EXPIRED"
LICENSE_EXPIRED_MESSAGE = "软件授权已到期，请联系服务人员续费。"
_ACTIVATION_EXTENSION = timedelta(days=7)


def effective_activation_expiry(deadline: datetime, now: datetime, auto_extend: bool) -> datetime:
    if not auto_extend or now < deadline:
        return deadline
    return deadline + _ACTIVATION_EXTENSION * (1 + (now - deadline) // _ACTIVATION_EXTENSION)


def add_authorization_years(start: datetime, years: int) -> datetime:
    start = start.astimezone(UTC)
    try:
        return start.replace(year=start.year + years)
    except ValueError:
        # 2 月 29 日在非闰年以 2 月 28 日为周年日。
        return start.replace(year=start.year + years, day=28)


def _activation_unavailable() -> HTTPException:
    return http_error(400, "SOFTWARE_ACTIVATION_UNAVAILABLE", "激活码无效、已使用或已过期。")


def bind_signup_license(conn: psycopg.Connection, *, raw_code: str, user_id: str) -> None:
    try:
        digests = [digest for digest, _version in iter_code_digests(raw_code)]
    except InvalidActivationCodeError as exc:
        raise _activation_unavailable() from exc
    except ActivationKeyError as exc:
        raise http_error(
            503, "ACTIVATION_KEYS_UNAVAILABLE", "激活服务暂不可用，请稍后重试。"
        ) from exc

    row = conn.execute(
        "SELECT id, activation_expires_at, authorization_years, user_id "
        "FROM software_licenses WHERE code_digest = ANY(%s) FOR UPDATE",
        (digests,),
    ).fetchone()
    if row is None or row[3] is not None:
        raise _activation_unavailable()
    settings = conn.execute(
        "SELECT software_activation_auto_extend FROM runtime_settings WHERE id = 1"
    ).fetchone()
    clock = conn.execute("SELECT clock_timestamp()").fetchone()
    assert clock is not None
    now = clock[0]
    if now >= effective_activation_expiry(row[1], now, bool(settings and settings[0])):
        raise _activation_unavailable()

    conn.execute(
        "UPDATE software_licenses SET user_id = %s, bound_at = %s, authorized_until = %s "
        "WHERE id = %s",
        (user_id, now, add_authorization_years(now, row[2]), row[0]),
    )


def bind_legacy_device_license(
    conn: psycopg.Connection, *, user_id: str, code_id: str, now: datetime
) -> None:
    conn.execute(
        "INSERT INTO software_licenses "
        "(id, masked_code, authorization_years, user_id, bound_at, authorized_until) "
        "SELECT %s, masked_code, 1, %s, %s, %s FROM activation_codes WHERE id = %s",
        (str(uuid4()), user_id, now, add_authorization_years(now, 1), code_id),
    )


def software_license_expired(conn: psycopg.Connection, user_id: str) -> bool:
    # 子账号使用所属主账号的授权，管理员和内部账号仍走各自的准入规则。
    row = conn.execute(
        "SELECT l.authorized_until <= clock_timestamp() "
        "FROM users u JOIN software_licenses l ON l.user_id = COALESCE(u.parent_user_id, u.id) "
        "WHERE u.id = %s",
        (user_id,),
    ).fetchone()
    return bool(row and row[0])
