from __future__ import annotations

import json
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_cargo_build_uses_workspace_isolated_target_directory() -> None:
    config_path = REPO_ROOT / ".cargo" / "config.toml"

    assert config_path.exists()
    config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert config["build"]["target-dir"] == ".cargo-target"
    assert ".cargo-target/" in (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")


def test_python_quality_commands_survive_a_relocated_virtualenv() -> None:
    package = json.loads((REPO_ROOT / "package.json").read_text(encoding="utf-8"))
    scripts = package["scripts"]

    assert "python -m mypy" in scripts["check"]
    assert "python -m pytest" in scripts["check"]
    assert "python -m pytest" in scripts["test"]
    assert "python -m pytest" in scripts["test:e2e"]
    assert "--locked mypy" not in scripts["check"]
    assert "--locked pytest" not in scripts["check"]
    assert "--locked pytest" not in scripts["test"]
    assert "--locked pytest" not in scripts["test:e2e"]


def test_pull_requests_run_linux_quality_and_windows_nsis_gates() -> None:
    workflow_path = REPO_ROOT / ".github" / "workflows" / "ci.yml"

    assert workflow_path.exists()
    workflow = workflow_path.read_text(encoding="utf-8")
    assert "pull_request:" in workflow
    assert "push:" in workflow
    assert workflow.count("branches: [main]") == 2
    assert "pull_request_target:" not in workflow
    assert "permissions:\n  contents: read" in workflow
    assert workflow.count("persist-credentials: false") == 3
    assert "runs-on: ubuntu-24.04" in workflow
    assert "npm run check:security" in workflow
    assert "run: npm run check\n" in workflow
    assert "npm run build" in workflow
    assert "npm audit --audit-level=high" in workflow
    assert "cargo test --manifest-path client/src-tauri/Cargo.toml --locked" in workflow
    assert "runs-on: windows-2025" in workflow
    assert "npm run check:tauri" in workflow
    assert "npm run tauri:build -- --bundles nsis" in workflow
    assert workflow.count("actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1") == 3
    assert workflow.count("actions/setup-node@820762786026740c76f36085b0efc47a31fe5020") == 3
    assert "actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97" in workflow
    assert "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a" in workflow
    assert ".cargo-target/release/bundle/nsis/*.exe" in workflow
