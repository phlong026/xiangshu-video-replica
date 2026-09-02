"""Durable worker state for first-frame and simple-character image jobs.

These helpers deliberately keep provider calls outside database transactions.
The synchronous routes remain available for older desktop releases; new
clients enqueue here and poll the durable task row instead.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from typing import Any, Literal, cast
from uuid import uuid4

from fastapi import HTTPException

from app.auth import CurrentUser, Role
from app.character_asset_review import cleanup_publication_objects
from app.db_portable import BusinessConnection
from app.first_frames import (
    FIRST_FRAME_IMAGE_CONTENT_TYPES,
    MAX_FIRST_FRAME_QUALITY_ATTEMPTS,
    FakeFirstFrameQualityInspector,
    FirstFrameGenerationPlan,
    FirstFrameGenerationWork,
    FirstFrameQualityInspector,
    FirstFrameQualityResult,
    GeneratedImage,
    ImageProvider,
    StoredFirstFrameCandidates,
    complete_first_frame_generation,
    load_first_frame_generation_work,
    perform_first_frame_generation,
    prepare_first_frame_generation,
    store_first_frame_generation,
)
from app.permissions import require_not_auditor, require_project_access
from app.simple_character import (
    SIMPLE_CONTACT_SHEET_MODEL,
    PreparedSimpleCharacterGeneration,
    SimpleCharacterCreationResult,
    SimpleCharacterRegenerationResult,
    SimpleSceneLookResult,
    create_simple_character,
    create_simple_scene_look,
    prepare_simple_character_generation,
    regenerate_simple_character_contact_sheet,
    scene_contact_sheet_prompt,
    store_simple_character_publication,
)
from app.storage import (
    StorageAdapter,
    StorageBackendUnavailable,
    require_storage_match,
    storage_object_ref_from_uri,
)

# Each external image/QC request has a 240-second timeout and may retry once.
# Keep a moderate crash-detection window and renew it between every long I/O
# phase instead of relying on one fixed lease for the whole multi-round job.
IMAGE_TASK_LEASE_MINUTES = 30
ACTIVE_IMAGE_TASK_STATUSES = ("PENDING", "RUNNING")

logger = logging.getLogger(__name__)


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
    quality_inspector: FirstFrameQualityInspector
    checkpoint_candidates: list[dict[str, object]]


@dataclass(frozen=True)
class CharacterSheetTaskPrepared:
    lease: ImageTaskLease
    actor: CurrentUser
    operation: Literal["CREATE", "REGENERATE", "SCENE"]
    project_id: str | None
    identity_id: str | None
    display_name: str
    persona_name: str
    scene_description: str | None
    costume_description: str | None
    source_content: bytes
    source_content_type: str
    source_storage_key: str
    provider: ImageProvider


def canonical_request_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _first_frame_checkpoint_candidates(row: sqlite3.Row) -> list[dict[str, object]]:
    raw = row["result_json"]
    if raw is None:
        return []
    candidates = _parse_first_frame_checkpoint(raw)
    if candidates is None:
        raise _task_error(
            409,
            "FIRST_FRAME_CHECKPOINT_INVALID",
            "已保存的首帧生成结果无效，请联系管理员核对。",
        )
    return candidates


def _parse_first_frame_checkpoint(raw: object) -> list[dict[str, object]] | None:
    try:
        payload = json.loads(str(raw))
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    checkpoint = payload.get("checkpoint")
    if not isinstance(checkpoint, dict) or checkpoint.get("schema_version") != 1:
        return None
    candidates = checkpoint.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        return None
    if not all(isinstance(candidate, dict) for candidate in candidates):
        return None
    return cast(list[dict[str, object]], candidates)


def _has_recoverable_first_frame_checkpoint(raw: object) -> bool:
    try:
        payload = json.loads(str(raw))
    except json.JSONDecodeError:
        return False
    if not isinstance(payload, dict):
        return False
    execution = payload.get("execution")
    if not isinstance(execution, dict):
        return False
    if not str(execution.get("provider") or "") or not str(execution.get("model") or ""):
        return False
    candidates = _parse_first_frame_checkpoint(raw)
    if candidates is None:
        return False
    for candidate in candidates:
        quality = candidate.get("quality")
        if quality is None or (isinstance(quality, dict) and quality.get("passed") is True):
            return True
    return False


def _checkpoint_candidate_payload(candidate: GeneratedImage) -> dict[str, object]:
    if candidate.stored_candidate is None or candidate.quality_attempt is None:
        raise ValueError("first-frame checkpoint candidate is not archived")
    return {
        **candidate.stored_candidate,
        "quality_attempt": candidate.quality_attempt,
        "quality": (
            candidate.quality.model_dump(mode="json") if candidate.quality is not None else None
        ),
    }


def _load_first_frame_checkpoint_candidates(
    candidates: list[dict[str, object]],
    *,
    storage: StorageAdapter,
) -> list[GeneratedImage]:
    loaded: list[GeneratedImage] = []
    try:
        for candidate in candidates:
            content_type = str(candidate["content_type"])
            if content_type not in FIRST_FRAME_IMAGE_CONTENT_TYPES:
                raise ValueError("unsupported checkpoint image type")
            if not str(candidate["storage_key"]):
                raise ValueError("checkpoint storage key is missing")
            reference = storage_object_ref_from_uri(str(candidate["storage_uri"]))
            require_storage_match(storage, reference)
            content = storage.get_object(reference.key)
            if len(content) != int(str(candidate["size_bytes"])):
                raise ValueError("checkpoint image size mismatch")
            if hashlib.sha256(content).hexdigest() != str(candidate["sha256"]):
                raise ValueError("checkpoint image checksum mismatch")
            quality_payload = candidate.get("quality")
            quality = (
                None
                if quality_payload is None
                else FirstFrameQualityResult.model_validate(quality_payload)
            )
            quality_attempt = int(str(candidate["quality_attempt"]))
            if not 1 <= quality_attempt <= MAX_FIRST_FRAME_QUALITY_ATTEMPTS:
                raise ValueError("checkpoint quality attempt is invalid")
            loaded.append(
                GeneratedImage(
                    content=content,
                    content_type=content_type,
                    quality=quality,
                    stored_candidate={
                        key: value
                        for key, value in candidate.items()
                        if key not in {"quality", "quality_attempt"}
                    },
                    quality_attempt=quality_attempt,
                )
            )
    except (KeyError, TypeError, ValueError) as exc:
        raise _task_error(
            409,
            "FIRST_FRAME_CHECKPOINT_INVALID",
            "已保存的首帧生成结果校验失败，请联系管理员核对。",
        ) from exc
    except (OSError, StorageBackendUnavailable) as exc:
        raise _task_error(
            503,
            "FIRST_FRAME_CHECKPOINT_UNAVAILABLE",
            "已生成的首帧暂时无法从素材库读取，将稍后重试。",
        ) from exc
    return loaded


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
    # Authorization must precede the idempotent replay lookup. Otherwise an
    # unrelated user who guesses a project/key pair can observe another
    # account's durable task without entering the normal preparation path.
    require_not_auditor(
        conn,
        actor=actor,
        action="first_frame_task.create",
        entity_type="project",
        entity_id=project_id,
    )
    require_project_access(
        conn,
        actor=actor,
        project_id=project_id,
        action="first_frame_task.create",
    )
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
        "project_appearance_fingerprint": plan.project_appearance.fingerprint,
        "source_analysis_version_id": plan.project_appearance.source_analysis_version_id,
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
    checkpoint_json: str | None = None
    previous = conn.execute(
        """
        SELECT result_json, error_code FROM first_frame_tasks
        WHERE project_id = %s AND request_hash = %s
          AND status IN ('FAILED','SUBMISSION_UNCERTAIN')
          AND result_json IS NOT NULL
        ORDER BY created_at DESC, id DESC LIMIT 1
        """,
        (project_id, request_hash),
    ).fetchone()
    checkpoint_blocked = previous is not None and previous["error_code"] in {
        "IMAGE_TASK_PROVIDER_CHANGED",
        "IMAGE_TASK_EXECUTION_UNKNOWN",
    }
    if (
        previous is not None
        and not checkpoint_blocked
        and _has_recoverable_first_frame_checkpoint(previous["result_json"])
    ):
        checkpoint_json = str(previous["result_json"])
    task_id = str(uuid4())
    conn.execute(
        """
        INSERT INTO first_frame_tasks (
            id, project_id, created_by_user_id, idempotency_key,
            request_hash, request_json, result_json, status
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, 'PENDING')
        ON CONFLICT DO NOTHING
        """,
        (
            task_id,
            project_id,
            actor.id,
            idempotency_key,
            request_hash,
            json.dumps(request_payload, ensure_ascii=False, sort_keys=True),
            checkpoint_json,
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
    operation: Literal["CREATE", "REGENERATE", "SCENE"],
    project_id: str | None,
    identity_id: str | None,
    display_name: str,
    persona_name: str,
    source_storage_uri: str,
    source_content_type: str,
    source_sha256: str,
    source_size_bytes: int,
    idempotency_key: str,
    scene_description: str | None = None,
    costume_description: str | None = None,
) -> sqlite3.Row:
    request_payload = {
        "operation": operation,
        "project_id": project_id,
        "identity_id": identity_id,
        "display_name": display_name.strip(),
        "persona_name": persona_name.strip(),
        "scene_description": None if scene_description is None else scene_description.strip(),
        "costume_description": (
            None if costume_description is None else costume_description.strip()
        ),
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
    if table == "first_frame_tasks":
        expired = conn.execute(
            """
            SELECT id, result_json, attempt FROM first_frame_tasks
            WHERE status = 'RUNNING'
              AND locked_until IS NOT NULL
              AND locked_until::timestamptz <= now()
            """
        ).fetchall()
        for task in expired:
            if int(task["attempt"]) >= 3 or not _has_recoverable_first_frame_checkpoint(
                task["result_json"]
            ):
                continue
            conn.execute(
                """
                UPDATE first_frame_tasks
                SET status = 'PENDING',
                    error_code = 'FIRST_FRAME_CHECKPOINT_RESUME',
                    error_message_redacted = '生成图片已保存，正在恢复后续处理。',
                    retryable = 1, locked_by = NULL, locked_until = NULL,
                    completed_at = NULL, updated_at = CURRENT_TIMESTAMP
                WHERE id = %s AND status = 'RUNNING'
                """,
                (str(task["id"]),),
            )
            logger.warning(
                "resuming expired first-frame task from archived checkpoint",
                extra={"task_id": str(task["id"]), "attempt": int(task["attempt"])},
            )
    conn.execute(
        f"""
        UPDATE {table}
        SET status = 'SUBMISSION_UNCERTAIN',
            error_code = 'IMAGE_TASK_LEASE_EXPIRED',
            error_message_redacted = '任务执行中断，已停止自动重试，请联系管理员核对。',
            retryable = 0, locked_by = NULL, locked_until = NULL,
            completed_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
        WHERE status = 'RUNNING'
          AND locked_until IS NOT NULL
          AND locked_until::timestamptz <= now()
        """
    )
    row = conn.execute(
        f"""
        UPDATE {table}
        SET status = 'RUNNING', attempt = attempt + 1,
            locked_by = %s,
            locked_until = now() + interval '{IMAGE_TASK_LEASE_MINUTES} minutes',
            started_at = COALESCE(started_at::timestamptz, CURRENT_TIMESTAMP),
            updated_at = CURRENT_TIMESTAMP,
            error_code = NULL, error_message_redacted = NULL, retryable = 0
        WHERE id = (
            SELECT id FROM {table}
            WHERE status = 'PENDING'
            ORDER BY created_at, id LIMIT 1
        ) AND status = 'PENDING'
        RETURNING *
        """,
        (worker_id,),
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


def renew_image_task_lease(
    conn: BusinessConnection,
    *,
    table: Literal["first_frame_tasks", "character_sheet_tasks"],
    lease: ImageTaskLease,
) -> None:
    """Extend an owned lease before the next bounded external-I/O phase."""

    updated = conn.execute(
        f"""
        UPDATE {table}
        SET locked_until = now() + interval '{IMAGE_TASK_LEASE_MINUTES} minutes',
            updated_at = CURRENT_TIMESTAMP
        WHERE id = %s AND status = 'RUNNING' AND locked_by = %s
        """,
        (lease.id, lease.worker_id),
    )
    conn.commit()
    if updated.rowcount != 1:
        raise RuntimeError("image task lease was lost")


def record_image_task_provider(
    conn: BusinessConnection,
    *,
    table: Literal["first_frame_tasks", "character_sheet_tasks"],
    lease: ImageTaskLease,
    provider: str,
    model: str,
) -> None:
    """Persist the selected provider before any paid image call can begin."""

    current = conn.execute(
        f"SELECT result_json FROM {table} WHERE id = %s AND status = 'RUNNING' AND locked_by = %s",
        (lease.id, lease.worker_id),
    ).fetchone()
    if current is None:
        raise RuntimeError("image task lease was lost")
    payload: dict[str, object] = {}
    if current["result_json"] is not None:
        try:
            stored_payload = json.loads(str(current["result_json"]))
        except json.JSONDecodeError as exc:
            raise _task_error(
                409,
                "IMAGE_TASK_RESULT_INVALID",
                "已保存的图像任务记录无效，请联系管理员核对。",
            ) from exc
        if not isinstance(stored_payload, dict):
            raise _task_error(
                409,
                "IMAGE_TASK_RESULT_INVALID",
                "已保存的图像任务记录无效，请联系管理员核对。",
            )
        payload.update(stored_payload)
    existing_execution = payload.get("execution")
    if existing_execution is not None:
        if not isinstance(existing_execution, dict):
            raise _task_error(
                409,
                "IMAGE_TASK_RESULT_INVALID",
                "已保存的图像任务记录无效，请联系管理员核对。",
            )
        if (
            existing_execution.get("provider") != provider
            or existing_execution.get("model") != model
        ):
            raise _task_error(
                409,
                "IMAGE_TASK_PROVIDER_CHANGED",
                "图像服务配置已变化，不能复用旧生成结果，请重新提交任务。",
            )
    else:
        if "checkpoint" in payload:
            raise _task_error(
                409,
                "IMAGE_TASK_EXECUTION_UNKNOWN",
                "旧版图像生成结果缺少服务来源，不能自动复用，请重新提交任务。",
            )
        payload["execution"] = {
            "provider": provider,
            "model": model,
        }
    updated = conn.execute(
        f"""
        UPDATE {table}
        SET result_json = %s, updated_at = CURRENT_TIMESTAMP
        WHERE id = %s AND status = 'RUNNING' AND locked_by = %s
        """,
        (json.dumps(payload, ensure_ascii=False, sort_keys=True), lease.id, lease.worker_id),
    )
    conn.commit()
    if updated.rowcount != 1:
        raise RuntimeError("image task lease was lost")


def prepare_first_frame_task(
    conn: BusinessConnection,
    *,
    lease: ImageTaskLease,
    provider: ImageProvider,
    quality_inspector: FirstFrameQualityInspector | None = None,
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
        "project_appearance_fingerprint": plan.project_appearance.fingerprint,
        "source_analysis_version_id": plan.project_appearance.source_analysis_version_id,
    }
    if canonical_request_hash(current_payload) != str(row["request_hash"]):
        raise _task_error(
            409,
            "FIRST_FRAME_TASK_INPUTS_CHANGED",
            "源画面或人物参考已变化，请重新提交首帧生成。",
        )
    conn.commit()
    return FirstFrameTaskPrepared(
        lease=lease,
        plan=plan,
        provider=provider,
        quality_inspector=quality_inspector or FakeFirstFrameQualityInspector(),
        checkpoint_candidates=_first_frame_checkpoint_candidates(row),
    )


def save_first_frame_task_checkpoint(
    conn: BusinessConnection,
    *,
    lease: ImageTaskLease,
    candidates: list[GeneratedImage],
) -> None:
    current = conn.execute(
        "SELECT result_json FROM first_frame_tasks WHERE id = %s",
        (lease.id,),
    ).fetchone()
    execution: object = None
    if current is not None and current["result_json"] is not None:
        try:
            current_payload = json.loads(str(current["result_json"]))
            if isinstance(current_payload, dict):
                execution = current_payload.get("execution")
        except json.JSONDecodeError:
            execution = None
    payload: dict[str, object] = {
        "checkpoint": {
            "schema_version": 1,
            "candidates": [_checkpoint_candidate_payload(candidate) for candidate in candidates],
        }
    }
    if isinstance(execution, dict):
        payload["execution"] = execution
    updated = conn.execute(
        """
        UPDATE first_frame_tasks
        SET result_json = %s, updated_at = CURRENT_TIMESTAMP
        WHERE id = %s AND status = 'RUNNING' AND locked_by = %s
        """,
        (json.dumps(payload, ensure_ascii=False, sort_keys=True), lease.id, lease.worker_id),
    )
    conn.commit()
    if updated.rowcount != 1:
        raise RuntimeError("first-frame task lease was lost")
    logger.info(
        "saved first-frame provider output checkpoint",
        extra={"task_id": lease.id, "candidate_count": len(candidates)},
    )


def run_first_frame_task_outside_transaction(
    prepared: FirstFrameTaskPrepared,
    *,
    storage: StorageAdapter,
    before_provider_call: Callable[[], None] | None = None,
    after_provider_call: Callable[[], None] | None = None,
    heartbeat: Callable[[], None] | None = None,
    checkpoint_candidates: Callable[[list[GeneratedImage]], None] | None = None,
) -> tuple[FirstFrameGenerationWork, StoredFirstFrameCandidates]:
    if heartbeat is not None:
        heartbeat()
    work = load_first_frame_generation_work(prepared.plan, storage=storage)
    resumed_candidates = _load_first_frame_checkpoint_candidates(
        prepared.checkpoint_candidates,
        storage=storage,
    )
    uncheckpointed_assets: list[tuple[str, str]] = []

    def archive_generated(
        generated: list[GeneratedImage],
        quality_attempt: int,
    ) -> list[GeneratedImage]:
        stored = store_first_frame_generation(work, storage=storage, generated=generated)
        uncheckpointed_assets.extend(stored.created_assets)
        return [
            replace(
                image,
                stored_candidate={
                    key: value for key, value in candidate.items() if key != "quality"
                },
                quality_attempt=quality_attempt,
            )
            for image, candidate in zip(generated, stored.candidates, strict=True)
        ]

    def persist_checkpoint(candidates: list[GeneratedImage]) -> None:
        if checkpoint_candidates is None:
            return
        checkpoint_candidates(candidates)
        uncheckpointed_assets.clear()

    try:
        generated = perform_first_frame_generation(
            work,
            provider=prepared.provider,
            quality_inspector=prepared.quality_inspector,
            before_provider_call=before_provider_call,
            after_provider_call=after_provider_call,
            heartbeat=heartbeat,
            resumed_candidates=resumed_candidates,
            archive_generated=archive_generated if checkpoint_candidates is not None else None,
            checkpoint_candidates=persist_checkpoint if checkpoint_candidates is not None else None,
        )
    except BaseException:
        if uncheckpointed_assets:
            from app.first_frames import delete_created_first_frames

            delete_created_first_frames(
                storage,
                uncheckpointed_assets,
                actor_id=work.actor.id,
            )
        raise
    if heartbeat is not None:
        heartbeat()
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
        operation=cast(Literal["CREATE", "REGENERATE", "SCENE"], str(row["operation"])),
        project_id=None if row["project_id"] is None else str(row["project_id"]),
        identity_id=None if row["identity_id"] is None else str(row["identity_id"]),
        display_name=str(payload["display_name"]),
        persona_name=str(payload["persona_name"]),
        scene_description=(
            None if payload.get("scene_description") is None else str(payload["scene_description"])
        ),
        costume_description=(
            None
            if payload.get("costume_description") is None
            else str(payload["costume_description"])
        ),
        source_content=source_content,
        source_content_type=str(row["source_content_type"]),
        source_storage_key=reference.key,
        provider=provider,
    )


def perform_character_sheet_task(
    prepared: CharacterSheetTaskPrepared,
) -> PreparedSimpleCharacterGeneration:
    if prepared.operation != "SCENE":
        return prepare_simple_character_generation(
            source_content=prepared.source_content,
            source_content_type=prepared.source_content_type,
            display_name=prepared.display_name,
            image_provider=prepared.provider,
            scene_description=prepared.scene_description,
            costume_description=prepared.costume_description,
        )
    if prepared.scene_description is None or prepared.costume_description is None:
        raise _task_error(409, "SCENE_LOOK_INPUTS_MISSING", "场景造型参数不完整。")
    return prepare_simple_character_generation(
        source_content=prepared.source_content,
        source_content_type=prepared.source_content_type,
        display_name=prepared.display_name,
        image_provider=prepared.provider,
        scene_description=prepared.scene_description,
        costume_description=prepared.costume_description,
        prompt_override=scene_contact_sheet_prompt(
            scene_description=prepared.scene_description,
            costume_description=prepared.costume_description,
        ),
    )


def latest_scene_look_task(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    identity_id: str,
) -> sqlite3.Row | None:
    owner_filter = "" if actor.role == "admin" else "AND created_by_user_id = %s"
    params: tuple[object, ...] = (
        (identity_id,) if actor.role == "admin" else (identity_id, actor.id)
    )
    return cast(
        sqlite3.Row | None,
        conn.execute(
            f"""
            SELECT * FROM character_sheet_tasks
            WHERE identity_id = %s AND operation = 'SCENE' {owner_filter}
            ORDER BY CASE WHEN status IN ('PENDING','RUNNING') THEN 0 ELSE 1 END,
                     created_at DESC, id DESC LIMIT 1
            """,
            params,
        ).fetchone(),
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
        result: (
            SimpleCharacterCreationResult
            | SimpleCharacterRegenerationResult
            | SimpleSceneLookResult
        ),
    ) -> None:
        result_payload = asdict(result)
        result_payload["provider"] = prepared.provider.provider_name
        result_payload["model"] = SIMPLE_CONTACT_SHEET_MODEL
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
    elif prepared.operation == "REGENERATE":
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
    else:
        if (
            prepared.identity_id is None
            or prepared.scene_description is None
            or prepared.costume_description is None
        ):
            raise _task_error(409, "SCENE_LOOK_INPUTS_MISSING", "场景造型参数不完整。")
        create_simple_scene_look(
            conn,
            actor=prepared.actor,
            identity_id=prepared.identity_id,
            scene_name=prepared.persona_name,
            scene_description=prepared.scene_description,
            costume_description=prepared.costume_description,
            storage=storage,
            prepared_generation=generation,
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
        # A validation/business rejection is a known outcome. A 5xx after a
        # paid provider call is not: the upstream may have accepted or even
        # completed the request before the transport/quality/storage failure.
        known_failure = cause.status_code < 500
    elif isinstance(cause, (StorageBackendUnavailable, OSError, ValueError)):
        code = "IMAGE_TASK_STORAGE_UNAVAILABLE"
        message = "素材库暂不可用，请稍后重试。"
        known_failure = not submission_started
    current = conn.execute(
        f"SELECT result_json FROM {table} WHERE id = %s AND status = 'RUNNING' AND locked_by = %s",
        (lease.id, lease.worker_id),
    ).fetchone()
    resumable_checkpoint = (
        table == "first_frame_tasks"
        and current is not None
        and _has_recoverable_first_frame_checkpoint(current["result_json"])
    )
    if resumable_checkpoint and not known_failure and lease.attempt < 2:
        conn.execute(
            """
            UPDATE first_frame_tasks
            SET status = 'PENDING', error_code = %s, error_message_redacted = %s,
                retryable = 1, locked_by = NULL, locked_until = NULL,
                completed_at = NULL, updated_at = %s
            WHERE id = %s AND status = 'RUNNING' AND locked_by = %s
            """,
            (
                code,
                "生成图片已保存，将自动继续后续处理。",
                _now_text(),
                lease.id,
                lease.worker_id,
            ),
        )
        conn.commit()
        logger.warning(
            "requeued first-frame task from archived checkpoint",
            extra={"task_id": lease.id, "attempt": lease.attempt, "error_code": code},
        )
        return
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


def latest_base_character_sheet_task(
    conn: BusinessConnection,
    *,
    owner_id: str,
) -> sqlite3.Row | None:
    return cast(
        sqlite3.Row | None,
        conn.execute(
            """
            SELECT * FROM character_sheet_tasks
            WHERE created_by_user_id = %s AND operation IN ('CREATE','REGENERATE')
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
        raise _task_error(404, "IMAGE_TASK_NOT_FOUND", "生成任务不存在。")


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
