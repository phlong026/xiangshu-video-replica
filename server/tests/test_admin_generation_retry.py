"""管理端生成记录一键重试端点（方案 P1-4）的真实 PG 泳道套件。

业务裁决（能否重试、重扣积分、幂等操作记录、审计）由
``retry_generation_task`` 自身的测试矩阵覆盖；本套件钉住管理端路由的边界：
写契约三件套、业务错误码到运营中文文案的映射、PRE_PROVIDER 重新预扣、
同键重放不重复改状态。
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

os.environ.setdefault(
    "VIDEO_REPLICA_ADMIN_SESSION_HMAC_KEY",
    "test-key-for-admin-gen-retry-tests-minimum-48-bytes-long",
)

import psycopg
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pg_test_kit import (
    create_test_database,
    drop_test_database,
    require_pg_or_explicit_skip,
    upgrade_test_database_to_head,
)

from app.admin_auth_routes import AdminActor, get_admin_writer
from app.admin_generation_routes import router as admin_generation_router
from app.db_pg import DATABASE_URL_ENV, close_pg_pool

RETRY_DB_NAME = "admin_gen_retry_test"
_RETRY_URL = "/api/control/generation-records/{task}/retry"

# 清单同时覆盖重试链路会写的每一张表（幂等操作、钱包账本、计费操作、
# 审计），否则上一条用例的余额或幂等行会泄漏进下一条。
_RESET_TABLES = (
    "user_queue_cursors, generation_task_operations, external_call_logs, assets, "
    "wallet_transactions, recharge_orders, internal_access_tokens, wallets, "
    "generation_tasks, generation_batches, billing_operations, audit_logs, "
    "runtime_settings, billing_tariffs, projects, versions, users"
)


def _pg_dsn() -> str:
    return os.environ.get(
        "TEST_POSTGRESQL_URL", "postgresql://testuser:testpass@localhost:5433/customer_v3_test"
    )


@pytest.fixture(scope="module")
def retry_dsn() -> Iterator[str]:
    require_pg_or_explicit_skip(_pg_dsn())
    dsn = create_test_database(RETRY_DB_NAME)
    upgrade_test_database_to_head(dsn)
    try:
        yield dsn
    finally:
        drop_test_database(RETRY_DB_NAME)


def _reset(dsn: str) -> None:
    """清库并重建最小计费配置与两条账号（owner 与管理员）。"""
    with psycopg.connect(dsn, autocommit=True) as conn:
        # Append-only audit triggers refuse TRUNCATE; the T19 fixture precedent
        # bypasses triggers for the isolated fixture database only.
        conn.execute("SET session_replication_role = replica")
        conn.execute(f"TRUNCATE {_RESET_TABLES} CASCADE")
        conn.execute("SET session_replication_role = DEFAULT")
        # 显式零售配置：每单位 1 积分（T26 先例），预扣数值可精确断言。
        conn.execute(
            "INSERT INTO billing_tariffs(service,enabled,unit_credits) VALUES "
            "('video_768p',true,1),('video_2k',true,1)"
        )
        conn.execute(
            "INSERT INTO runtime_settings "
            "(id, max_generation_count_per_batch, max_concurrent_h3_tasks, "
            " internal_base_unit_price_fen, min_recharge_fen, recharge_step_fen, "
            " fair_queue_enabled) "
            "VALUES (1, 4, 100, 1000, 10000, 1000, true)"
        )
        conn.execute(
            "INSERT INTO users (id, username, display_name, role) VALUES "
            "('u-owner', 'u-owner', 'Owner', 'user'), "
            "('admin_u', 'admin_u', 'Admin', 'admin')"
        )
        conn.execute(
            "INSERT INTO projects (id, name, owner_user_id) VALUES ('p-1', 'P', 'u-owner')"
        )
        conn.execute(
            "INSERT INTO generation_batches ("
            " id, project_id, created_by_user_id, idempotency_key, request_hash,"
            " request_snapshot_json, status"
            ") VALUES ('b-1', 'p-1', 'u-owner', 'ik-b1', 'rh-b1', '{}', 'QUEUED')"
        )
        conn.execute(
            "INSERT INTO wallets (user_id, available_credits, reserved_credits) "
            "VALUES ('u-owner', 100, 0)"
        )


def _insert_task(
    dsn: str,
    task_id: str,
    *,
    status: str,
    archive_status: str = "PENDING",
    error_code: str | None = None,
    provider_result_url: str | None = None,
    prompt_snapshot_json: str | None = None,
) -> None:
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO generation_tasks ("
            " id, batch_id, generation_mode, provider, model, status, archive_status,"
            " quality_status, error_code, provider_result_url, prompt_snapshot_json"
            ") VALUES (%s, 'b-1', 'I2V', 'metaso', 'MiniMax-H3', %s, %s, 'PENDING', %s, %s, %s)",
            (
                task_id,
                status,
                archive_status,
                error_code,
                provider_result_url,
                prompt_snapshot_json,
            ),
        )


def _record_released_quote(dsn: str, task_id: str, *, release: bool = True) -> str:
    from app.db_portable import BusinessConnection
    from app.quote_contracts import issue_quote, validate_quote
    from app.usage_billing import accept_operation, finish_operation

    with psycopg.connect(dsn) as raw:
        conn = BusinessConnection.postgres(raw)
        quote = issue_quote(conn, user_id="u-owner", service="video_768p", units=2)
        snapshot = validate_quote(
            conn,
            user_id="u-owner",
            service="video_768p",
            units=2,
            quote_token=quote["quote_token"],
            max_credits=quote["max_credits"],
        )
        operation_id = accept_operation(
            conn,
            user_id="u-owner",
            service="video_768p",
            source_id=task_id,
            units=2,
            pricing_snapshot=snapshot,
        )
        if release:
            finish_operation(conn, operation_id=operation_id, units=0, succeeded=False)
    return operation_id


def _wallet(dsn: str) -> tuple[int, int]:
    with psycopg.connect(dsn) as conn:
        row = conn.execute(
            "SELECT available_credits, reserved_credits FROM wallets WHERE user_id = 'u-owner'"
        ).fetchone()
    assert row is not None
    return int(row[0]), int(row[1])


def _task_row(dsn: str, task_id: str) -> dict[str, Any]:
    from psycopg.rows import dict_row

    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        row = conn.execute(
            "SELECT status, archive_status, error_code, retry_reason,"
            " retry_requested_by_user_id"
            " FROM generation_tasks WHERE id = %s",
            (task_id,),
        ).fetchone()
    assert row is not None
    return row


def _operation_count(dsn: str, task_id: str) -> int:
    with psycopg.connect(dsn) as conn:
        row = conn.execute(
            "SELECT count(*) FROM generation_task_operations WHERE task_id = %s", (task_id,)
        ).fetchone()
    assert row is not None
    return int(row[0])


@pytest.fixture()
def api(retry_dsn: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    close_pg_pool()
    monkeypatch.setenv(DATABASE_URL_ENV, retry_dsn)
    monkeypatch.setenv("VIDEO_REPLICA_SETTINGS_KEY", Fernet.generate_key().decode())
    _reset(retry_dsn)
    app = FastAPI()
    app.include_router(admin_generation_router)
    admin_actor = AdminActor(
        user_id="admin_u",
        username="admin_u",
        display_name="Admin",
        role="admin",
        is_super_admin=False,
        auth_method="password",
        session_id="sess-1",
        session_expires_at="2030-01-01T00:00:00Z",
        last_activity_at="2030-01-01T00:00:00Z",
    )
    app.dependency_overrides[get_admin_writer] = lambda: admin_actor
    with TestClient(app) as test_client:
        yield test_client
    close_pg_pool()


def _post(client: TestClient, task_id: str, *, key: str | None, body: dict[str, Any]) -> Any:
    headers = {} if key is None else {"Idempotency-Key": key}
    return client.post(
        _RETRY_URL.format(task=task_id),
        headers=headers,
        json=body,
    )


def test_retry_route_requires_write_contract(api: TestClient, retry_dsn: str) -> None:
    _insert_task(retry_dsn, "t-contract", status="FAILED", error_code="H3_SETTINGS_UNAVAILABLE")

    missing_key = _post(api, "t-contract", key=None, body={"confirm": True, "reason": "r"})
    assert missing_key.status_code == 400
    assert missing_key.json()["detail"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"

    missing_confirm = _post(api, "t-contract", key="k-1", body={"reason": "r"})
    assert missing_confirm.status_code == 400
    assert missing_confirm.json()["detail"]["code"] == "CONFIRMATION_REQUIRED"

    missing_reason = _post(api, "t-contract", key="k-1", body={"confirm": True, "reason": "  "})
    assert missing_reason.status_code == 400
    assert missing_reason.json()["detail"]["code"] == "REASON_REQUIRED"

    # 三次拒绝都发生在业务裁决之前：任务与幂等表保持原样。
    assert _task_row(retry_dsn, "t-contract")["status"] == "FAILED"
    assert _operation_count(retry_dsn, "t-contract") == 0


def test_retry_route_requeues_pre_provider_failure_and_recharges(
    api: TestClient, retry_dsn: str
) -> None:
    _insert_task(
        retry_dsn,
        "t-pre",
        status="FAILED",
        error_code="H3_SETTINGS_UNAVAILABLE",
        prompt_snapshot_json='{"output_duration_seconds": 2, "resolution": "768P"}',
    )

    _record_released_quote(retry_dsn, "t-pre")
    with psycopg.connect(retry_dsn) as conn:
        conn.execute("UPDATE billing_tariffs SET unit_credits=10,version=version+1")

    response = _post(
        api, "t-pre", key="k-pre", body={"confirm": True, "reason": "运营重试：设置不可用已恢复"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["task_id"] == "t-pre"
    assert body["status"] == "PENDING"

    row = _task_row(retry_dsn, "t-pre")
    assert row["status"] == "PENDING"
    assert row["error_code"] is None
    assert row["retry_reason"] == "运营重试：设置不可用已恢复"
    assert row["retry_requested_by_user_id"] == "admin_u"
    # PRE_PROVIDER 路径按 2 秒档位重新预扣：100 → 98 available，0 → 2 reserved。
    assert _wallet(retry_dsn) == (98, 2)


def test_retry_route_replays_same_key_without_second_charge(
    api: TestClient, retry_dsn: str
) -> None:
    _insert_task(
        retry_dsn,
        "t-replay",
        status="FAILED",
        error_code="H3_SETTINGS_UNAVAILABLE",
        prompt_snapshot_json='{"output_duration_seconds": 2, "resolution": "768P"}',
    )
    _record_released_quote(retry_dsn, "t-replay")
    body = {"confirm": True, "reason": "运营重试"}

    first = _post(api, "t-replay", key="k-replay", body=body)
    assert first.status_code == 200
    second = _post(api, "t-replay", key="k-replay", body=body)
    assert second.status_code == 200

    # 同键重放返回当前任务态，不执行第二次调度写、不再扣一次积分。
    assert _operation_count(retry_dsn, "t-replay") == 1
    assert _wallet(retry_dsn) == (98, 2)


def test_retry_waits_for_billing_release_and_preserves_original_quote(
    api: TestClient, retry_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from time import monotonic

    from app.db_portable import BusinessConnection
    from app.usage_billing import finish_operation

    task_id = "t-release-race"
    _insert_task(
        retry_dsn,
        task_id,
        status="FAILED",
        error_code="H3_SETTINGS_UNAVAILABLE",
        prompt_snapshot_json='{"output_duration_seconds": 2, "resolution": "768P"}',
    )
    original_operation_id = _record_released_quote(retry_dsn, task_id, release=False)
    assert _wallet(retry_dsn) == (98, 2)
    with psycopg.connect(retry_dsn) as raw:
        raw.execute("UPDATE billing_tariffs SET unit_credits=10,version=version+1")
        original = raw.execute(
            "SELECT state, pricing_snapshot_json FROM billing_operations WHERE id=%s",
            (original_operation_id,),
        ).fetchone()
    assert original is not None and original[0] == "PENDING"
    original_snapshot = json.loads(original[1])
    assert original_snapshot["credits"] == original_snapshot["quote_max_credits"] == 2

    task_read_started = Event()
    task_locked = Event()
    billing_read_started = Event()
    retry_pids: list[int] = []
    lock_order: list[str] = []
    real_execute = BusinessConnection.execute

    def observe_execute(conn: BusinessConnection, sql: str, params: Any = ()) -> Any:
        normalized = " ".join(sql.upper().split())
        task_read = (
            "FROM GENERATION_TASKS AS TASK" in normalized
            and "TASK.PROMPT_SNAPSHOT_JSON" in normalized
            and tuple(params) == (task_id,)
        )
        billing_read = (
            "FROM BILLING_OPERATIONS" in normalized
            and "PRICING_SNAPSHOT_JSON" in normalized
            and tuple(params) == (task_id, "u-owner", "video_768p")
        )
        if task_read:
            assert "FOR UPDATE OF TASK" in normalized
            retry_pids.append(conn.raw.info.backend_pid)
            task_read_started.set()
        if billing_read:
            assert task_locked.is_set(), "重试必须先锁任务，再读取并锁定原账务"
            assert "FOR UPDATE" in normalized
            billing_read_started.set()
        # 只观察执行顺序，路由、事务、行锁和账务写入仍全部使用真实 PostgreSQL。
        cursor = real_execute(conn, sql, params)
        if task_read:
            lock_order.append("task")
            task_locked.set()
        if billing_read:
            lock_order.append("billing")
        return cursor

    monkeypatch.setattr(BusinessConnection, "execute", observe_execute)
    body = {"confirm": True, "reason": "原预扣释放完成后安全重试"}
    with ThreadPoolExecutor(max_workers=1) as pool:
        # 退出持锁连接后才等待线程回收，断言失败也不会遗留阻塞事务。
        with psycopg.connect(retry_dsn) as release_raw:
            release_raw.execute("SET LOCAL lock_timeout='5s'")
            release_raw.execute(
                "SELECT id FROM generation_tasks WHERE id=%s FOR UPDATE", (task_id,)
            )
            pending = pool.submit(_post, api, task_id, key="k-release-race", body=body)
            assert task_read_started.wait(5), "重试请求未进入任务行锁"
            assert len(retry_pids) == 1
            assert retry_pids[0] != release_raw.info.backend_pid

            # 以数据库实际阻塞关系为屏障；不靠睡眠时长猜测两个事务是否交错。
            deadline = monotonic() + 5
            while True:
                blocked = release_raw.execute(
                    "SELECT pg_backend_pid() = ANY(pg_blocking_pids(%s))", (retry_pids[0],)
                ).fetchone()
                if blocked is not None and blocked[0]:
                    break
                assert not task_locked.is_set(), "重试未等待释放事务的任务行锁"
                assert monotonic() < deadline, "未观察到重试被释放事务阻塞"
                task_locked.wait(0.01)
            assert not billing_read_started.is_set()
            assert not pending.done()

            release_conn = BusinessConnection.postgres(release_raw)
            assert (
                finish_operation(
                    release_conn,
                    operation_id=original_operation_id,
                    units=0,
                    succeeded=False,
                )
                == 0
            )
            released_wallet = release_raw.execute(
                "SELECT available_credits, reserved_credits FROM wallets WHERE user_id='u-owner'"
            ).fetchone()
            assert released_wallet is not None and tuple(released_wallet) == (100, 0)
            assert not task_locked.is_set()
            assert not billing_read_started.is_set()
            assert not pending.done()
        response = pending.result(timeout=10)

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "PENDING"
    assert lock_order == ["task", "billing"]
    assert _post(api, task_id, key="k-release-race", body=body).status_code == 200
    assert _task_row(retry_dsn, task_id)["status"] == "PENDING"
    assert _operation_count(retry_dsn, task_id) == 1
    assert _wallet(retry_dsn) == (98, 2)
    with psycopg.connect(retry_dsn) as raw:
        operations = raw.execute(
            "SELECT billing_round, state, reserved_credits, charged_credits, "
            "pricing_snapshot_json FROM billing_operations WHERE source_id=%s "
            "ORDER BY billing_round",
            (task_id,),
        ).fetchall()
        ledger = raw.execute(
            "SELECT billing_round, type, available_delta, reserved_delta "
            "FROM wallet_transactions WHERE task_id=%s ORDER BY billing_round, type",
            (task_id,),
        ).fetchall()
    assert [row[:4] for row in operations] == [(1, "FAILED", 2, 0), (2, "PENDING", 2, 0)]
    assert all(json.loads(row[4]) == original_snapshot for row in operations)
    assert ledger == [(1, "RELEASE", 2, -2), (1, "RESERVE", -2, 2), (2, "RESERVE", -2, 2)]


def test_retry_route_rejects_non_retryable_state_with_localized_message(
    api: TestClient, retry_dsn: str
) -> None:
    # 正常成功件（DIRECT 存档）：不落入任何可重试分支。
    _insert_task(retry_dsn, "t-done", status="SUCCEEDED", archive_status="DIRECT")

    response = _post(api, "t-done", key="k-done", body={"confirm": True, "reason": "r"})
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "TASK_RETRY_NOT_ALLOWED"
    assert detail["message"] == "任务当前状态不允许原地重试，请刷新后确认最新状态。"


def test_retry_route_localizes_missing_task(api: TestClient, retry_dsn: str) -> None:
    response = _post(api, "no-such-task", key="k-404", body={"confirm": True, "reason": "r"})
    assert response.status_code == 404
    detail = response.json()["detail"]
    assert detail["code"] == "TASK_NOT_FOUND"
    assert detail["message"] == "任务不存在，或该记录不是视频生成任务（原地重试仅支持视频任务）。"


def test_retry_route_requeues_archive_recovery(api: TestClient, retry_dsn: str) -> None:
    # 已付款成片存档失败：ARCHIVE_ONLY 路径重新排队恢复，不重新扣费。
    _insert_task(
        retry_dsn,
        "t-archive",
        status="SUCCEEDED",
        archive_status="ARCHIVE_FAILED",
        provider_result_url="https://cdn.example.com/result.mp4",
    )

    response = _post(
        api, "t-archive", key="k-archive", body={"confirm": True, "reason": "恢复存档"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "SUCCEEDED"
    assert body["archive_status"] == "ARCHIVE_FAILED"
    assert _wallet(retry_dsn) == (100, 0)


def test_archive_guidance_drives_existing_retry_without_new_charge(
    api: TestClient, retry_dsn: str
) -> None:
    from app.control_routes import ControlGenerationRecord, _attach_retry_guidance
    from app.db_portable import BusinessConnection

    _insert_task(
        retry_dsn,
        "t-guidance",
        status="SUCCEEDED",
        archive_status="ARCHIVE_FAILED",
        provider_result_url="https://synthetic.invalid/already-paid.mp4",
    )
    record = ControlGenerationRecord.model_construct(
        record_id="t-guidance", record_type="VIDEO", status="SUCCEEDED"
    )
    with psycopg.connect(retry_dsn) as conn:
        _attach_retry_guidance(BusinessConnection.postgres(conn), [record])
    assert record.retry_path == "ARCHIVE_ONLY"
    assert "不重新生成或扣费" in str(record.handling_advice)
    body = {"confirm": True, "reason": "恢复已付款归档"}
    for _ in range(2):
        response = _post(api, "t-guidance", key="k-guidance", body=body)
        assert response.status_code == 200
    assert _wallet(retry_dsn) == (100, 0)
    assert _operation_count(retry_dsn, "t-guidance") == 1


def test_paid_terminal_failure_has_no_retry_guidance(api: TestClient, retry_dsn: str) -> None:
    from app.control_routes import ControlGenerationRecord, _attach_retry_guidance
    from app.db_portable import BusinessConnection

    _insert_task(retry_dsn, "t-paid-terminal", status="FAILED", error_code="PROVIDER_FAILED")
    with psycopg.connect(retry_dsn) as conn:
        conn.execute(
            "UPDATE generation_tasks SET provider_task_id='synthetic-paid-task' WHERE id=%s",
            ("t-paid-terminal",),
        )
        record = ControlGenerationRecord.model_construct(
            record_id="t-paid-terminal", record_type="VIDEO", status="FAILED"
        )
        _attach_retry_guidance(BusinessConnection.postgres(conn), [record])
    assert record.retry_path is None
    assert "确认费用" in str(record.handling_advice)
    response = _post(
        api, "t-paid-terminal", key="k-paid", body={"confirm": True, "reason": "核对后处理"}
    )
    assert response.status_code == 409
    assert _wallet(retry_dsn) == (100, 0)


def test_pre_provider_retry_without_accepted_quote_requires_reconfirmation(
    api: TestClient, retry_dsn: str
) -> None:
    _insert_task(
        retry_dsn,
        "t-legacy",
        status="FAILED",
        error_code="H3_SETTINGS_UNAVAILABLE",
        prompt_snapshot_json='{"output_duration_seconds": 2, "resolution": "768P"}',
    )
    response = _post(
        api, "t-legacy", key="k-legacy", body={"confirm": True, "reason": "历史任务重试"}
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "QUOTE_REQUIRED"
    assert "重新生成" in response.json()["detail"]["message"]
    assert _wallet(retry_dsn) == (100, 0)
    assert _task_row(retry_dsn, "t-legacy")["status"] == "FAILED"
    assert _operation_count(retry_dsn, "t-legacy") == 0


@pytest.mark.parametrize("kind", ["batch", "task"])
@pytest.mark.parametrize("first_outcome", ["commit", "rollback"])
def test_paid_regeneration_same_key_waits_before_quote_rejection(
    api: TestClient,
    retry_dsn: str,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    first_outcome: str,
) -> None:
    """跨凭据业务连接必须先等同键事务结束，不能把并行验价拒绝当作未受理。"""
    import json
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from time import monotonic

    from fastapi import HTTPException

    from app import generation, quote_contracts
    from app.auth import CurrentUser
    from app.db_portable import BusinessConnection

    task_id = "t-paid-same-key"
    frozen = {"resolution": "768P", "output_duration_seconds": 8, "quantity": 1}
    _insert_task(
        retry_dsn,
        task_id,
        status="FAILED",
        prompt_snapshot_json=json.dumps({**frozen, "prompt_text": "已确认的冻结提示词"}),
    )
    with psycopg.connect(retry_dsn) as raw:
        raw.execute(
            "UPDATE generation_batches SET request_snapshot_json=%s, status='FAILED' "
            "WHERE id='b-1'",
            (json.dumps(frozen),),
        )
        # 使用内置假供应商，仅避免外部调用；报价、权限、事务和账务均走真实实现。
        raw.execute(
            "UPDATE generation_tasks SET provider='fake_h3', "
            "provider_task_id='synthetic-previous-submission' WHERE id=%s",
            (task_id,),
        )

    actor = CurrentUser(id="u-owner", username="u-owner", display_name="Owner", role="customer")
    source_id = "b-1" if kind == "batch" else task_id
    with psycopg.connect(retry_dsn) as raw:
        quote = generation.generation_regeneration_price_quote(
            BusinessConnection.postgres(raw),
            actor=actor,
            source_kind=kind,
            source_id=source_id,
        )
    assert quote.max_credits == 8
    request = generation.PaidRegenerationRequest(
        idempotency_key="cross-credential-same-key",
        generation_reason="原请求结果未确认后核对",
        quote_token=quote.quote_token,
        max_credits=quote.max_credits,
    )
    first_validated = Event()
    finish_first = Event()
    second_lock_started = Event()
    second_quote_checked = Event()
    pids: dict[str, int] = {}
    accepted_snapshot: dict[str, Any] = {}
    real_validate = generation._validate_regeneration_quote
    real_now = quote_contracts._now
    real_execute = BusinessConnection.execute

    def quote_clock(conn: BusinessConnection) -> int:
        # 两次请求提交同一合同，只推进 B 的报价时钟，避免等待真实五分钟到期。
        if conn.raw.info.backend_pid == pids.get("second"):
            second_quote_checked.set()
            return quote.quote_expires_at
        return real_now(conn)

    def pause_after_valid_quote(conn: BusinessConnection, **kwargs: Any) -> dict[str, Any]:
        snapshot = real_validate(conn, **kwargs)
        if conn.raw.info.backend_pid == pids.get("first"):
            accepted_snapshot.update(snapshot)
            first_validated.set()
            assert finish_first.wait(10), "未放行已确认报价的原请求"
        return snapshot

    def observe_execute(conn: BusinessConnection, sql: str, params: Any = ()) -> Any:
        if (
            conn.raw.info.backend_pid == pids.get("second")
            and "PG_ADVISORY_XACT_LOCK" in sql.upper()
        ):
            second_lock_started.set()
        # 不模拟锁或结果，数据库实际阻塞关系必须由下方 pg_blocking_pids 证实。
        return real_execute(conn, sql, params)

    monkeypatch.setattr(quote_contracts, "_now", quote_clock)
    monkeypatch.setattr(generation, "_validate_regeneration_quote", pause_after_valid_quote)
    monkeypatch.setattr(BusinessConnection, "execute", observe_execute)

    def regenerate(label: str) -> Any:
        with psycopg.connect(retry_dsn) as raw:
            raw.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
            raw.execute("SET LOCAL lock_timeout='8s'")
            pids[label] = raw.info.backend_pid
            conn = BusinessConnection.postgres(raw)
            if kind == "batch":
                result = generation.regenerate_generation_batch(
                    conn, actor=actor, batch_id=source_id, request=request
                )
            else:
                result = generation.regenerate_generation_task(
                    conn, actor=actor, task_id=source_id, request=request
                )
            if label == "first" and first_outcome == "rollback":
                # 模拟业务函数已完成、外层事务在响应前失败；不留下受理或预扣。
                raw.rollback()
            return result

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(regenerate, "first")
        try:
            assert first_validated.wait(5), "原请求未完成真实报价校验"
            second = pool.submit(regenerate, "second")
            assert second_lock_started.wait(5), "重放未先获取同键事务锁"
            assert pids["first"] != pids["second"]
            with psycopg.connect(retry_dsn, autocommit=True) as observer:
                deadline = monotonic() + 5
                while True:
                    row = observer.execute(
                        "SELECT %s = ANY(pg_blocking_pids(%s))",
                        (pids["first"], pids["second"]),
                    ).fetchone()
                    if row is not None and row[0]:
                        break
                    assert not second.done(), "重放未等待原请求提交便提前返回"
                    assert monotonic() < deadline, "未观察到同键重放的真实 PG 阻塞"
                    second_quote_checked.wait(0.01)
            assert not second_quote_checked.is_set(), "同键请求结束前不应重新校验报价"
            assert not second.done()
        finally:
            # 任何断言失败都释放 A，避免线程回收时遗留等待事务。
            finish_first.set()
        first_result = first.result(timeout=10)
        if first_outcome == "commit":
            second_result = second.result(timeout=10)
            assert first_result.id == second_result.id
            assert not second_quote_checked.is_set(), "已受理回放不受报价到期影响"
        else:
            with pytest.raises(HTTPException) as rejected:
                second.result(timeout=10)
            assert rejected.value.detail["code"] == "QUOTE_EXPIRED"
            assert second_quote_checked.is_set()

    with psycopg.connect(retry_dsn) as raw:
        batches = raw.execute(
            "SELECT id FROM generation_batches WHERE idempotency_key=%s",
            (request.idempotency_key,),
        ).fetchall()
        operations = raw.execute(
            "SELECT state, reserved_credits, pricing_snapshot_json "
            "FROM billing_operations WHERE user_id='u-owner'"
        ).fetchall()
        ledger = raw.execute(
            "SELECT type, available_delta, reserved_delta FROM wallet_transactions "
            "WHERE user_id='u-owner'"
        ).fetchall()
        superseded = raw.execute(
            "SELECT superseded_by_task_id FROM generation_tasks WHERE id=%s", (task_id,)
        ).fetchone()
    if first_outcome == "commit":
        assert batches == [(first_result.id,)]
        assert len(operations) == 1
        assert operations[0][:2] == ("PENDING", 8)
        assert json.loads(operations[0][2]) == accepted_snapshot
        assert ledger == [("RESERVE", -8, 8)]
        assert _wallet(retry_dsn) == (92, 8)
        if kind == "task":
            assert superseded is not None and superseded[0] == first_result.tasks[0].id
    else:
        assert batches == []
        assert operations == []
        assert ledger == []
        assert superseded == (None,)
        assert _wallet(retry_dsn) == (100, 0)
