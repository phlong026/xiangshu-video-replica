"""Create/query optional H3 editing jobs; provider calls belong to the worker."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from app.analysis import ApilioGemini
from app.auth import ReadDatabase, ShortAuthUser
from app.customer_fence import BusinessDbDep
from app.db_portable import BusinessConnection
from app.prompt_optimizer import PromptOptimizeRequest, enqueue, task_result
from app.text_gemini import load_gemini_text_provider

router = APIRouter(prefix="/api/prompt-optimizations", tags=["prompts"])


def get_prompt_optimizer_providers(conn: BusinessConnection) -> tuple[ApilioGemini, ApilioGemini]:
    primary = load_gemini_text_provider(conn, timeout_seconds=120)
    if primary is None:
        raise HTTPException(
            503,
            detail={"code": "PROMPT_OPTIMIZER_UNAVAILABLE", "message": "请先配置提示词优化服务。"},
        )
    return primary, ApilioGemini(
        api_key=primary.api_key,
        base_url=primary.base_url,
        model="gemini-3.8-flash",
        transport=primary.transport,
    )


@router.post("", status_code=202)
def create_prompt_optimization(request: PromptOptimizeRequest, db: BusinessDbDep) -> dict[str, Any]:
    with db.write() as (conn, actor):
        return enqueue(conn, actor=actor, request=request)


@router.get("/{task_id}")
def read_prompt_optimization(
    task_id: str, conn: ReadDatabase, actor: ShortAuthUser
) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM prompt_optimization_receipts WHERE id=%s AND owner_user_id=%s",
        (task_id, actor.id),
    ).fetchone()
    if row is None:
        raise HTTPException(404, detail={"code": "PROMPT_OPTIMIZATION_NOT_FOUND"})
    return task_result(row)
