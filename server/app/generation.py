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
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from math import floor, isfinite
from pathlib import Path
from typing import Any, Literal, Protocol, cast
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlparse
from urllib.request import Request
from uuid import uuid4

import psycopg
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.analysis import get_version, insert_version, next_version_number
from app.auth import CurrentUser, Role
from app.db_pg import is_customer_production
from app.db_portable import (
    BusinessConnection,
    BusinessRow,
    IntegrityConstraintError,
    is_unique_conflict_on,
)
from app.external_calls import (
    endpoint_from_url,
    external_call_context,
    external_call_model,
    parse_provider_error,
    recorded_urlopen,
    redact_text,
    summarize_provider_message,
)
from app.first_frames import (
    FirstFrameQualityInspector,
    GeneratedVideoQualityResult,
    ImageInput,
    evaluate_generated_video_quality,
)
from app.h3_prompts import FORMATTER_VERSION, GenerationContext
from app.internal_billing import (
    BillingInvariantError,
    InsufficientCreditsError,
    finalize_internal_billing,
    reserve_internal_billing,
)
from app.operation_costs import (
    record_video_generation_cost,
    record_video_generation_not_called,
    snapshot_generation_rates,
)
from app.permissions import (
    insert_audit,
    remap_security_denial,
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
H3_PROMPT_TEMPLATE_VERSION = "h3.prompt.v7"
H3_PROMPT_TEMPLATE_SPEC = (
    (
        "intro",
        "生成一条 {duration_seconds} 秒、{resolution}、写实短视频，从提供的首帧自然开始；"
        "人物严格按各镜头的动作与运镜描述真实运动，不得僵立原地。",
    ),
    (
        "continuity",
        "严格延续已确认首帧中的完整人物身份、脸部、发型、肤色、身形比例、上下装、鞋履与手部，"
        "保持场景和光线连续；不得退化为局部换脸，不得恢复原视频人物的身体或服装，不得身份漂移。",
    ),
    # v7 新增：整体风格锚点（视觉风格/主色调/节奏/运镜语言基调）。内容由
    # build_style_clause() 按拆解结果动态拼接，任一字段缺失就跳过该子句，
    # 全部缺失（旧镜头卡）时 compile_prompt_text() 直接跳过整段，不留空行。
    ("style", "{style_clause}"),
    (
        "shot",
        "[{start}-{end}] {shot_type}，{composition}，{camera_motion}；"
        "主体：已确认首帧中的人物（身份与完整外观均以该图为准{wardrobe_clause}）；"
        "人物动作：{motion_clause}；场景：{scene}，转场：{transition}。"
        "口播意图：{spoken}",
    ),
    ("script", "口播意图：{full_text}"),
    # v7：outro 更名为 audio，audio_clause 由 build_audio_clause() 汇总
    # 各镜头的 ambient_sound/music_style_hint；旧镜头卡没有这些字段时
    # audio_clause 为空串，行为退化为原 outro 常量文案（不劣化）。
    (
        "audio",
        "{audio_clause}环境音与音乐保持自然；不要增加无关人物，不要身份突变、肢体异常或画面闪烁。",
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

# Final replica compilation is a separate contract from the retained preview API.
FINAL_REPLICA_TEMPLATE_VERSION = "h3.replica.final.v4"
FINAL_REPLICA_TEMPLATE_HASH = hashlib.sha256(
    b"h3.replica.final.v4:confirmed-script:first-frame-presenter-authority:"
    b"voice-gender-continuity:multi-person-role-separation:frame-scene-authority:"
    b"complete-narration:source-video-exclusion:real-cuts:explicit-duration:human-opening"
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
    "ORBIT": "镜头环绕主体",
    "CRANE_UP": "镜头升高",
    "CRANE_DOWN": "镜头降低",
    "ZOOM_IN": "镜头光学变焦推近",
    "ZOOM_OUT": "镜头光学变焦拉远",
}
PROMPT_STATUSES = {"SAVED", "LOCKED", "USED"}
TERMINAL_STATUSES = {"FAILED", "CANCELLED"}
H3_MODEL = "MiniMax-H3"
METASO_BASE_URL = "https://metaso.cn"
METASO_CREATE_PATH = "/api/minimax/v2/video_generation"
METASO_QUERY_PATH = "/api/minimax/v2/query/video_generation"
SUPPORTED_RESOLUTIONS = {"768P", "2K"}
SUPPORTED_RATIOS = {"adaptive", "21:9", "16:9", "4:3", "1:1", "3:4", "9:16"}
# 秘塔原生 H3 查询端点返回 6 种任务状态，其中尚未到达终态的 3 种（排队/生成中/处理中）
# 统一映射为 RUNNING 交给轮询继续等待；终态 succeeded/failed/cancelled 单独处理。
H3_PENDING_PROVIDER_STATUSES = {"queued", "running", "processing"}
# R2V 多模态参考：role 到 content 元素里媒体字段名的映射（与秘塔前端构造一致）。
REFERENCE_ROLE_MEDIA_KEYS = {
    "reference_image": "image_url",
    "reference_video": "video_url",
    "reference_audio": "audio_url",
}
# 客户可选时长 = provider 接受的范围（validate_h3_request 校验 4–30 的每一秒；
# 上游 16-30 秒走长视频规则、按基础费用 2 倍计费，2K 不支持超 15 秒）。
# 报价端点同样是 ge=4 le=30 的按秒线性计价。曾收窄成 {4, 15}，结果 12 秒的
# 源视频被拉成 15 秒或压成 4 秒，动作节奏与口播必然失真。
CUSTOMER_DURATION_OPTIONS = frozenset(range(4, 31))
CUSTOMER_QUANTITY_OPTIONS = {1, 2, 4}
MAX_GENERATION_PROMPT_CHARS = 7_000
# A real H3 request polls for up to five minutes. Leave headroom so another
# worker never mistakes an active request for an abandoned lease.
GENERATION_LEASE_SECONDS = 600
# A durable provider task is polled in small worker steps.  Each step only
# needs enough lease time for one bounded HTTP request; the task itself may
# remain RUNNING for much longer without holding a database connection.
GENERATION_STEP_LEASE_SECONDS = 120
GENERATION_POLL_INTERVAL_SECONDS = 5
# Production H3 normally finishes within ninety minutes.  Stop automatic
# polling after two hours so a vendor task that vanished from the query API
# cannot retain the user's only fair-queue slot forever.  The durable provider
# id remains available for reconciliation, so this never causes a paid retry.
GENERATION_MAX_POLL_AGE_SECONDS = 2 * 60 * 60
# Reconciliation performs one provider query plus optional download/archive;
# fifteen minutes exceeds those bounded calls while still recovering crashes.
RECONCILIATION_RESERVATION_SECONDS = 900
RECONCILE_OPERATION_LEASE_MINUTES = 15
FAKE_H3_OUTCOME_ENV = "VIDEO_REPLICA_FAKE_H3_OUTCOME"
FAKE_H3_RESULT_PATH_ENV = "VIDEO_REPLICA_FAKE_H3_RESULT_PATH"
ACCEPTANCE_GENERATION_USER_ID_ENV = "VIDEO_REPLICA_ACCEPTANCE_GENERATION_USER_ID"
ACCEPTANCE_PAID_GENERATION_LIMIT_ENV = "VIDEO_REPLICA_ACCEPTANCE_PAID_GENERATION_LIMIT"
FIRST_FRAME_URL_EXPIRES_IN = timedelta(minutes=15)
# Cap archive retries so a permanently expired provider URL does not keep the
# paid task spinning in ARCHIVE_FAILED forever.
MAX_ARCHIVE_RETRIES = 5
SAFE_PRE_PROVIDER_FAILURE_CODES = {
    "FIRST_FRAME_URL_SIGN_FAILED",
    "H3_SETTINGS_UNAVAILABLE",
    # 契约违规在发出 Provider 请求之前就失败，未触达供应商、未计费，
    # 与首帧签名失败同样允许原地重试。
    "PROVIDER_REQUEST_CONTRACT",
}

logger = logging.getLogger(__name__)


class ScriptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Literal["original", "custom", "no_narration"]
    text: str = Field(max_length=8000)
    shot_card_version_id: str = Field(min_length=1)


class PromptCompileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    script_version_id: str = Field(min_length=1)
    shot_card_version_id: str = Field(min_length=1)
    first_frame_asset_id: str = Field(min_length=1)
    # 编译/预览的时长上限与 CUSTOMER_DURATION_OPTIONS 同源（4–30）；复刻流调用方
    # 的源片 ≤15 秒，实际取值仍 ≤15，这里放宽只为独立创作长档可用。
    output_duration_seconds: int = Field(ge=4, le=30)
    resolution: Literal["768P", "2K"] = "768P"
    ratio: Literal["adaptive", "21:9", "16:9", "4:3", "1:1", "3:4", "9:16"] = "adaptive"
    timeline_policy: Literal["preserve", "scale_confirmed"] = "preserve"
    opening_action: str = Field(default="", max_length=1000)


class PromptPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    output_duration_seconds: int | None = Field(default=None, ge=4, le=30)
    resolution: Literal["768P", "2K"] = "768P"
    ratio: Literal["adaptive", "21:9", "16:9", "4:3", "1:1", "3:4", "9:16"] = "adaptive"


class PromptPreviewResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt_text: str
    output_duration_seconds: int
    resolution: Literal["768P", "2K"]
    ratio: Literal["adaptive", "21:9", "16:9", "4:3", "1:1", "3:4", "9:16"]
    script_source: Literal["script_version", "analysis_original"]
    shot_card_version_id: str | None


class PromptRevisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_prompt_version_id: str = Field(min_length=1)
    prompt_text: str = Field(min_length=1, max_length=7_000)


class SavedPromptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    prompt_text: str = Field(min_length=1, max_length=7_000)
    base_prompt_version_id: str | None = Field(default=None, min_length=1)
    generation_context: GenerationContext | None = None


class ApplySavedPromptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_prompt_version_id: str = Field(min_length=1)


class PromptContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Literal["analysis", "manual", "ai", "imported"] = "manual"
    analysis_version_id: str | None = None
    shot_card_version_id: str | None = None
    script_version_id: str | None = None
    optimization_task_id: str | None = None
    context_hash: str | None = None
    final_prompt_version_id: str | None = None


class GenerationBatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    quote_token: str | None = Field(default=None, min_length=1, max_length=8192)
    max_credits: int | None = Field(default=None, ge=0, le=2_147_483_647)

    quantity: int = Field(ge=1)
    prompt_version_id: str | None = Field(default=None, min_length=1)
    prompt_text: str | None = Field(default=None, min_length=1, max_length=7000)
    prompt_context: PromptContext | None = None
    first_frame_asset_id: str = Field(min_length=1)
    output_duration_seconds: int = Field(ge=4, le=15)
    resolution: Literal["768P", "2K"] = "768P"
    ratio: Literal["adaptive", "21:9", "16:9", "4:3", "1:1", "3:4", "9:16"] = "adaptive"
    idempotency_key: str = Field(min_length=1, max_length=128)
    provider: Literal["fake_h3", "metaso"] = "fake_h3"
    fake_audio_quality: Literal["ok", "missing"] = "ok"

    @model_validator(mode="after")
    def require_one_prompt(self) -> GenerationBatchRequest:
        if (self.prompt_version_id is None) == (self.prompt_text is None):
            raise ValueError("provide exactly one of prompt_text and prompt_version_id")
        if self.prompt_text is not None and not self.prompt_text.strip():
            raise ValueError("prompt_text cannot be blank")
        if self.prompt_version_id is not None and self.prompt_context is not None:
            raise ValueError("prompt_context requires prompt_text")
        return self


class PaidRegenerationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    quote_token: str | None = Field(default=None, min_length=1, max_length=8192)
    max_credits: int | None = Field(default=None, ge=0, le=2_147_483_647)
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
    audio_quality_status: Literal["AUDIO_OK", "AUDIO_QUALITY_FAILED", "NOT_REQUIRED"]
    quality_issue_codes: list[str]
    output_seconds: float | None = None


class H3QueryResult(BaseModel):
    """One non-blocking provider status observation.

    The PostgreSQL worker persists the provider task id immediately after
    submission and calls this method once per worker iteration.  Keeping the
    query result separate from ``H3CreateResult`` prevents a status check from
    accidentally downloading a large video while a database transaction is
    open.
    """

    model_config = ConfigDict(extra="forbid")

    status: Literal["RUNNING", "SUCCEEDED", "FAILED", "CANCELLED"]
    result_url: str | None = None
    output_seconds: float | None = None
    # 失败 / 取消时服务商给出的原因；此前只取状态字段，原因被丢弃（方案 P0-9）。
    failure_reason: str | None = None


class SubmissionUncertain(RuntimeError):
    pass


class GenerationTaskSupersededError(RuntimeError):
    """A worker's late terminal write hit a task that already has a paid
    replacement (the M4M5 review's L1 defense).

    Pathology: the provider call outlives its lease -> the sweeper parks the
    task in SUBMISSION_UNCERTAIN and frees the user's slot -> a paid
    replacement is created and ``superseded_by_task_id`` is set -> the
    original worker's late terminal write would otherwise flip the
    superseded task to SUCCEEDED/FAILED and settle billing a second time.
    The terminal write, its billing settlement and its slot release all
    belong to the replacement flow and are skipped; the worker loop treats
    this as an expected race outcome, not a fault.
    """


class H3ProviderFailed(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        provider_task_id: str | None = None,
        terminal: bool = False,
        provider_reason: str | None = None,
        http_status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.provider_task_id = provider_task_id
        self.terminal = terminal
        # 上游返回的 HTTP 状态码；网络层失败（超时、连接错误）没有状态码，保持 None。
        # 提交阶段据此区分「明确拒绝」与「可能已受理」，见 SUBMISSION_REJECTION_HTTP_STATUSES。
        self.http_status = http_status
        # 服务商给出的失败原话（已脱敏、截断）；落进任务的错误说明供管理端排查。
        self.provider_reason = provider_reason
        # 上游 HTTP 状态码：成片直链下载据此区分「链接已失效」与临时故障。
        self.http_status = http_status


class H3ProviderSettingsUnavailable(RuntimeError):
    pass


# 成片「直链交付」：供应商结果地址目前长期有效，但不能排除日后改为限时签名。
# 下载 / 探测时上游回这些状态码，即视为链接已失效——此时重试必然失败，必须如实告诉
# 用户「文件已失效」，而不是让他反复点「请重试」。
RESULT_URL_EXPIRED_STATUSES = frozenset({403, 404, 410})
RESULT_URL_EXPIRED_CODE = "RESULT_URL_EXPIRED"
RESULT_URL_EXPIRED_MESSAGE = (
    "成片文件已失效：生成服务提供的下载链接已过期，无法再预览、下载或保存。"
    "如需帮助，请联系客服并提供任务编号。"
)
RESULT_URL_PROBE_TIMEOUT_SECONDS = 10.0


def result_url_expired_error() -> HTTPException:
    return HTTPException(
        status_code=410,
        detail={"code": RESULT_URL_EXPIRED_CODE, "message": RESULT_URL_EXPIRED_MESSAGE},
    )


def provider_result_url_needs_refresh(url: str) -> bool:
    """成片文件仍保留时，刷新即将过期的访问签名，无需重新生成。"""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme != "https" or parsed.hostname != "files.metaso.cn":
        return False
    query = parse_qs(parsed.query)
    expires = query.get("expires", [])
    if len(expires) != 1 or not query.get("signature"):
        return False
    try:
        deadline = float(expires[0])
    except ValueError:
        return False
    if not isfinite(deadline):
        return False
    if deadline > 1_000_000_000_000:
        deadline /= 1000
    return deadline <= time.time() + 60


def provider_result_url_expired(url: str) -> bool | None:
    """探测供应商成片直链是否已失效：True 已失效，False 可用，None 无法判断。

    只发 HEAD、不下载正文；网络错误、超时等临时故障一律返回 None，不能误报失效。
    """
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        return None
    try:
        _require_public_https_host(parsed.hostname)
    except H3ProviderFailed:
        return None
    try:
        # 与其他供应商调用一样留调用记录，管理端可查到探测结果。
        recorded_urlopen(
            Request(url, method="HEAD"),
            timeout=RESULT_URL_PROBE_TIMEOUT_SECONDS,
            provider="metaso",
            endpoint=endpoint_from_url(url),
            read_limit=0,
        )
        return False
    except HTTPError as exc:
        if exc.code in RESULT_URL_EXPIRED_STATUSES:
            return True
        # 部分对象存储不支持 HEAD（405/501）：无法判断，交给真实下载决定。
        return None
    except (TimeoutError, URLError, OSError):
        return None


class ProviderRequestContractError(ValueError):
    """请求参数违反供应商契约（如 T2V 携带 adaptive ratio）。

    继承 ValueError 以兼容既有捕获方；worker 用它把契约违规与
    “首帧 URL 签名失败”分开归类，避免误导排障。新提交已在建批
    入口（independent.py 422）拦下，这里只兜底存量/异常路径。
    """


class GeneratedVideoValidationUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class ReconcileReservation:
    id: str
    actor: CurrentUser
    idempotency_key: str


@dataclass(frozen=True)
class ReconcileOperationLease:
    id: str
    task_id: str
    actor_user_id: str
    idempotency_key: str
    worker_id: str
    locked_until: str
    attempt: int


@dataclass(frozen=True)
class PreparedReconcileOperation:
    lease: ReconcileOperationLease
    actor: CurrentUser
    batch_id: str
    project_id: str
    created_by_user_id: str
    task_status: str
    provider_task_id: str | None
    provider: H3Provider | None
    first_frame_uri: str


@dataclass(frozen=True)
class ReconcileOperationOutcome:
    status: Literal["ALREADY_TERMINAL", "SUCCEEDED", "FAILED", "CANCELLED"]
    result_url: str | None = None
    audio_quality_status: str | None = None
    quality_issue_codes: list[str] | None = None
    output_seconds: float | None = None


@dataclass(frozen=True)
class GenerationSubmissionWork:
    provider: H3Provider
    provider_request: dict[str, Any]


class H3Provider:
    def submit_image_to_video(self, request: dict[str, Any]) -> str:
        raise H3ProviderFailed("submit_image_to_video is not implemented for this provider")

    def query_image_to_video(self, provider_task_id: str) -> H3QueryResult:
        raise H3ProviderFailed(
            "query_image_to_video is not implemented for this provider",
            provider_task_id=provider_task_id,
        )

    def create_image_to_video(self, request: dict[str, Any]) -> H3CreateResult:
        raise HTTPException(
            status_code=501,
            detail={
                "code": "H3_QUERY_SOURCE_PENDING",
                "message": (
                    "H3 create/query/archive is not implemented until the supported "
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


# 提交（create）阶段上游「明确拒绝」的 HTTP 状态码：请求没有被受理、没有创建任务、没有扣费，
# 属于确定性失败，应直接终态并释放暂扣，而不是 SUBMISSION_UNCERTAIN。
# 故意用白名单而非「所有 4xx」：408（超时）、429（限流）与 5xx 都可能发生在受理之后，
# 必须继续走未确认 + 对账。2026-10-13 H3 余额不足（HTTP 402）曾因被误判为未确认，
# 占满账号并发名额导致视频生成整体停摆。
SUBMISSION_REJECTION_HTTP_STATUSES = frozenset({400, 401, 402, 403, 404, 422})

# 明确被拒绝的提交写给客户的说明。不带服务商原话：原话会暴露供应商余额等内部状态，
# 原始响应已完整保留在 external_call_logs 供管理端排查。
SUBMISSION_REJECTED_CUSTOMER_MESSAGE = "视频生成服务暂时不可用，本次未扣费，请稍后重试。"


def is_submission_rejection(exc: H3ProviderFailed) -> bool:
    """提交阶段被上游明确拒绝：没有任务号 + 白名单状态码。

    轮询阶段的异常一定带 provider_task_id，所以不会被误判；worker 的各条提交分支都用这一个判定。
    """
    return exc.provider_task_id is None and exc.http_status in SUBMISSION_REJECTION_HTTP_STATUSES


def _provider_reason_from_body(detail: str) -> str | None:
    """从已脱敏的错误响应体取「原话（错误码）」；取不到返回 None，不编造原因。"""
    code, message = parse_provider_error(detail)
    if message and code:
        return f"{message}（{code}）"
    return message or code


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
            # 每次调用（提交、查询进度、下载）都留原始响应，失败原因可在管理端查到。
            response_body, _headers, _status = recorded_urlopen(
                request,
                timeout=self.timeout_seconds,
                provider="metaso",
                endpoint=endpoint_from_url(url),
                expected_json=True,
            )
            return response_body
        except HTTPError as exc:
            detail = ""
            try:
                detail = redact_text(exc.read().decode("utf-8", "replace"))[:1000]
            except OSError:
                pass
            logger.warning("H3 provider request failed with HTTP status %s: %s", exc.code, detail)
            raise H3ProviderFailed(
                f"H3 provider returned HTTP {exc.code}",
                provider_reason=_provider_reason_from_body(detail),
                http_status=exc.code,
            ) from exc
        except (TimeoutError, URLError, OSError) as exc:
            logger.warning("H3 provider request failed: %s", type(exc).__name__)
            raise H3ProviderFailed("H3 provider request failed") from exc


def _metaso_failure_reason(item: Mapping[str, Any]) -> str | None:
    """从查询结果里取失败原话；服务商没给就返回 None，不编造原因。"""
    code, message = parse_provider_error(json.dumps(item, ensure_ascii=False, default=str))
    if message and code:
        return f"{message}（{code}）"
    return message or code


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
        provider_task_id = self.submit_image_to_video(request)
        return self._poll_for_result(provider_task_id)

    def submit_image_to_video(self, request: dict[str, Any]) -> str:
        validate_h3_request(request)
        if not _h3_request_media_urls_are_https(request):
            raise H3ProviderFailed("H3 generation requires HTTPS media URLs")
        provider_request = _metaso_create_request(request)
        try:
            # 提交调用的日志带模型名（H3 只有这一个模型）。
            with external_call_model(H3_MODEL):
                response = self.transport.request(
                    "POST",
                    f"{METASO_BASE_URL}{METASO_CREATE_PATH}",
                    headers=self._api_headers(),
                    body=json.dumps(
                        provider_request, ensure_ascii=True, separators=(",", ":")
                    ).encode(),
                )
        except H3ProviderFailed as exc:
            if exc.http_status in SUBMISSION_REJECTION_HTTP_STATUSES:
                # 上游明确拒绝：没有受理、没有创建任务、没有扣费。原样抛出，交给 worker
                # 走终态失败并释放暂扣；绝不能当作「结果未知」占住账号并发名额。
                raise
            raise SubmissionUncertain("H3 provider submission result is unknown") from exc

        try:
            provider_task_id = _metaso_task_id(response)
        except H3ProviderFailed as exc:
            raise SubmissionUncertain("H3 provider submission result is unknown") from exc
        if self.task_created_observer is not None:
            # Persist the paid provider task id before polling so a timeout or
            # crash can never lose a result that reconciliation could recover.
            self.task_created_observer(provider_task_id)
        return provider_task_id

    def query_image_to_video(self, provider_task_id: str) -> H3QueryResult:
        try:
            item = self._query_task(provider_task_id)
        except H3ProviderFailed as exc:
            if exc.provider_task_id is not None:
                raise
            raise H3ProviderFailed(str(exc), provider_task_id=provider_task_id) from exc

        status = item.get("status")
        if status == "succeeded":
            return H3QueryResult(
                status="SUCCEEDED",
                result_url=_metaso_content_url(item, provider_task_id=provider_task_id),
                output_seconds=_metaso_output_seconds(item),
            )
        if status == "failed":
            return H3QueryResult(status="FAILED", failure_reason=_metaso_failure_reason(item))
        if status == "cancelled":
            return H3QueryResult(status="CANCELLED", failure_reason=_metaso_failure_reason(item))
        # queued / running / processing 以及任何未知状态都视为进行中：轮询继续等待，
        # 由轮询超时与对账机制兜底，绝不把仍在排队的任务误判为终态。
        if status not in H3_PENDING_PROVIDER_STATUSES:
            logger.warning(
                "H3 query returned unrecognized status %r for task %s; treating as RUNNING",
                status,
                provider_task_id,
            )
        return H3QueryResult(status="RUNNING")

    def _poll_for_result(self, provider_task_id: str) -> H3CreateResult:
        for attempt in range(self.max_poll_attempts):
            result = self.query_image_to_video(provider_task_id)
            if result.status == "SUCCEEDED":
                if result.result_url is None:
                    raise H3ProviderFailed(
                        "H3 succeeded task is missing a result URL",
                        provider_task_id=provider_task_id,
                        terminal=True,
                    )
                return H3CreateResult(
                    provider_task_id=provider_task_id,
                    status="SUCCEEDED",
                    result_url=result.result_url,
                    result_content=b"",
                    audio_quality_status="NOT_REQUIRED",
                    quality_issue_codes=[],
                    output_seconds=result.output_seconds,
                )
            if result.status in {"FAILED", "CANCELLED"}:
                raise H3ProviderFailed(
                    f"H3 task finished with status {result.status.lower()}",
                    provider_task_id=provider_task_id,
                    terminal=True,
                    provider_reason=result.failure_reason,
                )
            if attempt < self.max_poll_attempts - 1:
                self.sleeper(self.poll_interval_seconds)
        raise H3ProviderFailed(
            "H3 query did not return the created task as completed",
            provider_task_id=provider_task_id,
        )

    def download_result(self, url: str) -> bytes:
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise H3ProviderFailed("H3 result URL must be HTTPS", terminal=True)
        _require_public_https_host(parsed.hostname)
        return self.transport.request("GET", url, headers={})

    def _query_task(self, provider_task_id: str) -> dict[str, Any]:
        # 秘塔原生查询端点是路径式 /v2/query/video_generation/{task_id}（与官方 v2 及
        # 秘塔前端一致），返回单任务对象；task_id 做 URL 编码避免特殊字符破坏路径。
        url = f"{METASO_BASE_URL}{METASO_QUERY_PATH}/{quote(provider_task_id, safe='')}"
        payload = _metaso_json_object(
            self.transport.request("GET", url, headers=self._api_headers())
        )
        return _extract_metaso_task(payload, provider_task_id)

    def _api_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }


def metaso_h3_provider_from_settings(
    conn: BusinessConnection, *, task_id: str | None = None
) -> MetasoH3Provider:
    from app.h3_account_pool import account_api_key

    try:
        api_key = account_api_key(conn, task_id=task_id)
    except SettingsUnavailableError as exc:
        raise H3ProviderSettingsUnavailable("H3 provider settings cannot be read") from exc
    if not api_key:
        raise H3ProviderSettingsUnavailable("H3 provider API key is not configured")
    return MetasoH3Provider(
        api_key=api_key,
        # Real H3 rendering routinely exceeds five minutes; poll up to ~1.5h.
        poll_interval_seconds=5.0,
        max_poll_attempts=1080,
    )


def h3_provider_for_task(
    conn: BusinessConnection, provider_name: str, *, task_id: str | None = None
) -> H3Provider:
    if provider_name == "fake_h3":
        if is_customer_production():
            raise H3ProviderSettingsUnavailable(
                "fake H3 provider is forbidden in customer production"
            )
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
        return metaso_h3_provider_from_settings(conn, task_id=task_id)
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
            output_seconds=int(request["duration"]),
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


class GenerationPriceQuote(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resolution: Literal["768P", "2K"]
    duration_seconds: int
    quantity: int
    # 16-30 秒使用后台独立配置的长档费率，不能假设固定倍数。
    price_tier: Literal["4-15s", "16-30s"] = "4-15s"
    unit_price_fen_per_second: int
    estimated_seconds: int
    estimated_price_fen: int
    unit_credits: float
    estimated_credits: int
    credit_price_version: int
    quote_token: str
    quote_expires_at: int
    max_credits: int
    # 客户套餐折扣（取更优合并后）；无折扣时为 None（价格保持平台口径）。
    discount_rate: Decimal | None = None
    discount_source: str | None = None


class TaskSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    status: str
    archive_status: str
    quality_status: str
    quality_issue_codes: list[str]
    result_asset_id: str | None
    direct_result_available: bool
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


class BatchProgress(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total_count: int
    terminal_count: int
    progress_percent: int
    counts: dict[str, int]
    historical_counts: dict[str, int] = Field(default_factory=dict)


class BatchCredits(BaseModel):
    """本批任务已落账的积分去向（客户可见口径）。

    为什么挂在批次上：任务失败时用户第一眼看的是任务页而不是消费记录；有了这两个数，
    任务页可以直接说「预扣的 N 积分已退回」，用户不必再去流水里自己对账。
    只统计已经写入流水的 SETTLE / RELEASE，尚未结算的预扣不算在内——所以两个数都为 0
    只表示「还没有落账」，不表示「没扣过」。
    """

    model_config = ConfigDict(extra="forbid")

    charged_credits: int = 0
    refunded_credits: int = 0


class BatchResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    project_id: str | None = None
    project_name: str | None = None
    prompt_version_id: str
    status: str
    quantity: int
    stale: bool
    display_name: str | None = None
    source_batch_id: str | None = None
    source_task_id: str | None = None
    generation_reason: str | None = None
    creation_kind: str = "replica"
    progress: BatchProgress
    credits: BatchCredits = Field(default_factory=BatchCredits)
    tasks: list[TaskResult]

    @model_validator(mode="after")
    def validate_quantity(self) -> BatchResult:
        if self.quantity != self.progress.total_count:
            raise ValueError("quantity must match progress total_count")
        return self


class GenerationBatchListItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    project_id: str | None = None
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
    creation_kind: str = "replica"
    progress: BatchProgress
    credits: BatchCredits = Field(default_factory=BatchCredits)
    total_estimated_cost: float | None
    total_actual_cost: float | None
    needs_attention_count: int
    has_results: bool
    tasks: list[TaskSummary]


class GenerationBatchListPage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[GenerationBatchListItem]
    next_cursor: str | None
    total: int


BatchStatusFilter = Literal[
    "PENDING",
    "QUEUED",
    "NEEDS_ATTENTION",
    "SUCCEEDED",
    "COMPLETED_WITH_FAILURES",
]


def latest_version(
    conn: BusinessConnection,
    *,
    project_id: str,
    kind: str,
) -> BusinessRow | None:
    return cast(
        BusinessRow | None,
        conn.execute(
            """
            SELECT id, project_id, asset_id, kind, version_number, payload_json,
                   created_by_user_id, created_at
            FROM versions
            WHERE project_id = %s AND kind = %s
            ORDER BY version_number DESC
            LIMIT 1
            """,
            (project_id, kind),
        ).fetchone(),
    )


def require_latest_version(
    conn: BusinessConnection,
    *,
    row: BusinessRow,
    project_id: str,
    kind: str,
    code: str,
    message: str,
    extra: dict[str, Any] | None = None,
) -> None:
    latest = latest_version(conn, project_id=project_id, kind=kind)
    if latest is None or str(latest["id"]) != str(row["id"]):
        raise generation_error(409, code, message, extra=extra)


def version_state(
    conn: BusinessConnection,
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


def version_stale_reasons(conn: BusinessConnection, *, row: BusinessRow) -> list[str]:
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
    expected_version = (
        FINAL_REPLICA_TEMPLATE_VERSION
        if payload.get("final_composition")
        else H3_PROMPT_TEMPLATE_VERSION
    )
    expected_hash = (
        FINAL_REPLICA_TEMPLATE_HASH if payload.get("final_composition") else H3_PROMPT_TEMPLATE_HASH
    )
    if (frozen_template_version is not None and frozen_template_version != expected_version) or (
        frozen_template_hash is not None and frozen_template_hash != expected_hash
    ):
        reasons.append("TEMPLATE_SUPERSEDED")

    source_context = payload.get("prompt_context") or {}
    frozen_script_id = payload.get("script_version_id") or source_context.get("script_version_id")
    if isinstance(frozen_script_id, str):
        script = latest_version(conn, project_id=project_id, kind=SCRIPT_KIND)
        if script is None or frozen_script_id != str(script["id"]):
            reasons.append("SCRIPT_SUPERSEDED")
        else:
            reasons.extend(version_stale_reasons(conn, row=script))

    frozen_shot_card_id = payload.get("shot_card_version_id") or source_context.get(
        "shot_card_version_id"
    )
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
    conn: BusinessConnection,
    *,
    row: BusinessRow,
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
    conn: BusinessConnection,
    *,
    project_id: str,
    first_frame_asset_id: str,
) -> dict[str, Any]:
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
    if selection is None:
        raise generation_error(
            409,
            "FIRST_FRAME_CONFIRMATION_REQUIRED",
            "Confirm a first-frame candidate before compiling or submitting H3 generation.",
        )
    # 历史版本放开：来源绑定与确认记录同源——从 selection 指向的候选版本读取，
    # 而不是最新版本（用户可能显式选择了历史图，其输入绑定属于旧版本）。
    selection_payload = json.loads(str(selection["payload_json"]))
    candidates_version_id = (
        selection_payload.get("first_frame_candidates_version_id")
        if isinstance(selection_payload, dict)
        else None
    )
    candidates = (
        conn.execute(
            """
            SELECT id, payload_json
            FROM versions
            WHERE id = %s AND project_id = %s AND kind = 'first_frame_candidates'
            """,
            (candidates_version_id, project_id),
        ).fetchone()
        if isinstance(candidates_version_id, str)
        else None
    )
    if candidates is None:
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
        "first_frame_reconstruction_mode": candidate_payload.get("reconstruction_mode"),
        "first_frame_replace_scene": candidate_payload.get("replace_scene") is True,
        "character_contract": candidate_payload.get("character_contract"),
        "project_character_appearance_version_id": candidate_payload.get(
            "project_character_appearance_version_id"
        ),
    }


def create_script_version(
    conn: BusinessConnection,
    *,
    project_id: str,
    actor: CurrentUser,
    request: ScriptRequest,
) -> BusinessRow:
    require_not_auditor(
        conn,
        actor=actor,
        action="script.create",
        entity_type="project",
        entity_id=project_id,
    )
    require_project_access(conn, actor=actor, project_id=project_id, action="script.create")
    try:
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
            extra={"stale_reasons": ["SHOT_CARD_SUPERSEDED"]},
        )
        stale_reasons = shot_card_stale_reasons(conn, row=shot_card)
        if stale_reasons:
            raise generation_error(
                409,
                "SHOT_CARD_STALE",
                "Save the latest analysis as a new shot-card version before editing the script.",
                extra={"stale_reasons": stale_reasons},
            )
        shot_payload = json.loads(str(shot_card["payload_json"]))
        text = request.text.strip()
        if not text and request.source != "no_narration":
            raise generation_error(
                422,
                "SCRIPT_TEXT_REQUIRED",
                "Script text cannot be blank.",
            )
        if request.source == "no_narration" and text:
            raise generation_error(422, "SCRIPT_SOURCE_CONFLICT", "无口播选项不能同时包含台词。")
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
    conn: BusinessConnection,
    *,
    project_id: str,
    actor: CurrentUser,
    request: PromptCompileRequest,
) -> BusinessRow:
    require_not_auditor(
        conn,
        actor=actor,
        action="prompt.compile",
        entity_type="project",
        entity_id=project_id,
    )
    require_project_access(conn, actor=actor, project_id=project_id, action="prompt.compile")
    if (
        actor.role == "customer"
        and request.output_duration_seconds not in CUSTOMER_DURATION_OPTIONS
    ):
        raise generation_error(
            422,
            "DURATION_OPTION_INVALID",
            "Customer generation duration must be an integer from 4 to 30 seconds.",
        )
    try:
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
            extra={"stale_reasons": ["SCRIPT_SUPERSEDED"]},
        )
        stale_reasons = version_stale_reasons(conn, row=script)
        if stale_reasons:
            raise generation_error(
                409,
                "SCRIPT_STALE",
                "Save a script from the current analysis and shot-card version.",
                extra={"stale_reasons": stale_reasons},
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
            extra={"stale_reasons": ["SHOT_CARD_SUPERSEDED"]},
        )
        shot_stale_reasons = shot_card_stale_reasons(conn, row=shot_card)
        if shot_stale_reasons:
            raise generation_error(
                409,
                "SHOT_CARD_STALE",
                "Save the latest analysis as a new shot-card version before compiling.",
                extra={"stale_reasons": shot_stale_reasons},
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
        from app.h3_prompts import compile_replica_final_text, dialogue, prompt_issues

        source_frame_id = first_frame_sources.get("source_frame_selection_version_id")
        source_frame_time = -1.0  # Historical selections may not include a source timestamp.
        if source_frame_id:
            source_frame = require_version(
                conn,
                version_id=str(source_frame_id),
                project_id=project_id,
                kind="source_frame_selection",
            )
            source_frame_payload = json.loads(str(source_frame["payload_json"]))
            timestamp = source_frame_payload.get("timestamp_seconds")
            if isinstance(timestamp, int | float):
                source_frame_time = float(timestamp)
        timeline_scale_factor = (
            request.output_duration_seconds / source_duration_seconds
            if request.output_duration_seconds != source_duration_seconds
            else 1.0
        )
        prompt_text = compile_replica_final_text(
            shot_payload=shot_payload,
            script_text=str(script_payload["full_text"]),
            source_duration=source_duration_seconds,
            duration=request.output_duration_seconds,
            timeline_policy=request.timeline_policy,
            source_frame_time=source_frame_time,
            opening_action=request.opening_action,
            replace_scene=first_frame_sources["first_frame_replace_scene"],
        )
        if len(prompt_text) > MAX_GENERATION_PROMPT_CHARS:
            raise generation_error(
                422,
                "PROMPT_TEXT_TOO_LONG",
                "Generation prompt must not exceed 7000 characters.",
            )
        issues = prompt_issues(
            prompt_text,
            mode="I2VA",
            duration=request.output_duration_seconds,
            labels=["<Picture 1>"],
        )
        if issues:
            raise generation_error(422, issues[0].code, issues[0].message)
        if dialogue(prompt_text) != "".join(str(script_payload["full_text"]).split()):
            raise generation_error(
                409, "DIALOGUE_MISMATCH", "最终稿中存在未确认的台词，请检查分镜和开场方案。"
            )
        payload = {
            "schema_version": GENERATION_SCHEMA_VERSION,
            "status": "SAVED",
            "prompt_text": prompt_text,
            "content_hash": content_hash(prompt_text),
            "template_version": FINAL_REPLICA_TEMPLATE_VERSION,
            "template_hash": FINAL_REPLICA_TEMPLATE_HASH,
            "source_analysis_version_id": shot_payload.get("source_analysis_version_id"),
            "script_version_id": request.script_version_id,
            "shot_card_version_id": request.shot_card_version_id,
            "first_frame_asset_id": request.first_frame_asset_id,
            "first_frame_uri": str(first_frame["storage_uri"]),
            "source_duration_seconds": source_duration_seconds,
            "timeline_scale_factor": timeline_scale_factor,
            "timeline_policy": request.timeline_policy,
            "source_frame_time": source_frame_time,
            "opening_action": request.opening_action,
            "final_composition": True,
            "confirmed_script_text": str(script_payload["full_text"]),
            "output_duration_seconds": request.output_duration_seconds,
            "resolution": request.resolution,
            "ratio": request.ratio,
            **first_frame_sources,
        }
        row = insert_version(
            conn,
            project_id=project_id,
            asset_id=request.first_frame_asset_id,
            kind=H3_PROMPT_KIND,
            created_by_user_id=actor.id,
            payload=payload,
            commit=False,
        )
        conn.execute(
            "UPDATE versions SET scope = 'project', source = 'compiled', "
            "author_user_id = %s WHERE id = %s",
            (actor.id, str(row["id"])),
        )
        conn.commit()
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
    conn: BusinessConnection,
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
        # 复刻默认贴着源时长走；客户不再被二分到 4/15 两档。
        duration_seconds = max(4, min(15, round(source_duration_seconds)))
    elif actor.role == "customer" and duration_seconds not in CUSTOMER_DURATION_OPTIONS:
        raise generation_error(
            422,
            "DURATION_OPTION_INVALID",
            "Customer generation duration must be an integer from 4 to 30 seconds.",
        )
    prompt_text = compile_prompt_text(
        script_payload=script_payload,
        shot_payload=shot_payload,
        source_duration_seconds=source_duration_seconds,
        duration_seconds=duration_seconds,
        resolution=request.resolution,
    )
    if len(prompt_text) > MAX_GENERATION_PROMPT_CHARS:
        raise generation_error(
            422,
            "PROMPT_TEXT_TOO_LONG",
            "Generation prompt must not exceed 7000 characters.",
        )
    return PromptPreviewResult(
        prompt_text=prompt_text,
        output_duration_seconds=duration_seconds,
        resolution=request.resolution,
        ratio=request.ratio,
        script_source=script_source,
        shot_card_version_id=(str(shot_card["id"]) if shot_card is not None else None),
    )


def revise_prompt_version(
    conn: BusinessConnection,
    *,
    project_id: str,
    actor: CurrentUser,
    request: PromptRevisionRequest,
    commit: bool = True,
) -> BusinessRow:
    require_not_auditor(
        conn,
        actor=actor,
        action="prompt.revise",
        entity_type="project",
        entity_id=project_id,
    )
    require_project_access(conn, actor=actor, project_id=project_id, action="prompt.revise")
    try:
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
            extra={"stale_reasons": ["PROMPT_SUPERSEDED"]},
        )
        stale_reasons = version_stale_reasons(conn, row=base)
        if stale_reasons:
            raise generation_error(
                409,
                "PROMPT_STALE",
                "Upstream inputs changed; compile a new prompt before editing.",
                extra={"stale_reasons": stale_reasons},
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
            commit=False,
        )
        conn.execute(
            "UPDATE versions SET scope = 'project', source = 'manual_revision', "
            "author_user_id = %s WHERE id = %s",
            (actor.id, str(row["id"])),
        )
        insert_audit(
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
        if commit:
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    return row


def save_prompt_to_library(
    conn: BusinessConnection,
    *,
    project_id: str,
    actor: CurrentUser,
    request: SavedPromptRequest,
) -> BusinessRow:
    require_not_auditor(
        conn,
        actor=actor,
        action="saved_prompt.create",
        entity_type="project",
        entity_id=project_id,
    )
    require_project_access(conn, actor=actor, project_id=project_id, action="saved_prompt.create")
    base_payload: dict[str, Any] = {}
    source_version_id: str
    source_kind: str
    if request.base_prompt_version_id is not None:
        base = require_version(
            conn,
            version_id=request.base_prompt_version_id,
            project_id=project_id,
            kind=H3_PROMPT_KIND,
        )
        base_payload = json.loads(str(base["payload_json"]))
        source_version_id = str(base["id"])
        source_kind = H3_PROMPT_KIND
    else:
        source = latest_version(conn, project_id=project_id, kind="shot_card")
        if source is None:
            source = latest_version(conn, project_id=project_id, kind="analysis")
        if source is None:
            raise generation_error(
                409,
                "ANALYSIS_NOT_READY",
                "Analyze the reference video before saving its reverse prompt.",
            )
        source_version_id = str(source["id"])
        source_kind = str(source["kind"])
    prompt_text = request.prompt_text.strip()
    name = request.name.strip()
    if not prompt_text:
        raise generation_error(422, "PROMPT_TEXT_REQUIRED", "Prompt text cannot be blank.")
    if not name:
        raise generation_error(422, "PROMPT_NAME_REQUIRED", "Prompt name cannot be blank.")
    template_context = None
    if request.generation_context is not None:
        from app.prompt_context import resolve_context

        target = request.generation_context.model_copy(update={"project_id": project_id})
        template_context = resolve_context(conn, actor=actor, request=target)
        template_context["formatter_version"] = FORMATTER_VERSION
    payload = {
        "generation_context": template_context,
        "schema_version": GENERATION_SCHEMA_VERSION,
        "name": name,
        "prompt_text": prompt_text,
        "content_hash": content_hash(prompt_text),
        "scope": "user",
        "source": "reverse_prompt_revision",
        "author_user_id": actor.id,
        "base_prompt_version_id": request.base_prompt_version_id,
        "source_version_id": source_version_id,
        "source_kind": source_kind,
        "ratio": base_payload.get("ratio", "adaptive"),
        "output_duration_seconds": base_payload.get("output_duration_seconds"),
        "resolution": base_payload.get("resolution"),
    }
    try:
        version_id = str(uuid4())
        conn.execute(
            """
            INSERT INTO versions (
                id, project_id, asset_id, kind, version_number, payload_json,
                created_by_user_id, scope, source, author_user_id
            ) VALUES (%s, %s, NULL, 'saved_prompt', %s, %s, %s, 'user',
                      'reverse_prompt_revision', %s)
            """,
            (
                version_id,
                project_id,
                next_version_number(conn, project_id=project_id, kind="saved_prompt"),
                json.dumps(payload, ensure_ascii=True, sort_keys=True),
                actor.id,
                actor.id,
            ),
        )
        row = get_version(conn, version_id)
        insert_audit(
            conn,
            actor=actor,
            action="saved_prompt.create",
            entity_type="version",
            entity_id=str(row["id"]),
            metadata={
                "project_id": project_id,
                "base_prompt_version_id": request.base_prompt_version_id,
                "source_version_id": source_version_id,
                "source": "reverse_prompt_revision",
            },
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return row


def list_saved_prompts(
    conn: BusinessConnection,
    *,
    project_id: str,
    actor: CurrentUser,
) -> list[BusinessRow]:
    require_project_access(conn, actor=actor, project_id=project_id, action="saved_prompt.read")
    return list(
        conn.execute(
            """
            SELECT id, project_id, asset_id, kind, version_number, payload_json,
                   created_by_user_id, created_at
            FROM versions
            WHERE project_id = %s AND kind = 'saved_prompt' AND author_user_id = %s
            ORDER BY created_at DESC, version_number DESC
            """,
            (project_id, actor.id),
        ).fetchall()
    )


def apply_saved_prompt(
    conn: BusinessConnection,
    *,
    project_id: str,
    saved_prompt_id: str,
    actor: CurrentUser,
    request: ApplySavedPromptRequest,
) -> BusinessRow:
    require_not_auditor(
        conn,
        actor=actor,
        action="saved_prompt.apply",
        entity_type="version",
        entity_id=saved_prompt_id,
    )
    require_project_access(conn, actor=actor, project_id=project_id, action="saved_prompt.apply")
    saved = require_version(
        conn,
        version_id=saved_prompt_id,
        project_id=project_id,
        kind="saved_prompt",
    )
    owner = conn.execute(
        "SELECT author_user_id FROM versions WHERE id = %s",
        (saved_prompt_id,),
    ).fetchone()
    if owner is None or str(owner["author_user_id"]) != actor.id:
        raise generation_error(404, "SAVED_PROMPT_NOT_FOUND", "Saved prompt does not exist.")
    saved_payload = json.loads(str(saved["payload_json"]))
    try:
        revised = revise_prompt_version(
            conn,
            project_id=project_id,
            actor=actor,
            request=PromptRevisionRequest(
                base_prompt_version_id=request.base_prompt_version_id,
                prompt_text=str(saved_payload["prompt_text"]),
            ),
            commit=False,
        )
        insert_audit(
            conn,
            actor=actor,
            action="saved_prompt.apply",
            entity_type="version",
            entity_id=saved_prompt_id,
            metadata={
                "project_id": project_id,
                "created_prompt_version_id": str(revised["id"]),
            },
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return revised


def lock_prompt_version(
    conn: BusinessConnection,
    *,
    project_id: str,
    prompt_version_id: str,
    actor: CurrentUser,
) -> BusinessRow:
    require_not_auditor(
        conn,
        actor=actor,
        action="prompt.lock",
        entity_type="version",
        entity_id=prompt_version_id,
    )
    require_project_access(conn, actor=actor, project_id=project_id, action="prompt.lock")
    try:
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
            extra={"stale_reasons": ["PROMPT_SUPERSEDED"]},
        )
        lock_stale_reasons = version_stale_reasons(conn, row=row)
        if lock_stale_reasons:
            raise generation_error(
                409,
                "PROMPT_STALE",
                "Upstream inputs changed; compile a new prompt before locking.",
                extra={"stale_reasons": lock_stale_reasons},
            )
        payload = json.loads(str(row["payload_json"]))
        if payload["status"] == "USED":
            conn.rollback()
            return row
        if payload["status"] not in {"SAVED", "LOCKED"}:
            raise generation_error(409, "PROMPT_STATUS_INVALID", "Prompt cannot be locked.")
        payload["status"] = "LOCKED"
        conn.execute(
            "UPDATE versions SET payload_json = %s WHERE id = %s",
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
    conn: BusinessConnection, *, actor_id: str, project_id: str | None, key: str
) -> BusinessRow | None:
    # 独立批次（project_id NULL）在 SQL 等值比较下永远不命中，须用 IS NULL。
    if project_id is None:
        clause = "created_by_user_id = %s AND project_id IS NULL AND idempotency_key = %s"
        params: tuple[object, ...] = (actor_id, key)
    else:
        clause = "created_by_user_id = %s AND project_id = %s AND idempotency_key = %s"
        params = (actor_id, project_id, key)
    return cast(
        BusinessRow | None,
        conn.execute(
            f"""
            SELECT id, request_hash
            FROM generation_batches
            WHERE {clause}
            """,
            params,
        ).fetchone(),
    )


def _seconds_from_prompt_snapshot(snapshot: object) -> int:
    """W11：从锁定提示词快照读取提交档位秒数；缺失或不可解析回退 1。"""
    if not snapshot:
        return 1
    try:
        payload = json.loads(str(snapshot))
    except (TypeError, ValueError):
        return 1
    if not isinstance(payload, dict):
        return 1
    seconds = payload.get("output_duration_seconds")
    try:
        return max(1, int(seconds)) if seconds is not None else 1
    except (TypeError, ValueError):
        return 1


def _reserve_generation_credit(
    conn: BusinessConnection,
    *,
    user_id: str,
    task_id: str,
    billing_round: int | None = 1,
    seconds: int = 1,
    pricing_snapshot: dict[str, Any] | None = None,
) -> int:
    try:
        return reserve_internal_billing(
            conn,
            user_id=user_id,
            task_id=task_id,
            billing_round=billing_round,
            seconds=seconds,
            pricing_snapshot=pricing_snapshot,
        )
    except InsufficientCreditsError as exc:
        raise generation_error(
            402,
            "INSUFFICIENT_CREDITS",
            "Available credits are insufficient for this generation request.",
        ) from exc
    except BillingInvariantError as exc:
        logger.error(
            "generation billing invariant failed for user %s task %s: %s",
            user_id,
            task_id,
            exc,
        )
        raise generation_error(
            500,
            "BILLING_INVARIANT_VIOLATION",
            "Billing state is inconsistent; contact an administrator.",
        ) from exc


def _enforce_acceptance_generation_limit(
    conn: BusinessConnection,
    *,
    user_id: str,
    requested_quantity: int,
) -> None:
    """Cap the paid provider rehearsal to one task for one explicit customer.

    Existing tasks count even when they failed or need attention because a
    submission may already have incurred supplier cost. PostgreSQL locks the
    customer row so concurrent batches cannot both pass the count.
    """
    configured_user_id = os.environ.get(ACCEPTANCE_GENERATION_USER_ID_ENV, "").strip()
    raw_limit = os.environ.get(ACCEPTANCE_PAID_GENERATION_LIMIT_ENV, "").strip()
    if not configured_user_id and not raw_limit:
        return
    if not configured_user_id or not raw_limit:
        raise generation_error(
            503,
            "ACCEPTANCE_GENERATION_CONFIGURATION_INVALID",
            "The controlled generation rehearsal is not configured safely.",
        )
    if user_id != configured_user_id:
        return
    try:
        limit = int(raw_limit)
    except ValueError as exc:
        raise generation_error(
            503,
            "ACCEPTANCE_GENERATION_CONFIGURATION_INVALID",
            "The controlled generation rehearsal is not configured safely.",
        ) from exc
    if limit != 1:
        raise generation_error(
            503,
            "ACCEPTANCE_GENERATION_CONFIGURATION_INVALID",
            "The controlled generation rehearsal is limited to one paid task.",
        )
    if conn.is_postgres:
        conn.execute("SELECT id FROM users WHERE id = %s FOR UPDATE", (user_id,))
    existing = conn.execute(
        """
        SELECT COUNT(*)
        FROM generation_tasks AS task
        JOIN generation_batches AS batch ON batch.id = task.batch_id
        WHERE batch.created_by_user_id = %s AND task.provider = 'metaso'
        """,
        (user_id,),
    ).fetchone()
    existing_count = int(existing[0]) if existing is not None else 0
    if existing_count + requested_quantity > limit:
        raise generation_error(
            409,
            "ACCEPTANCE_GENERATION_LIMIT_REACHED",
            "The controlled generation rehearsal has already used its paid task.",
        )


def create_generation_batch(
    conn: BusinessConnection,
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

        if request.provider == "fake_h3" and is_customer_production():
            raise generation_error(
                503,
                "FAKE_H3_PROVIDER_FORBIDDEN",
                "Customer production cannot create simulated H3 generation tasks.",
            )

        runtime = read_runtime_limits(conn)
        max_quantity = runtime["max_generation_count_per_batch"]
        if request.quantity > max_quantity:
            raise generation_error(
                422,
                "QUANTITY_EXCEEDS_LIMIT",
                f"quantity must be less than or equal to {max_quantity}",
            )

        if request.provider == "metaso":
            _enforce_acceptance_generation_limit(
                conn,
                user_id=actor.id,
                requested_quantity=request.quantity,
            )

        # Mutable runtime/provider preflights apply only to a genuinely new
        # submission. Replayed keys return above even if settings changed.
        if request.provider == "metaso":
            try:
                metaso_h3_provider_from_settings(conn)
            except H3ProviderSettingsUnavailable as exc:
                raise generation_error(
                    503,
                    "H3_SETTINGS_UNAVAILABLE",
                    "Save a readable H3 provider API key before queuing a real H3 task.",
                ) from exc

        if request.prompt_text is not None:
            sources = confirmed_first_frame_sources(
                conn, project_id=project_id, first_frame_asset_id=request.first_frame_asset_id
            )
            context = request.prompt_context or PromptContext()
            from app.h3_prompts import prompt_issues

            issues = prompt_issues(
                request.prompt_text,
                mode="I2VA",
                duration=request.output_duration_seconds,
                labels=["<Picture 1>"],
                strict=False,
            )
            if issues:
                raise generation_error(422, issues[0].code, issues[0].message)
            if context.optimization_task_id:
                task = conn.execute(
                    "SELECT * FROM prompt_optimization_receipts WHERE id=%s AND owner_user_id=%s",
                    (context.optimization_task_id, actor.id),
                ).fetchone()
                if task is None:
                    raise generation_error(404, "PROMPT_TASK_NOT_FOUND", "优化任务不存在。")
                snapshot = json.loads(str(task["request_json"]))["context"]
                if (
                    snapshot["project_id"] != project_id
                    or snapshot["first_frame_asset_id"] != request.first_frame_asset_id
                    or snapshot["duration_seconds"] != request.output_duration_seconds
                    or snapshot["ratio"] != request.ratio
                    or snapshot["context_hash"] != context.context_hash
                ):
                    raise generation_error(
                        409, "PROMPT_CONTEXT_CHANGED", "素材或生成参数已改变，请核对提示词。"
                    )

                for key, kind, reason in (
                    ("analysis_version_id", "analysis", "ANALYSIS_SUPERSEDED"),
                    ("shot_card_version_id", "shot_card", "SHOT_CARD_SUPERSEDED"),
                    ("script_version_id", SCRIPT_KIND, "SCRIPT_SUPERSEDED"),
                ):
                    if snapshot.get(key):
                        source = require_version(
                            conn, version_id=snapshot[key], project_id=project_id, kind=kind
                        )
                        require_latest_version(
                            conn,
                            row=source,
                            project_id=project_id,
                            kind=kind,
                            code="PROMPT_STALE",
                            message="优化稿来源已更新，请重新合成最终提示词。",
                            extra={"stale_reasons": [reason]},
                        )
                        if getattr(context, key) not in (None, snapshot[key]):
                            raise generation_error(
                                409, "PROMPT_CONTEXT_CHANGED", "优化稿与确认来源不同。"
                            )

            for version_id, kind, reason in (
                (context.analysis_version_id, "analysis", "ANALYSIS_SUPERSEDED"),
                (context.shot_card_version_id, "shot_card", "SHOT_CARD_SUPERSEDED"),
                (context.script_version_id, SCRIPT_KIND, "SCRIPT_SUPERSEDED"),
            ):
                if version_id is not None:
                    source_version = require_version(
                        conn, version_id=version_id, project_id=project_id, kind=kind
                    )
                    require_latest_version(
                        conn,
                        row=source_version,
                        project_id=project_id,
                        kind=kind,
                        code="PROMPT_STALE",
                        message="文案或分镜已更新，请重新合成最终提示词。",
                        extra={"stale_reasons": [reason]},
                    )
            final_payload: dict[str, Any] = {}
            if context.final_prompt_version_id:
                final_version = require_version(
                    conn,
                    version_id=context.final_prompt_version_id,
                    project_id=project_id,
                    kind=H3_PROMPT_KIND,
                )
                final_stale_reasons = version_stale_reasons(conn, row=final_version)
                if final_stale_reasons:
                    raise generation_error(
                        409,
                        "PROMPT_STALE",
                        "最终稿的来源已变化，请重新合成。",
                        extra={"stale_reasons": final_stale_reasons},
                    )
                final_payload = json.loads(str(final_version["payload_json"]))
                if not final_payload.get("final_composition"):
                    raise generation_error(409, "FINAL_PROMPT_REQUIRED", "请先完成最终提示词合成。")
                for key in (
                    "first_frame_asset_id",
                    "output_duration_seconds",
                    "resolution",
                    "ratio",
                ):
                    if final_payload.get(key) != getattr(request, key):
                        raise generation_error(
                            409,
                            "PROMPT_PARAMETERS_MISMATCH",
                            "首帧或参数已变化，请重新合成最终稿。",
                        )
                from app.h3_prompts import dialogue

                final_script = require_version(
                    conn,
                    version_id=final_payload["script_version_id"],
                    project_id=project_id,
                    kind=SCRIPT_KIND,
                )
                protected = json.loads(str(final_script["payload_json"]))["full_text"]
                if "".join(protected.split()) != dialogue(request.prompt_text):
                    raise generation_error(
                        409,
                        "DIALOGUE_MISMATCH",
                        "提示词台词与确认文案不同，请先更新确认文案并重新合成。",
                    )
                final_issues = prompt_issues(
                    request.prompt_text,
                    mode="I2VA",
                    duration=request.output_duration_seconds,
                    labels=["<Picture 1>"],
                )
                if final_issues:
                    raise generation_error(422, final_issues[0].code, final_issues[0].message)
            prompt = insert_version(
                conn,
                project_id=project_id,
                asset_id=request.first_frame_asset_id,
                kind=H3_PROMPT_KIND,
                created_by_user_id=actor.id,
                commit=False,
                payload={
                    **final_payload,
                    **sources,
                    "schema_version": GENERATION_SCHEMA_VERSION,
                    "status": "LOCKED",
                    "prompt_text": request.prompt_text,
                    "content_hash": content_hash(request.prompt_text),
                    "prompt_context": context.model_dump(mode="json"),
                    "script_version_id": final_payload.get("script_version_id")
                    or context.script_version_id,
                    "shot_card_version_id": final_payload.get("shot_card_version_id")
                    or context.shot_card_version_id,
                    "source_analysis_version_id": final_payload.get("source_analysis_version_id")
                    or context.analysis_version_id,
                    "first_frame_asset_id": request.first_frame_asset_id,
                    "output_duration_seconds": request.output_duration_seconds,
                    "resolution": request.resolution,
                    "ratio": request.ratio,
                },
            )
        else:
            assert request.prompt_version_id is not None
            prompt = require_version(
                conn,
                version_id=request.prompt_version_id,
                project_id=project_id,
                kind=H3_PROMPT_KIND,
            )
        prompt_version_id = str(prompt["id"])
        prompt_stale_reasons = version_stale_reasons(conn, row=prompt)
        if prompt_stale_reasons:
            raise generation_error(
                409,
                "PROMPT_STALE",
                "Upstream inputs changed; compile and lock a new prompt before generating.",
                extra={"stale_reasons": prompt_stale_reasons},
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
        frozen_ratio = prompt_snapshot.get("ratio", "adaptive")
        if (
            (frozen_duration is not None and frozen_duration != request.output_duration_seconds)
            or (frozen_resolution is not None and frozen_resolution != request.resolution)
            or frozen_ratio != request.ratio
        ):
            raise generation_error(
                409,
                "PROMPT_PARAMETERS_MISMATCH",
                "Generation parameters must match the locked prompt snapshot.",
            )
        if (
            actor.role == "customer"
            and request.output_duration_seconds not in CUSTOMER_DURATION_OPTIONS
        ):
            raise generation_error(
                422,
                "DURATION_OPTION_INVALID",
                "Customer generation duration must be an integer from 4 to 30 seconds.",
            )
        if actor.role == "customer" and request.quantity not in CUSTOMER_QUANTITY_OPTIONS:
            raise generation_error(
                422,
                "QUANTITY_OPTION_INVALID",
                "Customer generation quantity must be 1, 2, or 4.",
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

        from app.billing_catalog import generation_service_for
        from app.quote_contracts import validate_quote

        pricing_snapshot = validate_quote(
            conn,
            user_id=actor.id,
            service=generation_service_for(request.resolution, request.output_duration_seconds),
            units=request.output_duration_seconds,
            quantity=request.quantity,
            quote_token=request.quote_token,
            max_credits=request.max_credits,
        )

        request_snapshot = generation_request_snapshot(request, prompt_snapshot)
        request_snapshot["prompt_version_id"] = prompt_version_id
        task_prompt_snapshot = {
            **prompt_snapshot,
            # 复刻流恒为图生视频（首帧已确认）。内联提示词路径不经 compile，
            # snapshot 会缺 first_frame_uri，导致 worker 误判为纯文本并以
            # adaptive 触发供应商契约失败；这里用已确认首帧的 storage_uri 兜底。
            "first_frame_uri": prompt_snapshot.get("first_frame_uri")
            or str(first_frame["storage_uri"]),
            "output_duration_seconds": request.output_duration_seconds,
            "resolution": request.resolution,
            "ratio": request.ratio,
        }
        batch_id = str(uuid4())
        used_prompt_snapshot = {**prompt_snapshot, "status": "USED"}
        cursor = conn.execute(
            """
            UPDATE versions
            SET payload_json = %s
            WHERE id = %s AND payload_json = %s
            """,
            (
                json.dumps(used_prompt_snapshot, ensure_ascii=True, sort_keys=True),
                prompt_version_id,
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
                creation_kind,
                status
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                batch_id,
                project_id,
                actor.id,
                request.idempotency_key,
                request_hash,
                json.dumps(request_snapshot, ensure_ascii=True, sort_keys=True),
                # 当前唯一的创建通道是项目复刻流；独立创作/人物置换接入时
                # 在各自的创建路径写入自己的通道值（I13 类型保真）。
                "replica",
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
                    next_poll_at,
                    billed_seconds
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP, %s)
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
                    prompt_version_id,
                    json.dumps(task_prompt_snapshot, ensure_ascii=True, sort_keys=True),
                    request.output_duration_seconds,
                ),
            )
            snapshot_generation_rates(
                conn,
                task_id=task_id,
                resolution=request.resolution,
                billed_seconds=request.output_duration_seconds,
            )
            _reserve_generation_credit(
                conn,
                user_id=actor.id,
                task_id=task_id,
                seconds=request.output_duration_seconds,
                pricing_snapshot=pricing_snapshot,
            )
        # Pattern D: this batch makes the user a queue participant; the cursor
        # row is upserted in the same transaction so rotation can serve them.
        ensure_user_queue_cursor(conn, user_id=actor.id)
        conn.commit()
    except IntegrityConstraintError as exc:
        conn.rollback()
        # 鉴别闸（r3 P1）：只有批次幂等键唯一约束的冲突才按竞争处理——
        # 同 try 块内其他 235xx（CHECK/外键/其他唯一约束）是真实数据错误，
        # 必须原样冒出，不得误报成「幂等冲突」。
        if not is_unique_conflict_on(exc, "uq_generation_batches_user_project_key"):
            raise
        # 同幂等键的并发提交者后到（事务已中止，无法在同事务内回放）：查既有
        # 批次回放而非误报。T2a 2026-11-01 按翻译层真实类型恢复兜底——
        # 此前 sqlite3/UniqueViolation 两个名字在 PG lane 都接不住翻译异常。
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


def _lock_paid_regeneration_admission(
    conn: BusinessConnection, *, user_id: str, project_id: str | None, key: str
) -> None:
    # 会话行/API key锁不覆盖同账号的其它凭证。必须在幂等读取和报价拒绝前
    # 串行同一受理身份，并持有到外层事务结束；否则QUOTE拒绝可能与另一未提交
    # 的成功受理并存，客户端会错误地允许新key重来。锁序为凭证→受理key→钱包。
    # 不锁源批次/任务行，避免与worker的任务→钱包→批次锁序形成环。
    scope = json.dumps([user_id, project_id, key], ensure_ascii=True, separators=(",", ":"))
    conn.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (f"generation:paid-regeneration:{scope}",),
    )


def regenerate_generation_batch(
    conn: BusinessConnection,
    *,
    batch_id: str,
    actor: CurrentUser,
    request: PaidRegenerationRequest,
) -> BatchResult:
    source_context = conn.execute(
        "SELECT project_id, created_by_user_id FROM generation_batches WHERE id = %s",
        (batch_id,),
    ).fetchone()
    if source_context is None:
        raise generation_error(404, "BATCH_NOT_FOUND", "Generation batch does not exist.")
    project_id = None if source_context["project_id"] is None else str(source_context["project_id"])
    require_not_auditor(
        conn,
        actor=actor,
        action="generation_batch.regenerate",
        entity_type="generation_batch",
        entity_id=batch_id,
    )
    require_batch_access(
        conn,
        actor=actor,
        project_id=project_id,
        created_by_user_id=str(source_context["created_by_user_id"]),
        action="generation_batch.regenerate",
    )

    new_batch_id = ""
    try:
        source_batch = conn.execute(
            """
            SELECT id, project_id, created_by_user_id, creation_kind,
                   request_snapshot_json
            FROM generation_batches
            WHERE id = %s
            """,
            (batch_id,),
        ).fetchone()
        if source_batch is None:
            raise generation_error(404, "BATCH_NOT_FOUND", "Generation batch does not exist.")
        billed_user_id = str(source_batch["created_by_user_id"])
        source_tasks = conn.execute(
            """
            SELECT id, generation_mode, provider, model, prompt_version_id,
                   prompt_snapshot_json, status, archive_status, quality_status,
                   quality_issue_codes, provider_task_id, provider_result_url,
                   submitted_at, superseded_by_task_id
            FROM generation_tasks
            WHERE batch_id = %s
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
        _lock_paid_regeneration_admission(
            conn, user_id=billed_user_id, project_id=project_id, key=request.idempotency_key
        )
        existing = _find_idempotent_batch(
            conn,
            actor_id=billed_user_id,
            project_id=project_id,
            key=request.idempotency_key,
        )
        if existing is not None:
            conn.rollback()
            require_matching_idempotent_batch(existing, request_hash=request_hash)
            return get_generation_batch(conn, batch_id=str(existing["id"]), actor=actor)

        # R0（深审 P0）：批级重生成此前绕过任务级终态门禁与 supersede 写入，
        # 对 RUNNING/SUBMISSION_UNCERTAIN 批次会双份生成 + 双份扣费。此处逐任务
        # 复用与任务级相同的门禁；进行中/未对账的源任务在此被 409 拦截。
        for source_task in source_tasks:
            if source_task["superseded_by_task_id"] is not None:
                raise generation_error(
                    409,
                    "SOURCE_TASK_ALREADY_SUPERSEDED",
                    f"Source task {source_task['id']} already has a paid replacement.",
                )
            require_task_paid_regeneration_state(source_task)

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

        pricing_snapshot = _validate_regeneration_quote(
            conn,
            user_id=billed_user_id,
            request=request,
            parameters=_regeneration_quote_parameters(
                actor_id=actor.id,
                operation_kind="MANUAL_BATCH_REGENERATE",
                source_batch_id=batch_id,
                source_task_ids=source_task_ids,
                request_snapshot=request_snapshot,
                prompt_snapshots=prompt_snapshots,
            ),
        )
        new_batch_id = str(uuid4())
        conn.execute(
            """
            INSERT INTO generation_batches (
                id, project_id, created_by_user_id, idempotency_key,
                request_hash, request_snapshot_json, creation_kind, status,
                source_batch_id, source_task_id, generation_reason
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'QUEUED', %s, NULL, %s)
            """,
            (
                new_batch_id,
                project_id,
                billed_user_id,
                request.idempotency_key,
                request_hash,
                request_snapshot,
                str(source_batch["creation_kind"]),
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
                    retry_requested_by_user_id, retry_requested_at, billed_seconds
                )
                VALUES (%s, %s, %s, %s, %s, 'PENDING', 'PENDING', 'PENDING',
                        %s, %s, CURRENT_TIMESTAMP, %s, %s, %s, %s, CURRENT_TIMESTAMP, %s)
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
                    _seconds_from_prompt_snapshot(prompt_snapshot),
                ),
            )
            replacement_snapshot = json.loads(prompt_snapshot)
            snapshot_generation_rates(
                conn,
                task_id=replacement_task_id,
                resolution=str(replacement_snapshot.get("resolution", "768P")),
                billed_seconds=_seconds_from_prompt_snapshot(prompt_snapshot),
            )
            _reserve_generation_credit(
                conn,
                user_id=billed_user_id,
                task_id=replacement_task_id,
                seconds=_seconds_from_prompt_snapshot(prompt_snapshot),
                pricing_snapshot=pricing_snapshot,
            )
            # 与任务级相同的 CAS supersede：并发窗口内源任务已被他方替换时整单回滚，
            # 防止同一源任务挂上两个付费替代。
            cursor = conn.execute(
                """
                UPDATE generation_tasks
                SET superseded_by_task_id = %s, superseded_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = %s AND superseded_by_task_id IS NULL
                """,
                (replacement_task_id, str(source_task["id"])),
            )
            if cursor.rowcount != 1:
                raise generation_error(
                    409,
                    "SOURCE_TASK_ALREADY_SUPERSEDED",
                    f"Source task {source_task['id']} already has a paid replacement.",
                )
        # 源批次任务已全部被替代，与任务级路径一致地刷新批次状态。
        _refresh_batch_status_in_transaction(conn, batch_id=batch_id)
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
                "billed_user_id": billed_user_id,
                "requested_by_user_id": actor.id,
            },
        )
        # Pattern D: regeneration also makes the user a queue participant
        # (same-transaction upsert; rotation serves them on the next round).
        ensure_user_queue_cursor(conn, user_id=billed_user_id)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return get_generation_batch(conn, batch_id=new_batch_id, actor=actor)


def regenerate_generation_task(
    conn: BusinessConnection,
    *,
    task_id: str,
    actor: CurrentUser,
    request: PaidRegenerationRequest,
) -> BatchResult:
    source_context = conn.execute(
        """
        SELECT generation_batches.project_id, generation_batches.created_by_user_id
        FROM generation_tasks
        JOIN generation_batches ON generation_batches.id = generation_tasks.batch_id
        WHERE generation_tasks.id = %s
        """,
        (task_id,),
    ).fetchone()
    if source_context is None:
        raise generation_error(404, "TASK_NOT_FOUND", "Generation task does not exist.")
    project_id = None if source_context["project_id"] is None else str(source_context["project_id"])
    require_not_auditor(
        conn,
        actor=actor,
        action="generation_task.regenerate",
        entity_type="generation_task",
        entity_id=task_id,
    )
    require_batch_access(
        conn,
        actor=actor,
        project_id=project_id,
        created_by_user_id=str(source_context["created_by_user_id"]),
        action="generation_task.regenerate",
    )

    new_batch_id = ""
    try:
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
                batch.created_by_user_id,
                batch.request_snapshot_json
            FROM generation_tasks AS task
            JOIN generation_batches AS batch ON batch.id = task.batch_id
            WHERE task.id = %s
            """,
            (task_id,),
        ).fetchone()
        if source is None:
            raise generation_error(404, "TASK_NOT_FOUND", "Generation task does not exist.")
        billed_user_id = str(source["created_by_user_id"])

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
        _lock_paid_regeneration_admission(
            conn, user_id=billed_user_id, project_id=project_id, key=request.idempotency_key
        )
        existing = _find_idempotent_batch(
            conn,
            actor_id=billed_user_id,
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

        pricing_snapshot = _validate_regeneration_quote(
            conn,
            user_id=billed_user_id,
            request=request,
            parameters=_regeneration_quote_parameters(
                actor_id=actor.id,
                operation_kind="TASK_PAID_REGENERATE",
                source_batch_id=source_batch_id,
                source_task_ids=[task_id],
                request_snapshot=request_snapshot,
                prompt_snapshots=[prompt_snapshot],
            ),
        )
        new_batch_id = str(uuid4())
        replacement_task_id = str(uuid4())
        conn.execute(
            """
            INSERT INTO generation_batches (
                id, project_id, created_by_user_id, idempotency_key,
                request_hash, request_snapshot_json, status,
                source_batch_id, source_task_id, generation_reason
            )
            VALUES (%s, %s, %s, %s, %s, %s, 'QUEUED', %s, %s, %s)
            """,
            (
                new_batch_id,
                project_id,
                billed_user_id,
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
                retry_requested_by_user_id, retry_requested_at, billed_seconds
            )
            VALUES (%s, %s, %s, %s, %s, 'PENDING', 'PENDING', 'PENDING',
                    %s, %s, CURRENT_TIMESTAMP, %s, %s, %s, %s, CURRENT_TIMESTAMP, %s)
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
                _seconds_from_prompt_snapshot(prompt_snapshot),
            ),
        )
        replacement_snapshot = json.loads(prompt_snapshot)
        snapshot_generation_rates(
            conn,
            task_id=replacement_task_id,
            resolution=str(replacement_snapshot.get("resolution", "768P")),
            billed_seconds=_seconds_from_prompt_snapshot(prompt_snapshot),
        )
        _reserve_generation_credit(
            conn,
            user_id=billed_user_id,
            task_id=replacement_task_id,
            seconds=_seconds_from_prompt_snapshot(prompt_snapshot),
            pricing_snapshot=pricing_snapshot,
        )
        cursor = conn.execute(
            """
            UPDATE generation_tasks
            SET superseded_by_task_id = %s, superseded_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = %s AND superseded_by_task_id IS NULL
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
                "billed_user_id": billed_user_id,
                "requested_by_user_id": actor.id,
            },
        )
        # Pattern D: the replacement makes the user a queue participant
        # (same-transaction upsert; rotation serves them on the next round).
        ensure_user_queue_cursor(conn, user_id=billed_user_id)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return get_generation_batch(conn, batch_id=new_batch_id, actor=actor)


def _regeneration_quote_parameters(
    *,
    actor_id: str,
    operation_kind: Literal["MANUAL_BATCH_REGENERATE", "TASK_PAID_REGENERATE"],
    source_batch_id: str,
    source_task_ids: Sequence[str],
    request_snapshot: str,
    prompt_snapshots: Sequence[str],
) -> dict[str, Any]:
    if not prompt_snapshots or len(source_task_ids) != len(prompt_snapshots):
        raise generation_error(409, "SOURCE_BATCH_EMPTY", "原批次缺少可重新生成的冻结任务。")
    try:
        request_payload = json.loads(request_snapshot)
        if not isinstance(request_payload, dict):
            raise ValueError("原批次冻结参数无效")
        worker_resolution = request_payload["resolution"]
        worker_duration = request_payload["output_duration_seconds"]
        if (
            worker_resolution not in {"768P", "2K"}
            or type(worker_duration) is not int
            or not 4 <= worker_duration <= 15
        ):
            raise ValueError("原批次冻结规格无效")
        specifications = []
        for snapshot in prompt_snapshots:
            payload = json.loads(snapshot)
            if not isinstance(payload, dict):
                raise ValueError("原任务冻结参数无效")
            resolution = payload["resolution"]
            duration = payload["output_duration_seconds"]
            if type(duration) is not int or (resolution, duration) != (
                worker_resolution,
                worker_duration,
            ):
                raise ValueError("报价与实际生成冻结规格不一致")
            specifications.append((resolution, duration))
    except (ValueError, TypeError, KeyError) as exc:
        raise generation_error(
            409,
            "QUOTE_SPECIFICATION_MISMATCH",
            "原任务冻结规格缺失或不一致，不能猜测重新生成报价，请重新选择生成参数。",
        ) from exc
    if len(set(specifications)) != 1:
        # 正常批次规格一致；历史混合批次不能用某一条的单价替整批报价。
        raise generation_error(
            409,
            "QUOTE_SPECIFICATION_MISMATCH",
            "原批次包含不同生成规格，请逐条获取报价并重新生成。",
        )
    resolution, duration = specifications[0]
    return {
        "resolution": resolution,
        "duration_seconds": duration,
        "quantity": len(source_task_ids),
        "specification": {
            "operation_kind": operation_kind,
            "requested_by_user_id": actor_id,
            "source_batch_id": source_batch_id,
            "source_task_ids": list(source_task_ids),
            "request_snapshot_hash": canonical_snapshot_hash(request_snapshot),
            "prompt_snapshot_hashes": [canonical_snapshot_hash(s) for s in prompt_snapshots],
        },
    }


def _validate_regeneration_quote(
    conn: BusinessConnection,
    *,
    user_id: str,
    request: PaidRegenerationRequest,
    parameters: dict[str, Any],
) -> dict[str, Any]:
    from app.billing_catalog import generation_service_for
    from app.quote_contracts import validate_quote

    return validate_quote(
        conn,
        user_id=user_id,
        service=generation_service_for(
            str(parameters["resolution"]), int(parameters["duration_seconds"])
        ),
        units=parameters["duration_seconds"],
        quantity=parameters["quantity"],
        specification=parameters["specification"],
        quote_token=request.quote_token,
        max_credits=request.max_credits,
    )


def generation_regeneration_price_quote(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    source_kind: Literal["batch", "task"],
    source_id: str,
) -> GenerationPriceQuote:
    if source_kind == "batch":
        source = conn.execute(
            "SELECT id, project_id, created_by_user_id, request_snapshot_json "
            "FROM generation_batches WHERE id = %s",
            (source_id,),
        ).fetchone()
    else:
        source = conn.execute(
            "SELECT task.id, task.batch_id, task.prompt_snapshot_json, "
            "batch.project_id, batch.created_by_user_id, batch.request_snapshot_json "
            "FROM generation_tasks AS task "
            "JOIN generation_batches AS batch ON batch.id = task.batch_id "
            "WHERE task.id = %s",
            (source_id,),
        ).fetchone()
    if source is None:
        raise generation_error(
            404, "BATCH_NOT_FOUND" if source_kind == "batch" else "TASK_NOT_FOUND", "原任务不存在。"
        )
    require_batch_access(
        conn,
        actor=actor,
        project_id=None if source["project_id"] is None else str(source["project_id"]),
        created_by_user_id=str(source["created_by_user_id"]),
        action=f"generation_{source_kind}.regeneration_quote",
    )
    if source_kind == "batch":
        tasks = conn.execute(
            "SELECT id, prompt_snapshot_json FROM generation_tasks "
            "WHERE batch_id = %s ORDER BY created_at, id",
            (source_id,),
        ).fetchall()
    else:
        tasks = [source]
    parameters = _regeneration_quote_parameters(
        actor_id=actor.id,
        operation_kind="MANUAL_BATCH_REGENERATE"
        if source_kind == "batch"
        else "TASK_PAID_REGENERATE",
        source_batch_id=source_id if source_kind == "batch" else str(source["batch_id"]),
        source_task_ids=[str(task["id"]) for task in tasks],
        request_snapshot=required_snapshot_text(source["request_snapshot_json"]),
        prompt_snapshots=[required_snapshot_text(task["prompt_snapshot_json"]) for task in tasks],
    )
    # 运维可代操作，但报价与实际预扣都必须使用原批次付费账号的钱包及折扣。
    return generation_price_quote(conn, user_id=str(source["created_by_user_id"]), **parameters)


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


def require_matching_idempotent_batch(row: BusinessRow, *, request_hash: str) -> None:
    if str(row["request_hash"]) != request_hash:
        raise generation_error(
            409,
            "IDEMPOTENCY_CONFLICT",
            "This idempotency key was already used for a different request.",
        )


def require_regeneration_provider_ready(
    conn: BusinessConnection,
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
            "H3_SETTINGS_UNAVAILABLE",
            "Save a readable H3 provider API key before queuing a real H3 task.",
        ) from exc
    require_cos_first_frame_storage(conn)


def require_task_paid_regeneration_state(row: BusinessRow) -> None:
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
    if str(row["status"]) == "SUCCEEDED" and str(row["archive_status"]) in {"DIRECT", "ARCHIVED"}:
        # A user's explicit paid retry is independent of optional historical QC.
        return
    quality_issue_codes = parse_json_list(row["quality_issue_codes"])
    if generation_quality_failed(str(row["quality_status"]), quality_issue_codes):
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
        "Only completed results or failed submitted tasks can be regenerated with a new paid call.",
    )


def paid_cost_per_task(total: float | None, *, quantity: int) -> float | None:
    if total is None:
        return None
    return round(total / quantity, 6)


def require_confirmed_first_frame(
    conn: BusinessConnection,
    *,
    project_id: str,
    first_frame_asset_id: str,
) -> None:
    selection = conn.execute(
        """
        SELECT id, payload_json, created_by_user_id
        FROM versions
        WHERE project_id = %s AND kind = 'first_frame_selection'
        ORDER BY version_number DESC
        LIMIT 1
        """,
        (project_id,),
    ).fetchone()
    latest_candidates = conn.execute(
        """
        SELECT id, payload_json
        FROM versions
        WHERE project_id = %s AND kind = 'first_frame_candidates'
        ORDER BY version_number DESC
        LIMIT 1
        """,
        (project_id,),
    ).fetchone()
    if selection is None or latest_candidates is None:
        raise generation_error(
            409,
            "FIRST_FRAME_CONFIRMATION_REQUIRED",
            "Confirm a first-frame candidate before compiling or submitting H3 generation.",
        )
    try:
        selection_payload = json.loads(str(selection["payload_json"]))
    except json.JSONDecodeError as exc:
        raise generation_error(
            409,
            "FIRST_FRAME_CONFIRMATION_REQUIRED",
            "Confirm a first-frame candidate before compiling or submitting H3 generation.",
        ) from exc
    # 历史版本放开：确认记录指向的候选版本才是这次确认的依据（可能不是最新
    # 版本）。候选内容、质检证据都从它读取；最新版本只用于判断“这次确认是否
    # 仍是最新”，b5 输入新鲜度检查只作用于最新确认。
    confirmed_candidates_id = (
        selection_payload.get("first_frame_candidates_version_id")
        if isinstance(selection_payload, dict)
        else None
    )
    confirmed_candidates = (
        conn.execute(
            """
            SELECT id, payload_json
            FROM versions
            WHERE id = %s AND project_id = %s AND kind = 'first_frame_candidates'
            """,
            (confirmed_candidates_id, project_id),
        ).fetchone()
        if isinstance(confirmed_candidates_id, str)
        else None
    )
    candidate_payload: object = None
    if confirmed_candidates is not None:
        try:
            candidate_payload = json.loads(str(confirmed_candidates["payload_json"]))
        except json.JSONDecodeError as exc:
            raise generation_error(
                409,
                "FIRST_FRAME_CONFIRMATION_REQUIRED",
                "Confirm a first-frame candidate before compiling or submitting H3 generation.",
            ) from exc
    candidate_values = (
        candidate_payload.get("candidates") if isinstance(candidate_payload, dict) else None
    )
    selected_candidate = (
        next(
            (
                value
                for value in candidate_values
                if isinstance(value, dict) and value.get("asset_id") == first_frame_asset_id
            ),
            None,
        )
        if isinstance(candidate_values, list)
        else None
    )
    if (
        selected_candidate is None
        or not isinstance(selection_payload, dict)
        or selection_payload.get("first_frame_asset_id") != first_frame_asset_id
    ):
        raise generation_error(
            409,
            "FIRST_FRAME_CONFIRMATION_REQUIRED",
            "Confirm a first-frame candidate from the latest candidate set before H3 generation.",
        )
    is_current_confirmation = confirmed_candidates is not None and str(
        latest_candidates["id"]
    ) == str(confirmed_candidates["id"])
    quality = selected_candidate.get("quality") if isinstance(selected_candidate, dict) else None
    quality_passed = isinstance(quality, dict) and quality.get("passed") is True
    quality_override = (
        isinstance(selection_payload, dict) and selection_payload.get("quality_override") is True
    )
    manual_review_confirmed = (
        isinstance(selection_payload, dict)
        and selection_payload.get("review_mode") == "HUMAN_CONFIRMATION"
        and bool(selection["created_by_user_id"])
        and selection_payload.get("reviewed_by_user_id") == selection["created_by_user_id"]
    )
    if not quality_passed and not quality_override and not manual_review_confirmed:
        raise generation_error(
            409,
            "FIRST_FRAME_QUALITY_NOT_VERIFIED",
            "The confirmed first frame has no current quality evidence; "
            "generate and confirm it again.",
        )
    if (
        is_current_confirmation
        and isinstance(candidate_payload, dict)
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
    conn: BusinessConnection,
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
            "H3_REQUIRES_CLOUD_STORAGE",
            "H3 generation requires an HTTPS first-frame URL; save COS settings before generating.",
        ) from exc


def run_next_generation_task(
    conn: BusinessConnection,
    *,
    worker_id: str,
    provider: H3Provider | None,
    storage: StorageAdapter,
    first_frame_storage: StorageAdapter | None = None,
    visual_quality_inspector: FirstFrameQualityInspector | None = None,
    video_frame_extractor: Callable[[bytes], list[ImageInput]] | None = None,
    lease: dict[str, Any] | None = None,
) -> TaskResult | None:
    if lease is None:
        lease = acquire_generation_task_lease(conn, worker_id=worker_id)
    if lease is None:
        return None
    attempt = lease.get("attempt")
    # 本任务内的第三方调用（提交、查询进度、下载）都记到这条视频任务名下（P0-9）。
    with external_call_context(
        "VIDEO", str(lease["id"]), attempt=None if attempt is None else int(attempt)
    ):
        return _run_leased_generation_task(
            conn,
            provider=provider,
            storage=storage,
            first_frame_storage=first_frame_storage,
            lease=lease,
        )


def _run_leased_generation_task(
    conn: BusinessConnection,
    *,
    provider: H3Provider | None,
    storage: StorageAdapter,
    first_frame_storage: StorageAdapter | None,
    lease: dict[str, Any],
) -> TaskResult | None:
    task_id = str(lease["id"])
    source_storage = first_frame_storage or storage
    archive_retry = bool(
        lease.get("archive_status") == "ARCHIVE_FAILED" and lease.get("provider_result_url")
    )
    if archive_retry:
        return _retry_archive(conn, lease=lease)
    if str(lease["provider"]) == "metaso" and source_storage.provider != "cos":
        mark_task_provider_settings_unavailable(
            conn,
            lease=lease,
        )
        return get_task_result(conn, task_id)
    if provider is None:
        try:
            provider = h3_provider_for_task(conn, str(lease["provider"]), task_id=str(lease["id"]))
        except H3ProviderSettingsUnavailable:
            mark_task_provider_settings_unavailable(
                conn,
                lease=lease,
            )
            return get_task_result(conn, task_id)
    if isinstance(provider, MetasoH3Provider) and provider.task_created_observer is None:

        def _record_created_provider_task(created_provider_task_id: str) -> None:
            # Persist the paid provider task id the moment it exists so a later
            # timeout/crash can be reconciled instead of losing the result.
            mark_task_submission_uncertain(
                conn,
                task_id=task_id,
                message="The H3 task was created; the result is still being awaited.",
                provider_task_id=created_provider_task_id,
            )

        provider.task_created_observer = _record_created_provider_task
    try:
        first_frame_uri = lease.get("first_frame_uri")
        last_frame_uri = lease.get("last_frame_uri")

        def _signed_url(storage_uri: str) -> str:
            ref = storage_object_ref_from_uri(storage_uri)
            require_storage_match(source_storage, ref)
            return source_storage.create_download_intent(
                ref.key,
                expires_in=FIRST_FRAME_URL_EXPIRES_IN,
                can_read=True,
            ).url

        first_frame_url = (
            _signed_url(str(first_frame_uri))
            if isinstance(first_frame_uri, str) and first_frame_uri
            else None
        )
        last_frame_url = (
            _signed_url(str(last_frame_uri))
            if isinstance(last_frame_uri, str) and last_frame_uri
            else None
        )
        reference_images = [
            {"name": str(image.get("name", "")), "url": _signed_url(str(image["uri"]))}
            for image in (lease.get("reference_images") or [])
            if isinstance(image, dict) and image.get("uri")
        ]
        reference_videos = [
            {"url": _signed_url(str(video["uri"]))}
            for video in (lease.get("reference_videos") or [])
            if isinstance(video, dict) and video.get("uri")
        ]
        reference_audios = [
            {"url": _signed_url(str(audio["uri"]))}
            for audio in (lease.get("reference_audios") or [])
            if isinstance(audio, dict) and audio.get("uri")
        ]
    except (StorageBackendUnavailable, StoragePermissionError, ValueError):
        mark_task_first_frame_url_sign_failed(
            conn,
            lease=lease,
        )
        return get_task_result(conn, task_id)
    provider_request = build_h3_request(
        prompt_text=str(lease["prompt_text"]),
        first_frame_url=first_frame_url,
        last_frame_url=last_frame_url,
        reference_images=reference_images,
        reference_videos=reference_videos,
        reference_audios=reference_audios,
        duration_seconds=int(lease["output_duration_seconds"]),
        resolution=str(lease["resolution"]),
        ratio=str(lease["ratio"]),
    )
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
                message="The H3 task needs recovery after a nonterminal provider failure.",
                provider_task_id=exc.provider_task_id,
            )
            return get_task_result(conn, task_id)
        mark_task_provider_failed(
            conn,
            lease=lease,
            provider_task_id=exc.provider_task_id,
            provider_reason=exc.provider_reason,
            submission_rejected=is_submission_rejection(exc),
        )
        return get_task_result(conn, task_id)

    # Keep a durable URL checkpoint; recovery must never repeat the paid call.
    with conn:
        mark_generation_task_running(
            conn,
            lease=lease,
            provider_task_id=provider_result.provider_task_id,
            provider_request=provider_request,
            release_lease=False,
        )
        mark_generation_task_archiving(conn, lease=lease, result_url=provider_result.result_url)
        return finalize_generation_direct_result(
            conn,
            lease=lease,
            quality_status="NOT_REQUIRED",
            quality_issue_codes=[],
            output_seconds=provider_result.output_seconds,
        )


def _retry_archive(
    conn: BusinessConnection,
    *,
    lease: dict[str, Any],
) -> TaskResult:
    """Finish a saved paid result without downloading or processing its media."""
    task_id = str(lease["id"])
    batch_id = str(lease["batch_id"])
    row = conn.execute(
        """
        SELECT generation_tasks.provider_result_url
        FROM generation_tasks
        WHERE generation_tasks.id = %s
        """,
        (task_id,),
    ).fetchone()
    if row is None or not row["provider_result_url"]:
        return get_task_result(conn, task_id)
    result_url = str(row["provider_result_url"])
    try:
        with conn:
            mark_generation_task_archiving(conn, lease=lease, result_url=result_url)
            return finalize_generation_direct_result(
                conn,
                lease=lease,
                quality_status="NOT_REQUIRED",
                quality_issue_codes=[],
            )
    except Exception as exc:
        logger.warning(
            "direct result retry finalization failed for task %s: %s", task_id, type(exc).__name__
        )
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
    conn: BusinessConnection,
    *,
    actor_id: str,
    task_id: str,
    action: str,
    idempotency_key: str,
    request_hash: str,
) -> BusinessRow | None:
    row = conn.execute(
        """
        SELECT request_hash, result_status
        FROM generation_task_operations
        WHERE actor_user_id = %s AND task_id = %s AND action = %s AND idempotency_key = %s
        """,
        (actor_id, task_id, action, idempotency_key),
    ).fetchone()
    if row is not None and str(row["request_hash"]) != request_hash:
        raise generation_error(
            409,
            "IDEMPOTENCY_CONFLICT",
            "This idempotency key was already used for a different task operation.",
        )
    return cast(BusinessRow | None, row)


def _record_completed_task_operation(
    conn: BusinessConnection,
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
        VALUES (%s, %s, %s, %s, %s, %s, %s, 'COMPLETED', %s)
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
    conn: BusinessConnection,
    *,
    task_id: str,
    batch_id: str,
    project_id: str,
    actor: CurrentUser,
) -> None:
    row = conn.execute(
        # The reservation window is a module constant, so it is spelled as a
        # literal interval in the SQL (PG-canonical; the SQLite translator
        # rewrites it to datetime('now', '-<n> seconds')). A parameterised
        # SQLite modifier would carry opposite signs on the two lanes.
        f"""
        SELECT id, idempotency_key
        FROM generation_task_operations
        WHERE task_id = %s AND action = 'RECONCILE' AND result_status = 'PENDING'
          AND updated_at::timestamptz <=
              now() - interval '{RECONCILIATION_RESERVATION_SECONDS} seconds'
        """,
        (task_id,),
    ).fetchone()
    if row is None:
        return
    conn.execute(
        """
        DELETE FROM generation_task_operations
        WHERE id = %s AND result_status = 'PENDING'
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
    conn: BusinessConnection,
    *,
    reservation: ReconcileReservation | None,
) -> None:
    if reservation is None:
        return
    with conn:
        _renew_reconcile_reservation_in_transaction(conn, reservation_id=reservation.id)


def _renew_reconcile_reservation_in_transaction(
    conn: BusinessConnection,
    *,
    reservation_id: str,
) -> None:
    cursor = conn.execute(
        """
        UPDATE generation_task_operations
        SET updated_at = CURRENT_TIMESTAMP
        WHERE id = %s AND action = 'RECONCILE' AND result_status = 'PENDING'
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
    conn: BusinessConnection,
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
            response_snapshot_json = %s,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = %s AND actor_user_id = %s AND task_id = %s
          AND action = 'RECONCILE' AND idempotency_key = %s AND result_status = 'PENDING'
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
            "generation_task.reconcile_direct"
            if result.archive_status == "DIRECT"
            else "generation_task.reconcile_archived"
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


def _pre_provider_retry_pricing_snapshot(
    conn: BusinessConnection,
    *,
    user_id: str,
    task_id: str,
    seconds: int,
    prompt_snapshot: str,
) -> dict[str, Any] | None:
    from decimal import InvalidOperation

    from app.billing_catalog import amount, credits_from_snapshot, generation_service_for

    try:
        payload = json.loads(prompt_snapshot)
        if not isinstance(payload, dict):
            raise ValueError("原任务冻结参数无效")
        service = generation_service_for(str(payload.get("resolution", "768P")), seconds)
        previous = conn.execute(
            "SELECT user_id, service, state, budget_units, reserved_credits, charged_credits, "
            "pricing_snapshot_json FROM billing_operations "
            "WHERE source_id=%s AND user_id=%s AND service=%s "
            "ORDER BY billing_round DESC LIMIT 1 FOR UPDATE",
            (task_id, user_id, service),
        ).fetchone()
        if previous is not None and previous["state"] == "PENDING":
            # 原预扣仍有效时，由账务幂等直接回用该轮次，不产生新报价或新预扣。
            return None
        if previous is None or previous["state"] not in {"FAILED", "CANCELLED"}:
            raise ValueError("缺少已释放的原账务记录")
        snapshot = json.loads(str(previous["pricing_snapshot_json"]))
        if not isinstance(snapshot, dict):
            raise ValueError("原账务快照无效")
        credits = credits_from_snapshot(snapshot, seconds)
        if (
            snapshot["service"] != service
            or snapshot["unit"] != "second"
            or previous["user_id"] != user_id
            or amount(previous["budget_units"]) != amount(seconds)
            or amount(snapshot["units"]) != amount(seconds)
            or int(str(snapshot["credits"])) != credits
            or int(str(previous["reserved_credits"])) != credits
            or int(str(previous["charged_credits"])) != 0
        ):
            raise ValueError("原账务用量与冻结任务不一致")
        if credits and (
            snapshot.get("quote_actor_user_id") != user_id
            or not snapshot.get("quote_wallet_owner_id")
            or int(str(snapshot["quote_quantity"])) < 1
            or credits * int(str(snapshot["quote_quantity"]))
            > int(str(snapshot["quote_max_credits"]))
            or not isinstance(snapshot.get("quote_expires_at"), int)
        ):
            raise ValueError("缺少可核验的原报价上限")
    except (ValueError, TypeError, KeyError, InvalidOperation) as exc:
        raise generation_error(
            409,
            "QUOTE_REQUIRED",
            "原任务缺少可核验的已确认报价，请获取新报价并选择付费重新生成。",
        ) from exc
    # 已受理报价的有效期只管首次受理；安全重试沿用原单价和上限，不能静默取现价。
    return snapshot


def retry_generation_task(
    conn: BusinessConnection,
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

        # 先锁任务再锁原账务，保持取消/完成路径的锁序，并冻结重试所依据的轮次状态。
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
                task.superseded_by_task_id,
                task.prompt_snapshot_json
            FROM generation_tasks AS task
            JOIN generation_batches AS batch ON batch.id = task.batch_id
            WHERE task.id = %s
            FOR UPDATE OF task
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
            if status != "SUCCEEDED":
                raise generation_error(
                    409,
                    "TASK_RETRY_NOT_ALLOWED",
                    "The saved result is already being recovered; refresh before retrying.",
                )
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
            updated = conn.execute(
                """
                UPDATE generation_tasks
                SET
                    status = 'SUCCEEDED',
                    locked_by = NULL,
                    locked_until = NULL,
                    next_poll_at = CURRENT_TIMESTAMP,
                    retry_reason = %s,
                    retry_requested_by_user_id = %s,
                    retry_requested_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = %s
                  AND status = 'SUCCEEDED'
                  AND archive_status = 'ARCHIVE_FAILED'
                  AND locked_by IS NULL AND locked_until IS NULL
                  AND provider_result_url = %s
                  AND archive_retry_count < %s
                  AND superseded_by_task_id IS NULL
                """,
                (request.retry_reason, actor.id, task_id, provider_result_url, MAX_ARCHIVE_RETRIES),
            )
            # Queue acquisition commits separately from the initial read.
            # Never clear a recovery lease or reopen a concurrently finished result.
            if updated.rowcount != 1:
                raise generation_error(
                    409,
                    "TASK_RETRY_NOT_ALLOWED",
                    "The saved result changed or is being recovered; refresh before retrying.",
                )
        elif quality_status in {"AUDIO_QUALITY_FAILED", "VISUAL_QUALITY_FAILED"}:
            raise generation_error(
                409,
                "REQUIRES_PAID_REGENERATION",
                "Quality failures require an explicit paid video regeneration.",
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
            retry_seconds = _seconds_from_prompt_snapshot(row["prompt_snapshot_json"])
            retry_pricing = _pre_provider_retry_pricing_snapshot(
                conn,
                user_id=str(row["created_by_user_id"]),
                task_id=task_id,
                seconds=retry_seconds,
                prompt_snapshot=str(row["prompt_snapshot_json"]),
            )
            conn.execute(
                """
                UPDATE generation_tasks
                SET billed_seconds = COALESCE(billed_seconds, %s)
                WHERE id = %s
                """,
                (retry_seconds, task_id),
            )
            _reserve_generation_credit(
                conn,
                user_id=str(row["created_by_user_id"]),
                task_id=task_id,
                billing_round=None,
                seconds=retry_seconds,
                pricing_snapshot=retry_pricing,
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
                    retry_reason = %s,
                    retry_requested_by_user_id = %s,
                    retry_requested_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = %s
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
                "billed_user_id": str(row["created_by_user_id"]),
                "requested_by_user_id": actor.id,
            },
        )
        _refresh_batch_status_in_transaction(conn, batch_id=str(row["batch_id"]))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return get_task_result(conn, task_id)


def confirm_generation_task_not_charged(
    conn: BusinessConnection,
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
            WHERE task.id = %s
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
                retry_reason = %s,
                retry_requested_by_user_id = %s,
                retry_requested_at = CURRENT_TIMESTAMP,
                billing_confirmation_status = 'CONFIRMED_NOT_CHARGED',
                billing_confirmed_by_user_id = %s,
                billing_confirmed_at = CURRENT_TIMESTAMP,
                billing_confirmation_reason = %s,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
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


def enqueue_generation_reconcile_operation(
    conn: BusinessConnection,
    *,
    task_id: str,
    batch_id: str,
    project_id: str,
    actor: CurrentUser,
    request: ReconcileGenerationTaskRequest,
) -> BusinessRow:
    action = "RECONCILE"
    request_hash = generation_task_operation_hash(
        action=action,
        task_id=task_id,
        payload={},
    )
    row = conn.execute(
        """
        SELECT status, provider_task_id, superseded_by_task_id
        FROM generation_tasks WHERE id = %s
        """,
        (task_id,),
    ).fetchone()
    if row is None:
        conn.rollback()
        raise generation_error(404, "TASK_NOT_FOUND", "Generation task does not exist.")
    if row["superseded_by_task_id"] is not None:
        conn.rollback()
        raise generation_error(
            409,
            "TASK_SUPERSEDED",
            "This historical task has already been replaced.",
        )
    existing = conn.execute(
        """
        SELECT * FROM generation_task_operations
        WHERE actor_user_id = %s AND task_id = %s AND action = %s
          AND idempotency_key = %s
        """,
        (actor.id, task_id, action, request.idempotency_key),
    ).fetchone()
    if existing is not None:
        if str(existing["request_hash"]) != request_hash:
            conn.rollback()
            raise generation_error(
                409,
                "IDEMPOTENCY_CONFLICT",
                "This idempotency key was already used for a different task operation.",
            )
        conn.commit()
        return cast(BusinessRow, existing)
    active = conn.execute(
        """
        SELECT * FROM generation_task_operations
        WHERE task_id = %s AND action = 'RECONCILE' AND result_status = 'PENDING'
        ORDER BY created_at DESC, id DESC LIMIT 1
        """,
        (task_id,),
    ).fetchone()
    if active is not None:
        conn.commit()
        return cast(BusinessRow, active)
    if str(row["status"]) != "SUBMISSION_UNCERTAIN":
        conn.rollback()
        raise generation_error(
            409,
            "TASK_NOT_UNCERTAIN",
            "Only SUBMISSION_UNCERTAIN tasks can be reconciled.",
        )
    if optional_text(row["provider_task_id"]) is None:
        conn.rollback()
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
        ) VALUES (%s, %s, %s, 'RECONCILE', %s, %s, %s, 'PENDING')
        ON CONFLICT DO NOTHING
        """,
        (
            operation_id,
            task_id,
            actor.id,
            request.idempotency_key,
            request_hash,
            task_id,
        ),
    )
    operation = conn.execute(
        "SELECT * FROM generation_task_operations WHERE id = %s",
        (operation_id,),
    ).fetchone()
    if operation is None:
        operation = conn.execute(
            """
            SELECT * FROM generation_task_operations
            WHERE task_id = %s AND action = 'RECONCILE' AND result_status = 'PENDING'
            ORDER BY created_at DESC, id DESC LIMIT 1
            """,
            (task_id,),
        ).fetchone()
    if operation is None:
        conn.rollback()
        raise generation_error(
            409,
            "RECONCILE_IN_PROGRESS",
            "A reconciliation request is already in progress for this task.",
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
            "operation_id": str(operation["id"]),
            "idempotency_key_hash": content_hash(request.idempotency_key),
            "provider_task_id_tail": redacted_provider_task_tail(
                optional_text(row["provider_task_id"])
            ),
        },
    )
    conn.commit()
    return cast(BusinessRow, operation)


def acquire_generation_reconcile_operation(
    conn: BusinessConnection,
    *,
    worker_id: str,
) -> ReconcileOperationLease | None:
    now = _timestamp_text(datetime.now(UTC))
    locked_until = _timestamp_text(
        datetime.now(UTC) + timedelta(minutes=RECONCILE_OPERATION_LEASE_MINUTES)
    )
    conn.execute(
        """
        UPDATE generation_task_operations
        SET locked_by = NULL, locked_until = NULL, updated_at = %s
        WHERE action = 'RECONCILE' AND result_status = 'PENDING'
          AND locked_until IS NOT NULL AND locked_until <= %s
        """,
        (now, now),
    )
    row = conn.execute(
        """
        UPDATE generation_task_operations
        SET locked_by = %s, locked_until = %s, attempt = attempt + 1,
            started_at = COALESCE(started_at, %s), updated_at = %s,
            error_code = NULL, error_message_redacted = NULL, retryable = 0
        WHERE id = (
            SELECT id FROM generation_task_operations
            WHERE action = 'RECONCILE' AND result_status = 'PENDING'
              AND locked_by IS NULL
            ORDER BY created_at, id LIMIT 1 FOR UPDATE SKIP LOCKED
        ) AND action = 'RECONCILE' AND result_status = 'PENDING'
          AND locked_by IS NULL
        RETURNING *
        """,
        (worker_id, locked_until, now, now),
    ).fetchone()
    conn.commit()
    if row is None:
        return None
    actor_user_id = optional_text(row["actor_user_id"])
    if actor_user_id is None:
        conn.execute(
            """
            UPDATE generation_task_operations
            SET result_status = 'FAILED', error_code = 'RECONCILE_ACTOR_UNAVAILABLE',
                error_message_redacted = 'The reconciliation actor is unavailable.',
                retryable = 0, locked_by = NULL, locked_until = NULL,
                completed_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
            WHERE id = %s AND locked_by = %s AND locked_until = %s
              AND attempt = %s
            """,
            (
                str(row["id"]),
                worker_id,
                str(row["locked_until"]),
                int(row["attempt"]),
            ),
        )
        conn.commit()
        return None
    return ReconcileOperationLease(
        id=str(row["id"]),
        task_id=str(row["task_id"]),
        actor_user_id=actor_user_id,
        idempotency_key=str(row["idempotency_key"]),
        worker_id=worker_id,
        locked_until=str(row["locked_until"]),
        attempt=int(row["attempt"]),
    )


def prepare_generation_reconcile_operation(
    conn: BusinessConnection,
    *,
    lease: ReconcileOperationLease,
    provider_factory: Callable[[BusinessConnection, str], H3Provider],
) -> PreparedReconcileOperation:
    _require_owned_reconcile_operation(conn, lease)
    row = conn.execute(
        """
        SELECT
            task.status,
            task.provider_task_id,
            task.provider,
            task.prompt_snapshot_json,
            batch.id AS batch_id,
            batch.project_id,
            batch.created_by_user_id,
            users.username,
            users.display_name,
            users.role
        FROM generation_tasks AS task
        JOIN generation_batches AS batch ON batch.id = task.batch_id
        JOIN users ON users.id = %s
        WHERE task.id = %s
        """,
        (lease.actor_user_id, lease.task_id),
    ).fetchone()
    if row is None:
        raise RuntimeError("reconciliation task context is unavailable")
    task_status = str(row["status"])
    prompt_snapshot = json.loads(str(row["prompt_snapshot_json"]))
    provider_task_id = optional_text(row["provider_task_id"])
    provider: H3Provider | None = None
    if task_status == "SUBMISSION_UNCERTAIN":
        if provider_task_id is None:
            raise generation_error(
                409,
                "SUBMISSION_REQUIRES_MANUAL_CONFIRMATION",
                "There is no provider task id; an admin must confirm no charge occurred.",
            )
        provider = provider_factory(conn, str(row["provider"]))
    return PreparedReconcileOperation(
        lease=lease,
        actor=CurrentUser(
            id=lease.actor_user_id,
            username=str(row["username"]),
            display_name=str(row["display_name"]),
            role=cast(Role, str(row["role"])),
        ),
        batch_id=str(row["batch_id"]),
        project_id=str(row["project_id"]),
        created_by_user_id=str(row["created_by_user_id"]),
        task_status=task_status,
        provider_task_id=provider_task_id,
        provider=provider,
        first_frame_uri=str(prompt_snapshot.get("first_frame_uri") or ""),
    )


def perform_generation_reconcile_operation(
    work: PreparedReconcileOperation,
    *,
    storage: StorageAdapter,
    first_frame_storage: StorageAdapter | None = None,
    visual_quality_inspector: FirstFrameQualityInspector | None = None,
    video_frame_extractor: Callable[[bytes], list[ImageInput]] | None = None,
) -> ReconcileOperationOutcome:
    if work.task_status != "SUBMISSION_UNCERTAIN":
        return ReconcileOperationOutcome(status="ALREADY_TERMINAL")
    if work.provider is None or work.provider_task_id is None:
        raise RuntimeError("reconciliation provider context is unavailable")
    try:
        with external_call_context("VIDEO", work.lease.task_id):
            query = work.provider.query_image_to_video(work.provider_task_id)
    except H3ProviderFailed as exc:
        raise generation_error(
            502,
            "PROVIDER_QUERY_FAILED",
            "Provider query failed during reconciliation.",
        ) from exc
    if query.status == "RUNNING":
        raise generation_error(
            409,
            "PROVIDER_STILL_PROCESSING",
            "Provider reports the task is still running.",
        )
    if query.status in {"FAILED", "CANCELLED"}:
        return ReconcileOperationOutcome(status=query.status)
    if query.result_url is None:
        raise generation_error(
            502,
            "PROVIDER_RESULT_URL_MISSING",
            "Provider reports success without a result URL.",
        )
    return ReconcileOperationOutcome(
        status="SUCCEEDED",
        result_url=query.result_url,
        audio_quality_status="NOT_REQUIRED",
        quality_issue_codes=[],
        output_seconds=query.output_seconds,
    )


def complete_generation_reconcile_operation(
    conn: BusinessConnection,
    *,
    work: PreparedReconcileOperation,
    outcome: ReconcileOperationOutcome,
) -> TaskResult:
    lease = work.lease
    with conn:
        _require_owned_reconcile_operation(conn, lease)
        if outcome.status == "ALREADY_TERMINAL":
            result = get_task_result(conn, lease.task_id)
        elif outcome.status in {"FAILED", "CANCELLED"}:
            task_update = conn.execute(
                """
                UPDATE generation_tasks
                SET status = 'FAILED', error_code = 'PROVIDER_TERMINAL',
                    error_message_redacted = %s,
                    locked_by = NULL, locked_until = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = %s AND status = 'SUBMISSION_UNCERTAIN'
                  AND superseded_by_task_id IS NULL
                """,
                (
                    f"服务商报告任务已结束，状态：{outcome.status.lower()}",
                    lease.task_id,
                ),
            )
            if task_update.rowcount != 1:
                raise _reconcile_reservation_lost()
            record_video_generation_cost(
                conn,
                task_id=lease.task_id,
                output_seconds=None,
            )
            finalize_internal_billing(
                conn,
                task_id=lease.task_id,
                outcome="cancelled" if outcome.status == "CANCELLED" else "failed",
            )
            _refresh_batch_status_in_transaction(conn, batch_id=work.batch_id)
            result = get_task_result(conn, lease.task_id)
        else:
            if outcome.result_url is None or outcome.audio_quality_status is None:
                raise RuntimeError("reconciled direct result is unavailable")
            task_update = conn.execute(
                """
                UPDATE generation_tasks
                SET status = 'SUCCEEDED', archive_status = 'DIRECT',
                    quality_status = %s, quality_issue_codes = %s,
                    provider_result_url = %s, result_asset_id = NULL, error_code = NULL,
                    error_message_redacted = NULL, locked_by = NULL,
                    locked_until = NULL, completed_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = %s AND status = 'SUBMISSION_UNCERTAIN'
                  AND result_asset_id IS NULL
                  AND superseded_by_task_id IS NULL
                """,
                (
                    outcome.audio_quality_status,
                    json.dumps(outcome.quality_issue_codes or [], ensure_ascii=True),
                    outcome.result_url,
                    lease.task_id,
                ),
            )
            if task_update.rowcount != 1:
                raise _reconcile_reservation_lost()
            record_video_generation_cost(
                conn,
                task_id=lease.task_id,
                output_seconds=outcome.output_seconds,
            )
            finalize_internal_billing(conn, task_id=lease.task_id, outcome="success")
            _refresh_batch_status_in_transaction(conn, batch_id=work.batch_id)
            result = get_task_result(conn, lease.task_id)
        operation_update = conn.execute(
            """
            UPDATE generation_task_operations
            SET result_status = 'COMPLETED', response_snapshot_json = %s,
                locked_by = NULL, locked_until = NULL, retryable = 0,
                completed_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
            WHERE id = %s AND action = 'RECONCILE' AND result_status = 'PENDING'
              AND locked_by = %s AND locked_until = %s AND attempt = %s
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
                lease.id,
                lease.worker_id,
                lease.locked_until,
                lease.attempt,
            ),
        )
        if operation_update.rowcount != 1:
            raise _reconcile_reservation_lost()
        insert_audit(
            conn,
            actor=work.actor,
            action=(
                "generation_task.reconcile_direct"
                if result.archive_status == "DIRECT"
                else "generation_task.reconcile_terminal_failed"
            ),
            entity_type="generation_task",
            entity_id=lease.task_id,
            metadata={
                "batch_id": work.batch_id,
                "project_id": work.project_id,
                "operation_id": lease.id,
                "status": result.status,
                "archive_status": result.archive_status,
            },
        )
    return result


def fail_generation_reconcile_operation(
    conn: BusinessConnection,
    *,
    lease: ReconcileOperationLease,
    cause: Exception,
) -> None:
    code = "RECONCILE_OPERATION_FAILED"
    message = "任务对账失败，请稍后重试。"
    retryable = True
    if isinstance(cause, HTTPException):
        detail: dict[str, Any] = cause.detail if isinstance(cause.detail, dict) else {}
        code = str(detail.get("code") or code)
        message = str(detail.get("message") or message)
        retryable = cause.status_code in {409, 429, 502, 503, 504}
    conn.execute(
        """
        UPDATE generation_task_operations
        SET result_status = 'FAILED', error_code = %s,
            error_message_redacted = %s, retryable = %s,
            locked_by = NULL, locked_until = NULL,
            completed_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
        WHERE id = %s AND action = 'RECONCILE' AND result_status = 'PENDING'
          AND locked_by = %s AND locked_until = %s AND attempt = %s
        """,
        (
            code,
            message,
            1 if retryable else 0,
            lease.id,
            lease.worker_id,
            lease.locked_until,
            lease.attempt,
        ),
    )
    conn.commit()


def load_generation_reconcile_operation(
    conn: BusinessConnection,
    operation_id: str,
) -> BusinessRow:
    row = conn.execute(
        """
        SELECT operation.*, batch.project_id
        FROM generation_task_operations AS operation
        JOIN generation_tasks AS task ON task.id = operation.task_id
        JOIN generation_batches AS batch ON batch.id = task.batch_id
        WHERE operation.id = %s AND operation.action = 'RECONCILE'
        """,
        (operation_id,),
    ).fetchone()
    if row is None:
        raise generation_error(404, "RECONCILE_OPERATION_NOT_FOUND", "对账任务不存在。")
    return cast(BusinessRow, row)


def latest_generation_reconcile_operation(
    conn: BusinessConnection,
    *,
    task_id: str,
) -> BusinessRow | None:
    return cast(
        BusinessRow | None,
        conn.execute(
            """
            SELECT operation.*, batch.project_id
            FROM generation_task_operations AS operation
            JOIN generation_tasks AS task ON task.id = operation.task_id
            JOIN generation_batches AS batch ON batch.id = task.batch_id
            WHERE operation.task_id = %s AND operation.action = 'RECONCILE'
            ORDER BY CASE WHEN operation.result_status = 'PENDING' THEN 0 ELSE 1 END,
                     operation.created_at DESC, operation.id DESC LIMIT 1
            """,
            (task_id,),
        ).fetchone(),
    )


def _require_owned_reconcile_operation(
    conn: BusinessConnection,
    lease: ReconcileOperationLease,
) -> BusinessRow:
    row = conn.execute(
        "SELECT * FROM generation_task_operations WHERE id = %s",
        (lease.id,),
    ).fetchone()
    if (
        row is None
        or str(row["result_status"]) != "PENDING"
        or str(row["locked_by"]) != lease.worker_id
        or str(row["locked_until"]) != lease.locked_until
        or int(row["attempt"]) != lease.attempt
    ):
        raise _reconcile_reservation_lost()
    return cast(BusinessRow, row)


def _timestamp_text(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S")


def reconcile_generation_task(
    conn: BusinessConnection,
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
        row = conn.execute(
            """
            SELECT
                status,
                archive_status,
                result_asset_id,
                provider_task_id,
                superseded_by_task_id
            FROM generation_tasks
            WHERE id = %s
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
                        response_snapshot_json = %s,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE actor_user_id = %s AND task_id = %s AND action = %s
                      AND idempotency_key = %s
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
                        "generation_task.reconcile_direct"
                        if str(row["archive_status"]) == "DIRECT"
                        else "generation_task.reconcile_archived"
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
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'PENDING')
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
    except IntegrityConstraintError as exc:
        conn.rollback()
        # 鉴别闸（r3 P1）：只有对账互斥唯一索引的冲突才是「已有对账在跑」；
        # 其他 235xx 是真实数据错误，必须原样冒出。
        if not is_unique_conflict_on(exc, "billing_reconciliation_open_key"):
            raise
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
                WHERE id = %s AND result_status = 'PENDING'
                """,
                (operation_id,),
            )
        raise
    return result


def reconcile_submission_uncertain_task(
    conn: BusinessConnection,
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

    If the provider task id is known, query it and retain its URL when it
    succeeded, or mark it terminal on an explicit provider failure. When the
    create response was lost (no provider task id), refuse to guess: a human
    must first confirm the charge did not occur before any resubmission.
    """
    row = conn.execute(
        "SELECT status, provider_task_id FROM generation_tasks WHERE id = %s",
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
        with external_call_context("VIDEO", task_id):
            item = provider._query_task(str(provider_task_id))
    except H3ProviderFailed as exc:
        raise generation_error(
            502, "PROVIDER_QUERY_FAILED", "Provider query failed during reconciliation."
        ) from exc
    _renew_reconcile_reservation(conn, reservation=reconcile_reservation)
    status = item.get("status")
    if status == "succeeded":
        result_url = _metaso_content_url(item, provider_task_id=str(provider_task_id))
        _renew_reconcile_reservation(conn, reservation=reconcile_reservation)
        task_state_guard = (
            "AND status = 'SUBMISSION_UNCERTAIN' AND result_asset_id IS NULL"
            if reconcile_reservation is not None
            else ""
        )
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
                    status = 'SUCCEEDED',
                    archive_status = 'DIRECT',
                    provider_result_url = %s,
                    quality_status = %s,
                    quality_issue_codes = %s,
                    result_asset_id = NULL,
                    error_code = NULL,
                    error_message_redacted = NULL,
                    locked_by = NULL,
                    locked_until = NULL,
                    completed_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = %s
                {task_state_guard}
                """,
                (
                    result_url,
                    "NOT_REQUIRED",
                    "[]",
                    task_id,
                ),
            )
            if reconcile_reservation is not None and task_update.rowcount != 1:
                raise _reconcile_reservation_lost()
            record_video_generation_cost(
                conn,
                task_id=task_id,
                output_seconds=_metaso_output_seconds(item),
            )
            finalize_internal_billing(conn, task_id=task_id, outcome="success")
            _refresh_batch_status_in_transaction(conn, batch_id=batch_id)
            if reconcile_reservation is None:
                release_user_queue_slot_for_task(conn, task_id=task_id)
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
                    error_message_redacted = '服务商报告任务已结束，状态：' || %s,
                    locked_by = NULL,
                    locked_until = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = %s
                {"AND status = 'SUBMISSION_UNCERTAIN'" if reconcile_reservation is not None else ""}
                """,
                (str(status), task_id),
            )
            if reconcile_reservation is not None and task_update.rowcount != 1:
                raise _reconcile_reservation_lost()
            record_video_generation_cost(conn, task_id=task_id, output_seconds=None)
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


def _release_archive_retry(
    conn: BusinessConnection,
    *,
    task_id: str,
    batch_id: str,
    quality_status: str | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
) -> None:
    """Release the lease and back off ~60s so a stuck provider/storage does not
    cause a hot retry loop. Once retries are exhausted, the task is failed so a
    permanently expired provider URL does not spin forever."""
    with conn:
        row = conn.execute(
            "SELECT archive_retry_count FROM generation_tasks WHERE id = %s", (task_id,)
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
                    archive_retry_count = %s,
                    quality_status = COALESCE(%s, quality_status),
                    error_code = 'ARCHIVE_RETRY_EXHAUSTED',
                    error_message_redacted =
                        'Archive retries exhausted; the provider URL may have expired.',
                    locked_by = NULL,
                    locked_until = NULL,
                    next_poll_at = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = %s
                """,
                (next_count, quality_status, task_id),
            )
            finalize_internal_billing(conn, task_id=task_id, outcome="failed")
            _refresh_batch_status_in_transaction(conn, batch_id=batch_id)
            release_user_queue_slot_for_task(conn, task_id=task_id)
            return
        conn.execute(
            """
            UPDATE generation_tasks
            SET
                status = 'SUCCEEDED',
                archive_status = 'ARCHIVE_FAILED',
                archive_retry_count = %s,
                quality_status = COALESCE(%s, quality_status),
                error_code = COALESCE(%s, error_code),
                error_message_redacted = COALESCE(%s, error_message_redacted),
                locked_by = NULL,
                locked_until = NULL,
                next_poll_at = now() + interval '60 seconds',
                updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
            """,
            (next_count, quality_status, error_code, error_message, task_id),
        )
        # The retry is no longer running; the slot is free until a worker
        # picks the archive retry up again.
        release_user_queue_slot_for_task(conn, task_id=task_id)


# ---------------------------------------------------------------------------
# T25 fair queue — patterns A-E of the revised T24 ADR (pseudo-SQL §2)
# ---------------------------------------------------------------------------

# Per-user queue-empty retry budget: when the first idle user has no eligible
# task (their only one is locked by another worker), rotate to the next idle
# user instead of giving up (pseudo-SQL §2 Pattern A, outer loop).
_FAIR_QUEUE_MAX_ROUNDS = 8


def _fair_queue_enabled(conn: BusinessConnection) -> bool:
    """Pattern A gate: the runtime_settings.fair_queue_enabled switch.

    The 030 migration is PostgreSQL-only, so the desktop SQLite lane has no
    such column and keeps the legacy global FIFO — the absence of the switch
    is the feature being off (revised ADR §4), not a degraded state.
    """
    try:
        row = conn.execute(
            "SELECT fair_queue_enabled FROM runtime_settings WHERE id = 1"
        ).fetchone()
    except psycopg.errors.UndefinedColumn:
        # Missing column is the documented "feature off" state (the desktop
        # SQLite lane has no such column; a PG database that never ran 041).
        return False
    except Exception as exc:
        # Any other failure is a real fault and must fail loudly instead of
        # silently degrading to global FIFO (M5 review P1-7). The message
        # sniff below only keeps translated driver wrappers honest.
        message = f"{type(exc).__name__}: {exc}"
        if "column" in message and ("does not exist" in message or "no such column" in message):
            return False
        logger.warning("fair_queue_enabled probe failed: %s", message)
        raise
    return bool(row[0]) if row is not None else False


def ensure_user_queue_cursor(conn: BusinessConnection, *, user_id: str) -> None:
    """Pattern D: cold start — make a user schedulable when they create work.

    Idempotent by ON CONFLICT DO NOTHING; called inside the same transaction
    as the task creation (race guard: two creators never duplicate the row).

    Runs on PostgreSQL regardless of the fair_queue_enabled switch: cursor
    rows must exist and stay accurate before the rollout flag is flipped, or
    users who queued work during the observation window stay invisible when
    the switch turns on (M5 review P1-4). The SQLite lane has no such table.
    """
    if not conn.is_postgres:
        return
    conn.execute(
        """
        INSERT INTO user_queue_cursors (user_id, last_dispatched_at, running_tasks_count)
        VALUES (%s, now(), 0)
        ON CONFLICT (user_id) DO NOTHING
        """,
        (user_id,),
    )


def release_user_queue_slot_for_task(conn: BusinessConnection, *, task_id: str) -> None:
    """Pattern B/C: release the task owner's concurrency slot at a terminal
    state (SUCCEEDED / FAILED / expired-lease takeover / archive-retry backoff).

    Per-user concurrency is 1, so at most one slot is held per user; the
    GREATEST guard makes a double release a no-op instead of a negative count
    (revised ADR §2 Pattern C: a FAILED task must release, or the user starves).

    Like ensure_user_queue_cursor this runs on PostgreSQL regardless of the
    switch: tasks finished under legacy FIFO must settle the seeded counts
    before the rollout flip (M5 review P1-4). The SQLite lane has no table.
    """
    if not conn.is_postgres:
        return
    conn.execute(
        """
        UPDATE user_queue_cursors uqc
        SET running_tasks_count = GREATEST(running_tasks_count - 1, 0),
            last_dispatched_at = now()
        WHERE uqc.user_id = (
            SELECT b.created_by_user_id
            FROM generation_tasks t
            JOIN generation_batches b ON b.id = t.batch_id
            WHERE t.id = %s
        )
        """,
        (task_id,),
    )


def cleanup_idle_queue_cursors(conn: BusinessConnection, *, idle_days: int) -> int:
    """Pattern E: remove cursors of long-idle users (maintenance job, run in a
    low-traffic window; the caller schedules the cadence).

    A removed cursor is rebuilt by ensure_user_queue_cursor the next time the
    user creates work. Existing pending work is never considered idle: without
    its cursor the fair worker cannot schedule it again. Runs on PostgreSQL
    regardless of the switch, like the other cursor-maintenance points
    (M5 review P1-4); the SQLite lane has no table.
    """
    if not conn.is_postgres:
        return 0
    deleted = conn.execute(
        """
        DELETE FROM user_queue_cursors
        WHERE user_id IN (
            SELECT user_id
            FROM user_queue_cursors
            WHERE running_tasks_count = 0
              AND last_dispatched_at < now() - make_interval(days => %s)
              AND NOT EXISTS (
                  SELECT 1
                  FROM generation_batches AS batch
                  JOIN generation_tasks AS task ON task.batch_id = batch.id
                  WHERE batch.created_by_user_id = user_queue_cursors.user_id
                    AND task.status IN ('PENDING', 'QUEUED')
              )
            LIMIT 1000
            FOR UPDATE SKIP LOCKED
        )
        RETURNING user_id
        """,
        (idle_days,),
    ).fetchall()
    return len(deleted)


def _acquire_fair_queue_lease(
    conn: BusinessConnection,
    *,
    worker_id: str,
) -> dict[str, Any] | None:
    """Pattern A: user candidate check + task lease acquisition.

    Lock order is cursor row first, then task row (ADR lock-order chapter):
    the idle-user scan takes ``FOR UPDATE SKIP LOCKED`` on the cursor, the
    per-user task pick takes ``FOR UPDATE SKIP LOCKED`` inside the UPDATE's
    subquery (against generation_tasks only — the ownership EXISTS lookup
    never locks batch rows), and the cursor counter update re-locks the
    already-held cursor row. A user whose queue is empty, or whose only task
    is locked by another worker, ends the round and rotates to the next idle
    user (PostgreSQL: the same transaction keeps running, SKIP LOCKED skips
    the held cursor; the commit calls are no-ops there). This function runs
    only on the PostgreSQL lane — the desktop SQLite lane has no
    fair_queue_enabled column and always takes the global FIFO below.
    """
    from app.h3_account_pool import eligible_task_sql

    locked_until = (datetime.now(UTC) + timedelta(seconds=GENERATION_LEASE_SECONDS)).isoformat()
    for _ in range(_FAIR_QUEUE_MAX_ROUNDS):
        cursor_row = conn.execute(
            """
            SELECT user_id
            FROM user_queue_cursors
            WHERE running_tasks_count = 0
            ORDER BY last_dispatched_at ASC
            LIMIT 1
            FOR UPDATE SKIP LOCKED
            """
        ).fetchone()
        if cursor_row is None:
            # No idle user at all — the whole queue is busy.
            conn.commit()
            return None
        user_id = str(cursor_row["user_id"])
        task_row = conn.execute(
            f"""
            UPDATE generation_tasks
            SET
                attempt = attempt + CASE WHEN status IN ('PENDING', 'QUEUED') THEN 1 ELSE 0 END,
                status = 'SUBMITTING',
                locked_by = %s,
                locked_until = %s,
                submitted_at = CASE
                    WHEN status IN ('PENDING', 'QUEUED')
                    THEN COALESCE(submitted_at::timestamptz, CURRENT_TIMESTAMP)
                    ELSE submitted_at::timestamptz
                END,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = (
                SELECT t.id
                FROM generation_tasks t
                WHERE
                    (
                        t.status IN ('PENDING', 'QUEUED')
                        OR (
                            t.status = 'SUCCEEDED'
                            AND t.archive_status = 'ARCHIVE_FAILED'
                            AND t.provider_result_url IS NOT NULL
                            AND t.provider_result_url != ''
                        )
                    )
                    AND (t.locked_until IS NULL OR t.locked_until::timestamptz <= now())
                    AND {eligible_task_sql(conn, "t")}
                    -- M6b/Codex r-P1：fencing hold 的任务被父编排挂起，排除领取
                    -- 防热循环与槽位抖动；开关恢复由 resume_from_suspension 清标记。
                    AND (t.error_code IS DISTINCT FROM 'REPLICA_PIPELINE_SUSPENDED')
                    AND (t.next_poll_at IS NULL OR t.next_poll_at::timestamptz <= now())
                    AND EXISTS (
                        SELECT 1
                        FROM generation_batches b
                        WHERE b.id = t.batch_id
                          AND b.created_by_user_id = %s
                    )
                ORDER BY t.created_at, t.id
                LIMIT 1
                FOR UPDATE SKIP LOCKED
            )
            RETURNING id
            """,
            (worker_id, locked_until, user_id),
        ).fetchone()
        if task_row is None:
            # This user's queue is empty, or their only task is locked by
            # another worker. Either way nothing is schedulable for them right
            # now — advance the dispatcher timestamp so the rotation moves to
            # the next idle user instead of pinning this one at the head of
            # the line and starving everyone behind them (the empty user
            # re-enters the rotation at the tail).
            conn.execute(
                """
                UPDATE user_queue_cursors
                SET last_dispatched_at = now()
                WHERE user_id = %s
                """,
                (user_id,),
            )
            conn.commit()
            continue
        conn.execute(
            """
            UPDATE user_queue_cursors
            SET running_tasks_count = running_tasks_count + 1,
                last_dispatched_at = now()
            WHERE user_id = %s
            """,
            (user_id,),
        )
        conn.commit()
        return load_worker_task(conn, str(task_row["id"]))
    return None


def _acquire_global_fifo_lease(
    conn: BusinessConnection,
    *,
    worker_id: str,
) -> dict[str, Any] | None:
    """Legacy global FIFO acquisition (fair_queue_enabled = FALSE).

    The revised ADR §4 keeps this path as the migration-safe default: the
    switch stays off until the queue rollout is observed. The SQL is the same
    as before with the SQLite-only ``datetime()`` predicates replaced by the
    portable ``::timestamptz <= now()`` form (the translator parses both sides
    on the SQLite lane).
    """
    from app.h3_account_pool import eligible_task_sql

    locked_until = (datetime.now(UTC) + timedelta(seconds=GENERATION_LEASE_SECONDS)).isoformat()
    row = conn.execute(
        f"""
        UPDATE generation_tasks
        SET
            attempt = attempt + CASE WHEN status IN ('PENDING', 'QUEUED') THEN 1 ELSE 0 END,
            status = 'SUBMITTING',
            locked_by = %s,
            locked_until = %s,
            submitted_at = CASE
                WHEN status IN ('PENDING', 'QUEUED')
                THEN COALESCE(submitted_at::timestamptz, CURRENT_TIMESTAMP)
                ELSE submitted_at::timestamptz
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
                    OR locked_until::timestamptz <= now()
                )
                AND {eligible_task_sql(conn, "generation_tasks")}
                -- M6b/Codex r-P1：fencing hold 的任务被父编排挂起，排除领取。
                AND (error_code IS DISTINCT FROM 'REPLICA_PIPELINE_SUSPENDED')
                AND (next_poll_at IS NULL OR next_poll_at::timestamptz <= now())
                AND NOT EXISTS (
                    SELECT 1
                    FROM oral_tasks AS oral
                    JOIN generation_batches AS owner_batch
                      ON owner_batch.id = generation_tasks.batch_id
                    WHERE oral.owner_user_id = owner_batch.created_by_user_id
                      AND oral.status IN ('SUBMITTING', 'RUNNING', 'ARCHIVING')
                )
            ORDER BY created_at, id
            LIMIT 1
            FOR UPDATE SKIP LOCKED
        )
        RETURNING id
        """,
        (worker_id, locked_until),
    ).fetchone()
    conn.commit()
    if row is None:
        return None
    return load_worker_task(conn, str(row["id"]))


def acquire_generation_task_lease(
    conn: BusinessConnection,
    *,
    worker_id: str,
) -> dict[str, Any] | None:
    mark_expired_active_leases_needing_attention(conn)
    try:
        from app.h3_account_pool import assign_task_account, pool_capacity

        lock_shared_generation_capacity(conn)
        runtime = read_runtime_limits(conn)
        if pool_capacity(conn) is None and not shared_generation_capacity_available(
            conn,
            max_concurrent_tasks=runtime["max_concurrent_h3_tasks"],
        ):
            conn.commit()
            return None
        if _fair_queue_enabled(conn):
            lease = _acquire_fair_queue_lease(conn, worker_id=worker_id)
        else:
            lease = _acquire_global_fifo_lease(conn, worker_id=worker_id)
        if lease is not None:
            assign_task_account(conn, lease)
        return lease
    except Exception:
        conn.rollback()
        raise


def acquire_generation_continuation_lease(
    conn: BusinessConnection,
    *,
    worker_id: str,
) -> dict[str, Any] | None:
    """Claim one durable provider-poll or archive step without a new slot."""

    mark_expired_active_leases_needing_attention(conn)
    locked_until = (
        datetime.now(UTC) + timedelta(seconds=GENERATION_STEP_LEASE_SECONDS)
    ).isoformat()
    row = conn.execute(
        """
        UPDATE generation_tasks
        SET locked_by = %s, locked_until = %s, updated_at = CURRENT_TIMESTAMP
        WHERE id = (
            SELECT id
            FROM generation_tasks
            WHERE (
                    (status = 'RUNNING' AND provider_task_id IS NOT NULL)
                    OR (
                        status = 'ARCHIVING'
                        AND provider_result_url IS NOT NULL
                        AND provider_result_url != ''
                    )
                )
              AND (locked_until IS NULL OR locked_until::timestamptz <= now())
              AND (next_poll_at IS NULL OR next_poll_at::timestamptz <= now())
            ORDER BY next_poll_at NULLS FIRST, created_at, id
            LIMIT 1
            FOR UPDATE SKIP LOCKED
        )
        RETURNING id
        """,
        (worker_id, locked_until),
    ).fetchone()
    if row is None:
        return None
    return load_worker_task(conn, str(row["id"]))


def load_worker_task(conn: BusinessConnection, task_id: str) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT
            generation_tasks.id,
            generation_tasks.attempt,
            generation_tasks.batch_id,
            generation_tasks.provider,
            generation_tasks.status,
            generation_tasks.archive_status,
            generation_tasks.provider_task_id,
            generation_tasks.provider_result_url,
            generation_tasks.provider_request_json,
            generation_tasks.submitted_at,
            generation_tasks.started_at,
            generation_tasks.locked_by,
            generation_tasks.locked_until,
            generation_tasks.archive_retry_count,
            generation_batches.project_id,
            generation_batches.created_by_user_id,
            generation_batches.request_snapshot_json,
            generation_tasks.prompt_snapshot_json
        FROM generation_tasks
        JOIN generation_batches ON generation_batches.id = generation_tasks.batch_id
        WHERE generation_tasks.id = %s
        """,
        (task_id,),
    ).fetchone()
    if row is None:
        raise LookupError("leased task disappeared")

    prompt_snapshot = json.loads(str(row["prompt_snapshot_json"]))
    request_snapshot = json.loads(str(row["request_snapshot_json"]))
    payload = dict(row)
    payload["prompt_text"] = prompt_snapshot["prompt_text"]
    payload["first_frame_uri"] = prompt_snapshot.get("first_frame_uri")
    payload["generation_mode"] = prompt_snapshot.get("generation_mode", "I2V")
    payload["last_frame_uri"] = prompt_snapshot.get("last_frame_uri")
    payload["reference_images"] = prompt_snapshot.get("reference_images", [])
    payload["reference_videos"] = prompt_snapshot.get("reference_videos", [])
    payload["reference_audios"] = prompt_snapshot.get("reference_audios", [])
    payload["output_duration_seconds"] = request_snapshot["output_duration_seconds"]
    payload["resolution"] = request_snapshot["resolution"]
    payload["ratio"] = request_snapshot.get("ratio", "adaptive")
    return payload


def prepare_generation_submission(
    conn: BusinessConnection,
    *,
    lease: dict[str, Any],
    first_frame_storage: StorageAdapter,
    provider: H3Provider | None = None,
) -> GenerationSubmissionWork:
    """Build a paid request while the caller owns only a short DB transaction."""

    provider_name = str(lease["provider"])
    if provider_name == "metaso" and first_frame_storage.provider != "cos":
        raise H3ProviderSettingsUnavailable("H3 generation requires COS first-frame storage")
    selected_provider = provider or h3_provider_for_task(
        conn, provider_name, task_id=str(lease["id"])
    )

    def _signed_url(storage_uri: str) -> str:
        ref = storage_object_ref_from_uri(storage_uri)
        require_storage_match(first_frame_storage, ref)
        return first_frame_storage.create_download_intent(
            ref.key,
            expires_in=FIRST_FRAME_URL_EXPIRES_IN,
            can_read=True,
        ).url

    first_frame_uri = lease.get("first_frame_uri")
    last_frame_uri = lease.get("last_frame_uri")
    reference_images = lease.get("reference_images") or []
    reference_videos = lease.get("reference_videos") or []
    reference_audios = lease.get("reference_audios") or []
    provider_request = build_h3_request(
        prompt_text=str(lease["prompt_text"]),
        first_frame_url=_signed_url(str(first_frame_uri))
        if isinstance(first_frame_uri, str) and first_frame_uri
        else None,
        last_frame_url=_signed_url(str(last_frame_uri))
        if isinstance(last_frame_uri, str) and last_frame_uri
        else None,
        reference_images=[
            {"name": str(image.get("name", "")), "url": _signed_url(str(image["uri"]))}
            for image in reference_images
            if isinstance(image, dict) and image.get("uri")
        ],
        reference_videos=[
            {"url": _signed_url(str(video["uri"]))}
            for video in reference_videos
            if isinstance(video, dict) and video.get("uri")
        ],
        reference_audios=[
            {"url": _signed_url(str(audio["uri"]))}
            for audio in reference_audios
            if isinstance(audio, dict) and audio.get("uri")
        ],
        duration_seconds=int(lease["output_duration_seconds"]),
        resolution=str(lease["resolution"]),
        ratio=str(lease["ratio"]),
    )
    return GenerationSubmissionWork(
        provider=selected_provider,
        provider_request=provider_request,
    )


def hold_generation_task_submission(conn: BusinessConnection, *, lease: dict[str, Any]) -> None:
    """父编排挂起/止付：无发送意图的生成提交退回 PENDING（方案 §6 三态表）.

    仅在 SUBMITTING 且尚无供应商回执时调用：不计费、不耗重试预算；
    预占保留（在途资格不释放），开关恢复后重新领取再签发意图。

    - CAS 绑定 ``locked_by``/``attempt``（Codex r-P1：失租旧 worker 的迟到
      hold 不得清掉新 worker 的租约，否则提交中的任务被重复排队）；
    - 同事务归还公平队列槽（Codex r-P1：一次拒绝若不还槽，用户槽位恒为 1，
      开关恢复后也无法再领取）；GREATEST 守卫使重复释放无害。
    """
    updated = conn.execute(
        """
        UPDATE generation_tasks
        SET status = 'PENDING',
            error_code = 'REPLICA_PIPELINE_SUSPENDED',
            error_message_redacted = '全自动复刻已暂停，任务等待恢复。',
            locked_by = NULL, locked_until = NULL,
            next_poll_at = CURRENT_TIMESTAMP,
            submitted_at = NULL, started_at = NULL, completed_at = NULL,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = %s AND status = 'SUBMITTING' AND provider_task_id IS NULL
          AND locked_by = %s AND attempt = %s
        """,
        (str(lease["id"]), str(lease["locked_by"]), int(lease["attempt"])),
    ).rowcount
    if updated != 1:
        conn.rollback()
        raise RuntimeError("generation task submission lease was lost")
    release_user_queue_slot_for_task(conn, task_id=str(lease["id"]))
    conn.commit()


def cancel_generation_task_on_drain(conn: BusinessConnection, *, lease: dict[str, Any]) -> None:
    """父编排进入 DRAINING/终态：无意图的确定未发送任务终态并释放预占.

    方案 §6 r6#3（Codex r-P1 挂账闭环）：hold 语义只服务可恢复的挂起；生成
    任务若在 DRAINING 里仍回 PENDING，批次观察会把未收口子任务永久占用在途
    资格与预占。处理与批次取消同款：CAS 绑定租约置 CANCELLED，
    ``finalize_internal_billing(outcome="failed")`` 释放预占（实战路径），
    公平队列槽同步归还。
    """
    updated = conn.execute(
        """
        UPDATE generation_tasks
        SET status = 'CANCELLED',
            error_code = 'REPLICA_PIPELINE_DRAIN_CANCELLED',
            error_message_redacted =
                '全自动复刻任务已取消，未产生供应商调用，费用已全额释放；可重新发起。',
            locked_by = NULL, locked_until = NULL,
            next_poll_at = CURRENT_TIMESTAMP,
            submitted_at = NULL, started_at = NULL, completed_at = NULL,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = %s AND status = 'SUBMITTING' AND provider_task_id IS NULL
          AND locked_by = %s AND attempt = %s
        """,
        (str(lease["id"]), str(lease["locked_by"]), int(lease["attempt"])),
    ).rowcount
    if updated != 1:
        conn.rollback()
        raise RuntimeError("generation task submission lease was lost")
    finalize_internal_billing(conn, task_id=str(lease["id"]), outcome="failed")
    release_user_queue_slot_for_task(conn, task_id=str(lease["id"]))
    conn.commit()


def mark_generation_task_running(
    conn: BusinessConnection,
    *,
    lease: dict[str, Any],
    provider_task_id: str,
    provider_request: dict[str, Any],
    release_lease: bool = True,
) -> None:
    """Durably record the paid provider id before any polling starts."""

    task_id = str(lease["id"])
    update = conn.execute(
        """
        UPDATE generation_tasks
        SET
            provider_task_id = %s,
            provider_request_json = %s,
            status = 'RUNNING',
            error_code = NULL,
            error_message_redacted = NULL,
            started_at = COALESCE(started_at::timestamptz, CURRENT_TIMESTAMP),
            next_poll_at = now() + interval '5 seconds',
            locked_by = %s,
            locked_until = %s,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = %s AND status = 'SUBMITTING' AND superseded_by_task_id IS NULL
        """,
        (
            provider_task_id,
            json.dumps(provider_request, ensure_ascii=True, sort_keys=True),
            None if release_lease else str(lease["locked_by"]),
            None if release_lease else str(lease["locked_until"]),
            task_id,
        ),
    )
    if update.rowcount != 1:
        row = conn.execute(
            "SELECT superseded_by_task_id FROM generation_tasks WHERE id = %s", (task_id,)
        ).fetchone()
        if row is not None and row["superseded_by_task_id"] is not None:
            raise GenerationTaskSupersededError(task_id)
        raise RuntimeError("generation submission lease was lost before task id persistence")
    # 调用日志由传输层统一记录（方案 P0-9），这里不再单独写行：同一次提交在日志里
    # 只会留下一条带任务归属与实际耗时、响应的记录。
    _refresh_batch_status_in_transaction(conn, batch_id=str(lease["batch_id"]))


def reschedule_generation_poll(
    conn: BusinessConnection,
    *,
    lease: dict[str, Any],
    delay_seconds: int = GENERATION_POLL_INTERVAL_SECONDS,
) -> None:
    if delay_seconds < 1:
        raise ValueError("delay_seconds must be positive")
    update = conn.execute(
        """
        WITH current_task AS (
            SELECT
                id,
                batch_id,
                COALESCE(
                    submitted_at::timestamptz,
                    started_at::timestamptz,
                    created_at::timestamptz
                ) <= now() - (%s * interval '1 second') AS timed_out
            FROM generation_tasks
            WHERE id = %s AND status = 'RUNNING'
              AND locked_by = %s AND locked_until = %s
              AND superseded_by_task_id IS NULL
            FOR UPDATE
        )
        UPDATE generation_tasks AS task
        SET
            status = CASE
                WHEN current_task.timed_out THEN 'SUBMISSION_UNCERTAIN'
                ELSE task.status
            END,
            error_code = CASE
                WHEN current_task.timed_out THEN 'PROVIDER_POLL_TIMEOUT'
                ELSE task.error_code
            END,
            error_message_redacted = CASE
                WHEN current_task.timed_out
                THEN '生成超过自动核对时限，请人工确认任务结果后再处理。'
                ELSE task.error_message_redacted
            END,
            next_poll_at = CASE
                WHEN current_task.timed_out THEN NULL
                ELSE now() + (%s * interval '1 second')
            END,
            locked_by = NULL,
            locked_until = NULL,
            updated_at = CURRENT_TIMESTAMP
        FROM current_task
        WHERE task.id = current_task.id
        RETURNING task.status, task.batch_id
        """,
        (
            GENERATION_MAX_POLL_AGE_SECONDS,
            str(lease["id"]),
            str(lease["locked_by"]),
            str(lease["locked_until"]),
            delay_seconds,
        ),
    ).fetchone()
    if update is None or str(update["status"]) != "SUBMISSION_UNCERTAIN":
        return
    task_id = str(lease["id"])
    batch_id = str(update["batch_id"])
    _insert_worker_audit(
        conn,
        action="generation_task.provider_poll_timeout",
        entity_type="generation_task",
        entity_id=task_id,
        metadata={
            "batch_id": batch_id,
            "max_poll_age_seconds": GENERATION_MAX_POLL_AGE_SECONDS,
            "provider_task_id_present": bool(lease.get("provider_task_id")),
        },
    )
    release_user_queue_slot_for_task(conn, task_id=task_id)
    _refresh_batch_status_in_transaction(conn, batch_id=batch_id)


def mark_generation_task_archiving(
    conn: BusinessConnection,
    *,
    lease: dict[str, Any],
    result_url: str,
) -> None:
    update = conn.execute(
        """
        UPDATE generation_tasks
        SET
            status = 'ARCHIVING',
            provider_result_url = %s,
            next_poll_at = now(),
            locked_by = NULL,
            locked_until = NULL,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = %s
          AND status IN ('RUNNING', 'SUBMITTING')
          AND locked_by = %s
          AND locked_until = %s
          AND superseded_by_task_id IS NULL
        """,
        (
            result_url,
            str(lease["id"]),
            str(lease["locked_by"]),
            str(lease["locked_until"]),
        ),
    )
    if update.rowcount != 1:
        row = conn.execute(
            "SELECT superseded_by_task_id FROM generation_tasks WHERE id = %s",
            (str(lease["id"]),),
        ).fetchone()
        if row is not None and row["superseded_by_task_id"] is not None:
            raise GenerationTaskSupersededError(str(lease["id"]))
        raise RuntimeError("generation polling lease was lost before archiving")
    _refresh_batch_status_in_transaction(conn, batch_id=str(lease["batch_id"]))


def finalize_generation_direct_result(
    conn: BusinessConnection,
    *,
    lease: dict[str, Any],
    quality_status: str,
    quality_issue_codes: list[str],
    output_seconds: float | None = None,
) -> TaskResult:
    """Settle a provider-hosted result without copying video bytes to storage."""

    task_id = str(lease["id"])
    update = conn.execute(
        """
        UPDATE generation_tasks
        SET
            status = 'SUCCEEDED',
            archive_status = 'DIRECT',
            quality_status = %s,
            quality_issue_codes = %s,
            result_asset_id = NULL,
            error_code = NULL,
            error_message_redacted = NULL,
            next_poll_at = NULL,
            locked_by = NULL,
            locked_until = NULL,
            completed_at = CURRENT_TIMESTAMP,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = %s AND status = 'ARCHIVING'
          AND provider_result_url IS NOT NULL
          AND (%s <> 'ARCHIVING' OR (locked_by = %s AND locked_until = %s))
          AND superseded_by_task_id IS NULL
        """,
        (
            quality_status,
            json.dumps(quality_issue_codes, ensure_ascii=True),
            task_id,
            str(lease["status"]),
            str(lease["locked_by"]),
            str(lease["locked_until"]),
        ),
    )
    if update.rowcount != 1:
        row = conn.execute(
            "SELECT superseded_by_task_id FROM generation_tasks WHERE id = %s", (task_id,)
        ).fetchone()
        if row is not None and row["superseded_by_task_id"] is not None:
            raise GenerationTaskSupersededError(task_id)
        raise RuntimeError("generation result lease was lost before direct delivery")
    record_video_generation_cost(conn, task_id=task_id, output_seconds=output_seconds)
    finalize_internal_billing(conn, task_id=task_id, outcome="success")
    _refresh_batch_status_in_transaction(conn, batch_id=str(lease["batch_id"]))
    release_user_queue_slot_for_task(conn, task_id=task_id)
    return get_task_result(conn, task_id)


def release_generation_archive_retry(
    conn: BusinessConnection,
    *,
    lease: dict[str, Any],
) -> None:
    _release_archive_retry(
        conn,
        task_id=str(lease["id"]),
        batch_id=str(lease["batch_id"]),
    )


def release_generation_visual_validation_retry(
    conn: BusinessConnection,
    *,
    lease: dict[str, Any],
) -> None:
    _release_archive_retry(
        conn,
        task_id=str(lease["id"]),
        batch_id=str(lease["batch_id"]),
        quality_status="VISUAL_VALIDATION_UNAVAILABLE",
        error_code="VISUAL_VALIDATION_UNAVAILABLE",
        error_message="Generated video is awaiting visual validation.",
    )


def mark_task_submission_uncertain(
    conn: BusinessConnection,
    *,
    task_id: str,
    message: str,
    provider_task_id: str | None = None,
    error_code: str = "SUBMISSION_UNCERTAIN",
) -> None:
    with conn:
        row = conn.execute(
            "SELECT batch_id FROM generation_tasks WHERE id = %s",
            (task_id,),
        ).fetchone()
        # CW-030: only an in-flight task (SUBMITTING = provider call in
        # flight, RUNNING = submitted/polling) may transition to
        # SUBMISSION_UNCERTAIN. A late duplicate signal for an already
        # uncertain/reconciled/replacement task must not transition anything
        # and must not release the owner's slot a second time — that would
        # let the same user run a second concurrent task (the mark-side twin
        # of the P1-5 reconcile guard).
        update = conn.execute(
            """
            UPDATE generation_tasks
            SET
                provider_task_id = COALESCE(%s, provider_task_id),
                status = 'SUBMISSION_UNCERTAIN',
                error_code = %s,
                error_message_redacted = %s,
                locked_by = NULL,
                locked_until = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = %s AND status IN ('SUBMITTING', 'RUNNING')
            RETURNING batch_id
            """,
            (provider_task_id, error_code, message, task_id),
        ).fetchone()
        # The task leaves the runnable/running states here; its concurrency
        # slot must be released with the transition, or the user's cursor
        # stays pinned at 1 and no later task of theirs ever runs (M5 review
        # P1-2 — the expiry sweeper cannot reach it: no lease remains).
        if update is not None:
            release_user_queue_slot_for_task(conn, task_id=task_id)
        if row is not None:
            _refresh_batch_status_in_transaction(conn, batch_id=str(row["batch_id"]))


def mark_task_provider_settings_unavailable(
    conn: BusinessConnection,
    *,
    lease: dict[str, Any],
) -> None:
    task_id = str(lease["id"])
    batch_id = str(lease["batch_id"])
    with conn:
        update = conn.execute(
            """
            UPDATE generation_tasks
            SET
                status = 'FAILED',
                error_code = 'H3_SETTINGS_UNAVAILABLE',
                error_message_redacted = '视频生成服务配置不可用，请到技术配置检查后重试。',
                submitted_at = NULL,
                locked_by = NULL,
                locked_until = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = %s AND status = %s AND locked_by = %s AND locked_until = %s
              AND superseded_by_task_id IS NULL
            """,
            (
                task_id,
                str(lease["status"]),
                str(lease["locked_by"]),
                str(lease["locked_until"]),
            ),
        )
        if update.rowcount != 1:
            raise GenerationTaskSupersededError(task_id)
        record_video_generation_not_called(conn, task_id=task_id)
        finalize_internal_billing(conn, task_id=task_id, outcome="failed")
        _refresh_batch_status_in_transaction(conn, batch_id=batch_id)
        release_user_queue_slot_for_task(conn, task_id=task_id)


def mark_task_provider_failed(
    conn: BusinessConnection,
    *,
    lease: dict[str, Any],
    provider_task_id: str | None,
    provider_reason: str | None = None,
    submission_rejected: bool = False,
) -> None:
    """Terminal failure of the provider call.

    ``submission_rejected`` marks a submission the provider explicitly refused
    (e.g. HTTP 402 insufficient balance): nothing was created or charged, so the
    cost closes at zero and the customer message must not echo the provider's
    own wording (it would expose supplier balance state). The raw response stays
    in external_call_logs for operators.
    """
    provider_reason = summarize_provider_message(provider_reason)
    task_id = str(lease["id"])
    batch_id = str(lease["batch_id"])
    if submission_rejected:
        error_code = "H3_SUBMISSION_REJECTED"
        message = SUBMISSION_REJECTED_CUSTOMER_MESSAGE
    else:
        error_code = "H3_PROVIDER_FAILED"
        message = (
            f"视频生成服务返回失败：{provider_reason}"
            if provider_reason
            else "视频生成失败或返回了无效结果，服务商未给出原因。"
        )
    with conn:
        provider_failed_update = conn.execute(
            """
            UPDATE generation_tasks
            SET
                provider_task_id = %s,
                status = 'FAILED',
                error_code = %s,
                error_message_redacted = %s,
                locked_by = NULL,
                locked_until = NULL,
                completed_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = %s AND status = %s AND locked_by = %s AND locked_until = %s
              AND superseded_by_task_id IS NULL
            """,
            (
                provider_task_id,
                error_code,
                message,
                task_id,
                str(lease["status"]),
                str(lease["locked_by"]),
                str(lease["locked_until"]),
            ),
        )
        if provider_failed_update.rowcount != 1:
            # L1 (M4M5 review): the replacement flow owns the terminal
            # state, the billing settlement and the slot release now.
            raise GenerationTaskSupersededError(task_id)
        if submission_rejected:
            record_video_generation_not_called(conn, task_id=task_id)
        else:
            record_video_generation_cost(conn, task_id=task_id, output_seconds=None)
        finalize_internal_billing(conn, task_id=task_id, outcome="failed")
        _refresh_batch_status_in_transaction(conn, batch_id=batch_id)
        release_user_queue_slot_for_task(conn, task_id=task_id)


def mark_task_first_frame_url_sign_failed(
    conn: BusinessConnection,
    *,
    lease: dict[str, Any],
) -> None:
    """A provider call never started, so this is safe to mark as a normal failure."""
    task_id = str(lease["id"])
    batch_id = str(lease["batch_id"])
    with conn:
        url_sign_failed_update = conn.execute(
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
            WHERE id = %s AND status = %s AND locked_by = %s AND locked_until = %s
              AND superseded_by_task_id IS NULL
            """,
            (
                task_id,
                str(lease["status"]),
                str(lease["locked_by"]),
                str(lease["locked_until"]),
            ),
        )
        if url_sign_failed_update.rowcount != 1:
            # L1 (M4M5 review): the replacement flow owns the terminal
            # state, the billing settlement and the slot release now.
            raise GenerationTaskSupersededError(task_id)
        record_video_generation_not_called(conn, task_id=task_id)
        finalize_internal_billing(conn, task_id=task_id, outcome="failed")
        _refresh_batch_status_in_transaction(conn, batch_id=batch_id)
        # Terminal failure: the per-user concurrency slot is free again.
        release_user_queue_slot_for_task(conn, task_id=task_id)


def mark_task_provider_request_contract_failed(
    conn: BusinessConnection,
    *,
    lease: dict[str, Any],
) -> None:
    """Terminal failure for a submission that violates the provider contract.

    Same safe-failure shape as ``mark_task_first_frame_url_sign_failed`` (the
    provider call never started), but with an honest error code so triage
    does not chase a first-frame signing problem that never existed.
    """
    task_id = str(lease["id"])
    batch_id = str(lease["batch_id"])
    with conn:
        contract_failed_update = conn.execute(
            """
            UPDATE generation_tasks
            SET
                status = 'FAILED',
                error_code = 'PROVIDER_REQUEST_CONTRACT',
                error_message_redacted = 'Generation parameters violate the provider contract.',
                submitted_at = NULL,
                locked_by = NULL,
                locked_until = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = %s AND status = %s AND locked_by = %s AND locked_until = %s
              AND superseded_by_task_id IS NULL
            """,
            (
                task_id,
                str(lease["status"]),
                str(lease["locked_by"]),
                str(lease["locked_until"]),
            ),
        )
        if contract_failed_update.rowcount != 1:
            raise GenerationTaskSupersededError(task_id)
        record_video_generation_not_called(conn, task_id=task_id)
        finalize_internal_billing(conn, task_id=task_id, outcome="failed")
        _refresh_batch_status_in_transaction(conn, batch_id=batch_id)
        release_user_queue_slot_for_task(conn, task_id=task_id)


def _insert_worker_audit(
    conn: BusinessConnection,
    *,
    action: str,
    entity_type: str,
    entity_id: str,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Audit row written by the worker itself when no user session exists.

    ``actor_user_id`` stays NULL: the FK is ON DELETE SET NULL (migration
    001), so worker-owned rows must never reference a user id.
    """
    conn.execute(
        """
        INSERT INTO audit_logs (id, actor_user_id, action, entity_type, entity_id, metadata_json)
        VALUES (%s, NULL, %s, %s, %s, %s)
        """,
        (
            str(uuid4()),
            action,
            entity_type,
            entity_id,
            json.dumps(metadata or {}, ensure_ascii=True, sort_keys=True),
        ),
    )


def mark_expired_active_leases_needing_attention(conn: BusinessConnection) -> None:
    """Recover durable steps and quarantine only truly uncertain submissions."""
    with conn:
        # Once the provider task id/result URL is durable, a crashed worker can
        # safely resume the next step.  Keep the user's running slot occupied:
        # the same paid task is still active and must not be double-submitted.
        recoverable_rows = conn.execute(
            """
            UPDATE generation_tasks
            SET
                locked_by = NULL,
                locked_until = NULL,
                next_poll_at = now(),
                updated_at = CURRENT_TIMESTAMP
            WHERE (
                    (status = 'RUNNING' AND provider_task_id IS NOT NULL)
                    OR (
                        status = 'ARCHIVING'
                        AND provider_result_url IS NOT NULL
                        AND provider_result_url != ''
                    )
                )
              AND archive_status != 'ARCHIVE_FAILED'
              AND locked_until IS NOT NULL
              AND locked_until::timestamptz <= now()
            RETURNING id, batch_id, status
            """
        ).fetchall()
        # Archive retries never start a paid call; an expired lease is safe to
        # reset so another worker can re-download and re-archive the result.
        # Both updates drive audit and slot release from RETURNING so a
        # concurrent sweeper's loser only ever sees the rows its own update
        # transitioned (M5 review P2-1: the former SELECT-then-update snapshot
        # let the loser audit the takeover twice and release a slot the winner
        # had already handed to the user's replacement task).
        archive_rows = conn.execute(
            """
            UPDATE generation_tasks
            SET
                status = 'SUCCEEDED',
                archive_status = 'ARCHIVE_FAILED',
                locked_by = NULL,
                locked_until = NULL,
                next_poll_at = now() + interval '60 seconds',
                updated_at = CURRENT_TIMESTAMP
            WHERE status IN ('SUBMITTING', 'RUNNING', 'ARCHIVING')
              AND archive_status = 'ARCHIVE_FAILED'
              AND provider_result_url IS NOT NULL
              AND locked_until IS NOT NULL
              AND locked_until::timestamptz <= now()
            RETURNING id, batch_id
            """
        ).fetchall()
        uncertain_rows = conn.execute(
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
              AND NOT (status = 'RUNNING' AND provider_task_id IS NOT NULL)
              AND NOT (
                  status = 'ARCHIVING'
                  AND provider_result_url IS NOT NULL
                  AND provider_result_url != ''
              )
              AND locked_until IS NOT NULL
              AND locked_until::timestamptz <= now()
            RETURNING id, batch_id
            """
        ).fetchall()
        for row in recoverable_rows:
            _insert_worker_audit(
                conn,
                action="generation_task.lease_expired_resumed",
                entity_type="generation_task",
                entity_id=str(row["id"]),
                metadata={
                    "batch_id": str(row["batch_id"]),
                    "status": str(row["status"]),
                },
            )
        # Every non-recoverable expired lease leaves the per-user concurrency slot occupied
        # (revised ADR §2.2 Step 2): release it. Archive-retry backoffs and
        # SUBMISSION_UNCERTAIN tasks both stop consuming a running slot here;
        # the former re-acquires one when a worker picks the retry up. Each
        # takeover is audited with a worker-owned row (no actor session).
        for row in archive_rows:
            _insert_worker_audit(
                conn,
                action="generation_task.lease_expired_archive_retry",
                entity_type="generation_task",
                entity_id=str(row["id"]),
                metadata={"batch_id": str(row["batch_id"])},
            )
            release_user_queue_slot_for_task(conn, task_id=str(row["id"]))
        for row in uncertain_rows:
            _insert_worker_audit(
                conn,
                action="generation_task.lease_expired_uncertain",
                entity_type="generation_task",
                entity_id=str(row["id"]),
                metadata={"batch_id": str(row["batch_id"])},
            )
            release_user_queue_slot_for_task(conn, task_id=str(row["id"]))
    batch_ids = (
        {str(row["batch_id"]) for row in recoverable_rows}
        | {str(row["batch_id"]) for row in archive_rows}
        | {str(row["batch_id"]) for row in uncertain_rows}
    )
    for batch_id in batch_ids:
        refresh_batch_status(conn, batch_id=batch_id)


def refresh_batch_status(conn: BusinessConnection, *, batch_id: str) -> None:
    with conn:
        _refresh_batch_status_in_transaction(conn, batch_id=batch_id)


def _refresh_batch_status_in_transaction(
    conn: BusinessConnection,
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
        WHERE batch_id = %s
        """,
        (batch_id,),
    ).fetchall()
    progress = calculate_progress([task_result(row) for row in rows])
    conn.execute(
        """
        UPDATE generation_batches
        SET status = %s, updated_at = CURRENT_TIMESTAMP
        WHERE id = %s
        """,
        (batch_status("QUEUED", progress), batch_id),
    )


MANUAL_ARCHIVE_LEASE_SECONDS = 600
STRICT_DURATION_POLICY = "strict_duration_v1"


def requires_strict_generation_duration(task: Mapping[str, Any]) -> bool:
    return (
        task.get("billed_seconds") == 30
        or _seconds_from_prompt_snapshot(task.get("prompt_snapshot_json")) == 30
    )


def generation_result_archive_ready(conn: BusinessConnection, task: Mapping[str, Any]) -> bool:
    if not task["result_asset_id"]:
        return False
    if not requires_strict_generation_duration(task):
        return True
    asset = conn.execute(
        "SELECT metadata_json FROM assets WHERE id=%s", (task["result_asset_id"],)
    ).fetchone()
    if asset is None:
        return False
    try:
        metadata = json.loads(str(asset["metadata_json"]))
        normalized = metadata["video_normalization"]
        return bool(
            normalized["policy"] == STRICT_DURATION_POLICY
            and normalized["target_duration_seconds"] == 30
            and metadata["duration_seconds"] == 30
        )
    except (TypeError, ValueError, KeyError):
        return False


def prepare_generation_result_archive(
    conn: BusinessConnection, *, actor: CurrentUser, task_id: str
) -> dict[str, Any]:
    """Authorize a completed result before doing any provider/storage I/O."""
    row = conn.execute(
        """SELECT task.*, batch.project_id, batch.created_by_user_id
           FROM generation_tasks AS task
           JOIN generation_batches AS batch ON batch.id=task.batch_id
           WHERE task.id=%s""",
        (task_id,),
    ).fetchone()
    if row is None:
        raise generation_error(404, "TASK_NOT_FOUND", "任务不存在。")
    require_batch_access(
        conn,
        actor=actor,
        project_id=row["project_id"],
        created_by_user_id=str(row["created_by_user_id"]),
        action="generation_task.archive",
    )
    require_not_auditor(
        conn,
        actor=actor,
        action="generation_task.archive",
        entity_type="generation_task",
        entity_id=task_id,
    )
    if (
        row["status"] != "SUCCEEDED"
        or row["archive_status"] not in {"DIRECT", "ARCHIVED"}
        or row["superseded_by_task_id"] is not None
    ):
        raise generation_error(409, "RESULT_NOT_READY", "当前任务没有可保存的成功成片。")
    if not row["result_asset_id"] and not row["provider_result_url"]:
        raise generation_error(409, "RESULT_URL_NOT_READY", "成片地址尚未就绪。")
    return dict(row)


def claim_generation_result_archive(
    conn: BusinessConnection, *, actor: CurrentUser, task_id: str
) -> dict[str, Any]:
    """Claim only a completed result; ordinary workers cannot consume this lease."""
    prepared = prepare_generation_result_archive(conn, actor=actor, task_id=task_id)
    if generation_result_archive_ready(conn, prepared):
        return prepared
    claimed = conn.execute(
        """UPDATE generation_tasks
           SET locked_by=%s,
               locked_until=(clock_timestamp()+%s*interval '1 second')::text,
               updated_at=CURRENT_TIMESTAMP
           WHERE id=%s AND status='SUCCEEDED' AND archive_status IN ('DIRECT','ARCHIVED')
             AND result_asset_id IS NOT DISTINCT FROM %s AND superseded_by_task_id IS NULL
             AND (locked_by IS NULL OR locked_until IS NULL
                  OR locked_until::timestamptz <= clock_timestamp())
           RETURNING id""",
        (f"archive:{uuid4()}", MANUAL_ARCHIVE_LEASE_SECONDS, task_id, prepared["result_asset_id"]),
    ).fetchone()
    current = prepare_generation_result_archive(conn, actor=actor, task_id=task_id)
    if claimed is None and not generation_result_archive_ready(conn, current):
        raise generation_error(
            409, "RESULT_ARCHIVE_IN_PROGRESS", "同一成片正在保存中，请稍后刷新任务核对。"
        )
    return {**current, "_archive_claim_acquired": claimed is not None}


def _require_archive_source_unchanged(current: dict[str, Any], prepared: dict[str, Any]) -> None:
    if any(
        current[key] != prepared[key]
        for key in (
            "provider",
            "provider_task_id",
            "provider_result_url",
            "project_id",
            "prompt_snapshot_json",
            "result_asset_id",
        )
    ):
        raise generation_error(409, "RESULT_CHANGED", "成片记录已变化，请刷新后重试。")


def _archive_lease_lost() -> HTTPException:
    return generation_error(
        409, "RESULT_ARCHIVE_LEASE_LOST", "本次保存处理权已失效，请刷新核对后重试。"
    )


def renew_generation_result_archive_claim(
    conn: BusinessConnection, *, actor: CurrentUser, prepared: dict[str, Any]
) -> dict[str, Any]:
    """Renew between I/O stages, never resurrect an expired or replaced lease."""
    current = prepare_generation_result_archive(conn, actor=actor, task_id=str(prepared["id"]))
    _require_archive_source_unchanged(current, prepared)
    row = conn.execute(
        """UPDATE generation_tasks
           SET locked_until=(clock_timestamp()+%s*interval '1 second')::text,
               updated_at=CURRENT_TIMESTAMP
           WHERE id=%s AND status='SUCCEEDED' AND result_asset_id IS NOT DISTINCT FROM %s
             AND superseded_by_task_id IS NULL
             AND locked_by=%s AND locked_until=%s
             AND locked_until::timestamptz > clock_timestamp()
           RETURNING locked_until""",
        (
            MANUAL_ARCHIVE_LEASE_SECONDS,
            prepared["id"],
            prepared["result_asset_id"],
            prepared.get("locked_by"),
            prepared.get("locked_until"),
        ),
    ).fetchone()
    if row is None:
        raise _archive_lease_lost()
    return {**prepared, "locked_until": row["locked_until"]}


def release_generation_result_archive_claim(
    conn: BusinessConnection, *, prepared: dict[str, Any]
) -> bool:
    """Internal cleanup capability: clear only this exact claim, no assets/billing."""
    owner = prepared.get("locked_by")
    if not isinstance(owner, str) or not owner.startswith("archive:"):
        return False
    result = conn.execute(
        """UPDATE generation_tasks SET locked_by=NULL,locked_until=NULL,
                  updated_at=CURRENT_TIMESTAMP
           WHERE id=%s AND status='SUCCEEDED' AND result_asset_id IS NOT DISTINCT FROM %s
             AND locked_by=%s AND locked_until=%s""",
        (prepared["id"], prepared["result_asset_id"], owner, prepared.get("locked_until")),
    )
    return result.rowcount == 1


def persist_generation_result_archive(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    prepared: dict[str, Any],
    stored: StoredObject,
    duration_seconds: float,
    normalization_metadata: dict[str, Any] | None = None,
    thumbnail_key: str | None = None,
) -> TaskResult:
    """Publish one physical asset; archiving never changes settled billing."""
    task_id = str(prepared["id"])
    with conn:
        conn.execute(
            "SELECT id FROM generation_tasks WHERE id=%s FOR UPDATE", (task_id,)
        ).fetchone()
        current = prepare_generation_result_archive(conn, actor=actor, task_id=task_id)
        if generation_result_archive_ready(conn, current):
            return get_task_result(conn, task_id)
        if requires_strict_generation_duration(current) and (
            duration_seconds != 30
            or normalization_metadata is None
            or normalization_metadata.get("policy") != STRICT_DURATION_POLICY
            or normalization_metadata.get("target_duration_seconds") != 30
        ):
            raise generation_error(
                409, "RESULT_DURATION_INVALID", "30 秒成片尚未通过交付时长校验。"
            )
        _require_archive_source_unchanged(current, prepared)
        claim = conn.execute(
            """SELECT id FROM generation_tasks WHERE id=%s AND locked_by=%s
                 AND locked_until=%s AND locked_until::timestamptz > clock_timestamp()""",
            (task_id, prepared.get("locked_by"), prepared.get("locked_until")),
        ).fetchone()
        if claim is None:
            raise _archive_lease_lost()
        asset_id = str(uuid4())
        conn.execute(
            """INSERT INTO assets(id,project_id,kind,storage_uri,sha256,size_bytes,
                       content_type,metadata_json,created_by_user_id)
               VALUES (%s,%s,'material_video',%s,%s,%s,'video/mp4',%s,%s)""",
            (
                asset_id,
                current["project_id"],
                stored.uri,
                stored.sha256,
                stored.size,
                json.dumps(
                    {
                        "duration_seconds": duration_seconds,
                        "generation_task_id": task_id,
                        **(
                            {"video_normalization": normalization_metadata}
                            if normalization_metadata is not None
                            else {}
                        ),
                        **({"thumbnail_key": thumbnail_key} if thumbnail_key is not None else {}),
                    }
                ),
                current["created_by_user_id"],
            ),
        )
        updated = conn.execute(
            """UPDATE generation_tasks SET archive_status='ARCHIVED', result_asset_id=%s,
                      locked_by=NULL,locked_until=NULL,updated_at=CURRENT_TIMESTAMP
               WHERE id=%s AND locked_by=%s AND locked_until=%s
                 AND locked_until::timestamptz > clock_timestamp()""",
            (asset_id, task_id, prepared.get("locked_by"), prepared.get("locked_until")),
        )
        if updated.rowcount != 1:
            raise _archive_lease_lost()
        write_audit(
            conn,
            actor=actor,
            action="generation_task.archive",
            entity_type="generation_task",
            entity_id=task_id,
            metadata={"asset_id": asset_id, "size_bytes": stored.size, "sha256": stored.sha256},
            commit=False,
        )
        return get_task_result(conn, task_id)


def get_task_result(conn: BusinessConnection, task_id: str) -> TaskResult:
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
        WHERE id = %s
        """,
        (task_id,),
    ).fetchone()
    if row is None:
        raise LookupError("task not found")
    return task_result(row)


def list_generation_batches(
    conn: BusinessConnection,
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

    clauses = [
        "NOT EXISTS (SELECT 1 FROM customer_batch_visibility AS visibility "
        "WHERE visibility.user_id = %s AND visibility.batch_id = batch.id)"
    ]
    parameters: list[object] = [actor.id]
    if project_id is not None:
        clauses.append("batch.project_id = %s")
        parameters.append(project_id)
    elif actor.role in {"employee", "customer"}:
        clauses.append(
            "(project.owner_user_id = %s OR (batch.project_id IS NULL "
            "AND batch.created_by_user_id = %s))"
        )
        parameters.extend([actor.id, actor.id])
    if created_by_user_id is not None:
        clauses.append("batch.created_by_user_id = %s")
        parameters.append(created_by_user_id)
    if status is not None:
        clauses.append("batch.status = %s")
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
                  )
            )
            """
        )
    count_clauses = list(clauses)
    count_parameters = list(parameters)
    if cursor_position is not None:
        cursor_created_at, cursor_batch_id = cursor_position
        clauses.append("(batch.created_at < %s OR (batch.created_at = %s AND batch.id < %s))")
        parameters.extend([cursor_created_at, cursor_created_at, cursor_batch_id])
    parameters.append(limit + 1)

    rows = conn.execute(
        f"""
        WITH scoped_total AS (
            SELECT COUNT(*) AS total
            FROM generation_batches AS batch
            LEFT JOIN projects AS project ON project.id = batch.project_id
            WHERE {" AND ".join(count_clauses)}
        )
        SELECT
            batch.id,
            batch.project_id,
            COALESCE(project.name, '') AS project_name,
            batch.created_by_user_id,
            creator.display_name AS created_by_display_name,
            batch.request_snapshot_json,
            batch.status,
            batch.created_at,
            batch.updated_at,
            batch.display_name,
            batch.source_batch_id,
            batch.source_task_id,
            batch.generation_reason,
            batch.creation_kind,
            scoped_total.total AS page_total
        FROM generation_batches AS batch
        LEFT JOIN projects AS project ON project.id = batch.project_id
        JOIN users AS creator ON creator.id = batch.created_by_user_id
        CROSS JOIN scoped_total
        WHERE {" AND ".join(clauses)}
        ORDER BY batch.created_at DESC, batch.id DESC
        LIMIT %s
        """,
        tuple([*count_parameters, *parameters]),
    ).fetchall()
    page_rows = rows[:limit]
    if not page_rows:
        total_row = conn.execute(
            f"""
            SELECT COUNT(*) AS total
            FROM generation_batches AS batch
            LEFT JOIN projects AS project ON project.id = batch.project_id
            WHERE {" AND ".join(count_clauses)}
            """,
            tuple(count_parameters),
        ).fetchone()
        total = int(total_row["total"]) if total_row is not None else 0
        return GenerationBatchListPage(items=[], next_cursor=None, total=total)
    total = int(page_rows[0]["page_total"])

    batch_ids = [str(row["id"]) for row in page_rows]
    placeholders = ", ".join("%s" for _ in batch_ids)
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
    credits_by_batch = batch_credits_by_batch(conn, batch_ids)

    items: list[GenerationBatchListItem] = []
    for row in page_rows:
        batch_id = str(row["id"])
        tasks = tasks_by_batch[batch_id]
        progress = calculate_progress(tasks)
        items.append(
            GenerationBatchListItem(
                id=batch_id,
                project_id=(None if row["project_id"] is None else str(row["project_id"])),
                project_name=str(row["project_name"]),
                created_by_user_id=str(row["created_by_user_id"]),
                created_by_display_name=str(row["created_by_display_name"]),
                prompt_version_id=request_prompt_version_id(row["request_snapshot_json"]),
                status=batch_status(str(row["status"]), progress),
                quantity=len(tasks),
                created_at=str(row["created_at"]),
                updated_at=str(row["updated_at"]),
                display_name=batch_display_name(
                    row["display_name"], row["creation_kind"], row["project_name"]
                ),
                source_batch_id=optional_text(row["source_batch_id"]),
                source_task_id=optional_text(row["source_task_id"]),
                generation_reason=optional_text(row["generation_reason"]),
                creation_kind=str(row["creation_kind"]),
                progress=progress,
                credits=credits_by_batch.get(batch_id, BatchCredits()),
                total_estimated_cost=optional_cost_total([task.estimated_cost for task in tasks]),
                total_actual_cost=optional_cost_total([task.actual_cost for task in tasks]),
                needs_attention_count=progress.counts["needs_attention"],
                has_results=any(
                    task.result_asset_id is not None or task.direct_result_available
                    for task in tasks
                ),
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
    return GenerationBatchListPage(items=items, next_cursor=next_cursor, total=total)


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
    conn: BusinessConnection,
    *,
    batch_id: str,
    actor: CurrentUser,
) -> BatchResult:
    batch = conn.execute(
        """
        SELECT id, project_id, created_by_user_id, request_snapshot_json, status,
               display_name, source_batch_id, source_task_id, generation_reason,
               creation_kind,
               (SELECT name FROM projects WHERE id=generation_batches.project_id) AS project_name
        FROM generation_batches
        WHERE id = %s
        """,
        (batch_id,),
    ).fetchone()
    if batch is None:
        raise generation_error(404, "BATCH_NOT_FOUND", "Generation batch does not exist.")
    if batch["project_id"] is None:
        # 独立创作批次：归属人是唯一可见方（管理员除外），不存在项目门禁。
        if actor.role != "admin" and str(batch["created_by_user_id"]) != actor.id:
            raise generation_error(404, "BATCH_NOT_FOUND", "Generation batch does not exist.")
    else:
        try:
            require_project_access(
                conn,
                actor=actor,
                project_id=str(batch["project_id"]),
                action="generation_batch.read",
            )
        except HTTPException as exc:
            if exc.status_code == 404:
                raise remap_security_denial(
                    exc,
                    status_code=404,
                    detail={
                        "code": "BATCH_NOT_FOUND",
                        "message": "Generation batch does not exist.",
                    },
                ) from exc
            raise
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
        WHERE batch_id = %s
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
    stale = False
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
        project_id=(None if batch["project_id"] is None else str(batch["project_id"])),
        project_name=optional_text(batch["project_name"]),
        prompt_version_id=prompt_version_id,
        status=status,
        quantity=len(tasks),
        stale=stale,
        display_name=batch_display_name(
            batch["display_name"], batch["creation_kind"], batch["project_name"]
        ),
        source_batch_id=optional_text(batch["source_batch_id"]),
        source_task_id=optional_text(batch["source_task_id"]),
        generation_reason=optional_text(batch["generation_reason"]),
        creation_kind=str(batch["creation_kind"]),
        progress=progress,
        credits=batch_credits_by_batch(conn, [batch_id]).get(batch_id, BatchCredits()),
        tasks=tasks,
    )


def rename_generation_batch(
    conn: BusinessConnection,
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
        WHERE id = %s
        """,
        (batch_id,),
    ).fetchone()
    if batch is None:
        raise generation_error(404, "BATCH_NOT_FOUND", "Generation batch does not exist.")
    if actor.role != "admin" and str(batch["created_by_user_id"]) != actor.id:
        raise generation_error(
            404,
            "BATCH_NOT_FOUND",
            "Generation batch does not exist.",
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
            SET display_name = %s, updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
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


def cancel_generation_batch(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    batch_id: str,
) -> BatchResult:
    """Cancel a QUEUED batch before any task leaves PENDING.

    只有还在排队的批次可以取消：任何一个任务一旦被 worker 认领
    （SUBMITTING）就可能已经产生付费提交，取消窗口即关闭。取消在
    BEGIN IMMEDIATE 事务里把批次与任务一起置 CANCELLED，并对每个
    任务走 internal-billing RELEASE 归还预扣积分（与 worker 终态写
    共用同一套账务收口）。
    """
    require_not_auditor(
        conn,
        actor=actor,
        action="generation_batch.cancel",
        entity_type="generation_batch",
        entity_id=batch_id,
    )
    batch = conn.execute(
        """
        SELECT id, project_id, created_by_user_id, status
        FROM generation_batches
        WHERE id = %s
        """,
        (batch_id,),
    ).fetchone()
    if batch is None:
        raise generation_error(404, "BATCH_NOT_FOUND", "Generation batch does not exist.")
    # 先按可见范围鉴权：看不到的批次一律 404，不泄露存在性。
    require_batch_access(
        conn,
        actor=actor,
        project_id=(None if batch["project_id"] is None else str(batch["project_id"])),
        created_by_user_id=str(batch["created_by_user_id"]),
        action="generation_batch.cancel",
    )
    # 委派项目里能看到他人提交的批次，但取消（释放对方的预扣）只允许提交者本人；
    # 此前这里回 404「批次不存在」，用户明明看得到却被告知不存在。
    if actor.role != "admin" and str(batch["created_by_user_id"]) != actor.id:
        raise generation_error(
            403,
            "BATCH_CANCEL_FORBIDDEN",
            "只能取消自己提交的任务。",
        )
    if str(batch["status"]) in TERMINAL_STATUSES:
        raise generation_error(
            409,
            "BATCH_ALREADY_TERMINAL",
            "This batch is already finished and cannot be cancelled.",
        )
    if str(batch["status"]) != "QUEUED":
        raise generation_error(
            409,
            "BATCH_NOT_CANCELLABLE",
            "Only queued batches can be cancelled.",
        )

    try:
        task_rows = conn.execute(
            """
            SELECT id, status FROM generation_tasks
            WHERE batch_id = %s
            """,
            (batch_id,),
        ).fetchall()
        non_pending = [str(row["id"]) for row in task_rows if str(row["status"]) != "PENDING"]
        if non_pending:
            conn.rollback()
            raise generation_error(
                409,
                "BATCH_ALREADY_ACTIVE",
                "A task in this batch is already being submitted or generated.",
            )
        cancelled_batch = conn.execute(
            """
            UPDATE generation_batches
            SET status = 'CANCELLED', updated_at = CURRENT_TIMESTAMP
            WHERE id = %s AND status = 'QUEUED'
            RETURNING id
            """,
            (batch_id,),
        ).fetchone()
        if cancelled_batch is None:
            raise generation_error(
                409,
                "BATCH_NOT_CANCELLABLE",
                "Only queued batches can be cancelled.",
            )
        cancelled_rows = conn.execute(
            """
            UPDATE generation_tasks
            SET status = 'CANCELLED', updated_at = CURRENT_TIMESTAMP
            WHERE batch_id = %s AND status = 'PENDING'
            RETURNING id
            """,
            (batch_id,),
        ).fetchall()
        # A worker may claim after the initial read. PostgreSQL rechecks the
        # PENDING predicate after acquiring each row lock, so require the
        # complete batch before releasing any credit. The outer transaction
        # rolls back both the batch update and any partial task cancellation.
        if {str(row["id"]) for row in cancelled_rows} != {str(row["id"]) for row in task_rows}:
            raise generation_error(
                409,
                "BATCH_ALREADY_ACTIVE",
                "A task in this batch is already being submitted or generated.",
            )
        for row in task_rows:
            finalize_internal_billing(conn, task_id=str(row["id"]), outcome="failed")
        write_audit(
            conn,
            actor=actor,
            action="generation_batch.cancel",
            entity_type="generation_batch",
            entity_id=batch_id,
            metadata={
                # 独立创作批次没有项目：写 None 而不是字符串 "None"。
                "project_id": batch["project_id"],
                "cancelled_task_count": len(task_rows),
            },
            commit=False,
        )
        conn.commit()
    except HTTPException:
        raise
    except Exception:
        conn.rollback()
        raise
    return get_generation_batch(conn, batch_id=batch_id, actor=actor)


def build_h3_request(
    *,
    prompt_text: str,
    first_frame_url: str | None = None,
    duration_seconds: int,
    resolution: str,
    ratio: str = "adaptive",
    last_frame_url: str | None = None,
    reference_images: Sequence[Mapping[str, str]] = (),
    reference_videos: Sequence[Mapping[str, str]] = (),
    reference_audios: Sequence[Mapping[str, str]] = (),
) -> dict[str, Any]:
    """Build an H3 request for any generation mode.

    I2V (default, 复刻流契约): text + first_frame（可选 last_frame）。
    T2V: 仅 text。R2V: text + 多模态参考（reference_image / reference_video /
    reference_audio）编排，不得携带首尾帧（H3 输入互斥规则）。参考视频/音频的
    content 元素结构与秘塔前端一致：``{type, video_url|audio_url: {url}, role}``。
    """
    if not prompt_text.strip():
        raise ValueError("prompt_text is required")
    _validate_h3_duration(resolution, duration_seconds)
    if resolution not in SUPPORTED_RESOLUTIONS:
        raise ValueError("resolution must be 768P or 2K")
    if ratio not in SUPPORTED_RATIOS:
        raise ValueError("ratio is unsupported")
    content: list[dict[str, Any]] = [{"type": "text", "text": prompt_text}]
    has_reference = bool(reference_images or reference_videos or reference_audios)
    has_frame = bool(first_frame_url or last_frame_url)
    # 供应商 ratio 契约（2026-09-19 对 MiniMax-H3 真实付费核对，见
    # 离线比例探针实测）：
    #   T2VA（仅文本）：ratio 必填且不能为 adaptive，否则供应商 400（err 2013）。
    #   I2VA/FL2VA/L2VA（含首/尾帧）：ratio 恒为 adaptive，传具体值不报错但被
    #     静默忽略、按首帧图片比例渲染——这里统一归一为 adaptive，使发出的请求
    #     与供应商真实行为一致，避免快照记录一个不会生效的比例。
    #   Ref2VA（含参考素材）：ratio 可选、默认 adaptive，具体比例被供应商尊重，透传。
    if has_reference:
        effective_ratio = ratio
    elif has_frame:
        effective_ratio = "adaptive"
    else:
        if ratio == "adaptive":
            raise ProviderRequestContractError(
                "text-to-video (T2V) requires a concrete ratio; "
                "adaptive is not supported by the provider"
            )
        effective_ratio = ratio
    if has_reference:
        if first_frame_url or last_frame_url:
            raise ValueError("reference mode must not carry first/last frame")
        for image in reference_images:
            name = str(image.get("name", "")).strip()
            url = str(image.get("url", "")).strip()
            if not name or not url:
                raise ValueError("reference image requires a name and a url")
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": url},
                    "role": "reference_image",
                    "name": name,
                }
            )
        for video in reference_videos:
            url = str(video.get("url", "")).strip()
            if not url:
                raise ValueError("reference video requires a url")
            content.append(
                {
                    "type": "video_url",
                    "video_url": {"url": url},
                    "role": "reference_video",
                }
            )
        for audio in reference_audios:
            url = str(audio.get("url", "")).strip()
            if not url:
                raise ValueError("reference audio requires a url")
            content.append(
                {
                    "type": "audio_url",
                    "audio_url": {"url": url},
                    "role": "reference_audio",
                }
            )
    else:
        for role, frame_url in (("first_frame", first_frame_url), ("last_frame", last_frame_url)):
            if frame_url:
                content.append({"type": "image_url", "image_url": {"url": frame_url}, "role": role})
    return {
        "model": H3_MODEL,
        "content": content,
        "resolution": resolution,
        "duration": duration_seconds,
        "ratio": effective_ratio,
    }


def _validate_h3_duration(resolution: str, duration: Any) -> None:
    # 上游契约：单次 4–30 秒整数；16–30 秒走长视频规则（按基础费用 2 倍计费），
    # 2K 分辨率目前最长 15 秒——超档组合在提交前本地拒绝，不烧一次不确定的调用。
    if not isinstance(duration, int) or isinstance(duration, bool) or not 4 <= duration <= 30:
        raise ValueError("H3 duration must be 4-30 seconds")
    if duration > 15 and str(resolution).upper() == "2K":
        raise ValueError("H3 2K resolution currently supports up to 15 seconds")


def validate_h3_request(request: dict[str, Any]) -> None:
    content = request.get("content")
    if not isinstance(content, list) or not content:
        raise ValueError("H3 I2V content must contain text and first_frame only")
    if content[0].get("type") != "text" or not str(content[0].get("text", "")).strip():
        raise ValueError("H3 I2V content requires non-empty text")
    roles = [str(item.get("role")) if isinstance(item, dict) else None for item in content[1:]]
    _validate_h3_duration(str(request.get("resolution", "768P")), request.get("duration"))
    if request.get("ratio") not in SUPPORTED_RATIOS:
        raise ValueError("H3 ratio is unsupported")
    if not roles:
        return  # T2V：仅文本。
    if roles == ["last_frame"]:
        _validate_h3_image_element(content[1], expected_role="last_frame")
        return
    if roles == ["first_frame"]:
        _validate_h3_image_element(content[1], expected_role="first_frame")
        return
    if roles == ["first_frame", "last_frame"]:
        _validate_h3_image_element(content[1], expected_role="first_frame")
        _validate_h3_image_element(content[2], expected_role="last_frame")
        return
    if any(role in REFERENCE_ROLE_MEDIA_KEYS for role in roles):
        _validate_h3_reference_request(request, content, roles)
        return
    raise ValueError("H3 I2V content must contain text and first_frame only")


def _validate_h3_image_element(element: Any, *, expected_role: str) -> None:
    # HTTPS 强校验只在真实 metaso 提交路径（_h3_request_media_urls_are_https）；
    # 本地/fake 存储的签名 URL 走 fake:// 协议，这里只校验角色契约。
    if not isinstance(element, dict) or element.get("role") != expected_role:
        raise ValueError(f"H3 I2V image must use {expected_role} role")
    if not isinstance(element.get("image_url"), dict):
        raise ValueError(f"H3 {expected_role} image requires an image_url object")


def _validate_h3_reference_request(
    request: dict[str, Any],
    content: list[Any],
    roles: list[str | None],
) -> None:
    if "first_frame" in roles or "last_frame" in roles:
        raise ValueError("H3 R2V content must not mix reference objects with first/last frame")
    if len(content) < 2:
        raise ValueError("H3 R2V content requires at least one reference object")
    for element, role in zip(content[1:], roles):
        media_key = REFERENCE_ROLE_MEDIA_KEYS.get(role) if isinstance(role, str) else None
        if media_key is None:
            raise ValueError(
                "H3 R2V supports reference_image/reference_video/reference_audio objects only"
            )
        if not isinstance(element, dict):
            raise ValueError("H3 R2V reference object must be a JSON object")
        # 参考图片沿用既有命名契约（供 UI 标注用途）；参考视频/音频与秘塔前端一致，
        # 不强制 name，只要求媒体 url。
        if role == "reference_image" and not str(element.get("name", "")).strip():
            raise ValueError("H3 R2V reference image requires a name")
        media = element.get(media_key)
        if not isinstance(media, dict) or not str(media.get("url", "")).strip():
            raise ValueError(f"H3 R2V {role} object requires a url")
    if request.get("ratio") not in SUPPORTED_RATIOS:
        raise ValueError("H3 R2V ratio is unsupported")
    _validate_h3_duration(str(request.get("resolution", "768P")), request.get("duration"))


def _metaso_json_object(content: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise H3ProviderFailed("H3 provider returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise H3ProviderFailed("H3 provider returned an invalid response object")
    return cast(dict[str, Any], payload)


def _metaso_task_id(content: bytes) -> str:
    task_id = _metaso_json_object(content).get("task_id")
    if not isinstance(task_id, str) or not task_id:
        raise H3ProviderFailed("H3 provider create response is missing task_id")
    return task_id


def _extract_metaso_task(payload: dict[str, Any], provider_task_id: str) -> dict[str, Any]:
    """把秘塔查询响应归一化为单个任务字典。

    秘塔原生查询端点 ``/v2/query/video_generation/{task_id}`` 返回单任务对象，
    这里兼容三种形态：官方 ``{"task": {...}}`` 包装、裸任务对象（含 status/id），
    以及历史 ``{"items": [...]}`` 列表（按 task_id 精确匹配，绝不把无关任务当结果）。
    无法识别时返回空字典，由调用方按 RUNNING 继续轮询。
    """
    task = payload.get("task")
    if isinstance(task, dict):
        return cast(dict[str, Any], task)
    if isinstance(payload.get("status"), str) or isinstance(payload.get("id"), str):
        return payload
    items = payload.get("items")
    if isinstance(items, list):
        for item in items:
            if isinstance(item, dict) and item.get("id") == provider_task_id:
                return cast(dict[str, Any], item)
    return {}


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
        raise H3ProviderFailed("H3 provider result URL hostname could not be resolved") from exc
    if not addresses:
        raise H3ProviderFailed("H3 provider result URL hostname could not be resolved")
    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])
        if not ip.is_global:
            raise H3ProviderFailed("H3 result URL must resolve to a public address", terminal=True)


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
            "H3 completed task is missing an HTTPS result URL",
            provider_task_id=provider_task_id,
            terminal=True,
        )
    _require_public_https_host(parsed.hostname)
    return result_url


def _metaso_output_seconds(item: dict[str, Any]) -> float | None:
    usage = item.get("usage")
    if not isinstance(usage, dict):
        return None
    value = usage.get("output_seconds")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    seconds = float(value)
    return seconds if isfinite(seconds) and seconds >= 0 else None


def _h3_request_media_urls_are_https(request: dict[str, Any]) -> bool:
    """所有输入媒体（首尾帧/参考图片、参考视频、参考音频）都必须是 HTTPS 公网 URL。

    纯文本 T2V 请求没有媒体元素，直接通过。秘塔需要据此公网拉取素材，
    非 HTTPS（含 fake:// 本地签名 URL）一律拒绝，避免提交后供应商无法访问。
    """
    content = request.get("content")
    if not isinstance(content, list):
        return False
    media_field_by_type = {
        "image_url": "image_url",
        "video_url": "video_url",
        "audio_url": "audio_url",
    }
    for item in content[1:]:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        media_field = media_field_by_type.get(item_type) if isinstance(item_type, str) else None
        if media_field is None:
            continue
        media = item.get(media_field)
        value = media.get("url") if isinstance(media, dict) else None
        parsed = urlparse(value) if isinstance(value, str) else None
        if not (parsed and parsed.scheme == "https" and parsed.hostname):
            return False
    return True


def inspect_generated_video_quality(
    *,
    content: bytes,
    first_frame: ImageInput,
    inspector: FirstFrameQualityInspector,
    frame_extractor: Callable[[bytes], list[ImageInput]] | None = None,
) -> GeneratedVideoQualityResult:
    try:
        sampled_frames = (frame_extractor or extract_generated_video_frames)(content)
        inspection = inspector.inspect_generated_video(
            first_frame=first_frame,
            sampled_frames=sampled_frames,
        )
    except Exception as exc:
        logger.warning("generated-video visual validation unavailable: %s", type(exc).__name__)
        raise GeneratedVideoValidationUnavailable(
            "generated-video visual validation is temporarily unavailable"
        ) from exc
    return evaluate_generated_video_quality(inspection)


def final_generation_quality(
    *,
    content: bytes,
    first_frame_uri: str,
    first_frame_storage: StorageAdapter,
    inspector: FirstFrameQualityInspector | None,
    audio_quality_status: Literal["AUDIO_OK", "AUDIO_QUALITY_FAILED"],
    quality_issue_codes: list[str],
    frame_extractor: Callable[[bytes], list[ImageInput]] | None = None,
) -> tuple[str, list[str]]:
    if inspector is None:
        return audio_quality_status, list(quality_issue_codes)
    try:
        reference = storage_object_ref_from_uri(first_frame_uri)
        require_storage_match(first_frame_storage, reference)
        metadata = first_frame_storage.head_object(reference.key)
        first_frame = ImageInput(
            content=first_frame_storage.get_object(reference.key),
            content_type=metadata.content_type if metadata is not None else "image/png",
            filename=Path(reference.key).name or "first-frame.png",
        )
    except Exception as exc:
        logger.warning("generated-video first-frame loading failed: %s", type(exc).__name__)
        raise GeneratedVideoValidationUnavailable(
            "generated-video first-frame input is temporarily unavailable"
        ) from exc
    visual = inspect_generated_video_quality(
        content=content,
        first_frame=first_frame,
        inspector=inspector,
        frame_extractor=frame_extractor,
    )
    issues = list(dict.fromkeys([*quality_issue_codes, *visual.issue_codes]))
    if not visual.passed:
        return "VISUAL_QUALITY_FAILED", issues
    return audio_quality_status, issues


def extract_generated_video_frames(content: bytes) -> list[ImageInput]:
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if ffmpeg is None or ffprobe is None:
        raise GeneratedVideoValidationUnavailable("ffmpeg and ffprobe are required")
    with tempfile.TemporaryDirectory() as directory:
        video_path = Path(directory) / "generated.mp4"
        video_path.write_bytes(content)
        try:
            duration_result = subprocess.run(
                [
                    ffprobe,
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    str(video_path),
                ],
                capture_output=True,
                check=False,
                text=True,
                timeout=5,
            )
            duration = float(duration_result.stdout.strip())
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            raise GeneratedVideoValidationUnavailable("video duration is unavailable") from exc
        if duration_result.returncode != 0 or duration <= 0:
            raise GeneratedVideoValidationUnavailable("video duration is invalid")
        frames: list[ImageInput] = []
        for index, position in enumerate((0.05, 0.25, 0.5, 0.75, 0.95), start=1):
            try:
                result = subprocess.run(
                    [
                        ffmpeg,
                        "-v",
                        "error",
                        "-ss",
                        f"{duration * position:.3f}",
                        "-i",
                        str(video_path),
                        "-frames:v",
                        "1",
                        "-f",
                        "image2pipe",
                        "-vcodec",
                        "mjpeg",
                        "pipe:1",
                    ],
                    capture_output=True,
                    check=False,
                    timeout=10,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise GeneratedVideoValidationUnavailable(
                    "generated-video frame extraction failed"
                ) from exc
            if result.returncode != 0 or not result.stdout:
                raise GeneratedVideoValidationUnavailable("generated-video frame extraction failed")
            frames.append(
                ImageInput(
                    content=result.stdout,
                    content_type="image/jpeg",
                    filename=f"generated-{index}.jpg",
                )
            )
    return frames


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
    conn: BusinessConnection,
    *,
    version_id: str,
    project_id: str,
    kind: str,
) -> BusinessRow:
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
    sentences = [part for part in re.split(r"(?<=[。！？!？])", text) if part]
    if not sentences and text:
        sentences = [text]
    spoken_segment_indexes = [
        index for index, shot in enumerate(shots) if str(shot.get("spoken_text") or "").strip()
    ]
    # Legacy/manual shot cards may not carry per-segment narration. In that
    # case retain the old all-segments distribution. When narration evidence
    # exists, action-only beats stay silent instead of consuming the next line.
    target_indexes = spoken_segment_indexes or list(range(len(shots)))
    text_by_index: dict[int, str] = {}
    for target_position, shot_index in enumerate(target_indexes):
        segment_text = sentences[target_position] if target_position < len(sentences) else ""
        if target_position == len(target_indexes) - 1 and len(sentences) > len(target_indexes):
            segment_text = "".join(sentences[target_position:])
        text_by_index[shot_index] = segment_text
    mappings: list[dict[str, Any]] = []
    for index, shot in enumerate(shots):
        segment_text = text_by_index.get(index, "")
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


def _format_shot_timestamp(seconds: float) -> str:
    """秒数转 MM:SS.mmm，对齐 MiniMax H3 官方提示词指南的时间戳格式。"""
    total_ms = round(max(0.0, seconds) * 1000)
    minutes, remainder_ms = divmod(total_ms, 60_000)
    secs, ms = divmod(remainder_ms, 1000)
    return f"{minutes:02d}:{secs:02d}.{ms:03d}"


def build_style_clause(shot_payload: Mapping[str, Any]) -> str | None:
    """把拆解出的整体风格字段拼成 style 段。

    任一字段缺失就跳过该子句；全部缺失（旧镜头卡，落库时还没有这些
    顶层字段）时返回 None，调用方直接跳过整个 style 段，不留空行。
    """
    labelled_fields = (
        ("visual_style", "整体视觉风格"),
        ("color_tone", "主色调"),
        ("pace", "剪辑节奏"),
        ("camera_language", "运镜语言基调"),
    )
    parts = [
        f"{label}：{value}"
        for field, label in labelled_fields
        if (value := str(shot_payload.get(field) or "").strip())
    ]
    if not parts:
        return None
    return "；".join(parts) + "；全片风格统一，中途不得切换。"


def shot_wardrobe_clause(shot: Mapping[str, Any]) -> str:
    """非身份类外观细节（服装/配饰/姿态），插入主体子句；缺失时不插入。"""
    detail = str(shot.get("wardrobe_pose_detail") or "").strip()
    if not detail or detail == "无人物出镜":
        return ""
    return f"，本镜头新增细节：{detail}"


def shot_scene_text(shot: Mapping[str, Any]) -> str:
    """场景描述 = scene + 可选的背景陈设 + 可选的光线质感。"""
    parts = [str(shot.get("scene", ""))]
    for field in ("scene_dressing", "scene_lighting"):
        value = str(shot.get(field) or "").strip()
        if value:
            parts.append(value)
    return "，".join(parts)


def build_audio_clause(shot_payload: Mapping[str, Any]) -> str:
    """汇总各镜头的环境音/配乐倾向；旧镜头卡没有这些字段时返回空串，
    audio 段退化为原 outro 常量文案（行为不劣化）。
    """
    ambient_sounds: list[str] = []
    music_hints: list[str] = []
    for shot in shot_payload.get("shots", []):
        if not isinstance(shot, Mapping):
            continue
        ambient = str(shot.get("ambient_sound") or "").strip()
        if ambient and ambient not in ("无", "无人物出镜") and ambient not in ambient_sounds:
            ambient_sounds.append(ambient)
        music = str(shot.get("music_style_hint") or "").strip()
        if music and music not in music_hints:
            music_hints.append(music)
    parts = []
    if ambient_sounds:
        parts.append(f"环境音：{'；'.join(ambient_sounds)}")
    if music_hints:
        parts.append(f"配乐风格：{'；'.join(music_hints)}")
    return "；".join(parts) + "；" if parts else ""


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
    style_clause = build_style_clause(shot_payload)
    if style_clause is not None:
        lines.append(H3_PROMPT_TEMPLATES["style"].format(style_clause=style_clause))
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
                start=_format_shot_timestamp(start),
                end=_format_shot_timestamp(end),
                shot_type=shot["shot_type"],
                composition=shot["composition"],
                camera_motion=shot_camera_motion_text(shot),
                wardrobe_clause=shot_wardrobe_clause(shot),
                motion_clause=render_shot_motion_clause(shot),
                scene=shot_scene_text(shot),
                transition=shot["transition"],
                spoken=spoken,
            )
        )
    lines.append(H3_PROMPT_TEMPLATES["script"].format(full_text=script_payload["full_text"]))
    lines.append(H3_PROMPT_TEMPLATES["audio"].format(audio_clause=build_audio_clause(shot_payload)))
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
        "first_frame_reconstruction_mode": prompt_snapshot.get("first_frame_reconstruction_mode"),
        "first_frame_replace_scene": prompt_snapshot.get("first_frame_replace_scene") is True,
        "character_contract": prompt_snapshot.get("character_contract"),
        "project_character_appearance_version_id": prompt_snapshot.get(
            "project_character_appearance_version_id"
        ),
        "first_frame_asset_id": request.first_frame_asset_id,
        "output_duration_seconds": request.output_duration_seconds,
        "resolution": request.resolution,
        "ratio": request.ratio,
        "provider": request.provider,
        "model": H3_MODEL,
        "fake_audio_quality": request.fake_audio_quality,
    }


def idempotency_request_hash(request: GenerationBatchRequest) -> str:
    payload = request.model_dump(
        mode="json", exclude={"idempotency_key", "quote_token", "max_credits"}
    )
    if request.prompt_version_id is not None:
        for key in ("prompt_text", "prompt_context"):
            payload.pop(key, None)
    return content_hash(json.dumps(payload, ensure_ascii=True, sort_keys=True))


def read_runtime_limits(conn: BusinessConnection) -> dict[str, int]:
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


def lock_shared_generation_capacity(conn: BusinessConnection) -> None:
    """Take the first lock in every oral/generation slot transition."""
    if conn.is_postgres:
        row = conn.execute("SELECT id FROM runtime_settings WHERE id = 1 FOR UPDATE").fetchone()
        if row is None:
            raise RuntimeError("runtime settings capacity row is missing")
    else:
        updated = conn.execute("UPDATE runtime_settings SET id = id WHERE id = 1")
        if updated.rowcount != 1:
            raise RuntimeError("runtime settings capacity row is missing")


def shared_generation_capacity_available(
    conn: BusinessConnection,
    *,
    max_concurrent_tasks: int,
) -> bool:
    """Serialize capacity checks before taking any cursor or task lock.

    Both generation and oral claims use the lock order ``capacity row -> user
    cursor -> task``. PostgreSQL cannot let two workers observe the same last
    slot; SQLite's no-op UPDATE acquires its single-writer lock.
    """
    lock_shared_generation_capacity(conn)
    from app.h3_account_pool import pool_capacity

    # Managed H3 accounts have independent upstream slots. Oral/fake work keeps
    # its existing local budget and cannot consume those account entitlements.
    provider_filter = "AND provider != 'metaso'" if pool_capacity(conn) is not None else ""
    active_count = conn.execute(
        f"""
        SELECT (
            SELECT COUNT(*) FROM generation_tasks
            WHERE status IN ('SUBMITTING', 'RUNNING', 'ARCHIVING') {provider_filter}
        ) + (
            SELECT COUNT(*) FROM oral_tasks
            WHERE status IN ('SUBMITTING', 'RUNNING', 'ARCHIVING')
        )
        """
    ).fetchone()[0]
    return int(active_count) < max_concurrent_tasks


def generation_runtime_limits(conn: BusinessConnection) -> GenerationRuntimeLimits:
    runtime = read_runtime_limits(conn)
    return GenerationRuntimeLimits(
        max_quantity=runtime["max_generation_count_per_batch"],
    )


def generation_price_quote(
    conn: BusinessConnection,
    *,
    resolution: Literal["768P", "2K"],
    duration_seconds: int,
    quantity: int,
    user_id: str | None = None,
    specification: dict[str, Any] | None = None,
) -> GenerationPriceQuote:
    from decimal import ROUND_CEILING, Decimal

    from app.billing_catalog import generation_service_for, snapshot_discount_rate
    from app.quote_contracts import issue_quote

    if duration_seconds > 15 and resolution == "2K":
        # 2K 不存在长档：不给出无法成交的报价，避免客户拿着报价过不了提交闸。
        raise generation_error(
            422, "DURATION_RESOLUTION_UNSUPPORTED", "2K 分辨率目前最长支持 15 秒。"
        )
    subject = generation_service_for(resolution, duration_seconds)
    if duration_seconds > 15:
        # 长档未启用时不给报价：0 积分报价会误导客户以为免费，提交闸也会拒绝。
        from app.billing_catalog import read_tariff

        tariff = read_tariff(conn, subject)
        if tariff is None or not tariff.enabled or not tariff.unit_credits:
            raise generation_error(
                422,
                "LONG_DURATION_TIER_DISABLED",
                "16-30 秒时长档暂未开放，请选择 15 秒及以内的时长。",
            )
    contract = issue_quote(
        conn,
        user_id=user_id or "",
        service=subject,
        units=duration_seconds,
        quantity=quantity,
        specification=specification,
    )
    snapshot = contract["pricing_snapshot"]
    estimated_seconds = duration_seconds * quantity
    estimated_credits = int(str(snapshot["credits"])) * quantity
    if estimated_credits > 2147483647:
        raise generation_error(422, "CREDIT_AMOUNT_OUT_OF_RANGE", "积分金额超出允许范围")
    unit_credits = float(str(snapshot["unit_credits"])) if snapshot["enabled"] else 0
    ratio = snapshot["points_per_yuan"]
    unit_price = (
        int(
            (Decimal(str(unit_credits)) * 100 / Decimal(str(ratio))).to_integral_value(
                rounding=ROUND_CEILING
            )
        )
        if ratio
        else 0
    )
    price_version = int(str(snapshot["version"]))
    return GenerationPriceQuote(
        resolution=resolution,
        duration_seconds=duration_seconds,
        quantity=quantity,
        price_tier="16-30s" if duration_seconds > 15 else "4-15s",
        unit_price_fen_per_second=unit_price,
        estimated_seconds=estimated_seconds,
        estimated_price_fen=unit_price * estimated_seconds,
        unit_credits=unit_credits,
        estimated_credits=estimated_credits,
        credit_price_version=price_version,
        quote_token=contract["quote_token"],
        quote_expires_at=contract["quote_expires_at"],
        max_credits=contract["max_credits"],
        discount_rate=snapshot_discount_rate(snapshot),
        discount_source=cast(str | None, snapshot["discount_source"]),
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
            if archive_status in {"ARCHIVED", "DIRECT"}:
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


def batch_credits_by_batch(
    conn: BusinessConnection, batch_ids: Sequence[str]
) -> dict[str, BatchCredits]:
    """一次查出若干批次的实扣与退回积分。

    路径是 任务 → 计费 operation（source_id 即任务 ID，仅视频科目）→ 流水，
    每一跳都走现成索引（idx_billing_operations_source、uq_billing_operation_ledger_type）；
    直接按 wallet_transactions.task_id 找会退化成全表扫描，而任务列表每 20 秒轮询一次。
    多轮计费（重试）的 operation 都算进来：「本批共退回多少」应包含每一次失败的预扣。
    """
    if not batch_ids:
        return {}
    placeholders = ", ".join("%s" for _ in batch_ids)
    rows = conn.execute(
        f"""
        SELECT task.batch_id,
               COALESCE(SUM(-tx.reserved_delta) FILTER (WHERE tx.type = 'SETTLE'), 0) AS charged,
               COALESCE(SUM(tx.available_delta) FILTER (WHERE tx.type = 'RELEASE'), 0) AS refunded
        FROM generation_tasks AS task
        JOIN billing_operations AS op
          ON op.source_id = task.id AND op.service IN ('video_768p', 'video_2k')
        JOIN wallet_transactions AS tx
          ON tx.billing_operation_id = op.id AND tx.type IN ('SETTLE', 'RELEASE')
        WHERE task.batch_id IN ({placeholders})
        GROUP BY task.batch_id
        """,  # noqa: S608
        tuple(batch_ids),
    ).fetchall()
    return {
        str(row["batch_id"]): BatchCredits(
            charged_credits=int(row["charged"]), refunded_credits=int(row["refunded"])
        )
        for row in rows
    }


def batch_status(stored_status: str, progress: BatchProgress) -> str:
    if progress.total_count == progress.terminal_count:
        # Delivery or billing uncertainty still requires attention; historical
        # optional quality checks no longer change a completed video's status.
        if progress.counts["needs_attention"]:
            return "NEEDS_ATTENTION"
        # A batch the user cancelled before any paid submission keeps its own
        # identity: it is not a failure and must not look like 待处理 work.
        if progress.counts["cancelled"] == progress.total_count:
            return "CANCELLED"
        if (
            progress.counts["failed"]
            or progress.counts["cancelled"]
            or progress.historical_counts.get("archive_failed", 0)
        ):
            return "COMPLETED_WITH_FAILURES"
        return "SUCCEEDED"
    if progress.counts["needs_attention"]:
        return "NEEDS_ATTENTION"
    if progress.terminal_count > 0 or any(
        progress.counts[stage] for stage in ("submitting", "queued", "running", "archiving")
    ):
        # QUEUED on a child task means the provider already accepted it; only a
        # batch whose children are all PENDING remains an unstarted queue item.
        return "RUNNING"
    return stored_status


def task_result(row: BusinessRow) -> TaskResult:
    summary = task_summary(row)
    prompt_snapshot = (
        None
        if row["prompt_snapshot_json"] is None
        else json.loads(str(row["prompt_snapshot_json"]))
    )
    return TaskResult(
        **summary.model_dump(),
        prompt_snapshot=prompt_snapshot,
    )


def task_summary(row: BusinessRow) -> TaskSummary:
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
        direct_result_available=(
            optional_text(row["provider_result_url"]) is not None
            and str(row["archive_status"]) in {"ARCHIVING", "DIRECT", "ARCHIVE_FAILED"}
        ),
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
    row: BusinessRow,
) -> list[Literal["RETRY", "RECONCILE", "CONFIRM_NOT_CHARGED", "REGENERATE"]]:
    status = str(row["status"])
    archive_status = str(row["archive_status"])
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
    if status == "SUCCEEDED" and archive_status in {"DIRECT", "ARCHIVED"}:
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
    if status == "SUCCEEDED" and archive_status in {"ARCHIVED", "DIRECT"}:
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


def batch_display_name(name: Any, creation_kind: Any, project_name: Any) -> str:
    explicit = (optional_text(name) or "").strip()
    if explicit:
        return explicit
    project = (optional_text(project_name) or "").strip()
    # 自动名称用中文；用户主动命名保持原样。提示词属于正文，不是作品名。
    if project and any("\u4e00" <= char <= "\u9fff" for char in project):
        return project
    return {
        "replica": "视频复刻",
        "independent": "视频生成",
        "replacement": "人物置换",
    }.get(str(creation_kind), "视频作品")


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


def generation_quality_failed(quality_status: str, issue_codes: Sequence[str]) -> bool:
    return quality_status in {"AUDIO_QUALITY_FAILED", "VISUAL_QUALITY_FAILED"} or any(
        code == "AUDIO_QUALITY_FAILED" or code.startswith("VIDEO_") for code in issue_codes
    )


def version_result(row: BusinessRow) -> VersionResult:
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


def generation_error(
    status_code: int,
    code: str,
    message: str,
    *,
    extra: dict[str, Any] | None = None,
) -> HTTPException:
    """业务错误统一从这里抛出。

    ``extra`` 用于携带机器可读的补充字段（如 ``stale_reasons``：到底哪一环变旧）。
    客户端据此把“上游内容已变化”落成具体的环节名与自救动作，而不是让用户
    自己猜该回哪一步。
    """
    detail: dict[str, Any] = {"code": code, "message": message}
    if extra:
        detail.update(extra)
    return HTTPException(status_code=status_code, detail=detail)


def require_batch_access(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    project_id: str | None,
    created_by_user_id: str | None,
    action: str,
) -> None:
    """批次/任务访问门禁：项目批走项目 ACL；独立批（project_id NULL）仅创建者与 admin。

    所有按批次行（或任务上下文行）做 ``require_project_access`` 的路径都应
    改走本助手——``str(None)`` 得到字面量 ``"None"``，会让独立批次在取消/
    预览/隐藏/重试/对账等复用端点上系统性 404。
    """
    if project_id is None:
        if not created_by_user_id or (actor.role != "admin" and created_by_user_id != actor.id):
            raise generation_error(404, "BATCH_NOT_FOUND", "Generation batch does not exist.")
        return
    require_project_access(conn, actor=actor, project_id=project_id, action=action)
