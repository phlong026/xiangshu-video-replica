"""Studio data-dashboard aggregates (C6: GET /api/studio/analytics).

数据看板 C6 切片：在 /api/studio/stats 的可见性口径之上，给出窗口内的
每日成片桶（北京日界）、任务类型分布与最近成片清单。播放/互动等外部
平台数据不在其中——不伪造。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from app import studio_routes
from app.auth import CurrentUser, get_database
from app.db import connect_database, initialize_database
from app.db_portable import BusinessConnection
from app.main import app
from app.studio_routes import studio_analytics, studio_task_stats

# 2026-09-06 12:00 UTC = 2026-09-06 20:00 北京；固定"当前时间"防跨天漂移。
_NOW = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)

# 北京日界两侧的完成时刻（UTC 文本，与 CURRENT_TIMESTAMP 写入口径一致）：
#   2026-09-05 15:59 UTC = 09-05 23:59 北京 → 属于 09-05
#   2026-09-05 16:01 UTC = 09-06 00:01 北京 → 属于 09-06
_TODAY = "2026-09-06 03:00:00"
_YESTERDAY_BEIJING = "2026-09-05T15:59:00+00:00"
_TODAY_BEIJING_EARLY = "2026-09-05T09:01:00-07:00"
_INDEPENDENT_COMPLETED = "2026-09-06 02:00:00"
_OUT_OF_RANGE = "2026-08-30 00:00:00"


def seed_analytics_scene(connection) -> None:
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
        [
            ("p-1", "employee_1", "庭院项目"),
            ("p-2", "employee_2", "邻家项目"),
        ],
    )
    connection.executemany(
        """
        INSERT INTO generation_batches (
            id, project_id, created_by_user_id, idempotency_key, request_hash,
            request_snapshot_json, status, creation_kind, display_name, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                "b-1",
                "p-1",
                "employee_1",
                "ik-1",
                "h-1",
                "{}",
                "SUCCEEDED",
                "replica",
                None,
                _TODAY,
                _TODAY,
            ),
            (
                "b-2",
                "p-1",
                "employee_1",
                "ik-2",
                "h-2",
                "{}",
                "SUCCEEDED",
                "independent",
                None,
                _TODAY,
                _TODAY,
            ),
            (
                "b-3",
                "p-2",
                "employee_2",
                "ik-3",
                "h-3",
                "{}",
                "SUCCEEDED",
                "replacement",
                None,
                _TODAY,
                _TODAY,
            ),
            (
                "b-hidden",
                "p-1",
                "employee_1",
                "ik-4",
                "h-4",
                "{}",
                "SUCCEEDED",
                "replica",
                None,
                _TODAY,
                _TODAY,
            ),
            (
                "b-independent",
                None,
                "employee_1",
                "ik-independent",
                "h-independent",
                "{}",
                "SUCCEEDED",
                "independent",
                "无项目独立创作",
                _INDEPENDENT_COMPLETED,
                _INDEPENDENT_COMPLETED,
            ),
        ],
    )
    connection.executemany(
        """
        INSERT INTO generation_tasks (
            id, batch_id, provider, model, status, archive_status,
            superseded_by_task_id, created_at, updated_at
        ) VALUES (?, ?, 'metaso', 'MiniMax-H3', ?, 'NONE', ?, ?, ?)
        """,
        [
            ("t1", "b-1", "SUCCEEDED", None, _TODAY, _TODAY),
            ("t2", "b-2", "SUCCEEDED", None, _TODAY, _TODAY_BEIJING_EARLY),
            ("t3", "b-1", "SUCCEEDED", None, _TODAY, _YESTERDAY_BEIJING),
            ("t4", "b-3", "SUCCEEDED", None, _TODAY, _TODAY),
            ("t5-failed", "b-1", "FAILED", None, _TODAY, _TODAY),
            ("t6-superseded", "b-1", "SUCCEEDED", "t1", _TODAY, _TODAY),
            ("t7-hidden", "b-hidden", "SUCCEEDED", None, _TODAY, _TODAY),
            ("t8-running", "b-1", "RUNNING", None, _TODAY, _TODAY),
            ("t9-old", "b-2", "SUCCEEDED", None, _OUT_OF_RANGE, _OUT_OF_RANGE),
            ("t10-submitting", "b-1", "SUBMITTING", None, _TODAY, _TODAY),
            ("t11-archiving", "b-1", "ARCHIVING", None, _TODAY, _TODAY),
            (
                "t-independent",
                "b-independent",
                "SUCCEEDED",
                None,
                _INDEPENDENT_COMPLETED,
                _INDEPENDENT_COMPLETED,
            ),
        ],
    )
    connection.execute(
        "INSERT INTO customer_batch_visibility (user_id, batch_id) VALUES (?, ?)",
        ("employee_1", "b-hidden"),
    )
    connection.execute(
        """
        INSERT INTO person_identities (id, owner_user_id, display_name, status)
        VALUES ('analytics-identity', 'employee_1', '口播人物', 'ACTIVE')
        """
    )
    connection.execute(
        """
        INSERT INTO oral_avatars (
            id, identity_id, owner_user_id, title, status, source_kind, source_asset_id
        ) VALUES (
            'analytics-avatar', 'analytics-identity', 'employee_1', '口播分身',
            'READY', 'IMAGE', 'analytics-source'
        )
        """
    )
    connection.executemany(
        """
        INSERT INTO oral_tasks (
            id, owner_user_id, identity_id, avatar_id, mode, title, status,
            estimated_cost_fen, idempotency_key, request_hash, submission_state,
            provider_charge_state, created_at, updated_at
        ) VALUES (?, ?, 'analytics-identity', 'analytics-avatar', 'TTS', ?, ?, 350,
                  ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                "oral-succeeded",
                "employee_1",
                "院落介绍口播",
                "SUCCEEDED",
                "oral-key-success",
                "oral-hash-success",
                "SUBMITTED",
                "CHARGED",
                _TODAY,
                _TODAY_BEIJING_EARLY,
            ),
            (
                "oral-failed",
                "employee_1",
                "失败口播",
                "FAILED",
                "oral-key-failed",
                "oral-hash-failed",
                "FAILED",
                "NOT_CHARGED",
                _TODAY,
                _TODAY,
            ),
            (
                "oral-queued",
                "employee_1",
                "排队口播",
                "QUEUED",
                "oral-key-queued",
                "oral-hash-queued",
                "LOCAL_PENDING",
                "NOT_SUBMITTED",
                _TODAY,
                _TODAY,
            ),
            (
                "oral-running",
                "employee_1",
                "处理中口播",
                "RUNNING",
                "oral-key-running",
                "oral-hash-running",
                "SUBMITTED",
                "CHARGED",
                _TODAY,
                _TODAY,
            ),
        ],
    )
    # 按秒计费账本（057 形状约束）：t1 预留 8 秒并成功结算 → 消耗 8 积分；
    # 其余成片无计费记录 → cost_credits 为 null（不伪造）。
    connection.executemany(
        """
        INSERT INTO wallet_transactions (
            id, user_id, type, available_delta, reserved_delta,
            task_id, billing_round, idempotency_key
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            ("wt-reserve-t1", "employee_1", "RESERVE", -8, 8, "t1", 1, "reserve:t1:1"),
            ("wt-settle-t1", "employee_1", "SETTLE", 0, -8, "t1", 1, "settle:t1:1"),
        ],
    )
    connection.commit()


def analytics_connection(tmp_path: Path, name: str) -> BusinessConnection:
    connection = initialize_database(tmp_path / name)
    seed_analytics_scene(connection)
    return BusinessConnection.sqlite(connection)


def actor(user_id: str, role: str) -> CurrentUser:
    return CurrentUser(id=user_id, username=user_id, display_name=user_id, role=role)  # type: ignore[arg-type]


def test_daily_series_covers_full_window_by_beijing_day(tmp_path: Path) -> None:
    conn = analytics_connection(tmp_path, "analytics-admin.db")

    result = studio_analytics(conn, actor=actor("admin_1", "admin"), now=_NOW, days=7)

    assert result.range_days == 7
    assert [day.day for day in result.daily] == [
        "2026-08-31",
        "2026-09-01",
        "2026-09-02",
        "2026-09-03",
        "2026-09-04",
        "2026-09-05",
        "2026-09-06",
    ]
    # 09-05：t3（北京 23:59）；09-06：5 个普通成片 + 1 个成功口播；
    # 失败桶为 t5-failed + oral-failed。
    by_day = {day.day: day for day in result.daily}
    assert by_day["2026-09-05"].completed == 1
    assert by_day["2026-09-05"].failed == 0
    assert by_day["2026-09-06"].completed == 6
    assert by_day["2026-09-06"].failed == 2
    assert result.range_completed == 7
    assert result.today_completed == by_day["2026-09-06"].completed
    assert result.range_completed == sum(day.completed for day in result.daily)
    assert result.range_generation_outputs == 6
    assert result.range_oral_outputs == 1
    assert result.range_generation_batches == 5
    assert result.total_generation_batches == 5
    # t3 完成于北京 09-05，不计入"今日"；t9（08-30）计入全期累计。
    assert result.today_completed == 6
    assert result.total_completed == 8


def test_analytics_uses_one_clock_read_across_beijing_midnight(tmp_path: Path, monkeypatch) -> None:
    connection = initialize_database(tmp_path / "analytics-midnight-clock.db")
    connection.execute(
        "INSERT INTO users (id, username, display_name, role) VALUES (?, ?, ?, ?)",
        ("admin_1", "admin_1", "Admin One", "admin"),
    )
    connection.execute(
        "INSERT INTO projects (id, owner_user_id, name) VALUES ('p-1', 'admin_1', 'P')"
    )
    connection.execute(
        """
        INSERT INTO generation_batches (
            id, project_id, created_by_user_id, idempotency_key, request_hash,
            request_snapshot_json, status, creation_kind, created_at, updated_at
        ) VALUES ('b-1', 'p-1', 'admin_1', 'ik', 'h', '{}', 'SUCCEEDED',
                  'replica', ?, ?)
        """,
        ("2026-09-05 15:59:59", "2026-09-05 15:59:59"),
    )
    connection.execute(
        """
        INSERT INTO generation_tasks (
            id, batch_id, provider, model, status, archive_status, created_at, updated_at
        ) VALUES ('t-before', 'b-1', 'metaso', 'MiniMax-H3', 'SUCCEEDED', 'NONE',
                  '2026-09-05 15:59:59', '2026-09-05 15:59:59')
        """
    )
    connection.commit()

    clock_values = iter(
        [
            datetime(2026, 9, 5, 15, 59, 59, 999999, tzinfo=UTC),
            datetime(2026, 9, 5, 16, 0, 0, tzinfo=UTC),
        ]
    )

    class MidnightClock(datetime):
        @classmethod
        def now(cls, tz=None):
            value = next(clock_values)
            return value if tz is None else value.astimezone(tz)

    monkeypatch.setattr(studio_routes, "datetime", MidnightClock)
    result = studio_analytics(
        BusinessConnection.sqlite(connection), actor=actor("admin_1", "admin"), days=1
    )

    assert result.today_completed == 1
    assert result.range_completed == 1
    assert result.generated_at == "2026-09-05T15:59:59.999999+00:00"
    assert result.daily == [
        studio_routes.StudioAnalyticsDay(day="2026-09-05", completed=1, failed=0)
    ]
    assert next(clock_values) == datetime(2026, 9, 5, 16, 0, 0, tzinfo=UTC)


def test_kind_breakdown_counts_only_visible_completed_tasks(tmp_path: Path) -> None:
    conn = analytics_connection(tmp_path, "analytics-kinds.db")

    admin_view = studio_analytics(conn, actor=actor("admin_1", "admin"), now=_NOW, days=7)
    assert {(item.kind, item.completed) for item in admin_view.kind_breakdown} == {
        ("replica", 3),
        ("independent", 2),
        ("replacement", 1),
        ("oral", 1),
    }

    employee_view = studio_analytics(conn, actor=actor("employee_1", "employee"), now=_NOW, days=7)
    # employee_1 可见 p-1 与自己创建的无项目批次，且 b-hidden 被本人隐藏。
    assert {(item.kind, item.completed) for item in employee_view.kind_breakdown} == {
        ("replica", 2),
        ("independent", 2),
        ("oral", 1),
    }
    # 普通生成 b-1 有两个产出项，但批次只计一次；b-2 为另一个批次。
    assert employee_view.range_generation_batches == 3
    assert employee_view.total_generation_batches == 3
    assert employee_view.range_generation_outputs == 4
    assert employee_view.range_oral_outputs == 1
    assert employee_view.range_completed == 5
    assert employee_view.total_completed == 6

    customer_view = studio_analytics(conn, actor=actor("employee_1", "customer"), now=_NOW, days=7)
    assert customer_view == employee_view

    other_employee = studio_analytics(conn, actor=actor("employee_2", "employee"), now=_NOW, days=7)
    assert all(work.batch_id != "b-independent" for work in other_employee.recent_works)


def test_task_stats_combine_visible_generation_and_oral_statuses(tmp_path: Path) -> None:
    conn = analytics_connection(tmp_path, "analytics-stats.db")

    admin_stats = studio_task_stats(conn, actor=actor("admin_1", "admin"), now=_NOW)
    employee_stats = studio_task_stats(conn, actor=actor("employee_1", "employee"), now=_NOW)
    customer_stats = studio_task_stats(conn, actor=actor("employee_1", "customer"), now=_NOW)
    other_employee_stats = studio_task_stats(conn, actor=actor("employee_2", "employee"), now=_NOW)

    assert admin_stats.today_completed == 6
    assert admin_stats.total_completed == 8
    assert employee_stats.today_completed == 4
    assert employee_stats.running == 4
    assert employee_stats.queued == 1
    assert employee_stats.needs_attention == 2
    assert employee_stats.total_completed == 6
    assert customer_stats == employee_stats
    assert other_employee_stats.today_completed == 1
    assert other_employee_stats.total_completed == 1


def test_recent_works_sorted_scoped_and_capped(tmp_path: Path) -> None:
    conn = analytics_connection(tmp_path, "analytics-works.db")

    admin_view = studio_analytics(conn, actor=actor("admin_1", "admin"), now=_NOW, days=7)
    # 按 completed_at 倒序；同一时刻按 task_id 稳定排序。
    assert [work.task_id for work in admin_view.recent_works] == [
        "t1",
        "t4",
        "t7-hidden",
        "t-independent",
        "oral-succeeded",
        "t2",
        "t3",
    ]
    first = admin_view.recent_works[0]
    assert (first.title, first.creation_kind, first.project_id) == (
        "庭院项目",
        "replica",
        "p-1",
    )
    assert first.completed_at == _TODAY
    # t1 有按秒计费的 RESERVE 记录 → 消耗 8 积分；无计费记录的成片为 null。
    assert first.cost_credits == 8
    oral = next(work for work in admin_view.recent_works if work.task_id == "oral-succeeded")
    assert oral.task_kind == "oral"
    assert oral.batch_id is None
    assert oral.project_id is None
    assert oral.cost_credits is None
    independent = next(work for work in admin_view.recent_works if work.task_id == "t-independent")
    assert independent.project_id is None
    assert independent.batch_id == "b-independent"
    assert independent.title == "无项目独立创作"

    employee_view = studio_analytics(conn, actor=actor("employee_1", "employee"), now=_NOW, days=7)
    # 隐藏批次与他人的 p-2 任务都不出现。
    assert [work.task_id for work in employee_view.recent_works] == [
        "t1",
        "t-independent",
        "oral-succeeded",
        "t2",
        "t3",
    ]


def test_recent_works_cap_at_twenty(tmp_path: Path) -> None:
    connection = initialize_database(tmp_path / "analytics-cap.db")
    connection.execute(
        "INSERT INTO users (id, username, display_name, role) VALUES (?, ?, ?, ?)",
        ("admin_1", "admin_1", "Admin One", "admin"),
    )
    connection.execute(
        "INSERT INTO projects (id, owner_user_id, name) VALUES ('p-1', 'admin_1', 'P')"
    )
    connection.execute(
        """
        INSERT INTO generation_batches (
            id, project_id, created_by_user_id, idempotency_key, request_hash,
            request_snapshot_json, status, creation_kind, created_at, updated_at
        ) VALUES ('b-1', 'p-1', 'admin_1', 'ik', 'h', '{}', 'SUCCEEDED', 'replica', ?, ?)
        """,
        (_TODAY, _TODAY),
    )
    connection.executemany(
        """
        INSERT INTO generation_tasks (
            id, batch_id, provider, model, status, archive_status,
            created_at, updated_at
        ) VALUES (?, 'b-1', 'metaso', 'MiniMax-H3', 'SUCCEEDED', 'NONE', ?, ?)
        """,
        [(f"t-{index:02d}", _TODAY, f"2026-09-06 01:{index:02d}:00") for index in range(22)],
    )
    connection.commit()
    conn = BusinessConnection.sqlite(connection)

    result = studio_analytics(conn, actor=actor("admin_1", "admin"), now=_NOW, days=7)

    assert result.range_completed == 22
    assert result.range_generation_batches == 1
    assert len(result.recent_works) == 20
    assert result.recent_works[0].task_id == "t-21"


def test_days_window_is_clamped(tmp_path: Path) -> None:
    conn = analytics_connection(tmp_path, "analytics-clamp.db")

    one_day = studio_analytics(conn, actor=actor("admin_1", "admin"), now=_NOW, days=0)
    assert one_day.range_days == 1
    assert len(one_day.daily) == 1
    assert one_day.daily[0].day == "2026-09-06"

    ninety_days = studio_analytics(conn, actor=actor("admin_1", "admin"), now=_NOW, days=999)
    assert ninety_days.range_days == 90
    assert len(ninety_days.daily) == 90
    # 30 天窗口外的 t9（08-30）在 90 天窗口内计入。
    assert ninety_days.range_completed == 8


def test_analytics_route_scopes_by_caller(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "analytics-route.db"
    with initialize_database(db_path) as connection:
        seed_analytics_scene(connection)
    monkeypatch.setenv("VIDEO_REPLICA_DB_PATH", str(db_path))

    def database_override():
        conn = BusinessConnection.sqlite(connect_database(db_path))
        try:
            yield conn
        finally:
            conn.close()

    app.dependency_overrides[get_database] = database_override
    try:
        client = TestClient(app)
        response = client.get("/api/studio/analytics", headers={"X-Dev-User-Id": "employee_1"})
        assert response.status_code == 200
        payload = response.json()
        assert payload["range_days"] == 7
        assert len(payload["daily"]) == 7
        assert payload["range_completed"] == 5
        assert payload["range_generation_batches"] == 3
        assert "range_completed_outputs" not in payload
        daily = {item["day"]: item for item in payload["daily"]}
        assert daily["2026-09-06"]["failed"] == 2
        works = payload["recent_works"]
        assert [work["task_id"] for work in works] == [
            "t1",
            "t-independent",
            "oral-succeeded",
            "t2",
            "t3",
        ]
        assert works[0]["cost_credits"] == 8
        assert works[1]["cost_credits"] is None

        month = client.get("/api/studio/analytics?days=30", headers={"X-Dev-User-Id": "employee_1"})
        assert month.status_code == 200
        assert len(month.json()["daily"]) == 30

        invalid = client.get(
            "/api/studio/analytics?days=abc", headers={"X-Dev-User-Id": "employee_1"}
        )
        assert invalid.status_code == 422
    finally:
        app.dependency_overrides.clear()
