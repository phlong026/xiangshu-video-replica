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
- 404 ``DEVICE_NOT_FOUND`` — the DELETE target does not exist or belongs to
  another user (one answer, no IDOR oracle);
- 409 ``DEVICE_ALREADY_UNBOUND`` — the target row is already released.

The DELETE needs no Idempotency-Key: unbinding is a single-row state flip
whose retry lands on ``DEVICE_ALREADY_UNBOUND`` / ``DEVICE_NOT_FOUND``
(dev doc §6.3 keeps it off the idempotency-key requirement list). The
``X-Request-Id`` header, when supplied, is echoed into the session audit
event; otherwise the route mints one (the activation-route precedent).

PostgreSQL is the customer source of truth: without a PG runtime both
routes fail closed with 503 (the SQLite lane keeps its internal P0 shape).
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime

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
from app.db_pg import get_pg_pool, pg_transaction

logger = logging.getLogger(__name__)

AUTHORIZATION_HEADER = "Authorization"
BEARER_SCHEME = "bearer"
REQUEST_ID_HEADER = "X-Request-Id"

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


@router.delete("/devices/{device_id}", status_code=204)
def unbind_device_route(device_id: str, request: Request) -> Response:
    """Unbind one of the caller's own devices; the row stays as history."""
    _require_pg()
    token = _bearer_token(request)
    request_id = _request_id(request)
    with pg_transaction() as conn:
        device = _authenticate(conn, token)
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
