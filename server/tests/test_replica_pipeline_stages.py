"""M3 五阶段执行器：状态迁移、子任务观察、失败出口与子步骤机.

验收基线（docs/复刻全自动链路实施任务清单-2026-11-11.md M3）：
- 每阶段成功推进与确定性失败出口（retryable 透传）；
- 导入完成即入队拆解并一次迁移（不空转一轮）；
- 文案桥接：original_script 直用 / 空文案 no_narration；
- 首帧子步骤机：源帧抽取→开场帧选定→候选→质检首位自动确认（AUTO_PIPELINE）；
- 编译冲突不自动代答：转失败出口。

域原语（导入/拆解/源帧/首帧/编译）全部桩化——本套件测的是**编排驱动逻辑**，
域内行为由各域自己的套件覆盖。真库只承载 pipeline 状态机（advance 的条件
UPDATE 与转换表校验是 M2 已验证的真实 SQL）。
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
from app import replica_pipeline_stages as stages
from app.db_portable import BusinessConnection

DATABASE = "rplc_pipeline_stages_test"


def _seed_user(conn: BusinessConnection, user_id: str, *, role: str = "customer") -> None:
    conn.execute(
        "INSERT INTO users (id, username, display_name, role) VALUES (%s, %s, %s, %s) "
        "ON CONFLICT (id) DO NOTHING",
        (user_id, user_id, user_id, role),
    )


def _seed_pipeline(
    conn: BusinessConnection,
    *,
    status: str,
    slot: str,
    character_version_id: str | None = "char-1",
    stage_detail: dict[str, object] | None = None,
) -> str:
    _seed_user(conn, f"u-{slot}")
    pipeline_id = f"rplc-stg-{slot}"
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
            idempotency_key, next_poll_at, stage_detail
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'fp', %s, now(), %s)
        """,
        (
            pipeline_id,
            f"u-{slot}",
            f"p-{slot}",
            "douyin",
            "v1",
            "hash-1",
            status,
            rp.STAGE_INITIATED,
            character_version_id,
            f"idem-{slot}",
            None if stage_detail is None else __import__("json").dumps(stage_detail),
        ),
    )
    return pipeline_id


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
def _clear_pipelines(biz: BusinessConnection) -> None:
    """每测清场：acquire 按 next_poll_at 选最旧可领行，遗留行会抢走领取."""
    biz.execute("DELETE FROM replica_pipeline_tasks")
    # drive_pipeline_once 的总开关门（M6）：测试默认放行；开关语义由
    # test_replica_pipeline_routes 专门覆盖。
    biz.execute("INSERT INTO runtime_settings (id) VALUES (1) ON CONFLICT (id) DO NOTHING")
    biz.execute("UPDATE runtime_settings SET replica_pipeline_enabled = true WHERE id = 1")


@pytest.fixture(autouse=True)
def _stub_domains(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """桩掉全部域原语；测试按需覆盖 registry 里的桩."""
    registry: dict[str, Any] = {}

    def fake_duration(conn: object, platform: str, video_id: str) -> int:
        return registry.get("duration_ms", 8000)

    def fake_enqueue_import(conn: object, **kwargs: Any) -> dict[str, Any]:
        registry["import_enqueue_kwargs"] = kwargs
        return {"id": "imp-1"}

    def fake_enqueue_analysis(conn: object, **kwargs: Any) -> tuple[dict[str, Any], bool]:
        registry["analysis_enqueue_kwargs"] = kwargs
        return {"id": "ana-1"}, True

    def fake_create_script(conn: object, **kwargs: Any) -> dict[str, Any]:
        registry["script_request"] = kwargs["request"]
        return {"id": "scr-1"}

    def fake_enqueue_source_frame(conn: object, **kwargs: Any) -> dict[str, Any]:
        registry["source_enqueue_kwargs"] = kwargs
        return {"id": "sf-1"}

    def fake_confirm_source(conn: object, **kwargs: Any) -> dict[str, Any]:
        registry["source_confirm_kwargs"] = kwargs
        return {"id": "sfs-1"}

    def fake_enqueue_first_frame(conn: object, **kwargs: Any) -> dict[str, Any]:
        registry["first_frame_enqueue_kwargs"] = kwargs
        return {"id": "ff-1"}

    def fake_confirm_first_frame(conn: object, **kwargs: Any) -> dict[str, Any]:
        registry["first_frame_confirm_kwargs"] = kwargs
        return {"id": "ffs-1"}

    def fake_compile(conn: object, **kwargs: Any) -> dict[str, Any]:
        registry["compile_request"] = kwargs["request"]
        if "compile_error" in registry:
            raise registry["compile_error"]
        return {"id": "pv-1"}

    def fake_sub_status(
        conn: object, table: str, task_id: str
    ) -> tuple[str, dict[str, Any]] | None:
        key = (table, task_id)
        value = registry.get("sub_status", {}).get(key)
        if value is None:
            return None
        status, row = value
        return status, row

    def fake_version_payload(conn: object, version_id: str) -> dict[str, object] | None:
        return registry.get("version_payload", {}).get(version_id)

    def fake_character_binding(conn: object, **kwargs: Any) -> Any:
        return registry.get("reference_selection")

    def fake_freeze_quote(conn: object, **kwargs: Any) -> dict[str, object]:
        return {"fingerprint": "fp-1", "quantity": 1, "durationSeconds": 8}

    def fake_choose_character(conn: object, **kwargs: Any) -> dict[str, object]:
        registry["choose_character_kwargs"] = kwargs
        return {"version_id": "mc-1"}

    def fake_create_reference(conn: object, **kwargs: Any) -> Any:
        from types import SimpleNamespace

        registry["create_reference_kwargs"] = kwargs
        return SimpleNamespace(character_version_id="char-1", id="ref-1")

    monkeypatch.setattr(stages, "_video_duration_ms", fake_duration)
    monkeypatch.setattr(stages, "enqueue_viral_import_task", fake_enqueue_import)
    monkeypatch.setattr(stages, "enqueue_analysis_task", fake_enqueue_analysis)
    monkeypatch.setattr(stages, "create_script_version", fake_create_script)
    monkeypatch.setattr(stages, "enqueue_source_frame_task", fake_enqueue_source_frame)
    monkeypatch.setattr(stages, "confirm_source_frame", fake_confirm_source)
    monkeypatch.setattr(stages, "enqueue_first_frame_task", fake_enqueue_first_frame)
    monkeypatch.setattr(stages, "confirm_first_frame", fake_confirm_first_frame)
    monkeypatch.setattr(stages, "compile_prompt_version", fake_compile)
    monkeypatch.setattr(stages, "_sub_task_status", fake_sub_status)
    monkeypatch.setattr(stages, "_version_payload", fake_version_payload)
    monkeypatch.setattr(
        stages, "current_character_reference_selection_for_generation", fake_character_binding
    )
    import app.replica_pipeline_confirm as confirm_module

    monkeypatch.setattr(confirm_module, "freeze_confirm_quote", fake_freeze_quote)
    # 桩 stages 模块属性（stages 顶部集中导入域原语，函数内不再延迟 import）。
    monkeypatch.setattr(stages, "choose_project_character_version", fake_choose_character)
    monkeypatch.setattr(stages, "create_character_reference_selection", fake_create_reference)
    return registry


def _release_for_next_round(biz: BusinessConnection, pipeline_id: str) -> None:
    """模拟 worker 的 waiting 收尾（真实路径是 rp.set_next_poll + 退避到期）.

    测试不等 30s 退避：直接置 next_poll_at=now() 并清租约。
    """
    biz.execute(
        "UPDATE replica_pipeline_tasks SET locked_by = NULL, locked_until = NULL, "
        "next_poll_at = now() WHERE id = %s",
        (pipeline_id,),
    )


def _acquire(biz: BusinessConnection, *, slot: str) -> rp.ReplicaPipelineLease:
    lease = rp.acquire_replica_pipeline(biz, worker_id="w1")
    assert lease is not None and lease.id == f"rplc-stg-{slot}"
    return lease


def test_enqueued_enqueues_import_and_advances(
    biz: BusinessConnection, _stub_domains: dict[str, Any]
) -> None:
    biz.execute("DELETE FROM replica_pipeline_tasks")
    pipeline_id = _seed_pipeline(biz, status=rp.STATUS_ENQUEUED, slot="enq")
    lease = _acquire(biz, slot="enq")
    result = stages.run_pipeline_stage(biz, lease)
    assert result.kind == "advanced"
    row = biz.execute(
        "SELECT status, import_task_id, stage FROM replica_pipeline_tasks WHERE id = %s",
        (pipeline_id,),
    ).fetchone()
    assert row is not None
    assert row["status"] == rp.STATUS_IMPORTING
    assert row["import_task_id"] == "imp-1"
    # 操作键派生幂等键（pipeline:{id}:import:{retry_no}）。
    kwargs = _stub_domains["import_enqueue_kwargs"]
    assert kwargs["idempotency_key"] == f"pipeline:{pipeline_id}:import:0"


def test_import_running_waits(biz: BusinessConnection, _stub_domains: dict[str, Any]) -> None:
    biz.execute("DELETE FROM replica_pipeline_tasks")
    _seed_pipeline(biz, status=rp.STATUS_IMPORTING, slot="impw")
    biz.execute(
        "UPDATE replica_pipeline_tasks SET import_task_id = 'imp-1' WHERE id = %s",
        ("rplc-stg-impw",),
    )
    _stub_domains["sub_status"] = {("viral_import_tasks", "imp-1"): ("RUNNING", {})}
    lease = _acquire(biz, slot="impw")
    assert stages.run_pipeline_stage(biz, lease).kind == "waiting"


def test_import_success_enqueues_analysis_same_round(
    biz: BusinessConnection, _stub_domains: dict[str, Any]
) -> None:
    biz.execute("DELETE FROM replica_pipeline_tasks")
    pipeline_id = _seed_pipeline(biz, status=rp.STATUS_IMPORTING, slot="imps")
    biz.execute(
        "UPDATE replica_pipeline_tasks SET import_task_id = 'imp-1' WHERE id = %s",
        (pipeline_id,),
    )
    _stub_domains["sub_status"] = {
        ("viral_import_tasks", "imp-1"): (
            "SUCCEEDED",
            {"source_asset_id": "asset-1", "error_code": None, "retryable": 0},
        )
    }
    lease = _acquire(biz, slot="imps")
    assert stages.run_pipeline_stage(biz, lease).kind == "advanced"
    row = biz.execute(
        "SELECT status, analysis_task_id, stage_detail FROM replica_pipeline_tasks WHERE id = %s",
        (pipeline_id,),
    ).fetchone()
    assert row is not None
    assert row["status"] == rp.STATUS_ANALYZING and row["analysis_task_id"] == "ana-1"
    detail = row["stage_detail"] if isinstance(row["stage_detail"], dict) else {}
    assert detail.get("sourceAssetId") == "asset-1"


def test_import_failure_surfaces_retryable(
    biz: BusinessConnection, _stub_domains: dict[str, Any]
) -> None:
    biz.execute("DELETE FROM replica_pipeline_tasks")
    _seed_pipeline(biz, status=rp.STATUS_IMPORTING, slot="impf")
    biz.execute(
        "UPDATE replica_pipeline_tasks SET import_task_id = 'imp-1' WHERE id = %s",
        ("rplc-stg-impf",),
    )
    _stub_domains["sub_status"] = {
        ("viral_import_tasks", "imp-1"): (
            "FAILED",
            {"error_code": "IMPORT_TIMEOUT", "error_message_redacted": "超时", "retryable": 1},
        )
    }
    lease = _acquire(biz, slot="impf")
    result = stages.run_pipeline_stage(biz, lease)
    assert result.kind == "failed"
    assert result.failure_code == "IMPORT_TIMEOUT" and result.retryable


def test_analysis_success_advances_to_script(
    biz: BusinessConnection, _stub_domains: dict[str, Any]
) -> None:
    biz.execute("DELETE FROM replica_pipeline_tasks")
    pipeline_id = _seed_pipeline(biz, status=rp.STATUS_ANALYZING, slot="ana")
    biz.execute(
        "UPDATE replica_pipeline_tasks SET analysis_task_id = 'ana-1' WHERE id = %s",
        (pipeline_id,),
    )
    # 真实契约（评审 A-r1#1）：result_version_id 是 analysis 版本；shot card
    # 通过 payload.source_analysis_version_id 反查——用真库 versions 行验证。
    import json as _json

    biz.execute(
        "INSERT INTO versions (id, project_id, asset_id, kind, version_number, "
        "payload_json, created_by_user_id) VALUES (%s, %s, NULL, 'analysis', 1, %s, %s) "
        "ON CONFLICT (id) DO NOTHING",
        ("ana-ver-1", "p-ana", _json.dumps({"analysis": {"original_script": "文案"}}), "u-ana"),
    )
    biz.execute(
        "INSERT INTO versions (id, project_id, asset_id, kind, version_number, "
        "payload_json, created_by_user_id) VALUES (%s, %s, NULL, 'shot_card', 2, %s, %s) "
        "ON CONFLICT (id) DO NOTHING",
        (
            "shot-1",
            "p-ana",
            _json.dumps({"source_analysis_version_id": "ana-ver-1", "shots": []}),
            "u-ana",
        ),
    )
    _stub_domains["sub_status"] = {
        ("analysis_tasks", "ana-1"): (
            "SUCCEEDED",
            {"result_version_id": "ana-ver-1", "error_code": None, "retryable": 0},
        )
    }
    lease = _acquire(biz, slot="ana")
    assert stages.run_pipeline_stage(biz, lease).kind == "advanced"
    row = biz.execute(
        "SELECT status, stage_detail FROM replica_pipeline_tasks WHERE id = %s",
        (pipeline_id,),
    ).fetchone()
    assert row is not None and row["status"] == rp.STATUS_SCRIPT_VERSIONING
    detail = row["stage_detail"] if isinstance(row["stage_detail"], dict) else {}
    assert detail.get("analysisVersionId") == "ana-ver-1"
    assert detail.get("shotCardVersionId") == "shot-1"


def test_script_bridge_uses_original_script(
    biz: BusinessConnection, _stub_domains: dict[str, Any]
) -> None:
    biz.execute("DELETE FROM replica_pipeline_tasks")
    pipeline_id = _seed_pipeline(
        biz,
        status=rp.STATUS_SCRIPT_VERSIONING,
        slot="scr",
        stage_detail={"shotCardVersionId": "shot-1", "analysisVersionId": "ana-ver-1"},
    )
    _stub_domains["version_payload"] = {
        "ana-ver-1": {"analysis": {"original_script": "  口播文案  "}}
    }
    lease = _acquire(biz, slot="scr")
    assert stages.run_pipeline_stage(biz, lease).kind == "advanced"
    request = _stub_domains["script_request"]
    assert request.source == "original" and request.text == "口播文案"
    row = biz.execute(
        "SELECT status, script_version_id FROM replica_pipeline_tasks WHERE id = %s",
        (pipeline_id,),
    ).fetchone()
    assert row is not None
    assert row["status"] == rp.STATUS_FIRST_FRAMING and row["script_version_id"] == "scr-1"


def test_script_bridge_empty_copy_uses_no_narration(
    biz: BusinessConnection, _stub_domains: dict[str, Any]
) -> None:
    biz.execute("DELETE FROM replica_pipeline_tasks")
    _seed_pipeline(
        biz,
        status=rp.STATUS_SCRIPT_VERSIONING,
        slot="scr0",
        stage_detail={"shotCardVersionId": "shot-1", "analysisVersionId": "ana-ver-1"},
    )
    _stub_domains["version_payload"] = {"ana-ver-1": {"analysis": {"original_script": ""}}}
    lease = _acquire(biz, slot="scr0")
    assert stages.run_pipeline_stage(biz, lease).kind == "advanced"
    assert _stub_domains["script_request"].source == "no_narration"


def _seed_first_framing_done_stages(
    biz: BusinessConnection,
    _stub_domains: dict[str, Any],
    *,
    slot: str,
) -> str:
    """跑到 FIRST_FRAMING 并完成源帧段：返回 pipeline id（候选待入队）."""
    pipeline_id = _seed_pipeline(
        biz,
        status=rp.STATUS_FIRST_FRAMING,
        slot=slot,
        stage_detail={
            "sourceAssetId": "asset-1",
            "shotCardVersionId": "shot-1",
            "analysisVersionId": "ana-ver-1",
        },
    )
    _stub_domains["sub_status"] = {
        ("source_frame_tasks", "sf-1"): (
            "SUCCEEDED",
            {"result_version_id": "sfc-1", "error_code": None, "retryable": 0},
        )
    }
    _stub_domains["version_payload"] = {
        "sfc-1": {
            "candidates": [
                {"asset_id": "sfb", "timestamp_seconds": 2.0},
                {"asset_id": "sfa", "timestamp_seconds": 0.5},
            ]
        }
    }
    return pipeline_id


def test_first_framing_source_stage_picks_opening_frame(
    biz: BusinessConnection, _stub_domains: dict[str, Any]
) -> None:
    biz.execute("DELETE FROM replica_pipeline_tasks")
    pipeline_id = _seed_first_framing_done_stages(biz, _stub_domains, slot="ff1")
    # 第一轮：入队源帧任务（waiting）。
    lease = _acquire(biz, slot="ff1")
    assert stages.run_pipeline_stage(biz, lease).kind == "waiting"
    _release_for_next_round(biz, pipeline_id)
    # 第二轮：源帧完成 → 选开场帧（timestamp 最小者 sfa）→ 入队候选（waiting）。
    lease = _acquire(biz, slot="ff1")
    result = stages.run_pipeline_stage(biz, lease)
    assert result.kind == "waiting", result
    kwargs = _stub_domains["source_confirm_kwargs"]
    assert kwargs["source_frame_asset_id"] == "sfa"
    assert _stub_domains["first_frame_enqueue_kwargs"]["quantity"] == 3
    row = biz.execute(
        "SELECT stage_detail FROM replica_pipeline_tasks WHERE id = %s", (pipeline_id,)
    ).fetchone()
    assert row is not None
    detail = row["stage_detail"] if isinstance(row["stage_detail"], dict) else {}
    first_frame = detail.get("firstFrame", {})
    assert first_frame.get("substage") == "candidates"
    assert first_frame.get("candidateTaskId") == "ff-1"


def test_first_framing_auto_confirms_quality_passed_candidate(
    biz: BusinessConnection, _stub_domains: dict[str, Any]
) -> None:
    biz.execute("DELETE FROM replica_pipeline_tasks")
    pipeline_id = _seed_first_framing_done_stages(biz, _stub_domains, slot="ff2")
    lease = _acquire(biz, slot="ff2")
    stages.run_pipeline_stage(biz, lease)  # 入队源帧
    _release_for_next_round(biz, pipeline_id)
    lease = _acquire(biz, slot="ff2")
    stages.run_pipeline_stage(biz, lease)  # 源帧完成 → 入队候选
    _release_for_next_round(biz, pipeline_id)
    _stub_domains["sub_status"][("first_frame_tasks", "ff-1")] = (
        "SUCCEEDED",
        {"result_version_id": "ffc-1", "error_code": None, "retryable": 0},
    )
    _stub_domains["version_payload"]["ffc-1"] = {
        "candidates": [
            {"asset_id": "bad", "quality": {"passed": False}},
            {"asset_id": "good", "quality": {"passed": True}},
        ]
    }
    lease = _acquire(biz, slot="ff2")
    assert stages.run_pipeline_stage(biz, lease).kind == "advanced"
    kwargs = _stub_domains["first_frame_confirm_kwargs"]
    # 质检通过首位胜出；来源如实记录 AUTO_PIPELINE（r2#2）。
    assert kwargs["first_frame_asset_id"] == "good"
    assert kwargs["review_mode"] == "AUTO_PIPELINE"
    # 有质检通过证据：不得使用 override（评审 A-r2#2）。
    assert kwargs["quality_override"] is False
    row = biz.execute(
        "SELECT status FROM replica_pipeline_tasks WHERE id = %s", (pipeline_id,)
    ).fetchone()
    assert row is not None and row["status"] == rp.STATUS_COMPOSING


def test_first_framing_all_quality_rejected_fails(
    biz: BusinessConnection, _stub_domains: dict[str, Any]
) -> None:
    biz.execute("DELETE FROM replica_pipeline_tasks")
    pipeline_id = _seed_first_framing_done_stages(biz, _stub_domains, slot="ff3")
    lease = _acquire(biz, slot="ff3")
    stages.run_pipeline_stage(biz, lease)
    _release_for_next_round(biz, pipeline_id)
    lease = _acquire(biz, slot="ff3")
    stages.run_pipeline_stage(biz, lease)
    _release_for_next_round(biz, pipeline_id)
    _stub_domains["sub_status"][("first_frame_tasks", "ff-1")] = (
        "SUCCEEDED",
        {"result_version_id": "ffc-1", "error_code": None, "retryable": 0},
    )
    _stub_domains["version_payload"]["ffc-1"] = {
        "candidates": [{"asset_id": "bad", "quality": {"passed": False}}]
    }
    lease = _acquire(biz, slot="ff3")
    result = stages.run_pipeline_stage(biz, lease)
    assert result.kind == "failed" and result.failure_code == "FIRST_FRAME_QUALITY_REJECTED"


def test_first_framing_without_quality_takes_first_candidate(
    biz: BusinessConnection, _stub_domains: dict[str, Any]
) -> None:
    """质检不可用（真实链路 quality 恒 None）：确定性取首位并如实记录
    qualityAvailable=false（评审 A-r1#3）."""
    biz.execute("DELETE FROM replica_pipeline_tasks")
    pipeline_id = _seed_first_framing_done_stages(biz, _stub_domains, slot="ff4")
    lease = _acquire(biz, slot="ff4")
    stages.run_pipeline_stage(biz, lease)
    _release_for_next_round(biz, pipeline_id)
    lease = _acquire(biz, slot="ff4")
    stages.run_pipeline_stage(biz, lease)
    _release_for_next_round(biz, pipeline_id)
    _stub_domains["sub_status"][("first_frame_tasks", "ff-1")] = (
        "SUCCEEDED",
        {"result_version_id": "ffc-1", "error_code": None, "retryable": 0},
    )
    _stub_domains["version_payload"]["ffc-1"] = {
        "candidates": [{"asset_id": "first"}, {"asset_id": "second"}]
    }
    lease = _acquire(biz, slot="ff4")
    assert stages.run_pipeline_stage(biz, lease).kind == "advanced"
    kwargs = _stub_domains["first_frame_confirm_kwargs"]
    assert kwargs["first_frame_asset_id"] == "first"
    # 无质检证据：显式 quality_override（生成域既有授权语义）。
    assert kwargs["quality_override"] is True
    row = biz.execute(
        "SELECT stage_detail FROM replica_pipeline_tasks WHERE id = %s", (pipeline_id,)
    ).fetchone()
    assert row is not None
    detail = row["stage_detail"] if isinstance(row["stage_detail"], dict) else {}
    first_frame = detail.get("firstFrame", {})
    assert first_frame.get("qualityAvailable") is False


def test_compose_advances_to_await_confirm(
    biz: BusinessConnection, _stub_domains: dict[str, Any]
) -> None:
    biz.execute("DELETE FROM replica_pipeline_tasks")
    pipeline_id = _seed_pipeline(
        biz,
        status=rp.STATUS_COMPOSING,
        slot="cmp",
        stage_detail={
            "shotCardVersionId": "shot-1",
            "scriptVersionId": "scr-1",
            "firstFrame": {"firstFrameAssetId": "ff-asset"},
        },
    )
    lease = _acquire(biz, slot="cmp")
    assert stages.run_pipeline_stage(biz, lease).kind == "advanced"
    request = _stub_domains["compile_request"]
    assert request.resolution == "768P" and request.timeline_policy == "preserve"
    assert request.output_duration_seconds == 8
    row = biz.execute(
        "SELECT status, prompt_version_id FROM replica_pipeline_tasks WHERE id = %s",
        (pipeline_id,),
    ).fetchone()
    assert row is not None
    assert row["status"] == rp.STATUS_AWAIT_CONFIRM and row["prompt_version_id"] == "pv-1"


def test_compose_conflict_fails_with_domain_code(
    biz: BusinessConnection, _stub_domains: dict[str, Any]
) -> None:
    from fastapi import HTTPException

    biz.execute("DELETE FROM replica_pipeline_tasks")
    _seed_pipeline(
        biz,
        status=rp.STATUS_COMPOSING,
        slot="cmpf",
        stage_detail={
            "shotCardVersionId": "shot-1",
            "scriptVersionId": "scr-1",
            "firstFrame": {"firstFrameAssetId": "ff-asset"},
        },
    )
    _stub_domains["compile_error"] = HTTPException(
        409, detail={"code": "TIMELINE_CONFIRMATION_REQUIRED", "message": "x"}
    )
    lease = _acquire(biz, slot="cmpf")
    result = stages.run_pipeline_stage(biz, lease)
    assert result.kind == "failed"
    assert result.failure_code == "TIMELINE_CONFIRMATION_REQUIRED"


def test_needs_review_without_batch_fails_loudly(biz: BusinessConnection) -> None:
    """观察态由 M5 分派：无批次引用时显式失败而非静默空转."""
    pipeline_id = _seed_pipeline(biz, status=rp.STATUS_NEEDS_REVIEW, slot="obs")
    assert pipeline_id == "rplc-stg-obs"
    lease = _acquire(biz, slot="obs")
    result = stages.run_pipeline_stage(biz, lease)
    assert result.kind == "failed" and result.failure_code == "GENERATION_BATCH_MISSING"


def test_first_frame_retry_resets_json_breakpoint_in_real_pg(
    biz: BusinessConnection, _stub_domains: dict[str, Any]
) -> None:
    """首帧失败重试的 jsonb 减键 SQL 必须是合法表达式（评审 A-r3#6）.

    真库执行 drive_pipeline_once 的重试分支：回退 firstFrame 的
    candidateTaskId/sourceFrameTaskId 断点并原子递增 retry_no——运算符优先级
    写错时该 UPDATE 会在 PG 上报 42725，此用例直接暴露。
    """
    pipeline_id = _seed_pipeline(
        biz,
        status=rp.STATUS_FIRST_FRAMING,
        slot="ffretry",
        stage_detail={
            "sourceAssetId": "asset-1",
            "shotCardVersionId": "shot-1",
            "analysisVersionId": "ana-ver-1",
            "firstFrame": {
                "substage": "candidates",
                "sourceFrameSelectionVersionId": "sfs-1",
                "candidateTaskId": "ff-1",
                "sourceFrameTaskId": "sf-1",
            },
        },
    )
    # 候选任务确定性失败（retryable）：handler 返回 failed → drive 进重试分支。
    _stub_domains["sub_status"] = {
        ("first_frame_tasks", "ff-1"): (
            "FAILED",
            {
                "error_code": "IMAGE_PROVIDER_TIMEOUT",
                "error_message_redacted": "超时",
                "retryable": 1,
            },
        )
    }
    lease = _acquire(biz, slot="ffretry")
    outcome = stages.drive_pipeline_once(biz, lease)
    assert outcome == "retrying"
    row = biz.execute(
        "SELECT stage_retry_no, stage_detail FROM replica_pipeline_tasks WHERE id = %s",
        (pipeline_id,),
    ).fetchone()
    assert row is not None
    assert int(row["stage_retry_no"]) == 1
    detail = row["stage_detail"] if isinstance(row["stage_detail"], dict) else {}
    first_frame = detail.get("firstFrame", {})
    assert "candidateTaskId" not in first_frame
    assert "sourceFrameTaskId" not in first_frame
    assert first_frame.get("substage") == "candidates"


def test_character_selection_http_error_lands_failed(
    biz: BusinessConnection, _stub_domains: dict[str, Any]
) -> None:
    """参考选择过期的域 HTTPException 不得穿透 driver（评审 A-r3#7）.

    候选入口应把它转成 failed 结果；drive_pipeline_once 随后落 FAILED
    终态（「转手动」必须真正落库）。
    """
    from fastapi import HTTPException

    def raising_binding(conn: object, **kwargs: Any) -> Any:
        raise HTTPException(409, detail={"code": "REFERENCE_SELECTION_STALE", "message": "x"})

    pipeline_id = _seed_pipeline(
        biz,
        status=rp.STATUS_FIRST_FRAMING,
        slot="ffhttp",
        stage_detail={
            "sourceAssetId": "asset-1",
            "shotCardVersionId": "shot-1",
            "analysisVersionId": "ana-ver-1",
            "firstFrame": {
                "substage": "candidates",
                "sourceFrameSelectionVersionId": "sfs-1",
            },
        },
    )
    _stub_domains["reference_selection"] = None
    # 直接替换 stages 模块属性为抛 HTTPException 的实现。
    import app.replica_pipeline_stages as stages_module

    original = stages_module.current_character_reference_selection_for_generation
    stages_module.current_character_reference_selection_for_generation = raising_binding
    try:
        lease = _acquire(biz, slot="ffhttp")
        outcome = stages.drive_pipeline_once(biz, lease)
    finally:
        stages_module.current_character_reference_selection_for_generation = original
    assert outcome == "failed"
    row = biz.execute(
        "SELECT status FROM replica_pipeline_tasks WHERE id = %s", (pipeline_id,)
    ).fetchone()
    assert row is not None and row["status"] == rp.STATUS_FAILED


def test_first_frame_retry_with_column_and_history(
    biz: BusinessConnection, _stub_domains: dict[str, Any]
) -> None:
    """首帧重试的真实危险组合（评审 M8-r4#1）：first_frame_task_id 列非空
    （触发 history 追加）且 stage_detail 无 history 键（首次重试）——含参
    jsonb 表达式与断点表达式必须合并正确，参数绑定数量一致。"""
    pipeline_id = _seed_pipeline(
        biz,
        status=rp.STATUS_FIRST_FRAMING,
        slot="ffhist",
        stage_detail={
            "sourceAssetId": "asset-1",
            "shotCardVersionId": "shot-1",
            "analysisVersionId": "ana-ver-1",
            "firstFrame": {
                "substage": "candidates",
                "sourceFrameSelectionVersionId": "sfs-1",
                "candidateTaskId": "ff-1",
            },
        },
    )
    biz.execute(
        "UPDATE replica_pipeline_tasks SET first_frame_task_id = 'ff-1' WHERE id = %s",
        (pipeline_id,),
    )
    _stub_domains["sub_status"] = {
        ("first_frame_tasks", "ff-1"): (
            "FAILED",
            {
                "error_code": "IMAGE_PROVIDER_TIMEOUT",
                "error_message_redacted": "超时",
                "retryable": 1,
            },
        )
    }
    lease = _acquire(biz, slot="ffhist")
    assert stages.drive_pipeline_once(biz, lease) == "retrying"
    row = biz.execute(
        "SELECT first_frame_task_id, stage_retry_no, stage_detail FROM replica_pipeline_tasks "
        "WHERE id = %s",
        (pipeline_id,),
    ).fetchone()
    assert row is not None
    assert row["first_frame_task_id"] is None
    assert int(row["stage_retry_no"]) == 1
    detail = row["stage_detail"] if isinstance(row["stage_detail"], dict) else {}
    assert detail.get("history") == ["ff-1"]
    assert detail.get("sourceAssetId") == "asset-1"  # 原字段不丢
    first_frame = detail.get("firstFrame", {})
    assert "candidateTaskId" not in first_frame
    assert first_frame.get("sourceFrameSelectionVersionId") == "sfs-1"


@pytest.mark.parametrize("status", [rp.STATUS_ENQUEUED, rp.STATUS_IMPORTING])
def test_import_rejection_settles_pipeline(
    biz: BusinessConnection, monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    """确定性导入拒绝必须释放任务槽，恢复缺失导入引用时也走相同出口。"""
    from app.viral_import import ViralImportError

    pipeline_id = _seed_pipeline(biz, status=status, slot="import-rejected")

    def reject(*args: Any, **kwargs: Any) -> None:
        raise ViralImportError(
            422, "VIRAL_IMPORT_DURATION_OUT_OF_RANGE", "素材时长不支持。", retryable=False
        )

    monkeypatch.setattr(stages, "enqueue_viral_import_task", reject)
    lease = _acquire(biz, slot="import-rejected")
    assert stages.drive_pipeline_once(biz, lease) == "failed"
    row = biz.execute(
        "SELECT status, stage_detail FROM replica_pipeline_tasks WHERE id = %s", (pipeline_id,)
    ).fetchone()
    assert row is not None and row["status"] == rp.STATUS_FAILED
    assert row["stage_detail"]["failureCode"] == "VIRAL_IMPORT_DURATION_OUT_OF_RANGE"


@pytest.mark.parametrize(
    "status", [rp.STATUS_ENQUEUED, rp.STATUS_ANALYZING, rp.STATUS_FIRST_FRAMING]
)
def test_expired_pipeline_does_not_start_another_stage(
    biz: BusinessConnection, monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    """生产驱动须先检查期限，不能先进入可能创建付费子任务的阶段。"""
    pipeline_id = _seed_pipeline(biz, status=status, slot="expired-drive")
    biz.execute(
        "UPDATE replica_pipeline_tasks SET created_at = now() - interval '25 hours' WHERE id = %s",
        (pipeline_id,),
    )
    lease = _acquire(biz, slot="expired-drive")

    def forbidden(*args: Any, **kwargs: Any) -> stages.StageResult:
        pytest.fail("超期任务不应执行下一阶段")

    monkeypatch.setattr(stages, "run_pipeline_stage", forbidden)
    assert stages.drive_pipeline_once(biz, lease) == "failed"
    row = biz.execute(
        "SELECT status, stage_detail FROM replica_pipeline_tasks WHERE id = %s", (pipeline_id,)
    ).fetchone()
    assert row is not None and row["status"] == rp.STATUS_FAILED
    assert row["stage_detail"]["failureCode"] == "REPLICA_PIPELINE_EXECUTION_EXPIRED"


@pytest.mark.parametrize(
    ("status", "suspended_seconds"),
    [(rp.STATUS_GENERATING, 0), (rp.STATUS_ENQUEUED, 7200)],
)
def test_deadline_preserves_post_confirm_and_suspended_time(
    biz: BusinessConnection, monkeypatch: pytest.MonkeyPatch, status: str, suspended_seconds: int
) -> None:
    pipeline_id = _seed_pipeline(biz, status=status, slot="deadline-exempt")
    biz.execute(
        "UPDATE replica_pipeline_tasks SET created_at = now() - interval '25 hours', "
        "suspended_seconds_total = %s WHERE id = %s",
        (suspended_seconds, pipeline_id),
    )
    lease = _acquire(biz, slot="deadline-exempt")
    monkeypatch.setattr(stages, "run_pipeline_stage", lambda *_: stages.WAITING)
    assert stages.drive_pipeline_once(biz, lease) == "waiting"
    row = biz.execute(
        "SELECT status FROM replica_pipeline_tasks WHERE id = %s", (pipeline_id,)
    ).fetchone()
    assert row is not None and row["status"] == status


@pytest.mark.parametrize("child_terminal", ["SUCCEEDED", "FAILED"])
def test_expired_pipeline_drains_without_new_work_or_losing_reason(
    biz: BusinessConnection, monkeypatch: pytest.MonkeyPatch, child_terminal: str
) -> None:
    biz.execute("DELETE FROM viral_import_tasks WHERE id='drain-import'")
    pipeline_id = _seed_pipeline(biz, status=rp.STATUS_IMPORTING, slot="drain-expired")
    biz.execute(
        "INSERT INTO viral_import_tasks (id,owner_user_id,project_id,platform,video_id,purpose,"
        "idempotency_key,request_hash,request_json,status) "
        "VALUES ('drain-import','u-drain-expired','p-drain-expired','douyin','v1',"
        "'replica','drain-import','h','{}','RUNNING')"
    )
    biz.execute(
        "UPDATE replica_pipeline_tasks SET created_at=now()-interval '25 hours', "
        "import_task_id='drain-import' WHERE id=%s",
        (pipeline_id,),
    )

    def forbidden(*args: Any, **kwargs: Any) -> None:
        pytest.fail("收口期间不得创建新子任务")

    for name in ("enqueue_viral_import_task", "enqueue_analysis_task", "enqueue_first_frame_task"):
        monkeypatch.setattr(stages, name, forbidden)
    assert stages.drive_pipeline_once(biz, _acquire(biz, slot="drain-expired")) == "failed"
    row = biz.execute(
        "SELECT status,stage_detail FROM replica_pipeline_tasks WHERE id=%s", (pipeline_id,)
    ).fetchone()
    assert row is not None and row["status"] == rp.STATUS_DRAINING
    original_detail = row["stage_detail"]
    assert original_detail["failureCode"] == "REPLICA_PIPELINE_EXECUTION_EXPIRED"
    assert stages.drive_pipeline_once(biz, _acquire(biz, slot="drain-expired")) == "waiting"
    biz.execute(
        "UPDATE viral_import_tasks SET status=%s WHERE id='drain-import'", (child_terminal,)
    )
    biz.execute("UPDATE replica_pipeline_tasks SET next_poll_at=now() WHERE id=%s", (pipeline_id,))
    assert stages.drive_pipeline_once(biz, _acquire(biz, slot="drain-expired")) == "advanced"
    row = biz.execute(
        "SELECT status,stage_detail,finished_at FROM replica_pipeline_tasks WHERE id=%s",
        (pipeline_id,),
    ).fetchone()
    assert row is not None and row["status"] == rp.STATUS_FAILED
    assert row["stage_detail"] == original_detail and row["finished_at"] is not None
