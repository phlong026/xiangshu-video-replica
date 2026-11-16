from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def enable_dev_identity_header(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VIDEO_REPLICA_AUTH_MODE", "development")
    monkeypatch.setenv("VIDEO_REPLICA_ALLOW_DEV_IDENTITY_HEADER", "1")
    # 生产默认停用 TikHub 爆款功能；遗留功能测试需显式启用其测试环境，
    # 专门验证下线行为的用例再覆盖为 "1"。
    monkeypatch.setenv("VIDEO_REPLICA_VIRAL_CLIENT_DISABLED", "0")
    monkeypatch.delenv("VIDEO_REPLICA_DESKTOP_USER_ID", raising=False)
