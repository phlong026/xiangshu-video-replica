"""ADM-21 工单/反馈 v1（管理端代录）。

- ``GET /api/control/support-tickets``：清单（状态/客户/类目筛选，分页）；
- ``POST /api/control/support-tickets``：代录登记（客户可空 = 平台侧问题）；
- ``PATCH /api/control/support-tickets/{id}``：处理——置状态 / 指派负责人 /
  填结论文本；状态机只允许 open→processing→resolved 与直接关单
  （open→resolved），不允许复活（新问题请新建工单，保持历史干净）。

写入走四段契约；每次变更落 ``support_ticket.*`` 审计。
"""

from __future__ import annotations

import json
import logging
import re
import uuid

import psycopg
from fastapi import APIRouter, Request, Response

from app.admin_auth_routes import AdminReader, AdminWriter
from app.admin_customer_routes import _write_with_idempotency
from app.admin_write_contract import AdminWriteContract as AdminWriteRequest
from app.admin_write_contract import http_error as _http
from app.db_pg import MissingDatabaseConfigError, pg_transaction

router = APIRouter(prefix="/api/control", tags=["admin-support-tickets"])
logger = logging.getLogger(__name__)

CATEGORIES = ("billing", "generation", "publish", "content", "account", "other")
STATUSES = ("open", "processing", "resolved")
PRIORITIES = ("P0", "P1", "P2", "P3")
# 关联对象：生成任务号或充值订单号，与迁移里的 CHECK 同一形状。
_RELATED_REF = re.compile(r"^(task|order):[A-Za-z0-9_:.-]{1,100}$")

_CATEGORY_LABELS = {
    "billing": "计费/资金",
    "generation": "生成问题",
    "publish": "发布问题",
    "content": "内容/素材",
    "account": "账号/设备",
    "other": "其他",
}
_STATUS_LABELS = {"open": "待处理", "processing": "处理中", "resolved": "已解决"}

MAX_SUMMARY = 500
MAX_RESOLUTION = 2000

_UNAVAILABLE_CODE = "SUPPORT_TICKET_UNAVAILABLE"
_UNAVAILABLE_MESSAGE = "工单功能需要 PostgreSQL 运行时。"


class TicketCreateRequest(AdminWriteRequest):
    target_user_id: str = ""
    category: str = "other"
    summary: str = ""
    priority: str = "P2"
    related_ref: str = ""


class TicketUpdateRequest(AdminWriteRequest):
    status: str = ""
    handler_user_id: str = ""
    resolution: str = ""
    priority: str = ""


def _validated_related_ref(value: str) -> str:
    ref = value.strip()
    if ref and not _RELATED_REF.match(ref):
        raise _http(422, "TICKET_VALIDATION_FAILED", "关联对象需为生成任务号或充值订单号。")
    return ref


def _ticket_row(row: tuple[object, ...]) -> dict[str, object]:
    return {
        "id": str(row[0]),
        "target_user_id": "" if row[1] is None else str(row[1]),
        "target_username": str(row[2] or ""),
        "target_display_name": str(row[3] or ""),
        "category": str(row[4]),
        "category_label": _CATEGORY_LABELS.get(str(row[4]), str(row[4])),
        "summary": str(row[5]),
        "status": str(row[6]),
        "status_label": _STATUS_LABELS.get(str(row[6]), str(row[6])),
        "handler_user_id": "" if row[7] is None else str(row[7]),
        "handler_username": str(row[8] or ""),
        "resolution": str(row[9] or ""),
        "created_by": str(row[10]),
        "created_at": (row[11].isoformat() if hasattr(row[11], "isoformat") else str(row[11])),
        "updated_at": (row[12].isoformat() if hasattr(row[12], "isoformat") else str(row[12])),
        "priority": str(row[13]),
        "related_ref": str(row[14] or ""),
    }


_TICKET_SELECT = """
    SELECT st.id, st.target_user_id, tu.username, tu.display_name,
           st.category, st.summary, st.status, st.handler_user_id,
           hu.username, st.resolution, st.created_by_user_id,
           st.created_at, st.updated_at, st.priority, st.related_ref
    FROM support_tickets st
    LEFT JOIN users tu ON tu.id = st.target_user_id
    LEFT JOIN users hu ON hu.id = st.handler_user_id
"""


@router.get("/support-tickets")
def list_support_tickets(
    actor: AdminReader,
    status: str = "",
    category: str = "",
    username: str = "",
    user_id: str = "",
    limit: int = 50,
    offset: int = 0,
) -> dict[str, object]:
    del actor
    bounded_limit = max(1, min(int(limit), 100))
    bounded_offset = max(0, int(offset))
    clauses: list[str] = []
    params: list[object] = []
    if status.strip() == "unresolved":
        # 「未结案」= 待处理 + 处理中，与总览「工单未结案」待办同口径。
        clauses.append("st.status IN ('open', 'processing')")
    elif status.strip():
        clauses.append("st.status = %s")
        params.append(status.strip())
    if category.strip():
        clauses.append("st.category = %s")
        params.append(category.strip())
    if user_id.strip():
        # 客户详情「工单」页签按客户编号精确取，不走用户名模糊匹配。
        clauses.append("st.target_user_id = %s")
        params.append(user_id.strip())
    if username.strip():
        clauses.append("(tu.username ILIKE %s OR tu.display_name ILIKE %s)")
        params.extend([f"%{username.strip()}%", f"%{username.strip()}%"])
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    try:
        with pg_transaction() as conn:
            rows = conn.execute(
                # 未结案在前、同组内按优先级（P0 最先），再按时间倒序。
                _TICKET_SELECT + f"{where} ORDER BY (st.status='resolved'), st.priority, "
                "st.created_at DESC "
                "LIMIT %s OFFSET %s",
                (*params, bounded_limit, bounded_offset),
            ).fetchall()
            total = conn.execute(
                f"SELECT count(*) FROM support_tickets st "
                f"LEFT JOIN users tu ON tu.id = st.target_user_id {where}",  # noqa: S608
                params,
            ).fetchone()
    except (RuntimeError, MissingDatabaseConfigError) as exc:
        raise _http(503, _UNAVAILABLE_CODE, _UNAVAILABLE_MESSAGE) from exc
    return {
        "items": [_ticket_row(row) for row in rows],
        "total": int(total[0]) if total else 0,
        "limit": bounded_limit,
        "offset": bounded_offset,
    }


@router.get("/support-tickets/handlers")
def list_ticket_handlers(actor: AdminReader) -> dict[str, object]:
    """可指派的负责人：启用中的管理员（与 PATCH 的负责人校验同口径）。

    团队成员接口只对超管开放；指派工单是日常操作，这里只暴露名字与编号。
    """
    del actor
    try:
        with pg_transaction() as conn:
            rows = conn.execute(
                "SELECT id, username, COALESCE(display_name, '') FROM users "
                "WHERE role = 'admin' AND is_active = 1 ORDER BY username"
            ).fetchall()
    except (RuntimeError, MissingDatabaseConfigError) as exc:
        raise _http(503, _UNAVAILABLE_CODE, _UNAVAILABLE_MESSAGE) from exc
    return {
        "items": [
            {"user_id": str(r[0]), "username": str(r[1]), "display_name": str(r[2])} for r in rows
        ]
    }


@router.post("/support-tickets")
def create_support_ticket(
    body: TicketCreateRequest,
    request: Request,
    response: Response,
    actor: AdminWriter,
) -> dict[str, object]:
    def business(conn: psycopg.Connection, request_id: str) -> dict[str, object]:
        summary = body.summary.strip()
        if not (1 <= len(summary) <= MAX_SUMMARY):
            raise _http(422, "TICKET_VALIDATION_FAILED", "问题描述必填且不超过 500 字。")
        if body.category not in CATEGORIES:
            raise _http(422, "TICKET_VALIDATION_FAILED", "问题类目无效。")
        if body.priority not in PRIORITIES:
            raise _http(422, "TICKET_VALIDATION_FAILED", "优先级无效。")
        related_ref = _validated_related_ref(body.related_ref)
        target = body.target_user_id.strip() or None
        if target is not None:
            if (
                conn.execute(
                    "SELECT 1 FROM users WHERE id=%s AND role IN ('customer','user','employee')",
                    (target,),
                ).fetchone()
                is None
            ):
                raise _http(422, "TICKET_VALIDATION_FAILED", "关联客户不存在。")
        ticket_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO support_tickets (id, target_user_id, category, summary, "
            "created_by_user_id, priority, related_ref) VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (
                ticket_id,
                target,
                body.category,
                summary,
                actor.user_id,
                body.priority,
                related_ref,
            ),
        )
        conn.execute(
            "INSERT INTO audit_logs (id, actor_user_id, action, entity_type, "
            "entity_id, metadata_json) VALUES (%s,%s,'support_ticket.create',"
            "'support_ticket',%s,%s)",
            (
                str(uuid.uuid4()),
                actor.user_id,
                ticket_id,
                json.dumps(
                    {
                        "category": body.category,
                        "priority": body.priority,
                        "request_id": request_id,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            ),
        )
        return {"id": ticket_id, "status": "open", "request_id": request_id}

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


@router.patch("/support-tickets/{ticket_id}")
def update_support_ticket(
    ticket_id: str,
    body: TicketUpdateRequest,
    request: Request,
    response: Response,
    actor: AdminWriter,
) -> dict[str, object]:
    def business(conn: psycopg.Connection, request_id: str) -> dict[str, object]:
        row = conn.execute(
            "SELECT status FROM support_tickets WHERE id=%s FOR UPDATE",
            (ticket_id,),
        ).fetchone()
        if row is None:
            raise _http(404, "TICKET_NOT_FOUND", "工单不存在。")
        current_status = str(row[0])
        updates: list[str] = []
        params: list[object] = []
        if body.status.strip():
            next_status = body.status.strip()
            if next_status not in STATUSES:
                raise _http(422, "TICKET_VALIDATION_FAILED", "目标状态无效。")
            allowed = {
                "open": {"processing", "resolved"},
                "processing": {"resolved"},
                "resolved": set(),
            }
            if next_status != current_status and next_status not in allowed[current_status]:
                raise _http(
                    422,
                    "TICKET_TRANSITION_INVALID",
                    f"不允许 {current_status} → {next_status}；已解决工单不可复活，请新建。",
                )
            updates.append("status=%s")
            params.append(next_status)
            # 开始处理时自动把当前管理员记为负责人（仍可被显式指派覆盖）。
            if next_status == "processing":
                updates.append("handler_user_id=COALESCE(handler_user_id,%s)")
                params.append(actor.user_id)
        handler = body.handler_user_id.strip()
        if handler:
            if (
                conn.execute(
                    "SELECT 1 FROM users WHERE id=%s AND role='admin' AND is_active=1",
                    (handler,),
                ).fetchone()
                is None
            ):
                raise _http(422, "TICKET_VALIDATION_FAILED", "负责人必须是启用中的管理员。")
            updates.append("handler_user_id=%s")
            params.append(handler)
        if body.priority.strip():
            if body.priority.strip() not in PRIORITIES:
                raise _http(422, "TICKET_VALIDATION_FAILED", "优先级无效。")
            updates.append("priority=%s")
            params.append(body.priority.strip())
        resolution = body.resolution.strip()
        if resolution:
            if len(resolution) > MAX_RESOLUTION:
                raise _http(422, "TICKET_VALIDATION_FAILED", "结论不超过 2000 字。")
            updates.append("resolution=%s")
            params.append(resolution)
        if not updates:
            raise _http(422, "TICKET_VALIDATION_FAILED", "没有可更新的字段。")
        params.extend([ticket_id])
        conn.execute(
            f"UPDATE support_tickets SET {', '.join(updates)}, updated_at=now() WHERE id=%s",  # noqa: S608
            tuple(params),
        )
        conn.execute(
            "INSERT INTO audit_logs (id, actor_user_id, action, entity_type, "
            "entity_id, metadata_json) VALUES (%s,%s,'support_ticket.update',"
            "'support_ticket',%s,%s)",
            (
                str(uuid.uuid4()),
                actor.user_id,
                ticket_id,
                json.dumps(
                    {
                        "fields": updates,
                        "reason": body.reason.strip(),
                        "request_id": request_id,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            ),
        )
        return {"id": ticket_id, "request_id": request_id}

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
