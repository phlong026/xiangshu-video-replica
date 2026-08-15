from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.auth import get_database
from app.character_identity import REQUIRED_CHARACTER_VIEW_TYPES, encode_json
from app.character_reference_matching import SourceFrameFeatures, recommended_body_view
from app.db import connect_database, initialize_database
from app.first_frame_routes import get_image_provider
from app.first_frames import GeneratedImage, ImageInput
from app.main import app
from app.media_routes import get_media_storage
from app.storage import FakeStorageAdapter


@dataclass(frozen=True)
class SeededReferenceContext:
    project_id: str
    source_selection_id: str
    character_version_id: str
    approved_asset_by_view: dict[str, str]
    publication_hash: str


@dataclass
class RecordingImageProvider:
    provider_name: str = "fake"
    calls: list[dict[str, object]] = field(default_factory=list)

    def edit(
        self,
        *,
        model: str,
        prompt: str,
        source_image: ImageInput,
        character_reference_images: list[ImageInput],
        output_count: int,
    ) -> list[GeneratedImage]:
        self.calls.append(
            {
                "model": model,
                "prompt": prompt,
                "source_image": source_image.content,
                "character_reference_images": [
                    image.content for image in character_reference_images
                ],
                "output_count": output_count,
            }
        )
        return [
            GeneratedImage(content=f"first-frame-{index}".encode(), content_type="image/png")
            for index in range(output_count)
        ]


@pytest.fixture()
def storage() -> FakeStorageAdapter:
    return FakeStorageAdapter(provider="fake", bucket="character-private")


@pytest.fixture()
def db_path(tmp_path: Path, storage: FakeStorageAdapter) -> Path:
    path = tmp_path / "character-reference-matching.db"
    with initialize_database(path) as conn:
        seed_users_and_projects(conn)
        seed_reference_context(conn, storage)
    return path


@pytest.fixture()
def provider() -> RecordingImageProvider:
    return RecordingImageProvider()


@pytest.fixture()
def client(
    db_path: Path,
    storage: FakeStorageAdapter,
    provider: RecordingImageProvider,
) -> Iterator[TestClient]:
    def database_override() -> Iterator[sqlite3.Connection]:
        conn = connect_database(db_path)
        try:
            yield conn
        finally:
            conn.close()

    app.dependency_overrides[get_database] = database_override
    app.dependency_overrides[get_media_storage] = lambda: storage
    app.dependency_overrides[get_image_provider] = lambda: provider
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def headers(user_id: str) -> dict[str, str]:
    return {"X-Dev-User-Id": user_id}


def seed_users_and_projects(conn: sqlite3.Connection) -> None:
    conn.executemany(
        "INSERT INTO users (id, username, display_name, role) VALUES (?, ?, ?, ?)",
        [
            ("admin_1", "admin_1", "Admin One", "admin"),
            ("employee_1", "employee_1", "Employee One", "employee"),
            ("employee_2", "employee_2", "Employee Two", "employee"),
            ("auditor_1", "auditor_1", "Auditor One", "auditor"),
        ],
    )
    conn.executemany(
        "INSERT INTO projects (id, owner_user_id, name) VALUES (?, ?, ?)",
        [
            ("project-owned", "employee_1", "Owned Project"),
            ("project-other", "employee_2", "Other Project"),
        ],
    )
    conn.commit()


def seed_reference_context(
    conn: sqlite3.Connection,
    storage: FakeStorageAdapter,
    *,
    include_features: bool = True,
) -> SeededReferenceContext:
    project_id = "project-owned"
    source_selection_id = "source-selection-v1"
    character_version_id = "character-version-v1"
    source_frame = storage.put_object(
        "projects/project-owned/source-frames/source-frame.png",
        b"source-frame",
        content_type="image/png",
    )
    authorization = storage.put_object(
        "users/employee_1/identities/identity-1/authorization/authorization.pdf",
        b"%PDF-1.7\nauthorized",
        content_type="application/pdf",
    )
    identity_source = storage.put_object(
        "users/employee_1/identities/identity-1/source/source.png",
        b"identity-source",
        content_type="image/png",
    )
    conn.executemany(
        """
        INSERT INTO assets (
            id, project_id, kind, storage_uri, sha256, size_bytes,
            content_type, created_by_user_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'admin_1')
        """,
        [
            (
                "source-frame-asset",
                project_id,
                "source_frame",
                source_frame.uri,
                source_frame.sha256,
                source_frame.size,
                source_frame.content_type,
            ),
            (
                "authorization-asset",
                None,
                "character_authorization",
                authorization.uri,
                authorization.sha256,
                authorization.size,
                authorization.content_type,
            ),
            (
                "identity-source-asset",
                None,
                "character_source_image",
                identity_source.uri,
                identity_source.sha256,
                identity_source.size,
                identity_source.content_type,
            ),
        ],
    )
    candidate_payload = {
        "schema_version": "b4.source-frame.v1",
        "candidates": [{"asset_id": "source-frame-asset", "timestamp_seconds": 0.5}],
    }
    selection_payload: dict[str, object] = {
        "schema_version": "b4.source-frame.v1",
        "source_frame_candidates_version_id": "source-candidates-v1",
        "source_frame_asset_id": "source-frame-asset",
        "timestamp_seconds": 0.5,
    }
    if include_features:
        selection_payload["character_features"] = {
            "orientation": "RIGHT_45",
            "shot_size": "HALF_BODY",
            "face_visible": True,
            "body_completeness": "UPPER_BODY",
        }
    conn.executemany(
        """
        INSERT INTO versions (
            id, project_id, asset_id, kind, version_number,
            payload_json, created_by_user_id
        ) VALUES (?, ?, ?, ?, 1, ?, 'employee_1')
        """,
        [
            (
                "source-candidates-v1",
                project_id,
                "source-frame-asset",
                "source_frame_candidates",
                encode_json(candidate_payload),
            ),
            (
                source_selection_id,
                project_id,
                "source-frame-asset",
                "source_frame_selection",
                encode_json(selection_payload),
            ),
            (
                "main-character-selection-v1",
                project_id,
                None,
                "main_character_selection",
                encode_json({"project_id": project_id}),
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
            'identity-1', 'employee_1', '林夏', 'AUTHORIZED',
            'authorization-asset', '["internal-short-video"]',
            '2035-01-01T00:00:00+00:00', 'identity-source-asset',
            'PASSED', 'ACTIVE', 'admin_1'
        )
        """
    )
    conn.execute(
        """
        INSERT INTO character_personas (
            id, identity_id, name, costume_description, created_by
        ) VALUES ('persona-1', 'identity-1', '乡墅项目管理专家', '工程马甲', 'admin_1')
        """
    )

    approved_asset_by_view: dict[str, str] = {}
    assets_by_view: dict[str, object] = {}
    for view_type in REQUIRED_CHARACTER_VIEW_TYPES:
        approved_asset_id = f"approved-{view_type.lower()}"
        character_asset_id = f"candidate-{view_type.lower()}"
        content = f"approved-content-{view_type}".encode()
        stored = storage.put_object(
            f"users/employee_1/personas/persona-1/versions/{character_version_id}"
            f"/approved/{view_type}/{approved_asset_id}.png",
            content,
            content_type="image/png",
        )
        conn.execute(
            """
            INSERT INTO assets (
                id, project_id, kind, storage_uri, sha256, size_bytes,
                content_type, created_by_user_id
            ) VALUES (?, NULL, 'character_approved_image', ?, ?, ?, 'image/png', 'admin_1')
            """,
            (approved_asset_id, stored.uri, stored.sha256, stored.size),
        )
        approved_asset_by_view[view_type] = approved_asset_id
        assets_by_view[view_type] = {
            "approved_asset_id": approved_asset_id,
            "character_asset_id": character_asset_id,
            "content_type": "image/png",
            "generated_asset_id": f"generated-{view_type.lower()}",
            "review_id": f"review-{view_type.lower()}",
            "sha256": stored.sha256,
            "size_bytes": stored.size,
            "storage_uri": stored.uri,
        }

    publication_snapshot = {
        "assets_by_view": assets_by_view,
        "character_version_id": character_version_id,
        "persona_snapshot_hash": hashlib.sha256(b"persona-snapshot").hexdigest(),
        "published_at": "2026-08-15T00:00:00+00:00",
        "required_view_types": list(REQUIRED_CHARACTER_VIEW_TYPES),
        "schema_version": "character-publication.v1",
        "template_hash": hashlib.sha256(b"character-prompt-v1").hexdigest(),
        "template_version": "character-prompt-v1",
    }
    publication_hash = hashlib.sha256(encode_json(publication_snapshot).encode()).hexdigest()
    conn.execute(
        """
        INSERT INTO character_versions (
            id, persona_id, version_number, status, source_asset_id,
            source_sha256, persona_snapshot_json, provider, model,
            generation_params_json, template_version, template_hash,
            required_view_types_json, published_by, published_at,
            publication_snapshot_json, publication_hash, created_by
        ) VALUES (
            ?, 'persona-1', 1, 'PUBLISHED', 'identity-source-asset', ?, ?,
            'fake_character', 'fake-character-v1', '{}',
            'character-prompt-v1', ?, ?, 'admin_1',
            '2026-08-15T00:00:00+00:00', ?, ?, 'admin_1'
        )
        """,
        (
            character_version_id,
            identity_source.sha256,
            encode_json({"name": "乡墅项目管理专家", "costume_description": "工程马甲"}),
            hashlib.sha256(b"character-prompt-v1").hexdigest(),
            encode_json(list(REQUIRED_CHARACTER_VIEW_TYPES)),
            encode_json(publication_snapshot),
            publication_hash,
        ),
    )
    for view_type in REQUIRED_CHARACTER_VIEW_TYPES:
        conn.execute(
            """
            INSERT INTO character_assets (
                id, character_version_id, asset_id, view_type,
                candidate_number, auto_quality_json, review_status,
                is_published_selection
            ) VALUES (?, ?, ?, ?, 1, '{}', 'APPROVED', 1)
            """,
            (
                f"candidate-{view_type.lower()}",
                character_version_id,
                approved_asset_by_view[view_type],
                view_type,
            ),
        )
    conn.execute(
        """
        INSERT INTO project_main_characters (
            project_id, character_id, version_id, character_version_id,
            selected_by_user_id
        ) VALUES (?, NULL, 'main-character-selection-v1', ?, 'employee_1')
        """,
        (project_id, character_version_id),
    )
    conn.commit()
    return SeededReferenceContext(
        project_id=project_id,
        source_selection_id=source_selection_id,
        character_version_id=character_version_id,
        approved_asset_by_view=approved_asset_by_view,
        publication_hash=publication_hash,
    )


def context(db_path: Path) -> SeededReferenceContext:
    with connect_database(db_path) as conn:
        publication_hash = conn.execute(
            "SELECT publication_hash FROM character_versions WHERE id = 'character-version-v1'"
        ).fetchone()[0]
    return SeededReferenceContext(
        project_id="project-owned",
        source_selection_id="source-selection-v1",
        character_version_id="character-version-v1",
        approved_asset_by_view={
            view_type: f"approved-{view_type.lower()}"
            for view_type in REQUIRED_CHARACTER_VIEW_TYPES
        },
        publication_hash=str(publication_hash),
    )


@pytest.mark.parametrize(
    ("features", "expected"),
    [
        (SourceFrameFeatures("FRONT", "CLOSE_UP", True, "FACE_ONLY"), "FRONT_HALF"),
        (SourceFrameFeatures("FRONT", "FULL_BODY", True, "FULL_BODY"), "FRONT_FULL"),
        (SourceFrameFeatures("LEFT_45", "HALF_BODY", True, "UPPER_BODY"), "LEFT_45"),
        (SourceFrameFeatures("RIGHT_45", "HALF_BODY", True, "UPPER_BODY"), "RIGHT_45"),
        (SourceFrameFeatures("LEFT_SIDE", "FULL_BODY", False, "PARTIAL"), "LEFT_SIDE"),
        (SourceFrameFeatures("RIGHT_SIDE", "FULL_BODY", False, "PARTIAL"), "RIGHT_SIDE"),
    ],
)
def test_recommended_body_view_is_deterministic(
    features: SourceFrameFeatures,
    expected: str,
) -> None:
    assert recommended_body_view(features) == expected


def test_default_selection_freezes_recommendation_and_reopens_idempotently(
    client: TestClient,
    db_path: Path,
) -> None:
    seeded = context(db_path)
    created = client.post(
        "/api/projects/project-owned/character-reference-selection",
        headers=headers("employee_1"),
        json={},
    )
    replay = client.post(
        "/api/projects/project-owned/character-reference-selection",
        headers=headers("employee_1"),
        json={},
    )
    latest = client.get(
        "/api/projects/project-owned/character-reference-selection/latest",
        headers=headers("employee_1"),
    )

    assert created.status_code == 201
    body = created.json()
    expected = [
        seeded.approved_asset_by_view["RIGHT_45"],
        seeded.approved_asset_by_view["FRONT_FACE"],
    ]
    assert body["recommended_asset_ids_json"] == expected
    assert body["selected_asset_ids_json"] == expected
    assert body["source_frame_version_id"] == seeded.source_selection_id
    assert body["character_version_id"] == seeded.character_version_id
    assert body["character_version_snapshot_json"]["publication_hash"] == seeded.publication_hash
    assert body["recommendation_reason_json"]["body_view_type"] == "RIGHT_45"
    assert set(body["recommendation_reason_json"]["candidate_asset_ids"]) == set(
        seeded.approved_asset_by_view.values()
    )
    assert replay.status_code == 201
    assert replay.json()["id"] == body["id"]
    assert latest.status_code == 200
    assert latest.json()["id"] == body["id"]
    with connect_database(db_path) as conn:
        selection_count = conn.execute(
            "SELECT COUNT(*) FROM character_reference_selections WHERE project_id = ?",
            (seeded.project_id,),
        ).fetchone()[0]
        audit_count = conn.execute(
            """
            SELECT COUNT(*) FROM audit_logs
            WHERE action = 'character_reference.select' AND entity_id = ?
            """,
            (body["id"],),
        ).fetchone()[0]
    assert selection_count == 1
    assert audit_count == 1


def test_employee_can_choose_one_to_four_published_assets_and_roles_fail_closed(
    client: TestClient,
    db_path: Path,
) -> None:
    seeded = context(db_path)
    selected_ids = [
        seeded.approved_asset_by_view["FRONT_FACE"],
        seeded.approved_asset_by_view["LEFT_SIDE"],
        seeded.approved_asset_by_view["FRONT_FULL"],
        seeded.approved_asset_by_view["RIGHT_45"],
    ]
    created = client.post(
        "/api/projects/project-owned/character-reference-selection",
        headers=headers("employee_1"),
        json={"selected_asset_ids": selected_ids},
    )
    foreign = client.post(
        "/api/projects/project-owned/character-reference-selection",
        headers=headers("employee_1"),
        json={"selected_asset_ids": ["authorization-asset"]},
    )
    too_many = client.post(
        "/api/projects/project-owned/character-reference-selection",
        headers=headers("employee_1"),
        json={"selected_asset_ids": list(seeded.approved_asset_by_view.values())[:5]},
    )
    other_employee = client.post(
        "/api/projects/project-owned/character-reference-selection",
        headers=headers("employee_2"),
        json={},
    )
    auditor = client.post(
        "/api/projects/project-owned/character-reference-selection",
        headers=headers("auditor_1"),
        json={},
    )

    assert created.status_code == 201
    assert created.json()["selected_asset_ids_json"] == selected_ids
    assert foreign.status_code == 422
    assert foreign.json()["detail"]["code"] == "CHARACTER_REFERENCE_ASSET_INVALID"
    assert too_many.status_code == 422
    assert other_employee.status_code == 403
    assert auditor.status_code == 403


def test_selection_rejects_missing_features_stale_source_and_unavailable_character(
    client: TestClient,
    db_path: Path,
) -> None:
    with connect_database(db_path) as conn:
        selection = conn.execute(
            "SELECT payload_json FROM versions WHERE id = 'source-selection-v1'"
        ).fetchone()
        payload = json.loads(str(selection["payload_json"]))
        features = payload.pop("character_features")
        conn.execute(
            "UPDATE versions SET payload_json = ? WHERE id = 'source-selection-v1'",
            (encode_json(payload),),
        )
        conn.commit()
    missing_features = client.post(
        "/api/projects/project-owned/character-reference-selection",
        headers=headers("employee_1"),
        json={},
    )
    assert missing_features.status_code == 409
    assert missing_features.json()["detail"]["code"] == "SOURCE_FRAME_FEATURES_REQUIRED"

    with connect_database(db_path) as conn:
        payload["character_features"] = features
        conn.execute(
            "UPDATE versions SET payload_json = ? WHERE id = 'source-selection-v1'",
            (encode_json(payload),),
        )
        conn.execute(
            """
            INSERT INTO versions (
                id, project_id, asset_id, kind, version_number,
                payload_json, created_by_user_id
            ) VALUES (
                'source-candidates-v2', 'project-owned', 'source-frame-asset',
                'source_frame_candidates', 2, '{"candidates": []}', 'employee_1'
            )
            """
        )
        conn.commit()
    stale = client.post(
        "/api/projects/project-owned/character-reference-selection",
        headers=headers("employee_1"),
        json={},
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "SOURCE_FRAME_SELECTION_STALE"

    with connect_database(db_path) as conn:
        conn.execute("DELETE FROM versions WHERE id = 'source-candidates-v2'")
        conn.execute(
            "UPDATE character_versions SET status = 'ARCHIVED' WHERE id = 'character-version-v1'"
        )
        conn.commit()
    archived = client.post(
        "/api/projects/project-owned/character-reference-selection",
        headers=headers("employee_1"),
        json={},
    )
    assert archived.status_code == 409
    assert archived.json()["detail"]["code"] == "CHARACTER_VERSION_NOT_PUBLISHED"


def test_first_frame_generation_uses_frozen_reference_selection_not_new_persona_version(
    client: TestClient,
    db_path: Path,
    provider: RecordingImageProvider,
) -> None:
    seeded = context(db_path)
    selected_ids = [
        seeded.approved_asset_by_view["LEFT_SIDE"],
        seeded.approved_asset_by_view["FRONT_FACE"],
        seeded.approved_asset_by_view["FRONT_FULL"],
    ]
    selection = client.post(
        "/api/projects/project-owned/character-reference-selection",
        headers=headers("employee_1"),
        json={"selected_asset_ids": selected_ids},
    )
    assert selection.status_code == 201

    with connect_database(db_path) as conn:
        conn.execute(
            """
            INSERT INTO character_versions (
                id, persona_id, version_number, status, source_asset_id,
                source_sha256, persona_snapshot_json, required_view_types_json,
                publication_snapshot_json, publication_hash, created_by
            ) VALUES (
                'character-version-v2', 'persona-1', 2, 'PUBLISHED',
                'identity-source-asset', 'v2-source', '{"name": "新版本"}', '[]',
                '{}', ?, 'admin_1'
            )
            """,
            (hashlib.sha256(b"{}").hexdigest(),),
        )
        conn.commit()

    generated = client.post(
        "/api/projects/project-owned/first-frames/generate",
        headers=headers("employee_1"),
        json={"model": "nano-banana-pro-2k", "quantity": 1},
    )

    assert generated.status_code == 200
    body = generated.json()["payload"]
    assert body["character_reference_selection_id"] == selection.json()["id"]
    assert body["character_version_id"] == seeded.character_version_id
    assert body["character_reference_asset_ids"] == selected_ids
    assert body["character_snapshot"]["publication_hash"] == seeded.publication_hash
    assert provider.calls[0]["character_reference_images"] == [
        b"approved-content-LEFT_SIDE",
        b"approved-content-FRONT_FACE",
        b"approved-content-FRONT_FULL",
    ]

    with connect_database(db_path) as conn:
        conn.execute(
            """
            UPDATE project_main_characters
            SET character_version_id = 'character-version-v2'
            WHERE project_id = 'project-owned'
            """
        )
        conn.commit()
    stale = client.get(
        "/api/projects/project-owned/first-frames/latest",
        headers=headers("employee_1"),
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "FIRST_FRAME_CANDIDATES_STALE"
