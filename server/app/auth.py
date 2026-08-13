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
    if not dev_user_id:
        raise HTTPException(
            status_code=401,
            detail={
                "code": "AUTH_REQUIRED",
                "message": "Internal dev login requires X-Dev-User-Id.",
            },
        )

    row = conn.execute(
        """
        SELECT id, username, display_name, role
        FROM users
        WHERE id = ? AND is_active = 1
        """,
        (dev_user_id,),
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
