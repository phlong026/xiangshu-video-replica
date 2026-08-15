from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import timedelta
from typing import Annotated
from urllib.parse import quote
from uuid import uuid4

from fastapi import APIRouter, Header, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field

from app.auth import (
    AuthenticatedUser,
    Database,
    authenticate_user,
    identity_source,
    identity_user_id,
)
from app.media import storage_key_from_uri
from app.media_routes import LOCAL_API_BASE_URL
from app.permissions import (
    project_id_for_task,
    require_asset_access,
    require_not_auditor,
    require_project_access,
    require_role,
    write_audit,
)
from app.settings import SettingsDecryptError, SettingsKeyMissing, SettingsRepository
from app.storage import (
    LocalStorageAdapter,
    StorageAdapter,
    StorageBackendUnavailable,
    cloud_storage_config_from_settings,
    create_storage_adapter,
    local_download_signature,
    local_storage_root,
    require_storage_match,
    storage_object_ref_from_uri,
)

router = APIRouter(prefix="/api", tags=["rbac"])
DOWNLOAD_URL_EXPIRES_IN = timedelta(minutes=15)


class UserResponse(BaseModel):
    id: str
    username: str
    display_name: str
    role: str


class CreateProjectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)


class ProjectResponse(BaseModel):
    id: str
    owner_user_id: str
    name: str
    status: str
    reference_asset_id: str | None
    reference_upload_status: str


class AcceptedResponse(BaseModel):
    status: str


class AssetResponse(BaseModel):
    id: str
    project_id: str | None
    kind: str
    sha256: str
    size_bytes: int
    content_type: str | None


class DownloadUrlResponse(BaseModel):
    url: str


def storage_for_asset(conn: sqlite3.Connection, storage_uri: str) -> StorageAdapter:
    reference = storage_object_ref_from_uri(storage_uri)
    if reference.provider == "local":
        local_storage = LocalStorageAdapter(root=local_storage_root(), bucket=reference.bucket)
        require_storage_match(local_storage, reference)
        return local_storage
    if reference.provider not in {"cos", "oss"}:
        raise HTTPException(
            status_code=503,
            detail={"code": "STORAGE_PROVIDER_UNAVAILABLE"},
        )
    try:
        config = SettingsRepository(conn).load_provider_config(reference.provider)
    except (SettingsKeyMissing, SettingsDecryptError) as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "STORAGE_SETTINGS_UNAVAILABLE"},
        ) from exc
    if config.get("bucket") != reference.bucket:
        raise HTTPException(
            status_code=409,
            detail={"code": "STORAGE_BUCKET_MISMATCH"},
        )
    try:
        cloud_storage = create_storage_adapter(
            cloud_storage_config_from_settings(reference.provider, config)
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "STORAGE_SETTINGS_UNAVAILABLE"},
        ) from exc
    require_storage_match(cloud_storage, reference)
    return cloud_storage


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
def read_me(
    conn: Database,
    dev_user_id: Annotated[str | None, Header(alias="X-Dev-User-Id")] = None,
) -> UserResponse:
    try:
        actor = authenticate_user(conn, identity_user_id(dev_user_id))
    except HTTPException as exc:
        write_login_failure(conn, error=exc, identity_source_name=identity_source(dev_user_id))
        raise
    write_audit(
        conn,
        actor=actor,
        action="auth.login_success",
        entity_type="user",
        entity_id=actor.id,
    )
    return UserResponse(
        id=actor.id,
        username=actor.username,
        display_name=actor.display_name,
        role=actor.role,
    )


def write_login_failure(
    conn: sqlite3.Connection,
    *,
    error: HTTPException,
    identity_source_name: str,
) -> None:
    code = "AUTH_UNKNOWN"
    if isinstance(error.detail, dict) and isinstance(error.detail.get("code"), str):
        code = str(error.detail["code"])
    conn.execute(
        """
        INSERT INTO audit_logs (id, actor_user_id, action, entity_type, entity_id, metadata_json)
        VALUES (?, NULL, ?, ?, ?, ?)
        """,
        (
            str(uuid4()),
            "auth.login_failure",
            "auth",
            "current_user",
            json.dumps(
                {"code": code, "identity_source": identity_source_name},
                separators=(",", ":"),
                sort_keys=True,
            ),
        ),
    )
    conn.commit()


@router.get("/projects", response_model=list[ProjectResponse])
def list_projects(
    conn: Database,
    actor: AuthenticatedUser,
) -> list[ProjectResponse]:
    if actor.role in {"admin", "auditor"}:
        rows = conn.execute(
            """
            SELECT
                projects.id,
                projects.owner_user_id,
                projects.name,
                projects.status,
                reference_assets.id AS reference_asset_id,
                CASE
                    WHEN reference_assets.id IS NULL THEN 'NOT_STARTED'
                    WHEN reference_assets.sha256 = '' OR reference_assets.size_bytes = 0
                        THEN 'UPLOAD_PENDING'
                    ELSE 'READY'
                END AS reference_upload_status
            FROM projects
            LEFT JOIN assets AS reference_assets ON reference_assets.id = (
                SELECT assets.id
                FROM assets
                WHERE assets.project_id = projects.id
                    AND (
                        assets.kind = 'reference_video'
                        OR (
                            assets.kind = 'video'
                            AND assets.storage_uri LIKE '%/projects/' || projects.id || '/uploads/%'
                        )
                    )
                ORDER BY assets.created_at DESC, assets.rowid DESC
                LIMIT 1
            )
            ORDER BY projects.created_at DESC, projects.rowid DESC
            """
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT
                projects.id,
                projects.owner_user_id,
                projects.name,
                projects.status,
                reference_assets.id AS reference_asset_id,
                CASE
                    WHEN reference_assets.id IS NULL THEN 'NOT_STARTED'
                    WHEN reference_assets.sha256 = '' OR reference_assets.size_bytes = 0
                        THEN 'UPLOAD_PENDING'
                    ELSE 'READY'
                END AS reference_upload_status
            FROM projects
            LEFT JOIN assets AS reference_assets ON reference_assets.id = (
                SELECT assets.id
                FROM assets
                WHERE assets.project_id = projects.id
                    AND (
                        assets.kind = 'reference_video'
                        OR (
                            assets.kind = 'video'
                            AND assets.storage_uri LIKE '%/projects/' || projects.id || '/uploads/%'
                        )
                    )
                ORDER BY assets.created_at DESC, assets.rowid DESC
                LIMIT 1
            )
            WHERE projects.owner_user_id = ?
            ORDER BY projects.created_at DESC, projects.rowid DESC
            """,
            (actor.id,),
        ).fetchall()
    return [project_response(row) for row in rows]


@router.post(
    "/projects",
    response_model=ProjectResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_project(
    payload: CreateProjectRequest,
    conn: Database,
    actor: AuthenticatedUser,
) -> ProjectResponse:
    require_not_auditor(
        conn,
        actor=actor,
        action="project.create",
        entity_type="project",
        entity_id="new",
    )
    name = payload.name.strip()
    if not name:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"code": "PROJECT_NAME_REQUIRED", "message": "Project name is required."},
        )

    project_id = str(uuid4())
    with conn:
        conn.execute(
            """
            INSERT INTO projects (id, owner_user_id, name)
            VALUES (?, ?, ?)
            """,
            (project_id, actor.id, name),
        )
    write_audit(
        conn,
        actor=actor,
        action="project.create",
        entity_type="project",
        entity_id=project_id,
        metadata={"name": name},
    )
    return ProjectResponse(
        id=project_id,
        owner_user_id=actor.id,
        name=name,
        status="ACTIVE",
        reference_asset_id=None,
        reference_upload_status="NOT_STARTED",
    )


@router.delete("/projects/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_unfinished_project(
    project_id: str,
    conn: Database,
    actor: AuthenticatedUser,
) -> Response:
    require_not_auditor(
        conn,
        actor=actor,
        action="project.delete",
        entity_type="project",
        entity_id=project_id,
    )
    require_project_access(conn, actor=actor, project_id=project_id, action="project.delete")

    assets = conn.execute(
        """
        SELECT id, storage_uri, sha256, size_bytes
        FROM assets
        WHERE project_id = ?
        """,
        (project_id,),
    ).fetchall()
    has_completed_reference = any(
        str(asset["sha256"]) or int(asset["size_bytes"]) > 0 for asset in assets
    )
    has_project_work = conn.execute(
        """
        SELECT 1
        FROM versions
        WHERE project_id = ?
        UNION ALL
        SELECT 1
        FROM generation_batches
        WHERE project_id = ?
        LIMIT 1
        """,
        (project_id, project_id),
    ).fetchone()
    if has_completed_reference or has_project_work:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "PROJECT_DELETE_REQUIRES_UNFINISHED",
                "message": "Only unfinished projects without completed work can be deleted.",
            },
        )

    try:
        for asset in assets:
            storage = storage_for_asset(conn, str(asset["storage_uri"]))
            storage.delete_object(
                storage_key_from_uri(str(asset["storage_uri"])), actor_id=actor.id
            )
    except StorageBackendUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "STORAGE_PROVIDER_UNAVAILABLE"},
        ) from exc

    with conn:
        conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
    write_audit(
        conn,
        actor=actor,
        action="project.delete",
        entity_type="project",
        entity_id=project_id,
        metadata={"deleted_pending_asset_count": len(assets)},
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/projects/{project_id}", response_model=ProjectResponse)
def read_project(
    project_id: str,
    conn: Database,
    actor: AuthenticatedUser,
) -> ProjectResponse:
    require_project_access(conn, actor=actor, project_id=project_id, action="project.read")
    row = conn.execute(
        """
        SELECT
            projects.id,
            projects.owner_user_id,
            projects.name,
            projects.status,
            reference_assets.id AS reference_asset_id,
            CASE
                WHEN reference_assets.id IS NULL THEN 'NOT_STARTED'
                WHEN reference_assets.sha256 = '' OR reference_assets.size_bytes = 0
                    THEN 'UPLOAD_PENDING'
                ELSE 'READY'
            END AS reference_upload_status
        FROM projects
        LEFT JOIN assets AS reference_assets ON reference_assets.id = (
            SELECT assets.id
            FROM assets
            WHERE assets.project_id = projects.id
                AND (
                    assets.kind = 'reference_video'
                    OR (
                        assets.kind = 'video'
                        AND assets.storage_uri LIKE '%/projects/' || projects.id || '/uploads/%'
                    )
                )
            ORDER BY assets.created_at DESC, assets.rowid DESC
            LIMIT 1
        )
        WHERE projects.id = ?
        """,
        (project_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError("project disappeared after access check")
    return project_response(row)


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
    try:
        storage = storage_for_asset(conn, str(row["storage_uri"]))
        object_key = storage_key_from_uri(str(row["storage_uri"]))
        if storage.provider == "local":
            secret = os.environ.get("VIDEO_REPLICA_SETTINGS_KEY", "")
            expires_at = str(int(time.time()) + int(DOWNLOAD_URL_EXPIRES_IN.total_seconds()))
            signature = local_download_signature(object_key, expires_at, secret=secret)
            url = (
                f"{LOCAL_API_BASE_URL}/api/assets/local-objects/{quote(object_key, safe='/')}"
                f"?expires={expires_at}&sig={signature}"
            )
            return DownloadUrlResponse(url=url)
        intent = storage.create_download_intent(
            object_key,
            expires_in=DOWNLOAD_URL_EXPIRES_IN,
            can_read=True,
        )
    except StorageBackendUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "STORAGE_PROVIDER_UNAVAILABLE"},
        ) from exc
    return DownloadUrlResponse(url=intent.url)


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
        project_id=None if row["project_id"] is None else str(row["project_id"]),
        kind=str(row["kind"]),
        sha256=str(row["sha256"]),
        size_bytes=int(row["size_bytes"]),
        content_type=None if row["content_type"] is None else str(row["content_type"]),
    )


def project_response(row: sqlite3.Row) -> ProjectResponse:
    reference_asset_id = (
        None
        if "reference_asset_id" not in row.keys() or row["reference_asset_id"] is None
        else str(row["reference_asset_id"])
    )
    reference_upload_status = (
        "NOT_STARTED"
        if "reference_upload_status" not in row.keys()
        else str(row["reference_upload_status"])
    )
    return ProjectResponse(
        id=str(row["id"]),
        owner_user_id=str(row["owner_user_id"]),
        name=str(row["name"]),
        status=str(row["status"]),
        reference_asset_id=reference_asset_id,
        reference_upload_status=reference_upload_status,
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
