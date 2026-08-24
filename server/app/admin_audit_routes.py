"""T34 — admin audit log API.

Read-only endpoint for operators and auditors to view the complete audit trail
of all administrative actions. Currently aggregates admin_adjustments; future
enhancements can add more event types.

Fail-closed runtime: SQLite/missing DSN returns 503 AUDIT_SERVICE_UNAVAILABLE
instead of falling back to legacy control identity (the T12/T18 precedent).
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.admin_auth_routes import AdminReader
from app.db_pg import MissingDatabaseConfigError, pg_transaction

router = APIRouter(prefix="/api/control", tags=["admin-audit"])

DEFAULT_LIST_LIMIT = 20
MAX_LIST_LIMIT = 100
AUDIT_SERVICE_UNAVAILABLE = "AUDIT_SERVICE_UNAVAILABLE"
AUDIT_SERVICE_UNAVAILABLE_MESSAGE = (
    "Audit log requires the PostgreSQL runtime."
)


def _http(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


@router.get("/audit-log")
def list_audit_log(
    actor: AdminReader,
    event_type: str | None = None,
    actor_user_id: str | None = None,
    limit: int = DEFAULT_LIST_LIMIT,
    offset: int = 0,
) -> dict[str, object]:
    """List all audit events with pagination.
    
    Currently aggregates:
    - ADMIN_ADJUSTMENT: admin adjustments from admin_adjustments table
    
    Future event types can be added (device unbind, session revoke, etc.)
    
    Both admin and auditor roles can access this endpoint (read-only).
    """
    bounded_limit = max(0, min(limit, MAX_LIST_LIMIT))
    bounded_offset = max(0, offset)
    
    # For now, only admin_adjustments are audited
    # Future: UNION with other audit tables
    clauses: list[str] = []
    params: list[object] = []
    
    if event_type and event_type == "ADMIN_ADJUSTMENT":
        # Filter by event type (only one type for now)
        pass  # No additional filter needed
    
    if actor_user_id:
        clauses.append("aa.admin_user_id = %s")
        params.append(actor_user_id)
    
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    
    try:
        with pg_transaction() as conn:
            # Query admin adjustments as audit events
            rows = conn.execute(
                f"""
                SELECT aa.id, aa.admin_user_id, aa.target_user_id,
                       aa.source_document_type, aa.source_document_ref,
                       aa.reason, aa.request_id, aa.created_at,
                       au.email as admin_email
                FROM admin_adjustments aa
                JOIN admin_users au ON au.id = aa.admin_user_id
                {where}
                ORDER BY aa.created_at DESC, aa.id
                LIMIT %s OFFSET %s
                """,
                (*params, bounded_limit, bounded_offset),
            ).fetchall()
            
            total_row = conn.execute(
                f"SELECT COUNT(*) FROM admin_adjustments aa {where}",
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
            "event_type": "ADMIN_ADJUSTMENT",
            "actor_user_id": str(row[1]),
            "actor_email": str(row[8]),
            "target_user_id": str(row[2]),
            "source_document_type": str(row[3]),
            "source_document_ref": str(row[4]),
            "reason": str(row[5]),
            "request_id": str(row[6]),
            "created_at": str(row[7]) if row[7] is not None else "",
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
