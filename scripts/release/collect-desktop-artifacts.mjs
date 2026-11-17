import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import {
  copyFile,
  mkdir,
  lstat,
  readdir,
  readFile,
  stat,
  writeFile,
} from "node:fs/promises";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { macPlatforms, validateMacosOverlay } from "./finalize-customer-release.mjs";

const platforms = {
  "windows-x86_64": { extension: ".exe", signature: "unsigned" },
  "macos-arm64": { extension: ".dmg", signature: "ad-hoc/unnotarized" },
  "macos-x86_64": { extension: ".dmg", signature: "ad-hoc/unnotarized" },
};

const repoRoot = resolve(dirname(fileURLToPath(import.meta.url)), "../..");

function run(command, args, options = {}) {
  const result = spawnSync(command, args, {
    cwd: repoRoot,
    encoding: "utf8",
    timeout: 30_000,
    ...options,
  });
  if (result.error) throw result.error;
  if (result.status !== 0) {
    throw new Error(result.stderr.trim() || `${command} failed`);
  }
  return result.stdout.trim();
}

async function requireEmptyOutput(outputDir) {
  try {
    const entries = await readdir(outputDir);
    if (entries.length > 0) throw new Error("output directory must be empty");
  } catch (error) {
    if (error?.code !== "ENOENT") throw error;
  }
}

async function main() {
  const [platform, bundleDir, outputDir, releaseConfigPath, ...extra] =
    process.argv.slice(2);
  if (!platform || !bundleDir || !outputDir || extra.length > 0) {
    throw new Error(
      "usage: collect-desktop-artifacts.mjs <platform> <bundle-dir> <output-dir> [macos-release-config]",
    );
  }
  if (!Object.hasOwn(platforms, platform)) {
    throw new Error(`unsupported platform: ${platform}`);
  }
  const platformConfig = platforms[platform];

  let bundleEntries;
  try {
    bundleEntries = await readdir(bundleDir, { withFileTypes: true });
  } catch (error) {
    throw new Error(`cannot read bundle directory: ${error.message}`);
  }
  const installers = bundleEntries.filter(
    (entry) =>
      entry.isFile() && entry.name.toLowerCase().endsWith(platformConfig.extension),
  );
  if (installers.length !== 1) {
    throw new Error(
      `bundle directory must contain exactly one non-empty ${platformConfig.extension} installer`,
    );
  }
  const installer = installers[0];
  const installerPath = join(bundleDir, installer.name);
  if ((await stat(installerPath)).size === 0) {
    throw new Error(
      `bundle directory must contain exactly one non-empty ${platformConfig.extension} installer`,
    );
  }
  await requireEmptyOutput(outputDir);

  run(process.execPath, [join(repoRoot, "scripts/require_customer_api_base.mjs")], {
    env: process.env,
  });
  const packageJson = JSON.parse(await readFile(join(repoRoot, "package.json"), "utf8"));
  const sourceSha = run("git", ["rev-parse", "HEAD"]);
  const payload = await readFile(installerPath);
  const sha256 = createHash("sha256").update(payload).digest("hex");

  const checksumLines = [`${sha256}  ${installer.name}`];
  const provenance = {};
  let updaterName;
  if (releaseConfigPath) {
    if (!Object.hasOwn(macPlatforms, platform))
      throw new Error("release config requires a Mac platform");
    const updater = await validateMacosOverlay(
      releaseConfigPath,
      process.env.VITE_API_BASE_URL,
    );
    Object.assign(provenance, {
      target: macPlatforms[platform].target,
      architecture: macPlatforms[platform].architecture,
      updater_endpoint: updater.endpoints[0],
      updater_pubkey: updater.pubkey,
      api_base_url: new URL(updater.endpoints[0]).origin,
    });
  }
  // 旧内部预览允许仅 DMG；明确提供发行配置时必须有可独立签名的更新归档。
  if (platform.startsWith("macos")) {
    const name = installer.name.replace(/\.dmg$/i, ".app.tar.gz");
    try {
      const updaterPath = join(bundleDir, name);
      const info = await lstat(updaterPath);
      if (!info.isFile() || info.isSymbolicLink() || !info.size)
        throw new Error("updater archive must be a non-empty regular file");
      const updaterSha = createHash("sha256")
        .update(await readFile(updaterPath))
        .digest("hex");
      updaterName = name;
      checksumLines.push(`${updaterSha}  ${name}`);
      Object.assign(provenance, { updater_artifact: name, updater_sha256: updaterSha });
    } catch (error) {
      if (error?.code !== "ENOENT" || releaseConfigPath) throw error;
    }
  }
  await mkdir(outputDir, { recursive: true });
  await copyFile(installerPath, join(outputDir, installer.name));
  if (updaterName)
    await copyFile(join(bundleDir, updaterName), join(outputDir, updaterName));
  await writeFile(
    join(outputDir, "SHA256SUMS.txt"),
    checksumLines.map((line) => `${line}\n`).join(""),
    "utf8",
  );
  await writeFile(
    join(outputDir, "RELEASE-CHANNEL.txt"),
    "internal-test-unsigned\n",
    "utf8",
  );
  await writeFile(
    join(outputDir, "manifest.json"),
    `${JSON.stringify(
      {
        version: packageJson.version,
        platform,
        source_sha: sourceSha,
        api_base_url: process.env.VITE_API_BASE_URL,
        signature: platformConfig.signature,
        artifact: installer.name,
        sha256,
        ...provenance,
      },
      null,
      2,
    )}\n`,
    "utf8",
  );
}

main().catch((error) => {
  console.error(error instanceof Error ? error.message : String(error));
  process.exitCode = 1;
});
