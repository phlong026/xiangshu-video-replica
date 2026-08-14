from typing import Literal

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from app.analysis_routes import router as analysis_router
from app.character_routes import router as character_router
from app.generation_routes import router as generation_router
from app.media_routes import router as media_router
from app.rbac_routes import router as rbac_router
from app.settings_routes import router as settings_router
from app.source_frame_routes import router as source_frame_router


class HealthResponse(BaseModel):
    status: Literal["ok"]
    service: str


app = FastAPI(title="Video Replica API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://127.0.0.1:5173",
        "http://localhost:5173",
        "http://tauri.localhost",
        "tauri://localhost",
    ],
    allow_credentials=False,
    allow_methods=["GET", "POST", "PATCH", "PUT"],
    allow_headers=["Content-Type", "X-Dev-User-Id"],
)
app.include_router(generation_router)
app.include_router(rbac_router)
app.include_router(settings_router)
app.include_router(media_router)
app.include_router(analysis_router)
app.include_router(character_router)
app.include_router(source_frame_router)


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(status="ok", service="video-replica-api")
