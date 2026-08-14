import hashlib
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from packaging_tools.install_skill import (
    InstallError,
    default_target,
    ensure_supported_python,
    install,
    validate_archive,
    verify_checksum,
)
from packaging_tools.build_release import BuildError, _validate_skill_layout, build_release


def make_archive(path: Path, skill_text: str = "skill") -> None:
    with zipfile.ZipFile(path, "w") as bundle:
        bundle.writestr("video-reverse-prompt-script-firstframe/SKILL.md", skill_text)
        bundle.writestr("video-reverse-prompt-script-firstframe/scripts/check.py", "print('ok')\n")


class PortableInstallerTests(unittest.TestCase):
    def test_python_39_or_newer_is_required(self):
        ensure_supported_python((3, 9))
        with self.assertRaisesRegex(InstallError, "Python 3.9"):
            ensure_supported_python((3, 8))

    def test_release_builder_creates_verified_inner_and_one_click_archives(self):
        project_root = Path(__file__).resolve().parents[2]
        skill_dir = project_root / "video-reverse-prompt-script-firstframe"
        with tempfile.TemporaryDirectory() as tmp:
            result = build_release(skill_dir, Path(tmp))
            inner = Path(result["skill_archive"])
            one_click = Path(result["one_click_archive"])
            self.assertTrue(inner.is_file())
            self.assertTrue(one_click.is_file())
            self.assertEqual(result["skill_files"], 23)
            with zipfile.ZipFile(one_click) as bundle:
                names = set(bundle.namelist())
            prefix = "Video_Reverse_SkillS_V0.6.3-OneClick/"
            for name in [
                "install_skill.py",
                "安装-macOS.command",
                "安装-Linux.sh",
                "安装-Windows.bat",
                "README-安装说明.md",
                "SHA256SUMS.txt",
                "video-reverse-prompt-script-firstframe-v0.6.3.zip",
            ]:
                self.assertIn(prefix + name, names)

    def test_default_target_uses_codex_home(self):
        with patch.dict(os.environ, {"CODEX_HOME": "/tmp/custom-codex"}, clear=False):
            self.assertEqual(
                default_target(),
                Path("/tmp/custom-codex/skills/video-reverse-prompt-script-firstframe"),
            )

    def test_checksum_mismatch_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "skill.zip"
            archive.write_bytes(b"archive")
            with self.assertRaisesRegex(InstallError, "checksum"):
                verify_checksum(archive, "0" * 64)

    def test_archive_path_traversal_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "skill.zip"
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr("../escape.txt", "bad")
            with self.assertRaisesRegex(InstallError, "unsafe"):
                validate_archive(archive)

    def test_unsafe_directory_entry_is_rejected_before_extraction(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "skill.zip"
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr("../escape/", "")
                bundle.writestr("video-reverse-prompt-script-firstframe/SKILL.md", "ok")
            with self.assertRaisesRegex(InstallError, "unsafe"):
                validate_archive(archive)

    def test_archive_requires_one_skill_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "skill.zip"
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr("one/SKILL.md", "one")
                bundle.writestr("two/file.txt", "two")
            with self.assertRaisesRegex(InstallError, "one top-level"):
                validate_archive(archive)

    def test_install_is_atomic_and_backs_up_existing_skill(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "skill.zip"
            make_archive(archive, "new")
            checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
            target = root / "skills" / "video-reverse-prompt-script-firstframe"
            target.mkdir(parents=True)
            (target / "SKILL.md").write_text("old", encoding="utf-8")

            result = install(archive, checksum, target, run_tests=False)

            self.assertEqual((target / "SKILL.md").read_text(encoding="utf-8"), "new")
            backup = Path(result["backup"])
            self.assertEqual((backup / "SKILL.md").read_text(encoding="utf-8"), "old")
            self.assertEqual(result["status"], "INSTALLED")

    def test_install_rejects_an_unrelated_target_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "skill.zip"
            make_archive(archive)
            checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
            with self.assertRaisesRegex(InstallError, "target directory"):
                install(archive, checksum, root / "unrelated", run_tests=False)

    def test_release_builder_rejects_unknown_skill_root_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill_dir = Path(tmp) / "video-reverse-prompt-script-firstframe"
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text("skill", encoding="utf-8")
            (skill_dir / ".coverage").write_text("debug", encoding="utf-8")
            with self.assertRaisesRegex(BuildError, "unsupported Skill file"):
                _validate_skill_layout(skill_dir)


if __name__ == "__main__":
    unittest.main()
