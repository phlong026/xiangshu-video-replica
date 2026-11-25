"""Admin issuance, deadline policy, delivery and paid software renewal."""

from __future__ import annotations

import json
from datetime import datetime
from uuid import uuid4

import psycopg
from fastapi import APIRouter, Request, Response
from pydantic import AwareDatetime, Field

from app.activation_code_service import (
    ActivationExportError,
    ActivationKeyError,
    activation_code_hmac_key,
    compute_code_digest,
    decrypt_code_package,
    encrypt_code_package,
    export_aead_key,
    generate_activation_code,
    highest_code_hmac_key_version,
    highest_export_aead_key_version,
    mask_activation_code,
)
from app.admin_auth_routes import CustomerReader, CustomerWriter
from app.admin_write_contract import (
    AdminWriteContract,
    http_error,
    require_write_contract,
    write_with_idempotency,
)
from app.db_pg import pg_transaction
from app.ops_metrics import get_or_create_request_id
from app.software_licenses import add_authorization_years, effective_activation_expiry
from app.sql_pagination import PAGE_CLAUSE, page_bounds

router = APIRouter(prefix="/api/control/software-licenses", tags=["admin-software-licenses"])


class IssueSoftwareLicense(AdminWriteContract):
    authorization_years: int = Field(default=1, ge=1, le=3)
    activation_expires_at: AwareDatetime


class UpdateActivationPolicy(AdminWriteContract):
    auto_extend: bool


def _audit(
    conn: psycopg.Connection,
    *,
    actor: CustomerWriter,
    action: str,
    license_id: str,
    reason: str,
    request_id: str,
    details: dict[str, object] | None = None,
) -> None:
    conn.execute(
        "INSERT INTO audit_logs (id, actor_user_id, action, entity_type, entity_id, metadata_json) "
        "VALUES (%s, %s, %s, 'software_license', %s, %s)",
        (
            str(uuid4()),
            actor.user_id,
            f"admin.software_license.{action}",
            license_id,
            json.dumps({"reason": reason.strip(), "request_id": request_id, **(details or {})}),
        ),
    )


@router.get("")
def list_software_licenses(
    _actor: CustomerReader, search: str = "", limit: int = 25, offset: int = 0
) -> dict[str, object]:
    limit, offset = page_bounds(limit, offset, max_limit=100, min_limit=1)
    search = search.strip()[:128]
    pattern = "%" + search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    where = "WHERE l.masked_code ILIKE %s OR u.username ILIKE %s" if search else ""
    params = (pattern, pattern) if search else ()
    with pg_transaction() as conn:
        settings = conn.execute(
            "SELECT software_activation_auto_extend, now() FROM runtime_settings WHERE id = 1"
        ).fetchone()
        if settings is None:
            raise http_error(503, "ACTIVATION_SETTINGS_UNAVAILABLE", "授权设置暂不可用。")
        auto_extend, now = settings
        total = conn.execute(
            f"SELECT count(*) FROM software_licenses l "  # noqa: S608
            f"LEFT JOIN users u ON u.id = l.user_id {where}",
            params,
        ).fetchone()
        rows = conn.execute(
            f"SELECT l.id, l.masked_code, l.activation_expires_at, "  # noqa: S608
            f"l.authorization_years, u.username, l.bound_at, l.authorized_until "
            f"FROM software_licenses l LEFT JOIN users u ON u.id = l.user_id {where} "
            f"ORDER BY l.created_at DESC, l.id DESC {PAGE_CLAUSE}",
            (*params, limit, offset),
        ).fetchall()

    items = []
    for code_id, masked, deadline, years, username, bound_at, until in rows:
        effective = (
            effective_activation_expiry(deadline, now, auto_extend)
            if deadline is not None and bound_at is None
            else deadline
        )
        expires_at = until if bound_at is not None else effective
        items.append(
            {
                "id": code_id,
                "masked_code": masked,
                "activation_expires_at": effective,
                "authorization_years": years,
                "username": username,
                "authorized_until": until,
                "expired": expires_at is not None and expires_at <= now,
                "can_reveal": deadline is not None and bound_at is None,
            }
        )
    return {"auto_extend": auto_extend, "items": items, "total": total[0] if total else 0}


@router.post("", status_code=201)
def issue_software_license(
    body: IssueSoftwareLicense, request: Request, response: Response, actor: CustomerWriter
) -> dict[str, object]:
    def business(conn: psycopg.Connection, request_id: str) -> dict[str, object]:
        clock = conn.execute("SELECT clock_timestamp()").fetchone()
        assert clock is not None
        if body.activation_expires_at <= clock[0]:
            raise http_error(400, "INVALID_ACTIVATION_DEADLINE", "激活截止时间必须晚于当前时间。")
        license_id = str(uuid4())
        code = generate_activation_code()
        masked = mask_activation_code(code)
        try:
            key_version = highest_code_hmac_key_version()
            digest = compute_code_digest(code, key=activation_code_hmac_key(key_version))
            export_version = highest_export_aead_key_version()
            ciphertext, _ = encrypt_code_package(
                [code], key=export_aead_key(export_version), batch_id=license_id
            )
        except ActivationKeyError as exc:
            raise http_error(503, "ACTIVATION_KEYS_UNAVAILABLE", "激活密钥尚未配置。") from exc
        conn.execute(
            "INSERT INTO software_licenses "
            "(id, masked_code, code_digest, ciphertext, export_key_version, "
            "activation_expires_at, authorization_years, created_by_user_id) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (
                license_id,
                masked,
                digest,
                ciphertext,
                export_version,
                body.activation_expires_at,
                body.authorization_years,
                actor.user_id,
            ),
        )
        _audit(
            conn,
            actor=actor,
            action="issued",
            license_id=license_id,
            reason=body.reason,
            request_id=request_id,
            details={
                "authorization_years": body.authorization_years,
                "activation_expires_at": body.activation_expires_at.isoformat(),
            },
        )
        return {"id": license_id, "masked_code": masked}

    return write_with_idempotency(request, response, actor, body, business, success_status=201)


@router.patch("/activation-policy")
def update_activation_policy(
    body: UpdateActivationPolicy, request: Request, response: Response, actor: CustomerWriter
) -> dict[str, object]:
    def business(conn: psycopg.Connection, request_id: str) -> dict[str, object]:
        changed = conn.execute(
            "UPDATE runtime_settings SET software_activation_auto_extend = %s, "
            "updated_at = CURRENT_TIMESTAMP WHERE id = 1",
            (body.auto_extend,),
        )
        if changed.rowcount != 1:
            raise http_error(503, "ACTIVATION_SETTINGS_UNAVAILABLE", "授权设置暂不可用。")
        _audit(
            conn,
            actor=actor,
            action="activation_policy_changed",
            license_id="activation-policy",
            reason=body.reason,
            request_id=request_id,
            details={"auto_extend": body.auto_extend},
        )
        return {"auto_extend": body.auto_extend}

    return write_with_idempotency(request, response, actor, body, business, success_status=200)


@router.post("/{license_id}/reveal")
def reveal_software_license(
    license_id: str,
    body: AdminWriteContract,
    request: Request,
    response: Response,
    actor: CustomerWriter,
) -> dict[str, str]:
    _, reason = require_write_contract(request, body)
    with pg_transaction() as conn:
        row = conn.execute(
            "SELECT ciphertext, export_key_version FROM software_licenses "
            "WHERE id = %s AND user_id IS NULL",
            (license_id,),
        ).fetchone()
        if row is None or row[0] is None:
            raise http_error(404, "SOFTWARE_ACTIVATION_NOT_FOUND", "没有可交付的激活码。")
        try:
            code = decrypt_code_package(row[0], key=export_aead_key(row[1]), batch_id=license_id)[0]
        except (ActivationKeyError, ActivationExportError) as exc:
            raise http_error(503, "ACTIVATION_KEYS_UNAVAILABLE", "激活码暂无法读取。") from exc
        _audit(
            conn,
            actor=actor,
            action="revealed",
            license_id=license_id,
            reason=reason,
            request_id=get_or_create_request_id(request),
        )
    response.headers["Cache-Control"] = "no-store"
    return {"activation_code": code}


@router.post("/{license_id}/renew")
def renew_software_license(
    license_id: str,
    body: AdminWriteContract,
    request: Request,
    response: Response,
    actor: CustomerWriter,
) -> dict[str, object]:
    def business(conn: psycopg.Connection, request_id: str) -> dict[str, object]:
        row = conn.execute(
            "SELECT authorized_until FROM software_licenses "
            "WHERE id = %s AND user_id IS NOT NULL FOR UPDATE",
            (license_id,),
        ).fetchone()
        if row is None:
            raise http_error(404, "SOFTWARE_LICENSE_NOT_FOUND", "未找到已绑定的软件授权。")
        clock = conn.execute("SELECT clock_timestamp()").fetchone()
        assert clock is not None
        previous: datetime = row[0]
        until = add_authorization_years(max(previous, clock[0]), 1)
        conn.execute(
            "UPDATE software_licenses SET authorized_until = %s WHERE id = %s",
            (until, license_id),
        )
        _audit(
            conn,
            actor=actor,
            action="renewed",
            license_id=license_id,
            reason=body.reason,
            request_id=request_id,
            details={"previous_until": previous.isoformat(), "authorized_until": until.isoformat()},
        )
        return {"authorized_until": until.isoformat()}

    return write_with_idempotency(request, response, actor, body, business, success_status=200)
