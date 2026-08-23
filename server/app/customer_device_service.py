"""T16 / DEV-01 — two current device slots, credentials and unbind history.

The device-slot half of the customer runtime (code checklist §3.2, frozen
name ``customer_device_service.py``). Revision 028 already proved the slot
invariants in PostgreSQL — the partial unique indexes on
``(activation_code_id, slot_no)`` and ``fingerprint_hmac`` for ``BOUND``
rows, the global ``token_digest`` uniqueness and the three-state shape
coupling. This module is the application layer on top:

- ``lookup_device_credential`` resolves a presented device token to its
  ``BOUND`` device row. The token is probed against *every* configured
  device-domain key version, so a credential issued under a retained older
  version keeps authenticating through a rotation window (the PR #44
  review P1 precedent on the fingerprint dimension). The return value
  distinguishes "no such credential" from "credential of a released
  device" so the routes can answer 401 ``DEVICE_CREDENTIAL_INVALID`` versus
  401 ``DEVICE_REVOKED`` — the client-side signal to wipe stored
  credentials (dev doc §13.2). Only keyed digests ever reach the database.
- ``list_device_slots`` builds the two-slot status view: slot 1 and slot 2
  each hold at most one currently ``BOUND`` device, and every released row
  stays in the history (dev doc §3.2: unbinding releases the current
  occupancy but never deletes the audit trail).
- ``unbind_device`` flips one ``BOUND`` row of the *caller's own* user to
  ``UNBOUND`` and, when the user's single live session rides that device,
  revokes it atomically in the same transaction: epoch bump, lease pulled
  into the past and a ``LOGOUT`` event with reason ``device_unbound``
  (dev doc §9.2: device revocation must atomically invalidate the current
  session). A missing or foreign device reports ``not_found`` — one
  answer, no IDOR oracle.
- ``next_free_slot`` reports the lowest free slot of an activation code,
  or ``None`` when both are ``BOUND`` — the third-device block that the
  T17 second-device enroll flow will consult before binding.

No-Go red lines: no plaintext device token in a column, event or log
record — only keyed digests; slot numbers are 1 or 2 only; unbind history
is never deleted or overwritten.

PostgreSQL is the customer source of truth, so every entry point expects a
live PG connection (the routes fail closed with 503 on the SQLite lane).
"""

from __future__ import annotations

import hashlib
import hmac
import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import psycopg

from app.activation_code_service import ActivationKeyError

DEVICE_FINGERPRINT_HMAC_KEY_ENV = "VIDEO_REPLICA_DEVICE_FINGERPRINT_HMAC_KEY"
MIN_HMAC_KEY_BYTES = 32
MAX_KEY_VERSION = 64
MAX_DEVICE_SLOTS = 2

BOUND = "BOUND"
UNBOUND = "UNBOUND"
REVOKED = "REVOKED"

# The session-event reason recorded when an unbind pulls the lease out from
# under the session riding the released device.
UNBIND_REASON = "device_unbound"

# Service-level outcomes of ``unbind_device`` (the routes translate them to
# HTTP): the row is gone / belongs to another user, it is not currently
# BOUND, or the unbind succeeded.
OUTCOME_UNBOUND = "unbound"
OUTCOME_NOT_FOUND = "not_found"
OUTCOME_NOT_BOUND = "not_bound"


@dataclass(frozen=True)
class AuthenticatedDevice:
    """The minimal device identity a management route needs."""

    id: str
    user_id: str
    activation_code_id: str
    slot_no: int
    display_name: str
    platform: str


@dataclass(frozen=True)
class DeviceCredentialLookup:
    """The resolution of one presented device token.

    ``device`` is set only for a currently ``BOUND`` device. ``row_status``
    carries the stored status when the token matched a released row, which
    is how the routes distinguish 401 ``DEVICE_REVOKED`` (the client should
    wipe its stored credentials) from 401 ``DEVICE_CREDENTIAL_INVALID``.
    """

    device: AuthenticatedDevice | None
    row_status: str | None


# ---------------------------------------------------------------------------
# Versioned device-domain keys (T13 activation-route precedent)
# ---------------------------------------------------------------------------


def _env_key_candidates(base_env: str, key_version: int) -> list[str]:
    candidates = [f"{base_env}_V{key_version}"]
    if key_version == 1:
        candidates.append(base_env)
    return candidates


def _configured_key_versions(base_env: str) -> list[int]:
    return [
        version
        for version in range(1, MAX_KEY_VERSION + 1)
        if any(os.environ.get(name, "").strip() for name in _env_key_candidates(base_env, version))
    ]


def _device_domain_hmac_key(key_version: int) -> bytes:
    """The device-domain HMAC key (raw bytes): fingerprints and credentials."""
    for name in _env_key_candidates(DEVICE_FINGERPRINT_HMAC_KEY_ENV, key_version):
        value = os.environ.get(name, "").strip()
        if not value:
            continue
        raw = value.encode("utf-8")
        if len(raw) < MIN_HMAC_KEY_BYTES:
            raise ActivationKeyError(f"{name} must be at least {MIN_HMAC_KEY_BYTES} bytes")
        return raw
    raise ActivationKeyError(
        f"{DEVICE_FINGERPRINT_HMAC_KEY_ENV} for key version {key_version} is not configured"
    )


def highest_device_domain_key() -> tuple[int, bytes]:
    """The highest configured device-domain key version and its raw bytes."""
    configured = _configured_key_versions(DEVICE_FINGERPRINT_HMAC_KEY_ENV)
    if not configured:
        raise ActivationKeyError(f"no {DEVICE_FINGERPRINT_HMAC_KEY_ENV} key version is configured")
    version = max(configured)
    return version, _device_domain_hmac_key(version)


def keyed_digest(key: bytes, value: str) -> str:
    """The device-domain keyed digest (fingerprints and credentials, §7)."""
    return hmac.new(key, value.encode("utf-8"), hashlib.sha256).hexdigest()


def _token_digests(token: str) -> list[str]:
    """The token's digest under every configured key version.

    Authentication must probe all of them: during a rotation window an
    older key version stays configured, and credentials issued under it
    remain valid (the same rule the activation route applies to
    fingerprints, PR #44 review P1). A deployment with *no* configured
    key version is a server-side configuration failure, so the empty
    probe list raises instead of quietly resolving every token to
    "invalid" — that would dress an operator problem up as a client
    credential problem (the §13.2 client contract wipes its stored
    credentials on 401s, which must never happen because of a misconfig).
    """
    versions = _configured_key_versions(DEVICE_FINGERPRINT_HMAC_KEY_ENV)
    if not versions:
        raise ActivationKeyError(f"no {DEVICE_FINGERPRINT_HMAC_KEY_ENV} key version is configured")
    return [keyed_digest(_device_domain_hmac_key(version), token) for version in versions]


# ---------------------------------------------------------------------------
# Credential authentication
# ---------------------------------------------------------------------------


def lookup_device_credential(conn: psycopg.Connection, token: str) -> DeviceCredentialLookup:
    """Resolve one presented device token against ``customer_devices``.

    The token itself never appears in the query — only its keyed digests.
    A ``BOUND`` match authenticates; a non-BOUND match means the credential
    belonged to a released (unbound or revoked) device; no match at all
    means the token was never issued by this deployment. Key-configuration
    failures (missing or too-short keys) raise ``ActivationKeyError`` —
    the routes translate that into a 503, never a 500 or a misleading 401.
    """
    digests = _token_digests(token)
    row = conn.execute(
        "SELECT id, user_id, activation_code_id, slot_no, display_name, platform, status "
        "FROM customer_devices WHERE token_digest = ANY(%s)",
        (digests,),
    ).fetchone()
    if row is None:
        return DeviceCredentialLookup(device=None, row_status=None)
    status = str(row[6])
    if status != BOUND:
        return DeviceCredentialLookup(device=None, row_status=status)
    device = AuthenticatedDevice(
        id=str(row[0]),
        user_id=str(row[1]),
        activation_code_id=str(row[2]),
        slot_no=int(row[3]),
        display_name=str(row[4]),
        platform=str(row[5]),
    )
    return DeviceCredentialLookup(device=device, row_status=status)


# ---------------------------------------------------------------------------
# The two-slot status view
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DeviceSlotView:
    """One slot of the two-slot status: its number and current device."""

    slot_no: int
    device: dict[str, object] | None


@dataclass(frozen=True)
class DeviceSlotsSnapshot:
    """The full device view for one customer user."""

    slots: list[DeviceSlotView]
    history: list[dict[str, object]]


def _device_view(row: tuple[object, ...], *, slot_no: int, is_current: bool) -> dict[str, object]:
    return {
        "id": str(row[0]),
        "slot_no": slot_no,
        "display_name": str(row[1]),
        "platform": str(row[2]),
        "status": str(row[3]),
        "bound_at": None if row[4] is None else str(row[4]),
        "last_active_at": None if row[5] is None else str(row[5]),
        "unbound_at": None if row[6] is None else str(row[6]),
        "revoked_at": None if row[7] is None else str(row[7]),
        "is_current": is_current,
    }


def list_device_slots(
    conn: psycopg.Connection, *, user_id: str, current_device_id: str
) -> DeviceSlotsSnapshot:
    """The two-slot status plus the unbind history for one user.

    Every row of the user is read (slot number, current status and the
    shape-coupled timestamps); ``BOUND`` rows occupy their slot in the
    status view, released rows fall through to the history list. Rows are
    never deleted, so the history outlives slot reuse (dev doc §3.2).
    """
    rows = conn.execute(
        "SELECT id, display_name, platform, status, bound_at, last_active_at, "
        "unbound_at, revoked_at, slot_no "
        "FROM customer_devices WHERE user_id = %s ORDER BY created_at, id",
        (user_id,),
    ).fetchall()
    occupied: dict[int, tuple[object, ...]] = {}
    history: list[dict[str, object]] = []
    for row in rows:
        slot_no = int(row[8])
        if str(row[3]) == BOUND:
            # The partial unique index guarantees at most one BOUND row per
            # slot, so a plain assignment cannot lose an occupant.
            occupied[slot_no] = row
        else:
            history.append(_device_view(row, slot_no=slot_no, is_current=False))
    slots = [
        DeviceSlotView(
            slot_no=slot_no,
            device=(
                _device_view(
                    occupied[slot_no],
                    slot_no=slot_no,
                    is_current=str(occupied[slot_no][0]) == current_device_id,
                )
                if slot_no in occupied
                else None
            ),
        )
        for slot_no in range(1, MAX_DEVICE_SLOTS + 1)
    ]
    return DeviceSlotsSnapshot(slots=slots, history=history)


# ---------------------------------------------------------------------------
# The third-device block
# ---------------------------------------------------------------------------


def next_free_slot(conn: psycopg.Connection, activation_code_id: str) -> int | None:
    """The lowest free slot of the activation code, or ``None`` when full.

    Slot 1 and slot 2 are the only legal numbers (the 028 CHECK); with both
    currently ``BOUND`` there is no free slot — the third-device block the
    T17 enroll flow must answer with 409 ``DEVICE_SLOTS_FULL``. An unbound
    row does not occupy its slot (partial unique index, DEV-01 No-Go), so
    the released slot is immediately reusable.
    """
    rows = conn.execute(
        "SELECT slot_no FROM customer_devices WHERE activation_code_id = %s AND status = 'BOUND'",
        (activation_code_id,),
    ).fetchall()
    taken = {int(row[0]) for row in rows}
    for slot_no in range(1, MAX_DEVICE_SLOTS + 1):
        if slot_no not in taken:
            return slot_no
    return None


# ---------------------------------------------------------------------------
# Unbinding (history preserved, session revoked atomically)
# ---------------------------------------------------------------------------


def unbind_device(
    conn: psycopg.Connection,
    *,
    device_id: str,
    owner_user_id: str,
    request_id: str,
    server_now: datetime,
) -> str:
    """Unbind one of the user's own devices; keep the row as history.

    Outcomes (translated by the routes):

    - ``not_found`` — no such device, or it belongs to another user. Both
      cases answer identically so the endpoint is not an IDOR oracle.
    - ``not_bound`` — the row exists but is already UNBOUND/REVOKED; the
      caller answers 409 ``DEVICE_ALREADY_UNBOUND``.
    - ``unbound`` — the row flipped to ``UNBOUND`` with ``unbound_at``; the
      slot is free for reuse and, when the user's single live session rode
      this device, that session was revoked in this same transaction
      (epoch + 1, lease in the past, ``LOGOUT`` event with reason
      ``device_unbound``), so the released credential can never ride it.
    """
    row = conn.execute(
        "SELECT status, user_id, activation_code_id FROM customer_devices WHERE id = %s FOR UPDATE",
        (device_id,),
    ).fetchone()
    if row is None or str(row[1]) != owner_user_id:
        return OUTCOME_NOT_FOUND
    if str(row[0]) != BOUND:
        return OUTCOME_NOT_BOUND
    activation_code_id = str(row[2])

    # PR #47 Codex review P2: keep the PostgreSQL microsecond precision —
    # trimming the timestamp to whole seconds is what once forced the
    # GREATEST fallback below to a full second (a lease living up to 1 s in
    # the future while the device was already released). The full-precision
    # transaction clock always postdates the session's created_at (the
    # activation transaction committed before this unbind began), so the
    # trimmed-seconds collision class disappears.
    now_iso = server_now.isoformat()
    conn.execute(
        "UPDATE customer_devices SET status = 'UNBOUND', unbound_at = %s WHERE id = %s",
        (now_iso, device_id),
    )

    # Dev doc §9.2: device revocation atomically invalidates the current
    # session. One live session per user — when it rides the released
    # device, bump the epoch (the monotonic trigger allows only upward
    # movement) and pull the lease into the past so fencing rejects any
    # in-flight request from the released credential's session token. The
    # GREATEST is a defensive same-transaction backstop for the
    # ck_customer_session_state_lease_after_created check (a hypothetical
    # future code path creating and unbinding within one transaction would
    # sample an identical now()); one microsecond — not one second — keeps
    # the "immediately expired" semantics (PR #47 Codex review P2: a lease
    # surviving up to a second past the unbind contradicts the acceptance
    # requirement that revocation invalidates the session immediately).
    session_row = conn.execute(
        "UPDATE customer_session_state "
        "SET session_epoch = session_epoch + 1, "
        "lease_until = GREATEST(%s::timestamptz, "
        "created_at::timestamptz + interval '1 microsecond'), "
        "updated_at = %s "
        "WHERE device_id = %s AND user_id = %s "
        "RETURNING session_id, session_epoch",
        (now_iso, now_iso, device_id, owner_user_id),
    ).fetchone()
    if session_row is not None:
        conn.execute(
            "INSERT INTO customer_session_events "
            "(id, event, user_id, activation_code_id, device_id, session_id, "
            " session_epoch, actor_user_id, reason, request_id) "
            "VALUES (%s, 'LOGOUT', %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                str(uuid.uuid4()),
                owner_user_id,
                activation_code_id,
                device_id,
                str(session_row[0]),
                int(session_row[1]),
                owner_user_id,
                UNBIND_REASON,
                request_id,
            ),
        )
    return OUTCOME_UNBOUND


def server_now_utc() -> datetime:
    """The server clock used for unbind timestamps (UTC, test-overridable)."""
    return datetime.now(UTC)
