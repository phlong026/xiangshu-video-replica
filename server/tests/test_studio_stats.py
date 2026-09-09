from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from app.auth import CurrentUser
from app.db import initialize_database
from app.db_portable import BusinessConnection
from app.studio_routes import StudioStatsResponse, studio_task_stats, utc_cutoff_for_beijing_day

_NOW = "2026-09-06 03:00:00"
_DAYS_AGO = "2026-09-03 03:00:00"


def seed_stats_scene(connection) -> None:
    connection.executemany(
        "INSERT INTO users (id, username, display_name, role) VALUES (?, ?, ?, ?)",
        [
            ("admin_1", "admin_1", "Admin One", "admin"),
            ("employee_1", "employee_1", "Employee One", "employee"),
            ("employee_2", "employee_2", "Employee Two", "employee"),
        ],
    )
    connection.executemany(
        "INSERT INTO projects (id, owner_user_id, name) VALUES (?, ?, ?)",
        [("p-1", "employee_1", "项目一"), ("p-2", "employee_2", "项目二")],
    )
    connection.executemany(
        """
        INSERT INTO generation_batches (
            id, project_id, created_by_user_id, idempotency_key, request_hash,
            request_snapshot_json, status, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            ("b-1", "p-1", "employee_1", "ik-1", "h-1", "{}", "SUCCEEDED", _NOW, _NOW),
            ("b-2", "p-2", "employee_2", "ik-2", "h-2", "{}", "SUCCEEDED", _DAYS_AGO, _DAYS_AGO),
            ("b-hidden", "p-1", "employee_1", "ik-3", "h-3", "{}", "SUCCEEDED", _NOW, _NOW),
        ],
    )
    connection.executemany(
        """
        INSERT INTO generation_tasks (
            id, batch_id, provider, model, status, archive_status,
            superseded_by_task_id, created_at, updated_at
        ) VALUES (?, ?, 'metaso', 'MiniMax-H3', ?, ?, ?, ?, ?)
        """,
        [
            ("t-today", "b-1", "SUCCEEDED", "NONE", None, _NOW, _NOW),
            ("t-old", "b-2", "SUCCEEDED", "NONE", None, _DAYS_AGO, _DAYS_AGO),
            ("t-run", "b-1", "RUNNING", "NONE", None, _NOW, _NOW),
            ("t-queued", "b-1", "QUEUED", "NONE", None, _NOW, _NOW),
            ("t-failed", "b-1", "FAILED", "NONE", None, _NOW, _NOW),
            ("t-uncertain", "b-1", "SUBMISSION_UNCERTAIN", "NONE", None, _NOW, _NOW),
            ("t-archive", "b-1", "SUCCEEDED", "ARCHIVE_FAILED", None, _NOW, _NOW),
            ("t-superseded", "b-2", "SUCCEEDED", "NONE", "t-current", _DAYS_AGO, _DAYS_AGO),
            ("t-current", "b-2", "SUCCEEDED", "NONE", None, _NOW, _NOW),
            ("t-hidden", "b-hidden", "SUCCEEDED", "NONE", None, _NOW, _NOW),
        ],
    )
    connection.execute(
        "INSERT INTO customer_batch_visibility (user_id, batch_id) VALUES (?, ?)",
        ("employee_1", "b-hidden"),
    )
    connection.commit()


def stats_connection(tmp_path: Path, name: str) -> BusinessConnection:
    connection = initialize_database(tmp_path / name)
    seed_stats_scene(connection)
    return BusinessConnection.sqlite(connection)


def actor(user_id: str, role: str) -> CurrentUser:
    return CurrentUser(id=user_id, username=user_id, display_name=user_id, role=role)  # type: ignore[arg-type]


def test_beijing_day_cutoff_converts_utc_to_beijing_day_start() -> None:
    # 2026-09-06 20:00 UTC = 2026-09-07 04:00 北京时间 → 当日零点对应的 UTC。
    now = datetime(2026, 9, 6, 20, 0, tzinfo=UTC)
    assert utc_cutoff_for_beijing_day(now) == "2026-09-06 16:00:00"


def test_admin_sees_workspace_wide_counters(tmp_path: Path) -> None:
    conn = stats_connection(tmp_path, "stats-admin.db")

    # 固定"当前时间"到种子日（2026-09-06）的北京时间傍晚，测试不随真实时钟跨天而漂移。
    stats = studio_task_stats(
        conn,
        actor=actor("admin_1", "admin"),
        now=datetime(2026, 9, 6, 12, 0, tzinfo=UTC),
    )

    assert stats == StudioStatsResponse(
        # t-today / t-archive / t-current / t-hidden completed today（t-superseded 已被替代不计）
        today_completed=4,
        running=1,
        queued=1,
        # t-failed + t-uncertain + t-archive(ARCHIVE_FAILED)
        needs_attention=3,
        # t-today / t-old / t-archive / t-current / t-hidden
        total_completed=5,
    )


def test_employee_counters_scope_to_own_projects_and_respect_hiding(tmp_path: Path) -> None:
    conn = stats_connection(tmp_path, "stats-employee.db")

    stats = studio_task_stats(
        conn,
        actor=actor("employee_1", "employee"),
        now=datetime(2026, 9, 6, 12, 0, tzinfo=UTC),
    )

    # employee_1 只看 p-1（p-2 属于 employee_2），且 b-hidden 被本人隐藏。
    assert stats == StudioStatsResponse(
        today_completed=2,
        running=1,
        queued=1,
        needs_attention=3,
        total_completed=2,
    )


def test_today_boundary_follows_beijing_day(tmp_path: Path) -> None:
    connection = initialize_database(tmp_path / "stats-boundary.db")
    connection.executemany(
        "INSERT INTO users (id, username, display_name, role) VALUES (?, ?, ?, ?)",
        [("admin_1", "admin_1", "Admin One", "admin")],
    )
    connection.execute(
        "INSERT INTO projects (id, owner_user_id, name) VALUES ('p-1', 'admin_1', 'P')"
    )
    connection.execute(
        """
        INSERT INTO generation_batches (
            id, project_id, created_by_user_id, idempotency_key, request_hash,
            request_snapshot_json, status, created_at, updated_at
        ) VALUES ('b-1', 'p-1', 'admin_1', 'ik', 'h', '{}', 'SUCCEEDED', ?, ?)
        """,
        ("2026-09-05 12:00:00", "2026-09-05 12:00:00"),
    )
    # 北京时间 2026-09-05 23:59 完成 = UTC 2026-09-05 15:59 → 属于前一天。
    # 北京时间 2026-09-06 00:01 完成 = UTC 2026-09-05 16:01 → 属于当日。
    connection.executemany(
        """
        INSERT INTO generation_tasks (
            id, batch_id, provider, model, status, created_at, updated_at
        ) VALUES (?, ?, 'metaso', 'MiniMax-H3', 'SUCCEEDED', '2026-09-05 12:00:00', ?)
        """,
        [
            ("t-before", "b-1", "2026-09-05T15:59:00+00:00"),
            ("t-after", "b-1", "2026-09-05T09:01:00-07:00"),
        ],
    )
    connection.commit()
    conn = BusinessConnection.sqlite(connection)

    stats = studio_task_stats(
        conn,
        actor=actor("admin_1", "admin"),
        now=datetime(2026, 9, 6, 10, 0, tzinfo=UTC),
    )
    assert stats.today_completed == 1
    assert stats.total_completed == 2
