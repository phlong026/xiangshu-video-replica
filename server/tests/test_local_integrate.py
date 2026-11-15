"""集成脚本的全量默认值、显式增量和失败回滚，均在独立临时仓库验证。"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "local-integrate.sh"


@pytest.mark.parametrize("gate", ["full", "incremental", "failed"])
def test_local_integrate_gates_and_rollback(tmp_path: Path, gate: str) -> None:
    bash = shutil.which("bash")
    if os.name == "nt" and (git_binary := shutil.which("git")):
        # Windows 的 PATH 可能先命中 WSL 启动器；本机 Git 仓库须用 Git Bash。
        git_path = Path(git_binary).resolve()
        for candidate in (git_path.parent / "bash.exe", git_path.parents[1] / "bin" / "bash.exe"):
            if candidate.is_file():
                bash = str(candidate)
                break
    assert bash is not None, "仓库集成流程要求 Bash"
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "integration-test",
        "GIT_AUTHOR_EMAIL": "integration-test@example.invalid",
        "GIT_COMMITTER_NAME": "integration-test",
        "GIT_COMMITTER_EMAIL": "integration-test@example.invalid",
        "GIT_CONFIG_COUNT": "2",
        "GIT_CONFIG_KEY_0": "gc.auto",
        "GIT_CONFIG_VALUE_0": "0",
        "GIT_CONFIG_KEY_1": "maintenance.auto",
        "GIT_CONFIG_VALUE_1": "false",
    }

    def git(*args: str) -> str:
        return subprocess.check_output(
            ["git", "-c", "commit.gpgsign=false", *args],
            cwd=tmp_path,
            env=env,
            encoding="utf-8",
        ).strip()

    git("init", "-b", "main")
    git("config", "core.autocrlf", "false")
    git("config", "core.hooksPath", "disabled-hooks")
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    shutil.copyfile(SCRIPT, scripts / "local-integrate.sh")
    git("add", ".")
    git("commit", "-m", "baseline")
    original_main = git("rev-parse", "main")
    git("checkout", "-b", "feature")
    (tmp_path / "feature.txt").write_text("changed\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-m", "feature")
    # 只替代门禁命令，不替代 Git：检查真实 merge、失败 abort 和提交证据。
    (tmp_path / "gate.sh").write_text(
        "printf incremental > called-incremental\n" + ("exit 1\n" if gate == "failed" else ""),
        encoding="utf-8",
        newline="\n",
    )
    tools = tmp_path / "tools"
    tools.mkdir()
    npm = tools / "npm"
    npm.write_text(
        "#!/usr/bin/env bash\nprintf full > called-full\n", encoding="utf-8", newline="\n"
    )
    npm.chmod(0o755)
    env["PATH"] = str(tools) + os.pathsep + env["PATH"]
    command = [bash, "scripts/local-integrate.sh", "feature"]
    if gate != "full":
        command.extend(["--incremental-gate", "gate.sh"])
    result = subprocess.run(command, cwd=tmp_path, env=env, capture_output=True, encoding="utf-8")
    assert result.returncode == (1 if gate == "failed" else 0), result.stdout + result.stderr
    assert (tmp_path / "called-full").exists() == (gate == "full")
    assert (tmp_path / "called-incremental").exists() == (gate != "full")
    git_dir = Path(git("rev-parse", "--absolute-git-dir"))
    assert not (git_dir / "MERGE_HEAD").exists()
    if gate == "failed":
        assert git("rev-parse", "main") == original_main
        assert git("branch", "--show-current") == "feature"
    else:
        assert git("rev-parse", "main^{tree}") == git("rev-parse", "feature^{tree}")
        message = git("log", "main", "-1", "--format=%B")
        if gate == "full":
            assert "Local gate: check:full PASS" in message
        else:
            assert "NOT rerun" in message
            assert "check:full PASS" not in message
