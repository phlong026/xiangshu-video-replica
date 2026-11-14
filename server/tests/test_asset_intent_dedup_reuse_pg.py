"""E2E 缺陷 #1 回归锁定：upload-intent 去重复用分支的 project 绑定语义。

生产 2026-11-14 曾出现：同一视频先经非项目通道上传（``assets.project_id``
为 NULL），后续项目通道 intent 命中去重时把不匹配的 asset 带进了拆解链路，
拆解任务创建报 ``ASSET_PROJECT_MISMATCH``，需要人工 UPDATE 兜底。

本套件在真 PG 上锁定 ``reuse_owned_completed_upload`` 的三条绑定语义：

- 跨项目复用：新建 asset 绑定本次 ``project_id``，源 asset 归属不变；
- 同项目复用：复用 ``asset_id``，且 ``metadata_json.upload_purpose`` 跟随
  本次 intent（先「提取文案」后「复刻」时用途不残留——complete 幂等重试
  按它校验时长门禁、判定自动拆解）；
- ``project_id`` 为 NULL 的孤儿 asset 永不进入去重候选（JOIN 归属项目）。
"""

from __future__ import annotations

import json
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

from app.auth import CurrentUser
from app.db_portable import BusinessConnection
from app.media import reuse_owned_completed_upload
from app.storage import FakeStorageAdapter

DATABASE = "asset_intent_dedup_test"


@pytest.fixture(scope="module", autouse=True)
def _require_pg() -> None:
    require_pg_or_explicit_skip()


@pytest.fixture(scope="module")
def dedup_dsn() -> Iterator[str]:
    dsn = create_test_database(DATABASE)
    upgrade_test_database_to_head(dsn)
    try:
        yield dsn
    finally:
        drop_test_database(DATABASE)


def _actor(user_id: str) -> CurrentUser:
    return CurrentUser(id=user_id, username=user_id, display_name="去重客户", role="customer")


def _seed_owner(dsn: str, user_id: str, project_ids: list[str]) -> None:
    with psycopg.connect(dsn, autocommit=True) as raw:
        raw.execute(
            "INSERT INTO users (id, username, display_name, role, is_active) "
            "VALUES (%s, %s, '去重客户', 'customer', 1) "
            "ON CONFLICT (id) DO NOTHING",
            (user_id, user_id),
        )
        for pid in project_ids:
            raw.execute(
                "INSERT INTO projects (id, owner_user_id, name) "
                "VALUES (%s, %s, %s) ON CONFLICT (id) DO NOTHING",
                (pid, user_id, f"项目-{pid[-6:]}"),
            )


def _seed_asset(
    dsn: str,
    *,
    asset_id: str,
    project_id: str | None,
    sha256: str,
    size_bytes: int,
    storage_uri: str,
    metadata: dict[str, object],
    owner_user_id: str,
) -> None:
    with psycopg.connect(dsn, autocommit=True) as raw:
        raw.execute(
            "INSERT INTO assets (id, project_id, kind, storage_uri, sha256, size_bytes, "
            "content_type, metadata_json, created_by_user_id) "
            "VALUES (%s, %s, 'reference_video', %s, %s, %s, 'video/mp4', %s, %s)",
            (
                asset_id,
                project_id,
                storage_uri,
                sha256,
                size_bytes,
                json.dumps(metadata),
                owner_user_id,
            ),
        )


def _fetch_asset(dsn: str, asset_id: str) -> dict[str, object] | None:
    with psycopg.connect(dsn, autocommit=True) as raw:
        row = raw.execute(
            "SELECT project_id, metadata_json FROM assets WHERE id = %s",
            (asset_id,),
        ).fetchone()
    if row is None:
        return None
    return {
        "project_id": row[0],
        "metadata": json.loads(str(row[1] or "{}")),
    }


def test_cross_project_reuse_inserts_a_new_bound_asset(dedup_dsn: str) -> None:
    suffix = uuid.uuid4().hex[:12]
    user = f"dedup-user-{suffix}"
    source_project = f"dedup-src-{suffix}"
    target_project = f"dedup-dst-{suffix}"
    asset_id = f"dedup-asset-{suffix}"
    sha = "b" * 64
    storage_uri = f"fake://dedup-bucket/{asset_id}.mp4"
    _seed_owner(dedup_dsn, user, [source_project, target_project])
    _seed_asset(
        dedup_dsn,
        asset_id=asset_id,
        project_id=source_project,
        sha256=sha,
        size_bytes=4096,
        storage_uri=storage_uri,
        metadata={"upload_status": "COMPLETED", "upload_purpose": "replica"},
        owner_user_id=user,
    )
    storage = FakeStorageAdapter(provider="fake", bucket="dedup-bucket")
    storage.put_object(f"{asset_id}.mp4", b"x" * 4096, content_type="video/mp4")

    with psycopg.connect(dedup_dsn) as raw:
        reused = reuse_owned_completed_upload(
            BusinessConnection.postgres(raw),
            actor=_actor(user),
            storage=storage,
            project_id=target_project,
            sha256=sha,
            size_bytes=4096,
            purpose="replica",
        )

    assert reused is not None
    assert reused.asset_id != asset_id, "跨项目复用必须新建 asset，不得复用源行"
    assert reused.project_id == target_project
    source = _fetch_asset(dedup_dsn, asset_id)
    assert source is not None
    assert source["project_id"] == source_project, "源 asset 归属不得被改写"
    target = _fetch_asset(dedup_dsn, str(reused.asset_id))
    assert target is not None
    assert target["project_id"] == target_project


def test_same_project_reuse_updates_upload_purpose(dedup_dsn: str) -> None:
    suffix = uuid.uuid4().hex[:12]
    user = f"dedup-user-{suffix}"
    project = f"dedup-proj-{suffix}"
    asset_id = f"dedup-asset-{suffix}"
    sha = "c" * 64
    storage_uri = f"fake://dedup-bucket/{asset_id}.mp4"
    _seed_owner(dedup_dsn, user, [project])
    _seed_asset(
        dedup_dsn,
        asset_id=asset_id,
        project_id=project,
        sha256=sha,
        size_bytes=4096,
        storage_uri=storage_uri,
        # 先以「提取文案」上传：purpose 残留 extract
        metadata={"upload_status": "COMPLETED", "upload_purpose": "extract"},
        owner_user_id=user,
    )
    storage = FakeStorageAdapter(provider="fake", bucket="dedup-bucket")
    storage.put_object(f"{asset_id}.mp4", b"x" * 4096, content_type="video/mp4")
    with psycopg.connect(dedup_dsn) as raw:
        conn = BusinessConnection.postgres(raw)

        reused = reuse_owned_completed_upload(
            conn,
            actor=_actor(user),
            storage=storage,
            project_id=project,
            sha256=sha,
            size_bytes=4096,
            purpose="replica",
        )

    assert reused is not None
    assert reused.asset_id == asset_id, "同项目复用直接复用源 asset"
    after = _fetch_asset(dedup_dsn, asset_id)
    assert after is not None
    assert after["metadata"]["upload_purpose"] == "replica", (
        "同项目复用时 upload_purpose 必须跟随本次 intent，"
        "否则 complete 幂等重试按旧用途校验时长门禁与自动拆解"
    )
    with psycopg.connect(dedup_dsn, autocommit=True) as raw:
        count = raw.execute("SELECT count(*) FROM assets WHERE sha256 = %s", (sha,)).fetchone()
    assert int(count[0]) == 1, "同项目复用不得新建重复 asset 行"


def test_null_project_asset_is_never_a_dedup_candidate(dedup_dsn: str) -> None:
    suffix = uuid.uuid4().hex[:12]
    user = f"dedup-user-{suffix}"
    project = f"dedup-proj-{suffix}"
    asset_id = f"dedup-orphan-{suffix}"
    sha = "d" * 64
    storage_uri = f"fake://dedup-bucket/{asset_id}.mp4"
    _seed_owner(dedup_dsn, user, [project])
    _seed_asset(
        dedup_dsn,
        asset_id=asset_id,
        project_id=None,
        sha256=sha,
        size_bytes=4096,
        storage_uri=storage_uri,
        metadata={"upload_status": "COMPLETED"},
        owner_user_id=user,
    )
    storage = FakeStorageAdapter(provider="fake", bucket="dedup-bucket")
    storage.put_object(f"{asset_id}.mp4", b"x" * 4096, content_type="video/mp4")

    reused = reuse_owned_completed_upload(
        BusinessConnection.postgres(psycopg.connect(dedup_dsn)),
        actor=_actor(user),
        storage=storage,
        project_id=project,
        sha256=sha,
        size_bytes=4096,
        purpose="replica",
    )

    assert reused is None, "project_id 为 NULL 的孤儿 asset 不进去重候选"
    orphan = _fetch_asset(dedup_dsn, asset_id)
    assert orphan is not None
    assert orphan["project_id"] is None, "孤儿 asset 不被隐式绑定"
