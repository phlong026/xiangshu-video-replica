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
from app.settings import SettingsKeyMissing, SettingsRepository
from app.settings_routes import (
    NoopProviderTester,
    ProviderTestResult,
    StorageProviderTester,
    configured_only_message,
    get_database,
    get_provider_tester,
    router,
)
from app.storage import FakeStorageAdapter, StorageBackendUnavailable


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
            SELECT max_generation_count_per_batch, max_concurrent_h3_tasks, active_storage_provider
            FROM runtime_settings
            WHERE id = 1
            """
        ).fetchone()

    assert version == "007_local_storage_provider"
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
            },
        ),
        (
            "oss",
            {
                "access_key_id": "oss-id",
                "secret_access_key": "oss-secret",
                "bucket": "video-private",
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
        repo.save_provider_config("oss", {"bucket": "video-private"}, actor_user_id="admin_1")


def test_runtime_limits_are_saved_and_validated(conn: sqlite3.Connection) -> None:
    repo = SettingsRepository(conn)

    repo.save_runtime_settings(
        max_generation_count_per_batch=8,
        max_concurrent_h3_tasks=3,
        active_storage_provider="oss",
        actor_user_id="admin_1",
    )

    assert repo.read_runtime_settings() == {
        "max_generation_count_per_batch": 8,
        "max_concurrent_h3_tasks": 3,
        "active_storage_provider": "oss",
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
            "active_storage_provider": "oss",
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "max_generation_count_per_batch": 6,
        "max_concurrent_h3_tasks": 4,
        "active_storage_provider": "oss",
    }


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
        "oss",
    }
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
