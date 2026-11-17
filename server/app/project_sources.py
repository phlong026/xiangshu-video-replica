"""项目来源只保存媒体身份；本机路径始终留在客户端。"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any, cast
from uuid import uuid4

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.auth import CurrentUser
from app.db_portable import BusinessConnection, BusinessRow
from app.media import is_project_source_asset, is_reference_video_asset
from app.permissions import require_asset_access, require_not_auditor, require_project_access


class CreateProjectSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    filename: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(gt=0, le=20 * 1024**3)
    duration_seconds: float | None = Field(default=None, gt=0, le=12 * 3600, allow_inf_nan=False)
    idempotency_key: str = Field(min_length=8, max_length=128)
    parent_source_id: str | None = Field(default=None, max_length=128)
    range_start_seconds: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    range_end_seconds: float | None = Field(default=None, gt=0, allow_inf_nan=False)


class BindProjectSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset_id: str = Field(min_length=1, max_length=128)
    expected_revision: int = Field(ge=1)


class ProjectSourceResponse(BaseModel):
    id: str
    project_id: str
    revision: int
    filename: str
    declared_size_bytes: int
    declared_duration_seconds: float | None
    original_video_asset_id: str | None
    audio_source_asset_id: str | None
    verified_duration_seconds: float | None
    verified_sha256: str | None
    parent_source_id: str | None = None
    range_start_seconds: float | None = None
    range_end_seconds: float | None = None


def source_response(row: BusinessRow) -> ProjectSourceResponse:
    return ProjectSourceResponse.model_validate(
        {key: row[key] for key in ProjectSourceResponse.model_fields}
    )


def source_error(code: str, message: str, status: int = 409) -> HTTPException:
    return HTTPException(status, detail={"code": code, "message": message})


def create_project_source(
    conn: BusinessConnection, *, actor: CurrentUser, project_id: str, request: CreateProjectSource
) -> BusinessRow:
    require_not_auditor(
        conn, actor=actor, action="source.create", entity_type="project", entity_id=project_id
    )
    project = require_project_access(
        conn, actor=actor, project_id=project_id, action="source.create"
    )
    # 项目锁统一换源顺序，旧请求重放只能取回原来源，不能覆盖后来选择的来源。
    locked = conn.execute(
        "SELECT id FROM projects WHERE id=%s FOR UPDATE", (project_id,)
    ).fetchone()
    if locked is None:
        raise source_error("PROJECT_NOT_FOUND", "项目不存在。", 404)
    project = require_project_access(
        conn, actor=actor, project_id=project_id, action="source.create"
    )
    fingerprint = hashlib.sha256(request.model_dump_json().encode()).hexdigest()
    old = conn.execute(
        "SELECT * FROM project_sources WHERE project_id=%s AND idempotency_key=%s",
        (project_id, request.idempotency_key),
    ).fetchone()
    if old is not None:
        if str(old["request_hash"]) != fingerprint:
            raise source_error("SOURCE_REQUEST_CONFLICT", "来源内容已变化，请使用新的请求编号。")
        return cast(BusinessRow, old)
    if request.parent_source_id is not None:
        parent = conn.execute("SELECT * FROM project_sources WHERE id=%s AND project_id=%s", (request.parent_source_id, project_id)).fetchone()
        current = conn.execute("SELECT current_source_id FROM projects WHERE id=%s", (project_id,)).fetchone()
        if parent is None or current["current_source_id"] != request.parent_source_id:
            raise source_error("SOURCE_REPLACED", "原视频来源已变化，请重新选择片段。")
        start, end = request.range_start_seconds, request.range_end_seconds
        duration = parent["verified_duration_seconds"] or parent["declared_duration_seconds"]
        if start is None or end is None or duration is None or not 3.9 <= end - start <= 180.1 or end > float(duration) + 0.1:
            raise source_error("SOURCE_RANGE_INVALID", "请选择原视频内 4–180 秒的连续片段。", 422)
    elif request.range_start_seconds is not None or request.range_end_seconds is not None:
        raise source_error("SOURCE_RANGE_INVALID", "片段必须关联原视频来源。", 422)
    source_id = str(uuid4())
    row = conn.execute(
        "INSERT INTO project_sources(id,project_id,owner_user_id,idempotency_key,"
        "request_hash,filename,declared_size_bytes,declared_duration_seconds,parent_source_id,range_start_seconds,range_end_seconds) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
        (
            source_id,
            project_id,
            str(project["owner_user_id"]),
            request.idempotency_key,
            fingerprint,
            request.filename,
            request.size_bytes,
            request.duration_seconds,
            request.parent_source_id,
            request.range_start_seconds,
            request.range_end_seconds,
        ),
    ).fetchone()
    assert row is not None
    conn.execute("UPDATE projects SET current_source_id=%s WHERE id=%s", (source_id, project_id))
    return cast(BusinessRow, row)


def current_project_source(
    conn: BusinessConnection, *, actor: CurrentUser, project_id: str
) -> BusinessRow | None:
    require_project_access(conn, actor=actor, project_id=project_id, action="source.read")
    row = conn.execute(
        "SELECT s.* FROM project_sources s JOIN projects p ON "
        "p.current_source_id=s.id AND p.id=s.project_id WHERE p.id=%s",
        (project_id,),
    ).fetchone()
    return cast(BusinessRow | None, row)


def bind_project_source(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    project_id: str,
    source_id: str,
    request: BindProjectSource,
) -> BusinessRow:
    require_not_auditor(
        conn, actor=actor, action="source.bind", entity_type="project", entity_id=project_id
    )
    require_project_access(conn, actor=actor, project_id=project_id, action="source.bind")
    project = conn.execute(
        "SELECT current_source_id FROM projects WHERE id=%s FOR UPDATE", (project_id,)
    ).fetchone()
    row = conn.execute(
        "SELECT * FROM project_sources WHERE id=%s AND project_id=%s FOR UPDATE",
        (source_id, project_id),
    ).fetchone()
    if row is None:
        raise source_error("SOURCE_NOT_FOUND", "来源不存在。", 404)
    if project is None or project["current_source_id"] != source_id:
        raise source_error("SOURCE_REPLACED", "当前来源已切换，请刷新后重试。")
    asset = require_asset_access(conn, actor=actor, asset_id=request.asset_id, action="source.bind")
    if str(asset["project_id"]) != project_id or not is_project_source_asset(asset):
        raise source_error("SOURCE_ASSET_INVALID", "请选择当前项目的视频或音频。", 422)
    if not asset["sha256"] or int(asset["size_bytes"] or 0) <= 0:
        raise source_error("SOURCE_ASSET_NOT_READY", "来源素材尚未完成上传。")
    metadata: dict[str, Any] = json.loads(str(asset["metadata_json"] or "{}"))
    if metadata.get("upload_status") in {"PENDING", "EXPIRED"}:
        raise source_error("SOURCE_ASSET_NOT_READY", "来源素材尚未完成上传。")
    video = is_reference_video_asset(asset)
    field = "original_video_asset_id" if video else "audio_source_asset_id"
    # 重试已成功的绑定不推进版本，也不再消耗费用。
    if row[field] == request.asset_id:
        return cast(BusinessRow, row)
    if int(row["revision"]) != request.expected_revision:
        raise source_error("SOURCE_REVISION_CONFLICT", "来源已更新，请刷新后重试。")
    if row[field] is not None:
        raise source_error("SOURCE_ASSET_REPLACED", "替换原素材需要重新建立来源。")
    if video and int(asset["size_bytes"]) != int(row["declared_size_bytes"]):
        raise source_error("SOURCE_FILE_MISMATCH", "原视频与选择的来源不一致，请重新选择。")
    duration = metadata.get("duration_seconds")
    if not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration <= 0:
        raise source_error("SOURCE_DURATION_UNAVAILABLE", "来源时长不可用，请重新上传。", 422)
    # 两条静态 SQL 保持视频事实与音轨事实分离，音轨不能覆盖视频哈希和时长。
    if video:
        result = conn.execute(
            "UPDATE project_sources SET "
            "original_video_asset_id=%s,v"
            "erified_duration_seconds=%s,v"
            "erified_sha256=%s,revision=revision+1,updated_at=now() WHERE id=%s "
            "RETURNING *",
            (request.asset_id, duration, str(asset["sha256"]), source_id),
        ).fetchone()
    else:
        result = conn.execute(
            "UPDATE project_sources SET "
            "audio_source_asset_id=%s,revision=revision+1,updated_at=now() WHERE id=%s "
            "RETURNING *",
            (request.asset_id, source_id),
        ).fetchone()
    assert result is not None
    return cast(BusinessRow, result)
