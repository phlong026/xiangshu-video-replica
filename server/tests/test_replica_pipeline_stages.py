"""M3 五阶段执行器：状态迁移、子任务观察、失败出口与子步骤机.

验收基线（docs/复刻全自动链路实施任务清单-2026-11-11.md M3）：
- 每阶段成功推进与确定性失败出口（retryable 透传）；
- 导入完成即入队拆解并一次迁移（不空转一轮）；
- 文案桥接：original_script 直用 / 空文案 no_narration；
- 首帧子步骤机：源帧抽取→开场帧选定→候选→质检首位自动确认（AUTO_PIPELINE）；
- 编译冲突不自动代答：转失败出口。

大部分用例桩化域原语以测编排驱动逻辑；小数秒回归用真实编译、报价、
建批次和预占验证跨域时长一致，外部媒体与供应商入口不执行。
"""

from __future__ import annotations

import json
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
from app.replica_pipeline_confirm import freeze_confirm_quote as real_freeze_confirm_quote
from app.replica_pipeline_stages import _version_payload as real_version_payload
from app.replica_pipeline_stages import _video_duration_ms as real_video_duration_ms

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
        payloads = registry.get("version_payload", {})
        if version_id in payloads:
            return payloads[version_id]
        if version_id == "shot-1":
            return {"duration_seconds": registry.get("duration_ms", 8000) / 1000}
        return None

    def fake_character_binding(conn: object, **kwargs: Any) -> Any:
        return registry.get("reference_selection")

    def fake_freeze_quote(conn: object, **kwargs: Any) -> dict[str, object]:
        if "quote_error" in registry:
            raise registry["quote_error"]
        return {"fingerprint": "fp-1", "quantity": 1, "durationSeconds": kwargs["duration_seconds"]}

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


def _seed_uploaded_pipeline(
    biz: BusinessConnection,
    *,
    slot: str,
    status: str,
    seconds: float = 8.123456,
    credits: int = 100,
) -> tuple[str, str]:
    asset_id = f"upload-source-{slot}"
    pipeline_id = _seed_pipeline(
        biz,
        status=status,
        slot=slot,
        stage_detail={"sourceAssetId": asset_id, "sourceDurationMs": seconds * 1000},
    )
    biz.execute(
        "UPDATE replica_pipeline_tasks SET platform = 'upload', video_id = %s WHERE id = %s",
        (asset_id, pipeline_id),
    )
    biz.execute(
        "INSERT INTO assets (id, project_id, kind, storage_uri, sha256, size_bytes, "
        "content_type, metadata_json, created_by_user_id) VALUES "
        "(%s, %s, 'reference_video', %s, 'test-hash', 100, 'video/mp4', %s, %s) "
        "ON CONFLICT (id) DO UPDATE SET sha256 = 'test-hash', size_bytes = 100, "
        "metadata_json = EXCLUDED.metadata_json",
        (
            asset_id,
            f"p-{slot}",
            f"local://test/{asset_id}.mp4",
            json.dumps({"duration_seconds": seconds, "upload_status": "COMPLETE"}),
            f"u-{slot}",
        ),
    )
    biz.execute(
        "INSERT INTO billing_tariffs (service, enabled, unit_credits, unit_rounding) "
        "VALUES ('analysis', true, 2, 'ceil') ON CONFLICT (service) "
        "DO UPDATE SET enabled = true, unit_credits = 2"
    )
    biz.execute(
        "INSERT INTO wallets (user_id, available_credits, reserved_credits) VALUES (%s, %s, 0) "
        "ON CONFLICT (user_id) DO UPDATE SET available_credits = EXCLUDED.available_credits, "
        "reserved_credits = 0",
        (f"u-{slot}", credits),
    )
    return pipeline_id, asset_id


def test_uploaded_worker_skips_import_and_recovers_completed_analysis_without_recharging(
    biz: BusinessConnection, _stub_domains: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.analysis import enqueue_analysis_task

    slot = "upload-resume"
    pipeline_id, asset_id = _seed_uploaded_pipeline(biz, slot=slot, status=rp.STATUS_ENQUEUED)
    monkeypatch.setattr(stages, "enqueue_analysis_task", enqueue_analysis_task)
    assert stages.drive_pipeline_once(biz, _acquire(biz, slot=slot)) == "advanced"
    row = biz.execute(
        "SELECT status, import_task_id FROM replica_pipeline_tasks WHERE id = %s", (pipeline_id,)
    ).fetchone()
    assert (
        row is not None and row["status"] == rp.STATUS_IMPORTING and row["import_task_id"] is None
    )
    assert "import_enqueue_kwargs" not in _stub_domains

    assert stages.drive_pipeline_once(biz, _acquire(biz, slot=slot)) == "advanced"
    task = biz.execute(
        "SELECT * FROM analysis_tasks WHERE project_id = %s", (f"p-{slot}",)
    ).fetchone()
    assert task is not None and task["asset_id"] == asset_id
    assert float(task["duration_seconds"]) == 8.123456
    assert task["request_id"] == f"pipeline:{pipeline_id}:analyze:0"
    assert (
        biz.execute(
            "SELECT count(*) FROM viral_import_tasks WHERE project_id = %s", (f"p-{slot}",)
        ).fetchone()[0]
        == 0
    )

    # 子任务已经成功而父引用丢失：恢复原收据，不重新创建付费分析。
    biz.execute("UPDATE analysis_tasks SET status = 'SUCCEEDED' WHERE id = %s", (task["id"],))
    biz.execute(
        "UPDATE replica_pipeline_tasks SET analysis_task_id = NULL WHERE id = %s", (pipeline_id,)
    )
    biz.execute("UPDATE assets SET metadata_json = '{}' WHERE id = %s", (asset_id,))
    assert stages.drive_pipeline_once(biz, _acquire(biz, slot=slot)) == "waiting"
    row = biz.execute(
        "SELECT analysis_task_id, stage_detail FROM replica_pipeline_tasks WHERE id = %s",
        (pipeline_id,),
    ).fetchone()
    assert row is not None and row["analysis_task_id"] == task["id"]
    assert row["stage_detail"]["sourceAssetId"] == asset_id
    assert (
        biz.execute(
            "SELECT count(*) FROM analysis_tasks WHERE project_id = %s", (f"p-{slot}",)
        ).fetchone()[0]
        == 1
    )
    assert (
        biz.execute(
            "SELECT count(*) FROM billing_operations WHERE user_id = %s AND service = 'analysis'",
            (f"u-{slot}",),
        ).fetchone()[0]
        == 1
    )
    wallet = biz.execute(
        "SELECT available_credits, reserved_credits FROM wallets WHERE user_id = %s", (f"u-{slot}",)
    ).fetchone()
    assert (
        wallet is not None and wallet["available_credits"] == 98 and wallet["reserved_credits"] == 2
    )


@pytest.mark.parametrize("status", [rp.STATUS_IMPORTING, rp.STATUS_ANALYZING])
@pytest.mark.parametrize(
    ("case", "code"),
    [
        ("missing", "ASSET_NOT_FOUND"),
        ("pending", "REFERENCE_VIDEO_NOT_READY"),
        ("unknown", "ANALYSIS_DURATION_UNAVAILABLE"),
        ("short", "SOURCE_TOO_SHORT_FOR_GENERATION"),
        ("long", "SOURCE_TOO_LONG_FOR_GENERATION"),
        ("long-tier-disabled", "LONG_DURATION_TIER_DISABLED"),
    ],
)
def test_uploaded_worker_rechecks_source_before_first_analysis_and_retry(
    biz: BusinessConnection,
    _stub_domains: dict[str, Any],
    status: str,
    case: str,
    code: str,
) -> None:
    slot = f"upload-check-{status}-{case}"
    pipeline_id, asset_id = _seed_uploaded_pipeline(biz, slot=slot, status=status)
    if case == "missing":
        biz.execute("DELETE FROM assets WHERE id = %s", (asset_id,))
    elif case == "pending":
        biz.execute("UPDATE assets SET sha256 = '', size_bytes = 0 WHERE id = %s", (asset_id,))
    else:
        duration = {"short": 3.9995, "long": 30.001, "long-tier-disabled": 15.001}.get(case)
        biz.execute(
            "UPDATE assets SET metadata_json = %s WHERE id = %s",
            (json.dumps({} if duration is None else {"duration_seconds": duration}), asset_id),
        )
        if case == "long-tier-disabled":
            biz.execute(
                "UPDATE billing_tariffs SET enabled = false WHERE service = 'video_768p_long'"
            )
    assert stages.drive_pipeline_once(biz, _acquire(biz, slot=slot)) == "failed"
    row = biz.execute(
        "SELECT status, stage_detail FROM replica_pipeline_tasks WHERE id = %s", (pipeline_id,)
    ).fetchone()
    assert row is not None and row["status"] == rp.STATUS_FAILED
    assert row["stage_detail"]["failureCode"] == code
    assert "analysis_enqueue_kwargs" not in _stub_domains


def test_uploaded_analysis_credit_rejection_rolls_back_unfunded_child(
    biz: BusinessConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.analysis import enqueue_analysis_task

    slot = "upload-no-credit"
    pipeline_id, _asset_id = _seed_uploaded_pipeline(
        biz, slot=slot, status=rp.STATUS_IMPORTING, credits=0
    )
    monkeypatch.setattr(stages, "enqueue_analysis_task", enqueue_analysis_task)
    assert stages.drive_pipeline_once(biz, _acquire(biz, slot=slot)) == "failed"
    row = biz.execute(
        "SELECT status, stage_detail FROM replica_pipeline_tasks WHERE id = %s", (pipeline_id,)
    ).fetchone()
    assert row is not None and row["status"] == rp.STATUS_FAILED
    assert row["stage_detail"]["failureCode"] == "INSUFFICIENT_CREDITS"
    assert (
        biz.execute(
            "SELECT count(*) FROM analysis_tasks WHERE project_id = %s", (f"p-{slot}",)
        ).fetchone()[0]
        == 0
    )
    assert (
        biz.execute(
            "SELECT count(*) FROM billing_operations WHERE user_id = %s", (f"u-{slot}",)
        ).fetchone()[0]
        == 0
    )


def test_uploaded_analysis_explicit_retry_uses_next_operation_key(
    biz: BusinessConnection, _stub_domains: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.analysis import enqueue_analysis_task

    slot = "upload-retry"
    pipeline_id, asset_id = _seed_uploaded_pipeline(biz, slot=slot, status=rp.STATUS_IMPORTING)
    monkeypatch.setattr(stages, "enqueue_analysis_task", enqueue_analysis_task)
    assert stages.drive_pipeline_once(biz, _acquire(biz, slot=slot)) == "advanced"
    task = biz.execute(
        "SELECT * FROM analysis_tasks WHERE project_id = %s", (f"p-{slot}",)
    ).fetchone()
    assert task is not None
    biz.execute("UPDATE analysis_tasks SET status = 'FAILED' WHERE id = %s", (task["id"],))
    _stub_domains["sub_status"] = {
        ("analysis_tasks", str(task["id"])): (
            "FAILED",
            {"error_code": "ANALYSIS_TIMEOUT", "error_message_redacted": "超时", "retryable": 1},
        )
    }
    assert stages.drive_pipeline_once(biz, _acquire(biz, slot=slot)) == "retrying"
    _release_for_next_round(biz, pipeline_id)
    assert stages.drive_pipeline_once(biz, _acquire(biz, slot=slot)) == "waiting"
    tasks = biz.execute(
        "SELECT asset_id, request_id FROM analysis_tasks WHERE project_id = %s "
        "ORDER BY created_at, id",
        (f"p-{slot}",),
    ).fetchall()
    assert len(tasks) == 2 and all(task["asset_id"] == asset_id for task in tasks)
    assert {task["request_id"] for task in tasks} == {
        f"pipeline:{pipeline_id}:analyze:0",
        f"pipeline:{pipeline_id}:analyze:1",
    }


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


def test_resume_consumes_analysis_completed_during_suspension(
    biz: BusinessConnection, _stub_domains: dict[str, Any]
) -> None:
    """挂起期间子任务完成 → 重开恢复 → 编排消费产物断点续跑（M6b #3）.

    验收口径（方案 §5.3/§9）：挂起不清空已落库产物；恢复后按子任务引用的
    result_version_id 继续推进，不产生孤儿引用，也不重复付费。
    """
    import json as _json

    biz.execute("DELETE FROM replica_pipeline_tasks")
    pipeline_id = _seed_pipeline(biz, status=rp.STATUS_ANALYZING, slot="susp-ana")
    biz.execute(
        "UPDATE replica_pipeline_tasks SET analysis_task_id = 'ana-susp',"
        " suspended_at = now() - interval '5 minutes' WHERE id = %s",
        (pipeline_id,),
    )
    # 子任务在挂起窗口内完成并落库版本（产物已在版本链中）。
    biz.execute(
        "INSERT INTO versions (id, project_id, asset_id, kind, version_number, "
        "payload_json, created_by_user_id) VALUES (%s, %s, NULL, 'analysis', 1, %s, %s) "
        "ON CONFLICT (id) DO NOTHING",
        (
            "ana-susp-ver",
            "p-susp-ana",
            _json.dumps({"analysis": {"original_script": "挂起期间完成"}}),
            "u-susp-ana",
        ),
    )
    biz.execute(
        "INSERT INTO versions (id, project_id, asset_id, kind, version_number, "
        "payload_json, created_by_user_id) VALUES (%s, %s, NULL, 'shot_card', 2, %s, %s) "
        "ON CONFLICT (id) DO NOTHING",
        (
            "shot-susp",
            "p-susp-ana",
            _json.dumps({"source_analysis_version_id": "ana-susp-ver", "shots": []}),
            "u-susp-ana",
        ),
    )
    # 开关重开：清挂起屏障并重设轮询（累计挂起时长）。
    rp.resume_from_suspension(biz, pipeline_id)
    biz.commit()
    _stub_domains["sub_status"] = {
        ("analysis_tasks", "ana-susp"): (
            "SUCCEEDED",
            {"result_version_id": "ana-susp-ver", "error_code": None, "retryable": 0},
        )
    }
    lease = _acquire(biz, slot="susp-ana")
    assert stages.run_pipeline_stage(biz, lease).kind == "advanced"
    row = biz.execute(
        "SELECT status, suspended_at, stage_detail FROM replica_pipeline_tasks WHERE id = %s",
        (pipeline_id,),
    ).fetchone()
    assert row is not None
    assert row["status"] == rp.STATUS_SCRIPT_VERSIONING
    assert row["suspended_at"] is None
    detail = row["stage_detail"] if isinstance(row["stage_detail"], dict) else {}
    assert detail.get("analysisVersionId") == "ana-susp-ver"
    assert detail.get("shotCardVersionId") == "shot-susp"


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


@pytest.mark.parametrize("seconds", [4, 8, 15, 16, 30])
def test_compose_advances_to_await_confirm(
    biz: BusinessConnection, _stub_domains: dict[str, Any], seconds: int
) -> None:
    _stub_domains["duration_ms"] = seconds * 1000
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
    assert request.output_duration_seconds == seconds
    row = biz.execute(
        "SELECT status, prompt_version_id, quote_snapshot "
        "FROM replica_pipeline_tasks WHERE id = %s",
        (pipeline_id,),
    ).fetchone()
    assert row is not None
    assert row["status"] == rp.STATUS_AWAIT_CONFIRM and row["prompt_version_id"] == "pv-1"
    assert row["quote_snapshot"]["durationSeconds"] == seconds


@pytest.mark.parametrize(
    ("source_seconds", "output_seconds", "uploaded"),
    [
        (4.0, 4, False),
        (15.0, 15, False),
        (15.001, 16, False),
        (16.1, 17, False),
        (16.5, 17, False),
        (29.4, 30, False),
        (30.0, 30, False),
        pytest.param(16.1, 17, True, id="upload-metadata-missing"),
    ],
)
def test_fractional_timeline_compiles_quotes_and_queues_same_duration(
    biz: BusinessConnection,
    _stub_domains: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    source_seconds: float,
    output_seconds: int,
    uploaded: bool,
) -> None:
    """真编译器不得触发压缩确认；报价、任务与预占共同使用向上取整后的秒数。"""
    from cryptography.fernet import Fernet

    from app import generation
    from app import replica_pipeline_confirm as confirm
    from app.analysis import (
        FakeGemini,
        analyze_video,
        create_analysis_version,
        create_shot_card_version,
        insert_version,
    )
    from app.auth import CurrentUser

    slot = f"real-{source_seconds}" + ("-upload" if uploaded else "")
    project_id, user_id = f"p-{slot}", f"u-{slot}"
    source_id, frame_id = f"source-{slot}", f"frame-{slot}"
    pipeline_id = _seed_pipeline(
        biz,
        status=rp.STATUS_FIRST_FRAMING if uploaded else rp.STATUS_COMPOSING,
        slot=slot,
    )
    actor = CurrentUser(id=user_id, username=user_id, display_name="测试", role="customer")
    for asset_id, kind, content_type in (
        (source_id, "reference_video", "video/mp4"),
        (frame_id, "first_frame", "image/png"),
    ):
        biz.execute(
            "INSERT INTO assets (id, project_id, kind, storage_uri, sha256, size_bytes, "
            "content_type, created_by_user_id) VALUES (%s, %s, %s, %s, 'test-hash', 10, %s, %s)",
            (asset_id, project_id, kind, f"local://test/{asset_id}", content_type, user_id),
        )
    biz.execute(
        "UPDATE assets SET metadata_json = %s WHERE id = %s",
        (json.dumps({"duration_seconds": source_seconds}), source_id),
    )
    analysis_result = analyze_video(
        video_uri="fake", video_duration_seconds=source_seconds, provider=FakeGemini()
    )
    analysis = create_analysis_version(
        biz,
        project_id=project_id,
        asset_id=source_id,
        asset_uri="fake",
        created_by_user_id=user_id,
        result=analysis_result,
    )
    shots = create_shot_card_version(
        biz,
        analysis_version=analysis,
        created_by_user_id=user_id,
        shots=analysis_result.analysis.shots,
    )
    if uploaded:
        # 分析和分镜已落库后，上游资产元数据缺失也不能改写冻结时间轴。
        biz.execute("UPDATE assets SET metadata_json = '{}' WHERE id = %s", (source_id,))
        biz.execute(
            "UPDATE replica_pipeline_tasks SET platform = 'upload', video_id = %s WHERE id = %s",
            (source_id, pipeline_id),
        )
        monkeypatch.setattr(stages, "_video_duration_ms", real_video_duration_ms)

    def version(kind: str, payload: dict[str, Any]) -> Any:
        return insert_version(
            biz,
            project_id=project_id,
            asset_id=frame_id,
            kind=kind,
            created_by_user_id=user_id,
            payload=payload,
        )

    source = version("source_frame_selection", {"timestamp_seconds": 0})
    candidates = version(
        "first_frame_candidates",
        {
            "source_frame_selection_version_id": source["id"],
            "review_mode": "HUMAN_CONFIRMATION",
            "candidates": [{"asset_id": frame_id}],
        },
    )
    version(
        "first_frame_selection",
        {
            "first_frame_candidates_version_id": candidates["id"],
            "first_frame_asset_id": frame_id,
            "review_mode": "HUMAN_CONFIRMATION",
            "reviewed_by_user_id": user_id,
        },
    )
    script = generation.create_script_version(
        biz,
        project_id=project_id,
        actor=actor,
        request=generation.ScriptRequest(
            source="custom", text="今天带你看庭院", shot_card_version_id=str(shots["id"])
        ),
    )
    biz.execute(
        "UPDATE replica_pipeline_tasks SET stage_detail = %s WHERE id = %s",
        (
            json.dumps(
                {
                    "shotCardVersionId": shots["id"],
                    "scriptVersionId": script["id"],
                    "firstFrame": {
                        "substage": "candidates",
                        "sourceFrameSelectionVersionId": source["id"],
                    }
                    if uploaded
                    else {"firstFrameAssetId": frame_id},
                }
            ),
            pipeline_id,
        ),
    )
    monkeypatch.setattr(stages, "_version_payload", real_version_payload)
    # 目录时长即使被刷新为 8 秒，也不能覆盖本次拆解的不可变媒体时间轴。
    _stub_domains["duration_ms"] = 8000
    monkeypatch.setattr(stages, "compile_prompt_version", generation.compile_prompt_version)
    monkeypatch.setattr(confirm, "freeze_confirm_quote", real_freeze_confirm_quote)
    cipher = Fernet(Fernet.generate_key())
    monkeypatch.setattr("app.quote_contracts._cipher", lambda: cipher)
    for service in ("video_768p", "video_768p_long"):
        biz.execute(
            "INSERT INTO billing_tariffs (service, enabled, unit_credits, unit_rounding) "
            "VALUES (%s, true, 2, 'ceil') ON CONFLICT (service) DO UPDATE "
            "SET enabled = true, unit_credits = 2",
            (service,),
        )
    biz.execute(
        "INSERT INTO wallets (user_id, available_credits, reserved_credits) VALUES (%s, 1000, 0)",
        (user_id,),
    )
    if uploaded:
        assert stages.drive_pipeline_once(biz, _acquire(biz, slot=slot)) == "waiting"
        assert "first_frame_enqueue_kwargs" in _stub_domains
        _stub_domains["sub_status"] = {
            ("first_frame_tasks", "ff-1"): ("SUCCEEDED", {"result_version_id": candidates["id"]})
        }
        _release_for_next_round(biz, pipeline_id)
        assert stages.drive_pipeline_once(biz, _acquire(biz, slot=slot)) == "advanced"
    lease = _acquire(biz, slot=slot)
    assert stages.drive_pipeline_once(biz, lease) == "advanced"
    row = biz.execute(
        "SELECT * FROM replica_pipeline_tasks WHERE id = %s", (pipeline_id,)
    ).fetchone()
    assert row is not None and row["status"] == rp.STATUS_AWAIT_CONFIRM
    assert row["locked_by"] is None and row["locked_until"] is None
    prompt = biz.execute(
        "SELECT payload_json FROM versions WHERE id = %s", (row["prompt_version_id"],)
    ).fetchone()
    assert prompt is not None
    payload = json.loads(str(prompt["payload_json"]))
    assert payload["source_duration_seconds"] == source_seconds
    assert payload["output_duration_seconds"] == output_seconds
    assert payload["timeline_policy"] == "preserve" and payload["timeline_scale_factor"] >= 1
    snapshot = row["quote_snapshot"]
    assert snapshot["durationSeconds"] == output_seconds
    assert snapshot["maxCredits"] == output_seconds * 2

    # 仅绕过外部存储/凭据预检；不运行 worker，真实建批次、报价校验和账务预占照常执行。
    monkeypatch.setattr(generation, "metaso_h3_provider_from_settings", lambda conn: object())
    monkeypatch.setattr(generation, "require_cos_first_frame_storage", lambda *args, **kwargs: None)
    quote = generation.generation_price_quote(
        biz, resolution="768P", duration_seconds=output_seconds, quantity=1, user_id=user_id
    )
    submitted = confirm.confirm_pipeline(
        biz,
        pipeline_id=pipeline_id,
        owner_user_id=user_id,
        quote_token=quote.quote_token,
        max_credits=quote.max_credits,
        expected_fingerprint=snapshot["fingerprint"],
    )
    tasks = biz.execute(
        "SELECT billed_seconds, prompt_snapshot_json FROM generation_tasks WHERE batch_id = %s",
        (submitted["batchId"],),
    ).fetchall()
    assert len(tasks) == 1 and tasks[0]["billed_seconds"] == output_seconds
    assert (
        json.loads(str(tasks[0]["prompt_snapshot_json"]))["output_duration_seconds"]
        == output_seconds
    )
    wallet = biz.execute(
        "SELECT available_credits, reserved_credits FROM wallets WHERE user_id = %s", (user_id,)
    ).fetchone()
    assert wallet is not None
    assert wallet["available_credits"] == 1000 - output_seconds * 2
    assert wallet["reserved_credits"] == output_seconds * 2


@pytest.mark.parametrize(
    ("payload", "failure_code"),
    [
        (None, "SHOT_CARD_TIMELINE_INVALID"),
        ({}, "SHOT_CARD_TIMELINE_INVALID"),
        ({"duration_seconds": 3.999}, "SOURCE_TOO_SHORT_FOR_GENERATION"),
        ({"duration_seconds": 30.001}, "SOURCE_TOO_LONG_FOR_GENERATION"),
        ({"duration_seconds": float("inf")}, "SOURCE_TOO_SHORT_FOR_GENERATION"),
    ],
)
def test_actual_timeline_rejected_before_compile_and_quote(
    biz: BusinessConnection,
    _stub_domains: dict[str, Any],
    payload: dict[str, object] | None,
    failure_code: str,
) -> None:
    _seed_pipeline(
        biz,
        status=rp.STATUS_COMPOSING,
        slot="timeline-invalid",
        stage_detail={
            "shotCardVersionId": "shot-1",
            "scriptVersionId": "scr-1",
            "firstFrame": {"firstFrameAssetId": "ff-asset"},
        },
    )
    _stub_domains["version_payload"] = {"shot-1": payload}
    result = stages.run_pipeline_stage(biz, _acquire(biz, slot="timeline-invalid"))
    assert result.kind == "failed" and result.failure_code == failure_code
    assert "compile_request" not in _stub_domains


@pytest.mark.parametrize(
    ("payload", "failure_code"),
    [
        ({}, "SHOT_CARD_TIMELINE_INVALID"),
        ({"duration_seconds": 3.999}, "SOURCE_TOO_SHORT_FOR_GENERATION"),
        ({"duration_seconds": 30.001}, "SOURCE_TOO_LONG_FOR_GENERATION"),
        ({"shots": [{"end_time": 15.001}]}, "LONG_DURATION_TIER_DISABLED"),
    ],
)
def test_actual_timeline_and_long_tier_checked_before_paid_first_frame(
    biz: BusinessConnection,
    _stub_domains: dict[str, Any],
    payload: dict[str, object],
    failure_code: str,
) -> None:
    _seed_pipeline(
        biz,
        status=rp.STATUS_FIRST_FRAMING,
        slot="duration-preflight",
        stage_detail={
            "shotCardVersionId": "shot-1",
            "firstFrame": {"substage": "candidates", "sourceFrameSelectionVersionId": "sfs-1"},
        },
    )
    _stub_domains["version_payload"] = {"shot-1": payload}
    biz.execute("UPDATE billing_tariffs SET enabled = false WHERE service = 'video_768p_long'")
    result = stages.run_pipeline_stage(biz, _acquire(biz, slot="duration-preflight"))
    assert result.kind == "failed" and result.failure_code == failure_code
    assert "first_frame_enqueue_kwargs" not in _stub_domains


@pytest.mark.parametrize(
    ("error_key", "code"),
    [
        ("compile_error", "TIMELINE_CONFIRMATION_REQUIRED"),
        ("quote_error", "LONG_DURATION_TIER_DISABLED"),
    ],
)
def test_compose_conflict_fails_with_domain_code(
    biz: BusinessConnection, _stub_domains: dict[str, Any], error_key: str, code: str
) -> None:
    from fastapi import HTTPException

    _stub_domains["duration_ms"] = 30_000
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
    _stub_domains[error_key] = HTTPException(422, detail={"code": code, "message": "x"})
    lease = _acquire(biz, slot="cmpf")
    result = stages.run_pipeline_stage(biz, lease)
    assert result.kind == "failed"
    assert result.failure_code == code


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
