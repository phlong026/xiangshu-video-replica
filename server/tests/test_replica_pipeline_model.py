"""全自动复刻编排器数据层：表结构、状态机 CHECK 与三个调度集合的真库验证.

M1（``docs/复刻全自动链路实施任务清单-2026-11-11.md``）。DDL 是应用层常量
（``app/replica_pipeline.py``）的冻结快照——本套件在真 PG 上证明两者不漂移，
且部分唯一索引的「在途占用」语义真实成立：同 user/project 在途唯一、收口
（在途集合外的状态）后资格释放，这是确认点资金闸门的数据库级兜底（方案
评审 r1#13、r3#3、r8#4 的落库证明）。

「释放」断言的纪律（m1-r1#2）：占用行用 SAVEPOINT 隔离预期冲突，违规后只回
savepoint 不清占用行；UPDATE 迁移占用行时断言恰好更新 1 行；后继任务用**新
幂等键**插入——这样插入成功只能来自「在途资格已释放」，不能被任何唯一约束
的移除假阳性掩盖。
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterator

import psycopg
import pytest
from pg_test_kit import (
    create_test_database,
    drop_test_database,
    require_pg_or_explicit_skip,
    upgrade_test_database_to_head,
)

from app import replica_pipeline as rp

DATABASE = "rplc_pipeline_model_test"

_EXPECTED_COLUMNS: frozenset[str] = frozenset(
    {
        "id",
        "owner_user_id",
        "project_id",
        "platform",
        "video_id",
        "request_hash",
        "status",
        "stage",
        "stage_retry_no",
        "character_version_id",
        "project_fingerprint",
        "suspended_at",
        "suspended_seconds_total",
        "idempotency_key",
        "locked_by",
        "locked_until",
        "attempt",
        "next_poll_at",
        "import_task_id",
        "analysis_task_id",
        "script_version_id",
        "first_frame_task_id",
        "prompt_version_id",
        "batch_id",
        "quote_token",
        "quote_snapshot",
        "awaiting_since",
        "stage_detail",
        "created_at",
        "updated_at",
        "confirmed_at",
        "finished_at",
    }
)

# 时间列 timestamptz、JSON 列 jsonb：类型契约落在数据库上（m1-r1#1）。
_TIMESTAMPTZ_COLUMNS: frozenset[str] = frozenset(
    {
        "awaiting_since",
        "suspended_at",
        "locked_until",
        "next_poll_at",
        "created_at",
        "updated_at",
        "confirmed_at",
        "finished_at",
    }
)
_JSONB_COLUMNS: frozenset[str] = frozenset({"quote_snapshot", "stage_detail"})

# 占用在途资格的状态（在途集合内抽样覆盖：推进/等待/待核对/收口中）。
_OCCUPYING_STATUSES = (
    rp.STATUS_COMPOSING,
    rp.STATUS_AWAIT_CONFIRM,
    rp.STATUS_NEEDS_REVIEW,
    rp.STATUS_DRAINING,
)
# 释放在途资格的状态：全部终态 + NEEDS_REVIEW_SETTLED（收口无敞口，r3#3）。
_RELEASING_STATUSES = (
    rp.STATUS_NEEDS_REVIEW_SETTLED,
    rp.STATUS_DELIVERED,
    rp.STATUS_DELIVERED_PARTIAL,
    rp.STATUS_FAILED,
    rp.STATUS_CANCELLED,
    rp.STATUS_EXPIRED,
)

_INSERT_SQL = (
    "INSERT INTO replica_pipeline_tasks ("
    "id, owner_user_id, project_id, platform, video_id, request_hash,"
    " status, stage, idempotency_key, next_poll_at"
    ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, now())"
)


def _insert(
    conn: psycopg.Connection,
    *,
    status: str,
    slot: str,
    idempotency_key: str | None = None,
) -> str:
    """插入一行合法最小记录；created_at/updated_at/next_poll_at 走数据库默认与时钟."""
    row_id = f"rplc-{uuid.uuid4().hex[:12]}"
    conn.execute(
        _INSERT_SQL,
        (
            row_id,
            f"rplc-user-{slot}",
            f"rplc-project-{slot}",
            "douyin",
            "v1",
            "req-hash",
            status,
            rp.STAGE_INITIATED,
            idempotency_key or f"rplc-idem-{slot}",
        ),
    )
    return row_id


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


def test_table_columns_and_types(dsn: str) -> None:
    """列集全等 + 类型契约：时间列 timestamptz、JSON 列 jsonb、默认时钟."""
    with psycopg.connect(dsn) as conn:
        rows = conn.execute(
            "SELECT column_name, data_type, column_default "
            "FROM information_schema.columns "
            "WHERE table_name='replica_pipeline_tasks'"
        ).fetchall()
    columns = {row[0]: (row[1], row[2]) for row in rows}
    assert set(columns) == _EXPECTED_COLUMNS
    for name in _TIMESTAMPTZ_COLUMNS:
        assert columns[name][0] == "timestamp with time zone", name
    for name in _JSONB_COLUMNS:
        assert columns[name][0] == "jsonb", name
    assert columns["created_at"][1].startswith("now()")
    assert columns["updated_at"][1].startswith("now()")


def test_status_check_accepts_every_state_and_rejects_unknown(dsn: str) -> None:
    # 16 态全部可落库（每行独立 user/project，避免撞在途唯一索引）。
    with psycopg.connect(dsn) as conn:
        for index, status in enumerate(rp.ALL_STATUSES):
            _insert(conn, status=status, slot=f"enum-{index}")
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "INSERT INTO replica_pipeline_tasks ("
                "id, owner_user_id, project_id, platform, video_id, request_hash,"
                " status, stage, idempotency_key, next_poll_at"
                ") VALUES ('x1','u1','p1','douyin','v1','h','NOT_A_STATE','initiated',"
                "'i1', now())"
            )
        conn.rollback()


def test_status_check_literals_match_constants(dsn: str) -> None:
    """CHECK 的冻结字面量与常量模块全等——两处漂移在此暴露."""
    with psycopg.connect(dsn) as conn:
        definition = conn.execute(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conname='ck_replica_pipeline_tasks_status'"
        ).fetchone()
    assert definition is not None
    literals = set(re.findall(r"'([A-Z_]+)'", definition[0]))
    assert literals == set(rp.ALL_STATUSES)


@pytest.mark.parametrize("status", _OCCUPYING_STATUSES)
def test_occupying_status_blocks_new_pipeline_per_user(dsn: str, status: str) -> None:
    """在途集合内的状态占用用户资格：同 user 异 project 也被拒（r1#13）."""
    with psycopg.connect(dsn) as conn:
        _insert(conn, status=status, slot="occ-user")
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute(
                _INSERT_SQL,
                (
                    "x-occ-user",
                    "rplc-user-occ-user",
                    "rplc-project-other",
                    "douyin",
                    "v1",
                    "h",
                    rp.STATUS_ENQUEUED,
                    rp.STAGE_INITIATED,
                    "i-occ-user",
                ),
            )
        conn.rollback()


@pytest.mark.parametrize("status", _OCCUPYING_STATUSES)
def test_occupying_status_blocks_new_pipeline_per_project(dsn: str, status: str) -> None:
    with psycopg.connect(dsn) as conn:
        _insert(conn, status=status, slot="occ-proj")
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute(
                _INSERT_SQL,
                (
                    "x-occ-proj",
                    f"rplc-user-occ-proj-{status}",
                    "rplc-project-occ-proj",
                    "douyin",
                    "v1",
                    "h",
                    rp.STATUS_ENQUEUED,
                    rp.STAGE_INITIATED,
                    "i-occ-proj",
                ),
            )
        conn.rollback()


@pytest.mark.parametrize("release_status", _RELEASING_STATUSES)
def test_release_status_frees_gate_on_user_and_project(dsn: str, release_status: str) -> None:
    """收口态释放资格（r3#3/r8#4）：占用行迁移后，同维度新任务可入.

    证明纪律（m1-r2/r1-r3 三轮收紧）：①冲突与重试使用**完全相同的维度组合**
    （user 维度：同占用 user + 同冲突 project；project 维度：同占用 project +
    同一全新异 user），仅换任务 id 与幂等键——插入成功只能来自该维度的索引
    不再拦截；②冲突断言 diag.constraint_name 钉死目标索引，防止另一维度
    索引掩盖（假阳性路径逐一堵死）；③跳过状态迁移则重试必然撞同一约束
    （occupying 系列证明同维度冲突真实存在）。
    """
    # 外层 transaction 让内层 transaction 成为真 savepoint（psycopg3 顶层
    # transaction 异常时全量回滚，会把占用行一起滚掉——m1-r1#2 的教训）。
    with psycopg.connect(dsn) as conn, conn.transaction():
        _insert(conn, status=rp.STATUS_COMPOSING, slot=f"rel-{release_status}")
        # 用户维度：同 user（=占用行 user）+ 专用异 project → 必撞 user 索引。
        with pytest.raises(psycopg.errors.UniqueViolation) as excinfo, conn.transaction():
            conn.execute(
                _INSERT_SQL,
                (
                    "x-rel-user",
                    f"rplc-user-rel-{release_status}",
                    f"rplc-project-rel-user-{release_status}",
                    "douyin",
                    "v1",
                    "h",
                    rp.STATUS_ENQUEUED,
                    rp.STAGE_INITIATED,
                    "i-rel-user",
                ),
            )
        assert excinfo.value.diag.constraint_name == "uq_replica_pipeline_active_user"
        moved = conn.execute(
            "UPDATE replica_pipeline_tasks SET status=%s WHERE owner_user_id=%s AND status=%s",
            (release_status, f"rplc-user-rel-{release_status}", rp.STATUS_COMPOSING),
        ).rowcount
        assert moved == 1
        # 释放后重试同一组合（同 user + 同 project），只换任务 id 与幂等键。
        conn.execute(
            _INSERT_SQL,
            (
                f"x-rel-user-retry-{release_status}",
                f"rplc-user-rel-{release_status}",
                f"rplc-project-rel-user-{release_status}",
                "douyin",
                "v1",
                "h",
                rp.STATUS_ENQUEUED,
                rp.STAGE_INITIATED,
                f"i-rel-user-retry-{release_status}",
            ),
        )
    # 项目维度：全新异 user（无在途占用）+ 同 project → 必撞 project 索引。
    with psycopg.connect(dsn) as conn, conn.transaction():
        _insert(conn, status=rp.STATUS_GENERATING, slot=f"rel-proj-{release_status}")
        with pytest.raises(psycopg.errors.UniqueViolation) as excinfo, conn.transaction():
            conn.execute(
                _INSERT_SQL,
                (
                    "x-rel-proj",
                    f"rplc-user-rel-proj-other-{release_status}",
                    f"rplc-project-rel-proj-{release_status}",
                    "douyin",
                    "v1",
                    "h",
                    rp.STATUS_ENQUEUED,
                    rp.STAGE_INITIATED,
                    "i-rel-proj",
                ),
            )
        assert excinfo.value.diag.constraint_name == "uq_replica_pipeline_active_project"
        moved = conn.execute(
            "UPDATE replica_pipeline_tasks SET status=%s WHERE project_id=%s AND status=%s",
            (release_status, f"rplc-project-rel-proj-{release_status}", rp.STATUS_GENERATING),
        ).rowcount
        assert moved == 1
        # 释放后重试同一组合（同异 user + 同 project），只换 id 与幂等键。
        conn.execute(
            _INSERT_SQL,
            (
                f"x-rel-proj-retry-{release_status}",
                f"rplc-user-rel-proj-other-{release_status}",
                f"rplc-project-rel-proj-{release_status}",
                "douyin",
                "v1",
                "h",
                rp.STATUS_ENQUEUED,
                rp.STAGE_INITIATED,
                f"i-rel-proj-retry-{release_status}",
            ),
        )


def test_partial_index_predicates_match_constants(dsn: str) -> None:
    """三个调度集合谓词与常量模块全等（r8#4：三集合差异即业务语义）."""
    with psycopg.connect(dsn) as conn:
        rows = conn.execute(
            "SELECT indexname, indexdef FROM pg_indexes WHERE tablename='replica_pipeline_tasks'"
        ).fetchall()
    definitions = dict(rows)
    poll_literals = set(re.findall(r"'([A-Z_]+)'", definitions["ix_replica_pipeline_poll"]))
    assert poll_literals == set(rp.POLL_STATUSES)
    user_literals = set(re.findall(r"'([A-Z_]+)'", definitions["uq_replica_pipeline_active_user"]))
    assert user_literals == set(rp.ACTIVE_STATUSES)
    project_literals = set(
        re.findall(r"'([A-Z_]+)'", definitions["uq_replica_pipeline_active_project"])
    )
    assert project_literals == set(rp.ACTIVE_STATUSES)
    # 三集合的核心差异必须成立：SETTLED 可轮询（产物恢复观察）但不占敞口；
    # AWAIT_CONFIRM 占敞口但不进轮询（独立过期扫描驱动）。
    assert (
        rp.STATUS_NEEDS_REVIEW_SETTLED in rp.POLL_STATUSES
        and rp.STATUS_NEEDS_REVIEW_SETTLED not in rp.ACTIVE_STATUSES
    )
    assert (
        rp.STATUS_AWAIT_CONFIRM in rp.ACTIVE_STATUSES
        and rp.STATUS_AWAIT_CONFIRM not in rp.POLL_STATUSES
    )


def test_idempotency_key_unique_across_owners(dsn: str) -> None:
    """幂等键全局唯一，且冲突必须落在幂等键约束上（m1-r1#3：防维度假阳性）.

    占用维度（user/project）全部错开，唯一可能的冲突源就是幂等键唯一约束。
    """
    with psycopg.connect(dsn) as conn:
        _insert(
            conn,
            status=rp.STATUS_ENQUEUED,
            slot="idem-a",
            idempotency_key="rplc-shared-idem",
        )
        with pytest.raises(psycopg.errors.UniqueViolation) as excinfo:
            _insert(
                conn,
                status=rp.STATUS_ENQUEUED,
                slot="idem-b",
                idempotency_key="rplc-shared-idem",
            )
        assert excinfo.value.diag.constraint_name == "uq_replica_pipeline_tasks_idempotency_key"
        conn.rollback()
