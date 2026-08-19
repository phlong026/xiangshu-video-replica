from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[2]


def read_repo_file(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


def load_acceptance_module() -> ModuleType:
    script_path = REPO_ROOT / "scripts/p0_acceptance_evidence.py"
    spec = importlib.util.spec_from_file_location("p0_acceptance_evidence", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_single_host_services_share_one_database_and_keep_api_on_loopback() -> None:
    api = read_repo_file("deploy/systemd/video-replica-api.service")
    worker = read_repo_file("deploy/systemd/video-replica-worker.service")
    backup = read_repo_file("deploy/systemd/video-replica-backup.service")
    timer = read_repo_file("deploy/systemd/video-replica-backup.timer")

    environment_file = "EnvironmentFile=/etc/video-replica/internal-p0.env"
    assert environment_file in api
    assert environment_file in worker
    assert "python -m app.bootstrap" in api
    assert "python -m uvicorn app.main:app --host 127.0.0.1 --port 8000" in api
    assert "Requires=video-replica-api.service" in worker
    assert "python -m app.generation_worker" in worker
    assert "ProtectSystem=strict" in api
    assert "ProtectSystem=strict" in worker
    assert "ReadWritePaths=/var/lib/video-replica" in api
    assert "ReadWritePaths=/var/lib/video-replica" in worker
    assert "python -m app.backup daily /var/lib/video-replica/app.db" in backup
    assert "ReadWritePaths=/var/backups/video-replica" in backup
    assert "Persistent=true" in timer


def test_production_environment_template_disables_desktop_identity_bypasses() -> None:
    environment = read_repo_file("deploy/internal-p0.env.example")

    assert "VIDEO_REPLICA_DB_PATH=/var/lib/video-replica/app.db" in environment
    assert "VIDEO_REPLICA_STORAGE_ROOT=/var/lib/video-replica/storage" in environment
    assert "VIDEO_REPLICA_AUTH_MODE=internal" in environment
    assert "VIDEO_REPLICA_ALLOW_DEV_IDENTITY_HEADER=0" in environment
    assert "VIDEO_REPLICA_DESKTOP_USER_ID=" in environment
    assert "VIDEO_REPLICA_DISABLE_LOCAL_KEYSTORE=1" in environment
    assert "PUBLIC_BASE_URL=https://internal.example.com" in environment
    assert "ZPAY_GATEWAY_URL=https://zpayz.cn/submit.php" in environment
    assert "CONTROL_PROXY_TOKEN_DIGEST=REPLACE_WITH_SHA256_DIGEST" in environment
    assert "CONTROL_ADMIN_USER_ID=REPLACE_WITH_ADMIN_USER_ID" in environment


def test_secret_scan_covers_deployment_files_and_rejects_raw_control_tokens() -> None:
    secret_scan = read_repo_file("scripts/verify_no_secrets.sh")
    gitignore = read_repo_file(".gitignore")

    assert '"deploy"' in secret_scan
    assert "REPLACE_WITH_32_BYTE_RANDOM_TOKEN" in secret_scan
    assert "Unexpected raw control proxy token" in secret_scan
    assert "command -v rg" not in secret_scan
    assert "Secret scan requires rg for deployment token checks." not in secret_scan
    assert "Deployment token scan failed." in secret_scan
    assert "git grep -n -I -E --untracked --no-exclude-standard" in secret_scan
    assert "rg --hidden --no-ignore" not in secret_scan
    assert "deploy || true" not in secret_scan
    assert "deploy/*.env" in gitignore
    assert "deploy/nginx/*.conf" in gitignore


def test_nginx_routes_health_to_api_and_keeps_payment_callbacks_public() -> None:
    nginx = read_repo_file("deploy/nginx/internal-p0.conf.example")

    health_index = nginx.index("location = /health")
    public_notify_index = nginx.index("location = /api/payments/zpay/notify")
    admin_index = nginx.index("location ^~ /admin")
    assert health_index < admin_index
    assert public_notify_index < admin_index
    assert nginx.count('proxy_set_header X-Control-Proxy-Token "";') >= 4
    assert "root /opt/video-replica/app/client/dist;" in nginx


def test_fake_acceptance_runner_is_local_only_and_declares_external_gaps() -> None:
    script_path = REPO_ROOT / "scripts/p0_acceptance_evidence.py"
    module = load_acceptance_module()

    tests = module.ACCEPTANCE_TESTS
    assert any(
        "test_valid_notify_credits_wallet_and_duplicate_notify_is_idempotent" in item
        for item in tests
    )
    assert any(
        "test_return_page_is_display_only_even_with_valid_payment_fields" in item for item in tests
    )
    assert any("test_reserve_moves_one_credit_and_is_idempotent" in item for item in tests)
    assert any("test_finalize_success_settles_only_an_archived_result" in item for item in tests)
    assert any(
        "test_finalize_failure_or_cancellation_releases_credit_once" in item for item in tests
    )
    assert any("test_real_provider_result_must_be_archived_in_cos" in item for item in tests)
    assert any("test_undownloadable_archived_result_does_not_settle" in item for item in tests)
    assert module.NOT_VERIFIED == (
        "real ZPay payment",
        "real COS archive and signed download",
        "real Provider generation",
    )
    assert "http://" not in script_path.read_text(encoding="utf-8")
    assert "https://" not in script_path.read_text(encoding="utf-8")
    assert '"--untracked-files=all"' in script_path.read_text(encoding="utf-8")


def test_fake_acceptance_manifest_is_redacted_and_locally_scoped() -> None:
    module = load_acceptance_module()

    manifest = module._manifest(
        repo_root=REPO_ROOT,
        command=["python", "-m", "pytest"],
        returncode=0,
        generated_at="2026-08-19T00:00:00+00:00",
    )

    assert manifest["verification_level"] == "LOCALLY_VERIFIED"
    assert manifest["scope"] == "fake_and_contract_only"
    assert manifest["command"][0] == "python"
    assert manifest["redaction"] == {
        "merchant_key_saved": False,
        "payment_signature_saved": False,
        "full_callback_query_saved": False,
    }
