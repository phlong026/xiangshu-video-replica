"""T20 / SES-03 — the customer session fencing verifier (dev doc §12.4).

The frozen file (code checklist §9.2): every customer *write* route must call
``verify_session_context`` inside its business transaction (T21 wires them
up) to re-check the session authority before any business write lands. The
verifier is deliberately minimal and self-contained:

- it resolves the presented session token to the single live
  ``customer_session_state`` row under a row lock (§11.2: one live session
  per user, proven by the schema itself);
- it answers the minimal ``CustomerSessionContext`` — user, activation
  code, device, session id, epoch, lease — nothing more ever leaves the
  verifier (§9.2: the smallest authority surface, no credential material);
- a token that no longer owns the row answers ``SESSION_REPLACED`` — an
  unknown token, or one displaced by a switch/takeover/revocation (the
  §12.3 rule that a stale device's write must never observe its epoch
  coming back);
- a matching token under a lapsed lease answers ``SESSION_EXPIRED`` and the
  session is never resurrected (§3.4);
- the authority chain is re-checked inside the same transaction: a
  non-ACTIVE activation code or a released device answers
  ``SESSION_REPLACED`` even if the lease still looks alive — the defence in
  depth behind the suspend/revoke revocation propagation (SES-03);
- the ``expected_*`` parameters mirror §12.4's in-transaction re-comparison
  (``user_id + device_id + session_id + session_epoch + lease``): any
  mismatch answers ``SESSION_REPLACED`` so a request that passed a FastAPI
  dependency earlier can never commit after a switch (the expected lease
  must still *be* the row's lease — a lease pulled back by logout/revocation
  is fenced even when the epoch is unchanged).

Timestamps: PostgreSQL is the only trusted clock (SES-01) — the lease
judgement samples ``SELECT clock_timestamp()`` after the row lock, so a
write transaction that began while the lease was valid but waited on the
``FOR UPDATE`` until after it expired is judged at the *actual* wall-clock
time (``now()`` is fixed at transaction start and would authorise the
stale write; PR #52 P2). Only keyed digests of the presented token
ever reach the database (§7); a misconfigured device-domain key raises
``ActivationKeyError`` for the calling route to fail closed with 503 (never
a 500, never a client-credential error).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import psycopg

from app.customer_device_service import _token_digests

# Stable fencing error codes (dev doc §13.2).
SESSION_REPLACED = "SESSION_REPLACED"
SESSION_EXPIRED = "SESSION_EXPIRED"


class SessionFencingError(Exception):
    """A fenced-out session write; ``code`` carries the §13.2 stable code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class CustomerSessionContext:
    """The minimal session authority context (code checklist §9.2).

    Exactly these six fields — no token, no digest, nothing a caller could
    reuse as a credential: the smallest surface a customer write route may
    act on.
    """

    user_id: str
    activation_code_id: str
    device_id: str
    session_id: str
    session_epoch: int
    lease_until: str


# SELECT column order: (user_id, activation_code_id, device_id, session_id,
# session_epoch, lease_until) — mirrors the session-service row probe.
_ROW_SQL = (
    "SELECT user_id, activation_code_id, device_id, session_id, "
    "session_epoch, lease_until FROM customer_session_state"
)


def _as_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _replaced(message: str) -> SessionFencingError:
    return SessionFencingError(SESSION_REPLACED, message)


def verify_session_context(
    conn: psycopg.Connection,
    *,
    presentation_session_token: str,
    expected_user_id: str | None = None,
    expected_device_id: str | None = None,
    expected_session_id: str | None = None,
    expected_session_epoch: int | None = None,
    expected_lease_until: str | None = None,
) -> CustomerSessionContext:
    """Fence one customer write behind the live session row (dev doc §12.4).

    Runs inside the caller's business transaction: the row lock taken here
    serializes against the switch/takeover/revocation writers, so once this
    answers a context the epoch cannot move until the caller commits — a
    request whose expectations were formed before a switch observes the
    bump and is fenced out instead of committing over the new session.
    """
    digests = _token_digests(presentation_session_token)
    row = conn.execute(
        f"{_ROW_SQL} WHERE token_digest = ANY(%s) LIMIT 1 FOR UPDATE",  # noqa: S608
        (digests,),
    ).fetchone()
    if row is None:
        # The token never resolved to the live row: unknown, or displaced by
        # a switch/takeover/revocation. One answer, no oracle (§13.2).
        raise _replaced("This session was replaced by another device.")

    user_id = str(row[0])
    activation_code_id = str(row[1])
    device_id = str(row[2])
    session_id = str(row[3])
    session_epoch = int(row[4])
    lease_until_raw = str(row[5])

    # §12.4 in-transaction re-comparison: the request's expectations (formed
    # when it passed the earlier dependency) must still describe the row —
    # otherwise a switch happened in between and this write must not commit.
    if expected_user_id is not None and expected_user_id != user_id:
        raise _replaced("This session no longer belongs to the expected user.")
    if expected_device_id is not None and expected_device_id != device_id:
        raise _replaced("This session no longer rides the expected device.")
    if expected_session_id is not None and expected_session_id != session_id:
        raise _replaced("This session was replaced by a newer session.")
    if expected_session_epoch is not None and expected_session_epoch != session_epoch:
        raise _replaced("This session epoch was superseded by a newer session.")
    if expected_lease_until is not None and lease_until_raw != expected_lease_until:
        # §12.4 re-comparison of the full snapshot: the lease is part of the
        # authority tuple. A heartbeat renews it forward (same logical
        # session, no epoch bump) and the client never heartbeats mid-request,
        # so an exact mismatch means the lease was pulled back (logout /
        # revocation) — fence it. (PR #52 P2.)
        raise _replaced("This session lease snapshot was superseded by a newer session.")

    # SES-01: PostgreSQL is the only trusted clock — the lease judgement
    # runs on the *actual* verification time, never the transaction-start
    # timestamp nor a skewed app clock. clock_timestamp() advances with the
    # wall clock, so a transaction that waited on the row lock until after
    # the lease expired still fences the write (PR #52 P2).
    now_row = conn.execute("SELECT clock_timestamp()").fetchone()
    now = now_row[0] if now_row is not None else datetime.now(UTC)
    if now > _as_utc(lease_until_raw):
        raise SessionFencingError(SESSION_EXPIRED, "This session lease has expired.")

    # Defence in depth (SES-03): the revocation paths pull the lease first,
    # but the verifier re-checks the authority chain itself — a non-ACTIVE
    # code or a released device fences the write even if the lease still
    # looks alive. Plain snapshot reads: no second lock is needed, the row
    # lock above already serializes the outcome against the writers.
    code_row = conn.execute(
        "SELECT status FROM activation_codes WHERE id = %s", (activation_code_id,)
    ).fetchone()
    if code_row is None or str(code_row[0]) != "ACTIVE":
        raise _replaced("The activation code behind this session is no longer active.")

    device_row = conn.execute(
        "SELECT status FROM customer_devices WHERE id = %s", (device_id,)
    ).fetchone()
    if device_row is None or str(device_row[0]) != "BOUND":
        raise _replaced("The device behind this session has been released.")

    return CustomerSessionContext(
        user_id=user_id,
        activation_code_id=activation_code_id,
        device_id=device_id,
        session_id=session_id,
        session_epoch=session_epoch,
        lease_until=lease_until_raw,
    )
