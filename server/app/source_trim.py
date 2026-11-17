"""网页长原片的异步选段；复用现有上传校验和来源分析服务。"""

from __future__ import annotations

import hashlib
import json
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.auth import CurrentUser, ReadDatabase, Role, ShortAuthUser
from app.customer_fence import BusinessDbDep
from app.db_portable import BusinessConnection, BusinessRow
from app.media import FFprobeVideoProbe, create_upload_intent, persist_upload_completion, prepare_upload_completion, probe_upload_completion, storage_key_from_uri
from app.media_tools import resolve_media_binary
from app.permissions import require_asset_access, require_not_auditor, require_project_access
from app.project_sources import BindProjectSource, CreateProjectSource, bind_project_source, create_project_source, source_response
from app.segmented_asr import Connection
from app.segmented_media import stream_source, temp_root, trim_video
from app.source_analysis import SourceAnalysisRequest, dispatch_source_analysis
from app.storage import StorageAdapter

router = APIRouter(prefix="/api/projects", tags=["source-trim"])


class SourceTrimRequest(BaseModel):
    source_id: str = Field(min_length=1, max_length=128)
    source_revision: int = Field(ge=1)
    start: float = Field(ge=0, allow_inf_nan=False)
    end: float = Field(gt=0, allow_inf_nan=False)
    idempotency_key: str = Field(min_length=8, max_length=128)


def trim_response(conn: BusinessConnection, row: BusinessRow) -> dict[str, object]:
    source = None if row["result_source_id"] is None else conn.execute("SELECT * FROM project_sources WHERE id=%s", (row["result_source_id"],)).fetchone()
    action = None if row["result_action_id"] is None else conn.execute("SELECT id,source_id,status,route,task_id FROM source_analysis_actions WHERE id=%s", (row["result_action_id"],)).fetchone()
    return {"id": str(row["id"]), "status": str(row["status"]), "project_id": str(row["project_id"]),
            "error_code": row["error_code"], "source": None if source is None else source_response(source).model_dump(),
            "action": None if action is None else dict(action)}


def enqueue_source_trim(conn: BusinessConnection, *, actor: CurrentUser, project_id: str, request: SourceTrimRequest) -> BusinessRow:
    require_not_auditor(conn, actor=actor, action="source.trim", entity_type="project", entity_id=project_id)
    require_project_access(conn, actor=actor, project_id=project_id, action="source.trim")
    project = conn.execute("SELECT current_source_id FROM projects WHERE id=%s FOR UPDATE", (project_id,)).fetchone()
    fingerprint = hashlib.sha256(request.model_dump_json().encode()).hexdigest()
    replay = conn.execute("SELECT * FROM source_trim_tasks WHERE project_id=%s AND owner_user_id=%s AND idempotency_key=%s", (project_id, actor.id, request.idempotency_key)).fetchone()
    if replay is not None:
        if replay["request_hash"] != fingerprint:
            raise HTTPException(409, detail={"code": "SOURCE_TRIM_REQUEST_CONFLICT", "message": "选段范围已变化，请重新提交。"})
        return cast(BusinessRow, replay)
    source = conn.execute("SELECT * FROM project_sources WHERE id=%s AND project_id=%s", (request.source_id, project_id)).fetchone()
    if source is None or project["current_source_id"] != request.source_id or int(source["revision"]) != request.source_revision:
        raise HTTPException(409, detail={"code": "SOURCE_REPLACED", "message": "当前来源已变化，请刷新后重试。"})
    if source["original_video_asset_id"] is None:
        raise HTTPException(409, detail={"code": "SOURCE_TRIM_NEEDS_ORIGINAL", "message": "原视频尚未上传，请使用桌面端从本机原片选择片段。"})
    asset = require_asset_access(conn, actor=actor, asset_id=str(source["original_video_asset_id"]), action="source.trim")
    if not str(asset["content_type"]).startswith("video/") or not 4 <= request.end - request.start <= 180 or request.end > float(source["verified_duration_seconds"] or 0) + 0.1:
        raise HTTPException(422, detail={"code": "SOURCE_RANGE_INVALID", "message": "请选择原视频内 4–180 秒的连续片段。"})
    row = conn.execute("INSERT INTO source_trim_tasks(id,project_id,source_id,source_revision,owner_user_id,range_start,range_end,idempotency_key,request_hash) "
                       "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
                       (str(uuid4()), project_id, request.source_id, request.source_revision, actor.id, request.start, request.end, request.idempotency_key, fingerprint)).fetchone()
    return cast(BusinessRow, row)


@router.post("/{project_id}/source-trims")
def create_trim(project_id: str, request: SourceTrimRequest, db: BusinessDbDep) -> dict[str, object]:
    with db.write() as (conn, actor):
        return trim_response(conn, enqueue_source_trim(conn, actor=actor, project_id=project_id, request=request))


@router.get("/{project_id}/source-trims/{task_id}")
def get_trim(project_id: str, task_id: str, conn: ReadDatabase, actor: ShortAuthUser) -> dict[str, object]:
    require_project_access(conn, actor=actor, project_id=project_id, action="source.trim_read")
    row = conn.execute("SELECT * FROM source_trim_tasks WHERE id=%s AND project_id=%s AND owner_user_id=%s", (task_id, project_id, actor.id)).fetchone()
    if row is None:
        raise HTTPException(404, detail={"code": "SOURCE_TRIM_MISSING", "message": "视频选段任务不存在。"})
    return trim_response(conn, row)


@router.post("/{project_id}/source-trims/{task_id}/retry")
def retry_trim(project_id: str, task_id: str, db: BusinessDbDep) -> dict[str, object]:
    with db.write() as (conn, actor):
        require_not_auditor(conn, actor=actor, action="source.trim_retry", entity_type="project", entity_id=project_id)
        require_project_access(conn, actor=actor, project_id=project_id, action="source.trim_retry")
        project = conn.execute("SELECT current_source_id FROM projects WHERE id=%s FOR UPDATE", (project_id,)).fetchone()
        row = conn.execute("SELECT * FROM source_trim_tasks WHERE id=%s AND project_id=%s AND owner_user_id=%s FOR UPDATE", (task_id, project_id, actor.id)).fetchone()
        if row is None:
            raise HTTPException(404, detail={"code": "SOURCE_TRIM_MISSING", "message": "视频选段任务不存在。"})
        if row["status"] == "FAILED":
            source = conn.execute("SELECT revision FROM project_sources WHERE id=%s", (row["source_id"],)).fetchone()
            if source is None or project["current_source_id"] != row["source_id"] or source["revision"] != row["source_revision"]:
                raise HTTPException(409, detail={"code": "SOURCE_REPLACED", "message": "来源已变化，请重新选择片段。"})
            row = conn.execute("UPDATE source_trim_tasks SET status='PENDING',cancel_requested_at=NULL,error_code=NULL,next_attempt_at=NULL,updated_at=now() WHERE id=%s RETURNING *", (task_id,)).fetchone()
        return trim_response(conn, row)


@router.post("/{project_id}/source-trims/{task_id}/cancel")
def cancel_trim(project_id: str, task_id: str, db: BusinessDbDep) -> dict[str, object]:
    with db.write() as (conn, actor):
        require_not_auditor(conn, actor=actor, action="source.trim_cancel", entity_type="project", entity_id=project_id)
        require_project_access(conn, actor=actor, project_id=project_id, action="source.trim_cancel")
        row = conn.execute("SELECT * FROM source_trim_tasks WHERE id=%s AND project_id=%s AND owner_user_id=%s FOR UPDATE", (task_id, project_id, actor.id)).fetchone()
        if row is None:
            raise HTTPException(404, detail={"code": "SOURCE_TRIM_MISSING", "message": "视频选段任务不存在。"})
        if row["status"] in {"PENDING", "RUNNING"}:
            row = conn.execute("UPDATE source_trim_tasks SET cancel_requested_at=now(),status=CASE WHEN status='PENDING' THEN 'FAILED' ELSE status END,error_code='SOURCE_TRIM_CANCELLED',updated_at=now() WHERE id=%s RETURNING *", (task_id,)).fetchone()
        return trim_response(conn, row)


def _owned(conn: BusinessConnection, task: BusinessRow, worker: str) -> BusinessRow:
    row = conn.execute("SELECT * FROM source_trim_tasks WHERE id=%s AND status='RUNNING' AND locked_by=%s AND attempt=%s AND locked_until>now() FOR UPDATE", (task["id"], worker, task["attempt"])).fetchone()
    if row is None:
        raise RuntimeError("视频选段租约已失效。")
    return cast(BusinessRow, row)


def run_source_trim_one(connection: Connection, storage: StorageAdapter, worker: str) -> bool:
    with connection() as conn:
        conn.execute("UPDATE source_trim_tasks SET status='PENDING',locked_by=NULL,locked_until=NULL WHERE status='RUNNING' AND locked_until<=now()")
        task = conn.execute("UPDATE source_trim_tasks SET status='RUNNING',attempt=attempt+1,locked_by=%s,locked_until=now()+interval '20 minutes',updated_at=now() "
                            "WHERE id=(SELECT id FROM source_trim_tasks WHERE status='PENDING' AND (next_attempt_at IS NULL OR next_attempt_at<=now()) ORDER BY created_at,id FOR UPDATE SKIP LOCKED LIMIT 1) RETURNING *", (worker,)).fetchone()
        if task is None:
            conn.commit()
            return False
        conn.execute("SELECT pg_advisory_xact_lock(720151901)")
        busy = conn.execute("SELECT 1 FROM source_trim_tasks WHERE id<>%s AND status='RUNNING' AND locked_until>now() UNION ALL "
                            "SELECT 1 FROM script_from_audio_tasks WHERE status='RUNNING' AND processing_phase='PREPARING' AND locked_until::timestamptz>now() LIMIT 1", (task["id"],)).fetchone()
        if busy:
            conn.execute("UPDATE source_trim_tasks SET status='PENDING',locked_by=NULL,locked_until=NULL,next_attempt_at=now()+interval '10 seconds' WHERE id=%s", (task["id"],))
            conn.commit()
            return False
        conn.commit()
    try:
        _perform_trim(task, worker, connection, storage)
    except Exception as exc:
        with connection() as conn:
            conn.execute("UPDATE source_trim_tasks SET status='FAILED',error_code=%s,locked_by=NULL,locked_until=NULL,updated_at=now() WHERE id=%s AND status='RUNNING' AND locked_by=%s AND attempt=%s",
                         ("SOURCE_TRIM_CANCELLED" if str(exc) == "cancelled" else "SOURCE_TRIM_BALANCE_INSUFFICIENT" if isinstance(exc, HTTPException) and exc.status_code == 402 else "SOURCE_TRIM_FAILED", task["id"], worker, task["attempt"]))
            conn.commit()
    return True


def _perform_trim(task: BusinessRow, worker: str, connection: Connection, storage: StorageAdapter) -> None:
    with connection() as conn:
        if _owned(conn, task, worker)["cancel_requested_at"] is not None:
            raise RuntimeError("cancelled")
        source = conn.execute("SELECT * FROM project_sources WHERE id=%s AND project_id=%s", (task["source_id"], task["project_id"])).fetchone()
        original = conn.execute("SELECT * FROM assets WHERE id=%s", (source["original_video_asset_id"],)).fetchone()
        user = conn.execute("SELECT * FROM users WHERE id=%s", (task["owner_user_id"],)).fetchone()
        actor = CurrentUser(id=str(user["id"]), username=str(user["username"]), display_name=str(user["display_name"]), role=cast(Role, str(user["role"])))
        existing = None if task["asset_id"] is None else conn.execute("SELECT * FROM assets WHERE id=%s AND project_id=%s", (task["asset_id"], task["project_id"])).fetchone()
        ready = existing is not None and bool(existing["sha256"]) and json.loads(str(existing["metadata_json"] or "{}")).get("upload_status") == "COMPLETE"
        conn.commit()

    def heartbeat() -> None:
        with connection() as conn:
            row = _owned(conn, task, worker)
            if row["cancel_requested_at"] is not None:
                raise RuntimeError("cancelled")
            conn.execute("UPDATE source_trim_tasks SET locked_until=now()+interval '20 minutes',updated_at=now() WHERE id=%s", (task["id"],))
            conn.commit()

    asset_id = str(task["asset_id"] or "")
    if not ready:
        with tempfile.TemporaryDirectory(prefix="source-trim-", dir=temp_root()) as directory:
            source_path, output = Path(directory) / "original", Path(directory) / "clip.mp4"
            stream_source(storage, storage_key_from_uri(str(original["storage_uri"])), source_path,
                          expected_size=int(original["size_bytes"]), expected_sha256=str(original["sha256"]), max_bytes=2 * 1024**3, heartbeat=heartbeat)
            trim_video(resolve_media_binary("ffmpeg"), source_path, output, start=float(task["range_start"]), end=float(task["range_end"]), heartbeat=heartbeat)
            with connection() as conn:
                _owned(conn, task, worker)
                intent = create_upload_intent(conn, actor=actor, storage=storage, project_id=str(task["project_id"]), filename="选中片段.mp4", content_type="video/mp4", size_bytes=output.stat().st_size, purpose="script", post_upload_action="store_only")
                asset_id = intent.asset_id
                conn.execute("UPDATE source_trim_tasks SET asset_id=%s WHERE id=%s AND locked_by=%s AND attempt=%s", (asset_id, task["id"], worker, task["attempt"]))
                conn.commit()
            storage.put_file(intent.storage_key, output, content_type="video/mp4")
            heartbeat()
            with connection() as conn:
                prepared = prepare_upload_completion(conn, actor=actor, asset_id=asset_id)
            probed = probe_upload_completion(prepared, storage=storage, probe=FFprobeVideoProbe())
            with connection() as conn:
                _owned(conn, task, worker)
                persist_upload_completion(conn, actor=actor, probed=probed)
    with connection() as conn:
        project = conn.execute("SELECT current_source_id FROM projects WHERE id=%s FOR UPDATE", (task["project_id"],)).fetchone()
        if _owned(conn, task, worker)["cancel_requested_at"] is not None:
            raise RuntimeError("cancelled")
        current = conn.execute("SELECT * FROM project_sources WHERE id=%s", (task["source_id"],)).fetchone()
        if project["current_source_id"] != task["source_id"] or int(current["revision"]) != int(task["source_revision"]):
            raise RuntimeError("来源已切换。")
        asset = conn.execute("SELECT * FROM assets WHERE id=%s", (asset_id,)).fetchone()
        duration = float(json.loads(str(asset["metadata_json"]))["duration_seconds"])
        if abs(duration - (float(task["range_end"]) - float(task["range_start"]))) > 0.15:
            raise RuntimeError("选段时长校验失败。")
        child = create_project_source(conn, actor=actor, project_id=str(task["project_id"]), request=CreateProjectSource(
            filename="选中片段.mp4", size_bytes=int(asset["size_bytes"]), duration_seconds=duration, idempotency_key=f"trim-source:{task['id']}",
            parent_source_id=str(task["source_id"]), range_start_seconds=float(task["range_start"]), range_end_seconds=float(task["range_end"])))
        bound = bind_project_source(conn, actor=actor, project_id=str(task["project_id"]), source_id=str(child["id"]), request=BindProjectSource(asset_id=asset_id, expected_revision=int(child["revision"])))
        action = dispatch_source_analysis(conn, actor=actor, project_id=str(task["project_id"]), request=SourceAnalysisRequest(source_id=str(child["id"]), source_revision=int(bound["revision"]), idempotency_key=f"trim-analysis:{task['id']}"))
        conn.execute("UPDATE source_trim_tasks SET status='SUCCEEDED',result_source_id=%s,result_action_id=%s,locked_by=NULL,locked_until=NULL,updated_at=now() WHERE id=%s", (child["id"], action.id, task["id"]))
        conn.commit()
