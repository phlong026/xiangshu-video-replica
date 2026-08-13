from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.analysis import FakeGemini, analyze_video, parse_analysis_response
from app.auth import get_database
from app.db import connect_database, initialize_database
from app.main import app


@pytest.fixture()
def db_path(tmp_path: Path) -> Iterator[Path]:
    db_path = tmp_path / "analysis.db"
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
        """
        INSERT INTO users (id, username, display_name, role)
        VALUES (?, ?, ?, ?)
        """,
        [
            ("employee_1", "employee_1", "Employee One", "employee"),
            ("employee_2", "employee_2", "Employee Two", "employee"),
            ("admin_1", "admin_1", "Admin One", "admin"),
            ("auditor_1", "auditor_1", "Auditor One", "auditor"),
        ],
    )
    conn.executemany(
        """
        INSERT INTO projects (id, owner_user_id, name)
        VALUES (?, ?, ?)
        """,
        [
            ("project_owned", "employee_1", "Owned Project"),
            ("project_other", "employee_2", "Other Project"),
        ],
    )
    conn.executemany(
        """
        INSERT INTO assets (
            id,
            project_id,
            kind,
            storage_uri,
            sha256,
            size_bytes,
            content_type,
            created_by_user_id
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                "asset_owned",
                "project_owned",
                "video",
                "local://owned.mp4",
                "sha-owned",
                12,
                "video/mp4",
                "employee_1",
            ),
            (
                "asset_other",
                "project_other",
                "video",
                "local://other.mp4",
                "sha-other",
                12,
                "video/mp4",
                "employee_2",
            ),
        ],
    )
    conn.commit()


def auth_headers(user_id: str) -> dict[str, str]:
    return {"X-Dev-User-Id": user_id}


def valid_analysis_payload() -> dict[str, object]:
    return {
        "summary": "短视频拆解",
        "duration_seconds": 10,
        "aspect_ratio": "9:16",
        "resolution": "1080x1920",
        "fps": 30,
        "theme": "人物口播",
        "visual_style": "写实",
        "pace": "快",
        "camera_language": "近景轻微推进",
        "original_script": "你好",
        "shots": [
            {
                "shot_id": "S01",
                "start_time": 0,
                "end_time": 5,
                "shot_type": "近景",
                "composition": "人物居中",
                "camera_motion": "轻微推进",
                "subject": "主讲人",
                "action": "看向镜头讲话",
                "scene": "室内",
                "spoken_text": "你好",
                "transition": "硬切",
            },
            {
                "shot_id": "S02",
                "start_time": 5,
                "end_time": 10,
                "shot_type": "中景",
                "composition": "三分法",
                "camera_motion": "固定",
                "subject": "产品",
                "action": "展示产品",
                "scene": "桌面",
                "spoken_text": "再见",
                "transition": "淡出",
            },
        ],
    }


def test_fake_gemini_analysis_repairs_invalid_json_once() -> None:
    provider = FakeGemini(
        analysis_json='{"summary":',
        repair_json=json.dumps(valid_analysis_payload()),
    )

    result = analyze_video(
        video_uri="local://owned.mp4",
        video_duration_seconds=10,
        provider=provider,
    )

    assert provider.repair_calls == 1
    assert result.analysis.summary == "短视频拆解"
    assert result.provider_response_ref["stored_as"] == "versions.payload_json"
    assert result.provider_response_ref["raw"]["text"] == '{"summary":'


def test_analysis_schema_rejects_overlap_and_unknown_fields() -> None:
    payload = valid_analysis_payload()
    payload["unexpected"] = "reject me"

    with pytest.raises(ValidationError):
        parse_analysis_response(json.dumps(payload), duration_seconds=10)

    overlap = valid_analysis_payload()
    shots = overlap["shots"]
    assert isinstance(shots, list)
    shots[1]["start_time"] = 4

    with pytest.raises(ValidationError):
        parse_analysis_response(json.dumps(overlap), duration_seconds=10)


def test_project_owner_can_create_and_read_analysis_version(client: TestClient) -> None:
    response = client.post(
        "/api/projects/project_owned/analysis",
        json={"asset_id": "asset_owned", "duration_seconds": 10},
        headers=auth_headers("employee_1"),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["kind"] == "analysis"
    assert body["version_number"] == 1
    assert body["payload"]["analysis"]["shots"][0]["shot_id"] == "S01"
    assert body["payload"]["provider_response_ref"]["stored_as"] == "versions.payload_json"

    fetched = client.get(f"/api/analysis/{body['id']}", headers=auth_headers("employee_1"))

    assert fetched.status_code == 200
    assert fetched.json()["id"] == body["id"]


def test_analysis_routes_require_existing_rbac(client: TestClient) -> None:
    auditor = client.post(
        "/api/projects/project_owned/analysis",
        json={"asset_id": "asset_owned", "duration_seconds": 10},
        headers=auth_headers("auditor_1"),
    )
    other_owner = client.post(
        "/api/projects/project_owned/analysis",
        json={"asset_id": "asset_owned", "duration_seconds": 10},
        headers=auth_headers("employee_2"),
    )

    assert auditor.status_code == 403
    assert auditor.json()["detail"]["code"] == "ROLE_FORBIDDEN"
    assert other_owner.status_code == 403
    assert other_owner.json()["detail"]["code"] == "PROJECT_FORBIDDEN"


def test_manual_shot_card_version_is_not_overwritten_by_new_analysis(
    client: TestClient,
    db_path: Path,
) -> None:
    first = client.post(
        "/api/projects/project_owned/analysis",
        json={"asset_id": "asset_owned", "duration_seconds": 10},
        headers=auth_headers("employee_1"),
    ).json()

    manual_shots = first["payload"]["analysis"]["shots"]
    manual_shots[0]["action"] = "人工修订后的动作"
    manual = client.put(
        f"/api/analysis/{first['id']}/shots",
        json={"shots": manual_shots},
        headers=auth_headers("employee_1"),
    )

    second = client.post(
        "/api/projects/project_owned/analysis",
        json={"asset_id": "asset_owned", "duration_seconds": 10},
        headers=auth_headers("employee_1"),
    )

    assert manual.status_code == 200
    assert manual.json()["kind"] == "shot_card"
    assert manual.json()["version_number"] == 1
    assert second.status_code == 200
    assert second.json()["version_number"] == 2

    with connect_database(db_path) as conn:
        rows = conn.execute(
            """
            SELECT kind, version_number, payload_json
            FROM versions
            WHERE project_id = ?
            ORDER BY kind, version_number
            """,
            ("project_owned",),
        ).fetchall()

    assert [(row["kind"], row["version_number"]) for row in rows] == [
        ("analysis", 1),
        ("analysis", 2),
        ("shot_card", 1),
    ]
    shot_card_payload = json.loads(rows[2]["payload_json"])
    assert shot_card_payload["shots"][0]["action"] == "人工修订后的动作"
