"""全自动复刻编排器 M6b #1：供应商发送意图（提交点 fencing）真库验收.

方案 `docs/复刻全自动链路方案-2026-11-11.md` §6：
- 新增付费提交的线性化点 = 发送意图落库；
- 提交许可谓词：父状态 ∈ 推进态 ∪ NEEDS_REVIEW、非挂起、非 uncertain、总开关开启；
- 同一提交点重放幂等，不同 attempt 允许新意图。

本套件在隔离迁移库上验证 DDL 与常量不漂移，并逐条钉住许可/拒绝语义。
"""

from __future__ import annotations

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
from app.db_portable import BusinessConnection

DATABASE = "rplc_pipeline_intents_test"

_INSERT_SQL = (
    "INSERT INTO replica_pipeline_tasks ("
    "id, owner_user_id, project_id, platform, video_id, request_hash,"
    " status, stage, character_version_id, project_fingerprint,"
    " idempotency_key, next_poll_at, created_at, updated_at,"
    " first_frame_task_id, analysis_task_id"
    ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now(), now(), now(), %s, %s)"
)

_EXPECTED_COLUMNS = frozenset(
    {"id", "pipeline_id", "sub_task_id", "domain", "attempt", "created_at"}
)


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
    first_frame_task_id: str | None = None,
    analysis_task_id: str | None = None,
) -> str:
    _seed_user(conn, f"u-{slot}")
    pipeline_id = f"rplc-intent-{slot}"
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
            rp.STAGE_FIRST_FRAME,
            "char-1",
            "fp-1",
            f"idem-{slot}",
            first_frame_task_id,
            analysis_task_id,
        ),
    )
    return pipeline_id


def _set_switch(conn: BusinessConnection, enabled: bool) -> None:
    conn.execute(
        "UPDATE runtime_settings SET replica_pipeline_enabled = %s WHERE id = 1",
        (enabled,),
    )
    conn.commit()


def _count_intents(conn: BusinessConnection, pipeline_id: str) -> int:
    row = conn.execute(
        "SELECT count(*) AS n FROM replica_pipeline_submit_intents WHERE pipeline_id = %s",
        (pipeline_id,),
    ).fetchone()
    return int(row["n"]) if row is not None else 0


def test_intent_table_schema_matches_constants(biz: BusinessConnection) -> None:
    columns = {
        str(row["column_name"])
        for row in biz.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'replica_pipeline_submit_intents'"
        ).fetchall()
    }
    assert columns == _EXPECTED_COLUMNS
    constraint = biz.execute(
        "SELECT pg_get_constraintdef(oid) AS definition FROM pg_constraint "
        "WHERE conname = 'ck_replica_pipeline_submit_intents_domain'"
    ).fetchone()
    assert constraint is not None
    definition = str(constraint["definition"])
    for domain in rp.INTENT_DOMAINS:
        assert f"'{domain}'" in definition


def test_issue_intent_allowed_for_progress_status_and_idempotent(
    biz: BusinessConnection,
) -> None:
    _set_switch(biz, True)
    pipeline_id = _seed(biz, slot="allow")
    allowed = rp.issue_submit_intent(
        biz,
        pipeline_id=pipeline_id,
        sub_task_id="ff-1",
        domain=rp.INTENT_DOMAIN_FIRST_FRAME,
        attempt=1,
    )
    biz.commit()
    assert allowed is True
    assert _count_intents(biz, pipeline_id) == 1
    # 同提交点重放：仍允许（意图已在案），但不重复插入。
    replayed = rp.issue_submit_intent(
        biz,
        pipeline_id=pipeline_id,
        sub_task_id="ff-1",
        domain=rp.INTENT_DOMAIN_FIRST_FRAME,
        attempt=1,
    )
    biz.commit()
    assert replayed is True
    assert _count_intents(biz, pipeline_id) == 1
    # 新 attempt = 新提交点：允许并新增一条。
    assert (
        rp.issue_submit_intent(
            biz,
            pipeline_id=pipeline_id,
            sub_task_id="ff-1",
            domain=rp.INTENT_DOMAIN_FIRST_FRAME,
            attempt=2,
        )
        is True
    )
    biz.commit()
    assert _count_intents(biz, pipeline_id) == 2


def test_issue_intent_denied_when_suspended(biz: BusinessConnection) -> None:
    _set_switch(biz, True)
    pipeline_id = _seed(biz, slot="suspended")
    biz.execute(
        "UPDATE replica_pipeline_tasks SET suspended_at = now() WHERE id = %s",
        (pipeline_id,),
    )
    biz.commit()
    assert (
        rp.issue_submit_intent(
            biz,
            pipeline_id=pipeline_id,
            sub_task_id="ff-1",
            domain=rp.INTENT_DOMAIN_FIRST_FRAME,
            attempt=1,
        )
        is False
    )
    biz.commit()
    assert _count_intents(biz, pipeline_id) == 0


def test_issue_intent_denied_when_switch_off(biz: BusinessConnection) -> None:
    _set_switch(biz, False)
    pipeline_id = _seed(biz, slot="switch-off")
    assert (
        rp.issue_submit_intent(
            biz,
            pipeline_id=pipeline_id,
            sub_task_id="ff-1",
            domain=rp.INTENT_DOMAIN_FIRST_FRAME,
            attempt=1,
        )
        is False
    )
    biz.commit()
    assert _count_intents(biz, pipeline_id) == 0
    _set_switch(biz, True)


def test_issue_intent_denied_for_draining_and_uncertain(biz: BusinessConnection) -> None:
    _set_switch(biz, True)
    draining = _seed(biz, status=rp.STATUS_DRAINING, slot="draining")
    assert (
        rp.issue_submit_intent(
            biz,
            pipeline_id=draining,
            sub_task_id="ff-1",
            domain=rp.INTENT_DOMAIN_FIRST_FRAME,
            attempt=1,
        )
        is False
    )
    uncertain = _seed(biz, status=rp.STATUS_NEEDS_REVIEW, slot="uncertain")
    biz.execute(
        "UPDATE replica_pipeline_tasks SET stage_detail = %s::jsonb WHERE id = %s",
        ('{"uncertain": true}', uncertain),
    )
    biz.commit()
    assert (
        rp.issue_submit_intent(
            biz,
            pipeline_id=uncertain,
            sub_task_id="ff-1",
            domain=rp.INTENT_DOMAIN_FIRST_FRAME,
            attempt=1,
        )
        is False
    )
    biz.commit()
    # NEEDS_REVIEW 且无 uncertain：既有授权内剩余排队任务允许继续提交（r6#3）。
    biz.execute(
        "UPDATE replica_pipeline_tasks SET stage_detail = %s::jsonb WHERE id = %s",
        ('{"uncertain": false}', uncertain),
    )
    biz.commit()
    assert (
        rp.issue_submit_intent(
            biz,
            pipeline_id=uncertain,
            sub_task_id="ff-2",
            domain=rp.INTENT_DOMAIN_FIRST_FRAME,
            attempt=1,
        )
        is True
    )
    biz.commit()


def test_pipeline_id_for_sub_task_resolves_reference_columns(
    biz: BusinessConnection,
) -> None:
    _set_switch(biz, True)
    pipeline_id = _seed(
        biz,
        slot="resolve",
        first_frame_task_id="ff-resolve",
        analysis_task_id="an-resolve",
    )
    biz.commit()
    assert (
        rp.pipeline_id_for_sub_task(
            biz, domain=rp.INTENT_DOMAIN_FIRST_FRAME, sub_task_id="ff-resolve"
        )
        == pipeline_id
    )
    assert (
        rp.pipeline_id_for_sub_task(biz, domain=rp.INTENT_DOMAIN_ANALYSIS, sub_task_id="an-resolve")
        == pipeline_id
    )
    assert (
        rp.pipeline_id_for_sub_task(
            biz, domain=rp.INTENT_DOMAIN_FIRST_FRAME, sub_task_id=f"missing-{uuid.uuid4().hex}"
        )
        is None
    )
