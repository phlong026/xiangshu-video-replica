from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TEST = ROOT / "server/tests/test_sqlite_to_postgres.py"
REC = ROOT / "server/scripts/reconcile_customer_billing.py"
MIG = ROOT / "server/scripts/sqlite_to_postgres.py"


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


def pytest_one(node: str) -> subprocess.CompletedProcess[str]:
    return run(
        "uv", "--cache-dir", ".uv-cache", "run", "--project", "server", "--locked",
        "python", "-m", "pytest", "--rootdir", "server", node, "-q", ok=False,
    )


def add_red_tests() -> None:
    security_test = '''def test_redaction_and_error_messages_never_expose_passwords() -> None:
    dsn = (
        "postgresql://migration:super%40secret@db.example/customer"
        "?sslpassword=query%40sensitive&application_name=t07#fragment%40sensitive"
    )
    redacted = redact_postgres_dsn(dsn)
    message = safe_error_message(
        RuntimeError(
            f"failed for {dsn}; credentials: super@secret, query@sensitive, "
            "query%40sensitive, fragment@sensitive, sslpassword"
        ),
        dsn,
    )
    assert redacted == "postgresql://migration@db.example/customer"
    assert (
        redact_postgres_dsn(
            "postgresql://migration:hidden@db.example:notaport/customer"
        )
        == "<redacted-postgres-dsn>"
    )
    for sensitive_value in (
        "super%40secret",
        "super@secret",
        "query@sensitive",
        "query%40sensitive",
        "fragment@sensitive",
        "sslpassword",
    ):
        assert sensitive_value not in message


'''
    edit(
        TEST,
        r"def test_redaction_and_error_messages_never_expose_passwords\(\) -> None:\n.*?(?=\ndef test_table_digest_)",
        security_test.rstrip("\n"),
        flags=re.S,
    )
    red = pytest_one(
        "server/tests/test_sqlite_to_postgres.py::"
        "test_redaction_and_error_messages_never_expose_passwords"
    )
    if red.returncode == 0 or "1 failed" not in red.stdout:
        raise RuntimeError("DSN regression did not produce the expected red test")
    print("T07_RED_SECURITY=1_failed", flush=True)

    edit(
        TEST,
        r"from scripts\.sqlite_to_postgres import \(\n",
        "from scripts.sqlite_to_postgres import (\n"
        "    MIGRATION_ADVISORY_LOCK_KEYS,\n",
    )
    edit(
        TEST,
        r"    require_maintenance_window,\n",
        "    require_maintenance_window,\n    require_migration_lock,\n",
    )
    lock_unit = '''class _BooleanCursor:
    def __init__(self, value: bool) -> None:
        self.value = value

    def fetchone(self) -> tuple[bool]:
        return (self.value,)


class _AdvisoryLockConnection:
    def __init__(self, acquired: bool) -> None:
        self.acquired = acquired

    def execute(self, query: str, params: tuple[int, int]) -> _BooleanCursor:
        assert "pg_try_advisory_xact_lock" in query
        assert params == MIGRATION_ADVISORY_LOCK_KEYS
        return _BooleanCursor(self.acquired)


def test_migration_advisory_lock_fails_closed() -> None:
    require_migration_lock(_AdvisoryLockConnection(True))
    with pytest.raises(MigrationSafetyError, match="another T07 migration"):
        require_migration_lock(_AdvisoryLockConnection(False))


'''
    edit(TEST, r'(?=DEFAULT_PG_DSN = )', lock_unit)
    red = pytest_one(
        "server/tests/test_sqlite_to_postgres.py::test_migration_advisory_lock_fails_closed"
    )
    if red.returncode == 0 or not any(
        marker in red.stdout for marker in ("ImportError", "cannot import name", "ERROR")
    ):
        raise RuntimeError("migration-lock regression did not fail on the missing contract")
    print("T07_RED_CONCURRENCY=missing_contract", flush=True)

    edit(
        TEST,
        r'(\s+target\.execute\(\n\s+"UPDATE assets SET storage_uri = %s WHERE id = %s",\n'
        r'\s+\("cos://private/changed\.mp4", "a-t07"\),\n\s+\)\n)',
        r'''\1            target.execute(
                "UPDATE characters SET reference_asset_ids_json = %s::jsonb WHERE id = %s",
                ('["missing-target-json"]', "c-t07"),
            )
''',
    )
    edit(
        TEST,
        r'(\s+assert \("table_hash_mismatch", "assets"\) in issue_pairs\n)',
        r'''\1        assert (
            "asset_reference_orphan",
            "target:characters.reference_asset_ids_json",
        ) in issue_pairs
''',
    )
    real_lock = '''@pg_only
def test_real_pg_migration_advisory_lock_blocks_concurrent_cutover(
    tmp_path: Path,
) -> None:
    import psycopg

    source = tmp_path / "source.db"
    _create_head_source(source)
    snapshot = create_readonly_snapshot(source, tmp_path / "snapshot.db")
    name = "t07_compact_lock"
    dsn = _create_database(name)
    try:
        _upgrade_pg(dsn)
        with psycopg.connect(dsn) as blocker:
            with blocker.transaction():
                blocker.execute(
                    "SELECT pg_advisory_xact_lock(%s, %s)",
                    MIGRATION_ADVISORY_LOCK_KEYS,
                )
                with pytest.raises(
                    MigrationSafetyError,
                    match="another T07 migration",
                ):
                    migrate_snapshot(snapshot, dsn)
    finally:
        _drop_database(name)


'''
    edit(
        TEST,
        r"(?=@pg_only\ndef test_real_pg_import_reconcile_repeat_and_rollback)",
        real_lock,
    )


def repair_sqlite_fixture() -> None:
    replacement = '''        conn.commit()
    conn.close()

    with sqlite3.connect(path) as checkpoint:
        checkpoint.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        checkpoint.execute("PRAGMA journal_mode = DELETE")


@pg_only'''
    edit(TEST, r"        conn\.commit\(\)\n\n\n@pg_only", replacement)


def implement() -> None:
    edit(
        REC,
        r"from urllib\.parse import quote, unquote, urlsplit, urlunsplit",
        "from urllib.parse import parse_qsl, quote, unquote, urlsplit, urlunsplit",
    )
    redaction = '''def redact_postgres_dsn(dsn: str) -> str:
    "Remove credentials and optional DSN parameters before logging."

    try:
        parts = urlsplit(dsn)
        if not parts.scheme.startswith("postgres"):
            return "<redacted-postgres-dsn>"
        hostname = parts.hostname or ""
        port = parts.port
        username = parts.username
    except (UnicodeError, ValueError):
        return "<redacted-postgres-dsn>"
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    netloc = hostname
    if port is not None:
        netloc += f":{port}"
    if username:
        netloc = f"{quote(unquote(username), safe='')}@{netloc}"
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def _dsn_sensitive_values(dsn: str) -> set[str]:
    candidates = {dsn}
    try:
        parts = urlsplit(dsn)
    except ValueError:
        return candidates
    for value in (parts.username, parts.password, parts.fragment):
        if value:
            decoded = unquote(value)
            candidates.update({value, decoded, quote(decoded, safe="")})
    try:
        query_pairs = parse_qsl(parts.query, keep_blank_values=True, strict_parsing=False)
    except ValueError:
        query_pairs = []
    for key, value in query_pairs:
        if key:
            decoded_key = unquote(key)
            candidates.update({key, decoded_key, quote(decoded_key, safe="")})
        if value:
            decoded_value = unquote(value)
            candidates.update({value, decoded_value, quote(decoded_value, safe="")})
    return candidates


def safe_error_message(error: Exception, dsn: str) -> str:
    message = str(error)
    for candidate in sorted(
        (item for item in _dsn_sensitive_values(dsn) if item),
        key=len,
        reverse=True,
    ):
        message = message.replace(candidate, "<redacted>")
    return message


'''
    edit(
        REC,
        r"def redact_postgres_dsn\(dsn: str\) -> str:\n.*?(?=def _quote_sqlite_identifier)",
        redaction,
        flags=re.S,
    )
    edit(
        MIG,
        r'SEED_TABLES = frozenset\(\{"runtime_settings"\}\)\n',
        'SEED_TABLES = frozenset({"runtime_settings"})\n'
        "MIGRATION_ADVISORY_LOCK_KEYS = (0x543037, 0x44423035)\n",
    )
    lock_function = '''def require_migration_lock(pg_conn: Any) -> None:
    row = pg_conn.execute(
        "SELECT pg_try_advisory_xact_lock(%s, %s) AS acquired",
        MIGRATION_ADVISORY_LOCK_KEYS,
    ).fetchone()
    if row is None:
        raise MigrationSafetyError("migration advisory-lock query returned no row")
    acquired = bool(row["acquired"] if isinstance(row, Mapping) else row[0])
    if not acquired:
        raise MigrationSafetyError(
            "another T07 migration owns the PostgreSQL cutover lock; aborting"
        )


'''
    edit(
        MIG,
        r"(?=def require_validated_postgres_foreign_keys)",
        lock_function,
    )
    edit(
        MIG,
        r"(\s+with pg_conn\.transaction\(\):\n)(\s+)_validate_revisions",
        r"\1\2require_migration_lock(pg_conn)\n\2_validate_revisions",
    )


def verify() -> None:
    base = ("uv", "--cache-dir", ".uv-cache", "run", "--project", "server", "--locked")
    run(
        *base, "ruff", "format",
        "server/scripts/reconcile_customer_billing.py",
        "server/scripts/sqlite_to_postgres.py",
        "server/tests/test_sqlite_to_postgres.py",
    )
    files = (
        "server/app/backup.py",
        "server/scripts/reconcile_customer_billing.py",
        "server/scripts/sqlite_to_postgres.py",
        "server/tests/test_sqlite_to_postgres.py",
    )
    run(*base, "ruff", "check", *files)
    run(*base, "ruff", "format", "--check", *files)
    run(
        *base, "python", "-m", "pytest", "--rootdir", "server",
        "server/tests/test_sqlite_to_postgres.py", "-q",
    )


def evidence() -> None:
    path = ROOT / "docs/evidence/T07-EVIDENCE.md"
    text = path.read_text(encoding="utf-8")
    marker = "## DB-06 维护窗口与回滚契约\n"
    section = '''## 独立安全评审新增红绿证据（2026-08-21）

- DSN 回归：query/fragment 和非法端口不得泄露凭据；修复前专项测试为 `1 failed`，修复后通过。
- 并发回归：两个 PostgreSQL 连接竞争同一 T07 导入时，第二个连接必须由事务级 advisory lock 立即失败关闭；实现前导入符号缺失红测，实现在 PG16 双连接测试中通过。
- 目标侧 JSON 资产引用：导入后篡改 `characters.reference_asset_ids_json` 为孤儿引用，对账必须报告 `target:characters.reference_asset_ids_json`。
- PG16 首次绿测因测试夹具遗留 WAL/SHM 共 `5 failed`；第二次因 SQLite 上下文未关闭连接导致切换 journal mode 时 `database is locked`。夹具现显式关闭连接、checkpoint 并切回 DELETE，生产维护窗口门禁保持不变。
- 三个原始格式失败文件已由仓库锁定 Ruff 版本格式化；最终证据层级仍以正式 PR 三门禁为准。

'''
    if section not in text:
        if marker not in text:
            raise RuntimeError("T07 evidence insertion anchor not found")
        path.write_text(text.replace(marker, section + marker), encoding="utf-8")


def main() -> None:
    add_red_tests()
    repair_sqlite_fixture()
    implement()
    verify()
    evidence()
    print("T07_AUTOFIX_COMPLETE=1", flush=True)


if __name__ == "__main__":
    main()
