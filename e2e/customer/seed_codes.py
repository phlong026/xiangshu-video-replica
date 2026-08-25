"""Seed a dedicated customer-E2E PostgreSQL database with admin + activation codes.

Usage:
    python e2e/customer/seed_codes.py

Reads env (same variables the backend runner exports):
    CUSTOMER_E2E_DATABASE_URL   postgres DSN of the already-migrated E2E database
    VIDEO_REPLICA_ACTIVATION_CODE_HMAC_KEY   plaintext HMAC key (>=32 bytes)

The plaintext codes are printed once at the end so the Playwright spec can
drive the activation UI with them. Never a real key: the E2E runner generates
a throwaway key for the run.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
from app.activation_code_service import (  # noqa: E402
    compute_code_digest,
    mask_activation_code,
)

E2E_CODES = [
    "XS04-ABCDEFG-HJKLMNP-QRSTVWX-YZ23456",
    "XS04-2345678-9ABCDEF-GHJKLMN-PQRSTVW",
    "XS04-XYZ2345-6789ABC-DEFGHJK-MNPQRST",
]


def main() -> None:
    dsn = os.environ["CUSTOMER_E2E_DATABASE_URL"]
    key = os.environ["VIDEO_REPLICA_ACTIVATION_CODE_HMAC_KEY"].encode("utf-8")
    if len(key) < 32:
        raise SystemExit("ACTIVATION_CODE_HMAC_KEY must be at least 32 bytes")

    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO users (id, username, display_name, role) "
            "VALUES ('admin_e2e', 'admin_e2e', 'Admin E2E', 'admin') "
            "ON CONFLICT (id) DO NOTHING"
        )
        for i, code in enumerate(E2E_CODES, start=1):
            code_id = f"e2e-code-{i}"
            batch_id = f"e2e-batch-{i}"
            conn.execute(
                "INSERT INTO activation_code_batches ("
                " id, name, face_value_fen, unit_price_fen_snapshot, credits_snapshot,"
                " quantity, activation_expires_at, status, created_by_user_id, created_at)"
                " VALUES (%s, %s, 10000, 10000, 1000, 1, '2099-01-01T00:00:00+00:00',"
                " 'OPEN', 'admin_e2e', CURRENT_TIMESTAMP) ON CONFLICT (id) DO NOTHING",
                (batch_id, f"E2E Batch {i}"),
            )
            conn.execute(
                "INSERT INTO activation_codes ("
                " id, batch_id, code_digest, digest_key_version, masked_code,"
                " status, issued_at, activated_at)"
                " VALUES (%s, %s, %s, 1, %s, 'ISSUED', CURRENT_TIMESTAMP, NULL)"
                " ON CONFLICT (id) DO NOTHING",
                (
                    code_id,
                    batch_id,
                    compute_code_digest(code, key=key),
                    mask_activation_code(code),
                ),
            )
            print(f"SEEDED {code_id} {code}")

    print(f"SEEDED admin_e2e + {len(E2E_CODES)} codes into {dsn.split('@')[-1]}")


if __name__ == "__main__":
    main()
