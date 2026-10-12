"""T38 人物删除与未结算积分保护：真实 PostgreSQL 矩阵。

人物硬删除会经 DB 级 CASCADE 抹掉 character_sheet_tasks、显式删除
character_generation_tasks；若其计费仍留有 PENDING 预留或 UNKNOWN 供应商
轮次，删除会让预留失去常规核对入口。本矩阵在删除事务内核验并拒绝：
SUBMISSION_UNCERTAIN 的场景任务、未闭合预留、UNKNOWN 轮次任一命中都 409，
且绝不产生部分删除；全部终态的干净人物正常删除，账本行（billing_operations）
独立保留。禁止「删除即退分」——本套件不断言任何退分行为。
"""

from __future__ import annotations

import os
from collections.abc import Iterator

os.environ.setdefault(
    "VIDEO_REPLICA_ADMIN_SESSION_HMAC_KEY",
    "test-key-for-character-delete-guard-tests-48-bytes-minimum",
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

from app.auth import CurrentUser
from app.db_pg import DATABASE_URL_ENV, close_pg_pool, pg_transaction
from app.db_portable import BusinessConnection
from app.simple_character import delete_simple_character_identity

GUARD_DB_NAME = "character_delete_guard_test"

_GUARD_TABLES = (
    "billing_operations, billing_attempts, wallet_transactions, wallets, "
    "character_generation_tasks, character_sheet_tasks, character_assets, "
    "character_asset_reviews, character_reference_selections, "
    "project_main_characters, character_versions, character_personas, "
    "person_identities, projects, runtime_settings, billing_tariffs, users"
)


@pytest.fixture(scope="module")
def guard_dsn() -> Iterator[str]:
    require_pg_or_explicit_skip()
    dsn = create_test_database(GUARD_DB_NAME)
    upgrade_test_database_to_head(dsn)
    try:
        yield dsn
    finally:
        drop_test_database(GUARD_DB_NAME)


@pytest.fixture()
def guard_db(guard_dsn: str, monkeypatch: pytest.MonkeyPatch) -> str:
    close_pg_pool()
    monkeypatch.setenv(DATABASE_URL_ENV, guard_dsn)
    monkeypatch.delenv("VIDEO_REPLICA_CUSTOMER_PRODUCTION", raising=False)
    yield guard_dsn
    close_pg_pool()


_OWNER = CurrentUser(id="user_1", username="user_1", display_name="User One", role="employee")


def _reset(dsn: str) -> None:
    with psycopg.connect(dsn, autocommit=True) as pg:
        pg.execute("SET session_replication_role = replica")
        pg.execute(f"TRUNCATE {_GUARD_TABLES} CASCADE")
        pg.execute("SET session_replication_role = DEFAULT")
        pg.execute(
            "INSERT INTO billing_tariffs(service,enabled,unit_credits) VALUES "
            "('character',true,1) ON CONFLICT(service) DO UPDATE SET "
            "enabled=true,unit_credits=1"
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
            "VALUES ('user_1', 100, 0)"
        )


def _seed_identity(dsn: str, *, identity_id: str = "identity-1") -> None:
    with psycopg.connect(dsn, autocommit=True) as pg:
        pg.execute(
            "INSERT INTO person_identities (id, owner_user_id, display_name, "
            "authorization_status, source_quality_status, status, created_by) VALUES "
            "(%s, 'user_1', '测试人物', 'AUTHORIZED', 'PASSED', 'ACTIVE', 'user_1')",
            (identity_id,),
        )
        pg.execute(
            "INSERT INTO character_personas (id, identity_id, name, occupation, "
            "appearance_constraints_json, created_by) VALUES "
            "(%s, %s, '测试角色', '设计师', '{}', 'user_1')",
            (f"persona-{identity_id}", identity_id),
        )
        pg.execute(
            "INSERT INTO character_versions (id, persona_id, version_number, status, "
            "persona_snapshot_json) VALUES (%s, %s, 1, 'PUBLISHED', '{}')",
            (f"version-{identity_id}", f"persona-{identity_id}"),
        )


def _seed_generation_task(
    dsn: str,
    *,
    task_id: str,
    identity_id: str,
    status: str = "SUCCEEDED",
) -> None:
    with psycopg.connect(dsn, autocommit=True) as pg:
        pg.execute(
            "INSERT INTO character_generation_tasks (id, character_version_id, "
            "view_type, provider, model, request_snapshot_json, status, "
            "idempotency_key, request_hash, candidate_number, attempt, max_attempts) "
            "VALUES (%s, %s, 'FRONT_FACE', 'fake', 'fake-model', '{}', %s, %s, %s, 1, 0, 3)",
            (
                task_id,
                f"version-{identity_id}",
                status,
                f"ik-{task_id}",
                f"rh-{task_id}",
            ),
        )


def _seed_sheet_task(
    dsn: str,
    *,
    task_id: str,
    identity_id: str,
    status: str,
) -> None:
    with psycopg.connect(dsn, autocommit=True) as pg:
        pg.execute(
            "INSERT INTO character_sheet_tasks ("
            " id, created_by_user_id, identity_id, idempotency_key, request_hash,"
            " request_json, status, attempt, operation, source_storage_uri,"
            " source_content_type, source_sha256, source_size_bytes"
            ") VALUES (%s, 'user_1', %s, %s, %s, '{}', %s, 1, 'CREATE',"
            " 'fake://sf-uploads/src.png', 'image/png', '" + "f" * 64 + "', 9)",
            (task_id, identity_id, f"ik-{task_id}", f"rh-{task_id}", status),
        )


def _seed_billing(
    dsn: str,
    *,
    operation_id: str,
    source_id: str,
    state: str = "PENDING",
    attempt_state: str | None = None,
) -> None:
    with psycopg.connect(dsn, autocommit=True) as pg:
        pg.execute(
            """
            INSERT INTO billing_operations (
                id, user_id, service, module, source_id, billing_round,
                pricing_snapshot_json, unit, budget_units, reserved_credits,
                funding_json, state, completed_at
            ) VALUES (%s, 'user_1', 'character', 'image', %s, 1,
                      '{"service":"character","unit_credits":1,"version":1}',
                      'image', 1, 1, '[]', %s,
                      CASE WHEN %s = 'PENDING' THEN NULL ELSE now() END)
            """,
            (operation_id, source_id, state, state),
        )
        if attempt_state is not None:
            pg.execute(
                """
                INSERT INTO billing_attempts (
                    id, operation_id, attempt_key, service, provider, unit,
                    state, created_at
                ) VALUES (%s, %s, 'attempt-1', 'character', 'fake', 'image',
                          %s, now())
                """,
                (f"att_{operation_id}", operation_id, attempt_state),
            )


def _attempt_delete(dsn: str, *, identity_id: str) -> None:
    with pg_transaction() as raw:
        delete_simple_character_identity(
            BusinessConnection.postgres(raw),
            actor=_OWNER,
            identity_id=identity_id,
            storage_for_uri=lambda conn, uri: None,  # 无资产场景不会触达
        )


def test_rejects_delete_with_pending_character_reservation(guard_db: str) -> None:
    dsn = guard_db
    _reset(dsn)
    _seed_identity(dsn)
    _seed_generation_task(dsn, task_id="gen-1", identity_id="identity-1")
    _seed_billing(dsn, operation_id="op-gen-1", source_id="gen-1", state="PENDING")

    with pytest.raises(HTTPException) as exc:
        _attempt_delete(dsn, identity_id="identity-1")
    assert exc.value.status_code == 409
    detail = exc.value.detail
    assert detail["code"] == "IDENTITY_DELETE_HAS_UNSETTLED_BILLING"
    # 拒绝即无部分写：人物、任务与预留全部原样保留。
    with psycopg.connect(dsn) as pg:
        counts = pg.execute(
            "SELECT "
            "(SELECT count(*) FROM person_identities WHERE id='identity-1'),"
            "(SELECT count(*) FROM character_generation_tasks WHERE id='gen-1'),"
            "(SELECT count(*) FROM billing_operations WHERE id='op-gen-1' "
            " AND state='PENDING')"
        ).fetchone()
    assert tuple(counts) == (1, 1, 1)


def test_rejects_delete_with_uncertain_sheet_task(guard_db: str) -> None:
    dsn = guard_db
    _reset(dsn)
    _seed_identity(dsn)
    _seed_sheet_task(
        dsn, task_id="sheet-1", identity_id="identity-1", status="SUBMISSION_UNCERTAIN"
    )
    _seed_billing(dsn, operation_id="op-sheet-1", source_id="sheet-1", state="PENDING")

    with pytest.raises(HTTPException) as exc:
        _attempt_delete(dsn, identity_id="identity-1")
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "IDENTITY_DELETE_HAS_UNSETTLED_BILLING"
    with psycopg.connect(dsn) as pg:
        remaining = pg.execute(
            "SELECT status FROM character_sheet_tasks WHERE id='sheet-1'"
        ).fetchone()[0]
    assert remaining == "SUBMISSION_UNCERTAIN"


def test_rejects_delete_with_unknown_provider_attempt(guard_db: str) -> None:
    dsn = guard_db
    _reset(dsn)
    _seed_identity(dsn)
    _seed_generation_task(dsn, task_id="gen-2", identity_id="identity-1")
    # 计费已收口但供应商轮次仍 UNKNOWN：证据链未闭，删除仍被拒。
    _seed_billing(
        dsn,
        operation_id="op-gen-2",
        source_id="gen-2",
        state="SUCCEEDED",
        attempt_state="UNKNOWN",
    )

    with pytest.raises(HTTPException) as exc:
        _attempt_delete(dsn, identity_id="identity-1")
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "IDENTITY_DELETE_HAS_UNSETTLED_BILLING"


def test_all_terminal_identity_deletes_and_keeps_ledger_rows(guard_db: str) -> None:
    dsn = guard_db
    _reset(dsn)
    _seed_identity(dsn)
    _seed_generation_task(dsn, task_id="gen-3", identity_id="identity-1", status="SUCCEEDED")
    _seed_sheet_task(dsn, task_id="sheet-3", identity_id="identity-1", status="SUCCEEDED")
    _seed_billing(dsn, operation_id="op-gen-3", source_id="gen-3", state="SUCCEEDED")
    _seed_billing(dsn, operation_id="op-sheet-3", source_id="sheet-3", state="SUCCEEDED")

    with pg_transaction() as raw:
        plan = delete_simple_character_identity(
            BusinessConnection.postgres(raw),
            actor=_OWNER,
            identity_id="identity-1",
            storage_for_uri=lambda conn, uri: None,
        )
    assert plan is not None
    with psycopg.connect(dsn) as pg:
        counts = pg.execute(
            "SELECT "
            "(SELECT count(*) FROM person_identities WHERE id='identity-1'),"
            "(SELECT count(*) FROM character_personas WHERE id='persona-identity-1'),"
            "(SELECT count(*) FROM character_versions WHERE id='version-identity-1'),"
            "(SELECT count(*) FROM character_generation_tasks WHERE id='gen-3'),"
            "(SELECT count(*) FROM character_sheet_tasks WHERE id='sheet-3'),"
            "(SELECT count(*) FROM billing_operations WHERE id IN "
            " ('op-gen-3','op-sheet-3'))"
        ).fetchone()
    # 人物域全部清除；账本行独立保留（对账关联不随人物删除消失）。
    assert tuple(counts) == (0, 0, 0, 0, 0, 2)


def test_sheet_task_unknown_status_missing_is_blocked_by_pending_check(
    guard_db: str,
) -> None:
    """场景任务被并发收口为终态、但预留尚未被 reconcile 释放的窗口内删除仍被拒。"""
    dsn = guard_db
    _reset(dsn)
    _seed_identity(dsn)
    _seed_sheet_task(dsn, task_id="sheet-4", identity_id="identity-1", status="FAILED")
    _seed_billing(dsn, operation_id="op-sheet-4", source_id="sheet-4", state="PENDING")

    with pytest.raises(HTTPException) as exc:
        _attempt_delete(dsn, identity_id="identity-1")
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "IDENTITY_DELETE_HAS_UNSETTLED_BILLING"
