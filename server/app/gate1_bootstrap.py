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
            conn.execute(
                """
                INSERT INTO wallets (user_id, available_credits, reserved_credits)
                VALUES (?, 10, 0)
                """,
                (user_id,),
            )
            recharge_order_id = f"gate1-recharge-{user_id}"
            conn.execute(
                """
                INSERT INTO recharge_orders (
                    id, user_id, merchant_order_no, channel, status, pricing_scope,
                    base_unit_price_fen_snapshot, charged_unit_price_fen_snapshot,
                    min_recharge_fen_snapshot, recharge_step_fen_snapshot,
                    amount_fen, credits, paid_at
                ) VALUES (?, ?, ?, 'gate1_fixture', 'PAID', 'INTERNAL',
                          1000, 1000, 10000, 1000, 10000, 10, CURRENT_TIMESTAMP)
                """,
                (recharge_order_id, user_id, recharge_order_id),
            )
            conn.execute(
                """
                INSERT INTO wallet_transactions (
                    id, user_id, type, available_delta, reserved_delta,
                    recharge_order_id, task_id, billing_round, idempotency_key
                ) VALUES (?, ?, 'CHARGE', 10, 0, ?, NULL, NULL, ?)
                """,
                (
                    f"gate1-charge-{user_id}",
                    user_id,
                    recharge_order_id,
                    f"gate1-charge:{user_id}",
                ),
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
