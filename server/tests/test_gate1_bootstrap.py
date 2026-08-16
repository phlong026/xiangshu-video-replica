from __future__ import annotations

from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from app.db import connect_database
from app.gate1_bootstrap import bootstrap_gate1_database


def test_gate1_bootstrap_creates_only_identity_and_local_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "gate1.sqlite3"
    monkeypatch.setenv("VIDEO_REPLICA_SETTINGS_KEY", Fernet.generate_key().decode("ascii"))

    summary = bootstrap_gate1_database(
        db_path,
        user_id="gate1_admin",
        display_name="Gate 1 Admin",
    )

    with connect_database(db_path) as conn:
        user = conn.execute("SELECT id, role FROM users WHERE id = 'gate1_admin'").fetchone()
        runtime = conn.execute(
            """
            SELECT max_generation_count_per_batch, max_concurrent_h3_tasks,
                   active_storage_provider
            FROM runtime_settings WHERE id = 1
            """
        ).fetchone()
        project_count = conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
        version_count = conn.execute("SELECT COUNT(*) FROM versions").fetchone()[0]
        provider_count = conn.execute("SELECT COUNT(*) FROM provider_settings").fetchone()[0]

    assert summary == {
        "database": str(db_path.resolve()),
        "desktop_user_id": "gate1_admin",
        "active_storage_provider": "local",
    }
    assert user is not None
    assert dict(user) == {"id": "gate1_admin", "role": "admin"}
    assert runtime is not None
    assert dict(runtime) == {
        "max_generation_count_per_batch": 6,
        "max_concurrent_h3_tasks": 2,
        "active_storage_provider": "local",
    }
    assert project_count == 0
    assert version_count == 0
    assert provider_count == 0

    with pytest.raises(FileExistsError):
        bootstrap_gate1_database(
            db_path,
            user_id="second_admin",
            display_name="Second Admin",
        )
