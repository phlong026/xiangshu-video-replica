from __future__ import annotations

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
) -> CurrentUser:
    return authenticate_user(conn, identity_user_id(dev_user_id))


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


def identity_source(dev_user_id: str | None) -> str:
    if os.environ.get(DESKTOP_USER_ID_ENV):
        return "desktop"
    if os.environ.get(ALLOW_DEV_IDENTITY_HEADER_ENV) == "1" and dev_user_id:
        return "dev_header"
    return "none"
