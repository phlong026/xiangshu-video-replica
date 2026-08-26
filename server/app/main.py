import ipaddress
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel

from app.activation_code_routes import router as customer_activation_router
from app.admin_activation_routes import router as admin_activation_router
from app.admin_audit_routes import router as admin_audit_router
from app.admin_auth_routes import router as admin_auth_router
from app.admin_customer_routes import router as admin_customer_router
from app.admin_device_routes import router as admin_device_router
from app.admin_runtime_routes import router as admin_runtime_router
from app.admin_session_routes import router as admin_session_router
from app.analysis_routes import router as analysis_router
from app.bootstrap import (
    check_customer_production_runtime_dependencies,
    customer_public_origin,
    is_customer_production,
    trusted_proxy_networks,
)
from app.character_contracts import character_domain_openapi_schemas
from app.character_generation_routes import router as character_generation_router
from app.character_identity_routes import router as character_identity_router
from app.character_reference_routes import router as character_reference_router
from app.character_routes import router as character_router
from app.control_routes import router as control_router
from app.customer_device_routes import router as customer_device_router
from app.customer_session_routes import router as customer_session_router
from app.db_pg import close_pg_pool
from app.first_frame_routes import router as first_frame_router
from app.generation_routes import router as generation_router
from app.media_routes import router as media_router
from app.ops_metrics import (
    business_http_exception_handler,
    metrics_response,
    request_observability_middleware,
    set_current_result_code,
    unhandled_exception_response,
)
from app.payment_routes import router as payment_router
from app.rbac_routes import router as rbac_router
from app.recharge_routes import router as recharge_router
from app.settings import SettingsUnavailableError
from app.settings_routes import router as settings_router
from app.simple_character_routes import router as character_simple_router
from app.source_frame_routes import router as source_frame_router
from app.wallet_routes import router as wallet_router

# Non-loopback hosts that are still accepted: TestClient uses "testclient",
# and "localhost" is a loopback alias but not parseable as an IP address.
LOOPBACK_ALIASES = {"localhost", "testclient"}
logger = logging.getLogger(__name__)


class HealthResponse(BaseModel):
    status: Literal["ok"]
    service: str


class ReadinessResponse(BaseModel):
    status: Literal["ready"]
    service: str
    database: Literal["internal", "postgresql"]
    storage: Literal["local", "cos"]


class VideoReplicaAPI(FastAPI):
    def openapi(self) -> dict[str, Any]:
        if self.openapi_schema is not None:
            return self.openapi_schema
        schema = get_openapi(title=self.title, version=self.version, routes=self.routes)
        schema.setdefault("components", {}).setdefault("schemas", {}).update(
            character_domain_openapi_schemas()
        )
        self.openapi_schema = schema
        return schema


@asynccontextmanager
async def _lifespan(_: FastAPI) -> AsyncIterator[None]:
    # T09 / DB-08: fail the API process closed at startup when a customer-
    # production boot still carries legacy single-admin mappings, dev identity,
    # local assets or a missing admin-session key (uvicorn aborts on lifespan
    # errors). No-op on the internal SQLite lane.
    from app.bootstrap import assert_customer_production_security

    assert_customer_production_security()
    # M1 review H1: the lifespan must run the database-mode fail-closed check
    # too, so a direct `uvicorn app.main:app` boot cannot reach the internal
    # SQLite lane in customer production (bootstrap.main already validates;
    # this closes the systemd/container entrypoint). resolve raises
    # RuntimeError for the customer boundary (missing DSN) — that propagates.
    # The internal lane may legitimately boot without any database env (the
    # legacy runtime resolves per-request), so its narrow
    # MissingDatabaseConfigError is tolerated here. Every other resolution
    # error — most importantly an unsupported/mistyped URL scheme — must
    # propagate: swallowing it would advertise a healthy startup without a
    # usable PostgreSQL runtime (Codex P1).
    from app.db_pg import (
        MissingDatabaseConfigError,
        resolve_database_config,
        validate_customer_production,
    )

    try:
        _database_config = resolve_database_config()
    except MissingDatabaseConfigError:
        _database_config = None
    if _database_config is not None:
        validate_customer_production(_database_config)
    if is_customer_production():
        check_customer_production_runtime_dependencies()
    yield
    # M0 review M2: release the PG pool on shutdown so pooled connections
    # don't outlive the process. No-op on the SQLite lane (the pool is never
    # opened there) and when the pool was never created in PG mode.
    close_pg_pool()


async def _business_http_exception_dispatch(request: Request, error: Exception) -> Response:
    if not isinstance(error, HTTPException):
        raise error
    return await business_http_exception_handler(request, error)


app = VideoReplicaAPI(title="Video Replica API", version="0.1.0", lifespan=_lifespan)
app.add_exception_handler(HTTPException, _business_http_exception_dispatch)
app.add_exception_handler(Exception, unhandled_exception_response)


def _business_error_response(*, status_code: int, code: str, message: str) -> JSONResponse:
    set_current_result_code(code)
    return JSONResponse(
        status_code=status_code,
        content={"code": code, "message": message},
    )


@app.exception_handler(SettingsUnavailableError)
async def settings_unavailable_handler(
    _: Request,
    error: SettingsUnavailableError,
) -> JSONResponse:
    logger.error("Local settings are unavailable: %s", type(error).__name__)
    set_current_result_code("SETTINGS_CONFIGURATION_UNAVAILABLE")
    return JSONResponse(
        status_code=503,
        content={
            "detail": {
                "code": "SETTINGS_CONFIGURATION_UNAVAILABLE",
                "message": (
                    "本地配置仍保存在数据库中，但当前主密钥缺失或不匹配；系统未覆盖已保存配置。"
                ),
            }
        },
    )


@app.middleware("http")
async def require_loopback_client(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Enforce the internal loopback or customer trusted-proxy boundary.

    Loopback is one layer of the desktop threat model, not an authentication
    mechanism. Release requests use the server-configured desktop identity;
    X-Dev-User-Id is accepted only when development identity mode is explicitly
    enabled. Customer production instead requires a raw peer in the configured
    proxy CIDRs, an exact public Host, HTTPS, and one proxy-overwritten client IP.
    Uvicorn must keep proxy-header parsing disabled so ``request.client`` remains
    the raw peer used to enforce this trust boundary.
    """
    host = request.client.host if request.client is not None else ""
    if is_customer_production():
        try:
            peer_ip = ipaddress.ip_address(host)
        except ValueError:
            return _business_error_response(
                status_code=403,
                code="UNTRUSTED_PROXY",
                message="The request did not arrive through a trusted proxy.",
            )
        try:
            proxy_networks = trusted_proxy_networks()
            public_origin = customer_public_origin()
        except ValueError:
            return _business_error_response(
                status_code=503,
                code="INGRESS_CONFIGURATION_INVALID",
                message="Customer ingress security is not configured correctly.",
            )
        if not any(peer_ip in network for network in proxy_networks):
            return _business_error_response(
                status_code=403,
                code="UNTRUSTED_PROXY",
                message="The request did not arrive through a trusted proxy.",
            )

        expected_host = public_origin.removeprefix("https://")
        host_values = request.headers.getlist("host")
        if len(host_values) != 1 or host_values[0].strip().casefold() != expected_host.casefold():
            return _business_error_response(
                status_code=421,
                code="HOST_NOT_ALLOWED",
                message="The request Host is not configured for this service.",
            )

        proto_values = request.headers.getlist("x-forwarded-proto")
        if len(proto_values) != 1 or proto_values[0].strip().casefold() != "https":
            return _business_error_response(
                status_code=400,
                code="HTTPS_REQUIRED",
                message="Customer requests must arrive through HTTPS.",
            )

        forwarded_values = request.headers.getlist("x-forwarded-for")
        forwarded = forwarded_values[0].strip() if len(forwarded_values) == 1 else ""
        if not forwarded or "," in forwarded:
            return _business_error_response(
                status_code=400,
                code="FORWARDED_CLIENT_INVALID",
                message="The trusted proxy must provide exactly one client IP.",
            )
        try:
            forwarded_ip = ipaddress.ip_address(forwarded)
        except ValueError:
            return _business_error_response(
                status_code=400,
                code="FORWARDED_CLIENT_INVALID",
                message="The trusted proxy must provide exactly one client IP.",
            )
        if forwarded_ip == peer_ip:
            # Uvicorn's ProxyHeadersMiddleware replaces scope["client"] with
            # the single X-Forwarded-For address. Equality therefore proves
            # that the raw last-hop peer was lost before this boundary ran.
            # Fail closed even when the forged/re-written address happens to
            # land inside the trusted proxy CIDR.
            return _business_error_response(
                status_code=503,
                code="PROXY_HEADER_REWRITE_DETECTED",
                message=(
                    "The ASGI server rewrote the proxy peer; disable proxy-header "
                    "parsing for customer production."
                ),
            )
        request.state.client_ip = str(forwarded_ip)
        return await call_next(request)

    try:
        allowed = ipaddress.ip_address(host).is_loopback or host in LOOPBACK_ALIASES
    except ValueError:
        allowed = host in LOOPBACK_ALIASES
    if not allowed:
        return _business_error_response(
            status_code=403,
            code="LOOPBACK_ONLY",
            message="API 仅允许本机访问。",
        )
    request.state.client_ip = host
    return await call_next(request)


def _cors_origins() -> list[str]:
    origins = [
        "http://127.0.0.1:5173",
        "http://localhost:5173",
        "http://tauri.localhost",
        "tauri://localhost",
    ]
    if is_customer_production():
        try:
            origins.append(customer_public_origin())
        except ValueError:
            # The lifespan gate aborts startup with the actionable error. Keep
            # module import deterministic so tooling can still load OpenAPI.
            pass
    return origins


app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins(),
    allow_credentials=False,
    allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
    # PR #44 review P1: first activation is called from the WebView/browser
    # client with a mandatory Idempotency-Key (plus X-Request-Id); without
    # them in allow_headers the CORS preflight fails before the handler runs.
    allow_headers=[
        "Authorization",
        "Content-Type",
        "X-Dev-User-Id",
        "X-Admin-CSRF",
        "Idempotency-Key",
        "X-Request-Id",
    ],
    # The same review's other half: browser JS must be able to read the
    # replay marker and the echoed request id on the activation response.
    # PR #46 review P2: Retry-After joins the exposed list — the browser/
    # Tauri client must read the 429 backoff hint, or it retries blind and
    # keeps burning the (shared, PG-backed) abuse budget.
    expose_headers=["X-Request-Id", "X-Idempotent-Replay", "Retry-After"],
)
# Starlette applies the last registered middleware first. Keep observability
# outside CORS so direct OPTIONS responses also receive a request id, log and
# bounded HTTP metric instead of being silently short-circuited.
app.middleware("http")(request_observability_middleware)
app.include_router(generation_router)
app.include_router(rbac_router)
app.include_router(payment_router)
app.include_router(control_router)
app.include_router(admin_auth_router)
app.include_router(admin_customer_router)
app.include_router(admin_session_router)
app.include_router(admin_runtime_router)
app.include_router(admin_audit_router)
app.include_router(customer_activation_router)
app.include_router(customer_device_router)
app.include_router(customer_session_router)
app.include_router(admin_activation_router)
app.include_router(admin_device_router)
app.include_router(recharge_router)
app.include_router(wallet_router)
app.include_router(settings_router)
app.include_router(media_router)
app.include_router(analysis_router)
app.include_router(character_router)
app.include_router(character_identity_router)
app.include_router(character_generation_router)
app.include_router(character_reference_router)
app.include_router(source_frame_router)
app.include_router(first_frame_router)
app.include_router(character_simple_router)


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(status="ok", service="video-replica-api")


@app.get("/live", response_model=HealthResponse)
async def live() -> HealthResponse:
    """Process liveness only; dependency failures belong to ``/ready``."""
    return HealthResponse(status="ok", service="video-replica-api")


@app.get("/ready", response_model=None)
def ready() -> ReadinessResponse | JSONResponse:
    """Return ready only while every customer runtime dependency is usable.

    This is deliberately synchronous: FastAPI runs it in its worker thread
    pool, so a slow PostgreSQL/COS probe cannot block the ASGI event loop or
    prevent the dependency-free liveness endpoint from responding.
    """
    if not is_customer_production():
        return ReadinessResponse(
            status="ready",
            service="video-replica-api",
            database="internal",
            storage="local",
        )
    try:
        check_customer_production_runtime_dependencies()
    except Exception as exc:
        logger.error("Runtime readiness check failed: %s", type(exc).__name__)
        set_current_result_code("RUNTIME_DEPENDENCY_UNAVAILABLE")
        return JSONResponse(
            status_code=503,
            content={
                "status": "not_ready",
                "service": "video-replica-api",
                "code": "RUNTIME_DEPENDENCY_UNAVAILABLE",
            },
        )
    return ReadinessResponse(
        status="ready",
        service="video-replica-api",
        database="postgresql",
        storage="cos",
    )


def _ready_status_for_metrics() -> bool:
    if not is_customer_production():
        return True
    try:
        check_customer_production_runtime_dependencies()
    except Exception:
        return False
    return True


@app.get("/metrics", include_in_schema=False, response_class=PlainTextResponse)
def metrics(request: Request) -> PlainTextResponse:
    return metrics_response(request, readiness_check=_ready_status_for_metrics)
