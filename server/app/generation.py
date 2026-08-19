from __future__ import annotations

import base64
import binascii
import hashlib
import ipaddress
import json
import logging
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from math import floor
from pathlib import Path
from typing import Any, Literal, Protocol, cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen
from uuid import uuid4

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.analysis import get_version, insert_version
from app.auth import CurrentUser
from app.internal_billing import (
    InsufficientCreditsError,
    finalize_internal_billing,
    reserve_internal_billing,
)
from app.permissions import (
    insert_audit,
    require_asset_access,
    require_not_auditor,
    require_project_access,
    write_audit,
)
from app.settings import SettingsRepository, SettingsUnavailableError
from app.storage import (
    StorageAdapter,
    StorageBackendUnavailable,
    StoragePermissionError,
    StoredObject,
    cloud_storage_config_from_settings,
    require_storage_match,
    storage_object_ref_from_uri,
)

SCRIPT_KIND = "script"
H3_PROMPT_KIND = "h3_prompt"
GENERATION_SCHEMA_VERSION = "c.generation.v1"
H3_PROMPT_TEMPLATE_VERSION = "h3.prompt.v4"
H3_PROMPT_TEMPLATE_SPEC = (
    (
        "intro",
        "生成一条 {duration_seconds} 秒、{resolution}、写实短视频，从提供的首帧自然开始；"
        "人物严格按各镜头的动作与运镜描述真实运动，不得僵立原地。",
    ),
    ("continuity", "保持首帧人物身份、服装、发型、场景和光线连续。"),
    (
        "shot",
        "[{start:.1f}-{end:.1f}s] {shot_type}，{composition}，{camera_motion}；"
        "主体：{subject}；人物动作：{motion_clause}；场景：{scene}，转场：{transition}。"
        "口播意图：{spoken}",
    ),
    ("script", "口播意图：{full_text}"),
    (
        "outro",
        "环境音与音乐保持自然；不要增加无关人物，不要身份突变、肢体异常或画面闪烁。",
    ),
    (
        "narration_sync",
        "严格按照每个时间段组织配音，不得漏句、改写、重复或者交换顺序，"
        "每句话的起止时间和对应的镜头同步。",
    ),
)
H3_PROMPT_TEMPLATES = dict(H3_PROMPT_TEMPLATE_SPEC)
H3_PROMPT_TEMPLATE_HASH = hashlib.sha256(
    json.dumps(
        H3_PROMPT_TEMPLATE_SPEC,
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode()
).hexdigest()

# 拆解结果 motion 枚举到中文运动指令的确定性映射：渲染逻辑在代码里，
# 不依赖分析模型把“行走”写成好句子——只要状态枚举正确，Prompt 必然
# 携带正向运动指令。
SUBJECT_MOTION_STATE_CLAUSES = {
    "STATIC": "人物保持站位，动作限于口播与手势，双脚位置不变",
    "WALKING": "人物持续行走，每一步脚掌落地、重心自然转移、双臂随步态摆动，不得僵立原地",
    "RUNNING": "人物持续跑动，步幅与摆臂真实连贯，不得僵立原地",
    "TURNING": "人物原地转身，躯干与视线自然转动，双脚位置基本不变",
    "GESTURING_ONLY": "人物站位固定，仅上半身与手部做手势和口播",
    "OBJECT_MOTION": "画面主体为物体运动，无人物位移",
    "NO_PERSON": "本镜头无人物出镜",
}
SUBJECT_DIRECTION_LABELS = {
    "toward_camera": "向镜头方向",
    "away_from_camera": "背离镜头方向",
    "left": "向画面左侧",
    "right": "向画面右侧",
    "lateral": "横向移动",
    "in_place": "原地",
    "none": "无位移方向",
}
MOTION_CAMERA_MOTION_LABELS = {
    "STATIC": "固定机位",
    "PUSH_IN": "镜头缓慢推近",
    "PULL_BACK": "镜头缓慢拉远",
    "HANDHELD_TRACKING": "手持平稳跟拍",
    "PAN": "镜头横摇",
    "TILT": "镜头纵摇",
    "FOLLOW": "镜头跟随主体",
}
PROMPT_STATUSES = {"SAVED", "LOCKED", "USED"}
TERMINAL_STATUSES = {"FAILED", "CANCELLED"}
H3_MODEL = "MiniMax-H3"
METASO_BASE_URL = "https://metaso.cn"
METASO_CREATE_PATH = "/api/minimax/v2/video_generation"
METASO_QUERY_PATH = "/api/minimax/v2/query/video_generation"
SUPPORTED_RESOLUTIONS = {"768P", "2K"}
# A real H3 request polls for up to five minutes. Leave headroom so another
# worker never mistakes an active request for an abandoned lease.
GENERATION_LEASE_SECONDS = 600
# Reconciliation performs one provider query plus optional download/archive;
# fifteen minutes exceeds those bounded calls while still recovering crashes.
RECONCILIATION_RESERVATION_SECONDS = 900
FAKE_H3_OUTCOME_ENV = "VIDEO_REPLICA_FAKE_H3_OUTCOME"
FAKE_H3_RESULT_PATH_ENV = "VIDEO_REPLICA_FAKE_H3_RESULT_PATH"
FIRST_FRAME_URL_EXPIRES_IN = timedelta(minutes=15)
RESULT_DOWNLOAD_CHECK_EXPIRES_IN = timedelta(minutes=5)
# Cap archive retries so a permanently expired provider URL does not keep the
# paid task spinning in ARCHIVE_FAILED forever.
MAX_ARCHIVE_RETRIES = 5
SAFE_PRE_PROVIDER_FAILURE_CODES = {
    "FIRST_FRAME_URL_SIGN_FAILED",
    "METASO_SETTINGS_UNAVAILABLE",
}

logger = logging.getLogger(__name__)


class ScriptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Literal["original", "custom"]
    text: str = Field(max_length=8000)
    shot_card_version_id: str = Field(min_length=1)


class PromptCompileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    script_version_id: str = Field(min_length=1)
    shot_card_version_id: str = Field(min_length=1)
    first_frame_asset_id: str = Field(min_length=1)
    output_duration_seconds: int = Field(ge=4, le=15)
    resolution: Literal["768P", "2K"] = "768P"


class PromptPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    output_duration_seconds: int | None = Field(default=None, ge=4, le=15)
    resolution: Literal["768P", "2K"] = "768P"


class PromptPreviewResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt_text: str
    output_duration_seconds: int
    resolution: Literal["768P", "2K"]
    script_source: Literal["script_version", "analysis_original"]
    shot_card_version_id: str | None


class PromptRevisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_prompt_version_id: str = Field(min_length=1)
    prompt_text: str = Field(min_length=1, max_length=20_000)


class GenerationBatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    quantity: int = Field(ge=1)
    prompt_version_id: str = Field(min_length=1)
    first_frame_asset_id: str = Field(min_length=1)
    output_duration_seconds: int = Field(ge=4, le=15)
    resolution: Literal["768P", "2K"] = "768P"
    idempotency_key: str = Field(min_length=1, max_length=128)
    provider: Literal["fake_h3", "metaso"] = "fake_h3"
    fake_audio_quality: Literal["ok", "missing"] = "ok"


class PaidRegenerationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    idempotency_key: str = Field(min_length=1, max_length=128)
    estimated_cost_snapshot: float | None = Field(default=None, ge=0)
    generation_reason: str = Field(min_length=1, max_length=500)


class GenerationTaskRetryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    idempotency_key: str = Field(min_length=1, max_length=128)
    retry_reason: str = Field(min_length=1, max_length=500)


class GenerationBatchRenameRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str = Field(min_length=1, max_length=120)


class ConfirmNotChargedRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    idempotency_key: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=500)


class ReconcileGenerationTaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    idempotency_key: str = Field(min_length=1, max_length=128)


class H3CreateResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider_task_id: str
    status: Literal["SUCCEEDED"]
    result_url: str
    result_content: bytes
    audio_quality_status: Literal["AUDIO_OK", "AUDIO_QUALITY_FAILED"]
    quality_issue_codes: list[str]


class SubmissionUncertain(RuntimeError):
    pass


class H3ProviderFailed(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        provider_task_id: str | None = None,
        terminal: bool = False,
    ) -> None:
        super().__init__(message)
        self.provider_task_id = provider_task_id
        self.terminal = terminal


class H3ProviderSettingsUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class ReconcileReservation:
    id: str
    actor: CurrentUser
    idempotency_key: str


class H3Provider:
    def create_image_to_video(self, request: dict[str, Any]) -> H3CreateResult:
        raise HTTPException(
            status_code=501,
            detail={
                "code": "H3_QUERY_SOURCE_PENDING",
                "message": (
                    "METASO create/query/archive is not implemented until the supported "
                    "business query endpoint is confirmed."
                ),
            },
        )

    def download_result(self, url: str) -> bytes:
        raise H3ProviderFailed("download_result is not implemented for this provider")

    def _query_task(self, provider_task_id: str) -> dict[str, Any]:
        raise H3ProviderFailed("_query_task is not implemented for this provider")


class MetasoHttpTransport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None = None,
    ) -> bytes: ...


class UrllibMetasoHttpTransport:
    def __init__(self, *, timeout_seconds: float = 90.0) -> None:
        self.timeout_seconds = timeout_seconds

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None = None,
    ) -> bytes:
        try:
            request = Request(url, data=body, headers=dict(headers), method=method)
            with urlopen(request, timeout=self.timeout_seconds) as response:  # noqa: S310
                return cast(bytes, response.read())
        except HTTPError as exc:
            detail = ""
            try:
                detail = exc.read()[:1000].decode("utf-8", "replace")
            except OSError:
                pass
            logger.warning("METASO H3 request failed with HTTP status %s: %s", exc.code, detail)
            raise H3ProviderFailed(f"METASO returned HTTP {exc.code}") from exc
        except (TimeoutError, URLError, OSError) as exc:
            logger.warning("METASO H3 request failed: %s", type(exc).__name__)
            raise H3ProviderFailed("METASO H3 request failed") from exc


class MetasoH3Provider(H3Provider):
    """Runs the verified METASO create, query-list, and signed-download protocol."""

    def __init__(
        self,
        *,
        api_key: str,
        transport: MetasoHttpTransport | None = None,
        poll_interval_seconds: float = 3.0,
        max_poll_attempts: int = 100,
        sleeper: Callable[[float], None] = time.sleep,
        audio_quality_checker: Callable[
            [bytes], tuple[Literal["AUDIO_OK", "AUDIO_QUALITY_FAILED"], list[str]]
        ]
        | None = None,
        task_created_observer: Callable[[str], None] | None = None,
    ) -> None:
        self.api_key = api_key
        self.transport = transport or UrllibMetasoHttpTransport()
        self.poll_interval_seconds = poll_interval_seconds
        self.max_poll_attempts = max_poll_attempts
        self.sleeper = sleeper
        self.audio_quality_checker = audio_quality_checker or h3_audio_quality
        self.task_created_observer = task_created_observer

    def create_image_to_video(self, request: dict[str, Any]) -> H3CreateResult:
        validate_h3_request(request)
        if not _h3_request_has_https_first_frame(request):
            raise H3ProviderFailed("METASO H3 requires an HTTPS first-frame URL")
        provider_request = _metaso_create_request(request)
        try:
            response = self.transport.request(
                "POST",
                f"{METASO_BASE_URL}{METASO_CREATE_PATH}",
                headers=self._api_headers(),
                body=json.dumps(
                    provider_request, ensure_ascii=True, separators=(",", ":")
                ).encode(),
            )
        except H3ProviderFailed as exc:
            raise SubmissionUncertain("METASO submission result is unknown") from exc

        try:
            provider_task_id = _metaso_task_id(response)
        except H3ProviderFailed as exc:
            raise SubmissionUncertain("METASO submission result is unknown") from exc
        if self.task_created_observer is not None:
            # Persist the paid provider task id before polling so a timeout or
            # crash can never lose a result that reconciliation could recover.
            self.task_created_observer(provider_task_id)
        return self._poll_for_result(provider_task_id)

    def _poll_for_result(self, provider_task_id: str) -> H3CreateResult:
        for attempt in range(self.max_poll_attempts):
            try:
                item = self._query_task(provider_task_id)
            except H3ProviderFailed as exc:
                if exc.provider_task_id is not None:
                    raise
                raise H3ProviderFailed(str(exc), provider_task_id=provider_task_id) from exc
            status = item.get("status")
            if status == "succeeded":
                result_url = _metaso_content_url(item, provider_task_id=provider_task_id)
                try:
                    content = self.transport.request("GET", result_url, headers={})
                except H3ProviderFailed as exc:
                    raise H3ProviderFailed(str(exc), provider_task_id=provider_task_id) from exc
                audio_quality_status, quality_issue_codes = self.audio_quality_checker(content)
                return H3CreateResult(
                    provider_task_id=provider_task_id,
                    status="SUCCEEDED",
                    result_url=result_url,
                    result_content=content,
                    audio_quality_status=audio_quality_status,
                    quality_issue_codes=quality_issue_codes,
                )
            if status in {"failed", "cancelled"}:
                raise H3ProviderFailed(
                    f"METASO task finished with status {status}",
                    provider_task_id=provider_task_id,
                    terminal=True,
                )
            if attempt < self.max_poll_attempts - 1:
                self.sleeper(self.poll_interval_seconds)
        raise H3ProviderFailed(
            "METASO query did not return the created task as completed",
            provider_task_id=provider_task_id,
        )

    def download_result(self, url: str) -> bytes:
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise H3ProviderFailed("METASO result URL must be HTTPS", terminal=True)
        _require_public_https_host(parsed.hostname)
        return self.transport.request("GET", url, headers={})

    def _query_task(self, provider_task_id: str) -> dict[str, Any]:
        url = f"{METASO_BASE_URL}{METASO_QUERY_PATH}?{urlencode({'task_id': provider_task_id})}"
        payload = _metaso_json_object(
            self.transport.request("GET", url, headers=self._api_headers())
        )
        items = payload.get("items")
        if not isinstance(items, list):
            raise H3ProviderFailed(
                "METASO query response is missing items", provider_task_id=provider_task_id
            )
        for item in items:
            if isinstance(item, dict) and item.get("id") == provider_task_id:
                return cast(dict[str, Any], item)
        return {}

    def _api_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }


def metaso_h3_provider_from_settings(conn: sqlite3.Connection) -> MetasoH3Provider:
    try:
        config = SettingsRepository(conn).load_provider_config("metaso")
    except SettingsUnavailableError as exc:
        raise H3ProviderSettingsUnavailable("METASO settings cannot be read") from exc
    api_key = config.get("api_key")
    if not api_key:
        raise H3ProviderSettingsUnavailable("METASO API key is not configured")
    return MetasoH3Provider(
        api_key=api_key,
        # Real H3 rendering routinely exceeds five minutes; poll up to ~1.5h.
        poll_interval_seconds=5.0,
        max_poll_attempts=1080,
    )


def h3_provider_for_task(conn: sqlite3.Connection, provider_name: str) -> H3Provider:
    if provider_name == "fake_h3":
        outcome = os.environ.get(FAKE_H3_OUTCOME_ENV, "ok").strip()
        if outcome not in {"ok", "provider_failed", "submission_uncertain"}:
            raise H3ProviderSettingsUnavailable(f"{FAKE_H3_OUTCOME_ENV} has an unsupported value")
        return FakeH3Provider(
            outcome=cast(
                Literal["ok", "provider_failed", "submission_uncertain"],
                outcome,
            ),
            result_content=_fake_h3_result_content(),
        )
    if provider_name == "metaso":
        return metaso_h3_provider_from_settings(conn)
    raise H3ProviderSettingsUnavailable("generation task has an unsupported provider")


@dataclass(frozen=True)
class FakeH3Provider(H3Provider):
    audio_quality: Literal["ok", "missing"] = "ok"
    outcome: Literal["ok", "provider_failed", "submission_uncertain"] = "ok"
    result_content: bytes | None = None

    def create_image_to_video(self, request: dict[str, Any]) -> H3CreateResult:
        validate_h3_request(request)
        if self.outcome == "provider_failed":
            raise H3ProviderFailed(
                "Fake H3 provider terminal failure",
                provider_task_id=f"fake-h3-failed-{uuid4()}",
                terminal=True,
            )
        if self.outcome == "submission_uncertain":
            raise SubmissionUncertain("Fake H3 submission result is unknown")
        provider_task_id = f"fake-h3-{uuid4()}"
        audio_ok = self.audio_quality == "ok"
        result_content = self.result_content
        if result_content is None:
            result_content = f"fake mp4 content for {provider_task_id}".encode()
        return H3CreateResult(
            provider_task_id=provider_task_id,
            status="SUCCEEDED",
            result_url=f"fake://h3-results/{provider_task_id}.mp4",
            result_content=result_content,
            audio_quality_status="AUDIO_OK" if audio_ok else "AUDIO_QUALITY_FAILED",
            quality_issue_codes=[] if audio_ok else ["AUDIO_QUALITY_FAILED"],
        )

    def download_result(self, url: str) -> bytes:
        if self.result_content is not None:
            return self.result_content
        return f"fake mp4 content re-downloaded from {url}".encode()


def _fake_h3_result_content() -> bytes | None:
    fixture_path = os.environ.get(FAKE_H3_RESULT_PATH_ENV, "").strip()
    if not fixture_path:
        return None
    try:
        content = Path(fixture_path).read_bytes()
    except OSError as exc:
        logger.error("fake H3 result fixture cannot be read: %s", type(exc).__name__)
        raise H3ProviderSettingsUnavailable(f"{FAKE_H3_RESULT_PATH_ENV} cannot be read") from exc
    if not content:
        logger.error("fake H3 result fixture is empty")
        raise H3ProviderSettingsUnavailable(f"{FAKE_H3_RESULT_PATH_ENV} is empty")
    return content


class VersionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    project_id: str
    asset_id: str | None
    kind: str
    version_number: int
    payload: dict[str, Any]
    created_by_user_id: str | None
    created_at: str


class VersionState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: VersionResult | None
    stale: bool
    stale_reasons: list[str]


class GenerationRuntimeLimits(BaseModel):
    model_config = ConfigDict(extra="forbid")

    min_quantity: int = 1
    max_quantity: int
    estimated_cost_per_task: float | None = None


class TaskSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    status: str
    archive_status: str
    quality_status: str
    quality_issue_codes: list[str]
    result_asset_id: str | None
    stage: str
    provider: str
    model: str
    provider_task_id_tail: str | None
    attempt: int
    archive_retry_count: int
    estimated_cost: float | None
    actual_cost: float | None
    error_code: str | None
    error_message_redacted: str | None
    submitted_at: str | None
    started_at: str | None
    completed_at: str | None
    duration_seconds: float | None
    retry_of_task_id: str | None
    superseded_by_task_id: str | None
    superseded_at: str | None
    retry_reason: str | None
    retry_requested_at: str | None
    available_actions: list[Literal["RETRY", "RECONCILE", "CONFIRM_NOT_CHARGED", "REGENERATE"]]


class TaskResult(TaskSummary):
    prompt_snapshot: dict[str, Any] | None
    # Provider 返回的成片直连播放链接（临时签名 URL）。客户端优先用它
    # 在线播放，链接过期后回退到本地归档副本；非 HTTPS（如 fake://）不外露。
    provider_result_url: str | None = None


class BatchProgress(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total_count: int
    terminal_count: int
    progress_percent: int
    counts: dict[str, int]
    historical_counts: dict[str, int] = Field(default_factory=dict)


class BatchResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    project_id: str
    prompt_version_id: str
    status: str
    quantity: int
    stale: bool
    display_name: str | None = None
    source_batch_id: str | None = None
    source_task_id: str | None = None
    generation_reason: str | None = None
    progress: BatchProgress
    tasks: list[TaskResult]

    @model_validator(mode="after")
    def validate_quantity(self) -> BatchResult:
        if self.quantity != self.progress.total_count:
            raise ValueError("quantity must match progress total_count")
        return self


class GenerationBatchListItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    project_id: str
    project_name: str
    created_by_user_id: str
    created_by_display_name: str
    prompt_version_id: str
    status: str
    quantity: int
    created_at: str
    updated_at: str
    display_name: str | None = None
    source_batch_id: str | None = None
    source_task_id: str | None = None
    generation_reason: str | None = None
    progress: BatchProgress
    total_estimated_cost: float | None
    total_actual_cost: float | None
    needs_attention_count: int
    has_results: bool
    tasks: list[TaskSummary]


class GenerationBatchListPage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[GenerationBatchListItem]
    next_cursor: str | None


BatchStatusFilter = Literal[
    "PENDING",
    "QUEUED",
    "NEEDS_ATTENTION",
    "SUCCEEDED",
    "COMPLETED_WITH_FAILURES",
]


def latest_version(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    kind: str,
) -> sqlite3.Row | None:
    return cast(
        sqlite3.Row | None,
        conn.execute(
            """
            SELECT id, project_id, asset_id, kind, version_number, payload_json,
                   created_by_user_id, created_at
            FROM versions
            WHERE project_id = ? AND kind = ?
            ORDER BY version_number DESC
            LIMIT 1
            """,
            (project_id, kind),
        ).fetchone(),
    )


def require_latest_version(
    conn: sqlite3.Connection,
    *,
    row: sqlite3.Row,
    project_id: str,
    kind: str,
    code: str,
    message: str,
) -> None:
    latest = latest_version(conn, project_id=project_id, kind=kind)
    if latest is None or str(latest["id"]) != str(row["id"]):
        raise generation_error(409, code, message)


def version_state(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    actor: CurrentUser,
    kind: str,
) -> VersionState:
    require_project_access(
        conn,
        actor=actor,
        project_id=project_id,
        action=f"{kind}.read",
    )
    row = latest_version(conn, project_id=project_id, kind=kind)
    if row is None:
        return VersionState(version=None, stale=False, stale_reasons=[])
    reasons = version_stale_reasons(conn, row=row)
    return VersionState(
        version=version_result(row),
        stale=bool(reasons),
        stale_reasons=reasons,
    )


def version_stale_reasons(conn: sqlite3.Connection, *, row: sqlite3.Row) -> list[str]:
    project_id = str(row["project_id"])
    kind = str(row["kind"])
    payload = json.loads(str(row["payload_json"]))
    reasons: list[str] = []

    if kind == SCRIPT_KIND:
        shot_card = latest_version(conn, project_id=project_id, kind="shot_card")
        if shot_card is None or payload.get("shot_card_version_id") != str(shot_card["id"]):
            reasons.append("SHOT_CARD_SUPERSEDED")
        else:
            reasons.extend(shot_card_stale_reasons(conn, row=shot_card))
        return reasons

    if kind != H3_PROMPT_KIND:
        return reasons

    current_prompt = latest_version(conn, project_id=project_id, kind=H3_PROMPT_KIND)
    if current_prompt is None or str(current_prompt["id"]) != str(row["id"]):
        reasons.append("PROMPT_SUPERSEDED")

    frozen_template_version = payload.get("template_version")
    frozen_template_hash = payload.get("template_hash")
    if (
        frozen_template_version is not None
        and frozen_template_version != H3_PROMPT_TEMPLATE_VERSION
    ) or (frozen_template_hash is not None and frozen_template_hash != H3_PROMPT_TEMPLATE_HASH):
        reasons.append("TEMPLATE_SUPERSEDED")

    frozen_script_id = payload.get("script_version_id")
    if isinstance(frozen_script_id, str):
        script = latest_version(conn, project_id=project_id, kind=SCRIPT_KIND)
        if script is None or frozen_script_id != str(script["id"]):
            reasons.append("SCRIPT_SUPERSEDED")
        else:
            reasons.extend(version_stale_reasons(conn, row=script))

    frozen_shot_card_id = payload.get("shot_card_version_id")
    if isinstance(frozen_shot_card_id, str):
        shot_card = latest_version(conn, project_id=project_id, kind="shot_card")
        if shot_card is None or frozen_shot_card_id != str(shot_card["id"]):
            if "SHOT_CARD_SUPERSEDED" not in reasons:
                reasons.append("SHOT_CARD_SUPERSEDED")
        else:
            reasons.extend(
                reason
                for reason in shot_card_stale_reasons(conn, row=shot_card)
                if reason not in reasons
            )

    first_frame_asset_id = payload.get("first_frame_asset_id")
    if not isinstance(first_frame_asset_id, str):
        reasons.append("FIRST_FRAME_SUPERSEDED")
        return reasons
    try:
        current_sources = confirmed_first_frame_sources(
            conn,
            project_id=project_id,
            first_frame_asset_id=first_frame_asset_id,
        )
    except HTTPException:
        reasons.append("FIRST_FRAME_SUPERSEDED")
        return reasons

    for key in (
        "first_frame_candidates_version_id",
        "first_frame_selection_version_id",
        "source_frame_selection_version_id",
        "main_character_version_id",
        "character_version_id",
        "character_reference_selection_id",
    ):
        frozen_value = payload.get(key)
        if frozen_value is not None and frozen_value != current_sources.get(key):
            reasons.append("FIRST_FRAME_SUPERSEDED")
            break
    return reasons


def shot_card_stale_reasons(
    conn: sqlite3.Connection,
    *,
    row: sqlite3.Row,
) -> list[str]:
    payload = json.loads(str(row["payload_json"]))
    source_analysis_version_id = payload.get("source_analysis_version_id")
    if not isinstance(source_analysis_version_id, str):
        return []
    analysis = latest_version(
        conn,
        project_id=str(row["project_id"]),
        kind="analysis",
    )
    if analysis is None or source_analysis_version_id != str(analysis["id"]):
        return ["ANALYSIS_SUPERSEDED"]
    return []


def confirmed_first_frame_sources(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    first_frame_asset_id: str,
) -> dict[str, str | None]:
    require_confirmed_first_frame(
        conn,
        project_id=project_id,
        first_frame_asset_id=first_frame_asset_id,
    )
    selection = latest_version(
        conn,
        project_id=project_id,
        kind="first_frame_selection",
    )
    candidates = latest_version(
        conn,
        project_id=project_id,
        kind="first_frame_candidates",
    )
    if selection is None or candidates is None:
        raise generation_error(
            409,
            "FIRST_FRAME_CONFIRMATION_REQUIRED",
            "Confirm a first-frame candidate before compiling or submitting H3 generation.",
        )
    candidate_payload = json.loads(str(candidates["payload_json"]))
    return {
        "first_frame_candidates_version_id": str(candidates["id"]),
        "first_frame_selection_version_id": str(selection["id"]),
        "source_frame_selection_version_id": candidate_payload.get(
            "source_frame_selection_version_id"
        ),
        "main_character_version_id": candidate_payload.get("main_character_version_id"),
        "character_version_id": candidate_payload.get("character_version_id"),
        "character_reference_selection_id": candidate_payload.get(
            "character_reference_selection_id"
        ),
    }


def create_script_version(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    actor: CurrentUser,
    request: ScriptRequest,
) -> sqlite3.Row:
    require_not_auditor(
        conn,
        actor=actor,
        action="script.create",
        entity_type="project",
        entity_id=project_id,
    )
    require_project_access(conn, actor=actor, project_id=project_id, action="script.create")
    try:
        conn.execute("BEGIN IMMEDIATE")
        shot_card = require_version(
            conn,
            version_id=request.shot_card_version_id,
            project_id=project_id,
            kind="shot_card",
        )
        require_latest_version(
            conn,
            row=shot_card,
            project_id=project_id,
            kind="shot_card",
            code="SHOT_CARD_STALE",
            message="Save the script against the latest shot-card version.",
        )
        if shot_card_stale_reasons(conn, row=shot_card):
            raise generation_error(
                409,
                "SHOT_CARD_STALE",
                "Save the latest analysis as a new shot-card version before editing the script.",
            )
        shot_payload = json.loads(str(shot_card["payload_json"]))
        text = request.text.strip()
        if not text:
            raise generation_error(
                422,
                "SCRIPT_TEXT_REQUIRED",
                "Script text cannot be blank.",
            )
        payload = {
            "schema_version": GENERATION_SCHEMA_VERSION,
            "source": request.source,
            "full_text": text,
            "char_count": len(text),
            "estimated_duration_seconds": estimate_spoken_duration(text),
            "shot_card_version_id": request.shot_card_version_id,
            "shot_mappings": map_script_to_shots(
                text,
                cast(list[dict[str, Any]], shot_payload["shots"]),
            ),
            "creates_audio_task": False,
        }
        row = insert_version(
            conn,
            project_id=project_id,
            asset_id=None,
            kind=SCRIPT_KIND,
            created_by_user_id=actor.id,
            payload=payload,
        )
    except Exception:
        conn.rollback()
        raise
    write_audit(
        conn,
        actor=actor,
        action="script.create",
        entity_type="version",
        entity_id=str(row["id"]),
        metadata={"project_id": project_id, "shot_card_version_id": request.shot_card_version_id},
    )
    return row


def compile_prompt_version(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    actor: CurrentUser,
    request: PromptCompileRequest,
) -> sqlite3.Row:
    require_not_auditor(
        conn,
        actor=actor,
        action="prompt.compile",
        entity_type="project",
        entity_id=project_id,
    )
    require_project_access(conn, actor=actor, project_id=project_id, action="prompt.compile")
    try:
        conn.execute("BEGIN IMMEDIATE")
        script = require_version(
            conn,
            version_id=request.script_version_id,
            project_id=project_id,
            kind=SCRIPT_KIND,
        )
        require_latest_version(
            conn,
            row=script,
            project_id=project_id,
            kind=SCRIPT_KIND,
            code="SCRIPT_STALE",
            message="Compile the prompt from the latest script version.",
        )
        if version_stale_reasons(conn, row=script):
            raise generation_error(
                409,
                "SCRIPT_STALE",
                "Save a script from the current analysis and shot-card version.",
            )
        shot_card = require_version(
            conn,
            version_id=request.shot_card_version_id,
            project_id=project_id,
            kind="shot_card",
        )
        require_latest_version(
            conn,
            row=shot_card,
            project_id=project_id,
            kind="shot_card",
            code="SHOT_CARD_STALE",
            message="Compile the prompt from the latest shot-card version.",
        )
        if shot_card_stale_reasons(conn, row=shot_card):
            raise generation_error(
                409,
                "SHOT_CARD_STALE",
                "Save the latest analysis as a new shot-card version before compiling.",
            )
        script_payload = json.loads(str(script["payload_json"]))
        if script_payload.get("shot_card_version_id") != request.shot_card_version_id:
            raise generation_error(
                409,
                "SCRIPT_SHOT_CARD_MISMATCH",
                "The current script was saved against a different shot-card version.",
            )
        first_frame = require_asset_access(
            conn,
            actor=actor,
            asset_id=request.first_frame_asset_id,
            action="prompt.compile",
        )
        if str(first_frame["project_id"]) != project_id:
            raise generation_error(
                400,
                "ASSET_PROJECT_MISMATCH",
                "First frame is not in this project.",
            )
        first_frame_sources = confirmed_first_frame_sources(
            conn,
            project_id=project_id,
            first_frame_asset_id=request.first_frame_asset_id,
        )

        shot_payload = json.loads(str(shot_card["payload_json"]))
        source_duration_seconds = shot_timeline_duration(shot_payload)
        timeline_scale_factor = request.output_duration_seconds / source_duration_seconds
        prompt_text = compile_prompt_text(
            script_payload=script_payload,
            shot_payload=shot_payload,
            source_duration_seconds=source_duration_seconds,
            duration_seconds=request.output_duration_seconds,
            resolution=request.resolution,
        )
        payload = {
            "schema_version": GENERATION_SCHEMA_VERSION,
            "status": "SAVED",
            "prompt_text": prompt_text,
            "content_hash": content_hash(prompt_text),
            "template_version": H3_PROMPT_TEMPLATE_VERSION,
            "template_hash": H3_PROMPT_TEMPLATE_HASH,
            "source_analysis_version_id": shot_payload.get("source_analysis_version_id"),
            "script_version_id": request.script_version_id,
            "shot_card_version_id": request.shot_card_version_id,
            "first_frame_asset_id": request.first_frame_asset_id,
            "first_frame_uri": str(first_frame["storage_uri"]),
            "source_duration_seconds": source_duration_seconds,
            "timeline_scale_factor": timeline_scale_factor,
            "timeline_policy": "linear_scale_to_output.v1",
            "output_duration_seconds": request.output_duration_seconds,
            "resolution": request.resolution,
            **first_frame_sources,
        }
        row = insert_version(
            conn,
            project_id=project_id,
            asset_id=request.first_frame_asset_id,
            kind=H3_PROMPT_KIND,
            created_by_user_id=actor.id,
            payload=payload,
        )
    except Exception:
        conn.rollback()
        raise
    write_audit(
        conn,
        actor=actor,
        action="prompt.compile",
        entity_type="version",
        entity_id=str(row["id"]),
        metadata={"project_id": project_id},
    )
    return row


def unwrap_analysis_payload(payload: Any) -> dict[str, Any]:
    """兼容 shot_card 与 analysis 两种版本格式。

    shot_card 版本顶层即 shots/duration_seconds；拆解自动落库的 analysis
    版本是 {"schema_version", "analysis": {...}, "source_asset", ...} 包装
    结构，镜头与原稿在内层——回退用 analysis 编译时必须先解包。
    """
    if not isinstance(payload, dict):
        return {}
    if isinstance(payload.get("shots"), list):
        return payload
    inner = payload.get("analysis")
    if isinstance(inner, dict):
        return inner
    return payload


def preview_prompt_text(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    actor: CurrentUser,
    request: PromptPreviewRequest,
) -> PromptPreviewResult:
    """无副作用地预览当前项目会编译出的 H3 Prompt 文本。

    镜头来源优先取最新镜头卡版本，共用的分析版本兜底；口播来源优先取最新
    口播稿版本（原样复用其 shot_mappings，与正式编译文本保持一致），否则用
    分析产出的原稿逐镜头映射。不落库、不写审计；正式编译仍要求首帧与
    最新版本链（fail-closed 语义不受影响）。
    """
    require_project_access(
        conn,
        actor=actor,
        project_id=project_id,
        action="prompt.preview",
    )
    shot_card = latest_version(conn, project_id=project_id, kind="shot_card")
    analysis = latest_version(conn, project_id=project_id, kind="analysis")
    shot_row = shot_card if shot_card is not None else analysis
    if shot_row is None:
        raise generation_error(
            409,
            "ANALYSIS_NOT_READY",
            "Analyze the reference video before previewing the prompt.",
        )
    shot_payload = unwrap_analysis_payload(json.loads(str(shot_row["payload_json"])))
    shots = shot_payload.get("shots")
    if not isinstance(shots, list) or not shots:
        raise generation_error(
            409,
            "SHOT_CARD_TIMELINE_INVALID",
            "The latest shot data has no shots to compile.",
        )

    script_source: Literal["script_version", "analysis_original"]
    script = latest_version(conn, project_id=project_id, kind=SCRIPT_KIND)
    if script is not None:
        script_payload = json.loads(str(script["payload_json"]))
        script_source = "script_version"
    else:
        analysis_payload = (
            unwrap_analysis_payload(json.loads(str(analysis["payload_json"])))
            if analysis is not None
            else {}
        )
        original_script = str(analysis_payload.get("original_script") or "")
        spoken_texts = [str(shot.get("spoken_text") or "") for shot in shots]
        script_payload = {
            "full_text": original_script or "".join(spoken_texts),
            "shot_mappings": [
                {
                    "shot_id": str(shot.get("shot_id") or ""),
                    "text": str(shot.get("spoken_text") or ""),
                }
                for shot in shots
            ],
        }
        script_source = "analysis_original"

    source_duration_seconds = shot_timeline_duration(shot_payload)
    duration_seconds = request.output_duration_seconds
    if duration_seconds is None:
        duration_seconds = max(4, min(15, round(source_duration_seconds)))
    prompt_text = compile_prompt_text(
        script_payload=script_payload,
        shot_payload=shot_payload,
        source_duration_seconds=source_duration_seconds,
        duration_seconds=duration_seconds,
        resolution=request.resolution,
    )
    return PromptPreviewResult(
        prompt_text=prompt_text,
        output_duration_seconds=duration_seconds,
        resolution=request.resolution,
        script_source=script_source,
        shot_card_version_id=(str(shot_card["id"]) if shot_card is not None else None),
    )


def revise_prompt_version(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    actor: CurrentUser,
    request: PromptRevisionRequest,
) -> sqlite3.Row:
    require_not_auditor(
        conn,
        actor=actor,
        action="prompt.revise",
        entity_type="project",
        entity_id=project_id,
    )
    require_project_access(conn, actor=actor, project_id=project_id, action="prompt.revise")
    try:
        conn.execute("BEGIN IMMEDIATE")
        base = require_version(
            conn,
            version_id=request.base_prompt_version_id,
            project_id=project_id,
            kind=H3_PROMPT_KIND,
        )
        require_latest_version(
            conn,
            row=base,
            project_id=project_id,
            kind=H3_PROMPT_KIND,
            code="PROMPT_STALE",
            message="Revise the latest prompt version.",
        )
        stale_reasons = version_stale_reasons(conn, row=base)
        if stale_reasons:
            raise generation_error(
                409,
                "PROMPT_STALE",
                "Upstream inputs changed; compile a new prompt before editing.",
            )
        prompt_text = request.prompt_text.strip()
        if not prompt_text:
            raise generation_error(
                422,
                "PROMPT_TEXT_REQUIRED",
                "Prompt text cannot be blank.",
            )
        payload = json.loads(str(base["payload_json"]))
        payload.update(
            {
                "status": "SAVED",
                "prompt_text": prompt_text,
                "content_hash": content_hash(prompt_text),
                "base_prompt_version_id": request.base_prompt_version_id,
            }
        )
        row = insert_version(
            conn,
            project_id=project_id,
            asset_id=None if base["asset_id"] is None else str(base["asset_id"]),
            kind=H3_PROMPT_KIND,
            created_by_user_id=actor.id,
            payload=payload,
        )
    except Exception:
        conn.rollback()
        raise
    write_audit(
        conn,
        actor=actor,
        action="prompt.revise",
        entity_type="version",
        entity_id=str(row["id"]),
        metadata={
            "project_id": project_id,
            "base_prompt_version_id": request.base_prompt_version_id,
        },
    )
    return row


def lock_prompt_version(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    prompt_version_id: str,
    actor: CurrentUser,
) -> sqlite3.Row:
    require_not_auditor(
        conn,
        actor=actor,
        action="prompt.lock",
        entity_type="version",
        entity_id=prompt_version_id,
    )
    require_project_access(conn, actor=actor, project_id=project_id, action="prompt.lock")
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = require_version(
            conn,
            version_id=prompt_version_id,
            project_id=project_id,
            kind=H3_PROMPT_KIND,
        )
        require_latest_version(
            conn,
            row=row,
            project_id=project_id,
            kind=H3_PROMPT_KIND,
            code="PROMPT_STALE",
            message="Lock the latest prompt version.",
        )
        if version_stale_reasons(conn, row=row):
            raise generation_error(
                409,
                "PROMPT_STALE",
                "Upstream inputs changed; compile a new prompt before locking.",
            )
        payload = json.loads(str(row["payload_json"]))
        if payload["status"] == "USED":
            conn.rollback()
            return row
        if payload["status"] not in {"SAVED", "LOCKED"}:
            raise generation_error(409, "PROMPT_STATUS_INVALID", "Prompt cannot be locked.")
        payload["status"] = "LOCKED"
        conn.execute(
            "UPDATE versions SET payload_json = ? WHERE id = ?",
            (json.dumps(payload, ensure_ascii=True, sort_keys=True), prompt_version_id),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    write_audit(
        conn,
        actor=actor,
        action="prompt.lock",
        entity_type="version",
        entity_id=prompt_version_id,
        metadata={"project_id": project_id},
    )
    return get_version(conn, prompt_version_id)


def _find_idempotent_batch(
    conn: sqlite3.Connection, *, actor_id: str, project_id: str, key: str
) -> sqlite3.Row | None:
    return cast(
        sqlite3.Row | None,
        conn.execute(
            """
            SELECT id, request_hash
            FROM generation_batches
            WHERE created_by_user_id = ? AND project_id = ? AND idempotency_key = ?
            """,
            (actor_id, project_id, key),
        ).fetchone(),
    )


def _reserve_generation_credit(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    task_id: str,
    billing_round: int | None = 1,
) -> int:
    try:
        return reserve_internal_billing(
            conn,
            user_id=user_id,
            task_id=task_id,
            billing_round=billing_round,
        )
    except InsufficientCreditsError as exc:
        raise generation_error(
            402,
            "INSUFFICIENT_CREDITS",
            "Available credits are insufficient for this generation request.",
        ) from exc


def create_generation_batch(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    actor: CurrentUser,
    request: GenerationBatchRequest,
    provider_client: H3Provider | None = None,
) -> BatchResult:
    _ = provider_client
    require_not_auditor(
        conn,
        actor=actor,
        action="generation_batch.create",
        entity_type="project",
        entity_id=project_id,
    )
    require_project_access(
        conn,
        actor=actor,
        project_id=project_id,
        action="generation_batch.create",
    )
    request_hash = idempotency_request_hash(request)
    existing = _find_idempotent_batch(
        conn, actor_id=actor.id, project_id=project_id, key=request.idempotency_key
    )
    if existing is not None:
        if str(existing["request_hash"]) != request_hash:
            raise generation_error(
                409,
                "IDEMPOTENCY_CONFLICT",
                "This idempotency key was already used for a different request.",
            )
        return get_generation_batch(conn, batch_id=str(existing["id"]), actor=actor)

    try:
        conn.execute("BEGIN IMMEDIATE")
        concurrent_existing = _find_idempotent_batch(
            conn,
            actor_id=actor.id,
            project_id=project_id,
            key=request.idempotency_key,
        )
        if concurrent_existing is not None:
            conn.rollback()
            if str(concurrent_existing["request_hash"]) != request_hash:
                raise generation_error(
                    409,
                    "IDEMPOTENCY_CONFLICT",
                    "This idempotency key was already used for a different request.",
                )
            return get_generation_batch(
                conn,
                batch_id=str(concurrent_existing["id"]),
                actor=actor,
            )

        runtime = read_runtime_limits(conn)
        max_quantity = runtime["max_generation_count_per_batch"]
        if request.quantity > max_quantity:
            raise generation_error(
                422,
                "QUANTITY_EXCEEDS_LIMIT",
                f"quantity must be less than or equal to {max_quantity}",
            )

        # Mutable runtime/provider preflights apply only to a genuinely new
        # submission. Replayed keys return above even if settings changed.
        if request.provider == "metaso":
            try:
                metaso_h3_provider_from_settings(conn)
            except H3ProviderSettingsUnavailable as exc:
                raise generation_error(
                    503,
                    "METASO_SETTINGS_UNAVAILABLE",
                    "Save a readable METASO API Key before queuing a real H3 task.",
                ) from exc

        prompt = require_version(
            conn,
            version_id=request.prompt_version_id,
            project_id=project_id,
            kind=H3_PROMPT_KIND,
        )
        if version_stale_reasons(conn, row=prompt):
            raise generation_error(
                409,
                "PROMPT_STALE",
                "Upstream inputs changed; compile and lock a new prompt before generating.",
            )
        prompt_snapshot = json.loads(str(prompt["payload_json"]))
        if prompt_snapshot.get("status") != "LOCKED":
            raise generation_error(
                409,
                "PROMPT_NOT_LOCKED",
                "Generation requires a LOCKED prompt.",
            )
        frozen_duration = prompt_snapshot.get("output_duration_seconds")
        frozen_resolution = prompt_snapshot.get("resolution")
        if (frozen_duration is not None and frozen_duration != request.output_duration_seconds) or (
            frozen_resolution is not None and frozen_resolution != request.resolution
        ):
            raise generation_error(
                409,
                "PROMPT_PARAMETERS_MISMATCH",
                "Generation parameters must match the locked prompt snapshot.",
            )
        first_frame = require_asset_access(
            conn,
            actor=actor,
            asset_id=request.first_frame_asset_id,
            action="generation_batch.create",
        )
        if request.provider == "metaso":
            require_cos_first_frame_storage(
                conn,
                storage_uri=str(first_frame["storage_uri"]),
            )
        if str(first_frame["project_id"]) != project_id:
            raise generation_error(
                400,
                "ASSET_PROJECT_MISMATCH",
                "First frame is not in this project.",
            )
        require_confirmed_first_frame(
            conn,
            project_id=project_id,
            first_frame_asset_id=request.first_frame_asset_id,
        )
        if prompt_snapshot.get("first_frame_asset_id") != request.first_frame_asset_id:
            raise generation_error(
                409,
                "FIRST_FRAME_PROMPT_MISMATCH",
                "Generation must use the first frame that was used to compile the locked prompt.",
            )

        request_snapshot = generation_request_snapshot(request, prompt_snapshot)
        batch_id = str(uuid4())
        used_prompt_snapshot = {**prompt_snapshot, "status": "USED"}
        cursor = conn.execute(
            """
            UPDATE versions
            SET payload_json = ?
            WHERE id = ? AND payload_json = ?
            """,
            (
                json.dumps(used_prompt_snapshot, ensure_ascii=True, sort_keys=True),
                request.prompt_version_id,
                str(prompt["payload_json"]),
            ),
        )
        if cursor.rowcount != 1:
            raise generation_error(
                409,
                "PROMPT_ALREADY_USED",
                "This LOCKED prompt was already consumed by another batch.",
            )
        conn.execute(
            """
            INSERT INTO generation_batches (
                id,
                project_id,
                created_by_user_id,
                idempotency_key,
                request_hash,
                request_snapshot_json,
                status
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                batch_id,
                project_id,
                actor.id,
                request.idempotency_key,
                request_hash,
                json.dumps(request_snapshot, ensure_ascii=True, sort_keys=True),
                "QUEUED",
            ),
        )
        for _ in range(request.quantity):
            task_id = str(uuid4())
            conn.execute(
                """
                INSERT INTO generation_tasks (
                    id,
                    batch_id,
                    generation_mode,
                    provider,
                    model,
                    status,
                    archive_status,
                    quality_status,
                    prompt_version_id,
                    prompt_snapshot_json,
                    next_poll_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (
                    task_id,
                    batch_id,
                    "I2V",
                    request.provider,
                    H3_MODEL,
                    "PENDING",
                    "PENDING",
                    "PENDING",
                    request.prompt_version_id,
                    json.dumps(prompt_snapshot, ensure_ascii=True, sort_keys=True),
                ),
            )
            _reserve_generation_credit(
                conn,
                user_id=actor.id,
                task_id=task_id,
            )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        # A concurrent request committed the same idempotency key first; return
        # the existing batch instead of a misleading 409.
        existing = _find_idempotent_batch(
            conn, actor_id=actor.id, project_id=project_id, key=request.idempotency_key
        )
        if existing is not None:
            if str(existing["request_hash"]) != request_hash:
                raise generation_error(
                    409,
                    "IDEMPOTENCY_CONFLICT",
                    "This idempotency key was already used for a different request.",
                )
            return get_generation_batch(conn, batch_id=str(existing["id"]), actor=actor)
        raise
    except Exception:
        conn.rollback()
        raise

    write_audit(
        conn,
        actor=actor,
        action="generation_batch.create",
        entity_type="generation_batch",
        entity_id=batch_id,
        metadata={"project_id": project_id, "quantity": request.quantity},
    )
    return get_generation_batch(conn, batch_id=batch_id, actor=actor)


def regenerate_generation_batch(
    conn: sqlite3.Connection,
    *,
    batch_id: str,
    actor: CurrentUser,
    request: PaidRegenerationRequest,
) -> BatchResult:
    source_context = conn.execute(
        "SELECT project_id FROM generation_batches WHERE id = ?",
        (batch_id,),
    ).fetchone()
    if source_context is None:
        raise generation_error(404, "BATCH_NOT_FOUND", "Generation batch does not exist.")
    project_id = str(source_context["project_id"])
    require_not_auditor(
        conn,
        actor=actor,
        action="generation_batch.regenerate",
        entity_type="generation_batch",
        entity_id=batch_id,
    )
    require_project_access(
        conn,
        actor=actor,
        project_id=project_id,
        action="generation_batch.regenerate",
    )

    new_batch_id = ""
    try:
        conn.execute("BEGIN IMMEDIATE")
        source_batch = conn.execute(
            """
            SELECT id, project_id, request_snapshot_json
            FROM generation_batches
            WHERE id = ?
            """,
            (batch_id,),
        ).fetchone()
        if source_batch is None:
            raise generation_error(404, "BATCH_NOT_FOUND", "Generation batch does not exist.")
        source_tasks = conn.execute(
            """
            SELECT id, generation_mode, provider, model, prompt_version_id,
                   prompt_snapshot_json
            FROM generation_tasks
            WHERE batch_id = ?
            ORDER BY created_at, id
            """,
            (batch_id,),
        ).fetchall()
        if not source_tasks:
            raise generation_error(
                409,
                "SOURCE_BATCH_EMPTY",
                "A batch without frozen tasks cannot be regenerated.",
            )

        source_task_ids = [str(row["id"]) for row in source_tasks]
        prompt_snapshots = [
            required_snapshot_text(row["prompt_snapshot_json"]) for row in source_tasks
        ]
        request_snapshot = required_snapshot_text(source_batch["request_snapshot_json"])
        request_hash = paid_regeneration_request_hash(
            request,
            operation_kind="MANUAL_BATCH_REGENERATE",
            source_batch_id=batch_id,
            source_task_ids=source_task_ids,
            request_snapshot=request_snapshot,
            prompt_snapshots=prompt_snapshots,
        )
        existing = _find_idempotent_batch(
            conn,
            actor_id=actor.id,
            project_id=project_id,
            key=request.idempotency_key,
        )
        if existing is not None:
            conn.rollback()
            require_matching_idempotent_batch(existing, request_hash=request_hash)
            return get_generation_batch(conn, batch_id=str(existing["id"]), actor=actor)

        runtime = read_runtime_limits(conn)
        if len(source_tasks) > runtime["max_generation_count_per_batch"]:
            raise generation_error(
                422,
                "QUANTITY_EXCEEDS_LIMIT",
                "quantity must be less than or equal to "
                f"{runtime['max_generation_count_per_batch']}",
            )
        for provider_name in {str(row["provider"]) for row in source_tasks}:
            require_regeneration_provider_ready(conn, provider_name=provider_name)

        new_batch_id = str(uuid4())
        conn.execute(
            """
            INSERT INTO generation_batches (
                id, project_id, created_by_user_id, idempotency_key,
                request_hash, request_snapshot_json, status,
                source_batch_id, source_task_id, generation_reason
            )
            VALUES (?, ?, ?, ?, ?, ?, 'QUEUED', ?, NULL, ?)
            """,
            (
                new_batch_id,
                project_id,
                actor.id,
                request.idempotency_key,
                request_hash,
                request_snapshot,
                batch_id,
                request.generation_reason,
            ),
        )
        estimated_cost = paid_cost_per_task(
            request.estimated_cost_snapshot,
            quantity=len(source_tasks),
        )
        for source_task, prompt_snapshot in zip(source_tasks, prompt_snapshots, strict=True):
            replacement_task_id = str(uuid4())
            conn.execute(
                """
                INSERT INTO generation_tasks (
                    id, batch_id, generation_mode, provider, model,
                    status, archive_status, quality_status,
                    prompt_version_id, prompt_snapshot_json, next_poll_at,
                    estimated_cost, retry_of_task_id, retry_reason,
                    retry_requested_by_user_id, retry_requested_at
                )
                VALUES (?, ?, ?, ?, ?, 'PENDING', 'PENDING', 'PENDING',
                        ?, ?, CURRENT_TIMESTAMP, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (
                    replacement_task_id,
                    new_batch_id,
                    str(source_task["generation_mode"]),
                    str(source_task["provider"]),
                    str(source_task["model"]),
                    source_task["prompt_version_id"],
                    prompt_snapshot,
                    estimated_cost,
                    str(source_task["id"]),
                    request.generation_reason,
                    actor.id,
                ),
            )
            _reserve_generation_credit(
                conn,
                user_id=actor.id,
                task_id=replacement_task_id,
            )
        insert_audit(
            conn,
            actor=actor,
            action="generation_batch.regenerate",
            entity_type="generation_batch",
            entity_id=new_batch_id,
            metadata={
                "project_id": project_id,
                "source_batch_id": batch_id,
                "source_task_ids": source_task_ids,
                "quantity": len(source_tasks),
                "generation_reason": request.generation_reason,
                "estimated_cost_snapshot": request.estimated_cost_snapshot,
                "idempotency_key_hash": content_hash(request.idempotency_key),
            },
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return get_generation_batch(conn, batch_id=new_batch_id, actor=actor)


def regenerate_generation_task(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    actor: CurrentUser,
    request: PaidRegenerationRequest,
) -> BatchResult:
    source_context = conn.execute(
        """
        SELECT generation_batches.project_id
        FROM generation_tasks
        JOIN generation_batches ON generation_batches.id = generation_tasks.batch_id
        WHERE generation_tasks.id = ?
        """,
        (task_id,),
    ).fetchone()
    if source_context is None:
        raise generation_error(404, "TASK_NOT_FOUND", "Generation task does not exist.")
    project_id = str(source_context["project_id"])
    require_not_auditor(
        conn,
        actor=actor,
        action="generation_task.regenerate",
        entity_type="generation_task",
        entity_id=task_id,
    )
    require_project_access(
        conn,
        actor=actor,
        project_id=project_id,
        action="generation_task.regenerate",
    )

    new_batch_id = ""
    try:
        conn.execute("BEGIN IMMEDIATE")
        source = conn.execute(
            """
            SELECT
                task.id,
                task.batch_id,
                task.generation_mode,
                task.provider,
                task.model,
                task.provider_task_id,
                task.provider_result_url,
                task.status,
                task.archive_status,
                task.quality_status,
                task.quality_issue_codes,
                task.submitted_at,
                task.prompt_version_id,
                task.prompt_snapshot_json,
                task.superseded_by_task_id,
                batch.project_id,
                batch.request_snapshot_json
            FROM generation_tasks AS task
            JOIN generation_batches AS batch ON batch.id = task.batch_id
            WHERE task.id = ?
            """,
            (task_id,),
        ).fetchone()
        if source is None:
            raise generation_error(404, "TASK_NOT_FOUND", "Generation task does not exist.")

        request_snapshot = required_snapshot_text(source["request_snapshot_json"])
        prompt_snapshot = required_snapshot_text(source["prompt_snapshot_json"])
        source_batch_id = str(source["batch_id"])
        request_hash = paid_regeneration_request_hash(
            request,
            operation_kind="TASK_PAID_REGENERATE",
            source_batch_id=source_batch_id,
            source_task_ids=[task_id],
            request_snapshot=request_snapshot,
            prompt_snapshots=[prompt_snapshot],
        )
        existing = _find_idempotent_batch(
            conn,
            actor_id=actor.id,
            project_id=project_id,
            key=request.idempotency_key,
        )
        if existing is not None:
            conn.rollback()
            require_matching_idempotent_batch(existing, request_hash=request_hash)
            return get_generation_batch(conn, batch_id=str(existing["id"]), actor=actor)

        if source["superseded_by_task_id"] is not None:
            raise generation_error(
                409,
                "SOURCE_TASK_ALREADY_SUPERSEDED",
                "This source task already has a paid replacement.",
            )
        require_task_paid_regeneration_state(source)
        require_regeneration_provider_ready(conn, provider_name=str(source["provider"]))

        new_batch_id = str(uuid4())
        replacement_task_id = str(uuid4())
        conn.execute(
            """
            INSERT INTO generation_batches (
                id, project_id, created_by_user_id, idempotency_key,
                request_hash, request_snapshot_json, status,
                source_batch_id, source_task_id, generation_reason
            )
            VALUES (?, ?, ?, ?, ?, ?, 'QUEUED', ?, ?, ?)
            """,
            (
                new_batch_id,
                project_id,
                actor.id,
                request.idempotency_key,
                request_hash,
                request_snapshot,
                source_batch_id,
                task_id,
                request.generation_reason,
            ),
        )
        conn.execute(
            """
            INSERT INTO generation_tasks (
                id, batch_id, generation_mode, provider, model,
                status, archive_status, quality_status,
                prompt_version_id, prompt_snapshot_json, next_poll_at,
                estimated_cost, retry_of_task_id, retry_reason,
                retry_requested_by_user_id, retry_requested_at
            )
            VALUES (?, ?, ?, ?, ?, 'PENDING', 'PENDING', 'PENDING',
                    ?, ?, CURRENT_TIMESTAMP, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (
                replacement_task_id,
                new_batch_id,
                str(source["generation_mode"]),
                str(source["provider"]),
                str(source["model"]),
                source["prompt_version_id"],
                prompt_snapshot,
                request.estimated_cost_snapshot,
                task_id,
                request.generation_reason,
                actor.id,
            ),
        )
        _reserve_generation_credit(
            conn,
            user_id=actor.id,
            task_id=replacement_task_id,
        )
        cursor = conn.execute(
            """
            UPDATE generation_tasks
            SET superseded_by_task_id = ?, superseded_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ? AND superseded_by_task_id IS NULL
            """,
            (replacement_task_id, task_id),
        )
        if cursor.rowcount != 1:
            raise generation_error(
                409,
                "SOURCE_TASK_ALREADY_SUPERSEDED",
                "This source task already has a paid replacement.",
            )
        _refresh_batch_status_in_transaction(conn, batch_id=source_batch_id)
        insert_audit(
            conn,
            actor=actor,
            action="generation_task.regenerate",
            entity_type="generation_task",
            entity_id=replacement_task_id,
            metadata={
                "project_id": project_id,
                "source_batch_id": source_batch_id,
                "source_task_id": task_id,
                "replacement_batch_id": new_batch_id,
                "generation_reason": request.generation_reason,
                "estimated_cost_snapshot": request.estimated_cost_snapshot,
                "idempotency_key_hash": content_hash(request.idempotency_key),
            },
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return get_generation_batch(conn, batch_id=new_batch_id, actor=actor)


def required_snapshot_text(value: Any) -> str:
    if value is None:
        raise generation_error(
            409,
            "SOURCE_SNAPSHOT_UNAVAILABLE",
            "The frozen generation snapshot is unavailable.",
        )
    text = str(value)
    try:
        json.loads(text)
    except json.JSONDecodeError as exc:
        raise generation_error(
            409,
            "SOURCE_SNAPSHOT_INVALID",
            "The frozen generation snapshot is invalid.",
        ) from exc
    return text


def paid_regeneration_request_hash(
    request: PaidRegenerationRequest,
    *,
    operation_kind: Literal["MANUAL_BATCH_REGENERATE", "TASK_PAID_REGENERATE"],
    source_batch_id: str,
    source_task_ids: Sequence[str],
    request_snapshot: str,
    prompt_snapshots: Sequence[str],
) -> str:
    payload = {
        "operation_kind": operation_kind,
        "source_batch_id": source_batch_id,
        "source_task_ids": list(source_task_ids),
        "generation_reason": request.generation_reason,
        "request_snapshot_hash": canonical_snapshot_hash(request_snapshot),
        "prompt_snapshot_hashes": [
            canonical_snapshot_hash(snapshot) for snapshot in prompt_snapshots
        ],
        "quantity": len(source_task_ids),
        "estimated_cost_snapshot": request.estimated_cost_snapshot,
    }
    return content_hash(json.dumps(payload, ensure_ascii=True, sort_keys=True))


def canonical_snapshot_hash(snapshot: str) -> str:
    parsed = json.loads(snapshot)
    return content_hash(json.dumps(parsed, ensure_ascii=True, sort_keys=True))


def require_matching_idempotent_batch(row: sqlite3.Row, *, request_hash: str) -> None:
    if str(row["request_hash"]) != request_hash:
        raise generation_error(
            409,
            "IDEMPOTENCY_CONFLICT",
            "This idempotency key was already used for a different request.",
        )


def require_regeneration_provider_ready(
    conn: sqlite3.Connection,
    *,
    provider_name: str,
) -> None:
    if provider_name != "metaso":
        return
    try:
        metaso_h3_provider_from_settings(conn)
    except H3ProviderSettingsUnavailable as exc:
        raise generation_error(
            503,
            "METASO_SETTINGS_UNAVAILABLE",
            "Save a readable METASO API Key before queuing a real H3 task.",
        ) from exc
    require_cos_first_frame_storage(conn)


def require_task_paid_regeneration_state(row: sqlite3.Row) -> None:
    if str(row["archive_status"]) == "ARCHIVE_FAILED":
        raise generation_error(
            409,
            "ARCHIVE_RETRY_ONLY",
            "Archive failures must recover the existing paid result without a new Provider call.",
        )
    if str(row["status"]) == "SUBMISSION_UNCERTAIN":
        raise generation_error(
            409,
            "MUST_RECONCILE_SUBMISSION",
            "An uncertain submission must be reconciled before any paid regeneration.",
        )
    quality_issue_codes = parse_json_list(row["quality_issue_codes"])
    if (
        str(row["quality_status"]) == "AUDIO_QUALITY_FAILED"
        or "AUDIO_QUALITY_FAILED" in quality_issue_codes
    ):
        return
    if str(row["status"]) == "FAILED" and (
        row["provider_task_id"] is not None
        or row["provider_result_url"] is not None
        or row["submitted_at"] is not None
    ):
        return
    raise generation_error(
        409,
        "PAID_REGENERATION_NOT_ALLOWED",
        "Only audio-quality failures or failed submitted tasks can be regenerated "
        "with a new paid call.",
    )


def paid_cost_per_task(total: float | None, *, quantity: int) -> float | None:
    if total is None:
        return None
    return round(total / quantity, 6)


def require_confirmed_first_frame(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    first_frame_asset_id: str,
) -> None:
    selection = conn.execute(
        """
        SELECT id, payload_json
        FROM versions
        WHERE project_id = ? AND kind = 'first_frame_selection'
        ORDER BY version_number DESC
        LIMIT 1
        """,
        (project_id,),
    ).fetchone()
    candidates = conn.execute(
        """
        SELECT id, payload_json
        FROM versions
        WHERE project_id = ? AND kind = 'first_frame_candidates'
        ORDER BY version_number DESC
        LIMIT 1
        """,
        (project_id,),
    ).fetchone()
    if selection is None or candidates is None:
        raise generation_error(
            409,
            "FIRST_FRAME_CONFIRMATION_REQUIRED",
            "Confirm a first-frame candidate before compiling or submitting H3 generation.",
        )
    try:
        selection_payload = json.loads(str(selection["payload_json"]))
        candidate_payload = json.loads(str(candidates["payload_json"]))
    except json.JSONDecodeError as exc:
        raise generation_error(
            409,
            "FIRST_FRAME_CONFIRMATION_REQUIRED",
            "Confirm a first-frame candidate before compiling or submitting H3 generation.",
        ) from exc
    candidate_values = (
        candidate_payload.get("candidates") if isinstance(candidate_payload, dict) else None
    )
    candidate_asset_ids = (
        {
            value.get("asset_id")
            for value in candidate_values
            if isinstance(value, dict) and isinstance(value.get("asset_id"), str)
        }
        if isinstance(candidate_values, list)
        else set()
    )
    is_current_confirmation = (
        isinstance(selection_payload, dict)
        and selection_payload.get("first_frame_candidates_version_id") == str(candidates["id"])
        and selection_payload.get("first_frame_asset_id") == first_frame_asset_id
        and first_frame_asset_id in candidate_asset_ids
    )
    if not is_current_confirmation:
        raise generation_error(
            409,
            "FIRST_FRAME_CONFIRMATION_REQUIRED",
            "Confirm a first-frame candidate from the latest candidate set before H3 generation.",
        )
    if (
        isinstance(candidate_payload, dict)
        and candidate_payload.get("schema_version") == "b5.first-frame.v1"
    ):
        from app.first_frames import current_first_frame_candidates

        try:
            current_first_frame_candidates(conn, project_id=project_id)
        except HTTPException as exc:
            raise generation_error(
                409,
                "FIRST_FRAME_CONFIRMATION_REQUIRED",
                "The confirmed first frame is stale; generate and confirm it again before H3.",
            ) from exc


def require_cos_first_frame_storage(
    conn: sqlite3.Connection,
    *,
    storage_uri: str | None = None,
) -> None:
    """真实 Metaso 生成只接受当前 COS 配置下的首帧。"""
    try:
        config = SettingsRepository(conn).load_provider_config("cos")
        if not config:
            raise ValueError("COS settings are missing")
        cloud = cloud_storage_config_from_settings("cos", config)
        if storage_uri is not None:
            first_frame = storage_object_ref_from_uri(storage_uri)
            if first_frame.provider != "cos" or first_frame.bucket != cloud.bucket:
                raise ValueError("first frame is not stored in the configured COS bucket")
    except (SettingsUnavailableError, ValueError) as exc:
        raise generation_error(
            422,
            "METASO_REQUIRES_CLOUD_STORAGE",
            "METASO H3 requires an HTTPS first-frame URL; save COS settings before generating.",
        ) from exc


def run_next_generation_task(
    conn: sqlite3.Connection,
    *,
    worker_id: str,
    provider: H3Provider | None,
    storage: StorageAdapter,
    first_frame_storage: StorageAdapter | None = None,
) -> TaskResult | None:
    lease = acquire_generation_task_lease(conn, worker_id=worker_id)
    if lease is None:
        return None

    task_id = str(lease["id"])
    batch_id = str(lease["batch_id"])
    source_storage = first_frame_storage or storage
    archive_retry = bool(
        lease.get("archive_status") == "ARCHIVE_FAILED" and lease.get("provider_result_url")
    )
    if str(lease["provider"]) == "metaso" and (
        storage.provider != "cos" or (not archive_retry and source_storage.provider != "cos")
    ):
        if archive_retry:
            _release_archive_retry(conn, task_id=task_id, batch_id=batch_id)
        else:
            mark_task_provider_settings_unavailable(
                conn,
                task_id=task_id,
                batch_id=batch_id,
            )
        return get_task_result(conn, task_id)
    if provider is None:
        try:
            provider = h3_provider_for_task(conn, str(lease["provider"]))
        except H3ProviderSettingsUnavailable:
            if lease.get("archive_status") == "ARCHIVE_FAILED" and lease.get("provider_result_url"):
                # Archive retry never starts a paid call; if the provider settings
                # vanished, back off instead of failing the paid result forever.
                _release_archive_retry(conn, task_id=task_id, batch_id=batch_id)
                return get_task_result(conn, task_id)
            mark_task_provider_settings_unavailable(
                conn,
                task_id=task_id,
                batch_id=batch_id,
            )
            return get_task_result(conn, task_id)
    if isinstance(provider, MetasoH3Provider) and provider.task_created_observer is None:

        def _record_created_provider_task(created_provider_task_id: str) -> None:
            # Persist the paid provider task id the moment it exists so a later
            # timeout/crash can be reconciled instead of losing the result.
            mark_task_submission_uncertain(
                conn,
                task_id=task_id,
                message="METASO task was created; the result is still being awaited.",
                provider_task_id=created_provider_task_id,
            )

        provider.task_created_observer = _record_created_provider_task
    if lease.get("archive_status") == "ARCHIVE_FAILED" and lease.get("provider_result_url"):
        return _retry_archive(
            conn,
            task_id=task_id,
            batch_id=batch_id,
            storage=storage,
            provider=provider,
        )
    try:
        first_frame = storage_object_ref_from_uri(str(lease["first_frame_uri"]))
        require_storage_match(source_storage, first_frame)
        first_frame_url = source_storage.create_download_intent(
            first_frame.key,
            expires_in=FIRST_FRAME_URL_EXPIRES_IN,
            can_read=True,
        ).url
    except (StorageBackendUnavailable, StoragePermissionError, ValueError):
        mark_task_first_frame_url_sign_failed(
            conn,
            task_id=task_id,
            batch_id=str(lease["batch_id"]),
        )
        return get_task_result(conn, task_id)
    provider_request = build_h3_request(
        prompt_text=str(lease["prompt_text"]),
        first_frame_url=first_frame_url,
        duration_seconds=int(lease["output_duration_seconds"]),
        resolution=str(lease["resolution"]),
    )
    request_hash = content_hash(json.dumps(provider_request, ensure_ascii=True, sort_keys=True))
    try:
        provider_result = provider.create_image_to_video(provider_request)
    except SubmissionUncertain as exc:
        mark_task_submission_uncertain(conn, task_id=task_id, message=str(exc))
        return get_task_result(conn, task_id)
    except H3ProviderFailed as exc:
        if exc.provider_task_id is not None and not exc.terminal:
            mark_task_submission_uncertain(
                conn,
                task_id=task_id,
                message="METASO task needs recovery after a nonterminal provider failure.",
                provider_task_id=exc.provider_task_id,
            )
            return get_task_result(conn, task_id)
        mark_task_provider_failed(
            conn,
            task_id=task_id,
            batch_id=str(lease["batch_id"]),
            provider_task_id=exc.provider_task_id,
        )
        return get_task_result(conn, task_id)

    result_asset_id: str | None = None
    stored: StoredObject | None = None
    archive_status = "ARCHIVED"
    error_code = None
    error_message = None
    # 成功路径也保留 Provider 直连链接：客户端优先用它在线播放（零下载
    # 等待）；归档失败时它同时是后续 Worker 补救重下的唯一来源（会过期）。
    retained_result_url = provider_result.result_url
    try:
        stored = storage.put_object(
            f"generation-results/{task_id}.mp4",
            provider_result.result_content,
            content_type="video/mp4",
        )
        _verify_archived_result(storage, stored)
    except (StorageBackendUnavailable, StoragePermissionError, ValueError):
        if stored is not None:
            try:
                storage.delete_object(stored.key, actor_id=None)
            except Exception as cleanup_exc:
                logger.warning(
                    "archive verification cleanup failed for task %s: %s",
                    task_id,
                    type(cleanup_exc).__name__,
                )
            stored = None
        archive_status = "ARCHIVE_FAILED"
        error_code = "ARCHIVE_STORAGE_UNAVAILABLE"
        error_message = "Generation result could not be archived to configured storage."
    else:
        result_asset_id = str(uuid4())
    try:
        with conn:
            if stored is not None and result_asset_id is not None:
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
                    (
                        result_asset_id,
                        str(lease["project_id"]),
                        "video",
                        stored.uri,
                        stored.sha256,
                        stored.size,
                        stored.content_type,
                        str(lease["created_by_user_id"]),
                    ),
                )
            conn.execute(
                """
                UPDATE generation_tasks
                SET
                    provider_task_id = ?,
                    status = 'SUCCEEDED',
                    archive_status = ?,
                    quality_status = ?,
                    quality_issue_codes = ?,
                    result_asset_id = ?,
                    provider_request_json = ?,
                    provider_result_url = ?,
                    error_code = ?,
                    error_message_redacted = ?,
                    submitted_at = COALESCE(submitted_at, CURRENT_TIMESTAMP),
                    completed_at = CURRENT_TIMESTAMP,
                    locked_by = NULL,
                    locked_until = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (
                    provider_result.provider_task_id,
                    archive_status,
                    provider_result.audio_quality_status,
                    json.dumps(provider_result.quality_issue_codes, ensure_ascii=True),
                    result_asset_id,
                    json.dumps(provider_request, ensure_ascii=True, sort_keys=True),
                    retained_result_url,
                    error_code,
                    error_message,
                    task_id,
                ),
            )
            conn.execute(
                """
                INSERT INTO external_call_logs (
                    id,
                    generation_task_id,
                    provider,
                    model,
                    endpoint_name,
                    provider_request_id,
                    http_status,
                    request_hash
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid4()),
                    task_id,
                    str(lease["provider"]),
                    H3_MODEL,
                    "createImageToVideo",
                    provider_result.provider_task_id,
                    200,
                    request_hash,
                ),
            )
            if archive_status == "ARCHIVED":
                finalize_internal_billing(conn, task_id=task_id, outcome="success")
            _refresh_batch_status_in_transaction(conn, batch_id=str(lease["batch_id"]))
    except Exception:
        if stored is not None:
            try:
                storage.delete_object(stored.key, actor_id=None)
            except Exception as cleanup_exc:
                logger.warning(
                    "archive rollback cleanup failed for task %s: %s",
                    task_id,
                    type(cleanup_exc).__name__,
                )
        raise
    return get_task_result(conn, task_id)


def _verify_archived_result(storage: StorageAdapter, stored: StoredObject) -> None:
    archived = storage.head_object(stored.key)
    if archived is None or archived.size != stored.size:
        raise StorageBackendUnavailable("archived result metadata is unavailable")
    storage.create_download_intent(
        stored.key,
        expires_in=RESULT_DOWNLOAD_CHECK_EXPIRES_IN,
        can_read=True,
    )


def _store_and_finalize_archive(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    batch_id: str,
    project_id: str,
    created_by_user_id: str,
    content: bytes,
    storage: StorageAdapter,
    reconcile_reservation: ReconcileReservation | None = None,
) -> TaskResult:
    """Archive already-downloaded H3 result bytes and mark the task terminal."""
    object_key = (
        f"generation-results/{task_id}/{reconcile_reservation.id}.mp4"
        if reconcile_reservation is not None
        else f"generation-results/{task_id}.mp4"
    )
    stored = storage.put_object(object_key, content, content_type="video/mp4")
    result_asset_id = str(uuid4())
    task_state_guard = (
        "AND status = 'SUBMISSION_UNCERTAIN' AND result_asset_id IS NULL"
        if reconcile_reservation is not None
        else ""
    )
    try:
        _verify_archived_result(storage, stored)
        with conn:
            if reconcile_reservation is not None:
                _renew_reconcile_reservation_in_transaction(
                    conn,
                    reservation_id=reconcile_reservation.id,
                )
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
                VALUES (?, ?, 'video', ?, ?, ?, ?, ?)
                """,
                (
                    result_asset_id,
                    project_id,
                    stored.uri,
                    stored.sha256,
                    stored.size,
                    stored.content_type,
                    created_by_user_id,
                ),
            )
            task_update = conn.execute(
                f"""
                UPDATE generation_tasks
                SET
                    status = 'SUCCEEDED',
                    archive_status = 'ARCHIVED',
                    result_asset_id = ?,
                    error_code = NULL,
                    error_message_redacted = NULL,
                    locked_by = NULL,
                    locked_until = NULL,
                    completed_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                {task_state_guard}
                """,
                (result_asset_id, task_id),
            )
            if reconcile_reservation is not None and task_update.rowcount != 1:
                raise _reconcile_reservation_lost()
            finalize_internal_billing(conn, task_id=task_id, outcome="success")
            _refresh_batch_status_in_transaction(conn, batch_id=batch_id)
            result = get_task_result(conn, task_id)
            if reconcile_reservation is not None:
                _complete_reconcile_operation_in_transaction(
                    conn,
                    reservation=reconcile_reservation,
                    task_id=task_id,
                    batch_id=batch_id,
                    project_id=project_id,
                    result=result,
                )
    except Exception:
        # The object was written outside the DB transaction; remove it if the
        # asset/task rows could not be committed, so no orphan object is left.
        try:
            storage.delete_object(object_key, actor_id=None)
        except Exception as cleanup_exc:
            logger.warning(
                "archive cleanup failed for task %s: %s", task_id, type(cleanup_exc).__name__
            )
        raise
    return result


def _retry_archive(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    batch_id: str,
    storage: StorageAdapter,
    provider: H3Provider,
) -> TaskResult:
    """Re-download a paid H3 result whose earlier archive attempt failed and
    archive it to enterprise storage. Keeps the task retryable on failure."""
    row = conn.execute(
        """
        SELECT
            generation_tasks.provider_result_url,
            generation_batches.project_id,
            generation_batches.created_by_user_id
        FROM generation_tasks
        JOIN generation_batches ON generation_batches.id = generation_tasks.batch_id
        WHERE generation_tasks.id = ?
        """,
        (task_id,),
    ).fetchone()
    if row is None or not row["provider_result_url"]:
        return get_task_result(conn, task_id)
    result_url = str(row["provider_result_url"])
    try:
        content = provider.download_result(result_url)
    except Exception as exc:
        logger.warning("archive retry download failed for task %s: %s", task_id, type(exc).__name__)
        _release_archive_retry(conn, task_id=task_id, batch_id=batch_id)
        return get_task_result(conn, task_id)
    try:
        return _store_and_finalize_archive(
            conn,
            task_id=task_id,
            batch_id=batch_id,
            project_id=str(row["project_id"]),
            created_by_user_id=str(row["created_by_user_id"]),
            content=content,
            storage=storage,
        )
    except Exception as exc:
        logger.warning("archive retry put failed for task %s: %s", task_id, type(exc).__name__)
        _release_archive_retry(conn, task_id=task_id, batch_id=batch_id)
        return get_task_result(conn, task_id)


def generation_task_operation_hash(
    *,
    action: str,
    task_id: str,
    payload: Mapping[str, Any],
) -> str:
    return content_hash(
        json.dumps(
            {"action": action, "task_id": task_id, "payload": dict(payload)},
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
    )


def _idempotent_task_operation(
    conn: sqlite3.Connection,
    *,
    actor_id: str,
    task_id: str,
    action: str,
    idempotency_key: str,
    request_hash: str,
) -> sqlite3.Row | None:
    row = conn.execute(
        """
        SELECT request_hash, result_status
        FROM generation_task_operations
        WHERE actor_user_id = ? AND task_id = ? AND action = ? AND idempotency_key = ?
        """,
        (actor_id, task_id, action, idempotency_key),
    ).fetchone()
    if row is not None and str(row["request_hash"]) != request_hash:
        raise generation_error(
            409,
            "IDEMPOTENCY_CONFLICT",
            "This idempotency key was already used for a different task operation.",
        )
    return cast(sqlite3.Row | None, row)


def _record_completed_task_operation(
    conn: sqlite3.Connection,
    *,
    actor: CurrentUser,
    task_id: str,
    action: str,
    idempotency_key: str,
    request_hash: str,
    response: Mapping[str, Any],
) -> None:
    conn.execute(
        """
        INSERT INTO generation_task_operations (
            id, task_id, actor_user_id, action, idempotency_key,
            request_hash, result_task_id, result_status, response_snapshot_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, 'COMPLETED', ?)
        """,
        (
            str(uuid4()),
            task_id,
            actor.id,
            action,
            idempotency_key,
            request_hash,
            task_id,
            json.dumps(dict(response), ensure_ascii=True, sort_keys=True),
        ),
    )


def _release_stale_reconcile_reservation(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    batch_id: str,
    project_id: str,
    actor: CurrentUser,
) -> None:
    row = conn.execute(
        """
        SELECT id, idempotency_key
        FROM generation_task_operations
        WHERE task_id = ? AND action = 'RECONCILE' AND result_status = 'PENDING'
          AND datetime(updated_at) <= datetime('now', ?)
        """,
        (task_id, f"-{RECONCILIATION_RESERVATION_SECONDS} seconds"),
    ).fetchone()
    if row is None:
        return
    conn.execute(
        """
        DELETE FROM generation_task_operations
        WHERE id = ? AND result_status = 'PENDING'
        """,
        (str(row["id"]),),
    )
    insert_audit(
        conn,
        actor=actor,
        action="generation_task.reconcile_stale_reservation_released",
        entity_type="generation_task",
        entity_id=task_id,
        metadata={
            "batch_id": batch_id,
            "project_id": project_id,
            "idempotency_key_hash": content_hash(str(row["idempotency_key"])),
            "reservation_timeout_seconds": RECONCILIATION_RESERVATION_SECONDS,
        },
    )


def _renew_reconcile_reservation(
    conn: sqlite3.Connection,
    *,
    reservation: ReconcileReservation | None,
) -> None:
    if reservation is None:
        return
    with conn:
        _renew_reconcile_reservation_in_transaction(conn, reservation_id=reservation.id)


def _renew_reconcile_reservation_in_transaction(
    conn: sqlite3.Connection,
    *,
    reservation_id: str,
) -> None:
    cursor = conn.execute(
        """
        UPDATE generation_task_operations
        SET updated_at = CURRENT_TIMESTAMP
        WHERE id = ? AND action = 'RECONCILE' AND result_status = 'PENDING'
        """,
        (reservation_id,),
    )
    if cursor.rowcount != 1:
        raise _reconcile_reservation_lost()


def _reconcile_reservation_lost() -> HTTPException:
    return generation_error(
        409,
        "RECONCILE_RESERVATION_LOST",
        "The reconciliation reservation expired and was taken over; retry the request.",
    )


def _complete_reconcile_operation_in_transaction(
    conn: sqlite3.Connection,
    *,
    reservation: ReconcileReservation,
    task_id: str,
    batch_id: str,
    project_id: str,
    result: TaskResult,
) -> None:
    cursor = conn.execute(
        """
        UPDATE generation_task_operations
        SET
            result_status = 'COMPLETED',
            response_snapshot_json = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ? AND actor_user_id = ? AND task_id = ?
          AND action = 'RECONCILE' AND idempotency_key = ? AND result_status = 'PENDING'
        """,
        (
            json.dumps(
                {
                    "status": result.status,
                    "archive_status": result.archive_status,
                    "result_asset_id": result.result_asset_id,
                },
                ensure_ascii=True,
                sort_keys=True,
            ),
            reservation.id,
            reservation.actor.id,
            task_id,
            reservation.idempotency_key,
        ),
    )
    if cursor.rowcount != 1:
        raise _reconcile_reservation_lost()
    insert_audit(
        conn,
        actor=reservation.actor,
        action=(
            "generation_task.reconcile_archived"
            if result.archive_status == "ARCHIVED"
            else "generation_task.reconcile_terminal_failed"
        ),
        entity_type="generation_task",
        entity_id=task_id,
        metadata={
            "batch_id": batch_id,
            "project_id": project_id,
            "status": result.status,
            "archive_status": result.archive_status,
        },
    )


def retry_generation_task(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    actor: CurrentUser,
    request: GenerationTaskRetryRequest,
) -> TaskResult:
    action = "RETRY"
    request_hash = generation_task_operation_hash(
        action=action,
        task_id=task_id,
        payload={"retry_reason": request.retry_reason},
    )
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = _idempotent_task_operation(
            conn,
            actor_id=actor.id,
            task_id=task_id,
            action=action,
            idempotency_key=request.idempotency_key,
            request_hash=request_hash,
        )
        if existing is not None:
            conn.commit()
            return get_task_result(conn, task_id)

        row = conn.execute(
            """
            SELECT
                task.batch_id,
                batch.project_id,
                batch.created_by_user_id,
                task.status,
                task.archive_status,
                task.quality_status,
                task.provider_task_id,
                task.provider_result_url,
                task.result_asset_id,
                task.archive_retry_count,
                task.error_code,
                task.submitted_at,
                task.superseded_by_task_id
            FROM generation_tasks AS task
            JOIN generation_batches AS batch ON batch.id = task.batch_id
            WHERE task.id = ?
            """,
            (task_id,),
        ).fetchone()
        if row is None:
            raise generation_error(404, "TASK_NOT_FOUND", "Generation task does not exist.")

        status = str(row["status"])
        archive_status = str(row["archive_status"])
        quality_status = str(row["quality_status"])
        provider_task_id = optional_text(row["provider_task_id"])
        provider_result_url = optional_text(row["provider_result_url"])
        error_code = optional_text(row["error_code"])

        if row["superseded_by_task_id"] is not None:
            raise generation_error(
                409,
                "TASK_SUPERSEDED",
                "This historical task has already been replaced and cannot be retried.",
            )

        if archive_status == "ARCHIVE_FAILED":
            if (
                provider_result_url is None
                or int(row["archive_retry_count"]) >= MAX_ARCHIVE_RETRIES
            ):
                raise generation_error(
                    409,
                    "ARCHIVE_RESULT_UNAVAILABLE",
                    "The paid result can no longer be recovered from the provider URL.",
                )
            retry_path = "ARCHIVE_ONLY"
            audit_action = "generation_task.archive_retry_queued"
            conn.execute(
                """
                UPDATE generation_tasks
                SET
                    status = 'SUCCEEDED',
                    locked_by = NULL,
                    locked_until = NULL,
                    next_poll_at = CURRENT_TIMESTAMP,
                    retry_reason = ?,
                    retry_requested_by_user_id = ?,
                    retry_requested_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (request.retry_reason, actor.id, task_id),
            )
        elif quality_status == "AUDIO_QUALITY_FAILED":
            raise generation_error(
                409,
                "REQUIRES_PAID_REGENERATION",
                "Audio quality failures require an explicit paid video regeneration.",
            )
        elif status == "SUBMISSION_UNCERTAIN":
            if provider_task_id is not None:
                raise generation_error(
                    409,
                    "MUST_RECONCILE_SUBMISSION",
                    "This task has a provider task id and must be reconciled.",
                )
            raise generation_error(
                409,
                "ADMIN_BILLING_CONFIRMATION_REQUIRED",
                "An admin must confirm that the provider did not charge before requeueing.",
            )
        elif status == "FAILED":
            if (
                provider_task_id is not None
                or provider_result_url is not None
                or row["submitted_at"] is not None
                or error_code not in SAFE_PRE_PROVIDER_FAILURE_CODES
            ):
                raise generation_error(
                    409,
                    "REQUIRES_PAID_REGENERATION",
                    "This task may already have reached the provider and cannot be "
                    "retried in place.",
                )
            retry_path = "PRE_PROVIDER"
            audit_action = "generation_task.retry_queued"
            _reserve_generation_credit(
                conn,
                user_id=str(row["created_by_user_id"]),
                task_id=task_id,
                billing_round=None,
            )
            conn.execute(
                """
                UPDATE generation_tasks
                SET
                    status = 'PENDING',
                    archive_status = 'PENDING',
                    quality_status = 'PENDING',
                    quality_issue_codes = '[]',
                    error_code = NULL,
                    error_message_redacted = NULL,
                    locked_by = NULL,
                    locked_until = NULL,
                    next_poll_at = CURRENT_TIMESTAMP,
                    submitted_at = NULL,
                    started_at = NULL,
                    completed_at = NULL,
                    retry_reason = ?,
                    retry_requested_by_user_id = ?,
                    retry_requested_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (request.retry_reason, actor.id, task_id),
            )
        else:
            raise generation_error(
                409,
                "TASK_RETRY_NOT_ALLOWED",
                "The task is not in a safely retryable state.",
            )

        _record_completed_task_operation(
            conn,
            actor=actor,
            task_id=task_id,
            action=action,
            idempotency_key=request.idempotency_key,
            request_hash=request_hash,
            response={"retry_path": retry_path},
        )
        insert_audit(
            conn,
            actor=actor,
            action=audit_action,
            entity_type="generation_task",
            entity_id=task_id,
            metadata={
                "batch_id": str(row["batch_id"]),
                "project_id": str(row["project_id"]),
                "previous_status": status,
                "previous_archive_status": archive_status,
                "retry_path": retry_path,
                "retry_reason": request.retry_reason,
                "idempotency_key_hash": content_hash(request.idempotency_key),
            },
        )
        _refresh_batch_status_in_transaction(conn, batch_id=str(row["batch_id"]))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return get_task_result(conn, task_id)


def confirm_generation_task_not_charged(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    actor: CurrentUser,
    request: ConfirmNotChargedRequest,
) -> TaskResult:
    action = "CONFIRM_NOT_CHARGED"
    request_hash = generation_task_operation_hash(
        action=action,
        task_id=task_id,
        payload={"reason": request.reason},
    )
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = _idempotent_task_operation(
            conn,
            actor_id=actor.id,
            task_id=task_id,
            action=action,
            idempotency_key=request.idempotency_key,
            request_hash=request_hash,
        )
        if existing is not None:
            conn.commit()
            return get_task_result(conn, task_id)

        row = conn.execute(
            """
            SELECT
                task.batch_id,
                batch.project_id,
                task.status,
                task.provider_task_id,
                task.provider_result_url,
                task.result_asset_id,
                task.superseded_by_task_id
            FROM generation_tasks AS task
            JOIN generation_batches AS batch ON batch.id = task.batch_id
            WHERE task.id = ?
            """,
            (task_id,),
        ).fetchone()
        if row is None:
            raise generation_error(404, "TASK_NOT_FOUND", "Generation task does not exist.")
        if row["superseded_by_task_id"] is not None:
            raise generation_error(
                409,
                "TASK_SUPERSEDED",
                "This historical task has already been replaced.",
            )
        if str(row["status"]) != "SUBMISSION_UNCERTAIN":
            raise generation_error(
                409,
                "TASK_NOT_UNCERTAIN",
                "Only an uncertain submission can be confirmed as unbilled.",
            )
        if any(
            optional_text(row[column]) is not None
            for column in ("provider_task_id", "provider_result_url", "result_asset_id")
        ):
            raise generation_error(
                409,
                "SUBMISSION_MAY_HAVE_BEEN_CHARGED",
                "Provider evidence exists; reconcile or use paid regeneration instead.",
            )

        conn.execute(
            """
            UPDATE generation_tasks
            SET
                status = 'PENDING',
                error_code = NULL,
                error_message_redacted = NULL,
                locked_by = NULL,
                locked_until = NULL,
                next_poll_at = CURRENT_TIMESTAMP,
                submitted_at = NULL,
                started_at = NULL,
                completed_at = NULL,
                retry_reason = ?,
                retry_requested_by_user_id = ?,
                retry_requested_at = CURRENT_TIMESTAMP,
                billing_confirmation_status = 'CONFIRMED_NOT_CHARGED',
                billing_confirmed_by_user_id = ?,
                billing_confirmed_at = CURRENT_TIMESTAMP,
                billing_confirmation_reason = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (request.reason, actor.id, actor.id, request.reason, task_id),
        )
        _record_completed_task_operation(
            conn,
            actor=actor,
            task_id=task_id,
            action=action,
            idempotency_key=request.idempotency_key,
            request_hash=request_hash,
            response={"billing_confirmation_status": "CONFIRMED_NOT_CHARGED"},
        )
        insert_audit(
            conn,
            actor=actor,
            action="generation_task.billing_confirmed_not_charged",
            entity_type="generation_task",
            entity_id=task_id,
            metadata={
                "batch_id": str(row["batch_id"]),
                "project_id": str(row["project_id"]),
                "reason": request.reason,
                "idempotency_key_hash": content_hash(request.idempotency_key),
            },
        )
        _refresh_batch_status_in_transaction(conn, batch_id=str(row["batch_id"]))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return get_task_result(conn, task_id)


def reconcile_generation_task(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    batch_id: str,
    project_id: str,
    created_by_user_id: str,
    actor: CurrentUser,
    request: ReconcileGenerationTaskRequest,
    storage_factory: Callable[[], StorageAdapter],
    provider_factory: Callable[[], H3Provider],
) -> TaskResult:
    action = "RECONCILE"
    operation_id: str | None = None
    request_hash = generation_task_operation_hash(
        action=action,
        task_id=task_id,
        payload={},
    )
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT
                status,
                archive_status,
                result_asset_id,
                provider_task_id,
                superseded_by_task_id
            FROM generation_tasks
            WHERE id = ?
            """,
            (task_id,),
        ).fetchone()
        if row is None:
            raise generation_error(404, "TASK_NOT_FOUND", "Generation task does not exist.")
        if row["superseded_by_task_id"] is not None:
            raise generation_error(
                409,
                "TASK_SUPERSEDED",
                "This historical task has already been replaced.",
            )
        if str(row["status"]) == "SUBMISSION_UNCERTAIN":
            _release_stale_reconcile_reservation(
                conn,
                task_id=task_id,
                batch_id=batch_id,
                project_id=project_id,
                actor=actor,
            )
        existing = _idempotent_task_operation(
            conn,
            actor_id=actor.id,
            task_id=task_id,
            action=action,
            idempotency_key=request.idempotency_key,
            request_hash=request_hash,
        )
        if existing is not None:
            if str(existing["result_status"]) == "PENDING" and str(row["status"]) == (
                "SUBMISSION_UNCERTAIN"
            ):
                raise generation_error(
                    409,
                    "RECONCILE_IN_PROGRESS",
                    "A reconciliation request is already in progress for this task.",
                )
            if str(existing["result_status"]) == "PENDING":
                conn.execute(
                    """
                    UPDATE generation_task_operations
                    SET
                        result_status = 'COMPLETED',
                        response_snapshot_json = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE actor_user_id = ? AND task_id = ? AND action = ?
                      AND idempotency_key = ?
                    """,
                    (
                        json.dumps(
                            {
                                "status": str(row["status"]),
                                "archive_status": str(row["archive_status"]),
                                "result_asset_id": optional_text(row["result_asset_id"]),
                            },
                            ensure_ascii=True,
                            sort_keys=True,
                        ),
                        actor.id,
                        task_id,
                        action,
                        request.idempotency_key,
                    ),
                )
                insert_audit(
                    conn,
                    actor=actor,
                    action=(
                        "generation_task.reconcile_archived"
                        if str(row["archive_status"]) == "ARCHIVED"
                        else "generation_task.reconcile_terminal_failed"
                    ),
                    entity_type="generation_task",
                    entity_id=task_id,
                    metadata={
                        "batch_id": batch_id,
                        "project_id": project_id,
                        "status": str(row["status"]),
                        "archive_status": str(row["archive_status"]),
                        "recovered_pending_operation": True,
                    },
                )
            conn.commit()
            return get_task_result(conn, task_id)
        if str(row["status"]) != "SUBMISSION_UNCERTAIN":
            raise generation_error(
                409,
                "TASK_NOT_UNCERTAIN",
                "Only SUBMISSION_UNCERTAIN tasks can be reconciled.",
            )
        if optional_text(row["provider_task_id"]) is None:
            raise generation_error(
                409,
                "SUBMISSION_REQUIRES_MANUAL_CONFIRMATION",
                "There is no provider task id; an admin must confirm no charge occurred.",
            )
        operation_id = str(uuid4())
        conn.execute(
            """
            INSERT INTO generation_task_operations (
                id, task_id, actor_user_id, action, idempotency_key,
                request_hash, result_task_id, result_status
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, 'PENDING')
            """,
            (
                operation_id,
                task_id,
                actor.id,
                action,
                request.idempotency_key,
                request_hash,
                task_id,
            ),
        )
        insert_audit(
            conn,
            actor=actor,
            action="generation_task.reconcile_requested",
            entity_type="generation_task",
            entity_id=task_id,
            metadata={
                "batch_id": batch_id,
                "project_id": project_id,
                "idempotency_key_hash": content_hash(request.idempotency_key),
                "provider_task_id_tail": redacted_provider_task_tail(
                    optional_text(row["provider_task_id"])
                ),
            },
        )
        conn.commit()
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        raise generation_error(
            409,
            "RECONCILE_IN_PROGRESS",
            "A reconciliation request is already in progress for this task.",
        ) from exc
    except Exception:
        conn.rollback()
        raise

    if operation_id is None:
        raise RuntimeError("reconciliation reservation was not created")
    reservation = ReconcileReservation(
        id=operation_id,
        actor=actor,
        idempotency_key=request.idempotency_key,
    )
    try:
        provider = provider_factory()
        result = reconcile_submission_uncertain_task(
            conn,
            task_id=task_id,
            batch_id=batch_id,
            project_id=project_id,
            created_by_user_id=created_by_user_id,
            storage_factory=storage_factory,
            provider=provider,
            reconcile_reservation=reservation,
        )
    except Exception:
        with conn:
            conn.execute(
                """
                DELETE FROM generation_task_operations
                WHERE id = ? AND result_status = 'PENDING'
                """,
                (operation_id,),
            )
        raise
    return result


def reconcile_submission_uncertain_task(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    batch_id: str,
    project_id: str,
    created_by_user_id: str,
    storage_factory: Callable[[], StorageAdapter],
    provider: H3Provider,
    reconcile_reservation: ReconcileReservation | None = None,
) -> TaskResult:
    """Reconcile a SUBMISSION_UNCERTAIN task against the provider.

    If the provider task id is known, query it and archive the result when it
    succeeded, or mark it terminal on an explicit provider failure. When the
    create response was lost (no provider task id), refuse to guess: a human
    must first confirm the charge did not occur before any resubmission.
    """
    row = conn.execute(
        "SELECT status, provider_task_id FROM generation_tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    if row is None:
        raise LookupError("generation task disappeared")
    if row["status"] != "SUBMISSION_UNCERTAIN":
        raise generation_error(
            409, "TASK_NOT_UNCERTAIN", "Only SUBMISSION_UNCERTAIN tasks can be reconciled."
        )
    provider_task_id = row["provider_task_id"]
    if not provider_task_id:
        raise generation_error(
            409,
            "SUBMISSION_REQUIRES_MANUAL_CONFIRMATION",
            "The submission result is unknown and there is no provider task id; "
            "confirm the charge did not occur before deciding to resubmit.",
        )
    _renew_reconcile_reservation(conn, reservation=reconcile_reservation)
    try:
        item = provider._query_task(str(provider_task_id))
    except H3ProviderFailed as exc:
        raise generation_error(
            502, "PROVIDER_QUERY_FAILED", "Provider query failed during reconciliation."
        ) from exc
    _renew_reconcile_reservation(conn, reservation=reconcile_reservation)
    status = item.get("status")
    if status == "succeeded":
        result_url = _metaso_content_url(item, provider_task_id=str(provider_task_id))
        try:
            content = provider.download_result(result_url)
        except Exception as exc:
            raise generation_error(
                503, "RESULT_DOWNLOAD_FAILED", "Result download failed; retry reconciliation."
            ) from exc
        _renew_reconcile_reservation(conn, reservation=reconcile_reservation)
        storage = storage_factory()
        return _store_and_finalize_archive(
            conn,
            task_id=task_id,
            batch_id=batch_id,
            project_id=project_id,
            created_by_user_id=created_by_user_id,
            content=content,
            storage=storage,
            reconcile_reservation=reconcile_reservation,
        )
    if status in {"failed", "cancelled"}:
        with conn:
            if reconcile_reservation is not None:
                _renew_reconcile_reservation_in_transaction(
                    conn,
                    reservation_id=reconcile_reservation.id,
                )
            task_update = conn.execute(
                f"""
                UPDATE generation_tasks
                SET
                    status = 'FAILED',
                    error_code = 'PROVIDER_TERMINAL',
                    error_message_redacted = 'Provider reports the task finished with ' || ?,
                    locked_by = NULL,
                    locked_until = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                {"AND status = 'SUBMISSION_UNCERTAIN'" if reconcile_reservation is not None else ""}
                """,
                (str(status), task_id),
            )
            if reconcile_reservation is not None and task_update.rowcount != 1:
                raise _reconcile_reservation_lost()
            finalize_internal_billing(
                conn,
                task_id=task_id,
                outcome="cancelled" if status == "cancelled" else "failed",
            )
            _refresh_batch_status_in_transaction(conn, batch_id=batch_id)
            result = get_task_result(conn, task_id)
            if reconcile_reservation is not None:
                _complete_reconcile_operation_in_transaction(
                    conn,
                    reservation=reconcile_reservation,
                    task_id=task_id,
                    batch_id=batch_id,
                    project_id=project_id,
                    result=result,
                )
        return result
    raise generation_error(
        409, "PROVIDER_STILL_PROCESSING", "Provider reports the task is still running."
    )


def _release_archive_retry(conn: sqlite3.Connection, *, task_id: str, batch_id: str) -> None:
    """Release the lease and back off ~60s so a stuck provider/storage does not
    cause a hot retry loop. Once retries are exhausted, the task is failed so a
    permanently expired provider URL does not spin forever."""
    with conn:
        row = conn.execute(
            "SELECT archive_retry_count FROM generation_tasks WHERE id = ?", (task_id,)
        ).fetchone()
        next_count = int(row["archive_retry_count"]) + 1 if row is not None else 1
        if next_count >= MAX_ARCHIVE_RETRIES:
            conn.execute(
                """
                UPDATE generation_tasks
                SET
                    status = 'FAILED',
                    archive_status = 'ARCHIVE_FAILED',
                    provider_result_url = NULL,
                    archive_retry_count = ?,
                    error_code = 'ARCHIVE_RETRY_EXHAUSTED',
                    error_message_redacted =
                        'Archive retries exhausted; the provider URL may have expired.',
                    locked_by = NULL,
                    locked_until = NULL,
                    next_poll_at = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (next_count, task_id),
            )
            finalize_internal_billing(conn, task_id=task_id, outcome="failed")
            _refresh_batch_status_in_transaction(conn, batch_id=batch_id)
            return
        conn.execute(
            """
            UPDATE generation_tasks
            SET
                status = 'SUCCEEDED',
                archive_status = 'ARCHIVE_FAILED',
                archive_retry_count = ?,
                locked_by = NULL,
                locked_until = NULL,
                next_poll_at = datetime('now', '+60 seconds'),
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (next_count, task_id),
        )


def acquire_generation_task_lease(
    conn: sqlite3.Connection,
    *,
    worker_id: str,
) -> dict[str, Any] | None:
    mark_expired_active_leases_needing_attention(conn)
    locked_until = (datetime.now(UTC) + timedelta(seconds=GENERATION_LEASE_SECONDS)).isoformat()
    try:
        conn.execute("BEGIN IMMEDIATE")
        runtime = read_runtime_limits(conn)
        active_count = conn.execute(
            """
            SELECT COUNT(*)
            FROM generation_tasks
            WHERE status IN ('SUBMITTING', 'QUEUED', 'RUNNING', 'ARCHIVING')
            """
        ).fetchone()[0]
        if int(active_count) >= runtime["max_concurrent_h3_tasks"]:
            conn.commit()
            return None

        row = conn.execute(
            """
            UPDATE generation_tasks
            SET
                attempt = attempt + CASE WHEN status IN ('PENDING', 'QUEUED') THEN 1 ELSE 0 END,
                status = 'SUBMITTING',
                locked_by = ?,
                locked_until = ?,
                submitted_at = CASE
                    WHEN status IN ('PENDING', 'QUEUED')
                    THEN COALESCE(submitted_at, CURRENT_TIMESTAMP)
                    ELSE submitted_at
                END,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = (
                SELECT id
                FROM generation_tasks
                WHERE
                    (
                        status IN ('PENDING', 'QUEUED')
                        OR (
                            status = 'SUCCEEDED'
                            AND archive_status = 'ARCHIVE_FAILED'
                            AND provider_result_url IS NOT NULL
                            AND provider_result_url != ''
                        )
                    )
                    AND (
                        locked_until IS NULL
                        OR datetime(locked_until) <= CURRENT_TIMESTAMP
                    )
                    AND (next_poll_at IS NULL OR next_poll_at <= CURRENT_TIMESTAMP)
                ORDER BY created_at, id
                LIMIT 1
            )
            RETURNING id
            """,
            (worker_id, locked_until),
        ).fetchone()
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    if row is None:
        return None
    return load_worker_task(conn, str(row["id"]))


def load_worker_task(conn: sqlite3.Connection, task_id: str) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT
            generation_tasks.id,
            generation_tasks.batch_id,
            generation_tasks.provider,
            generation_tasks.archive_status,
            generation_tasks.provider_result_url,
            generation_batches.project_id,
            generation_batches.created_by_user_id,
            generation_batches.request_snapshot_json,
            generation_tasks.prompt_snapshot_json
        FROM generation_tasks
        JOIN generation_batches ON generation_batches.id = generation_tasks.batch_id
        WHERE generation_tasks.id = ?
        """,
        (task_id,),
    ).fetchone()
    if row is None:
        raise LookupError("leased task disappeared")

    prompt_snapshot = json.loads(str(row["prompt_snapshot_json"]))
    request_snapshot = json.loads(str(row["request_snapshot_json"]))
    payload = dict(row)
    payload["prompt_text"] = prompt_snapshot["prompt_text"]
    payload["first_frame_uri"] = prompt_snapshot["first_frame_uri"]
    payload["output_duration_seconds"] = request_snapshot["output_duration_seconds"]
    payload["resolution"] = request_snapshot["resolution"]
    return payload


def mark_task_submission_uncertain(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    message: str,
    provider_task_id: str | None = None,
) -> None:
    with conn:
        row = conn.execute(
            "SELECT batch_id FROM generation_tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        conn.execute(
            """
            UPDATE generation_tasks
            SET
                provider_task_id = COALESCE(?, provider_task_id),
                status = 'SUBMISSION_UNCERTAIN',
                error_code = 'SUBMISSION_UNCERTAIN',
                error_message_redacted = ?,
                locked_by = NULL,
                locked_until = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (provider_task_id, message, task_id),
        )
        if row is not None:
            _refresh_batch_status_in_transaction(conn, batch_id=str(row["batch_id"]))


def mark_task_provider_settings_unavailable(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    batch_id: str,
) -> None:
    with conn:
        conn.execute(
            """
            UPDATE generation_tasks
            SET
                status = 'FAILED',
                error_code = 'METASO_SETTINGS_UNAVAILABLE',
                error_message_redacted = 'METASO settings are unavailable to the worker.',
                submitted_at = NULL,
                locked_by = NULL,
                locked_until = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (task_id,),
        )
        finalize_internal_billing(conn, task_id=task_id, outcome="failed")
        _refresh_batch_status_in_transaction(conn, batch_id=batch_id)


def mark_task_provider_failed(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    batch_id: str,
    provider_task_id: str | None,
) -> None:
    with conn:
        conn.execute(
            """
            UPDATE generation_tasks
            SET
                provider_task_id = ?,
                status = 'FAILED',
                error_code = 'H3_PROVIDER_FAILED',
                error_message_redacted = 'METASO H3 task failed or returned an invalid result.',
                locked_by = NULL,
                locked_until = NULL,
                completed_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (provider_task_id, task_id),
        )
        finalize_internal_billing(conn, task_id=task_id, outcome="failed")
        _refresh_batch_status_in_transaction(conn, batch_id=batch_id)


def mark_task_first_frame_url_sign_failed(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    batch_id: str,
) -> None:
    """A provider call never started, so this is safe to mark as a normal failure."""
    with conn:
        conn.execute(
            """
            UPDATE generation_tasks
            SET
                status = 'FAILED',
                error_code = 'FIRST_FRAME_URL_SIGN_FAILED',
                error_message_redacted = 'First-frame download URL could not be signed.',
                submitted_at = NULL,
                locked_by = NULL,
                locked_until = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (task_id,),
        )
        finalize_internal_billing(conn, task_id=task_id, outcome="failed")
        _refresh_batch_status_in_transaction(conn, batch_id=batch_id)


def mark_expired_active_leases_needing_attention(conn: sqlite3.Connection) -> None:
    """Do not resubmit work when a worker died after a provider call may have started."""
    with conn:
        rows = conn.execute(
            """
            SELECT id, batch_id
            FROM generation_tasks
            WHERE status IN ('SUBMITTING', 'RUNNING', 'ARCHIVING')
              AND locked_until IS NOT NULL
              AND datetime(locked_until) <= CURRENT_TIMESTAMP
            """
        ).fetchall()
        if not rows:
            return
        # Archive retries never start a paid call; an expired lease is safe to
        # reset so another worker can re-download and re-archive the result.
        conn.execute(
            """
            UPDATE generation_tasks
            SET
                status = 'SUCCEEDED',
                archive_status = 'ARCHIVE_FAILED',
                locked_by = NULL,
                locked_until = NULL,
                next_poll_at = datetime('now', '+60 seconds'),
                updated_at = CURRENT_TIMESTAMP
            WHERE status IN ('SUBMITTING', 'RUNNING', 'ARCHIVING')
              AND archive_status = 'ARCHIVE_FAILED'
              AND provider_result_url IS NOT NULL
              AND locked_until IS NOT NULL
              AND datetime(locked_until) <= CURRENT_TIMESTAMP
            """
        )
        conn.execute(
            """
            UPDATE generation_tasks
            SET
                status = 'SUBMISSION_UNCERTAIN',
                error_code = 'LEASE_EXPIRED_NEEDS_ATTENTION',
                error_message_redacted = 'Worker lease expired during active provider operation.',
                locked_by = NULL,
                locked_until = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE status IN ('SUBMITTING', 'RUNNING', 'ARCHIVING')
              AND NOT (
                  archive_status = 'ARCHIVE_FAILED' AND provider_result_url IS NOT NULL
              )
              AND locked_until IS NOT NULL
              AND datetime(locked_until) <= CURRENT_TIMESTAMP
            """
        )
    for row in rows:
        refresh_batch_status(conn, batch_id=str(row["batch_id"]))


def refresh_batch_status(conn: sqlite3.Connection, *, batch_id: str) -> None:
    with conn:
        _refresh_batch_status_in_transaction(conn, batch_id=batch_id)


def _refresh_batch_status_in_transaction(
    conn: sqlite3.Connection,
    *,
    batch_id: str,
) -> None:
    rows = conn.execute(
        """
        SELECT
            id,
            status,
            archive_status,
            quality_status,
            quality_issue_codes,
            result_asset_id,
            provider,
            model,
            provider_task_id,
            provider_result_url,
            attempt,
            archive_retry_count,
            estimated_cost,
            actual_cost,
            error_code,
            error_message_redacted,
            submitted_at,
            started_at,
            completed_at,
            retry_of_task_id,
            superseded_by_task_id,
            superseded_at,
            retry_reason,
            retry_requested_at,
            prompt_snapshot_json
        FROM generation_tasks
        WHERE batch_id = ?
        """,
        (batch_id,),
    ).fetchall()
    progress = calculate_progress([task_result(row) for row in rows])
    conn.execute(
        """
        UPDATE generation_batches
        SET status = ?, updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (batch_status("QUEUED", progress), batch_id),
    )


def get_task_result(conn: sqlite3.Connection, task_id: str) -> TaskResult:
    row = conn.execute(
        """
        SELECT
            id,
            status,
            archive_status,
            quality_status,
            quality_issue_codes,
            result_asset_id,
            provider,
            model,
            provider_task_id,
            provider_result_url,
            attempt,
            archive_retry_count,
            estimated_cost,
            actual_cost,
            error_code,
            error_message_redacted,
            submitted_at,
            started_at,
            completed_at,
            retry_of_task_id,
            superseded_by_task_id,
            superseded_at,
            retry_reason,
            retry_requested_at,
            prompt_snapshot_json
        FROM generation_tasks
        WHERE id = ?
        """,
        (task_id,),
    ).fetchone()
    if row is None:
        raise LookupError("task not found")
    return task_result(row)


def list_generation_batches(
    conn: sqlite3.Connection,
    *,
    actor: CurrentUser,
    project_id: str | None = None,
    created_by_user_id: str | None = None,
    status: BatchStatusFilter | None = None,
    needs_attention: bool | None = None,
    limit: int = 20,
    cursor: str | None = None,
) -> GenerationBatchListPage:
    if limit < 1 or limit > 100:
        raise generation_error(422, "LIST_LIMIT_INVALID", "limit must be between 1 and 100.")
    if project_id is not None:
        require_project_access(
            conn,
            actor=actor,
            project_id=project_id,
            action="generation_batch.list",
        )

    filters_hash = batch_list_filters_hash(
        actor=actor,
        project_id=project_id,
        created_by_user_id=created_by_user_id,
        status=status,
        needs_attention=needs_attention,
    )
    cursor_position = (
        None if cursor is None else decode_batch_list_cursor(cursor, filters_hash=filters_hash)
    )

    clauses = ["1 = 1"]
    parameters: list[object] = []
    if project_id is not None:
        clauses.append("batch.project_id = ?")
        parameters.append(project_id)
    elif actor.role == "employee":
        clauses.append("project.owner_user_id = ?")
        parameters.append(actor.id)
    if created_by_user_id is not None:
        clauses.append("batch.created_by_user_id = ?")
        parameters.append(created_by_user_id)
    if status is not None:
        clauses.append("batch.status = ?")
        parameters.append(status)
    if needs_attention is not None:
        exists_prefix = "" if needs_attention else "NOT "
        clauses.append(
            f"""
            {exists_prefix}EXISTS (
                SELECT 1
                FROM generation_tasks AS attention_task
                WHERE attention_task.batch_id = batch.id
                  AND attention_task.superseded_by_task_id IS NULL
                  AND (
                    attention_task.status = 'SUBMISSION_UNCERTAIN'
                    OR attention_task.archive_status = 'ARCHIVE_FAILED'
                    OR attention_task.quality_status = 'AUDIO_QUALITY_FAILED'
                    OR instr(
                        COALESCE(attention_task.quality_issue_codes, ''),
                        '"AUDIO_QUALITY_FAILED"'
                    ) > 0
                  )
            )
            """
        )
    if cursor_position is not None:
        cursor_created_at, cursor_batch_id = cursor_position
        clauses.append("(batch.created_at < ? OR (batch.created_at = ? AND batch.id < ?))")
        parameters.extend([cursor_created_at, cursor_created_at, cursor_batch_id])
    parameters.append(limit + 1)

    rows = conn.execute(
        f"""
        SELECT
            batch.id,
            batch.project_id,
            project.name AS project_name,
            batch.created_by_user_id,
            creator.display_name AS created_by_display_name,
            batch.request_snapshot_json,
            batch.status,
            batch.created_at,
            batch.updated_at,
            batch.display_name,
            batch.source_batch_id,
            batch.source_task_id,
            batch.generation_reason
        FROM generation_batches AS batch
        JOIN projects AS project ON project.id = batch.project_id
        JOIN users AS creator ON creator.id = batch.created_by_user_id
        WHERE {" AND ".join(clauses)}
        ORDER BY batch.created_at DESC, batch.id DESC
        LIMIT ?
        """,
        tuple(parameters),
    ).fetchall()
    page_rows = rows[:limit]
    if not page_rows:
        return GenerationBatchListPage(items=[], next_cursor=None)

    batch_ids = [str(row["id"]) for row in page_rows]
    placeholders = ", ".join("?" for _ in batch_ids)
    task_rows = conn.execute(
        f"""
        SELECT
            task.batch_id,
            task.id,
            task.status,
            task.archive_status,
            task.quality_status,
            task.quality_issue_codes,
            task.result_asset_id,
            task.provider,
            task.model,
            task.provider_task_id,
            task.provider_result_url,
            task.attempt,
            task.archive_retry_count,
            task.estimated_cost,
            task.actual_cost,
            task.error_code,
            task.error_message_redacted,
            task.submitted_at,
            task.started_at,
            task.completed_at,
            task.retry_of_task_id,
            task.superseded_by_task_id,
            task.superseded_at,
            task.retry_reason,
            task.retry_requested_at
        FROM generation_tasks AS task
        WHERE task.batch_id IN ({placeholders})
        ORDER BY task.created_at, task.id
        """,
        tuple(batch_ids),
    ).fetchall()
    tasks_by_batch: dict[str, list[TaskSummary]] = {batch_id: [] for batch_id in batch_ids}
    for row in task_rows:
        tasks_by_batch[str(row["batch_id"])].append(task_summary(row))

    items: list[GenerationBatchListItem] = []
    for row in page_rows:
        batch_id = str(row["id"])
        tasks = tasks_by_batch[batch_id]
        progress = calculate_progress(tasks)
        items.append(
            GenerationBatchListItem(
                id=batch_id,
                project_id=str(row["project_id"]),
                project_name=str(row["project_name"]),
                created_by_user_id=str(row["created_by_user_id"]),
                created_by_display_name=str(row["created_by_display_name"]),
                prompt_version_id=request_prompt_version_id(row["request_snapshot_json"]),
                status=batch_status(str(row["status"]), progress),
                quantity=len(tasks),
                created_at=str(row["created_at"]),
                updated_at=str(row["updated_at"]),
                display_name=optional_text(row["display_name"]),
                source_batch_id=optional_text(row["source_batch_id"]),
                source_task_id=optional_text(row["source_task_id"]),
                generation_reason=optional_text(row["generation_reason"]),
                progress=progress,
                total_estimated_cost=optional_cost_total([task.estimated_cost for task in tasks]),
                total_actual_cost=optional_cost_total([task.actual_cost for task in tasks]),
                needs_attention_count=progress.counts["needs_attention"],
                has_results=any(task.result_asset_id is not None for task in tasks),
                tasks=tasks,
            )
        )

    next_cursor = None
    if len(rows) > limit:
        last_row = page_rows[-1]
        next_cursor = encode_batch_list_cursor(
            created_at=str(last_row["created_at"]),
            batch_id=str(last_row["id"]),
            filters_hash=filters_hash,
        )
    return GenerationBatchListPage(items=items, next_cursor=next_cursor)


def batch_list_filters_hash(
    *,
    actor: CurrentUser,
    project_id: str | None,
    created_by_user_id: str | None,
    status: BatchStatusFilter | None,
    needs_attention: bool | None,
) -> str:
    payload = {
        "actor_id": actor.id,
        "actor_role": actor.role,
        "project_id": project_id,
        "created_by_user_id": created_by_user_id,
        "status": status,
        "needs_attention": needs_attention,
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def encode_batch_list_cursor(*, created_at: str, batch_id: str, filters_hash: str) -> str:
    payload = json.dumps(
        {
            "v": 1,
            "created_at": created_at,
            "id": batch_id,
            "filters_hash": filters_hash,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def decode_batch_list_cursor(value: str, *, filters_hash: str) -> tuple[str, str]:
    try:
        padding = "=" * (-len(value) % 4)
        decoded = base64.b64decode(value + padding, altchars=b"-_", validate=True)
        payload = json.loads(decoded.decode())
    except (ValueError, binascii.Error, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise generation_error(400, "INVALID_CURSOR", "The batch cursor is invalid.") from exc
    if not isinstance(payload, dict):
        raise generation_error(400, "INVALID_CURSOR", "The batch cursor is invalid.")
    created_at = payload.get("created_at")
    batch_id = payload.get("id")
    encoded_filters_hash = payload.get("filters_hash")
    if (
        payload.get("v") != 1
        or not isinstance(created_at, str)
        or not created_at
        or not isinstance(batch_id, str)
        or not batch_id
        or not isinstance(encoded_filters_hash, str)
    ):
        raise generation_error(400, "INVALID_CURSOR", "The batch cursor is invalid.")
    if encoded_filters_hash != filters_hash:
        raise generation_error(
            400,
            "CURSOR_FILTER_MISMATCH",
            "The batch cursor does not match the current filters.",
        )
    return created_at, batch_id


def get_generation_batch(
    conn: sqlite3.Connection,
    *,
    batch_id: str,
    actor: CurrentUser,
) -> BatchResult:
    batch = conn.execute(
        """
        SELECT id, project_id, request_snapshot_json, status, display_name,
               source_batch_id, source_task_id, generation_reason
        FROM generation_batches
        WHERE id = ?
        """,
        (batch_id,),
    ).fetchone()
    if batch is None:
        raise generation_error(404, "BATCH_NOT_FOUND", "Generation batch does not exist.")
    require_project_access(
        conn,
        actor=actor,
        project_id=str(batch["project_id"]),
        action="generation_batch.read",
    )
    rows = conn.execute(
        """
        SELECT
            id,
            status,
            archive_status,
            quality_status,
            quality_issue_codes,
            result_asset_id,
            provider,
            model,
            provider_task_id,
            provider_result_url,
            attempt,
            archive_retry_count,
            estimated_cost,
            actual_cost,
            error_code,
            error_message_redacted,
            submitted_at,
            started_at,
            completed_at,
            retry_of_task_id,
            superseded_by_task_id,
            superseded_at,
            retry_reason,
            retry_requested_at,
            prompt_snapshot_json
        FROM generation_tasks
        WHERE batch_id = ?
        ORDER BY created_at, id
        """,
        (batch_id,),
    ).fetchall()
    tasks = [task_result(row) for row in rows]
    progress = calculate_progress(tasks)
    status = batch_status(str(batch["status"]), progress)
    try:
        request_snapshot = json.loads(str(batch["request_snapshot_json"]))
    except json.JSONDecodeError:
        request_snapshot = {}
    prompt_version_id = request_snapshot.get("prompt_version_id")
    stale = True
    if isinstance(prompt_version_id, str):
        try:
            prompt = require_version(
                conn,
                version_id=prompt_version_id,
                project_id=str(batch["project_id"]),
                kind=H3_PROMPT_KIND,
            )
        except HTTPException:
            stale = True
        else:
            stale = bool(version_stale_reasons(conn, row=prompt))
    else:
        prompt_version_id = "unknown"
    return BatchResult(
        id=str(batch["id"]),
        project_id=str(batch["project_id"]),
        prompt_version_id=prompt_version_id,
        status=status,
        quantity=len(tasks),
        stale=stale,
        display_name=optional_text(batch["display_name"]),
        source_batch_id=optional_text(batch["source_batch_id"]),
        source_task_id=optional_text(batch["source_task_id"]),
        generation_reason=optional_text(batch["generation_reason"]),
        progress=progress,
        tasks=tasks,
    )


def rename_generation_batch(
    conn: sqlite3.Connection,
    *,
    actor: CurrentUser,
    batch_id: str,
    display_name: str,
) -> BatchResult:
    """Rename a generation batch (creator or admin only).

    只有创建者或管理员可以改批次显示名；重命名不改变批次的任何生成
    状态与任务数据，审计日志记录前后名称以便对账。
    """
    batch = conn.execute(
        """
        SELECT id, project_id, created_by_user_id, display_name
        FROM generation_batches
        WHERE id = ?
        """,
        (batch_id,),
    ).fetchone()
    if batch is None:
        raise generation_error(404, "BATCH_NOT_FOUND", "Generation batch does not exist.")
    if actor.role != "admin" and str(batch["created_by_user_id"]) != actor.id:
        raise generation_error(
            403,
            "GENERATION_BATCH_FORBIDDEN",
            "只有批次创建者或管理员可以修改批次名称。",
        )
    clean_name = display_name.strip()
    if not clean_name:
        raise generation_error(
            422,
            "GENERATION_BATCH_NAME_REQUIRED",
            "批次名称不能为空。",
        )
    with conn:
        conn.execute(
            """
            UPDATE generation_batches
            SET display_name = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (clean_name, batch_id),
        )
    write_audit(
        conn,
        actor=actor,
        action="generation_batch.rename",
        entity_type="generation_batch",
        entity_id=batch_id,
        metadata={
            "from_name": (None if batch["display_name"] is None else str(batch["display_name"])),
            "to_name": clean_name,
        },
    )
    return get_generation_batch(conn, batch_id=batch_id, actor=actor)


def build_h3_request(
    *,
    prompt_text: str,
    first_frame_url: str,
    duration_seconds: int,
    resolution: str,
) -> dict[str, Any]:
    if not prompt_text.strip():
        raise ValueError("prompt_text is required")
    if duration_seconds < 4 or duration_seconds > 15:
        raise ValueError("duration must be between 4 and 15 seconds")
    if resolution not in SUPPORTED_RESOLUTIONS:
        raise ValueError("resolution must be 768P or 2K")
    return {
        "model": H3_MODEL,
        "content": [
            {"type": "text", "text": prompt_text},
            {
                "type": "image_url",
                "image_url": {"url": first_frame_url},
                "role": "first_frame",
            },
        ],
        "resolution": resolution,
        "duration": duration_seconds,
        "ratio": "adaptive",
    }


def validate_h3_request(request: dict[str, Any]) -> None:
    content = request.get("content")
    if not isinstance(content, list) or len(content) != 2:
        raise ValueError("H3 I2V content must contain text and first_frame only")
    if content[0].get("type") != "text" or not str(content[0].get("text", "")).strip():
        raise ValueError("H3 I2V content requires non-empty text")
    if content[1].get("role") != "first_frame":
        raise ValueError("H3 I2V image must use first_frame role")
    if request.get("ratio") != "adaptive":
        raise ValueError("H3 I2V ratio must be adaptive")
    duration = request.get("duration")
    if not isinstance(duration, int) or duration < 4 or duration > 15:
        raise ValueError("H3 I2V duration must be 4-15 seconds")


def _metaso_json_object(content: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise H3ProviderFailed("METASO returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise H3ProviderFailed("METASO returned an invalid response object")
    return cast(dict[str, Any], payload)


def _metaso_task_id(content: bytes) -> str:
    task_id = _metaso_json_object(content).get("task_id")
    if not isinstance(task_id, str) or not task_id:
        raise H3ProviderFailed("METASO create response is missing task_id")
    return task_id


def _metaso_create_request(request: dict[str, Any]) -> dict[str, Any]:
    """METASO accepts the UI resolution labels as-is (768P / 2K)."""
    return dict(request)


def _require_public_https_host(hostname: str) -> None:
    """Reject result URLs that resolve to private/loopback/link-local addresses
    so a compromised METASO response cannot drive server-side SSRF."""
    provider_host = urlparse(METASO_BASE_URL).hostname or ""
    if hostname == provider_host or hostname.endswith(f".{provider_host}"):
        # Provider-owned hosts are trusted by contract; desktop machines often
        # route them through local proxies whose synthetic addresses would
        # otherwise fail the public-address check below.
        return
    try:
        addresses = socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise H3ProviderFailed("METASO result URL hostname could not be resolved") from exc
    if not addresses:
        raise H3ProviderFailed("METASO result URL hostname could not be resolved")
    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])
        if not ip.is_global:
            raise H3ProviderFailed(
                "METASO result URL must resolve to a public address", terminal=True
            )


def _metaso_content_url(item: dict[str, Any], *, provider_task_id: str) -> str:
    content = item.get("content")
    result_url = content.get("url") if isinstance(content, dict) else None
    parsed = urlparse(result_url) if isinstance(result_url, str) else None
    if (
        not isinstance(result_url, str)
        or parsed is None
        or parsed.scheme != "https"
        or not parsed.hostname
    ):
        raise H3ProviderFailed(
            "METASO completed task is missing an HTTPS result URL",
            provider_task_id=provider_task_id,
            terminal=True,
        )
    _require_public_https_host(parsed.hostname)
    return result_url


def _h3_request_has_https_first_frame(request: dict[str, Any]) -> bool:
    content = request.get("content")
    if not isinstance(content, list) or len(content) < 2:
        return False
    image = content[1]
    image_url = image.get("image_url") if isinstance(image, dict) else None
    value = image_url.get("url") if isinstance(image_url, dict) else None
    parsed = urlparse(value) if isinstance(value, str) else None
    return bool(parsed and parsed.scheme == "https" and parsed.hostname)


def h3_audio_quality(
    content: bytes,
) -> tuple[Literal["AUDIO_OK", "AUDIO_QUALITY_FAILED"], list[str]]:
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        return "AUDIO_QUALITY_FAILED", ["AUDIO_VALIDATION_UNAVAILABLE"]
    with tempfile.NamedTemporaryFile(suffix=".mp4") as temporary_video:
        temporary_video.write(content)
        temporary_video.flush()
        try:
            result = subprocess.run(
                [
                    ffprobe,
                    "-v",
                    "error",
                    "-show_entries",
                    "stream=codec_type",
                    "-of",
                    "json",
                    temporary_video.name,
                ],
                capture_output=True,
                check=False,
                text=True,
                timeout=5,
            )
        except (subprocess.TimeoutExpired, OSError):
            return "AUDIO_QUALITY_FAILED", ["AUDIO_VALIDATION_UNAVAILABLE"]
    if result.returncode != 0:
        return "AUDIO_QUALITY_FAILED", ["AUDIO_VALIDATION_UNAVAILABLE"]
    try:
        streams = json.loads(result.stdout).get("streams", [])
    except (AttributeError, json.JSONDecodeError):
        return "AUDIO_QUALITY_FAILED", ["AUDIO_VALIDATION_UNAVAILABLE"]
    has_audio = any(
        isinstance(stream, dict) and stream.get("codec_type") == "audio" for stream in streams
    )
    return ("AUDIO_OK", []) if has_audio else ("AUDIO_QUALITY_FAILED", ["AUDIO_QUALITY_FAILED"])


def require_version(
    conn: sqlite3.Connection,
    *,
    version_id: str,
    project_id: str,
    kind: str,
) -> sqlite3.Row:
    try:
        row = get_version(conn, version_id)
    except LookupError as exc:
        raise generation_error(404, "VERSION_NOT_FOUND", "Version does not exist.") from exc
    if str(row["project_id"]) != project_id or str(row["kind"]) != kind:
        raise generation_error(404, "VERSION_NOT_FOUND", "Version does not exist.")
    return row


def estimate_spoken_duration(text: str) -> float:
    if not text:
        return 0.0
    return round(len(text) / 5.0, 2)


def map_script_to_shots(text: str, shots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    sentences = [part for part in re.split(r"(?<=[。！？!?])", text) if part]
    if not sentences and text:
        sentences = [text]
    mappings: list[dict[str, Any]] = []
    for index, shot in enumerate(shots):
        segment_text = sentences[index] if index < len(sentences) else ""
        if index == len(shots) - 1 and len(sentences) > len(shots):
            segment_text = "".join(sentences[index:])
        mappings.append(
            {
                "shot_id": str(shot["shot_id"]),
                "start_time": float(shot["start_time"]),
                "end_time": float(shot["end_time"]),
                "text": segment_text,
                "estimated_duration_seconds": estimate_spoken_duration(segment_text),
            }
        )
    return mappings


def render_shot_motion_clause(shot: Mapping[str, Any]) -> str:
    """把镜头的 motion 枚举确定性渲染成中文运动指令。

    旧版拆解结果没有 motion 时回退到 action 文本（行为不劣化）；
    motion 存在时由代码而不是模型措辞决定运动指令的强度。
    """
    motion = shot.get("motion")
    if not isinstance(motion, Mapping):
        return str(shot.get("action", ""))

    state = str(motion.get("subject_motion_state", ""))
    state_clause = SUBJECT_MOTION_STATE_CLAUSES.get(state, "")
    direction = SUBJECT_DIRECTION_LABELS.get(str(motion.get("subject_direction", "")), "")
    displacement = str(motion.get("subject_displacement") or "")
    hand_action = str(motion.get("hand_action") or "")
    relative_motion = str(motion.get("relative_motion") or "")

    parts: list[str] = []
    action = str(shot.get("action") or "")
    if action:
        parts.append(action)
    if state_clause:
        if direction and direction not in ("无位移方向",):
            parts.append(f"{state_clause}（{direction}）")
        else:
            parts.append(state_clause)
    if displacement and displacement not in ("无位移", "无人物出镜"):
        parts.append(f"位移：{displacement}")
    if hand_action and hand_action not in ("无人物出镜",):
        parts.append(f"手部：{hand_action}")
    if relative_motion:
        parts.append(f"相对运动：{relative_motion}")
    return "；".join(parts) if parts else str(shot.get("action", ""))


def shot_camera_motion_text(shot: Mapping[str, Any]) -> str:
    """运镜描述：motion 枚举标签优先，旧数据用自由文本。"""
    motion = shot.get("motion")
    if isinstance(motion, Mapping):
        label = MOTION_CAMERA_MOTION_LABELS.get(str(motion.get("camera_motion", "")))
        if label:
            return label
    return str(shot.get("camera_motion", ""))


def compile_prompt_text(
    *,
    script_payload: dict[str, Any],
    shot_payload: dict[str, Any],
    source_duration_seconds: float,
    duration_seconds: int,
    resolution: str,
) -> str:
    lines = [
        H3_PROMPT_TEMPLATES["intro"].format(
            duration_seconds=duration_seconds,
            resolution=resolution,
        ),
        H3_PROMPT_TEMPLATES["continuity"],
    ]
    mappings_by_shot = {
        str(mapping["shot_id"]): str(mapping["text"])
        for mapping in script_payload.get("shot_mappings", [])
        if isinstance(mapping, dict)
    }
    timeline_scale = duration_seconds / source_duration_seconds
    for shot in shot_payload["shots"]:
        shot_id = str(shot["shot_id"])
        spoken = mappings_by_shot.get(shot_id, str(shot.get("spoken_text", "")))
        start = max(0.0, min(float(duration_seconds), float(shot["start_time"]) * timeline_scale))
        end = max(start, min(float(duration_seconds), float(shot["end_time"]) * timeline_scale))
        lines.append(
            H3_PROMPT_TEMPLATES["shot"].format(
                start=start,
                end=end,
                shot_type=shot["shot_type"],
                composition=shot["composition"],
                camera_motion=shot_camera_motion_text(shot),
                subject=shot["subject"],
                motion_clause=render_shot_motion_clause(shot),
                scene=shot["scene"],
                transition=shot["transition"],
                spoken=spoken,
            )
        )
    lines.append(H3_PROMPT_TEMPLATES["script"].format(full_text=script_payload["full_text"]))
    lines.append(H3_PROMPT_TEMPLATES["outro"])
    lines.append(H3_PROMPT_TEMPLATES["narration_sync"])
    return "\n".join(lines)


def shot_timeline_duration(shot_payload: dict[str, Any]) -> float:
    source_duration = shot_payload.get("duration_seconds")
    if isinstance(source_duration, (int, float)) and not isinstance(source_duration, bool):
        duration = float(source_duration)
        if duration > 0:
            return duration
    shots = shot_payload.get("shots")
    if isinstance(shots, list):
        end_times = [
            float(shot["end_time"])
            for shot in shots
            if isinstance(shot, dict) and isinstance(shot.get("end_time"), (int, float))
        ]
        if end_times and max(end_times) > 0:
            return max(end_times)
    raise generation_error(
        409,
        "SHOT_CARD_TIMELINE_INVALID",
        "Shot-card timeline has no valid source duration.",
    )


def generation_request_snapshot(
    request: GenerationBatchRequest,
    prompt_snapshot: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": GENERATION_SCHEMA_VERSION,
        "generation_mode": "I2V",
        "quantity": request.quantity,
        "prompt_version_id": request.prompt_version_id,
        "prompt_content_hash": prompt_snapshot["content_hash"],
        "prompt_text": prompt_snapshot.get("prompt_text"),
        "template_version": prompt_snapshot.get("template_version"),
        "template_hash": prompt_snapshot.get("template_hash"),
        "source_analysis_version_id": prompt_snapshot.get("source_analysis_version_id"),
        "script_version_id": prompt_snapshot.get("script_version_id"),
        "shot_card_version_id": prompt_snapshot.get("shot_card_version_id"),
        "first_frame_candidates_version_id": prompt_snapshot.get(
            "first_frame_candidates_version_id"
        ),
        "first_frame_selection_version_id": prompt_snapshot.get("first_frame_selection_version_id"),
        "source_frame_selection_version_id": prompt_snapshot.get(
            "source_frame_selection_version_id"
        ),
        "main_character_version_id": prompt_snapshot.get("main_character_version_id"),
        "character_version_id": prompt_snapshot.get("character_version_id"),
        "character_reference_selection_id": prompt_snapshot.get("character_reference_selection_id"),
        "first_frame_asset_id": request.first_frame_asset_id,
        "output_duration_seconds": request.output_duration_seconds,
        "resolution": request.resolution,
        "provider": request.provider,
        "model": H3_MODEL,
        "fake_audio_quality": request.fake_audio_quality,
    }


def idempotency_request_hash(request: GenerationBatchRequest) -> str:
    payload = request.model_dump(mode="json", exclude={"idempotency_key"})
    return content_hash(json.dumps(payload, ensure_ascii=True, sort_keys=True))


def read_runtime_limits(conn: sqlite3.Connection) -> dict[str, int]:
    row = conn.execute(
        """
        SELECT max_generation_count_per_batch, max_concurrent_h3_tasks
        FROM runtime_settings
        WHERE id = 1
        """
    ).fetchone()
    if row is None:
        return {"max_generation_count_per_batch": 4, "max_concurrent_h3_tasks": 2}
    return {
        "max_generation_count_per_batch": int(row["max_generation_count_per_batch"]),
        "max_concurrent_h3_tasks": int(row["max_concurrent_h3_tasks"]),
    }


def generation_runtime_limits(conn: sqlite3.Connection) -> GenerationRuntimeLimits:
    runtime = read_runtime_limits(conn)
    return GenerationRuntimeLimits(
        max_quantity=runtime["max_generation_count_per_batch"],
    )


def calculate_progress(tasks: Sequence[TaskSummary]) -> BatchProgress:
    counts = {
        "pending": 0,
        "submitting": 0,
        "queued": 0,
        "running": 0,
        "archiving": 0,
        "succeeded": 0,
        "failed": 0,
        "cancelled": 0,
        "needs_attention": 0,
    }
    historical_counts = {
        "archive_failed": 0,
        "audio_quality_failed": 0,
        "failed": 0,
        "superseded": 0,
    }
    terminal_count = 0
    for task in tasks:
        status = task.status
        archive_status = task.archive_status
        needs_attention = False
        if status == "FAILED":
            historical_counts["failed"] += 1
        if archive_status == "ARCHIVE_FAILED":
            historical_counts["archive_failed"] += 1
        if (
            task.quality_status == "AUDIO_QUALITY_FAILED"
            or "AUDIO_QUALITY_FAILED" in task.quality_issue_codes
        ):
            historical_counts["audio_quality_failed"] += 1
        if task.superseded_by_task_id is not None:
            historical_counts["superseded"] += 1
        if status == "SUCCEEDED":
            if archive_status == "ARCHIVED":
                counts["succeeded"] += 1
                terminal_count += 1
            # SUCCEEDED + ARCHIVE_FAILED: the paid result is not deliverable yet,
            # so it is not counted as a success; it shows under needs_attention.
        elif status == "FAILED":
            counts["failed"] += 1
            terminal_count += 1
        elif status == "CANCELLED":
            counts["cancelled"] += 1
            terminal_count += 1
        elif status == "SUBMISSION_UNCERTAIN":
            # Paid-protection state: never counted as pending; needs attention.
            pass
        elif status == "SUBMITTING":
            counts["submitting"] += 1
        elif status == "QUEUED":
            counts["queued"] += 1
        elif status == "RUNNING":
            counts["running"] += 1
        elif status == "ARCHIVING":
            counts["archiving"] += 1
        else:
            counts["pending"] += 1
        if status == "SUBMISSION_UNCERTAIN" or archive_status == "ARCHIVE_FAILED":
            needs_attention = True
        if (
            task.quality_status == "AUDIO_QUALITY_FAILED"
            or "AUDIO_QUALITY_FAILED" in task.quality_issue_codes
        ):
            needs_attention = True
        if needs_attention and task.superseded_by_task_id is None:
            counts["needs_attention"] += 1

    total_count = len(tasks)
    progress_percent = 100 if total_count == 0 else floor(terminal_count / total_count * 100)
    return BatchProgress(
        total_count=total_count,
        terminal_count=terminal_count,
        progress_percent=progress_percent,
        counts=counts,
        historical_counts=historical_counts,
    )


def batch_status(stored_status: str, progress: BatchProgress) -> str:
    if progress.total_count == progress.terminal_count:
        # Even when every task is terminal, audio/archive quality failures must
        # surface as NEEDS_ATTENTION instead of being masked by SUCCEEDED.
        if progress.counts["needs_attention"]:
            return "NEEDS_ATTENTION"
        if (
            progress.counts["failed"]
            or progress.counts["cancelled"]
            or progress.historical_counts.get("archive_failed", 0)
            or progress.historical_counts.get("audio_quality_failed", 0)
        ):
            return "COMPLETED_WITH_FAILURES"
        return "SUCCEEDED"
    if progress.counts["needs_attention"]:
        return "NEEDS_ATTENTION"
    return stored_status


def task_result(row: sqlite3.Row) -> TaskResult:
    summary = task_summary(row)
    prompt_snapshot = (
        None
        if row["prompt_snapshot_json"] is None
        else json.loads(str(row["prompt_snapshot_json"]))
    )
    provider_result_url = optional_text(row["provider_result_url"])
    return TaskResult(
        **summary.model_dump(),
        prompt_snapshot=prompt_snapshot,
        provider_result_url=(
            provider_result_url
            if provider_result_url is not None and provider_result_url.startswith("https://")
            else None
        ),
    )


def task_summary(row: sqlite3.Row) -> TaskSummary:
    quality_issue_codes = parse_json_list(row["quality_issue_codes"])
    provider_task_id = optional_text(row["provider_task_id"])
    submitted_at = optional_text(row["submitted_at"])
    started_at = optional_text(row["started_at"])
    completed_at = optional_text(row["completed_at"])
    return TaskSummary(
        id=str(row["id"]),
        status=str(row["status"]),
        archive_status=str(row["archive_status"]),
        quality_status=str(row["quality_status"]),
        quality_issue_codes=quality_issue_codes,
        result_asset_id=optional_text(row["result_asset_id"]),
        stage=generation_task_stage(
            status=str(row["status"]),
            archive_status=str(row["archive_status"]),
            quality_status=str(row["quality_status"]),
            quality_issue_codes=quality_issue_codes,
        ),
        provider=str(row["provider"]),
        model=str(row["model"]),
        provider_task_id_tail=redacted_provider_task_tail(provider_task_id),
        attempt=int(row["attempt"]),
        archive_retry_count=int(row["archive_retry_count"]),
        estimated_cost=optional_float(row["estimated_cost"]),
        actual_cost=optional_float(row["actual_cost"]),
        error_code=optional_text(row["error_code"]),
        error_message_redacted=optional_text(row["error_message_redacted"]),
        submitted_at=submitted_at,
        started_at=started_at,
        completed_at=completed_at,
        duration_seconds=completed_duration_seconds(
            started_at=started_at or submitted_at,
            completed_at=completed_at,
        ),
        retry_of_task_id=optional_text(row["retry_of_task_id"]),
        superseded_by_task_id=optional_text(row["superseded_by_task_id"]),
        superseded_at=optional_text(row["superseded_at"]),
        retry_reason=optional_text(row["retry_reason"]),
        retry_requested_at=optional_text(row["retry_requested_at"]),
        available_actions=generation_task_available_actions(row),
    )


def generation_task_available_actions(
    row: sqlite3.Row,
) -> list[Literal["RETRY", "RECONCILE", "CONFIRM_NOT_CHARGED", "REGENERATE"]]:
    status = str(row["status"])
    archive_status = str(row["archive_status"])
    quality_status = str(row["quality_status"])
    provider_task_id = optional_text(row["provider_task_id"])
    provider_result_url = optional_text(row["provider_result_url"])
    error_code = optional_text(row["error_code"])

    if row["superseded_by_task_id"] is not None:
        return []

    if archive_status == "ARCHIVE_FAILED":
        if (
            provider_result_url is not None
            and int(row["archive_retry_count"]) < MAX_ARCHIVE_RETRIES
        ):
            return ["RETRY"]
        return []
    if status == "SUBMISSION_UNCERTAIN":
        return ["RECONCILE"] if provider_task_id is not None else ["CONFIRM_NOT_CHARGED"]
    if quality_status == "AUDIO_QUALITY_FAILED":
        return ["REGENERATE"]
    if status == "FAILED":
        if (
            provider_task_id is None
            and provider_result_url is None
            and row["submitted_at"] is None
            and error_code in SAFE_PRE_PROVIDER_FAILURE_CODES
        ):
            return ["RETRY"]
        if provider_task_id is not None or provider_result_url is not None or row["submitted_at"]:
            return ["REGENERATE"]
        return []
    return []


def generation_task_stage(
    *,
    status: str,
    archive_status: str,
    quality_status: str,
    quality_issue_codes: Sequence[str],
) -> str:
    if status == "SUBMISSION_UNCERTAIN":
        return "SUBMISSION_UNCERTAIN"
    if archive_status == "ARCHIVE_FAILED":
        return "ARCHIVE_FAILED"
    if quality_status == "AUDIO_QUALITY_FAILED" or "AUDIO_QUALITY_FAILED" in quality_issue_codes:
        return "QUALITY_FAILED"
    if status == "SUCCEEDED" and archive_status == "ARCHIVED":
        return "COMPLETED"
    return status


def redacted_provider_task_tail(value: str | None) -> str | None:
    if value is None or len(value) <= 8:
        return None
    return value[-8:]


def completed_duration_seconds(*, started_at: str | None, completed_at: str | None) -> float | None:
    if started_at is None or completed_at is None:
        return None
    try:
        started = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        completed = datetime.fromisoformat(completed_at.replace("Z", "+00:00"))
        return round(max(0.0, (completed - started).total_seconds()), 3)
    except (TypeError, ValueError):
        return None


def request_prompt_version_id(value: Any) -> str:
    try:
        payload = json.loads(str(value))
    except json.JSONDecodeError:
        return "unknown"
    if not isinstance(payload, dict):
        return "unknown"
    prompt_version_id = payload.get("prompt_version_id")
    return prompt_version_id if isinstance(prompt_version_id, str) else "unknown"


def optional_cost_total(values: Sequence[float | None]) -> float | None:
    present = [value for value in values if value is not None]
    return None if not present else round(sum(present), 6)


def optional_text(value: Any) -> str | None:
    return None if value is None else str(value)


def optional_float(value: Any) -> float | None:
    return None if value is None else float(value)


def parse_json_list(value: Any) -> list[str]:
    if value is None:
        return []
    parsed = json.loads(str(value))
    if not isinstance(parsed, list):
        return []
    return [str(item) for item in parsed]


def version_result(row: sqlite3.Row) -> VersionResult:
    return VersionResult(
        id=str(row["id"]),
        project_id=str(row["project_id"]),
        asset_id=None if row["asset_id"] is None else str(row["asset_id"]),
        kind=str(row["kind"]),
        version_number=int(row["version_number"]),
        payload=json.loads(str(row["payload_json"])),
        created_by_user_id=None
        if row["created_by_user_id"] is None
        else str(row["created_by_user_id"]),
        created_at=str(row["created_at"]),
    )


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def generation_error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})
