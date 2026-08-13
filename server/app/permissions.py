from __future__ import annotations

import json
import sqlite3
from typing import Any, cast
from uuid import uuid4

from fastapi import HTTPException

from app.auth import CurrentUser, Role


def write_audit(
    conn: sqlite3.Connection,
    *,
    actor: CurrentUser,
    action: str,
    entity_type: str,
    entity_id: str,
    metadata: dict[str, Any] | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO audit_logs (id, actor_user_id, action, entity_type, entity_id, metadata_json)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            str(uuid4()),
            actor.id,
            action,
            entity_type,
            entity_id,
            json.dumps(metadata or {}, ensure_ascii=True, sort_keys=True),
        ),
    )
    conn.commit()


def require_role(
    conn: sqlite3.Connection,
    *,
    actor: CurrentUser,
    allowed_roles: set[Role],
    action: str,
    entity_type: str,
    entity_id: str,
) -> None:
    if actor.role in allowed_roles:
        return

    write_audit(
        conn,
        actor=actor,
        action="security.role_denied",
        entity_type=entity_type,
        entity_id=entity_id,
        metadata={"attempted_action": action, "required_roles": sorted(allowed_roles)},
    )
    raise forbidden(
        "ROLE_FORBIDDEN",
        f"{actor.role} is not allowed to perform {action}.",
    )


def require_not_auditor(
    conn: sqlite3.Connection,
    *,
    actor: CurrentUser,
    action: str,
    entity_type: str,
    entity_id: str,
) -> None:
    require_role(
        conn,
        actor=actor,
        allowed_roles={"employee", "admin"},
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
    )


def require_project_access(
    conn: sqlite3.Connection,
    *,
    actor: CurrentUser,
    project_id: str,
    action: str,
) -> sqlite3.Row:
    row = conn.execute(
        """
        SELECT id, owner_user_id, name, status
        FROM projects
        WHERE id = ?
        """,
        (project_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "PROJECT_NOT_FOUND", "message": "Project does not exist."},
        )

    if actor.role == "admin" or str(row["owner_user_id"]) == actor.id:
        return cast(sqlite3.Row, row)

    write_audit(
        conn,
        actor=actor,
        action="security.project_denied",
        entity_type="project",
        entity_id=project_id,
        metadata={"attempted_action": action},
    )
    raise forbidden(
        "PROJECT_FORBIDDEN",
        "User is not the project owner or an allowed project team member.",
    )


def require_asset_access(
    conn: sqlite3.Connection,
    *,
    actor: CurrentUser,
    asset_id: str,
    action: str,
) -> sqlite3.Row:
    row = conn.execute(
        """
        SELECT id, project_id, kind, storage_uri, sha256, size_bytes, content_type
        FROM assets
        WHERE id = ?
        """,
        (asset_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "ASSET_NOT_FOUND", "message": "Asset does not exist."},
        )

    require_project_access(conn, actor=actor, project_id=str(row["project_id"]), action=action)
    return cast(sqlite3.Row, row)


def project_id_for_task(conn: sqlite3.Connection, task_id: str) -> str:
    row = conn.execute(
        """
        SELECT generation_batches.project_id
        FROM generation_tasks
        JOIN generation_batches ON generation_batches.id = generation_tasks.batch_id
        WHERE generation_tasks.id = ?
        """,
        (task_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "TASK_NOT_FOUND", "message": "Generation task does not exist."},
        )
    return str(row["project_id"])


def forbidden(code: str, message: str) -> HTTPException:
    return HTTPException(status_code=403, detail={"code": code, "message": message})
