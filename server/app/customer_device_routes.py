"""T16 / DEV-01 — device slot listing and unbinding routes.

``GET /api/customer/devices`` and ``DELETE /api/customer/devices/{id}``
(dev doc §6.1), the application half of the frozen topic
``customer_device_routes.py`` (code checklist §3.2). Authentication is the
device credential — the long-lived secret returned once at bind time —
presented as ``Authorization: Bearer <device-token>``. The credential layer
and the online session are deliberately separate (dev doc §3.2): device
management keeps working across session leases, while T19/T21 layer the
session fencing on top.

Stable error codes (dev doc §13.2):

- 401 ``DEVICE_CREDENTIAL_REQUIRED`` — missing or malformed Authorization;
- 401 ``DEVICE_CREDENTIAL_INVALID`` — the token never resolved to a device
  of this deployment;
- 401 ``DEVICE_REVOKED`` — the token belonged to a device that has since
  been unbound or revoked; the client must wipe its stored credentials;
- 400 ``IDEMPOTENCY_KEY_REQUIRED`` — the DELETE carries no Idempotency-Key;
- 404 ``DEVICE_NOT_FOUND`` — the DELETE target does not exist or belongs to
  another user (one answer, no IDOR oracle);
- 409 ``DEVICE_ALREADY_UNBOUND`` — the target row is already released;
- 409 ``IDEMPOTENCY_CONFLICT`` — the key was spent on a different request
  (request-hash mismatch) or is no longer recoverable (see below).

The DELETE carries an ``Idempotency-Key`` sealed with the shared envelope
engine (PR #47 Codex review P2): the unbind is a single-row state flip, but
a client that *lost* the 204 could not previously recover the successful
result — retrying the unbind of the caller's own device answered
401 ``DEVICE_REVOKED`` (the credential it just released) and retrying the
unbind of the other device answered 409 ``DEVICE_ALREADY_UNBOUND``, so an
ordinary network retry surfaced as an ambiguous fresh failure. The envelope
seals the minimal audit payload (target device id + request id) and the
replay answers 204 with ``X-Idempotent-Replay: true``. The recovery probe
runs *before* credential authentication on purpose: after unbinding its own
device the caller's credential is by design no longer resolvable, yet the
same key + same target must still replay the original 204 — the sealed
envelope itself is the proof of the completed submission. The envelope
scope is the fixed ``devices`` namespace (the operation plus the client's
random key already identify the submission; the target rides the request
hash, so the same key against a different target answers 409
``IDEMPOTENCY_CONFLICT``).

The ``X-Request-Id`` header, when supplied, is echoed into the session audit
event; otherwise the route mints one (the activation-route precedent).

PostgreSQL is the customer source of truth: without a PG runtime both
routes fail closed with 503 (the SQLite lane keeps its internal P0 shape).
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta

import psycopg
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict

from app.activation_code_service import ActivationKeyError
from app.customer_device_service import (
    OUTCOME_NOT_BOUND,
    OUTCOME_NOT_FOUND,
    AuthenticatedDevice,
    DeviceCredentialLookup,
    DeviceSlotsSnapshot,
    list_device_slots,
    lookup_device_credential,
    server_now_utc,
    unbind_device,
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
    seal_response,
)
from app.customer_idempotency import (
    request_hash as compute_request_hash,
)
from app.db_pg import get_pg_pool, pg_transaction

logger = logging.getLogger(__name__)

AUTHORIZATION_HEADER = "Authorization"
BEARER_SCHEME = "bearer"
REQUEST_ID_HEADER = "X-Request-Id"
IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"
REPLAY_HEADER = "X-Idempotent-Replay"

# The unbind envelope operation and its fixed scope namespace (see the
# module docstring: the probe runs before authentication, so the scope
# cannot derive from the authenticated caller).
UNBIND_OPERATION = "device_unbind"
UNBIND_SCOPE = "devices"

router = APIRouter(prefix="/api/customer", tags=["customer-devices"])


def _http(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


# ---------------------------------------------------------------------------
# Request / response contracts
# ---------------------------------------------------------------------------


class DeviceView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    slot_no: int
    display_name: str
    platform: str
    status: str
    bound_at: str | None
    last_active_at: str | None
    unbound_at: str | None
    revoked_at: str | None
    is_current: bool


class DeviceSlotView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slot_no: int
    device: DeviceView | None


class DeviceListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slots: list[DeviceSlotView]
    history: list[DeviceView]


# ---------------------------------------------------------------------------
# Bearer authentication (device credential layer)
# ---------------------------------------------------------------------------


def _bearer_token(request: Request) -> str | None:
    """The raw bearer token, or ``None`` when the header is absent/malformed."""
    header = request.headers.get(AUTHORIZATION_HEADER, "").strip()
    if not header:
        return None
    parts = header.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != BEARER_SCHEME:
        return None
    token = parts[1].strip()
    return token or None


def _authenticate(conn: psycopg.Connection, token: str | None) -> AuthenticatedDevice:
    """Authenticate the request's device credential inside an open transaction.

    Raises the stable 401s; returns the authenticated device. The lookup
    runs on the caller's live connection so both routes re-validate the
    credential against the same transaction snapshot that serves the
    request — a credential released mid-flight cannot slip through.
    Key-configuration failures surface as 503 (the activation-route
    precedent): a server-side misconfiguration must never leak as a 500,
    and even less as a 401 that would trick the client into wiping its
    perfectly valid stored credentials (§13.2).
    """
    if token is None:
        raise _http(
            401,
            "DEVICE_CREDENTIAL_REQUIRED",
            "A Bearer device credential is required.",
        )
    try:
        lookup: DeviceCredentialLookup = lookup_device_credential(conn, token)
    except ActivationKeyError:
        logger.warning("device keys unavailable: configuration is incomplete")
        raise _http(
            503,
            "DEVICE_SERVICE_UNAVAILABLE",
            "Device credential keys are not configured; device management is refused.",
        ) from None
    if lookup.device is not None:
        return lookup.device
    if lookup.row_status is not None:
        # The credential belonged to a device that has since been released:
        # the client-side contract is to wipe the stored credentials (§13.2).
        raise _http(
            401,
            "DEVICE_REVOKED",
            "This device credential has been revoked.",
        )
    raise _http(
        401,
        "DEVICE_CREDENTIAL_INVALID",
        "The device credential is invalid.",
    )


def _require_pg() -> None:
    """Fail closed with 503 when the PG runtime is not configured."""
    try:
        get_pg_pool()
    except (RuntimeError, ValueError) as exc:
        raise _http(
            503,
            "DEVICE_SERVICE_UNAVAILABLE",
            "Device management requires the PostgreSQL runtime.",
        ) from exc


def _request_id(request: Request) -> str:
    return request.headers.get(REQUEST_ID_HEADER, "").strip() or str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def _snapshot_response(snapshot: DeviceSlotsSnapshot) -> DeviceListResponse:
    return DeviceListResponse(
        slots=[
            DeviceSlotView(
                slot_no=slot.slot_no,
                device=(
                    DeviceView.model_validate(slot.device) if slot.device is not None else None
                ),
            )
            for slot in snapshot.slots
        ],
        history=[DeviceView.model_validate(entry) for entry in snapshot.history],
    )


@router.get("/devices", response_model=DeviceListResponse)
def list_devices(request: Request) -> DeviceListResponse:
    """The two-slot status view: current bindings plus unbind history."""
    _require_pg()
    token = _bearer_token(request)
    with pg_transaction() as conn:
        device = _authenticate(conn, token)
        snapshot = list_device_slots(conn, user_id=device.user_id, current_device_id=device.id)
    return _snapshot_response(snapshot)


def _replay_unbind_response(
    record: EnvelopeRecord,
    *,
    req_hash: str,
    key_digest: str,
) -> Response:
    """Replay the sealed 204 of a previously committed unbind.

    Same key + different target (request hash) answers 409; a purged,
    never-completed or lapsed envelope answers 409 — the key is spent. A
    ciphertext the configured keys cannot open is a server-side failure and
    answers 503 (the activation-route precedent).
    """
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
        or datetime.fromisoformat(str(record.recovery_expires_at)) <= datetime.now(UTC)
    ):
        raise _http(
            409,
            "IDEMPOTENCY_CONFLICT",
            "This idempotency key is no longer recoverable.",
        )
    try:
        replayed = open_response(
            str(record.ciphertext),
            key=customer_aead_key(int(record.key_version)),
            aad=envelope_aad(UNBIND_OPERATION, UNBIND_SCOPE, key_digest),
        )
    except IdempotencyKeyError:
        raise _http(
            503,
            "DEVICE_SERVICE_UNAVAILABLE",
            "The device service cannot recover this response.",
        ) from None
    headers = {REPLAY_HEADER: "true"}
    replay_request_id = replayed.get("request_id")
    if isinstance(replay_request_id, str) and replay_request_id:
        headers[REQUEST_ID_HEADER] = replay_request_id
    logger.info(
        "customer device unbind idempotent replay: key_version=%s request=%s",
        record.key_version,
        replay_request_id if isinstance(replay_request_id, str) else "-",
    )
    return Response(status_code=204, headers=headers)


@router.delete("/devices/{device_id}", status_code=204)
def unbind_device_route(device_id: str, request: Request) -> Response:
    """Unbind one of the caller's own devices; the row stays as history."""
    _require_pg()
    idempotency_key = request.headers.get(IDEMPOTENCY_KEY_HEADER, "").strip()
    if not idempotency_key:
        raise _http(
            400,
            "IDEMPOTENCY_KEY_REQUIRED",
            "An Idempotency-Key header is required.",
        )
    token = _bearer_token(request)
    request_id = _request_id(request)
    req_hash = compute_request_hash({"device_id": device_id})
    key_digest = idempotency_key_digest(idempotency_key)
    try:
        aead_key_version, aead_key = highest_customer_aead_key()
    except IdempotencyKeyError:
        logger.warning("idempotency AEAD keys unavailable: configuration is incomplete")
        raise _http(
            503,
            "DEVICE_SERVICE_UNAVAILABLE",
            "Idempotency keys are not configured; device management is refused.",
        ) from None

    # Recovery probe *before* authentication (see the module docstring): the
    # caller that unbound its own device can no longer authenticate — the
    # sealed envelope is the only proof of that completed submission.
    with pg_transaction() as conn:
        record = load_envelope(
            conn,
            operation=UNBIND_OPERATION,
            scope=UNBIND_SCOPE,
            key_digest=key_digest,
        )
    if record is not None:
        return _replay_unbind_response(record, req_hash=req_hash, key_digest=key_digest)

    recovery_seconds = recovery_window_seconds()
    with pg_transaction() as conn:
        device = _authenticate(conn, token)
        envelope_id = insert_envelope(
            conn,
            operation=UNBIND_OPERATION,
            scope=UNBIND_SCOPE,
            key_digest=key_digest,
            request_hash=req_hash,
        )
        if envelope_id is None:
            # A concurrent same-key writer committed first: replay the
            # committed envelope instead of unbinding twice.
            loaded = load_envelope(
                conn,
                operation=UNBIND_OPERATION,
                scope=UNBIND_SCOPE,
                key_digest=key_digest,
            )
            if loaded is None:
                raise _http(
                    503,
                    "DEVICE_SERVICE_UNAVAILABLE",
                    "The unbind envelope could not be loaded after a key conflict.",
                )
            # This transaction holds no writes of its own (the insert above
            # lost the race), so answering from the sealed copy while the
            # block unwinds — committing nothing — is safe.
            return _replay_unbind_response(loaded, req_hash=req_hash, key_digest=key_digest)
        # SES-01: PostgreSQL is the only trusted clock — sample it inside
        # the transaction (the activation-route precedent) so unbound_at,
        # the pulled lease and the audit event share one server-side
        # timestamp instead of a possibly skewed application clock.
        now_row = conn.execute("SELECT now()").fetchone()
        now: datetime = now_row[0] if now_row is not None else server_now_utc()
        outcome = unbind_device(
            conn,
            device_id=device_id,
            owner_user_id=device.user_id,
            request_id=request_id,
            server_now=now,
        )
        if outcome == OUTCOME_NOT_FOUND:
            # Missing and foreign devices answer identically — the endpoint
            # must not become an enumeration oracle for other customers'
            # device ids.
            raise _http(
                404,
                "DEVICE_NOT_FOUND",
                "No such device for this account.",
            )
        if outcome == OUTCOME_NOT_BOUND:
            raise _http(
                409,
                "DEVICE_ALREADY_UNBOUND",
                "The device is already unbound.",
            )
        sealed_ciphertext = seal_response(
            {"device_id": device_id, "request_id": request_id},
            key=aead_key,
            aad=envelope_aad(UNBIND_OPERATION, UNBIND_SCOPE, key_digest),
        )
        recovery_expires_at = (
            (now + timedelta(seconds=recovery_seconds)).replace(microsecond=0).isoformat()
        )
        complete_envelope(
            conn,
            envelope_id,
            ciphertext=sealed_ciphertext,
            key_version=aead_key_version,
            recovery_expires_at=recovery_expires_at,
        )
    # Log only after the transaction committed: a rolled-back unbind must
    # not leave an audit log claiming success. Identifiers only, never the
    # token.
    logger.info(
        "customer device unbound: device=%s user=%s request=%s",
        device_id,
        device.user_id,
        request_id,
    )
    return Response(status_code=204)
