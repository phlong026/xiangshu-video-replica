from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict

from app.auth import AuthenticatedUser, Database
from app.permissions import (
    project_id_for_task,
    require_asset_access,
    require_not_auditor,
    require_project_access,
    require_role,
    write_audit,
)

router = APIRouter(prefix="/api", tags=["rbac"])


class UserResponse(BaseModel):
    id: str
    username: str
    display_name: str
    role: str


class AcceptedResponse(BaseModel):
    status: str


class AssetResponse(BaseModel):
    id: str
    project_id: str
    kind: str
    sha256: str
    size_bytes: int
    content_type: str | None


class DownloadUrlResponse(BaseModel):
    url: str


class AuditLogResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    actor_user_id: str | None
    action: str
    entity_type: str
    entity_id: str
    metadata_json: str
    created_at: str


@router.get("/auth/me", response_model=UserResponse)
def read_me(actor: AuthenticatedUser) -> UserResponse:
    return UserResponse(
        id=actor.id,
        username=actor.username,
        display_name=actor.display_name,
        role=actor.role,
    )


@router.patch("/admin/provider-settings", response_model=AcceptedResponse)
def update_provider_settings(
    payload: dict[str, Any],
    conn: Database,
    actor: AuthenticatedUser,
) -> AcceptedResponse:
    require_role(
        conn,
        actor=actor,
        allowed_roles={"admin"},
        action="provider_settings.update",
        entity_type="provider_settings",
        entity_id="global",
    )
    write_audit(
        conn,
        actor=actor,
        action="provider_settings.update",
        entity_type="provider_settings",
        entity_id="global",
        metadata={"fields": sorted(payload.keys())},
    )
    return AcceptedResponse(status="accepted")


@router.get("/projects/{project_id}", response_model=dict[str, str])
def read_project(
    project_id: str,
    conn: Database,
    actor: AuthenticatedUser,
) -> dict[str, str]:
    row = require_project_access(conn, actor=actor, project_id=project_id, action="project.read")
    return {
        "id": str(row["id"]),
        "owner_user_id": str(row["owner_user_id"]),
        "name": str(row["name"]),
        "status": str(row["status"]),
    }


@router.get("/assets/{asset_id}", response_model=AssetResponse)
def read_asset(
    asset_id: str,
    conn: Database,
    actor: AuthenticatedUser,
) -> AssetResponse:
    row = require_asset_access(conn, actor=actor, asset_id=asset_id, action="asset.read")
    return asset_response(row)


@router.post("/assets/{asset_id}/download-url", response_model=DownloadUrlResponse)
def create_download_url(
    asset_id: str,
    conn: Database,
    actor: AuthenticatedUser,
) -> DownloadUrlResponse:
    require_not_auditor(
        conn,
        actor=actor,
        action="asset.download_url.create",
        entity_type="asset",
        entity_id=asset_id,
    )
    row = require_asset_access(
        conn,
        actor=actor,
        asset_id=asset_id,
        action="asset.download_url.create",
    )
    write_audit(
        conn,
        actor=actor,
        action="asset.download_url.create",
        entity_type="asset",
        entity_id=asset_id,
        metadata={"project_id": str(row["project_id"])},
    )
    return DownloadUrlResponse(url=f"internal-dev://assets/{asset_id}/download")


@router.post("/projects/{project_id}/generation-batches", response_model=AcceptedResponse)
def create_generation_batch(
    project_id: str,
    conn: Database,
    actor: AuthenticatedUser,
) -> AcceptedResponse:
    require_not_auditor(
        conn,
        actor=actor,
        action="generation_batch.create",
        entity_type="project",
        entity_id=project_id,
    )
    require_project_access(
        conn,
        actor=actor,
        project_id=project_id,
        action="generation_batch.create",
    )
    write_audit(
        conn,
        actor=actor,
        action="generation_batch.create",
        entity_type="project",
        entity_id=project_id,
    )
    return AcceptedResponse(status="accepted")


@router.post("/generation-tasks/{task_id}/retry", response_model=AcceptedResponse)
def retry_generation_task(
    task_id: str,
    conn: Database,
    actor: AuthenticatedUser,
) -> AcceptedResponse:
    require_not_auditor(
        conn,
        actor=actor,
        action="generation_task.retry",
        entity_type="generation_task",
        entity_id=task_id,
    )
    project_id = project_id_for_task(conn, task_id)
    require_project_access(
        conn,
        actor=actor,
        project_id=project_id,
        action="generation_task.retry",
    )
    write_audit(
        conn,
        actor=actor,
        action="generation_task.retry",
        entity_type="generation_task",
        entity_id=task_id,
        metadata={"project_id": project_id},
    )
    return AcceptedResponse(status="accepted")


@router.get("/audit-logs", response_model=list[AuditLogResponse])
def read_audit_logs(
    conn: Database,
    actor: AuthenticatedUser,
) -> list[AuditLogResponse]:
    require_role(
        conn,
        actor=actor,
        allowed_roles={"admin", "auditor"},
        action="audit_log.read",
        entity_type="audit_log",
        entity_id="collection",
    )
    rows = conn.execute(
        """
        SELECT id, actor_user_id, action, entity_type, entity_id, metadata_json, created_at
        FROM audit_logs
        ORDER BY created_at, id
        """
    ).fetchall()
    return [audit_log_response(row) for row in rows]


def asset_response(row: sqlite3.Row) -> AssetResponse:
    return AssetResponse(
        id=str(row["id"]),
        project_id=str(row["project_id"]),
        kind=str(row["kind"]),
        sha256=str(row["sha256"]),
        size_bytes=int(row["size_bytes"]),
        content_type=None if row["content_type"] is None else str(row["content_type"]),
    )


def audit_log_response(row: sqlite3.Row) -> AuditLogResponse:
    return AuditLogResponse(
        id=str(row["id"]),
        actor_user_id=None if row["actor_user_id"] is None else str(row["actor_user_id"]),
        action=str(row["action"]),
        entity_type=str(row["entity_type"]),
        entity_id=str(row["entity_id"]),
        metadata_json=str(row["metadata_json"]),
        created_at=str(row["created_at"]),
    )
