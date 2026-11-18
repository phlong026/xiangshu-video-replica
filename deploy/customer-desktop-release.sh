#!/usr/bin/env bash
# 发布目录来自签名机；服务器只持 updater 公钥，不接收或使用私钥。
# nginx 的真实 alias 与 SPA 根目录无关，正常发布应显式指定 --channel-root。
# 版本目录不可覆盖；完整暂存和验签后落位，最后原子切换 stable.json。
set -Eeuo pipefail

usage() {
  cat >&2 <<EOF
usage:
  $0 --channel-root /opt/video-replica/app/desktop-releases/customer-cloud --release-dir <version-dir> [--public-key <Tauri-base64-public-key>]
  $0 --channel-root /opt/video-replica/app/desktop-releases/customer-cloud --rollback <version> [--public-key <Tauri-base64-public-key>]

environment:
  VIDEO_REPLICA_DESKTOP_CHANNEL_ROOT       nginx 的 channel alias 目录
  VIDEO_REPLICA_DESKTOP_UPDATER_PUBLIC_KEY  受信 Tauri base64 公钥；默认读取本仓库 customer overlay
  VIDEO_REPLICA_SITE_ROOT                  兼容旧测试：显式设置时使用 <SITE>/downloads/customer-cloud

默认 channel: /opt/video-replica/app/desktop-releases/customer-cloud
服务器依赖: bash、python3、flock、OpenSSL（支持 Ed25519）、curl，以及 GNU coreutils。
EOF
  exit 2
}

CHANNEL="${VIDEO_REPLICA_DESKTOP_CHANNEL_ROOT:-${VIDEO_REPLICA_SITE_ROOT:+$VIDEO_REPLICA_SITE_ROOT/downloads/customer-cloud}}"
CHANNEL="${CHANNEL:-/opt/video-replica/app/desktop-releases/customer-cloud}"
PUBLIC_KEY="${VIDEO_REPLICA_DESKTOP_UPDATER_PUBLIC_KEY:-}"
SCRIPT_MODE=""
RELEASE_DIR=""
ROLLBACK_VERSION=""
while [[ $# -gt 0 ]]; do
  [[ $# -ge 2 && -n "$2" ]] || usage
  case "$1" in
    --channel-root) CHANNEL="$2" ;;
    --public-key) PUBLIC_KEY="$2" ;;
    --release-dir)
      [[ -z "$SCRIPT_MODE" ]] || usage
      SCRIPT_MODE="publish"
      RELEASE_DIR="$2"
      ;;
    --rollback)
      [[ -z "$SCRIPT_MODE" ]] || usage
      SCRIPT_MODE="rollback"
      ROLLBACK_VERSION="$2"
      ;;
    *) usage ;;
  esac
  shift 2
done
[[ -n "$SCRIPT_MODE" ]] || usage

# Python 以隔离模式启动，上传目录中的模块不能劫持标准库。
# 校验和与 manifest 共同限定白名单，绝不把 .sig 存在当作密码学验签。
metadata() {
  python3 -I - "$@" <<'PY'
import base64
import hashlib
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import unquote, urlsplit

sys.stdout.reconfigure(encoding="utf-8", newline="\n")
sys.stderr.reconfigure(encoding="utf-8", newline="\n")

SUPPORTED = {"windows-x86_64", "darwin-aarch64", "darwin-x86_64"}
DOCUMENTS = {"stable.json", "release-manifest.json", "SHA256SUMS.txt"}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "JSON 包含重复字段")
        result[key] = value
    return result


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"),
                      object_pairs_hook=unique_object,
                      parse_constant=lambda _: require(False, "JSON 包含无效数值"))


def version_of(doc):
    value = doc.get("version")
    require(isinstance(value, str) and re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", value),
            "manifest version 无效")
    return value


def safe_name(value):
    require(isinstance(value, str) and value and len(value) <= 255
            and value not in {".", ".."} and not value.startswith((".", "-"))
            and not any(c in value for c in "/\\:")
            and not any(ord(c) < 32 or ord(c) == 127 for c in value),
            "资产路径必须是安全的平铺文件名（禁止路径穿越）")
    return value


def https_url(value):
    require(isinstance(value, str) and not any(ord(c) < 32 or ord(c) == 127 for c in value)
            and "\\" not in value, "下载地址必须是完整 HTTPS URL")
    parsed = urlsplit(value)
    require(parsed.scheme == "https" and parsed.hostname and parsed.netloc
            and not any(c.isspace() for c in parsed.netloc)
            and not parsed.username and not parsed.password
            and not parsed.query and not parsed.fragment,
            "下载地址必须是包含主机和路径的 HTTPS URL（不含查询或片段）")
    _ = parsed.port
    return parsed


def endpoint_of(stable):
    origins = set()
    version = version_of(stable)
    platforms = stable.get("platforms")
    require(isinstance(platforms, dict) and platforms and set(platforms) <= SUPPORTED,
            "stable platforms 无效或含不支持的平台")
    for platform, entry in platforms.items():
        require(isinstance(entry, dict), "stable platform 条目无效")
        parsed = https_url(entry.get("url"))
        prefix = f"/downloads/customer-cloud/{version}/"
        require(parsed.path.startswith(prefix), "下载地址路径不匹配")
        name = safe_name(unquote(parsed.path[len(prefix):], errors="strict"))
        require(unquote(parsed.path, errors="strict") == prefix + name,
                "下载地址路径不匹配")
        origins.add(f"https://{parsed.netloc}")
    require(len(origins) == 1, "各平台下载地址必须使用同一 origin")
    return origins.pop() + "/downloads/customer-cloud/stable.json"


def digest(path):
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest()


def hash_value(value):
    require(isinstance(value, str) and re.fullmatch(r"[a-fA-F0-9]{64}", value),
            "manifest SHA256 无效")
    return value.lower()


def decode_envelope(value, description):
    require(isinstance(value, str) and value, description + " 缺失")
    try:
        decoded = base64.b64decode(value, validate=True)
        # updater 严格解码 stable.signature；trim 会放行客户端无法安装的制品。
        require(base64.b64encode(decoded).decode("ascii") == value,
                description + " 必须是无空白的 canonical Tauri base64")
        return decoded
    except ValueError as error:
        raise ValueError(description + " 不是有效 canonical Tauri base64（禁止外层空白）") from error


def validate(folder, expected_version, public_key):
    root = Path(folder)
    require(root.is_dir() and not root.is_symlink(), "发行目录不存在或是符号链接")
    for name in DOCUMENTS:
        require((root / name).is_file() and not (root / name).is_symlink(), "缺少发行文档: " + name)
    stable = read_json(root / "stable.json")
    version = version_of(stable)
    require(version == expected_version, "stable.json version 与目录不一致")
    endpoint = endpoint_of(stable)
    manifest = read_json(root / "release-manifest.json")
    require(manifest.get("channel") == "signed-release" and version_of(manifest) == version,
            "发行 manifest channel/version 不一致")
    updater = manifest.get("updater")
    require(isinstance(updater, dict) and updater.get("endpoint") == endpoint,
            "发行 manifest updater endpoint 不一致")
    require(updater.get("manifestArtifact", "stable.json") == "stable.json",
            "发行 manifestArtifact 必须是 stable.json")
    modern = "schemaVersion" in manifest
    if modern:
        require(manifest["schemaVersion"] == 2, "不支持的 release manifest schemaVersion")
        # schemaVersion=2 允许受支持平台的非空子集（2.6.8 因 macOS x64 updater
        # 端点问题先发两平台）；三个集合必须一致：stable.platforms、
        # manifest.platforms、assets 覆盖的平台。assets 覆盖集与 stable 的相等
        # 由两处共同强制：每条资产 platform 必须已列（下方循环），每个已列平台
        # 必须配齐 update/signature（再下方循环）。
        platforms = set(stable["platforms"])
        require(platforms and platforms <= SUPPORTED, "stable platforms 必须是受支持平台的非空子集")
        require(isinstance(manifest.get("platforms"), dict)
                and set(manifest["platforms"]) == platforms, "发行 manifest platforms 不一致")
        require(isinstance(manifest.get("source_sha"), str)
                and re.fullmatch(r"[a-fA-F0-9]{40}|[a-fA-F0-9]{64}", manifest["source_sha"]),
                "发行 source_sha 无效")
        assets = manifest.get("assets")
        require(isinstance(assets, list) and assets, "发行 assets 白名单缺失")
    else:
        # 保留旧 Windows 正式签名合同；旧 SHA256SUMS 只覆盖 exe 与 exe.sig。
        require(set(stable["platforms"]) == {"windows-x86_64"}
                and manifest.get("platform") == "windows-x86_64", "legacy 仅支持 Windows")
        require(isinstance(manifest.get("signature"), dict)
                and manifest["signature"].get("status") == "Valid", "缺少 Windows 正式签名记录")
        assets = [
            {"path": manifest.get("artifact"), "sha256": manifest.get("sha256"),
             "role": "update", "platform": "windows-x86_64"},
            {"path": updater.get("signatureArtifact"), "sha256": updater.get("signatureSha256"),
             "role": "signature", "platform": "windows-x86_64"},
        ]
    allowed = {}
    roles = {}
    for asset in assets:
        require(isinstance(asset, dict), "资产记录无效")
        name = safe_name(asset.get("path"))
        require(name not in DOCUMENTS and name not in allowed, "资产白名单重复或占用元数据名")
        platform, role = asset.get("platform"), asset.get("role")
        require(platform in stable["platforms"] and role in {"installer", "update", "signature"},
                "资产 platform/role 无效")
        require((platform, role) not in roles, "资产 platform/role 重复")
        allowed[name] = hash_value(asset.get("sha256"))
        roles[platform, role] = name
    for platform, entry in stable["platforms"].items():
        update = roles.get((platform, "update"))
        signature = roles.get((platform, "signature"))
        require(update and signature == update + ".sig", "updater/signature 资产不匹配: " + platform)
        parsed = https_url(entry["url"])
        require(unquote(parsed.path, errors="strict") == f"/downloads/customer-cloud/{version}/{update}",
                "下载地址与平台资产不一致: " + platform)
        if platform == "windows-x86_64":
            require(update.endswith(".exe") and (platform, "installer") not in roles,
                    "Windows updater 必须是唯一 exe")
        else:
            require(update.endswith(".tar.gz") and roles.get((platform, "installer"), "").endswith(".dmg"),
                    "Mac 平台必须包含 DMG 与 tar.gz updater")
    if modern:
        # 每平台资产数：Windows 2（exe+sig），Mac 3（DMG+tar.gz+sig）。
        expected_assets = sum(2 if name == "windows-x86_64" else 3 for name in platforms)
        require(len(allowed) == expected_assets, "受控资产数量与平台子集不一致")
        for platform, record in manifest["platforms"].items():
            update, signature = roles[platform, "update"], roles[platform, "signature"]
            installer = roles.get((platform, "installer"), update)
            require(isinstance(record, dict) and record.get("artifact") == installer
                    and hash_value(record.get("sha256")) == allowed[installer]
                    and record.get("updateArtifact") == update
                    and hash_value(record.get("updateSha256")) == allowed[update]
                    and record.get("signatureArtifact") == signature
                    and hash_value(record.get("signatureSha256")) == allowed[signature],
                    "平台记录与资产白名单不一致: " + platform)
        require(manifest.get("platform") == "windows-x86_64"
                and manifest.get("artifact") == roles["windows-x86_64", "update"]
                and hash_value(manifest.get("sha256")) == allowed[manifest["artifact"]]
                and isinstance(manifest.get("signature"), dict)
                and manifest["signature"].get("status") == "Valid"
                and updater.get("signatureArtifact") == roles["windows-x86_64", "signature"]
                and hash_value(updater.get("signatureSha256")) == allowed[updater["signatureArtifact"]],
                "Windows 兼容字段与统一资产白名单不一致")
    files = set(allowed) | DOCUMENTS
    require({p.name for p in root.iterdir()} == files, "发行目录与受控白名单不一致（缺件或多余文件）")
    for name in files:
        path = root / name
        require(path.is_file() and not path.is_symlink(), "资产必须是普通文件: " + name)
    checksums = {}
    for line in (root / "SHA256SUMS.txt").read_text(encoding="utf-8-sig").splitlines():
        match = re.fullmatch(r"([a-fA-F0-9]{64}) [ *](.+)", line)
        require(match is not None, "SHA256SUMS 行格式无效")
        name = safe_name(match[2])
        require(name not in checksums, "SHA256SUMS 文件名重复")
        checksums[name] = match[1].lower()
    expected_hashes = set(allowed) | ({"stable.json", "release-manifest.json"} if modern else set())
    require(expected_hashes <= set(checksums) <= set(allowed) | {"stable.json", "release-manifest.json"},
            "SHA256SUMS 与资产白名单不一致")
    for name, expected in checksums.items():
        require(digest(root / name) == expected, "SHA256 校验失败: " + name)
    for name, expected in allowed.items():
        require(checksums[name] == expected, "manifest 与 SHA256SUMS 不一致: " + name)
    # 公钥来自受信配置/参数；包内的公钥字段不能成为信任锚。
    key_lines = decode_envelope(public_key, "受信 updater 公钥").decode("utf-8").splitlines()
    require(len(key_lines) == 2 and key_lines[0].startswith("untrusted comment: "), "公钥 envelope 无效")
    key_packet = base64.b64decode(key_lines[1], validate=True)
    require(len(key_packet) == 42 and key_packet[:2] == b"Ed", "minisign 公钥格式无效")
    with tempfile.TemporaryDirectory(prefix="desktop-signatures-") as temporary:
        temporary = Path(temporary)
        key_file = temporary / "updater.der"
        # RFC 8410 SubjectPublicKeyInfo，后接 Ed25519 的 32 字节公钥。
        key_file.write_bytes(bytes.fromhex("302a300506032b6570032100") + key_packet[10:])

        def verify_ed25519(message, signature):
            message_file, signature_file = temporary / "message", temporary / "signature"
            message_file.write_bytes(message)
            signature_file.write_bytes(signature)
            result = subprocess.run(["openssl", "pkeyutl", "-verify", "-pubin", "-keyform", "DER",
                                     "-inkey", str(key_file), "-rawin", "-in", str(message_file),
                                     "-sigfile", str(signature_file)], stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, check=False)
            require(result.returncode == 0, "updater 密码学验签失败")

        for platform, entry in stable["platforms"].items():
            update, signature = roles[platform, "update"], roles[platform, "signature"]
            signature_text = (root / signature).read_bytes().decode("utf-8")
            require(isinstance(entry.get("signature"), str)
                    and entry["signature"] == signature_text,
                    "stable signature 与签名资产不一致: " + platform)
            lines = decode_envelope(signature_text, "updater 签名").decode("utf-8").splitlines()
            require(len(lines) == 4 and lines[0].startswith("untrusted comment: ")
                    and lines[2].startswith("trusted comment: "), "minisign 签名 envelope 无效")
            packet = base64.b64decode(lines[1], validate=True)
            global_signature = base64.b64decode(lines[3], validate=True)
            require(len(packet) == 74 and len(global_signature) == 64
                    and packet[2:10] == key_packet[2:10], "minisign 公钥 ID 不匹配")
            require(packet[:2] in {b"Ed", b"ED"}, "不支持的 minisign 签名算法")
            if packet[:2] == b"ED":
                hasher = hashlib.blake2b(digest_size=64)
                with (root / update).open("rb") as stream:
                    for block in iter(lambda: stream.read(1024 * 1024), b""):
                        hasher.update(block)
                message = hasher.digest()
            else:
                message = (root / update).read_bytes()
            verify_ed25519(message, packet[10:])
            # global signature 绑定数据签名和去掉前缀的 trusted comment。
            verify_ed25519(packet[10:] + lines[2][len("trusted comment: "):].encode("utf-8"),
                           global_signature)
    for name in sorted(files):
        print(name)


try:
    mode, *args = sys.argv[1:]
    if mode == "validate":
        validate(*args)
    elif mode == "version":
        print(version_of(read_json(args[0])))
    elif mode == "endpoint":
        print(endpoint_of(read_json(args[0])))
    elif mode == "same-manifest":
        require(read_json(args[0]) == read_json(args[1]), "公网/本地 manifest 内容不一致")
    elif mode == "public-key":
        print(read_json(args[0])["plugins"]["updater"]["pubkey"])
    else:
        raise ValueError("未知 metadata 操作")
except (ValueError, KeyError, TypeError, OSError, UnicodeError) as error:
    print("发行校验失败: " + str(error), file=sys.stderr)
    sys.exit(1)
PY
}

for tool in python3 flock openssl curl; do
  command -v "$tool" >/dev/null || { printf '缺少服务器发布依赖: %s\n' "$tool" >&2; exit 1; }
done
if [[ -z "$PUBLIC_KEY" ]]; then
  SCRIPT_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"
  PUBLIC_KEY="$(metadata public-key "$SCRIPT_DIR/../client/src-tauri/tauri.customer.conf.json")"
fi
[[ -n "$PUBLIC_KEY" ]] || { printf '必须提供受信 updater 公钥\n' >&2; exit 1; }
umask 022
mkdir -p -- "$CHANNEL"
CHANNEL="$(cd -- "$CHANNEL" && pwd -P)"
[[ ! -L "$CHANNEL/.publish.lock" ]] || { printf '发布锁不能是符号链接\n' >&2; exit 1; }
exec 9>"$CHANNEL/.publish.lock"
flock -x 9
# 锁从读取旧状态前一直持有到公网回读/失败恢复完成；不能仅锁 stable 切换。
LOG="$(mktemp "$CHANNEL/.publish-XXXXXXXX.log")"
exec > >(tee -a "$LOG") 2>&1
WORK=""
STAGE=""
SWAPPED=0
HAD_PREVIOUS=0
PREVIOUS_VERSION=""

cleanup() {
  [[ -z "$STAGE" ]] || rm -rf -- "$STAGE"
  [[ -z "$WORK" ]] || rm -rf -- "$WORK"
  return 0
}

on_error() {
  local code="$1" line="$2"
  trap - ERR INT TERM
  # 不记录 BASH_COMMAND/命令行；上游环境即使意外带私钥也不会被错误 trap 打印。
  printf 'PUBLISH_FAILED exit=%s line=%s\n' "$code" "$line"
  if [[ "$SWAPPED" == 1 ]]; then
    if [[ "$HAD_PREVIOUS" == 1 ]]; then
      if cp -- "$WORK/previous.json" "$WORK/restore.json" && mv -fT -- "$WORK/restore.json" "$CHANNEL/stable.json"; then
        printf '已恢复发布前 stable.json\n'
      else
        printf '恢复 stable.json 失败，请使用保留的历史 manifest 恢复\n' >&2
      fi
    else
      rm -f -- "$CHANNEL/stable.json"
      printf '首次发布失败，已撤回 stable.json\n'
    fi
  fi
  exit "$code"
}
trap cleanup EXIT
trap 'on_error $? $LINENO' ERR
trap 'on_error 130 $LINENO' INT
trap 'on_error 143 $LINENO' TERM
WORK="$(mktemp -d "$CHANNEL/.publish-XXXXXXXX")"

verify_channel() {
  local endpoint
  endpoint="$(metadata endpoint "$CHANNEL/stable.json")"
  curl -fsS --max-time 30 "$endpoint" -o "$WORK/served.json"
  metadata same-manifest "$CHANNEL/stable.json" "$WORK/served.json"
  printf 'verified %s\n' "$endpoint"
}

archive_previous() {
  local backup
  [[ ! -L "$CHANNEL/stable.json" ]] || { printf 'stable.json 不能是符号链接\n' >&2; return 1; }
  if [[ -e "$CHANNEL/stable.json" ]]; then
    PREVIOUS_VERSION="$(metadata version "$CHANNEL/stable.json")"
    cp -- "$CHANNEL/stable.json" "$WORK/previous.json"
    HAD_PREVIOUS=1
    backup="$CHANNEL/stable.json.$PREVIOUS_VERSION.bak"
    [[ ! -L "$backup" ]] || { printf '历史 manifest 不能是符号链接\n' >&2; return 1; }
    if [[ -e "$backup" ]]; then
      cmp -s -- "$WORK/previous.json" "$backup" || { printf '历史同版本 manifest 冲突，拒绝覆盖\n' >&2; return 1; }
    else
      cp -- "$WORK/previous.json" "$WORK/archive.json"
      mv -nT -- "$WORK/archive.json" "$backup"
      cmp -s -- "$WORK/previous.json" "$backup"
    fi
  fi
}

switch_manifest() {
  cp -- "$1" "$WORK/next.json"
  chmod 644 "$WORK/next.json"
  SWAPPED=1
  mv -fT -- "$WORK/next.json" "$CHANNEL/stable.json"
  verify_channel
  SWAPPED=0
}

if [[ "$SCRIPT_MODE" == rollback ]]; then
  [[ "$ROLLBACK_VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || usage
  BACKUP="$CHANNEL/stable.json.$ROLLBACK_VERSION.bak"
  [[ -f "$BACKUP" && ! -L "$BACKUP" ]] || { printf 'no archived manifest for %s\n' "$ROLLBACK_VERSION" >&2; exit 1; }
  metadata validate "$CHANNEL/$ROLLBACK_VERSION" "$ROLLBACK_VERSION" "$PUBLIC_KEY" > "$WORK/rollback-files"
  metadata same-manifest "$BACKUP" "$CHANNEL/$ROLLBACK_VERSION/stable.json"
  archive_previous
  switch_manifest "$BACKUP"
  printf 'rolled back to %s\n' "$ROLLBACK_VERSION"
  exit 0
fi

[[ -d "$RELEASE_DIR" && ! -L "$RELEASE_DIR" ]] || { printf 'release directory missing or symlink\n' >&2; exit 1; }
RELEASE_DIR="$(cd -- "$RELEASE_DIR" && pwd -P)"
VERSION="$(basename -- "$RELEASE_DIR")"
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { printf 'release directory must be named <version>\n' >&2; exit 1; }
metadata validate "$RELEASE_DIR" "$VERSION" "$PUBLIC_KEY" > "$WORK/source-files"
STAGE="$(mktemp -d "$CHANNEL/.staging-XXXXXXXX")"
while IFS= read -r name; do
  cp -- "$RELEASE_DIR/$name" "$STAGE/$name"
done < "$WORK/source-files"
chmod 755 "$STAGE"
while IFS= read -r name; do
  chmod 644 "$STAGE/$name"
done < "$WORK/source-files"
# 对实际暂存副本重新验 hash/签名，阻断上传目录在校验后被改写的情况。
metadata validate "$STAGE" "$VERSION" "$PUBLIC_KEY" > "$WORK/staged-files"
cmp -s -- "$WORK/source-files" "$WORK/staged-files"
TARGET="$CHANNEL/$VERSION"
[[ ! -L "$TARGET" ]] || { printf 'version directory cannot be a symlink\n' >&2; exit 1; }
if [[ -e "$TARGET" ]]; then
  metadata validate "$TARGET" "$VERSION" "$PUBLIC_KEY" > "$WORK/existing-files"
  cmp -s -- "$WORK/staged-files" "$WORK/existing-files"
  while IFS= read -r name; do
    cmp -s -- "$STAGE/$name" "$TARGET/$name" || { printf '同版本资产身份不一致，拒绝覆盖: %s\n' "$name" >&2; exit 1; }
  done < "$WORK/staged-files"
  rm -rf -- "$STAGE"
  STAGE=""
else
  # GNU mv -nT 不会把版本放入已有目录，也不会替换并发出现的目标。
  mv -nT -- "$STAGE" "$TARGET"
  [[ ! -d "$STAGE" ]] || { printf 'version directory appeared during publish; refusing overwrite\n' >&2; exit 1; }
  STAGE=""
fi
if [[ -f "$CHANNEL/stable.json" && "$(metadata version "$CHANNEL/stable.json")" == "$VERSION" ]]; then
  [[ ! -L "$CHANNEL/stable.json" ]]
  metadata same-manifest "$CHANNEL/stable.json" "$TARGET/stable.json"
  verify_channel
  printf 'version %s is already live; verified identical assets\n' "$VERSION"
  exit 0
fi
archive_previous
switch_manifest "$TARGET/stable.json"
printf 'published %s\n' "$VERSION"
if [[ -n "$PREVIOUS_VERSION" ]]; then
  printf 'rollback version: %s (use the same --channel-root and trusted public key)\n' "$PREVIOUS_VERSION"
fi
