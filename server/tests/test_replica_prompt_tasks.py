"""长视频复刻提示词任务族（replica_prompt）——入队/租约/完成/失败/取消（TEST-PG lane）。

与 analysis_tasks 刻意分族的长任务链路：本文件按 test_analysis_task_cancel.py
的服务层先例，直接驱动 enqueue/acquire/prepare/perform/complete/fail/cancel，
用真实 PG 验证三件事——

1. 计费闭环：入队预扣（RESERVE）、成功结算（SETTLE）、失败/取消释放（RELEASE），
   且同资产活跃任务复用不重复计费；
2. 租约语义：认领串行、过期 RUNNING 翻转 INTERRUPTED、迟到完成被 CAS 拦截；
3. 结果发布：versions 落 kind=replica_prompt，载荷为 replica-prompt.v1。

The database is the only real dependency: a dedicated TEST-PG database created
through the CW-007 kit, migrated to alembic ``head`` and TRUNCATEd per test.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

# Set the audit HMAC key before importing app modules (audit writers require it).
os.environ.setdefault(
    "VIDEO_REPLICA_ADMIN_SESSION_HMAC_KEY",
    "test-key-for-replica-prompt-task-tests-minimum-48-bytes-long-1234",
)

import psycopg
import pytest
from fastapi import HTTPException
from pg_test_kit import (
    create_test_database,
    drop_test_database,
    require_pg_or_explicit_skip,
    upgrade_test_database_to_head,
)

from app.analysis import FakeGemini
from app.auth import CurrentUser
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
from app.usage_billing import accept_operation, finish_source, reconcile_operations

RP_DB_NAME = "replica_prompt_test"

_TABLES = (
    "replica_prompt_tasks, analysis_tasks, versions, billing_operations, "
    "wallet_transactions, billing_credit_lots, billing_tariffs, assets, projects, "
    "wallets, audit_logs, users"
)

_ASSET_METADATA = (
    '{"duration_seconds": 120.0, "resolution": "1920x1080", "fps": 30.0, '
    '"aspect_ratio": "16:9"}'
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
    dsn: str, *, duration_seconds: float = 120.0
) -> tuple[dict[str, Any], bool]:
    with pg_transaction() as raw:
        row, created = enqueue_replica_prompt_task(
            BusinessConnection.postgres(raw),
            project_id="proj-1",
            asset_id="asset-1",
            created_by_user_id="owner-1",
            duration_seconds=duration_seconds,
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


def _ledger(dsn: str) -> list[str]:
    with pg_transaction() as raw:
        rows = (
            BusinessConnection.postgres(raw)
            .execute(
                "SELECT type FROM wallet_transactions WHERE billing_operation_id = "
                "(SELECT id FROM billing_operations WHERE source_id = 'task-1' "
                " AND service = 'analysis') ORDER BY ledger_sequence"
            )
            .fetchall()
        )
    return [str(row["type"]) for row in rows]


def _version_payload(dsn: str) -> dict[str, Any] | None:
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

    assert str(row["kind"]) == "replica_prompt"
    return dict(json.loads(str(row["payload_json"])))


def test_enqueue_reserves_once_and_reuses_active_task(rp_state: str) -> None:
    """同资产已有活跃任务时复用：不新插入、不重复预扣."""
    _seed_asset(rp_state)
    row, created = _enqueue(rp_state)
    assert created is True
    assert row["status"] == "PENDING"
    assert _wallet(rp_state) == (9, 1)  # 预扣 1 积分

    again, created_again = _enqueue(rp_state)
    assert created_again is False
    assert again["id"] == row["id"]
    assert _wallet(rp_state) == (9, 1)


def test_acquire_claims_pending_and_expires_stale_running(rp_state: str) -> None:
    _seed_asset(rp_state)
    row, _ = _enqueue(rp_state)

    with pg_transaction() as raw:
        lease = acquire_replica_prompt_task(
            BusinessConnection.postgres(raw), worker_id="worker-a"
        )
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


def test_prepare_perform_complete_publishes_version_and_settles(rp_state: str) -> None:
    """FakeGemini 全链路：prepare 保留本地 URI → perform 出 replica-prompt.v1 →
    complete 落 versions + 结算 SETTLE."""
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
        lease = acquire_replica_prompt_task(
            BusinessConnection.postgres(raw), worker_id="worker-a"
        )
    assert lease is not None

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

    payload = perform_replica_prompt_task(work)
    assert payload["schema_version"] == "replica-prompt.v1"
    assert payload["replica_prompt"].strip()
    assert set(payload["summary"]) == {
        "theme",
        "visual_style",
        "pace",
        "camera_language",
        "color_tone",
    }

    with pg_transaction() as raw:
        complete_replica_prompt_task(
            BusinessConnection.postgres(raw), work=work, payload=payload
        )
    task = _task_row(rp_state, lease.id)
    assert task is not None
    assert task["status"] == "SUCCEEDED"
    assert task["result_version_id"] is not None
    published = _version_payload(rp_state)
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
        lease = acquire_replica_prompt_task(
            BusinessConnection.postgres(raw), worker_id="worker-a"
        )
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
        from datetime import UTC, datetime, timedelta

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
        complete_replica_prompt_task(
            BusinessConnection.postgres(raw), work=work, payload=payload
        )
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
        asset = conn.execute(
            "SELECT * FROM assets WHERE id = 'asset-1'"
        ).fetchone()
        assert asset is not None
        # 180.05 在容差内放行；180.5 拒绝。
        assert_replica_prompt_enqueue_allowed(asset, measured_duration=180.05)
        with pytest.raises(HTTPException) as failure:
            assert_replica_prompt_enqueue_allowed(asset, measured_duration=180.5)
        assert failure.value.detail["code"] == "REPLICA_PROMPT_DURATION_EXCEEDED"
