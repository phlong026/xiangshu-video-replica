"""SUBMISSION_UNCERTAIN 自动处置参数的读写端点（2026-11-11 极简化 WP-C）.

- ``GET /api/control/uncertain-auto-reopen``：任意管理员可读；
- ``PUT /api/control/uncertain-auto-reopen``：写操作走统一写契约（确认 + 原因 +
  幂等键），审计 ``uncertain.auto_reopen_params.update``，记录改前改后值。

两个参数控制转写（asr）与改写（rewrite）任务的无人值守处置：
- ``threshold_minutes``：提交结果不可证明后多少分钟自动重开新一轮（0 = 关闭自动重开）；
- ``max_reopens``：同一任务自动重开次数上限，耗尽落 FAILED 并在观测页可见。
"""

from __future__ import annotations

import json
import uuid

import psycopg
from fastapi import APIRouter, Request, Response
from pydantic import Field

from app.admin_auth_routes import AdminReader, SettingsWriter
from app.admin_customer_routes import _write_with_idempotency
from app.admin_write_contract import AdminWriteContract as AdminWriteRequest
from app.admin_write_contract import http_error as _http
from app.db_pg import MissingDatabaseConfigError, pg_transaction
from app.uncertain_auto_reopen import (
    UncertainAutoReopenParams,
    read_uncertain_auto_reopen_params,
)

router = APIRouter(prefix="/api/control", tags=["admin-uncertain-auto-reopen"])

_UNAVAILABLE_CODE = "UNCERTAIN_AUTO_REOPEN_UNAVAILABLE"
_UNAVAILABLE_MESSAGE = "自动处置参数需要 PostgreSQL 运行时。"
PARAMS_RULE = (
    "转写与改写任务提交结果不可证明（SUBMISSION_UNCERTAIN）时不再等人工核对："
    "超过阈值分钟数自动重开新一轮计费并重跑；同一任务重开次数达到上限后落 FAILED "
    "并在观测页可见。阈值或上限任一填 0 表示关闭自动重开（保持人工兜底，"
    "不做耗尽转 FAILED）；上限是运营可调值（默认 2，最大 10），不是硬限制。"
    "两个参数对转写与改写两类任务同时生效。"
)


class UncertainAutoReopenRequest(AdminWriteRequest):
    threshold_minutes: int = Field(ge=0, le=1440)
    max_reopens: int = Field(ge=0, le=10)


def _response(params: UncertainAutoReopenParams) -> dict[str, object]:
    return {**params, "rule": PARAMS_RULE}


@router.get("/uncertain-auto-reopen")
def get_uncertain_auto_reopen(actor: AdminReader) -> dict[str, object]:
    del actor
    try:
        with pg_transaction() as conn:
            return _response(read_uncertain_auto_reopen_params(conn))
    except (RuntimeError, MissingDatabaseConfigError) as exc:
        raise _http(503, _UNAVAILABLE_CODE, _UNAVAILABLE_MESSAGE) from exc


@router.put("/uncertain-auto-reopen")
def update_uncertain_auto_reopen(
    body: UncertainAutoReopenRequest,
    request: Request,
    response: Response,
    actor: SettingsWriter,
) -> dict[str, object]:
    def business(conn: psycopg.Connection, request_id: str) -> dict[str, object]:
        before = read_uncertain_auto_reopen_params(conn)
        conn.execute(
            "INSERT INTO runtime_settings (id, uncertain_auto_reopen_minutes, "
            "uncertain_auto_reopen_max) VALUES (1, %s, %s) "
            "ON CONFLICT (id) DO UPDATE SET "
            "uncertain_auto_reopen_minutes = EXCLUDED.uncertain_auto_reopen_minutes, "
            "uncertain_auto_reopen_max = EXCLUDED.uncertain_auto_reopen_max",
            (body.threshold_minutes, body.max_reopens),
        )
        conn.execute(
            "INSERT INTO audit_logs (id, actor_user_id, action, entity_type, "
            "entity_id, metadata_json) VALUES (%s,%s,'uncertain.auto_reopen_params.update',"
            "'runtime_settings','uncertain_auto_reopen',%s)",
            (
                str(uuid.uuid4()),
                actor.user_id,
                json.dumps(
                    {
                        "before": before,
                        "after": {
                            "threshold_minutes": body.threshold_minutes,
                            "max_reopens": body.max_reopens,
                        },
                        "reason": body.reason.strip(),
                        "request_id": request_id,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            ),
        )
        return {
            **_response(
                {
                    "threshold_minutes": body.threshold_minutes,
                    "max_reopens": body.max_reopens,
                }
            ),
            "request_id": request_id,
        }

    return _write_with_idempotency(
        request,
        response,
        actor,
        body,
        business,
        success_status=200,
        unavailable_code=_UNAVAILABLE_CODE,
        unavailable_message=_UNAVAILABLE_MESSAGE,
    )
