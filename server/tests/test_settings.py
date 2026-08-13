from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.db import connect_database, initialize_database
from app.settings import SettingsKeyMissing, SettingsRepository
from app.settings_routes import ProviderTestResult, get_database, get_provider_tester, router


@pytest.fixture()
def settings_key(monkeypatch: pytest.MonkeyPatch) -> str:
    key = Fernet.generate_key().decode("ascii")
    monkeypatch.setenv("VIDEO_REPLICA_SETTINGS_KEY", key)
    return key


@pytest.fixture()
def db_path(tmp_path: Path, settings_key: str) -> Path:
    db_path = tmp_path / "settings.db"
    with initialize_database(db_path) as connection:
        seed_users(connection)
    return db_path


@pytest.fixture()
def conn(db_path: Path) -> Iterator[sqlite3.Connection]:
    with connect_database(db_path) as connection:
        yield connection


@pytest.fixture()
def client(db_path: Path) -> Iterator[TestClient]:
    app = FastAPI()
    app.include_router(router)

    def override_database() -> Iterator[sqlite3.Connection]:
        with connect_database(db_path) as connection:
            yield connection

    app.dependency_overrides[get_database] = override_database
    tester = FakeProviderTester()
    app.dependency_overrides[get_provider_tester] = lambda: tester
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


class FakeProviderTester:
    def connection_test(self, provider: str, config: dict[str, str]) -> ProviderTestResult:
        return ProviderTestResult(status="ok", provider=provider, test_kind="connection")

    def paid_test(self, provider: str, config: dict[str, str]) -> ProviderTestResult:
        return ProviderTestResult(status="ok", provider=provider, test_kind="paid")


def seed_users(conn: sqlite3.Connection) -> None:
    conn.executemany(
        """
        INSERT INTO users (id, username, display_name, role)
        VALUES (?, ?, ?, ?)
        """,
        [
            ("admin_1", "admin_1", "Admin One", "admin"),
            ("employee_1", "employee_1", "Employee One", "employee"),
        ],
    )
    conn.commit()


def admin_headers(extra: dict[str, str] | None = None) -> dict[str, str]:
    headers = {"X-Dev-User-Id": "admin_1"}
    if extra:
        headers.update(extra)
    return headers


def test_settings_migration_creates_tables_and_defaults(tmp_path: Path, settings_key: str) -> None:
    db_path = tmp_path / "settings.db"

    with initialize_database(db_path) as conn:
        version = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            ).fetchall()
        }
        runtime = conn.execute(
            """
            SELECT max_generation_count_per_batch, max_concurrent_h3_tasks
            FROM runtime_settings
            WHERE id = 1
            """
        ).fetchone()

    assert version == "002_settings"
    assert {"provider_settings", "runtime_settings"}.issubset(tables)
    assert dict(runtime) == {
        "max_generation_count_per_batch": 4,
        "max_concurrent_h3_tasks": 2,
    }


def test_master_key_must_come_from_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("VIDEO_REPLICA_SETTINGS_KEY", raising=False)
    with initialize_database(tmp_path / "settings.db") as conn:
        with pytest.raises(SettingsKeyMissing):
            SettingsRepository(conn)


def test_provider_config_is_encrypted_at_rest_and_masked_on_read(
    conn: sqlite3.Connection,
) -> None:
    repo = SettingsRepository(conn)

    repo.save_provider_config(
        "metaso",
        {"api_key": "metaso-secret-token", "base_url": "https://metaso.example/api"},
        actor_user_id="admin_1",
    )

    stored = conn.execute(
        "SELECT encrypted_config FROM provider_settings WHERE provider = ?",
        ("metaso",),
    ).fetchone()["encrypted_config"]
    masked = repo.read_provider_config("metaso")
    actual = repo.load_provider_config("metaso")

    assert "metaso-secret-token" not in stored
    assert masked == {
        "provider": "metaso",
        "configured": True,
        "config": {"api_key": "********oken", "base_url": "https://metaso.example/api"},
    }
    assert actual == {"api_key": "metaso-secret-token", "base_url": "https://metaso.example/api"}


@pytest.mark.parametrize(
    ("provider", "config"),
    [
        ("apilio", {"api_key": "apilio-key", "base_url": "https://apilio.example/api"}),
        ("metaso", {"api_key": "metaso-key", "base_url": "https://metaso.example/api"}),
        (
            "cos",
            {
                "access_key_id": "cos-id",
                "secret_access_key": "cos-secret",
                "bucket": "video-private",
                "region": "ap-shanghai",
                "endpoint": "https://cos.example",
            },
        ),
        (
            "oss",
            {
                "access_key_id": "oss-id",
                "secret_access_key": "oss-secret",
                "bucket": "video-private",
                "region": "cn-shanghai",
                "endpoint": "https://oss.example",
            },
        ),
    ],
)
def test_provider_minimum_required_fields_are_accepted(
    conn: sqlite3.Connection,
    provider: str,
    config: dict[str, str],
) -> None:
    repo = SettingsRepository(conn)

    repo.save_provider_config(provider, config, actor_user_id="admin_1")

    assert repo.read_provider_config(provider)["configured"] is True


@pytest.mark.parametrize("provider", ["apilio", "metaso"])
def test_model_provider_uses_default_base_url_when_only_api_key_is_supplied(
    conn: sqlite3.Connection,
    provider: str,
) -> None:
    repo = SettingsRepository(conn)

    repo.save_provider_config(provider, {"api_key": "provider-key"}, actor_user_id="admin_1")

    assert repo.read_provider_config(provider)["configured"] is True


def test_missing_provider_required_field_is_rejected(conn: sqlite3.Connection) -> None:
    repo = SettingsRepository(conn)

    with pytest.raises(ValueError, match="missing required setting"):
        repo.save_provider_config("oss", {"bucket": "video-private"}, actor_user_id="admin_1")


def test_runtime_limits_are_saved_and_validated(conn: sqlite3.Connection) -> None:
    repo = SettingsRepository(conn)

    repo.save_runtime_settings(
        max_generation_count_per_batch=8,
        max_concurrent_h3_tasks=3,
        actor_user_id="admin_1",
    )

    assert repo.read_runtime_settings() == {
        "max_generation_count_per_batch": 8,
        "max_concurrent_h3_tasks": 3,
    }
    with pytest.raises(ValueError, match="max_generation_count_per_batch"):
        repo.save_runtime_settings(
            max_generation_count_per_batch=0,
            max_concurrent_h3_tasks=3,
            actor_user_id="admin_1",
        )


def test_employee_cannot_update_or_read_settings(client: TestClient) -> None:
    headers = {"X-Dev-User-Id": "employee_1"}

    read_response = client.get("/api/admin/settings", headers=headers)
    update_response = client.put(
        "/api/admin/settings/providers/metaso",
        headers=headers,
        json={"config": {"api_key": "secret", "base_url": "https://metaso.example/api"}},
    )

    assert read_response.status_code == 403
    assert update_response.status_code == 403
    assert read_response.json()["detail"]["code"] == "ROLE_FORBIDDEN"
    assert update_response.json()["detail"]["code"] == "ROLE_FORBIDDEN"


def test_admin_updates_settings_without_echoing_secret_or_authorization(
    client: TestClient,
    conn: sqlite3.Connection,
) -> None:
    response = client.put(
        "/api/admin/settings/providers/metaso",
        headers=admin_headers({"Authorization": "Bearer must-not-be-stored"}),
        json={
            "config": {"api_key": "metaso-secret-token", "base_url": "https://metaso.example/api"}
        },
    )

    assert response.status_code == 200
    assert response.json()["config"]["api_key"] == "********oken"
    assert "metaso-secret-token" not in response.text

    audit_metadata = conn.execute("SELECT metadata_json FROM audit_logs").fetchone()[0]
    assert "Authorization" not in audit_metadata
    assert "must-not-be-stored" not in audit_metadata


def test_admin_can_update_runtime_limits(client: TestClient) -> None:
    response = client.patch(
        "/api/admin/settings/runtime",
        headers=admin_headers(),
        json={"max_generation_count_per_batch": 6, "max_concurrent_h3_tasks": 4},
    )

    assert response.status_code == 200
    assert response.json() == {
        "max_generation_count_per_batch": 6,
        "max_concurrent_h3_tasks": 4,
    }


def test_connection_test_and_paid_test_are_separate_interfaces(client: TestClient) -> None:
    client.put(
        "/api/admin/settings/providers/metaso",
        headers=admin_headers(),
        json={
            "config": {"api_key": "metaso-secret-token", "base_url": "https://metaso.example/api"}
        },
    )

    connection = client.post(
        "/api/admin/settings/providers/metaso/connection-test",
        headers=admin_headers(),
    )
    paid = client.post(
        "/api/admin/settings/providers/metaso/paid-test",
        headers=admin_headers(),
    )

    assert connection.status_code == 200
    assert paid.status_code == 200
    assert connection.json() == {"status": "ok", "provider": "metaso", "test_kind": "connection"}
    assert paid.json() == {"status": "ok", "provider": "metaso", "test_kind": "paid"}
