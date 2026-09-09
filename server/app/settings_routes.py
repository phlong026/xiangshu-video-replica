from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Annotated, Literal, Protocol

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field, StrictInt

from app.auth import AuthenticatedUser, CurrentUser, Database
from app.auth import get_database as auth_get_database
from app.db_portable import BusinessConnection
from app.hifly import (
    HiflyError,
    HiflySettingsUnavailable,
    HiflyTimeoutError,
    hifly_client_from_config,
)
from app.permissions import require_role
from app.settings import ProviderName, SettingsRepository, is_secret_field, normalize_provider
from app.storage import (
    CloudStorageAdapter,
    CloudStorageConfig,
    StorageAdapter,
    StorageBackendUnavailable,
    cloud_storage_config_from_settings,
    create_storage_adapter,
)

router = APIRouter(prefix="/api/admin/settings", tags=["settings"])
logger = logging.getLogger(__name__)
get_database = auth_get_database


class ProviderSettingsRequest(BaseModel):
    config: dict[str, str] = Field(default_factory=dict)


class RuntimeSettingsRequest(BaseModel):
    max_generation_count_per_batch: int
    max_concurrent_h3_tasks: int
    active_storage_provider: Literal["cos", "local"] | None = None
    # M4/M5 review M2: the fair-queue rollout switch (revised ADR §4) gets an
    # audited write path — None leaves it unchanged; PostgreSQL-only (the
    # desktop SQLite lane keeps its legacy global FIFO and answers 422).
    fair_queue_enabled: bool | None = None


class BillingSettingsRequest(BaseModel):
    internal_base_unit_price_fen: StrictInt
    oral_unit_price_fen: StrictInt
    min_recharge_fen: StrictInt
    recharge_step_fen: StrictInt


class ProviderTestResult(BaseModel):
    status: str
    provider: str
    test_kind: str
    account_credit: int | None = None


class DiagnosticProviderResult(BaseModel):
    provider: str
    status: Literal["ok", "not_configured", "configured_only", "error"]
    configured_fields: list[str]
    adapter_capability: Literal["configuration_only", "connection_test"]
    test_kind: str
    http_status: int | None = None
    error_code: str | None = None
    failure_phase: str | None = None
    cleanup_failed: bool | None = None
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


class HiflyAccountProbe(Protocol):
    def account_credit(self) -> int: ...


class HiflyProviderTester:
    def __init__(
        self,
        *,
        fallback: ProviderTester | None = None,
        client_factory: Callable[[Mapping[str, str]], HiflyAccountProbe] = hifly_client_from_config,
    ) -> None:
        self.fallback = fallback or NoopProviderTester()
        self.client_factory = client_factory

    def connection_test(self, provider: str, config: dict[str, str]) -> ProviderTestResult:
        if provider != "hifly" or not config:
            return self.fallback.connection_test(provider, config)
        try:
            account_credit = self.client_factory(config).account_credit()
        except HiflyTimeoutError as exc:
            raise HTTPException(
                status_code=504,
                detail={
                    "code": "HIFLY_ACCOUNT_CHECK_TIMEOUT",
                    "failure_phase": "account_credit",
                    "message": "Hifly 只读账户检查超时；未创建收费任务。",
                },
            ) from exc
        except HiflySettingsUnavailable as exc:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "HIFLY_SETTINGS_INVALID",
                    "failure_phase": "configuration",
                    "message": "Hifly 配置不完整；请重新保存 API Key。",
                },
            ) from exc
        except HiflyError as exc:
            is_auth_failure = exc.vendor_code == 2003 or exc.http_status in {401, 403}
            raise HTTPException(
                # A vendor credential failure is an invalid saved setting, not
                # an expired administrator session. Returning 401 here would
                # make both settings clients sign the operator out.
                status_code=422 if is_auth_failure else 503,
                detail={
                    "code": (
                        "HIFLY_AUTH_FAILED" if is_auth_failure else "HIFLY_ACCOUNT_CHECK_FAILED"
                    ),
                    "failure_phase": "authenticate" if is_auth_failure else "account_credit",
                    "message": (
                        "Hifly 凭据认证失败；未创建收费任务。"
                        if is_auth_failure
                        else "Hifly 只读账户检查失败；未创建收费任务。"
                    ),
                },
            ) from exc
        return ProviderTestResult(
            status="ok",
            provider=provider,
            test_kind="account_credit",
            account_credit=account_credit,
        )

    def paid_test(self, provider: str, config: dict[str, str]) -> ProviderTestResult:
        return self.fallback.paid_test(provider, config)


class StorageProviderTester:
    def __init__(
        self,
        *,
        fallback: ProviderTester | None = None,
        storage_factory: Callable[[CloudStorageConfig], StorageAdapter] = create_storage_adapter,
    ) -> None:
        self.fallback = fallback or NoopProviderTester()
        self.storage_factory = storage_factory

    def connection_test(self, provider: str, config: dict[str, str]) -> ProviderTestResult:
        if provider != "cos":
            return self.fallback.connection_test(provider, config)
        if not config:
            return self.fallback.connection_test(provider, config)

        adapter: StorageAdapter | None = None
        cleanup_required = False
        put_succeeded = False
        test_key = f"projects/settings-diagnostics/{uuid.uuid4().hex}.txt"
        payload = b"video-replica storage connection check"
        failure_phase = "initialize"
        operation_error: Exception | None = None
        cleanup_error: Exception | None = None
        try:
            adapter = self.storage_factory(cloud_storage_config_from_settings(provider, config))
            cleanup_required = True
            failure_phase = "put"
            adapter.put_object(test_key, payload, content_type="text/plain")
            put_succeeded = True
            failure_phase = "head"
            metadata = adapter.head_object(test_key)
            failure_phase = "get"
            content = adapter.get_object(test_key)
            failure_phase = "verify"
            if metadata is None or metadata.size != len(payload) or content != payload:
                raise StorageBackendUnavailable("storage connection test verification failed")
        except Exception as exc:
            operation_error = exc
        finally:
            if adapter is not None and cleanup_required:
                try:
                    adapter.delete_object(test_key, actor_id="settings-diagnostic")
                except Exception as exc:
                    cleanup_error = exc
                    logger.error("Storage connection test cleanup failed for provider %s", provider)

        if cleanup_error is not None and put_succeeded:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "STORAGE_CONNECTION_TEST_CLEANUP_FAILED",
                    "cleanup_failed": True,
                    "failure_phase": "delete",
                    "message": "对象存储测试对象清理失败；可能残留测试对象，请检查本地服务日志。",
                },
            ) from cleanup_error
        if isinstance(operation_error, ValueError):
            logger.warning("Storage connection test has invalid settings for provider %s", provider)
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "STORAGE_SETTINGS_INVALID",
                    "cleanup_failed": cleanup_error is not None,
                    "failure_phase": failure_phase,
                    "message": "对象存储配置无效；请检查必填参数。",
                },
            ) from operation_error
        if operation_error is not None:
            logger.warning("Storage connection test failed for provider %s", provider)
            message = "对象存储连接测试失败；请运行测试设置并查看本地服务日志。"
            if cleanup_error is not None:
                message = (
                    "对象存储连接测试失败，且清理动作失败；可能残留测试对象，请查看本地服务日志。"
                )
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "STORAGE_CONNECTION_TEST_FAILED",
                    "cleanup_failed": cleanup_error is not None,
                    "failure_phase": failure_phase,
                    "message": message,
                },
            ) from operation_error

        return ProviderTestResult(status="ok", provider=provider, test_kind="storage_connection")

    def paid_test(self, provider: str, config: dict[str, str]) -> ProviderTestResult:
        return self.fallback.paid_test(provider, config)


def remove_cos_lifecycle_rules(
    config: dict[str, str],
    *,
    actor_id: str,
) -> dict[str, str]:
    """保存 COS 配置后删除自动过期规则；失败不阻断配置保存。"""
    try:
        adapter = create_storage_adapter(cloud_storage_config_from_settings("cos", config))
    except (ValueError, StorageBackendUnavailable) as exc:
        logger.warning("COS lifecycle rule removal skipped: %s", exc, exc_info=True)
        return {
            "status": "skipped",
            "message": "对象存储配置不完整或客户端初始化失败，已跳过旧媒体过期规则清理。",
        }
    cloud_adapter = adapter if isinstance(adapter, CloudStorageAdapter) else None
    if cloud_adapter is None:
        return {
            "status": "skipped",
            "message": "当前存储适配器不支持生命周期规则。",
        }
    try:
        cloud_adapter.remove_lifecycle_rules(actor_id=actor_id)
    except StorageBackendUnavailable as exc:
        logger.warning("COS lifecycle rules removal failed: %s", exc)
        return {
            "status": "failed",
            "message": "自动过期规则删除失败；配置已保存，可重新保存以重试。",
        }
    return {
        "status": "removed",
        "message": "已移除项目素材与成片的 180 天自动过期规则，按永久保存执行。",
    }


def get_provider_tester() -> ProviderTester:
    return StorageProviderTester(fallback=HiflyProviderTester())


def require_settings_admin(conn: Database, actor: AuthenticatedUser) -> CurrentUser:
    require_role(
        conn,
        actor=actor,
        allowed_roles={"admin"},
        action="settings.manage",
        entity_type="settings",
        entity_id="admin_settings",
    )
    return actor


SettingsAdmin = Annotated[CurrentUser, Depends(require_settings_admin)]


def require_supported_provider(provider: str) -> ProviderName:
    try:
        return normalize_provider(provider)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "UNSUPPORTED_PROVIDER",
                "message": "该服务已不受支持。",
            },
        ) from exc


@router.get("")
def read_settings(
    conn: Database,
    _: SettingsAdmin,
) -> dict[str, object]:
    repo = SettingsRepository(conn)
    return {
        "providers": repo.read_all_provider_configs(),
        "runtime": repo.read_runtime_settings(),
        "billing": repo.read_billing_settings(),
    }


@router.put("/providers/{provider}")
def update_provider_settings(
    provider: str,
    payload: ProviderSettingsRequest,
    conn: Database,
    admin: SettingsAdmin,
) -> dict[str, object]:
    provider_name = require_supported_provider(provider)
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

    lifecycle = None
    if provider_name == "cos":
        lifecycle = remove_cos_lifecycle_rules(incoming_config, actor_id=admin.id)
        write_audit_log(
            conn,
            actor_user_id=admin.id,
            action=f"cos_lifecycle.{lifecycle['status']}",
            entity_type="provider_settings",
            entity_id="cos",
            metadata_json=json.dumps({"status": lifecycle["status"]}, sort_keys=True),
        )
    if lifecycle is not None and isinstance(result, dict):
        result["lifecycle"] = lifecycle
    return result


@router.post("/providers/{provider}/secrets/{field}/reveal")
def reveal_provider_secret(
    provider: str,
    field: str,
    conn: Database,
    admin: SettingsAdmin,
) -> JSONResponse:
    provider_name = require_supported_provider(provider)
    if not is_secret_field(field):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "SETTINGS_FIELD_NOT_SECRET",
                "message": "该字段不是密钥。",
            },
        )

    value = SettingsRepository(conn).load_provider_config(provider_name).get(field)
    if not value:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "SETTINGS_SECRET_NOT_CONFIGURED",
                "message": "该密钥尚未配置。",
            },
        )

    # 只记录“谁查看了哪个字段”；明文密钥不进日志、审计或普通设置快照。
    write_audit_log(
        conn,
        actor_user_id=admin.id,
        action="provider_settings.secret_reveal",
        entity_type="provider_settings",
        entity_id=provider_name,
        metadata_json=json.dumps(
            {"provider": provider_name, "field": field},
            sort_keys=True,
        ),
    )
    return JSONResponse(
        content={"value": value},
        headers={
            "Cache-Control": "no-store",
            "Pragma": "no-cache",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.patch("/runtime")
def update_runtime_settings(
    payload: RuntimeSettingsRequest,
    conn: Database,
    admin: SettingsAdmin,
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
            fair_queue_enabled=payload.fair_queue_enabled,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "INVALID_SETTINGS", "message": str(exc)},
        ) from exc

    # The queue mode is a production rollout decision (revised ADR §4) —
    # record it in the audit metadata, not just the generic limits label.
    audit_metadata: dict[str, object] = {"setting": "runtime_limits"}
    if payload.fair_queue_enabled is not None:
        audit_metadata["fair_queue_enabled"] = payload.fair_queue_enabled
    write_audit_log(
        conn,
        actor_user_id=admin.id,
        action="runtime_settings.update",
        entity_type="runtime_settings",
        entity_id="1",
        metadata_json=json.dumps(audit_metadata, sort_keys=True),
    )
    return result


@router.patch("/billing")
def update_billing_settings(
    payload: BillingSettingsRequest,
    conn: Database,
    admin: SettingsAdmin,
) -> dict[str, int]:
    try:
        result = SettingsRepository(conn).save_billing_settings(
            internal_base_unit_price_fen=payload.internal_base_unit_price_fen,
            oral_unit_price_fen=payload.oral_unit_price_fen,
            min_recharge_fen=payload.min_recharge_fen,
            recharge_step_fen=payload.recharge_step_fen,
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
        action="billing_settings.update",
        entity_type="runtime_settings",
        entity_id="1",
        metadata_json='{"setting":"internal_billing"}',
    )
    return result


@router.post(
    "/providers/{provider}/connection-test",
    response_model=ProviderTestResult,
    response_model_exclude_none=True,
)
def connection_test(
    provider: str,
    conn: Database,
    _: SettingsAdmin,
    tester: ProviderTester = Depends(get_provider_tester),
) -> ProviderTestResult:
    provider_name = require_supported_provider(provider)
    config = SettingsRepository(conn).load_provider_config(provider_name)
    return tester.connection_test(provider_name, config)


@router.post(
    "/providers/{provider}/paid-test",
    response_model=ProviderTestResult,
    response_model_exclude_none=True,
)
def paid_test(
    provider: str,
    conn: Database,
    _: SettingsAdmin,
    tester: ProviderTester = Depends(get_provider_tester),
) -> ProviderTestResult:
    provider_name = require_supported_provider(provider)
    config = SettingsRepository(conn).load_provider_config(provider_name)
    return tester.paid_test(provider_name, config)


@router.post("/diagnostic-test", response_model=SettingsDiagnosticReport)
def run_settings_diagnostic(
    conn: Database,
    admin: SettingsAdmin,
    tester: ProviderTester = Depends(get_provider_tester),
) -> SettingsDiagnosticReport:
    repo = SettingsRepository(conn)
    results: list[DiagnosticProviderResult] = []

    for provider in ("metaso", "apilio", "cos", "deepseek"):
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
                    failure_phase=failure_phase_from_http_exception(exc),
                    cleanup_failed=cleanup_failed_from_http_exception(exc),
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
        message = (
            configured_only_message(provider)
            if status == "configured_only"
            else {
                "ok": "连接测试通过。",
                "not_configured": "缺少必要参数。",
                "error": "连接测试失败；请下载诊断日志查看上下文。",
            }[status]
        )
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
        "ok"
        if all(result.status in {"ok", "configured_only"} for result in results)
        else "attention"
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
    conn: Database,
    _: SettingsAdmin,
) -> Response:
    row = conn.execute(
        """
        SELECT metadata_json
        FROM audit_logs
        WHERE entity_id = %s AND action = 'settings.diagnostic_test'
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
        # 掩码值（mask_secret 产出的 ******** 尾号形态）回传等于"未修改该
        # 密钥"：保留库中原值，避免把真实凭据覆盖成星号字符串。
        if is_secret_field(key) and value.strip().startswith("********"):
            continue
        if value.strip():
            merged[key] = value
        elif not is_secret_field(key):
            merged.pop(key, None)
    return merged


def error_code_from_http_exception(error: HTTPException) -> str:
    if isinstance(error.detail, dict) and isinstance(error.detail.get("code"), str):
        return str(error.detail["code"])
    return "PROVIDER_CONNECTION_ERROR"


def failure_phase_from_http_exception(error: HTTPException) -> str | None:
    if isinstance(error.detail, dict) and isinstance(error.detail.get("failure_phase"), str):
        return str(error.detail["failure_phase"])
    return None


def cleanup_failed_from_http_exception(error: HTTPException) -> bool | None:
    if isinstance(error.detail, dict) and isinstance(error.detail.get("cleanup_failed"), bool):
        return bool(error.detail["cleanup_failed"])
    return None


def elapsed_milliseconds(started_at: float) -> int:
    return int((time.monotonic() - started_at) * 1000)


def configured_only_message(provider: str) -> str:
    if provider == "metaso":
        return "".join(
            (
                "H3 参数已保存。测试设置不会提交会产生费用的生成任务；",
                "实际任务由生成 Worker 处理。",
            )
        )
    if provider == "apilio":
        return "".join(
            (
                "模型服务参数已保存。视频拆解和首帧任务会按需调用；",
                "测试设置不会发起计费模型请求。",
            )
        )
    if provider == "deepseek":
        return "".join(
            (
                "AI 改写服务参数已保存。点击工作台「AI 改写」按钮时会实际调用；",
                "测试设置不会发起计费请求。",
            )
        )
    return "参数已保存；本次测试未发起外部调用。"


def write_audit_log(
    conn: BusinessConnection,
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
            VALUES (%s, %s, %s, %s, %s, %s)
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
