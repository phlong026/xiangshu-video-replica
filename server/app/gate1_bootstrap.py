from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.db import initialize_database
from app.settings import SettingsRepository


def bootstrap_gate1_database(
    db_path: Path,
    *,
    user_id: str,
    display_name: str,
) -> dict[str, str]:
    """Create an empty migrated Gate 1 database with only its desktop identity/runtime."""
    resolved_path = db_path.resolve()
    if resolved_path.exists():
        raise FileExistsError(f"Gate 1 database already exists: {resolved_path}")

    with initialize_database(resolved_path) as conn:
        with conn:
            conn.execute(
                """
                INSERT INTO users (id, username, display_name, role)
                VALUES (?, ?, ?, 'admin')
                """,
                (user_id, user_id, display_name),
            )
        SettingsRepository(conn).save_runtime_settings(
            max_generation_count_per_batch=6,
            max_concurrent_h3_tasks=2,
            active_storage_provider="local",
            actor_user_id=user_id,
        )

    return {
        "database": str(resolved_path),
        "desktop_user_id": user_id,
        "active_storage_provider": "local",
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create a from-scratch local database for the desktop Gate 1 run"
    )
    parser.add_argument("--db-path", required=True, type=Path)
    parser.add_argument("--user-id", default="gate1_admin")
    parser.add_argument("--display-name", default="Gate 1 Admin")
    args = parser.parse_args()
    summary = bootstrap_gate1_database(
        args.db_path,
        user_id=args.user_id,
        display_name=args.display_name,
    )
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
