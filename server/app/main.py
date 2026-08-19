import ipaddress
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.analysis_routes import router as analysis_router
from app.character_contracts import character_domain_openapi_schemas
from app.character_generation_routes import router as character_generation_router
from app.character_identity_routes import router as character_identity_router
from app.character_reference_routes import router as character_reference_router
from app.character_routes import router as character_router
from app.first_frame_routes import router as first_frame_router
from app.generation_routes import router as generation_router
from app.media_routes import router as media_router
from app.rbac_routes import router as rbac_router
from app.recharge_routes import router as recharge_router
from app.settings import SettingsUnavailableError
from app.settings_routes import router as settings_router
from app.simple_character_routes import router as character_simple_router
from app.source_frame_routes import router as source_frame_router

# Non-loopback hosts that are still accepted: TestClient uses "testclient",
# and "localhost" is a loopback alias but not parseable as an IP address.
LOOPBACK_ALIASES = {"localhost", "testclient"}
logger = logging.getLogger(__name__)


class HealthResponse(BaseModel):
    status: Literal["ok"]
    service: str


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


app = VideoReplicaAPI(title="Video Replica API", version="0.1.0")


@app.exception_handler(SettingsUnavailableError)
async def settings_unavailable_handler(
    _: Request,
    error: SettingsUnavailableError,
) -> JSONResponse:
    logger.error("Local settings are unavailable: %s", type(error).__name__)
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
    """Reject requests that did not originate from this machine.

    Loopback is one layer of the desktop threat model, not an authentication
    mechanism. Release requests use the server-configured desktop identity;
    X-Dev-User-Id is accepted only when development identity mode is explicitly
    enabled. Any non-loopback caller gets 403 before identity or business logic.
    """
    host = request.client.host if request.client is not None else ""
    try:
        allowed = ipaddress.ip_address(host).is_loopback or host in LOOPBACK_ALIASES
    except ValueError:
        allowed = host in LOOPBACK_ALIASES
    if not allowed:
        return JSONResponse(
            status_code=403,
            content={"code": "LOOPBACK_ONLY", "message": "API 仅允许本机访问。"},
        )
    return await call_next(request)


app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://127.0.0.1:5173",
        "http://localhost:5173",
        "http://tauri.localhost",
        "tauri://localhost",
    ],
    allow_credentials=False,
    allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE"],
    allow_headers=["Authorization", "Content-Type", "X-Dev-User-Id"],
)
app.include_router(generation_router)
app.include_router(rbac_router)
app.include_router(recharge_router)
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
