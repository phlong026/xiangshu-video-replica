"""发布标题与描述的独立 AI 建议。"""

from __future__ import annotations

import json
from hashlib import sha256
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.customer_fence import BusinessDbDep
from app.permissions import require_not_auditor
from app.publish_records import MAX_DESCRIPTION_LENGTH, MAX_TITLE_LENGTH
from app.script_rewrite import InvalidScriptTextOutput, load_script_text_route, request_script_text
from app.security_rate_limit import DIMENSION_PUBLISH_COPY_ACCOUNT, consume_rate_limit


class PublishCopyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    field: Literal["title", "description"]
    platform: Literal["抖音", "视频号", "快手", "小红书"]
    title: str = Field(default="", max_length=MAX_TITLE_LENGTH)
    description: str = Field(default="", max_length=MAX_DESCRIPTION_LENGTH)


class PublishCopySuggestion(BaseModel):
    text: str


def _validated_title(text: str) -> str:
    title = text.strip()
    try:
        length = len(title.encode("utf-16-le")) // 2
    except UnicodeEncodeError as exc:
        raise InvalidScriptTextOutput("发布标题含无效字符") from exc
    if not title or len(title.splitlines()) != 1 or length > MAX_TITLE_LENGTH:
        raise InvalidScriptTextOutput("发布标题须为一行有效文本")
    return title


def optimize_publish_copy(request: PublishCopyRequest, db: BusinessDbDep) -> PublishCopySuggestion:
    # 模型调用在短事务外执行，避免阻塞会话与账号的其他操作。
    with db.write() as (conn, actor):
        require_not_auditor(
            conn,
            actor=actor,
            action="publish.copy.optimize",
            entity_type="user",
            entity_id=actor.id,
        )
        if not (request.title or request.description):
            raise HTTPException(
                422,
                detail={"code": "PUBLISH_COPY_EMPTY", "message": "请先填写发布标题或描述。"},
            )
        try:
            primary, fallback = load_script_text_route(conn)
        except HTTPException as exc:
            raise HTTPException(
                503,
                detail={
                    "code": "PUBLISH_COPY_UNAVAILABLE",
                    "message": "发布文案 AI 服务暂不可用。",
                },
            ) from exc
        budget = consume_rate_limit(
            conn.raw,
            dimension=DIMENSION_PUBLISH_COPY_ACCOUNT,
            identifier=sha256(actor.id.encode()).hexdigest(),
            limit=20,
            window_seconds=60,
        )
    if not budget.allowed:
        raise HTTPException(
            429,
            detail={"code": "RATE_LIMITED", "message": "AI 优化使用频繁，请稍后重试。"},
            headers={"Retry-After": str(budget.retry_after_seconds)},
        )

    is_title = request.field == "title"
    try:
        result = request_script_text(
            primary,
            fallback,
            purpose="publish_title" if is_title else "publish_description",
            source_text=json.dumps(
                {"发布标题": request.title, "发布描述": request.description}, ensure_ascii=False
            ),
            instructions=f"目标发布平台为{request.platform}。另一字段仅供理解内容。本次仅返回"
            + ("标题。" if is_title else "描述。"),
            validate_output=_validated_title if is_title else None,
        )
        text = result.rewritten_text.strip()
    except InvalidScriptTextOutput:
        text = ""
    except HTTPException as exc:
        raise HTTPException(
            exc.status_code,
            detail={"code": "PUBLISH_COPY_FAILED", "message": "AI 优化暂时失败，请稍后重试。"},
        ) from exc
    if not text or (not is_title and len(text.encode("utf-16-le")) // 2 > MAX_DESCRIPTION_LENGTH):
        raise HTTPException(
            502,
            detail={"code": "PUBLISH_COPY_INVALID", "message": "未生成可用文案，请重新优化。"},
        )
    return PublishCopySuggestion(text=text)
