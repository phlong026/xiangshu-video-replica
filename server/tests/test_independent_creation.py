"""Independent video creation domain tests (C2 standalone channel).

SQLite lane: routes drive ``create_independent_batch`` through the real
fenced write path, a scripted FakeH3Provider runs the worker end to end, and
the wallet ledger proves per-second RESERVE/SETTLE/RELEASE. No network, no
real buckets, no provider names in customer-visible payloads.
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
from app.generation_worker import run_worker_once
from app.main import app
from app.storage import FakeStorageAdapter

ENABLED_UPDATE = "UPDATE runtime_settings SET h3_extended_modes_enabled = 1 WHERE id = 1"


@pytest.fixture()
def db_path(tmp_path: Path) -> Iterator[Path]:
    db_path = tmp_path / "independent.db"
    with initialize_database(db_path) as conn:
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
            "INSERT INTO wallets (user_id, available_credits, reserved_credits) VALUES (?, ?, 0)",
            [("employee_1", 1000), ("employee_2", 1000)],
        )
        conn.executemany(
            "INSERT INTO projects (id, owner_user_id, name) VALUES (?, ?, ?)",
            [("project_a", "employee_1", "A"), ("project_b", "employee_2", "B")],
        )
        # 独立创作素材：用户归属、无项目（materials 通道产物形态）。
        conn.execute(
            """
            INSERT INTO assets (
                id, project_id, kind, storage_uri, sha256, size_bytes,
                content_type, created_by_user_id
            ) VALUES (
                'frame-owned', NULL, 'material_image', 'fake://generation-results/frame.png',
                'frame-hash', 9, 'image/png', 'employee_1'
            )
            """
        )
        conn.execute(
            """
            INSERT INTO assets (
                id, project_id, kind, storage_uri, sha256, size_bytes,
                content_type, created_by_user_id
            ) VALUES (
                'frame-other', NULL, 'material_image', 'fake://generation-results/other.png',
                'other-hash', 9, 'image/png', 'employee_2'
            )
            """
        )
        conn.execute(
            """
            INSERT INTO assets (
                id, project_id, kind, storage_uri, sha256, size_bytes,
                content_type, created_by_user_id
            ) VALUES (
                'asset-video', 'project_a', 'reference_video', 'local://assets/ref.mp4',
                'video-hash', 9, 'video/mp4', 'employee_1'
            )
            """
        )
        conn.commit()
    yield db_path


@pytest.fixture()
def client(
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
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


def auth_headers(user_id: str) -> dict[str, str]:
    return {"X-Dev-User-Id": user_id}


def _enable_extended_modes(db_path: Path) -> None:
    raw = sqlite3.connect(db_path)
    try:
        raw.execute(ENABLED_UPDATE)
        raw.commit()
    finally:
        raw.close()


def _wallet(conn: BusinessConnection, user_id: str) -> tuple[int, int]:
    row = conn.execute(
        "SELECT available_credits, reserved_credits FROM wallets WHERE user_id = %s",
        (user_id,),
    ).fetchone()
    return int(row["available_credits"]), int(row["reserved_credits"])


def test_capabilities_report_extended_modes_disabled_by_default(client: TestClient) -> None:
    response = client.get("/api/independent/capabilities", headers=auth_headers("employee_1"))

    assert response.status_code == 200
    body = response.json()
    assert body["i2v_enabled"] is True
    assert body["t2v_enabled"] is False
    assert body["r2v_enabled"] is False
    assert body["last_frame_enabled"] is False
    assert body["extended_modes_enabled"] is False
    assert body["max_quantity"] >= 1


def test_capabilities_flip_with_runtime_flag(client: TestClient, db_path: Path) -> None:
    _enable_extended_modes(db_path)

    body = client.get("/api/independent/capabilities", headers=auth_headers("employee_1")).json()

    assert body["extended_modes_enabled"] is True
    assert body["t2v_enabled"] is True
    assert body["r2v_enabled"] is True
    assert body["last_frame_enabled"] is True


def test_i2v_batch_creation_reserves_seconds_and_marks_independent(
    client: TestClient, db_path: Path
) -> None:
    created = client.post(
        "/api/independent/video-tasks",
        headers=auth_headers("employee_1"),
        json={
            "mode": "i2v",
            "prompt_text": "镜头缓缓推进，展示乡墅庭院的黄昏",
            "first_frame_asset_id": "frame-owned",
            "output_duration_seconds": 10,
            "resolution": "768P",
            "ratio": "16:9",
            "quantity": 2,
            "idempotency_key": "i2v-key-1",
        },
    )

    assert created.status_code == 201, created.text
    batch = created.json()
    assert batch["project_id"] is None
    assert batch["creation_kind"] == "independent"
    assert batch["stale"] is False
    assert batch["progress"]["total_count"] == 2

    with BusinessConnection.sqlite(connect_database(db_path)) as conn:
        mode_row = conn.execute(
            "SELECT generation_mode, billed_seconds FROM generation_tasks WHERE batch_id = %s",
            (batch["id"],),
        ).fetchall()
        assert {str(row["generation_mode"]) for row in mode_row} == {"I2V"}
        assert {int(row["billed_seconds"]) for row in mode_row} == {10}
        available, reserved = _wallet(conn, "employee_1")
        assert (available, reserved) == (980, 20)
        ledger_rounds = conn.execute(
            "SELECT COUNT(*) AS n FROM wallet_transactions WHERE type = 'RESERVE'"
        ).fetchone()
        assert int(ledger_rounds["n"]) == 2


def test_i2v_replay_returns_same_batch_and_conflicting_key_is_rejected(
    client: TestClient,
) -> None:
    payload = {
        "mode": "i2v",
        "prompt_text": "黄昏庭院航拍",
        "first_frame_asset_id": "frame-owned",
        "output_duration_seconds": 8,
        "quantity": 1,
        "idempotency_key": "replay-key",
    }
    first = client.post(
        "/api/independent/video-tasks", headers=auth_headers("employee_1"), json=payload
    )
    replay = client.post(
        "/api/independent/video-tasks", headers=auth_headers("employee_1"), json=payload
    )

    assert first.status_code == 201
    assert replay.status_code == 200 or replay.status_code == 201
    assert replay.json()["id"] == first.json()["id"]

    conflict = client.post(
        "/api/independent/video-tasks",
        headers=auth_headers("employee_1"),
        json={**payload, "prompt_text": "不一样的提示词"},
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "IDEMPOTENCY_CONFLICT"


def test_extended_modes_are_gated_until_verified(client: TestClient) -> None:
    gated_cases = [
        {
            "mode": "t2v",
            "prompt_text": "纯文字生成",
            "output_duration_seconds": 6,
            "quantity": 1,
        },
        {
            "mode": "i2v",
            "prompt_text": "带尾帧",
            "first_frame_asset_id": "frame-owned",
            "last_frame_asset_id": "frame-owned",
            "output_duration_seconds": 6,
            "quantity": 1,
        },
        {
            "mode": "r2v",
            "prompt_text": "参考生成",
            "reference_asset_ids": ["frame-owned"],
            "output_duration_seconds": 6,
            "quantity": 1,
        },
    ]
    for payload in gated_cases:
        response = client.post(
            "/api/independent/video-tasks",
            headers=auth_headers("employee_1"),
            json={**payload, "idempotency_key": f"gate-{payload['mode']}-{len(payload)}"},
        )
        assert response.status_code == 409, payload
        assert response.json()["detail"]["code"] == "EXTENDED_MODE_PENDING_VERIFICATION"


def test_extended_modes_open_after_verification(client: TestClient, db_path: Path) -> None:
    _enable_extended_modes(db_path)

    t2v = client.post(
        "/api/independent/video-tasks",
        headers=auth_headers("employee_1"),
        json={
            "mode": "t2v",
            "prompt_text": "清晨山间别墅的延时摄影",
            "output_duration_seconds": 6,
            "quantity": 1,
            "idempotency_key": "t2v-open",
        },
    )
    r2v = client.post(
        "/api/independent/video-tasks",
        headers=auth_headers("employee_1"),
        json={
            "mode": "r2v",
            "prompt_text": "按照参考图生成别墅外观",
            "reference_asset_ids": ["frame-owned"],
            "output_duration_seconds": 6,
            "quantity": 1,
            "idempotency_key": "r2v-open",
        },
    )
    tail = client.post(
        "/api/independent/video-tasks",
        headers=auth_headers("employee_1"),
        json={
            "mode": "i2v",
            "prompt_text": "首尾帧过渡",
            "first_frame_asset_id": "frame-owned",
            "last_frame_asset_id": "frame-owned",
            "output_duration_seconds": 6,
            "quantity": 1,
            "idempotency_key": "tail-open",
        },
    )

    assert t2v.status_code == 201, t2v.text
    assert r2v.status_code == 201, r2v.text
    assert tail.status_code == 201, tail.text
    assert t2v.json()["stale"] is False


def test_mode_asset_matrix_is_enforced(client: TestClient, db_path: Path) -> None:
    _enable_extended_modes(db_path)
    base = {"output_duration_seconds": 8, "quantity": 1}

    missing_first = client.post(
        "/api/independent/video-tasks",
        headers=auth_headers("employee_1"),
        json={
            **base,
            "mode": "i2v",
            "prompt_text": "没有首帧",
            "idempotency_key": "matrix-1",
        },
    )
    assert missing_first.status_code == 422
    assert missing_first.json()["detail"]["code"] == "INDEPENDENT_FIRST_FRAME_REQUIRED"

    t2v_with_frame = client.post(
        "/api/independent/video-tasks",
        headers=auth_headers("employee_1"),
        json={
            **base,
            "mode": "t2v",
            "prompt_text": "文生却带首帧",
            "first_frame_asset_id": "frame-owned",
            "idempotency_key": "matrix-2",
        },
    )
    assert t2v_with_frame.status_code == 422
    assert t2v_with_frame.json()["detail"]["code"] == "INDEPENDENT_MODE_ASSET_CONFLICT"

    r2v_without_ref = client.post(
        "/api/independent/video-tasks",
        headers=auth_headers("employee_1"),
        json={**base, "mode": "r2v", "prompt_text": "无参考", "idempotency_key": "matrix-3"},
    )
    assert r2v_without_ref.status_code == 422
    assert r2v_without_ref.json()["detail"]["code"] == "INDEPENDENT_REFERENCE_REQUIRED"

    foreign_asset = client.post(
        "/api/independent/video-tasks",
        headers=auth_headers("employee_1"),
        json={
            **base,
            "mode": "i2v",
            "prompt_text": "别人的素材",
            "first_frame_asset_id": "frame-other",
            "idempotency_key": "matrix-4",
        },
    )
    assert foreign_asset.status_code == 404

    video_asset = client.post(
        "/api/independent/video-tasks",
        headers=auth_headers("employee_1"),
        json={
            **base,
            "mode": "i2v",
            "prompt_text": "视频当首帧",
            "first_frame_asset_id": "asset-video",
            "idempotency_key": "matrix-5",
        },
    )
    assert video_asset.status_code == 422
    assert video_asset.json()["detail"]["code"] == "INDEPENDENT_ASSET_KIND_UNSUPPORTED"


def test_reference_images_reject_duplicates_and_more_than_capability_limit(
    client: TestClient, db_path: Path
) -> None:
    _enable_extended_modes(db_path)
    base = {
        "mode": "r2v",
        "prompt_text": "严格校验参考图",
        "output_duration_seconds": 8,
        "quantity": 1,
    }

    duplicate = client.post(
        "/api/independent/video-tasks",
        headers=auth_headers("employee_1"),
        json={
            **base,
            "reference_asset_ids": ["frame-owned", "frame-owned"],
            "idempotency_key": "reference-duplicate",
        },
    )
    assert duplicate.status_code == 422
    assert duplicate.json()["detail"]["code"] == "INDEPENDENT_REFERENCE_DUPLICATE"

    over_limit = client.post(
        "/api/independent/video-tasks",
        headers=auth_headers("employee_1"),
        json={
            **base,
            "reference_asset_ids": [f"frame-{index}" for index in range(5)],
            "idempotency_key": "reference-over-limit",
        },
    )
    assert over_limit.status_code == 422
    assert over_limit.json()["detail"][0]["type"] == "too_long"


def test_auditor_cannot_create_independent_tasks(client: TestClient) -> None:
    response = client.post(
        "/api/independent/video-tasks",
        headers=auth_headers("auditor_1"),
        json={
            "mode": "i2v",
            "prompt_text": "审计员不可提交",
            "first_frame_asset_id": "frame-owned",
            "output_duration_seconds": 8,
            "quantity": 1,
            "idempotency_key": "auditor-key",
        },
    )

    assert response.status_code == 403


def test_worker_settles_independent_task_and_releases_on_failure(
    client: TestClient, db_path: Path
) -> None:
    created = client.post(
        "/api/independent/video-tasks",
        headers=auth_headers("employee_1"),
        json={
            "mode": "i2v",
            "prompt_text": "首帧推进镜头",
            "first_frame_asset_id": "frame-owned",
            "output_duration_seconds": 10,
            "quantity": 2,
            "idempotency_key": "worker-key",
        },
    )
    assert created.status_code == 201
    batch_id = created.json()["id"]

    storage = FakeStorageAdapter(provider="fake", bucket="generation-results")
    with BusinessConnection.sqlite(connect_database(db_path)) as conn:
        processed = run_worker_once(conn, worker_id="independent-worker", storage=storage)

    assert processed == 2
    with BusinessConnection.sqlite(connect_database(db_path)) as conn:
        available, reserved = _wallet(conn, "employee_1")
        assert (available, reserved) == (980, 0)  # SETTLE：reserved 清零
        settles = conn.execute(
            "SELECT COUNT(*) AS n FROM wallet_transactions WHERE type = 'SETTLE'"
        ).fetchone()
        assert int(settles["n"]) == 2

    listed = client.get(
        "/api/generation-batches",
        headers=auth_headers("employee_1"),
    ).json()
    independent_items = [item for item in listed["items"] if item["id"] == batch_id]
    assert len(independent_items) == 1
    assert independent_items[0]["creation_kind"] == "independent"
    assert independent_items[0]["progress"]["progress_percent"] == 100

    other = client.get("/api/generation-batches", headers=auth_headers("employee_2")).json()
    assert all(item["id"] != batch_id for item in other["items"])

    detail = client.get(f"/api/generation-batches/{batch_id}", headers=auth_headers("employee_1"))
    assert detail.status_code == 200
    assert detail.json()["project_id"] is None

    denied = client.get(f"/api/generation-batches/{batch_id}", headers=auth_headers("employee_2"))
    assert denied.status_code == 404


def test_independent_batches_never_leak_provider_names(client: TestClient) -> None:
    created = client.post(
        "/api/independent/video-tasks",
        headers=auth_headers("employee_1"),
        json={
            "mode": "i2v",
            "prompt_text": "红线检查",
            "first_frame_asset_id": "frame-owned",
            "output_duration_seconds": 6,
            "quantity": 1,
            "idempotency_key": "redline-key",
        },
    )
    body = created.text.lower()
    assert "metaso" not in body
    assert "tikhub" not in body


def test_saved_prompts_aggregate_across_projects(client: TestClient, db_path: Path) -> None:
    raw = sqlite3.connect(db_path)
    try:
        raw.execute(
            "INSERT INTO users (id, username, display_name, role) "
            "VALUES ('u3','u3','U3','employee')"
        )
        raw.execute("INSERT INTO projects (id, owner_user_id, name) VALUES ('project_c','u3','C')")
        raw.executemany(
            """
            INSERT INTO versions (
                id, project_id, kind, version_number, payload_json,
                created_by_user_id, scope, source, author_user_id
            ) VALUES (?, ?, 'saved_prompt', 1, ?, ?, 'user', 'reverse_prompt_revision', ?)
            """,
            [
                (
                    "sp-1",
                    "project_a",
                    json.dumps({"name": "庭院黄昏", "prompt_text": "A 的提示词"}),
                    "employee_1",
                    "employee_1",
                ),
                (
                    "sp-2",
                    "project_b",
                    json.dumps({"name": "别人的", "prompt_text": "B 的提示词"}),
                    "employee_2",
                    "employee_2",
                ),
                (
                    "sp-3",
                    "project_c",
                    json.dumps({"name": "空文本", "prompt_text": ""}),
                    "u3",
                    "u3",
                ),
            ],
        )
        raw.commit()
    finally:
        raw.close()

    response = client.get("/api/studio/saved-prompts", headers=auth_headers("employee_1"))

    assert response.status_code == 200
    items = response.json()["items"]
    assert [item["id"] for item in items] == ["sp-1"]
    assert items[0]["name"] == "庭院黄昏"
    assert items[0]["prompt_text"] == "A 的提示词"


def test_cancel_independent_batch_releases_reserved_seconds(
    client: TestClient, db_path: Path
) -> None:
    created = client.post(
        "/api/independent/video-tasks",
        headers=auth_headers("employee_1"),
        json={
            "mode": "i2v",
            "prompt_text": "排队中取消",
            "first_frame_asset_id": "frame-owned",
            "output_duration_seconds": 10,
            "quantity": 1,
            "idempotency_key": "cancel-key",
        },
    )
    assert created.status_code == 201
    batch_id = created.json()["id"]

    from app.db import connect_database

    with BusinessConnection.sqlite(connect_database(db_path)) as conn:
        available, reserved = _wallet(conn, "employee_1")
        assert (available, reserved) == (990, 10)

    cancelled = client.post(
        f"/api/generation-batches/{batch_id}/cancel",
        headers=auth_headers("employee_1"),
    )
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "CANCELLED"

    with BusinessConnection.sqlite(connect_database(db_path)) as conn:
        available, reserved = _wallet(conn, "employee_1")
        assert (available, reserved) == (1000, 0)  # RELEASE：全额退还
        releases = conn.execute(
            "SELECT COUNT(*) AS n FROM wallet_transactions WHERE type = 'RELEASE'"
        ).fetchone()
        assert int(releases["n"]) == 1


def test_failed_independent_task_releases_credits(
    client: TestClient,
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.db import connect_database

    monkeypatch.setenv("VIDEO_REPLICA_FAKE_H3_OUTCOME", "provider_failed")
    created = client.post(
        "/api/independent/video-tasks",
        headers=auth_headers("employee_1"),
        json={
            "mode": "i2v",
            "prompt_text": "供应商失败的回款",
            "first_frame_asset_id": "frame-owned",
            "output_duration_seconds": 6,
            "quantity": 1,
            "idempotency_key": "fail-key",
        },
    )
    assert created.status_code == 201

    storage = FakeStorageAdapter(provider="fake", bucket="generation-results")
    with BusinessConnection.sqlite(connect_database(db_path)) as conn:
        run_worker_once(conn, worker_id="fail-worker", storage=storage)

    with BusinessConnection.sqlite(connect_database(db_path)) as conn:
        available, reserved = _wallet(conn, "employee_1")
        assert (available, reserved) == (1000, 0)  # RELEASE：失败不扣费
        status_row = conn.execute(
            "SELECT status FROM generation_tasks WHERE batch_id = %s",
            (created.json()["id"],),
        ).fetchone()
        assert str(status_row["status"]) == "FAILED"


def test_t2v_and_r2v_tasks_run_through_worker_with_protocol_payload(
    client: TestClient, db_path: Path
) -> None:
    from app.db import connect_database

    _enable_extended_modes(db_path)
    t2v = client.post(
        "/api/independent/video-tasks",
        headers=auth_headers("employee_1"),
        json={
            "mode": "t2v",
            "prompt_text": "清晨山间别墅的延时摄影",
            "output_duration_seconds": 6,
            "quantity": 1,
            "idempotency_key": "t2v-worker",
        },
    )
    r2v = client.post(
        "/api/independent/video-tasks",
        headers=auth_headers("employee_1"),
        json={
            "mode": "r2v",
            "prompt_text": "按照参考图生成别墅外观",
            "reference_asset_ids": ["frame-owned"],
            "output_duration_seconds": 6,
            "quantity": 1,
            "idempotency_key": "r2v-worker",
        },
    )
    assert t2v.status_code == 201 and r2v.status_code == 201

    storage = FakeStorageAdapter(provider="fake", bucket="generation-results")
    with BusinessConnection.sqlite(connect_database(db_path)) as conn:
        processed = run_worker_once(conn, worker_id="mode-worker", storage=storage)

    assert processed == 2
    with BusinessConnection.sqlite(connect_database(db_path)) as conn:
        rows = conn.execute(
            "SELECT generation_mode, provider_request_json FROM generation_tasks"
        ).fetchall()
        by_mode = {str(row["generation_mode"]): row for row in rows}
        t2v_request = json.loads(str(by_mode["T2V"]["provider_request_json"]))
        assert [item["type"] for item in t2v_request["content"]] == ["text"]

        r2v_request = json.loads(str(by_mode["R2V"]["provider_request_json"]))
        roles = [item.get("role") for item in r2v_request["content"][1:]]
        assert roles == ["reference_image"]
        assert r2v_request["content"][1]["name"] == "ref-1"
        assert "first_frame" not in roles and "last_frame" not in roles
