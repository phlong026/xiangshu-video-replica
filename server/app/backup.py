from __future__ import annotations

import argparse
import hashlib
import os
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from app.db import connect_database


@dataclass(frozen=True)
class SqliteSnapshot:
    path: Path
    source_sha256: str
    snapshot_sha256: str
    source_size_bytes: int
    created_at: str

    @property
    def snapshot_path(self) -> Path:
        """Compatibility alias used by the migration and review tests."""

        return self.path

    @property
    def sha256(self) -> str:
        """Compatibility alias for the immutable snapshot digest."""

        return self.snapshot_sha256


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _readonly_connection(database: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(
        f"{database.resolve().as_uri()}?mode=ro",
        uri=True,
        timeout=5,
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def _source_state(source: Path) -> tuple[tuple[str, int, int, str], ...]:
    """Return a stable digest of the SQLite main database and WAL sidecar.

    The shared-memory sidecar is intentionally excluded because merely opening a
    WAL database may update lock bytes in that file. Any committed data that can
    affect the snapshot must appear in the main database or the WAL sidecar.
    """

    state: list[tuple[str, int, int, str]] = []
    for candidate in (source, Path(f"{source}-wal")):
        if candidate.exists():
            stat_result = candidate.stat()
            state.append(
                (
                    candidate.name,
                    stat_result.st_size,
                    stat_result.st_mtime_ns,
                    sha256_file(candidate),
                )
            )
        else:
            state.append((candidate.name, -1, -1, "missing"))
    return tuple(state)


def _reject_uncheckpointed_wal(source: Path) -> None:
    wal = Path(f"{source}-wal")
    if wal.exists() and wal.stat().st_size > 0:
        raise RuntimeError(
            "SQLite WAL contains uncheckpointed frames; stop all writers, run a "
            "TRUNCATE checkpoint, and retry the maintenance-window snapshot"
        )


def _create_private_empty_file(path: Path) -> None:
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
    os.close(descriptor)
    os.chmod(path, 0o600)


def create_readonly_snapshot(
    source_path: str | Path,
    snapshot_path: str | Path,
) -> SqliteSnapshot:
    """Create an immutable, private and integrity-checked SQLite snapshot.

    The source is never opened for writing. A non-empty WAL sidecar is rejected
    so committed WAL-only changes cannot be silently omitted. The main database
    and WAL state are compared before and after the backup, while SQLite's
    ``data_version`` guards commits observed by the read connection. The target
    path is created with ``O_EXCL`` and therefore can never overwrite rollback
    evidence from a previous migration rehearsal.
    """

    source = Path(source_path)
    snapshot = Path(snapshot_path)
    if not source.is_file():
        raise FileNotFoundError(source)
    if source.resolve() == snapshot.resolve():
        raise ValueError("snapshot path must differ from the source database")
    if snapshot.exists():
        raise FileExistsError(snapshot)

    snapshot.parent.mkdir(parents=True, exist_ok=True)
    temporary = snapshot.with_name(f".{snapshot.name}.{secrets.token_hex(8)}.tmp")
    _reject_uncheckpointed_wal(source)
    before_state = _source_state(source)
    source_hash = sha256_file(source)
    source_size = source.stat().st_size

    try:
        _create_private_empty_file(temporary)
        with _readonly_connection(source) as source_conn:
            _check_integrity(source_conn)
            journal_mode_row = source_conn.execute("PRAGMA journal_mode").fetchone()
            journal_mode = "" if journal_mode_row is None else str(journal_mode_row[0]).lower()
            if journal_mode == "wal":
                _reject_uncheckpointed_wal(source)
            data_version_before = int(source_conn.execute("PRAGMA data_version").fetchone()[0])
            with sqlite3.connect(temporary) as snapshot_conn:
                source_conn.backup(snapshot_conn)
                _check_integrity(snapshot_conn)
            data_version_after = int(source_conn.execute("PRAGMA data_version").fetchone()[0])
            if data_version_before != data_version_after:
                raise RuntimeError(
                    "SQLite source changed while the read-only snapshot was being created; "
                    "stop writers and retry the maintenance window"
                )

        _reject_uncheckpointed_wal(source)
        after_state = _source_state(source)
        if before_state != after_state or source_hash != sha256_file(source):
            raise RuntimeError(
                "SQLite source or WAL changed while the read-only snapshot was being created; "
                "stop writers, checkpoint WAL, and retry"
            )

        os.chmod(temporary, 0o600)
        _create_private_empty_file(snapshot)
        os.replace(temporary, snapshot)
        os.chmod(snapshot, 0o600)
        with sqlite3.connect(snapshot) as snapshot_conn:
            _check_integrity(snapshot_conn)
    except Exception:
        temporary.unlink(missing_ok=True)
        if snapshot.exists() and snapshot.stat().st_size == 0:
            snapshot.unlink(missing_ok=True)
        raise

    return SqliteSnapshot(
        path=snapshot.resolve(),
        source_sha256=source_hash,
        snapshot_sha256=sha256_file(snapshot),
        source_size_bytes=source_size,
        created_at=datetime.now(UTC).isoformat(),
    )


def check_database(database_path: str | Path) -> Path:
    database = Path(database_path)
    if not database.exists():
        raise FileNotFoundError(database)

    with connect_database(database) as conn:
        _check_integrity(conn)
    return database.resolve()


def backup_database(source_path: str | Path, backup_path: str | Path) -> Path:
    source = Path(source_path)
    backup = Path(backup_path)
    if not source.exists():
        raise FileNotFoundError(source)

    backup.parent.mkdir(parents=True, exist_ok=True)
    temp_backup = backup.with_name(f".{backup.name}.tmp")
    if temp_backup.exists():
        temp_backup.unlink()

    with connect_database(source) as source_conn:
        _check_integrity(source_conn)
        with sqlite3.connect(temp_backup) as backup_conn:
            source_conn.backup(backup_conn)
            _check_integrity(backup_conn)

    os.replace(temp_backup, backup)
    return backup.resolve()


def restore_database(backup_path: str | Path, target_path: str | Path) -> Path:
    backup = Path(backup_path)
    target = Path(target_path)
    if not backup.exists():
        raise FileNotFoundError(backup)

    target.parent.mkdir(parents=True, exist_ok=True)
    temp_target = target.with_name(f".{target.name}.tmp")
    if temp_target.exists():
        temp_target.unlink()

    with sqlite3.connect(backup) as backup_conn:
        _check_integrity(backup_conn)
        with sqlite3.connect(temp_target) as target_conn:
            backup_conn.backup(target_conn)
            _check_integrity(target_conn)

    os.replace(temp_target, target)
    return target.resolve()


def run_daily_backup(source_path: str | Path, backup_dir: str | Path) -> Path:
    source = Path(source_path)
    timestamp = datetime.now(UTC).strftime("%Y%m%d%H%M")
    backup_path = backup_database(source, Path(backup_dir) / f"{source.stem}-{timestamp}.db")
    _prune_backups(Path(backup_dir), source.stem, keep=BACKUP_RETENTION_COUNT)
    return backup_path


BACKUP_RETENTION_COUNT = 30


def _prune_backups(backup_dir: Path, stem: str, *, keep: int) -> None:
    backups = sorted(backup_dir.glob(f"{stem}-*.db"))
    for stale in backups[:-keep]:
        stale.unlink(missing_ok=True)


def _check_integrity(conn: sqlite3.Connection) -> None:
    result = conn.execute("PRAGMA integrity_check").fetchone()
    if result is None or result[0] != "ok":
        detail = "missing result" if result is None else str(result[0])
        raise sqlite3.DatabaseError(f"SQLite integrity check failed: {detail}")


def main() -> None:
    parser = argparse.ArgumentParser(description="SQLite backup and restore tools")
    subparsers = parser.add_subparsers(dest="command", required=True)

    check_parser = subparsers.add_parser("check")
    check_parser.add_argument("database")

    backup_parser = subparsers.add_parser("backup")
    backup_parser.add_argument("source")
    backup_parser.add_argument("backup")

    restore_parser = subparsers.add_parser("restore")
    restore_parser.add_argument("backup")
    restore_parser.add_argument("target")

    daily_parser = subparsers.add_parser("daily")
    daily_parser.add_argument("source")
    daily_parser.add_argument("backup_dir")

    args = parser.parse_args()
    if args.command == "check":
        output = check_database(args.database)
    elif args.command == "backup":
        output = backup_database(args.source, args.backup)
    elif args.command == "restore":
        output = restore_database(args.backup, args.target)
    else:
        output = run_daily_backup(args.source, args.backup_dir)
    print(output)


if __name__ == "__main__":
    main()
