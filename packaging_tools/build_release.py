#!/usr/bin/env python3
"""Build deterministic Skill and one-click installer archives."""

import argparse
import hashlib
import json
import os
import shutil
import stat
import sys
import zipfile
from pathlib import Path
from typing import Dict, Iterable


RELEASE_VERSION = "0.6.3"
SKILL_ARCHIVE_NAME = "video-reverse-prompt-script-firstframe-v0.6.3.zip"
BUNDLE_NAME = "Video_Reverse_SkillS_V%s-OneClick" % RELEASE_VERSION
EXCLUDED_PARTS = {"__pycache__", "video_reverse_runs", "verification", "docs"}
EXCLUDED_NAMES = {".DS_Store"}
EXCLUDED_SUFFIXES = {".pyc", ".pyo"}
ALLOWED_ROOT_FILES = {"SKILL.md"}
ALLOWED_ROOT_DIRS = {"agents", "references", "scripts", "templates", "tests"}
FIXED_TIMESTAMP = (2026, 1, 1, 0, 0, 0)


class BuildError(RuntimeError):
    pass


def _files(root: Path) -> Iterable[Path]:
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if not path.is_file():
            continue
        if EXCLUDED_PARTS.intersection(relative.parts):
            continue
        if path.name in EXCLUDED_NAMES or path.suffix in EXCLUDED_SUFFIXES:
            continue
        yield path


def _validate_skill_layout(skill_dir: Path) -> None:
    for path in sorted(skill_dir.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(skill_dir)
        if EXCLUDED_PARTS.intersection(relative.parts):
            continue
        if path.name in EXCLUDED_NAMES or path.suffix in EXCLUDED_SUFFIXES:
            continue
        allowed = (
            len(relative.parts) == 1 and relative.name in ALLOWED_ROOT_FILES
        ) or (
            len(relative.parts) > 1 and relative.parts[0] in ALLOWED_ROOT_DIRS
        )
        if not allowed:
            raise BuildError("unsupported Skill file: %s" % relative.as_posix())


def _write_entry(bundle: zipfile.ZipFile, name: str, content: bytes, executable: bool) -> None:
    info = zipfile.ZipInfo(name, FIXED_TIMESTAMP)
    info.compress_type = zipfile.ZIP_DEFLATED
    mode = 0o755 if executable else 0o644
    info.external_attr = (stat.S_IFREG | mode) << 16
    bundle.writestr(info, content)


def _write_tree(archive: Path, root: Path, archive_root: str) -> None:
    with zipfile.ZipFile(archive, "w") as bundle:
        for path in _files(root):
            relative = path.relative_to(root).as_posix()
            executable = os.access(path, os.X_OK) or path.suffix in {".command", ".sh"}
            _write_entry(
                bundle,
                "%s/%s" % (archive_root, relative),
                path.read_bytes(),
                executable,
            )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _verify_skill_archive(skill_dir: Path, archive: Path) -> int:
    sys.path.insert(0, str(skill_dir))
    try:
        from scripts.verify_package import PackageValidationError, validate_package

        try:
            return int(validate_package(skill_dir, archive)["files"])
        except PackageValidationError as exc:
            raise BuildError("Skill archive verification failed: %s" % exc)
    finally:
        sys.path.pop(0)


def build_release(skill_dir: Path, output_dir: Path) -> Dict[str, object]:
    skill_dir = skill_dir.expanduser().resolve()
    output_dir = output_dir.expanduser().resolve()
    if not (skill_dir / "SKILL.md").is_file():
        raise BuildError("Skill directory is incomplete: %s" % skill_dir)
    _validate_skill_layout(skill_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    bundle_dir = output_dir / BUNDLE_NAME
    if bundle_dir.exists():
        shutil.rmtree(bundle_dir)
    bundle_dir.mkdir()

    skill_archive = bundle_dir / SKILL_ARCHIVE_NAME
    _write_tree(skill_archive, skill_dir, skill_dir.name)
    file_count = _verify_skill_archive(skill_dir, skill_archive)

    tool_root = Path(__file__).resolve().parent
    shutil.copy2(tool_root / "install_skill.py", bundle_dir / "install_skill.py")
    for asset in _files(tool_root / "assets"):
        shutil.copy2(asset, bundle_dir / asset.name)
    for script in [bundle_dir / "安装-macOS.command", bundle_dir / "安装-Linux.sh"]:
        script.chmod(0o755)

    digest = _sha256(skill_archive)
    (bundle_dir / "SHA256SUMS.txt").write_text(
        "%s  %s\n" % (digest, SKILL_ARCHIVE_NAME), encoding="utf-8"
    )

    one_click_archive = output_dir / (BUNDLE_NAME + ".zip")
    _write_tree(one_click_archive, bundle_dir, bundle_dir.name)
    return {
        "status": "BUILT",
        "version": RELEASE_VERSION,
        "skill_files": file_count,
        "skill_archive": str(skill_archive),
        "skill_sha256": digest,
        "bundle_dir": str(bundle_dir),
        "one_click_archive": str(one_click_archive),
        "one_click_sha256": _sha256(one_click_archive),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("skill_dir", type=Path)
    parser.add_argument("output_dir", type=Path)
    args = parser.parse_args()
    try:
        result = build_release(args.skill_dir, args.output_dir)
    except (BuildError, OSError, zipfile.BadZipFile) as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
