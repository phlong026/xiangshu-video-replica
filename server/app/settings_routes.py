from __future__ import annotations

import os
import sqlite3
import uuid
from collections.abc import Iterator
from typing import Protocol

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

from app.db import connect_database
from app.settings import SettingsRepository, normalize_provider

DATABASE_PATH_ENV = "VIDEO_REPLICA_DB_PATH"

router = APIRouter(prefix="/api/admin/settings", tags=["settings"])


class User(BaseModel):
    id: str
    role: str


class ProviderSettingsRequest(BaseModel):
    config: dict[str, str] = Field(default_factory=dict)


class RuntimeSettingsRequest(BaseModel):
    max_generation_count_per_batch: int
    max_concurrent_h3_tasks: int


class ProviderTestResult(BaseModel):
    status: str
    provider: str
    test_kind: str


class ProviderTester(Protocol):
    def connection_test(self, provider: str, config: dict[str, str]) -> ProviderTestResult: ...

    def paid_test(self, provider: str, config: dict[str, str]) -> ProviderTestResult: ...


class NoopProviderTester:
    def connection_test(self, provider: str, config: dict[str, str]) -> ProviderTestResult:
        return ProviderTestResult(
            status="configured_only" if config else "not_configured",
            provider=provider,
            test_kind="connection",
        )

    def paid_test(self, provider: str, config: dict[str, str]) -> ProviderTestResult:
        raise HTTPException(
            status_code=501,
            detail={
                "code": "PROVIDER_TEST_NOT_IMPLEMENTED",
                "message": "A real provider client is required before paid tests can run.",
            },
        )


def get_database() -> Iterator[sqlite3.Connection]:
    db_path = os.environ.get(DATABASE_PATH_ENV)
    if not db_path:
        raise HTTPException(status_code=500, detail={"code": "DATABASE_NOT_CONFIGURED"})
    with connect_database(db_path) as conn:
        yield conn


def get_provider_tester() -> ProviderTester:
    return NoopProviderTester()


def current_admin(
    conn: sqlite3.Connection = Depends(get_database),
    x_dev_user_id: str | None = Header(default=None, alias="X-Dev-User-Id"),
) -> User:
    if not x_dev_user_id:
        raise HTTPException(status_code=401, detail={"code": "AUTH_REQUIRED"})

    row = conn.execute(
        "SELECT id, role FROM users WHERE id = ? AND is_active = 1",
        (x_dev_user_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=401, detail={"code": "AUTH_REQUIRED"})

    user = User(id=str(row["id"]), role=str(row["role"]))
    if user.role != "admin":
        write_audit_log(
            conn,
            actor_user_id=user.id,
            action="security.role_denied",
            entity_type="settings",
            entity_id="provider_settings",
            metadata_json='{"required_role":"admin"}',
        )
        raise HTTPException(status_code=403, detail={"code": "ROLE_FORBIDDEN"})
    return user


@router.get("")
def read_settings(
    conn: sqlite3.Connection = Depends(get_database),
    _: User = Depends(current_admin),
) -> dict[str, object]:
    repo = SettingsRepository(conn)
    return {
        "providers": repo.read_all_provider_configs(),
        "runtime": repo.read_runtime_settings(),
    }


@router.put("/providers/{provider}")
def update_provider_settings(
    provider: str,
    payload: ProviderSettingsRequest,
    conn: sqlite3.Connection = Depends(get_database),
    admin: User = Depends(current_admin),
) -> dict[str, object]:
    provider_name = normalize_provider(provider)
    repo = SettingsRepository(conn)
    try:
        result = repo.save_provider_config(
            provider_name,
            payload.config,
            actor_user_id=admin.id,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "INVALID_SETTINGS", "message": str(exc)},
        ) from exc

    write_audit_log(
        conn,
        actor_user_id=admin.id,
        action="provider_settings.update",
        entity_type="provider_settings",
        entity_id=provider_name,
        metadata_json=f'{{"provider":"{provider_name}"}}',
    )
    return result


@router.patch("/runtime")
def update_runtime_settings(
    payload: RuntimeSettingsRequest,
    conn: sqlite3.Connection = Depends(get_database),
    admin: User = Depends(current_admin),
) -> dict[str, int]:
    repo = SettingsRepository(conn)
    try:
        result = repo.save_runtime_settings(
            max_generation_count_per_batch=payload.max_generation_count_per_batch,
            max_concurrent_h3_tasks=payload.max_concurrent_h3_tasks,
            actor_user_id=admin.id,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "INVALID_SETTINGS", "message": str(exc)},
        ) from exc

    write_audit_log(
        conn,
        actor_user_id=admin.id,
        action="runtime_settings.update",
        entity_type="runtime_settings",
        entity_id="1",
        metadata_json='{"setting":"runtime_limits"}',
    )
    return result


@router.post("/providers/{provider}/connection-test")
def connection_test(
    provider: str,
    conn: sqlite3.Connection = Depends(get_database),
    _: User = Depends(current_admin),
    tester: ProviderTester = Depends(get_provider_tester),
) -> ProviderTestResult:
    provider_name = normalize_provider(provider)
    config = SettingsRepository(conn).load_provider_config(provider_name)
    return tester.connection_test(provider_name, config)


@router.post("/providers/{provider}/paid-test")
def paid_test(
    provider: str,
    conn: sqlite3.Connection = Depends(get_database),
    _: User = Depends(current_admin),
    tester: ProviderTester = Depends(get_provider_tester),
) -> ProviderTestResult:
    provider_name = normalize_provider(provider)
    config = SettingsRepository(conn).load_provider_config(provider_name)
    return tester.paid_test(provider_name, config)


def write_audit_log(
    conn: sqlite3.Connection,
    *,
    actor_user_id: str,
    action: str,
    entity_type: str,
    entity_id: str,
    metadata_json: str,
) -> None:
    with conn:
        conn.execute(
            """
            INSERT INTO audit_logs (
                id,
                actor_user_id,
                action,
                entity_type,
                entity_id,
                metadata_json
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid.uuid4()),
                actor_user_id,
                action,
                entity_type,
                entity_id,
                metadata_json,
            ),
        )
