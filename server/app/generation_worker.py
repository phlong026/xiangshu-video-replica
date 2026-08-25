from __future__ import annotations

import argparse
import logging
import os
import time
from pathlib import Path

from fastapi import HTTPException

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
from app.generation import acquire_generation_task_lease, run_next_generation_task
from app.media_routes import get_media_storage
from app.storage import StorageAdapter

logger = logging.getLogger(__name__)


def run_worker_once(
    conn: BusinessConnection,
    *,
    worker_id: str,
    storage: StorageAdapter,
    generation_storage: StorageAdapter | None = None,
    first_frame_storage: StorageAdapter | None = None,
    character_provider: CharacterImageProvider | None = None,
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
        if not processed_round:
            break
    return processed


def run_pg_worker_once(
    *,
    worker_id: str,
    storage: StorageAdapter,
    generation_storage: StorageAdapter | None = None,
    first_frame_storage: StorageAdapter | None = None,
    character_provider: CharacterImageProvider | None = None,
    max_tasks: int | None = None,
) -> int:
    """Process all currently eligible tasks on the PostgreSQL lane.

    Two-phase per task (M5 review P1-1 — the earlier single fenced
    transaction rolling the lease, the paid provider call, the result write
    and the release into one commit could lose the whole round on a crash
    mid-poll: the task fell back to PENDING and the next worker re-sent the
    paid POST; it also blinded the global concurrency gate until the round
    ended):

    1. Claim — a short fenced transaction: ``acquire_generation_task_lease``
       commits the SUBMITTING transition, the per-user slot increment and the
       global concurrency count immediately. A crash after this point leaves
       the task SUBMITTING; the expiry sweeper moves it to
       SUBMISSION_UNCERTAIN (a manual reconciliation gate), so a provider
       call is never silently double-fired.
    2. Work — a second fenced transaction: the provider poll, the terminal
       write and the slot release. The claim's durability makes this a
       crash-and-recover boundary, not a retry.

    This mirrors the SQLite lane's per-block commit shape and the T27 drain
    worker's two-transaction shape.
    """
    if max_tasks is not None and max_tasks < 1:
        raise ValueError("max_tasks must be at least 1")
    processed = 0
    while True:
        processed_round = False
        with pg_transaction() as raw_conn:
            conn = BusinessConnection.postgres(raw_conn)
            lease = acquire_generation_task_lease(conn, worker_id=worker_id)
        if lease is not None:
            with pg_transaction() as raw_conn:
                conn = BusinessConnection.postgres(raw_conn)
                if (
                    run_next_generation_task(
                        conn,
                        worker_id=worker_id,
                        provider=None,
                        storage=generation_storage or storage,
                        first_frame_storage=first_frame_storage or storage,
                        lease=lease,
                    )
                    is not None
                ):
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
