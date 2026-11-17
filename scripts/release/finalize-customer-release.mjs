import { spawnSync } from "node:child_process";
import { createHash, createPublicKey, verify } from "node:crypto";
import {
  copyFile,
  lstat,
  mkdir,
  readFile,
  readdir,
  unlink,
  writeFile,
} from "node:fs/promises";
import { BlockList, isIP } from "node:net";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { parseArgs } from "node:util";

const repoRoot = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const planName = ".finalize-plan.json";
export const macPlatforms = {
  "macos-arm64": {
    platform: "darwin-aarch64",
    target: "aarch64-apple-darwin",
    architecture: "aarch64",
  },
  "macos-x86_64": {
    platform: "darwin-x86_64",
    target: "x86_64-apple-darwin",
    architecture: "x86_64",
  },
};
const shaPattern = /^[a-f0-9]{64}$/;
export const sha256 = (data) => createHash("sha256").update(data).digest("hex");
const jsonText = (data) => `${JSON.stringify(data, null, 2)}\n`;
const assert = (condition, message) => {
  if (!condition) throw new Error(message);
};

export async function productionPublicKey() {
  const config = JSON.parse(
    await readFile(join(repoRoot, "client/src-tauri/tauri.customer.conf.json"), "utf8"),
  );
  const key = config.plugins.updater.pubkey;
  decodePublicKey(key);
  return key;
}

export function validateOrigin(value) {
  const result = spawnSync(
    process.execPath,
    [join(repoRoot, "scripts/require_customer_api_base.mjs")],
    {
      cwd: repoRoot,
      encoding: "utf8",
      env: { ...process.env, VITE_API_BASE_URL: value },
    },
  );
  assert(result.status === 0, "发行 origin 必须是公共 HTTPS origin");
  const url = new URL(value);
  const host = url.hostname.replace(/^\[|\]$/g, "").replace(/\.$/, "");
  const privateNetworks = new BlockList();
  for (const [network, bits] of [
    ["10.0.0.0", 8],
    ["172.16.0.0", 12],
    ["192.168.0.0", 16],
    ["100.64.0.0", 10],
  ]) {
    privateNetworks.addSubnet(network, bits, "ipv4");
  }
  privateNetworks.addSubnet("fc00::", 7, "ipv6");
  privateNetworks.addSubnet("::ffff:0:0", 96, "ipv6");
  const family = isIP(host);
  assert(
    !(family && privateNetworks.check(host, family === 4 ? "ipv4" : "ipv6")) &&
      (family ||
        (host.includes(".") &&
          !/(?:^|\.)(?:invalid|test|example|local|internal|localhost)$/.test(host))),
    "发行 origin 必须是公共 HTTPS origin",
  );
  return url.origin;
}

export function safeName(name) {
  assert(
    typeof name === "string" &&
      name.length > 0 &&
      name !== "." &&
      name !== ".." &&
      !/[\x00-\x1f\x7f/\\:%?#<>|"*]/.test(name) &&
      !/[ .]$/.test(name) &&
      !/^(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)/i.test(name),
    "不安全的制品路径",
  );
  return name;
}

async function directory(path) {
  const info = await lstat(path);
  assert(info.isDirectory() && !info.isSymbolicLink(), `目录必须是普通目录: ${path}`);
  return readdir(path);
}

async function regular(path) {
  const info = await lstat(path);
  assert(
    info.isFile() && !info.isSymbolicLink() && info.size > 0,
    `制品必须是非空普通文件: ${path}`,
  );
  return readFile(path);
}

async function jsonFile(path) {
  return JSON.parse((await regular(path)).toString("utf8").replace(/^\uFEFF/, ""));
}

async function exactFiles(path, expected) {
  const names = await directory(path);
  assert(
    names.length === expected.length && names.every((name) => expected.includes(name)),
    `目录包含未知或缺失制品: ${path}`,
  );
  for (const name of names) await regular(join(path, safeName(name)));
}

async function checksums(path) {
  const result = new Map();
  const lines = (await regular(path))
    .toString("utf8")
    .replace(/^\uFEFF/, "")
    .trim()
    .split(/\r?\n/);
  for (const line of lines) {
    const match = /^([a-fA-F0-9]{64}) [ *](.+)$/.exec(line);
    assert(match, "无效 SHA256SUMS");
    const name = safeName(match[2]);
    assert(!result.has(name), "SHA256SUMS 含重复制品");
    result.set(name, match[1].toLowerCase());
  }
  return result;
}

function base64(value) {
  assert(
    typeof value === "string" &&
      /^[A-Za-z0-9+/]+={0,2}$/.test(value) &&
      value.length % 4 === 0,
    "无效 minisign base64",
  );
  const decoded = Buffer.from(value, "base64");
  assert(decoded.toString("base64") === value, "非规范 minisign base64");
  return decoded;
}

function decodePublicKey(value) {
  const lines = base64(value.trim()).toString("utf8").trimEnd().split(/\r?\n/);
  assert(
    lines.length === 2 && lines[0].startsWith("untrusted comment: "),
    "无效 minisign 公钥",
  );
  const packet = base64(lines[1]);
  assert(
    packet.length === 42 && packet.subarray(0, 2).toString() === "Ed",
    "不支持的 minisign 公钥格式",
  );
  return packet;
}

// 同时验证数据签名和 trusted comment 签名；仅非空 .sig 无法证明与生产公钥匹配。
export function verifyMinisign(payload, signature, publicKey) {
  const key = decodePublicKey(publicKey);
  // updater 2.13.1 直接解码外层字符串，不会 trim；必须与客户端同样拒绝外层空白。
  const lines = base64(signature).toString("utf8").trimEnd().split(/\r?\n/);
  assert(
    lines.length === 4 &&
      lines[0].startsWith("untrusted comment: ") &&
      lines[2].startsWith("trusted comment: "),
    "无效 minisign 签名格式",
  );
  const packet = base64(lines[1]);
  const globalSignature = base64(lines[3]);
  assert(
    packet.length === 74 &&
      globalSignature.length === 64 &&
      packet.subarray(2, 10).equals(key.subarray(2, 10)),
    "minisign 公钥 ID 不匹配",
  );
  const algorithm = packet.subarray(0, 2).toString();
  assert(algorithm === "ED" || algorithm === "Ed", "不支持的 minisign 签名算法");
  const message =
    algorithm === "ED" ? createHash("blake2b512").update(payload).digest() : payload;
  const edKey = createPublicKey({
    key: Buffer.concat([
      Buffer.from("302a300506032b6570032100", "hex"),
      key.subarray(10),
    ]),
    format: "der",
    type: "spki",
  });
  assert(verify(null, message, edKey, packet.subarray(10)), "minisign 数据签名验证失败");
  const trustedComment = Buffer.from(lines[2].slice("trusted comment: ".length), "utf8");
  assert(
    verify(
      null,
      Buffer.concat([packet.subarray(10), trustedComment]),
      edKey,
      globalSignature,
    ),
    "minisign trusted comment 签名验证失败",
  );
}

export async function macosOverlay(origin, publicKey) {
  publicKey ??= await productionPublicKey();
  return {
    bundle: { createUpdaterArtifacts: false },
    plugins: {
      updater: {
        pubkey: publicKey,
        endpoints: [`${validateOrigin(origin)}/downloads/customer-cloud/stable.json`],
      },
    },
  };
}

export async function validateMacosOverlay(path, origin) {
  const overlay = await jsonFile(path);
  const expected = await macosOverlay(origin);
  assert(
    JSON.stringify(overlay) === JSON.stringify(expected),
    "Mac 发行 overlay 与公共 origin/生产公钥不一致",
  );
  return expected.plugins.updater;
}

export async function verifyMacosApp(path, origin) {
  const config = (await macosOverlay(origin)).plugins.updater;
  const binary = await regular(path);
  const endpoint = binary.includes(Buffer.from(config.endpoints[0]));
  const pubkey = binary.includes(Buffer.from(config.pubkey));
  const staging = binary.includes(Buffer.from("staging.example.invalid"));
  // 三路独立诊断：缺端点 / 缺公钥 / 仍含占位端点，各自给出明确失败原因。
  assert(endpoint, "Mac 应用缺少生产更新端点（updater 配置未编译进二进制）");
  assert(pubkey, "Mac 应用缺少生产更新公钥");
  assert(!staging, "Mac 应用仍包含 staging 占位更新端点");
}

function downloadUrl(origin, version, name) {
  return `${origin}/downloads/customer-cloud/${version}/${encodeURIComponent(safeName(name))}`;
}

async function verifyFiles(folder, expected) {
  const sums = await checksums(join(folder, "SHA256SUMS.txt"));
  assert(
    sums.size === expected.length && expected.every((name) => sums.has(name)),
    "SHA256SUMS 资产集合不匹配",
  );
  for (const name of expected)
    assert(
      sha256(await regular(join(folder, name))) === sums.get(name),
      `制品 SHA256 不匹配: ${name}`,
    );
  return sums;
}

// 函数的显式 publicKey 注入仅供独立测试；命令行始终从受审阅仓库读取生产公钥。
export async function prepareRelease(options, publicKey) {
  publicKey ??= await productionPublicKey();
  const { windowsReleaseDir, macArtifactsDir, expectedCommit, version, stageDir } =
    options;
  const origin = validateOrigin(options.origin);
  assert(/^[a-f0-9]{40}$/.test(expectedCommit), "ExpectedCommit 必须是完整 commit SHA");
  assert(/^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$/.test(version), "版本格式无效");
  const win = await jsonFile(join(windowsReleaseDir, "release-manifest.json"));
  assert(
    win.channel === "signed-release" &&
      win.version === version &&
      win.source_sha === expectedCommit &&
      win.platform === "windows-x86_64" &&
      win.signature?.status === "Valid",
    "Windows 正式发行出处或签名 gate 不匹配",
  );
  const exe = safeName(win.artifact);
  assert(
    exe.endsWith(".exe") &&
      win.updater?.signatureArtifact === `${exe}.sig` &&
      win.updater.manifestArtifact === "stable.json" &&
      win.updater.endpoint === `${origin}/downloads/customer-cloud/stable.json`,
    "Windows updater 配置不匹配",
  );
  const winFiles = [exe, `${exe}.sig`];
  await exactFiles(windowsReleaseDir, [
    ...winFiles,
    "stable.json",
    "release-manifest.json",
    "SHA256SUMS.txt",
  ]);
  const winSums = await verifyFiles(windowsReleaseDir, winFiles);
  assert(
    winSums.get(exe) === win.sha256?.toLowerCase() &&
      winSums.get(`${exe}.sig`) === win.updater.signatureSha256?.toLowerCase(),
    "Windows 清单 SHA 不匹配",
  );
  const winStable = await jsonFile(join(windowsReleaseDir, "stable.json"));
  const winEntry = winStable.platforms?.["windows-x86_64"];
  const winSignature = (await regular(join(windowsReleaseDir, `${exe}.sig`))).toString(
    "utf8",
  );
  assert(
    winStable.version === version &&
      Object.keys(winStable.platforms ?? {}).length === 1 &&
      winEntry?.signature === winSignature &&
      new URL(winEntry.url).href === new URL(downloadUrl(origin, version, exe)).href &&
      typeof winStable.notes === "string" &&
      typeof winStable.pub_date === "string" &&
      !Number.isNaN(Date.parse(winStable.pub_date)),
    "Windows stable 清单不匹配",
  );
  verifyMinisign(await regular(join(windowsReleaseDir, exe)), winSignature, publicKey);

  const macFolders = Object.keys(macPlatforms).map((platform) => `desktop-${platform}`);
  assert(
    (await directory(macArtifactsDir)).length === 2 &&
      (await directory(macArtifactsDir)).every((name) => macFolders.includes(name)),
    "必须提供隔离的两个 Mac CI 制品目录",
  );
  const records = [];
  for (const [ciPlatform, config] of Object.entries(macPlatforms)) {
    const folder = join(macArtifactsDir, `desktop-${ciPlatform}`);
    const manifestBytes = await regular(join(folder, "manifest.json"));
    const pin =
      ciPlatform === "macos-arm64"
        ? options.macArm64ManifestSha256
        : options.macX64ManifestSha256;
    assert(
      shaPattern.test(pin ?? "") && sha256(manifestBytes) === pin,
      "Mac manifest 与可信 SHA256 pin 不匹配",
    );
    const manifest = JSON.parse(manifestBytes.toString("utf8"));
    assert(
      manifest.version === version &&
        manifest.source_sha === expectedCommit &&
        manifest.platform === ciPlatform &&
        manifest.target === config.target &&
        manifest.architecture === config.architecture &&
        manifest.signature === "ad-hoc/unnotarized" &&
        manifest.api_base_url === origin &&
        manifest.updater_endpoint === `${origin}/downloads/customer-cloud/stable.json` &&
        manifest.updater_pubkey === publicKey,
      "Mac CI 出处、架构或生产 updater 配置不匹配",
    );
    const dmg = safeName(manifest.artifact);
    const archive = safeName(manifest.updater_artifact);
    assert(
      dmg.endsWith(".dmg") && archive === dmg.slice(0, -4) + ".app.tar.gz",
      "Mac 安装包和更新归档不匹配",
    );
    await exactFiles(folder, [
      dmg,
      archive,
      "manifest.json",
      "SHA256SUMS.txt",
      "RELEASE-CHANNEL.txt",
    ]);
    assert(
      (await regular(join(folder, "RELEASE-CHANNEL.txt"))).toString() ===
        "internal-test-unsigned\n",
      "Mac CI 渠道标记不匹配",
    );
    const sums = await verifyFiles(folder, [dmg, archive]);
    assert(
      sums.get(dmg) === manifest.sha256 && sums.get(archive) === manifest.updater_sha256,
      "Mac 清单 SHA 不匹配",
    );
    records.push({
      ...config,
      folder,
      artifact: dmg,
      sha256: sums.get(dmg),
      updateArtifact: archive,
      updateSha256: sums.get(archive),
    });
  }

  const allNames = [
    ...winFiles,
    ...records.flatMap((record) => [
      record.artifact,
      record.updateArtifact,
      `${record.updateArtifact}.sig`,
    ]),
  ];
  assert(
    new Set(allNames.map((name) => name.toLowerCase())).size === allNames.length,
    "跨平台制品名称冲突",
  );
  try {
    assert((await directory(stageDir)).length === 0, "暂存目录必须为空");
  } catch (error) {
    if (error.code !== "ENOENT") throw error;
  }
  await mkdir(stageDir, { recursive: true });
  for (const name of winFiles) {
    await copyFile(join(windowsReleaseDir, name), join(stageDir, name));
    assert(
      sha256(await regular(join(stageDir, name))) === winSums.get(name),
      `Windows 暂存复制校验失败: ${name}`,
    );
  }
  for (const record of records) {
    for (const [name, digest] of [
      [record.artifact, record.sha256],
      [record.updateArtifact, record.updateSha256],
    ]) {
      await copyFile(join(record.folder, name), join(stageDir, name));
      assert(
        sha256(await regular(join(stageDir, name))) === digest,
        `Mac 暂存复制校验失败: ${name}`,
      );
    }
    delete record.folder;
  }
  const plan = {
    origin,
    version,
    source_sha: expectedCommit,
    windows: win,
    stable: winStable,
    mac: records,
  };
  await writeFile(join(stageDir, planName), jsonText(plan), { flag: "wx" });
  return {
    windowsInstaller: join(stageDir, exe),
    windowsThumbprint: win.signature.thumbprint,
    macArchives: records.map((record) => join(stageDir, record.updateArtifact)),
  };
}

export async function finalizeRelease(stageDir, publicKey) {
  publicKey ??= await productionPublicKey();
  const plan = await jsonFile(join(stageDir, planName));
  const { version, origin } = plan;
  const win = plan.windows;
  const winName = safeName(win.artifact);
  const platforms = {
    "windows-x86_64": {
      artifact: winName,
      sha256: win.sha256.toLowerCase(),
      updateArtifact: winName,
      updateSha256: win.sha256.toLowerCase(),
      signatureArtifact: `${winName}.sig`,
      signatureSha256: win.updater.signatureSha256.toLowerCase(),
      signature: win.signature,
    },
  };
  for (const record of plan.mac) {
    platforms[record.platform] = {
      artifact: record.artifact,
      sha256: record.sha256,
      updateArtifact: record.updateArtifact,
      updateSha256: record.updateSha256,
      signatureArtifact: `${record.updateArtifact}.sig`,
      signatureSha256: sha256(
        await regular(join(stageDir, `${safeName(record.updateArtifact)}.sig`)),
      ),
      signature: { status: "ad-hoc/unnotarized" },
    };
  }
  const assets = [];
  const stable = {
    version,
    notes: plan.stable.notes,
    pub_date: plan.stable.pub_date,
    platforms: {},
  };
  for (const [platform, record] of Object.entries(platforms)) {
    for (const [path, digest, role] of [
      [record.updateArtifact, record.updateSha256, "update"],
      [record.signatureArtifact, record.signatureSha256, "signature"],
      ...(record.artifact === record.updateArtifact
        ? []
        : [[record.artifact, record.sha256, "installer"]]),
    ]) {
      assert(
        sha256(await regular(join(stageDir, safeName(path)))) === digest,
        `暂存制品被修改: ${path}`,
      );
      assets.push({ path, sha256: digest, role, platform });
    }
    const signature = (await regular(join(stageDir, record.signatureArtifact))).toString(
      "utf8",
    );
    verifyMinisign(
      await regular(join(stageDir, record.updateArtifact)),
      signature,
      publicKey,
    );
    stable.platforms[platform] = {
      signature,
      url: downloadUrl(origin, version, record.updateArtifact),
    };
  }
  await exactFiles(stageDir, [planName, ...assets.map((asset) => asset.path)]);
  assets.sort((a, b) => a.path.localeCompare(b.path, "en"));
  const manifest = {
    ...win,
    schemaVersion: 2,
    source_sha: plan.source_sha,
    platforms,
    assets,
    updater: { ...win.updater, publicKey },
    generatedAt: new Date().toISOString(),
  };
  await writeFile(join(stageDir, "stable.json"), jsonText(stable), { flag: "wx" });
  await writeFile(join(stageDir, "release-manifest.json"), jsonText(manifest), {
    flag: "wx",
  });
  const sums = assets.map((asset) => `${asset.sha256}  ${asset.path}\n`);
  for (const name of ["stable.json", "release-manifest.json"])
    sums.push(`${sha256(await regular(join(stageDir, name)))}  ${name}\n`);
  await writeFile(join(stageDir, "SHA256SUMS.txt"), sums.join(""), { flag: "wx" });
  await unlink(join(stageDir, planName));
  return manifest;
}

async function main() {
  const [command, ...args] = process.argv.slice(2);
  const names = [
    "windows-release-dir",
    "mac-artifacts-dir",
    "expected-commit",
    "version",
    "origin",
    "stage-dir",
    "mac-arm64-manifest-sha256",
    "mac-x64-manifest-sha256",
    "output",
    "app-binary",
  ];
  const { values } = parseArgs({
    args,
    options: Object.fromEntries(names.map((name) => [name, { type: "string" }])),
  });
  const origin =
    values.origin ?? process.env.VITE_API_BASE_URL ?? "https://video.zszhj.cn";
  if (command === "macos-overlay") {
    await writeFile(values.output, jsonText(await macosOverlay(origin)), { flag: "wx" });
  } else if (command === "verify-macos-app") {
    await verifyMacosApp(values["app-binary"], origin);
  } else if (command === "prepare") {
    const options = Object.fromEntries(
      Object.entries(values).map(([name, value]) => [
        name.replace(/-([a-z])/g, (_, letter) => letter.toUpperCase()),
        value,
      ]),
    );
    console.log(JSON.stringify(await prepareRelease({ ...options, origin })));
  } else if (command === "finalize") {
    await finalizeRelease(values["stage-dir"]);
  } else {
    throw new Error(
      "使用 macos-overlay / verify-macos-app / prepare / finalize；正式入口为 finalize-customer-release.ps1",
    );
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  main().catch((error) => {
    console.error(error.message);
    process.exitCode = 1;
  });
}
