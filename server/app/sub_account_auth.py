"""子账号相关的共享准入规则。

历史上本模块还承载过 T2.8/T2.9 的 ``ensure_sub_account_access`` /
``prevent_cross_parent_access`` 等守卫，但路由侧从未接线（全仓零引用），
2026-10-31 冗余清理时移除；现仅保留登录与会话栅栏共用的密码通道准入判定。
"""

from __future__ import annotations

import psycopg

# ---------------------------------------------------------------------------
# Shared password-lane admission (login + session fence)
# ---------------------------------------------------------------------------

# Account origins that authenticate with a username + password of their own.
_PASSWORD_SOURCES = frozenset({"self_register", "activation_code"})


def password_login_account_ok(
    conn: psycopg.Connection,
    *,
    registration_source: str | None,
    account_type: str | None,
    parent_user_id: str | None,
) -> bool:
    """The shared admission rule for password-authenticated accounts.

    Used by both ``POST /api/customer/password-login`` and the session fence
    (``verify_session_context``) so login and every fenced write agree on who
    may hold a password session:

    - ``self_register`` / ``activation_code`` masters authenticate on their
      own credentials (the pre-sub-account lanes);
    - a sub-account is admitted only with an admin-issued password
      (``registration_source='admin_create'``, written by the sub-account
      creation/reset endpoints), a bound parent and an *active* master —
      deactivating the master freezes every session riding under it.
    """
    if registration_source in _PASSWORD_SOURCES:
        return True
    if (
        registration_source == "admin_create"
        and account_type in {"SUB", "SUB_ADMIN"}
        and parent_user_id is not None
    ):
        parent = conn.execute(
            "SELECT is_active FROM users WHERE id = %s", (parent_user_id,)
        ).fetchone()
        return parent is not None and bool(parent[0])
    return False
