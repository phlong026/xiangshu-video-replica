from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _bash_path() -> str | None:
    # Resolve the interpreter explicitly: Windows' CreateProcess searches
    # System32 before PATH, so a bare "bash" argument can silently resolve
    # to the WSL launcher stub (System32\bash.exe / WindowsApps\bash.exe)
    # even when git-bash is first on PATH. The stub's exit status drifts
    # with the WSL service state and its UTF-16 diagnostics kill text-mode
    # output readers mid-decode, so the POSIX launcher tests need a native
    # Windows POSIX shell (git-bash) or a real POSIX system, probed once at
    # import time.
    bash = shutil.which("bash")
    if bash is None:
        return None
    if sys.platform == "win32":
        lowered = {part.lower() for part in Path(bash).parts}
        if "system32" in lowered or "windowsapps" in lowered:
            return None
    probe = subprocess.run(
        [bash, "-c", "exit 0"],
        check=False,
        capture_output=True,
        timeout=30,
    )
    return bash if probe.returncode == 0 else None


BASH = _bash_path()


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


def test_api_uses_the_project_interpreter_instead_of_a_global_uvicorn() -> None:
    package = json.loads((REPO_ROOT / "package.json").read_text(encoding="utf-8"))

    assert "python -m uvicorn" in package["scripts"]["dev:server"]


def test_local_start_commands_upgrade_the_database_before_api_or_worker() -> None:
    package = json.loads((REPO_ROOT / "package.json").read_text(encoding="utf-8"))
    posix_launcher = (REPO_ROOT / "client/src-tauri/resources/start-backend.sh").read_text(
        encoding="utf-8"
    )
    windows_launcher = (REPO_ROOT / "client/src-tauri/resources/start-backend.bat").read_text(
        encoding="utf-8"
    )

    server_command = package["scripts"]["dev:server"]
    worker_command = package["scripts"]["dev:worker"]
    assert server_command.index("python -m app.bootstrap") < server_command.index(
        "python -m uvicorn"
    )
    assert worker_command.index("python -m app.bootstrap") < worker_command.index(
        "python -m app.generation_worker"
    )
    assert posix_launcher.index("python -m app.bootstrap") < posix_launcher.index("start_server")
    assert windows_launcher.index("python -m app.bootstrap") < windows_launcher.index(
        'start "video-replica-api"'
    )


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


def test_posix_backend_launcher_executes_default_commands(tmp_path: Path) -> None:
    if BASH is None:
        pytest.skip("a functional bash is required for the POSIX launcher flow")
    assert BASH is not None
    launcher = tmp_path / "start-backend.sh"
    shutil.copy2(REPO_ROOT / "client/src-tauri/resources/start-backend.sh", launcher)
    launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_uv = fake_bin / "uv"
    fake_uv.write_text(
        """#!/bin/sh
case "$*" in
  *app.bootstrap*) printf '%s' "$*" > "$TEST_BOOTSTRAP_MARKER" ;;
  *uvicorn*) printf '%s' "$*" > "$TEST_SERVER_MARKER" ;;
  *generation_worker*) printf '%s' "$*" > "$TEST_WORKER_MARKER" ;;
  *) exit 64 ;;
esac
""",
        encoding="utf-8",
    )
    fake_uv.chmod(fake_uv.stat().st_mode | stat.S_IXUSR)

    server_marker = tmp_path / "server.args"
    worker_marker = tmp_path / "worker.args"
    bootstrap_marker = tmp_path / "bootstrap.args"
    env = {
        **os.environ,
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "TEST_SERVER_MARKER": str(server_marker),
        "TEST_WORKER_MARKER": str(worker_marker),
        "TEST_BOOTSTRAP_MARKER": str(bootstrap_marker),
        "VIDEO_REPLICA_DB_PATH": str(tmp_path / "app.db"),
        "VIDEO_REPLICA_SETTINGS_KEY": "test-settings-key",
        "VIDEO_REPLICA_DESKTOP_USER_ID": "employee_1",
    }

    result = subprocess.run(
        [BASH, str(launcher)],
        check=False,
        capture_output=True,
        env=env,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    assert "python -m app.bootstrap" in bootstrap_marker.read_text(encoding="utf-8")
    assert "python -m uvicorn app.main:app" in server_marker.read_text(encoding="utf-8")
    assert "python -m app.generation_worker" in worker_marker.read_text(encoding="utf-8")


def test_packaged_launchers_reject_partial_command_overrides(tmp_path: Path) -> None:
    launcher = tmp_path / "start-backend.sh"
    shutil.copy2(REPO_ROOT / "client/src-tauri/resources/start-backend.sh", launcher)
    launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR)

    partial_overrides = (
        {
            "VIDEO_REPLICA_SERVER_CMD": "/usr/bin/true",
            "VIDEO_REPLICA_WORKER_CMD": "/usr/bin/true",
        },
        {
            "VIDEO_REPLICA_BOOTSTRAP_CMD": "/usr/bin/true",
            "VIDEO_REPLICA_SERVER_CMD": "/usr/bin/true",
        },
        {
            "VIDEO_REPLICA_BOOTSTRAP_CMD": "/usr/bin/true",
            "VIDEO_REPLICA_WORKER_CMD": "/usr/bin/true",
        },
    )
    expected_error = "packaged bootstrap, server, and worker commands must be set together"
    if BASH is not None:
        for overrides in partial_overrides:
            env = {
                **os.environ,
                "VIDEO_REPLICA_DB_PATH": str(tmp_path / "app.db"),
                "VIDEO_REPLICA_DESKTOP_USER_ID": "employee_1",
                **overrides,
            }
            result = subprocess.run(
                [BASH, str(launcher)],
                check=False,
                capture_output=True,
                env=env,
                text=True,
                timeout=10,
            )

            assert result.returncode != 0
            assert expected_error in str(result.stderr)

    windows_launcher = (REPO_ROOT / "client/src-tauri/resources/start-backend.bat").read_text(
        encoding="utf-8"
    )
    distribution_plan = (REPO_ROOT / "docs/服务端分发与自动拉起方案.md").read_text(encoding="utf-8")
    assert expected_error in windows_launcher
    assert "VIDEO_REPLICA_BOOTSTRAP_CMD" in distribution_plan


def test_posix_packaged_launcher_runs_without_uv_or_server_sources(tmp_path: Path) -> None:
    if BASH is None:
        pytest.skip("a functional bash is required for the POSIX launcher flow")
    assert BASH is not None
    launcher = tmp_path / "start-backend.sh"
    shutil.copy2(REPO_ROOT / "client/src-tauri/resources/start-backend.sh", launcher)
    launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR)

    marker_dir = tmp_path / "markers"
    marker_dir.mkdir()
    commands: dict[str, str] = {}
    for name in ("bootstrap", "server", "worker"):
        command = tmp_path / name
        # as_posix(): the launcher hands the override commands to
        # ``sh -c``, where Windows backslash separators would be eaten as
        # escape characters (identical to str() on POSIX).
        command_path = command.as_posix()
        marker_path = (marker_dir / name).as_posix()
        command.write_text(
            f"#!/bin/sh\nprintf '%s' '{name}' > '{marker_path}'\n",
            encoding="utf-8",
        )
        command.chmod(command.stat().st_mode | stat.S_IXUSR)
        commands[name] = command_path

    env = {
        **os.environ,
        "PATH": "/usr/bin:/bin",
        "VIDEO_REPLICA_DB_PATH": str(tmp_path / "app.db"),
        "VIDEO_REPLICA_DESKTOP_USER_ID": "employee_1",
        "VIDEO_REPLICA_BOOTSTRAP_CMD": commands["bootstrap"],
        "VIDEO_REPLICA_SERVER_CMD": commands["server"],
        "VIDEO_REPLICA_WORKER_CMD": commands["worker"],
    }
    result = subprocess.run(
        [BASH, str(launcher)],
        check=False,
        capture_output=True,
        env=env,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    assert {path.name for path in marker_dir.iterdir()} == {"bootstrap", "server", "worker"}
