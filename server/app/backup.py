from __future__ import annotations

import argparse
import hashlib
import os
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


def create_readonly_snapshot(
    source_path: str | Path,
    snapshot_path: str | Path,
) -> SqliteSnapshot:
    """Create an atomic, integrity-checked SQLite snapshot without writing the source.

    The source is opened through SQLite's ``mode=ro`` URI and ``query_only`` is
    enabled before the backup API is used. The source file hash is checked
    before and after the snapshot so a maintenance-window migration fails
    closed if another writer changed the file while the snapshot was taken.
    """

    source = Path(source_path)
    snapshot = Path(snapshot_path)
    if not source.is_file():
        raise FileNotFoundError(source)
    if source.resolve() == snapshot.resolve():
        raise ValueError("snapshot path must differ from the source database")

    snapshot.parent.mkdir(parents=True, exist_ok=True)
    temporary = snapshot.with_name(f".{snapshot.name}.tmp")
    temporary.unlink(missing_ok=True)

    before_hash = sha256_file(source)
    source_size = source.stat().st_size
    try:
        with _readonly_connection(source) as source_conn:
            _check_integrity(source_conn)
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
        after_hash = sha256_file(source)
        if before_hash != after_hash:
            raise RuntimeError(
                "SQLite source changed while the read-only snapshot was being created; "
                "stop writers and retry the maintenance window"
            )
        os.replace(temporary, snapshot)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise

    return SqliteSnapshot(
        path=snapshot.resolve(),
        source_sha256=before_hash,
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
