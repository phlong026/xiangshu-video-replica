"""客户运营参数的读写端点（整改 T29）。

- ``GET /api/control/customer-ops-params``：任意管理员可读；
- ``PUT /api/control/customer-ops-params``：写操作走统一写契约（确认 + 原因 + 幂等键），
  审计 ``customer.ops_params.update``，记录改前改后值。
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
from app.customer_ops_params import (
    LOW_BALANCE_THRESHOLD_MAX,
    CustomerOpsParams,
    read_customer_ops_params,
)
from app.db_pg import MissingDatabaseConfigError, pg_transaction

router = APIRouter(prefix="/api/control", tags=["admin-customer-ops-params"])

_UNAVAILABLE_CODE = "CUSTOMER_OPS_PARAMS_UNAVAILABLE"
_UNAVAILABLE_MESSAGE = "客户运营参数需要 PostgreSQL 运行时。"
PARAMS_RULE = (
    "低余额阈值：可用积分严格小于该值算「余额不足」，同时高于该值才计健康分的余额加分；"
    "健康分预警线：低于该值的客户在客户运营里标红。两个页面共用这份参数。"
)


class CustomerOpsParamsRequest(AdminWriteRequest):
    low_balance_threshold: int = Field(ge=0, le=LOW_BALANCE_THRESHOLD_MAX)
    health_alert_score: int = Field(ge=1, le=100)


def _response(params: CustomerOpsParams) -> dict[str, object]:
    return {**params, "rule": PARAMS_RULE}


@router.get("/customer-ops-params")
def get_customer_ops_params(actor: AdminReader) -> dict[str, object]:
    del actor
    try:
        with pg_transaction() as conn:
            return _response(read_customer_ops_params(conn))
    except (RuntimeError, MissingDatabaseConfigError) as exc:
        raise _http(503, _UNAVAILABLE_CODE, _UNAVAILABLE_MESSAGE) from exc


@router.put("/customer-ops-params")
def update_customer_ops_params(
    body: CustomerOpsParamsRequest,
    request: Request,
    response: Response,
    actor: SettingsWriter,
) -> dict[str, object]:
    def business(conn: psycopg.Connection, request_id: str) -> dict[str, object]:
        before = read_customer_ops_params(conn)
        conn.execute(
            "INSERT INTO runtime_settings (id, customer_low_balance_threshold, "
            "customer_health_alert_score) VALUES (1, %s, %s) "
            "ON CONFLICT (id) DO UPDATE SET "
            "customer_low_balance_threshold = EXCLUDED.customer_low_balance_threshold, "
            "customer_health_alert_score = EXCLUDED.customer_health_alert_score",
            (body.low_balance_threshold, body.health_alert_score),
        )
        conn.execute(
            "INSERT INTO audit_logs (id, actor_user_id, action, entity_type, "
            "entity_id, metadata_json) VALUES (%s,%s,'customer.ops_params.update',"
            "'runtime_settings','customer_ops_params',%s)",
            (
                str(uuid.uuid4()),
                actor.user_id,
                json.dumps(
                    {
                        "before": before,
                        "after": {
                            "low_balance_threshold": body.low_balance_threshold,
                            "health_alert_score": body.health_alert_score,
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
                    "low_balance_threshold": body.low_balance_threshold,
                    "health_alert_score": body.health_alert_score,
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
