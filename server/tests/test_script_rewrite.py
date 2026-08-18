from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

import app.script_rewrite as script_rewrite
from app.auth import get_database
from app.db import connect_database, initialize_database
from app.main import app
from app.settings import SETTINGS_KEY_ENV, SettingsRepository


@pytest.fixture()
def db_path(tmp_path: Path) -> Iterator[Path]:
    db_path = tmp_path / "script-rewrite.db"
    with initialize_database(db_path) as conn:
        seed_data(conn)
    yield db_path


@pytest.fixture()
def client(db_path: Path) -> Iterator[TestClient]:
    def database_override() -> Iterator[sqlite3.Connection]:
        conn = connect_database(db_path)
        try:
            yield conn
        finally:
            conn.close()

    app.dependency_overrides[get_database] = database_override
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def seed_data(conn: sqlite3.Connection) -> None:
    conn.executemany(
        "INSERT INTO users (id, username, display_name, role) VALUES (?, ?, ?, ?)",
        [
            ("employee_1", "employee_1", "Employee One", "employee"),
            ("employee_2", "employee_2", "Employee Two", "employee"),
            ("auditor_1", "auditor_1", "Auditor One", "auditor"),
        ],
    )
    conn.execute(
        "INSERT INTO projects (id, owner_user_id, name) VALUES (?, ?, ?)",
        ("project_owned", "employee_1", "Owned Project"),
    )


def auth_headers(user_id: str) -> dict[str, str]:
    return {"X-Dev-User-Id": user_id}


def configure_deepseek(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    key = Fernet.generate_key().decode("ascii")
    monkeypatch.setenv(SETTINGS_KEY_ENV, key)
    with connect_database(db_path) as conn:
        SettingsRepository(conn, fernet=Fernet(key.encode("ascii"))).save_provider_config(
            "deepseek",
            {"api_key": "deepseek-test-key"},
            actor_user_id="employee_1",
        )


def test_script_rewrite_requires_deepseek_configuration(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Keep the repo functional without touching the local OS keystore.
    key = Fernet.generate_key().decode("ascii")
    monkeypatch.setenv(SETTINGS_KEY_ENV, key)

    response = client.post(
        "/api/projects/project_owned/script-rewrite",
        headers=auth_headers("employee_1"),
        json={"text": "原始口播稿内容。"},
    )

    assert response.status_code == 503
    detail = response.json()["detail"]
    assert detail["code"] == "DEEPSEEK_NOT_CONFIGURED"


def test_script_rewrite_rejects_empty_text(
    client: TestClient,
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure_deepseek(db_path, monkeypatch)

    response = client.post(
        "/api/projects/project_owned/script-rewrite",
        headers=auth_headers("employee_1"),
        json={"text": "   "},
    )

    assert response.status_code == 422


def test_script_rewrite_denies_other_projects_and_auditors(
    client: TestClient,
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure_deepseek(db_path, monkeypatch)

    foreign = client.post(
        "/api/projects/project_owned/script-rewrite",
        headers=auth_headers("employee_2"),
        json={"text": "原始口播稿内容。"},
    )
    auditor = client.post(
        "/api/projects/project_owned/script-rewrite",
        headers=auth_headers("auditor_1"),
        json={"text": "原始口播稿内容。"},
    )

    assert foreign.status_code in (403, 404)
    assert auditor.status_code == 403


def test_script_rewrite_returns_rewritten_text(
    client: TestClient,
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure_deepseek(db_path, monkeypatch)
    monkeypatch.setattr(
        script_rewrite,
        "_request_deepseek",
        lambda **kwargs: "这是全新的二创口播稿。",
    )

    response = client.post(
        "/api/projects/project_owned/script-rewrite",
        headers=auth_headers("employee_1"),
        json={"text": "原始口播稿内容，需要被改写。"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["rewritten_text"] == "这是全新的二创口播稿。"
    assert payload["provider"] == "deepseek"
    assert payload["model"] == "deepseek-chat"
