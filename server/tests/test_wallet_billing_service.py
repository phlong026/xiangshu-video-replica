from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from app.db import connect_database, initialize_database
from app.internal_billing import (
    BillingInvariantError,
    InsufficientCreditsError,
    finalize_internal_billing,
    reserve_internal_billing,
)


def seed_task(conn: sqlite3.Connection, *, available_credits: int = 2) -> None:
    conn.execute(
        "INSERT INTO users (id, username, display_name, role) VALUES (?, ?, ?, ?)",
        ("user_1", "user_1", "User One", "employee"),
    )
    conn.execute(
        "INSERT INTO projects (id, owner_user_id, name) VALUES (?, ?, ?)",
        ("project_1", "user_1", "Project One"),
    )
    conn.execute(
        """
        INSERT INTO generation_batches (
            id, project_id, created_by_user_id, idempotency_key,
            request_hash, request_snapshot_json
        ) VALUES ('batch_1', 'project_1', 'user_1', 'batch-key', 'hash', '{}')
        """
    )
    conn.execute(
        """
        INSERT INTO generation_tasks (id, batch_id, generation_mode, provider, model, status)
        VALUES ('task_1', 'batch_1', 'I2V', 'fake_h3', 'MiniMax-H3', 'PENDING')
        """
    )
    conn.execute(
        """
        INSERT INTO wallets (user_id, available_credits, reserved_credits)
        VALUES ('user_1', ?, 0)
        """,
        (available_credits,),
    )
    conn.commit()


def wallet_state(conn: sqlite3.Connection) -> tuple[int, int]:
    row = conn.execute(
        "SELECT available_credits, reserved_credits FROM wallets WHERE user_id = 'user_1'"
    ).fetchone()
    assert row is not None
    return int(row["available_credits"]), int(row["reserved_credits"])


def transaction_types(conn: sqlite3.Connection) -> list[tuple[str, int]]:
    return [
        (str(row["type"]), int(row["billing_round"]))
        for row in conn.execute(
            """
            SELECT type, billing_round
            FROM wallet_transactions
            WHERE task_id = 'task_1'
            ORDER BY created_at, type
            """
        ).fetchall()
    ]


def test_reserve_moves_one_credit_and_is_idempotent(tmp_path: Path) -> None:
    with initialize_database(tmp_path / "reserve.db") as conn:
        seed_task(conn)
        with conn:
            first_round = reserve_internal_billing(
                conn,
                user_id="user_1",
                task_id="task_1",
                billing_round=1,
            )
            replay_round = reserve_internal_billing(
                conn,
                user_id="user_1",
                task_id="task_1",
                billing_round=1,
            )

        assert first_round == replay_round == 1
        assert wallet_state(conn) == (1, 1)
        assert transaction_types(conn) == [("RESERVE", 1)]


def test_reserve_rejects_insufficient_credits_without_partial_write(tmp_path: Path) -> None:
    with initialize_database(tmp_path / "insufficient.db") as conn:
        seed_task(conn, available_credits=0)

        with pytest.raises(InsufficientCreditsError):
            with conn:
                reserve_internal_billing(
                    conn,
                    user_id="user_1",
                    task_id="task_1",
                    billing_round=1,
                )

        assert wallet_state(conn) == (0, 0)
        assert transaction_types(conn) == []


def test_reserve_rejects_a_new_round_while_the_previous_round_is_active(
    tmp_path: Path,
) -> None:
    with initialize_database(tmp_path / "active-round.db") as conn:
        seed_task(conn)
        with conn:
            reserve_internal_billing(
                conn,
                user_id="user_1",
                task_id="task_1",
                billing_round=1,
            )

        with pytest.raises(BillingInvariantError):
            with conn:
                reserve_internal_billing(
                    conn,
                    user_id="user_1",
                    task_id="task_1",
                    billing_round=2,
                )

        assert wallet_state(conn) == (1, 1)
        assert transaction_types(conn) == [("RESERVE", 1)]


def test_finalize_success_settles_only_an_archived_result(tmp_path: Path) -> None:
    with initialize_database(tmp_path / "settle.db") as conn:
        seed_task(conn)
        with conn:
            reserve_internal_billing(
                conn,
                user_id="user_1",
                task_id="task_1",
                billing_round=1,
            )

        with pytest.raises(BillingInvariantError):
            with conn:
                finalize_internal_billing(conn, task_id="task_1", outcome="success")

        assert wallet_state(conn) == (1, 1)
        assert transaction_types(conn) == [("RESERVE", 1)]

        with conn:
            conn.execute(
                """
                INSERT INTO assets (
                    id, project_id, kind, storage_uri, sha256,
                    size_bytes, content_type, created_by_user_id
                ) VALUES ('result_1', 'project_1', 'video', 'cos://bucket/result.mp4',
                          'sha', 12, 'video/mp4', 'user_1')
                """
            )
            conn.execute(
                """
                UPDATE generation_tasks
                SET status = 'SUCCEEDED', archive_status = 'ARCHIVED', result_asset_id = 'result_1'
                WHERE id = 'task_1'
                """
            )
            first = finalize_internal_billing(conn, task_id="task_1", outcome="success")
            replay = finalize_internal_billing(conn, task_id="task_1", outcome="success")

        assert first.transaction_type == replay.transaction_type == "SETTLE"
        assert first.billing_round == replay.billing_round == 1
        assert wallet_state(conn) == (1, 0)
        assert transaction_types(conn) == [("RESERVE", 1), ("SETTLE", 1)]


def test_real_provider_result_must_be_archived_in_cos(tmp_path: Path) -> None:
    with initialize_database(tmp_path / "real-provider-storage.db") as conn:
        seed_task(conn)
        with conn:
            reserve_internal_billing(
                conn,
                user_id="user_1",
                task_id="task_1",
                billing_round=1,
            )
            conn.execute(
                """
                INSERT INTO assets (
                    id, project_id, kind, storage_uri, sha256,
                    size_bytes, content_type, created_by_user_id
                ) VALUES ('result_local', 'project_1', 'video',
                          'local://results/task.mp4', 'sha', 12, 'video/mp4', 'user_1')
                """
            )
            conn.execute(
                """
                UPDATE generation_tasks
                SET provider = 'metaso', status = 'SUCCEEDED',
                    archive_status = 'ARCHIVED', result_asset_id = 'result_local'
                WHERE id = 'task_1'
                """
            )

        with pytest.raises(BillingInvariantError):
            with conn:
                finalize_internal_billing(conn, task_id="task_1", outcome="success")

        assert wallet_state(conn) == (1, 1)
        assert transaction_types(conn) == [("RESERVE", 1)]


@pytest.mark.parametrize("outcome", ["failed", "cancelled"])
def test_finalize_failure_or_cancellation_releases_credit_once(
    tmp_path: Path,
    outcome: str,
) -> None:
    with initialize_database(tmp_path / f"release-{outcome}.db") as conn:
        seed_task(conn, available_credits=1)
        with conn:
            reserve_internal_billing(
                conn,
                user_id="user_1",
                task_id="task_1",
                billing_round=1,
            )
            conn.execute(
                "UPDATE generation_tasks SET status = ? WHERE id = 'task_1'",
                ("FAILED" if outcome == "failed" else "CANCELLED",),
            )
            first = finalize_internal_billing(conn, task_id="task_1", outcome=outcome)
            replay = finalize_internal_billing(conn, task_id="task_1", outcome=outcome)

        assert first.transaction_type == replay.transaction_type == "RELEASE"
        assert wallet_state(conn) == (1, 0)
        assert transaction_types(conn) == [("RELEASE", 1), ("RESERVE", 1)]


def test_released_task_can_reserve_a_new_billing_round(tmp_path: Path) -> None:
    with initialize_database(tmp_path / "retry-round.db") as conn:
        seed_task(conn, available_credits=1)
        with conn:
            reserve_internal_billing(
                conn,
                user_id="user_1",
                task_id="task_1",
                billing_round=1,
            )
            conn.execute("UPDATE generation_tasks SET status = 'FAILED' WHERE id = 'task_1'")
            finalize_internal_billing(conn, task_id="task_1", outcome="failed")
            second_round = reserve_internal_billing(
                conn,
                user_id="user_1",
                task_id="task_1",
            )

        assert second_round == 2
        assert wallet_state(conn) == (0, 1)
        assert transaction_types(conn) == [
            ("RELEASE", 1),
            ("RESERVE", 1),
            ("RESERVE", 2),
        ]


def test_historical_unbilled_task_is_a_noop(tmp_path: Path) -> None:
    with initialize_database(tmp_path / "historical.db") as conn:
        seed_task(conn)
        with conn:
            conn.execute("UPDATE generation_tasks SET status = 'FAILED' WHERE id = 'task_1'")
            result = finalize_internal_billing(conn, task_id="task_1", outcome="failed")

        assert result.transaction_type is None
        assert result.billing_round is None
        assert wallet_state(conn) == (2, 0)
        assert transaction_types(conn) == []


def test_concurrent_task_reservations_cannot_overspend(tmp_path: Path) -> None:
    db_path = tmp_path / "concurrent-reserve.db"
    with initialize_database(db_path) as conn:
        seed_task(conn, available_credits=1)
        conn.execute(
            """
            INSERT INTO generation_tasks (id, batch_id, generation_mode, provider, model, status)
            VALUES ('task_2', 'batch_1', 'I2V', 'fake_h3', 'MiniMax-H3', 'PENDING')
            """
        )
        conn.commit()

    barrier = threading.Barrier(2)
    results: list[str] = []
    result_lock = threading.Lock()

    def reserve(task_id: str) -> None:
        with connect_database(db_path) as conn:
            barrier.wait()
            try:
                conn.execute("BEGIN IMMEDIATE")
                reserve_internal_billing(
                    conn,
                    user_id="user_1",
                    task_id=task_id,
                    billing_round=1,
                )
                conn.commit()
                result = "reserved"
            except InsufficientCreditsError:
                conn.rollback()
                result = "insufficient"
        with result_lock:
            results.append(result)

    threads = [
        threading.Thread(target=reserve, args=("task_1",)),
        threading.Thread(target=reserve, args=("task_2",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(results) == ["insufficient", "reserved"]
    with connect_database(db_path) as conn:
        assert wallet_state(conn) == (0, 1)
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM wallet_transactions WHERE type = 'RESERVE'"
            ).fetchone()[0]
            == 1
        )
