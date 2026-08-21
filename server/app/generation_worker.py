from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import time
from pathlib import Path

from fastapi import HTTPException

from app.character_image_generation import (
    CharacterImageProvider,
    run_next_character_generation_task,
)
from app.db import connect_database
from app.db_pg import (
    DatabaseMode,
    check_pg_ready,
    close_pg_pool,
    resolve_database_config,
    validate_customer_production,
)
from app.generation import run_next_generation_task
from app.media_routes import get_media_storage
from app.storage import StorageAdapter

logger = logging.getLogger(__name__)


def run_worker_once(
    conn: sqlite3.Connection,
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


def run_forever(*, db_path: Path, worker_id: str, idle_seconds: float) -> None:
    while True:
        try:
            with connect_database(db_path) as conn:
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
    if config.mode is DatabaseMode.POSTGRESQL:
        # PG worker runtime: prove readiness (pool + server round-trip). The
        # fair-queue task loop moves onto PG with T24/T25 (Lane C); until then
        # the PG mode intentionally stops after the ready check — but loudly:
        # exiting 0 would look healthy to systemd's Restart=on-failure and
        # silently drop the worker (M0 review H1 fail-open).
        ready = check_pg_ready()
        logger.info(
            "PostgreSQL worker runtime ready (pool_max=%d, server_now=%s); "
            "PG task loop lands with the fair-queue lane (T25)",
            ready.pool_size,
            ready.server_now.isoformat(),
        )
        close_pg_pool()
        raise SystemExit(
            "PostgreSQL worker task loop is not implemented until T25 (fair-queue "
            "lane); exiting with failure so process supervision does not mistake "
            "this for a healthy idle worker"
        )

    db_path_value = config.sqlite_path
    if not db_path_value:
        raise SystemExit("VIDEO_REPLICA_DB_PATH is required")
    db_path = Path(db_path_value)

    if args.once:
        with connect_database(db_path) as conn:
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
