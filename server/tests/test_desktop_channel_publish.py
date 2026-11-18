"""桌面静态发布器：本地签名制品、临时 channel 与假 curl，禁止访问网络。"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "deploy" / "customer-desktop-release.sh"
BASH = shutil.which("bash") or "bash"
ORIGIN = "https://release.example.invalid:8443"
PLATFORMS = ("windows-x86_64", "darwin-aarch64", "darwin-x86_64")


def _json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def signing_key() -> tuple[Ed25519PrivateKey, str]:
    key = Ed25519PrivateKey.generate()
    raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    packet = b"Ed" + b"fixture1" + raw
    envelope = b"untrusted comment: ephemeral test public key\n" + base64.b64encode(packet) + b"\n"
    return key, base64.b64encode(envelope).decode()


def _signature(payload: bytes, key: Ed25519PrivateKey, algorithm: bytes) -> str:
    message = hashlib.blake2b(payload).digest() if algorithm == b"ED" else payload
    signature = key.sign(message)
    packet = algorithm + b"fixture1" + signature
    comment = b"timestamp:1\tfile:fixture"
    envelope = (
        b"untrusted comment: ephemeral test signature\n"
        + base64.b64encode(packet)
        + b"\ntrusted comment: "
        + comment
        + b"\n"
        + base64.b64encode(key.sign(signature + comment))
        + b"\n"
    )
    return base64.b64encode(envelope).decode()


def save_bundle(release: Path, stable: dict, manifest: dict) -> None:
    _json(release / "stable.json", stable)
    _json(release / "release-manifest.json", manifest)
    names = (
        [asset["path"] for asset in manifest["assets"]] + ["stable.json", "release-manifest.json"]
        if "assets" in manifest
        else [manifest["artifact"], manifest["updater"]["signatureArtifact"]]
    )
    (release / "SHA256SUMS.txt").write_text(
        "".join(f"{_sha(release / name)} *{name}\n" for name in names), encoding="utf-8"
    )


def sync_hashes(release: Path, manifest: dict) -> None:
    manifest["sha256"] = _sha(release / manifest["artifact"])
    manifest["updater"]["signatureSha256"] = _sha(
        release / manifest["updater"]["signatureArtifact"]
    )
    for asset in manifest.get("assets", []):
        asset["sha256"] = _sha(release / asset["path"])
    for record in manifest.get("platforms", {}).values():
        for field, hash_field in (
            ("artifact", "sha256"),
            ("updateArtifact", "updateSha256"),
            ("signatureArtifact", "signatureSha256"),
        ):
            record[hash_field] = _sha(release / record[field])


def make_release(
    root: Path,
    version: str = "2.4.0",
    *,
    modern: bool = True,
    algorithm: bytes = b"ED",
    key: Ed25519PrivateKey | None = None,
    platforms: tuple[str, ...] | None = None,
) -> tuple[Path, dict, dict, str]:
    generated_key, public_key = signing_key()
    key = key or generated_key
    raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    public_key = base64.b64encode(
        b"untrusted comment: ephemeral test public key\n"
        + base64.b64encode(b"Edfixture1" + raw)
        + b"\n"
    ).decode()
    release = root / version
    release.mkdir(parents=True)
    stable = {"version": version, "platforms": {}}
    assets, platforms_meta = [], {}
    # platforms=None 沿用默认集合（schema2 三平台 / legacy 仅 Windows）；显式传入
    # 允许构造任意子集，包括触发"非空子集"拒绝的空集。
    selected = platforms if platforms is not None else (PLATFORMS if modern else PLATFORMS[:1])
    for platform in selected:
        update = (
            "setup.exe"
            if platform == "windows-x86_64"
            else f"众墅之家_{version}_{platform}.app.tar.gz"
        )
        installer = update if platform == "windows-x86_64" else f"众墅之家_{version}_{platform}.dmg"
        payload = f"public test payload {version} {platform}".encode()
        (release / update).write_bytes(payload)
        signature = _signature(payload, key, algorithm)
        (release / f"{update}.sig").write_text(signature, encoding="utf-8")
        if installer != update:
            (release / installer).write_bytes(f"fixture DMG {version} {platform}".encode())
        stable["platforms"][platform] = {
            "url": f"{ORIGIN}/downloads/customer-cloud/{version}/{quote(update, safe='')}",
            "signature": signature,
        }
        platforms_meta[platform] = {
            "artifact": installer,
            "sha256": _sha(release / installer),
            "updateArtifact": update,
            "updateSha256": _sha(release / update),
            "signatureArtifact": f"{update}.sig",
            "signatureSha256": _sha(release / f"{update}.sig"),
            "signature": {"status": "Valid"},
        }
        for name, role in [(update, "update"), (f"{update}.sig", "signature")] + (
            [(installer, "installer")] if installer != update else []
        ):
            assets.append(
                {"path": name, "sha256": _sha(release / name), "role": role, "platform": platform}
            )
    manifest = {
        "channel": "signed-release",
        "version": version,
        "platform": "windows-x86_64",
        "artifact": "setup.exe",
        # 空子集夹具不产生 Windows 文件；这些 legacy 字段在子集校验阶段即被
        # 拒绝，占位哈希只为让夹具能构造出来。
        "sha256": (_sha(release / "setup.exe") if (release / "setup.exe").exists() else "0" * 64),
        "signature": {"status": "Valid"},
        "updater": {
            "endpoint": ORIGIN + "/downloads/customer-cloud/stable.json",
            "signatureArtifact": "setup.exe.sig",
            "signatureSha256": (
                _sha(release / "setup.exe.sig")
                if (release / "setup.exe.sig").exists()
                else "0" * 64
            ),
            "manifestArtifact": "stable.json",
        },
    }
    if modern:
        manifest.update(
            schemaVersion=2, source_sha="a" * 40, platforms=platforms_meta, assets=assets
        )
    save_bundle(release, stable, manifest)
    return release, stable, manifest, public_key


class Publisher:
    def __init__(self, root: Path, public_key: str):
        self.root = root
        self.channel = root / "channel"
        self.channel.mkdir(parents=True)
        self.bin = root / "bin"
        self.bin.mkdir()
        self.calls = root / "curl-calls.txt"
        self.env = {
            **os.environ,
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "VIDEO_REPLICA_DESKTOP_CHANNEL_ROOT": str(self.channel),
            "VIDEO_REPLICA_DESKTOP_UPDATER_PUBLIC_KEY": public_key,
            "TEST_CHANNEL_ROOT": str(self.channel),
            "TEST_CURL_CALLS": str(self.calls),
            "TEST_EXPECTED_ENDPOINT": ORIGIN + "/downloads/customer-cloud/stable.json",
            "TMPDIR": str(root),
            "PYTHONUTF8": "1",
        }
        # Windows 只有 Git Bash。其余命令和密码学均执行真实工具；锁并发用例只在
        # Linux 真 flock 上运行，Windows 适配器仅检查脚本发出的锁参数，不声称验证锁。
        if os.name == "nt":
            executable = sys.executable.replace("\\", "/").replace("'", "'\\''")
            self._tool("python3", f"#!/usr/bin/env bash\nexec '{executable}' \"$@\"\n")
            self._tool("flock", '#!/usr/bin/env bash\n[[ "$1" == -x && "$2" == 9 ]]\n')
        self._tool(
            "curl",
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            '[[ "$#" == 6 && "$1" == -fsS && "$2" == --max-time && "$3" == 30 '
            '&& "$4" == "$TEST_EXPECTED_ENDPOINT" && "$5" == -o ]] || exit 97\n'
            'printf "%s\\n" "$4" >> "$TEST_CURL_CALLS"\n'
            'if [[ -n "${TEST_CURL_BLOCK_MARKER:-}" ]]; then\n'
            '  printf ready > "$TEST_CURL_BLOCK_MARKER"\n'
            '  while [[ ! -f "$TEST_CURL_RELEASE" ]]; do sleep 0.05; done\n'
            "fi\n"
            '[[ "${TEST_CURL_MODE:-}" != fail ]] || exit 22\n'
            'cp "${TEST_SERVED_FILE:-$TEST_CHANNEL_ROOT/stable.json}" "$6"\n',
        )

    def _tool(self, name: str, content: str) -> None:
        path = self.bin / name
        path.write_text(content, encoding="utf-8")
        path.chmod(0o755)

    def command(self, *args: str) -> list[str]:
        # Windows CreateProcess 会优先找到 System32 的 WSL bash，必须传 Git Bash 绝对路径。
        return [BASH, str(SCRIPT), *args]

    def run(self, *args: str, extra_env: dict | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(
            self.command(*args),
            env=self.env | (extra_env or {}),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
            check=False,
        )

    def publish(self, release: Path, **kwargs) -> subprocess.CompletedProcess:
        return self.run("--release-dir", str(release), **kwargs)

    def assert_clean(self) -> None:
        assert not list(self.channel.glob(".staging-*"))
        assert not [p for p in self.channel.glob(".publish-*") if p.is_dir()]


@pytest.fixture
def bundle(tmp_path: Path):
    release, stable, manifest, public_key = make_release(tmp_path / "release")
    return Publisher(tmp_path / "publisher", public_key), release, stable, manifest


@pytest.mark.parametrize("modern", [False, True], ids=["legacy-windows", "three-platform"])
@pytest.mark.parametrize("algorithm", [b"Ed", b"ED"], ids=["raw-ed25519", "prehashed-ed25519"])
def test_publish_complete_signed_bundle_and_verify_idempotency(
    tmp_path: Path, modern: bool, algorithm: bytes
):
    release, _, _, key = make_release(tmp_path / "release", modern=modern, algorithm=algorithm)
    publisher = Publisher(tmp_path / "publisher", key)
    first = publisher.publish(release)
    assert first.returncode == 0, first.stdout + first.stderr
    target = publisher.channel / "2.4.0"
    assert {p.name for p in target.iterdir()} == {p.name for p in release.iterdir()}
    before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in target.iterdir()}
    second = publisher.publish(release)
    assert second.returncode == 0, second.stdout + second.stderr
    assert "verified identical assets" in second.stdout
    assert {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in target.iterdir()} == before
    assert len(publisher.calls.read_text().splitlines()) == 2
    publisher.assert_clean()


def test_explicit_channel_root_overrides_env_and_legacy_site(bundle, tmp_path: Path):
    publisher, release, _, _ = bundle
    legacy_site = tmp_path / "site"
    ignored = tmp_path / "ignored-channel"
    publisher.env.update(
        VIDEO_REPLICA_DESKTOP_CHANNEL_ROOT=str(ignored), VIDEO_REPLICA_SITE_ROOT=str(legacy_site)
    )
    result = publisher.run("--channel-root", str(publisher.channel), "--release-dir", str(release))
    assert result.returncode == 0, result.stdout + result.stderr
    assert (publisher.channel / "2.4.0").is_dir()
    assert not ignored.exists()
    assert not legacy_site.exists()


def test_two_platform_subset_publishes_with_full_per_platform_contract(tmp_path: Path):
    # 2.6.8 发行形态：windows-x86_64 + darwin-aarch64 子集（macOS x64 端点问题
    # 未解）。子集放行绝不削弱单平台校验：每平台 update/signature 配对、URL
    # 对应、Windows exe 唯一、Mac DMG+tar.gz 与资产计数全部照常执行。
    release, _, _, key = make_release(
        tmp_path / "release", platforms=("windows-x86_64", "darwin-aarch64")
    )
    publisher = Publisher(tmp_path / "publisher", key)
    result = publisher.publish(release)
    assert result.returncode == 0, result.stdout + result.stderr
    target = publisher.channel / "2.4.0"
    assert {p.name for p in target.iterdir()} == {p.name for p in release.iterdir()}
    served_names = {p.name for p in target.iterdir()}
    assert sum(1 for name in served_names if name.endswith(".exe.sig")) == 1
    assert sum(1 for name in served_names if name.endswith(".dmg")) == 1
    assert sum(1 for name in served_names if name.endswith(".app.tar.gz.sig")) == 1
    assert not any("darwin-x86_64" in name for name in served_names)
    before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in target.iterdir()}
    rerun = publisher.publish(release)
    assert rerun.returncode == 0, rerun.stdout + rerun.stderr
    assert "verified identical assets" in rerun.stdout
    assert {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in target.iterdir()} == before
    assert len(publisher.calls.read_text().splitlines()) == 2
    publisher.assert_clean()


def test_empty_platform_subset_missing_updater_assets_is_rejected(tmp_path: Path):
    # 空子集等于没有任何可升级路径：缺少全部 updater 资产时必须在切换前拒绝。
    release, _, _, key = make_release(tmp_path / "release", platforms=())
    publisher = Publisher(tmp_path / "publisher", key)
    previous = b'{"version":"2.3.0"}\n'
    (publisher.channel / "stable.json").write_bytes(previous)
    result = publisher.publish(release)
    assert result.returncode != 0, result.stdout + result.stderr
    # 空集可能先触发 endpoint_of 的守卫、也可能触发 schema2 的子集守卫；
    # 两者消息都以 "stable platforms" 开头，且都发生在切换之前。
    assert "stable platforms" in result.stdout + result.stderr
    assert (publisher.channel / "stable.json").read_bytes() == previous
    assert not (publisher.channel / "2.4.0").exists()
    assert not publisher.calls.exists()
    publisher.assert_clean()


@pytest.mark.parametrize(
    "mutation",
    [
        "missing-dmg",
        "traversal",
        "checksum-traversal",
        "extra-file",
        "hash",
        "origin",
        "version-url",
        "platform-url",
        "platform-record",
        "metadata-hash",
        "duplicate-checksum",
        "missing-checksum",
        "duplicate-json",
    ],
)
def test_invalid_bundle_never_changes_live_channel(bundle, mutation: str):
    publisher, release, stable, manifest = bundle
    previous = b'{"version":"2.3.0"}\n'
    (publisher.channel / "stable.json").write_bytes(previous)
    if mutation == "missing-dmg":
        next(release.glob("*.dmg")).unlink()
    elif mutation == "traversal":
        manifest["assets"][0]["path"] = "../setup.exe"
        _json(release / "release-manifest.json", manifest)
    elif mutation == "checksum-traversal":
        with (release / "SHA256SUMS.txt").open("a", encoding="utf-8") as stream:
            stream.write("0" * 64 + " *../outside\n")
    elif mutation == "extra-file":
        (release / "secret.key").write_text("unlisted fixture", encoding="utf-8")
    elif mutation == "hash":
        (release / "setup.exe").write_bytes(b"tampered")
    elif mutation == "origin":
        stable["platforms"]["darwin-aarch64"]["url"] = stable["platforms"]["darwin-aarch64"][
            "url"
        ].replace("release.example.invalid", "elsewhere.example.invalid")
        save_bundle(release, stable, manifest)
    elif mutation == "version-url":
        stable["platforms"]["darwin-aarch64"]["url"] = stable["platforms"]["darwin-aarch64"][
            "url"
        ].replace("2.4.0", "2.3.0")
        save_bundle(release, stable, manifest)
    elif mutation == "platform-url":
        stable["platforms"]["darwin-aarch64"]["url"] = stable["platforms"]["darwin-x86_64"]["url"]
        save_bundle(release, stable, manifest)
    elif mutation == "platform-record":
        manifest["platforms"]["darwin-aarch64"]["updateSha256"] = "0" * 64
        save_bundle(release, stable, manifest)
    elif mutation == "metadata-hash":
        stable["notes"] = "changed after hash"
        _json(release / "stable.json", stable)
    elif mutation == "duplicate-json":
        path = release / "release-manifest.json"
        path.write_text(
            path.read_text(encoding="utf-8").replace(
                '"schemaVersion": 2', '"schemaVersion": 2, "schemaVersion": 2'
            ),
            encoding="utf-8",
        )
    elif mutation == "missing-checksum":
        path = release / "SHA256SUMS.txt"
        path.write_text(
            "\n".join(path.read_text(encoding="utf-8").splitlines()[1:]) + "\n", encoding="utf-8"
        )
    else:
        path = release / "SHA256SUMS.txt"
        with path.open("a", encoding="utf-8") as stream:
            stream.write(path.read_text(encoding="utf-8").splitlines()[0] + "\n")
    result = publisher.publish(release)
    assert result.returncode != 0, result.stdout + result.stderr
    assert (publisher.channel / "stable.json").read_bytes() == previous
    assert not (publisher.channel / "2.4.0").exists()
    assert not publisher.calls.exists()
    assert not list(publisher.channel.glob("stable.json.*.bak"))
    publisher.assert_clean()


@pytest.mark.parametrize(
    "mutation", ["data", "global", "comment", "keyid", "algorithm", "wrong-key", "sidecar"]
)
def test_signature_must_verify_even_after_all_hashes_are_recomputed(bundle, mutation: str):
    publisher, release, stable, manifest = bundle
    platform = "darwin-aarch64"
    name = manifest["platforms"][platform]["signatureArtifact"]
    lines = base64.b64decode((release / name).read_text().strip()).decode().splitlines()
    if mutation in {"data", "keyid", "algorithm"}:
        packet = bytearray(base64.b64decode(lines[1]))
        packet[{"data": 10, "keyid": 2, "algorithm": 1}[mutation]] ^= 1
        lines[1] = base64.b64encode(packet).decode()
    elif mutation == "global":
        packet = bytearray(base64.b64decode(lines[3]))
        packet[0] ^= 1
        lines[3] = base64.b64encode(packet).decode()
    elif mutation == "comment":
        lines[2] += " tampered"
    elif mutation == "wrong-key":
        _, publisher.env["VIDEO_REPLICA_DESKTOP_UPDATER_PUBLIC_KEY"] = signing_key()
    text = base64.b64encode(("\n".join(lines) + "\n").encode()).decode()
    (release / name).write_text(text, encoding="utf-8")
    if mutation != "sidecar":
        stable["platforms"][platform]["signature"] = text
    else:
        stable["platforms"][platform]["signature"] = "nonempty is not a signature"
    sync_hashes(release, manifest)
    save_bundle(release, stable, manifest)
    result = publisher.publish(release)
    assert result.returncode != 0, result.stdout + result.stderr
    assert not (publisher.channel / "stable.json").exists()
    assert not (publisher.channel / "2.4.0").exists()
    publisher.assert_clean()


@pytest.mark.parametrize("mutation", ["lf", "crlf", "space", "extra-padding", "stable-only-lf"])
def test_noncanonical_outer_signature_is_rejected_before_switch(bundle, mutation: str):
    publisher, release, stable, manifest = bundle
    previous = b'{"version":"2.3.0"}\n'
    (publisher.channel / "stable.json").write_bytes(previous)
    platform = "darwin-aarch64"
    name = manifest["platforms"][platform]["signatureArtifact"]
    original = stable["platforms"][platform]["signature"]
    signature = {
        "lf": original + "\n",
        "crlf": original + "\r\n",
        "space": " " + original,
        "extra-padding": original + "=",
        "stable-only-lf": original + "\n",
    }[mutation]
    stable["platforms"][platform]["signature"] = signature
    if mutation != "stable-only-lf":
        (release / name).write_bytes(signature.encode("utf-8"))
    sync_hashes(release, manifest)
    save_bundle(release, stable, manifest)
    result = publisher.publish(release)
    assert result.returncode != 0, result.stdout + result.stderr
    assert "canonical" in result.stdout or "stable signature" in result.stdout
    assert (publisher.channel / "stable.json").read_bytes() == previous
    assert not (publisher.channel / "2.4.0").exists()
    assert not publisher.calls.exists()
    publisher.assert_clean()


@pytest.mark.parametrize("failure", ["http", "same-version-wrong-content"])
def test_public_readback_failure_restores_exact_previous_manifest(bundle, failure: str):
    publisher, release, stable, _ = bundle
    previous = b'{"version":"2.3.0","notes":"preserve exact bytes"}\n'
    (publisher.channel / "stable.json").write_bytes(previous)
    if failure == "http":
        publisher.env["TEST_CURL_MODE"] = "fail"
    else:
        served = publisher.root / "wrong-served.json"
        stable["platforms"]["windows-x86_64"]["signature"] = "wrong but same version"
        _json(served, stable)
        publisher.env["TEST_SERVED_FILE"] = str(served)
    publisher.env["TAURI_SIGNING_PRIVATE_KEY"] = "test-secret-must-not-appear-in-output"
    result = publisher.publish(release)
    assert result.returncode != 0
    assert (publisher.channel / "stable.json").read_bytes() == previous
    assert (publisher.channel / "stable.json.2.3.0.bak").read_bytes() == previous
    assert (publisher.channel / "2.4.0").is_dir()
    assert "test-secret-must-not-appear" not in result.stdout + result.stderr
    assert "command=" not in result.stdout + result.stderr
    publisher.assert_clean()


def test_failed_first_publish_withdraws_stable_and_can_retry(bundle):
    publisher, release, _, _ = bundle
    failed = publisher.publish(release, extra_env={"TEST_CURL_MODE": "fail"})
    assert failed.returncode != 0
    assert not (publisher.channel / "stable.json").exists()
    target = publisher.channel / "2.4.0"
    before = {p.name: p.stat().st_mtime_ns for p in target.iterdir()}
    retried = publisher.publish(release)
    assert retried.returncode == 0, retried.stdout + retried.stderr
    assert {p.name: p.stat().st_mtime_ns for p in target.iterdir()} == before


def test_same_version_different_metadata_is_refused_without_overwrite(bundle):
    publisher, release, stable, manifest = bundle
    assert publisher.publish(release).returncode == 0
    original = (publisher.channel / "stable.json").read_bytes()
    original_files = {p.name: p.read_bytes() for p in (publisher.channel / "2.4.0").iterdir()}
    stable["notes"] = "same version, new identity"
    save_bundle(release, stable, manifest)
    result = publisher.publish(release)
    assert result.returncode != 0
    assert (publisher.channel / "stable.json").read_bytes() == original
    assert {
        p.name: p.read_bytes() for p in (publisher.channel / "2.4.0").iterdir()
    } == original_files
    publisher.assert_clean()


def test_existing_version_corruption_blocks_idempotent_rerun(bundle):
    publisher, release, _, _ = bundle
    assert publisher.publish(release).returncode == 0
    original = (publisher.channel / "stable.json").read_bytes()
    installer = publisher.channel / "2.4.0" / "setup.exe"
    installer.write_bytes(b"corrupt existing version")
    result = publisher.publish(release)
    assert result.returncode != 0
    assert installer.read_bytes() == b"corrupt existing version"
    assert (publisher.channel / "stable.json").read_bytes() == original
    publisher.assert_clean()


def test_rollback_snapshots_current_manifest_and_recovers_on_readback_failure(tmp_path: Path):
    key, public = signing_key()
    publisher = Publisher(tmp_path / "publisher", public)
    releases = [
        make_release(tmp_path / "release", version, key=key)[0] for version in ("2.3.0", "2.4.0")
    ]
    for release in releases:
        result = publisher.publish(release)
        assert result.returncode == 0, result.stdout + result.stderr
    current = (publisher.channel / "stable.json").read_bytes()
    # 发布完当前版本后通常没有该版本的 .bak；失败回滚必须恢复刚才的实时快照。
    assert not (publisher.channel / "stable.json.2.4.0.bak").exists()
    failed = publisher.run("--rollback", "2.3.0", extra_env={"TEST_CURL_MODE": "fail"})
    assert failed.returncode != 0
    assert (publisher.channel / "stable.json").read_bytes() == current
    success = publisher.run("--rollback", "2.3.0")
    assert success.returncode == 0, success.stdout + success.stderr
    assert (publisher.channel / "stable.json").read_bytes() == (
        releases[0] / "stable.json"
    ).read_bytes()
    assert (publisher.channel / "stable.json.2.4.0.bak").read_bytes() == current
    assert all((publisher.channel / release.name).is_dir() for release in releases)
    publisher.assert_clean()


@pytest.mark.skipif(
    os.name == "nt", reason="Windows Git Bash 无真实 flock；其余集成测试使用明确标注的锁适配器"
)
def test_flock_serializes_publish_through_public_readback(bundle):
    publisher, release, _, _ = bundle
    if shutil.which("flock") is None:
        pytest.skip("需要服务器 flock")
    marker, unblock = publisher.root / "curl-blocked", publisher.root / "curl-unblock"
    first = subprocess.Popen(
        publisher.command("--release-dir", str(release)),
        env=publisher.env
        | {"TEST_CURL_BLOCK_MARKER": str(marker), "TEST_CURL_RELEASE": str(unblock)},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    second = None
    try:
        deadline = time.monotonic() + 20
        while not marker.exists() and time.monotonic() < deadline and first.poll() is None:
            time.sleep(0.05)
        assert marker.exists(), first.communicate(timeout=2)
        second = subprocess.Popen(
            publisher.command("--release-dir", str(release)),
            env=publisher.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        time.sleep(0.25)
        assert second.poll() is None
        assert len(publisher.calls.read_text().splitlines()) == 1
        unblock.touch()
        first_output = first.communicate(timeout=30)
        second_output = second.communicate(timeout=30)
        assert first.returncode == 0, first_output
        assert second.returncode == 0, second_output
        assert len(publisher.calls.read_text().splitlines()) == 2
    finally:
        unblock.touch()
        for process in (first, second):
            if process is not None and process.poll() is None:
                process.kill()
                process.communicate(timeout=5)
