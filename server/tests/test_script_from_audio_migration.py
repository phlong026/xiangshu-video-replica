from pathlib import Path

from alembic import command

from app.db import alembic_config, connect_database


def test_audio_migration_preserves_duplicate_tasks_and_rolls_back_schema(tmp_path: Path) -> None:
    path = tmp_path / "audio-migration.db"
    config = alembic_config(path)
    command.upgrade(config, "076_studio_notification_preferences")
    with connect_database(path) as conn:
        conn.execute("INSERT INTO users (id,username,display_name) VALUES ('u','u','U')")
        conn.execute("INSERT INTO projects (id,owner_user_id,name) VALUES ('p','u','P')")
        conn.executemany(
            "INSERT INTO script_from_audio_tasks "
            "(id,project_id,source_asset_id,created_by_user_id,idempotency_key,request_hash,"
            "request_json,status,provider_started_at) VALUES (?,'p','a','u',?,'h','{}',?,?)",
            [
                ("submitted", "first", "RUNNING", "2026-01-01 00:00:00"),
                ("duplicate", "second", "PENDING", None),
            ],
        )
    command.upgrade(config, "077_durable_script_from_audio")
    with connect_database(path) as conn:
        states = dict(conn.execute("SELECT id,status FROM script_from_audio_tasks").fetchall())
        assert states == {"submitted": "RUNNING", "duplicate": "SUBMISSION_UNCERTAIN"}
        indexes = {row[1] for row in conn.execute("PRAGMA index_list(script_from_audio_tasks)")}
        assert "uq_script_from_audio_tasks_active_project" in indexes
        columns = {row[1] for row in conn.execute("PRAGMA table_info(script_from_audio_tasks)")}
        assert {"provider_task_id", "next_attempt_at"}.issubset(columns)
    command.downgrade(config, "076_studio_notification_preferences")
    with connect_database(path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(script_from_audio_tasks)")}
        assert "provider_task_id" not in columns
        assert (
            dict(conn.execute("SELECT id,status FROM script_from_audio_tasks").fetchall()) == states
        )
    command.upgrade(config, "077_durable_script_from_audio")
