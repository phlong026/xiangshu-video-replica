from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from math import floor
from typing import Any, Literal, cast
from uuid import uuid4

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.analysis import get_version, insert_version
from app.auth import CurrentUser
from app.permissions import (
    require_asset_access,
    require_not_auditor,
    require_project_access,
    write_audit,
)
from app.storage import (
    StorageAdapter,
    StorageBackendUnavailable,
    StoragePermissionError,
    require_storage_match,
    storage_object_ref_from_uri,
)

SCRIPT_KIND = "script"
H3_PROMPT_KIND = "h3_prompt"
GENERATION_SCHEMA_VERSION = "c.generation.v1"
PROMPT_STATUSES = {"SAVED", "LOCKED", "USED"}
TERMINAL_STATUSES = {"FAILED", "CANCELLED"}
H3_MODEL = "MiniMax-H3"
SUPPORTED_RESOLUTIONS = {"768P", "2K"}
GENERATION_LEASE_SECONDS = 300
FIRST_FRAME_URL_EXPIRES_IN = timedelta(minutes=15)


class ScriptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Literal["original", "custom"]
    text: str
    shot_card_version_id: str = Field(min_length=1)


class PromptCompileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    script_version_id: str = Field(min_length=1)
    shot_card_version_id: str = Field(min_length=1)
    first_frame_asset_id: str = Field(min_length=1)
    output_duration_seconds: int = Field(ge=4, le=15)
    resolution: Literal["768P", "2K"] = "768P"


class GenerationBatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    quantity: int = Field(ge=1)
    prompt_version_id: str = Field(min_length=1)
    first_frame_asset_id: str = Field(min_length=1)
    output_duration_seconds: int = Field(ge=4, le=15)
    resolution: Literal["768P", "2K"] = "768P"
    idempotency_key: str = Field(min_length=1, max_length=128)
    provider: Literal["fake_h3", "metaso"] = "fake_h3"
    fake_audio_quality: Literal["ok", "missing"] = "ok"


class H3CreateResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider_task_id: str
    status: Literal["SUCCEEDED"]
    result_url: str
    result_content: bytes
    audio_quality_status: Literal["AUDIO_OK", "AUDIO_QUALITY_FAILED"]
    quality_issue_codes: list[str]


class SubmissionUncertain(RuntimeError):
    pass


class H3Provider:
    def create_image_to_video(self, request: dict[str, Any]) -> H3CreateResult:
        raise HTTPException(
            status_code=501,
            detail={
                "code": "H3_QUERY_SOURCE_PENDING",
                "message": (
                    "METASO create/query/archive is not implemented until the supported "
                    "business query endpoint is confirmed."
                ),
            },
        )


@dataclass(frozen=True)
class FakeH3Provider(H3Provider):
    audio_quality: Literal["ok", "missing"] = "ok"

    def create_image_to_video(self, request: dict[str, Any]) -> H3CreateResult:
        validate_h3_request(request)
        provider_task_id = f"fake-h3-{uuid4()}"
        audio_ok = self.audio_quality == "ok"
        return H3CreateResult(
            provider_task_id=provider_task_id,
            status="SUCCEEDED",
            result_url=f"fake://h3-results/{provider_task_id}.mp4",
            result_content=f"fake mp4 content for {provider_task_id}".encode(),
            audio_quality_status="AUDIO_OK" if audio_ok else "AUDIO_QUALITY_FAILED",
            quality_issue_codes=[] if audio_ok else ["AUDIO_QUALITY_FAILED"],
        )


class VersionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    project_id: str
    asset_id: str | None
    kind: str
    version_number: int
    payload: dict[str, Any]
    created_by_user_id: str | None
    created_at: str


class TaskResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    status: str
    archive_status: str
    quality_status: str
    quality_issue_codes: list[str]
    result_asset_id: str | None
    prompt_snapshot: dict[str, Any] | None


class BatchProgress(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total_count: int
    terminal_count: int
    progress_percent: int
    counts: dict[str, int]


class BatchResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    status: str
    quantity: int
    progress: BatchProgress
    tasks: list[TaskResult]

    @model_validator(mode="after")
    def validate_quantity(self) -> BatchResult:
        if self.quantity != self.progress.total_count:
            raise ValueError("quantity must match progress total_count")
        return self


def create_script_version(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    actor: CurrentUser,
    request: ScriptRequest,
) -> sqlite3.Row:
    require_not_auditor(
        conn,
        actor=actor,
        action="script.create",
        entity_type="project",
        entity_id=project_id,
    )
    require_project_access(conn, actor=actor, project_id=project_id, action="script.create")
    shot_card = require_version(
        conn,
        version_id=request.shot_card_version_id,
        project_id=project_id,
        kind="shot_card",
    )
    shot_payload = json.loads(str(shot_card["payload_json"]))
    text = request.text.strip()
    payload = {
        "schema_version": GENERATION_SCHEMA_VERSION,
        "source": request.source,
        "full_text": text,
        "char_count": len(text),
        "estimated_duration_seconds": estimate_spoken_duration(text),
        "shot_card_version_id": request.shot_card_version_id,
        "shot_mappings": map_script_to_shots(
            text,
            cast(list[dict[str, Any]], shot_payload["shots"]),
        ),
        "creates_audio_task": False,
    }
    row = insert_version(
        conn,
        project_id=project_id,
        asset_id=None,
        kind=SCRIPT_KIND,
        created_by_user_id=actor.id,
        payload=payload,
    )
    write_audit(
        conn,
        actor=actor,
        action="script.create",
        entity_type="version",
        entity_id=str(row["id"]),
        metadata={"project_id": project_id, "shot_card_version_id": request.shot_card_version_id},
    )
    return row


def compile_prompt_version(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    actor: CurrentUser,
    request: PromptCompileRequest,
) -> sqlite3.Row:
    require_not_auditor(
        conn,
        actor=actor,
        action="prompt.compile",
        entity_type="project",
        entity_id=project_id,
    )
    require_project_access(conn, actor=actor, project_id=project_id, action="prompt.compile")
    script = require_version(
        conn,
        version_id=request.script_version_id,
        project_id=project_id,
        kind=SCRIPT_KIND,
    )
    shot_card = require_version(
        conn,
        version_id=request.shot_card_version_id,
        project_id=project_id,
        kind="shot_card",
    )
    first_frame = require_asset_access(
        conn,
        actor=actor,
        asset_id=request.first_frame_asset_id,
        action="prompt.compile",
    )
    if str(first_frame["project_id"]) != project_id:
        raise generation_error(400, "ASSET_PROJECT_MISMATCH", "First frame is not in this project.")

    script_payload = json.loads(str(script["payload_json"]))
    shot_payload = json.loads(str(shot_card["payload_json"]))
    prompt_text = compile_prompt_text(
        script_payload=script_payload,
        shot_payload=shot_payload,
        duration_seconds=request.output_duration_seconds,
        resolution=request.resolution,
    )
    payload = {
        "schema_version": GENERATION_SCHEMA_VERSION,
        "status": "SAVED",
        "prompt_text": prompt_text,
        "content_hash": content_hash(prompt_text),
        "script_version_id": request.script_version_id,
        "shot_card_version_id": request.shot_card_version_id,
        "first_frame_asset_id": request.first_frame_asset_id,
        "first_frame_uri": str(first_frame["storage_uri"]),
        "output_duration_seconds": request.output_duration_seconds,
        "resolution": request.resolution,
    }
    row = insert_version(
        conn,
        project_id=project_id,
        asset_id=request.first_frame_asset_id,
        kind=H3_PROMPT_KIND,
        created_by_user_id=actor.id,
        payload=payload,
    )
    write_audit(
        conn,
        actor=actor,
        action="prompt.compile",
        entity_type="version",
        entity_id=str(row["id"]),
        metadata={"project_id": project_id},
    )
    return row


def lock_prompt_version(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    prompt_version_id: str,
    actor: CurrentUser,
) -> sqlite3.Row:
    require_not_auditor(
        conn,
        actor=actor,
        action="prompt.lock",
        entity_type="version",
        entity_id=prompt_version_id,
    )
    row = require_version(
        conn,
        version_id=prompt_version_id,
        project_id=project_id,
        kind=H3_PROMPT_KIND,
    )
    require_project_access(conn, actor=actor, project_id=project_id, action="prompt.lock")
    payload = json.loads(str(row["payload_json"]))
    if payload["status"] == "USED":
        return row
    if payload["status"] not in {"SAVED", "LOCKED"}:
        raise generation_error(409, "PROMPT_STATUS_INVALID", "Prompt cannot be locked.")
    payload["status"] = "LOCKED"
    with conn:
        conn.execute(
            "UPDATE versions SET payload_json = ? WHERE id = ?",
            (json.dumps(payload, ensure_ascii=True, sort_keys=True), prompt_version_id),
        )
    write_audit(
        conn,
        actor=actor,
        action="prompt.lock",
        entity_type="version",
        entity_id=prompt_version_id,
        metadata={"project_id": project_id},
    )
    return get_version(conn, prompt_version_id)


def create_generation_batch(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    actor: CurrentUser,
    request: GenerationBatchRequest,
    provider_client: H3Provider | None = None,
) -> BatchResult:
    _ = provider_client
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
    runtime = read_runtime_limits(conn)
    max_quantity = runtime["max_generation_count_per_batch"]
    if request.quantity > max_quantity:
        raise generation_error(
            422,
            "QUANTITY_EXCEEDS_LIMIT",
            f"quantity must be less than or equal to {max_quantity}",
        )
    if request.provider != "fake_h3":
        raise generation_error(
            501,
            "H3_QUERY_SOURCE_PENDING",
            "METASO query/result archive endpoint is SOURCE_PENDING; "
            "real provider calls are disabled.",
        )

    request_hash = idempotency_request_hash(request)
    existing = conn.execute(
        """
        SELECT id, request_hash
        FROM generation_batches
        WHERE created_by_user_id = ? AND idempotency_key = ?
        """,
        (actor.id, request.idempotency_key),
    ).fetchone()
    if existing is not None:
        if str(existing["request_hash"]) != request_hash:
            raise generation_error(
                409,
                "IDEMPOTENCY_CONFLICT",
                "This idempotency key was already used for a different request.",
            )
        return get_generation_batch(conn, batch_id=str(existing["id"]), actor=actor)

    prompt = require_version(
        conn,
        version_id=request.prompt_version_id,
        project_id=project_id,
        kind=H3_PROMPT_KIND,
    )
    prompt_snapshot = json.loads(str(prompt["payload_json"]))
    if prompt_snapshot.get("status") != "LOCKED":
        raise generation_error(409, "PROMPT_NOT_LOCKED", "Generation requires a LOCKED prompt.")
    first_frame = require_asset_access(
        conn,
        actor=actor,
        asset_id=request.first_frame_asset_id,
        action="generation_batch.create",
    )
    if str(first_frame["project_id"]) != project_id:
        raise generation_error(400, "ASSET_PROJECT_MISMATCH", "First frame is not in this project.")

    request_snapshot = generation_request_snapshot(request, prompt_snapshot)
    batch_id = str(uuid4())
    used_prompt_snapshot = {**prompt_snapshot, "status": "USED"}
    with conn:
        cursor = conn.execute(
            """
            UPDATE versions
            SET payload_json = ?
            WHERE id = ? AND payload_json = ?
            """,
            (
                json.dumps(used_prompt_snapshot, ensure_ascii=True, sort_keys=True),
                request.prompt_version_id,
                str(prompt["payload_json"]),
            ),
        )
        if cursor.rowcount != 1:
            raise generation_error(
                409,
                "PROMPT_ALREADY_USED",
                "This LOCKED prompt was already consumed by another batch.",
            )
        conn.execute(
            """
            INSERT INTO generation_batches (
                id,
                project_id,
                created_by_user_id,
                idempotency_key,
                request_hash,
                request_snapshot_json,
                status
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                batch_id,
                project_id,
                actor.id,
                request.idempotency_key,
                request_hash,
                json.dumps(request_snapshot, ensure_ascii=True, sort_keys=True),
                "QUEUED",
            ),
        )
        for _ in range(request.quantity):
            task_id = str(uuid4())
            conn.execute(
                """
                INSERT INTO generation_tasks (
                    id,
                    batch_id,
                    generation_mode,
                    provider,
                    model,
                    status,
                    archive_status,
                    quality_status,
                    prompt_version_id,
                    prompt_snapshot_json,
                    next_poll_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (
                    task_id,
                    batch_id,
                    "I2V",
                    request.provider,
                    H3_MODEL,
                    "PENDING",
                    "PENDING",
                    "PENDING",
                    request.prompt_version_id,
                    json.dumps(prompt_snapshot, ensure_ascii=True, sort_keys=True),
                ),
            )

    write_audit(
        conn,
        actor=actor,
        action="generation_batch.create",
        entity_type="generation_batch",
        entity_id=batch_id,
        metadata={"project_id": project_id, "quantity": request.quantity},
    )
    return get_generation_batch(conn, batch_id=batch_id, actor=actor)


def run_next_generation_task(
    conn: sqlite3.Connection,
    *,
    worker_id: str,
    provider: H3Provider,
    storage: StorageAdapter,
    first_frame_storage: StorageAdapter | None = None,
) -> TaskResult | None:
    lease = acquire_generation_task_lease(conn, worker_id=worker_id)
    if lease is None:
        return None

    task_id = str(lease["id"])
    source_storage = first_frame_storage or storage
    try:
        first_frame = storage_object_ref_from_uri(str(lease["first_frame_uri"]))
        require_storage_match(source_storage, first_frame)
        first_frame_url = source_storage.create_download_intent(
            first_frame.key,
            expires_in=FIRST_FRAME_URL_EXPIRES_IN,
            can_read=True,
        ).url
    except (StorageBackendUnavailable, StoragePermissionError, ValueError):
        mark_task_first_frame_url_sign_failed(
            conn,
            task_id=task_id,
            batch_id=str(lease["batch_id"]),
        )
        return get_task_result(conn, task_id)
    provider_request = build_h3_request(
        prompt_text=str(lease["prompt_text"]),
        first_frame_url=first_frame_url,
        duration_seconds=int(lease["output_duration_seconds"]),
        resolution=str(lease["resolution"]),
    )
    request_hash = content_hash(json.dumps(provider_request, ensure_ascii=True, sort_keys=True))
    try:
        provider_result = provider.create_image_to_video(provider_request)
    except SubmissionUncertain as exc:
        mark_task_submission_uncertain(conn, task_id=task_id, message=str(exc))
        return get_task_result(conn, task_id)

    result_asset_id: str | None = None
    archive_status = "ARCHIVED"
    error_code = None
    error_message = None
    if storage.provider not in {"fake", "local"} or not provider_result.result_url.startswith(
        "fake://"
    ):
        archive_status = "ARCHIVE_FAILED"
        error_code = "ARCHIVE_STORAGE_UNSUPPORTED"
        error_message = "Only fake/local archive storage is enabled for FakeH3 results."
    else:
        result_asset_id = str(uuid4())
        stored = storage.put_object(
            f"generation-results/{task_id}.mp4",
            provider_result.result_content,
            content_type="video/mp4",
        )
        with conn:
            conn.execute(
                """
                INSERT INTO assets (
                    id,
                    project_id,
                    kind,
                    storage_uri,
                    sha256,
                    size_bytes,
                    content_type,
                    created_by_user_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    result_asset_id,
                    str(lease["project_id"]),
                    "video",
                    stored.uri,
                    stored.sha256,
                    stored.size,
                    stored.content_type,
                    str(lease["created_by_user_id"]),
                ),
            )

    with conn:
        conn.execute(
            """
            UPDATE generation_tasks
            SET
                provider_task_id = ?,
                status = 'SUCCEEDED',
                archive_status = ?,
                quality_status = ?,
                quality_issue_codes = ?,
                result_asset_id = ?,
                provider_request_json = ?,
                error_code = ?,
                error_message_redacted = ?,
                submitted_at = COALESCE(submitted_at, CURRENT_TIMESTAMP),
                completed_at = CURRENT_TIMESTAMP,
                locked_by = NULL,
                locked_until = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (
                provider_result.provider_task_id,
                archive_status,
                provider_result.audio_quality_status,
                json.dumps(provider_result.quality_issue_codes, ensure_ascii=True),
                result_asset_id,
                json.dumps(provider_request, ensure_ascii=True, sort_keys=True),
                error_code,
                error_message,
                task_id,
            ),
        )
        conn.execute(
            """
            INSERT INTO external_call_logs (
                id,
                generation_task_id,
                provider,
                model,
                endpoint_name,
                provider_request_id,
                http_status,
                request_hash
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid4()),
                task_id,
                str(lease["provider"]),
                H3_MODEL,
                "createImageToVideo",
                provider_result.provider_task_id,
                200,
                request_hash,
            ),
        )
        refresh_batch_status(conn, batch_id=str(lease["batch_id"]))
    return get_task_result(conn, task_id)


def acquire_generation_task_lease(
    conn: sqlite3.Connection,
    *,
    worker_id: str,
) -> dict[str, Any] | None:
    mark_expired_active_leases_needing_attention(conn)
    locked_until = (datetime.now(UTC) + timedelta(seconds=GENERATION_LEASE_SECONDS)).isoformat()
    try:
        conn.execute("BEGIN IMMEDIATE")
        runtime = read_runtime_limits(conn)
        active_count = conn.execute(
            """
            SELECT COUNT(*)
            FROM generation_tasks
            WHERE status IN ('SUBMITTING', 'QUEUED', 'RUNNING', 'ARCHIVING')
            """
        ).fetchone()[0]
        if int(active_count) >= runtime["max_concurrent_h3_tasks"]:
            conn.commit()
            return None

        row = conn.execute(
            """
            UPDATE generation_tasks
            SET
                status = 'SUBMITTING',
                locked_by = ?,
                locked_until = ?,
                submitted_at = COALESCE(submitted_at, CURRENT_TIMESTAMP),
                updated_at = CURRENT_TIMESTAMP
            WHERE id = (
                SELECT id
                FROM generation_tasks
                WHERE
                    status IN ('PENDING', 'QUEUED')
                    AND (locked_until IS NULL OR locked_until <= CURRENT_TIMESTAMP)
                    AND (next_poll_at IS NULL OR next_poll_at <= CURRENT_TIMESTAMP)
                ORDER BY created_at, id
                LIMIT 1
            )
            RETURNING id
            """,
            (worker_id, locked_until),
        ).fetchone()
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    if row is None:
        return None
    return load_worker_task(conn, str(row["id"]))


def load_worker_task(conn: sqlite3.Connection, task_id: str) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT
            generation_tasks.id,
            generation_tasks.batch_id,
            generation_tasks.provider,
            generation_batches.project_id,
            generation_batches.created_by_user_id,
            generation_batches.request_snapshot_json,
            generation_tasks.prompt_snapshot_json
        FROM generation_tasks
        JOIN generation_batches ON generation_batches.id = generation_tasks.batch_id
        WHERE generation_tasks.id = ?
        """,
        (task_id,),
    ).fetchone()
    if row is None:
        raise LookupError("leased task disappeared")

    prompt_snapshot = json.loads(str(row["prompt_snapshot_json"]))
    request_snapshot = json.loads(str(row["request_snapshot_json"]))
    payload = dict(row)
    payload["prompt_text"] = prompt_snapshot["prompt_text"]
    payload["first_frame_uri"] = prompt_snapshot["first_frame_uri"]
    payload["output_duration_seconds"] = request_snapshot["output_duration_seconds"]
    payload["resolution"] = request_snapshot["resolution"]
    return payload


def mark_task_submission_uncertain(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    message: str,
) -> None:
    with conn:
        conn.execute(
            """
            UPDATE generation_tasks
            SET
                status = 'SUBMISSION_UNCERTAIN',
                error_code = 'SUBMISSION_UNCERTAIN',
                error_message_redacted = ?,
                locked_by = NULL,
                locked_until = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (message, task_id),
        )


def mark_task_first_frame_url_sign_failed(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    batch_id: str,
) -> None:
    """A provider call never started, so this is safe to mark as a normal failure."""
    with conn:
        conn.execute(
            """
            UPDATE generation_tasks
            SET
                status = 'FAILED',
                error_code = 'FIRST_FRAME_URL_SIGN_FAILED',
                error_message_redacted = 'First-frame download URL could not be signed.',
                submitted_at = NULL,
                locked_by = NULL,
                locked_until = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (task_id,),
        )
    refresh_batch_status(conn, batch_id=batch_id)


def mark_expired_active_leases_needing_attention(conn: sqlite3.Connection) -> None:
    """Do not resubmit work when a worker died after a provider call may have started."""
    with conn:
        rows = conn.execute(
            """
            SELECT id, batch_id
            FROM generation_tasks
            WHERE status IN ('SUBMITTING', 'RUNNING', 'ARCHIVING')
              AND locked_until IS NOT NULL
              AND datetime(locked_until) <= CURRENT_TIMESTAMP
            """
        ).fetchall()
        if not rows:
            return
        conn.execute(
            """
            UPDATE generation_tasks
            SET
                status = 'SUBMISSION_UNCERTAIN',
                error_code = 'LEASE_EXPIRED_NEEDS_ATTENTION',
                error_message_redacted = 'Worker lease expired during active provider operation.',
                locked_by = NULL,
                locked_until = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE status IN ('SUBMITTING', 'RUNNING', 'ARCHIVING')
              AND locked_until IS NOT NULL
              AND datetime(locked_until) <= CURRENT_TIMESTAMP
            """
        )
    for row in rows:
        refresh_batch_status(conn, batch_id=str(row["batch_id"]))


def refresh_batch_status(conn: sqlite3.Connection, *, batch_id: str) -> None:
    rows = conn.execute(
        """
        SELECT
            id,
            status,
            archive_status,
            quality_status,
            quality_issue_codes,
            result_asset_id,
            prompt_snapshot_json
        FROM generation_tasks
        WHERE batch_id = ?
        """,
        (batch_id,),
    ).fetchall()
    progress = calculate_progress([task_result(row) for row in rows])
    with conn:
        conn.execute(
            """
            UPDATE generation_batches
            SET status = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (batch_status("QUEUED", progress), batch_id),
        )


def get_task_result(conn: sqlite3.Connection, task_id: str) -> TaskResult:
    row = conn.execute(
        """
        SELECT
            id,
            status,
            archive_status,
            quality_status,
            quality_issue_codes,
            result_asset_id,
            prompt_snapshot_json
        FROM generation_tasks
        WHERE id = ?
        """,
        (task_id,),
    ).fetchone()
    if row is None:
        raise LookupError("task not found")
    return task_result(row)


def get_generation_batch(
    conn: sqlite3.Connection,
    *,
    batch_id: str,
    actor: CurrentUser,
) -> BatchResult:
    batch = conn.execute(
        """
        SELECT id, project_id, status
        FROM generation_batches
        WHERE id = ?
        """,
        (batch_id,),
    ).fetchone()
    if batch is None:
        raise generation_error(404, "BATCH_NOT_FOUND", "Generation batch does not exist.")
    require_project_access(
        conn,
        actor=actor,
        project_id=str(batch["project_id"]),
        action="generation_batch.read",
    )
    rows = conn.execute(
        """
        SELECT
            id,
            status,
            archive_status,
            quality_status,
            quality_issue_codes,
            result_asset_id,
            prompt_snapshot_json
        FROM generation_tasks
        WHERE batch_id = ?
        ORDER BY created_at, id
        """,
        (batch_id,),
    ).fetchall()
    tasks = [task_result(row) for row in rows]
    progress = calculate_progress(tasks)
    status = batch_status(str(batch["status"]), progress)
    return BatchResult(
        id=str(batch["id"]),
        status=status,
        quantity=len(tasks),
        progress=progress,
        tasks=tasks,
    )


def build_h3_request(
    *,
    prompt_text: str,
    first_frame_url: str,
    duration_seconds: int,
    resolution: str,
) -> dict[str, Any]:
    if not prompt_text.strip():
        raise ValueError("prompt_text is required")
    if duration_seconds < 4 or duration_seconds > 15:
        raise ValueError("duration must be between 4 and 15 seconds")
    if resolution not in SUPPORTED_RESOLUTIONS:
        raise ValueError("resolution must be 768P or 2K")
    return {
        "model": H3_MODEL,
        "content": [
            {"type": "text", "text": prompt_text},
            {
                "type": "image_url",
                "image_url": {"url": first_frame_url},
                "role": "first_frame",
            },
        ],
        "resolution": resolution,
        "duration": duration_seconds,
        "ratio": "adaptive",
    }


def validate_h3_request(request: dict[str, Any]) -> None:
    content = request.get("content")
    if not isinstance(content, list) or len(content) != 2:
        raise ValueError("H3 I2V content must contain text and first_frame only")
    if content[0].get("type") != "text" or not str(content[0].get("text", "")).strip():
        raise ValueError("H3 I2V content requires non-empty text")
    if content[1].get("role") != "first_frame":
        raise ValueError("H3 I2V image must use first_frame role")
    if request.get("ratio") != "adaptive":
        raise ValueError("H3 I2V ratio must be adaptive")
    duration = request.get("duration")
    if not isinstance(duration, int) or duration < 4 or duration > 15:
        raise ValueError("H3 I2V duration must be 4-15 seconds")


def require_version(
    conn: sqlite3.Connection,
    *,
    version_id: str,
    project_id: str,
    kind: str,
) -> sqlite3.Row:
    try:
        row = get_version(conn, version_id)
    except LookupError as exc:
        raise generation_error(404, "VERSION_NOT_FOUND", "Version does not exist.") from exc
    if str(row["project_id"]) != project_id or str(row["kind"]) != kind:
        raise generation_error(404, "VERSION_NOT_FOUND", "Version does not exist.")
    return row


def estimate_spoken_duration(text: str) -> float:
    if not text:
        return 0.0
    return round(len(text) / 5.0, 2)


def map_script_to_shots(text: str, shots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    sentences = [part for part in re.split(r"(?<=[。！？!?])", text) if part]
    if not sentences and text:
        sentences = [text]
    mappings: list[dict[str, Any]] = []
    for index, shot in enumerate(shots):
        segment_text = sentences[index] if index < len(sentences) else ""
        if index == len(shots) - 1 and len(sentences) > len(shots):
            segment_text = "".join(sentences[index:])
        mappings.append(
            {
                "shot_id": str(shot["shot_id"]),
                "start_time": float(shot["start_time"]),
                "end_time": float(shot["end_time"]),
                "text": segment_text,
                "estimated_duration_seconds": estimate_spoken_duration(segment_text),
            }
        )
    return mappings


def compile_prompt_text(
    *,
    script_payload: dict[str, Any],
    shot_payload: dict[str, Any],
    duration_seconds: int,
    resolution: str,
) -> str:
    lines = [
        f"生成一条 {duration_seconds} 秒、{resolution}、写实短视频，从提供的首帧自然开始。",
        "保持首帧人物身份、服装、发型、场景和光线连续。",
    ]
    mappings_by_shot = {
        str(mapping["shot_id"]): str(mapping["text"])
        for mapping in script_payload.get("shot_mappings", [])
        if isinstance(mapping, dict)
    }
    for shot in shot_payload["shots"]:
        shot_id = str(shot["shot_id"])
        spoken = mappings_by_shot.get(shot_id, str(shot.get("spoken_text", "")))
        lines.append(
            "[{start:.1f}-{end:.1f}s] {shot_type}，{composition}，{camera_motion}；"
            "{subject}{action}，场景：{scene}，转场：{transition}。口播意图：{spoken}".format(
                start=float(shot["start_time"]),
                end=float(shot["end_time"]),
                shot_type=shot["shot_type"],
                composition=shot["composition"],
                camera_motion=shot["camera_motion"],
                subject=shot["subject"],
                action=shot["action"],
                scene=shot["scene"],
                transition=shot["transition"],
                spoken=spoken,
            )
        )
    lines.append(f"口播意图：{script_payload['full_text']}")
    lines.append("环境音与音乐保持自然；不要增加无关人物，不要身份突变、肢体异常或画面闪烁。")
    return "\n".join(lines)


def generation_request_snapshot(
    request: GenerationBatchRequest,
    prompt_snapshot: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": GENERATION_SCHEMA_VERSION,
        "generation_mode": "I2V",
        "quantity": request.quantity,
        "prompt_version_id": request.prompt_version_id,
        "prompt_content_hash": prompt_snapshot["content_hash"],
        "first_frame_asset_id": request.first_frame_asset_id,
        "output_duration_seconds": request.output_duration_seconds,
        "resolution": request.resolution,
        "provider": request.provider,
        "model": H3_MODEL,
        "fake_audio_quality": request.fake_audio_quality,
    }


def idempotency_request_hash(request: GenerationBatchRequest) -> str:
    payload = request.model_dump(mode="json", exclude={"idempotency_key"})
    return content_hash(json.dumps(payload, ensure_ascii=True, sort_keys=True))


def read_runtime_limits(conn: sqlite3.Connection) -> dict[str, int]:
    row = conn.execute(
        """
        SELECT max_generation_count_per_batch, max_concurrent_h3_tasks
        FROM runtime_settings
        WHERE id = 1
        """
    ).fetchone()
    if row is None:
        return {"max_generation_count_per_batch": 4, "max_concurrent_h3_tasks": 2}
    return {
        "max_generation_count_per_batch": int(row["max_generation_count_per_batch"]),
        "max_concurrent_h3_tasks": int(row["max_concurrent_h3_tasks"]),
    }


def calculate_progress(tasks: list[TaskResult]) -> BatchProgress:
    counts = {
        "pending": 0,
        "submitting": 0,
        "queued": 0,
        "running": 0,
        "archiving": 0,
        "succeeded": 0,
        "failed": 0,
        "cancelled": 0,
        "needs_attention": 0,
    }
    terminal_count = 0
    for task in tasks:
        status = task.status
        archive_status = task.archive_status
        needs_attention = False
        if status == "SUCCEEDED":
            counts["succeeded"] += 1
            if archive_status == "ARCHIVED":
                terminal_count += 1
        elif status == "FAILED":
            counts["failed"] += 1
            terminal_count += 1
        elif status == "CANCELLED":
            counts["cancelled"] += 1
            terminal_count += 1
        elif status == "SUBMITTING":
            counts["submitting"] += 1
        elif status == "QUEUED":
            counts["queued"] += 1
        elif status == "RUNNING":
            counts["running"] += 1
        elif status == "ARCHIVING":
            counts["archiving"] += 1
        else:
            counts["pending"] += 1
        if status == "SUBMISSION_UNCERTAIN" or archive_status == "ARCHIVE_FAILED":
            needs_attention = True
        if "AUDIO_QUALITY_FAILED" in task.quality_issue_codes:
            needs_attention = True
        if needs_attention:
            counts["needs_attention"] += 1

    total_count = len(tasks)
    progress_percent = 100 if total_count == 0 else floor(terminal_count / total_count * 100)
    return BatchProgress(
        total_count=total_count,
        terminal_count=terminal_count,
        progress_percent=progress_percent,
        counts=counts,
    )


def batch_status(stored_status: str, progress: BatchProgress) -> str:
    if progress.total_count == progress.terminal_count:
        if progress.counts["failed"] or progress.counts["cancelled"]:
            return "COMPLETED_WITH_FAILURES"
        return "SUCCEEDED"
    if progress.counts["needs_attention"]:
        return "NEEDS_ATTENTION"
    return stored_status


def task_result(row: sqlite3.Row) -> TaskResult:
    quality_issue_codes = parse_json_list(row["quality_issue_codes"])
    prompt_snapshot = (
        None
        if row["prompt_snapshot_json"] is None
        else json.loads(str(row["prompt_snapshot_json"]))
    )
    return TaskResult(
        id=str(row["id"]),
        status=str(row["status"]),
        archive_status=str(row["archive_status"]),
        quality_status=str(row["quality_status"]),
        quality_issue_codes=quality_issue_codes,
        result_asset_id=None if row["result_asset_id"] is None else str(row["result_asset_id"]),
        prompt_snapshot=prompt_snapshot,
    )


def parse_json_list(value: Any) -> list[str]:
    if value is None:
        return []
    parsed = json.loads(str(value))
    if not isinstance(parsed, list):
        return []
    return [str(item) for item in parsed]


def version_result(row: sqlite3.Row) -> VersionResult:
    return VersionResult(
        id=str(row["id"]),
        project_id=str(row["project_id"]),
        asset_id=None if row["asset_id"] is None else str(row["asset_id"]),
        kind=str(row["kind"]),
        version_number=int(row["version_number"]),
        payload=json.loads(str(row["payload_json"])),
        created_by_user_id=None
        if row["created_by_user_id"] is None
        else str(row["created_by_user_id"]),
        created_at=str(row["created_at"]),
    )


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def generation_error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})
