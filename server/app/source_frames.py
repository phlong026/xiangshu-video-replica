from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast
from uuid import uuid4

from fastapi import HTTPException

from app.analysis import insert_version
from app.auth import CurrentUser
from app.media import is_reference_video_asset
from app.permissions import (
    require_asset_access,
    require_not_auditor,
    require_project_access,
    write_audit,
)
from app.storage import (
    StorageAdapter,
    StorageBackendUnavailable,
    require_storage_match,
    storage_object_ref_from_uri,
)

SOURCE_FRAME_CANDIDATES_KIND = "source_frame_candidates"
SOURCE_FRAME_SELECTION_KIND = "source_frame_selection"
SOURCE_FRAME_SCHEMA_VERSION = "b4.source-frame.v1"
SOURCE_FRAME_TIMESTAMPS_SECONDS = (0.5, 1.5, 2.5)
FFMPEG_TIMEOUT_SECONDS = 15


@dataclass(frozen=True)
class ExtractedSourceFrame:
    timestamp_seconds: float
    image: bytes
    technical_score: float | None = None


class SourceFrameExtractor(Protocol):
    def extract(
        self,
        content: bytes,
        *,
        filename: str,
        timestamps_seconds: tuple[float, ...],
    ) -> list[ExtractedSourceFrame]: ...


class SourceFrameExtractorUnavailable(RuntimeError):
    pass


class SourceFrameExtractionFailed(RuntimeError):
    pass


class FFmpegSourceFrameExtractor:
    def extract(
        self,
        content: bytes,
        *,
        filename: str,
        timestamps_seconds: tuple[float, ...],
    ) -> list[ExtractedSourceFrame]:
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg is None:
            raise SourceFrameExtractorUnavailable("ffmpeg is required for source frame extraction")

        suffix = Path(filename).suffix.lower()
        with tempfile.NamedTemporaryFile(suffix=suffix) as video_file:
            video_file.write(content)
            video_file.flush()
            frames = []
            for timestamp in timestamps_seconds:
                image = self._extract_jpeg(ffmpeg, video_file.name, timestamp)
                grayscale = self._extract_grayscale(ffmpeg, video_file.name, timestamp)
                frames.append(
                    ExtractedSourceFrame(
                        timestamp_seconds=timestamp,
                        image=image,
                        technical_score=score_grayscale_frame(grayscale),
                    )
                )
            return frames

    def _extract_jpeg(self, ffmpeg: str, source_path: str, timestamp: float) -> bytes:
        command = [
            ffmpeg,
            "-v",
            "error",
            "-ss",
            str(timestamp),
            "-i",
            source_path,
            "-frames:v",
            "1",
            "-f",
            "image2",
            "-vcodec",
            "mjpeg",
            "pipe:1",
        ]
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                check=False,
                timeout=FFMPEG_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as exc:
            raise SourceFrameExtractionFailed("ffmpeg timed out") from exc
        if result.returncode != 0 or not result.stdout:
            raise SourceFrameExtractionFailed("ffmpeg could not extract a source frame")
        return result.stdout

    def _extract_grayscale(self, ffmpeg: str, source_path: str, timestamp: float) -> bytes:
        command = [
            ffmpeg,
            "-v",
            "error",
            "-ss",
            str(timestamp),
            "-i",
            source_path,
            "-frames:v",
            "1",
            "-vf",
            "scale=160:-2,format=gray",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "gray",
            "pipe:1",
        ]
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                check=False,
                timeout=FFMPEG_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as exc:
            raise SourceFrameExtractionFailed("ffmpeg timed out") from exc
        if result.returncode != 0 or not result.stdout:
            raise SourceFrameExtractionFailed("ffmpeg could not score a source frame")
        return result.stdout


def score_grayscale_frame(pixels: bytes) -> float:
    if len(pixels) < 2:
        return 0.0
    count = len(pixels)
    average = sum(pixels) / count
    variance = sum((value - average) ** 2 for value in pixels) / count
    contrast = min(1.0, variance**0.5 / 64)
    detail = sum(abs(left - right) for left, right in zip(pixels, pixels[1:])) / (count - 1)
    sharpness = min(1.0, detail / 32)
    exposure = max(0.0, 1.0 - abs(average - 127.5) / 127.5)
    return float(round(0.6 * sharpness + 0.25 * contrast + 0.15 * exposure, 3))


def extract_source_frame_candidates(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    asset_id: str,
    actor: CurrentUser,
    storage: StorageAdapter,
    extractor: SourceFrameExtractor,
    timestamps_seconds: tuple[float, ...] = SOURCE_FRAME_TIMESTAMPS_SECONDS,
) -> sqlite3.Row:
    require_not_auditor(
        conn,
        actor=actor,
        action="source_frame.extract",
        entity_type="project",
        entity_id=project_id,
    )
    require_project_access(conn, actor=actor, project_id=project_id, action="source_frame.extract")
    asset = require_asset_access(
        conn,
        actor=actor,
        asset_id=asset_id,
        action="source_frame.extract",
    )
    if str(asset["project_id"]) != project_id:
        raise source_frame_error(
            400,
            "ASSET_PROJECT_MISMATCH",
            "Asset does not belong to the requested project.",
        )
    if not is_reference_video_asset(asset):
        raise source_frame_error(
            422,
            "SOURCE_FRAME_ASSET_NOT_REFERENCE_VIDEO",
            "Source frames require a reference video asset.",
        )
    if not str(asset["sha256"]) or int(asset["size_bytes"]) <= 0:
        raise source_frame_error(
            409,
            "REFERENCE_VIDEO_NOT_READY",
            "Reference video upload is not ready for source frame extraction.",
        )

    try:
        reference = storage_object_ref_from_uri(str(asset["storage_uri"]))
        require_storage_match(storage, reference)
        video = storage.get_object(reference.key)
    except (KeyError, OSError, StorageBackendUnavailable, ValueError) as exc:
        raise source_frame_error(
            503,
            "SOURCE_FRAME_STORAGE_UNAVAILABLE",
            "Reference video storage is temporarily unavailable.",
        ) from exc

    try:
        frames = extractor.extract(
            video,
            filename=Path(reference.key).name,
            timestamps_seconds=timestamps_seconds,
        )
    except SourceFrameExtractorUnavailable as exc:
        raise source_frame_error(
            503,
            "SOURCE_FRAME_EXTRACTOR_UNAVAILABLE",
            "ffmpeg is required for source frame extraction.",
        ) from exc
    except SourceFrameExtractionFailed as exc:
        raise source_frame_error(
            422,
            "SOURCE_FRAME_EXTRACTION_FAILED",
            "No usable source frame could be extracted from the reference video.",
        ) from exc

    if not frames:
        raise source_frame_error(
            422,
            "SOURCE_FRAME_EXTRACTION_FAILED",
            "No usable source frame could be extracted from the reference video.",
        )
    if any(not frame.image for frame in frames):
        raise source_frame_error(
            422,
            "SOURCE_FRAME_EXTRACTION_FAILED",
            "No usable source frame could be extracted from the reference video.",
        )

    created_assets: list[tuple[str, str]] = []
    try:
        candidates = []
        for frame in frames:
            frame_asset_id = str(uuid4())
            storage_key = f"projects/{project_id}/source-frames/{frame_asset_id}.jpg"
            created_assets.append((frame_asset_id, storage_key))
            stored = storage.put_object(storage_key, frame.image, content_type="image/jpeg")
            candidates.append(
                {
                    "asset_id": frame_asset_id,
                    "timestamp_seconds": frame.timestamp_seconds,
                    "score": frame.technical_score,
                    "selection_reason": "技术质量分数基于细节、对比度和曝光。",
                    "storage_uri": stored.uri,
                    "sha256": stored.sha256 or hashlib.sha256(frame.image).hexdigest(),
                    "size_bytes": stored.size,
                }
            )
        candidates.sort(key=technical_score_of_candidate, reverse=True)

        with conn:
            for candidate in candidates:
                conn.execute(
                    """
                    INSERT INTO assets (
                        id, project_id, kind, storage_uri, sha256, size_bytes, content_type,
                        created_by_user_id
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        candidate["asset_id"],
                        project_id,
                        "source_frame",
                        candidate["storage_uri"],
                        candidate["sha256"],
                        candidate["size_bytes"],
                        "image/jpeg",
                        actor.id,
                    ),
                )
            row = insert_version(
                conn,
                project_id=project_id,
                asset_id=asset_id,
                kind=SOURCE_FRAME_CANDIDATES_KIND,
                created_by_user_id=actor.id,
                payload={
                    "schema_version": SOURCE_FRAME_SCHEMA_VERSION,
                    "source_asset_id": asset_id,
                    "requested_timestamps_seconds": list(timestamps_seconds),
                    "candidates": candidates,
                },
            )
    except sqlite3.Error as exc:
        delete_created_source_frames(storage, created_assets, actor_id=actor.id)
        raise source_frame_error(
            500,
            "SOURCE_FRAME_PERSIST_FAILED",
            "Source frame candidates could not be saved. Extract them again.",
        ) from exc
    except (OSError, StorageBackendUnavailable, ValueError) as exc:
        delete_created_source_frames(storage, created_assets, actor_id=actor.id)
        raise source_frame_error(
            503,
            "SOURCE_FRAME_STORAGE_UNAVAILABLE",
            "Source frame storage is temporarily unavailable.",
        ) from exc

    write_audit(
        conn,
        actor=actor,
        action="source_frame.extract",
        entity_type="version",
        entity_id=str(row["id"]),
        metadata={"project_id": project_id, "source_asset_id": asset_id},
    )
    return row


def delete_created_source_frames(
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


def technical_score_of_candidate(candidate: dict[str, object]) -> float:
    score = candidate["score"]
    return float(score) if isinstance(score, int | float) else -1.0


def confirm_source_frame(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    source_frame_asset_id: str,
    actor: CurrentUser,
    character_features: dict[str, object] | None = None,
) -> sqlite3.Row:
    require_not_auditor(
        conn,
        actor=actor,
        action="source_frame.confirm",
        entity_type="project",
        entity_id=project_id,
    )
    require_project_access(conn, actor=actor, project_id=project_id, action="source_frame.confirm")
    candidate_version = latest_version(conn, project_id, SOURCE_FRAME_CANDIDATES_KIND)
    if candidate_version is None:
        raise source_frame_error(
            409,
            "SOURCE_FRAME_CANDIDATES_NOT_FOUND",
            "Extract source frame candidates before confirming one.",
        )
    payload = json.loads(str(candidate_version["payload_json"]))
    candidates = payload.get("candidates")
    if not isinstance(candidates, list):
        raise source_frame_error(
            409,
            "SOURCE_FRAME_CANDIDATES_INVALID",
            "Source frame candidates are invalid. Extract them again.",
        )
    candidate = next(
        (
            value
            for value in candidates
            if isinstance(value, dict) and value.get("asset_id") == source_frame_asset_id
        ),
        None,
    )
    if candidate is None:
        raise source_frame_error(
            422,
            "SOURCE_FRAME_CANDIDATE_NOT_FOUND",
            "The selected source frame is not in the latest candidate set.",
        )
    frame_asset = require_asset_access(
        conn,
        actor=actor,
        asset_id=source_frame_asset_id,
        action="source_frame.confirm",
    )
    if str(frame_asset["project_id"]) != project_id or str(frame_asset["kind"]) != "source_frame":
        raise source_frame_error(
            422,
            "SOURCE_FRAME_CANDIDATE_NOT_FOUND",
            "The selected source frame is not valid for this project.",
        )

    selection_payload: dict[str, object] = {
        "schema_version": SOURCE_FRAME_SCHEMA_VERSION,
        "source_frame_candidates_version_id": str(candidate_version["id"]),
        "source_frame_asset_id": source_frame_asset_id,
        "timestamp_seconds": candidate["timestamp_seconds"],
    }
    if character_features is not None:
        selection_payload["character_features"] = character_features

    row = insert_version(
        conn,
        project_id=project_id,
        asset_id=source_frame_asset_id,
        kind=SOURCE_FRAME_SELECTION_KIND,
        created_by_user_id=actor.id,
        payload=selection_payload,
    )
    write_audit(
        conn,
        actor=actor,
        action="source_frame.confirm",
        entity_type="version",
        entity_id=str(row["id"]),
        metadata={"project_id": project_id, "source_frame_asset_id": source_frame_asset_id},
    )
    return row


def latest_version(
    conn: sqlite3.Connection,
    project_id: str,
    kind: str,
) -> sqlite3.Row | None:
    row = conn.execute(
        """
        SELECT id, project_id, asset_id, kind, version_number, payload_json, created_by_user_id,
               created_at
        FROM versions
        WHERE project_id = ? AND kind = ?
        ORDER BY version_number DESC
        LIMIT 1
        """,
        (project_id, kind),
    ).fetchone()
    return None if row is None else cast(sqlite3.Row, row)


def source_frame_error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})
