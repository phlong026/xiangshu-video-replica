"""T34 — admin session management API.

Read-only endpoint for operators and auditors to view customer session history.
Supports pagination (limit/offset) and status filtering.

Fail-closed runtime: SQLite/missing DSN returns 503 SESSION_SERVICE_UNAVAILABLE
instead of falling back to legacy control identity (the T12/T18 precedent).
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.admin_auth_routes import AdminReader
from app.db_pg import MissingDatabaseConfigError, pg_transaction

router = APIRouter(prefix="/api/control", tags=["admin-sessions"])

DEFAULT_LIST_LIMIT = 20
MAX_LIST_LIMIT = 100
SESSION_SERVICE_UNAVAILABLE = "SESSION_SERVICE_UNAVAILABLE"
SESSION_SERVICE_UNAVAILABLE_MESSAGE = (
    "Session management requires the PostgreSQL runtime."
)


def _http(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


@router.get("/customers/{user_id}/sessions")
def list_customer_sessions(
    user_id: str,
    actor: AdminReader,
    status: str | None = None,
    limit: int = DEFAULT_LIST_LIMIT,
    offset: int = 0,
) -> dict[str, object]:
    """List all sessions for a target customer with pagination.
    
    Returns session metadata including device info, status, and timestamps.
    Both admin and auditor roles can access this endpoint (read-only).
    """
    bounded_limit = max(0, min(limit, MAX_LIST_LIMIT))
    bounded_offset = max(0, offset)
    
    clauses: list[str] = ["cs.user_id = %s"]
    params: list[object] = [user_id]
    
    if status:
        clauses.append("cs.status = %s")
        params.append(status)
    
    where = f"WHERE {' AND '.join(clauses)}"
    
    try:
        with pg_transaction() as conn:
            rows = conn.execute(
                f"""
                SELECT cs.id, cs.user_id, cs.device_id, cs.session_token,
                       cs.status, cs.created_at, cs.expires_at,
                       cd.display_name, cd.platform, cd.slot_no
                FROM customer_sessions cs
                JOIN customer_devices cd ON cd.id = cs.device_id
                {where}
                ORDER BY cs.created_at DESC, cs.id
                LIMIT %s OFFSET %s
                """,
                (*params, bounded_limit, bounded_offset),
            ).fetchall()
            
            total_row = conn.execute(
                f"SELECT COUNT(*) FROM customer_sessions cs {where}",
                tuple(params),
            ).fetchone()
    except (RuntimeError, MissingDatabaseConfigError) as exc:
        raise _http(
            503,
            SESSION_SERVICE_UNAVAILABLE,
            SESSION_SERVICE_UNAVAILABLE_MESSAGE,
        ) from exc
    
    items = [
        {
            "session_id": str(row[0]),
            "user_id": str(row[1]),
            "device_id": str(row[2]),
            "session_token": str(row[3])[:8] + "...",  # Masked for security
            "status": str(row[4]),
            "created_at": str(row[5]) if row[5] is not None else "",
            "expires_at": str(row[6]) if row[6] is not None else "",
            "device_name": row[7],
            "platform": str(row[8]),
            "slot_no": int(row[9]),
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
