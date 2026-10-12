"""验证三平台出处后生成独立归档，避免同名清单被覆盖。"""

import hashlib
import json
import os
import re
import sys
import zipfile
from pathlib import Path

PLATFORMS = {
    "windows-x86_64": (".exe", "unsigned"),
    "macos-arm64": (".dmg", "ad-hoc/unnotarized"),
    "macos-x86_64": (".dmg", "ad-hoc/unnotarized"),
}


def prepare(source: Path, output: Path) -> None:
    sha = os.environ["GITHUB_SHA"]
    version = json.loads(Path("package.json").read_text())["version"]
    if os.environ["GITHUB_REF"] != f"refs/tags/v{version}":
        raise ValueError("标签必须与发行版本一致")
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("无效来源提交")
    if {p.name for p in source.iterdir()} != {f"desktop-{p}" for p in PLATFORMS}:
        raise ValueError("三平台制品不完整或含多余平台")
    validated = []
    origins = set()
    for platform, (suffix, signature) in PLATFORMS.items():
        folder = source / f"desktop-{platform}"
        manifest = json.loads((folder / "manifest.json").read_text())
        if any(
            manifest.get(k) != v
            for k, v in {
                "source_sha": sha,
                "version": version,
                "platform": platform,
                "signature": signature,
            }.items()
        ):
            raise ValueError("出处与本轮构建不一致")
        origins.add(manifest["api_base_url"])
        if (folder / "RELEASE-CHANNEL.txt").read_text() != "internal-test-unsigned\n":
            raise ValueError("内部渠道标记不一致")
        files = {}
        for line in (folder / "SHA256SUMS.txt").read_text().splitlines():
            digest, name = line.split("  ", 1)
            if (
                not re.fullmatch(r"[0-9a-f]{64}", digest)
                or Path(name).name != name
                or "\\" in name
            ):
                raise ValueError("不安全的校验清单")
            path = folder / name
            if name in files or path.is_symlink() or not path.is_file():
                raise ValueError("重复或非普通制品")
            if (
                not path.stat().st_size
                or hashlib.sha256(path.read_bytes()).hexdigest() != digest
            ):
                raise ValueError("制品校验失败")
            files[name] = digest
        artifact = manifest["artifact"]
        if not artifact.endswith(suffix) or files.get(artifact) != manifest["sha256"]:
            raise ValueError("安装包清单不匹配")
        allowed = {artifact}
        if platform.startswith("macos"):
            allowed.add(artifact.removesuffix(".dmg") + ".app.tar.gz")
        if set(files) - allowed:
            raise ValueError("未知安装制品")
        expected = set(files) | {
            "manifest.json",
            "SHA256SUMS.txt",
            "RELEASE-CHANNEL.txt",
        }
        if {p.name for p in folder.iterdir()} != expected or any(
            p.is_symlink() for p in folder.iterdir()
        ):
            raise ValueError("制品目录包含未核验内容")
        validated.append((platform, folder, expected))
    if len(origins) != 1:
        raise ValueError("三平台 API 地址不一致")
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise ValueError("输出目录必须为空")
    for platform, folder, names in validated:
        with zipfile.ZipFile(
            output / f"desktop-v{version}-{platform}.zip", "w", zipfile.ZIP_DEFLATED
        ) as archive:
            for name in sorted(names):
                info = zipfile.ZipInfo(f"{platform}/{name}")
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o100644 << 16
                archive.writestr(info, (folder / name).read_bytes())


if __name__ == "__main__":
    prepare(Path(sys.argv[1]), Path(sys.argv[2]))
