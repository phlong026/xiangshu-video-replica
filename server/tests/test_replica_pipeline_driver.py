"""全自动复刻编排器 M2 驱动核心：幂等受理、租约领取、状态机迁移与期限.

验收基线（docs/复刻全自动链路实施任务清单-2026-11-11.md M2）：转换表逐行、
崩溃恢复复用同 retry_no（操作键稳定性）、假时钟期限触发、失租写入被拒、
挂起行不被领取、等待期退避后可领。全部真 PG 执行（隔离迁移库）。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from pg_test_kit import (
    create_test_database,
    drop_test_database,
    require_pg_or_explicit_skip,
    upgrade_test_database_to_head,
)

from app import replica_pipeline as rp
from app.db_portable import BusinessConnection

DATABASE = "rplc_pipeline_driver_test"

_INSERT_SQL = (
    "INSERT INTO replica_pipeline_tasks ("
    "id, owner_user_id, project_id, platform, video_id, request_hash,"
    " status, stage, character_version_id, project_fingerprint,"
    " idempotency_key, next_poll_at, created_at, updated_at"
    ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())"
)


def _seed_user(conn: BusinessConnection, user_id: str) -> None:
    conn.execute(
        "INSERT INTO users (id, username, display_name, role) VALUES (%s, %s, %s, 'customer') "
        "ON CONFLICT (id) DO NOTHING",
        (user_id, user_id, user_id),
    )


def _seed(
    conn: BusinessConnection,
    *,
    status: str = rp.STATUS_ENQUEUED,
    slot: str = "seed",
    poll_offset_seconds: int = -10,
    created_offset_seconds: int = -60,
    suspended_seconds_total: int = 0,
    suspended: bool = False,
) -> str:
    _seed_user(conn, f"u-{slot}")
    pipeline_id = f"rplc-drv-{slot}"
    next_poll = datetime.now(UTC) + timedelta(seconds=poll_offset_seconds)
    created = datetime.now(UTC) + timedelta(seconds=created_offset_seconds)
    conn.execute(
        _INSERT_SQL,
        (
            pipeline_id,
            f"u-{slot}",
            f"p-{slot}",
            "douyin",
            "v1",
            "hash-1",
            status,
            rp.STAGE_INITIATED,
            "char-1",
            "fp-1",
            f"idem-{slot}",
            next_poll,
            created,
        ),
    )
    if suspended:
        conn.execute(
            "UPDATE replica_pipeline_tasks SET suspended_at = now() WHERE id = %s",
            (pipeline_id,),
        )
    if suspended_seconds_total:
        conn.execute(
            "UPDATE replica_pipeline_tasks SET suspended_seconds_total = %s WHERE id = %s",
            (suspended_seconds_total, pipeline_id),
        )
    return pipeline_id


@pytest.fixture(scope="module", autouse=True)
def _require_pg() -> None:
    require_pg_or_explicit_skip()


@pytest.fixture(scope="module")
def dsn() -> Iterator[str]:
    dsn = create_test_database(DATABASE)
    upgrade_test_database_to_head(dsn)
    try:
        yield dsn
    finally:
        drop_test_database(DATABASE)


@pytest.fixture()
def biz(dsn: str) -> Iterator[BusinessConnection]:
    with psycopg.connect(dsn) as raw:
        yield BusinessConnection.postgres(raw)


def test_enqueue_creates_project_and_pipeline(biz: BusinessConnection) -> None:
    _seed_user(biz, "u-enc")
    result = rp.enqueue_replica_pipeline(
        biz,
        owner_user_id="u-enc",
        platform="douyin",
        video_id="v1",
        character_version_id="char-1",
        idempotency_key="client-key-1",
        request_hash="hash-1",
    )
    assert result["replay"] is False
    row = biz.execute(
        "SELECT status, stage, project_id, character_version_id FROM replica_pipeline_tasks "
        "WHERE id = %s",
        (result["id"],),
    ).fetchone()
    assert row is not None
    assert row["status"] == rp.STATUS_ENQUEUED
    # project 同事务创建（project_id NOT NULL 落库；IMPORTING 挂既有项目）
    project = biz.execute(
        "SELECT owner_user_id, name FROM projects WHERE id = %s",
        (row["project_id"],),
    ).fetchone()
    assert project is not None and project["owner_user_id"] == "u-enc"


def test_enqueue_replays_same_key_and_params(biz: BusinessConnection) -> None:
    _seed_user(biz, "u-replay")
    first = rp.enqueue_replica_pipeline(
        biz,
        owner_user_id="u-replay",
        platform="douyin",
        video_id="v1",
        character_version_id="char-1",
        idempotency_key="client-key-2",
        request_hash="hash-1",
    )
    second = rp.enqueue_replica_pipeline(
        biz,
        owner_user_id="u-replay",
        platform="douyin",
        video_id="v1",
        character_version_id="char-1",
        idempotency_key="client-key-2",
        request_hash="hash-1",
    )
    assert second["replay"] is True and second["id"] == first["id"]
    # 重放必须携带 projectId（评审 B-r4#1：路由人物预检依赖它）。
    assert second["projectId"] == first["projectId"]


def test_enqueue_rejects_same_key_with_different_params(biz: BusinessConnection) -> None:
    _seed_user(biz, "u-conflict")
    rp.enqueue_replica_pipeline(
        biz,
        owner_user_id="u-conflict",
        platform="douyin",
        video_id="v1",
        character_version_id="char-1",
        idempotency_key="client-key-3",
        request_hash="hash-1",
    )
    with pytest.raises(rp.ReplicaPipelineError) as excinfo:
        rp.enqueue_replica_pipeline(
            biz,
            owner_user_id="u-conflict",
            platform="douyin",
            video_id="v1",
            character_version_id="char-1",
            idempotency_key="client-key-3",
            request_hash="hash-DIFFERENT",
        )
    assert excinfo.value.code == "REPLICA_PIPELINE_IDEMPOTENCY_CONFLICT"


def test_acquire_respects_poll_predicate_and_suspension(biz: BusinessConnection) -> None:
    # 全库视角断言：先清场，排除其他用例遗留行对「领完即无」的污染。
    biz.execute("DELETE FROM replica_pipeline_tasks")
    # 可领：ENQUEUED 且 next_poll_at 已过。
    due_id = _seed(biz, slot="due")  # noqa: SLF001
    # 不可领：next_poll_at 在未来。
    _seed(biz, slot="future", poll_offset_seconds=3600)
    # 不可领：挂起中。
    _seed(biz, slot="susp", suspended=True)
    # 不可领：AWAIT_CONFIRM 不在轮询白名单（独立过期扫描驱动）。
    _seed(biz, status=rp.STATUS_AWAIT_CONFIRM, slot="await")
    lease = rp.acquire_replica_pipeline(biz, worker_id="w1")
    assert lease is not None and lease.id == due_id
    assert lease.attempt == 1
    # 领完即无（future/susp/await 均不满足谓词）。
    assert rp.acquire_replica_pipeline(biz, worker_id="w2") is None


def test_acquire_recovers_expired_lease(biz: BusinessConnection) -> None:
    pipeline_id = _seed(biz, slot="recover")
    first = rp.acquire_replica_pipeline(biz, worker_id="w1")
    assert first is not None and first.id == pipeline_id
    # 模拟租约过期：locked_until 拨回过去，其他 worker 可重新领取（attempt 递增）。
    biz.execute(
        "UPDATE replica_pipeline_tasks SET locked_until = now() - interval '1 minute' "
        "WHERE id = %s",
        (pipeline_id,),
    )
    second = rp.acquire_replica_pipeline(biz, worker_id="w2")
    assert second is not None and second.id == pipeline_id
    assert second.attempt == first.attempt + 1
    assert second.worker_id == "w2"


def test_advance_follows_transition_table_and_rejects_illegal(biz: BusinessConnection) -> None:
    pipeline_id = _seed(biz, slot="trans")
    lease = rp.acquire_replica_pipeline(biz, worker_id="w1")
    assert lease is not None and lease.id == pipeline_id
    rp.advance_status(
        biz,
        lease,
        decision=rp.AdvanceDecision(
            kind="completed",
            next_status=rp.STATUS_IMPORTING,
            next_stage=rp.STAGE_IMPORT,
            sub_task_id="imp-1",
            sub_task_column="import_task_id",
        ),
    )
    row = biz.execute(
        "SELECT status, stage, import_task_id, locked_by FROM replica_pipeline_tasks WHERE id = %s",
        (pipeline_id,),
    ).fetchone()
    assert row is not None
    assert row["status"] == rp.STATUS_IMPORTING and row["stage"] == rp.STAGE_IMPORT
    assert row["import_task_id"] == "imp-1" and row["locked_by"] is None
    # 非法直跳（ENQUEUED→COMPOSING 不在转换表）被拒绝——但该行已在 IMPORTING，
    # 用 IMPORTING→COMPOSING（跳过 ANALYZING）验证同一语义。
    lease2 = rp.acquire_replica_pipeline(biz, worker_id="w1")
    assert lease2 is not None
    with pytest.raises(rp.ReplicaPipelineError) as excinfo:
        rp.advance_status(
            biz,
            lease2,
            decision=rp.AdvanceDecision(
                kind="completed", next_status=rp.STATUS_COMPOSING, next_stage=rp.STAGE_COMPOSE
            ),
        )
    assert excinfo.value.code == "REPLICA_PIPELINE_ILLEGAL_TRANSITION"


def test_set_next_poll_backs_off_and_releases_lease(biz: BusinessConnection) -> None:
    pipeline_id = _seed(biz, slot="backoff")
    lease = rp.acquire_replica_pipeline(biz, worker_id="w1")
    assert lease is not None
    rp.set_next_poll(biz, lease, wait_failures=1)
    row = biz.execute(
        "SELECT next_poll_at, locked_by, locked_until FROM replica_pipeline_tasks WHERE id = %s",
        (pipeline_id,),
    ).fetchone()
    assert row is not None and row["locked_by"] is None
    # 首次退避 = 30s（30→60→120→240→300 封顶）。
    gap = row["next_poll_at"] - datetime.now(UTC)
    assert 25 <= gap.total_seconds() <= 35


def test_set_next_poll_rejects_stale_worker(biz: BusinessConnection) -> None:
    pipeline_id = _seed(biz, slot="stale")
    lease = rp.acquire_replica_pipeline(biz, worker_id="w1")
    assert lease is not None
    # 其他 worker 重新领取（attempt 变化）→ 旧 worker 的等待写入必须被拒。
    biz.execute(
        "UPDATE replica_pipeline_tasks SET locked_until = now() - interval '1 minute' "
        "WHERE id = %s",
        (pipeline_id,),
    )
    newer = rp.acquire_replica_pipeline(biz, worker_id="w2")
    assert newer is not None
    with pytest.raises(rp.ReplicaPipelineError) as excinfo:
        rp.set_next_poll(biz, lease, wait_failures=1)
    assert excinfo.value.code == "REPLICA_PIPELINE_LEASE_LOST"


def test_settle_failure_lands_failed_and_stamps_finished(biz: BusinessConnection) -> None:
    pipeline_id = _seed(biz, status=rp.STATUS_IMPORTING, slot="fail")
    lease = rp.acquire_replica_pipeline(biz, worker_id="w1")
    assert lease is not None and lease.id == pipeline_id
    target = rp.settle_failure(biz, lease, code="IMPORT_FAILED", message="导入失败", retryable=True)
    assert target == rp.STATUS_FAILED
    row = biz.execute(
        "SELECT status, stage, finished_at, stage_detail FROM replica_pipeline_tasks WHERE id = %s",
        (pipeline_id,),
    ).fetchone()
    assert row is not None
    assert row["status"] == rp.STATUS_FAILED and row["finished_at"] is not None
    detail = row["stage_detail"]
    assert isinstance(detail, dict) and detail["failureCode"] == "IMPORT_FAILED"


def test_check_deadlines_uses_effective_elapsed(biz: BusinessConnection) -> None:
    # 未超期：created 1 小时前。
    _seed(biz, slot="fresh", created_offset_seconds=-3600)
    # 超期：created 25 小时前（无挂起补偿）。
    overdue_id = _seed(biz, slot="overdue", created_offset_seconds=-25 * 3600)
    # 挂起补偿：created 30 小时前，但累计挂起 8 小时 → 有效 22 小时未超期。
    compensated_id = _seed(
        biz,
        slot="compensated",
        created_offset_seconds=-30 * 3600,
        suspended_seconds_total=8 * 3600,
    )
    now = datetime.now(UTC)
    fresh = biz.execute(
        "SELECT * FROM replica_pipeline_tasks WHERE id = %s",
        (_seed(biz, slot="fresh2", created_offset_seconds=-3600),),
    ).fetchone()
    assert fresh is not None
    assert rp.check_deadlines(biz, fresh, now=now) is False
    overdue = biz.execute(
        "SELECT * FROM replica_pipeline_tasks WHERE id = %s", (overdue_id,)
    ).fetchone()
    assert overdue is not None
    assert rp.check_deadlines(biz, overdue, now=now) is True
    compensated = biz.execute(
        "SELECT * FROM replica_pipeline_tasks WHERE id = %s", (compensated_id,)
    ).fetchone()
    assert compensated is not None
    assert rp.check_deadlines(biz, compensated, now=now) is False
    # AWAIT_CONFIRM 恒不触发执行期限（确认期限由独立过期扫描驱动）。
    awaiting = biz.execute(
        "SELECT * FROM replica_pipeline_tasks WHERE id = %s",
        (_seed(biz, status=rp.STATUS_AWAIT_CONFIRM, slot="await2"),),
    ).fetchone()
    assert awaiting is not None
    assert rp.check_deadlines(biz, awaiting, now=now) is False


def test_suspend_resume_accumulates_seconds(biz: BusinessConnection) -> None:
    pipeline_id = _seed(
        biz,
        slot="suspend",
        suspended_seconds_total=100,
        suspended=True,
    )
    # 挂起起点拨回 10 分钟前，恢复后累计 = 100 + 600±。
    biz.execute(
        "UPDATE replica_pipeline_tasks SET suspended_at = now() - interval '10 minutes' "
        "WHERE id = %s",
        (pipeline_id,),
    )
    rp.resume_from_suspension(biz, pipeline_id)
    row = biz.execute(
        "SELECT suspended_at, suspended_seconds_total FROM replica_pipeline_tasks WHERE id = %s",
        (pipeline_id,),
    ).fetchone()
    assert row is not None
    assert row["suspended_at"] is None
    assert 690 <= int(row["suspended_seconds_total"]) <= 700


def test_stage_operation_key_stable_per_retry_no() -> None:
    assert (
        rp.stage_operation_key("p1", rp.STAGE_IMPORT, 0)
        == rp.stage_operation_key("p1", rp.STAGE_IMPORT, 0)
        == "pipeline:p1:import:0"
    )
    assert rp.stage_operation_key("p1", rp.STAGE_IMPORT, 1) != rp.stage_operation_key(
        "p1", rp.STAGE_IMPORT, 0
    )


def test_backoff_caps_at_five_minutes() -> None:
    assert rp._backoff_seconds(1) == 30
    assert rp._backoff_seconds(2) == 60
    assert rp._backoff_seconds(4) == 240
    assert rp._backoff_seconds(5) == 300
    assert rp._backoff_seconds(9) == 300


def _wait_for_lock_waiter(dsn: str, *, timeout: float) -> None:
    """轮询直到出现等待行锁的会话（确定性竞争同步，m2-r2#3）.

    没有它，赢家 commit 时机与输家 INSERT 时机无因果保证——输家可能走前置
    重放命中而非唯一冲突回滚路径，测试将测不到要保护的分支。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        with psycopg.connect(dsn) as raw:
            row = raw.execute(
                "SELECT count(*) AS n FROM pg_stat_activity "
                "WHERE datname = current_database() AND wait_event_type = 'Lock'"
            ).fetchone()
        if row is not None and int(row[0]) >= 1:
            return
        time.sleep(0.1)
    pytest.fail("输家未在超时内进入锁等待——竞争路径未发生")


# ================= m2-r1 修复验证（并发/交错/DRAINING/豁免） =================


def _enqueue_args(owner: str, key: str, request_hash: str) -> dict[str, object]:
    return {
        "owner_user_id": owner,
        "platform": "douyin",
        "video_id": "v1",
        "character_version_id": "char-1",
        "idempotency_key": key,
        "request_hash": request_hash,
    }


def test_concurrent_enqueue_race_resolves_to_replay(dsn: str) -> None:
    """双连接真实竞争（m2-r1#2）：赢家受理、输家经唯一冲突重读后重放同 id."""

    # 预建 user（独立连接立即提交，避免竞争线程里的 FK 依赖）。
    with psycopg.connect(dsn) as raw:
        _seed_user(BusinessConnection.postgres(raw), "u-race")
    a_started = threading.Event()
    go_commit = threading.Event()
    outcome: dict[str, object] = {}

    def run_winner() -> None:
        with psycopg.connect(dsn) as raw:
            biz = BusinessConnection.postgres(raw)
            outcome["winner"] = rp.enqueue_replica_pipeline(
                biz, **_enqueue_args("u-race", "race-key", "hash-1")
            )
            a_started.set()
            # 保持事务开启（行锁持有）。等主线程通过 pg_stat_activity 确认输家
            # 真的阻塞在唯一索引上（异常恢复路径确实发生）才 commit——
            # 否则输家可能走前置重放命中，测不到冲突回滚路径（m2-r2#3）。
            assert go_commit.wait(timeout=10)
        # with 退出 → commit → 输家解除阻塞并走冲突重读路径。

    def run_loser() -> None:
        a_started.wait(timeout=10)
        with psycopg.connect(dsn) as raw:
            biz = BusinessConnection.postgres(raw)
            outcome["loser"] = rp.enqueue_replica_pipeline(
                biz, **_enqueue_args("u-race", "race-key", "hash-1")
            )

    winner_thread = threading.Thread(target=run_winner)
    loser_thread = threading.Thread(target=run_loser)
    winner_thread.start()
    a_started.wait(timeout=10)
    loser_thread.start()
    _wait_for_lock_waiter(dsn, timeout=10)
    go_commit.set()
    loser_thread.join(timeout=15)
    winner_thread.join(timeout=15)
    winner = outcome["winner"]
    loser = outcome["loser"]
    assert isinstance(winner, dict) and isinstance(loser, dict)
    assert winner["replay"] is False and loser["replay"] is True
    assert winner["id"] == loser["id"]
    # 输家的多余 project 已随事务回滚：该 user 只应有一个项目。
    with psycopg.connect(dsn) as raw:
        biz = BusinessConnection.postgres(raw)
        count = biz.execute(
            "SELECT count(*) AS n FROM projects WHERE owner_user_id = %s", ("u-race",)
        ).fetchone()
    assert count is not None and int(count["n"]) == 1


def test_concurrent_enqueue_race_with_different_params_rejects(dsn: str) -> None:
    """同键异参竞争（m2-r1#2）：输家重读后按参数一致性给出 422 而非静默重放."""

    with psycopg.connect(dsn) as raw:
        _seed_user(BusinessConnection.postgres(raw), "u-race2")
    a_started = threading.Event()
    go_commit = threading.Event()
    outcome: dict[str, object] = {}

    def run_winner() -> None:
        with psycopg.connect(dsn) as raw:
            biz = BusinessConnection.postgres(raw)
            outcome["winner"] = rp.enqueue_replica_pipeline(
                biz, **_enqueue_args("u-race2", "race-key-2", "hash-1")
            )
            a_started.set()
            assert go_commit.wait(timeout=10)

    def run_loser() -> None:
        a_started.wait(timeout=10)
        with psycopg.connect(dsn) as raw:
            biz = BusinessConnection.postgres(raw)
            try:
                outcome["loser"] = rp.enqueue_replica_pipeline(
                    biz, **_enqueue_args("u-race2", "race-key-2", "hash-DIFFERENT")
                )
            except rp.ReplicaPipelineError as exc:
                outcome["loser"] = exc

    winner_thread = threading.Thread(target=run_winner)
    loser_thread = threading.Thread(target=run_loser)
    winner_thread.start()
    a_started.wait(timeout=10)
    loser_thread.start()
    _wait_for_lock_waiter(dsn, timeout=10)
    go_commit.set()
    loser_thread.join(timeout=15)
    winner_thread.join(timeout=15)
    loser = outcome["loser"]
    assert isinstance(loser, rp.ReplicaPipelineError)
    assert loser.code == "REPLICA_PIPELINE_IDEMPOTENCY_CONFLICT"


def test_stale_worker_write_rejected_after_real_takeover(dsn: str, biz: BusinessConnection) -> None:
    """真交错（m2-r1#3）：w1 租约过期回收后，w1 的等待写入在另一连接上被拒."""
    # 连接 A：清场（排除其他用例遗留可领行对 SKIP LOCKED 选择序的污染）
    # + 种子 + w1 领取（租约落库，块退出即提交）。
    with psycopg.connect(dsn) as raw_a:
        conn_a = BusinessConnection.postgres(raw_a)
        conn_a.execute("DELETE FROM replica_pipeline_tasks")
        pipeline_id = _seed(conn_a, slot="takeover")
        lease_w1 = rp.acquire_replica_pipeline(conn_a, worker_id="w1")
        assert lease_w1 is not None
        conn_a.execute(
            "UPDATE replica_pipeline_tasks SET locked_until = now() - interval '1 minute' "
            "WHERE id = %s",
            (pipeline_id,),
        )
    # 连接 B：独立事务抢租成功（attempt 递增）——真实交错，非同连接模拟。
    with psycopg.connect(dsn) as raw_b:
        conn_b = BusinessConnection.postgres(raw_b)
        lease_w2 = rp.acquire_replica_pipeline(conn_b, worker_id="w2")
        assert lease_w2 is not None and lease_w2.id == pipeline_id
        assert lease_w2.attempt == lease_w1.attempt + 1
    # 连接 C：旧 worker w1 的等待写入必须被拒（条件 UPDATE 影响 0 行）。
    with psycopg.connect(dsn) as raw_c:
        conn_c = BusinessConnection.postgres(raw_c)
        with pytest.raises(rp.ReplicaPipelineError) as excinfo:
            rp.set_next_poll(conn_c, lease_w1, wait_failures=1)
    assert excinfo.value.code == "REPLICA_PIPELINE_LEASE_LOST"


def test_backoff_window_fully_gates_and_reopens(biz: BusinessConnection) -> None:
    """退避窗口完整断言（m2-r1#5）：置退避后窗口内不可领、到期后可领."""
    biz.execute("DELETE FROM replica_pipeline_tasks")
    pipeline_id = _seed(biz, slot="gate")
    lease = rp.acquire_replica_pipeline(biz, worker_id="w1")
    assert lease is not None
    rp.set_next_poll(biz, lease, wait_failures=1)
    row = biz.execute(
        "SELECT next_poll_at, locked_by, locked_until FROM replica_pipeline_tasks WHERE id = %s",
        (pipeline_id,),
    ).fetchone()
    assert row is not None
    assert row["locked_by"] is None and row["locked_until"] is None
    # 窗口内：不可领取。
    assert rp.acquire_replica_pipeline(biz, worker_id="w2") is None
    # 到期后：可领取（attempt 续增）。
    biz.execute(
        "UPDATE replica_pipeline_tasks SET next_poll_at = now() - interval '1 second' "
        "WHERE id = %s",
        (pipeline_id,),
    )
    again = rp.acquire_replica_pipeline(biz, worker_id="w3")
    assert again is not None and again.id == pipeline_id


def _seed_open_import_subtask(biz: BusinessConnection, pipeline_id: str, *, status: str) -> None:
    """构造未收口/已收口的导入子任务挂到 pipeline（DRAINING 出口的真库前提）。"""
    # viral_import_tasks.project_id 有 FK——先补 projects 行（幂等）。
    proj_row = biz.execute(
        "SELECT owner_user_id, project_id FROM replica_pipeline_tasks WHERE id = %s",
        (pipeline_id,),
    ).fetchone()
    assert proj_row is not None
    biz.execute(
        "INSERT INTO projects (id, owner_user_id, name) VALUES (%s, %s, %s) "
        "ON CONFLICT (id) DO NOTHING",
        (proj_row["project_id"], proj_row["owner_user_id"], "drain-fixture"),
    )
    row = biz.execute(
        "SELECT owner_user_id, project_id, platform, video_id FROM replica_pipeline_tasks "
        "WHERE id = %s",
        (pipeline_id,),
    ).fetchone()
    assert row is not None
    biz.execute(
        """
        INSERT INTO viral_import_tasks (
            id, owner_user_id, project_id, platform, video_id, purpose,
            idempotency_key, request_hash, request_json, status
        ) VALUES (%s, %s, %s, %s, %s, 'replica', %s, 'h', '{}', %s)
        """,
        (
            f"imp-{pipeline_id}",
            row["owner_user_id"],
            row["project_id"],
            row["platform"],
            row["video_id"],
            f"idem-{pipeline_id}",
            status,
        ),
    )
    biz.execute(
        "UPDATE replica_pipeline_tasks SET import_task_id = %s WHERE id = %s",
        (f"imp-{pipeline_id}", pipeline_id),
    )


def test_settle_failure_with_open_subtask_drains_then_fails(biz: BusinessConnection) -> None:
    """DRAINING 出口（m2-r1#1）：开放子任务 → DRAINING（撤销推进权）；
    子任务收口后观察轮询经 DRAINING→FAILED 释放资格。"""
    pipeline_id = _seed(biz, status=rp.STATUS_IMPORTING, slot="drain")
    _seed_open_import_subtask(biz, pipeline_id, status="RUNNING")
    lease = rp.acquire_replica_pipeline(biz, worker_id="w1")
    assert lease is not None
    target = rp.settle_failure(biz, lease, code="IMPORT_TIMEOUT", message="导入超时")
    assert target == rp.STATUS_DRAINING
    row = biz.execute(
        "SELECT status, finished_at FROM replica_pipeline_tasks WHERE id = %s",
        (pipeline_id,),
    ).fetchone()
    assert row is not None
    assert row["status"] == rp.STATUS_DRAINING and row["finished_at"] is None
    # 子任务收口后：观察轮询可领取 DRAINING 行（无付费动作），落终态释放资格。
    biz.execute(
        "UPDATE viral_import_tasks SET status = 'FAILED' WHERE id = %s",
        (f"imp-{pipeline_id}",),
    )
    drain_lease = rp.acquire_replica_pipeline(biz, worker_id="w2")
    assert drain_lease is not None and drain_lease.id == pipeline_id
    final = rp.settle_failure(biz, drain_lease, code="IMPORT_FAILED", message="导入失败")
    assert final == rp.STATUS_FAILED


def test_enqueued_timeout_exits_failed_directly(biz: BusinessConnection) -> None:
    """排队超时（m2-r1#1）：ENQUEUED 无子任务时经 FAILED 直落（转换表出口）。"""
    pipeline_id = _seed(biz, slot="queue-timeout", created_offset_seconds=-25 * 3600)
    lease = rp.acquire_replica_pipeline(biz, worker_id="w1")
    assert lease is not None
    row = biz.execute(
        "SELECT * FROM replica_pipeline_tasks WHERE id = %s", (pipeline_id,)
    ).fetchone()
    assert row is not None
    assert rp.check_deadlines(biz, row, now=datetime.now(UTC)) is True
    target = rp.settle_failure(biz, lease, code="QUEUE_TIMEOUT", message="排队超时")
    assert target == rp.STATUS_FAILED


def test_deadline_exempts_post_confirm_stages(biz: BusinessConnection) -> None:
    """晚确认豁免（m2-r1#4）：确认点之后（GENERATING/观察态）不受执行期限约束."""
    late_generate = _seed(
        biz, status=rp.STATUS_GENERATING, slot="late-gen", created_offset_seconds=-48 * 3600
    )
    late_review = _seed(
        biz,
        status=rp.STATUS_NEEDS_REVIEW,
        slot="late-review",
        created_offset_seconds=-48 * 3600,
    )
    row_generate = biz.execute(
        "SELECT * FROM replica_pipeline_tasks WHERE id = %s", (late_generate,)
    ).fetchone()
    row_review = biz.execute(
        "SELECT * FROM replica_pipeline_tasks WHERE id = %s", (late_review,)
    ).fetchone()
    assert row_generate is not None and row_review is not None
    assert rp.check_deadlines(biz, row_generate, now=datetime.now(UTC)) is False
    assert rp.check_deadlines(biz, row_review, now=datetime.now(UTC)) is False


@pytest.mark.parametrize(
    "writer",
    ["release_lease", "set_next_poll", "advance_status", "settle_failure"],
)
def test_lease_expiry_enforced_across_transaction_boundary(
    biz: BusinessConnection, writer: str
) -> None:
    """租约过期判定用 clock_timestamp（m2-r2#2）：同事务跨过到期点后，
    四个写入口的预检查/条件 UPDATE 都必须拒绝，且数据库保持不变（m2-r2#4）."""
    biz.execute("DELETE FROM replica_pipeline_tasks")
    pipeline_id = _seed(biz, status=rp.STATUS_IMPORTING, slot="expiry")
    lease = rp.acquire_replica_pipeline(biz, worker_id="w1")
    assert lease is not None
    # 租约拨到 1.5 秒后：预检查此刻通过，pg_sleep(2) 跨过到期点后判定必须翻转。
    biz.execute(
        "UPDATE replica_pipeline_tasks SET locked_until = clock_timestamp() "
        "+ interval '1.5 seconds' WHERE id = %s",
        (pipeline_id,),
    )
    before = biz.execute(
        "SELECT status, locked_by, attempt FROM replica_pipeline_tasks WHERE id = %s",
        (pipeline_id,),
    ).fetchone()
    biz.execute("SELECT pg_sleep(2)")
    with pytest.raises(rp.ReplicaPipelineError) as excinfo:
        if writer == "release_lease":
            rp.release_lease(biz, lease)
        elif writer == "set_next_poll":
            rp.set_next_poll(biz, lease, wait_failures=1)
        elif writer == "advance_status":
            rp.advance_status(
                biz,
                lease,
                decision=rp.AdvanceDecision(
                    kind="completed",
                    next_status=rp.STATUS_ANALYZING,
                    next_stage=rp.STAGE_ANALYZE,
                ),
            )
        else:
            rp.settle_failure(biz, lease, code="X", message="x")
    assert excinfo.value.code == "REPLICA_PIPELINE_LEASE_LOST"
    # 数据库状态不变（条件 UPDATE 未生效）。_NamedRow 无值相等语义，
    # 提取字段值比较（m2-r2 复审：身份比较恒假）。
    after = biz.execute(
        "SELECT status, locked_by, attempt FROM replica_pipeline_tasks WHERE id = %s",
        (pipeline_id,),
    ).fetchone()
    assert after is not None and before is not None
    assert (
        after["status"],
        after["locked_by"],
        after["attempt"],
    ) == (
        before["status"],
        before["locked_by"],
        before["attempt"],
    )
