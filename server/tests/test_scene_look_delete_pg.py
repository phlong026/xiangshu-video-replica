"""场景造型删除矩阵：真实 PostgreSQL。

场景造型 = appearance_type=scene 的 persona + 其 PUBLISHED 版本 + 资产。
DELETE /identities/{id}/scene-looks/{contact_sheet_asset_id} 的服务层守卫：
归属/归档、进行中任务、未闭合计费、项目选用任一命中即 409 且无部分删除；
干净场景连 persona/版本/资产行一起删除，存储对象按清理计划在提交后回收，
共享引用不回收；他人身份 404。禁止断言任何退分行为（口径与人物删除一致）。
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from types import SimpleNamespace

os.environ.setdefault(
    "VIDEO_REPLICA_ADMIN_SESSION_HMAC_KEY",
    "test-key-for-scene-look-delete-tests-48-bytes-minimum",
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
from app.simple_character import delete_simple_scene_look

SCENE_DB_NAME = "scene_look_delete_test"

_SCENE_TABLES = (
    "billing_operations, billing_attempts, character_generation_tasks, "
    "character_asset_reviews, character_assets, character_reference_selections, "
    "character_versions, character_personas, person_identities, projects, "
    "assets, runtime_settings, billing_tariffs, users"
)


@pytest.fixture(scope="module")
def scene_dsn() -> Iterator[str]:
    require_pg_or_explicit_skip()
    dsn = create_test_database(SCENE_DB_NAME)
    upgrade_test_database_to_head(dsn)
    try:
        yield dsn
    finally:
        drop_test_database(SCENE_DB_NAME)


@pytest.fixture()
def scene_db(scene_dsn: str, monkeypatch: pytest.MonkeyPatch) -> str:
    close_pg_pool()
    monkeypatch.setenv(DATABASE_URL_ENV, scene_dsn)
    monkeypatch.delenv("VIDEO_REPLICA_CUSTOMER_PRODUCTION", raising=False)
    yield scene_dsn
    close_pg_pool()


_OWNER = CurrentUser(id="user_1", username="user_1", display_name="User One", role="employee")
_FOREIGN = CurrentUser(id="user_2", username="user_2", display_name="User Two", role="employee")

# 测试内联伪造 storage 解析结果：解析器只被要求给出 provider/bucket 供
# 去重键与注册表核验使用，对象 I/O 在测试里不发生（提交后清理由路由层做）。
_FAKE_STORAGE = SimpleNamespace(provider="cos", bucket="bucket")


def _reset(dsn: str) -> None:
    with psycopg.connect(dsn, autocommit=True) as pg:
        pg.execute("SET session_replication_role = replica")
        pg.execute(f"TRUNCATE {_SCENE_TABLES} CASCADE")
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
            "INSERT INTO users (id, username, display_name, role) VALUES (%s, %s, %s, %s)",
            ("user_2", "user_2", "User Two", "employee"),
        )


def _seed_scene_look(
    dsn: str,
    *,
    identity_id: str = "identity-1",
    seed_identity: bool = True,
    persona_id: str = "persona-1",
    version_id: str = "version-1",
    contact_id: str = "contact-1",
    owner_user_id: str = "user_1",
    with_assets: bool = False,
) -> None:
    with psycopg.connect(dsn, autocommit=True) as pg:
        if seed_identity:
            pg.execute(
                "INSERT INTO person_identities (id, owner_user_id, display_name, "
                "authorization_status, source_quality_status, status, created_by) VALUES "
                "(%s, %s, '测试人物', 'AUTHORIZED', 'PASSED', 'ACTIVE', %s)",
                (identity_id, owner_user_id, owner_user_id),
            )
        pg.execute(
            "INSERT INTO character_personas (id, identity_id, name, occupation, "
            "appearance_constraints_json, created_by) VALUES "
            "(%s, %s, '庭院场景', '设计师', '{\"appearance_type\": \"scene\"}', %s)",
            (persona_id, identity_id, owner_user_id),
        )
        pg.execute(
            "INSERT INTO character_versions (id, persona_id, version_number, status, "
            "persona_snapshot_json, publication_snapshot_json, publication_hash) VALUES "
            "(%s, %s, 1, 'PUBLISHED', '{}', %s, %s)",
            (version_id, persona_id, f'{{"contact_sheet_asset_id": "{contact_id}"}}', "h" * 64),
        )
        if with_assets:
            # contact sheet 只由快照引用（走 delete 的快照扫描分支）；
            # character_assets 行 (version, view_type) 唯一，只放一个已发布视图。
            for asset_id in (contact_id, f"view-{contact_id}"):
                pg.execute(
                    "INSERT INTO assets (id, project_id, kind, storage_uri, sha256, "
                    "size_bytes, content_type, created_by_user_id) VALUES "
                    "(%s, NULL, 'character_image', %s, %s, 1024, 'image/png', %s)",
                    (asset_id, f"cos://bucket/{asset_id}.png", "c" * 64, owner_user_id),
                )
            pg.execute(
                "INSERT INTO character_assets (id, character_version_id, "
                "asset_id, view_type, candidate_number, review_status, "
                "is_published_selection) VALUES (%s, %s, %s, 'FRONT_FULL', 1, "
                "'APPROVED', 1)",
                (f"ca-{contact_id}", version_id, f"view-{contact_id}"),
            )


def _attempt_delete(
    dsn: str,
    *,
    identity_id: str = "identity-1",
    contact_id: str = "contact-1",
    actor: CurrentUser = _OWNER,
) -> None:
    with pg_transaction() as raw:
        delete_simple_scene_look(
            BusinessConnection.postgres(raw),
            actor=actor,
            identity_id=identity_id,
            contact_sheet_asset_id=contact_id,
            storage_for_uri=lambda conn, uri: _FAKE_STORAGE,
        )


def test_deletes_scene_persona_version_and_assets(scene_db: str) -> None:
    dsn = scene_db
    _reset(dsn)
    _seed_scene_look(dsn, with_assets=True)

    with pg_transaction() as raw:
        plan = delete_simple_scene_look(
            BusinessConnection.postgres(raw),
            actor=_OWNER,
            identity_id="identity-1",
            contact_sheet_asset_id="contact-1",
            storage_for_uri=lambda conn, uri: _FAKE_STORAGE,
        )
    # 两个资产各产生一个清理目标；测试解析器恒等可用，无失败计数。
    assert plan.resolution_failed_count == 0
    assert {target.asset_id for target in plan.targets} == {
        "contact-1",
        "view-contact-1",
    }
    with psycopg.connect(dsn) as pg:
        counts = pg.execute(
            "SELECT "
            "(SELECT count(*) FROM character_personas WHERE id='persona-1'),"
            "(SELECT count(*) FROM character_versions WHERE id='version-1'),"
            "(SELECT count(*) FROM character_assets),"
            "(SELECT count(*) FROM assets WHERE id IN ('contact-1','view-contact-1')),"
            "(SELECT count(*) FROM person_identities WHERE id='identity-1')"
        ).fetchone()
    # 场景域全部清除；人物身份本身保留（只删了一套装造）。
    assert tuple(counts) == (0, 0, 0, 0, 1)


def test_deleting_one_scene_keeps_sibling_scene(scene_db: str) -> None:
    dsn = scene_db
    _reset(dsn)
    _seed_scene_look(dsn)
    _seed_scene_look(
        dsn,
        seed_identity=False,
        persona_id="persona-2",
        version_id="version-2",
        contact_id="contact-2",
    )

    _attempt_delete(dsn, contact_id="contact-1")

    with psycopg.connect(dsn) as pg:
        remaining = pg.execute(
            "SELECT count(*) FROM character_personas WHERE id='persona-2'"
        ).fetchone()[0]
    assert remaining == 1


def test_returns_404_for_unknown_contact_sheet(scene_db: str) -> None:
    dsn = scene_db
    _reset(dsn)
    _seed_scene_look(dsn)

    with pytest.raises(HTTPException) as exc:
        _attempt_delete(dsn, contact_id="contact-missing")
    assert exc.value.status_code == 404
    assert exc.value.detail["code"] == "SCENE_LOOK_NOT_FOUND"


def test_foreign_owner_gets_404(scene_db: str) -> None:
    dsn = scene_db
    _reset(dsn)
    _seed_scene_look(dsn)

    with pytest.raises(HTTPException) as exc:
        _attempt_delete(dsn, actor=_FOREIGN)
    assert exc.value.status_code == 404
    assert exc.value.detail["code"] == "PERSON_IDENTITY_NOT_FOUND"


def test_blocks_delete_with_active_generation_task(scene_db: str) -> None:
    dsn = scene_db
    _reset(dsn)
    _seed_scene_look(dsn)
    with psycopg.connect(dsn, autocommit=True) as pg:
        pg.execute(
            "INSERT INTO character_generation_tasks (id, character_version_id, "
            "view_type, provider, model, request_snapshot_json, status, "
            "idempotency_key, request_hash, candidate_number, attempt, max_attempts) "
            "VALUES ('gen-1', 'version-1', 'FRONT_FACE', 'fake', 'fake-model', '{}', "
            "'RUNNING', 'ik-gen-1', 'rh-gen-1', 1, 0, 3)"
        )

    with pytest.raises(HTTPException) as exc:
        _attempt_delete(dsn)
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "SCENE_LOOK_HAS_ACTIVE_TASKS"
    with psycopg.connect(dsn) as pg:
        remaining = pg.execute(
            "SELECT status FROM character_generation_tasks WHERE id='gen-1'"
        ).fetchone()[0]
    assert remaining == "RUNNING"


def test_blocks_delete_with_pending_billing(scene_db: str) -> None:
    dsn = scene_db
    _reset(dsn)
    _seed_scene_look(dsn)
    with psycopg.connect(dsn, autocommit=True) as pg:
        pg.execute(
            "INSERT INTO character_generation_tasks (id, character_version_id, "
            "view_type, provider, model, request_snapshot_json, status, "
            "idempotency_key, request_hash, candidate_number, attempt, max_attempts) "
            "VALUES ('gen-2', 'version-1', 'FRONT_FACE', 'fake', 'fake-model', '{}', "
            "'SUCCEEDED', 'ik-gen-2', 'rh-gen-2', 1, 0, 3)"
        )
        pg.execute(
            """
            INSERT INTO billing_operations (
                id, user_id, service, module, source_id, billing_round,
                pricing_snapshot_json, unit, budget_units, reserved_credits,
                funding_json, state, completed_at
            ) VALUES ('op-gen-2', 'user_1', 'character', 'image', 'gen-2', 1,
                      '{"service":"character","unit_credits":1,"version":1}',
                      'image', 1, 1, '[]', 'PENDING', NULL)
            """
        )

    with pytest.raises(HTTPException) as exc:
        _attempt_delete(dsn)
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "SCENE_LOOK_HAS_UNSETTLED_BILLING"


def test_blocks_delete_with_project_selection(scene_db: str) -> None:
    dsn = scene_db
    _reset(dsn)
    _seed_scene_look(dsn)
    with psycopg.connect(dsn, autocommit=True) as pg:
        pg.execute(
            "INSERT INTO projects (id, owner_user_id, name) VALUES "
            "('project-1', 'user_1', '自建房项目')"
        )
        # 选用记录带 NOT NULL 的源画面版本（分析 versions 表）外键。
        pg.execute(
            "INSERT INTO assets (id, project_id, kind, storage_uri, sha256, "
            "size_bytes, content_type, created_by_user_id) VALUES "
            "(%s, %s, 'reference_video', %s, %s, 1024, 'video/mp4', %s)",
            ("project-1-src", "project-1", "cos://bucket/src.mp4", "c" * 64, "user_1"),
        )
        pg.execute(
            "INSERT INTO versions (id, project_id, asset_id, kind, version_number, "
            "payload_json, created_by_user_id) VALUES "
            "(%s, %s, %s, 'analysis', 1, '{}', %s)",
            ("project-1-srcver", "project-1", "project-1-src", "user_1"),
        )
        pg.execute(
            "INSERT INTO character_reference_selections (id, project_id, "
            "source_frame_version_id, character_version_id, selected_by) VALUES "
            "('sel-1', 'project-1', 'project-1-srcver', 'version-1', 'user_1')"
        )

    with pytest.raises(HTTPException) as exc:
        _attempt_delete(dsn)
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "SCENE_LOOK_IN_USE"
    assert "自建房项目" in exc.value.detail["message"]
    # 拒绝即无部分删除：选用记录与场景域数据全部原样保留。
    with psycopg.connect(dsn) as pg:
        counts = pg.execute(
            "SELECT "
            "(SELECT count(*) FROM character_reference_selections WHERE id='sel-1'),"
            "(SELECT count(*) FROM character_personas WHERE id='persona-1')"
        ).fetchone()
    assert tuple(counts) == (1, 1)
