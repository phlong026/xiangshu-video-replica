"""T40 卡单积分核对与结算闭环：真实 PostgreSQL 矩阵。

reconcile_operations 的资格条件之外的悬留预留（成功缺成片秒数、口播缺交付
证据、first_frame 交付未收口、孤儿源任务）按 reason 入队；证据补齐后自动
收口、超时只升级人工（绝不自动退分）、底层已终态时被动对齐（迟到回调），
终态只写一次。每个用例独立 TRUNCATE 重建场景，库经 pg_test_kit 迁移到 head。
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator

os.environ.setdefault(
    "VIDEO_REPLICA_ADMIN_SESSION_HMAC_KEY",
    "test-key-for-billing-reconcile-tests-minimum-48-bytes-long-x",
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

from app.billing_reconciliation import (
    enqueue_stuck_billing_operations,
    list_reconciliation_items,
    next_step_for,
    process_reconciliation_items,
    resolve_reconciliation_item,
)
from app.db_pg import DATABASE_URL_ENV, close_pg_pool, pg_transaction
from app.db_portable import BusinessConnection
from app.usage_billing import finish_operation

RECONCILE_DB_NAME = "billing_reconcile_test"

_RECONCILE_TABLES = (
    "billing_reconciliation_items, billing_operations, billing_attempts, "
    "wallet_transactions, wallets, generation_tasks, generation_batches, "
    "oral_tasks, first_frame_tasks, character_sheet_tasks, "
    "character_generation_tasks, character_personas, character_versions, "
    "person_identities, script_from_audio_tasks, projects, runtime_settings, "
    "billing_tariffs, users"
)


@pytest.fixture(scope="module")
def reconcile_dsn() -> Iterator[str]:
    require_pg_or_explicit_skip()
    dsn = create_test_database(RECONCILE_DB_NAME)
    upgrade_test_database_to_head(dsn)
    try:
        yield dsn
    finally:
        drop_test_database(RECONCILE_DB_NAME)


@pytest.fixture()
def reconcile_db(reconcile_dsn: str, monkeypatch: pytest.MonkeyPatch) -> str:
    close_pg_pool()
    monkeypatch.setenv(DATABASE_URL_ENV, reconcile_dsn)
    monkeypatch.delenv("VIDEO_REPLICA_CUSTOMER_PRODUCTION", raising=False)
    # 缩短入队与重查窗口，让用例不必真等 60 分钟。
    monkeypatch.setenv("VIDEO_REPLICA_BILLING_STUCK_AFTER_MINUTES", "60")
    monkeypatch.setenv("VIDEO_REPLICA_BILLING_RECHECK_AFTER_MINUTES", "0")
    yield reconcile_dsn
    close_pg_pool()


def _reset(dsn: str) -> None:
    with psycopg.connect(dsn, autocommit=True) as pg:
        pg.execute("SET session_replication_role = replica")
        pg.execute(f"TRUNCATE {_RECONCILE_TABLES} CASCADE")
        pg.execute("SET session_replication_role = DEFAULT")
        pg.execute(
            "INSERT INTO billing_tariffs(service,enabled,unit_credits) VALUES "
            "('video_768p',true,1),('video_2k',true,1),('oral',true,1),"
            "('first_frame',true,1),('character',true,1),('asr',true,1) "
            "ON CONFLICT(service) DO UPDATE SET enabled=true,unit_credits=1"
        )
        pg.execute(
            "INSERT INTO runtime_settings "
            "(id, max_generation_count_per_batch, max_concurrent_h3_tasks, "
            " internal_base_unit_price_fen, min_recharge_fen, recharge_step_fen, "
            " fair_queue_enabled) "
            "VALUES (1, 4, 100, 1000, 10000, 1000, true)"
        )
        pg.execute(
            "INSERT INTO users (id, username, display_name, role) VALUES (%s, %s, %s, %s)",
            ("user_1", "user_1", "User One", "employee"),
        )
        pg.execute(
            "INSERT INTO wallets (user_id, available_credits, reserved_credits) "
            "VALUES ('user_1', 1000, 0)"
        )


def _seed_pending_operation(
    dsn: str,
    *,
    operation_id: str,
    service: str,
    source_id: str,
    reserved: int = 30,
    age_hours: float = 2.0,
    module: str = "studio",
) -> None:
    """直接落一条超龄 PENDING 计费（绕过 accept_operation 的场景耦合）。

    队列扫描与收口都只读 billing_operations；钱包侧流水由 finish_operation
    自行写出，不依赖这条种子。
    """
    with psycopg.connect(dsn, autocommit=True) as pg:
        pg.execute(
            """
            INSERT INTO billing_operations (
                id, user_id, service, module, source_id, billing_round,
                pricing_snapshot_json, unit, budget_units, reserved_credits,
                funding_json, state, created_at
            ) VALUES (%s, 'user_1', %s, %s, %s, 1, %s, 'second', 30, %s,
                      %s, 'PENDING', now() - (%s * interval '1 hour'))
            """,
            (
                operation_id,
                service,
                module,
                source_id,
                json.dumps(
                    {
                        "service": service,
                        "enabled": True,
                        "unit_credits": 1,
                        "unit_rounding": "ceil",
                        "discount_basis_points": 10000,
                        "consumption_rounding": "ceil",
                        "points_per_yuan": 100,
                        "version": 1,
                    }
                ),
                reserved,
                json.dumps(
                    [
                        {
                            "lot_id": None,
                            "credits": reserved,
                            "lot_credits": reserved,
                            "amount_fen": None,
                        }
                    ]
                ),
                age_hours,
            ),
        )
        # accept_operation 会把预留额度从可用挪进暂扣；直接种子的场景同样要
        # 冻结钱包，否则 finish_operation 的「钱包预留积分不一致」守卫会拦。
        pg.execute(
            "UPDATE wallets SET available_credits = available_credits - %s, "
            "reserved_credits = reserved_credits + %s WHERE user_id = 'user_1'",
            (reserved, reserved),
        )


def _seed_video_task(dsn: str, *, task_id: str, status: str, seconds: int | None) -> None:
    with psycopg.connect(dsn, autocommit=True) as pg:
        pg.execute(
            "INSERT INTO projects (id, owner_user_id, name) VALUES (%s, 'user_1', %s)",
            (f"proj_{task_id}", f"proj_{task_id}"),
        )
        pg.execute(
            "INSERT INTO generation_batches ("
            " id, project_id, created_by_user_id, idempotency_key, "
            " request_hash, request_snapshot_json"
            ") VALUES (%s, %s, 'user_1', %s, 'hash', '{}')",
            (f"batch_{task_id}", f"proj_{task_id}", f"key_{task_id}"),
        )
        pg.execute(
            "INSERT INTO generation_tasks (id, batch_id, generation_mode, provider, "
            "model, status, actual_output_seconds) VALUES (%s, %s, 'I2V', 'fake_h3', "
            "'MiniMax-H3', %s, %s)",
            (task_id, f"batch_{task_id}", status, seconds),
        )


def _queue_rows(dsn: str) -> list[dict[str, object]]:
    with psycopg.connect(dsn, row_factory=psycopg.rows.dict_row) as pg:
        rows = pg.execute(
            "SELECT service, source_id, reason, state, check_count "
            "FROM billing_reconciliation_items ORDER BY source_id"
        ).fetchall()
    return [dict(r) for r in rows]


def test_enqueue_classifies_stuck_reasons(reconcile_db: str) -> None:
    dsn = reconcile_db
    _reset(dsn)
    # 视频成功但缺成片秒数 → SUCCESS_MISSING_DURATION。
    _seed_video_task(dsn, task_id="video_nodur", status="SUCCEEDED", seconds=None)
    _seed_pending_operation(
        dsn, operation_id="op_nodur", service="video_768p", source_id="video_nodur"
    )
    # 口播成功但无交付证据（finalize 冻结分支）→ SUCCESS_EVIDENCE_MISSING。
    with psycopg.connect(dsn, autocommit=True) as pg:
        pg.execute(
            "INSERT INTO person_identities (id, owner_user_id, display_name, "
            "authorization_status, source_quality_status, status, created_by) VALUES "
            "('oral-identity', 'user_1', '口播人物', 'AUTHORIZED', 'PASSED', "
            "'ACTIVE', 'user_1')"
        )
        pg.execute(
            "INSERT INTO assets (id, kind, storage_uri, sha256, size_bytes, "
            "content_type, metadata_json) VALUES ('oral-avatar-src', 'character', "
            "'fake://avatars/src.png', %s, 9, 'image/png', '{}')",
            ("a" * 64,),
        )
        pg.execute(
            "INSERT INTO oral_avatars (id, identity_id, owner_user_id, title, status, "
            "source_kind, source_asset_id) VALUES ('oral-avatar', 'oral-identity', "
            "'user_1', '口播分身', 'READY', 'IMAGE', 'oral-avatar-src')"
        )
        pg.execute(
            "INSERT INTO oral_tasks (id, owner_user_id, identity_id, avatar_id, "
            "mode, title, script_text, status, estimated_cost_fen, idempotency_key, "
            "request_hash, billing_round) "
            "VALUES ('oral_stuck', 'user_1', 'oral-identity', 'oral-avatar', 'TTS', "
            "'卡单口播', '口播词', 'SUCCEEDED', 0, 'ik-oral-stuck', 'rh-oral-stuck', 1)"
        )
    _seed_pending_operation(dsn, operation_id="op_oral", service="oral", source_id="oral_stuck")
    # 源任务不存在（T38 孤儿）→ ORPHAN_SOURCE_MISSING。
    _seed_pending_operation(
        dsn,
        operation_id="op_orphan",
        service="character",
        source_id="task_gone",
        module="image",
    )
    # first_frame 成功但发布事务未收口 → DELIVERY_UNFINALIZED。
    with psycopg.connect(dsn, autocommit=True) as pg:
        pg.execute(
            "INSERT INTO projects (id, owner_user_id, name) VALUES ('proj_ff', 'user_1', 'ff')"
        )
        pg.execute(
            "INSERT INTO first_frame_tasks (id, created_by_user_id, project_id, "
            "idempotency_key, request_hash, request_json, status) "
            "VALUES ('ff_stuck', 'user_1', 'proj_ff', 'ik-ff-stuck', 'rh-ff-stuck', "
            "'{}', 'SUCCEEDED')"
        )
    _seed_pending_operation(
        dsn,
        operation_id="op_ff",
        service="first_frame",
        source_id="ff_stuck",
        module="image",
    )

    with pg_transaction() as raw:
        enqueued = enqueue_stuck_billing_operations(BusinessConnection.postgres(raw), limit=100)
    assert enqueued == 4
    rows = {str(r["source_id"]): r for r in _queue_rows(dsn)}
    assert rows["video_nodur"]["reason"] == "SUCCESS_MISSING_DURATION"
    assert rows["oral_stuck"]["reason"] == "SUCCESS_EVIDENCE_MISSING"
    assert rows["task_gone"]["reason"] == "ORPHAN_SOURCE_MISSING"
    assert rows["ff_stuck"]["reason"] == "DELIVERY_UNFINALIZED"

    # 重扫不重复建条目（部分唯一索引 + 幂等更新）。
    with pg_transaction() as raw:
        again = enqueue_stuck_billing_operations(BusinessConnection.postgres(raw), limit=100)
    assert again == 0
    assert len(_queue_rows(dsn)) == 4


def test_process_auto_settles_once_evidence_lands(reconcile_db: str) -> None:
    dsn = reconcile_db
    _reset(dsn)
    _seed_video_task(dsn, task_id="video_evidence", status="SUCCEEDED", seconds=None)
    _seed_pending_operation(
        dsn, operation_id="op_evidence", service="video_768p", source_id="video_evidence"
    )
    with pg_transaction() as raw:
        enqueue_stuck_billing_operations(BusinessConnection.postgres(raw))
    # 证据未落：不结算、不升级（未到 24h）、只记一次重查。
    with pg_transaction() as raw:
        stats = process_reconciliation_items(BusinessConnection.postgres(raw))
    assert stats["resolved"] == 0
    rows = _queue_rows(dsn)
    assert rows[0]["state"] == "OPEN"
    assert int(rows[0]["check_count"]) == 1
    with psycopg.connect(dsn) as pg:
        state = pg.execute(
            "SELECT state FROM billing_operations WHERE id='op_evidence'"
        ).fetchone()[0]
    assert state == "PENDING"

    # 证据落账（成片秒数回传 + 归档交付证据补齐）后自动收口，且终态只写一次。
    with psycopg.connect(dsn, autocommit=True) as pg:
        pg.execute(
            "INSERT INTO assets (id, kind, storage_uri, sha256, size_bytes, "
            "content_type, metadata_json) VALUES ('asset_video_evidence', 'video', "
            "'fake://videos/evidence.mp4', %s, 9, 'video/mp4', '{}')",
            ("b" * 64,),
        )
        pg.execute(
            "UPDATE generation_tasks SET actual_output_seconds = 20, "
            "archive_status = 'ARCHIVED', result_asset_id = 'asset_video_evidence' "
            "WHERE id='video_evidence'"
        )
    with pg_transaction() as raw:
        stats = process_reconciliation_items(BusinessConnection.postgres(raw))
    assert stats["resolved"] == 1
    rows = _queue_rows(dsn)
    assert rows[0]["state"] == "RESOLVED"
    with psycopg.connect(dsn) as pg:
        op = pg.execute(
            "SELECT state, charged_credits FROM billing_operations WHERE id='op_evidence'"
        ).fetchone()
    assert op[0] == "SUCCEEDED"
    assert int(op[1]) == 20
    # 重放 process 不再产生任何变化（终态只写一次）。
    with pg_transaction() as raw:
        replay = process_reconciliation_items(BusinessConnection.postgres(raw))
    assert replay["resolved"] == 0


def test_escalation_never_releases_and_late_callback_resolves(reconcile_db: str) -> None:
    dsn = reconcile_db
    _reset(dsn)
    # 孤儿预留 + 超过升级时限（created_at 25 小时前）。
    _seed_pending_operation(
        dsn,
        operation_id="op_orphan2",
        service="character",
        source_id="task_gone2",
        age_hours=25.0,
        module="image",
    )
    with pg_transaction() as raw:
        enqueue_stuck_billing_operations(BusinessConnection.postgres(raw))
    with pg_transaction() as raw:
        stats = process_reconciliation_items(BusinessConnection.postgres(raw))
    assert stats["escalated"] == 1
    rows = _queue_rows(dsn)
    assert rows[0]["state"] == "ESCALATED"
    # 升级不动账：预留仍 PENDING，绝不自动退分。
    with psycopg.connect(dsn) as pg:
        state = pg.execute("SELECT state FROM billing_operations WHERE id='op_orphan2'").fetchone()[
            0
        ]
    assert state == "PENDING"
    # 升级条目不再自动 finalize（人工接手边界）。
    with pg_transaction() as raw:
        again = process_reconciliation_items(BusinessConnection.postgres(raw))
    assert again["resolved"] == 0

    # 迟到回调：人工核对后底层计费落终态 → 队列被动对齐收口。
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        finish_operation(conn, operation_id="op_orphan2", units=0, succeeded=False, cancelled=True)
    with pg_transaction() as raw:
        stats = process_reconciliation_items(BusinessConnection.postgres(raw))
    assert stats["resolved"] == 1
    rows = _queue_rows(dsn)
    assert rows[0]["state"] == "RESOLVED"
    assert str(rows[0]["reason"]) == "ORPHAN_SOURCE_MISSING"


def test_manual_resolve_requires_terminal_billing(reconcile_db: str) -> None:
    dsn = reconcile_db
    _reset(dsn)
    _seed_pending_operation(
        dsn,
        operation_id="op_manual",
        service="character",
        source_id="task_gone3",
        age_hours=25.0,
        module="image",
    )
    with pg_transaction() as raw:
        enqueue_stuck_billing_operations(BusinessConnection.postgres(raw))
    with pg_transaction() as raw:
        items = list_reconciliation_items(
            BusinessConnection.postgres(raw), state="OPEN", service=None, limit=10, offset=0
        )
    item_id = str(items["items"][0]["id"])
    # 计费仍 PENDING：一句备注不能把没对清的钱标记解决。
    with pytest.raises(HTTPException) as exc:
        with pg_transaction() as raw:
            resolve_reconciliation_item(
                BusinessConnection.postgres(raw),
                item_id=item_id,
                actor_id="admin_1",
                note="口头确认已处理",
            )
    assert exc.value.status_code == 409

    # 底层收口后人工核对可关闭，并带上最终账务状态。
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        finish_operation(conn, operation_id="op_manual", units=0, succeeded=False)
    with pg_transaction() as raw:
        result = resolve_reconciliation_item(
            BusinessConnection.postgres(raw),
            item_id=item_id,
            actor_id="admin_1",
            note="已按失败核对，预留已释放",
        )
    assert result["state"] == "RESOLVED"
    assert result["billing_state"] == "FAILED"

    # 重复收口拒绝（幂等保护）。
    with pytest.raises(HTTPException) as repeat:
        with pg_transaction() as raw:
            resolve_reconciliation_item(
                BusinessConnection.postgres(raw),
                item_id=item_id,
                actor_id="admin_1",
                note="再来一次",
            )
    assert repeat.value.status_code == 409


def test_next_step_copy_never_promises_refund(reconcile_db: str) -> None:
    # 队列话术红线：升级只触发人工，不承诺退分。
    assert "退分" in next_step_for("ESCALATED", "ORPHAN_SOURCE_MISSING")
    assert "自动退" not in next_step_for("ESCALATED", "UNKNOWN_TIMEOUT")
    assert "人工" in next_step_for("ESCALATED", "UNKNOWN_TIMEOUT")
