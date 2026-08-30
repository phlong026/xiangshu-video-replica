"""Durable worker state for first-frame and simple-character image jobs.

These helpers deliberately keep provider calls outside database transactions.
The synchronous routes remain available for older desktop releases; new
clients enqueue here and poll the durable task row instead.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast
from uuid import uuid4

from fastapi import HTTPException

from app.auth import CurrentUser, Role
from app.character_asset_review import cleanup_publication_objects
from app.db_portable import BusinessConnection
from app.first_frames import (
    FirstFrameGenerationPlan,
    FirstFrameGenerationWork,
    ImageProvider,
    StoredFirstFrameCandidates,
    complete_first_frame_generation,
    load_first_frame_generation_work,
    perform_first_frame_generation,
    prepare_first_frame_generation,
    store_first_frame_generation,
)
from app.permissions import require_project_access
from app.simple_character import (
    PreparedSimpleCharacterGeneration,
    SimpleCharacterCreationResult,
    SimpleCharacterRegenerationResult,
    create_simple_character,
    prepare_simple_character_generation,
    regenerate_simple_character_contact_sheet,
    store_simple_character_publication,
)
from app.storage import (
    StorageAdapter,
    StorageBackendUnavailable,
    require_storage_match,
    storage_object_ref_from_uri,
)

IMAGE_TASK_LEASE_MINUTES = 12
ACTIVE_IMAGE_TASK_STATUSES = ("PENDING", "RUNNING")


@dataclass(frozen=True)
class ImageTaskLease:
    id: str
    created_by_user_id: str
    worker_id: str
    attempt: int


@dataclass(frozen=True)
class FirstFrameTaskPrepared:
    lease: ImageTaskLease
    plan: FirstFrameGenerationPlan
    provider: ImageProvider


@dataclass(frozen=True)
class CharacterSheetTaskPrepared:
    lease: ImageTaskLease
    actor: CurrentUser
    operation: Literal["CREATE", "REGENERATE"]
    project_id: str | None
    identity_id: str | None
    display_name: str
    persona_name: str
    source_content: bytes
    source_content_type: str
    source_storage_key: str
    provider: ImageProvider


def canonical_request_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def enqueue_first_frame_task(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    project_id: str,
    model: str,
    prompt: str | None,
    quantity: int,
    character_version_id: str | None,
    character_reference_selection_id: str | None,
    idempotency_key: str,
) -> sqlite3.Row:
    request_parameters = {
        "model": model,
        "prompt": prompt,
        "quantity": quantity,
        "character_version_id": character_version_id,
        "character_reference_selection_id": character_reference_selection_id,
    }
    replay = conn.execute(
        "SELECT * FROM first_frame_tasks WHERE project_id = %s AND idempotency_key = %s",
        (project_id, idempotency_key),
    ).fetchone()
    if replay is not None:
        stored_payload = json.loads(str(replay["request_json"]))
        stored_parameters = {key: stored_payload.get(key) for key in request_parameters}
        if canonical_request_hash(stored_parameters) != canonical_request_hash(request_parameters):
            raise _task_error(
                409,
                "FIRST_FRAME_TASK_IDEMPOTENCY_CONFLICT",
                "生成参数已经变化，请重新提交。",
            )
        return cast(sqlite3.Row, replay)

    # Preparation is read-only and gives the request hash the exact upstream
    # versions/assets the provider will consume.
    plan = prepare_first_frame_generation(
        conn,
        project_id=project_id,
        actor=actor,
        model=cast(Any, model),
        prompt=prompt,
        quantity=quantity,
        character_version_id=character_version_id,
        character_reference_selection_id=character_reference_selection_id,
    )
    request_payload = {
        **request_parameters,
        "source_frame_selection_version_id": plan.source_frame_selection_version_id,
        "source_frame_asset_id": plan.source_frame_asset_id,
        "reference_asset_ids": plan.character_inputs.reference_asset_ids,
    }
    request_hash = canonical_request_hash(request_payload)
    active = conn.execute(
        """
        SELECT * FROM first_frame_tasks
        WHERE project_id = %s AND request_hash = %s
          AND status IN ('PENDING','RUNNING')
        ORDER BY created_at DESC, id DESC LIMIT 1
        """,
        (project_id, request_hash),
    ).fetchone()
    if active is not None:
        return cast(sqlite3.Row, active)
    task_id = str(uuid4())
    conn.execute(
        """
        INSERT INTO first_frame_tasks (
            id, project_id, created_by_user_id, idempotency_key,
            request_hash, request_json, status
        ) VALUES (%s, %s, %s, %s, %s, %s, 'PENDING')
        ON CONFLICT DO NOTHING
        """,
        (
            task_id,
            project_id,
            actor.id,
            idempotency_key,
            request_hash,
            json.dumps(request_payload, ensure_ascii=False, sort_keys=True),
        ),
    )
    row = conn.execute("SELECT * FROM first_frame_tasks WHERE id = %s", (task_id,)).fetchone()
    if row is None:
        row = conn.execute(
            """
            SELECT * FROM first_frame_tasks
            WHERE project_id = %s AND request_hash = %s
              AND status IN ('PENDING','RUNNING')
            ORDER BY created_at DESC, id DESC LIMIT 1
            """,
            (project_id, request_hash),
        ).fetchone()
    if row is None:
        raise _task_error(409, "FIRST_FRAME_TASK_ENQUEUE_CONFLICT", "任务状态已变化，请重试。")
    conn.commit()
    return cast(sqlite3.Row, row)


def enqueue_character_sheet_task(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    operation: Literal["CREATE", "REGENERATE"],
    project_id: str | None,
    identity_id: str | None,
    display_name: str,
    persona_name: str,
    source_storage_uri: str,
    source_content_type: str,
    source_sha256: str,
    source_size_bytes: int,
    idempotency_key: str,
) -> sqlite3.Row:
    request_payload = {
        "operation": operation,
        "project_id": project_id,
        "identity_id": identity_id,
        "display_name": display_name.strip(),
        "persona_name": persona_name.strip(),
        "source_sha256": source_sha256,
        "source_content_type": source_content_type,
        "source_size_bytes": source_size_bytes,
    }
    request_hash = canonical_request_hash(request_payload)
    replay = conn.execute(
        """
        SELECT * FROM character_sheet_tasks
        WHERE created_by_user_id = %s AND idempotency_key = %s
        """,
        (actor.id, idempotency_key),
    ).fetchone()
    if replay is not None:
        if str(replay["request_hash"]) != request_hash:
            raise _task_error(
                409,
                "CHARACTER_SHEET_TASK_IDEMPOTENCY_CONFLICT",
                "人物生成参数已经变化，请重新提交。",
            )
        return cast(sqlite3.Row, replay)
    active = conn.execute(
        """
        SELECT * FROM character_sheet_tasks
        WHERE created_by_user_id = %s AND request_hash = %s
          AND status IN ('PENDING','RUNNING')
        ORDER BY created_at DESC, id DESC LIMIT 1
        """,
        (actor.id, request_hash),
    ).fetchone()
    if active is not None:
        return cast(sqlite3.Row, active)
    task_id = str(uuid4())
    conn.execute(
        """
        INSERT INTO character_sheet_tasks (
            id, project_id, identity_id, created_by_user_id, operation,
            source_storage_uri, source_content_type, source_sha256,
            source_size_bytes, idempotency_key, request_hash, request_json, status
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'PENDING')
        ON CONFLICT DO NOTHING
        """,
        (
            task_id,
            project_id,
            identity_id,
            actor.id,
            operation,
            source_storage_uri,
            source_content_type,
            source_sha256,
            source_size_bytes,
            idempotency_key,
            request_hash,
            json.dumps(request_payload, ensure_ascii=False, sort_keys=True),
        ),
    )
    row = conn.execute("SELECT * FROM character_sheet_tasks WHERE id = %s", (task_id,)).fetchone()
    if row is None:
        row = conn.execute(
            """
            SELECT * FROM character_sheet_tasks
            WHERE created_by_user_id = %s AND request_hash = %s
              AND status IN ('PENDING','RUNNING')
            ORDER BY created_at DESC, id DESC LIMIT 1
            """,
            (actor.id, request_hash),
        ).fetchone()
    if row is None:
        raise _task_error(409, "CHARACTER_SHEET_TASK_ENQUEUE_CONFLICT", "任务状态已变化，请重试。")
    conn.commit()
    return cast(sqlite3.Row, row)


def acquire_first_frame_task(conn: BusinessConnection, *, worker_id: str) -> ImageTaskLease | None:
    return _acquire_image_task(conn, table="first_frame_tasks", worker_id=worker_id)


def acquire_character_sheet_task(
    conn: BusinessConnection, *, worker_id: str
) -> ImageTaskLease | None:
    return _acquire_image_task(conn, table="character_sheet_tasks", worker_id=worker_id)


def _acquire_image_task(
    conn: BusinessConnection,
    *,
    table: Literal["first_frame_tasks", "character_sheet_tasks"],
    worker_id: str,
) -> ImageTaskLease | None:
    now = _now_text()
    locked_until = _time_text(datetime.now(UTC) + timedelta(minutes=IMAGE_TASK_LEASE_MINUTES))
    conn.execute(
        f"""
        UPDATE {table}
        SET status = 'SUBMISSION_UNCERTAIN',
            error_code = 'IMAGE_TASK_LEASE_EXPIRED',
            error_message_redacted = '任务执行中断，已停止自动重试，请联系管理员核对。',
            retryable = 0, locked_by = NULL, locked_until = NULL,
            completed_at = %s, updated_at = %s
        WHERE status = 'RUNNING' AND locked_until IS NOT NULL AND locked_until <= %s
        """,
        (now, now, now),
    )
    row = conn.execute(
        f"""
        UPDATE {table}
        SET status = 'RUNNING', attempt = attempt + 1,
            locked_by = %s, locked_until = %s,
            started_at = COALESCE(started_at, %s), updated_at = %s,
            error_code = NULL, error_message_redacted = NULL, retryable = 0
        WHERE id = (
            SELECT id FROM {table}
            WHERE status = 'PENDING'
            ORDER BY created_at, id LIMIT 1
        ) AND status = 'PENDING'
        RETURNING *
        """,
        (worker_id, locked_until, now, now),
    ).fetchone()
    conn.commit()
    if row is None:
        return None
    return ImageTaskLease(
        id=str(row["id"]),
        created_by_user_id=str(row["created_by_user_id"]),
        worker_id=worker_id,
        attempt=int(row["attempt"]),
    )


def prepare_first_frame_task(
    conn: BusinessConnection,
    *,
    lease: ImageTaskLease,
    provider: ImageProvider,
) -> FirstFrameTaskPrepared:
    row = _require_owned_task(conn, "first_frame_tasks", lease)
    payload = json.loads(str(row["request_json"]))
    actor = load_image_task_actor(conn, lease.created_by_user_id)
    try:
        plan = prepare_first_frame_generation(
            conn,
            project_id=str(row["project_id"]),
            actor=actor,
            model=cast(Any, payload["model"]),
            prompt=cast(str | None, payload.get("prompt")),
            quantity=int(payload["quantity"]),
            character_version_id=cast(str | None, payload.get("character_version_id")),
            character_reference_selection_id=cast(
                str | None, payload.get("character_reference_selection_id")
            ),
        )
    except HTTPException as exc:
        if exc.status_code == 409:
            raise _task_error(
                409,
                "FIRST_FRAME_TASK_INPUTS_CHANGED",
                "源画面或人物参考已变化，请重新提交首帧生成。",
            ) from exc
        raise
    current_payload = {
        "model": payload["model"],
        "prompt": payload.get("prompt"),
        "quantity": int(payload["quantity"]),
        "character_version_id": payload.get("character_version_id"),
        "character_reference_selection_id": payload.get("character_reference_selection_id"),
        "source_frame_selection_version_id": plan.source_frame_selection_version_id,
        "source_frame_asset_id": plan.source_frame_asset_id,
        "reference_asset_ids": plan.character_inputs.reference_asset_ids,
    }
    if canonical_request_hash(current_payload) != str(row["request_hash"]):
        raise _task_error(
            409,
            "FIRST_FRAME_TASK_INPUTS_CHANGED",
            "源画面或人物参考已变化，请重新提交首帧生成。",
        )
    conn.commit()
    return FirstFrameTaskPrepared(lease=lease, plan=plan, provider=provider)


def run_first_frame_task_outside_transaction(
    prepared: FirstFrameTaskPrepared,
    *,
    storage: StorageAdapter,
    before_provider_call: Callable[[], None] | None = None,
) -> tuple[FirstFrameGenerationWork, StoredFirstFrameCandidates]:
    work = load_first_frame_generation_work(prepared.plan, storage=storage)
    if before_provider_call is not None:
        before_provider_call()
    generated = perform_first_frame_generation(work, provider=prepared.provider)
    stored = store_first_frame_generation(work, storage=storage, generated=generated)
    return work, stored


def complete_first_frame_task(
    conn: BusinessConnection,
    *,
    prepared: FirstFrameTaskPrepared,
    work: FirstFrameGenerationWork,
    stored: StoredFirstFrameCandidates,
) -> None:
    _require_owned_task(conn, "first_frame_tasks", prepared.lease)

    def mark_task_succeeded(version: sqlite3.Row) -> None:
        now = _now_text()
        updated = conn.execute(
            """
            UPDATE first_frame_tasks
            SET status = 'SUCCEEDED', result_version_id = %s,
                result_json = %s, error_code = NULL,
                error_message_redacted = NULL, retryable = 0,
                locked_by = NULL, locked_until = NULL,
                completed_at = %s, updated_at = %s
            WHERE id = %s AND status = 'RUNNING' AND locked_by = %s
            """,
            (
                str(version["id"]),
                json.dumps({"version_id": str(version["id"])}, sort_keys=True),
                now,
                now,
                prepared.lease.id,
                prepared.lease.worker_id,
            ),
        )
        if updated.rowcount != 1:
            raise RuntimeError("first-frame task lease was lost")

    complete_first_frame_generation(
        conn,
        work=work,
        provider=prepared.provider,
        stored=stored,
        before_commit=mark_task_succeeded,
    )


def prepare_character_sheet_task(
    conn: BusinessConnection,
    *,
    lease: ImageTaskLease,
    storage: StorageAdapter,
    provider: ImageProvider,
) -> CharacterSheetTaskPrepared:
    row = _require_owned_task(conn, "character_sheet_tasks", lease)
    payload = json.loads(str(row["request_json"]))
    actor = load_image_task_actor(conn, lease.created_by_user_id)
    reference = storage_object_ref_from_uri(str(row["source_storage_uri"]))
    require_storage_match(storage, reference)
    conn.commit()
    source_content = storage.get_object(reference.key)
    if hashlib.sha256(source_content).hexdigest() != str(row["source_sha256"]):
        raise _task_error(
            409, "CHARACTER_SHEET_SOURCE_CHANGED", "人物授权图片校验失败，请重新上传。"
        )
    return CharacterSheetTaskPrepared(
        lease=lease,
        actor=actor,
        operation=cast(Literal["CREATE", "REGENERATE"], str(row["operation"])),
        project_id=None if row["project_id"] is None else str(row["project_id"]),
        identity_id=None if row["identity_id"] is None else str(row["identity_id"]),
        display_name=str(payload["display_name"]),
        persona_name=str(payload["persona_name"]),
        source_content=source_content,
        source_content_type=str(row["source_content_type"]),
        source_storage_key=reference.key,
        provider=provider,
    )


def perform_character_sheet_task(
    prepared: CharacterSheetTaskPrepared,
) -> PreparedSimpleCharacterGeneration:
    return prepare_simple_character_generation(
        source_content=prepared.source_content,
        source_content_type=prepared.source_content_type,
        display_name=prepared.display_name,
        image_provider=prepared.provider,
    )


def complete_character_sheet_task(
    conn: BusinessConnection,
    *,
    prepared: CharacterSheetTaskPrepared,
    generation: PreparedSimpleCharacterGeneration,
    storage: StorageAdapter,
) -> None:
    _require_owned_task(conn, "character_sheet_tasks", prepared.lease)

    def mark_task_succeeded(
        result: SimpleCharacterCreationResult | SimpleCharacterRegenerationResult,
    ) -> None:
        result_payload = asdict(result)
        now = _now_text()
        updated = conn.execute(
            """
            UPDATE character_sheet_tasks
            SET status = 'SUCCEEDED', result_identity_id = %s,
                result_version_id = %s, result_json = %s,
                error_code = NULL, error_message_redacted = NULL,
                retryable = 0, locked_by = NULL, locked_until = NULL,
                completed_at = %s, updated_at = %s
            WHERE id = %s AND status = 'RUNNING' AND locked_by = %s
            """,
            (
                result.identity_id,
                result.character_version_id,
                json.dumps(result_payload, ensure_ascii=False, sort_keys=True),
                now,
                now,
                prepared.lease.id,
                prepared.lease.worker_id,
            ),
        )
        if updated.rowcount != 1:
            raise RuntimeError("character-sheet task lease was lost")

    if prepared.operation == "CREATE":
        publication = store_simple_character_publication(
            actor=prepared.actor,
            storage=storage,
            source_content=prepared.source_content,
            source_content_type=prepared.source_content_type,
            display_name=prepared.display_name,
            generation=generation,
        )
        try:
            create_simple_character(
                conn,
                actor=prepared.actor,
                project_id=prepared.project_id,
                storage=storage,
                source_content=prepared.source_content,
                source_content_type=prepared.source_content_type,
                display_name=prepared.display_name,
                persona_name=prepared.persona_name,
                prepared_publication=publication,
                before_commit=mark_task_succeeded,
            )
        except Exception:
            cleanup_publication_objects(storage, list(publication.object_keys))
            raise
    else:
        if prepared.identity_id is None:
            raise _task_error(409, "CHARACTER_SHEET_IDENTITY_MISSING", "人物身份不存在。")
        regenerate_simple_character_contact_sheet(
            conn,
            actor=prepared.actor,
            identity_id=prepared.identity_id,
            storage=storage,
            prepared_generation=generation,
            source_content_override=prepared.source_content,
            before_commit=mark_task_succeeded,
        )
    if prepared.operation == "CREATE":
        try:
            storage.delete_object(prepared.source_storage_key, actor_id=prepared.actor.id)
        except (OSError, StorageBackendUnavailable):
            pass


def fail_image_task(
    conn: BusinessConnection,
    *,
    table: Literal["first_frame_tasks", "character_sheet_tasks"],
    lease: ImageTaskLease,
    cause: Exception,
    submission_started: bool,
) -> None:
    code = "IMAGE_TASK_FAILED"
    message = "图像生成失败，请稍后重试。"
    retryable = True
    known_failure = False
    if isinstance(cause, HTTPException):
        detail: dict[str, Any] = cause.detail if isinstance(cause.detail, dict) else {}
        code = str(detail.get("code") or code)
        message = str(detail.get("message") or message)
        retryable = cause.status_code in {429, 502, 503, 504}
        known_failure = True
    elif isinstance(cause, (StorageBackendUnavailable, OSError, ValueError)):
        code = "IMAGE_TASK_STORAGE_UNAVAILABLE"
        message = "素材库暂不可用，请稍后重试。"
        known_failure = not submission_started
    status = "FAILED" if known_failure or not submission_started else "SUBMISSION_UNCERTAIN"
    if status == "SUBMISSION_UNCERTAIN":
        code = "IMAGE_TASK_SUBMISSION_UNCERTAIN"
        message = "任务执行结果需要核对，已停止自动重试，请联系管理员。"
        retryable = False
    now = _now_text()
    conn.execute(
        f"""
        UPDATE {table}
        SET status = %s, error_code = %s, error_message_redacted = %s,
            retryable = %s, locked_by = NULL, locked_until = NULL,
            completed_at = %s, updated_at = %s
        WHERE id = %s AND status = 'RUNNING' AND locked_by = %s
        """,
        (
            status,
            code,
            message,
            1 if retryable else 0,
            now,
            now,
            lease.id,
            lease.worker_id,
        ),
    )
    conn.commit()


def load_image_task(
    conn: BusinessConnection,
    *,
    table: Literal["first_frame_tasks", "character_sheet_tasks"],
    task_id: str,
) -> sqlite3.Row:
    row = conn.execute(f"SELECT * FROM {table} WHERE id = %s", (task_id,)).fetchone()
    if row is None:
        raise _task_error(404, "IMAGE_TASK_NOT_FOUND", "生成任务不存在。")
    return cast(sqlite3.Row, row)


def latest_image_task(
    conn: BusinessConnection,
    *,
    table: Literal["first_frame_tasks", "character_sheet_tasks"],
    owner_column: Literal["project_id", "created_by_user_id"],
    owner_id: str,
) -> sqlite3.Row | None:
    return cast(
        sqlite3.Row | None,
        conn.execute(
            f"""
            SELECT * FROM {table} WHERE {owner_column} = %s
            ORDER BY CASE WHEN status IN ('PENDING','RUNNING') THEN 0 ELSE 1 END,
                     created_at DESC, id DESC LIMIT 1
            """,
            (owner_id,),
        ).fetchone(),
    )


def load_image_task_actor(conn: BusinessConnection, user_id: str) -> CurrentUser:
    row = conn.execute(
        "SELECT id, username, display_name, role FROM users WHERE id = %s", (user_id,)
    ).fetchone()
    if row is None:
        raise RuntimeError("image task actor is unavailable")
    return CurrentUser(
        id=str(row["id"]),
        username=str(row["username"]),
        display_name=str(row["display_name"]),
        role=cast(Role, str(row["role"])),
    )


def require_first_frame_task_access(
    conn: BusinessConnection, *, actor: CurrentUser, row: sqlite3.Row
) -> None:
    require_project_access(
        conn,
        actor=actor,
        project_id=str(row["project_id"]),
        action="first_frame.task.read",
    )


def require_character_sheet_task_access(*, actor: CurrentUser, row: sqlite3.Row) -> None:
    if actor.role != "admin" and str(row["created_by_user_id"]) != actor.id:
        raise _task_error(403, "CHARACTER_SHEET_TASK_FORBIDDEN", "无权查看该人物生成任务。")


def _require_owned_task(
    conn: BusinessConnection,
    table: Literal["first_frame_tasks", "character_sheet_tasks"],
    lease: ImageTaskLease,
) -> sqlite3.Row:
    row = load_image_task(conn, table=table, task_id=lease.id)
    if str(row["status"]) != "RUNNING" or str(row["locked_by"]) != lease.worker_id:
        raise RuntimeError("image task lease was lost")
    return row


def _now_text() -> str:
    return _time_text(datetime.now(UTC))


def _time_text(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S")


def _task_error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})
