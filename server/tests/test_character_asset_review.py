from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.auth import get_database
from app.character_identity import (
    REQUIRED_CHARACTER_VIEW_TYPES,
    encode_json,
    generated_character_asset_key,
)
from app.character_identity_routes import get_character_storage
from app.character_image_generation import deterministic_png
from app.db import connect_database, initialize_database
from app.main import app
from app.media_routes import get_media_storage
from app.storage import FakeStorageAdapter, StorageBackendUnavailable


@dataclass(frozen=True)
class SeededCharacterVersion:
    version_id: str
    identity_id: str
    selected_by_view: dict[str, str]
    generated_asset_by_view: dict[str, str]


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "character-review.db"
    with initialize_database(path) as conn:
        conn.executemany(
            """
            INSERT INTO users (id, username, display_name, role)
            VALUES (?, ?, ?, ?)
            """,
            [
                ("admin_1", "admin_1", "Admin One", "admin"),
                ("employee_1", "employee_1", "Employee One", "employee"),
                ("auditor_1", "auditor_1", "Auditor One", "auditor"),
            ],
        )
        conn.commit()
    return path


@pytest.fixture()
def storage() -> FakeStorageAdapter:
    return FakeStorageAdapter(provider="fake", bucket="character-private")


@pytest.fixture()
def client(db_path: Path, storage: FakeStorageAdapter) -> Iterator[TestClient]:
    def database_override() -> Iterator[sqlite3.Connection]:
        conn = connect_database(db_path)
        try:
            yield conn
        finally:
            conn.close()

    app.dependency_overrides[get_database] = database_override
    app.dependency_overrides[get_character_storage] = lambda: storage
    app.dependency_overrides[get_media_storage] = lambda: storage
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def headers(user_id: str) -> dict[str, str]:
    return {"X-Dev-User-Id": user_id}


def seed_reviewing_version(
    db_path: Path,
    storage: FakeStorageAdapter,
    *,
    extra_front_candidate: bool = False,
) -> SeededCharacterVersion:
    version_id = "character-version-1"
    identity_id = "identity-1"
    persona_id = "persona-1"
    source_content = deterministic_png(b"source-image")
    source = storage.put_object(
        "users/employee_1/identities/identity-1/source/source-asset.png",
        source_content,
        content_type="image/png",
    )
    authorization = storage.put_object(
        "users/employee_1/identities/identity-1/authorization/authorization.pdf",
        b"%PDF-1.7\nauthorized",
        content_type="application/pdf",
    )
    selected_by_view: dict[str, str] = {}
    generated_asset_by_view: dict[str, str] = {}
    with connect_database(db_path) as conn:
        conn.executemany(
            """
            INSERT INTO assets (
                id, project_id, kind, storage_uri, sha256, size_bytes,
                content_type, created_by_user_id
            ) VALUES (?, NULL, ?, ?, ?, ?, ?, 'admin_1')
            """,
            [
                (
                    "authorization-asset",
                    "character_authorization",
                    authorization.uri,
                    authorization.sha256,
                    authorization.size,
                    authorization.content_type,
                ),
                (
                    "source-asset",
                    "character_source_image",
                    source.uri,
                    source.sha256,
                    source.size,
                    source.content_type,
                ),
            ],
        )
        conn.execute(
            """
            INSERT INTO person_identities (
                id, owner_user_id, display_name, authorization_status,
                authorization_asset_id, authorization_scope,
                authorization_expires_at, source_asset_id,
                source_quality_status, status, created_by
            ) VALUES (
                ?, 'employee_1', '荣哥', 'AUTHORIZED',
                'authorization-asset', '["internal-short-video"]',
                '2035-01-01T00:00:00+00:00', 'source-asset',
                'PASSED', 'ACTIVE', 'admin_1'
            )
            """,
            (identity_id,),
        )
        conn.execute(
            """
            INSERT INTO character_personas (
                id, identity_id, name, costume_description, created_by
            ) VALUES (?, ?, '乡墅项目管理专家', '工程马甲', 'admin_1')
            """,
            (persona_id, identity_id),
        )
        conn.execute(
            """
            INSERT INTO character_versions (
                id, persona_id, version_number, status, source_asset_id,
                source_sha256, persona_snapshot_json, provider, model,
                generation_params_json, template_version, template_hash,
                required_view_types_json, created_by
            ) VALUES (
                ?, ?, 1, 'REVIEWING', 'source-asset', ?, '{}',
                'fake_character', 'fake-character-v1', '{}',
                'character-prompt-v1', ?, ?, 'admin_1'
            )
            """,
            (
                version_id,
                persona_id,
                source.sha256,
                hashlib.sha256(b"character-prompt-v1").hexdigest(),
                encode_json(list(REQUIRED_CHARACTER_VIEW_TYPES)),
            ),
        )
        for view_type in REQUIRED_CHARACTER_VIEW_TYPES:
            character_asset_id = f"candidate-{view_type.lower()}-1"
            generated_asset_id = f"generated-{view_type.lower()}-1"
            content = deterministic_png(view_type.encode())
            stored = storage.put_object(
                generated_character_asset_key(
                    owner_user_id="employee_1",
                    persona_id=persona_id,
                    version_id=version_id,
                    view_type=view_type,
                    asset_id=generated_asset_id,
                ),
                content,
                content_type="image/png",
            )
            conn.execute(
                """
                INSERT INTO assets (
                    id, project_id, kind, storage_uri, sha256, size_bytes,
                    content_type, created_by_user_id
                ) VALUES (?, NULL, 'character_generated_image', ?, ?, ?, ?, 'admin_1')
                """,
                (
                    generated_asset_id,
                    stored.uri,
                    stored.sha256,
                    stored.size,
                    stored.content_type,
                ),
            )
            conn.execute(
                """
                INSERT INTO character_assets (
                    id, character_version_id, asset_id, view_type,
                    candidate_number, auto_quality_json, review_status,
                    is_published_selection
                ) VALUES (?, ?, ?, ?, 1, ?, 'NOT_REVIEWED', 0)
                """,
                (
                    character_asset_id,
                    version_id,
                    generated_asset_id,
                    view_type,
                    encode_json(
                        {
                            "blocking_issue_codes": [],
                            "schema_version": "character-quality.v1",
                            "simulated": True,
                        }
                    ),
                ),
            )
            selected_by_view[view_type] = character_asset_id
            generated_asset_by_view[view_type] = generated_asset_id

        if extra_front_candidate:
            content = deterministic_png(b"front-face-candidate-2")
            stored = storage.put_object(
                generated_character_asset_key(
                    owner_user_id="employee_1",
                    persona_id=persona_id,
                    version_id=version_id,
                    view_type="FRONT_FACE",
                    asset_id="generated-front_face-2",
                ),
                content,
                content_type="image/png",
            )
            conn.execute(
                """
                INSERT INTO assets (
                    id, project_id, kind, storage_uri, sha256, size_bytes,
                    content_type, created_by_user_id
                ) VALUES (
                    'generated-front_face-2', NULL, 'character_generated_image',
                    ?, ?, ?, ?, 'admin_1'
                )
                """,
                (stored.uri, stored.sha256, stored.size, stored.content_type),
            )
            conn.execute(
                """
                INSERT INTO character_assets (
                    id, character_version_id, asset_id, view_type,
                    candidate_number, auto_quality_json, review_status,
                    is_published_selection
                ) VALUES (
                    'candidate-front_face-2', ?, 'generated-front_face-2',
                    'FRONT_FACE', 2, '{}', 'NOT_REVIEWED', 0
                )
                """,
                (version_id,),
            )
        conn.commit()
    return SeededCharacterVersion(
        version_id=version_id,
        identity_id=identity_id,
        selected_by_view=selected_by_view,
        generated_asset_by_view=generated_asset_by_view,
    )


def review_asset(
    client: TestClient,
    asset_id: str,
    *,
    decision: str,
    issue_codes: list[str] | None = None,
    comment: str | None = None,
    user_id: str = "admin_1",
) -> object:
    return client.post(
        f"/api/character-assets/{asset_id}/review",
        headers=headers(user_id),
        json={
            "decision": decision,
            "issue_codes": issue_codes or [],
            "comment": comment,
        },
    )


def approve_all(client: TestClient, seeded: SeededCharacterVersion) -> None:
    for asset_id in seeded.selected_by_view.values():
        response = review_asset(client, asset_id, decision="APPROVED")
        assert response.status_code == 201


def test_admin_review_is_append_only_and_roles_fail_closed(
    client: TestClient,
    db_path: Path,
    storage: FakeStorageAdapter,
) -> None:
    seeded = seed_reviewing_version(db_path, storage)
    asset_id = seeded.selected_by_view["FRONT_FACE"]

    employee = review_asset(client, asset_id, decision="APPROVED", user_id="employee_1")
    auditor = review_asset(client, asset_id, decision="APPROVED", user_id="auditor_1")
    no_reason = review_asset(client, asset_id, decision="REJECTED")
    rejected = review_asset(
        client,
        asset_id,
        decision="REJECTED",
        issue_codes=["FACE_DRIFT"],
        comment="脸型偏离源图",
    )
    approved = review_asset(client, asset_id, decision="APPROVED", comment="人工复核通过")
    history = client.get(
        f"/api/character-assets/{asset_id}/reviews",
        headers=headers("auditor_1"),
    )

    assert employee.status_code == 403
    assert auditor.status_code == 403
    assert no_reason.status_code == 422
    assert no_reason.json()["detail"]["code"] == "CHARACTER_ASSET_REVIEW_REASON_REQUIRED"
    assert rejected.status_code == 201
    assert approved.status_code == 201
    assert history.status_code == 200
    assert [review["decision"] for review in history.json()] == ["REJECTED", "APPROVED"]
    with connect_database(db_path) as conn:
        asset_status = conn.execute(
            "SELECT review_status FROM character_assets WHERE id = ?",
            (asset_id,),
        ).fetchone()[0]
        review_count = conn.execute(
            "SELECT COUNT(*) FROM character_asset_reviews WHERE character_asset_id = ?",
            (asset_id,),
        ).fetchone()[0]
        audit_count = conn.execute(
            """
            SELECT COUNT(*) FROM audit_logs
            WHERE action = 'character_asset.review' AND entity_id = ?
            """,
            (asset_id,),
        ).fetchone()[0]
    assert asset_status == "APPROVED"
    assert review_count == 2
    assert audit_count == 2


def test_publish_freezes_seven_approved_assets_and_is_idempotent(
    client: TestClient,
    db_path: Path,
    storage: FakeStorageAdapter,
) -> None:
    seeded = seed_reviewing_version(db_path, storage, extra_front_candidate=True)
    rejected = review_asset(
        client,
        "candidate-front_face-2",
        decision="REJECTED",
        issue_codes=["VIEW_TYPE_MISMATCH"],
    )
    assert rejected.status_code == 201
    approve_all(client, seeded)

    published = client.post(
        f"/api/character-versions/{seeded.version_id}/publish",
        headers=headers("admin_1"),
        json={"selected_asset_ids": seeded.selected_by_view},
    )
    replay = client.post(
        f"/api/character-versions/{seeded.version_id}/publish",
        headers=headers("admin_1"),
        json={"selected_asset_ids": seeded.selected_by_view},
    )
    different_selection = dict(seeded.selected_by_view)
    different_selection["FRONT_FACE"] = "candidate-front_face-2"
    mismatched_replay = client.post(
        f"/api/character-versions/{seeded.version_id}/publish",
        headers=headers("admin_1"),
        json={"selected_asset_ids": different_selection},
    )

    assert published.status_code == 200
    assert replay.status_code == 200
    body = published.json()
    assert body["status"] == "PUBLISHED"
    assert len(body["publication_hash"]) == 64
    assert body["publication_snapshot_json"]["schema_version"] == "character-publication.v1"
    assert set(body["publication_snapshot_json"]["assets_by_view"]) == set(
        REQUIRED_CHARACTER_VIEW_TYPES
    )
    assert (
        hashlib.sha256(encode_json(body["publication_snapshot_json"]).encode()).hexdigest()
        == body["publication_hash"]
    )
    assert replay.json()["publication_hash"] == body["publication_hash"]
    assert mismatched_replay.status_code == 409
    assert (
        mismatched_replay.json()["detail"]["code"]
        == "CHARACTER_VERSION_ALREADY_PUBLISHED_DIFFERENT_SELECTION"
    )

    with connect_database(db_path) as conn:
        selected = conn.execute(
            """
            SELECT character_asset.view_type, character_asset.id,
                   character_asset.asset_id, asset.storage_uri, asset.sha256
            FROM character_assets AS character_asset
            JOIN assets AS asset ON asset.id = character_asset.asset_id
            WHERE character_asset.character_version_id = ?
              AND character_asset.is_published_selection = 1
            ORDER BY character_asset.view_type
            """,
            (seeded.version_id,),
        ).fetchall()
        publication_audits = conn.execute(
            """
            SELECT COUNT(*) FROM audit_logs
            WHERE action = 'character_version.publish' AND entity_id = ?
            """,
            (seeded.version_id,),
        ).fetchone()[0]
    assert len(selected) == 7
    assert publication_audits == 1
    assert {row["id"] for row in selected} == set(seeded.selected_by_view.values())
    for row in selected:
        view_type = str(row["view_type"])
        snapshot = body["publication_snapshot_json"]["assets_by_view"][view_type]
        assert snapshot["character_asset_id"] == row["id"]
        assert snapshot["generated_asset_id"] == seeded.generated_asset_by_view[view_type]
        assert snapshot["approved_asset_id"] == row["asset_id"]
        assert snapshot["sha256"] == row["sha256"]
        assert "/approved/" in row["storage_uri"]

    employee_assets = client.get(
        f"/api/character-versions/{seeded.version_id}/assets",
        headers=headers("employee_1"),
    )
    employee_asset = client.get(
        f"/api/assets/{selected[0]['asset_id']}",
        headers=headers("employee_1"),
    )
    assert employee_assets.status_code == 200
    assert len(employee_assets.json()) == 7
    assert employee_asset.status_code == 200

    immutable_review = review_asset(
        client,
        seeded.selected_by_view["FRONT_FACE"],
        decision="REJECTED",
        issue_codes=["LATE_CHANGE"],
    )
    regenerate = client.post(
        f"/api/character-assets/{seeded.selected_by_view['FRONT_FACE']}/regenerate",
        headers=headers("admin_1"),
        json={"idempotency_key": "after-publish"},
    )
    assert immutable_review.status_code == 409
    assert immutable_review.json()["detail"]["code"] == "CHARACTER_VERSION_IMMUTABLE"
    assert regenerate.status_code == 409


def test_publish_rejects_incomplete_unapproved_active_or_revoked_state(
    client: TestClient,
    db_path: Path,
    storage: FakeStorageAdapter,
) -> None:
    seeded = seed_reviewing_version(db_path, storage)
    incomplete = dict(seeded.selected_by_view)
    incomplete.pop("RIGHT_SIDE")
    response = client.post(
        f"/api/character-versions/{seeded.version_id}/publish",
        headers=headers("admin_1"),
        json={"selected_asset_ids": incomplete},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "CHARACTER_PUBLISH_SELECTION_INCOMPLETE"

    unapproved = client.post(
        f"/api/character-versions/{seeded.version_id}/publish",
        headers=headers("admin_1"),
        json={"selected_asset_ids": seeded.selected_by_view},
    )
    assert unapproved.status_code == 409
    assert unapproved.json()["detail"]["code"] == "CHARACTER_PUBLISH_SELECTION_NOT_APPROVED"

    approve_all(client, seeded)
    with connect_database(db_path) as conn:
        conn.execute(
            """
            INSERT INTO character_generation_tasks (
                id, character_version_id, view_type, provider, model,
                request_snapshot_json, status, idempotency_key, request_hash,
                candidate_number, created_by
            ) VALUES (
                'active-task', ?, 'FRONT_FACE', 'fake_character', 'fake-character-v1',
                '{}', 'PENDING', 'active-task', 'active-task', 2, 'admin_1'
            )
            """,
            (seeded.version_id,),
        )
        conn.commit()
    active = client.post(
        f"/api/character-versions/{seeded.version_id}/publish",
        headers=headers("admin_1"),
        json={"selected_asset_ids": seeded.selected_by_view},
    )
    assert active.status_code == 409
    assert active.json()["detail"]["code"] == "CHARACTER_PUBLISH_TASKS_ACTIVE"

    with connect_database(db_path) as conn:
        conn.execute("DELETE FROM character_generation_tasks WHERE id = 'active-task'")
        conn.execute(
            """
            UPDATE person_identities
            SET authorization_status = 'REVOKED', status = 'REVOKED'
            WHERE id = ?
            """,
            (seeded.identity_id,),
        )
        conn.commit()
    revoked = client.post(
        f"/api/character-versions/{seeded.version_id}/publish",
        headers=headers("admin_1"),
        json={"selected_asset_ids": seeded.selected_by_view},
    )
    assert revoked.status_code == 409
    assert revoked.json()["detail"]["code"] == "IDENTITY_NOT_ACTIVE"


def test_publish_storage_failure_removes_partial_approved_objects(
    client: TestClient,
    db_path: Path,
    storage: FakeStorageAdapter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seeded = seed_reviewing_version(db_path, storage)
    approve_all(client, seeded)
    original_put = storage.put_object
    approved_keys: list[str] = []

    def fail_third_approved_put(
        key: str,
        content: bytes,
        *,
        content_type: str,
    ) -> object:
        if "/approved/" in key:
            approved_keys.append(key)
            if len(approved_keys) == 3:
                raise StorageBackendUnavailable("simulated approved storage failure")
        return original_put(key, content, content_type=content_type)

    monkeypatch.setattr(storage, "put_object", fail_third_approved_put)
    response = client.post(
        f"/api/character-versions/{seeded.version_id}/publish",
        headers=headers("admin_1"),
        json={"selected_asset_ids": seeded.selected_by_view},
    )

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "CHARACTER_PUBLICATION_STORAGE_UNAVAILABLE"
    assert len(approved_keys) == 3
    assert all(storage.head_object(key) is None for key in approved_keys)
    with connect_database(db_path) as conn:
        version = conn.execute(
            "SELECT status, publication_snapshot_json FROM character_versions WHERE id = ?",
            (seeded.version_id,),
        ).fetchone()
        published_count = conn.execute(
            """
            SELECT COUNT(*) FROM character_assets
            WHERE character_version_id = ? AND is_published_selection = 1
            """,
            (seeded.version_id,),
        ).fetchone()[0]
    assert version["status"] == "REVIEWING"
    assert version["publication_snapshot_json"] is None
    assert published_count == 0


def test_publish_rejects_non_png_candidate_before_copying(
    client: TestClient,
    db_path: Path,
    storage: FakeStorageAdapter,
) -> None:
    seeded = seed_reviewing_version(db_path, storage)
    approve_all(client, seeded)
    with connect_database(db_path) as conn:
        conn.execute(
            "UPDATE assets SET content_type = 'application/octet-stream' WHERE id = ?",
            (seeded.generated_asset_by_view["FRONT_FACE"],),
        )
        conn.commit()

    response = client.post(
        f"/api/character-versions/{seeded.version_id}/publish",
        headers=headers("admin_1"),
        json={"selected_asset_ids": seeded.selected_by_view},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "CHARACTER_PUBLISH_ASSET_INVALID"
    assert not any(event.object_key.find("/approved/") >= 0 for event in storage.audit_events)


def test_publish_revalidates_version_after_approved_object_copy(
    client: TestClient,
    db_path: Path,
    storage: FakeStorageAdapter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seeded = seed_reviewing_version(db_path, storage)
    approve_all(client, seeded)
    original_put = storage.put_object
    approved_keys: list[str] = []

    def archive_during_first_approved_put(
        key: str,
        content: bytes,
        *,
        content_type: str,
    ) -> object:
        stored = original_put(key, content, content_type=content_type)
        if "/approved/" in key:
            approved_keys.append(key)
            if len(approved_keys) == 1:
                with connect_database(db_path) as conn:
                    conn.execute(
                        "UPDATE character_versions SET status = 'ARCHIVED' WHERE id = ?",
                        (seeded.version_id,),
                    )
                    conn.commit()
        return stored

    monkeypatch.setattr(storage, "put_object", archive_during_first_approved_put)
    response = client.post(
        f"/api/character-versions/{seeded.version_id}/publish",
        headers=headers("admin_1"),
        json={"selected_asset_ids": seeded.selected_by_view},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "CHARACTER_VERSION_IMMUTABLE"
    assert len(approved_keys) == 7
    assert all(storage.head_object(key) is None for key in approved_keys)
    with connect_database(db_path) as conn:
        version = conn.execute(
            "SELECT status, publication_snapshot_json FROM character_versions WHERE id = ?",
            (seeded.version_id,),
        ).fetchone()
        published_count = conn.execute(
            """
            SELECT COUNT(*) FROM character_assets
            WHERE character_version_id = ? AND is_published_selection = 1
            """,
            (seeded.version_id,),
        ).fetchone()[0]
    assert version["status"] == "ARCHIVED"
    assert version["publication_snapshot_json"] is None
    assert published_count == 0


def test_publication_snapshot_is_canonical_json() -> None:
    payload = {
        "schema_version": "character-publication.v1",
        "assets_by_view": {"FRONT_FACE": {"sha256": "a" * 64}},
    }
    first = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    second = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    assert hashlib.sha256(first.encode()).hexdigest() == hashlib.sha256(second.encode()).hexdigest()
