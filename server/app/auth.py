from __future__ import annotations

import hashlib
import os
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal, cast

from fastapi import Depends, Header, HTTPException

from app.db import connect_database
from app.db_pg import DATABASE_URL_ENV, pg_transaction
from app.db_portable import BusinessConnection

Role = Literal["employee", "admin", "auditor", "customer"]
VALID_ROLES: set[str] = {"employee", "admin", "auditor", "customer"}
DESKTOP_USER_ID_ENV = "VIDEO_REPLICA_DESKTOP_USER_ID"
ALLOW_DEV_IDENTITY_HEADER_ENV = "VIDEO_REPLICA_ALLOW_DEV_IDENTITY_HEADER"
AUTH_MODE_ENV = "VIDEO_REPLICA_AUTH_MODE"
LEGACY_AUTH_MODES = {"desktop", "development"}
# Same flag the rest of the control plane reads (control_auth / db_pg each
# carry their own copy; auth.py follows that precedent to stay import-light).
CUSTOMER_PRODUCTION_ENV = "VIDEO_REPLICA_CUSTOMER_PRODUCTION"
_TRUTHY = {"1", "true", "yes", "on"}


def _customer_production_lane() -> bool:
    return os.environ.get(CUSTOMER_PRODUCTION_ENV, "").strip().lower() in _TRUTHY


@dataclass(frozen=True)
class CurrentUser:
    id: str
    username: str
    display_name: str
    role: Role


def get_database() -> Iterator[BusinessConnection]:
    """One request-scoped business connection.

    Customer production sets only ``VIDEO_REPLICA_DATABASE_URL``: the
    connection is a pooled PostgreSQL one owned by an explicit transaction
    (payment callbacks ride this path — they have no customer session, so
    the fenced writer does not apply; the pool's ``conn.transaction()``
    commits on success and rolls back on error). The SQLite path serves the
    internal/desktop lane.
    """
    if os.environ.get(DATABASE_URL_ENV, "").strip():
        from app.permissions import AuditedSecurityDenial, persist_security_denial

        try:
            with pg_transaction() as pg_conn:
                yield BusinessConnection.postgres(pg_conn)
        except AuditedSecurityDenial as exc:
            # pg_transaction has rolled the denied business request back.
            persist_security_denial(exc)
            raise
        return

    db_path = os.environ.get("VIDEO_REPLICA_DB_PATH")
    if not db_path:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "DATABASE_NOT_CONFIGURED",
                "message": "VIDEO_REPLICA_DB_PATH is required for API requests.",
            },
        )

    conn = BusinessConnection.sqlite(connect_database(Path(db_path)))
    try:
        yield conn
    finally:
        conn.close()


Database = Annotated[BusinessConnection, Depends(get_database)]


def get_current_user(
    conn: Database,
    dev_user_id: Annotated[str | None, Header(alias="X-Dev-User-Id")] = None,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> CurrentUser:
    return authenticate_request(
        conn,
        authorization=authorization,
        dev_user_id=dev_user_id,
    )


def authenticate_request(
    conn: BusinessConnection,
    *,
    authorization: str | None,
    dev_user_id: str | None,
) -> CurrentUser:
    # PostgreSQL is the customer-capable lane.  A customer desktop presents
    # the same Bearer session token for the shared read routes that it uses for
    # fenced writes; accepting only ``internal_access_tokens`` here made
    # activation succeed while every GET in the workspace failed.  Preserve
    # the internal-token path first, then reuse the existing session verifier
    # for customer tokens inside this request's read transaction.
    #
    # A1（2026-09-02 admin-console assessment）: on the customer-production
    # lane business routes must NOT accept internal access tokens.  Otherwise
    # any single internal Bearer would walk straight past the admin-session
    # + CSRF + auditor-read-only gates that guard ``/api/control/*`` and read
    # every project, wallet and audit row.  Internal operators keep their own
    # channels (control plane via admin session, CLI via its own endpoints);
    # non-production PG lanes (internal tooling on VIDEO_REPLICA_DATABASE_URL
    # without the customer flag) keep the internal-token path unchanged.
    if authorization is not None and conn.is_postgres:
        token = parse_bearer_token(authorization)
        if not _customer_production_lane():
            internal_user_id = internal_access_token_user_id(conn, token)
            if internal_user_id is not None:
                return authenticate_user(conn, internal_user_id)
        return authenticate_customer_read_session(conn, token)
    if internal_auth_required():
        if authorization is not None:
            return authenticate_access_token(conn, parse_bearer_token(authorization))
        raise HTTPException(
            status_code=401,
            detail={
                "code": "AUTH_TOKEN_REQUIRED",
                "message": "A valid internal Bearer token is required.",
            },
        )
    return authenticate_user(conn, identity_user_id(dev_user_id))


def internal_access_token_user_id(conn: BusinessConnection, token: str) -> str | None:
    row = conn.execute(
        """
        SELECT user_id
        FROM internal_access_tokens
        WHERE token_digest = %s AND revoked_at IS NULL
        """,
        (digest_access_token(token),),
    ).fetchone()
    return None if row is None else str(row["user_id"])


def authenticate_customer_read_session(
    conn: BusinessConnection,
    token: str,
) -> CurrentUser:
    """Resolve a live customer session for a read-only business route.

    ``get_database`` already owns the PostgreSQL transaction used by the
    route.  Reusing the established verifier here keeps code/device status,
    lease and single-online-session semantics identical to fenced writes and
    avoids a second identity implementation.
    """
    from app.activation_code_service import ActivationKeyError
    from app.customer_auth import SessionFencingError, verify_session_context

    try:
        context = verify_session_context(
            cast(Any, conn.raw),
            presentation_session_token=token,
        )
    except ActivationKeyError as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "SESSION_SERVICE_UNAVAILABLE",
                "message": "Session keys are not configured; customer sessions are refused.",
            },
        ) from exc
    except SessionFencingError as exc:
        raise HTTPException(
            status_code=401,
            detail={"code": exc.code, "message": exc.message},
        ) from exc
    return authenticate_user(conn, context.user_id)


def parse_bearer_token(authorization: str) -> str:
    scheme, separator, token = authorization.strip().partition(" ")
    if (
        separator != " "
        or scheme.lower() != "bearer"
        or not token
        or any(character.isspace() for character in token)
    ):
        raise invalid_token_error()
    return token


def digest_access_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def authenticate_access_token(conn: BusinessConnection, token: str) -> CurrentUser:
    user_id = internal_access_token_user_id(conn, token)
    if user_id is None:
        raise invalid_token_error()
    return authenticate_user(conn, user_id)


def invalid_token_error() -> HTTPException:
    return HTTPException(
        status_code=401,
        detail={"code": "AUTH_INVALID_TOKEN", "message": "Bearer token is invalid or revoked."},
    )


def internal_auth_required() -> bool:
    return os.environ.get(AUTH_MODE_ENV, "").lower() not in LEGACY_AUTH_MODES


def authenticate_user(conn: BusinessConnection, user_id: str | None) -> CurrentUser:
    if not user_id:
        raise HTTPException(
            status_code=401,
            detail={
                "code": "AUTH_DESKTOP_IDENTITY_REQUIRED",
                "message": "VIDEO_REPLICA_DESKTOP_USER_ID is required for desktop API requests.",
            },
        )

    row = conn.execute(
        """
        SELECT id, username, display_name, role
        FROM users
        WHERE id = %s AND is_active = 1
        """,
        (user_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(
            status_code=401,
            detail={"code": "AUTH_INVALID", "message": "User is missing or inactive."},
        )

    role = str(row["role"])
    if role not in VALID_ROLES:
        raise HTTPException(
            status_code=403,
            detail={"code": "ROLE_INVALID", "message": "User role is not supported."},
        )

    return CurrentUser(
        id=str(row["id"]),
        username=str(row["username"]),
        display_name=str(row["display_name"]),
        role=cast(Role, role),
    )


AuthenticatedUser = Annotated[CurrentUser, Depends(get_current_user)]


def identity_user_id(dev_user_id: str | None) -> str | None:
    desktop_user_id = os.environ.get(DESKTOP_USER_ID_ENV)
    if desktop_user_id:
        return desktop_user_id
    if os.environ.get(ALLOW_DEV_IDENTITY_HEADER_ENV) == "1":
        return dev_user_id
    return None


def identity_source(dev_user_id: str | None, authorization: str | None = None) -> str:
    if internal_auth_required():
        if authorization is not None:
            return "bearer"
        return "none"
    if os.environ.get(DESKTOP_USER_ID_ENV):
        return "desktop"
    if os.environ.get(ALLOW_DEV_IDENTITY_HEADER_ENV) == "1" and dev_user_id:
        return "dev_header"
    return "none"
