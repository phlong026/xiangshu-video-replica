"""全自动复刻编排器 M5：生成域映射与重生成（方案 §3.2.1/§5.1）.

交付口径（r3#1/r7#4/r9#1 统一）：
- 交付单位是**交付名额**（原始生成任务数），不是镜头数；
- 768P 可交付谓词 = 任务 ``status='SUCCEEDED'`` 且 ``archive_status ∈
  {DIRECT, ARCHIVED}``（成片直链/归档就绪），不依赖规范化后处理；
- DELIVERED 三条件充要：全部子任务收口无未知 ＋ 名额被有效结果覆盖 ＋
  全部有效产物可交付。不要求原批次状态为 SUCCEEDED（原失败名额即使已有
  成功替代，原批次仍停在 COMPLETED_WITH_FAILURES）。

重生成：SETTLED → 新一轮确认（新报价新批次），替代批次挂接原 pipeline；
观察时以「全部关联批次的有效结果」合并计算覆盖，超额重算后可达 DELIVERED。
"""

from __future__ import annotations

import json

from app import replica_pipeline as rp
from app.db_portable import BusinessConnection, BusinessRow
from app.replica_pipeline_stages import ADVANCED, WAITING, StageResult, _failed

# 有效交付：成功且成片直链/归档就绪（generation.py:3417 同款口径）。
_DELIVERABLE_SQL = "status = 'SUCCEEDED' AND archive_status IN ('DIRECT','ARCHIVED')"
_TERMINAL_TASK_STATUSES = ("SUCCEEDED", "FAILED", "CANCELLED")


def _batch_ids(conn: BusinessConnection, row: BusinessRow) -> list[str]:
    """pipeline 关联的全部批次：stage_detail.generation.batchHistory + 当前 batch.

    评审 A-r1#6：重生成会覆盖 ``batch_id`` 引用，历史批次的有效成功任务必须
    并入交付判定——历史批次 id 由受理时写入 batchHistory 保存。
    """
    detail = _stage_detail(row)
    generation = detail.get("generation")
    history = generation.get("batchHistory") if isinstance(generation, dict) else None
    ids: list[str] = []
    if isinstance(history, list):
        ids.extend(str(value) for value in history if value)
    if row["batch_id"]:
        ids.append(str(row["batch_id"]))
    return ids


def _open_tasks(conn: BusinessConnection, row: BusinessRow) -> int:
    """未收口语义：存在非终态任务或存在提交未知任务（方案 §3.2.2）."""
    batch_ids = _batch_ids(conn, row)
    if not batch_ids:
        return 0
    row_count = conn.execute(
        """
        SELECT count(*) AS n FROM generation_tasks t
        WHERE t.batch_id = ANY(%s)
            AND (t.status <> ALL(%s)
                 OR (t.status = 'PENDING' AND t.provider_task_id IS NULL
                     AND t.attempt > 0 AND t.error_code = 'SUBMISSION_UNCERTAIN'))
        """,
        (batch_ids, list(_TERMINAL_TASK_STATUSES)),
    ).fetchone()
    if row_count is None:
        return 0
    return int(row_count["n"])


def _has_uncertain(conn: BusinessConnection, row: BusinessRow) -> bool:
    """提交未知/待核对：止付逻辑依赖的不确定信号（评审 A-r1#10）."""
    batch_ids = _batch_ids(conn, row)
    if not batch_ids:
        return False
    found = conn.execute(
        "SELECT 1 FROM generation_tasks WHERE batch_id = ANY(%s) "
        "AND (status IN ('SUBMISSION_UNCERTAIN', 'NEEDS_ATTENTION') "
        "     OR error_code = 'LEASE_EXPIRED_NEEDS_ATTENTION') LIMIT 1",
        (batch_ids,),
    ).fetchone()
    return found is not None


def _deliverable_count(conn: BusinessConnection, row: BusinessRow) -> int:
    """有效交付名额数（评审 A-r1#6）：关联批次中满足可交付谓词、且未被替代.

    ``superseded_by_task_id`` 语义（评审 A-r2#4 修正方向）：**原任务**的该列
    指向替代者——即 ``IS NULL`` 者从未被替代，才是有效末端；被替代的源任务
    即使曾成功也退出有效集合。quantity=1 时每链至多一个末端，天然满足
    「每条替代链最多贡献一个有效结果」。
    """
    batch_ids = _batch_ids(conn, row)
    if not batch_ids:
        return 0
    counted = conn.execute(
        f"""
        SELECT count(*) AS n FROM generation_tasks t
        WHERE t.batch_id = ANY(%s) AND t.{_DELIVERABLE_SQL}
            AND t.superseded_by_task_id IS NULL
        """,
        (batch_ids,),
    ).fetchone()
    if counted is None:
        return 0
    return int(counted["n"])


def _expected_quantity(row: BusinessRow) -> int:
    snapshot = row["quote_snapshot"] if isinstance(row["quote_snapshot"], dict) else {}
    quantity = snapshot.get("quantity")
    return int(quantity) if isinstance(quantity, int) and quantity > 0 else 1


def _batch_status(conn: BusinessConnection, batch_id: str) -> str | None:
    row = conn.execute(
        "SELECT status FROM generation_batches WHERE id = %s", (batch_id,)
    ).fetchone()
    if row is None:
        return None
    return str(row["status"])


def observe_generation(
    conn: BusinessConnection, lease: rp.ReplicaPipelineLease, row: BusinessRow
) -> StageResult:
    """GENERATING 观察：批次推进 → 交付/待核对（观察态由 run_pipeline_stage 分派）.

    返回 ``advanced`` 表示已迁移（DELIVERED/NEEDS_REVIEW/SETTLED）；
    ``waiting`` 表示批次仍在跑；``failed`` 仅用于任务/批次引用缺失。
    """
    batch_id = row["batch_id"]
    if not batch_id:
        return _failed("GENERATION_BATCH_MISSING", "生成批次引用缺失。")
    status = _batch_status(conn, str(batch_id))
    if status is None:
        return _failed("GENERATION_BATCH_MISSING", "生成批次不存在。")
    # 提交未知/待核对：置父任务冻结标记（止付逻辑依赖），保持等待等对账
    # （评审 A-r1#10：只有真实对账确定性结果才能清标记）。
    if _has_uncertain(conn, row):
        detail = _stage_detail(row)
        if detail.get("uncertain") is not True:
            detail["uncertain"] = True
            _save(conn, lease.id, detail)
        return WAITING
    detail = _stage_detail(row)
    if detail.get("uncertain") is True:
        detail["uncertain"] = False
        _save(conn, lease.id, detail)
    if _open_tasks(conn, row) > 0:
        return WAITING
    deliverable = _deliverable_count(conn, row)
    expected = _expected_quantity(row)
    # 合并而非覆盖（评审 A-r2#5）：batchHistory/regenNo 是重生成受理的
    # 不可丢状态——整体赋值会把历史批次与轮次清空，导致下一轮重生成回放旧批次。
    generation_detail = detail.get("generation")
    if not isinstance(generation_detail, dict):
        generation_detail = {}
    generation_detail["batchStatus"] = status
    generation_detail["deliverable"] = deliverable
    generation_detail["expected"] = expected
    detail["generation"] = generation_detail
    _save(conn, lease.id, detail)
    if deliverable >= expected:
        _advance(conn, lease, rp.STATUS_DELIVERED, rp.STAGE_DELIVER)
        return ADVANCED
    # 收口但未达交付：待人工选择（重生成/接受部分/转手动）。
    if deliverable > 0:
        _advance(conn, lease, rp.STATUS_NEEDS_REVIEW_SETTLED, rp.STAGE_GENERATE)
        return ADVANCED
    _advance(conn, lease, rp.STATUS_NEEDS_REVIEW_SETTLED, rp.STAGE_GENERATE)
    return ADVANCED


def observe_needs_review(
    conn: BusinessConnection, lease: rp.ReplicaPipelineLease, row: BusinessRow
) -> StageResult:
    """NEEDS_REVIEW 观察收口（r4#1/r5#1）：全收口后重算覆盖——
    全覆盖可交付 → DELIVERED；否则 → NEEDS_REVIEW_SETTLED.

    无批次引用属于数据异常（待核对态必来自某个批次）：显式失败而非静默
    迁移，避免把坏数据「观察」成正常终态。
    """
    if not row["batch_id"]:
        return _failed("GENERATION_BATCH_MISSING", "生成批次引用缺失。")
    if _open_tasks(conn, row) > 0:
        return WAITING
    deliverable = _deliverable_count(conn, row)
    expected = _expected_quantity(row)
    if deliverable >= expected:
        _advance(conn, lease, rp.STATUS_DELIVERED, rp.STAGE_DELIVER)
        return ADVANCED
    _advance(conn, lease, rp.STATUS_NEEDS_REVIEW_SETTLED, rp.STAGE_GENERATE)
    return ADVANCED


def observe_settled_recovery(
    conn: BusinessConnection, lease: rp.ReplicaPipelineLease, row: BusinessRow
) -> StageResult:
    """SETTLED 产物恢复观察（r7#3）：免费恢复把可交付名额补足 → DELIVERED.

    仅观察，不触发重生成（重生成必须走显式授权入口）。无进展时保持等待，
    由用户动作或 72h 窗口另行处理。
    """
    deliverable = _deliverable_count(conn, row)
    expected = _expected_quantity(row)
    if deliverable >= expected:
        _advance(conn, lease, rp.STATUS_DELIVERED, rp.STAGE_DELIVER)
        return ADVANCED
    return WAITING


def accept_partial(
    conn: BusinessConnection, *, pipeline_id: str, owner_user_id: str
) -> dict[str, object]:
    """接受部分成果 → DELIVERED_PARTIAL（仅 SETTLED 可达；要求有效且可交付集合非空）."""
    _lock_pipeline(conn, pipeline_id, owner_user_id)
    row = conn.execute(
        "SELECT * FROM replica_pipeline_tasks WHERE id = %s", (pipeline_id,)
    ).fetchone()
    if row is None:
        raise rp.ReplicaPipelineError(404, "REPLICA_PIPELINE_NOT_FOUND", "编排任务不存在。")
    if str(row["status"]) != rp.STATUS_NEEDS_REVIEW_SETTLED:
        raise rp.ReplicaPipelineError(
            409, "REPLICA_PIPELINE_NOT_SETTLED", "任务尚不可接受部分成果。"
        )
    if _open_tasks(conn, row) > 0:
        raise rp.ReplicaPipelineError(409, "REPLICA_PIPELINE_NOT_SETTLED", "仍有未收口任务。")
    deliverable = _deliverable_count(conn, row)
    if deliverable <= 0:
        raise rp.ReplicaPipelineError(
            409, "REPLICA_PIPELINE_NOTHING_DELIVERABLE", "没有可交付成果。"
        )
    conn.execute(
        "UPDATE replica_pipeline_tasks SET status = %s, finished_at = now(), "
        "updated_at = now(), next_poll_at = now() WHERE id = %s",
        (rp.STATUS_DELIVERED_PARTIAL, pipeline_id),
    )
    return {"pipelineId": pipeline_id, "status": rp.STATUS_DELIVERED_PARTIAL}


def regenerate_pipeline(
    conn: BusinessConnection,
    *,
    pipeline_id: str,
    owner_user_id: str,
    quote_token: str,
    max_credits: int,
    reason: str = "全自动复刻重生成",
) -> dict[str, object]:
    """重生成受理（方案 §5.1.1 + 评审 A-r2#3）：SETTLED → 付费重生成原语.

    **不走 lock_prompt + create_generation_batch**：首次确认后提示词已 USED，
    该路径必报 PROMPT_NOT_LOCKED。改用生成域既有付费重生成原语
    ``regenerate_generation_batch``（对原批次整批重生成，内部复用冻结任务
    快照并校验重生成报价）。资格互斥：迁回 GENERATING 触发唯一索引裁决——
    用户/项目已另发任务时整体回滚（原语创建的批次随事务撤销）。
    """
    from app.auth import CurrentUser
    from app.generation import (
        PaidRegenerationRequest,
        regenerate_generation_batch,
    )

    _lock_pipeline(conn, pipeline_id, owner_user_id)
    row = conn.execute(
        "SELECT * FROM replica_pipeline_tasks WHERE id = %s", (pipeline_id,)
    ).fetchone()
    if row is None:
        raise rp.ReplicaPipelineError(404, "REPLICA_PIPELINE_NOT_FOUND", "编排任务不存在。")
    if str(row["status"]) != rp.STATUS_NEEDS_REVIEW_SETTLED:
        raise rp.ReplicaPipelineError(
            409, "REPLICA_PIPELINE_NOT_SETTLED", "仅已收口的待核对任务可重生成。"
        )
    if _open_tasks(conn, row) > 0:
        raise rp.ReplicaPipelineError(409, "REPLICA_PIPELINE_NOT_SETTLED", "仍有未收口任务。")
    source_batch_id = row["batch_id"]
    if not source_batch_id:
        raise rp.ReplicaPipelineError(409, "REPLICA_PIPELINE_REGEN_INPUT", "缺少原批次引用。")
    actor = CurrentUser(
        id=owner_user_id,
        username=owner_user_id,
        display_name=owner_user_id,
        role="customer",
    )
    # 重生成轮次（评审 A-r2#7）：独立授权 +1，幂等键含轮次与源批次。
    detail = _stage_detail(row)
    generation_detail = detail.get("generation")
    if not isinstance(generation_detail, dict):
        generation_detail = {}
    regen_no = int(generation_detail.get("regenNo") or 0) + 1
    history = generation_detail.get("batchHistory")
    history_ids = [str(value) for value in history] if isinstance(history, list) else []
    history_ids.append(str(source_batch_id))
    generation_detail["regenNo"] = regen_no
    generation_detail["batchHistory"] = history_ids
    detail["generation"] = generation_detail
    _save(conn, pipeline_id, detail)
    # 迁回 GENERATING 取得在途资格（唯一索引裁决；冲突即整体回滚）。
    moved = conn.execute(
        """
        UPDATE replica_pipeline_tasks SET status = %s, stage = %s, confirmed_at = now(),
            updated_at = now(), next_poll_at = now()
        WHERE id = %s AND status = %s
        """,
        (
            rp.STATUS_GENERATING,
            rp.STAGE_GENERATE,
            pipeline_id,
            rp.STATUS_NEEDS_REVIEW_SETTLED,
        ),
    ).rowcount
    if moved != 1:
        raise rp.ReplicaPipelineError(409, "REPLICA_PIPELINE_NOT_SETTLED", "状态已变化，请刷新。")
    batch = regenerate_generation_batch(
        conn,
        batch_id=str(source_batch_id),
        actor=actor,
        request=PaidRegenerationRequest(
            quote_token=quote_token,
            max_credits=max_credits,
            idempotency_key=f"pipeline:{pipeline_id}:regenerate:{regen_no}",
            # 积分数不是人民币成本（评审 A-r3#3/A-r4）：无法确定真实成本时传 None。
            estimated_cost_snapshot=None,
            generation_reason=reason[:500],
        ),
    )
    batch_id = str(batch.id)
    conn.execute(
        "UPDATE replica_pipeline_tasks SET batch_id = %s WHERE id = %s",
        (batch_id, pipeline_id),
    )
    return {"pipelineId": pipeline_id, "batchId": batch_id, "status": rp.STATUS_GENERATING}


# ---------------------------------------------------------------- helpers


def _first_frame_asset_id(detail: dict[str, object]) -> str | None:
    first_frame = detail.get("firstFrame")
    if isinstance(first_frame, dict):
        value = first_frame.get("firstFrameAssetId")
        if isinstance(value, str) and value:
            return value
    return None


def _stage_detail(row: BusinessRow) -> dict[str, object]:
    from app.replica_pipeline_stages import _stage_detail as loader

    return loader(row)


def _save(conn: BusinessConnection, pipeline_id: str, detail: dict[str, object]) -> None:
    conn.execute(
        "UPDATE replica_pipeline_tasks SET stage_detail = %s WHERE id = %s",
        (json.dumps(detail, ensure_ascii=False), pipeline_id),
    )


def _advance(
    conn: BusinessConnection, lease: rp.ReplicaPipelineLease, status: str, stage: str
) -> None:
    rp.advance_status(
        conn,
        lease,
        decision=rp.AdvanceDecision(kind="completed", next_status=status, next_stage=stage),
    )


def _lock_pipeline(conn: BusinessConnection, pipeline_id: str, owner_user_id: str) -> None:
    """锁序：非锁定读→锁 projects→锁 pipeline（方案 §6/r8#2）."""
    pre = conn.execute(
        "SELECT project_id FROM replica_pipeline_tasks WHERE id = %s AND owner_user_id = %s",
        (pipeline_id, owner_user_id),
    ).fetchone()
    if pre is None:
        raise rp.ReplicaPipelineError(404, "REPLICA_PIPELINE_NOT_FOUND", "编排任务不存在。")
    conn.execute("SELECT id FROM projects WHERE id = %s FOR UPDATE", (str(pre["project_id"]),))
    conn.execute("SELECT id FROM replica_pipeline_tasks WHERE id = %s FOR UPDATE", (pipeline_id,))
