from __future__ import annotations

import base64
import binascii
import ipaddress
import logging
import os
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import urlsplit

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
from app.db_portable import BusinessConnection
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
CUSTOMER_PUBLIC_ORIGIN_ENV = "VIDEO_REPLICA_PUBLIC_ORIGIN"
PUBLIC_BASE_URL_ENV = "PUBLIC_BASE_URL"
TRUSTED_PROXY_CIDRS_ENV = "VIDEO_REPLICA_TRUSTED_PROXY_CIDRS"
_ACTIVATION_CODE_HMAC_KEY_ENV = "VIDEO_REPLICA_ACTIVATION_CODE_HMAC_KEY"
_ACTIVATION_EXPORT_AEAD_KEY_ENV = "VIDEO_REPLICA_ACTIVATION_EXPORT_AEAD_KEY"
_DEVICE_FINGERPRINT_HMAC_KEY_ENV = "VIDEO_REPLICA_DEVICE_FINGERPRINT_HMAC_KEY"
_CUSTOMER_IDEMPOTENCY_AEAD_KEY_ENV = "VIDEO_REPLICA_CUSTOMER_IDEMPOTENCY_AEAD_KEY"
# Keep in sync with admin_auth_routes.MIN_HMAC_KEY_BYTES; importing the route
# module here would drag the FastAPI dependency chain into bootstrap.
_MIN_ADMIN_KEY_BYTES = 32
_MIN_HMAC_KEY_BYTES = 32
_AEAD_KEY_BYTES = 32
_MAX_KEY_VERSION = 64
_LEGACY_CONTROL_ENVS = ("CONTROL_PROXY_TOKEN_DIGEST", "CONTROL_ADMIN_USER_ID")
_DEV_AUTH_MODES = {"desktop", "development"}


def is_customer_production(*, environ: Mapping[str, str] | None = None) -> bool:
    source = os.environ if environ is None else environ
    return source.get(CUSTOMER_PRODUCTION_ENV, "").strip().lower() in _TRUTHY


def customer_public_origin(*, environ: Mapping[str, str] | None = None) -> str:
    """Return the canonical HTTPS browser origin for the customer deployment."""
    source = os.environ if environ is None else environ
    raw = source.get(CUSTOMER_PUBLIC_ORIGIN_ENV, "").strip()
    if not raw:
        raise ValueError(f"{CUSTOMER_PUBLIC_ORIGIN_ENV} is required")
    if any(character.isspace() for character in raw):
        raise ValueError(f"{CUSTOMER_PUBLIC_ORIGIN_ENV} must not contain whitespace")
    try:
        parsed = urlsplit(raw)
        parsed_hostname = parsed.hostname
    except ValueError as exc:
        raise ValueError(f"{CUSTOMER_PUBLIC_ORIGIN_ENV} is not a valid origin") from exc
    if (
        parsed.scheme.lower() != "https"
        or not parsed_hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            f"{CUSTOMER_PUBLIC_ORIGIN_ENV} must be an HTTPS origin without credentials, "
            "path, query or fragment"
        )
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"{CUSTOMER_PUBLIC_ORIGIN_ENV} has an invalid port") from exc
    try:
        hostname = parsed_hostname.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise ValueError(f"{CUSTOMER_PUBLIC_ORIGIN_ENV} has an invalid hostname") from exc
    authority = f"[{hostname}]" if ":" in hostname else hostname
    if port not in (None, 443):
        authority = f"{authority}:{port}"
    return f"https://{authority}"


def trusted_proxy_networks(
    *, environ: Mapping[str, str] | None = None
) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """Parse the exact proxy/LB networks allowed to assert forwarding headers."""
    source = os.environ if environ is None else environ
    raw = source.get(TRUSTED_PROXY_CIDRS_ENV, "").strip()
    if not raw:
        raise ValueError(f"{TRUSTED_PROXY_CIDRS_ENV} is required")
    values = [item.strip() for item in raw.split(",")]
    if not values or any(not item for item in values):
        raise ValueError(f"{TRUSTED_PROXY_CIDRS_ENV} contains an empty entry")
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for value in values:
        try:
            network = ipaddress.ip_network(value, strict=True)
        except ValueError as exc:
            raise ValueError(
                f"{TRUSTED_PROXY_CIDRS_ENV} contains an invalid canonical CIDR: {value}"
            ) from exc
        if network.prefixlen == 0:
            raise ValueError(f"{TRUSTED_PROXY_CIDRS_ENV} must not trust the entire internet")
        networks.append(network)
    return tuple(networks)


def _configured_versioned_values(
    environ: Mapping[str, str], base_env: str
) -> list[tuple[str, str]]:
    """Resolve configured rotation values with explicit ``_V1`` precedence."""
    values: dict[int, tuple[str, str]] = {}
    base_value = environ.get(base_env, "").strip()
    if base_value:
        values[1] = (base_env, base_value)
    prefix = f"{base_env}_V"
    for name in sorted(environ):
        if not name.startswith(prefix):
            continue
        suffix = name[len(prefix) :]
        value = environ[name].strip()
        if (
            value
            and suffix.isdigit()
            and 1 <= int(suffix) <= _MAX_KEY_VERSION
            and not suffix.startswith("0")
        ):
            values[int(suffix)] = (name, value)
    return [values[version] for version in sorted(values)]


def _append_raw_hmac_key_violations(
    violations: list[str], environ: Mapping[str, str], base_env: str
) -> None:
    configured = _configured_versioned_values(environ, base_env)
    if not configured:
        violations.append(f"{base_env} is missing: configure at least one key version")
        return
    for name, value in configured:
        if len(value.encode("utf-8")) < _MIN_HMAC_KEY_BYTES:
            violations.append(f"{name} must be at least {_MIN_HMAC_KEY_BYTES} bytes")


def _append_aead_key_violations(
    violations: list[str], environ: Mapping[str, str], base_env: str
) -> None:
    configured = _configured_versioned_values(environ, base_env)
    if not configured:
        violations.append(f"{base_env} is missing: configure at least one key version")
        return
    for name, value in configured:
        try:
            decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        except (ValueError, binascii.Error):
            violations.append(f"{name} must be valid urlsafe base64")
            continue
        if len(decoded) != _AEAD_KEY_BYTES:
            violations.append(f"{name} must decode to exactly {_AEAD_KEY_BYTES} bytes")


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
            # Reject zero-padded suffixes like _V01: they pass int() >= 1 but
            # admin_hmac_key(1) only reads _V1, so accepting them would boot
            # the door open while every login 401s (M1 review LOW).
            if suffix.isdigit() and int(suffix) >= 1 and not suffix.startswith("0"):
                found.append((name, value))
    return found


def customer_production_security_violations() -> list[str]:
    """List every customer-production boundary violation in the environment.

    No-op outside customer production so internal P0 deployments keep their
    dev identity, local assets and legacy control proxy token.
    """
    if not is_customer_production():
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
    public_origin: str | None = None
    try:
        public_origin = customer_public_origin()
    except ValueError as exc:
        violations.append(str(exc))
    public_base_url = os.environ.get(PUBLIC_BASE_URL_ENV, "").strip()
    if not public_base_url:
        violations.append(
            f"{PUBLIC_BASE_URL_ENV} is required in customer production and must equal "
            f"{CUSTOMER_PUBLIC_ORIGIN_ENV}"
        )
    elif public_origin is not None and public_base_url != public_origin:
        violations.append(
            f"{PUBLIC_BASE_URL_ENV} must exactly equal {CUSTOMER_PUBLIC_ORIGIN_ENV} "
            "so browser ingress, signed asset URLs and payment callbacks share one origin"
        )
    try:
        trusted_proxy_networks()
    except ValueError as exc:
        violations.append(str(exc))

    _append_raw_hmac_key_violations(violations, os.environ, _ACTIVATION_CODE_HMAC_KEY_ENV)
    _append_aead_key_violations(violations, os.environ, _ACTIVATION_EXPORT_AEAD_KEY_ENV)
    _append_raw_hmac_key_violations(violations, os.environ, _DEVICE_FINGERPRINT_HMAC_KEY_ENV)
    _append_aead_key_violations(violations, os.environ, _CUSTOMER_IDEMPOTENCY_AEAD_KEY_ENV)
    settings_key = os.environ.get(SETTINGS_KEY_ENV, "").strip()
    if not settings_key:
        violations.append(
            f"{SETTINGS_KEY_ENV} is required in customer production; an app-local OS "
            "keystore is not a shared multi-instance secret source"
        )
    else:
        try:
            Fernet(settings_key.encode("ascii"))
        except (UnicodeEncodeError, ValueError):
            violations.append(f"{SETTINGS_KEY_ENV} must be a valid Fernet key")

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
    # M1 review M2: an explicitly configured but out-of-range admin-session
    # TTL must fail the boot itself instead of surfacing later as a 500 on
    # the first exchange — the same fail-later shape PR #40 P2-2 fixed for
    # keys. Delayed import: bootstrap must not pull the FastAPI layer.
    from app.admin_auth_routes import (
        ADMIN_SESSION_TTL_ENV,
        MAX_ADMIN_SESSION_TTL_SECONDS,
        MIN_ADMIN_SESSION_TTL_SECONDS,
        resolve_admin_session_ttl_seconds,
    )

    if os.environ.get(ADMIN_SESSION_TTL_ENV, "").strip():
        try:
            resolve_admin_session_ttl_seconds()
        except ValueError:
            violations.append(
                f"{ADMIN_SESSION_TTL_ENV} is invalid: must be an integer between "
                f"{MIN_ADMIN_SESSION_TTL_SECONDS} and {MAX_ADMIN_SESSION_TTL_SECONDS} "
                "seconds"
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
    with BusinessConnection.sqlite(initialize_database(Path(db_path))) as conn:
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
