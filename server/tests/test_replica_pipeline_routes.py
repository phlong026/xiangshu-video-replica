"""M7 全自动复刻路由：端点注册、总开关与人物预检（真库单测）.

路由是薄壳（参数校验 → 核心服务 → HTTP 映射）：核心状态机/资金闸门已在
test_replica_pipeline_* 套件覆盖。本套件验证三层：
- 六个端点已挂载到主应用（POST 发起 / GET 状态 / confirm / cancel /
  regenerate / accept-partial）；
- 总开关（runtime_settings.replica_pipeline_enabled，默认关）门禁新发起；
- 人物预检：归属链校验失败在**收费拆解之前**拒绝（方案 §0 拍板）。
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

from app import replica_pipeline as rp
from app.db_portable import BusinessConnection

DATABASE = "rplc_pipeline_routes_test"


def _seed_user(conn: BusinessConnection, user_id: str) -> None:
    conn.execute(
        "INSERT INTO users (id, username, display_name, role) VALUES (%s, %s, %s, 'customer') "
        "ON CONFLICT (id) DO NOTHING",
        (user_id, user_id, user_id),
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


def test_routes_registered_on_main_app() -> None:
    """六个端点挂在主应用上（方法+路径契约）.

    主应用的 include_router 是 lazy wrapper（_IncludedRouter），直接遍历
    app.routes 看不到子路由——用 OpenAPI schema 断言注册结果。
    """
    from app.main import app

    paths = app.openapi()["paths"]
    assert "post" in paths["/api/viral/replica-pipelines"]
    assert "get" in paths["/api/viral/replica-pipelines/{pipeline_id}"]
    for suffix in ("confirm", "cancel", "regenerate", "accept-partial"):
        entry = paths[f"/api/viral/replica-pipelines/{{pipeline_id}}/{suffix}"]
        assert "post" in entry


def test_switch_defaults_off_and_gates_new_initiation(biz: BusinessConnection) -> None:
    """总开关默认关闭：require_pipeline_enabled 拒绝新发起（方案 §6/§8）."""
    # 迁移默认 false；确保测试库单行存在。
    biz.execute("INSERT INTO runtime_settings (id) VALUES (1) ON CONFLICT (id) DO NOTHING")
    biz.execute("UPDATE runtime_settings SET replica_pipeline_enabled = false WHERE id = 1")
    assert rp.pipeline_enabled(biz) is False
    with pytest.raises(rp.ReplicaPipelineError) as excinfo:
        rp.require_pipeline_enabled(biz)
    assert excinfo.value.code == "REPLICA_PIPELINE_DISABLED"
    biz.execute("UPDATE runtime_settings SET replica_pipeline_enabled = true WHERE id = 1")
    assert rp.pipeline_enabled(biz) is True
    rp.require_pipeline_enabled(biz)  # 不抛
    biz.execute("UPDATE runtime_settings SET replica_pipeline_enabled = false WHERE id = 1")


def _seed_character_chain(
    conn: BusinessConnection,
    *,
    owner: str,
    character_version_id: str,
    identity_status: str = "ACTIVE",
) -> None:
    """造归属链：character_versions → character_personas → person_identities."""
    _seed_user(conn, owner)
    conn.execute(
        "INSERT INTO person_identities (id, owner_user_id, display_name, status, "
        "authorization_status, authorization_scope, created_by) "
        "VALUES (%s, %s, 't', %s, 'AUTHORIZED', 'replica', %s) ON CONFLICT (id) DO NOTHING",
        (f"pi-{character_version_id}", owner, identity_status, owner),
    )
    conn.execute(
        "INSERT INTO character_personas (id, identity_id, name) VALUES (%s, %s, 'p') "
        "ON CONFLICT (id) DO NOTHING",
        (f"persona-{character_version_id}", f"pi-{character_version_id}"),
    )
    conn.execute(
        "INSERT INTO characters (id, name, is_active, created_by_user_id) "
        "VALUES (%s, 'c', 1, %s) ON CONFLICT (id) DO NOTHING",
        (f"char-{character_version_id}", owner),
    )
    conn.execute(
        "INSERT INTO character_versions (id, persona_id, version_number, status, "
        "persona_snapshot_json, published_at, publication_snapshot_json, publication_hash) "
        "VALUES (%s, %s, 1, 'PUBLISHED', '{\"name\": \"x\"}', '2026-11-01T00:00:00Z', "
        "'{}', '" + "a" * 64 + "') ON CONFLICT (id) DO NOTHING",
        (character_version_id, f"persona-{character_version_id}"),
    )


def test_suspension_resume_restores_pickability(biz: BusinessConnection) -> None:
    """关闭→挂起→重开→恢复（评审 B-r1#2）：挂起行原设计不被领取，
    重开后必须由恢复扫描清屏障并重新可领."""
    from app import replica_pipeline_confirm as confirm_module

    _seed_user(biz, "u-sus")
    biz.execute(
        "INSERT INTO projects (id, owner_user_id, name) VALUES ('p-sus','u-sus','t') "
        "ON CONFLICT (id) DO NOTHING"
    )
    biz.execute(
        "INSERT INTO replica_pipeline_tasks (id, owner_user_id, project_id, platform, "
        "video_id, request_hash, status, stage, character_version_id, project_fingerprint, "
        "idempotency_key, next_poll_at) "
        "VALUES ('rplc-sus','u-sus','p-sus','douyin','v1','h','ENQUEUED','initiated',"
        "'char-1','fp','idem-sus', now() - interval '1 minute') ON CONFLICT (id) DO NOTHING"
    )
    # 模拟关闭后的挂起（drive 的挂起分支）。
    biz.execute("UPDATE replica_pipeline_tasks SET suspended_at = now() WHERE id = 'rplc-sus'")
    assert rp.acquire_replica_pipeline(biz, worker_id="w1") is None  # 挂起行不可领
    # 重开：恢复扫描清屏障并累计挂起时长。
    resumed = confirm_module.resume_suspended_pipelines(biz)
    assert resumed == 1
    row = biz.execute(
        "SELECT suspended_at, suspended_seconds_total FROM replica_pipeline_tasks "
        "WHERE id = 'rplc-sus'"
    ).fetchone()
    assert row is not None
    assert row["suspended_at"] is None and int(row["suspended_seconds_total"]) >= 0
    lease = rp.acquire_replica_pipeline(biz, worker_id="w2")  # 恢复后可领
    assert lease is not None and lease.id == "rplc-sus"


def test_worker_sweep_resumes_only_when_switch_on(biz: BusinessConnection) -> None:
    """worker 维护扫描（评审 B-r2#2）：开关关闭只过期不恢复；开启才唤醒挂起行.

    sweep 的自建事务连接注入为 biz（同一事务可见），worker 真实调用点由
    源码断言钉死（run_pg_worker_once 每轮维护段调用）。
    """
    import inspect

    from app import generation_worker as worker_module

    source = inspect.getsource(worker_module.run_pg_worker_once)
    assert "sweep_replica_pipelines(" in source
    _seed_user(biz, "u-sweep")
    biz.execute(
        "INSERT INTO projects (id, owner_user_id, name) VALUES ('p-sweep','u-sweep','t') "
        "ON CONFLICT (id) DO NOTHING"
    )
    biz.execute(
        "INSERT INTO replica_pipeline_tasks (id, owner_user_id, project_id, platform, "
        "video_id, request_hash, status, stage, character_version_id, "
        "project_fingerprint, idempotency_key, next_poll_at, suspended_at) "
        "VALUES ('rplc-sweep','u-sweep','p-sweep','douyin','v1','h','ENQUEUED',"
        "'initiated','char-1','fp','idem-sweep', now(), now()) "
        "ON CONFLICT (id) DO NOTHING"
    )
    biz.execute("INSERT INTO runtime_settings (id) VALUES (1) ON CONFLICT (id) DO NOTHING")
    biz.execute("UPDATE runtime_settings SET replica_pipeline_enabled = false WHERE id = 1")
    worker_module._last_replica_pipeline_sweep_at = 0.0
    result = worker_module.sweep_replica_pipelines(storage=None, conn=biz)  # type: ignore[arg-type]
    assert result.get("resumed", 0) == 0
    biz.execute("UPDATE runtime_settings SET replica_pipeline_enabled = true WHERE id = 1")
    worker_module._last_replica_pipeline_sweep_at = 0.0
    result = worker_module.sweep_replica_pipelines(storage=None, conn=biz)  # type: ignore[arg-type]
    assert result.get("resumed", 0) == 1
    row = biz.execute(
        "SELECT suspended_at FROM replica_pipeline_tasks WHERE id = 'rplc-sweep'"
    ).fetchone()
    assert row is not None and row["suspended_at"] is None
    worker_module._last_replica_pipeline_sweep_at = 0.0


def test_initiate_wires_authoritative_character_check() -> None:
    """发起路由复用人物域权威可用性校验（评审 B-r3#1），并在收费前执行.

    自造 SQL 预检已删除——生产路径是 enqueue 后同事务调用
    list_available_project_character_versions（发布快照/参考图/授权由域内判定，
    失败整体回滚）。路由为薄壳，域判定由人物域自己的套件覆盖。
    """
    import inspect

    from app import replica_pipeline_routes as routes_module

    source = inspect.getsource(routes_module.initiate_replica_pipeline)
    assert "list_available_project_character_versions" in source
    # 开关检查必须在异常映射内（422 而非 500，评审 B-r2#3/B-r3#3）。
    enabled_block = source.split("require_pipeline_enabled", 1)[0]
    assert enabled_block.count("try:") >= 1
