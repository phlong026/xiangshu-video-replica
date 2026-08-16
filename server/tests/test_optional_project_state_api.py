from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.auth import get_database
from app.db import connect_database, initialize_database
from app.main import app


@pytest.fixture()
def client(tmp_path: Path) -> Iterator[TestClient]:
    database_path = tmp_path / "optional-project-state.db"
    with initialize_database(database_path) as conn:
        conn.executemany(
            """
            INSERT INTO users (id, username, display_name, role)
            VALUES (?, ?, ?, ?)
            """,
            (
                ("employee_1", "employee_1", "Employee One", "employee"),
                ("employee_2", "employee_2", "Employee Two", "employee"),
            ),
        )
        conn.executemany(
            """
            INSERT INTO projects (id, owner_user_id, name)
            VALUES (?, ?, ?)
            """,
            (
                ("project_empty", "employee_1", "Empty Project"),
                ("project_other", "employee_2", "Other Project"),
            ),
        )

    def database_override() -> Iterator[sqlite3.Connection]:
        conn = connect_database(database_path)
        try:
            yield conn
        finally:
            conn.close()

    app.dependency_overrides[get_database] = database_override
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def test_empty_optional_project_state_returns_successful_nulls(client: TestClient) -> None:
    paths = (
        "shot-cards/latest",
        "main-character",
        "source-frames/latest",
        "source-frames/selection/latest",
        "character-reference-selection/latest",
        "first-frames/latest",
        "first-frames/selection/latest",
    )

    for path in paths:
        response = client.get(
            f"/api/projects/project_empty/{path}",
            headers={"X-Dev-User-Id": "employee_1"},
        )

        assert response.status_code == 200, path
        assert response.json() is None, path


@pytest.mark.parametrize(
    ("project_id", "expected_status"),
    (("project_missing", 404), ("project_other", 403)),
)
def test_optional_project_state_preserves_access_failures(
    client: TestClient,
    project_id: str,
    expected_status: int,
) -> None:
    paths = (
        "shot-cards/latest",
        "main-character",
        "source-frames/latest",
        "source-frames/selection/latest",
        "character-reference-selection/latest",
        "first-frames/latest",
        "first-frames/selection/latest",
    )

    for path in paths:
        response = client.get(
            f"/api/projects/{project_id}/{path}",
            headers={"X-Dev-User-Id": "employee_1"},
        )

        assert response.status_code == expected_status, path
