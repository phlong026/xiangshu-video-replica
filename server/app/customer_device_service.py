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
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import psycopg

from app.activation_code_service import ActivationKeyError

DEVICE_FINGERPRINT_HMAC_KEY_ENV = "VIDEO_REPLICA_DEVICE_FINGERPRINT_HMAC_KEY"
MIN_HMAC_KEY_BYTES = 32
MAX_KEY_VERSION = 64
MAX_DEVICE_SLOTS = 2

BOUND = "BOUND"
UNBOUND = "UNBOUND"
REVOKED = "REVOKED"

# T17 / DEV-02 — the one-shot pairing state machine (dev doc §12.2).
PAIRING_PENDING = "PENDING"
PAIRING_APPROVED = "APPROVED"
PAIRING_CONSUMED = "CONSUMED"
PAIRING_EXPIRED = "EXPIRED"

# How long a pairing request stays usable: the approval is time-boxed and
# an unconsumed request lapses (lazily flipped to EXPIRED on the next touch).
PAIRING_TTL_SECONDS = 900

# The session-event reason recorded when an unbind pulls the lease out from
# under the session riding the released device.
UNBIND_REASON = "device_unbound"

# Service-level outcomes of ``unbind_device`` (the routes translate them to
# HTTP): the row is gone / belongs to another user, it is not currently
# BOUND, or the unbind succeeded.
OUTCOME_UNBOUND = "unbound"
OUTCOME_NOT_FOUND = "not_found"
OUTCOME_NOT_BOUND = "not_bound"
OUTCOME_REVOKED = "revoked"

# T18 — administrator device operations (dev doc §12.2 step 3, §9.2, §15).
# The admin-verified approval is the fallback lane for a first device that
# is no longer available; the outcome distinct from the T17 device-lane
# constants tells the route to answer 403 (the administrator must not
# shortcut a live first device).
ADMIN_APPROVE_FIRST_DEVICE_AVAILABLE = "first_device_available"
# The append-only audit rows (revision 038) carrying the real admin actor.
ADMIN_EVENT_PAIRING_APPROVED = "PAIRING_ADMIN_APPROVED"
ADMIN_EVENT_DEVICE_UNBOUND = "DEVICE_ADMIN_UNBOUND"
ADMIN_EVENT_CREDENTIAL_REVOKED = "DEVICE_CREDENTIAL_REVOKED"


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


def fingerprint_digests_for(value: str) -> tuple[list[str], int]:
    """The value's digest under every configured key version, plus the top one.

    The PR #44 review P1 rotation-window rule: during a rotation several key
    versions stay configured, and identity established under a retained
    older version must remain recognizable — so callers probing a fingerprint
    (binding checks, pairing lookups, envelope scopes) pass the whole list,
    while new rows always carry the highest version's digest (last element).
    A deployment with no configured version is a server-side configuration
    failure and raises ``ActivationKeyError``.
    """
    versions = _configured_key_versions(DEVICE_FINGERPRINT_HMAC_KEY_ENV)
    if not versions:
        raise ActivationKeyError(f"no {DEVICE_FINGERPRINT_HMAC_KEY_ENV} key version is configured")
    digests = [keyed_digest(_device_domain_hmac_key(version), value) for version in versions]
    return digests, versions[-1]


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
    # in-flight request from the released credential's session token.
    _revoke_session_riding_device(
        conn,
        device_id=device_id,
        owner_user_id=owner_user_id,
        activation_code_id=activation_code_id,
        actor_user_id=owner_user_id,
        reason=UNBIND_REASON,
        request_id=request_id,
        now_iso=now_iso,
    )
    return OUTCOME_UNBOUND


def _revoke_session_riding_device(
    conn: psycopg.Connection,
    *,
    device_id: str,
    owner_user_id: str,
    activation_code_id: str,
    actor_user_id: str,
    reason: str,
    request_id: str,
    now_iso: str,
) -> None:
    """Terminate the live session riding a released device, atomically.

    The T16 unbind and the T18 administrator unbind/revocation share this
    core (dev doc §9.2): epoch bump, lease pulled into the past and a
    ``LOGOUT`` event naming the acting user and the reason. The GREATEST is
    a defensive same-transaction backstop for the
    ``ck_customer_session_state_lease_after_created`` check (a hypothetical
    future code path creating and unbinding within one transaction would
    sample an identical now()); one microsecond — not one second — keeps
    the "immediately expired" semantics (PR #47 Codex review P2: a lease
    surviving up to a second past the release contradicts the acceptance
    requirement that revocation invalidates the session immediately).
    """
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
                actor_user_id,
                reason,
                request_id,
            ),
        )


def server_now_utc() -> datetime:
    """The server clock used for unbind timestamps (UTC, test-overridable)."""
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# Second-device pairing (T17 / DEV-02, dev doc §12.2)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ActivePairing:
    """An active (PENDING or APPROVED) pairing request, row-locked.

    ``lookup_active_pairing`` flips a lapsed row to ``EXPIRED`` in the same
    transaction and reports nothing — the caller then creates a fresh
    request, so expiry is lazy but terminal.

    ``candidate_fingerprint_key_version`` is the key version the digest was
    keyed with *when the row was created*: consumption must copy it onto the
    ``customer_devices`` row, or a rotation between the 202 and the 201 would
    store the old digest mislabelled as the new version (PR #49 Codex
    review P2 — the device-domain lookups probe every configured version,
    but the stored pair must still be truthful).
    """

    id: str
    activation_code_id: str
    candidate_fingerprint_hmac: str
    candidate_fingerprint_key_version: int
    display_name: str
    platform: str
    status: str
    expires_at: str


@dataclass(frozen=True)
class ConsumedPairing:
    """The credentials issued when an approved pairing is consumed."""

    device_id: str
    slot_no: int
    device_token: str


# Service-level outcomes the routes translate to HTTP.
APPROVE_APPROVED = "approved"
APPROVE_ALREADY_APPROVED = "already_approved"
APPROVE_ALREADY_CONSUMED = "already_consumed"
APPROVE_EXPIRED = "expired"
APPROVE_FORBIDDEN = "forbidden"
APPROVE_NOT_FOUND = "not_found"
APPROVE_REVOKED = "revoked"
APPROVE_SELF = "self_approval"


def _pairing_expired(expires_at: str, server_now: datetime) -> bool:
    expires = datetime.fromisoformat(expires_at)
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    return expires <= server_now


def lookup_active_pairing(
    conn: psycopg.Connection,
    *,
    activation_code_id: str,
    fingerprint_digests: list[str],
    server_now: datetime,
) -> ActivePairing | None:
    """Find and row-lock the active pairing request for a candidate digest.

    The lookup probes every configured fingerprint-key version (the PR #44
    review P1 rotation-window rule: a candidate that enrolled under a
    retained older version must still find its request). A request past
    ``expires_at`` is flipped to ``EXPIRED`` here — lazy, terminal, in this
    transaction — and reported as absent so the caller creates a fresh one.
    """
    row = conn.execute(
        "SELECT id, activation_code_id, candidate_fingerprint_hmac, "
        "candidate_fingerprint_key_version, display_name, "
        "platform, status, expires_at "
        "FROM device_pairing_requests "
        "WHERE activation_code_id = %s AND candidate_fingerprint_hmac = ANY(%s) "
        "AND status IN ('PENDING', 'APPROVED') "
        "FOR UPDATE",
        (activation_code_id, fingerprint_digests),
    ).fetchone()
    if row is None:
        return None
    pairing = ActivePairing(
        id=str(row[0]),
        activation_code_id=str(row[1]),
        candidate_fingerprint_hmac=str(row[2]),
        candidate_fingerprint_key_version=int(row[3]),
        display_name=str(row[4]),
        platform=str(row[5]),
        status=str(row[6]),
        expires_at=str(row[7]),
    )
    if _pairing_expired(pairing.expires_at, server_now):
        conn.execute(
            "UPDATE device_pairing_requests SET status = 'EXPIRED' WHERE id = %s",
            (pairing.id,),
        )
        return None
    return pairing


def create_pairing_request(
    conn: psycopg.Connection,
    *,
    activation_code_id: str,
    candidate_fingerprint_hmac: str,
    candidate_fingerprint_key_version: int,
    display_name: str,
    platform: str,
    server_now: datetime,
) -> ActivePairing:
    """Create a PENDING pairing request bound to the candidate digest.

    The partial unique index ``uq_device_pairing_requests_active`` keeps one
    active row per (code, digest); a concurrent duplicate insert raises
    ``UniqueViolation`` and the route reloads the winner's row.
    """
    pairing_id = str(uuid.uuid4())
    expires_at = (
        (server_now + timedelta(seconds=PAIRING_TTL_SECONDS)).replace(microsecond=0).isoformat()
    )
    conn.execute(
        "INSERT INTO device_pairing_requests "
        "(id, activation_code_id, candidate_fingerprint_hmac, "
        " candidate_fingerprint_key_version, display_name, platform, status, expires_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, 'PENDING', %s)",
        (
            pairing_id,
            activation_code_id,
            candidate_fingerprint_hmac,
            candidate_fingerprint_key_version,
            display_name,
            platform,
            expires_at,
        ),
    )
    return ActivePairing(
        id=pairing_id,
        activation_code_id=activation_code_id,
        candidate_fingerprint_hmac=candidate_fingerprint_hmac,
        candidate_fingerprint_key_version=candidate_fingerprint_key_version,
        display_name=display_name,
        platform=platform,
        status=PAIRING_PENDING,
        expires_at=expires_at,
    )


def approve_pairing_request(
    conn: psycopg.Connection,
    *,
    pairing_id: str,
    approver_device: AuthenticatedDevice,
    server_now: datetime,
) -> str:
    """Approve a PENDING pairing from the first currently-bound device.

    Outcomes (translated by the routes):

    - ``revoked`` — the approver's own binding was released mid-flight: the
      route's unlocked authentication snapshot is not proof the device is
      still ``BOUND`` (a concurrent unbind may have committed after it), so
      the approver row is re-locked and re-validated inside this transaction
      (PR #49 Codex review P1 — a released credential must not be able to
      approve a pairing in the window between authentication and the state
      transition). Lock order: devices → pairing, the tail of the enroll
      route's code → devices → pairing order;
    - ``not_found`` — no such pairing, or it belongs to another activation
      code: both answer identically (no IDOR oracle, the DELETE precedent);
    - ``forbidden`` — the approver is a bound device of this very code but
      not the *first* device: §12.2 step 3 grants the approval lane to the
      first currently-bound device (validated against
      ``activation_code_activations.first_device_id``, written once at
      activation and never rewritten — an unlocked read is race-free), and
      once that device is unavailable the lane moves to the T18
      administrator verification, never down to the surviving slot 2 (PR
      #49 GitHub Codex review P1). The check runs *before* the state
      machine, so a non-first device cannot even re-approve;
    - ``expired`` — the request lapsed; the row flips to ``EXPIRED`` here
      (a lapsed APPROVED flips too — its approval lineage stays visible
      while the partial-unique occupancy is released, PR #49 Codex review
      P2);
    - ``already_approved`` — idempotent re-approval, the current state stays;
    - ``already_consumed`` — terminal; the one-shot request is spent;
    - ``self_approval`` — defensive depth: the pairing names the approver's
      own fingerprint (enroll structurally prevents this, but the check
      keeps a tampered row from laundering an approval);
    - ``approved`` — PENDING → APPROVED with the approver lineage recorded.
    """
    approver_row = conn.execute(
        "SELECT status, fingerprint_hmac FROM customer_devices WHERE id = %s FOR UPDATE",
        (approver_device.id,),
    ).fetchone()
    if approver_row is None or str(approver_row[0]) != BOUND:
        return APPROVE_REVOKED
    row = conn.execute(
        "SELECT activation_code_id, candidate_fingerprint_hmac, status, expires_at "
        "FROM device_pairing_requests WHERE id = %s FOR UPDATE",
        (pairing_id,),
    ).fetchone()
    if row is None or str(row[0]) != approver_device.activation_code_id:
        return APPROVE_NOT_FOUND
    # §12.2 step 3 (PR #49 GitHub Codex review P1): the approval is the
    # *first* currently-bound device's lane. ``first_device_id`` is written
    # once at activation and never rewritten, so the unlocked read is
    # race-free; a missing or NULL fact row fails closed (the pairing is a
    # post-activation artefact, so the fact row structurally exists).
    activation_row = conn.execute(
        "SELECT first_device_id FROM activation_code_activations WHERE code_id = %s",
        (approver_device.activation_code_id,),
    ).fetchone()
    if activation_row is None or str(activation_row[0]) != approver_device.id:
        return APPROVE_FORBIDDEN
    status = str(row[2])
    if status == PAIRING_CONSUMED:
        return APPROVE_ALREADY_CONSUMED
    if status == PAIRING_EXPIRED or _pairing_expired(str(row[3]), server_now):
        if status in (PAIRING_PENDING, PAIRING_APPROVED):
            conn.execute(
                "UPDATE device_pairing_requests SET status = 'EXPIRED' WHERE id = %s",
                (pairing_id,),
            )
        return APPROVE_EXPIRED
    if status == PAIRING_APPROVED:
        return APPROVE_ALREADY_APPROVED
    # status == PENDING here.
    if str(approver_row[1]) == str(row[1]):
        return APPROVE_SELF
    conn.execute(
        "UPDATE device_pairing_requests "
        "SET status = 'APPROVED', approved_at = %s, approved_by_device_id = %s "
        "WHERE id = %s",
        (server_now.isoformat(), approver_device.id, pairing_id),
    )
    return APPROVE_APPROVED


def consume_pairing_request(
    conn: psycopg.Connection,
    *,
    pairing: ActivePairing,
    hmac_key: bytes,
    token_key_version: int,
    owner_user_id: str,
    fingerprint_canonical: str,
    server_now: datetime,
) -> ConsumedPairing | None:
    """Consume an APPROVED pairing: bind the candidate to the free slot.

    The caller holds the code-row lock and the current device-row locks (the
    route locks them in that order before the pairing row), so
    ``next_free_slot`` runs under the serialization the §12.2 contract
    demands; ``None`` means both slots are BOUND (the pairing row stays
    APPROVED — a freed slot may yet consume it inside the expiry window).
    On success the request flips to ``CONSUMED`` with the binding recorded,
    and the one-time device credential is returned for the route to seal.

    The fingerprint digest and its key version are copied from the pairing
    row — the digest was keyed when the request was created, possibly under
    a version that has since been rotated below the highest one, and the
    stored pair must stay truthful (PR #49 Codex review P2). The fresh
    device token is keyed with the caller's current highest version.

    ``fingerprint_canonical`` is the caller's current lowest-retained-key
    digest of the same physical fingerprint (revision 034): the cross-version
    probe key that keeps a re-enroll of a released device detectable even
    after the row's own ``fingerprint_hmac`` version falls out of the
    retained set (the activation-route M2 precedent).
    """
    slot_no = next_free_slot(conn, pairing.activation_code_id)
    if slot_no is None:
        return None
    device_id = str(uuid.uuid4())
    device_token = secrets.token_urlsafe(32)
    token_digest = keyed_digest(hmac_key, device_token)
    now_iso = server_now.isoformat()
    conn.execute(
        "INSERT INTO customer_devices "
        "(id, activation_code_id, user_id, slot_no, display_name, platform, "
        " fingerprint_hmac, fingerprint_key_version, fingerprint_canonical, "
        " token_digest, token_key_version, bound_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            device_id,
            pairing.activation_code_id,
            owner_user_id,
            slot_no,
            pairing.display_name,
            pairing.platform,
            pairing.candidate_fingerprint_hmac,
            pairing.candidate_fingerprint_key_version,
            fingerprint_canonical,
            token_digest,
            token_key_version,
            now_iso,
        ),
    )
    conn.execute(
        "UPDATE device_pairing_requests "
        "SET status = 'CONSUMED', consumed_at = %s, consumed_device_id = %s "
        "WHERE id = %s",
        (now_iso, device_id, pairing.id),
    )
    return ConsumedPairing(
        device_id=device_id,
        slot_no=slot_no,
        device_token=device_token,
    )


# ---------------------------------------------------------------------------
# Administrator device operations (T18, dev doc §12.2 step 3 / §9.2 / §15)
# ---------------------------------------------------------------------------


def _insert_admin_device_event(
    conn: psycopg.Connection,
    *,
    event: str,
    admin_user_id: str,
    target_user_id: str,
    device_id: str | None,
    pairing_request_id: str | None,
    activation_code_id: str,
    reason: str,
    request_id: str,
) -> None:
    """Insert one append-only administrator device-operation audit row.

    Revision 038: every admin lane (approve / unbind / revoke) writes the
    real actor, the affected customer and the operator-supplied reason —
    the T18 DoD (真实 actor、原因、二次确认和审计存在). The append-only
    trigger and the shared TRUNCATE guard (revision 036) protect the table
    the same way they protect the other audit trails.
    """
    conn.execute(
        "INSERT INTO admin_device_events "
        "(id, event, admin_user_id, target_user_id, device_id, "
        " pairing_request_id, activation_code_id, reason, request_id) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            str(uuid.uuid4()),
            event,
            admin_user_id,
            target_user_id,
            device_id,
            pairing_request_id,
            activation_code_id,
            reason,
            request_id,
        ),
    )


def admin_approve_pairing_request(
    conn: psycopg.Connection,
    *,
    pairing_id: str,
    admin_user_id: str,
    reason: str,
    request_id: str,
    server_now: datetime,
) -> str:
    """Approve a PENDING pairing as the administrator fallback lane.

    Dev doc §12.2 step 3: the approval belongs to the *first currently
    bound* device; only when that device is no longer ``BOUND`` (released
    or revoked) does the lane move to the administrator verification —
    exactly the precondition checked first here, so a live first device is
    never shortcut by an admin (``first_device_available`` tells the route
    to answer 403).

    Lock order devices → pairing, the tail of the enroll route's
    code → devices → pairing order (the T17 approve precedent):

    - the pairing header is read unlocked once for its ``code_id`` — the
      column is immutable, so the read is race-free (the
      ``first_device_id`` precedent);
    - the first device row is locked ``FOR UPDATE`` and checked for
      ``BOUND``: still-bound → ``first_device_available`` (fail closed for
      the admin lane, checked before the state machine exactly like the
      T17 ``revoked`` gate), a missing row counts as unavailable (the
      history is never deleted, so this is the corruption corner and the
      admin verification is the recovery lane);
    - the pairing row is then locked and the T17 state machine replays:
      ``not_found`` / ``already_consumed`` / ``expired`` (lazily flipped,
      an approved-then-lapsed request included) / ``already_approved``
      (idempotent) / ``approved`` — PENDING → APPROVED with the admin
      lineage (``approved_by_admin_user_id``) and the audit row.
    """
    pairing_header = conn.execute(
        "SELECT activation_code_id FROM device_pairing_requests WHERE id = %s",
        (pairing_id,),
    ).fetchone()
    if pairing_header is None:
        return APPROVE_NOT_FOUND
    code_id = str(pairing_header[0])

    activation_row = conn.execute(
        "SELECT user_id, first_device_id FROM activation_code_activations WHERE code_id = %s",
        (code_id,),
    ).fetchone()
    if activation_row is None:
        # A pairing is a post-activation artefact, so the fact row
        # structurally exists; a missing one fails closed like the T17 lane.
        return APPROVE_NOT_FOUND

    first_device_row = conn.execute(
        "SELECT status FROM customer_devices WHERE id = %s FOR UPDATE",
        (str(activation_row[1]),),
    ).fetchone()
    if first_device_row is not None and str(first_device_row[0]) == BOUND:
        return ADMIN_APPROVE_FIRST_DEVICE_AVAILABLE

    row = conn.execute(
        "SELECT activation_code_id, status, expires_at "
        "FROM device_pairing_requests WHERE id = %s FOR UPDATE",
        (pairing_id,),
    ).fetchone()
    if row is None or str(row[0]) != code_id:
        return APPROVE_NOT_FOUND
    status = str(row[1])
    if status == PAIRING_CONSUMED:
        return APPROVE_ALREADY_CONSUMED
    if status == PAIRING_EXPIRED or _pairing_expired(str(row[2]), server_now):
        if status in (PAIRING_PENDING, PAIRING_APPROVED):
            conn.execute(
                "UPDATE device_pairing_requests SET status = 'EXPIRED' WHERE id = %s",
                (pairing_id,),
            )
        return APPROVE_EXPIRED
    if status == PAIRING_APPROVED:
        return APPROVE_ALREADY_APPROVED
    # status == PENDING here: the admin lineage replaces the device lineage
    # (the _APPROVAL_LINEAGE shape of revision 038 allows exactly one).
    conn.execute(
        "UPDATE device_pairing_requests "
        "SET status = 'APPROVED', approved_at = %s, approved_by_admin_user_id = %s "
        "WHERE id = %s",
        (server_now.isoformat(), admin_user_id, pairing_id),
    )
    _insert_admin_device_event(
        conn,
        event=ADMIN_EVENT_PAIRING_APPROVED,
        admin_user_id=admin_user_id,
        target_user_id=str(activation_row[0]),
        device_id=None,
        pairing_request_id=pairing_id,
        activation_code_id=code_id,
        reason=reason,
        request_id=request_id,
    )
    return APPROVE_APPROVED


def admin_unbind_device(
    conn: psycopg.Connection,
    *,
    device_id: str,
    admin_user_id: str,
    reason: str,
    request_id: str,
    server_now: datetime,
) -> str:
    """Release a ``BOUND`` device as the administrator (T18, §9.2/§15).

    The same state transition as the customer-lane ``unbind_device`` (slot
    released, history kept), with two admin-lane differences: no owner
    check (the administrator operates any device by design) and the audit
    row names the real admin actor with the operator-supplied reason. The
    session riding the released device is revoked atomically, with the
    acting user recorded as the administrator.
    """
    row = conn.execute(
        "SELECT status, user_id, activation_code_id FROM customer_devices WHERE id = %s FOR UPDATE",
        (device_id,),
    ).fetchone()
    if row is None:
        return OUTCOME_NOT_FOUND
    if str(row[0]) != BOUND:
        return OUTCOME_NOT_BOUND
    owner_user_id = str(row[1])
    activation_code_id = str(row[2])

    now_iso = server_now.isoformat()
    conn.execute(
        "UPDATE customer_devices SET status = 'UNBOUND', unbound_at = %s WHERE id = %s",
        (now_iso, device_id),
    )
    _revoke_session_riding_device(
        conn,
        device_id=device_id,
        owner_user_id=owner_user_id,
        activation_code_id=activation_code_id,
        actor_user_id=admin_user_id,
        reason=reason,
        request_id=request_id,
        now_iso=now_iso,
    )
    _insert_admin_device_event(
        conn,
        event=ADMIN_EVENT_DEVICE_UNBOUND,
        admin_user_id=admin_user_id,
        target_user_id=owner_user_id,
        device_id=device_id,
        pairing_request_id=None,
        activation_code_id=activation_code_id,
        reason=reason,
        request_id=request_id,
    )
    return OUTCOME_UNBOUND


def revoke_device_credential(
    conn: psycopg.Connection,
    *,
    device_id: str,
    admin_user_id: str,
    reason: str,
    request_id: str,
    server_now: datetime,
) -> str:
    """Flip a ``BOUND`` device to ``REVOKED`` (T18, §9.2/§15).

    Revocation is the stronger release: the credential is invalidated
    without the customer's participation (the leaked-device response), the
    row keeps the ``REVOKED``/``revoked_at`` shape of revision 028
    (``unbound_at`` stays NULL), and the live session riding the device is
    revoked atomically — epoch bump, lease pulled into the past, ``LOGOUT``
    event — exactly like the unbind lane.
    """
    row = conn.execute(
        "SELECT status, user_id, activation_code_id FROM customer_devices WHERE id = %s FOR UPDATE",
        (device_id,),
    ).fetchone()
    if row is None:
        return OUTCOME_NOT_FOUND
    if str(row[0]) != BOUND:
        return OUTCOME_NOT_BOUND
    owner_user_id = str(row[1])
    activation_code_id = str(row[2])

    now_iso = server_now.isoformat()
    conn.execute(
        "UPDATE customer_devices SET status = 'REVOKED', revoked_at = %s WHERE id = %s",
        (now_iso, device_id),
    )
    _revoke_session_riding_device(
        conn,
        device_id=device_id,
        owner_user_id=owner_user_id,
        activation_code_id=activation_code_id,
        actor_user_id=admin_user_id,
        reason=reason,
        request_id=request_id,
        now_iso=now_iso,
    )
    _insert_admin_device_event(
        conn,
        event=ADMIN_EVENT_CREDENTIAL_REVOKED,
        admin_user_id=admin_user_id,
        target_user_id=owner_user_id,
        device_id=device_id,
        pairing_request_id=None,
        activation_code_id=activation_code_id,
        reason=reason,
        request_id=request_id,
    )
    return OUTCOME_REVOKED
