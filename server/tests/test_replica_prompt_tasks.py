"""长视频复刻提示词任务族（replica_prompt）——入队/租约/完成/失败/取消（TEST-PG lane）。

与 analysis_tasks 刻意分族的长任务链路：本文件按 test_analysis_task_cancel.py
的服务层先例，直接驱动 enqueue/acquire/prepare/perform/complete/fail/cancel，
用真实 PG 验证三件事——

1. 计费闭环：入队预扣（RESERVE）、成功结算（SETTLE）、失败/取消释放（RELEASE），
   且同资产活跃任务复用不重复计费；
2. 租约语义：认领串行、过期 RUNNING 翻转 INTERRUPTED、迟到完成被 CAS 拦截；
3. 结果发布：任务绑定精确版本，prompt / storyboard 分别落对应 kind 与载荷。

The database is the only real dependency: a dedicated TEST-PG database created
through the CW-007 kit, migrated to alembic ``head`` and TRUNCATEd per test.
"""

from __future__ import annotations

import os
from collections.abc import Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Barrier
from typing import Any

# Set the audit HMAC key before importing app modules (audit writers require it).
os.environ.setdefault(
    "VIDEO_REPLICA_ADMIN_SESSION_HMAC_KEY",
    "test-key-for-replica-prompt-task-tests-minimum-48-bytes-long-1234",
)

import psycopg
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pg_test_kit import (
    create_test_database,
    drop_test_database,
    require_pg_or_explicit_skip,
    upgrade_test_database_to_head,
)

from app.analysis import FakeGemini
from app.auth import CurrentUser, Role, get_current_user_short_tx
from app.customer_fence import get_business_db
from app.db_pg import DATABASE_URL_ENV, close_pg_pool, pg_transaction
from app.db_portable import BusinessConnection
from app.replica_prompt import (
    ERROR_CANCELLED,
    ERROR_INTERRUPTED,
    acquire_replica_prompt_task,
    assert_replica_prompt_enqueue_allowed,
    cancel_replica_prompt_task,
    complete_replica_prompt_task,
    enqueue_replica_prompt_task,
    fail_replica_prompt_task,
    perform_replica_prompt_task,
    prepare_replica_prompt_task,
)
from app.storage import FakeStorageAdapter
from app.usage_billing import accept_operation, reconcile_operations

RP_DB_NAME = "replica_prompt_test"

_TABLES = (
    "replica_prompt_tasks, analysis_tasks, versions, billing_operations, "
    "wallet_transactions, billing_credit_lots, billing_tariffs, assets, projects, "
    "wallets, audit_logs, users"
)

_ASSET_METADATA = (
    '{"duration_seconds": 120.0, "resolution": "1920x1080", "fps": 30.0, "aspect_ratio": "16:9"}'
)


def _owner() -> CurrentUser:
    return CurrentUser(id="owner-1", username="owner_1", display_name="Owner", role="employee")


@pytest.fixture(scope="module")
def rp_dsn() -> Iterator[str]:
    require_pg_or_explicit_skip()
    dsn = create_test_database(RP_DB_NAME)
    upgrade_test_database_to_head(dsn)
    try:
        yield dsn
    finally:
        drop_test_database(RP_DB_NAME)


@pytest.fixture()
def rp_state(rp_dsn: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    close_pg_pool()
    monkeypatch.setenv(DATABASE_URL_ENV, rp_dsn)
    monkeypatch.delenv("VIDEO_REPLICA_CUSTOMER_PRODUCTION", raising=False)

    def unexpected_model_http(*_args: object, **_kwargs: object) -> None:
        pytest.fail("长视频 PG 测试必须使用假模型或已保存版本，禁止模型 HTTP 外呼。")

    monkeypatch.setattr("app.analysis.UrllibApilioChatTransport.post", unexpected_model_http)
    yield rp_dsn
    close_pg_pool()


def _seed_asset(
    dsn: str,
    *,
    wallet_credits: int = 10,
    duration_seconds: float = 120.0,
    task_status: str | None = None,
) -> None:
    """素材 + 项目 + 钱包 + 价目；``task_status`` 非空时再种一条对应状态的任务."""
    with psycopg.connect(dsn, autocommit=True) as pg:
        pg.execute("SET session_replication_role = replica")
        pg.execute(f"TRUNCATE {_TABLES} CASCADE")
        pg.execute("SET session_replication_role = DEFAULT")
        pg.execute(
            "INSERT INTO billing_tariffs(service, enabled, unit_credits) "
            "VALUES ('analysis', true, 1)"
        )
        pg.execute(
            "INSERT INTO users (id, username, display_name, role) VALUES "
            "('owner-1', 'owner_1', 'Owner', 'employee')"
        )
        pg.execute(
            "INSERT INTO wallets (user_id, available_credits, reserved_credits) "
            "VALUES ('owner-1', %s, 0)",
            (wallet_credits,),
        )
        pg.execute(
            "INSERT INTO projects (id, name, owner_user_id) VALUES "
            "('proj-1', 'Long Replica Project', 'owner-1')"
        )
        pg.execute(
            "INSERT INTO assets (id, project_id, kind, storage_uri, sha256, "
            " size_bytes, content_type, created_by_user_id, metadata_json) VALUES "
            "('asset-1', 'proj-1', 'reference_video', 'cos://bucket/proj-1/a.mp4', "
            " repeat('a', 64), 1024, 'video/mp4', 'owner-1', %s)",
            (f'{{"duration_seconds": {duration_seconds}}}',),
        )
        if task_status == "RUNNING":
            pg.execute(
                "INSERT INTO replica_prompt_tasks ("
                " id, project_id, asset_id, created_by_user_id, duration_seconds,"
                " status, attempt, locked_by, locked_until, started_at"
                ") VALUES ("
                " 'task-1', 'proj-1', 'asset-1', 'owner-1', 120,"
                " 'RUNNING', 1, 'worker-a', now() + interval '25 minutes', now())"
            )
        elif task_status == "PENDING":
            pg.execute(
                "INSERT INTO replica_prompt_tasks ("
                " id, project_id, asset_id, created_by_user_id, duration_seconds, status"
                ") VALUES ("
                " 'task-1', 'proj-1', 'asset-1', 'owner-1', 120, 'PENDING')"
            )


def _enqueue(
    dsn: str, *, duration_seconds: float = 120.0, mode: str = "prompt"
) -> tuple[dict[str, Any], bool]:
    with pg_transaction() as raw:
        row, created = enqueue_replica_prompt_task(
            BusinessConnection.postgres(raw),
            project_id="proj-1",
            asset_id="asset-1",
            created_by_user_id="owner-1",
            duration_seconds=duration_seconds,
            mode=mode,
        )
    return dict(row), created


def _task_row(dsn: str, task_id: str = "task-1") -> dict[str, Any] | None:
    with pg_transaction() as raw:
        row = (
            BusinessConnection.postgres(raw)
            .execute("SELECT * FROM replica_prompt_tasks WHERE id = %s", (task_id,))
            .fetchone()
        )
    return None if row is None else dict(row)


def _wallet(dsn: str) -> tuple[int, int]:
    with pg_transaction() as raw:
        row = (
            BusinessConnection.postgres(raw)
            .execute(
                "SELECT available_credits, reserved_credits FROM wallets WHERE user_id = 'owner-1'"
            )
            .fetchone()
        )
    assert row is not None
    return int(row["available_credits"]), int(row["reserved_credits"])


def _ledger(dsn: str, task_id: str = "task-1") -> list[str]:
    with pg_transaction() as raw:
        rows = (
            BusinessConnection.postgres(raw)
            .execute(
                "SELECT type FROM wallet_transactions WHERE billing_operation_id = "
                "(SELECT id FROM billing_operations WHERE source_id = %s "
                " AND service = 'analysis') ORDER BY ledger_sequence",
                (task_id,),
            )
            .fetchall()
        )
    return [str(row["type"]) for row in rows]


def _version_payload(dsn: str, *, kind: str = "replica_prompt") -> dict[str, Any] | None:
    with pg_transaction() as raw:
        row = (
            BusinessConnection.postgres(raw)
            .execute(
                "SELECT kind, payload_json FROM versions "
                "WHERE project_id = 'proj-1' ORDER BY version_number DESC LIMIT 1"
            )
            .fetchone()
        )
    if row is None:
        return None
    import json

    assert str(row["kind"]) == kind
    return dict(json.loads(str(row["payload_json"])))


@pytest.mark.parametrize("mode", ["prompt", "storyboard"])
def test_enqueue_reserves_once_and_reuses_active_task(rp_state: str, mode: str) -> None:
    """同资产已有活跃任务时复用：不新插入、不重复预扣."""
    _seed_asset(rp_state)
    row, created = _enqueue(rp_state, mode=mode)
    assert created is True
    assert row["status"] == "PENDING"
    assert _wallet(rp_state) == (9, 1)  # 预扣 1 积分

    again, created_again = _enqueue(rp_state, mode=mode)
    assert created_again is False
    assert again["id"] == row["id"]
    assert _wallet(rp_state) == (9, 1)


def test_acquire_claims_pending_and_expires_stale_running(rp_state: str) -> None:
    _seed_asset(rp_state)
    row, _ = _enqueue(rp_state)

    with pg_transaction() as raw:
        lease = acquire_replica_prompt_task(BusinessConnection.postgres(raw), worker_id="worker-a")
    assert lease is not None
    assert lease.id == row["id"]
    assert lease.attempt == 1
    assert lease.worker_id == "worker-a"
    task = _task_row(rp_state, lease.id)
    assert task is not None and task["status"] == "RUNNING"

    # 没有其他 PENDING：第二次认领拿不到任务。
    with pg_transaction() as raw:
        assert (
            acquire_replica_prompt_task(BusinessConnection.postgres(raw), worker_id="worker-b")
            is None
        )

    # 把租约拨到过期：下一次认领先把 RUNNING 翻转成 INTERRUPTED 可重试失败。
    with pg_transaction() as raw:
        BusinessConnection.postgres(raw).execute(
            "UPDATE replica_prompt_tasks SET locked_until = now() - interval '1 second' "
            "WHERE id = %s",
            (lease.id,),
        )
    with pg_transaction() as raw:
        assert (
            acquire_replica_prompt_task(BusinessConnection.postgres(raw), worker_id="worker-b")
            is None
        )
    task = _task_row(rp_state, lease.id)
    assert task is not None
    assert task["status"] == "FAILED"
    assert task["error_code"] == ERROR_INTERRUPTED
    assert bool(task["retryable"]) is True


@pytest.mark.parametrize(
    ("mode", "kind", "schema"),
    [
        ("prompt", "replica_prompt", "replica-prompt.v1"),
        ("storyboard", "long_analysis", "long-analysis.v1"),
    ],
)
def test_prepare_perform_complete_publishes_version_and_settles(
    rp_state: str, mode: str, kind: str, schema: str
) -> None:
    """FakeGemini 两种模式全链路：执行解析 → 发布精确结果版本 → 单次结算."""
    _seed_asset(rp_state, task_status="PENDING")
    with pg_transaction() as raw:
        raw.execute("UPDATE replica_prompt_tasks SET mode = %s WHERE id = 'task-1'", (mode,))
        accept_operation(
            BusinessConnection.postgres(raw),
            user_id="owner-1",
            service="analysis",
            source_id="task-1",
            units=1,
        )
    with pg_transaction() as raw:
        lease = acquire_replica_prompt_task(BusinessConnection.postgres(raw), worker_id="worker-a")
    assert lease is not None
    assert lease.mode == mode

    with pg_transaction() as raw:
        work = prepare_replica_prompt_task(
            BusinessConnection.postgres(raw),
            lease=lease,
            storage=FakeStorageAdapter(provider="fake", bucket="test-bucket"),
            provider=FakeGemini(),
        )
    # FakeGemini 不要求 HTTPS：video_uri 保留 asset URI；媒体信息带服务端实测时长。
    assert work.video_uri.startswith("cos://")
    assert work.media_info["duration_seconds"] == 120.0
    assert work.mode == mode

    payload = perform_replica_prompt_task(work)
    assert payload["schema_version"] == schema
    if mode == "storyboard":
        assert payload["shots"]
    assert payload["replica_prompt"].strip()
    assert set(payload["summary"]) == {
        "theme",
        "visual_style",
        "pace",
        "camera_language",
        "color_tone",
    }

    with pg_transaction() as raw:
        complete_replica_prompt_task(BusinessConnection.postgres(raw), work=work, payload=payload)
    task = _task_row(rp_state, lease.id)
    assert task is not None
    assert task["status"] == "SUCCEEDED"
    assert task["result_version_id"] is not None
    with psycopg.connect(rp_state) as pg:
        assert pg.execute(
            "SELECT kind, asset_id FROM versions WHERE id = %s", (task["result_version_id"],)
        ).fetchone() == (kind, "asset-1")
    published = _version_payload(rp_state, kind=kind)
    assert published is not None
    assert published["replica_prompt"] == payload["replica_prompt"]
    assert published["source_asset"]["id"] == "asset-1"
    # 钱包：预扣 1 → 成功结算扣 1（RESERVE → SETTLE）。
    assert _wallet(rp_state) == (9, 0)
    assert _ledger(rp_state) == ["RESERVE", "SETTLE"]


def test_fail_maps_provider_error_and_releases(rp_state: str) -> None:
    from app.analysis import AnalysisProviderFailed

    _seed_asset(rp_state, task_status="PENDING")
    with pg_transaction() as raw:
        accept_operation(
            BusinessConnection.postgres(raw),
            user_id="owner-1",
            service="analysis",
            source_id="task-1",
            units=1,
        )
    with pg_transaction() as raw:
        lease = acquire_replica_prompt_task(BusinessConnection.postgres(raw), worker_id="worker-a")
    assert lease is not None
    with pg_transaction() as raw:
        fail_replica_prompt_task(
            BusinessConnection.postgres(raw),
            lease=lease,
            cause=AnalysisProviderFailed("上游拒绝", http_status=502, retryable=True),
        )
    task = _task_row(rp_state, lease.id)
    assert task is not None
    assert task["status"] == "FAILED"
    assert bool(task["retryable"]) is True
    assert _wallet(rp_state) == (10, 0)  # 预扣全额释放
    assert _ledger(rp_state) == ["RESERVE", "RELEASE"]


def test_cancel_releases_and_blocks_late_completion(rp_state: str) -> None:
    """RUNNING 取消后，worker 晚到的完成被 status+locked_by CAS 拦截."""
    _seed_asset(rp_state, task_status="RUNNING")
    with pg_transaction() as raw:
        accept_operation(
            BusinessConnection.postgres(raw),
            user_id="owner-1",
            service="analysis",
            source_id="task-1",
            units=1,
        )

        from app.replica_prompt import ReplicaPromptTaskLease

        lease = ReplicaPromptTaskLease(
            id="task-1",
            project_id="proj-1",
            asset_id="asset-1",
            created_by_user_id="owner-1",
            duration_seconds=120.0,
            worker_id="worker-a",
            attempt=1,
        )
        row = cancel_replica_prompt_task(
            BusinessConnection.postgres(raw), task_id="task-1", actor=_owner()
        )
    assert str(row["status"]) == "FAILED"
    assert str(row["error_code"]) == ERROR_CANCELLED
    assert _wallet(rp_state) == (10, 0)

    # 晚到的 worker 结果：任务已 FAILED，prepare 按租约核验拒绝；
    # 直接构造 work 调 complete，验证 CAS 前提（RUNNING + locked_by）已破坏、结果被丢弃。
    from app.replica_prompt import ReplicaPromptTaskWork

    work = ReplicaPromptTaskWork(
        lease=lease,
        provider=FakeGemini(),
        video_uri="cos://bucket/proj-1/a.mp4",
        asset_uri="cos://bucket/proj-1/a.mp4",
        media_info={"duration_seconds": 120.0},
        storage=None,
    )
    payload = perform_replica_prompt_task(work)
    with pg_transaction() as raw:
        complete_replica_prompt_task(BusinessConnection.postgres(raw), work=work, payload=payload)
    task = _task_row(rp_state)
    assert task is not None
    assert task["status"] == "FAILED"  # 取消终态未被覆盖
    assert task["result_version_id"] is None
    assert _version_payload(rp_state) is None  # 结果未发布
    assert _wallet(rp_state) == (10, 0)
    assert _ledger(rp_state) == ["RESERVE", "RELEASE"]

    # 对账重扫不改变任何事实。
    with pg_transaction() as raw:
        reconcile_operations(BusinessConnection.postgres(raw))
    assert _ledger(rp_state) == ["RESERVE", "RELEASE"]


def test_enqueue_guard_rejects_over_limit_and_non_video(rp_state: str) -> None:
    _seed_asset(rp_state)
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        asset = conn.execute("SELECT * FROM assets WHERE id = 'asset-1'").fetchone()
        assert asset is not None
        # 180.05 在容差内放行；180.5 拒绝。
        assert_replica_prompt_enqueue_allowed(asset, measured_duration=180.05)
        with pytest.raises(HTTPException) as failure:
            assert_replica_prompt_enqueue_allowed(asset, measured_duration=180.5)
        assert failure.value.detail["code"] == "REPLICA_PROMPT_DURATION_EXCEEDED"


@pytest.fixture()
def history_api(rp_state: str) -> Iterator[TestClient]:
    from app.main import app

    _seed_asset(rp_state)
    with psycopg.connect(rp_state, autocommit=True) as pg:
        pg.execute("UPDATE users SET role = 'customer' WHERE id = 'owner-1'")
        pg.execute(
            "INSERT INTO users (id, username, display_name, role, account_type, parent_user_id) "
            "VALUES ('other', 'other', '其他账号', 'customer', 'MASTER', NULL), "
            "('child', 'child', '子账号', 'customer', 'SUB', 'owner-1'), "
            "('admin', 'admin', '管理员', 'admin', 'MASTER', NULL), "
            "('auditor', 'auditor', '审计员', 'auditor', 'MASTER', NULL)"
        )
        pg.execute(
            "INSERT INTO projects (id, name, owner_user_id) VALUES "
            "('proj-2', '我的第二个项目', 'owner-1'), "
            "('proj-other', '其他账号项目', 'other'), "
            "('proj-child', '子账号项目', 'child')"
        )
        pg.execute(
            "INSERT INTO versions "
            "(id, project_id, kind, version_number, payload_json, created_at) VALUES "
            "('own-a', 'proj-1', 'replica_prompt', 1, '{}', '2026-11-20T10:00:00Z'), "
            "('own-b', 'proj-2', 'long_analysis', 1, '{}', '2026-11-20T10:00:00Z'), "
            "('foreign', 'proj-other', 'long_analysis', 1, '{}', '2026-11-22T10:00:00Z'), "
            "('child-version', 'proj-child', 'long_analysis', 1, '{}', '2026-11-21T10:00:00Z'), "
            "('other-kind', 'proj-1', 'analysis', 1, '{}', '2026-11-23T10:00:00Z')"
        )
    app.dependency_overrides[get_current_user_short_tx] = lambda: CurrentUser(
        id="owner-1", username="owner_1", display_name="Owner", role="customer"
    )
    client = TestClient(app)
    try:
        yield client
    finally:
        client.close()
        app.dependency_overrides.clear()


@pytest.mark.parametrize(
    ("user_id", "role", "expected_ids"),
    [
        ("owner-1", "customer", ["own-b", "own-a"]),
        ("child", "customer", ["child-version"]),
        ("other", "customer", ["foreign"]),
        ("admin", "admin", ["foreign", "child-version", "own-b", "own-a"]),
        ("auditor", "auditor", ["foreign", "child-version", "own-b", "own-a"]),
    ],
)
def test_account_replica_prompt_history_uses_project_ownership(
    rp_state: str, history_api: TestClient, user_id: str, role: Role, expected_ids: list[str]
) -> None:
    from app.main import app

    app.dependency_overrides[get_current_user_short_tx] = lambda: CurrentUser(
        id=user_id, username=user_id, display_name=user_id, role=role
    )
    # 只存在持久化版本，没有当前任务或内存 projectId 也应能发现全部自有项目。
    with psycopg.connect(rp_state) as pg:
        assert pg.execute("SELECT count(*) FROM replica_prompt_tasks").fetchone() == (0,)
    response = history_api.get("/api/replica-prompt/versions")
    assert response.status_code == 200
    items = response.json()["items"]
    assert [item["id"] for item in items] == expected_ids
    assert all("payload" not in item for item in items)
    for version_id in ("own-a", "own-b", "child-version", "foreign"):
        detail = history_api.get(f"/api/replica-prompt-versions/{version_id}")
        if version_id in expected_ids:
            assert detail.status_code == 200
            assert detail.json()["id"] == version_id
        else:
            assert detail.status_code == 404
            assert detail.json()["detail"]["code"] == "REPLICA_PROMPT_VERSION_NOT_FOUND"
    for version_id in ("other-kind", "missing"):
        assert history_api.get(f"/api/replica-prompt-versions/{version_id}").status_code == 404


def test_account_replica_prompt_history_has_bounded_limit_and_keeps_project_route(
    rp_state: str, history_api: TestClient
) -> None:
    project = history_api.get("/api/projects/proj-1/replica-prompt/versions")
    assert project.status_code == 200
    assert [item["id"] for item in project.json()["items"]] == ["own-a"]
    assert history_api.get("/api/projects/proj-other/replica-prompt/versions").status_code == 404
    with psycopg.connect(rp_state, autocommit=True) as pg, pg.cursor() as cursor:
        cursor.executemany(
            "INSERT INTO versions "
            "(id, project_id, kind, version_number, payload_json, created_at) "
            "VALUES (%s, 'proj-1', 'long_analysis', %s, '{}', '2026-11-24T10:00:00Z')",
            [(f"extra-{index:03d}", index + 1) for index in range(105)],
        )
    for query, expected_count in (("", 30), ("?limit=0", 1), ("?limit=1000", 100)):
        response = history_api.get(f"/api/replica-prompt/versions{query}")
        assert response.status_code == 200
        items = response.json()["items"]
        assert len(items) == expected_count
        assert items[0]["id"] == "extra-104"


@pytest.fixture()
def task_api(rp_state: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    from app.main import app

    _seed_asset(rp_state)

    class TaskDb:
        @contextmanager
        def write(self) -> Iterator[tuple[BusinessConnection, CurrentUser]]:
            with pg_transaction() as raw:
                yield BusinessConnection.postgres(raw), _owner()

    app.dependency_overrides[get_current_user_short_tx] = _owner
    app.dependency_overrides[get_business_db] = TaskDb
    monkeypatch.setattr(
        "app.replica_prompt_routes.get_video_analysis_provider", lambda _c: FakeGemini()
    )
    client = TestClient(app)
    try:
        yield client
    finally:
        client.close()
        app.dependency_overrides.clear()


def _seed_completed_mode(dsn: str, *, mode: str, created_at: str) -> str:
    task_id = f"complete-{mode}"
    version_id = f"version-{mode}"
    kind = "long_analysis" if mode == "storyboard" else "replica_prompt"
    with psycopg.connect(dsn, autocommit=True) as pg:
        pg.execute(
            "INSERT INTO versions "
            "(id, project_id, asset_id, kind, version_number, payload_json) "
            "VALUES (%s, 'proj-1', 'asset-1', %s, 1, '{}')",
            (version_id, kind),
        )
        pg.execute(
            "INSERT INTO replica_prompt_tasks "
            "(id, project_id, asset_id, created_by_user_id, duration_seconds, status, "
            "result_version_id, mode, created_at) "
            "VALUES (%s, 'proj-1', 'asset-1', 'owner-1', 120, 'SUCCEEDED', %s, %s, %s)",
            (task_id, version_id, mode, created_at),
        )
    return task_id


@pytest.mark.parametrize("mode", ["prompt", "storyboard"])
def test_mode_specific_create_does_not_reuse_completed_other_mode(
    rp_state: str, task_api: TestClient, mode: str
) -> None:
    other_mode = "prompt" if mode == "storyboard" else "storyboard"
    previous_id = _seed_completed_mode(rp_state, mode=other_mode, created_at="2026-11-20T10:00:00Z")
    request = {"asset_id": "asset-1", "reuse_existing": True, "mode": mode}
    response = task_api.post("/api/projects/proj-1/replica-prompt-tasks", json=request)
    assert response.status_code == 202
    task = response.json()
    assert task["id"] != previous_id
    assert task["status"] == "PENDING"
    row = _task_row(rp_state, task["id"])
    assert row is not None and row["mode"] == mode
    assert _wallet(rp_state) == (9, 1)
    assert _ledger(rp_state, task["id"]) == ["RESERVE"]
    repeated = task_api.post("/api/projects/proj-1/replica-prompt-tasks", json=request)
    assert repeated.status_code == 202
    assert repeated.json()["id"] == task["id"]
    assert _wallet(rp_state) == (9, 1)
    assert _ledger(rp_state, task["id"]) == ["RESERVE"]


def test_latest_and_reuse_choose_requested_mode(rp_state: str, task_api: TestClient) -> None:
    expected = {
        "prompt": _seed_completed_mode(rp_state, mode="prompt", created_at="2026-11-20T10:00:00Z"),
        "storyboard": _seed_completed_mode(
            rp_state, mode="storyboard", created_at="2026-11-21T10:00:00Z"
        ),
    }
    for mode, task_id in expected.items():
        latest = task_api.get(f"/api/projects/proj-1/replica-prompt-tasks/latest?mode={mode}")
        assert latest.status_code == 200
        assert latest.json()["id"] == task_id
        reused = task_api.post(
            "/api/projects/proj-1/replica-prompt-tasks",
            json={"asset_id": "asset-1", "reuse_existing": True, "mode": mode},
        )
        assert reused.status_code == 202
        assert reused.json()["id"] == task_id
    assert _wallet(rp_state) == (10, 0)
    with psycopg.connect(rp_state) as pg:
        assert pg.execute("SELECT count(*) FROM wallet_transactions").fetchone() == (0,)


@pytest.mark.parametrize("reuse_existing", [True, False])
@pytest.mark.parametrize("active_status", ["PENDING", "RUNNING"])
def test_other_mode_active_returns_conflict_without_reserve(
    rp_state: str, task_api: TestClient, reuse_existing: bool, active_status: str
) -> None:
    active, _created = _enqueue(rp_state, mode="prompt")
    with psycopg.connect(rp_state, autocommit=True) as pg:
        pg.execute(
            "UPDATE replica_prompt_tasks SET status = %s, locked_by = 'worker-a', "
            "locked_until = now() + interval '25 minutes' WHERE id = %s",
            (active_status, active["id"]),
        )
    response = task_api.post(
        "/api/projects/proj-1/replica-prompt-tasks",
        json={"asset_id": "asset-1", "reuse_existing": reuse_existing, "mode": "storyboard"},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "REPLICA_PROMPT_MODE_CONFLICT"
    assert _wallet(rp_state) == (9, 1)
    assert _ledger(rp_state, active["id"]) == ["RESERVE"]
    with psycopg.connect(rp_state) as pg:
        assert pg.execute("SELECT count(*) FROM replica_prompt_tasks").fetchone() == (1,)
        assert pg.execute("SELECT count(*) FROM wallet_transactions").fetchone() == (1,)


@pytest.mark.parametrize("second_mode", ["prompt", "storyboard"])
def test_concurrent_enqueue_respects_mode_and_reserves_once(
    rp_state: str, monkeypatch: pytest.MonkeyPatch, second_mode: str
) -> None:
    _seed_asset(rp_state)
    gate = Barrier(2)
    execute = BusinessConnection.execute

    def simultaneous_insert(
        conn: BusinessConnection, sql: str, params: Sequence[object] = ()
    ) -> Any:
        if "INSERT INTO replica_prompt_tasks" in sql:
            # 两个首查均已真实返回空，再同时 INSERT，强制走数据库唯一冲突回查。
            gate.wait(timeout=10)
        return execute(conn, sql, params)

    monkeypatch.setattr(BusinessConnection, "execute", simultaneous_insert)

    def submit(mode: str) -> tuple[dict[str, Any], bool] | HTTPException:
        try:
            return _enqueue(rp_state, mode=mode)
        except HTTPException as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(submit, mode) for mode in ("prompt", second_mode)]
        results = [future.result(timeout=20) for future in futures]
    successes = [result for result in results if not isinstance(result, HTTPException)]
    failures = [result for result in results if isinstance(result, HTTPException)]
    assert sum(created for _row, created in successes) == 1
    if second_mode == "prompt":
        assert not failures
        assert len(successes) == 2
        assert successes[0][0]["id"] == successes[1][0]["id"]
    else:
        assert len(successes) == 1
        assert len(failures) == 1
        assert failures[0].status_code == 409
        assert failures[0].detail["code"] == "REPLICA_PROMPT_MODE_CONFLICT"
    task_id = successes[0][0]["id"]
    assert _wallet(rp_state) == (9, 1)
    assert _ledger(rp_state, task_id) == ["RESERVE"]
    with psycopg.connect(rp_state) as pg:
        assert pg.execute("SELECT count(*) FROM replica_prompt_tasks").fetchone() == (1,)
        assert pg.execute("SELECT count(*) FROM wallet_transactions").fetchone() == (1,)
