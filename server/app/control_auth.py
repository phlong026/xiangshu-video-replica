from __future__ import annotations

import hashlib
import hmac
import os
import string
from typing import Annotated

from fastapi import Depends, Header, HTTPException

from app.auth import CurrentUser, Database, authenticate_user

CONTROL_PROXY_TOKEN_DIGEST_ENV = "CONTROL_PROXY_TOKEN_DIGEST"
CONTROL_ADMIN_USER_ID_ENV = "CONTROL_ADMIN_USER_ID"


def get_control_user(
    conn: Database,
    proxy_token: Annotated[str | None, Header(alias="X-Control-Proxy-Token")] = None,
) -> CurrentUser:
    expected_digest = os.environ.get(CONTROL_PROXY_TOKEN_DIGEST_ENV, "").strip().lower()
    admin_user_id = os.environ.get(CONTROL_ADMIN_USER_ID_ENV, "").strip()
    if not _valid_sha256_digest(expected_digest) or not admin_user_id:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "CONTROL_AUTH_NOT_CONFIGURED",
                "message": "Control proxy authentication is not configured.",
            },
        )
    if proxy_token is None:
        raise control_auth_error()

    supplied_digest = hashlib.sha256(proxy_token.encode("utf-8")).hexdigest()
    if not hmac.compare_digest(supplied_digest, expected_digest):
        raise control_auth_error()

    actor = authenticate_user(conn, admin_user_id)
    if actor.role != "admin":
        raise HTTPException(
            status_code=403,
            detail={
                "code": "CONTROL_ADMIN_REQUIRED",
                "message": "The configured control identity must be an active admin.",
            },
        )
    return actor


def control_auth_error() -> HTTPException:
    return HTTPException(
        status_code=401,
        detail={
            "code": "CONTROL_AUTH_INVALID",
            "message": "A valid control proxy token is required.",
        },
    )


def _valid_sha256_digest(value: str) -> bool:
    return len(value) == 64 and all(character in string.hexdigits for character in value)


ControlUser = Annotated[CurrentUser, Depends(get_control_user)]
