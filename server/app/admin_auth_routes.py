"""T09 / DB-08 — per-operator admin sessions: exchange, CSRF, RBAC.

Implements the application layer on top of the ``admin_sessions`` table
published by revision 026 (dev doc §15): every operator exchanges a short
lived, single-use HMAC-signed credential for an HttpOnly admin cookie plus a
per-session CSRF token. Only digests ever reach the database; the nonce digest
becomes the session primary key, which makes replaying a consumed credential
fail on the unique constraint instead of issuing a second session.

The legacy proxy-token control identity (``control_auth.get_control_user``)
stays the internal P0 path; customer production rejects it at startup
(``bootstrap.assert_customer_production_security``) and again at runtime.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import string
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from app.db_pg import pg_transaction

logger = logging.getLogger(__name__)

ADMIN_SESSION_COOKIE = "admin_session"
ADMIN_CSRF_HEADER = "X-Admin-CSRF"
ADMIN_SESSION_HMAC_KEY_ENV = "VIDEO_REPLICA_ADMIN_SESSION_HMAC_KEY"
ADMIN_SESSION_TTL_ENV = "VIDEO_REPLICA_ADMIN_SESSION_TTL_SECONDS"
CUSTOMER_PRODUCTION_ENV = "VIDEO_REPLICA_CUSTOMER_PRODUCTION"

DEFAULT_ADMIN_SESSION_TTL_SECONDS = 8 * 3600
MIN_ADMIN_SESSION_TTL_SECONDS = 60
MAX_ADMIN_SESSION_TTL_SECONDS = 24 * 3600
MIN_HMAC_KEY_BYTES = 32
EXCHANGE_CREDENTIAL_PREFIX = "ASX1"
MAX_EXCHANGE_CREDENTIAL_LENGTH = 512
NONCE_HEX_LENGTH = 32
ADMIN_COOKIE_PATH = "/api/control"

_TRUTHY = {"1", "true", "yes", "on"}
_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_ADMIN_ROLES = frozenset({"admin", "auditor"})


class ExchangeCredentialError(ValueError):
    """Raised when an admin exchange credential fails verification."""


def is_customer_production() -> bool:
    return os.environ.get(CUSTOMER_PRODUCTION_ENV, "").strip().lower() in _TRUTHY


# ---------------------------------------------------------------------------
# Exchange credentials (HMAC-signed, single-use via the nonce digest PK)
# ---------------------------------------------------------------------------


def admin_hmac_key(key_version: int, *, environ: Mapping[str, str] | None = None) -> bytes:
    """Resolve the versioned admin-session HMAC key from the environment.

    Version N reads ``VIDEO_REPLICA_ADMIN_SESSION_HMAC_KEY_VN``; version 1 also
    accepts the un-suffixed variable so single-version deployments stay simple.
    """
    source: Mapping[str, str] = os.environ if environ is None else environ
    candidates = [f"{ADMIN_SESSION_HMAC_KEY_ENV}_V{key_version}"]
    if key_version == 1:
        candidates.append(ADMIN_SESSION_HMAC_KEY_ENV)
    for name in candidates:
        value = source.get(name, "").strip()
        if value:
            raw = value.encode("utf-8")
            if len(raw) < MIN_HMAC_KEY_BYTES:
                raise ValueError(
                    f"{name} must be at least {MIN_HMAC_KEY_BYTES} bytes, got {len(raw)}"
                )
            return raw
    raise ExchangeCredentialError(
        f"admin session HMAC key for key version {key_version} is not configured "
        f"(expected {candidates[0]} or a later key version)"
    )


@dataclass(frozen=True)
class ExchangeCredentialPayload:
    actor_user_id: str
    expires_at: datetime
    nonce: str
    key_version: int


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _credential_signature(key: bytes, prefix: str, body: str) -> bytes:
    message = f"{prefix}.{body}".encode("ascii")
    return hmac.new(key, message, hashlib.sha256).digest()


def issue_exchange_credential(
    actor_user_id: str,
    *,
    ttl_seconds: int,
    key_version: int = 1,
    key: bytes | None = None,
    now: datetime | None = None,
) -> str:
    """Mint a short-lived, single-use exchange credential for an operator.

    The credential is self-contained (actor, expiry, nonce, key version) and
    HMAC-signed; it is handed to the operator out of band and never stored.
    """
    if ttl_seconds <= 0:
        raise ValueError("ttl_seconds must be positive")
    key_bytes = key if key is not None else admin_hmac_key(key_version)
    issued_at = now if now is not None else datetime.now(UTC)
    expires_at = issued_at + timedelta(seconds=ttl_seconds)
    nonce = secrets.token_hex(NONCE_HEX_LENGTH // 2)
    payload = json.dumps(
        {
            "actor": actor_user_id,
            "exp": int(expires_at.timestamp()),
            "nonce": nonce,
            "v": key_version,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    body = _b64encode(payload)
    signature = _b64encode(_credential_signature(key_bytes, EXCHANGE_CREDENTIAL_PREFIX, body))
    return f"{EXCHANGE_CREDENTIAL_PREFIX}.{body}.{signature}"


def parse_and_verify_exchange_credential(
    credential: str,
    *,
    now: datetime | None = None,
    key: bytes | None = None,
) -> ExchangeCredentialPayload:
    """Verify signature, expiry and shape of an exchange credential."""
    if not credential or len(credential) > MAX_EXCHANGE_CREDENTIAL_LENGTH:
        raise ExchangeCredentialError("exchange credential is malformed")
    parts = credential.split(".")
    if len(parts) != 3 or parts[0] != EXCHANGE_CREDENTIAL_PREFIX:
        raise ExchangeCredentialError("exchange credential is malformed")
    prefix, body, signature_text = parts
    try:
        loaded = json.loads(_b64decode(body))
        signature = _b64decode(signature_text)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ExchangeCredentialError("exchange credential is malformed") from exc
    # Valid non-object JSON (null/number/string/array) must reject as malformed
    # instead of raising TypeError from a dict() conversion.
    if not isinstance(loaded, dict):
        raise ExchangeCredentialError("exchange credential is malformed")
    decoded: dict[str, object] = loaded

    actor = decoded.get("actor")
    exp = decoded.get("exp")
    nonce = decoded.get("nonce")
    key_version = decoded.get("v")
    if (
        not isinstance(actor, str)
        or not actor
        or not isinstance(nonce, str)
        or len(nonce) != NONCE_HEX_LENGTH
        or any(character not in string.hexdigits for character in nonce)
        or not isinstance(key_version, int)
        or key_version < 1
        or not isinstance(exp, int)
    ):
        raise ExchangeCredentialError("exchange credential is malformed")

    key_bytes = key if key is not None else admin_hmac_key(key_version)
    expected = _credential_signature(key_bytes, prefix, body)
    if not hmac.compare_digest(signature, expected):
        raise ExchangeCredentialError("exchange credential signature mismatch")

    expires_at = datetime.fromtimestamp(exp, tz=UTC)
    current = now if now is not None else datetime.now(UTC)
    if current >= expires_at:
        raise ExchangeCredentialError("exchange credential has expired")
    return ExchangeCredentialPayload(
        actor_user_id=actor,
        expires_at=expires_at,
        nonce=nonce,
        key_version=key_version,
    )


def resolve_admin_session_ttl_seconds() -> int:
    raw = os.environ.get(ADMIN_SESSION_TTL_ENV, "").strip()
    if not raw:
        return DEFAULT_ADMIN_SESSION_TTL_SECONDS
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{ADMIN_SESSION_TTL_ENV} must be an integer number of seconds") from exc
    if not MIN_ADMIN_SESSION_TTL_SECONDS <= value <= MAX_ADMIN_SESSION_TTL_SECONDS:
        raise ValueError(
            f"{ADMIN_SESSION_TTL_ENV} must be between {MIN_ADMIN_SESSION_TTL_SECONDS} "
            f"and {MAX_ADMIN_SESSION_TTL_SECONDS} seconds, got {value}"
        )
    return value


# ---------------------------------------------------------------------------
# Session storage / verification (PostgreSQL only — customer data plane)
# ---------------------------------------------------------------------------


def _sha256_hex(value: str | bytes) -> str:
    data = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(data).hexdigest()


def _as_datetime(value: object) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return datetime.fromisoformat(str(value))


@dataclass(frozen=True)
class AdminActor:
    user_id: str
    username: str
    display_name: str
    role: str
    session_id: str
    session_expires_at: str
    last_activity_at: str


def _http(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


def create_admin_session(
    payload: ExchangeCredentialPayload, request: Request
) -> tuple[AdminActor, str, str, int]:
    """Exchange a verified credential for an admin session row + cookie values.

    Returns (actor, session_token, csrf_token, ttl_seconds). The nonce digest is
    the session id, so a replayed credential collides on the primary key and is
    rejected as reused instead of minting a second session.
    """
    ttl_seconds = resolve_admin_session_ttl_seconds()
    session_token = secrets.token_urlsafe(32)
    csrf_token = secrets.token_urlsafe(32)
    client_ip = request.client.host if request.client is not None else ""
    user_agent = request.headers.get("user-agent", "")

    with pg_transaction() as conn:
        user_row = conn.execute(
            "SELECT id, username, display_name, role FROM users WHERE id = %s AND is_active = 1",
            (payload.actor_user_id,),
        ).fetchone()
        if user_row is None:
            raise _http(401, "ADMIN_ACTOR_INVALID", "Operator is missing or inactive.")
        role = str(user_row[3])
        if role not in _ADMIN_ROLES:
            raise _http(403, "ADMIN_ROLE_REQUIRED", "Only admin/auditor operators may sign in.")
        now_row = conn.execute("SELECT now()").fetchone()
        if now_row is None:  # pragma: no cover - SELECT now() always returns a row
            raise RuntimeError("database clock unavailable")
        db_now = _as_datetime(now_row[0])
        expires_at = db_now + timedelta(seconds=ttl_seconds)
        try:
            conn.execute(
                "INSERT INTO admin_sessions "
                "(id, actor_user_id, session_digest, csrf_digest, created_at, "
                " last_activity_at, expires_at, created_ip_digest, created_ua_digest) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    _sha256_hex(payload.nonce),
                    str(user_row[0]),
                    _sha256_hex(session_token),
                    _sha256_hex(csrf_token),
                    db_now.isoformat(),
                    db_now.isoformat(),
                    expires_at.isoformat(),
                    _sha256_hex(client_ip),
                    _sha256_hex(user_agent),
                ),
            )
        except psycopg.errors.UniqueViolation as exc:
            raise _http(
                401,
                "EXCHANGE_CREDENTIAL_REUSED",
                "This exchange credential has already been used.",
            ) from exc
        actor = AdminActor(
            user_id=str(user_row[0]),
            username=str(user_row[1]),
            display_name=str(user_row[2]),
            role=role,
            session_id=_sha256_hex(payload.nonce),
            session_expires_at=expires_at.isoformat(),
            last_activity_at=db_now.isoformat(),
        )
    return actor, session_token, csrf_token, ttl_seconds


def load_admin_session(session_token: str) -> tuple[AdminActor, str]:
    """Verify an admin session cookie and refresh its activity timestamp.

    PostgreSQL time is the only clock: the stored ISO expiry is compared against
    ``now()`` fetched in the same transaction. Actor, revocation, expiry and
    role are re-checked on every request, so disabling a user or revoking a
    session invalidates it immediately.
    """
    with pg_transaction() as conn:
        row = conn.execute(
            "SELECT s.id, s.csrf_digest, s.expires_at, s.last_activity_at, "
            "       s.actor_user_id, u.username, u.display_name, u.role, now() AS db_now "
            "FROM admin_sessions s JOIN users u ON u.id = s.actor_user_id "
            "WHERE s.session_digest = %s AND s.revoked_at IS NULL AND u.is_active = 1",
            (_sha256_hex(session_token),),
        ).fetchone()
        if row is None:
            raise _http(
                401, "ADMIN_SESSION_INVALID", "Admin session is missing, revoked or invalid."
            )
        db_now = _as_datetime(row[8])
        expires_at = _as_datetime(row[2])
        if db_now >= expires_at:
            raise _http(401, "ADMIN_SESSION_EXPIRED", "Admin session has expired.")
        role = str(row[7])
        if role not in _ADMIN_ROLES:
            raise _http(
                401, "ADMIN_SESSION_INVALID", "Operator role no longer permits admin access."
            )
        conn.execute(
            "UPDATE admin_sessions SET last_activity_at = %s WHERE id = %s",
            (db_now.isoformat(), str(row[0])),
        )
        actor = AdminActor(
            user_id=str(row[4]),
            username=str(row[5]),
            display_name=str(row[6]),
            role=role,
            session_id=str(row[0]),
            session_expires_at=expires_at.isoformat(),
            last_activity_at=db_now.isoformat(),
        )
    return actor, str(row[1])


def revoke_admin_session(session_id: str) -> None:
    with pg_transaction() as conn:
        now_row = conn.execute("SELECT now()").fetchone()
        if now_row is None:  # pragma: no cover - SELECT now() always returns a row
            raise RuntimeError("database clock unavailable")
        db_now = _as_datetime(now_row[0])
        conn.execute(
            "UPDATE admin_sessions SET revoked_at = %s WHERE id = %s AND revoked_at IS NULL",
            (db_now.isoformat(), session_id),
        )


# ---------------------------------------------------------------------------
# FastAPI dependencies
# ---------------------------------------------------------------------------


def get_admin_actor(request: Request) -> AdminActor:
    """Authenticate an operator via the admin cookie; enforce CSRF on writes."""
    token = request.cookies.get(ADMIN_SESSION_COOKIE, "")
    if not token:
        raise _http(401, "ADMIN_SESSION_INVALID", "An admin session cookie is required.")
    try:
        actor, csrf_digest = load_admin_session(token)
    except RuntimeError as exc:
        # The PG runtime is unavailable (internal SQLite deployments): fail
        # closed with 503 instead of falling back to any legacy identity.
        raise _http(
            503,
            "ADMIN_SESSIONS_UNAVAILABLE",
            "Admin sessions require the PostgreSQL runtime.",
        ) from exc
    if request.method.upper() in _WRITE_METHODS:
        supplied = request.headers.get(ADMIN_CSRF_HEADER, "")
        if not supplied:
            raise _http(403, "ADMIN_CSRF_REQUIRED", "The CSRF header is required.")
        if not hmac.compare_digest(_sha256_hex(supplied), csrf_digest):
            raise _http(403, "ADMIN_CSRF_INVALID", "The CSRF token does not match.")
    return actor


def get_admin_writer(actor: Annotated[AdminActor, Depends(get_admin_actor)]) -> AdminActor:
    """RBAC: auditors are strictly read-only."""
    if actor.role != "admin":
        raise _http(403, "AUDITOR_READ_ONLY", "Auditors may read but not modify control data.")
    return actor


AdminReader = Annotated[AdminActor, Depends(get_admin_actor)]
AdminWriter = Annotated[AdminActor, Depends(get_admin_writer)]


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


class ExchangeRequest(BaseModel):
    credential: str


class AdminActorInfo(BaseModel):
    user_id: str
    username: str
    display_name: str
    role: str


class ExchangeResponse(BaseModel):
    session_id: str
    expires_at: str
    csrf_token: str
    actor: AdminActorInfo


class AdminSessionInfo(BaseModel):
    session_id: str
    expires_at: str
    last_activity_at: str
    actor: AdminActorInfo


router = APIRouter(prefix="/api/control/admin", tags=["admin-auth"])


@router.post("/session/exchange", response_model=ExchangeResponse, status_code=201)
def exchange_admin_session(
    body: ExchangeRequest, request: Request, response: Response
) -> ExchangeResponse:
    try:
        payload = parse_and_verify_exchange_credential(body.credential)
    except ExchangeCredentialError as exc:
        logger.info("admin exchange credential rejected: %s", type(exc).__name__)
        raise _http(
            401, "EXCHANGE_CREDENTIAL_INVALID", "Exchange credential is invalid or expired."
        ) from exc
    try:
        actor, session_token, csrf_token, ttl_seconds = create_admin_session(payload, request)
    except RuntimeError as exc:
        raise _http(
            503,
            "ADMIN_SESSIONS_UNAVAILABLE",
            "Admin sessions require the PostgreSQL runtime.",
        ) from exc
    response.set_cookie(
        ADMIN_SESSION_COOKIE,
        session_token,
        max_age=ttl_seconds,
        httponly=True,
        samesite="strict",
        path=ADMIN_COOKIE_PATH,
        secure=is_customer_production(),
    )
    logger.info("admin session exchanged: actor=%s session=%s", actor.user_id, actor.session_id)
    return ExchangeResponse(
        session_id=actor.session_id,
        expires_at=actor.session_expires_at,
        csrf_token=csrf_token,
        actor=AdminActorInfo(
            user_id=actor.user_id,
            username=actor.username,
            display_name=actor.display_name,
            role=actor.role,
        ),
    )


@router.get("/session", response_model=AdminSessionInfo)
def get_current_admin_session(actor: AdminReader) -> AdminSessionInfo:
    return AdminSessionInfo(
        session_id=actor.session_id,
        expires_at=actor.session_expires_at,
        last_activity_at=actor.last_activity_at,
        actor=AdminActorInfo(
            user_id=actor.user_id,
            username=actor.username,
            display_name=actor.display_name,
            role=actor.role,
        ),
    )


@router.delete("/session", status_code=204)
def logout_admin_session(actor: AdminReader, response: Response) -> None:
    try:
        revoke_admin_session(actor.session_id)
    except RuntimeError as exc:
        raise _http(
            503,
            "ADMIN_SESSIONS_UNAVAILABLE",
            "Admin sessions require the PostgreSQL runtime.",
        ) from exc
    response.delete_cookie(ADMIN_SESSION_COOKIE, path=ADMIN_COOKIE_PATH)
    logger.info("admin session revoked: actor=%s session=%s", actor.user_id, actor.session_id)
