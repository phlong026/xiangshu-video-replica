from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import sqlite3
import time
from _thread import LockType
from datetime import timedelta
from pathlib import Path
from threading import Lock
from typing import Annotated, cast
from urllib.parse import quote
from uuid import uuid4

from fastapi import APIRouter, Header, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field

from app.auth import (
    AuthenticatedUser,
    Database,
    authenticate_request,
    identity_source,
)
from app.media import storage_key_from_uri
from app.media_routes import LOCAL_API_BASE_URL
from app.permissions import (
    require_asset_access,
    require_not_auditor,
    require_project_access,
    require_role,
    write_audit,
)
from app.settings import SettingsRepository, SettingsUnavailableError, settings_encryption_key
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
CHARACTER_CACHE_KINDS = frozenset({"character_contact_sheet", "character_generated_image"})
CHARACTER_CACHE_SUFFIXES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}
CHARACTER_CACHE_CONTENT_TYPES = {
    suffix: content_type for content_type, suffix in CHARACTER_CACHE_SUFFIXES.items()
}
CHARACTER_CACHE_NAME = re.compile(r"^[0-9a-f]{64}\.(?:jpg|png|webp)$")
CHARACTER_CACHE_LOCKS: dict[str, LockType] = {}
CHARACTER_CACHE_LOCKS_GUARD = Lock()
logger = logging.getLogger(__name__)


class UserResponse(BaseModel):
    id: str
    username: str
    display_name: str
    role: str


class CreateProjectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)


class RenameProjectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)


class ProjectResponse(BaseModel):
    id: str
    owner_user_id: str
    name: str
    status: str
    reference_asset_id: str | None
    reference_upload_status: str
    analysis_status: str


class AssetResponse(BaseModel):
    id: str
    project_id: str | None
    kind: str
    sha256: str
    size_bytes: int
    content_type: str | None


class DownloadUrlResponse(BaseModel):
    url: str


def _character_cache_root() -> Path:
    home = os.environ.get("VIDEO_REPLICA_HOME", "").strip()
    if home:
        return (Path(home) / "storage-cache" / "character-images").resolve()
    return (local_storage_root() / ".cache" / "character-images").resolve()


def _character_cache_identity(row: sqlite3.Row) -> tuple[str, str]:
    kind = str(row["kind"])
    content_type = str(row["content_type"] or "").split(";", 1)[0].strip().lower()
    if kind not in CHARACTER_CACHE_KINDS or content_type not in CHARACTER_CACHE_SUFFIXES:
        raise HTTPException(
            status_code=409,
            detail={"code": "CHARACTER_CACHE_UNSUPPORTED"},
        )
    source_version = str(row["sha256"] or row["storage_uri"])
    digest = hashlib.sha256(f"{row['id']}:{source_version}".encode()).hexdigest()
    return f"{digest}{CHARACTER_CACHE_SUFFIXES[content_type]}", content_type


def _character_cache_path(cache_name: str) -> Path:
    if CHARACTER_CACHE_NAME.fullmatch(cache_name) is None:
        raise HTTPException(status_code=404, detail={"code": "CHARACTER_CACHE_NOT_FOUND"})
    return _character_cache_root() / cache_name


def _character_cache_lock(cache_name: str) -> LockType:
    with CHARACTER_CACHE_LOCKS_GUARD:
        return CHARACTER_CACHE_LOCKS.setdefault(cache_name, Lock())


def _populate_character_cache(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
) -> tuple[str, str]:
    cache_name, content_type = _character_cache_identity(row)
    try:
        cache_path = _character_cache_path(cache_name)
    except StorageBackendUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "CHARACTER_CACHE_UNAVAILABLE"},
        ) from exc
    with _character_cache_lock(cache_name):
        if cache_path.is_file():
            return cache_name, content_type

        storage_uri = str(row["storage_uri"])
        object_key = storage_key_from_uri(storage_uri)
        try:
            content = storage_for_asset(conn, storage_uri).get_object(object_key)
        except (KeyError, OSError, StorageBackendUnavailable) as exc:
            logger.error(
                "character cache source read failed for asset %s: %s",
                row["id"],
                type(exc).__name__,
            )
            raise HTTPException(
                status_code=503,
                detail={"code": "CHARACTER_CACHE_UNAVAILABLE"},
            ) from exc

        expected_sha256 = str(row["sha256"] or "").lower()
        if expected_sha256 and not hmac.compare_digest(
            hashlib.sha256(content).hexdigest(),
            expected_sha256,
        ):
            logger.error("character cache source hash mismatch for asset %s", row["id"])
            raise HTTPException(
                status_code=503,
                detail={"code": "CHARACTER_CACHE_UNAVAILABLE"},
            )

        temporary_path = cache_path.with_name(f".{cache_path.name}.{uuid4().hex}.tmp")
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            temporary_path.write_bytes(content)
            temporary_path.replace(cache_path)
        except OSError as exc:
            logger.error(
                "character cache write failed for asset %s: %s",
                row["id"],
                type(exc).__name__,
            )
            raise HTTPException(
                status_code=503,
                detail={"code": "CHARACTER_CACHE_UNAVAILABLE"},
            ) from exc
        finally:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError as exc:
                logger.warning(
                    "character cache temporary cleanup failed for asset %s: %s",
                    row["id"],
                    type(exc).__name__,
                )
    return cache_name, content_type


def storage_for_asset(conn: sqlite3.Connection, storage_uri: str) -> StorageAdapter:
    reference = storage_object_ref_from_uri(storage_uri)
    if reference.provider == "local":
        local_storage = LocalStorageAdapter(root=local_storage_root(), bucket=reference.bucket)
        require_storage_match(local_storage, reference)
        return local_storage
    if reference.provider != "cos":
        raise HTTPException(
            status_code=503,
            detail={"code": "STORAGE_PROVIDER_UNAVAILABLE"},
        )
    try:
        config = SettingsRepository(conn).load_provider_config(reference.provider)
    except SettingsUnavailableError as exc:
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
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> UserResponse:
    try:
        actor = authenticate_request(
            conn,
            authorization=authorization,
            dev_user_id=dev_user_id,
        )
    except HTTPException as exc:
        write_login_failure(
            conn,
            error=exc,
            identity_source_name=identity_source(dev_user_id, authorization),
        )
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
                END AS reference_upload_status,
                CASE
                    WHEN reference_assets.id IS NULL
                        OR reference_assets.sha256 = ''
                        OR reference_assets.size_bytes = 0
                        THEN 'NOT_READY'
                    WHEN EXISTS (
                        SELECT 1 FROM versions
                        WHERE versions.project_id = projects.id
                            AND versions.kind = 'analysis'
                            AND versions.asset_id = reference_assets.id
                    ) THEN 'READY'
                    ELSE 'PENDING'
                END AS analysis_status
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
                END AS reference_upload_status,
                CASE
                    WHEN reference_assets.id IS NULL
                        OR reference_assets.sha256 = ''
                        OR reference_assets.size_bytes = 0
                        THEN 'NOT_READY'
                    WHEN EXISTS (
                        SELECT 1 FROM versions
                        WHERE versions.project_id = projects.id
                            AND versions.kind = 'analysis'
                            AND versions.asset_id = reference_assets.id
                    ) THEN 'READY'
                    ELSE 'PENDING'
                END AS analysis_status
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
        analysis_status="NOT_READY",
    )


@router.patch("/projects/{project_id}/name", response_model=ProjectResponse)
def rename_project(
    project_id: str,
    payload: RenameProjectRequest,
    conn: Database,
    actor: AuthenticatedUser,
) -> ProjectResponse:
    require_not_auditor(
        conn,
        actor=actor,
        action="project.rename",
        entity_type="project",
        entity_id=project_id,
    )
    current_row = require_project_access(
        conn,
        actor=actor,
        project_id=project_id,
        action="project.rename",
    )

    name = payload.name.strip()
    if not name:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"code": "PROJECT_NAME_REQUIRED", "message": "Project name is required."},
        )
    with conn:
        conn.execute(
            """
            UPDATE projects
            SET name = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (name, project_id),
        )
    write_audit(
        conn,
        actor=actor,
        action="project.rename",
        entity_type="project",
        entity_id=project_id,
        metadata={"from_name": str(current_row["name"]), "to_name": name},
    )
    row = project_detail_row(conn, project_id)
    if row is None:
        raise RuntimeError("project disappeared after rename")
    return project_response(row)


@router.delete("/projects/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_project(
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

    # Paid provider calls may still be in flight for leased tasks; deleting the
    # project underneath them would lose their write-back, so require the
    # operator to wait until they settle (succeed, fail, or supersede).
    has_active_tasks = conn.execute(
        """
        SELECT 1
        FROM generation_tasks
        JOIN generation_batches ON generation_batches.id = generation_tasks.batch_id
        WHERE generation_batches.project_id = ?
          AND generation_tasks.status IN
              ('PENDING', 'SUBMITTING', 'QUEUED', 'RUNNING', 'ARCHIVING')
        LIMIT 1
        """,
        (project_id,),
    ).fetchone()
    if has_active_tasks:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "PROJECT_DELETE_HAS_ACTIVE_TASKS",
                "message": "项目存在进行中的生成任务，请等待任务结束或失败后再删除。",
            },
        )

    assets = conn.execute(
        """
        SELECT id, storage_uri, sha256, size_bytes
        FROM assets
        WHERE project_id = ?
        """,
        (project_id,),
    ).fetchall()
    versions_count = int(
        conn.execute(
            "SELECT COUNT(*) FROM versions WHERE project_id = ?",
            (project_id,),
        ).fetchone()[0]
    )

    # Best-effort object cleanup: the operator is discarding the whole project,
    # so an unavailable backend (e.g. cloud credentials removed) must not block
    # the delete. Failures are counted and surfaced through the audit log.
    storage_cleanup_failed_count = 0
    for asset in assets:
        try:
            storage = storage_for_asset(conn, str(asset["storage_uri"]))
            storage.delete_object(
                storage_key_from_uri(str(asset["storage_uri"])), actor_id=actor.id
            )
        except (HTTPException, StorageBackendUnavailable, OSError, ValueError):
            storage_cleanup_failed_count += 1

    with conn:
        # character_reference_selections references versions with ON DELETE
        # RESTRICT, so it must be cleared before the cascade removes versions.
        conn.execute(
            "DELETE FROM character_reference_selections WHERE project_id = ?",
            (project_id,),
        )
        conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
    write_audit(
        conn,
        actor=actor,
        action="project.delete",
        entity_type="project",
        entity_id=project_id,
        metadata={
            "deleted_asset_count": len(assets),
            "deleted_versions_count": versions_count,
            "storage_cleanup_failed_count": storage_cleanup_failed_count,
        },
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/projects/{project_id}", response_model=ProjectResponse)
def read_project(
    project_id: str,
    conn: Database,
    actor: AuthenticatedUser,
) -> ProjectResponse:
    require_project_access(conn, actor=actor, project_id=project_id, action="project.read")
    row = project_detail_row(conn, project_id)
    if row is None:
        raise RuntimeError("project disappeared after access check")
    return project_response(row)


def project_detail_row(
    conn: sqlite3.Connection,
    project_id: str,
) -> sqlite3.Row | None:
    return cast(
        "sqlite3.Row | None",
        conn.execute(
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
            END AS reference_upload_status,
            CASE
                WHEN reference_assets.id IS NULL
                    OR reference_assets.sha256 = ''
                    OR reference_assets.size_bytes = 0
                    THEN 'NOT_READY'
                WHEN EXISTS (
                    SELECT 1 FROM versions
                    WHERE versions.project_id = projects.id
                        AND versions.kind = 'analysis'
                        AND versions.asset_id = reference_assets.id
                ) THEN 'READY'
                ELSE 'PENDING'
            END AS analysis_status
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
        ).fetchone(),
    )


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
            secret = settings_encryption_key()
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


@router.post("/assets/{asset_id}/cached-url", response_model=DownloadUrlResponse)
def create_cached_character_url(
    asset_id: str,
    conn: Database,
    actor: AuthenticatedUser,
) -> DownloadUrlResponse:
    row = require_asset_access(
        conn,
        actor=actor,
        asset_id=asset_id,
        action="asset.character_cache.read",
    )
    cache_name, _ = _populate_character_cache(conn, row)
    expires_at = str(int(time.time()) + int(DOWNLOAD_URL_EXPIRES_IN.total_seconds()))
    signed_key = f"character-cache/{cache_name}"
    signature = local_download_signature(
        signed_key,
        expires_at,
        secret=settings_encryption_key(),
    )
    return DownloadUrlResponse(
        url=(
            f"{LOCAL_API_BASE_URL}/api/assets/character-cache/{cache_name}"
            f"?expires={expires_at}&sig={signature}"
        )
    )


@router.get("/assets/character-cache/{cache_name}")
def read_cached_character_asset(cache_name: str, request: Request) -> Response:
    expires_at = request.query_params.get("expires")
    signature = request.query_params.get("sig")
    signed_key = f"character-cache/{cache_name}"
    if (
        not expires_at
        or not signature
        or not expires_at.isdigit()
        or len(expires_at) > 20
        or int(expires_at) < int(time.time())
        or not hmac.compare_digest(
            signature,
            local_download_signature(
                signed_key,
                expires_at,
                secret=settings_encryption_key(),
            ),
        )
    ):
        raise HTTPException(
            status_code=403,
            detail={"code": "CHARACTER_CACHE_FORBIDDEN"},
        )
    try:
        cache_path = _character_cache_path(cache_name)
    except StorageBackendUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "CHARACTER_CACHE_UNAVAILABLE"},
        ) from exc
    if not cache_path.is_file():
        raise HTTPException(
            status_code=404,
            detail={"code": "CHARACTER_CACHE_NOT_FOUND"},
        )
    try:
        content = cache_path.read_bytes()
    except OSError as exc:
        logger.error(
            "character cache read failed for file %s: %s",
            cache_name,
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=503,
            detail={"code": "CHARACTER_CACHE_UNAVAILABLE"},
        ) from exc
    return Response(
        content=content,
        media_type=CHARACTER_CACHE_CONTENT_TYPES[cache_path.suffix],
        headers={"Cache-Control": "private, max-age=900"},
    )


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
    analysis_status = (
        "NOT_READY" if "analysis_status" not in row.keys() else str(row["analysis_status"])
    )
    return ProjectResponse(
        id=str(row["id"]),
        owner_user_id=str(row["owner_user_id"]),
        name=str(row["name"]),
        status=str(row["status"]),
        reference_asset_id=reference_asset_id,
        reference_upload_status=reference_upload_status,
        analysis_status=analysis_status,
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
