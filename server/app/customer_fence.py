"""T21 / SES-04 — the customer session fencing wiring (dev doc §12.4, plan B).

The early-snapshot + in-transaction re-verification pair every customer write
route uses:

- ``CustomerSessionSnapshot`` + ``customer_session_snapshot`` — the FastAPI
  dependency that resolves the presented session token to the expected
  session context at request time. It is an *early* 401 gate only; the final
  verdict is always the in-transaction ``verify_session_context`` inside
  ``fenced_pg_transaction`` (the code-checklist §9.2 / SES-04 red line: the
  dependency alone never closes the task). Returns ``None`` when no
  PostgreSQL runtime is configured — the internal/desktop lane has no
  customer sessions and authenticates through ``AuthenticatedUser`` instead.
- ``fenced_pg_transaction`` — the only customer business-write transaction
  entry: opens ``pg_transaction()``, re-verifies the session under the row
  lock inside it (epoch/device/session/lease re-compared, code/device status
  re-checked, lease judged on ``clock_timestamp()``), and yields ``(conn,
  ctx)`` for the business write. Any business exception rolls the whole
  transaction back; a ``SessionFencingError`` answers 401 — a request that
  passed the snapshot can never commit a write after a switch.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

import psycopg
from fastapi import HTTPException, Request

from app.activation_code_service import ActivationKeyError
from app.customer_auth import (
    CustomerSessionContext,
    SessionFencingError,
    verify_session_context,
)
from app.customer_device_service import _token_digests
from app.db_pg import IsolationLevel, get_pg_pool, pg_transaction

AUTHORIZATION_HEADER = "Authorization"
BEARER_SCHEME = "bearer"


def _bearer_token(request: Request) -> str | None:
    header = request.headers.get(AUTHORIZATION_HEADER, "").strip()
    if not header:
        return None
    parts = header.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != BEARER_SCHEME:
        return None
    token = parts[1].strip()
    return token or None


@dataclass(frozen=True)
class CustomerSessionSnapshot:
    """The expected session context captured at request time (early, unlocked).

    Mirrors the §12.4 re-comparison tuple (user / device / session / epoch /
    lease) so ``fenced_pg_transaction`` can re-check every field inside the
    business transaction. ``token`` is the presented session token; the raw
    value is never written anywhere.
    """

    token: str
    expected_user_id: str
    expected_device_id: str
    expected_session_id: str
    expected_session_epoch: int
    expected_lease_until: str


def customer_session_snapshot(request: Request) -> CustomerSessionSnapshot | None:
    """Resolve the presented Bearer session token to the expected context.

    Early and non-authoritative: the read carries no row lock and the 401 here
    is only a fast gate. Returns ``None`` when no PostgreSQL runtime is
    configured (internal/desktop lane — the caller authenticates through
    ``AuthenticatedUser``). Raises 401 for a missing/unknown/replaced token and
    503 fail-closed when the device-domain keys are misconfigured (a server
    outage must never masquerade as a client credential problem).
    """
    token = _bearer_token(request)
    if token is None:
        return None
    try:
        get_pg_pool()
    except (RuntimeError, ValueError):
        # No PostgreSQL runtime — the internal/desktop lane has no customer
        # sessions; the caller uses the internal identity instead.
        return None
    try:
        digests = _token_digests(token)
    except ActivationKeyError as exc:
        raise HTTPException(
            503,
            detail={
                "code": "SESSION_SERVICE_UNAVAILABLE",
                "message": "Session keys are not configured; customer sessions are refused.",
            },
        ) from exc
    with pg_transaction() as conn:
        row = conn.execute(
            "SELECT user_id, device_id, session_id, session_epoch, lease_until "
            "FROM customer_session_state WHERE token_digest = ANY(%s) LIMIT 1",
            (digests,),
        ).fetchone()
    if row is None:
        raise HTTPException(
            401,
            detail={
                "code": "SESSION_REPLACED",
                "message": "This session token no longer owns a live session.",
            },
        )
    return CustomerSessionSnapshot(
        token=token,
        expected_user_id=str(row[0]),
        expected_device_id=str(row[1]),
        expected_session_id=str(row[2]),
        expected_session_epoch=int(row[3]),
        expected_lease_until=str(row[4]),
    )


@contextmanager
def fenced_pg_transaction(
    snapshot: CustomerSessionSnapshot,
    *,
    isolation: IsolationLevel | None = None,
) -> Iterator[tuple[psycopg.Connection, CustomerSessionContext]]:
    """The customer business-write transaction entry (SES-04).

    Opens the PostgreSQL transaction, runs ``verify_session_context`` under the
    session-row lock inside it (the full §12.4 re-comparison plus the code and
    device status re-check and the ``clock_timestamp()`` lease verdict), then
    yields ``(conn, ctx)`` for the business write. A business exception rolls
    the transaction back; a ``SessionFencingError`` raises 401 — so a request
    that passed the early snapshot can never commit a write after a switch.
    """
    try:
        with pg_transaction(isolation=isolation) as conn:
            ctx = verify_session_context(
                conn,
                presentation_session_token=snapshot.token,
                expected_user_id=snapshot.expected_user_id,
                expected_device_id=snapshot.expected_device_id,
                expected_session_id=snapshot.expected_session_id,
                expected_session_epoch=snapshot.expected_session_epoch,
                expected_lease_until=snapshot.expected_lease_until,
            )
            yield conn, ctx
    except SessionFencingError as exc:
        raise HTTPException(
            401,
            detail={"code": exc.code, "message": exc.message},
        ) from None
