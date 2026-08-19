from __future__ import annotations

import pytest

from app.settings import clear_local_settings_key_cache


@pytest.fixture(autouse=True)
def enable_dev_identity_header(monkeypatch: pytest.MonkeyPatch) -> None:
    clear_local_settings_key_cache()
    monkeypatch.setenv("VIDEO_REPLICA_AUTH_MODE", "development")
    monkeypatch.setenv("VIDEO_REPLICA_ALLOW_DEV_IDENTITY_HEADER", "1")
    # Unit tests must never read or create credentials in the developer's real
    # macOS Keychain / Windows DPAPI store. Tests that cover the local store opt
    # in explicitly with an injected command runner.
    monkeypatch.setenv("VIDEO_REPLICA_DISABLE_LOCAL_KEYSTORE", "1")
    monkeypatch.delenv("VIDEO_REPLICA_DESKTOP_USER_ID", raising=False)
