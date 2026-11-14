from __future__ import annotations

import json
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_rollout_uses_installed_topology_before_fetching_new_release() -> None:
    script = (REPO_ROOT / "deploy/customer-git-rollout.sh").read_text(encoding="utf-8")
    assert 'COMPOSE="${CUSTOMER_COMPOSE:-$SCRIPT_DIR/customer/compose.yaml}"' in script
    assert 'COMPOSE_ENV="${CUSTOMER_COMPOSE_ENV:-/etc/video-replica/compose.env}"' in script
    assert "text.replace(old, new)" not in script


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX-only：断言 0600 权限位，Windows 的 st_mode 结构性不满足",
)
def test_image_override_changes_parameterized_images_without_touching_topology(tmp_path) -> None:
    script = (REPO_ROOT / "deploy/customer-git-rollout.sh").read_text(encoding="utf-8")
    start = script.index("write_image_override() {")
    function = script[start : script.index("\n}\n", start) + 3]
    target = tmp_path / "image.json"
    subprocess.run(
        [
            "bash",
            "-c",
            function + '\nwrite_image_override "$1" "$2" api-1 worker-1 migrate',
            "test",
            str(target),
            "registry.example/release:abc",
        ],
        check=True,
    )
    document = json.loads(target.read_text())
    assert document == {
        "services": {
            service: {"image": "registry.example/release:abc"}
            for service in ("api-1", "worker-1", "migrate")
        }
    }
    assert target.stat().st_mode & 0o077 == 0


def test_bootstrap_records_the_database_head_required_by_first_upgrade() -> None:
    script = (REPO_ROOT / "deploy/customer/bootstrap-base-image.sh").read_text(encoding="utf-8")
    assert "video-replica.database-head=$EXPECTED_DB_HEAD" in script
    assert "org.opencontainers.image.revision=$RELEASE_SHA" in script


def _rollout_function(name: str) -> str:
    script = (REPO_ROOT / "deploy/customer-git-rollout.sh").read_text(encoding="utf-8")
    start = script.index(f"{name}() {{")
    return script[start : script.index("\n}\n", start) + 3]


_API_IMAGE = "sha256:" + "a" * 64
_WORKER_IMAGE = "sha256:" + "b" * 64
_NEW_IMAGE = "sha256:" + "c" * 64
_DRIFTED_IMAGE = "sha256:" + "d" * 64
_REQUIRED = ["api-1", "api-2", "worker-1", "worker-2", "worker-3", "worker-4"]
_OPTIONAL = ["worker-viral", "worker-publish"]

# 真实执行发布函数，Docker 边界用本地进程模拟；不连接守护进程或生产服务。
_MOCK_DOCKER = r"""#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

root = Path(os.environ["ROLLOUT_TEST_ROOT"])
path = root / "docker-state.json"
state = json.loads(path.read_text())
args = sys.argv[1:]
with (root / "docker-commands.jsonl").open("a") as log:
    log.write(json.dumps(args) + "\n")
result = ""
exit_code = 0
if args[0] == "inspect":
    container = state["containers"][args[-1]]
    result = {
        "{{.Image}}": container["image"],
        "{{.Config.Image}}": container["reference"],
        "{{.State.Running}}": str(container["running"]).lower(),
    }[args[2]]
elif args[:2] == ["image", "inspect"]:
    result = state["heads"][args[-1]]
elif args[:2] == ["image", "tag"]:
    state["aliases"][args[-1]] = args[-2]
elif args[0] == "compose":
    files = []
    i = 1
    while args[i].startswith("-"):
        if args[i] == "-f":
            files.append(args[i + 1])
        i += 2
    command = args[i:]
    if command[:2] == ["ps", "-q"]:
        service = command[-1]
        if state["containers"][service]["running"]:
            result = service
    elif command[0] == "stop":
        if state["failure"] == "stop":
            exit_code = 1
        else:
            state["containers"][command[-1]]["running"] = False
    elif command[0] == "up":
        rollback = any(f.endswith("rollback-images.json") for f in files)
        if rollback and state["failure"] == "up":
            exit_code = 1
        else:
            images = {}
            for file in files:
                document = json.loads(Path(file).read_text())
                images.update({k: v["image"] for k, v in document["services"].items()})
            for service in command[3:]:
                reference = images[service]
                image = state["aliases"].get(reference, reference)
                if rollback and state["failure"] == "wrong-image" and service == "worker-1":
                    image = state["aliases"]["new"]
                state["containers"][service] = {
                    "reference": reference, "image": image, "running": True,
                }
            if not rollback:
                state["aliases"]["api:stable"] = state["drifted_image"]
    else:
        raise AssertionError(command)
else:
    raise AssertionError(args)
path.write_text(json.dumps(state))
if result:
    print(result)
sys.exit(exit_code)
"""


def _run_rollout_mock(tmp_path, *, mode="rollback", previous=False, failure="", incompatible=False):
    import os

    services = _REQUIRED + _OPTIONAL
    baseline = {
        service: {
            "reference": "api:stable" if service.startswith("api-") else "worker:old",
            "image": _API_IMAGE if service.startswith("api-") else _WORKER_IMAGE,
            "running": service != "worker-publish",
        }
        for service in services
    }
    if mode == "missing-required":
        baseline["worker-1"]["running"] = False
    state = {
        "containers": baseline,
        "heads": {
            _API_IMAGE: "schema1",
            _WORKER_IMAGE: ""
            if incompatible == "missing"
            else "schema2"
            if incompatible
            else "schema1",
        },
        "aliases": {"api:stable": _API_IMAGE, "worker:old": _WORKER_IMAGE, "new": _NEW_IMAGE},
        "failure": failure,
        "drifted_image": _DRIFTED_IMAGE,
    }
    (tmp_path / "docker-state.json").write_text(json.dumps(state))
    topology = json.dumps({"services": {k: {"image": v["reference"]} for k, v in baseline.items()}})
    (tmp_path / "compose.json").write_text(topology)
    previous_text = '{"services":{"api-1":{"image":"api:stable"}},"x-keep":{"value":42}}'
    if previous:
        (tmp_path / "image.json").write_text(previous_text)
    previous_site = tmp_path / "previous-index.html"
    previous_site.write_text("previous-artifact")
    for name in ("site", "admin"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "index.html").write_text("current-artifact")
        with tarfile.open(tmp_path / f"{name}-before.tar.gz", "w:gz") as archive:
            archive.add(previous_site, arcname="index.html")
    (tmp_path / "bin").mkdir()
    docker = tmp_path / "bin/docker"
    docker.write_text(_MOCK_DOCKER)
    docker.chmod(0o700)
    harness = r"""
set -Eeuo pipefail
ROOT="$1"
export ROLLOUT_TEST_ROOT="$ROOT"
export PATH="$ROOT/bin:$PATH"
BACKUP="$ROOT/backup"
IMAGE_OVERRIDE="$ROOT/image.json"
COMPOSE="$ROOT/compose.json"
COMPOSE_ENV="$ROOT/compose.env"
SITE="$ROOT/site"
ADMIN_SITE="$ROOT/admin"
SHORT_SHA=1234567
STAMP=test-release
ROLLOUT_STARTED=0
IMAGE_SWITCHED=0
SITE_SWITCHED=0
ROLLBACK_PINNED=0
declare -A ROLLBACK_IMAGE_IDS=()
REQUIRED_SERVICES=(api-1 api-2 worker-1 worker-2 worker-3 worker-4)
OPTIONAL_SERVICES=(worker-viral worker-publish)
SERVICES=("${REQUIRED_SERVICES[@]}" worker-viral worker-publish)
mark() { echo "$1" > "$ROOT/status"; }
wait_ready() { [[ "$2" != not-ready ]]; }
""".replace('[[ "$2" != not-ready ]]', f'[[ "{failure}" != not-ready ]]')
    functions = "\n".join(
        _rollout_function(name)
        for name in (
            "compose",
            "snapshot_running_images",
            "verify_running_image_snapshot",
            "write_rollback_images",
            "write_image_override",
            "rollback",
        )
    )
    invocation = r"""
snapshot_running_images
mkdir "$BACKUP"
write_rollback_images
cp "$ROOT/site-before.tar.gz" "$BACKUP/site-before.tar.gz"
cp "$ROOT/admin-before.tar.gz" "$BACKUP/admin-site-before.tar.gz"
if [[ -f "$IMAGE_OVERRIDE" ]]; then cp -a "$IMAGE_OVERRIDE" "$BACKUP/image-before.json"; fi
"""
    if mode == "drift":
        invocation += r"""
python3 - "$ROOT/docker-state.json" <<'PY'
import json
import sys
from pathlib import Path
path = Path(sys.argv[1])
state = json.loads(path.read_text())
state["containers"]["api-1"]["image"] = state["aliases"]["new"]
path.write_text(json.dumps(state))
PY
"""
    invocation += r"""
verify_running_image_snapshot
write_image_override "$IMAGE_OVERRIDE" new "${SERVICES[@]}"
IMAGE_SWITCHED=1
"""
    if mode == "site-rollback":
        invocation += "\nSITE_SWITCHED=1\n"
    if mode in {"rollback", "site-rollback"}:
        invocation += r"""
ROLLOUT_STARTED=1
compose up -d --no-deps api-1 worker-publish
rollback 17 1 simulated-mid-rollout-failure
"""
    result = subprocess.run(
        ["bash", "-c", harness + functions + invocation, "test", str(tmp_path)],
        capture_output=True,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    assert (tmp_path / "compose.json").read_text() == topology
    expected_site = "previous-artifact" if mode == "site-rollback" else "current-artifact"
    for name in ("site", "admin"):
        assert (tmp_path / name / "index.html").read_text() == expected_site
    return result, previous_text


@pytest.mark.skipif(sys.platform == "win32", reason="本地 Linux Docker mock 验证 POSIX 发布函数")
def test_rollout_accepts_mixed_images_and_pins_actual_running_ids(tmp_path) -> None:
    result, _ = _run_rollout_mock(tmp_path, mode="select")
    assert result.returncode == 0, result.stderr.decode()
    inventory = json.loads((tmp_path / "backup/running-images-before.json").read_text())
    assert set(inventory) == set(_REQUIRED + ["worker-viral"])
    assert inventory["api-1"]["image_id"] == _API_IMAGE
    assert inventory["worker-1"]["image_id"] == _WORKER_IMAGE
    selected = json.loads((tmp_path / "image.json").read_text())
    assert selected == {"services": {s: {"image": "new"} for s in _REQUIRED + _OPTIONAL}}
    commands = [
        json.loads(line) for line in (tmp_path / "docker-commands.jsonl").read_text().splitlines()
    ]
    first_tag = next(i for i, c in enumerate(commands) if c[:2] == ["image", "tag"])
    assert all(
        c[0] == "inspect" or c[:2] == ["image", "inspect"] or "ps" in c
        for c in commands[:first_tag]
    )
    assert len([c for c in commands if c[:2] == ["image", "tag"]]) == 7


@pytest.mark.skipif(sys.platform == "win32", reason="本地 Linux Docker mock 验证 POSIX 发布函数")
@pytest.mark.parametrize("has_previous", [False, True])
@pytest.mark.parametrize("failure", ["", "not-ready", "up", "wrong-image", "stop"])
def test_rollout_restores_image_selection_and_checks_recovery(
    tmp_path, has_previous, failure
) -> None:
    result, previous = _run_rollout_mock(tmp_path, previous=has_previous, failure=failure)
    assert result.returncode == 17, result.stderr.decode()
    if has_previous:
        assert (tmp_path / "image.json").read_text() == previous
    else:
        assert not (tmp_path / "image.json").exists()
    expected = "FAILED_ROLLBACK_INCOMPLETE" if failure else "FAILED_ROLLED_BACK"
    assert (tmp_path / "status").read_text().strip() == expected
    state = json.loads((tmp_path / "docker-state.json").read_text())
    assert state["aliases"]["api:stable"] == _DRIFTED_IMAGE
    if failure != "up":
        assert state["containers"]["api-1"]["image"] == _API_IMAGE
        assert state["containers"]["worker-2"]["image"] == _WORKER_IMAGE
        assert state["containers"]["worker-viral"]["running"] is True
    if failure != "stop":
        assert state["containers"]["worker-publish"]["running"] is False


@pytest.mark.skipif(sys.platform == "win32", reason="本地 Linux Docker mock 验证 POSIX 发布函数")
@pytest.mark.parametrize("incompatible", ["different", "missing"])
def test_rollout_rejects_incompatible_running_schema_before_mutation(
    tmp_path, incompatible
) -> None:
    result, _ = _run_rollout_mock(tmp_path, incompatible=incompatible)
    assert result.returncode != 0
    assert b"incompatible running image database head" in result.stderr
    assert not (tmp_path / "backup").exists()
    assert not (tmp_path / "image.json").exists()
    commands = [
        json.loads(line) for line in (tmp_path / "docker-commands.jsonl").read_text().splitlines()
    ]
    assert all(c[0] == "inspect" or c[:2] == ["image", "inspect"] or "ps" in c for c in commands)


@pytest.mark.skipif(sys.platform == "win32", reason="本地 Linux Docker mock 验证 POSIX 发布函数")
@pytest.mark.parametrize("mode", ["missing-required", "drift"])
def test_rollout_refuses_missing_or_changed_baseline_without_switching_override(
    tmp_path, mode
) -> None:
    result, previous = _run_rollout_mock(tmp_path, mode=mode, previous=True)
    assert result.returncode != 0
    assert (tmp_path / "image.json").read_text() == previous
    if mode == "missing-required":
        assert b"required service is not running" in result.stderr
    else:
        assert b"running image changed after snapshot" in result.stderr


@pytest.mark.skipif(sys.platform == "win32", reason="本地 Linux Docker mock 验证 POSIX 发布函数")
def test_rollout_restores_sites_only_after_switching_the_site_artifacts(tmp_path) -> None:
    result, _ = _run_rollout_mock(tmp_path, mode="site-rollback")
    assert result.returncode == 17, result.stderr.decode()
    assert (tmp_path / "status").read_text().strip() == "FAILED_ROLLED_BACK"


def test_customer_git_rollout_requires_an_exact_revision_before_any_mutation() -> None:
    script = (REPO_ROOT / "deploy" / "customer-git-rollout.sh").read_text(encoding="utf-8")

    assert (
        'REPO_URL="${VIDEO_REPLICA_GIT_REPO_URL:-'
        'https://github.com/z7hh2s48ct-cmyk/xiangshu-video-replica}"'
    ) in script
    assert '[[ "$RELEASE_SHA" =~ ^[0-9a-f]{40}$ ]]' in script
    assert 'git -C "$SOURCE" fetch --depth=1 origin "$RELEASE_SHA"' in script
    assert '[[ "$(git -C "$SOURCE" rev-parse HEAD)" == "$RELEASE_SHA" ]]' in script
    assert 'git -C "$SOURCE" diff --quiet' in script
    assert "mark PREFLIGHT" in script
    runtime = script[script.index('cd "$ROOT"') :]
    assert runtime.index("mark PREFLIGHT") < runtime.index("compose up")


def test_customer_git_rollout_builds_web_and_preserves_database_rollback_evidence() -> None:
    script = (REPO_ROOT / "deploy" / "customer-git-rollout.sh").read_text(encoding="utf-8")

    assert "npm ci --ignore-scripts" in script
    # CW-019：客户与管理是两个独立制品，发布链必须一次构建两个并跑排除断言。
    # 原 needle "npm run build --workspace client" 只产出 client/dist，会让
    # /admin 在合并后首次发布时静默 500。
    assert "npm run build:all" in script
    assert "npm run verify:customer-bundle" in script
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


def test_customer_git_rollout_injects_the_cloud_admin_origin() -> None:
    script = (REPO_ROOT / "deploy" / "customer-git-rollout.sh").read_text(encoding="utf-8")

    assert 'PUBLIC_ORIGIN="https://video.zszhj.cn"' in script
    assert '-e "VITE_CLOUD_ADMIN_ORIGIN=$PUBLIC_ORIGIN"' in script


def test_customer_git_rollout_only_rolls_optional_services_when_configured() -> None:
    script = (REPO_ROOT / "deploy" / "customer-git-rollout.sh").read_text(encoding="utf-8")

    assert "OPTIONAL_SERVICES=(worker-viral worker-publish)" in script
    assert ("mapfile -t CONFIGURED_SERVICES < <(compose config --services)") in script
    assert 'SERVICES+=("$service")' in script
    assert 'WORKER_SERVICES+=("$service")' in script
    assert 'for service in "${WORKER_SERVICES[@]}"; do' in script


def test_customer_git_rollout_allows_retry_after_forward_compatible_rollback() -> None:
    script = (REPO_ROOT / "deploy" / "customer-git-rollout.sh").read_text(encoding="utf-8")

    assert (
        '"$CURRENT_HEAD_BEFORE" != "$OLD_IMAGE_DB_HEAD" '
        '&& "$CURRENT_HEAD_BEFORE" != "$EXPECTED_DB_HEAD"'
    ) in script
    assert (
        "database revision is neither the active image head nor the target release head" in script
    )


def test_customer_git_rollout_ignores_only_root_package_version_metadata() -> None:
    script = (REPO_ROOT / "deploy" / "customer-git-rollout.sh").read_text(encoding="utf-8")

    assert "dependency_manifest_hash" in script
    assert "format_path = sys.argv[2]" in script
    assert 'if format_path.endswith("pyproject.toml"):' in script
    assert 'package["name"] != "video-replica-api"' in script
    assert 'dependency_manifest_hash "$SOURCE/$dependency_file" "$dependency_file"' in script
    assert 'dependency_manifest_hash - "$dependency_file"' in script
    assert "Python dependency change requires a base-image release" in script


def test_customer_git_rollout_ships_admin_artifact_in_the_same_release_run() -> None:
    """CW-019：管理制品必须与客户制品同 SHA 同轮替换，且失败要能回滚。

    本脚本是仓库里唯一的客户站部署可执行路径。拆分后 nginx 从 $ADMIN_SITE 提供
    /admin/，若脚本不产出、不备份、不原子替换 dist-admin，合并后首次发布仍会照常
    打印 SUCCESS，而 /admin 静默 500——失败发生在脚本的成功判据之外，故必须由
    断言锁住，不能只靠人工评审。
    """
    script = (REPO_ROOT / "deploy" / "customer-git-rollout.sh").read_text(encoding="utf-8")

    # 管理站与客户站并列，且暂存目录同样按 release SHA + STAMP 命名（同轮）
    assert 'ADMIN_SITE="${SITE}-admin"' in script
    assert 'STAGE_ADMIN_SITE="$ROOT/admin-site-git-$SHORT_SHA-$STAMP"' in script
    # 构建产物落地校验：与客户站同规格（index.html 非空 + assets 目录 + 提取资源名）
    assert (
        '[[ -s "$SOURCE/client/dist-admin/index.html" '
        '&& -d "$SOURCE/client/dist-admin/assets" ]]' in script
    )
    assert (
        "EXPECTED_ADMIN_ASSET=$(grep -oE 'assets/[^\" ]+\\.js' "
        '"$SOURCE/client/dist-admin/index.html" | head -n 1)' in script
    )
    # 备份与回滚：归档存在才恢复（首次引入 dist-admin 的发布没有前一版可回滚）
    assert 'tar -czf "$BACKUP/admin-site-before.tar.gz" -C "$ADMIN_SITE" .' in script
    assert 'sha256sum "$BACKUP/admin-site-before.tar.gz" >> "$BACKUP/BACKUP-SHA256SUMS"' in script
    assert 'if [[ -f "$BACKUP/admin-site-before.tar.gz" ]]; then' in script
    assert 'tar -xzf "$BACKUP/admin-site-before.tar.gz" -C "$ADMIN_SITE"' in script
    # 原子替换：先清空再落盘，紧随客户站之后，两制品永不出自不同 release SHA
    assert 'mkdir -p "$STAGE_SITE" "$STAGE_ADMIN_SITE"' in script
    assert 'cp -a "$SOURCE/client/dist-admin"/. "$STAGE_ADMIN_SITE"/' in script
    assert 'grep -q "$EXPECTED_ADMIN_ASSET" "$STAGE_ADMIN_SITE/index.html"' in script
    assert 'cp -a "$STAGE_ADMIN_SITE"/. "$ADMIN_SITE"/' in script
    assert script.index('cp -a "$STAGE_SITE"/. "$SITE"/') < script.index(
        'cp -a "$STAGE_ADMIN_SITE"/. "$ADMIN_SITE"/'
    )
    # 真实探活：只比对落盘文件不够，必须证明 /admin/ 经 nginx 真的返回新资源名。
    # 拆分前 /admin 由客户 bundle 自己渲染，缺 nginx location 或缺 dist-admin 会
    # 降级到客户激活页而非报错，脚本会误判 SUCCESS。
    assert (
        'ADMIN_HTML=$(curl -fsS --max-time 20 "$PUBLIC_ORIGIN/admin/?release=$SHORT_SHA")' in script
    )
    assert 'grep -q "$EXPECTED_ADMIN_ASSET" <<< "$ADMIN_HTML"' in script
    assert "ADMIN_SITE=%s\\nADMIN_ASSET=%s" in script
    # 暂存残留会让 cp -a 混入上一轮字节，precheck 必须拒绝
    assert '! -e "$STAGE_ADMIN_SITE"' in script
