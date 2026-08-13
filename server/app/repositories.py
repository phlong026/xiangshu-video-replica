from __future__ import annotations

import sqlite3

from app.models import GenerationTaskLease


class GenerationTaskRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def create_minimal_task(
        self,
        *,
        user_id: str,
        project_id: str,
        batch_id: str,
        task_id: str,
    ) -> str:
        with self.conn:
            self.conn.execute(
                """
                INSERT OR IGNORE INTO users (id, username, display_name)
                VALUES (?, ?, ?)
                """,
                (user_id, user_id, user_id),
            )
            self.conn.execute(
                """
                INSERT OR IGNORE INTO projects (id, owner_user_id, name)
                VALUES (?, ?, ?)
                """,
                (project_id, user_id, project_id),
            )
            self.conn.execute(
                """
                INSERT OR IGNORE INTO generation_batches (
                    id,
                    project_id,
                    created_by_user_id,
                    idempotency_key,
                    request_hash,
                    request_snapshot_json
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (batch_id, project_id, user_id, f"{batch_id}:key", f"{batch_id}:hash", "{}"),
            )
            self.conn.execute(
                """
                INSERT INTO generation_tasks (
                    id,
                    batch_id,
                    generation_mode,
                    provider,
                    model,
                    status,
                    next_poll_at
                )
                VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (task_id, batch_id, "I2V", "metaso", "MiniMax-H3", "PENDING"),
            )
        return task_id

    def acquire_next_lease(
        self,
        *,
        worker_id: str,
        lease_seconds: int,
    ) -> GenerationTaskLease | None:
        with self.conn:
            row = self.conn.execute(
                """
                UPDATE generation_tasks
                SET
                    status = 'RUNNING',
                    attempt = attempt + 1,
                    locked_by = ?,
                    locked_until = datetime('now', ?),
                    started_at = COALESCE(started_at, CURRENT_TIMESTAMP),
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = (
                    SELECT id
                    FROM generation_tasks
                    WHERE
                        (
                            status IN ('PENDING', 'RETRY_READY')
                            OR (status = 'RUNNING' AND locked_until <= CURRENT_TIMESTAMP)
                        )
                        AND (locked_until IS NULL OR locked_until <= CURRENT_TIMESTAMP)
                        AND (next_poll_at IS NULL OR next_poll_at <= CURRENT_TIMESTAMP)
                    ORDER BY created_at, id
                    LIMIT 1
                )
                RETURNING id, status, attempt, locked_by, locked_until
                """,
                (worker_id, f"{lease_seconds} seconds"),
            ).fetchone()

        if row is None:
            return None

        return GenerationTaskLease(
            id=str(row["id"]),
            status=str(row["status"]),
            attempt=int(row["attempt"]),
            locked_by=str(row["locked_by"]),
            locked_until=str(row["locked_until"]),
        )
