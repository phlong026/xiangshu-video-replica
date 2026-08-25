"""Seed the customer-E2E database with a throwaway ZPay merchant configuration.

The recharge browser E2E drives the customer wallet "充值" button, which calls
`POST /api/customer/recharge-orders`. That route reads the ZPay merchant config
from `provider_settings` (decrypted with `VIDEO_REPLICA_SETTINGS_KEY`) and the
deployment config from `ZPAY_GATEWAY_URL` / `PUBLIC_BASE_URL` — the same shape
`test_customer_recharge.py::recharge_config_fixture` uses. Without it the route
answers 503 `ZPAY_CONFIGURATION_INVALID`.

Usage:
    python e2e/customer/seed_zpay.py

Reads:
    CUSTOMER_E2E_DATABASE_URL   postgres DSN of the already-migrated E2E database
    CUSTOMER_E2E_ADMIN_USER     admin actor for updated_by_user_id (default admin_e2e)

Prints `VIDEO_REPLICA_SETTINGS_KEY=<key>` so the E2E runner can pass the same
Fernet key to the API process (the config is encrypted with it). Never a real
key: freshly generated per run, and the merchant credentials are throwaway.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg
from cryptography.fernet import Fernet

ZPAY_MERCHANT_CONFIG = (
    b'{"pid":"merchant-123","key":"merchant-secret","enabled_channels":"alipay,wxpay"}'
)


def main() -> None:
    dsn = os.environ["CUSTOMER_E2E_DATABASE_URL"]
    admin_user = os.environ.get("CUSTOMER_E2E_ADMIN_USER", "admin_e2e")
    settings_fernet_key = Fernet.generate_key()
    encrypted_config = Fernet(settings_fernet_key).encrypt(ZPAY_MERCHANT_CONFIG).decode("ascii")

    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            """
            INSERT INTO provider_settings (
                provider, encrypted_config, updated_by_user_id, created_at, updated_at
            ) VALUES (%s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            ON CONFLICT(provider) DO UPDATE SET
                encrypted_config = excluded.encrypted_config,
                updated_by_user_id = excluded.updated_by_user_id,
                updated_at = CURRENT_TIMESTAMP
            """,
            ("zpay", encrypted_config, admin_user),
        )

    print(f"SEEDED zpay provider_settings for {dsn.split('@')[-1]}")
    # The runner captures this to set VIDEO_REPLICA_SETTINGS_KEY on the API.
    print(f"VIDEO_REPLICA_SETTINGS_KEY={settings_fernet_key.decode('ascii')}")


if __name__ == "__main__":
    main()
