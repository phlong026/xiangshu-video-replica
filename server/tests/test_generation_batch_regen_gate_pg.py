"""R0 真库回滚验证（深审 2026-10-31 唯一 P0 的收尾条件）。

编排层断言（409 / 不提交 / 回滚）由假连接用例承担（``test_quote_admission.py``
的 ``write_rowcount`` 用例）；本套件在真实 PostgreSQL 上验证批级重生成写序列
的数据库语义——Mimosa 候选扫描误报测试替身 ``execute`` 重载之后选定的真库方案：

- 批级流程的写序列：新批次 INSERT → 每源任务「替换任务 INSERT + CAS supersede
  （``WHERE superseded_by_task_id IS NULL``，与任务级逐字同构）」；
- 第二个源任务被并发赢家抢先 supersede 时，其 CAS 在 READ COMMITTED 下返回
  ``rowcount=0``，流程按 409 语义整单回滚；
- 回滚后：新批次 / 替换任务不落库，任务 1 的 supersede 一并不留——多任务
  原子性在真库成立；并发赢家的替换关系正常持久。

需要 PG 夹具（``scripts/pg-fixture.sh start``）；夹具不可达时按 kit 约定
显式跳过，跳过不计入验收证据。
"""

from __future__ import annotations

from collections.abc import Iterator

import psycopg
import pytest
from pg_test_kit import (
    create_test_database,
    drop_test_database,
    require_pg_or_explicit_skip,
    upgrade_test_database_to_head,
)

R0_DB_NAME = "r0_batch_regen_gate_test"

# 与 app/generation.py 批级/任务级流程逐字同构的三类写（占位符参数化）。
_INSERT_BATCH = """
    INSERT INTO generation_batches (
        id, project_id, created_by_user_id, idempotency_key,
        request_hash, request_snapshot_json, creation_kind, status,
        source_batch_id, source_task_id, generation_reason
    )
    VALUES (%s, %s, %s, %s, %s, %s, %s, 'QUEUED', %s, NULL, %s)
"""
_INSERT_REPLACEMENT = """
    INSERT INTO generation_tasks (
        id, batch_id, generation_mode, provider, model,
        status, archive_status, quality_status,
        prompt_version_id, prompt_snapshot_json, next_poll_at,
        estimated_cost, retry_of_task_id, retry_reason,
        retry_requested_by_user_id, retry_requested_at, billed_seconds
    )
    VALUES (%s, %s, %s, %s, %s, 'PENDING', 'PENDING', 'PENDING',
            %s, %s, CURRENT_TIMESTAMP, %s, %s, %s, %s, CURRENT_TIMESTAMP, %s)
"""
_CAS_SUPERSEDE = """
    UPDATE generation_tasks
    SET superseded_by_task_id = %s, superseded_at = CURRENT_TIMESTAMP,
        updated_at = CURRENT_TIMESTAMP
    WHERE id = %s AND superseded_by_task_id IS NULL
"""


@pytest.fixture(scope="module")
def gate_dsn() -> Iterator[str]:
    require_pg_or_explicit_skip()
    dsn = create_test_database(R0_DB_NAME)
    upgrade_test_database_to_head(dsn)
    try:
        yield dsn
    finally:
        drop_test_database(R0_DB_NAME)


def _seed_source(dsn: str) -> None:
    """种子：一个用户 + 源批次 + 两个未被替代的源任务。"""
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO users (id, username, display_name, role) VALUES "
            "(%s, %s, %s, %s) ON CONFLICT (id) DO NOTHING",
            ("r0-user", "r0-user", "R0 Gate User", "customer"),
        )
        conn.execute(
            _INSERT_BATCH,
            (
                "source-batch",
                None,
                "r0-user",
                "seed-source-batch-key",
                "seed-source-batch-hash",
                '{"quantity": 2, "resolution": "768P"}',
                "replica",
                None,
                "回归",
            ),
        )
        for task_id in ("r0-task-1", "r0-task-2"):
            conn.execute(
                "INSERT INTO generation_tasks (id, batch_id, provider, model) "
                "VALUES (%s, 'source-batch', 'fake_h3', 'test-model') "
                "ON CONFLICT (id) DO NOTHING",
                (task_id,),
            )


def test_cas_conflict_rolls_back_whole_write_sequence(gate_dsn: str) -> None:
    _seed_source(gate_dsn)

    # 批级流程事务：新批次 -> 任务 1 的替换+CAS（成功）-> 任务 2 的 CAS（冲突）。
    with psycopg.connect(gate_dsn) as flow:
        flow.execute(
            _INSERT_BATCH,
            (
                "new-batch",
                None,
                "r0-user",
                "racing-key",
                "racing-hash",
                '{"quantity": 2, "resolution": "768P"}',
                "replica",
                "source-batch",
                "回归",
            ),
        )
        flow.execute(
            _INSERT_REPLACEMENT,
            (
                "replacement-1",
                "new-batch",
                "I2V",
                "fake_h3",
                "test-model",
                None,
                '{"resolution": "768P", "output_duration_seconds": 8}',
                8,
                "r0-task-1",
                "回归",
                "r0-user",
                8,
            ),
        )
        assert flow.execute(_CAS_SUPERSEDE, ("replacement-1", "r0-task-1")).rowcount == 1

        # 并发赢家在流程事务外抢先 supersede 任务 2（模拟真实竞争窗口）。
        with psycopg.connect(gate_dsn, autocommit=True) as winner:
            assert winner.execute(_CAS_SUPERSEDE, ("winner-replacement", "r0-task-2")).rowcount == 1

        # 流程事务内的任务 2 CAS 必须看到已替代行：READ COMMITTED 下逐语句
        # 取新快照，superseded_by_task_id 空值条件不再命中。
        assert flow.execute(_CAS_SUPERSEDE, ("replacement-2", "r0-task-2")).rowcount == 0
        flow.rollback()

    # 整单回滚：新批次 / 替换任务 / 任务 1 的 supersede 全部不落库；
    # 并发赢家的替换关系正常持久。
    with psycopg.connect(gate_dsn, autocommit=True) as verify:
        assert verify.execute(
            "SELECT count(*) FROM generation_batches WHERE id = 'new-batch'"
        ).fetchone() == (0,)
        assert verify.execute(
            "SELECT count(*) FROM generation_tasks WHERE id = 'replacement-1'"
        ).fetchone() == (0,)
        row = verify.execute(
            "SELECT superseded_by_task_id FROM generation_tasks WHERE id = 'r0-task-1'"
        ).fetchone()
        assert row is not None and row[0] is None
        row = verify.execute(
            "SELECT superseded_by_task_id FROM generation_tasks WHERE id = 'r0-task-2'"
        ).fetchone()
        assert row is not None and row[0] == "winner-replacement"
