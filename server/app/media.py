from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from fastapi import HTTPException

from app.auth import CurrentUser
from app.permissions import (
    require_asset_access,
    require_not_auditor,
    require_project_access,
    write_audit,
)
from app.storage import StorageAdapter

ALLOWED_CONTENT_TYPES = {"video/mp4", "video/quicktime"}
ALLOWED_SUFFIXES = {".mp4", ".mov"}
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
MIN_DURATION_SECONDS = 4.0
MAX_DURATION_SECONDS = 15.0
UPLOAD_INTENT_EXPIRES_IN = timedelta(minutes=15)
FFPROBE_TIMEOUT_SECONDS = 5


@dataclass(frozen=True)
class VideoMetadata:
    duration_seconds: float


@dataclass(frozen=True)
class CreatedUploadIntent:
    asset_id: str
    project_id: str
    storage_key: str
    method: str
    url: str
    headers: dict[str, str]
    expires_at: str


@dataclass(frozen=True)
class CompletedUpload:
    asset_id: str
    project_id: str
    status: str
    storage_uri: str
    sha256: str
    size_bytes: int
    content_type: str
    metadata: VideoMetadata


class VideoProbe(Protocol):
    def probe(self, content: bytes, *, filename: str) -> VideoMetadata: ...


class VideoProbeUnavailable(RuntimeError):
    pass


class VideoProbeFailed(RuntimeError):
    pass


class FFprobeVideoProbe:
    def probe(self, content: bytes, *, filename: str) -> VideoMetadata:
        ffprobe = shutil.which("ffprobe")
        if ffprobe is None:
            raise VideoProbeUnavailable("ffprobe is required for video precheck")

        suffix = Path(filename).suffix.lower()
        with tempfile.NamedTemporaryFile(suffix=suffix) as temp:
            temp.write(content)
            temp.flush()
            command = [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "json",
                temp.name,
            ]
            try:
                result = subprocess.run(
                    command,
                    capture_output=True,
                    check=False,
                    text=True,
                    timeout=FFPROBE_TIMEOUT_SECONDS,
                )
            except subprocess.TimeoutExpired as exc:
                raise VideoProbeFailed("ffprobe timed out") from exc

        if result.returncode != 0:
            raise VideoProbeFailed("ffprobe could not read video metadata")

        try:
            payload = json.loads(result.stdout)
            duration = float(payload["format"]["duration"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise VideoProbeFailed("ffprobe returned invalid video metadata") from exc

        return VideoMetadata(duration_seconds=duration)


def create_upload_intent(
    conn: sqlite3.Connection,
    *,
    actor: CurrentUser,
    storage: StorageAdapter,
    project_id: str,
    filename: str,
    content_type: str,
    size_bytes: int,
) -> CreatedUploadIntent:
    require_not_auditor(
        conn,
        actor=actor,
        action="asset.upload_intent.create",
        entity_type="project",
        entity_id=project_id,
    )
    require_project_access(
        conn,
        actor=actor,
        project_id=project_id,
        action="asset.upload_intent.create",
    )
    validate_upload_request(filename=filename, content_type=content_type, size_bytes=size_bytes)

    asset_id = str(uuid4())
    storage_key = f"projects/{project_id}/uploads/{asset_id}/{Path(filename).name}"
    intent = storage.create_upload_intent(
        storage_key,
        content_type=content_type,
        expires_in=UPLOAD_INTENT_EXPIRES_IN,
    )
    storage_uri = f"{storage.provider}://{storage.bucket}/{intent.key}"

    with conn:
        conn.execute(
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
            (asset_id, project_id, "video", storage_uri, "", 0, content_type, actor.id),
        )

    write_audit(
        conn,
        actor=actor,
        action="asset.upload_intent.create",
        entity_type="asset",
        entity_id=asset_id,
        metadata={"project_id": project_id, "storage_key": intent.key},
    )
    return CreatedUploadIntent(
        asset_id=asset_id,
        project_id=project_id,
        storage_key=intent.key,
        method=intent.method,
        url=intent.url,
        headers=intent.headers,
        expires_at=intent.expires_at.isoformat(),
    )


def complete_upload(
    conn: sqlite3.Connection,
    *,
    actor: CurrentUser,
    storage: StorageAdapter,
    probe: VideoProbe,
    asset_id: str,
) -> CompletedUpload:
    require_not_auditor(
        conn,
        actor=actor,
        action="asset.upload_complete",
        entity_type="asset",
        entity_id=asset_id,
    )
    row = require_asset_access(
        conn,
        actor=actor,
        asset_id=asset_id,
        action="asset.upload_complete",
    )
    storage_key = storage_key_from_uri(str(row["storage_uri"]))
    stored = storage.head_object(storage_key)
    if stored is None:
        raise media_error(
            409,
            "UPLOAD_OBJECT_MISSING",
            "Uploaded object is not available yet. Retry completion after upload finishes.",
        )

    content_type = str(row["content_type"] or stored.content_type)
    validate_upload_request(
        filename=Path(storage_key).name,
        content_type=content_type,
        size_bytes=stored.size,
    )
    metadata = probe_video(probe, storage.get_object(storage_key), filename=Path(storage_key).name)
    validate_duration(metadata.duration_seconds)

    with conn:
        conn.execute(
            """
            UPDATE assets
            SET storage_uri = ?, sha256 = ?, size_bytes = ?, content_type = ?
            WHERE id = ?
            """,
            (stored.uri, stored.sha256, stored.size, content_type, asset_id),
        )

    write_audit(
        conn,
        actor=actor,
        action="asset.upload_complete",
        entity_type="asset",
        entity_id=asset_id,
        metadata={
            "project_id": str(row["project_id"]),
            "duration_seconds": metadata.duration_seconds,
        },
    )
    return CompletedUpload(
        asset_id=asset_id,
        project_id=str(row["project_id"]),
        status="uploaded",
        storage_uri=stored.uri,
        sha256=stored.sha256,
        size_bytes=stored.size,
        content_type=content_type,
        metadata=metadata,
    )


def validate_upload_request(*, filename: str, content_type: str, size_bytes: int) -> None:
    suffix = Path(filename).suffix.lower()
    if content_type not in ALLOWED_CONTENT_TYPES or suffix not in ALLOWED_SUFFIXES:
        raise media_error(
            415,
            "UNSUPPORTED_MEDIA_TYPE",
            "Only MP4 and MOV videos are accepted.",
        )
    if size_bytes > MAX_UPLOAD_BYTES:
        raise media_error(413, "MEDIA_TOO_LARGE", "Video upload must be 50MB or smaller.")
    if size_bytes < 0:
        raise media_error(422, "INVALID_MEDIA_SIZE", "Video size must be non-negative.")


def probe_video(probe: VideoProbe, content: bytes, *, filename: str) -> VideoMetadata:
    try:
        return probe.probe(content, filename=filename)
    except VideoProbeUnavailable as exc:
        raise media_error(
            503,
            "VIDEO_PROBE_UNAVAILABLE",
            "ffprobe is required for video precheck.",
        ) from exc
    except VideoProbeFailed as exc:
        raise media_error(
            422,
            "VIDEO_PROBE_FAILED",
            "Video metadata could not be read.",
        ) from exc


def validate_duration(duration_seconds: float) -> None:
    if duration_seconds < MIN_DURATION_SECONDS or duration_seconds > MAX_DURATION_SECONDS:
        raise media_error(
            422,
            "VIDEO_DURATION_OUT_OF_RANGE",
            "Video duration must be between 4 and 15 seconds.",
        )


def storage_key_from_uri(uri: str) -> str:
    marker = "://"
    if marker not in uri:
        raise ValueError(f"invalid storage uri: {uri}")
    path = uri.split(marker, 1)[1]
    if "/" not in path:
        raise ValueError(f"invalid storage uri: {uri}")
    return path.split("/", 1)[1]


def media_error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})
