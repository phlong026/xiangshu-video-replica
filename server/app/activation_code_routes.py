"""T13 / ACT-05 — the first-activation atomic transaction.

``POST /api/customer/activate`` redeems an ISSUED activation code and creates
the whole customer identity chain in exactly one PostgreSQL transaction (dev
doc §12.1): a server-generated ``customer`` user, the funded wallet, the
activation fact, the slot-1 device with keyed credential digests, the PAID
``provider=activation_code`` first-charge order priced by the frozen batch
snapshot, the unique CHARGE ledger row, the ACTIVE code state and the
epoch-1 session with a 90-second lease — all or nothing.

Idempotency envelope (revision 029, ``customer_idempotency_envelopes``): the
engine lives in ``app.customer_idempotency`` since T14 / ACT-07 so the later
customer write paths (second-device enroll, login, recharge) share one
contract. The raw client key never reaches the database — only its SHA-256
digest. The envelope placeholder is inserted first with ``ON CONFLICT DO
NOTHING``, so
concurrent same-key writers serialize on the unique index; the winner seals
the one-time response into an AES-GCM envelope (keyed digests of the device
fingerprint / credentials never persist in plaintext), and a same-key retry
replays the stored response with ``X-Idempotent-Replay: true``. The same key
against a different request body answers 409 ``IDEMPOTENCY_CONFLICT``. A
business failure rolls the placeholder back with the transaction, so the key
stays reusable.

Anti-enumeration (ACT-08 groundwork): every code-side rejection — unknown,
malformed, undelivered, expired, suspended, revoked or already active — is
the single unified 400 ``ACTIVATION_UNAVAILABLE`` with a message that never
distinguishes the sub-state. A device fingerprint already bound to a live
customer answers 409 ``USER_ALREADY_ACTIVATED``; the concurrent race for one
fingerprint is settled by the partial unique index
``uq_customer_devices_fingerprint`` (§11.3).

No-Go red lines: no plaintext activation code, device token or session token
in a column, event, envelope scope, log record or error message — only keyed
digests; the one-time response exists in plaintext only in the HTTP response
and inside the AEAD envelope column.

PostgreSQL is the customer source of truth, so without a PG runtime the route
fails closed with 503 (SQLite stays the internal P0 lane).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import uuid
from datetime import UTC, datetime, timedelta

import psycopg
from fastapi import APIRouter, HTTPException, Request, Response
from psycopg.errors import UniqueViolation
from pydantic import BaseModel, ConfigDict, Field

from app.activation_code_service import (
    ActivationKeyError,
    InvalidActivationCodeError,
    iter_code_digests,
    normalize_activation_code,
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
from app.security_rate_limit import (
    DIMENSION_ACTIVATE_CODE,
    DIMENSION_ACTIVATE_IP,
    RateLimitDecision,
    _server_now,
    activation_code_limit,
    activation_ip_limit,
    apply_anti_enumeration_delay,
    consume_rate_limit,
    failure_alert_active,
    failure_alert_threshold,
    rate_limit_window_seconds,
    record_auth_failure,
)

logger = logging.getLogger(__name__)

IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"
REQUEST_ID_HEADER = "X-Request-Id"
REPLAY_HEADER = "X-Idempotent-Replay"
RETRY_AFTER_HEADER = "Retry-After"

DEVICE_FINGERPRINT_HMAC_KEY_ENV = "VIDEO_REPLICA_DEVICE_FINGERPRINT_HMAC_KEY"

# T19 / SES-01: heartbeat every 30 seconds, lease 90 seconds. The activation
# transaction grants the first lease; renewals are the T19 application layer.
SESSION_LEASE_SECONDS = 90
MIN_HMAC_KEY_BYTES = 32
MAX_KEY_VERSION = 64
USERNAME_MAX_ATTEMPTS = 5
FINGERPRINT_UNIQUE_CONSTRAINT = "uq_customer_devices_fingerprint"
# M2 review M1: the cross-version probe key (revision 034) — its unique
# violation is the same "device already holds an activation" fact.
FINGERPRINT_CANONICAL_UNIQUE_CONSTRAINT = "uq_customer_devices_fingerprint_canonical"
USERS_USERNAME_CONSTRAINT = "users_username_key"
ACTIVATE_OPERATION = "activate"

router = APIRouter(prefix="/api/customer", tags=["customer-activation"])


def _http(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


def _unavailable() -> HTTPException:
    """The unified code-side rejection (anti-enumeration, ACT-08 groundwork)."""
    return _http(400, "ACTIVATION_UNAVAILABLE", "The activation code cannot be used.")


def _is_unified_rejection(exc: HTTPException) -> bool:
    """Whether the exception is the single code-side 400 rejection."""
    detail = exc.detail
    return (
        exc.status_code == 400
        and isinstance(detail, dict)
        and detail.get("code") == "ACTIVATION_UNAVAILABLE"
    )


def _audit_code_rejection(
    *,
    code_identifier: str,
    request_id: str,
) -> None:
    """T15 / ACT-08: audit one unified rejection and burn the constant delay.

    The failure event lands in its own autocommit transaction so a rolled-
    back activation transaction never refunds the audit trail. Recording is
    best-effort: a flaky audit write must not turn a legal 400 into a 500,
    so it logs and swallows its own failure. The trailing-window alert
    threshold is checked here and crosses as an ERROR-level log record —
    the hook the T37 / OPS-02 alerting pipeline consumes.
    """
    try:
        window = rate_limit_window_seconds()
        with pg_transaction() as conn:
            record_auth_failure(
                conn,
                dimension=DIMENSION_ACTIVATE_CODE,
                identifier=code_identifier,
                request_id=request_id,
            )
            if failure_alert_active(
                conn,
                dimension=DIMENSION_ACTIVATE_CODE,
                window_seconds=window,
                threshold=failure_alert_threshold(),
            ):
                logger.error(
                    "security alert: activation code-side failures crossed the "
                    "alert threshold (window=%ss threshold=%s)",
                    window,
                    failure_alert_threshold(),
                )
    except Exception:
        logger.warning("security failure audit unavailable", exc_info=True)
    # The constant-cost delay runs even when the audit write failed: the
    # timing profile must not depend on database health.
    apply_anti_enumeration_delay()


def _probe_replayable_response(
    conn: psycopg.Connection,
    *,
    scope_candidates: list[str],
    key_digest: str,
    req_hash: str,
    response: Response,
) -> CustomerActivationResponse | None:
    """Read-only probe for a replayable idempotency envelope (review P2).

    A fully validated, openable replay short-circuits here — the caller
    returns it *before* the shared rate limiter runs, so a legitimate retry
    (the client lost the response of an already-successful activation)
    spends no abuse budget, honouring the T14 contract that retries are
    side-effect free. Every other outcome — no envelope found, a
    request-hash conflict, a purged or expired recovery window, a retired
    key version — returns ``None`` and the request flows on to the original
    limiter + transaction path, whose checks stay authoritative. The probe
    never writes: a fall-through pays one extra indexed point read.
    """
    for scope_candidate in scope_candidates:
        record = load_envelope(
            conn,
            operation=ACTIVATE_OPERATION,
            scope=scope_candidate,
            key_digest=key_digest,
        )
        if record is None:
            continue
        if record.request_hash != req_hash:
            return None
        if record.ciphertext is None or record.key_version is None:
            return None
        if record.recovery_expires_at is not None and (
            datetime.fromisoformat(str(record.recovery_expires_at)) <= _server_now(conn)
        ):
            return None
        try:
            replayed = open_response(
                record.ciphertext,
                key=customer_aead_key(record.key_version),
                aad=envelope_aad(ACTIVATE_OPERATION, scope_candidate, key_digest),
            )
        except IdempotencyKeyError:
            return None
        replay_request_id = replayed.get("request_id")
        response.headers[REPLAY_HEADER] = "true"
        if isinstance(replay_request_id, str) and replay_request_id:
            response.headers[REQUEST_ID_HEADER] = replay_request_id
        # The replay is a security-sensitive event (a one-time credential
        # re-issued from the sealed envelope) — log observably, identifiers
        # only, never plaintext.
        logger.info(
            "customer activation idempotent replay: scope=%s key_version=%s request=%s",
            scope_candidate,
            record.key_version,
            replay_request_id if isinstance(replay_request_id, str) else "-",
        )
        return _response_from_payload(replayed)
    return None


def _generate_customer_username() -> str:
    """Server-generated identity: no code fragment, phone or enumerable order."""
    return f"customer-{secrets.token_hex(6)}"


# ---------------------------------------------------------------------------
# Versioned keys (T11 activation-code-service precedent, device domain)
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


def _highest_device_domain_key() -> tuple[int, bytes]:
    configured = _configured_key_versions(DEVICE_FINGERPRINT_HMAC_KEY_ENV)
    if not configured:
        raise ActivationKeyError(f"no {DEVICE_FINGERPRINT_HMAC_KEY_ENV} key version is configured")
    version = max(configured)
    return version, _device_domain_hmac_key(version)


# ---------------------------------------------------------------------------
# Keyed digests (§7 / §12.1)
# ---------------------------------------------------------------------------


def _keyed_digest(key: bytes, value: str) -> str:
    return hmac.new(key, value.encode("utf-8"), hashlib.sha256).hexdigest()


# ---------------------------------------------------------------------------
# Request / response contracts
# ---------------------------------------------------------------------------


class CustomerActivationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    activation_code: str = Field(min_length=1, max_length=64)
    device_fingerprint: str = Field(min_length=1, max_length=512)
    device_name: str = Field(min_length=1, max_length=128)
    device_platform: str = Field(min_length=1, max_length=64)


class CustomerActivationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str
    user_id: str
    device_id: str
    device_token: str
    session_token: str
    session_epoch: int
    session_lease_expires_at: str


_RESPONSE_FIELDS = (
    "username",
    "user_id",
    "device_id",
    "device_token",
    "session_token",
    "session_epoch",
    "session_lease_expires_at",
)


def _response_from_payload(payload: dict[str, object]) -> CustomerActivationResponse:
    return CustomerActivationResponse.model_validate(
        {field: payload[field] for field in _RESPONSE_FIELDS}
    )


# ---------------------------------------------------------------------------
# The activation business (one transaction, §12.1)
# ---------------------------------------------------------------------------


def _insert_customer_user(conn: psycopg.Connection) -> tuple[str, str]:
    """Create the server-named customer user; username collisions regenerate.

    The insert runs inside a savepoint so a ``users.username`` collision only
    rolls the attempt back and a fresh candidate retries inside the same
    activation transaction (§12.1 note: recovery must never create a second
    user for the same idempotency key).
    """
    for _ in range(USERNAME_MAX_ATTEMPTS):
        username = _generate_customer_username()
        user_id = str(uuid.uuid4())
        try:
            with conn.transaction():
                conn.execute(
                    "INSERT INTO users (id, username, display_name, role) "
                    "VALUES (%s, %s, %s, 'customer')",
                    (user_id, username, username),
                )
        except UniqueViolation as exc:
            constraint = exc.diag.constraint_name or ""
            if constraint == USERS_USERNAME_CONSTRAINT:
                continue
            raise
        return user_id, username
    raise _http(
        409,
        "USERNAME_UNAVAILABLE",
        "Unable to allocate a username for this activation.",
    )


def _run_activation(
    conn: psycopg.Connection,
    *,
    canonical_code: str,
    code_digests: list[str],
    fingerprint_digests: list[str],
    fingerprint_key_version: int,
    hmac_key: bytes,
    device_name: str,
    device_platform: str,
    request_id: str,
    server_now: datetime,
) -> dict[str, object]:
    unavailable = _unavailable()
    # Lock the code row: 100 concurrent activations of one code serialize
    # here and every loser observes the winner's ACTIVE state.
    code_row = conn.execute(
        "SELECT c.id, c.status, "
        "b.unit_price_fen_snapshot, b.credits_snapshot, b.activation_expires_at "
        "FROM activation_codes c "
        "JOIN activation_code_batches b ON b.id = c.batch_id "
        "WHERE c.code_digest = ANY(%s) "
        "FOR UPDATE OF c",
        (code_digests,),
    ).fetchone()
    if code_row is None:
        raise unavailable
    code_id = str(code_row[0])
    code_status = str(code_row[1])
    unit_price_fen = int(code_row[2])
    credits = int(code_row[3])
    batch_expiry = str(code_row[4])
    if code_status != "ISSUED":
        # GENERATED / ACTIVE / SUSPENDED / REVOKED / EXPIRED all answer the
        # same unified rejection (anti-enumeration).
        raise unavailable
    # T12 accepts naive batch-expiry timestamps (coerced to UTC in memory at
    # creation), so a naive stored string must be coerced the same way here —
    # otherwise the aware-vs-naive comparison would raise and turn a legal
    # batch into a 500 on every activation attempt (PR review P2).
    expires_at = datetime.fromisoformat(batch_expiry)
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if expires_at <= server_now:
        raise unavailable

    # Fast-path fingerprint check; the concurrent race is settled by the
    # partial unique index uq_customer_devices_fingerprint (§11.3).
    # PR #44 review P1: check every retained fingerprint-key version — during
    # a rotation window (V1 retained, V2 added) a device bound under the older
    # version must still be recognized, or the same physical device could
    # redeem a second code and receive a second user, wallet and first charge.
    bound = conn.execute(
        "SELECT 1 FROM customer_devices "
        "WHERE (fingerprint_hmac = ANY(%s) OR fingerprint_canonical = %s) "
        "AND status = 'BOUND'",
        (fingerprint_digests, fingerprint_digests[0]),
    ).fetchone()
    if bound is not None:
        raise _http(
            409,
            "USER_ALREADY_ACTIVATED",
            "This device already holds a customer activation.",
        )

    user_id, username = _insert_customer_user(conn)

    conn.execute(
        "INSERT INTO wallets (user_id, available_credits, reserved_credits) VALUES (%s, %s, 0)",
        (user_id, credits),
    )

    device_id = str(uuid.uuid4())
    device_token = secrets.token_urlsafe(32)
    token_digest = _keyed_digest(hmac_key, device_token)
    conn.execute(
        "INSERT INTO customer_devices "
        "(id, activation_code_id, user_id, slot_no, display_name, platform, "
        " fingerprint_hmac, fingerprint_key_version, fingerprint_canonical, "
        " token_digest, token_key_version) "
        "VALUES (%s, %s, %s, 1, %s, %s, %s, %s, %s, %s, %s)",
        (
            device_id,
            code_id,
            user_id,
            device_name,
            device_platform,
            # New bindings always carry the highest configured key version.
            fingerprint_digests[-1],
            fingerprint_key_version,
            # M2 review M1: the cross-version probe key — the *lowest*
            # retained version's digest. In an add-version rollout window
            # every instance shares that lowest version, so the canonical
            # value is identical across a heterogeneous fleet and the
            # binding check / unique index cross the version boundary.
            fingerprint_digests[0],
            token_digest,
            fingerprint_key_version,
        ),
    )

    # The PAID first-charge order, priced by the frozen batch snapshot:
    # credits x frozen unit price. PRICE-01 keeps the charged price at or
    # above the order's own base snapshot; the activation lane carries no
    # third-party trade number (revision 026 shapes).
    order_id = str(uuid.uuid4())
    merchant_order_no = f"ACT-{uuid.uuid4().hex}"
    amount_fen = credits * unit_price_fen
    now_iso = server_now.replace(microsecond=0).isoformat()
    conn.execute(
        "INSERT INTO recharge_orders "
        "(id, user_id, merchant_order_no, provider, provider_trade_no, channel, "
        " status, pricing_scope, base_unit_price_fen_snapshot, "
        " charged_unit_price_fen_snapshot, min_recharge_fen_snapshot, "
        " recharge_step_fen_snapshot, amount_fen, credits, paid_at) "
        "VALUES (%s, %s, %s, 'activation_code', NULL, NULL, 'PAID', "
        " 'CUSTOMER_STANDARD', %s, %s, %s, %s, %s, %s, %s)",
        (
            order_id,
            user_id,
            merchant_order_no,
            unit_price_fen,
            unit_price_fen,
            1,
            1,
            amount_fen,
            credits,
            now_iso,
        ),
    )
    conn.execute(
        "INSERT INTO wallet_transactions "
        "(id, user_id, type, available_delta, reserved_delta, recharge_order_id, "
        " idempotency_key) "
        "VALUES (%s, %s, 'CHARGE', %s, 0, %s, %s)",
        (str(uuid.uuid4()), user_id, credits, order_id, f"activation_code:charge:{order_id}"),
    )

    conn.execute(
        "INSERT INTO activation_code_activations "
        "(id, code_id, user_id, first_device_id, recharge_order_id) "
        "VALUES (%s, %s, %s, %s, %s)",
        (str(uuid.uuid4()), code_id, user_id, device_id, order_id),
    )

    conn.execute(
        "UPDATE activation_codes "
        "SET status = 'ACTIVE', activated_at = %s, bound_user_id = %s "
        "WHERE id = %s",
        (now_iso, user_id, code_id),
    )
    conn.execute(
        "INSERT INTO activation_code_events (id, code_id, event, actor_user_id, request_id) "
        "VALUES (%s, %s, 'ACTIVATED', %s, %s)",
        (str(uuid.uuid4()), code_id, user_id, request_id),
    )

    # Epoch-1 session on the server clock with the 90-second lease.
    session_token = secrets.token_urlsafe(32)
    session_token_digest = _keyed_digest(hmac_key, session_token)
    session_id = str(uuid.uuid4())
    lease_until = (server_now + timedelta(seconds=SESSION_LEASE_SECONDS)).replace(microsecond=0)
    conn.execute(
        "INSERT INTO customer_session_state "
        "(user_id, activation_code_id, device_id, session_id, token_digest, "
        " session_epoch, lease_until) "
        "VALUES (%s, %s, %s, %s, %s, 1, %s)",
        (user_id, code_id, device_id, session_id, session_token_digest, lease_until.isoformat()),
    )
    conn.execute(
        "INSERT INTO customer_session_events "
        "(id, event, user_id, activation_code_id, device_id, session_id, "
        " session_epoch, actor_user_id, request_id) "
        "VALUES (%s, 'ACTIVATED', %s, %s, %s, %s, 1, %s, %s)",
        (str(uuid.uuid4()), user_id, code_id, device_id, session_id, user_id, request_id),
    )

    return {
        "username": username,
        "user_id": user_id,
        "device_id": device_id,
        "device_token": device_token,
        "session_token": session_token,
        "session_epoch": 1,
        "session_lease_expires_at": lease_until.isoformat(),
        "request_id": request_id,
    }


# ---------------------------------------------------------------------------
# The route
# ---------------------------------------------------------------------------


@router.post("/activate", response_model=CustomerActivationResponse, status_code=201)
def activate_first_device(
    body: CustomerActivationRequest,
    request: Request,
    response: Response,
) -> CustomerActivationResponse:
    """Redeem an activation code and create the whole customer chain atomically."""
    idempotency_key = request.headers.get(IDEMPOTENCY_KEY_HEADER, "").strip()
    if not idempotency_key:
        raise _http(400, "IDEMPOTENCY_KEY_REQUIRED", "An Idempotency-Key header is required.")

    # T15 / ACT-08: a normalization failure must not short-circuit — the
    # request still passes through the shared IP-dimension limiter below, so
    # a malformed-code burst (format probing) cannot skirt the abuse budget.
    # The unified rejection (audit + constant delay) fires after the limiter.
    canonical_code: str | None
    try:
        canonical_code = normalize_activation_code(body.activation_code)
    except InvalidActivationCodeError:
        canonical_code = None

    fingerprint = body.device_fingerprint.strip()
    device_name = body.device_name.strip()
    device_platform = body.device_platform.strip()
    if not fingerprint or not device_name or not device_platform:
        raise _http(
            400, "INVALID_DEVICE_INFO", "Device fingerprint, name and platform are required."
        )

    # Fail closed first when the PG runtime is not configured (SQLite
    # internal lane): the runtime is the more fundamental precondition, so a
    # misconfigured deployment reports the service outage, not a key problem.
    try:
        get_pg_pool()
    except (RuntimeError, ValueError) as exc:
        raise _http(
            503,
            "ACTIVATION_SERVICE_UNAVAILABLE",
            "Customer activation requires the PostgreSQL runtime.",
        ) from exc

    try:
        fingerprint_key_version, hmac_key = _highest_device_domain_key()
        aead_key_version, aead_key = highest_customer_aead_key()
        code_digests = (
            [digest for digest, _version in iter_code_digests(canonical_code)]
            if canonical_code is not None
            else []
        )
    except (ActivationKeyError, IdempotencyKeyError):
        logger.warning("activation keys unavailable: configuration is incomplete")
        raise _http(
            503,
            "ACTIVATION_KEYS_UNAVAILABLE",
            "Activation keys are not configured; activation is refused.",
        ) from None

    # PR #44 review P1: during a rotation window several device-domain key
    # versions stay configured at once. Compute the fingerprint digest under
    # *every* retained version — the binding check (inside the transaction)
    # and the envelope-scope lookup below must both recognize an identity
    # established under an older version, while new rows always carry the
    # highest version's digest.
    fingerprint_digests = [
        _keyed_digest(_device_domain_hmac_key(version), fingerprint)
        for version in _configured_key_versions(DEVICE_FINGERPRINT_HMAC_KEY_ENV)
    ]
    fingerprint_hmac = fingerprint_digests[-1]
    scope_candidates = list(reversed(fingerprint_digests))
    key_digest = idempotency_key_digest(idempotency_key)
    # canonical_code is None only for malformed codes; those requests are
    # rejected before any envelope is ever written, so their hash value can
    # never match a stored envelope — the empty string just keeps the
    # request-hash dict str-typed.
    req_hash = compute_request_hash(
        {
            "activation_code": canonical_code or "",
            "device_fingerprint": fingerprint,
            "device_name": device_name,
            "device_platform": device_platform,
        }
    )
    request_id = request.headers.get(REQUEST_ID_HEADER, "").strip() or str(uuid.uuid4())

    # Session review P2 (T15 / ACT-08): a legitimate idempotent retry — the
    # client lost the response of an already-successful activation — must
    # replay from the sealed envelope without spending any rate-limit
    # budget. The T14 contract keeps retries side-effect free; charging
    # them against the shared code budget would lock a legal user out of
    # their own cached response after a few network retries. Only a fully
    # validated, openable replay short-circuits here; every other envelope
    # state falls through to the original flow below.
    with pg_transaction() as conn:
        replayed_response = _probe_replayable_response(
            conn,
            scope_candidates=scope_candidates,
            key_digest=key_digest,
            req_hash=req_hash,
            response=response,
        )
    if replayed_response is not None:
        return replayed_response

    # T15 / ACT-08: the shared PG-backed rate limiter. The IP dimension is
    # consumed by *every* activation attempt — malformed codes included, or
    # a format-probing burst would bypass the abuse budget. The code
    # dimension exists to stop one real code being hammered; a malformed
    # input has no digest, so it draws no code-dimension budget (the failure
    # audit still counts it under the fixed "malformed" identifier). Both
    # are consumed in one autocommit transaction *outside* the activation
    # transaction below: the budget must never be refunded when the business
    # transaction rolls back — a rejected attempt is exactly what the
    # limiter exists to count, and every API instance behind the load
    # balancer draws from this same PostgreSQL budget.
    client_ip = request.client.host if request.client is not None else "unknown"
    _window_seconds = rate_limit_window_seconds()
    with pg_transaction() as conn:
        ip_decision = consume_rate_limit(
            conn,
            dimension=DIMENSION_ACTIVATE_IP,
            identifier=client_ip,
            limit=activation_ip_limit(),
            window_seconds=_window_seconds,
        )
        code_decision: RateLimitDecision | None = None
        # PR #46 review P1: once the IP dimension blocks, the request is
        # already dead — consuming the code dimension anyway would let a
        # blocked client mint one unbounded counter row per random
        # well-shaped code (the code identifier is attacker-controlled and
        # expired rows are never swept), growing the table without bound.
        # A blocked IP draws no code budget: its requests never reach the
        # business layer the code dimension exists to protect.
        if code_digests and ip_decision.allowed:
            code_decision = consume_rate_limit(
                conn,
                dimension=DIMENSION_ACTIVATE_CODE,
                identifier=code_digests[0],
                limit=activation_code_limit(),
                window_seconds=_window_seconds,
            )
    if not ip_decision.allowed or (code_decision is not None and not code_decision.allowed):
        retry_after = max(
            ip_decision.retry_after_seconds,
            code_decision.retry_after_seconds if code_decision is not None else 0,
        )
        # The 429 leaves via raise, so the header rides the exception —
        # mutating the response object here would be lost on the error path.
        blocked = _http(
            429,
            "RATE_LIMITED",
            "Too many activation attempts; retry later.",
        )
        blocked.headers = {RETRY_AFTER_HEADER: str(retry_after)}
        raise blocked

    if canonical_code is None:
        # T15 / ACT-08: the malformed rejection joins the unified audit and
        # constant-delay path (no valid digest exists for it — the fixed
        # "malformed" identifier keeps the failure countable).
        _audit_code_rejection(
            code_identifier="malformed",
            request_id=request.headers.get(REQUEST_ID_HEADER, "").strip() or str(uuid.uuid4()),
        )
        raise _unavailable() from None
    assert canonical_code is not None  # the malformed branch above returned

    recovery_seconds = recovery_window_seconds()

    try:
        with pg_transaction() as conn:
            # Key reuse may have scoped its envelope under an older fingerprint
            # digest before a rotation: look through every configured version's
            # scope (highest first) before inserting a fresh placeholder.
            found_scope: str | None = None
            record: EnvelopeRecord | None = None
            for scope_candidate in scope_candidates:
                loaded = load_envelope(
                    conn,
                    operation=ACTIVATE_OPERATION,
                    scope=scope_candidate,
                    key_digest=key_digest,
                )
                if loaded is not None:
                    found_scope = scope_candidate
                    record = loaded
                    break

            envelope_id: str | None = None
            if record is None:
                envelope_id = insert_envelope(
                    conn,
                    operation=ACTIVATE_OPERATION,
                    scope=fingerprint_hmac,
                    key_digest=key_digest,
                    request_hash=req_hash,
                )
                if envelope_id is None:
                    # Concurrent same-key writer won the placeholder insert (the
                    # insert blocked on the unique index until the other
                    # transaction committed): load the committed envelope back.
                    loaded = load_envelope(
                        conn,
                        operation=ACTIVATE_OPERATION,
                        scope=fingerprint_hmac,
                        key_digest=key_digest,
                    )
                    if loaded is not None:
                        found_scope = fingerprint_hmac
                        record = loaded

            if record is not None:
                assert found_scope is not None
                if record.request_hash != req_hash:
                    raise _http(
                        409,
                        "IDEMPOTENCY_CONFLICT",
                        "This idempotency key was already used for a different request.",
                    )
                ciphertext = record.ciphertext
                key_version = record.key_version
                if ciphertext is None or key_version is None:
                    # Purged or never completed: the key is spent and the
                    # response is no longer recoverable (T14 / ACT-07).
                    raise _http(
                        409,
                        "IDEMPOTENCY_CONFLICT",
                        "This idempotency key is no longer recoverable.",
                    )
                if record.recovery_expires_at is not None and (
                    datetime.fromisoformat(str(record.recovery_expires_at)) <= _server_now(conn)
                ):
                    raise _http(
                        409,
                        "IDEMPOTENCY_CONFLICT",
                        "This idempotency key is no longer recoverable.",
                    )
                try:
                    replayed = open_response(
                        ciphertext,
                        key=customer_aead_key(key_version),
                        aad=envelope_aad(ACTIVATE_OPERATION, found_scope, key_digest),
                    )
                except IdempotencyKeyError:
                    # The envelope's key version was retired inside the
                    # recovery window (or the ciphertext is otherwise
                    # unopenable): a server-side failure answers 503, never an
                    # unhandled 500 (error contract §13.2).
                    raise _http(
                        503,
                        "ACTIVATION_SERVICE_UNAVAILABLE",
                        "The activation service cannot recover this response.",
                    ) from None
                replay_request_id = replayed.get("request_id")
                response.headers[REPLAY_HEADER] = "true"
                if isinstance(replay_request_id, str) and replay_request_id:
                    response.headers[REQUEST_ID_HEADER] = replay_request_id
                # The replay is a security-sensitive event (a one-time
                # credential re-issued from the sealed envelope) — log it
                # observably, with identifiers only, never plaintext (PR
                # review P3).
                logger.info(
                    "customer activation idempotent replay: scope=%s key_version=%s request=%s",
                    found_scope,
                    key_version,
                    replay_request_id if isinstance(replay_request_id, str) else "-",
                )
                return _response_from_payload(replayed)

            now_row = conn.execute("SELECT now()").fetchone()
            if now_row is None:
                raise _http(
                    503,
                    "ACTIVATION_SERVICE_UNAVAILABLE",
                    "The database clock is unavailable.",
                )
            server_now = now_row[0]
            if not isinstance(server_now, datetime):
                raise _http(
                    503,
                    "ACTIVATION_SERVICE_UNAVAILABLE",
                    "The database clock is unavailable.",
                )
            # mypy: the replay branch above returns or raises whenever an
            # existing envelope was found, so only a fresh placeholder flows on.
            assert envelope_id is not None
            payload = _run_activation(
                conn,
                canonical_code=canonical_code,
                code_digests=code_digests,
                fingerprint_digests=fingerprint_digests,
                fingerprint_key_version=fingerprint_key_version,
                hmac_key=hmac_key,
                device_name=device_name,
                device_platform=device_platform,
                request_id=request_id,
                server_now=server_now,
            )
            recovery_expires_at = (
                (server_now + timedelta(seconds=recovery_seconds))
                .replace(microsecond=0)
                .isoformat()
            )
            sealed_ciphertext = seal_response(
                payload,
                key=aead_key,
                aad=envelope_aad(ACTIVATE_OPERATION, fingerprint_hmac, key_digest),
            )
            complete_envelope(
                conn,
                envelope_id,
                ciphertext=sealed_ciphertext,
                key_version=aead_key_version,
                recovery_expires_at=recovery_expires_at,
            )
    except UniqueViolation as exc:
        constraint = exc.diag.constraint_name or ""
        if constraint in (
            FINGERPRINT_UNIQUE_CONSTRAINT,
            FINGERPRINT_CANONICAL_UNIQUE_CONSTRAINT,
        ):
            # The concurrent second code on the same fingerprint lost the
            # partial-unique-index race (same-string or cross-version
            # canonical): exactly one binding survives.
            raise _http(
                409,
                "USER_ALREADY_ACTIVATED",
                "This device already holds a customer activation.",
            ) from exc
        raise
    except HTTPException as exc:
        # T15 / ACT-08: the unified code-side rejection is audited and pays
        # the constant anti-enumeration delay on its way out, so unknown,
        # expired, suspended, revoked and already-active codes share one
        # response body *and* one latency profile.
        if _is_unified_rejection(exc):
            _audit_code_rejection(
                code_identifier=code_digests[0],
                request_id=request_id,
            )
        raise

    response.headers[REQUEST_ID_HEADER] = request_id
    # Plaintext code / tokens never reach the logs — only opaque identifiers.
    logger.info(
        "customer activation completed: user=%s device=%s request=%s",
        payload["user_id"],
        payload["device_id"],
        request_id,
    )
    return _response_from_payload(payload)
