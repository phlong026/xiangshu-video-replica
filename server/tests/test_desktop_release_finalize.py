"""离线发行合同：临时测试密钥只存于 Node 内存，不需要 pwsh、PG 或生产私钥。"""

import base64
import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
CORE = REPO / "scripts/release/finalize-customer-release.mjs"
COLLECTOR = REPO / "scripts/release/collect-desktop-artifacts.mjs"
ORIGIN = "https://video.zszhj.cn"


def run_node(code: str, *args: str) -> subprocess.CompletedProcess[str]:
    # 即使签名机执行测试，也绝不把真实签名环境送进测试进程。
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("TAURI_SIGNING_", "VIDEO_REPLICA_RELEASE_SIGN_"))
    }
    return subprocess.run(
        ["node", "--input-type=module", "-e", code, CORE.as_uri(), *args],
        cwd=REPO,
        env=env,
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
        timeout=30,
    )


# 用标准 crypto 构造 Ed/ED 测试向量；真实 Tauri 2.6.7 公共制品另做只读互通核验。
FIXTURE = r"""
import { generateKeyPairSync, sign, createHash } from "node:crypto";
import { mkdir, writeFile, readFile, unlink } from "node:fs/promises";
import { join } from "node:path";
const core = await import(process.argv[1]);
const root = process.argv[2],
  fault = process.argv[3],
  algorithm = process.argv[4];
const { publicKey, privateKey } = generateKeyPairSync("ed25519");
const keyId = Buffer.from("0102030405060708", "hex");
const text64 = (text) => Buffer.from(text).toString("base64");
const pub = text64(
  "untrusted comment: test only\n" +
    Buffer.concat([
      Buffer.from("Ed"),
      keyId,
      publicKey.export({ format: "der", type: "spki" }).subarray(-32),
    ]).toString("base64") +
    "\n",
);
function signature(data, key = privateKey, id = keyId) {
  const digest =
    algorithm === "ED" ? createHash("blake2b512").update(data).digest() : data;
  const sig = sign(null, digest, key);
  const comment = "timestamp:1700000000\tfile:test";
  return text64(
    "untrusted comment: test signature\n" +
      Buffer.concat([Buffer.from(algorithm), id, sig]).toString("base64") +
      "\ntrusted comment: " +
      comment +
      "\n" +
      sign(null, Buffer.concat([sig, Buffer.from(comment)]), key).toString("base64") +
      "\n",
  );
}
const origin = "https://video.zszhj.cn",
  version = "2.6.8",
  commit = "a".repeat(40);
const windows = join(root, "windows"),
  mac = join(root, "mac"),
  stage = join(root, "stage");
await mkdir(windows, { recursive: true });
await mkdir(mac);
const exe = "众墅之家_2.6.8_x64-setup.exe",
  exeData = Buffer.from("test installer");
const outerWhitespace = fault.endsWith("-lf") ? "\n" : "\r\n";
const exeSig = signature(exeData) + (
  fault.startsWith("windows-sig-") ? outerWhitespace : ""
);
const endpoint = origin + "/downloads/customer-cloud/stable.json";
const stable = {
  version,
  notes: "保留发行说明",
  pub_date: "2026-11-17T00:00:00Z",
  platforms: {
    "windows-x86_64": {
      signature: exeSig,
      url: origin + "/downloads/customer-cloud/" + version + "/" + exe,
    },
  },
};
const win = {
  channel: "signed-release",
  version,
  platform: "windows-x86_64",
  source_sha: commit,
  artifact: exe,
  sha256: core.sha256(exeData).toUpperCase(),
  signature: { status: "Valid", thumbprint: "B".repeat(40), subject: "Test Certificate" },
  updater: {
    endpoint,
    signatureArtifact: exe + ".sig",
    signatureSha256: core.sha256(Buffer.from(exeSig)),
    manifestArtifact: "stable.json",
  },
};
if (fault.startsWith("windows-stable-sig-")) {
  stable.platforms["windows-x86_64"].signature += outerWhitespace;
}
await writeFile(join(windows, exe), exeData);
await writeFile(join(windows, exe + ".sig"), exeSig);
await writeFile(join(windows, "stable.json"), JSON.stringify(stable));
await writeFile(
  join(windows, "SHA256SUMS.txt"),
  "\uFEFF" +
    win.sha256 +
    " *" +
    exe +
    "\r\n" +
    win.updater.signatureSha256 +
    " *" +
    exe +
    ".sig\r\n",
);
if (fault === "windows-unsigned") win.signature.status = "NotSigned";
if (fault === "windows-commit") delete win.source_sha;
if (fault === "windows-staging")
  win.updater.endpoint =
    "https://staging.example.invalid/downloads/customer-cloud/stable.json";
if (fault === "windows-digest") win.sha256 = "f".repeat(64);
if (fault === "windows-fake-signature") {
  const fake = signature(Buffer.from("some other installer"));
  await writeFile(join(windows, exe + ".sig"), fake);
  stable.platforms["windows-x86_64"].signature = fake;
  await writeFile(join(windows, "stable.json"), JSON.stringify(stable));
  win.updater.signatureSha256 = core.sha256(Buffer.from(fake));
  await writeFile(
    join(windows, "SHA256SUMS.txt"),
    win.sha256 + " *" + exe + "\n" + win.updater.signatureSha256 + " *" + exe + ".sig\n",
  );
}
await writeFile(join(windows, "release-manifest.json"), JSON.stringify(win));
const pins = {};
for (const [ci, config] of Object.entries(core.macPlatforms)) {
  const folder = join(mac, "desktop-" + ci);
  await mkdir(folder);
  const dmg = "VideoReplica_" + version + "_" + ci + ".dmg",
    archive = dmg.slice(0, -4) + ".app.tar.gz";
  const dmgData = Buffer.from("test dmg " + ci),
    archiveData = Buffer.from("test archive " + ci);
  const manifest = {
    version,
    source_sha: commit,
    platform: ci,
    ...config,
    artifact: dmg,
    sha256: core.sha256(dmgData),
    updater_artifact: archive,
    updater_sha256: core.sha256(archiveData),
    signature: "ad-hoc/unnotarized",
    api_base_url: origin,
    updater_endpoint: endpoint,
    updater_pubkey: pub,
  };
  // config.platform 是 updater 平台名，CI manifest.platform 仍是 macos-*。
  manifest.platform = ci;
  await writeFile(join(folder, dmg), dmgData);
  await writeFile(join(folder, archive), archiveData);
  await writeFile(join(folder, "RELEASE-CHANNEL.txt"), "internal-test-unsigned\n");
  await writeFile(
    join(folder, "SHA256SUMS.txt"),
    manifest.sha256 + "  " + dmg + "\n" + manifest.updater_sha256 + "  " + archive + "\n",
  );
  if (ci === "macos-arm64") {
    if (fault === "mac-commit") manifest.source_sha = "b".repeat(40);
    if (fault === "mac-version") manifest.version = "2.6.7";
    if (fault === "mac-target") manifest.target = "x86_64-apple-darwin";
    if (fault === "mac-architecture") manifest.architecture = "x86_64";
    if (fault === "mac-pubkey") manifest.updater_pubkey = "wrong production key";
    if (fault === "mac-staging")
      manifest.updater_endpoint =
        "https://staging.example.invalid/downloads/customer-cloud/stable.json";
    if (fault === "mac-path") manifest.updater_artifact = "../escape.app.tar.gz";
    if (fault === "mac-digest") await writeFile(join(folder, archive), "tampered");
    if (fault === "mac-missing") await unlink(join(folder, archive));
    if (fault === "mac-extra-script")
      await writeFile(
        join(folder, "post-download.mjs"),
        'throw new Error("MUST NOT EXECUTE")',
      );
  }
  const bytes = JSON.stringify(manifest);
  await writeFile(join(folder, "manifest.json"), bytes);
  pins[ci] = core.sha256(Buffer.from(bytes));
}
const plan = await core.prepareRelease(
  {
    windowsReleaseDir: windows,
    macArtifactsDir: mac,
    stageDir: stage,
    expectedCommit: commit,
    version,
    origin,
    macArm64ManifestSha256: fault === "mac-pin" ? "0".repeat(64) : pins["macos-arm64"],
    macX64ManifestSha256: pins["macos-x86_64"],
  },
  pub,
);
for (const archive of plan.macArchives) {
  await writeFile(archive + ".sig", signature(await readFile(archive)));
}
const archive = plan.macArchives[0];
if (fault.startsWith("stage-sig-outer-")) {
  await writeFile(archive + ".sig", signature(await readFile(archive)) + outerWhitespace);
}
if (fault === "stage-payload") await writeFile(archive, "tampered staged archive");
if (fault === "stage-win-payload")
  await writeFile(plan.windowsInstaller, "tampered staged installer");
if (fault === "stage-extra-script")
  await writeFile(
    join(stage, "artifact-tool.mjs"),
    'throw new Error("MUST NOT EXECUTE")',
  );
if (fault === "stage-sig-nonempty")
  await writeFile(archive + ".sig", "not-empty-is-not-verification");
if (fault === "stage-sig-missing") await unlink(archive + ".sig");
if (fault === "stage-wrong-key") {
  await writeFile(
    archive + ".sig",
    signature(await readFile(archive), generateKeyPairSync("ed25519").privateKey),
  );
}
if (fault === "stage-wrong-key-id")
  await writeFile(
    archive + ".sig",
    signature(await readFile(archive), privateKey, Buffer.alloc(8)),
  );
if (fault === "stage-global" || fault === "stage-comment") {
  const lines = Buffer.from((await readFile(archive + ".sig", "utf8")).trim(), "base64")
    .toString()
    .trimEnd()
    .split("\n");
  if (fault === "stage-comment") lines[2] += "tampered";
  else {
    const bytes = Buffer.from(lines[3], "base64");
    bytes[0] ^= 1;
    lines[3] = bytes.toString("base64");
  }
  await writeFile(archive + ".sig", text64(lines.join("\n") + "\n"));
}
await core.finalizeRelease(stage, pub);
console.log(stage);
"""


@pytest.mark.parametrize("algorithm", ["Ed", "ED"])
def test_three_platform_finalize_checks_all_assets(tmp_path: Path, algorithm: str) -> None:
    result = run_node(FIXTURE, str(tmp_path), "ok", algorithm)
    assert result.returncode == 0, result.stderr
    stage = tmp_path / "stage"
    manifest = json.loads((stage / "release-manifest.json").read_text(encoding="utf-8"))
    stable = json.loads((stage / "stable.json").read_text(encoding="utf-8"))
    assert manifest["schemaVersion"] == 2
    assert manifest["source_sha"] == "a" * 40
    assert manifest["platform"] == "windows-x86_64"
    assert manifest["signature"]["status"] == "Valid"
    assert stable["notes"] == "保留发行说明"
    assert set(stable["platforms"]) == {
        "windows-x86_64",
        "darwin-aarch64",
        "darwin-x86_64",
    }
    assert len(manifest["assets"]) == 8
    assert {asset["role"] for asset in manifest["assets"]} == {
        "installer",
        "update",
        "signature",
    }
    for platform, record in manifest["platforms"].items():
        signature = stable["platforms"][platform]["signature"]
        assert (stage / record["signatureArtifact"]).read_bytes() == signature.encode("ascii")
        decoded = base64.b64decode(signature, validate=True)
        assert base64.b64encode(decoded).decode("ascii") == signature
        if platform.startswith("darwin"):
            assert record["artifact"].endswith(".dmg")
            assert record["updateArtifact"].endswith(".app.tar.gz")
            assert stable["platforms"][platform]["url"].endswith(".app.tar.gz")
    sums = dict(
        (name, digest)
        for digest, name in (
            line.split("  ", 1)
            for line in (stage / "SHA256SUMS.txt").read_text(encoding="utf-8").splitlines()
        )
    )
    assert set(sums) == {asset["path"] for asset in manifest["assets"]} | {
        "stable.json",
        "release-manifest.json",
    }
    assert len(sums) == 10
    assert set(path.name for path in stage.iterdir()) == set(sums) | {"SHA256SUMS.txt"}
    for name, digest in sums.items():
        assert hashlib.sha256((stage / name).read_bytes()).hexdigest() == digest


@pytest.mark.parametrize(
    ("fault", "error"),
    [
        ("windows-unsigned", "Windows 正式发行出处或签名 gate"),
        ("windows-commit", "Windows 正式发行出处或签名 gate"),
        ("windows-staging", "Windows updater 配置"),
        ("windows-digest", "Windows 清单 SHA"),
        ("windows-fake-signature", "minisign 数据签名"),
        ("mac-commit", "Mac CI 出处"),
        ("mac-version", "Mac CI 出处"),
        ("mac-target", "Mac CI 出处"),
        ("mac-architecture", "Mac CI 出处"),
        ("mac-pubkey", "Mac CI 出处"),
        ("mac-staging", "Mac CI 出处"),
        ("mac-path", "不安全的制品路径"),
        ("mac-digest", "SHA256 不匹配"),
        ("mac-missing", "未知或缺失制品"),
        ("mac-extra-script", "未知或缺失制品"),
        ("mac-pin", "可信 SHA256 pin"),
        ("stage-payload", "暂存制品被修改"),
        ("stage-win-payload", "暂存制品被修改"),
        ("stage-extra-script", "未知或缺失制品"),
        ("stage-sig-nonempty", "minisign base64"),
        ("stage-sig-missing", "ENOENT"),
        ("stage-wrong-key", "minisign 数据签名"),
        ("stage-wrong-key-id", "公钥 ID 不匹配"),
        ("stage-global", "trusted comment 签名"),
        ("stage-comment", "trusted comment 签名"),
    ],
)
def test_finalize_rejects_untrusted_or_modified_inputs(
    tmp_path: Path, fault: str, error: str
) -> None:
    result = run_node(FIXTURE, str(tmp_path), fault, "ED")
    assert result.returncode != 0
    assert error in result.stderr
    assert not (tmp_path / "stage/stable.json").exists()


@pytest.mark.parametrize("algorithm", ["Ed", "ED"])
@pytest.mark.parametrize(
    ("fault", "error"),
    [
        ("windows-sig-lf", "minisign base64"),
        ("windows-sig-crlf", "minisign base64"),
        ("windows-stable-sig-lf", "Windows stable 清单不匹配"),
        ("windows-stable-sig-crlf", "Windows stable 清单不匹配"),
        ("stage-sig-outer-lf", "minisign base64"),
        ("stage-sig-outer-crlf", "minisign base64"),
    ],
)
def test_finalize_rejects_client_undecodable_outer_signature_whitespace(
    tmp_path: Path, algorithm: str, fault: str, error: str
) -> None:
    result = run_node(FIXTURE, str(tmp_path), fault, algorithm)
    assert result.returncode != 0
    assert error in result.stderr
    assert not (tmp_path / "stage/stable.json").exists()


@pytest.mark.parametrize(
    "origin",
    [
        "http://video.zszhj.cn",
        "https://staging.example.invalid",
        "https://localhost",
        "https://10.0.0.1",
        "https://172.16.0.1",
        "https://192.168.0.1",
        "https://127.0.0.1",
        "https://[::1]",
        "https://[::ffff:127.0.0.1]",
        "https://[fc00::1]",
        "https://user:password@video.zszhj.cn",
        "https://video.zszhj.cn/path",
        "https://video.zszhj.cn?query=1",
    ],
)
def test_production_origin_rejects_private_or_non_origin_urls(origin: str) -> None:
    result = run_node(
        "const {validateOrigin}=await import(process.argv[1]); validateOrigin(process.argv[2]);",
        origin,
    )
    assert result.returncode != 0
    assert "公共 HTTPS origin" in result.stderr


def test_macos_overlay_is_explicit_and_binary_gate_uses_production_key(
    tmp_path: Path,
) -> None:
    mac = json.loads((REPO / "client/src-tauri/tauri.macos.conf.json").read_text(encoding="utf-8"))
    win = json.loads(
        (REPO / "client/src-tauri/tauri.customer.conf.json").read_text(encoding="utf-8")
    )
    assert mac["plugins"]["updater"]["pubkey"] == win["plugins"]["updater"]["pubkey"]
    assert "staging.example.invalid" in mac["plugins"]["updater"]["endpoints"][0]
    result = run_node(
        r"""
import { writeFile } from "node:fs/promises";
const core = await import(process.argv[1]);
const config = await core.macosOverlay("https://video.zszhj.cn/");
if (config.bundle.createUpdaterArtifacts !== false)
  throw new Error("CI must not sign updater");
const updater = config.plugins.updater,
  file = process.argv[2];
await writeFile(file, updater.pubkey + "\0" + updater.endpoints[0]);
await core.verifyMacosApp(file, "https://video.zszhj.cn");
await writeFile(
  file,
  updater.pubkey + "\0" + updater.endpoints[0] + "\0staging.example.invalid",
);
let rejected = false;
try {
  await core.verifyMacosApp(file, "https://video.zszhj.cn");
} catch {
  rejected = true;
}
if (!rejected) throw new Error("staging accepted");
console.log(JSON.stringify(config));
""",
        str(tmp_path / "app-binary"),
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["plugins"]["updater"]["endpoints"] == [
        f"{ORIGIN}/downloads/customer-cloud/stable.json"
    ]


@pytest.mark.parametrize("missing_archive", [False, True])
def test_collector_requires_archive_for_explicit_mac_release(
    tmp_path: Path, missing_archive: bool
) -> None:
    overlay = tmp_path / "overlay.json"
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    dmg = bundle / "VideoReplica_2.6.8_macos-arm64.dmg"
    dmg.write_bytes(b"test dmg")
    if not missing_archive:
        (bundle / dmg.name.replace(".dmg", ".app.tar.gz")).write_bytes(b"test archive")
    code = r"""
import { writeFile } from "node:fs/promises";
import { spawnSync } from "node:child_process";
const core = await import(process.argv[1]);
await writeFile(
  process.argv[2],
  JSON.stringify(await core.macosOverlay("https://video.zszhj.cn")),
);
const result = spawnSync(
  process.execPath,
  [process.argv[3], "macos-arm64", process.argv[4], process.argv[5], process.argv[2]],
  {
    encoding: "utf8",
    env: { ...process.env, VITE_API_BASE_URL: "https://video.zszhj.cn/" },
  },
);
process.stdout.write(result.stdout);
process.stderr.write(result.stderr);
process.exitCode = result.status;
"""
    output = tmp_path / "collected"
    result = run_node(code, str(overlay), str(COLLECTOR), str(bundle), str(output))
    if missing_archive:
        assert result.returncode != 0
        assert not output.exists()
    else:
        assert result.returncode == 0, result.stderr
        manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["target"] == "aarch64-apple-darwin"
        assert manifest["architecture"] == "aarch64"
        assert manifest["api_base_url"] == ORIGIN
        assert manifest["updater_endpoint"] == f"{ORIGIN}/downloads/customer-cloud/stable.json"
        assert manifest["updater_artifact"].endswith(".app.tar.gz")
        assert manifest["updater_sha256"] == hashlib.sha256(b"test archive").hexdigest()


def test_workflow_and_signing_driver_keep_private_material_local() -> None:
    workflow = (REPO / ".github/workflows/desktop-build.yml").read_text(encoding="utf-8")
    driver = (REPO / "scripts/release/finalize-customer-release.ps1").read_text(encoding="utf-8")
    assert "macos-overlay" in workflow
    assert 'config_args+=(--config "$RUNNER_TEMP/customer-macos-release.json")' in workflow
    assert "verify-macos-app" in workflow
    assert "TAURI_SIGNING_" not in workflow
    assert "Get-AuthenticodeSignature -LiteralPath $plan.windowsInstaller" in driver
    assert "$signature.Status -ne 'Valid'" in driver
    assert "$signature.SignerCertificate.Thumbprint -ne $plan.windowsThumbprint" in driver
    assert "signer sign $archive" in driver
    assert "prepare-desktop-release.py" not in driver
    assert "npx" not in driver
