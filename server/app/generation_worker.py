from __future__ import annotations

import argparse
import logging
import os
import time
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
from app.first_frame_routes import get_image_provider
from app.first_frames import ImageProvider
from app.generation import (
    H3Provider,
    H3ProviderFailed,
    H3ProviderSettingsUnavailable,
    MetasoH3Provider,
    SubmissionUncertain,
    acquire_generation_continuation_lease,
    acquire_generation_task_lease,
    finalize_generation_archive,
    h3_audio_quality,
    h3_provider_for_task,
    mark_generation_task_archiving,
    mark_generation_task_running,
    mark_task_first_frame_url_sign_failed,
    mark_task_provider_failed,
    mark_task_provider_settings_unavailable,
    mark_task_submission_uncertain,
    prepare_generation_submission,
    release_generation_archive_retry,
    reschedule_generation_poll,
    run_next_generation_task,
    store_generation_result,
)
from app.image_tasks import (
    acquire_character_sheet_task,
    acquire_first_frame_task,
    complete_character_sheet_task,
    complete_first_frame_task,
    fail_image_task,
    perform_character_sheet_task,
    prepare_character_sheet_task,
    prepare_first_frame_task,
    run_first_frame_task_outside_transaction,
)
from app.media_routes import get_media_storage
from app.storage import (
    StorageAdapter,
    StorageBackendUnavailable,
    StoragePermissionError,
)

logger = logging.getLogger(__name__)


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
    max_tasks: int | None = None,
) -> int:
    """Process all currently eligible tasks, then return so SQLite connections stay short-lived."""
    if max_tasks is not None and max_tasks < 1:
        raise ValueError("max_tasks must be at least 1")
    processed = 0
    while True:
        processed_round = False
        if (
            run_next_generation_task(
                conn,
                worker_id=worker_id,
                provider=None,
                storage=generation_storage or storage,
                first_frame_storage=first_frame_storage or storage,
            )
            is not None
        ):
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
        first_frame_lease = acquire_first_frame_task(conn, worker_id=worker_id)
        if first_frame_lease is not None:
            submission_started = False
            stored = None
            work = None

            def mark_submission_started() -> None:
                nonlocal submission_started
                submission_started = True

            try:
                prepared = prepare_first_frame_task(
                    conn,
                    lease=first_frame_lease,
                    provider=image_provider or get_image_provider(conn),
                )
                work, stored = run_first_frame_task_outside_transaction(
                    prepared,
                    storage=first_frame_storage or storage,
                    before_provider_call=mark_submission_started,
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
) -> None:
    """Run exactly one recoverable generation state transition.

    Every database mutation is fenced by its own short ``pg_transaction``.
    Provider POST/query/download and object storage calls deliberately happen
    between those transactions, so a slow vendor can never exhaust the API
    connection pool or make the desktop appear offline.
    """

    task_id = str(lease["id"])
    batch_id = str(lease["batch_id"])
    status = str(lease["status"])

    # Legacy archive-retry rows are claimed as SUBMITTING by the shared queue
    # acquisition SQL.  They already have a paid result URL, so move directly
    # to ARCHIVING and never issue another create call.
    if (
        status == "SUBMITTING"
        and lease.get("archive_status") == "ARCHIVE_FAILED"
        and lease.get("provider_result_url")
    ):
        try:
            with pg_transaction() as raw_conn:
                conn = BusinessConnection.postgres(raw_conn)
                provider = provider_override or h3_provider_for_task(conn, str(lease["provider"]))
                mark_generation_task_archiving(
                    conn,
                    lease=lease,
                    result_url=str(lease["provider_result_url"]),
                )
        except H3ProviderSettingsUnavailable:
            with pg_transaction() as raw_conn:
                release_generation_archive_retry(BusinessConnection.postgres(raw_conn), lease=lease)
            return
        status = "ARCHIVING"
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
                    task_id=task_id,
                    batch_id=batch_id,
                )
            return
        except (StorageBackendUnavailable, StoragePermissionError, ValueError):
            with pg_transaction() as raw_conn:
                mark_task_first_frame_url_sign_failed(
                    BusinessConnection.postgres(raw_conn),
                    task_id=task_id,
                    batch_id=batch_id,
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
                            task_id=task_id,
                            batch_id=batch_id,
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
                mark_task_provider_failed(
                    BusinessConnection.postgres(raw_conn),
                    task_id=task_id,
                    batch_id=batch_id,
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
            )
            mark_generation_task_archiving(
                conn,
                lease=lease,
                result_url=result.result_url,
            )
        try:
            stored = store_generation_result(
                storage,
                task_id=task_id,
                content=result.result_content,
            )
        except (StorageBackendUnavailable, StoragePermissionError, ValueError):
            with pg_transaction() as raw_conn:
                release_generation_archive_retry(BusinessConnection.postgres(raw_conn), lease=lease)
            return
        try:
            with pg_transaction() as raw_conn:
                finalize_generation_archive(
                    BusinessConnection.postgres(raw_conn),
                    lease=lease,
                    stored=stored,
                    audio_quality_status=result.audio_quality_status,
                    quality_issue_codes=result.quality_issue_codes,
                )
        except Exception:
            storage.delete_object(stored.key, actor_id=None)
            raise
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
                        task_id=task_id,
                        batch_id=batch_id,
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
                mark_task_provider_failed(
                    BusinessConnection.postgres(raw_conn),
                    task_id=task_id,
                    batch_id=batch_id,
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
            mark_generation_task_archiving(
                BusinessConnection.postgres(raw_conn),
                lease=lease,
                result_url=query.result_url,
            )
        return

    if status == "ARCHIVING":
        try:
            with pg_transaction() as raw_conn:
                conn = BusinessConnection.postgres(raw_conn)
                provider = provider_override or h3_provider_for_task(conn, str(lease["provider"]))
        except H3ProviderSettingsUnavailable:
            with pg_transaction() as raw_conn:
                release_generation_archive_retry(BusinessConnection.postgres(raw_conn), lease=lease)
            return
        try:
            content = provider.download_result(str(lease["provider_result_url"]))
            audio_quality_status, quality_issue_codes = h3_audio_quality(content)
            stored = store_generation_result(storage, task_id=task_id, content=content)
        except (H3ProviderFailed, StorageBackendUnavailable, StoragePermissionError, ValueError):
            with pg_transaction() as raw_conn:
                release_generation_archive_retry(BusinessConnection.postgres(raw_conn), lease=lease)
            return
        try:
            with pg_transaction() as raw_conn:
                finalize_generation_archive(
                    BusinessConnection.postgres(raw_conn),
                    lease=lease,
                    stored=stored,
                    audio_quality_status=audio_quality_status,
                    quality_issue_codes=quality_issue_codes,
                )
        except Exception:
            storage.delete_object(stored.key, actor_id=None)
            raise


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
    max_tasks: int | None = None,
) -> int:
    """Process all currently eligible tasks on the PostgreSQL lane.

    PostgreSQL generation is a durable multi-step state machine.  Claim and
    state writes use short fenced transactions; paid submit, one status poll,
    result download and archive each run with no database transaction open.
    Existing RUNNING/ARCHIVING work is always resumed before a new task, so a
    worker crash cannot turn a known provider task into a second paid POST.
    """
    if max_tasks is not None and max_tasks < 1:
        raise ValueError("max_tasks must be at least 1")
    processed = 0
    while True:
        processed_round = False
        with pg_transaction() as raw_conn:
            conn = BusinessConnection.postgres(raw_conn)
            lease = acquire_generation_continuation_lease(conn, worker_id=worker_id)
            if lease is None:
                lease = acquire_generation_task_lease(conn, worker_id=worker_id)
        if lease is not None:
            _run_pg_generation_step(
                lease=lease,
                storage=generation_storage or storage,
                first_frame_storage=first_frame_storage or storage,
                provider_override=generation_provider,
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
                analysis_result = perform_analysis_task(analysis_work)
                with pg_transaction() as raw_conn:
                    conn = BusinessConnection.postgres(raw_conn)
                    complete_analysis_task(
                        conn,
                        work=analysis_work,
                        result=analysis_result,
                    )
            except Exception as exc:
                with pg_transaction() as raw_conn:
                    conn = BusinessConnection.postgres(raw_conn)
                    fail_analysis_task(conn, lease=analysis_lease, cause=exc)
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

            def mark_pg_submission_started() -> None:
                nonlocal submission_started
                submission_started = True

            try:
                with pg_transaction() as raw_conn:
                    conn = BusinessConnection.postgres(raw_conn)
                    prepared = prepare_first_frame_task(
                        conn,
                        lease=first_frame_lease,
                        provider=image_provider or get_image_provider(conn),
                    )
                work, stored = run_first_frame_task_outside_transaction(
                    prepared,
                    storage=first_frame_storage or storage,
                    before_provider_call=mark_pg_submission_started,
                )
                with pg_transaction() as raw_conn:
                    complete_first_frame_task(
                        BusinessConnection.postgres(raw_conn),
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
                    fail_image_task(
                        BusinessConnection.postgres(raw_conn),
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
            try:
                with pg_transaction() as raw_conn:
                    conn = BusinessConnection.postgres(raw_conn)
                    prepared_sheet = prepare_character_sheet_task(
                        conn,
                        lease=character_sheet_lease,
                        storage=storage,
                        provider=image_provider or get_image_provider(conn),
                    )
                submission_started = True
                sheet_generation = perform_character_sheet_task(prepared_sheet)
                with pg_transaction() as raw_conn:
                    complete_character_sheet_task(
                        BusinessConnection.postgres(raw_conn),
                        prepared=prepared_sheet,
                        generation=sheet_generation,
                        storage=storage,
                    )
            except Exception as exc:
                with pg_transaction() as raw_conn:
                    fail_image_task(
                        BusinessConnection.postgres(raw_conn),
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


def run_forever(*, db_path: Path, worker_id: str, idle_seconds: float) -> None:
    while True:
        try:
            with BusinessConnection.sqlite(connect_database(db_path)) as conn:
                # 云端模式下所有需要持久保留的生成资产都进入 COS；
                # 未配置 COS 的桌面开发环境仍由 get_media_storage 回退本地盘。
                asset_storage = get_media_storage(conn)
                processed = run_worker_once(
                    conn,
                    worker_id=worker_id,
                    storage=asset_storage,
                    generation_storage=asset_storage,
                    first_frame_storage=asset_storage,
                )
        except HTTPException as exc:
            code = exc.detail.get("code") if isinstance(exc.detail, dict) else exc.detail
            logger.error("generation worker configuration unavailable: %s", code)
            processed = 0
        except Exception:
            logger.exception("generation worker iteration failed")
            processed = 0
        if processed == 0:
            time.sleep(idle_seconds)


def run_forever_pg(*, worker_id: str, idle_seconds: float) -> None:
    while True:
        try:
            # The media-storage configuration lives in the business database;
            # read it once per round inside a short fenced transaction.
            with pg_transaction() as raw_conn:
                conn = BusinessConnection.postgres(raw_conn)
                asset_storage = get_media_storage(conn)
            processed = run_pg_worker_once(
                worker_id=worker_id,
                storage=asset_storage,
                generation_storage=asset_storage,
                first_frame_storage=asset_storage,
            )
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
                with pg_transaction() as raw_conn:
                    conn = BusinessConnection.postgres(raw_conn)
                    asset_storage = get_media_storage(conn)
                processed = run_pg_worker_once(
                    worker_id=args.worker_id,
                    storage=asset_storage,
                    generation_storage=asset_storage,
                    first_frame_storage=asset_storage,
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
        with BusinessConnection.sqlite(connect_database(db_path)) as conn:
            asset_storage = get_media_storage(conn)
            processed = run_worker_once(
                conn,
                worker_id=args.worker_id,
                storage=asset_storage,
                generation_storage=asset_storage,
                first_frame_storage=asset_storage,
                max_tasks=args.max_tasks,
            )
        logger.info("generation worker processed %s task(s)", processed)
        return
    run_forever(db_path=db_path, worker_id=args.worker_id, idle_seconds=args.idle_seconds)


if __name__ == "__main__":
    main()
