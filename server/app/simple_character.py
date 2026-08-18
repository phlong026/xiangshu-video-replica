"""Simple character upload flow (方案 A: 极简人物库).

Uploads a single authorization image, derives the seven standard views from a
deterministic local generator, records approval reviews, and publishes the
character version in one transaction so it immediately shows up in the
project's available character version list.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import struct
import uuid
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast

from fastapi import HTTPException

from app.auth import CurrentUser
from app.character_asset_review import (
    CHARACTER_PUBLICATION_SCHEMA_VERSION,
    cleanup_publication_objects,
)
from app.character_contracts import PersonIdentity, RequiredCharacterViewType
from app.character_identity import (
    CHARACTER_TEMPLATE_HASH,
    CHARACTER_TEMPLATE_VERSION,
    REQUIRED_CHARACTER_VIEW_TYPES,
    approved_character_asset_key,
    character_error,
    encode_json,
    generated_character_asset_key,
    get_person_identity,
    identity_asset_key,
    persona_snapshot,
    read_identity_row,
    required_text,
    validate_key_segment,
)
from app.character_image_generation import deterministic_png, png_chunk
from app.first_frames import FirstFrameModel, ImageInput, ImageProvider, ImageProviderFailed
from app.media import storage_key_from_uri
from app.permissions import require_project_access, write_audit
from app.storage import StorageAdapter, StorageBackendUnavailable

logger = logging.getLogger(__name__)

SIMPLE_UPLOAD_MAX_BYTES = 10 * 1024 * 1024
SIMPLE_UPLOAD_ALLOWED_TYPES = {"image/png": ".png", "image/jpeg": ".jpg"}
SIMPLE_AUTHORIZATION_SCOPE = ["internal-short-video"]
SIMPLE_PERSONA_USAGE_SCOPE = ["internal-short-video"]
SIMPLE_GENERATION_MODE = "simple_upload"

# Single-sheet five-view generation: the uploaded photo is the identity
# reference and the provider renders one wide landscape contact sheet with
# five views of the SAME person (identity-preserve prompt).
SIMPLE_CONTACT_SHEET_MODEL: FirstFrameModel = "gpt-image-2"
SIMPLE_CONTACT_SHEET_EXTENSIONS = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}
SIMPLE_CONTACT_SHEET_PROMPT = """\
Use case: identity-preserve
Asset type: production-ready photorealistic character reference board for
future image creation

Input image role: the attached photo is the primary and only authoritative
identity reference. Preserve this person's recognizable face, visible age,
hairstyle, headwear, jewelry, makeup level, and clothing exactly as shown.

Primary request: re-create the same person from the attached photo in a clean
five-panel multi-view layout. One wide landscape contact sheet with five
clean panels:
1) left tall panel: full-body straight front view;
2) second tall panel: full-body three-quarter view facing slightly to
   camera-left;
3) third tall panel: full-body left profile view;
4) upper-right panel: close-up straight-front head-and-shoulders portrait;
5) lower-right panel: close-up three-quarter head-and-shoulders portrait
   matching the full-body three-quarter angle.
The first three panels occupy roughly 72% of the width as equal tall vertical
columns. The rightmost roughly 28% is split into two equal stacked portrait
panels. Use thin clean white dividers and a narrow white outer border.

Identity and styling invariants: the exact same person in every panel;
preserve the facial proportions and recognizable appearance from the attached
photo, including eye shape, nose, lips, skin tone, hairstyle and hair length,
headwear, visible jewelry, makeup level, and clothing. Keep face, eye shape,
hair, accessories, body build, and clothing identical across all five panels.
Neutral relaxed expression with a very subtle friendly softness; eyes level
when visible.

Conservative full-body continuation: where the attached photo does not show
the lower body, extend the visible outfit simply and neutrally with plain
trousers and plain low-profile closed shoes. No belt, bag, visible brand,
pattern, or extra accessories. Keep body proportions realistic and
consistent. Arms relaxed naturally at the sides; feet parallel in the front
view.

Scene/backdrop: uniform seamless light-gray studio backdrop with a subtle
neutral center glow, exactly consistent across all panels.
Style/medium: high-fidelity natural studio photography, realistic skin,
hair, fabric, jewelry, hands, and shoes; minimal retouching; no illustration,
no 3D render, no fashion-campaign drama.
Composition/framing: full bodies completely visible from head to soles with
generous safe margins in the three full-body panels; the two right panels
crop at upper chest; identical camera height and focal length within
corresponding panel types; no overlap between panels.
Lighting/mood: soft even studio illumination, neutral white balance, mild
floor grounding shadow only in full-body panels, consistent exposure
everywhere.
Constraints: exactly five panels and exactly five appearances of the same
person; layout fidelity is critical; identity fidelity to the attached photo
is the highest subject priority; no text, labels, arrows, captions, logos,
watermark, props, furniture, room background, or extra people.
Avoid: face drift; different people; altered eye size; changed hairstyle;
missing or changed accessories; different clothing; glamour makeup;
exaggerated beauty filter; cropped head or shoes; malformed hands; extra
limbs; duplicated jewelry; busy background.
"""


@dataclass(frozen=True)
class SimpleCharacterView:
    view_type: RequiredCharacterViewType
    asset_id: str


@dataclass(frozen=True)
class SimpleCharacterCreationResult:
    identity_id: str
    persona_id: str
    character_version_id: str
    publication_hash: str
    contact_sheet_asset_id: str
    views: tuple[SimpleCharacterView, ...]


@dataclass(frozen=True)
class SimpleLibraryEntry:
    """One character in the simplified library with its published seven views."""

    identity_id: str
    display_name: str
    owner_user_id: str | None
    status: str
    contact_sheet_asset_id: str | None
    views: tuple[SimpleCharacterView, ...]


def create_simple_character(
    conn: sqlite3.Connection,
    *,
    actor: CurrentUser,
    project_id: str | None,
    storage: StorageAdapter,
    source_content: bytes,
    source_content_type: str,
    display_name: str,
    persona_name: str,
    image_provider: ImageProvider | None = None,
) -> SimpleCharacterCreationResult:
    """Create and publish a character from a single uploaded image.

    The uploaded image acts as both the authorization proof and the source
    asset (self-authorization). A single five-view contact sheet is rendered
    from the photo (image provider when configured, local placeholder as a
    fallback), the seven per-view assets are produced by the local
    deterministic generator, every view is auto-approved, and the version is
    published in the same transaction.

    ``project_id`` is only an access-control/audit hint: the global character
    library page passes ``None`` (no project context), while the in-project
    flow passes the owning project so employee access can be verified.
    """
    if project_id is not None:
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

    # Provider-backed generation can take tens of seconds, so render the
    # contact sheet BEFORE opening the write transaction to avoid holding
    # the SQLite lock for the whole image generation.
    normalized_content_type = source_content_type.split(";", 1)[0].strip().lower()
    contact_content, contact_content_type, contact_source = _generate_contact_sheet_content(
        image_provider,
        source_content=source_content,
        source_content_type=normalized_content_type,
        version_id=version_id,
    )

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
            contact_content=contact_content,
            contact_content_type=contact_content_type,
        )
        contact_sheet_asset_id = _store_contact_sheet_asset(
            conn,
            storage=storage,
            actor=actor,
            identity_id=identity_id,
            version_id=version_id,
            content=contact_content,
            content_type=contact_content_type,
            generation_source=contact_source,
            attempted_keys=attempted_keys,
        )
        publication_hash, assets_by_view = _publish_views(
            conn,
            storage=storage,
            actor=actor,
            version_id=version_id,
            persona_id=persona_id,
            persona_snapshot_json=persona_snapshot_json,
            views=views,
            contact_sheet_asset_id=contact_sheet_asset_id,
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
                "publication_hash": publication_hash,
                "contact_sheet_asset_id": contact_sheet_asset_id,
                **({"project_id": project_id} if project_id else {}),
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
        contact_sheet_asset_id=contact_sheet_asset_id,
        views=tuple(
            SimpleCharacterView(
                view_type=view_type,
                asset_id=str(assets_by_view[view_type]["approved_asset_id"]),
            )
            for view_type in REQUIRED_CHARACTER_VIEW_TYPES
            if view_type in assets_by_view
        ),
    )


def list_simple_library(
    conn: sqlite3.Connection,
    *,
    actor: CurrentUser,
) -> list[SimpleLibraryEntry]:
    """List characters with the published seven-view assets for previews.

    Every authenticated role can read the library (mirroring the existing
    identity list), and for each identity only the latest published version's
    approved selection is returned so the preview always matches what video
    generation would actually consume.
    """
    del actor  # visibility intentionally matches GET /api/person-identities
    rows = conn.execute(
        """
        SELECT identity.id AS identity_id,
               identity.display_name AS display_name,
               identity.owner_user_id AS owner_user_id,
               identity.status AS identity_status,
               version.id AS version_id,
               version.published_at AS published_at,
               version.publication_snapshot_json AS snapshot_json,
               view.view_type AS view_type,
               view.asset_id AS asset_id
        FROM person_identities AS identity
        LEFT JOIN character_personas AS persona ON persona.identity_id = identity.id
        LEFT JOIN character_versions AS version
          ON version.persona_id = persona.id
         AND version.status = 'PUBLISHED'
        LEFT JOIN character_assets AS view
          ON view.character_version_id = version.id
         AND view.review_status = 'APPROVED'
         AND view.is_published_selection = 1
        ORDER BY identity.created_at DESC, identity.id,
                 version.published_at DESC, view.view_type
        """
    ).fetchall()

    entries: list[SimpleLibraryEntry] = []
    for identity_id, identity_rows in _group_by_identity(rows).items():
        usable = [row for row in identity_rows if row["version_id"] is not None]
        latest = usable[0] if usable else None
        views = tuple(
            SimpleCharacterView(
                view_type=cast(RequiredCharacterViewType, str(row["view_type"])),
                asset_id=str(row["asset_id"]),
            )
            for row in identity_rows
            if latest is not None
            and row["version_id"] == latest["version_id"]
            and row["asset_id"] is not None
        )
        entries.append(
            SimpleLibraryEntry(
                identity_id=identity_id,
                display_name=str(identity_rows[0]["display_name"]),
                owner_user_id=(
                    None
                    if identity_rows[0]["owner_user_id"] is None
                    else str(identity_rows[0]["owner_user_id"])
                ),
                status=str(identity_rows[0]["identity_status"]),
                contact_sheet_asset_id=_snapshot_contact_sheet_asset_id(latest),
                views=views,
            )
        )
    return entries


def _snapshot_contact_sheet_asset_id(row: sqlite3.Row | None) -> str | None:
    """Read ``contact_sheet_asset_id`` from a publication snapshot.

    Versions published before the contact sheet feature have no such field,
    so ``None`` (and the seven-grid fallback in the UI) is a valid result.
    """
    if row is None or row["snapshot_json"] is None:
        return None
    try:
        snapshot = json.loads(str(row["snapshot_json"]))
    except json.JSONDecodeError:
        return None
    if not isinstance(snapshot, dict):
        return None
    value = snapshot.get("contact_sheet_asset_id")
    return value if isinstance(value, str) and value else None


def _group_by_identity(rows: list[sqlite3.Row]) -> dict[str, list[sqlite3.Row]]:
    grouped: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        grouped.setdefault(str(row["identity_id"]), []).append(row)
    return grouped


def rename_simple_character_identity(
    conn: sqlite3.Connection,
    *,
    actor: CurrentUser,
    identity_id: str,
    display_name: str,
) -> PersonIdentity:
    """Rename an identity from the simplified character library page.

    Unlike the admin-only ``update_person_identity``, this endpoint only
    touches ``display_name`` and allows the identity owner (or an admin);
    renames must never widen access to authorization or source assets.
    """
    row = read_identity_row(conn, identity_id)
    if actor.role != "admin" and str(row["owner_user_id"]) != actor.id:
        raise character_error(
            403,
            "IDENTITY_RENAME_FORBIDDEN",
            "只有创建者或管理员可以修改人物名称。",
        )
    if str(row["status"]) == "ARCHIVED":
        raise character_error(409, "IDENTITY_ARCHIVED", "已归档人物身份不能修改。")
    clean_name = required_text(
        display_name,
        "IDENTITY_NAME_REQUIRED",
        "人物显示名不能为空。",
    )
    with conn:
        conn.execute(
            """
            UPDATE person_identities
            SET display_name = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (clean_name, identity_id),
        )
    write_audit(
        conn,
        actor=actor,
        action="person_identity.rename",
        entity_type="person_identity",
        entity_id=identity_id,
        metadata={"display_name": clean_name},
    )
    return get_person_identity(conn, actor=actor, identity_id=identity_id)


@dataclass(frozen=True)
class SimpleCharacterRegenerationResult:
    identity_id: str
    persona_id: str
    character_version_id: str
    previous_version_id: str
    version_number: int
    publication_hash: str
    contact_sheet_asset_id: str
    views: tuple[SimpleCharacterView, ...]


def regenerate_simple_character_contact_sheet(
    conn: sqlite3.Connection,
    *,
    actor: CurrentUser,
    identity_id: str,
    storage: StorageAdapter,
    image_provider: ImageProvider | None = None,
) -> SimpleCharacterRegenerationResult:
    """Re-run the single-photo five-view generation and publish a new version.

    The character library's “重新生成多视图” action: read the identity's
    original source photo, re-render the contact sheet with the same
    identity-preserve prompt, and publish it as the next version under the
    same persona. The previously published version is left untouched so
    projects already bound to it keep working (their reference selections
    stay valid), while the library preview switches to the new version
    immediately because it always shows the latest published version.
    """
    identity = read_identity_row(conn, identity_id)
    if actor.role != "admin" and str(identity["owner_user_id"]) != actor.id:
        raise character_error(
            403,
            "IDENTITY_REGENERATE_FORBIDDEN",
            "只有创建者或管理员可以重新生成多视图。",
        )
    if str(identity["status"]) == "ARCHIVED":
        raise character_error(409, "IDENTITY_ARCHIVED", "已归档人物身份不能重新生成。")

    source_asset_id = identity["source_asset_id"]
    source_asset = (
        conn.execute(
            "SELECT storage_uri, content_type FROM assets WHERE id = ?",
            (str(source_asset_id),),
        ).fetchone()
        if source_asset_id
        else None
    )
    if source_asset is None:
        raise character_error(
            409,
            "SIMPLE_CHARACTER_SOURCE_MISSING",
            "人物缺少原始授权照片，无法重新生成多视图。",
        )

    persona = conn.execute(
        """
        SELECT id FROM character_personas WHERE identity_id = ?
        ORDER BY created_at DESC, id LIMIT 1
        """,
        (identity_id,),
    ).fetchone()
    if persona is None:
        raise character_error(
            409,
            "SIMPLE_CHARACTER_VERSION_MISSING",
            "人物没有可用的角色版本，请重新创建。",
        )
    persona_id = str(persona["id"])
    baseline = conn.execute(
        """
        SELECT id, persona_snapshot_json FROM character_versions
        WHERE persona_id = ?
        ORDER BY version_number DESC LIMIT 1
        """,
        (persona_id,),
    ).fetchone()
    if baseline is None:
        raise character_error(
            409,
            "SIMPLE_CHARACTER_VERSION_MISSING",
            "人物没有可用的角色版本，请重新创建。",
        )
    persona_snapshot_json = str(baseline["persona_snapshot_json"])
    previous_version_id = str(baseline["id"])

    # Read the photo and render the new sheet before opening the write
    # transaction (same policy as create_simple_character) so provider calls
    # never hold the SQLite lock.
    try:
        source_content = storage.get_object(storage_key_from_uri(str(source_asset["storage_uri"])))
    except (StorageBackendUnavailable, OSError, ValueError, KeyError) as exc:
        raise character_error(
            503,
            "SIMPLE_CHARACTER_SOURCE_UNAVAILABLE",
            "原始授权照片读取失败，请稍后重试。",
        ) from exc
    source_content_type = (
        str(source_asset["content_type"] or "image/png").split(";", 1)[0].strip().lower()
    )

    version_id = str(uuid.uuid4())
    now_iso = _utc_now_iso()
    contact_content, contact_content_type, contact_source = _generate_contact_sheet_content(
        image_provider,
        source_content=source_content,
        source_content_type=source_content_type,
        version_id=version_id,
    )

    attempted_keys: list[str] = []
    try:
        conn.execute("BEGIN IMMEDIATE")
        next_version_number = _next_version_number(conn, persona_id=persona_id)
        _insert_version(
            conn,
            actor=actor,
            version_id=version_id,
            persona_id=persona_id,
            persona_snapshot_json=persona_snapshot_json,
            now_iso=now_iso,
            version_number=next_version_number,
        )
        views = _generate_and_approve_views(
            conn,
            storage=storage,
            actor=actor,
            version_id=version_id,
            persona_id=persona_id,
            now_iso=now_iso,
            attempted_keys=attempted_keys,
            contact_content=contact_content,
            contact_content_type=contact_content_type,
        )
        contact_sheet_asset_id = _store_contact_sheet_asset(
            conn,
            storage=storage,
            actor=actor,
            identity_id=identity_id,
            version_id=version_id,
            content=contact_content,
            content_type=contact_content_type,
            generation_source=contact_source,
            attempted_keys=attempted_keys,
        )
        publication_hash, assets_by_view = _publish_views(
            conn,
            storage=storage,
            actor=actor,
            version_id=version_id,
            persona_id=persona_id,
            persona_snapshot_json=persona_snapshot_json,
            views=views,
            contact_sheet_asset_id=contact_sheet_asset_id,
            now_iso=now_iso,
            attempted_keys=attempted_keys,
        )

        write_audit(
            conn,
            actor=actor,
            action="simple_character.regenerate",
            entity_type="character_version",
            entity_id=version_id,
            metadata={
                "identity_id": identity_id,
                "persona_id": persona_id,
                "previous_version_id": previous_version_id,
                "version_number": next_version_number,
                "publication_hash": publication_hash,
                "contact_sheet_asset_id": contact_sheet_asset_id,
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
            "SIMPLE_CHARACTER_REGENERATION_FAILED",
            "重新生成多视图失败，请稍后重试。",
        ) from exc

    return SimpleCharacterRegenerationResult(
        identity_id=identity_id,
        persona_id=persona_id,
        character_version_id=version_id,
        previous_version_id=previous_version_id,
        version_number=next_version_number,
        publication_hash=publication_hash,
        contact_sheet_asset_id=contact_sheet_asset_id,
        views=tuple(
            SimpleCharacterView(
                view_type=view_type,
                asset_id=str(assets_by_view[view_type]["approved_asset_id"]),
            )
            for view_type in REQUIRED_CHARACTER_VIEW_TYPES
            if view_type in assets_by_view
        ),
    )


def _next_version_number(conn: sqlite3.Connection, *, persona_id: str) -> int:
    row = conn.execute(
        "SELECT MAX(version_number) FROM character_versions WHERE persona_id = ?",
        (persona_id,),
    ).fetchone()
    current = int(row[0]) if row is not None and row[0] is not None else 0
    return current + 1


StorageResolver = Callable[[sqlite3.Connection, str], StorageAdapter]


def delete_simple_character_identity(
    conn: sqlite3.Connection,
    *,
    actor: CurrentUser,
    identity_id: str,
    storage_for_uri: StorageResolver,
) -> None:
    """Delete an identity together with every derived character record.

    Mirrors project deletion: in-flight character generation tasks and project
    character selections block the delete (409), storage object cleanup is
    best-effort so an unavailable backend never blocks the operator, and the
    outcome is recorded in the audit log.
    """
    row = read_identity_row(conn, identity_id)
    if actor.role != "admin" and str(row["owner_user_id"]) != actor.id:
        raise character_error(
            403,
            "IDENTITY_DELETE_FORBIDDEN",
            "只有创建者或管理员可以删除人物。",
        )

    # Paid provider calls may still be in flight for this character; deleting
    # the versions underneath them would lose their write-back.
    active_task = conn.execute(
        """
        SELECT 1
        FROM character_generation_tasks AS task
        JOIN character_versions AS version ON version.id = task.character_version_id
        JOIN character_personas AS persona ON persona.id = version.persona_id
        WHERE persona.identity_id = ?
          AND task.status IN ('PENDING', 'RUNNING')
        LIMIT 1
        """,
        (identity_id,),
    ).fetchone()
    if active_task:
        raise character_error(
            409,
            "IDENTITY_DELETE_HAS_ACTIVE_TASKS",
            "人物存在进行中的生成任务，请等待任务结束后再删除。",
        )

    # Project character selections reference versions with ON DELETE RESTRICT;
    # removing a character a project still relies on must stay explicit.
    used_project = conn.execute(
        """
        SELECT selection.project_id
        FROM character_reference_selections AS selection
        JOIN character_versions AS version ON version.id = selection.character_version_id
        JOIN character_personas AS persona ON persona.id = version.persona_id
        WHERE persona.identity_id = ?
        LIMIT 1
        """,
        (identity_id,),
    ).fetchone()
    if used_project:
        raise character_error(
            409,
            "IDENTITY_IN_USE",
            "人物已被项目选用，请先在项目中移除该角色后再删除。",
        )

    asset_ids = _identity_asset_ids(conn, identity_id)
    placeholders = ",".join("?" for _ in asset_ids)
    asset_rows = (
        conn.execute(
            f"SELECT id, storage_uri FROM assets WHERE id IN ({placeholders})",  # noqa: S608
            tuple(asset_ids),
        ).fetchall()
        if asset_ids
        else []
    )

    # Best-effort object cleanup before removing the rows, mirroring project
    # deletion: an unavailable backend (e.g. cloud credentials removed) must
    # not block the delete; failures are counted into the audit log.
    storage_cleanup_failed_count = 0
    for asset in asset_rows:
        uri = str(asset["storage_uri"])
        try:
            storage = storage_for_uri(conn, uri)
            storage.delete_object(storage_key_from_uri(uri), actor_id=actor.id)
        except (HTTPException, StorageBackendUnavailable, OSError, ValueError):
            storage_cleanup_failed_count += 1

    version_ids_sql = """
        SELECT version.id
        FROM character_versions AS version
        JOIN character_personas AS persona ON persona.id = version.persona_id
        WHERE persona.identity_id = ?
    """
    with conn:
        conn.execute(
            "DELETE FROM character_generation_tasks WHERE character_version_id IN "
            f"({version_ids_sql})",  # noqa: S608
            (identity_id,),
        )
        conn.execute(
            """
            DELETE FROM character_asset_reviews
            WHERE character_asset_id IN (
                SELECT view.id FROM character_assets AS view
                WHERE view.character_version_id IN (
                    SELECT version.id FROM character_versions AS version
                    JOIN character_personas AS persona ON persona.id = version.persona_id
                    WHERE persona.identity_id = ?
                )
            )
            """,
            (identity_id,),
        )
        conn.execute(
            f"DELETE FROM character_assets WHERE character_version_id IN ({version_ids_sql})",  # noqa: S608
            (identity_id,),
        )
        conn.execute(
            "DELETE FROM character_versions WHERE persona_id IN "
            "(SELECT id FROM character_personas WHERE identity_id = ?)",
            (identity_id,),
        )
        conn.execute(
            "DELETE FROM character_personas WHERE identity_id = ?",
            (identity_id,),
        )
        conn.execute("DELETE FROM person_identities WHERE id = ?", (identity_id,))
        if asset_ids:
            conn.execute(
                f"DELETE FROM assets WHERE id IN ({placeholders})",
                tuple(asset_ids),
            )

    write_audit(
        conn,
        actor=actor,
        action="simple_character.delete",
        entity_type="person_identity",
        entity_id=identity_id,
        metadata={
            "deleted_asset_count": len(asset_rows),
            "storage_cleanup_failed_count": storage_cleanup_failed_count,
        },
    )


def _identity_asset_ids(conn: sqlite3.Connection, identity_id: str) -> set[str]:
    """Collect every asset owned by an identity.

    Covers the authorization/source upload, the per-view candidates of every
    version, and each version's contact sheet (snapshots predating the contact
    sheet feature simply contribute nothing).
    """
    identity = conn.execute(
        """
        SELECT authorization_asset_id, source_asset_id
        FROM person_identities WHERE id = ?
        """,
        (identity_id,),
    ).fetchone()
    asset_ids: set[str] = set()
    if identity is not None:
        for column in ("authorization_asset_id", "source_asset_id"):
            value = identity[column]
            if value is not None:
                asset_ids.add(str(value))

    for view_asset_id in conn.execute(
        """
        SELECT view.asset_id
        FROM character_assets AS view
        JOIN character_versions AS version ON version.id = view.character_version_id
        JOIN character_personas AS persona ON persona.id = version.persona_id
        WHERE persona.identity_id = ? AND view.asset_id IS NOT NULL
        """,
        (identity_id,),
    ).fetchall():
        asset_ids.add(str(view_asset_id[0]))

    for snapshot_row in conn.execute(
        """
        SELECT version.publication_snapshot_json AS snapshot_json
        FROM character_versions AS version
        JOIN character_personas AS persona ON persona.id = version.persona_id
        WHERE persona.identity_id = ?
        """,
        (identity_id,),
    ).fetchall():
        contact_id = _snapshot_contact_sheet_asset_id(snapshot_row)
        if contact_id is not None:
            asset_ids.add(contact_id)
    return asset_ids


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
    version_number: int = 1,
) -> None:
    conn.execute(
        """
        INSERT INTO character_versions (
            id, persona_id, version_number, status,
            persona_snapshot_json, provider, model, generation_params_json,
            template_version, template_hash, required_view_types_json,
            created_by, created_at, generation_mode
        ) VALUES (?, ?, ?, 'REVIEWING', ?, 'local_simple_upload',
                  'deterministic-v1', '{}', ?, ?, ?, ?, ?, 'simple_upload')
        """,
        (
            version_id,
            persona_id,
            version_number,
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
    view_type: RequiredCharacterViewType
    character_asset_id: str
    generated_asset_id: str
    review_id: str
    content: bytes
    content_type: str
    sha256: str


# Per-view images are cropped out of the real contact sheet so the published
# views show the actual person instead of a locally generated solid rectangle.
# Layout recovery is divider-strip based: real sheets follow the prompt's
# "three tall columns + two stacked close-ups separated by thin white
# dividers" layout, and the white strips are detected per column/row.
CONTACT_SHEET_WHITE_THRESHOLD = 240
CONTACT_SHEET_FRONT_HALF_HEIGHT_RATIO = 0.62
CONTACT_SHEET_MIN_PANEL_SIZE = 16


@dataclass(frozen=True)
class _ContactSheetPanels:
    """Pixel rects (x0, y0, x1, y1; half-open) of the sheet's key panels."""

    front_full: tuple[int, int, int, int]
    left_45: tuple[int, int, int, int]
    left_side: tuple[int, int, int, int]
    front_face: tuple[int, int, int, int]


def crop_contact_sheet_views(
    contact_content: bytes, contact_content_type: str
) -> dict[str, bytes] | None:
    """Crop the seven standard views out of the five-panel contact sheet.

    Returns ``{view_type: png_bytes}`` or ``None`` when the sheet cannot be
    decoded (non-PNG provider output, other bit depths, interlacing) so the
    caller can fall back to the deterministic placeholder. Sheets without
    detectable dividers use the nominal layout geometry instead, so a
    slightly off-layout sheet still yields real cropped views.
    """
    if contact_content_type.split(";", 1)[0].strip().lower() != "image/png":
        return None
    decoded = _decode_png_rgb(contact_content)
    if decoded is None:
        return None
    width, height, rows = decoded
    panels = _contact_sheet_panels_from_pixels(width, height, rows)
    if panels is None:
        return None

    x0, y0, x1, y1 = panels.front_full
    half_bottom = y0 + round((y1 - y0) * CONTACT_SHEET_FRONT_HALF_HEIGHT_RATIO)
    # RIGHT_* views mirror the LEFT_* panels: the sheet has no dedicated
    # right-side renderings, and a horizontal flip of one view of the same
    # person is the standard way to derive the opposite-side reference.
    crops: dict[str, bytes] = {
        "FRONT_FULL": _encode_rgb_panel_png(rows, panels.front_full),
        "FRONT_HALF": _encode_rgb_panel_png(rows, (x0, y0, x1, half_bottom)),
        "FRONT_FACE": _encode_rgb_panel_png(rows, panels.front_face),
        "LEFT_45": _encode_rgb_panel_png(rows, panels.left_45),
        "RIGHT_45": _encode_rgb_panel_png(rows, panels.left_45, mirror=True),
        "LEFT_SIDE": _encode_rgb_panel_png(rows, panels.left_side),
        "RIGHT_SIDE": _encode_rgb_panel_png(rows, panels.left_side, mirror=True),
    }
    return crops


def _decode_png_rgb(data: bytes) -> tuple[int, int, list[bytes]] | None:
    """Decode a non-interlaced 8-bit RGB/RGBA PNG into per-row RGB bytes."""
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return None
    width = height = bit_depth = color_type = interlace = 0
    compressed = bytearray()
    pos = 8
    while pos + 8 <= len(data):
        (length,) = struct.unpack(">I", data[pos : pos + 4])
        chunk_type = data[pos + 4 : pos + 8]
        body = data[pos + 8 : pos + 8 + length]
        pos += 12 + length
        if chunk_type == b"IHDR" and length >= 13:
            width, height, bit_depth, color_type = struct.unpack(">IIBB", body[:10])
            interlace = body[12]
        elif chunk_type == b"IDAT":
            compressed += body
        elif chunk_type == b"IEND":
            break
    if width <= 0 or height <= 0 or bit_depth != 8 or interlace != 0:
        return None
    if color_type == 2:
        channels = 3
    elif color_type == 6:
        channels = 4
    else:
        return None
    try:
        raw = zlib.decompress(bytes(compressed))
    except zlib.error:
        return None
    stride = width * channels
    if len(raw) < height * (stride + 1):
        return None
    rows: list[bytes] = []
    previous = bytes(stride)
    for y in range(height):
        offset = y * (stride + 1)
        filter_type = raw[offset]
        row = bytearray(raw[offset + 1 : offset + 1 + stride])
        if filter_type == 1:  # Sub
            for i in range(channels, stride):
                row[i] = (row[i] + row[i - channels]) & 0xFF
        elif filter_type == 2:  # Up
            for i in range(stride):
                row[i] = (row[i] + previous[i]) & 0xFF
        elif filter_type == 3:  # Average
            for i in range(stride):
                left = row[i - channels] if i >= channels else 0
                row[i] = (row[i] + ((left + previous[i]) >> 1)) & 0xFF
        elif filter_type == 4:  # Paeth
            for i in range(stride):
                a = row[i - channels] if i >= channels else 0
                b = previous[i]
                c = previous[i - channels] if i >= channels else 0
                p = a + b - c
                pa = abs(p - a)
                pb = abs(p - b)
                pc = abs(p - c)
                if pa <= pb and pa <= pc:
                    predictor = a
                elif pb <= pc:
                    predictor = b
                else:
                    predictor = c
                row[i] = (row[i] + predictor) & 0xFF
        elif filter_type != 0:  # Unknown filter byte: refuse instead of guessing.
            return None
        current = bytes(row)
        if channels == 4:
            rgb = bytearray(width * 3)
            rgb[0::3] = current[0::4]
            rgb[1::3] = current[1::4]
            rgb[2::3] = current[2::4]
            rows.append(bytes(rgb))
        else:
            rows.append(current)
        previous = current
    return width, height, rows


def _contact_sheet_panels_from_pixels(
    width: int, height: int, rows: list[bytes]
) -> _ContactSheetPanels | None:
    if width < 3 * CONTACT_SHEET_MIN_PANEL_SIZE or height < 2 * CONTACT_SHEET_MIN_PANEL_SIZE:
        return None
    column_runs = _white_runs([_column_is_divider_white(x, height, rows) for x in range(width)])
    left_edge = 0
    right_edge = width
    internal_columns: list[tuple[int, int]] = []
    for start, end in column_runs:
        if end - start + 1 > max(80, width // 12):
            # A run this wide is a white background area, not a thin divider.
            continue
        if start < width * 0.05:
            left_edge = end + 1
        elif end > width * 0.95:
            right_edge = start
        else:
            internal_columns.append((start, end))
    if len(internal_columns) == 3:
        panels = _panels_from_dividers(width, height, rows, left_edge, right_edge, internal_columns)
    else:
        panels = _nominal_contact_sheet_panels(width, height)
    for rect in (panels.front_full, panels.left_45, panels.left_side, panels.front_face):
        if (
            rect[2] - rect[0] < CONTACT_SHEET_MIN_PANEL_SIZE
            or rect[3] - rect[1] < CONTACT_SHEET_MIN_PANEL_SIZE
        ):
            return None
    return panels


def _panels_from_dividers(
    width: int,
    height: int,
    rows: list[bytes],
    left_edge: int,
    right_edge: int,
    internal_columns: list[tuple[int, int]],
) -> _ContactSheetPanels:
    """Build panel rects from three detected vertical divider strips."""
    top_edge = 0
    bottom_edge = height
    for start, end in _white_runs([_row_is_divider_white(y, width, rows) for y in range(height)]):
        if end - start + 1 > max(80, height // 12):
            continue
        if start < height * 0.05:
            top_edge = end + 1
        elif end > height * 0.95:
            bottom_edge = start
    (col1_start, col1_end), (col2_start, col2_end), (col3_start, col3_end) = internal_columns
    right_x0 = col3_end + 1
    # The stacked close-ups' horizontal divider only spans the right zone, so
    # restrict row scanning to it (the image's outer 10% is also skipped to
    # ignore edge darkening that real sheets carry at their borders).
    scan_x0 = right_x0 + max(1, (right_edge - right_x0) // 10)
    scan_x1 = right_edge - max(1, (right_edge - right_x0) // 10)
    divider: tuple[int, int] | None = None
    for start, end in _white_runs(
        [_range_row_is_white(y, scan_x0, scan_x1, rows) for y in range(height)]
    ):
        centered = top_edge + (bottom_edge - top_edge) * 0.2
        lower = top_edge + (bottom_edge - top_edge) * 0.8
        if end - start + 1 <= max(80, height // 12) and start >= centered and end <= lower:
            divider = (start, end)
            break
    if divider is None:
        # No horizontal divider between the close-ups: split the right zone
        # at its midpoint as the best geometric guess.
        middle = (top_edge + bottom_edge) // 2
        divider = (middle, middle)
    return _ContactSheetPanels(
        front_full=(left_edge, top_edge, col1_start, bottom_edge),
        left_45=(col1_end + 1, top_edge, col2_start, bottom_edge),
        left_side=(col2_end + 1, top_edge, col3_start, bottom_edge),
        front_face=(right_x0, top_edge, right_edge, divider[0]),
    )


def _nominal_contact_sheet_panels(width: int, height: int) -> _ContactSheetPanels:
    """Fallback layout geometry when no dividers can be detected."""
    border = max(4, round(width * 0.005))
    gap = max(4, round(width * 0.004))
    inner_width = width - 2 * border
    left_zone = round(inner_width * 0.705)
    column = (left_zone - 2 * gap) / 3
    right_x0 = border + left_zone + gap
    middle = height // 2
    half_gap = max(2, gap // 2)
    return _ContactSheetPanels(
        front_full=(border, border, round(border + column), height - border),
        left_45=(
            round(border + column + gap),
            border,
            round(border + 2 * column + gap),
            height - border,
        ),
        left_side=(
            round(border + 2 * column + 2 * gap),
            border,
            round(border + 3 * column + 2 * gap),
            height - border,
        ),
        front_face=(right_x0, border, width - border, middle - half_gap),
    )


def _column_is_divider_white(x: int, height: int, rows: list[bytes]) -> bool:
    sampled = 0
    white = 0
    for y in range(height // 10, height - height // 10, 4):
        index = x * 3
        sampled += 1
        if _pixel_is_white(rows[y], index):
            white += 1
    return sampled > 0 and white / sampled >= 0.99


def _row_is_divider_white(y: int, width: int, rows: list[bytes]) -> bool:
    sampled = 0
    white = 0
    for x in range(width // 10, width - width // 10, 4):
        sampled += 1
        if _pixel_is_white(rows[y], x * 3):
            white += 1
    return sampled > 0 and white / sampled >= 0.99


def _range_row_is_white(y: int, x_begin: int, x_end: int, rows: list[bytes]) -> bool:
    sampled = 0
    white = 0
    for x in range(x_begin, x_end, 2):
        sampled += 1
        if _pixel_is_white(rows[y], x * 3):
            white += 1
    return sampled > 0 and white / sampled >= 0.98


def _pixel_is_white(row: bytes, index: int) -> bool:
    return (
        row[index] >= CONTACT_SHEET_WHITE_THRESHOLD
        and row[index + 1] >= CONTACT_SHEET_WHITE_THRESHOLD
        and row[index + 2] >= CONTACT_SHEET_WHITE_THRESHOLD
    )


def _white_runs(flags: list[bool]) -> list[tuple[int, int]]:
    runs: list[tuple[int, int]] = []
    for index, flag in enumerate(flags):
        if not flag:
            continue
        if runs and index == runs[-1][1] + 1:
            runs[-1] = (runs[-1][0], index)
        else:
            runs.append((index, index))
    return runs


def _encode_rgb_panel_png(
    rows: list[bytes], rect: tuple[int, int, int, int], *, mirror: bool = False
) -> bytes:
    """Encode one panel rect as a standalone 8-bit RGB PNG (filter 0)."""
    x0, y0, x1, y1 = rect
    scanlines = bytearray()
    for y in range(y0, y1):
        row = rows[y][x0 * 3 : x1 * 3]
        if mirror:
            reversed_row = row[::-1]
            flipped = bytearray(len(reversed_row))
            # Byte-reversing swaps both the pixel order and the channel order;
            # re-interleave the channel planes to restore RGB per pixel.
            flipped[0::3] = reversed_row[2::3]
            flipped[1::3] = reversed_row[1::3]
            flipped[2::3] = reversed_row[0::3]
            row = bytes(flipped)
        scanlines += b"\x00"
        scanlines += row
    header = struct.pack(">IIBBBBB", x1 - x0, y1 - y0, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + png_chunk(b"IHDR", header)
        + png_chunk(b"IDAT", zlib.compress(bytes(scanlines), 6))
        + png_chunk(b"IEND", b"")
    )


def _generate_and_approve_views(
    conn: sqlite3.Connection,
    *,
    storage: StorageAdapter,
    actor: CurrentUser,
    version_id: str,
    persona_id: str,
    now_iso: str,
    attempted_keys: list[str],
    contact_content: bytes,
    contact_content_type: str,
) -> list[_ApprovedView]:
    """Store one approved per-view asset for each required view type.

    Views are cropped from the real contact sheet whenever its pixels can be
    decoded, so every published view shows the actual person. Undecodable
    sheets (provider returned a non-PNG or unsupported PNG) keep the
    deterministic placeholder fallback so the flow never blocks on cropping.
    """
    cropped_views = crop_contact_sheet_views(contact_content, contact_content_type)
    views: list[_ApprovedView] = []
    for view_type in REQUIRED_CHARACTER_VIEW_TYPES:
        character_asset_id = str(uuid.uuid4())
        generated_asset_id = str(uuid.uuid4())
        review_id = str(uuid.uuid4())
        content = cropped_views.get(view_type) if cropped_views else None
        if content is None:
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
                        "view_content_source": (
                            "contact_sheet_crop" if cropped_views else "local_placeholder"
                        ),
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
    contact_sheet_asset_id: str,
    now_iso: str,
    attempted_keys: list[str],
) -> tuple[str, dict[str, dict[str, object]]]:
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
        "contact_sheet_asset_id": contact_sheet_asset_id,
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
    return publication_hash, assets_by_view


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _generate_contact_sheet_content(
    provider: ImageProvider | None,
    *,
    source_content: bytes,
    source_content_type: str,
    version_id: str,
) -> tuple[bytes, str, str]:
    """Render the single five-view contact sheet image.

    Returns ``(content, content_type, generation_source)``. Whenever no
    provider is configured, the provider call fails, or its output is not a
    usable raster image, a locally composed placeholder is used instead so
    uploads never block on image generation.
    """
    if provider is not None:
        extension = SIMPLE_CONTACT_SHEET_EXTENSIONS.get(source_content_type, ".png")
        try:
            generated = provider.edit(
                model=SIMPLE_CONTACT_SHEET_MODEL,
                prompt=SIMPLE_CONTACT_SHEET_PROMPT,
                source_image=ImageInput(
                    content=source_content,
                    content_type=source_content_type,
                    filename=f"character-source{extension}",
                ),
                character_reference_images=[],
                output_count=1,
            )
        except ImageProviderFailed as exc:
            logger.warning("Contact sheet provider failed, using placeholder: %s", exc)
        else:
            if generated:
                image = generated[0]
                content_type = image.content_type.split(";", 1)[0].strip().lower()
                if image.content and content_type in SIMPLE_CONTACT_SHEET_EXTENSIONS:
                    return image.content, content_type, "image_provider"
            logger.warning("Contact sheet provider returned no usable image, using placeholder")
    return (
        contact_sheet_placeholder_png(f"contact-sheet:{version_id}".encode()),
        "image/png",
        "local_placeholder",
    )


def _contact_sheet_asset_key(
    *, owner_user_id: str, identity_id: str, asset_id: str, extension: str
) -> str:
    for value in (owner_user_id, identity_id, asset_id):
        validate_key_segment(value)
    if extension not in {".png", ".jpg", ".webp"}:
        raise ValueError("unsupported contact sheet extension")
    return f"users/{owner_user_id}/identities/{identity_id}/contact-sheets/{asset_id}{extension}"


def _store_contact_sheet_asset(
    conn: sqlite3.Connection,
    *,
    storage: StorageAdapter,
    actor: CurrentUser,
    identity_id: str,
    version_id: str,
    content: bytes,
    content_type: str,
    generation_source: str,
    attempted_keys: list[str],
) -> str:
    """Persist the five-view contact sheet as its own downloadable asset."""
    asset_id = str(uuid.uuid4())
    key = _contact_sheet_asset_key(
        owner_user_id=actor.id,
        identity_id=identity_id,
        asset_id=asset_id,
        extension=SIMPLE_CONTACT_SHEET_EXTENSIONS[content_type],
    )
    stored = storage.put_object(key, content, content_type=content_type)
    attempted_keys.append(stored.key)
    conn.execute(
        """
        INSERT INTO assets (
            id, project_id, kind, storage_uri, sha256, size_bytes,
            content_type, created_by_user_id, metadata_json
        ) VALUES (?, NULL, 'character_contact_sheet', ?, ?, ?, ?, ?, ?)
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
                    "character_version_id": version_id,
                    "generation_mode": SIMPLE_GENERATION_MODE,
                    "generation_source": generation_source,
                    "object_key": stored.key,
                    "purpose": "five_view_contact_sheet",
                }
            ),
        ),
    )
    return asset_id


CONTACT_SHEET_PLACEHOLDER_WIDTH = 2240
CONTACT_SHEET_PLACEHOLDER_HEIGHT = 1400


def contact_sheet_placeholder_png(
    seed: bytes,
    *,
    width: int = CONTACT_SHEET_PLACEHOLDER_WIDTH,
    height: int = CONTACT_SHEET_PLACEHOLDER_HEIGHT,
) -> bytes:
    """Locally composed five-panel placeholder mirroring the sheet layout.

    Three equal tall columns on the left plus two stacked portrait panels on
    the right, separated by thin near-white dividers, so even the fallback
    visually reads as one multi-view contact sheet.
    """
    divider = b"\xe8\xe8\xe8"
    panels = _contact_sheet_panels(width, height, seed)
    scanlines: list[bytes] = []
    for y in range(height):
        row = bytearray(b"\x00")
        cursor = 0
        for x0, y0, panel_width, panel_height, color in panels:
            if y0 <= y < y0 + panel_height:
                if x0 > cursor:
                    row += divider * (x0 - cursor)
                    cursor = x0
                row += color * panel_width
                cursor = x0 + panel_width
        if cursor < width:
            row += divider * (width - cursor)
        scanlines.append(bytes(row))
    pixels = zlib.compress(b"".join(scanlines))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + png_chunk(b"IHDR", header)
        + png_chunk(b"IDAT", pixels)
        + png_chunk(b"IEND", b"")
    )


def _contact_sheet_panels(
    width: int, height: int, seed: bytes
) -> list[tuple[int, int, int, int, bytes]]:
    border = max(8, width // 140)
    gap = border
    inner_width = width - 2 * border
    inner_height = height - 2 * border
    left_width = round(inner_width * 0.72)
    right_width = inner_width - left_width - gap

    columns = 3
    base_column_width, remainder = divmod(left_width - (columns - 1) * gap, columns)
    panels: list[tuple[int, int, int, int, bytes]] = []
    x = border
    for index in range(columns):
        column_width = base_column_width + (1 if index < remainder else 0)
        panels.append((x, border, column_width, inner_height, _panel_color(seed, index)))
        x += column_width + gap

    base_row_height, row_remainder = divmod(inner_height - gap, 2)
    right_x = border + left_width + gap
    panels.append(
        (
            right_x,
            border,
            right_width,
            base_row_height + row_remainder,
            _panel_color(seed, columns),
        )
    )
    panels.append(
        (
            right_x,
            border + base_row_height + row_remainder + gap,
            right_width,
            base_row_height,
            _panel_color(seed, columns + 1),
        )
    )
    return panels


def _panel_color(seed: bytes, index: int) -> bytes:
    digest = hashlib.sha256(seed + bytes((index,))).digest()
    return bytes((80 + digest[0] % 120, 80 + digest[1] % 120, 80 + digest[2] % 120))
