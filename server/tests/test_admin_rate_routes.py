"""W10 — operation rate management route tests (费率管理读写面).

Dedicated PG database (w10_rates_test) migrated to head, mirroring
``test_admin_activation_routes``. Covers: seeded defaults, the shared
admin write contract (idempotency key / confirm / reason), auditor
read-only enforcement, unknown-subject rejection, and the audit trail
(old → new + reason + request id) that backs the「历史变更」view.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pg_test_kit import require_pg_or_explicit_skip

from app.admin_auth_routes import (
    ADMIN_CSRF_HEADER,
    ADMIN_SESSION_HMAC_KEY_ENV,
    issue_exchange_credential,
)
from app.admin_write_contract import IDEMPOTENCY_KEY_HEADER
from app.db_pg import DATABASE_URL_ENV

TEST_KEY = "w10-rate-test-hmac-key-0123456789abcdef"


DEFAULT_DSN = "postgresql://testuser:testpass@localhost:5433/customer_v3_test"
W10_DB_NAME = "w10_rates_test"


def _pg_dsn() -> str:
    import os

    return os.environ.get("TEST_POSTGRESQL_URL", DEFAULT_DSN)


def _admin_dsn() -> str:
    return _pg_dsn().rsplit("/", 1)[0] + "/postgres"


def _w10_dsn() -> str:
    return _pg_dsn().rsplit("/", 1)[0] + f"/{W10_DB_NAME}"


@pytest.fixture(scope="module")
def rates_pg_dsn() -> Iterator[str]:
    """Dedicated migrated database with operator seed users."""
    from alembic import command
    from alembic.config import Config

    require_pg_or_explicit_skip(_pg_dsn())
    with psycopg.connect(_admin_dsn(), autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{W10_DB_NAME}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{W10_DB_NAME}"')
    server_dir = Path(__file__).resolve().parent.parent
    config = Config(str(server_dir / "alembic.ini"))
    config.set_main_option("script_location", str(server_dir / "migrations"))
    config.set_main_option(
        "sqlalchemy.url", _w10_dsn().replace("postgresql://", "postgresql+psycopg://")
    )
    command.upgrade(config, "head")
    with psycopg.connect(_w10_dsn(), autocommit=True) as conn:
        for user_id, role in (("admin_u", "admin"), ("auditor_u", "auditor")):
            conn.execute(
                "INSERT INTO users (id, username, display_name, role) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT (id) DO NOTHING",
                (user_id, user_id, user_id.replace("_", " ").title(), role),
            )
    try:
        yield _w10_dsn()
    finally:
        with psycopg.connect(_admin_dsn(), autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{W10_DB_NAME}" WITH (FORCE)')


@pytest.fixture()
def rates_app(monkeypatch: pytest.MonkeyPatch, rates_pg_dsn: str) -> Iterator[FastAPI]:
    from app.admin_auth_routes import router as admin_auth_router
    from app.admin_rate_routes import router as admin_rate_router

    app = FastAPI()
    app.include_router(admin_auth_router)
    app.include_router(admin_rate_router)
    monkeypatch.setenv(DATABASE_URL_ENV, rates_pg_dsn)
    monkeypatch.setenv(ADMIN_SESSION_HMAC_KEY_ENV, TEST_KEY)
    monkeypatch.delenv("VIDEO_REPLICA_CUSTOMER_PRODUCTION", raising=False)
    yield app


@pytest.fixture()
def client(rates_app: FastAPI) -> Iterator[TestClient]:
    with TestClient(rates_app) as test_client:
        yield test_client


def _exchange(client: TestClient, actor: str = "admin_u") -> dict[str, str]:
    response = client.post(
        "/api/control/admin/session/exchange",
        json={"credential": issue_exchange_credential(actor, ttl_seconds=3600)},
    )
    assert response.status_code == 201, response.text
    return {ADMIN_CSRF_HEADER: response.json()["csrf_token"]}


@pytest.fixture()
def admin_headers(client: TestClient) -> dict[str, str]:
    return _exchange(client)


@pytest.fixture()
def auditor_headers(client: TestClient) -> dict[str, str]:
    return _exchange(client, "auditor_u")


def _write_headers(base: dict[str, str], *, key: str | None = None) -> dict[str, str]:
    headers = dict(base)
    headers[IDEMPOTENCY_KEY_HEADER] = key or f"key-{uuid.uuid4()}"
    return headers


def test_get_rates_requires_admin_session(client: TestClient) -> None:
    response = client.get("/api/control/settings/rates")
    assert response.status_code == 401


def test_get_rates_returns_seeded_defaults(
    admin_headers: dict[str, str], client: TestClient
) -> None:
    response = client.get("/api/control/settings/rates", headers=admin_headers)
    assert response.status_code == 200, response.text
    payload = response.json()
    subjects = {rate["subject"]: rate for rate in payload["rates"]}
    assert len(payload["rates"]) == 9
    assert subjects["video_generation_768p"]["unit_price_fen"] == 9
    assert subjects["video_generation_768p"]["unit"] == "second"
    assert subjects["video_generation_2k"]["unit_price_fen"] == 15
    assert subjects["first_frame_image"]["unit"] == "image"
    assert subjects["context_ir"]["unit"] == "call"
    assert subjects["external_price_768p"]["unit_price_fen"] == 12
    assert subjects["external_price_768p"]["kind"] == "external_price"
    # 尚无调整时历史为空。
    assert payload["history"] == []


def test_put_rates_requires_the_write_contract(
    admin_headers: dict[str, str], client: TestClient
) -> None:
    base_payload = {
        "updates": [{"subject": "video_generation_768p", "unit_price_fen": 10}],
    }
    # 缺 Idempotency-Key
    response = client.put("/api/control/settings/rates", json=base_payload, headers=admin_headers)
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"
    # 缺 confirm
    response = client.put(
        "/api/control/settings/rates",
        json={**base_payload, "reason": "调价"},
        headers=_write_headers(admin_headers),
    )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "CONFIRMATION_REQUIRED"
    # 缺 reason
    response = client.put(
        "/api/control/settings/rates",
        json={**base_payload, "confirm": True},
        headers=_write_headers(admin_headers),
    )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "REASON_REQUIRED"


def test_put_rates_rejects_auditor(auditor_headers: dict[str, str], client: TestClient) -> None:
    response = client.put(
        "/api/control/settings/rates",
        json={
            "updates": [{"subject": "video_generation_768p", "unit_price_fen": 10}],
            "confirm": True,
            "reason": "审计员不应能调价",
        },
        headers=_write_headers(auditor_headers),
    )
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "AUDITOR_READ_ONLY"


def test_put_rates_rejects_unknown_subject(
    admin_headers: dict[str, str], client: TestClient
) -> None:
    response = client.put(
        "/api/control/settings/rates",
        json={
            "updates": [{"subject": "made_up_subject", "unit_price_fen": 10}],
            "confirm": True,
            "reason": "测试未知科目",
        },
        headers=_write_headers(admin_headers),
    )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "RATE_SUBJECT_UNKNOWN"


def test_put_rates_rejects_out_of_bounds_price(
    admin_headers: dict[str, str], client: TestClient
) -> None:
    response = client.put(
        "/api/control/settings/rates",
        json={
            "updates": [{"subject": "video_generation_768p", "unit_price_fen": -1}],
            "confirm": True,
            "reason": "负价",
        },
        headers=_write_headers(admin_headers),
    )
    assert response.status_code == 422


def test_put_rates_updates_prices_and_writes_old_new_audit(
    admin_headers: dict[str, str], client: TestClient, rates_pg_dsn: str
) -> None:
    response = client.put(
        "/api/control/settings/rates",
        json={
            "updates": [
                {"subject": "video_generation_768p", "unit_price_fen": 10},
                {"subject": "external_price_768p", "unit_price_fen": 15},
            ],
            "confirm": True,
            "reason": "上游调价同步",
        },
        headers=_write_headers(admin_headers, key="w10-update-key-1"),
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    subjects = {rate["subject"]: rate for rate in payload["rates"]}
    assert subjects["video_generation_768p"]["unit_price_fen"] == 10
    assert subjects["video_generation_768p"]["updated_by_username"] == "admin_u"
    assert subjects["external_price_768p"]["unit_price_fen"] == 15

    # 历史变更：两条，各带 old→new 与原因。
    history = {entry["subject"]: entry for entry in payload["history"]}
    assert history["video_generation_768p"]["old_unit_price_fen"] == 9
    assert history["video_generation_768p"]["new_unit_price_fen"] == 10
    assert history["video_generation_768p"]["reason"] == "上游调价同步"
    assert history["video_generation_768p"]["actor_username"] == "admin_u"
    assert history["external_price_768p"]["old_unit_price_fen"] == 12
    assert history["external_price_768p"]["new_unit_price_fen"] == 15

    # 同幂等键重放返回同一快照，不重复写审计。
    replay = client.put(
        "/api/control/settings/rates",
        json={
            "updates": [
                {"subject": "video_generation_768p", "unit_price_fen": 10},
                {"subject": "external_price_768p", "unit_price_fen": 15},
            ],
            "confirm": True,
            "reason": "上游调价同步",
        },
        headers=_write_headers(admin_headers, key="w10-update-key-1"),
    )
    assert replay.status_code == 200
    with psycopg.connect(rates_pg_dsn) as conn:
        audit_rows = conn.execute(
            "SELECT count(*) FROM audit_logs WHERE action = 'operation_rate.update'"
        ).fetchone()
        assert int(audit_rows[0]) == 2
