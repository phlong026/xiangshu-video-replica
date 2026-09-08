"""C7 studio drafts: cloud persistence for the V1.4 copy workshop.

The copy workshop previously kept the working draft and the saved-script
list in React memory only — a refresh destroyed both (toast even warned the
user to copy text out by hand). These tests lock the cloud draft contract:

- one draft row per (user, workspace kind), last-write-wins upsert;
- drafts are private to their owner, including from other employees;
- the saved-script list is user-scoped and upserts by script id;
- auditors never write; payloads are size-capped JSON blobs.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.auth import get_database
from app.db import connect_database, initialize_database
from app.db_portable import BusinessConnection
from app.main import app


@pytest.fixture()
def db_path(tmp_path: Path) -> Iterator[Path]:
    db_path = tmp_path / "studio-drafts.db"
    with initialize_database(db_path) as conn:
        seed_data(conn)
    yield db_path


@pytest.fixture()
def client(
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    # Migrated routes (BusinessDb.write) open their own SQLite connection from
    # the env path; it must point at the same database the override yields.
    monkeypatch.setenv("VIDEO_REPLICA_DB_PATH", str(db_path))

    def database_override() -> Iterator[BusinessConnection]:
        conn = BusinessConnection.sqlite(connect_database(db_path))
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
            ("customer_1", "customer_1", "Customer One", "customer"),
            ("auditor_1", "auditor_1", "Auditor One", "auditor"),
        ],
    )


def auth_headers(user_id: str) -> dict[str, str]:
    return {"X-Dev-User-Id": user_id}


DRAFT_URL = "/api/studio/drafts/copy"


def draft_body(**overrides: object) -> dict[str, object]:
    payload = {
        "script": {
            "id": "script-1",
            "title": "测试文案",
            "original": "原文内容",
            "text": "二创文案内容",
            "version": 1,
            "confirmed": True,
        },
        "ipId": "person-1",
        "sourceId": "project-1",
    }
    body: dict[str, object] = {"payload": payload, "script_confirmed": True}
    body.update(overrides)
    return body


def test_put_then_get_draft_roundtrip(client: TestClient) -> None:
    response = client.put(DRAFT_URL, json=draft_body(), headers=auth_headers("employee_1"))
    assert response.status_code == 200
    created = response.json()
    assert created["revision"] == 1
    assert created["script_confirmed"] is True
    assert created["draft_kind"] == "copy"

    fetched = client.get(DRAFT_URL, headers=auth_headers("employee_1"))
    assert fetched.status_code == 200
    assert fetched.json()["payload"]["script"]["text"] == "二创文案内容"
    assert fetched.json()["script_confirmed"] is True


def test_get_missing_draft_returns_404(client: TestClient) -> None:
    response = client.get(DRAFT_URL, headers=auth_headers("employee_1"))
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "STUDIO_DRAFT_NOT_FOUND"


def test_put_upsert_bumps_revision(client: TestClient) -> None:
    client.put(DRAFT_URL, json=draft_body(), headers=auth_headers("employee_1"))
    updated = client.put(
        DRAFT_URL,
        json=draft_body(script_confirmed=False),
        headers=auth_headers("employee_1"),
    )
    assert updated.status_code == 200
    assert updated.json()["revision"] == 2
    assert updated.json()["script_confirmed"] is False


def test_draft_isolated_per_user(client: TestClient) -> None:
    client.put(DRAFT_URL, json=draft_body(), headers=auth_headers("employee_1"))

    other = client.get(DRAFT_URL, headers=auth_headers("employee_2"))
    assert other.status_code == 404

    own = client.put(
        DRAFT_URL,
        json=draft_body(),
        headers=auth_headers("employee_2"),
    )
    assert own.status_code == 200
    assert own.json()["revision"] == 1

    still_one = client.get(DRAFT_URL, headers=auth_headers("employee_1"))
    assert still_one.json()["revision"] == 1


def test_delete_draft_then_get_404(client: TestClient) -> None:
    client.put(DRAFT_URL, json=draft_body(), headers=auth_headers("employee_1"))
    deleted = client.delete(DRAFT_URL, headers=auth_headers("employee_1"))
    assert deleted.status_code == 200
    assert client.get(DRAFT_URL, headers=auth_headers("employee_1")).status_code == 404

    missing = client.delete(DRAFT_URL, headers=auth_headers("employee_1"))
    assert missing.status_code == 404


def test_unknown_draft_kind_rejected(client: TestClient) -> None:
    response = client.put(
        "/api/studio/drafts/banana",
        json=draft_body(),
        headers=auth_headers("employee_1"),
    )
    assert response.status_code == 422


def test_oversized_draft_payload_rejected(client: TestClient) -> None:
    oversized = "字" * 600_000
    response = client.put(
        DRAFT_URL,
        json=draft_body(payload={"text": oversized}),
        headers=auth_headers("employee_1"),
    )
    assert response.status_code == 413
    assert response.json()["detail"]["code"] == "STUDIO_DRAFT_PAYLOAD_TOO_LARGE"


def test_auditor_cannot_write_draft(client: TestClient) -> None:
    headers = auth_headers("auditor_1")
    draft = client.put(DRAFT_URL, json=draft_body(), headers=headers)
    script = client.post(SAVED_URL, json=saved_script_body(), headers=headers)

    assert draft.status_code == 403
    assert script.status_code == 403
    assert client.get(DRAFT_URL, headers=headers).status_code == 404
    assert client.get(SAVED_URL, headers=headers).json()["items"] == []


def test_auditor_cannot_delete_cloud_writes_without_side_effects(
    client: TestClient,
    db_path: Path,
) -> None:
    with BusinessConnection.sqlite(connect_database(db_path)) as conn:
        conn.execute(
            "INSERT INTO studio_drafts (id, user_id, draft_kind, payload) "
            "VALUES ('auditor-draft', 'auditor_1', 'copy', '{}')"
        )
        conn.execute(
            "INSERT INTO studio_saved_scripts ("
            "id, user_id, script_id, title, text, version"
            ") VALUES ('auditor-script-row', 'auditor_1', 'auditor-script', '标题', '正文', 1)"
        )
        conn.commit()

    draft_delete = client.delete(DRAFT_URL, headers=auth_headers("auditor_1"))
    script_delete = client.delete(f"{SAVED_URL}/auditor-script", headers=auth_headers("auditor_1"))

    assert draft_delete.status_code == 403
    assert script_delete.status_code == 403
    with BusinessConnection.sqlite(connect_database(db_path)) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM studio_drafts WHERE id = 'auditor-draft'"
            ).fetchone()[0]
            == 1
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM studio_saved_scripts WHERE id = 'auditor-script-row'"
            ).fetchone()[0]
            == 1
        )


def test_draft_requires_authentication(client: TestClient) -> None:
    assert client.get(DRAFT_URL).status_code == 401
    assert client.put(DRAFT_URL, json=draft_body()).status_code == 401


SAVED_URL = "/api/studio/saved-scripts"


def saved_script_body(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "script_id": "script-1",
        "title": "乡墅口播第一期",
        "text": "大家好，今天带大家看一套…",
        "original": "原文",
        "version": 2,
        "source_kind": "upload",
    }
    body.update(overrides)
    return body


def test_saved_scripts_crud_and_upsert(client: TestClient) -> None:
    created = client.post(SAVED_URL, json=saved_script_body(), headers=auth_headers("employee_1"))
    assert created.status_code == 200
    assert created.json()["script_id"] == "script-1"

    upsert = client.post(
        SAVED_URL,
        json=saved_script_body(text="更新后的文案", version=3),
        headers=auth_headers("employee_1"),
    )
    assert upsert.status_code == 200
    assert upsert.json()["text"] == "更新后的文案"

    listed = client.get(SAVED_URL, headers=auth_headers("employee_1"))
    assert listed.status_code == 200
    items = listed.json()["items"]
    assert len(items) == 1
    assert items[0]["version"] == 3

    deleted = client.delete(f"{SAVED_URL}/script-1", headers=auth_headers("employee_1"))
    assert deleted.status_code == 200
    empty = client.get(SAVED_URL, headers=auth_headers("employee_1"))
    assert empty.json()["items"] == []

    missing = client.delete(f"{SAVED_URL}/script-1", headers=auth_headers("employee_1"))
    assert missing.status_code == 404


def test_saved_script_update_moves_it_to_the_front(client: TestClient) -> None:
    headers = auth_headers("employee_1")
    assert (
        client.post(
            SAVED_URL,
            json=saved_script_body(script_id="older"),
            headers=headers,
        ).status_code
        == 200
    )
    assert (
        client.post(
            SAVED_URL,
            json=saved_script_body(script_id="newer"),
            headers=headers,
        ).status_code
        == 200
    )

    updated = client.post(
        SAVED_URL,
        json=saved_script_body(script_id="older", text="刚更新的文案"),
        headers=headers,
    )
    assert updated.status_code == 200

    listed = client.get(SAVED_URL, headers=headers)
    assert [item["script_id"] for item in listed.json()["items"][:2]] == [
        "older",
        "newer",
    ]


def test_customer_can_write_and_delete_cloud_draft_and_saved_script(
    client: TestClient,
) -> None:
    headers = auth_headers("customer_1")

    draft = client.put(DRAFT_URL, json=draft_body(), headers=headers)
    script = client.post(SAVED_URL, json=saved_script_body(), headers=headers)

    assert draft.status_code == 200
    assert script.status_code == 200
    assert client.delete(DRAFT_URL, headers=headers).status_code == 200
    assert client.delete(f"{SAVED_URL}/script-1", headers=headers).status_code == 200


def test_saved_scripts_isolated_per_user(client: TestClient) -> None:
    client.post(SAVED_URL, json=saved_script_body(), headers=auth_headers("employee_1"))

    other_list = client.get(SAVED_URL, headers=auth_headers("employee_2"))
    assert other_list.json()["items"] == []

    other_delete = client.delete(f"{SAVED_URL}/script-1", headers=auth_headers("employee_2"))
    assert other_delete.status_code == 404

    still_there = client.get(SAVED_URL, headers=auth_headers("employee_1"))
    assert len(still_there.json()["items"]) == 1


def test_saved_script_requires_text(client: TestClient) -> None:
    response = client.post(
        SAVED_URL,
        json=saved_script_body(text="   "),
        headers=auth_headers("employee_1"),
    )
    assert response.status_code == 422


def test_saved_script_list_capped_at_fifty(client: TestClient) -> None:
    for index in range(55):
        response = client.post(
            SAVED_URL,
            json=saved_script_body(script_id=f"script-{index}"),
            headers=auth_headers("employee_1"),
        )
        assert response.status_code == 200
    listed = client.get(SAVED_URL, headers=auth_headers("employee_1"))
    items = listed.json()["items"]
    assert len(items) == 50
    # Newest first: the oldest saved scripts fall off the list.
    assert items[0]["script_id"] == "script-54"


def test_draft_payload_stored_as_json_text(db_path: Path, client: TestClient) -> None:
    """Dual-dialect storage: payload is TEXT JSON, readable on both engines."""
    client.put(DRAFT_URL, json=draft_body(), headers=auth_headers("employee_1"))
    with BusinessConnection.sqlite(connect_database(db_path)) as conn:
        row = conn.execute(
            "SELECT payload FROM studio_drafts WHERE user_id = %s", ("employee_1",)
        ).fetchone()
    assert row is not None
    assert json.loads(str(row["payload"]))["ipId"] == "person-1"
