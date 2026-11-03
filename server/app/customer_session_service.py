"""Independent customer device sessions with PostgreSQL leases and fencing.

Each device owns one session row, token, monotonic epoch and a 7-day sliding
expiry (``SESSION_VALIDITY_SECONDS``). Login or recovery on one device never
changes another device's session. The legacy switch endpoint remains compatible
and follows the same policy. Expiry slides forward on every verified request
(``renew_session_expiry``) and on the legacy heartbeat endpoint, which is kept
for older clients; logout expires only that session. Administrator account
revocation can revoke all matching sessions, while device revocation is scoped
to its own session. Events remain append-only and tokens are stored as keyed
digests. Routes use the PostgreSQL clock.
"""

from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg

from app.customer_device_service import (
    _token_digests,
    highest_device_domain_key,
    keyed_digest,
)

# 会话有效期（2026-11-02 契约变更，替代 SES-01 的「心跳 30 秒 / 租约 90 秒」）：
# 登录、激活与每个通过 fence 校验的请求都把 lease_until 滑动到 now + 7 天。
# 旧契约把任何 ≥90 秒的服务端断供（连接池耗尽、进程重启、PG 抖动）放大成
# 全体用户终态登出；长滑动有效期下同类故障只表现为短暂请求失败，恢复后
# 用户无感续用。旧版客户端仍在调 heartbeat 端点，该端点保留并同样按 7 天续期。
SESSION_VALIDITY_SECONDS = 7 * 24 * 3600
# 滑动续期的节流窗口：仅当 lease_until 落后 now + (7 天 - 1 小时) 时才写库，
# 保证每个会话至多每小时一次 UPDATE，而不是每个请求一次写。
SESSION_RENEWAL_WRITE_INTERVAL_SECONDS = 3600
# 活动时间（last_seen_at）的独立节流窗口：必须小于管理端在线判定的 10 分钟
# 窗口（ONLINE_SESSION_PREDICATE），否则持续发请求的活跃用户在两次续期之间
# 会被判成离线（2026-11-03 Codex 回评 r1/r6 双确认的 P1）。
SESSION_ACTIVITY_WRITE_INTERVAL_SECONDS = 300

# 管理端「会话/设备在线」判定 SQL（会话表别名固定为 ``css``）。7 天滑动
# 契约下 lease_until 长期恒真，不能再当「在线」用；在线 = 会话仍有效
# （登出/吊销仍把 lease 拉回过去，必须保留该条件）且最近 10 分钟有活动。
# 活动时间依次回退：last_seen_at（新契约写）→ last_heartbeat_at（旧客户端
# 心跳写）→ lease_until - 7 天（≈最近一次续期时刻，兜底历史行）。
ONLINE_SESSION_PREDICATE = (
    "css.lease_until::timestamptz > clock_timestamp() "
    "AND COALESCE(css.last_seen_at::timestamptz, css.last_heartbeat_at::timestamptz, "
    "css.lease_until::timestamptz - interval '7 days') "
    "> clock_timestamp() - interval '10 minutes'"
)

# Login state-machine outcomes.
LOGIN_CREATED = "created"  # new session established (no row / lapsed / recovery)
LOGIN_RENEWED = "renewed"  # same device + valid token: renewed in place
LOGIN_CONFLICT = "conflict"  # another device holds a live lease

# Heartbeat outcomes.
HEARTBEAT_OK = "ok"
HEARTBEAT_REPLACED = "replaced"
HEARTBEAT_EXPIRED = "expired"

# Logout outcomes.
LOGOUT_OK = "ok"
LOGOUT_REPLACED = "replaced"
LOGOUT_EXPIRED = "expired"

# The session-event reason recorded on user-initiated logout.
LOGOUT_REASON = "user_logout"

# The session-event reason recorded when an explicit switch replaces a live
# session (dev doc §12.3 fifth line).
SWITCH_REASON = "explicit_switch"


def mask_device_name(display_name: str) -> str:
    """Mask a device display name for safe client exposure (§13.2).

    At most the first two characters survive; anything shorter is a single
    asterisk. An empty name has nothing to leak and stays empty. The masked
    hint must never leak the full name.
    """
    if not display_name:
        return ""
    if len(display_name) <= 2:
        return "*"
    return display_name[:2] + "**"


@dataclass(frozen=True)
class LoginResult:
    """The outcome of one login attempt (§12.3)."""

    outcome: str
    session_token: str | None
    session_id: str
    session_epoch: int
    lease_until: str
    # Conflict extras (masked hint + remaining lease, dev doc §13.2).
    online_device_name_masked: str | None = None
    online_slot_no: int | None = None
    online_lease_expires_at: str | None = None


@dataclass(frozen=True)
class HeartbeatResult:
    """The outcome of one heartbeat attempt."""

    outcome: str
    session_id: str | None = None
    session_epoch: int | None = None
    lease_until: str | None = None


@dataclass(frozen=True)
class LogoutResult:
    """The outcome of one logout attempt."""

    outcome: str
    session_id: str | None = None
    session_epoch: int | None = None


# ---------------------------------------------------------------------------
# Row helpers
# ---------------------------------------------------------------------------

# SELECT column order shared by login/heartbeat/logout row probes:
# (user_id, activation_code_id, session_id, session_epoch, lease_until,
#  device_id, token_digest)
_ROW_SQL = (
    "SELECT user_id, activation_code_id, session_id, session_epoch, "
    "lease_until, device_id, token_digest FROM customer_session_state"
)


def _as_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _write_event(
    conn: psycopg.Connection,
    *,
    event: str,
    user_id: str,
    activation_code_id: str | None,
    device_id: str,
    session_id: str,
    session_epoch: int,
    request_id: str,
    actor_user_id: str | None = None,
    reason: str | None = None,
) -> None:
    """Append one row to the append-only session audit trail."""
    conn.execute(
        "INSERT INTO customer_session_events "
        "(id, event, user_id, activation_code_id, device_id, session_id, "
        " session_epoch, actor_user_id, reason, request_id) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            str(uuid.uuid4()),
            event,
            user_id,
            activation_code_id,
            device_id,
            session_id,
            session_epoch,
            actor_user_id,
            reason,
            request_id,
        ),
    )


# ---------------------------------------------------------------------------
# Login (dev doc §12.3)
# ---------------------------------------------------------------------------


def login_session(
    conn: psycopg.Connection,
    *,
    user_id: str,
    activation_code_id: str | None,
    device_id: str,
    presentation_session_token: str | None,
    request_id: str,
    now: datetime,
    takeover: bool = False,
) -> LoginResult:
    """Establish or renew a session for this device only.

    Different devices never replace one another. The legacy takeover parameter
    remains accepted for older clients, with no cross-device effect.
    """
    # SES-01: the caller must supply the PostgreSQL server clock — a missing
    # clock must fail closed instead of silently drifting to the host clock.
    now_full = now
    now_iso = now_full.replace(microsecond=0).isoformat()

    row = conn.execute(
        f"{_ROW_SQL} WHERE user_id = %s AND device_id = %s FOR UPDATE",
        (user_id, device_id),
    ).fetchone()
    if row is None:
        # Defensive branch: activation always inserts the row, but a lost row
        # must still yield a sound epoch-1 session instead of a hard failure.
        # Two concurrent first-writers race on the PK: ON CONFLICT DO NOTHING
        # (inside _establish) hands the loser the winner's committed row to
        # re-drive the state machine — never a UniqueViolation 500. No
        # production path deletes session rows, so a lost race always
        # re-reads a row; the retry bound is pure paranoia.
        for _attempt in range(3):
            established = _establish(
                conn,
                device_id=device_id,
                user_id=user_id,
                activation_code_id=activation_code_id,
                old_row=None,
                now=now_full,
                request_id=request_id,
            )
            if established is not None:
                return established
            row = conn.execute(
                f"{_ROW_SQL} WHERE user_id = %s AND device_id = %s FOR UPDATE",
                (user_id, device_id),
            ).fetchone()
            if row is not None:
                break
        else:
            raise RuntimeError(
                "customer_session_state row vanished during a defensive first-write race"
            )

    user_id = str(row[0])
    activation_code_id = str(row[1]) if row[1] is not None else None
    session_id = str(row[2])
    session_epoch = int(row[3])
    lease_until_raw = str(row[4])
    token_digest = str(row[6])
    lease_until = _as_utc(lease_until_raw)

    lapsed = now_full > lease_until

    presented_digests = (
        _token_digests(presentation_session_token) if presentation_session_token else []
    )
    token_matches = any(digest == token_digest for digest in presented_digests)

    if token_matches and not lapsed:
        # Renewal only: same token, same epoch, expiry slid out to now + 7 days.
        # 租约防回缩（2026-11-03 回评 r1 P1）：取现值与新值较大者——时钟回拨或
        # 异常路径写入更短有效期时不得缩短已授予的会话；logout/吊销走独立
        # 语句保持缩短语义。
        lease_until_iso = (
            max(lease_until, now_full + timedelta(seconds=SESSION_VALIDITY_SECONDS))
            .replace(microsecond=0)
            .isoformat()
        )
        # last_seen_at 的 GREATEST 兜底沿袭 logout 的 lease 写法：now() 是事务
        # 起点时钟，一个早于行创建才开始的事务在行锁等待后拿到锁时，其
        # now() 可能早于 created_at，裸写会触发 last_seen_not_before_created。
        conn.execute(
            "UPDATE customer_session_state "
            "SET lease_until = GREATEST(lease_until::timestamptz, %s::timestamptz), "
            "last_seen_at = GREATEST(%s::timestamptz, "
            "created_at::timestamptz + interval '1 microsecond'), "
            "updated_at = %s WHERE session_id = %s",
            (lease_until_iso, now_full.isoformat(), now_iso, session_id),
        )
        _write_event(
            conn,
            event="LOGIN",
            user_id=user_id,
            activation_code_id=activation_code_id,
            device_id=device_id,
            session_id=session_id,
            session_epoch=session_epoch,
            request_id=request_id,
            actor_user_id=user_id,
        )
        return LoginResult(
            outcome=LOGIN_RENEWED,
            session_token=presentation_session_token,
            session_id=session_id,
            session_epoch=session_epoch,
            lease_until=lease_until_iso,
        )
    # Same device, unusable token (or lapsed): the recovery path — epoch
    # + 1, fresh token. A lapsed prior lease records the TIMEOUT event.
    return _require_established(
        _establish(
            conn,
            device_id=device_id,
            user_id=user_id,
            activation_code_id=activation_code_id,
            old_row=row,
            lapsed=lapsed,
            now=now_full,
            request_id=request_id,
        )
    )


def _require_established(result: LoginResult | None) -> LoginResult:
    """Narrow ``_establish``: only the defensive first-write path answers None."""
    if result is None:
        raise RuntimeError("unreachable: establish returned None with a locked row")
    return result


def _establish(
    conn: psycopg.Connection,
    *,
    device_id: str,
    user_id: str,
    activation_code_id: str | None,
    old_row: tuple[Any, ...] | None,
    now: datetime,
    request_id: str,
    lapsed: bool = False,
) -> LoginResult | None:
    """Establish (create or bump) the caller's session and audit it.

    ``old_row`` is the locked pre-existing row (``None`` = the defensive
    first-session insert). A lapsed prior lease appends the system ``TIMEOUT``
    event before the fresh ``LOGIN`` event. The defensive insert answers
    ``None`` when a concurrent first-writer won the PK — the caller re-reads
    the winner's row and re-drives the state machine (never a 500).
    """
    now_iso = now.replace(microsecond=0).isoformat()
    # last_seen_at 保留微秒精度：与 last_heartbeat_at 同理，整秒截断可能在
    # 同秒写入时落到 created_at 之前而触发 last_seen_not_before_created CHECK。
    now_seen_iso = now.isoformat()
    lease_until_iso = (
        (now + timedelta(seconds=SESSION_VALIDITY_SECONDS)).replace(microsecond=0).isoformat()
    )

    session_token = secrets.token_urlsafe(32)
    version, key = highest_device_domain_key()
    token_digest = keyed_digest(key, session_token)
    session_id = str(uuid.uuid4())

    if old_row is None:
        new_epoch = 1
        inserted = conn.execute(
            "INSERT INTO customer_session_state "
            "(user_id, activation_code_id, device_id, session_id, token_digest, "
            " session_epoch, lease_until, last_seen_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (device_id) DO NOTHING",
            (
                user_id,
                activation_code_id,
                device_id,
                session_id,
                token_digest,
                new_epoch,
                lease_until_iso,
                now_seen_iso,
            ),
        )
        if inserted.rowcount == 0:
            # Lost the concurrent first-write race: the winner's committed
            # row exists — the caller re-reads it and re-drives the state
            # machine instead of dying on UniqueViolation.
            return None
    else:
        old_session_id = str(old_row[2])
        old_epoch = int(old_row[3])
        new_epoch = old_epoch + 1
        conn.execute(
            "UPDATE customer_session_state "
            "SET device_id = %s, session_id = %s, token_digest = %s, "
            "session_epoch = %s, lease_until = %s, last_heartbeat_at = NULL, "
            "last_seen_at = GREATEST(%s::timestamptz, "
            "created_at::timestamptz + interval '1 microsecond'), "
            "updated_at = %s WHERE session_id = %s",
            (
                device_id,
                session_id,
                token_digest,
                new_epoch,
                lease_until_iso,
                now_seen_iso,
                now_iso,
                old_session_id,
            ),
        )
        if lapsed:
            # System-driven timeout of the prior lease: no acting user. The
            # event describes the *timed-out* session, so it carries the old
            # row's binding (device id, activation code, session, epoch).
            _write_event(
                conn,
                event="TIMEOUT",
                user_id=user_id,
                activation_code_id=str(old_row[1]) if old_row[1] is not None else None,
                device_id=str(old_row[5]),
                session_id=old_session_id,
                session_epoch=old_epoch,
                request_id=request_id,
            )

    _write_event(
        conn,
        event="LOGIN",
        user_id=user_id,
        activation_code_id=activation_code_id,
        device_id=device_id,
        session_id=session_id,
        session_epoch=new_epoch,
        request_id=request_id,
        actor_user_id=user_id,
    )
    return LoginResult(
        outcome=LOGIN_CREATED,
        session_token=session_token,
        session_id=session_id,
        session_epoch=new_epoch,
        lease_until=lease_until_iso,
    )


# ---------------------------------------------------------------------------
# Heartbeat (dev doc §12.3 same-device lease extension) —— 旧客户端兼容端点。
# 新客户端不再心跳：会话由 fence 校验时的 renew_session_expiry 滑动续期。
# ---------------------------------------------------------------------------


def heartbeat_session(
    conn: psycopg.Connection,
    *,
    presentation_session_token: str,
    request_id: str,
    now: datetime,
) -> HeartbeatResult:
    """Renew the live session lease; refuse stale and lapsed tokens.

    The row is located by the presented token's digests (probed across every
    configured key version) and locked: either the token still owns the row
    or it does not. A matching row under a lapsed lease is terminal — the
    lease is never resurrected (acceptance §3.4).
    """
    now_full = now

    digests = _token_digests(presentation_session_token)
    row = conn.execute(
        f"{_ROW_SQL} WHERE token_digest = ANY(%s) LIMIT 1 FOR UPDATE",
        (digests,),
    ).fetchone()
    if row is None:
        # The token never resolved to the live row: replaced (or forged — one
        # answer, no oracle).
        return HeartbeatResult(outcome=HEARTBEAT_REPLACED)

    user_id = str(row[0])
    activation_code_id = str(row[1]) if row[1] is not None else None
    session_id = str(row[2])
    session_epoch = int(row[3])
    lease_until_raw = str(row[4])
    device_id = str(row[5])

    if now_full > _as_utc(lease_until_raw):
        # Lapsed: terminal. No event, no resurrection.
        return HeartbeatResult(
            outcome=HEARTBEAT_EXPIRED,
            session_id=session_id,
            session_epoch=session_epoch,
        )

    lease_until_iso = (
        max(
            _as_utc(lease_until_raw),
            now_full + timedelta(seconds=SESSION_VALIDITY_SECONDS),
        )
        .replace(microsecond=0)
        .isoformat()
    )
    # PR #47 Codex review P2 (the unbind lesson): keep the PostgreSQL
    # microsecond precision — a whole-second trim lands a same-second
    # heartbeat before created_at and trips the heartbeat_not_before_created
    # CHECK. last_seen_at 同理保留微秒，并叠 GREATEST 兜底（事务起点时钟在
    # 行锁等待后可能早于 created_at，见 login_session 续租处的同款注释）。
    # 租约防回缩与 login 续租同款：max(现值, 新值) + SQL GREATEST 双保险。
    now_full_iso = now_full.isoformat()
    conn.execute(
        "UPDATE customer_session_state "
        "SET lease_until = GREATEST(lease_until::timestamptz, %s::timestamptz), "
        "last_heartbeat_at = %s, "
        "last_seen_at = GREATEST(%s::timestamptz, "
        "created_at::timestamptz + interval '1 microsecond'), "
        "updated_at = %s WHERE session_id = %s",
        (lease_until_iso, now_full_iso, now_full_iso, now_full_iso, session_id),
    )
    _write_event(
        conn,
        event="HEARTBEAT",
        user_id=user_id,
        activation_code_id=activation_code_id,
        device_id=device_id,
        session_id=session_id,
        session_epoch=session_epoch,
        request_id=request_id,
        actor_user_id=user_id,
    )
    return HeartbeatResult(
        outcome=HEARTBEAT_OK,
        session_id=session_id,
        session_epoch=session_epoch,
        lease_until=lease_until_iso,
    )


# ---------------------------------------------------------------------------
# Sliding renewal (verify_session_context 的续期副作用)
# ---------------------------------------------------------------------------


def renew_session_expiry(
    conn: psycopg.Connection,
    *,
    session_id: str,
    lease_until: datetime,
    last_seen_at: datetime | None,
    now: datetime,
) -> str | None:
    """滑动有效期与活动时间的双节流写（供 fence 校验调用）。

    会话从 90 秒租约改为 7 天滑动有效后，「续命」从客户端心跳移到服务端：
    每个通过 :func:`~app.customer_auth.verify_session_context` 校验的请求都
    在同一事务内调用本函数。两个维度各自节流、互不绑定（2026-11-03 回评
    r1/r6 的 P1：旧实现共用 1 小时节流，未到续期阈值早退时活动时间不写，
    持续发请求的活跃用户在管理端 10 分钟在线窗口之外被判离线）：

    - 有效期续期：仅当 ``lease_until`` 落后 ``now + (7 天 - 1 小时)`` 才写
      ——至多每小时一次；
    - 活动时间：``last_seen_at`` 落后 ``now - 5 分钟`` 才写——活跃会话至多
      每 5 分钟一次，仍远低于逐请求写的负担，且始终落在在线窗口内。

    调用方已持有该行的 FOR UPDATE 锁（verify 取锁），这里只补 UPDATE，
    无二次锁竞争。租约写入取 ``max(现值, 新值)`` + SQL ``GREATEST`` 双保险
    （防回缩，与 login/heartbeat 同款）。last_seen_at 保留微秒并叠
    GREATEST 兜底，防同秒写入触发 last_seen_not_before_created CHECK。

    返回新的到期 ISO 串；有效期未动（仅活动写或零写）返回 ``None``。
    """
    renewal_needed = lease_until < now + timedelta(
        seconds=SESSION_VALIDITY_SECONDS - SESSION_RENEWAL_WRITE_INTERVAL_SECONDS
    )
    activity_needed = last_seen_at is None or last_seen_at < now - timedelta(
        seconds=SESSION_ACTIVITY_WRITE_INTERVAL_SECONDS
    )
    if not renewal_needed and not activity_needed:
        return None
    now_seen_iso = now.isoformat()
    now_second_iso = now.replace(microsecond=0).isoformat()
    if renewal_needed:
        new_expiry = max(lease_until, now + timedelta(seconds=SESSION_VALIDITY_SECONDS))
        new_expiry_iso = new_expiry.replace(microsecond=0).isoformat()
        conn.execute(
            "UPDATE customer_session_state "
            "SET lease_until = GREATEST(lease_until::timestamptz, %s::timestamptz), "
            "last_seen_at = GREATEST(%s::timestamptz, "
            "created_at::timestamptz + interval '1 microsecond'), "
            "updated_at = %s WHERE session_id = %s",
            (new_expiry_iso, now_seen_iso, now_second_iso, session_id),
        )
        return new_expiry_iso
    conn.execute(
        "UPDATE customer_session_state "
        "SET last_seen_at = GREATEST(%s::timestamptz, "
        "created_at::timestamptz + interval '1 microsecond'), "
        "updated_at = %s WHERE session_id = %s",
        (now_seen_iso, now_second_iso, session_id),
    )
    return None


def logout_session(
    conn: psycopg.Connection,
    *,
    presentation_session_token: str,
    request_id: str,
    now: datetime,
) -> LogoutResult:
    """Release the single-online slot by pulling the lease into the past.

    Mirrors the T16 unbind pattern: the lease lands in the past with the full
    transaction-clock precision (the PR #47 Codex review P2 lesson — trimming
    to whole seconds once left a released lease alive up to a second), the
    GREATEST backstop keeps the ``lease_after_created`` check satisfied, a
    ``LOGOUT`` event with reason ``user_logout`` lands on the audit trail,
    and the other device may log in immediately. A token that no longer owns
    the row answers ``replaced`` without touching the new session; a lapsed
    lease answers ``expired`` (nothing to release).
    """
    now_full = now

    digests = _token_digests(presentation_session_token)
    row = conn.execute(
        f"{_ROW_SQL} WHERE token_digest = ANY(%s) LIMIT 1 FOR UPDATE",
        (digests,),
    ).fetchone()
    if row is None:
        return LogoutResult(outcome=LOGOUT_REPLACED)

    user_id = str(row[0])
    activation_code_id = str(row[1]) if row[1] is not None else None
    session_id = str(row[2])
    session_epoch = int(row[3])
    lease_until_raw = str(row[4])
    device_id = str(row[5])

    if now_full > _as_utc(lease_until_raw):
        return LogoutResult(
            outcome=LOGOUT_EXPIRED,
            session_id=session_id,
            session_epoch=session_epoch,
        )

    now_full_iso = now_full.isoformat()
    conn.execute(
        "UPDATE customer_session_state "
        "SET lease_until = GREATEST(%s::timestamptz, "
        "created_at::timestamptz + interval '1 microsecond'), "
        "updated_at = %s WHERE session_id = %s",
        (now_full_iso, now_full_iso, session_id),
    )
    _write_event(
        conn,
        event="LOGOUT",
        user_id=user_id,
        activation_code_id=activation_code_id,
        device_id=device_id,
        session_id=session_id,
        session_epoch=session_epoch,
        request_id=request_id,
        actor_user_id=user_id,
        reason=LOGOUT_REASON,
    )
    return LogoutResult(
        outcome=LOGOUT_OK,
        session_id=session_id,
        session_epoch=session_epoch,
    )


# ---------------------------------------------------------------------------
# Explicit switch (dev doc §12.3 fifth line)
# ---------------------------------------------------------------------------


def switch_session(
    conn: psycopg.Connection,
    *,
    user_id: str,
    activation_code_id: str | None,
    device_id: str,
    presentation_session_token: str | None,
    request_id: str,
    now: datetime,
) -> LoginResult:
    """Compatibility entry point: log in this device without displacing others."""
    return login_session(
        conn,
        user_id=user_id,
        activation_code_id=activation_code_id,
        device_id=device_id,
        presentation_session_token=presentation_session_token,
        request_id=request_id,
        now=now,
        takeover=True,
    )


# ---------------------------------------------------------------------------
# Revocation propagation (T20 / SES-03)
# ---------------------------------------------------------------------------


# The session-event reasons recorded by the admin-driven revocations.
REASON_CODE_SUSPENDED = "code_suspended"
REASON_CODE_REVOKED = "code_revoked"

# The reasons recorded when a master reevaluates one of its sub-accounts
# (customer self-service lane): deactivation and credential rotation both
# terminate the sub's live session in the same transaction.
REASON_SUB_ACCOUNT_DEACTIVATED = "sub_account_deactivated"
REASON_SUB_ACCOUNT_PASSWORD_RESET = "sub_account_password_reset"

# The reason recorded when an account rotates its *own* password from the
# personal center: the rotation is the self-service leak response, so the
# live session it kills must say so in the audit trail.
REASON_PASSWORD_CHANGED = "password_changed"

# The reason recorded when an account pulls the "log me out of every device"
# lever on its own, without rotating the credential.
REASON_ALL_DEVICES_REVOKED = "all_devices_revoked"

# The reason recorded when a forgotten password is reset through an emailed
# code: whoever held the old session may be the reason the owner lost access.
REASON_PASSWORD_RESET = "password_reset"

# The session/device rows that must be purged before a customer row can be
# physically deleted: their ``user_id`` FKs carry no CASCADE. Order matters —
# the session state references the device, so it goes first. The session-event
# log is deliberately absent: ``customer_session_events`` is append-only (029 —
# the database refuses UPDATE/DELETE outright), so a session history is kept
# and pins the account the same way business history does — the DELETE
# degrades (customer lane) or answers the has-history conflict (admin lane)
# instead of rewriting the audit. Business history (ledger, tasks) is absent
# for the same reason.
SUB_ACCOUNT_FOOTPRINT_TABLES = (
    "customer_session_state",
    "customer_devices",
)


def revoke_session(
    conn: psycopg.Connection,
    *,
    user_id: str,
    actor_user_id: str | None = None,
    reason: str,
    request_id: str,
    now_iso: str,
    device_id: str | None = None,
) -> bool:
    """Terminate the user's live session atomically (SES-03 propagation).

    Epoch bump + lease pulled into the past + a ``LOGOUT`` event naming the
    acting user and the reason — one transaction, exactly the T16 unbind
    core generalized. The GREATEST backstop keeps the
    ``lease_after_created`` CHECK satisfied; one microsecond — not one
    second — preserves the "immediately expired" semantics (the PR #47
    Codex review P2 lesson). ``device_id`` scopes the revocation to the
    session riding that device (the T16/T18 unbind/revoke delegation);
    ``None`` revokes whatever session the user holds (the admin code
    suspend/revoke paths — a suspended, revoked or otherwise disabled
    account loses its session in the same transaction).

    Answers ``True`` when a session row was actually revoked; a user with
    no session row (never activated, or the row was lost) is a no-op —
    there was nothing to propagate.
    """
    where_clause = "WHERE user_id = %s"
    params: list[object] = [user_id]
    if device_id is not None:
        where_clause += " AND device_id = %s"
        params.append(device_id)
    session_rows = conn.execute(
        "UPDATE customer_session_state "
        "SET session_epoch = session_epoch + 1, "
        "lease_until = GREATEST(%s::timestamptz, "
        "created_at::timestamptz + interval '1 microsecond'), "
        "updated_at = %s "
        f"{where_clause} "
        "RETURNING activation_code_id, device_id, session_id, session_epoch",
        (now_iso, now_iso, *params),
    ).fetchall()
    for session_row in session_rows:
        _write_event(
            conn,
            event="LOGOUT",
            user_id=user_id,
            activation_code_id=str(session_row[0]) if session_row[0] is not None else None,
            device_id=str(session_row[1]),
            session_id=str(session_row[2]),
            session_epoch=int(session_row[3]),
            request_id=request_id,
            actor_user_id=actor_user_id,
            reason=reason,
        )
    return bool(session_rows)
