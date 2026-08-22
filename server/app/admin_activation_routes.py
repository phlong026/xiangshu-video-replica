"""T12 / ACT-04 — admin activation-code management API.

Application layer on top of the T10 catalog (revision 027) and the T11
service primitives: batch creation, generation + one-time AEAD export
creation, the audited plaintext download, delivery, suspension, resume,
revocation and listing — every write behind the T09 admin session / CSRF /
RBAC gate plus the admin write contract (dev doc §15: real actor, reason,
confirmation, Idempotency-Key, request id).

Write idempotency (dev doc §6.3 / §11.3, table from revision 028): each
business write runs inside one PostgreSQL transaction that first inserts an
``admin_write_idempotency`` placeholder keyed by (actor, canonical route,
key digest) with ``INSERT ... ON CONFLICT DO NOTHING``. The winner back-fills
the response snapshot before commit; a same-key retry replays the stored
response (``X-Idempotent-Replay: true``), and the same key against a
different canonical request is rejected with ``IDEMPOTENCY_CONFLICT``. A
business failure rolls the placeholder back with the transaction, so the
key stays reusable. Concurrent same-key writers serialize on the unique
index insert (PostgreSQL waits on the conflicting transaction).

The one-time download deliberately stays *outside* the snapshot layer: its
response carries the plaintext codes, which must never persist anywhere
(No-Go red line), and the ``downloaded_at`` one-shot constraint already
makes a second download impossible.

No-Go red lines: plaintext activation codes live only in the one-time
download response handed to the operator (T11 principle) — never in a
column, event, idempotency snapshot or log record. Generation responses
carry masked codes only; the export ciphertext stays sealed until the
single audited download.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

import psycopg
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from app.activation_code_service import (
    ActivationCodeError,
    ActivationExportError,
    ActivationKeyError,
    InvalidCodeTransitionError,
    activation_code_hmac_key,
    assert_code_transition,
    configured_export_aead_keys,
    create_batch_export,
    export_aead_key,
    fetch_export_package,
    generate_batch_codes,
    highest_code_hmac_key_version,
    highest_export_aead_key_version,
)
from app.admin_auth_routes import AdminActor, AdminReader, AdminWriter
from app.db_pg import pg_transaction

logger = logging.getLogger(__name__)

IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"
REQUEST_ID_HEADER = "X-Request-Id"
REPLAY_HEADER = "X-Idempotent-Replay"

DEFAULT_EXPORT_TTL_SECONDS = 15 * 60
DEFAULT_LIST_LIMIT = 100
MAX_LIST_LIMIT = 200

router = APIRouter(prefix="/api/control", tags=["admin-activation"])


def _http(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


# ---------------------------------------------------------------------------
# The admin write contract (dev doc §15)
# ---------------------------------------------------------------------------


class AdminWriteContract(BaseModel):
    """Shared write-contract fields for every admin activation mutation."""

    confirm: bool = False
    reason: str = ""


def _require_write_contract(request: Request, body: AdminWriteContract) -> tuple[str, str]:
    """Validate the write contract; returns (idempotency_key, reason)."""
    key = request.headers.get(IDEMPOTENCY_KEY_HEADER, "").strip()
    if not key:
        raise _http(400, "IDEMPOTENCY_KEY_REQUIRED", "An Idempotency-Key header is required.")
    if not body.confirm:
        raise _http(400, "CONFIRMATION_REQUIRED", "This write requires confirm=true.")
    reason = body.reason.strip()
    if not reason:
        raise _http(400, "REASON_REQUIRED", "A non-blank reason is required.")
    return key, reason


def _canonical_route(request: Request) -> str:
    """``METHOD /route/template`` — the route, never the concrete path."""
    route = request.scope.get("route")
    template = getattr(route, "path", request.url.path)
    return f"{request.method.upper()} {template}"


def _request_hash(route: str, path_params: Mapping[str, str], body: BaseModel) -> str:
    """Freeze the canonical request for conflict checks.

    The route *template* alone does not identify the target resource: the same
    key with the same body against ``/activation-codes/{code_id}/revoke`` for
    code A and code B would otherwise hash identically, so the second call
    would wrongly replay the first response while B stays untouched (PR #43
    review P2). The concrete path parameters are therefore part of the
    fingerprint.
    """
    payload = json.dumps(
        {
            "route": route,
            "path_params": {name: path_params[name] for name in sorted(path_params)},
            "body": body.model_dump(),
        },
        separators=(",", ":"),
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Idempotency snapshot layer (revision 028)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _IdempotencySnapshot:
    request_hash: str
    response_status: int | None
    response_body: str | None


def _idempotency_key_digest(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _begin_idempotent_write(
    conn: psycopg.Connection,
    *,
    actor_user_id: str,
    route: str,
    idempotency_key: str,
    request_hash: str,
) -> str | None:
    """Insert the placeholder row; returns its id, or ``None`` on key reuse."""
    row_id = str(uuid.uuid4())
    inserted = conn.execute(
        "INSERT INTO admin_write_idempotency "
        "(id, actor_user_id, route, idempotency_key_digest, request_hash) "
        "VALUES (%s, %s, %s, %s, %s) "
        "ON CONFLICT (actor_user_id, route, idempotency_key_digest) DO NOTHING",
        (
            row_id,
            actor_user_id,
            route,
            _idempotency_key_digest(idempotency_key),
            request_hash,
        ),
    ).rowcount
    return row_id if inserted == 1 else None


def _load_idempotent_snapshot(
    conn: psycopg.Connection,
    *,
    actor_user_id: str,
    route: str,
    idempotency_key: str,
) -> _IdempotencySnapshot | None:
    row = conn.execute(
        "SELECT request_hash, response_status, response_body "
        "FROM admin_write_idempotency "
        "WHERE actor_user_id = %s AND route = %s AND idempotency_key_digest = %s",
        (actor_user_id, route, _idempotency_key_digest(idempotency_key)),
    ).fetchone()
    if row is None:
        return None
    return _IdempotencySnapshot(
        request_hash=str(row[0]),
        response_status=row[1] if row[1] is None else int(row[1]),
        response_body=None if row[2] is None else str(row[2]),
    )


def _finish_idempotent_write(
    conn: psycopg.Connection,
    placeholder_id: str,
    *,
    response_status: int,
    response_body: dict[str, object],
) -> None:
    conn.execute(
        "UPDATE admin_write_idempotency SET response_status = %s, response_body = %s WHERE id = %s",
        (
            response_status,
            json.dumps(response_body, ensure_ascii=False, separators=(",", ":")),
            placeholder_id,
        ),
    )


def _write_with_idempotency(
    request: Request,
    response: Response,
    actor: AdminActor,
    body: AdminWriteContract,
    business: Callable[[psycopg.Connection, str], dict[str, object]],
    *,
    success_status: int,
) -> dict[str, object]:
    """Run one admin write behind the idempotency snapshot layer.

    ``business`` receives the transaction connection and the request id and
    returns the response payload; the payload is snapshotted before commit.
    Callers keep plaintext codes out of it (No-Go red line) — the download
    path bypasses this layer precisely because its response must not persist.
    """
    idempotency_key, _reason = _require_write_contract(request, body)
    route = _canonical_route(request)
    request_hash = _request_hash(route, dict(request.path_params), body)
    request_id = str(uuid.uuid4())
    try:
        with pg_transaction() as conn:
            placeholder = _begin_idempotent_write(
                conn,
                actor_user_id=actor.user_id,
                route=route,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
            )
            if placeholder is None:
                snapshot = _load_idempotent_snapshot(
                    conn,
                    actor_user_id=actor.user_id,
                    route=route,
                    idempotency_key=idempotency_key,
                )
                if (
                    snapshot is None
                    or snapshot.request_hash != request_hash
                    or snapshot.response_status is None
                    or snapshot.response_body is None
                ):
                    raise _http(
                        409,
                        "IDEMPOTENCY_CONFLICT",
                        "This idempotency key was already used for a different request.",
                    )
                replayed: dict[str, object] = json.loads(snapshot.response_body)
                response.status_code = snapshot.response_status
                response.headers[REPLAY_HEADER] = "true"
                replay_request_id = replayed.get("request_id")
                if isinstance(replay_request_id, str):
                    response.headers[REQUEST_ID_HEADER] = replay_request_id
                return replayed
            payload = business(conn, request_id)
            _finish_idempotent_write(
                conn,
                placeholder,
                response_status=success_status,
                response_body=payload,
            )
            response.headers[REQUEST_ID_HEADER] = request_id
            return payload
    except RuntimeError as exc:
        # The PG runtime is unavailable (internal SQLite deployments): fail
        # closed instead of falling back to any legacy control identity.
        raise _http(
            503,
            "ACTIVATION_SERVICE_UNAVAILABLE",
            "Activation code management requires the PostgreSQL runtime.",
        ) from exc


# ---------------------------------------------------------------------------
# Batch creation
# ---------------------------------------------------------------------------


class BatchCreateRequest(AdminWriteContract):
    name: str
    face_value_fen: int
    credits: int
    quantity: int
    activation_expires_at: str


def _validate_batch_payload(body: BatchCreateRequest) -> None:
    problems: list[str] = []
    if not body.name.strip():
        problems.append("name must not be blank")
    if body.face_value_fen <= 0:
        problems.append("face_value_fen must be positive")
    if body.credits <= 0:
        problems.append("credits must be positive")
    if body.quantity <= 0:
        problems.append("quantity must be positive")
    try:
        expires_at = datetime.fromisoformat(body.activation_expires_at)
    except ValueError:
        problems.append("activation_expires_at must be an ISO-8601 timestamp")
    else:
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        if expires_at <= datetime.now(UTC):
            problems.append("activation_expires_at must be in the future")
    if problems:
        raise _http(400, "BATCH_VALIDATION_FAILED", "; ".join(problems))


@router.post("/activation-code-batches", status_code=201)
def create_activation_code_batch(
    body: BatchCreateRequest,
    request: Request,
    response: Response,
    actor: AdminWriter,
) -> dict[str, object]:
    """Create an OPEN batch with frozen commercial snapshots."""

    def business(conn: psycopg.Connection, request_id: str) -> dict[str, object]:
        _validate_batch_payload(body)
        batch_id = str(uuid.uuid4())
        name = body.name.strip()
        conn.execute(
            "INSERT INTO activation_code_batches "
            "(id, name, face_value_fen, unit_price_fen_snapshot, credits_snapshot, "
            " quantity, activation_expires_at, status, created_by_user_id) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, 'OPEN', %s)",
            (
                batch_id,
                name,
                body.face_value_fen,
                # The unit price is the face value at creation time — the
                # frozen snapshot later price changes can never rewrite.
                body.face_value_fen,
                body.credits,
                body.quantity,
                body.activation_expires_at,
                actor.user_id,
            ),
        )
        logger.info(
            "activation code batch created: batch=%s quantity=%d actor=%s request=%s",
            batch_id,
            body.quantity,
            actor.user_id,
            request_id,
        )
        return {
            "batch_id": batch_id,
            "name": name,
            "face_value_fen": body.face_value_fen,
            "unit_price_fen_snapshot": body.face_value_fen,
            "credits_snapshot": body.credits,
            "quantity": body.quantity,
            "activation_expires_at": body.activation_expires_at,
            "status": "OPEN",
            "created_by_user_id": actor.user_id,
            "request_id": request_id,
        }

    return _write_with_idempotency(request, response, actor, body, business, success_status=201)


# ---------------------------------------------------------------------------
# Generation + one-time AEAD export creation
# ---------------------------------------------------------------------------


class GenerateRequest(AdminWriteContract):
    quantity: int


def _resolve_generation_keys() -> tuple[int, bytes, int, bytes]:
    """Resolve the highest configured HMAC / AEAD keys for new material."""
    hmac_version = highest_code_hmac_key_version()
    hmac_key = activation_code_hmac_key(hmac_version)
    aead_version = highest_export_aead_key_version()
    aead_key = export_aead_key(aead_version)
    return hmac_version, hmac_key, aead_version, aead_key


@router.post("/activation-code-batches/{batch_id}/generate", status_code=201)
def generate_activation_codes(
    batch_id: str,
    body: GenerateRequest,
    request: Request,
    response: Response,
    actor: AdminWriter,
) -> dict[str, object]:
    """Mint codes for an OPEN batch and seal them into one export package.

    Generation and export sealing share one transaction: the plaintext exists
    only inside that transaction's memory and lands in exactly two places —
    the AEAD envelope column (sealed) and the one-time download response. The
    API response itself carries masked codes only.
    """
    try:
        hmac_version, hmac_key, aead_version, aead_key = _resolve_generation_keys()
    except ActivationKeyError as exc:
        logger.warning("activation keys unavailable for generation: %s", type(exc).__name__)
        raise _http(
            503,
            "ACTIVATION_KEYS_UNAVAILABLE",
            "Activation code keys are not configured; generation is refused.",
        ) from exc

    def business(conn: psycopg.Connection, request_id: str) -> dict[str, object]:
        if body.quantity < 1:
            raise _http(400, "BATCH_VALIDATION_FAILED", "quantity must be at least 1")
        batch_row = conn.execute(
            "SELECT status FROM activation_code_batches WHERE id = %s FOR UPDATE",
            (batch_id,),
        ).fetchone()
        if batch_row is None:
            raise _http(404, "BATCH_NOT_FOUND", "Unknown activation code batch.")
        if str(batch_row[0]) != "OPEN":
            raise _http(409, "BATCH_NOT_OPEN", "The batch is closed; codes cannot be minted.")
        try:
            generated = generate_batch_codes(
                conn,
                batch_id,
                quantity=body.quantity,
                key_version=hmac_version,
                hmac_key=hmac_key,
                actor_user_id=actor.user_id,
                request_id=request_id,
            )
        except ActivationCodeError as exc:
            raise _http(
                409,
                "BATCH_BUDGET_EXCEEDED",
                "Generating this quantity would exceed the frozen batch budget.",
            ) from exc
        export_id = create_batch_export(
            conn,
            batch_id,
            generated,
            requested_by_user_id=actor.user_id,
            ttl_seconds=DEFAULT_EXPORT_TTL_SECONDS,
            key_version=aead_version,
            aead_key=aead_key,
        )
        expires_row = conn.execute(
            "SELECT expires_at FROM activation_code_exports WHERE id = %s",
            (export_id,),
        ).fetchone()
        logger.info(
            "activation codes generated: batch=%s count=%d export=%s actor=%s request=%s",
            batch_id,
            len(generated),
            export_id,
            actor.user_id,
            request_id,
        )
        return {
            "batch_id": batch_id,
            "export_id": export_id,
            "expires_at": str(expires_row[0]) if expires_row is not None else "",
            "codes": [
                {"code_id": code.code_id, "masked_code": code.masked_code} for code in generated
            ],
            "request_id": request_id,
        }

    return _write_with_idempotency(request, response, actor, body, business, success_status=201)


# ---------------------------------------------------------------------------
# One-time audited export download
# ---------------------------------------------------------------------------


class DownloadRequest(AdminWriteContract):
    pass


@router.post("/activation-code-exports/{export_id}/download")
def download_activation_code_export(
    export_id: str,
    body: DownloadRequest,
    request: Request,
    response: Response,
    actor: AdminWriter,
) -> dict[str, object]:
    """The single plaintext delivery path for an export package.

    Deliberately outside the idempotency snapshot layer: the response carries
    plaintext codes that must never persist (No-Go), and the one-time
    ``downloaded_at`` constraint already refuses any second download. The
    write contract (key / confirm / reason) is still enforced, and the reason
    + request id land in the durable export audit columns (PR #43 review P1).
    """
    _key, reason = _require_write_contract(request, body)
    request_id = str(uuid.uuid4())
    try:
        aead_keys = configured_export_aead_keys()
        with pg_transaction() as conn:
            try:
                codes = fetch_export_package(
                    conn,
                    export_id,
                    downloaded_by_user_id=actor.user_id,
                    aead_keys=aead_keys,
                    download_reason=reason,
                    download_request_id=request_id,
                )
            except ActivationExportError as exc:
                message = str(exc)
                if "unknown" in message:
                    raise _http(404, "EXPORT_NOT_FOUND", "Unknown export package.") from exc
                if "already downloaded" in message:
                    raise _http(
                        409,
                        "EXPORT_ALREADY_DOWNLOADED",
                        "This export package was already downloaded exactly once.",
                    ) from exc
                if "expired" in message:
                    raise _http(
                        409,
                        "EXPORT_EXPIRED",
                        "This export package has expired; generate a new one.",
                    ) from exc
                raise _http(
                    503,
                    "ACTIVATION_KEYS_UNAVAILABLE",
                    "The export key version is not configured; download is refused.",
                ) from exc
            audit_row = conn.execute(
                "SELECT batch_id, downloaded_at FROM activation_code_exports WHERE id = %s",
                (export_id,),
            ).fetchone()
    except RuntimeError as exc:
        raise _http(
            503,
            "ACTIVATION_SERVICE_UNAVAILABLE",
            "Activation code management requires the PostgreSQL runtime.",
        ) from exc
    payload: dict[str, object] = {
        "export_id": export_id,
        "batch_id": str(audit_row[0]) if audit_row is not None else "",
        "codes": codes,
        "downloaded_at": str(audit_row[1]) if audit_row is not None else "",
        "request_id": request_id,
    }
    response.headers[REQUEST_ID_HEADER] = request_id
    # Plaintext codes never reach the logs — only counts and identifiers.
    logger.info(
        "activation export downloaded: export=%s actor=%s codes=%d request=%s reason=%s",
        export_id,
        actor.user_id,
        len(codes),
        request_id,
        reason,
    )
    return payload


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------


class DeliverRequest(AdminWriteContract):
    channel: str
    external_order_ref: str | None = None
    recipient_ref: str | None = None


@router.post("/activation-codes/{code_id}/deliver", status_code=201)
def deliver_activation_code(
    code_id: str,
    body: DeliverRequest,
    request: Request,
    response: Response,
    actor: AdminWriter,
) -> dict[str, object]:
    """Record a channel delivery and flip the code from GENERATED to ISSUED."""

    def business(conn: psycopg.Connection, request_id: str) -> dict[str, object]:
        channel = body.channel.strip()
        if not channel:
            raise _http(400, "DELIVERY_VALIDATION_FAILED", "channel must not be blank")
        code_row = conn.execute(
            "SELECT status FROM activation_codes WHERE id = %s FOR UPDATE",
            (code_id,),
        ).fetchone()
        if code_row is None:
            raise _http(404, "CODE_NOT_FOUND", "Unknown activation code.")
        try:
            assert_code_transition(str(code_row[0]), "ISSUED")
        except InvalidCodeTransitionError as exc:
            raise _http(
                409,
                "CODE_TRANSITION_INVALID",
                "Only a GENERATED code can be delivered.",
            ) from exc
        now = _now_iso()
        delivery_id = str(uuid.uuid4())
        reason = body.reason.strip()
        conn.execute(
            "INSERT INTO activation_code_deliveries "
            "(id, code_id, channel, external_order_ref, recipient_ref, "
            " delivered_by_user_id, note) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (
                delivery_id,
                code_id,
                channel,
                body.external_order_ref,
                body.recipient_ref,
                actor.user_id,
                reason,
            ),
        )
        conn.execute(
            "UPDATE activation_codes SET status = 'ISSUED', issued_at = %s WHERE id = %s",
            (now, code_id),
        )
        conn.execute(
            "INSERT INTO activation_code_events "
            "(id, code_id, event, actor_user_id, reason, request_id) "
            "VALUES (%s, %s, 'DELIVERED', %s, %s, %s)",
            (str(uuid.uuid4()), code_id, actor.user_id, reason, request_id),
        )
        logger.info(
            "activation code delivered: code=%s channel=%s actor=%s request=%s",
            code_id,
            channel,
            actor.user_id,
            request_id,
        )
        return {
            "code_id": code_id,
            "status": "ISSUED",
            "delivery_id": delivery_id,
            "request_id": request_id,
        }

    return _write_with_idempotency(request, response, actor, body, business, success_status=201)


# ---------------------------------------------------------------------------
# Suspension, resume and revocation
# ---------------------------------------------------------------------------


def _locked_code_status(
    conn: psycopg.Connection, code_id: str, columns: str
) -> tuple[object, ...] | None:
    return conn.execute(
        f"SELECT {columns} FROM activation_codes WHERE id = %s FOR UPDATE",  # noqa: S608
        (code_id,),
    ).fetchone()


def _record_code_event(
    conn: psycopg.Connection,
    *,
    code_id: str,
    event: str,
    actor_user_id: str,
    reason: str,
    request_id: str,
) -> None:
    conn.execute(
        "INSERT INTO activation_code_events "
        "(id, code_id, event, actor_user_id, reason, request_id) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (str(uuid.uuid4()), code_id, event, actor_user_id, reason, request_id),
    )


def _transition_error(current: str, target: str) -> HTTPException:
    return _http(
        409,
        "CODE_TRANSITION_INVALID",
        f"The activation code cannot move from {current} to {target}.",
    )


@router.post("/activation-codes/{code_id}/suspend")
def suspend_activation_code(
    code_id: str,
    body: AdminWriteContract,
    request: Request,
    response: Response,
    actor: AdminWriter,
) -> dict[str, object]:
    """Suspend a delivered code (operator side state, frozen matrix)."""

    def business(conn: psycopg.Connection, request_id: str) -> dict[str, object]:
        code_row = _locked_code_status(conn, code_id, "status")
        if code_row is None:
            raise _http(404, "CODE_NOT_FOUND", "Unknown activation code.")
        current = str(code_row[0])
        try:
            assert_code_transition(current, "SUSPENDED")
        except InvalidCodeTransitionError as exc:
            raise _transition_error(current, "SUSPENDED") from exc
        reason = body.reason.strip()
        conn.execute(
            "UPDATE activation_codes SET status = 'SUSPENDED', suspended_at = %s WHERE id = %s",
            (_now_iso(), code_id),
        )
        _record_code_event(
            conn,
            code_id=code_id,
            event="SUSPENDED",
            actor_user_id=actor.user_id,
            reason=reason,
            request_id=request_id,
        )
        logger.info(
            "activation code suspended: code=%s actor=%s request=%s",
            code_id,
            actor.user_id,
            request_id,
        )
        return {"code_id": code_id, "status": "SUSPENDED", "request_id": request_id}

    return _write_with_idempotency(request, response, actor, body, business, success_status=200)


@router.post("/activation-codes/{code_id}/resume")
def resume_activation_code(
    code_id: str,
    body: AdminWriteContract,
    request: Request,
    response: Response,
    actor: AdminWriter,
) -> dict[str, object]:
    """Resume a suspended, activated code (the matrix has no back-to-ISSUED edge)."""

    def business(conn: psycopg.Connection, request_id: str) -> dict[str, object]:
        code_row = _locked_code_status(conn, code_id, "status, bound_user_id")
        if code_row is None:
            raise _http(404, "CODE_NOT_FOUND", "Unknown activation code.")
        current = str(code_row[0])
        try:
            assert_code_transition(current, "ACTIVE")
        except InvalidCodeTransitionError as exc:
            raise _transition_error(current, "ACTIVE") from exc
        if code_row[1] is None:
            # A never-activated suspended code cannot resume: the frozen
            # matrix has no SUSPENDED -> ISSUED edge, so only a bound code
            # may come back to ACTIVE.
            raise _http(
                409,
                "CODE_NOT_ACTIVATED",
                "Only an activated code can be resumed.",
            )
        reason = body.reason.strip()
        conn.execute(
            "UPDATE activation_codes SET status = 'ACTIVE', suspended_at = NULL WHERE id = %s",
            (code_id,),
        )
        _record_code_event(
            conn,
            code_id=code_id,
            event="RESUMED",
            actor_user_id=actor.user_id,
            reason=reason,
            request_id=request_id,
        )
        logger.info(
            "activation code resumed: code=%s actor=%s request=%s",
            code_id,
            actor.user_id,
            request_id,
        )
        return {"code_id": code_id, "status": "ACTIVE", "request_id": request_id}

    return _write_with_idempotency(request, response, actor, body, business, success_status=200)


@router.post("/activation-codes/{code_id}/revoke")
def revoke_activation_code(
    code_id: str,
    body: AdminWriteContract,
    request: Request,
    response: Response,
    actor: AdminWriter,
) -> dict[str, object]:
    """Revoke a code permanently (terminal state, binding kept for audit)."""

    def business(conn: psycopg.Connection, request_id: str) -> dict[str, object]:
        code_row = _locked_code_status(conn, code_id, "status")
        if code_row is None:
            raise _http(404, "CODE_NOT_FOUND", "Unknown activation code.")
        current = str(code_row[0])
        try:
            assert_code_transition(current, "REVOKED")
        except InvalidCodeTransitionError as exc:
            raise _transition_error(current, "REVOKED") from exc
        reason = body.reason.strip()
        conn.execute(
            "UPDATE activation_codes SET status = 'REVOKED', revoked_at = %s WHERE id = %s",
            (_now_iso(), code_id),
        )
        _record_code_event(
            conn,
            code_id=code_id,
            event="REVOKED",
            actor_user_id=actor.user_id,
            reason=reason,
            request_id=request_id,
        )
        logger.info(
            "activation code revoked: code=%s actor=%s request=%s",
            code_id,
            actor.user_id,
            request_id,
        )
        return {"code_id": code_id, "status": "REVOKED", "request_id": request_id}

    return _write_with_idempotency(request, response, actor, body, business, success_status=200)


# ---------------------------------------------------------------------------
# Listing (read path)
# ---------------------------------------------------------------------------


@router.get("/activation-codes")
def list_activation_codes(
    actor: AdminReader,
    batch_id: str | None = None,
    status: str | None = None,
    limit: int = DEFAULT_LIST_LIMIT,
    offset: int = 0,
) -> dict[str, object]:
    """List codes with masked display forms — digests never leave the store."""
    bounded_limit = max(0, min(limit, MAX_LIST_LIMIT))
    bounded_offset = max(0, offset)
    clauses: list[str] = []
    params: list[object] = []
    if batch_id:
        clauses.append("batch_id = %s")
        params.append(batch_id)
    if status:
        clauses.append("status = %s")
        params.append(status)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    try:
        with pg_transaction() as conn:
            rows = conn.execute(
                f"SELECT id, batch_id, masked_code, status, bound_user_id, issued_at "
                f"FROM activation_codes {where} "
                f"ORDER BY id LIMIT %s OFFSET %s",
                (*params, bounded_limit, bounded_offset),
            ).fetchall()
    except RuntimeError as exc:
        raise _http(
            503,
            "ACTIVATION_SERVICE_UNAVAILABLE",
            "Activation code management requires the PostgreSQL runtime.",
        ) from exc
    items = [
        {
            "code_id": str(row[0]),
            "batch_id": str(row[1]),
            "masked_code": str(row[2]),
            "status": str(row[3]),
            "bound_user_id": row[4],
            "issued_at": row[5],
        }
        for row in rows
    ]
    return {"items": items, "limit": bounded_limit, "offset": bounded_offset}
