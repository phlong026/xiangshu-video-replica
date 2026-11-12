"""全自动复刻编排器 M4：确认点资金闸门（方案 §4）.

确认点 = 唯一一次付费授权：
- 进入 AWAIT_CONFIRM 时冻结报价快照（quote_snapshot：档位/时长/名额/金额上限/
  产物指纹/单价快照）——72 小时确认窗口的授权基准；
- 用户确认时携带**当前有效**报价凭证（quote_token，300s 有效期）与实际所见
  金额上限——服务端校验凭证、快照指纹与金额上限三方一致后才建批次；任何
  不一致（旧页面、过期报价、目录变更）都要求重新展示，绝不静默按新价授权
  （评审 r1#1）；
- 确认是单一事务：非锁定读→锁 projects→锁 pipeline→持锁重验→校验凭证→
  锁提示词→建批次（含资金预占）→回填 batch_id/GENERATING（评审 r1#2/r8#2）。

批次请求自带报价校验与预占（generation.create_generation_batch 内部
validate_quote + 预占），本模块不重复扣费逻辑。
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import cast

from app import replica_pipeline as rp
from app.db_portable import BusinessConnection, BusinessRow
from app.replica_pipeline_stages import _stage_detail

# 确认卡有效期（方案 §4.1：报价凭证 300s，过期前端重新核价）。
QUOTE_WINDOW_SECONDS = 300


def _confirm_error(code: str, message: str, status_code: int = 409) -> rp.ReplicaPipelineError:
    return rp.ReplicaPipelineError(status_code, code, message)


def _first_frame_asset_id(detail: dict[str, object]) -> str | None:
    first_frame = detail.get("firstFrame")
    if isinstance(first_frame, dict):
        value = first_frame.get("firstFrameAssetId")
        if isinstance(value, str) and value:
            return value
    return None


def quote_fingerprint(
    *,
    prompt_version_id: str,
    shot_card_version_id: str,
    script_version_id: str,
    first_frame_asset_id: str,
    resolution: str,
    duration_seconds: int,
    quantity: int,
) -> str:
    """产物指纹：用户看到的这一组输入的唯一标识（评审 r1#1）."""
    payload = "|".join(
        (
            prompt_version_id,
            shot_card_version_id,
            script_version_id,
            first_frame_asset_id,
            resolution,
            str(duration_seconds),
            str(quantity),
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def freeze_confirm_quote(
    conn: BusinessConnection,
    *,
    row: BusinessRow,
    owner_user_id: str,
) -> dict[str, object]:
    """进入 AWAIT_CONFIRM 前冻结授权基准（金额上限/档位/名额/指纹/报价凭证）.

    固定策略（方案 §3.4）：768P、单任务、时长=源片时长（4–15 秒为可成交口径，
    超出 15 秒的源片在 COMPOSING 已按失败出口转手动——批次请求限 4–15 秒，
    30 秒复刻走既有两段方案，不由本管道自动代答）。
    """
    from app.generation import generation_price_quote

    detail = _stage_detail(row)
    prompt_version_id = detail.get("promptVersionId")
    shot_card_version_id = detail.get("shotCardVersionId")
    script_version_id = detail.get("scriptVersionId")
    if not isinstance(script_version_id, str) or not script_version_id:
        script_version_id = str(row["script_version_id"] or "")
    first_frame_asset_id = _first_frame_asset_id(detail)
    if not (
        isinstance(prompt_version_id, str)
        and isinstance(shot_card_version_id, str)
        and isinstance(script_version_id, str)
        and isinstance(first_frame_asset_id, str)
    ):
        raise _confirm_error("CONFIRM_INPUT_INCOMPLETE", "确认点输入不完整。")
    duration_seconds = _output_duration_seconds(conn, row)
    quote = generation_price_quote(
        conn,
        resolution="768P",
        duration_seconds=duration_seconds,
        quantity=1,
        user_id=owner_user_id,
    )
    snapshot = {
        "schemaVersion": 1,
        "resolution": "768P",
        "durationSeconds": duration_seconds,
        "quantity": 1,
        "priceTier": quote.price_tier,
        "unitCredits": quote.unit_credits,
        "estimatedCredits": quote.estimated_credits,
        "maxCredits": quote.estimated_credits,
        "estimatedPriceFen": quote.estimated_price_fen,
        "fingerprint": quote_fingerprint(
            prompt_version_id=prompt_version_id,
            shot_card_version_id=shot_card_version_id,
            script_version_id=script_version_id,
            first_frame_asset_id=first_frame_asset_id,
            resolution="768P",
            duration_seconds=duration_seconds,
            quantity=1,
        ),
        "frozenAt": datetime.now(UTC).isoformat(),
    }
    return snapshot


def _output_duration_seconds(conn: BusinessConnection, row: BusinessRow) -> int:
    """输出时长=源片时长（4–15 秒）。>15 秒源片不自动生成（见模块注释）."""
    duration_ms = conn.execute(
        "SELECT duration_ms FROM viral_videos WHERE platform = %s AND video_id = %s",
        (str(row["platform"]), str(row["video_id"])),
    ).fetchone()
    source_seconds = (
        0
        if duration_ms is None or duration_ms["duration_ms"] is None
        else round(int(duration_ms["duration_ms"]) / 1000)
    )
    return max(4, min(15, source_seconds or 4))


def confirm_pipeline(
    conn: BusinessConnection,
    *,
    pipeline_id: str,
    owner_user_id: str,
    quote_token: str,
    max_credits: int,
    expected_fingerprint: str,
) -> dict[str, object]:
    """确认并建批次（单一事务；调用方负责 BEGIN/COMMIT 与失败回滚）.

    锁序（评审 r8#2）：非锁定读 → 锁 projects → 锁 pipeline → 持锁重验。
    """
    from app.auth import CurrentUser
    from app.generation import (
        GenerationBatchRequest,
        create_generation_batch,
        lock_prompt_version,
    )

    # 1) 非锁定读拿 project（锁序要求先锁项目行）。
    pre = conn.execute(
        "SELECT id, project_id, owner_user_id, platform, video_id, status, quote_snapshot, "
        "stage_detail FROM replica_pipeline_tasks WHERE id = %s",
        (pipeline_id,),
    ).fetchone()
    if pre is None:
        raise _confirm_error("REPLICA_PIPELINE_NOT_FOUND", "编排任务不存在。", 404)
    if str(pre["owner_user_id"]) != owner_user_id:
        raise _confirm_error("REPLICA_PIPELINE_NOT_FOUND", "编排任务不存在。", 404)
    project_id = str(pre["project_id"])
    # 2) 锁项目行（全局锁序第一位）。
    conn.execute("SELECT id FROM projects WHERE id = %s FOR UPDATE", (project_id,))
    # 3) 锁编排行并持锁重验。
    locked = conn.execute(
        "SELECT * FROM replica_pipeline_tasks WHERE id = %s FOR UPDATE", (pipeline_id,)
    ).fetchone()
    if locked is None:
        raise _confirm_error("REPLICA_PIPELINE_NOT_FOUND", "编排任务不存在。", 404)
    row = cast(BusinessRow, locked)
    if str(row["status"]) != rp.STATUS_AWAIT_CONFIRM:
        raise _confirm_error("REPLICA_PIPELINE_NOT_AWAITING", "该任务当前不在待确认状态。")
    detail = _stage_detail(row)
    awaiting_since = row["awaiting_since"]
    if awaiting_since is not None:
        elapsed = conn.execute(
            "SELECT EXTRACT(EPOCH FROM (now() - %s::timestamptz)) AS seconds",
            (awaiting_since,),
        ).fetchone()
        if elapsed is not None and float(elapsed["seconds"]) > rp.CONFIRM_WINDOW_SECONDS:
            conn.execute(
                "UPDATE replica_pipeline_tasks SET status = %s, finished_at = now(), "
                "updated_at = now() WHERE id = %s AND status = %s",
                (rp.STATUS_EXPIRED, pipeline_id, rp.STATUS_AWAIT_CONFIRM),
            )
            raise _confirm_error("REPLICA_PIPELINE_EXPIRED", "确认窗口已过期，请重新发起。", 410)
    if detail.get("uncertain") is True:
        raise _confirm_error("REPLICA_PIPELINE_UNCERTAIN", "存在未对账的提交，暂时不可确认。")
    if detail.get("revokedReason"):
        raise _confirm_error("REPLICA_PIPELINE_REVOKED", "该任务已撤销。")
    snapshot = row["quote_snapshot"]
    if not isinstance(snapshot, dict):
        raise _confirm_error("REPLICA_PIPELINE_QUOTE_MISSING", "报价快照缺失，请重新发起。")
    # 4) 三方一致性：快照指纹 / 用户所见指纹 / 当前输入指纹。
    frozen_fingerprint = str(snapshot.get("fingerprint") or "")
    if not frozen_fingerprint or expected_fingerprint != frozen_fingerprint:
        raise _confirm_error(
            "REPLICA_PIPELINE_QUOTE_STALE", "内容已变化，请刷新确认页重新核价。", 422
        )
    current_fingerprint = _current_fingerprint(conn, row, detail)
    if current_fingerprint != frozen_fingerprint:
        raise _confirm_error(
            "REPLICA_PIPELINE_QUOTE_STALE", "内容已变化，请刷新确认页重新核价。", 422
        )
    frozen_max = snapshot.get("maxCredits")
    if not isinstance(frozen_max, int) or max_credits > frozen_max:
        raise _confirm_error(
            "REPLICA_PIPELINE_QUOTE_STALE", "授权金额与冻结报价不一致，请刷新后重试。", 422
        )
    duration_seconds = int(snapshot.get("durationSeconds") or 0)
    # 5) 时长档位能力：冻结时 generation_price_quote 已按 tariff 校验（长档
    #    未启用即 422 无报价），confirm 不重复查询同一档位目录——批次创建
    #    内部也会再走一次报价校验，不存在绕过路径。
    prompt_version_id = str(detail.get("promptVersionId") or "")
    if not prompt_version_id:
        raise _confirm_error("REPLICA_PIPELINE_PROMPT_MISSING", "提示词版本缺失。")
    first_frame_asset_id = _first_frame_asset_id(detail)
    if first_frame_asset_id is None:
        raise _confirm_error("REPLICA_PIPELINE_FIRST_FRAME_MISSING", "首帧缺失。")
    actor = CurrentUser(
        id=owner_user_id,
        username=owner_user_id,
        display_name=owner_user_id,
        role="customer",
    )
    lock_prompt_version(
        conn, project_id=project_id, prompt_version_id=prompt_version_id, actor=actor
    )
    batch = create_generation_batch(
        conn,
        project_id=project_id,
        actor=actor,
        request=GenerationBatchRequest(
            quote_token=quote_token,
            max_credits=max_credits,
            quantity=1,
            prompt_version_id=prompt_version_id,
            first_frame_asset_id=first_frame_asset_id,
            output_duration_seconds=duration_seconds,
            resolution="768P",
            ratio="adaptive",
            provider="metaso",
            idempotency_key=f"pipeline:{pipeline_id}:confirm",
        ),
        provider_client=None,
    )
    batch_id = str(batch.id)
    conn.execute(
        """
        UPDATE replica_pipeline_tasks SET status = %s, stage = %s, batch_id = %s,
            confirmed_at = now(), updated_at = now(), next_poll_at = now(),
            quote_token = %s
        WHERE id = %s AND status = %s
        """,
        (
            rp.STATUS_GENERATING,
            rp.STAGE_GENERATE,
            batch_id,
            quote_token,
            pipeline_id,
            rp.STATUS_AWAIT_CONFIRM,
        ),
    )
    return {"pipelineId": pipeline_id, "batchId": batch_id, "status": rp.STATUS_GENERATING}


def _current_fingerprint(
    conn: BusinessConnection, row: BusinessRow, detail: dict[str, object]
) -> str:
    prompt_version_id = str(detail.get("promptVersionId") or "")
    shot_card_version_id = str(detail.get("shotCardVersionId") or "")
    script_version_id = str(detail.get("scriptVersionId") or row["script_version_id"] or "")
    first_frame_asset_id = _first_frame_asset_id(detail) or ""
    snapshot = row["quote_snapshot"] if isinstance(row["quote_snapshot"], dict) else {}
    return quote_fingerprint(
        prompt_version_id=prompt_version_id,
        shot_card_version_id=shot_card_version_id,
        script_version_id=script_version_id,
        first_frame_asset_id=first_frame_asset_id,
        resolution=str(snapshot.get("resolution") or "768P"),
        duration_seconds=int(snapshot.get("durationSeconds") or 0),
        quantity=int(snapshot.get("quantity") or 1),
    )


def cancel_pipeline(
    conn: BusinessConnection, *, pipeline_id: str, owner_user_id: str
) -> dict[str, object]:
    """放弃：AWAIT_CONFIRM → CANCELLED（同一把行锁与数据库时钟裁决竞争）.

    已结算/未收口的费用按既有结算规则处理；分镜/文案/提示词全部保留，
    可随时转手动链路接管（方案 §4.3/§5.3）。
    """
    pre = conn.execute(
        "SELECT project_id FROM replica_pipeline_tasks WHERE id = %s AND owner_user_id = %s",
        (pipeline_id, owner_user_id),
    ).fetchone()
    if pre is None:
        raise _confirm_error("REPLICA_PIPELINE_NOT_FOUND", "编排任务不存在。", 404)
    conn.execute("SELECT id FROM projects WHERE id = %s FOR UPDATE", (str(pre["project_id"]),))
    moved = conn.execute(
        """
        UPDATE replica_pipeline_tasks SET status = %s, finished_at = now(),
            updated_at = now(), next_poll_at = now(),
            stage_detail = COALESCE(stage_detail, '{}'::jsonb)
                || jsonb_build_object('revokedReason', 'user_cancelled')
        WHERE id = %s AND owner_user_id = %s AND status = %s
        """,
        (rp.STATUS_CANCELLED, pipeline_id, owner_user_id, rp.STATUS_AWAIT_CONFIRM),
    ).rowcount
    if moved != 1:
        raise _confirm_error("REPLICA_PIPELINE_NOT_AWAITING", "该任务当前不在待确认状态。")
    return {"pipelineId": pipeline_id, "status": rp.STATUS_CANCELLED}


def expire_pipelines(conn: BusinessConnection, *, limit: int = 50) -> int:
    """独立过期扫描（方案 §3.2.3：72h 恒定计时，不排除挂起）.

    AWAIT_CONFIRM 进入时刻用不可变的 ``awaiting_since``（评审 A-r1#12：
    updated_at 会被挂起/恢复等动作刷新，期限不可依赖）；到期待确认 →
    EXPIRED（可重新发起，产物保留）。迁移前遗留行回退用 updated_at。
    """
    expired = conn.execute(
        """
        UPDATE replica_pipeline_tasks SET status = %s, finished_at = now(),
            updated_at = now(), next_poll_at = now()
        WHERE id IN (
            SELECT id FROM replica_pipeline_tasks
            WHERE status = %s
                AND COALESCE(awaiting_since, updated_at)
                    <= now() - (%s || ' seconds')::interval
            ORDER BY COALESCE(awaiting_since, updated_at) LIMIT %s
            FOR UPDATE SKIP LOCKED
        ) AND status = %s
        RETURNING id
        """,
        (
            rp.STATUS_EXPIRED,
            rp.STATUS_AWAIT_CONFIRM,
            str(rp.CONFIRM_WINDOW_SECONDS),
            limit,
            rp.STATUS_AWAIT_CONFIRM,
        ),
    ).fetchall()
    return len(expired)


def resume_suspended_pipelines(conn: BusinessConnection, *, limit: int = 50) -> int:
    """总开关重开后的恢复扫描（评审 B-r1#2）：挂起行里清屏障、累计时长、重轮询.

    与 expire_pipelines 同为独立扫描（不进轮询白名单——挂起行按设计不被领取）。
    调用方：worker 定时任务在开关开启时调用；恢复后行重新可领、断点续跑
    （挂起产物纳回版本链依赖子任务域既有恢复语义，M6b 迭代登记）。
    """
    resumed = conn.execute(
        """
        UPDATE replica_pipeline_tasks SET
            suspended_seconds_total = suspended_seconds_total +
                CASE WHEN suspended_at IS NULL THEN 0
                     ELSE FLOOR(EXTRACT(EPOCH FROM (now() - suspended_at)))::int END,
            suspended_at = NULL,
            next_poll_at = now(),
            updated_at = now()
        WHERE id IN (
            SELECT id FROM replica_pipeline_tasks
            WHERE suspended_at IS NOT NULL
            ORDER BY suspended_at LIMIT %s
            FOR UPDATE SKIP LOCKED
        )
        RETURNING id
        """,
        (limit,),
    ).fetchall()
    return len(resumed)


def confirm_quote_payload(row: BusinessRow) -> dict[str, object]:
    """GET 状态接口的报价展示载荷（M7 消费；也供前端确认卡渲染）."""
    snapshot = row["quote_snapshot"]
    if not isinstance(snapshot, dict):
        return {"quoteReady": False}
    payload: dict[str, object] = {"quoteReady": True}
    for key in (
        "resolution",
        "durationSeconds",
        "quantity",
        "priceTier",
        "unitCredits",
        "estimatedCredits",
        "maxCredits",
        "estimatedPriceFen",
        "fingerprint",
        "frozenAt",
    ):
        payload[key] = snapshot.get(key)
    return payload


__all__ = [
    "QUOTE_WINDOW_SECONDS",
    "cancel_pipeline",
    "confirm_pipeline",
    "confirm_quote_payload",
    "expire_pipelines",
    "freeze_confirm_quote",
    "quote_fingerprint",
]
