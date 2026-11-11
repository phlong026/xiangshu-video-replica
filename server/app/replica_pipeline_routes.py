"""全自动复刻编排器 M7：客户 API 路由与权限.

端点（方案 §7）：
- POST   /api/viral/replica-pipelines                    发起（人物预检+开关+预处理确认）
- GET    /api/viral/replica-pipelines/{pipeline_id}      状态/阶段/报价/产物断点
- POST   /api/viral/replica-pipelines/{pipeline_id}/confirm          确认并生成
- POST   /api/viral/replica-pipelines/{pipeline_id}/cancel           放弃
- POST   /api/viral/replica-pipelines/{pipeline_id}/regenerate       重新授权重生成
- POST   /api/viral/replica-pipelines/{pipeline_id}/accept-partial   接受部分成果

权限口径：发起/查询/操作一律 owner 本人（confirm 在锁内二次校验）；审计员
拒绝（require_not_auditor）；总开关关闭拒绝**新发起**（既有在途由 drive 挂起，
见 replica_pipeline_stages.drive_pipeline_once）。对外文案中性（无供应商名）。
"""

from __future__ import annotations

import hashlib
from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app import replica_pipeline as rp
from app import replica_pipeline_confirm as confirm
from app import replica_pipeline_generation as generation
from app.auth import AuthenticatedUser, Database
from app.customer_fence import BusinessDbDep
from app.db_portable import BusinessRow
from app.permissions import require_not_auditor

router = APIRouter(prefix="/api/viral", tags=["viral"])


class ReplicaPipelineInitiateRequest(BaseModel):
    """发起请求：预处理费用披露必须先经用户确认（方案 §4.3）。"""

    platform: Literal["douyin", "xiaohongshu", "wechat_channels"]
    video_id: str = Field(min_length=1, max_length=200)
    character_version_id: str = Field(min_length=1, max_length=200)
    idempotency_key: str = Field(min_length=8, max_length=128)
    preprocess_acknowledged: bool


class ReplicaPipelineConfirmRequest(BaseModel):
    quote_token: str = Field(min_length=1, max_length=8192)
    max_credits: int = Field(ge=1, le=2_147_483_647)
    fingerprint: str = Field(min_length=8, max_length=128)


class ReplicaPipelineRegenerateRequest(BaseModel):
    quote_token: str = Field(min_length=1, max_length=8192)
    max_credits: int = Field(ge=1, le=2_147_483_647)


class ReplicaPipelineAcceptPartialRequest(BaseModel):
    pass


@router.post("/replica-pipelines", status_code=202)
def initiate_replica_pipeline(
    request: ReplicaPipelineInitiateRequest,
    db: BusinessDbDep,
) -> dict[str, object]:
    """发起全自动复刻（幂等受理；人物预检在收费拆解前）."""
    from fastapi import HTTPException

    with db.write() as (conn, actor):
        require_not_auditor(
            conn,
            actor=actor,
            action="replica_pipeline.initiate",
            entity_type="viral_video",
            entity_id=f"{request.platform}:{request.video_id}",
        )
        if not request.preprocess_acknowledged:
            raise HTTPException(
                422,
                detail={
                    "code": "REPLICA_PREPROCESS_NOT_ACKNOWLEDGED",
                    "message": "请先确认预处理费用说明。",
                },
            )
        try:
            rp.require_pipeline_enabled(conn)
        except rp.ReplicaPipelineError as exc:
            raise HTTPException(exc.status_code, detail={"code": exc.code, "message": exc.message})
        # 自动生成只支持 4–15 秒输出（批次请求硬约束）：超长源片在此拒绝，
        # 避免先收费拆解/首帧、再在 COMPOSING 必然失败（评审 M8-r2#2）。
        video_row = conn.execute(
            "SELECT duration_ms FROM viral_videos WHERE platform = %s AND video_id = %s",
            (request.platform, request.video_id),
        ).fetchone()
        if video_row is not None and video_row["duration_ms"] is not None:
            if int(video_row["duration_ms"]) > 15_000:
                raise HTTPException(
                    422,
                    detail={
                        "code": "SOURCE_TOO_LONG_FOR_GENERATION",
                        "message": "该视频超过 15 秒，暂不支持全自动生成，请转手动复刻。",
                    },
                )
        request_hash = hashlib.sha256(
            f"{request.platform}|{request.video_id}|{request.character_version_id}".encode()
        ).hexdigest()
        try:
            result = rp.enqueue_replica_pipeline(
                conn,
                owner_user_id=actor.id,
                platform=request.platform,
                video_id=request.video_id,
                character_version_id=request.character_version_id,
                idempotency_key=request.idempotency_key,
                request_hash=request_hash,
            )
        except rp.ReplicaPipelineError as exc:
            raise HTTPException(exc.status_code, detail={"code": exc.code, "message": exc.message})
        # 人物可用性校验（评审 B-r3#1：复用既有人物域判定，而非自造 SQL）：
        # enqueue 已在**同一事务**建好项目，用项目上下文跑权威校验——
        # 发布快照/参考图/授权凭据不满足时抛 HTTPException，事务整体回滚
        # （项目与 pipeline 行一并撤销），绝不带着不可用人物进入收费链路。
        from app.project_character_selection import (
            list_available_project_character_versions,
        )

        try:
            options = list_available_project_character_versions(
                conn,
                actor=actor,
                project_id=str(result["projectId"]),
                character_version_id=request.character_version_id,
            )
        except HTTPException:
            raise
        if not options:
            raise HTTPException(
                422,
                detail={
                    "code": "REPLICA_CHARACTER_NOT_FOUND",
                    "message": "所选人物不存在或不可用。",
                },
            )
        return result


@router.get("/replica-pipelines/characters")
def list_pipeline_characters(conn: Database, actor: AuthenticatedUser) -> dict[str, object]:
    """发起弹层的人物候选（owner 的可用已发布人物版本）.

    归属链、发布状态与授权状态在 SQL 里一次过滤（域权威判定仍由
    list_available_project_character_versions 在受理时兜底）。
    """
    rows = conn.execute(
        """
        SELECT cv.id, cv.version_number, p.name
        FROM character_versions cv
        JOIN character_personas p ON p.id = cv.persona_id
        JOIN person_identities pi ON pi.id = p.identity_id
        WHERE pi.owner_user_id = %s AND pi.status = 'ACTIVE'
            AND pi.authorization_status = 'AUTHORIZED'
            AND (pi.authorization_expires_at IS NULL
                 OR pi.authorization_expires_at::timestamptz > now())
            AND cv.status = 'PUBLISHED'
            AND cv.published_at IS NOT NULL
            AND cv.publication_hash IS NOT NULL
            AND cv.publication_snapshot_json IS NOT NULL
        ORDER BY cv.created_at DESC
        LIMIT 50
        """,
        (actor.id,),
    ).fetchall()
    return {
        "characters": [
            {
                "characterVersionId": str(row["id"]),
                "name": str(row["name"]),
                "versionNumber": int(row["version_number"]),
            }
            for row in rows
        ]
    }


@router.post("/replica-pipelines/{pipeline_id}/quote")
def quote_replica_pipeline(pipeline_id: str, db: BusinessDbDep) -> dict[str, object]:
    """确认卡报价（300s 凭证）：仅待确认、owner、未过 72h 窗口.

    方案 §4.1：确认必须携带**当前有效**报价凭证；确认卡过期重核价走本端点。
    """
    from fastapi import HTTPException

    expired_now = False
    with db.write() as (conn, actor):
        row = conn.execute(
            "SELECT * FROM replica_pipeline_tasks WHERE id = %s AND owner_user_id = %s",
            (pipeline_id, actor.id),
        ).fetchone()
        if row is None:
            raise HTTPException(
                404, detail={"code": "REPLICA_PIPELINE_NOT_FOUND", "message": "编排任务不存在。"}
            )
        if str(row["status"]) != rp.STATUS_AWAIT_CONFIRM:
            raise HTTPException(
                409,
                detail={"code": "REPLICA_PIPELINE_NOT_AWAITING", "message": "当前不在待确认状态。"},
            )
        snapshot = row["quote_snapshot"]
        if not isinstance(snapshot, dict):
            raise HTTPException(
                409,
                detail={"code": "REPLICA_PIPELINE_QUOTE_MISSING", "message": "报价快照缺失。"},
            )
        # 72h 确认期限（评审 M8-r1#7）：过期直接拒绝报价，不依赖扫描先跑。
        awaiting_since = row["awaiting_since"]
        if awaiting_since is not None:
            elapsed = conn.execute(
                "SELECT EXTRACT(EPOCH FROM (now() - %s::timestamptz)) AS seconds",
                (awaiting_since,),
            ).fetchone()
            if elapsed is not None and float(elapsed["seconds"]) > rp.CONFIRM_WINDOW_SECONDS:
                # 先落 EXPIRED 并提交（评审 M8-r2#5：异常回滚会撤销状态迁移），
                # 退出事务后再返回 410。
                conn.execute(
                    "UPDATE replica_pipeline_tasks SET status = %s, finished_at = now(), "
                    "updated_at = now() WHERE id = %s AND status = %s",
                    (rp.STATUS_EXPIRED, pipeline_id, rp.STATUS_AWAIT_CONFIRM),
                )
                expired_now = True
        from app.generation import generation_price_quote

        resolution = str(snapshot.get("resolution") or "768P")
        if resolution not in ("768P", "2K"):
            resolution = "768P"
        quote = generation_price_quote(
            conn,
            resolution=resolution,  # type: ignore[arg-type]
            duration_seconds=int(snapshot.get("durationSeconds") or 0),
            quantity=int(snapshot.get("quantity") or 1),
            user_id=actor.id,
        )
        frozen_max = int(snapshot.get("maxCredits") or 0)
        if frozen_max > 0 and quote.estimated_credits > frozen_max:
            # 冻结上限是用户在确认页授权过的金额（评审 M8-r2#3）：现价上涨
            # 不能静默扩大授权——返回专用错误，用户可放弃后重新发起（新报价
            # 会重新冻结上限）。不提供「原地涨价确认」通道。
            raise HTTPException(
                409,
                detail={
                    "code": "REPLICA_PIPELINE_PRICE_CHANGED",
                    "message": "价格已变化，请放弃后重新发起以重新确认费用。",
                },
            )
        quote_response = {
            "quoteToken": quote.quote_token,
            "maxCredits": min(quote.estimated_credits, frozen_max or quote.estimated_credits),
            "estimatedCredits": quote.estimated_credits,
            "priceTier": quote.price_tier,
            "durationSeconds": quote.duration_seconds,
            "fingerprint": str(snapshot.get("fingerprint") or ""),
        }
    if expired_now:
        raise HTTPException(
            410, detail={"code": "REPLICA_PIPELINE_EXPIRED", "message": "确认窗口已过期。"}
        )
    return quote_response


@router.get("/replica-pipelines/{pipeline_id}")
def read_replica_pipeline(
    pipeline_id: str, conn: Database, actor: AuthenticatedUser
) -> dict[str, object]:
    """状态查询（owner 本人）：阶段/报价/产物断点/失败原因."""
    from fastapi import HTTPException

    row = conn.execute(
        "SELECT * FROM replica_pipeline_tasks WHERE id = %s AND owner_user_id = %s",
        (pipeline_id, actor.id),
    ).fetchone()
    if row is None:
        raise HTTPException(
            404, detail={"code": "REPLICA_PIPELINE_NOT_FOUND", "message": "编排任务不存在。"}
        )
    from app.replica_pipeline_stages import _stage_detail  # noqa: PLC0415

    detail = _stage_detail(row)
    payload: dict[str, object] = {
        "id": str(row["id"]),
        "projectId": str(row["project_id"]),
        "platform": str(row["platform"]),
        "videoId": str(row["video_id"]),
        "status": str(row["status"]),
        "stage": str(row["stage"]),
        "createdAt": str(row["created_at"]),
        "awaitingSince": None if row["awaiting_since"] is None else str(row["awaiting_since"]),
        "stageDetail": detail,
    }
    if detail.get("promptVersionId"):
        payload["promptVersionId"] = detail.get("promptVersionId")
    first_frame = detail.get("firstFrame")
    if isinstance(first_frame, dict) and first_frame.get("firstFrameAssetId"):
        payload["firstFrameAssetId"] = first_frame.get("firstFrameAssetId")
    payload.update(confirm.confirm_quote_payload(row))
    if row["batch_id"]:
        payload["batchId"] = str(row["batch_id"])
    # 确认卡核对内容（评审 M8-r1#2）：提示词/文案正文 + 三栏费用。
    _attach_review_material(conn, row, detail, payload)
    return payload


def _attach_review_material(
    conn: Database,
    row: BusinessRow,
    detail: dict[str, object],
    payload: dict[str, object],
) -> None:
    """确认卡核对产物与费用三栏（方案 §7/§4.3）.

    三栏：已结算（charged_credits 汇总）/ 仍预占（reserved_credits 汇总，
    按尚未终态的计费操作）/ 本次新增（冻结报价 estimatedCredits）。
    计费按子任务 source_id 关联（billing_operations 无 project 维度）。
    """
    prompt_version_id = detail.get("promptVersionId")
    if isinstance(prompt_version_id, str) and prompt_version_id:
        version = conn.execute(
            "SELECT payload_json FROM versions WHERE id = %s", (prompt_version_id,)
        ).fetchone()
        if version is not None:
            prompt_payload = version["payload_json"]
            if isinstance(prompt_payload, str):
                import json as _json

                try:
                    prompt_payload = _json.loads(prompt_payload)
                except ValueError:
                    prompt_payload = None
            if isinstance(prompt_payload, dict) and isinstance(
                prompt_payload.get("prompt_text"), str
            ):
                payload["promptText"] = prompt_payload["prompt_text"]
    script_version_id = detail.get("scriptVersionId")
    if isinstance(script_version_id, str) and script_version_id:
        version = conn.execute(
            "SELECT payload_json FROM versions WHERE id = %s", (script_version_id,)
        ).fetchone()
        if version is not None:
            script_payload = version["payload_json"]
            if isinstance(script_payload, str):
                import json as _json

                try:
                    script_payload = _json.loads(script_payload)
                except ValueError:
                    script_payload = None
            if isinstance(script_payload, dict):
                text = script_payload.get("full_text") or script_payload.get("text")
                if isinstance(text, str):
                    payload["scriptText"] = text
    # 三栏费用（按预处理子任务 source 汇总）。
    source_ids = [
        value
        for value in (
            str(row["analysis_task_id"]) if row["analysis_task_id"] else None,
            str(row["first_frame_task_id"]) if row["first_frame_task_id"] else None,
        )
        if value
    ]
    # 重试替换掉的旧尝试也计入（评审 M8-r2#4）：费用汇总必须覆盖全部已收费
    # 尝试，否则「旧尝试已结算、新尝试成功」时确认卡会丢失已结算金额。
    history = detail.get("history")
    if isinstance(history, list):
        source_ids.extend(str(value) for value in history if value)
    source_ids = list(dict.fromkeys(source_ids))
    settled = 0
    reserved = 0
    if source_ids:
        sums = conn.execute(
            """
            SELECT COALESCE(SUM(CASE WHEN state = 'SUCCEEDED' THEN charged_credits END), 0)
                       AS settled,
                   COALESCE(SUM(CASE WHEN state NOT IN ('SUCCEEDED','CANCELLED','FAILED')
                       THEN reserved_credits END), 0) AS reserved
            FROM billing_operations WHERE source_id = ANY(%s)
            """,
            (source_ids,),
        ).fetchone()
        if sums is not None:
            settled = int(sums["settled"] or 0)
            reserved = int(sums["reserved"] or 0)
    payload["feeSummary"] = {
        "settledCredits": settled,
        "reservedCredits": reserved,
        "newCredits": int(
            (row["quote_snapshot"] or {}).get("estimatedCredits", 0)
            if isinstance(row["quote_snapshot"], dict)
            else 0
        ),
    }


@router.post("/replica-pipelines/{pipeline_id}/confirm")
def confirm_replica_pipeline(
    pipeline_id: str, request: ReplicaPipelineConfirmRequest, db: BusinessDbDep
) -> dict[str, object]:
    """确认并建批次（单一事务：锁序 projects→pipeline→校验→预占）."""
    from fastapi import HTTPException

    with db.write() as (conn, actor):
        require_not_auditor(
            conn,
            actor=actor,
            action="replica_pipeline.confirm",
            entity_type="replica_pipeline",
            entity_id=pipeline_id,
        )
        try:
            rp.require_pipeline_enabled(conn)
        except rp.ReplicaPipelineError as exc:
            raise HTTPException(exc.status_code, detail={"code": exc.code, "message": exc.message})
        try:
            return confirm.confirm_pipeline(
                conn,
                pipeline_id=pipeline_id,
                owner_user_id=actor.id,
                quote_token=request.quote_token,
                max_credits=request.max_credits,
                expected_fingerprint=request.fingerprint,
            )
        except rp.ReplicaPipelineError as exc:
            raise HTTPException(exc.status_code, detail={"code": exc.code, "message": exc.message})


@router.post("/replica-pipelines/{pipeline_id}/cancel")
def cancel_replica_pipeline(pipeline_id: str, db: BusinessDbDep) -> dict[str, object]:
    from fastapi import HTTPException

    with db.write() as (conn, actor):
        try:
            return confirm.cancel_pipeline(conn, pipeline_id=pipeline_id, owner_user_id=actor.id)
        except rp.ReplicaPipelineError as exc:
            raise HTTPException(exc.status_code, detail={"code": exc.code, "message": exc.message})


@router.post("/replica-pipelines/{pipeline_id}/regenerate")
def regenerate_replica_pipeline(
    pipeline_id: str, request: ReplicaPipelineRegenerateRequest, db: BusinessDbDep
) -> dict[str, object]:
    from fastapi import HTTPException

    with db.write() as (conn, actor):
        require_not_auditor(
            conn,
            actor=actor,
            action="replica_pipeline.regenerate",
            entity_type="replica_pipeline",
            entity_id=pipeline_id,
        )
        try:
            rp.require_pipeline_enabled(conn)
        except rp.ReplicaPipelineError as exc:
            raise HTTPException(exc.status_code, detail={"code": exc.code, "message": exc.message})
        try:
            return generation.regenerate_pipeline(
                conn,
                pipeline_id=pipeline_id,
                owner_user_id=actor.id,
                quote_token=request.quote_token,
                max_credits=request.max_credits,
            )
        except rp.ReplicaPipelineError as exc:
            raise HTTPException(exc.status_code, detail={"code": exc.code, "message": exc.message})


@router.post("/replica-pipelines/{pipeline_id}/accept-partial")
def accept_partial_replica_pipeline(pipeline_id: str, db: BusinessDbDep) -> dict[str, object]:
    from fastapi import HTTPException

    with db.write() as (conn, actor):
        try:
            return generation.accept_partial(conn, pipeline_id=pipeline_id, owner_user_id=actor.id)
        except rp.ReplicaPipelineError as exc:
            raise HTTPException(exc.status_code, detail={"code": exc.code, "message": exc.message})


__all__ = ["router", "ReplicaPipelineInitiateRequest"]
