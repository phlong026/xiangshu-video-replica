from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any, Protocol, cast
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

ANALYSIS_KIND = "analysis"
SHOT_CARD_KIND = "shot_card"
SCHEMA_VERSION = "b2.analysis.v1"


class ShotCard(BaseModel):
    model_config = ConfigDict(extra="forbid")

    shot_id: str = Field(min_length=1)
    start_time: float = Field(ge=0)
    end_time: float = Field(gt=0)
    shot_type: str = Field(min_length=1)
    composition: str = Field(min_length=1)
    camera_motion: str = Field(min_length=1)
    subject: str = Field(min_length=1)
    action: str = Field(min_length=1)
    scene: str = Field(min_length=1)
    spoken_text: str
    transition: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_time_range(self) -> ShotCard:
        if self.end_time <= self.start_time:
            raise ValueError("shot end_time must be greater than start_time")
        return self


class VideoAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1)
    duration_seconds: float = Field(gt=0)
    aspect_ratio: str = Field(min_length=1)
    resolution: str = Field(min_length=1)
    fps: float = Field(gt=0)
    theme: str = Field(min_length=1)
    visual_style: str = Field(min_length=1)
    pace: str = Field(min_length=1)
    camera_language: str = Field(min_length=1)
    original_script: str
    shots: list[ShotCard] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_shot_timeline(self) -> VideoAnalysis:
        previous_end = 0.0
        for shot in self.shots:
            if shot.start_time < previous_end:
                raise ValueError("shots must not overlap")
            if shot.end_time > self.duration_seconds:
                raise ValueError("shot end_time must not exceed duration_seconds")
            previous_end = shot.end_time
        return self


class ProviderResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str
    raw: dict[str, Any]


class VideoAnalysisProvider(Protocol):
    def analyze(self, *, video_uri: str, duration_seconds: float) -> ProviderResponse: ...

    def repair_json(self, *, invalid_json: str, error: str) -> ProviderResponse: ...


@dataclass(init=False)
class FakeGemini:
    analysis_json: str | None = None
    repair_json_text: str | None = None
    repair_calls: int = 0

    def __init__(self, analysis_json: str | None = None, repair_json: str | None = None) -> None:
        self.analysis_json = analysis_json
        self.repair_json_text = repair_json
        self.repair_calls = 0

    def analyze(self, *, video_uri: str, duration_seconds: float) -> ProviderResponse:
        text = self.analysis_json or json.dumps(
            _default_analysis_payload(duration_seconds), ensure_ascii=True, sort_keys=True
        )
        return ProviderResponse(
            text=text,
            raw={"provider": "fake_gemini", "video_uri": video_uri, "text": text},
        )

    def repair_json(self, *, invalid_json: str, error: str) -> ProviderResponse:
        self.repair_calls += 1
        if self.repair_json_text is None:
            text = json.dumps(_default_analysis_payload(10), ensure_ascii=True, sort_keys=True)
        else:
            text = self.repair_json_text
        return ProviderResponse(
            text=text,
            raw={
                "provider": "fake_gemini",
                "repaired_from": invalid_json,
                "error": error,
                "text": text,
            },
        )


class AnalysisResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    analysis: VideoAnalysis
    provider_response_ref: dict[str, Any]


def analyze_video(
    *,
    video_uri: str,
    video_duration_seconds: float,
    provider: VideoAnalysisProvider,
) -> AnalysisResult:
    response = provider.analyze(video_uri=video_uri, duration_seconds=video_duration_seconds)
    try:
        analysis = parse_analysis_response(response.text, duration_seconds=video_duration_seconds)
    except (json.JSONDecodeError, ValidationError) as exc:
        repaired = provider.repair_json(invalid_json=response.text, error=str(exc))
        analysis = parse_analysis_response(repaired.text, duration_seconds=video_duration_seconds)
        return AnalysisResult(
            analysis=analysis,
            provider_response_ref=_provider_response_ref(response.raw, repaired.raw),
        )

    return AnalysisResult(
        analysis=analysis,
        provider_response_ref=_provider_response_ref(response.raw, None),
    )


def parse_analysis_response(text: str, *, duration_seconds: float) -> VideoAnalysis:
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ValueError("analysis response must be a JSON object")
    payload["duration_seconds"] = duration_seconds
    return VideoAnalysis.model_validate(payload)


def create_analysis_version(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    asset_id: str,
    asset_uri: str,
    created_by_user_id: str,
    result: AnalysisResult,
) -> sqlite3.Row:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "analysis": result.analysis.model_dump(mode="json"),
        "source_asset": {"id": asset_id, "storage_uri": asset_uri},
        "provider_response_ref": result.provider_response_ref,
    }
    return insert_version(
        conn,
        project_id=project_id,
        asset_id=asset_id,
        kind=ANALYSIS_KIND,
        created_by_user_id=created_by_user_id,
        payload=payload,
    )


def create_shot_card_version(
    conn: sqlite3.Connection,
    *,
    analysis_version: sqlite3.Row,
    created_by_user_id: str,
    shots: list[ShotCard],
) -> sqlite3.Row:
    source_payload = json.loads(str(analysis_version["payload_json"]))
    analysis_payload = source_payload["analysis"]
    payload = {
        "schema_version": SCHEMA_VERSION,
        "source_analysis_version_id": str(analysis_version["id"]),
        "duration_seconds": analysis_payload["duration_seconds"],
        "shots": [shot.model_dump(mode="json") for shot in shots],
    }
    return insert_version(
        conn,
        project_id=str(analysis_version["project_id"]),
        asset_id=None
        if analysis_version["asset_id"] is None
        else str(analysis_version["asset_id"]),
        kind=SHOT_CARD_KIND,
        created_by_user_id=created_by_user_id,
        payload=payload,
    )


def validate_shot_cards(shots: list[dict[str, Any]], *, duration_seconds: float) -> list[ShotCard]:
    analysis = VideoAnalysis(
        summary="manual shot cards",
        duration_seconds=duration_seconds,
        aspect_ratio="manual",
        resolution="manual",
        fps=1,
        theme="manual",
        visual_style="manual",
        pace="manual",
        camera_language="manual",
        original_script="",
        shots=[ShotCard.model_validate(shot) for shot in shots],
    )
    return analysis.shots


def insert_version(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    asset_id: str | None,
    kind: str,
    created_by_user_id: str,
    payload: dict[str, Any],
) -> sqlite3.Row:
    version_number = next_version_number(conn, project_id=project_id, kind=kind)
    version_id = str(uuid4())
    conn.execute(
        """
        INSERT INTO versions (
            id,
            project_id,
            asset_id,
            kind,
            version_number,
            payload_json,
            created_by_user_id
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            version_id,
            project_id,
            asset_id,
            kind,
            version_number,
            json.dumps(payload, ensure_ascii=True, sort_keys=True),
            created_by_user_id,
        ),
    )
    conn.commit()
    return get_version(conn, version_id)


def get_version(conn: sqlite3.Connection, version_id: str) -> sqlite3.Row:
    row = conn.execute(
        """
        SELECT id, project_id, asset_id, kind, version_number, payload_json, created_by_user_id,
               created_at
        FROM versions
        WHERE id = ?
        """,
        (version_id,),
    ).fetchone()
    if row is None:
        raise LookupError("version not found")
    return cast(sqlite3.Row, row)


def next_version_number(conn: sqlite3.Connection, *, project_id: str, kind: str) -> int:
    row = conn.execute(
        """
        SELECT COALESCE(MAX(version_number), 0) + 1
        FROM versions
        WHERE project_id = ? AND kind = ?
        """,
        (project_id, kind),
    ).fetchone()
    return int(row[0])


def _provider_response_ref(
    raw_response: dict[str, Any],
    repaired_response: dict[str, Any] | None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "stored_as": "versions.payload_json",
        "raw": raw_response,
    }
    if repaired_response is not None:
        payload["repaired_raw"] = repaired_response
    return payload


def _default_analysis_payload(duration_seconds: float) -> dict[str, Any]:
    midpoint = duration_seconds / 2
    return {
        "summary": "FakeGemini video analysis",
        "duration_seconds": duration_seconds,
        "aspect_ratio": "9:16",
        "resolution": "1080x1920",
        "fps": 30,
        "theme": "人物口播",
        "visual_style": "写实竖屏",
        "pace": "快",
        "camera_language": "近景为主",
        "original_script": "",
        "shots": [
            {
                "shot_id": "S01",
                "start_time": 0,
                "end_time": midpoint,
                "shot_type": "近景",
                "composition": "人物居中",
                "camera_motion": "固定",
                "subject": "主讲人",
                "action": "看向镜头讲话",
                "scene": "室内",
                "spoken_text": "",
                "transition": "硬切",
            },
            {
                "shot_id": "S02",
                "start_time": midpoint,
                "end_time": duration_seconds,
                "shot_type": "中景",
                "composition": "三分法",
                "camera_motion": "轻微推进",
                "subject": "主讲人",
                "action": "继续口播",
                "scene": "室内",
                "spoken_text": "",
                "transition": "硬切",
            },
        ],
    }
