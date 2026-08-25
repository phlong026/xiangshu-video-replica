"""T34 — admin customer session API.

Read-only endpoint for operators and auditors to view the live customer
session state (revision 029 ``customer_session_state``: one row per user)
joined to the bound device.

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
SESSION_SERVICE_UNAVAILABLE_MESSAGE = "Session management requires the PostgreSQL runtime."


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
    """List the live session state for a target customer.

    The 029 model keeps exactly one session row per user
    (``customer_session_state``, primary key *is* ``user_id``) so the list is
    at most one row; device columns come from the bound ``customer_devices``
    row and ``status`` filters on the device status (BOUND/UNBOUND/REVOKED).
    Both admin and auditor roles can read (read-only).

    "Live" is judged on the PostgreSQL clock: logout, revocation and natural
    lease expiry keep the row (the lease is pulled into the past, the T16/T19
    pattern) rather than deleting it, so a row's existence alone is not a
    live session.
    """
    bounded_limit = max(0, min(limit, MAX_LIST_LIMIT))
    bounded_offset = max(0, offset)

    clauses: list[str] = [
        "css.user_id = %s",
        # Logout/revocation/expiry pull lease_until into the past instead of
        # deleting the row; without this predicate administrators would see
        # a supposedly live session indefinitely. The column is text holding
        # mixed ISO / PG-text timestamps, so compare on the cast (the 029
        # CHECK ``lease_until::timestamptz > created_at::timestamptz``
        # guarantees every stored value parses) against the PostgreSQL clock.
        "css.lease_until::timestamptz > clock_timestamp()",
    ]
    params: list[object] = [user_id]

    if status:
        clauses.append("cd.status = %s")
        params.append(status)

    where = f"WHERE {' AND '.join(clauses)}"

    try:
        with pg_transaction() as conn:
            rows = conn.execute(
                f"""
                SELECT css.session_id, css.user_id, u.username,
                       css.device_id, css.session_epoch,
                       css.lease_until, css.last_heartbeat_at,
                       css.created_at, css.updated_at,
                       cd.display_name, cd.platform, cd.slot_no, cd.status
                FROM customer_session_state css
                JOIN customer_devices cd ON cd.id = css.device_id
                JOIN users u ON u.id = css.user_id
                {where}
                ORDER BY css.created_at DESC, css.session_id
                LIMIT %s OFFSET %s
                """,
                (*params, bounded_limit, bounded_offset),
            ).fetchall()

            total_row = conn.execute(
                f"""
                SELECT COUNT(*) FROM customer_session_state css
                JOIN customer_devices cd ON cd.id = css.device_id
                {where}
                """,
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
            "username": row[2],
            "device_id": str(row[3]),
            "session_epoch": int(row[4]),
            "lease_until": str(row[5]) if row[5] is not None else "",
            "last_heartbeat_at": str(row[6]) if row[6] is not None else "",
            "created_at": str(row[7]) if row[7] is not None else "",
            "updated_at": str(row[8]) if row[8] is not None else "",
            "device_name": row[9],
            "platform": str(row[10]),
            "slot_no": int(row[11]),
            "device_status": str(row[12]),
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
