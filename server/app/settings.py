from __future__ import annotations

import json
import os
from functools import lru_cache
from typing import Any, Literal

from cryptography.fernet import Fernet, InvalidToken

from app.db_portable import BusinessConnection
from app.local_settings_key import LocalSettingsKeyStoreError, load_or_create_local_settings_key
from app.zpay import parse_enabled_channels

ProviderName = Literal["apilio", "metaso", "cos", "deepseek"]

SETTINGS_KEY_ENV = "VIDEO_REPLICA_SETTINGS_KEY"
LOCAL_KEYSTORE_DISABLED_ENV = "VIDEO_REPLICA_DISABLE_LOCAL_KEYSTORE"
ACCEPTANCE_PAYMENT_USER_ID_ENV = "VIDEO_REPLICA_ACCEPTANCE_PAYMENT_USER_ID"
ACCEPTANCE_PAYMENT_AMOUNT_FEN_ENV = "VIDEO_REPLICA_ACCEPTANCE_PAYMENT_AMOUNT_FEN"
MAX_ACCEPTANCE_PAYMENT_FEN = 500
SECRET_FIELDS = (
    "api_key",
    "access_key_id",
    "secret",
    "token",
    "password",
    "authorization",
)
REQUIRED_PROVIDER_FIELDS: dict[ProviderName, tuple[str, ...]] = {
    # Apilio can use a dedicated Gemini key while image generation is not configured yet.
    "apilio": (),
    "metaso": ("api_key",),
    "cos": ("access_key_id", "secret_access_key", "bucket", "region"),
    # 二创口播稿改写默认走 DeepSeek；除 API Key 外的参数（base_url/model）
    # 由服务端固定，界面无需暴露。
    "deepseek": ("api_key",),
}
DEFAULT_RUNTIME_SETTINGS: dict[str, int | str] = {
    "max_generation_count_per_batch": 4,
    "max_concurrent_h3_tasks": 2,
    "active_storage_provider": "cos",
}
DEFAULT_BILLING_SETTINGS: dict[str, int] = {
    "internal_base_unit_price_fen": 1000,
    "charged_unit_price_fen": 1000,
    "min_recharge_fen": 10000,
    "recharge_step_fen": 1000,
}


class SettingsUnavailableError(RuntimeError):
    pass


class SettingsKeyMissing(SettingsUnavailableError):
    pass


class SettingsKeyInvalid(SettingsUnavailableError):
    pass


class SettingsDecryptError(SettingsUnavailableError):
    pass


class SettingsRepository:
    def __init__(self, conn: BusinessConnection, fernet: Fernet | None = None) -> None:
        self.conn = conn
        self.fernet = fernet or fernet_from_environment()

    def save_provider_config(
        self,
        provider: str,
        config: dict[str, Any],
        *,
        actor_user_id: str,
    ) -> dict[str, Any]:
        provider_name = normalize_provider(provider)
        normalized = normalize_config(config)
        validate_provider_config(provider_name, normalized)
        return self._save_encrypted_config(
            provider_name,
            normalized,
            actor_user_id=actor_user_id,
        )

    def save_zpay_config(
        self,
        config: dict[str, Any],
        *,
        actor_user_id: str,
    ) -> dict[str, Any]:
        normalized = normalize_config(config)
        validate_zpay_config(normalized)
        normalized["enabled_channels"] = ",".join(
            parse_enabled_channels(normalized["enabled_channels"])
        )
        return self._save_encrypted_config(
            "zpay",
            normalized,
            actor_user_id=actor_user_id,
        )

    def _save_encrypted_config(
        self,
        provider: str,
        normalized: dict[str, str],
        *,
        actor_user_id: str,
    ) -> dict[str, Any]:
        encrypted_config = self.fernet.encrypt(
            json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).decode("ascii")

        with self.conn:
            self.conn.execute(
                """
                INSERT INTO provider_settings (
                    provider,
                    encrypted_config,
                    updated_by_user_id,
                    created_at,
                    updated_at
                )
                VALUES (%s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT(provider) DO UPDATE SET
                    encrypted_config = excluded.encrypted_config,
                    updated_by_user_id = excluded.updated_by_user_id,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (provider, encrypted_config, actor_user_id),
            )

        return self._read_encrypted_config(provider)

    def read_provider_config(self, provider: str) -> dict[str, Any]:
        provider_name = normalize_provider(provider)
        return self._read_encrypted_config(provider_name)

    def read_zpay_config(self) -> dict[str, Any]:
        return self._read_encrypted_config("zpay")

    def _read_encrypted_config(self, provider: str) -> dict[str, Any]:
        config = self._load_encrypted_config(provider)
        return {
            "provider": provider,
            "configured": bool(config),
            "config": mask_config(config),
        }

    def read_all_provider_configs(self) -> dict[str, dict[str, Any]]:
        return {
            provider: self.read_provider_config(provider) for provider in REQUIRED_PROVIDER_FIELDS
        }

    def load_provider_config(self, provider: str) -> dict[str, str]:
        provider_name = normalize_provider(provider)
        return self._load_encrypted_config(provider_name)

    def load_zpay_config(self) -> dict[str, str]:
        return self._load_encrypted_config("zpay")

    def _load_encrypted_config(self, provider: str) -> dict[str, str]:
        row = self.conn.execute(
            "SELECT encrypted_config FROM provider_settings WHERE provider = %s",
            (provider,),
        ).fetchone()
        if row is None:
            return {}

        try:
            raw = self.fernet.decrypt(str(row["encrypted_config"]).encode("ascii"))
        except InvalidToken as exc:
            raise SettingsDecryptError("provider settings cannot be decrypted") from exc

        decoded = json.loads(raw.decode("utf-8"))
        if not isinstance(decoded, dict):
            raise SettingsDecryptError("provider settings payload is invalid")
        return {str(key): str(value) for key, value in decoded.items()}

    def save_runtime_settings(
        self,
        *,
        max_generation_count_per_batch: int,
        max_concurrent_h3_tasks: int,
        active_storage_provider: str,
        actor_user_id: str,
        fair_queue_enabled: bool | None = None,
    ) -> dict[str, int | str]:
        validate_runtime_settings(
            max_generation_count_per_batch=max_generation_count_per_batch,
            max_concurrent_h3_tasks=max_concurrent_h3_tasks,
            active_storage_provider=active_storage_provider,
        )
        # M4/M5 review M2: the fair-queue rollout switch (revised ADR §4)
        # gets an audited write path instead of ad-hoc SQL. The column only
        # exists on PostgreSQL (migration 041); the desktop SQLite lane keeps
        # its legacy global FIFO, so a provided value there is a caller error.
        # ``getattr`` rather than ``is_postgres`` because unit tests inject a
        # raw sqlite3 connection, which is by definition not the PG lane.
        # The saved dict deliberately does NOT echo the switch (PR #68 Codex
        # P2): echoing it would let a stale SettingsPanel round-trip silently
        # disable the queue on an unrelated limits save. The switch is read
        # explicitly via read_fair_queue_enabled / the control-plane route.
        if fair_queue_enabled is not None and not getattr(self.conn, "is_postgres", False):
            raise ValueError("fair_queue_enabled is only available on the PostgreSQL lane")
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO runtime_settings (
                    id,
                    max_generation_count_per_batch,
                    max_concurrent_h3_tasks,
                    active_storage_provider,
                    updated_by_user_id,
                    created_at,
                    updated_at
                )
                VALUES (1, %s, %s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT(id) DO UPDATE SET
                    max_generation_count_per_batch = excluded.max_generation_count_per_batch,
                    max_concurrent_h3_tasks = excluded.max_concurrent_h3_tasks,
                    active_storage_provider = excluded.active_storage_provider,
                    updated_by_user_id = excluded.updated_by_user_id,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    max_generation_count_per_batch,
                    max_concurrent_h3_tasks,
                    active_storage_provider,
                    actor_user_id,
                ),
            )
            if fair_queue_enabled is not None:
                # The upsert above guarantees the id=1 row; flip the switch in
                # the same transaction so limits and the queue mode commit
                # (or roll back) together.
                self.conn.execute(
                    """
                    UPDATE runtime_settings
                    SET fair_queue_enabled = %s, updated_at = CURRENT_TIMESTAMP
                    WHERE id = 1
                    """,
                    (fair_queue_enabled,),
                )
        return self.read_runtime_settings()

    def read_fair_queue_enabled(self) -> bool:
        """The fair-queue switch on the PostgreSQL lane; anything else (the
        SQLite lane, a missing settings row) is the documented feature-off
        state (revised ADR §4, mirroring ``_fair_queue_enabled``)."""
        if not getattr(self.conn, "is_postgres", False):
            return False
        row = self.conn.execute(
            "SELECT fair_queue_enabled FROM runtime_settings WHERE id = 1"
        ).fetchone()
        return bool(row[0]) if row is not None else False

    def read_runtime_settings(self) -> dict[str, int | str]:
        row = self.conn.execute(
            """
            SELECT max_generation_count_per_batch, max_concurrent_h3_tasks, active_storage_provider
            FROM runtime_settings
            WHERE id = 1
            """
        ).fetchone()
        if row is None:
            return dict(DEFAULT_RUNTIME_SETTINGS)
        return {
            "max_generation_count_per_batch": int(row["max_generation_count_per_batch"]),
            "max_concurrent_h3_tasks": int(row["max_concurrent_h3_tasks"]),
            "active_storage_provider": str(row["active_storage_provider"]),
        }

    def save_billing_settings(
        self,
        *,
        internal_base_unit_price_fen: int,
        min_recharge_fen: int,
        recharge_step_fen: int,
        actor_user_id: str | None,
    ) -> dict[str, int]:
        validate_billing_settings(
            internal_base_unit_price_fen=internal_base_unit_price_fen,
            min_recharge_fen=min_recharge_fen,
            recharge_step_fen=recharge_step_fen,
        )
        with self.conn:
            self.conn.execute(
                """
                UPDATE runtime_settings
                SET internal_base_unit_price_fen = %s,
                    min_recharge_fen = %s,
                    recharge_step_fen = %s,
                    updated_by_user_id = %s,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = 1
                """,
                (
                    internal_base_unit_price_fen,
                    min_recharge_fen,
                    recharge_step_fen,
                    actor_user_id,
                ),
            )
        return self.read_billing_settings()

    def read_billing_settings(self) -> dict[str, int]:
        row = self.conn.execute(
            """
            SELECT internal_base_unit_price_fen, min_recharge_fen, recharge_step_fen
            FROM runtime_settings
            WHERE id = 1
            """
        ).fetchone()
        if row is None:
            return dict(DEFAULT_BILLING_SETTINGS)
        base_price = int(row["internal_base_unit_price_fen"])
        return {
            "internal_base_unit_price_fen": base_price,
            "charged_unit_price_fen": base_price,
            "min_recharge_fen": int(row["min_recharge_fen"]),
            "recharge_step_fen": int(row["recharge_step_fen"]),
        }

    def read_customer_billing_settings(self, *, user_id: str) -> dict[str, int]:
        """Return billing settings with an optional per-customer sale price.

        Customized customers recharge in whole-video increments.  The global
        minimum remains the lower bound and is rounded up to the next whole
        video at that customer's price.
        """
        billing = self.read_billing_settings()
        row = self.conn.execute(
            "SELECT unit_price_fen FROM customer_unit_prices WHERE user_id = %s",
            (user_id,),
        ).fetchone()
        if row is None:
            return billing
        return apply_customer_unit_price(billing, unit_price_fen=int(row["unit_price_fen"]))


def fernet_from_environment() -> Fernet:
    return Fernet(settings_encryption_key().encode("ascii"))


def settings_encryption_key() -> str:
    key = os.environ.get(SETTINGS_KEY_ENV)
    if not key:
        if os.environ.get(LOCAL_KEYSTORE_DISABLED_ENV) == "1":
            raise SettingsKeyMissing(f"{SETTINGS_KEY_ENV} is required")
        try:
            key = _local_settings_key()
        except LocalSettingsKeyStoreError as exc:
            raise SettingsKeyMissing(
                f"{SETTINGS_KEY_ENV} or an operating-system key store is required"
            ) from exc
    try:
        Fernet(key.encode("ascii"))
    except (UnicodeEncodeError, ValueError) as exc:
        raise SettingsKeyInvalid("settings encryption key is invalid") from exc
    return key


@lru_cache(maxsize=1)
def _local_settings_key() -> str:
    return load_or_create_local_settings_key()


def clear_local_settings_key_cache() -> None:
    _local_settings_key.cache_clear()


def normalize_provider(provider: str) -> ProviderName:
    normalized = provider.lower()
    if normalized not in REQUIRED_PROVIDER_FIELDS:
        raise ValueError(f"unsupported provider: {provider}")
    return normalized


def normalize_config(config: dict[str, Any]) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for key, value in config.items():
        if value is None:
            continue
        normalized[str(key)] = str(value).strip()
    return normalized


def validate_provider_config(provider: ProviderName, config: dict[str, str]) -> None:
    missing = [field for field in REQUIRED_PROVIDER_FIELDS[provider] if not config.get(field)]
    if missing:
        raise ValueError(f"missing required setting: {', '.join(missing)}")


def validate_zpay_config(config: dict[str, str]) -> None:
    allowed_fields = {"pid", "key", "enabled_channels"}
    unexpected = sorted(set(config) - allowed_fields)
    if unexpected:
        raise ValueError(f"unsupported ZPay setting: {', '.join(unexpected)}")
    missing = [field for field in allowed_fields if not config.get(field)]
    if missing:
        raise ValueError(f"missing required ZPay setting: {', '.join(sorted(missing))}")
    parse_enabled_channels(config["enabled_channels"])


def validate_runtime_settings(
    *,
    max_generation_count_per_batch: int,
    max_concurrent_h3_tasks: int,
    active_storage_provider: str,
) -> None:
    if max_generation_count_per_batch < 1:
        raise ValueError("max_generation_count_per_batch must be at least 1")
    if max_concurrent_h3_tasks < 1:
        raise ValueError("max_concurrent_h3_tasks must be at least 1")
    if active_storage_provider not in {"cos", "local"}:
        raise ValueError("active_storage_provider must be cos or local")


def validate_billing_settings(
    *,
    internal_base_unit_price_fen: int,
    min_recharge_fen: int,
    recharge_step_fen: int,
) -> None:
    values = (
        internal_base_unit_price_fen,
        min_recharge_fen,
        recharge_step_fen,
    )
    if any(isinstance(value, bool) or not isinstance(value, int) for value in values):
        raise ValueError("billing settings must use integer fen values")
    if internal_base_unit_price_fen <= 0:
        raise ValueError("internal_base_unit_price_fen must be positive")
    if min_recharge_fen < 10000:
        raise ValueError("min_recharge_fen must be at least 10000")
    if recharge_step_fen < 1000:
        raise ValueError("recharge_step_fen must be at least 1000")
    if min_recharge_fen % recharge_step_fen != 0:
        raise ValueError("min_recharge_fen must be divisible by recharge_step_fen")
    if recharge_step_fen % internal_base_unit_price_fen != 0:
        raise ValueError("recharge_step_fen must be divisible by internal_base_unit_price_fen")


def apply_customer_unit_price(
    billing: dict[str, int],
    *,
    unit_price_fen: int,
) -> dict[str, int]:
    """Apply a positive per-customer sale price to a global billing snapshot."""
    if isinstance(unit_price_fen, bool) or not isinstance(unit_price_fen, int):
        raise ValueError("unit_price_fen must be an integer fen value")
    if unit_price_fen < 1 or unit_price_fen > 2_147_483_647:
        raise ValueError("unit_price_fen must be between 1 and 2147483647")
    global_minimum = billing["min_recharge_fen"]
    effective_minimum = ((global_minimum + unit_price_fen - 1) // unit_price_fen) * unit_price_fen
    return {
        **billing,
        "charged_unit_price_fen": unit_price_fen,
        "min_recharge_fen": effective_minimum,
        "recharge_step_fen": unit_price_fen,
    }


def effective_customer_billing_settings(
    billing: dict[str, int],
    *,
    user_id: str,
) -> dict[str, int]:
    """Return the normal billing snapshot or a tightly scoped real-chain rehearsal.

    The deployment-only override never changes stored global pricing.  It is
    enabled only when both environment variables are present, only for the
    exact customer id, and can never raise the real payment above five yuan.
    """
    configured_user_id = os.environ.get(ACCEPTANCE_PAYMENT_USER_ID_ENV, "").strip()
    raw_amount = os.environ.get(ACCEPTANCE_PAYMENT_AMOUNT_FEN_ENV, "").strip()
    if not configured_user_id and not raw_amount:
        return dict(billing)
    if not configured_user_id or not raw_amount:
        raise ValueError("acceptance payment requires both user id and amount")
    try:
        amount_fen = int(raw_amount)
    except ValueError as exc:
        raise ValueError("acceptance payment amount must be integer fen") from exc
    if amount_fen < 1 or amount_fen > MAX_ACCEPTANCE_PAYMENT_FEN:
        raise ValueError(
            f"acceptance payment amount must be between 1 and {MAX_ACCEPTANCE_PAYMENT_FEN} fen"
        )
    if user_id != configured_user_id:
        return dict(billing)
    charged_unit_price_fen = billing["charged_unit_price_fen"]
    if amount_fen % charged_unit_price_fen != 0:
        raise ValueError("acceptance payment amount must be divisible by the configured unit price")
    return {
        **billing,
        "min_recharge_fen": amount_fen,
        "recharge_step_fen": amount_fen,
    }


def mask_config(config: dict[str, str]) -> dict[str, str]:
    return {
        key: mask_secret(value) if is_secret_field(key) else value for key, value in config.items()
    }


def is_secret_field(key: str) -> bool:
    lowered = key.lower()
    return lowered == "key" or any(marker in lowered for marker in SECRET_FIELDS)


def mask_secret(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 4:
        return "********"
    return f"********{value[-4:]}"
