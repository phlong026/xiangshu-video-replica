from __future__ import annotations

import json
import logging
import math
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol, cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.db_portable import BusinessConnection

ANALYSIS_KIND = "analysis"
SHOT_CARD_KIND = "shot_card"
SCHEMA_VERSION = "b3.analysis.v1"
APILIO_DEFAULT_BASE_URL = "https://api.apilio.ai"
APILIO_GEMINI_MODEL = "gemini-3.1-pro-preview"

# 结构化运动枚举：拆解结果必须对“人物是否在动、机位是否在动”显式表态，
# 下游（H3 Prompt 编译）据此确定性渲染运动指令，避免“边走边说”被
# 概括成“站着说话”后生成的人物僵立原地。
SUBJECT_MOTION_STATES = (
    "STATIC",
    "WALKING",
    "RUNNING",
    "TURNING",
    "GESTURING_ONLY",
    "OBJECT_MOTION",
    "NO_PERSON",
)
SUBJECT_DIRECTIONS = (
    "toward_camera",
    "away_from_camera",
    "left",
    "right",
    "lateral",
    "in_place",
    "none",
)
MOTION_CAMERA_MODES = (
    "STATIC",
    "PUSH_IN",
    "PULL_BACK",
    "HANDHELD_TRACKING",
    "PAN",
    "TILT",
    "FOLLOW",
)

# Failure phases let the desktop tell "the network hiccuped, retry" apart from
# "the model answered with something we cannot use".
REQUEST_FAILURE_PHASE = "request"
NETWORK_FAILURE_PHASE = "network"
HTTP_FAILURE_PHASE = "http"
RESPONSE_FAILURE_PHASE = "response"
# Provider timelines are commonly rounded to 1–3 decimal places while ffprobe
# reports microsecond precision.  A small frame-scale tolerance absorbs that
# harmless representation drift without accepting materially invalid timelines.
TIMELINE_ROUNDING_TOLERANCE_SECONDS = 0.05

logger = logging.getLogger(__name__)


class ShotMotion(BaseModel):
    """镜头运动的结构化描述：人物运动 / 机位运动 / 相对运动三分。"""

    model_config = ConfigDict(extra="forbid")

    subject_motion_state: Literal[
        "STATIC",
        "WALKING",
        "RUNNING",
        "TURNING",
        "GESTURING_ONLY",
        "OBJECT_MOTION",
        "NO_PERSON",
    ]
    subject_direction: Literal[
        "toward_camera",
        "away_from_camera",
        "left",
        "right",
        "lateral",
        "in_place",
        "none",
    ]
    subject_displacement: str = Field(min_length=1)
    hand_action: str = Field(min_length=1)
    camera_motion: Literal[
        "STATIC",
        "PUSH_IN",
        "PULL_BACK",
        "HANDHELD_TRACKING",
        "PAN",
        "TILT",
        "FOLLOW",
    ]
    relative_motion: str = Field(min_length=1)


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
    # 旧版本拆解结果与手动保存的镜头卡没有 motion；缺失时 H3 Prompt
    # 编译回退到 action 文本拼接（行为不劣化），新生成的分析必须携带。
    motion: ShotMotion | None = None

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
    requires_https_video_url: bool

    def analyze(self, *, video_uri: str, duration_seconds: float) -> ProviderResponse: ...

    def repair_json(self, *, invalid_json: str, error: str) -> ProviderResponse: ...


class AnalysisProviderFailed(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        http_status: int | None = None,
        failure_phase: str = RESPONSE_FAILURE_PHASE,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.http_status = http_status
        self.failure_phase = failure_phase
        self.retryable = retryable


class ApilioChatTransport(Protocol):
    def post(
        self, url: str, *, headers: Mapping[str, str], body: bytes
    ) -> tuple[bytes, Mapping[str, str]]: ...


class UrllibApilioChatTransport:
    # Measured on a real 15s reference video: 71–91s per call for the full
    # shot-card prompt. 90s cut real traffic in half; 240s leaves headroom for
    # slower days while the desktop timeout (300s) still bounds the wait.
    def __init__(self, *, timeout_seconds: float = 240.0) -> None:
        self.timeout_seconds = timeout_seconds

    def post(
        self, url: str, *, headers: Mapping[str, str], body: bytes
    ) -> tuple[bytes, Mapping[str, str]]:
        try:
            request = Request(url, data=body, headers=dict(headers), method="POST")
            with urlopen(request, timeout=self.timeout_seconds) as response:  # noqa: S310
                return response.read(), dict(response.headers.items())
        except HTTPError as exc:
            logger.warning("Apilio video analysis request failed with HTTP status %s", exc.code)
            raise AnalysisProviderFailed(
                f"Apilio returned HTTP {exc.code}",
                http_status=exc.code,
                failure_phase=HTTP_FAILURE_PHASE,
                retryable=exc.code == 429 or exc.code >= 500,
            ) from exc
        except (TimeoutError, URLError, OSError) as exc:
            # Only the exception class name is carried forward: the original reason can
            # embed the signed video URL or the request headers.
            reason = type(exc).__name__
            logger.warning("Apilio video analysis request failed: %s", reason)
            raise AnalysisProviderFailed(
                f"Apilio video analysis request failed ({reason})",
                failure_phase=NETWORK_FAILURE_PHASE,
                retryable=True,
            ) from exc


class ApilioGemini:
    """Apilio's OpenAI-compatible Gemini adapter for a signed reference-video URL."""

    requires_https_video_url = True

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = APILIO_DEFAULT_BASE_URL,
        model: str = APILIO_GEMINI_MODEL,
        transport: ApilioChatTransport | None = None,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.transport = transport or UrllibApilioChatTransport()

    def analyze(self, *, video_uri: str, duration_seconds: float) -> ProviderResponse:
        if not is_https_video_url(video_uri):
            raise AnalysisProviderFailed(
                "Gemini analysis requires an HTTPS signed video URL",
                failure_phase=REQUEST_FAILURE_PHASE,
            )
        payload = {
            "model": self.model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": analysis_instruction(duration_seconds)},
                        {"type": "image_url", "image_url": {"url": video_uri}},
                    ],
                }
            ],
        }
        text, raw = self._complete(payload)
        return ProviderResponse(text=text, raw=raw)

    def repair_json(self, *, invalid_json: str, error: str) -> ProviderResponse:
        payload = {
            "model": self.model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "user",
                    "content": (
                        "修复下面的视频拆解 JSON，只返回符合要求结构的合法 JSON，不要解释。"
                        f"校验错误：{error}。"
                        f"待修复的 JSON：{invalid_json}"
                    ),
                }
            ],
        }
        text, raw = self._complete(payload)
        return ProviderResponse(text=text, raw=raw)

    def _complete(self, payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        raw_body, _ = self.transport.post(
            f"{self.base_url}/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            body=json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode(),
        )
        try:
            response = json.loads(raw_body.decode("utf-8"))
            content = response["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AnalysisProviderFailed("Apilio returned an invalid Gemini response") from exc
        if not isinstance(content, str) or not content.strip():
            raise AnalysisProviderFailed("Apilio Gemini response is missing analysis content")
        return content, {
            "provider": "apilio_gemini",
            "model": self.model,
            "response_id": response.get("id") if isinstance(response.get("id"), str) else None,
        }


@dataclass(init=False)
class FakeGemini:
    analysis_json: str | None = None
    repair_json_text: str | None = None
    repair_calls: int = 0
    requires_https_video_url = False

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
            raw={"provider": "fake_gemini", "text": text},
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
    except (json.JSONDecodeError, ValidationError, ValueError) as exc:
        logger.warning(
            "Video analysis response validation failed before repair: %s",
            _validation_diagnostic(exc),
        )
        repaired = provider.repair_json(
            invalid_json=response.text,
            error=(
                f"verified duration_seconds={_canonical_duration_text(video_duration_seconds)}; "
                f"{exc}"
            ),
        )
        try:
            analysis = parse_analysis_response(
                repaired.text, duration_seconds=video_duration_seconds
            )
        except (json.JSONDecodeError, ValidationError, ValueError) as repair_exc:
            logger.warning(
                "Video analysis response validation failed after repair: %s",
                _validation_diagnostic(repair_exc),
            )
            raise AnalysisProviderFailed(
                "Provider returned invalid JSON even after a repair attempt"
            ) from repair_exc
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
    _discard_invalid_optional_motion(payload)
    _normalize_timeline_rounding(payload, duration_seconds=duration_seconds)
    payload["duration_seconds"] = duration_seconds
    return VideoAnalysis.model_validate(payload)


def _normalize_timeline_rounding(
    payload: dict[str, Any],
    *,
    duration_seconds: float,
) -> None:
    """Snap harmless provider rounding to the ffprobe-canonical timeline.

    The prompt and ffprobe can represent the same instant with different
    decimal precision (for example 12.067 versus 12.066667).  Only boundaries
    within a small frame-scale window are adjusted; larger overlaps and overruns remain schema
    errors so genuinely broken analyses still fail closed.
    """
    shots = payload.get("shots")
    if not isinstance(shots, list) or not shots:
        return

    previous_end = 0.0
    for index, shot in enumerate(shots):
        if not isinstance(shot, dict):
            continue
        start_time = _finite_number(shot.get("start_time"))
        if start_time is not None:
            expected_start = 0.0 if index == 0 else previous_end
            if abs(start_time - expected_start) <= TIMELINE_ROUNDING_TOLERANCE_SECONDS:
                shot["start_time"] = expected_start
        end_time = _finite_number(shot.get("end_time"))
        if end_time is not None:
            if index == len(shots) - 1 and (
                abs(end_time - duration_seconds) <= TIMELINE_ROUNDING_TOLERANCE_SECONDS
            ):
                end_time = duration_seconds
                shot["end_time"] = duration_seconds
            previous_end = end_time


def _finite_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _canonical_duration_text(duration_seconds: float) -> str:
    return f"{duration_seconds:.6f}".rstrip("0").rstrip(".")


def _validation_diagnostic(exc: Exception) -> str:
    """Return field/type-only diagnostics without logging provider content."""
    if isinstance(exc, ValidationError):
        issues: list[str] = []
        for item in exc.errors(include_url=False, include_input=False)[:8]:
            location = ".".join(str(part) for part in item.get("loc", ())) or "root"
            issues.append(f"{location}:{item.get('type', 'validation_error')}")
        return "validation:" + ",".join(issues)
    if isinstance(exc, json.JSONDecodeError):
        return f"json_decode:line={exc.lineno}:column={exc.colno}"
    return type(exc).__name__


def _discard_invalid_optional_motion(payload: dict[str, Any]) -> None:
    """Keep b2 responses usable when the provider omits or misshapes b3 motion data.

    The motion extension improves video prompts but is not part of the proven legacy
    analysis contract.  Invalid motion must therefore not trigger a second paid
    provider request or discard an otherwise valid analysis response.
    """
    shots = payload.get("shots")
    if not isinstance(shots, list):
        return
    for shot in shots:
        if not isinstance(shot, dict) or "motion" not in shot:
            continue
        try:
            ShotMotion.model_validate(shot["motion"])
        except ValidationError:
            shot.pop("motion", None)


def is_https_video_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme == "https" and bool(parsed.hostname)


def analysis_instruction(duration_seconds: float) -> str:
    canonical_duration = _canonical_duration_text(duration_seconds)
    return (
        "分析这条参考短视频，只返回合法 JSON 对象，不要 markdown 代码块。\n"
        "JSON 结构：summary, aspect_ratio, resolution, fps, theme, visual_style, "
        "pace, camera_language, original_script, shots。\n"
        "shots 内每个镜头必须包含 shot_id, start_time, end_time, shot_type, "
        "composition, camera_motion, subject, action, scene, spoken_text, "
        "transition, motion。镜头时间覆盖全片且互不重叠。\n"
        f"已验证的视频总时长为 {canonical_duration} 秒；最后一个镜头的 end_time "
        f"必须精确等于 {canonical_duration}。\n"
        "除 shot_id 和枚举值外，所有文本字段一律用中文填写。\n"
        "\n"
        "motion 是结构化运动描述，每个镜头都必须完整填写以下六个字段：\n"
        "- subject_motion_state（人物运动状态，枚举）：STATIC 静止 / WALKING 行走 / "
        "RUNNING 跑动 / TURNING 转身 / GESTURING_ONLY 仅手势站位不动 / "
        "OBJECT_MOTION 仅物体运动 / NO_PERSON 无人物；\n"
        "- subject_direction（人物位移方向，枚举）：toward_camera 向镜头 / "
        "away_from_camera 背离镜头 / left 向画面左 / right 向画面右 / lateral 横向 / "
        "in_place 原地 / none 无；\n"
        "- subject_displacement（位移幅度，中文）：如“向镜头走近两三步”“无位移”；\n"
        "- hand_action（左右手动作，中文）：如“双臂随步态交替自然摆动，不指点不握拳”；\n"
        "- camera_motion（机位运动，枚举）：STATIC 固定 / PUSH_IN 推近 / PULL_BACK 拉远 / "
        "HANDHELD_TRACKING 手持跟拍 / PAN 横摇 / TILT 纵摇 / FOLLOW 跟随；机位在动时"
        "禁止填 STATIC；\n"
        "- relative_motion（人物与摄影机相对运动，中文）：如“人物逐渐靠近镜头，画面占比增大”。\n"
        "\n"
        "关键规则：\n"
        "1. 人物在镜头内移动（行走、跑动、转身）时，subject_motion_state 必须选对应"
        "运动状态，action 必须写明运动方向与幅度；不得把移动中的人物概括成“说话”或"
        "“站立”，也不得把运动镜头写成固定机位。\n"
        "2. 人物确实静止时选 STATIC，不得凭空增加运动。\n"
        "3. 无人物出镜的镜头 subject_motion_state 选 NO_PERSON 或 OBJECT_MOTION，"
        "文本字段写“无人物出镜”。"
    )


def create_analysis_version(
    conn: BusinessConnection,
    *,
    project_id: str,
    asset_id: str,
    asset_uri: str,
    created_by_user_id: str,
    result: AnalysisResult,
) -> sqlite3.Row:
    return insert_version(
        conn,
        project_id=project_id,
        asset_id=asset_id,
        kind=ANALYSIS_KIND,
        created_by_user_id=created_by_user_id,
        payload=analysis_version_payload(
            asset_id=asset_id,
            asset_uri=asset_uri,
            result=result,
        ),
    )


def create_or_recover_analysis_version(
    conn: BusinessConnection,
    *,
    project_id: str,
    asset_id: str,
    asset_uri: str,
    created_by_user_id: str,
    result: AnalysisResult,
) -> tuple[sqlite3.Row, bool]:
    """Create one analysis version, or reuse the winner of a concurrent recovery."""
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = find_analysis_version_for_asset(
            conn,
            project_id=project_id,
            asset_id=asset_id,
        )
        if existing is not None:
            conn.commit()
            return existing, False
        row = _insert_version(
            conn,
            project_id=project_id,
            asset_id=asset_id,
            kind=ANALYSIS_KIND,
            created_by_user_id=created_by_user_id,
            payload=analysis_version_payload(
                asset_id=asset_id,
                asset_uri=asset_uri,
                result=result,
            ),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return row, True


def find_analysis_version_for_asset(
    conn: BusinessConnection,
    *,
    project_id: str,
    asset_id: str,
) -> sqlite3.Row | None:
    row = conn.execute(
        """
        SELECT id, project_id, asset_id, kind, version_number, payload_json,
               created_by_user_id, created_at
        FROM versions
        WHERE project_id = %s AND asset_id = %s AND kind = %s
        ORDER BY version_number DESC
        LIMIT 1
        """,
        (project_id, asset_id, ANALYSIS_KIND),
    ).fetchone()
    return None if row is None else cast(sqlite3.Row, row)


def analysis_version_payload(
    *,
    asset_id: str,
    asset_uri: str,
    result: AnalysisResult,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "analysis": result.analysis.model_dump(mode="json"),
        "source_asset": {"id": asset_id, "storage_uri": asset_uri},
        "provider_response_ref": result.provider_response_ref,
    }


def create_shot_card_version(
    conn: BusinessConnection,
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
    conn: BusinessConnection,
    *,
    project_id: str,
    asset_id: str | None,
    kind: str,
    created_by_user_id: str,
    payload: dict[str, Any],
) -> sqlite3.Row:
    row = _insert_version(
        conn,
        project_id=project_id,
        asset_id=asset_id,
        kind=kind,
        created_by_user_id=created_by_user_id,
        payload=payload,
    )
    conn.commit()
    return row


def _insert_version(
    conn: BusinessConnection,
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
        VALUES (%s, %s, %s, %s, %s, %s, %s)
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
    return get_version(conn, version_id)


def get_version(conn: BusinessConnection, version_id: str) -> sqlite3.Row:
    row = conn.execute(
        """
        SELECT id, project_id, asset_id, kind, version_number, payload_json, created_by_user_id,
               created_at
        FROM versions
        WHERE id = %s
        """,
        (version_id,),
    ).fetchone()
    if row is None:
        raise LookupError("version not found")
    return cast(sqlite3.Row, row)


def next_version_number(conn: BusinessConnection, *, project_id: str, kind: str) -> int:
    row = conn.execute(
        """
        SELECT COALESCE(MAX(version_number), 0) + 1
        FROM versions
        WHERE project_id = %s AND kind = %s
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
        "summary": "FakeGemini 演示拆解（未配置真实分析服务）",
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
                "shot_type": "中景",
                "composition": "人物居中",
                "camera_motion": "手持跟拍",
                "subject": "主讲人",
                "action": "边向镜头走近边口播",
                "scene": "室内",
                "spoken_text": "",
                "transition": "硬切",
                "motion": {
                    "subject_motion_state": "WALKING",
                    "subject_direction": "toward_camera",
                    "subject_displacement": "向镜头走近两三步",
                    "hand_action": "双臂随步态交替自然摆动，不指点不握拳",
                    "camera_motion": "HANDHELD_TRACKING",
                    "relative_motion": "人物逐渐靠近镜头，画面占比增大",
                },
            },
            {
                "shot_id": "S02",
                "start_time": midpoint,
                "end_time": duration_seconds,
                "shot_type": "近景",
                "composition": "三分法",
                "camera_motion": "缓慢推近",
                "subject": "主讲人",
                "action": "站位固定，边做讲解手势边口播",
                "scene": "室内",
                "spoken_text": "",
                "transition": "硬切",
                "motion": {
                    "subject_motion_state": "GESTURING_ONLY",
                    "subject_direction": "in_place",
                    "subject_displacement": "无位移",
                    "hand_action": "双手在胸前做自然讲解手势",
                    "camera_motion": "PUSH_IN",
                    "relative_motion": "镜头缓慢推近，人物站位不变",
                },
            },
        ],
    }
