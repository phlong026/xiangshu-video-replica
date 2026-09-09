"""Unified Studio material projection over existing business sources.

Assets remain the physical-file source of truth. Provider-hosted H3 DIRECT
results are projected as read-only virtual materials until a separate, explicit
archive action creates a real asset. User preferences never grant access.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast
from uuid import uuid4

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.auth import CurrentUser
from app.db_portable import BusinessConnection
from app.media import MAX_UPLOAD_BYTES, UPLOAD_INTENT_EXPIRES_IN
from app.media_tools import probe_duration_seconds, resolve_media_binary
from app.permissions import require_asset_access, require_not_auditor, write_audit
from app.storage import (
    StorageAdapter,
    StorageBackendUnavailable,
    require_storage_match,
    storage_object_ref_from_uri,
)

MaterialMediaType = Literal["image", "video", "audio"]
MaterialSource = Literal["upload", "project", "character", "oral", "generation"]
MaterialStatus = Literal["uploading", "ready", "unavailable"]
MaterialDelivery = Literal["stored", "direct"]
AudioPurpose = Literal["oral_audio", "voice_clone"]

IMAGE_UPLOAD_LIMIT = 10 * 1024 * 1024
ALLOWED_UPLOADS: dict[tuple[str, str], tuple[MaterialMediaType, str]] = {
    (".jpg", "image/jpeg"): ("image", ".jpg"),
    (".jpeg", "image/jpeg"): ("image", ".jpg"),
    (".png", "image/png"): ("image", ".png"),
    (".mp3", "audio/mpeg"): ("audio", ".mp3"),
    (".mp4", "video/mp4"): ("video", ".mp4"),
    (".mov", "video/quicktime"): ("video", ".mov"),
}
ASSET_KIND_FOR_MEDIA: dict[MaterialMediaType, str] = {
    "image": "material_image",
    "video": "material_video",
    "audio": "material_audio",
}


class MaterialItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    owner_user_id: str
    asset_id: str | None
    generation_task_id: str | None
    project_id: str | None
    person_id: str | None
    title: str
    group: str
    media_type: MaterialMediaType
    source: MaterialSource
    status: MaterialStatus
    delivery: MaterialDelivery
    content_type: str | None
    size_bytes: int | None
    duration_seconds: float | None
    created_at: str
    hidden: bool
    saved: bool
    allowed_uses: list[str]
    allowed_actions: list[str]


class MaterialPage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[MaterialItem]
    page: int
    page_size: int
    total: int


class MaterialResolveResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[MaterialItem]
    unavailable_ids: list[str]


class MaterialUploadIntentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    filename: str = Field(min_length=1, max_length=255)
    content_type: str = Field(min_length=1, max_length=100)
    size_bytes: int = Field(gt=0)
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    title: str | None = Field(default=None, min_length=1, max_length=120)
    group: str | None = Field(default=None, min_length=1, max_length=80)
    audio_purpose: AudioPurpose | None = None
    duration_seconds: float | None = Field(default=None, gt=0)


class MaterialUploadIntentResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    material_id: str
    asset_id: str
    storage_key: str | None
    method: str
    url: str
    headers: dict[str, str]
    expires_at: str


class MaterialUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, max_length=120)
    group: str | None = Field(default=None, max_length=80)
    hidden: bool | None = None


@dataclass(frozen=True)
class PreparedMaterialUpload:
    asset_id: str
    owner_user_id: str
    storage_uri: str
    storage_key: str
    media_type: MaterialMediaType
    content_type: str
    requested_size_bytes: int
    expected_sha256: str | None
    audio_purpose: AudioPurpose | None = None
    requested_duration_seconds: float | None = None


@dataclass(frozen=True)
class ProbedMaterialUpload:
    prepared: PreparedMaterialUpload
    storage_uri: str
    sha256: str
    size_bytes: int
    duration_seconds: float | None = None


def material_error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


def parse_material_id(material_id: str) -> tuple[Literal["asset", "generation"], str]:
    source_type, separator, source_id = material_id.partition(":")
    if source_type not in {"asset", "generation"} or not separator or not source_id:
        raise material_error(404, "MATERIAL_NOT_FOUND", "素材不存在。")
    return cast(Literal["asset", "generation"], source_type), source_id


def validate_upload_request(
    *, filename: str, content_type: str, size_bytes: int
) -> tuple[MaterialMediaType, str]:
    suffix = Path(filename).suffix.lower()
    matched = ALLOWED_UPLOADS.get((suffix, content_type.lower()))
    if matched is None:
        raise material_error(
            415,
            "MATERIAL_TYPE_UNSUPPORTED",
            "仅支持 JPG、PNG、MP3、MP4 和 MOV 素材。",
        )
    media_type, safe_suffix = matched
    limit = IMAGE_UPLOAD_LIMIT if media_type == "image" else MAX_UPLOAD_BYTES
    if size_bytes > limit:
        raise material_error(413, "MATERIAL_TOO_LARGE", "素材文件超过允许大小。")
    return media_type, safe_suffix


def validate_audio_contract(
    *,
    media_type: MaterialMediaType,
    audio_purpose: AudioPurpose | None,
    duration_seconds: float | None,
) -> None:
    if media_type != "audio":
        if audio_purpose is not None or duration_seconds is not None:
            raise material_error(
                422,
                "MATERIAL_AUDIO_PURPOSE_INVALID",
                "非音频素材不能声明音频用途或时长。",
            )
        return
    if audio_purpose is None:
        raise material_error(
            422,
            "MATERIAL_AUDIO_PURPOSE_REQUIRED",
            "音频素材必须声明完整口播或声音克隆用途。",
        )
    if duration_seconds is None:
        raise material_error(
            422,
            "MATERIAL_AUDIO_DURATION_REQUIRED",
            "音频用途素材必须提供可验证时长。",
        )
    if audio_purpose == "voice_clone" and not 5 <= duration_seconds <= 180:
        raise material_error(
            422,
            "MATERIAL_AUDIO_DURATION_INVALID",
            "声音克隆样本时长必须为 5–180 秒。",
        )


def probe_audio_duration(content: bytes) -> float | None:
    try:
        ffprobe = resolve_media_binary("ffprobe")
        with tempfile.NamedTemporaryFile(suffix=".mp3") as audio_file:
            audio_file.write(content)
            audio_file.flush()
            return probe_duration_seconds(ffprobe, Path(audio_file.name))
    except (OSError, RuntimeError):
        return None


def _candidate_cte() -> str:
    return """
    WITH material_candidates AS (
        SELECT
            'asset' AS source_type,
            asset.id AS source_id,
            project.owner_user_id,
            asset.id AS asset_id,
            NULL AS generation_task_id,
            project.id AS project_id,
            NULL AS person_id,
            project.name AS base_title,
            '项目素材' AS base_group,
            'project' AS source,
            asset.content_type,
            asset.size_bytes,
            asset.metadata_json,
            asset.created_at,
            CASE
                WHEN asset.content_type LIKE 'image/%%' THEN 'image'
                WHEN asset.content_type LIKE 'audio/%%' THEN 'audio'
                ELSE 'video'
            END AS media_type,
            CASE WHEN asset.size_bytes > 0 THEN 'ready' ELSE 'uploading' END AS status,
            'stored' AS delivery
        FROM assets AS asset
        JOIN projects AS project ON project.id = asset.project_id
        WHERE asset.content_type LIKE 'image/%%'
           OR asset.content_type LIKE 'audio/%%'
           OR asset.content_type LIKE 'video/%%'

        UNION ALL

        SELECT
            'asset', asset.id, asset.created_by_user_id, asset.id, NULL,
            NULL, NULL, asset.id, '我的上传', 'upload', asset.content_type,
            asset.size_bytes, asset.metadata_json, asset.created_at,
            CASE asset.kind
                WHEN 'material_image' THEN 'image'
                WHEN 'material_audio' THEN 'audio'
                ELSE 'video'
            END,
            CASE WHEN asset.size_bytes > 0 AND asset.sha256 != '' THEN 'ready' ELSE 'uploading' END,
            'stored'
        FROM assets AS asset
        WHERE asset.project_id IS NULL
          AND asset.kind IN ('material_image', 'material_audio', 'material_video')
          AND asset.created_by_user_id IS NOT NULL

        UNION ALL

        SELECT
            'asset', asset.id, oral.owner_user_id, asset.id, NULL,
            NULL, oral.identity_id, oral.title, '口播成片', 'oral',
            asset.content_type, asset.size_bytes, asset.metadata_json,
            asset.created_at, 'video', 'ready', 'stored'
        FROM oral_tasks AS oral
        JOIN assets AS asset ON asset.id = oral.result_asset_id
        WHERE oral.status = 'SUCCEEDED'

        UNION ALL

        SELECT DISTINCT
            'asset', asset.id, identity.owner_user_id, asset.id, NULL,
            NULL, identity.id,
            identity.display_name || ' · 人物素材', '人物素材', 'character',
            asset.content_type, asset.size_bytes, asset.metadata_json,
            asset.created_at, 'image', 'ready', 'stored'
        FROM character_assets AS character_asset
        JOIN assets AS asset ON asset.id = character_asset.asset_id
        JOIN character_versions AS version
          ON version.id = character_asset.character_version_id
        JOIN character_personas AS persona ON persona.id = version.persona_id
        JOIN person_identities AS identity ON identity.id = persona.identity_id
        WHERE character_asset.review_status = 'APPROVED'
          AND character_asset.is_published_selection = 1
          AND version.status = 'PUBLISHED'
          AND identity.owner_user_id IS NOT NULL

        UNION ALL

        SELECT
            'generation', task.id, project.owner_user_id, NULL, task.id,
            project.id, NULL,
            COALESCE(batch.display_name, project.name) || ' · 成片',
            '任务结果', 'generation', 'video/mp4', NULL, '{}',
            task.created_at, 'video', 'ready', 'direct'
        FROM generation_tasks AS task
        JOIN generation_batches AS batch ON batch.id = task.batch_id
        JOIN projects AS project ON project.id = batch.project_id
        WHERE task.status = 'SUCCEEDED'
          AND task.archive_status = 'DIRECT'
          AND task.provider_result_url IS NOT NULL
          AND task.provider_result_url != ''
          AND task.result_asset_id IS NULL
          AND task.superseded_by_task_id IS NULL
          AND NOT EXISTS (
              SELECT 1 FROM customer_batch_visibility AS visibility
              WHERE visibility.user_id = %s AND visibility.batch_id = batch.id
          )
    )
    """


def _scope_clause(actor: CurrentUser) -> tuple[str, list[object]]:
    if actor.role in {"employee", "customer"}:
        return "candidate.owner_user_id = %s", [actor.id]
    return "1 = 1", []


def _read_rows(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    media_type: str | None,
    source: str | None,
    query: str | None,
    include_hidden: bool,
    limit: int | None,
    offset: int = 0,
    material_ids: list[tuple[Literal["asset", "generation"], str]] | None = None,
) -> list[Any]:
    scope, scope_params = _scope_clause(actor)
    clauses = [scope]
    parameters: list[object] = [actor.id, actor.id, *scope_params]
    if not include_hidden:
        clauses.append("COALESCE(preference.hidden, 0) = 0")
    if media_type:
        clauses.append("candidate.media_type = %s")
        parameters.append(media_type)
    if source:
        clauses.append("candidate.source = %s")
        parameters.append(source)
    if query:
        clauses.append("LOWER(COALESCE(preference.title_override, candidate.base_title)) LIKE %s")
        parameters.append(f"%{query.lower()}%")
    if material_ids:
        clauses.append(
            "("
            + " OR ".join(
                "(candidate.source_type = %s AND candidate.source_id = %s)" for _ in material_ids
            )
            + ")"
        )
        for source_type, source_id in material_ids:
            parameters.extend([source_type, source_id])
    pagination = ""
    if limit is not None:
        pagination = "LIMIT %s OFFSET %s"
        parameters.extend([limit, offset])
    return conn.execute(
        _candidate_cte()
        + f"""
        SELECT
            candidate.*,
            preference.title_override,
            preference.group_override,
            COALESCE(preference.hidden, 0) AS hidden
        FROM material_candidates AS candidate
        LEFT JOIN studio_material_preferences AS preference
          ON preference.user_id = %s
         AND preference.source_type = candidate.source_type
         AND preference.source_id = candidate.source_id
        WHERE {" AND ".join(clauses)}
        ORDER BY candidate.created_at DESC, candidate.source_type DESC,
                 candidate.source_id DESC
        {pagination}
        """,
        tuple(parameters),
    ).fetchall()


def _count_rows(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    media_type: str | None,
    source: str | None,
    query: str | None,
) -> int:
    scope, scope_params = _scope_clause(actor)
    clauses = [scope, "COALESCE(preference.hidden, 0) = 0"]
    parameters: list[object] = [actor.id, actor.id, *scope_params]
    if media_type:
        clauses.append("candidate.media_type = %s")
        parameters.append(media_type)
    if source:
        clauses.append("candidate.source = %s")
        parameters.append(source)
    if query:
        clauses.append("LOWER(COALESCE(preference.title_override, candidate.base_title)) LIKE %s")
        parameters.append(f"%{query.lower()}%")
    row = conn.execute(
        _candidate_cte()
        + f"""
        SELECT COUNT(*) AS total
        FROM material_candidates AS candidate
        LEFT JOIN studio_material_preferences AS preference
          ON preference.user_id = %s
         AND preference.source_type = candidate.source_type
         AND preference.source_id = candidate.source_id
        WHERE {" AND ".join(clauses)}
        """,
        tuple(parameters),
    ).fetchone()
    return int(row["total"]) if row is not None else 0


def _metadata(raw: object) -> dict[str, Any]:
    try:
        parsed = json.loads(str(raw))
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _duration(metadata: dict[str, Any]) -> float | None:
    value = metadata.get("duration_seconds")
    return float(value) if isinstance(value, (int, float)) and value >= 0 else None


def _row_title(row: Any, metadata: dict[str, Any]) -> str:
    override = row["title_override"]
    if isinstance(override, str) and override.strip():
        return override.strip()
    original = metadata.get("original_filename")
    if row["source"] == "upload" and isinstance(original, str) and original.strip():
        return original.strip()
    return str(row["base_title"])


def material_item(row: Any) -> MaterialItem:
    metadata = _metadata(row["metadata_json"])
    ready = str(row["status"]) == "ready"
    direct = str(row["delivery"]) == "direct"
    media_type = str(row["media_type"])
    uses: list[str] = []
    if ready and not direct:
        if media_type == "image":
            uses = ["original_frame", "first_frame", "tail_frame", "reference"]
        elif media_type == "audio":
            purpose = metadata.get("audio_purpose")
            duration = _duration(metadata)
            duration_valid = duration is not None and duration > 0
            if purpose == "voice_clone" and duration is not None:
                duration_valid = 5 <= duration <= 180
            if (
                purpose in {"oral_audio", "voice_clone"}
                and duration_valid
                and metadata.get("audio_duration_verified") is True
            ):
                uses = [purpose]
        elif media_type == "video":
            uses = ["reference"]
    actions = ["preview"] if ready else []
    if ready and not direct:
        actions.append("download")
    if direct:
        actions.append("hide")
    else:
        actions.extend(["rename", "hide"])
    group_override = row["group_override"]
    return MaterialItem(
        id=f"{row['source_type']}:{row['source_id']}",
        owner_user_id=str(row["owner_user_id"]),
        asset_id=None if row["asset_id"] is None else str(row["asset_id"]),
        generation_task_id=(
            None if row["generation_task_id"] is None else str(row["generation_task_id"])
        ),
        project_id=None if row["project_id"] is None else str(row["project_id"]),
        person_id=None if row["person_id"] is None else str(row["person_id"]),
        title=_row_title(row, metadata),
        group=(
            group_override.strip()
            if isinstance(group_override, str) and group_override.strip()
            else str(row["base_group"])
        ),
        media_type=media_type,  # type: ignore[arg-type]
        source=str(row["source"]),  # type: ignore[arg-type]
        status=str(row["status"]),  # type: ignore[arg-type]
        delivery=str(row["delivery"]),  # type: ignore[arg-type]
        content_type=None if row["content_type"] is None else str(row["content_type"]),
        size_bytes=None if row["size_bytes"] is None else int(row["size_bytes"]),
        duration_seconds=_duration(metadata),
        created_at=str(row["created_at"]),
        hidden=bool(row["hidden"]),
        saved=ready and not direct,
        allowed_uses=uses,
        allowed_actions=actions,
    )


def list_materials(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    media_type: str | None,
    source: str | None,
    query: str | None,
    page: int,
    page_size: int,
) -> MaterialPage:
    total = _count_rows(
        conn,
        actor=actor,
        media_type=media_type,
        source=source,
        query=query,
    )
    start = (page - 1) * page_size
    rows = _read_rows(
        conn,
        actor=actor,
        media_type=media_type,
        source=source,
        query=query,
        include_hidden=False,
        limit=page_size,
        offset=start,
    )
    return MaterialPage(
        items=[material_item(row) for row in rows],
        page=page,
        page_size=page_size,
        total=total,
    )


def resolve_materials(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    material_ids: list[str],
) -> MaterialResolveResponse:
    deduplicated = list(dict.fromkeys(material_ids))
    parsed_ids = [parse_material_id(material_id) for material_id in deduplicated]
    rows = _read_rows(
        conn,
        actor=actor,
        media_type=None,
        source=None,
        query=None,
        include_hidden=True,
        limit=None,
        material_ids=parsed_ids,
    )
    by_id = {item.id: item for item in map(material_item, rows)}
    items = [by_id[item_id] for item_id in deduplicated if item_id in by_id]
    return MaterialResolveResponse(
        items=items,
        unavailable_ids=[item_id for item_id in deduplicated if item_id not in by_id],
    )


def require_material(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    material_id: str,
) -> MaterialItem:
    resolved = resolve_materials(conn, actor=actor, material_ids=[material_id])
    if not resolved.items:
        raise material_error(404, "MATERIAL_NOT_FOUND", "素材不存在。")
    return resolved.items[0]


def create_material_upload_intent(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    storage: StorageAdapter,
    request: MaterialUploadIntentRequest,
) -> MaterialUploadIntentResponse:
    require_not_auditor(
        conn,
        actor=actor,
        action="studio.material.upload_intent",
        entity_type="material",
        entity_id="new",
    )
    media_type, safe_suffix = validate_upload_request(
        filename=request.filename,
        content_type=request.content_type,
        size_bytes=request.size_bytes,
    )
    validate_audio_contract(
        media_type=media_type,
        audio_purpose=request.audio_purpose,
        duration_seconds=request.duration_seconds,
    )
    asset_id = str(uuid4())
    object_key = f"materials/{actor.id}/{asset_id}/original{safe_suffix}"
    intent = storage.create_upload_intent(
        object_key,
        content_type=request.content_type,
        expires_in=UPLOAD_INTENT_EXPIRES_IN,
    )
    metadata = {
        "upload_status": "PENDING",
        "object_key": intent.key,
        "original_filename": request.filename,
        "requested_size_bytes": request.size_bytes,
        "requested_content_type": request.content_type,
        "expected_sha256": request.sha256,
        "intent_expires_at": intent.expires_at.isoformat(),
    }
    if request.audio_purpose is not None:
        metadata["audio_purpose"] = request.audio_purpose
        metadata["duration_seconds"] = request.duration_seconds
    storage_uri = f"{storage.provider}://{storage.bucket}/{intent.key}"
    with conn:
        conn.execute(
            """
            INSERT INTO assets (
                id, project_id, kind, storage_uri, sha256, size_bytes,
                content_type, metadata_json, created_by_user_id
            ) VALUES (%s, NULL, %s, %s, '', 0, %s, %s, %s)
            """,
            (
                asset_id,
                ASSET_KIND_FOR_MEDIA[media_type],
                storage_uri,
                request.content_type,
                json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                actor.id,
            ),
        )
        if request.title or request.group:
            _upsert_preference(
                conn,
                actor_id=actor.id,
                source_type="asset",
                source_id=asset_id,
                title=request.title,
                group=request.group,
                hidden=False,
            )
        write_audit(
            conn,
            actor=actor,
            action="studio.material.upload_intent",
            entity_type="asset",
            entity_id=asset_id,
            metadata={"media_type": media_type, "size_bytes": request.size_bytes},
            commit=False,
        )
    return MaterialUploadIntentResponse(
        material_id=f"asset:{asset_id}",
        asset_id=asset_id,
        storage_key=intent.key,
        method=intent.method,
        url=intent.url,
        headers=intent.headers,
        expires_at=intent.expires_at.isoformat(),
    )


def prepare_material_upload(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    asset_id: str,
) -> PreparedMaterialUpload:
    row = require_asset_access(
        conn,
        actor=actor,
        asset_id=asset_id,
        action="studio.material.upload_complete",
    )
    kind = str(row["kind"])
    media_by_kind = {value: key for key, value in ASSET_KIND_FOR_MEDIA.items()}
    media_type = media_by_kind.get(kind)
    if media_type is None:
        raise material_error(409, "MATERIAL_UPLOAD_INVALID", "该素材不是通用上传任务。")
    metadata = _metadata(row["metadata_json"])
    requested_size: object
    if metadata.get("upload_status") == "READY":
        requested_size = int(row["size_bytes"])
    elif metadata.get("upload_status") == "PENDING":
        requested_size = metadata.get("requested_size_bytes")
    else:
        raise material_error(409, "MATERIAL_UPLOAD_INVALID", "上传状态无效。")
    if not isinstance(requested_size, int) or requested_size <= 0:
        raise material_error(409, "MATERIAL_UPLOAD_INVALID", "上传记录不完整。")
    storage_uri = str(row["storage_uri"])
    reference = storage_object_ref_from_uri(storage_uri)
    return PreparedMaterialUpload(
        asset_id=asset_id,
        owner_user_id=actor.id,
        storage_uri=storage_uri,
        storage_key=reference.key,
        media_type=media_type,
        content_type=str(row["content_type"]),
        requested_size_bytes=requested_size,
        expected_sha256=(
            str(metadata["expected_sha256"]) if metadata.get("expected_sha256") else None
        ),
        audio_purpose=(
            cast(AudioPurpose, metadata["audio_purpose"])
            if metadata.get("audio_purpose") in {"oral_audio", "voice_clone"}
            else None
        ),
        requested_duration_seconds=(
            float(metadata["duration_seconds"])
            if isinstance(metadata.get("duration_seconds"), (int, float))
            else None
        ),
    )


def probe_material_upload(
    prepared: PreparedMaterialUpload,
    *,
    storage: StorageAdapter,
) -> ProbedMaterialUpload:
    reference = storage_object_ref_from_uri(prepared.storage_uri)
    require_storage_match(storage, reference)
    stored = storage.head_object(prepared.storage_key)
    if stored is None:
        raise material_error(409, "MATERIAL_OBJECT_MISSING", "上传文件尚未到达存储。")
    if stored.size != prepared.requested_size_bytes:
        raise material_error(409, "MATERIAL_SIZE_MISMATCH", "上传文件大小不一致。")
    try:
        content = storage.get_object(prepared.storage_key)
    except OSError as exc:
        raise StorageBackendUnavailable("material object read failed") from exc
    if not _content_matches(prepared.media_type, content):
        raise material_error(422, "MATERIAL_CONTENT_INVALID", "文件内容与素材类型不匹配。")
    duration_seconds = None
    if prepared.media_type == "audio" and prepared.audio_purpose is not None:
        duration_seconds = probe_audio_duration(content)
        if duration_seconds is None:
            raise material_error(422, "MATERIAL_AUDIO_INVALID", "无法读取音频时长。")
        requested_duration = prepared.requested_duration_seconds
        if requested_duration is None or abs(duration_seconds - requested_duration) > 1:
            raise material_error(
                422,
                "MATERIAL_AUDIO_DURATION_MISMATCH",
                "音频实际时长与上传声明不一致。",
            )
    digest = hashlib.sha256(content).hexdigest()
    if prepared.expected_sha256 and digest != prepared.expected_sha256:
        raise material_error(409, "MATERIAL_HASH_MISMATCH", "上传文件校验失败。")
    return ProbedMaterialUpload(
        prepared=prepared,
        storage_uri=stored.uri,
        sha256=digest,
        size_bytes=stored.size,
        duration_seconds=duration_seconds,
    )


def persist_material_upload(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    probed: ProbedMaterialUpload,
) -> MaterialItem:
    prepared = prepare_material_upload(conn, actor=actor, asset_id=probed.prepared.asset_id)
    if prepared.storage_uri != probed.prepared.storage_uri:
        raise material_error(409, "MATERIAL_UPLOAD_CHANGED", "上传记录已变化。")
    row = conn.execute(
        "SELECT metadata_json FROM assets WHERE id = %s", (prepared.asset_id,)
    ).fetchone()
    metadata = _metadata(row["metadata_json"] if row is not None else "{}")
    metadata["upload_status"] = "READY"
    if probed.duration_seconds is not None:
        metadata["duration_seconds"] = probed.duration_seconds
        metadata["audio_duration_verified"] = True
        validate_audio_contract(
            media_type="audio",
            audio_purpose=metadata.get("audio_purpose"),
            duration_seconds=probed.duration_seconds,
        )
    with conn:
        conn.execute(
            """
            UPDATE assets
            SET storage_uri = %s, sha256 = %s, size_bytes = %s, metadata_json = %s
            WHERE id = %s
            """,
            (
                probed.storage_uri,
                probed.sha256,
                probed.size_bytes,
                json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                prepared.asset_id,
            ),
        )
        write_audit(
            conn,
            actor=actor,
            action="studio.material.upload_complete",
            entity_type="asset",
            entity_id=prepared.asset_id,
            metadata={"media_type": prepared.media_type, "size_bytes": probed.size_bytes},
            commit=False,
        )
        result = require_material(conn, actor=actor, material_id=f"asset:{prepared.asset_id}")
    return result


def update_material(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    material_id: str,
    request: MaterialUpdateRequest,
) -> MaterialItem:
    require_not_auditor(
        conn,
        actor=actor,
        action="studio.material.update",
        entity_type="material",
        entity_id=material_id,
    )
    source_type, source_id = parse_material_id(material_id)
    current = require_material(conn, actor=actor, material_id=material_id)
    if source_type == "generation" and (request.title is not None or request.group is not None):
        raise material_error(409, "MATERIAL_ACTION_UNAVAILABLE", "直出成片暂不支持重命名。")
    if request.title is None and request.group is None and request.hidden is None:
        raise material_error(422, "MATERIAL_UPDATE_EMPTY", "至少提交一项修改。")
    title = request.title.strip() if isinstance(request.title, str) else None
    group = request.group.strip() if isinstance(request.group, str) else None
    if request.title is not None and not title:
        raise material_error(422, "MATERIAL_TITLE_EMPTY", "素材名称不能为空。")
    if request.group is not None and not group:
        raise material_error(422, "MATERIAL_GROUP_EMPTY", "素材分组不能为空。")
    with conn:
        _upsert_preference(
            conn,
            actor_id=actor.id,
            source_type=source_type,
            source_id=source_id,
            title=title,
            group=group,
            hidden=current.hidden if request.hidden is None else request.hidden,
        )
        write_audit(
            conn,
            actor=actor,
            action="studio.material.update",
            entity_type="material",
            entity_id=current.id,
            metadata={
                "title_changed": request.title is not None,
                "group_changed": request.group is not None,
                "hidden_changed": request.hidden is not None,
            },
            commit=False,
        )
        result = require_material(conn, actor=actor, material_id=material_id)
    return result


def hide_material(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    material_id: str,
) -> None:
    require_not_auditor(
        conn,
        actor=actor,
        action="studio.material.hide",
        entity_type="material",
        entity_id=material_id,
    )
    current = require_material(conn, actor=actor, material_id=material_id)
    source_type, source_id = parse_material_id(material_id)
    with conn:
        _upsert_preference(
            conn,
            actor_id=actor.id,
            source_type=source_type,
            source_id=source_id,
            title=None,
            group=None,
            hidden=True,
            preserve_overrides=True,
        )
        write_audit(
            conn,
            actor=actor,
            action="studio.material.hide",
            entity_type="material",
            entity_id=current.id,
            metadata={},
            commit=False,
        )


def _upsert_preference(
    conn: BusinessConnection,
    *,
    actor_id: str,
    source_type: str,
    source_id: str,
    title: str | None,
    group: str | None,
    hidden: bool,
    preserve_overrides: bool = False,
) -> None:
    if preserve_overrides:
        conn.execute(
            """
            INSERT INTO studio_material_preferences (
                user_id, source_type, source_id, hidden
            ) VALUES (%s, %s, %s, 1)
            ON CONFLICT (user_id, source_type, source_id) DO UPDATE SET
                hidden = 1,
                updated_at = CURRENT_TIMESTAMP
            """,
            (actor_id, source_type, source_id),
        )
        return
    conn.execute(
        """
        INSERT INTO studio_material_preferences (
            user_id, source_type, source_id, title_override, group_override, hidden
        ) VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (user_id, source_type, source_id) DO UPDATE SET
            title_override = COALESCE(excluded.title_override,
                                      studio_material_preferences.title_override),
            group_override = COALESCE(excluded.group_override,
                                      studio_material_preferences.group_override),
            hidden = excluded.hidden,
            updated_at = CURRENT_TIMESTAMP
        """,
        (actor_id, source_type, source_id, title, group, 1 if hidden else 0),
    )


def _content_matches(media_type: MaterialMediaType, content: bytes) -> bool:
    if media_type == "image":
        return content.startswith(b"\x89PNG\r\n\x1a\n") or content.startswith(b"\xff\xd8\xff")
    if media_type == "audio":
        return content.startswith(b"ID3") or (
            len(content) >= 2 and content[0] == 0xFF and content[1] & 0xE0 == 0xE0
        )
    return len(content) >= 12 and content[4:8] == b"ftyp"
