import asyncio

from httpx import ASGITransport, AsyncClient, Response

from app.main import app


def get(path: str, *, origin: str | None = None) -> Response:
    async def request() -> Response:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            headers = {"Origin": origin} if origin else None
            return await client.get(path, headers=headers)

    return asyncio.run(request())


def test_health_returns_service_identity() -> None:
    response = get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "video-replica-api"}


def test_health_allows_packaged_app_origins() -> None:
    for origin in ("http://tauri.localhost", "tauri://localhost"):
        response = get("/health", origin=origin)

        assert response.headers["access-control-allow-origin"] == origin


def test_openapi_exposes_health_contract() -> None:
    schema = get("/openapi.json").json()

    assert schema["paths"]["/health"]["get"]["responses"]["200"]["content"]["application/json"][
        "schema"
    ] == {"$ref": "#/components/schemas/HealthResponse"}
