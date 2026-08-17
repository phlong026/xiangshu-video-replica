"""Simple character upload flow (方案 A: 极简人物库).

Uploads a single authorization image, derives the seven standard views from a
deterministic local generator, records approval reviews, and publishes the
character version in one transaction so it immediately shows up in the
project's available character version list.
"""

from __future__ import annotations

import hashlib
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import HTTPException

from app.auth import CurrentUser
from app.character_asset_review import (
    CHARACTER_PUBLICATION_SCHEMA_VERSION,
    cleanup_publication_objects,
)
from app.character_identity import (
    CHARACTER_TEMPLATE_HASH,
    CHARACTER_TEMPLATE_VERSION,
    REQUIRED_CHARACTER_VIEW_TYPES,
    approved_character_asset_key,
    character_error,
    encode_json,
    generated_character_asset_key,
    identity_asset_key,
    persona_snapshot,
)
from app.character_image_generation import deterministic_png
from app.permissions import require_project_access, write_audit
from app.storage import StorageAdapter

SIMPLE_UPLOAD_MAX_BYTES = 10 * 1024 * 1024
SIMPLE_UPLOAD_ALLOWED_TYPES = {"image/png": ".png", "image/jpeg": ".jpg"}
SIMPLE_AUTHORIZATION_SCOPE = ["internal-short-video"]
SIMPLE_PERSONA_USAGE_SCOPE = ["internal-short-video"]
SIMPLE_GENERATION_MODE = "simple_upload"


@dataclass(frozen=True)
class SimpleCharacterCreationResult:
    identity_id: str
    persona_id: str
    character_version_id: str
    publication_hash: str


def create_simple_character(
    conn: sqlite3.Connection,
    *,
    actor: CurrentUser,
    project_id: str,
    storage: StorageAdapter,
    source_content: bytes,
    source_content_type: str,
    display_name: str,
    persona_name: str,
) -> SimpleCharacterCreationResult:
    """Create and publish a character from a single uploaded image.

    The uploaded image acts as both the authorization proof and the source
    asset (self-authorization), the seven views are produced by the local
    deterministic generator, every view is auto-approved, and the version is
    published in the same transaction.
    """
    require_project_access(
        conn,
        actor=actor,
        project_id=project_id,
        action="simple_character.create",
    )
    _validate_source(source_content, source_content_type, display_name)

    now_iso = _utc_now_iso()
    identity_id = str(uuid.uuid4())
    persona_id = str(uuid.uuid4())
    version_id = str(uuid.uuid4())

    # Track every object written during the transaction so a rollback can
    # remove orphaned storage objects, mirroring the publication flow.
    attempted_keys: list[str] = []

    try:
        conn.execute("BEGIN IMMEDIATE")

        source_asset_id = _store_source_asset(
            conn,
            storage=storage,
            actor=actor,
            identity_id=identity_id,
            content=source_content,
            content_type=source_content_type,
            attempted_keys=attempted_keys,
        )
        _insert_identity(
            conn,
            actor=actor,
            identity_id=identity_id,
            display_name=display_name,
            source_asset_id=source_asset_id,
            now_iso=now_iso,
        )
        _insert_persona(
            conn,
            actor=actor,
            persona_id=persona_id,
            identity_id=identity_id,
            persona_name=persona_name,
            now_iso=now_iso,
        )
        # Build the snapshot from the stored row so it stays structurally
        # identical to the traditional flow's persona_snapshot contract.
        persona_row = conn.execute(
            "SELECT * FROM character_personas WHERE id = ?",
            (persona_id,),
        ).fetchone()
        if persona_row is None:  # pragma: no cover - inserted above
            raise character_error(
                500,
                "SIMPLE_CHARACTER_PERSONA_MISSING",
                "人设记录写入失败，请重试。",
            )
        persona_snapshot_json = encode_json(persona_snapshot(persona_row))
        _insert_version(
            conn,
            actor=actor,
            version_id=version_id,
            persona_id=persona_id,
            persona_snapshot_json=persona_snapshot_json,
            now_iso=now_iso,
        )

        views = _generate_and_approve_views(
            conn,
            storage=storage,
            actor=actor,
            version_id=version_id,
            persona_id=persona_id,
            now_iso=now_iso,
            attempted_keys=attempted_keys,
        )
        publication_hash = _publish_views(
            conn,
            storage=storage,
            actor=actor,
            version_id=version_id,
            persona_id=persona_id,
            persona_snapshot_json=persona_snapshot_json,
            views=views,
            now_iso=now_iso,
            attempted_keys=attempted_keys,
        )

        write_audit(
            conn,
            actor=actor,
            action="simple_character.create",
            entity_type="character_version",
            entity_id=version_id,
            metadata={
                "identity_id": identity_id,
                "persona_id": persona_id,
                "project_id": project_id,
                "publication_hash": publication_hash,
            },
        )
        conn.commit()
    except HTTPException:
        conn.rollback()
        cleanup_publication_objects(storage, attempted_keys)
        raise
    except Exception as exc:  # pragma: no cover - defensive guard
        conn.rollback()
        cleanup_publication_objects(storage, attempted_keys)
        raise character_error(
            500,
            "SIMPLE_CHARACTER_CREATION_FAILED",
            "一键生成人物失败，请稍后重试。",
        ) from exc

    return SimpleCharacterCreationResult(
        identity_id=identity_id,
        persona_id=persona_id,
        character_version_id=version_id,
        publication_hash=publication_hash,
    )


def _validate_source(content: bytes, content_type: str, display_name: str) -> None:
    name = display_name.strip()
    if not name:
        raise character_error(422, "SIMPLE_CHARACTER_NAME_REQUIRED", "请填写人物名称。")
    if not content:
        raise character_error(422, "SIMPLE_CHARACTER_IMAGE_REQUIRED", "请上传人物授权图片。")
    if len(content) > SIMPLE_UPLOAD_MAX_BYTES:
        raise character_error(
            422,
            "SIMPLE_CHARACTER_IMAGE_TOO_LARGE",
            "人物授权图片超过 10MB 限制。",
        )
    normalized_type = content_type.split(";", 1)[0].strip().lower()
    if normalized_type not in SIMPLE_UPLOAD_ALLOWED_TYPES:
        raise character_error(
            422,
            "SIMPLE_CHARACTER_IMAGE_TYPE_UNSUPPORTED",
            "仅支持 PNG 或 JPEG 图片。",
        )


def _store_source_asset(
    conn: sqlite3.Connection,
    *,
    storage: StorageAdapter,
    actor: CurrentUser,
    identity_id: str,
    content: bytes,
    content_type: str,
    attempted_keys: list[str],
) -> str:
    asset_id = str(uuid.uuid4())
    extension = SIMPLE_UPLOAD_ALLOWED_TYPES[content_type.split(";", 1)[0].strip().lower()]
    key = identity_asset_key(
        owner_user_id=actor.id,
        identity_id=identity_id,
        purpose="source",
        asset_id=asset_id,
        extension=extension,
    )
    stored = storage.put_object(key, content, content_type=content_type)
    attempted_keys.append(stored.key)
    conn.execute(
        """
        INSERT INTO assets (
            id, project_id, kind, storage_uri, sha256, size_bytes,
            content_type, created_by_user_id, metadata_json
        ) VALUES (?, NULL, 'character_source_image', ?, ?, ?, ?, ?, ?)
        """,
        (
            asset_id,
            stored.uri,
            stored.sha256,
            stored.size,
            stored.content_type,
            actor.id,
            encode_json(
                {
                    "identity_id": identity_id,
                    "object_key": stored.key,
                    "purpose": "simple_upload_source",
                    "upload_status": "UPLOADED",
                }
            ),
        ),
    )
    return asset_id


def _insert_identity(
    conn: sqlite3.Connection,
    *,
    actor: CurrentUser,
    identity_id: str,
    display_name: str,
    source_asset_id: str,
    now_iso: str,
) -> None:
    conn.execute(
        """
        INSERT INTO person_identities (
            id, owner_user_id, display_name, authorization_status,
            authorization_asset_id, authorization_scope, authorization_expires_at,
            source_asset_id, source_quality_status, status, created_by,
            created_at, updated_at
        ) VALUES (?, ?, ?, 'AUTHORIZED', ?, ?, NULL, ?, 'PASSED', 'ACTIVE', ?, ?, ?)
        """,
        (
            identity_id,
            actor.id,
            display_name.strip(),
            source_asset_id,
            encode_json(SIMPLE_AUTHORIZATION_SCOPE),
            source_asset_id,
            actor.id,
            now_iso,
            now_iso,
        ),
    )


def _insert_persona(
    conn: sqlite3.Connection,
    *,
    actor: CurrentUser,
    persona_id: str,
    identity_id: str,
    persona_name: str,
    now_iso: str,
) -> None:
    conn.execute(
        """
        INSERT INTO character_personas (
            id, identity_id, name, usage_scope_json, created_by,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            persona_id,
            identity_id,
            persona_name.strip(),
            encode_json(SIMPLE_PERSONA_USAGE_SCOPE),
            actor.id,
            now_iso,
            now_iso,
        ),
    )


def _insert_version(
    conn: sqlite3.Connection,
    *,
    actor: CurrentUser,
    version_id: str,
    persona_id: str,
    persona_snapshot_json: str,
    now_iso: str,
) -> None:
    conn.execute(
        """
        INSERT INTO character_versions (
            id, persona_id, version_number, status,
            persona_snapshot_json, provider, model, generation_params_json,
            template_version, template_hash, required_view_types_json,
            created_by, created_at, generation_mode
        ) VALUES (?, ?, 1, 'REVIEWING', ?, 'local_simple_upload',
                  'deterministic-v1', '{}', ?, ?, ?, ?, ?, 'simple_upload')
        """,
        (
            version_id,
            persona_id,
            persona_snapshot_json,
            CHARACTER_TEMPLATE_VERSION,
            CHARACTER_TEMPLATE_HASH,
            encode_json(list(REQUIRED_CHARACTER_VIEW_TYPES)),
            actor.id,
            now_iso,
        ),
    )


@dataclass(frozen=True)
class _ApprovedView:
    view_type: str
    character_asset_id: str
    generated_asset_id: str
    review_id: str
    content: bytes
    content_type: str
    sha256: str


def _generate_and_approve_views(
    conn: sqlite3.Connection,
    *,
    storage: StorageAdapter,
    actor: CurrentUser,
    version_id: str,
    persona_id: str,
    now_iso: str,
    attempted_keys: list[str],
) -> list[_ApprovedView]:
    views: list[_ApprovedView] = []
    for view_type in REQUIRED_CHARACTER_VIEW_TYPES:
        character_asset_id = str(uuid.uuid4())
        generated_asset_id = str(uuid.uuid4())
        review_id = str(uuid.uuid4())
        content = deterministic_png(
            f"{version_id}:{view_type}".encode(), width=1024, height=1536
        )
        generated_key = generated_character_asset_key(
            owner_user_id=actor.id,
            persona_id=persona_id,
            version_id=version_id,
            view_type=view_type,
            asset_id=generated_asset_id,
        )
        stored = storage.put_object(generated_key, content, content_type="image/png")
        attempted_keys.append(stored.key)
        conn.execute(
            """
            INSERT INTO assets (
                id, project_id, kind, storage_uri, sha256, size_bytes,
                content_type, created_by_user_id, metadata_json
            ) VALUES (?, NULL, 'character_generated_image', ?, ?, ?, ?, ?, ?)
            """,
            (
                generated_asset_id,
                stored.uri,
                stored.sha256,
                stored.size,
                stored.content_type,
                actor.id,
                encode_json(
                    {
                        "character_version_id": version_id,
                        "generation_mode": SIMPLE_GENERATION_MODE,
                        "view_type": view_type,
                    }
                ),
            ),
        )
        conn.execute(
            """
            INSERT INTO character_assets (
                id, character_version_id, asset_id, view_type, candidate_number,
                auto_quality_json, review_status, is_published_selection, created_at
            ) VALUES (?, ?, ?, ?, 1, '{}', 'APPROVED', 0, ?)
            """,
            (character_asset_id, version_id, generated_asset_id, view_type, now_iso),
        )
        conn.execute(
            """
            INSERT INTO character_asset_reviews (
                id, character_asset_id, reviewer_user_id, decision,
                issue_codes_json, comment, created_at
            ) VALUES (?, ?, ?, 'APPROVED', '[]', ?, ?)
            """,
            (
                review_id,
                character_asset_id,
                actor.id,
                "Auto-approved by simple upload flow.",
                now_iso,
            ),
        )
        views.append(
            _ApprovedView(
                view_type=view_type,
                character_asset_id=character_asset_id,
                generated_asset_id=generated_asset_id,
                review_id=review_id,
                content=content,
                content_type=stored.content_type,
                sha256=stored.sha256,
            )
        )
    return views


def _publish_views(
    conn: sqlite3.Connection,
    *,
    storage: StorageAdapter,
    actor: CurrentUser,
    version_id: str,
    persona_id: str,
    persona_snapshot_json: str,
    views: list[_ApprovedView],
    now_iso: str,
    attempted_keys: list[str],
) -> str:
    assets_by_view: dict[str, dict[str, object]] = {}
    for view in views:
        approved_asset_id = str(uuid.uuid4())
        approved_key = approved_character_asset_key(
            owner_user_id=actor.id,
            persona_id=persona_id,
            version_id=version_id,
            view_type=view.view_type,
            asset_id=approved_asset_id,
        )
        stored = storage.put_object(approved_key, view.content, content_type=view.content_type)
        attempted_keys.append(stored.key)
        conn.execute(
            """
            INSERT INTO assets (
                id, project_id, kind, storage_uri, sha256, size_bytes,
                content_type, created_by_user_id, metadata_json
            ) VALUES (?, NULL, 'character_approved_image', ?, ?, ?, ?, ?, ?)
            """,
            (
                approved_asset_id,
                stored.uri,
                stored.sha256,
                stored.size,
                stored.content_type,
                actor.id,
                encode_json(
                    {
                        "character_asset_id": view.character_asset_id,
                        "character_version_id": version_id,
                        "generated_asset_id": view.generated_asset_id,
                        "view_type": view.view_type,
                    }
                ),
            ),
        )
        updated = conn.execute(
            """
            UPDATE character_assets
            SET asset_id = ?, is_published_selection = 1
            WHERE id = ? AND character_version_id = ?
              AND asset_id = ? AND review_status = 'APPROVED'
            """,
            (
                approved_asset_id,
                view.character_asset_id,
                version_id,
                view.generated_asset_id,
            ),
        )
        if updated.rowcount != 1:
            raise character_error(
                409,
                "SIMPLE_CHARACTER_ASSET_CHANGED",
                "生成资产在发布前发生变化，请重试。",
            )
        assets_by_view[view.view_type] = {
            "approved_asset_id": approved_asset_id,
            "character_asset_id": view.character_asset_id,
            "content_type": stored.content_type,
            "generated_asset_id": view.generated_asset_id,
            "review_id": view.review_id,
            "sha256": stored.sha256,
            "size_bytes": stored.size,
            "storage_uri": stored.uri,
        }

    snapshot: dict[str, object] = {
        "assets_by_view": assets_by_view,
        "character_version_id": version_id,
        "persona_snapshot_hash": hashlib.sha256(persona_snapshot_json.encode()).hexdigest(),
        "published_at": now_iso,
        "required_view_types": list(REQUIRED_CHARACTER_VIEW_TYPES),
        "schema_version": CHARACTER_PUBLICATION_SCHEMA_VERSION,
        "template_hash": CHARACTER_TEMPLATE_HASH,
        "template_version": CHARACTER_TEMPLATE_VERSION,
    }
    snapshot_json = encode_json(snapshot)
    publication_hash = hashlib.sha256(snapshot_json.encode()).hexdigest()
    updated_version = conn.execute(
        """
        UPDATE character_versions
        SET status = 'PUBLISHED', published_by = ?, published_at = ?,
            publication_snapshot_json = ?, publication_hash = ?
        WHERE id = ? AND status = 'REVIEWING'
        """,
        (actor.id, now_iso, snapshot_json, publication_hash, version_id),
    )
    if updated_version.rowcount != 1:
        raise character_error(
            409,
            "SIMPLE_CHARACTER_VERSION_NOT_REVIEWING",
            "角色版本状态异常，无法发布。",
        )
    return publication_hash


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()
