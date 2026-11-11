"""全自动复刻编排器 M3：五阶段执行器（方案 §3.4 固定策略）.

编排不重写环节：本模块只驱动既有域的原语（导入/拆解/源帧/首帧/编译），
所有子任务入队都用操作键 ``stage_operation_key`` 派生幂等键——崩溃恢复
按键找回在途任务，确定性失败才递增业务重试序号（方案 §3.3）。

每次被领取执行一个 handler：
- ``StageResult(kind="advanced")``：已按转换表迁移到下一状态（含子任务引用回填）；
- ``StageResult(kind="waiting")``：子任务未完成/需下轮再观察（调用方 set_next_poll）；
- ``StageResult(kind="failed")``：失败出口（调用方 settle_failure，DRAINING 由
  ``_failure_exit_status`` 裁决）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import cast

from app import replica_pipeline as rp
from app.analysis import enqueue_analysis_task
from app.auth import CurrentUser, Role
from app.character_reference_matching import (
    create_character_reference_selection,
    current_character_reference_selection_for_generation,
)
from app.db_portable import BusinessConnection, BusinessRow, IntegrityConstraintError
from app.first_frames import confirm_first_frame
from app.generation import (
    PromptCompileRequest,
    ScriptRequest,
    compile_prompt_version,
    create_script_version,
)
from app.image_tasks import enqueue_first_frame_task
from app.project_character_selection import choose_project_character_version
from app.source_frames import confirm_source_frame, enqueue_source_frame_task
from app.viral_import import ViralImportRequest, enqueue_viral_import_task

# 首帧候选数量：与前端 GenerateFirstFramesRequest 默认一致（1–3 上界）。
_FIRST_FRAME_QUANTITY = 3
# 首帧模型：与前端默认一致（first_frame_routes.GenerateFirstFramesRequest）。
_FIRST_FRAME_MODEL = "gpt-image-2"


@dataclass(frozen=True)
class StageResult:
    kind: str  # "advanced" | "waiting" | "failed"
    failure_code: str = ""
    failure_message: str = ""
    retryable: bool = False


ADVANCED = StageResult(kind="advanced")
WAITING = StageResult(kind="waiting")


def _failed(code: str, message: str, *, retryable: bool = False) -> StageResult:
    return StageResult(
        kind="failed", failure_code=code, failure_message=message, retryable=retryable
    )


def _pipeline_actor(conn: BusinessConnection, owner_user_id: str) -> CurrentUser:
    """按 owner 的真实用户行构造 CurrentUser（角色/权限由域内校验兜底）."""
    row = conn.execute(
        "SELECT id, username, display_name, role FROM users WHERE id = %s",
        (owner_user_id,),
    ).fetchone()
    if row is None:
        raise rp.ReplicaPipelineError(404, "REPLICA_PIPELINE_OWNER_MISSING", "发起用户不存在。")
    return CurrentUser(
        id=str(row["id"]),
        username=str(row["username"]),
        display_name=str(row["display_name"]),
        role=cast(Role, row["role"]),
    )


def _load_row(conn: BusinessConnection, lease: rp.ReplicaPipelineLease) -> BusinessRow:
    row = conn.execute("SELECT * FROM replica_pipeline_tasks WHERE id = %s", (lease.id,)).fetchone()
    if row is None:
        raise rp.ReplicaPipelineError(404, "REPLICA_PIPELINE_NOT_FOUND", "编排任务不存在。")
    return cast(BusinessRow, row)


def _stage_detail(row: BusinessRow) -> dict[str, object]:
    raw = row["stage_detail"]
    if not raw:
        return {}
    if isinstance(raw, dict):
        return cast(dict[str, object], raw)
    return cast(dict[str, object], json.loads(str(raw)))


def _save_stage_detail(
    conn: BusinessConnection, pipeline_id: str, detail: dict[str, object]
) -> None:
    conn.execute(
        "UPDATE replica_pipeline_tasks SET stage_detail = %s, updated_at = now() WHERE id = %s",
        (json.dumps(detail, ensure_ascii=False), pipeline_id),
    )


def _video_duration_ms(conn: BusinessConnection, platform: str, video_id: str) -> int:
    row = conn.execute(
        "SELECT duration_ms FROM viral_videos WHERE platform = %s AND video_id = %s",
        (platform, video_id),
    ).fetchone()
    if row is None or row["duration_ms"] is None:
        return 0
    return int(row["duration_ms"])


def _version_payload(conn: BusinessConnection, version_id: str) -> dict[str, object] | None:
    """读版本 payload（测试可桩化；真实实现读 versions.payload_json）."""
    row = conn.execute("SELECT payload_json FROM versions WHERE id = %s", (version_id,)).fetchone()
    if row is None:
        return None
    payload = json.loads(str(row["payload_json"]))
    return cast(dict[str, object], payload) if isinstance(payload, dict) else {}


def _sub_task_status(
    conn: BusinessConnection, table: str, task_id: str
) -> tuple[str, BusinessRow] | None:
    row = conn.execute(f"SELECT * FROM {table} WHERE id = %s", (task_id,)).fetchone()
    if row is None:
        return None
    return str(row["status"]), cast(BusinessRow, row)


def run_pipeline_stage(conn: BusinessConnection, lease: rp.ReplicaPipelineLease) -> StageResult:
    """按当前状态分派阶段执行（调用方负责 set_next_poll / settle_failure）."""
    row = _load_row(conn, lease)
    status = str(row["status"])
    if status == rp.STATUS_ENQUEUED:
        return _stage_enqueue_import(conn, lease, row)
    if status == rp.STATUS_IMPORTING:
        return _stage_poll_import(conn, lease, row)
    if status == rp.STATUS_ANALYZING:
        return _stage_poll_analysis(conn, lease, row)
    if status == rp.STATUS_SCRIPT_VERSIONING:
        return _stage_bridge_script(conn, lease, row)
    if status == rp.STATUS_FIRST_FRAMING:
        return _stage_first_framing(conn, lease, row)
    if status == rp.STATUS_COMPOSING:
        return _stage_compose(conn, lease, row)
    # 生成域与观察态（M5）。
    from app import replica_pipeline_generation as generation

    if status == rp.STATUS_GENERATING:
        return generation.observe_generation(conn, lease, row)
    if status == rp.STATUS_NEEDS_REVIEW:
        return generation.observe_needs_review(conn, lease, row)
    if status == rp.STATUS_NEEDS_REVIEW_SETTLED:
        return generation.observe_settled_recovery(conn, lease, row)
    return _failed("STAGE_UNSUPPORTED", f"状态 {status} 无执行器。")


# ---------------------------------------------------------------- IMPORTING


def _stage_enqueue_import(
    conn: BusinessConnection, lease: rp.ReplicaPipelineLease, row: BusinessRow
) -> StageResult:
    """ENQUEUED → 入队 purpose=replica 导入（挂既有 project）→ IMPORTING."""
    actor = _pipeline_actor(conn, lease.owner_user_id)
    key = rp.stage_operation_key(lease.id, rp.STAGE_IMPORT, lease.stage_retry_no)
    request = ViralImportRequest.model_validate(
        {
            "platform": lease.platform,
            "videoId": lease.video_id,
            "purpose": "replica",
            "projectId": lease.project_id,
        }
    )
    try:
        task = enqueue_viral_import_task(conn, actor=actor, request=request, idempotency_key=key)
    except IntegrityConstraintError:
        # 并发同键（另一个 worker 抢先入队）：按键找回在途任务继续。
        existing = conn.execute(
            "SELECT * FROM viral_import_tasks WHERE owner_user_id = %s AND idempotency_key = %s",
            (lease.owner_user_id, key),
        ).fetchone()
        if existing is None:
            raise
        task = existing
    rp.advance_status(
        conn,
        lease,
        decision=rp.AdvanceDecision(
            kind="completed",
            next_status=rp.STATUS_IMPORTING,
            next_stage=rp.STAGE_IMPORT,
            sub_task_id=str(task["id"]),
            sub_task_column="import_task_id",
        ),
    )
    return ADVANCED


def _stage_poll_import(
    conn: BusinessConnection, lease: rp.ReplicaPipelineLease, row: BusinessRow
) -> StageResult:
    """IMPORTING：等待导入完成 → 入队拆解 → ANALYZING."""
    import_task_id = row["import_task_id"]
    if not import_task_id:
        # 引用未回填（崩溃窗口/重试清引用后）：同状态重新入队，不迁移
        # （IMPORTING→IMPORTING 是非法自迁移，评审 A-r1#8）。
        return _enqueue_import_again(conn, lease)
    observed = _sub_task_status(conn, "viral_import_tasks", str(import_task_id))
    if observed is None:
        return _failed("IMPORT_TASK_MISSING", "导入任务不存在，请重试。")
    status, task = observed
    if status in {"PENDING", "RUNNING"}:
        return WAITING
    if status != "SUCCEEDED":
        return _failed(
            str(task["error_code"] or "IMPORT_FAILED"),
            str(task["error_message_redacted"] or "导入失败。"),
            retryable=bool(task["retryable"]),
        )
    asset_id = task["source_asset_id"]
    if not asset_id:
        return _failed("IMPORT_NO_ASSET", "导入结果缺少素材。")
    detail = _stage_detail(row)
    detail["sourceAssetId"] = str(asset_id)
    _save_stage_detail(conn, lease.id, detail)
    # 立即入队拆解（同一操作键幂等），迁移到 ANALYZING。
    duration_ms = _video_duration_ms(conn, lease.platform, lease.video_id)
    if duration_ms <= 0:
        return _failed("ANALYZE_NO_DURATION", "素材时长未知，无法拆解。")
    analyze_key = rp.stage_operation_key(lease.id, rp.STAGE_ANALYZE, lease.stage_retry_no)
    analysis_task, _created = enqueue_analysis_task(
        conn,
        project_id=lease.project_id,
        asset_id=str(asset_id),
        created_by_user_id=lease.owner_user_id,
        duration_seconds=duration_ms / 1000,
        request_id=analyze_key,
    )
    rp.advance_status(
        conn,
        lease,
        decision=rp.AdvanceDecision(
            kind="completed",
            next_status=rp.STATUS_ANALYZING,
            next_stage=rp.STAGE_ANALYZE,
            sub_task_id=str(analysis_task["id"]),
            sub_task_column="analysis_task_id",
        ),
    )
    return ADVANCED


# ---------------------------------------------------------------- ANALYZING


def _stage_poll_analysis(
    conn: BusinessConnection, lease: rp.ReplicaPipelineLease, row: BusinessRow
) -> StageResult:
    """ANALYZING：等待拆解完成（analysis 版本）→ 反查 shot card → SCRIPT_VERSIONING.

    真实契约（评审 A-r1#1）：``analysis_tasks.result_version_id`` 指向 **analysis**
    版本，不是 shot card；shot card 由分析完成时同步创建，payload 里带
    ``source_analysis_version_id`` 反向引用——按它反查本次拆解的 shot card。
    """
    analysis_task_id = row["analysis_task_id"]
    if not analysis_task_id:
        # 引用未回填（崩溃窗口/重试清引用后）：同状态重新入队，不迁移。
        return _enqueue_analysis_again(conn, lease, row)
    observed = _sub_task_status(conn, "analysis_tasks", str(analysis_task_id))
    if observed is None:
        return _failed("ANALYSIS_TASK_MISSING", "拆解任务不存在。")
    status, task = observed
    if status in {"PENDING", "RUNNING"}:
        return WAITING
    if status != "SUCCEEDED":
        return _failed(
            str(task["error_code"] or "ANALYSIS_FAILED"),
            str(task["error_message_redacted"] or "拆解失败。"),
            retryable=bool(task["retryable"]),
        )
    analysis_version_id = task["result_version_id"]
    if not analysis_version_id:
        return _failed("ANALYSIS_NO_RESULT", "拆解结果缺少分析版本。")
    shot_card = conn.execute(
        "SELECT id FROM versions WHERE project_id = %s AND kind = 'shot_card' "
        "AND (payload_json::jsonb ->> 'source_analysis_version_id') = %s "
        "ORDER BY version_number DESC LIMIT 1",
        (lease.project_id, str(analysis_version_id)),
    ).fetchone()
    if shot_card is None:
        return _failed("ANALYSIS_NO_SHOT_CARD", "拆解结果缺少分镜版本。")
    detail = _stage_detail(row)
    detail["analysisVersionId"] = str(analysis_version_id)
    detail["shotCardVersionId"] = str(shot_card["id"])
    _save_stage_detail(conn, lease.id, detail)
    rp.advance_status(
        conn,
        lease,
        decision=rp.AdvanceDecision(
            kind="completed",
            next_status=rp.STATUS_SCRIPT_VERSIONING,
            next_stage=rp.STAGE_SCRIPT,
        ),
    )
    return ADVANCED


# ------------------------------------------------------------ SCRIPT_VERSIONING


def _stage_bridge_script(
    conn: BusinessConnection, lease: rp.ReplicaPipelineLease, row: BusinessRow
) -> StageResult:
    """文案桥接（r1#4：拆解不产 script version）：从拆解稿建版本 → FIRST_FRAMING.

    真实契约（评审 A-r1#1）：``original_script`` 在 analysis 版本 payload 的
    ``analysis`` 对象内（shot card payload 没有顶层文案字段）；空文案走
    ``source=no_narration``——不走 AI 改写（规避迭代清单 N5 依赖）。
    """
    detail = _stage_detail(row)
    shot_card_version_id = detail.get("shotCardVersionId")
    analysis_version_id = detail.get("analysisVersionId")
    if not isinstance(shot_card_version_id, str) or not shot_card_version_id:
        return _failed("SCRIPT_NO_SHOT_CARD", "缺少分镜版本，无法保存文案。")
    if not isinstance(analysis_version_id, str) or not analysis_version_id:
        return _failed("SCRIPT_NO_ANALYSIS", "缺少分析版本，无法读取文案。")
    payload = _version_payload(conn, analysis_version_id)
    if payload is None:
        return _failed("SCRIPT_ANALYSIS_MISSING", "分析版本不存在。")
    analysis_payload = payload.get("analysis")
    script_text = (
        analysis_payload.get("original_script") if isinstance(analysis_payload, dict) else None
    )
    text = script_text.strip()[:8000] if isinstance(script_text, str) else ""
    actor = _pipeline_actor(conn, lease.owner_user_id)
    request = ScriptRequest(
        source="original" if text else "no_narration",
        text=text,
        shot_card_version_id=shot_card_version_id,
    )
    script_version = create_script_version(
        conn, project_id=lease.project_id, actor=actor, request=request
    )
    detail["scriptSource"] = request.source
    _save_stage_detail(conn, lease.id, detail)
    rp.advance_status(
        conn,
        lease,
        decision=rp.AdvanceDecision(
            kind="completed",
            next_status=rp.STATUS_FIRST_FRAMING,
            next_stage=rp.STAGE_FIRST_FRAME,
            sub_task_id=str(script_version["id"]),
            sub_task_column="script_version_id",
        ),
    )
    return ADVANCED


# --------------------------------------------------------------- FIRST_FRAMING


def _stage_first_framing(
    conn: BusinessConnection, lease: rp.ReplicaPipelineLease, row: BusinessRow
) -> StageResult:
    """首帧前置链（r1#5/r2#2）：源帧抽取与选定 → 人物绑定 → 候选 → 自动确认.

    子步骤进度存 stage_detail.firstFrame.substage（不新增状态，方案 16 态不变）：
    ``source_frames`` → ``candidates`` → 完成迁 COMPOSING。
    """
    detail = _stage_detail(row)
    first_frame = cast(dict[str, object], detail.setdefault("firstFrame", {}))
    substage = str(first_frame.get("substage") or "source_frames")
    if substage == "source_frames":
        return _first_frame_source_frames(conn, lease, row, detail, first_frame)
    if substage == "candidates":
        return _first_frame_candidates(conn, lease, row, detail, first_frame)
    return _failed("FIRST_FRAME_SUBSTAGE_INVALID", "首帧子步骤异常。")


def _first_frame_source_frames(
    conn: BusinessConnection,
    lease: rp.ReplicaPipelineLease,
    row: BusinessRow,
    detail: dict[str, object],
    first_frame: dict[str, object],
) -> StageResult:
    """源帧抽取（自动全部候选时间点）→ 自动选定开场帧（首张资产）."""

    actor = _pipeline_actor(conn, lease.owner_user_id)
    source_asset_id = detail.get("sourceAssetId")
    if not isinstance(source_asset_id, str) or not source_asset_id:
        return _failed("FIRST_FRAME_NO_SOURCE", "缺少导入素材，无法抽取源帧。")
    task_id = first_frame.get("sourceFrameTaskId")
    if not isinstance(task_id, str) or not task_id:
        key = rp.stage_operation_key(
            lease.id, f"{rp.STAGE_FIRST_FRAME}:source", lease.stage_retry_no
        )
        task = enqueue_source_frame_task(
            conn,
            actor=actor,
            project_id=lease.project_id,
            asset_id=source_asset_id,
            timestamps_seconds=None,
            idempotency_key=key,
        )
        first_frame["sourceFrameTaskId"] = str(task["id"])
        _save_stage_detail(conn, lease.id, detail)
        return WAITING
    observed = _sub_task_status(conn, "source_frame_tasks", task_id)
    if observed is None:
        return _failed("SOURCE_FRAME_TASK_MISSING", "源帧任务不存在。")
    status, task = observed
    if status in {"PENDING", "RUNNING"}:
        return WAITING
    if status != "SUCCEEDED":
        return _failed(
            str(task["error_code"] or "SOURCE_FRAME_FAILED"),
            str(task["error_message_redacted"] or "源帧抽取失败。"),
            retryable=bool(task["retryable"]),
        )
    candidates_version_id = task["result_version_id"]
    if not candidates_version_id:
        return _failed("SOURCE_FRAME_NO_RESULT", "源帧抽取缺少候选。")
    payload = _version_payload(conn, str(candidates_version_id))
    if payload is None:
        return _failed("SOURCE_FRAME_NO_RESULT", "源帧候选版本不存在。")
    candidates = payload.get("candidates")
    opening_asset_id: str | None = None
    if isinstance(candidates, list):
        # 固定策略：开场帧 = 候选按时间戳排序的首张（方案 §3.4「固定取开场帧」）。
        ordered = sorted(
            (value for value in candidates if isinstance(value, dict)),
            key=lambda value: float(value.get("timestamp_seconds") or 0),
        )
        if ordered and isinstance(ordered[0].get("asset_id"), str):
            opening_asset_id = cast(str, ordered[0]["asset_id"])
    if opening_asset_id is None:
        return _failed("SOURCE_FRAME_NO_RESULT", "源帧候选为空，无法选定开场帧。")
    selection = confirm_source_frame(
        conn,
        project_id=lease.project_id,
        source_frame_asset_id=opening_asset_id,
        actor=actor,
    )
    first_frame["sourceFrameSelectionVersionId"] = str(selection["id"])
    first_frame["substage"] = "candidates"
    _save_stage_detail(conn, lease.id, detail)
    # 同一轮继续入队候选（省一次领取往返）。
    return _first_frame_candidates(conn, lease, row, detail, first_frame)


def _first_frame_candidates(
    conn: BusinessConnection,
    lease: rp.ReplicaPipelineLease,
    row: BusinessRow,
    detail: dict[str, object],
    first_frame: dict[str, object],
) -> StageResult:
    """首帧候选生成 → 质检通过首位自动确认（AUTO_PIPELINE，r2#2）→ COMPOSING."""
    actor = _pipeline_actor(conn, lease.owner_user_id)
    selection_id = first_frame.get("sourceFrameSelectionVersionId")
    if not isinstance(selection_id, str) or not selection_id:
        return _failed("FIRST_FRAME_NO_SELECTION", "缺少源帧选定。")
    task_id = first_frame.get("candidateTaskId")
    if not isinstance(task_id, str) or not task_id:
        from fastapi import HTTPException

        try:
            character_version_id, reference_selection_id = _resolve_character_binding(
                conn, lease, selection_id, actor=actor
            )
        except rp.ReplicaPipelineError as exc:
            # 确定性业务失败转失败出口（不重试、转手动），而不是让异常穿透
            # driver（评审 A-r2#7：穿透就不会 settle_failure，「转手动」不落库）。
            return _failed(exc.code, exc.message, retryable=False)
        except HTTPException as exc:
            # 参考选择过期等域异常同样是确定性失败（评审 A-r3）：不能穿透
            # driver，否则失败状态不落库。
            detail_code = exc.detail
            code = "REPLICA_PIPELINE_CHARACTER_STALE"
            message = "人物参考图选择已过期，请转手动处理。"
            if isinstance(detail_code, dict) and isinstance(detail_code.get("code"), str):
                code = detail_code["code"]
            return _failed(code, message, retryable=False)
        key = rp.stage_operation_key(
            lease.id, f"{rp.STAGE_FIRST_FRAME}:candidates", lease.stage_retry_no
        )
        task = enqueue_first_frame_task(
            conn,
            actor=actor,
            project_id=lease.project_id,
            model=_FIRST_FRAME_MODEL,
            prompt=None,
            quantity=_FIRST_FRAME_QUANTITY,
            character_version_id=character_version_id,
            character_reference_selection_id=reference_selection_id,
            idempotency_key=key,
        )
        first_frame["candidateTaskId"] = str(task["id"])
        first_frame["characterVersionId"] = character_version_id
        first_frame["characterReferenceSelectionId"] = reference_selection_id
        _save_stage_detail(conn, lease.id, detail)
        # 引用回填（评审 A-r1#9）：失败出口（_failure_exit_status）靠该列
        # 判断未收口候选任务——只存 stage_detail 会让期限失败直接落 FAILED。
        conn.execute(
            "UPDATE replica_pipeline_tasks SET first_frame_task_id = %s WHERE id = %s",
            (str(task["id"]), lease.id),
        )
        return WAITING
    observed = _sub_task_status(conn, "first_frame_tasks", task_id)
    if observed is None:
        return _failed("FIRST_FRAME_TASK_MISSING", "首帧任务不存在。")
    status, task = observed
    if status in {"PENDING", "RUNNING"}:
        return WAITING
    if status != "SUCCEEDED":
        return _failed(
            str(task["error_code"] or "FIRST_FRAME_FAILED"),
            str(task["error_message_redacted"] or "首帧生成失败。"),
            retryable=bool(task["retryable"]),
        )
    candidates_version_id = task["result_version_id"]
    if not candidates_version_id:
        return _failed("FIRST_FRAME_NO_RESULT", "首帧候选缺失。")
    payload = _version_payload(conn, str(candidates_version_id))
    if payload is None:
        return _failed("FIRST_FRAME_NO_RESULT", "首帧候选版本不存在。")
    candidates = payload.get("candidates")
    chosen_asset_id: str | None = None
    quality_available = False
    if isinstance(candidates, list):
        ordered = [value for value in candidates if isinstance(value, dict)]
        for value in ordered:
            quality = value.get("quality")
            if quality is not None:
                quality_available = True
                passed = (isinstance(quality, dict) and bool(quality.get("passed"))) or (
                    quality is True
                )
                if passed and isinstance(value.get("asset_id"), str):
                    chosen_asset_id = cast(str, value["asset_id"])
                    break
        if not quality_available:
            # 真实首帧链路不执行质检（生成路径强制 quality=None，评审
            # A-r1#3）：确定性取生成顺序首位，stage_detail 如实记录
            # qualityAvailable=false，供前端/审计区分「无质检」与「质检通过」。
            for value in ordered:
                if isinstance(value.get("asset_id"), str):
                    chosen_asset_id = cast(str, value["asset_id"])
                    break
    first_frame["qualityAvailable"] = quality_available
    if chosen_asset_id is None:
        return _failed(
            "FIRST_FRAME_QUALITY_REJECTED",
            "首帧候选全部未通过质检，请转手动挑选。",
        )
    selection = confirm_first_frame(
        conn,
        project_id=lease.project_id,
        first_frame_asset_id=chosen_asset_id,
        actor=actor,
        first_frame_candidates_version_id=str(candidates_version_id),
        review_mode="AUTO_PIPELINE",
        # 无质检证据时显式授权（生成域既有 quality_override 语义）；
        # review_mode=AUTO_PIPELINE 不构成人工确认，不能冒充（评审 A-r2#2）。
        quality_override=not quality_available,
    )
    first_frame["firstFrameSelectionVersionId"] = str(selection["id"])
    first_frame["firstFrameAssetId"] = chosen_asset_id
    _save_stage_detail(conn, lease.id, detail)
    rp.advance_status(
        conn,
        lease,
        decision=rp.AdvanceDecision(
            kind="completed",
            next_status=rp.STATUS_COMPOSING,
            next_stage=rp.STAGE_COMPOSE,
        ),
    )
    return ADVANCED


def _resolve_character_binding(
    conn: BusinessConnection,
    lease: rp.ReplicaPipelineLease,
    source_frame_selection_version_id: str,
    *,
    actor: CurrentUser,
) -> tuple[str | None, str | None]:
    """解析人物绑定：建主角绑定 + 建参考图 selection，再取可供生成的 selection.

    评审 A-r1#2 + A-r2#1：新项目没有 project_main_characters 与参考图选择；
    只读解析必然返回 None 并在真实首帧原语处失败。这里按冻结版本：
    ① choose_project_character_version 建立主角绑定；
    ② current selection 为空时 create_character_reference_selection 建立
       （selected_asset_ids=None 走自动选择）；
    ③ 最终仍无可用 selection → 显式失败转手动（绝不静默降级到无人物生成）。
    """
    if not lease.character_version_id:
        # 发起时未冻结人物（M7 预检保证不会走到这里）：按无人物生成。
        return (None, None)
    try:
        choose_project_character_version(
            conn,
            actor=actor,
            project_id=lease.project_id,
            character_version_id=lease.character_version_id,
        )
    except Exception as exc:  # 建绑失败是确定性失败：不重试、转手动
        raise rp.ReplicaPipelineError(
            409,
            "REPLICA_PIPELINE_CHARACTER_BINDING",
            "人物绑定失败，请转手动处理。",
        ) from exc
    selection = current_character_reference_selection_for_generation(
        conn,
        project_id=lease.project_id,
        source_frame_version_id=source_frame_selection_version_id,
        require_usable_character=False,
    )
    if selection is None:
        try:
            created = create_character_reference_selection(
                conn,
                actor=actor,
                project_id=lease.project_id,
                expected_source_frame_version_id=source_frame_selection_version_id,
                expected_character_version_id=lease.character_version_id,
                selected_asset_ids=None,
            )
        except Exception as exc:
            raise rp.ReplicaPipelineError(
                409,
                "REPLICA_PIPELINE_CHARACTER_BINDING",
                "参考图选择建立失败，请转手动处理。",
            ) from exc
        return (created.character_version_id, created.id)
    if selection.character_version_id != lease.character_version_id:
        raise rp.ReplicaPipelineError(
            409,
            "REPLICA_PIPELINE_CHARACTER_STALE",
            "人物版本已变化，请转手动处理。",
        )
    return (selection.character_version_id, selection.id)


# ------------------------------------------------------------------ COMPOSING


def _stage_compose(
    conn: BusinessConnection, lease: rp.ReplicaPipelineLease, row: BusinessRow
) -> StageResult:
    """提示词编译（确定性本地合成，无计费）→ AWAIT_CONFIRM.

    编译参数固定策略（方案 §3.4）：768P / 画幅跟随原片（adaptive）/
    timeline_policy=preserve / 时长跟随原片（4–30s 门禁内）。编译冲突
    （TIMELINE_CONFIRMATION_REQUIRED / FIRST_FRAME_ALIGNMENT_REQUIRED）
    不自动代答——落失败出口转手动（人工链路本就有对应交互分支）。
    """
    from fastapi import HTTPException

    detail = _stage_detail(row)
    script_version_id = detail.get("scriptVersionId")
    shot_card_version_id = detail.get("shotCardVersionId")
    first_frame = cast(dict[str, object], detail.get("firstFrame") or {})
    first_frame_asset_id = first_frame.get("firstFrameAssetId")
    if not isinstance(script_version_id, str):
        script_version_id = str(row["script_version_id"] or "")
    if not isinstance(shot_card_version_id, str) or not isinstance(first_frame_asset_id, str):
        return _failed("COMPOSE_INPUT_MISSING", "编译输入缺失。")
    duration_ms = _video_duration_ms(conn, lease.platform, lease.video_id)
    duration_seconds = round(duration_ms / 1000) if duration_ms > 0 else 0
    if duration_seconds > 15:
        # 批次请求限 4–15 秒（GenerationBatchRequest）：>15 秒源片在 v1 不支持
        # 自动生成——显式失败转手动，不静默 clamp（否则编译 30s、报价 15s、
        # 建批次 PROMPT_PARAMETERS_MISMATCH，评审 A-r1#11）。
        return _failed(
            "SOURCE_TOO_LONG_FOR_GENERATION",
            "源片超过 15 秒，暂不支持自动生成；请转手动分段处理。",
        )
    duration_seconds = max(4, duration_seconds)
    actor = _pipeline_actor(conn, lease.owner_user_id)
    request = PromptCompileRequest(
        script_version_id=script_version_id,
        shot_card_version_id=shot_card_version_id,
        first_frame_asset_id=first_frame_asset_id,
        output_duration_seconds=duration_seconds,
        resolution="768P",
        ratio="adaptive",
        timeline_policy="preserve",
    )
    try:
        prompt_version = compile_prompt_version(
            conn, project_id=lease.project_id, actor=actor, request=request
        )
    except HTTPException as exc:
        code = "COMPILE_FAILED"
        detail_obj = exc.detail
        if isinstance(detail_obj, dict) and isinstance(detail_obj.get("code"), str):
            code = detail_obj["code"]
        return _failed(code, "提示词编译冲突，请转手动处理。")
    detail["scriptVersionId"] = script_version_id
    detail["promptVersionId"] = str(prompt_version["id"])
    detail["outputDurationSeconds"] = duration_seconds
    _save_stage_detail(conn, lease.id, detail)
    # 进入确认点前冻结授权基准（方案 §4）：档位/时长/名额/金额上限/产物指纹。
    from app.replica_pipeline_confirm import freeze_confirm_quote

    row_after = _load_row(conn, lease)
    snapshot = freeze_confirm_quote(conn, row=row_after, owner_user_id=lease.owner_user_id)
    conn.execute(
        "UPDATE replica_pipeline_tasks SET quote_snapshot = %s, "
        "awaiting_since = now() WHERE id = %s",
        (json.dumps(snapshot, ensure_ascii=False), lease.id),
    )
    rp.advance_status(
        conn,
        lease,
        decision=rp.AdvanceDecision(
            kind="completed",
            next_status=rp.STATUS_AWAIT_CONFIRM,
            next_stage=rp.STAGE_CONFIRM,
            sub_task_id=str(prompt_version["id"]),
            sub_task_column="prompt_version_id",
        ),
    )
    return ADVANCED


def _enqueue_import_again(conn: BusinessConnection, lease: rp.ReplicaPipelineLease) -> StageResult:
    """同状态（IMPORTING）重新入队导入：操作键复用同一 retry_no（崩溃找回）."""
    actor = _pipeline_actor(conn, lease.owner_user_id)
    key = rp.stage_operation_key(lease.id, rp.STAGE_IMPORT, lease.stage_retry_no)
    request = ViralImportRequest.model_validate(
        {
            "platform": lease.platform,
            "videoId": lease.video_id,
            "purpose": "replica",
            "projectId": lease.project_id,
        }
    )
    try:
        task = enqueue_viral_import_task(conn, actor=actor, request=request, idempotency_key=key)
    except IntegrityConstraintError:
        existing = conn.execute(
            "SELECT * FROM viral_import_tasks WHERE owner_user_id = %s AND idempotency_key = %s",
            (lease.owner_user_id, key),
        ).fetchone()
        if existing is None:
            raise
        task = existing
    conn.execute(
        "UPDATE replica_pipeline_tasks SET import_task_id = %s WHERE id = %s",
        (str(task["id"]), lease.id),
    )
    return WAITING


def _enqueue_analysis_again(
    conn: BusinessConnection, lease: rp.ReplicaPipelineLease, row: BusinessRow
) -> StageResult:
    """同状态（ANALYZING）重新入队拆解：素材从 stage_detail 找回."""
    detail = _stage_detail(row)
    asset_id = detail.get("sourceAssetId")
    if not isinstance(asset_id, str) or not asset_id:
        return _failed("ANALYZE_NO_ASSET", "缺少导入素材，无法重新拆解。")
    duration_ms = _video_duration_ms(conn, lease.platform, lease.video_id)
    if duration_ms <= 0:
        return _failed("ANALYZE_NO_DURATION", "素材时长未知。")
    analyze_key = rp.stage_operation_key(lease.id, rp.STAGE_ANALYZE, lease.stage_retry_no)
    analysis_task, _created = enqueue_analysis_task(
        conn,
        project_id=lease.project_id,
        asset_id=asset_id,
        created_by_user_id=lease.owner_user_id,
        duration_seconds=duration_ms / 1000,
        request_id=analyze_key,
    )
    conn.execute(
        "UPDATE replica_pipeline_tasks SET analysis_task_id = %s WHERE id = %s",
        (str(analysis_task["id"]), lease.id),
    )
    return WAITING


def drive_pipeline_once(conn: BusinessConnection, lease: rp.ReplicaPipelineLease) -> str:
    """worker 侧一次驱动：advanced/waiting/failed 的统一收尾（评审 A-r1#8）.

    - waiting → set_next_poll（退避，等待期不占执行槽）；
    - failed → 业务重试预算内：原子递增 stage_retry_no、清理本阶段子任务引用、
      下一轮同状态重新入队（操作键换新键，首帧域不会重放失败终态）；
      预算耗尽 → settle_failure（DRAINING/终态由 _failure_exit_status 裁决）。
    - advanced → 状态机已迁移并释放租约。
    """
    row = _load_row(conn, lease)
    status = str(row["status"])
    result = run_pipeline_stage(conn, lease)
    if result.kind == "waiting":
        rp.set_next_poll(conn, lease, wait_failures=1)
        return "waiting"
    if result.kind == "advanced":
        return "advanced"
    retry_no = int(row["stage_retry_no"] or 0)
    clear_columns = _STAGE_CLEAR_COLUMNS.get(status, ())
    if result.retryable and retry_no < rp.MAX_STAGE_RETRIES:
        sets = ["stage_retry_no = %s", "updated_at = now()"]
        params: list[object] = [retry_no + 1]
        for column in clear_columns:
            sets.append(f"{column} = NULL")
        if status == rp.STATUS_FIRST_FRAMING:
            # 首帧重试按子步骤回退 JSON 断点（评审 A-r2#6）：不清候选任务
            # 引用会让 handler 永远观察旧失败任务直至预算耗尽。
            sets.append(
                "stage_detail = jsonb_set(COALESCE(stage_detail, '{}'::jsonb), "
                "'{firstFrame}', "
                "(COALESCE(stage_detail, '{}'::jsonb) #> '{firstFrame}') "
                "- 'candidateTaskId' - 'sourceFrameTaskId', true)"
            )
        params.extend([lease.id, lease.attempt, lease.worker_id])
        moved = conn.execute(
            f"UPDATE replica_pipeline_tasks SET {', '.join(sets)} "
            "WHERE id = %s AND attempt = %s AND locked_by = %s "
            "AND locked_until IS NOT NULL AND locked_until > clock_timestamp()",
            tuple(params),
        ).rowcount
        if moved != 1:
            raise rp.ReplicaPipelineError(
                409, "REPLICA_PIPELINE_LEASE_LOST", "编排任务租约已失效。"
            )
        rp.set_next_poll(conn, lease, wait_failures=1)
        return "retrying"
    rp.settle_failure(
        conn,
        lease,
        code=result.failure_code or "STAGE_FAILED",
        message=result.failure_message or "阶段失败。",
        retryable=False,
    )
    return "failed"


# 各推进态重试时需清理的引用列（子任务/版本句柄交给操作键重建）。
_STAGE_CLEAR_COLUMNS: dict[str, tuple[str, ...]] = {
    rp.STATUS_ENQUEUED: (),
    rp.STATUS_IMPORTING: ("import_task_id",),
    rp.STATUS_ANALYZING: ("analysis_task_id",),
    rp.STATUS_SCRIPT_VERSIONING: ("script_version_id",),
    rp.STATUS_FIRST_FRAMING: ("first_frame_task_id",),
    rp.STATUS_COMPOSING: ("prompt_version_id",),
}
