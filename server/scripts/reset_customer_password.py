"""Reset a password-enabled customer account from an operator terminal.

Run inside the API image with the production customer environment loaded:

    python -m scripts.reset_customer_password --username <exact-username>

The new password is entered twice with terminal echo disabled. It is hashed
with the customer login scheme, never printed or included in SQL parameters' logs,
and all existing sessions are revoked in the same transaction.
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys
import uuid
import warnings
from typing import cast

import psycopg

from app.admin_write_contract import transaction_now_iso
from app.customer_session_service import revoke_session
from app.db_pg import CliDatabaseConfigError, resolve_cli_pg_dsn
from app.password_hashing import PasswordPolicyError, hash_password
from app.sub_account_auth import password_login_account_ok


def _read_account(database_url: str, username: str) -> tuple[object, ...] | None:
    with psycopg.connect(database_url) as conn, conn.transaction():
        return conn.execute(
            "SELECT id, username, display_name, is_active, role, password_hash, "
            "registration_source, account_type, parent_user_id "
            "FROM users WHERE username = %s",
            (username,),
        ).fetchone()


def _check_password_lane(conn: psycopg.Connection, row: tuple[object, ...]) -> None:
    if not bool(row[3]) or row[4] != "customer":
        raise ValueError("target must be an active customer account")
    if not row[5]:
        raise ValueError("target has no password to reset; use the account setup flow")
    if not password_login_account_ok(
        conn,
        registration_source=cast(str | None, row[6]),
        account_type=cast(str | None, row[7]),
        parent_user_id=str(row[8]) if row[8] is not None else None,
    ):
        raise ValueError("target is not admitted to customer password login")


def reset_password(
    database_url: str, username: str, new_password: str, expected_user_id: str
) -> int:
    encoded = hash_password(new_password)
    request_id = str(uuid.uuid4())

    with psycopg.connect(database_url) as conn, conn.transaction():
        row = conn.execute(
            "SELECT id, username, display_name, is_active, role, password_hash, "
            "registration_source, account_type, parent_user_id "
            "FROM users WHERE username = %s FOR UPDATE",
            (username,),
        ).fetchone()
        if row is None:
            raise ValueError("account was not found; no changes were made")
        if str(row[0]) != expected_user_id:
            raise ValueError("账号身份已变化，已取消重置。")
        _check_password_lane(conn, row)

        user_id = str(row[0])
        live_sessions = conn.execute(
            "SELECT count(*) FROM customer_session_state WHERE user_id = %s",
            (user_id,),
        ).fetchone()
        sessions_revoked = int(live_sessions[0]) if live_sessions else 0

        conn.execute(
            "UPDATE users SET password_hash = %s, updated_at = NOW() WHERE id = %s",
            (encoded, user_id),
        )
        revoke_session(
            conn,
            user_id=user_id,
            actor_user_id=None,
            reason="admin_password_reset",
            request_id=request_id,
            now_iso=transaction_now_iso(conn),
        )
        conn.execute(
            "INSERT INTO audit_logs "
            "(id, actor_user_id, action, entity_type, entity_id, metadata_json) "
            "VALUES (%s, NULL, %s, %s, %s, %s)",
            (
                str(uuid.uuid4()),
                "customer.password.admin_reset",
                "user",
                user_id,
                json.dumps(
                    {"username": str(row[1]), "sessions_revoked": sessions_revoked},
                    ensure_ascii=True,
                    sort_keys=True,
                ),
            ),
        )

    return sessions_revoked


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--username", required=True, help="exact customer login username")
    parser.add_argument(
        "--database-url",
        default=None,
        help="explicit PostgreSQL DSN; defaults to the customer environment (CW-057)",
    )
    args = parser.parse_args(argv)

    try:
        database_url = resolve_cli_pg_dsn(args.database_url)
    except CliDatabaseConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    try:
        row = _read_account(database_url, args.username)
        if row is None:
            raise ValueError("account was not found; no changes were made")
        with psycopg.connect(database_url) as conn, conn.transaction():
            _check_password_lane(conn, row)

        print(f"Account: {row[1]} ({row[2]})")
        if input(f"Type {args.username!r} to confirm this account: ") != args.username:
            print("Confirmation did not match; no changes were made.", file=sys.stderr)
            return 2

        # 无法关闭回显时停止，避免 getpass 回退到明文输入。
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            password = getpass.getpass("新密码（6–128 个字符，输入不显示）：")
            confirmation = getpass.getpass("再次输入新密码：")
        if password != confirmation:
            print("Passwords do not match; no changes were made.", file=sys.stderr)
            return 2

        sessions_revoked = reset_password(database_url, args.username, password, str(row[0]))
    except (PasswordPolicyError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except (getpass.GetPassWarning, EOFError, KeyboardInterrupt):
        print("输入已取消或终端无法隐藏密码，未提交重置。", file=sys.stderr)
        return 2
    except Exception as exc:
        # 数据库错误可能携带行内容，只输出类型，避免泄露凭据。
        print(f"重置未确认成功，请核查后重试（{type(exc).__name__}）。", file=sys.stderr)
        return 1

    print(f"Password reset completed; revoked sessions: {sessions_revoked}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
