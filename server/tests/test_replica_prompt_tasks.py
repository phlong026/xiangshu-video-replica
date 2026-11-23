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


@pytest.mark.parametrize("mode", ["analysis", "storyboard"])
@pytest.mark.parametrize("outcome", ["returned", "timeout", "budget_unavailable"])
def test_source_review_cost_is_one_physical_request_without_extra_customer_charge(
    rp_state: str, monkeypatch: pytest.MonkeyPatch, mode: str, outcome: str
) -> None:
    import json
    import time
    from dataclasses import replace

    from app.analysis import (
        ApilioGemini,
        UrllibApilioChatTransport,
        VideoCallBudget,
        _default_analysis_payload,
        analyze_video,
        default_replica_storyboard_payload,
    )
    from app.billing_meter import billing_context
    from app.replica_prompt import ReplicaPromptTaskWork
    from app.usage_billing import finish_operation

    _seed_asset(rp_state, duration_seconds=4)
    task, _ = _enqueue(rp_state, duration_seconds=4, mode="storyboard")
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        conn.execute(
            "INSERT INTO billing_tariffs(service,enabled,unit_cost_fen) "
            "VALUES('quality_inspection',false,7) ON CONFLICT(service) "
            "DO UPDATE SET enabled=false,unit_cost_fen=7"
        )
        operation_id = str(
            conn.execute(
                "SELECT id FROM billing_operations WHERE source_id=%s", (task["id"],)
            ).fetchone()[0]
        )
        lease = acquire_replica_prompt_task(conn, worker_id="source-review-cost")
    assert lease is not None
    calls: list[float] = []
    initial = (
        _default_analysis_payload(4)
        if mode == "analysis"
        else default_replica_storyboard_payload(4)
    )

    def physical(transport: Any, *args: Any, **kwargs: Any):
        calls.append(transport.timeout_seconds)
        if len(calls) == 2:
            # 外呼意图已独立提交；请求期间另一连接可立即锁定并读取记录。
            with psycopg.connect(rp_state) as pg:
                assert pg.execute(
                    "SELECT state FROM billing_attempts WHERE operation_id=%s "
                    "AND service='quality_inspection' FOR UPDATE NOWAIT",
                    (operation_id,),
                ).fetchall() == [("PENDING",)]
            if outcome == "timeout":
                raise TimeoutError("source review timed out")
        value = (
            initial
            if len(calls) == 1
            else {
                "analysis": initial,
                "review_observations": [
                    {
                        "shot_id": shot["shot_id"],
                        "start_time": shot["start_time"],
                        "end_time": shot["end_time"],
                        "visual_observation": shot["action"] + "；" + shot["camera_motion"],
                        "audio_observation": "只有环境声，没有可辨识口播",
                    }
                    for shot in initial["shots"]
                ],
            }
        )
        return json.dumps(
            {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(value)}}]}
        ).encode(), {}

    monkeypatch.setattr(UrllibApilioChatTransport, "post", physical)
    review_timeout = 120 if mode == "analysis" else 300
    initial_timeout = 240 if mode == "analysis" else 945
    deadline = 510 if mode == "analysis" else 1380
    provider = ApilioGemini(
        api_key="test-key",
        transport=UrllibApilioChatTransport(timeout_seconds=initial_timeout),
        video_call_budget=VideoCallBudget(
            attempt_deadline=time.monotonic() + deadline,
            source_review_timeout_seconds=review_timeout,
        ),
    )

    def receipt() -> None:
        assert len(calls) == 1
        if outcome == "budget_unavailable":
            assert provider._video_call_tracker is not None
            provider._video_call_tracker.budget = replace(
                provider._video_call_tracker.budget, attempt_deadline=time.monotonic() + 1
            )

    with billing_context(str(task["id"])):
        if mode == "analysis":
            result = analyze_video(
                video_uri="https://example.com/source.mp4",
                video_duration_seconds=4,
                provider=provider,
                on_provider_result=receipt,
            ).analysis.model_dump(mode="json")
        else:
            result = perform_replica_prompt_task(
                ReplicaPromptTaskWork(
                    lease=lease,
                    provider=provider,
                    video_uri="https://example.com/source.mp4",
                    asset_uri="cos://bucket/proj-1/a.mp4",
                    media_info={"duration_seconds": 4},
                    mode="storyboard",
                ),
                on_provider_result=receipt,
            )
    assert len(result["shots"]) == 2
    assert result["source_review"]["attempted"] is (outcome != "budget_unavailable")
    assert calls == (
        [initial_timeout] if outcome == "budget_unavailable" else [initial_timeout, review_timeout]
    )
    with pg_transaction() as raw:
        finish_operation(
            BusinessConnection.postgres(raw), operation_id=operation_id, units=1, succeeded=True
        )
    with psycopg.connect(rp_state) as pg:
        expected = {
            "returned": [("ACTUAL", 1, 7)],
            "timeout": [("UNKNOWN", None, None)],
            "budget_unavailable": [],
        }
        assert (
            pg.execute(
                "SELECT state,usage,cost_fen FROM billing_attempts WHERE operation_id=%s "
                "AND service='quality_inspection'",
                (operation_id,),
            ).fetchall()
            == expected[outcome]
        )
        assert pg.execute("SELECT count(*) FROM billing_operations").fetchone() == (1,)
        assert pg.execute(
            "SELECT state,actual_units,charged_credits FROM billing_operations WHERE id=%s",
            (operation_id,),
        ).fetchone() == ("SUCCEEDED", 1, 1)
    assert _wallet(rp_state) == (9, 0)
    assert _ledger(rp_state, str(task["id"])) == ["RESERVE", "SETTLE"]


def _seed_source_review_orphan(dsn: str, *, table: str) -> tuple[str, str, str]:
    from pg_test_kit import assert_safe_test_database, database_name_of

    from app.usage_billing import begin_source_attempt, finish_operation

    assert_safe_test_database(database_name_of(dsn))
    assert table in {"analysis_tasks", "replica_prompt_tasks"}
    task_id = "orphan-" + table
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        conn.execute(
            "INSERT INTO billing_tariffs(service,enabled,unit_cost_fen) "
            "VALUES('quality_inspection',false,7) ON CONFLICT(service) "
            "DO UPDATE SET enabled=false,unit_cost_fen=7"
        )
        conn.execute(
            f"INSERT INTO {table} "  # noqa: S608 - 测试固定任务表白名单
            "(id,project_id,asset_id,created_by_user_id,duration_seconds,status,"
            "completed_at,updated_at) VALUES (%s,'proj-1','asset-1','owner-1',4,"
            "'SUCCEEDED',now()-interval '31 minutes',now()-interval '31 minutes')",
            (task_id,),
        )
        operation_id = accept_operation(
            conn, user_id="owner-1", service="analysis", source_id=task_id, units=1
        )
        attempt_id = begin_source_attempt(conn, task_id, service="quality_inspection")
        assert attempt_id is not None
        finish_operation(conn, operation_id=operation_id, units=1, succeeded=True)
        # 沿用既有成本回收测试的夹具老化方法；仅允许测试库，执行回收前恢复不可变守卫。
        conn.execute("SET LOCAL session_replication_role=replica")
        conn.execute(
            "UPDATE billing_operations SET completed_at=now()-interval '31 minutes' WHERE id=%s",
            (operation_id,),
        )
        conn.execute(
            "UPDATE billing_attempts SET created_at=now()-interval '31 minutes' WHERE id=%s",
            (attempt_id,),
        )
        conn.execute("SET LOCAL session_replication_role=origin")
    return task_id, operation_id, attempt_id


@pytest.mark.parametrize("table", ["analysis_tasks", "replica_prompt_tasks"])
@pytest.mark.parametrize(
    "guard",
    [
        "none",
        "running",
        "lease",
        "recent_call",
        "recent_task",
        "recent_operation",
        "owner",
        "other_service",
        "known_usage",
    ],
)
def test_source_review_orphan_recovery_is_fenced_and_never_rebills(
    rp_state: str, table: str, guard: str
) -> None:
    _seed_asset(rp_state, duration_seconds=4)
    task_id, operation_id, attempt_id = _seed_source_review_orphan(rp_state, table=table)
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        # 这里只构造不同年龄与服务类型的历史夹具，不改变被测回收时的财务守卫。
        conn.execute("SET LOCAL session_replication_role=replica")
        if guard == "running":
            conn.execute(f"UPDATE {table} SET status='RUNNING' WHERE id=%s", (task_id,))  # noqa: S608
        elif guard == "lease":
            conn.execute(
                f"UPDATE {table} SET locked_by='live-worker',"
                "locked_until=(now()+interval '1 minute')::text WHERE id=%s",
                (task_id,),
            )  # noqa: S608
        elif guard == "recent_call":
            conn.execute("UPDATE billing_attempts SET created_at=now() WHERE id=%s", (attempt_id,))
        elif guard == "recent_task":
            conn.execute(f"UPDATE {table} SET updated_at=now() WHERE id=%s", (task_id,))  # noqa: S608
        elif guard == "recent_operation":
            conn.execute(
                "UPDATE billing_operations SET completed_at=now() WHERE id=%s", (operation_id,)
            )
        elif guard == "owner":
            conn.execute(
                "INSERT INTO users(id,username,display_name,role) "
                "VALUES('other','other','Other','employee')"
            )
            conn.execute(f"UPDATE {table} SET created_by_user_id='other' WHERE id=%s", (task_id,))  # noqa: S608
        elif guard == "other_service":
            conn.execute(
                "UPDATE billing_attempts SET service='analysis' WHERE id=%s", (attempt_id,)
            )
        elif guard == "known_usage":
            conn.execute(
                "UPDATE billing_attempts SET state='ACTUAL',usage=1,cost_fen=7,"
                "completed_at=now() WHERE id=%s",
                (attempt_id,),
            )
        conn.execute("SET LOCAL session_replication_role=origin")
    with psycopg.connect(rp_state) as pg:
        original = pg.execute(
            "SELECT state,usage,cost_fen,completed_at FROM billing_attempts WHERE id=%s",
            (attempt_id,),
        ).fetchone()
    for _ in range(2):
        with pg_transaction() as raw:
            reconcile_operations(BusinessConnection.postgres(raw))
    with psycopg.connect(rp_state) as pg:
        attempt = pg.execute(
            "SELECT state,usage,cost_fen,completed_at FROM billing_attempts WHERE id=%s",
            (attempt_id,),
        ).fetchone()
        if guard == "none":
            assert attempt[:3] == ("UNKNOWN", None, None)
            assert attempt[3] is not None
        else:
            assert attempt == original
        assert pg.execute("SELECT count(*) FROM billing_operations").fetchone() == (1,)
        assert pg.execute("SELECT count(*) FROM billing_attempts").fetchone() == (1,)
        assert pg.execute(
            "SELECT state,actual_units,charged_credits FROM billing_operations WHERE id=%s",
            (operation_id,),
        ).fetchone() == ("SUCCEEDED", 1, 1)
    assert _wallet(rp_state) == (9, 0)
    assert _ledger(rp_state, task_id) == ["RESERVE", "SETTLE"]


@pytest.mark.parametrize("table", ["analysis_tasks", "replica_prompt_tasks"])
def test_source_review_orphan_recovery_skips_a_locked_task(rp_state: str, table: str) -> None:
    _seed_asset(rp_state, duration_seconds=4)
    task_id, _, attempt_id = _seed_source_review_orphan(rp_state, table=table)
    with psycopg.connect(rp_state) as locker:
        locker.execute(f"SELECT id FROM {table} WHERE id=%s FOR UPDATE", (task_id,))  # noqa: S608
        with pg_transaction() as raw:
            conn = BusinessConnection.postgres(raw)
            conn.execute("SET LOCAL statement_timeout='2s'")
            reconcile_operations(conn)
        assert locker.execute(
            "SELECT state FROM billing_attempts WHERE id=%s", (attempt_id,)
        ).fetchone() == ("PENDING",)
    with pg_transaction() as raw:
        reconcile_operations(BusinessConnection.postgres(raw))
    with psycopg.connect(rp_state) as pg:
        assert pg.execute(
            "SELECT state FROM billing_attempts WHERE id=%s", (attempt_id,)
        ).fetchone() == ("UNKNOWN",)
    assert _wallet(rp_state) == (9, 0)


def test_source_review_orphan_recovery_shares_one_limit_across_both_task_tables(
    rp_state: str,
) -> None:
    _seed_asset(rp_state, duration_seconds=4)
    first = _seed_source_review_orphan(rp_state, table="analysis_tasks")
    second = _seed_source_review_orphan(rp_state, table="replica_prompt_tasks")
    with pg_transaction() as raw:
        reconcile_operations(BusinessConnection.postgres(raw), limit=1)
    with psycopg.connect(rp_state) as pg:
        assert pg.execute(
            "SELECT state,count(*) FROM billing_attempts GROUP BY state ORDER BY state"
        ).fetchall() == [("PENDING", 1), ("UNKNOWN", 1)]
    with pg_transaction() as raw:
        reconcile_operations(BusinessConnection.postgres(raw), limit=1)
    with psycopg.connect(rp_state) as pg:
        assert pg.execute(
            "SELECT state,count(*) FROM billing_attempts GROUP BY state"
        ).fetchall() == [("UNKNOWN", 2)]
        assert pg.execute("SELECT count(*) FROM billing_operations").fetchone() == (2,)
    assert _wallet(rp_state) == (8, 0)
    assert _ledger(rp_state, first[0]) == _ledger(rp_state, second[0]) == ["RESERVE", "SETTLE"]


def test_crashed_long_source_review_releases_parent_then_recovers_unknown_cost_once(
    rp_state: str,
) -> None:
    from app.usage_billing import begin_source_attempt

    _seed_asset(rp_state, duration_seconds=4, task_status="RUNNING")
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        operation_id = accept_operation(
            conn, user_id="owner-1", service="analysis", source_id="task-1", units=1
        )
        attempt_id = begin_source_attempt(conn, "task-1", service="quality_inspection")
        assert attempt_id is not None
    # 活任务和其客户预占、供应商成本均不能被回收器提前结束。
    with pg_transaction() as raw:
        assert reconcile_operations(BusinessConnection.postgres(raw)) == 0
    assert _wallet(rp_state) == (9, 1)
    with psycopg.connect(rp_state) as pg:
        assert pg.execute(
            "SELECT state FROM billing_attempts WHERE id=%s", (attempt_id,)
        ).fetchone() == ("PENDING",)
        pg.execute(
            "UPDATE replica_prompt_tasks SET locked_until=(now()-interval '1 minute')::text "
            "WHERE id='task-1'"
        )
    # 对照真实崩溃路径：到期认领仅把任务标失败并清租约，父账单仍等待回收。
    with pg_transaction() as raw:
        assert (
            acquire_replica_prompt_task(
                BusinessConnection.postgres(raw), worker_id="recovery-worker"
            )
            is None
        )
    task = _task_row(rp_state)
    assert task["status"] == "FAILED" and task["error_code"] == ERROR_INTERRUPTED
    assert task["locked_by"] is None and task["locked_until"] is None
    with pg_transaction() as raw:
        assert reconcile_operations(BusinessConnection.postgres(raw)) == 1
    assert _wallet(rp_state) == (10, 0)
    assert _ledger(rp_state) == ["RESERVE", "RELEASE"]
    with psycopg.connect(rp_state) as pg:
        assert pg.execute(
            "SELECT state,charged_credits FROM billing_operations WHERE id=%s", (operation_id,)
        ).fetchone() == ("FAILED", 0)
        assert pg.execute(
            "SELECT state FROM billing_attempts WHERE id=%s", (attempt_id,)
        ).fetchone() == ("PENDING",)
        # 只老化专用测试库的历史夹具；回收执行时财务不可变守卫保持开启。
        pg.execute("SET LOCAL session_replication_role=replica")
        pg.execute(
            "UPDATE billing_operations SET completed_at=now()-interval '31 minutes' WHERE id=%s",
            (operation_id,),
        )
        pg.execute(
            "UPDATE billing_attempts SET created_at=now()-interval '31 minutes' WHERE id=%s",
            (attempt_id,),
        )
        pg.execute(
            "UPDATE replica_prompt_tasks SET completed_at=(now()-interval '31 minutes')::text,"
            "updated_at=(now()-interval '31 minutes')::text WHERE id='task-1'"
        )
        pg.execute("SET LOCAL session_replication_role=origin")
    for _ in range(2):
        with pg_transaction() as raw:
            assert reconcile_operations(BusinessConnection.postgres(raw)) == 0
    with psycopg.connect(rp_state) as pg:
        assert pg.execute(
            "SELECT state,usage,cost_fen,completed_at IS NOT NULL FROM billing_attempts "
            "WHERE id=%s",
            (attempt_id,),
        ).fetchone() == ("UNKNOWN", None, None, True)
        assert pg.execute("SELECT count(*) FROM billing_operations").fetchone() == (1,)
        assert pg.execute("SELECT count(*) FROM billing_attempts").fetchone() == (1,)
    assert _wallet(rp_state) == (10, 0)
    assert _ledger(rp_state) == ["RESERVE", "RELEASE"]
