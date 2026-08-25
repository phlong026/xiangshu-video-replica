"""T36 deployment and readiness contracts for customer staging.

These tests deliberately prove the repository-owned topology without claiming
that a laptop is a real HA environment. Live two-API/four-worker/PG-HA/private-
COS evidence is recorded only after the deployment runbook is executed on the
staging hosts.
"""

from __future__ import annotations

import inspect
import json
import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[2]


def _read(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


def test_liveness_and_readiness_are_separate_endpoints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("VIDEO_REPLICA_CUSTOMER_PRODUCTION", raising=False)
    from app.main import app

    with TestClient(app) as client:
        assert client.get("/live").json() == {
            "status": "ok",
            "service": "video-replica-api",
        }
        assert client.get("/ready").json() == {
            "status": "ready",
            "service": "video-replica-api",
            "database": "internal",
            "storage": "local",
        }


def test_customer_runtime_dependency_gate_requires_private_cos(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import bootstrap

    monkeypatch.setenv("VIDEO_REPLICA_CUSTOMER_PRODUCTION", "true")
    ready = SimpleNamespace(pool_size=4)
    monkeypatch.setattr(bootstrap, "check_pg_ready", lambda: ready)

    @contextmanager
    def fake_transaction() -> Any:
        yield SimpleNamespace(row_factory=None)

    class MissingCosRepository:
        def __init__(self, _: object) -> None:
            pass

        def read_runtime_settings(self) -> dict[str, object]:
            return {"active_storage_provider": "cos"}

        def load_provider_config(self, provider: str) -> dict[str, str]:
            assert provider == "cos"
            return {}

    monkeypatch.setattr(bootstrap, "pg_transaction", fake_transaction)
    monkeypatch.setattr(bootstrap, "SettingsRepository", MissingCosRepository)

    def unexpected_storage(_: object) -> None:
        raise AssertionError("storage must not initialize with incomplete COS settings")

    monkeypatch.setattr(bootstrap, "create_storage_adapter", unexpected_storage)

    with pytest.raises(RuntimeError, match="private COS"):
        bootstrap.check_customer_production_runtime_dependencies()


def test_customer_runtime_dependency_gate_accepts_pg_and_cos(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import bootstrap

    monkeypatch.setenv("VIDEO_REPLICA_CUSTOMER_PRODUCTION", "true")
    ready = SimpleNamespace(pool_size=4)
    monkeypatch.setattr(bootstrap, "check_pg_ready", lambda: ready)

    events: list[str] = []

    @contextmanager
    def fake_transaction() -> Any:
        events.append("pg-enter")
        try:
            yield SimpleNamespace(row_factory=None)
        finally:
            events.append("pg-exit")

    class ConfiguredCosRepository:
        def __init__(self, _: object) -> None:
            pass

        def read_runtime_settings(self) -> dict[str, object]:
            return {"active_storage_provider": "cos"}

        def load_provider_config(self, provider: str) -> dict[str, str]:
            assert provider == "cos"
            return {
                "access_key_id": "placeholder-access-id",
                "secret_access_key": "placeholder-secret",
                "bucket": "private-staging-bucket",
                "region": "ap-shanghai",
            }

    monkeypatch.setattr(bootstrap, "pg_transaction", fake_transaction)
    monkeypatch.setattr(bootstrap, "SettingsRepository", ConfiguredCosRepository)

    probes: list[str] = []

    class ReachableStorage:
        def head_object(self, key: str) -> None:
            assert events == ["pg-enter", "pg-exit"]
            events.append("cos-head")
            probes.append(key)

    monkeypatch.setattr(
        bootstrap,
        "create_storage_adapter",
        lambda _: ReachableStorage(),
    )

    assert bootstrap.check_customer_production_runtime_dependencies() is ready
    assert probes == ["health/readiness-probe"]
    assert events == ["pg-enter", "pg-exit", "cos-head"]


def test_customer_readiness_returns_generic_503_without_leaking_dependency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import main

    monkeypatch.setenv("VIDEO_REPLICA_CUSTOMER_PRODUCTION", "true")

    def unavailable() -> None:
        raise RuntimeError("postgresql://user:secret@db/private")

    monkeypatch.setattr(main, "check_customer_production_runtime_dependencies", unavailable)
    assert not inspect.iscoroutinefunction(main.ready)
    response = main.ready()

    assert response.status_code == 503
    assert json.loads(response.body) == {
        "status": "not_ready",
        "service": "video-replica-api",
        "code": "RUNTIME_DEPENDENCY_UNAVAILABLE",
    }
    assert b"secret" not in response.body


def test_direct_customer_worker_fails_before_entering_loop_when_cos_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import bootstrap, generation_worker

    monkeypatch.setattr(sys, "argv", ["generation-worker"])
    monkeypatch.setattr(
        generation_worker,
        "resolve_database_config",
        lambda: SimpleNamespace(mode=generation_worker.DatabaseMode.POSTGRESQL),
    )
    monkeypatch.setattr(generation_worker, "validate_customer_production", lambda _: None)
    monkeypatch.setattr(bootstrap, "assert_customer_production_security", lambda: None)
    monkeypatch.setattr(bootstrap, "is_customer_production", lambda: True)

    def unavailable() -> None:
        raise RuntimeError("customer production requires private COS")

    monkeypatch.setattr(
        bootstrap,
        "check_customer_production_runtime_dependencies",
        unavailable,
    )
    monkeypatch.setattr(
        generation_worker,
        "run_forever_pg",
        lambda **_: pytest.fail("worker loop must not start"),
    )

    with pytest.raises(RuntimeError, match="private COS"):
        generation_worker.main()


def test_nginx_contract_load_balances_two_apis_and_rewrites_forwarding_headers() -> None:
    nginx = _read("deploy/nginx/customer.conf.example")

    assert "server 127.0.0.1:8001" in nginx
    assert "server 127.0.0.1:8002" in nginx
    assert "location = /live" in nginx
    assert "location = /ready" in nginx
    assert "location ^~ /api/" in nginx
    assert "try_files $uri $uri/ /index.html;" in nginx
    assert "proxy_set_header X-Forwarded-For $remote_addr;" in nginx
    assert "proxy_set_header X-Forwarded-Proto https;" in nginx
    assert "proxy_set_header Host $host;" in nginx
    assert "$proxy_add_x_forwarded_for" not in nginx
    assert "proxy_next_upstream_tries 2;" in nginx


def test_systemd_contract_has_two_api_ports_and_four_independent_workers() -> None:
    api = _read("deploy/systemd/video-replica-api@.service")
    worker = _read("deploy/systemd/video-replica-worker@.service")

    assert "EnvironmentFile=/etc/video-replica/customer.env" in api
    assert "python -m app.bootstrap" in api
    assert "--host 127.0.0.1 --port %i --no-proxy-headers" in api
    assert "EnvironmentFile=/etc/video-replica/customer.env" in worker
    assert "python -m app.bootstrap" in worker
    assert "python -m app.generation_worker" in worker
    assert "--worker-id %H-worker-%i" in worker
    assert "Requires=video-replica-api" not in worker
    assert "PartOf=video-replica-api" not in worker


def test_maintenance_and_pg_migration_are_single_owner_fail_closed_jobs() -> None:
    maintenance = _read("deploy/systemd/video-replica-maintenance.service")
    migration = _read("deploy/postgres/migrate.sh")

    assert "Type=oneshot" in maintenance
    assert "EnvironmentFile=/etc/video-replica/customer.env" in maintenance
    assert maintenance.count("ExecStart=") >= 3
    assert "flock -n" in migration
    assert "alembic upgrade head" in migration
    assert "VIDEO_REPLICA_DATABASE_URL" in migration
    assert "set -euo pipefail" in migration


def test_customer_deployment_docs_keep_evidence_levels_honest() -> None:
    runbook = _read("docs/客户版部署与灰度手册.md")
    evidence = _read("docs/客户版验收证据包模板.md")
    postgres = _read("deploy/postgres/README.md")

    for required in (
        "video-replica-api@8001",
        "video-replica-api@8002",
        "video-replica-worker@1",
        "video-replica-worker@4",
        "/live",
        "/ready",
        "--no-proxy-headers",
        "STAGING_VERIFIED",
        "回滚",
    ):
        assert required in runbook
    for level in (
        "AUTOMATED_VERIFIED",
        "STAGING_VERIFIED",
        "REAL_CHAIN_VERIFIED",
        "PRODUCTION_GO",
    ):
        assert level in evidence
    assert "PostgreSQL 16" in postgres
    assert "HA" in postgres
    assert "公网" in postgres
    assert "T38" in postgres


def test_customer_desktop_build_is_an_explicit_no_sidecar_target() -> None:
    package = _read("package.json")
    workflow = _read(".github/workflows/ci.yml")
    origin_guard = _read("scripts/require_customer_api_base.mjs")

    assert '"check:tauri:customer"' in package
    assert '"tauri:build:customer"' in package
    assert '"require:customer-api-base"' in package
    assert "--ci -- --no-default-features" in package
    assert "VITE_API_BASE_URL must be a non-loopback HTTPS origin" in origin_guard
    assert 'addSubnet("127.0.0.0", 8, "ipv4")' in origin_guard
    assert 'addAddress("::1", "ipv6")' in origin_guard
    assert "npm run check:tauri:customer" in workflow
    assert "npm run tauri:build:customer" in workflow
    assert "VITE_API_BASE_URL: https://staging.example.invalid" in workflow
    assert "unsigned-customer-cloud-windows-nsis" in workflow


@pytest.mark.parametrize(
    "origin",
    [
        "https://127.0.0.2",
        "https://localhost.",
        "https://api.localhost",
        "https://[::1]",
        "https://[::ffff:127.0.0.1]",
        "https://[::ffff:7f00:1]",
    ],
)
def test_customer_cloud_build_rejects_the_full_loopback_space(origin: str) -> None:
    env = os.environ.copy()
    env["VITE_API_BASE_URL"] = origin
    result = subprocess.run(
        ["node", "scripts/require_customer_api_base.mjs"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "non-loopback HTTPS origin" in result.stderr


def test_customer_cloud_build_accepts_a_remote_https_origin() -> None:
    env = os.environ.copy()
    env["VITE_API_BASE_URL"] = "https://staging.example.invalid"
    result = subprocess.run(
        ["node", "scripts/require_customer_api_base.mjs"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
