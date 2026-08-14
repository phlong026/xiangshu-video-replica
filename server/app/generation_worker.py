from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import time
from pathlib import Path

from fastapi import HTTPException

from app.db import connect_database
from app.generation import run_next_generation_task
from app.media_routes import get_media_storage
from app.storage import StorageAdapter

logger = logging.getLogger(__name__)


def run_worker_once(
    conn: sqlite3.Connection,
    *,
    worker_id: str,
    storage: StorageAdapter,
) -> int:
    """Process all currently eligible tasks, then return so SQLite connections stay short-lived."""
    processed = 0
    while (
        run_next_generation_task(
            conn,
            worker_id=worker_id,
            provider=None,
            storage=storage,
        )
        is not None
    ):
        processed += 1
    return processed


def run_forever(*, db_path: Path, worker_id: str, idle_seconds: float) -> None:
    while True:
        try:
            with connect_database(db_path) as conn:
                storage = get_media_storage(conn)
                processed = run_worker_once(conn, worker_id=worker_id, storage=storage)
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
    args = parser.parse_args()

    db_path_value = os.environ.get("VIDEO_REPLICA_DB_PATH")
    if not db_path_value:
        raise SystemExit("VIDEO_REPLICA_DB_PATH is required")
    db_path = Path(db_path_value)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.once:
        with connect_database(db_path) as conn:
            storage = get_media_storage(conn)
            processed = run_worker_once(conn, worker_id=args.worker_id, storage=storage)
        logger.info("generation worker processed %s task(s)", processed)
        return
    run_forever(db_path=db_path, worker_id=args.worker_id, idle_seconds=args.idle_seconds)


if __name__ == "__main__":
    main()
