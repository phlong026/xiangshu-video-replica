"""仅向匹配提交的标签预发布上传缺失资产；不覆盖既有文件。"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def gh(*args: str) -> str:
    return subprocess.check_output(["gh", *args], text=True)


def publish(folder: Path) -> None:
    version = json.loads(Path("package.json").read_text())["version"]
    tag = f"v{version}"
    sha = os.environ["GITHUB_SHA"]
    if (
        os.environ.get("GITHUB_EVENT_NAME") != "push"
        or os.environ.get("GITHUB_REF_TYPE") != "tag"
        or os.environ.get("GITHUB_REF") != f"refs/tags/{tag}"
    ):
        raise ValueError("只允许版本标签 push 发布内部预览")
    expected = {
        f"desktop-{tag}-{p}.zip"
        for p in ("windows-x86_64", "macos-arm64", "macos-x86_64")
    }
    if {p.name for p in folder.iterdir()} != expected:
        raise ValueError("发布制品不完整")
    if any(
        p.is_symlink() or not p.is_file() or not p.stat().st_size
        for p in folder.iterdir()
    ):
        raise ValueError("发布制品必须为非空普通文件")
    repo = os.environ["GITHUB_REPOSITORY"]
    releases = json.loads(
        gh("release", "list", "--repo", repo, "--limit", "100", "--json", "tagName")
    )
    if not any(r["tagName"] == tag for r in releases):
        # 列表失败或创建失败直接终止，不把权限/网络错误当作已存在。
        gh(
            "release",
            "create",
            tag,
            "--repo",
            repo,
            "--verify-tag",
            "--target",
            sha,
            "--prerelease",
            "--title",
            f"内部未签名预览 {tag}",
            "--notes",
            "internal-test-unsigned：仅供内部验证，不是正式签名客户发布或 stable 更新。",
        )
    release = json.loads(
        gh(
            "release",
            "view",
            tag,
            "--repo",
            repo,
            "--json",
            "tagName,targetCommitish,isPrerelease,isDraft,assets",
        )
    )
    if (
        release["tagName"] != tag
        or release["targetCommitish"] != sha
        or not release["isPrerelease"]
        or release["isDraft"]
    ):
        raise ValueError("已有 Release 的目标提交或渠道不匹配")
    existing = {a["name"] for a in release["assets"]}
    with tempfile.TemporaryDirectory() as temporary:
        for name in sorted(expected & existing):
            gh(
                "release",
                "download",
                tag,
                "--repo",
                repo,
                "--pattern",
                name,
                "--dir",
                temporary,
            )
            if (Path(temporary) / name).read_bytes() != (folder / name).read_bytes():
                raise ValueError("同名远端资产内容不同，拒绝覆盖")
    missing = sorted(expected - existing)
    if missing:
        gh(
            "release",
            "upload",
            tag,
            "--repo",
            repo,
            *(str(folder / n) for n in missing),
        )


if __name__ == "__main__":
    publish(Path(sys.argv[1]))
