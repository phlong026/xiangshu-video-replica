from __future__ import annotations

import argparse
import logging
import os
import time
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from app.analysis import VideoAnalysisProvider
from app.analysis_routes import (
    acquire_analysis_task,
    complete_analysis_task,
    fail_analysis_task,
    perform_analysis_task,
    prepare_analysis_task,
)
from app.character_image_generation import (
    CharacterImageProvider,
    acquire_character_generation_task,
    run_next_character_generation_task,
)
from app.db import connect_database
from app.db_pg import (
    DatabaseMode,
    check_pg_ready,
    close_pg_pool,
    pg_transaction,
    resolve_database_config,
    validate_customer_production,
)
from app.db_portable import BusinessConnection
from app.first_frame_routes import get_first_frame_quality_inspector, get_image_provider
from app.first_frames import (
    FirstFrameQualityInspector,
    ImageInput,
    ImageProvider,
    bounded_source_frame_quality_inspector,
)
from app.generation import (
    GenerationTaskSupersededError,
    H3Provider,
    H3ProviderFailed,
    H3ProviderSettingsUnavailable,
    MetasoH3Provider,
    SubmissionUncertain,
    acquire_generation_continuation_lease,
    acquire_generation_reconcile_operation,
    acquire_generation_task_lease,
    complete_generation_reconcile_operation,
    fail_generation_reconcile_operation,
    finalize_generation_direct_result,
    h3_provider_for_task,
    mark_generation_task_archiving,
    mark_generation_task_running,
    mark_task_first_frame_url_sign_failed,
    mark_task_provider_failed,
    mark_task_provider_settings_unavailable,
    mark_task_submission_uncertain,
    perform_generation_reconcile_operation,
    prepare_generation_reconcile_operation,
    prepare_generation_submission,
    reschedule_generation_poll,
    run_next_generation_task,
)
from app.hifly import HiflyClient, HiflySettingsUnavailable, hifly_client_from_settings
from app.image_tasks import (
    acquire_character_sheet_task,
    acquire_first_frame_task,
    complete_character_sheet_task,
    complete_first_frame_task,
    fail_image_task,
    perform_character_sheet_task,
    prepare_character_sheet_task,
    prepare_first_frame_task,
    record_image_task_provider,
    renew_image_task_lease,
    run_first_frame_task_outside_transaction,
    save_first_frame_task_checkpoint,
)
from app.media_routes import get_media_storage
from app.operation_costs import begin_operation_cost, complete_operation_cost
from app.oral_worker import (
    OralLeaseLostError,
    OralWorkKind,
    OralWorkResult,
    claim_oral_work,
    discard_uncommitted_oral_asset,
    finalize_oral_work,
    perform_oral_work,
    prepare_oral_work,
)
from app.script_from_audio import (
    ScriptFromAudioTaskLease,
    acquire_script_from_audio_task,
    checkpoint_script_from_audio_task,
    complete_script_from_audio_task,
    fail_script_from_audio_task,
    mark_script_from_audio_submission_started,
    perform_script_from_audio_task,
    prepare_script_from_audio_task,
)
from app.script_rewrite import (
    acquire_script_rewrite_task,
    complete_script_rewrite_task,
    fail_script_rewrite_task,
    mark_script_rewrite_submission_started,
    perform_script_rewrite_task,
    prepare_script_rewrite_task,
)
from app.simple_character import SIMPLE_CONTACT_SHEET_MODEL
from app.source_frames import (
    FFmpegSourceFrameExtractor,
    SourceFrameExtractor,
    SourceFrameQualityInspector,
    SourceFrameTaskLease,
    acquire_source_frame_task,
    complete_source_frame_task,
    delete_created_source_frames,
    fail_source_frame_task,
    perform_source_frame_extraction,
    prepare_source_frame_task,
    record_source_frame_quality_started,
)
from app.storage import (
    StorageAdapter,
    StorageBackendUnavailable,
    StoragePermissionError,
)
from app.viral_import import (
    acquire_viral_import_task,
    complete_viral_import_task,
    discard_viral_import_outcome,
    fail_viral_import_task,
    fail_viral_media_preparation,
    perform_viral_import_task,
    prepare_viral_import_task,
)
from app.viral_refresh import (
    ViralRefreshLease,
    acquire_viral_refresh_task,
    complete_viral_refresh_task,
    fail_viral_refresh_task,
)
from app.viral_routes import _collect_videos, get_viral_source_client

logger = logging.getLogger(__name__)


@contextmanager
def _sqlite_audio_connection(conn: BusinessConnection) -> Iterator[BusinessConnection]:
    yield conn


@contextmanager
def _pg_audio_connection() -> Iterator[BusinessConnection]:
    with pg_transaction() as raw_conn:
        yield BusinessConnection.postgres(raw_conn)


def _cleanup_audio_objects(
    connection: Callable[[], AbstractContextManager[BusinessConnection]],
    storage: StorageAdapter,
) -> None:
    # Leave uncertain submissions' input available until the signed URL expires.
    cutoff = (datetime.now(UTC) - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")
    with connection() as conn:
        rows = conn.execute(
            "SELECT id,audio_object_key FROM script_from_audio_tasks "
            "WHERE audio_object_key IS NOT NULL AND (status IN ('SUCCEEDED','FAILED') "
            "OR (status='SUBMISSION_UNCERTAIN' AND updated_at<=%s)) "
            "ORDER BY updated_at,id LIMIT 10",
            (cutoff,),
        ).fetchall()
    for row in rows:
        try:
            storage.delete_object(
                str(row["audio_object_key"]), actor_id="script-from-audio-cleanup"
            )
            with connection() as conn:
                conn.execute(
                    "UPDATE script_from_audio_tasks SET audio_object_key=NULL "
                    "WHERE id=%s AND audio_object_key=%s "
                    "AND status IN ('SUCCEEDED','FAILED','SUBMISSION_UNCERTAIN')",
                    (row["id"], row["audio_object_key"]),
                )
                conn.commit()
        except Exception as exc:
            logger.warning("ASR cleanup deferred for task %s: %s", row["id"], type(exc).__name__)


def _run_audio_lease(
    lease: ScriptFromAudioTaskLease,
    *,
    storage: StorageAdapter,
    connection: Callable[[], AbstractContextManager[BusinessConnection]],
) -> None:
    submission_started = False
    work = None

    def before_provider_call() -> None:
        nonlocal submission_started
        with connection() as conn:
            mark_script_from_audio_submission_started(conn, lease=lease)
        submission_started = True

    def checkpoint(provider_task_id: str | None = None) -> None:
        with connection() as conn:
            checkpoint_script_from_audio_task(conn, lease=lease, provider_task_id=provider_task_id)

    try:
        with connection() as conn:
            work = prepare_script_from_audio_task(conn, lease=lease, storage=storage)
        submission_started = work.provider_task_id is not None
        result = perform_script_from_audio_task(
            work,
            before_provider_call=before_provider_call,
            on_submitted=checkpoint,
            heartbeat=checkpoint,
        )
        with connection() as conn:
            complete_script_from_audio_task(
                conn,
                lease=lease,
                result=result,
                audio_deleted=work.audio_deleted,
            )
    except Exception as exc:
        with connection() as conn:
            fail_script_from_audio_task(
                conn,
                lease=lease,
                cause=exc,
                submission_started=submission_started,
                audio_deleted=work.audio_deleted if work is not None else False,
            )


def source_frame_semantic_inspector(
    conn: BusinessConnection,
    *,
    override: SourceFrameQualityInspector | None,
    shared_inspector: FirstFrameQualityInspector | None,
) -> SourceFrameQualityInspector | None:
    if override is not None:
        return override
    if shared_inspector is not None:
        return bounded_source_frame_quality_inspector(shared_inspector)
    try:
        return bounded_source_frame_quality_inspector(get_first_frame_quality_inspector(conn))
    except HTTPException as exc:
        detail: dict[str, Any] = exc.detail if isinstance(exc.detail, dict) else {}
        logger.warning(
            "source-frame semantic scoring unavailable: code=%s",
            detail.get("code", "UNKNOWN"),
        )
        return None


def _record_pg_source_frame_quality_started(
    lease: SourceFrameTaskLease,
    inspector: SourceFrameQualityInspector,
) -> None:
    with pg_transaction() as raw_conn:
        record_source_frame_quality_started(
            BusinessConnection.postgres(raw_conn),
            lease=lease,
            provider=getattr(inspector, "provider_name", None),
            model=getattr(inspector, "model", None),
        )


def _run_source_frame_once(
    conn: BusinessConnection,
    *,
    worker_id: str,
    storage: StorageAdapter,
    extractor: SourceFrameExtractor | None,
    quality_inspector: SourceFrameQualityInspector | None,
    shared_inspector: FirstFrameQualityInspector | None,
) -> bool:
    lease = acquire_source_frame_task(conn, worker_id=worker_id)
    if lease is None:
        return False
    plan = None
    stored = None
    try:
        plan = prepare_source_frame_task(conn, lease=lease)
        semantic_inspector = source_frame_semantic_inspector(
            conn,
            override=quality_inspector,
            shared_inspector=shared_inspector,
        )

        def mark_quality_started() -> None:
            record_source_frame_quality_started(
                conn,
                lease=lease,
                provider=getattr(semantic_inspector, "provider_name", None),
                model=getattr(semantic_inspector, "model", None),
            )

        stored = perform_source_frame_extraction(
            plan,
            storage=storage,
            extractor=extractor or FFmpegSourceFrameExtractor(),
            quality_inspector=semantic_inspector,
            before_quality_call=(mark_quality_started if semantic_inspector is not None else None),
        )
        complete_source_frame_task(
            conn,
            lease=lease,
            plan=plan,
            stored=stored,
        )
    except Exception as exc:
        if plan is not None and stored is not None:
            delete_created_source_frames(
                storage,
                stored.created_assets,
                actor_id=plan.actor.id,
            )
        fail_source_frame_task(conn, lease=lease, cause=exc)
    return True


def _run_pg_source_frame_once(
    *,
    worker_id: str,
    storage: StorageAdapter,
    extractor: SourceFrameExtractor | None,
    quality_inspector: SourceFrameQualityInspector | None,
    shared_inspector: FirstFrameQualityInspector | None,
) -> bool:
    with pg_transaction() as raw_conn:
        lease = acquire_source_frame_task(
            BusinessConnection.postgres(raw_conn),
            worker_id=worker_id,
        )
    if lease is None:
        return False
    plan = None
    stored = None
    try:
        with pg_transaction() as raw_conn:
            conn = BusinessConnection.postgres(raw_conn)
            plan = prepare_source_frame_task(conn, lease=lease)
            semantic_inspector = source_frame_semantic_inspector(
                conn,
                override=quality_inspector,
                shared_inspector=shared_inspector,
            )
        stored = perform_source_frame_extraction(
            plan,
            storage=storage,
            extractor=extractor or FFmpegSourceFrameExtractor(),
            quality_inspector=semantic_inspector,
            before_quality_call=(
                lambda: _record_pg_source_frame_quality_started(lease, semantic_inspector)
            )
            if semantic_inspector is not None
            else None,
        )
        with pg_transaction() as raw_conn:
            complete_source_frame_task(
                BusinessConnection.postgres(raw_conn),
                lease=lease,
                plan=plan,
                stored=stored,
            )
    except Exception as exc:
        if plan is not None and stored is not None:
            delete_created_source_frames(
                storage,
                stored.created_assets,
                actor_id=plan.actor.id,
            )
        with pg_transaction() as raw_conn:
            fail_source_frame_task(
                BusinessConnection.postgres(raw_conn),
                lease=lease,
                cause=exc,
            )
    return True


def _is_quality_settings_failure(exc: HTTPException) -> bool:
    detail: dict[str, Any] = exc.detail if isinstance(exc.detail, dict) else {}
    return str(detail.get("code", "")).startswith("FIRST_FRAME_QUALITY_SETTINGS_")


def _oral_settings_failure(kind: OralWorkKind) -> OralWorkResult:
    if kind.endswith("submit"):
        return OralWorkResult("failed", message="数字人服务未配置，任务未提交")
    if kind == "task_archive":
        return OralWorkResult("failed", message="数字人服务暂不可用，成片归档失败")
    return OralWorkResult("waiting", message="数字人服务暂不可用")


def _run_sqlite_oral_step(
    conn: BusinessConnection,
    *,
    worker_id: str,
    storage: StorageAdapter,
    vendor_override: HiflyClient | None,
) -> bool:
    lease = claim_oral_work(conn, worker_id=worker_id)
    if lease is None:
        return False
    prepared = prepare_oral_work(conn, lease)
    try:
        vendor = vendor_override or hifly_client_from_settings(conn)
    except HiflySettingsUnavailable:
        result = _oral_settings_failure(prepared.kind)
    else:
        result = perform_oral_work(prepared, vendor=vendor, storage=storage)
    try:
        finalize_oral_work(conn, lease=prepared, result=result)
    except OralLeaseLostError:
        discard_uncommitted_oral_asset(
            storage,
            result=result,
            actor_id=str(prepared.row.get("owner_user_id") or "") or None,
        )
        logger.warning("oral worker finalize discarded after lease loss: kind=%s", prepared.kind)
    return True


def _run_sqlite_viral_refresh_step(conn: BusinessConnection, *, worker_id: str) -> bool:
    lease = acquire_viral_refresh_task(conn, worker_id=worker_id)
    if lease is None:
        return False
    try:
        _collect_videos(
            conn,
            get_viral_source_client(conn),
            platform=lease.platform,
            sort=lease.sort,
            max_age=timedelta(0),
            read_result=False,
        )
        complete_viral_refresh_task(conn, lease=lease)
    except Exception as exc:
        conn.rollback()
        _fail_viral_refresh_if_current(conn, lease=lease, cause=exc)
    return True


def _fail_viral_refresh_if_current(
    conn: BusinessConnection, *, lease: ViralRefreshLease, cause: Exception
) -> None:
    try:
        fail_viral_refresh_task(conn, lease=lease, cause=cause)
    except HTTPException as lease_error:
        detail: dict[str, object] = (
            lease_error.detail if isinstance(lease_error.detail, dict) else {}
        )
        if detail.get("code") != "VIRAL_REFRESH_LEASE_LOST":
            raise
        conn.rollback()
        logger.warning("viral refresh failure ignored after lease loss: task=%s", lease.id)


def _run_pg_viral_refresh(lease: ViralRefreshLease) -> None:
    try:
        with pg_transaction() as raw_conn:
            client = get_viral_source_client(BusinessConnection.postgres(raw_conn))
        _collect_videos(
            None,
            client,
            platform=lease.platform,
            sort=lease.sort,
            max_age=timedelta(0),
            read_result=False,
        )
        with pg_transaction() as raw_conn:
            complete_viral_refresh_task(BusinessConnection.postgres(raw_conn), lease=lease)
    except Exception as exc:
        with pg_transaction() as raw_conn:
            _fail_viral_refresh_if_current(
                BusinessConnection.postgres(raw_conn), lease=lease, cause=exc
            )


def run_worker_once(
    conn: BusinessConnection,
    *,
    worker_id: str,
    storage: StorageAdapter,
    generation_storage: StorageAdapter | None = None,
    first_frame_storage: StorageAdapter | None = None,
    character_provider: CharacterImageProvider | None = None,
    analysis_provider: VideoAnalysisProvider | None = None,
    image_provider: ImageProvider | None = None,
    first_frame_quality_inspector: FirstFrameQualityInspector | None = None,
    source_frame_extractor: SourceFrameExtractor | None = None,
    source_frame_quality_inspector: SourceFrameQualityInspector | None = None,
    video_frame_extractor: Callable[[bytes], list[ImageInput]] | None = None,
    reconcile_provider: H3Provider | None = None,
    oral_vendor: HiflyClient | None = None,
    max_tasks: int | None = None,
) -> int:
    """Process all currently eligible tasks, then return so SQLite connections stay short-lived."""
    if max_tasks is not None and max_tasks < 1:
        raise ValueError("max_tasks must be at least 1")
    _cleanup_audio_objects(lambda: _sqlite_audio_connection(conn), storage)
    processed = 0
    while True:
        processed_round = False
        if _run_sqlite_viral_refresh_step(conn, worker_id=worker_id):
            processed += 1
            processed_round = True
            if max_tasks is not None and processed >= max_tasks:
                return processed
        viral_import_lease = acquire_viral_import_task(conn, worker_id=worker_id)
        if viral_import_lease is not None:
            viral_import_work = None
            viral_import_outcome = None
            try:
                viral_import_work = prepare_viral_import_task(
                    conn, lease=viral_import_lease, storage=storage
                )
                viral_import_outcome = perform_viral_import_task(viral_import_work)
                complete_viral_import_task(
                    conn, lease=viral_import_lease, outcome=viral_import_outcome
                )
            except Exception as exc:
                if viral_import_outcome is not None:
                    discard_viral_import_outcome(
                        storage,
                        outcome=viral_import_outcome,
                        actor_id=viral_import_lease.owner_user_id,
                    )
                conn.rollback()
                fail_viral_media_preparation(
                    conn,
                    preparation=(
                        viral_import_work.media_preparation
                        if viral_import_work is not None
                        else None
                    ),
                )
                fail_viral_import_task(conn, lease=viral_import_lease, cause=exc)
            processed += 1
            processed_round = True
            if max_tasks is not None and processed >= max_tasks:
                return processed
        if _run_sqlite_oral_step(
            conn,
            worker_id=worker_id,
            storage=storage,
            vendor_override=oral_vendor,
        ):
            processed += 1
            processed_round = True
            if max_tasks is not None and processed >= max_tasks:
                return processed
        generation_handled = False
        try:
            generation_handled = (
                run_next_generation_task(
                    conn,
                    worker_id=worker_id,
                    provider=None,
                    storage=generation_storage or storage,
                    first_frame_storage=first_frame_storage or storage,
                    visual_quality_inspector=first_frame_quality_inspector,
                    video_frame_extractor=video_frame_extractor,
                )
                is not None
            )
        except GenerationTaskSupersededError:
            # L1 (M4M5 review): the task gained a paid replacement
            # mid-flight and the late terminal write was discarded by
            # design — the lease left with the write, so this counts as a
            # processed task, not a worker fault.
            logger.info("generation task superseded while running; late terminal write discarded")
            generation_handled = True
        if generation_handled:
            processed += 1
            processed_round = True
            if max_tasks is not None and processed >= max_tasks:
                return processed
        if (
            run_next_character_generation_task(
                conn,
                worker_id=worker_id,
                provider=character_provider,
                storage=storage,
            )
            is not None
        ):
            processed += 1
            processed_round = True
            if max_tasks is not None and processed >= max_tasks:
                return processed
        analysis_lease = acquire_analysis_task(conn, worker_id=worker_id)
        if analysis_lease is not None:
            try:
                analysis_work = prepare_analysis_task(
                    conn,
                    lease=analysis_lease,
                    storage=storage,
                    provider=analysis_provider,
                )
                analysis_result = perform_analysis_task(analysis_work)
                complete_analysis_task(conn, work=analysis_work, result=analysis_result)
            except Exception as exc:
                fail_analysis_task(conn, lease=analysis_lease, cause=exc)
            processed += 1
            processed_round = True
            if max_tasks is not None and processed >= max_tasks:
                return processed
        script_rewrite_lease = acquire_script_rewrite_task(conn, worker_id=worker_id)
        if script_rewrite_lease is not None:
            submission_started = False
            try:
                rewrite_work = prepare_script_rewrite_task(
                    conn,
                    lease=script_rewrite_lease,
                )
                mark_script_rewrite_submission_started(
                    conn,
                    lease=script_rewrite_lease,
                )
                submission_started = True
                rewrite_result = perform_script_rewrite_task(rewrite_work)
                complete_script_rewrite_task(
                    conn,
                    lease=script_rewrite_lease,
                    result=rewrite_result,
                )
            except Exception as exc:
                fail_script_rewrite_task(
                    conn,
                    lease=script_rewrite_lease,
                    cause=exc,
                    submission_started=submission_started,
                )
            processed += 1
            processed_round = True
            if max_tasks is not None and processed >= max_tasks:
                return processed
        script_from_audio_lease = acquire_script_from_audio_task(conn, worker_id=worker_id)
        if script_from_audio_lease is not None:
            _run_audio_lease(
                script_from_audio_lease,
                storage=storage,
                connection=lambda: _sqlite_audio_connection(conn),
            )
            processed += 1
            processed_round = True
            if max_tasks is not None and processed >= max_tasks:
                return processed
        reconcile_lease = acquire_generation_reconcile_operation(
            conn,
            worker_id=worker_id,
        )
        if reconcile_lease is not None:
            reconcile_work = None
            reconcile_outcome = None
            try:
                reconcile_work = prepare_generation_reconcile_operation(
                    conn,
                    lease=reconcile_lease,
                    provider_factory=lambda active_conn, provider_name: (
                        reconcile_provider or h3_provider_for_task(active_conn, provider_name)
                    ),
                )
                reconcile_outcome = perform_generation_reconcile_operation(
                    reconcile_work,
                    storage=generation_storage or storage,
                    first_frame_storage=first_frame_storage or storage,
                    visual_quality_inspector=first_frame_quality_inspector,
                    video_frame_extractor=video_frame_extractor,
                )
                complete_generation_reconcile_operation(
                    conn,
                    work=reconcile_work,
                    outcome=reconcile_outcome,
                )
            except Exception as exc:
                fail_generation_reconcile_operation(
                    conn,
                    lease=reconcile_lease,
                    cause=exc,
                )
            processed += 1
            processed_round = True
            if max_tasks is not None and processed >= max_tasks:
                return processed
        if _run_source_frame_once(
            conn,
            worker_id=worker_id,
            storage=storage,
            extractor=source_frame_extractor,
            quality_inspector=source_frame_quality_inspector,
            shared_inspector=first_frame_quality_inspector,
        ):
            processed += 1
            processed_round = True
            if max_tasks is not None and processed >= max_tasks:
                return processed
        first_frame_lease = acquire_first_frame_task(conn, worker_id=worker_id)
        if first_frame_lease is not None:
            submission_started = False
            stored = None
            work = None

            def mark_submission_started() -> None:
                nonlocal submission_started
                submission_started = True

            def mark_submission_completed() -> None:
                # The next step archives the paid output and writes a durable
                # checkpoint before quality inspection begins.
                return

            def renew_first_frame_lease() -> None:
                renew_image_task_lease(
                    conn,
                    table="first_frame_tasks",
                    lease=first_frame_lease,
                )

            def persist_first_frame_checkpoint(candidates: list[Any]) -> None:
                save_first_frame_task_checkpoint(
                    conn,
                    lease=first_frame_lease,
                    candidates=candidates,
                )

            try:
                prepared = prepare_first_frame_task(
                    conn,
                    lease=first_frame_lease,
                    provider=image_provider or get_image_provider(conn),
                    quality_inspector=(
                        first_frame_quality_inspector or get_first_frame_quality_inspector(conn)
                    ),
                )
                record_image_task_provider(
                    conn,
                    table="first_frame_tasks",
                    lease=first_frame_lease,
                    provider=prepared.provider.provider_name,
                    model=prepared.plan.model,
                )
                work, stored = run_first_frame_task_outside_transaction(
                    prepared,
                    storage=first_frame_storage or storage,
                    before_provider_call=mark_submission_started,
                    after_provider_call=mark_submission_completed,
                    heartbeat=renew_first_frame_lease,
                    checkpoint_candidates=persist_first_frame_checkpoint,
                )
                complete_first_frame_task(
                    conn,
                    prepared=prepared,
                    work=work,
                    stored=stored,
                )
            except Exception as exc:
                if stored is not None and work is not None:
                    from app.first_frames import delete_created_first_frames

                    delete_created_first_frames(
                        first_frame_storage or storage,
                        stored.created_assets,
                        actor_id=work.actor.id,
                    )
                fail_image_task(
                    conn,
                    table="first_frame_tasks",
                    lease=first_frame_lease,
                    cause=exc,
                    submission_started=submission_started,
                )
            processed += 1
            processed_round = True
            if max_tasks is not None and processed >= max_tasks:
                return processed
        character_sheet_lease = acquire_character_sheet_task(conn, worker_id=worker_id)
        if character_sheet_lease is not None:
            submission_started = False
            try:
                prepared_sheet = prepare_character_sheet_task(
                    conn,
                    lease=character_sheet_lease,
                    storage=storage,
                    provider=image_provider or get_image_provider(conn),
                )
                record_image_task_provider(
                    conn,
                    table="character_sheet_tasks",
                    lease=character_sheet_lease,
                    provider=prepared_sheet.provider.provider_name,
                    model=SIMPLE_CONTACT_SHEET_MODEL,
                )
                submission_started = True
                sheet_generation = perform_character_sheet_task(prepared_sheet)
                complete_character_sheet_task(
                    conn,
                    prepared=prepared_sheet,
                    generation=sheet_generation,
                    storage=storage,
                )
            except Exception as exc:
                fail_image_task(
                    conn,
                    table="character_sheet_tasks",
                    lease=character_sheet_lease,
                    cause=exc,
                    submission_started=submission_started,
                )
            processed += 1
            processed_round = True
            if max_tasks is not None and processed >= max_tasks:
                return processed
        if not processed_round:
            break
    return processed


def _run_pg_generation_step(
    *,
    lease: dict[str, Any],
    storage: StorageAdapter,
    first_frame_storage: StorageAdapter,
    provider_override: H3Provider | None = None,
    visual_quality_inspector: FirstFrameQualityInspector | None = None,
    video_frame_extractor: Callable[[bytes], list[ImageInput]] | None = None,
) -> None:
    """Run exactly one recoverable generation state transition.

    Every database mutation is fenced by its own short ``pg_transaction``.
    Provider POST/query and input object storage calls deliberately happen
    between those transactions, so a slow vendor can never exhaust the API
    connection pool or make the desktop appear offline.
    """

    task_id = str(lease["id"])
    status = str(lease["status"])

    # Legacy archive-retry rows are claimed as SUBMITTING by the shared queue
    # acquisition SQL.  They already have a paid result URL, so move directly
    # to ARCHIVING and never issue another create call.
    if (
        status == "SUBMITTING"
        and lease.get("archive_status") == "ARCHIVE_FAILED"
        and lease.get("provider_result_url")
    ):
        with pg_transaction() as raw_conn:
            conn = BusinessConnection.postgres(raw_conn)
            mark_generation_task_archiving(
                conn,
                lease=lease,
                result_url=str(lease["provider_result_url"]),
            )
            finalize_generation_direct_result(
                conn,
                lease=lease,
                quality_status="NOT_REQUIRED",
                quality_issue_codes=[],
            )
        return
    elif status == "SUBMITTING":
        try:
            with pg_transaction() as raw_conn:
                conn = BusinessConnection.postgres(raw_conn)
                work = prepare_generation_submission(
                    conn,
                    lease=lease,
                    first_frame_storage=first_frame_storage,
                    provider=provider_override,
                )
        except H3ProviderSettingsUnavailable:
            with pg_transaction() as raw_conn:
                mark_task_provider_settings_unavailable(
                    BusinessConnection.postgres(raw_conn),
                    lease=lease,
                )
            return
        except (StorageBackendUnavailable, StoragePermissionError, ValueError):
            with pg_transaction() as raw_conn:
                mark_task_first_frame_url_sign_failed(
                    BusinessConnection.postgres(raw_conn),
                    lease=lease,
                )
            return

        if isinstance(work.provider, MetasoH3Provider):
            created_task_ids: list[str] = []

            def persist_created_task(provider_task_id: str) -> None:
                created_task_ids.append(provider_task_id)
                with pg_transaction() as raw_conn:
                    mark_generation_task_running(
                        BusinessConnection.postgres(raw_conn),
                        lease=lease,
                        provider_task_id=provider_task_id,
                        provider_request=work.provider_request,
                        request_hash=work.request_hash,
                    )

            work.provider.task_created_observer = persist_created_task
            try:
                work.provider.submit_image_to_video(work.provider_request)
            except SubmissionUncertain as exc:
                with pg_transaction() as raw_conn:
                    mark_task_submission_uncertain(
                        BusinessConnection.postgres(raw_conn),
                        task_id=task_id,
                        message=str(exc),
                        provider_task_id=created_task_ids[-1] if created_task_ids else None,
                    )
            except H3ProviderFailed as exc:
                with pg_transaction() as raw_conn:
                    conn = BusinessConnection.postgres(raw_conn)
                    if exc.provider_task_id is not None and not exc.terminal:
                        mark_task_submission_uncertain(
                            conn,
                            task_id=task_id,
                            message="Provider submission needs reconciliation.",
                            provider_task_id=exc.provider_task_id,
                        )
                    else:
                        mark_task_provider_failed(
                            conn,
                            lease=lease,
                            provider_task_id=exc.provider_task_id,
                        )
            except Exception:
                # If the provider id was observed but its persistence callback
                # failed, quarantine with that id.  A later reconciliation may
                # recover it, while an automatic paid resubmit is forbidden.
                if created_task_ids:
                    with pg_transaction() as raw_conn:
                        mark_task_submission_uncertain(
                            BusinessConnection.postgres(raw_conn),
                            task_id=task_id,
                            message="Provider task was created but persistence failed.",
                            provider_task_id=created_task_ids[-1],
                        )
                    return
                raise
            return

        # Fake/test providers remain synchronous, but all provider and storage
        # I/O is still outside PostgreSQL transactions.
        try:
            result = work.provider.create_image_to_video(work.provider_request)
        except SubmissionUncertain as exc:
            with pg_transaction() as raw_conn:
                mark_task_submission_uncertain(
                    BusinessConnection.postgres(raw_conn),
                    task_id=task_id,
                    message=str(exc),
                )
            return
        except H3ProviderFailed as exc:
            with pg_transaction() as raw_conn:
                conn = BusinessConnection.postgres(raw_conn)
                mark_task_provider_failed(
                    conn,
                    lease=lease,
                    provider_task_id=exc.provider_task_id,
                )
            return
        with pg_transaction() as raw_conn:
            conn = BusinessConnection.postgres(raw_conn)
            mark_generation_task_running(
                conn,
                lease=lease,
                provider_task_id=result.provider_task_id,
                provider_request=work.provider_request,
                request_hash=work.request_hash,
                release_lease=False,
            )
            mark_generation_task_archiving(
                conn,
                lease=lease,
                result_url=result.result_url,
            )
            finalize_generation_direct_result(
                conn,
                lease=lease,
                quality_status="NOT_REQUIRED",
                quality_issue_codes=[],
                output_seconds=result.output_seconds,
            )
        return

    if status == "RUNNING":
        provider_task_id = str(lease["provider_task_id"])
        try:
            with pg_transaction() as raw_conn:
                conn = BusinessConnection.postgres(raw_conn)
                provider = provider_override or h3_provider_for_task(conn, str(lease["provider"]))
        except H3ProviderSettingsUnavailable:
            with pg_transaction() as raw_conn:
                reschedule_generation_poll(
                    BusinessConnection.postgres(raw_conn),
                    lease=lease,
                    delay_seconds=60,
                )
            return
        try:
            query = provider.query_image_to_video(provider_task_id)
        except H3ProviderFailed as exc:
            with pg_transaction() as raw_conn:
                conn = BusinessConnection.postgres(raw_conn)
                if exc.terminal:
                    mark_task_provider_failed(
                        conn,
                        lease=lease,
                        provider_task_id=provider_task_id,
                    )
                else:
                    reschedule_generation_poll(conn, lease=lease, delay_seconds=15)
            return
        if query.status == "RUNNING":
            with pg_transaction() as raw_conn:
                reschedule_generation_poll(BusinessConnection.postgres(raw_conn), lease=lease)
            return
        if query.status in {"FAILED", "CANCELLED"}:
            with pg_transaction() as raw_conn:
                conn = BusinessConnection.postgres(raw_conn)
                mark_task_provider_failed(
                    conn,
                    lease=lease,
                    provider_task_id=provider_task_id,
                )
            return
        if query.result_url is None:
            with pg_transaction() as raw_conn:
                reschedule_generation_poll(
                    BusinessConnection.postgres(raw_conn),
                    lease=lease,
                    delay_seconds=15,
                )
            return
        with pg_transaction() as raw_conn:
            conn = BusinessConnection.postgres(raw_conn)
            mark_generation_task_archiving(
                conn,
                lease=lease,
                result_url=query.result_url,
            )
            finalize_generation_direct_result(
                conn,
                lease=lease,
                quality_status="NOT_REQUIRED",
                quality_issue_codes=[],
                output_seconds=query.output_seconds,
            )
        return

    if status == "ARCHIVING":
        with pg_transaction() as raw_conn:
            finalize_generation_direct_result(
                BusinessConnection.postgres(raw_conn),
                lease=lease,
                quality_status="NOT_REQUIRED",
                quality_issue_codes=[],
            )


def run_pg_worker_once(
    *,
    worker_id: str,
    storage: StorageAdapter,
    generation_storage: StorageAdapter | None = None,
    first_frame_storage: StorageAdapter | None = None,
    character_provider: CharacterImageProvider | None = None,
    analysis_provider: VideoAnalysisProvider | None = None,
    generation_provider: H3Provider | None = None,
    image_provider: ImageProvider | None = None,
    first_frame_quality_inspector: FirstFrameQualityInspector | None = None,
    source_frame_extractor: SourceFrameExtractor | None = None,
    source_frame_quality_inspector: SourceFrameQualityInspector | None = None,
    video_frame_extractor: Callable[[bytes], list[ImageInput]] | None = None,
    oral_vendor: HiflyClient | None = None,
    max_tasks: int | None = None,
) -> int:
    """Process all currently eligible tasks on the PostgreSQL lane.

    PostgreSQL generation is a durable multi-step state machine.  Claim and
    state writes use short fenced transactions; paid submit, one status poll,
    provider calls run with no database transaction open; generated media is not processed.
    Existing RUNNING/ARCHIVING work is always resumed before a new task, so a
    worker crash cannot turn a known provider task into a second paid POST.
    """
    if max_tasks is not None and max_tasks < 1:
        raise ValueError("max_tasks must be at least 1")
    _cleanup_audio_objects(_pg_audio_connection, storage)
    processed = 0
    while True:
        processed_round = False
        with pg_transaction() as raw_conn:
            viral_refresh_lease = acquire_viral_refresh_task(
                BusinessConnection.postgres(raw_conn), worker_id=worker_id
            )
        if viral_refresh_lease is not None:
            _run_pg_viral_refresh(viral_refresh_lease)
            processed += 1
            processed_round = True
            if max_tasks is not None and processed >= max_tasks:
                return processed
        with pg_transaction() as raw_conn:
            viral_import_lease = acquire_viral_import_task(
                BusinessConnection.postgres(raw_conn), worker_id=worker_id
            )
        if viral_import_lease is not None:
            viral_import_work = None
            viral_import_outcome = None
            try:
                with pg_transaction() as raw_conn:
                    viral_import_work = prepare_viral_import_task(
                        BusinessConnection.postgres(raw_conn),
                        lease=viral_import_lease,
                        storage=storage,
                    )
                viral_import_outcome = perform_viral_import_task(viral_import_work)
                with pg_transaction() as raw_conn:
                    complete_viral_import_task(
                        BusinessConnection.postgres(raw_conn),
                        lease=viral_import_lease,
                        outcome=viral_import_outcome,
                    )
            except Exception as exc:
                if viral_import_outcome is not None:
                    discard_viral_import_outcome(
                        storage,
                        outcome=viral_import_outcome,
                        actor_id=viral_import_lease.owner_user_id,
                    )
                with pg_transaction() as raw_conn:
                    import_conn = BusinessConnection.postgres(raw_conn)
                    fail_viral_media_preparation(
                        import_conn,
                        preparation=(
                            viral_import_work.media_preparation
                            if viral_import_work is not None
                            else None
                        ),
                    )
                    fail_viral_import_task(
                        import_conn,
                        lease=viral_import_lease,
                        cause=exc,
                    )
            processed += 1
            processed_round = True
            if max_tasks is not None and processed >= max_tasks:
                return processed
        with pg_transaction() as raw_conn:
            oral_lease = claim_oral_work(BusinessConnection.postgres(raw_conn), worker_id=worker_id)
        if oral_lease is not None:
            with pg_transaction() as raw_conn:
                conn = BusinessConnection.postgres(raw_conn)
                prepared_oral = prepare_oral_work(conn, oral_lease)
                try:
                    resolved_oral_vendor = oral_vendor or hifly_client_from_settings(conn)
                except HiflySettingsUnavailable:
                    resolved_oral_vendor = None
            if resolved_oral_vendor is None:
                oral_result = _oral_settings_failure(prepared_oral.kind)
            else:
                oral_result = perform_oral_work(
                    prepared_oral,
                    vendor=resolved_oral_vendor,
                    storage=storage,
                )
            try:
                with pg_transaction() as raw_conn:
                    finalize_oral_work(
                        BusinessConnection.postgres(raw_conn),
                        lease=prepared_oral,
                        result=oral_result,
                    )
            except OralLeaseLostError:
                discard_uncommitted_oral_asset(
                    storage,
                    result=oral_result,
                    actor_id=(str(prepared_oral.row.get("owner_user_id") or "") or None),
                )
                logger.warning(
                    "oral worker finalize discarded after lease loss: kind=%s",
                    prepared_oral.kind,
                )
            processed += 1
            processed_round = True
            if max_tasks is not None and processed >= max_tasks:
                return processed
        with pg_transaction() as raw_conn:
            conn = BusinessConnection.postgres(raw_conn)
            lease = acquire_generation_continuation_lease(conn, worker_id=worker_id)
            if lease is None:
                lease = acquire_generation_task_lease(conn, worker_id=worker_id)
        if lease is not None:
            try:
                _run_pg_generation_step(
                    lease=lease,
                    storage=generation_storage or storage,
                    first_frame_storage=first_frame_storage or storage,
                    provider_override=generation_provider,
                    visual_quality_inspector=first_frame_quality_inspector,
                    video_frame_extractor=video_frame_extractor,
                )
            except GenerationTaskSupersededError:
                # L1 (M4M5 review): the task gained a paid replacement
                # mid-flight and the late terminal write was discarded by
                # design — the lease left with the write, so this counts
                # as a processed task, not a worker fault.
                logger.info(
                    "generation task superseded while running; late terminal write discarded"
                )
            processed += 1
            processed_round = True
            if max_tasks is not None and processed >= max_tasks:
                return processed
        with pg_transaction() as raw_conn:
            conn = BusinessConnection.postgres(raw_conn)
            char_lease = acquire_character_generation_task(conn, worker_id=worker_id)
        if char_lease is not None:
            with pg_transaction() as raw_conn:
                conn = BusinessConnection.postgres(raw_conn)
                if (
                    run_next_character_generation_task(
                        conn,
                        worker_id=worker_id,
                        provider=character_provider,
                        storage=storage,
                        lease=char_lease,
                    )
                    is not None
                ):
                    processed += 1
                    processed_round = True
                    if max_tasks is not None and processed >= max_tasks:
                        return processed
        with pg_transaction() as raw_conn:
            conn = BusinessConnection.postgres(raw_conn)
            analysis_lease = acquire_analysis_task(conn, worker_id=worker_id)
        if analysis_lease is not None:
            analysis_cost_id = ""
            analysis_cost_usage: float | None = None
            try:
                # Preparation only reads settings/asset state and creates the
                # short-lived signed URL.  The paid provider call below runs
                # after this transaction has committed.
                with pg_transaction() as raw_conn:
                    conn = BusinessConnection.postgres(raw_conn)
                    analysis_work = prepare_analysis_task(
                        conn,
                        lease=analysis_lease,
                        storage=storage,
                        provider=analysis_provider,
                    )
                    analysis_cost_id = begin_operation_cost(
                        conn,
                        source_type="analysis_task",
                        source_id=analysis_lease.id,
                        subject="video_analysis_768p",
                        user_id=analysis_lease.created_by_user_id,
                        resolution="768P",
                        metadata={"resolution_basis": "default_generation_tier"},
                    )
                analysis_result = perform_analysis_task(analysis_work)
                analysis_cost_usage = analysis_lease.duration_seconds
                with pg_transaction() as raw_conn:
                    conn = BusinessConnection.postgres(raw_conn)
                    complete_operation_cost(
                        conn,
                        record_id=analysis_cost_id,
                        usage_amount=analysis_lease.duration_seconds,
                    )
                    complete_analysis_task(
                        conn,
                        work=analysis_work,
                        result=analysis_result,
                    )
            except Exception as exc:
                with pg_transaction() as raw_conn:
                    conn = BusinessConnection.postgres(raw_conn)
                    complete_operation_cost(
                        conn, record_id=analysis_cost_id, usage_amount=analysis_cost_usage
                    )
                    fail_analysis_task(conn, lease=analysis_lease, cause=exc)
            processed += 1
            processed_round = True
            if max_tasks is not None and processed >= max_tasks:
                return processed
        with pg_transaction() as raw_conn:
            script_rewrite_lease = acquire_script_rewrite_task(
                BusinessConnection.postgres(raw_conn),
                worker_id=worker_id,
            )
        if script_rewrite_lease is not None:
            submission_started = False
            try:
                with pg_transaction() as raw_conn:
                    rewrite_work = prepare_script_rewrite_task(
                        BusinessConnection.postgres(raw_conn),
                        lease=script_rewrite_lease,
                    )
                with pg_transaction() as raw_conn:
                    mark_script_rewrite_submission_started(
                        BusinessConnection.postgres(raw_conn),
                        lease=script_rewrite_lease,
                    )
                submission_started = True
                rewrite_result = perform_script_rewrite_task(rewrite_work)
                with pg_transaction() as raw_conn:
                    complete_script_rewrite_task(
                        BusinessConnection.postgres(raw_conn),
                        lease=script_rewrite_lease,
                        result=rewrite_result,
                    )
            except Exception as exc:
                with pg_transaction() as raw_conn:
                    fail_script_rewrite_task(
                        BusinessConnection.postgres(raw_conn),
                        lease=script_rewrite_lease,
                        cause=exc,
                        submission_started=submission_started,
                    )
            processed += 1
            processed_round = True
            if max_tasks is not None and processed >= max_tasks:
                return processed
        with pg_transaction() as raw_conn:
            audio_lease = acquire_script_from_audio_task(
                BusinessConnection.postgres(raw_conn),
                worker_id=worker_id,
            )
        if audio_lease is not None:
            _run_audio_lease(audio_lease, storage=storage, connection=_pg_audio_connection)
            processed += 1
            processed_round = True
            if max_tasks is not None and processed >= max_tasks:
                return processed
        with pg_transaction() as raw_conn:
            reconcile_lease = acquire_generation_reconcile_operation(
                BusinessConnection.postgres(raw_conn),
                worker_id=worker_id,
            )
        if reconcile_lease is not None:
            reconcile_work = None
            reconcile_outcome = None
            try:
                with pg_transaction() as raw_conn:
                    reconcile_work = prepare_generation_reconcile_operation(
                        BusinessConnection.postgres(raw_conn),
                        lease=reconcile_lease,
                        provider_factory=lambda active_conn, provider_name: (
                            generation_provider or h3_provider_for_task(active_conn, provider_name)
                        ),
                    )
                reconcile_outcome = perform_generation_reconcile_operation(
                    reconcile_work,
                    storage=generation_storage or storage,
                    first_frame_storage=first_frame_storage or storage,
                    visual_quality_inspector=first_frame_quality_inspector,
                    video_frame_extractor=video_frame_extractor,
                )
                with pg_transaction() as raw_conn:
                    complete_generation_reconcile_operation(
                        BusinessConnection.postgres(raw_conn),
                        work=reconcile_work,
                        outcome=reconcile_outcome,
                    )
            except Exception as exc:
                with pg_transaction() as raw_conn:
                    fail_generation_reconcile_operation(
                        BusinessConnection.postgres(raw_conn),
                        lease=reconcile_lease,
                        cause=exc,
                    )
            processed += 1
            processed_round = True
            if max_tasks is not None and processed >= max_tasks:
                return processed
        if _run_pg_source_frame_once(
            worker_id=worker_id,
            storage=storage,
            extractor=source_frame_extractor,
            quality_inspector=source_frame_quality_inspector,
            shared_inspector=first_frame_quality_inspector,
        ):
            processed += 1
            processed_round = True
            if max_tasks is not None and processed >= max_tasks:
                return processed
        with pg_transaction() as raw_conn:
            first_frame_lease = acquire_first_frame_task(
                BusinessConnection.postgres(raw_conn), worker_id=worker_id
            )
        if first_frame_lease is not None:
            submission_started = False
            stored = None
            work = None
            first_frame_cost_id = ""
            first_frame_call_number = 0

            def mark_pg_submission_started() -> None:
                nonlocal submission_started, first_frame_cost_id, first_frame_call_number
                with pg_transaction() as raw_conn:
                    conn = BusinessConnection.postgres(raw_conn)
                    # A retried transport response has no proven output count.
                    if first_frame_cost_id:
                        complete_operation_cost(
                            conn, record_id=first_frame_cost_id, usage_amount=None
                        )
                    first_frame_call_number += 1
                    first_frame_cost_id = begin_operation_cost(
                        conn,
                        source_type="first_frame_task",
                        source_id=f"{first_frame_lease.id}:{first_frame_lease.attempt}:{first_frame_call_number}",
                        subject="first_frame_image",
                        user_id=first_frame_lease.created_by_user_id,
                    )
                submission_started = True

            def record_pg_generated_images(count: int) -> None:
                nonlocal first_frame_cost_id
                # Record paid output before storage/QC, which can fail or resume
                # entirely from checkpoints without another provider call.
                with pg_transaction() as raw_conn:
                    complete_operation_cost(
                        BusinessConnection.postgres(raw_conn),
                        record_id=first_frame_cost_id,
                        usage_amount=count,
                    )
                first_frame_cost_id = ""

            def renew_pg_first_frame_lease() -> None:
                with pg_transaction() as raw_conn:
                    renew_image_task_lease(
                        BusinessConnection.postgres(raw_conn),
                        table="first_frame_tasks",
                        lease=first_frame_lease,
                    )

            def persist_pg_first_frame_checkpoint(candidates: list[Any]) -> None:
                with pg_transaction() as raw_conn:
                    save_first_frame_task_checkpoint(
                        BusinessConnection.postgres(raw_conn),
                        lease=first_frame_lease,
                        candidates=candidates,
                    )

            try:
                with pg_transaction() as raw_conn:
                    conn = BusinessConnection.postgres(raw_conn)
                    prepared = prepare_first_frame_task(
                        conn,
                        lease=first_frame_lease,
                        provider=image_provider or get_image_provider(conn),
                        quality_inspector=(
                            first_frame_quality_inspector or get_first_frame_quality_inspector(conn)
                        ),
                    )
                    record_image_task_provider(
                        conn,
                        table="first_frame_tasks",
                        lease=first_frame_lease,
                        provider=prepared.provider.provider_name,
                        model=prepared.plan.model,
                    )
                work, stored = run_first_frame_task_outside_transaction(
                    prepared,
                    storage=first_frame_storage or storage,
                    before_provider_call=mark_pg_submission_started,
                    on_generated_images=record_pg_generated_images,
                    heartbeat=renew_pg_first_frame_lease,
                    checkpoint_candidates=persist_pg_first_frame_checkpoint,
                )
                with pg_transaction() as raw_conn:
                    conn = BusinessConnection.postgres(raw_conn)
                    complete_first_frame_task(
                        conn,
                        prepared=prepared,
                        work=work,
                        stored=stored,
                    )
            except Exception as exc:
                if stored is not None and work is not None:
                    from app.first_frames import delete_created_first_frames

                    delete_created_first_frames(
                        first_frame_storage or storage,
                        stored.created_assets,
                        actor_id=work.actor.id,
                    )
                with pg_transaction() as raw_conn:
                    conn = BusinessConnection.postgres(raw_conn)
                    complete_operation_cost(conn, record_id=first_frame_cost_id, usage_amount=None)
                    fail_image_task(
                        conn,
                        table="first_frame_tasks",
                        lease=first_frame_lease,
                        cause=exc,
                        submission_started=submission_started,
                    )
            processed += 1
            processed_round = True
            if max_tasks is not None and processed >= max_tasks:
                return processed
        with pg_transaction() as raw_conn:
            character_sheet_lease = acquire_character_sheet_task(
                BusinessConnection.postgres(raw_conn), worker_id=worker_id
            )
        if character_sheet_lease is not None:
            submission_started = False
            character_sheet_cost_id = ""
            character_sheet_cost_usage: int | None = None
            try:
                with pg_transaction() as raw_conn:
                    conn = BusinessConnection.postgres(raw_conn)
                    prepared_sheet = prepare_character_sheet_task(
                        conn,
                        lease=character_sheet_lease,
                        storage=storage,
                        provider=image_provider or get_image_provider(conn),
                    )
                    record_image_task_provider(
                        conn,
                        table="character_sheet_tasks",
                        lease=character_sheet_lease,
                        provider=prepared_sheet.provider.provider_name,
                        model=SIMPLE_CONTACT_SHEET_MODEL,
                    )
                    character_sheet_cost_id = begin_operation_cost(
                        conn,
                        source_type="character_sheet_task",
                        source_id=character_sheet_lease.id,
                        subject="character_sheet_image",
                        user_id=character_sheet_lease.created_by_user_id,
                    )
                submission_started = True
                sheet_generation = perform_character_sheet_task(prepared_sheet)
                character_sheet_cost_usage = 1
                with pg_transaction() as raw_conn:
                    conn = BusinessConnection.postgres(raw_conn)
                    complete_operation_cost(conn, record_id=character_sheet_cost_id, usage_amount=1)
                    complete_character_sheet_task(
                        conn,
                        prepared=prepared_sheet,
                        generation=sheet_generation,
                        storage=storage,
                    )
            except Exception as exc:
                with pg_transaction() as raw_conn:
                    conn = BusinessConnection.postgres(raw_conn)
                    complete_operation_cost(
                        conn,
                        record_id=character_sheet_cost_id,
                        usage_amount=character_sheet_cost_usage,
                    )
                    fail_image_task(
                        conn,
                        table="character_sheet_tasks",
                        lease=character_sheet_lease,
                        cause=exc,
                        submission_started=submission_started,
                    )
            processed += 1
            processed_round = True
            if max_tasks is not None and processed >= max_tasks:
                return processed
        if not processed_round:
            break
    return processed


def run_sqlite_worker_round(
    *,
    db_path: Path,
    worker_id: str,
    max_tasks: int | None = None,
) -> int:
    try:
        with BusinessConnection.sqlite(connect_database(db_path)) as conn:
            # 云端模式下所有需要持久保留的生成资产都进入 COS；
            # 未配置 COS 的桌面开发环境仍由 get_media_storage 回退本地盘。
            asset_storage = get_media_storage(conn)
            return run_worker_once(
                conn,
                worker_id=worker_id,
                storage=asset_storage,
                generation_storage=asset_storage,
                first_frame_storage=asset_storage,
                max_tasks=max_tasks,
            )
    except HTTPException as exc:
        if not _is_quality_settings_failure(exc):
            raise
        logger.warning("visual quality settings unavailable; processing source frames locally")
        with BusinessConnection.sqlite(connect_database(db_path)) as conn:
            asset_storage = get_media_storage(conn)
            return int(
                _run_source_frame_once(
                    conn,
                    worker_id=worker_id,
                    storage=asset_storage,
                    extractor=None,
                    quality_inspector=None,
                    shared_inspector=None,
                )
            )


def run_forever(*, db_path: Path, worker_id: str, idle_seconds: float) -> None:
    while True:
        try:
            processed = run_sqlite_worker_round(db_path=db_path, worker_id=worker_id)
        except HTTPException as exc:
            code = exc.detail.get("code") if isinstance(exc.detail, dict) else exc.detail
            logger.error("generation worker configuration unavailable: %s", code)
            processed = 0
        except Exception:
            logger.exception("generation worker iteration failed")
            processed = 0
        if processed == 0:
            time.sleep(idle_seconds)


def run_pg_worker_round(*, worker_id: str, max_tasks: int | None = None) -> int:
    try:
        # The media-storage configuration lives in the business database;
        # read it once per round inside a short fenced transaction.
        with pg_transaction() as raw_conn:
            conn = BusinessConnection.postgres(raw_conn)
            asset_storage = get_media_storage(conn)
        return run_pg_worker_once(
            worker_id=worker_id,
            storage=asset_storage,
            generation_storage=asset_storage,
            first_frame_storage=asset_storage,
            max_tasks=max_tasks,
        )
    except HTTPException as exc:
        if not _is_quality_settings_failure(exc):
            raise
        logger.warning("visual quality settings unavailable; processing source frames locally")
        with pg_transaction() as raw_conn:
            asset_storage = get_media_storage(BusinessConnection.postgres(raw_conn))
        return int(
            _run_pg_source_frame_once(
                worker_id=worker_id,
                storage=asset_storage,
                extractor=None,
                quality_inspector=None,
                shared_inspector=None,
            )
        )


def run_forever_pg(*, worker_id: str, idle_seconds: float) -> None:
    while True:
        try:
            processed = run_pg_worker_round(worker_id=worker_id)
        except HTTPException as exc:
            code = exc.detail.get("code") if isinstance(exc.detail, dict) else exc.detail
            logger.error("generation worker configuration unavailable: %s", code)
            processed = 0
        except Exception:
            logger.exception("generation worker iteration failed")
            processed = 0
        if processed == 0:
            time.sleep(idle_seconds)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run local Video Replica generation tasks")
    parser.add_argument(
        "--once", action="store_true", help="process current eligible tasks then exit"
    )
    parser.add_argument("--idle-seconds", type=float, default=1.0)
    parser.add_argument("--worker-id", default=f"generation-worker-{os.getpid()}")
    parser.add_argument(
        "--max-tasks",
        type=int,
        help="with --once, stop after processing this many generation/character tasks",
    )
    args = parser.parse_args()
    if args.max_tasks is not None and not args.once:
        parser.error("--max-tasks requires --once")

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    # T05: resolve the database mode first so customer production fails closed
    # before any SQLite file is touched.
    config = resolve_database_config()
    validate_customer_production(config)
    from app.bootstrap import (
        assert_customer_production_security,
        check_customer_production_runtime_dependencies,
        is_customer_production,
    )

    assert_customer_production_security()
    if config.mode is DatabaseMode.POSTGRESQL:
        # PG worker runtime (T25/T26): prove readiness (pool + server round-trip),
        # then run the fair-queue task loop. Each task runs in the two-phase
        # claim/work shape of run_pg_worker_once (M5 review P1-1): the claim
        # transaction COMMITS the SUBMITTING transition, so a crash after the
        # claim does NOT roll the lease back — the expiry sweeper moves the
        # task to SUBMISSION_UNCERTAIN (a manual reconciliation gate) and a
        # paid provider call is never silently re-fired.
        if is_customer_production():
            ready = check_customer_production_runtime_dependencies()
        else:
            ready = check_pg_ready()
        if ready is None:  # pragma: no cover - customer gate always returns PG info
            raise RuntimeError("PostgreSQL worker readiness check returned no result")
        logger.info(
            "PostgreSQL worker runtime ready (pool_max=%d, server_now=%s)",
            ready.pool_size,
            ready.server_now.isoformat(),
        )
        if args.once:
            try:
                processed = run_pg_worker_round(
                    worker_id=args.worker_id,
                    max_tasks=args.max_tasks,
                )
            finally:
                close_pg_pool()
            logger.info("PostgreSQL worker processed %s task(s)", processed)
            return
        try:
            run_forever_pg(worker_id=args.worker_id, idle_seconds=args.idle_seconds)
        finally:
            close_pg_pool()
        return

    db_path_value = config.sqlite_path
    if not db_path_value:
        raise SystemExit("VIDEO_REPLICA_DB_PATH is required")
    db_path = Path(db_path_value)

    if args.once:
        processed = run_sqlite_worker_round(
            db_path=db_path,
            worker_id=args.worker_id,
            max_tasks=args.max_tasks,
        )
        logger.info("generation worker processed %s task(s)", processed)
        return
    run_forever(db_path=db_path, worker_id=args.worker_id, idle_seconds=args.idle_seconds)


if __name__ == "__main__":
    main()
