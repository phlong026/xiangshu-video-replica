"""T13 / ACT-05 — the first-activation atomic transaction.

``POST /api/customer/activate`` redeems an ISSUED activation code and creates
the whole customer identity chain in exactly one PostgreSQL transaction (dev
doc §12.1): a server-generated ``customer`` user, the funded wallet, the
activation fact, the slot-1 device with keyed credential digests, the PAID
``provider=activation_code`` first-charge order priced by the frozen batch
snapshot, the unique CHARGE ledger row, the ACTIVE code state and the
epoch-1 session with a 90-second lease — all or nothing.

Idempotency envelope (revision 029, ``customer_idempotency_envelopes``): the
raw client key never reaches the database — only its SHA-256 digest. The
envelope placeholder is inserted first with ``ON CONFLICT DO NOTHING``, so
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

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import uuid
from datetime import UTC, datetime, timedelta

import psycopg
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import APIRouter, HTTPException, Request, Response
from psycopg.errors import UniqueViolation
from pydantic import BaseModel, ConfigDict, Field

from app.activation_code_service import (
    ActivationKeyError,
    InvalidActivationCodeError,
    iter_code_digests,
    normalize_activation_code,
)
from app.db_pg import get_pg_pool, pg_transaction

logger = logging.getLogger(__name__)

IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"
REQUEST_ID_HEADER = "X-Request-Id"
REPLAY_HEADER = "X-Idempotent-Replay"

DEVICE_FINGERPRINT_HMAC_KEY_ENV = "VIDEO_REPLICA_DEVICE_FINGERPRINT_HMAC_KEY"
CUSTOMER_IDEMPOTENCY_AEAD_KEY_ENV = "VIDEO_REPLICA_CUSTOMER_IDEMPOTENCY_AEAD_KEY"
CUSTOMER_IDEMPOTENCY_RECOVERY_SECONDS_ENV = "VIDEO_REPLICA_CUSTOMER_IDEMPOTENCY_RECOVERY_SECONDS"

# T19 / SES-01: heartbeat every 30 seconds, lease 90 seconds. The activation
# transaction grants the first lease; renewals are the T19 application layer.
SESSION_LEASE_SECONDS = 90
DEFAULT_RECOVERY_WINDOW_SECONDS = 24 * 60 * 60
MIN_HMAC_KEY_BYTES = 32
AESGCM_KEY_BYTES = 32
AESGCM_NONCE_BYTES = 12
MAX_KEY_VERSION = 64
USERNAME_MAX_ATTEMPTS = 5
FINGERPRINT_UNIQUE_CONSTRAINT = "uq_customer_devices_fingerprint"
USERS_USERNAME_CONSTRAINT = "users_username_key"
ACTIVATE_OPERATION = "activate"

router = APIRouter(prefix="/api/customer", tags=["customer-activation"])


def _http(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


def _unavailable() -> HTTPException:
    """The unified code-side rejection (anti-enumeration, ACT-08 groundwork)."""
    return _http(400, "ACTIVATION_UNAVAILABLE", "The activation code cannot be used.")


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


def _customer_idempotency_aead_key(key_version: int) -> bytes:
    """The response-envelope AEAD key (base64, exactly 32 decoded bytes)."""
    for name in _env_key_candidates(CUSTOMER_IDEMPOTENCY_AEAD_KEY_ENV, key_version):
        value = os.environ.get(name, "").strip()
        if not value:
            continue
        try:
            raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        except ValueError as exc:
            raise ActivationKeyError(f"{name} is not valid base64") from exc
        if len(raw) != AESGCM_KEY_BYTES:
            raise ActivationKeyError(f"{name} must decode to exactly {AESGCM_KEY_BYTES} bytes")
        return raw
    raise ActivationKeyError(
        f"{CUSTOMER_IDEMPOTENCY_AEAD_KEY_ENV} for key version {key_version} is not configured"
    )


def _highest_device_domain_key() -> tuple[int, bytes]:
    configured = _configured_key_versions(DEVICE_FINGERPRINT_HMAC_KEY_ENV)
    if not configured:
        raise ActivationKeyError(f"no {DEVICE_FINGERPRINT_HMAC_KEY_ENV} key version is configured")
    version = max(configured)
    return version, _device_domain_hmac_key(version)


def _highest_customer_aead_key() -> tuple[int, bytes]:
    configured = _configured_key_versions(CUSTOMER_IDEMPOTENCY_AEAD_KEY_ENV)
    if not configured:
        raise ActivationKeyError(
            f"no {CUSTOMER_IDEMPOTENCY_AEAD_KEY_ENV} key version is configured"
        )
    version = max(configured)
    return version, _customer_idempotency_aead_key(version)


def _recovery_window_seconds() -> int:
    raw = os.environ.get(CUSTOMER_IDEMPOTENCY_RECOVERY_SECONDS_ENV, "").strip()
    if not raw:
        return DEFAULT_RECOVERY_WINDOW_SECONDS
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "invalid %s=%r, using default %d",
            CUSTOMER_IDEMPOTENCY_RECOVERY_SECONDS_ENV,
            raw,
            DEFAULT_RECOVERY_WINDOW_SECONDS,
        )
        return DEFAULT_RECOVERY_WINDOW_SECONDS
    return value if value > 0 else DEFAULT_RECOVERY_WINDOW_SECONDS


# ---------------------------------------------------------------------------
# Keyed digests and the AEAD response envelope (§7 / §12.1)
# ---------------------------------------------------------------------------


def _keyed_digest(key: bytes, value: str) -> str:
    return hmac.new(key, value.encode("utf-8"), hashlib.sha256).hexdigest()


def _envelope_aad(operation: str, scope: str, key_digest: str) -> bytes:
    return f"customer-idempotency:{operation}:{scope}:{key_digest}".encode()


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _seal_response(payload: dict[str, object], *, key: bytes, aad: bytes) -> str:
    plaintext = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    nonce = secrets.token_bytes(AESGCM_NONCE_BYTES)
    sealed = AESGCM(key).encrypt(nonce, plaintext, aad)
    return _b64encode(nonce + sealed)


def _open_response(ciphertext: str, *, key: bytes, aad: bytes) -> dict[str, object]:
    try:
        blob = _b64decode(ciphertext)
        nonce, sealed = blob[:AESGCM_NONCE_BYTES], blob[AESGCM_NONCE_BYTES:]
        plaintext = AESGCM(key).decrypt(nonce, sealed, aad)
    except (InvalidTag, ValueError) as exc:
        # A sealed envelope that no configured key can open is a configuration
        # failure, not a client error — refuse loudly instead of replaying
        # garbage (T14 completes the rotation-window story).
        raise ActivationKeyError("idempotency envelope ciphertext verification failed") from exc
    try:
        decoded = json.loads(plaintext)
    except json.JSONDecodeError as exc:
        raise ActivationKeyError("idempotency envelope payload is malformed") from exc
    if not isinstance(decoded, dict):
        raise ActivationKeyError("idempotency envelope payload is malformed")
    return decoded


def _idempotency_key_digest(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _request_hash(canonical_code: str, body: CustomerActivationRequest) -> str:
    # The hash freezes the *normalized* request (the same values the business
    # path uses after strip()), so a retry that only differs in surrounding
    # whitespace still replays instead of conflicting (PR review P3).
    payload = json.dumps(
        {
            "activation_code": canonical_code,
            "device_fingerprint": body.device_fingerprint.strip(),
            "device_name": body.device_name.strip(),
            "device_platform": body.device_platform.strip(),
        },
        separators=(",", ":"),
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


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
# Idempotency envelope persistence (revision 029)
# ---------------------------------------------------------------------------


def _insert_envelope(
    conn: psycopg.Connection,
    *,
    scope: str,
    key_digest: str,
    request_hash: str,
) -> str | None:
    """Insert the placeholder; returns its id, or ``None`` on key reuse."""
    envelope_id = str(uuid.uuid4())
    inserted = conn.execute(
        "INSERT INTO customer_idempotency_envelopes "
        "(id, operation, scope, key_digest, request_hash) "
        "VALUES (%s, %s, %s, %s, %s) "
        "ON CONFLICT (operation, scope, key_digest) DO NOTHING",
        (envelope_id, ACTIVATE_OPERATION, scope, key_digest, request_hash),
    ).rowcount
    return envelope_id if inserted == 1 else None


def _load_envelope(
    conn: psycopg.Connection,
    *,
    scope: str,
    key_digest: str,
) -> tuple[str, str | None, int | None, str | None] | None:
    """Return (request_hash, ciphertext, key_version, recovery_expires_at)."""
    row = conn.execute(
        "SELECT request_hash, ciphertext, key_version, recovery_expires_at "
        "FROM customer_idempotency_envelopes "
        "WHERE operation = %s AND scope = %s AND key_digest = %s",
        (ACTIVATE_OPERATION, scope, key_digest),
    ).fetchone()
    if row is None:
        return None
    return str(row[0]), row[1], row[2], row[3]


def _complete_envelope(
    conn: psycopg.Connection,
    envelope_id: str,
    *,
    ciphertext: str,
    key_version: int,
    recovery_expires_at: str,
) -> None:
    conn.execute(
        "UPDATE customer_idempotency_envelopes "
        "SET ciphertext = %s, key_version = %s, recovery_expires_at = %s "
        "WHERE id = %s",
        (ciphertext, key_version, recovery_expires_at, envelope_id),
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
    fingerprint_hmac: str,
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
    bound = conn.execute(
        "SELECT 1 FROM customer_devices WHERE fingerprint_hmac = %s AND status = 'BOUND'",
        (fingerprint_hmac,),
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
        " fingerprint_hmac, fingerprint_key_version, token_digest, token_key_version) "
        "VALUES (%s, %s, %s, 1, %s, %s, %s, %s, %s, %s)",
        (
            device_id,
            code_id,
            user_id,
            device_name,
            device_platform,
            fingerprint_hmac,
            fingerprint_key_version,
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

    try:
        canonical_code = normalize_activation_code(body.activation_code)
    except InvalidActivationCodeError:
        raise _unavailable() from None

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
        aead_key_version, aead_key = _highest_customer_aead_key()
        code_digests = [digest for digest, _version in iter_code_digests(canonical_code)]
    except ActivationKeyError:
        logger.warning("activation keys unavailable: configuration is incomplete")
        raise _http(
            503,
            "ACTIVATION_KEYS_UNAVAILABLE",
            "Activation keys are not configured; activation is refused.",
        ) from None

    fingerprint_hmac = _keyed_digest(hmac_key, fingerprint)
    scope = fingerprint_hmac
    key_digest = _idempotency_key_digest(idempotency_key)
    request_hash = _request_hash(canonical_code, body)
    aad = _envelope_aad(ACTIVATE_OPERATION, scope, key_digest)
    recovery_seconds = _recovery_window_seconds()
    request_id = request.headers.get(REQUEST_ID_HEADER, "").strip() or str(uuid.uuid4())

    try:
        with pg_transaction() as conn:
            envelope_id = _insert_envelope(
                conn,
                scope=scope,
                key_digest=key_digest,
                request_hash=request_hash,
            )
            if envelope_id is None:
                # Concurrent or sequential key reuse: a committed envelope is
                # the only visible shape here (the insert waited for the other
                # transaction), so anything unrecoverable is a conflict.
                loaded = _load_envelope(conn, scope=scope, key_digest=key_digest)
                if loaded is None or loaded[0] != request_hash:
                    raise _http(
                        409,
                        "IDEMPOTENCY_CONFLICT",
                        "This idempotency key was already used for a different request.",
                    )
                _, ciphertext, key_version, recovery_expires_at = loaded
                if ciphertext is None or key_version is None:
                    # Purged or never completed: the key is spent and the
                    # response is no longer recoverable (T14 refines this).
                    raise _http(
                        409,
                        "IDEMPOTENCY_CONFLICT",
                        "This idempotency key is no longer recoverable.",
                    )
                if recovery_expires_at is not None and (
                    datetime.fromisoformat(str(recovery_expires_at)) <= datetime.now(UTC)
                ):
                    raise _http(
                        409,
                        "IDEMPOTENCY_CONFLICT",
                        "This idempotency key is no longer recoverable.",
                    )
                try:
                    replayed = _open_response(
                        str(ciphertext),
                        key=_customer_idempotency_aead_key(int(key_version)),
                        aad=aad,
                    )
                except ActivationKeyError:
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
                    scope,
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
            payload = _run_activation(
                conn,
                canonical_code=canonical_code,
                code_digests=code_digests,
                fingerprint_hmac=fingerprint_hmac,
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
            ciphertext = _seal_response(payload, key=aead_key, aad=aad)
            _complete_envelope(
                conn,
                envelope_id,
                ciphertext=ciphertext,
                key_version=aead_key_version,
                recovery_expires_at=recovery_expires_at,
            )
    except UniqueViolation as exc:
        constraint = exc.diag.constraint_name or ""
        if constraint == FINGERPRINT_UNIQUE_CONSTRAINT:
            # The concurrent second code on the same fingerprint lost the
            # partial-unique-index race: exactly one binding survives.
            raise _http(
                409,
                "USER_ALREADY_ACTIVATED",
                "This device already holds a customer activation.",
            ) from exc
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
