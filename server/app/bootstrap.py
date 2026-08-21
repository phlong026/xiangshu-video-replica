from __future__ import annotations

import logging
import os
from pathlib import Path

from cryptography.fernet import Fernet

from app.db import initialize_database
from app.db_pg import (
    DatabaseMode,
    check_pg_ready,
    close_pg_pool,
    resolve_database_config,
    validate_customer_production,
)
from app.local_settings_key import LocalSettingsKeyStoreError, persist_local_settings_key
from app.settings import (
    LOCAL_KEYSTORE_DISABLED_ENV,
    SETTINGS_KEY_ENV,
    SettingsRepository,
    SettingsUnavailableError,
    settings_encryption_key,
)

logger = logging.getLogger(__name__)


def bootstrap_runtime(db_path: str | Path) -> None:
    key = settings_encryption_key()
    with initialize_database(Path(db_path)) as conn:
        # Decrypt every retained provider before starting either process. A
        # wrong key therefore fails closed without overwriting stored data.
        SettingsRepository(conn, fernet=Fernet(key.encode("ascii"))).read_all_provider_configs()

    # Import an explicitly provisioned desktop key only after it has decrypted
    # the current database. Future restarts can then use the OS key store even
    # when the one-time deployment environment is no longer present.
    if os.environ.get(SETTINGS_KEY_ENV) and os.environ.get(LOCAL_KEYSTORE_DISABLED_ENV) != "1":
        persist_local_settings_key(key)


def main() -> None:
    # T05: resolve the database mode first so customer production fails closed
    # before any SQLite file is touched.
    config = resolve_database_config()
    validate_customer_production(config)

    if config.mode is DatabaseMode.POSTGRESQL:
        # PG runtime: warm the pool and verify the server round-trip. Alembic
        # migrations against PG are executed once T06 lands; the ready check
        # itself is the API bootstrap contract for the PG lane.
        ready = check_pg_ready()
        logging.getLogger(__name__).info(
            "PostgreSQL runtime ready (pool_max=%d, server_now=%s)",
            ready.pool_size,
            ready.server_now.isoformat(),
        )
        # bootstrap is a short-lived process: release the pooled connections
        # before exit (M0 review M2; close_pg_pool is a no-op on the SQLite
        # lane, which never opens a pool).
        close_pg_pool()
        return

    db_path_value = config.sqlite_path
    if not db_path_value:
        raise SystemExit("VIDEO_REPLICA_DB_PATH is required")

    try:
        bootstrap_runtime(db_path_value)
    except (SettingsUnavailableError, LocalSettingsKeyStoreError) as exc:
        logger.error("Local settings bootstrap failed: %s", type(exc).__name__)
        raise SystemExit(
            "Local settings are still stored, but the encryption key is unavailable or invalid."
        ) from exc


if __name__ == "__main__":
    main()
