from __future__ import annotations

import argparse
import json
import secrets
import sqlite3
from pathlib import Path
from uuid import uuid4

from app.auth import VALID_ROLES, digest_access_token
from app.db import initialize_database


def create_user(
    conn: sqlite3.Connection,
    *,
    username: str,
    display_name: str,
    role: str = "employee",
    user_id: str | None = None,
) -> dict[str, str | bool]:
    normalized_username = username.strip()
    normalized_display_name = display_name.strip()
    if not normalized_username:
        raise ValueError("username is required")
    if not normalized_display_name:
        raise ValueError("display_name is required")
    if role not in VALID_ROLES:
        raise ValueError(f"unsupported role: {role}")

    resolved_user_id = user_id or str(uuid4())
    with conn:
        conn.execute(
            """
            INSERT INTO users (id, username, display_name, role)
            VALUES (?, ?, ?, ?)
            """,
            (resolved_user_id, normalized_username, normalized_display_name, role),
        )
        conn.execute("INSERT INTO wallets (user_id) VALUES (?)", (resolved_user_id,))
    return {
        "user_id": resolved_user_id,
        "username": normalized_username,
        "display_name": normalized_display_name,
        "role": role,
    }


def issue_token(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    raw_token: str | None = None,
    token_id: str | None = None,
) -> dict[str, str | bool]:
    user = conn.execute(
        "SELECT id FROM users WHERE id = ? AND is_active = 1", (user_id,)
    ).fetchone()
    if user is None:
        raise ValueError("active user does not exist")

    token = raw_token or secrets.token_urlsafe(32)
    resolved_token_id = token_id or str(uuid4())
    with conn:
        conn.execute(
            """
            INSERT INTO internal_access_tokens (id, user_id, token_digest)
            VALUES (?, ?, ?)
            """,
            (resolved_token_id, user_id, digest_access_token(token)),
        )
    return {"token_id": resolved_token_id, "user_id": user_id, "token": token}


def revoke_token(conn: sqlite3.Connection, *, token_id: str) -> dict[str, str | bool]:
    with conn:
        cursor = conn.execute(
            """
            UPDATE internal_access_tokens
            SET revoked_at = COALESCE(revoked_at, CURRENT_TIMESTAMP)
            WHERE id = ?
            """,
            (token_id,),
        )
    if cursor.rowcount != 1:
        raise ValueError("token does not exist")
    return {"revoked": True, "token_id": token_id}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage internal P0 users and access tokens")
    parser.add_argument("--db-path", required=True, type=Path)
    commands = parser.add_subparsers(dest="command", required=True)

    create = commands.add_parser("create-user", help="Create an internal user and empty wallet")
    create.add_argument("--username", required=True)
    create.add_argument("--display-name", required=True)
    create.add_argument("--role", choices=sorted(VALID_ROLES), default="employee")
    create.add_argument("--user-id")

    issue = commands.add_parser("issue-token", help="Issue a token shown only in this output")
    issue.add_argument("--user-id", required=True)

    revoke = commands.add_parser("revoke-token", help="Revoke a token by its public id")
    revoke.add_argument("--token-id", required=True)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    with initialize_database(args.db_path) as conn:
        if args.command == "create-user":
            result = create_user(
                conn,
                username=args.username,
                display_name=args.display_name,
                role=args.role,
                user_id=args.user_id,
            )
        elif args.command == "issue-token":
            result = issue_token(conn, user_id=args.user_id)
        else:
            result = revoke_token(conn, token_id=args.token_id)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
