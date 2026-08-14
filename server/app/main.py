import ipaddress
from collections.abc import Awaitable, Callable
from typing import Literal

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.analysis_routes import router as analysis_router
from app.character_routes import router as character_router
from app.first_frame_routes import router as first_frame_router
from app.generation_routes import router as generation_router
from app.media_routes import router as media_router
from app.rbac_routes import router as rbac_router
from app.settings_routes import router as settings_router
from app.source_frame_routes import router as source_frame_router

# Non-loopback hosts that are still accepted: TestClient uses "testclient",
# and "localhost" is a loopback alias but not parseable as an IP address.
LOOPBACK_ALIASES = {"localhost", "testclient"}


class HealthResponse(BaseModel):
    status: Literal["ok"]
    service: str


app = FastAPI(title="Video Replica API", version="0.1.0")


@app.middleware("http")
async def require_loopback_client(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Reject requests that did not originate from this machine.

    The dev identity header X-Dev-User-Id is trusted only because the API is
    bound to the loopback interface. This middleware fails closed if the API
    is ever started on 0.0.0.0 or exposed through a tunnel/port-forward: any
    non-loopback caller gets 403 before any identity or business logic runs.
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
    allow_headers=["Content-Type", "X-Dev-User-Id"],
)
app.include_router(generation_router)
app.include_router(rbac_router)
app.include_router(settings_router)
app.include_router(media_router)
app.include_router(analysis_router)
app.include_router(character_router)
app.include_router(source_frame_router)
app.include_router(first_frame_router)


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(status="ok", service="video-replica-api")
