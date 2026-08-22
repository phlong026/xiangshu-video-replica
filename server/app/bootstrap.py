from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from pathlib import Path

from cryptography.fernet import Fernet

from app.db import initialize_database
from app.db_pg import (
    CUSTOMER_PRODUCTION_ENV,
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

# T09 / DB-08 — customer-production security gate (dev doc §17).
_TRUTHY = {"1", "true", "yes", "on"}
_ADMIN_SESSION_HMAC_KEY_ENV = "VIDEO_REPLICA_ADMIN_SESSION_HMAC_KEY"
_ADMIN_KEY_VERSION_PREFIX = f"{_ADMIN_SESSION_HMAC_KEY_ENV}_V"
# Keep in sync with admin_auth_routes.MIN_HMAC_KEY_BYTES; importing the route
# module here would drag the FastAPI dependency chain into bootstrap.
_MIN_ADMIN_KEY_BYTES = 32
_LEGACY_CONTROL_ENVS = ("CONTROL_PROXY_TOKEN_DIGEST", "CONTROL_ADMIN_USER_ID")
_DEV_AUTH_MODES = {"desktop", "development"}


def _is_customer_production() -> bool:
    return os.environ.get(CUSTOMER_PRODUCTION_ENV, "").strip().lower() in _TRUTHY


def _configured_admin_session_keys(
    environ: Mapping[str, str],
) -> list[tuple[str, str]]:
    """Every configured admin-session HMAC key variable (un-suffixed or ``_VN``).

    Key rotation retires old versions once outstanding credentials expire, so
    any configured version (e.g. only ``_V2`` after retiring ``_V1``) must keep
    customer production booting instead of tripping a V1-only check.
    """
    found: list[tuple[str, str]] = []
    for name in sorted(environ):
        value = environ[name].strip()
        if not value:
            continue
        if name == _ADMIN_SESSION_HMAC_KEY_ENV:
            found.append((name, value))
        elif name.startswith(_ADMIN_KEY_VERSION_PREFIX):
            suffix = name[len(_ADMIN_KEY_VERSION_PREFIX) :]
            if suffix.isdigit() and int(suffix) >= 1:
                found.append((name, value))
    return found


def customer_production_security_violations() -> list[str]:
    """List every customer-production boundary violation in the environment.

    No-op outside customer production so internal P0 deployments keep their
    dev identity, local assets and legacy control proxy token.
    """
    if not _is_customer_production():
        return []
    violations: list[str] = []
    legacy = [name for name in _LEGACY_CONTROL_ENVS if os.environ.get(name, "").strip()]
    if legacy:
        violations.append(
            "legacy single-admin control identity must not represent operators in "
            f"customer production: unset {', '.join(legacy)}"
        )
    auth_mode = os.environ.get("VIDEO_REPLICA_AUTH_MODE", "").strip().lower()
    if auth_mode in _DEV_AUTH_MODES:
        violations.append(
            f"development identity mode is forbidden in customer production: "
            f"VIDEO_REPLICA_AUTH_MODE={auth_mode}"
        )
    if os.environ.get("VIDEO_REPLICA_ALLOW_DEV_IDENTITY_HEADER", "").strip() == "1":
        violations.append(
            "VIDEO_REPLICA_ALLOW_DEV_IDENTITY_HEADER=1 is forbidden in customer production"
        )
    if os.environ.get("VIDEO_REPLICA_DESKTOP_USER_ID", "").strip():
        violations.append(
            "VIDEO_REPLICA_DESKTOP_USER_ID (dev identity) is forbidden in customer production"
        )
    if os.environ.get("VIDEO_REPLICA_STORAGE_ROOT", "").strip():
        violations.append(
            "persistent local assets (VIDEO_REPLICA_STORAGE_ROOT) are forbidden in "
            "customer production: configure the private COS storage provider instead"
        )
    configured_keys = _configured_admin_session_keys(os.environ)
    if not configured_keys:
        violations.append(
            "admin session HMAC key is missing: set "
            f"{_ADMIN_SESSION_HMAC_KEY_ENV}_V1, the un-suffixed "
            f"{_ADMIN_SESSION_HMAC_KEY_ENV}, or any later key version kept "
            "after a rotation"
        )
    else:
        # A configured-but-weak key must fail the boot itself instead of
        # surfacing later as a runtime error from admin_hmac_key().
        for name, value in configured_keys:
            if len(value.encode("utf-8")) < _MIN_ADMIN_KEY_BYTES:
                violations.append(
                    f"{name} must be at least {_MIN_ADMIN_KEY_BYTES} bytes "
                    "for the customer-production admin session HMAC key"
                )
    return violations


def assert_customer_production_security() -> None:
    """Fail closed (RuntimeError) when a customer-production boot carries any
    forbidden legacy/dev/local configuration (T09 exit gate)."""
    violations = customer_production_security_violations()
    if violations:
        raise RuntimeError(
            "customer production security gate failed:\n- " + "\n- ".join(violations)
        )


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
    # before any SQLite file is touched. T09: the security gate then rejects
    # legacy single-admin mappings, dev identities, local assets and missing
    # admin-session keys before the ready check or any pool warm-up.
    config = resolve_database_config()
    validate_customer_production(config)
    assert_customer_production_security()

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
