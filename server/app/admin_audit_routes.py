"""T34 / A10 — admin audit log API.

Read-only endpoint for operators and auditors. The 2026-09-02 admin-console
assessment (A10) replaced the single-table ADMIN_ADJUSTMENT view with one
UNION across every audited admin surface, so "查审计" answers the whole
question instead of one ledger slice:

- ``admin_adjustments`` (039)      → ADMIN_ADJUSTMENT
- ``admin_device_events`` (038)    → ADMIN_DEVICE_<EVENT>
- ``activation_code_events`` (027) → ACTIVATION_CODE_<EVENT>
- ``activation_code_deliveries`` (027) → ACTIVATION_CODE_DELIVERED
- ``audit_logs`` (001)             → the action itself (runtime switches,
  control exports, payment syncs, security denials, …)

Filters: ``event_type`` (exact match on the unified type), ``actor_user_id``,
``target_user_id`` and a ``created_from``/``created_to`` ISO timestamp range.
All fail closed on the SQLite lane (the union reads PG-only JSON operators).

The acting administrator is resolved through ``users`` wherever the source
table stores a real actor id; machine-only rows surface with an empty actor.
"""

from __future__ import annotations

import re
from datetime import datetime

from fastapi import APIRouter, HTTPException

from app.admin_auth_routes import AdminReader
from app.db_pg import MissingDatabaseConfigError, pg_transaction

router = APIRouter(prefix="/api/control", tags=["admin-audit"])

DEFAULT_LIST_LIMIT = 20
MAX_LIST_LIMIT = 100
AUDIT_SERVICE_UNAVAILABLE = "AUDIT_SERVICE_UNAVAILABLE"
AUDIT_SERVICE_UNAVAILABLE_MESSAGE = "Audit log requires the PostgreSQL runtime."

# PR #85 review P2: the sources write two text shapes — the Python lanes store
# ISO ``T`` timestamps while the ``CURRENT_TIMESTAMP`` defaults store the
# space-separated form — so every union arm is cast to timestamptz and the
# date-only ``created_to`` bound is widened to the last microsecond of that
# day; lexicographic ordering/paging on mixed formats grouped same-day events
# by shape instead of time, and a bare date excluded everything after midnight.
_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _inclusive_created_to(value: str) -> str:
    """A date-only end bound covers the whole day (UTC), inclusive."""
    if _DATE_ONLY.match(value):
        return f"{value} 23:59:59.999999+00:00"
    return value


def _format_created_at(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value) if value is not None else ""


def _http(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


_UNION_SQL = """
    SELECT aa.id, 'ADMIN_ADJUSTMENT', aa.admin_user_id, u.username,
           aa.target_user_id, aa.source_document_type, aa.source_document_ref,
           aa.reason, aa.request_id, aa.created_at::timestamptz
    FROM admin_adjustments aa
    JOIN users u ON u.id = aa.admin_user_id
    UNION ALL
    SELECT de.id, 'ADMIN_DEVICE_' || de.event, de.admin_user_id, u.username,
           de.target_user_id, 'DEVICE', COALESCE(de.device_id, ''),
           de.reason, de.request_id, de.created_at::timestamptz
    FROM admin_device_events de
    JOIN users u ON u.id = de.admin_user_id
    UNION ALL
    SELECT ae.id, 'ACTIVATION_CODE_' || ae.event,
           COALESCE(ae.actor_user_id, ''), COALESCE(u2.username, ''),
           COALESCE(code.bound_user_id, ''), 'ACTIVATION_CODE', ae.code_id,
           COALESCE(ae.reason, ''), COALESCE(ae.request_id, ''), ae.created_at::timestamptz
    FROM activation_code_events ae
    LEFT JOIN users u2 ON u2.id = ae.actor_user_id
    LEFT JOIN activation_codes code ON code.id = ae.code_id
    UNION ALL
    SELECT d.id, 'ACTIVATION_CODE_DELIVERED', d.delivered_by_user_id, u3.username,
           COALESCE(code.bound_user_id, ''), 'ACTIVATION_CODE_DELIVERY',
           COALESCE(d.external_order_ref, ''), COALESCE(d.note, ''),
           '', d.delivered_at::timestamptz
    FROM activation_code_deliveries d
    JOIN users u3 ON u3.id = d.delivered_by_user_id
    LEFT JOIN activation_codes code ON code.id = d.code_id
    UNION ALL
    SELECT al.id, al.action, COALESCE(al.actor_user_id, ''),
           COALESCE(u4.username, ''),
           CASE WHEN al.entity_type = 'user' THEN al.entity_id ELSE '' END,
           al.entity_type, al.entity_id,
           COALESCE(al.metadata_json::json ->> 'reason', ''),
           COALESCE(al.metadata_json::json ->> 'request_id', ''),
           al.created_at::timestamptz
    FROM audit_logs al
    LEFT JOIN users u4 ON u4.id = al.actor_user_id
"""


@router.get("/audit-log")
def list_audit_log(
    actor: AdminReader,
    event_type: str | None = None,
    actor_user_id: str | None = None,
    target_user_id: str | None = None,
    created_from: str | None = None,
    created_to: str | None = None,
    limit: int = DEFAULT_LIST_LIMIT,
    offset: int = 0,
) -> dict[str, object]:
    """List the unified audit trail with pagination and combined filters.

    Both admin and auditor roles can access this endpoint (read-only).
    """
    bounded_limit = max(0, min(limit, MAX_LIST_LIMIT))
    bounded_offset = max(0, offset)

    clauses: list[str] = []
    params: list[object] = []
    if event_type:
        clauses.append("ev.event_type = %s")
        params.append(event_type)
    if actor_user_id:
        clauses.append("ev.actor_user_id = %s")
        params.append(actor_user_id)
    if target_user_id:
        clauses.append("ev.target_user_id = %s")
        params.append(target_user_id)
    if created_from:
        clauses.append("ev.created_at >= %s")
        params.append(created_from)
    if created_to:
        clauses.append("ev.created_at <= %s")
        params.append(_inclusive_created_to(created_to))
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    try:
        with pg_transaction() as conn:
            rows = conn.execute(
                f"""
                SELECT ev.event_id, ev.event_type, ev.actor_user_id,
                       ev.actor_username, ev.target_user_id,
                       ev.source_document_type, ev.source_document_ref,
                       ev.reason, ev.request_id, ev.created_at
                FROM ({_UNION_SQL}) AS ev(event_id, event_type, actor_user_id,
                                          actor_username, target_user_id,
                                          source_document_type,
                                          source_document_ref, reason,
                                          request_id, created_at)
                {where}
                ORDER BY ev.created_at DESC, ev.event_id
                LIMIT %s OFFSET %s
                """,
                (*params, bounded_limit, bounded_offset),
            ).fetchall()

            total_row = conn.execute(
                f"""
                SELECT COUNT(*) FROM ({_UNION_SQL}) AS ev(
                    event_id, event_type, actor_user_id, actor_username,
                    target_user_id, source_document_type, source_document_ref,
                    reason, request_id, created_at)
                {where}
                """,
                tuple(params),
            ).fetchone()
    except (RuntimeError, MissingDatabaseConfigError) as exc:
        raise _http(
            503,
            AUDIT_SERVICE_UNAVAILABLE,
            AUDIT_SERVICE_UNAVAILABLE_MESSAGE,
        ) from exc

    items = [
        {
            "event_id": str(row[0]),
            "event_type": str(row[1]),
            "actor_user_id": str(row[2]),
            "actor_username": str(row[3]),
            "target_user_id": str(row[4]),
            "source_document_type": str(row[5]),
            "source_document_ref": str(row[6]),
            "reason": str(row[7]),
            "request_id": str(row[8]),
            "created_at": _format_created_at(row[9]),
        }
        for row in rows
    ]

    total = int(total_row[0]) if total_row is not None else 0

    return {
        "items": items,
        "total": total,
        "limit": bounded_limit,
        "offset": bounded_offset,
    }
