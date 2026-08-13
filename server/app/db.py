from __future__ import annotations

import sqlite3
from pathlib import Path

from alembic import command
from alembic.config import Config

BUSY_TIMEOUT_MS = 5000
SERVER_DIR = Path(__file__).resolve().parent.parent


def connect_database(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    if path != Path(":memory:"):
        path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(path, timeout=BUSY_TIMEOUT_MS / 1000)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    return conn


def initialize_database(db_path: str | Path) -> sqlite3.Connection:
    upgrade_database(db_path)
    return connect_database(db_path)


def upgrade_database(db_path: str | Path, revision: str = "head") -> None:
    path = Path(db_path)
    if path != Path(":memory:"):
        path.parent.mkdir(parents=True, exist_ok=True)
    command.upgrade(alembic_config(db_path), revision)


def alembic_config(db_path: str | Path) -> Config:
    config = Config(str(SERVER_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(SERVER_DIR / "migrations"))
    config.set_main_option("sqlalchemy.url", _sqlite_url(db_path))
    return config


def _sqlite_url(db_path: str | Path) -> str:
    if db_path == ":memory:":
        return "sqlite:///:memory:"
    return f"sqlite:///{Path(db_path).resolve()}"
