from __future__ import annotations

import json
import os
import sqlite3
from functools import lru_cache
from typing import Any, Literal

from cryptography.fernet import Fernet, InvalidToken

from app.local_settings_key import LocalSettingsKeyStoreError, load_or_create_local_settings_key

ProviderName = Literal["apilio", "metaso", "cos"]

SETTINGS_KEY_ENV = "VIDEO_REPLICA_SETTINGS_KEY"
LOCAL_KEYSTORE_DISABLED_ENV = "VIDEO_REPLICA_DISABLE_LOCAL_KEYSTORE"
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
}
DEFAULT_RUNTIME_SETTINGS: dict[str, int | str] = {
    "max_generation_count_per_batch": 4,
    "max_concurrent_h3_tasks": 2,
    "active_storage_provider": "cos",
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
    def __init__(self, conn: sqlite3.Connection, fernet: Fernet | None = None) -> None:
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
                VALUES (?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT(provider) DO UPDATE SET
                    encrypted_config = excluded.encrypted_config,
                    updated_by_user_id = excluded.updated_by_user_id,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (provider_name, encrypted_config, actor_user_id),
            )

        return self.read_provider_config(provider_name)

    def read_provider_config(self, provider: str) -> dict[str, Any]:
        provider_name = normalize_provider(provider)
        config = self.load_provider_config(provider_name)
        return {
            "provider": provider_name,
            "configured": bool(config),
            "config": mask_config(config),
        }

    def read_all_provider_configs(self) -> dict[str, dict[str, Any]]:
        return {
            provider: self.read_provider_config(provider) for provider in REQUIRED_PROVIDER_FIELDS
        }

    def load_provider_config(self, provider: str) -> dict[str, str]:
        provider_name = normalize_provider(provider)
        row = self.conn.execute(
            "SELECT encrypted_config FROM provider_settings WHERE provider = ?",
            (provider_name,),
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
    ) -> dict[str, int | str]:
        validate_runtime_settings(
            max_generation_count_per_batch=max_generation_count_per_batch,
            max_concurrent_h3_tasks=max_concurrent_h3_tasks,
            active_storage_provider=active_storage_provider,
        )
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
                VALUES (1, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
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
        return self.read_runtime_settings()

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


def mask_config(config: dict[str, str]) -> dict[str, str]:
    return {
        key: mask_secret(value) if is_secret_field(key) else value for key, value in config.items()
    }


def is_secret_field(key: str) -> bool:
    lowered = key.lower()
    return any(marker in lowered for marker in SECRET_FIELDS)


def mask_secret(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 4:
        return "********"
    return f"********{value[-4:]}"
