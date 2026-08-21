from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TEST = ROOT / "server/tests/test_sqlite_to_postgres.py"
BACKUP = ROOT / "server/app/backup.py"
RECONCILE = ROOT / "server/scripts/reconcile_customer_billing.py"
MIGRATION = ROOT / "server/scripts/sqlite_to_postgres.py"


def edit(path: Path, pattern: str, replacement: str, *, flags: int = 0) -> None:
    text = path.read_text(encoding="utf-8")
    updated, count = re.subn(pattern, replacement, text, count=1, flags=flags)
    if count != 1:
        raise RuntimeError(f"patch anchor failed for {path}: {pattern!r} ({count})")
    path.write_text(updated, encoding="utf-8")


def run(*args: str, ok: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        args,
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    print("$", *args, flush=True)
    print(result.stdout, flush=True)
    if ok and result.returncode:
        raise RuntimeError(f"command failed ({result.returncode}): {' '.join(args)}")
    return result


def add_red_tests() -> None:
    edit(
        TEST,
        r"import pytest\n\nfrom app\.backup import create_readonly_snapshot",
        "import pytest\n\nfrom app import backup as backup_module\n"
        "from app.backup import create_readonly_snapshot",
    )
    tests = '''class _TrackingConnection:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self.closed = False

    def __getattr__(self, name: str):  # type: ignore[no-untyped-def]
        return getattr(self.connection, name)

    def __enter__(self) -> _TrackingConnection:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if exc_type is None:
            self.connection.commit()
        else:
            self.connection.rollback()

    def close(self) -> None:
        self.closed = True
        self.connection.close()


def test_snapshot_closes_readonly_connection_deterministically(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.db"
    snapshot = tmp_path / "snapshot.db"
    _basic_sqlite(source)
    tracking = _TrackingConnection(backup_module._readonly_connection(source))

    def open_tracking(_: Path) -> _TrackingConnection:
        return tracking

    monkeypatch.setattr(backup_module, "_readonly_connection", open_tracking)
    create_readonly_snapshot(source, snapshot)

    assert tracking.closed


def test_snapshot_publish_rolls_back_when_temp_cleanup_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    temporary = tmp_path / ".snapshot.tmp"
    destination = tmp_path / "snapshot.db"
    temporary.write_bytes(b"immutable-evidence")
    original_unlink = Path.unlink

    def fail_temporary_unlink(self: Path, missing_ok: bool = False) -> None:
        if self == temporary:
            raise OSError("injected temporary cleanup failure")
        original_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", fail_temporary_unlink)
    with pytest.raises(OSError, match="injected temporary cleanup failure"):
        backup_module._publish_without_overwrite(temporary, destination)

    assert temporary.exists()
    assert not destination.exists()


'''
    edit(TEST, r"(?=def test_snapshot_refuses_existing_evidence)", tests)
    result = run(
        "uv",
        "--cache-dir",
        ".uv-cache",
        "run",
        "--project",
        "server",
        "--locked",
        "python",
        "-m",
        "pytest",
        "--rootdir",
        "server",
        "server/tests/test_sqlite_to_postgres.py::test_snapshot_closes_readonly_connection_deterministically",
        "server/tests/test_sqlite_to_postgres.py::test_snapshot_publish_rolls_back_when_temp_cleanup_fails",
        "-q",
        ok=False,
    )
    if result.returncode == 0 or "2 failed" not in result.stdout:
        raise RuntimeError("snapshot resource/atomicity regressions did not produce two red tests")
    print("T07_RED_SNAPSHOT_RESOURCE_ATOMICITY=2_failed", flush=True)


def implement() -> None:
    edit(BACKUP, r"import sqlite3\n", "import sqlite3\nfrom contextlib import closing\n")
    publish = '''def _publish_without_overwrite(temporary: Path, destination: Path) -> None:
    """Atomically publish a same-directory file without replacing evidence."""

    try:
        os.link(temporary, destination)
    except FileExistsError:
        raise FileExistsError(f"migration snapshot already exists: {destination}") from None
    try:
        temporary.unlink()
    except Exception:
        destination.unlink(missing_ok=True)
        raise


'''
    edit(
        BACKUP,
        r"def _publish_without_overwrite\(temporary: Path, destination: Path\) -> None:\n.*?(?=def create_readonly_snapshot)",
        publish,
        flags=re.S,
    )
    edit(
        BACKUP,
        r"with _readonly_connection\(source\) as source_conn:",
        "with closing(_readonly_connection(source)) as source_conn:",
    )
    edit(
        BACKUP,
        r"with sqlite3\.connect\(temporary\) as snapshot_conn:",
        "with closing(sqlite3.connect(temporary)) as snapshot_conn:",
    )

    edit(RECONCILE, r"import sqlite3\n", "import sqlite3\nfrom contextlib import closing\n")
    edit(
        RECONCILE,
        r"with connect_sqlite_readonly\(sqlite_path\) as sqlite_conn:",
        "with closing(connect_sqlite_readonly(sqlite_path)) as sqlite_conn:",
    )

    edit(MIGRATION, r"import sqlite3\n", "import sqlite3\nfrom contextlib import closing\n")
    edit(
        MIGRATION,
        r"with connect_sqlite_readonly\(snapshot\.path\) as sqlite_conn:",
        "with closing(connect_sqlite_readonly(snapshot.path)) as sqlite_conn:",
    )


def verify() -> None:
    base = ("uv", "--cache-dir", ".uv-cache", "run", "--project", "server", "--locked")
    files = (
        "server/app/backup.py",
        "server/scripts/reconcile_customer_billing.py",
        "server/scripts/sqlite_to_postgres.py",
        "server/tests/test_sqlite_to_postgres.py",
    )
    run(*base, "ruff", "format", *files)
    run(*base, "ruff", "check", *files)
    run(*base, "ruff", "format", "--check", *files)
    run(
        *base,
        "python",
        "-m",
        "pytest",
        "--rootdir",
        "server",
        "server/tests/test_sqlite_to_postgres.py",
        "-q",
    )


def evidence() -> None:
    path = ROOT / "docs/evidence/T07-EVIDENCE.md"
    text = path.read_text(encoding="utf-8")
    marker = "## DB-06 维护窗口与回滚契约\n"
    section = '''## 最终快照原子性评审红绿证据（2026-08-21）

- 红测：SQLite 连接上下文只提交、不关闭；hard-link 已发布后临时文件删除失败会残留目标快照。两个专项回归结果为 `2 failed`。
- 绿测：快照源连接、目标连接和迁移/对账只读连接均显式关闭；发布清理失败时回删目标 link，保持“成功或无目标”的原子语义。
- PostgreSQL 16 下完整 T07 专项测试在修复后通过；最终任务状态仍以标准三门禁为准。

'''
    if section not in text:
        if marker not in text:
            raise RuntimeError("T07 evidence insertion anchor not found")
        path.write_text(text.replace(marker, section + marker), encoding="utf-8")


def main() -> None:
    add_red_tests()
    implement()
    verify()
    evidence()
    print("T07_REVIEW_AUTOFIX_COMPLETE=1", flush=True)


if __name__ == "__main__":
    main()
