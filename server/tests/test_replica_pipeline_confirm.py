"""M4 确认点资金闸门 + M5 生成域映射/接收入口：真库状态机与校验路径.

域原语（报价/建批次/锁提示词）桩化——测的是闸门自己的校验与状态迁移：
- 指纹三方一致（快照/用户所见/当前输入）不齐时拒绝并要求重新核价（r1#1）；
- 非待确认 / 已撤销 / uncertain 冻结一律拒绝；
- cancel 与 confirm 竞争同一把行锁（只有 AWAIT_CONFIRM 可迁）；
- 72h 过期扫描恒定计时；
- 观察映射：可交付覆盖 → DELIVERED；收口未覆盖 → SETTLED；未收口 → 等待；
- 接收部分成果要求已收口且可交付集合非空。
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import psycopg
import pytest
from pg_test_kit import (
    create_test_database,
    drop_test_database,
    require_pg_or_explicit_skip,
    upgrade_test_database_to_head,
)

from app import replica_pipeline as rp
from app import replica_pipeline_confirm as confirm
from app import replica_pipeline_generation as generation
from app.db_portable import BusinessConnection

DATABASE = "rplc_pipeline_confirm_test"

_DETAIL = {
    "shotCardVersionId": "shot-1",
    "scriptVersionId": "scr-1",
    "promptVersionId": "pv-1",
    "firstFrame": {"firstFrameAssetId": "ff-1"},
}
_SNAPSHOT = {
    "schemaVersion": 1,
    "resolution": "768P",
    "durationSeconds": 8,
    "quantity": 1,
    "priceTier": "4-15s",
    "unitCredits": 2.0,
    "estimatedCredits": 16,
    "maxCredits": 16,
    "estimatedPriceFen": 800,
    "fingerprint": confirm.quote_fingerprint(
        prompt_version_id="pv-1",
        shot_card_version_id="shot-1",
        script_version_id="scr-1",
        first_frame_asset_id="ff-1",
        resolution="768P",
        duration_seconds=8,
        quantity=1,
    ),
    "frozenAt": "2026-11-12T00:00:00+00:00",
}


def _seed_user(conn: BusinessConnection, user_id: str) -> None:
    conn.execute(
        "INSERT INTO users (id, username, display_name, role) VALUES (%s, %s, %s, 'customer') "
        "ON CONFLICT (id) DO NOTHING",
        (user_id, user_id, user_id),
    )


def _seed_pipeline(
    conn: BusinessConnection,
    *,
    status: str,
    slot: str,
    detail: dict[str, object] | None = None,
    snapshot: dict[str, object] | None = None,
) -> str:
    import json

    _seed_user(conn, f"u-{slot}")
    conn.execute(
        "INSERT INTO projects (id, owner_user_id, name) VALUES (%s, %s, %s) "
        "ON CONFLICT (id) DO NOTHING",
        (f"p-{slot}", f"u-{slot}", "全自动复刻"),
    )
    conn.execute(
        """
        INSERT INTO replica_pipeline_tasks (
            id, owner_user_id, project_id, platform, video_id, request_hash,
            status, stage, character_version_id, project_fingerprint,
            idempotency_key, next_poll_at, stage_detail, quote_snapshot
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'char-1', 'fp', %s, now(), %s, %s)
        """,
        (
            f"rplc-cfm-{slot}",
            f"u-{slot}",
            f"p-{slot}",
            "douyin",
            "v1",
            "hash-1",
            status,
            rp.STAGE_CONFIRM,
            f"idem-{slot}",
            json.dumps(detail or _DETAIL),
            json.dumps(snapshot or _SNAPSHOT),
        ),
    )
    return f"rplc-cfm-{slot}"


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


@pytest.fixture(autouse=True)
def _clear(biz: BusinessConnection) -> None:
    biz.execute("DELETE FROM replica_pipeline_tasks")
    # 批次/任务 seed 用固定 id（batch-obs/gt-N）：不清理会让上一个测试的
    # 行靠 ON CONFLICT 存活并污染观察结果。
    biz.execute("DELETE FROM generation_tasks")
    biz.execute("DELETE FROM generation_batches")


@pytest.fixture(autouse=True)
def _stub_domains(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    registry: dict[str, Any] = {}

    def fake_batch(conn: object, **kwargs: Any) -> Any:
        registry["batch_kwargs"] = kwargs
        if "batch_error" in registry:
            raise registry["batch_error"]
        from types import SimpleNamespace

        return SimpleNamespace(id="batch-new")

    def fake_lock(conn: object, **kwargs: Any) -> dict[str, Any]:
        return {"id": "pv-locked"}

    def fake_quote(conn: object, **kwargs: Any) -> Any:
        from types import SimpleNamespace

        return SimpleNamespace(
            price_tier="4-15s",
            unit_credits=2.0,
            estimated_credits=16,
            estimated_price_fen=800,
        )

    monkeypatch.setattr("app.generation.create_generation_batch", fake_batch)
    monkeypatch.setattr("app.generation.lock_prompt_version", fake_lock)
    monkeypatch.setattr("app.generation.generation_price_quote", fake_quote)
    return registry


def test_quote_fingerprint_binds_all_render_inputs() -> None:
    kwargs = {
        "prompt_version_id": "pv-1",
        "shot_card_version_id": "shot-1",
        "script_version_id": "scr-1",
        "first_frame_asset_id": "ff-1",
        "resolution": "768P",
        "duration_seconds": 8,
        "quantity": 1,
    }
    baseline = confirm.quote_fingerprint(**kwargs)
    assert baseline == confirm.quote_fingerprint(**kwargs)
    for field, value in (
        ("prompt_version_id", "pv-2"),
        ("first_frame_asset_id", "ff-2"),
        ("duration_seconds", 9),
    ):
        changed = dict(kwargs)
        changed[field] = value
        assert confirm.quote_fingerprint(**changed) != baseline


def test_confirm_happy_path_moves_to_generating(
    biz: BusinessConnection, _stub_domains: dict[str, Any]
) -> None:
    pipeline_id = _seed_pipeline(biz, status=rp.STATUS_AWAIT_CONFIRM, slot="ok")
    result = confirm.confirm_pipeline(
        biz,
        pipeline_id=pipeline_id,
        owner_user_id="u-ok",
        quote_token="qt-1",
        max_credits=16,
        expected_fingerprint=str(_SNAPSHOT["fingerprint"]),
    )
    assert result["batchId"] == "batch-new"
    row = biz.execute(
        "SELECT status, batch_id, confirmed_at FROM replica_pipeline_tasks WHERE id = %s",
        (pipeline_id,),
    ).fetchone()
    assert row is not None
    assert row["status"] == rp.STATUS_GENERATING
    assert row["batch_id"] == "batch-new" and row["confirmed_at"] is not None


def test_confirm_rejects_fingerprint_mismatch(
    biz: BusinessConnection, _stub_domains: dict[str, Any]
) -> None:
    pipeline_id = _seed_pipeline(biz, status=rp.STATUS_AWAIT_CONFIRM, slot="fp")
    with pytest.raises(rp.ReplicaPipelineError) as excinfo:
        confirm.confirm_pipeline(
            biz,
            pipeline_id=pipeline_id,
            owner_user_id="u-fp",
            quote_token="qt-1",
            max_credits=16,
            expected_fingerprint="stale-fingerprint",
        )
    assert excinfo.value.code == "REPLICA_PIPELINE_QUOTE_STALE"


def test_confirm_rejects_over_cap(biz: BusinessConnection, _stub_domains: dict[str, Any]) -> None:
    pipeline_id = _seed_pipeline(biz, status=rp.STATUS_AWAIT_CONFIRM, slot="cap")
    with pytest.raises(rp.ReplicaPipelineError) as excinfo:
        confirm.confirm_pipeline(
            biz,
            pipeline_id=pipeline_id,
            owner_user_id="u-cap",
            quote_token="qt-1",
            max_credits=999,
            expected_fingerprint=str(_SNAPSHOT["fingerprint"]),
        )
    assert excinfo.value.code == "REPLICA_PIPELINE_QUOTE_STALE"


def test_confirm_rejects_non_awaiting_and_uncertain(
    biz: BusinessConnection, _stub_domains: dict[str, Any]
) -> None:
    pipeline_id = _seed_pipeline(biz, status=rp.STATUS_COMPOSING, slot="noawait")
    with pytest.raises(rp.ReplicaPipelineError) as excinfo:
        confirm.confirm_pipeline(
            biz,
            pipeline_id=pipeline_id,
            owner_user_id="u-noawait",
            quote_token="qt-1",
            max_credits=16,
            expected_fingerprint=str(_SNAPSHOT["fingerprint"]),
        )
    assert excinfo.value.code == "REPLICA_PIPELINE_NOT_AWAITING"
    detail = dict(_DETAIL)
    detail["uncertain"] = True
    pipeline_id = _seed_pipeline(biz, status=rp.STATUS_AWAIT_CONFIRM, slot="unc", detail=detail)
    with pytest.raises(rp.ReplicaPipelineError) as excinfo:
        confirm.confirm_pipeline(
            biz,
            pipeline_id=pipeline_id,
            owner_user_id="u-unc",
            quote_token="qt-1",
            max_credits=16,
            expected_fingerprint=str(_SNAPSHOT["fingerprint"]),
        )
    assert excinfo.value.code == "REPLICA_PIPELINE_UNCERTAIN"


def test_cancel_only_from_await_confirm(biz: BusinessConnection) -> None:
    pipeline_id = _seed_pipeline(biz, status=rp.STATUS_AWAIT_CONFIRM, slot="cnl")
    result = confirm.cancel_pipeline(biz, pipeline_id=pipeline_id, owner_user_id="u-cnl")
    assert result["status"] == rp.STATUS_CANCELLED
    row = biz.execute(
        "SELECT status, finished_at FROM replica_pipeline_tasks WHERE id = %s",
        (pipeline_id,),
    ).fetchone()
    assert row is not None and row["finished_at"] is not None
    # 再取消（已非待确认）→ 拒绝（行锁与状态裁决竞争的唯一赢家语义）。
    with pytest.raises(rp.ReplicaPipelineError):
        confirm.cancel_pipeline(biz, pipeline_id=pipeline_id, owner_user_id="u-cnl")


def test_expire_scan_hits_only_aged_awaiting(biz: BusinessConnection) -> None:
    aged = _seed_pipeline(biz, status=rp.STATUS_AWAIT_CONFIRM, slot="old")
    fresh = _seed_pipeline(biz, status=rp.STATUS_AWAIT_CONFIRM, slot="new")
    biz.execute(
        "UPDATE replica_pipeline_tasks SET updated_at = now() - interval '73 hours' WHERE id = %s",
        (aged,),
    )
    assert confirm.expire_pipelines(biz) == 1
    rows = {
        str(row["id"]): str(row["status"])
        for row in biz.execute(
            "SELECT id, status FROM replica_pipeline_tasks WHERE id = ANY(%s)",
            ([aged, fresh],),
        ).fetchall()
    }
    assert rows[aged] == rp.STATUS_EXPIRED
    assert rows[fresh] == rp.STATUS_AWAIT_CONFIRM


# ---------------------------------------------------------------- M5 观察


def _seed_batch_with_tasks(
    biz: BusinessConnection,
    pipeline_id: str,
    *,
    task_statuses: list[tuple[str, str | None]],
) -> None:
    """造批次与任务：[(status, archive_status)]；不依赖真实供应商字段."""
    batch_id = "batch-obs"
    biz.execute(
        "UPDATE replica_pipeline_tasks SET batch_id = %s WHERE id = %s",
        (batch_id, pipeline_id),
    )
    proj = biz.execute(
        "SELECT project_id, owner_user_id FROM replica_pipeline_tasks WHERE id = %s",
        (pipeline_id,),
    ).fetchone()
    assert proj is not None
    project_id = str(proj["project_id"])
    owner = str(proj["owner_user_id"])
    _seed_user(biz, owner)
    biz.execute(
        "INSERT INTO generation_batches (id, project_id, created_by_user_id, "
        "idempotency_key, request_hash, request_snapshot_json, status, "
        "created_at, updated_at) VALUES (%s, %s, %s, %s, 'h', '{}', 'RUNNING', now(), now()) "
        "ON CONFLICT (id) DO NOTHING",
        (batch_id, project_id, owner, f"batch-{batch_id}"),
    )
    for index, (status, archive) in enumerate(task_statuses):
        biz.execute(
            "INSERT INTO generation_tasks (id, batch_id, provider, model, status, "
            "archive_status, created_at, updated_at) "
            "VALUES (%s, %s, 'fake_h3', 'h3-stub', %s, %s, now(), now()) "
            "ON CONFLICT (id) DO NOTHING",
            (f"gt-{index}", batch_id, status, archive or "PENDING"),
        )


def test_observe_generation_delivers_when_direct_ready(
    biz: BusinessConnection, _stub_domains: dict[str, Any]
) -> None:
    pipeline_id = _seed_pipeline(biz, status=rp.STATUS_GENERATING, slot="gen-ok")
    _seed_batch_with_tasks(biz, pipeline_id, task_statuses=[("SUCCEEDED", "DIRECT")])
    lease = rp.acquire_replica_pipeline(biz, worker_id="w1")
    assert lease is not None
    result = generation.observe_generation(biz, lease, _row(biz, pipeline_id))
    assert result.kind == "advanced"
    row = biz.execute(
        "SELECT status FROM replica_pipeline_tasks WHERE id = %s", (pipeline_id,)
    ).fetchone()
    assert row is not None and row["status"] == rp.STATUS_DELIVERED


def test_observe_generation_partial_failure_settles(
    biz: BusinessConnection, _stub_domains: dict[str, Any]
) -> None:
    pipeline_id = _seed_pipeline(biz, status=rp.STATUS_GENERATING, slot="gen-fail")
    _seed_batch_with_tasks(biz, pipeline_id, task_statuses=[("FAILED", None)])
    lease = rp.acquire_replica_pipeline(biz, worker_id="w1")
    assert lease is not None
    result = generation.observe_generation(biz, lease, _row(biz, pipeline_id))
    assert result.kind == "advanced"
    row = biz.execute(
        "SELECT status FROM replica_pipeline_tasks WHERE id = %s", (pipeline_id,)
    ).fetchone()
    assert row is not None and row["status"] == rp.STATUS_NEEDS_REVIEW_SETTLED


def test_observe_generation_waits_while_task_open(
    biz: BusinessConnection, _stub_domains: dict[str, Any]
) -> None:
    pipeline_id = _seed_pipeline(biz, status=rp.STATUS_GENERATING, slot="gen-wait")
    _seed_batch_with_tasks(biz, pipeline_id, task_statuses=[("RUNNING", None)])
    lease = rp.acquire_replica_pipeline(biz, worker_id="w1")
    assert lease is not None
    result = generation.observe_generation(biz, lease, _row(biz, pipeline_id))
    assert result.kind == "waiting"


def test_accept_partial_requires_settled_and_deliverable(
    biz: BusinessConnection, _stub_domains: dict[str, Any]
) -> None:
    pipeline_id = _seed_pipeline(biz, status=rp.STATUS_NEEDS_REVIEW_SETTLED, slot="acc")
    _seed_batch_with_tasks(
        biz, pipeline_id, task_statuses=[("SUCCEEDED", "DIRECT"), ("FAILED", None)]
    )
    result = generation.accept_partial(biz, pipeline_id=pipeline_id, owner_user_id="u-acc")
    assert result["status"] == rp.STATUS_DELIVERED_PARTIAL
    # 非 SETTLED 拒绝。
    fresh = _seed_pipeline(biz, status=rp.STATUS_COMPOSING, slot="acc2")
    with pytest.raises(rp.ReplicaPipelineError) as excinfo:
        generation.accept_partial(biz, pipeline_id=fresh, owner_user_id="u-acc2")
    assert excinfo.value.code == "REPLICA_PIPELINE_NOT_SETTLED"


def _row(biz: BusinessConnection, pipeline_id: str) -> Any:
    return biz.execute(
        "SELECT * FROM replica_pipeline_tasks WHERE id = %s", (pipeline_id,)
    ).fetchone()


def test_regenerate_uses_paid_regeneration_primitive(
    biz: BusinessConnection, _stub_domains: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """重生成走付费重生成原语（评审 A-r2#3）：参数断言 + 轮次幂等键 + 成本快照 None."""
    from types import SimpleNamespace

    pipeline_id = _seed_pipeline(biz, status=rp.STATUS_NEEDS_REVIEW_SETTLED, slot="regen")
    _seed_batch_with_tasks(biz, pipeline_id, task_statuses=[("FAILED", None)])
    captured: dict[str, Any] = {}

    def fake_regenerate(conn: object, **kwargs: Any) -> Any:
        captured.update(kwargs)
        return SimpleNamespace(id="batch-regen")

    import app.generation as generation_module

    monkeypatch.setattr(generation_module, "regenerate_generation_batch", fake_regenerate)
    result = generation.regenerate_pipeline(
        biz,
        pipeline_id=pipeline_id,
        owner_user_id="u-regen",
        quote_token="qt-1",
        max_credits=16,
    )
    assert result["batchId"] == "batch-regen"
    request = captured["request"]
    # 成本快照不得把积分数写进人民币字段（评审 A-r3#3/A-r4）。
    assert request.estimated_cost_snapshot is None
    # 轮次幂等键：首个重生成轮次=1，同一授权重放命中同键。
    assert request.idempotency_key == f"pipeline:{pipeline_id}:regenerate:1"
    row = biz.execute(
        "SELECT status, batch_id FROM replica_pipeline_tasks WHERE id = %s", (pipeline_id,)
    ).fetchone()
    assert row is not None
    assert row["status"] == rp.STATUS_GENERATING and row["batch_id"] == "batch-regen"
