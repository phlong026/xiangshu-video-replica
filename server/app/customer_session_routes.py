"""T19 / SES-01 — session login, heartbeat and logout routes.

Routes (dev doc §6.1):

- ``POST /api/customer/sessions/login`` — device-credential login; returns a
  new session token on establishment/recovery, renews on same-device
  presentation of the valid session token, answers 409 ``OTHER_DEVICE_ONLINE``
  when the other device holds a live lease.
- ``POST /api/customer/sessions/heartbeat`` — session-token renewal of the lease.
- ``POST /api/customer/sessions/logout`` — session-token logout; pulls the
  lease into the past and appends the LOGOUT event.

Authentication layers:

- login: the *device credential* (``Authorization: Bearer <device-token>``, the
  T16 layer);
- heartbeat/logout: the *session token* (``Authorization: Bearer <session-token>``).

Idempotency (dev doc §6.3): login and logout carry a mandatory
``Idempotency-Key``. The sealed envelope replays the lost response (same
token, same epoch, no second LOGIN event). Same key against a different
request body answers 409 ``IDEMPOTENCY_CONFLICT``. Heartbeat is naturally
idempotent (renewal) and carries no envelope.

Rate limiting (T15 infrastructure): login draws the ``login:ip`` budget and
answers 429 ``RATE_LIMITED`` with ``Retry-After`` once spent.

Stable error codes (dev doc §13.2 plus the T16 precedent for REQUIRED /
INVALID variants):

- 401 ``DEVICE_CREDENTIAL_REQUIRED`` / ``DEVICE_CREDENTIAL_INVALID`` /
  ``DEVICE_REVOKED`` (login authentication);
- 401 ``SESSION_TOKEN_REQUIRED`` (heartbeat/logout missing the Bearer token);
- 401 ``SESSION_REPLACED`` — the presented token no longer owns the live
  session (another device took over, or the token is unknown);
- 401 ``SESSION_EXPIRED`` — the token matched but the lease has lapsed;
  the session is never resurrected;
- 409 ``OTHER_DEVICE_ONLINE`` — login conflict, masked device hint + the
  remaining lease (never a silent kick, dev doc §3.3);
- 400 ``IDEMPOTENCY_KEY_REQUIRED`` / 409 ``IDEMPOTENCY_CONFLICT``;
- 429 ``RATE_LIMITED``;
- 503 ``SESSION_SERVICE_UNAVAILABLE`` — PG runtime / key misconfiguration.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import UTC, datetime, timedelta

import psycopg
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict

from app.activation_code_service import ActivationKeyError
from app.customer_device_service import (
    DeviceCredentialLookup,
    _token_digests,
    lookup_device_credential,
)
from app.customer_idempotency import (
    EnvelopeRecord,
    IdempotencyKeyError,
    complete_envelope,
    customer_aead_key,
    envelope_aad,
    highest_customer_aead_key,
    idempotency_key_digest,
    insert_envelope,
    load_envelope,
    open_response,
    recovery_window_seconds,
    request_hash,
    seal_response,
)
from app.customer_session_service import (
    HEARTBEAT_EXPIRED,
    HEARTBEAT_REPLACED,
    LOGIN_CONFLICT,
    LOGIN_CREATED,
    LOGIN_RENEWED,
    LOGOUT_EXPIRED,
    LOGOUT_REPLACED,
    heartbeat_session,
    login_session,
    logout_session,
)
from app.db_pg import get_pg_pool, pg_transaction
from app.security_rate_limit import (
    DIMENSION_LOGIN_IP,
    consume_rate_limit,
    login_ip_limit,
    rate_limit_window_seconds,
)

logger = logging.getLogger(__name__)

AUTHORIZATION_HEADER = "Authorization"
BEARER_SCHEME = "bearer"
REQUEST_ID_HEADER = "X-Request-Id"
IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"
REPLAY_HEADER = "X-Idempotent-Replay"
RETRY_AFTER_HEADER = "Retry-After"

LOGIN_OPERATION = "session_login"
LOGOUT_OPERATION = "session_logout"

# The sealed login payload carries the state-machine outcome so a replay can
# reproduce the original status code (200 renewed / 201 established).
OUTCOME_FIELD = "_outcome"

router = APIRouter(prefix="/api/customer/sessions", tags=["customer-sessions"])


def _http(status: int, code: str, message: str, **extra: object) -> HTTPException:
    detail: dict[str, object] = {"code": code, "message": message}
    detail.update(extra)
    return HTTPException(status_code=status, detail=detail)


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_token: str | None = None


class LoginResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str
    device_id: str
    session_id: str
    session_token: str
    session_epoch: int
    session_lease_expires_at: str
    request_id: str


class HeartbeatResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str
    session_epoch: int
    lease_expires_at: str
    request_id: str


def _bearer_token(request: Request) -> str | None:
    header = request.headers.get(AUTHORIZATION_HEADER, "").strip()
    if not header:
        return None
    parts = header.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != BEARER_SCHEME:
        return None
    token = parts[1].strip()
    return token or None


def _request_id(request: Request) -> str:
    return request.headers.get(REQUEST_ID_HEADER, "").strip() or str(uuid.uuid4())


def _require_pg() -> None:
    try:
        get_pg_pool()
    except (RuntimeError, ValueError) as exc:
        raise _http(
            503,
            "SESSION_SERVICE_UNAVAILABLE",
            "Customer sessions require the PostgreSQL runtime.",
        ) from exc


def _transaction_now(conn: psycopg.Connection) -> datetime:
    """SES-01: PostgreSQL is the only trusted clock — sample it inside the
    business transaction (the unbind/activation precedents) so the lease
    judgement and every written timestamp share one server-side clock
    instead of a possibly skewed application clock."""
    now_row = conn.execute("SELECT now()").fetchone()
    now: datetime = now_row[0] if now_row is not None else datetime.now(UTC)
    return now


# ---------------------------------------------------------------------------
# Envelope replay helpers (the T16 unbind-envelope precedent)
# ---------------------------------------------------------------------------


def _find_envelope(
    conn: psycopg.Connection,
    *,
    operation: str,
    scopes: list[str],
    key_digest: str,
) -> tuple[str, EnvelopeRecord, datetime] | None:
    """The committed envelope for this key across the scope candidates.

    Returns the PostgreSQL ``now()`` sampled in this same envelope-read
    transaction: the recovery-window verdict must use the trusted
    server-side clock, never the application process clock (SES-01, the
    activation-route ``_server_now`` precedent; PR #51 review P2)."""
    for scope in scopes:
        record = load_envelope(conn, operation=operation, scope=scope, key_digest=key_digest)
        if record is not None:
            return scope, record, _transaction_now(conn)
    return None


def _envelope_recoverable(
    record: EnvelopeRecord, *, req_hash: str, now: datetime
) -> tuple[str, int]:
    """Validate the envelope for replay; returns (ciphertext, key_version).

    Raises 409 ``IDEMPOTENCY_CONFLICT`` unless the envelope replays this
    exact request; the returned pair is fully narrowed for the AEAD open.
    The recovery window is judged against ``now`` — the PostgreSQL clock
    sampled in the envelope-read transaction (never the process clock;
    PR #51 review P2)."""
    if record.request_hash != req_hash:
        raise _http(
            409,
            "IDEMPOTENCY_CONFLICT",
            "This idempotency key was already used for a different request.",
        )
    if (
        record.purged_at is not None
        or record.ciphertext is None
        or record.key_version is None
        or record.recovery_expires_at is None
        or datetime.fromisoformat(str(record.recovery_expires_at)) <= now
    ):
        raise _http(
            409,
            "IDEMPOTENCY_CONFLICT",
            "This idempotency key is no longer recoverable.",
        )
    return str(record.ciphertext), int(record.key_version)


def _replay_login_response(
    record: EnvelopeRecord,
    *,
    req_hash: str,
    scope: str,
    key_digest: str,
    now: datetime,
) -> tuple[LoginResponse, int]:
    """Replay the sealed login response (and its original status code)."""
    ciphertext, key_version = _envelope_recoverable(record, req_hash=req_hash, now=now)
    try:
        sealed = open_response(
            ciphertext,
            key=customer_aead_key(key_version),
            aad=envelope_aad(LOGIN_OPERATION, scope, key_digest),
        )
    except IdempotencyKeyError:
        raise _http(
            503,
            "SESSION_SERVICE_UNAVAILABLE",
            "The sealed login response could not be decrypted.",
        ) from None
    outcome = str(sealed.pop(OUTCOME_FIELD, LOGIN_CREATED))
    status_code = 200 if outcome == LOGIN_RENEWED else 201
    return LoginResponse.model_validate(sealed), status_code


def _replay_logout_response(
    record: EnvelopeRecord,
    *,
    req_hash: str,
    scope: str,
    key_digest: str,
    now: datetime,
) -> None:
    """Replay the sealed logout (a 204 with no body — integrity-checked)."""
    ciphertext, key_version = _envelope_recoverable(record, req_hash=req_hash, now=now)
    try:
        open_response(
            ciphertext,
            key=customer_aead_key(key_version),
            aad=envelope_aad(LOGOUT_OPERATION, scope, key_digest),
        )
    except IdempotencyKeyError:
        raise _http(
            503,
            "SESSION_SERVICE_UNAVAILABLE",
            "The sealed logout response could not be decrypted.",
        ) from None


def _recovery_expires_at(conn: psycopg.Connection) -> str:
    """The recovery-window deadline on the PostgreSQL clock (the trusted one)."""
    now_row = conn.execute("SELECT now()").fetchone()
    now = now_row[0] if now_row is not None else datetime.now(UTC)
    return (now + timedelta(seconds=recovery_window_seconds())).replace(microsecond=0).isoformat()


# ---------------------------------------------------------------------------
# POST /login
# ---------------------------------------------------------------------------


@router.post("/login", response_model=LoginResponse, status_code=201)
def login(body: LoginRequest, request: Request, response: Response) -> LoginResponse:
    """Drive the §12.3 login state machine (see the module docstring)."""
    _require_pg()

    idempotency_key = request.headers.get(IDEMPOTENCY_KEY_HEADER, "").strip()
    if not idempotency_key:
        raise _http(400, "IDEMPOTENCY_KEY_REQUIRED", "An Idempotency-Key header is required.")

    device_token = _bearer_token(request)
    if device_token is None:
        raise _http(401, "DEVICE_CREDENTIAL_REQUIRED", "A Bearer device credential is required.")

    request_id = _request_id(request)
    key_digest = idempotency_key_digest(idempotency_key)

    # Request hash over the normalized body (session_token is the only field;
    # its sha256 stands in so the raw secret never reaches the hash).
    req_hash = request_hash(
        {
            "session_token_sha256": hashlib.sha256(
                (body.session_token or "").encode("utf-8")
            ).hexdigest(),
        }
    )

    # Envelope keys + scope (fail closed with 503 when *either* key family is
    # misconfigured — a server-side outage must never masquerade as a client
    # credential problem; the activation-route precedent). The scope is the
    # device token digest, probed across all configured key versions (the
    # enroll precedent).
    try:
        aead_key_version, aead_key = highest_customer_aead_key()
        scope_candidates = list(reversed(_token_digests(device_token)))
    except (ActivationKeyError, IdempotencyKeyError):
        logger.warning("session keys unavailable: configuration is incomplete")
        raise _http(
            503,
            "SESSION_SERVICE_UNAVAILABLE",
            "Session keys are not configured; customer sessions are refused.",
        ) from None

    # Replay probe *before* the rate limiter (the activation-route T15 review
    # P2 rule): the sealed 201 is the proof of the completed submission, and
    # a legitimate retry — the client lost the response of an
    # already-successful login — must replay without spending any login:ip
    # budget; charging retries would lock a legal user out of their own
    # cached response after a few network retries. The probe is read-only
    # and scoped to the presented credential's digest.
    with pg_transaction() as conn:
        found = _find_envelope(
            conn, operation=LOGIN_OPERATION, scopes=scope_candidates, key_digest=key_digest
        )
    if found is not None:
        scope, existing, envelope_now = found
        replayed, replay_status = _replay_login_response(
            existing, req_hash=req_hash, scope=scope, key_digest=key_digest, now=envelope_now
        )
        response.headers[REPLAY_HEADER] = "true"
        response.status_code = replay_status
        return replayed

    # IP-dimension rate limit (T15 shared counters, a separate transaction —
    # the spent budget is never refunded).
    client_ip = request.client.host if request.client is not None else "unknown"
    with pg_transaction() as conn:
        decision = consume_rate_limit(
            conn,
            dimension=DIMENSION_LOGIN_IP,
            identifier=client_ip,
            limit=login_ip_limit(),
            window_seconds=rate_limit_window_seconds(),
        )
    if decision.allowed is False:
        blocked = _http(429, "RATE_LIMITED", "Too many login attempts from this address.")
        blocked.headers = {RETRY_AFTER_HEADER: str(decision.retry_after_seconds)}
        raise blocked

    # The business transaction: authenticate, take the envelope, drive the
    # state machine, seal the response — one commit.
    with pg_transaction() as conn:
        try:
            lookup: DeviceCredentialLookup = lookup_device_credential(conn, device_token)
        except ActivationKeyError:
            logger.warning("device keys unavailable: configuration is incomplete")
            raise _http(
                503,
                "SESSION_SERVICE_UNAVAILABLE",
                "Device credential keys are not configured.",
            ) from None
        if lookup.device is None:
            if lookup.row_status is not None:
                raise _http(401, "DEVICE_REVOKED", "This device credential has been revoked.")
            raise _http(401, "DEVICE_CREDENTIAL_INVALID", "The device credential is invalid.")
        device = lookup.device

        envelope_id = insert_envelope(
            conn,
            operation=LOGIN_OPERATION,
            scope=scope_candidates[0],
            key_digest=key_digest,
            request_hash=req_hash,
        )
        if envelope_id is None:
            # A concurrent same-key writer committed first (the insert blocked
            # on the unique index until the other transaction committed):
            # answer from the winner's envelope. This transaction holds no
            # writes of its own, so returning while the block unwinds is safe.
            found = _find_envelope(
                conn, operation=LOGIN_OPERATION, scopes=scope_candidates, key_digest=key_digest
            )
            if found is None:
                raise _http(
                    409,
                    "IDEMPOTENCY_CONFLICT",
                    "The sealed response is no longer recoverable.",
                )
            scope, existing, envelope_now = found
            replayed, replay_status = _replay_login_response(
                existing, req_hash=req_hash, scope=scope, key_digest=key_digest, now=envelope_now
            )
            response.headers[REPLAY_HEADER] = "true"
            response.status_code = replay_status
            return replayed

        # SES-01: PostgreSQL is the only trusted clock — sample it inside the
        # business transaction so the lease judgement and every written
        # timestamp share one server-side clock.
        now = _transaction_now(conn)
        result = login_session(
            conn,
            user_id=device.user_id,
            activation_code_id=device.activation_code_id,
            device_id=device.id,
            presentation_session_token=body.session_token,
            request_id=request_id,
            now=now,
        )
        if result.outcome == LOGIN_CONFLICT:
            # Business failure: rolls back with the transaction — the key
            # stays reusable once the lease actually lapses.
            raise _http(
                409,
                "OTHER_DEVICE_ONLINE",
                "Another device is currently online.",
                online_device_name_masked=result.online_device_name_masked,
                online_slot_no=result.online_slot_no,
                lease_expires_at=result.online_lease_expires_at,
            )

        aad = envelope_aad(LOGIN_OPERATION, scope_candidates[0], key_digest)
        sealed_payload: dict[str, object] = {
            "user_id": device.user_id,
            "device_id": device.id,
            "session_id": result.session_id,
            "session_token": result.session_token or "",
            "session_epoch": result.session_epoch,
            "session_lease_expires_at": result.lease_until,
            "request_id": request_id,
            OUTCOME_FIELD: result.outcome,
        }
        ciphertext = seal_response(sealed_payload, key=aead_key, aad=aad)
        complete_envelope(
            conn,
            envelope_id,
            ciphertext=ciphertext,
            key_version=aead_key_version,
            recovery_expires_at=_recovery_expires_at(conn),
        )

    outcome = result.outcome
    sealed_payload.pop(OUTCOME_FIELD, None)
    response.status_code = 200 if outcome == LOGIN_RENEWED else 201
    return LoginResponse.model_validate(sealed_payload)


# ---------------------------------------------------------------------------
# POST /heartbeat
# ---------------------------------------------------------------------------


@router.post("/heartbeat", response_model=HeartbeatResponse)
def heartbeat(request: Request) -> HeartbeatResponse:
    """Renew the session lease (epoch untouched)."""
    _require_pg()

    session_token = _bearer_token(request)
    if session_token is None:
        raise _http(401, "SESSION_TOKEN_REQUIRED", "A Bearer session token is required.")

    request_id = _request_id(request)
    with pg_transaction() as conn:
        try:
            # SES-01: the lease judgement runs on the in-transaction
            # PostgreSQL clock; the token digests need the device-domain key —
            # misconfiguration fails closed with 503 (never a 500, never a
            # client-credential error).
            result = heartbeat_session(
                conn,
                presentation_session_token=session_token,
                request_id=request_id,
                now=_transaction_now(conn),
            )
        except ActivationKeyError:
            logger.warning("device keys unavailable: configuration is incomplete")
            raise _http(
                503,
                "SESSION_SERVICE_UNAVAILABLE",
                "Device credential keys are not configured.",
            ) from None
        if result.outcome == HEARTBEAT_REPLACED:
            raise _http(401, "SESSION_REPLACED", "This session was replaced by another device.")
        if result.outcome == HEARTBEAT_EXPIRED:
            raise _http(401, "SESSION_EXPIRED", "This session lease has expired.")

    return HeartbeatResponse(
        session_id=result.session_id or "",
        session_epoch=result.session_epoch or 0,
        lease_expires_at=result.lease_until or "",
        request_id=request_id,
    )


# ---------------------------------------------------------------------------
# POST /logout
# ---------------------------------------------------------------------------


@router.post("/logout", status_code=204)
def logout(request: Request, response: Response) -> None:
    """Pull the lease into the past and append the LOGOUT event."""
    _require_pg()

    idempotency_key = request.headers.get(IDEMPOTENCY_KEY_HEADER, "").strip()
    if not idempotency_key:
        raise _http(400, "IDEMPOTENCY_KEY_REQUIRED", "An Idempotency-Key header is required.")

    session_token = _bearer_token(request)
    if session_token is None:
        raise _http(401, "SESSION_TOKEN_REQUIRED", "A Bearer session token is required.")

    request_id = _request_id(request)
    key_digest = idempotency_key_digest(idempotency_key)
    req_hash = request_hash({"operation": LOGOUT_OPERATION})

    # Envelope keys + scope: fail closed with 503 when *either* key family
    # is misconfigured (the activation-route precedent). The scope is the
    # session token digest (probed across key versions).
    try:
        aead_key_version, aead_key = highest_customer_aead_key()
        scope_candidates = list(reversed(_token_digests(session_token)))
    except (ActivationKeyError, IdempotencyKeyError):
        logger.warning("session keys unavailable: configuration is incomplete")
        raise _http(
            503,
            "SESSION_SERVICE_UNAVAILABLE",
            "Session keys are not configured.",
        ) from None

    # Replay probe before authentication (the unbind precedent: the sealed 204
    # is the proof even though the token is by then expired).
    with pg_transaction() as conn:
        found = _find_envelope(
            conn, operation=LOGOUT_OPERATION, scopes=scope_candidates, key_digest=key_digest
        )
    if found is not None:
        scope, existing, envelope_now = found
        _replay_logout_response(
            existing, req_hash=req_hash, scope=scope, key_digest=key_digest, now=envelope_now
        )
        response.headers[REPLAY_HEADER] = "true"
        return None

    with pg_transaction() as conn:
        envelope_id = insert_envelope(
            conn,
            operation=LOGOUT_OPERATION,
            scope=scope_candidates[0],
            key_digest=key_digest,
            request_hash=req_hash,
        )
        if envelope_id is None:
            # A concurrent same-key writer committed first: replay its 204.
            # This transaction holds no writes of its own, so returning while
            # the block unwinds is safe.
            found = _find_envelope(
                conn, operation=LOGOUT_OPERATION, scopes=scope_candidates, key_digest=key_digest
            )
            if found is None:
                raise _http(
                    409,
                    "IDEMPOTENCY_CONFLICT",
                    "The sealed response is no longer recoverable.",
                )
            scope, existing, envelope_now = found
            _replay_logout_response(
                existing, req_hash=req_hash, scope=scope, key_digest=key_digest, now=envelope_now
            )
            response.headers[REPLAY_HEADER] = "true"
            return None

        # SES-01: the PostgreSQL transaction clock drives the release.
        result = logout_session(
            conn,
            presentation_session_token=session_token,
            request_id=request_id,
            now=_transaction_now(conn),
        )
        if result.outcome == LOGOUT_REPLACED:
            raise _http(401, "SESSION_REPLACED", "This session was replaced by another device.")
        if result.outcome == LOGOUT_EXPIRED:
            raise _http(401, "SESSION_EXPIRED", "This session lease has expired.")

        aad = envelope_aad(LOGOUT_OPERATION, scope_candidates[0], key_digest)
        payload: dict[str, object] = {
            "session_id": result.session_id or "",
            "request_id": request_id,
        }
        ciphertext = seal_response(payload, key=aead_key, aad=aad)
        complete_envelope(
            conn,
            envelope_id,
            ciphertext=ciphertext,
            key_version=aead_key_version,
            recovery_expires_at=_recovery_expires_at(conn),
        )

    return None
