from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from typing import Literal, Protocol, cast
from uuid import uuid4

from fastapi import HTTPException

from app.analysis import insert_version
from app.auth import CurrentUser
from app.characters import character_is_available, get_project_main_character, read_character
from app.permissions import (
    require_asset_access,
    require_not_auditor,
    require_project_access,
    write_audit,
)
from app.source_frames import (
    SOURCE_FRAME_CANDIDATES_KIND,
    SOURCE_FRAME_SELECTION_KIND,
    latest_version,
)
from app.storage import (
    StorageAdapter,
    StorageBackendUnavailable,
    require_storage_match,
    storage_object_ref_from_uri,
)

FIRST_FRAME_CANDIDATES_KIND = "first_frame_candidates"
FIRST_FRAME_SELECTION_KIND = "first_frame_selection"
FIRST_FRAME_SCHEMA_VERSION = "b5.first-frame.v1"
FIRST_FRAME_MODELS = ("gpt-image-2", "nano-banana-pro-2k")
FIRST_FRAME_IMAGE_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp"}
MAX_FIRST_FRAME_CANDIDATES = 3

FirstFrameModel = Literal["gpt-image-2", "nano-banana-pro-2k"]


@dataclass(frozen=True)
class GeneratedImage:
    content: bytes
    content_type: str


class ImageProvider(Protocol):
    provider_name: str

    def edit(
        self,
        *,
        model: FirstFrameModel,
        prompt: str,
        source_image: bytes,
        character_reference_images: list[bytes],
        output_count: int,
    ) -> list[GeneratedImage]: ...


class ImageProviderFailed(RuntimeError):
    pass


class FakeImageProvider:
    provider_name = "fake"

    def edit(
        self,
        *,
        model: FirstFrameModel,
        prompt: str,
        source_image: bytes,
        character_reference_images: list[bytes],
        output_count: int,
    ) -> list[GeneratedImage]:
        del model, prompt, character_reference_images
        return [
            GeneratedImage(content=source_image, content_type="image/jpeg")
            for _ in range(output_count)
        ]


def generate_first_frame_candidates(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    actor: CurrentUser,
    storage: StorageAdapter,
    provider: ImageProvider,
    model: FirstFrameModel,
    prompt: str | None,
    quantity: int,
) -> sqlite3.Row:
    require_not_auditor(
        conn,
        actor=actor,
        action="first_frame.generate",
        entity_type="project",
        entity_id=project_id,
    )
    require_project_access(conn, actor=actor, project_id=project_id, action="first_frame.generate")
    if model not in FIRST_FRAME_MODELS:
        raise first_frame_error(
            422, "FIRST_FRAME_MODEL_UNSUPPORTED", "The requested image model is unavailable."
        )
    if quantity < 1 or quantity > MAX_FIRST_FRAME_CANDIDATES:
        raise first_frame_error(
            422,
            "FIRST_FRAME_QUANTITY_INVALID",
            f"Generate between 1 and {MAX_FIRST_FRAME_CANDIDATES} candidates.",
        )

    source_selection = current_source_frame_selection(conn, project_id=project_id)
    source_frame_asset_id = str(source_selection["source_frame_asset_id"])
    source_frame = require_asset_access(
        conn,
        actor=actor,
        asset_id=source_frame_asset_id,
        action="first_frame.generate",
    )
    if str(source_frame["project_id"]) != project_id or str(source_frame["kind"]) != "source_frame":
        raise first_frame_error(
            422, "SOURCE_FRAME_INVALID", "The confirmed source frame is invalid."
        )

    main_character = get_project_main_character(conn, project_id=project_id)
    character = read_character(conn, str(main_character["character_id"]))
    if not character_is_available(character, project_id=project_id):
        raise first_frame_error(
            422,
            "CHARACTER_NOT_AVAILABLE",
            "The selected character is inactive, expired, or not authorized for this project.",
        )
    character_snapshot = main_character["character_snapshot"]
    if not isinstance(character_snapshot, dict):
        raise first_frame_error(
            409, "MAIN_CHARACTER_SNAPSHOT_INVALID", "Select the character again."
        )
    reference_asset_ids = character_snapshot.get("reference_asset_ids")
    character_name = character_snapshot.get("name")
    if not isinstance(reference_asset_ids, list) or not all(
        isinstance(asset_id, str) for asset_id in reference_asset_ids
    ):
        raise first_frame_error(
            409, "MAIN_CHARACTER_SNAPSHOT_INVALID", "Select the character again."
        )
    if not isinstance(character_name, str) or not character_name:
        raise first_frame_error(
            409, "MAIN_CHARACTER_SNAPSHOT_INVALID", "Select the character again."
        )
    if not reference_asset_ids:
        raise first_frame_error(
            422,
            "CHARACTER_REFERENCE_REQUIRED",
            "The selected character needs at least one reference image.",
        )

    source_content = read_asset_content(storage, source_frame)
    reference_assets = [
        read_character_reference_asset(conn, asset_id=asset_id) for asset_id in reference_asset_ids
    ]
    reference_images = [read_asset_content(storage, asset) for asset in reference_assets]
    effective_prompt = normalize_prompt(prompt, character_name=character_name)

    generated = edit_once_with_retry(
        provider,
        model=model,
        prompt=effective_prompt,
        source_image=source_content,
        character_reference_images=reference_images,
        quantity=quantity,
    )
    if len(generated) != quantity or any(
        not item.content or item.content_type not in FIRST_FRAME_IMAGE_CONTENT_TYPES
        for item in generated
    ):
        raise first_frame_error(
            502,
            "FIRST_FRAME_PROVIDER_RESPONSE_INVALID",
            "The image provider did not return the requested candidates.",
        )
    require_current_first_frame_inputs(
        conn,
        project_id=project_id,
        source_frame_selection_version_id=str(source_selection["id"]),
        main_character_version_id=str(main_character["version_id"]),
    )

    created_assets: list[tuple[str, str]] = []
    try:
        candidates: list[dict[str, object]] = []
        for image in generated:
            extension = image_extension(image.content_type)
            asset_id = str(uuid4())
            storage_key = f"projects/{project_id}/first-frames/{asset_id}.{extension}"
            created_assets.append((asset_id, storage_key))
            stored = storage.put_object(storage_key, image.content, content_type=image.content_type)
            candidates.append(
                {
                    "asset_id": asset_id,
                    "storage_key": storage_key,
                    "storage_uri": stored.uri,
                    "sha256": stored.sha256 or hashlib.sha256(image.content).hexdigest(),
                    "size_bytes": stored.size,
                    "content_type": image.content_type,
                }
            )

        with conn:
            for candidate in candidates:
                conn.execute(
                    """
                    INSERT INTO assets (
                        id, project_id, kind, storage_uri, sha256, size_bytes, content_type,
                        created_by_user_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        candidate["asset_id"],
                        project_id,
                        "first_frame",
                        candidate["storage_uri"],
                        candidate["sha256"],
                        candidate["size_bytes"],
                        candidate["content_type"],
                        actor.id,
                    ),
                )
            row = insert_version(
                conn,
                project_id=project_id,
                asset_id=source_frame_asset_id,
                kind=FIRST_FRAME_CANDIDATES_KIND,
                created_by_user_id=actor.id,
                payload={
                    "schema_version": FIRST_FRAME_SCHEMA_VERSION,
                    "source_frame_selection_version_id": str(source_selection["id"]),
                    "source_frame_asset_id": source_frame_asset_id,
                    "main_character_version_id": str(main_character["version_id"]),
                    "character_snapshot": character_snapshot,
                    "character_reference_asset_ids": reference_asset_ids,
                    "provider": provider.provider_name,
                    "model": model,
                    "prompt": effective_prompt,
                    "candidates": candidates,
                },
            )
    except sqlite3.Error as exc:
        delete_created_first_frames(storage, created_assets, actor_id=actor.id)
        raise first_frame_error(
            500,
            "FIRST_FRAME_PERSIST_FAILED",
            "First-frame candidates could not be saved. Generate them again.",
        ) from exc
    except (OSError, StorageBackendUnavailable, ValueError) as exc:
        delete_created_first_frames(storage, created_assets, actor_id=actor.id)
        raise first_frame_error(
            503,
            "FIRST_FRAME_STORAGE_UNAVAILABLE",
            "First-frame storage is temporarily unavailable.",
        ) from exc

    write_audit(
        conn,
        actor=actor,
        action="first_frame.generate",
        entity_type="version",
        entity_id=str(row["id"]),
        metadata={"project_id": project_id, "model": model, "quantity": quantity},
    )
    return row


def confirm_first_frame(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    first_frame_asset_id: str,
    actor: CurrentUser,
) -> sqlite3.Row:
    require_not_auditor(
        conn,
        actor=actor,
        action="first_frame.confirm",
        entity_type="project",
        entity_id=project_id,
    )
    require_project_access(conn, actor=actor, project_id=project_id, action="first_frame.confirm")
    candidate_version = current_first_frame_candidates(conn, project_id=project_id)
    payload = json.loads(str(candidate_version["payload_json"]))
    candidates = payload.get("candidates")
    if not isinstance(candidates, list):
        raise first_frame_error(
            409, "FIRST_FRAME_CANDIDATES_INVALID", "Generate first-frame candidates again."
        )
    candidate = next(
        (
            value
            for value in candidates
            if isinstance(value, dict) and value.get("asset_id") == first_frame_asset_id
        ),
        None,
    )
    if candidate is None:
        raise first_frame_error(
            422, "FIRST_FRAME_CANDIDATE_NOT_FOUND", "Select a candidate from the latest set."
        )
    asset = require_asset_access(
        conn,
        actor=actor,
        asset_id=first_frame_asset_id,
        action="first_frame.confirm",
    )
    if str(asset["project_id"]) != project_id or str(asset["kind"]) != "first_frame":
        raise first_frame_error(
            422, "FIRST_FRAME_CANDIDATE_NOT_FOUND", "The selected first frame is invalid."
        )

    row = insert_version(
        conn,
        project_id=project_id,
        asset_id=first_frame_asset_id,
        kind=FIRST_FRAME_SELECTION_KIND,
        created_by_user_id=actor.id,
        payload={
            "schema_version": FIRST_FRAME_SCHEMA_VERSION,
            "first_frame_candidates_version_id": str(candidate_version["id"]),
            "first_frame_asset_id": first_frame_asset_id,
        },
    )
    write_audit(
        conn,
        actor=actor,
        action="first_frame.confirm",
        entity_type="version",
        entity_id=str(row["id"]),
        metadata={"project_id": project_id, "first_frame_asset_id": first_frame_asset_id},
    )
    return row


def current_source_frame_selection(
    conn: sqlite3.Connection, *, project_id: str
) -> dict[str, object]:
    selection = latest_version(conn, project_id, SOURCE_FRAME_SELECTION_KIND)
    candidates = latest_version(conn, project_id, SOURCE_FRAME_CANDIDATES_KIND)
    if selection is None or candidates is None:
        raise first_frame_error(
            409, "SOURCE_FRAME_SELECTION_REQUIRED", "Confirm a source frame first."
        )
    payload = json.loads(str(selection["payload_json"]))
    if payload.get("source_frame_candidates_version_id") != str(candidates["id"]):
        raise first_frame_error(
            409,
            "SOURCE_FRAME_SELECTION_STALE",
            "Select a source frame from the latest candidate set.",
        )
    return cast(dict[str, object], payload | {"id": str(selection["id"])})


def current_first_frame_candidates(conn: sqlite3.Connection, *, project_id: str) -> sqlite3.Row:
    candidates = latest_version(conn, project_id, FIRST_FRAME_CANDIDATES_KIND)
    if candidates is None:
        raise first_frame_error(
            409, "FIRST_FRAME_CANDIDATES_NOT_FOUND", "Generate first-frame candidates first."
        )
    payload = json.loads(str(candidates["payload_json"]))
    if not isinstance(payload, dict):
        raise first_frame_error(
            409, "FIRST_FRAME_CANDIDATES_INVALID", "Generate first-frame candidates again."
        )
    source_version_id = payload.get("source_frame_selection_version_id")
    main_character_version_id = payload.get("main_character_version_id")
    if not isinstance(source_version_id, str) or not isinstance(main_character_version_id, str):
        raise first_frame_error(
            409, "FIRST_FRAME_CANDIDATES_INVALID", "Generate first-frame candidates again."
        )
    require_current_first_frame_inputs(
        conn,
        project_id=project_id,
        source_frame_selection_version_id=source_version_id,
        main_character_version_id=main_character_version_id,
    )
    return candidates


def require_current_first_frame_inputs(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    source_frame_selection_version_id: str,
    main_character_version_id: str,
) -> None:
    try:
        source_selection = current_source_frame_selection(conn, project_id=project_id)
        main_character = get_project_main_character(conn, project_id=project_id)
        character = read_character(conn, str(main_character["character_id"]))
    except HTTPException as exc:
        if exc.status_code in {404, 409}:
            raise first_frame_error(
                409,
                "FIRST_FRAME_CANDIDATES_STALE",
                "Generate first-frame candidates again using the current source frame "
                "and character.",
            ) from exc
        raise
    if (
        str(source_selection["id"]) != source_frame_selection_version_id
        or str(main_character["version_id"]) != main_character_version_id
        or not character_is_available(character, project_id=project_id)
    ):
        raise first_frame_error(
            409,
            "FIRST_FRAME_CANDIDATES_STALE",
            "Generate first-frame candidates again using the current source frame and character.",
        )


def read_character_reference_asset(conn: sqlite3.Connection, *, asset_id: str) -> sqlite3.Row:
    row = conn.execute(
        """
        SELECT id, project_id, kind, storage_uri, sha256, size_bytes, content_type
        FROM assets WHERE id = ?
        """,
        (asset_id,),
    ).fetchone()
    if row is None:
        raise first_frame_error(
            422, "CHARACTER_REFERENCE_NOT_FOUND", "A character reference image is missing."
        )
    if not str(row["content_type"]).startswith("image/"):
        raise first_frame_error(
            422, "CHARACTER_REFERENCE_INVALID", "Character references must be images."
        )
    return cast(sqlite3.Row, row)


def read_asset_content(storage: StorageAdapter, asset: sqlite3.Row) -> bytes:
    try:
        reference = storage_object_ref_from_uri(str(asset["storage_uri"]))
        require_storage_match(storage, reference)
        return storage.get_object(reference.key)
    except (KeyError, OSError, StorageBackendUnavailable, ValueError) as exc:
        raise first_frame_error(
            503,
            "FIRST_FRAME_INPUT_STORAGE_UNAVAILABLE",
            "Source or character reference storage is temporarily unavailable.",
        ) from exc


def edit_once_with_retry(
    provider: ImageProvider,
    *,
    model: FirstFrameModel,
    prompt: str,
    source_image: bytes,
    character_reference_images: list[bytes],
    quantity: int,
) -> list[GeneratedImage]:
    for attempt in range(2):
        try:
            return provider.edit(
                model=model,
                prompt=prompt,
                source_image=source_image,
                character_reference_images=character_reference_images,
                output_count=quantity,
            )
        except ImageProviderFailed as exc:
            if attempt == 1:
                raise first_frame_error(
                    502,
                    "FIRST_FRAME_PROVIDER_FAILED",
                    "The image provider could not generate a first frame.",
                ) from exc
    raise AssertionError("image provider retry loop must return or raise")


def normalize_prompt(prompt: str | None, *, character_name: str) -> str:
    clean = (prompt or "").strip()
    if clean:
        return clean
    return (
        "保留原图的镜头位置、人物姿态、动作、场景、构图、道具、光线与色调，"
        f"只将原人物身份替换为角色库人物“{character_name}”；"
        "保持自然皮肤、正确肢体和真实透视；不得增加或删除主体。"
    )


def image_extension(content_type: str) -> str:
    return {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}[content_type]


def delete_created_first_frames(
    storage: StorageAdapter,
    created_assets: list[tuple[str, str]],
    *,
    actor_id: str,
) -> None:
    for _, storage_key in created_assets:
        try:
            storage.delete_object(storage_key, actor_id=actor_id)
        except (OSError, StorageBackendUnavailable):
            pass


def first_frame_error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})
