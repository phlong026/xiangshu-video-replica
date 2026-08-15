from __future__ import annotations

import argparse
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from app.db import connect_database


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
    if args.command == "backup":
        output = backup_database(args.source, args.backup)
    elif args.command == "restore":
        output = restore_database(args.backup, args.target)
    else:
        output = run_daily_backup(args.source, args.backup_dir)
    print(output)


if __name__ == "__main__":
    main()
