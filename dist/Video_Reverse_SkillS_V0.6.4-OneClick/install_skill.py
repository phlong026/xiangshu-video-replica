#!/usr/bin/env python3
"""Install the bundled Skill safely into Codex on macOS, Linux, or Windows."""

import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Dict, Optional


SKILL_NAME = "video-reverse-prompt-script-firstframe"
ARCHIVE_NAME = "video-reverse-prompt-script-firstframe-v0.6.4.zip"


class InstallError(RuntimeError):
    pass


def ensure_supported_python(version=sys.version_info) -> None:
    if tuple(version[:2]) < (3, 9):
        raise InstallError("Python 3.9 or newer is required")


def default_target() -> Path:
    codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    return codex_home.expanduser() / "skills" / SKILL_NAME


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise InstallError("cannot read archive: %s" % exc)
    return digest.hexdigest()


def verify_checksum(archive: Path, expected: str) -> None:
    expected = expected.strip().lower()
    if len(expected) != 64 or any(char not in "0123456789abcdef" for char in expected):
        raise InstallError("expected checksum is invalid")
    if sha256(archive) != expected:
        raise InstallError("archive checksum mismatch; installation stopped")


def _validate_members(bundle: zipfile.ZipFile) -> str:
    members = bundle.infolist()
    names = [member.filename for member in members]
    if len(names) != len(set(names)):
        raise InstallError("archive contains duplicate entries")
    for member in members:
        name = member.filename
        path = PurePosixPath(name)
        file_type = (member.external_attr >> 16) & 0o170000
        if (
            not name
            or "\\" in name
            or path.is_absolute()
            or ".." in path.parts
            or "" in path.parts
            or file_type == stat.S_IFLNK
        ):
            raise InstallError("unsafe archive path: %s" % name)

    files = [name for name in names if not name.endswith("/")]
    if not files:
        raise InstallError("Skill archive is empty")
    roots = set()
    for name in files:
        path = PurePosixPath(name)
        roots.add(path.parts[0])
    if len(roots) != 1:
        raise InstallError("archive must contain one top-level Skill directory")
    root = next(iter(roots))
    if "%s/SKILL.md" % root not in files:
        raise InstallError("archive does not contain a Skill entrypoint")
    return root


def validate_archive(archive: Path) -> str:
    if not archive.is_file():
        raise InstallError("Skill archive is missing: %s" % archive)
    try:
        with zipfile.ZipFile(archive) as bundle:
            return _validate_members(bundle)
    except (OSError, zipfile.BadZipFile) as exc:
        raise InstallError("invalid Skill archive: %s" % exc)


def _backup_path(target: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    candidate = target.with_name("%s.backup-%s" % (target.name, stamp))
    suffix = 1
    while candidate.exists():
        candidate = target.with_name("%s.backup-%s-%d" % (target.name, stamp, suffix))
        suffix += 1
    return candidate


def _run_tests(skill_dir: Path) -> None:
    environment = dict(os.environ)
    environment["PYTHONPYCACHEPREFIX"] = str(Path(tempfile.gettempdir()) / "video-skill-pycache")
    result = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-q"],
        cwd=str(skill_dir),
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise InstallError("installed Skill self-test failed: %s" % detail)


def install(
    archive: Path,
    expected_checksum: str,
    target: Path,
    run_tests: bool = True,
) -> Dict[str, Optional[str]]:
    ensure_supported_python()
    archive = archive.expanduser().resolve()
    target = target.expanduser().resolve()
    if target.name != SKILL_NAME:
        raise InstallError("target directory must be named %s" % SKILL_NAME)
    verify_checksum(archive, expected_checksum)
    target.parent.mkdir(parents=True, exist_ok=True)

    backup = None
    with tempfile.TemporaryDirectory(prefix="video-skill-install-", dir=str(target.parent)) as tmp:
        staging = Path(tmp)
        try:
            with zipfile.ZipFile(archive) as bundle:
                archive_root = _validate_members(bundle)
                bundle.extractall(staging)
        except (OSError, zipfile.BadZipFile) as exc:
            raise InstallError("cannot extract Skill archive: %s" % exc)
        extracted = staging / archive_root
        if not (extracted / "SKILL.md").is_file():
            raise InstallError("extracted Skill is incomplete")
        if run_tests:
            _run_tests(extracted)

        if target.exists():
            backup = _backup_path(target)
            try:
                os.replace(str(target), str(backup))
            except OSError as exc:
                raise InstallError("cannot back up existing Skill: %s" % exc)
        try:
            os.replace(str(extracted), str(target))
        except OSError as exc:
            if backup and backup.exists() and not target.exists():
                os.replace(str(backup), str(target))
            raise InstallError("cannot activate installed Skill: %s" % exc)

    return {
        "status": "INSTALLED",
        "target": str(target),
        "backup": str(backup) if backup else None,
        "ffmpeg": shutil.which("ffmpeg"),
        "ffprobe": shutil.which("ffprobe"),
    }


def _read_checksum(path: Path, archive_name: str) -> str:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise InstallError("cannot read checksum file: %s" % exc)
    for line in lines:
        parts = line.strip().split()
        if len(parts) >= 2 and parts[-1].lstrip("*") == archive_name:
            return parts[0]
    raise InstallError("checksum file has no entry for %s" % archive_name)


def main() -> int:
    bundle_dir = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, default=bundle_dir / ARCHIVE_NAME)
    parser.add_argument("--checksums", type=Path, default=bundle_dir / "SHA256SUMS.txt")
    parser.add_argument("--target", type=Path, default=default_target())
    parser.add_argument("--skip-tests", action="store_true")
    args = parser.parse_args()
    try:
        expected = _read_checksum(args.checksums, args.archive.name)
        result = install(args.archive, expected, args.target, not args.skip_tests)
    except (InstallError, OSError, subprocess.SubprocessError) as exc:
        print("安装失败：%s" % exc, file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result["ffmpeg"] or not result["ffprobe"]:
        print("提示：Skill 已安装，但运行视频拆解前还需安装 FFmpeg。")
    else:
        print("安装完成。重启 Codex 后即可使用短视频复刻 Skill。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
