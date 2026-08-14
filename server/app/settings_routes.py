from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Literal, Protocol

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.db import connect_database
from app.settings import SettingsRepository, is_secret_field, normalize_provider

DATABASE_PATH_ENV = "VIDEO_REPLICA_DB_PATH"

router = APIRouter(prefix="/api/admin/settings", tags=["settings"])
logger = logging.getLogger(__name__)


class User(BaseModel):
    id: str
    role: str


class ProviderSettingsRequest(BaseModel):
    config: dict[str, str] = Field(default_factory=dict)


class RuntimeSettingsRequest(BaseModel):
    max_generation_count_per_batch: int
    max_concurrent_h3_tasks: int
    active_storage_provider: Literal["cos", "oss"] | None = None


class ProviderTestResult(BaseModel):
    status: str
    provider: str
    test_kind: str


class DiagnosticProviderResult(BaseModel):
    provider: str
    status: Literal["ok", "not_configured", "configured_only", "error"]
    configured_fields: list[str]
    adapter_capability: Literal["configuration_only", "connection_test"]
    test_kind: str
    http_status: int | None = None
    error_code: str | None = None
    latency_ms: int | None = None
    message: str


class SettingsDiagnosticReport(BaseModel):
    id: str
    status: Literal["ok", "attention"]
    generated_at: str
    providers: list[DiagnosticProviderResult]
    download_url: str


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
        saved_config = repo.load_provider_config(provider_name)
        incoming_config = merge_provider_config(saved_config, payload.config)
        result = repo.save_provider_config(
            provider_name,
            incoming_config,
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
) -> dict[str, int | str]:
    repo = SettingsRepository(conn)
    try:
        current = repo.read_runtime_settings()
        result = repo.save_runtime_settings(
            max_generation_count_per_batch=payload.max_generation_count_per_batch,
            max_concurrent_h3_tasks=payload.max_concurrent_h3_tasks,
            active_storage_provider=str(
                payload.active_storage_provider or current["active_storage_provider"]
            ),
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


@router.post("/diagnostic-test", response_model=SettingsDiagnosticReport)
def run_settings_diagnostic(
    conn: sqlite3.Connection = Depends(get_database),
    admin: User = Depends(current_admin),
    tester: ProviderTester = Depends(get_provider_tester),
) -> SettingsDiagnosticReport:
    repo = SettingsRepository(conn)
    results: list[DiagnosticProviderResult] = []

    for provider in ("metaso", "apilio", "cos", "oss"):
        config = repo.load_provider_config(provider)
        configured_fields = sorted(config)
        if not config:
            results.append(
                DiagnosticProviderResult(
                    provider=provider,
                    status="not_configured",
                    configured_fields=[],
                    adapter_capability="configuration_only",
                    test_kind="configuration",
                    message="尚未保存该服务的必要参数。",
                )
            )
            continue

        try:
            started_at = time.monotonic()
            provider_result = tester.connection_test(provider, config)
        except HTTPException as exc:
            logger.warning(
                "Provider diagnostic failed for %s with HTTP status %s",
                provider,
                exc.status_code,
            )
            results.append(
                DiagnosticProviderResult(
                    provider=provider,
                    status="error",
                    configured_fields=configured_fields,
                    adapter_capability="connection_test",
                    test_kind="connection",
                    http_status=exc.status_code,
                    error_code=error_code_from_http_exception(exc),
                    latency_ms=elapsed_milliseconds(started_at),
                    message="测试接口返回错误；请下载诊断日志查看错误码。",
                )
            )
            continue
        except Exception as exc:
            logger.warning(
                "Provider diagnostic raised %s for %s",
                type(exc).__name__,
                provider,
            )
            results.append(
                DiagnosticProviderResult(
                    provider=provider,
                    status="error",
                    configured_fields=configured_fields,
                    adapter_capability="connection_test",
                    test_kind="connection",
                    error_code="DIAGNOSTIC_INTERNAL_ERROR",
                    latency_ms=elapsed_milliseconds(started_at),
                    message="测试过程发生内部错误；请下载诊断日志并检查本地服务日志。",
                )
            )
            continue

        if provider_result.status == "ok":
            status: Literal["ok", "not_configured", "configured_only", "error"] = "ok"
        elif provider_result.status == "configured_only":
            status = "configured_only"
        elif provider_result.status == "not_configured":
            status = "not_configured"
        else:
            status = "error"
        message = {
            "ok": "连接测试通过。",
            "configured_only": "参数已保存；真实服务适配器尚未启用，因此未发起外部调用。",
            "not_configured": "缺少必要参数。",
            "error": "连接测试失败；请下载诊断日志查看上下文。",
        }[status]
        results.append(
            DiagnosticProviderResult(
                provider=provider,
                status=status,
                configured_fields=configured_fields,
                adapter_capability=(
                    "configuration_only" if status == "configured_only" else "connection_test"
                ),
                test_kind=provider_result.test_kind,
                latency_ms=elapsed_milliseconds(started_at),
                message=message,
            )
        )

    report_id = str(uuid.uuid4())
    report_status: Literal["ok", "attention"] = (
        "ok" if all(result.status == "ok" for result in results) else "attention"
    )
    download_url = f"/api/admin/settings/diagnostic-reports/{report_id}/download"
    report = SettingsDiagnosticReport(
        id=report_id,
        status=report_status,
        generated_at=datetime.now(UTC).isoformat(),
        providers=results,
        download_url=download_url,
    )
    write_audit_log(
        conn,
        actor_user_id=admin.id,
        action="settings.diagnostic_test",
        entity_type="settings_diagnostic",
        entity_id=report_id,
        metadata_json=json.dumps(report.model_dump(), ensure_ascii=False, sort_keys=True),
    )
    return report


@router.get("/diagnostic-reports/{report_id}/download")
def download_settings_diagnostic(
    report_id: str,
    conn: sqlite3.Connection = Depends(get_database),
    _: User = Depends(current_admin),
) -> Response:
    row = conn.execute(
        """
        SELECT metadata_json
        FROM audit_logs
        WHERE entity_id = ? AND action = 'settings.diagnostic_test'
        """,
        (report_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail={"code": "DIAGNOSTIC_REPORT_NOT_FOUND"})

    return Response(
        content=str(row["metadata_json"]),
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="settings-diagnostic-{report_id}.json"'
        },
    )


def merge_provider_config(
    saved_config: dict[str, str], incoming_config: dict[str, str]
) -> dict[str, str]:
    merged = dict(saved_config)
    for key, value in incoming_config.items():
        if value.strip():
            merged[key] = value
        elif not is_secret_field(key):
            merged.pop(key, None)
    return merged


def error_code_from_http_exception(error: HTTPException) -> str:
    if isinstance(error.detail, dict) and isinstance(error.detail.get("code"), str):
        return str(error.detail["code"])
    return "PROVIDER_CONNECTION_ERROR"


def elapsed_milliseconds(started_at: float) -> int:
    return int((time.monotonic() - started_at) * 1000)


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
