from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.db import connect_database, initialize_database
from app.settings import (
    SettingsDecryptError,
    SettingsKeyMissing,
    SettingsRepository,
    fernet_from_environment,
)
from app.settings_routes import (
    NoopProviderTester,
    ProviderTestResult,
    StorageProviderTester,
    configured_only_message,
    get_database,
    get_provider_tester,
    router,
)
from app.storage import (
    CloudStorageAdapter,
    CloudStorageConfig,
    FakeStorageAdapter,
    StorageBackendUnavailable,
)


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
            ("auditor_1", "auditor_1", "Auditor One", "auditor"),
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
            SELECT max_generation_count_per_batch, max_concurrent_h3_tasks, active_storage_provider
            FROM runtime_settings
            WHERE id = 1
            """
        ).fetchone()

    assert version == "029_customer_sessions_and_idempotency"
    assert {"provider_settings", "runtime_settings"}.issubset(tables)
    assert dict(runtime) == {
        "max_generation_count_per_batch": 4,
        "max_concurrent_h3_tasks": 2,
        "active_storage_provider": "cos",
    }


def test_master_key_must_come_from_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("VIDEO_REPLICA_SETTINGS_KEY", raising=False)
    with initialize_database(tmp_path / "settings.db") as conn:
        with pytest.raises(SettingsKeyMissing):
            SettingsRepository(conn)


def test_master_key_uses_local_secure_store_when_environment_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = Fernet.generate_key().decode("ascii")
    monkeypatch.delenv("VIDEO_REPLICA_SETTINGS_KEY", raising=False)
    monkeypatch.delenv("VIDEO_REPLICA_DISABLE_LOCAL_KEYSTORE", raising=False)
    monkeypatch.setattr("app.settings.load_or_create_local_settings_key", lambda: key)

    fernet = fernet_from_environment()
    payload = b"persistent settings"

    assert fernet.decrypt(fernet.encrypt(payload)) == payload


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


def test_provider_config_survives_database_reopen(
    db_path: Path,
    settings_key: str,
) -> None:
    with connect_database(db_path) as conn:
        SettingsRepository(conn).save_provider_config(
            "cos",
            {
                "access_key_id": "cos-id",
                "secret_access_key": "cos-secret",
                "bucket": "video-private",
                "region": "ap-shanghai",
            },
            actor_user_id="admin_1",
        )

    with connect_database(db_path) as reopened:
        stored = SettingsRepository(reopened).load_provider_config("cos")

    assert settings_key
    assert stored == {
        "access_key_id": "cos-id",
        "secret_access_key": "cos-secret",
        "bucket": "video-private",
        "region": "ap-shanghai",
    }


def test_provider_config_remains_encrypted_when_reopened_with_the_wrong_key(
    db_path: Path,
) -> None:
    with connect_database(db_path) as conn:
        SettingsRepository(conn).save_provider_config(
            "metaso",
            {"api_key": "metaso-secret"},
            actor_user_id="admin_1",
        )

    with connect_database(db_path) as reopened:
        with pytest.raises(SettingsDecryptError, match="cannot be decrypted"):
            SettingsRepository(reopened, fernet=Fernet(Fernet.generate_key())).load_provider_config(
                "metaso"
            )

        assert (
            reopened.execute(
                "SELECT COUNT(*) FROM provider_settings WHERE provider = 'metaso'"
            ).fetchone()[0]
            == 1
        )


def test_settings_api_reports_wrong_key_without_deleting_saved_config(
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with connect_database(db_path) as conn:
        SettingsRepository(conn).save_provider_config(
            "metaso",
            {"api_key": "metaso-secret"},
            actor_user_id="admin_1",
        )

    monkeypatch.setenv(
        "VIDEO_REPLICA_SETTINGS_KEY",
        Fernet.generate_key().decode("ascii"),
    )
    from app.main import app as production_app

    def override_database() -> Iterator[sqlite3.Connection]:
        with connect_database(db_path) as connection:
            yield connection

    production_app.dependency_overrides[get_database] = override_database
    try:
        with TestClient(production_app, raise_server_exceptions=False) as api:
            response = api.get("/api/admin/settings", headers=admin_headers())
    finally:
        production_app.dependency_overrides.clear()

    assert response.status_code == 503
    assert response.json() == {
        "detail": {
            "code": "SETTINGS_CONFIGURATION_UNAVAILABLE",
            "message": "本地配置仍保存在数据库中，但当前主密钥缺失或不匹配；系统未覆盖已保存配置。",
        }
    }
    with connect_database(db_path) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM provider_settings WHERE provider = 'metaso'"
            ).fetchone()[0]
            == 1
        )


def test_bootstrap_persists_an_explicit_key_only_after_saved_settings_decrypt(
    db_path: Path,
    settings_key: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.bootstrap import bootstrap_runtime

    monkeypatch.delenv("VIDEO_REPLICA_DISABLE_LOCAL_KEYSTORE", raising=False)

    with connect_database(db_path) as conn:
        SettingsRepository(conn).save_provider_config(
            "metaso",
            {"api_key": "metaso-secret"},
            actor_user_id="admin_1",
        )

    persisted: list[str] = []
    monkeypatch.setattr("app.bootstrap.persist_local_settings_key", persisted.append)

    bootstrap_runtime(db_path)

    assert persisted == [settings_key]

    persisted.clear()
    monkeypatch.setenv("VIDEO_REPLICA_SETTINGS_KEY", Fernet.generate_key().decode("ascii"))
    with pytest.raises(SettingsDecryptError):
        bootstrap_runtime(db_path)
    assert persisted == []

    monkeypatch.setenv("VIDEO_REPLICA_SETTINGS_KEY", settings_key)
    monkeypatch.setenv("VIDEO_REPLICA_DISABLE_LOCAL_KEYSTORE", "1")
    bootstrap_runtime(db_path)
    assert persisted == []


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


def test_apilio_can_keep_a_dedicated_analysis_key_separate_from_its_image_key(
    conn: sqlite3.Connection,
) -> None:
    repo = SettingsRepository(conn)
    repo.save_provider_config(
        "apilio",
        {"api_key": "image-key", "analysis_api_key": "analysis-key"},
        actor_user_id="admin_1",
    )

    assert repo.load_provider_config("apilio") == {
        "api_key": "image-key",
        "analysis_api_key": "analysis-key",
    }
    assert repo.read_provider_config("apilio")["config"]["analysis_api_key"] == "********-key"


def test_apilio_allows_an_analysis_key_before_an_image_key(
    conn: sqlite3.Connection,
) -> None:
    repo = SettingsRepository(conn)

    saved = repo.save_provider_config(
        "apilio",
        {"analysis_api_key": "analysis-key"},
        actor_user_id="admin_1",
    )

    assert saved["configured"] is True
    assert repo.load_provider_config("apilio") == {"analysis_api_key": "analysis-key"}


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
        repo.save_provider_config("cos", {"bucket": "video-private"}, actor_user_id="admin_1")


def test_oss_provider_is_rejected(conn: sqlite3.Connection) -> None:
    repo = SettingsRepository(conn)

    with pytest.raises(ValueError, match="unsupported provider: oss"):
        repo.save_provider_config(
            "oss",
            {
                "access_key_id": "oss-id",
                "secret_access_key": "oss-secret",
                "bucket": "video-private",
                "endpoint": "https://oss.example",
            },
            actor_user_id="admin_1",
        )


def test_runtime_limits_are_saved_and_validated(conn: sqlite3.Connection) -> None:
    repo = SettingsRepository(conn)

    repo.save_runtime_settings(
        max_generation_count_per_batch=8,
        max_concurrent_h3_tasks=3,
        active_storage_provider="cos",
        actor_user_id="admin_1",
    )

    assert repo.read_runtime_settings() == {
        "max_generation_count_per_batch": 8,
        "max_concurrent_h3_tasks": 3,
        "active_storage_provider": "cos",
    }
    with pytest.raises(ValueError, match="max_generation_count_per_batch"):
        repo.save_runtime_settings(
            max_generation_count_per_batch=0,
            max_concurrent_h3_tasks=3,
            active_storage_provider="cos",
            actor_user_id="admin_1",
        )


def test_local_storage_provider_is_allowed(conn: sqlite3.Connection) -> None:
    repo = SettingsRepository(conn)
    repo.save_runtime_settings(
        max_generation_count_per_batch=4,
        max_concurrent_h3_tasks=2,
        active_storage_provider="local",
        actor_user_id="admin_1",
    )
    assert repo.read_runtime_settings()["active_storage_provider"] == "local"


def test_oss_runtime_provider_is_rejected(conn: sqlite3.Connection) -> None:
    repo = SettingsRepository(conn)

    with pytest.raises(ValueError, match="active_storage_provider must be cos or local"):
        repo.save_runtime_settings(
            max_generation_count_per_batch=4,
            max_concurrent_h3_tasks=2,
            active_storage_provider="oss",
            actor_user_id="admin_1",
        )


@pytest.mark.parametrize(
    ("user_id", "expected_status"),
    [
        ("admin_1", 200),
        ("employee_1", 403),
        ("auditor_1", 403),
    ],
)
def test_settings_read_uses_admin_role_matrix(
    client: TestClient,
    conn: sqlite3.Connection,
    user_id: str,
    expected_status: int,
) -> None:
    response = client.get("/api/admin/settings", headers={"X-Dev-User-Id": user_id})

    assert response.status_code == expected_status
    if expected_status == 403:
        assert response.json()["detail"]["code"] == "ROLE_FORBIDDEN"
        audit = conn.execute(
            """
            SELECT action, actor_user_id, entity_type, entity_id, metadata_json
            FROM audit_logs
            WHERE action = 'security.role_denied'
            ORDER BY rowid DESC
            LIMIT 1
            """
        ).fetchone()
        assert audit is not None
        assert dict(audit)["actor_user_id"] == user_id
        assert dict(audit)["entity_type"] == "settings"
    else:
        assert "providers" in response.json()


@pytest.mark.parametrize(
    ("user_id", "expected_status"),
    [
        ("admin_1", 200),
        ("employee_1", 403),
        ("auditor_1", 403),
    ],
)
def test_settings_update_uses_admin_role_matrix(
    client: TestClient,
    user_id: str,
    expected_status: int,
) -> None:
    response = client.put(
        "/api/admin/settings/providers/metaso",
        headers={"X-Dev-User-Id": user_id},
        json={"config": {"api_key": "secret", "base_url": "https://metaso.example/api"}},
    )

    assert response.status_code == expected_status
    if expected_status == 403:
        assert response.json()["detail"]["code"] == "ROLE_FORBIDDEN"
    else:
        assert response.json()["configured"] is True


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


def test_admin_can_update_a_non_secret_field_without_reentering_a_saved_secret(
    client: TestClient,
    conn: sqlite3.Connection,
) -> None:
    repo = SettingsRepository(conn)
    repo.save_provider_config(
        "cos",
        {
            "access_key_id": "cos-id",
            "secret_access_key": "cos-secret",
            "bucket": "before-update",
            "region": "ap-shanghai",
        },
        actor_user_id="admin_1",
    )

    response = client.put(
        "/api/admin/settings/providers/cos",
        headers=admin_headers(),
        json={"config": {"bucket": "after-update"}},
    )

    assert response.status_code == 200
    assert repo.load_provider_config("cos") == {
        "access_key_id": "cos-id",
        "secret_access_key": "cos-secret",
        "bucket": "after-update",
        "region": "ap-shanghai",
    }


def test_empty_optional_field_can_be_cleared_without_overwriting_a_secret(
    client: TestClient,
    conn: sqlite3.Connection,
) -> None:
    repo = SettingsRepository(conn)
    repo.save_provider_config(
        "metaso",
        {"api_key": "metaso-secret-token", "base_url": "https://old.example/api"},
        actor_user_id="admin_1",
    )

    response = client.put(
        "/api/admin/settings/providers/metaso",
        headers=admin_headers(),
        json={"config": {"api_key": "", "base_url": ""}},
    )

    assert response.status_code == 200
    assert repo.load_provider_config("metaso") == {"api_key": "metaso-secret-token"}


def test_admin_can_update_runtime_limits(client: TestClient) -> None:
    response = client.patch(
        "/api/admin/settings/runtime",
        headers=admin_headers(),
        json={
            "max_generation_count_per_batch": 6,
            "max_concurrent_h3_tasks": 4,
            "active_storage_provider": "cos",
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "max_generation_count_per_batch": 6,
        "max_concurrent_h3_tasks": 4,
        "active_storage_provider": "cos",
    }


def test_admin_can_read_and_update_internal_billing_settings(client: TestClient) -> None:
    initial = client.get("/api/admin/settings", headers=admin_headers())
    updated = client.patch(
        "/api/admin/settings/billing",
        headers=admin_headers(),
        json={
            "internal_base_unit_price_fen": 1000,
            "min_recharge_fen": 20000,
            "recharge_step_fen": 2000,
        },
    )

    assert initial.status_code == 200
    assert initial.json()["billing"] == {
        "internal_base_unit_price_fen": 1000,
        "charged_unit_price_fen": 1000,
        "min_recharge_fen": 10000,
        "recharge_step_fen": 1000,
    }
    assert updated.status_code == 200
    assert updated.json() == {
        "internal_base_unit_price_fen": 1000,
        "charged_unit_price_fen": 1000,
        "min_recharge_fen": 20000,
        "recharge_step_fen": 2000,
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("internal_base_unit_price_fen", True),
        ("min_recharge_fen", "10000"),
        ("recharge_step_fen", 1000.0),
    ],
)
def test_billing_settings_api_rejects_coerced_integer_values(
    client: TestClient,
    field: str,
    value: object,
) -> None:
    payload: dict[str, object] = {
        "internal_base_unit_price_fen": 1000,
        "min_recharge_fen": 10000,
        "recharge_step_fen": 1000,
    }
    payload[field] = value

    response = client.patch(
        "/api/admin/settings/billing",
        headers=admin_headers(),
        json=payload,
    )

    assert response.status_code == 422


def test_admin_cannot_enable_removed_oss_storage(client: TestClient) -> None:
    response = client.patch(
        "/api/admin/settings/runtime",
        headers=admin_headers(),
        json={
            "max_generation_count_per_batch": 6,
            "max_concurrent_h3_tasks": 4,
            "active_storage_provider": "oss",
        },
    )

    assert response.status_code == 422


@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [
        ("put", "/api/admin/settings/providers/oss", {"config": {"bucket": "removed"}}),
        ("post", "/api/admin/settings/providers/oss/connection-test", None),
        ("post", "/api/admin/settings/providers/oss/paid-test", None),
    ],
)
def test_removed_oss_provider_routes_return_validation_error(
    client: TestClient,
    method: str,
    path: str,
    payload: dict[str, object] | None,
) -> None:
    response = client.request(method, path, headers=admin_headers(), json=payload)

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "UNSUPPORTED_PROVIDER"


def test_admin_can_enable_local_storage_via_api(client: TestClient) -> None:
    response = client.patch(
        "/api/admin/settings/runtime",
        headers=admin_headers(),
        json={
            "max_generation_count_per_batch": 4,
            "max_concurrent_h3_tasks": 2,
            "active_storage_provider": "local",
        },
    )

    assert response.status_code == 200
    assert response.json()["active_storage_provider"] == "local"


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


def test_admin_can_generate_and_download_a_redacted_settings_diagnostic_log(
    client: TestClient,
) -> None:
    client.put(
        "/api/admin/settings/providers/metaso",
        headers=admin_headers(),
        json={"config": {"api_key": "metaso-secret-token"}},
    )

    response = client.post("/api/admin/settings/diagnostic-test", headers=admin_headers())

    assert response.status_code == 200
    report = response.json()
    assert report["status"] == "attention"
    assert report["generated_at"].endswith("+00:00")
    assert report["download_url"].startswith("/api/admin/settings/diagnostic-reports/")
    assert {item["provider"] for item in report["providers"]} == {
        "apilio",
        "metaso",
        "cos",
        "deepseek",
    }
    deepseek = next(item for item in report["providers"] if item["provider"] == "deepseek")
    assert deepseek["status"] == "not_configured"
    metaso = next(item for item in report["providers"] if item["provider"] == "metaso")
    assert metaso["status"] == "ok"
    assert metaso["configured_fields"] == ["api_key"]
    assert metaso["adapter_capability"] == "connection_test"
    assert metaso["test_kind"] == "connection"
    assert isinstance(metaso["latency_ms"], int)

    download = client.get(report["download_url"], headers=admin_headers())

    assert download.status_code == 200
    assert download.headers["content-type"].startswith("application/json")
    assert "attachment" in download.headers["content-disposition"]
    assert "metaso-secret-token" not in download.text
    assert "api_key" in download.text


def test_configuration_only_model_checks_do_not_mark_a_complete_report_as_attention(
    client: TestClient,
) -> None:
    client.put(
        "/api/admin/settings/providers/metaso",
        headers=admin_headers(),
        json={"config": {"api_key": "metaso-secret-token"}},
    )
    client.put(
        "/api/admin/settings/providers/apilio",
        headers=admin_headers(),
        json={"config": {"analysis_api_key": "analysis-secret-token"}},
    )
    client.put(
        "/api/admin/settings/providers/cos",
        headers=admin_headers(),
        json={
            "config": {
                "access_key_id": "cos-id",
                "secret_access_key": "cos-secret",
                "bucket": "video-private",
                "region": "ap-shanghai",
            }
        },
    )
    client.put(
        "/api/admin/settings/providers/deepseek",
        headers=admin_headers(),
        json={"config": {"api_key": "deepseek-secret-token"}},
    )
    storage = FakeStorageAdapter(provider="cos", bucket="video-private")
    client.app.dependency_overrides[get_provider_tester] = lambda: StorageProviderTester(
        storage_factory=lambda _: storage
    )

    response = client.post("/api/admin/settings/diagnostic-test", headers=admin_headers())

    assert response.status_code == 200
    report = response.json()
    assert report["status"] == "ok"
    assert {item["provider"]: item["status"] for item in report["providers"]} == {
        "metaso": "configured_only",
        "apilio": "configured_only",
        "cos": "ok",
        "deepseek": "configured_only",
    }


def test_diagnostic_log_keeps_http_error_codes_but_not_provider_error_details(
    client: TestClient,
) -> None:
    class FailingProviderTester:
        def connection_test(self, provider: str, config: dict[str, str]) -> ProviderTestResult:
            raise HTTPException(
                status_code=429,
                detail={
                    "code": "RATE_LIMITED",
                    "failure_phase": "put",
                    "message": "sensitive provider response",
                },
            )

        def paid_test(self, provider: str, config: dict[str, str]) -> ProviderTestResult:
            raise AssertionError("not called")

    client.app.dependency_overrides[get_provider_tester] = FailingProviderTester
    client.put(
        "/api/admin/settings/providers/metaso",
        headers=admin_headers(),
        json={"config": {"api_key": "metaso-secret-token"}},
    )

    response = client.post("/api/admin/settings/diagnostic-test", headers=admin_headers())
    metaso = next(item for item in response.json()["providers"] if item["provider"] == "metaso")

    assert metaso["status"] == "error"
    assert metaso["http_status"] == 429
    assert metaso["error_code"] == "RATE_LIMITED"
    assert metaso["failure_phase"] == "put"
    assert "sensitive provider response" not in response.text


def test_default_provider_tester_never_claims_real_connectivity_or_paid_access() -> None:
    tester = NoopProviderTester()

    assert tester.connection_test("metaso", {"api_key": "configured"}).status == "configured_only"
    with pytest.raises(HTTPException) as error:
        tester.paid_test("metaso", {"api_key": "configured"})

    assert error.value.status_code == 501
    detail = cast(dict[str, str], error.value.detail)
    assert detail["code"] == "PROVIDER_TEST_NOT_IMPLEMENTED"


@pytest.mark.parametrize(
    ("provider", "expected"),
    [
        (
            "metaso",
            "H3 参数已保存。测试设置不会提交会产生费用的生成任务；实际任务由生成 Worker 处理。",
        ),
        (
            "apilio",
            "模型服务参数已保存。视频拆解和首帧任务会按需调用；测试设置不会发起计费模型请求。",
        ),
    ],
)
def test_model_provider_configuration_status_explains_the_real_task_path(
    provider: str, expected: str
) -> None:
    assert configured_only_message(provider) == expected


def test_storage_provider_tester_runs_a_recoverable_cos_connection_check() -> None:
    class CapturingStorage(FakeStorageAdapter):
        deleted_key: str | None = None

        def delete_object(self, key: str, *, actor_id: str | None = None) -> None:
            self.deleted_key = key
            super().delete_object(key, actor_id=actor_id)

    storage = CapturingStorage(provider="cos", bucket="video-private")
    tester = StorageProviderTester(storage_factory=lambda _: storage)

    result = tester.connection_test(
        "cos",
        {
            "access_key_id": "cos-id",
            "secret_access_key": "cos-secret",
            "bucket": "video-private",
            "region": "ap-shanghai",
        },
    )

    assert result == ProviderTestResult(status="ok", provider="cos", test_kind="storage_connection")
    assert storage.deleted_key is not None
    assert storage.deleted_key.startswith("projects/settings-diagnostics/")
    assert storage.head_object(storage.deleted_key) is None
    assert [event.action for event in storage.audit_events] == ["object.deleted"]


@pytest.mark.parametrize("provider", ["metaso", "apilio"])
def test_storage_provider_tester_keeps_model_provider_checks_non_billing(provider: str) -> None:
    result = StorageProviderTester().connection_test(provider, {"api_key": "configured"})

    assert result == ProviderTestResult(
        status="configured_only", provider=provider, test_kind="connection"
    )


def test_storage_provider_tester_redacts_cloud_failure_from_connection_result() -> None:
    class FailingStorage(FakeStorageAdapter):
        def put_object(self, key: str, content: bytes, *, content_type: str):  # type: ignore[no-untyped-def]
            del key, content, content_type
            raise StorageBackendUnavailable("provider returned sensitive detail")

    tester = StorageProviderTester(
        storage_factory=lambda _: FailingStorage(provider="cos", bucket="video-private")
    )

    with pytest.raises(HTTPException) as error:
        tester.connection_test(
            "cos",
            {
                "access_key_id": "cos-id",
                "secret_access_key": "cos-secret",
                "bucket": "video-private",
                "region": "ap-shanghai",
            },
        )

    assert error.value.status_code == 503
    assert error.value.detail == {
        "code": "STORAGE_CONNECTION_TEST_FAILED",
        "cleanup_failed": False,
        "failure_phase": "put",
        "message": "对象存储连接测试失败；请运行测试设置并查看本地服务日志。",
    }


def test_storage_provider_tester_reports_cleanup_failure_without_provider_details() -> None:
    class FailingCleanupStorage(FakeStorageAdapter):
        def delete_object(self, key: str, *, actor_id: str | None = None) -> None:
            del key, actor_id
            raise StorageBackendUnavailable("provider returned sensitive cleanup detail")

    tester = StorageProviderTester(
        storage_factory=lambda _: FailingCleanupStorage(provider="cos", bucket="video-private")
    )

    with pytest.raises(HTTPException) as error:
        tester.connection_test(
            "cos",
            {
                "access_key_id": "cos-id",
                "secret_access_key": "cos-secret",
                "bucket": "video-private",
                "region": "ap-shanghai",
            },
        )

    assert error.value.status_code == 503
    assert error.value.detail == {
        "code": "STORAGE_CONNECTION_TEST_CLEANUP_FAILED",
        "cleanup_failed": True,
        "failure_phase": "delete",
        "message": "对象存储测试对象清理失败；可能残留测试对象，请检查本地服务日志。",
    }


def test_storage_provider_tester_prioritizes_cleanup_failure_after_verification_error() -> None:
    class FailingVerificationAndCleanupStorage(FakeStorageAdapter):
        def head_object(self, key: str):  # type: ignore[no-untyped-def]
            del key
            raise StorageBackendUnavailable("provider returned sensitive verification detail")

        def delete_object(self, key: str, *, actor_id: str | None = None) -> None:
            del key, actor_id
            raise StorageBackendUnavailable("provider returned sensitive cleanup detail")

    tester = StorageProviderTester(
        storage_factory=lambda _: FailingVerificationAndCleanupStorage(
            provider="cos", bucket="video-private"
        )
    )

    with pytest.raises(HTTPException) as error:
        tester.connection_test(
            "cos",
            {
                "access_key_id": "cos-id",
                "secret_access_key": "cos-secret",
                "bucket": "video-private",
                "region": "ap-shanghai",
            },
        )

    assert error.value.status_code == 503
    assert error.value.detail == {
        "code": "STORAGE_CONNECTION_TEST_CLEANUP_FAILED",
        "cleanup_failed": True,
        "failure_phase": "delete",
        "message": "对象存储测试对象清理失败；可能残留测试对象，请检查本地服务日志。",
    }


def test_storage_provider_tester_preserves_put_failure_when_best_effort_cleanup_fails() -> None:
    class FailingPutAndCleanupStorage(FakeStorageAdapter):
        def put_object(self, key: str, content: bytes, *, content_type: str):  # type: ignore[no-untyped-def]
            del key, content, content_type
            raise StorageBackendUnavailable("provider returned sensitive upload detail")

        def delete_object(self, key: str, *, actor_id: str | None = None) -> None:
            del key, actor_id
            raise StorageBackendUnavailable("provider returned sensitive cleanup detail")

    tester = StorageProviderTester(
        storage_factory=lambda _: FailingPutAndCleanupStorage(
            provider="cos", bucket="video-private"
        )
    )

    with pytest.raises(HTTPException) as error:
        tester.connection_test(
            "cos",
            {
                "access_key_id": "cos-id",
                "secret_access_key": "cos-secret",
                "bucket": "video-private",
                "region": "ap-shanghai",
            },
        )

    assert error.value.status_code == 503
    assert error.value.detail == {
        "code": "STORAGE_CONNECTION_TEST_FAILED",
        "cleanup_failed": True,
        "failure_phase": "put",
        "message": "对象存储连接测试失败，且清理动作失败；可能残留测试对象，请查看本地服务日志。",
    }


def test_update_cos_settings_applies_lifecycle_rules(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """保存 COS 配置后下发桶生命周期规则：视频/项目素材 180 天过期，
    users/（人物图片）不配规则即长期。"""
    calls: list[dict[str, object]] = []

    class RecordingLifecycleClient:
        def put_bucket_lifecycle(self, **kwargs: object) -> None:
            calls.append(cast("dict[str, object]", kwargs))

    def fake_factory(config: object) -> CloudStorageAdapter:
        return CloudStorageAdapter(
            cast("CloudStorageConfig", config), client=RecordingLifecycleClient()
        )

    monkeypatch.setattr("app.settings_routes.create_storage_adapter", fake_factory)

    response = client.put(
        "/api/admin/settings/providers/cos",
        headers=admin_headers(),
        json={
            "config": {
                "access_key_id": "cos-id",
                "secret_access_key": "cos-secret",
                "bucket": "lifecycle-bucket",
                "region": "ap-shanghai",
            }
        },
    )

    assert response.status_code == 200
    assert len(calls) == 1
    assert calls[0]["Bucket"] == "lifecycle-bucket"
    configuration = cast("dict[str, object]", calls[0]["LifecycleConfiguration"])
    rules = cast("list[dict[str, object]]", configuration["Rule"])
    prefix_days = {
        cast("dict[str, object]", rule["Filter"])["Prefix"]: cast(
            "dict[str, object]", rule["Expiration"]
        )["Days"]
        for rule in rules
    }
    assert prefix_days == {"projects/": 180, "generation-results/": 180}
    assert response.json()["lifecycle"]["status"] == "applied"


def test_update_cos_settings_lifecycle_failure_does_not_block_save(
    client: TestClient,
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """生命周期下发失败不得阻断配置保存：状态回 failed、配置落库、审计留痕。"""

    class FailingLifecycleClient:
        def put_bucket_lifecycle(self, **kwargs: object) -> None:
            raise RuntimeError("lifecycle api down")

    monkeypatch.setattr(
        "app.settings_routes.create_storage_adapter",
        lambda config: CloudStorageAdapter(
            cast("CloudStorageConfig", config), client=FailingLifecycleClient()
        ),
    )

    response = client.put(
        "/api/admin/settings/providers/cos",
        headers=admin_headers(),
        json={
            "config": {
                "access_key_id": "cos-id",
                "secret_access_key": "cos-secret",
                "bucket": "lifecycle-bucket",
                "region": "ap-shanghai",
            }
        },
    )

    assert response.status_code == 200
    assert response.json()["lifecycle"]["status"] == "failed"
    repo = SettingsRepository(conn)
    assert repo.load_provider_config("cos")["bucket"] == "lifecycle-bucket"
    audit_actions = [
        row[0]
        for row in conn.execute(
            "SELECT action FROM audit_logs WHERE action LIKE 'cos_lifecycle%'"
        ).fetchall()
    ]
    assert audit_actions == ["cos_lifecycle.failed"]


def test_masked_secret_roundtrip_does_not_overwrite_saved_secret(
    client: TestClient,
    conn: sqlite3.Connection,
) -> None:
    """GET 返回的掩码配置被原样 PUT 回传时，不得把真实凭据覆盖成掩码字符串。"""
    repo = SettingsRepository(conn)
    repo.save_provider_config(
        "cos",
        {
            "access_key_id": "AKID-real-key-id-1234",
            "secret_access_key": "real-secret-value-5678",
            "bucket": "mask-roundtrip",
            "region": "ap-shanghai",
        },
        actor_user_id="admin_1",
    )

    # 模拟前端把 GET 到的掩码 config 原样回传
    masked = repo.read_provider_config("cos")["config"]
    response = client.put(
        "/api/admin/settings/providers/cos",
        headers=admin_headers(),
        json={"config": masked},
    )

    assert response.status_code == 200
    saved = repo.load_provider_config("cos")
    assert saved["access_key_id"] == "AKID-real-key-id-1234"
    assert saved["secret_access_key"] == "real-secret-value-5678"
    assert saved["bucket"] == "mask-roundtrip"
