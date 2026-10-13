"""MATERIAL-UX-10 / T28 — 使用记录（生产来源与输入引用分节）PG 矩阵.

本模块钉住 ``GET /api/studio/materials/{id}/usages`` 的 T28 新语义：

- ``producer`` 节：产出该素材的任务（result_asset_id 指向它）——原「被引用
  次数」的真实含义，名称改准确；
- ``reference`` 节：把该素材当输入的任务，读冻结输入关系（generation batch
  快照的首帧/尾帧/参考清单 + oral_tasks.audio_asset_id），同一任务多路径
  引用只计一次（DISTINCT task）；
- 两节各自 owner 围栏、各自全量 total（LIMIT 20 截断仍准确）；失败/取消
  任务不过滤但逐行带 status（口径在响应模型 docstring 显式声明）。
- owner 围栏：他人素材 404；跨组织引用（他人 batch 引用我的素材）不可见。

PG 约束（沿 CW-031 / CW-078 / UX-03）：共享库 ``customer_v3_test``；模块级
``shared_suite_lock``；数据用 ``ux10-`` 前缀；绝不 TRUNCATE；CW-007 硬门。
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, cast

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pg_test_kit import (
    database_name_of,
    require_pg_or_explicit_skip,
    resolve_test_dsn,
    shared_suite_lock,
    upgrade_test_database_to_head,
)

from app.auth import CurrentUser, get_current_user, get_database
from app.customer_fence import get_business_db
from app.db_pg import close_pg_pool
from app.db_portable import BusinessConnection
from app.material_routes import router as material_router

DB_NAME_SHARED = "customer_v3_test"
UX10 = "ux10-"
EMP1 = f"{UX10}emp1"
EMP2 = f"{UX10}emp2"
UX10_LIKE = f"{UX10}%"
MATERIALS_PATH = "/api/studio/materials"


def _actor(user_id: str, role: str = "employee") -> CurrentUser:
    return CurrentUser(id=user_id, username=user_id, display_name=user_id, role=role)  # type: ignore[arg-type]


def _clean_ux10_rows(conn: psycopg.Connection) -> None:
    conn.execute(
        "DELETE FROM audit_logs WHERE actor_user_id LIKE %s OR entity_id LIKE %s",
        (UX10_LIKE, UX10_LIKE),
    )
    conn.execute(
        "DELETE FROM studio_material_preferences WHERE user_id LIKE %s OR source_id LIKE %s",
        (UX10_LIKE, UX10_LIKE),
    )
    conn.execute(
        "DELETE FROM generation_tasks WHERE id LIKE %s OR batch_id LIKE %s",
        (UX10_LIKE, UX10_LIKE),
    )
    conn.execute("DELETE FROM generation_batches WHERE id LIKE %s", (UX10_LIKE,))
    conn.execute("DELETE FROM oral_tasks WHERE id LIKE %s", (UX10_LIKE,))
    conn.execute(
        "DELETE FROM oral_avatars WHERE id LIKE %s OR owner_user_id LIKE %s",
        (UX10_LIKE, UX10_LIKE),
    )
    conn.execute(
        "DELETE FROM person_identities WHERE id LIKE %s OR owner_user_id LIKE %s",
        (UX10_LIKE, UX10_LIKE),
    )
    conn.execute(
        "DELETE FROM assets WHERE id LIKE %s OR created_by_user_id LIKE %s",
        (UX10_LIKE, UX10_LIKE),
    )
    conn.execute("DELETE FROM projects WHERE id LIKE %s", (UX10_LIKE,))
    conn.execute("DELETE FROM users WHERE id LIKE %s", (UX10_LIKE,))


@pytest.fixture(scope="module")
def pg_dsn() -> Iterator[str]:
    require_pg_or_explicit_skip()
    dsn = resolve_test_dsn()
    assert database_name_of(dsn) == DB_NAME_SHARED
    upgrade_test_database_to_head(dsn)
    with shared_suite_lock():
        with psycopg.connect(dsn, autocommit=True) as conn:
            _clean_ux10_rows(conn)
        try:
            yield dsn
        finally:
            with psycopg.connect(dsn, autocommit=True) as conn:
                _clean_ux10_rows(conn)
            close_pg_pool()


@pytest.fixture()
def pg(pg_dsn: str) -> Iterator[psycopg.Connection]:
    close_pg_pool()
    conn = psycopg.connect(pg_dsn, autocommit=True)
    _clean_ux10_rows(conn)
    with conn.cursor() as cursor:
        cursor.executemany(
            "INSERT INTO users (id, username, display_name, role) VALUES (%s, %s, %s, %s)",
            [
                (EMP1, EMP1, "UX10 Employee One", "employee"),
                (EMP2, EMP2, "UX10 Employee Two", "employee"),
            ],
        )
    try:
        yield conn
    finally:
        _clean_ux10_rows(conn)
        conn.close()
        close_pg_pool()


class _ScopedBusinessDb:
    def __init__(self, bus: BusinessConnection, actor: CurrentUser) -> None:
        self._bus = bus
        self._actor = actor

    @contextmanager
    def write(self) -> Iterator[tuple[BusinessConnection, CurrentUser]]:
        yield self._bus, self._actor


def _build_client(
    pg: psycopg.Connection, *, user_id: str = EMP1, role: str = "employee"
) -> TestClient:
    bus = BusinessConnection.postgres(pg)
    current = _actor(user_id, role)
    application = FastAPI()
    application.include_router(material_router)
    application.dependency_overrides[get_database] = lambda: bus
    application.dependency_overrides[get_current_user] = lambda: current
    application.dependency_overrides[get_business_db] = lambda: _ScopedBusinessDb(bus, current)
    return TestClient(application)


@pytest.fixture()
def client(pg: psycopg.Connection) -> TestClient:
    return _build_client(pg)


def _seed_material_asset(conn: psycopg.Connection, asset_id: str, owner: str) -> None:
    conn.execute(
        "INSERT INTO assets (id, project_id, kind, storage_uri, sha256, size_bytes, "
        "content_type, created_by_user_id, metadata_json, created_at) "
        "VALUES (%s, NULL, 'material_video', %s, %s, 4096, 'video/mp4', %s, '{}', "
        "CURRENT_TIMESTAMP)",
        (
            asset_id,
            f"local://ux10/{asset_id}.mp4",
            hashlib.sha256(asset_id.encode()).hexdigest(),
            owner,
        ),
    )


def _seed_generation_reference(
    conn: psycopg.Connection,
    owner: str,
    *,
    task_id: str,
    batch_id: str,
    asset_id: str,
    snapshot: str = "{}",
) -> None:
    conn.execute(
        "INSERT INTO generation_batches (id, created_by_user_id, idempotency_key, "
        "request_hash, request_snapshot_json, status) VALUES (%s, %s, %s, %s, %s, "
        "'SUCCEEDED') ON CONFLICT (id) DO NOTHING",
        (
            batch_id,
            owner,
            f"ux10-idem-{batch_id}",
            f"ux10-hash-{batch_id}",
            snapshot,
        ),
    )
    conn.execute(
        "INSERT INTO generation_tasks (id, batch_id, generation_mode, provider, "
        "model, status, result_asset_id) VALUES (%s, %s, 'I2V', 'video_gen', "
        "'model-x', 'SUCCEEDED', %s) ON CONFLICT (id) DO NOTHING",
        (task_id, batch_id, asset_id),
    )


def _usages(client: TestClient, asset_id: str) -> dict[str, Any]:
    response = client.get(f"{MATERIALS_PATH}/asset:{asset_id}/usages")
    assert response.status_code == 200, response.text
    return cast(dict[str, Any], response.json())


def test_unused_material_reports_empty_sections(pg: psycopg.Connection, client: TestClient) -> None:
    _seed_material_asset(pg, f"{UX10}lonely", EMP1)
    body = _usages(client, f"{UX10}lonely")
    assert body == {
        "producer": {"total": 0, "items": []},
        "reference": {"total": 0, "items": []},
    }


def test_producer_and_consumer_roles_are_listed_separately(
    pg: psycopg.Connection, client: TestClient
) -> None:
    """T28 验收 A+B：产出与消费分节；同任务多引用计一次；跨 owner 不可见。"""
    asset_id = f"{UX10}dual-role"
    _seed_material_asset(pg, asset_id, EMP1)

    # 生产者：任务产出该素材（result_asset_id）。
    _seed_generation_reference(
        pg,
        EMP1,
        task_id=f"{UX10}producer",
        batch_id=f"{UX10}producer-batch",
        asset_id=asset_id,
    )
    # 消费者任务的产出资产也必须真实存在（result_asset_id 外键）。
    _seed_material_asset(pg, f"{UX10}other-output", EMP1)
    _seed_material_asset(pg, f"{UX10}other-output-2", EMP1)
    # 消费者 A：batch 快照冻结该素材为首帧（独立创作流形状）。
    _seed_generation_reference(
        pg,
        EMP1,
        task_id=f"{UX10}consumer-first",
        batch_id=f"{UX10}consumer-first-batch",
        asset_id=f"{UX10}other-output",
        snapshot=f'{{"first_frame_asset_id": "{asset_id}"}}',
    )
    # 消费者 B：同一 batch 快照里首帧与参考清单都指向它——同任务多引用计 1。
    _seed_generation_reference(
        pg,
        EMP1,
        task_id=f"{UX10}consumer-multi",
        batch_id=f"{UX10}consumer-multi-batch",
        asset_id=f"{UX10}other-output-2",
        snapshot='{{"first_frame_asset_id": "{}", "reference_asset_ids": ["{}", "{}"]}}'.format(
            asset_id, asset_id, f"{UX10}elsewhere"
        ),
    )
    # 消费者 C：oral 音频输入（冻结直列 audio_asset_id）。
    identity_id = f"{UX10}identity"
    avatar_id = f"{UX10}avatar"
    oral_id = f"{UX10}otask"
    pg.execute(
        "INSERT INTO person_identities (id, owner_user_id, display_name, "
        "authorization_status, authorization_scope, source_quality_status, status, created_by) "
        "VALUES (%s, %s, 'UX10 口播人', 'AUTHORIZED', '[]', 'PASSED', 'ACTIVE', %s)",
        (identity_id, EMP1, EMP1),
    )
    pg.execute(
        "INSERT INTO oral_avatars (id, identity_id, owner_user_id, title, "
        "source_kind, source_asset_id, vendor_avatar_id, status) VALUES "
        "(%s, %s, %s, 'UX10 形象', 'IMAGE', %s, %s, 'READY') "
        "ON CONFLICT (id) DO NOTHING",
        (avatar_id, identity_id, EMP1, f"{UX10}avatar-src", avatar_id),
    )
    pg.execute(
        "INSERT INTO oral_tasks (id, owner_user_id, identity_id, avatar_id, mode, "
        "title, idempotency_key, estimated_cost_fen, status, audio_asset_id) "
        "VALUES (%s, %s, %s, %s, 'AUDIO', 'UX10 口播', %s, 0, 'SUCCEEDED', %s) "
        "ON CONFLICT (id) DO NOTHING",
        (oral_id, EMP1, identity_id, avatar_id, f"ux10-idem-{oral_id}", asset_id),
    )
    # 跨 owner：他人 batch 冻结了同一素材 —— 不可见。
    _seed_material_asset(pg, f"{UX10}foreign-output", EMP2)
    _seed_generation_reference(
        pg,
        EMP2,
        task_id=f"{UX10}foreign-consumer",
        batch_id=f"{UX10}foreign-batch",
        asset_id=f"{UX10}foreign-output",
        snapshot=f'{{"first_frame_asset_id": "{asset_id}"}}',
    )

    body = _usages(client, asset_id)
    producer = body["producer"]
    assert producer["total"] == 1
    assert [item["task_id"] for item in producer["items"]] == [f"{UX10}producer"]
    assert all(item["role"] == "producer" for item in producer["items"])
    reference = body["reference"]
    assert reference["total"] == 3
    ref_ids = {item["task_id"] for item in reference["items"]}
    assert ref_ids == {
        f"{UX10}consumer-first",
        f"{UX10}consumer-multi",
        oral_id,
    }
    assert all(item["role"] == "consumer" for item in reference["items"])
    # 生产者任务同时出现在消费节的边界（同任务先产出再用作输入）不会被
    # 误并入引用节：它没有冻结任何输入快照。


def test_other_users_material_not_found(pg: psycopg.Connection) -> None:
    _seed_material_asset(pg, f"{UX10}theirs", EMP2)
    client = _build_client(pg, user_id=EMP1)
    response = client.get(f"{MATERIALS_PATH}/asset:{UX10}theirs/usages")
    assert response.status_code == 404
