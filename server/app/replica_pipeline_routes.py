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
            AND cv.status = 'PUBLISHED'
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
        return {
            "quoteToken": quote.quote_token,
            "maxCredits": quote.estimated_credits,
            "estimatedCredits": quote.estimated_credits,
            "priceTier": quote.price_tier,
            "durationSeconds": quote.duration_seconds,
            "fingerprint": str(snapshot.get("fingerprint") or ""),
        }


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
    return payload


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
