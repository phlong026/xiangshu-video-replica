from __future__ import annotations

import hashlib
import os
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal, cast

from fastapi import Depends, Header, HTTPException

from app.db import connect_database

Role = Literal["employee", "admin", "auditor"]
VALID_ROLES: set[str] = {"employee", "admin", "auditor"}
DESKTOP_USER_ID_ENV = "VIDEO_REPLICA_DESKTOP_USER_ID"
ALLOW_DEV_IDENTITY_HEADER_ENV = "VIDEO_REPLICA_ALLOW_DEV_IDENTITY_HEADER"
AUTH_MODE_ENV = "VIDEO_REPLICA_AUTH_MODE"
INTERNAL_AUTH_MODES = {"internal", "internal_token"}


@dataclass(frozen=True)
class CurrentUser:
    id: str
    username: str
    display_name: str
    role: Role


def get_database() -> Iterator[sqlite3.Connection]:
    db_path = os.environ.get("VIDEO_REPLICA_DB_PATH")
    if not db_path:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "DATABASE_NOT_CONFIGURED",
                "message": "VIDEO_REPLICA_DB_PATH is required for API requests.",
            },
        )

    conn = connect_database(Path(db_path))
    try:
        yield conn
    finally:
        conn.close()


Database = Annotated[sqlite3.Connection, Depends(get_database)]


def get_current_user(
    conn: Database,
    dev_user_id: Annotated[str | None, Header(alias="X-Dev-User-Id")] = None,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> CurrentUser:
    return authenticate_request(
        conn,
        authorization=authorization,
        dev_user_id=dev_user_id,
    )


def authenticate_request(
    conn: sqlite3.Connection,
    *,
    authorization: str | None,
    dev_user_id: str | None,
) -> CurrentUser:
    if internal_auth_required():
        if authorization is not None:
            return authenticate_access_token(conn, parse_bearer_token(authorization))
        raise HTTPException(
            status_code=401,
            detail={
                "code": "AUTH_TOKEN_REQUIRED",
                "message": "A valid internal Bearer token is required.",
            },
        )
    return authenticate_user(conn, identity_user_id(dev_user_id))


def parse_bearer_token(authorization: str) -> str:
    scheme, separator, token = authorization.strip().partition(" ")
    if (
        separator != " "
        or scheme.lower() != "bearer"
        or not token
        or any(character.isspace() for character in token)
    ):
        raise invalid_token_error()
    return token


def digest_access_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def authenticate_access_token(conn: sqlite3.Connection, token: str) -> CurrentUser:
    row = conn.execute(
        """
        SELECT user_id
        FROM internal_access_tokens
        WHERE token_digest = ? AND revoked_at IS NULL
        """,
        (digest_access_token(token),),
    ).fetchone()
    if row is None:
        raise invalid_token_error()
    return authenticate_user(conn, str(row["user_id"]))


def invalid_token_error() -> HTTPException:
    return HTTPException(
        status_code=401,
        detail={"code": "AUTH_INVALID_TOKEN", "message": "Bearer token is invalid or revoked."},
    )


def internal_auth_required() -> bool:
    return os.environ.get(AUTH_MODE_ENV, "").lower() in INTERNAL_AUTH_MODES


def authenticate_user(conn: sqlite3.Connection, user_id: str | None) -> CurrentUser:
    if not user_id:
        raise HTTPException(
            status_code=401,
            detail={
                "code": "AUTH_DESKTOP_IDENTITY_REQUIRED",
                "message": "VIDEO_REPLICA_DESKTOP_USER_ID is required for desktop API requests.",
            },
        )

    row = conn.execute(
        """
        SELECT id, username, display_name, role
        FROM users
        WHERE id = ? AND is_active = 1
        """,
        (user_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(
            status_code=401,
            detail={"code": "AUTH_INVALID", "message": "User is missing or inactive."},
        )

    role = str(row["role"])
    if role not in VALID_ROLES:
        raise HTTPException(
            status_code=403,
            detail={"code": "ROLE_INVALID", "message": "User role is not supported."},
        )

    return CurrentUser(
        id=str(row["id"]),
        username=str(row["username"]),
        display_name=str(row["display_name"]),
        role=cast(Role, role),
    )


AuthenticatedUser = Annotated[CurrentUser, Depends(get_current_user)]


def identity_user_id(dev_user_id: str | None) -> str | None:
    desktop_user_id = os.environ.get(DESKTOP_USER_ID_ENV)
    if desktop_user_id:
        return desktop_user_id
    if os.environ.get(ALLOW_DEV_IDENTITY_HEADER_ENV) == "1":
        return dev_user_id
    return None


def identity_source(dev_user_id: str | None, authorization: str | None = None) -> str:
    if internal_auth_required():
        if authorization is not None:
            return "bearer"
        return "none"
    if os.environ.get(DESKTOP_USER_ID_ENV):
        return "desktop"
    if os.environ.get(ALLOW_DEV_IDENTITY_HEADER_ENV) == "1" and dev_user_id:
        return "dev_header"
    return "none"
