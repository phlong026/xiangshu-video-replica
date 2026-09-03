from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_customer_git_rollout_requires_an_exact_revision_before_any_mutation() -> None:
    script = (REPO_ROOT / "deploy" / "customer-git-rollout.sh").read_text(encoding="utf-8")

    assert (
        'REPO_URL="${VIDEO_REPLICA_GIT_REPO_URL:-'
        'https://github.com/phlong026/xiangshu-video-replica.git}"'
    ) in script
    assert '[[ "$RELEASE_SHA" =~ ^[0-9a-f]{40}$ ]]' in script
    assert 'git -C "$SOURCE" fetch --depth=1 origin "$RELEASE_SHA"' in script
    assert '[[ "$(git -C "$SOURCE" rev-parse HEAD)" == "$RELEASE_SHA" ]]' in script
    assert 'git -C "$SOURCE" diff --quiet' in script
    assert "mark PREFLIGHT" in script
    runtime = script[script.index('cd "$ROOT"') :]
    assert runtime.index("mark PREFLIGHT") < runtime.index('docker compose -f "$COMPOSE" up')


def test_customer_git_rollout_builds_web_and_preserves_database_rollback_evidence() -> None:
    script = (REPO_ROOT / "deploy" / "customer-git-rollout.sh").read_text(encoding="utf-8")

    assert "npm ci --ignore-scripts" in script
    assert "npm run build --workspace client" in script
    assert 'NODE_BUILD_IMAGE="node:24-bookworm-slim"' in script
    assert 'docker image inspect "$NODE_BUILD_IMAGE"' in script
    assert 'docker run --rm -v "$SOURCE:/workspace"' in script
    assert 'require_command "node"' not in script
    assert 'require_command "npm"' not in script
    assert "pg_dump -Fc" in script
    assert "DATABASE_HEAD_LEFT_FORWARD_COMPATIBLE" in script
    assert 'trap \'rollback "$?" "$LINENO" "$BASH_COMMAND"\' ERR' in script
    assert "VIDEO_REPLICA_DATABASE_URL" not in script
    assert "VIDEO_REPLICA_SETTINGS_KEY" not in script


def test_customer_git_rollout_ignores_only_root_package_version_metadata() -> None:
    script = (REPO_ROOT / "deploy" / "customer-git-rollout.sh").read_text(encoding="utf-8")

    assert "dependency_manifest_hash" in script
    assert "format_path = sys.argv[2]" in script
    assert 'if format_path.endswith("pyproject.toml"):' in script
    assert 'package["name"] != "video-replica-api"' in script
    assert 'dependency_manifest_hash "$SOURCE/$dependency_file" "$dependency_file"' in script
    assert 'dependency_manifest_hash - "$dependency_file"' in script
    assert "Python dependency change requires a base-image release" in script
